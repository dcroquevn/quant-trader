"""Optimisation tests: parameter spaces, the objective function, and the leakage guard.

The most important test in this file is
``TestLeakageGuard::test_run_search_has_no_split_parameter``. Everything else here checks
arithmetic; that one checks that the search *cannot be asked* to read validation or test
data. A guard you can pass an argument to defeat is a suggestion.
"""

from __future__ import annotations

import inspect
import math

import pytest

from app.core.exceptions import DataLeakageError
from app.optimization.objective import (
    PRESETS,
    ObjectiveWeights,
    apply_fragility_penalty,
    score_result,
)
from app.optimization.space import (
    OVERFITTING_RISK_COMBINATIONS,
    DEFAULT_TREND_MOMENTUM_SPACE,
    ParameterAxis,
    ParameterSpace,
)


# --------------------------------------------------------------------------- #
# Parameter axes and spaces
# --------------------------------------------------------------------------- #


class TestParameterAxis:
    def test_rejects_an_empty_axis(self) -> None:
        with pytest.raises(ValueError, match="no values to search"):
            ParameterAxis("rsi_min", ())

    def test_rejects_duplicate_values(self) -> None:
        """A duplicated value silently wastes a grid slot and skews stability counts."""
        with pytest.raises(ValueError, match="duplicate values"):
            ParameterAxis("x", (1.0, 2.0, 1.0))

    def test_numeric_range_is_inclusive(self) -> None:
        axis = ParameterAxis.numeric("x", 1.0, 3.0, 1.0)
        assert axis.values == (1.0, 2.0, 3.0)

    def test_numeric_range_rounds_away_float_noise(self) -> None:
        """Without rounding, 0.1+0.2 produces a spurious extra grid point."""
        axis = ParameterAxis.numeric("x", 0.0, 0.5, 0.1)
        assert axis.values == (0.0, 0.1, 0.2, 0.3, 0.4, 0.5)

    def test_integer_range_produces_ints(self) -> None:
        axis = ParameterAxis.numeric("n", 10, 30, 10, integer=True)
        assert axis.values == (10, 20, 30)
        assert all(isinstance(v, int) for v in axis.values)

    def test_rejects_non_positive_step(self) -> None:
        with pytest.raises(ValueError, match="step must be positive"):
            ParameterAxis.numeric("x", 0.0, 1.0, 0.0)

    def test_rejects_inverted_range(self) -> None:
        with pytest.raises(ValueError, match="below start"):
            ParameterAxis.numeric("x", 5.0, 1.0, 1.0)


