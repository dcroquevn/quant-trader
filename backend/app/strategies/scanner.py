"""Universe scanner.

Evaluates a strategy against every instrument's most recent bar and ranks the results.
It answers "what does the strategy think right now?", which is a different question from
"did the strategy work", and it is important that the two never get confused: a scanner
row is a statement about current conditions, not evidence of anything.

What a row is and is not
------------------------
``score`` counts how many of the strategy's conditions currently hold. It is ordinal.
It is not a probability of profit, an expected return, or a confidence level, and
:attr:`ScanRow.score_description` carries a phrasing any surface can use safely.

Rows carry their data-quality state (``stale``, ``carried_forward_dropped``,
``bars_available``) because a BUY on an instrument whose last real print was three weeks
ago is not a signal, and the scanner refuses to present it as one: such rows are marked
``tradable=False`` with a reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.core.exceptions import InsufficientDataError, StaleDataError
from app.core.logging import get_logger, log_decision
from app.core.markets import all_market_codes, get_market
from app.core.universe import AssetSpec, universe_for_market
from app.data.engine import DataEngine
from app.data.provider import Timeframe
from app.indicators.registry import MIN_BARS_FOR_FULL_FEATURES, compute_features
from app.strategies.base import Action, Strategy

logger = get_logger(__name__)

__all__ = ["ScanRow", "ScanResult", "scan_market", "scan_all"]


@dataclass(slots=True)
class ScanRow:
    """One instrument's current reading."""

    symbol: str
    market: str
    currency: str
    as_of: datetime
    price: float | None

    action: str = Action.HOLD.value
    score: float = 0.0

    trend_score: float | None = None
    momentum_score: float | None = None
    rsi: float | None = None
    macd_hist: float | None = None
    relative_volume: float | None = None
    atr_pct: float | None = None
    dist_52w_high_pct: float | None = None
    return_20d: float | None = None
    dollar_volume: float | None = None

    stop_price: float | None = None
    take_profit_price: float | None = None
    risk_reward: float | None = None
    """Target distance divided by stop distance. A property of the *plan*, not of the
    outcome: it says nothing about how likely either level is to be reached."""

    bars_available: int = 0
    carried_forward_dropped: int = 0
    stale: bool = False
    volume_feed_degraded: bool = False
    """Recent bars report no volume, so every volume-based condition is unevaluable.

    Surfaced on the row because the alternative is a bland HOLD that looks like a
    verdict: the strategy's volume gate simply cannot pass, and a reader would conclude
    "no setups" rather than "the input is broken".
    """

    recent_zero_volume_pct: float = 0.0
    tradable: bool = True
    blocked_reason: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def score_description(self) -> str:
        return (
            f"{self.score:.2f} — counts how many strategy conditions currently hold. "
            "Not a probability of profit."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "currency": self.currency,
            "as_of": self.as_of.isoformat(),
            "price": self.price,
            "action": self.action,
            "score": round(self.score, 4),
            "score_description": self.score_description,
            "trend_score": self.trend_score,
            "momentum_score": self.momentum_score,
            "rsi": self.rsi,
            "macd_hist": self.macd_hist,
            "relative_volume": self.relative_volume,
            "atr_pct": self.atr_pct,
            "dist_52w_high_pct": self.dist_52w_high_pct,
            "return_20d": self.return_20d,
            "dollar_volume": self.dollar_volume,
            "stop_price": self.stop_price,
            "take_profit_price": self.take_profit_price,
            "risk_reward": self.risk_reward,
            "bars_available": self.bars_available,
            "carried_forward_dropped": self.carried_forward_dropped,
            "stale": self.stale,
            "volume_feed_degraded": self.volume_feed_degraded,
            "recent_zero_volume_pct": self.recent_zero_volume_pct,
            "tradable": self.tradable,
            "blocked_reason": self.blocked_reason,
            "reasons": self.reasons,
        }


