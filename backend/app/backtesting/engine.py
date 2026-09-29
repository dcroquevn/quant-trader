"""Event-driven backtester.

The timing convention, which is the whole ballgame
--------------------------------------------------
Every decision is made on information that existed when it was made. Concretely, for
each bar ``t`` the loop does four things **in this order**:

1. **Fill orders queued on bar t-1**, at bar ``t``'s *open*. A signal generated from
   bar ``t-1``'s close cannot be filled at that same close — the close has already
   happened by the time you know it. Filling at ``t``'s open is the earliest honest
   execution, and it is why a backtest here is worse than a naive one.
2. **Mark open positions** against bar ``t``'s high, low and close, advancing trailing
   stops and recording excursions.
3. **Check price-based exits** (stop, target, trailing) against bar ``t``'s intrabar
   range, filling immediately.
4. **Evaluate the strategy** on bar ``t``'s features and queue orders for bar ``t+1``.

Step 4 happens *after* step 3 so a stop-out and a fresh entry signal on the same bar do
not collide, and so no decision ever sees a fill that happened later than it.

Ambiguities resolved pessimistically
------------------------------------
**Stop and target inside the same bar.** Daily bars cannot say which came first, so the
**stop is assumed to hit first**. Any other choice flatters the strategy on exactly the
bars where it matters most. :data:`INTRABAR_ASSUMPTION` records this in every result.

**Gaps through the stop.** If a bar opens below the stop, the fill is at the *open*, not
at the stop price. You cannot be filled at a price the market skipped.

**The final open position** is closed at the last bar's close so the equity curve ends
on a realised number, and the trade is flagged ``end_of_backtest``.

What is not modelled
--------------------
No shorting, no leverage, no adding to positions, no partial fills, no intrabar path,
no size-dependent market impact, no dividends as cash (adjusted prices already embed
them in the price series, so total return is captured but the cash timing is not), no
taxes, no borrow. Each appears in :attr:`BacktestResult.limitations`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from app.backtesting.costs import CostModel, Side, cost_model_for
from app.backtesting.portfolio import InsufficientCashError, Portfolio
from app.config import get_settings
from app.core.logging import get_logger
from app.core.markets import get_market
from app.indicators.forward import assert_no_forward_columns
from app.strategies.base import Action, Strategy

logger = get_logger(__name__)

__all__ = [
    "TradeRecord",
    "BacktestConfig",
    "BacktestResult",
    "Backtester",
    "INTRABAR_ASSUMPTION",
]

INTRABAR_ASSUMPTION = (
    "When a bar's range contains both the stop and the take-profit, the stop is assumed "
    "to fill first. Daily bars cannot resolve intrabar order, and any other assumption "
    "flatters the strategy."
)

EXIT_STOP = "stop_loss"
EXIT_TRAILING = "trailing_stop"
EXIT_TARGET = "take_profit"
EXIT_SIGNAL = "signal"
EXIT_TIME = "max_holding_period"
EXIT_END = "end_of_backtest"


@dataclass(slots=True)
class TradeRecord:
    """One completed round trip. Mirrors the ``trades`` table."""

    symbol: str
    market: str
    currency: str
    entry_date: datetime
    entry_price: float
    exit_date: datetime
    exit_price: float
    quantity: float
    gross_pnl: float
    commission: float
    slippage_cost: float
    pnl: float
    """Net of both legs' commission. The only figure worth reporting."""

    pnl_pct: float
    holding_period_days: int
    bars_held: int
    entry_reason: str
    exit_reason: str
    stop_price: float | None
    take_profit_price: float | None
    max_adverse_excursion_pct: float
    max_favorable_excursion_pct: float

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "currency": self.currency,
            "entry_date": self.entry_date.isoformat(),
            "entry_price": round(self.entry_price, 6),
            "exit_date": self.exit_date.isoformat(),
            "exit_price": round(self.exit_price, 6),
            "quantity": self.quantity,
            "gross_pnl": round(self.gross_pnl, 4),
            "commission": round(self.commission, 4),
            "slippage_cost": round(self.slippage_cost, 4),
            "pnl": round(self.pnl, 4),
            "pnl_pct": round(self.pnl_pct, 4),
            "holding_period_days": self.holding_period_days,
            "bars_held": self.bars_held,
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
            "max_adverse_excursion_pct": round(self.max_adverse_excursion_pct, 4),
            "max_favorable_excursion_pct": round(self.max_favorable_excursion_pct, 4),
        }


