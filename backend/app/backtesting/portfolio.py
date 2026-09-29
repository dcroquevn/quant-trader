"""Portfolio accounting for the backtester.

One job: track cash and positions so that **equity is always cash plus the mark-to-market
value of open positions**, with no unexplained drift. That identity is checked after
every fill in the test suite, because accounting bugs here do not look like bugs — they
look like alpha.

Conventions
-----------
* **Long only.** Shorting needs borrow costs and locate risk to be modelled honestly,
  and neither is available free. :class:`Position` therefore holds positive quantities.
* **One position per symbol.** Adding to a winner is a separate strategy decision that
  Phase 2 does not make; allowing it silently would make average entry price and
  holding period ambiguous.
* **Cash can never go negative.** Attempting it raises rather than quietly implying
  leverage that was never configured or charged for.
* **Multi-currency positions are never summed.** A USD portfolio and a CLP portfolio
  are tracked separately; combining them requires an FX series this project does not
  have, so :meth:`equity` refuses a mixed book rather than adding pesos to dollars.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.backtesting.costs import CostModel, FillCosts, Side

__all__ = ["Position", "Portfolio", "InsufficientCashError", "PositionExistsError"]


class InsufficientCashError(Exception):
    """A fill would have driven cash negative."""


class PositionExistsError(Exception):
    """An open position already exists for that symbol."""


@dataclass(slots=True)
class Position:
    """One open long position."""

    symbol: str
    market: str
    currency: str
    quantity: float
    entry_price: float
    """Actual fill price, including spread and slippage."""

    entry_date: datetime
    entry_commission: float
    entry_slippage: float
    entry_reason: str = ""

    stop_price: float | None = None
    take_profit_price: float | None = None
    trailing_stop_price: float | None = None

    highest_close: float = 0.0
    """Best close seen while held. Drives the trailing stop and favourable excursion."""

    lowest_close: float = 0.0
    bars_held: int = 0

    max_favorable_excursion_pct: float = 0.0
    max_adverse_excursion_pct: float = 0.0
    """Worst unrealised drawdown while held, in percent. Negative or zero.

    Recorded per trade because a setup with a good median outcome can still be
    untradeable: a routine -12% excursion stops you out long before the median arrives.
    """

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"Position quantity must be positive, got {self.quantity}")
        if self.entry_price <= 0:
            raise ValueError(f"Position entry price must be positive, got {self.entry_price}")
        if self.highest_close <= 0:
            self.highest_close = self.entry_price
        if self.lowest_close <= 0:
            self.lowest_close = self.entry_price

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealised_pnl(self, price: float) -> float:
        """Gross of the exit's costs, which are not known until the exit happens."""
        return (price - self.entry_price) * self.quantity

    def unrealised_pnl_pct(self, price: float) -> float:
        return (price / self.entry_price - 1.0) * 100.0

    def update_marks(self, high: float, low: float, close: float) -> None:
        """Record one bar's excursions and advance the trailing stop.

        Excursions use the bar's high and low, not its close: the point is the worst
        and best the position was actually at, which is what a real stop would have
        reacted to.
        """
        self.bars_held += 1
        self.highest_close = max(self.highest_close, close)
        self.lowest_close = min(self.lowest_close, close)

        favourable = (high / self.entry_price - 1.0) * 100.0
        adverse = (low / self.entry_price - 1.0) * 100.0
        self.max_favorable_excursion_pct = max(self.max_favorable_excursion_pct, favourable)
        self.max_adverse_excursion_pct = min(self.max_adverse_excursion_pct, adverse)

    def advance_trailing_stop(self, atr: float, atr_multiple: float) -> None:
        """Ratchet the trailing stop upward. It never moves down.

        A trailing stop that can loosen is not a stop; it is a way of turning a small
        loss into a large one.
        """
        if atr_multiple <= 0 or atr <= 0:
            return
        candidate = self.highest_close - atr_multiple * atr
        if self.trailing_stop_price is None or candidate > self.trailing_stop_price:
            self.trailing_stop_price = candidate

    @property
    def effective_stop(self) -> float | None:
        """The binding stop: the higher of the initial and trailing stops."""
        stops = [s for s in (self.stop_price, self.trailing_stop_price) if s is not None]
        return max(stops) if stops else None


