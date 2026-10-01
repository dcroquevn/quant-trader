"""Inline-SVG chart primitives for the static digest.

Everything here emits self-contained SVG with no script and no external reference, because the
digest has to open from a file:// URL, from an email attachment, and from a phone with no network.
That rules out every charting library and is the reason these exist at all.

Design rules these follow, and why each one is not negotiable:

* **No ``preserveAspectRatio="none"``.** The older helpers in ``app.backtesting.report`` use it,
  which stretches marks non-uniformly: a 2px stroke becomes 2px vertically and 6px horizontally,
  and round caps turn into ellipses. Here the viewBox scales uniformly.
* **4px rounded data-ends, anchored to the baseline.** A bar is rounded on the end that carries
  the value and square on the end that sits on the axis, so length stays readable.
* **A 2px surface-coloured gap between adjacent marks**, rather than relying on a fill difference.
* **Direct labels, selectively.** A number on every mark is noise; a number on the ones a reader
  will look up is the point.
* **Text never wears the series colour.** Identity is carried by the mark beside it.

Colour comes from the project palette in ``frontend/tailwind.config.js``, which was validated
against the ``#141924`` panel surface. The three categorical steps used for regions were
re-validated for this module: lightness band, chroma floor, all-pairs CVD separation (worst ΔE 9.4
deutan), normal-vision floor (20.9) and 3:1 contrast all pass.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

__all__ = [
    "SURFACE",
    "SERIES",
    "GAIN",
    "LOSS",
    "CAUTION",
    "MUTED",
    "Bar",
    "horizontal_bars",
    "indexed_lines",
    "sparkline",
]

# --------------------------------------------------------------------------- #
# Palette. Mirrors frontend/tailwind.config.js; see the module docstring.
# --------------------------------------------------------------------------- #

SURFACE = "#141924"
SERIES = ("#3987e5", "#d95926", "#199e70")
"""Categorical, fixed order, never cycled. Three regions, three steps."""

GAIN = "#26d98a"
LOSS = "#ff5c7c"
CAUTION = "#ffb84d"
MUTED = "#8a94a8"
AXIS = "#2f3a52"

GAP = 2.0
"""Surface-coloured gap between adjacent marks, in user units."""

RADIUS = 4.0
"""Corner radius on the data-carrying end of a bar."""


def _esc(value: object) -> str:
    return html.escape(str(value))


def _rounded_h_bar(x: float, y: float, w: float, h: float, *, positive: bool, fill: str) -> str:
    """A horizontal bar rounded on its value end and square against the baseline.

    Drawn as a path rather than a ``<rect rx=…>`` because a rect rounds all four corners, which
    detaches the bar from its axis and makes short bars read as floating pills.
    """
    r = min(RADIUS, h / 2, max(w, 0.1))
    if w < 0.5:
        # Below a visible width, draw a hairline so the entity is not silently absent.
        return (
            f'<rect x="{x:.2f}" y="{y:.2f}" width="0.8" height="{h:.2f}" fill="{fill}" />'
        )
    if positive:
        return (
            f'<path d="M{x:.2f},{y:.2f} H{x + w - r:.2f} Q{x + w:.2f},{y:.2f} '
            f'{x + w:.2f},{y + r:.2f} V{y + h - r:.2f} Q{x + w:.2f},{y + h:.2f} '
            f'{x + w - r:.2f},{y + h:.2f} H{x:.2f} Z" fill="{fill}" />'
        )
    return (
        f'<path d="M{x + w:.2f},{y:.2f} H{x + r:.2f} Q{x:.2f},{y:.2f} {x:.2f},{y + r:.2f} '
        f'V{y + h - r:.2f} Q{x:.2f},{y + h:.2f} {x + r:.2f},{y + h:.2f} '
        f'H{x + w:.2f} Z" fill="{fill}" />'
    )


@dataclass(frozen=True, slots=True)
class Bar:
    """One row of a horizontal bar chart."""

    label: str
    value: float
    group: str = ""
    """Used for the legend and the mark colour when ``by_group`` is set."""

    detail: str = ""
    """Hover text. The phone has no hover, so anything essential goes in a direct label."""

    flagged: bool = False
    """Draws the label in the caution ink. Carries a second encoding via the detail text."""


def horizontal_bars(
    bars: list[Bar],
    *,
    title: str,
    value_suffix: str = "",
    diverging: bool = False,
    by_group: bool = False,
    group_colours: dict[str, str] | None = None,
    width: float = 460.0,
    row_height: float = 22.0,
    label_width: float = 54.0,
    value_width: float = 50.0,
    threshold: float | None = None,
    threshold_label: str = "",
    log_scale: bool = False,
) -> str:
    """Horizontal bars, one row per entity.

    Horizontal because the labels are instrument symbols: vertical bars would force them to rotate,
    and a rotated label is unreadable at phone width. One row per entity also means the chart grows
    downward rather than getting denser, which is the right direction on a narrow screen.

    ``diverging`` colours by sign, for returns. The sign is also carried by which side of the zero
    line the bar sits on and by the ``+``/``-`` in the direct label, so meaning never rests on
    colour alone.

    ``log_scale`` is for quantities spanning orders of magnitude -- turnover here runs from 1.7M to
    37 billion, and on a linear axis every instrument but SPY would be an invisible sliver.
    """
    if not bars:
        return f'<figure class="chart"><figcaption>{_esc(title)}</figcaption>' \
               f'<p class="chart-empty">No data.</p></figure>'

    pad_top, pad_bottom = 6.0, 6.0
    plot_x = label_width
    plot_w = width - label_width - value_width
    height = pad_top + pad_bottom + row_height * len(bars)

    values = [b.value for b in bars]

    if log_scale:
        import math

        safe = [max(v, 1.0) for v in values]
        logs = [math.log10(v) for v in safe]
        lo, hi = 0.0, max(logs) * 1.02
        def scale(v: float) -> float:
            return (math.log10(max(v, 1.0)) - lo) / (hi - lo) * plot_w
        zero_x = plot_x
    elif diverging:
        largest = max((abs(v) for v in values), default=1.0) or 1.0
        zero_x = plot_x + plot_w / 2
        def scale(v: float) -> float:
            return abs(v) / largest * (plot_w / 2)
    else:
        largest = max((abs(v) for v in values), default=1.0) or 1.0
        zero_x = plot_x
        def scale(v: float) -> float:
            return abs(v) / largest * plot_w

    colours = group_colours or {}
    marks: list[str] = []
    for i, bar in enumerate(bars):
        y = pad_top + i * row_height + GAP / 2
        h = row_height - GAP
        length = scale(bar.value)
        positive = bar.value >= 0

        if diverging:
            fill = GAIN if positive else LOSS
            x = zero_x if positive else zero_x - length
        elif by_group:
            fill = colours.get(bar.group, SERIES[0])
            x = zero_x
        else:
            fill = SERIES[0]
            x = zero_x

        label_ink = CAUTION if bar.flagged else MUTED
        detail = f"<title>{_esc(bar.detail or bar.label)}</title>" if bar.detail else ""
        marks.append(
            f'<g>{detail}'
            f'<text x="{label_width - 8:.2f}" y="{y + h / 2 + 3.5:.2f}" text-anchor="end" '
            f'class="bl" fill="{label_ink}">{_esc(bar.label)}</text>'
            + _rounded_h_bar(x, y, length, h, positive=positive, fill=fill)
            + f'<text x="{width - value_width + 8:.2f}" y="{y + h / 2 + 3.5:.2f}" '
            f'class="bv">{_esc(_fmt(bar.value, value_suffix, log_scale))}</text>'
            "</g>"
        )

    guides = [
        f'<line x1="{zero_x:.2f}" y1="{pad_top:.2f}" x2="{zero_x:.2f}" '
        f'y2="{height - pad_bottom:.2f}" stroke="{AXIS}" stroke-width="1" />'
    ]
    if threshold is not None:
        tx = zero_x + scale(threshold)
        guides.append(
            f'<line x1="{tx:.2f}" y1="{pad_top:.2f}" x2="{tx:.2f}" y2="{height - pad_bottom:.2f}" '
            f'stroke="{CAUTION}" stroke-width="1" stroke-dasharray="3 3" opacity="0.7" />'
        )
        if threshold_label:
            guides.append(
                f'<text x="{tx + 4:.2f}" y="{pad_top + 9:.2f}" class="bt" fill="{CAUTION}">'
                f"{_esc(threshold_label)}</text>"
            )

    legend = ""
    if by_group and colours:
        chips = "".join(
            f'<span class="chip"><i style="background:{c}"></i>{_esc(g)}</span>'
            for g, c in colours.items()
        )
        legend = f'<div class="legend">{chips}</div>'

    return f"""
