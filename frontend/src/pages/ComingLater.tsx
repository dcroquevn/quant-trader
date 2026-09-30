import { Link } from 'react-router-dom';

import { Card } from '../components/ui';

/**
 * Placeholder for a route the brief specifies but whose engine does not exist yet.
 *
 * Currently unused: every route in the nav is now backed by something real. Kept because Phases 7
 * and 8 will add routes again, and because the rule it encodes is worth not relearning -- see the
 * note about empty states below.
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
