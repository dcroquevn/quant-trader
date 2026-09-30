"""The objective function.

Optimising for return alone reliably produces a strategy that concentrates everything
into a handful of lucky trades and drops 60% on the way. So the score is a weighted
combination of return, risk-adjusted return, drawdown, turnover and trade count, with
the weights configurable because different people want different things.

Two design choices worth defending
----------------------------------
**Components are normalised before weighting.** Sharpe lives around 0–3, total return
around 0–200, drawdown around 0 to −60. Weighting raw values would make the weights
meaningless — a weight of 1.0 on return would swamp a weight of 10 on Sharpe. Each
component is mapped onto a roughly comparable scale first, and the mapping is stated in
the component's docstring rather than buried.

**A missing metric scores zero, and the result says so.** When Sharpe is undefined —
zero volatility, too few bars — its component contributes nothing rather than being
skipped. Skipping it would silently re-weight the remaining terms and let a degenerate
result outrank a real one.

The fragility penalty
---------------------
:class:`ObjectiveWeights.fragility_penalty` is applied by the *search*, not here: it
needs the scores of neighbouring parameter points, which a single result does not have.
:func:`apply_fragility_penalty` does that once the neighbourhood is known. A
configuration that scores well only at exactly its own settings is the definition of an
overfit one, and this is where that gets priced in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ObjectiveWeights",
    "ObjectiveScore",
    "score_result",
    "apply_fragility_penalty",
    "PRESETS",
]


@dataclass(frozen=True, slots=True)
class ObjectiveWeights:
    """How much each component counts.

    Positive weights reward, negative weights punish. The defaults deliberately do not
    put return first: ``sharpe`` outweighs ``total_return`` because a sample that
    produced a high return with wild volatility is far less likely to repeat.
    """

    total_return: float = 1.0
    sharpe: float = 2.0
    sortino: float = 1.0
    max_drawdown: float = 2.0
    """Weight on the drawdown penalty. Positive; the component itself is negative."""

    turnover: float = 0.5
    """Weight on the turnover penalty. High turnover makes results cost-assumption bound."""

    trade_count: float = 0.5
    """Weight on the sample-size term. Rewards having enough trades to mean anything."""

    volatility: float = 0.5
    """Weight on the volatility penalty."""

    fragility_penalty: float = 1.0
    """Weight on the neighbourhood penalty, applied by the search."""

    min_trades: int = 30
    """Below this the sample-size term is negative rather than merely small.

    A configuration with eight trades has not been measured, it has been glimpsed. Thirty
    is still thin, but it is the point below which a win rate carries almost no
    information.
    """

    def __post_init__(self) -> None:
        for name in ("max_drawdown", "turnover", "volatility", "fragility_penalty"):
            if getattr(self, name) < 0:
                raise ValueError(
                    f"{name} is a penalty weight and must be non-negative; the component "
                    f"it scales is already negative. Got {getattr(self, name)}."
                )
        if self.min_trades < 1:
            raise ValueError(f"min_trades must be at least 1, got {self.min_trades}")
        if self.total() == 0:
            raise ValueError(
                "All weights are zero, so every configuration would score identically."
            )

    def total(self) -> float:
        return sum(
            abs(v)
            for k, v in self.to_dict().items()
            if k not in {"min_trades"}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_return": self.total_return,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "max_drawdown": self.max_drawdown,
            "turnover": self.turnover,
            "trade_count": self.trade_count,
            "volatility": self.volatility,
            "fragility_penalty": self.fragility_penalty,
            "min_trades": self.min_trades,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ObjectiveWeights":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ValueError(
                f"Unknown objective weights {sorted(unknown)}. Known: {sorted(known)}"
            )
        return cls(**data)


PRESETS: dict[str, ObjectiveWeights] = {
    "balanced": ObjectiveWeights(),
    "return": ObjectiveWeights(
        total_return=3.0, sharpe=1.0, sortino=0.5, max_drawdown=1.0,
        turnover=0.25, trade_count=0.5, volatility=0.25,
    ),
    "risk_adjusted": ObjectiveWeights(
        total_return=0.5, sharpe=3.0, sortino=2.0, max_drawdown=2.0,
        turnover=0.5, trade_count=0.5, volatility=1.0,
    ),
    "capital_preservation": ObjectiveWeights(
        total_return=0.5, sharpe=1.5, sortino=1.5, max_drawdown=4.0,
        turnover=1.0, trade_count=0.5, volatility=2.0,
    ),
    "stability": ObjectiveWeights(
        total_return=1.0, sharpe=2.0, sortino=1.0, max_drawdown=2.0,
        turnover=1.0, trade_count=1.0, volatility=1.0, fragility_penalty=3.0,
    ),
}
"""Named weightings. ``stability`` triples the fragility penalty, so it prefers a
configuration whose neighbours also work over one that scores higher alone."""


@dataclass(slots=True)
class ObjectiveScore:
    """A scored result, with every component kept for inspection."""

    value: float
    components: dict[str, float]
    """Normalised component values, before weighting."""

    contributions: dict[str, float]
    """Weighted contributions. These sum to ``value`` before any fragility penalty."""

    missing: list[str] = field(default_factory=list)
    """Metrics that were undefined and therefore contributed zero."""

    notes: list[str] = field(default_factory=list)
    fragility: float | None = None
    """Set once the search knows the neighbourhood. 0 means neighbours score the same."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 6),
            "components": {k: round(v, 6) for k, v in self.components.items()},
            "contributions": {k: round(v, 6) for k, v in self.contributions.items()},
            "missing": list(self.missing),
            "notes": list(self.notes),
            "fragility": None if self.fragility is None else round(self.fragility, 6),
        }


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def _squash(value: float, scale: float) -> float:
    """Map an unbounded value onto ``(-1, 1)`` with ``tanh``.

    Keeps one spectacular outlier from dominating the whole objective: the difference
    between a 300% and a 3,000% return should not be thirty times the difference between
    10% and 100%, because at those magnitudes the sample is telling you about one trade,
    not about a strategy.
    """
    if scale <= 0:
        raise ValueError(f"scale must be positive, got {scale}")
    return math.tanh(value / scale)


