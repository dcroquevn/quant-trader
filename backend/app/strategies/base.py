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

__all__ = [
    "Action",
    "Decision",
    "StrategyParams",
    "Strategy",
    "ComponentScore",
    "BarRow",
    "PreparedFrame",
]


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


def _is_missing(value: Any) -> bool:
    """True for None or NaN. Cheaper than pd.isna on a scalar in a hot loop."""
    if value is None:
        return True
    return isinstance(value, float) and value != value


class BarRow(dict):
    """One bar's values as a plain dict, with the timestamp on ``.name``.

    Stands in for a ``pandas.Series`` in the hot path. A Series lookup goes through label
    indexing; a dict lookup is a hash. With 600,000 lookups in a single backtest that
    difference dominated the whole run. ``.name`` mirrors the Series attribute so a
    strategy reading ``row.name`` works either way.
    """

    __slots__ = ("name",)

    def __init__(self, values: dict[str, Any], name: Any = None) -> None:
        super().__init__(values)
        self.name = name


@dataclass(slots=True)
class PreparedFrame:
    """A frame validated once and unpacked into per-bar dicts.

    Built by :meth:`Strategy.prepare`. Holds:

    ``rows``
        One :class:`BarRow` per bar, carrying the bar columns plus the strategy's required
        features. Built with a single vectorised pass rather than a Series per access.

    ``ready``
        Whether every required feature is non-null on that bar. Computed as one vectorised
        mask, replacing a per-bar label reindex that was the single largest cost in the
        engine.
    """

    index: pd.DatetimeIndex
    rows: list["BarRow"]
    ready: list[bool]
    position_of: dict[Any, int]

    def __len__(self) -> int:
        return len(self.rows)


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
    def _decide(self, row: "BarRow | pd.Series", in_position: bool) -> Decision:
        """Decide for one bar.

        ``row`` is a mapping of that bar's values, keyed by column name, with the timestamp
        on ``.name``. It is a :class:`BarRow` on the engine's path and a ``pandas.Series``
        when a caller uses :meth:`evaluate` directly; both support ``row[key]``.
        """

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

    def prepare(self, frame: pd.DataFrame) -> PreparedFrame:
        """Validate ``frame`` once and unpack it for repeated evaluation.

        Everything the per-bar path used to redo — the leakage check, the missing-feature
        check, and the "are the indicators warm" test — happens here, once, vectorised.
        The engine calls this per symbol at the start of a run and then
        :meth:`evaluate_prepared` per bar.
        """
        if frame.empty:
            raise ValueError(f"{self.name} cannot evaluate an empty frame")
        self._validate(frame)

        needed = list(self.required_features)
        # One vectorised pass instead of a label reindex per bar.
        ready_mask = frame[needed].notna().all(axis=1)

        # Carry the bar columns as well: the engine and the exit logic read open/high/low.
        columns = list(dict.fromkeys([*needed, "open", "high", "low", "close", "volume", "atr_14"]))
        present = [c for c in columns if c in frame.columns]
        records = frame[present].to_dict("records")

        stamps = list(frame.index)
        return PreparedFrame(
            index=frame.index,
            rows=[BarRow(record, name=stamp) for record, stamp in zip(records, stamps)],
            ready=[bool(v) for v in ready_mask.to_numpy()],
            position_of={stamp: i for i, stamp in enumerate(stamps)},
        )

    def evaluate_prepared(
        self, prepared: PreparedFrame, index: int, *, in_position: bool = False
    ) -> Decision:
        """Decide for one bar of an already-prepared frame.

        The fast path. Identical in result to :meth:`evaluate`, which
        :mod:`tests.test_strategies` asserts directly — a fast path that quietly disagreed
        with the slow one would be worse than a slow backtest.
        """
        if not prepared.ready[index]:
            row = prepared.rows[index]
            unready = [c for c in self.required_features if _is_missing(row.get(c))]
            return Decision(
                action=Action.HOLD,
                score=0.0,
                reasons=(
                    f"Indicators not warmed up: {', '.join(unready[:5])}"
                    f"{' ...' if len(unready) > 5 else ''}",
                ),
            )
        return self._decide(prepared.rows[index], in_position)

    def evaluate(
        self, frame: pd.DataFrame, *, in_position: bool = False, index: int = -1
    ) -> Decision:
        """Decide for the bar at ``index`` (default: the most recent).

        Convenience wrapper over :meth:`prepare` and :meth:`evaluate_prepared`, for a
        one-off decision. The engine prepares once and loops instead.
        """
        prepared = self.prepare(frame)
        position = index if index >= 0 else len(prepared) + index
        return self.evaluate_prepared(prepared, position, in_position=in_position)

    def evaluate_series(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Decide for every bar, vectorised where the subclass allows it.

        Returns a frame indexed like ``frame`` with ``action``, ``score`` and
        ``reasons`` columns. Used by the scanner and for charting historical signals.
        Note this evaluates each bar *as if flat*; a real position's exit logic
        depends on entry price, which only the backtester knows.
        """
        prepared = self.prepare(frame)
        decisions = [
            self.evaluate_prepared(prepared, i, in_position=False) for i in range(len(prepared))
        ]
        return pd.DataFrame(
            {
                "action": [d.action.value for d in decisions],
                "score": [d.score for d in decisions],
                "reasons": ["; ".join(d.reasons) for d in decisions],
            },
            index=frame.index,
        )

    def propose_levels(self, frame: pd.DataFrame) -> "tuple[float, float] | None":
        """Stop and target for a position opened now, independent of the current signal.

        Returns ``(stop, target)`` or None when the strategy does not define levels, or the data
        does not support them.

        Separate from :meth:`evaluate` because the two answer different questions. Whether to
        enter is a signal and can change between the close that produced it and the next
        session, when the trade is actually made. Where the risk ends is a volatility
        measurement and is defined regardless -- so a position recorded the day after a signal
        still gets a stop, rather than being stored unprotected because the reading moved
        overnight.

        The default returns None: a strategy that proposes no levels says so rather than having
        some invented for it.
        """
        return None

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
