/**
 * Chart primitives.
 *
 * Every chart in the dashboard is built from these, so the rules hold everywhere
 * instead of being re-decided per page:
 *
 * **Colour follows the job, not the rank.** Series identity uses the categorical
 * palette in fixed slot order (`SERIES`), validated against the panel surface.
 * Signed magnitudes use the status pair (gain/loss) and always carry a second
 * encoding — position relative to the zero line — so meaning never rests on colour.
 *
 * **One axis, always.** No component here accepts a second y-scale. Two measures of
 * different scale get two charts or a common index; a dual-axis chart lets the author
 * imply any correlation they like by sliding one scale.
 *
 * **Hover by default.** A crosshair and tooltip on time series, per-mark tooltips on
 * bars. An SVG chart in a browser is interactive; shipping it inert wastes the medium.
 *
 * **A table view exists.** Colour and position are not accessible to everyone, and a
 * reader who wants the number should not have to hover for it.
 */

import { Table2, TrendingUp } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { DASH, num } from '../lib/format';

/* ------------------------------------------------------------------ */
/* Tokens                                                             */
/* ------------------------------------------------------------------ */

/**
 * Categorical slots, fixed order. Validated on the #141924 panel surface:
 * lightness band, chroma floor, adjacent CVD separation (worst 8.4 protan),
 * normal-vision floor (19.8) and 3:1 contrast all pass. The first three also pass
 * all-pairs. A fifth series folds into "Other" — never a generated hue.
 */
export const SERIES = ['#3987e5', '#d95926', '#199e70', '#c98500'] as const;

/** Status pair. Reserved for sign, never for identity. */
export const GAIN = '#26d98a';
export const LOSS = '#ff5c7c';

/** Recessive chrome, so the data is the most prominent thing on screen. */
const GRID = '#1a2030';
const AXIS_TEXT = '#8a94a8';
const INK = '#e2e8f0';

const axisTick = { fill: AXIS_TEXT, fontSize: 10 };

/** Compact month-year label, e.g. "24-03". Keeps a dense x-axis readable. */
function shortDate(value: string): string {
  return typeof value === 'string' && value.length >= 7 ? value.slice(2, 7) : String(value);
}

/* ------------------------------------------------------------------ */
/* Shared frame                                                       */
/* ------------------------------------------------------------------ */

export interface SeriesSpec {
  key: string;
  label: string;
  /** Omit to take the next categorical slot in order. */
  colour?: string;
  /** Primary ink rather than a hue: for the subject of a chart about one entity. */
  ink?: boolean;
  width?: number;
  dashed?: boolean;
}

function resolveColour(spec: SeriesSpec, index: number): string {
  if (spec.ink) return INK;
  return spec.colour ?? SERIES[index % SERIES.length];
}

/**
 * Chart frame: title, optional note, a legend for two or more series, and a
 * table-view toggle.
 *
 * The legend is unconditional above two series because identity must never rest on
 * colour alone. A single series needs none — the title names it.
 */
