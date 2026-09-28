"""Indicator correctness.

Values are checked against hand-computed arithmetic on small series rather than
against another library. Comparing two implementations only proves they agree;
comparing against the definition proves one of them is right.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.indicators import momentum, price, trend, volatility, volume
from app.indicators.base import pct_distance, safe_divide, validate_frame, wilder_rma
from app.indicators.registry import (
    FEATURE_COLUMNS,
    MIN_BARS_FOR_FULL_FEATURES,
    compute_features,
    latest_features,
)


def series(values: list[float], start: str = "2024-01-01") -> pd.Series:
    index = pd.bdate_range(start=start, periods=len(values), tz="UTC")
    return pd.Series(values, index=index, dtype=float)


# --------------------------------------------------------------------------- #
# base
# --------------------------------------------------------------------------- #


class TestWilderRMA:
    def test_seeds_with_simple_mean_then_recurses(self) -> None:
        data = series([1, 2, 3, 4, 5, 6, 7, 8])
        result = wilder_rma(data, 3)

        assert result.iloc[:2].isna().all()
        # Seed: mean of the first 3 = 2.0
        assert result.iloc[2] == pytest.approx(2.0)
        # Then rma[i] = (rma[i-1] * 2 + x[i]) / 3
        assert result.iloc[3] == pytest.approx((2.0 * 2 + 4) / 3)
        assert result.iloc[4] == pytest.approx(((2.0 * 2 + 4) / 3 * 2 + 5) / 3)

    def test_skips_leading_nan(self) -> None:
        """A differenced series starts with NaN; the seed must begin after it."""
        data = series([np.nan, 2, 4, 6, 8])
        result = wilder_rma(data, 3)
        assert result.iloc[:3].isna().all()
        assert result.iloc[3] == pytest.approx(4.0)  # mean(2, 4, 6)

    def test_returns_all_nan_when_too_short(self) -> None:
        result = wilder_rma(series([1, 2]), 5)
        assert result.isna().all()

    def test_rejects_bad_length(self) -> None:
        with pytest.raises(ValueError, match="length must be >= 1"):
            wilder_rma(series([1, 2, 3]), 0)


class TestSafeDivide:
    def test_zero_denominator_becomes_nan_not_inf(self) -> None:
        result = safe_divide(series([1, 2, 3]), series([1, 0, 3]))
        assert result.iloc[0] == pytest.approx(1.0)
        assert pd.isna(result.iloc[1])
        assert result.iloc[2] == pytest.approx(1.0)
        assert not np.isinf(result).any()

    def test_pct_distance_sign_and_scale(self) -> None:
        result = pct_distance(series([104.0]), series([100.0]))
        assert result.iloc[0] == pytest.approx(4.0)


class TestValidateFrame:
    def test_rejects_missing_columns(self, bars: pd.DataFrame) -> None:
        with pytest.raises(ValueError, match="missing required columns"):
            validate_frame(bars.drop(columns=["volume"]))

    def test_rejects_duplicate_timestamps(self, bars: pd.DataFrame) -> None:
        doubled = pd.concat([bars.iloc[:10], bars.iloc[:10]])
        with pytest.raises(ValueError, match="duplicate timestamps"):
            validate_frame(doubled)

    def test_rejects_non_datetime_index(self, bars: pd.DataFrame) -> None:
        reset = bars.reset_index(drop=True)
        with pytest.raises(ValueError, match="DatetimeIndex"):
            validate_frame(reset)

    def test_sorts_unsorted_input(self, bars: pd.DataFrame) -> None:
        shuffled = bars.iloc[:20].iloc[::-1]
        result = validate_frame(shuffled)
        assert result.index.is_monotonic_increasing


# --------------------------------------------------------------------------- #
# trend
# --------------------------------------------------------------------------- #


class TestMovingAverages:
    def test_sma_is_the_mean_of_the_window(self) -> None:
        data = series([1, 2, 3, 4, 5, 6])
        result = trend.sma(data, 3)
        assert result.iloc[:2].isna().all()
        assert result.iloc[2] == pytest.approx(2.0)
        assert result.iloc[5] == pytest.approx(5.0)

    def test_sma_respects_min_periods(self) -> None:
        """No value before a full window -- not even a partial mean."""
        result = trend.sma(series([1, 2, 3]), 5)
        assert result.isna().all()

    def test_ema_follows_the_standard_recursion(self) -> None:
        data = series([10, 11, 12, 13, 14, 15])
        length = 3
        alpha = 2.0 / (length + 1)
        result = trend.ema(data, length)

        # adjust=False seeds from the first observation, so by index 2 the value is
        # the recursion applied twice.
        seed = 10.0
        step1 = seed + alpha * (11 - seed)
        step2 = step1 + alpha * (12 - step1)
        assert result.iloc[2] == pytest.approx(step2)
        assert result.iloc[:2].isna().all()

    def test_ema_reacts_faster_than_sma(self, bars: pd.DataFrame) -> None:
        close = bars["close"]
        ema_line = trend.ema(close, 20)
        sma_line = trend.sma(close, 20)
        # On a trending series the EMA sits closer to price on average.
        ema_gap = (close - ema_line).abs().mean()
        sma_gap = (close - sma_line).abs().mean()
        assert ema_gap < sma_gap

    @pytest.mark.parametrize("length", [0, -5])
    def test_rejects_non_positive_length(self, length: int) -> None:
        with pytest.raises(ValueError):
            trend.sma(series([1, 2, 3]), length)
        with pytest.raises(ValueError):
            trend.ema(series([1, 2, 3]), length)


class TestTrendConditions:
    def test_above_ma_treats_nan_as_false(self) -> None:
        close = series([100, 101, 102])
        ma = pd.Series([np.nan, 100.0, 103.0], index=close.index)
        result = trend.above_ma(close, ma)
        assert result.tolist() == [False, True, False]

    def test_golden_cross_treats_nan_as_false(self) -> None:
        fast = series([np.nan, 10, 12])
        slow = series([5, 11, 11])
        assert trend.golden_cross(fast, slow).tolist() == [False, False, True]

    def test_trend_score_is_bounded_and_ordinal(self, bars: pd.DataFrame) -> None:
        features = compute_features(bars)
        score = features["trend_score"].dropna()
        assert not score.empty
        assert score.min() >= 0.0
        assert score.max() <= 1.0
        # Four conditions -> only multiples of 0.25 are reachable.
        assert set(np.round(score.unique(), 6)) <= {0.0, 0.25, 0.5, 0.75, 1.0}

    def test_trend_score_requires_its_inputs(self, bars: pd.DataFrame) -> None:
        with pytest.raises(ValueError, match="trend_score needs columns"):
            trend.trend_score(bars)


# --------------------------------------------------------------------------- #
# momentum
# --------------------------------------------------------------------------- #


class TestRSI:
    def test_matches_hand_computed_wilder_value(self) -> None:
        """14-period RSI on a series with 11 up moves and 3 down moves of size 1.

        avg_gain = 11/14, avg_loss = 3/14, RS = 11/3,
        RSI = 100 - 100 / (1 + 11/3) = 100 - 300/14 = 78.5714...
        """
        closes = [10, 11, 12, 11, 12, 13, 14, 13, 14, 15, 16, 15, 16, 17, 18]
        result = momentum.rsi(series(closes), 14)
        assert result.iloc[14] == pytest.approx(100.0 - 300.0 / 14.0)

    def test_unbroken_gains_give_100(self) -> None:
        result = momentum.rsi(series(list(range(1, 40))), 14)
        assert result.iloc[-1] == pytest.approx(100.0)

    def test_unbroken_losses_give_0(self) -> None:
        result = momentum.rsi(series(list(range(40, 1, -1))), 14)
        assert result.iloc[-1] == pytest.approx(0.0)

    def test_always_within_bounds(self, bars: pd.DataFrame) -> None:
        result = momentum.rsi(bars["close"], 14).dropna()
        assert result.min() >= 0.0
        assert result.max() <= 100.0

    def test_rejects_short_length(self) -> None:
        with pytest.raises(ValueError, match="RSI length must be >= 2"):
            momentum.rsi(series([1, 2, 3]), 1)


class TestMACD:
    def test_histogram_is_line_minus_signal(self, bars: pd.DataFrame) -> None:
        result = momentum.macd(bars["close"])
        computed = result["macd"] - result["macd_signal"]
        pd.testing.assert_series_equal(
            result["macd_hist"].dropna(), computed.dropna(), check_names=False
        )

    def test_masks_the_warmup_period(self, bars: pd.DataFrame) -> None:
        result = momentum.macd(bars["close"], 12, 26, 9)
        warmup = 26 + 9
        assert result["macd"].iloc[: warmup - 1].isna().all()
        assert result["macd"].iloc[warmup - 1 :].notna().all()

    def test_rejects_fast_slower_than_slow(self) -> None:
        with pytest.raises(ValueError, match="must be shorter than slow"):
            momentum.macd(series([1.0] * 50), fast=26, slow=12)

    def test_positive_on_a_rising_series(self) -> None:
        rising = series([100 * 1.01**i for i in range(80)])
        result = momentum.macd(rising)
        assert result["macd"].iloc[-1] > 0


class TestROC:
    def test_is_the_trailing_percentage_change(self) -> None:
        data = series([100, 105, 110, 121])
        result = momentum.roc(data, 3)
        assert pd.isna(result.iloc[2])
        assert result.iloc[3] == pytest.approx(21.0)

    def test_momentum_score_is_bounded(self, bars: pd.DataFrame) -> None:
        score = compute_features(bars)["momentum_score"].dropna()
        assert score.min() >= 0.0
        assert score.max() <= 1.0
        # Three conditions -> only thirds are reachable.
        reachable = {round(k / 3, 6) for k in range(4)}
        assert set(np.round(score.unique(), 6)) <= reachable


# --------------------------------------------------------------------------- #
# volatility
# --------------------------------------------------------------------------- #


class TestTrueRangeAndATR:
    def test_true_range_includes_the_overnight_gap(self) -> None:
        """A gap up must count as range, even when the bar itself is narrow."""
        index = pd.bdate_range("2024-01-01", periods=2, tz="UTC")
        high = pd.Series([100.0, 120.0], index=index)
        low = pd.Series([95.0, 118.0], index=index)
        close = pd.Series([98.0, 119.0], index=index)

        result = volatility.true_range(high, low, close)
        assert pd.isna(result.iloc[0])  # no previous close
        # max(120-118=2, |120-98|=22, |118-98|=20) = 22
        assert result.iloc[1] == pytest.approx(22.0)

    def test_atr_is_non_negative(self, bars: pd.DataFrame) -> None:
        result = volatility.atr(bars["high"], bars["low"], bars["close"], 14).dropna()
        assert (result >= 0).all()

    def test_atr_pct_is_scale_invariant(self, bars: pd.DataFrame) -> None:
        """Multiplying every price by 1000 must not change ATR as a percentage.

        This is what makes the measure comparable between a USD stock and a CLP one
        quoted in the tens of thousands.
        """
        scaled = bars.copy()
        for column in ("open", "high", "low", "close"):
            scaled[column] = scaled[column] * 1000.0

        original = volatility.atr_pct(bars["high"], bars["low"], bars["close"], 14)
        rescaled = volatility.atr_pct(scaled["high"], scaled["low"], scaled["close"], 14)
        pd.testing.assert_series_equal(original, rescaled, check_names=False, rtol=1e-10)


class TestVolatilityAndBands:
    def test_rolling_volatility_is_zero_on_a_flat_series(self) -> None:
        result = volatility.rolling_volatility(series([100.0] * 40), 20)
        assert result.dropna().abs().max() == pytest.approx(0.0)

    def test_annualisation_scales_by_sqrt_of_periods(self, bars: pd.DataFrame) -> None:
        raw = volatility.rolling_volatility(bars["close"], 20, annualize=False)
        annual = volatility.rolling_volatility(bars["close"], 20, annualize=True)
        ratio = (annual / raw).dropna()
        assert ratio.min() == pytest.approx(np.sqrt(252.0), rel=1e-9)
        assert ratio.max() == pytest.approx(np.sqrt(252.0), rel=1e-9)

    def test_bollinger_mid_equals_sma(self, bars: pd.DataFrame) -> None:
        bands = volatility.bollinger_bands(bars["close"], 20, 2.0)
        pd.testing.assert_series_equal(
            bands["bb_mid"], trend.sma(bars["close"], 20), check_names=False
        )

    def test_bollinger_bands_are_symmetric(self, bars: pd.DataFrame) -> None:
        bands = volatility.bollinger_bands(bars["close"], 20, 2.0).dropna()
        above = bands["bb_upper"] - bands["bb_mid"]
        below = bands["bb_mid"] - bands["bb_lower"]
        pd.testing.assert_series_equal(above, below, check_names=False)

    def test_percent_b_is_nan_when_bands_have_no_width(self, flat_bars: pd.DataFrame) -> None:
        """A perfectly flat window gives zero-width bands -- position is undefined."""
        result = volatility.bollinger_percent_b(flat_bars["close"], 20)
        assert result.isna().all()


# --------------------------------------------------------------------------- #
# volume
# --------------------------------------------------------------------------- #


class TestVolume:
    def test_relative_volume_excludes_the_current_bar(self) -> None:
        """The baseline must not contain the spike it is measuring.

        Ten bars of 100 followed by one of 500: relative volume must read exactly
        5.0. If the current bar were inside the average it would read about 4.5.
        """
        volumes = [100.0] * 10 + [500.0]
        result = volume.relative_volume(series(volumes), 10)
        assert result.iloc[10] == pytest.approx(5.0)

    def test_relative_volume_is_nan_when_baseline_is_zero(
        self, flat_bars: pd.DataFrame
    ) -> None:
        result = volume.relative_volume(flat_bars["volume"], 20)
        assert result.isna().all()

    def test_volume_spike_treats_nan_as_false(self, flat_bars: pd.DataFrame) -> None:
        result = volume.volume_spike(flat_bars["volume"], 20, 2.0)
        assert result.dtype == bool
        assert not result.any()

    def test_volume_spike_fires_at_the_threshold(self) -> None:
        volumes = [100.0] * 10 + [200.0]
        result = volume.volume_spike(series(volumes), 10, threshold=2.0)
        assert bool(result.iloc[10]) is True

    def test_dollar_volume_uses_traded_value(self) -> None:
        closes = series([10.0] * 5)
        volumes = series([1000.0] * 5)
        result = volume.dollar_volume(closes, volumes, 5)
        assert result.iloc[4] == pytest.approx(10_000.0)


# --------------------------------------------------------------------------- #
# price
# --------------------------------------------------------------------------- #


class TestPriceFeatures:
    def test_trailing_return_is_backward_looking(self) -> None:
        data = series([100, 110, 121])
        result = price.trailing_return(data, 2)
        assert pd.isna(result.iloc[0])
        assert pd.isna(result.iloc[1])
        assert result.iloc[2] == pytest.approx(21.0)

    def test_rolling_high_includes_the_current_bar(self) -> None:
        """At the close of a bar its own high is known -- including it is not a leak."""
        high = series([10, 12, 11, 15, 13])
        result = price.rolling_high(high, 3)
        assert result.iloc[3] == pytest.approx(15.0)

    def test_distance_from_high_is_zero_at_a_new_high(self) -> None:
        index = pd.bdate_range("2024-01-01", periods=5, tz="UTC")
        high = pd.Series([10, 12, 11, 15, 15], index=index, dtype=float)
        close = pd.Series([10, 12, 11, 15, 15], index=index, dtype=float)
        result = price.distance_from_high(close, high, length=3)
        assert result.iloc[3] == pytest.approx(0.0)

    def test_distance_from_high_is_never_positive(self, bars: pd.DataFrame) -> None:
        result = price.distance_from_high(bars["close"], bars["high"]).dropna()
        assert result.max() <= 1e-9

    def test_distance_from_low_is_never_negative(self, bars: pd.DataFrame) -> None:
        result = price.distance_from_low(bars["close"], bars["low"]).dropna()
        assert result.min() >= -1e-9

    def test_position_in_range_is_bounded(self, bars: pd.DataFrame) -> None:
        result = price.position_in_range(bars["close"], bars["high"], bars["low"]).dropna()
        assert result.min() >= 0.0
        assert result.max() <= 1.0


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


class TestFeatureRegistry:
    def test_produces_every_declared_column(self, bars: pd.DataFrame) -> None:
        """FEATURE_COLUMNS and compute_features must not drift apart."""
        features = compute_features(bars)
        missing = [c for c in FEATURE_COLUMNS if c not in features.columns]
        assert missing == [], f"declared but not computed: {missing}"

    def test_declares_every_produced_column(self, bars: pd.DataFrame) -> None:
        features = compute_features(bars)
        bar_columns = set(bars.columns)
        undeclared = [
            c
            for c in features.columns
            if c not in bar_columns and c not in FEATURE_COLUMNS
        ]
        assert undeclared == [], f"computed but not declared in FEATURE_COLUMNS: {undeclared}"

    def test_preserves_the_index(self, bars: pd.DataFrame) -> None:
        features = compute_features(bars)
        pd.testing.assert_index_equal(features.index, bars.index)

    def test_keeps_original_bar_columns_untouched(self, bars: pd.DataFrame) -> None:
        features = compute_features(bars)
        for column in ("open", "high", "low", "close", "volume"):
            pd.testing.assert_series_equal(features[column], bars[column])

    def test_latest_features_returns_json_safe_values(self, bars: pd.DataFrame) -> None:
        features = compute_features(bars)
        latest = latest_features(features)
        assert latest["rsi_14"] is not None
        for key, value in latest.items():
            assert value is None or isinstance(value, (float, bool)), f"{key} -> {type(value)}"

    def test_latest_features_refuses_an_incomplete_row(self, short_bars: pd.DataFrame) -> None:
        """30 bars cannot produce a 200-day average; saying so beats guessing."""
        from app.core.exceptions import InsufficientDataError

        features = compute_features(short_bars)
        with pytest.raises(InsufficientDataError, match="still NaN on the latest bar"):
            latest_features(features, require_complete=True)

    def test_latest_features_allows_incomplete_when_asked(self, short_bars: pd.DataFrame) -> None:
        features = compute_features(short_bars)
        latest = latest_features(features, require_complete=False)
        assert latest["sma_200"] is None

    def test_min_bars_constant_matches_the_slowest_indicator(self, bars: pd.DataFrame) -> None:
        """The documented warm-up must be the real one."""
        features = compute_features(bars)
        first_complete = features[list(FEATURE_COLUMNS)].dropna().index[0]
        position = features.index.get_loc(first_complete)
        assert position + 1 == MIN_BARS_FOR_FULL_FEATURES, (
            f"features first complete at bar {position + 1}, but "
            f"MIN_BARS_FOR_FULL_FEATURES says {MIN_BARS_FOR_FULL_FEATURES}"
        )

    def test_survives_a_degenerate_flat_series(self, flat_bars: pd.DataFrame) -> None:
        """Zero volume and zero volatility must not raise or produce infinities."""
        features = compute_features(flat_bars)
        numeric = features.select_dtypes(include=[np.number])
        assert not np.isinf(numeric.to_numpy()).any()
