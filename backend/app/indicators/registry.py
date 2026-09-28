"""Feature computation: turns a bar frame into the full indicator set.

One entry point, :func:`compute_features`, so there is exactly one place where the
feature vocabulary is defined. The strategy engine, the scanner and the
historical-analogue search all consume the same columns, computed the same way --
otherwise a backtest and a live signal can silently disagree about what "RSI" means.

Everything is vectorised over the whole series in one pass, which is both fast and
a structural defence against look-ahead bias: there is no per-bar loop that could
accidentally index forward.

``FEATURE_VERSION`` is stamped into the ``features`` table. Bump it whenever a
definition changes, or the cache will keep serving values computed by the old
code -- including, in the worst case, values computed by a bug you just fixed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.indicators import momentum, price, trend, volatility, volume
from app.indicators.base import pct_distance, validate_frame
from app.indicators.forward import FORWARD_HORIZONS, forward_frame

__all__ = [
    "FEATURE_VERSION",
    "FEATURE_COLUMNS",
    "MIN_BARS_FOR_FULL_FEATURES",
    "compute_features",
    "latest_features",
]

FEATURE_VERSION = "v1"

# The slowest indicator is the 252-bar 52-week high/low, followed by the 200-bar
# moving averages. A frame shorter than this yields a feature set with NaN holes --
# not an error, but not a complete observation either, and the scanner must not
# rank a partially-computed row against a complete one.
MIN_BARS_FOR_FULL_FEATURES = 252


def compute_features(
    frame: pd.DataFrame,
    *,
    include_forward: bool = False,
    forward_horizons: tuple[int, ...] = FORWARD_HORIZONS,
) -> pd.DataFrame:
    """Compute the full indicator set for one instrument's bars.

    Parameters
    ----------
    frame:
        Bars indexed by timestamp with ``open/high/low/close/volume``. Should
        already be on adjusted prices -- see
        ``app.database.repository.load_bars``.
    include_forward:
        Adds the ``forward_*`` outcome columns. **Off by default.** Only the
        projection engine and analogue search should switch it on; a strategy
        receiving these columns is reading the answer. See
        :mod:`app.indicators.forward`.

    Returns
    -------
    DataFrame
        The original bar columns plus every indicator, on the same index. Early
        rows carry ``NaN`` where an indicator has not warmed up; they are left in
        place rather than dropped so the frame stays aligned with the bars.
    """
    df = validate_frame(frame).copy()
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

    features: dict[str, pd.Series] = {}

    # ---- Trend ----------------------------------------------------------
    for length in trend.SMA_LENGTHS:
        features[f"sma_{length}"] = trend.sma(close, length)
    for length in trend.EMA_LENGTHS:
        features[f"ema_{length}"] = trend.ema(close, length)

    # ---- Momentum -------------------------------------------------------
    features["rsi_14"] = momentum.rsi(close, 14)
    macd_frame = momentum.macd(close, 12, 26, 9)
    for column in macd_frame.columns:
        features[column] = macd_frame[column]
    for period in momentum.ROC_PERIODS:
        features[f"roc_{period}"] = momentum.roc(close, period)

    # ---- Volatility -----------------------------------------------------
    features["atr_14"] = volatility.atr(high, low, close, 14)
    features["atr_pct_14"] = volatility.atr_pct(high, low, close, 14)
    features["volatility_20"] = volatility.rolling_volatility(close, 20)
    features["volatility_60"] = volatility.rolling_volatility(close, 60)
    bb = volatility.bollinger_bands(close, 20, 2.0)
    for column in bb.columns:
        features[column] = bb[column]
    features["bb_percent_b"] = volatility.bollinger_percent_b(close, 20, 2.0)

    # ---- Volume ---------------------------------------------------------
    features["volume_sma_20"] = volume.volume_sma(vol, 20)
    features["relative_volume_20"] = volume.relative_volume(vol, 20)
    features["volume_spike_20"] = volume.volume_spike(vol, 20, 2.0)
    features["dollar_volume_20"] = volume.dollar_volume(close, vol, 20)

    # ---- Price / returns ------------------------------------------------
    for period in price.RETURN_PERIODS:
        features[f"return_{period}d"] = price.trailing_return(close, period)

    # ---- Distance from references ---------------------------------------
    # Computed against the EMAs already in `features` so the two cannot drift apart.
    for length in trend.EMA_LENGTHS:
        features[f"dist_ema_{length}_pct"] = pct_distance(close, features[f"ema_{length}"])

    features["high_52w"] = price.rolling_high(high, price.WEEKS_52_IN_BARS)
    features["low_52w"] = price.rolling_low(low, price.WEEKS_52_IN_BARS)
    features["dist_52w_high_pct"] = price.distance_from_high(close, high)
    features["dist_52w_low_pct"] = price.distance_from_low(close, low)
    features["position_52w_range"] = price.position_in_range(close, high, low)

    out = pd.concat([df, pd.DataFrame(features, index=df.index)], axis=1)

    # ---- Composite scores (need the columns above to exist) --------------
    out["trend_score"] = trend.trend_score(out)
    out["momentum_score"] = momentum.momentum_score(out)

    if include_forward:
        out = pd.concat([out, forward_frame(df, forward_horizons)], axis=1)

    return out


FEATURE_COLUMNS: tuple[str, ...] = (
    *(f"sma_{n}" for n in trend.SMA_LENGTHS),
    *(f"ema_{n}" for n in trend.EMA_LENGTHS),
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    *(f"roc_{n}" for n in momentum.ROC_PERIODS),
    "atr_14",
    "atr_pct_14",
    "volatility_20",
    "volatility_60",
    "bb_mid",
    "bb_upper",
    "bb_lower",
    "bb_width",
    "bb_percent_b",
    "volume_sma_20",
    "relative_volume_20",
    "volume_spike_20",
    "dollar_volume_20",
    *(f"return_{n}d" for n in price.RETURN_PERIODS),
    *(f"dist_ema_{n}_pct" for n in trend.EMA_LENGTHS),
    "high_52w",
    "low_52w",
    "dist_52w_high_pct",
    "dist_52w_low_pct",
    "position_52w_range",
    "trend_score",
    "momentum_score",
)
"""Every non-forward feature column, in computation order.