<figure class="chart">
  <figcaption>{_esc(title)}</figcaption>
  {legend}
  <svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{_esc(title)}">
    {''.join(guides)}
    {''.join(marks)}
  </svg>
</figure>"""


def _fmt(value: float, suffix: str, log_scale: bool) -> str:
    if log_scale:
        if value >= 1_000_000_000:
            return f"{value / 1_000_000_000:.1f}B"
        if value >= 1_000_000:
            return f"{value / 1_000_000:.0f}M"
        return f"{value:,.0f}"
    return f"{value:+.1f}{suffix}" if suffix == "%" else f"{value:,.1f}{suffix}"


def indexed_lines(
    series: dict[str, list[tuple[str, float]]],
    *,
    title: str,
    width: float = 460.0,
    height: float = 215.0,
) -> str:
    """Several price series rebased to 100 at their first common point.

    Rebasing is what lets three instruments at $38, $187 and $764 share one axis. The alternative
    -- a second y-scale -- is the single worst chart mistake available: it lets the author place
    the crossover anywhere they like by choosing the scales.

    Each line is direct-labelled at its right end, so identity never depends on matching a colour
    to a legend entry, which is exactly what fails under colour-vision deficiency.
    """
    series = {k: v for k, v in series.items() if len(v) >= 2}
    if not series:
        return f'<figure class="chart"><figcaption>{_esc(title)}</figcaption>' \
               f'<p class="chart-empty">Not enough history.</p></figure>'

    pad_l, pad_r, pad_t, pad_b = 26.0, 52.0, 10.0, 16.0
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    rebased = {
        name: [(d, v / points[0][1] * 100.0) for d, v in points]
        for name, points in series.items()
        if points[0][1]
    }
    all_values = [v for pts in rebased.values() for _, v in pts]
    lo, hi = min(all_values), max(all_values)
    span = (hi - lo) or 1.0
    lo, hi = lo - span * 0.08, hi + span * 0.08
    span = hi - lo

    def y_of(v: float) -> float:
        return pad_t + plot_h * (1.0 - (v - lo) / span)

    # A reference line at 100: the level everything started from, so "above the line" means
    # "up since the start" without the reader doing arithmetic.
    base_y = y_of(100.0)
    grid = (
        f'<line x1="{pad_l:.2f}" y1="{base_y:.2f}" x2="{pad_l + plot_w:.2f}" y2="{base_y:.2f}" '
        f'stroke="{AXIS}" stroke-width="1" stroke-dasharray="4 4" />'
        f'<text x="{pad_l - 6:.2f}" y="{base_y + 3.5:.2f}" text-anchor="end" class="bt">100</text>'
    )

    # Two series finishing at nearly the same level would print their labels on top of each
    # other. Resolve by nudging, in finishing order, so the vertical order still matches the
    # lines' order at the right edge.
    ends = sorted(
        ((name, pts[-1][1]) for name, pts in rebased.items()), key=lambda kv: -kv[1]
    )
    label_y: dict[str, float] = {}
    previous = -1e9
    for name, value in ends:
        y = max(y_of(value), previous + 11.0)
        label_y[name] = y
        previous = y

    paths: list[str] = []
    for i, (name, points) in enumerate(rebased.items()):
        colour = SERIES[i % len(SERIES)]
        n = len(points)
        coords = [
            f"{pad_l + plot_w * (j / (n - 1)):.2f},{y_of(v):.2f}" for j, (_, v) in enumerate(points)
        ]
        last_v = points[-1][1]
        # The end label carries the ticker, not the full series name: "Emerging Asia (AAXJ) 134"
        # is three times the right margin and was being clipped mid-word. The legend above
        # carries the full name against the same colour, so identity is still not colour-alone.
        short = name[name.find("(") + 1 : name.find(")")] if "(" in name else name[:6]
        paths.append(
            f'<polyline points="{" ".join(coords)}" fill="none" stroke="{colour}" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />'
            f'<circle cx="{pad_l + plot_w:.2f}" cy="{y_of(last_v):.2f}" r="3.5" fill="{colour}" '
            f'stroke="{SURFACE}" stroke-width="2" />'
            f'<text x="{pad_l + plot_w + 7:.2f}" y="{label_y[name] + 3.5:.2f}" class="bl">'
            f"{_esc(short)} {last_v:.0f}</text>"
        )

    first_date = next(iter(rebased.values()))[0][0]
    last_date = next(iter(rebased.values()))[-1][0]
    chips = "".join(
        f'<span class="chip"><i style="background:{SERIES[i % len(SERIES)]}"></i>{_esc(n)}</span>'
        for i, n in enumerate(rebased)
    )

    return f"""
