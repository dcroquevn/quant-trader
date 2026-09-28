"""Shared test fixtures.

No test in this suite touches the network by default. Provider behaviour is
exercised through :class:`FakeProvider`, and the two tests that do call Yahoo are
marked ``network`` and deselected in the default run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.markets import MARKET_CHILE, MARKET_USA
from app.core.universe import AssetSpec
from app.data.provider import (
    DataProvider,
    ProviderCapabilities,
    Timeframe,
    TimeframeLimit,
)
from app.database.base import create_db_engine, init_database

# --------------------------------------------------------------------------- #
# Synthetic bars
# --------------------------------------------------------------------------- #

BARS_IN_FIXTURE = 800
"""Comfortably above MIN_BARS_FOR_FULL_FEATURES (252) so every indicator warms up."""


def make_bars(
    n: int = BARS_IN_FIXTURE,
    *,
    seed: int = 42,
    start_price: float = 100.0,
    start: str = "2020-01-01",
    annual_drift: float = 0.08,
    annual_vol: float = 0.25,
) -> pd.DataFrame:
    """Deterministic synthetic daily OHLCV bars on a business-day index.

    A geometric random walk, so prices stay positive and returns are
    scale-invariant. The OHLC relationships are constructed to be internally
    consistent (``low <= min(open, close) <= max(open, close) <= high``), because
    several code paths validate exactly that and a fixture that violates it would
    make those checks untestable.

    Seeded, so a failure is reproducible.
    """
    rng = np.random.default_rng(seed)
    index = pd.bdate_range(start=start, periods=n, tz="UTC", name="ts")

    daily_drift = annual_drift / 252.0
    daily_vol = annual_vol / np.sqrt(252.0)
    log_returns = rng.normal(daily_drift, daily_vol, size=n)
    close = start_price * np.exp(np.cumsum(log_returns))

    # Open gaps modestly from the previous close.
    open_ = np.empty(n)
    open_[0] = start_price
    open_[1:] = close[:-1] * (1.0 + rng.normal(0.0, daily_vol * 0.3, size=n - 1))

    body_low = np.minimum(open_, close)
    body_high = np.maximum(open_, close)
    # Wicks are strictly non-negative extensions of the body.
    high = body_high * (1.0 + np.abs(rng.normal(0.0, daily_vol * 0.5, size=n)))
    low = body_low * (1.0 - np.abs(rng.normal(0.0, daily_vol * 0.5, size=n)))

    volume = rng.lognormal(mean=13.5, sigma=0.6, size=n).round()

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


@pytest.fixture
def bars() -> pd.DataFrame:
    """800 synthetic daily bars, enough for every indicator to warm up."""
    return make_bars()


@pytest.fixture
def short_bars() -> pd.DataFrame:
    """30 bars -- too few for the 200- and 252-bar indicators."""
    return make_bars(30, seed=7)


@pytest.fixture
def flat_bars() -> pd.DataFrame:
    """Bars with zero volume and an unchanging price.

    The pathological case that thinly traded Chilean names actually produce, and
    which makes every ratio-based indicator divide by zero.
    """
    index = pd.bdate_range("2023-01-02", periods=60, tz="UTC", name="ts")
    return pd.DataFrame(
        {
            "open": 1000.0,
            "high": 1000.0,
            "low": 1000.0,
            "close": 1000.0,
            "volume": 0.0,
        },
        index=index,
    )


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


@pytest.fixture
def engine():
    """A fresh in-memory SQLite database with the full schema.

    StaticPool (configured in ``create_db_engine``) keeps one connection alive so
    the schema survives between sessions in the same test.
    """
    eng = create_db_engine("sqlite+pysqlite:///:memory:")
    init_database(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Asset specs
# --------------------------------------------------------------------------- #


@pytest.fixture
def usa_spec() -> AssetSpec:
    return AssetSpec(
        symbol="TESTUS",
        name="Test US Instrument",
        market=MARKET_USA.code,
        sector="Technology",
        provider_symbols={"fake": ("TESTUS",), "yfinance": ("TESTUS",)},
    )


@pytest.fixture
def chile_spec() -> AssetSpec:
    return AssetSpec(
        symbol="TESTCL",
        name="Test Chilean Instrument",
        market=MARKET_CHILE.code,
        sector="Financials",
        provider_symbols={"fake": ("TESTCL",), "yfinance": ("TESTCL.SN",)},
    )


# --------------------------------------------------------------------------- #
# Fake provider
# --------------------------------------------------------------------------- #


class FakeProvider(DataProvider):
    """In-memory provider for testing the engine without a network.

    Serves a pre-built frame per ticker and records every request, so tests can
    assert *what was asked for* -- which is how incremental-refresh behaviour is
    verified.
    """

    def __init__(
        self,
        frames: dict[str, pd.DataFrame] | None = None,
        *,
        markets: frozenset[str] = frozenset({"USA", "CHILE"}),
        fail_with: Exception | None = None,
    ) -> None:
        self.frames = frames or {}
        self.requests: list[tuple[str, str, datetime, datetime]] = []
        self.fail_with = fail_with
        self._markets = markets

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="fake",
            markets=self._markets,
            timeframes=(
                TimeframeLimit(Timeframe.D1, None, ""),
                TimeframeLimit(Timeframe.M5, 60, "60-day cap, same as Yahoo."),
            ),
            provides_adjusted_prices=True,
            requires_api_key=False,
            cost="free (test double)",
        )

    def _fetch_raw(
        self,
        provider_symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        self.requests.append((provider_symbol, timeframe.value, start, end))
        if self.fail_with is not None:
            raise self.fail_with
        frame = self.frames.get(provider_symbol)
        if frame is None:
            return pd.DataFrame()
        window = frame.loc[(frame.index >= start) & (frame.index <= end)]
        out = window.copy()
        out.columns = [c.title() if c != "adj_close" else "Adj Close" for c in out.columns]
        return out

    def resolve_symbol(self, candidates: tuple[str, ...], canonical: str) -> str:
        """Resolve against the configured frames, independent of today's date.

        The base implementation probes a trailing one-year window, which is right
        for a live vendor but makes fixture resolution depend on when the suite
        runs -- the synthetic bars are dated 2020-2023. A test double should answer
        the question it exists to answer: is this ticker known?
        """
        from app.core.exceptions import SymbolNotFoundError

        if self.fail_with is not None:
            raise self.fail_with

        now = datetime.now(timezone.utc)
        for candidate in candidates:
            # Recorded so tests can assert that resolution is cached and not
            # re-probed on every download.
            self.requests.append((candidate, "resolve", now, now))
            frame = self.frames.get(candidate)
            if frame is not None and not frame.empty:
                return candidate
        raise SymbolNotFoundError(canonical, self.name, candidates)


@pytest.fixture
def fake_provider(bars) -> FakeProvider:
    frame = bars.copy()
    frame["adj_close"] = frame["close"]
    return FakeProvider({"TESTUS": frame, "TESTCL": frame})


@pytest.fixture
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def recent_bars() -> pd.DataFrame:
    """Bars ending today, so freshness checks see a current series."""
    today = datetime.now(timezone.utc).date()
    index = pd.bdate_range(end=pd.Timestamp(today, tz="UTC"), periods=400, tz="UTC", name="ts")
    frame = make_bars(len(index), seed=11)
    frame.index = index
    return frame


@pytest.fixture
def stale_bars() -> pd.DataFrame:
    """Bars that stop 60 days ago -- well beyond the staleness tolerance."""
    end = datetime.now(timezone.utc).date() - timedelta(days=60)
    index = pd.bdate_range(end=pd.Timestamp(end, tz="UTC"), periods=400, tz="UTC", name="ts")
    frame = make_bars(len(index), seed=13)
    frame.index = index
    return frame
