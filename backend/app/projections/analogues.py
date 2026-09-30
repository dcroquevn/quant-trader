"""Historical analogue search.

Answers one question: **when conditions looked like this before, what happened next?**

It never answers "what will happen next", and the distinction is not rhetorical. Every
number this module produces is a summary of past observations, and the code is arranged so
that presenting one as a forecast requires deliberately ignoring the fields that say
otherwise.

The overlap problem, which is the whole difficulty
--------------------------------------------------
Take every bar as an observation and measure the next 20 days from each. Two observations
one day apart share 19 of those 20 days. They are not independent, and treating them as
though they were is the single most common error in retail backtesting tools: "127
historical observations" sounds like a sample worth trusting when it may contain only six
or seven independent ones.

So this module reports two counts and bases every judgement on the smaller:

``n_matches``
    Raw bars whose conditions resembled the setup.

``n_independent``
    Matches remaining after enforcing a separation of at least ``horizon`` bars, so no two
    retained observations share any forward window. This is the effective sample size, and
    it is what :attr:`AnalogueResult.sufficient` is computed from.

Pooling across instruments has the same problem in the cross-section: fifteen US large
caps on one day are nearer to one observation than to fifteen, because they move together.
Matches are therefore also thinned so that no two share a *date*, and the caveat says so.

Where the forward-looking data comes from
-----------------------------------------
:mod:`app.indicators.forward` is quarantined precisely because it reads the future. This
module is its one legitimate consumer: measuring what followed a past setup is the entire
point. The guard is direction — forward values are used only to summarise *completed*
history, never to decide anything, and a bar whose forward window has not finished is
excluded rather than partially counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from app.core.exceptions import InsufficientDataError
from app.core.logging import get_logger
from app.indicators.forward import (
    forward_max_adverse_excursion,
    forward_max_favorable_excursion,
    forward_return,
)

logger = get_logger(__name__)

__all__ = [
    "MatchFeature",
    "DEFAULT_MATCH_FEATURES",
    "AnalogueMatch",
    "AnalogueResult",
    "find_analogues",
    "MIN_INDEPENDENT_OBSERVATIONS",
    "MIN_RAW_MATCHES",
    "DEFAULT_MAX_DISTANCE",
    "BASELINE_DIVERGENCE_THRESHOLD",
]

MIN_INDEPENDENT_OBSERVATIONS = 20
"""Independent observations below which no statistics are reported.

Twenty non-overlapping observations is already thin — a percentile estimated from twenty
points has a wide confidence interval — but below it the numbers stop carrying information
at all. The result then says "insufficient historical evidence" rather than printing a
median that would be read as meaningful.
"""

MIN_RAW_MATCHES = 30
"""Raw matches below which the search is abandoned before thinning."""

DEFAULT_MAX_DISTANCE = 0.3
"""Default similarity ceiling, in weighted standard deviations.

Calibrated rather than chosen. At 1.0 the search matched 54% of all candidate bars and the
resulting distribution was indistinguishable from the unconditional one — median +2.05%
against a base rate of +2.12%, p10 -8.27% against -7.78%. A "conditional" distribution that
reproduces the base rate is worse than no distribution, because it carries the authority of
a finding without the content.

At 0.3 the search matches roughly 2-4% of candidates and the distribution genuinely
differs. Tighter finds more distinctive setups and fewer of them; the trade-off is real and
:func:`find_analogues` reports which side of it a given run landed on.
"""

BASELINE_DIVERGENCE_THRESHOLD = 1.0
"""Percentage points of difference from the base rate below which a result adds nothing.

