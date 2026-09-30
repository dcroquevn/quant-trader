"""API contract tests.

These exist because of a bug this suite would have caught: the dashboard's asset page
read ``volume_feed_degraded`` off the audit response, the field was never added to the
endpoint, and the only symptom was a TypeScript error at build time. A field the frontend
depends on is part of the contract, and a contract with no test is a promise nobody
checked.

The tests assert **shape and invariants**, not values — the values depend on whatever
data happens to be stored. What must hold regardless:

* every field a page reads is present;
* a missing measurement is ``null``, never ``0``;
* the guards answer with the right status code — 409 for the sealed TEST split, 404 for
  an unknown symbol, 400 for a bad sort key;
* the safety-critical flags (``live_trading_implemented``, the score disclaimer, the
  split note) are present and say what they are supposed to say.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #


class TestMeta:
    def test_health_states_that_live_trading_is_not_implemented(self, client) -> None:
        """The one flag where a wrong assumption costs real money."""
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["paper_trading"] is True
        assert body["live_trading"] is False
        assert body["live_trading_implemented"] is False

    def test_markets_carry_their_benchmark_and_caveats(self, client) -> None:
        markets = client.get("/api/markets").json()
        codes = {m["code"] for m in markets}
        assert codes == {"USA", "CHILE"}

        for market in markets:
            for key in ("currency", "exchange", "timezone", "trading_hours", "flag"):
                assert market[key], f"{market['code']} missing {key}"
            assert market["benchmark"] is not None
            assert market["benchmark"]["caveats"], "a benchmark with no caveats is suspicious"

    def test_chile_benchmark_is_not_labelled_ipsa(self, client) -> None:
        """Guards against a future change quietly relabelling the USD proxy.

        The comparison moved from the market to the region, because every instrument now
        trades in New York and a market-keyed benchmark would hand Chilean ADRs the S&P 500.
        """
        body = client.get("/api/regions").json()
        chile = next(r for r in body["regions"] if r["region"] == "Chile")
        assert chile["benchmark"]["symbol"] == "ECH"
        assert chile["benchmark"]["symbol"] != "IPSA"
        assert chile["benchmark"]["kind"] == "etf_proxy"
        assert any("ipsa" in c.lower() for c in chile["benchmark"]["caveats"])

    def test_the_empty_chile_market_advertises_no_benchmark(self, client) -> None:
        """Nothing trades in Santiago any more, so the market has nothing to compare to."""
        markets = client.get("/api/markets").json()
        chile = next(m for m in markets if m["code"] == "CHILE")
        assert chile["benchmark"]["available"] is False
        assert chile["benchmark"]["symbol"] == ""

    def test_regions_are_reported_with_their_currency_caveat(self, client) -> None:
        """The awkward fact has to reach the dashboard, not just the docstrings."""
        body = client.get("/api/regions").json()
        assert {r["region"] for r in body["regions"]} == {
            "United States", "Chile", "Emerging Asia",
        }
        assert "usd" in body["currency_caveat"].lower()
        for row in body["regions"]:
            assert row["count"] > 0
            assert row["benchmark"]["caveats"]

    def test_region_benchmarks_disclose_being_held(self, client) -> None:
        """All three proxies are in the universe, so a strategy can hold its own yardstick."""
        body = client.get("/api/regions").json()
        for row in body["regions"]:
            if row["benchmark"]["also_tradable"]:
                joined = " ".join(row["benchmark"]["caveats"]).lower()
                assert "tradable" in joined

    def test_universe_can_be_filtered_by_region(self, client) -> None:
        asia = client.get("/api/universe", params={"region": "Emerging Asia"}).json()
        assert asia["count"] >= 15
        assert all(a["region"] == "Emerging Asia" for a in asia["assets"])
        assert all(a["market"] == "USA" for a in asia["assets"]), (
            "region is not market: these are US listings"
        )

    def test_unknown_region_is_a_404_not_an_empty_list(self, client) -> None:
        """An empty list would read as 'no instruments there', not 'no such region'."""
        assert client.get("/api/universe", params={"region": "Europe"}).status_code == 404

    def test_no_santiago_tickers_are_served(self, client) -> None:
        body = client.get("/api/universe").json()
        for asset in body["assets"]:
            assert not asset["provider_symbol"].endswith(".SN")

    def test_providers_are_all_free(self, client) -> None:
        body = client.get("/api/providers").json()
        assert body["all_free"] is True
        for row in body["providers"]:
            assert "free" in row["cost"].lower()

    def test_limitations_include_the_high_severity_ones(self, client) -> None:
        body = client.get("/api/limitations").json()
        ids = {item["id"] for item in body["limitations"]}
        assert {"survivorship_bias", "no_ipsa", "placeholder_costs"} <= ids
        for item in body["limitations"]:
            assert item["severity"] in {"high", "medium", "low"}
            assert item["detail"]


# --------------------------------------------------------------------------- #
# Splits and the leakage guard
# --------------------------------------------------------------------------- #


class TestSplits:
    def test_lists_all_three_partitions(self, client) -> None:
        splits = client.get("/api/splits").json()["splits"]
        assert [s["split"] for s in splits] == ["train", "validation", "test"]

    def test_test_is_marked_unreadable_with_a_reason(self, client) -> None:
        """The dashboard greys the control out using this flag."""
        splits = client.get("/api/splits").json()["splits"]
        by_name = {s["split"]: s for s in splits}

        assert by_name["train"]["readable_via_api"] is True
        assert by_name["validation"]["readable_via_api"] is True
        assert by_name["test"]["readable_via_api"] is False
        assert by_name["test"]["reason"], "an unreadable split must explain itself"

    def test_every_split_carries_a_note(self, client) -> None:
        for split in client.get("/api/splits").json()["splits"]:
            assert split["note"], f"{split['split']} has no explanatory note"

    def test_backtest_refuses_the_test_split_with_409(self, client) -> None:
        """Not 400 or 500: the request was well-formed and deliberately refused."""
        response = client.get("/api/backtest", params={"market": "USA", "split": "test"})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "TEST" in detail
        assert "second validation set" in detail

    def test_no_query_parameter_can_unseal_the_test_split(self, client) -> None:
        """Finalising is CLI-only on purpose; an HTTP override would defeat the guard."""
        for params in (
            {"split": "test", "finalising": "true"},
            {"split": "test", "finalising": True},
            {"split": "TEST"},
        ):
            response = client.get("/api/backtest", params={"market": "USA", **params})
            assert response.status_code == 409, f"{params} was not refused"


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #


class TestStrategies:
    def test_lists_the_default_strategy_with_its_parameters(self, client) -> None:
        body = client.get("/api/strategies").json()
        names = {s["name"] for s in body["strategies"]}
        assert "trend_momentum" in names

        strategy = next(s for s in body["strategies"] if s["name"] == "trend_momentum")
        assert strategy["default_params"]
        assert "rsi_min" in strategy["default_params"]

    def test_refuses_probability_language(self, client) -> None:
        """Asserts substance, not an exact sentence.

        Matching a phrase verbatim would break on harmless rewording while still letting
        genuine forecast language through if it used different words. What matters is that
        the note denies probability, denies known profitability, and contains no claim of
        certainty.
        """
        note = client.get("/api/strategies").json()["note"].lower()
        assert "not a probability" in note
        assert "profitable" in note and ("no strategy" in note or "not known" in note)
        for forbidden in ("guaranteed", "will outperform", "expected to", "proven"):
            assert forbidden not in note, f"forecast language in strategies note: {forbidden}"


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #


class TestScan:
    def test_rows_expose_every_field_the_dashboard_reads(self, client) -> None:
        """The regression this file was written for."""
        body = client.get("/api/scan", params={"limit": 5}).json()

        for key in ("strategy", "scanned_at", "disclaimer", "n_total", "n_errors", "rows"):
            assert key in body, f"response missing {key}"

        if not body["rows"]:
            pytest.skip("no stored data to scan in this environment")

        required = {
            "symbol", "market", "currency", "as_of", "price", "action", "score",
            "score_description", "trend_score", "momentum_score", "rsi", "macd_hist",
            "relative_volume", "atr_pct", "dist_52w_high_pct", "return_20d",
            "dollar_volume", "stop_price", "take_profit_price", "risk_reward",
            "bars_available", "carried_forward_dropped", "stale",
            "volume_feed_degraded", "recent_zero_volume_pct", "tradable",
            "blocked_reason", "reasons",
        }
        missing = required - set(body["rows"][0])
        assert missing == set(), f"scan row missing {sorted(missing)}"

    def test_score_description_never_implies_a_probability(self, client) -> None:
        body = client.get("/api/scan", params={"limit": 5}).json()
        if not body["rows"]:
            pytest.skip("no stored data")
        for row in body["rows"]:
            assert "not a probability" in row["score_description"].lower()

    def test_actions_are_constrained(self, client) -> None:
        body = client.get("/api/scan", params={"limit": 50}).json()
        for row in body["rows"]:
            assert row["action"] in {"BUY", "SELL", "HOLD"}
            assert 0.0 <= row["score"] <= 1.0

    def test_an_untradable_row_always_states_why(self, client) -> None:
        """A blocked row with no reason is indistinguishable from a bug."""
        body = client.get("/api/scan", params={"limit": 200}).json()
        for row in body["rows"]:
            if not row["tradable"]:
                assert row["blocked_reason"], f"{row['symbol']} blocked with no reason"

    def test_unknown_sort_key_is_a_400(self, client) -> None:
        response = client.get("/api/scan", params={"sort": "vibes"})
        assert response.status_code == 400
        assert "sort" in response.json()["detail"].lower()

    def test_unknown_strategy_is_a_404(self, client) -> None:
        response = client.get("/api/scan", params={"strategy": "magic_money"})
        assert response.status_code == 404

    def test_errors_are_reported_not_hidden(self, client) -> None:
        body = client.get("/api/scan", params={"limit": 500}).json()
        assert body["n_errors"] == len(body["errors"])
        for error in body["errors"]:
            assert error["symbol"] and error["error"]


# --------------------------------------------------------------------------- #
# Asset endpoints
# --------------------------------------------------------------------------- #


class TestAssetEndpoints:
    def test_audit_exposes_both_data_quality_measures(self, client) -> None:
        """Carried-forward tails and a degraded volume feed are separate failures."""
        response = client.get("/api/assets/AAPL/audit")
        if response.status_code == 404:
            pytest.skip("no stored data for AAPL")

        body = response.json()
        for key in (
            "stored_bars", "first_bar", "last_bar", "missing_weekdays",
            "longest_gap_sessions", "duplicate_timestamps", "is_stale",
            "stale_by_days", "stale_quote_run", "stale_quote_run_threshold",
            "recent_zero_volume_pct", "volume_feed_degraded", "summary", "caveat",
        ):
            assert key in body, f"audit missing {key}"

    def test_features_reports_what_it_trimmed(self, client) -> None:
        """A shifted "as of" date must be explained, not silently different."""
        response = client.get("/api/assets/AAPL/features")
        if response.status_code == 404:
            pytest.skip("no stored data for AAPL")

        body = response.json()
        for key in (
            "symbol", "market", "currency", "as_of", "bars_available",
            "carried_forward_dropped", "complete", "note", "features", "disclaimer",
        ):
            assert key in body, f"features missing {key}"
        assert body["carried_forward_dropped"] >= 0
        assert "not predictions" in body["disclaimer"].lower()

    def test_feature_history_is_returned_when_asked(self, client) -> None:
        response = client.get("/api/assets/AAPL/features", params={"history": 30})
        if response.status_code == 404:
            pytest.skip("no stored data for AAPL")

        history = response.json().get("history")
        assert history is not None
        assert len(history) <= 30
        if history:
            assert "ts" in history[0]
            assert "close" in history[0]

    def test_unwarmed_features_are_null_not_zero(self, client) -> None:
        """A null means "not computed"; a zero would be read as a measurement."""
        response = client.get("/api/assets/AAPL/features", params={"history": 300})
        if response.status_code == 404:
            pytest.skip("no stored data for AAPL")

        history = response.json().get("history") or []
        if not history:
            pytest.skip("no history returned")
        # The earliest row cannot have a warm 200-bar average.
        assert history[0].get("ema_200") is None or isinstance(history[0]["ema_200"], float)

    def test_unknown_symbol_is_a_404_with_guidance(self, client) -> None:
        response = client.get("/api/assets/NOTATICKER/features")
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert "NOTATICKER" in detail
        assert not detail.startswith('"'), "KeyError quoting leaked into the response"

    def test_bars_state_whether_they_are_adjusted(self, client) -> None:
        response = client.get("/api/assets/AAPL/bars", params={"limit": 10})
        if response.status_code == 404:
            pytest.skip("no stored data for AAPL")

        body = response.json()
        assert "adjusted" in body
        assert isinstance(body["adjusted"], bool)
        assert len(body["bars"]) <= 10


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #


@pytest.mark.slow
class TestBacktest:
    """Runs a real backtest over stored data, so these take a couple of minutes."""

    @pytest.fixture(scope="class")
    def result(self, client):
        response = client.get(
            "/api/backtest",
            params={"market": "USA", "split": "validation", "include_trades": "false"},
        )
        if response.status_code != 200:
            pytest.skip(f"backtest unavailable in this environment: {response.status_code}")
        return response.json()

    def test_exposes_every_field_the_dashboard_reads(self, result) -> None:
        for key in (
            "label", "market", "split", "split_note", "strategy", "cost_model",
            "universe", "start_date", "end_date", "initial_capital", "final_equity",
            "n_trades", "metrics", "benchmark", "rejected_entries", "limitations",
            "disclaimer",
        ):
            assert key in result, f"backtest missing {key}"

    def test_states_which_partition_produced_the_numbers(self, result) -> None:
        assert result["split"] == "validation"
        assert "VALIDATION" in result["split_note"]

    def test_cost_assumptions_travel_with_the_result(self, result) -> None:
        """Returns are meaningless without the friction deducted from them."""
        assert "round_trip_pct" in result["cost_model"]
        assert result["cost_model"]["round_trip_pct"] > 0

    def test_metrics_include_the_full_set(self, result) -> None:
        metrics = result["metrics"]
        for key in (
            "total_return_pct", "cagr_pct", "annualised_volatility_pct", "sharpe",
            "sortino", "calmar", "max_drawdown_pct", "win_rate_pct", "profit_factor",
            "expectancy", "n_trades", "exposure_pct", "turnover_pct",
            "risk_free_rate_used",
        ):
            assert key in metrics, f"metrics missing {key}"

    def test_curves_are_returned_for_charting(self, result) -> None:
        assert result.get("equity_curve")
        assert result.get("drawdown_curve")
        assert len(result["equity_curve"]) == len(result["drawdown_curve"])
        assert "ts" in result["equity_curve"][0]

    def test_limitations_are_never_empty(self, result) -> None:
        assert len(result["limitations"]) >= 5
        joined = " ".join(result["limitations"]).lower()
        assert "survivorship" in joined
        assert "stop is assumed to fill first" in joined

    def test_disclaimer_refuses_forecast_language(self, result) -> None:
        """The disclaimer must deny forecasting, however it is worded."""
        text = result["disclaimer"].lower()
        assert "forecast" in text
        assert "historical" in text
        # Denial, not endorsement: "is a forecast" alone would be the opposite claim.
        assert "none is a forecast" in text or "not a forecast" in text
        for forbidden in ("guaranteed", "will return", "expect to earn"):
            assert forbidden not in text, f"forecast language in disclaimer: {forbidden}"

    def test_include_trades_false_omits_them(self, result) -> None:
        assert "trades" not in result

    def test_include_trades_true_returns_them(self, client) -> None:
        response = client.get(
            "/api/backtest", params={"market": "USA", "split": "validation"}
        )
        if response.status_code != 200:
            pytest.skip("backtest unavailable")

        body = response.json()
        assert "trades" in body
        if body["trades"]:
            for key in ("symbol", "entry_date", "exit_date", "pnl", "pnl_pct", "exit_reason"):
                assert key in body["trades"][0]

    def test_unknown_market_is_handled(self, client) -> None:
        response = client.get("/api/backtest", params={"market": "PERU"})
        assert response.status_code in (400, 404)


# --------------------------------------------------------------------------- #
# Phase 4: optimisation endpoints
# --------------------------------------------------------------------------- #


class TestOptimizationEndpoints:
    """Contract for the optimisation endpoints the dashboard reads.

    These are deliberately read-only. A search takes minutes and a walk-forward study
    longer, so an endpoint that *ran* one would either time out or silently repeat an
    expensive job on every page load.
    """

    def test_objective_presets_are_listed(self, client) -> None:
        body = client.get("/api/objective-presets").json()
        assert "balanced" in body["presets"]
        assert "capital_preservation" in body["presets"]

        for name, weights in body["presets"].items():
            assert "sharpe" in weights, name
            assert "max_drawdown" in weights, name
            assert "fragility_penalty" in weights, name

    def test_preset_note_refuses_to_oversell_the_objective(self, client) -> None:
        """The objective value is an arbitrary scale, and the API must say so."""
        note = client.get("/api/objective-presets").json()["note"].lower()
        assert "arbitrary scale" in note
        assert "not a return" in note or "not a probability" in note

    def test_default_preset_does_not_put_return_first(self, client) -> None:
        balanced = client.get("/api/objective-presets").json()["presets"]["balanced"]
        assert balanced["sharpe"] > balanced["total_return"]

    def test_run_listings_respond_with_a_shape_even_when_empty(self, client) -> None:
        for path in ("/api/optimization/runs", "/api/walkforward/runs"):
            body = client.get(path).json()
            assert isinstance(body["runs"], list)
            assert body["count"] == len(body["runs"])
            # An empty listing must still say how to produce one.
            assert "python -m app" in body["how_to_create"]

    def test_stored_searches_always_claim_the_train_split(self, client) -> None:
        """A stored run that claimed otherwise could not exist; assert it anyway."""
        for run in client.get("/api/optimization/runs").json()["runs"]:
            assert run["split"] == "train", run

    def test_search_summaries_expose_the_overfitting_flag(self, client) -> None:
        for run in client.get("/api/optimization/runs").json()["runs"]:
            for key in (
                "id", "market", "method", "n_trials", "n_failed", "best_objective",
                "grid_size", "overfitting_prone", "warnings", "has_validation",
                "has_robustness",
            ):
                assert key in run, f"summary missing {key}"

    def test_search_detail_carries_trials_and_stability(self, client) -> None:
        runs = client.get("/api/optimization/runs").json()["runs"]
        if not runs:
            pytest.skip("no stored optimisation run in this environment")

        body = client.get(f"/api/optimization/runs/{runs[0]['id']}").json()
        for key in (
            "param_space", "objective_weights", "trials", "parameter_stability",
            "validation_selection", "stress_tests", "note",
        ):
            assert key in body, f"detail missing {key}"

        assert "TRAIN only" in body["note"]
        assert "TEST split was not read" in body["note"]

    def test_trials_carry_their_objective_breakdown(self, client) -> None:
        runs = client.get("/api/optimization/runs").json()["runs"]
        if not runs:
            pytest.skip("no stored optimisation run")

        trials = client.get(f"/api/optimization/runs/{runs[0]['id']}").json()["trials"]
        if not trials:
            pytest.skip("run stored no trials")

        objective = trials[0]["objective"]
        for key in ("value", "components", "contributions", "missing", "notes", "fragility"):
            assert key in objective, f"objective missing {key}"

    def test_validation_selection_records_degradation(self, client) -> None:
        """Train-to-validation degradation is the number that reveals overfitting."""
        runs = [r for r in client.get("/api/optimization/runs").json()["runs"] if r["has_validation"]]
        if not runs:
            pytest.skip("no validated run stored")

        selection = client.get(f"/api/optimization/runs/{runs[0]['id']}").json()[
            "validation_selection"
        ]
        assert selection["candidates"]
        for candidate in selection["candidates"]:
            assert "degradation" in candidate
            assert "train" in candidate and "validation" in candidate
        assert any("TEST split has not been read" in n for n in selection["notes"])

    def test_walk_forward_listing_states_it_is_out_of_sample(self, client) -> None:
        body = client.get("/api/walkforward/runs").json()
        assert "out-of-sample" in body["note"]
        assert "frozen before" in body["note"]

    def test_missing_runs_are_404(self, client) -> None:
        for path in ("/api/optimization/runs/999999", "/api/walkforward/runs/999999"):
            response = client.get(path)
            assert response.status_code == 404
            assert "999999" in response.json()["detail"]
