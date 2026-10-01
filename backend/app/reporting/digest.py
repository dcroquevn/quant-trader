"""The daily digest: one static HTML file describing the universe as of today.

Built for a scheduled job to publish and a phone to read. Everything is inlined, so it works
from a file:// URL, from GitHub Pages, or from an email attachment years later.

Privacy is a design constraint, not an option
---------------------------------------------
``include_holdings`` defaults to **False** and the digest that a public workflow publishes must
leave it that way. GitHub Pages on a free plan serves from a public repository, so anything in
this file is world-readable. Position sizes and entry prices are exactly the information that
should not be. Holdings reach the user through Telegram, which is private; the published page
carries market analysis only.

This is enforced by the default rather than by documentation: a caller has to ask for the
personal data explicitly, and :func:`generate_digest` prints a warning when it does.

What the digest deliberately does not say
-----------------------------------------
No "top picks", no ranking presented as confidence, no projection. A BUY row means the strategy's
entry conditions currently hold on historical data; the score counts conditions and is not a
probability. Every table that could be misread as a recommendation carries the backend's own
phrasing rather than wording invented here.
"""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.backtesting.report import _CSS
from app.config import REPORTS_DIR
from app.core.logging import get_logger
from app.core.universe import (
    find_asset,
    ALL_REGIONS,
    DEFAULT_UNIVERSE,
    LIQUIDITY_CONCERN_USD,
    TURNOVER_WINDOW,
    VERIFICATION_DATE,
    benchmark_for_region,
    universe_for_region,
)
from app.data.engine import DataEngine
from app.reporting.position_chart import (
    READOUT_SCRIPT,
    PositionChartData,
    position_chart,
)
from app.reporting.charts import (
    SERIES,
    Bar,
    horizontal_bars,
    indexed_lines,
    sparkline,
)
from app.strategies.registry import build_strategy
from app.strategies.scanner import scan_market

logger = get_logger(__name__)

__all__ = ["render_digest", "generate_digest", "collect_digest_data"]