Compares the conditional median and 10th percentile against the unconditional ones. Under
this, the matching has not isolated anything and the result says so.
"""


@dataclass(frozen=True, slots=True)
class MatchFeature:
    """One dimension of similarity.

    ``tolerance`` is in standard deviations of that feature over the searched history, not
    in the feature's own units. That makes a single tolerance meaningful across RSI
    (0-100), relative volume (around 1) and ATR percentage (single digits) without hand-
    tuning each one — and it means "similar" adapts to how variable the feature actually is
    in this instrument.
    """

    name: str
    tolerance: float = 0.5
    weight: float = 1.0

    def __post_init__(self) -> None:
        if self.tolerance <= 0:
            raise ValueError(f"{self.name}: tolerance must be positive, got {self.tolerance}")
        if self.weight <= 0:
            raise ValueError(f"{self.name}: weight must be positive, got {self.weight}")


DEFAULT_MATCH_FEATURES: tuple[MatchFeature, ...] = (
    # Momentum position: where in its own range the oscillator sits.
    MatchFeature("rsi_14", tolerance=0.4, weight=1.5),
    # Trend context: how far above or below the long average price is. Scale-free, so it
    # compares across a USD stock and a CLP one.
    MatchFeature("dist_ema_200_pct", tolerance=0.5, weight=1.5),
    # Participation.
    MatchFeature("relative_volume_20", tolerance=0.5, weight=1.0),
    # Momentum direction and strength.
    MatchFeature("macd_hist", tolerance=0.5, weight=1.0),
    # Volatility regime: a setup in a calm market is not the same setup in a wild one.
    MatchFeature("atr_pct_14", tolerance=0.5, weight=1.0),
    # Position in the yearly range, which carries what two separate distance features
    # cannot: one normalised number finds far more comparable observations.
    MatchFeature("position_52w_range", tolerance=0.4, weight=1.0),
)
"""Six scale-free dimensions. Deliberately few.

