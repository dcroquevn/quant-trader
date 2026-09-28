"""Command-line interface: ``python -m app <command>``.

Phase 1 implements the data commands. The commands belonging to later phases are
registered but refuse to run, printing what they will do and which phase they
belong to. A command that silently did nothing, or printed a fabricated result,
would be worse than one that says it does not exist yet.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from app.config import ensure_directories, get_settings
from app.core.logging import get_logger, setup_logging
from app.core.markets import MARKETS, all_market_codes
from app.core.universe import (
    BENCHMARK_AVAILABILITY,
    CHILE_UNIVERSE,
    DEFAULT_UNIVERSE,
    USA_UNIVERSE,
    VERIFICATION_DATE,
    find_asset,
)
from app.data.engine import DataEngine
from app.data.provider import Timeframe
from app.data.registry import provider_cost_table
from app.database.base import init_database, session_scope
from app.indicators.registry import compute_features, latest_features

app = typer.Typer(
    name="quant-trader",
    help="Research, backtesting and paper-trading platform for US and Chilean equities.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
logger = get_logger(__name__)


PHASE_MESSAGE = (
    "[yellow]Not implemented yet.[/yellow] This command belongs to Phase {phase}: "
    "{what}\n\nPhase 1 (data foundation) is complete. Run "
    "[cyan]python -m app --help[/cyan] to see what works today."
)


def _fail_not_implemented(phase: str, what: str) -> None:
    console.print(Panel(PHASE_MESSAGE.format(phase=phase, what=what), title="Coming later"))
    raise typer.Exit(code=2)


def _day(value: object) -> str:
    """Render a date cell, tolerating SQL NULL.

    An asset with no bars makes ``min(ts)`` return NULL, which pandas surfaces as
    ``NaT`` -- and ``NaT is not None`` is True, so a plain None check passes it
    straight into strftime and raises. Every empty-coverage row would crash the
    table that exists precisely to report empty coverage.
    """
    import pandas as pd

    if value is None or pd.isna(value):
        return "-"
    return f"{pd.Timestamp(value):%Y-%m-%d}"


def _banner() -> None:
    settings = get_settings()
    mode = "PAPER" if settings.paper_trading else "?"
    console.print(
        Panel(
            "[bold cyan]QUANT TRADER[/bold cyan]  "
            f"[dim]markets:[/dim] {', '.join(all_market_codes())}  "
            f"[dim]mode:[/dim] [green]{mode}[/green]  "
            f"[dim]live trading:[/dim] [red]disabled[/red]",
            expand=False,
        )
    )


# --------------------------------------------------------------------------- #
# Phase 1 -- implemented
# --------------------------------------------------------------------------- #


@app.command("init-db")
def init_db() -> None:
    """Create the SQLite schema and register markets and the asset universe."""
    setup_logging()
    ensure_directories()
    _banner()

    settings = get_settings()
    console.print(f"[dim]database:[/dim] {settings.resolved_database_url}")

    init_database()
    with session_scope() as session:
        count = DataEngine(session).sync_universe()

    console.print(f"[green]OK[/green] schema created, {count} assets registered.")


@app.command("providers")
def providers() -> None:
    """List data providers with their capabilities and cost."""
    table = Table(title="Data providers", header_style="bold cyan")
    for column in ("provider", "markets", "timeframes", "API key", "adjusted", "cost"):
        table.add_column(column, overflow="fold")

    for row in provider_cost_table():
        table.add_row(
            row["provider"],
            row["markets"],
            row["timeframes"],
            row["api_key_required"],
            row["adjusted_prices"],
            row["cost"],
        )
    console.print(table)
    console.print(
        "[dim]Every provider above is free and needs no account. "
        "No paid data source is wired into this project.[/dim]"
    )


@app.command("universe")
def universe() -> None:
    """Show the configured universe and each market's benchmark."""
    table = Table(title="Universe", header_style="bold cyan")
    for column in ("market", "symbol", "name", "sector", "currency", "provider symbol"):
        table.add_column(column, overflow="fold")

    for spec in DEFAULT_UNIVERSE:
        candidates = spec.candidates("yfinance")
        table.add_row(
            spec.market,
            spec.symbol,
            spec.name,
            spec.sector,
            spec.currency,
            candidates[0] if candidates else "[red]none[/red]",
        )
    console.print(table)
    console.print(
        f"[dim]{len(USA_UNIVERSE)} US + {len(CHILE_UNIVERSE)} Chilean instruments. "
        f"Symbol mappings verified {VERIFICATION_DATE}.[/dim]\n"
    )

    bench = Table(title="Benchmarks", header_style="bold cyan")
    for column in ("market", "symbol", "kind", "currency", "available", "caveats"):
        bench.add_column(column, overflow="fold")
    for code, spec in BENCHMARK_AVAILABILITY.items():
        bench.add_row(
            code,
            spec.symbol,
            spec.kind,
            spec.currency,
            "yes" if spec.available else "[red]no[/red]",
            "\n".join(f"- {c}" for c in spec.caveats),
        )
    console.print(bench)