export function ChartFrame({
  title,
  note,
  series,
  rows,
  columns,
  children,
  height = 240,
}: {
  title: string;
  note?: ReactNode;
  series?: SeriesSpec[];
  /** Table-view rows. Omit to hide the toggle. */
  rows?: Array<Record<string, string | number | null>>;
  columns?: Array<{ key: string; label: string; digits?: number }>;
  children: ReactNode;
  height?: number;
}) {
  const [asTable, setAsTable] = useState(false);
  const showToggle = Boolean(rows && columns);
  const showLegend = (series?.length ?? 0) >= 2;

  return (
    <section className="card">
      <header className="card-header">
        <div className="min-w-0">
          <h3 className="truncate text-xs font-semibold uppercase tracking-wider text-slate-400">
            {title}
          </h3>
          {note && <p className="mt-1 text-2xs leading-snug text-slate-500">{note}</p>}
        </div>
        {showToggle && (
          <button
            type="button"
            onClick={() => setAsTable((v) => !v)}
            title={asTable ? 'Show chart' : 'Show the underlying numbers'}
            className="inline-flex shrink-0 items-center gap-1 rounded border border-terminal-700 px-2 py-1 text-2xs text-slate-400 transition hover:bg-terminal-800 hover:text-slate-200"
          >
            {asTable ? <TrendingUp className="h-3 w-3" /> : <Table2 className="h-3 w-3" />}
            {asTable ? 'Chart' : 'Table'}
          </button>
        )}
      </header>

      <div className="p-3">
        {asTable && rows && columns ? (
          <div className="max-h-[320px] overflow-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th key={c.key} className="th">
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => (
                  <tr key={i} className="hover:bg-terminal-850">
                    {columns.map((c) => {
                      const value = row[c.key];
                      return (
                        <td key={c.key} className="td font-mono tnum text-slate-300">
                          {value === null || value === undefined
                            ? DASH
                            : typeof value === 'number'
                              ? num(value, c.digits ?? 2)
                              : value}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div style={{ height }}>{children}</div>
        )}

        {showLegend && !asTable && (
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
            {series!.map((spec, i) => (
              <span key={spec.key} className="flex items-center gap-1.5 text-2xs text-slate-400">
                <span
                  className="h-0.5 w-4 shrink-0 rounded"
                  style={{
                    background: resolveColour(spec, i),
                    ...(spec.dashed ? { backgroundImage: 'none', opacity: 0.85 } : {}),
                  }}
                />
                {spec.label}
              </span>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* Tooltip                                                            */
/* ------------------------------------------------------------------ */

interface TooltipPayload {
  name?: string;
  value?: number | string;
  color?: string;
  dataKey?: string | number;
}

function VizTooltip({
  active,
  payload,
  label,
  digits = 2,
  suffix = '',
  formatLabel,
}: {
  active?: boolean;
  payload?: TooltipPayload[];
  label?: string | number;
  digits?: number;
  suffix?: string;
  formatLabel?: (value: string) => string;
}) {
  if (!active || !payload?.length) return null;
  const heading = formatLabel ? formatLabel(String(label)) : String(label ?? '');

  return (
    <div className="pointer-events-none rounded border border-terminal-700 bg-terminal-950/96 px-2.5 py-2 text-2xs shadow-lg">
      <div className="mb-1 font-mono text-slate-400">{heading}</div>
      {payload.map((entry, i) => (
        <div key={`${entry.dataKey}-${i}`} className="flex items-center justify-between gap-4">
          <span className="flex items-center gap-1.5 text-slate-400">
            <span
              className="h-1.5 w-1.5 shrink-0 rounded-full"
              style={{ background: entry.color }}
            />
            {entry.name}
          </span>
          <span className="font-mono tnum text-slate-100">
            {typeof entry.value === 'number' ? `${num(entry.value, digits)}${suffix}` : DASH}
          </span>
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Time series                                                        */
/* ------------------------------------------------------------------ */

/**
 * Multi-series line chart with a crosshair and shared tooltip.
 *
 * All series share one y-axis, deliberately. If two measures do not belong on the
 * same scale they belong in two charts.
 */
export function TimeSeriesChart({
  data,
  xKey = 'ts',
  series,
  title,
  note,
  height = 260,
  digits = 2,
  suffix = '',
  zeroLine = false,
  fillFirst = false,
  tableColumns,
}: {
  data: Array<Record<string, string | number | null>>;
  xKey?: string;
  series: SeriesSpec[];
  title: string;
  note?: ReactNode;
  height?: number;
  digits?: number;
  suffix?: string;
  zeroLine?: boolean;
  fillFirst?: boolean;
  tableColumns?: Array<{ key: string; label: string; digits?: number }>;
}) {
  const columns =
    tableColumns ??
    [
      { key: xKey, label: 'Date' },
      ...series.map((s) => ({ key: s.key, label: s.label, digits })),
    ];

  if (!data.length) {
    return (
      <ChartFrame title={title} note={note} height={height}>
        <EmptyChart message="No data for this window" />
      </ChartFrame>
    );
  }

  return (
    <ChartFrame
      title={title}
      note={note}
      series={series}
      rows={data}
      columns={columns}
      height={height}
    >
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 6, right: 10, bottom: 0, left: 4 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis
            dataKey={xKey}
            tick={axisTick}
            tickFormatter={shortDate}
            minTickGap={44}
            stroke={GRID}
          />
          <YAxis
            tick={axisTick}
            width={60}
            stroke={GRID}
            domain={['auto', 'auto']}
            tickFormatter={(v: number) => num(v, v >= 1000 || v <= -1000 ? 0 : digits)}
          />
          <Tooltip
            content={<VizTooltip digits={digits} suffix={suffix} />}
            cursor={{ stroke: '#465575', strokeWidth: 1, strokeDasharray: '3 3' }}
          />
          {zeroLine && <ReferenceLine y={0} stroke={AXIS_TEXT} strokeWidth={1} />}
          {series.map((spec, i) => {
            const colour = resolveColour(spec, i);
            return (
              <Line
                key={spec.key}
                type="monotone"
                dataKey={spec.key}
                name={spec.label}
                stroke={colour}
                strokeWidth={spec.width ?? 2}
                strokeDasharray={spec.dashed ? '4 3' : undefined}
                dot={false}
                // Filling under the first series reads as "this is the subject";
                // filling several would stack visually and imply a total.
                fill={fillFirst && i === 0 ? colour : undefined}
                fillOpacity={fillFirst && i === 0 ? 0.12 : 0}
                connectNulls={false}
                isAnimationActive={false}
              />
            );
          })}
        </ComposedChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

/* ------------------------------------------------------------------ */
/* Signed bars                                                        */
/* ------------------------------------------------------------------ */

/**
 * Signed bar chart: gains above the zero line, losses below.
 *
 * Colour and position agree, which is the secondary encoding that makes the status
 * pair legitimate here — a reader who cannot separate the hues still sees the side
 * of the line each bar is on.
 */
export function SignedBarChart({
  data,
  xKey,
  valueKey,
  title,
  note,
  height = 200,
  digits = 2,
  suffix = '%',
  labelName = 'Return',
}: {
  data: Array<Record<string, string | number | null>>;
  xKey: string;
  valueKey: string;
  title: string;
  note?: ReactNode;
  height?: number;
  digits?: number;
  suffix?: string;
  labelName?: string;
}) {
  if (!data.length) {
    return (
      <ChartFrame title={title} note={note} height={height}>
        <EmptyChart message="No completed periods yet" />
      </ChartFrame>
    );
  }

  return (
    <ChartFrame
      title={title}
      note={note}
      rows={data}
      columns={[
        { key: xKey, label: 'Period' },
        { key: valueKey, label: `${labelName} ${suffix}`.trim(), digits },
      ]}
      height={height}
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 6, right: 10, bottom: 0, left: 4 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey={xKey} tick={axisTick} minTickGap={24} stroke={GRID} />
          <YAxis
            tick={axisTick}
            width={48}
            stroke={GRID}
            tickFormatter={(v: number) => num(v, 0)}
          />
          <Tooltip
            content={<VizTooltip digits={digits} suffix={suffix} />}
            cursor={{ fill: '#1a2030', opacity: 0.5 }}
          />
          <ReferenceLine y={0} stroke={AXIS_TEXT} strokeWidth={1} />
          <Bar
            dataKey={valueKey}
            name={labelName}
            // 4px rounded data-end anchored to the baseline; the sign decides which
            // end is rounded.
            radius={[3, 3, 0, 0]}
            isAnimationActive={false}
            // Recharts types the custom shape as (props: unknown) => Element, so the
            // narrowing happens here rather than in the signature.
            shape={(raw: unknown) => {
              const { x, y, width, height: h, value } = raw as {
                x: number;
                y: number;
                width: number;
                height: number;
                value: number;
              };
              const positive = (value ?? 0) >= 0;
              // 2px surface gap between adjacent bars.
              const gap = Math.min(2, width * 0.25);
              const w = Math.max(1, width - gap);
              const radius = Math.min(3, w / 2, Math.abs(h));
              const top = positive ? y : y + h;
              const path = positive
                ? `M${x + gap / 2},${y + h} L${x + gap / 2},${top + radius} Q${x + gap / 2},${top} ${x + gap / 2 + radius},${top} L${x + gap / 2 + w - radius},${top} Q${x + gap / 2 + w},${top} ${x + gap / 2 + w},${top + radius} L${x + gap / 2 + w},${y + h} Z`
                : `M${x + gap / 2},${y} L${x + gap / 2},${top - radius} Q${x + gap / 2},${top} ${x + gap / 2 + radius},${top} L${x + gap / 2 + w - radius},${top} Q${x + gap / 2 + w},${top} ${x + gap / 2 + w},${top - radius} L${x + gap / 2 + w},${y} Z`;
              return <path d={path} fill={positive ? GAIN : LOSS} />;
            }}
          />
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

/* ------------------------------------------------------------------ */
/* Histogram                                                          */
/* ------------------------------------------------------------------ */

export interface HistogramBin {
  label: string;
  count: number;
  midpoint: number;
}

/** Equal-width bins over ``values``. Returns [] when there is nothing to bin. */
export function buildHistogram(values: number[], binCount = 20): HistogramBin[] {
  const finite = values.filter((v) => Number.isFinite(v));
  if (finite.length < 2) return [];

  const lowest = Math.min(...finite);
  const highest = Math.max(...finite);
  if (highest === lowest) {
    return [{ label: num(lowest, 1), count: finite.length, midpoint: lowest }];
  }

  const width = (highest - lowest) / binCount;
  const bins: HistogramBin[] = Array.from({ length: binCount }, (_, i) => {
    const from = lowest + i * width;
    return { label: num(from + width / 2, 1), count: 0, midpoint: from + width / 2 };
  });

  for (const value of finite) {
    const index = Math.min(binCount - 1, Math.floor((value - lowest) / width));
    bins[index].count += 1;
  }
  return bins;
}

/**
 * Distribution of signed values — trade P&L, most often.
 *
 * Bars are coloured by the sign of their bin, so the loss and gain halves separate
 * at a glance while the x-position still carries the magnitude.
 */
export function DistributionChart({
  values,
  title,
  note,
  height = 200,
  binCount = 20,
  suffix = '%',
}: {
  values: number[];
  title: string;
  note?: ReactNode;
  height?: number;
  binCount?: number;
  suffix?: string;
}) {
  const bins = buildHistogram(values, binCount);

  if (!bins.length) {
    return (
      <ChartFrame title={title} note={note} height={height}>
        <EmptyChart message="Not enough trades to show a distribution" />
      </ChartFrame>
    );
  }

  return (
    <ChartFrame
      title={title}
      note={note}
      rows={bins.map((b) => ({ label: b.label, count: b.count }))}
      columns={[
        { key: 'label', label: `Bin midpoint ${suffix}` },
        { key: 'count', label: 'Trades', digits: 0 },
      ]}
      height={height}
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={bins} margin={{ top: 6, right: 10, bottom: 0, left: 4 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="label" tick={axisTick} minTickGap={18} stroke={GRID} />
          <YAxis tick={axisTick} width={36} stroke={GRID} allowDecimals={false} />
          <Tooltip
            content={<VizTooltip digits={0} formatLabel={(v) => `Bin ${v}${suffix}`} />}
            cursor={{ fill: '#1a2030', opacity: 0.5 }}
          />
          <ReferenceLine x={bins.find((b) => b.midpoint >= 0)?.label} stroke={AXIS_TEXT} />
          <Bar dataKey="count" name="Trades" isAnimationActive={false} radius={[3, 3, 0, 0]}>
            {bins.map((bin) => (
              <Cell key={bin.label} fill={bin.midpoint >= 0 ? GAIN : LOSS} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */

export function EmptyChart({ message }: { message: string }) {
  return (
    <div className="flex h-full items-center justify-center text-2xs text-slate-600">
      {message}
    </div>
  );
}

/**
 * Horizontal proportion bar for a two-part split (wins vs losses).
 *
 * Not a pie: two quantities compared against a whole read faster as one bar, and the
 * labels sit inline rather than in a legend.
 */
export function ProportionBar({
  label,
  positive,
  negative,
  positiveLabel = 'Wins',
  negativeLabel = 'Losses',
}: {
  label: string;
  positive: number;
  negative: number;
  positiveLabel?: string;
  negativeLabel?: string;
}) {
  const total = positive + negative;
  const share = total > 0 ? (positive / total) * 100 : 0;

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="label">{label}</span>
        <span className="font-mono text-2xs tnum text-slate-400">
          {total > 0 ? `${num(share, 1)}%` : DASH}
        </span>
      </div>
      <div className="mt-1.5 flex h-2 gap-0.5 overflow-hidden rounded">
        {/* 2px surface gap between segments, from the flex gap above. */}
        <div style={{ width: `${share}%`, background: GAIN }} title={`${positiveLabel}: ${positive}`} />
        <div
          style={{ width: `${100 - share}%`, background: LOSS }}
          title={`${negativeLabel}: ${negative}`}
        />
      </div>
      <div className="mt-1 flex justify-between text-2xs text-slate-500">
        <span>
          {positiveLabel} {positive}
        </span>
        <span>
          {negativeLabel} {negative}
        </span>
      </div>
    </div>
  );
}

/** Small inline sparkline, for a table cell or a stat tile. */
export function Sparkline({
  values,
  width = 88,
  height = 22,
  colour,
}: {
  values: number[];
  width?: number;
  height?: number;
  colour?: string;
}) {
  const finite = values.filter((v) => Number.isFinite(v));
  if (finite.length < 2) return <span className="text-2xs text-slate-700">{DASH}</span>;

  const lowest = Math.min(...finite);
  const highest = Math.max(...finite);
  const span = highest - lowest || Math.abs(highest) || 1;
  const step = width / (finite.length - 1);
  const points = finite
    .map((v, i) => `${(i * step).toFixed(1)},${(height - ((v - lowest) / span) * height).toFixed(1)}`)
    .join(' ');

  const rising = finite[finite.length - 1] >= finite[0];
  return (
    <svg width={width} height={height} className="block" aria-hidden>
      <polyline
        points={points}
        fill="none"
        stroke={colour ?? (rising ? GAIN : LOSS)}
        strokeWidth={1.4}
      />
    </svg>
  );
}
