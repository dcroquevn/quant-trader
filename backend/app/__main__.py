"""Command-line interface: ``python -m app <command>``.

Phase 1 implements the data commands. The commands belonging to later phases are
registered but refuse to run, printing what they will do and which phase they
belong to. A command that silently did nothing, or printed a fabricated result,
would be worse than one that says it does not exist yet.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from app.config import ensure_directories, get_settings
from app.core.exceptions import DataLeakageError, InsufficientDataError
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
from app.data.registry import provider_cost_table, provider_for_market
from app.database.base import init_database, session_scope
from app.backtesting.runner import resolve_window, run_backtest
from app.optimization.objective import PRESETS, ObjectiveWeights
from app.optimization.robustness import run_robustness_suite
from app.optimization.search import run_search, select_on_validation
from app.optimization.space import DEFAULT_TREND_MOMENTUM_SPACE
from app.optimization.store import (
    list_optimization_runs,
    list_walk_forward_runs,
    save_search,
    save_walk_forward,
)
from app.optimization.walkforward import run_walk_forward
from app.indicators.registry import compute_features, latest_features
from app.strategies.registry import available_strategies, build_strategy, strategy_catalog
from app.strategies.scanner import scan_all, scan_market

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

    rows = provider_cost_table()
    needs_account = [r["provider"] for r in rows if r["api_key_required"] == "yes"]
    defaults = {code: provider_for_market(code).name for code in all_market_codes()}

    console.print(
        "[dim]No paid data source is wired into this project: every provider above "
        "costs nothing.[/dim]"
    )
    if needs_account:
        console.print(
            f"[dim]Needs a free account: {', '.join(needs_account)}. "
            "Registration costs nothing, but it is a signup -- so none of these is a "
            "default for any market, and the system works with an empty .env.[/dim]"
        )
    console.print(
        "[dim]Defaults: "
        + "  ".join(f"{code}={name}" for code, name in sorted(defaults.items()))
        + "[/dim]"
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
        frame = DataEngine(session).load(
            spec.symbol, spec.market, tf, trim_carried_forward=True
        )

    dropped = int(frame.attrs.get("carried_forward_dropped", 0))

    if frame.empty:
        console.print(
            f"[red]No usable bars for {spec.symbol}.[/red] "
            + (
                f"All {dropped} stored bars are flat with zero volume -- the vendor "
                "carried a price forward and none of it is a real print."
                if dropped
                else f"Run `python -m app download-data --symbols {spec.symbol}` first."
            )
        )
        raise typer.Exit(code=1)

    computed = compute_features(frame)
    try:
        values = latest_features(computed, require_complete=True)
        complete, note = True, ""
    except InsufficientDataError as exc:
        values = latest_features(computed, require_complete=False)
        complete, note = False, str(exc)

    as_of = computed.index[-1]
    console.print(
        Panel(
            f"[bold]{spec.symbol}[/bold] ({spec.market}, {spec.currency})  "
            f"[dim]as of[/dim] {as_of:%Y-%m-%d}  [dim]bars:[/dim] {len(frame)}",
            expand=False,
        )
    )
    if dropped:
        console.print(
            f"[yellow]Dropped {dropped} trailing bar(s) that were flat with zero "
            f"volume.[/yellow] The vendor carried the last price forward, so the "
            f"stored series ran past its last real print. Values below are as of "
            f"{as_of:%Y-%m-%d}, the last session that actually traded."
        )
    if not complete:
        console.print(f"[yellow]{note}[/yellow]")

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


@app.command("strategies")
def strategies() -> None:
    """List registered strategies and their default parameters."""
    for entry in strategy_catalog():
        console.print(
            Panel(
                f"[bold cyan]{entry['name']}[/bold cyan] v{entry['version']}\n\n"
                f"{entry['description']}",
                expand=False,
            )
        )
        table = Table(header_style="bold cyan", show_edge=False)
        table.add_column("parameter")
        table.add_column("default", justify="right")
        for key, value in entry["default_params"].items():
            table.add_row(key, str(value))
        console.print(table)


@app.command("scan")
def scan(
    market: str = typer.Option(None, "--market", "-m", help="USA, CHILE, or omit for both."),
    strategy_name: str = typer.Option("trend_momentum", "--strategy", "-s"),
    action: str = typer.Option("ALL", "--action", "-a", help="BUY, SELL, HOLD or ALL."),
    min_score: float = typer.Option(0.0, "--min-score"),
    sort: str = typer.Option("score", "--sort", help="score, rsi, relative_volume, ..."),
    limit: int = typer.Option(25, "--limit", "-n"),
    tradable_only: bool = typer.Option(False, "--tradable-only", help="Hide stale instruments."),
) -> None:
    """Scan the universe and rank instruments by the strategy's current reading."""
    setup_logging()
    init_database()
    _banner()

    strategy = build_strategy(strategy_name)

    with session_scope() as session:
        result = (
            scan_market(session, strategy, market)
            if market
            else scan_all(session, strategy)
        )

    rows = result.filtered(
        market=market, action=action, min_score=min_score, tradable_only=tradable_only
    )
    order = {r.symbol: i for i, r in enumerate(result.sorted_by(sort))}
    rows.sort(key=lambda r: order.get(r.symbol, 10**6))
    rows = rows[:limit]

    table = Table(
        title=f"Scanner -- {strategy.name} ({len(rows)} of {len(result.rows)} shown)",
        header_style="bold cyan",
    )
    for column in ("mkt", "symbol", "price", "signal", "score", "RSI", "rVol",
                   "ATR%", "20d", "R:R", "status"):
        table.add_column(column, overflow="fold")

    for row in rows:
        colour = {"BUY": "green", "SELL": "red", "HOLD": "dim"}.get(row.action, "white")
        # Several data problems can coexist, and each blocks a different thing. Showing
        # only the first would hide that a name is both out of date and missing volume.
        flags: list[str] = []
        if row.stale:
            flags.append("[red]stale[/red]")
        if row.volume_feed_degraded:
            flags.append(f"[red]novol {row.recent_zero_volume_pct:.0f}%[/red]")
        if row.carried_forward_dropped:
            flags.append(f"[yellow]-{row.carried_forward_dropped}fab[/yellow]")
        status = " ".join(flags) if flags else "[green]ok[/green]"

        table.add_row(
            row.market[:3],
            row.symbol,
            _fmt(row.price, 2),
            f"[{colour}]{row.action}[/{colour}]",
            _fmt(row.score, 2),
            _fmt(row.rsi, 1),
            _fmt(row.relative_volume, 2),
            _fmt(row.atr_pct, 2),
            _fmt(row.return_20d, 1),
            _fmt(row.risk_reward, 2),
            status,
        )
    console.print(table)

    if result.errors:
        console.print(f"[yellow]{len(result.errors)} instrument(s) could not be evaluated:[/yellow]")
        for error in result.errors[:10]:
            console.print(f"  [dim]{error['symbol']}: {error['error']}[/dim]")

    console.print(
        "[dim]Score counts how many of the strategy's conditions currently hold. "
        "It is not a probability of profit and not an expected return.[/dim]"
    )


