"""Self-contained HTML output that needs no server.

The React dashboard is the richer surface, but it needs a running FastAPI process, which means
the user's computer has to be on. This package produces a single HTML file with everything
inlined -- no fetch, no build step, no backend -- so a scheduled job can publish it and a phone
can open it.

That is the only reason this exists alongside the dashboard, and the division of labour is:

* :mod:`app.backtesting.report` -- one backtest, in depth. Written on demand.
* :mod:`app.reporting.digest` -- the daily state of the universe. Written by a schedule.

Both are deliberately static files rather than snapshots of the API, because a page that fetches
is a page that breaks the moment the thing it fetches from stops running.
"""

from app.reporting.digest import generate_digest, render_digest

__all__ = ["generate_digest", "render_digest"]
