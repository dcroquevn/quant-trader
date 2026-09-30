import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { AlertTriangle, Info, TrendingDown, TrendingUp } from 'lucide-react';
import { useState } from 'react';

import { api, type ScenarioSet } from '../lib/api';
import { compact, num, pct, price as fmtPrice } from '../lib/format';
import { Badge, Card, Caveat, ErrorState, Loading } from './ui';

/**
 * Historical-analogue projection.
 *
 * The hardest thing to get right on this page is not the chart, it is the wording. A
 * percentile band renders exactly like a price target, so every affordance here pushes the
 * other way:
 *
 * - The panel is titled by what it measures ("what followed similar setups"), never by what
 *   might happen.
 * - "Base case" carries its own definition inline — the median of past observations, half of
 *   which did worse — rather than being left to imply a most-likely outcome.
 * - The unconditional base rate is drawn on the same scale. When the conditional band
 *   barely differs from it, the panel says the matching found nothing, because a band that
 *   restates the base rate looks identical to one that discovered something.
 * - Insufficient evidence renders as a statement, with no numbers at all.
 *
 * Every string that interprets a figure comes from the backend verbatim (`basis`,
 * `language_note`, `reason`). Paraphrasing them here is how a caveat quietly turns into a
 * forecast.
 */
export function Projection({
  symbol,
  market,
  currency,
}: {
  symbol: string;
  market?: string;
  currency: string;
}) {
  const [horizon, setHorizon] = useState<string>('20');

  const projection = useQuery({
    queryKey: ['projection', symbol, market],
    queryFn: () => api.projection(symbol, market),
    staleTime: 10 * 60 * 1000,
  });

  if (projection.isLoading) {
    return (
      <Card title="Historical analogues" subtitle="Searching comparable past conditions">
        <Loading rows={4} />
      </Card>
    );
  }
  if (projection.isError) {
    return (
      <Card title="Historical analogues">
        <ErrorState error={projection.error} onRetry={() => projection.refetch()} />
      </Card>
    );
  }
  if (!projection.data) return null;

  const data = projection.data;
  const horizons = Object.keys(data.horizons).sort((a, b) => Number(a) - Number(b));
  const current = data.horizons[horizon] ?? data.horizons[horizons[0]];

  return (
    <Card
      title="What followed similar historical situations"
      subtitle={
        `Matched on ${data.match_features.length} scale-free dimensions, pooled over ` +
        `${data.pooled_symbols.length} instruments. This is a description of the past, not a forecast.`
      }
      actions={
        <div className="inline-flex rounded border border-terminal-700 p-0.5">
          {horizons.map((h) => {
            const available = data.horizons[h]?.available;
            return (
              <button
                key={h}
                type="button"
                onClick={() => setHorizon(h)}
                aria-pressed={horizon === h}
                title={available ? undefined : 'Insufficient historical evidence at this horizon'}
                className={clsx(
                  'rounded px-2.5 py-1 text-2xs font-medium transition',
                  horizon === h
                    ? 'bg-terminal-700 text-slate-100'
                    : 'text-slate-400 hover:text-slate-200',
                  !available && horizon !== h && 'text-slate-600',
                )}
              >
                {h}b{!available && ' ·'}
              </button>
            );
          })}
        </div>
      }
    >
      {current ? (
        <HorizonPanel scenarios={current} currency={currency} />
      ) : (
        <p className="py-6 text-center text-sm text-slate-500">No horizon available.</p>
      )}

      <p className="mt-4 text-2xs leading-relaxed text-slate-600">{data.note}</p>
    </Card>
  );
}

/* ------------------------------------------------------------------ */

