import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';

import { Badge, Card, ErrorState, Loading } from '../components/ui';
import { api } from '../lib/api';
import { marketFlag } from '../lib/format';

/**
 * Limitations, as a first-class page rather than a footnote.
 *
 * The reasoning is simple: the failure mode of a tool like this is not a wrong
 * number, it is a right number that gets over-trusted. Survivorship bias and a
 * substituted benchmark cannot be fixed with free data, so the only honest
 * response is to keep them one click away at all times.
 */
export default function Limitations() {
  const limitations = useQuery({ queryKey: ['limitations'], queryFn: api.limitations });

  if (limitations.isError)
    return <ErrorState error={limitations.error} onRetry={() => limitations.refetch()} />;

  const severityTone = { high: 'loss', medium: 'caution', low: 'neutral' } as const;
  const severityBorder = {
    high: 'border-loss/30',
    medium: 'border-caution/30',
    low: 'border-terminal-700',
  } as const;

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-slate-100">Known limitations</h2>
        <p className="mt-0.5 max-w-3xl text-xs leading-relaxed text-slate-500">
          These are properties of the data and the design, not bugs awaiting a fix. Several cannot
          be resolved with free data sources at all. Read them before drawing a conclusion from any
          number in this application.
        </p>
      </div>

      {limitations.isLoading ? (
        <Loading rows={6} label="Loading limitations" />
      ) : (
        <>
          <div className="grid gap-3 lg:grid-cols-2">
            {limitations.data?.limitations.map((item) => (
              <div key={item.id} className={clsx('card p-4', severityBorder[item.severity])}>
                <div className="flex items-start justify-between gap-3">
                  <h3 className="text-sm font-semibold text-slate-100">{item.title}</h3>
                  <Badge tone={severityTone[item.severity]}>{item.severity}</Badge>
                </div>
                <p className="mt-2 text-xs leading-relaxed text-slate-400">{item.detail}</p>
              </div>
            ))}
          </div>

          <Card
            title="Instrument-specific caveats"
            subtitle="Recorded against each asset and carried into every report"
          >
            <div className="space-y-2">
              {limitations.data?.instrument_notes.map((note) => (
                <div
                  key={`${note.market}:${note.symbol}`}
                  className="flex gap-3 rounded border border-terminal-800 p-3"
                >
                  <div className="shrink-0">
                    <span className="font-mono text-xs font-medium text-slate-200">
                      <span aria-hidden>{marketFlag(note.market)}</span> {note.symbol}
                    </span>
                  </div>
                  <p className="text-2xs leading-relaxed text-slate-400">{note.note}</p>
                </div>
              ))}
              {limitations.data?.instrument_notes.length === 0 && (
                <p className="text-xs text-slate-500">No instrument-specific caveats recorded.</p>
              )}
            </div>
          </Card>

          <Card title="How this project talks about the future" subtitle="Language rules, applied everywhere">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <div className="label mb-2 text-loss">Never says</div>
                <ul className="space-y-1.5 text-xs text-slate-400">
                  <li>&ldquo;NVDA will rise 8%&rdquo;</li>
                  <li>&ldquo;This strategy is profitable&rdquo;</li>
                  <li>&ldquo;Score 0.75 means 75% chance of a gain&rdquo;</li>
                  <li>&ldquo;Expected return: +3.4%&rdquo;</li>
                </ul>
              </div>
              <div>
                <div className="label mb-2 text-gain">Says instead</div>
                <ul className="space-y-1.5 text-xs text-slate-400">
                  <li>&ldquo;In 127 similar historical setups, the median 20-day return was +3.4%&rdquo;</li>
                  <li>&ldquo;Over this sample, with these costs, the strategy returned X&rdquo;</li>
                  <li>&ldquo;Score 0.75 means 3 of 4 conditions currently hold&rdquo;</li>
                  <li>&ldquo;Insufficient historical evidence&rdquo;</li>
                </ul>
              </div>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}
