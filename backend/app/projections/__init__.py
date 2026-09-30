"""Historical analogues and statistical scenarios.

Answers "when conditions looked like this before, what happened next?" and never "what
will happen next". The distinction is enforced in the data structures: every result carries
its sample size, its caveats, and an explicit ``sufficient`` flag, and an insufficient
sample yields the phrase "Insufficient historical evidence" rather than a median computed
from too few points.

The hard part is not the search, it is the overlap: consecutive bars share most of their
forward window, so raw match counts overstate the evidence by roughly the horizon length.
See :mod:`app.projections.analogues` for how that is handled.
"""

from app.projections.analogues import (
    DEFAULT_MATCH_FEATURES,
    MIN_INDEPENDENT_OBSERVATIONS,
    AnalogueMatch,
    AnalogueResult,
    MatchFeature,
    find_analogues,
)
from app.projections.runner import DEFAULT_HORIZONS, project_market, project_symbol
from app.projections.scenarios import (
    INSUFFICIENT_EVIDENCE,
    Scenario,
    ScenarioSet,
    build_scenarios,
    format_scenarios,
    scenarios_as_evidence,
)

__all__ = [
    "MatchFeature",
    "DEFAULT_MATCH_FEATURES",
    "AnalogueMatch",
    "AnalogueResult",
    "find_analogues",
    "MIN_INDEPENDENT_OBSERVATIONS",
    "Scenario",
    "ScenarioSet",
    "build_scenarios",
    "format_scenarios",
    "scenarios_as_evidence",
    "INSUFFICIENT_EVIDENCE",
    "project_symbol",
    "project_market",
    "DEFAULT_HORIZONS",
]
