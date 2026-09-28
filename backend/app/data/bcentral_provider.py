"""Banco Central de Chile — API BDE (Base de Datos Estadísticos).

Cost: **free**, but requires registration (email + password) at
https://si3.bcentral.cl/estadisticas/Principal1/Web_Services/ . No payment, no card,
no tier upgrade. Credentials go in ``.env`` as ``BCCH_USER`` / ``BCCH_PASSWORD``.

Why this exists
---------------
It is the only free source found that carries the **IPSA**. The index itself is a
licensed S&P Dow Jones product and Yahoo serves nothing for it: six spellings were
probed on 2026-09-27 (``^IPSA``, ``IPSA.SN``, ``^SPIPSA``, ``^SPCLXIPSA``, ``^CLX``,
``IPSA``) and every one returned zero rows. Without this provider the Chilean
benchmark is ECH, a USD-denominated NYSE ETF whose returns embed the CLP/USD
exchange rate — see ``app.core.universe.BENCHMARK_AVAILABILITY``.

What it does *not* do
---------------------
It carries no individual equity prices. The BDE is a macro-financial database:
indices, exchange rates, interest rates, monetary aggregates. Chilean per-stock data
still comes from Yahoo. No free source for that was found — the Bolsa de Santiago's
public site sits behind Imperva bot protection, its "API Brain Data" is a commercial
product, and Stooq gates every request behind a proof-of-work challenge.

Verification status — read before trusting this
----------------------------------------------
**Verified live on 2026-09-27, without credentials:** the endpoint is reachable, and
it answers malformed requests with well-formed JSON in the documented shape —
``{"Codigo": -5, "Descripcion": "Invalid username or password", "Series": {...},
"SeriesInfos": []}``. So the transport, the response schema and the error contract
are confirmed.

**Not verified:** the success path, and the IPSA series identifier. The API
authenticates *before* it validates the series argument, so no amount of probing
without credentials can confirm a series code. This provider therefore **discovers**
the identifier through ``SearchSeries`` instead of hard-coding a guess, in keeping
with how Chilean equity tickers are resolved elsewhere in this project. A guessed
code presented as fact is exactly the kind of claim this codebase avoids.

Terms and limits
----------------
Per the Bank's published terms: consulting, reproducing, disseminating, publishing
and adapting BDE content is permitted provided the Bank is credited as the owner of
the information. The documented rate limit is **5 series per second per account**,
regardless of source IP. :attr:`BancoCentralProvider.MIN_REQUEST_INTERVAL` enforces a
more conservative spacing than that, since one index series is all this needs.
"""

from __future__ import annotations

import time as time_module
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests

from app.config import get_settings
from app.core.exceptions import (
    EmptyDataError,
    ProviderUnavailableError,
    RateLimitError,
    SymbolNotFoundError,
)
from app.core.logging import get_logger
from app.data.provider import (
    DataProvider,
    ProviderCapabilities,
    Timeframe,
    TimeframeLimit,
)

logger = get_logger(__name__)

__all__ = ["BancoCentralProvider", "BCCH_ENDPOINT", "MissingCredentialsError"]

BCCH_ENDPOINT = "https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx"

ATTRIBUTION = "Source: Banco Central de Chile, Base de Datos Estadísticos (BDE)."
"""Required credit line. Carried into any report that uses a series from here."""

# Documented API result codes. Only -5 is confirmed by live observation.
_CODE_OK = 0
_CODE_BAD_CREDENTIALS = -5


class MissingCredentialsError(ProviderUnavailableError):
    """No BDE credentials configured.

    A subclass of ``ProviderUnavailableError`` on purpose: from a caller's point of
    view an unconfigured provider is unavailable, and the universe download already
    continues past that rather than aborting. Registration is free but it is the
    user's decision, so the absence of credentials must never be fatal.
    """


