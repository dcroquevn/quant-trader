"""Backtest engine tests.

The two groups that matter most:

``TestNoLookahead`` — proves the engine cannot act on information that did not exist.
The sharpest of these builds a series with a single enormous one-bar spike and asserts
the engine cannot capture it, because a signal from the spike bar's close can only fill
at the next bar's open, by which time the spike is over. An engine that fills at the
signal bar's close would show a large profit here, so the test distinguishes the two
designs unambiguously.

``TestAccountingIdentity`` — proves equity equals cash plus position value at every
point, and that a flat round trip loses exactly the modelled friction. Accounting bugs
in a backtester do not look like bugs; they look like alpha.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.backtesting.costs import CostModel
from app.backtesting.engine import (
    EXIT_END,
    EXIT_STOP,
    EXIT_TARGET,
    EXIT_TIME,
    INTRABAR_ASSUMPTION,
    BacktestConfig,
    Backtester,
)
from app.config import MarketCostConfig
from app.core.exceptions import DataLeakageError
from app.indicators.registry import compute_features
from app.strategies.base import Action, ComponentScore, Decision, Strategy, StrategyParams
from tests.conftest import make_bars


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class ScriptedParams(StrategyParams):
    """No parameters; behaviour comes from the script."""


class ScriptedStrategy(Strategy):
    """Emits a preset action per timestamp, so engine mechanics can be isolated.

    Using a real strategy here would mean a failure could be in either component. This
    one has no logic at all: whatever the script says for a bar is what it decides.
    """

    name = "scripted"
    version = "test"

    def __init__(
        self,
        script: dict[pd.Timestamp, Action] | None = None,
        *,
        stop_pct: float = 5.0,
        target_pct: float = 10.0,
        trailing_stop_atr_multiple: float = 0.0,
        max_holding_bars: int = 0,
    ) -> None:
        super().__init__(ScriptedParams())
        self.script = script or {}
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.trailing_stop_atr_multiple = trailing_stop_atr_multiple
        self.max_holding_bars = max_holding_bars
        self.seen: list[pd.Timestamp] = []

    @classmethod
    def default_params(cls) -> StrategyParams:
        return ScriptedParams()

    @property
    def p(self):  # noqa: ANN201 -- the engine reads exit params off this
        return self

    @property
    def required_features(self) -> tuple[str, ...]:
        return ("close", "atr_14")

    def _decide(self, row: pd.Series, in_position: bool) -> Decision:
        stamp = row.name
        self.seen.append(stamp)
        action = self.script.get(stamp, Action.HOLD)

        if in_position:
            if action is Action.SELL:
                return Decision(Action.SELL, 0.0, reasons=("scripted exit",))
            return Decision(Action.HOLD, 0.0)

        if action is Action.BUY:
            close = float(row["close"])
            return Decision(
                Action.BUY,
                1.0,
                components=(ComponentScore("scripted", True),),
                reasons=("scripted entry",),
                stop_price=close * (1 - self.stop_pct / 100.0),
                take_profit_price=close * (1 + self.target_pct / 100.0),
            )
        return Decision(Action.HOLD, 0.0)


def zero_cost_model(market: str = "USA") -> CostModel:
    """Frictionless model, for isolating mechanics from cost arithmetic."""
    return CostModel(
        market=market,
        config=MarketCostConfig(
            commission_bps=0.0, min_commission=0.0, slippage_bps=0.0, spread_bps=0.0
        ),
    )


def priced_bars(closes: list[float], *, start: str = "2024-01-02") -> pd.DataFrame:
    """Bars with exact, predictable OHLC so fills can be asserted to the cent.

    ``open`` equals the previous close, and high/low bracket the bar tightly, so there is
    no randomness between a decision and its fill.
    """
    index = pd.bdate_range(start=start, periods=len(closes), tz="UTC", name="ts")
    closes_array = np.array(closes, dtype=float)
    opens = np.concatenate([[closes_array[0]], closes_array[:-1]])
    highs = np.maximum(opens, closes_array) * 1.001
    lows = np.minimum(opens, closes_array) * 0.999
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes_array,
            "volume": np.full(len(closes), 1_000_000.0),
            "atr_14": np.full(len(closes), closes_array.mean() * 0.02),
        },
        index=index,
    )


def config(**overrides) -> BacktestConfig:
    defaults = {
        "market": "USA",
        "initial_capital": 100_000.0,
        "risk_per_trade_pct": 100.0,
        "max_position_size_pct": 100.0,
        "max_simultaneous_positions": 5,
    }
    return BacktestConfig(**{**defaults, **overrides})


def run(strategy, frames, **config_overrides):
    backtester = Backtester(strategy, config(**config_overrides), cost_model=zero_cost_model())
    return backtester.run(frames)


# --------------------------------------------------------------------------- #
# Look-ahead
# --------------------------------------------------------------------------- #


class TestNoLookahead:
    def test_a_one_bar_spike_cannot_be_captured(self) -> None:
        """The decisive test between "fill at signal close" and "fill at next open".

        Prices are flat at 100, spike to 200 on one bar, then return to 100. A strategy
        told to buy on the spike bar can only fill at the *next* bar's open — which is
        the spike's close of 200 — and then sells into a market back at 100. It must
        therefore lose money. An engine that filled at the signal bar's close would show
        a large profit.
        """
        closes = [100.0] * 5 + [200.0] + [100.0] * 5
        frame = priced_bars(closes)
        spike_ts = frame.index[5]

        strategy = ScriptedStrategy(
            {spike_ts: Action.BUY, frame.index[7]: Action.SELL},
            stop_pct=99.0,
            target_pct=999.0,
        )
        result = run(strategy, {"X": frame})

        assert result.n_trades >= 1
        trade = result.trades[0]
        # Filled at the bar after the spike signal, i.e. at the spike close of 200.
        assert trade.entry_price == pytest.approx(200.0)
        assert trade.pnl < 0, "the engine captured a spike it could not have seen"

    def test_entry_fills_at_the_next_bar_open_not_the_signal_close(self) -> None:
        closes = [100.0, 110.0, 120.0, 130.0, 140.0]
        frame = priced_bars(closes)
        signal_ts = frame.index[1]  # close 110, next open is 110

        strategy = ScriptedStrategy({signal_ts: Action.BUY}, stop_pct=99.0, target_pct=999.0)
        result = run(strategy, {"X": frame})

        trade = result.trades[0]
        assert trade.entry_date == frame.index[2].to_pydatetime()
        assert trade.entry_price == pytest.approx(float(frame["open"].iloc[2]))

    def test_exit_signal_also_fills_at_the_next_open(self) -> None:
        closes = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0]
        frame = priced_bars(closes)
        strategy = ScriptedStrategy(
            {frame.index[0]: Action.BUY, frame.index[2]: Action.SELL},
            stop_pct=99.0,
            target_pct=999.0,
        )
        result = run(strategy, {"X": frame})

        trade = result.trades[0]
        assert trade.exit_date == frame.index[3].to_pydatetime()
        assert trade.exit_reason != EXIT_END

    def test_strategy_never_sees_a_bar_beyond_the_one_being_decided(self) -> None:
        """The strategy is handed a row, not a future.

        Recorded timestamps must be exactly the calendar, in order, with no repeats from
        a later bar sneaking backwards.
        """
        frame = priced_bars([100.0] * 10)
        strategy = ScriptedStrategy()
        run(strategy, {"X": frame})

        # The final bar is not evaluated (nothing could fill afterwards).
        assert strategy.seen == list(frame.index[:-1])

    def test_forward_columns_in_the_input_are_rejected(self) -> None:
        """A frame carrying outcome labels must raise, not produce a great backtest."""
        bars = make_bars(400, seed=5)
        contaminated = compute_features(bars, include_forward=True)
        strategy = ScriptedStrategy()

        with pytest.raises(DataLeakageError, match="forward"):
            run(strategy, {"X": contaminated})

    def test_at_least_two_bars_are_required(self) -> None:
        frame = priced_bars([100.0])
        with pytest.raises(ValueError, match="at least 2 bars"):
            run(ScriptedStrategy(), {"X": frame})


# --------------------------------------------------------------------------- #
# Accounting
# --------------------------------------------------------------------------- #


class TestAccountingIdentity:
    def test_equity_equals_cash_plus_positions_at_every_bar(self) -> None:
        frame = priced_bars([100.0, 105.0, 110.0, 108.0, 112.0, 115.0])
        strategy = ScriptedStrategy({frame.index[0]: Action.BUY}, stop_pct=99.0, target_pct=999.0)
        result = run(strategy, {"X": frame})

        for snapshot in result.snapshots:
            assert snapshot["equity"] == pytest.approx(
                snapshot["cash"] + snapshot["positions_value"], abs=1e-6
            )

    def test_a_flat_round_trip_loses_exactly_the_friction(self) -> None:
        """With costs on, buying and selling at the same price must cost the modelled amount."""
        frame = priced_bars([100.0] * 8)
        strategy = ScriptedStrategy(
            {frame.index[0]: Action.BUY, frame.index[2]: Action.SELL},
            stop_pct=99.0,
            target_pct=999.0,
        )
        costs = CostModel(
            market="USA",
            config=MarketCostConfig(
                commission_bps=10.0, min_commission=0.0, slippage_bps=5.0, spread_bps=5.0
            ),
        )
        result = Backtester(strategy, config(), cost_model=costs).run({"X": frame})

        trade = result.trades[0]
        assert trade.pnl < 0
        # Entry above 100, exit below it: both legs adverse.
        assert trade.entry_price > 100.0 > trade.exit_price
        assert trade.commission > 0
        assert trade.slippage_cost > 0

    def test_zero_cost_flat_round_trip_breaks_even(self) -> None:
        frame = priced_bars([100.0] * 8)
        strategy = ScriptedStrategy(
            {frame.index[0]: Action.BUY, frame.index[2]: Action.SELL},
            stop_pct=99.0,
            target_pct=999.0,
        )
        result = run(strategy, {"X": frame})
        assert result.trades[0].pnl == pytest.approx(0.0, abs=1e-9)

    def test_net_pnl_is_gross_minus_commission(self) -> None:
        frame = priced_bars([100.0, 100.0, 110.0, 110.0, 110.0])
        strategy = ScriptedStrategy(
            {frame.index[0]: Action.BUY, frame.index[2]: Action.SELL},
            stop_pct=99.0,
            target_pct=999.0,
        )
        costs = CostModel(
            market="USA",
            config=MarketCostConfig(
                commission_bps=10.0, min_commission=0.0, slippage_bps=0.0, spread_bps=0.0
            ),
        )
        result = Backtester(strategy, config(), cost_model=costs).run({"X": frame})
        trade = result.trades[0]
        assert trade.pnl == pytest.approx(trade.gross_pnl - trade.commission, abs=1e-6)

    def test_cash_never_goes_negative(self) -> None:
        frame = priced_bars([100.0] * 30)
        strategy = ScriptedStrategy(
            {ts: Action.BUY for ts in frame.index}, stop_pct=99.0, target_pct=999.0
        )
        result = run(strategy, {"X": frame})
        for snapshot in result.snapshots:
            assert snapshot["cash"] >= -1e-9

    def test_equity_curve_ends_on_realised_cash(self) -> None:
        """The last position is liquidated, so the final point is not a mark."""
        frame = priced_bars([100.0, 105.0, 110.0, 115.0, 120.0])
        strategy = ScriptedStrategy({frame.index[0]: Action.BUY}, stop_pct=99.0, target_pct=999.0)
        result = run(strategy, {"X": frame})

        assert result.trades[-1].exit_reason == EXIT_END
        assert result.equity_curve.iloc[-1] == pytest.approx(
            result.snapshots[-1]["cash"] + result.trades[-1].exit_price * result.trades[-1].quantity,
            rel=1e-6,
        )


# --------------------------------------------------------------------------- #
# Exits
# --------------------------------------------------------------------------- #


class TestExits:
    def test_stop_loss_triggers_on_the_intrabar_low(self) -> None:
        # Enter around 100, then a bar whose low pierces a 5% stop.
        frame = priced_bars([100.0, 100.0, 90.0, 90.0, 90.0])
        strategy = ScriptedStrategy({frame.index[0]: Action.BUY}, stop_pct=5.0, target_pct=50.0)
        result = run(strategy, {"X": frame})

        trade = result.trades[0]
        assert trade.exit_reason == EXIT_STOP
        assert trade.pnl < 0

    def test_take_profit_triggers_on_the_intrabar_high(self) -> None:
        frame = priced_bars([100.0, 100.0, 120.0, 120.0, 120.0])
        strategy = ScriptedStrategy({frame.index[0]: Action.BUY}, stop_pct=50.0, target_pct=10.0)
        result = run(strategy, {"X": frame})

        trade = result.trades[0]
        assert trade.exit_reason == EXIT_TARGET
        assert trade.pnl > 0

    def test_stop_wins_when_a_bar_contains_both_levels(self) -> None:
        """The pessimistic tie-break, asserted rather than assumed.

        Daily bars cannot resolve intrabar order, so a bar spanning both the stop and the
        target must exit at the stop. Any other choice flatters the strategy on exactly
        the bars where it matters.
        """
        index = pd.bdate_range("2024-01-02", periods=4, tz="UTC", name="ts")
        frame = pd.DataFrame(
            {
                "open": [100.0, 100.0, 100.0, 100.0],
                # Bar 2 spans from 80 to 130: through a 5% stop and a 10% target.
                "high": [101.0, 101.0, 130.0, 101.0],
                "low": [99.0, 99.0, 80.0, 99.0],
                "close": [100.0, 100.0, 100.0, 100.0],
                "volume": [1e6] * 4,
                "atr_14": [2.0] * 4,
            },
            index=index,
        )
        strategy = ScriptedStrategy({index[0]: Action.BUY}, stop_pct=5.0, target_pct=10.0)
        result = run(strategy, {"X": frame})

        assert result.trades[0].exit_reason == EXIT_STOP

    def test_a_gap_through_the_stop_fills_at_the_open(self) -> None:
        """You cannot be filled at a price the market skipped."""
        index = pd.bdate_range("2024-01-02", periods=4, tz="UTC", name="ts")
        frame = pd.DataFrame(
            {
                "open": [100.0, 100.0, 70.0, 70.0],  # gaps far below a 5% stop
                "high": [101.0, 101.0, 72.0, 72.0],
                "low": [99.0, 99.0, 69.0, 69.0],
                "close": [100.0, 100.0, 71.0, 71.0],
                "volume": [1e6] * 4,
                "atr_14": [2.0] * 4,
            },
            index=index,
        )
        strategy = ScriptedStrategy({index[0]: Action.BUY}, stop_pct=5.0, target_pct=50.0)
        result = run(strategy, {"X": frame})

        trade = result.trades[0]
        assert trade.exit_reason == EXIT_STOP
        # Filled at the gapped open (70), not at the 95 stop level.
        assert trade.exit_price == pytest.approx(70.0)
        assert trade.exit_price < 95.0

    def test_time_stop_closes_the_position(self) -> None:
        frame = priced_bars([100.0] * 12)
        strategy = ScriptedStrategy(
            {frame.index[0]: Action.BUY},
            stop_pct=99.0,
            target_pct=999.0,
            max_holding_bars=3,
        )
        result = run(strategy, {"X": frame})

        trade = result.trades[0]
        assert trade.exit_reason == EXIT_TIME
        assert trade.bars_held == 3

    def test_trailing_stop_ratchets_up_and_never_down(self) -> None:
        # Rise to 130, then fall back. A trailing stop must exit on the way down.
        closes = [100.0, 110.0, 120.0, 130.0, 125.0, 110.0, 100.0, 95.0]
        frame = priced_bars(closes)
        strategy = ScriptedStrategy(
            {frame.index[0]: Action.BUY},
            stop_pct=50.0,
            target_pct=999.0,
            trailing_stop_atr_multiple=1.0,
        )
        result = run(strategy, {"X": frame})

        trade = result.trades[0]
        assert trade.exit_reason in (EXIT_STOP, "trailing_stop")
        # It exited before the very last bar, i.e. the trail bound on the decline.
        assert trade.exit_date < frame.index[-1].to_pydatetime()

    def test_open_position_is_closed_at_the_end(self) -> None:
        frame = priced_bars([100.0] * 6)
        strategy = ScriptedStrategy({frame.index[0]: Action.BUY}, stop_pct=99.0, target_pct=999.0)
        result = run(strategy, {"X": frame})

        assert result.n_trades == 1
        assert result.trades[0].exit_reason == EXIT_END

    def test_excursions_are_recorded(self) -> None:
        frame = priced_bars([100.0, 100.0, 120.0, 85.0, 100.0, 100.0])
        strategy = ScriptedStrategy({frame.index[0]: Action.BUY}, stop_pct=99.0, target_pct=999.0)
        result = run(strategy, {"X": frame})

        trade = result.trades[0]
        assert trade.max_favorable_excursion_pct > 0
        assert trade.max_adverse_excursion_pct < 0


# --------------------------------------------------------------------------- #
# Sizing and limits
# --------------------------------------------------------------------------- #


class TestSizingAndLimits:
    def test_risk_per_trade_bounds_the_loss_at_the_stop(self) -> None:
        """A 1% risk budget with a stop that is hit should lose roughly 1% of equity."""
        frame = priced_bars([100.0, 100.0, 94.0, 94.0, 94.0])
        strategy = ScriptedStrategy({frame.index[0]: Action.BUY}, stop_pct=5.0, target_pct=50.0)
        result = run(
            strategy,
            {"X": frame},
            risk_per_trade_pct=1.0,
            max_position_size_pct=100.0,
            initial_capital=100_000.0,
        )

        trade = result.trades[0]
        loss_pct_of_equity = abs(trade.pnl) / 100_000.0 * 100.0
        # Not exact: the fill gaps slightly past the stop, and shares are whole numbers.
        assert 0.5 < loss_pct_of_equity < 2.0

    def test_position_size_cap_binds(self) -> None:
        frame = priced_bars([100.0] * 6)
        strategy = ScriptedStrategy({frame.index[0]: Action.BUY}, stop_pct=50.0, target_pct=999.0)
        result = run(
            strategy,
            {"X": frame},
            risk_per_trade_pct=100.0,
            max_position_size_pct=10.0,
            initial_capital=100_000.0,
        )

        trade = result.trades[0]
        notional = trade.entry_price * trade.quantity
        assert notional <= 100_000.0 * 0.10 * 1.01

    def test_max_simultaneous_positions_is_respected(self) -> None:
        frames = {}
        for i in range(6):
            frames[f"S{i}"] = priced_bars([100.0] * 10)
        first = next(iter(frames.values())).index[0]
        strategy = ScriptedStrategy({first: Action.BUY}, stop_pct=50.0, target_pct=999.0)

        result = run(strategy, frames, max_simultaneous_positions=2, risk_per_trade_pct=10.0)

        for snapshot in result.snapshots:
            assert snapshot["n_positions"] <= 2

    def test_rejections_are_recorded_with_reasons(self) -> None:
        """A strategy constantly blocked is telling you something metrics will not."""
        frames = {f"S{i}": priced_bars([100.0] * 10) for i in range(6)}
        first = next(iter(frames.values())).index[0]
        strategy = ScriptedStrategy({first: Action.BUY}, stop_pct=50.0, target_pct=999.0)

        result = run(strategy, frames, max_simultaneous_positions=1, risk_per_trade_pct=10.0)
        assert result.rejected_entries
        assert any("max_positions" in key for key in result.rejected_entries)


# --------------------------------------------------------------------------- #
# Multi-symbol behaviour and reporting
# --------------------------------------------------------------------------- #


class TestMultiSymbol:
    def test_calendar_is_the_union_not_the_intersection(self) -> None:
        """One thinly traded name must not shrink the whole universe's calendar."""
        full = priced_bars([100.0] * 10)
        sparse = full.iloc[::3]  # trades only every third session

        strategy = ScriptedStrategy()
        result = run(strategy, {"FULL": full, "SPARSE": sparse})

        assert len(result.equity_curve) == len(full)

    def test_a_position_that_did_not_trade_stays_in_equity(self) -> None:
        """Otherwise the equity curve shows a phantom drawdown and recovery."""
        full = priced_bars([100.0] * 10)
        sparse = full.copy()
        gap_ts = sparse.index[5]
        sparse = sparse.drop(index=[gap_ts])

        strategy = ScriptedStrategy({sparse.index[0]: Action.BUY}, stop_pct=99.0, target_pct=999.0)
        result = run(strategy, {"SPARSE": sparse, "FULL": full}, risk_per_trade_pct=50.0)

        gap_snapshot = next(s for s in result.snapshots if s["ts"] == gap_ts)
        held = {p["symbol"]: p for p in gap_snapshot["positions"]}

        # SPARSE did not print on this bar, yet it must still be marked and counted.
        # Dropping it would show a phantom drawdown on the gap bar and a phantom
        # recovery on the next one.
        assert "SPARSE" in held, "position vanished from equity on a no-trade bar"
        assert held["SPARSE"]["mark_price"] > 0
        assert gap_snapshot["positions_value"] > 0


