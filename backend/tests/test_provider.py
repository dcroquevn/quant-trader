"""DataProvider contract tests.

All offline. The provider abstraction is exercised through ``FakeProvider``; the
two tests that touch Yahoo live are marked ``network`` and excluded from the
default run so the suite stays deterministic and fast.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.core.exceptions import (
    CorruptDataError,
    EmptyDataError,
    ProviderUnavailableError,
    RateLimitError,
    SymbolNotFoundError,
    UnsupportedTimeframeError,
)
from app.data.chile_provider import ChileDataProvider, flag_illiquid_bars
from app.data.provider import Timeframe, validate_ohlcv
from app.data.registry import (
    available_providers,
    get_provider,
    provider_cost_table,
    provider_for_market,
    register_provider,
)
from app.data.yfinance_provider import YFinanceProvider
from tests.conftest import FakeProvider


class TestTimeframe:
    def test_parses_canonical_strings(self) -> None:
        assert Timeframe.parse("1D") is Timeframe.D1
        assert Timeframe.parse("15m") is Timeframe.M15
        assert Timeframe.parse("1d") is Timeframe.D1

    def test_passes_through_enum_members(self) -> None:
        assert Timeframe.parse(Timeframe.H1) is Timeframe.H1

    def test_rejects_unknown_values(self) -> None:
        with pytest.raises(UnsupportedTimeframeError, match="Unknown timeframe"):
            Timeframe.parse("1w")

    def test_daily_is_not_intraday(self) -> None:
        assert Timeframe.D1.is_intraday is False
        assert Timeframe.M5.is_intraday is True

    def test_annualisation_factors_are_ordered(self) -> None:
        assert Timeframe.D1.bars_per_year < Timeframe.H1.bars_per_year
        assert Timeframe.H1.bars_per_year < Timeframe.M5.bars_per_year


class TestValidateOHLCV:
    def frame(self, **overrides) -> pd.DataFrame:
        index = pd.bdate_range("2024-01-02", periods=5, tz="UTC")
        data = {
            "Open": [10.0, 11, 12, 13, 14],
            "High": [11.0, 12, 13, 14, 15],
            "Low": [9.0, 10, 11, 12, 13],
            "Close": [10.5, 11.5, 12.5, 13.5, 14.5],
            "Volume": [1000.0] * 5,
        }
        data.update(overrides)
        return pd.DataFrame(data, index=index)

    def test_lowercases_and_orders_columns(self) -> None:
        result = validate_ohlcv(self.frame(), symbol="X", provider="test")
        assert list(result.columns) == ["open", "high", "low", "close", "volume"]

    def test_maps_adjusted_close(self) -> None:
        frame = self.frame()
        frame["Adj Close"] = frame["Close"] * 0.9
        result = validate_ohlcv(frame, symbol="X", provider="test")
        assert "adj_close" in result.columns

    def test_normalises_index_to_utc(self) -> None:
        frame = self.frame()
        frame.index = frame.index.tz_convert("America/Santiago")
        result = validate_ohlcv(frame, symbol="X", provider="test")
        assert str(result.index.tz) == "UTC"
        assert result.index.name == "ts"

    def test_sorts_and_deduplicates(self) -> None:
        frame = self.frame()
        scrambled = pd.concat([frame.iloc[2:], frame.iloc[:3]])
        result = validate_ohlcv(scrambled, symbol="X", provider="test")
        assert result.index.is_monotonic_increasing
        assert not result.index.has_duplicates
        assert len(result) == 5

    def test_rejects_an_empty_frame(self) -> None:
        with pytest.raises(EmptyDataError, match="no rows"):
            validate_ohlcv(pd.DataFrame(), symbol="X", provider="test")

    def test_rejects_missing_columns(self) -> None:
        with pytest.raises(CorruptDataError, match="missing columns"):
            validate_ohlcv(self.frame().drop(columns=["Volume"]), symbol="X", provider="test")

    def test_drops_invalid_rows_by_default(self) -> None:
        frame = self.frame()
        frame.iloc[2, frame.columns.get_loc("Low")] = 999.0  # low > high
        result = validate_ohlcv(frame, symbol="X", provider="test")
        assert len(result) == 4

    def test_can_raise_instead_of_dropping(self) -> None:
        frame = self.frame()
        frame.iloc[2, frame.columns.get_loc("Close")] = -1.0
        with pytest.raises(CorruptDataError, match="invalid bar"):
            validate_ohlcv(frame, symbol="X", provider="test", drop_invalid=False)

    def test_rejects_a_frame_where_nothing_is_valid(self) -> None:
        frame = self.frame()
        frame["Close"] = -1.0
        with pytest.raises(EmptyDataError, match="none were valid"):
            validate_ohlcv(frame, symbol="X", provider="test")

    def test_rejects_close_outside_the_high_low_range(self) -> None:
        """A close above the high is structurally impossible, not a rounding artefact."""
        frame = self.frame()
        frame.iloc[1, frame.columns.get_loc("Close")] = 99.0
        result = validate_ohlcv(frame, symbol="X", provider="test")
        assert len(result) == 4

    def test_flattens_multiindex_columns(self) -> None:
        """yfinance returns a MultiIndex for multi-ticker requests."""
        frame = self.frame()
        frame.columns = pd.MultiIndex.from_product([frame.columns, ["AAPL"]])
        result = validate_ohlcv(frame, symbol="AAPL", provider="test")
        assert "close_aapl" in result.columns or "close" in result.columns


class TestProviderContract:
    def test_fetch_bars_returns_validated_output(self, fake_provider: FakeProvider) -> None:
        frame = fake_provider.fetch_bars("TESTUS", Timeframe.D1)
        assert not frame.empty
        assert list(frame.columns)[:5] == ["open", "high", "low", "close", "volume"]
        assert str(frame.index.tz) == "UTC"

    def test_clamps_start_to_the_provider_lookback_limit(
        self, fake_provider: FakeProvider
    ) -> None:
        """Asking for more intraday history than exists must not silently succeed.

        Yahoo caps 5-minute bars at 60 days and simply returns 60 days for a
        two-year request. Clamping makes the limit visible in the request itself.
        """
        requested_start = datetime.now(timezone.utc) - timedelta(days=730)
        try:
            fake_provider.fetch_bars("TESTUS", Timeframe.M5, start=requested_start)
        except EmptyDataError:
            pass  # the fixture has no bars inside the clamped window

        _, timeframe, actual_start, _ = fake_provider.requests[-1]
        assert timeframe == "5m"
        assert actual_start > requested_start
        assert (datetime.now(timezone.utc) - actual_start).days <= 61

    def test_rejects_an_unsupported_timeframe(self, fake_provider: FakeProvider) -> None:
        with pytest.raises(UnsupportedTimeframeError, match="does not offer timeframe"):
            fake_provider.fetch_bars("TESTUS", Timeframe.H1)

    def test_reports_supported_markets(self, fake_provider: FakeProvider) -> None:
        assert fake_provider.supports_market("usa")
        assert not fake_provider.supports_market("PERU")

    def test_resolve_symbol_returns_the_first_working_candidate(
        self, fake_provider: FakeProvider
    ) -> None:
        resolved = fake_provider.resolve_symbol(("NOPE", "ALSONOPE", "TESTUS"), "TESTUS")
        assert resolved == "TESTUS"

    def test_resolve_symbol_raises_when_nothing_resolves(
        self, fake_provider: FakeProvider
    ) -> None:
        with pytest.raises(SymbolNotFoundError) as excinfo:
            fake_provider.resolve_symbol(("NOPE", "STILLNOPE"), "GHOST")
        assert excinfo.value.tried == ("NOPE", "STILLNOPE")
        assert "GHOST" in str(excinfo.value)

    def test_provider_failure_propagates(self, bars) -> None:
        broken = FakeProvider({"X": bars}, fail_with=RateLimitError("slow down"))
        with pytest.raises(RateLimitError):
            broken.fetch_bars("X", Timeframe.D1)


class TestYFinanceProvider:
    def test_declares_itself_free_and_keyless(self) -> None:
        caps = YFinanceProvider().capabilities
        assert caps.requires_api_key is False
        assert "free" in caps.cost.lower()

    def test_covers_both_markets(self) -> None:
        caps = YFinanceProvider().capabilities
        assert caps.markets == frozenset({"USA", "CHILE"})

    def test_documents_the_intraday_lookback_caps(self) -> None:
        """The real Yahoo limits, encoded rather than discovered at runtime."""
        caps = YFinanceProvider().capabilities
        assert caps.limit_for(Timeframe.D1).max_lookback_days is None
        assert caps.limit_for(Timeframe.H1).max_lookback_days == 730
        assert caps.limit_for(Timeframe.M5).max_lookback_days == 60

    def test_classifies_rate_limit_errors(self) -> None:
        classified = YFinanceProvider._classify(Exception("429 Too Many Requests"))
        assert isinstance(classified, RateLimitError)

    def test_classifies_connectivity_errors(self) -> None:
        classified = YFinanceProvider._classify(Exception("connection timed out"))
        assert isinstance(classified, ProviderUnavailableError)

    def test_restamps_daily_bars_to_midnight_utc(self) -> None:
        """Daily bars from both markets must land on the same timestamp per session.

        Yahoo stamps them at exchange-local midnight, so New York and Santiago
        would otherwise differ by two hours and never align.
        """
        ny_index = pd.DatetimeIndex(
            pd.to_datetime(["2024-01-02", "2024-01-03"])
        ).tz_localize("America/New_York")
        cl_index = pd.DatetimeIndex(
            pd.to_datetime(["2024-01-02", "2024-01-03"])
        ).tz_localize("America/Santiago")

        ny = pd.DataFrame({"Close": [1.0, 2.0]}, index=ny_index)
        cl = pd.DataFrame({"Close": [1.0, 2.0]}, index=cl_index)

        ny_out = YFinanceProvider._restamp_daily(ny)
        cl_out = YFinanceProvider._restamp_daily(cl)

        pd.testing.assert_index_equal(ny_out.index, cl_out.index)
        assert str(ny_out.index.tz) == "UTC"
        assert ny_out.index[0].hour == 0


class TestChileProvider:
    def test_only_supports_chile(self) -> None:
        provider = ChileDataProvider()
        assert provider.supports_market("CHILE")
        assert not provider.supports_market("USA")

    def test_reads_the_yfinance_symbol_namespace(self) -> None:
        """It shares Yahoo's ticker vocabulary, so it must not need duplicate mappings."""
        assert ChileDataProvider().symbol_namespace == "yfinance"

    def test_appends_the_santiago_suffix(self) -> None:
        assert ChileDataProvider.to_provider_symbol("SQM-B") == "SQM-B.SN"
        assert ChileDataProvider.to_provider_symbol("falabella") == "FALABELLA.SN"

    def test_does_not_double_the_suffix(self) -> None:
        assert ChileDataProvider.to_provider_symbol("BCI.SN") == "BCI.SN"

    def test_does_not_offer_minute_bars(self) -> None:
        """Minute bars for illiquid Chilean names are mostly carried-forward quotes."""
        provider = ChileDataProvider()
        assert not provider.supports_timeframe(Timeframe.M5)
        assert not provider.supports_timeframe(Timeframe.M15)
        assert provider.supports_timeframe(Timeframe.D1)

    def test_states_that_the_ipsa_is_unavailable(self) -> None:
        notes = ChileDataProvider().capabilities.notes
        assert "IPSA" in notes
        assert "NOT available" in notes or "not available" in notes