_EXTRA_CSS = """
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;
margin:0 0 20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px 14px}
.card .k{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
.card .v{font-size:20px;margin-top:4px;font-variant-numeric:tabular-nums}
.card .n{font-size:11px;color:var(--dim);margin-top:4px}
.tag{font-size:10px;padding:2px 6px;border-radius:3px;border:1px solid var(--line);
color:var(--dim);white-space:nowrap}
.tag.thin{border-color:rgba(255,184,77,.4);color:var(--warn)}
.phead .tag{white-space:normal;max-width:100%;line-height:1.45;flex:1 1 180px}
.tag.buy{border-color:rgba(38,217,138,.4);color:var(--pos)}
.tag.sell{border-color:rgba(255,92,124,.4);color:var(--neg)}
.priv{background:rgba(77,159,255,.06);border:1px solid rgba(77,159,255,.28);border-radius:6px;
padding:12px 14px;margin:14px 0;font-size:12px;color:#a9cdff}
.chart svg{width:100%;height:auto;display:block;overflow:visible;max-width:580px;margin:0 auto}
.bl{font-size:9.5px;fill:var(--dim);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.bv{font-size:9.5px;fill:var(--text);font-variant-numeric:tabular-nums;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.bt{font-size:8.5px;fill:#4a5468;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin:0 0 8px}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:10.5px;color:var(--dim)}
.chip i{width:9px;height:9px;border-radius:2px;display:inline-block}
.spark{width:72px;height:20px;display:block}
.chart-empty{color:var(--dim);font-size:12px;text-align:center;padding:18px 0;margin:0}
.signal{background:rgba(38,217,138,.05);border:1px solid rgba(38,217,138,.3);border-radius:6px;
padding:14px 16px;margin:0 0 14px}
.signal h3{margin:0 0 6px;font-size:14px;color:var(--pos)}
.signal .why{margin:8px 0 0;padding:0;list-style:none}
.signal .why li{font-size:11.5px;color:#b8c0d0;margin:3px 0}
.signal .lv{display:flex;flex-wrap:wrap;gap:16px;margin-top:8px;font-size:11.5px}
.signal .lv b{font-variant-numeric:tabular-nums;color:var(--text);font-weight:600}
.position{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:16px 18px;margin:0 0 18px}
.phead{display:flex;align-items:baseline;flex-wrap:wrap;gap:10px}
.phead h3{margin:0;font-size:20px;letter-spacing:-.01em}
.pnl{font-size:20px;font-variant-numeric:tabular-nums;font-weight:600}
.pnl.up{color:var(--pos)}.pnl.down{color:var(--neg)}
.poschart{margin:14px 0 10px}
.poschart svg{max-width:100%}
.readout{font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums;
background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 10px;
margin:0 0 8px;min-height:17px;text-align:center}
.readout.up{color:var(--pos)}.readout.down{color:var(--neg)}
.pstats{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:9px;
margin:12px 0 0}
.pstats div{background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:7px 9px}
.pstats dt{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em}
.pstats dd{margin:3px 0 0;font-size:14px;font-variant-numeric:tabular-nums;color:var(--text)}
.warn-inline{font-size:11.5px;line-height:1.5;color:var(--warn);margin:10px 0 0}
.teach{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:16px 18px;margin:0 0 18px}
.teach h3{margin:0 0 8px;font-size:15px}
.teach p{font-size:12.5px;line-height:1.6;color:#b8c0d0;margin:8px 0 0}
.exits{margin:10px 0 0;font-size:12.5px}
.exits td{text-align:left;vertical-align:top;padding:7px 8px}
.exits td.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.exits td.dim{color:var(--dim)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.dot.trend{background:#3987e5}.dot.stop{background:var(--neg)}
.dot.target{background:var(--pos)}.dot.time{background:var(--dim)}
.note-inline{border-left:2px solid var(--line);padding-left:11px}
@media (max-width:640px){
  th,td{padding:5px 6px;font-size:11.5px}
  .wrap{padding:18px 12px 48px}
  h1{font-size:19px}
}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _pct(value: Any, digits: int = 2) -> str:
    """A percentage with its sign class, or an explicit null marker.

    Never renders a missing value as 0.00: a zero return and an unknown one are different facts
    and the reader has to be able to tell them apart.
    """
    if value is None:
        return '<span class="null">n/a</span>'
    cls = "pos" if float(value) > 0 else ("neg" if float(value) < 0 else "")
    return f'<span class="{cls}">{float(value):+.{digits}f}%</span>'


def _money(value: Any, digits: int = 0) -> str:
    if value is None:
        return '<span class="null">n/a</span>'
    return f"{float(value):,.{digits}f}"


def _price(value: Any) -> str:
    """Two decimals. Rounding to whole units turned ENIC's 4.12 into "4"."""
    return _money(value, 2)


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #


def collect_digest_data(
    session: Session,
    *,
    strategy_name: str = "trend_momentum",
    include_holdings: bool = False,
) -> dict[str, Any]:
    """Gather everything the digest reports. Separated so it can be tested without HTML."""
    engine = DataEngine(session)
    strategy = build_strategy(strategy_name)

    coverage = engine.coverage()
    declared = coverage[coverage["declared"]]

    # One market, because every tradable instrument is US-listed now; the regional
    # split comes from `region`, not from the market.
    scan = scan_market(session, strategy, "USA", log_decisions=False)

    regions: list[dict[str, Any]] = []
    for region in ALL_REGIONS:
        specs = universe_for_region(region)
        symbols = {s.symbol for s in specs}
        rows = [r for r in scan.rows if r.symbol in symbols]
        benchmark = benchmark_for_region(region)
        regions.append(
            {
                "region": region,
                "specs": specs,
                "rows": sorted(rows, key=lambda r: r.score, reverse=True),
                "n_buy": sum(1 for r in rows if r.action == "BUY"),
                "n_sell": sum(1 for r in rows if r.action == "SELL"),
                "n_thin": sum(1 for s in specs if s.is_thinly_traded),
                "n_blocked": sum(1 for r in rows if not r.tradable),
                "benchmark": benchmark,
                "history": {},  # filled below, once the shared history is loaded
            }
        )

    data: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc),
        "history": {},  # set immediately below; regions borrow from it
        "strategy": strategy.describe(),
        "scan": scan,
        "regions": regions,
        "n_instruments": len(DEFAULT_UNIVERSE),
        "total_bars": int(declared["bars"].sum()),
        "stalest": _stalest(declared),
        "orphaned_bars": int(coverage[~coverage["declared"]]["bars"].sum()),
        "orphaned_symbols": sorted(coverage[~coverage["declared"]]["symbol"].tolist()),
        "holdings": None,
    }

    data["history"] = _history(engine, [s.symbol for s in DEFAULT_UNIVERSE])
    for entry in data["regions"]:
        entry["history"] = data["history"]

    if include_holdings:
        from app.portfolio.holdings import list_holdings, realised_performance
        from app.portfolio.watch import check_holdings

        data["holdings"] = {
            "rows": list_holdings(session, include_closed=True),
            "realised": realised_performance(session),
            "watch": check_holdings(session),
        }

    return data