Each extra dimension shrinks the number of matches roughly geometrically — the curse of
dimensionality is not an abstraction here, it is the difference between 120 matches and
four. Six is about the most that leaves a usable sample over ten years of daily bars.
"""


@dataclass(slots=True)
class AnalogueMatch:
    """One historical bar whose conditions resembled the setup."""

    symbol: str
    timestamp: datetime
    distance: float
    """Weighted Euclidean distance in standardised feature space. Lower is more similar."""

    features: dict[str, float]
    forward_return_pct: float
    max_adverse_excursion_pct: float
    max_favorable_excursion_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "distance": round(self.distance, 4),
            "forward_return_pct": round(self.forward_return_pct, 4),
            "max_adverse_excursion_pct": round(self.max_adverse_excursion_pct, 4),
            "max_favorable_excursion_pct": round(self.max_favorable_excursion_pct, 4),
        }


@dataclass(slots=True)
class AnalogueResult:
    """What happened after historically similar setups.

    ``sufficient`` is the field that matters. When it is False the statistics are absent
    and :attr:`insufficiency_reason` explains why — a thin sample produces a median that
    looks exactly as authoritative as a good one, so the honest response is to withhold it.
    """

    symbol: str
    market: str
    as_of: datetime
    horizon: int
    setup: dict[str, float]

    n_candidates: int = 0
    """Historical bars examined, i.e. those with a completed forward window."""

    n_matches: int = 0
    """Bars whose conditions resembled the setup, before thinning."""

    n_independent: int = 0
    """Matches left after removing overlapping forward windows. The effective sample."""

    sufficient: bool = False
    insufficiency_reason: str = ""

    statistics: dict[str, Any] = field(default_factory=dict)

    baseline: dict[str, Any] = field(default_factory=dict)
    """The unconditional distribution over the same bars, for comparison.

    Without it there is no way to tell a genuinely conditional finding from one that merely
    reproduces the base rate, and the two look identical on screen.
    """

    adds_information: bool = False
    """Whether the conditional distribution differs materially from the base rate."""

    matches: list[AnalogueMatch] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    pooled_symbols: list[str] = field(default_factory=list)

    @property
    def headline(self) -> str:
        """One sentence, phrased so it cannot be mistaken for a forecast."""
        if not self.sufficient:
            return (
                f"Insufficient historical evidence for {self.symbol}: "
                f"{self.insufficiency_reason}"
            )
        median = self.statistics.get("median_return_pct")
        positive = self.statistics.get("positive_share_pct")
        return (
            f"In {self.n_independent} non-overlapping historical situations resembling this "
            f"one, the median {self.horizon}-bar return that followed was "
            f"{median:+.2f}% and {positive:.0f}% were positive. This describes those past "
            "observations and is not a prediction."
        )

    def to_dict(self, *, include_matches: int = 20) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "as_of": self.as_of.isoformat(),
            "horizon": self.horizon,
            "setup": {k: round(v, 6) for k, v in self.setup.items()},
            "n_candidates": self.n_candidates,
            "n_matches": self.n_matches,
            "n_independent": self.n_independent,
            "sufficient": self.sufficient,
            "insufficiency_reason": self.insufficiency_reason,
            "statistics": self.statistics,
            "baseline": self.baseline,
            "adds_information": self.adds_information,
            "matches": [m.to_dict() for m in self.matches[:include_matches]],
            "caveats": self.caveats,
            "pooled_symbols": self.pooled_symbols,
            "headline": self.headline,
            "disclaimer": (
                "Every figure here summarises what followed similar conditions in the past. "
                "None is a forecast, an expected value, or a probability of any future "
                "outcome. The sample is one historical path over a universe of instruments "
                "that still exist, which makes it optimistic by an unknown amount."
            ),
        }


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def _standardise(
    frame: pd.DataFrame, features: tuple[MatchFeature, ...]
) -> tuple[pd.DataFrame, dict[str, tuple[float, float]]]:
    """Z-score the match features. Returns the standardised frame and the (mean, sd) used.

    A feature with zero variance is dropped: every bar would match on it, so it contributes
    nothing to similarity while still consuming a dimension. Standardising by zero would
    also produce infinities.
    """
    stats: dict[str, tuple[float, float]] = {}
    columns: dict[str, pd.Series] = {}

    for feature in features:
        if feature.name not in frame.columns:
            continue
        series = pd.to_numeric(frame[feature.name], errors="coerce")
        mean = float(series.mean())
        sd = float(series.std(ddof=1))
        if not np.isfinite(sd) or sd <= 1e-12:
            logger.debug("Dropping %s from matching: no variance in this sample", feature.name)
            continue
        stats[feature.name] = (mean, sd)
        columns[feature.name] = (series - mean) / sd

    return pd.DataFrame(columns, index=frame.index), stats


def _thin_overlapping(
    matches: list[AnalogueMatch], horizon: int
) -> list[AnalogueMatch]:
    """Keep the closest match, then drop anything whose forward window overlaps it.

    Greedy by similarity rather than chronological: when two nearby bars both matched, the
    one that actually resembled the setup more is the one worth keeping.

    Two matches overlap when they are fewer than ``horizon`` calendar-ish bars apart, so no
    retained pair shares any part of its forward window. Matches on the *same date* across
    different instruments are also dropped, because instruments in one market move together
    and fifteen of them on one day are nearer to one observation than to fifteen.
    """
    kept: list[AnalogueMatch] = []
    used_dates: set[Any] = set()

    for candidate in sorted(matches, key=lambda m: m.distance):
        date = candidate.timestamp.date()
        if date in used_dates:
            continue
        if any(
            m.symbol == candidate.symbol
            and abs((candidate.timestamp - m.timestamp).days) < horizon
            for m in kept
        ):
            continue
        kept.append(candidate)
        used_dates.add(date)

    return sorted(kept, key=lambda m: m.timestamp)


def find_analogues(
    frames: dict[str, pd.DataFrame],
    target_symbol: str,
    market: str,
    *,
    horizon: int = 20,
    features: tuple[MatchFeature, ...] = DEFAULT_MATCH_FEATURES,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    pool_across_symbols: bool = True,
    as_of_index: int = -1,
) -> AnalogueResult:
    """Find historical bars resembling ``target_symbol``'s current setup.

    Parameters
    ----------
    frames:
        Feature frames per symbol, from ``app.indicators.registry.compute_features``.
        Forward-looking columns are computed here rather than expected in the input, so a
        caller never has to hand this function a frame carrying outcome labels.
    horizon:
        Bars ahead to measure. Also the minimum separation used for thinning.
    max_distance:
        Weighted distance ceiling, in standard deviations. Larger admits looser matches and
        a bigger sample; the trade-off between sample size and actual similarity is
        unavoidable and is reported rather than hidden.
    pool_across_symbols:
        Pool matches from other instruments in the same market. Necessary for a usable
        sample, and it assumes those instruments are comparable — which is an assumption,
        recorded as a caveat.

    Raises
    ------
    InsufficientDataError
        The target has no usable current bar at all.
    """
    if horizon < 1:
        raise ValueError(f"horizon must be at least 1, got {horizon}")
    if target_symbol not in frames:
        raise InsufficientDataError(f"No feature frame supplied for {target_symbol}")

    target = frames[target_symbol]
    if target.empty:
        raise InsufficientDataError(f"{target_symbol} has no bars")

    position = as_of_index if as_of_index >= 0 else len(target) + as_of_index
    if not 0 <= position < len(target):
        raise InsufficientDataError(
            f"as_of_index {as_of_index} is outside {target_symbol}'s {len(target)} bars"
        )

    current = target.iloc[position]
    as_of = target.index[position].to_pydatetime()

    usable = [f for f in features if f.name in target.columns]
    missing = [f.name for f in features if f.name not in target.columns]
    setup = {f.name: current[f.name] for f in usable if pd.notna(current[f.name])}
    unready = [f.name for f in usable if pd.isna(current[f.name])]

    result = AnalogueResult(
        symbol=target_symbol,
        market=market,
        as_of=as_of,
        horizon=horizon,
        setup={k: float(v) for k, v in setup.items()},
    )

    if unready:
        result.insufficiency_reason = (
            f"the current bar has no value for {', '.join(unready)}, so there is nothing to "
            "match on"
        )
        return result
    if missing:
        result.caveats.append(
            f"Matched on {len(usable)} of {len(features)} dimensions; "
            f"{', '.join(missing)} were absent from the data."
        )

    # ---- Which instruments to search ----------------------------------
    searched = [target_symbol]
    if pool_across_symbols:
        searched.extend(sorted(s for s in frames if s != target_symbol))
    result.pooled_symbols = searched

    all_matches: list[AnalogueMatch] = []
    baseline_returns: list[float] = []
    candidates = 0

    for symbol in searched:
        frame = frames[symbol]
        if frame.empty or len(frame) <= horizon:
            continue

        # Forward outcomes, computed here and used only to describe completed history.
        # The last `horizon` bars have no finished window and drop out on the notna filter.
        returns = forward_return(frame["close"], horizon)
        adverse = forward_max_adverse_excursion(frame["close"], frame["low"], horizon)
        favourable = forward_max_favorable_excursion(frame["close"], frame["high"], horizon)

        # Standardise once, and keep the (mean, sd) used so the setup can be expressed in
        # the same units. "Similar" then means similar relative to how variable the feature
        # actually is for this instrument -- comparing raw RSI across a placid utility and a
        # volatile semiconductor would call very different situations alike.
        standardised, frame_stats = _standardise(frame, tuple(usable))
        if standardised.empty:
            continue

        names = [f.name for f in usable if f.name in standardised.columns]
        if not names:
            continue

        weights = np.array([f.weight for f in usable if f.name in names], dtype=float)
        matrix = standardised[names].to_numpy(dtype=float)
        point = np.array(
            [
                (setup[name] - frame_stats[name][0]) / frame_stats[name][1]
                for name in names
            ],
            dtype=float,
        )

        # Weighted Euclidean distance, normalised by total weight so `max_distance` means
        # the same thing however many dimensions are in play.
        deltas = matrix - point
        distances = np.sqrt(np.nansum(weights * deltas**2, axis=1) / weights.sum())

        valid = (
            np.isfinite(distances)
            & returns.notna().to_numpy()
            & adverse.notna().to_numpy()
            & favourable.notna().to_numpy()
        )
        # Exclude the target's own current bar and anything after it: a "historical"
        # analogue drawn from the future of the bar being described would be circular.
        if symbol == target_symbol:
            positional = np.arange(len(frame))
            valid &= positional < position

        candidates += int(valid.sum())
        # The unconditional sample: every bar with a completed forward window, matched or
        # not. Collected here so the conditional result can be compared against the base
        # rate over exactly the same bars rather than against a differently-scoped figure.
        baseline_returns.extend(returns.to_numpy()[valid].tolist())

        within = valid & (distances <= max_distance)

        for i in np.flatnonzero(within):
            all_matches.append(
                AnalogueMatch(
                    symbol=symbol,
                    timestamp=frame.index[i].to_pydatetime(),
                    distance=float(distances[i]),
                    features={n: float(frame.iloc[i][n]) for n in names},
                    forward_return_pct=float(returns.iloc[i]),
                    max_adverse_excursion_pct=float(adverse.iloc[i]),
                    max_favorable_excursion_pct=float(favourable.iloc[i]),
                )
            )

    result.n_candidates = candidates
    result.n_matches = len(all_matches)

    if result.n_matches < MIN_RAW_MATCHES:
        result.insufficiency_reason = (
            f"only {result.n_matches} historical bars resembled this setup "
            f"(minimum {MIN_RAW_MATCHES}) out of {candidates:,} examined. Loosening "
            "max_distance would find more, at the cost of them being less similar."
        )
        result.matches = sorted(all_matches, key=lambda m: m.distance)
        return result

    independent = _thin_overlapping(all_matches, horizon)
    result.n_independent = len(independent)
    result.matches = independent

    if result.n_independent < MIN_INDEPENDENT_OBSERVATIONS:
        result.insufficiency_reason = (
            f"{result.n_matches} bars matched, but only {result.n_independent} of them are "
            f"independent (minimum {MIN_INDEPENDENT_OBSERVATIONS}). The rest overlap: "
            f"observations closer together than {horizon} bars share part of the same "
            "forward window, and instruments on the same date move together, so counting "
            "them separately would overstate the evidence."
        )
        return result

    result.sufficient = True
    result.statistics = _summarise(independent, horizon)
    result.baseline = _baseline(baseline_returns, result.n_matches, candidates)
    result.adds_information = _differs_from_baseline(result.statistics, result.baseline)
    result.caveats = _caveats(result, searched, horizon)
    return result


def _baseline(returns: list[float], n_matches: int, n_candidates: int) -> dict[str, Any]:
    """The unconditional distribution over the same bars.

    Overlap is not corrected here, deliberately: this is a reference point for comparison,
    not a result to be trusted on its own, and thinning it would make the comparison
    against the thinned conditional sample harder to reason about rather than easier.
    """
    if len(returns) < 2:
        return {"available": False, "reason": "no unconditional sample"}

    array = np.array(returns, dtype=float)
    return {
        "available": True,
        "n_bars": int(array.size),
        "match_share_pct": round(100.0 * n_matches / n_candidates, 2) if n_candidates else None,
        "median_return_pct": round(float(np.median(array)), 4),
        "p10_return_pct": round(float(np.percentile(array, 10)), 4),
        "p90_return_pct": round(float(np.percentile(array, 90)), 4),
        "positive_share_pct": round(float((array > 0).mean() * 100.0), 2),
        "note": (
            "Every bar with a completed forward window, matched or not. Not overlap-"
            "corrected: it is a reference point for the conditional result, not a finding."
        ),
    }


def _differs_from_baseline(
    statistics: dict[str, Any], baseline: dict[str, Any]
) -> bool:
    """Whether the conditional distribution says anything the base rate does not.

    Checks the median and the 10th percentile. The downside tail matters as much as the
    centre: a setup whose median matches the base rate but whose losses are materially
    deeper has told you something real.
    """
    if not baseline.get("available"):
        return False

    median_gap = abs(statistics["median_return_pct"] - baseline["median_return_pct"])
    tail_gap = abs(statistics["p10_return_pct"] - baseline["p10_return_pct"])
    return max(median_gap, tail_gap) >= BASELINE_DIVERGENCE_THRESHOLD


def _summarise(matches: list[AnalogueMatch], horizon: int) -> dict[str, Any]:
    """Distribution of what followed. Percentiles, not a point estimate.

    A single number invites being read as a prediction. A distribution invites being read
    as a range of things that happened, which is what it is.
    """
    returns = np.array([m.forward_return_pct for m in matches], dtype=float)
    adverse = np.array([m.max_adverse_excursion_pct for m in matches], dtype=float)
    favourable = np.array([m.max_favorable_excursion_pct for m in matches], dtype=float)

    return {
        "horizon_bars": horizon,
        "n_observations": int(returns.size),
        "mean_return_pct": round(float(returns.mean()), 4),
        "median_return_pct": round(float(np.median(returns)), 4),
        "p10_return_pct": round(float(np.percentile(returns, 10)), 4),
        "p25_return_pct": round(float(np.percentile(returns, 25)), 4),
        "p75_return_pct": round(float(np.percentile(returns, 75)), 4),
        "p90_return_pct": round(float(np.percentile(returns, 90)), 4),
        "worst_return_pct": round(float(returns.min()), 4),
        "best_return_pct": round(float(returns.max()), 4),
        "std_return_pct": round(float(returns.std(ddof=1)), 4) if returns.size > 1 else None,
        "positive_share_pct": round(float((returns > 0).mean() * 100.0), 2),
        # Excursions matter more than the close-to-close return for anything with a stop:
        # a median of +3% behind a routine -12% excursion gets stopped out long before the
        # median arrives.
        "median_adverse_excursion_pct": round(float(np.median(adverse)), 4),
        "worst_adverse_excursion_pct": round(float(adverse.min()), 4),
        "median_favorable_excursion_pct": round(float(np.median(favourable)), 4),
        "best_favorable_excursion_pct": round(float(favourable.max()), 4),
        "first_observation": min(m.timestamp for m in matches).isoformat(),
        "last_observation": max(m.timestamp for m in matches).isoformat(),
    }


def _caveats(result: AnalogueResult, searched: list[str], horizon: int) -> list[str]:
    """Everything that limits how far these numbers can be taken."""
    caveats = list(result.caveats)
    statistics = result.statistics

    caveats.append(
        f"{result.n_matches} bars matched but only {result.n_independent} are counted: the "
        f"rest overlapped. Observations within {horizon} bars of each other share part of "
        "the same forward window, and instruments on the same date move together."
    )

    if len(searched) > 1:
        caveats.append(
            f"Observations are pooled across {len(searched)} instruments in the same "
            "market. That assumes they are comparable, which is an assumption rather than "
            "a finding."
        )

    if result.n_independent < 50:
        caveats.append(
            f"{result.n_independent} independent observations is a thin sample. The 10th "
            "and 90th percentiles in particular rest on a handful of points each and would "
            "move substantially with a few more."
        )

    first = statistics.get("first_observation", "")
    last = statistics.get("last_observation", "")
    if first and last:
        caveats.append(
            f"All observations come from {first[:10]} to {last[:10]}, one historical path. "
            "Conditions outside that span are not represented, and nothing here says this "
            "period was typical."
        )

    caveats.append(
        "The universe contains only instruments that still exist, so setups that preceded "
        "a delisting or bankruptcy are absent. The distribution is optimistic by an unknown "
        "amount."
    )

    baseline = result.baseline
    if baseline.get("available"):
        share = baseline.get("match_share_pct")
        if not result.adds_information:
            caveats.insert(
                0,
                "This distribution barely differs from the unconditional base rate "
                f"(median {statistics['median_return_pct']:+.2f}% against "
                f"{baseline['median_return_pct']:+.2f}%, 10th percentile "
                f"{statistics['p10_return_pct']:+.2f}% against "
                f"{baseline['p10_return_pct']:+.2f}%). The matching has not isolated "
                "anything: these figures describe how these instruments behaved generally "
                "over this period, not what this particular setup preceded. Tighten "
                "max_distance to make the search selective.",
            )
        if share is not None and share > 20:
            caveats.insert(
                0,
                f"The search matched {share:.0f}% of all candidate bars, which is too many "
                "to be called similar. A threshold this loose turns an analogue study into "
                "a restatement of the base rate.",
            )

    adverse = statistics.get("median_adverse_excursion_pct")
    median = statistics.get("median_return_pct")
    if adverse is not None and median is not None and abs(adverse) > abs(median) * 1.5:
        caveats.append(
            f"The median adverse excursion ({adverse:.1f}%) is larger than the median "
            f"return ({median:+.1f}%). Typical outcomes went meaningfully against the "
            "position before arriving, so a tight stop would have exited many of these "
            "before the median was reached."
        )

    return caveats
