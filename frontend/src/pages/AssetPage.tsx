import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { ArrowDown, ArrowLeft, ArrowUp, Minus } from 'lucide-react';
import { useMemo } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';

import { SignedBarChart, TimeSeriesChart } from '../components/charts';
import { Badge, Card, Caveat, ErrorState, Loading, Stat } from '../components/ui';
import { api, type FeatureHistoryRow, type ScanRow } from '../lib/api';
import { DASH, compact, date, marketFlag, num, pct, price, signClass } from '../lib/format';

const HISTORY_BARS = 320;

/**
 * Asset page.
 *
 * Built on the shared chart primitives so the palette, tooltips and table views behave
 * the same as everywhere else. Two page-specific concerns:
 *
 * **Data-quality warnings sit above the charts, not beneath them.** A carried-forward
 * tail makes a price series look current, and a reader who sees the chart before the
 * caveat has already drawn a conclusion.
 *
 * **The verdict is shown with its components**, so "HOLD" is legible as "five of seven
 * conditions hold, and here are the two that do not" rather than as an opaque outcome.
 */
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
  const scan = useQuery({
    queryKey: ['scan', 'single', symbol, market],
    queryFn: () => api.scan({ market, limit: 500 }),
    staleTime: 5 * 60 * 1000,
  });

  const currency = features.data?.currency ?? 'USD';
  const verdict: ScanRow | undefined = scan.data?.rows.find((r) => r.symbol === symbol);

  const series = useMemo(() => {
    const rows: FeatureHistoryRow[] = features.data?.history ?? [];
    return rows.map((row) => ({
      ts: String(row.ts).slice(0, 10),
      close: row.close as number | null,
      ema20: row.ema_20 as number | null,
      ema50: row.ema_50 as number | null,
      ema200: row.ema_200 as number | null,
      volume: row.volume as number | null,
      rsi: row.rsi_14 as number | null,
      macd: row.macd as number | null,
      macdSignal: row.macd_signal as number | null,
      macdHist: row.macd_hist as number | null,
      return1d: row.return_1d as number | null,
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
  const dropped = features.data?.carried_forward_dropped ?? 0;

  return (
    <div className="space-y-4">
      <BackLink />

      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-lg font-semibold text-slate-100">
            <span aria-hidden>{marketFlag(features.data?.market ?? '')}</span>
            <span className="font-mono">{symbol}</span>
            <Badge tone="neutral">{currency}</Badge>
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            {features.data ? (
              <>
                as of {date(features.data.as_of)} · {compact(features.data.bars_available)} real bars
              </>
            ) : (
              'loading'
            )}
          </p>
        </div>
        {verdict && <VerdictBadge row={verdict} />}
      </header>

      {/* Warnings first: a chart seen before its caveat has already been believed. */}
      {dropped > 0 && (
        <Caveat>
          <strong>{dropped} trailing bars were removed before computing anything.</strong> They
          were flat with zero volume — the vendor carrying the last traded price forward — so the
          stored series ran past its last real print. Everything below is as of{' '}
          {date(features.data?.as_of)}, the last session that actually traded.
        </Caveat>
      )}
      {audit.data && audit.data.volume_feed_degraded && (
        <Caveat>
          <strong>
            {num(audit.data.recent_zero_volume_pct, 0)}% of recent bars report zero volume.
          </strong>{' '}
          The prices moved, so these are not carried-forward bars and they are kept — but every
          volume-derived reading below is unreliable, and a strategy with a volume condition
          cannot evaluate it.
        </Caveat>
      )}
      {audit.data?.is_stale && (
        <Caveat>
          <strong>This series is stale.</strong> {audit.data.summary}
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
            <Stat
              label="1D return"
              value={pct(asNum('return_1d'))}
              tone={toneOf(asNum('return_1d'))}
            />
            <Stat
              label="20D return"
              value={pct(asNum('return_20d'))}
              tone={toneOf(asNum('return_20d'))}
            />
            <Stat label="RSI 14" value={num(asNum('rsi_14'), 1)} hint="Wilder. 0-100, not a probability" />
            <Stat label="ATR %" value={pct(asNum('atr_pct_14'))} hint="Volatility, comparable across markets" />
            <Stat
              label="From 52w high"
              value={pct(asNum('dist_52w_high_pct'))}
              tone={toneOf(asNum('dist_52w_high_pct'))}
            />
          </div>

          {verdict && <VerdictCard row={verdict} />}

          <TimeSeriesChart
            title="Price and exponential moving averages"
            note="Split- and dividend-adjusted closes"
            data={series}
            series={[
              { key: 'close', label: 'Close', ink: true },
              { key: 'ema20', label: 'EMA 20' },
              { key: 'ema50', label: 'EMA 50' },
              { key: 'ema200', label: 'EMA 200' },
            ]}
            height={300}
            digits={currency === 'CLP' ? 0 : 2}
          />

          <div className="grid gap-4 lg:grid-cols-2">
            <TimeSeriesChart
              title="RSI (14)"
              note="Wilder's smoothing. Conventional bands at 30 and 70."
              data={series}
              series={[{ key: 'rsi', label: 'RSI' }]}
              height={180}
              digits={1}
            />
            <TimeSeriesChart
              title="MACD (12, 26, 9)"
              note="Line and signal. The histogram is their difference."
              data={series}
              series={[
                { key: 'macd', label: 'MACD' },
                { key: 'macdSignal', label: 'Signal', colour: '#c98500' },
              ]}
              height={180}
              digits={3}
              zeroLine
            />
          </div>

          <SignedBarChart
            title="Daily returns"
            note="Percentage change of the adjusted close"
            data={series.map((s) => ({ ts: s.ts, return1d: s.return1d }))}
            xKey="ts"
            valueKey="return1d"
            height={160}
            digits={2}
            labelName="Daily return"
          />

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
                    className={clsx(
                      'font-mono text-xs tnum',
                      typeof value === 'number' && /return|dist_|roc_/.test(key)
                        ? signClass(value)
                        : 'text-slate-200',
                    )}
                  >
                    {value === null
                      ? DASH
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
              <div className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-5">
                <Field label="Stored bars" value={compact(audit.data.stored_bars)} />
                <Field label="Missing weekdays" value={String(audit.data.missing_weekdays.length)} />
                <Field label="Longest gap" value={`${audit.data.longest_gap_sessions} sessions`} />
                <Field label="Carried forward" value={`${audit.data.stale_quote_run} bars`} />
                <Field
                  label="Zero volume (recent)"
                  value={`${num(audit.data.recent_zero_volume_pct, 0)}%`}
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

/* ------------------------------------------------------------------ */

function VerdictBadge({ row }: { row: ScanRow }) {
  const tone = row.action === 'BUY' ? 'gain' : row.action === 'SELL' ? 'loss' : 'neutral';
  const Icon = row.action === 'BUY' ? ArrowUp : row.action === 'SELL' ? ArrowDown : Minus;
  return (
    <div className="flex items-center gap-2">
      <Badge tone={tone} title={row.score_description}>
        <Icon className="mr-1 h-3 w-3" />
        {row.action}
      </Badge>
      {!row.tradable && (
        <Badge tone="loss" title={row.blocked_reason}>
          not tradable
        </Badge>
      )}
    </div>
  );
}

/**
 * The verdict with its components, so a HOLD is legible.
 *
 * Showing which conditions failed is the difference between a decision a reader can
 * argue with and an opaque label they either trust or ignore.
 */
function VerdictCard({ row }: { row: ScanRow }) {
  return (
    <Card
      title={`Strategy verdict: ${row.action}`}
      subtitle={row.score_description}
      actions={
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm tnum text-slate-200">{num(row.score, 2)}</span>
          <span className="h-1.5 w-20 overflow-hidden rounded bg-terminal-800" aria-hidden>
            <span
              className="block h-full bg-accent"
              style={{ width: `${Math.max(0, Math.min(1, row.score)) * 100}%` }}
            />
          </span>
        </div>
      }
    >
      {!row.tradable && (
        <div className="mb-3">
          <Caveat>{row.blocked_reason}</Caveat>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <div className="label mb-1.5">
            {row.action === 'BUY' ? 'Conditions that hold' : 'Why not an entry'}
          </div>
          <ul className="space-y-1">
            {row.reasons.length === 0 ? (
              <li className="text-2xs text-slate-600">No reasons recorded.</li>
            ) : (
              row.reasons.map((reason) => (
                <li key={reason} className="flex gap-2 text-2xs leading-relaxed text-slate-400">
                  <span
                    className={clsx(
                      'mt-1.5 h-1 w-1 shrink-0 rounded-full',
                      row.action === 'BUY' ? 'bg-gain/70' : 'bg-caution/60',
                    )}
                    aria-hidden
                  />
                  {reason}
                </li>
              ))
            )}
          </ul>
        </div>

        <div>
          <div className="label mb-1.5">If this were entered now</div>
          <dl className="space-y-1">
            {[
              ['Stop', row.stop_price === null ? DASH : price(row.stop_price, row.currency)],
              [
                'Target',
                row.take_profit_price === null
                  ? DASH
                  : price(row.take_profit_price, row.currency),
              ],
              [
                'Risk / reward',
                row.risk_reward === null ? DASH : `${num(row.risk_reward, 2)} : 1`,
              ],
              ['Trend score', num(row.trend_score, 2)],
              ['Momentum score', num(row.momentum_score, 2)],
            ].map(([label, value]) => (
              <div
                key={label}
                className="flex items-baseline justify-between gap-2 border-b border-terminal-850 py-1"
              >
                <dt className="text-2xs text-slate-500">{label}</dt>
                <dd className="font-mono text-xs tnum text-slate-200">{value}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-2 text-2xs leading-relaxed text-slate-600">
            Risk / reward describes the <em>plan</em>. It says nothing about how likely either
            level is to be reached.
          </p>
        </div>
      </div>
    </Card>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className="mt-0.5 font-mono tnum text-slate-200">{value}</div>
    </div>
  );
}

function BackLink() {
  return (
    <Link
      to="/scanner"
      className="inline-flex items-center gap-1.5 text-2xs text-slate-500 transition hover:text-slate-300"
    >
      <ArrowLeft className="h-3 w-3" />
      Back to scanner
    </Link>
  );
}

function toneOf(value: number | null): 'gain' | 'loss' | 'neutral' {
  if (value === null) return 'neutral';
  return value > 0 ? 'gain' : value < 0 ? 'loss' : 'neutral';
}
