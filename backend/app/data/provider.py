"""The ``DataProvider`` abstraction.

Nothing above this layer knows which vendor the bars came from. That is the
point: free vendors are unreliable and change without notice, so swapping one
out must not touch the indicators, the backtester or the database.

A provider has three jobs and no others:

1. Say what it supports (``supported_markets``, ``supported_timeframes``, and
   how far back each timeframe goes).
2. Resolve a canonical symbol to its own ticker, empirically.
3. Return a clean, validated, UTC-indexed OHLCV frame -- or raise.

A provider must never return a partially-valid frame with silent gaps filled in.
Invented prices propagate into backtest results that look plausible and are
wrong. Raise instead.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum

import pandas as pd

from app.core.exceptions import (
    CorruptDataError,
    EmptyDataError,
    SymbolNotFoundError,
    UnsupportedTimeframeError,
)

__all__ = [
    "Timeframe",
    "TimeframeLimit",
    "ProviderCapabilities",
    "DataProvider",
    "OHLCV_COLUMNS",
    "validate_ohlcv",
]

OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


class Timeframe(str, Enum):
    """Supported bar intervals.

    Daily is the priority everywhere in this project. Intraday data from free
    sources is shallow (60 days for minute bars on Yahoo) and unadjusted, so any
    intraday research here is exploratory, not a basis for conclusions.
    """

    D1 = "1D"
    H1 = "1H"
    M15 = "15m"
    M5 = "5m"

    @property
    def is_intraday(self) -> bool:
        return self is not Timeframe.D1

    @property
    def pandas_freq(self) -> str:
        return {
            Timeframe.D1: "1D",
            Timeframe.H1: "1h",
            Timeframe.M15: "15min",
            Timeframe.M5: "5min",
        }[self]

    @property
    def bars_per_year(self) -> float:
        """Approximate bar count per calendar year, for annualising metrics.

        Based on 252 trading days and a 6.5-hour US session. Used only for
        scaling volatility and Sharpe; a small error here shifts those numbers
        slightly and affects nothing about trade generation.
        """
        return {
            Timeframe.D1: 252.0,
            Timeframe.H1: 252.0 * 6.5,
            Timeframe.M15: 252.0 * 26,
            Timeframe.M5: 252.0 * 78,
        }[self]

    @classmethod
    def parse(cls, value: "str | Timeframe") -> "Timeframe":
        if isinstance(value, cls):
            return value
        text = str(value).strip()
        for member in cls:
            if member.value.lower() == text.lower():
                return member
        raise UnsupportedTimeframeError(
            f"Unknown timeframe {value!r}. Supported: {[m.value for m in cls]}"
        )


@dataclass(frozen=True, slots=True)
class TimeframeLimit:
    """How far back a provider serves a given timeframe."""

    timeframe: Timeframe
    max_lookback_days: int | None
    """``None`` means "as far back as the instrument's history goes"."""

    note: str = ""

    def earliest_available(self, now: datetime | None = None) -> datetime | None:
        if self.max_lookback_days is None:
            return None
        reference = now or datetime.now(timezone.utc)
        return reference - timedelta(days=self.max_lookback_days)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a provider can and cannot do, and what it costs."""

    name: str
    markets: frozenset[str]
    timeframes: tuple[TimeframeLimit, ...]
    provides_adjusted_prices: bool
    requires_api_key: bool
    cost: str
    """Free-text cost statement, surfaced in reports. e.g. ``"free, no API key"``."""

    notes: str = ""

    def limit_for(self, timeframe: Timeframe) -> TimeframeLimit:
        for limit in self.timeframes:
            if limit.timeframe is timeframe:
                return limit
        raise UnsupportedTimeframeError(
            f"Provider {self.name!r} does not offer timeframe {timeframe.value}. "
            f"Available: {[t.timeframe.value for t in self.timeframes]}"
        )


