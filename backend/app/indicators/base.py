"""Shared primitives for every indicator.

The one rule
------------
An indicator value at position ``i`` may only depend on data at positions
``<= i``. Violating this is *look-ahead bias*, and it is the failure mode that
makes a worthless strategy look excellent in a backtest and lose money live.

Three habits enforce it here, and the test suite checks all three:

1. **Never ``center=True``.** A centred rolling window averages the future.
2. **Never backward-fill.** ``bfill`` copies a later value into an earlier slot.
   Forward-fill is fine: it propagates what was already known.
3. **Respect ``min_periods``.** A 200-day average computed from 5 observations is
   not an early estimate, it is a different statistic wearing the same name.
   Every function below returns ``NaN`` until it has a full window.

The canonical test is :func:`assert_no_lookahead`-style truncation: computing an
indicator on the first ``k`` bars must give exactly the same values as computing
it on all ``n`` bars and slicing to ``k``. Anything that peeks forward fails it.

Forward-looking quantities *are* needed -- for labelling historical analogues and
measuring what happened next. Those live in :mod:`app.indicators.forward`, are
prefixed ``forward_``, and must never be used as strategy inputs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "OHLCV_REQUIRED",
    "validate_frame",
    "wilder_rma",
    "safe_divide",
    "pct_distance",
]

OHLCV_REQUIRED: tuple[str, ...] = ("open", "high", "low", "close", "volume")


def validate_frame(frame: pd.DataFrame, *, required: tuple[str, ...] = OHLCV_REQUIRED) -> pd.DataFrame:
    """Check a bar frame is usable, and return it sorted ascending.

    Raises
    ------
    ValueError
        Missing columns, a non-datetime index, or duplicate timestamps.

    Duplicate timestamps are rejected rather than de-duplicated: two rows for one
    session means an upstream bug, and silently picking one would hide it.
    """
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(
            f"Bar frame is missing required columns {missing}; got {sorted(frame.columns)}"
        )
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError(
            f"Bar frame must have a DatetimeIndex, got {type(frame.index).__name__}"
        )
    if frame.index.has_duplicates:
        dupes = frame.index[frame.index.duplicated()].unique()
        raise ValueError(
            f"Bar frame has {len(dupes)} duplicate timestamps, first at {dupes[0]}. "
            "De-duplicate upstream -- indicators must not guess which row is real."
        )
    if not frame.index.is_monotonic_increasing:
        frame = frame.sort_index()
    return frame


def wilder_rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's Running Moving Average -- the smoothing behind RSI and ATR.

    Seeded with the simple mean of the first ``length`` observations, then
    recursive::

        rma[i] = (rma[i-1] * (length - 1) + x[i]) / length

    This is implemented explicitly rather than as
    ``series.ewm(alpha=1/length, adjust=False).mean()``. The two converge, but
    they disagree over roughly the first ``3 * length`` bars, which is exactly the
    range where a short backtest lives -- and it is the reason hand-rolled RSI
    implementations so often fail to match a charting platform.

    Returns ``NaN`` for the first ``length - 1`` positions.
    """
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")

    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    out = np.full(values.shape, np.nan, dtype=float)

    # Skip any leading NaNs (a differenced series always starts with one).
    valid = np.flatnonzero(~np.isnan(values))
    if valid.size < length:
        return pd.Series(out, index=series.index, name=series.name)

    start = valid[0]
    if start + length > values.size:
        return pd.Series(out, index=series.index, name=series.name)

    seed_slice = values[start : start + length]
    if np.isnan(seed_slice).any():
        # A NaN inside the seed window makes the recursion undefined from the
        # outset; refuse rather than silently seeding from fewer points.
        return pd.Series(out, index=series.index, name=series.name)

    previous = float(seed_slice.mean())
    out[start + length - 1] = previous

    for i in range(start + length, values.size):
        current = values[i]
        if np.isnan(current):
            out[i] = previous
            continue
        previous = (previous * (length - 1) + current) / length
        out[i] = previous

    return pd.Series(out, index=series.index, name=series.name)


def safe_divide(
    numerator: pd.Series, denominator: pd.Series, *, fill: float = np.nan
) -> pd.Series:
    """Element-wise division with zero denominators mapped to ``fill``.

    Zero denominators are real in this domain: a zero-volume Chilean session
    makes relative volume undefined. ``NaN`` is the honest answer; ``0`` or ``1``
    would be a fabricated observation that a scanner would happily rank.
    """
    denom = denominator.replace(0.0, np.nan)
    result = (numerator / denom).replace([np.inf, -np.inf], np.nan)
    if not np.isnan(fill):
        result = result.fillna(fill)
    return result


def pct_distance(price: pd.Series, reference: pd.Series) -> pd.Series:
    """Percentage distance of ``price`` from ``reference``, as a percentage.

    ``+4.0`` means price is 4% above the reference.
    """
    return safe_divide(price - reference, reference) * 100.0