@dataclass(slots=True)
class Portfolio:
    """Cash and open positions for one currency."""

    initial_capital: float
    currency: str
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    realised_pnl: float = 0.0
    total_commission: float = 0.0
    total_slippage: float = 0.0
    n_fills: int = 0

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError(
                f"initial_capital must be positive, got {self.initial_capital}"
            )
        if self.cash == 0.0:
            self.cash = self.initial_capital

    # ------------------------------------------------------------------ #
    # Valuation
    # ------------------------------------------------------------------ #

    def positions_value(self, prices: dict[str, float]) -> float:
        """Mark-to-market value of open positions.

        A symbol missing from ``prices`` raises. Valuing it at its entry price would
        silently freeze a position's contribution to equity — which looks like a
        drawdown that stopped, rather than a bug.
        """
        total = 0.0
        for symbol, position in self.positions.items():
            if symbol not in prices:
                raise KeyError(
                    f"No mark price for open position {symbol!r}. Every open position "
                    "must be priced on every bar, or equity is wrong."
                )
            total += position.market_value(prices[symbol])
        return total

    def equity(self, prices: dict[str, float]) -> float:
        """Cash plus position value. The single number that defines performance."""
        currencies = {p.currency for p in self.positions.values()}
        if currencies - {self.currency}:
            raise ValueError(
                f"Portfolio is denominated in {self.currency} but holds positions in "
                f"{sorted(currencies)}. Summing them would require an FX series this "
                "project does not have; run one portfolio per currency instead."
            )
        return self.cash + self.positions_value(prices)

    @property
    def n_positions(self) -> int:
        return len(self.positions)

    def exposure_pct(self, prices: dict[str, float]) -> float:
        """Invested fraction of equity, in percent."""
        equity = self.equity(prices)
        if equity <= 0:
            return 0.0
        return 100.0 * self.positions_value(prices) / equity

    # ------------------------------------------------------------------ #
    # Fills
    # ------------------------------------------------------------------ #

    def open_position(
        self,
        *,
        symbol: str,
        market: str,
        fill: FillCosts,
        cost_model: CostModel,
        when: datetime,
        stop_price: float | None = None,
        take_profit_price: float | None = None,
        reason: str = "",
    ) -> Position:
        """Buy ``fill.quantity`` and record the position."""
        if symbol in self.positions:
            raise PositionExistsError(
                f"Already holding {symbol}; this build does not add to positions."
            )

        delta = cost_model.cash_delta(fill, Side.BUY)
        if self.cash + delta < -1e-9:
            raise InsufficientCashError(
                f"Buying {fill.quantity:g} {symbol} costs {-delta:,.2f} "
                f"{self.currency} but only {self.cash:,.2f} is available."
            )

        self.cash += delta
        self.total_commission += fill.commission
        self.total_slippage += fill.slippage_cost
        self.n_fills += 1

        position = Position(
            symbol=symbol,
            market=market,
            currency=self.currency,
            quantity=fill.quantity,
            entry_price=fill.fill_price,
            entry_date=when,
            entry_commission=fill.commission,
            entry_slippage=fill.slippage_cost,
            entry_reason=reason,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
        )
        self.positions[symbol] = position
        return position

    def close_position(
        self,
        *,
        symbol: str,
        fill: FillCosts,
        cost_model: CostModel,
    ) -> tuple[Position, float]:
        """Sell the whole position. Returns ``(position, net_pnl)``.

        ``net_pnl`` is after both legs' commission and slippage — the only P&L figure
        worth reporting. The gross figure is kept on the trade record for diagnosis.
        """
        position = self.positions.pop(symbol, None)
        if position is None:
            raise KeyError(f"No open position in {symbol!r} to close")
        if abs(fill.quantity - position.quantity) > 1e-9:
            raise ValueError(
                f"Partial closes are not supported: tried to sell {fill.quantity:g} of "
                f"{position.quantity:g} {symbol}"
            )

        delta = cost_model.cash_delta(fill, Side.SELL)
        self.cash += delta
        self.total_commission += fill.commission
        self.total_slippage += fill.slippage_cost
        self.n_fills += 1

        gross = (fill.fill_price - position.entry_price) * position.quantity
        net = gross - position.entry_commission - fill.commission
        self.realised_pnl += net
        return position, net

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #

    def snapshot(self, prices: dict[str, float], when: datetime) -> dict:
        """Serialisable state, written to ``portfolio_snapshots``."""
        equity = self.equity(prices)
        return {
            "ts": when,
            "cash": round(self.cash, 4),
            "positions_value": round(self.positions_value(prices), 4),
            "equity": round(equity, 4),
            "n_positions": self.n_positions,
            "exposure_pct": round(self.exposure_pct(prices), 4),
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "entry_price": round(p.entry_price, 6),
                    "mark_price": round(prices[p.symbol], 6),
                    "unrealised_pnl": round(p.unrealised_pnl(prices[p.symbol]), 4),
                    "unrealised_pnl_pct": round(p.unrealised_pnl_pct(prices[p.symbol]), 4),
                    "bars_held": p.bars_held,
                    "stop": None if p.effective_stop is None else round(p.effective_stop, 6),
                }
                for p in self.positions.values()
            ],
        }