def score_result(
    metrics: dict[str, Any],
    weights: ObjectiveWeights | None = None,
) -> ObjectiveScore:
    """Score one backtest's metrics. Higher is better.

    The scale is arbitrary and only meaningful *relative to other configurations scored
    with the same weights*. It is not a return, a probability, or a quality rating, and
    nothing should present it as one.
    """
    w = weights or ObjectiveWeights()
    components: dict[str, float] = {}
    missing: list[str] = []
    notes: list[str] = []

    def metric(name: str) -> float | None:
        value = metrics.get(name)
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

    # ---- Return ---------------------------------------------------------
    # Squashed at 100%, chosen so the *realistic* range still separates: +40% scores
    # 0.38, +100% scores 0.76, +200% scores 0.96. A tighter scale (50) saturated by
    # 100%, which made a 200% return indistinguishable from a 40% one and meant even
    # the return-focused preset preferred the steadier result — the squashing is there
    # to stop absurd outliers dominating, not to flatten the range people actually see.
    total_return = metric("total_return_pct")
    if total_return is None:
        missing.append("total_return_pct")
        components["total_return"] = 0.0
    else:
        components["total_return"] = _squash(total_return, 100.0)

    # ---- Sharpe: squashed at 1.0. Sharpe 1 scores ~0.76, Sharpe 3 ~0.995 ----
    sharpe = metric("sharpe")
    if sharpe is None:
        missing.append("sharpe")
        components["sharpe"] = 0.0
    else:
        components["sharpe"] = _squash(sharpe, 1.0)

    sortino = metric("sortino")
    if sortino is None:
        missing.append("sortino")
        components["sortino"] = 0.0
    else:
        components["sortino"] = _squash(sortino, 1.5)

    # ---- Drawdown: a penalty, so the component is negative. -20% scores -0.76 ----
    drawdown = metric("max_drawdown_pct")
    if drawdown is None:
        missing.append("max_drawdown_pct")
        components["max_drawdown"] = 0.0
    else:
        components["max_drawdown"] = -_squash(abs(drawdown), 20.0)

    # ---- Turnover: penalty. 300%/yr scores -0.76; 1,000% is nearly saturated ----
    turnover = metric("turnover_pct")
    if turnover is None:
        # No trades means no turnover, which is not a virtue; the trade-count term
        # handles that case.
        components["turnover"] = 0.0
    else:
        components["turnover"] = -_squash(abs(turnover), 300.0)

    # ---- Volatility: penalty, squashed at 20% annualised ----
    volatility = metric("annualised_volatility_pct")
    if volatility is None:
        missing.append("annualised_volatility_pct")
        components["volatility"] = 0.0
    else:
        components["volatility"] = -_squash(abs(volatility), 20.0)

    # ---- Trade count: rewards a sample big enough to mean something ----
    n_trades = int(metrics.get("n_trades") or 0)
    if n_trades == 0:
        components["trade_count"] = -1.0
        notes.append("No trades were taken, so nothing was measured.")
    elif n_trades < w.min_trades:
        # Linear from -1 at zero trades to 0 at the minimum.
        components["trade_count"] = -(1.0 - n_trades / w.min_trades)
        notes.append(
            f"Only {n_trades} trades, below the {w.min_trades} needed for the statistics "
            "to carry much information."
        )
    else:
        components["trade_count"] = _squash(n_trades - w.min_trades, 100.0)

    contributions = {
        "total_return": w.total_return * components["total_return"],
        "sharpe": w.sharpe * components["sharpe"],
        "sortino": w.sortino * components["sortino"],
        "max_drawdown": w.max_drawdown * components["max_drawdown"],
        "turnover": w.turnover * components["turnover"],
        "volatility": w.volatility * components["volatility"],
        "trade_count": w.trade_count * components["trade_count"],
    }

    if missing:
        notes.append(
            f"{len(missing)} metric(s) were undefined and contributed zero: "
            f"{', '.join(missing)}. They were not dropped, because dropping them would "
            "silently re-weight everything else."
        )

    return ObjectiveScore(
        value=sum(contributions.values()),
        components=components,
        contributions=contributions,
        missing=missing,
        notes=notes,
    )


