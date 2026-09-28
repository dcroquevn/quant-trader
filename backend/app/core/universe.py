"""Tradable universe and provider symbol mapping.

Two separate ideas live here and must not be conflated:

**Canonical symbol**
    The identifier *this project* uses, e.g. ``SQM-B`` or ``BSANTANDER``. It is
    stable, it is what goes in the database, and it never changes because a data
    vendor renamed something.

**Provider symbol**
    What a specific provider calls that instrument, e.g. ``SQM-B.SN`` on
    Yahoo Finance. Each provider gets an *ordered list of candidates* rather
    than a single string, because Chilean tickers are not consistently named
    across vendors and a plausible guess is not a verified fact.

The data engine walks the candidates and records which one actually returned
data. Resolution is an empirical step, never an assumption; see
``VERIFICATION_DATE`` below for when these mappings were last confirmed against
live provider responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.core.markets import MARKET_CHILE, MARKET_USA, get_market

__all__ = [
    "AssetSpec",
    "USA_UNIVERSE",
    "CHILE_UNIVERSE",
    "BENCHMARKS",
    "DEFAULT_UNIVERSE",
    "VERIFICATION_DATE",
    "BENCHMARK_AVAILABILITY",
    "universe_for_market",
    "benchmark_for_market",
    "find_asset",
    "candidate_symbols",
]

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
    """Market code, must exist in ``app.core.markets.MARKETS``."""

    sector: str = "Unknown"
    """Coarse sector label, used by the risk engine's sector-exposure cap."""

    asset_class: str = "equity"
    """``"equity"``, ``"etf"`` or ``"index"``."""

    provider_symbols: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Provider name -> ordered candidate tickers, most likely first."""

    is_benchmark: bool = False
    """True for index/benchmark instruments, excluded from trading universes."""

    notes: str = ""
    """Caveats worth surfacing in reports (illiquidity, listing changes, ...)."""

    def __post_init__(self) -> None:
        get_market(self.market)  # raises on an unknown market code

    @property
    def currency(self) -> str:
        return get_market(self.market).currency

    def candidates(self, provider: str) -> tuple[str, ...]:
        """Candidate provider tickers, falling back to the canonical symbol."""
        return self.provider_symbols.get(provider, (self.symbol,))

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
    is_benchmark: bool = False,
    notes: str = "",
) -> AssetSpec:
    """US instrument. Yahoo Finance uses the plain ticker for US listings."""
    return AssetSpec(
        symbol=symbol,
        name=name,
        market=MARKET_USA.code,
        sector=sector,
        asset_class=asset_class,
        provider_symbols={"yfinance": (symbol,), "alpaca": (symbol,)},
        is_benchmark=is_benchmark,
        notes=notes,
    )


def _cl(
    symbol: str,
    name: str,
    sector: str,
    *candidates: str,
    asset_class: str = "equity",
    is_benchmark: bool = False,
    notes: str = "",
) -> AssetSpec:
    """Chilean instrument.

    Yahoo Finance suffixes Bolsa de Santiago listings with ``.SN`` applied to the
    local nemotécnico -- verified for all 18 names on ``VERIFICATION_DATE``.
    Extra ``candidates`` are tried in order if the primary stops resolving.

    Alpaca is intentionally absent: it does not offer Chilean equities, so no
    candidate is declared for it and any attempt to route a CHILE order through
    Alpaca fails loudly instead of silently mapping to a US ticker.
    """
    yahoo = tuple(f"{c}.SN" if not c.startswith("^") else c for c in (symbol, *candidates))
    return AssetSpec(
        symbol=symbol,
        name=name,
        market=MARKET_CHILE.code,
        sector=sector,
        asset_class=asset_class,
        provider_symbols={"yfinance": yahoo},
        is_benchmark=is_benchmark,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# United States
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Chile -- Bolsa de Comercio de Santiago
# --------------------------------------------------------------------------- #
# Canonical symbols follow the local nemotécnico. All 18 mappings below resolved
# against live Yahoo Finance responses on VERIFICATION_DATE; the alternate
# spellings that were tried and failed are recorded in
# docs/symbol_verification.md so nobody re-litigates them.

CHILE_UNIVERSE: tuple[AssetSpec, ...] = (
    _cl("CHILE", "Banco de Chile", "Financials"),
    _cl("BSANTANDER", "Banco Santander Chile", "Financials"),
    _cl("BCI", "Banco de Credito e Inversiones", "Financials"),
    _cl(
        "ITAUCL",
        "Itau Chile (ex Itau Corpbanca)",
        "Financials",
        notes=(
            "Renamed and restructured multiple times (Corpbanca -> Itau Corpbanca "
            "-> Itau Chile). History before the 2016 merger is not comparable and "
            "the vendor series may be truncated or spliced."
        ),
    ),
    _cl(
        "SQM-B",
        "Sociedad Quimica y Minera de Chile, Series B",
        "Materials",
        notes="Series B is the liquid local line; Series A trades separately.",
    ),
    _cl("CENCOSUD", "Cencosud S.A.", "Consumer Staples"),
    _cl("FALABELLA", "S.A.C.I. Falabella", "Consumer Discretionary"),
    _cl(
        "LTM",
        "LATAM Airlines Group",
        "Industrials",
        notes=(
            "Chapter 11 reorganisation completed 2022 with massive dilution and a "
            "share restructuring. Pre-2022 prices are not comparable to post-2022 "
            "prices without careful adjustment; treat long backtests as suspect."
        ),
    ),
    _cl(
        "ENELCHILE",
        "Enel Chile S.A.",
        "Utilities",
        notes=(
            "Yahoo history begins 2016-04-22, following the 2016 reorganisation that "
            "separated Enel Chile from Enel Americas. Verified 2026-09-27."
        ),
    ),
    _cl("COLBUN", "Colbun S.A.", "Utilities"),
    _cl("CAP", "CAP S.A.", "Materials"),
    _cl("COPEC", "Empresas Copec S.A.", "Energy"),
    _cl("CMPC", "Empresas CMPC S.A.", "Materials"),
    _cl("ENTEL", "Empresas Entel Chile S.A.", "Communication Services"),
    _cl("ANDINA-B", "Embotelladora Andina, Series B", "Consumer Staples"),
    _cl("CCU", "Compania Cervecerias Unidas S.A.", "Consumer Staples"),
    _cl("PARAUCO", "Parque Arauco S.A.", "Real Estate"),
    _cl(
        "MALLPLAZA",
        "Plaza S.A. (Mallplaza)",
        "Real Estate",
        notes=(
            "Yahoo history begins 2018-07-27, not at the 2016 IPO -- verified "
            "2026-09-27. Any backtest starting earlier silently runs this name on a "
            "shorter sample than the rest of the universe."
        ),
    ),
)


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
        name="S&P/CLX IPSA Index (Chile reference index)",
        market=MARKET_CHILE.code,
        sector="Broad Market",
        asset_class="index",
        provider_symbols={},  # no free provider resolves it -- see below
        is_benchmark=True,
        notes=(
            "NOT AVAILABLE from any free provider wired into this project. Six "
            "Yahoo spellings were probed on 2026-09-27 (^IPSA, IPSA.SN, ^SPIPSA, "
            "^SPCLXIPSA, ^CLX, IPSA) and all returned zero rows. The index is "
            "licensed by S&P Dow Jones and its history is a paid product. Declared "
            "here so reports can state the gap explicitly instead of quietly "
            "substituting something else."
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


DEFAULT_UNIVERSE: tuple[AssetSpec, ...] = USA_UNIVERSE + CHILE_UNIVERSE


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
        symbol="ECH",
        kind="etf_proxy",
        currency="USD",
        available=True,
        caveats=(
            "The IPSA itself is not obtainable free of charge: all six Yahoo "
            "spellings probed on 2026-09-27 returned zero rows. ECH is a "
            "substitute, not the benchmark the brief asked for.",
            "ECH is quoted in USD while the strategy trades in CLP, so the "
            "comparison silently includes CLP/USD currency moves.",
            "ECH follows NYSE sessions and US holidays; Santiago's calendar "
            "differs, so daily returns are not aligned one-for-one.",
            "ECH holds only MSCI-eligible Chilean large caps and charges a "
            "management fee, so it is not the full local market.",
        ),
        fallback="synthetic_equal_weight_universe",
    ),
}
"""Per-market benchmark, with the gap between what was asked for and what exists.

The Chile entry is the honest outcome of an empirical check, not a design choice:
no free source for the IPSA was found. Two imperfect comparisons are offered
instead, and every report states which one it used and why it is flawed.
"""


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