@app.command("verify-symbols")
def verify_symbols(
    market: str = typer.Option(None, "--market", "-m", help="USA or CHILE. Default: both."),
    force: bool = typer.Option(False, "--force", help="Re-probe already-resolved symbols."),
) -> None:
    """Probe each provider mapping and record which ticker actually returns data.

    This hits the network. It is the only honest way to know a mapping works --
    a free vendor returns an empty frame for an unknown ticker rather than an error.
    """
    setup_logging()
    ensure_directories()
    _banner()
    init_database()

    codes = [market.upper()] if market else all_market_codes()

    table = Table(title="Symbol resolution", header_style="bold cyan")
    for column in ("market", "symbol", "resolved", "status"):
        table.add_column(column, overflow="fold")

    resolved_count = failed_count = 0
    with session_scope() as session:
        engine = DataEngine(session)
        engine.sync_universe()

        for code in codes:
            specs = [s for s in DEFAULT_UNIVERSE if s.market == code]
            for spec in specs:
                try:
                    _, ticker = engine.resolve(spec, force=force)
                    table.add_row(code, spec.symbol, ticker, "[green]OK[/green]")
                    resolved_count += 1
                except Exception as exc:  # noqa: BLE001 -- report, never abort
                    table.add_row(code, spec.symbol, "-", f"[red]{exc}[/red]")
                    failed_count += 1

    console.print(table)
    console.print(
        f"resolved [green]{resolved_count}[/green], failed [red]{failed_count}[/red]"
    )
    if failed_count:
        raise typer.Exit(code=1)


@app.command("download-data")
def download_data(
    market: str = typer.Option(None, "--market", "-m", help="USA or CHILE. Default: both."),
    symbols: str = typer.Option(None, "--symbols", "-s", help="Comma-separated canonical symbols."),
    timeframe: str = typer.Option("1D", "--timeframe", "-t", help="1D, 1H, 15m or 5m."),
    start: str = typer.Option(None, "--start", help="YYYY-MM-DD. Ignored on an incremental run."),
    end: str = typer.Option(None, "--end", help="YYYY-MM-DD."),
    full: bool = typer.Option(False, "--full", help="Re-download everything, not just new bars."),
) -> None:
    """Download historical bars into the local database.

    Incremental by default: only new bars are fetched, plus a small overlap so a
    provisional final bar gets corrected.
    """
    setup_logging()
    ensure_directories()
    _banner()
    init_database()

    tf = Timeframe.parse(timeframe)
    symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    start_date = date.fromisoformat(start) if start else None
    end_date = date.fromisoformat(end) if end else None

    console.print(
        f"[dim]timeframe:[/dim] {tf.value}  "
        f"[dim]mode:[/dim] {'full re-download' if full else 'incremental'}"
    )

    with session_scope() as session:
        engine = DataEngine(session)
        engine.sync_universe()
        results = engine.download_universe(
            market,
            tf,
            start=start_date,
            end=end_date,
            incremental=not full,
            symbols=symbol_list,
        )

    table = Table(title=f"Download results ({tf.value})", header_style="bold cyan")
    for column in ("market", "symbol", "ticker", "status", "written", "dropped", "range"):
        table.add_column(column, overflow="fold")

    for result in results:
        span = (
            f"{result.first_bar:%Y-%m-%d} to {result.last_bar:%Y-%m-%d}"
            if result.first_bar and result.last_bar
            else "-"
        )
        colour = {"OK": "green", "UP-TO-DATE": "cyan", "FAILED": "red"}[result.status]
        table.add_row(
            result.market,
            result.symbol,
            result.provider_symbol or "-",
            f"[{colour}]{result.status}[/{colour}]",
            str(result.bars_written),
            str(result.bars_skipped) if result.bars_skipped else "",
            span,
        )
        if not result.ok:
            table.add_row("", "", "", f"[red]{result.error}[/red]", "", "", "")

    console.print(table)
    ok = sum(1 for r in results if r.ok)
    console.print(f"succeeded [green]{ok}[/green] / {len(results)}")
    if ok < len(results):
        raise typer.Exit(code=1)


