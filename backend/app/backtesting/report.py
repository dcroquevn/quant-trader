"""Self-contained HTML backtest reports.

A report is a standalone file: no CDN, no network, no build step. It opens from disk in
ten years' time and still renders, which is the point of an audit artefact.

What every report is required to state
--------------------------------------
Section 42 of the brief lists the contents, and three of them are non-negotiable because
leaving them out is how a backtest gets misread:

**Which data partition produced the numbers.** A TRAIN result and a TEST result look
identical on a chart and mean entirely different things. The split is in the header, not
a footnote.

**The cost assumptions.** Round-trip friction is printed next to the returns it was
deducted from, so nobody has to go and check whether costs were modelled at all.

**The limitations.** Survivorship bias, the intrabar tie-break, the benchmark's flaws.
These sit at the bottom of the document in full, not compressed into a disclaimer line.

Charts are inline SVG drawn from the data directly. A charting library would mean either
a CDN dependency (breaking the standalone promise) or vendoring a megabyte of JavaScript
into every report.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.backtesting.metrics import annual_returns, drawdown_series, monthly_returns
from app.config import REPORTS_DIR
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["generate_report", "render_report"]


# --------------------------------------------------------------------------- #
# Inline SVG charts
# --------------------------------------------------------------------------- #


def _sparkline_path(values: list[float], width: float, height: float, pad: float = 4.0) -> str:
    """SVG polyline points for a series, scaled to fit.

    Returns an empty string for a degenerate series rather than emitting a path with NaN
    coordinates, which renders as an invisible mess in some viewers and a browser console
    error in others.
    """
    usable = [v for v in values if v is not None and pd.notna(v)]
    if len(usable) < 2:
        return ""

    lowest, highest = min(usable), max(usable)
    span = highest - lowest
    if span <= 0:
        span = abs(highest) or 1.0

    inner_w, inner_h = width - 2 * pad, height - 2 * pad
    step = inner_w / (len(values) - 1)

    points: list[str] = []
    for i, value in enumerate(values):
        if value is None or pd.isna(value):
            continue
        x = pad + i * step
        y = pad + inner_h * (1.0 - (value - lowest) / span)
        points.append(f"{x:.2f},{y:.2f}")
    return " ".join(points)


def _line_chart(
    series: pd.Series,
    *,
    title: str,
    colour: str = "#4d9fff",
    fill: str | None = None,
    width: int = 900,
    height: int = 220,
) -> str:
    if series.empty or series.notna().sum() < 2:
        return f'<div class="chart-empty">{html.escape(title)}: not enough data</div>'

    values = series.tolist()
    points = _sparkline_path(values, width, height)
    if not points:
        return f'<div class="chart-empty">{html.escape(title)}: not enough data</div>'

    usable = [v for v in values if pd.notna(v)]
    lowest, highest = min(usable), max(usable)
    area = ""
    if fill:
        area = (
            f'<polygon points="4,{height - 4} {points} {width - 4},{height - 4}" '
            f'fill="{fill}" opacity="0.18" />'
        )

    return f"""
<figure class="chart">
  <figcaption>{html.escape(title)}</figcaption>
  <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img"
       aria-label="{html.escape(title)}">
    <line x1="4" y1="{height - 4}" x2="{width - 4}" y2="{height - 4}" class="axis" />
    {area}
    <polyline points="{points}" fill="none" stroke="{colour}" stroke-width="1.6" />
  </svg>
  <div class="chart-range">
    <span>{lowest:,.2f}</span><span>{highest:,.2f}</span>
  </div>
</figure>"""


def _bar_chart(
    series: pd.Series, *, title: str, width: int = 900, height: int = 200
) -> str:
    """Signed bar chart: gains green, losses red."""
    if series.empty:
        return f'<div class="chart-empty">{html.escape(title)}: no data</div>'

    values = series.tolist()
    labels = [str(i.date()) if hasattr(i, "date") else str(i) for i in series.index]
    largest = max(abs(v) for v in values if pd.notna(v)) or 1.0

    pad = 6.0
    inner_h = height - 2 * pad
    zero_y = pad + inner_h / 2
    slot = (width - 2 * pad) / max(1, len(values))
    bar_w = max(1.0, slot * 0.7)

    bars: list[str] = []
    for i, (value, label) in enumerate(zip(values, labels)):
        if pd.isna(value):
            continue
        magnitude = abs(value) / largest * (inner_h / 2)
        x = pad + i * slot + (slot - bar_w) / 2
        y = zero_y - magnitude if value >= 0 else zero_y
        colour = "#26d98a" if value >= 0 else "#ff5c7c"
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" '
            f'height="{magnitude:.2f}" fill="{colour}">'
            f"<title>{html.escape(label)}: {value:+.2f}%</title></rect>"
        )

    return f"""
