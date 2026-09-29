"""Transaction costs.

Every fill in this project goes through here. There is no code path that trades for
free, because a zero-friction backtest is not a conservative estimate — it is a wrong
one, and the error compounds with turnover. A strategy that trades daily and looks
profitable at zero cost is usually a strategy that pays its entire edge to the broker.

Three separate frictions, applied per leg
-----------------------------------------
**Commission** — what the broker charges. Basis points of notional, with an optional
currency floor (``min_commission``), which is what actually bites on small Chilean
orders.

**Spread** — you buy at the ask and sell at the bid. Modelled as a half-spread paid
on entry *and* exit, so a round trip pays the full spread once.

**Slippage** — the gap between the price your decision was based on and the price you
actually got. Distinct from spread: spread is the quoted cost of immediacy, slippage
is everything else (your own market impact, latency, the market moving while you act).

All three are configured per market in ``.env``. Chile's placeholders are deliberately
higher than the US ones: lower liquidity, wider spreads, brokerage minimums.

What this does not model
------------------------
Costs here are deterministic functions of notional. Reality is worse in ways that are
not free to model honestly:

* **Market impact is not size-dependent.** A 10,000-share order in a thin Chilean name
  moves the price against you far more than a 100-share order. That non-linearity is
  not captured, so large positions in illiquid instruments are *understated*.
* **Spreads are constant.** In reality they widen exactly when you most want to exit.
* **Borrow costs for shorts are absent.** Only long positions are supported today.
* **No taxes.** Chilean and US capital-gains treatment differ substantially.

These are listed in every backtest's ``limitations``, not buried here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.config import MarketCostConfig, get_settings

__all__ = ["Side", "FillCosts", "CostModel", "cost_model_for"]

BPS = 1e-4
"""One basis point. 10 bps = 0.10%."""


class Side(str, Enum):
    """Direction of a fill."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """``+1`` for a buy, ``-1`` for a sell.

        Used to push a fill price against the trader: a buy fills higher than the
        decision price, a sell fills lower. Getting this sign backwards produces a
        backtest where friction *helps*, which is the most embarrassing possible bug.
        """
        return 1 if self is Side.BUY else -1


@dataclass(frozen=True, slots=True)
class FillCosts:
    """The full cost breakdown of one simulated fill."""

    reference_price: float
    """Price the decision was based on — usually the bar's open."""

    fill_price: float
    """Price actually paid or received, after spread and slippage."""

    quantity: float
    commission: float
    slippage_cost: float
    """Currency value of the adverse price move, relative to ``reference_price``."""

    @property
    def notional(self) -> float:
        """Value transacted at the fill price."""
        return self.fill_price * self.quantity

    @property
    def total_cost(self) -> float:
        """Every currency unit lost to friction on this fill."""
        return self.commission + self.slippage_cost


