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

from app.config import get_settings
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


def _round_evidence(rows: dict[str, Any]) -> dict[str, Any]:
    """Two decimals for display. The artifact on disk keeps full precision as the record.

    A CAGR printed as 13.442112468503641 claims a precision the measurement does not have:
    it came from 586 trades over six years of one universe.
    """
    out: dict[str, Any] = {}
    for split, row in rows.items():
        out[split] = {
            key: (round(value, 2) if isinstance(value, float) else value)
            for key, value in row.items()
            if key not in ("exits", "holding")
        }
        # Only the endings worth naming: a tail of 0.3% rows is noise on a phone, and
        # "end_of_backtest" is the sample running out rather than a rule firing.
        out[split]["exits"] = {
            k: v for k, v in (row.get("exits") or {}).items()
            if v.get("share_pct", 0) >= 1.0 and k != "end_of_backtest"
        }
        out[split]["holding"] = row.get("holding") or {}
    return out


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

    from app.reporting.evidence import load_evidence, resolved_profile_params
    from app.strategies.profiles import RISK_PROFILES, get_profile

    settings = get_settings()
    active = get_profile(settings.strategy_profile).name
    resolved = resolved_profile_params(strategy_name)
    built = {name: build_strategy(strategy_name, p) for name, p in resolved.items()}

    strategy = built[active]
    params = strategy.params.to_dict()
    symbols = [spec.symbol for spec in DEFAULT_UNIVERSE]

    try:
        frames, skipped = load_features(session, symbols, "USA")
    except InsufficientDataError:
        frames, skipped = {}, [f"{s}: no stored bars" for s in symbols]

    instruments: dict[str, Any] = {}
    for spec in DEFAULT_UNIVERSE:
        frame = frames.get(spec.symbol)
        if frame is None or frame.empty:
            continue
        series = _series(frame, HISTORY_SESSIONS)
        last_close = float(frame["close"].iloc[-1])

        # Once per profile, because the entry conditions themselves differ between them. A
        # single signal would silently describe whichever one the server happened to be set
        # to, while the reader looked at a different one.
        by_profile: dict[str, Any] = {}
        for name, candidate in built.items():
            decision = candidate.evaluate(frame, in_position=False)

            # What it would propose if entered today, whatever the signal says: a stop is a
            # volatility measurement, not part of the entry decision.
            proposed = candidate.propose_levels(frame)
            levels = None
            if proposed and last_close > proposed[0]:
                stop, target = proposed
                levels = {
                    "stop": round(stop, 4),
                    "target": round(target, 4),
                    "rr": round((target - last_close) / (last_close - stop), 2),
                }

            by_profile[name] = {
                "signal": decision.action.value
                if hasattr(decision.action, "value")
                else str(decision.action),
                "score": round(decision.score, 2),
                # The name and the verdict. The measured value is already a flat field,
                # and the threshold travels in `params`, so the page can write the sentence
                # itself -- in whatever language it is read in.
                "conditions": [
                    {"name": c.name, "ok": bool(c.passed)} for c in decision.components
                ],
                "levels": levels,
            }

        def feature(name: str) -> float | None:
            if name not in frame.columns:
                return None
            value = frame[name].iloc[-1]
            return None if value != value else round(float(value), 4)

        instruments[spec.symbol] = {
            "by_profile": by_profile,
            "ema200": feature("ema_200"),
            "rsi": feature("rsi_14"),
            "macd_hist": feature("macd_hist"),
            "rel_volume": feature("relative_volume_20"),
            "atr_pct": feature("atr_pct_14"),
            "roc": feature(f"roc_{params.get('roc_period', 20)}"),
            "from_high": feature("dist_52w_high_pct"),
            "name": spec.name,
            "region": spec.region,
            "sector": spec.sector,
            "etf": spec.asset_class == "etf",
            "turnover": spec.median_turnover_usd,
            "thin": spec.is_thinly_traded,
            "notes": spec.notes_es or spec.notes,
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

    evidence = load_evidence(strategy_name=strategy_name)
    profiles = [
        {
            "name": profile.name,
            "title": profile.title_es or profile.title,
            "summary": profile.summary_es or profile.summary,
            # Every parameter the browser needs to recompute levels and walk the exits. Not
            # the whole set: the entry conditions are evaluated here, server-side.
            "params": {
                key: resolved[profile.name].get(key)
                for key in (
                    # Exits, which the browser recomputes.
                    "stop_atr_multiple",
                    "take_profit_r_multiple",
                    "max_holding_bars",
                    "exit_on_trend_break",
                    "exit_rsi_max",
                    # Entry thresholds, which the browser only describes. Published so the
                    # page can say what a condition was measured against instead of
                    # repeating a number that lives in Python.
                    "require_price_above_ema200",
                    "require_ema50_above_ema200",
                    "rsi_min",
                    "rsi_max",
                    "require_macd_positive",
                    "roc_period",
                    "min_roc_pct",
                    "min_relative_volume",
                    "min_atr_pct",
                    "max_atr_pct",
                )
            },
            "changes": profile.overrides,
            "evidence": _round_evidence(evidence.by_profile.get(profile.name, {})),
        }
        for profile in RISK_PROFILES
    ]

    return {
        "version": PAYLOAD_VERSION,
        "generated_at": date.today().isoformat(),
        "as_of": as_of,
        "verification_date": VERIFICATION_DATE,
        "turnover_window": TURNOVER_WINDOW,
        "liquidity_threshold": LIQUIDITY_CONCERN_USD,
        "profiles": profiles,
        "evidence": {
            "available": evidence.available,
            "reason": evidence.reason,
            "measured_on": evidence.measured_on,
            "universe_size": evidence.universe_size,
            "windows": evidence.windows,
        },
        "strategy": {
            "name": strategy_name,
            "profile": active,
            # The browser recomputes the exit levels, so the parameters travel with the data.
            # Hardcoding them in the JavaScript is how the two implementations would drift.
            "stop_atr_multiple": params.get("stop_atr_multiple"),
            "take_profit_r_multiple": params.get("take_profit_r_multiple"),
            "max_holding_bars": params.get("max_holding_bars"),
            "exit_on_trend_break": params.get("exit_on_trend_break"),
            "exit_rsi_max": params.get("exit_rsi_max"),
        },
        # Measured over 915 TRAIN trades; see the horizon section of the README.
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
