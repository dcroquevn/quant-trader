"""ORM models.

Conventions that hold across every table
----------------------------------------
**Timestamps are UTC.** Bar timestamps are normalised to UTC on ingest by the
data engine. SQLite cannot store an offset, so nothing here relies on one being
round-tripped; the exchange-local view is reconstructed from
``Market.timezone`` when displaying. Mixing naive local timestamps into this
schema is the fastest way to produce an off-by-one-session look-ahead bug.

**Money is ``Float``.** Acceptable for research, where results are reported to
two significant figures. It is *not* acceptable for live accounting; the broker
is the ledger of record, and a live build would need ``Numeric``.

**JSON columns hold open-ended payloads** (indicator values, parameter sets,
metric dictionaries). ``sqlalchemy.JSON`` maps to ``TEXT`` on SQLite and native
``JSON`` on PostgreSQL, so the schema stays portable. The cost is that JSON
contents are not indexable on SQLite -- anything that needs filtering gets a
real column instead.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

__all__ = [
    "Market",
    "Asset",
    "Bar",
    "Feature",
    "Strategy",
    "Signal",
    "Backtest",
    "OptimizationRun",
    "WalkForwardRun",
    "Trade",
    "Order",
    "PortfolioSnapshot",
    "RiskEvent",
    "Holding",
    "Alert",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #


class Market(Base):
    """A trading venue. Mirrors ``app.core.markets.Market`` into the database."""

    __tablename__ = "markets"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    session_open: Mapped[str] = mapped_column(String(8), nullable=False)
    session_close: Mapped[str] = mapped_column(String(8), nullable=False)
    benchmark_symbol: Mapped[str] = mapped_column(String(32), nullable=False)

    assets: Mapped[list["Asset"]] = relationship(
        back_populates="market", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Market {self.code} {self.currency}>"


class Asset(Base):
    """An instrument, keyed by canonical symbol within a market."""

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("symbol", "market_code", name="uq_asset_symbol_market"),
        Index("ix_asset_market_active", "market_code", "is_active"),
        CheckConstraint(
            "asset_class IN ('equity', 'etf', 'index', 'fund', 'other')",
            name="ck_asset_class",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    market_code: Mapped[str] = mapped_column(
        ForeignKey("markets.code", ondelete="CASCADE"), nullable=False
    )
    sector: Mapped[str] = mapped_column(String(64), nullable=False, default="Unknown")
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False, default="equity")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    is_benchmark: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Provider whose symbol was verified to return data, if any."""

    provider_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """The resolved vendor ticker, cached so resolution runs once per asset."""

    resolution_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """When symbol resolution last ran. ``NULL`` means never verified."""

    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """Data caveats: restructurings, listing dates, illiquidity."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    market: Mapped["Market"] = relationship(back_populates="assets")
    bars: Mapped[list["Bar"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<Asset {self.symbol} ({self.market_code})>"


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


class Bar(Base):
    """One OHLCV bar.

    The unique constraint on ``(asset_id, timeframe, ts)`` is what makes
    incremental downloads idempotent: re-fetching an overlapping window updates
    rows instead of duplicating them.

    ``adj_close`` carries the split- and dividend-adjusted close where the
    provider supplies one. Indicators run on adjusted prices; an unadjusted
    series shows a fake gap at every split, which a momentum rule happily
    trades. ``is_adjusted`` records whether adjustment actually happened rather
    than letting callers assume it.
    """

    __tablename__ = "bars"
    __table_args__ = (
        UniqueConstraint("asset_id", "timeframe", "ts", name="uq_bar_asset_tf_ts"),
        # Covers the dominant query: one asset's series over a date range.
        Index("ix_bar_asset_tf_ts", "asset_id", "timeframe", "ts"),
        # Covers cross-sectional scans: every asset on one date.
        Index("ix_bar_tf_ts", "timeframe", "ts"),
        CheckConstraint("high >= low", name="ck_bar_high_ge_low"),
        CheckConstraint("volume >= 0", name="ck_bar_volume_non_negative"),
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_bar_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    adj_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    is_adjusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    asset: Mapped["Asset"] = relationship(back_populates="bars")

    def __repr__(self) -> str:
        return f"<Bar asset={self.asset_id} {self.timeframe} {self.ts} c={self.close}>"


class Feature(Base):
    """Cached indicator values for one ``(asset, timeframe, timestamp)``.

    Stored as a JSON map rather than one row per indicator: a wide universe with
    thirty indicators over ten years is millions of narrow rows, and the whole
    map is always read together anyway.

    ``feature_version`` invalidates the cache when an indicator's definition
    changes -- without it, a fixed look-ahead bug would keep serving the buggy
    values it had already cached.
    """

    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint(
            "asset_id", "timeframe", "ts", "feature_version", name="uq_feature_asset_tf_ts_ver"
        ),
        Index("ix_feature_asset_tf_ts", "asset_id", "timeframe", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v1")
    values: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


# --------------------------------------------------------------------------- #
# Strategies and signals
# --------------------------------------------------------------------------- #


class Strategy(Base):
    """A named, versioned strategy with its parameter set."""

    __tablename__ = "strategies"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_strategy_name_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Signal(Base):
    """A BUY / SELL / HOLD decision for one asset at one timestamp.

    ``score`` is a strategy-internal ranking number on an arbitrary scale. It is
    explicitly **not** a probability of profit, and the API layer must never
    present it as one -- hence the separate ``evidence`` column, which carries
    the historical-analogue statistics that do have a defensible interpretation.
    """

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("asset_id", "strategy_id", "ts", name="uq_signal_asset_strategy_ts"),
        Index("ix_signal_ts_action", "ts", "action"),
        CheckConstraint("action IN ('BUY', 'SELL', 'HOLD')", name="ck_signal_action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="1D")

    action: Mapped[str] = mapped_column(String(8), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)

    reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    features: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


# --------------------------------------------------------------------------- #
# Research runs
# --------------------------------------------------------------------------- #


class Backtest(Base):
    """One backtest execution and its results.

    ``split`` records which data partition the run read: ``train``,
    ``validation``, ``test`` or ``full``. Persisting it means a report can state
    where a number came from, and an audit can detect a "test" result that was
    quietly produced during a parameter search.
    """

    __tablename__ = "backtests"
    __table_args__ = (
        Index("ix_backtest_strategy_created", "strategy_id", "created_at"),
        CheckConstraint(
            "split IN ('train', 'validation', 'test', 'full')", name="ck_backtest_split"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    optimization_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("optimization_runs.id", ondelete="SET NULL"), nullable=True
    )
    walk_forward_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("walk_forward_runs.id", ondelete="SET NULL"), nullable=True
    )

    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    market: Mapped[str] = mapped_column(String(16), nullable=False, default="USA")
    universe: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="1D")
    split: Mapped[str] = mapped_column(String(16), nullable=False, default="full")

    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    initial_capital: Mapped[float] = mapped_column(Float, nullable=False)
    final_equity: Mapped[float | None] = mapped_column(Float, nullable=True)

    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    cost_model: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    """The exact commission/slippage/spread assumptions used, for reproducibility."""

    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    benchmark_metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    equity_curve: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    data_source: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    limitations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    """Known caveats, e.g. survivorship bias, price-only benchmark, thin history."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    trades: Mapped[list["Trade"]] = relationship(
        back_populates="backtest", cascade="all, delete-orphan", passive_deletes=True
    )


