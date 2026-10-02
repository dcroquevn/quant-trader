"""Tradable universe and provider symbol mapping.

Two separate ideas live here and must not be conflated:

**Canonical symbol**
    The identifier *this project* uses, e.g. ``SQM`` or ``AAXJ``. It is stable, it is
    what goes in the database, and it never changes because a data vendor renamed
    something.

**Provider symbol**
    What a specific provider calls that instrument. For a US listing the two coincide;
    the distinction earned its keep on Bolsa de Santiago tickers, where Yahoo wanted
    ``SQM-B.SN`` for the canonical ``SQM-B``. Each provider gets an *ordered list of
    candidates* rather
    than a single string, because Chilean tickers are not consistently named
    across vendors and a plausible guess is not a verified fact.

The data engine walks the candidates and records which one actually returned
data. Resolution is an empirical step, never an assumption; see
``VERIFICATION_DATE`` below for when these mappings were last confirmed against
live provider responses.

Market versus region
--------------------
``market`` is **where an instrument trades**: it determines the currency, the session hours,
the holiday calendar and the transaction-cost model. ``region`` is **what it is exposed to**:
it determines how the portfolio groups and allocates.

For a US stock these coincide. For a Chilean ADR or an emerging-Asia ETF they do not: those
trade on NYSE in dollars on the US calendar while tracking companies elsewhere. Collapsing
the two would give them a Santiago timezone and CLP pricing, and every calculation
downstream would inherit the error.

The honest consequence, stated here because it is easy to forget: a position taken in a
Chilean or Asian instrument this way is **denominated in USD**. Its return is the underlying
move *and* the currency move, and nothing in this project can separate them.

Why there are no Bolsa de Santiago tickers
------------------------------------------
There were, and they were removed. They resolved cleanly on Yahoo (18 of 18) but could not
be bought: the broker available here lists US instruments only. Their data was also the
worst in the project -- every one ended in vendor-invented flat bars, several reported no
volume on 85% of recent sessions, and the strategy lost money on them. The full record is in
``docs/chilean_data_sources.md``; Chilean exposure is now taken through US-listed ADRs and
ETFs, which are both purchasable and clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from collections.abc import Iterable
from typing import Any

from app.core.markets import MARKET_CHILE, MARKET_USA, get_market

__all__ = [
    "AssetSpec",
    "USA_UNIVERSE",
    "BENCHMARKS",
    "DEFAULT_UNIVERSE",
    "VERIFICATION_DATE",
    "TURNOVER_WINDOW",
    "LIQUIDITY_CONCERN_USD",
    "MEDIAN_TURNOVER_USD",
    "BENCHMARK_AVAILABILITY",
    "REGION_US",
    "REGION_CHILE",
    "REGION_ASIA",
    "ALL_REGIONS",
    "CHILE_EXPOSURE_UNIVERSE",
    "ASIA_UNIVERSE",
    "universe_for_region",
    "regions_summary",
    "universe_for_market",
    "benchmark_for_market",
    "benchmark_for_region",
    "REGION_BENCHMARKS",
    "regions_of",
    "find_asset",
    "candidate_symbols",
]

REGION_US = "United States"
REGION_CHILE = "Chile"
REGION_ASIA = "Emerging Asia"

ALL_REGIONS: tuple[str, ...] = (REGION_US, REGION_CHILE, REGION_ASIA)
"""Exposure groups the portfolio can allocate across.

Not markets: every instrument in all three trades on a US exchange in dollars. See the
module docstring for why the distinction is load-bearing.
"""

TURNOVER_WINDOW = "2023-09-01 to 2026-09-25"
"""The window the liquidity figures were measured over.

Three years, not the full history: the question the number answers is how large a position
could be taken *today*, and a 2016 median does not answer it.
"""

LIQUIDITY_CONCERN_USD = 20_000_000.0
"""Below this daily turnover, position size is capped by liquidity rather than by risk.

