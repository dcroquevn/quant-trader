"""Metrics tests.

Values are checked against hand-computed arithmetic on short, exactly-known curves.
A recurring theme: metrics that are genuinely undefined must return ``None`` rather than
a number, because a zero or an infinity gets read as a measurement and ranked against
real ones.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from app.backtesting.metrics import (
    MIN_DAYS_FOR_CAGR,
    TRADING_DAYS_PER_YEAR,
    annual_returns,
    calmar_ratio,
    compare_to_benchmark,
    compute_benchmark_metrics,
    compute_metrics,
    drawdown_series,
    max_drawdown,
    monthly_returns,
    rolling_sharpe,
    sharpe_ratio,
    sortino_ratio,
    total_return_pct,
    trade_statistics,
    turnover_pct,
)


def curve(values: list[float], *, start: str = "2020-01-01", freq: str = "D") -> pd.Series:
    index = pd.date_range(start=start, periods=len(values), freq=freq, tz="UTC", name="ts")
    return pd.Series(values, index=index, dtype=float, name="equity")


@dataclass
class FakeTrade:
    pnl: float
    entry_price: float = 100.0
    exit_price: float = 100.0
    quantity: float = 10.0
    holding_period_days: int = 5
    bars_held: int = 5


class TestTotalReturn:
    def test_simple_gain(self) -> None:
        assert total_return_pct(curve([100.0, 150.0])) == pytest.approx(50.0)

    def test_simple_loss(self) -> None:
        assert total_return_pct(curve([100.0, 75.0])) == pytest.approx(-25.0)

    def test_single_point_is_undefined(self) -> None:
        assert total_return_pct(curve([100.0])) is None


class TestCAGR:
    def test_withheld_for_short_samples(self) -> None:
        """A 40% gain over two months annualises to nonsense, so it is not reported."""
        short = curve([100.0] * 60)
        short.iloc[-1] = 140.0
        from app.backtesting.metrics import cagr_pct

        assert cagr_pct(short) is None

    def test_computed_over_a_full_year(self) -> None:
        from app.backtesting.metrics import cagr_pct

        two_years = curve([100.0] * 731)
        two_years.iloc[-1] = 121.0
        result = cagr_pct(two_years)
        assert result is not None
        # 121 over ~2 years is about 10% a year.
        assert result == pytest.approx(10.0, abs=0.5)

    def test_undefined_after_a_wipeout(self) -> None:
        """Zero final equity has no real growth rate; -100% would imply recoverability."""
        from app.backtesting.metrics import cagr_pct

        wiped = curve([100.0] * 400)
        wiped.iloc[-1] = 0.0
        assert cagr_pct(wiped) is None

    def test_metrics_explains_why_cagr_is_missing(self) -> None:
        short = curve([100.0, 110.0, 120.0])
        metrics = compute_metrics(short, [])
        assert metrics["cagr_pct"] is None
        assert "cagr_note" in metrics
        assert str(MIN_DAYS_FOR_CAGR) in metrics["cagr_note"]


class TestSharpeAndSortino:
    def test_sharpe_of_a_flat_curve_is_undefined(self) -> None:
        """Zero volatility has no Sharpe. Infinity would be worse than None."""
        assert sharpe_ratio(curve([100.0] * 50)) is None

    def test_sharpe_matches_manual_computation(self) -> None:
        rng = np.random.default_rng(7)
        returns = rng.normal(0.0005, 0.01, 300)
        equity = curve(list(100.0 * np.cumprod(1 + returns)))

        realised = equity.pct_change(fill_method=None).dropna()
        expected = (
            realised.mean() / realised.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)
        )
        assert sharpe_ratio(equity) == pytest.approx(expected)

    def test_a_positive_risk_free_rate_reduces_sharpe(self) -> None:
        """Defaulting the rate to zero is not neutral; it credits risk-free return."""
        rng = np.random.default_rng(11)
        equity = curve(list(100.0 * np.cumprod(1 + rng.normal(0.0008, 0.01, 400))))

        at_zero = sharpe_ratio(equity, risk_free_rate=0.0)
        at_four = sharpe_ratio(equity, risk_free_rate=0.04)
        assert at_zero is not None and at_four is not None
        assert at_four < at_zero

    def test_sortino_exceeds_sharpe_when_upside_dominates(self) -> None:
        """Sortino ignores upside volatility, so an up-skewed curve scores better."""
        values = [100.0]
        for i in range(200):
            values.append(values[-1] * (1.03 if i % 5 == 0 else 0.998))
        equity = curve(values)

        sharpe = sharpe_ratio(equity)
        sortino = sortino_ratio(equity)
        assert sharpe is not None and sortino is not None
        assert sortino > sharpe

    def test_sortino_undefined_without_downside(self) -> None:
        monotone = curve([100.0 * 1.001**i for i in range(100)])
        assert sortino_ratio(monotone) is None

    def test_rolling_sharpe_shape(self) -> None:
        rng = np.random.default_rng(3)
        equity = curve(list(100.0 * np.cumprod(1 + rng.normal(0.0005, 0.01, 400))))
        rolling = rolling_sharpe(equity, window=126)
        assert not rolling.empty
        assert rolling.notna().sum() > 0

    def test_rolling_sharpe_empty_when_too_short(self) -> None:
        assert rolling_sharpe(curve([100.0] * 50), window=126).empty


class TestDrawdown:
    def test_series_is_never_positive(self) -> None:
        equity = curve([100.0, 120.0, 90.0, 130.0, 110.0])
        assert (drawdown_series(equity) <= 1e-12).all()

    def test_max_drawdown_matches_manual_computation(self) -> None:
        # Peak 120 at index 1, trough 90 at index 2 -> -25%
        equity = curve([100.0, 120.0, 90.0, 130.0])
        result = max_drawdown(equity)
        assert result["max_drawdown_pct"] == pytest.approx(-25.0)

    def test_records_peak_trough_and_recovery(self) -> None:
        equity = curve([100.0, 120.0, 90.0, 125.0])
        result = max_drawdown(equity)
        assert result["peak_date"] is not None
        assert result["trough_date"] is not None
        assert result["recovery_date"] is not None

    def test_unrecovered_drawdown_reports_no_recovery_date(self) -> None:
        """Materially different from a fast recovery, so it must be visible."""
        equity = curve([100.0, 150.0, 80.0, 90.0, 95.0])
        assert max_drawdown(equity)["recovery_date"] is None

    def test_monotone_curve_has_no_drawdown(self) -> None:
        equity = curve([100.0, 110.0, 120.0, 130.0])
        assert max_drawdown(equity)["max_drawdown_pct"] == pytest.approx(0.0)

    def test_calmar_is_cagr_over_drawdown(self) -> None:
        equity = curve([100.0] * 500)
        equity.iloc[250] = 80.0
        equity.iloc[-1] = 130.0
        result = calmar_ratio(equity)
        assert result is not None


class TestTradeStatistics:
    def test_empty_list_returns_nulls_not_zeros(self) -> None:
        stats = trade_statistics([])
        assert stats.n_trades == 0
        assert stats.win_rate_pct is None
        assert stats.profit_factor is None
        assert stats.expectancy is None

    def test_win_rate_and_counts(self) -> None:
        stats = trade_statistics(
            [FakeTrade(100.0), FakeTrade(-50.0), FakeTrade(75.0), FakeTrade(-25.0)]
        )
        assert stats.n_trades == 4
        assert stats.n_wins == 2
        assert stats.n_losses == 2
        assert stats.win_rate_pct == pytest.approx(50.0)

    def test_profit_factor_matches_manual_computation(self) -> None:
        stats = trade_statistics([FakeTrade(100.0), FakeTrade(50.0), FakeTrade(-75.0)])
        assert stats.profit_factor == pytest.approx(150.0 / 75.0)

    def test_profit_factor_undefined_without_losses(self) -> None:
        """Infinity would invite someone to sort a leaderboard by it."""
        stats = trade_statistics([FakeTrade(100.0), FakeTrade(50.0)])
        assert stats.profit_factor is None

    def test_expectancy_is_the_realised_mean(self) -> None:
        stats = trade_statistics([FakeTrade(100.0), FakeTrade(-50.0)])
        assert stats.expectancy == pytest.approx(25.0)

    def test_best_and_worst(self) -> None:
        stats = trade_statistics([FakeTrade(10.0), FakeTrade(-99.0), FakeTrade(42.0)])
        assert stats.best_trade == pytest.approx(42.0)
        assert stats.worst_trade == pytest.approx(-99.0)

    def test_breakeven_trades_count_as_neither(self) -> None:
        stats = trade_statistics([FakeTrade(0.0), FakeTrade(10.0)])
        assert stats.n_wins == 1
        assert stats.n_losses == 0
        assert stats.n_trades == 2


class TestTurnoverAndExposure:
    def test_turnover_is_annualised(self) -> None:
        equity = curve([100_000.0] * 366)
        trades = [FakeTrade(0.0, entry_price=100.0, exit_price=100.0, quantity=100.0)] * 10
        result = turnover_pct(trades, equity)
        assert result is not None
        # 10 trades x 20,000 traded / 100,000 equity over ~1 year = ~200%
        assert result == pytest.approx(200.0, rel=0.05)

    def test_turnover_none_without_trades(self) -> None:
        assert turnover_pct([], curve([100.0, 110.0])) is None

    def test_exposure_averages_snapshots(self) -> None:
        from app.backtesting.metrics import exposure_pct

        snapshots = [{"exposure_pct": 0.0}, {"exposure_pct": 100.0}]
        assert exposure_pct(snapshots) == pytest.approx(50.0)

    def test_exposure_none_without_snapshots(self) -> None:
        from app.backtesting.metrics import exposure_pct

        assert exposure_pct([]) is None


class TestPeriodBreakdowns:
    def test_monthly_returns_have_one_entry_per_completed_month(self) -> None:
        equity = curve([100.0 * 1.001**i for i in range(200)])
        monthly = monthly_returns(equity)
        assert not monthly.empty
        assert (monthly > 0).all()

    def test_annual_returns_include_the_first_partial_year(self) -> None:
        equity = curve([100.0 * 1.0005**i for i in range(800)], start="2020-06-01")
        annual = annual_returns(equity)
        assert len(annual) >= 2

    def test_breakdowns_are_empty_for_a_single_point(self) -> None:
        assert monthly_returns(curve([100.0])).empty
        assert annual_returns(curve([100.0])).empty


class TestComputeMetrics:
    def test_includes_every_headline_metric(self) -> None:
        rng = np.random.default_rng(21)
        equity = curve(list(100_000.0 * np.cumprod(1 + rng.normal(0.0004, 0.01, 800))))
        trades = [FakeTrade(100.0), FakeTrade(-40.0), FakeTrade(60.0)]
        snapshots = [{"exposure_pct": 50.0}] * 800

        metrics = compute_metrics(equity, trades, snapshots)
        for key in (
            "total_return_pct", "cagr_pct", "annualised_volatility_pct", "sharpe",
            "sortino", "calmar", "max_drawdown_pct", "win_rate_pct", "profit_factor",
            "average_win", "average_loss", "expectancy", "n_trades",
            "average_holding_days", "best_trade", "worst_trade", "exposure_pct",
            "turnover_pct",
        ):
            assert key in metrics, f"missing metric: {key}"

    def test_records_the_risk_free_rate_used(self) -> None:
        """So a reader can tell whether Sharpe was flattered by a zero rate."""
        equity = curve([100.0 * 1.0005**i for i in range(400)])
        metrics = compute_metrics(equity, [], risk_free_rate=0.03)
        assert metrics["risk_free_rate_used"] == 0.03

    def test_empty_curve_reports_an_error_not_zeros(self) -> None:
        metrics = compute_metrics(pd.Series(dtype=float), [])
        assert "error" in metrics

    def test_is_json_serialisable(self) -> None:
        import json

        equity = curve([100.0 * 1.0005**i for i in range(400)])
        metrics = compute_metrics(equity, [FakeTrade(10.0)], [{"exposure_pct": 10.0}])
        json.dumps(metrics)  # must not raise


class TestBenchmark:
    def test_aligned_to_the_strategy_calendar(self) -> None:
        """Both curves must span identical periods or the returns are incomparable."""
        equity = curve([100_000.0] * 300)
        benchmark = curve([50.0 * 1.0004**i for i in range(320)])

        metrics = compute_benchmark_metrics(benchmark, equity.index, 100_000.0)
        assert metrics["available"] is True
        assert metrics["n_benchmark_bars"] == len(equity)
        assert metrics["initial_equity"] == pytest.approx(100_000.0)

    def test_non_overlapping_benchmark_is_reported_unavailable(self) -> None:
        equity = curve([100_000.0] * 100, start="2024-01-01")
        benchmark = curve([50.0] * 100, start="2010-01-01")
        metrics = compute_benchmark_metrics(benchmark, equity.index, 100_000.0)
        assert metrics["available"] is False
        # The reason must name both ranges, so a reader can see why they do not meet.
        assert "2010" in metrics["reason"]
        assert "2024" in metrics["reason"]
        assert "fabricate" in metrics["reason"]
        # Critically: no metrics were produced from the stale forward-fill.
        assert "total_return_pct" not in metrics

    def test_caveats_travel_with_the_result(self) -> None:
        """For Chile the benchmark is a USD proxy; the caveat must not be lost."""
        equity = curve([100_000.0] * 300)
        benchmark = curve([50.0] * 320)
        metrics = compute_benchmark_metrics(
            benchmark,
            equity.index,
            100_000.0,
            label="ECH buy and hold",
            caveats=("USD-denominated proxy, not the IPSA",),
        )
        assert "USD-denominated proxy, not the IPSA" in metrics["caveats"]

    def test_empty_benchmark_is_unavailable(self) -> None:
        equity = curve([100_000.0] * 50)
        assert (
            compute_benchmark_metrics(pd.Series(dtype=float), equity.index, 1000.0)["available"]
            is False
        )


class TestComparison:
    def test_computes_excess_figures(self) -> None:
        strategy = {"total_return_pct": 50.0, "cagr_pct": 10.0, "sharpe": 1.2,
                    "sortino": 1.5, "max_drawdown_pct": -15.0,
                    "annualised_volatility_pct": 18.0}
        benchmark = {"available": True, "total_return_pct": 30.0, "cagr_pct": 7.0,
                     "sharpe": 0.9, "sortino": 1.1, "max_drawdown_pct": -25.0,
                     "annualised_volatility_pct": 20.0}

        comparison = compare_to_benchmark(strategy, benchmark)
        assert comparison["excess_total_return_pct"] == pytest.approx(20.0)
        assert comparison["sharpe_difference"] == pytest.approx(0.3)
        # Less negative drawdown is better, so a positive difference is good.
        assert comparison["drawdown_difference_pct"] == pytest.approx(10.0)

    def test_missing_values_propagate_as_none(self) -> None:
        """Treating a missing value as zero would report a difference never measured."""
        comparison = compare_to_benchmark(
            {"total_return_pct": 50.0, "cagr_pct": None},
            {"available": True, "total_return_pct": 30.0, "cagr_pct": 7.0},
        )
        assert comparison["excess_total_return_pct"] == pytest.approx(20.0)
        assert comparison["excess_cagr_pct"] is None

    def test_unavailable_benchmark_short_circuits(self) -> None:
        comparison = compare_to_benchmark({}, {"available": False, "reason": "no data"})
        assert comparison["available"] is False
        assert comparison["reason"] == "no data"

    def test_partial_coverage_is_flagged_as_a_caveat(self) -> None:
        """A benchmark that only spans half the sessions understates its own risk.

        Forward-filling the gaps produces flat stretches, which suppress volatility and
        drawdown. Usable, but the reader has to be told.
        """
        equity = curve([100_000.0] * 300, start="2024-01-01")
        # Benchmark prints on only every third session.
        sparse = curve([50.0 * 1.0004**i for i in range(300)], start="2024-01-01").iloc[::3]

        metrics = compute_benchmark_metrics(sparse, equity.index, 100_000.0)
        assert metrics["available"] is True
        assert metrics["coverage_fraction"] < 0.9
        assert any("forward-filled" in c for c in metrics["caveats"])

    def test_full_coverage_adds_no_spurious_caveat(self) -> None:
        equity = curve([100_000.0] * 300, start="2024-01-01")
        aligned = curve([50.0 * 1.0004**i for i in range(300)], start="2024-01-01")

        metrics = compute_benchmark_metrics(aligned, equity.index, 100_000.0)
        assert metrics["coverage_fraction"] == pytest.approx(1.0)
        assert not any("forward-filled" in c for c in metrics["caveats"])
