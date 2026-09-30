"""Persisting optimisation results.

A search takes tens of minutes and a walk-forward study longer. That rules out running
either behind an HTTP request: the dashboard reads *stored* runs, and the CLI is what
produces them. Any other arrangement ends with a web request timing out halfway through an
hour of computation, or — worse — a page that silently re-runs an expensive job every time
someone opens it.

The split guard, once more
-------------------------
:func:`save_search` writes ``split="train"`` unconditionally and the ``optimization_runs``
table carries ``CHECK (split = 'train')``. A stored run that claims to have optimised on
validation or test data therefore cannot exist — not as a matter of convention, but because
the database would reject the row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.database.models import OptimizationRun, Strategy, WalkForwardRun
from app.optimization.robustness import RobustnessReport
from app.optimization.search import SearchResult
from app.optimization.walkforward import WalkForwardResult

logger = get_logger(__name__)

__all__ = [
    "save_search",
    "save_walk_forward",
    "list_optimization_runs",
    "get_optimization_run",
    "list_walk_forward_runs",
    "get_walk_forward_run",
]

# Keep stored payloads bounded. A grid search can produce thousands of trials, and the
# tail of a ranked list is not worth the database size — the top of it is what anyone
# looks at, and the summary statistics are computed before truncation.
MAX_STORED_TRIALS = 200


def _ensure_strategy(session: Session, name: str, params: dict[str, Any]) -> Strategy:
    """Find or create the ``strategies`` row for a name, recording its parameters."""
    row = session.scalar(select(Strategy).where(Strategy.name == name, Strategy.version == "1"))
    if row is None:
        row = Strategy(name=name, version="1", params=params)
        session.add(row)
        session.flush()
    return row


def save_search(
    session: Session,
    search: SearchResult,
    *,
    selection: dict[str, Any] | None = None,
    robustness: RobustnessReport | None = None,
    label: str = "",
) -> OptimizationRun:
    """Persist a search, its validation selection and any robustness report."""
    if search.split != "train":
        # Unreachable through run_search, which hard-codes it. Checked anyway: this is
        # the last point before the claim becomes a stored fact.
        raise ValueError(
            f"Refusing to store a search that claims split={search.split!r}. Only TRAIN "
            "searches are valid, and the database constraint would reject this anyway."
        )

    strategy = _ensure_strategy(
        session, search.strategy, search.best.params if search.best else {}
    )
    payload = search.to_dict(trial_limit=MAX_STORED_TRIALS)

    run = OptimizationRun(
        strategy_id=strategy.id,
        label=label or f"{search.strategy} {search.market} {search.method}",
        market=search.market,
        method=search.method,
        split="train",
        param_space=payload["space"],
        objective_weights=payload["weights"],
        n_trials=len(search.trials),
        best_params=search.best.params if search.best else {},
        best_objective=search.best.score.value if search.best else None,
        trials=payload["trials"],
        robustness={
            "warnings": search.warnings,
            "parameter_stability": payload["parameter_stability"],
            "seed": search.seed,
            "n_failed": payload["n_failed"],
            "validation_selection": selection,
            "stress_tests": robustness.to_dict() if robustness else None,
        },
        started_at=search.started_at,
        finished_at=search.finished_at or datetime.now(timezone.utc),
    )
    session.add(run)
    session.flush()
    logger.info("Stored optimization run %d (%s)", run.id, run.label)
    return run


def save_walk_forward(
    session: Session, result: WalkForwardResult, *, label: str = ""
) -> WalkForwardRun:
    """Persist a walk-forward study."""
    strategy = _ensure_strategy(session, result.strategy, {})
    payload = result.to_dict()

    run = WalkForwardRun(
        strategy_id=strategy.id,
        label=label or f"{result.strategy} {result.market} walk-forward",
        market=result.market,
        train_years=result.train_years,
        test_years=result.test_years,
        param_space=payload["space"],
        windows=payload["windows"],
        aggregate_metrics={
            **payload["aggregate_metrics"],
            "warnings": result.warnings,
            "interpretation": payload["interpretation"],
            # The stitched curve lives here rather than in its own column: it belongs
            # with the aggregate figures it was used to compute.
            "equity_curve": payload["equity_curve"],
        },
        parameter_stability=payload["parameter_stability"],
        started_at=result.started_at,
        finished_at=result.finished_at or datetime.now(timezone.utc),
    )
    session.add(run)
    session.flush()
    logger.info("Stored walk-forward run %d (%s)", run.id, run.label)
    return run


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def _summarise_optimization(run: OptimizationRun) -> dict[str, Any]:
    extras = run.robustness or {}
    selection = extras.get("validation_selection") or {}
    recommended = (selection or {}).get("recommended")

    return {
        "id": run.id,
        "label": run.label,
        "market": run.market,
        "method": run.method,
        "split": run.split,
        "n_trials": run.n_trials,
        "n_failed": extras.get("n_failed", 0),
        "seed": extras.get("seed"),
        "best_objective": run.best_objective,
        "best_params": run.best_params,
        "grid_size": (run.param_space or {}).get("grid_size"),
        "overfitting_prone": (run.param_space or {}).get("overfitting_prone"),
        "warnings": extras.get("warnings", []),
        "has_validation": bool(selection),
        "recommended_params": (recommended or {}).get("params"),
        "has_robustness": bool(extras.get("stress_tests")),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def list_optimization_runs(
    session: Session, *, market: str | None = None, limit: int = 25
) -> list[dict[str, Any]]:
    """Recent searches, newest first, as summaries."""
    statement = select(OptimizationRun).order_by(OptimizationRun.started_at.desc())
    if market:
        statement = statement.where(OptimizationRun.market == market.upper())
    return [_summarise_optimization(r) for r in session.scalars(statement.limit(limit))]


def get_optimization_run(session: Session, run_id: int) -> dict[str, Any] | None:
    """One search in full, including trials and any validation or robustness results."""
    run = session.get(OptimizationRun, run_id)
    if run is None:
        return None

    extras = run.robustness or {}
    return {
        **_summarise_optimization(run),
        "param_space": run.param_space,
        "objective_weights": run.objective_weights,
        "trials": run.trials,
        "parameter_stability": extras.get("parameter_stability"),
        "validation_selection": extras.get("validation_selection"),
        "stress_tests": extras.get("stress_tests"),
        "note": (
            "Trials were scored on TRAIN only. Where a validation selection is present, "
            "the recommended configuration is the one that scored best on VALIDATION, "
            "which is usually not the train winner. The TEST split was not read."
        ),
    }


def _summarise_walk_forward(run: WalkForwardRun) -> dict[str, Any]:
    aggregate = run.aggregate_metrics or {}
    windows = run.windows or []
    usable = [w for w in windows if not w.get("error")]
    losing = [
        w for w in usable if (w.get("test", {}).get("total_return_pct") or 0) < 0
    ]

    return {
        "id": run.id,
        "label": run.label,
        "market": run.market,
        "train_years": run.train_years,
        "test_years": run.test_years,
        "n_windows": len(windows),
        "n_usable": len(usable),
        "n_losing_windows": len(losing),
        # Out-of-sample by construction, which is what makes these worth reading.
        "total_return_pct": aggregate.get("total_return_pct"),
        "cagr_pct": aggregate.get("cagr_pct"),
        "sharpe": aggregate.get("sharpe"),
        "sortino": aggregate.get("sortino"),
        "max_drawdown_pct": aggregate.get("max_drawdown_pct"),
        "n_trades": aggregate.get("n_trades"),
        "unstable_parameters": (run.parameter_stability or {}).get("unstable", []),
        "warnings": aggregate.get("warnings", []),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def list_walk_forward_runs(
    session: Session, *, market: str | None = None, limit: int = 25
) -> list[dict[str, Any]]:
    statement = select(WalkForwardRun).order_by(WalkForwardRun.started_at.desc())
    if market:
        statement = statement.where(WalkForwardRun.market == market.upper())
    return [_summarise_walk_forward(r) for r in session.scalars(statement.limit(limit))]


def get_walk_forward_run(session: Session, run_id: int) -> dict[str, Any] | None:
    """One study in full, with every window and the stitched curve."""
    run = session.get(WalkForwardRun, run_id)
    if run is None:
        return None

    aggregate = dict(run.aggregate_metrics or {})
    equity = aggregate.pop("equity_curve", [])

    return {
        **_summarise_walk_forward(run),
        "param_space": run.param_space,
        "windows": run.windows,
        "aggregate_metrics": aggregate,
        "parameter_stability": run.parameter_stability,
        "equity_curve": equity,
        "note": aggregate.get(
            "interpretation",
            "Aggregate figures come from the stitched out-of-sample windows. Per-window "
            "train figures are diagnostic only; averaging them would turn this back into "
            "an ordinary in-sample backtest.",
        ),
    }