Not a market convention -- a threshold chosen here so the warning fires on the instruments
where it matters. The backtester models no market impact at any size, so the caveat is the only
thing standing between a modelled fill and an optimistic one.
"""

MEDIAN_TURNOVER_USD: dict[str, float] = {
    'SPY': 36_678_489_748,
    'NVDA': 30_528_134_943,
    'TSLA': 25_013_070_175,
    'QQQ': 21_783_409_866,
    'AAPL': 10_921_239_657,
    'MSFT': 9_298_962_875,
    'AMZN': 8_101_674_296,
    'META': 7_739_782_578,
    'AMD': 7_162_309_097,
    'IWM': 6_811_419_354,
    'GOOGL': 5_733_943_537,
    'AVGO': 5_471_223_129,
    'TSM': 2_746_970_359,
    'JPM': 2_084_741_863,
    'V': 1_878_332_386,
    'DIA': 1_580_187_907,
    'BABA': 1_366_756_873,
    'EEM': 1_240_329_835,
    'PDD': 819_614_369,
    'SE': 405_497_448,
    'JD': 312_934_505,
    'INDA': 257_044_919,
    'EWY': 255_048_505,
    'EWT': 184_992_866,
    'INFY': 184_494_450,
    'HDB': 153_504_009,
    'ASHR': 139_259_476,
    'IBN': 138_536_484,
    'MCHI': 136_631_462,
    'NTES': 110_012_176,
    'SQM': 57_487_256,
    'AAXJ': 38_797_939,
    'EWS': 11_910_076,
    'ECH': 10_510_235,
    'EIDO': 9_337_889,
    'VNM': 8_004_576,
    'BSAC': 6_959_128,
    'EWM': 6_563_187,
    'BCH': 6_362_856,
    'THD': 2_982_196,
    'ENIC': 1_786_491,
    'CCU': 1_680_206,
}
"""Median daily traded value (close x volume) over :data:`TURNOVER_WINDOW`.

Measured, not estimated, and all on the same window so the numbers can be compared against
each other. Kept as one table rather than spread across the specs so a re-measurement is one
edit and cannot leave half the universe on an old window.

A median is a poor guide to what a large order would do -- it says nothing about depth, spread
or how fast liquidity evaporates in a selloff. It is a floor on the problem, not a measure of
it.
"""


VERIFICATION_DATE = "2026-09-27"
"""Date the ``yfinance`` mappings below were last confirmed against live responses.

