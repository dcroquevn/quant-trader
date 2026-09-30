"""Parameter search, walk-forward analysis and robustness testing.

The guarantee that makes any of this worth reading: the search reads **TRAIN only**,
selection happens on **VALIDATION**, and **TEST** stays sealed until a result is
finalised. See :mod:`app.optimization.search` for where that is enforced and why it is
enforced more than once.
"""

from app.optimization.objective import ObjectiveScore, ObjectiveWeights, PRESETS
from app.optimization.robustness import RobustnessReport, run_robustness_suite
from app.optimization.search import SearchResult, Trial, run_search, select_on_validation
from app.optimization.space import ParameterAxis, ParameterSpace
from app.optimization.walkforward import WalkForwardResult, run_walk_forward

__all__ = [
    "ObjectiveWeights",
    "ObjectiveScore",
    "PRESETS",
    "ParameterAxis",
    "ParameterSpace",
    "Trial",
    "SearchResult",
    "run_search",
    "select_on_validation",
    "WalkForwardResult",
    "run_walk_forward",
    "RobustnessReport",
    "run_robustness_suite",
]
