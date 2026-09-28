"""Trend indicators: simple and exponential moving averages, and trend state."""

from __future__ import annotations

import pandas as pd

from app.indicators.base import pct_distance

__all__ = [
    "sma",
    "ema",
    "ema_slope",
    "golden_cross",
    "above_ma",
    "trend_score",
    "distance_from_ema",
    "SMA_LENGTHS",
    "EMA_LENGTHS",
]

SMA_LENGTHS: tuple[int, ...] = (20, 50, 100, 200)
EMA_LENGTHS: tuple[int, ...] = (20, 50, 200)


def sma(series: pd.Series, length: int) -> pd.Series:
    """Simple moving average over a trailing window of ``length`` bars.

    ``min_periods=length`` so the first ``length - 1`` values are ``NaN`` rather
    than a mean of however many bars happened to exist.
    """
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")
    return series.rolling(window=length, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average, ``alpha = 2 / (length + 1)``.

    ``adjust=False`` gives the standard recursive form used by charting platforms.
    ``min_periods=length`` suppresses the head of the series: the recursion is
    defined from bar one, but until it has seen ``length`` bars it is dominated by
    its own seed, and publishing that as "the 200-day EMA" invites a strategy to
    trade a number that means nothing.
    """
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def ema_slope(series: pd.Series, length: int, lookback: int = 5) -> pd.Series:
    """Percentage change of an EMA over ``lookback`` bars.

    Positive means the average itself is rising -- a cleaner trend read than the
    price/average comparison alone, which flickers in a sideways market.
    """
    line = ema(series, length)
    # fill_method=None is explicit on purpose: pandas' historical default padded
    # NaNs before differencing, which quietly turns a warm-up gap into a 0% change.
    return line.pct_change(periods=lookback, fill_method=None) * 100.0


def above_ma(close: pd.Series, ma_line: pd.Series) -> pd.Series:
    """Boolean: is price above the moving average?

    ``NaN`` in either input yields ``False``, not ``True``. An unknown answer must
    never read as a satisfied condition -- that is how a strategy ends up
    "trading" the warm-up period of its own indicators.
    """
    return (close > ma_line).where(close.notna() & ma_line.notna(), other=False).astype(bool)


def golden_cross(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """Boolean: is the fast average above the slow one? (``EMA50 > EMA200``)"""
    return (fast > slow).where(fast.notna() & slow.notna(), other=False).astype(bool)


def trend_score(frame: pd.DataFrame) -> pd.Series:
    """Composite trend reading in ``[0, 1]``, from four independent conditions.

    The conditions: price above EMA200, price above EMA50, EMA50 above EMA200, and
    a rising EMA50. Each contributes 0.25.

    This is an ordinal summary for ranking and nothing more. A score of 1.0 means
    "all four trend conditions currently hold", **not** "75% likely to rise". The
    scanner must present it as the former.

    Requires ``ema_50`` and ``ema_200`` columns.
    """
    required = {"close", "ema_50", "ema_200"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"trend_score needs columns {sorted(missing)}")

    conditions = [
        above_ma(frame["close"], frame["ema_200"]),
        above_ma(frame["close"], frame["ema_50"]),
        golden_cross(frame["ema_50"], frame["ema_200"]),
        (frame["ema_50"].pct_change(5, fill_method=None) > 0).where(
            frame["ema_50"].notna(), other=False
        ),
    ]
    total = sum(c.astype(float) for c in conditions)
    score = total / len(conditions)
    # Undefined while the slowest input is still warming up.
    return score.where(frame["ema_200"].notna())


def distance_from_ema(close: pd.Series, length: int) -> pd.Series:
    """Percentage distance of price from its ``length``-bar EMA."""
    return pct_distance(close, ema(close, length))
