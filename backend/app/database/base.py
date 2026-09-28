"""Database engine, session factory and declarative base.

SQLite is the default and needs no setup. The schema is written in portable
SQLAlchemy 2.0 style so the same models run on PostgreSQL by changing only
``DATABASE_URL`` -- hence no SQLite-only column types anywhere in ``models.py``.

SQLite specifics handled here:

``check_same_thread=False``
    FastAPI serves requests on a thread pool; the default would reject those.

``PRAGMA foreign_keys=ON``
    SQLite ignores foreign keys unless asked, per connection. Without this the
    cascade rules in ``models.py`` are silently decorative.

``PRAGMA journal_mode=WAL``
    Lets the dashboard read while a download writes, instead of blocking.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """Apply per-connection SQLite pragmas."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        # NORMAL is durable against process crashes (only OS-level crashes can
        # lose the last transactions) and much faster for bulk bar inserts.
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_db_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Build an engine for ``url`` (defaults to the configured database)."""
    settings = get_settings()
    resolved = url or settings.resolved_database_url
    is_sqlite = resolved.startswith("sqlite")
    is_memory = ":memory:" in resolved

    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
        if is_memory:
            # A fresh in-memory database per connection would appear empty to
            # the next caller; StaticPool keeps one connection so tests see the
            # schema they just created.
            kwargs["poolclass"] = StaticPool

    engine = create_engine(resolved, **kwargs)
    if is_sqlite:
        event.listen(engine, "connect", _configure_sqlite)
    return engine


def get_engine() -> Engine:
    """Process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commit on success, roll back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_database(engine: Engine | None = None) -> Engine:
    """Create every table and index that does not exist yet.

    Safe to call repeatedly. This is schema creation only -- there is no
    migration tooling yet, so a change to ``models.py`` on an existing database
    needs either a manual ``ALTER`` or deleting ``data/quant_trader.db`` and
    re-downloading. Bars are reproducible from the providers, so dropping the
    file loses nothing but time.
    """
    from app.database import models  # noqa: F401  -- registers mappers

    target = engine or get_engine()
    Base.metadata.create_all(target)
    return target


def reset_engine() -> None:
    """Drop the cached engine and session factory.

    Used by tests and after changing ``DATABASE_URL`` at runtime.
    """
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
