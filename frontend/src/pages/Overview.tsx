import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { ArrowDown, ArrowRight, ArrowUp } from 'lucide-react';
import { Link } from 'react-router-dom';

import { Badge, Card, Caveat, ErrorState, Loading, Stat } from '../components/ui';
import { api, type ScanRow } from '../lib/api';
import { DASH, compact, date, marketFlag, num, pct, price, signClass } from '../lib/format';

/**
 * Overview.
 *
 * Deliberately *not* the portfolio dashboard the brief sketches. There is no portfolio
 * yet — paper trading is Phase 6 — and rendering "Portfolio value $0.00, Daily P&L 0.00%"
 * would look like a flat account rather than an absent one. That misreading is more
 * expensive than an honestly incomplete page.
 *
 * What it shows instead is everything that genuinely exists: the data inventory, each
 * market's configuration and benchmark, and the strategy's current top readings.
 */
export default function Overview() {
  const coverage = useQuery({ queryKey: ['coverage', '1D'], queryFn: () => api.coverage('1D') });
  const markets = useQuery({ queryKey: ['markets'], queryFn: api.markets });
  const scan = useQuery({
    queryKey: ['scan', 'overview'],
    queryFn: () => api.scan({ sort: 'score', limit: 8 }),
    staleTime: 5 * 60 * 1000,
  });

  if (coverage.isError) {
    return <ErrorState error={coverage.error} onRetry={() => coverage.refetch()} />;
  }

  const rows = coverage.data?.rows ?? [];
  const latest = rows
    .map((r) => r.last_bar)
    .filter((d): d is string => Boolean(d))
    .sort()
    .at(-1);

  const signals = scan.data?.rows ?? [];
  const blocked = signals.filter((r) => !r.tradable).length;

  return (
    <div className="space-y-5">
      <header>
        <h2 className="text-lg font-semibold text-slate-100">Overview</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          What is stored, how each market is configured, and what the strategy currently reads.
        </p>
      </header>

      {coverage.isLoading ? (
        <Loading rows={2} label="Loading inventory" />
      ) : (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat
            label="Bars stored"
            value={compact(coverage.data?.total_bars)}
            hint="Daily OHLCV rows in local SQLite"
          />
          <Stat
            label="Instruments with data"
            value={`${coverage.data?.assets_with_data ?? 0}`}
            hint={`${coverage.data?.assets_without_data ?? 0} declared but empty`}
          />
          <Stat label="Newest bar" value={date(latest)} hint="Across all instruments" />
          <Stat label="Data cost" value="$0" tone="gain" hint="Every provider is free" />
        </div>
      )}

      {/* No portfolio yet, and the page says so rather than implying one exists. */}
      <Caveat tone="neutral">
        There is no portfolio here because nothing has traded. Paper execution arrives in
        Phase 6; until then this page shows data and current readings rather than positions
        and P&amp;L. See{' '}
        <Link to="/portfolio" className="underline">
          Portfolio
        </Link>{' '}
        for what that page will contain.
      </Caveat>

      <div className="grid gap-4 lg:grid-cols-2">
        {markets.isLoading && <Loading rows={4} />}
        {markets.data?.map((market) => {
          const marketRows = rows.filter((r) => r.market === market.code);
          const withData = marketRows.filter((r) => r.bars > 0);
          const bars = marketRows.reduce((sum, r) => sum + r.bars, 0);
          const marketSignals = signals.filter((s) => s.market === market.code);

          return (
            <Card
              key={market.code}
              title={
                <span className="flex items-center gap-2">
                  <span aria-hidden>{marketFlag(market.code)}</span>
                  {market.name}
                </span>
              }
              subtitle={`${market.exchange} · ${market.currency} · ${market.trading_hours}`}
            >
              <div className="grid grid-cols-4 gap-3 text-sm">
                <Field label="Instruments">
                  {withData.length}
                  <span className="text-slate-600">/{marketRows.length}</span>
                </Field>
                <Field label="Bars">{compact(bars)}</Field>
                <Field label="Benchmark">{market.benchmark?.symbol ?? DASH}</Field>
                <Field label="Usable now">
                  {marketSignals.length === 0
                    ? DASH
                    : `${marketSignals.filter((s) => s.tradable).length}/${marketSignals.length}`}
                </Field>
              </div>

              {market.benchmark && market.benchmark.kind === 'etf_proxy' && (
                <div className="mt-3">
                  <Caveat>
                    <strong>{market.benchmark.symbol} is a proxy, not the IPSA.</strong>{' '}
                    {market.benchmark.caveats[0]}
                  </Caveat>
                </div>
              )}
            </Card>
          );
        })}
      </div>

      <Card
        title="Top current readings"
        subtitle={
          scan.data
            ? `${scan.data.strategy.name} · scanned ${date(scan.data.scanned_at)}`
            : 'Evaluating'
        }
        actions={
          <Link
            to="/scanner"
            className="inline-flex items-center gap-1 text-2xs text-accent hover:underline"
          >
            Full scanner <ArrowRight className="h-3 w-3" />
          </Link>
        }
      >
        {scan.isLoading ? (
          <Loading rows={6} />
        ) : scan.isError ? (
          <ErrorState error={scan.error} onRetry={() => scan.refetch()} />
        ) : signals.length === 0 ? (
          <p className="py-6 text-center text-sm text-slate-500">
            Nothing evaluable yet. Run{' '}
            <span className="font-mono text-slate-400">python -m app download-data</span> first.
          </p>
        ) : (
          <>
            <div className="-m-4 overflow-x-auto">
              <table className="w-full border-collapse">
                <thead>
                  <tr>
                    <th className="th">Market</th>
                    <th className="th">Symbol</th>
                    <th className="th text-right">Price</th>
                    <th className="th">Signal</th>
                    <th className="th text-right">Score</th>
                    <th className="th text-right">20d</th>
                    <th className="th text-right">R:R</th>
                    <th className="th">Data</th>
                  </tr>
                </thead>
                <tbody>
                  {signals.map((row) => (
                    <SignalRow key={`${row.market}:${row.symbol}`} row={row} />
                  ))}
                </tbody>
              </table>
            </div>
            {blocked > 0 && (
              <p className="mt-3 text-2xs leading-relaxed text-slate-500">
                {blocked} of these {signals.length} cannot produce a usable verdict: their data is
                stale or their volume feed has stopped reporting. Shown rather than hidden,
                because &ldquo;no setups&rdquo; and &ldquo;broken input&rdquo; are different
                conclusions.
              </p>
            )}
            <p className="mt-2 text-2xs leading-relaxed text-slate-600">
              {scan.data?.disclaimer}
            </p>
          </>
        )}
      </Card>

      <Card title="Not implemented yet" subtitle="Listed so the absence is explicit">
        <ul className="grid gap-2 text-xs text-slate-400 sm:grid-cols-2">
          {[
            ['Phase 4', 'Parameter optimisation, walk-forward, robustness testing'],
            ['Phase 5', 'Historical analogues and statistical scenarios'],
            ['Phase 6', 'Paper trading (Alpaca for US, internal broker for Chile)'],
            ['Phase 7', 'Telegram alerts'],
          ].map(([phase, what]) => (
            <li key={phase} className="flex gap-2 rounded border border-terminal-800 px-3 py-2">
              <span className="shrink-0 font-mono text-2xs text-slate-600">{phase}</span>
              <span>{what}</span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className="mt-0.5 font-mono tnum text-slate-100">{children}</div>
    </div>
  );
}

function SignalRow({ row }: { row: ScanRow }) {
  const tone = row.action === 'BUY' ? 'gain' : row.action === 'SELL' ? 'loss' : 'neutral';
  return (
    <tr className={clsx('hover:bg-terminal-850', !row.tradable && 'opacity-60')}>
      <td className="td whitespace-nowrap text-slate-400">
        <span aria-hidden>{marketFlag(row.market)}</span>
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
      <td className="td">
        <Badge tone={tone}>
          {row.action === 'BUY' && <ArrowUp className="mr-0.5 h-2.5 w-2.5" />}
          {row.action === 'SELL' && <ArrowDown className="mr-0.5 h-2.5 w-2.5" />}
          {row.action}
        </Badge>
      </td>
      <td className="td text-right font-mono tnum text-slate-200" title={row.score_description}>
        {num(row.score, 2)}
      </td>
      <td className={clsx('td text-right font-mono tnum', signClass(row.return_20d))}>
        {pct(row.return_20d, 1)}
      </td>
      <td className="td text-right font-mono tnum text-slate-300">
        {row.risk_reward === null ? DASH : `${num(row.risk_reward, 1)}:1`}
      </td>
      <td className="td">
        {row.tradable ? (
          <span className="text-2xs text-slate-600">ok</span>
        ) : (
          <Badge tone="loss" title={row.blocked_reason}>
            {row.stale ? 'stale' : 'no volume'}
          </Badge>
        )}
      </td>
    </tr>
  );
}
