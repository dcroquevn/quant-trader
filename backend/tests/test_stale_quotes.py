"""Carried-forward (stale) quote detection.

Motivated by a real observation, not a hypothetical. On 2026-09-27 Yahoo served
eight consecutive identical zero-volume daily bars for ``SQM-B.SN`` and five for
``CHILE.SN``, all dated to that week. The series therefore *looked* current while
containing no recent trading at all.

This is more dangerous than ordinary stale data. An old last bar is obvious --
its date gives it away. A carried-forward quote is dated today, passes every
freshness check based on timestamps, and produces indicator values that look
perfectly reasonable. A signal generated from one is a signal about a price nobody
offered.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.core.exceptions import StaleDataError
from app.data.engine import (
    STALE_QUOTE_RUN_LIMIT,
    DataEngine,
    _trailing_stale_quote_run,
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


class TestRunDetection:
    def test_clean_series_has_no_run(self, bars: pd.DataFrame) -> None:
        assert _trailing_stale_quote_run(bars) == 0

    @pytest.mark.parametrize("n", [1, 3, 8])
    def test_counts_the_trailing_run(self, bars: pd.DataFrame, n: int) -> None:
        assert _trailing_stale_quote_run(carry_forward(bars, n)) == n

    def test_stops_at_the_first_real_bar(self, bars: pd.DataFrame) -> None:
        """Only the trailing run counts, not dead sessions earlier in the history."""
        frame = carry_forward(bars, 4)
        # A dead session in the middle must not extend the trailing count.
        middle = frame.columns.get_loc("volume")
        frame.iloc[100, middle] = 0.0
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


class TestAuditIntegration:
    def _stored(self, session, spec, frame):
        provider = FakeProvider({spec.symbol: frame})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)
        engine.download_symbol(spec, incremental=False)
        return engine

    def test_audit_reports_the_run(self, session, chile_spec) -> None:
        frame = carry_forward(make_bars(400, seed=21), 8)
        engine = self._stored(session, chile_spec, frame)

        report = engine.audit_symbol(chile_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.stale_quote_run == 8
        assert report.has_findings
        assert "carried-forward" in report.summary()

    def test_audit_is_clean_for_a_real_series(self, session, usa_spec) -> None:
        frame = make_bars(400, seed=22)
        engine = self._stored(session, usa_spec, frame)

        report = engine.audit_symbol(usa_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.stale_quote_run == 0
        assert "carried-forward" not in report.summary()


class TestFreshnessGuard:
    def _stored_recent(self, session, spec, n_stale: int):
        """Store a series ending today, with ``n_stale`` carried-forward bars."""
        index = pd.bdate_range(
            end=pd.Timestamp.now(tz="UTC").normalize(), periods=400, tz="UTC", name="ts"
        )
        frame = make_bars(len(index), seed=23)
        frame.index = index
        if n_stale:
            frame = carry_forward(frame, n_stale)

        provider = FakeProvider({spec.symbol: frame})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)
        engine.download_symbol(spec, incremental=False)
        return engine

    def test_refuses_a_series_ending_in_carried_forward_quotes(
        self, session, chile_spec
    ) -> None:
        """The core guarantee: a current-looking but fictional tail blocks signals."""
        engine = self._stored_recent(session, chile_spec, STALE_QUOTE_RUN_LIMIT + 2)

        with pytest.raises(StaleDataError, match="carrying forward"):
            engine.assert_fresh(chile_spec)

    def test_tolerates_a_short_quiet_run(self, session, chile_spec) -> None:
        """One or two quiet sessions are plausible for a thin instrument."""
        engine = self._stored_recent(session, chile_spec, STALE_QUOTE_RUN_LIMIT - 1)
        engine.assert_fresh(chile_spec)  # must not raise

    def test_a_genuinely_fresh_series_passes(self, session, usa_spec) -> None:
        engine = self._stored_recent(session, usa_spec, 0)
        engine.assert_fresh(usa_spec)

    def test_the_error_explains_why_it_matters(self, session, chile_spec) -> None:
        engine = self._stored_recent(session, chile_spec, 6)
        with pytest.raises(StaleDataError) as excinfo:
            engine.assert_fresh(chile_spec)

        message = str(excinfo.value)
        assert "zero volume" in message
        assert "nobody offered" in message
