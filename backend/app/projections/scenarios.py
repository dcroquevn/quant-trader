"""Statistical scenarios from historical analogues.

Turns the analogue distribution into three labelled cases. The labels are the point of
friction: "bear / base / bull" invites being read as a forecast with a most-likely middle,
and it is not that. So every scenario carries its own definition, and
:attr:`Scenario.basis` states in words which percentile it is and what that means.

What a scenario is
------------------
``bear``  the 10th percentile of what followed similar setups
``base``  the **median** — not the mean, and not a most-likely value
``bull``  the 90th percentile

The median is used because a mean is dragged around by one extreme observation, and with
twenty to a hundred observations that happens routinely. It is still a description of past
observations, not a central expectation: half of those setups did worse, which is a
different statement from "this will probably return about that".

Why prices are shown alongside percentages
------------------------------------------
A percentage is easy to read as a target. A price level next to the current one makes the
range concrete, which is harder to over-trust. Both carry the same caveat.

What this cannot do
-------------------
It cannot tell you the probability of anything. A 63% positive share among 40 past
observations does not make the next outcome 63% likely — that would require the future to
be drawn from the same distribution, which is precisely the thing nobody can check. The
module refuses to phrase it that way anywhere, and :func:`format_scenarios` exists so no
display surface has to invent its own wording.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.logging import get_logger
from app.projections.analogues import AnalogueResult

logger = get_logger(__name__)

__all__ = [
    "Scenario",
    "ScenarioSet",
    "build_scenarios",
    "format_scenarios",
    "INSUFFICIENT_EVIDENCE",
]

INSUFFICIENT_EVIDENCE = "Insufficient historical evidence"
"""The exact phrase used when the sample is too thin, per the brief.

A first-class outcome, not an error. A median computed from six observations looks exactly
as authoritative as one from six hundred, so the only safe response to a thin sample is to
withhold the number and say why.
"""


@dataclass(frozen=True, slots=True)
class Scenario:
    """One labelled point in the historical distribution."""

    label: str
    percentile: int | None
    return_pct: float
    price: float | None
    basis: str
    """What this figure is, in words. Rendered verbatim so no surface has to paraphrase."""

    adverse_excursion_pct: float | None = None
    """Worst unrealised loss along the way, for this scenario's neighbourhood."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "percentile": self.percentile,
            "return_pct": round(self.return_pct, 4),
            "price": None if self.price is None else round(self.price, 6),
            "basis": self.basis,
            "adverse_excursion_pct": (
                None if self.adverse_excursion_pct is None
                else round(self.adverse_excursion_pct, 4)
            ),
        }


@dataclass(slots=True)
class ScenarioSet:
    """Scenarios for one instrument, or an explicit statement that there are none."""

    symbol: str
    market: str
    currency: str
    as_of: datetime
    horizon: int
    current_price: float | None

    available: bool = False
    reason: str = ""
    """When ``available`` is False, why. Carries :data:`INSUFFICIENT_EVIDENCE` verbatim."""

    scenarios: list[Scenario] = field(default_factory=list)
    n_observations: int = 0
    n_raw_matches: int = 0
    positive_share_pct: float | None = None

    baseline: dict[str, Any] = field(default_factory=dict)
    """The unconditional distribution, for comparison."""

    adds_information: bool = False
    """False when these percentiles merely restate the base rate."""

    setup: dict[str, float] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    @property
    def base(self) -> Scenario | None:
        return next((s for s in self.scenarios if s.label == "base"), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "currency": self.currency,
            "as_of": self.as_of.isoformat(),
            "horizon": self.horizon,
            "current_price": self.current_price,
            "available": self.available,
            "reason": self.reason,
            "n_observations": self.n_observations,
            "n_raw_matches": self.n_raw_matches,
            "positive_share_pct": self.positive_share_pct,
            "baseline": self.baseline,
            "adds_information": self.adds_information,
            "setup": {k: round(v, 6) for k, v in self.setup.items()},
            "scenarios": [s.to_dict() for s in self.scenarios],
            "caveats": self.caveats,
            "language_note": (
                "These are percentiles of what followed similar historical conditions. They "
                "are not forecasts, targets, or probabilities. 'Base case' means the median "
                "of those past observations -- half did worse -- not a most-likely future "
                "outcome."
            ),
        }


