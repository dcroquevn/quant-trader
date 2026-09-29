import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { AlertTriangle, ArrowDown, ArrowUp, Ban, RefreshCw, Search } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Card, Caveat, ErrorState, Loading } from '../components/ui';
import { api, type ScanRow } from '../lib/api';
import { DASH, marketFlag, num, pct, price, signClass } from '../lib/format';

/**
 * Scanner page.
 *
 * Two things this page is careful about:
 *
 * A **score is not a probability.** It counts how many of the strategy's conditions
 * currently hold. The backend supplies the safe phrasing in ``score_description`` and
 * the tooltip uses it verbatim rather than inventing a gloss.
 *
 * A **BUY on broken data is not a signal.** Rows whose last real print is weeks old, or
 * whose volume feed has stopped reporting, arrive with ``tradable: false`` and a reason.
 * They are shown — hiding them would look like an absence of setups — but visibly
 * separated, and they never appear in the "actionable" count.
 */

type MarketFilter = 'ALL' | 'USA' | 'CHILE';
type ActionFilter = 'ALL' | 'BUY' | 'SELL' | 'HOLD';

const SORTS = [
  { key: 'score', label: 'Score' },
  { key: 'return_20d', label: '20d return' },
  { key: 'relative_volume', label: 'Rel. volume' },
  { key: 'rsi', label: 'RSI' },
  { key: 'atr_pct', label: 'Volatility' },
  { key: 'risk_reward', label: 'Risk / reward' },
  { key: 'dist_52w_high_pct', label: 'From 52w high' },
  { key: 'dollar_volume', label: 'Liquidity' },
] as const;

