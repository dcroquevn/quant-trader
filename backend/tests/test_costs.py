"""Transaction cost tests.

The invariant that matters most: **friction must always hurt**. A sign error here makes
costs subsidise the strategy, and the resulting backtest looks merely good rather than
obviously broken. Several tests below exist purely to pin that sign down.
"""

from __future__ import annotations

import pytest

from app.backtesting.costs import BPS, CostModel, Side, cost_model_for
from app.config import MarketCostConfig


def model(
    commission_bps: float = 10.0,
    min_commission: float = 0.0,
    slippage_bps: float = 5.0,
    spread_bps: float = 5.0,
    market: str = "USA",
) -> CostModel:
    return CostModel(
        market=market,
        config=MarketCostConfig(
            commission_bps=commission_bps,
            min_commission=min_commission,
            slippage_bps=slippage_bps,
            spread_bps=spread_bps,
        ),
    )


class TestSide:
    def test_signs_push_against_the_trader(self) -> None:
        assert Side.BUY.sign == 1
        assert Side.SELL.sign == -1


class TestFillPrice:
    def test_a_buy_fills_above_the_reference(self) -> None:
        costs = model(slippage_bps=5.0, spread_bps=5.0)
        assert costs.fill_price(100.0, Side.BUY) == pytest.approx(100.0 * (1 + 10 * BPS))

    def test_a_sell_fills_below_the_reference(self) -> None:
        costs = model(slippage_bps=5.0, spread_bps=5.0)
        assert costs.fill_price(100.0, Side.SELL) == pytest.approx(100.0 * (1 - 10 * BPS))

    def test_friction_never_helps(self) -> None:
        """The single most important property in this module.

        A round trip at an unchanged price must lose money. If this ever passes with
        equality, costs have stopped being applied; if it passes with a gain, a sign is
        inverted and every backtest is wrong in the flattering direction.
        """
        costs = model()
        buy = costs.fill_price(100.0, Side.BUY)
        sell = costs.fill_price(100.0, Side.SELL)
        assert buy > 100.0 > sell

    def test_zero_cost_configuration_is_a_no_op(self) -> None:
        """Explicit zeroes are allowed -- for sensitivity analysis -- and must do nothing."""
        costs = model(commission_bps=0, slippage_bps=0, spread_bps=0)
        assert costs.fill_price(100.0, Side.BUY) == pytest.approx(100.0)

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_non_positive_reference_price_is_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError, match="reference_price must be positive"):
            model().fill_price(bad, Side.BUY)


class TestCommission:
    def test_proportional_to_notional(self) -> None:
        assert model(commission_bps=10.0).commission_for(10_000.0) == pytest.approx(10.0)

    def test_minimum_binds_on_small_orders(self) -> None:
        """The floor is what makes tiny Chilean orders uneconomic."""
        costs = model(commission_bps=10.0, min_commission=5.0)
        assert costs.commission_for(100.0) == pytest.approx(5.0)
        assert costs.commission_for(1_000_000.0) == pytest.approx(1000.0)

    def test_negative_notional_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="notional must be non-negative"):
            model().commission_for(-1.0)


class TestPriceFill:
    def test_reports_every_component(self) -> None:
        costs = model(commission_bps=10.0, slippage_bps=5.0, spread_bps=5.0)
        fill = costs.price_fill(100.0, 100, Side.BUY)

        assert fill.reference_price == 100.0
        assert fill.fill_price == pytest.approx(100.1)
        assert fill.quantity == 100
        assert fill.commission == pytest.approx(fill.notional * 10 * BPS)
        # 10 bps of adverse move on 100 shares of a 100-unit price.
        assert fill.slippage_cost == pytest.approx(0.1 * 100)

    def test_slippage_cost_is_never_negative(self) -> None:
        for side in (Side.BUY, Side.SELL):
            assert model().price_fill(50.0, 10, side).slippage_cost >= 0

    def test_total_cost_sums_commission_and_slippage(self) -> None:
        fill = model().price_fill(100.0, 10, Side.BUY)
        assert fill.total_cost == pytest.approx(fill.commission + fill.slippage_cost)

    def test_zero_quantity_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="quantity must be positive"):
            model().price_fill(100.0, 0, Side.BUY)


