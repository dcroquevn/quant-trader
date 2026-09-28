import { useQuery } from '@tanstack/react-query';

import { Badge, Card, Caveat, ErrorState, Loading, Stat } from '../components/ui';
import { api } from '../lib/api';
import { compact, date, marketFlag } from '../lib/format';

/**
 * Data inventory and provider cost.
 *
 * Instruments with no data are listed, not hidden. A universe page that quietly
 * omits what failed to download leaves the user believing they have coverage they
 * do not have.
 */
export default function DataHealth() {
  const coverage = useQuery({ queryKey: ['coverage', '1D'], queryFn: () => api.coverage('1D') });
  const providers = useQuery({ queryKey: ['providers'], queryFn: api.providers });

  if (coverage.isError) return <ErrorState error={coverage.error} onRetry={() => coverage.refetch()} />;

  const rows = coverage.data?.rows ?? [];
  const empty = rows.filter((r) => r.bars === 0);
  const staleTail = rows
    .filter((r) => r.last_bar)
    .sort((a, b) => (a.last_bar! < b.last_bar! ? -1 : 1))
    .slice(0, 3);

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-slate-100">Data health</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          What is stored, where it came from, and what it cost.
        </p>
      </div>

      {coverage.isLoading ? (
        <Loading rows={3} label="Loading coverage" />
      ) : (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat label="Total bars" value={compact(coverage.data?.total_bars)} />
          <Stat label="With data" value={String(coverage.data?.assets_with_data ?? 0)} />
          <Stat
            label="Without data"
            value={String(coverage.data?.assets_without_data ?? 0)}
            tone={empty.length > 0 ? 'caution' : 'neutral'}
          />
          <Stat label="Oldest last bar" value={date(staleTail[0]?.last_bar)} hint="Most out-of-date instrument" />
        </div>
      )}

      {empty.length > 0 && (
        <Caveat>
          <strong>
            {empty.length} declared instrument{empty.length === 1 ? '' : 's'} have no stored data:
          </strong>{' '}
          {empty.map((r) => r.symbol).join(', ')}. The IPSA index is expected to be empty -- no free
          provider carries it. Anything else here failed to download.
        </Caveat>
      )}

      <Card title="Coverage by instrument" subtitle={`Timeframe ${coverage.data?.timeframe ?? '1D'}`}>
        {coverage.isLoading ? (
          <Loading rows={8} />
        ) : (
          <div className="-m-4 max-h-[32rem] overflow-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="th">Market</th>
                  <th className="th">Symbol</th>
                  <th className="th">Vendor ticker</th>
                  <th className="th text-right">Bars</th>
                  <th className="th">First</th>
                  <th className="th">Last</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={`${row.market}:${row.symbol}`} className="hover:bg-terminal-850">
                    <td className="td whitespace-nowrap text-slate-400">
                      <span aria-hidden>{marketFlag(row.market)}</span> {row.market}
                    </td>
                    <td className="td whitespace-nowrap font-mono text-slate-200">{row.symbol}</td>
                    <td className="td whitespace-nowrap font-mono text-2xs text-slate-500">
                      {row.provider_symbol || '—'}
                    </td>
                    <td className="td text-right font-mono tnum">
                      {row.bars > 0 ? (
                        <span className="text-slate-200">{row.bars.toLocaleString('en-US')}</span>
                      ) : (
                        <Badge tone="loss">0</Badge>
                      )}
                    </td>
                    <td className="td whitespace-nowrap font-mono text-2xs text-slate-500">
                      {date(row.first_bar)}
                    </td>
                    <td className="td whitespace-nowrap font-mono text-2xs text-slate-500">
                      {date(row.last_bar)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Providers" subtitle="Every source wired into this project, with its cost">
        {providers.isLoading ? (
          <Loading rows={3} />
        ) : (
          <div className="space-y-3">
            {providers.data?.providers.map((provider) => (
              <div key={provider.provider} className="rounded border border-terminal-800 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm font-medium text-slate-100">
                    {provider.provider}
                  </span>
                  <Badge tone="gain">{provider.cost}</Badge>
                  <Badge tone={provider.api_key_required === 'no' ? 'neutral' : 'caution'}>
                    API key: {provider.api_key_required}
                  </Badge>
                  <Badge tone="neutral">{provider.markets}</Badge>
                  <Badge tone="neutral">{provider.timeframes}</Badge>
                </div>
                {provider.notes && (
                  <p className="mt-2 text-2xs leading-relaxed text-slate-500">{provider.notes}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
