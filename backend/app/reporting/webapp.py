"""Market data for the static web app, as JSON a browser can compute against.

The problem this solves. The repository is public, so GitHub Pages works -- but anything it
serves is world-readable, and the user wants their positions visible on their phone without
Telegram. Those pull in opposite directions: a page that knows your positions and is served
publicly has published your positions.

The resolution is to split what is published from what is personal:

* **Published here**: prices, moving averages, ATR, and today's signal, per instrument. Public
  market data. Nothing about anyone.
* **Never published**: what you own. It is typed into the app on your phone and kept in that
  browser's ``localStorage``. It never reaches the network, the repository, or this process.

The app then computes your position's status in the browser, from public data and private
holdings, and nobody but you ever sees the combination. That is why this module exports
*inputs* rather than conclusions: the conclusion is personal, so it has to be reached on the
device that knows the personal half.

The cost is that the browser must reimplement the exit rules. That duplication is a real risk --
two implementations drift -- so the parameters travel in the payload rather than being hardcoded
in the JavaScript, and ``tests/test_webapp.py`` checks the browser's arithmetic against the
Python engine's on the same inputs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.universe import (
    ALL_REGIONS,
    DEFAULT_UNIVERSE,
    LIQUIDITY_CONCERN_USD,
    TURNOVER_WINDOW,
    VERIFICATION_DATE,
    benchmark_for_region,
    universe_for_region,
)

logger = get_logger(__name__)

__all__ = ["build_market_payload", "write_app", "HISTORY_SESSIONS", "PAYLOAD_VERSION"]

PAYLOAD_VERSION = 1
"""Bumped when the shape changes.

The app checks it and refuses stale data rather than reading a field that moved. A browser can
hold a cached copy for a long time, and silently misreading it would produce a confident wrong
answer about someone's money.
"""

HISTORY_SESSIONS = 260
"""About a year of trading days.

Enough to place an entry in context and to carry the levels for a position opened months ago,
while keeping the payload small enough to load on a phone connection.
"""


@dataclass(frozen=True, slots=True)
class _Row:
    """One instrument's series, stored column-wise to keep the JSON compact."""

    dates: list[str]
    high: list[float]
    low: list[float]
    close: list[float]
    ema50: list[float | None]
    atr14: list[float | None]


def _series(frame, sessions: int) -> _Row:
    tail = frame.tail(sessions)

    def col(name: str, nullable: bool = False) -> list:
        values = tail[name].tolist()
        if nullable:
            return [None if v != v else round(float(v), 4) for v in values]
        return [round(float(v), 4) for v in values]

    return _Row(
        dates=[str(i.date()) for i in tail.index],
        high=col("high"),
        low=col("low"),
        close=col("close"),
        ema50=col("ema_50", nullable=True),
        atr14=col("atr_14", nullable=True),
    )


def _fx_rates(session: Session) -> dict[str, float]:
    """Currency rates per USD, for amounts entered in something other than dollars.

    Missing rather than guessed when a provider call fails: the page refuses a CLP amount it
    cannot convert instead of applying a stale number to someone's cost basis.
    """
    from app.portfolio.holdings import FX_SYMBOLS, fx_rate_to_usd

    rates: dict[str, float] = {}
    for currency in FX_SYMBOLS:
        rate = fx_rate_to_usd(session, currency)
        if rate:
            rates[currency] = round(rate, 4)
    return rates


