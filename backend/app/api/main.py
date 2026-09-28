"""FastAPI application backing the dashboard.

Phase 1 exposes what Phase 1 actually has: markets, the universe, stored bars,
computed indicators, data coverage and the limitations list. Endpoints for
signals, backtests and portfolios arrive with the engines that produce them --
there are no placeholder endpoints returning invented numbers.

Two conventions the frontend relies on:

* Every response that carries a derived statistic also carries enough context to
  interpret it (``as_of``, ``bars_available``, ``complete``).
* ``/api/limitations`` is a real endpoint, not a documentation page, so the UI can
  surface caveats next to the numbers rather than burying them in a README.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import get_settings
from app.core.exceptions import InsufficientDataError
from app.core.markets import MARKETS
from app.core.universe import (
    BENCHMARK_AVAILABILITY,
    DEFAULT_UNIVERSE,
    VERIFICATION_DATE,
    find_asset,
)
from app.data.engine import STALE_QUOTE_RUN_LIMIT, DataEngine
from app.data.provider import Timeframe
from app.data.registry import provider_cost_table
from app.database.base import init_database, session_scope
from app.indicators.registry import compute_features, latest_features

app = FastAPI(
    title="quant-trader API",
    version=__version__,
    description=(
        "Research API for US and Chilean equities. Every figure returned describes "
        "historical behaviour; none is a forecast."
    ),
)

# The Vite dev server runs on 5173. Locked to localhost because this API is a local
# research tool and has no authentication -- it must never be exposed to a network.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    ],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_database()


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #


@app.get("/api/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "phase": "1 -- data foundation, database and indicators",
        "paper_trading": settings.paper_trading,
        "live_trading": settings.live_trading,
        "live_trading_implemented": False,
    }


@app.get("/api/markets")
def markets() -> list[dict[str, Any]]:
    result = []
    for market in MARKETS.values():
        benchmark = BENCHMARK_AVAILABILITY.get(market.code)
        result.append(
            {
                "code": market.code,
                "name": market.name,
                "flag": market.flag,
                "currency": market.currency,
                "exchange": market.exchange,
                "timezone": market.timezone,
                "trading_hours": market.trading_hours,
                "benchmark": (
                    {
                        "symbol": benchmark.symbol,
                        "kind": benchmark.kind,
                        "currency": benchmark.currency,
                        "available": benchmark.available,
                        "caveats": list(benchmark.caveats),
                    }
                    if benchmark
                    else None
                ),
            }
        )
    return result


@app.get("/api/providers")
def providers() -> dict[str, Any]:
    return {"providers": provider_cost_table(), "all_free": True}


@app.get("/api/universe")
def universe(market: str | None = Query(None)) -> dict[str, Any]:
    specs = [s for s in DEFAULT_UNIVERSE if market is None or s.market == market.upper()]
    return {
        "verification_date": VERIFICATION_DATE,
        "count": len(specs),
        "assets": [
            {
                "symbol": s.symbol,
                "name": s.name,
                "market": s.market,
                "sector": s.sector,
                "currency": s.currency,
                "asset_class": s.asset_class,
                "provider_symbol": (s.candidates("yfinance") or ("",))[0],
                "notes": s.notes,
            }
            for s in specs
        ],
    }


@app.get("/api/coverage")
def coverage(timeframe: str = Query("1D")) -> dict[str, Any]:
    tf = Timeframe.parse(timeframe)
    with session_scope() as session:
        frame = DataEngine(session).coverage(tf)

    rows = [
        {
            "symbol": row["symbol"],
            "market": row["market"],
            "provider_symbol": row["provider_symbol"],
            "bars": int(row["bars"]),
            "first_bar": _iso(row["first_bar"]),
            "last_bar": _iso(row["last_bar"]),
        }
        for _, row in frame.iterrows()
    ]
    return {
        "timeframe": tf.value,
        "total_bars": sum(r["bars"] for r in rows),
        "assets_with_data": sum(1 for r in rows if r["bars"] > 0),
        "assets_without_data": sum(1 for r in rows if r["bars"] == 0),
        "rows": rows,
    }


# --------------------------------------------------------------------------- #
# Instrument data
# --------------------------------------------------------------------------- #


@app.get("/api/assets/{symbol}/bars")
def asset_bars(
    symbol: str,
    market: str | None = Query(None),
    timeframe: str = Query("1D"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    limit: int = Query(2000, ge=1, le=20000),
) -> dict[str, Any]:
    """Stored OHLCV bars, adjusted for splits and dividends where available."""
    spec = _resolve(symbol, market)
    tf = Timeframe.parse(timeframe)

    with session_scope() as session:
        frame = DataEngine(session).load(spec.symbol, spec.market, tf, start=start, end=end)

    if frame.empty:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No stored bars for {spec.symbol} ({spec.market}, {tf.value}). "
                f"Run: python -m app download-data --symbols {spec.symbol}"
            ),
        )

    tail = frame.tail(limit)
    return {
        "symbol": spec.symbol,
        "market": spec.market,
        "currency": spec.currency,
        "timeframe": tf.value,
        "bars_available": len(frame),
        "bars_returned": len(tail),
        "adjusted": bool(tail["is_adjusted"].any()) if "is_adjusted" in tail else False,
        "source": str(tail["source"].iloc[-1]) if "source" in tail else "",
        "bars": [
            {
                "ts": ts.isoformat(),
                "open": _num(row["open"]),
                "high": _num(row["high"]),
                "low": _num(row["low"]),
                "close": _num(row["close"]),
                "volume": _num(row["volume"]),
            }
            for ts, row in tail.iterrows()
        ],
    }


@app.get("/api/assets/{symbol}/features")
def asset_features(
    symbol: str,
    market: str | None = Query(None),
    timeframe: str = Query("1D"),
    history: int = Query(0, ge=0, le=5000, description="Also return this many historical rows."),
) -> dict[str, Any]:
    """Computed indicators for one instrument.

    ``complete`` is false when the stored history is shorter than the 252 bars the
    slowest indicator needs. The incomplete values are still returned, with nulls,
    so the UI can show what it has and say what it is missing -- rather than
    presenting a partial feature set as a finished one.
    """
    spec = _resolve(symbol, market)
    tf = Timeframe.parse(timeframe)

    with session_scope() as session:
        # Features are computed on real prints only. A carried-forward tail would
        # otherwise feed invented prices into every rolling indicator, and the
        # "as of" date would claim the readings describe today.
        frame = DataEngine(session).load(
            spec.symbol, spec.market, tf, trim_carried_forward=True
        )

    dropped = int(frame.attrs.get("carried_forward_dropped", 0))

    if frame.empty:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No usable bars for {spec.symbol} ({spec.market}, {tf.value})."
                + (
                    f" All {dropped} stored bars are flat with zero volume: the vendor "
                    "carried a price forward and none of it is a real print."
                    if dropped
                    else ""
                )
            ),
        )

    computed = compute_features(frame)
    try:
        values = latest_features(computed, require_complete=True)
        complete = True
        note = ""
    except InsufficientDataError as exc:
        values = latest_features(computed, require_complete=False)
        complete = False
        note = str(exc)

    payload: dict[str, Any] = {
        "symbol": spec.symbol,
        "market": spec.market,
        "currency": spec.currency,
        "timeframe": tf.value,
        "as_of": computed.index[-1].isoformat(),
        "bars_available": len(frame),
        "carried_forward_dropped": dropped,
        "complete": complete,
        "note": note,
        "features": values,
        "disclaimer": (
            "These values measure past prices. They are not predictions and imply "
            "no probability of any future outcome."
        ),
    }

    if history:
        columns = [c for c in computed.columns if c not in {"source", "is_adjusted"}]
        tail = computed[columns].tail(history)
        payload["history"] = [
            {"ts": ts.isoformat(), **{c: _num(row[c]) for c in columns}}
            for ts, row in tail.iterrows()
        ]
    return payload


@app.get("/api/assets/{symbol}/audit")
def asset_audit(
    symbol: str,
    market: str | None = Query(None),
    timeframe: str = Query("1D"),
) -> dict[str, Any]:
    """Data-integrity audit for one instrument."""
    spec = _resolve(symbol, market)
    tf = Timeframe.parse(timeframe)

    with session_scope() as session:
        report = DataEngine(session).audit_symbol(spec, tf)

    return {
        "symbol": report.symbol,
        "market": report.market,
        "timeframe": report.timeframe,
        "stored_bars": report.stored_bars,
        "first_bar": _iso(report.first_bar),
        "last_bar": _iso(report.last_bar),
        "missing_weekdays": [d.isoformat() for d in report.missing_weekdays],
        "longest_gap_sessions": report.largest_gap_sessions,
        "duplicate_timestamps": [_iso(t) for t in report.duplicate_timestamps],
        "is_stale": report.is_stale,
        "stale_by_days": report.stale_by_days,
        "stale_quote_run": report.stale_quote_run,
        "stale_quote_run_threshold": STALE_QUOTE_RUN_LIMIT,
        "summary": report.summary(),
        "caveat": (
            "Missing weekdays may be exchange holidays. No free holiday calendar is "
            "available for the Bolsa de Santiago, so these are candidates only."
        ),
    }


@app.get("/api/limitations")
def limitations() -> dict[str, Any]:
    """Known limitations, as data the UI can render beside the numbers."""
    return {
        "limitations": [
            {
                "id": "survivorship_bias",
                "severity": "high",
                "title": "Survivorship bias",
                "detail": (
                    "The universe lists companies that exist today. Delisted, "
                    "bankrupt and acquired firms are absent, so any backtest is "
                    "measured only on survivors and is optimistic by an unknown "
                    "amount. Free data sources cannot correct this."
                ),
            },
            {
                "id": "no_ipsa",
                "severity": "high",
                "title": "No IPSA benchmark available",
                "detail": (
                    "The IPSA is a licensed S&P Dow Jones product with no free "
                    "source; six Yahoo spellings were probed and all returned "
                    "nothing. ECH, a USD-denominated NYSE ETF, is used as a proxy, "
                    "so the comparison embeds CLP/USD currency moves."
                ),
            },
            {
                "id": "unofficial_source",
                "severity": "medium",
                "title": "Unofficial data source",
                "detail": (
                    "yfinance scrapes Yahoo's own website endpoints. No SLA. "
                    "Adjusted prices are recomputed per request, so a historical "
                    "bar can change between downloads."
                ),
            },
            {
                "id": "no_holiday_calendar",
                "severity": "medium",
                "title": "No exchange-holiday calendar",
                "detail": (
                    "A missing bar cannot be distinguished from a closed session, "
                    "so gap reports list candidates rather than confirmed gaps."
                ),
            },
            {
                "id": "placeholder_costs",
                "severity": "high",
                "title": "Transaction costs are placeholders",
                "detail": (
                    "The commission, spread and slippage values in .env are guesses, "
                    "not your broker's schedule. Replace them before trusting any "
                    "net return figure."
                ),
            },
            {
                "id": "thin_liquidity_chile",
                "severity": "medium",
                "title": "Thin liquidity in Chilean names",
                "detail": (
                    "Several instruments print rarely. Zero-volume bars are flagged "
                    "rather than dropped, because filling an order on one would be "
                    "fiction."
                ),
            },
            {
                "id": "shallow_intraday",
                "severity": "low",
                "title": "Shallow intraday history",
                "detail": (
                    "Free intraday history is capped at 60 days for minute bars and "
                    "730 days for hourly -- too little for a serious intraday study."
                ),
            },
        ],
        "instrument_notes": [
            {"symbol": s.symbol, "market": s.market, "note": s.notes}
            for s in DEFAULT_UNIVERSE
            if s.notes
        ],
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _resolve(symbol: str, market: str | None):
    try:
        return find_asset(symbol, market)
    except KeyError as exc:
        # str() on a KeyError wraps the message in its own quotes, which then get
        # escaped again by the JSON encoder and reach the client as \"...\".
        detail = exc.args[0] if exc.args else f"Unknown symbol {symbol!r}"
        raise HTTPException(status_code=404, detail=str(detail)) from exc


def _num(value: Any) -> float | bool | None:
    """JSON-safe numeric conversion. NaN becomes null, never 0."""
    import pandas as pd

    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, bool):
        return bool(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    try:
        import pandas as pd

        if pd.isna(value):
            return None
        return pd.Timestamp(value).isoformat()
    except Exception:  # noqa: BLE001
        return str(value)