HISTORY_DAYS = 365
"""How much price history the charts draw. A year: long enough for a trend line to mean
something, short enough that a 42-instrument load stays quick and the SVG stays small."""

SPARK_POINTS = 60
"""Points in a table sparkline. More than this and a 72px-wide cell renders mush."""


def _history(engine: DataEngine, symbols: list[str]) -> dict[str, list[tuple[str, float]]]:
    """Daily closes per symbol for the chart layer, loaded once and shared.

    Carried-forward bars are trimmed, so a chart never draws a vendor-invented flat tail as if it
    were a real price. A symbol with no stored data is absent from the result rather than present
    and empty, so callers have to handle it rather than plotting a blank.
    """
    cutoff = date.today() - timedelta(days=HISTORY_DAYS)
    out: dict[str, list[tuple[str, float]]] = {}
    for symbol in symbols:
        try:
            bars = engine.load(symbol, start=cutoff, trim_carried_forward=True)
        except (KeyError, LookupError):
            continue
        if bars.empty or "close" in bars and bars["close"].notna().sum() < 2:
            continue
        closes = bars["close"].dropna()
        out[symbol] = [(str(i.date()), float(v)) for i, v in closes.items()]
    return out


def _stalest(coverage) -> tuple[str, date] | None:
    """The instrument whose newest bar is oldest, for the freshness banner.

    Surfaced prominently because everything else in the digest is computed from stored bars, and
    a digest built on week-old data looks identical to one built on today's.
    """
    with_data = coverage[coverage["bars"] > 0]
    if with_data.empty:
        return None
    row = with_data.loc[with_data["last_bar"].idxmin()]
    last = row["last_bar"]
    return str(row["symbol"]), (last.date() if hasattr(last, "date") else last)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _returns_chart(data: dict[str, Any]) -> str:
    """20-session return, one small-multiple chart per region.

    One combined chart could not say where a region ended: the colour already encodes gain and
    loss, so it was not free to also encode region, and a reader seeing a Chilean ADR beside a
    Taiwanese one had no way to tell them apart. Three charts spend vertical space instead, which
    is the cheap axis on a phone.

    Sign is carried by the side of the zero line and by the +/- in the direct label, so meaning
    never rests on colour alone.
    """
    charts: list[str] = []
    for entry in data["regions"]:
        rows = [r for r in entry["rows"] if r.return_20d is not None]
        if not rows:
            continue
        bars = [
            Bar(
                label=row.symbol,
                value=float(row.return_20d),
                detail=f"{row.symbol}: {row.return_20d:+.2f}% over 20 sessions, "
                f"last {row.price:.2f}",
            )
            for row in sorted(rows, key=lambda r: r.return_20d, reverse=True)
        ]
        charts.append(
            horizontal_bars(
                bars,
                title=f"{entry['region']} — 20-session return",
                value_suffix="%",
                diverging=True,
                row_height=19.0,
            )
        )
    return "".join(charts)


def _benchmark_chart(data: dict[str, Any]) -> str:
    """Each region's benchmark over a year, rebased to 100."""
    history = data["history"]
    series: dict[str, list[tuple[str, float]]] = {}
    for entry in data["regions"]:
        symbol = entry["benchmark"].symbol
        if symbol and symbol in history:
            series[f"{entry['region']} ({symbol})"] = history[symbol]
    return indexed_lines(series, title="Regional benchmarks, rebased to 100 one year ago")