Re-run ``python -m app verify-symbols`` if this is stale. Free vendors delist and
rename without notice, and a mapping that worked last quarter is not evidence
that it works today.
"""


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """One instrument in the universe."""

    symbol: str
    """Canonical symbol used internally and as the database key."""

    name: str
    """Company or fund name."""

    market: str
    """Where it trades. Determines currency, session hours and cost model.

    Must exist in ``app.core.markets.MARKETS``.
    """

    region: str = "United States"
    """What it is exposed to. Determines portfolio grouping and allocation.

    Differs from ``market`` for an ADR or a country ETF: those trade in New York while
    tracking companies elsewhere. See the module docstring.
    """

    sector: str = "Unknown"
    """Coarse sector label, used by the risk engine's sector-exposure cap."""

    asset_class: str = "equity"
    """``"equity"``, ``"etf"`` or ``"index"``."""

    provider_symbols: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Provider name -> ordered candidate tickers, most likely first."""

    is_benchmark: bool = False
    """True for index/benchmark instruments, excluded from trading universes."""

    median_turnover_usd: float | None = None
    """Median daily traded value over :data:`TURNOVER_WINDOW`. Filled from
    :data:`MEDIAN_TURNOVER_USD` unless overridden.

    Recorded because turnover across this universe spans four orders of magnitude, from SPY at
    37 billion a day to CCU at under 2 million. At the thin end, position size is capped by
    liquidity rather than by risk -- a constraint the backtester does not model at all.
    """

    notes: str = ""

    notes_es: str = ""
    """The same note for the published page, which is read in Spanish.

    A second field rather than a translation at the point of display: these are statements of
    fact about an instrument, and paraphrasing "History begins 2016-04-21, after the
    reorganisation that separated Enel Chile from Enel Americas" on the fly is a good way to
    end up asserting something subtly different. The English stays because the CLI prints it."""
    """Hand-written caveats worth surfacing in reports (listing changes, overlap, ...).

    Liquidity does not belong here: it is measured, so it is generated by
    :attr:`liquidity_caveat` instead. A number in prose beside the same number in a field is
    two things that can drift apart.
    """

    def __post_init__(self) -> None:
        get_market(self.market)  # raises on an unknown market code

    @property
    def currency(self) -> str:
        return get_market(self.market).currency

    def candidates(self, provider: str) -> tuple[str, ...]:
        """Candidate provider tickers, falling back to the canonical symbol."""
        return self.provider_symbols.get(provider, (self.symbol,))


    @property
    def is_thinly_traded(self) -> bool:
        """Whether turnover is low enough that liquidity, not risk, caps the position.

        ``None`` turnover counts as *not* thin, which is the wrong-but-necessary default: an
        unmeasured instrument is unknown rather than liquid, and this returns False so a
        missing measurement does not silently label everything a problem. Anything added to
        the universe should be measured.
        """
        return (
            self.median_turnover_usd is not None
            and self.median_turnover_usd < LIQUIDITY_CONCERN_USD
        )

    @property
    def liquidity_caveat(self) -> str:
        """The illiquidity warning, generated from the measured figure.

        Empty for an instrument that does not need one. Generated rather than hand-written so
        it cannot contradict the number it is describing.
        """
        if self.median_turnover_usd is None:
            return (
                f"{self.symbol} has no turnover measurement, so nothing here says whether a "
                "position of any size could be filled at the modelled price."
            )
        if not self.is_thinly_traded:
            return ""
        millions = self.median_turnover_usd / 1_000_000
        return (
            f"{self.symbol} trades about {millions:.1f}M USD a day (median, "
            f"{TURNOVER_WINDOW}). The backtester models no market impact, so a position "
            "large enough to matter would move the price in a way no figure in this "
            "project captures. Treat modelled fills as optimistic."
        )

    def with_provider_symbol(self, provider: str, symbol: str) -> "AssetSpec":
        """Copy of this spec with ``symbol`` promoted to the sole candidate."""
        mapping = dict(self.provider_symbols)
        mapping[provider] = (symbol,)
        return replace(self, provider_symbols=mapping)


# --------------------------------------------------------------------------- #
# Helpers for declaring the universes compactly
# --------------------------------------------------------------------------- #


def _us(
    symbol: str,
    name: str,
    sector: str,
    asset_class: str = "equity",
    *,
    region: str = REGION_US,
    is_benchmark: bool = False,
    turnover: float | None = None,
    notes: str = "",
    notes_es: str = "",
) -> AssetSpec:
    # ``turnover`` is an escape hatch for something absent from MEDIAN_TURNOVER_USD; the
    # table is the normal path so every figure shares one measurement window.
    """A US-listed instrument. Yahoo uses the plain ticker for US listings.

    Every tradable instrument in this project goes through here, including the Chilean and
    Asian ones: they all trade in New York. ``region`` is what separates them.
    """
    return AssetSpec(
        symbol=symbol,
        name=name,
        market=MARKET_USA.code,
        region=region,
        sector=sector,
        asset_class=asset_class,
        provider_symbols={"yfinance": (symbol,), "alpaca": (symbol,)},
        is_benchmark=is_benchmark,
        median_turnover_usd=(
            turnover if turnover is not None else MEDIAN_TURNOVER_USD.get(symbol)
        ),
        notes=notes,
        notes_es=notes_es,
    )


# --------------------------------------------------------------------------- #
# United States
# --------------------------------------------------------------------------- #
# The home region. Survivorship bias applies here as everywhere in this project: these are
# the large caps that are large today, chosen in 2026, so a backtest starting in 2016 runs
# them over a decade they are known to have survived.

_US_ETFS: tuple[AssetSpec, ...] = (
    _us("SPY", "SPDR S&P 500 ETF Trust", "Broad Market", "etf"),
    _us("QQQ", "Invesco QQQ Trust", "Technology", "etf"),
    _us("DIA", "SPDR Dow Jones Industrial Average ETF", "Broad Market", "etf"),
    _us("IWM", "iShares Russell 2000 ETF", "Small Cap", "etf"),
)

_US_STOCKS: tuple[AssetSpec, ...] = (
    _us("AAPL", "Apple Inc.", "Technology"),
    _us("MSFT", "Microsoft Corporation", "Technology"),
    _us("NVDA", "NVIDIA Corporation", "Semiconductors"),
    _us("AMZN", "Amazon.com Inc.", "Consumer Discretionary"),
    _us("META", "Meta Platforms Inc.", "Communication Services"),
    _us("GOOGL", "Alphabet Inc. Class A", "Communication Services"),
    _us("TSLA", "Tesla Inc.", "Consumer Discretionary"),
    _us("AMD", "Advanced Micro Devices Inc.", "Semiconductors"),
    _us("JPM", "JPMorgan Chase & Co.", "Financials"),
    _us("V", "Visa Inc.", "Financials"),
    _us("AVGO", "Broadcom Inc.", "Semiconductors"),
)

USA_UNIVERSE: tuple[AssetSpec, ...] = _US_ETFS + _US_STOCKS
"""US-exposure instruments. Region and market coincide here, and only here."""


# --------------------------------------------------------------------------- #
# Chile exposure -- US-listed, because that is what can be bought
# --------------------------------------------------------------------------- #
# These replace the 18 Bolsa de Santiago tickers this project used to carry. The reason is
# not preference: no Santiago share is purchasable through a US-listed broker, so the old
# universe produced analysis nobody could act on. Their data was also the worst here --
# every one ended in vendor-invented flat bars and several reported no volume on 85% of
# recent sessions.
#
# All verified 2026-09-27: full history since 2016, zero carried-forward bars, zero
# zero-volume bars. Clean exactly where the .SN tickers were broken.
#
# Three trade-offs, all real:
#
#   1. USD-denominated, so a return mixes the Chilean move with the CLP/USD move.
#   2. Five companies rather than eighteen.
#   3. Thin. This one was a surprise and it is the serious one: measured over the three
#      years to 2026-09-25, five of the six trade under 20M USD a day, and CCU (1.7M) and
#      ENIC (1.8M) are the least liquid instruments anywhere in this project -- below every
#      Asian country ETF except Thailand. Only SQM (57M) is comfortable. The .SN tickers were
#      illiquid too, so this is not a regression, but it is not the improvement that
#      "NYSE-listed" suggests either. See MEDIAN_TURNOVER_USD and
#      AssetSpec.liquidity_caveat.

CHILE_EXPOSURE_UNIVERSE: tuple[AssetSpec, ...] = (
    _us(
        "SQM", "Sociedad Quimica y Minera de Chile (ADR)", "Materials",
        region=REGION_CHILE,
        notes=(
            "NYSE ADR for the company whose local B share this project used to track. "
            "USD-denominated: the return includes the CLP/USD move."
        ),
        notes_es="ADR en NYSE de la empresa cuya acción B local seguía antes este proyecto. Cotiza en USD: el retorno incluye el movimiento CLP/USD.",
    ),
    _us(
        "BSAC", "Banco Santander Chile (ADR)", "Financials",
        region=REGION_CHILE,
        notes="NYSE ADR. USD-denominated.",
        notes_es="ADR en NYSE. Cotiza en USD.",
    ),
    _us(
        "BCH", "Banco de Chile (ADR)", "Financials",
        region=REGION_CHILE,
        notes="NYSE ADR. USD-denominated.",
        notes_es="ADR en NYSE. Cotiza en USD.",
    ),
    _us(
        "ENIC", "Enel Chile (ADR)", "Utilities",
        region=REGION_CHILE,
        notes=(
            "NYSE ADR. History begins 2016-04-21, after the reorganisation that separated "
            "Enel Chile from Enel Americas. USD-denominated."
        ),
        notes_es="ADR en NYSE. El historial parte el 2016-04-21, después de la reorganización que separó Enel Chile de Enel Américas. Cotiza en USD.",
    ),
    _us(
        "CCU", "Compania Cervecerias Unidas (ADR)", "Consumer Staples",
        region=REGION_CHILE,
        notes="NYSE ADR. USD-denominated.",
        notes_es="ADR en NYSE. Cotiza en USD.",
    ),
    _us(
        "ECH", "iShares MSCI Chile ETF", "Broad Market", "etf",
        region=REGION_CHILE,
        notes=(
            "The whole Chilean large-cap market in one instrument, and the closest available "
            "stand-in for the IPSA -- which is a licensed S&P product with no free source. "
            "USD-denominated and charges a management fee."
        ),
        notes_es="Todo el mercado chileno de alta capitalización en un instrumento, y lo más parecido al IPSA que hay disponible: el IPSA es un producto licenciado de S&P sin fuente gratuita. Cotiza en USD y cobra comisión de administración.",
    ),
)
"""Chilean exposure that can actually be bought: five ADRs and one country ETF."""


# --------------------------------------------------------------------------- #
# Emerging Asia -- also US-listed
# --------------------------------------------------------------------------- #
# Screened on the same criteria that disqualified the .SN tickers: enough history, current
# data, no zero-volume or carried-forward bars. 30 of 31 candidates passed; the selection
# below balances country coverage against liquidity.
#
# Turnover comes from MEDIAN_TURNOVER_USD and varies by three orders of magnitude within
# this group, from EEM at 1.2 billion a day to Thailand at 3 million. The backtester does not
# model size-dependent market impact, so on the thin names a position large enough to matter
# would move the price in a way nothing here captures; AssetSpec.liquidity_caveat says so per
# instrument. Treat the low-turnover ones as research subjects rather than as positions.
#
# Country coverage was chosen over liquidity where the two conflicted -- dropping Thailand,
# Malaysia, Indonesia and Vietnam would leave emerging Asia as China, India, Korea and Taiwan,
# which is most of the market capitalisation but a much narrower question.

_ASIA_BROAD: tuple[AssetSpec, ...] = (
    _us(
        "AAXJ", "iShares MSCI All Country Asia ex Japan ETF", "Broad Market", "etf",
        region=REGION_ASIA,
        notes="The widest single instrument for this region. Excludes Japan by construction.",
        notes_es="El instrumento más amplio de esta región. Excluye Japón por construccion.",
    ),
    _us(
        "EEM", "iShares MSCI Emerging Markets ETF", "Broad Market", "etf",
        region=REGION_ASIA,
        notes=(
            "Global emerging markets, not Asia-only -- roughly three quarters Asian by "
            "weight, with Latin America and EMEA making up the rest. Included as the "
            "liquidity anchor for the region."
        ),
        notes_es="Mercados emergentes globales, no solo Asia: alrededor de tres cuartos del peso es asiático y el resto es Latinoamérica y EMEA. Está incluido como ancla de liquidez de la región.",
    ),
)

_ASIA_COUNTRIES: tuple[AssetSpec, ...] = (
    _us(
        "MCHI", "iShares MSCI China ETF", "Broad Market", "etf",
        region=REGION_ASIA,
        notes="Chinese large and mid caps, mostly Hong Kong and US listings.",
        notes_es="Empresas chinas grandes y medianas, en su mayoría listadas en Hong Kong y Estados Unidos.",
    ),
    _us(
        "ASHR", "Xtrackers Harvest CSI 300 China A-Shares ETF", "Broad Market", "etf",
        region=REGION_ASIA,
        notes=(
            "Mainland-listed A-shares, a different market from MCHI's holdings despite both "
            "being 'China'. Holding both is more correlated than diversified."
        ),
        notes_es="Acciones A listadas en China continental, un mercado distinto al de MCHI pese a que ambos son 'China'. Tener los dos correlaciona más de lo que diversifica.",
    ),
    _us(
        "INDA", "iShares MSCI India ETF", "Broad Market", "etf",
        region=REGION_ASIA,
    ),
    _us(
        "EWY", "iShares MSCI South Korea ETF", "Broad Market", "etf",
        region=REGION_ASIA,
    ),
    _us(
        "EWT", "iShares MSCI Taiwan ETF", "Broad Market", "etf",
        region=REGION_ASIA,
        notes="Heavily concentrated in semiconductors; overlaps substantially with TSM.",
        notes_es="Muy concentrado en semiconductores; se superpone bastante con TSM.",
    ),
    _us(
        "EIDO", "iShares MSCI Indonesia ETF", "Broad Market", "etf",
        region=REGION_ASIA,
    ),
    _us(
        "EWS", "iShares MSCI Singapore ETF", "Broad Market", "etf",
        region=REGION_ASIA,
        notes="Singapore is not an emerging market by most classifications.",
        notes_es="Singapur no es un mercado emergente segun la mayoría de las clasificaciones.",
    ),
    _us(
        "EWM", "iShares MSCI Malaysia ETF", "Broad Market", "etf",
        region=REGION_ASIA,
    ),
    _us(
        "THD", "iShares MSCI Thailand ETF", "Broad Market", "etf",
        region=REGION_ASIA,
    ),
    _us(
        "VNM", "VanEck Vietnam ETF", "Broad Market", "etf",
        region=REGION_ASIA,
        notes="Vietnam is a frontier rather than an emerging market.",
        notes_es="Vietnam es un mercado frontera, no emergente.",
    ),
)

_ASIA_COMPANIES: tuple[AssetSpec, ...] = (
    _us(
        "TSM", "Taiwan Semiconductor Manufacturing (ADR)", "Semiconductors",
        region=REGION_ASIA,
        notes="Overlaps heavily with EWT, which it dominates by weight.",
        notes_es="Se superpone fuertemente con EWT, al que domina por peso.",
    ),
    _us(
        "BABA", "Alibaba Group (ADR)", "Consumer Discretionary",
        region=REGION_ASIA,
    ),
    _us(
        "PDD", "PDD Holdings (ADR)", "Consumer Discretionary",
        region=REGION_ASIA,
        notes="History begins 2018; shorter sample than the rest of this group.",
        notes_es="El historial parte en 2018; muestra más corta que la del resto del grupo.",
    ),
    _us(
        "JD", "JD.com (ADR)", "Consumer Discretionary",
        region=REGION_ASIA,
    ),
    _us(
        "NTES", "NetEase (ADR)", "Communication Services",
        region=REGION_ASIA,
    ),
    _us(
        "INFY", "Infosys (ADR, India)", "Technology",
        region=REGION_ASIA,
    ),
    _us(
        "HDB", "HDFC Bank (ADR, India)", "Financials",
        region=REGION_ASIA,
    ),
    _us(
        "IBN", "ICICI Bank (ADR, India)", "Financials",
        region=REGION_ASIA,
    ),
    _us(
        "SE", "Sea Limited (ADR, Southeast Asia)", "Consumer Discretionary",
        region=REGION_ASIA,
        notes="History begins 2017 (NYSE listing).",
        notes_es="El historial parte en 2017 (listado en NYSE).",
    ),
)

ASIA_UNIVERSE: tuple[AssetSpec, ...] = _ASIA_BROAD + _ASIA_COUNTRIES + _ASIA_COMPANIES
"""Emerging-Asia exposure, all US-listed and USD-denominated.

