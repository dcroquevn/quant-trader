"""DataEngine tests: universe sync, resolution, incremental refresh, integrity.

All offline, driven by ``FakeProvider``. The behaviours worth guarding:

* an incremental refresh asks for an **overlapping** window, not the next day;
* one failing symbol must not abort a universe download;
* gap detection reports *candidates* and does not claim to know about holidays;
* nothing is ever interpolated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.core.exceptions import StaleDataError
from app.core.universe import AssetSpec, find_asset
from app.data.engine import INCREMENTAL_OVERLAP_DAYS, DataEngine
from app.data.provider import Timeframe
from app.database import repository as repo
from tests.conftest import FakeProvider, make_bars


@pytest.fixture
def engine_with_fake(session, fake_provider):
    engine = DataEngine(session, provider=fake_provider)
    repo.sync_markets(session)
    return engine


class TestUniverseSync:
    def test_writes_every_asset_and_market(self, session) -> None:
        engine = DataEngine(session)
        count = engine.sync_universe()
        assert count > 30  # 15 US + 18 Chilean + benchmarks
        assert len(repo.list_assets(session, include_benchmarks=True)) == count

    def test_is_idempotent(self, session) -> None:
        engine = DataEngine(session)
        first = engine.sync_universe()
        second = engine.sync_universe()
        assert first == second
        assert len(repo.list_assets(session, include_benchmarks=True)) == first

    def test_includes_benchmarks(self, session) -> None:
        """Benchmarks must be downloadable even though they are never traded."""
        DataEngine(session).sync_universe()
        spy = repo.get_asset(session, "SPY", "USA")
        ech = repo.get_asset(session, "ECH", "USA")
        assert spy is not None
        assert ech is not None

    def test_stores_data_limitation_notes(self, session) -> None:
        DataEngine(session).sync_universe()
        latam = repo.get_asset(session, "LTM", "CHILE")
        assert latam is not None
        assert "Chapter 11" in latam.notes


class TestSymbolResolution:
    def test_resolves_and_caches(self, engine_with_fake, session, usa_spec, fake_provider) -> None:
        provider, ticker = engine_with_fake.resolve(usa_spec)
        assert (provider, ticker) == ("fake", "TESTUS")

        calls_after_first = len(fake_provider.requests)
        engine_with_fake.resolve(usa_spec)
        assert len(fake_provider.requests) == calls_after_first, "resolution was not cached"

    def test_cache_survives_in_the_database(self, engine_with_fake, session, usa_spec) -> None:
        engine_with_fake.resolve(usa_spec)
        row = repo.get_asset(session, "TESTUS", "USA")
        assert row is not None
        assert row.provider == "fake"
        assert row.provider_symbol == "TESTUS"
        assert row.resolution_checked_at is not None

    def test_force_reprobes(self, engine_with_fake, usa_spec, fake_provider) -> None:
        engine_with_fake.resolve(usa_spec)
        before = len(fake_provider.requests)
        engine_with_fake.resolve(usa_spec, force=True)
        assert len(fake_provider.requests) > before

    def test_tries_candidates_in_order(self, session, bars) -> None:
        frame = bars.copy()
        provider = FakeProvider({"THIRD": frame})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)

        spec = AssetSpec(
            symbol="MULTI",
            name="Multi-candidate",
            market="USA",
            provider_symbols={"fake": ("FIRST", "SECOND", "THIRD")},
        )
        _, resolved = engine.resolve(spec)
        assert resolved == "THIRD"

    def test_unavailable_instrument_fails_immediately(self, engine_with_fake) -> None:
        """An instrument with no declared candidates -- the IPSA -- must fail fast.

        No probing, because there is nothing to probe: the provider is known not to
        carry it.
        """
        from app.core.exceptions import SymbolNotFoundError

        ipsa = find_asset("IPSA", "CHILE")
        with pytest.raises(SymbolNotFoundError):
            engine_with_fake.resolve(ipsa)


class TestDownload:
    def test_stores_bars(self, engine_with_fake, session, usa_spec, bars) -> None:
        result = engine_with_fake.download_symbol(usa_spec, Timeframe.D1, incremental=False)
        assert result.ok
        assert result.status == "OK"
        assert result.bars_written == len(bars)

        asset = repo.get_asset(session, "TESTUS", "USA")
        assert repo.bar_count(session, asset.id, "1D") == len(bars)

    def test_is_idempotent(self, engine_with_fake, session, usa_spec, bars) -> None:
        engine_with_fake.download_symbol(usa_spec, incremental=False)
        engine_with_fake.download_symbol(usa_spec, incremental=False)
        asset = repo.get_asset(session, "TESTUS", "USA")
        assert repo.bar_count(session, asset.id, "1D") == len(bars)

    def test_incremental_refetches_an_overlap(
        self, engine_with_fake, session, usa_spec, bars, fake_provider
    ) -> None:
        """The newest stored bar may have been provisional, so re-fetch it.

        Starting exactly where the last download stopped would permanently keep a
        mid-session bar that was never corrected.
        """
        engine_with_fake.download_symbol(usa_spec, incremental=False)
        newest = bars.index[-1]

        fake_provider.requests.clear()
        engine_with_fake.download_symbol(usa_spec, incremental=True)

        assert fake_provider.requests, "incremental download made no request"
        _, _, requested_start, _ = fake_provider.requests[-1]
        expected = newest.to_pydatetime() - timedelta(days=INCREMENTAL_OVERLAP_DAYS)
        assert requested_start == expected

    def test_incremental_on_an_empty_database_fetches_everything(
        self, engine_with_fake, usa_spec, bars
    ) -> None:
        result = engine_with_fake.download_symbol(usa_spec, incremental=True)
        assert result.bars_written == len(bars)

    def test_reports_failure_without_raising(self, session, bars) -> None:
        """A universe download must survive one missing ticker."""
        provider = FakeProvider({})  # resolves nothing
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)

        spec = AssetSpec(
            symbol="GHOST", name="Delisted", market="USA",
            provider_symbols={"fake": ("GHOST",)},
        )
        result = engine.download_symbol(spec)
        assert result.ok is False
        assert result.status == "FAILED"
        assert result.error

    def test_universe_download_continues_past_failures(self, session, bars) -> None:
        provider = FakeProvider({"GOOD": bars.copy()})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)

        specs = (
            AssetSpec(symbol="GOOD", name="ok", market="USA",
                      provider_symbols={"fake": ("GOOD",)}),
            AssetSpec(symbol="BAD", name="missing", market="USA",
                      provider_symbols={"fake": ("BAD",)}),
            AssetSpec(symbol="ALSOGOOD", name="ok2", market="USA",
                      provider_symbols={"fake": ("GOOD",)}),
        )
        for spec in specs:
            repo.upsert_asset(session, spec)
        results = [engine.download_symbol(s) for s in specs]

        assert [r.ok for r in results] == [True, False, True]

    def test_records_the_provider_and_resolved_ticker(
        self, engine_with_fake, usa_spec
    ) -> None:
        result = engine_with_fake.download_symbol(usa_spec, incremental=False)
        assert result.provider == "fake"
        assert result.provider_symbol == "TESTUS"

    def test_reports_dropped_invalid_bars(self, session, bars) -> None:
        corrupted = bars.iloc[:100].copy()
        corrupted.iloc[10, corrupted.columns.get_loc("volume")] = -1.0

        provider = FakeProvider({"X": corrupted})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)
        spec = AssetSpec(symbol="X", name="x", market="USA",
                         provider_symbols={"fake": ("X",)})

        result = engine.download_symbol(spec, incremental=False)
        assert result.ok
        # validate_ohlcv drops it before it reaches the database.
        assert result.bars_written == 99


class TestGapDetection:
    def test_clean_series_reports_no_gaps(self, session, usa_spec) -> None:
        """A business-day series has no missing weekdays by construction."""
        frame = make_bars(200, seed=3)
        provider = FakeProvider({"TESTUS": frame})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)
        engine.download_symbol(usa_spec, incremental=False)

        report = engine.audit_symbol(usa_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.missing_weekdays == []
        assert report.duplicate_timestamps == []
        assert report.stored_bars == 200

    def test_detects_a_removed_block_of_weekdays(self, session, usa_spec) -> None:
        frame = make_bars(200, seed=3)
        # Drop a full week from the middle.
        with_hole = pd.concat([frame.iloc[:100], frame.iloc[105:]])

        provider = FakeProvider({"TESTUS": with_hole})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)
        engine.download_symbol(usa_spec, incremental=False)

        report = engine.audit_symbol(usa_spec, as_of=frame.index[-1].to_pydatetime())
        assert len(report.missing_weekdays) == 5
        # Five absent weekdays span a weekend, so they are three calendar days plus
        # two -- but one run of five sessions, which is the number that matters.
        assert report.largest_gap_sessions == 5
        assert report.has_findings

    def test_weekends_are_not_reported_as_gaps(self, session, usa_spec) -> None:
        frame = make_bars(60, seed=5)
        provider = FakeProvider({"TESTUS": frame})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)
        engine.download_symbol(usa_spec, incremental=False)

        report = engine.audit_symbol(usa_spec, as_of=frame.index[-1].to_pydatetime())
        assert report.missing_weekdays == []

    def test_reports_nothing_stored_for_an_unknown_asset(self, session, usa_spec) -> None:
        engine = DataEngine(session)
        report = engine.audit_symbol(usa_spec)
        assert report.stored_bars == 0
        assert report.summary() == "no data stored"

    def test_summary_is_human_readable(self, session, usa_spec) -> None:
        frame = make_bars(100, seed=9)
        with_hole = pd.concat([frame.iloc[:50], frame.iloc[53:]])
        provider = FakeProvider({"TESTUS": with_hole})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)
        engine.download_symbol(usa_spec, incremental=False)

        summary = engine.audit_symbol(
            usa_spec, as_of=frame.index[-1].to_pydatetime()
        ).summary()
        assert "bars" in summary
        assert "missing weekdays" in summary


class TestStaleness:
    def test_recent_data_passes(self, session, usa_spec, recent_bars) -> None:
        provider = FakeProvider({"TESTUS": recent_bars})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)
        engine.download_symbol(usa_spec, incremental=False)

        engine.assert_fresh(usa_spec)  # must not raise

    def test_old_data_raises(self, session, usa_spec, stale_bars) -> None:
        """Acting on stale data means trading last month's setup at today's price."""
        provider = FakeProvider({"TESTUS": stale_bars})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)
        engine.download_symbol(usa_spec, incremental=False)

        with pytest.raises(StaleDataError, match="beyond the"):
            engine.assert_fresh(usa_spec)

    def test_missing_data_raises_a_clear_error(self, session, usa_spec) -> None:
        engine = DataEngine(session)
        with pytest.raises(StaleDataError, match="No stored bars"):
            engine.assert_fresh(usa_spec)

    def test_audit_quantifies_the_staleness(self, session, usa_spec, stale_bars) -> None:
        provider = FakeProvider({"TESTUS": stale_bars})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)
        engine.download_symbol(usa_spec, incremental=False)

        report = engine.audit_symbol(usa_spec)
        assert report.is_stale
        assert report.stale_by_days > 0


class TestCoverageAndLoading:
    def test_coverage_lists_stored_ranges(self, engine_with_fake, usa_spec, bars) -> None:
        engine_with_fake.download_symbol(usa_spec, incremental=False)
        coverage = engine_with_fake.coverage("1D")
        row = coverage[coverage["symbol"] == "TESTUS"].iloc[0]
        assert row["bars"] == len(bars)
        assert row["provider"] == "fake"

    def test_load_returns_stored_bars(self, engine_with_fake, session, usa_spec, bars) -> None:
        engine_with_fake.download_symbol(usa_spec, incremental=False)
        # `load` resolves through the declared universe, so register the test spec.
        frame = repo.load_bars(
            session, repo.get_asset(session, "TESTUS", "USA").id, "1D"
        )
        assert len(frame) == len(bars)

    def test_load_of_an_unknown_symbol_returns_empty(self, session) -> None:
        engine = DataEngine(session)
        frame = engine.load("AAPL", "USA")
        assert frame.empty


class TestNoInterpolation:
    def test_gaps_are_never_filled(self, session, usa_spec) -> None:
        """The hole must still be a hole after a full download/store/load cycle.

        Filling it would make the series look continuous and hand every rolling
        indicator fabricated observations.
        """
        frame = make_bars(200, seed=17)
        with_hole = pd.concat([frame.iloc[:100], frame.iloc[110:]])

        provider = FakeProvider({"TESTUS": with_hole})
        engine = DataEngine(session, provider=provider)
        repo.sync_markets(session)
        engine.download_symbol(usa_spec, incremental=False)

        asset = repo.get_asset(session, "TESTUS", "USA")
        stored = repo.load_bars(session, asset.id, "1D")
        assert len(stored) == 190
        for missing in frame.index[100:110]:
            assert missing not in stored.index
