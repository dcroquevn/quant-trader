"""Ties the pieces together: bars → features → backtest → metrics → benchmark.

Kept separate from :mod:`app.backtesting.engine` so the engine stays testable with
synthetic frames and never touches the database. This module is the only place that
knows about both persistence and performance measurement.

The split guard
---------------
:func:`resolve_window` is the single place that turns a named data partition into dates,
and it refuses to hand out the TEST range unless the caller explicitly says it is
finalising a result. Phase 4's optimiser will call this with ``split="train"`` and
cannot reach test data by accident — which is the whole point of having the guard in one
function rather than trusting every call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.backtesting.engine import BacktestConfig, BacktestResult, Backtester
from app.backtesting.metrics import (
    compare_to_benchmark,
    compute_benchmark_metrics,
    compute_metrics,
)
from app.config import get_settings
from app.core.exceptions import DataLeakageError, InsufficientDataError
from app.core.logging import get_logger
from app.core.markets import get_market
from app.core.universe import benchmark_for_market, find_asset, universe_for_market
from app.data.engine import DataEngine
from app.data.provider import Timeframe
from app.indicators.registry import MIN_BARS_FOR_FULL_FEATURES, compute_features
from app.strategies.base import Strategy

logger = get_logger(__name__)

WARMUP_CALENDAR_DAYS = 500
"""Extra history loaded before a backtest window so indicators start warm.

Comfortably more than the 252 trading days the slowest indicator needs. Features are
computed over everything loaded; trading is confined to the window itself.
"""

__all__ = [
    "WindowSpec",
    "resolve_window",
    "load_features",
    "run_backtest",
    "WARMUP_CALENDAR_DAYS",
]


@dataclass(frozen=True, slots=True)
class WindowSpec:
    """A resolved date range and the partition it came from."""

    split: str
    start: date
    end: date

    def describe(self) -> str:
        return f"{self.split} ({self.start} to {self.end})"


def resolve_window(
    split: str = "full",
    *,
    start: date | None = None,
    end: date | None = None,
    finalising: bool = False,
) -> WindowSpec:
    """Turn a split name into a date range.

    ``split="full"`` uses the whole available history, or the explicit ``start``/``end``
    when given.

    Parameters
    ----------
    finalising:
        Required to be True before the TEST range will be returned. The test partition
        is meant to be read **once**, at the end, after parameters are frozen. Reading
        it during a search turns it into a second validation set and destroys the only
        out-of-sample estimate there is. Passing this flag is deliberately awkward so it
        cannot happen absent-mindedly.

    Raises
    ------
    DataLeakageError
        ``split="test"`` without ``finalising=True``.
    """
    settings = get_settings()
    key = split.strip().lower()

    if key == "test" and not finalising:
        raise DataLeakageError(
            "Refusing to open the TEST split. It is reserved for a single evaluation "
            "after parameters are frozen; reading it during a search turns it into a "
            "second validation set and leaves no out-of-sample estimate at all. Pass "
            "finalising=True only when you are reporting a final result."
        )

    if key == "full":
        resolved_start = start or date(1970, 1, 1)
        resolved_end = end or datetime.now(timezone.utc).date()
        return WindowSpec("full", resolved_start, resolved_end)

    bounds_start, bounds_end = settings.split_bounds(key)
    # An explicit range may narrow a split but never widen it past its bounds.
    resolved_start = max(bounds_start, start) if start else bounds_start
    resolved_end = min(bounds_end, end) if end else bounds_end
    if resolved_start > resolved_end:
        raise ValueError(
            f"Requested range {start}..{end} does not overlap the {key} split "
            f"({bounds_start}..{bounds_end})"
        )
    return WindowSpec(key, resolved_start, resolved_end)


def load_features(
    session: Session,
    symbols: list[str],
    market: str,
    *,
    timeframe: "str | Timeframe" = Timeframe.D1,
    warmup_start: date | None = None,
    end: date | None = None,
    trim_carried_forward: bool = True,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Load bars and compute features per symbol. Returns ``(frames, skipped)``.

    ``warmup_start`` should be *earlier* than the backtest window so indicators are
    already warmed up on the first bar that gets traded. Slicing to the window first and
    computing features afterwards would leave the first 252 bars of every run with NaN
    features, silently making the strategy inactive for the first year.

    Symbols with too little history are skipped and returned in ``skipped`` rather than
    included with NaN features, because a strategy comparing a price against NaN
    evaluates to False and produces a HOLD that looks like a considered decision.
    """
    engine = DataEngine(session)
    frames: dict[str, pd.DataFrame] = {}
    skipped: list[str] = []

    for symbol in symbols:
        spec = find_asset(symbol, market)
        bars = engine.load_spec(
            spec,
            timeframe,
            start=warmup_start,
            end=end,
            trim_carried_forward=trim_carried_forward,
        )
        if len(bars) < MIN_BARS_FOR_FULL_FEATURES:
            skipped.append(
                f"{symbol}: {len(bars)} bars, needs {MIN_BARS_FOR_FULL_FEATURES}"
            )
            continue
        frames[symbol] = compute_features(bars)

    if not frames:
        raise InsufficientDataError(
            f"No symbol in {market} has the {MIN_BARS_FOR_FULL_FEATURES} bars needed for "
            f"a complete feature set. Skipped: {skipped or 'nothing loaded'}. "
            "Run `python -m app download-data` first."
        )
    return frames, skipped