@dataclass(slots=True)
class BacktestConfig:
    """Everything that defines a run, other than the data itself."""

    market: str
    initial_capital: float
    risk_per_trade_pct: float = 1.0
    max_position_size_pct: float = 10.0
    max_simultaneous_positions: int = 10
    split: str = "full"
    """Which data partition this run read: train, validation, test or full.

    Persisted so a report can state where its numbers came from, and so an audit can
    spot a "test" result that was quietly produced during a parameter search.
    """

    label: str = ""

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError(f"initial_capital must be positive, got {self.initial_capital}")
        if not 0 < self.risk_per_trade_pct <= 100:
            raise ValueError(
                f"risk_per_trade_pct must be in (0, 100], got {self.risk_per_trade_pct}"
            )
        if not 0 < self.max_position_size_pct <= 100:
            raise ValueError(
                f"max_position_size_pct must be in (0, 100], got {self.max_position_size_pct}"
            )
        if self.max_simultaneous_positions < 1:
            raise ValueError(
                f"max_simultaneous_positions must be >= 1, got "
                f"{self.max_simultaneous_positions}"
            )
        if self.split not in ("train", "validation", "test", "full"):
            raise ValueError(
                f"split must be train, validation, test or full; got {self.split!r}"
            )


@dataclass(slots=True)
class BacktestResult:
    """Everything a run produced."""

    config: BacktestConfig
    strategy: dict[str, Any]
    cost_model: dict[str, Any]
    universe: list[str]
    start_date: datetime
    end_date: datetime
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    snapshots: list[dict] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    benchmark_metrics: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    rejected_entries: dict[str, int] = field(default_factory=dict)
    """Why candidate entries were not taken, by reason. A strategy constantly blocked by
    position limits or cash is telling you something the summary metrics will not."""

    @property
    def final_equity(self) -> float:
        return float(self.equity_curve.iloc[-1]) if len(self.equity_curve) else 0.0

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    def trades_frame(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame(
                columns=[
                    "symbol", "market", "entry_date", "entry_price", "exit_date",
                    "exit_price", "quantity", "pnl", "pnl_pct", "holding_period_days",
                    "entry_reason", "exit_reason",
                ]
            )
        return pd.DataFrame([t.to_dict() for t in self.trades])


class Backtester:
    """Runs one strategy over one market's bars."""

    def __init__(
        self,
        strategy: Strategy,
        config: BacktestConfig,
        *,
        cost_model: CostModel | None = None,
    ) -> None:
        self.strategy = strategy
        self.config = config
        self.market = get_market(config.market)
        self.costs = cost_model or cost_model_for(config.market)
        self.settings = get_settings()

    # ------------------------------------------------------------------ #
    # Sizing
    # ------------------------------------------------------------------ #

    def _position_size(
        self,
        *,
        equity: float,
        cash: float,
        entry_reference: float,
        stop_price: float,
    ) -> tuple[float, str]:
        """Shares to buy, and the binding constraint. Returns ``(quantity, reason)``.

        Risk-based sizing: ``risk_amount / stop_distance``. The position is then capped
        by ``max_position_size_pct`` of equity and by available cash.

        Sizing off the *stop distance* rather than a fixed notional is what makes risk
        comparable across instruments — a volatile name gets a smaller position for the
        same currency risk, automatically.
        """
        stop_distance = entry_reference - stop_price
        if stop_distance <= 0:
            return 0.0, "stop_not_below_entry"

        risk_amount = equity * self.config.risk_per_trade_pct / 100.0
        by_risk = risk_amount / stop_distance

        max_notional = equity * self.config.max_position_size_pct / 100.0
        by_size_cap = max_notional / entry_reference

        by_cash = self.costs.affordable_quantity(cash, entry_reference, Side.BUY)

        quantity = float(int(min(by_risk, by_size_cap, by_cash)))
        if quantity < 1:
            if by_cash < 1:
                return 0.0, "insufficient_cash"
            if by_size_cap < 1:
                return 0.0, "position_cap_below_one_share"
            return 0.0, "risk_budget_below_one_share"

        binding = min(
            (by_risk, "risk_per_trade"),
            (by_size_cap, "max_position_size"),
            (by_cash, "available_cash"),
            key=lambda pair: pair[0],
        )[1]
        return quantity, binding

    # ------------------------------------------------------------------ #
    # Exit resolution
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_price_exit(
        position, bar: pd.Series
    ) -> tuple[str, float] | None:
        """Decide whether a price-based exit triggered on this bar, and at what price.

        Returns ``(reason, fill_reference_price)`` or ``None``.

        The stop is checked before the target, so a bar containing both exits at the
        stop — see :data:`INTRABAR_ASSUMPTION`. A gap through the stop fills at the
        open, because the market never traded at the stop price.
        """
        open_, high, low = float(bar["open"]), float(bar["high"]), float(bar["low"])
        stop = position.effective_stop
        target = position.take_profit_price

        if stop is not None and low <= stop:
            reason = (
                EXIT_TRAILING
                if position.trailing_stop_price is not None
                and position.trailing_stop_price >= (position.stop_price or float("-inf"))
                else EXIT_STOP
            )
            # Gap down through the stop: you get the open, not the stop.
            fill_reference = min(stop, open_)
            return reason, fill_reference

        if target is not None and high >= target:
            # Gap up through the target: you get the open, which is better than the
            # target. Capping it at the target would understate the fill, which is the
            # one direction of error this project does not need to guard against --
            # but it would also be wrong, so take the open.
            fill_reference = max(target, open_)
            return EXIT_TARGET, fill_reference

        return None

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def run(
        self,
        features_by_symbol: dict[str, pd.DataFrame],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> BacktestResult:
        """Run the backtest.

        ``features_by_symbol`` maps canonical symbol to a frame from
        ``app.indicators.registry.compute_features``. Every frame must carry the bar
        columns plus the strategy's required features, and none may carry
        forward-looking columns.
        """
        if not features_by_symbol:
            raise ValueError("Cannot backtest an empty universe")

        for symbol, frame in features_by_symbol.items():
            assert_no_forward_columns(frame, context=f"backtest input for {symbol}")

        frames = self._slice(features_by_symbol, start, end)
        calendar = self._calendar(frames)
        if len(calendar) < 2:
            raise ValueError(
                f"Need at least 2 bars to backtest (a signal on one bar fills on the "
                f"next); the sliced window has {len(calendar)}."
            )

        portfolio = Portfolio(
            initial_capital=self.config.initial_capital, currency=self.market.currency
        )
        trades: list[TradeRecord] = []
        snapshots: list[dict] = []
        equity_points: list[tuple[datetime, float]] = []
        rejected: dict[str, int] = {}
        pending: list[dict[str, Any]] = []

        params = getattr(self.strategy, "p", None)
        trailing_multiple = float(getattr(params, "trailing_stop_atr_multiple", 0.0) or 0.0)
        max_holding = int(getattr(params, "max_holding_bars", 0) or 0)

        for bar_index, timestamp in enumerate(calendar):
            bars = self._bars_at(frames, timestamp)

            # ---- 1. Fill orders queued on the previous bar -------------
            for order in pending:
                symbol = order["symbol"]
                bar = bars.get(symbol)
                if bar is None:
                    # The instrument did not trade on the fill bar. The order is
                    # abandoned rather than carried forward: a stale intent executed
                    # days later is not what the strategy decided.
                    rejected["no_bar_on_fill_day"] = rejected.get("no_bar_on_fill_day", 0) + 1
                    continue
                filled = self._process_order(
                    portfolio=portfolio,
                    symbol=symbol,
                    bar=bar,
                    order=order,
                    when=timestamp,
                    rejected=rejected,
                )
                if filled is not None:
                    trades.append(filled)
            pending = []

            # ---- 2 & 3. Mark positions and resolve price exits ---------
            for symbol in list(portfolio.positions):
                bar = bars.get(symbol)
                if bar is None:
                    continue
                position = portfolio.positions[symbol]
                position.update_marks(
                    float(bar["high"]), float(bar["low"]), float(bar["close"])
                )
                if trailing_multiple > 0 and not pd.isna(bar.get("atr_14", float("nan"))):
                    position.advance_trailing_stop(float(bar["atr_14"]), trailing_multiple)

                resolved = self._resolve_price_exit(position, bar)
                if resolved is not None:
                    reason, reference = resolved
                    trades.append(
                        self._exit(
                            portfolio=portfolio,
                            symbol=symbol,
                            reference_price=reference,
                            when=timestamp,
                            reason=reason,
                        )
                    )
                    continue

                if max_holding and position.bars_held >= max_holding:
                    trades.append(
                        self._exit(
                            portfolio=portfolio,
                            symbol=symbol,
                            reference_price=float(bar["close"]),
                            when=timestamp,
                            reason=EXIT_TIME,
                        )
                    )

            # ---- 4. Decide, and queue for the next bar -----------------
            is_last_bar = bar_index == len(calendar) - 1
            if not is_last_bar:
                for symbol, bar in bars.items():
                    frame = frames[symbol]
                    position_index = frame.index.get_loc(timestamp)
                    holding = symbol in portfolio.positions

                    decision = self.strategy.evaluate(
                        frame, in_position=holding, index=int(position_index)
                    )

                    if holding and decision.action is Action.SELL:
                        # Signal exits fill at the next bar's open, like entries. An
                        # exit decided from a close cannot fill at that close.
                        pending.append(
                            {
                                "symbol": symbol,
                                "action": Action.SELL,
                                "reason": "; ".join(decision.reasons) or EXIT_SIGNAL,
                            }
                        )
                    elif not holding and decision.action is Action.BUY:
                        if portfolio.n_positions + sum(
                            1 for o in pending if o["action"] is Action.BUY
                        ) >= self.config.max_simultaneous_positions:
                            rejected["max_positions"] = rejected.get("max_positions", 0) + 1
                            continue
                        pending.append(
                            {
                                "symbol": symbol,
                                "action": Action.BUY,
                                "stop_price": decision.stop_price,
                                "take_profit_price": decision.take_profit_price,
                                "reference_close": float(bar["close"]),
                                "score": decision.score,
                                "reason": "; ".join(decision.reasons),
                            }
                        )

                # Highest-scoring candidates first, so the position limit binds on the
                # weakest rather than on whichever symbol happened to be iterated last.
                pending.sort(key=lambda o: o.get("score", 0.0), reverse=True)

            # ---- Mark equity at this bar's close -----------------------
            prices = self._mark_prices(portfolio, bars, frames, timestamp)
            equity = portfolio.equity(prices)
            equity_points.append((timestamp, equity))
            snapshots.append(portfolio.snapshot(prices, timestamp))

        # ---- Close whatever is still open --------------------------------
        final_ts = calendar[-1]
        final_bars = self._bars_at(frames, final_ts)
        for symbol in list(portfolio.positions):
            bar = final_bars.get(symbol)
            reference = (
                float(bar["close"])
                if bar is not None
                else portfolio.positions[symbol].entry_price
            )
            trades.append(
                self._exit(
                    portfolio=portfolio,
                    symbol=symbol,
                    reference_price=reference,
                    when=final_ts,
                    reason=EXIT_END,
                )
            )
        if portfolio.positions:  # pragma: no cover -- defensive
            raise RuntimeError("Positions remained open after final liquidation")

        equity_curve = pd.Series(
            [value for _, value in equity_points],
            index=pd.DatetimeIndex([ts for ts, _ in equity_points], name="ts"),
            name="equity",
        )
        # The final close realises the last position, so restate the last point.
        if len(equity_curve):
            equity_curve.iloc[-1] = portfolio.cash

        return BacktestResult(
            config=self.config,
            strategy=self.strategy.describe(),
            cost_model=self.costs.describe(),
            universe=sorted(frames),
            start_date=calendar[0].to_pydatetime(),
            end_date=calendar[-1].to_pydatetime(),
            trades=trades,
            equity_curve=equity_curve,
            snapshots=snapshots,
            rejected_entries=rejected,
            limitations=self._limitations(),
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _process_order(
        self,
        *,
        portfolio: Portfolio,
        symbol: str,
        bar: pd.Series,
        order: dict[str, Any],
        when: datetime,
        rejected: dict[str, int],
    ) -> TradeRecord | None:
        """Execute one queued order at this bar's open.

        Returns a :class:`TradeRecord` when the order closed a position, otherwise
        ``None``. The caller is responsible for collecting it -- an earlier version
        stashed exits on the instance and never read them back, which silently dropped
        every signal-driven trade from the results.
        """
        if order["action"] is Action.SELL:
            if symbol not in portfolio.positions:
                # Already closed by a stop or target on the bar the signal was raised.
                rejected["exit_already_closed"] = rejected.get("exit_already_closed", 0) + 1
                return None
            return self._exit(
                portfolio=portfolio,
                symbol=symbol,
                reference_price=float(bar["open"]),
                when=when,
                reason=order.get("reason") or EXIT_SIGNAL,
            )

        if symbol in portfolio.positions:
            rejected["already_holding"] = rejected.get("already_holding", 0) + 1
            return None
        if portfolio.n_positions >= self.config.max_simultaneous_positions:
            rejected["max_positions"] = rejected.get("max_positions", 0) + 1
            return None

        reference = float(bar["open"])
        stop = order.get("stop_price")
        if stop is None:
            rejected["no_stop_price"] = rejected.get("no_stop_price", 0) + 1
            return None

        # The stop was computed from the previous bar's close. Re-anchor it to the
        # actual entry price so the risked *distance* is what the strategy intended,
        # rather than whatever the overnight gap turned the absolute level into. Without
        # this, a gap up leaves the stop far below entry and the position is sized as
        # though it were low-risk.
        decision_close = order.get("reference_close")
        if decision_close is not None:
            intended_distance = float(decision_close) - float(stop)
            if intended_distance > 0:
                stop = reference - intended_distance

        if stop >= reference:
            rejected["stop_not_below_entry"] = rejected.get("stop_not_below_entry", 0) + 1
            return None

        # Size against mark-to-market equity at this bar, not entry-price equity: the
        # risk budget is a fraction of what the account is worth now.
        marks = self._mark_prices_for(portfolio, bar, symbol)
        quantity, binding = self._position_size(
            equity=portfolio.equity(marks),
            cash=portfolio.cash,
            entry_reference=reference,
            stop_price=stop,
        )
        if quantity < 1:
            rejected[binding] = rejected.get(binding, 0) + 1
            return None

        target = order.get("take_profit_price")
        if target is not None and decision_close is not None:
            intended_target_distance = float(target) - float(decision_close)
            if intended_target_distance > 0:
                target = reference + intended_target_distance

        fill = self.costs.price_fill(reference, quantity, Side.BUY)
        try:
            portfolio.open_position(
                symbol=symbol,
                market=self.config.market,
                fill=fill,
                cost_model=self.costs,
                when=when,
                stop_price=stop,
                take_profit_price=target,
                reason=order.get("reason", ""),
            )
        except InsufficientCashError:
            rejected["insufficient_cash"] = rejected.get("insufficient_cash", 0) + 1
        return None

    @staticmethod
    def _mark_prices_for(
        portfolio: Portfolio, bar: pd.Series, entering_symbol: str
    ) -> dict[str, float]:
        """Marks for sizing: the entering bar's open for held names, entry price otherwise.

        Approximate by design. A precise mark would need every held instrument's bar on
        this timestamp, which the caller does not have here, and the difference changes
        the risk budget by a fraction of a percent. Using the *entry* price for held
        names is the conservative side of that approximation.
        """
        marks = {s: p.entry_price for s, p in portfolio.positions.items()}
        if entering_symbol in marks:
            marks[entering_symbol] = float(bar["open"])
        return marks

    def _exit(
        self,
        *,
        portfolio: Portfolio,
        symbol: str,
        reference_price: float,
        when: datetime,
        reason: str,
    ) -> TradeRecord:
        position = portfolio.positions[symbol]
        fill = self.costs.price_fill(reference_price, position.quantity, Side.SELL)
        closed, net = portfolio.close_position(
            symbol=symbol, fill=fill, cost_model=self.costs
        )

        gross = (fill.fill_price - closed.entry_price) * closed.quantity
        cost_basis = closed.entry_price * closed.quantity
        return TradeRecord(
            symbol=symbol,
            market=closed.market,
            currency=closed.currency,
            entry_date=closed.entry_date,
            entry_price=closed.entry_price,
            exit_date=when,
            exit_price=fill.fill_price,
            quantity=closed.quantity,
            gross_pnl=gross,
            commission=closed.entry_commission + fill.commission,
            slippage_cost=closed.entry_slippage + fill.slippage_cost,
            pnl=net,
            pnl_pct=(net / cost_basis * 100.0) if cost_basis else 0.0,
            holding_period_days=max(0, (when - closed.entry_date).days),
            bars_held=closed.bars_held,
            entry_reason=closed.entry_reason,
            exit_reason=reason,
            stop_price=closed.stop_price,
            take_profit_price=closed.take_profit_price,
            max_adverse_excursion_pct=closed.max_adverse_excursion_pct,
            max_favorable_excursion_pct=closed.max_favorable_excursion_pct,
        )

    @staticmethod
    def _slice(
        features_by_symbol: dict[str, pd.DataFrame],
        start: datetime | None,
        end: datetime | None,
    ) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for symbol, frame in features_by_symbol.items():
            sliced = frame
            if start is not None:
                sliced = sliced[sliced.index >= pd.Timestamp(start, tz="UTC")]
            if end is not None:
                sliced = sliced[sliced.index <= pd.Timestamp(end, tz="UTC")]
            if not sliced.empty:
                out[symbol] = sliced
        if not out:
            raise ValueError(
                f"No bars remain for any symbol between {start} and {end}. "
                "Check the requested window against `python -m app coverage`."
            )
        return out

    @staticmethod
    def _calendar(frames: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
        """Union of every symbol's timestamps, ascending.

        A union rather than an intersection: requiring every symbol to have a bar would
        silently drop whole sessions because one thinly traded Chilean name did not
        print, and would shrink a 30-symbol universe to the calendar of its least
        liquid member.
        """
        index = pd.DatetimeIndex([])
        for frame in frames.values():
            index = index.union(frame.index)
        return index.sort_values()

    @staticmethod
    def _bars_at(
        frames: dict[str, pd.DataFrame], timestamp: pd.Timestamp
    ) -> dict[str, pd.Series]:
        bars: dict[str, pd.Series] = {}
        for symbol, frame in frames.items():
            if timestamp in frame.index:
                bars[symbol] = frame.loc[timestamp]
        return bars

    @staticmethod
    def _mark_prices(
        portfolio: Portfolio,
        bars: dict[str, pd.Series],
        frames: dict[str, pd.DataFrame],
        timestamp: pd.Timestamp,
    ) -> dict[str, float]:
        """A mark for every open position, even one that did not trade today.

        A position whose instrument did not print is marked at its most recent prior
        close. Skipping it would drop it out of equity for that bar, producing a phantom
        drawdown and recovery in the equity curve.
        """
        prices: dict[str, float] = {}
        for symbol in portfolio.positions:
            bar = bars.get(symbol)
            if bar is not None:
                prices[symbol] = float(bar["close"])
                continue
            frame = frames[symbol]
            prior = frame[frame.index <= timestamp]
            prices[symbol] = (
                float(prior["close"].iloc[-1])
                if not prior.empty
                else portfolio.positions[symbol].entry_price
            )
        return prices

    def _limitations(self) -> list[str]:
        return [
            INTRABAR_ASSUMPTION,
            "Survivorship bias: the universe contains only instruments that exist "
            "today, so results are measured on survivors and are optimistic by an "
            "unknown amount.",
            "Long only. No shorting, so no borrow cost or locate risk is modelled.",
            "No leverage, no adding to positions, no partial fills.",
            "Market impact is not size-dependent, so large positions in illiquid "
            "instruments have their costs understated.",
            "Spreads are constant, whereas real spreads widen exactly when you most "
            "want to exit.",
            "No taxes. Chilean and US capital-gains treatment differ substantially.",
            "Dividends are embedded in adjusted prices, so total return is captured but "
            "the cash timing of payments is not.",
            f"Transaction costs are the configured assumptions for {self.config.market}, "
            "not a broker's actual schedule: "
            f"{self.costs.round_trip_cost_pct():.3f}% round trip.",
        ]