Used to validate that :func:`compute_features` produces what downstream code
expects. A column added to one and not the other is a test failure.
"""


def latest_features(frame: pd.DataFrame, *, require_complete: bool = True) -> dict[str, float]:
    """Feature values on the most recent bar, as a plain dict.

    Parameters
    ----------
    require_complete:
        When True, raises ``InsufficientDataError`` if the newest row still has
        ``NaN`` in any feature. That is the right default for signal generation:
        a strategy comparing ``price > ema_200`` against ``NaN`` silently evaluates
        to False and produces a HOLD that looks like a considered decision.

    Returns
    -------
    dict
        Feature name to value. ``NaN`` becomes ``None`` so the result is
        JSON-serialisable for the ``signals`` table and the API.
    """
    from app.core.exceptions import InsufficientDataError

    if frame.empty:
        raise InsufficientDataError("Cannot extract features from an empty frame")

    row = frame.iloc[-1]
    present = [c for c in FEATURE_COLUMNS if c in frame.columns]

    if require_complete:
        incomplete = [c for c in present if pd.isna(row[c])]
        if incomplete:
            shown = ", ".join(incomplete[:8]) + (" ..." if len(incomplete) > 8 else "")
            if len(frame) < MIN_BARS_FOR_FULL_FEATURES:
                cause = (
                    f"the frame has {len(frame)} bars but a complete feature set needs "
                    f"at least {MIN_BARS_FOR_FULL_FEATURES}"
                )
            else:
                # The history is long enough, so the gap is in the data itself --
                # most often a run of zero-volume bars, which makes every
                # volume-ratio feature undefined. Saying "not enough bars" here
                # would send the reader to re-download data that is already there.
                cause = (
                    f"the frame has {len(frame)} bars, which is enough history, so the "
                    "gap is in the values themselves -- typically zero-volume or flat "
                    "bars that make ratio features undefined. Run `python -m app audit` "
                    "to see whether the vendor is carrying quotes forward"
                )
            raise InsufficientDataError(
                f"{len(incomplete)} of {len(present)} features are still NaN on the "
                f"latest bar ({frame.index[-1]:%Y-%m-%d}): {shown}. Because {cause}."
            )

    result: dict[str, float | bool | None] = {}
    for column in present:
        value = row[column]
        if pd.isna(value):
            result[column] = None
        elif isinstance(value, (bool, np.bool_)):
            # numpy.bool_ is not a subclass of bool, so a plain isinstance(value,
            # bool) check lets boolean features through as 0.0/1.0 floats and the UI
            # renders "0.0000" where it should say "no".
            result[column] = bool(value)
        else:
            result[column] = float(value)
    return result