def _flatten_ohlcv_multiindex(
    frame: pd.DataFrame, *, symbol: str, provider: str
) -> pd.DataFrame:
    """Collapse a ``(field, ticker)`` MultiIndex down to plain field names.

    ``yfinance.download`` returns two-level columns even for a single ticker, in
    either level order. Naively joining the levels yields ``close_aapl``, which no
    downstream code recognises -- so the field level is identified by content and
    the ticker level is dropped.

    A frame holding several tickers is rejected rather than silently collapsed:
    picking one arbitrarily would attribute one instrument's prices to another.
    """
    field_names = set(OHLCV_COLUMNS)
    field_level: int | None = None

    for level in range(frame.columns.nlevels):
        values = {str(v).strip().lower().replace(" ", "_") for v in frame.columns.get_level_values(level)}
        if values & field_names:
            field_level = level
            break

    if field_level is None:
        raise CorruptDataError(
            f"{provider} returned MultiIndex columns for {symbol!r} with no "
            f"recognisable OHLCV level: {list(frame.columns)}"
        )

    other_levels = [lvl for lvl in range(frame.columns.nlevels) if lvl != field_level]
    for level in other_levels:
        distinct = frame.columns.get_level_values(level).unique()
        if len(distinct) > 1:
            raise CorruptDataError(
                f"{provider} returned a frame holding several instruments "
                f"({list(distinct)}) while {symbol!r} was requested. Fetch one "
                "symbol at a time -- collapsing this would mix their prices."
            )

    out = frame.copy()
    out.columns = [str(c) for c in frame.columns.get_level_values(field_level)]
    return out


def validate_ohlcv(
    frame: pd.DataFrame,
    *,
    symbol: str,
    provider: str,
    drop_invalid: bool = True,
) -> pd.DataFrame:
    """Normalise and validate a raw provider frame.

    Returns a frame that is UTC-indexed, ascending, de-duplicated, lower-cased
    and free of structurally impossible bars.

    ``drop_invalid=True`` discards bad rows and keeps the rest; ``False`` raises
    ``CorruptDataError`` on the first violation. Dropping is the default because
    free vendors reliably emit a handful of malformed rows in long histories, and
    losing three bars out of 2,500 is preferable to losing the series. What is
    never acceptable is *repairing* them.

    Raises
    ------
    EmptyDataError
        The frame is empty, or every row was invalid.
    CorruptDataError
        Required columns are missing, or ``drop_invalid=False`` and a row is bad.
    """
    if frame is None or len(frame) == 0:
        raise EmptyDataError(f"{provider} returned no rows for {symbol!r}")

    df = frame.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df = _flatten_ohlcv_multiindex(df, symbol=symbol, provider=provider)

    rename = {}
    for col in df.columns:
        key = str(col).strip().lower().replace(" ", "_")
        if key.startswith("adj_close"):
            key = "adj_close"
        elif key.startswith("stock_split"):
            key = "stock_splits"
        rename[col] = key
    df = df.rename(columns=rename)

    missing = [c for c in OHLCV_COLUMNS if c not in df.columns]
    if missing:
        raise CorruptDataError(
            f"{provider} frame for {symbol!r} is missing columns {missing}; "
            f"received {sorted(df.columns)}"
        )

    keep = [c for c in (*OHLCV_COLUMNS, "adj_close") if c in df.columns]
    df = df[keep]

    df.index = pd.to_datetime(df.index, utc=True, errors="coerce")
    df = df[df.index.notna()]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "ts"

    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    ohlc = ["open", "high", "low", "close"]
    ok = (
        df[ohlc].notna().all(axis=1)
        & (df[ohlc] > 0).all(axis=1)
        & (df["high"] >= df["low"])
        & (df["high"] >= df[["open", "close"]].max(axis=1))
        & (df["low"] <= df[["open", "close"]].min(axis=1))
        & df["volume"].notna()
        & (df["volume"] >= 0)
    )

    if not ok.all():
        bad = df[~ok]
        if not drop_invalid:
            first = bad.iloc[0]
            raise CorruptDataError(
                f"{provider} returned an invalid bar for {symbol!r} at "
                f"{bad.index[0]}: {first.to_dict()}"
            )
        df = df[ok]

    if df.empty:
        raise EmptyDataError(
            f"{provider} returned {len(frame)} rows for {symbol!r} but none were valid"
        )

    return df