def _liquidity_chart(data: dict[str, Any]) -> str:
    """Daily traded value on a log scale, with the threshold that triggers the caveat."""
    bars: list[Bar] = []
    colours = {
        entry["region"]: SERIES[i % len(SERIES)] for i, entry in enumerate(data["regions"])
    }
    for entry in data["regions"]:
        specs = [s for s in entry["specs"] if s.median_turnover_usd]
        for spec in sorted(specs, key=lambda s: s.median_turnover_usd or 0, reverse=True):
            bars.append(
                Bar(
                    label=spec.symbol,
                    value=float(spec.median_turnover_usd or 0),
                    group=entry["region"],
                    flagged=spec.is_thinly_traded,
                    detail=spec.liquidity_caveat
                    or f"{spec.symbol}: {spec.median_turnover_usd:,.0f} USD/day",
                )
            )
    return horizontal_bars(
        bars,
        title="Median daily traded value (log scale)",
        by_group=True,
        group_colours=colours,
        log_scale=True,
        row_height=19.0,
        threshold=LIQUIDITY_CONCERN_USD,
        threshold_label=f"{LIQUIDITY_CONCERN_USD / 1_000_000:.0f}M",
    )


def _signals_block(data: dict[str, Any]) -> str:
    """Today's entries and exits, with the conditions that produced each one.

    Given its own block above the charts because it is the only part that might prompt an action,
    and listing the reasons because a signal you cannot interrogate is one you either obey blindly
    or ignore.
    """
    signals = [
        (entry["region"], row)
        for entry in data["regions"]
        for row in entry["rows"]
        if row.action in {"BUY", "SELL"}
    ]
    if not signals:
        return (
            '<p class="sub">No entry or exit conditions hold on any instrument today. That is '
            "the normal case: over the 2016-2021 backtest this strategy made 915 entries across "
            "all 42 instruments, about 13 a month in total.</p>"
        )

    blocks = []
    for region, row in signals:
        spec = find_asset(row.symbol, row.market)
        levels = [
            f"Last <b>{row.price:,.2f}</b>",
            f"20d <b>{row.return_20d:+.1f}%</b>" if row.return_20d is not None else "",
            f"Stop <b>{row.stop_price:,.2f}</b>" if row.stop_price else "",
            f"Target <b>{row.take_profit_price:,.2f}</b>" if row.take_profit_price else "",
            f"Reward/risk <b>{row.risk_reward:.1f}x</b>" if row.risk_reward else "",
        ]
        why = "".join(f"<li>{_esc(r)}</li>" for r in row.reasons)
        caveat = (
            f'<p class="sub" style="margin-top:8px">{_esc(spec.liquidity_caveat)}</p>'
            if spec.liquidity_caveat
            else ""
        )
        blocks.append(
            f'<div class="signal"><h3>{_esc(row.action)} &middot; {_esc(row.symbol)} '
            f"&mdash; {_esc(spec.name)}</h3>"
            f'<p class="sub">{_esc(region)} &middot; score {row.score:.2f}: '
            f"{_esc(row.score_description)}</p>"
            f'<div class="lv">{"".join(f"<span>{p}</span>" for p in levels if p)}</div>'
            f'<ul class="why">{why}</ul>{caveat}</div>'
        )
    return "".join(blocks)


