import { Link } from 'react-router-dom';

import { Card } from '../components/ui';

/**
 * Placeholder for a route the brief specifies but whose engine does not exist yet.
 *
 * These pages exist rather than being omitted because the routes are part of the agreed
 * design, and a 404 would read as a bug. What they must not do is render a plausible
 * empty state: a portfolio page showing "$0.00 / 0.00% / 0 positions" looks like a flat
 * account, not an absent one, and that is the more expensive misreading.
 */
export function ComingLater({
  title,
  phase,
  summary,
  willShow,
  blockedBy,
  cli,
}: {
  title: string;
  phase: string;
  summary: string;
  willShow: string[];
  blockedBy?: string;
  cli?: string;
}) {
  return (
    <div className="space-y-4">
      <header>
        <h2 className="text-lg font-semibold text-slate-100">{title}</h2>
        <p className="mt-0.5 text-xs text-slate-500">Scheduled for Phase {phase}</p>
      </header>

      <Card>
        <div className="mx-auto max-w-2xl py-6">
          <p className="text-sm leading-relaxed text-slate-300">{summary}</p>

          {blockedBy && (
            <p className="mt-4 rounded border border-terminal-700 bg-terminal-850 px-3 py-2 text-2xs leading-relaxed text-slate-400">
              {blockedBy}
            </p>
          )}

          <h3 className="mt-6 text-2xs font-semibold uppercase tracking-wider text-slate-500">
            What this page will show
          </h3>
          <ul className="mt-2 space-y-1.5">
            {willShow.map((item) => (
              <li key={item} className="flex gap-2 text-xs text-slate-400">
                <span
                  className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent/50"
                  aria-hidden
                />
                {item}
              </li>
            ))}
          </ul>

          {cli && (
            <>
              <h3 className="mt-6 text-2xs font-semibold uppercase tracking-wider text-slate-500">
                Current behaviour
              </h3>
              <pre className="mt-2 overflow-x-auto rounded bg-terminal-950 p-3 text-2xs text-slate-400">
                {cli}
              </pre>
            </>
          )}

          <p className="mt-6 text-2xs leading-relaxed text-slate-600">
            This page shows nothing rather than showing zeros. An empty portfolio and a
            portfolio worth nothing look identical on a dashboard, and they are not the same
            thing. Meanwhile,{' '}
            <Link to="/scanner" className="text-accent hover:underline">
              the scanner
            </Link>{' '}
            and{' '}
            <Link to="/backtest" className="text-accent hover:underline">
              backtests
            </Link>{' '}
            work today.
          </p>
        </div>
      </Card>
    </div>
  );
}

export function Portfolio() {
  return (
    <ComingLater
      title="Portfolio"
      phase="6"
      summary={
        'Live cash, equity, open positions and trade history for the paper-trading account, ' +
        'separated by market so a USD book and a CLP book are never summed.'
      }
      blockedBy={
        'Blocked on the paper broker, not on this page. There is no portfolio yet because ' +
        'nothing has traded: paper execution arrives in Phase 6, using Alpaca for US ' +
        'equities and an internal broker for Chile, since no free Chilean execution API ' +
        'was found.'
      }
      willShow={[
        'Cash and equity per market, never combined across currencies',
        'Open positions with entry price, current mark, unrealised P&L and the binding stop',
        'Daily P&L and drawdown against the account high-water mark',
        'Exposure by market and by sector, against the configured risk limits',
        'Full trade history with entry and exit reasons',
      ]}
      cli={'python -m app paper\n  → Not implemented yet. Belongs to Phase 6.'}
    />
  );
}

export function Optimization() {
  return (
    <ComingLater
      title="Optimization"
      phase="4"
      summary={
        'Parameter search results, ranked by a configurable objective rather than by return ' +
        'alone, with walk-forward windows and parameter-stability analysis.'
      }
      blockedBy={
        'The split guard this depends on already exists: the optimiser will only ever read ' +
        'the TRAIN partition, selection happens on VALIDATION, and TEST stays sealed until a ' +
        'result is finalised. What is missing is the search itself.'
      }
      willShow={[
        'Parameter search results sorted by a weighted objective, never by return alone',
        'Train, validation and test performance side by side, each labelled with its partition',
        'Walk-forward windows: chosen parameters, out-of-sample metrics and trade counts per window',
        'Parameter-stability heatmaps — a strategy that only works at one exact setting is overfit',
        'Robustness checks: slippage and commission sensitivity, Monte Carlo trade reshuffling',
      ]}
      cli={
        'python -m app optimize\n  → Not implemented yet. Belongs to Phase 4.\n\n' +
        'python -m app walk-forward\n  → Not implemented yet. Belongs to Phase 4.'
      }
    />
  );
}
