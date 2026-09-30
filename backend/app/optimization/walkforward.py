"""Walk-forward analysis.

The most honest measurement this project can produce, and the reason it exists.

An ordinary backtest optimised on its own data tells you nothing: the parameters were
chosen knowing the answer. Walk-forward asks the harder question — *if I had been running
this, re-tuning periodically, what would have happened?* — by repeating the whole
procedure at each point in time:

1. Optimise on the training window.
2. **Freeze** the chosen parameters.
3. Run the next, unseen window with them.
4. Record it, roll forward, repeat.

Every test window is out-of-sample with respect to the parameters that traded it. Nothing
in step 3 can see step 3's data, because step 1 finished before it began.

The number that matters
-----------------------
:attr:`WalkForwardResult.aggregate_metrics` is computed over the **stitched out-of-sample
equity curve** — every test window concatenated, in order. That is the closest thing to
an honest estimate available here. The per-window train figures are shown for diagnosis
and must never be averaged into a headline: averaging in-sample results is how a
walk-forward study gets quietly turned back into a normal backtest.

Parameter stability across windows
----------------------------------
If each window picks wildly different parameters, the procedure is not finding a stable
edge; it is refitting noise every period. :attr:`WalkForwardResult.parameter_stability`
measures that, and a strategy that fails it should not be trusted even when the stitched
curve looks acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import pandas as pd
from sqlalchemy.orm import Session

from app.backtesting.metrics import compute_metrics
from app.backtesting.runner import prepare_frames, run_backtest
from app.config import get_settings
from app.core.logging import get_logger
from app.optimization.objective import ObjectiveWeights, score_result
from app.optimization.space import ParameterSpace
from app.strategies.registry import build_strategy

logger = get_logger(__name__)

__all__ = ["Window", "WindowResult", "WalkForwardResult", "build_windows", "run_walk_forward"]


@dataclass(frozen=True, slots=True)
class Window:
    """One train/test pair."""

    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date

    def describe(self) -> str:
        return (
            f"#{self.index}: train {self.train_start}..{self.train_end}, "
            f"test {self.test_start}..{self.test_end}"
        )

    def to_dict(self) -> dict[str, str | int]:
        return {
            "index": self.index,
            "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "test_end": self.test_end.isoformat(),
        }


def build_windows(
    start: date,
    end: date,
    *,
    train_years: int = 4,
    test_years: int = 1,
    step_years: int = 1,
) -> list[Window]:
    """Rolling train/test windows across ``[start, end]``.

    Windows *roll* rather than expand: each keeps a fixed-length training period rather
    than accumulating all history. That is the harder and more realistic setting — a
    fixed window has to keep working as the regime changes, while an expanding one is
    increasingly dominated by old data and quietly becomes easier over time.

    Raises ``ValueError`` when the span cannot fit a single window, rather than returning
    an empty list that a caller might read as "no problems found".
    """
    if train_years < 1 or test_years < 1 or step_years < 1:
        raise ValueError("train_years, test_years and step_years must all be at least 1")
    if end <= start:
        raise ValueError(f"end ({end}) must be after start ({start})")

    windows: list[Window] = []
    index = 1
    train_start = start

    while True:
        # stdlib timedelta: adding a pandas Timedelta to a `date` routes through NumPy and
        # emits a generic-unit DeprecationWarning, which the strict warning filter turns
        # into an error.
        one_day = timedelta(days=1)
        train_end = _add_years(train_start, train_years) - one_day
        test_start = train_end + one_day
        test_end = _add_years(test_start, test_years) - one_day

        if test_start > end:
            break
        # A truncated final window is kept, but only if it has a meaningful test period.
        # Reporting a two-week "year" alongside full years would distort every average.
        if test_end > end:
            if (end - test_start).days < 180:
                break
            test_end = end

        windows.append(
            Window(
                index=index,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        index += 1
        train_start = _add_years(train_start, step_years)

    if not windows:
        raise ValueError(
            f"{start}..{end} is too short for {train_years}y train + {test_years}y test. "
            f"It spans {(end - start).days} days; at least "
            f"{(train_years + test_years) * 365} are needed."
        )
    return windows


def _add_years(value: date, years: int) -> date:
    """Add whole years, clamping 29 February to 28 February in a non-leap year."""
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class WindowResult:
    """What one window produced."""

    window: Window
    chosen_params: dict[str, Any]
    train_objective: float | None
    train_metrics: dict[str, Any]
    test_metrics: dict[str, Any]
    test_equity: pd.Series
    n_candidates: int
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and not self.test_equity.empty

    def to_dict(self) -> dict[str, Any]:
        keep = ("total_return_pct", "cagr_pct", "sharpe", "sortino", "max_drawdown_pct",
                "annualised_volatility_pct", "n_trades", "win_rate_pct", "profit_factor",
                "turnover_pct", "exposure_pct")
        return {
            "window": self.window.to_dict(),
            "chosen_params": dict(self.chosen_params),
            "n_candidates_searched": self.n_candidates,
            "train": {
                "objective": None if self.train_objective is None else round(self.train_objective, 6),
                **{k: self.train_metrics.get(k) for k in keep},
            },
            # The only figures here that are out-of-sample.
            "test": {k: self.test_metrics.get(k) for k in keep},
            "error": self.error,
        }


@dataclass(slots=True)
class WalkForwardResult:
    """A whole walk-forward study."""

    market: str
    strategy: str
    train_years: int
    test_years: int
    space: dict[str, Any]
    weights: dict[str, Any]
    windows: list[WindowResult] = field(default_factory=list)
    stitched_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    aggregate_metrics: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def successful(self) -> list[WindowResult]:
        return [w for w in self.windows if w.ok]

    def parameter_stability(self) -> dict[str, Any]:
        """How consistently each parameter was chosen across windows.

        Wild variation means the procedure refits noise each period. That is a finding
        about the strategy, not a defect of the analysis, and it should override a
        flattering stitched curve.
        """
        usable = self.successful
        if len(usable) < 2:
            return {"available": False, "reason": "fewer than two successful windows"}

        names = sorted({k for w in usable for k in w.chosen_params})
        out: dict[str, Any] = {"available": True, "n_windows": len(usable), "parameters": {}}
        unstable: list[str] = []

        for name in names:
            values = [w.chosen_params.get(name) for w in usable]
            labels = [repr(v) for v in values]
            distinct = set(labels)
            concentration = max(labels.count(v) for v in distinct) / len(labels)
            entry: dict[str, Any] = {
                "values_by_window": values,
                "distinct_values": len(distinct),
                "concentration": round(concentration, 4),
            }
            numeric = [v for v in values if isinstance(v, (int, float))]
            if numeric:
                entry["min"] = min(numeric)
                entry["max"] = max(numeric)
                entry["mean"] = round(sum(numeric) / len(numeric), 4)
                spread = max(numeric) - min(numeric)
                mean = sum(numeric) / len(numeric)
                entry["relative_spread"] = round(spread / abs(mean), 4) if mean else None
            out["parameters"][name] = entry
            if concentration < 0.5:
                unstable.append(name)

        if unstable:
            out["unstable"] = unstable
            out["note"] = (
                f"{', '.join(unstable)} changed in more than half the windows. The "
                "procedure is re-fitting these each period rather than finding a stable "
                "setting, which means the value it would pick for the next period carries "
                "little information."
            )
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "strategy": self.strategy,
            "train_years": self.train_years,
            "test_years": self.test_years,
            "space": self.space,
            "weights": self.weights,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "n_windows": len(self.windows),
            "n_successful": len(self.successful),
            "warnings": self.warnings,
            "windows": [w.to_dict() for w in self.windows],
            # The headline. Computed on stitched out-of-sample results only.
            "aggregate_metrics": self.aggregate_metrics,
            "parameter_stability": self.parameter_stability(),
            "equity_curve": [
                {"ts": ts.isoformat(), "equity": float(v)}
                for ts, v in self.stitched_equity.items()
            ],
            "interpretation": (
                "aggregate_metrics is computed over the concatenated out-of-sample test "
                "windows, using parameters frozen before each window began. The per-window "
                "train figures are diagnostic only: averaging them would turn this back "
                "into an ordinary in-sample backtest."
            ),
        }


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def run_walk_forward(
    session: Session,
    *,
    market: str,
    strategy: str = "trend_momentum",
    space: ParameterSpace | None = None,
    weights: ObjectiveWeights | None = None,
    train_years: int = 4,
    test_years: int = 1,
    step_years: int = 1,
    n_trials: int = 20,
    seed: int = 0,
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
    capital: float | None = None,
    progress: Callable[[int, int, WindowResult], None] | None = None,
) -> WalkForwardResult:
    """Run a rolling walk-forward study.

    Each window searches its own training period, freezes the winner, and trades the
    following period with it. No window's parameters are informed by that window's test
    data — that is the entire point, and it is why this is slow: the search is repeated
    per window rather than done once.

    ``start``/``end`` default to spanning the configured train and test splits, so the
    study covers the whole available history. Note that a walk-forward run deliberately
    crosses the split boundaries: the splits exist to protect a *single* optimisation,
    while walk-forward re-optimises continuously and gets its out-of-sample guarantee
    from the time ordering instead.
    """
    from app.optimization.space import DEFAULT_TREND_MOMENTUM_SPACE

    settings = get_settings()
    search_space = space or DEFAULT_TREND_MOMENTUM_SPACE
    objective_weights = weights or ObjectiveWeights()

    span_start = start or settings.split_train_start
    span_end = end or settings.split_test_end
    windows = build_windows(
        span_start, span_end,
        train_years=train_years, test_years=test_years, step_years=step_years,
    )

    result = WalkForwardResult(
        market=market.upper(),
        strategy=strategy,
        train_years=train_years,
        test_years=test_years,
        space=search_space.describe(),
        weights=objective_weights.to_dict(),
    )
    result.warnings.append(
        "A walk-forward run crosses the configured train/validation/test boundaries by "
        "design. Its out-of-sample guarantee comes from time ordering — each window's "
        "parameters were frozen before that window's data was seen — not from the split "
        "configuration, which protects a single one-off optimisation instead."
    )
    if search_space.is_overfitting_prone and n_trials < search_space.grid_size():
        result.warnings.append(
            f"Each window samples {n_trials} of {search_space.grid_size():,} combinations. "
            "A different random subset per window adds noise to the parameter-stability "
            "measurement, so read that section as a lower bound on stability."
        )

    logger.info(
        "Walk-forward: %s %s, %d windows (%dy train / %dy test)",
        strategy, result.market, len(windows), train_years, test_years,
    )

    for window in windows:
        window_result = _run_window(
            session, window,
            market=result.market, strategy=strategy, space=search_space,
            weights=objective_weights, n_trials=n_trials, seed=seed + window.index,
            symbols=symbols, capital=capital,
        )
        result.windows.append(window_result)
        if progress:
            progress(window.index, len(windows), window_result)

        logger.info(
            "  %s -> %s",
            window.describe(),
            window_result.error
            or f"OOS return {window_result.test_metrics.get('total_return_pct')}",
        )

    _stitch(result, capital or settings.initial_capital_usd)
    result.finished_at = datetime.now(timezone.utc)
    return result


def _run_window(
    session: Session,
    window: Window,
    *,
    market: str,
    strategy: str,
    space: ParameterSpace,
    weights: ObjectiveWeights,
    n_trials: int,
    seed: int,
    symbols: list[str] | None,
    capital: float | None,
) -> WindowResult:
    """Search this window's train period, freeze the winner, run its test period."""
    points = space.sample(n_trials, seed=seed)

    # One feature computation for this window, shared by every trial and the frozen test
    # run. Features depend only on bars, so recomputing them per trial was pure waste --
    # and walk-forward multiplies a search by the number of windows, so it was the worst
    # place to leave it.
    try:
        window_frames, _ = prepare_frames(
            session, market, split='full',
            start=window.train_start, end=window.test_end, symbols=symbols,
        )
    except Exception:  # noqa: BLE001 -- fall back to per-trial loading
        window_frames = None

    best_params: dict[str, Any] | None = None
    best_objective = float("-inf")
    best_metrics: dict[str, Any] = {}

    for params in points:
        try:
            candidate = build_strategy(strategy, params)
            train = run_backtest(
                session, candidate, market,
                split="full",  # the window's own bounds define the period
                start=window.train_start, end=window.train_end,
                symbols=symbols, initial_capital=capital,
                include_benchmark=False, label=f"wf train {window.index}",
                frames=window_frames,
            )
        except Exception:  # noqa: BLE001 -- an invalid or failing point is expected
            continue

        if int(train.metrics.get("n_trades") or 0) == 0:
            continue
        score = score_result(train.metrics, weights)
        if score.value > best_objective:
            best_objective, best_params, best_metrics = score.value, params, train.metrics

    if best_params is None:
        return WindowResult(
            window=window, chosen_params={}, train_objective=None, train_metrics={},
            test_metrics={}, test_equity=pd.Series(dtype=float), n_candidates=len(points),
            error="no candidate produced any trades on this window's training period",
        )

    # Parameters are now frozen. Nothing below may change them.
    try:
        frozen = build_strategy(strategy, best_params)
        test = run_backtest(
            session, frozen, market,
            split="full", start=window.test_start, end=window.test_end,
            symbols=symbols, initial_capital=capital,
            include_benchmark=False, label=f"wf test {window.index}",
            frames=window_frames,
        )
    except Exception as exc:  # noqa: BLE001
        return WindowResult(
            window=window, chosen_params=best_params, train_objective=best_objective,
            train_metrics=best_metrics, test_metrics={},
            test_equity=pd.Series(dtype=float), n_candidates=len(points),
            error=f"test window failed: {type(exc).__name__}: {exc}",
        )

    return WindowResult(
        window=window, chosen_params=best_params, train_objective=best_objective,
        train_metrics=best_metrics, test_metrics=test.metrics,
        test_equity=test.equity_curve, n_candidates=len(points),
    )