def _region_section(entry: dict[str, Any]) -> str:
    region = entry["region"]
    benchmark = entry["benchmark"]
    rows = entry["rows"]

    if not rows:
        body = (
            '<p class="sub">No instruments could be scanned in this region. Run '
            "<code>python -m app download-data</code>.</p>"
        )
    else:
        cells = []
        history = entry.get("history", {})
        for row in rows:
            tags = []
            if row.action == "BUY":
                tags.append('<span class="tag buy">entry conditions hold</span>')
            elif row.action == "SELL":
                tags.append('<span class="tag sell">exit conditions hold</span>')
            if not row.tradable:
                tags.append(f'<span class="tag thin">{_esc(row.blocked_reason)}</span>')
            points = history.get(row.symbol, [])
            spark = sparkline([v for _, v in points[-SPARK_POINTS:]])
            cells.append(
                "<tr>"
                f"<td>{_esc(row.symbol)}</td>"
                f"<td>{spark}</td>"
                f"<td>{_esc(row.action)}</td>"
                f"<td>{row.score:.2f}</td>"
                f"<td>{_price(row.price)}</td>"
                f"<td>{_pct(row.return_20d)}</td>"
                f"<td>{_money(row.dollar_volume)}</td>"
                f"<td>{' '.join(tags)}</td>"
                "</tr>"
            )
        body = (
            '<div class="scroll"><table><thead><tr>'
            "<th>Symbol</th><th>60 sessions</th><th>Signal</th><th>Score</th><th>Price</th>"
            "<th>20d return</th><th>Traded value</th><th></th>"
            "</tr></thead><tbody>" + "".join(cells) + "</tbody></table></div>"
        )

    caveats = "".join(f"<li>{_esc(c)}</li>" for c in benchmark.caveats)
    return f"""
<h2>{_esc(region)}</h2>
<div class="grid">
  <div class="card"><div class="k">Instruments</div><div class="v">{len(entry['specs'])}</div>
    <div class="n">{entry['n_thin']} below
    {LIQUIDITY_CONCERN_USD / 1_000_000:.0f}M USD a day</div></div>
  <div class="card"><div class="k">Entry conditions hold</div>
    <div class="v">{entry['n_buy']}</div>
    <div class="n">counts conditions, not a probability</div></div>
  <div class="card"><div class="k">Exit conditions hold</div>
    <div class="v">{entry['n_sell']}</div>
    <div class="n">relevant only if you hold it</div></div>
  <div class="card"><div class="k">Benchmark</div>
    <div class="v">{_esc(benchmark.symbol or 'none')}</div>
    <div class="n">{_esc(benchmark.kind)}</div></div>
</div>
{body}
<div class="limits" style="margin-top:12px"><ul>{caveats}</ul></div>
"""


def _exit_rules_block() -> str:
    """What actually closes a position, and in what proportion.

    Written because the obvious reading of a target -- that it marks where the price is expected
    to turn -- is wrong, and acting on that reading leads somewhere different from acting on the
    rule. The target is a pre-committed exit at three times the risk taken. It says nothing
    about what the price will do next.
    """
    return """
<div class="teach">
  <h3>How a position ends</h3>
  <p>Four rules can close this. Whichever comes first wins, and the percentages are how often
  each one actually ended a trade across 915 backtest trades (2016&ndash;2021).</p>
  <table class="exits">
    <tbody>
      <tr>
        <td><span class="dot trend"></span><b>Trend break</b></td>
        <td>The close falls below the 50-day average.</td>
        <td class="n">39.8%</td>
        <td class="n dim">median &minus;1.0%</td>
      </tr>
      <tr>
        <td><span class="dot stop"></span><b>Stop</b></td>
        <td>Price reaches the lower line. Placed two ATR below the entry bar&rsquo;s close
        &mdash; a volatility measurement, not a round number.</td>
        <td class="n">33.2%</td>
        <td class="n dim">median &minus;3.7%</td>
      </tr>
      <tr>
        <td><span class="dot target"></span><b>Target</b></td>
        <td>Price reaches the upper line, three times the risk above the entry.</td>
        <td class="n">21.4%</td>
        <td class="n dim">median +10.3%</td>
      </tr>
      <tr>
        <td><span class="dot time"></span><b>Time</b></td>
        <td>60 trading days pass.</td>
        <td class="n">1.6%</td>
        <td class="n dim">median +10.8%</td>
      </tr>
    </tbody>
  </table>
  <p class="note-inline"><b>The target is not a prediction.</b> It does not mark where the price
  is expected to turn &mdash; nothing in this project forecasts that. It is a level chosen in
  advance so the decision to take a profit is made before there is a profit to be emotional
  about, at three times what you were risking. The price may keep rising after you sell. It may
  also not reach the target at all: four times out of five it did not.</p>
  <p class="note-inline">Notice which row is largest. <b>The trend break ends more positions
  than the stop and the target</b>, and it is the one with no line on the chart &mdash; it
  depends on the moving average, drawn dashed.</p>
</div>
"""