def apply_fragility_penalty(
    score: ObjectiveScore,
    neighbour_values: list[float],
    weights: ObjectiveWeights | None = None,
) -> ObjectiveScore:
    """Penalise a configuration whose neighbours score much worse.

    ``fragility`` is how far the point's own score sits above the mean of its
    neighbours, normalised by the spread of the whole neighbourhood. A value near zero
    means the parameters sit on a plateau; a large value means a spike, and a spike in a
    backtest is almost always a fitted artefact rather than a discovered edge.

    Only *positive* deviations are penalised. A point that scores worse than its
    neighbours is not fragile — it is simply bad, and the base score already says so.
    """
    w = weights or ObjectiveWeights()
    if not neighbour_values or w.fragility_penalty == 0:
        return score

    mean = sum(neighbour_values) / len(neighbour_values)
    spread = max(neighbour_values) - min(neighbour_values)
    everything = [*neighbour_values, score.value]
    full_spread = max(everything) - min(everything)

    # Normalise by the neighbourhood's own spread so the penalty is scale-free. A flat
    # neighbourhood has no spread, and a point identical to flat neighbours is not
    # fragile at all.
    denominator = max(full_spread, spread, 1e-9)
    excess = max(0.0, score.value - mean)
    fragility = excess / denominator

    penalty = w.fragility_penalty * fragility
    score.fragility = fragility
    score.contributions["fragility_penalty"] = -penalty
    score.value -= penalty

    if fragility > 0.5:
        score.notes.append(
            f"Fragile: this configuration scores {excess:.3f} above the mean of its "
            f"{len(neighbour_values)} neighbours, {fragility:.0%} of the neighbourhood's "
            "spread. A result that holds only at these exact settings is usually fitted "
            "to the sample rather than discovered in it."
        )
    return score