@dataclass(slots=True)
class ScanResult:
    """A whole scan, with the rows it could not evaluate recorded separately."""

    strategy: dict[str, Any]
    scanned_at: datetime
    rows: list[ScanRow] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    """Instruments that could not be evaluated, and why. Never silently dropped."""

    disclaimer: str = (
        "Every figure describes current and past prices. Nothing here forecasts a future "
        "outcome, and the score counts conditions rather than estimating a probability."
    )

    def filtered(
        self,
        *,
        market: str | None = None,
        action: str | None = None,
        min_score: float | None = None,
        tradable_only: bool = False,
        min_dollar_volume: float | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> list[ScanRow]:
        rows = list(self.rows)
        if market and market.upper() != "ALL":
            code = get_market(market).code
            rows = [r for r in rows if r.market == code]
        if action and action.upper() != "ALL":
            rows = [r for r in rows if r.action == action.upper()]
        if min_score is not None:
            rows = [r for r in rows if r.score >= min_score]
        if tradable_only:
            rows = [r for r in rows if r.tradable]
        if min_dollar_volume is not None:
            rows = [
                r for r in rows
                if r.dollar_volume is not None and r.dollar_volume >= min_dollar_volume
            ]
        if min_price is not None:
            rows = [r for r in rows if r.price is not None and r.price >= min_price]
        if max_price is not None:
            rows = [r for r in rows if r.price is not None and r.price <= max_price]
        return rows

    def sorted_by(self, key: str = "score", descending: bool = True) -> list[ScanRow]:
        """Sort rows by a named column, with missing values always last.

        Missing values sort last in both directions on purpose: a null is "not measured",
        and letting it win a ranking because ``None`` compares low is how an instrument
        with no data ends up at the top of a BUY list.
        """
        allowed = {
            "score", "price", "rsi", "relative_volume", "atr_pct", "return_20d",
            "dist_52w_high_pct", "risk_reward", "dollar_volume", "momentum_score",
            "trend_score", "symbol",
        }
        if key not in allowed:
            raise KeyError(f"Cannot sort by {key!r}. Allowed: {sorted(allowed)}")

        def sort_key(row: ScanRow):
            value = getattr(row, key)
            if value is None:
                return (1, 0)
            if isinstance(value, str):
                return (0, value)
            return (0, -value if descending else value)

        rows = sorted(self.rows, key=sort_key)
        if key == "symbol" and descending:
            rows.reverse()
        return rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "scanned_at": self.scanned_at.isoformat(),
            "n_rows": len(self.rows),
            "n_errors": len(self.errors),
            "disclaimer": self.disclaimer,
            "rows": [r.to_dict() for r in self.rows],
            "errors": self.errors,
        }


