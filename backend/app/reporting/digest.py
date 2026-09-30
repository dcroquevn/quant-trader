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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.backtesting.report import _CSS
from app.config import REPORTS_DIR
from app.core.logging import get_logger
from app.core.universe import (
    ALL_REGIONS,
    DEFAULT_UNIVERSE,
    LIQUIDITY_CONCERN_USD,
    TURNOVER_WINDOW,
    VERIFICATION_DATE,
    benchmark_for_region,
    universe_for_region,
)
from app.data.engine import DataEngine
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
.tag.buy{border-color:rgba(38,217,138,.4);color:var(--pos)}
.tag.sell{border-color:rgba(255,92,124,.4);color:var(--neg)}
.priv{background:rgba(77,159,255,.06);border:1px solid rgba(77,159,255,.28);border-radius:6px;
padding:12px 14px;margin:14px 0;font-size:12px;color:#a9cdff}
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
            }
        )

    data: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc),
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

    if include_holdings:
        from app.portfolio.holdings import list_holdings, realised_performance
        from app.portfolio.watch import check_holdings

        data["holdings"] = {
            "rows": list_holdings(session, include_closed=True),
            "realised": realised_performance(session),
            "watch": check_holdings(session),
        }

    return data


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
        for row in rows:
            tags = []
            if row.action == "BUY":
                tags.append('<span class="tag buy">entry conditions hold</span>')
            elif row.action == "SELL":
                tags.append('<span class="tag sell">exit conditions hold</span>')
            if not row.tradable:
                tags.append(f'<span class="tag thin">{_esc(row.blocked_reason)}</span>')
            cells.append(
                "<tr>"
                f"<td>{_esc(row.symbol)}</td>"
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
            "<th>Symbol</th><th>Signal</th><th>Score</th><th>Price</th>"
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


def _holdings_section(holdings: dict[str, Any]) -> str:
    """Rendered only when explicitly requested. See the module docstring on privacy."""
    rows = holdings["rows"]
    watch = {o.holding_id: o for o in holdings["watch"]}
    if not rows:
        return '<h2>Your positions</h2><p class="sub">None recorded.</p>'

    cells = []
    for holding in rows:
        outcome = watch.get(holding.id)
        if holding.closed_on is not None:
            state = f"closed {holding.closed_on.date()}"
            pnl = _pct(holding.pnl_pct)
        elif outcome is None or not outcome.usable:
            state = '<span class="tag thin">not checkable</span>'
            pnl = '<span class="null">n/a</span>'
        elif outcome.exit_triggered:
            ago = outcome.sessions_since_trigger or 0
            when = "today" if ago == 0 else f"{outcome.exit_triggered_on} ({ago} back)"
            state = f'<span class="tag sell">exit rule fired {when}</span>'
            pnl = _pct(outcome.unrealised_pnl_pct)
        else:
            state = '<span class="tag">holding</span>'
            pnl = _pct(outcome.unrealised_pnl_pct)
        cells.append(
            "<tr>"
            f"<td>{_esc(holding.symbol)}</td>"
            f"<td>{_esc(holding.region)}</td>"
            f"<td>{holding.quantity:g}</td>"
            f"<td>{holding.entry_price:g}</td>"
            f"<td>{_esc(holding.opened_on.date())}</td>"
            f"<td>{pnl}</td>"
            f"<td>{state}</td>"
            "</tr>"
        )

    return f"""
<h2>Your positions</h2>
<div class="priv"><strong>This section contains personal position data.</strong> It was included
because <code>--include-holdings</code> was passed. Do not publish this file anywhere public: a
GitHub Pages site on a free plan is served from a public repository and this table would be
world-readable.</div>
<table><thead><tr>
<th>Symbol</th><th>Region</th><th>Qty</th><th>Entry</th><th>Since</th><th>P&amp;L</th><th></th>
</tr></thead><tbody>{''.join(cells)}</tbody></table>
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
        _holdings_section(data["holdings"]) if data.get("holdings") is not None else ""
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

{''.join(_region_section(entry) for entry in data["regions"])}

<h2>Data</h2>
<p class="sub">{_esc(scan.disclaimer)}</p>
{orphaned}
<p class="sub">Liquidity figures are medians over {TURNOVER_WINDOW}. The backtester models no
market impact at any position size, so on the {sum(e['n_thin'] for e in data['regions'])}
instruments below {LIQUIDITY_CONCERN_USD / 1_000_000:.0f}M USD a day every modelled fill is
optimistic by an amount nothing here measures.</p>

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