def _stitch(result: WalkForwardResult, initial_capital: float) -> None:
    """Concatenate the out-of-sample windows into one continuous equity curve.

    Each window's backtest restarts from the same capital, so the curves are converted to
    *returns* and compounded. Simply concatenating the raw equity values would reset the
    account at every window boundary and produce a sawtooth that understates both the
    compounding and the drawdowns.
    """
    usable = result.successful
    if not usable:
        result.warnings.append("No window produced a usable out-of-sample result.")
        return

    equity = initial_capital
    points: list[tuple[pd.Timestamp, float]] = []

    for window_result in usable:
        curve = window_result.test_equity.dropna()
        if len(curve) < 2 or curve.iloc[0] <= 0:
            continue
        # Index the window to its own start, then scale onto the running balance.
        relative = curve / curve.iloc[0]
        scaled = relative * equity
        for ts, value in scaled.items():
            points.append((ts, float(value)))
        equity = float(scaled.iloc[-1])

    if not points:
        result.warnings.append("Windows produced no usable equity curves to stitch.")
        return

    series = pd.Series(
        [v for _, v in points],
        index=pd.DatetimeIndex([ts for ts, _ in points], name="ts"),
        name="equity",
    )
    # Window boundaries can share a timestamp; keep the later value.
    series = series[~series.index.duplicated(keep="last")].sort_index()
    result.stitched_equity = series

    trades: list[Any] = []
    result.aggregate_metrics = compute_metrics(series, trades)
    result.aggregate_metrics["n_trades"] = sum(
        int(w.test_metrics.get("n_trades") or 0) for w in usable
    )
    result.aggregate_metrics["n_windows"] = len(usable)
    result.aggregate_metrics["basis"] = (
        "Stitched out-of-sample windows, compounded. Per-trade statistics are omitted "
        "because trades come from separate runs with different parameters; the windows "
        "section carries them individually."
    )

    negative = [
        w.window.index for w in usable
        if (w.test_metrics.get("total_return_pct") or 0) < 0
    ]
    if negative:
        result.warnings.append(
            f"{len(negative)} of {len(usable)} out-of-sample windows lost money "
            f"(windows {negative}). Consistency across windows matters more than the "
            "aggregate: one strong window can carry an otherwise failing strategy."
        )