class BancoCentralProvider(DataProvider):
    """Statistical series from the Banco Central de Chile.

    Used for the IPSA benchmark. Instantiating this without credentials is
    harmless; every request then raises :class:`MissingCredentialsError` with
    instructions, and the rest of the system carries on with ECH as the proxy.
    """

    MIN_REQUEST_INTERVAL = 0.5
    """Seconds between requests. The published cap is 5 series/second per account."""

    IPSA_SEARCH_TERMS = ("ipsa", "precios selectivo de acciones")
    """Substrings used to recognise the IPSA in the series catalogue.

    Matched case-insensitively against each series' Spanish and English
    descriptions. Two terms rather than one because the Bank labels the series by
    its full name in some tables and by the acronym in others.
    """

    _CAPABILITIES = ProviderCapabilities(
        name="bcentral",
        markets=frozenset({"CHILE"}),
        timeframes=(
            TimeframeLimit(Timeframe.D1, None, "Full published history of the series."),
        ),
        provides_adjusted_prices=False,
        requires_api_key=True,
        cost=(
            "Free, but requires free registration (email + password) at "
            "si3.bcentral.cl. No payment of any kind."
        ),
        notes=(
            "Macro-financial series only -- indices, FX, rates. Carries the IPSA, "
            "which no other free source in this project does. Does NOT carry "
            "individual equity prices. Index levels have no volume, so synthesised "
            "bars have open=high=low=close and volume=0. Attribution to the Bank is "
            "required when republishing. Rate limit: 5 series/second per account."
        ),
    )

    def __init__(
        self,
        *,
        user: str | None = None,
        password: str | None = None,
        timeout: int | None = None,
    ) -> None:
        settings = get_settings()
        self._user = user if user is not None else settings.bcch_user
        self._password = password if password is not None else settings.bcch_password
        self._timeout = timeout if timeout is not None else settings.provider_timeout
        self._last_request_at = 0.0
        self._series_cache: dict[str, str] = {}

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._CAPABILITIES

    @property
    def is_configured(self) -> bool:
        """Whether credentials are present. Checked before wiring this in anywhere."""
        return bool(self._user and self._password)

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #

    def _require_credentials(self) -> None:
        if self.is_configured:
            return
        raise MissingCredentialsError(
            "No Banco Central de Chile BDE credentials configured, so the IPSA "
            "cannot be fetched. Registration is free (email + password) at "
            "https://si3.bcentral.cl/estadisticas/Principal1/Web_Services/ ; put the "
            "result in .env as BCCH_USER and BCCH_PASSWORD. Until then the Chilean "
            "benchmark falls back to ECH, a USD-denominated proxy -- see "
            "app.core.universe.BENCHMARK_AVAILABILITY."
        )

    def _throttle(self) -> None:
        elapsed = time_module.monotonic() - self._last_request_at
        remaining = self.MIN_REQUEST_INTERVAL - elapsed
        if remaining > 0:
            time_module.sleep(remaining)
        self._last_request_at = time_module.monotonic()

    def _call(self, **params: Any) -> dict[str, Any]:
        """One BDE request. Returns the parsed payload or raises.

        Credentials are passed as query parameters because the API requires it. They
        are therefore never logged: the ``params`` dict is not included in any log
        line, and failures report only the function name.
        """
        self._require_credentials()
        self._throttle()

        query = {"user": self._user, "pass": self._password, **params}
        function = params.get("function", "?")

        try:
            response = requests.get(BCCH_ENDPOINT, params=query, timeout=self._timeout)
        except requests.Timeout as exc:
            raise ProviderUnavailableError(
                f"Banco Central BDE timed out after {self._timeout}s ({function})"
            ) from exc
        except requests.RequestException as exc:
            raise ProviderUnavailableError(
                f"Banco Central BDE could not be reached ({function}): {exc}"
            ) from exc

        if response.status_code == 429:
            raise RateLimitError(
                "Banco Central BDE rate-limited the request. The documented cap is "
                "5 series per second per account."
            )
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"Banco Central BDE returned HTTP {response.status_code} ({function})"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError(
                f"Banco Central BDE returned non-JSON content ({function}); the "
                "service may be down or behind an interstitial page."
            ) from exc

        code = payload.get("Codigo")
        if code == _CODE_BAD_CREDENTIALS:
            raise MissingCredentialsError(
                "Banco Central BDE rejected the credentials in BCCH_USER / "
                "BCCH_PASSWORD. Note that registering on the BDE site is only the "
                "first step -- the credentials must then be activated for API access."
            )
        if code is not None and code != _CODE_OK:
            raise ProviderUnavailableError(
                f"Banco Central BDE error {code} ({function}): "
                f"{payload.get('Descripcion') or 'no description given'}"
            )
        return payload

    # ------------------------------------------------------------------ #
    # Series discovery
    # ------------------------------------------------------------------ #

    def find_series(self, *terms: str, frequency: str = "DAILY") -> list[dict[str, str]]:
        """Search the catalogue for series whose description matches any of ``terms``.

        Returns ``[{"seriesId": ..., "descripEsp": ..., "descripIng": ...}]``.
        Discovery rather than a hard-coded identifier: the API authenticates before
        validating a series argument, so a code cannot be confirmed without
        credentials, and shipping an unverified one as though it were verified is not
        acceptable in this codebase.
        """
        payload = self._call(function="SearchSeries", frequency=frequency)
        infos = payload.get("SeriesInfos") or []
        needles = [t.lower() for t in terms]

        matches: list[dict[str, str]] = []
        for info in infos:
            haystack = " ".join(
                str(info.get(key) or "") for key in ("descripEsp", "descripIng", "seriesId")
            ).lower()
            if any(needle in haystack for needle in needles):
                matches.append(
                    {
                        "seriesId": str(info.get("seriesId") or ""),
                        "descripEsp": str(info.get("descripEsp") or ""),
                        "descripIng": str(info.get("descripIng") or ""),
                    }
                )
        return matches

    def resolve_ipsa_series(self) -> str:
        """Discover the IPSA series identifier, cached for the process lifetime.

        Raises
        ------
        SymbolNotFoundError
            The catalogue contains nothing matching :attr:`IPSA_SEARCH_TERMS`. That
            would mean the Bank no longer publishes it at daily frequency, in which
            case ECH remains the proxy and the limitation stands.
        """
        if "IPSA" in self._series_cache:
            return self._series_cache["IPSA"]

        matches = self.find_series(*self.IPSA_SEARCH_TERMS)
        if not matches:
            raise SymbolNotFoundError("IPSA", self.name, self.IPSA_SEARCH_TERMS)

        if len(matches) > 1:
            logger.info(
                "BDE catalogue has %d IPSA-like daily series; using %s (%s). Others: %s",
                len(matches),
                matches[0]["seriesId"],
                matches[0]["descripEsp"],
                ", ".join(m["seriesId"] for m in matches[1:5]),
            )

        series_id = matches[0]["seriesId"]
        self._series_cache["IPSA"] = series_id
        return series_id

    def resolve_symbol(self, candidates: tuple[str, ...], canonical: str) -> str:
        """Resolve a canonical symbol to a BDE series identifier.

        ``IPSA`` is discovered through the catalogue. Anything else is taken as a
        literal series id, so a caller can pull another BDE series directly.
        """
        if canonical.upper() == "IPSA" or any(c.upper() == "IPSA" for c in candidates):
            return self.resolve_ipsa_series()
        for candidate in candidates:
            if candidate:
                return candidate
        raise SymbolNotFoundError(canonical, self.name, candidates)

    # ------------------------------------------------------------------ #
    # Data
    # ------------------------------------------------------------------ #

    def _fetch_raw(
        self,
        provider_symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Fetch one series and shape it as OHLCV.

        The BDE returns a level per date, not a bar. ``open``, ``high``, ``low`` and
        ``close`` are therefore all set to the level and ``volume`` to zero — which is
        the truth for an index, not a shortcut. Consumers must not read the absence of
        intraday range as a flat market.
        """
        payload = self._call(
            function="GetSeries",
            timeseries=provider_symbol,
            firstdate=start.date().isoformat(),
            lastdate=end.date().isoformat(),
        )

        series = payload.get("Series") or {}
        observations = series.get("Obs") or []
        if not observations:
            raise EmptyDataError(
                f"Banco Central BDE returned no observations for series "
                f"{provider_symbol!r} between {start.date()} and {end.date()}"
            )

        timestamps: list[pd.Timestamp] = []
        values: list[float] = []
        skipped = 0

        for observation in observations:
            raw_date = observation.get("indexDateString")
            raw_value = observation.get("value")

            # The BDE marks a non-published date with the literal string "NaN".
            # Those are dropped, never carried forward -- the same rule applied to
            # every other source in this project.
            if raw_value in (None, "", "NaN", "nan"):
                skipped += 1
                continue

            # Documented format is dd-MM-yyyy. dayfirst=True keeps 03-04 from being
            # read as 4 March.
            stamp = pd.to_datetime(raw_date, dayfirst=True, errors="coerce")
            if pd.isna(stamp):
                skipped += 1
                continue

            try:
                values.append(float(str(raw_value).replace(",", ".")))
            except (TypeError, ValueError):
                skipped += 1
                continue
            timestamps.append(stamp)

        if not timestamps:
            raise EmptyDataError(
                f"Banco Central BDE returned {len(observations)} observations for "
                f"{provider_symbol!r} but none carried a usable value"
            )
        if skipped:
            logger.debug(
                "BDE series %s: skipped %d unpublished or unparsable observations",
                provider_symbol,
                skipped,
            )

        index = pd.DatetimeIndex(timestamps).tz_localize("UTC")
        level = pd.Series(values, index=index, dtype=float)
        level = level[~level.index.duplicated(keep="last")].sort_index()

        return pd.DataFrame(
            {
                "Open": level,
                "High": level,
                "Low": level,
                "Close": level,
                "Volume": 0.0,
            }
        )

    def fetch_ipsa(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """Convenience wrapper: discover the IPSA series and fetch it."""
        series_id = self.resolve_ipsa_series()
        return self.fetch_bars(
            series_id,
            Timeframe.D1,
            start=start or (datetime.now().astimezone() - timedelta(days=365 * 12)),
            end=end,
            canonical_symbol="IPSA",
        )
