"""Real positions: what was bought, what the exit rule says about it, what it made.

Phase 6. The distinction that organises this package is between *simulated* and *real*:

* :mod:`app.backtesting.portfolio` holds positions this software opened in a simulation.
* :mod:`app.portfolio.holdings` records positions the user opened with their own money,
  through a broker this system cannot see and does not talk to.

They are kept apart so a modelled fill and a real one can never end up in the same average. A
backtest's entry price is an assumption; a holding's is what was paid.

:mod:`app.portfolio.watch` is the bridge: it rebuilds the backtester's own ``Position`` from a
real holding and runs the backtester's own exit resolution against it, so the alert fires on
exactly the rule that was measured rather than on a reimplementation of it.
"""

from app.portfolio.holdings import (
    HoldingSummary,
    close_holding,
    get_holding,
    list_holdings,
    open_holding,
    open_holdings,
    realised_performance,
)
from app.portfolio.watch import WatchOutcome, WatchReport, check_holdings, run_watch

__all__ = [
    "HoldingSummary",
    "open_holding",
    "close_holding",
    "get_holding",
    "list_holdings",
    "open_holdings",
    "realised_performance",
    "WatchOutcome",
    "WatchReport",
    "check_holdings",
    "run_watch",
]
