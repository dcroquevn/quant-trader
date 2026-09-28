"""Volume indicators: volume average, relative volume, volume spikes."""

from __future__ import annotations

import pandas as pd

from app.indicators.base import safe_divide

__all__ = ["volume_sma", "relative_volume", "volume_spike", "dollar_volume"]


def volume_sma(volume: pd.Series, length: int = 20) -> pd.Series:
    """Simple moving average of volume over a trailing window."""
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")
    return volume.rolling(window=length, min_periods=length).mean()


def relative_volume(volume: pd.Series, length: int = 20) -> pd.Series:
    """Current volume divided by its trailing average. ``1.8`` means 80% above normal.

    The average **excludes the current bar** (``shift(1)``). Including it would
    dampen exactly the spike being measured -- on a 5x volume day a 20-bar window
    that contains that day reads about 4.2x instead. Worse, it makes the indicator
    partly a function of itself.

    Returns ``NaN`` when the trailing average is zero, which is a real condition
    for thinly traded Chilean names: "infinitely more volume than nothing" is not
    a tradable observation.
    """
    baseline = volume_sma(volume, length).shift(1)
    return safe_divide(volume, baseline)


def volume_spike(volume: pd.Series, length: int = 20, threshold: float = 2.0) -> pd.Series:
    """Boolean: is relative volume at or above ``threshold``?

    ``NaN`` relative volume yields ``False`` -- an unknown must not read as a
    satisfied condition.
    """
    rvol = relative_volume(volume, length)
    return (rvol >= threshold).where(rvol.notna(), other=False).astype(bool)


def dollar_volume(close: pd.Series, volume: pd.Series, length: int = 20) -> pd.Series:
    """Average traded value per bar, in the instrument's own currency.

    This is the liquidity filter that share counts cannot provide: 100,000 shares
    is meaningful for a CLP stock at 50,000 pesos and negligible for a penny
    stock. Note the currency differs by market, so a cross-market liquidity
    threshold has to be expressed per market, never as one global number.
    """
    turnover = close * volume
    return turnover.rolling(window=length, min_periods=length).mean()
