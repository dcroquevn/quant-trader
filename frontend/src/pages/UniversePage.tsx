import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { Search } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Card, ErrorState, Loading } from '../components/ui';
import { api } from '../lib/api';
import { compact, date } from '../lib/format';

/**
 * The universe, grouped by exposure rather than by market.
 *
 * It used to filter on market. That stopped being useful when every instrument moved to a US
 * listing: "USA" returns all 42 and "CHILE" returns none, which is accurate and tells the reader
 * nothing. Region is what a portfolio allocates across, and it is what each benchmark follows.
 *
 * Liquidity is given its own column because turnover here spans four orders of magnitude, from
 * SPY at 37 billion a day to CCU at under 2 million, and the backtester models no market impact
 * at any size. The caveat text is the backend's, generated from the measured figure.
 */

const ALL = 'ALL';

export default function UniversePage() {
  const [region, setRegion] = useState<string>(ALL);
  const [query, setQuery] = useState('');

  const universe = useQuery({ queryKey: ['universe'], queryFn: () => api.universe() });
  const regions = useQuery({ queryKey: ['regions'], queryFn: () => api.regions() });
  const coverage = useQuery({ queryKey: ['coverage', '1D'], queryFn: () => api.coverage('1D') });

  const barsBySymbol = useMemo(() => {
    const map = new Map<string, { bars: number; first: string | null; last: string | null }>();
    for (const row of coverage.data?.rows ?? []) {
      map.set(`${row.market}:${row.symbol}`, {
        bars: row.bars,
        first: row.first_bar,
        last: row.last_bar,
      });
    }
    return map;
  }, [coverage.data]);

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (universe.data?.assets ?? []).filter((asset) => {
      if (region !== ALL && asset.region !== region) return false;
      if (!needle) return true;
      return (
        asset.symbol.toLowerCase().includes(needle) ||
        asset.name.toLowerCase().includes(needle) ||
        asset.sector.toLowerCase().includes(needle)
      );
    });
  }, [universe.data, region, query]);

  if (universe.isError)
    return <ErrorState error={universe.error} onRetry={() => universe.refetch()} />;

  const regionNames = regions.data?.regions.map((r) => r.region) ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">Universe</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            {universe.data?.count ?? 0} instruments, all listed in the US and priced in USD.
            Provider symbols verified {universe.data?.verification_date ?? '—'} against live
            responses.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded border border-terminal-700 p-0.5">
            {[ALL, ...regionNames].map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => setRegion(name)}
                className={clsx(
                  'rounded px-2.5 py-1 text-2xs font-medium transition',
                  region === name
                    ? 'bg-terminal-700 text-slate-100'
                    : 'text-slate-400 hover:text-slate-200',
                )}
              >
                {name === ALL ? 'All' : name}
              </button>
            ))}
          </div>

          <label className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter symbol, name, sector"
              className="w-56 rounded border border-terminal-700 bg-terminal-850 py-1.5 pl-7 pr-2 text-xs text-slate-200 placeholder:text-slate-600 focus:border-accent/50 focus:outline-none"
            />
          </label>
        </div>
      </div>

      {regions.data && (
        <>
          {/*
            Market and region are different things and conflating them would put every
            instrument in one bucket with the wrong benchmark. Stated on the page because the
            consequence — a USD return that contains a currency move — lands on the reader, not
            on the code.
          */}
          <Card>
            <p className="text-2xs leading-relaxed text-caution">{regions.data.currency_caveat}</p>
          </Card>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {regions.data.regions.map((row) => (
              <Card key={row.region}>
                <div className="flex items-baseline justify-between gap-2">
                  <h3 className="text-sm font-semibold text-slate-100">{row.region}</h3>
                  <span className="font-mono text-lg tnum text-slate-200">{row.count}</span>
                </div>
                <dl className="mt-2 space-y-1 text-2xs text-slate-400">
                  <div className="flex justify-between gap-2">
                    <dt>Benchmark</dt>
                    <dd className="font-mono text-slate-300">
                      {row.benchmark.symbol || 'none'}
                      {row.benchmark.also_tradable && (
                        <span className="ml-1 text-caution" title="A strategy can hold its own benchmark, so outperformance measures timing rather than selection.">
                          (also tradable)
                        </span>
                      )}
                    </dd>
                  </div>
                  <div className="flex justify-between gap-2">
                    <dt>ETFs</dt>
                    <dd className="font-mono tnum text-slate-300">{row.n_etfs}</dd>
                  </div>
                  <div className="flex justify-between gap-2">
                    <dt>Thinly traded</dt>
                    <dd className="font-mono tnum">
                      {row.n_thinly_traded > 0 ? (
                        <span className="text-caution">{row.n_thinly_traded}</span>
                      ) : (
                        <span className="text-slate-300">0</span>
                      )}
                    </dd>
                  </div>
                </dl>
                {row.n_thinly_traded > 0 && (
                  <p className="mt-2 text-2xs leading-snug text-slate-500">
                    {row.thinly_traded.join(', ')}
                  </p>
                )}
              </Card>
            ))}
          </div>

          <Card>
            <p className="text-2xs leading-relaxed text-caution">{regions.data.liquidity_caveat}</p>
            <p className="mt-1 text-2xs text-slate-600">
              Turnover measured over {regions.data.turnover_window}.
            </p>
          </Card>
        </>
      )}

      <Card>
        {universe.isLoading ? (
          <Loading rows={8} label="Loading universe" />
        ) : (
          <div className="-m-4 overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="th">Region</th>
                  <th className="th">Symbol</th>
                  <th className="th">Name</th>
                  <th className="th">Sector</th>
                  <th className="th text-right">Traded value/day</th>
                  <th className="th text-right">Bars</th>
                  <th className="th">Range</th>
                  <th className="th">Caveat</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((asset) => {
                  const stored = barsBySymbol.get(`${asset.market}:${asset.symbol}`);
                  const bars = stored?.bars ?? 0;
                  // Two caveats can apply. Liquidity is generated from a measurement and
                  // matters for sizing; the hand-written note covers listing history and
                  // overlap. Both are shown rather than one winning.
                  const caveats = [asset.liquidity_caveat, asset.notes].filter(Boolean);
                  return (
                    <tr key={`${asset.market}:${asset.symbol}`} className="hover:bg-terminal-850">
                      <td className="td whitespace-nowrap text-slate-400">{asset.region}</td>
                      <td className="td whitespace-nowrap">
                        {bars > 0 ? (
                          <Link
                            to={`/asset/${encodeURIComponent(asset.symbol)}?market=${asset.market}`}
                            className="font-mono font-medium text-accent hover:underline"
                          >
                            {asset.symbol}
                          </Link>
                        ) : (
                          <span className="font-mono text-slate-500">{asset.symbol}</span>
                        )}
                        {asset.asset_class === 'etf' && (
                          <span className="ml-1.5 text-3xs uppercase tracking-wide text-slate-600">
                            etf
                          </span>
                        )}
                      </td>
                      <td className="td max-w-[18rem] truncate text-slate-300" title={asset.name}>
                        {asset.name}
                      </td>
                      <td className="td whitespace-nowrap text-slate-400">{asset.sector}</td>
                      <td className="td text-right font-mono tnum">
                        {asset.median_turnover_usd == null ? (
                          <Badge tone="loss">unmeasured</Badge>
                        ) : (
                          <span className={asset.thinly_traded ? 'text-caution' : 'text-slate-300'}>
                            {compact(asset.median_turnover_usd)}
                          </span>
                        )}
                      </td>
                      <td className="td text-right font-mono tnum">
                        {bars > 0 ? (
                          <span className="text-slate-200">{compact(bars)}</span>
                        ) : (
                          <Badge tone="loss">none</Badge>
                        )}
                      </td>
                      <td className="td whitespace-nowrap font-mono text-2xs text-slate-500">
                        {stored?.first ? `${date(stored.first)} → ${date(stored.last)}` : '—'}
                      </td>
                      <td className="td max-w-[16rem]">
                        {caveats.length > 0 ? (
                          <span
                            className="line-clamp-2 text-2xs leading-snug text-caution"
                            title={caveats.join('\n\n')}
                          >
                            {caveats.join(' ')}
                          </span>
                        ) : (
                          <span className="text-slate-700">{'—'}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {rows.length === 0 && (
                  <tr>
                    <td className="td text-center text-slate-500" colSpan={8}>
                      No instruments match this filter.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
