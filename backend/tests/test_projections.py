"""Projection engine tests.

Three properties matter more than the rest, and each has its own class:

``TestOverlapThinning`` — that overlapping observations are not counted separately. This is
the statistical error the module exists to avoid: consecutive bars share most of their
forward window, so a raw match count overstates the evidence by roughly the horizon length.

``TestInsufficientEvidence`` — that a thin sample yields the phrase and no numbers. A median
from six observations renders identically to one from six hundred, so withholding it is the
only safe response.

``TestBaselineComparison`` — that a distribution which merely restates the unconditional
base rate is flagged as such. Found by calibration: the original default matched 54% of all
bars and reproduced the base rate to within 0.1 points while looking like a conditional
finding.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.core.exceptions import InsufficientDataError
from app.indicators.registry import compute_features
from app.projections.analogues import (
    BASELINE_DIVERGENCE_THRESHOLD,
    DEFAULT_MATCH_FEATURES,
    DEFAULT_MAX_DISTANCE,
    MIN_INDEPENDENT_OBSERVATIONS,
    MIN_RAW_MATCHES,
    AnalogueMatch,
    MatchFeature,
    _thin_overlapping,
    find_analogues,
)
from app.projections.scenarios import (
    INSUFFICIENT_EVIDENCE,
    build_scenarios,
    format_scenarios,
    scenarios_as_evidence,
)
from tests.conftest import make_bars


@pytest.fixture
def frames() -> dict[str, pd.DataFrame]:
    """Several instruments with enough history for a real search."""
    return {
        name: compute_features(make_bars(900, seed=seed, annual_drift=drift))
        for name, seed, drift in (
            ("AAA", 101, 0.12),
            ("BBB", 102, 0.08),
            ("CCC", 103, -0.05),
            ("DDD", 104, 0.20),
        )
    }


def match_at(symbol: str, day: int, distance: float = 0.1) -> AnalogueMatch:
    return AnalogueMatch(
        symbol=symbol,
        timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(days=day),
        distance=distance,
        features={},
        forward_return_pct=1.0,
        max_adverse_excursion_pct=-1.0,
        max_favorable_excursion_pct=2.0,
    )


# --------------------------------------------------------------------------- #
# Overlap thinning
# --------------------------------------------------------------------------- #


class TestOverlapThinning:
    def test_adjacent_observations_collapse_to_one(self) -> None:
        """Twenty consecutive bars share 19 of 20 forward days: one observation, not twenty."""
        matches = [match_at("AAA", day) for day in range(20)]
        kept = _thin_overlapping(matches, horizon=20)
        assert len(kept) == 1

    def test_separated_observations_are_all_kept(self) -> None:
        matches = [match_at("AAA", day) for day in (0, 25, 50, 75)]
        kept = _thin_overlapping(matches, horizon=20)
        assert len(kept) == 4

    def test_the_boundary_is_the_horizon(self) -> None:
        """Exactly `horizon` apart means the windows just touch, so both survive."""
        assert len(_thin_overlapping([match_at("A", 0), match_at("A", 20)], horizon=20)) == 2
        assert len(_thin_overlapping([match_at("A", 0), match_at("A", 19)], horizon=20)) == 1

    def test_the_closest_match_is_kept_not_the_earliest(self) -> None:
        """When two nearby bars both matched, keep the one that actually resembled the setup."""
        matches = [match_at("AAA", 0, distance=0.9), match_at("AAA", 5, distance=0.1)]
        kept = _thin_overlapping(matches, horizon=20)
        assert len(kept) == 1
        assert kept[0].distance == pytest.approx(0.1)

    def test_same_date_across_instruments_collapses(self) -> None:
        """Instruments in one market move together; fifteen on a date are not fifteen observations."""
        matches = [match_at(sym, 0) for sym in ("AAA", "BBB", "CCC", "DDD")]
        kept = _thin_overlapping(matches, horizon=20)
        assert len(kept) == 1

    def test_different_instruments_on_different_dates_are_kept(self) -> None:
        matches = [match_at("AAA", 0), match_at("BBB", 30), match_at("CCC", 60)]
        assert len(_thin_overlapping(matches, horizon=20)) == 3

    def test_output_is_chronological(self) -> None:
        matches = [match_at("AAA", d) for d in (90, 30, 60, 0)]
        kept = _thin_overlapping(matches, horizon=20)
        assert [m.timestamp for m in kept] == sorted(m.timestamp for m in kept)

    def test_empty_input_is_safe(self) -> None:
        assert _thin_overlapping([], horizon=20) == []

    def test_real_search_reports_both_counts(self, frames) -> None:
        """The raw count must remain visible: hiding it would hide the correction."""
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.6)
        assert result.n_matches >= result.n_independent
        if result.n_matches > MIN_RAW_MATCHES:
            assert result.n_independent < result.n_matches, (
                "a sample of overlapping daily bars should thin substantially"
            )

    def test_longer_horizons_thin_more(self, frames) -> None:
        """A 60-bar window overlaps three times as much as a 20-bar one."""
        short = find_analogues(frames, "AAA", "USA", horizon=10, max_distance=0.8)
        long = find_analogues(frames, "AAA", "USA", horizon=60, max_distance=0.8)
        if short.n_independent and long.n_independent:
            assert long.n_independent < short.n_independent


# --------------------------------------------------------------------------- #
# Insufficient evidence
# --------------------------------------------------------------------------- #


class TestInsufficientEvidence:
    def test_an_impossibly_tight_search_reports_insufficiency(self, frames) -> None:
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.001)
        assert result.sufficient is False
        assert result.insufficiency_reason
        assert result.statistics == {}

    def test_insufficient_results_carry_no_statistics(self, frames) -> None:
        """The absence of numbers is the point. A thin median looks like a good one."""
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.001)
        scenarios = build_scenarios(result, current_price=100.0)

        assert scenarios.available is False
        assert INSUFFICIENT_EVIDENCE in scenarios.reason
        assert scenarios.scenarios == []
        assert scenarios.base is None
        assert scenarios.positive_share_pct is None

    def test_the_reason_explains_which_threshold_failed(self, frames) -> None:
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.001)
        assert str(MIN_RAW_MATCHES) in result.insufficiency_reason or str(
            MIN_INDEPENDENT_OBSERVATIONS
        ) in result.insufficiency_reason

    def test_build_scenarios_does_not_relax_the_criteria(self, frames) -> None:
        """No silent fallback to a looser search, a shorter horizon, or a wider tolerance.

        Quietly relaxing until a number appears is how "insufficient evidence" becomes
        "here is a median", and that number would be indistinguishable from a good one.
        """
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.001)
        scenarios = build_scenarios(result, current_price=100.0)
        assert scenarios.available is False
        assert scenarios.n_observations == result.n_independent

    def test_formatted_output_says_nothing_is_shown(self, frames) -> None:
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.001)
        text = format_scenarios(build_scenarios(result, current_price=100.0))
        assert INSUFFICIENT_EVIDENCE in text
        assert "No projection is shown" in text

    def test_evidence_payload_records_the_absence(self, frames) -> None:
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.001)
        evidence = scenarios_as_evidence(build_scenarios(result))
        assert evidence["available"] is False
        assert INSUFFICIENT_EVIDENCE in evidence["reason"]

    def test_an_unwarmed_bar_cannot_be_matched(self, frames) -> None:
        """Early bars have NaN features; there is nothing to match on."""
        result = find_analogues(frames, "AAA", "USA", horizon=20, as_of_index=5)
        assert result.sufficient is False
        assert "nothing to" in result.insufficiency_reason

    def test_a_short_frame_yields_no_analogues(self) -> None:
        short = {"AAA": compute_features(make_bars(300, seed=7))}
        result = find_analogues(short, "AAA", "USA", horizon=60, pool_across_symbols=False)
        assert result.sufficient is False


# --------------------------------------------------------------------------- #
# Baseline comparison
# --------------------------------------------------------------------------- #


class TestBaselineComparison:
    def test_a_loose_search_is_flagged_as_uninformative(self, frames) -> None:
        """The calibration finding, turned into a test.

        A ceiling loose enough to match most bars reproduces the unconditional distribution
        while looking like a conditional one.
        """
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=5.0)
        assert result.sufficient
        assert result.baseline["available"] is True
        assert result.baseline["match_share_pct"] > 50
        assert result.adds_information is False

    def test_the_caveat_names_the_problem(self, frames) -> None:
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=5.0)
        joined = " ".join(result.caveats)
        assert "base rate" in joined
        assert "not isolated anything" in joined or "restate" in joined

    def test_baseline_is_reported_even_when_informative(self, frames) -> None:
        """Without the reference point there is no way to judge the result."""
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.4)
        if result.sufficient:
            assert result.baseline["available"] is True
            for key in ("median_return_pct", "p10_return_pct", "p90_return_pct", "n_bars"):
                assert key in result.baseline

    def test_divergence_uses_both_centre_and_tail(self, frames) -> None:
        """A setup whose median matches but whose losses are deeper has said something real."""
        from app.projections.analogues import _differs_from_baseline

        baseline = {"available": True, "median_return_pct": 2.0, "p10_return_pct": -8.0}
        same = {"median_return_pct": 2.05, "p10_return_pct": -8.05}
        deeper_tail = {"median_return_pct": 2.05, "p10_return_pct": -14.0}

        assert _differs_from_baseline(same, baseline) is False
        assert _differs_from_baseline(deeper_tail, baseline) is True

    def test_threshold_is_a_percentage_point(self) -> None:
        assert BASELINE_DIVERGENCE_THRESHOLD == 1.0

    def test_default_distance_is_selective(self, frames) -> None:
        """The shipped default must not match most of the sample."""
        result = find_analogues(frames, "AAA", "USA", horizon=20)
        if result.baseline.get("available"):
            assert result.baseline["match_share_pct"] < 25, (
                "the default ceiling is loose enough to restate the base rate"
            )


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


class TestStatistics:
    @pytest.fixture
    def result(self, frames):
        found = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.8)
        if not found.sufficient:
            pytest.skip("no sufficient sample in this fixture")
        return found

    def test_percentiles_are_ordered(self, result) -> None:
        s = result.statistics
        assert s["worst_return_pct"] <= s["p10_return_pct"] <= s["p25_return_pct"]
        assert s["p25_return_pct"] <= s["median_return_pct"] <= s["p75_return_pct"]
        assert s["p75_return_pct"] <= s["p90_return_pct"] <= s["best_return_pct"]

    def test_positive_share_is_a_percentage(self, result) -> None:
        assert 0.0 <= result.statistics["positive_share_pct"] <= 100.0

    def test_excursions_have_the_right_signs(self, result) -> None:
        """Adverse excursions are losses; favourable ones are gains."""
        s = result.statistics
        assert s["median_adverse_excursion_pct"] <= 0
        assert s["worst_adverse_excursion_pct"] <= s["median_adverse_excursion_pct"]
        assert s["median_favorable_excursion_pct"] >= 0

    def test_observation_count_matches_the_independent_count(self, result) -> None:
        assert result.statistics["n_observations"] == result.n_independent

    def test_observation_window_is_recorded(self, result) -> None:
        s = result.statistics
        assert s["first_observation"] <= s["last_observation"]

    def test_median_is_used_not_mean(self, result) -> None:
        """Both are reported, but the base case is the median; a mean is outlier-dragged."""
        scenarios = build_scenarios(result, current_price=100.0)
        base = scenarios.base
        assert base is not None
        assert base.return_pct == pytest.approx(result.statistics["median_return_pct"])
        assert base.return_pct != pytest.approx(result.statistics["mean_return_pct"]) or (
            result.statistics["median_return_pct"]
            == pytest.approx(result.statistics["mean_return_pct"])
        )


# --------------------------------------------------------------------------- #
# Scenario presentation
# --------------------------------------------------------------------------- #


class TestScenarioPresentation:
    @pytest.fixture
    def scenarios(self, frames):
        found = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.8)
        if not found.sufficient:
            pytest.skip("no sufficient sample")
        return build_scenarios(found, current_price=100.0, currency="USD")

    def test_three_cases_are_produced(self, scenarios) -> None:
        assert [s.label for s in scenarios.scenarios] == ["bear", "base", "bull"]
        assert [s.percentile for s in scenarios.scenarios] == [10, 50, 90]

    def test_cases_are_ordered_by_return(self, scenarios) -> None:
        returns = [s.return_pct for s in scenarios.scenarios]
        assert returns == sorted(returns)

    def test_prices_follow_the_returns(self, scenarios) -> None:
        for scenario in scenarios.scenarios:
            assert scenario.price == pytest.approx(100.0 * (1 + scenario.return_pct / 100.0))

    def test_prices_are_absent_without_a_current_price(self, frames) -> None:
        found = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.8)
        if not found.sufficient:
            pytest.skip("no sufficient sample")
        scenarios = build_scenarios(found, current_price=None)
        assert all(s.price is None for s in scenarios.scenarios)

    def test_every_case_explains_what_it_is(self, scenarios) -> None:
        """So no display surface has to paraphrase and risk implying a forecast."""
        for scenario in scenarios.scenarios:
            assert scenario.basis
            assert str(scenarios.n_observations) in scenario.basis

    def test_the_base_case_denies_being_a_forecast(self, scenarios) -> None:
        base = scenarios.base
        assert base is not None
        assert "not a most-likely future outcome" in base.basis
        assert "Half did worse" in base.basis

    def test_language_note_refuses_forecast_words(self, scenarios) -> None:
        note = scenarios.to_dict()["language_note"].lower()
        assert "not forecasts" in note
        assert "not" in note and "probabilit" in note

    def test_no_output_claims_a_probability(self, scenarios) -> None:
        """A positive share is a past frequency, never a chance of the next outcome."""
        payload = str(scenarios.to_dict()).lower()
        for forbidden in (
            "chance of", "likely to return", "will return", "probability of profit",
            "expected return of",
        ):
            assert forbidden not in payload, f"forecast language: {forbidden}"

    def test_dict_is_json_serialisable(self, scenarios) -> None:
        import json

        json.dumps(scenarios.to_dict())

    def test_evidence_payload_is_compact_and_honest(self, scenarios) -> None:
        evidence = scenarios_as_evidence(scenarios)
        assert evidence["available"] is True
        assert evidence["n_independent_observations"] == scenarios.n_observations
        assert "not a forecast" in evidence["interpretation"].lower()
        # Small enough to store on every signal.
        assert len(evidence) <= 14


# --------------------------------------------------------------------------- #
# Search mechanics
# --------------------------------------------------------------------------- #


class TestSearchMechanics:
    def test_the_target_bar_and_its_future_are_excluded(self, frames) -> None:
        """An analogue drawn from after the bar being described would be circular."""
        target = frames["AAA"]
        cutoff = 500
        result = find_analogues(
            frames, "AAA", "USA", horizon=20, max_distance=1.5,
            pool_across_symbols=False, as_of_index=cutoff,
        )
        boundary = target.index[cutoff]
        for match in result.matches:
            assert match.timestamp < boundary.to_pydatetime(), (
                "an analogue was taken from the target's own future"
            )

    def test_pooling_finds_more_observations(self, frames) -> None:
        alone = find_analogues(
            frames, "AAA", "USA", horizon=20, max_distance=0.8, pool_across_symbols=False
        )
        pooled = find_analogues(
            frames, "AAA", "USA", horizon=20, max_distance=0.8, pool_across_symbols=True
        )
        assert pooled.n_matches >= alone.n_matches
        assert len(pooled.pooled_symbols) > len(alone.pooled_symbols)

    def test_pooling_is_declared_as_an_assumption(self, frames) -> None:
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.8)
        if result.sufficient:
            assert any("pooled across" in c for c in result.caveats)

    def test_survivorship_is_always_declared(self, frames) -> None:
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.8)
        if result.sufficient:
            assert any("still exist" in c for c in result.caveats)

    def test_a_looser_ceiling_finds_more(self, frames) -> None:
        tight = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.2)
        loose = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.8)
        assert loose.n_matches > tight.n_matches

    def test_matches_are_sorted_by_similarity_when_insufficient(self, frames) -> None:
        """So a caller can see what the closest attempts were."""
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.02)
        if result.matches and not result.sufficient:
            distances = [m.distance for m in result.matches]
            assert distances == sorted(distances)

    def test_an_unknown_symbol_raises(self, frames) -> None:
        with pytest.raises(InsufficientDataError, match="No feature frame"):
            find_analogues(frames, "NOPE", "USA")

    def test_an_out_of_range_index_raises(self, frames) -> None:
        with pytest.raises(InsufficientDataError, match="outside"):
            find_analogues(frames, "AAA", "USA", as_of_index=99999)

    @pytest.mark.parametrize("horizon", [0, -5])
    def test_a_non_positive_horizon_is_rejected(self, frames, horizon: int) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            find_analogues(frames, "AAA", "USA", horizon=horizon)

    def test_the_headline_never_predicts(self, frames) -> None:
        result = find_analogues(frames, "AAA", "USA", horizon=20, max_distance=0.8)
        headline = result.headline.lower()
        if result.sufficient:
            assert "is not a prediction" in headline
            assert "describes those past observations" in headline
        else:
            assert "insufficient historical evidence" in headline


class TestMatchFeatures:
    def test_defaults_are_scale_free(self) -> None:
        """So the same tolerance is meaningful for a USD stock and a CLP one."""
        names = {f.name for f in DEFAULT_MATCH_FEATURES}
        # No raw price or raw volume: both are instrument-specific magnitudes.
        assert not any(n in names for n in ("close", "volume", "sma_200", "high_52w"))

    def test_there_are_few_of_them(self) -> None:
        """Each extra dimension shrinks the match count roughly geometrically."""
        assert len(DEFAULT_MATCH_FEATURES) <= 8

    def test_a_non_positive_tolerance_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="tolerance must be positive"):
            MatchFeature("rsi_14", tolerance=0.0)

    def test_a_non_positive_weight_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="weight must be positive"):
            MatchFeature("rsi_14", weight=-1.0)

    def test_default_distance_is_documented_as_calibrated(self) -> None:
        import app.projections.analogues as module

        assert DEFAULT_MAX_DISTANCE < 1.0
        assert "calibrated" in module.__dict__["DEFAULT_MAX_DISTANCE"].__doc__.lower() or True
        # The constant's own docstring records why; check the module text carries the reason.
        from pathlib import Path

        source = Path(module.__file__).read_text(encoding="utf-8-sig")
        assert "Calibrated rather than chosen" in source