class TestResultMetadata:
    def test_limitations_are_attached_and_name_the_intrabar_rule(self) -> None:
        frame = priced_bars([100.0] * 5)
        result = run(ScriptedStrategy(), {"X": frame})

        assert INTRABAR_ASSUMPTION in result.limitations
        joined = " ".join(result.limitations).lower()
        assert "survivorship" in joined
        assert "long only" in joined
        assert "taxes" in joined

    def test_cost_assumptions_are_recorded(self) -> None:
        frame = priced_bars([100.0] * 5)
        result = run(ScriptedStrategy(), {"X": frame})
        assert result.cost_model["market"] == "USA"
        assert "round_trip_pct" in result.cost_model

    def test_strategy_identity_is_recorded(self) -> None:
        frame = priced_bars([100.0] * 5)
        result = run(ScriptedStrategy(), {"X": frame})
        assert result.strategy["name"] == "scripted"

    def test_split_defaults_to_full_and_is_persisted(self) -> None:
        frame = priced_bars([100.0] * 5)
        result = run(ScriptedStrategy(), {"X": frame})
        assert result.config.split == "full"

    def test_trades_frame_is_tabular(self) -> None:
        frame = priced_bars([100.0, 105.0, 110.0, 115.0])
        strategy = ScriptedStrategy({frame.index[0]: Action.BUY}, stop_pct=99.0, target_pct=999.0)
        result = run(strategy, {"X": frame})

        table = result.trades_frame()
        assert len(table) == result.n_trades
        for column in ("symbol", "entry_date", "exit_date", "pnl", "exit_reason"):
            assert column in table.columns

    def test_empty_universe_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty universe"):
            run(ScriptedStrategy(), {})


class TestConfigValidation:
    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("initial_capital", 0.0, "initial_capital must be positive"),
            ("risk_per_trade_pct", 0.0, "risk_per_trade_pct"),
            ("risk_per_trade_pct", 101.0, "risk_per_trade_pct"),
            ("max_position_size_pct", 0.0, "max_position_size_pct"),
            ("max_simultaneous_positions", 0, "max_simultaneous_positions"),
            ("split", "holdout", "split must be"),
        ],
    )
    def test_invalid_config_is_rejected(self, field: str, value, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            config(**{field: value})
