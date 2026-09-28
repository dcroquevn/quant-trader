"""Yahoo Finance provider via the ``yfinance`` package.

Cost: free, no API key, no account.

What you are actually getting
-----------------------------
``yfinance`` scrapes endpoints that Yahoo publishes for its own website. There is
no contract, no SLA and no support. Yahoo has changed the response shape and
broken the library repeatedly. Consequences that matter for research:

* **Symbols vanish.** A delisted or renamed ticker returns an empty frame rather
  than an error, which is why symbol resolution is empirical here.
* **Rate limiting is undocumented** and shows up as sporadic empty responses, not
  as a clean 429. The retry logic below treats both the same way.
* **Adjusted prices are recomputed on every request.** Two downloads weeks apart
  can disagree about the same historical bar after a dividend. The database
  stores ``adj_close`` alongside the raw close so a change is at least visible.
* **Intraday history is shallow**: 730 days for hourly, 60 days for 5/15-minute.
  These limits are enforced in :attr:`capabilities`, not discovered at runtime.

Daily timestamp convention
--------------------------
Yahoo returns daily bars stamped at exchange-local midnight, so the same session
lands at 05:00 UTC for New York and 03:00 UTC for Santiago. Comparing markets on
exact timestamps would then never align. Daily bars are therefore re-stamped to
**midnight UTC of the exchange-local session date**, which makes one row per
session per market and lets a multi-market portfolio join on the index. Intraday
bars keep their true UTC instants, where the offset is the whole point.
"""

from __future__ import annotations

import time as time_module
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.config import get_settings
from app.core.exceptions import (
    EmptyDataError,
    ProviderUnavailableError,
    RateLimitError,
)
from app.core.logging import get_logger
from app.data.provider import (
    DataProvider,
    ProviderCapabilities,
    Timeframe,
    TimeframeLimit,
)

logger = get_logger(__name__)

__all__ = ["YFinanceProvider", "YF_INTERVALS"]


YF_INTERVALS: dict[Timeframe, str] = {
    Timeframe.D1: "1d",
    Timeframe.H1: "1h",
    Timeframe.M15: "15m",
    Timeframe.M5: "5m",
}

_RATE_LIMIT_HINTS = ("rate limit", "too many requests", "429")
_UNAVAILABLE_HINTS = ("timeout", "timed out", "connection", "dns", "ssl", "503", "502")