class DataProvider(abc.ABC):
    """Base class for every market-data source.

    Subclasses implement :meth:`_fetch_raw` and declare
    :attr:`capabilities`. The public :meth:`fetch_bars` handles timeframe
    validation, lookback clamping and output validation so every provider
    behaves identically from the outside.
    """

    @property
    @abc.abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @property
    def name(self) -> str:
        return self.capabilities.name

    @property
    def symbol_namespace(self) -> str:
        """Key under which ``AssetSpec.provider_symbols`` declares this provider's tickers.

        Defaults to :attr:`name`. It differs when several providers share one
        vendor's ticker vocabulary: ``chile-yfinance`` is a specialised wrapper
        around Yahoo, so it reads the ``"yfinance"`` mappings rather than
        requiring every Chilean asset to declare the same strings twice.
        """
        return self.name

    def supports_market(self, market: str) -> bool:
        return market.strip().upper() in self.capabilities.markets

    def supports_timeframe(self, timeframe: "str | Timeframe") -> bool:
        tf = Timeframe.parse(timeframe)
        return any(limit.timeframe is tf for limit in self.capabilities.timeframes)

    # ------------------------------------------------------------------ #
    # Subclass hooks
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def _fetch_raw(
        self,
        provider_symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Return the vendor's raw frame. May raise any ``DataError`` subclass."""

    RESOLUTION_PROBE_DAYS = 365
    """Width of the window used to test whether a candidate ticker exists.

    A month is too narrow: a thinly traded Chilean name can go days without a
    print, and an instrument that stopped trading mid-history would be declared
    missing despite having years of usable data. A year is wide enough to see any
    real listing, and one request is cheap since the result is cached.
    """

    def resolve_symbol(self, candidates: tuple[str, ...], canonical: str) -> str:
        """Return the first candidate that actually returns data.

        The default implementation probes each candidate over
        :attr:`RESOLUTION_PROBE_DAYS`. Providers with a real symbol-lookup endpoint
        should override this with something cheaper.

        Raises
        ------
        SymbolNotFoundError
            No candidate produced data.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=self.RESOLUTION_PROBE_DAYS)
        for candidate in candidates:
            try:
                raw = self._fetch_raw(candidate, Timeframe.D1, start, end)
            except Exception:
                continue
            if raw is not None and len(raw) > 0:
                return candidate
        raise SymbolNotFoundError(canonical, self.name, candidates)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def fetch_bars(
        self,
        provider_symbol: str,
        timeframe: "str | Timeframe" = Timeframe.D1,
        start: datetime | date | str | None = None,
        end: datetime | date | str | None = None,
        *,
        canonical_symbol: str | None = None,
    ) -> pd.DataFrame:
        """Fetch validated bars.

        The returned frame is UTC-indexed and ascending, with lower-case
        ``open/high/low/close/volume`` columns plus ``adj_close`` when the
        provider supplies adjusted prices.

        ``start`` is clamped to the provider's documented lookback for that
        timeframe. Asking Yahoo for two years of 5-minute bars silently returns
        60 days; clamping makes that limit explicit rather than letting a caller
        believe it got what it asked for.
        """
        tf = Timeframe.parse(timeframe)
        limit = self.capabilities.limit_for(tf)

        now = datetime.now(timezone.utc)
        end_dt = _as_utc_datetime(end) if end is not None else now
        start_dt = _as_utc_datetime(start) if start is not None else end_dt - timedelta(days=365 * 10)

        earliest = limit.earliest_available(now)
        if earliest is not None and start_dt < earliest:
            start_dt = earliest

        if start_dt >= end_dt:
            raise EmptyDataError(
                f"Requested window for {provider_symbol!r} is empty after clamping to "
                f"{self.name}'s {tf.value} lookback limit "
                f"({limit.max_lookback_days} days). {limit.note}".strip()
            )

        raw = self._fetch_raw(provider_symbol, tf, start_dt, end_dt)
        return validate_ohlcv(
            raw,
            symbol=canonical_symbol or provider_symbol,
            provider=self.name,
        )

    def __repr__(self) -> str:
        caps = self.capabilities
        return f"<{type(self).__name__} name={caps.name!r} markets={sorted(caps.markets)}>"


def _as_utc_datetime(value: datetime | date | str) -> datetime:
    """Coerce a datetime, date or ISO-8601 string to an aware UTC datetime.

    Strings are accepted because a date is the most natural thing for a caller to
    hand in, and rejecting one used to surface as an ``AttributeError`` raised deep
    inside the provider rather than as a usable message.
    """
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(
                f"Could not parse {value!r} as a date. Use an ISO-8601 value such as "
                "'2024-01-02' or '2024-01-02T13:30:00+00:00'."
            ) from exc
        return _as_utc_datetime(parsed)

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)

    raise TypeError(
        f"Expected a datetime, date or ISO-8601 string, got {type(value).__name__}"
    )