@app.command("coverage")
def coverage(
    timeframe: str = typer.Option("1D", "--timeframe", "-t"),
) -> None:
    """Show what data is actually stored, per instrument."""
    setup_logging()
    init_database()
    tf = Timeframe.parse(timeframe)

    with session_scope() as session:
        frame = DataEngine(session).coverage(tf)

    if frame.empty:
        console.print("[yellow]No assets registered. Run `python -m app init-db` first.[/yellow]")
        raise typer.Exit(code=1)

    table = Table(title=f"Stored coverage ({tf.value})", header_style="bold cyan")
    for column in ("market", "symbol", "ticker", "bars", "first", "last"):
        table.add_column(column, overflow="fold")

    for _, row in frame.iterrows():
        bars = int(row["bars"])
        table.add_row(
            row["market"],
            row["symbol"],
            row["provider_symbol"] or "-",
            f"[green]{bars}[/green]" if bars else "[red]0[/red]",
            _day(row["first_bar"]),
            _day(row["last_bar"]),
        )
    console.print(table)

    total = int(frame["bars"].sum())
    empty = int((frame["bars"] == 0).sum())
    console.print(f"{total:,} bars stored across {len(frame)} assets; {empty} with no data.")


@app.command("audit")
def audit(
    market: str = typer.Option(None, "--market", "-m"),
    timeframe: str = typer.Option("1D", "--timeframe", "-t"),
) -> None:
    """Audit stored series for gaps, duplicates and staleness.

    Missing weekdays are reported as *candidates*, not confirmed gaps: this project
    has no free, reliable exchange-holiday calendar, so it cannot tell a closed
    session from a lost one. Interpretation is yours.
    """
    setup_logging()
    init_database()
    tf = Timeframe.parse(timeframe)

    with session_scope() as session:
        reports = DataEngine(session).audit_universe(market, tf)

    table = Table(title=f"Data audit ({tf.value})", header_style="bold cyan")
    for column in ("market", "symbol", "bars", "missing weekdays", "longest run", "stale"):
        table.add_column(column, overflow="fold")

    flagged = 0
    for report in reports:
        if report.has_findings or report.stored_bars == 0:
            flagged += 1
        table.add_row(
            report.market,
            report.symbol,
            str(report.stored_bars),
            str(len(report.missing_weekdays)) if report.missing_weekdays else "",
            str(report.largest_gap_sessions) if report.largest_gap_sessions else "",
            f"[red]+{report.stale_by_days}d[/red]" if report.is_stale else "",
        )
    console.print(table)
    console.print(
        f"{flagged} of {len(reports)} instruments have findings.\n"
        "[dim]Missing weekdays may be exchange holidays -- no free holiday calendar "
        "is available for the Bolsa de Santiago, so these are candidates only.[/dim]"
    )


@app.command("features")
def features(
    symbol: str = typer.Argument(..., help="Canonical symbol, e.g. AAPL or SQM-B."),
    market: str = typer.Option(None, "--market", "-m", help="Needed only if ambiguous."),
    timeframe: str = typer.Option("1D", "--timeframe", "-t"),
) -> None:
    """Show the latest computed indicator values for one instrument."""
    setup_logging()
    init_database()
    tf = Timeframe.parse(timeframe)
    spec = find_asset(symbol, market)

    with session_scope() as session:
        frame = DataEngine(session).load(spec.symbol, spec.market, tf)

    if frame.empty:
        console.print(
            f"[red]No stored bars for {spec.symbol}.[/red] "
            f"Run `python -m app download-data --symbols {spec.symbol}` first."
        )
        raise typer.Exit(code=1)

    computed = compute_features(frame)
    try:
        values = latest_features(computed, require_complete=True)
        complete = True
    except Exception:
        values = latest_features(computed, require_complete=False)
        complete = False

    as_of = computed.index[-1]
    console.print(
        Panel(
            f"[bold]{spec.symbol}[/bold] ({spec.market}, {spec.currency})  "
            f"[dim]as of[/dim] {as_of:%Y-%m-%d}  [dim]bars:[/dim] {len(frame)}",
            expand=False,
        )
    )
    if not complete:
        console.print(
            f"[yellow]Feature set incomplete: {len(frame)} bars is fewer than the 252 "
            "needed for every indicator to warm up. Missing values show as '-'.[/yellow]"
        )

    table = Table(header_style="bold cyan")
    table.add_column("feature")
    table.add_column("value", justify="right")
    for name, value in values.items():
        if value is None:
            rendered = "[dim]-[/dim]"
        elif isinstance(value, bool):
            rendered = "yes" if value else "no"
        else:
            rendered = f"{value:,.4f}"
        table.add_row(name, rendered)
    console.print(table)
    console.print(
        "[dim]These are historical measurements of past prices. They are not "
        "forecasts and carry no probability of any future outcome.[/dim]"
    )


