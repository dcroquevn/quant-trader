"""Scanner and HTML report tests.

The scanner's job is to rank current readings without ever implying a forecast, and to
refuse to present a verdict when its inputs are broken. The report's job is to be a
standalone audit artefact that states which data partition produced its numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.backtesting.engine import BacktestConfig, Backtester
from app.backtesting.metrics import compute_metrics
from app.backtesting.report import generate_report, render_report
from app.core.universe import AssetSpec
from app.database import repository as repo
from app.strategies.registry import build_strategy
from app.strategies.scanner import ScanResult, ScanRow, scan_market
from tests.conftest import FakeProvider, make_bars
from tests.test_backtest_engine import ScriptedStrategy, priced_bars, zero_cost_model

from app.strategies.base import Action


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def store_symbols(session, specs_and_frames: list[tuple[AssetSpec, pd.DataFrame]]):
    """Persist bars for several specs and return a DataEngine.

    Takes a list of pairs rather than a dict: ``AssetSpec`` carries a ``dict`` field, so
    it is unhashable despite being frozen and cannot be a mapping key.
    """
    from app.data.engine import DataEngine

    repo.sync_markets(session)
    # Declared specs map only yfinance/alpaca tickers, so give each one a "fake" mapping
    # for the store step. The scan itself only reads stored bars -- it never downloads --
    # so it can look the asset up by its real declared spec afterwards.
    staged = [(spec.with_provider_symbol("fake", spec.symbol), frame)
              for spec, frame in specs_and_frames]

    frames = {spec.symbol: frame for spec, frame in staged}
    engine = DataEngine(session, provider=FakeProvider(frames))
    for spec, _ in staged:
        result = engine.download_symbol(spec, incremental=False)
        assert result.ok, f"fixture failed to store {spec.symbol}: {result.error}"
    session.commit()
    return engine


def recent_bars(n: int = 400, *, seed: int = 71, end: datetime | None = None) -> pd.DataFrame:
    """Bars ending today, so freshness checks pass."""
    last = pd.Timestamp((end or datetime.now(timezone.utc)).date(), tz="UTC")
    index = pd.bdate_range(end=last, periods=n, tz="UTC", name="ts")
    frame = make_bars(len(index), seed=seed, annual_drift=0.25)
    frame.index = index
    return frame


# --------------------------------------------------------------------------- #
# ScanRow / ScanResult
# --------------------------------------------------------------------------- #


class TestScanRowPresentation:
    def test_score_description_refuses_probability_language(self) -> None:
        row = ScanRow(
            symbol="X", market="USA", currency="USD",
            as_of=datetime.now(timezone.utc), price=100.0, score=0.75,
        )
        text = row.score_description.lower()
        assert "not a probability" in text
        assert "counts" in text

    def test_to_dict_is_json_serialisable(self) -> None:
        import json

        row = ScanRow(
            symbol="X", market="USA", currency="USD",
            as_of=datetime.now(timezone.utc), price=100.0,
        )
        json.dumps(row.to_dict())

    def test_dict_carries_the_score_caveat(self) -> None:
        """Any surface consuming the API gets the correct phrasing for free."""
        row = ScanRow(
            symbol="X", market="USA", currency="USD",
            as_of=datetime.now(timezone.utc), price=1.0, score=0.5,
        )
        assert "not a probability" in row.to_dict()["score_description"].lower()


class TestScanResultFiltering:
    def rows(self) -> list[ScanRow]:
        now = datetime.now(timezone.utc)
        return [
            ScanRow("AAA", "USA", "USD", now, 100.0, action="BUY", score=1.0,
                    dollar_volume=1e9, rsi=55.0),
            ScanRow("BBB", "USA", "USD", now, 5.0, action="HOLD", score=0.5,
                    dollar_volume=1e5, rsi=40.0),
            ScanRow("CCC", "CHILE", "CLP", now, 50_000.0, action="BUY", score=0.8,
                    dollar_volume=1e7, rsi=None, tradable=False),
        ]

    def result(self) -> ScanResult:
        out = ScanResult(strategy={"name": "t"}, scanned_at=datetime.now(timezone.utc))
        out.rows = self.rows()
        return out

    def test_filter_by_market(self) -> None:
        assert [r.symbol for r in self.result().filtered(market="CHILE")] == ["CCC"]

    def test_all_market_is_a_passthrough(self) -> None:
        assert len(self.result().filtered(market="ALL")) == 3

    def test_filter_by_action(self) -> None:
        assert {r.symbol for r in self.result().filtered(action="BUY")} == {"AAA", "CCC"}

    def test_filter_by_min_score(self) -> None:
        assert {r.symbol for r in self.result().filtered(min_score=0.8)} == {"AAA", "CCC"}

    def test_filter_tradable_only(self) -> None:
        assert [r.symbol for r in self.result().filtered(tradable_only=True)] == ["AAA", "BBB"]

    def test_filter_by_liquidity_and_price(self) -> None:
        assert [r.symbol for r in self.result().filtered(min_dollar_volume=1e6)] == ["AAA", "CCC"]
        assert [r.symbol for r in self.result().filtered(max_price=10.0)] == ["BBB"]

    def test_filters_compose(self) -> None:
        rows = self.result().filtered(market="USA", action="BUY", min_score=0.9)
        assert [r.symbol for r in rows] == ["AAA"]


class TestScanResultSorting:
    def test_sorts_descending_by_default(self) -> None:
        result = ScanResult(strategy={}, scanned_at=datetime.now(timezone.utc))
        now = datetime.now(timezone.utc)
        result.rows = [
            ScanRow("LOW", "USA", "USD", now, 1.0, score=0.2),
            ScanRow("HIGH", "USA", "USD", now, 1.0, score=0.9),
            ScanRow("MID", "USA", "USD", now, 1.0, score=0.5),
        ]
        assert [r.symbol for r in result.sorted_by("score")] == ["HIGH", "MID", "LOW"]

    def test_missing_values_always_sort_last(self) -> None:
        """A null is "not measured" and must never win a ranking.

        Letting ``None`` compare low is how an instrument with no data ends up at the top
        of a BUY list.
        """
        result = ScanResult(strategy={}, scanned_at=datetime.now(timezone.utc))
        now = datetime.now(timezone.utc)
        result.rows = [
            ScanRow("NULL", "USA", "USD", now, 1.0, rsi=None),
            ScanRow("HIGH", "USA", "USD", now, 1.0, rsi=90.0),
            ScanRow("LOW", "USA", "USD", now, 1.0, rsi=10.0),
        ]
        assert [r.symbol for r in result.sorted_by("rsi")] == ["HIGH", "LOW", "NULL"]
        assert [r.symbol for r in result.sorted_by("rsi", descending=False)][-1] == "NULL"

    def test_unknown_sort_key_raises(self) -> None:
        result = ScanResult(strategy={}, scanned_at=datetime.now(timezone.utc))
        with pytest.raises(KeyError, match="Cannot sort by"):
            result.sorted_by("vibes")


# --------------------------------------------------------------------------- #
# Scanning against stored data
# --------------------------------------------------------------------------- #


class TestScanMarket:
    def test_produces_a_row_per_instrument(self, session) -> None:
        """Uses real universe symbols, so the find_asset lookup path is exercised too."""
        from app.core.universe import find_asset

        symbols = ["AAPL", "MSFT", "NVDA"]
        store_symbols(
            session,
            [(find_asset(s, "USA"), recent_bars(seed=80 + i)) for i, s in enumerate(symbols)],
        )

        result = scan_market(
            session,
            build_strategy("trend_momentum"),
            "USA",
            symbols=symbols,
            log_decisions=False,
        )

        assert len(result.rows) == 3
        assert result.errors == []
        assert {r.symbol for r in result.rows} == set(symbols)
        assert all(r.price is not None for r in result.rows)
        assert all(r.action in {"BUY", "SELL", "HOLD"} for r in result.rows)

    def test_fresh_us_data_is_marked_tradable(self, session) -> None:
        """A current, volume-complete series must not be blocked by a data gate."""
        from app.core.universe import find_asset

        store_symbols(session, [(find_asset("AAPL", "USA"), recent_bars(seed=91))])
        result = scan_market(
            session, build_strategy("trend_momentum"), "USA",
            symbols=["AAPL"], log_decisions=False,
        )

        row = result.rows[0]
        assert row.stale is False
        assert row.volume_feed_degraded is False
        assert row.tradable is True
        assert row.blocked_reason == ""

    def test_a_degraded_volume_feed_blocks_a_misleading_verdict(self, session) -> None:
        """The point of the flag: "no setups" and "broken input" must not look alike.

        Every volume-derived feature is zero here, so a volume-gated strategy can never
        fire. Without the flag the scanner reports a bland HOLD that reads as a considered
        verdict rather than an unevaluable one.
        """
        from app.core.universe import find_asset

        frame = recent_bars(seed=92)
        frame.iloc[-18:, frame.columns.get_loc("volume")] = 0.0
        store_symbols(session, [(find_asset("MSFT", "USA"), frame)])

        result = scan_market(
            session, build_strategy("trend_momentum"), "USA",
            symbols=["MSFT"], log_decisions=False,
        )

        row = result.rows[0]
        assert row.volume_feed_degraded is True
        assert row.tradable is False
        assert "zero volume" in row.blocked_reason
        assert "not an absence of setups" in row.blocked_reason

    def test_a_stale_series_is_blocked(self, session) -> None:
        from app.core.universe import find_asset

        old = recent_bars(seed=93, end=datetime.now(timezone.utc) - timedelta(days=60))
        store_symbols(session, [(find_asset("NVDA", "USA"), old)])

        result = scan_market(
            session, build_strategy("trend_momentum"), "USA",
            symbols=["NVDA"], log_decisions=False,
        )

        row = result.rows[0]
        assert row.stale is True
        assert row.tradable is False

    def test_errors_are_recorded_never_silently_dropped(self, session) -> None:
        repo.sync_markets(session)
        strategy = build_strategy("trend_momentum")
        result = scan_market(session, strategy, "USA", log_decisions=False)

        # Nothing is stored, so every declared instrument fails for lack of data.
        assert result.errors
        assert all("bars" in e["error"] or "usable" in e["error"] for e in result.errors)
        assert result.rows == []

    def test_disclaimer_is_present(self, session) -> None:
        repo.sync_markets(session)
        result = scan_market(
            session, build_strategy("trend_momentum"), "USA", log_decisions=False
        )
        assert "not a probability" in result.disclaimer.lower() or "forecast" in result.disclaimer.lower()

    def test_result_is_json_serialisable(self, session) -> None:
        import json

        repo.sync_markets(session)
        result = scan_market(
            session, build_strategy("trend_momentum"), "USA", log_decisions=False
        )
        json.dumps(result.to_dict())


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #


@pytest.fixture
def sample_result():
    """A small but complete backtest result to render."""
    frame = priced_bars([100.0, 105.0, 110.0, 108.0, 115.0, 120.0, 118.0, 125.0])
    strategy = ScriptedStrategy(
        {frame.index[0]: Action.BUY, frame.index[4]: Action.SELL},
        stop_pct=99.0,
        target_pct=999.0,
    )
    config = BacktestConfig(
        market="USA", initial_capital=100_000.0, risk_per_trade_pct=50.0,
        max_position_size_pct=100.0, split="validation", label="test run",
    )
    result = Backtester(strategy, config, cost_model=zero_cost_model()).run({"X": frame})
    result.metrics = compute_metrics(result.equity_curve, result.trades, result.snapshots)
    return result


class TestReportRendering:
    def test_is_a_complete_html_document(self, sample_result) -> None:
        document = render_report(sample_result)
        assert document.lstrip().startswith("<!doctype html")
        assert "</html>" in document

    def test_is_standalone_with_no_external_resources(self, sample_result) -> None:
        """A report must still render from disk years later, offline."""
        document = render_report(sample_result)
        for forbidden in ("src=\"http", "href=\"http", "cdn.", "googleapis"):
            assert forbidden not in document, f"external resource: {forbidden}"

    def test_states_the_data_partition_prominently(self, sample_result) -> None:
        """A TRAIN result and a TEST result look identical on a chart."""
        document = render_report(sample_result)
        assert "VALIDATION partition" in document
        assert "split" in document

    def test_train_split_gets_its_own_warning(self, sample_result) -> None:
        sample_result.config.split = "train"
        document = render_report(sample_result)
        assert "TRAIN partition" in document
        assert "no out-of-sample information" in document

    def test_test_split_gets_its_own_warning(self, sample_result) -> None:
        sample_result.config.split = "test"
        document = render_report(sample_result)
        assert "TEST partition" in document
        assert "read once" in document

    def test_full_split_notes_there_is_no_holdout(self, sample_result) -> None:
        sample_result.config.split = "full"
        document = render_report(sample_result)
        assert "no held-out data" in document

    def test_costs_are_stated_next_to_returns(self, sample_result) -> None:
        document = render_report(sample_result)
        assert "round-trip cost" in document
        assert "Cost assumptions" in document
        assert "not a broker" in document

    def test_limitations_are_included_in_full(self, sample_result) -> None:
        document = render_report(sample_result)
        assert "Limitations" in document
        assert "Survivorship" in document
        assert "stop is assumed to fill first" in document

    def test_footer_refuses_forecast_language(self, sample_result) -> None:
        document = render_report(sample_result)
        assert "None is a forecast" in document
        assert "probability of any future outcome" in document

    def test_contains_inline_svg_charts(self, sample_result) -> None:
        document = render_report(sample_result)
        assert document.count("<svg") >= 2
        assert "Equity curve" in document
        assert "Drawdown" in document

    def test_every_trade_is_listed(self, sample_result) -> None:
        document = render_report(sample_result)
        assert f"Every trade ({sample_result.n_trades})" in document
        for trade in sample_result.trades:
            assert trade.symbol in document

    def test_strategy_parameters_are_recorded(self, sample_result) -> None:
        document = render_report(sample_result)
        assert "Strategy parameters" in document

    def test_missing_metrics_render_as_a_dash_not_zero(self, sample_result) -> None:
        """"0.00 Sharpe" and "Sharpe is undefined here" lead to opposite conclusions."""
        sample_result.metrics["sharpe"] = None
        sample_result.metrics["cagr_pct"] = None
        document = render_report(sample_result)
        assert "&mdash;" in document
        assert "not defined for this sample" in document

    def test_symbols_are_html_escaped(self, sample_result) -> None:
        """A vendor symbol is untrusted text as far as the document is concerned."""
        sample_result.trades[0].symbol = "<script>alert(1)</script>"
        document = render_report(sample_result)
        assert "<script>alert(1)</script>" not in document
        assert "&lt;script&gt;" in document

    def test_renders_with_no_trades(self) -> None:
        frame = priced_bars([100.0] * 6)
        config = BacktestConfig(market="USA", initial_capital=10_000.0)
        result = Backtester(ScriptedStrategy(), config, cost_model=zero_cost_model()).run(
            {"X": frame}
        )
        result.metrics = compute_metrics(result.equity_curve, result.trades, result.snapshots)

        document = render_report(result)
        assert "No trades were taken" in document

    def test_benchmark_caveats_are_rendered(self, sample_result) -> None:
        sample_result.benchmark_metrics = {
            "available": True,
            "label": "ECH buy and hold",
            "caveats": ["ECH is quoted in USD while the strategy trades in CLP"],
            "total_return_pct": 5.0,
        }
        document = render_report(sample_result)
        assert "Benchmark caveats" in document
        assert "quoted in USD" in document

    def test_unavailable_benchmark_says_why(self, sample_result) -> None:
        sample_result.benchmark_metrics = {"available": False, "reason": "no stored bars"}
        document = render_report(sample_result)
        assert "No comparison available" in document
        assert "no stored bars" in document


class TestReportWriting:
    def test_writes_a_file_and_returns_its_path(self, sample_result, tmp_path) -> None:
        path = generate_report(sample_result, output_dir=tmp_path)
        assert path.exists()
        assert path.suffix == ".html"
        assert path.read_text(encoding="utf-8").startswith("<!doctype html")

    def test_filename_records_strategy_market_and_split(self, sample_result, tmp_path) -> None:
        path = generate_report(sample_result, output_dir=tmp_path)
        assert "scripted" in path.name
        assert "USA" in path.name
        assert "validation" in path.name

    def test_explicit_filename_is_honoured(self, sample_result, tmp_path) -> None:
        path = generate_report(sample_result, output_dir=tmp_path, filename="fixed.html")
        assert path.name == "fixed.html"

    def test_creates_the_directory_if_absent(self, sample_result, tmp_path) -> None:
        target = tmp_path / "nested" / "deeper"
        path = generate_report(sample_result, output_dir=target)
        assert path.exists()
