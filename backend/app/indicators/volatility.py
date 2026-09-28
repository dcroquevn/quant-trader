"""Volatility indicators: ATR, rolling volatility, Bollinger Bands."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators.base import safe_divide, wilder_rma

__all__ = [
    "true_range",
    "atr",
    "atr_pct",
    "rolling_volatility",
    "bollinger_bands",
    "bollinger_percent_b",
]


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Wilder's True Range: the largest of three measures of the bar's span.

    ``max(high - low, |high - prev_close|, |low - prev_close|)``

    The previous close is included so an overnight gap counts as movement. Using
    ``high - low`` alone understates risk precisely on the days that matter most.
    The first value is ``NaN`` -- there is no previous close.
    """
    prev_close = close.shift(1)
    spans = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    result = spans.max(axis=1)
    return result.where(prev_close.notna())


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Average True Range, smoothed the way Wilder defined it.

    This is the position-sizing input: a stop placed at ``N * ATR`` below entry
    adapts to each instrument's own volatility, which matters enormously here --
    a Chilean bank and NVDA do not deserve the same percentage stop.
    """
    return wilder_rma(true_range(high, low, close), length)


def atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """ATR as a percentage of price -- comparable across markets and currencies.

    Raw ATR is in the instrument's own currency, so an ATR of 500 is alarming for
    a USD stock and unremarkable for a CLP one quoted in the tens of thousands.
    Any cross-market comparison must use this, not :func:`atr`.
    """
    return safe_divide(atr(high, low, close, length), close) * 100.0


def rolling_volatility(
    close: pd.Series,
    length: int = 20,
    *,
    annualize: bool = True,
    periods_per_year: float = 252.0,
) -> pd.Series:
    """Standard deviation of log returns over a trailing window, in percent.

    Log returns are used rather than simple returns because they are additive
    across time, which is what makes the ``sqrt(periods_per_year)`` annualisation
    scaling valid in the first place.

    ``ddof=1`` (sample standard deviation) matches every reference implementation.
    """
    if length < 2:
        raise ValueError(f"volatility window must be >= 2, got {length}")

    log_returns = np.log(close / close.shift(1))
    std = log_returns.rolling(window=length, min_periods=length).std(ddof=1)
    if annualize:
        std = std * np.sqrt(periods_per_year)
    return std * 100.0


def bollinger_bands(
    close: pd.Series, length: int = 20, n_std: float = 2.0
) -> pd.DataFrame:
    """Bollinger Bands around a simple moving average.

    Returns ``bb_mid``, ``bb_upper``, ``bb_lower`` and ``bb_width`` (the band span
    as a percentage of the midline -- the squeeze/expansion measure).
    """
    if length < 2:
        raise ValueError(f"Bollinger window must be >= 2, got {length}")

    mid = close.rolling(window=length, min_periods=length).mean()
    std = close.rolling(window=length, min_periods=length).std(ddof=1)
    upper = mid + n_std * std
    lower = mid - n_std * std

    return pd.DataFrame(
        {
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_width": safe_divide(upper - lower, mid) * 100.0,
        }
    )


def bollinger_percent_b(
    close: pd.Series, length: int = 20, n_std: float = 2.0
) -> pd.Series:
    """Where price sits within its bands: 0 at the lower band, 1 at the upper.

    Values outside ``[0, 1]`` are meaningful -- price has broken through a band.
    Returns ``NaN`` when the bands have zero width (a perfectly flat window),
    which happens on illiquid Chilean names with repeated closes.
    """
    bands = bollinger_bands(close, length, n_std)
    return safe_divide(close - bands["bb_lower"], bands["bb_upper"] - bands["bb_lower"])