Chinese instruments carry a risk the others do not and it is worth naming: ADRs of Chinese
companies are claims on offshore holding structures rather than direct equity, and their
listing status has been politically contingent more than once. That is not a modellable risk,
so it is not in any number here.
"""


# --------------------------------------------------------------------------- #
# Benchmarks -- measured against, never traded
# --------------------------------------------------------------------------- #

BENCHMARKS: tuple[AssetSpec, ...] = (
    _us(
        "SPY",
        "SPDR S&P 500 ETF Trust (USA benchmark)",
        "Broad Market",
        "etf",
        is_benchmark=True,
    ),
    AssetSpec(
        symbol="IPSA",
        name="S&P/CLX IPSA Index (Chilean reference index)",
        market=MARKET_CHILE.code,
        region=REGION_CHILE,
        sector="Broad Market",
        asset_class="index",
        provider_symbols={},  # no free provider resolves it
        is_benchmark=True,
        notes=(
            "NOT AVAILABLE from any free provider. Six Yahoo spellings were probed on "
            "2026-09-27 (^IPSA, IPSA.SN, ^SPIPSA, ^SPCLXIPSA, ^CLX, IPSA) and all returned "
            "zero rows; the index is a licensed S&P Dow Jones product. Kept declared so "
            "reports can state the gap rather than quietly substituting something. ECH is "
            "the stand-in actually used."
        ),
    ),
    _us(
        "ECH",
        "iShares MSCI Chile ETF (Chile benchmark proxy)",
        "Broad Market",
        "etf",
        is_benchmark=True,
        notes=(
            "USD-denominated, NYSE-listed proxy for Chilean equities. Verified "
            "available on 2026-09-27. It is NOT the IPSA: returns embed the "
            "CLP/USD exchange rate, it follows NYSE sessions and holidays rather "
            "than Santiago's, it holds only MSCI-eligible large caps, and it "
            "charges a management fee. A CLP strategy compared against ECH is "
            "partly being measured on currency moves it never took."
        ),
    ),
)


DEFAULT_UNIVERSE: tuple[AssetSpec, ...] = (
    USA_UNIVERSE + CHILE_EXPOSURE_UNIVERSE + ASIA_UNIVERSE
)
"""Everything tradable. All US-listed; ``region`` separates the exposures."""


@dataclass(frozen=True, slots=True)
class BenchmarkSpec:
    """What a market is measured against, and why that comparison is imperfect."""

    market: str
    symbol: str
    """Canonical symbol to download and compare against. Empty when unavailable."""

    kind: str
    """``"etf_total_return"``, ``"etf_proxy"``, ``"index"`` or ``"unavailable"``."""

    currency: str
    available: bool
    caveats: tuple[str, ...] = ()
    """Every reason this comparison is not apples-to-apples. Printed in reports."""

    fallback: str = ""
    """A computable alternative when ``available`` is False or badly flawed."""


BENCHMARK_AVAILABILITY: dict[str, BenchmarkSpec] = {
    "USA": BenchmarkSpec(
        market="USA",
        symbol="SPY",
        kind="etf_total_return",
        currency="USD",
        available=True,
        caveats=(
            "SPY's adjusted close reinvests dividends, so it is a total-return "
            "series -- the right comparison for a strategy that also collects "
            "dividends, and a demanding one.",
            "SPY's own expense ratio is already deducted, slightly understating "
            "the index it tracks.",
        ),
    ),
    "CHILE": BenchmarkSpec(
        market="CHILE",
        symbol="",
        kind="none",
        currency="CLP",
        available=False,
        caveats=(
            "Nothing in this project trades on the Bolsa de Santiago any more, so this "
            "market has no benchmark to offer. Chilean exposure is taken through "
            "US-listed ADRs and ETFs and is benchmarked by region instead; see "
            "REGION_BENCHMARKS.",
            "The IPSA itself is not obtainable free of charge: all six Yahoo spellings "
            "probed on 2026-09-27 returned zero rows.",
        ),
        fallback="",
    ),
}
"""Per-market benchmark. Only one market has tradable instruments, so only one is useful.

