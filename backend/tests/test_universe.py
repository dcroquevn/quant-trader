"""Market definitions, universe and symbol mapping.

These tests encode the findings of the live symbol verification run (2026-09-27)
so a regression in the mapping table is caught without hitting the network.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.markets import MARKET_CHILE, MARKET_USA, MARKETS, get_market
from app.core.universe import (
    BENCHMARK_AVAILABILITY,
    CHILE_UNIVERSE,
    DEFAULT_UNIVERSE,
    USA_UNIVERSE,
    AssetSpec,
    benchmark_for_market,
    candidate_symbols,
    find_asset,
    universe_for_market,
)


class TestMarkets:
    def test_both_target_markets_exist(self) -> None:
        assert set(MARKETS) == {"USA", "CHILE"}

    def test_lookup_is_case_insensitive(self) -> None:
        assert get_market("usa") is MARKET_USA
        assert get_market(" chile ") is MARKET_CHILE

    def test_unknown_market_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown market"):
            get_market("ARGENTINA")

    def test_each_market_carries_its_full_identity(self) -> None:
        """Every field the brief requires per asset comes from the market."""
        for market in MARKETS.values():
            assert market.currency
            assert market.exchange
            assert market.timezone
            assert market.benchmark_symbol
            assert market.tzinfo is not None
            assert "-" in market.trading_hours

    def test_currencies_differ(self) -> None:
        assert MARKET_USA.currency == "USD"
        assert MARKET_CHILE.currency == "CLP"

    def test_timezones_differ(self) -> None:
        assert MARKET_USA.timezone == "America/New_York"
        assert MARKET_CHILE.timezone == "America/Santiago"

    def test_weekends_are_not_trading_days(self) -> None:
        saturday = date(2026, 9, 26)
        sunday = date(2026, 9, 27)
        assert saturday.weekday() == 5
        for market in MARKETS.values():
            assert not market.is_trading_day(saturday)
            assert not market.is_trading_day(sunday)

    def test_weekdays_are_trading_days(self) -> None:
        monday = date(2026, 9, 28)
        assert all(market.is_trading_day(monday) for market in MARKETS.values())

    def test_session_open_respects_local_time(self) -> None:
        during = datetime(2026, 9, 28, 10, 30)
        before = datetime(2026, 9, 28, 8, 0)
        after = datetime(2026, 9, 28, 17, 0)
        assert MARKET_USA.is_session_open(during)
        assert not MARKET_USA.is_session_open(before)
        assert not MARKET_USA.is_session_open(after)

    def test_session_is_closed_on_a_weekend(self) -> None:
        saturday_mid_session = datetime(2026, 9, 26, 11, 0)
        assert not MARKET_USA.is_session_open(saturday_mid_session)


class TestUniverse:
    def test_usa_universe_contains_the_required_instruments(self) -> None:
        symbols = {spec.symbol for spec in USA_UNIVERSE}
        required = {
            "SPY", "QQQ", "DIA", "IWM",
            "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL",
            "TSLA", "AMD", "JPM", "V", "AVGO",
        }
        assert required <= symbols, f"missing: {sorted(required - symbols)}"

    def test_chile_universe_has_all_eighteen_names(self) -> None:
        assert len(CHILE_UNIVERSE) == 18

    def test_chile_universe_covers_the_requested_companies(self) -> None:
        symbols = {spec.symbol for spec in CHILE_UNIVERSE}
        required = {
            "CHILE", "BSANTANDER", "BCI", "ITAUCL", "SQM-B", "CENCOSUD",
            "FALABELLA", "LTM", "ENELCHILE", "COLBUN", "CAP", "COPEC",
            "CMPC", "ENTEL", "ANDINA-B", "CCU", "PARAUCO", "MALLPLAZA",
        }
        assert required == symbols

    def test_every_spec_has_a_valid_market(self) -> None:
        for spec in DEFAULT_UNIVERSE:
            assert get_market(spec.market).code == spec.market

    def test_currency_follows_the_market(self) -> None:
        for spec in DEFAULT_UNIVERSE:
            expected = "USD" if spec.market == "USA" else "CLP"
            assert spec.currency == expected

    def test_invalid_market_is_rejected_at_construction(self) -> None:
        with pytest.raises(KeyError, match="Unknown market"):
            AssetSpec(symbol="XYZ", name="Nowhere", market="ATLANTIS")

    def test_universe_for_market_filters_correctly(self) -> None:
        usa = universe_for_market("USA")
        chile = universe_for_market("CHILE")
        assert all(s.market == "USA" for s in usa)
        assert all(s.market == "CHILE" for s in chile)
        assert len(usa) + len(chile) == len(DEFAULT_UNIVERSE)

    def test_tradable_universe_excludes_benchmarks_by_default(self) -> None:
        """A strategy must not take positions in its own yardstick."""
        for market in ("USA", "CHILE"):
            assert all(not s.is_benchmark for s in universe_for_market(market))

    def test_every_asset_has_a_sector_for_exposure_limits(self) -> None:
        for spec in DEFAULT_UNIVERSE:
            assert spec.sector and spec.sector != ""


class TestSymbolMapping:
    def test_us_tickers_map_to_themselves(self) -> None:
        assert candidate_symbols("AAPL", "yfinance") == ("AAPL",)

    def test_chilean_tickers_get_the_santiago_suffix(self) -> None:
        """Verified live on 2026-09-27: all 18 resolve with nemotécnico + .SN."""
        assert candidate_symbols("SQM-B", "yfinance")[0] == "SQM-B.SN"
        assert candidate_symbols("FALABELLA", "yfinance")[0] == "FALABELLA.SN"
        assert candidate_symbols("ANDINA-B", "yfinance")[0] == "ANDINA-B.SN"

    def test_every_chilean_symbol_has_a_yfinance_candidate(self) -> None:
        for spec in CHILE_UNIVERSE:
            candidates = spec.candidates("yfinance")
            assert candidates, f"{spec.symbol} has no yfinance candidate"
            assert candidates[0].endswith(".SN")

    def test_alpaca_has_no_chilean_mapping(self) -> None:
        """Alpaca does not list Chilean equities; that must fail, not fall back.

        A silent fallback to the bare symbol would route a Santiago order to
        whatever US ticker happens to share the name.
        """
        with pytest.raises(LookupError, match="does not cover"):
            candidate_symbols("SQM-B", "alpaca")

    def test_alpaca_maps_us_tickers(self) -> None:
        assert candidate_symbols("AAPL", "alpaca") == ("AAPL",)

    def test_find_asset_is_case_insensitive(self) -> None:
        assert find_asset("aapl").symbol == "AAPL"

    def test_find_asset_rejects_unknown_symbols(self) -> None:
        with pytest.raises(KeyError, match="not in the declared universe"):
            find_asset("NOTATICKER")

    def test_with_provider_symbol_pins_a_verified_ticker(self) -> None:
        spec = find_asset("SQM-B")
        pinned = spec.with_provider_symbol("yfinance", "SQM-B.SN")
        assert pinned.candidates("yfinance") == ("SQM-B.SN",)
        # The original is frozen and unchanged.
        assert spec.candidates("yfinance")[0] == "SQM-B.SN"


class TestBenchmarks:
    def test_usa_benchmark_is_spy(self) -> None:
        bench = benchmark_for_market("USA")
        assert bench.symbol == "SPY"
        assert bench.available is True
        assert bench.currency == "USD"

    def test_chile_benchmark_is_declared_as_a_proxy_not_the_ipsa(self) -> None:
        """The IPSA is not obtainable free of charge, and the code must say so.

        This test exists to stop a future change from quietly relabelling the ECH
        proxy as "IPSA" and presenting a USD ETF as the Chilean index.
        """
        bench = benchmark_for_market("CHILE")
        assert bench.symbol == "ECH"
        assert bench.kind == "etf_proxy"
        assert bench.symbol != "IPSA"

    def test_chile_benchmark_documents_its_flaws(self) -> None:
        bench = benchmark_for_market("CHILE")
        assert len(bench.caveats) >= 3
        joined = " ".join(bench.caveats).lower()
        assert "usd" in joined, "the currency mismatch must be stated"
        assert "ipsa" in joined, "the missing index must be named"

    def test_ipsa_is_declared_with_no_provider(self) -> None:
        """IPSA stays in the universe as an explicit, documented gap."""
        ipsa = find_asset("IPSA", "CHILE")
        assert ipsa.provider_symbols == {}
        assert "NOT AVAILABLE" in ipsa.notes

    def test_ipsa_symbol_lookup_fails_clearly(self) -> None:
        with pytest.raises(LookupError, match="does not cover"):
            candidate_symbols("IPSA", "yfinance", "CHILE")

    def test_every_market_has_a_benchmark(self) -> None:
        for code in MARKETS:
            assert code in BENCHMARK_AVAILABILITY
            assert benchmark_for_market(code).caveats, f"{code} benchmark has no caveats"

    def test_unknown_market_benchmark_raises(self) -> None:
        with pytest.raises(KeyError):
            benchmark_for_market("BRAZIL")


class TestDataLimitations:
    def test_restructured_companies_carry_warnings(self) -> None:
        """Survivorship and comparability caveats must travel with the asset.

        LATAM's Chapter 11 and Itaú's merger history make long backtests on those
        names misleading. The note is what surfaces that in a report.
        """
        for symbol in ("LTM", "ITAUCL", "MALLPLAZA"):
            spec = find_asset(symbol, "CHILE")
            assert spec.notes, f"{symbol} has no data-limitation note"

    def test_sqm_series_b_is_documented(self) -> None:
        assert "Series B" in find_asset("SQM-B", "CHILE").notes
