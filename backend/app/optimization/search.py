"""Parameter search.

The search reads **TRAIN only**. That is enforced in three places, deliberately
redundantly, because a single guard is a single point of failure for the one property
that makes an optimisation result worth anything:

1. :func:`run_search` hard-codes ``split="train"`` and does not accept a split argument.
2. It calls ``resolve_window("train")``, which is the same guard the CLI uses.
3. The ``optimization_runs`` table has a ``CHECK (split = 'train')`` constraint, so a
   persisted run that claims otherwise cannot exist.

Selection happens on VALIDATION, in a separate step (:func:`select_on_validation`), and
it re-runs only the shortlist. That split matters: if the search itself scored candidates
on validation, validation would become training data and there would be no held-out
estimate left at all.

The fragility pass
------------------
After scoring, the best handful of candidates have their *neighbours* evaluated too, and
the fragility penalty is applied. This is the expensive part and it is the point of the
whole module: without it a search reports the highest peak it found, and the highest peak
in a noisy landscape is noise. See :mod:`app.optimization.objective`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

import pandas as pd
from sqlalchemy.orm import Session

from app.backtesting.runner import prepare_frames, resolve_window, run_backtest
from app.core.exceptions import DataLeakageError
from app.core.logging import get_logger
from app.optimization.objective import (
    ObjectiveScore,
    ObjectiveWeights,
    apply_fragility_penalty,
    score_result,
)
from app.optimization.space import ParameterSpace
from app.strategies.registry import build_strategy

logger = get_logger(__name__)

__all__ = ["Trial", "SearchResult", "run_search", "select_on_validation"]


@dataclass(slots=True)
class Trial:
    """One evaluated parameter combination."""

    params: dict[str, Any]
    metrics: dict[str, Any]
    score: ObjectiveScore
    seconds: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        keep = (
            "total_return_pct", "cagr_pct", "sharpe", "sortino", "calmar",
            "max_drawdown_pct", "annualised_volatility_pct", "n_trades",
            "win_rate_pct", "profit_factor", "expectancy", "turnover_pct",
            "exposure_pct", "average_holding_days",
        )
        return {
            "params": dict(self.params),
            "metrics": {k: self.metrics.get(k) for k in keep},
            "objective": self.score.to_dict(),
            "seconds": round(self.seconds, 3),
            "error": self.error,
        }


@dataclass(slots=True)
class SearchResult:
    """Everything a search produced."""

    market: str
    strategy: str
    method: str
    split: str
    space: dict[str, Any]
    weights: dict[str, Any]
    trials: list[Trial] = field(default_factory=list)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    warnings: list[str] = field(default_factory=list)
    seed: int | None = None

    @property
    def successful(self) -> list[Trial]:
        return [t for t in self.trials if t.ok]

    @property
    def failed(self) -> list[Trial]:
        return [t for t in self.trials if not t.ok]

    def ranked(self, limit: int | None = None) -> list[Trial]:
        """Trials best-first by objective score.

        Trials that produced no trades are ranked last regardless of score. A
        configuration that never traded has a defined objective value but has measured
        nothing, and letting it place highly would be the most misleading possible
        output.
        """
        usable = [t for t in self.successful if int(t.metrics.get("n_trades") or 0) > 0]
        empty = [t for t in self.successful if int(t.metrics.get("n_trades") or 0) == 0]
        ordered = sorted(usable, key=lambda t: t.score.value, reverse=True) + empty
        return ordered[:limit] if limit else ordered

    @property
    def best(self) -> Trial | None:
        ranked = self.ranked(1)
        return ranked[0] if ranked else None

    def parameter_stability(self) -> dict[str, Any]:
        """How much each parameter varies across the top decile of trials.

        A parameter whose best value is the same across the whole top decile is doing
        real work. One that takes every possible value among equally good results is
        either irrelevant or being fitted to noise — and either way the search has not
        learned anything about it.
        """
        ranked = self.ranked()
        if len(ranked) < 4:
            return {"available": False, "reason": "too few successful trials"}

        top_n = max(3, len(ranked) // 10)
        top = ranked[:top_n]
        out: dict[str, Any] = {"available": True, "sample": top_n, "parameters": {}}

        names = sorted({k for t in top for k in t.params})
        for name in names:
            values = [t.params.get(name) for t in top]
            distinct = sorted({repr(v) for v in values})
            numeric = [v for v in values if isinstance(v, (int, float))]
            entry: dict[str, Any] = {
                "distinct_values": len(distinct),
                "modal_value": max(set(map(repr, values)), key=list(map(repr, values)).count),
                "concentration": round(
                    max(list(map(repr, values)).count(v) for v in distinct) / len(values), 4
                ),
            }
            if numeric:
                entry["min"] = min(numeric)
                entry["max"] = max(numeric)
                entry["mean"] = round(sum(numeric) / len(numeric), 4)
            out["parameters"][name] = entry

        scattered = [
            name
            for name, entry in out["parameters"].items()
            if entry["concentration"] < 0.4
        ]
        if scattered:
            out["note"] = (
                f"{', '.join(scattered)} took widely different values among the best "
                "trials. Either they do not matter, or the search is fitting noise in "
                "them — in both cases the chosen value carries little information."
            )
        return out

    def to_dict(self, *, trial_limit: int = 200) -> dict[str, Any]:
        return {
            "market": self.market,
            "strategy": self.strategy,
            "method": self.method,
            "split": self.split,
            "space": self.space,
            "weights": self.weights,
            "seed": self.seed,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "n_trials": len(self.trials),
            "n_failed": len(self.failed),
            "warnings": self.warnings,
            "best": self.best.to_dict() if self.best else None,
            "trials": [t.to_dict() for t in self.ranked(trial_limit)],
            "parameter_stability": self.parameter_stability(),
        }


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def _evaluate(
    session: Session,
    strategy_name: str,
    market: str,
    params: dict[str, Any],
    window_start: date,
    window_end: date,
    weights: ObjectiveWeights,
    symbols: list[str] | None,
    capital: float | None,
    frames: dict[str, pd.DataFrame] | None = None,
) -> Trial:
    """Backtest one parameter set on TRAIN and score it.

    ``frames`` carries the shared feature set; see :func:`prepare_frames`.
    """
    began = time.monotonic()
    try:
        strategy = build_strategy(strategy_name, params)
    except ValueError as exc:
        # An invalid combination (rsi_min above rsi_max, say) is an expected outcome of
        # searching a product space, not a failure of the search.
        return Trial(
            params=params,
            metrics={},
            score=score_result({}, weights),
            seconds=time.monotonic() - began,
            error=f"invalid parameters: {exc}",
        )

    try:
        result = run_backtest(
            session,
            strategy,
            market,
            split="train",
            start=window_start,
            end=window_end,
            symbols=symbols,
            initial_capital=capital,
            include_benchmark=False,
            label=f"search trial {strategy_name} {market}",
            frames=frames,
        )
    except Exception as exc:  # noqa: BLE001 -- one bad trial must not abort the search
        return Trial(
            params=params,
            metrics={},
            score=score_result({}, weights),
            seconds=time.monotonic() - began,
            error=f"{type(exc).__name__}: {exc}",
        )

    return Trial(
        params=params,
        metrics=result.metrics,
        score=score_result(result.metrics, weights),
        seconds=time.monotonic() - began,
    )


def run_search(
    session: Session,
    *,
    market: str,
    strategy: str = "trend_momentum",
    space: ParameterSpace | None = None,
    weights: ObjectiveWeights | None = None,
    method: str = "random",
    n_trials: int = 40,
    seed: int = 0,
    symbols: list[str] | None = None,
    capital: float | None = None,
    fragility_candidates: int = 5,
    progress: Callable[[int, int, Trial], None] | None = None,
) -> SearchResult:
    """Search parameters on the TRAIN split.

    There is no ``split`` argument, on purpose. A search that could be pointed at
    validation or test would eventually be pointed at them.

    Parameters
    ----------
    method:
        ``"grid"`` evaluates every combination; ``"random"`` samples ``n_trials``
        reproducibly from ``seed``.
    fragility_candidates:
        How many of the top trials get their neighbourhood evaluated. Each costs
        roughly two backtests per parameter axis, so this is the main cost knob.
    """
    from app.optimization.space import DEFAULT_TREND_MOMENTUM_SPACE

    search_space = space or DEFAULT_TREND_MOMENTUM_SPACE
    objective_weights = weights or ObjectiveWeights()

    # The same guard the CLI and API use. Belt and braces: this function also cannot be
    # asked for another split.
    window = resolve_window("train")

    result = SearchResult(
        market=market.upper(),
        strategy=strategy,
        method=method,
        split="train",
        space=search_space.describe(),
        weights=objective_weights.to_dict(),
        seed=seed if method == "random" else None,
    )

    if search_space.is_overfitting_prone:
        result.warnings.append(
            f"The parameter space has {search_space.grid_size():,} combinations, above the "
            f"{search_space.describe()['overfitting_threshold']} at which best-of-N results "
            "start to owe more to the number of attempts than to any parameter. Treat the "
            "winner as a hypothesis, and judge it on validation."
        )

    if method == "grid":
        points = list(search_space.grid())
    elif method == "random":
        points = search_space.sample(n_trials, seed=seed)
    else:
        raise ValueError(
            f"Unknown search method {method!r}. Supported: 'grid', 'random'. "
            "Bayesian optimisation would need scikit-optimize, which is not a dependency."
        )

    if method == "random" and n_trials > search_space.grid_size():
        result.warnings.append(
            f"Requested {n_trials} random trials over a space of only "
            f"{search_space.grid_size()}; the full grid was evaluated instead."
        )

    logger.info(
        "Search: %s %s, %s over %d point(s) on %s",
        strategy, result.market, method, len(points), window.describe(),
    )

    # Features depend only on bars, so compute them once for the whole search. Without
    # this, forty indicators over the universe were recomputed per trial, and the
    # fragility pass multiplied that by the neighbour count.
    try:
        shared_frames, skipped = prepare_frames(
            session, result.market, split="train", symbols=symbols
        )
        if skipped:
            result.warnings.append(
                f"{len(skipped)} instrument(s) excluded for insufficient history: "
                f"{'; '.join(skipped)}."
            )
    except Exception as exc:  # noqa: BLE001
        result.warnings.append(
            f"Could not precompute features ({type(exc).__name__}: {exc}); each trial will "
            "load its own, which is much slower."
        )
        shared_frames = None

    for index, params in enumerate(points, start=1):
        trial = _evaluate(
            session, strategy, result.market, params, window.start, window.end,
            objective_weights, symbols, capital, frames=shared_frames,
        )
        result.trials.append(trial)
        if progress:
            progress(index, len(points), trial)
        if not trial.ok:
            logger.debug("Trial %d failed: %s", index, trial.error)

    if not result.successful:
        result.warnings.append(
            "Every trial failed. Check that data is downloaded for this market and that "
            "the parameter space produces valid combinations."
        )
        result.finished_at = datetime.now(timezone.utc)
        return result

    # ---- Fragility: evaluate the neighbourhood of the top candidates ----
    evaluated: dict[str, float] = {
        repr(sorted(t.params.items())): t.score.value for t in result.successful
    }

    for trial in result.ranked(fragility_candidates):
        neighbours = search_space.neighbours(trial.params)
        values: list[float] = []
        for neighbour in neighbours:
            key = repr(sorted(neighbour.items()))
            if key in evaluated:
                values.append(evaluated[key])
                continue
            probe = _evaluate(
                session, strategy, result.market, neighbour, window.start, window.end,
                objective_weights, symbols, capital, frames=shared_frames,
            )
            if probe.ok:
                evaluated[key] = probe.score.value
                values.append(probe.score.value)

        if values:
            apply_fragility_penalty(trial.score, values, objective_weights)

    fragile = [t for t in result.ranked(fragility_candidates) if (t.score.fragility or 0) > 0.5]
    if fragile:
        result.warnings.append(
            f"{len(fragile)} of the top {fragility_candidates} configurations score well "
            "only at their own exact settings; their neighbours are materially worse. "
            "That pattern is what an overfitted parameter set looks like."
        )

    result.finished_at = datetime.now(timezone.utc)
    logger.info(
        "Search finished: %d/%d trials usable, best objective %.4f",
        len(result.successful), len(result.trials),
        result.best.score.value if result.best else float("nan"),
    )
    return result


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ValidationOutcome:
    """One candidate re-run on the validation split."""

    params: dict[str, Any]
    train_objective: float
    train_metrics: dict[str, Any]
    validation_objective: float | None
    validation_metrics: dict[str, Any]
    error: str = ""

    @property
    def degradation(self) -> float | None:
        """How much the objective fell from train to validation.

        The number to look at. A small drop suggests the configuration found something;
        a collapse means the train result was fitted. Some drop is always expected —
        train is where the winner was chosen, so it is the optimistic estimate by
        construction.
        """
        if self.validation_objective is None:
            return None
        return self.train_objective - self.validation_objective

    def to_dict(self) -> dict[str, Any]:
        keep = ("total_return_pct", "sharpe", "sortino", "max_drawdown_pct", "n_trades",
                "win_rate_pct", "profit_factor", "turnover_pct")
        return {
            "params": dict(self.params),
            "train": {
                "objective": round(self.train_objective, 6),
                **{k: self.train_metrics.get(k) for k in keep},
            },
            "validation": (
                None
                if self.validation_objective is None
                else {
                    "objective": round(self.validation_objective, 6),
                    **{k: self.validation_metrics.get(k) for k in keep},
                }
            ),
            "degradation": None if self.degradation is None else round(self.degradation, 6),
            "error": self.error,
        }


def select_on_validation(
    session: Session,
    search: SearchResult,
    *,
    top_n: int = 5,
    symbols: list[str] | None = None,
    capital: float | None = None,
) -> dict[str, Any]:
    """Re-run the top ``top_n`` train candidates on VALIDATION and pick among them.

    Only a shortlist is evaluated, and only once. Scoring every trial on validation, or
    re-running the shortlist repeatedly, turns validation into a second training set: each
    look leaks a little more, and after enough looks the "out-of-sample" estimate is
    nothing of the kind.

    Returns the candidates with their train and validation figures, and the recommended
    one — chosen by validation objective, not by train.
    """
    weights = ObjectiveWeights.from_dict(search.weights)
    window = resolve_window("validation")
    outcomes: list[ValidationOutcome] = []

    # Validation needs its own frames: the train set was built over a different window.
    try:
        validation_frames, _ = prepare_frames(
            session, search.market, split="validation", symbols=symbols
        )
    except Exception:  # noqa: BLE001
        validation_frames = None

    for trial in search.ranked(top_n):
        try:
            strategy = build_strategy(search.strategy, trial.params)
            result = run_backtest(
                session,
                strategy,
                search.market,
                split="validation",
                start=window.start,
                end=window.end,
                symbols=symbols,
                initial_capital=capital,
                include_benchmark=False,
                label=f"validation {search.strategy} {search.market}",
                frames=validation_frames,
            )
            score = score_result(result.metrics, weights)
            outcomes.append(
                ValidationOutcome(
                    params=trial.params,
                    train_objective=trial.score.value,
                    train_metrics=trial.metrics,
                    validation_objective=score.value,
                    validation_metrics=result.metrics,
                )
            )
        except Exception as exc:  # noqa: BLE001
            outcomes.append(
                ValidationOutcome(
                    params=trial.params,
                    train_objective=trial.score.value,
                    train_metrics=trial.metrics,
                    validation_objective=None,
                    validation_metrics={},
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    usable = [
        o for o in outcomes
        if o.validation_objective is not None
        and int(o.validation_metrics.get("n_trades") or 0) > 0
    ]
    recommended = max(usable, key=lambda o: o.validation_objective) if usable else None

    notes = [
        "Selection used the VALIDATION split; the search itself saw only TRAIN.",
        "The TEST split has not been read. It stays sealed until a result is finalised, "
        "and reading it more than once would leave no out-of-sample estimate at all.",
    ]

    if recommended is not None:
        train_rank = [o.params for o in outcomes].index(recommended.params)
        if train_rank != 0:
            notes.append(
                f"The configuration that won on validation ranked #{train_rank + 1} on "
                "train, not first. That is normal and is the reason for having a "
                "validation split: the train winner is the most overfitted candidate by "
                "construction."
            )
        drop = recommended.degradation
        if drop is not None and drop > 1.0:
            notes.append(
                f"The recommended configuration's objective fell {drop:.2f} from train to "
                "validation. A large drop means the train figure was substantially fitted."
            )

    return {
        "market": search.market,
        "strategy": search.strategy,
        "validation_window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
        },
        "n_candidates": len(outcomes),
        "candidates": [o.to_dict() for o in outcomes],
        "recommended": recommended.to_dict() if recommended else None,
        "notes": notes,
    }