def _scan_symbol(
    engine: DataEngine,
    strategy: Strategy,
    spec: AssetSpec,
    timeframe: Timeframe,
    *,
    as_of: datetime,
    log_decisions: bool,
) -> ScanRow:
    frame = engine.load_spec(spec, timeframe, trim_carried_forward=True)
    dropped = int(frame.attrs.get("carried_forward_dropped", 0))

    if frame.empty:
        raise InsufficientDataError(
            f"{spec.symbol}: no usable bars"
            + (f" ({dropped} carried-forward bars removed)" if dropped else "")
        )
    if len(frame) < MIN_BARS_FOR_FULL_FEATURES:
        raise InsufficientDataError(
            f"{spec.symbol}: {len(frame)} bars, needs {MIN_BARS_FOR_FULL_FEATURES}"
        )

    features = compute_features(frame)
    decision = strategy.evaluate(features, in_position=False)
    last = features.iloc[-1]

    def value(column: str) -> float | None:
        if column not in features.columns:
            return None
        raw = last[column]
        return None if pd.isna(raw) else float(raw)

    row = ScanRow(
        symbol=spec.symbol,
        market=spec.market,
        currency=spec.currency,
        as_of=features.index[-1].to_pydatetime(),
        price=value("close"),
        action=decision.action.value,
        score=decision.score,
        trend_score=value("trend_score"),
        momentum_score=value("momentum_score"),
        rsi=value("rsi_14"),
        macd_hist=value("macd_hist"),
        relative_volume=value("relative_volume_20"),
        atr_pct=value("atr_pct_14"),
        dist_52w_high_pct=value("dist_52w_high_pct"),
        return_20d=value("return_20d"),
        dollar_volume=value("dollar_volume_20"),
        stop_price=decision.stop_price,
        take_profit_price=decision.take_profit_price,
        bars_available=len(frame),
        carried_forward_dropped=dropped,
        reasons=list(decision.reasons),
    )

    if decision.stop_price and decision.take_profit_price and row.price:
        risk = row.price - decision.stop_price
        reward = decision.take_profit_price - row.price
        if risk > 0:
            row.risk_reward = round(reward / risk, 3)

    # Data-quality gate. A BUY on a series whose last real print is weeks old is not a
    # signal, and presenting it as one is the failure this whole path exists to avoid.
    report = engine.audit_symbol(spec, timeframe, as_of=as_of)
    row.volume_feed_degraded = report.volume_feed_degraded
    row.recent_zero_volume_pct = report.recent_zero_volume_pct

    try:
        engine.assert_fresh(spec, timeframe, as_of=as_of)
    except StaleDataError as exc:
        row.stale = True
        row.tradable = False
        row.blocked_reason = str(exc)

    if row.volume_feed_degraded and row.tradable:
        # Not fatal on its own -- a strategy with no volume component is unaffected --
        # but a volume-gated strategy cannot produce a meaningful verdict here.
        row.tradable = False
        row.blocked_reason = (
            f"{report.recent_zero_volume_pct:.0f}% of recent bars report zero volume, "
            "so every volume-based condition is unevaluable. This is a data problem, "
            "not an absence of setups."
        )

    if log_decisions and decision.is_actionable:
        log_decision(
            symbol=spec.symbol,
            market=spec.market,
            action=decision.action.value,
            reasons=list(decision.reasons),
            features=decision.features,
            evidence={
                "note": "no historical-analogue evidence yet; Phase 5 adds it",
                "bars_available": len(frame),
                "tradable": row.tradable,
            },
        )

    return row


def scan_market(
    session: Session,
    strategy: Strategy,
    market: str,
    *,
    timeframe: "str | Timeframe" = Timeframe.D1,
    symbols: list[str] | None = None,
    as_of: datetime | None = None,
    log_decisions: bool = True,
) -> ScanResult:
    """Scan one market's universe."""
    from app.core.universe import find_asset

    code = get_market(market).code
    tf = Timeframe.parse(timeframe)
    reference = as_of or datetime.now(timezone.utc)

    specs = (
        [find_asset(s, code) for s in symbols]
        if symbols
        else list(universe_for_market(code))
    )

    engine = DataEngine(session)
    result = ScanResult(strategy=strategy.describe(), scanned_at=reference)

    for spec in specs:
        try:
            result.rows.append(
                _scan_symbol(
                    engine, strategy, spec, tf, as_of=reference, log_decisions=log_decisions
                )
            )
        except Exception as exc:  # noqa: BLE001 -- one bad symbol must not stop the scan
            result.errors.append(
                {"symbol": spec.symbol, "market": spec.market, "error": str(exc)}
            )
            logger.debug("Scan skipped %s: %s", spec.symbol, exc)

    return result


def scan_all(
    session: Session,
    strategy: Strategy,
    *,
    timeframe: "str | Timeframe" = Timeframe.D1,
    as_of: datetime | None = None,
    log_decisions: bool = True,
) -> ScanResult:
    """Scan every configured market into one result."""
    reference = as_of or datetime.now(timezone.utc)
    combined = ScanResult(strategy=strategy.describe(), scanned_at=reference)

    for code in all_market_codes():
        partial = scan_market(
            session,
            strategy,
            code,
            timeframe=timeframe,
            as_of=reference,
            log_decisions=log_decisions,
        )
        combined.rows.extend(partial.rows)
        combined.errors.extend(partial.errors)

    return combined
