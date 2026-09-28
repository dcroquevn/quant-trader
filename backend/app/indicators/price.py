"""Price-derived features: trailing returns and distance from reference levels."""

from __future__ import annotations

import pandas as pd

from app.indicators.base import pct_distance, safe_divide

__all__ = [
    "trailing_return",
    "rolling_high",
    "rolling_low",
    "distance_from_high",
    "distance_from_low",
    "position_in_range",
    "RETURN_PERIODS",
    "WEEKS_52_IN_BARS",
]

RETURN_PERIODS: tuple[int, ...] = (1, 5, 20, 60)

WEEKS_52_IN_BARS = 252
"""Bars in a 52-week window, on the US convention of 252 trading days.

Chile's calendar has a different holiday count, so a 252-bar window there spans
slightly more or less than a literal year. The difference is immaterial for a
"distance from the 52-week high" feature and is preferable to maintaining a
holiday calendar this project has no free, reliable source for.
"""


def trailing_return(close: pd.Series, periods: int) -> pd.Series:
    """Return over the trailing ``periods`` bars, as a percentage.

    Strictly backward-looking: ``close[i] / close[i - periods] - 1``.
    """
    if periods < 1:
        raise ValueError(f"periods must be >= 1, got {periods}")
    # fill_method=None: a missing session must stay NaN, not become a 0% return.
    return close.pct_change(periods=periods, fill_method=None) * 100.0


def rolling_high(high: pd.Series, length: int = WEEKS_52_IN_BARS) -> pd.Series:
    """Highest high over a trailing window, **including the current bar**.

    Including the current bar is correct, not a leak: at the close of bar ``i`` its
    own high is known. This is what makes "price is at a 52-week high" expressible
    at the moment it happens.
    """
    return high.rolling(window=length, min_periods=length).max()


def rolling_low(low: pd.Series, length: int = WEEKS_52_IN_BARS) -> pd.Series:
    """Lowest low over a trailing window, including the current bar."""
    return low.rolling(window=length, min_periods=length).min()


def distance_from_high(
    close: pd.Series, high: pd.Series, length: int = WEEKS_52_IN_BARS
) -> pd.Series:
    """Percentage below the trailing high. ``-8.0`` means 8% off the 52-week high.

    Always ``<= 0`` when the window is full, since the high includes the current
    bar. A value of exactly ``0`` means price is at a new window high.
    """
    return pct_distance(close, rolling_high(high, length))


def distance_from_low(
    close: pd.Series, low: pd.Series, length: int = WEEKS_52_IN_BARS
) -> pd.Series:
    """Percentage above the trailing low. Always ``>= 0`` on a full window."""
    return pct_distance(close, rolling_low(low, length))


def position_in_range(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    length: int = WEEKS_52_IN_BARS,
) -> pd.Series:
    """Where price sits in its 52-week range: 0 at the low, 1 at the high.

    A single normalised number, which makes it usable for the historical-analogue
    search in a way that two separate distance features are not -- matching on one
    dimension finds far more comparable observations than matching on two.
    """
    window_high = rolling_high(high, length)
    window_low = rolling_low(low, length)
    return safe_divide(close - window_low, window_high - window_low)