class TestParameterSpace:
    def space(self) -> ParameterSpace:
        return ParameterSpace(
            axes=(
                ParameterAxis("a", (1, 2, 3)),
                ParameterAxis("b", (10.0, 20.0)),
            )
        )

    def test_rejects_an_empty_space(self) -> None:
        with pytest.raises(ValueError, match="at least one axis"):
            ParameterSpace(axes=())

    def test_rejects_duplicate_axis_names(self) -> None:
        with pytest.raises(ValueError, match="Duplicate axis names"):
            ParameterSpace(axes=(ParameterAxis("a", (1,)), ParameterAxis("a", (2,))))

    def test_rejects_a_parameter_that_is_both_searched_and_fixed(self) -> None:
        """A fixed value would override every point the search tried, silently."""
        with pytest.raises(ValueError, match="both searched and fixed"):
            ParameterSpace(axes=(ParameterAxis("a", (1, 2)),), fixed={"a": 5})

    def test_grid_size_is_the_product(self) -> None:
        assert self.space().grid_size() == 6

    def test_grid_yields_every_combination_once(self) -> None:
        points = list(self.space().grid())
        assert len(points) == 6
        assert len({repr(sorted(p.items())) for p in points}) == 6

    def test_grid_includes_fixed_parameters(self) -> None:
        space = ParameterSpace(axes=(ParameterAxis("a", (1, 2)),), fixed={"c": 99})
        for point in space.grid():
            assert point["c"] == 99

    def test_sampling_is_reproducible(self) -> None:
        """An irreproducible optimisation result is an anecdote."""
        space = ParameterSpace(axes=(ParameterAxis("a", tuple(range(50))),))
        first = space.sample(10, seed=7)
        second = space.sample(10, seed=7)
        assert first == second
        assert space.sample(10, seed=8) != first

    def test_sampling_draws_without_replacement(self) -> None:
        space = ParameterSpace(axes=(ParameterAxis("a", tuple(range(100))),))
        points = space.sample(30, seed=1)
        assert len({repr(sorted(p.items())) for p in points}) == 30

    def test_oversampling_returns_the_full_grid(self) -> None:
        """Sampling 400 points from a space of 6 is just a slow grid search."""
        space = self.space()
        assert len(space.sample(400)) == space.grid_size()

    def test_rejects_a_zero_sample(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            self.space().sample(0)

    def test_neighbours_move_one_axis_at_a_time(self) -> None:
        space = self.space()
        point = {"a": 2, "b": 10.0}
        neighbours = space.neighbours(point)

        for neighbour in neighbours:
            differences = [k for k in point if neighbour[k] != point[k]]
            assert len(differences) == 1, "a neighbour changed more than one axis"

    def test_neighbours_respect_axis_bounds(self) -> None:
        space = self.space()
        # 'a' is at its minimum and 'b' at its minimum: only upward moves exist.
        neighbours = space.neighbours({"a": 1, "b": 10.0})
        assert all(n["a"] >= 1 and n["b"] >= 10.0 for n in neighbours)

    def test_neighbours_of_an_off_grid_point_are_empty(self) -> None:
        """Sensitivity analysis needs the point to be on the grid to have neighbours."""
        assert self.space().neighbours({"a": 99, "b": 99.0}) == []

    def test_overfitting_flag_tracks_the_threshold(self) -> None:
        small = ParameterSpace(axes=(ParameterAxis("a", (1, 2)),))
        assert small.is_overfitting_prone is False

        big = ParameterSpace(
            axes=(ParameterAxis("a", tuple(range(OVERFITTING_RISK_COMBINATIONS + 1))),)
        )
        assert big.is_overfitting_prone is True

    def test_describe_is_serialisable_and_states_the_risk(self) -> None:
        import json

        described = self.space().describe()
        json.dumps(described)
        assert described["grid_size"] == 6
        assert "overfitting_prone" in described
        assert described["overfitting_threshold"] == OVERFITTING_RISK_COMBINATIONS


class TestDefaultSpace:
    def test_covers_the_parameters_that_matter(self) -> None:
        names = set(DEFAULT_TREND_MOMENTUM_SPACE.names)
        assert {"rsi_min", "rsi_max", "min_relative_volume", "stop_atr_multiple"} <= names

    def test_is_deliberately_flagged_as_overfitting_prone(self) -> None:
        """The default demonstrates the warning rather than hiding behind a small grid."""
        assert DEFAULT_TREND_MOMENTUM_SPACE.is_overfitting_prone is True

    def test_includes_a_neutral_volume_setting(self) -> None:
        """1.0x lets the search tell us whether the volume filter earns its place."""
        axis = next(a for a in DEFAULT_TREND_MOMENTUM_SPACE.axes if a.name == "min_relative_volume")
        assert 1.0 in axis.values

    def test_every_point_builds_or_fails_cleanly(self) -> None:
        """A product space contains invalid combinations; they must raise, not corrupt."""
        from app.strategies.registry import build_strategy

        valid = invalid = 0
        for point in DEFAULT_TREND_MOMENTUM_SPACE.sample(40, seed=3):
            try:
                build_strategy("trend_momentum", point)
                valid += 1
            except ValueError:
                invalid += 1
        assert valid > 0
        assert valid + invalid == 40


# --------------------------------------------------------------------------- #
# Objective
# --------------------------------------------------------------------------- #

GOOD = {
    "total_return_pct": 80.0, "sharpe": 1.5, "sortino": 2.0,
    "max_drawdown_pct": -10.0, "annualised_volatility_pct": 9.0,
    "turnover_pct": 200.0, "n_trades": 200,
}
BAD = {
    "total_return_pct": -30.0, "sharpe": -0.5, "sortino": -0.6,
    "max_drawdown_pct": -45.0, "annualised_volatility_pct": 30.0,
    "turnover_pct": 1500.0, "n_trades": 200,
}


class TestObjectiveWeights:
    def test_rejects_a_negative_penalty_weight(self) -> None:
        """The component is already negative; a negative weight would reward drawdown."""
        with pytest.raises(ValueError, match="penalty weight"):
            ObjectiveWeights(max_drawdown=-1.0)

    def test_rejects_all_zero_weights(self) -> None:
        with pytest.raises(ValueError, match="score identically"):
            ObjectiveWeights(
                total_return=0, sharpe=0, sortino=0, max_drawdown=0,
                turnover=0, trade_count=0, volatility=0, fragility_penalty=0,
            )

    def test_rejects_unknown_weight_names(self) -> None:
        with pytest.raises(ValueError, match="Unknown objective weights"):
            ObjectiveWeights.from_dict({"shrpe": 1.0})

    def test_round_trips_through_dict(self) -> None:
        weights = ObjectiveWeights(sharpe=3.0, min_trades=50)
        assert ObjectiveWeights.from_dict(weights.to_dict()) == weights

    def test_default_does_not_put_return_first(self) -> None:
        """Optimising for return alone is the failure this objective exists to avoid."""
        weights = ObjectiveWeights()
        assert weights.sharpe > weights.total_return

    def test_every_preset_is_valid(self) -> None:
        for name, weights in PRESETS.items():
            assert isinstance(weights, ObjectiveWeights), name
            assert weights.total() > 0

    def test_stability_preset_emphasises_fragility(self) -> None:
        assert PRESETS["stability"].fragility_penalty > PRESETS["balanced"].fragility_penalty

    def test_capital_preservation_emphasises_drawdown(self) -> None:
        assert (
            PRESETS["capital_preservation"].max_drawdown > PRESETS["return"].max_drawdown
        )


class TestScoring:
    def test_a_good_result_outscores_a_bad_one(self) -> None:
        assert score_result(GOOD).value > score_result(BAD).value

    def test_components_are_bounded(self) -> None:
        """Normalisation is what makes the weights mean anything."""
        for metrics in (GOOD, BAD):
            for name, value in score_result(metrics).components.items():
                assert -1.0001 <= value <= 1.0001, f"{name} = {value} is out of range"

    def test_contributions_sum_to_the_value(self) -> None:
        score = score_result(GOOD)
        assert sum(score.contributions.values()) == pytest.approx(score.value)

    def test_drawdown_is_a_penalty(self) -> None:
        shallow = score_result({**GOOD, "max_drawdown_pct": -5.0})
        deep = score_result({**GOOD, "max_drawdown_pct": -50.0})
        assert shallow.value > deep.value
        assert deep.components["max_drawdown"] < 0

    def test_turnover_is_a_penalty(self) -> None:
        low = score_result({**GOOD, "turnover_pct": 50.0})
        high = score_result({**GOOD, "turnover_pct": 2000.0})
        assert low.value > high.value

    def test_volatility_is_a_penalty(self) -> None:
        calm = score_result({**GOOD, "annualised_volatility_pct": 5.0})
        wild = score_result({**GOOD, "annualised_volatility_pct": 50.0})
        assert calm.value > wild.value

    def test_an_extreme_return_cannot_dominate(self) -> None:
        """tanh squashing stops one spectacular sample from swamping every other term.

        A 3,000% return is not thirty times more informative than 100%; at that magnitude
        the sample is describing one trade.
        """
        big = score_result({**GOOD, "total_return_pct": 3000.0})
        moderate = score_result({**GOOD, "total_return_pct": 100.0})
        assert big.value > moderate.value
        assert big.value - moderate.value < 0.5

    def test_no_trades_is_heavily_penalised(self) -> None:
        """A configuration that never traded has measured nothing."""
        score = score_result({**GOOD, "n_trades": 0})
        assert score.components["trade_count"] == -1.0
        assert any("No trades" in note for note in score.notes)

    def test_too_few_trades_is_penalised_proportionally(self) -> None:
        weights = ObjectiveWeights(min_trades=100)
        few = score_result({**GOOD, "n_trades": 10}, weights)
        many = score_result({**GOOD, "n_trades": 100}, weights)
        assert few.components["trade_count"] < 0
        assert many.components["trade_count"] >= 0
        assert any("below the 100" in note for note in few.notes)

    def test_a_missing_metric_scores_zero_and_is_recorded(self) -> None:
        """Skipping it would silently re-weight everything else."""
        score = score_result({**GOOD, "sharpe": None})
        assert "sharpe" in score.missing
        assert score.components["sharpe"] == 0.0
        assert any("undefined" in note for note in score.notes)

    def test_non_finite_metrics_are_treated_as_missing(self) -> None:
        score = score_result({**GOOD, "sharpe": math.inf})
        assert "sharpe" in score.missing

    def test_empty_metrics_do_not_crash(self) -> None:
        score = score_result({})
        assert score.value < 0
        assert len(score.missing) >= 4

    def test_weights_change_the_ranking(self) -> None:
        """Otherwise the configurable objective is decoration."""
        high_return_wild = {
            "total_return_pct": 200.0, "sharpe": 0.6, "sortino": 0.8,
            "max_drawdown_pct": -50.0, "annualised_volatility_pct": 40.0,
            "turnover_pct": 800.0, "n_trades": 200,
        }
        steady = {
            "total_return_pct": 40.0, "sharpe": 1.8, "sortino": 2.4,
            "max_drawdown_pct": -8.0, "annualised_volatility_pct": 7.0,
            "turnover_pct": 150.0, "n_trades": 200,
        }
        by_return = PRESETS["return"]
        by_preservation = PRESETS["capital_preservation"]

        assert score_result(high_return_wild, by_return).value > score_result(steady, by_return).value
        assert (
            score_result(steady, by_preservation).value
            > score_result(high_return_wild, by_preservation).value
        )

    def test_score_is_serialisable(self) -> None:
        import json

        json.dumps(score_result(GOOD).to_dict())


class TestFragilityPenalty:
    def test_a_spike_is_penalised(self) -> None:
        """A point far above its neighbours is what an overfitted result looks like."""
        score = score_result(GOOD)
        before = score.value
        apply_fragility_penalty(score, [before - 3.0] * 4)

        assert score.value < before
        assert score.fragility is not None and score.fragility > 0
        assert score.contributions["fragility_penalty"] < 0

    def test_a_plateau_is_not_penalised(self) -> None:
        score = score_result(GOOD)
        before = score.value
        apply_fragility_penalty(score, [before] * 4)

        assert score.value == pytest.approx(before)
        assert score.fragility == pytest.approx(0.0)

    def test_scoring_below_neighbours_is_not_fragility(self) -> None:
        """Being worse than your neighbours is being bad, which the base score already says."""
        score = score_result(GOOD)
        before = score.value
        apply_fragility_penalty(score, [before + 5.0] * 4)
        assert score.value == pytest.approx(before)

    def test_no_neighbours_leaves_the_score_untouched(self) -> None:
        score = score_result(GOOD)
        before = score.value
        apply_fragility_penalty(score, [])
        assert score.value == pytest.approx(before)

    def test_a_zero_penalty_weight_disables_it(self) -> None:
        weights = ObjectiveWeights(fragility_penalty=0.0)
        score = score_result(GOOD, weights)
        before = score.value
        apply_fragility_penalty(score, [before - 10.0], weights)
        assert score.value == pytest.approx(before)

    def test_a_strong_spike_adds_an_explanatory_note(self) -> None:
        score = score_result(GOOD)
        apply_fragility_penalty(score, [score.value - 10.0] * 3)
        assert any("Fragile" in note for note in score.notes)


# --------------------------------------------------------------------------- #
# The leakage guard
# --------------------------------------------------------------------------- #


class TestLeakageGuard:
    def test_run_search_has_no_split_parameter(self) -> None:
        """The most important assertion in this file.

        A search that could be pointed at validation or test would eventually be pointed
        at them. The guard is the absence of the argument, so the test is that the
        argument stays absent.
        """
        from app.optimization.search import run_search

        parameters = set(inspect.signature(run_search).parameters)
        assert "split" not in parameters
        assert "finalising" not in parameters

    def test_run_search_hardcodes_train(self) -> None:
        """Source-level check: the split is a literal, not a variable."""
        from pathlib import Path

        import app.optimization.search as module

        source = Path(module.__file__).read_text(encoding="utf-8-sig")
        assert 'split="train"' in source
        assert 'resolve_window("train")' in source
        # And nothing in the search reaches for the other partitions.
        assert 'resolve_window("test")' not in source
        assert "finalising=True" not in source

    def test_resolve_window_still_refuses_test(self) -> None:
        """Re-asserted here because the whole module depends on it."""
        from app.backtesting.runner import resolve_window

        with pytest.raises(DataLeakageError):
            resolve_window("test")

    def test_selection_reads_validation_only(self) -> None:
        from pathlib import Path

        import app.optimization.search as module

        source = Path(module.__file__).read_text(encoding="utf-8-sig")
        selection = source[source.index("def select_on_validation") :]
        assert 'split="validation"' in selection
        assert '"test"' not in selection.split("return {")[0]

    def test_the_database_constraint_also_enforces_train(self, session) -> None:
        """Belt and braces: a persisted run that claims another split cannot exist."""
        from sqlalchemy.exc import IntegrityError

        from app.database.models import OptimizationRun

        session.add(OptimizationRun(label="leak", method="random", split="validation"))
        with pytest.raises(IntegrityError):
            session.flush()
