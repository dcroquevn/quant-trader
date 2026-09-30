"""Wires the projection engine to stored data.

Kept separate from :mod:`app.projections.analogues` so that module stays testable with
synthetic frames and never touches the database.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.backtesting.runner import prepare_frames
from app.core.exceptions import InsufficientDataError
from app.core.logging import get_logger, log_decision
from app.core.markets import get_market
from app.core.universe import find_asset
from app.data.provider import Timeframe
from app.projections.analogues import (
    DEFAULT_MATCH_FEATURES,
    DEFAULT_MAX_DISTANCE,
    MatchFeature,
    find_analogues,
)
from app.projections.scenarios import ScenarioSet, build_scenarios, scenarios_as_evidence

logger = get_logger(__name__)

__all__ = ["project_symbol", "project_market", "DEFAULT_HORIZONS"]

DEFAULT_HORIZONS: tuple[int, ...] = (5, 20, 60)
"""Horizons reported by default.

Several rather than one, because a setup can look encouraging at five bars and
discouraging at sixty. Showing a single horizon lets the reader assume it generalises.
"""


def project_symbol(
    session: Session,
    symbol: str,
    market: str | None = None,
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    timeframe: "str | Timeframe" = Timeframe.D1,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    features: tuple[MatchFeature, ...] = DEFAULT_MATCH_FEATURES,
    pool_across_symbols: bool = True,
    start: date | None = None,
    end: date | None = None,
    log: bool = True,
) -> dict[str, Any]:
    """Project one instrument across several horizons.

    Returns a payload carrying one :class:`ScenarioSet` per horizon. Horizons are reported
    independently and never combined into a single number: averaging a 5-bar and a 60-bar
    distribution produces something that describes neither.
    """
    spec = find_asset(symbol, market)
    market_spec = get_market(spec.market)

    frames, skipped = prepare_frames(
        session,
        market_spec.code,
        split="full",
        start=start,
        end=end,
        timeframe=timeframe,
        symbols=None if pool_across_symbols else [spec.symbol],
    )
    if spec.symbol not in frames:
        raise InsufficientDataError(
            f"{spec.symbol} has no usable feature frame. "
            f"{'Skipped: ' + '; '.join(skipped) if skipped else 'Run a download first.'}"
        )

    target = frames[spec.symbol]
    current_price = float(target["close"].iloc[-1])

    by_horizon: dict[int, ScenarioSet] = {}
    for horizon in horizons:
        analogues = find_analogues(
            frames,
            spec.symbol,
            market_spec.code,
            horizon=horizon,
            features=features,
            max_distance=max_distance,
            pool_across_symbols=pool_across_symbols,
        )
        by_horizon[horizon] = build_scenarios(
            analogues, current_price=current_price, currency=market_spec.currency
        )

    available = [h for h, s in by_horizon.items() if s.available]

    if log and available:
        # Section 38: a recorded decision carries the evidence behind it. This is the
        # projection half of that record.
        primary = by_horizon[available[0]]
        log_decision(
            symbol=spec.symbol,
            market=market_spec.code,
            action="PROJECTION",
            reasons=[
                f"{primary.n_observations} independent historical analogues at "
                f"{primary.horizon} bars"
            ],
            features=primary.setup,
            evidence=scenarios_as_evidence(primary),
        )

    return {
        "symbol": spec.symbol,
        "market": market_spec.code,
        "currency": market_spec.currency,
        "current_price": current_price,
        "as_of": target.index[-1].isoformat(),
        "bars_available": len(target),
        "pooled_symbols": sorted(frames) if pool_across_symbols else [spec.symbol],
        "horizons": {str(h): s.to_dict() for h, s in by_horizon.items()},
        "horizons_with_evidence": available,
        "match_features": [
            {"name": f.name, "tolerance": f.tolerance, "weight": f.weight} for f in features
        ],
        "max_distance": max_distance,
        "note": (
            "Each horizon is reported on its own. They are never averaged: a distribution "
            "over five bars and one over sixty describe different things, and a blend would "
            "describe neither."
        ),
    }


def project_market(
    session: Session,
    market: str,
    *,
    horizon: int = 20,
    timeframe: "str | Timeframe" = Timeframe.D1,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    symbols: list[str] | None = None,
    log: bool = False,
) -> dict[str, Any]:
    """Project every instrument in one market at a single horizon.

    Used by the scanner to attach evidence to current signals. Instruments with insufficient
    evidence are included with ``available=False`` rather than dropped — an absent row would
    read as "no setup" when it means "not enough comparable history".
    """
    market_spec = get_market(market)

    frames, skipped = prepare_frames(
        session, market_spec.code, split="full", timeframe=timeframe, symbols=symbols
    )
    if not frames:
        raise InsufficientDataError(
            f"No usable feature frames for {market_spec.code}. Skipped: {skipped}"
        )

    rows: list[dict[str, Any]] = []
    for symbol in sorted(frames):
        try:
            analogues = find_analogues(
                frames, symbol, market_spec.code,
                horizon=horizon, max_distance=max_distance, pool_across_symbols=True,
            )
            scenarios = build_scenarios(
                analogues,
                current_price=float(frames[symbol]["close"].iloc[-1]),
                currency=market_spec.currency,
            )
            rows.append(scenarios.to_dict())
        except Exception as exc:  # noqa: BLE001 -- one instrument must not stop the sweep
            rows.append(
                {
                    "symbol": symbol,
                    "market": market_spec.code,
                    "available": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )

    with_evidence = [r for r in rows if r.get("available")]
    return {
        "market": market_spec.code,
        "horizon": horizon,
        "n_instruments": len(rows),
        "n_with_evidence": len(with_evidence),
        "rows": rows,
        "note": (
            f"{len(rows) - len(with_evidence)} of {len(rows)} instruments have insufficient "
            "comparable history at this horizon. They are listed rather than dropped: an "
            "absent row would read as 'no setup' when it means 'not enough evidence'."
        ),
    }