function HorizonPanel({
  scenarios,
  currency,
}: {
  scenarios: ScenarioSet;
  currency: string;
}) {
  if (!scenarios.available) {
    return (
      <div className="py-6">
        <div className="mx-auto max-w-xl text-center">
          <Info className="mx-auto h-6 w-6 text-slate-600" />
          <h4 className="mt-2 text-sm font-medium text-caution">Insufficient historical evidence</h4>
          <p className="mt-2 text-2xs leading-relaxed text-slate-500">{scenarios.reason}</p>
          <p className="mt-3 text-2xs leading-relaxed text-slate-600">
            No projection is shown. A median computed from too few observations renders
            exactly like one computed from many, so the only safe response is to withhold it.
          </p>
          {scenarios.n_raw_matches > 0 && (
            <p className="mt-2 font-mono text-2xs text-slate-600">
              {compact(scenarios.n_raw_matches)} bars matched · {scenarios.n_observations}{' '}
              independent after removing overlap
            </p>
          )}
        </div>
      </div>
    );
  }

  const bear = scenarios.scenarios.find((s) => s.label === 'bear');
  const base = scenarios.scenarios.find((s) => s.label === 'base');
  const bull = scenarios.scenarios.find((s) => s.label === 'bull');
  const baseline = scenarios.baseline;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="neutral">
          {scenarios.n_observations} independent observations
        </Badge>
        <Badge tone="neutral" title="Before removing overlapping forward windows">
          from {compact(scenarios.n_raw_matches)} matches
        </Badge>
        {scenarios.positive_share_pct !== null && (
          <Badge tone="neutral">
            {num(scenarios.positive_share_pct, 0)}% were followed by a gain
          </Badge>
        )}
        {!scenarios.adds_information && (
          <Badge tone="loss">
            <AlertTriangle className="mr-1 h-2.5 w-2.5" />
            matches the base rate
          </Badge>
        )}
      </div>

      {/* The distribution as a band, with the base rate on the same scale. */}
      <DistributionBand
        bear={bear?.return_pct ?? 0}
        base={base?.return_pct ?? 0}
        bull={bull?.return_pct ?? 0}
        baselineBear={baseline?.p10_return_pct ?? null}
        baselineBase={baseline?.median_return_pct ?? null}
        baselineBull={baseline?.p90_return_pct ?? null}
      />

      <div className="grid gap-3 sm:grid-cols-3">
        {scenarios.scenarios.map((scenario) => {
          const tone =
            scenario.label === 'bear'
              ? 'border-loss/30 bg-loss/5'
              : scenario.label === 'bull'
                ? 'border-gain/30 bg-gain/5'
                : 'border-terminal-700 bg-terminal-850';
          const colour =
            scenario.label === 'bear'
              ? 'text-loss'
              : scenario.label === 'bull'
                ? 'text-gain'
                : 'text-slate-100';

          return (
            <div key={scenario.label} className={clsx('rounded border p-3', tone)}>
              <div className="flex items-baseline justify-between">
                <span className="text-2xs font-semibold uppercase tracking-wider text-slate-400">
                  {scenario.label}
                </span>
                <span className="font-mono text-2xs text-slate-600">
                  p{scenario.percentile}
                </span>
              </div>
              <div className={clsx('mt-1 font-mono text-xl font-semibold tnum', colour)}>
                {pct(scenario.return_pct)}
              </div>
              {scenario.price !== null && (
                <div className="mt-0.5 font-mono text-2xs tnum text-slate-500">
                  {fmtPrice(scenario.price, currency)} {currency}
                </div>
              )}
              {/* The definition travels with the number, verbatim from the backend. */}
              <p className="mt-2 text-2xs leading-relaxed text-slate-500">{scenario.basis}</p>
              {scenario.adverse_excursion_pct !== null && (
                <p className="mt-1.5 flex items-center gap-1 text-2xs text-slate-600">
                  {scenario.label === 'bull' ? (
                    <TrendingUp className="h-2.5 w-2.5" />
                  ) : (
                    <TrendingDown className="h-2.5 w-2.5" />
                  )}
                  excursion {pct(scenario.adverse_excursion_pct, 1)}
                </p>
              )}
            </div>
          );
        })}
      </div>

      {baseline?.available && (
        <div
          className={clsx(
            'rounded border px-3 py-2',
            scenarios.adds_information ? 'border-terminal-800' : 'border-loss/30 bg-loss/5',
          )}
        >
          <div className="label mb-1">Unconditional base rate, same bars</div>
          <div className="flex flex-wrap gap-x-5 gap-y-1 font-mono text-2xs text-slate-400">
            <span>p10 {pct(baseline.p10_return_pct, 2)}</span>
            <span>median {pct(baseline.median_return_pct, 2)}</span>
            <span>p90 {pct(baseline.p90_return_pct, 2)}</span>
            <span className="text-slate-600">
              matched {num(baseline.match_share_pct, 1)}% of {compact(baseline.n_bars)} bars
            </span>
          </div>
          <p
            className={clsx(
              'mt-1.5 text-2xs leading-relaxed',
              scenarios.adds_information ? 'text-slate-600' : 'text-loss',
            )}
          >
            {scenarios.adds_information
              ? 'The conditional distribution differs from the base rate, so the matching isolated something.'
              : 'These percentiles barely differ from the base rate. The matching did not isolate anything: they describe how these instruments behaved generally over this period, not what this setup preceded.'}
          </p>
        </div>
      )}

      {scenarios.caveats.length > 0 && (
        <details className="group">
          <summary className="cursor-pointer text-2xs text-slate-500 transition hover:text-slate-300">
            {scenarios.caveats.length} caveats on this distribution
          </summary>
          <div className="mt-2 space-y-2">
            {scenarios.caveats.map((caveat) => (
              <Caveat key={caveat} tone="neutral">
                {caveat}
              </Caveat>
            ))}
          </div>
        </details>
      )}

      <p className="text-2xs leading-relaxed text-slate-600">{scenarios.language_note}</p>

      {scenarios.setup && Object.keys(scenarios.setup).length > 0 && (
        <details>
          <summary className="cursor-pointer text-2xs text-slate-500 transition hover:text-slate-300">
            The setup that was matched
          </summary>
          <dl className="mt-2 grid gap-x-6 gap-y-1 sm:grid-cols-2 lg:grid-cols-3">
            {Object.entries(scenarios.setup)
              .sort()
              .map(([name, value]) => (
                <div
                  key={name}
                  className="flex items-baseline justify-between gap-2 border-b border-terminal-850 py-1"
                >
                  <dt className="truncate font-mono text-2xs text-slate-500">{name}</dt>
                  <dd className="font-mono text-2xs tnum text-slate-300">{num(value, 4)}</dd>
                </div>
              ))}
          </dl>
        </details>
      )}
    </div>
  );
}