@app.command("backtest")
def backtest(
    market: str = typer.Option("USA", "--market", "-m"),
    strategy_name: str = typer.Option("trend_momentum", "--strategy", "-s"),
    split: str = typer.Option("full", "--split", help="full, train, validation or test."),
    start: str = typer.Option(None, "--start", help="YYYY-MM-DD."),
    end: str = typer.Option(None, "--end", help="YYYY-MM-DD."),
    symbols: str = typer.Option(None, "--symbols", help="Comma-separated canonical symbols."),
    capital: float = typer.Option(None, "--capital"),
    finalising: bool = typer.Option(
        False,
        "--finalising",
        help="Required to open the TEST split. Use once, after parameters are frozen.",
    ),
) -> None:
    """Run a backtest with transaction costs, slippage and a benchmark comparison."""
    setup_logging()
    init_database()
    _banner()

    strategy = build_strategy(strategy_name)
    symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None

    try:
        window = resolve_window(
            split,
            start=date.fromisoformat(start) if start else None,
            end=date.fromisoformat(end) if end else None,
            finalising=finalising,
        )
    except DataLeakageError as exc:
        console.print(Panel(f"[red]{exc}[/red]", title="Leakage guard", expand=False))
        raise typer.Exit(code=3) from exc

    console.print(
        f"[dim]strategy:[/dim] {strategy.name}  [dim]market:[/dim] {market}  "
        f"[dim]window:[/dim] {window.describe()}"
    )

    with session_scope() as session:
        result = run_backtest(
            session,
            strategy,
            market,
            split=split,
            start=date.fromisoformat(start) if start else None,
            end=date.fromisoformat(end) if end else None,
            symbols=symbol_list,
            initial_capital=capital,
            finalising=finalising,
        )

    _print_backtest(result)


