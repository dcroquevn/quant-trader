import { useQuery } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { useMemo } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Link, useParams, useSearchParams } from 'react-router-dom';

import { Badge, Card, Caveat, ErrorState, Loading, Stat } from '../components/ui';
import { api } from '../lib/api';
import type { FeatureHistoryRow } from '../lib/api';
import { compact, date, marketFlag, num, pct, price, signClass } from '../lib/format';

const HISTORY_BARS = 320;

const GRID = '#1a2030';
const AXIS = '#465575';

/** Recharts tooltip, styled to match the terminal palette. */
function ChartTooltip({
  active,
  payload,
  label,
  currency,
}: {
  active?: boolean;
  payload?: Array<{ name?: string; value?: number | string; color?: string }>;
  label?: string | number;
  currency?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded border border-terminal-700 bg-terminal-950/95 px-2.5 py-2 text-2xs shadow-lg">
      <div className="mb-1 font-mono text-slate-400">{date(String(label))}</div>
      {payload.map((entry) => (
        <div key={entry.name} className="flex items-center justify-between gap-3">
          <span className="flex items-center gap-1.5" style={{ color: entry.color }}>
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: entry.color }} />
            {entry.name}
          </span>
          <span className="font-mono tnum text-slate-200">
            {typeof entry.value === 'number'
              ? currency
                ? price(entry.value, currency)
                : num(entry.value)
              : String(entry.value ?? '')}
          </span>
        </div>
      ))}
    </div>
  );
}

