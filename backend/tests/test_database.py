"""Database schema and persistence tests.

The behaviour that matters most here is **upsert idempotency**. Incremental
downloads deliberately re-fetch an overlapping window, so writing the same bars
twice must update rows rather than duplicate them. A duplicated bar silently
doubles a day's weight in every average the strategy computes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.core.universe import AssetSpec, find_asset
from app.database import repository as repo
from app.database.models import Asset, Bar, Market, Order, Signal
from app.indicators.registry import compute_features


class TestSchema:
    def test_every_declared_table_exists(self, engine) -> None:
        expected = {
            "markets", "assets", "bars", "features", "strategies", "signals",
            "backtests", "optimization_runs", "walk_forward_runs", "trades",
            "orders", "portfolio_snapshots", "risk_events",
        }
        actual = set(inspect(engine).get_table_names())
        assert expected <= actual, f"missing tables: {sorted(expected - actual)}"

    def test_bars_have_the_indexes_queries_rely_on(self, engine) -> None:
        """The hot query is one asset's series over a date range."""
        indexes = {ix["name"] for ix in inspect(engine).get_indexes("bars")}
        assert "ix_bar_asset_tf_ts" in indexes
        assert "ix_bar_tf_ts" in indexes

    def test_foreign_keys_are_enforced(self, session) -> None:
        """SQLite ignores foreign keys unless the pragma is set; verify it is."""
        session.add(
            Bar(
                asset_id=99999,
                timeframe="1D",
                ts=datetime(2024, 1, 2, tzinfo=timezone.utc),
                open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_bar_check_constraints_reject_impossible_prices(self, session, usa_spec) -> None:
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        session.add(
            Bar(
                asset_id=asset.id, timeframe="1D",
                ts=datetime(2024, 1, 2, tzinfo=timezone.utc),
                open=10.0, high=5.0, low=8.0, close=9.0, volume=1.0,  # high < low
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_negative_volume_is_rejected(self, session, usa_spec) -> None:
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        session.add(
            Bar(
                asset_id=asset.id, timeframe="1D",
                ts=datetime(2024, 1, 2, tzinfo=timezone.utc),
                open=10.0, high=11.0, low=9.0, close=10.0, volume=-5.0,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_signal_action_is_constrained(self, session, usa_spec) -> None:
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        session.add(
            Signal(
                asset_id=asset.id,
                ts=datetime(2024, 1, 2, tzinfo=timezone.utc),
                action="MAYBE",
                score=0.5,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_optimization_run_cannot_claim_a_non_train_split(self, session) -> None:
        """A database-level guard against optimising on validation or test data."""
        from app.database.models import OptimizationRun

        session.add(OptimizationRun(label="leak", method="grid", split="test"))
        with pytest.raises(IntegrityError):
            session.flush()


class TestMarketAndAssetSync:
    def test_sync_markets_writes_both_markets(self, session) -> None:
        count = repo.sync_markets(session)
        assert count == 2
        assert session.scalar(select(func.count()).select_from(Market)) == 2

    def test_sync_markets_is_idempotent(self, session) -> None:
        repo.sync_markets(session)
        repo.sync_markets(session)
        assert session.scalar(select(func.count()).select_from(Market)) == 2

    def test_upsert_asset_is_idempotent(self, session, usa_spec) -> None:
        repo.sync_markets(session)
        first = repo.upsert_asset(session, usa_spec)
        second = repo.upsert_asset(session, usa_spec)
        assert first.id == second.id
        assert session.scalar(select(func.count()).select_from(Asset)) == 1

    def test_upsert_asset_preserves_symbol_resolution(self, session, usa_spec) -> None:
        """Re-syncing the universe must not discard a verified provider symbol.

        Resolution costs a network round trip per instrument; throwing it away on
        every sync would make the universe sync quietly expensive.
        """
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        asset.provider = "yfinance"
        asset.provider_symbol = "TESTUS"
        asset.resolution_checked_at = datetime.now(timezone.utc)
        session.flush()

        repo.upsert_asset(session, usa_spec)
        assert asset.provider == "yfinance"
        assert asset.provider_symbol == "TESTUS"

    def test_same_symbol_can_exist_in_two_markets(self, session) -> None:
        """CAP and CCU-style collisions across venues must not clash."""
        repo.sync_markets(session)
        repo.upsert_asset(session, AssetSpec(symbol="DUAL", name="US listing", market="USA"))
        repo.upsert_asset(session, AssetSpec(symbol="DUAL", name="CL listing", market="CHILE"))
        assert session.scalar(select(func.count()).select_from(Asset)) == 2

    def test_list_assets_filters_by_market(self, session) -> None:
        repo.sync_markets(session)
        repo.upsert_asset(session, AssetSpec(symbol="A1", name="a", market="USA"))
        repo.upsert_asset(session, AssetSpec(symbol="B1", name="b", market="CHILE"))
        assert [a.symbol for a in repo.list_assets(session, market="USA")] == ["A1"]


class TestBarPersistence:
    @pytest.fixture
    def asset_id(self, session, usa_spec) -> int:
        repo.sync_markets(session)
        return repo.upsert_asset(session, usa_spec).id

    def test_writes_every_valid_bar(self, session, asset_id, bars) -> None:
        written, skipped = repo.upsert_bars(
            session, asset_id, "1D", bars, source="test", is_adjusted=False
        )
        assert written == len(bars)
        assert skipped == 0
        assert repo.bar_count(session, asset_id, "1D") == len(bars)

    def test_re_writing_the_same_window_does_not_duplicate(
        self, session, asset_id, bars
    ) -> None:
        """The property incremental downloads depend on."""
        repo.upsert_bars(session, asset_id, "1D", bars, source="test", is_adjusted=False)
        repo.upsert_bars(session, asset_id, "1D", bars, source="test", is_adjusted=False)
        assert repo.bar_count(session, asset_id, "1D") == len(bars)

    def test_overlapping_window_updates_in_place(self, session, asset_id, bars) -> None:
        repo.upsert_bars(session, asset_id, "1D", bars.iloc[:100], source="v1", is_adjusted=False)

        revised = bars.iloc[90:200].copy()
        revised["close"] = revised["close"] * 1.05
        repo.upsert_bars(session, asset_id, "1D", revised, source="v2", is_adjusted=False)

        assert repo.bar_count(session, asset_id, "1D") == 200
        stored = repo.load_bars(session, asset_id, "1D", use_adjusted=False)
        overlap_ts = bars.index[95]
        assert stored.loc[overlap_ts, "close"] == pytest.approx(bars["close"].iloc[95] * 1.05)
        assert stored.loc[overlap_ts, "source"] == "v2"

    def test_duplicate_timestamps_in_input_keep_the_last(self, session, asset_id, bars) -> None:
        """A provider can return a provisional and a settled bar for one session."""
        head = bars.iloc[:10].copy()
        stale = head.copy()
        stale["close"] = 1.0
        combined = pd.concat([stale, head])  # later rows are the settled ones

        written, _ = repo.upsert_bars(
            session, asset_id, "1D", combined, source="test", is_adjusted=False
        )
        assert written == 10
        stored = repo.load_bars(session, asset_id, "1D", use_adjusted=False)
        assert stored["close"].iloc[0] == pytest.approx(head["close"].iloc[0])

    def test_invalid_bars_are_dropped_not_repaired(self, session, asset_id, bars) -> None:
        """Interpolating a bad bar invents a price; dropping it leaves a visible hole."""
        corrupted = bars.iloc[:50].copy()
        corrupted.iloc[10, corrupted.columns.get_loc("close")] = float("nan")
        corrupted.iloc[20, corrupted.columns.get_loc("low")] = 1e9  # low > high
        corrupted.iloc[30, corrupted.columns.get_loc("open")] = -5.0

        written, skipped = repo.upsert_bars(
            session, asset_id, "1D", corrupted, source="test", is_adjusted=False
        )
        assert skipped == 3
        assert written == 47

        stored = repo.load_bars(session, asset_id, "1D", use_adjusted=False)
        assert corrupted.index[10] not in stored.index
        assert stored["close"].notna().all()

    def test_empty_frame_is_a_no_op(self, session, asset_id) -> None:
        written, skipped = repo.upsert_bars(
            session, asset_id, "1D", pd.DataFrame(), source="test", is_adjusted=False
        )
        assert (written, skipped) == (0, 0)

    def test_missing_columns_raise(self, session, asset_id, bars) -> None:
        with pytest.raises(ValueError, match="missing columns"):
            repo.upsert_bars(
                session, asset_id, "1D", bars.drop(columns=["close"]),
                source="test", is_adjusted=False,
            )

    def test_timeframes_are_stored_independently(self, session, asset_id, bars) -> None:
        repo.upsert_bars(session, asset_id, "1D", bars.iloc[:50], source="t", is_adjusted=False)
        repo.upsert_bars(session, asset_id, "1H", bars.iloc[:30], source="t", is_adjusted=False)
        assert repo.bar_count(session, asset_id, "1D") == 50
        assert repo.bar_count(session, asset_id, "1H") == 30


class TestBarLoading:
    @pytest.fixture
    def loaded(self, session, usa_spec, bars) -> tuple[int, pd.DataFrame]:
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        repo.upsert_bars(session, asset.id, "1D", bars, source="test", is_adjusted=False)
        return asset.id, bars

    def test_returns_ascending_utc_index(self, session, loaded) -> None:
        asset_id, _ = loaded
        frame = repo.load_bars(session, asset_id, "1D")
        assert frame.index.is_monotonic_increasing
        assert str(frame.index.tz) == "UTC"

    def test_round_trips_prices_exactly(self, session, loaded) -> None:
        asset_id, original = loaded
        frame = repo.load_bars(session, asset_id, "1D", use_adjusted=False)
        # check_freq=False: the fixture's index was built by bdate_range and carries
        # freq=BusinessDay, which a database round trip does not preserve. Only the
        # timestamps and values are part of the contract.
        pd.testing.assert_series_equal(
            frame["close"],
            original["close"],
            check_names=False,
            check_freq=False,
            rtol=1e-12,
        )

    def test_date_range_filter_is_inclusive(self, session, loaded) -> None:
        asset_id, original = loaded
        start, end = original.index[10], original.index[20]
        frame = repo.load_bars(session, asset_id, "1D", start=start, end=end)
        assert len(frame) == 11
        assert frame.index[0] == start
        assert frame.index[-1] == end

    def test_missing_asset_returns_empty_frame_not_none(self, session) -> None:
        frame = repo.load_bars(session, 12345, "1D")
        assert frame.empty
        assert "close" in frame.columns

    def test_adjusted_close_scales_the_whole_bar(self, session, usa_spec, bars) -> None:
        """OHLC must stay internally consistent after adjustment.

        Replacing only the close would produce bars where close > high, which every
        downstream validity check would then reject.
        """
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        frame = bars.iloc[:50].copy()
        frame["adj_close"] = frame["close"] * 0.5  # as if a 2:1 split
        repo.upsert_bars(session, asset.id, "1D", frame, source="test", is_adjusted=True)

        loaded = repo.load_bars(session, asset.id, "1D", use_adjusted=True)
        assert (loaded["high"] >= loaded["close"] - 1e-9).all()
        assert (loaded["low"] <= loaded["close"] + 1e-9).all()
        assert loaded["close"].iloc[0] == pytest.approx(frame["close"].iloc[0] * 0.5)
        assert loaded["close_unadjusted"].iloc[0] == pytest.approx(frame["close"].iloc[0])

    def test_unadjusted_load_keeps_raw_prices(self, session, usa_spec, bars) -> None:
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        frame = bars.iloc[:50].copy()
        frame["adj_close"] = frame["close"] * 0.5
        repo.upsert_bars(session, asset.id, "1D", frame, source="test", is_adjusted=True)

        loaded = repo.load_bars(session, asset.id, "1D", use_adjusted=False)
        assert loaded["close"].iloc[0] == pytest.approx(frame["close"].iloc[0])

    def test_loaded_bars_feed_the_indicator_pipeline(self, session, loaded) -> None:
        """End-to-end: database -> features, with no shape surprises."""
        asset_id, original = loaded
        frame = repo.load_bars(session, asset_id, "1D")
        features = compute_features(frame)
        assert len(features) == len(original)
        assert features["rsi_14"].notna().any()


class TestDateHelpers:
    def test_latest_and_earliest_are_aware_utc(self, session, usa_spec, bars) -> None:
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        repo.upsert_bars(session, asset.id, "1D", bars, source="test", is_adjusted=False)

        latest = repo.latest_bar_date(session, asset.id, "1D")
        earliest = repo.earliest_bar_date(session, asset.id, "1D")
        assert latest is not None and earliest is not None
        assert latest.tzinfo is not None
        assert earliest.tzinfo is not None
        assert earliest < latest
        assert latest.date() == bars.index[-1].date()

    def test_helpers_return_none_with_no_bars(self, session, usa_spec) -> None:
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        assert repo.latest_bar_date(session, asset.id, "1D") is None
        assert repo.earliest_bar_date(session, asset.id, "1D") is None

    def test_delete_bars_removes_only_the_requested_timeframe(
        self, session, usa_spec, bars
    ) -> None:
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        repo.upsert_bars(session, asset.id, "1D", bars.iloc[:20], source="t", is_adjusted=False)
        repo.upsert_bars(session, asset.id, "1H", bars.iloc[:10], source="t", is_adjusted=False)

        removed = repo.delete_bars(session, asset.id, "1D")
        assert removed == 20
        assert repo.bar_count(session, asset.id, "1D") == 0
        assert repo.bar_count(session, asset.id, "1H") == 10


class TestCoverageReport:
    def test_lists_assets_with_and_without_data(self, session, usa_spec, chile_spec, bars) -> None:
        repo.sync_markets(session)
        with_data = repo.upsert_asset(session, usa_spec)
        repo.upsert_asset(session, chile_spec)
        repo.upsert_bars(session, with_data.id, "1D", bars.iloc[:100], source="t", is_adjusted=False)

        report = repo.coverage_report(session, "1D")
        assert len(report) == 2
        assert set(report["symbol"]) == {"TESTUS", "TESTCL"}

        us_row = report[report["symbol"] == "TESTUS"].iloc[0]
        cl_row = report[report["symbol"] == "TESTCL"].iloc[0]
        assert us_row["bars"] == 100
        assert cl_row["bars"] == 0  # reported honestly, not omitted

    def test_reports_the_resolved_provider_symbol(self, session, chile_spec, bars) -> None:
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, chile_spec)
        asset.provider = "chile-yfinance"
        asset.provider_symbol = "TESTCL.SN"
        session.flush()

        row = repo.coverage_report(session, "1D").iloc[0]
        assert row["provider_symbol"] == "TESTCL.SN"


class TestDuplicateOrderGuard:
    def test_client_order_id_is_unique(self, session, usa_spec) -> None:
        """The database is the last line of defence against a double-submitted order."""
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)

        for _ in range(2):
            session.add(
                Order(
                    client_order_id="dedupe-me",
                    asset_id=asset.id,
                    market="USA",
                    side="BUY",
                    quantity=10.0,
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_zero_quantity_order_is_rejected(self, session, usa_spec) -> None:
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        session.add(
            Order(
                client_order_id="zero-qty",
                asset_id=asset.id,
                market="USA",
                side="BUY",
                quantity=0.0,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_live_mode_orders_are_constrained_to_known_modes(self, session, usa_spec) -> None:
        repo.sync_markets(session)
        asset = repo.upsert_asset(session, usa_spec)
        session.add(
            Order(
                client_order_id="bad-mode",
                asset_id=asset.id,
                market="USA",
                side="BUY",
                quantity=1.0,
                mode="yolo",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


class TestRealUniverseRoundTrip:
    def test_the_declared_universe_persists(self, session) -> None:
        """Every real asset spec must be storable -- no constraint surprises."""
        from app.core.universe import BENCHMARKS, DEFAULT_UNIVERSE

        repo.sync_markets(session)
        seen = set()
        for spec in DEFAULT_UNIVERSE + BENCHMARKS:
            key = (spec.symbol, spec.market)
            if key in seen:
                continue
            seen.add(key)
            repo.upsert_asset(session, spec)

        stored = session.scalar(select(func.count()).select_from(Asset))
        assert stored == len(seen)

    def test_chilean_assets_persist_with_clp(self, session) -> None:
        repo.sync_markets(session)
        repo.upsert_asset(session, find_asset("SQM-B", "CHILE"))
        asset = repo.get_asset(session, "SQM-B", "CHILE")
        assert asset is not None
        assert asset.currency == "CLP"
        assert asset.market_code == "CHILE"