class OptimizationRun(Base):
    """A parameter search.

    ``split`` is constrained to ``train`` at the application level: the optimiser
    is never allowed to read validation or test data. The column exists so that
    constraint is *auditable after the fact*, not merely asserted in code.
    """

    __tablename__ = "optimization_runs"
    __table_args__ = (
        CheckConstraint("method IN ('grid', 'random', 'bayesian')", name="ck_opt_method"),
        CheckConstraint("split = 'train'", name="ck_opt_split_train_only"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    market: Mapped[str] = mapped_column(String(16), nullable=False, default="USA")
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="grid")
    split: Mapped[str] = mapped_column(String(16), nullable=False, default="train")

    param_space: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    objective_weights: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    n_trials: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    best_params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    best_objective: Mapped[float | None] = mapped_column(Float, nullable=True)

    trials: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    """Per-trial params and metrics, used for heatmaps and stability analysis."""

    robustness: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    """Sensitivity results: parameter, slippage, commission, Monte Carlo."""

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WalkForwardRun(Base):
    """A rolling train/test walk-forward analysis."""

    __tablename__ = "walk_forward_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    market: Mapped[str] = mapped_column(String(16), nullable=False, default="USA")

    train_years: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    test_years: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    param_space: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    windows: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    """Per-window record: bounds, chosen params, out-of-sample metrics, trades."""

    aggregate_metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    """Metrics over the stitched out-of-sample equity curve -- the honest number."""

    parameter_stability: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    """How much each parameter moved between windows. Wild swings = overfitting."""

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# Execution records
# --------------------------------------------------------------------------- #


class Order(Base):
    """An order, simulated or broker-routed.

    ``client_order_id`` is globally unique and is the duplicate-order guard: the
    same intent submitted twice hits a database constraint instead of opening a
    second position.
    """

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_order_client_id"),
        Index("ix_order_status_created", "status", "created_at"),
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_order_side"),
        CheckConstraint(
            "status IN ('PENDING', 'SUBMITTED', 'PARTIAL', 'FILLED', 'CANCELLED', 'REJECTED')",
            name="ck_order_status",
        ),
        CheckConstraint("quantity > 0", name="ck_order_quantity_positive"),
        CheckConstraint("mode IN ('paper', 'backtest', 'live')", name="ck_order_mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="paper")

    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="MARKET")
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    filled_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Trade(Base):
    """A completed round trip, or an open position when ``exit_date`` is NULL."""

    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trade_backtest", "backtest_id"),
        Index("ix_trade_asset_entry", "asset_id", "entry_date"),
        CheckConstraint("direction IN ('LONG', 'SHORT')", name="ck_trade_direction"),
        CheckConstraint("quantity > 0", name="ck_trade_quantity_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_id: Mapped[int | None] = mapped_column(
        ForeignKey("backtests.id", ondelete="CASCADE"), nullable=True
    )
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="LONG")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="backtest")

    entry_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)

    gross_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Net P&L after commission and slippage -- the only number worth reporting."""

    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    max_adverse_excursion_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_favorable_excursion_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    entry_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    exit_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    backtest: Mapped["Backtest | None"] = relationship(back_populates="trades")



class Holding(Base):
    """A position the user actually bought, with their own money, through their own broker.

    Distinct from :class:`Trade`, which is something this software simulated. The distinction
    is the point: a Trade's entry price is a modelled fill and a Holding's is what was paid.
    Letting the two share a table would let a backtest's P&L and a real one end up in the same
    average.

    This system never places the order. It records what the user says they did, watches the
    strategy's exit rule against it, and tells them when the rule fires. Whether to act is
    theirs.

    ``stop_price`` and ``take_profit_price`` are stored at entry rather than recomputed,
    because a level recomputed from today's data is not the level the position was opened on
    and an exit check against it would be answering a different question.
    """

    __tablename__ = "holdings"
    __table_args__ = (
        Index("ix_holding_open", "closed_on", "symbol"),
        CheckConstraint("quantity > 0", name="ck_holding_quantity_positive"),
        CheckConstraint("entry_price > 0", name="ck_holding_entry_price_positive"),
        CheckConstraint(
            "exit_price IS NULL OR exit_price > 0", name="ck_holding_exit_price_positive"
        ),
        CheckConstraint(
            "closed_on IS NULL OR closed_on >= opened_on", name="ck_holding_dates_ordered"
        ),
        # A closed holding needs both halves of the exit or its P&L is not computable.
        CheckConstraint(
            "(closed_on IS NULL AND exit_price IS NULL)"
            " OR (closed_on IS NOT NULL AND exit_price IS NOT NULL)",
            name="ck_holding_exit_complete",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    region: Mapped[str] = mapped_column(String(32), nullable=False, default="United States")

    broker: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    """Free text. This system has no broker connection; it is a note to the user."""

    opened_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    """What was actually paid per share, as reported by the user. Not a modelled fill."""

    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    """Shares held. Fractional is normal: a broker selling by cash amount produces 0.0592."""

    entry_fees: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    entry_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    """The cash the user said they spent, before conversion. Null when they gave a share count.

    Stored even though ``quantity`` is derived from it, because a derived number should not be
    the only record of the fact it came from -- "I put in 20.36 dollars" is what the user will
    remember, and 0.0592 shares is not.
    """

    entry_amount_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    entry_fx_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Units of ``entry_amount_currency`` per USD, when a conversion happened.

    Recorded so the share count can be checked later. The rate is a daily close, not the rate the
    broker actually gave, so it is an approximation of a number the user could look up exactly.
    """

    entry_price_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    """True when the entry price came from the latest stored close, not from the user.

    This table exists to keep real fills apart from modelled ones, so an inferred price has to
    announce itself. Every surface that shows a P&L derived from it says so.
    """

    closed_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fees: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """The exact parameters the exit rule is evaluated with.

    Pinned per holding so re-tuning the strategy later does not retroactively change what the
    watch would have said about a position already open.
    """

    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    exit_signal_on: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """The bar date on which the exit rule first fired. Null while it has not."""

    exit_signal_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_checked_on: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """When the watch last ran on this holding. A stale value means no one is watching."""

    gross_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Net of the fees the user reported. The only figure worth showing."""

    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    entry_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    exit_note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    @property
    def is_open(self) -> bool:
        return self.closed_on is None


class Alert(Base):
    """One notification, and whether it actually went out.

    Delivery is recorded separately from the condition that caused it because they fail
    independently: a Telegram token can expire, the network can be down, the user can have
    blocked the bot. A system whose only output is a message has to be able to say whether the
    message arrived.

    ``dedupe_key`` is uniquely constrained. A watch run daily would otherwise re-send the same
    exit signal every morning until the user sold, and an alert that repeats is an alert that
    gets muted.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_alert_dedupe"),
        Index("ix_alert_created", "created_at"),
        CheckConstraint(
            "status IN ('PENDING', 'SENT', 'FAILED', 'SUPPRESSED')", name="ck_alert_status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    holding_id: Mapped[int | None] = mapped_column(
        ForeignKey("holdings.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    subject: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="console")

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PortfolioSnapshot(Base):
    """Portfolio state at one point in time."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("backtest_id", "ts", "mode", name="uq_snapshot_backtest_ts_mode"),
        Index("ix_snapshot_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_id: Mapped[int | None] = mapped_column(
        ForeignKey("backtests.id", ondelete="CASCADE"), nullable=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="backtest")

    cash: Mapped[float] = mapped_column(Float, nullable=False)
    positions_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    n_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    daily_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    exposure_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    exposure_by_market: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    positions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class RiskEvent(Base):
    """A risk limit breach or kill-switch activation.

    Every event is persisted, including ones that only blocked an entry. A
    strategy that is constantly hitting limits is telling you something the
    summary metrics will not.
    """

    __tablename__ = "risk_events"
    __table_args__ = (
        Index("ix_risk_event_ts_severity", "ts", "severity"),
        CheckConstraint("severity IN ('INFO', 'WARNING', 'CRITICAL')", name="ck_risk_severity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    backtest_id: Mapped[int | None] = mapped_column(
        ForeignKey("backtests.id", ondelete="CASCADE"), nullable=True
    )

    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    """e.g. ``max_daily_loss``, ``stale_data``, ``duplicate_order``, ``kill_switch``."""

    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="WARNING")
    market: Mapped[str | None] = mapped_column(String(16), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)

    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    limit_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    blocked_action: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """True when this event actually prevented an order."""