def build_market_payload(
    session: Session, *, strategy_name: str = "trend_momentum"
) -> dict[str, Any]:
    """Everything the app needs, and nothing about anybody.

    Deliberately contains no holding, no amount and no name. If this file ever grows a field
    that identifies a person, the privacy split it exists to enforce has been broken.
    """
    from app.backtesting.runner import load_features
    from app.core.exceptions import InsufficientDataError
    from app.strategies.registry import build_strategy

    strategy = build_strategy(strategy_name)
    params = strategy.params.to_dict()
    symbols = [spec.symbol for spec in DEFAULT_UNIVERSE]

    try:
        frames, skipped = load_features(session, symbols, "USA")
    except InsufficientDataError:
        frames, skipped = {}, [f"{s}: no stored bars" for s in symbols]

    from app.strategies.scanner import scan_market

    scan = scan_market(session, strategy, "USA", log_decisions=False)
    readings = {row.symbol: row for row in scan.rows}

    instruments: dict[str, Any] = {}
    for spec in DEFAULT_UNIVERSE:
        frame = frames.get(spec.symbol)
        if frame is None or frame.empty:
            continue
        series = _series(frame, HISTORY_SESSIONS)
        reading = readings.get(spec.symbol)
        instruments[spec.symbol] = {
            "name": spec.name,
            "region": spec.region,
            "sector": spec.sector,
            "etf": spec.asset_class == "etf",
            "turnover": spec.median_turnover_usd,
            "thin": spec.is_thinly_traded,
            "liquidity_caveat": spec.liquidity_caveat,
            "notes": spec.notes,
            "signal": reading.action if reading else "UNKNOWN",
            "score": round(reading.score, 2) if reading else None,
            "reasons": list(reading.reasons) if reading else [],
            "d": series.dates,
            "h": series.high,
            "l": series.low,
            "c": series.close,
            "e": series.ema50,
            "a": series.atr14,
        }

    regions = []
    for region in ALL_REGIONS:
        specs = universe_for_region(region)
        benchmark = benchmark_for_region(region)
        present = [s.symbol for s in specs if s.symbol in instruments]
        regions.append(
            {
                "name": region,
                "symbols": present,
                "declared": len(specs),
                "benchmark": benchmark.symbol,
                "benchmark_caveats": list(benchmark.caveats),
            }
        )

    as_of = max(
        (row["d"][-1] for row in instruments.values() if row["d"]), default=None
    )

    return {
        "version": PAYLOAD_VERSION,
        "generated_at": date.today().isoformat(),
        "as_of": as_of,
        "verification_date": VERIFICATION_DATE,
        "turnover_window": TURNOVER_WINDOW,
        "liquidity_threshold": LIQUIDITY_CONCERN_USD,
        "strategy": {
            "name": strategy_name,
            # The browser recomputes the exit levels, so the parameters travel with the data.
            # Hardcoding them in the JavaScript is how the two implementations would drift.
            "stop_atr_multiple": params.get("stop_atr_multiple"),
            "take_profit_r_multiple": params.get("take_profit_r_multiple"),
            "max_holding_bars": params.get("max_holding_bars"),
            "exit_on_trend_break": params.get("exit_on_trend_break"),
            "exit_rsi_max": params.get("exit_rsi_max"),
        },
        # Measured over 915 TRAIN trades; see the horizon section of the README.
        "exit_base_rates": {
            "trend_break": 0.398,
            "stop_loss": 0.332,
            "take_profit": 0.214,
            "time": 0.016,
        },
        # Rates the page needs to turn a cash amount into a share count. A daily close, not
        # what a broker applied -- stated here so the page can say so too.
        "fx": _fx_rates(session),
        "regions": regions,
        "instruments": instruments,
        "skipped": skipped,
    }


def write_app(
    session: Session,
    output: Path,
    *,
    strategy_name: str = "trend_momentum",
) -> dict[str, Path]:
    """Write ``index.html`` and ``market.json`` into ``output``. Returns what was written.

    The HTML ships from ``app_template.html`` beside this module rather than being built as a
    string here, so it can be edited as HTML with an editor's help instead of as Python quoting.
    """
    output.mkdir(parents=True, exist_ok=True)

    payload = build_market_payload(session, strategy_name=strategy_name)
    data_path = output / "market.json"
    data_path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=False), encoding="utf-8"
    )

    template = Path(__file__).with_name("app_template.html")
    page_path = output / "index.html"
    page_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

    logger.info(
        "Wrote app to %s (%d instruments, %.1f KB of data)",
        output,
        len(payload["instruments"]),
        data_path.stat().st_size / 1024,
    )
    return {"page": page_path, "data": data_path}
