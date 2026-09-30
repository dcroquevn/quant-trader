"""Walk-forward window construction and the robustness checks.

The statistical functions (Monte Carlo, trade removal) are tested against hand-constructed
inputs where the right answer is known by arithmetic rather than by running the strategy.
Window construction is tested against the calendar.

The property that matters most for walk-forward is **time ordering**: every test window
must begin strictly after its own training window ends. If that ever breaks, the whole
analysis becomes an ordinary in-sample backtest wearing a different name, and nothing in
the output would look wrong.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from app.optimization.robustness import (
    FRAGILE_SENSITIVITY_THRESHOLD,
    monte_carlo_reshuffle,
    random_trade_removal,
)
from app.optimization.walkforward import build_windows


# --------------------------------------------------------------------------- #
# Window construction
# --------------------------------------------------------------------------- #


class TestBuildWindows:
    def test_produces_the_expected_number_of_windows(self) -> None:
        windows = build_windows(date(2016, 1, 1), date(2026, 1, 1), train_years=4, test_years=1)
        # Train 2016-19 -> test 2020; rolling by a year until the test period runs out.
        assert len(windows) == 6
        assert windows[0].train_start == date(2016, 1, 1)
        assert windows[0].test_start == date(2020, 1, 1)

    def test_every_test_window_starts_after_its_training_window_ends(self) -> None:
        """The property the entire analysis rests on.

        If a test window overlapped its own training window, the parameters would have
        been chosen with knowledge of the data they are being judged on, and nothing in
        the output would reveal it.
        """
        windows = build_windows(date(2010, 1, 1), date(2026, 1, 1), train_years=3, test_years=1)
        assert windows
        for window in windows:
            assert window.train_end < window.test_start
            assert window.train_start < window.train_end
            assert window.test_start <= window.test_end

    def test_windows_roll_rather_than_expand(self) -> None:
        """A fixed-length training window has to keep working as the regime changes.

        An expanding window is increasingly dominated by old data and quietly gets easier
        over time, which flatters the later windows.
        """
        windows = build_windows(date(2010, 1, 1), date(2026, 1, 1), train_years=4, test_years=1)
        lengths = {(w.train_end - w.train_start).days for w in windows}
        # Every training window spans the same number of days, give or take a leap year.
        assert max(lengths) - min(lengths) <= 2

    def test_step_years_controls_the_overlap(self) -> None:
        yearly = build_windows(date(2010, 1, 1), date(2026, 1, 1), train_years=4, test_years=1, step_years=1)
        biennial = build_windows(date(2010, 1, 1), date(2026, 1, 1), train_years=4, test_years=1, step_years=2)
        assert len(biennial) < len(yearly)

    def test_windows_are_numbered_from_one(self) -> None:
        windows = build_windows(date(2016, 1, 1), date(2024, 1, 1))
        assert [w.index for w in windows] == list(range(1, len(windows) + 1))

    def test_a_too_short_span_raises_rather_than_returning_nothing(self) -> None:
        """An empty list would read as "no problems found"."""
        with pytest.raises(ValueError, match="too short"):
            build_windows(date(2024, 1, 1), date(2024, 6, 1), train_years=4, test_years=1)

    def test_rejects_an_inverted_span(self) -> None:
        with pytest.raises(ValueError, match="must be after"):
            build_windows(date(2026, 1, 1), date(2016, 1, 1))

    @pytest.mark.parametrize(
        ("train", "test", "step"),
        [(0, 1, 1), (4, 0, 1), (4, 1, 0)],
    )
    def test_rejects_non_positive_periods(self, train: int, test: int, step: int) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            build_windows(
                date(2010, 1, 1), date(2026, 1, 1),
                train_years=train, test_years=test, step_years=step,
            )

    def test_a_short_final_window_is_dropped(self) -> None:
        """Reporting a three-month "year" beside full years would distort every average.

        This span fits one full window; the next window's test period would be only about
        90 days, so it is dropped rather than reported alongside a full year.
        """
        windows = build_windows(
            date(2016, 1, 1), date(2021, 4, 1), train_years=4, test_years=1
        )
        assert len(windows) == 1
        for window in windows:
            assert (window.test_end - window.test_start).days >= 180

    def test_a_substantial_final_window_is_kept_truncated(self) -> None:
        windows = build_windows(
            date(2016, 1, 1), date(2020, 10, 1), train_years=4, test_years=1
        )
        assert windows
        assert windows[-1].test_end == date(2020, 10, 1)

    def test_leap_day_start_does_not_crash(self) -> None:
        windows = build_windows(date(2016, 2, 29), date(2026, 1, 1), train_years=1, test_years=1)
        assert windows

    def test_describe_and_dict_are_usable(self) -> None:
        import json

        window = build_windows(date(2016, 1, 1), date(2024, 1, 1))[0]
        assert "train" in window.describe()
        json.dumps(window.to_dict())


# --------------------------------------------------------------------------- #
# Monte Carlo reshuffling
# --------------------------------------------------------------------------- #


class TestMonteCarloReshuffle:
    def test_refuses_too_small_a_sample(self) -> None:
        result = monte_carlo_reshuffle([1.0, -1.0, 2.0])
        assert result["available"] is False
        assert "at least 10" in result["reason"]

    def test_reports_the_drawdown_distribution(self) -> None:
        returns = [5.0, -3.0, 4.0, -8.0, 6.0, -2.0, 7.0, -5.0, 3.0, -1.0, 9.0, -4.0]
        result = monte_carlo_reshuffle(returns, n_simulations=200, seed=1)

        assert result["available"] is True
        assert result["n_trades"] == len(returns)
        distribution = result["simulated_drawdown"]
        # Percentiles must be ordered; drawdowns are negative, so worst is the lowest.
        assert distribution["worst"] <= distribution["p5"] <= distribution["median"]
        assert distribution["median"] <= distribution["p95"] <= 0

    def test_is_reproducible(self) -> None:
        returns = [3.0, -2.0, 5.0, -4.0, 1.0, -1.0, 6.0, -3.0, 2.0, -5.0, 4.0]
        first = monte_carlo_reshuffle(returns, n_simulations=100, seed=42)
        second = monte_carlo_reshuffle(returns, n_simulations=100, seed=42)
        assert first["simulated_drawdown"] == second["simulated_drawdown"]

    def test_all_winners_have_no_drawdown_in_any_order(self) -> None:
        """A sanity check on the mechanism: with no losses, reordering cannot create one."""
        result = monte_carlo_reshuffle([2.0] * 15, n_simulations=50, seed=0)
        assert result["actual_max_drawdown_pct"] == pytest.approx(0.0)
        assert result["simulated_drawdown"]["worst"] == pytest.approx(0.0)

    def test_states_that_return_is_unchanged_by_reordering(self) -> None:
        """The note exists so nobody reads this as a return distribution."""
        result = monte_carlo_reshuffle([3.0, -2.0] * 8, n_simulations=50)
        assert "Total return is unchanged" in result["note"]

    @pytest.mark.parametrize(
        ("label", "returns"),
        [
            ("losses last", [5.0] * 10 + [-6.0] * 6),
            ("losses first", [-6.0] * 6 + [5.0] * 10),
        ],
    )
    def test_bunched_losses_are_the_unluckiest_orderings(
        self, label: str, returns: list[float]
    ) -> None:
        """Consecutive losses produce one deep hole wherever they sit in the sequence.

        Both of these sit near the *worst* of the distribution, because a random shuffle
        breaks the losing run up. An earlier version of this test assumed "losses first"
        would be kind, which is wrong: what matters is whether the losses are adjacent, not
        where they are.
        """
        result = monte_carlo_reshuffle(returns, n_simulations=400, seed=3)
        assert result["actual_percentile"] < 30, label

    def test_interleaved_losses_are_the_kindest_ordering(self) -> None:
        """Spreading losses between wins never lets a drawdown compound."""
        returns = [5.0, 5.0, -6.0] * 5
        result = monte_carlo_reshuffle(returns, n_simulations=400, seed=3)
        assert result["actual_percentile"] > 50

    def test_a_first_trade_loss_counts_as_a_drawdown(self) -> None:
        """The opening balance is the first peak.

        Without prepending it, cumprod starts at the first trade's result and a loss there
        is invisible — understating exactly the orderings that begin badly.
        """
        result = monte_carlo_reshuffle([-10.0] + [1.0] * 14, n_simulations=50, seed=0)
        assert result["actual_max_drawdown_pct"] <= -9.5


# --------------------------------------------------------------------------- #
# Random trade removal
# --------------------------------------------------------------------------- #


class TestRandomTradeRemoval:
    def test_refuses_too_small_a_sample(self) -> None:
        result = random_trade_removal([1.0] * 10)
        assert result["available"] is False
        assert "at least 20" in result["reason"]

    def test_reports_a_row_per_fraction(self) -> None:
        returns = [2.0, -1.0] * 15
        result = random_trade_removal(returns, fractions=(0.1, 0.2), n_simulations=50, seed=1)
        assert result["available"] is True
        assert [r["fraction_removed"] for r in result["rows"]] == [0.1, 0.2]
        for row in result["rows"]:
            assert row["trades_kept"] < len(returns)
            assert row["p5_return_pct"] <= row["median_return_pct"] <= row["p95_return_pct"]

    def test_is_reproducible(self) -> None:
        returns = [3.0, -2.0] * 15
        first = random_trade_removal(returns, n_simulations=40, seed=7)
        second = random_trade_removal(returns, n_simulations=40, seed=7)
        assert first["rows"] == second["rows"]

    def test_detects_a_result_carried_by_one_outlier(self) -> None:
        """The check's whole purpose: 24 small losses plus one huge win.

        Removing 10% at random drops that one trade often enough that the result should be
        flagged as concentrated.
        """
        returns = [-1.0] * 24 + [400.0]
        result = random_trade_removal(returns, n_simulations=400, seed=2)
        assert result["baseline_return_pct"] > 0

        ten = next(r for r in result["rows"] if abs(r["fraction_removed"] - 0.10) < 1e-9)
        # With one outlier out of 25, removing 10% drops it about 10% of the time, so the
        # frequency alone can never clear a higher bar. The 5th percentile is what detects
        # this case, and it must be negative.
        assert ten["p5_return_pct"] < 0
        assert "FRAGILE" in result["verdict"]
        assert "small number" in result["verdict"] or "carrying" in result["verdict"]

    def test_a_broadly_based_result_is_not_flagged(self) -> None:
        returns = [1.5] * 20 + [-0.5] * 10
        result = random_trade_removal(returns, n_simulations=200, seed=5)
        assert result["baseline_return_pct"] > 0
        assert "FRAGILE" not in result["verdict"]

    def test_an_unprofitable_baseline_says_so(self) -> None:
        result = random_trade_removal([-2.0] * 25, n_simulations=50)
        assert "Baseline is unprofitable" in result["verdict"]

    def test_notes_the_sizing_caveat(self) -> None:
        """Compounded per-trade returns ignore position sizing and time in cash."""
        result = random_trade_removal([1.0, -1.0] * 15, n_simulations=30)
        assert "position sizing" in result["note"]


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #


class TestThresholds:
    def test_fragility_threshold_is_a_majority(self) -> None:
        """The brief's "works only with very specific parameters" made measurable.

        A majority of losing neighbours is the point at which a profitable centre is a
        spike rather than a plateau.
        """
        assert FRAGILE_SENSITIVITY_THRESHOLD == 0.5

    def test_robustness_module_exports_every_check(self) -> None:
        """Section 20 of the brief lists six checks; all six must be reachable."""
        import app.optimization.robustness as module

        for name in (
            "parameter_sensitivity",
            "cost_sensitivity",
            "execution_delay_sensitivity",
            "monte_carlo_reshuffle",
            "random_trade_removal",
            "run_robustness_suite",
        ):
            assert hasattr(module, name), f"missing {name}"

    def test_execution_delay_documents_that_it_is_a_proxy(self) -> None:
        """Claiming to simulate latency when it widens slippage would be overselling it."""
        import inspect

        import app.optimization.robustness as module

        source = inspect.getsource(module.execution_delay_sensitivity)
        assert "proxy" in source.lower()
        assert "not a true delay simulation" in source.lower()
