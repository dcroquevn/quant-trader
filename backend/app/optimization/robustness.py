"""Robustness testing.

A backtest result is one draw from a distribution. These checks ask how much of it
survives when the assumptions wobble — and the honest answer is often "not much".

Six checks, each answering a different question
-----------------------------------------------
**Parameter sensitivity** — does it still work one step away? A configuration that
collapses when RSI moves from 40 to 45 was fitted to this sample, not discovered in it.
This is the single most diagnostic check here.

**Cost sensitivity** (commission and slippage, separately) — at what assumed cost does the
edge disappear? Since the shipped costs are placeholders, the *breakeven cost* is more
useful than any return figure computed at one guess.

**Execution delay** — what if the fill came a bar later? A strategy that needs
next-open execution and dies at next-next-open is trading a very short-lived signal, which
retail execution will not capture.

**Monte Carlo trade reshuffling** — the sequence of the same trades, reordered. Total
return is unchanged by construction; *drawdown* is not. This separates "the strategy made
money" from "the strategy made money in an order that happened to avoid a deep hole".

**Random trade removal** — drop a fraction of trades at random. If removing 10% of trades
destroys the result, a handful of trades carried it, and there is no reason to expect the
next handful to behave the same.

What these cannot tell you
--------------------------
All of it resamples **one historical path**. Monte Carlo over the realised trades explores
orderings that did happen to be available; it says nothing about trades the strategy never
took, regimes not in the sample, or the possibility that the whole period was unusual. A
strategy can pass every check here and still fail, and the report says so.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.backtesting.costs import CostModel
from app.backtesting.metrics import max_drawdown
from app.backtesting.runner import run_backtest
from app.config import MarketCostConfig, get_settings
from app.core.logging import get_logger
from app.optimization.objective import ObjectiveWeights, score_result
from app.optimization.space import ParameterSpace
from app.strategies.registry import build_strategy

logger = get_logger(__name__)

__all__ = [
    "RobustnessReport",
    "parameter_sensitivity",
    "cost_sensitivity",
    "execution_delay_sensitivity",
    "monte_carlo_reshuffle",
    "random_trade_removal",
    "run_robustness_suite",
    "FRAGILE_SENSITIVITY_THRESHOLD",
]

FRAGILE_SENSITIVITY_THRESHOLD = 0.5
"""Fraction of neighbours that may lose money before the parameters are called fragile.

