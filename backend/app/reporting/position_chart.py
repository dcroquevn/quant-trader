"""The large, annotated price chart for a position you actually hold.

This is the one chart in the project that answers a question about *your money* rather than
about the market, so it gets the space and the explanation the others do not.

What it draws, and why each element is there:

* **The price since before you bought**, so the entry has context rather than appearing at the
  left edge of a chart that starts the day you acted.
* **Your entry**, as a line and a marker. Not the signal price -- what you paid.
* **The stop and the target**, as bands rather than bare lines. A band shows that crossing one is
  a region you are in, not an instant you might miss between two daily closes.
* **The strategy's trend line (EMA50)**, because a trend break is the exit that fires most often
  in backtests -- more often than the stop and the target combined -- and a chart that drew the
  two price levels and omitted it would misrepresent how positions actually end.

Interaction, and its limits: a touch or hover anywhere shows the date, the close, and the
unrealised return at that point. There is no zoom and no range selector. Both are possible, but
they need enough script that the page stops being a single self-contained file that opens from a
``file://`` URL on a plane -- which is the property that makes the digest reach the user at all.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

from app.reporting.charts import AXIS, CAUTION, GAIN, LOSS, MUTED, SERIES, SURFACE

__all__ = ["PositionChartData", "position_chart"]


def _esc(value: object) -> str:
    return html.escape(str(value))


@dataclass(frozen=True, slots=True)
class PositionChartData:
    """Everything the chart needs, already resolved by the caller."""

    symbol: str
    name: str
    points: list[tuple[str, float]]
    """(date, close), oldest first."""

    entry_price: float
    entry_date: str
    stop_price: float | None
    take_profit_price: float | None
    quantity: float
    trend: list[float] | None = None
    """EMA50 aligned to ``points``. The trend-break exit is what it makes visible."""

    exit_triggered_on: str | None = None
    exit_reason: str = ""


def position_chart(data: PositionChartData, *, width: float = 460.0, height: float = 260.0) -> str:
    """A large annotated chart with a touch/hover readout.

    The y-axis always includes the stop and the target when they exist. Scaling to the price
    alone would push them off-screen exactly when they matter -- a position near its stop would
    draw a chart that does not show the stop.
    """
    if len(data.points) < 2:
        return (
            f'<figure class="chart"><figcaption>{_esc(data.symbol)}</figcaption>'
            '<p class="chart-empty">Not enough price history to draw this yet.</p></figure>'
        )

    pad_l, pad_r, pad_t, pad_b = 6.0, 54.0, 12.0, 20.0
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    closes = [c for _, c in data.points]
    anchors = [v for v in (data.stop_price, data.take_profit_price, data.entry_price) if v]
    lo, hi = min(closes + anchors), max(closes + anchors)
    span = (hi - lo) or 1.0
    lo, hi = lo - span * 0.06, hi + span * 0.06
    span = hi - lo

    def y_of(value: float) -> float:
        return pad_t + plot_h * (1.0 - (value - lo) / span)

    def x_of(i: int) -> float:
        return pad_l + plot_w * (i / (len(data.points) - 1))

    layers: list[str] = []

    # Bands first, so the price line draws over them. A band rather than a line because
    # crossing a level is a region you end up in, not an instant between two daily closes.
    if data.stop_price:
        y = y_of(data.stop_price)
        layers.append(
            f'<rect x="{pad_l}" y="{y:.2f}" width="{plot_w:.2f}" '
            f'height="{max(0.0, height - pad_b - y):.2f}" fill="{LOSS}" opacity="0.07" />'
            f'<line x1="{pad_l}" y1="{y:.2f}" x2="{pad_l + plot_w:.2f}" y2="{y:.2f}" '
            f'stroke="{LOSS}" stroke-width="1" stroke-dasharray="4 3" />'
            f'<text x="{pad_l + plot_w + 5:.2f}" y="{y + 3:.2f}" class="bl" fill="{LOSS}">'
            f"stop {data.stop_price:,.0f}</text>"
        )
    if data.take_profit_price:
        y = y_of(data.take_profit_price)
        layers.append(
            f'<rect x="{pad_l}" y="{pad_t:.2f}" width="{plot_w:.2f}" '
            f'height="{max(0.0, y - pad_t):.2f}" fill="{GAIN}" opacity="0.07" />'
            f'<line x1="{pad_l}" y1="{y:.2f}" x2="{pad_l + plot_w:.2f}" y2="{y:.2f}" '
            f'stroke="{GAIN}" stroke-width="1" stroke-dasharray="4 3" />'
            f'<text x="{pad_l + plot_w + 5:.2f}" y="{y + 3:.2f}" class="bl" fill="{GAIN}">'
            f"target {data.take_profit_price:,.0f}</text>"
        )

    entry_y = y_of(data.entry_price)
    layers.append(
        f'<line x1="{pad_l}" y1="{entry_y:.2f}" x2="{pad_l + plot_w:.2f}" y2="{entry_y:.2f}" '
        f'stroke="{MUTED}" stroke-width="1" />'
        f'<text x="{pad_l + plot_w + 5:.2f}" y="{entry_y + 3:.2f}" class="bl">'
        f"you {data.entry_price:,.0f}</text>"
    )

    if data.trend and len(data.trend) == len(data.points):
        pts = " ".join(
            f"{x_of(i):.2f},{y_of(v):.2f}"
            for i, v in enumerate(data.trend)
            if v is not None and v == v  # skip NaN
        )
        if pts:
            layers.append(
                f'<polyline points="{pts}" fill="none" stroke="{SERIES[0]}" '
                f'stroke-width="1.5" stroke-dasharray="2 3" opacity="0.8" />'
            )

    price_pts = " ".join(f"{x_of(i):.2f},{y_of(c):.2f}" for i, c in enumerate(closes))
    last_up = closes[-1] >= data.entry_price
    layers.append(
        f'<polyline points="{price_pts}" fill="none" stroke="{GAIN if last_up else LOSS}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />'
    )

    # The entry marker, placed on the bar the position opened.
    entry_i = next(
        (i for i, (d, _) in enumerate(data.points) if d >= data.entry_date), None
    )
    if entry_i is not None:
        layers.append(
            f'<circle cx="{x_of(entry_i):.2f}" cy="{entry_y:.2f}" r="4" '
            f'fill="{SURFACE}" stroke="{MUTED}" stroke-width="2" />'
        )

    if data.exit_triggered_on:
        exit_i = next(
            (i for i, (d, _) in enumerate(data.points) if d >= data.exit_triggered_on), None
        )
        if exit_i is not None:
            x = x_of(exit_i)
            layers.append(
                f'<line x1="{x:.2f}" y1="{pad_t:.2f}" x2="{x:.2f}" y2="{height - pad_b:.2f}" '
                f'stroke="{CAUTION}" stroke-width="1.5" />'
                f'<text x="{x + 4:.2f}" y="{pad_t + 8:.2f}" class="bt" fill="{CAUTION}">'
                f"exit rule fired</text>"
            )

    # The readout layer. One invisible full-height band per point, so a touch anywhere in a
    # column registers -- a hit target the width of the line itself is unusable on a phone.
    series = json.dumps(
        [
            {"d": d, "c": round(c, 4), "r": round((c / data.entry_price - 1) * 100, 2)}
            for d, c in data.points
        ]
    )
    slot = plot_w / max(1, len(data.points) - 1)
    hits = "".join(
        f'<rect class="hit" data-i="{i}" x="{x_of(i) - slot / 2:.2f}" y="{pad_t:.2f}" '
        f'width="{slot:.2f}" height="{plot_h:.2f}" fill="transparent" />'
        for i in range(len(data.points))
    )

    chart_id = f"pc{abs(hash((data.symbol, data.entry_date))) % 10**8}"
    return f"""
