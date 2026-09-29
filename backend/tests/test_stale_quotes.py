"""Carried-forward (stale) quote detection, trimming and freshness.

Motivated by a real observation, not a hypothetical. On 2026-09-27 Yahoo served
**49** consecutive identical zero-volume daily bars for `SQM-B.SN` and `ANDINA-B.SN`,
and 5 for the other 16 Chilean tickers, all dated to that week. Every US ticker was
clean. The Chilean series therefore *looked* current while containing no recent
trading at all.

This is more dangerous than ordinary stale data. An old last bar gives itself away
by its date. A carried-forward quote is dated today, passes any freshness check based
on timestamps, and produces indicator values that look perfectly reasonable.

The handling has three parts, each tested below:

1. **Detect** the trailing run (`_trailing_stale_quote_run`).
2. **Trim** it before computing anything (`trim_carried_forward_tail`).
3. **Judge freshness on the last real print**, not the last stored row
   (`DataEngine.assert_fresh`). This is stricter than a blanket refusal for names
   that really are dead, and more permissive for names that merely had a quiet day.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from app.core.exceptions import StaleDataError
from app.data.engine import (
    STALE_QUOTE_RUN_LIMIT,
    DataEngine,
    _trailing_stale_quote_run,
    trim_carried_forward_tail,
)
from app.database import repository as repo
from tests.conftest import FakeProvider, make_bars


def carry_forward(frame: pd.DataFrame, n: int) -> pd.DataFrame:
    """Replace the final ``n`` bars with a repeat of the last real close, volume 0.

    Reproduces exactly what Yahoo does for a Chilean ticker that has not printed.
    """
    out = frame.copy()
    last_close = float(out["close"].iloc[-n - 1])
    for column in ("open", "high", "low", "close"):
        out.iloc[-n:, out.columns.get_loc(column)] = last_close
    out.iloc[-n:, out.columns.get_loc("volume")] = 0.0
    return out


def store(session, spec, frame) -> DataEngine:
    """Persist ``frame`` for ``spec`` through the engine and return the engine."""
    engine = DataEngine(session, provider=FakeProvider({spec.symbol: frame}))
    repo.sync_markets(session)
    engine.download_symbol(spec, incremental=False)
    return engine


# --------------------------------------------------------------------------- #
# 1. Detection
# --------------------------------------------------------------------------- #


class TestRunDetection:
    def test_clean_series_has_no_run(self, bars: pd.DataFrame) -> None:
        assert _trailing_stale_quote_run(bars) == 0

    @pytest.mark.parametrize("n", [1, 3, 8, 49])
    def test_counts_the_trailing_run(self, bars: pd.DataFrame, n: int) -> None:
        assert _trailing_stale_quote_run(carry_forward(bars, n)) == n

    def test_stops_at_the_first_real_bar(self, bars: pd.DataFrame) -> None:
        """Only the trailing run counts, not dead sessions earlier in the history."""
        frame = carry_forward(bars, 4)
        frame.iloc[100, frame.columns.get_loc("volume")] = 0.0
        assert _trailing_stale_quote_run(frame) == 4

    def test_a_flat_bar_with_volume_is_not_stale(self, bars: pd.DataFrame) -> None:
        """A price that genuinely did not move, on real volume, is real data."""
        frame = bars.copy()
        last = len(frame) - 1
        close = float(frame["close"].iloc[last])
        for column in ("open", "high", "low", "close"):
            frame.iloc[last, frame.columns.get_loc(column)] = close
        frame.iloc[last, frame.columns.get_loc("volume")] = 500_000.0
        assert _trailing_stale_quote_run(frame) == 0

    def test_a_zero_volume_bar_with_range_is_not_counted(self, bars: pd.DataFrame) -> None:
        """Volume reporting can fail while the price data is genuine."""
        frame = bars.copy()
        frame.iloc[-1, frame.columns.get_loc("volume")] = 0.0
        assert _trailing_stale_quote_run(frame) == 0

    def test_empty_frame_is_safe(self) -> None:
        assert _trailing_stale_quote_run(pd.DataFrame()) == 0

    def test_entirely_flat_series_counts_every_bar(self, flat_bars: pd.DataFrame) -> None:
        assert _trailing_stale_quote_run(flat_bars) == len(flat_bars)


# --------------------------------------------------------------------------- #
# 2. Trimming
# --------------------------------------------------------------------------- #


class TestTrimming:
    def test_clean_series_is_returned_unchanged(self, bars: pd.DataFrame) -> None:
        trimmed, dropped = trim_carried_forward_tail(bars)
        assert dropped == 0
        assert trimmed is bars

    @pytest.mark.parametrize("n", [1, 5, 49])
    def test_removes_exactly_the_invented_tail(self, bars: pd.DataFrame, n: int) -> None:
        frame = carry_forward(bars, n)
        trimmed, dropped = trim_carried_forward_tail(frame)

        assert dropped == n
        assert len(trimmed) == len(frame) - n
        # What remains is the untouched original history.
        pd.testing.assert_frame_equal(trimmed, bars.iloc[: len(bars) - n])

    def test_preserves_earlier_dead_sessions(self, bars: pd.DataFrame) -> None:
        """Trimming is for the fabricated tail only. A mid-history gap is a fact."""
        frame = carry_forward(bars, 3)
        frame.iloc[50, frame.columns.get_loc("volume")] = 0.0

        trimmed, dropped = trim_carried_forward_tail(frame)
        assert dropped == 3
        assert trimmed["volume"].iloc[50] == 0.0

    def test_entirely_flat_series_trims_to_empty(self, flat_bars: pd.DataFrame) -> None:
        """Nothing real to keep. Returning an empty frame beats returning fiction."""
        trimmed, dropped = trim_carried_forward_tail(flat_bars)
        assert trimmed.empty
        assert dropped == len(flat_bars)


class TestLoadIntegration:
    def test_load_keeps_the_tail_by_default(self, session, usa_spec) -> None:
        """A plain load shows what is stored -- an audit must see the fiction."""
        frame = carry_forward(make_bars(300, seed=31), 6)
        engine = store(session, usa_spec, frame)

        loaded = engine.load_spec(usa_spec)
        assert len(loaded) == 300
        assert loaded.attrs["carried_forward_dropped"] == 0

    def test_load_can_trim_and_reports_how_many(self, session, usa_spec) -> None:
        frame = carry_forward(make_bars(300, seed=31), 6)
        engine = store(session, usa_spec, frame)

        loaded = engine.load_spec(usa_spec, trim_carried_forward=True)
        assert len(loaded) == 294
        assert loaded.attrs["carried_forward_dropped"] == 6

    def test_trimmed_load_shifts_the_effective_as_of_date(self, session, usa_spec) -> None:
        """The caller must be able to see that "as of" moved, not just get a shorter frame."""
        frame = carry_forward(make_bars(300, seed=31), 6)
        engine = store(session, usa_spec, frame)

        untrimmed = engine.load_spec(usa_spec)
        trimmed = engine.load_spec(usa_spec, trim_carried_forward=True)
        assert trimmed.index[-1] < untrimmed.index[-1]

    def test_missing_asset_still_reports_the_attribute(self, session) -> None:
        engine = DataEngine(session)
        loaded = engine.load("AAPL", "USA", trim_carried_forward=True)
        assert loaded.empty
        assert loaded.attrs["carried_forward_dropped"] == 0


# --------------------------------------------------------------------------- #
# 3. Freshness, judged on the last real print
# --------------------------------------------------------------------------- #


class TestFreshnessGuard:
    """``assert_fresh`` trims the invented tail, then applies the age tolerance."""

    def _series_ending(self, last_real: pd.Timestamp, n_stale: int, periods: int = 300):
        """Business-day series whose last *real* print is ``last_real``.

        ``n_stale`` carried-forward bars are appended after it, so the stored series
        ends later than the last genuine observation -- the SQM-B shape.
        """
        index = pd.bdate_range(end=last_real, periods=periods, tz="UTC", name="ts")
        frame = make_bars(len(index), seed=41)
        frame.index = index

        if n_stale:
            extra = pd.bdate_range(
                start=last_real + timedelta(days=1), periods=n_stale, tz="UTC", name="ts"
            )
            tail = pd.DataFrame(
                {
                    "open": float(frame["close"].iloc[-1]),
                    "high": float(frame["close"].iloc[-1]),
                    "low": float(frame["close"].iloc[-1]),
                    "close": float(frame["close"].iloc[-1]),
                    "volume": 0.0,
                },
                index=extra,
            )
            frame = pd.concat([frame, tail])
        return frame

    def test_refuses_when_the_last_real_print_is_too_old(self, session, chile_spec) -> None:
        """The SQM-B case: series dated today, genuine trading stopped weeks ago."""
        as_of = pd.Timestamp("2026-09-25", tz="UTC")
        last_real = pd.Timestamp("2026-07-15", tz="UTC")
        frame = self._series_ending(last_real, n_stale=49)
        engine = store(session, chile_spec, frame)

        with pytest.raises(StaleDataError) as excinfo:
            engine.assert_fresh(chile_spec, as_of=as_of.to_pydatetime())

        message = str(excinfo.value)
        assert "last traded 2026-07-15" in message
        assert "zero volume" in message
        assert "nobody offered" in message

    def test_accepts_a_recent_print_behind_a_short_invented_tail(
        self, session, chile_spec
    ) -> None:
        """The permissive half of the change, and the reason it is not a blanket ban.

        A thin instrument that printed two days ago and has two carried-forward bars
        after it is perfectly usable. The previous implementation refused any run at
        or above the threshold regardless of how recent the real print was.
        """
        as_of = pd.Timestamp("2026-09-25", tz="UTC")
        last_real = pd.Timestamp("2026-09-23", tz="UTC")
        frame = self._series_ending(last_real, n_stale=STALE_QUOTE_RUN_LIMIT + 3)
        engine = store(session, chile_spec, frame)

        engine.assert_fresh(chile_spec, as_of=as_of.to_pydatetime())  # must not raise

    def test_a_genuinely_fresh_clean_series_passes(self, session, usa_spec) -> None:
        as_of = pd.Timestamp("2026-09-25", tz="UTC")
        frame = self._series_ending(pd.Timestamp("2026-09-24", tz="UTC"), n_stale=0)
        engine = store(session, usa_spec, frame)

        engine.assert_fresh(usa_spec, as_of=as_of.to_pydatetime())

    def test_a_clean_but_old_series_is_still_refused(self, session, usa_spec) -> None:
        """The ordinary staleness path must keep working."""
        as_of = pd.Timestamp("2026-09-25", tz="UTC")
        frame = self._series_ending(pd.Timestamp("2026-06-01", tz="UTC"), n_stale=0)
        engine = store(session, usa_spec, frame)

        with pytest.raises(StaleDataError, match="beyond the"):
            engine.assert_fresh(usa_spec, as_of=as_of.to_pydatetime())

    def test_an_entirely_fabricated_series_is_refused(self, session, chile_spec) -> None:
        frame = make_bars(60, seed=43)
        for column in ("open", "high", "low", "close"):
            frame[column] = 1000.0
        frame["volume"] = 0.0
        engine = store(session, chile_spec, frame)

        with pytest.raises(StaleDataError, match="no real prints at all"):
            engine.assert_fresh(chile_spec)

    def test_missing_data_raises_a_clear_error(self, session, usa_spec) -> None:
        engine = DataEngine(session)
        with pytest.raises(StaleDataError, match="No stored bars"):
            engine.assert_fresh(usa_spec)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


class TestAuditReporting:
    def test_audit_reports_the_run(self, session, chile_spec) -> None:
        frame = carry_forward(make_bars(400, seed=21), 8)
        engine = store(session, chile_spec, frame)

        report = engine.audit_symbol(chile_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.stale_quote_run == 8
        assert report.has_findings
        assert "carried-forward" in report.summary()

    def test_audit_is_clean_for_a_real_series(self, session, usa_spec) -> None:
        frame = make_bars(400, seed=22)
        engine = store(session, usa_spec, frame)

        report = engine.audit_symbol(usa_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.stale_quote_run == 0
        assert "carried-forward" not in report.summary()

    def test_one_quiet_session_does_not_flag_findings(self, session, chile_spec) -> None:
        """A thin instrument with a single quiet day is not a data-quality problem.

        Before the threshold was applied to ``has_findings``, every Chilean name with
        one carried-forward bar was reported as having findings, which made the audit
        output impossible to triage.
        """
        frame = carry_forward(make_bars(400, seed=23), 1)
        engine = store(session, chile_spec, frame)

        report = engine.audit_symbol(chile_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.stale_quote_run == 1
        assert report.has_findings is False

    def test_the_threshold_is_where_flagging_begins(self, session, chile_spec) -> None:
        frame = carry_forward(make_bars(400, seed=24), STALE_QUOTE_RUN_LIMIT)
        engine = store(session, chile_spec, frame)

        report = engine.audit_symbol(chile_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.has_findings is True


# --------------------------------------------------------------------------- #
# Index series are exempt
# --------------------------------------------------------------------------- #


class TestIndexExemption:
    """An index level series is flat and volumeless by nature, not by fabrication.

    Without this exemption the IPSA -- whose bars come from the Banco Central as a
    level with open=high=low=close and volume=0 -- would be classified as entirely
    carried forward, trimmed to nothing, and refused as a benchmark.
    """

    def _index_spec(self):
        from app.core.universe import AssetSpec

        return AssetSpec(
            symbol="TESTIDX",
            name="Test index",
            market="CHILE",
            asset_class="index",
            is_benchmark=True,
            provider_symbols={"fake": ("TESTIDX",)},
        )

    def _level_series(self, n: int = 300) -> pd.DataFrame:
        """A level series shaped the way an index provider returns one."""
        frame = make_bars(n, seed=51)
        for column in ("open", "high", "low"):
            frame[column] = frame["close"]
        frame["volume"] = 0.0
        return frame

    def test_audit_does_not_flag_an_index(self, session) -> None:
        spec = self._index_spec()
        frame = self._level_series()
        engine = store(session, spec, frame)

        report = engine.audit_symbol(spec, as_of=frame.index[-1].to_pydatetime())
        assert report.stored_bars == 300
        assert report.stale_quote_run == 0
        assert "carried-forward" not in report.summary()

    def test_trimming_leaves_an_index_untouched(self, session) -> None:
        spec = self._index_spec()
        engine = store(session, spec, self._level_series())

        loaded = engine.load_spec(spec, trim_carried_forward=True)
        assert len(loaded) == 300
        assert loaded.attrs["carried_forward_dropped"] == 0

    def test_an_index_can_still_be_judged_fresh(self, session) -> None:
        """Freshness by bar age must keep working for indices; only the tail check is skipped."""
        spec = self._index_spec()
        frame = self._level_series()
        engine = store(session, spec, frame)

        engine.assert_fresh(spec, as_of=frame.index[-1].to_pydatetime())

    def test_a_stale_index_is_still_refused(self, session) -> None:
        spec = self._index_spec()
        frame = self._level_series()
        engine = store(session, spec, frame)

        very_late = frame.index[-1].to_pydatetime() + timedelta(days=90)
        with pytest.raises(StaleDataError, match="beyond the"):
            engine.assert_fresh(spec, as_of=very_late)

    def test_an_equity_with_the_same_shape_is_still_flagged(self, session, chile_spec) -> None:
        """The exemption is keyed on asset_class, not on the data looking flat.

        A traded instrument whose bars are flat and volumeless is the fabricated case
        this whole mechanism exists to catch, and must not be let through.
        """
        engine = store(session, chile_spec, self._level_series())
        report = engine.audit_symbol(chile_spec)
        assert report.stale_quote_run == 300


# --------------------------------------------------------------------------- #
# Degraded volume feed -- a different failure from a carried-forward tail
# --------------------------------------------------------------------------- #


class TestVolumeFeedDegradation:
    """Bars with a real price range but zero reported volume.

    Found by running the Phase 2 scanner against live data: 17 of the last 20 *real*
    bars for several Chilean names carried genuine price movement and zero volume, while
    every US name was clean. These are not carried-forward quotes, so the flat-tail
    detector correctly leaves them alone — but every volume-derived feature becomes zero
    or undefined, and a volume-gated strategy silently stops firing. Undetected, that
    reads as "no setups" rather than "the input is broken".
    """

    def _with_zero_volume_tail(self, n_bars: int, n_zero: int) -> pd.DataFrame:
        """Real price movement throughout; the last ``n_zero`` bars report no volume."""
        frame = make_bars(n_bars, seed=61)
        if n_zero:
            frame.iloc[-n_zero:, frame.columns.get_loc("volume")] = 0.0
        return frame

    def test_flags_a_mostly_volumeless_recent_window(self, session, chile_spec) -> None:
        frame = self._with_zero_volume_tail(400, 17)
        engine = store(session, chile_spec, frame)

        report = engine.audit_symbol(chile_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.volume_feed_degraded is True
        assert report.recent_zero_volume_pct == pytest.approx(85.0)
        assert report.has_findings

    def test_a_clean_feed_is_not_flagged(self, session, usa_spec) -> None:
        frame = self._with_zero_volume_tail(400, 0)
        engine = store(session, usa_spec, frame)

        report = engine.audit_symbol(usa_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.volume_feed_degraded is False
        assert report.recent_zero_volume_pct == pytest.approx(0.0)

    def test_an_occasional_quiet_session_is_not_degradation(self, session, chile_spec) -> None:
        """Two zero-volume days in twenty is a thin instrument, not a broken feed."""
        frame = self._with_zero_volume_tail(400, 2)
        engine = store(session, chile_spec, frame)

        report = engine.audit_symbol(chile_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.recent_zero_volume_pct == pytest.approx(10.0)
        assert report.volume_feed_degraded is False

    def test_measured_separately_from_the_carried_forward_tail(
        self, session, chile_spec
    ) -> None:
        """The two failures are distinct and must be counted independently.

        A flat zero-volume tail is trimmed; a real-price zero-volume run is kept. Folding
        them together would let the trimmed bars inflate the volume statistic and hide
        which problem a given instrument actually has.
        """
        frame = self._with_zero_volume_tail(400, 17)
        frame = carry_forward(frame, 5)
        engine = store(session, chile_spec, frame)

        report = engine.audit_symbol(chile_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.stale_quote_run == 5
        assert report.volume_feed_degraded is True
        summary = report.summary()
        assert "carried-forward" in summary
        assert "volume feed degraded" in summary

    def test_summary_states_the_percentage(self, session, chile_spec) -> None:
        frame = self._with_zero_volume_tail(400, 20)
        engine = store(session, chile_spec, frame)
        report = engine.audit_symbol(chile_spec, as_of=frame.index[-1].to_pydatetime())
        assert "100%" in report.summary()