<figure class="chart">
  <figcaption>{_esc(title)}</figcaption>
  <div class="legend">{chips}</div>
  <svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{_esc(title)}">
    {grid}
    {''.join(paths)}
    <text x="{pad_l:.2f}" y="{height - 4:.2f}" class="bt">{_esc(first_date)}</text>
    <text x="{pad_l + plot_w:.2f}" y="{height - 4:.2f}" text-anchor="end" class="bt">
      {_esc(last_date)}</text>
  </svg>
</figure>"""


def sparkline(values: list[float], *, width: float = 72.0, height: float = 20.0) -> str:
    """A bare trend line for a table cell. No axis, no labels -- shape only.

    Coloured by net direction over the window, with the direction also readable from the shape
    itself, so the colour is reinforcement rather than the only cue.
    """
    usable = [v for v in values if v is not None]
    if len(usable) < 2:
        return '<span class="null">—</span>'

    lo, hi = min(usable), max(usable)
    span = (hi - lo) or (abs(hi) or 1.0)
    pad = 2.0
    step = (width - 2 * pad) / (len(usable) - 1)
    coords = [
        f"{pad + i * step:.1f},{pad + (height - 2 * pad) * (1 - (v - lo) / span):.1f}"
        for i, v in enumerate(usable)
    ]
    colour = GAIN if usable[-1] >= usable[0] else LOSS
    return (
        f'<svg class="spark" viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
        f'aria-label="trend">'
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="{colour}" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" /></svg>'
    )
