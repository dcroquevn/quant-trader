import { useQuery } from '@tanstack/react-query';

import { Card, Caveat, ErrorState, Loading, Stat } from '../components/ui';
import { api } from '../lib/api';
import { compact, date, marketFlag } from '../lib/format';

/**
 * Phase-1 overview.
 *
 * It shows data inventory and market configuration, not portfolio value or P&L.
 * Those numbers do not exist yet -- there is no strategy and no portfolio -- and
 * rendering them as zeros would look like a flat account rather than an absent one.
 */
export default function Overview() {
  const coverage = useQuery({ queryKey: ['coverage', '1D'], queryFn: () => api.coverage('1D') });
  const markets = useQuery({ queryKey: ['markets'], queryFn: api.markets });
  const providers = useQuery({ queryKey: ['providers'], queryFn: api.providers });

  if (coverage.isError) return <ErrorState error={coverage.error} onRetry={() => coverage.refetch()} />;

  const rows = coverage.data?.rows ?? [];
  const byMarket = (code: string) => rows.filter((r) => r.market === code);
  const latest = rows
    .map((r) => r.last_bar)
    .filter((d): d is string => Boolean(d))
    .sort()
    .at(-1);

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-slate-100">Data foundation</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          What is stored locally right now, and how each market is configured.
        </p>
      </div>

      {coverage.isLoading ? (
        <Loading rows={2} label="Loading coverage" />
      ) : (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat
            label="Bars stored"
            value={compact(coverage.data?.total_bars)}
            hint="Daily OHLCV rows in the local SQLite database"
          />
          <Stat
            label="Instruments with data"
            value={`${coverage.data?.assets_with_data ?? 0}`}
            hint={`${coverage.data?.assets_without_data ?? 0} declared but empty`}
          />
          <Stat label="Newest bar" value={date(latest)} hint="Across all instruments" />
          <Stat
            label="Data cost"
            value="$0"
            tone="gain"
            hint={providers.data?.all_free ? 'Every provider is free, no API key' : ''}
          />
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {markets.isLoading && <Loading rows={4} />}
        {markets.data?.map((market) => {
          const marketRows = byMarket(market.code);
          const withData = marketRows.filter((r) => r.bars > 0);
          const bars = marketRows.reduce((sum, r) => sum + r.bars, 0);

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
              <div className="grid grid-cols-3 gap-3 text-sm">
                <div>
                  <div className="label">Instruments</div>
                  <div className="mt-0.5 font-mono tnum text-slate-100">
                    {withData.length}
                    <span className="text-slate-600">/{marketRows.length}</span>
                  </div>
                </div>
                <div>
                  <div className="label">Bars</div>
                  <div className="mt-0.5 font-mono tnum text-slate-100">{compact(bars)}</div>
                </div>
                <div>
                  <div className="label">Benchmark</div>
                  <div className="mt-0.5 font-mono text-slate-100">
                    {market.benchmark?.symbol ?? '—'}
                  </div>
                </div>
              </div>

              {/* A benchmark that is not the benchmark must say so next to itself,
                  not three pages away in a limitations list. */}
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
        title="Not implemented yet"
        subtitle="Listed so the absence is explicit rather than looking like empty data"
      >
        <ul className="grid gap-2 text-xs text-slate-400 sm:grid-cols-2">
          {[
            ['Phase 2', 'Strategy engine, scanner, backtester, metrics'],
            ['Phase 4', 'Parameter optimisation, train/validation/test, walk-forward'],
            ['Phase 5', 'Historical analogues and statistical scenarios'],
            ['Phase 6', 'Paper trading (Alpaca for US, internal broker for Chile)'],
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