export default function Scanner() {
  const [market, setMarket] = useState<MarketFilter>('ALL');
  const [action, setAction] = useState<ActionFilter>('ALL');
  const [sort, setSort] = useState<string>('score');
  const [minScore, setMinScore] = useState(0);
  const [tradableOnly, setTradableOnly] = useState(false);
  const [query, setQuery] = useState('');

  const scan = useQuery({
    queryKey: ['scan', market, action, sort, minScore, tradableOnly],
    queryFn: () =>
      api.scan({
        market: market === 'ALL' ? undefined : market,
        action,
        sort,
        min_score: minScore,
        tradable_only: tradableOnly,
        limit: 500,
      }),
    // A scan recomputes indicators over every instrument; it is not free.
    staleTime: 2 * 60 * 1000,
  });

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const all = scan.data?.rows ?? [];
    return needle ? all.filter((r) => r.symbol.toLowerCase().includes(needle)) : all;
  }, [scan.data, query]);

  const actionable = rows.filter((r) => r.action !== 'HOLD' && r.tradable);
  const blocked = rows.filter((r) => !r.tradable);

  if (scan.isError) return <ErrorState error={scan.error} onRetry={() => scan.refetch()} />;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">Scanner</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            {scan.data
              ? `${scan.data.strategy.name} v${scan.data.strategy.version} · ${rows.length} of ${scan.data.n_total} instruments`
              : 'Evaluating the universe'}
          </p>
        </div>
        <div className="flex items-center gap-3 text-2xs text-slate-500">
          {actionable.length > 0 && (
            <Badge tone="accent">{actionable.length} actionable</Badge>
          )}
          {blocked.length > 0 && <Badge tone="loss">{blocked.length} blocked by data</Badge>}
          <button
            type="button"
            onClick={() => scan.refetch()}
            disabled={scan.isFetching}
            className="inline-flex items-center gap-1.5 rounded border border-terminal-700 px-2 py-1 transition hover:bg-terminal-800 hover:text-slate-200 disabled:opacity-50"
          >
            <RefreshCw className={clsx('h-3 w-3', scan.isFetching && 'animate-spin')} />
            Rescan
          </button>
        </div>
      </header>

      {/* Filters in one row above the table, as a single control surface. */}
      <div className="card flex flex-wrap items-center gap-x-5 gap-y-3 px-4 py-3">
        <Segmented
          label="Market"
          value={market}
          options={[
            { value: 'ALL', label: 'All' },
            { value: 'USA', label: `${marketFlag('USA')} USA` },
            { value: 'CHILE', label: `${marketFlag('CHILE')} Chile` },
          ]}
          onChange={(v) => setMarket(v as MarketFilter)}
        />
        <Segmented
          label="Signal"
          value={action}
          options={[
            { value: 'ALL', label: 'All' },
            { value: 'BUY', label: 'Buy' },
            { value: 'SELL', label: 'Sell' },
            { value: 'HOLD', label: 'Hold' },
          ]}
          onChange={(v) => setAction(v as ActionFilter)}
        />

        <label className="flex flex-col gap-1">
          <span className="label">Sort by</span>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value)}
            className="rounded border border-terminal-700 bg-terminal-850 px-2 py-1.5 text-xs text-slate-200 focus:border-accent/50 focus:outline-none"
          >
            {SORTS.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="label">Min score {num(minScore, 2)}</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={minScore}
            onChange={(e) => setMinScore(Number(e.target.value))}
            className="h-7 w-32 accent-accent"
          />
        </label>

        <label className="flex cursor-pointer items-center gap-2 self-end pb-1.5 text-xs text-slate-400">
          <input
            type="checkbox"
            checked={tradableOnly}
            onChange={(e) => setTradableOnly(e.target.checked)}
            className="accent-accent"
          />
          Hide rows blocked by data quality
        </label>

        <label className="relative ml-auto self-end pb-0.5">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter symbol"
            className="w-40 rounded border border-terminal-700 bg-terminal-850 py-1.5 pl-7 pr-2 text-xs text-slate-200 placeholder:text-slate-600 focus:border-accent/50 focus:outline-none"
          />
        </label>
      </div>

      {scan.data && blocked.length > 0 && !tradableOnly && (
        <Caveat>
          <strong>
            {blocked.length} instrument{blocked.length === 1 ? '' : 's'} cannot produce a usable
            verdict right now.
          </strong>{' '}
          Their data is stale or their volume feed has stopped reporting, so the strategy's
          conditions are unevaluable. They are listed with a reason rather than hidden, because
          &ldquo;no setups&rdquo; and &ldquo;broken input&rdquo; are different conclusions.
        </Caveat>
      )}

      <Card>
        {scan.isLoading ? (
          <Loading rows={10} label="Computing indicators across the universe" />
        ) : rows.length === 0 ? (
          <div className="py-12 text-center text-sm text-slate-500">
            No instrument matches these filters.
          </div>
        ) : (
          <div className="-m-4 overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="th">Market</th>
                  <th className="th">Symbol</th>
                  <th className="th text-right">Price</th>
                  <th className="th">Signal</th>
                  <th className="th text-right">Score</th>
                  <th className="th text-right">RSI</th>
                  <th className="th text-right">Rel. vol</th>
                  <th className="th text-right">ATR %</th>
                  <th className="th text-right">20d</th>
                  <th className="th text-right">52w high</th>
                  <th className="th text-right">R:R</th>
                  <th className="th">Data</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <ScannerRow key={`${row.market}:${row.symbol}`} row={row} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {scan.data && scan.data.n_errors > 0 && (
        <Card
          title={`${scan.data.n_errors} instrument(s) could not be evaluated`}
          subtitle="Recorded rather than silently dropped"
        >
          <ul className="space-y-1">
            {scan.data.errors.map((e) => (
              <li key={`${e.market}:${e.symbol}`} className="text-2xs text-slate-500">
                <span className="font-mono text-slate-400">{e.symbol}</span> &mdash; {e.error}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {scan.data && (
        <p className="text-2xs leading-relaxed text-slate-600">{scan.data.disclaimer}</p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function ScannerRow({ row }: { row: ScanRow }) {
  const tone =
    row.action === 'BUY' ? 'gain' : row.action === 'SELL' ? 'loss' : 'neutral';

  return (
    <tr className={clsx('hover:bg-terminal-850', !row.tradable && 'opacity-60')}>
      <td className="td whitespace-nowrap text-slate-400">
        <span aria-hidden>{marketFlag(row.market)}</span> {row.market}
      </td>
      <td className="td whitespace-nowrap">
        <Link
          to={`/asset/${encodeURIComponent(row.symbol)}?market=${row.market}`}
          className="font-mono font-medium text-accent hover:underline"
        >
          {row.symbol}
        </Link>
      </td>
      <td className="td text-right font-mono tnum text-slate-200">
        {price(row.price, row.currency)}
      </td>
      <td className="td whitespace-nowrap">
        {/* An icon plus the word, never colour alone. */}
        <Badge tone={tone}>
          {row.action === 'BUY' && <ArrowUp className="mr-0.5 h-2.5 w-2.5" />}
          {row.action === 'SELL' && <ArrowDown className="mr-0.5 h-2.5 w-2.5" />}
          {row.action}
        </Badge>
      </td>
      <td className="td text-right" title={row.score_description}>
        <div className="flex items-center justify-end gap-1.5">
          <span className="font-mono tnum text-slate-200">{num(row.score, 2)}</span>
          <span className="h-1 w-8 overflow-hidden rounded bg-terminal-800" aria-hidden>
            <span
              className="block h-full bg-accent"
              style={{ width: `${Math.max(0, Math.min(1, row.score)) * 100}%` }}
            />
          </span>
        </div>
      </td>
      <td className="td text-right font-mono tnum text-slate-300">{num(row.rsi, 1)}</td>
      <td className="td text-right font-mono tnum text-slate-300">
        {num(row.relative_volume, 2)}
      </td>
      <td className="td text-right font-mono tnum text-slate-300">{num(row.atr_pct, 2)}</td>
      <td className={clsx('td text-right font-mono tnum', signClass(row.return_20d))}>
        {pct(row.return_20d, 1)}
      </td>
      <td className={clsx('td text-right font-mono tnum', signClass(row.dist_52w_high_pct))}>
        {pct(row.dist_52w_high_pct, 1)}
      </td>
      <td className="td text-right font-mono tnum text-slate-300">
        {row.risk_reward === null ? DASH : `${num(row.risk_reward, 1)}:1`}
      </td>
      <td className="td">
        <DataFlags row={row} />
      </td>
    </tr>
  );
}

function DataFlags({ row }: { row: ScanRow }) {
  const flags: Array<{ label: string; tone: 'loss' | 'caution'; title: string }> = [];

  if (row.stale) {
    flags.push({
      label: 'stale',
      tone: 'loss',
      title: row.blocked_reason || 'Last real print is older than the freshness tolerance',
    });
  }
  if (row.volume_feed_degraded) {
    flags.push({
      label: `no vol ${num(row.recent_zero_volume_pct, 0)}%`,
      tone: 'loss',
      title:
        row.blocked_reason ||
        'Recent bars report zero volume, so volume conditions are unevaluable',
    });
  }
  if (row.carried_forward_dropped > 0) {
    flags.push({
      label: `-${row.carried_forward_dropped} fabricated`,
      tone: 'caution',
      title: `${row.carried_forward_dropped} trailing bars were flat with zero volume and were removed before computing`,
    });
  }

  if (flags.length === 0) {
    return (
      <span className="flex items-center gap-1 text-2xs text-slate-600">
        <span className="h-1.5 w-1.5 rounded-full bg-gain/70" aria-hidden />
        ok
      </span>
    );
  }

  return (
    <div className="flex flex-wrap gap-1">
      {flags.map((f) => (
        <Badge key={f.label} tone={f.tone} title={f.title}>
          {f.tone === 'loss' ? (
            <Ban className="mr-0.5 h-2.5 w-2.5" />
          ) : (
            <AlertTriangle className="mr-0.5 h-2.5 w-2.5" />
          )}
          {f.label}
        </Badge>
      ))}
    </div>
  );
}

function Segmented({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="label">{label}</span>
      <div className="inline-flex rounded border border-terminal-700 p-0.5">
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            onClick={() => onChange(o.value)}
            aria-pressed={value === o.value}
            className={clsx(
              'rounded px-2.5 py-1 text-2xs font-medium transition',
              value === o.value
                ? 'bg-terminal-700 text-slate-100'
                : 'text-slate-400 hover:text-slate-200',
            )}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}