def _fmt(value, digits: int = 2) -> str:
    """Render a possibly-missing number. A null shows as a dash, never as zero."""
    if value is None:
        return "[dim]-[/dim]"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _print_backtest(result) -> None:
    """Print a backtest result: metrics, benchmark, trades and limitations."""
    metrics = result.metrics
    console.print(
        Panel(
            f"[bold]{result.config.label}[/bold]\n"
            f"[dim]split:[/dim] {result.config.split}  "
            f"[dim]period:[/dim] {result.start_date:%Y-%m-%d} to {result.end_date:%Y-%m-%d}  "
            f"[dim]universe:[/dim] {len(result.universe)} instruments\n"
            f"[dim]costs:[/dim] {result.cost_model['round_trip_pct']}% round trip "
            f"({result.cost_model['market']})",
            expand=False,
        )
    )

    table = Table(title="Performance", header_style="bold cyan")
    table.add_column("metric")
    table.add_column("strategy", justify="right")
    table.add_column("benchmark", justify="right")

    benchmark = result.benchmark_metrics or {}
    available = benchmark.get("available")

    rows = [
        ("Total return %", "total_return_pct", 2),
        ("CAGR %", "cagr_pct", 2),
        ("Volatility %", "annualised_volatility_pct", 2),
        ("Sharpe", "sharpe", 3),
        ("Sortino", "sortino", 3),
        ("Calmar", "calmar", 3),
        ("Max drawdown %", "max_drawdown_pct", 2),
        ("Exposure %", "exposure_pct", 1),
        ("Turnover %", "turnover_pct", 1),
    ]
    for label, key, digits in rows:
        table.add_row(
            label,
            _fmt(metrics.get(key), digits),
            _fmt(benchmark.get(key), digits) if available else "[dim]n/a[/dim]",
        )

    table.add_section()
    for label, key, digits in [
        ("Trades", "n_trades", 0),
        ("Win rate %", "win_rate_pct", 1),
        ("Profit factor", "profit_factor", 2),
        ("Expectancy", "expectancy", 2),
        ("Average win", "average_win", 2),
        ("Average loss", "average_loss", 2),
        ("Best trade", "best_trade", 2),
        ("Worst trade", "worst_trade", 2),
        ("Avg holding days", "average_holding_days", 1),
    ]:
        table.add_row(label, _fmt(metrics.get(key), digits), "[dim]-[/dim]")

    console.print(table)

    if not available:
        console.print(
            f"[yellow]No benchmark comparison: {benchmark.get('reason', 'unavailable')}[/yellow]"
        )
    elif benchmark.get("caveats"):
        console.print("[yellow]Benchmark caveats:[/yellow]")
        for caveat in benchmark["caveats"]:
            console.print(f"  [dim]- {caveat}[/dim]")

    if result.rejected_entries:
        console.print("[dim]Entries not taken:[/dim]")
        for reason, count in sorted(
            result.rejected_entries.items(), key=lambda kv: -kv[1]
        ):
            console.print(f"  [dim]{reason}: {count}[/dim]")

    if result.trades:
        recent = Table(title="Last 10 trades", header_style="bold cyan")
        for column in ("symbol", "entry", "exit", "days", "PnL", "PnL %", "exit reason"):
            recent.add_column(column, overflow="fold")
        for trade in result.trades[-10:]:
            colour = "green" if trade.pnl > 0 else "red"
            recent.add_row(
                trade.symbol,
                f"{trade.entry_date:%Y-%m-%d}",
                f"{trade.exit_date:%Y-%m-%d}",
                str(trade.holding_period_days),
                f"[{colour}]{trade.pnl:,.2f}[/{colour}]",
                f"[{colour}]{trade.pnl_pct:+.2f}[/{colour}]",
                trade.exit_reason,
            )
        console.print(recent)
    else:
        console.print("[yellow]No trades were taken.[/yellow]")

    console.print(Panel(
        "\n".join(f"- {item}" for item in result.limitations),
        title="[yellow]Limitations[/yellow]",
        expand=False,
    ))
    console.print(
        "[dim]These figures describe one historical sample with these cost assumptions. "
        "They are not an expectation of future results.[/dim]"
    )