def _trend_line(symbol: str, window: list[tuple[str, float]]) -> "list[float] | None":
    """EMA50 over the charted window, aligned to it.

    Computed from the window's own closes rather than loaded, because the digest already has
    them and a second database round trip per position would buy nothing. The first 49 points
    are NaN by definition and the renderer skips them, so the line starts where it becomes
    meaningful instead of being drawn from a half-formed average.
    """
    if len(window) < 50:
        return None
    closes = pd.Series([c for _, c in window], dtype="float64")
    return closes.ewm(span=50, adjust=False, min_periods=50).mean().tolist()


def _position_block(holding: Any, outcome: Any, history: dict[str, Any]) -> str:
    """One held position: the chart, the numbers, and what has to happen for it to end."""
    points = history.get(holding.symbol, [])
    entry_date = holding.opened_on.date().isoformat()
    # Context before the entry, so it is not pinned to the left edge. 90 sessions is about four
    # months, long enough to show the trend the entry was taken in.
    start_index = max(0, next(
        (i for i, (d, _) in enumerate(points) if d >= entry_date), len(points)
    ) - 90)
    window = points[start_index:]

    trend = _trend_line(holding.symbol, window)
    chart = position_chart(
        PositionChartData(
            symbol=holding.symbol,
            name="",
            points=window,
            entry_price=holding.entry_price,
            entry_date=entry_date,
            stop_price=holding.stop_price,
            take_profit_price=holding.take_profit_price,
            quantity=holding.quantity,
            trend=trend,
            exit_triggered_on=(
                holding.exit_signal_on.date().isoformat() if holding.exit_signal_on else None
            ),
            exit_reason=holding.exit_signal_reason,
        )
    )

    # Mark from the newest stored close regardless of whether the exit rule could be
    # evaluated. Those are different questions: the rule needs a bar *after* the entry, which
    # does not exist on the day you buy, while marking the position needs only a price.
    last = window[-1][1] if window else None
    pnl_pct = (last / holding.entry_price - 1) * 100 if last else None
    value = (last or holding.entry_price) * holding.quantity
    # The recorded amount, not price x quantity. The share count is derived from the cash, so
    # multiplying back gives 20.35 for a 20.36 purchase -- the stored figure is the fact.
    cost = (
        holding.entry_amount
        if holding.entry_amount is not None and holding.entry_amount_currency == "USD"
        else holding.entry_price * holding.quantity
    )

    def distance(level: float | None) -> str:
        if not level or not last:
            return '<span class="null">n/a</span>'
        return f"{(level / last - 1) * 100:+.1f}%"

    state: str
    if outcome is None or not outcome.usable:
        # On the day you buy there is no session bar yet, which is normal and temporary. Any
        # other reason means the position genuinely is not being watched, and the two must not
        # read the same.
        problem = (outcome.problem if outcome else "").lower()
        if "nothing to evaluate" in problem:
            state = (
                '<span class="tag">the exit rule starts tomorrow &mdash; today&rsquo;s bar '
                "does not exist yet</span>"
            )
        else:
            state = (
                '<span class="tag thin">NOT being watched: '
                f'{_esc(outcome.problem if outcome else "never checked")}</span>'
            )
    elif outcome.exit_triggered:
        ago = outcome.sessions_since_trigger or 0
        when = "today" if ago == 0 else f"{outcome.exit_triggered_on}, {ago} sessions ago"
        state = (
            f'<span class="tag sell">{_esc(outcome.exit_reason)} &mdash; {when}</span>'
        )
    else:
        state = '<span class="tag buy">within the rules</span>'

    rows = [
        ("You paid", f"{holding.entry_price:,.2f}"),
        ("Shares", f"{holding.quantity:.6f}"),
        ("Put in", f"{cost:,.2f} USD"),
        ("Worth now", f"{value:,.2f} USD" if last else "n/a"),
        ("Target", f"{holding.take_profit_price:,.2f}" if holding.take_profit_price else "none"),
        ("...from here", distance(holding.take_profit_price)),
        ("Stop", f"{holding.stop_price:,.2f}" if holding.stop_price else "none"),
        ("...from here", distance(holding.stop_price)),
    ]
    cells = "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in rows)

    warnings = []
    if holding.stop_price is None:
        warnings.append(
            "<b>No stop is recorded</b>, so only a trend break can close this and nothing "
            "defines where the risk ends. Fix it with "
            "<code>python -m app set-levels " + _esc(holding.symbol) + "</code>."
        )
    if holding.entry_price_estimated:
        warnings.append(
            "The entry price was assumed from a closing price, not reported, so every figure "
            "here is approximate."
        )
    if outcome is not None and outcome.liquidity_caveat:
        warnings.append(_esc(outcome.liquidity_caveat))
    warning_html = "".join(f'<p class="warn-inline">{w}</p>' for w in warnings)

    return f"""
<div class="position">
  <div class="phead">
    <h3>{_esc(holding.symbol)}</h3>
    <span class="pnl {'up' if (pnl_pct or 0) >= 0 else 'down'}">
      {_pct(pnl_pct)}
    </span>
    {state}
  </div>
  <p class="sub">{_esc(holding.region)} &middot; held since {entry_date}
  &middot; via {_esc(holding.broker) or 'your broker'}</p>
  {chart}
  <dl class="pstats">{cells}</dl>
  {warning_html}
</div>
"""


