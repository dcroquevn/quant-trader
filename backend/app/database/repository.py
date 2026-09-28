"""Persistence helpers over the ORM models.

Everything here is deliberately plain: no lazy magic, no implicit commits. The
data engine and backtester read and write large bar sets, and surprises in this
layer show up as wrong prices three modules later.

The one non-obvious piece is :func:`upsert_bars`, which makes re-downloading an
overlapping window idempotent. See its docstring for the ordering rules.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.markets import MARKETS, Market as MarketSpec, get_market
from app.core.universe import AssetSpec
from app.database.models import Asset, Bar, Market

__all__ = [
    "sync_markets",
    "upsert_asset",
    "get_asset",
    "list_assets",
    "upsert_bars",
    "load_bars",
    "bar_count",
    "latest_bar_date",
    "earliest_bar_date",
    "delete_bars",
    "coverage_report",
]


# --------------------------------------------------------------------------- #
# Timestamp normalisation
# --------------------------------------------------------------------------- #


def _to_utc(value: datetime | date | pd.Timestamp) -> datetime:
    """Coerce any date-ish value to an aware UTC ``datetime``.

    A plain ``date`` becomes midnight UTC. A naive ``datetime`` is *assumed* to
    already be UTC -- the data engine is responsible for converting exchange
    time before it reaches this layer, so a naive value here means "already
    normalised", never "local time".
    """
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _naive_utc(value: datetime | date | pd.Timestamp) -> datetime:
    """UTC timestamp with the tzinfo stripped, for SQLite comparisons.

    SQLite stores ``DateTime`` as a string with no offset. Comparing a stored
    naive value against an aware bind parameter makes SQLAlchemy emit a warning
    and, worse, compares mismatched string formats. Every timestamp crossing
    into a query goes through here.
    """
    return _to_utc(value).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #


def sync_markets(session: Session, markets: dict[str, MarketSpec] | None = None) -> int:
    """Mirror the in-code market definitions into the ``markets`` table.

    Idempotent: existing rows are updated in place so foreign keys from assets
    survive. Returns the number of rows written.
    """
    source = markets if markets is not None else MARKETS
    written = 0
    for spec in source.values():
        row = session.get(Market, spec.code)
        if row is None:
            row = Market(code=spec.code)
            session.add(row)
        row.name = spec.name
        row.currency = spec.currency
        row.exchange = spec.exchange
        row.timezone = spec.timezone
        row.session_open = spec.session_open.strftime("%H:%M")
        row.session_close = spec.session_close.strftime("%H:%M")
        row.benchmark_symbol = spec.benchmark_symbol
        written += 1
    session.flush()
    return written


def upsert_asset(session: Session, spec: AssetSpec) -> Asset:
    """Insert or update the ``assets`` row for ``spec`` and return it.

    Resolution fields (``provider``, ``provider_symbol``,
    ``resolution_checked_at``) are *not* touched: they are empirical results
    owned by the data engine, and re-running the universe sync must not discard
    a verification that already succeeded.
    """
    get_market(spec.market)  # validate before writing
    row = session.scalar(
        select(Asset).where(Asset.symbol == spec.symbol, Asset.market_code == spec.market)
    )
    if row is None:
        row = Asset(symbol=spec.symbol, market_code=spec.market)
        session.add(row)

    row.name = spec.name
    row.sector = spec.sector
    row.asset_class = spec.asset_class
    row.currency = spec.currency
    row.is_benchmark = spec.is_benchmark
    row.notes = spec.notes
    session.flush()
    return row


def get_asset(session: Session, symbol: str, market: str | None = None) -> Asset | None:
    """Fetch one asset by canonical symbol, optionally scoped to a market."""
    stmt = select(Asset).where(Asset.symbol == symbol.strip().upper())
    if market is not None:
        stmt = stmt.where(Asset.market_code == get_market(market).code)
    return session.scalars(stmt).first()


def list_assets(
    session: Session,
    *,
    market: str | None = None,
    include_benchmarks: bool = False,
    only_active: bool = True,
) -> list[Asset]:
    stmt = select(Asset)
    if market is not None:
        stmt = stmt.where(Asset.market_code == get_market(market).code)
    if not include_benchmarks:
        stmt = stmt.where(Asset.is_benchmark.is_(False))
    if only_active:
        stmt = stmt.where(Asset.is_active.is_(True))
    return list(session.scalars(stmt.order_by(Asset.market_code, Asset.symbol)))


# --------------------------------------------------------------------------- #
# Bars
# --------------------------------------------------------------------------- #

_BAR_COLUMNS = ("open", "high", "low", "close", "adj_close", "volume")


def upsert_bars(
    session: Session,
    asset_id: int,
    timeframe: str,
    frame: pd.DataFrame,
    *,
    source: str,
    is_adjusted: bool,
) -> tuple[int, int]:
    """Insert or update bars for one asset/timeframe. Returns ``(written, skipped)``.

    ``frame`` must be indexed by timestamp and carry ``open/high/low/close/volume``
    columns, optionally ``adj_close``.

    Three things happen in order, and the order matters:

    1. **Duplicate timestamps are collapsed**, keeping the last occurrence.
       Providers occasionally return a partially-formed bar for the current
       session alongside the settled one; the later row is the fresher one.
    2. **Invalid rows are dropped, not fixed.** A bar with a NaN close, a
       non-positive price or ``high < low`` is discarded and counted in
       ``skipped``. Interpolating would fabricate prices, which is worse than a
       gap: a gap is visible, invented data is not.
    3. **Remaining rows are upserted** on ``(asset_id, timeframe, ts)``.

    The write uses SQLite's ``ON CONFLICT DO UPDATE`` when available, and falls
    back to a portable delete-then-insert on other dialects.
    """
    if frame is None or frame.empty:
        return 0, 0

    df = frame.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Bar frame for asset {asset_id} is missing columns {sorted(missing)}; "
            f"got {sorted(df.columns)}"
        )
    if "volume" not in df.columns:
        df["volume"] = 0.0
    if "adj_close" not in df.columns:
        df["adj_close"] = None

    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    total_before = len(df)
    numeric = ["open", "high", "low", "close", "volume"]
    df[numeric] = df[numeric].apply(pd.to_numeric, errors="coerce")
    df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")

    valid = (
        df[["open", "high", "low", "close"]].notna().all(axis=1)
        & (df[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (df["high"] >= df["low"])
        & df["volume"].notna()
        & (df["volume"] >= 0)
    )
    df = df[valid]
    skipped = total_before - len(df)
    if df.empty:
        return 0, skipped

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [
        {
            "asset_id": asset_id,
            "timeframe": timeframe,
            "ts": ts.to_pydatetime().replace(tzinfo=None),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "adj_close": (None if pd.isna(row.adj_close) else float(row.adj_close)),
            "volume": float(row.volume),
            "is_adjusted": is_adjusted,
            "source": source,
            "ingested_at": now,
        }
        for ts, row in df.iterrows()
    ]

    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        stmt = sqlite_insert(Bar).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["asset_id", "timeframe", "ts"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "adj_close": stmt.excluded.adj_close,
                "volume": stmt.excluded.volume,
                "is_adjusted": stmt.excluded.is_adjusted,
                "source": stmt.excluded.source,
                "ingested_at": stmt.excluded.ingested_at,
            },
        )
        session.execute(stmt)
    else:
        timestamps = [r["ts"] for r in rows]
        session.execute(
            delete(Bar).where(
                Bar.asset_id == asset_id,
                Bar.timeframe == timeframe,
                Bar.ts.in_(timestamps),
            )
        )
        session.execute(Bar.__table__.insert(), rows)

    session.flush()
    return len(rows), skipped


def load_bars(
    session: Session,
    asset_id: int,
    timeframe: str = "1D",
    *,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    use_adjusted: bool = True,
) -> pd.DataFrame:
    """Load bars as a timestamp-indexed DataFrame, ascending.

    With ``use_adjusted=True`` the ``close`` column carries ``adj_close`` where
    the provider supplied one, and the raw close is preserved as
    ``close_unadjusted``. Indicators should always run on the adjusted series:
    an unadjusted price series has a discontinuity at every split, and a
    momentum rule reads that discontinuity as a real move.

    Returns an empty frame with the right columns when nothing matches, so
    callers can rely on ``.empty`` rather than a ``None`` check.
    """
    stmt = (
        select(Bar)
        .where(Bar.asset_id == asset_id, Bar.timeframe == timeframe)
        .order_by(Bar.ts.asc())
    )
    if start is not None:
        stmt = stmt.where(Bar.ts >= _naive_utc(start))
    if end is not None:
        stmt = stmt.where(Bar.ts <= _naive_utc(end))

    bars = list(session.scalars(stmt))
    columns = ["open", "high", "low", "close", "volume", "adj_close", "source", "is_adjusted"]
    if not bars:
        empty = pd.DataFrame(columns=columns)
        empty.index = pd.DatetimeIndex([], tz="UTC", name="ts")
        return empty

    frame = pd.DataFrame(
        {
            "ts": [b.ts for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
            "adj_close": [b.adj_close for b in bars],
            "source": [b.source for b in bars],
            "is_adjusted": [b.is_adjusted for b in bars],
        }
    )
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    frame = frame.set_index("ts").sort_index()

    if use_adjusted:
        adj = frame["adj_close"]
        if adj.notna().any():
            ratio = (adj / frame["close"]).where(adj.notna(), 1.0)
            frame["close_unadjusted"] = frame["close"]
            # Scale the whole bar by the close's adjustment factor so OHLC stays
            # internally consistent (high >= close >= low survives adjustment).
            for col in ("open", "high", "low"):
                frame[col] = frame[col] * ratio
            frame["close"] = adj.where(adj.notna(), frame["close"])

    return frame


def bar_count(session: Session, asset_id: int, timeframe: str = "1D") -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(Bar)
            .where(Bar.asset_id == asset_id, Bar.timeframe == timeframe)
        )
        or 0
    )


def latest_bar_date(session: Session, asset_id: int, timeframe: str = "1D") -> datetime | None:
    """Newest stored timestamp, as aware UTC, or ``None`` if there are no bars."""
    value = session.scalar(
        select(func.max(Bar.ts)).where(Bar.asset_id == asset_id, Bar.timeframe == timeframe)
    )
    return _to_utc(value) if value is not None else None


def earliest_bar_date(session: Session, asset_id: int, timeframe: str = "1D") -> datetime | None:
    value = session.scalar(
        select(func.min(Bar.ts)).where(Bar.asset_id == asset_id, Bar.timeframe == timeframe)
    )
    return _to_utc(value) if value is not None else None


def delete_bars(session: Session, asset_id: int, timeframe: str | None = None) -> int:
    """Delete bars for an asset. Returns the row count removed."""
    stmt = delete(Bar).where(Bar.asset_id == asset_id)
    if timeframe is not None:
        stmt = stmt.where(Bar.timeframe == timeframe)
    result = session.execute(stmt)
    session.flush()
    return int(result.rowcount or 0)


def coverage_report(session: Session, timeframe: str = "1D") -> pd.DataFrame:
    """Per-asset bar coverage: row count and date range.

    This is the honest answer to "what data do I actually have?", and it is what
    the CLI prints after a download instead of claiming success per symbol.
    """
    stmt = (
        select(
            Asset.symbol,
            Asset.market_code,
            Asset.provider,
            Asset.provider_symbol,
            func.count(Bar.id).label("bars"),
            func.min(Bar.ts).label("first_bar"),
            func.max(Bar.ts).label("last_bar"),
        )
        .join(Bar, (Bar.asset_id == Asset.id) & (Bar.timeframe == timeframe), isouter=True)
        .group_by(Asset.id)
        .order_by(Asset.market_code, Asset.symbol)
    )
    records = [
        {
            "symbol": r.symbol,
            "market": r.market_code,
            "provider": r.provider or "",
            "provider_symbol": r.provider_symbol or "",
            "bars": int(r.bars or 0),
            "first_bar": r.first_bar,
            "last_bar": r.last_bar,
        }
        for r in session.execute(stmt)
    ]
    return pd.DataFrame.from_records(
        records,
        columns=["symbol", "market", "provider", "provider_symbol", "bars", "first_bar", "last_bar"],
    )