Kept keyed by market because the data engine's coverage reporting works per market. What a
backtest is measured against comes from :data:`REGION_BENCHMARKS` below.
"""


REGION_BENCHMARKS: dict[str, BenchmarkSpec] = {
    REGION_US: BenchmarkSpec(
        market=MARKET_USA.code,
        symbol="SPY",
        kind="etf_total_return",
        currency="USD",
        available=True,
        caveats=(
            "SPY's adjusted close reinvests dividends, so it is a total-return series -- "
            "the right comparison for a strategy that also collects dividends, and a "
            "demanding one.",
            "SPY's own expense ratio is already deducted, slightly understating the index "
            "it tracks.",
            "SPY is also tradable in this universe, so a strategy can hold its own "
            "benchmark. When it does, outperformance measures timing, not selection.",
        ),
    ),
    REGION_CHILE: BenchmarkSpec(
        market=MARKET_USA.code,
        symbol="ECH",
        kind="etf_proxy",
        currency="USD",
        available=True,
        caveats=(
            "The IPSA is not obtainable free of charge: all six Yahoo spellings probed on "
            "2026-09-27 returned zero rows, as it is a licensed S&P product. ECH is a "
            "substitute, not the benchmark the brief asked for.",
            "ECH holds only MSCI-eligible Chilean large caps and charges a management fee, "
            "so it is not the full local market.",
            "ECH is also tradable in this universe, so a strategy can hold its own "
            "benchmark.",
            "Both ECH and the ADRs measured against it are USD-denominated, so the "
            "comparison is at least currency-consistent -- but both carry the CLP/USD move "
            "and neither isolates the Chilean equity move from it.",
        ),
        fallback="synthetic_equal_weight_universe",
    ),
    REGION_ASIA: BenchmarkSpec(
        market=MARKET_USA.code,
        symbol="AAXJ",
        kind="etf_proxy",
        currency="USD",
        available=True,
        caveats=(
            "AAXJ is Asia ex-Japan across all market capitalisations. A strategy that "
            "traded only India, or only Chinese ADRs, is being compared against a "
            "different mix of countries, not against its own opportunity set.",
            "AAXJ includes developed Asian markets such as Korea, Taiwan, Hong Kong and "
            "Singapore, so 'emerging Asia' is a loose description of it.",
            "AAXJ charges a management fee and is USD-denominated, so it carries the same "
            "currency exposure as the instruments measured against it.",
            "AAXJ is also tradable in this universe, so a strategy can hold its own "
            "benchmark.",
        ),
        fallback="synthetic_equal_weight_universe",
    ),
}
"""What each exposure group is measured against.

