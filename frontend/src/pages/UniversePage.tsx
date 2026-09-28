import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { Search } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge, Card, ErrorState, Loading } from '../components/ui';
import { api } from '../lib/api';
import { compact, date, marketFlag } from '../lib/format';

type MarketFilter = 'ALL' | 'USA' | 'CHILE';

export default function UniversePage() {
  const [market, setMarket] = useState<MarketFilter>('ALL');
  const [query, setQuery] = useState('');

  const universe = useQuery({ queryKey: ['universe'], queryFn: () => api.universe() });
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
      if (market !== 'ALL' && asset.market !== market) return false;
      if (!needle) return true;
      return (
        asset.symbol.toLowerCase().includes(needle) ||
        asset.name.toLowerCase().includes(needle) ||
        asset.sector.toLowerCase().includes(needle)
      );
    });
  }, [universe.data, market, query]);

  if (universe.isError) return <ErrorState error={universe.error} onRetry={() => universe.refetch()} />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">Universe</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            {universe.data?.count ?? 0} instruments. Provider symbols verified{' '}
            {universe.data?.verification_date ?? '—'} against live responses.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex rounded border border-terminal-700 p-0.5">
            {(['ALL', 'USA', 'CHILE'] as const).map((code) => (
              <button
                key={code}
                type="button"
                onClick={() => setMarket(code)}
                className={clsx(
                  'rounded px-2.5 py-1 text-2xs font-medium transition',
                  market === code
                    ? 'bg-terminal-700 text-slate-100'
                    : 'text-slate-400 hover:text-slate-200',
                )}
              >
                {code === 'ALL' ? 'All' : `${marketFlag(code)} ${code}`}
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

      <Card>
        {universe.isLoading ? (
          <Loading rows={8} label="Loading universe" />
        ) : (
          <div className="-m-4 overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="th">Market</th>
                  <th className="th">Symbol</th>
                  <th className="th">Name</th>
                  <th className="th">Sector</th>
                  <th className="th">Vendor ticker</th>
                  <th className="th text-right">Bars</th>
                  <th className="th">Range</th>
                  <th className="th">Caveat</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((asset) => {
                  const stored = barsBySymbol.get(`${asset.market}:${asset.symbol}`);
                  const bars = stored?.bars ?? 0;
                  return (
                    <tr key={`${asset.market}:${asset.symbol}`} className="hover:bg-terminal-850">
                      <td className="td whitespace-nowrap text-slate-400">
                        <span aria-hidden>{marketFlag(asset.market)}</span> {asset.market}
                      </td>
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
                      </td>
                      <td className="td max-w-[18rem] truncate text-slate-300" title={asset.name}>
                        {asset.name}
                      </td>
                      <td className="td whitespace-nowrap text-slate-400">{asset.sector}</td>
                      <td className="td whitespace-nowrap font-mono text-2xs text-slate-500">
                        {asset.provider_symbol || '—'}
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
                        {asset.notes ? (
                          <span className="line-clamp-2 text-2xs leading-snug text-caution" title={asset.notes}>
                            {asset.notes}
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
