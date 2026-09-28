"""Banco Central de Chile (API BDE) provider tests.

The provider exists for one reason: it is the only free source found that carries
the IPSA. Yahoo serves nothing for it under any of six spellings.

What is and is not verified against the live service
---------------------------------------------------
Confirmed live on 2026-09-27 without credentials, and re-checked by
``TestLiveErrorContract`` below: the endpoint is reachable and answers a malformed
request with well-formed JSON in the documented shape, carrying
``Codigo: -5`` / ``"Invalid username or password"``.

Not confirmed: the success path and the IPSA series identifier. The API
authenticates before it validates the series argument, so no probing without
credentials can confirm a code. That is precisely why the provider *discovers* the
identifier through the catalogue instead of hard-coding one, and why the response
parsing below is tested against synthetic payloads built from the published schema
rather than against a recorded real response that does not exist yet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
import pytest
import requests

from app.core.exceptions import (
    EmptyDataError,
    ProviderUnavailableError,
    RateLimitError,
    SymbolNotFoundError,
)
from app.data.bcentral_provider import (
    BCCH_ENDPOINT,
    BancoCentralProvider,
    MissingCredentialsError,
)
from app.data.provider import Timeframe
from app.data.registry import get_provider, provider_cost_table, provider_for_market


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, payload: Any, status_code: int = 200, *, valid_json: bool = True) -> None:
        self._payload = payload
        self.status_code = status_code
        self._valid_json = valid_json

    def json(self) -> Any:
        if not self._valid_json:
            raise ValueError("not json")
        return self._payload


def observation(date: str, value: str) -> dict[str, str]:
    """One BDE observation in the documented shape (dd-MM-yyyy, value as string)."""
    return {"indexDateString": date, "value": value, "statusCode": "OK"}


def series_payload(observations: list[dict[str, str]], series_id: str = "F013.IPSA.X") -> dict:
    return {
        "Codigo": 0,
        "Descripcion": "",
        "Series": {
            "descripEsp": "Indice de precios selectivo de acciones (IPSA)",
            "descripIng": "Selective stock price index (IPSA)",
            "seriesId": series_id,
            "Obs": observations,
        },
        "SeriesInfos": [],
    }


@pytest.fixture
def provider() -> BancoCentralProvider:
    return BancoCentralProvider(user="u", password="p")


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    """Skip the inter-request sleep so the suite stays fast."""
    monkeypatch.setattr(BancoCentralProvider, "MIN_REQUEST_INTERVAL", 0.0)


# --------------------------------------------------------------------------- #
# Configuration and the zero-hard-dependency rule
# --------------------------------------------------------------------------- #


class TestConfiguration:
    def test_unconfigured_provider_constructs_without_error(self) -> None:
        """Instantiating it with no credentials must be harmless.

        The registry builds every provider eagerly to report capabilities, so a
        constructor that raised would break `python -m app providers` for a user who
        never signed up.
        """
        unconfigured = BancoCentralProvider(user="", password="")
        assert unconfigured.is_configured is False
        assert unconfigured.name == "bcentral"

    def test_requests_without_credentials_explain_how_to_register(self) -> None:
        unconfigured = BancoCentralProvider(user="", password="")
        with pytest.raises(MissingCredentialsError) as excinfo:
            unconfigured.fetch_bars("F013.IPSA.X", Timeframe.D1)

        message = str(excinfo.value)
        assert "si3.bcentral.cl" in message
        assert "BCCH_USER" in message
        assert "free" in message.lower()
        # It must also say what happens meanwhile, not just what is missing.
        assert "ECH" in message

    def test_missing_credentials_is_an_unavailability_not_a_crash(self) -> None:
        """Subclassing ProviderUnavailableError is what makes a download skip it."""
        assert issubclass(MissingCredentialsError, ProviderUnavailableError)

    def test_it_is_not_the_default_for_any_market(self) -> None:
        """A credentialled provider must never be required for a fresh checkout."""
        for market in ("USA", "CHILE"):
            assert provider_for_market(market).name != "bcentral"

    def test_it_is_registered_and_declares_itself_free(self) -> None:
        assert get_provider("bcentral").name == "bcentral"
        row = next(r for r in provider_cost_table() if r["provider"] == "bcentral")
        assert row["api_key_required"] == "yes"
        assert "free" in row["cost"].lower()
        assert "registration" in row["cost"].lower()

    def test_it_only_covers_chile_and_carries_no_equities(self) -> None:
        caps = BancoCentralProvider(user="u", password="p").capabilities
        assert caps.markets == frozenset({"CHILE"})
        assert "not carry individual equity" in caps.notes.lower()


# --------------------------------------------------------------------------- #
# Error contract
# --------------------------------------------------------------------------- #


class TestErrorHandling:
    def test_code_minus_five_maps_to_a_credentials_error(self, provider, monkeypatch) -> None:
        """The exact payload observed live on 2026-09-27."""
        payload = {
            "Codigo": -5,
            "Descripcion": "Invalid username or password",
            "Series": {"descripEsp": None, "descripIng": None, "seriesId": None, "Obs": None},
            "SeriesInfos": [],
        }
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))

        with pytest.raises(MissingCredentialsError, match="activated"):
            provider.fetch_bars("F013.IPSA.X", Timeframe.D1)

    def test_other_error_codes_surface_the_description(self, provider, monkeypatch) -> None:
        payload = {"Codigo": -99, "Descripcion": "Series not found", "Series": {}, "SeriesInfos": []}
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))

        with pytest.raises(ProviderUnavailableError, match="Series not found"):
            provider.fetch_bars("NOPE", Timeframe.D1)

    def test_http_429_is_a_rate_limit(self, provider, monkeypatch) -> None:
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse({}, status_code=429))
        with pytest.raises(RateLimitError, match="5 series per second"):
            provider.fetch_bars("F013.IPSA.X", Timeframe.D1)

    def test_http_500_is_unavailability(self, provider, monkeypatch) -> None:
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse({}, status_code=503))
        with pytest.raises(ProviderUnavailableError, match="503"):
            provider.fetch_bars("F013.IPSA.X", Timeframe.D1)

    def test_non_json_response_is_explained(self, provider, monkeypatch) -> None:
        """An interstitial or captcha page returns HTML; say so rather than crashing."""
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: FakeResponse(None, valid_json=False)
        )
        with pytest.raises(ProviderUnavailableError, match="non-JSON"):
            provider.fetch_bars("F013.IPSA.X", Timeframe.D1)

    def test_timeout_is_classified(self, provider, monkeypatch) -> None:
        def boom(*args, **kwargs):
            raise requests.Timeout("too slow")

        monkeypatch.setattr(requests, "get", boom)
        with pytest.raises(ProviderUnavailableError, match="timed out"):
            provider.fetch_bars("F013.IPSA.X", Timeframe.D1)

    def test_credentials_never_appear_in_an_error_message(self, provider, monkeypatch) -> None:
        """A stack trace or log line must not leak the password."""
        secret = "sup3rs3cret"
        leaky = BancoCentralProvider(user="someone@example.com", password=secret)
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse({}, status_code=503))

        with pytest.raises(ProviderUnavailableError) as excinfo:
            leaky.fetch_bars("F013.IPSA.X", Timeframe.D1)
        assert secret not in str(excinfo.value)
        assert "someone@example.com" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


class TestSeriesParsing:
    def _with(self, monkeypatch, payload):
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))

    def test_level_series_becomes_flat_ohlcv(self, provider, monkeypatch) -> None:
        """An index has no intraday range and no volume. That is the truth, not a stub."""
        self._with(
            monkeypatch,
            series_payload(
                [
                    observation("02-01-2024", "6100.5"),
                    observation("03-01-2024", "6150.25"),
                ]
            ),
        )
        frame = provider.fetch_bars("F013.IPSA.X", Timeframe.D1, start="2024-01-01")

        assert len(frame) == 2
        assert frame["close"].tolist() == [6100.5, 6150.25]
        for column in ("open", "high", "low"):
            assert frame[column].tolist() == frame["close"].tolist()
        assert frame["volume"].tolist() == [0.0, 0.0]

    def test_dates_are_parsed_day_first(self, provider, monkeypatch) -> None:
        """03-04-2024 is 3 April, not 4 March. Getting this wrong silently reorders history."""
        self._with(monkeypatch, series_payload([observation("03-04-2024", "6000")]))
        frame = provider.fetch_bars("F013.IPSA.X", Timeframe.D1, start="2024-01-01")

        stamp = frame.index[0]
        assert (stamp.year, stamp.month, stamp.day) == (2024, 4, 3)
        assert str(frame.index.tz) == "UTC"

    def test_nan_observations_are_dropped_not_carried_forward(
        self, provider, monkeypatch
    ) -> None:
        """The BDE marks unpublished dates with the string "NaN".

        Dropping them keeps the same rule this project applies everywhere: a gap
        stays a gap, and a fabricated value is never invented to fill it.
        """
        self._with(
            monkeypatch,
            series_payload(
                [
                    observation("02-01-2024", "6100"),
                    observation("03-01-2024", "NaN"),
                    observation("04-01-2024", "6200"),
                ]
            ),
        )
        frame = provider.fetch_bars("F013.IPSA.X", Timeframe.D1, start="2024-01-01")

        assert len(frame) == 2
        assert frame["close"].tolist() == [6100.0, 6200.0]
        assert pd.Timestamp("2024-01-03", tz="UTC") not in frame.index

    def test_comma_decimal_separator_is_handled(self, provider, monkeypatch) -> None:
        """Chilean locale formatting would otherwise parse as a failure."""
        self._with(monkeypatch, series_payload([observation("02-01-2024", "6100,75")]))
        frame = provider.fetch_bars("F013.IPSA.X", Timeframe.D1, start="2024-01-01")
        assert frame["close"].iloc[0] == pytest.approx(6100.75)

    def test_empty_observation_list_raises(self, provider, monkeypatch) -> None:
        self._with(monkeypatch, series_payload([]))
        with pytest.raises(EmptyDataError, match="no observations"):
            provider.fetch_bars("F013.IPSA.X", Timeframe.D1, start="2024-01-01")

    def test_all_unusable_observations_raise(self, provider, monkeypatch) -> None:
        self._with(
            monkeypatch,
            series_payload([observation("02-01-2024", "NaN"), observation("03-01-2024", "")]),
        )
        with pytest.raises(EmptyDataError, match="none carried a usable value"):
            provider.fetch_bars("F013.IPSA.X", Timeframe.D1, start="2024-01-01")

    def test_duplicate_dates_keep_the_last(self, provider, monkeypatch) -> None:
        self._with(
            monkeypatch,
            series_payload(
                [observation("02-01-2024", "6100"), observation("02-01-2024", "6111")]
            ),
        )
        frame = provider.fetch_bars("F013.IPSA.X", Timeframe.D1, start="2024-01-01")
        assert len(frame) == 1
        assert frame["close"].iloc[0] == pytest.approx(6111.0)

    def test_output_is_sorted_ascending(self, provider, monkeypatch) -> None:
        self._with(
            monkeypatch,
            series_payload(
                [
                    observation("05-01-2024", "6300"),
                    observation("02-01-2024", "6100"),
                    observation("03-01-2024", "6200"),
                ]
            ),
        )
        frame = provider.fetch_bars("F013.IPSA.X", Timeframe.D1, start="2024-01-01")
        assert frame.index.is_monotonic_increasing


# --------------------------------------------------------------------------- #
# Series discovery
# --------------------------------------------------------------------------- #


class TestSeriesDiscovery:
    def _catalogue(self, monkeypatch, infos):
        payload = {"Codigo": 0, "Descripcion": "", "Series": {}, "SeriesInfos": infos}
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(payload))

    def test_finds_the_ipsa_by_description(self, provider, monkeypatch) -> None:
        """Discovery, not a hard-coded guess -- the code cannot be verified offline."""
        self._catalogue(
            monkeypatch,
            [
                {"seriesId": "F013.IBC.X", "descripEsp": "Indice general de precios", "descripIng": ""},
                {
                    "seriesId": "F013.IPSA.X",
                    "descripEsp": "Indice de precios selectivo de acciones (IPSA)",
                    "descripIng": "Selective stock price index",
                },
            ],
        )
        assert provider.resolve_ipsa_series() == "F013.IPSA.X"

    def test_matches_the_full_name_when_the_acronym_is_absent(
        self, provider, monkeypatch
    ) -> None:
        self._catalogue(
            monkeypatch,
            [
                {
                    "seriesId": "F013.SOMETHING.X",
                    "descripEsp": "Indice de Precios Selectivo de Acciones",
                    "descripIng": "",
                }
            ],
        )
        assert provider.resolve_ipsa_series() == "F013.SOMETHING.X"

    def test_discovery_is_cached(self, provider, monkeypatch) -> None:
        calls = {"n": 0}

        def counting(*args, **kwargs):
            calls["n"] += 1
            return FakeResponse(
                {
                    "Codigo": 0,
                    "Descripcion": "",
                    "Series": {},
                    "SeriesInfos": [
                        {"seriesId": "F013.IPSA.X", "descripEsp": "IPSA", "descripIng": ""}
                    ],
                }
            )

        monkeypatch.setattr(requests, "get", counting)
        provider.resolve_ipsa_series()
        provider.resolve_ipsa_series()
        assert calls["n"] == 1, "catalogue was searched twice"

    def test_absent_ipsa_raises_rather_than_guessing(self, provider, monkeypatch) -> None:
        """If the Bank stops publishing it, say so. ECH remains the documented proxy."""
        self._catalogue(
            monkeypatch,
            [{"seriesId": "F013.TCO.X", "descripEsp": "Tipo de cambio observado", "descripIng": ""}],
        )
        with pytest.raises(SymbolNotFoundError, match="IPSA"):
            provider.resolve_ipsa_series()

    def test_resolve_symbol_passes_through_a_literal_series_id(
        self, provider, monkeypatch
    ) -> None:
        """Any other BDE series can be pulled directly without catalogue lookup."""
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: pytest.fail("should not call the API")
        )
        assert provider.resolve_symbol(("F073.TCO.PRE.Z.D",), "USDCLP") == "F073.TCO.PRE.Z.D"


# --------------------------------------------------------------------------- #
# Live: the one thing that can be checked without credentials
# --------------------------------------------------------------------------- #


@pytest.mark.network
class TestLiveErrorContract:
    def test_endpoint_still_answers_with_the_documented_error_shape(self) -> None:
        """Guards against the API being retired or moved behind a captcha.

        This asserts only what is verifiable without an account: that the service is
        reachable and returns JSON in the documented shape with the credentials error.
        If this starts failing, the provider's assumptions need re-checking before
        anyone registers.
        """
        response = requests.get(
            BCCH_ENDPOINT,
            params={
                "user": "quant-trader-contract-check",
                "pass": "not-a-real-password",
                "function": "GetSeries",
                "timeseries": "F013.IPSA.X",
            },
            timeout=30,
        )
        assert response.status_code == 200

        payload = response.json()
        assert payload["Codigo"] == -5
        assert "username or password" in payload["Descripcion"].lower()
        # The schema the provider parses must still be present, even on an error.
        assert "Series" in payload
        assert "SeriesInfos" in payload

    def test_a_real_credentials_error_is_raised_as_such(self) -> None:
        """End to end against the live service, using deliberately wrong credentials."""
        provider = BancoCentralProvider(user="not-a-user", password="not-a-password")
        with pytest.raises(MissingCredentialsError):
            provider.fetch_bars(
                "F013.IPSA.X",
                Timeframe.D1,
                start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end=datetime(2024, 2, 1, tzinfo=timezone.utc),
            )
