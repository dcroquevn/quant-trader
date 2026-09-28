"""Momentum indicators: RSI, MACD, rate of change."""

from __future__ import annotations

import pandas as pd

from app.indicators.base import safe_divide, wilder_rma

__all__ = ["rsi", "macd", "roc", "momentum_score", "ROC_PERIODS"]

ROC_PERIODS: tuple[int, ...] = (5, 20, 60)


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index, in ``[0, 100]``.

    Uses :func:`~app.indicators.base.wilder_rma` for both the average gain and the
    average loss, which is what reproduces the values charting platforms show.

    When average loss is zero (an unbroken run of up closes) RSI is defined as
    100. That is the mathematical limit, not a fudge.

    Returns ``NaN`` for the first ``length`` positions.
    """
    if length < 2:
        raise ValueError(f"RSI length must be >= 2, got {length}")

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = wilder_rma(gain, length)
    avg_loss = wilder_rma(loss, length)

    rs = safe_divide(avg_gain, avg_loss)
    result = 100.0 - (100.0 / (1.0 + rs))

    # avg_loss == 0 makes rs infinite -> RSI 100. safe_divide turned it into NaN,
    # so restore the limit explicitly where the inputs say it applies.
    all_gains = (avg_loss == 0.0) & avg_gain.notna()
    result = result.mask(all_gains, 100.0)

    # The mirror case: no gains at all -> RSI 0.
    all_losses = (avg_gain == 0.0) & avg_loss.notna() & (avg_loss > 0.0)
    result = result.mask(all_losses, 0.0)

    return result


def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD line, signal line and histogram.

    Returns a frame with ``macd``, ``macd_signal`` and ``macd_hist``.

    The EMAs here are seeded from the first bar (``min_periods=1``) rather than
    masked like :func:`app.indicators.trend.ema`, because the MACD *difference*
    stabilises far faster than either EMA alone -- the shared seed bias largely
    cancels. The first ``slow`` values are masked afterwards regardless, so the
    published series still only starts where it is meaningful.
    """
    if fast >= slow:
        raise ValueError(f"MACD fast ({fast}) must be shorter than slow ({slow})")

    ema_fast = close.ewm(span=fast, adjust=False, min_periods=1).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=1).mean()
    line = ema_fast - ema_slow
    signal_line = line.ewm(span=signal, adjust=False, min_periods=1).mean()

    warmup = slow + signal
    mask = pd.Series(range(len(close)), index=close.index) >= (warmup - 1)

    return pd.DataFrame(
        {
            "macd": line.where(mask),
            "macd_signal": signal_line.where(mask),
            "macd_hist": (line - signal_line).where(mask),
        }
    )


def roc(close: pd.Series, length: int) -> pd.Series:
    """Rate of change over ``length`` bars, as a percentage.

    ``pct_change`` looks strictly backwards -- ``close[i] / close[i - length]`` --
    so this is safe. The forward-looking mirror image lives in
    :mod:`app.indicators.forward` and is never a strategy input.
    """
    if length < 1:
        raise ValueError(f"ROC length must be >= 1, got {length}")
    # fill_method=None: never pad a gap before differencing. Padding would report a
    # 0% change across a missing session instead of an honest NaN.
    return close.pct_change(periods=length, fill_method=None) * 100.0


def momentum_score(frame: pd.DataFrame, rsi_low: float = 40.0, rsi_high: float = 70.0) -> pd.Series:
    """Composite momentum reading in ``[0, 1]`` from three conditions.

    The conditions: RSI inside ``(rsi_low, rsi_high)``, MACD histogram positive,
    and 20-bar ROC positive.

    The RSI condition is a *band*, not "RSI is low". Buying the lowest RSI
    available is a mean-reversion bet, and combining it with trend-following rules
    produces a strategy at war with itself. The band asks for momentum that is
    positive but not yet stretched.

    As with :func:`app.indicators.trend.trend_score`, this is an ordinal ranking
    number and not a probability.
    """
    required = {"rsi_14", "macd_hist", "roc_20"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"momentum_score needs columns {sorted(missing)}")

    conditions = [
        frame["rsi_14"].between(rsi_low, rsi_high, inclusive="neither").where(
            frame["rsi_14"].notna(), other=False
        ),
        (frame["macd_hist"] > 0).where(frame["macd_hist"].notna(), other=False),
        (frame["roc_20"] > 0).where(frame["roc_20"].notna(), other=False),
    ]
    total = sum(c.astype(float) for c in conditions)
    score = total / len(conditions)
    return score.where(frame["rsi_14"].notna() & frame["macd_hist"].notna())