export default function AssetPage() {
  const { symbol = '' } = useParams();
  const [params] = useSearchParams();
  const market = params.get('market') ?? undefined;

  const features = useQuery({
    queryKey: ['features', symbol, market, HISTORY_BARS],
    queryFn: () => api.features(symbol, market, HISTORY_BARS),
  });
  const audit = useQuery({
    queryKey: ['audit', symbol, market],
    queryFn: () => api.audit(symbol, market),
  });

  const currency = features.data?.currency ?? 'USD';

  const series = useMemo(() => {
    const rows: FeatureHistoryRow[] = features.data?.history ?? [];
    return rows.map((row) => ({
      ts: String(row.ts),
      close: row.close as number | null,
      ema20: row.ema_20 as number | null,
      ema50: row.ema_50 as number | null,
      ema200: row.ema_200 as number | null,
      volume: row.volume as number | null,
      rsi: row.rsi_14 as number | null,
      macd: row.macd as number | null,
      macdSignal: row.macd_signal as number | null,
      macdHist: row.macd_hist as number | null,
    }));
  }, [features.data]);

  if (features.isError) {
    return (
      <div className="space-y-4">
        <BackLink />
        <ErrorState error={features.error} onRetry={() => features.refetch()} />
      </div>
    );
  }

  const f = features.data?.features ?? {};
  const asNum = (key: string) => (typeof f[key] === 'number' ? (f[key] as number) : null);

  return (
    <div className="space-y-5">
      <BackLink />

      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-semibold text-slate-100">
            <span aria-hidden>{marketFlag(features.data?.market ?? '')}</span>
            <span className="font-mono">{symbol}</span>
            <Badge tone="neutral">{currency}</Badge>
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            {features.data ? (
              <>
                as of {date(features.data.as_of)} · {compact(features.data.bars_available)} bars stored
              </>
            ) : (
              'loading'
            )}
          </p>
        </div>
        {features.data && !features.data.complete && <Badge tone="caution">incomplete features</Badge>}
      </div>

      {/* Data-quality warnings come first, above the numbers they qualify. */}
      {audit.data &&
        audit.data.stale_quote_run >= audit.data.stale_quote_run_threshold && (
        <Caveat>
          <strong>
            The last {audit.data.stale_quote_run} bars are flat with zero volume.
          </strong>{' '}
          The vendor is carrying forward the last traded price, so this series is dated to today
          but contains no recent trading. Treat the most recent values as unusable.
        </Caveat>
      )}
      {features.data && !features.data.complete && (
        <Caveat tone="neutral">{features.data.note}</Caveat>
      )}

      {features.isLoading ? (
        <Loading rows={4} label={`Loading ${symbol}`} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
            <Stat label="Close" value={price(series.at(-1)?.close ?? null, currency)} />
            <Stat label="1D return" value={pct(asNum('return_1d'))} tone={toneOf(asNum('return_1d'))} />
            <Stat label="20D return" value={pct(asNum('return_20d'))} tone={toneOf(asNum('return_20d'))} />
            <Stat label="RSI 14" value={num(asNum('rsi_14'), 1)} hint="Wilder. 0-100, not a probability" />
            <Stat label="ATR %" value={pct(asNum('atr_pct_14'))} hint="Volatility, comparable across markets" />
            <Stat
              label="From 52w high"
              value={pct(asNum('dist_52w_high_pct'))}
              tone={toneOf(asNum('dist_52w_high_pct'))}
            />
          </div>

          <Card
            title="Price and exponential moving averages"
            subtitle={`Split- and dividend-adjusted closes. Last ${series.length} bars.`}
          >
            <ResponsiveContainer width="100%" height={320}>
              <ComposedChart data={series} margin={{ top: 5, right: 8, bottom: 0, left: 8 }}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis
                  dataKey="ts"
                  tick={{ fill: AXIS, fontSize: 10 }}
                  tickFormatter={(v: string) => date(v).slice(2, 7)}
                  minTickGap={40}
                  stroke={GRID}
                />
                <YAxis
                  tick={{ fill: AXIS, fontSize: 10 }}
                  domain={['auto', 'auto']}
                  tickFormatter={(v: number) => price(v, currency)}
                  width={64}
                  stroke={GRID}
                />
                <Tooltip content={<ChartTooltip currency={currency} />} />
                <Line dataKey="close" name="Close" stroke="#e2e8f0" strokeWidth={1.5} dot={false} />
                <Line dataKey="ema20" name="EMA 20" stroke="#4d9fff" strokeWidth={1} dot={false} />
                <Line dataKey="ema50" name="EMA 50" stroke="#ffb84d" strokeWidth={1} dot={false} />
                <Line dataKey="ema200" name="EMA 200" stroke="#ff5c7c" strokeWidth={1} dot={false} />
              </ComposedChart>
            </ResponsiveContainer>
            <Legend
              items={[
                ['Close', '#e2e8f0'],
                ['EMA 20', '#4d9fff'],
                ['EMA 50', '#ffb84d'],
                ['EMA 200', '#ff5c7c'],
              ]}
            />
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="RSI (14)" subtitle="Wilder's smoothing. Bands at 30 and 70.">
              <ResponsiveContainer width="100%" height={180}>
                <ComposedChart data={series} margin={{ top: 5, right: 8, bottom: 0, left: 8 }}>
                  <CartesianGrid stroke={GRID} vertical={false} />
                  <XAxis dataKey="ts" hide />
                  <YAxis domain={[0, 100]} ticks={[0, 30, 50, 70, 100]} tick={{ fill: AXIS, fontSize: 10 }} width={32} stroke={GRID} />
                  <Tooltip content={<ChartTooltip />} />
                  <ReferenceLine y={70} stroke="#c73e58" strokeDasharray="3 3" />
                  <ReferenceLine y={30} stroke="#1a9c63" strokeDasharray="3 3" />
                  <Line dataKey="rsi" name="RSI" stroke="#4d9fff" strokeWidth={1.3} dot={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </Card>

            <Card title="MACD (12, 26, 9)" subtitle="Line, signal and histogram.">
              <ResponsiveContainer width="100%" height={180}>
                <ComposedChart data={series} margin={{ top: 5, right: 8, bottom: 0, left: 8 }}>
                  <CartesianGrid stroke={GRID} vertical={false} />
                  <XAxis dataKey="ts" hide />
                  <YAxis tick={{ fill: AXIS, fontSize: 10 }} width={48} stroke={GRID} />
                  <Tooltip content={<ChartTooltip />} />
                  <ReferenceLine y={0} stroke={AXIS} />
                  <Bar dataKey="macdHist" name="Histogram" fill="#2f3a52" />
                  <Line dataKey="macd" name="MACD" stroke="#4d9fff" strokeWidth={1.3} dot={false} />
                  <Line dataKey="macdSignal" name="Signal" stroke="#ffb84d" strokeWidth={1} dot={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </Card>
          </div>

          <Card title="Volume" subtitle="Zero-volume sessions are real, and are not tradeable.">
            <ResponsiveContainer width="100%" height={140}>
              <BarChart data={series} margin={{ top: 5, right: 8, bottom: 0, left: 8 }}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis
                  dataKey="ts"
                  tick={{ fill: AXIS, fontSize: 10 }}
                  tickFormatter={(v: string) => date(v).slice(2, 7)}
                  minTickGap={40}
                  stroke={GRID}
                />
                <YAxis tick={{ fill: AXIS, fontSize: 10 }} tickFormatter={compact} width={48} stroke={GRID} />
                <Tooltip content={<ChartTooltip />} />
                <Bar dataKey="volume" name="Volume" fill="#2f3a52" />
              </BarChart>
            </ResponsiveContainer>
          </Card>

          <Card
            title="All computed features"
            subtitle="Historical measurements. Not forecasts, and not probabilities."
          >
            <div className="grid gap-x-6 gap-y-1 sm:grid-cols-2 lg:grid-cols-3">
              {Object.entries(f).map(([key, value]) => (
                <div
                  key={key}
                  className="flex items-baseline justify-between gap-3 border-b border-terminal-850 py-1"
                >
                  <span className="truncate font-mono text-2xs text-slate-500">{key}</span>
                  <span
                    className={
                      typeof value === 'number' && /return|dist_|roc_/.test(key)
                        ? `font-mono text-xs tnum ${signClass(value)}`
                        : 'font-mono text-xs tnum text-slate-200'
                    }
                  >
                    {value === null
                      ? '—'
                      : typeof value === 'boolean'
                        ? value
                          ? 'yes'
                          : 'no'
                        : num(value, 4)}
                  </span>
                </div>
              ))}
            </div>
          </Card>

          {audit.data && (
            <Card title="Data integrity" subtitle={audit.data.summary}>
              <div className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
                <Field label="Stored bars" value={compact(audit.data.stored_bars)} />
                <Field label="Missing weekdays" value={String(audit.data.missing_weekdays.length)} />
                <Field label="Longest gap" value={`${audit.data.longest_gap_sessions} sessions`} />
                <Field
                  label="Carried-forward tail"
                  value={`${audit.data.stale_quote_run} bars`}
                />
              </div>
              <p className="mt-3 text-2xs leading-relaxed text-slate-500">{audit.data.caveat}</p>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

function toneOf(value: number | null): 'gain' | 'loss' | 'neutral' {
  if (value === null) return 'neutral';
  return value > 0 ? 'gain' : value < 0 ? 'loss' : 'neutral';
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className="mt-0.5 font-mono tnum text-slate-200">{value}</div>
    </div>
  );
}

function Legend({ items }: { items: Array<[string, string]> }) {
  return (
    <div className="mt-2 flex flex-wrap gap-3">
      {items.map(([name, colour]) => (
        <span key={name} className="flex items-center gap-1.5 text-2xs text-slate-500">
          <span className="h-0.5 w-4 rounded" style={{ background: colour }} />
          {name}
        </span>
      ))}
    </div>
  );
}

function BackLink() {
  return (
    <Link
      to="/universe"
      className="inline-flex items-center gap-1.5 text-2xs text-slate-500 transition hover:text-slate-300"
    >
      <ArrowLeft className="h-3 w-3" />
      Back to universe
    </Link>
  );
}
