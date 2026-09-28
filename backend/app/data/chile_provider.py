"""Free data provider for the Bolsa de Comercio de Santiago.

Cost: free, no API key.

Why this exists as its own class
--------------------------------
Mechanically it is Yahoo Finance with a ``.SN`` suffix, and it inherits that
transport. What it adds is the Chilean market's quirks, which are different
enough from US large caps that ignoring them produces confidently wrong
backtests:

**Liquidity is an order of magnitude thinner.** Several names in the universe go
whole sessions without a meaningful print. A zero-volume bar is not corrupt data
-- it is a real session where nothing traded -- but a strategy that "buys" on
such a bar is trading against a price nobody was offering. This provider flags
those bars instead of dropping them, so the backtester can refuse to fill there.

**Prices are whole pesos with large magnitudes.** CLP quotes run into the tens of
thousands, so a "1.0 move" means something entirely different from a USD tick.
Anything comparing raw price levels across markets is a bug.

**No free index.** The IPSA is a licensed S&P Dow Jones product. Six Yahoo
spellings were probed on 2026-09-27 and all returned zero rows, so this provider
reports the index as unavailable rather than substituting something and calling
it IPSA. See ``app.core.universe.BENCHMARK_AVAILABILITY``.

**No free execution venue.** Nothing here routes orders. Chilean paper trading
uses the internal paper broker, because no free Chilean broker API was found.

Adding a genuinely local source later
-------------------------------------
The Comisión para el Mercado Financiero (CMF) and the exchange itself publish
some data without charge but with no documented, stable API. If one is wired up
later it should subclass :class:`~app.data.provider.DataProvider` directly and be
registered alongside this one, so the two can be compared rather than one
silently replacing the other.
"""

from __future__ import annotations

import pandas as pd

from app.core.logging import get_logger
from app.data.provider import ProviderCapabilities, Timeframe, TimeframeLimit
from app.data.yfinance_provider import YFinanceProvider

logger = get_logger(__name__)

__all__ = ["ChileDataProvider", "SANTIAGO_SUFFIX", "flag_illiquid_bars"]

SANTIAGO_SUFFIX = ".SN"
"""Yahoo's suffix for Bolsa de Santiago listings. Verified 2026-09-27."""


def flag_illiquid_bars(
    frame: pd.DataFrame,
    *,
    min_volume: float = 1.0,
    flat_bar_tolerance: float = 0.0,
) -> pd.DataFrame:
    """Add an ``is_illiquid`` column marking bars that should not be traded on.

    A bar is flagged when either:

    * **Volume is below ``min_volume``** -- nothing traded, so any fill is fiction.
    * **The bar is perfectly flat** (``open == high == low == close`` within
      ``flat_bar_tolerance``) *and* volume is zero -- the classic stale-quote
      carry-forward, where the vendor repeats yesterday's close for a session
      that had no activity.

    The bars are kept, not dropped. Removing them would close the gap in the
    index and make the series look continuous when it is not; the backtester
    needs to see that a day existed and was untradeable.
    """
    out = frame.copy()
    volume = out["volume"] if "volume" in out.columns else pd.Series(0.0, index=out.index)

    span = (out["high"] - out["low"]).abs()
    is_flat = span <= flat_bar_tolerance
    no_volume = volume.fillna(0.0) < min_volume

    out["is_illiquid"] = (no_volume | (is_flat & (volume.fillna(0.0) <= 0))).astype(bool)
    return out


class ChileDataProvider(YFinanceProvider):
    """Bolsa de Santiago bars, sourced free from Yahoo Finance.

    Accepts either the canonical nemotécnico (``SQM-B``) or the already-suffixed
    vendor ticker (``SQM-B.SN``); :meth:`to_provider_symbol` normalises both.
    """

    _CAPABILITIES = ProviderCapabilities(
        name="chile-yfinance",
        markets=frozenset({"CHILE"}),
        timeframes=(
            TimeframeLimit(Timeframe.D1, None, "Full available history."),
            TimeframeLimit(
                Timeframe.H1,
                730,
                "Hourly bars exist but are sparse for thinly traded Chilean names.",
            ),
        ),
        provides_adjusted_prices=True,
        requires_api_key=False,
        cost="Free. No API key, no account.",
        notes=(
            "Yahoo Finance with the .SN suffix; all 18 universe symbols verified "
            "2026-09-27. Daily bars only in practice -- intraday Chilean data is "
            "too sparse to be usable. The IPSA index is NOT available free of "
            "charge. No free order-routing API exists, so execution is simulated "
            "by the internal paper broker."
        ),
    )

    # Minute bars are deliberately not offered. Yahoo technically returns them for
    # .SN tickers, but for names that print a handful of times a session the
    # result is mostly carried-forward quotes: real-looking, and not real.

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._CAPABILITIES

    @property
    def symbol_namespace(self) -> str:
        """Reads the ``"yfinance"`` mappings -- same vendor, same ticker vocabulary."""
        return "yfinance"

    @staticmethod
    def to_provider_symbol(symbol: str) -> str:
        """Append ``.SN`` unless it is already there."""
        clean = symbol.strip().upper()
        if clean.endswith(SANTIAGO_SUFFIX):
            return clean
        return f"{clean}{SANTIAGO_SUFFIX}"

    def _fetch_raw(
        self,
        provider_symbol: str,
        timeframe: Timeframe,
        start,  # noqa: ANN001 -- signature fixed by the base class
        end,  # noqa: ANN001
    ) -> pd.DataFrame:
        return super()._fetch_raw(
            self.to_provider_symbol(provider_symbol), timeframe, start, end
        )

    def resolve_symbol(self, candidates: tuple[str, ...], canonical: str) -> str:
        """Resolve, normalising every candidate to the ``.SN`` form first."""
        suffixed = tuple(dict.fromkeys(self.to_provider_symbol(c) for c in candidates))
        return super().resolve_symbol(suffixed, canonical)

    def fetch_bars(self, *args, **kwargs) -> pd.DataFrame:  # type: ignore[override]
        """Fetch bars and annotate untradeable sessions.

        The extra ``is_illiquid`` column is the only place this provider's output
        differs in shape from the US one. Downstream code treats a missing column
        as "no liquidity information", never as "all bars are liquid".
        """
        frame = super().fetch_bars(*args, **kwargs)
        flagged = flag_illiquid_bars(frame)

        n_illiquid = int(flagged["is_illiquid"].sum())
        if n_illiquid:
            logger.info(
                "%d of %d Chilean bars flagged illiquid (%.1f%%): zero volume or "
                "stale flat quote; these are kept but must not be filled on",
                n_illiquid,
                len(flagged),
                100.0 * n_illiquid / len(flagged),
            )
        return flagged
