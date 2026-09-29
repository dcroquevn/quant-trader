import { useQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { Lock, Play } from 'lucide-react';
import { useMemo, useState } from 'react';

import {
  DistributionChart,
  ProportionBar,
  SignedBarChart,
  TimeSeriesChart,
} from '../components/charts';
import { Badge, Card, Caveat, ErrorState, Loading, Stat } from '../components/ui';
import { api, type BacktestResponse, type TradeRow } from '../lib/api';
import { DASH, compact, date, num, pct, signClass } from '../lib/format';

/**
 * Backtest page.
 *
 * The design problem here is that a backtest chart is persuasive regardless of whether
 * it means anything. Three counterweights:
 *
 * **The partition is stated above the numbers, not below them.** A TRAIN curve and a
 * TEST curve look identical; only the label distinguishes a measurement from a
 * rehearsal. The TEST split cannot be selected at all — the API refuses it, and
 * offering a button that returns 409 would be worse than not offering one.
 *
 * **Costs appear next to the returns they were deducted from**, so nobody has to go
 * looking for whether friction was modelled.
 *
 * **Limitations are a section, not a footnote.**
 */

const MARKETS = [
  { value: 'USA', label: 'USA' },
  { value: 'CHILE', label: 'Chile' },
];

export default function Backtest() {
  const [market, setMarket] = useState('USA');
  const [strategy, setStrategy] = useState('trend_momentum');
  const [split, setSplit] = useState('train');
  const [capital, setCapital] = useState<string>('');
  const [symbols, setSymbols] = useState('');
  const [submitted, setSubmitted] = useState<Record<string, string | number | undefined> | null>(
    null,
  );

  const strategies = useQuery({ queryKey: ['strategies'], queryFn: api.strategies });
  const splits = useQuery({ queryKey: ['splits'], queryFn: api.splits });

  const run = useQuery({
    queryKey: ['backtest', submitted],
    queryFn: () =>
      api.backtest({
        market: submitted!.market as string,
        strategy: submitted!.strategy as string,
        split: submitted!.split as string,
        symbols: (submitted!.symbols as string) || undefined,
        capital: submitted!.capital ? Number(submitted!.capital) : undefined,
      }),
    enabled: submitted !== null,
    // A backtest is minutes of CPU over thousands of bars; never refetch it silently.
    staleTime: Infinity,
    retry: false,
  });

  const selectedSplit = splits.data?.splits.find((s) => s.split === split);

  return (
    <div className="space-y-4">
      <header>
        <h2 className="text-lg font-semibold text-slate-100">Backtest</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          Simulated over stored history with transaction costs, slippage and stops.
        </p>
      </header>

      {/* Controls in one row above the results. */}
      <div className="card flex flex-wrap items-end gap-x-5 gap-y-3 px-4 py-3">
        <Field label="Market">
          <select
            value={market}
            onChange={(e) => setMarket(e.target.value)}
            className="control"
          >
            {MARKETS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Strategy">
          <select
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
            className="control"
          >
            {(strategies.data?.strategies ?? [{ name: 'trend_momentum' }]).map((s) => (
              <option key={s.name} value={s.name}>
                {s.name}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Data partition">
          <div className="inline-flex rounded border border-terminal-700 p-0.5">
            {(splits.data?.splits ?? []).map((s) => {
              const locked = !s.readable_via_api;
              return (
                <button
                  key={s.split}
                  type="button"
                  disabled={locked}
                  onClick={() => setSplit(s.split)}
                  title={locked ? s.reason : s.note}
                  aria-pressed={split === s.split}
                  className={clsx(
                    'inline-flex items-center gap-1 rounded px-2.5 py-1 text-2xs font-medium transition',
                    locked && 'cursor-not-allowed text-slate-600',
                    !locked && split === s.split && 'bg-terminal-700 text-slate-100',
                    !locked && split !== s.split && 'text-slate-400 hover:text-slate-200',
                  )}
                >
                  {locked && <Lock className="h-2.5 w-2.5" />}
                  {s.split}
                </button>
              );
            })}
            <button
              type="button"
              onClick={() => setSplit('full')}
              aria-pressed={split === 'full'}
              className={clsx(
                'rounded px-2.5 py-1 text-2xs font-medium transition',
                split === 'full'
                  ? 'bg-terminal-700 text-slate-100'
                  : 'text-slate-400 hover:text-slate-200',
              )}
            >
              full
            </button>
          </div>
        </Field>

        <Field label="Initial capital">
          <input
            value={capital}
            onChange={(e) => setCapital(e.target.value.replace(/[^0-9]/g, ''))}
            placeholder="default"
            inputMode="numeric"
            className="control w-28"
          />
        </Field>

        <Field label="Symbols (optional)">
          <input
            value={symbols}
            onChange={(e) => setSymbols(e.target.value.toUpperCase())}
            placeholder="whole universe"
            className="control w-44"
          />
        </Field>

        <button
          type="button"
          onClick={() =>
            setSubmitted({ market, strategy, split, capital, symbols, at: Date.now() })
          }
          disabled={run.isFetching}
          className="inline-flex items-center gap-1.5 rounded bg-accent/15 px-3 py-1.5 text-xs font-medium text-accent ring-1 ring-inset ring-accent/30 transition hover:bg-accent/25 disabled:opacity-50"
        >
          <Play className="h-3.5 w-3.5" />
          {run.isFetching ? 'Running' : 'Run backtest'}
        </button>
      </div>

      {selectedSplit && (
        <Caveat tone={split === 'full' ? 'caution' : 'neutral'}>{selectedSplit.note}</Caveat>
      )}
      {split === 'full' && (
        <Caveat>
          Full history holds nothing back, so nothing produced from it is out-of-sample.
          Convenient for inspection, not for judging whether a strategy works.
        </Caveat>
      )}
      {splits.data?.splits.some((s) => !s.readable_via_api) && (
        <p className="text-2xs text-slate-600">
          The <span className="font-mono">test</span> partition is locked here on purpose. It is
          meant to be read once, after parameters are frozen, so finalising it is a CLI-only
          action: <span className="font-mono text-slate-500">python -m app backtest --split test --finalising</span>
        </p>
      )}

      {submitted === null && (
        <div className="card py-16 text-center">
          <p className="text-sm text-slate-400">Choose a configuration and run a backtest.</p>
          <p className="mx-auto mt-2 max-w-md text-2xs leading-relaxed text-slate-600">
            Nothing is precomputed. Results depend entirely on the cost assumptions in your
            <span className="font-mono"> .env</span>, which are placeholders until you replace them
            with your broker&rsquo;s actual schedule.
          </p>
        </div>
      )}

      {run.isFetching && <Loading rows={6} label="Simulating bar by bar" />}
      {run.isError && <ErrorState error={run.error} onRetry={() => run.refetch()} />}
      {run.data && !run.isFetching && <Results result={run.data} />}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function Results({ result }: { result: BacktestResponse }) {
  const m = result.metrics;
  const benchmark = result.benchmark;
  const comparison = m.vs_benchmark;

  const equity = useMemo(
    () =>
      (result.equity_curve ?? []).map((p) => ({
        ts: p.ts.slice(0, 10),
        equity: p.equity,
      })),
    [result.equity_curve],
  );

  const drawdown = useMemo(
    () =>
      (result.drawdown_curve ?? []).map((p) => ({
        ts: p.ts.slice(0, 10),
        drawdown: p.drawdown_pct,
      })),
    [result.drawdown_curve],
  );

  const trades = result.trades ?? [];

  return (
    <div className="space-y-4">
      {/* Partition and cost, above every number they qualify. */}
      <div className="card px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={result.split === 'full' ? 'caution' : 'accent'}>
            split: {result.split}
          </Badge>
          <Badge tone="neutral">
            {date(result.start_date)} &rarr; {date(result.end_date)}
          </Badge>
          <Badge tone="neutral">{result.universe.length} instruments</Badge>
          <Badge tone="caution" title="Total assumed friction for a full entry and exit">
            round trip {result.cost_model.round_trip_pct}%
          </Badge>
          <Badge tone="neutral">{result.strategy.name}</Badge>
        </div>
        <p className="mt-2 text-2xs leading-relaxed text-slate-500">{result.split_note}</p>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Stat
          label="Total return"
          value={pct(m.total_return_pct)}
          tone={toneOf(m.total_return_pct)}
          hint={
            benchmark.available
              ? `benchmark ${pct(benchmark.total_return_pct)}`
              : 'no benchmark available'
          }
        />
        <Stat
          label="CAGR"
          value={pct(m.cagr_pct)}
          tone={toneOf(m.cagr_pct)}
          hint={m.cagr_note ? 'sample too short to annualise' : undefined}
        />
        <Stat
          label="Sharpe"
          value={num(m.sharpe, 2)}
          hint={`risk-free ${pct(m.risk_free_rate_used * 100, 1)} assumed`}
        />
        <Stat
          label="Max drawdown"
          value={pct(m.max_drawdown_pct)}
          tone="loss"
          hint={m.recovery_date ? `recovered ${date(m.recovery_date)}` : 'never recovered in sample'}
        />
        <Stat label="Trades" value={String(m.n_trades)} hint={`${num(m.exposure_pct, 0)}% exposure`} />
        <Stat
          label="Turnover"
          value={pct(m.turnover_pct, 0)}
          tone={(m.turnover_pct ?? 0) > 500 ? 'caution' : 'neutral'}
          hint="annualised; high turnover makes cost assumptions dominate"
        />
      </div>

      {(m.turnover_pct ?? 0) > 500 && (
        <Caveat>
          <strong>Turnover is {pct(m.turnover_pct, 0)} a year.</strong> At that rate the result
          is governed by the cost assumption ({result.cost_model.round_trip_pct}% round trip)
          more than by the strategy. Replace the placeholders in <span className="font-mono">.env</span>{' '}
          with your broker&rsquo;s real schedule before drawing a conclusion.
        </Caveat>
      )}

      <TimeSeriesChart
        title="Equity curve"
        note={
          <>
            Starting capital {compact(result.initial_capital)}. The benchmark is compared on
            summary metrics below rather than overlaid here &mdash; its own curve is not returned
            by the API, and drawing a line the data does not contain would be worse than
            omitting it.
          </>
        }
        data={equity}
        series={[{ key: 'equity', label: 'Strategy equity', ink: true }]}
        height={280}
        digits={0}
        fillFirst
      />

      <TimeSeriesChart
        title="Drawdown"
        note="Peak-to-trough decline of the equity curve, including unrealised losses"
        data={drawdown}
        series={[{ key: 'drawdown', label: 'Drawdown', colour: '#ff5c7c' }]}
        height={180}
        suffix="%"
        zeroLine
        fillFirst
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <SignedBarChart
          title="Monthly returns"
          note="Month-end to month-end"
          data={(result.monthly_returns ?? []).map((r) => ({
            period: r.period.slice(0, 7),
            return_pct: r.return_pct,
          }))}
          xKey="period"
          valueKey="return_pct"
        />
        <SignedBarChart
          title="Annual returns"
          note="First and last years are partial and are not annualised"
          data={(result.annual_returns ?? []).map((r) => ({
            period: r.period,
            return_pct: r.return_pct,
          }))}
          xKey="period"
          valueKey="return_pct"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <DistributionChart
          title="Trade outcome distribution"
          note={`Net P&L per trade, after commission and slippage (${trades.length} trades)`}
          values={trades.map((t) => t.pnl_pct)}
        />

        <Card title="Trade statistics" subtitle="All figures net of costs">
          <div className="space-y-4">
            <ProportionBar
              label="Win rate"
              positive={m.n_wins}
              negative={m.n_losses}
            />
            <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5">
              {[
                ['Profit factor', num(m.profit_factor, 2)],
                ['Expectancy / trade', num(m.expectancy, 2)],
                ['Average win', num(m.average_win, 2)],
                ['Average loss', num(m.average_loss, 2)],
                ['Best trade', num(m.best_trade, 2)],
                ['Worst trade', num(m.worst_trade, 2)],
                ['Avg holding', `${num(m.average_holding_days, 1)} days`],
                ['Sortino', num(m.sortino, 2)],
                ['Calmar', num(m.calmar, 2)],
                ['Volatility', pct(m.annualised_volatility_pct)],
              ].map(([label, value]) => (
                <div key={label} className="flex items-baseline justify-between gap-2 border-b border-terminal-850 py-1">
                  <dt className="text-2xs text-slate-500">{label}</dt>
                  <dd className="font-mono text-xs tnum text-slate-200">{value}</dd>
                </div>
              ))}
            </dl>
            {m.profit_factor === null && m.n_trades > 0 && (
              <p className="text-2xs text-slate-600">
                Profit factor is undefined: this sample has no losing trades, so there is
                nothing to divide by.
              </p>
            )}
          </div>
        </Card>
      </div>

      {comparison?.available ? (
        <Card
          title="Versus benchmark"
          subtitle={benchmark.label ?? 'buy and hold'}
        >
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-3 lg:grid-cols-5">
            {[
              ['Excess return', comparison.excess_total_return_pct, '%'],
              ['Excess CAGR', comparison.excess_cagr_pct, '%'],
              ['Sharpe diff.', comparison.sharpe_difference, ''],
              ['Sortino diff.', comparison.sortino_difference, ''],
              ['Drawdown diff.', comparison.drawdown_difference_pct, '%'],
            ].map(([label, value, suffix]) => (
              <div key={label as string}>
                <div className="label">{label as string}</div>
                <div
                  className={clsx(
                    'mt-0.5 font-mono text-sm tnum',
                    signClass(value as number | null),
                  )}
                >
                  {value === null || value === undefined
                    ? DASH
                    : `${(value as number) > 0 ? '+' : ''}${num(value as number, 2)}${suffix}`}
                </div>
              </div>
            ))}
          </div>
          <p className="mt-3 text-2xs text-slate-600">
            A positive drawdown difference means the strategy drew down less than the
            benchmark.
          </p>
          {benchmark.caveats && benchmark.caveats.length > 0 && (
            <div className="mt-3 space-y-2">
              {benchmark.caveats.map((c) => (
                <Caveat key={c}>{c}</Caveat>
              ))}
            </div>
          )}
        </Card>
      ) : (
        <Caveat>
          <strong>No benchmark comparison.</strong> {benchmark.reason ?? 'unavailable'}
        </Caveat>
      )}

      {Object.keys(result.rejected_entries).length > 0 && (
        <Card
          title="Entries not taken"
          subtitle="A strategy constantly blocked by its own limits is telling you something the metrics will not"
        >
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-3">
            {Object.entries(result.rejected_entries)
              .sort((a, b) => b[1] - a[1])
              .map(([reason, count]) => (
                <div
                  key={reason}
                  className="flex items-baseline justify-between gap-2 border-b border-terminal-850 py-1"
                >
                  <span className="truncate text-2xs text-slate-500" title={reason}>
                    {reason.replace(/_/g, ' ')}
                  </span>
                  <span className="font-mono text-xs tnum text-slate-300">{count}</span>
                </div>
              ))}
          </div>
        </Card>
      )}

      <TradesTable trades={trades} />

      <Card title="Limitations" subtitle="Properties of the method, not bugs awaiting a fix">
        <ul className="space-y-2">
          {result.limitations.map((item) => (
            <li key={item} className="flex gap-2 text-2xs leading-relaxed text-slate-400">
              <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-caution/60" aria-hidden />
              {item}
            </li>
          ))}
        </ul>
      </Card>

      <p className="text-2xs leading-relaxed text-slate-600">{result.disclaimer}</p>
    </div>
  );
}

/* ------------------------------------------------------------------ */

function TradesTable({ trades }: { trades: TradeRow[] }) {
  const [showAll, setShowAll] = useState(false);
  if (trades.length === 0) {
    return (
      <Card title="Trades">
        <p className="py-6 text-center text-sm text-slate-500">
          No trades were taken in this window.
        </p>
      </Card>
    );
  }

  const shown = showAll ? trades : trades.slice(-25);

  return (
    <Card
      title={`Trades (${trades.length})`}
      subtitle="Net P&L after commission and slippage"
      actions={
        trades.length > 25 && (
          <button
            type="button"
            onClick={() => setShowAll((v) => !v)}
            className="rounded border border-terminal-700 px-2 py-1 text-2xs text-slate-400 transition hover:bg-terminal-800 hover:text-slate-200"
          >
            {showAll ? 'Show last 25' : `Show all ${trades.length}`}
          </button>
        )
      }
    >
      <div className="-m-4 max-h-[440px] overflow-auto">
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className="th">Symbol</th>
              <th className="th">Entry</th>
              <th className="th text-right">Price</th>
              <th className="th">Exit</th>
              <th className="th text-right">Price</th>
              <th className="th text-right">Qty</th>
              <th className="th text-right">Net P&amp;L</th>
              <th className="th text-right">%</th>
              <th className="th text-right">Days</th>
              <th className="th text-right">MAE %</th>
              <th className="th">Exit reason</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((t, i) => (
              <tr key={`${t.symbol}-${t.entry_date}-${i}`} className="hover:bg-terminal-850">
                <td className="td whitespace-nowrap font-mono text-slate-200">{t.symbol}</td>
                <td className="td whitespace-nowrap font-mono text-2xs text-slate-500">
                  {date(t.entry_date)}
                </td>
                <td className="td text-right font-mono tnum text-slate-300">
                  {num(t.entry_price, 2)}
                </td>
                <td className="td whitespace-nowrap font-mono text-2xs text-slate-500">
                  {date(t.exit_date)}
                </td>
                <td className="td text-right font-mono tnum text-slate-300">
                  {num(t.exit_price, 2)}
                </td>
                <td className="td text-right font-mono tnum text-slate-400">
                  {num(t.quantity, 0)}
                </td>
                <td className={clsx('td text-right font-mono tnum', signClass(t.pnl))}>
                  {num(t.pnl, 2)}
                </td>
                <td className={clsx('td text-right font-mono tnum', signClass(t.pnl_pct))}>
                  {pct(t.pnl_pct, 1)}
                </td>
                <td className="td text-right font-mono tnum text-slate-400">
                  {t.holding_period_days}
                </td>
                <td
                  className="td text-right font-mono tnum text-slate-500"
                  title="Maximum adverse excursion: the worst unrealised loss while the position was held"
                >
                  {pct(t.max_adverse_excursion_pct, 1)}
                </td>
                <td className="td max-w-[14rem]">
                  <span className="line-clamp-1 text-2xs text-slate-500" title={t.exit_reason}>
                    {t.exit_reason}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

function toneOf(value: number | null | undefined): 'gain' | 'loss' | 'neutral' {
  if (value === null || value === undefined) return 'neutral';
  return value > 0 ? 'gain' : value < 0 ? 'loss' : 'neutral';
}
