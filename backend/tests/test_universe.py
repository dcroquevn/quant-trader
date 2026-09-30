"""Market definitions, universe and symbol mapping.

These tests encode the findings of the live symbol verification run (2026-09-27)
so a regression in the mapping table is caught without hitting the network.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.core.markets import MARKET_CHILE, MARKET_USA, MARKETS, get_market
from app.core.universe import (
    ALL_REGIONS,
    ASIA_UNIVERSE,
    BENCHMARK_AVAILABILITY,
    CHILE_EXPOSURE_UNIVERSE,
    DEFAULT_UNIVERSE,
    REGION_ASIA,
    REGION_BENCHMARKS,
    REGION_CHILE,
    REGION_US,
    USA_UNIVERSE,
    AssetSpec,
    benchmark_for_market,
    benchmark_for_region,
    candidate_symbols,
    find_asset,
    regions_of,
    universe_for_market,
    universe_for_region,
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

    def test_chile_exposure_is_us_listed(self) -> None:
        """The whole point of the replacement: these can be bought."""
        symbols = {spec.symbol for spec in CHILE_EXPOSURE_UNIVERSE}
        assert symbols == {"SQM", "BSAC", "BCH", "ENIC", "CCU", "ECH"}
        assert all(spec.market == "USA" for spec in CHILE_EXPOSURE_UNIVERSE)
        assert all(spec.region == REGION_CHILE for spec in CHILE_EXPOSURE_UNIVERSE)

    def test_no_santiago_tickers_remain(self) -> None:
        """A ``.SN`` instrument cannot be bought through the broker available here.

        The ``.SN`` provider machinery is still present and still tested, so re-adding one
        is easy. This test makes it a deliberate act with a visible failure.
        """
        for spec in DEFAULT_UNIVERSE:
            for candidates in spec.provider_symbols.values():
                for candidate in candidates:
                    assert not candidate.endswith(".SN"), (
                        f"{spec.symbol} resolves to {candidate}, which cannot be bought"
                    )

    def test_asia_universe_spans_several_countries(self) -> None:
        """A single-country ETF collection would not be regional exposure."""
        symbols = {spec.symbol for spec in ASIA_UNIVERSE}
        assert {"MCHI", "INDA", "EWY", "EWT", "EIDO", "THD", "VNM"} <= symbols
        assert all(spec.region == REGION_ASIA for spec in ASIA_UNIVERSE)
        assert len(ASIA_UNIVERSE) >= 15

    def test_every_instrument_has_a_measured_turnover(self) -> None:
        """An unmeasured instrument cannot be told apart from a liquid one."""
        for spec in DEFAULT_UNIVERSE:
            assert spec.median_turnover_usd is not None, (
                f"{spec.symbol} is missing from MEDIAN_TURNOVER_USD"
            )
            assert spec.median_turnover_usd > 0

    def test_thin_instruments_generate_a_liquidity_caveat(self) -> None:
        """Position sizing on a 2M-a-day ADR is capped by liquidity, not by risk.

        The caveat is generated from the measured figure rather than hand-written beside it,
        because an earlier version had the prose and the number disagreeing: the notes called
        the Asian ETFs thin and were silent about the Chilean ADRs, which are thinner.
        """
        thin = [spec for spec in DEFAULT_UNIVERSE if spec.is_thinly_traded]
        assert thin, "the measurement found ten; none are flagged"
        for spec in thin:
            caveat = spec.liquidity_caveat
            assert caveat, f"{spec.symbol} is thin and says nothing"
            assert spec.symbol in caveat
            assert "optimistic" in caveat, "the caveat must say what it means for a fill"

    def test_liquid_instruments_generate_no_caveat(self) -> None:
        """A warning on everything is a warning on nothing."""
        assert find_asset("SPY").liquidity_caveat == ""
        assert find_asset("AAPL").liquidity_caveat == ""

    def test_the_chilean_adrs_are_the_thin_ones(self) -> None:
        """Recorded because it is counter-intuitive and it changed the notes.

        "NYSE-listed" suggests liquidity. Measured over the three years to 2026-09-25, five of
        the six Chilean instruments trade under 20M USD a day, and CCU and ENIC are the least
        liquid anywhere in this universe. If a re-measurement changes that, this test should
        fail so the section comments get revisited rather than quietly going stale.
        """
        thin_chile = [s for s in CHILE_EXPOSURE_UNIVERSE if s.is_thinly_traded]
        assert len(thin_chile) == 5
        assert not find_asset("SQM").is_thinly_traded
        assert find_asset("CCU").median_turnover_usd < find_asset("THD").median_turnover_usd

    def test_every_spec_has_a_valid_market(self) -> None:
        for spec in DEFAULT_UNIVERSE:
            assert get_market(spec.market).code == spec.market

    def test_currency_follows_the_market(self) -> None:
        for spec in DEFAULT_UNIVERSE:
            expected = "USD" if spec.market == "USA" else "CLP"
            assert spec.currency == expected

    def test_everything_tradable_is_denominated_in_usd(self) -> None:
        """Including the Chilean and Asian exposure, which is the uncomfortable part.

        A position in ECH or INDA earns the underlying move *and* the currency move, and
        nothing in this project separates them. Asserted so the fact stays visible.
        """
        assert all(spec.currency == "USD" for spec in DEFAULT_UNIVERSE)

    def test_every_spec_declares_a_known_region(self) -> None:
        for spec in DEFAULT_UNIVERSE:
            assert spec.region in ALL_REGIONS, f"{spec.symbol} has region {spec.region!r}"

    def test_regions_partition_the_universe(self) -> None:
        total = sum(len(universe_for_region(region)) for region in ALL_REGIONS)
        assert total == len(DEFAULT_UNIVERSE)

    def test_region_is_not_market(self) -> None:
        """If these ever coincide the distinction has collapsed and benchmarks are wrong."""
        assert universe_for_region(REGION_CHILE), "Chile exposure vanished"
        assert all(spec.market == "USA" for spec in universe_for_region(REGION_CHILE))

    def test_unknown_region_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown region"):
            universe_for_region("Europe")

    def test_regions_of_reports_every_exposure_a_basket_spans(self) -> None:
        assert regions_of(["AAPL"]) == (REGION_US,)
        assert regions_of(["SQM", "ECH"]) == (REGION_CHILE,)
        assert regions_of(["AAPL", "TSM"]) == (REGION_US, REGION_ASIA)

    def test_invalid_market_is_rejected_at_construction(self) -> None:
        with pytest.raises(KeyError, match="Unknown market"):
            AssetSpec(symbol="XYZ", name="Nowhere", market="ATLANTIS")

    def test_universe_for_market_filters_correctly(self) -> None:
        usa = universe_for_market("USA")
        assert all(s.market == "USA" for s in usa)
        assert len(usa) == len(DEFAULT_UNIVERSE)

    def test_the_chile_market_is_declared_but_empty(self) -> None:
        """The market definition survives the universe that used it.

        Its calendar, currency and cost model are correct and verified, and someone with a
        Santiago broker could populate it. Keeping the definition and emptying the universe
        records that honestly; deleting it would lose the work.
        """
        assert universe_for_market("CHILE") == ()
        assert get_market("CHILE").currency == "CLP"

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

    def test_every_tradable_symbol_has_a_yfinance_candidate(self) -> None:
        for spec in DEFAULT_UNIVERSE:
            candidates = spec.candidates("yfinance")
            assert candidates, f"{spec.symbol} has no yfinance candidate"

    def test_adrs_and_country_etfs_map_to_their_plain_ticker(self) -> None:
        """They are ordinary US listings; no suffix, no special namespace."""
        assert candidate_symbols("SQM", "yfinance") == ("SQM",)
        assert candidate_symbols("ECH", "yfinance") == ("ECH",)
        assert candidate_symbols("TSM", "yfinance") == ("TSM",)

    def test_alpaca_maps_us_tickers(self) -> None:
        assert candidate_symbols("AAPL", "alpaca") == ("AAPL",)

    def test_find_asset_is_case_insensitive(self) -> None:
        assert find_asset("aapl").symbol == "AAPL"

    def test_find_asset_rejects_unknown_symbols(self) -> None:
        with pytest.raises(KeyError, match="not in the declared universe"):
            find_asset("NOTATICKER")

    def test_with_provider_symbol_pins_a_verified_ticker(self) -> None:
        spec = find_asset("SQM")
        pinned = spec.with_provider_symbol("yfinance", "SQM.MX")
        assert pinned.candidates("yfinance") == ("SQM.MX",)
        # The original is frozen and unchanged.
        assert spec.candidates("yfinance") == ("SQM",)


class TestBenchmarks:
    def test_usa_benchmark_is_spy(self) -> None:
        bench = benchmark_for_region(REGION_US)
        assert bench.symbol == "SPY"
        assert bench.available is True
        assert bench.currency == "USD"

    def test_chile_benchmark_is_declared_as_a_proxy_not_the_ipsa(self) -> None:
        """The IPSA is not obtainable free of charge, and the code must say so.

        This test exists to stop a future change from quietly relabelling the ECH proxy as
        "IPSA" and presenting a USD ETF as the Chilean index.
        """
        bench = benchmark_for_region(REGION_CHILE)
        assert bench.symbol == "ECH"
        assert bench.kind == "etf_proxy"
        assert bench.symbol != "IPSA"

    def test_chile_benchmark_documents_its_flaws(self) -> None:
        bench = benchmark_for_region(REGION_CHILE)
        assert len(bench.caveats) >= 3
        joined = " ".join(bench.caveats).lower()
        assert "usd" in joined, "the currency exposure must be stated"
        assert "ipsa" in joined, "the missing index must be named"

    def test_the_empty_chile_market_offers_no_benchmark(self) -> None:
        """Rather than keeping ECH there, where it would look market-appropriate."""
        bench = benchmark_for_market("CHILE")
        assert bench.available is False
        assert bench.symbol == ""
        assert bench.caveats

    def test_asia_strategy_is_not_measured_against_the_sp500(self) -> None:
        """The reason ``region`` exists at all.

        Every tradable instrument is US-listed, so a market-keyed benchmark would compare an
        emerging-Asia backtest against the S&P 500 and read the difference as skill.
        """
        assert benchmark_for_market("USA").symbol == "SPY"
        assert benchmark_for_region(REGION_ASIA).symbol == "AAXJ"
        assert benchmark_for_region(REGION_ASIA).symbol != "SPY"

    def test_every_region_has_a_benchmark_with_caveats(self) -> None:
        for region in ALL_REGIONS:
            bench = benchmark_for_region(region)
            assert region in REGION_BENCHMARKS
            assert bench.caveats, f"{region} benchmark has no caveats"

    def test_region_benchmarks_admit_they_are_also_tradable(self) -> None:
        """SPY, ECH and AAXJ are all in the universe, so a strategy can hold its yardstick.

        That is not a bug to fix -- they are the only free proxies available -- but a
        comparison against something you own measures timing, not selection, and the caveat
        has to travel with the number.
        """
        for region in ALL_REGIONS:
            bench = benchmark_for_region(region)
            held = {spec.symbol for spec in universe_for_region(region)}
            if bench.symbol in held:
                joined = " ".join(bench.caveats).lower()
                assert "tradable" in joined, f"{region}: self-benchmarking not disclosed"

    def test_unknown_region_benchmark_raises(self) -> None:
        with pytest.raises(KeyError, match="No benchmark declared for region"):
            benchmark_for_region("Europe")

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
    def test_short_histories_carry_warnings(self) -> None:
        """A shorter sample than the rest of the universe has to be visible in reports.

        ENIC begins in 2016 after the Enel Chile/Americas split, PDD in 2018 and SE in 2017.
        A backtest starting in 2016 runs them on less data than everything beside them, and
        the note is what surfaces that.
        """
        for symbol in ("ENIC", "PDD", "SE"):
            spec = find_asset(symbol)
            assert spec.notes, f"{symbol} has no data-limitation note"
            assert "20" in spec.notes, f"{symbol}'s note does not say when its history starts"

    def test_adrs_disclose_their_currency_exposure(self) -> None:
        """A Chilean ADR's return is the Chilean move plus the CLP/USD move."""
        assert "USD" in find_asset("SQM").notes

    def test_overlapping_instruments_say_so(self) -> None:
        """Holding TSM and EWT, or MCHI and ASHR, is more correlated than diversified.

        The risk engine's sector cap will not catch it: EWT is 'Broad Market'.
        """
        for symbol in ("EWT", "TSM", "ASHR"):
            notes = find_asset(symbol).notes.lower()
            assert "overlap" in notes or "correlated" in notes, f"{symbol} hides its overlap"
