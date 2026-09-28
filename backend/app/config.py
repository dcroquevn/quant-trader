"""Central configuration, loaded from environment / ``.env``.

Nothing here reaches the network and nothing here has a paid default. A missing
``.env`` yields a fully working local configuration.

Two settings are safety-critical and are validated hard:

``PAPER_TRADING`` / ``LIVE_TRADING``
    Live trading is not implemented. ``LIVE_TRADING=true`` is rejected at import
    time rather than silently ignored, so a stray environment variable cannot
    quietly flip the system into a mode it does not actually support.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# quant-trader/backend/app/config.py -> quant-trader/
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

DATA_DIR: Path = PROJECT_ROOT / "data"
REPORTS_DIR: Path = PROJECT_ROOT / "reports"
LOGS_DIR: Path = PROJECT_ROOT / "logs"


class MarketCostConfig(BaseModel):
    """Transaction costs for one market.

    Costs are deliberately not defaulted to zero. A backtest run with zero
    friction is not a conservative estimate, it is a wrong one, and the error
    grows with turnover.
    """

    commission_bps: float
    min_commission: float
    slippage_bps: float
    spread_bps: float

    def round_trip_bps(self) -> float:
        """Total assumed friction for a full entry + exit, in basis points.

        Commission and slippage are charged on both legs; ``spread_bps`` is
        treated as a half-spread, also paid on both legs.
        """
        one_way = self.commission_bps + self.slippage_bps + self.spread_bps
        return 2.0 * one_way


class Settings(BaseSettings):
    """Application settings.

    Field names map to upper-case environment variables (case-insensitive).
    """

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Trading mode (safety critical) ----------------------------------
    paper_trading: bool = True
    live_trading: bool = False

    # ---- Database --------------------------------------------------------
    database_url: str = "sqlite:///data/quant_trader.db"

    # ---- Providers -------------------------------------------------------
    default_provider_usa: str = "yfinance"
    default_provider_chile: str = "yfinance"
    provider_request_delay: float = 0.4
    provider_max_retries: int = 3
    provider_timeout: int = 30

    # ---- Alpaca (optional) -----------------------------------------------
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # ---- Telegram (optional) ---------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = False
    telegram_cooldown_seconds: int = 300

    # ---- Transaction costs: USA ------------------------------------------
    usa_commission_bps: float = 1.0
    usa_min_commission: float = 0.0
    usa_slippage_bps: float = 2.0
    usa_spread_bps: float = 1.0

    # ---- Transaction costs: Chile ----------------------------------------
    chile_commission_bps: float = 25.0
    chile_min_commission: float = 0.0
    chile_slippage_bps: float = 15.0
    chile_spread_bps: float = 10.0

    # ---- Risk ------------------------------------------------------------
    risk_per_trade_pct: float = 1.0
    max_position_size_pct: float = 10.0
    max_daily_loss_pct: float = 3.0
    max_portfolio_drawdown_pct: float = 20.0
    max_simultaneous_positions: int = 10
    max_market_exposure_pct: float = 100.0
    max_sector_exposure_pct: float = 35.0
    stale_data_max_age_days: int = 5

    # ---- Backtest defaults ----------------------------------------------
    initial_capital_usd: float = 100_000.0
    initial_capital_clp: float = 50_000_000.0

    # ---- Train / validation / test split --------------------------------
    split_train_start: date = date(2016, 1, 1)
    split_train_end: date = date(2021, 12, 31)
    split_validation_start: date = date(2022, 1, 1)
    split_validation_end: date = date(2023, 12, 31)
    split_test_start: date = date(2024, 1, 1)
    split_test_end: date = date(2026, 12, 31)

    # ---- API / logging ---------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"
    log_file: str = "logs/quant_trader.log"

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _check_trading_mode(self) -> "Settings":
        if self.live_trading:
            raise ValueError(
                "LIVE_TRADING=true is not supported: real order routing is not "
                "implemented in this build. Set LIVE_TRADING=false. A broker "
                "adapter interface exists (app.execution.broker.BrokerAdapter) "
                "but no live adapter is wired to it."
            )
        if not self.paper_trading:
            raise ValueError(
                "PAPER_TRADING=false with LIVE_TRADING=false leaves no execution "
                "mode enabled. Set PAPER_TRADING=true."
            )
        return self

    @model_validator(mode="after")
    def _check_split_ordering(self) -> "Settings":
        """Train < validation < test, strictly, with no overlap.

        An overlap here is the single easiest way to leak test data into
        optimisation, so it is a startup error rather than a runtime surprise.
        """
        spans = [
            ("train", self.split_train_start, self.split_train_end),
            ("validation", self.split_validation_start, self.split_validation_end),
            ("test", self.split_test_start, self.split_test_end),
        ]
        for name, start, end in spans:
            if start > end:
                raise ValueError(f"{name} split starts ({start}) after it ends ({end})")
        for (a_name, _, a_end), (b_name, b_start, _) in zip(spans, spans[1:]):
            if a_end >= b_start:
                raise ValueError(
                    f"{a_name} split ends {a_end} but {b_name} starts {b_start}: "
                    "splits must not overlap or touch"
                )
        return self

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #

    @property
    def resolved_database_url(self) -> str:
        """``database_url`` with relative SQLite paths anchored to the project root.

        ``sqlite:///data/quant_trader.db`` otherwise resolves against the current
        working directory, which would silently create a second, empty database
        whenever a command is run from a different folder.
        """
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return self.database_url

        raw = self.database_url[len(prefix) :]
        if raw.startswith(":memory:") or raw == "":
            return self.database_url

        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return prefix + path.as_posix()

    @property
    def log_path(self) -> Path:
        path = Path(self.log_file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    def costs_for(self, market: str) -> MarketCostConfig:
        """Transaction-cost configuration for ``market`` ("USA" or "CHILE")."""
        key = market.strip().upper()
        if key == "USA":
            return MarketCostConfig(
                commission_bps=self.usa_commission_bps,
                min_commission=self.usa_min_commission,
                slippage_bps=self.usa_slippage_bps,
                spread_bps=self.usa_spread_bps,
            )
        if key == "CHILE":
            return MarketCostConfig(
                commission_bps=self.chile_commission_bps,
                min_commission=self.chile_min_commission,
                slippage_bps=self.chile_slippage_bps,
                spread_bps=self.chile_spread_bps,
            )
        raise KeyError(
            f"No transaction-cost configuration for market {market!r}. "
            "Add <MARKET>_COMMISSION_BPS / _SLIPPAGE_BPS / _SPREAD_BPS to .env "
            "and extend Settings.costs_for()."
        )

    def split_bounds(self, split: str) -> tuple[date, date]:
        """Inclusive ``(start, end)`` for "train", "validation" or "test"."""
        key = split.strip().lower()
        table = {
            "train": (self.split_train_start, self.split_train_end),
            "validation": (self.split_validation_start, self.split_validation_end),
            "test": (self.split_test_start, self.split_test_end),
        }
        if key not in table:
            raise KeyError(f"Unknown split {split!r}; expected one of {sorted(table)}")
        return table[key]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so the ``.env`` file is read once. Tests that need a different
    configuration should call ``get_settings.cache_clear()``.
    """
    return Settings()


def ensure_directories() -> None:
    """Create the local directories the app writes into."""
    for directory in (DATA_DIR, REPORTS_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