def build_scenarios(
    analogues: AnalogueResult,
    *,
    current_price: float | None = None,
    currency: str = "USD",
) -> ScenarioSet:
    """Turn an :class:`AnalogueResult` into labelled scenarios.

    When the analogue search found insufficient evidence, this returns a set with
    ``available=False`` and the reason. It does not fall back to a wider search, a shorter
    horizon, or a looser tolerance: quietly relaxing the criteria until a number appears is
    how "insufficient evidence" becomes "here is a median", and the number would be worse
    than nothing because it would look the same as a good one.
    """
    result = ScenarioSet(
        symbol=analogues.symbol,
        market=analogues.market,
        currency=currency,
        as_of=analogues.as_of,
        horizon=analogues.horizon,
        current_price=current_price,
        n_raw_matches=analogues.n_matches,
        n_observations=analogues.n_independent,
        setup=dict(analogues.setup),
        caveats=list(analogues.caveats),
    )

    if not analogues.sufficient:
        result.available = False
        result.reason = f"{INSUFFICIENT_EVIDENCE}: {analogues.insufficiency_reason}"
        return result

    statistics = analogues.statistics
    n = analogues.n_independent
    horizon = analogues.horizon

    def price_at(return_pct: float) -> float | None:
        if current_price is None:
            return None
        return current_price * (1.0 + return_pct / 100.0)

    scenarios = [
        Scenario(
            label="bear",
            percentile=10,
            return_pct=statistics["p10_return_pct"],
            price=price_at(statistics["p10_return_pct"]),
            basis=(
                f"10th percentile of {n} similar historical situations: one in ten did this "
                "badly or worse over the following "
                f"{horizon} bars."
            ),
            adverse_excursion_pct=statistics["worst_adverse_excursion_pct"],
        ),
        Scenario(
            label="base",
            percentile=50,
            return_pct=statistics["median_return_pct"],
            price=price_at(statistics["median_return_pct"]),
            basis=(
                f"Median of {n} similar historical situations. Half did worse. This is a "
                "description of those observations, not a most-likely future outcome."
            ),
            adverse_excursion_pct=statistics["median_adverse_excursion_pct"],
        ),
        Scenario(
            label="bull",
            percentile=90,
            return_pct=statistics["p90_return_pct"],
            price=price_at(statistics["p90_return_pct"]),
            basis=(
                f"90th percentile of {n} similar historical situations: one in ten did this "
                f"well or better over the following {horizon} bars."
            ),
            adverse_excursion_pct=statistics["median_favorable_excursion_pct"],
        ),
    ]

    result.available = True
    result.scenarios = scenarios
    result.positive_share_pct = statistics["positive_share_pct"]
    result.baseline = dict(analogues.baseline)
    result.adds_information = analogues.adds_information

    # A range that straddles zero is the normal case and worth naming, because a positive
    # base case invites reading the whole thing as bullish.
    if statistics["p10_return_pct"] < 0 < statistics["p90_return_pct"]:
        result.caveats.append(
            f"The 10th-to-90th percentile range spans {statistics['p10_return_pct']:+.1f}% "
            f"to {statistics['p90_return_pct']:+.1f}%, i.e. it straddles zero. Similar "
            "setups produced both meaningful gains and meaningful losses."
        )

    spread = statistics["p90_return_pct"] - statistics["p10_return_pct"]
    median = statistics["median_return_pct"]
    if abs(median) > 1e-9 and spread > abs(median) * 6:
        result.caveats.append(
            f"The spread between the 10th and 90th percentiles ({spread:.1f} points) is "
            f"more than six times the median ({median:+.1f}%). The distribution is wide "
            "relative to its centre, so the median carries little information about any "
            "single outcome."
        )

    return result


def format_scenarios(scenarios: ScenarioSet) -> str:
    """Plain-text rendering, for the CLI and for logs.

    Exists so no display surface writes its own phrasing and accidentally turns a
    percentile into a forecast.
    """
    if not scenarios.available:
        return (
            f"{scenarios.symbol} ({scenarios.market}) — {scenarios.reason}\n"
            "No projection is shown. A median computed from too few observations looks "
            "exactly as authoritative as a good one."
        )

    lines = [
        f"{scenarios.symbol} ({scenarios.market}) -- {scenarios.horizon}-bar outcomes after "
        f"{scenarios.n_observations} similar historical situations",
        f"  {scenarios.positive_share_pct:.0f}% of those situations were followed by a gain.",
        "",
    ]
    for scenario in scenarios.scenarios:
        # ASCII only: this is plain text that may land on a cp1252 console, and a
        # UnicodeEncodeError from a decorative arrow is a silly way to lose a report.
        price = (
            f"  ->  {scenario.price:,.2f} {scenarios.currency}"
            if scenario.price is not None
            else ""
        )
        lines.append(
            f"  {scenario.label.upper():<5} p{scenario.percentile:<3} "
            f"{scenario.return_pct:+7.2f}%{price}"
        )
    lines.append("")

    baseline = scenarios.baseline
    if baseline.get("available"):
        marker = "" if scenarios.adds_information else "   <-- essentially the same"
        lines.append(
            f"  base rate (all bars): median {baseline['median_return_pct']:+.2f}%  "
            f"p10 {baseline['p10_return_pct']:+.2f}%  "
            f"p90 {baseline['p90_return_pct']:+.2f}%{marker}"
        )
        if not scenarios.adds_information:
            lines.append(
                "  The matching did not isolate anything: these figures restate how these "
                "instruments behaved generally."
            )
        lines.append("")

    lines.append("  These are percentiles of past observations, not predictions.")
    return "\n".join(lines)


def scenarios_as_evidence(scenarios: ScenarioSet) -> dict[str, Any]:
    """Compact form for the ``signals.evidence`` column and the decision log.

    Section 38 of the brief requires that every recorded decision carry the evidence behind
    it. This is that payload: enough to reconstruct what the projection said, small enough
    to store on every signal.
    """
    if not scenarios.available:
        return {
            "available": False,
            "reason": scenarios.reason,
            "n_independent_observations": scenarios.n_observations,
            "n_raw_matches": scenarios.n_raw_matches,
        }

    base = scenarios.base
    return {
        "available": True,
        "horizon_bars": scenarios.horizon,
        "n_independent_observations": scenarios.n_observations,
        "n_raw_matches": scenarios.n_raw_matches,
        "median_return_pct": None if base is None else round(base.return_pct, 4),
        "p10_return_pct": next(
            (round(s.return_pct, 4) for s in scenarios.scenarios if s.label == "bear"), None
        ),
        "p90_return_pct": next(
            (round(s.return_pct, 4) for s in scenarios.scenarios if s.label == "bull"), None
        ),
        "positive_share_pct": scenarios.positive_share_pct,
        "adds_information": scenarios.adds_information,
        "baseline_median_return_pct": scenarios.baseline.get("median_return_pct"),
        "median_adverse_excursion_pct": (
            None if base is None else base.adverse_excursion_pct
        ),
        "n_caveats": len(scenarios.caveats),
        "interpretation": (
            "Percentiles of what followed similar past conditions. Not a forecast and not a "
            "probability."
        ),
    }