def _holdings_section(holdings: dict[str, Any], history: dict[str, Any]) -> str:
    """Rendered only when explicitly requested. See the module docstring on privacy."""
    rows = holdings["rows"]
    watch = {o.holding_id: o for o in holdings["watch"]}
    if not rows:
        return '<h2>Your positions</h2><p class="sub">None recorded.</p>'

    open_rows = [h for h in rows if h.closed_on is None]
    closed_rows = [h for h in rows if h.closed_on is not None]

    blocks = "".join(
        _position_block(h, watch.get(h.id), history) for h in open_rows
    )

    closed_html = ""
    if closed_rows:
        closed_cells = "".join(
            "<tr>"
            f"<td>{_esc(h.symbol)}</td>"
            f"<td>{_esc(h.opened_on.date())} &rarr; {_esc(h.closed_on.date())}</td>"
            f"<td>{h.entry_price:,.2f}</td>"
            f"<td>{h.exit_price:,.2f}</td>"
            f"<td>{_pct(h.pnl_pct)}</td>"
            f"<td>{h.holding_period_days} days</td>"
            "</tr>"
            for h in closed_rows
        )
        closed_html = f"""
<h3>Closed</h3>
<table><thead><tr>
<th>Symbol</th><th>Held</th><th>In</th><th>Out</th><th>Result</th><th>For</th>
</tr></thead><tbody>{closed_cells}</tbody></table>"""

    return f"""
<h2>Your positions</h2>
<div class="priv"><strong>This section contains personal position data.</strong> It was included
because <code>--include-holdings</code> was passed. Do not publish this file anywhere public: a
GitHub Pages site on a free plan is served from a public repository and this would be
world-readable.</div>
{blocks}
{_exit_rules_block()}
{closed_html}
<p class="sub">{_esc(holdings['realised']['evidence'])}</p>
"""


