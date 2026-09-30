import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { AlertTriangle, CheckCircle2, ChevronRight, Lock, Terminal } from 'lucide-react';
import { useState } from 'react';

import { SignedBarChart, TimeSeriesChart } from '../components/charts';
import { Badge, Card, Caveat, ErrorState, Loading, Stat } from '../components/ui';
import {
  api,
  type OptimizationRunDetail,
  type ParameterStability,
  type RobustnessCheck,
  type WalkForwardDetail,
} from '../lib/api';
import { DASH, compact, date, num, pct, signClass } from '../lib/format';

/**
 * Optimisation page.
 *
 * Read-only by design. A search takes tens of minutes and a walk-forward study longer, so
 * runs are produced by the CLI and displayed here. An endpoint that ran one would either
 * time out or silently re-run an hour of computation whenever someone opened the page.
 *
 * The presentation problem this page exists to solve: **a search result is the maximum of
 * many attempts, and a maximum looks like a discovery.** So the page leads with the
 * warnings, shows validation degradation next to every candidate, and gives walk-forward
 * — the only genuinely out-of-sample view — its own prominent section.
 */
export default function Optimization() {
  const [tab, setTab] = useState<'walkforward' | 'search'>('walkforward');
  const [openSearch, setOpenSearch] = useState<number | null>(null);
  const [openStudy, setOpenStudy] = useState<number | null>(null);

  const searches = useQuery({
    queryKey: ['optimizationRuns'],
    queryFn: () => api.optimizationRuns(),
  });
  const studies = useQuery({
    queryKey: ['walkForwardRuns'],
    queryFn: () => api.walkForwardRuns(),
  });

  const hasNothing =
    !searches.isLoading &&
    !studies.isLoading &&
    (searches.data?.count ?? 0) === 0 &&
    (studies.data?.count ?? 0) === 0;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">Optimization</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Parameter searches and walk-forward studies, produced by the CLI and read here.
          </p>
        </div>
        <div className="inline-flex rounded border border-terminal-700 p-0.5">
          {(
            [
              ['walkforward', 'Walk-forward'],
              ['search', 'Parameter search'],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setTab(value)}
              aria-pressed={tab === value}
              className={clsx(
                'rounded px-3 py-1 text-2xs font-medium transition',
                tab === value
                  ? 'bg-terminal-700 text-slate-100'
                  : 'text-slate-400 hover:text-slate-200',
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </header>

      <Caveat tone="neutral">
        <strong>The TEST partition has not been read by anything on this page.</strong> Searches
        read TRAIN only and selection happens on VALIDATION. Finalising a test result is a
        CLI-only action, so a dashboard click cannot spend the one out-of-sample estimate you
        have.
      </Caveat>

      {hasNothing ? (
        <NoRuns />
      ) : tab === 'walkforward' ? (
        <>
          {studies.isLoading && <Loading rows={4} label="Loading studies" />}
          {studies.isError && (
            <ErrorState error={studies.error} onRetry={() => studies.refetch()} />
          )}
          {studies.data?.runs.length === 0 && (
            <EmptyTab
              what="walk-forward study"
              command="python -m app walk-forward --market USA"
              why="The only genuinely out-of-sample measurement this project produces."
            />
          )}
          <div className="space-y-3">
            {studies.data?.runs.map((run) => (
              <StudyCard
                key={run.id}
                summary={run}
                open={openStudy === run.id}
                onToggle={() => setOpenStudy(openStudy === run.id ? null : run.id)}
              />
            ))}
          </div>
        </>
      ) : (
        <>
          {searches.isLoading && <Loading rows={4} label="Loading searches" />}
          {searches.isError && (
            <ErrorState error={searches.error} onRetry={() => searches.refetch()} />
          )}
          {searches.data?.runs.length === 0 && (
            <EmptyTab
              what="parameter search"
              command="python -m app optimize --market USA --trials 40"
              why="Searches TRAIN, then re-runs the shortlist on VALIDATION."
            />
          )}
          <div className="space-y-3">
            {searches.data?.runs.map((run) => (
              <SearchCard
                key={run.id}
                id={run.id}
                label={run.label}
                market={run.market}
                method={run.method}
                nTrials={run.n_trials}
                nFailed={run.n_failed}
                bestObjective={run.best_objective}
                gridSize={run.grid_size}
                overfittingProne={run.overfitting_prone}
                warnings={run.warnings}
                hasValidation={run.has_validation}
                hasRobustness={run.has_robustness}
                finishedAt={run.finished_at}
                open={openSearch === run.id}
                onToggle={() => setOpenSearch(openSearch === run.id ? null : run.id)}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Walk-forward                                                       */
/* ------------------------------------------------------------------ */

function StudyCard({
  summary,
  open,
  onToggle,
}: {
  summary: { id: number; label: string; market: string; n_windows: number; n_usable: number;
    n_losing_windows: number; total_return_pct: number | null; sharpe: number | null;
    max_drawdown_pct: number | null; n_trades: number | null; unstable_parameters: string[];
    warnings: string[]; finished_at: string | null; train_years: number; test_years: number };
  open: boolean;
  onToggle: () => void;
}) {
  const detail = useQuery({
    queryKey: ['walkForwardRun', summary.id],
    queryFn: () => api.walkForwardRun(summary.id),
    enabled: open,
  });

  const losingShare =
    summary.n_usable > 0 ? summary.n_losing_windows / summary.n_usable : 0;

  return (
    <Card
      title={
        <button type="button" onClick={onToggle} className="flex items-center gap-2 text-left">
          <ChevronRight
            className={clsx('h-3.5 w-3.5 shrink-0 transition', open && 'rotate-90')}
          />
          {summary.label}
        </button>
      }
      subtitle={`${summary.train_years}y train / ${summary.test_years}y test · ${summary.n_usable}/${summary.n_windows} usable windows · ${date(summary.finished_at)}`}
      actions={
        <div className="flex items-center gap-2">
          <Badge tone="accent">out-of-sample</Badge>
          {summary.n_losing_windows > 0 && (
            <Badge tone={losingShare > 0.4 ? 'loss' : 'caution'}>
              {summary.n_losing_windows} losing
            </Badge>
          )}
        </div>
      }
    >
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
        <Stat
          label="OOS return"
          value={pct(summary.total_return_pct)}
          tone={toneOf(summary.total_return_pct)}
          hint="Stitched across windows"
        />
        <Stat label="OOS Sharpe" value={num(summary.sharpe, 2)} />
        <Stat label="OOS max drawdown" value={pct(summary.max_drawdown_pct)} tone="loss" />
        <Stat label="Trades" value={String(summary.n_trades ?? 0)} />
        <Stat
          label="Losing windows"
          value={`${summary.n_losing_windows}/${summary.n_usable}`}
          tone={losingShare > 0.4 ? 'loss' : 'neutral'}
          hint="Consistency matters more than the aggregate"
        />
      </div>

      {summary.unstable_parameters.length > 0 && (
        <div className="mt-3">
          <Caveat>
            <strong>
              {summary.unstable_parameters.join(', ')} changed in more than half the windows.
            </strong>{' '}
            The procedure is re-fitting these each period rather than finding a stable
            setting, so the value it would pick for the next period carries little
            information — even if the stitched curve looks acceptable.
          </Caveat>
        </div>
      )}

      {summary.warnings.map((w) => (
        <div key={w} className="mt-2">
          <Caveat tone="neutral">{w}</Caveat>
        </div>
      ))}

      {open && (
        <div className="mt-4 space-y-4 border-t border-terminal-750 pt-4">
          {detail.isLoading && <Loading rows={4} />}
          {detail.isError && <ErrorState error={detail.error} />}
          {detail.data && <StudyDetail study={detail.data} />}
        </div>
      )}
    </Card>
  );
}

function StudyDetail({ study }: { study: WalkForwardDetail }) {
  const equity = study.equity_curve.map((p) => ({
    ts: p.ts.slice(0, 10),
    equity: p.equity,
  }));
  const perWindow = study.windows
    .filter((w) => !w.error)
    .map((w) => ({
      period: String(w.window.test_start).slice(0, 7),
      return_pct: w.test.total_return_pct ?? null,
    }));

  return (
    <>
      <TimeSeriesChart
        title="Stitched out-of-sample equity"
        note="Each window traded with parameters frozen before it began, compounded across windows. This is the honest headline; the per-window train figures below are diagnostic only."
        data={equity}
        series={[{ key: 'equity', label: 'Out-of-sample equity', ink: true }]}
        height={260}
        digits={0}
        fillFirst
      />

      <SignedBarChart
        title="Return by out-of-sample window"
        note="One bar per test window. A single strong window can carry an otherwise failing strategy, which is why consistency is read separately from the aggregate."
        data={perWindow}
        xKey="period"
        valueKey="return_pct"
        height={180}
      />

      <Card title="Windows" subtitle="Frozen parameters and the out-of-sample result each produced">
        <div className="-m-4 overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className="th">#</th>
                <th className="th">Test period</th>
                <th className="th text-right">OOS return</th>
                <th className="th text-right">OOS Sharpe</th>
                <th className="th text-right">OOS maxDD</th>
                <th className="th text-right">Trades</th>
                <th className="th text-right">Train obj</th>
                <th className="th">Frozen parameters</th>
              </tr>
            </thead>
            <tbody>
              {study.windows.map((w) => (
                <tr key={w.window.index} className="hover:bg-terminal-850">
                  <td className="td text-slate-500">{w.window.index}</td>
                  <td className="td whitespace-nowrap font-mono text-2xs text-slate-400">
                    {w.window.test_start} &rarr; {w.window.test_end}
                  </td>
                  {w.error ? (
                    <td className="td text-2xs text-loss" colSpan={6}>
                      {w.error}
                    </td>
                  ) : (
                    <>
                      <td
                        className={clsx(
                          'td text-right font-mono tnum',
                          signClass(w.test.total_return_pct),
                        )}
                      >
                        {pct(w.test.total_return_pct)}
                      </td>
                      <td className="td text-right font-mono tnum text-slate-300">
                        {num(w.test.sharpe, 2)}
                      </td>
                      <td className="td text-right font-mono tnum text-slate-400">
                        {pct(w.test.max_drawdown_pct)}
                      </td>
                      <td className="td text-right font-mono tnum text-slate-400">
                        {w.test.n_trades ?? 0}
                      </td>
                      <td
                        className="td text-right font-mono tnum text-slate-600"
                        title="In-sample. Shown for diagnosis only."
                      >
                        {num(w.train.objective, 2)}
                      </td>
                      <td className="td max-w-[20rem]">
                        <span className="line-clamp-2 font-mono text-2xs text-slate-500">
                          {Object.entries(w.chosen_params)
                            .sort()
                            .map(([k, v]) => `${k}=${v}`)
                            .join('  ')}
                        </span>
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <StabilityCard stability={study.parameter_stability} perWindow />
      <p className="text-2xs leading-relaxed text-slate-600">{study.note}</p>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* Parameter search                                                   */
/* ------------------------------------------------------------------ */

function SearchCard(props: {
  id: number;
  label: string;
  market: string;
  method: string;
  nTrials: number;
  nFailed: number;
  bestObjective: number | null;
  gridSize: number | null;
  overfittingProne: boolean | null;
  warnings: string[];
  hasValidation: boolean;
  hasRobustness: boolean;
  finishedAt: string | null;
  open: boolean;
  onToggle: () => void;
}) {
  const detail = useQuery({
    queryKey: ['optimizationRun', props.id],
    queryFn: () => api.optimizationRun(props.id),
    enabled: props.open,
  });

  return (
    <Card
      title={
        <button
          type="button"
          onClick={props.onToggle}
          className="flex items-center gap-2 text-left"
        >
          <ChevronRight
            className={clsx('h-3.5 w-3.5 shrink-0 transition', props.open && 'rotate-90')}
          />
          {props.label}
        </button>
      }
      subtitle={`${props.method} search · ${props.nTrials} trials${
        props.nFailed ? ` (${props.nFailed} invalid)` : ''
      } of ${compact(props.gridSize)} combinations · ${date(props.finishedAt)}`}
      actions={
        <div className="flex items-center gap-2">
          <Badge tone="neutral" title="The search reads TRAIN only; it has no split argument">
            <Lock className="mr-1 h-2.5 w-2.5" />
            TRAIN
          </Badge>
          {props.hasValidation && <Badge tone="accent">validated</Badge>}
          {props.hasRobustness && <Badge tone="accent">stress-tested</Badge>}
        </div>
      }
    >
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat
          label="Best objective"
          value={num(props.bestObjective, 3)}
          hint="Arbitrary scale; comparable only within this run"
        />
        <Stat label="Trials" value={String(props.nTrials)} />
        <Stat label="Space size" value={compact(props.gridSize)} />
        <Stat
          label="Overfitting risk"
          value={props.overfittingProne ? 'elevated' : 'moderate'}
          tone={props.overfittingProne ? 'caution' : 'neutral'}
        />
      </div>

      {props.warnings.map((w) => (
        <div key={w} className="mt-3">
          <Caveat>{w}</Caveat>
        </div>
      ))}

      {props.open && (
        <div className="mt-4 space-y-4 border-t border-terminal-750 pt-4">
          {detail.isLoading && <Loading rows={5} />}
          {detail.isError && <ErrorState error={detail.error} />}
          {detail.data && <SearchDetail run={detail.data} />}
        </div>
      )}
    </Card>
  );
}

function SearchDetail({ run }: { run: OptimizationRunDetail }) {
  const selection = run.validation_selection;

  return (
    <>
      {selection ? (
        <Card
          title="Shortlist re-run on validation"
          subtitle={`Validation window ${selection.validation_window.start} → ${selection.validation_window.end}`}
        >
          <div className="-m-4 overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="th">#</th>
                  <th className="th text-right">Train obj</th>
                  <th className="th text-right">Valid obj</th>
                  <th className="th text-right">Degradation</th>
                  <th className="th text-right">Valid return</th>
                  <th className="th text-right">Valid Sharpe</th>
                  <th className="th text-right">Trades</th>
                  <th className="th">Parameters</th>
                </tr>
              </thead>
              <tbody>
                {selection.candidates.map((candidate, i) => {
                  const recommended =
                    selection.recommended &&
                    JSON.stringify(selection.recommended.params) ===
                      JSON.stringify(candidate.params);
                  return (
                    <tr
                      key={i}
                      className={clsx(
                        'hover:bg-terminal-850',
                        recommended && 'bg-accent/5 ring-1 ring-inset ring-accent/20',
                      )}
                    >
                      <td className="td text-slate-500">
                        {i + 1}
                        {recommended && (
                          <CheckCircle2 className="ml-1 inline h-3 w-3 text-accent" />
                        )}
                      </td>
                      <td className="td text-right font-mono tnum text-slate-400">
                        {num(candidate.train.objective, 3)}
                      </td>
                      <td className="td text-right font-mono tnum text-slate-100">
                        {candidate.validation === null
                          ? DASH
                          : num(candidate.validation.objective, 3)}
                      </td>
                      <td
                        className={clsx(
                          'td text-right font-mono tnum',
                          (candidate.degradation ?? 0) > 1 ? 'text-loss' : 'text-slate-400',
                        )}
                        title="How far the objective fell from train to validation. Some drop is always expected; a collapse means the train figure was fitted."
                      >
                        {candidate.degradation === null
                          ? DASH
                          : `${candidate.degradation > 0 ? '+' : ''}${num(candidate.degradation, 3)}`}
                      </td>
                      <td
                        className={clsx(
                          'td text-right font-mono tnum',
                          signClass(candidate.validation?.total_return_pct ?? null),
                        )}
                      >
                        {pct(candidate.validation?.total_return_pct ?? null)}
                      </td>
                      <td className="td text-right font-mono tnum text-slate-300">
                        {num(candidate.validation?.sharpe ?? null, 2)}
                      </td>
                      <td className="td text-right font-mono tnum text-slate-400">
                        {candidate.validation?.n_trades ?? DASH}
                      </td>
                      <td className="td max-w-[18rem]">
                        <span className="line-clamp-2 font-mono text-2xs text-slate-500">
                          {Object.entries(candidate.params)
                            .sort()
                            .map(([k, v]) => `${k}=${v}`)
                            .join('  ')}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="mt-4 space-y-1.5">
            {selection.notes.map((note) => (
              <p key={note} className="text-2xs leading-relaxed text-slate-500">
                &mdash; {note}
              </p>
            ))}
          </div>
        </Card>
      ) : (
        <Caveat>
          <strong>No validation step was run.</strong> The best configuration below was chosen
          on the same data it was fitted to, which makes it the most overfitted candidate by
          construction.
        </Caveat>
      )}

      <StabilityCard stability={run.parameter_stability} />

      {run.stress_tests && <StressTests tests={run.stress_tests} />}

      <Card
        title={`Trials (${run.trials.length} shown)`}
        subtitle="Scored on TRAIN. Fragility compares a trial against its immediate neighbours."
      >
        <div className="-m-4 max-h-[420px] overflow-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className="th">#</th>
                <th className="th text-right">Objective</th>
                <th className="th text-right">Return</th>
                <th className="th text-right">Sharpe</th>
                <th className="th text-right">maxDD</th>
                <th className="th text-right">Trades</th>
                <th className="th text-right">Fragility</th>
                <th className="th">Parameters</th>
              </tr>
            </thead>
            <tbody>
              {run.trials.map((trial, i) => (
                <tr key={i} className="hover:bg-terminal-850">
                  <td className="td text-slate-500">{i + 1}</td>
                  <td className="td text-right font-mono tnum text-slate-100">
                    {num(trial.objective.value, 3)}
                  </td>
                  <td
                    className={clsx(
                      'td text-right font-mono tnum',
                      signClass(trial.metrics.total_return_pct),
                    )}
                  >
                    {pct(trial.metrics.total_return_pct)}
                  </td>
                  <td className="td text-right font-mono tnum text-slate-300">
                    {num(trial.metrics.sharpe, 2)}
                  </td>
                  <td className="td text-right font-mono tnum text-slate-400">
                    {pct(trial.metrics.max_drawdown_pct)}
                  </td>
                  <td className="td text-right font-mono tnum text-slate-400">
                    {trial.metrics.n_trades ?? 0}
                  </td>
                  <td
                    className={clsx(
                      'td text-right font-mono tnum',
                      (trial.objective.fragility ?? 0) > 0.5
                        ? 'text-loss'
                        : 'text-slate-500',
                    )}
                    title="How far this trial scores above its neighbours, as a share of the neighbourhood spread. A spike is usually fitted."
                  >
                    {trial.objective.fragility === null
                      ? DASH
                      : num(trial.objective.fragility, 2)}
                  </td>
                  <td className="td max-w-[16rem]">
                    <span className="line-clamp-1 font-mono text-2xs text-slate-500">
                      {Object.entries(trial.params)
                        .sort()
                        .map(([k, v]) => `${k}=${v}`)
                        .join(' ')}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <p className="text-2xs leading-relaxed text-slate-600">{run.note}</p>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* Shared pieces                                                      */
/* ------------------------------------------------------------------ */

function StabilityCard({
  stability,
  perWindow = false,
}: {
  stability: ParameterStability;
  perWindow?: boolean;
}) {
  if (!stability.available) {
    return (
      <Card title="Parameter stability">
        <p className="py-4 text-center text-xs text-slate-500">
          Not available &mdash; {stability.reason}
        </p>
      </Card>
    );
  }

  return (
    <Card
      title="Parameter stability"
      subtitle={
        perWindow
          ? 'How consistently each parameter was chosen across windows. Wild variation means the procedure refits noise each period.'
          : 'How much each parameter varies across the best trials. A parameter taking every value among equally good results is either irrelevant or being fitted to noise.'
      }
    >
      <div className="-m-4 overflow-x-auto">
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className="th">Parameter</th>
              <th className="th text-right">Distinct</th>
              <th className="th text-right">Consistency</th>
              <th className="th">{perWindow ? 'Per window' : 'Range'}</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(stability.parameters ?? {})
              .sort()
              .map(([name, entry]) => {
                const weak = entry.concentration < (perWindow ? 0.5 : 0.4);
                return (
                  <tr key={name} className="hover:bg-terminal-850">
                    <td className="td font-mono text-slate-300">{name}</td>
                    <td className="td text-right font-mono tnum text-slate-400">
                      {entry.distinct_values}
                    </td>
                    <td
                      className={clsx(
                        'td text-right font-mono tnum',
                        weak ? 'text-loss' : 'text-slate-200',
                      )}
                    >
                      {num(entry.concentration * 100, 0)}%
                    </td>
                    <td className="td font-mono text-2xs text-slate-500">
                      {perWindow
                        ? (entry.values_by_window ?? []).join(', ')
                        : entry.min !== undefined
                          ? `${entry.min} .. ${entry.max}`
                          : DASH}
                    </td>
                  </tr>
                );
              })}
          </tbody>
        </table>
      </div>
      {stability.note && (
        <div className="mt-3">
          <Caveat>{stability.note}</Caveat>
        </div>
      )}
    </Card>
  );
}

function StressTests({
  tests,
}: {
  tests: {
    baseline: Record<string, number | null>;
    checks: Record<string, RobustnessCheck>;
    verdicts: string[];
    is_fragile: boolean;
    caveats: string[];
  };
}) {
  return (
    <Card
      title="Robustness"
      subtitle="What survives when the assumptions wobble"
      actions={
        <Badge tone={tests.is_fragile ? 'loss' : 'gain'}>
          {tests.is_fragile ? (
            <>
              <AlertTriangle className="mr-1 h-2.5 w-2.5" />
              fragile
            </>
          ) : (
            <>
              <CheckCircle2 className="mr-1 h-2.5 w-2.5" />
              no flags
            </>
          )}
        </Badge>
      }
    >
      <div className="space-y-2.5">
        {Object.entries(tests.checks).map(([name, check]) => {
          const fragile = String(check.verdict ?? '').startsWith('FRAGILE');
          return (
            <div
              key={name}
              className={clsx(
                'rounded border px-3 py-2',
                fragile ? 'border-loss/30 bg-loss/5' : 'border-terminal-800',
              )}
            >
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-2xs font-semibold uppercase tracking-wider text-slate-400">
                  {name.replace(/_/g, ' ')}
                </span>
                {!check.available && (
                  <span className="text-2xs text-slate-600">not available</span>
                )}
              </div>
              <p
                className={clsx(
                  'mt-1 text-2xs leading-relaxed',
                  fragile ? 'text-loss' : 'text-slate-400',
                )}
              >
                {check.verdict ?? check.reason}
              </p>
            </div>
          );
        })}
      </div>

      <div className="mt-4 space-y-2">
        {tests.caveats.map((c) => (
          <Caveat key={c} tone="neutral">
            {c}
          </Caveat>
        ))}
      </div>
    </Card>
  );
}

function EmptyTab({
  what,
  command,
  why,
}: {
  what: string;
  command: string;
  why: string;
}) {
  return (
    <Card>
      <div className="py-10 text-center">
        <p className="text-sm text-slate-400">No {what} has been run yet.</p>
        <p className="mx-auto mt-1 max-w-md text-2xs leading-relaxed text-slate-600">{why}</p>
        <pre className="mx-auto mt-4 inline-block rounded bg-terminal-950 px-3 py-2 text-left text-2xs text-accent">
          {command}
        </pre>
      </div>
    </Card>
  );
}

function NoRuns() {
  return (
    <Card>
      <div className="mx-auto max-w-2xl py-8">
        <div className="flex items-center gap-2 text-sm font-medium text-slate-300">
          <Terminal className="h-4 w-4" />
          Nothing has been optimised yet
        </div>
        <p className="mt-3 text-xs leading-relaxed text-slate-400">
          Searches and walk-forward studies are produced by the CLI and read here. They take
          tens of minutes, which is why this page does not offer to run one: an HTTP request
          that spends an hour computing either times out or quietly repeats the work on every
          page load.
        </p>
        <pre className="mt-4 overflow-x-auto rounded bg-terminal-950 p-3 text-2xs leading-relaxed text-slate-400">
          {`# search TRAIN, then select on VALIDATION
python -m app optimize --market USA --trials 40

# rolling out-of-sample: optimise, freeze, trade the next window
python -m app walk-forward --market USA

# stress-test one configuration
python -m app robustness --market USA

# list what has been stored
python -m app runs`}
        </pre>
        <p className="mt-4 text-2xs leading-relaxed text-slate-600">
          Start with <span className="font-mono text-slate-500">walk-forward</span>. It is
          slower but it is the only view here where every figure is out-of-sample with respect
          to the parameters that produced it.
        </p>
      </div>
    </Card>
  );
}

function toneOf(value: number | null | undefined): 'gain' | 'loss' | 'neutral' {
  if (value === null || value === undefined) return 'neutral';
  return value > 0 ? 'gain' : value < 0 ? 'loss' : 'neutral';
}