@app.command("optimize")
def optimize(
    market: str = typer.Option("USA", "--market", "-m"),
    strategy_name: str = typer.Option("trend_momentum", "--strategy", "-s"),
    method: str = typer.Option("random", "--method", help="grid or random."),
    trials: int = typer.Option(40, "--trials", "-n", help="Random-search sample size."),
    seed: int = typer.Option(0, "--seed", help="Random seed, recorded for reproducibility."),
    preset: str = typer.Option("balanced", "--objective", help=f"One of {sorted(PRESETS)}."),
    symbols: str = typer.Option(None, "--symbols"),
    capital: float = typer.Option(None, "--capital"),
    validate: bool = typer.Option(
        True, "--validate/--no-validate", help="Re-run the shortlist on VALIDATION."
    ),
    top: int = typer.Option(5, "--top", help="Shortlist size for validation."),
) -> None:
    """Search parameters on TRAIN, then select among the best on VALIDATION.

    The search never sees validation or test data. There is no flag to change that.
    """
    setup_logging()
    init_database()
    _banner()

    if preset not in PRESETS:
        console.print(f"[red]Unknown objective preset {preset!r}. Choose from {sorted(PRESETS)}.[/red]")
        raise typer.Exit(code=2)

    weights = PRESETS[preset]
    space = DEFAULT_TREND_MOMENTUM_SPACE
    symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    window = resolve_window("train")

    # A backtest over 15 symbols x 6 years takes roughly 20 seconds, and the fragility
    # pass costs about two extra backtests per axis for each candidate examined. Saying so
    # up front beats a user discovering it half an hour in.
    planned = space.grid_size() if method == "grid" else min(trials, space.grid_size())
    fragility_cost = 5 * 2 * len(space.axes)
    total_runs = planned + fragility_cost + (top if validate else 0)

    console.print(
        f"[dim]strategy:[/dim] {strategy_name}  [dim]market:[/dim] {market}  "
        f"[dim]objective:[/dim] {preset}  [dim]method:[/dim] {method}\n"
        f"[dim]searching:[/dim] {window.describe()}  "
        f"[dim]space:[/dim] {space.grid_size():,} combinations\n"
        f"[dim]planned work:[/dim] ~{total_runs} backtests "
        f"({planned} trials + ~{fragility_cost} fragility probes"
        f"{f' + {top} validation runs' if validate else ''})"
    )
    if space.is_overfitting_prone:
        console.print(
            f"[yellow]The space has {space.grid_size():,} combinations. Best-of-N over a "
            "space this size owes a lot to the number of attempts; judge the winner on "
            "validation, not here.[/yellow]"
        )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Searching TRAIN", total=None)

        def tick(index: int, total: int, _trial) -> None:
            progress.update(task, total=total, completed=index)

        with session_scope() as session:
            search = run_search(
                session,
                market=market,
                strategy=strategy_name,
                space=space,
                weights=weights,
                method=method,
                n_trials=trials,
                seed=seed,
                symbols=symbol_list,
                capital=capital,
                progress=tick,
            )
            selection = (
                select_on_validation(
                    session, search, top_n=top, symbols=symbol_list, capital=capital
                )
                if validate and search.successful
                else None
            )
            run_id = None
            if search.successful:
                run_id = save_search(session, search, selection=selection).id

    _print_search(search, selection)
    if run_id is not None:
        console.print(
            f"[green]Stored as optimization run {run_id}.[/green] "
            f"[dim]View it with `python -m app runs`, or in the dashboard.[/dim]"
        )