Every entry is a compromise and says which one. Two of the three are proxies standing in for
an index no free source provides, and all three are instruments this project can also buy --
facts that belong in any report quoting a number derived from them.
"""


def benchmark_for_region(region: str) -> BenchmarkSpec:
    """Benchmark specification for one exposure group.

    Raises ``KeyError`` for an unknown region rather than defaulting to SPY, which would
    measure an emerging-Asia strategy against the S&P 500 and read the gap as skill.
    """
    known = {r.lower(): r for r in ALL_REGIONS}
    key = region.strip().lower()
    if key not in known:
        raise KeyError(
            f"No benchmark declared for region {region!r}. Known regions: {list(ALL_REGIONS)}"
        )
    return REGION_BENCHMARKS[known[key]]


def regions_of(symbols: Iterable[str]) -> tuple[str, ...]:
    """The exposure groups a set of symbols spans, in declaration order.

    Used to pick a benchmark: one region means a clean comparison exists, several means the
    comparison has to be labelled as a blend or declined.
    """
    found = set()
    for symbol in symbols:
        try:
            found.add(find_asset(symbol).region)
        except KeyError:
            continue
    return tuple(r for r in ALL_REGIONS if r in found)


def benchmark_for_market(market: str) -> BenchmarkSpec:
    """Benchmark specification for ``market``.

    Raises ``KeyError`` for a market with no declared benchmark rather than
    silently falling back to SPY, which would compare a Chilean strategy against
    the S&P 500 and call the currency drift alpha.
    """
    code = get_market(market).code
    try:
        return BENCHMARK_AVAILABILITY[code]
    except KeyError as exc:
        raise KeyError(
            f"No benchmark declared for market {code!r}. Add one to "
            "BENCHMARK_AVAILABILITY, including its caveats."
        ) from exc


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #


def _all_specs() -> tuple[AssetSpec, ...]:
    """Every declared spec, benchmarks included, de-duplicated by (symbol, market).

    ``SPY`` appears both as a tradable ETF and as the USA benchmark; the tradable
    entry wins so lookups do not accidentally return a benchmark-flagged spec.
    """
    seen: dict[tuple[str, str], AssetSpec] = {}
    for spec in DEFAULT_UNIVERSE + BENCHMARKS:
        seen.setdefault((spec.symbol, spec.market), spec)
    return tuple(seen.values())


def universe_for_market(market: str, *, include_benchmarks: bool = False) -> tuple[AssetSpec, ...]:
    """Tradable specs for one market code.

    Benchmarks are excluded unless asked for, so a scanner or backtest cannot
    accidentally take positions in the thing it is being measured against.
    """
    code = get_market(market).code
    specs = tuple(s for s in DEFAULT_UNIVERSE if s.market == code)
    if include_benchmarks:
        bench = tuple(
            s
            for s in BENCHMARKS
            if s.market == code and not any(s.symbol == t.symbol for t in specs)
        )
        specs = specs + bench
    return specs


def universe_for_region(region: str, *, include_benchmarks: bool = False) -> tuple[AssetSpec, ...]:
    """Tradable specs for one exposure group.

    The counterpart to :func:`universe_for_market`, and the one a portfolio allocates over:
    every instrument here trades in the same market, so grouping by market would put all of
    them in one bucket.
    """
    key = region.strip().lower()
    known = {r.lower(): r for r in ALL_REGIONS}
    if key not in known:
        raise KeyError(f"Unknown region {region!r}. Known regions: {list(ALL_REGIONS)}")

    resolved = known[key]
    specs = tuple(s for s in DEFAULT_UNIVERSE if s.region == resolved)
    if include_benchmarks:
        specs = specs + tuple(
            b for b in BENCHMARKS
            if b.region == resolved and not any(b.symbol == s.symbol for s in specs)
        )
    return specs


def regions_summary() -> list[dict[str, Any]]:
    """Instrument counts and liquidity per exposure group, for the dashboard and CLI."""
    out = []
    for region in ALL_REGIONS:
        specs = universe_for_region(region)
        turnovers = [s.median_turnover_usd for s in specs if s.median_turnover_usd]
        out.append(
            {
                "region": region,
                "n_instruments": len(specs),
                "symbols": [s.symbol for s in specs],
                "n_etfs": sum(1 for s in specs if s.asset_class == "etf"),
                "thinnest_turnover_usd": min(turnovers) if turnovers else None,
                "all_usd": all(s.currency == "USD" for s in specs),
            }
        )
    return out


def find_asset(symbol: str, market: str | None = None) -> AssetSpec:
    """Look up a spec by canonical symbol, optionally disambiguated by market."""
    key = symbol.strip().upper()
    matches = [s for s in _all_specs() if s.symbol.upper() == key]
    if market is not None:
        code = get_market(market).code
        matches = [s for s in matches if s.market == code]

    if not matches:
        raise KeyError(
            f"Symbol {symbol!r} is not in the declared universe. Add it to "
            "app/core/universe.py, or pass an explicit AssetSpec."
        )
    if len(matches) > 1:
        markets = sorted(s.market for s in matches)
        raise KeyError(
            f"Symbol {symbol!r} is ambiguous across markets {markets}; "
            "pass market= to disambiguate."
        )
    return matches[0]


def candidate_symbols(symbol: str, provider: str, market: str | None = None) -> tuple[str, ...]:
    """Ordered provider tickers to try for a canonical ``symbol``.

    Raises ``LookupError`` when the provider declares no candidates at all,
    which is how "this provider does not cover this market" surfaces.
    """
    spec = find_asset(symbol, market)
    if provider not in spec.provider_symbols:
        raise LookupError(
            f"Provider {provider!r} has no symbol mapping for {spec.symbol} "
            f"({spec.market}). This usually means the provider does not cover "
            "that market -- Alpaca, for example, does not list Chilean equities."
        )
    return spec.candidates(provider)
