"""Forward-looking quantities -- **outcomes, never inputs**.

Everything in this module looks into the future on purpose. It exists because the
projection engine has to answer "what happened *after* setups like this one?", and
that requires measuring the future of past bars.

The hard rule
-------------
**No function here may ever feed a strategy, a signal or a scanner score.** They
are labels describing what happened next, and a rule that reads them is not a
strategy, it is a lookup of the answer.

Three guardrails:

1. Every name is prefixed ``forward_``, so a leak is visible in a diff.
2. ``FORWARD_COLUMNS`` lists them all, and
   :func:`app.indicators.registry.compute_features` excludes them by default --
   they only appear when a caller explicitly asks.
3. :func:`assert_no_forward_columns` is called by the strategy engine on its input
   frame, so a leak raises instead of quietly producing a spectacular backtest.

The tail of a forward-looking series is necessarily ``NaN``: the last ``horizon``
bars have no future yet. Those rows must be **dropped, not filled**. Filling them
with zero tells the analogue search that recent setups went nowhere.
"""

from __future__ import annotations

import pandas as pd

from app.core.exceptions import DataLeakageError
from app.indicators.base import safe_divide

__all__ = [
    "forward_return",
    "forward_max_favorable_excursion",
    "forward_max_adverse_excursion",
    "FORWARD_HORIZONS",
    "FORWARD_COLUMNS",
    "forward_frame",
    "assert_no_forward_columns",
]

FORWARD_HORIZONS: tuple[int, ...] = (5, 10, 20, 60)

FORWARD_PREFIX = "forward_"


def forward_return(close: pd.Series, horizon: int) -> pd.Series:
    """Return from bar ``i`` to bar ``i + horizon``, as a percentage.

    Implemented as ``close.shift(-horizon) / close - 1``. The negative shift is
    exactly the look-ahead the rest of the codebase forbids, which is why it is
    quarantined in this module.
    """
    if horizon < 1:
        raise ValueError(f"horizon must be >= 1, got {horizon}")
    future = close.shift(-horizon)
    return safe_divide(future - close, close) * 100.0


def forward_max_favorable_excursion(
    close: pd.Series, high: pd.Series, horizon: int
) -> pd.Series:
    """Best unrealised gain available within the next ``horizon`` bars, in percent.

    Answers "how much did this setup offer at its best?", which is the honest way
    to frame an upside scenario -- distinct from where it happened to close.
    """
    if horizon < 1:
        raise ValueError(f"horizon must be >= 1, got {horizon}")
    # Reverse, roll, reverse back: a forward-looking rolling max with no Python
    # loop. The reversed rolling window ending at j covers high[i .. i+horizon-1]
    # once un-reversed, so shift(-1) moves it to high[i+1 .. i+horizon] -- the
    # future proper, excluding the bar the setup was observed on.
    future_high = high[::-1].rolling(window=horizon, min_periods=horizon).max()[::-1].shift(-1)
    return safe_divide(future_high - close, close) * 100.0


def forward_max_adverse_excursion(
    close: pd.Series, low: pd.Series, horizon: int
) -> pd.Series:
    """Worst unrealised loss suffered within the next ``horizon`` bars, in percent.

    Negative. This is the number a stop-loss has to survive, and the reason a
    setup with a good median outcome can still be untradeable: a median of +3%
    behind a routine -12% excursion will be stopped out long before the median
    arrives.
    """
    if horizon < 1:
        raise ValueError(f"horizon must be >= 1, got {horizon}")
    future_low = low[::-1].rolling(window=horizon, min_periods=horizon).min()[::-1].shift(-1)
    return safe_divide(future_low - close, close) * 100.0


def forward_frame(
    frame: pd.DataFrame, horizons: tuple[int, ...] = FORWARD_HORIZONS
) -> pd.DataFrame:
    """All forward-looking measures for the given horizons.

    The returned frame is index-aligned with ``frame`` and its final rows are
    ``NaN`` by construction. Drop them; do not fill them.
    """
    close, high, low = frame["close"], frame["high"], frame["low"]
    out: dict[str, pd.Series] = {}
    for horizon in horizons:
        out[f"{FORWARD_PREFIX}return_{horizon}"] = forward_return(close, horizon)
        out[f"{FORWARD_PREFIX}mfe_{horizon}"] = forward_max_favorable_excursion(
            close, high, horizon
        )
        out[f"{FORWARD_PREFIX}mae_{horizon}"] = forward_max_adverse_excursion(
            close, low, horizon
        )
    return pd.DataFrame(out, index=frame.index)


FORWARD_COLUMNS: tuple[str, ...] = tuple(
    f"{FORWARD_PREFIX}{kind}_{horizon}"
    for horizon in FORWARD_HORIZONS
    for kind in ("return", "mfe", "mae")
)


def assert_no_forward_columns(frame: pd.DataFrame, *, context: str = "strategy input") -> None:
    """Raise ``DataLeakageError`` if ``frame`` carries any forward-looking column.

    Called by the strategy engine and the optimiser on every input frame. Cheap,
    and it converts the most expensive class of bug in quantitative research into
    an immediate, loud failure.
    """
    leaked = sorted(c for c in frame.columns if str(c).startswith(FORWARD_PREFIX))
    if leaked:
        raise DataLeakageError(
            f"{context} contains forward-looking columns {leaked}. These describe "
            "what happened after each bar and must never be used to decide what to "
            "do at that bar. Drop them before calling this code path."
        )