class YFinanceProvider(DataProvider):
    """Free historical bars for any Yahoo-listed instrument.

    Covers both markets this project targets: US tickers are used verbatim,
    Bolsa de Santiago tickers carry the ``.SN`` suffix. The market-specific
    handling lives in the symbol mapping (``app.core.universe``), not here, so
    this class stays a thin, honest wrapper.
    """

    _CAPABILITIES = ProviderCapabilities(
        name="yfinance",
        markets=frozenset({"USA", "CHILE"}),
        timeframes=(
            TimeframeLimit(Timeframe.D1, None, "Full available history."),
            TimeframeLimit(
                Timeframe.H1,
                730,
                "Yahoo serves at most ~730 days of hourly bars.",
            ),
            TimeframeLimit(
                Timeframe.M15,
                60,
                "Yahoo serves at most ~60 days of 15-minute bars.",
            ),
            TimeframeLimit(
                Timeframe.M5,
                60,
                "Yahoo serves at most ~60 days of 5-minute bars.",
            ),
        ),
        provides_adjusted_prices=True,
        requires_api_key=False,
        cost="Free. No API key, no account, no rate-limit guarantee.",
        notes=(
            "Unofficial scraping of Yahoo endpoints. No SLA. Adjusted prices are "
            "recomputed per request and can change retroactively. Corporate "
            "actions are available for daily bars only."
        ),
    )

    def __init__(
        self,
        *,
        request_delay: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        settings = get_settings()
        self._request_delay = (
            request_delay if request_delay is not None else settings.provider_request_delay
        )
        self._max_retries = max_retries if max_retries is not None else settings.provider_max_retries
        self._last_request_at = 0.0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._CAPABILITIES

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _throttle(self) -> None:
        """Space out requests. Yahoo's limits are undocumented; be polite."""
        if self._request_delay <= 0:
            return
        elapsed = time_module.monotonic() - self._last_request_at
        remaining = self._request_delay - elapsed
        if remaining > 0:
            time_module.sleep(remaining)
        self._last_request_at = time_module.monotonic()

    @staticmethod
    def _classify(exc: Exception) -> Exception:
        """Map a vendor exception onto this project's taxonomy."""
        text = str(exc).lower()
        if any(hint in text for hint in _RATE_LIMIT_HINTS):
            return RateLimitError(f"yfinance rate-limited the request: {exc}")
        if any(hint in text for hint in _UNAVAILABLE_HINTS):
            return ProviderUnavailableError(f"yfinance could not be reached: {exc}")
        return ProviderUnavailableError(f"yfinance request failed: {exc}")

    @staticmethod
    def _restamp_daily(frame: pd.DataFrame) -> pd.DataFrame:
        """Re-stamp daily bars to midnight UTC of the exchange-local session date.

        See the module docstring for why. Without this, a US bar and a Chilean bar
        from the same session differ by two hours and never join.
        """
        index = pd.DatetimeIndex(frame.index)
        if index.tz is None:
            local_midnight = index.normalize()
        else:
            # Already in exchange-local tz as returned by yfinance.
            local_midnight = index.normalize().tz_localize(None)
        out = frame.copy()
        out.index = pd.DatetimeIndex(local_midnight).tz_localize("UTC")
        out.index.name = "ts"
        return out

    def _fetch_raw(
        self,
        provider_symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        import yfinance as yf

        interval = YF_INTERVALS[timeframe]
        last_error: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            self._throttle()
            try:
                ticker = yf.Ticker(provider_symbol)
                frame = ticker.history(
                    start=start.date().isoformat(),
                    # Yahoo treats `end` as exclusive; nudge it so the caller's
                    # inclusive end date is actually returned.
                    end=(end.date() + timedelta(days=1)).isoformat(),
                    interval=interval,
                    auto_adjust=False,
                    actions=True,
                    raise_errors=False,
                )
            except Exception as exc:  # noqa: BLE001 -- vendor raises bare Exceptions
                last_error = self._classify(exc)
                logger.warning(
                    "yfinance attempt %d/%d failed for %s: %s",
                    attempt,
                    self._max_retries,
                    provider_symbol,
                    exc,
                )
                if attempt < self._max_retries:
                    time_module.sleep(min(2.0**attempt, 10.0))
                continue

            if frame is None or frame.empty:
                # Indistinguishable from a soft rate-limit, so retry once or twice
                # before concluding the symbol simply has no data.
                last_error = EmptyDataError(
                    f"yfinance returned no rows for {provider_symbol!r} "
                    f"({interval}, {start.date()} to {end.date()})"
                )
                if attempt < self._max_retries:
                    logger.debug(
                        "Empty response for %s (attempt %d/%d); retrying",
                        provider_symbol,
                        attempt,
                        self._max_retries,
                    )
                    time_module.sleep(min(1.5**attempt, 5.0))
                    continue
                break

            if timeframe is Timeframe.D1:
                frame = self._restamp_daily(frame)
            return frame

        raise last_error or EmptyDataError(f"yfinance returned nothing for {provider_symbol!r}")

    # ------------------------------------------------------------------ #
    # Symbol resolution
    # ------------------------------------------------------------------ #

    def resolve_symbol(self, candidates: tuple[str, ...], canonical: str) -> str:
        """Probe candidates over a window wide enough to survive a quiet ticker.

        The base implementation uses 30 days, which is too short for a thinly
        traded Chilean name that may not print for days at a time. A one-year
        window keeps a real-but-illiquid listing from being declared missing.
        """
        from app.core.exceptions import SymbolNotFoundError

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=self.RESOLUTION_PROBE_DAYS)

        for candidate in candidates:
            try:
                raw = self._fetch_raw(candidate, Timeframe.D1, start, end)
            except Exception as exc:  # noqa: BLE001 -- any failure means "try next"
                logger.debug("Candidate %s did not resolve: %s", candidate, exc)
                continue
            if raw is not None and len(raw) > 0:
                logger.info("Resolved %s -> %s on yfinance", canonical, candidate)
                return candidate

        raise SymbolNotFoundError(canonical, self.name, candidates)
