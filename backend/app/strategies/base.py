"""Strategy abstraction.

A strategy turns a feature frame into a decision per bar. It does nothing else: no
position sizing, no order routing, no knowledge of cash. That separation is what lets
the same strategy object be driven by the backtester, the scanner and (later) the
paper broker without behaving differently in each.

Two invariants, both enforced rather than documented
----------------------------------------------------
**No look-ahead.** ``Strategy.evaluate`` is handed a frame and must only read rows at
or before the bar being decided. The base class calls
:func:`app.indicators.forward.assert_no_forward_columns` on every input, so a frame
carrying outcome labels raises ``DataLeakageError`` instead of producing a
spectacular backtest.

**A score is not a probability.** :class:`Decision.score` is an ordinal ranking
number on an arbitrary scale, used to sort candidates against each other. It is not
a win probability, an expected return, or a confidence level, and
:meth:`Decision.describe_score` exists so any surface that displays it has a correct
phrasing available. The projection engine (Phase 5) is where defensible statistics
about outcomes will live, based on historical analogues.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, ClassVar

import pandas as pd

from app.indicators.forward import assert_no_forward_columns

__all__ = ["Action", "Decision", "StrategyParams", "Strategy", "ComponentScore"]


class Action(str, Enum):
    """What the strategy wants to do about a position."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class ComponentScore:
    """One component's contribution to a decision.

    Kept separately rather than collapsed into a single number so a signal can be
    explained after the fact: "which part of this fired?" is the first question asked
    of any trade that went wrong.
    """

    name: str
    passed: bool
    value: float | None = None
    """The underlying measurement, when there is one (RSI level, relative volume...)."""

    detail: str = ""
    """Human-readable reason, e.g. ``"RSI 31.2 inside band (35, 70)"``."""


@dataclass(frozen=True, slots=True)
class Decision:
    """A strategy's verdict on one bar."""

    action: Action
    score: float
    """Ordinal ranking number in ``[0, 1]``. **Not** a probability — see the module docstring."""

    components: tuple[ComponentScore, ...] = ()
    reasons: tuple[str, ...] = ()
    features: dict[str, Any] = field(default_factory=dict)

    stop_price: float | None = None
    """Suggested initial stop. The backtester decides whether to honour it."""

    take_profit_price: float | None = None

    @property
    def is_actionable(self) -> bool:
        return self.action is not Action.HOLD

    def describe_score(self) -> str:
        """Phrasing any UI can use safely.

        Exists so no surface has to invent its own wording and accidentally imply a
        probability.
        """
        passed = sum(1 for component in self.components if component.passed)
        total = len(self.components)
        if total == 0:
            return f"Score {self.score:.2f} (no components evaluated)"
        return (
            f"Score {self.score:.2f}: {passed} of {total} conditions currently hold. "
            "This counts conditions, it is not a probability of profit."
        )


@dataclass(frozen=True, slots=True)
class StrategyParams:
    """Base class for a strategy's parameter set.

    Frozen so a parameter set cannot be mutated mid-backtest, which would make a
    result irreproducible. :meth:`replace` returns a modified copy, which is what the
    optimiser will use in Phase 4.
    """

    def replace(self, **changes: Any) -> "StrategyParams":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form, persisted with every backtest and signal."""
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__  # type: ignore[attr-defined]
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategyParams":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ValueError(
                f"{cls.__name__} received unknown parameters {sorted(unknown)}. "
                f"Known parameters: {sorted(known)}"
            )
        return cls(**data)


class Strategy(abc.ABC):
    """Base class for every strategy.

    Subclasses implement :meth:`_decide` for a single bar and declare
    :attr:`required_features`. The public :meth:`evaluate` and :meth:`evaluate_series`
    handle leakage checks and warm-up masking, so no subclass can forget them.
    """

    name: ClassVar[str] = "unnamed"
    version: ClassVar[str] = "1"
    description: ClassVar[str] = ""

    def __init__(self, params: StrategyParams | None = None) -> None:
        self.params = params or self.default_params()

    # ------------------------------------------------------------------ #
    # Subclass contract
    # ------------------------------------------------------------------ #

    @classmethod
    @abc.abstractmethod
    def default_params(cls) -> StrategyParams: ...

    @property
    @abc.abstractmethod
    def required_features(self) -> tuple[str, ...]:
        """Feature columns this strategy reads.

        Declared so :meth:`evaluate` can refuse a frame that is missing one, rather
        than silently comparing a price against ``NaN`` — which evaluates to False and
        produces a HOLD indistinguishable from a considered decision.
        """

    @abc.abstractmethod
    def _decide(self, row: pd.Series, in_position: bool) -> Decision:
        """Decide for one bar. ``row`` holds that bar's features only."""

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def _validate(self, frame: pd.DataFrame) -> None:
        assert_no_forward_columns(frame, context=f"{self.name} strategy input")
        missing = [c for c in self.required_features if c not in frame.columns]
        if missing:
            raise ValueError(
                f"{self.name} requires feature columns {missing}, which are absent. "
                "Run app.indicators.registry.compute_features() on the bars first."
            )

    def evaluate(
        self, frame: pd.DataFrame, *, in_position: bool = False, index: int = -1
    ) -> Decision:
        """Decide for the bar at ``index`` (default: the most recent).

        Only the row at ``index`` is passed to :meth:`_decide`, which makes reaching
        forward structurally awkward rather than merely discouraged.
        """
        if frame.empty:
            # Checked before column validation: an empty frame has no columns at all, so
            # the missing-feature message would list every requirement and bury the
            # actual problem.
            raise ValueError(f"{self.name} cannot evaluate an empty frame")
        self._validate(frame)

        row = frame.iloc[index]
        if row[list(self.required_features)].isna().any():
            unready = [c for c in self.required_features if pd.isna(row[c])]
            return Decision(
                action=Action.HOLD,
                score=0.0,
                reasons=(
                    f"Indicators not warmed up: {', '.join(unready[:5])}"
                    f"{' ...' if len(unready) > 5 else ''}",
                ),
            )
        return self._decide(row, in_position)

    def evaluate_series(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Decide for every bar, vectorised where the subclass allows it.

        Returns a frame indexed like ``frame`` with ``action``, ``score`` and
        ``reasons`` columns. Used by the scanner and for charting historical signals.
        Note this evaluates each bar *as if flat*; a real position's exit logic
        depends on entry price, which only the backtester knows.
        """
        self._validate(frame)
        decisions = [
            self.evaluate(frame, in_position=False, index=i) for i in range(len(frame))
        ]
        return pd.DataFrame(
            {
                "action": [d.action.value for d in decisions],
                "score": [d.score for d in decisions],
                "reasons": ["; ".join(d.reasons) for d in decisions],
            },
            index=frame.index,
        )

    def describe(self) -> dict[str, Any]:
        """Serialisable identity, persisted with backtests and signals."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "params": self.params.to_dict(),
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} v{self.version}>"