/**
 * The percentile band, with the base rate drawn beneath it on the same scale.
 *
 * Two bands rather than one number: side by side, a conditional distribution that merely
 * reproduces the base rate is obvious at a glance, which no amount of prose achieves.
 */
function DistributionBand({
  bear,
  base,
  bull,
  baselineBear,
  baselineBase,
  baselineBull,
}: {
  bear: number;
  base: number;
  bull: number;
  baselineBear: number | null;
  baselineBase: number | null;
  baselineBull: number | null;
}) {
  const values = [bear, bull, baselineBear ?? bear, baselineBull ?? bull, 0];
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low || 1;

  const at = (value: number) => ((value - low) / span) * 100;

  return (
    <div className="rounded border border-terminal-800 px-3 pb-2 pt-3">
      <div className="relative h-14">
        {/* Zero line: the reference that makes a straddling range legible. */}
        <div
          className="absolute inset-y-0 w-px bg-terminal-600"
          style={{ left: `${at(0)}%` }}
          aria-hidden
        />
        <span
          className="absolute -top-0.5 -translate-x-1/2 text-[9px] text-slate-600"
          style={{ left: `${at(0)}%` }}
        >
          0%
        </span>

        {/* Conditional band. */}
        <div className="absolute left-0 right-0 top-5">
          <div
            className="absolute h-1.5 rounded bg-accent/25"
            style={{ left: `${at(bear)}%`, width: `${at(bull) - at(bear)}%` }}
          />
          <div
            className="absolute -top-1 h-3.5 w-[2px] rounded bg-accent"
            style={{ left: `${at(base)}%` }}
            title={`median ${base.toFixed(2)}%`}
          />
        </div>
        <span className="absolute left-0 top-[7px] text-[9px] text-slate-500">
          similar setups
        </span>

        {/* Base rate, same scale. */}
        {baselineBear !== null && baselineBull !== null && (
          <>
            <div className="absolute left-0 right-0 top-11">
              <div
                className="absolute h-1 rounded bg-terminal-600"
                style={{
                  left: `${at(baselineBear)}%`,
                  width: `${at(baselineBull) - at(baselineBear)}%`,
                }}
              />
              {baselineBase !== null && (
                <div
                  className="absolute -top-1 h-3 w-[2px] rounded bg-slate-500"
                  style={{ left: `${at(baselineBase)}%` }}
                  title={`base rate median ${baselineBase.toFixed(2)}%`}
                />
              )}
            </div>
            <span className="absolute left-0 top-[52px] text-[9px] text-slate-600">
              all bars
            </span>
          </>
        )}
      </div>

      <div className="mt-1 flex justify-between font-mono text-[9px] text-slate-600">
        <span>{pct(low, 1)}</span>
        <span>{pct(high, 1)}</span>
      </div>
    </div>
  );
}
