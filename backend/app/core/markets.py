"""Market definitions.

A market bundles everything that is a property of *where* an instrument trades
rather than of the instrument itself: currency, exchange, timezone, session
hours and the benchmark it should be measured against.

Adding a third market means appending one ``Market`` entry to ``MARKETS`` plus a
cost block in ``.env`` -- no other module hard-codes "USA" or "CHILE".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

__all__ = [
    "Market",
    "MARKETS",
    "MARKET_USA",
    "MARKET_CHILE",
    "get_market",
    "all_market_codes",
]


@dataclass(frozen=True, slots=True)
class Market:
    """Static description of a trading venue."""

    code: str
    """Canonical internal code, e.g. ``"USA"``. Upper-case, no spaces."""

    name: str
    """Human-readable name for display."""

    currency: str
    """ISO-4217 code of the currency positions settle in."""

    exchange: str
    """Primary exchange label, used for display and provider hints."""

    timezone: str
    """IANA timezone name of the exchange's local session."""

    session_open: time
    """Regular-session open, in exchange-local time."""

    session_close: time
    """Regular-session close, in exchange-local time."""

    benchmark_symbol: str
    """Canonical symbol of the index this market is benchmarked against."""

    flag: str
    """Emoji used in dashboards and CLI output."""

    trading_days: tuple[int, ...] = (0, 1, 2, 3, 4)
    """Weekdays the venue trades, Monday=0 (``datetime.weekday()`` convention)."""

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def trading_hours(self) -> str:
        """Session as a display string, e.g. ``"09:30-16:00 America/New_York"``."""
        return (
            f"{self.session_open.strftime('%H:%M')}"
            f"-{self.session_close.strftime('%H:%M')} {self.timezone}"
        )

    def is_trading_day(self, day: date) -> bool:
        """Whether ``day`` falls on a regular trading weekday.

        This checks the weekday only. Exchange holidays are *not* modelled --
        there is no free, reliable holiday calendar for the Bolsa de Santiago
        that ships with this project. Downstream code must therefore treat a
        missing bar as "no session or no data" rather than as a data gap; see
        ``app.data.engine.DataEngine.detect_gaps``.
        """
        return day.weekday() in self.trading_days

    def is_session_open(self, moment: datetime) -> bool:
        """Whether ``moment`` falls inside a regular session.

        A naive ``moment`` is interpreted as exchange-local time; an aware one is
        converted first.
        """
        local = (
            moment.replace(tzinfo=self.tzinfo)
            if moment.tzinfo is None
            else moment.astimezone(self.tzinfo)
        )
        if not self.is_trading_day(local.date()):
            return False
        return self.session_open <= local.time() <= self.session_close


MARKET_USA = Market(
    code="USA",
    name="United States",
    currency="USD",
    exchange="NYSE/NASDAQ",
    timezone="America/New_York",
    session_open=time(9, 30),
    session_close=time(16, 0),
    benchmark_symbol="SPY",
    flag="\U0001F1FA\U0001F1F8",  # US flag
)

MARKET_CHILE = Market(
    code="CHILE",
    name="Chile",
    currency="CLP",
    exchange="BCS",  # Bolsa de Comercio de Santiago
    timezone="America/Santiago",
    session_open=time(9, 30),
    session_close=time(16, 0),
    benchmark_symbol="IPSA",
    flag="\U0001F1E8\U0001F1F1",  # Chile flag
)

MARKETS: dict[str, Market] = {
    MARKET_USA.code: MARKET_USA,
    MARKET_CHILE.code: MARKET_CHILE,
}


def get_market(code: str) -> Market:
    """Look up a market by code, case-insensitively."""
    key = code.strip().upper()
    try:
        return MARKETS[key]
    except KeyError as exc:
        raise KeyError(
            f"Unknown market {code!r}. Known markets: {sorted(MARKETS)}"
        ) from exc


def all_market_codes() -> list[str]:
    return sorted(MARKETS)