<figure class="chart poschart" id="{chart_id}">
  <figcaption>{_esc(data.symbol)} &mdash; since before you bought</figcaption>
  <div class="readout" data-default="Touch the chart to read a date">
    Touch the chart to read a date
  </div>
  <svg viewBox="0 0 {width:.0f} {height:.0f}" role="img"
       aria-label="{_esc(data.symbol)} price with your entry, stop and target">
    <line x1="{pad_l}" y1="{height - pad_b:.2f}" x2="{pad_l + plot_w:.2f}"
          y2="{height - pad_b:.2f}" stroke="{AXIS}" stroke-width="1" />
    {''.join(layers)}
    <g class="cursor" style="display:none">
      <line y1="{pad_t:.2f}" y2="{height - pad_b:.2f}" stroke="{MUTED}" stroke-width="1" />
      <circle r="4" fill="{SURFACE}" stroke="{MUTED}" stroke-width="2" />
    </g>
    {hits}
    <text x="{pad_l}" y="{height - 5:.2f}" class="bt">{_esc(data.points[0][0])}</text>
    <text x="{pad_l + plot_w:.2f}" y="{height - 5:.2f}" text-anchor="end" class="bt">
      {_esc(data.points[-1][0])}</text>
  </svg>
  <script type="application/json" class="pcdata">{series}</script>
</figure>"""


READOUT_SCRIPT = """
// Touch/hover readout for the position charts.
//
// Inline and dependency-free on purpose: the digest has to open from a file:// URL and from a
// phone with no network, which rules out every charting library. This is the smallest thing
// that makes the chart answer "what was it worth on that day".
//
// Pointer events rather than mouse events, so one code path covers finger and cursor. The hit
// targets are full-height columns, because a target the width of a 2px line cannot be hit with
// a thumb.
(function () {
  document.querySelectorAll('.poschart').forEach(function (fig) {
    var raw = fig.querySelector('.pcdata');
    var out = fig.querySelector('.readout');
    var svg = fig.querySelector('svg');
    var cursor = fig.querySelector('.cursor');
    if (!raw || !out || !svg || !cursor) return;

    var points;
    try { points = JSON.parse(raw.textContent); } catch (e) { return; }
    var line = cursor.querySelector('line');
    var dot = cursor.querySelector('circle');
    var price = svg.querySelector('polyline[stroke-width="2"]');
    var coords = price ? price.getAttribute('points').split(' ') : [];

    function show(i) {
      var p = points[i];
      if (!p) return;
      var sign = p.r > 0 ? '+' : '';
      out.textContent = p.d + '  ·  ' + p.c.toLocaleString(undefined, {
        minimumFractionDigits: 2, maximumFractionDigits: 2
      }) + '  ·  ' + sign + p.r.toFixed(2) + '% vs your entry';
      out.classList.toggle('up', p.r >= 0);
      out.classList.toggle('down', p.r < 0);

      var xy = (coords[i] || '').split(',');
      if (xy.length === 2) {
        line.setAttribute('x1', xy[0]); line.setAttribute('x2', xy[0]);
        dot.setAttribute('cx', xy[0]); dot.setAttribute('cy', xy[1]);
        cursor.style.display = '';
      }
    }

    function clear() {
      cursor.style.display = 'none';
      out.textContent = out.dataset.default;
      out.classList.remove('up', 'down');
    }

    fig.querySelectorAll('.hit').forEach(function (hit) {
      var i = parseInt(hit.dataset.i, 10);
      hit.addEventListener('pointerenter', function () { show(i); });
      hit.addEventListener('pointerdown', function () { show(i); });
    });
    svg.addEventListener('pointerleave', clear);
  });
})();
"""