If more than half of a configuration's immediate neighbours are unprofitable while it is
profitable, the result sits on a spike. That is the brief's "works only with extremely
specific parameters" condition, made measurable.
"""


@dataclass(slots=True)
class RobustnessReport:
    """Results of every check that was run."""

    market: str
    strategy: str
    params: dict[str, Any]
    baseline: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, Any] = field(default_factory=dict)
    verdicts: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    @property
    def is_fragile(self) -> bool:
        """True when any check flagged the configuration as fragile."""
        return any(v.startswith("FRAGILE") for v in self.verdicts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "strategy": self.strategy,
            "params": dict(self.params),
            "baseline": self.baseline,
            "checks": self.checks,
            "verdicts": self.verdicts,
            "is_fragile": self.is_fragile,
            "caveats": self.caveats,
        }


def _summary(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        k: metrics.get(k)
        for k in (
            "total_return_pct", "cagr_pct", "sharpe", "sortino", "max_drawdown_pct",
            "n_trades", "win_rate_pct", "profit_factor", "turnover_pct",
        )
    }


def _run(
    session: Session,
    *,
    strategy_name: str,
    market: str,
    params: dict[str, Any],
    start: date,
    end: date,
    symbols: list[str] | None,
    capital: float | None,
    cost_model: CostModel | None = None,
) -> dict[str, Any] | None:
    """One backtest, returning metrics or ``None`` when it could not run."""
    try:
        strategy = build_strategy(strategy_name, params)
    except ValueError:
        return None

    try:
        if cost_model is None:
            result = run_backtest(
                session, strategy, market, split="full", start=start, end=end,
                symbols=symbols, initial_capital=capital, include_benchmark=False,
                label="robustness",
            )
            return result.metrics

        # A custom cost model has to bypass run_backtest, which builds its own from
        # configuration. Compose the pieces directly instead.
        from app.backtesting.engine import BacktestConfig, Backtester
        from app.backtesting.metrics import compute_metrics
        from app.backtesting.runner import load_features
        from app.core.markets import get_market
        from app.core.universe import universe_for_market

        settings = get_settings()
        market_spec = get_market(market)
        tradable = symbols or [s.symbol for s in universe_for_market(market_spec.code)]
        frames, _ = load_features(
            session, tradable, market_spec.code,
            warmup_start=start - timedelta(days=500), end=end,
        )
        config = BacktestConfig(
            market=market_spec.code,
            initial_capital=capital
            or (
                settings.initial_capital_usd
                if market_spec.currency == "USD"
                else settings.initial_capital_clp
            ),
            risk_per_trade_pct=settings.risk_per_trade_pct,
            max_position_size_pct=settings.max_position_size_pct,
            max_simultaneous_positions=settings.max_simultaneous_positions,
            split="full",
            label="robustness (custom costs)",
        )
        run = Backtester(strategy, config, cost_model=cost_model).run(
            frames, start=start, end=end
        )
        return compute_metrics(run.equity_curve, run.trades, run.snapshots)
    except Exception as exc:  # noqa: BLE001 -- a failed variant is data, not a crash
        logger.debug("Robustness variant failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# 1. Parameter sensitivity
# --------------------------------------------------------------------------- #


def parameter_sensitivity(
    session: Session,
    *,
    strategy: str,
    market: str,
    params: dict[str, Any],
    space: ParameterSpace,
    start: date,
    end: date,
    baseline: dict[str, Any],
    symbols: list[str] | None = None,
    capital: float | None = None,
    weights: ObjectiveWeights | None = None,
) -> dict[str, Any]:
    """Re-run every immediate neighbour of ``params``.

    The brief's "works only with extremely specific parameters" test. A configuration
    surrounded by losses is a spike in a noisy surface; one on a plateau might be real.
    """
    objective_weights = weights or ObjectiveWeights()
    neighbours = space.neighbours(params)
    if not neighbours:
        return {
            "available": False,
            "reason": "the chosen parameters are not on the search grid, so neighbours "
            "cannot be identified",
        }

    baseline_return = baseline.get("total_return_pct")
    baseline_score = score_result(baseline, objective_weights).value
    rows: list[dict[str, Any]] = []

    for neighbour in neighbours:
        metrics = _run(
            session, strategy_name=strategy, market=market, params=neighbour,
            start=start, end=end, symbols=symbols, capital=capital,
        )
        if metrics is None:
            continue
        changed = {k: v for k, v in neighbour.items() if params.get(k) != v}
        rows.append(
            {
                "changed": changed,
                "objective": round(score_result(metrics, objective_weights).value, 4),
                **_summary(metrics),
            }
        )

    if not rows:
        return {"available": False, "reason": "no neighbour produced a usable result"}

    returns = [r["total_return_pct"] for r in rows if r["total_return_pct"] is not None]
    losing = [r for r in returns if r < 0]
    objectives = [r["objective"] for r in rows]

    out: dict[str, Any] = {
        "available": True,
        "n_neighbours": len(rows),
        "baseline_return_pct": baseline_return,
        "baseline_objective": round(baseline_score, 4),
        "neighbour_return_median": round(float(np.median(returns)), 4) if returns else None,
        "neighbour_return_min": round(min(returns), 4) if returns else None,
        "neighbour_return_max": round(max(returns), 4) if returns else None,
        "neighbour_objective_median": round(float(np.median(objectives)), 4),
        "fraction_losing": round(len(losing) / len(returns), 4) if returns else None,
        "neighbours": rows,
    }

    if returns and out["fraction_losing"] is not None:
        if out["fraction_losing"] > FRAGILE_SENSITIVITY_THRESHOLD and (baseline_return or 0) > 0:
            out["verdict"] = (
                f"FRAGILE: {out['fraction_losing']:.0%} of the {len(rows)} immediate "
                "neighbours lose money while these parameters make money. The result sits "
                "on a spike, which is what a configuration fitted to this particular "
                "sample looks like."
            )
        elif out["fraction_losing"] == 0:
            out["verdict"] = (
                f"Every one of the {len(rows)} neighbours is also profitable, so the "
                "parameters sit on a plateau rather than a peak."
            )
        else:
            out["verdict"] = (
                f"{out['fraction_losing']:.0%} of neighbours lose money — a mixed "
                "neighbourhood, neither a plateau nor a spike."
            )
    return out


# --------------------------------------------------------------------------- #
# 2 & 3. Cost sensitivity
# --------------------------------------------------------------------------- #


def cost_sensitivity(
    session: Session,
    *,
    strategy: str,
    market: str,
    params: dict[str, Any],
    start: date,
    end: date,
    component: str = "commission",
    multipliers: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0),
    symbols: list[str] | None = None,
    capital: float | None = None,
) -> dict[str, Any]:
    """Re-run at multiples of the configured commission or slippage.

    Reports the **breakeven multiplier**: the assumed cost at which the strategy stops
    making money. That is the useful output, because the configured costs are guesses —
    knowing the edge survives to 3x the assumption is worth more than knowing the return
    at 1x.
    """
    if component not in ("commission", "slippage", "both"):
        raise ValueError(
            f"component must be 'commission', 'slippage' or 'both', got {component!r}"
        )

    settings = get_settings()
    base = settings.costs_for(market)
    rows: list[dict[str, Any]] = []

    for multiplier in multipliers:
        config = MarketCostConfig(
            commission_bps=(
                base.commission_bps * multiplier
                if component in ("commission", "both")
                else base.commission_bps
            ),
            min_commission=base.min_commission * (
                multiplier if component in ("commission", "both") else 1.0
            ),
            slippage_bps=(
                base.slippage_bps * multiplier
                if component in ("slippage", "both")
                else base.slippage_bps
            ),
            spread_bps=(
                base.spread_bps * multiplier if component == "both" else base.spread_bps
            ),
        )
        metrics = _run(
            session, strategy_name=strategy, market=market, params=params,
            start=start, end=end, symbols=symbols, capital=capital,
            cost_model=CostModel(market=market.upper(), config=config),
        )
        if metrics is None:
            continue
        rows.append(
            {
                "multiplier": multiplier,
                "round_trip_pct": round(config.round_trip_bps() * 1e-4 * 100, 4),
                **_summary(metrics),
            }
        )

    if not rows:
        return {"available": False, "reason": f"no {component} variant produced a result"}

    profitable = [r for r in rows if (r["total_return_pct"] or 0) > 0]
    breakeven: float | None = None
    for row in sorted(rows, key=lambda r: r["multiplier"]):
        if (row["total_return_pct"] or 0) <= 0:
            breakeven = row["multiplier"]
            break

    out: dict[str, Any] = {
        "available": True,
        "component": component,
        "baseline_round_trip_pct": round(base.round_trip_bps() * 1e-4 * 100, 4),
        "breakeven_multiplier": breakeven,
        "rows": rows,
    }

    if breakeven is None:
        out["verdict"] = (
            f"Still profitable at {max(r['multiplier'] for r in rows):g}x the assumed "
            f"{component}. The edge is not cost-bound over this range."
        )
    elif breakeven <= 1.0:
        out["verdict"] = (
            f"FRAGILE: unprofitable at {breakeven:g}x the assumed {component}, i.e. at or "
            "below the cost already configured. The result depends entirely on the cost "
            "assumption being optimistic."
        )
    elif breakeven <= 2.0:
        out["verdict"] = (
            f"FRAGILE: the edge disappears at {breakeven:g}x the assumed {component}. "
            "Since these costs are placeholders rather than a broker's schedule, that is "
            "well within the range of being wrong."
        )
    else:
        out["verdict"] = (
            f"The edge survives to {breakeven:g}x the assumed {component} and fails beyond "
            "it."
        )
    if not profitable:
        out["verdict"] = "Unprofitable at every cost level tested, including zero cost."
    return out


# --------------------------------------------------------------------------- #
# 4. Execution delay
# --------------------------------------------------------------------------- #


def execution_delay_sensitivity(
    session: Session,
    *,
    strategy: str,
    market: str,
    params: dict[str, Any],
    start: date,
    end: date,
    symbols: list[str] | None = None,
    capital: float | None = None,
) -> dict[str, Any]:
    """Approximate a one-bar-later fill by widening the assumed slippage.

    An honest caveat: this is a **proxy**, not a true delay simulation. Modelling a real
    extra bar of latency would need the engine to queue orders two bars out, which is a
    change to the execution model rather than a parameter. Widening slippage captures the
    cost of a worse fill but not the chance that the signal has reversed by then, so the
    true sensitivity is worse than this reports.
    """
    settings = get_settings()
    base = settings.costs_for(market)
    rows: list[dict[str, Any]] = []

    # Roughly: one extra bar of drift on a 1.5%-daily-volatility instrument.
    for label, extra_bps in (("no delay", 0.0), ("~1 bar", 25.0), ("~2 bars", 50.0)):
        config = MarketCostConfig(
            commission_bps=base.commission_bps,
            min_commission=base.min_commission,
            slippage_bps=base.slippage_bps + extra_bps,
            spread_bps=base.spread_bps,
        )
        metrics = _run(
            session, strategy_name=strategy, market=market, params=params,
            start=start, end=end, symbols=symbols, capital=capital,
            cost_model=CostModel(market=market.upper(), config=config),
        )
        if metrics is None:
            continue
        rows.append({"delay": label, "extra_slippage_bps": extra_bps, **_summary(metrics)})

    if not rows:
        return {"available": False, "reason": "no delay variant produced a result"}

    first = rows[0].get("total_return_pct") or 0
    last = rows[-1].get("total_return_pct") or 0
    out: dict[str, Any] = {
        "available": True,
        "rows": rows,
        "method": (
            "Approximated by widening assumed slippage, not by re-queuing orders a bar "
            "later. It captures the worse fill but not the signal decaying in the "
            "meantime, so real delay sensitivity is worse than shown."
        ),
    }
    if first > 0 and last <= 0:
        out["verdict"] = (
            "FRAGILE: the edge does not survive a delayed fill. A signal this short-lived "
            "is unlikely to be captured with retail execution."
        )
    else:
        out["verdict"] = f"Return moves from {first:.2f}% to {last:.2f}% across the range tested."
    return out


# --------------------------------------------------------------------------- #
# 5. Monte Carlo reshuffling
# --------------------------------------------------------------------------- #


def monte_carlo_reshuffle(
    trade_returns: list[float],
    *,
    n_simulations: int = 1000,
    seed: int = 0,
) -> dict[str, Any]:
    """Reorder the same trades many times and look at the drawdown distribution.

    Total return is invariant under reordering — the same multipliers compound to the same
    product — so this says nothing about return. What it does show is how much of the
    realised drawdown was **sequence luck**. If the actual drawdown sits at the optimistic
    edge of the distribution, the account happened to avoid a hole that the same trades in
    a different order would have produced.

    ``trade_returns`` are per-trade percentage returns.
    """
    if len(trade_returns) < 10:
        return {
            "available": False,
            "reason": f"only {len(trade_returns)} trades; reshuffling needs at least 10 "
            "to say anything about the distribution",
        }

    rng = random.Random(seed)
    multipliers = [1.0 + r / 100.0 for r in trade_returns]

    def drawdown_of(order: list[float]) -> float:
        # The starting balance is prepended so the account's opening value is the first
        # peak. Without it, a loss on the very first trade is not counted as a drawdown at
        # all, which understates the risk of exactly the orderings that begin badly.
        equity = np.concatenate([[1.0], np.cumprod(order)])
        series = pd.Series(
            equity,
            index=pd.date_range("2000-01-01", periods=len(equity), freq="D", tz="UTC"),
        )
        return float(max_drawdown(series)["max_drawdown_pct"] or 0.0)

    actual = drawdown_of(multipliers)
    simulated: list[float] = []
    for _ in range(n_simulations):
        shuffled = multipliers[:]
        rng.shuffle(shuffled)
        simulated.append(drawdown_of(shuffled))

    array = np.array(simulated)
    percentile_of_actual = float((array < actual).mean() * 100.0)

    out: dict[str, Any] = {
        "available": True,
        "n_simulations": n_simulations,
        "n_trades": len(trade_returns),
        "actual_max_drawdown_pct": round(actual, 4),
        "simulated_drawdown": {
            "p5": round(float(np.percentile(array, 5)), 4),
            "p25": round(float(np.percentile(array, 25)), 4),
            "median": round(float(np.median(array)), 4),
            "p75": round(float(np.percentile(array, 75)), 4),
            "p95": round(float(np.percentile(array, 95)), 4),
            "worst": round(float(array.min()), 4),
        },
        "actual_percentile": round(percentile_of_actual, 1),
        "note": (
            "Total return is unchanged by reordering, so this measures sequence risk only. "
            "It resamples the trades that were actually taken and says nothing about "
            "trades the strategy never took or regimes absent from the sample."
        ),
    }

    if percentile_of_actual > 70:
        out["verdict"] = (
            f"The realised drawdown was luckier than {percentile_of_actual:.0f}% of "
            "reorderings of the same trades. A different sequence would plausibly have "
            f"produced a drawdown as deep as {out['simulated_drawdown']['p5']:.1f}%, so "
            "plan for that rather than for what happened."
        )
    else:
        out["verdict"] = (
            f"The realised drawdown sits at the {percentile_of_actual:.0f}th percentile of "
            "reorderings, so it was not unusually kind."
        )
    return out


# --------------------------------------------------------------------------- #
# 6. Random trade removal
# --------------------------------------------------------------------------- #


def random_trade_removal(
    trade_returns: list[float],
    *,
    fractions: tuple[float, ...] = (0.05, 0.10, 0.20, 0.30),
    n_simulations: int = 300,
    seed: int = 0,
) -> dict[str, Any]:
    """Drop a random fraction of trades and see what survives.

    Tests whether the result rests on a few trades. If removing 10% at random turns a
    profit into a loss a meaningful share of the time, then a handful of trades carried
    the strategy — and there is no reason to expect the next handful to behave the same.
    """
    if len(trade_returns) < 20:
        return {
            "available": False,
            "reason": f"only {len(trade_returns)} trades; removal testing needs at least 20",
        }

    rng = random.Random(seed)
    multipliers = [1.0 + r / 100.0 for r in trade_returns]
    baseline = float(np.prod(multipliers) - 1.0) * 100.0
    rows: list[dict[str, Any]] = []

    for fraction in fractions:
        keep = max(1, int(round(len(multipliers) * (1.0 - fraction))))
        results: list[float] = []
        for _ in range(n_simulations):
            sample = rng.sample(multipliers, keep)
            results.append(float(np.prod(sample) - 1.0) * 100.0)

        array = np.array(results)
        rows.append(
            {
                "fraction_removed": fraction,
                "trades_kept": keep,
                "median_return_pct": round(float(np.median(array)), 4),
                "p5_return_pct": round(float(np.percentile(array, 5)), 4),
                "p95_return_pct": round(float(np.percentile(array, 95)), 4),
                "share_unprofitable": round(float((array <= 0).mean()), 4),
            }
        )

    out: dict[str, Any] = {
        "available": True,
        "baseline_return_pct": round(baseline, 4),
        "n_simulations": n_simulations,
        "rows": rows,
        "note": (
            "Compounded per-trade returns, so the figures ignore position sizing and the "
            "time spent in cash. Useful for concentration, not as a return estimate."
        ),
    }

    ten_percent = next((r for r in rows if abs(r["fraction_removed"] - 0.10) < 1e-9), None)

    # Two conditions, because either alone misses a real case.
    #
    # `share_unprofitable` catches a result carried by *several* trades. It cannot catch a
    # single load-bearing trade: with one outlier out of N, removing fraction f drops it
    # about f of the time, so at f=0.10 the share can never exceed roughly 10% however
    # concentrated the result is. A threshold above that is mathematically unreachable.
    #
    # The 5th percentile catches exactly that case: if the unluckiest 5% of removals turns
    # a profit into a loss, then dropping a handful of specific trades is enough to undo
    # the result, which is the definition of concentration.
    frequently_negative = bool(
        ten_percent and baseline > 0 and ten_percent["share_unprofitable"] > 0.15
    )
    tail_negative = bool(
        ten_percent and baseline > 0 and (ten_percent["p5_return_pct"] or 0) < 0
    )

    if frequently_negative:
        out["verdict"] = (
            f"FRAGILE: removing 10% of trades at random turns the result negative "
            f"{ten_percent['share_unprofitable']:.0%} of the time. Several trades are "
            "carrying this."
        )
    elif tail_negative:
        out["verdict"] = (
            f"FRAGILE: in the unluckiest 5% of removals the result falls to "
            f"{ten_percent['p5_return_pct']:.1f}%, from a baseline of {baseline:.1f}%. "
            "Dropping a few specific trades is enough to undo the result, so it rests on a "
            "small number of them."
        )
    elif baseline > 0:
        out["verdict"] = (
            "The result survives random trade removal, so it is not resting on a handful "
            "of outliers."
        )
    else:
        out["verdict"] = "Baseline is unprofitable, so removal testing adds nothing."
    return out


# --------------------------------------------------------------------------- #
# Suite
# --------------------------------------------------------------------------- #


def run_robustness_suite(
    session: Session,
    *,
    market: str,
    strategy: str = "trend_momentum",
    params: dict[str, Any] | None = None,
    space: ParameterSpace | None = None,
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
    capital: float | None = None,
    weights: ObjectiveWeights | None = None,
    include_parameter_sensitivity: bool = True,
) -> RobustnessReport:
    """Run every check against one configuration.

    Defaults to the TRAIN window: robustness testing is part of deciding whether to
    believe a configuration, which happens before validation is consulted.
    """
    from app.backtesting.runner import resolve_window
    from app.optimization.space import DEFAULT_TREND_MOMENTUM_SPACE

    search_space = space or DEFAULT_TREND_MOMENTUM_SPACE
    window = resolve_window("train")
    window_start = start or window.start
    window_end = end or window.end
    configuration = params or build_strategy(strategy).params.to_dict()

    report = RobustnessReport(market=market.upper(), strategy=strategy, params=configuration)
    report.caveats = [
        "Every check resamples one historical path. None of them explores regimes absent "
        "from the sample, trades the strategy never took, or the possibility that this "
        "whole period was unusual.",
        "A configuration can pass all of these and still fail. They detect fragility; "
        "they cannot establish robustness.",
        "Transaction costs are placeholders, so the cost-sensitivity breakeven is the "
        "most decision-relevant figure here.",
    ]

    baseline = _run(
        session, strategy_name=strategy, market=report.market, params=configuration,
        start=window_start, end=window_end, symbols=symbols, capital=capital,
    )
    if baseline is None:
        report.verdicts.append("Baseline backtest failed; no check could be run.")
        return report

    report.baseline = _summary(baseline)
    trade_returns: list[float] = []

    try:
        strategy_obj = build_strategy(strategy, configuration)
        run = run_backtest(
            session, strategy_obj, report.market, split="full",
            start=window_start, end=window_end, symbols=symbols,
            initial_capital=capital, include_benchmark=False, label="robustness baseline",
        )
        trade_returns = [t.pnl_pct for t in run.trades]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not collect trade returns: %s", exc)

    if include_parameter_sensitivity:
        report.checks["parameter_sensitivity"] = parameter_sensitivity(
            session, strategy=strategy, market=report.market, params=configuration,
            space=search_space, start=window_start, end=window_end, baseline=baseline,
            symbols=symbols, capital=capital, weights=weights,
        )

    report.checks["commission_sensitivity"] = cost_sensitivity(
        session, strategy=strategy, market=report.market, params=configuration,
        start=window_start, end=window_end, component="commission",
        symbols=symbols, capital=capital,
    )
    report.checks["slippage_sensitivity"] = cost_sensitivity(
        session, strategy=strategy, market=report.market, params=configuration,
        start=window_start, end=window_end, component="slippage",
        symbols=symbols, capital=capital,
    )
    report.checks["execution_delay"] = execution_delay_sensitivity(
        session, strategy=strategy, market=report.market, params=configuration,
        start=window_start, end=window_end, symbols=symbols, capital=capital,
    )
    report.checks["monte_carlo_reshuffle"] = monte_carlo_reshuffle(trade_returns)
    report.checks["random_trade_removal"] = random_trade_removal(trade_returns)

    for name, check in report.checks.items():
        verdict = check.get("verdict")
        if verdict:
            report.verdicts.append(f"{name}: {verdict}")

    if report.is_fragile:
        report.verdicts.insert(
            0,
            "FRAGILE OVERALL: at least one check indicates the result depends on "
            "conditions unlikely to repeat. Treat the backtest as a hypothesis that "
            "failed its stress test, not as a measurement.",
        )
    else:
        report.verdicts.insert(
            0,
            "No check flagged fragility. That is the absence of a negative finding, not "
            "evidence the strategy works.",
        )
    return report
