import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, CircleSlash, MailWarning } from 'lucide-react';

import { Badge, Card, Caveat, EmptyState, ErrorState, Loading, Stat } from '../components/ui';
import { api } from '../lib/api';
import { date, pct, price, signClass } from '../lib/format';

/**
 * Real positions, and what the exit rule says about them.
 *
 * Read-only on purpose. Recording a purchase happens through the CLI, which runs where the user
 * is; an HTTP endpoint that created position records would make the dashboard — the surface most
 * likely to end up exposed somewhere it should not be — able to write them.
 *
 * Three things this page is careful about:
 *
 * 1. **A trigger's date, not just its existence.** A rule that fired eleven months ago and one
 *    that fired this morning call for different reactions, and only one of them is news.
 * 2. **Positions that could not be checked.** These are shown first and loudly. A holding the
 *    user believes is being watched, that is not, is the worst failure this feature has.
 * 3. **The evidence statement on realised P&L.** Rendered verbatim from the backend. Real money
 *    makes a result feel like proof in a way a backtest does not, and four trades establish
 *    nothing.
 */

export default function Positions() {
  const holdings = useQuery({ queryKey: ['holdings'], queryFn: () => api.holdings() });
  const watch = useQuery({ queryKey: ['watch'], queryFn: () => api.watch() });
  const alerts = useQuery({ queryKey: ['alerts'], queryFn: () => api.alerts(20) });

  if (holdings.isError)
    return <ErrorState error={holdings.error} onRetry={() => holdings.refetch()} />;
  if (holdings.isLoading) return <Loading rows={6} label="Loading positions" />;

  const rows = holdings.data?.holdings ?? [];
  const realised = holdings.data?.realised;
  const outcomes = new Map((watch.data?.outcomes ?? []).map((o) => [o.holding_id, o]));

  if (rows.length === 0) {
    return (
      <EmptyState
        title="No positions recorded"
        detail={
          'This system does not place orders. Buy through your broker, then record it here so ' +
          'the daily check can watch the strategy’s exit rule against it.'
        }
        command="python -m app buy SQM --qty 50 --price 47.30"
      />
    );
  }

  const open = rows.filter((row) => row.is_open);
  const closed = rows.filter((row) => !row.is_open);
  const unwatchable = (watch.data?.outcomes ?? []).filter((o) => !o.usable);
  const triggered = (watch.data?.outcomes ?? []).filter((o) => o.usable && o.exit_triggered);
  const neverChecked = open.filter((row) => row.last_checked_on === null);

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-slate-100">Positions</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          Purchases you made through your own broker. Nothing here was ordered by this software,
          and nothing here can be.
        </p>
      </div>

      {/* Anything unwatched comes first. Silence and ignorance look identical otherwise. */}
      {unwatchable.length > 0 && (
        <Card>
          <div className="flex items-start gap-2">
            <CircleSlash className="mt-0.5 h-4 w-4 shrink-0 text-loss" />
            <div>
              <h3 className="text-sm font-semibold text-loss">
                {unwatchable.length} position{unwatchable.length > 1 ? 's' : ''} could not be
                checked
              </h3>
              <p className="mt-0.5 text-2xs text-slate-400">
                These are <strong>not</strong> being watched until this is fixed.
              </p>
              <ul className="mt-2 space-y-1.5">
                {unwatchable.map((outcome) => (
                  <li key={outcome.holding_id} className="text-2xs leading-snug text-slate-300">
                    <span className="font-mono font-medium text-slate-100">{outcome.symbol}</span>
                    {' — '}
                    {outcome.problem}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </Card>
      )}

      {neverChecked.length > 0 && (
        <Caveat>
          {neverChecked.length} position{neverChecked.length > 1 ? 's have' : ' has'} never been
          checked. Run <code className="text-slate-300">python -m app watch</code>, or set up the
          scheduled run in <code className="text-slate-300">.github/workflows/</code>.
        </Caveat>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Open" value={String(open.length)} />
        <Stat
          label="Exit rule fired"
          value={String(triggered.length)}
          tone={triggered.length > 0 ? 'caution' : undefined}
        />
        <Stat label="Closed" value={String(closed.length)} />
        <Stat
          label="Realised net"
          value={realised?.net_pnl != null ? realised.net_pnl.toFixed(2) : '—'}
          tone={realised?.net_pnl != null && realised.net_pnl < 0 ? 'loss' : 'gain'}
        />
      </div>

      {watch.data && (
        <Card>
          <p className="text-2xs leading-relaxed text-caution">{watch.data.interpretation}</p>
        </Card>
      )}

      <Card title="Open positions">
        <div className="-m-4 overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className="th">Symbol</th>
                <th className="th">Region</th>
                <th className="th text-right">Qty</th>
                <th className="th text-right">Entry</th>
                <th className="th text-right">Last</th>
                <th className="th text-right">Unrealised</th>
                <th className="th text-right">Stop in force</th>
                <th className="th">Exit rule</th>
                <th className="th">Today</th>
              </tr>
            </thead>
            <tbody>
              {open.map((row) => {
                const outcome = outcomes.get(row.id);
                const ago = outcome?.sessions_since_trigger ?? null;
                return (
                  <tr key={row.id} className="hover:bg-terminal-850">
                    <td className="td whitespace-nowrap font-mono font-medium text-slate-100">
                      {row.symbol}
                    </td>
                    <td className="td whitespace-nowrap text-slate-400">{row.region}</td>
                    <td className="td text-right font-mono tnum text-slate-300">{row.quantity}</td>
                    <td className="td text-right font-mono tnum text-slate-300">
                      {price(row.entry_price, row.currency)}
                    </td>
                    <td className="td text-right font-mono tnum text-slate-300">
                      {price(row.last_price, row.currency)}
                    </td>
                    <td
                      className={`td text-right font-mono tnum ${signClass(row.unrealised_pnl_pct)}`}
                    >
                      {pct(row.unrealised_pnl_pct)}
                    </td>
                    <td className="td text-right font-mono tnum text-slate-400">
                      {outcome?.effective_stop != null
                        ? outcome.effective_stop.toFixed(2)
                        : '—'}
                    </td>
                    <td className="td whitespace-nowrap">
                      {!outcome ? (
                        <span className="text-slate-600">—</span>
                      ) : !outcome.usable ? (
                        <Badge tone="loss">unchecked</Badge>
                      ) : outcome.exit_triggered ? (
                        <span className="text-2xs text-caution">
                          {outcome.exit_reason}
                          <br />
                          <span className="text-slate-500">
                            {ago === 0
                              ? 'today'
                              : `${outcome.exit_triggered_on} (${ago} back)`}
                          </span>
                        </span>
                      ) : (
                        <Badge tone="gain">within the rules</Badge>
                      )}
                    </td>
                    <td className="td whitespace-nowrap text-2xs text-slate-400">
                      {outcome?.decision_action ?? '—'}
                    </td>
                  </tr>
                );
              })}
              {open.length === 0 && (
                <tr>
                  <td className="td text-center text-slate-500" colSpan={9}>
                    No open positions.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {/*
        The reasons behind a trigger, spelled out. A signal you cannot interrogate is one you
        either obey blindly or ignore, and neither is what this is for.
      */}
      {triggered.length > 0 && (
        <Card title="Why each rule fired">
          <ul className="space-y-3">
            {triggered.map((outcome) => (
              <li key={outcome.holding_id} className="border-l-2 border-caution/40 pl-3">
                <p className="text-xs font-medium text-slate-100">
                  <span className="font-mono">{outcome.symbol}</span>
                  <span className="ml-2 font-normal text-slate-400">
                    {outcome.exit_reason} on {outcome.exit_triggered_on}
                    {(outcome.sessions_since_trigger ?? 0) > 0 &&
                      ` — ${outcome.sessions_since_trigger} sessions ago, so a backtest considers this position already exited`}
                  </span>
                </p>
                <ul className="mt-1 space-y-0.5">
                  {outcome.decision_reasons.map((reason, index) => (
                    <li key={index} className="text-2xs text-slate-400">
                      {reason}
                    </li>
                  ))}
                </ul>
                {outcome.liquidity_caveat && (
                  <p className="mt-1 text-2xs leading-snug text-caution">
                    {outcome.liquidity_caveat}
                  </p>
                )}
                <p className="mt-1 text-2xs text-slate-600">
                  Record a sale with{' '}
                  <code className="text-slate-400">
                    python -m app sell {outcome.holding_id} --price &lt;what you got&gt;
                  </code>
                </p>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {closed.length > 0 && (
        <Card title="Closed positions">
          <div className="-m-4 overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  <th className="th">Symbol</th>
                  <th className="th">Held</th>
                  <th className="th text-right">Entry</th>
                  <th className="th text-right">Exit</th>
                  <th className="th text-right">Net</th>
                  <th className="th text-right">%</th>
                  <th className="th text-right">Days</th>
                </tr>
              </thead>
              <tbody>
                {closed.map((row) => (
                  <tr key={row.id} className="hover:bg-terminal-850">
                    <td className="td whitespace-nowrap font-mono font-medium text-slate-100">
                      {row.symbol}
                    </td>
                    <td className="td whitespace-nowrap font-mono text-2xs text-slate-500">
                      {date(row.opened_on)} → {date(row.closed_on)}
                    </td>
                    <td className="td text-right font-mono tnum text-slate-300">
                      {price(row.entry_price, row.currency)}
                    </td>
                    <td className="td text-right font-mono tnum text-slate-300">
                      {price(row.exit_price, row.currency)}
                    </td>
                    <td className={`td text-right font-mono tnum ${signClass(row.realised_pnl)}`}>
                      {row.realised_pnl != null ? row.realised_pnl.toFixed(2) : '—'}
                    </td>
                    <td
                      className={`td text-right font-mono tnum ${signClass(row.realised_pnl_pct)}`}
                    >
                      {pct(row.realised_pnl_pct)}
                    </td>
                    <td className="td text-right font-mono tnum text-slate-400">
                      {row.holding_period_days ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {realised && (
            <p className="mt-3 text-2xs leading-relaxed text-caution">{realised.evidence}</p>
          )}
        </Card>
      )}

      {/*
        Delivery, separate from the trigger. They fail independently, and a system whose only
        output is a message has to be able to say whether the message arrived.
      */}
      {alerts.data && alerts.data.alerts.length > 0 && (
        <Card title="Alerts">
          {alerts.data.n_failed > 0 && (
            <div className="mb-3 flex items-center gap-1.5 text-2xs text-loss">
              <MailWarning className="h-3.5 w-3.5" />
              {alerts.data.n_failed} alert{alerts.data.n_failed > 1 ? 's were' : ' was'} not
              delivered.
            </div>
          )}
          <ul className="space-y-1.5">
            {alerts.data.alerts.map((alert) => (
              <li key={alert.id} className="flex items-start gap-2 text-2xs">
                {alert.status === 'SENT' ? (
                  <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0 text-gain" />
                ) : (
                  <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-loss" />
                )}
                <span className="font-mono text-slate-600">{date(alert.created_at)}</span>
                <span className="text-slate-300">{alert.subject}</span>
                <span className="text-slate-600">via {alert.channel}</span>
                {alert.failure_reason && (
                  <span className="text-loss">{alert.failure_reason}</span>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}

      {holdings.data && (
        <Card title="What these numbers are">
          <ul className="space-y-1.5">
            {holdings.data.caveats.map((caveat, index) => (
              <li key={index} className="text-2xs leading-relaxed text-slate-400">
                {caveat}
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