def render_digest(data: dict[str, Any]) -> str:
    """The whole digest as one HTML string."""
    stamp: datetime = data["generated_at"]
    scan = data["scan"]

    stalest = data["stalest"]
    freshness = ""
    if stalest is not None:
        symbol, last = stalest
        age = (stamp.date() - last).days
        level = "note" if age > 5 else "priv"
        freshness = (
            f'<div class="{level}">Data as of <strong>{last}</strong> at the oldest '
            f"({_esc(symbol)}, {age} days back). Every figure below is computed from stored "
            "bars, so a digest built on stale data looks exactly like one built on today's -- "
            "this line is how you tell.</div>"
        )

    badges = [
        f'<span class="badge"><strong>{data["n_instruments"]}</strong> instruments</span>',
        f'<span class="badge"><strong>{data["total_bars"]:,}</strong> stored bars</span>',
        f'<span class="badge">strategy <strong>{_esc(scan.strategy["name"])}</strong></span>',
        f'<span class="badge">symbols verified {VERIFICATION_DATE}</span>',
        '<span class="badge warn">paper only: this software places no orders</span>',
    ]

    orphaned = ""
    if data["orphaned_bars"]:
        orphaned = (
            f'<p class="sub">{data["orphaned_bars"]:,} stored bars belong to '
            f"{len(data['orphaned_symbols'])} instrument(s) no longer in the universe "
            f"({_esc(', '.join(data['orphaned_symbols'][:6]))}"
            f"{'...' if len(data['orphaned_symbols']) > 6 else ''}). Nothing reads them; "
            "<code>python -m app prune-data</code> removes them.</p>"
        )

    holdings_block = (
        _holdings_section(data["holdings"], data["history"])
        if data.get("holdings") is not None
        else ""
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>quant-trader digest &mdash; {stamp:%Y-%m-%d}</title>
<style>{_CSS}{_EXTRA_CSS}</style>
</head><body><div class="wrap">

<h1>Daily digest</h1>
<p class="sub">Generated {stamp:%Y-%m-%d %H:%M} UTC &middot; quant-trader</p>
<div class="badges">{''.join(badges)}</div>

{freshness}

<div class="note"><strong>What a signal here is.</strong> A BUY row means this strategy's entry
conditions currently hold on historical data. The score counts how many conditions hold; it is
<em>not</em> a probability of profit and not a forecast. Nothing on this page says any instrument
will rise or fall. The strategy has not been shown to be profitable, and a historical pattern
repeating is an assumption, not a finding.</div>

<div class="note"><strong>Everything here is priced in USD.</strong> The Chilean and Asian
instruments are ADRs and country ETFs listed in New York, not local shares. Their returns include
the currency move as well as the underlying move, and nothing in this project separates the
two.</div>

{holdings_block}

<h2>Today</h2>
{_signals_block(data)}

<h2>What is moving</h2>
{_returns_chart(data)}

<h2>How each region has done</h2>
<p class="sub">Each region's benchmark over the past year, rebased so all three start at 100.
Rebasing is what lets instruments trading at very different prices share one axis &mdash; a second
vertical scale would let the crossover point be placed anywhere by choosing the scales.</p>
{_benchmark_chart(data)}

<h2>How much each one trades</h2>
<p class="sub">Log scale: this spans four orders of magnitude, and on a linear axis everything
except SPY would be an invisible sliver. Bars left of the dashed line are the ones where a
position large enough to matter would move the price.</p>
{_liquidity_chart(data)}

{''.join(_region_section(entry) for entry in data["regions"])}

<h2>Data</h2>
<p class="sub">{_esc(scan.disclaimer)}</p>
{orphaned}
<p class="sub">Liquidity figures are medians over {TURNOVER_WINDOW}. The backtester models no
market impact at any position size, so on the {sum(e['n_thin'] for e in data['regions'])}
instruments below {LIQUIDITY_CONCERN_USD / 1_000_000:.0f}M USD a day every modelled fill is
optimistic by an amount nothing here measures.</p>

<script>{READOUT_SCRIPT}</script>

<footer>
No part of this system places orders, and live trading is not implemented. Prices come from free
providers with no service guarantee. Every figure describes one historical sample; none is a
forecast.
</footer>

</div></body></html>"""


def generate_digest(
    session: Session,
    *,
    output: Path | None = None,
    strategy_name: str = "trend_momentum",
    include_holdings: bool = False,
) -> Path:
    """Build the digest and write it. Returns the path.

    ``include_holdings`` logs a warning, because the resulting file is unsafe to publish and the
    person who passed the flag may not be the person who later runs the deploy.
    """
    if include_holdings:
        logger.warning(
            "Digest will contain personal position data. Do not publish this file to a public "
            "location -- GitHub Pages on a free plan serves from a public repository."
        )

    data = collect_digest_data(
        session, strategy_name=strategy_name, include_holdings=include_holdings
    )
    path = output or (REPORTS_DIR / "digest.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_digest(data), encoding="utf-8")
    logger.info("Wrote digest to %s", path)
    return path