class TestCashDelta:
    def test_buy_reduces_cash_by_notional_plus_commission(self) -> None:
        costs = model()
        fill = costs.price_fill(100.0, 10, Side.BUY)
        expected = -(fill.fill_price * 10 + fill.commission)
        assert costs.cash_delta(fill, Side.BUY) == pytest.approx(expected)

    def test_sell_increases_cash_by_notional_minus_commission(self) -> None:
        costs = model()
        fill = costs.price_fill(100.0, 10, Side.SELL)
        expected = fill.fill_price * 10 - fill.commission
        assert costs.cash_delta(fill, Side.SELL) == pytest.approx(expected)

    def test_slippage_is_not_double_counted(self) -> None:
        """Slippage lives inside fill_price; adding it again would charge it twice.

        Asserted directly because the error is invisible in aggregate results -- it just
        makes every strategy look slightly worse, which nobody investigates.
        """
        costs = model()
        fill = costs.price_fill(100.0, 10, Side.BUY)
        delta = costs.cash_delta(fill, Side.BUY)
        assert delta == pytest.approx(-(fill.fill_price * 10 + fill.commission))
        assert delta != pytest.approx(
            -(fill.fill_price * 10 + fill.commission + fill.slippage_cost)
        )

    def test_a_flat_round_trip_loses_money(self) -> None:
        """End-to-end cash identity: buy and sell at the same reference, lose friction."""
        costs = model()
        buy = costs.price_fill(100.0, 100, Side.BUY)
        sell = costs.price_fill(100.0, 100, Side.SELL)
        net = costs.cash_delta(buy, Side.BUY) + costs.cash_delta(sell, Side.SELL)
        assert net < 0


class TestAffordableQuantity:
    def test_leaves_enough_for_commission(self) -> None:
        """Sizing that ignores commission is how a fill ends with negative cash."""
        costs = model(commission_bps=10.0)
        cash = 10_000.0
        quantity = costs.affordable_quantity(cash, 100.0)

        fill = costs.price_fill(100.0, quantity, Side.BUY)
        assert cash + costs.cash_delta(fill, Side.BUY) >= 0

    def test_accounts_for_the_commission_floor(self) -> None:
        costs = model(commission_bps=1.0, min_commission=50.0)
        cash = 1_000.0
        quantity = costs.affordable_quantity(cash, 100.0)

        fill = costs.price_fill(100.0, quantity, Side.BUY)
        assert cash + costs.cash_delta(fill, Side.BUY) >= 0

    def test_returns_whole_shares(self) -> None:
        quantity = model().affordable_quantity(10_000.0, 333.0)
        assert quantity == int(quantity)

    def test_zero_cash_buys_nothing(self) -> None:
        assert model().affordable_quantity(0.0, 100.0) == 0.0
        assert model().affordable_quantity(-5.0, 100.0) == 0.0


class TestRoundTripAndDescription:
    def test_round_trip_charges_both_legs(self) -> None:
        costs = model(commission_bps=10.0, slippage_bps=5.0, spread_bps=5.0)
        # (10 + 5 + 5) bps per leg, two legs = 40 bps = 0.40%
        assert costs.round_trip_cost_pct() == pytest.approx(0.40)

    def test_breakeven_move_equals_round_trip_cost(self) -> None:
        costs = model()
        assert costs.breakeven_move_pct() == pytest.approx(costs.round_trip_cost_pct())

    def test_describe_is_serialisable_and_complete(self) -> None:
        described = model(market="CHILE").describe()
        for key in (
            "market", "commission_bps", "min_commission",
            "slippage_bps", "spread_bps", "round_trip_pct",
        ):
            assert key in described


class TestConfiguredMarkets:
    def test_both_markets_resolve(self) -> None:
        assert cost_model_for("USA").market == "USA"
        assert cost_model_for("chile").market == "CHILE"

    def test_no_market_is_free_to_trade(self) -> None:
        for market in ("USA", "CHILE"):
            assert cost_model_for(market).round_trip_cost_pct() > 0

    def test_chile_is_assumed_more_expensive(self) -> None:
        assert (
            cost_model_for("CHILE").round_trip_cost_pct()
            > cost_model_for("USA").round_trip_cost_pct()
        )

    def test_unknown_market_raises(self) -> None:
        with pytest.raises(KeyError):
            cost_model_for("PERU")