@app.command("limitations")
def limitations() -> None:
    """Print the known limitations of the data and the system.

    Deliberately a first-class command. A user who never reads the README should
    still be one command away from knowing what these numbers cannot tell them.
    """
    from app.core.universe import DEFAULT_UNIVERSE as specs

    console.print(
        Panel(
            "[bold yellow]Known limitations[/bold yellow]\n\n"
            "[bold]1. Survivorship bias.[/bold] The universe lists companies that "
            "exist today. Firms that were delisted, went bankrupt or were acquired "
            "are absent, so any backtest over it is measured only on survivors and "
            "is optimistic by an unknown amount. Free data cannot fix this.\n\n"
            "[bold]2. No IPSA benchmark.[/bold] The Chilean index is a licensed S&P "
            "product and is not available free of charge. ECH, a USD-denominated "
            "NYSE-listed ETF, is used as a proxy; the comparison therefore includes "
            "CLP/USD currency moves the strategy never made.\n\n"
            "[bold]3. Unofficial data source.[/bold] yfinance scrapes endpoints Yahoo "
            "publishes for its own site. No SLA, no guarantee, and adjusted prices are "
            "recomputed per request, so a historical bar can change between downloads.\n\n"
            "[bold]4. No exchange-holiday calendar.[/bold] A missing bar cannot be "
            "distinguished from a closed session, so gap reports are candidates only.\n\n"
            "[bold]5. Transaction costs are placeholders.[/bold] The defaults in .env "
            "are guesses, not your broker's rates. Replace them before believing any "
            "net return.\n\n"
            "[bold]6. Restructured companies.[/bold] LATAM (Chapter 11, 2022), Itaú "
            "Chile (repeated mergers) and Mallplaza (listed 2016) have price histories "
            "that are not continuously comparable.\n\n"
            "[bold]7. Thin liquidity in Chile.[/bold] Several names print rarely. Bars "
            "with no volume are flagged, not dropped -- filling an order on one would "
            "be fiction.\n\n"
            "[bold]8. Intraday data is shallow.[/bold] 60 days for minute bars, 730 for "
            "hourly. Not enough for a serious intraday study.",
            expand=False,
        )
    )
    documented = [s.symbol for s in specs if s.notes]
    console.print(f"[dim]Instruments with specific data caveats recorded: {', '.join(documented)}[/dim]")


# --------------------------------------------------------------------------- #
# Later phases -- registered, refuse to run
# --------------------------------------------------------------------------- #


@app.command("scan")
def scan() -> None:
    """[Phase 2] Scan the universe and rank instruments by signal."""
    _fail_not_implemented("2", "the strategy engine and scanner.")


@app.command("backtest")
def backtest() -> None:
    """[Phase 2] Run a backtest with transaction costs and slippage."""
    _fail_not_implemented("2", "the event-driven backtester and performance metrics.")


@app.command("optimize")
def optimize() -> None:
    """[Phase 4] Search parameters on TRAIN only, select on VALIDATION."""
    _fail_not_implemented(
        "4", "parameter search with a train/validation/test split and leakage guards."
    )


@app.command("walk-forward")
def walk_forward() -> None:
    """[Phase 4] Rolling out-of-sample walk-forward analysis."""
    _fail_not_implemented("4", "walk-forward analysis with per-window parameter freezing.")


@app.command("project")
def project() -> None:
    """[Phase 5] Historical analogues and statistical scenarios."""
    _fail_not_implemented(
        "5", "the historical-analogue search and percentile scenarios (never forecasts)."
    )


@app.command("paper")
def paper() -> None:
    """[Phase 6] Run the paper-trading loop."""
    _fail_not_implemented("6", "paper trading via Alpaca (US) and the internal broker (Chile).")


@app.command("report")
def report() -> None:
    """[Phase 2] Generate an HTML backtest report."""
    _fail_not_implemented("2", "HTML reports including limitations and split provenance.")


@app.command("serve")
def serve(
    host: str = typer.Option(None, "--host"),
    port: int = typer.Option(None, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Run the API server that backs the dashboard."""
    import uvicorn

    settings = get_settings()
    setup_logging()
    init_database()
    uvicorn.run(
        "app.api.main:app",
        host=host or settings.api_host,
        port=port or settings.api_port,
        reload=reload,
    )


if __name__ == "__main__":
    app()