def run_backtest(
    session: Session,
    strategy: Strategy,
    market: str,
    *,
    split: str = "full",
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
    initial_capital: float | None = None,
    timeframe: "str | Timeframe" = Timeframe.D1,
    finalising: bool = False,
    label: str = "",
    include_benchmark: bool = True,
) -> BacktestResult:
    """Run one backtest end to end and attach metrics.

    Warm-up is handled by loading history from before the window and then restricting
    *trading* to the window: features are computed on everything available, and the
    engine only acts from ``window.start`` onwards.
    """
    settings = get_settings()
    market_spec = get_market(market)
    window = resolve_window(split, start=start, end=end, finalising=finalising)

    tradable = symbols or [s.symbol for s in universe_for_market(market_spec.code)]

    # Pull extra history so the 252-bar indicators are warm on the window's first traded
    # bar. 500 calendar days is comfortably more than 252 trading days.
    #
    # stdlib timedelta, not pd.Timedelta: adding a pandas Timedelta to a `date` routes
    # through NumPy and emits a generic-unit DeprecationWarning, which this project's
    # strict warning filter turns into an error.
    warmup_start = window.start - timedelta(days=WARMUP_CALENDAR_DAYS)

    frames, skipped = load_features(
        session,
        tradable,
        market_spec.code,
        timeframe=timeframe,
        warmup_start=warmup_start,
        end=window.end,
    )
    if skipped:
        logger.info("Skipped %d symbol(s) with insufficient history: %s", len(skipped), skipped)

    capital = initial_capital or (
        settings.initial_capital_usd
        if market_spec.currency == "USD"
        else settings.initial_capital_clp
    )

    config = BacktestConfig(
        market=market_spec.code,
        initial_capital=capital,
        risk_per_trade_pct=settings.risk_per_trade_pct,
        max_position_size_pct=settings.max_position_size_pct,
        max_simultaneous_positions=settings.max_simultaneous_positions,
        split=window.split,
        label=label or f"{strategy.name} {market_spec.code} {window.describe()}",
    )

    result = Backtester(strategy, config).run(
        frames, start=window.start, end=window.end
    )

    result.metrics = compute_metrics(
        result.equity_curve, result.trades, result.snapshots
    )
    if skipped:
        result.limitations.append(
            f"{len(skipped)} instrument(s) excluded for insufficient history: "
            f"{'; '.join(skipped)}. The universe actually traded is smaller than the "
            "one requested."
        )

    if include_benchmark:
        result.benchmark_metrics = _benchmark(
            session, market_spec.code, result, timeframe
        )
        result.metrics["vs_benchmark"] = compare_to_benchmark(
            result.metrics, result.benchmark_metrics
        )

    return result


def _benchmark(
    session: Session,
    market: str,
    result: BacktestResult,
    timeframe: "str | Timeframe",
) -> dict[str, Any]:
    """Buy-and-hold benchmark metrics, with the market's caveats attached."""
    try:
        spec = benchmark_for_market(market)
    except KeyError as exc:
        return {"available": False, "reason": str(exc)}

    if not spec.available or not spec.symbol:
        return {
            "available": False,
            "reason": f"no benchmark data source for {market}",
            "caveats": list(spec.caveats),
        }

    engine = DataEngine(session)
    try:
        asset = find_asset(spec.symbol)
    except KeyError:
        return {"available": False, "reason": f"{spec.symbol} not in the declared universe"}

    bars = engine.load_spec(
        asset,
        timeframe,
        start=result.start_date,
        end=result.end_date,
        trim_carried_forward=True,
    )
    if bars.empty:
        return {
            "available": False,
            "reason": (
                f"no stored bars for benchmark {spec.symbol}; run "
                f"`python -m app download-data --symbols {spec.symbol}`"
            ),
            "caveats": list(spec.caveats),
        }

    return compute_benchmark_metrics(
        bars["close"],
        result.equity_curve.index,
        result.config.initial_capital,
        label=f"{spec.symbol} buy and hold ({spec.kind})",
        caveats=spec.caveats,
    )