@dataclass(frozen=True, slots=True)
class CostModel:
    """Applies one market's cost configuration to a proposed trade."""

    market: str
    config: MarketCostConfig

    # ------------------------------------------------------------------ #
    # Price adjustment
    # ------------------------------------------------------------------ #

    def fill_price(self, reference_price: float, side: Side) -> float:
        """Price after spread and slippage, always against the trader.

        A buy fills above the reference, a sell below it. The adjustment is
        ``(spread_bps + slippage_bps)`` in the adverse direction.
        """
        if reference_price <= 0:
            raise ValueError(f"reference_price must be positive, got {reference_price}")

        adverse_bps = self.config.spread_bps + self.config.slippage_bps
        return reference_price * (1.0 + side.sign * adverse_bps * BPS)

    def commission_for(self, notional: float) -> float:
        """Commission on a given notional, respecting the minimum.

        The floor is what makes small orders uneconomic in Chile, and omitting it is
        how a backtest ends up recommending 40 tiny positions.
        """
        if notional < 0:
            raise ValueError(f"notional must be non-negative, got {notional}")
        proportional = notional * self.config.commission_bps * BPS
        return max(proportional, self.config.min_commission)

    # ------------------------------------------------------------------ #
    # Full fill
    # ------------------------------------------------------------------ #

    def price_fill(
        self, reference_price: float, quantity: float, side: Side
    ) -> FillCosts:
        """Cost one fill of ``quantity`` at ``reference_price``.

        ``quantity`` is always positive; direction comes from ``side``.
        """
        if quantity <= 0:
            raise ValueError(f"quantity must be positive, got {quantity}")

        fill = self.fill_price(reference_price, side)
        notional = fill * quantity
        commission = self.commission_for(notional)
        # Adverse price movement, expressed in currency. Always non-negative.
        slippage_cost = abs(fill - reference_price) * quantity

        return FillCosts(
            reference_price=reference_price,
            fill_price=fill,
            quantity=quantity,
            commission=commission,
            slippage_cost=slippage_cost,
        )

    def cash_delta(self, fill: FillCosts, side: Side) -> float:
        """Signed cash change for a fill: negative for a buy, positive for a sell.

        Slippage is already embedded in ``fill.fill_price``, so only commission is
        subtracted here. Adding ``slippage_cost`` again would charge it twice — a
        mistake that silently doubles friction and is hard to spot in aggregate
        results, so :mod:`tests.test_costs` asserts the identity directly.
        """
        gross = fill.fill_price * fill.quantity
        if side is Side.BUY:
            return -(gross + fill.commission)
        return gross - fill.commission

    # ------------------------------------------------------------------ #
    # Sizing helpers
    # ------------------------------------------------------------------ #

    def affordable_quantity(
        self, cash: float, reference_price: float, side: Side = Side.BUY
    ) -> float:
        """Largest whole-share quantity ``cash`` can buy, commission included.

        Solves for quantity rather than approximating, because ignoring commission
        when sizing is how a backtest ends up with negative cash on the fill that
        was supposed to use exactly the cash available.
        """
        if cash <= 0:
            return 0.0

        fill = self.fill_price(reference_price, side)
        if fill <= 0:
            return 0.0

        # cash >= fill * q + max(fill * q * rate, min_commission)
        rate = self.config.commission_bps * BPS
        # First assume the proportional commission binds.
        quantity = cash / (fill * (1.0 + rate))
        # If the floor binds instead, re-solve against it.
        if fill * quantity * rate < self.config.min_commission:
            quantity = (cash - self.config.min_commission) / fill

        return max(0.0, float(int(quantity)))

    def round_trip_cost_pct(self) -> float:
        """Total friction for a full entry and exit, as a percentage of notional.

        The single most useful number for sanity-checking a strategy: if the average
        winning trade is smaller than this, the strategy loses money no matter how
        often it is right.
        """
        return self.config.round_trip_bps() * BPS * 100.0

    def breakeven_move_pct(self) -> float:
        """Percentage price move needed just to cover friction.

        Ignores the commission floor, which makes small positions worse still.
        """
        return self.round_trip_cost_pct()

    def describe(self) -> dict[str, float | str]:
        """Serialisable record of the assumptions, stored on every backtest.

        Persisted so a result can be reproduced and so a reader can see what was
        assumed rather than having to trust that it was reasonable.
        """
        return {
            "market": self.market,
            "commission_bps": self.config.commission_bps,
            "min_commission": self.config.min_commission,
            "slippage_bps": self.config.slippage_bps,
            "spread_bps": self.config.spread_bps,
            "round_trip_pct": round(self.round_trip_cost_pct(), 4),
        }


def cost_model_for(market: str) -> CostModel:
    """Cost model for ``market``, from the configured ``.env`` values.

    Raises ``KeyError`` for a market with no cost configuration rather than
    defaulting to zero — the whole point is that trading is never free.
    """
    settings = get_settings()
    return CostModel(market=market.strip().upper(), config=settings.costs_for(market))
