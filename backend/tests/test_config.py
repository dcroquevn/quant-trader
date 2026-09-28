"""Configuration and safety-guard tests.

Two of these matter more than the rest: live trading must be impossible to enable
by accident, and the train/validation/test splits must not overlap. Both are
checked at construction time so a bad ``.env`` fails at startup rather than
midway through an optimisation run.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import MarketCostConfig, Settings


def build(**overrides) -> Settings:
    """A Settings instance from explicit values, ignoring any local ``.env``."""
    defaults = {"paper_trading": True, "live_trading": False}
    return Settings(_env_file=None, **{**defaults, **overrides})


class TestTradingModeGuards:
    def test_defaults_are_paper_only(self) -> None:
        settings = build()
        assert settings.paper_trading is True
        assert settings.live_trading is False

    def test_live_trading_is_rejected(self) -> None:
        """LIVE_TRADING=true must fail loudly, not be silently ignored.

        Ignoring it would leave a user believing live trading was enabled and
        working, when in fact no order would ever be routed.
        """
        with pytest.raises(ValidationError, match="not supported"):
            build(live_trading=True)

    def test_disabling_both_modes_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no execution mode enabled"):
            build(paper_trading=False, live_trading=False)


class TestSplitGuards:
    def test_default_splits_do_not_overlap(self) -> None:
        settings = build()
        _, train_end = settings.split_bounds("train")
        val_start, val_end = settings.split_bounds("validation")
        test_start, _ = settings.split_bounds("test")
        assert train_end < val_start
        assert val_end < test_start

    def test_overlapping_splits_are_rejected(self) -> None:
        """An overlap is the easiest way to leak test data into optimisation."""
        with pytest.raises(ValidationError, match="must not overlap"):
            build(split_train_end="2022-06-30")

    def test_inverted_split_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="starts .* after it ends"):
            build(split_train_start="2021-01-01", split_train_end="2016-01-01")

    def test_unknown_split_name_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown split"):
            build().split_bounds("holdout")


class TestTransactionCosts:
    def test_both_markets_have_independent_costs(self) -> None:
        settings = build()
        usa = settings.costs_for("USA")
        chile = settings.costs_for("CHILE")
        assert isinstance(usa, MarketCostConfig)
        assert usa.commission_bps != chile.commission_bps

    def test_chile_is_assumed_more_expensive_than_usa(self) -> None:
        """The placeholder defaults must not flatter the harder market.

        Chilean equities are less liquid with wider spreads. Defaulting both
        markets to US-style costs would make Chilean backtests look better than
        they could possibly be.
        """
        settings = build()
        assert settings.costs_for("CHILE").round_trip_bps() > settings.costs_for("USA").round_trip_bps()

    def test_round_trip_charges_both_legs(self) -> None:
        costs = MarketCostConfig(
            commission_bps=5.0, min_commission=0.0, slippage_bps=3.0, spread_bps=2.0
        )
        assert costs.round_trip_bps() == pytest.approx(2 * (5.0 + 3.0 + 2.0))

    def test_no_market_defaults_to_zero_cost(self) -> None:
        """Zero friction is not a conservative assumption; it is a wrong one."""
        settings = build()
        for market in ("USA", "CHILE"):
            assert settings.costs_for(market).round_trip_bps() > 0.0

    def test_unknown_market_raises(self) -> None:
        with pytest.raises(KeyError, match="No transaction-cost configuration"):
            build().costs_for("PERU")

    def test_costs_are_configurable(self) -> None:
        settings = build(usa_commission_bps=12.5, usa_slippage_bps=0.0, usa_spread_bps=0.0)
        assert settings.costs_for("USA").commission_bps == pytest.approx(12.5)
        assert settings.costs_for("USA").round_trip_bps() == pytest.approx(25.0)


class TestPathsAndLogging:
    def test_relative_sqlite_path_is_anchored_to_the_project(self) -> None:
        """A relative SQLite path must not follow the working directory.

        Otherwise running the CLI from a different folder silently creates a
        second, empty database instead of opening the real one.
        """
        settings = build(database_url="sqlite:///data/test_anchor.db")
        resolved = settings.resolved_database_url
        assert resolved.startswith("sqlite:///")
        assert "quant-trader" in resolved.replace("\\", "/")

    def test_memory_database_is_passed_through(self) -> None:
        settings = build(database_url="sqlite:///:memory:")
        assert settings.resolved_database_url == "sqlite:///:memory:"

    def test_postgres_url_is_untouched(self) -> None:
        """PostgreSQL readiness is a claim this test keeps honest."""
        url = "postgresql+psycopg://user:pw@localhost:5432/quant"
        assert build(database_url=url).resolved_database_url == url

    def test_log_level_is_normalised(self) -> None:
        assert build(log_level="debug").log_level == "DEBUG"

    def test_invalid_log_level_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="LOG_LEVEL must be one of"):
            build(log_level="CHATTY")


class TestSecrets:
    def test_optional_credentials_default_to_empty(self) -> None:
        """The system must run with no API keys at all."""
        settings = build()
        assert settings.alpaca_api_key == ""
        assert settings.alpaca_secret_key == ""
        assert settings.telegram_bot_token == ""
        assert settings.telegram_enabled is False

    def test_env_example_contains_no_real_secrets(self) -> None:
        """.env.example must ship placeholders, never populated credentials."""
        from app.config import PROJECT_ROOT

        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "TELEGRAM_BOT_TOKEN"):
            line = next(
                line for line in text.splitlines() if line.startswith(f"{key}=")
            )
            assert line == f"{key}=", f"{key} in .env.example is not empty: {line!r}"

    def test_env_example_defaults_to_paper_trading(self) -> None:
        from app.config import PROJECT_ROOT

        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "PAPER_TRADING=true" in text
        assert "LIVE_TRADING=false" in text

    def test_gitignore_excludes_env(self) -> None:
        from app.config import PROJECT_ROOT

        ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert ".env" in [line.strip() for line in ignored]
