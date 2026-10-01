"""A trend-following strategy with momentum, volume and volatility filters.

This is a *starting point for measurement*, not a recommendation. Nothing about it is
known to be profitable, and it was written to be measurable rather than to be good.
The point of Phase 2 is to find out how it behaves once costs are charged.

The shape of it
---------------
Four components, all of which must agree before entering:

**Trend** — price above EMA200 and EMA50 above EMA200. The regime filter: it keeps the
strategy from buying dips in a downtrend, which is where trend-following systems go to
die.

**Momentum** — RSI inside a *band*, MACD histogram positive, ROC positive. The band
matters: buying the lowest available RSI is a mean-reversion bet, and bolting that onto
trend-following rules produces a strategy at war with itself. The band asks for
momentum that is positive but not yet stretched.

**Volume** — relative volume above a threshold, so a breakout has participation behind
it. Also the liquidity gate: a minimum average traded value, expressed per market
because 100,000 shares means something entirely different in CLP than in USD.

**Volatility** — ATR as a percentage of price must sit inside a band. Too quiet and
there is no move to capture after costs; too wild and the stop distance makes the
position size trivially small.

Exits are deliberately mechanical: stop loss at a multiple of ATR, take profit at a
multiple of the stop distance, optional trailing stop, and a maximum holding period.
The strategy suggests them; the backtester enforces them, because only it can see
intrabar highs and lows.

Why every threshold is a parameter
----------------------------------
Every number below is configurable, because Phase 4 will optimise them separately per
market — and the hypothesis that US and Chilean equities want the same parameters is
one the optimiser should be allowed to reject.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.strategies.base import (
    Action,
    ComponentScore,
    Decision,
    Strategy,
    StrategyParams,
)

__all__ = ["TrendMomentumParams", "TrendMomentumStrategy"]


@dataclass(frozen=True, slots=True)
class TrendMomentumParams(StrategyParams):
    """Parameters for :class:`TrendMomentumStrategy`.

    Defaults are plausible starting values, not tuned ones. Treating them as tuned
    would be exactly the overfitting this project is built to avoid.
    """

    # ---- Trend ----
    require_price_above_ema200: bool = True
    require_ema50_above_ema200: bool = True
    min_ema50_slope_pct: float = 0.0
    """Minimum 5-bar EMA50 change, in percent. 0.0 means "not falling"."""

    # ---- Momentum ----
    rsi_min: float = 40.0
    rsi_max: float = 70.0
    """Upper bound on RSI. Above this, the move is already extended."""

    require_macd_positive: bool = True
    roc_period: int = 20
    min_roc_pct: float = 0.0

    # ---- Volume ----
    min_relative_volume: float = 1.2
    min_dollar_volume: float = 0.0
    """Minimum 20-bar average traded value, in the instrument's own currency.

    Zero disables the check. A cross-market threshold has to be set per market — the
    same number cannot be meaningful in both USD and CLP.
    """

    # ---- Volatility ----
    min_atr_pct: float = 0.5
    max_atr_pct: float = 8.0

    # ---- Exits ----
    stop_atr_multiple: float = 2.0
    """Initial stop distance, in ATR units below entry."""

    take_profit_r_multiple: float = 3.0
    """Target distance as a multiple of the stop distance. 3.0 means 3R."""

    trailing_stop_atr_multiple: float = 0.0
    """Trailing stop distance in ATR units. 0.0 disables trailing."""

    max_holding_bars: int = 60
    """Force an exit after this many bars. 0 disables the time stop."""

    # ---- Exit signal ----
    exit_on_trend_break: bool = True
    """Exit when price closes back below EMA50."""

    exit_rsi_max: float = 80.0
    """Exit when RSI exceeds this, as a mean-reversion guard on an extended move."""

    def __post_init__(self) -> None:
        if self.rsi_min >= self.rsi_max:
            raise ValueError(
                f"rsi_min ({self.rsi_min}) must be below rsi_max ({self.rsi_max})"
            )
        if self.min_atr_pct >= self.max_atr_pct:
            raise ValueError(
                f"min_atr_pct ({self.min_atr_pct}) must be below "
                f"max_atr_pct ({self.max_atr_pct})"
            )
        if self.stop_atr_multiple <= 0:
            raise ValueError(
                f"stop_atr_multiple must be positive, got {self.stop_atr_multiple}. "
                "A strategy with no stop has unbounded loss per trade."
            )
        if self.roc_period not in (5, 20, 60):
            raise ValueError(
                f"roc_period must be one of the computed periods (5, 20, 60), got "
                f"{self.roc_period}. Add it to app.indicators.momentum.ROC_PERIODS first."
            )


class TrendMomentumStrategy(Strategy):
    """Long-only trend following with momentum, volume and volatility gates."""

    name = "trend_momentum"
    version = "1"
    description = (
        "Long-only. Enters when trend, momentum, volume and volatility conditions all "
        "hold. Exits on stop, target, trailing stop, trend break, RSI extension or "
        "time. Not known to be profitable."
    )

    @classmethod
    def default_params(cls) -> TrendMomentumParams:
        return TrendMomentumParams()

    @property
    def p(self) -> TrendMomentumParams:
        """Typed accessor for the parameter set."""
        assert isinstance(self.params, TrendMomentumParams)
        return self.params

    @property
    def required_features(self) -> tuple[str, ...]:
        return (
            "close",
            "ema_50",
            "ema_200",
            "rsi_14",
            "macd_hist",
            f"roc_{self.p.roc_period}",
            "relative_volume_20",
            "dollar_volume_20",
            "atr_14",
            "atr_pct_14",
        )

    # ------------------------------------------------------------------ #
    # Entry
    # ------------------------------------------------------------------ #

    def _entry_components(self, row: pd.Series) -> list[ComponentScore]:
        p = self.p
        close = float(row["close"])
        components: list[ComponentScore] = []

        # ---- Trend ----
        if p.require_price_above_ema200:
            ema200 = float(row["ema_200"])
            passed = close > ema200
            components.append(
                ComponentScore(
                    "trend_price_above_ema200",
                    passed,
                    value=close / ema200 - 1.0,
                    detail=(
                        f"price {close:,.2f} {'above' if passed else 'below'} "
                        f"EMA200 {ema200:,.2f}"
                    ),
                )
            )

        if p.require_ema50_above_ema200:
            ema50, ema200 = float(row["ema_50"]), float(row["ema_200"])
            passed = ema50 > ema200
            components.append(
                ComponentScore(
                    "trend_ema50_above_ema200",
                    passed,
                    value=ema50 / ema200 - 1.0,
                    detail=(
                        f"EMA50 {ema50:,.2f} {'above' if passed else 'below'} "
                        f"EMA200 {ema200:,.2f}"
                    ),
                )
            )

        # ---- Momentum ----
        rsi = float(row["rsi_14"])
        rsi_ok = p.rsi_min < rsi < p.rsi_max
        components.append(
            ComponentScore(
                "momentum_rsi_band",
                rsi_ok,
                value=rsi,
                detail=(
                    f"RSI {rsi:.1f} {'inside' if rsi_ok else 'outside'} band "
                    f"({p.rsi_min:.0f}, {p.rsi_max:.0f})"
                ),
            )
        )

        if p.require_macd_positive:
            hist = float(row["macd_hist"])
            components.append(
                ComponentScore(
                    "momentum_macd_positive",
                    hist > 0,
                    value=hist,
                    detail=f"MACD histogram {hist:+.4f}",
                )
            )

        roc = float(row[f"roc_{p.roc_period}"])
        components.append(
            ComponentScore(
                "momentum_roc",
                roc > p.min_roc_pct,
                value=roc,
                detail=f"{p.roc_period}-bar ROC {roc:+.2f}% vs min {p.min_roc_pct:+.2f}%",
            )
        )

        # ---- Volume ----
        rvol = float(row["relative_volume_20"])
        components.append(
            ComponentScore(
                "volume_relative",
                rvol >= p.min_relative_volume,
                value=rvol,
                detail=f"relative volume {rvol:.2f}x vs min {p.min_relative_volume:.2f}x",
            )
        )

        if p.min_dollar_volume > 0:
            turnover = float(row["dollar_volume_20"])
            components.append(
                ComponentScore(
                    "volume_liquidity",
                    turnover >= p.min_dollar_volume,
                    value=turnover,
                    detail=(
                        f"20-bar average turnover {turnover:,.0f} vs min "
                        f"{p.min_dollar_volume:,.0f}"
                    ),
                )
            )

        # ---- Volatility ----
        atr_pct = float(row["atr_pct_14"])
        vol_ok = p.min_atr_pct <= atr_pct <= p.max_atr_pct
        components.append(
            ComponentScore(
                "volatility_band",
                vol_ok,
                value=atr_pct,
                detail=(
                    f"ATR {atr_pct:.2f}% of price, "
                    f"{'inside' if vol_ok else 'outside'} band "
                    f"({p.min_atr_pct:.2f}%, {p.max_atr_pct:.2f}%)"
                ),
            )
        )

        return components

    # ------------------------------------------------------------------ #
    # Exit
    # ------------------------------------------------------------------ #

    def _exit_components(self, row: pd.Series) -> list[ComponentScore]:
        """Signal-based exits only. Price-based stops belong to the backtester.

        The split matters: a stop is checked against the *intrabar* low, which the
        strategy never sees. Duplicating that logic here would produce two slightly
        different answers about the same trade.
        """
        p = self.p
        close = float(row["close"])
        components: list[ComponentScore] = []

        if p.exit_on_trend_break:
            ema50 = float(row["ema_50"])
            broke = close < ema50
            components.append(
                ComponentScore(
                    "exit_trend_break",
                    broke,
                    value=close / ema50 - 1.0,
                    detail=(
                        f"price {close:,.2f} closed "
                        f"{'below' if broke else 'above'} EMA50 {ema50:,.2f}"
                    ),
                )
            )

        rsi = float(row["rsi_14"])
        extended = rsi > p.exit_rsi_max
        components.append(
            ComponentScore(
                "exit_rsi_extended",
                extended,
                value=rsi,
                detail=f"RSI {rsi:.1f} vs exit threshold {p.exit_rsi_max:.0f}",
            )
        )

        return components

    # ------------------------------------------------------------------ #
    # Decision
    # ------------------------------------------------------------------ #

    def propose_levels(self, frame: "pd.DataFrame") -> "tuple[float, float] | None":
        """The same ``close - k x ATR`` stop and ``R``-multiple target, without needing a BUY.

        Uses the last bar with a usable ATR. A position recorded a day or two after the signal
        still gets the levels the backtest would have used, instead of none at all.
        """
        if frame.empty or "atr_14" not in frame.columns or "close" not in frame.columns:
            return None

        usable = frame[frame["atr_14"].notna() & frame["close"].notna()]
        if usable.empty:
            return None

        row = usable.iloc[-1]
        close, atr = float(row["close"]), float(row["atr_14"])
        if close <= 0 or atr <= 0:
            return None

        stop = close - self.p.stop_atr_multiple * atr
        # An ATR exceeding the price means corrupt data, not a wide stop. The entry path
        # refuses to size against it and so does this one.
        if stop <= 0:
            return None

        target = close + self.p.take_profit_r_multiple * (close - stop)
        return stop, target

    def _decide(self, row: pd.Series, in_position: bool) -> Decision:
        features = {
            key: (None if pd.isna(row[key]) else float(row[key]))
            for key in self.required_features
        }

        if in_position:
            components = self._exit_components(row)
            fired = [c for c in components if c.passed]
            if fired:
                return Decision(
                    action=Action.SELL,
                    # An exit is not ranked against other exits, so the score carries
                    # no information here. Reporting 1.0 would invite a reader to
                    # compare it with an entry score, which measures something else.
                    score=0.0,
                    components=tuple(components),
                    reasons=tuple(c.detail for c in fired),
                    features=features,
                )
            return Decision(
                action=Action.HOLD,
                score=0.0,
                components=tuple(components),
                reasons=("no exit condition met",),
                features=features,
            )

        components = self._entry_components(row)
        passed = sum(1 for c in components if c.passed)
        score = passed / len(components) if components else 0.0
        all_passed = passed == len(components)

        if not all_passed:
            failing = [c.detail for c in components if not c.passed]
            return Decision(
                action=Action.HOLD,
                score=score,
                components=tuple(components),
                reasons=tuple(failing),
                features=features,
            )

        close = float(row["close"])
        atr = float(row["atr_14"])
        stop = close - self.p.stop_atr_multiple * atr
        # A stop at or below zero means ATR exceeds price, which happens only on
        # corrupt data. Refuse rather than sizing a position against it.
        if stop <= 0:
            return Decision(
                action=Action.HOLD,
                score=score,
                components=tuple(components),
                reasons=(
                    f"computed stop {stop:,.2f} is not positive "
                    f"(close {close:,.2f}, ATR {atr:,.2f}); refusing to size a trade",
                ),
                features=features,
            )

        risk_per_share = close - stop
        target = close + self.p.take_profit_r_multiple * risk_per_share

        return Decision(
            action=Action.BUY,
            score=score,
            components=tuple(components),
            reasons=tuple(c.detail for c in components),
            features=features,
            stop_price=stop,
            take_profit_price=target,
        )
