"""Additive schema migrations for an existing SQLite database.

``Base.metadata.create_all`` creates missing *tables* and silently ignores missing *columns* on a
table that already exists. So adding a field to a model leaves every existing database broken in a
way that only shows up as an ``OperationalError: no such column`` the next time that table is
queried -- and for this project that would be mid-way through a scheduled run, after the download
and before the alerts.

Only additive changes live here: new nullable columns, and new columns with a default. A rename or
a type change needs a table rebuild and a considered data migration, which this does not attempt
and must not pretend to.

This runs on every ``init_database()``, so it has to be idempotent and cheap. It is both: one
``PRAGMA table_info`` per table that has pending migrations, and nothing at all once they are
applied.
"""

from __future__ import annotations

from sqlalchemy import Engine, inspect, text

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["run_migrations", "PENDING"]


PENDING: dict[str, dict[str, str]] = {
    "holdings": {
        # Recording a purchase by the cash spent rather than by a share count. See the Holding
        # model for why each is stored rather than derived on the fly.
        "entry_amount": "FLOAT",
        "entry_amount_currency": "VARCHAR(8) NOT NULL DEFAULT 'USD'",
        "entry_fx_rate": "FLOAT",
        "entry_price_estimated": "BOOLEAN NOT NULL DEFAULT 0",
    },
}
"""``{table: {column: SQL type and constraints}}``, applied in order if absent.

Entries stay here permanently rather than being deleted once applied: a database created before a
given version can turn up at any time -- restored from a backup, or pulled from an Actions cache
written weeks ago -- and the check costs nothing when there is nothing to do.
"""


def run_migrations(engine: Engine) -> list[str]:
    """Add any missing columns. Returns what was added, for logging and tests."""
    inspector = inspect(engine)
    applied: list[str] = []

    with engine.begin() as connection:
        for table, columns in PENDING.items():
            if not inspector.has_table(table):
                # create_all will build it complete; nothing to migrate.
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for column, definition in columns.items():
                if column in existing:
                    continue
                connection.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                )
                applied.append(f"{table}.{column}")

    if applied:
        logger.info("Applied %d schema migration(s): %s", len(applied), ", ".join(applied))
    return applied