class TestIlliquidityFlag:
    def test_flags_zero_volume_bars(self, flat_bars: pd.DataFrame) -> None:
        result = flag_illiquid_bars(flat_bars)
        assert result["is_illiquid"].all()

    def test_does_not_flag_normal_bars(self, bars: pd.DataFrame) -> None:
        result = flag_illiquid_bars(bars)
        assert not result["is_illiquid"].any()

    def test_keeps_the_bars_rather_than_dropping_them(self, flat_bars: pd.DataFrame) -> None:
        """A dead session must stay visible; removing it would fake a continuous series."""
        result = flag_illiquid_bars(flat_bars)
        assert len(result) == len(flat_bars)

    def test_flags_a_single_dead_session_inside_a_live_series(
        self, bars: pd.DataFrame
    ) -> None:
        frame = bars.iloc[:50].copy()
        frame.iloc[25, frame.columns.get_loc("volume")] = 0.0
        result = flag_illiquid_bars(frame)
        assert bool(result["is_illiquid"].iloc[25]) is True
        assert int(result["is_illiquid"].sum()) == 1


class TestRegistry:
    def test_both_free_providers_are_registered(self) -> None:
        assert "yfinance" in available_providers()
        assert "chile-yfinance" in available_providers()

    def test_returns_a_singleton(self) -> None:
        assert get_provider("yfinance") is get_provider("yfinance")

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown data provider"):
            get_provider("bloomberg")

    def test_usa_routes_to_the_generic_yahoo_provider(self) -> None:
        assert provider_for_market("USA").name == "yfinance"

    def test_chile_routes_to_the_specialised_provider(self) -> None:
        """"yfinance" configured for CHILE must resolve to the .SN-aware subclass."""
        assert provider_for_market("CHILE").name == "chile-yfinance"

    def test_refuses_to_shadow_an_existing_provider(self) -> None:
        with pytest.raises(ValueError, match="already registered"):
            register_provider("yfinance", FakeProvider)

    def test_can_register_a_new_provider(self) -> None:
        register_provider("fake-test-only", FakeProvider, replace=True)
        assert get_provider("fake-test-only").name == "fake"

    def test_no_provider_costs_money(self) -> None:
        """The zero-cost rule, made auditable.

        This is about money, not credentials. A provider that is free but needs a
        free registration still satisfies the rule -- what it must not do is become
        a required dependency, which the next test covers.
        """
        for row in provider_cost_table():
            cost = row["cost"].lower()
            assert "free" in cost, row
            for forbidden in ("usd", "eur", "$", "/month", "per month", "subscription"):
                assert forbidden not in cost, f"{row['provider']} looks paid: {row['cost']}"

    def test_credentialled_providers_are_never_a_market_default(self) -> None:
        """The system must work end to end with a completely empty .env.

        A provider that needs credentials may be registered and offered, but it can
        never be what a market resolves to by default -- otherwise a fresh checkout
        cannot download anything until the user signs up for something.
        """
        for market in ("USA", "CHILE"):
            provider = provider_for_market(market)
            assert not provider.capabilities.requires_api_key, (
                f"{market} defaults to {provider.name!r}, which requires credentials"
            )

    def test_credentialled_providers_declare_registration_is_free(self) -> None:
        """If a provider needs an account, its cost string must say the account is free."""
        for row in provider_cost_table():
            if row["api_key_required"] == "yes":
                assert "registration" in row["cost"].lower(), row
                assert "free" in row["cost"].lower(), row

    def test_cost_table_lists_markets_and_timeframes(self) -> None:
        rows = {row["provider"]: row for row in provider_cost_table()}
        assert "1D" in rows["yfinance"]["timeframes"]
        assert "CHILE" in rows["chile-yfinance"]["markets"]


# --------------------------------------------------------------------------- #
# Live network checks -- deselected by default with `-m "not network"`
# --------------------------------------------------------------------------- #


@pytest.mark.network
class TestLiveProviders:
    def test_us_ticker_returns_bars(self) -> None:
        frame = YFinanceProvider().fetch_bars(
            "AAPL", Timeframe.D1, start="2024-01-02", end="2024-02-01"
        )
        assert len(frame) > 15
        assert frame["close"].gt(0).all()

    def test_chilean_ticker_returns_clp_bars(self) -> None:
        """SQM-B.SN verified 2026-09-27. CLP quotes are in the tens of thousands."""
        frame = ChileDataProvider().fetch_bars(
            "SQM-B", Timeframe.D1, start="2024-01-02", end="2024-02-01"
        )
        assert len(frame) > 15
        assert frame["close"].min() > 1000