<figure class="chart">
  <figcaption>{html.escape(title)}</figcaption>
  <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img"
       aria-label="{html.escape(title)}">
    <line x1="{pad}" y1="{zero_y}" x2="{width - pad}" y2="{zero_y}" class="axis" />
    {''.join(bars)}
  </svg>
</figure>"""


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def _num(value: Any, digits: int = 2, suffix: str = "") -> str:
    """Render a number, or an em dash when it is genuinely undefined.

    Never renders a missing value as zero: in a performance report the difference between
    "0.00 Sharpe" and "Sharpe is undefined for this sample" changes the conclusion.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return '<span class="null" title="not defined for this sample">&mdash;</span>'
    try:
        return f"{float(value):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return html.escape(str(value))


def _signed(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return '<span class="null">&mdash;</span>'
    cls = "pos" if float(value) > 0 else "neg" if float(value) < 0 else ""
    return f'<span class="{cls}">{float(value):+,.{digits}f}{suffix}</span>'


def _metric_rows(
    metrics: dict[str, Any], benchmark: dict[str, Any], rows: list[tuple[str, str, int, str]]
) -> str:
    available = benchmark.get("available")
    out: list[str] = []
    for label, key, digits, suffix in rows:
        bench_cell = (
            _num(benchmark.get(key), digits, suffix)
            if available
            else '<span class="null">n/a</span>'
        )
        out.append(
            f"<tr><th>{html.escape(label)}</th>"
            f"<td>{_num(metrics.get(key), digits, suffix)}</td>"
            f"<td>{bench_cell}</td></tr>"
        )
    return "".join(out)


# --------------------------------------------------------------------------- #
# Template
# --------------------------------------------------------------------------- #

_CSS = """
:root{--bg:#0b0e14;--panel:#141924;--line:#232b3d;--text:#e2e8f0;--dim:#8a94a8;
--accent:#4d9fff;--pos:#26d98a;--neg:#ff5c7c;--warn:#ffb84d}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Inter,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:28px 16px 64px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:32px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.sub{color:var(--dim);font-size:12px;margin:0 0 18px}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0 22px}
.badge{font-size:11px;padding:3px 8px;border-radius:4px;background:var(--panel);
border:1px solid var(--line);color:var(--dim)}
.badge strong{color:var(--text);font-weight:600}
.badge.warn{border-color:rgba(255,184,77,.35);color:var(--warn)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
thead th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em;
font-weight:600}
tbody th{font-weight:400;color:var(--dim)}
.pos{color:var(--pos)}.neg{color:var(--neg)}.null{color:#4a5468}
.chart{margin:0 0 20px;background:var(--panel);border:1px solid var(--line);
border-radius:6px;padding:12px}
.chart figcaption{font-size:11px;color:var(--dim);text-transform:uppercase;
letter-spacing:.05em;margin-bottom:8px}
.chart svg{width:100%;height:auto;display:block}
.chart-range{display:flex;justify-content:space-between;font-size:10px;color:#4a5468;
margin-top:4px}
.chart-empty{background:var(--panel);border:1px solid var(--line);border-radius:6px;
padding:24px;text-align:center;color:var(--dim);font-size:12px;margin-bottom:20px}
.axis{stroke:#2f3a52;stroke-width:1}
.note{background:rgba(255,184,77,.06);border:1px solid rgba(255,184,77,.25);
border-radius:6px;padding:12px 14px;margin:14px 0;font-size:12px;color:#ffd08a}
.note strong{color:var(--warn)}
.limits{background:var(--panel);border:1px solid var(--line);border-radius:6px;
padding:14px 18px}
.limits li{margin:7px 0;font-size:12.5px;color:#b8c0d0}
.scroll{max-height:420px;overflow:auto;border:1px solid var(--line);border-radius:6px}
.scroll table{font-size:12.5px}
.scroll thead th{position:sticky;top:0;background:var(--panel)}
footer{margin-top:40px;padding-top:14px;border-top:1px solid var(--line);
color:#4a5468;font-size:11px}
"""


def render_report(result, *, generated_at: datetime | None = None) -> str:
    """Render a :class:`~app.backtesting.engine.BacktestResult` to a full HTML document."""
    metrics = result.metrics or {}
    benchmark = result.benchmark_metrics or {}
    comparison = metrics.get("vs_benchmark") or {}
    stamp = generated_at or datetime.now(timezone.utc)

    equity = result.equity_curve
    drawdown = drawdown_series(equity) if not equity.empty else pd.Series(dtype=float)

    # ---- Header badges ------------------------------------------------
    split = result.config.split
    badges = [
        f'<span class="badge">strategy <strong>{html.escape(result.strategy.get("name", "?"))}'
        f' v{html.escape(str(result.strategy.get("version", "?")))}</strong></span>',
        f'<span class="badge">market <strong>{html.escape(result.config.market)}</strong></span>',
        f'<span class="badge{" warn" if split == "test" else ""}">split '
        f'<strong>{html.escape(split)}</strong></span>',
        f'<span class="badge">period <strong>{result.start_date:%Y-%m-%d}'
        f' &rarr; {result.end_date:%Y-%m-%d}</strong></span>',
        f'<span class="badge">universe <strong>{len(result.universe)}</strong></span>',
        f'<span class="badge warn">round-trip cost '
        f'<strong>{result.cost_model.get("round_trip_pct", "?")}%</strong></span>',
    ]

    split_note = ""
    if split == "train":
        split_note = (
            '<div class="note"><strong>This is the TRAIN partition.</strong> It is the '
            "data any parameter search is allowed to read, so results here carry no "
            "out-of-sample information whatsoever. Do not treat them as an estimate of "
            "future behaviour.</div>"
        )
    elif split == "validation":
        split_note = (
            '<div class="note"><strong>This is the VALIDATION partition.</strong> Used to '
            "choose between candidates. Repeatedly selecting on it gradually turns it "
            "into training data, so the more often it is consulted the less it tells "
            "you.</div>"
        )
    elif split == "test":
        split_note = (
            '<div class="note"><strong>This is the TEST partition.</strong> It is meant to '
            "be read once, after parameters are frozen. If it has been opened before, "
            "this is no longer an out-of-sample result.</div>"
        )
    elif split == "full":
        split_note = (
            '<div class="note"><strong>This run used the full history.</strong> Convenient '
            "for inspection, but it leaves no held-out data, so nothing here can be "
            "described as out-of-sample.</div>"
        )

    # ---- Metric tables ------------------------------------------------
    performance = _metric_rows(
        metrics,
        benchmark,
        [
            ("Total return", "total_return_pct", 2, "%"),
            ("CAGR", "cagr_pct", 2, "%"),
            ("Annualised volatility", "annualised_volatility_pct", 2, "%"),
            ("Sharpe", "sharpe", 3, ""),
            ("Sortino", "sortino", 3, ""),
            ("Calmar", "calmar", 3, ""),
            ("Maximum drawdown", "max_drawdown_pct", 2, "%"),
        ],
    )

    trade_rows = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{_num(metrics.get(key), digits, suffix)}</td></tr>"
        for label, key, digits, suffix in [
            ("Trades", "n_trades", 0, ""),
            ("Wins / losses", "n_wins", 0, ""),
            ("Win rate", "win_rate_pct", 1, "%"),
            ("Profit factor", "profit_factor", 2, ""),
            ("Expectancy per trade", "expectancy", 2, ""),
            ("Average win", "average_win", 2, ""),
            ("Average loss", "average_loss", 2, ""),
            ("Best trade", "best_trade", 2, ""),
            ("Worst trade", "worst_trade", 2, ""),
            ("Average holding period", "average_holding_days", 1, " days"),
            ("Average exposure", "exposure_pct", 1, "%"),
            ("Annualised turnover", "turnover_pct", 1, "%"),
        ]
    )

    comparison_block = ""
    if comparison.get("available"):
        comparison_block = f"""
<h2>Versus benchmark</h2>
<table><tbody>
<tr><th>Excess total return</th><td>{_signed(comparison.get("excess_total_return_pct"), 2, "%")}</td></tr>
<tr><th>Excess CAGR</th><td>{_signed(comparison.get("excess_cagr_pct"), 2, "%")}</td></tr>
<tr><th>Sharpe difference</th><td>{_signed(comparison.get("sharpe_difference"), 3)}</td></tr>
<tr><th>Sortino difference</th><td>{_signed(comparison.get("sortino_difference"), 3)}</td></tr>
<tr><th>Drawdown difference<br><small style="color:#4a5468">positive = strategy drew down less</small></th>
    <td>{_signed(comparison.get("drawdown_difference_pct"), 2, "%")}</td></tr>
</tbody></table>"""
    elif benchmark:
        comparison_block = (
            f'<h2>Versus benchmark</h2><div class="note">No comparison available: '
            f'{html.escape(str(benchmark.get("reason", "unknown")))}</div>'
        )

    benchmark_caveats = ""
    if benchmark.get("caveats"):
        items = "".join(f"<li>{html.escape(c)}</li>" for c in benchmark["caveats"])
        benchmark_caveats = (
            f'<div class="note"><strong>Benchmark caveats '
            f'({html.escape(str(benchmark.get("label", "")))}):</strong>'
            f"<ul>{items}</ul></div>"
        )

    # ---- Trades table -------------------------------------------------
    if result.trades:
        trade_body = "".join(
            f"<tr><td>{html.escape(t.symbol)}</td>"
            f"<td>{t.entry_date:%Y-%m-%d}</td><td>{_num(t.entry_price, 4)}</td>"
            f"<td>{t.exit_date:%Y-%m-%d}</td><td>{_num(t.exit_price, 4)}</td>"
            f"<td>{_num(t.quantity, 0)}</td>"
            f"<td>{_signed(t.pnl, 2)}</td><td>{_signed(t.pnl_pct, 2, '%')}</td>"
            f"<td>{t.holding_period_days}</td>"
            f"<td style='text-align:left'>{html.escape(t.exit_reason[:60])}</td></tr>"
            for t in result.trades
        )
        trades_block = f"""
<h2>Every trade ({len(result.trades)})</h2>
<div class="scroll"><table>
<thead><tr><th>Symbol</th><th>Entry</th><th>Price</th><th>Exit</th><th>Price</th>
<th>Qty</th><th>Net P&amp;L</th><th>%</th><th>Days</th>
<th style="text-align:left">Exit reason</th></tr></thead>
<tbody>{trade_body}</tbody></table></div>"""
    else:
        trades_block = (
            '<h2>Trades</h2><div class="chart-empty">No trades were taken in this window.'
            "</div>"
        )

    rejected_block = ""
    if result.rejected_entries:
        items = "".join(
            f"<tr><th>{html.escape(reason)}</th><td>{count}</td></tr>"
            for reason, count in sorted(result.rejected_entries.items(), key=lambda kv: -kv[1])
        )
        rejected_block = f"""
<h2>Entries not taken</h2>
<p class="sub">A strategy constantly blocked by its own limits is telling you something
the summary metrics will not.</p>
<table><tbody>{items}</tbody></table>"""

    limitations = "".join(f"<li>{html.escape(item)}</li>" for item in result.limitations)

    params = result.strategy.get("params", {})
    param_rows = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in params.items()
    )
    cost_rows = "".join(
        f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in result.cost_model.items()
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest &mdash; {html.escape(result.strategy.get("name", "strategy"))} &mdash; {html.escape(result.config.market)}</title>
<style>{_CSS}</style>
</head><body><div class="wrap">

<h1>{html.escape(result.config.label or "Backtest report")}</h1>
<p class="sub">Generated {stamp:%Y-%m-%d %H:%M} UTC &middot; quant-trader</p>
<div class="badges">{''.join(badges)}</div>

{split_note}

<h2>Performance</h2>
<table>
<thead><tr><th>Metric</th><th>Strategy</th><th>Benchmark</th></tr></thead>
<tbody>{performance}</tbody>
</table>
{benchmark_caveats}

{_line_chart(equity, title="Equity curve", colour="#4d9fff", fill="#4d9fff")}
{_line_chart(drawdown, title="Drawdown (%)", colour="#ff5c7c", fill="#ff5c7c")}
{_bar_chart(monthly_returns(equity), title="Monthly returns (%)")}
{_bar_chart(annual_returns(equity), title="Annual returns (%) — first and last years are partial")}

<h2>Trade statistics</h2>
<table><tbody>{trade_rows}</tbody></table>

{comparison_block}
{rejected_block}
{trades_block}

<h2>Strategy parameters</h2>
<table><tbody>{param_rows}</tbody></table>

<h2>Cost assumptions</h2>
<p class="sub">These are configured values, not a broker's actual schedule.</p>
<table><tbody>{cost_rows}</tbody></table>

<h2>Limitations</h2>
<div class="limits"><ul>{limitations}</ul></div>

<footer>
Every figure in this document describes one historical sample under the cost assumptions
stated above. None is a forecast, and none implies a probability of any future outcome.
Data source: {html.escape(", ".join(sorted(set(result.universe))[:6]))}
{"&hellip;" if len(result.universe) > 6 else ""} via the configured free providers.
</footer>

</div></body></html>"""


def generate_report(
    result,
    *,
    output_dir: Path | None = None,
    filename: str | None = None,
) -> Path:
    """Render ``result`` and write it to disk. Returns the path written."""
    directory = output_dir or REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)

    if filename is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        strategy = str(result.strategy.get("name", "strategy")).replace(" ", "_")
        filename = f"{stamp}_{strategy}_{result.config.market}_{result.config.split}.html"

    path = directory / filename
    path.write_text(render_report(result), encoding="utf-8")
    logger.info("Wrote backtest report to %s", path)
    return path
