"""Parameter spaces and sampling.

A search is only as honest as the space it searches. Two failure modes this module is
built against:

**A space so large it guarantees a lucky winner.** With 10,000 combinations and 300
trades, the best result is mostly noise: you have run 10,000 experiments and reported
the maximum. :func:`ParameterSpace.grid_size` is checked against
:data:`OVERFITTING_RISK_COMBINATIONS` and the run carries a warning when it is exceeded,
because that ratio — combinations tried against observations available — is the single
best predictor of a backtest that will not survive contact with new data.

**A space that only contains good answers.** A grid centred tightly on values someone
already knows work is not a search, it is a confirmation. Nothing here can detect that
automatically, so :meth:`ParameterSpace.describe` prints the full extent of every axis
for a human to look at.

Sampling is deterministic. Random search takes a seed and records it, so a result can be
reproduced exactly — an irreproducible optimisation result is an anecdote.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Iterator

__all__ = [
    "ParameterAxis",
    "ParameterSpace",
    "OVERFITTING_RISK_COMBINATIONS",
    "DEFAULT_TREND_MOMENTUM_SPACE",
]

OVERFITTING_RISK_COMBINATIONS = 500
"""Grid size above which a search is flagged as overfitting-prone.

Not a hard limit — a large space is sometimes the right thing — but past this point the
best-of-N result owes more to the number of attempts than to any parameter. The run
records the warning so a report cannot omit it.
"""


@dataclass(frozen=True, slots=True)
class ParameterAxis:
    """One parameter and the values a search may try for it."""

    name: str
    values: tuple[Any, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError(f"Axis {self.name!r} has no values to search")
        if len(set(map(repr, self.values))) != len(self.values):
            raise ValueError(f"Axis {self.name!r} contains duplicate values: {self.values}")

    @classmethod
    def numeric(
        cls, name: str, start: float, stop: float, step: float, *, integer: bool = False
    ) -> "ParameterAxis":
        """Inclusive numeric range.

        Values are rounded to a sane precision before de-duplication, so a float step
        does not produce ``0.30000000000000004`` as a distinct point from ``0.3``.
        """
        if step <= 0:
            raise ValueError(f"Axis {name!r} step must be positive, got {step}")
        if stop < start:
            raise ValueError(f"Axis {name!r} stop ({stop}) is below start ({start})")

        count = int(math.floor((stop - start) / step + 1e-9)) + 1
        raw = [start + i * step for i in range(count)]
        values = tuple(dict.fromkeys(int(round(v)) if integer else round(v, 6) for v in raw))
        return cls(name=name, values=values)

    @property
    def span(self) -> str:
        if len(self.values) == 1:
            return f"{self.values[0]}"
        return f"{self.values[0]} .. {self.values[-1]} ({len(self.values)} values)"


@dataclass(frozen=True, slots=True)
class ParameterSpace:
    """A set of axes to search over."""

    axes: tuple[ParameterAxis, ...]
    fixed: dict[str, Any] = field(default_factory=dict)
    """Parameters held constant. Recorded so a result states the whole configuration."""

    def __post_init__(self) -> None:
        if not self.axes:
            raise ValueError("A parameter space needs at least one axis")
        names = [a.name for a in self.axes]
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate axis names: {names}")
        overlap = set(names) & set(self.fixed)
        if overlap:
            raise ValueError(
                f"Parameters {sorted(overlap)} are both searched and fixed. One or the "
                "other — a fixed value would silently override every point the search "
                "tried."
            )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.axes)

    def grid_size(self) -> int:
        """Number of combinations a full grid would evaluate."""
        total = 1
        for axis in self.axes:
            total *= len(axis.values)
        return total

    @property
    def is_overfitting_prone(self) -> bool:
        return self.grid_size() > OVERFITTING_RISK_COMBINATIONS

    def grid(self) -> Iterator[dict[str, Any]]:
        """Every combination, in a deterministic order."""
        for combination in product(*(a.values for a in self.axes)):
            yield {**self.fixed, **dict(zip(self.names, combination))}

    def sample(self, n: int, *, seed: int = 0) -> list[dict[str, Any]]:
        """``n`` distinct random combinations, reproducibly.

        Draws without replacement: a random search that evaluates the same point twice
        has wasted a trial and inflates the apparent breadth of the search. When ``n``
        meets or exceeds the grid size the full grid is returned instead, because
        sampling 400 points from a space of 300 is just a slow grid search.
        """
        if n < 1:
            raise ValueError(f"Sample size must be at least 1, got {n}")

        total = self.grid_size()
        if n >= total:
            return list(self.grid())

        rng = random.Random(seed)
        seen: set[tuple[Any, ...]] = set()
        out: list[dict[str, Any]] = []
        # Bounded so a pathological space cannot spin forever; the cap is generous
        # relative to the birthday-collision rate for n well below total.
        attempts = 0
        limit = max(1000, n * 50)
        while len(out) < n and attempts < limit:
            attempts += 1
            point = tuple(rng.choice(a.values) for a in self.axes)
            if point in seen:
                continue
            seen.add(point)
            out.append({**self.fixed, **dict(zip(self.names, point))})
        return out

    def neighbours(self, point: dict[str, Any], *, steps: int = 1) -> list[dict[str, Any]]:
        """Points one step away on each axis, for sensitivity analysis.

        This is what turns "these parameters scored well" into "these parameters scored
        well and so did their neighbours". A peak with a cliff on every side is a fitted
        artefact; a peak on a plateau might be real.
        """
        out: list[dict[str, Any]] = []
        for axis in self.axes:
            current = point.get(axis.name)
            if current not in axis.values:
                continue
            index = axis.values.index(current)
            for offset in range(-steps, steps + 1):
                if offset == 0:
                    continue
                neighbour_index = index + offset
                if 0 <= neighbour_index < len(axis.values):
                    variant = dict(point)
                    variant[axis.name] = axis.values[neighbour_index]
                    out.append(variant)
        return out

    def describe(self) -> dict[str, Any]:
        """Serialisable description, persisted with every run."""
        return {
            "axes": [{"name": a.name, "values": list(a.values), "span": a.span} for a in self.axes],
            "fixed": dict(self.fixed),
            "grid_size": self.grid_size(),
            "overfitting_prone": self.is_overfitting_prone,
            "overfitting_threshold": OVERFITTING_RISK_COMBINATIONS,
        }


# --------------------------------------------------------------------------- #
# Default space
# --------------------------------------------------------------------------- #

DEFAULT_TREND_MOMENTUM_SPACE = ParameterSpace(
    axes=(
        # Momentum band. The width matters more than either edge: a narrow band trades
        # rarely, a wide one stops filtering anything.
        ParameterAxis("rsi_min", (30.0, 35.0, 40.0, 45.0)),
        ParameterAxis("rsi_max", (65.0, 70.0, 75.0)),
        # Volume confirmation. 1.0 means "no better than average", which is effectively
        # off, and is included so the search can tell us whether the filter earns its place.
        ParameterAxis("min_relative_volume", (1.0, 1.2, 1.5, 2.0)),
        # Stop distance in ATR units. The dominant driver of both position size and
        # win rate, so the range is deliberately wide.
        ParameterAxis("stop_atr_multiple", (1.5, 2.0, 2.5, 3.0)),
        # Reward target as a multiple of the stop.
        ParameterAxis("take_profit_r_multiple", (2.0, 3.0, 4.0)),
        # Time stop.
        ParameterAxis("max_holding_bars", (20, 40, 60)),
    ),
)
"""A starting space for ``trend_momentum``: 4x3x4x4x3x3 = 1,728 combinations.

Deliberately above :data:`OVERFITTING_RISK_COMBINATIONS`, so the default run demonstrates
the warning rather than hiding the problem behind a conveniently small grid. Use random
search over it, or narrow the axes.
"""