@app.command("walk-forward")
def walk_forward(
    market: str = typer.Option("USA", "--market", "-m"),
    strategy_name: str = typer.Option("trend_momentum", "--strategy", "-s"),
    train_years: int = typer.Option(4, "--train-years"),
    test_years: int = typer.Option(1, "--test-years"),
    step_years: int = typer.Option(1, "--step-years"),
    trials: int = typer.Option(20, "--trials", "-n", help="Search size per window."),
    seed: int = typer.Option(0, "--seed"),
    preset: str = typer.Option("balanced", "--objective"),
    symbols: str = typer.Option(None, "--symbols"),
    capital: float = typer.Option(None, "--capital"),
) -> None:
    """Rolling walk-forward: optimise, freeze parameters, trade the next window, repeat.

    The most honest measurement this project produces. Every test window is out-of-sample
    with respect to the parameters that traded it.
    """
    setup_logging()
    init_database()
    _banner()

    if preset not in PRESETS:
        console.print(f"[red]Unknown objective preset {preset!r}.[/red]")
        raise typer.Exit(code=2)

    symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    console.print(
        f"[dim]strategy:[/dim] {strategy_name}  [dim]market:[/dim] {market}  "
        f"[dim]windows:[/dim] {train_years}y train / {test_years}y test, step {step_years}y  "
        f"[dim]trials per window:[/dim] {trials}"
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Walking forward", total=None)

        def tick(index: int, total: int, _window) -> None:
            progress.update(task, total=total, completed=index)

        with session_scope() as session:
            result = run_walk_forward(
                session,
                market=market,
                strategy=strategy_name,
                weights=PRESETS[preset],
                train_years=train_years,
                test_years=test_years,
                step_years=step_years,
                n_trials=trials,
                seed=seed,
                symbols=symbol_list,
                capital=capital,
                progress=tick,
            )
            run_id = save_walk_forward(session, result).id

    _print_walk_forward(result)
    console.print(
        f"[green]Stored as walk-forward run {run_id}.[/green] "
        f"[dim]View it with `python -m app runs`, or in the dashboard.[/dim]"
    )


@app.command("runs")
def runs(
    market: str = typer.Option(None, "--market", "-m"),
    limit: int = typer.Option(15, "--limit", "-n"),
) -> None:
    """List stored optimisation and walk-forward runs.

    Searches and walk-forward studies take tens of minutes, so they are computed by these
    commands and read back here and by the dashboard, rather than re-run on demand.
    """
    setup_logging()
    init_database()

    with session_scope() as session:
        searches = list_optimization_runs(session, market=market, limit=limit)
        studies = list_walk_forward_runs(session, market=market, limit=limit)

    if not searches and not studies:
        console.print(
            "[yellow]No stored runs.[/yellow] Produce one with "
            "[cyan]python -m app optimize[/cyan] or [cyan]python -m app walk-forward[/cyan]."
        )
        return

    if searches:
        table = Table(title="Optimisation runs (TRAIN search)", header_style="bold cyan")
        for column in ("id", "market", "method", "trials", "best obj", "validated", "stress", "finished"):
            table.add_column(column, overflow="fold")
        for row in searches:
            table.add_row(
                str(row["id"]),
                row["market"],
                row["method"],
                f"{row['n_trials']}" + (f" ([red]{row['n_failed']} failed[/red])" if row["n_failed"] else ""),
                _fmt(row["best_objective"], 3),
                "[green]yes[/green]" if row["has_validation"] else "[dim]no[/dim]",
                "[green]yes[/green]" if row["has_robustness"] else "[dim]no[/dim]",
                (row["finished_at"] or "")[:16].replace("T", " "),
            )
        console.print(table)

    if studies:
        table = Table(title="Walk-forward studies (out-of-sample)", header_style="bold cyan")
        for column in ("id", "market", "windows", "OOS return %", "OOS Sharpe", "OOS maxDD %", "losing", "unstable params"):
            table.add_column(column, overflow="fold")
        for row in studies:
            losing = row["n_losing_windows"]
            table.add_row(
                str(row["id"]),
                row["market"],
                f"{row['n_usable']}/{row['n_windows']}",
                _fmt(row["total_return_pct"]),
                _fmt(row["sharpe"], 2),
                _fmt(row["max_drawdown_pct"]),
                f"[red]{losing}[/red]" if losing else "0",
                ", ".join(row["unstable_parameters"]) or "[dim]none[/dim]",
            )
        console.print(table)


@app.command("robustness")
def robustness(
    market: str = typer.Option("USA", "--market", "-m"),
    strategy_name: str = typer.Option("trend_momentum", "--strategy", "-s"),
    symbols: str = typer.Option(None, "--symbols"),
    capital: float = typer.Option(None, "--capital"),
    skip_parameters: bool = typer.Option(
        False, "--skip-parameters", help="Skip the neighbour sweep, which is the slow part."
    ),
) -> None:
    """Stress-test a configuration: parameters, costs, delay, sequence and concentration."""
    setup_logging()
    init_database()
    _banner()

    symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None
    console.print(
        f"[dim]strategy:[/dim] {strategy_name}  [dim]market:[/dim] {market}  "
        f"[dim]window:[/dim] {resolve_window('train').describe()}"
    )

    with session_scope() as session:
        report = run_robustness_suite(
            session,
            market=market,
            strategy=strategy_name,
            symbols=symbol_list,
            capital=capital,
            include_parameter_sensitivity=not skip_parameters,
        )

    _print_robustness(report)


# --------------------------------------------------------------------------- #
# Phase 4 printing
# --------------------------------------------------------------------------- #


def _print_search(search, selection) -> None:
    for warning in search.warnings:
        console.print(f"[yellow]{warning}[/yellow]")

    if not search.successful:
        console.print("[red]No trial produced a usable result.[/red]")
        raise typer.Exit(code=1)

    table = Table(
        title=f"Top configurations on TRAIN ({len(search.successful)}/{len(search.trials)} usable)",
        header_style="bold cyan",
    )
    for column in ("#", "objective", "return %", "Sharpe", "maxDD %", "trades", "fragility", "params"):
        table.add_column(column, overflow="fold")

    for rank, trial in enumerate(search.ranked(10), start=1):
        fragility = trial.score.fragility
        table.add_row(
            str(rank),
            _fmt(trial.score.value, 3),
            _fmt(trial.metrics.get("total_return_pct")),
            _fmt(trial.metrics.get("sharpe"), 2),
            _fmt(trial.metrics.get("max_drawdown_pct")),
            str(trial.metrics.get("n_trades", 0)),
            "[dim]-[/dim]" if fragility is None else
            (f"[red]{fragility:.2f}[/red]" if fragility > 0.5 else f"{fragility:.2f}"),
            ", ".join(f"{k}={v}" for k, v in sorted(trial.params.items())),
        )
    console.print(table)

    stability = search.parameter_stability()
    if stability.get("available"):
        stab = Table(title="Parameter stability across the top decile", header_style="bold cyan")
        for column in ("parameter", "distinct", "concentration", "modal", "range"):
            stab.add_column(column)
        for name, entry in sorted(stability["parameters"].items()):
            span = (
                f"{entry['min']} .. {entry['max']}"
                if "min" in entry
                else "[dim]-[/dim]"
            )
            concentration = entry["concentration"]
            stab.add_row(
                name,
                str(entry["distinct_values"]),
                f"[red]{concentration:.0%}[/red]" if concentration < 0.4 else f"{concentration:.0%}",
                str(entry["modal_value"]),
                span,
            )
        console.print(stab)
        if stability.get("note"):
            console.print(f"[yellow]{stability['note']}[/yellow]")

    if selection is None:
        console.print(
            "[yellow]Validation was skipped, so the winner above is the most overfitted "
            "candidate by construction -- it was chosen on the data it was fitted to.[/yellow]"
        )
        return

    val = Table(title="Shortlist re-run on VALIDATION", header_style="bold cyan")
    for column in ("#", "train obj", "valid obj", "degradation", "valid return %", "valid Sharpe", "trades"):
        val.add_column(column, overflow="fold")

    for rank, candidate in enumerate(selection["candidates"], start=1):
        validation = candidate.get("validation")
        degradation = candidate.get("degradation")
        val.add_row(
            str(rank),
            _fmt(candidate["train"]["objective"], 3),
            "[red]failed[/red]" if validation is None else _fmt(validation["objective"], 3),
            "[dim]-[/dim]" if degradation is None else
            (f"[red]{degradation:+.3f}[/red]" if degradation > 1.0 else f"{degradation:+.3f}"),
            "[dim]-[/dim]" if validation is None else _fmt(validation.get("total_return_pct")),
            "[dim]-[/dim]" if validation is None else _fmt(validation.get("sharpe"), 2),
            "[dim]-[/dim]" if validation is None else str(validation.get("n_trades", 0)),
        )
    console.print(val)

    recommended = selection.get("recommended")
    if recommended:
        console.print(
            Panel(
                "[bold]Recommended by validation[/bold]\n\n"
                + "\n".join(f"  {k} = {v}" for k, v in sorted(recommended["params"].items()))
                + f"\n\nvalidation objective {recommended['validation']['objective']:.3f}"
                + f"  ·  return {_fmt(recommended['validation'].get('total_return_pct'))}%"
                + f"  ·  Sharpe {_fmt(recommended['validation'].get('sharpe'), 2)}",
                expand=False,
            )
        )
    else:
        console.print("[red]No candidate produced a usable validation result.[/red]")

    for note in selection["notes"]:
        console.print(f"[dim]- {note}[/dim]")


def _print_walk_forward(result) -> None:
    for warning in result.warnings:
        console.print(f"[yellow]{warning}[/yellow]")

    table = Table(
        title=f"Walk-forward windows ({len(result.successful)}/{len(result.windows)} usable)",
        header_style="bold cyan",
    )
    for column in ("#", "test period", "OOS return %", "OOS Sharpe", "OOS maxDD %", "trades", "frozen params"):
        table.add_column(column, overflow="fold")

    for window_result in result.windows:
        window = window_result.window
        if not window_result.ok:
            table.add_row(
                str(window.index),
                f"{window.test_start} .. {window.test_end}",
                f"[red]{window_result.error[:40]}[/red]", "", "", "", "",
            )
            continue
        test = window_result.test_metrics
        ret = test.get("total_return_pct")
        table.add_row(
            str(window.index),
            f"{window.test_start} .. {window.test_end}",
            ("[green]" if (ret or 0) > 0 else "[red]") + _fmt(ret) + ("[/green]" if (ret or 0) > 0 else "[/red]"),
            _fmt(test.get("sharpe"), 2),
            _fmt(test.get("max_drawdown_pct")),
            str(test.get("n_trades", 0)),
            ", ".join(f"{k}={v}" for k, v in sorted(window_result.chosen_params.items())),
        )
    console.print(table)

    aggregate = result.aggregate_metrics
    if aggregate:
        console.print(
            Panel(
                "[bold]Stitched out-of-sample result[/bold]  "
                "[dim](the only honest headline here)[/dim]\n\n"
                f"  total return   {_fmt(aggregate.get('total_return_pct'))}%\n"
                f"  CAGR           {_fmt(aggregate.get('cagr_pct'))}%\n"
                f"  Sharpe         {_fmt(aggregate.get('sharpe'), 3)}\n"
                f"  Sortino        {_fmt(aggregate.get('sortino'), 3)}\n"
                f"  max drawdown   {_fmt(aggregate.get('max_drawdown_pct'))}%\n"
                f"  volatility     {_fmt(aggregate.get('annualised_volatility_pct'))}%\n"
                f"  trades         {aggregate.get('n_trades', 0)}  across "
                f"{aggregate.get('n_windows', 0)} windows",
                expand=False,
            )
        )

    stability = result.parameter_stability()
    if stability.get("available"):
        stab = Table(title="Parameter choices across windows", header_style="bold cyan")
        stab.add_column("parameter")
        stab.add_column("per window", overflow="fold")
        stab.add_column("consistency")
        for name, entry in sorted(stability["parameters"].items()):
            concentration = entry["concentration"]
            stab.add_row(
                name,
                ", ".join(str(v) for v in entry["values_by_window"]),
                f"[red]{concentration:.0%}[/red]" if concentration < 0.5 else f"{concentration:.0%}",
            )
        console.print(stab)
        if stability.get("note"):
            console.print(f"[yellow]{stability['note']}[/yellow]")

    console.print(f"[dim]{result.to_dict()['interpretation']}[/dim]")


def _print_robustness(report) -> None:
    console.print(
        Panel(
            f"[bold]{report.strategy} on {report.market}[/bold]\n\n"
            f"baseline: return {_fmt(report.baseline.get('total_return_pct'))}%  ·  "
            f"Sharpe {_fmt(report.baseline.get('sharpe'), 2)}  ·  "
            f"maxDD {_fmt(report.baseline.get('max_drawdown_pct'))}%  ·  "
            f"{report.baseline.get('n_trades', 0)} trades",
            expand=False,
        )
    )

    for name, check in report.checks.items():
        label = name.replace("_", " ").title()
        if not check.get("available"):
            console.print(f"[dim]{label}: not available -- {check.get('reason')}[/dim]")
            continue

        verdict = check.get("verdict", "")
        colour = "red" if verdict.startswith("FRAGILE") else "green"
        console.print(f"\n[bold]{label}[/bold]")
        console.print(f"  [{colour}]{verdict}[/{colour}]")

        if name == "parameter_sensitivity":
            console.print(
                f"  [dim]{check['n_neighbours']} neighbours · "
                f"median return {_fmt(check['neighbour_return_median'])}% · "
                f"range {_fmt(check['neighbour_return_min'])}% .. "
                f"{_fmt(check['neighbour_return_max'])}%[/dim]"
            )
        elif name.endswith("sensitivity") and "rows" in check:
            console.print(
                f"  [dim]breakeven at {check.get('breakeven_multiplier') or 'beyond tested range'}"
                f"x the assumed cost (baseline round trip "
                f"{check.get('baseline_round_trip_pct')}%)[/dim]"
            )
        elif name == "monte_carlo_reshuffle":
            simulated = check["simulated_drawdown"]
            console.print(
                f"  [dim]actual maxDD {check['actual_max_drawdown_pct']}% sits at the "
                f"{check['actual_percentile']}th percentile; reorderings ranged "
                f"{simulated['worst']}% .. {simulated['p95']}%[/dim]"
            )
        elif name == "random_trade_removal":
            for row in check["rows"]:
                console.print(
                    f"  [dim]remove {row['fraction_removed']:.0%}: median "
                    f"{row['median_return_pct']}%, unprofitable "
                    f"{row['share_unprofitable']:.0%} of the time[/dim]"
                )

    console.print()
    overall = report.verdicts[0] if report.verdicts else ""
    console.print(
        Panel(
            overall,
            title="[red]Verdict[/red]" if report.is_fragile else "[green]Verdict[/green]",
            expand=False,
        )
    )
    console.print(Panel(
        "\n".join(f"- {c}" for c in report.caveats),
        title="[yellow]What these checks cannot tell you[/yellow]",
        expand=False,
    ))


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
def report(
    market: str = typer.Option("USA", "--market", "-m"),
    strategy_name: str = typer.Option("trend_momentum", "--strategy", "-s"),
    split: str = typer.Option("full", "--split"),
    start: str = typer.Option(None, "--start"),
    end: str = typer.Option(None, "--end"),
    symbols: str = typer.Option(None, "--symbols"),
    capital: float = typer.Option(None, "--capital"),
    finalising: bool = typer.Option(False, "--finalising"),
    output: str = typer.Option(None, "--output", "-o", help="Explicit output file path."),
) -> None:
    """Run a backtest and write a standalone HTML report.

    The report needs no network and no build step: charts are inline SVG, so the file
    still renders from disk years later.
    """
    from app.backtesting.report import generate_report

    setup_logging()
    init_database()
    _banner()

    strategy = build_strategy(strategy_name)
    symbol_list = [s.strip().upper() for s in symbols.split(",")] if symbols else None

    try:
        with session_scope() as session:
            result = run_backtest(
                session,
                strategy,
                market,
                split=split,
                start=date.fromisoformat(start) if start else None,
                end=date.fromisoformat(end) if end else None,
                symbols=symbol_list,
                initial_capital=capital,
                finalising=finalising,
            )
    except DataLeakageError as exc:
        console.print(Panel(f"[red]{exc}[/red]", title="Leakage guard", expand=False))
        raise typer.Exit(code=3) from exc

    path = (
        generate_report(result, output_dir=Path(output).parent, filename=Path(output).name)
        if output
        else generate_report(result)
    )

    console.print(f"[green]OK[/green] report written to [cyan]{path}[/cyan]")
    console.print(
        f"[dim]{result.n_trades} trades, "
        f"total return {_fmt(result.metrics.get('total_return_pct'))}%, "
        f"Sharpe {_fmt(result.metrics.get('sharpe'), 3)}, "
        f"split {result.config.split}[/dim]"
    )


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
