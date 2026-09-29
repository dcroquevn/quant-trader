"""Strategy engine tests.

The recurring concern: a strategy must never treat "unknown" as "condition satisfied".
An indicator that has not warmed up yields ``NaN``, and ``NaN > x`` is False in Python —
which is the right answer for an entry gate but the *wrong* answer if a codebase ever
relies on the negation. Several tests below pin that down explicitly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core.exceptions import DataLeakageError
from app.indicators.registry import compute_features
from app.strategies.base import Action, ComponentScore, Decision
from app.strategies.registry import (
    available_strategies,
    build_strategy,
    get_strategy_class,
    register_strategy,
    strategy_catalog,
)
from app.strategies.trend_momentum import TrendMomentumParams, TrendMomentumStrategy
from tests.conftest import make_bars


@pytest.fixture
def features() -> pd.DataFrame:
    """Features over a long uptrend, so entry conditions can actually fire."""
    bars = make_bars(600, seed=101, annual_drift=0.30, annual_vol=0.20)
    return compute_features(bars)


@pytest.fixture
def downtrend_features() -> pd.DataFrame:
    bars = make_bars(600, seed=102, annual_drift=-0.30, annual_vol=0.20)
    return compute_features(bars)


class TestParameterValidation:
    def test_defaults_are_valid(self) -> None:
        TrendMomentumParams()

    def test_inverted_rsi_band_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="rsi_min .* must be below rsi_max"):
            TrendMomentumParams(rsi_min=70.0, rsi_max=40.0)

    def test_inverted_volatility_band_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_atr_pct .* must be below"):
            TrendMomentumParams(min_atr_pct=9.0, max_atr_pct=1.0)

    def test_a_strategy_without_a_stop_is_rejected(self) -> None:
        """Unbounded loss per trade is not a configuration, it is a bug."""
        with pytest.raises(ValueError, match="unbounded loss"):
            TrendMomentumParams(stop_atr_multiple=0.0)

    def test_an_uncomputed_roc_period_is_rejected(self) -> None:
        """Silently falling back to a different period would misreport the strategy."""
        with pytest.raises(ValueError, match="must be one of the computed periods"):
            TrendMomentumParams(roc_period=13)

    def test_params_round_trip_through_dict(self) -> None:
        params = TrendMomentumParams(rsi_min=35.0, stop_atr_multiple=2.5)
        assert TrendMomentumParams.from_dict(params.to_dict()) == params

    def test_unknown_parameter_name_is_rejected(self) -> None:
        """A typo must not silently produce a run of the default configuration."""
        with pytest.raises(ValueError, match="unknown parameters"):
            TrendMomentumParams.from_dict({"rsi_minimum": 30.0})

    def test_params_are_immutable(self) -> None:
        params = TrendMomentumParams()
        with pytest.raises(Exception):
            params.rsi_min = 10.0  # type: ignore[misc]

    def test_replace_returns_a_modified_copy(self) -> None:
        original = TrendMomentumParams(rsi_min=40.0)
        modified = original.replace(rsi_min=30.0)
        assert original.rsi_min == 40.0
        assert modified.rsi_min == 30.0


class TestRequiredFeatures:
    def test_declared_features_exist_in_the_registry_output(self, features) -> None:
        strategy = TrendMomentumStrategy()
        for column in strategy.required_features:
            assert column in features.columns, f"{column} not produced by compute_features"

    def test_roc_period_changes_the_required_column(self) -> None:
        strategy = TrendMomentumStrategy(TrendMomentumParams(roc_period=60))
        assert "roc_60" in strategy.required_features
        assert "roc_20" not in strategy.required_features

    def test_a_missing_feature_raises(self, features) -> None:
        strategy = TrendMomentumStrategy()
        with pytest.raises(ValueError, match="requires feature columns"):
            strategy.evaluate(features.drop(columns=["atr_pct_14"]))

    def test_forward_columns_are_rejected(self) -> None:
        bars = make_bars(400, seed=7)
        contaminated = compute_features(bars, include_forward=True)
        with pytest.raises(DataLeakageError, match="forward"):
            TrendMomentumStrategy().evaluate(contaminated)


class TestWarmup:
    def test_holds_while_indicators_are_unwarmed(self, features) -> None:
        """A HOLD must be a decision, not the accidental result of NaN comparisons."""
        strategy = TrendMomentumStrategy()
        decision = strategy.evaluate(features, index=10)

        assert decision.action is Action.HOLD
        assert decision.score == 0.0
        assert any("not warmed up" in reason for reason in decision.reasons)

    def test_never_buys_before_its_own_slowest_indicator_is_ready(self, features) -> None:
        """No entry before EMA200 has a full window.

        Note this is 200 bars, not the 252 that `compute_features` needs for a complete
        feature set: this strategy does not read the 52-week high/low, so it becomes
        active as soon as the features it actually declares are warm. Asserting 252 here
        would be testing a requirement the strategy does not have.
        """
        strategy = TrendMomentumStrategy()
        first_warm = int(features["ema_200"].notna().argmax())
        assert first_warm == 199

        for i in range(0, first_warm):
            decision = strategy.evaluate(features, index=i)
            assert decision.action is not Action.BUY
            assert decision.score == 0.0

    def test_empty_frame_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty frame"):
            TrendMomentumStrategy().evaluate(pd.DataFrame())


class TestEntryLogic:
    def test_buys_when_every_condition_holds(self, features) -> None:
        """Permissive parameters over an uptrend must produce at least one entry."""
        strategy = TrendMomentumStrategy(
            TrendMomentumParams(
                rsi_min=30.0, rsi_max=90.0, min_relative_volume=0.0,
                min_atr_pct=0.0, max_atr_pct=100.0, require_macd_positive=False,
            )
        )
        actions = strategy.evaluate_series(features)["action"]
        assert (actions == "BUY").any()

    def test_does_not_buy_in_a_downtrend(self, downtrend_features) -> None:
        """The regime filter is the point of the trend component."""
        strategy = TrendMomentumStrategy(
            TrendMomentumParams(rsi_min=0.0, rsi_max=100.0, min_relative_volume=0.0)
        )
        actions = strategy.evaluate_series(downtrend_features)["action"]
        buys = (actions == "BUY").sum()
        # A strong downtrend should produce very few or no entries.
        assert buys <= len(actions) * 0.02

    def test_a_failing_condition_blocks_the_entry_and_is_named(self, features) -> None:
        impossible = TrendMomentumStrategy(
            TrendMomentumParams(min_relative_volume=1_000.0)
        )
        decision = impossible.evaluate(features)
        assert decision.action is Action.HOLD
        assert any("relative volume" in reason for reason in decision.reasons)

    def test_score_counts_conditions_and_stays_bounded(self, features) -> None:
        strategy = TrendMomentumStrategy()
        scores = strategy.evaluate_series(features)["score"]
        assert scores.min() >= 0.0
        assert scores.max() <= 1.0

    def test_score_reaches_one_only_on_a_buy(self, features) -> None:
        strategy = TrendMomentumStrategy(
            TrendMomentumParams(
                rsi_min=30.0, rsi_max=90.0, min_relative_volume=0.0,
                min_atr_pct=0.0, max_atr_pct=100.0, require_macd_positive=False,
            )
        )
        table = strategy.evaluate_series(features)
        perfect = table[table["score"] >= 0.999]
        if not perfect.empty:
            assert (perfect["action"] == "BUY").all()

    def test_stop_is_below_entry_and_target_above(self, features) -> None:
        strategy = TrendMomentumStrategy(
            TrendMomentumParams(
                rsi_min=30.0, rsi_max=90.0, min_relative_volume=0.0,
                min_atr_pct=0.0, max_atr_pct=100.0, require_macd_positive=False,
            )
        )
        for i in range(300, len(features)):
            decision = strategy.evaluate(features, index=i)
            if decision.action is Action.BUY:
                close = float(features["close"].iloc[i])
                assert decision.stop_price is not None
                assert decision.take_profit_price is not None
                assert decision.stop_price < close < decision.take_profit_price
                return
        pytest.skip("no entry generated in this sample")

    def test_risk_reward_matches_the_configured_multiple(self, features) -> None:
        strategy = TrendMomentumStrategy(
            TrendMomentumParams(
                rsi_min=30.0, rsi_max=90.0, min_relative_volume=0.0,
                min_atr_pct=0.0, max_atr_pct=100.0, require_macd_positive=False,
                take_profit_r_multiple=3.0,
            )
        )
        for i in range(300, len(features)):
            decision = strategy.evaluate(features, index=i)
            if decision.action is Action.BUY:
                close = float(features["close"].iloc[i])
                risk = close - decision.stop_price
                reward = decision.take_profit_price - close
                assert reward / risk == pytest.approx(3.0, rel=1e-6)
                return
        pytest.skip("no entry generated in this sample")


class TestExitLogic:
    def test_exits_on_a_trend_break(self, features) -> None:
        strategy = TrendMomentumStrategy()
        # Find a bar where price closed below EMA50.
        broken = features[features["close"] < features["ema_50"]]
        if broken.empty:
            pytest.skip("no trend break in this sample")

        index = features.index.get_loc(broken.index[len(broken) // 2])
        decision = strategy.evaluate(features, in_position=True, index=int(index))
        assert decision.action is Action.SELL
        assert any("EMA50" in reason for reason in decision.reasons)

    def test_holds_while_the_trend_persists(self, features) -> None:
        strategy = TrendMomentumStrategy(TrendMomentumParams(exit_rsi_max=99.0))
        intact = features[
            (features["close"] > features["ema_50"]) & (features["rsi_14"] < 90)
        ]
        if intact.empty:
            pytest.skip("no intact-trend bar in this sample")

        index = features.index.get_loc(intact.index[len(intact) // 2])
        decision = strategy.evaluate(features, in_position=True, index=int(index))
        assert decision.action is Action.HOLD

    def test_exit_score_is_not_comparable_to_an_entry_score(self, features) -> None:
        """An exit carries no ranking information, so it must not report a high score.

        Reporting 1.0 would invite a reader to compare it against an entry score, which
        measures an entirely different thing.
        """
        strategy = TrendMomentumStrategy()
        broken = features[features["close"] < features["ema_50"]]
        if broken.empty:
            pytest.skip("no trend break in this sample")
        index = features.index.get_loc(broken.index[0])
        decision = strategy.evaluate(features, in_position=True, index=int(index))
        assert decision.score == 0.0


class TestDecisionPresentation:
    def test_score_description_refuses_probability_language(self) -> None:
        decision = Decision(
            action=Action.BUY,
            score=0.75,
            components=(
                ComponentScore("a", True),
                ComponentScore("b", True),
                ComponentScore("c", True),
                ComponentScore("d", False),
            ),
        )
        text = decision.describe_score()
        assert "3 of 4" in text
        assert "not a probability" in text.lower()

    def test_describe_score_handles_no_components(self) -> None:
        assert "no components" in Decision(Action.HOLD, 0.0).describe_score()

    def test_hold_is_not_actionable(self) -> None:
        assert Decision(Action.HOLD, 0.0).is_actionable is False
        assert Decision(Action.BUY, 1.0).is_actionable is True


class TestEvaluateSeries:
    def test_returns_one_row_per_bar(self, features) -> None:
        table = TrendMomentumStrategy().evaluate_series(features)
        assert len(table) == len(features)
        pd.testing.assert_index_equal(table.index, features.index)

    def test_actions_are_valid_values(self, features) -> None:
        table = TrendMomentumStrategy().evaluate_series(features)
        assert set(table["action"]).issubset({"BUY", "SELL", "HOLD"})

    def test_no_nan_scores(self, features) -> None:
        table = TrendMomentumStrategy().evaluate_series(features)
        assert not np.isnan(table["score"].to_numpy()).any()


class TestRegistry:
    def test_the_default_strategy_is_registered(self) -> None:
        assert "trend_momentum" in available_strategies()

    def test_lookup_is_case_insensitive(self) -> None:
        assert get_strategy_class("TREND_MOMENTUM") is TrendMomentumStrategy

    def test_unknown_strategy_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown strategy"):
            get_strategy_class("magic_money")

    def test_build_with_defaults(self) -> None:
        strategy = build_strategy("trend_momentum")
        assert isinstance(strategy, TrendMomentumStrategy)

    def test_build_with_a_parameter_dict(self) -> None:
        strategy = build_strategy("trend_momentum", {"rsi_min": 25.0})
        assert strategy.params.rsi_min == 25.0  # type: ignore[attr-defined]

    def test_build_rejects_an_unknown_parameter(self) -> None:
        with pytest.raises(ValueError, match="unknown parameters"):
            build_strategy("trend_momentum", {"not_a_param": 1})

    def test_build_validates_immediately(self) -> None:
        """A bad combination must fail before a backtest spends minutes on it."""
        with pytest.raises(ValueError, match="rsi_min"):
            build_strategy("trend_momentum", {"rsi_min": 90.0, "rsi_max": 10.0})

    def test_catalog_exposes_defaults(self) -> None:
        catalog = strategy_catalog()
        entry = next(e for e in catalog if e["name"] == "trend_momentum")
        assert "rsi_min" in entry["default_params"]
        assert entry["description"]

    def test_catalog_description_does_not_claim_profitability(self) -> None:
        for entry in strategy_catalog():
            text = entry["description"].lower()
            assert "guaranteed" not in text
            assert "profitable" not in text or "not known to be profitable" in text

    def test_refuses_to_shadow_a_registered_name(self) -> None:
        with pytest.raises(ValueError, match="already registered"):
            register_strategy(TrendMomentumStrategy)


class TestDescribe:
    def test_identity_is_serialisable(self) -> None:
        import json

        described = TrendMomentumStrategy().describe()
        json.dumps(described)
        assert described["name"] == "trend_momentum"
        assert "params" in described
