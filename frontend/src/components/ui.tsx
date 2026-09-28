/**
 * Shared presentational primitives.
 *
 * Two of these exist to enforce honesty rather than to save typing:
 *
 * `ErrorState` always shows the real reason and the command that fixes it, because
 * a research tool that fails silently teaches the user to distrust every blank
 * panel.
 *
 * `Stat` renders a missing value as an em dash, never as zero. In a quant tool
 * "0.00" and "not computed yet" lead to opposite decisions.
 */

import clsx from 'clsx';
import { AlertTriangle, Info, RefreshCw, ServerCrash } from 'lucide-react';
import type { ReactNode } from 'react';

import { ApiError } from '../lib/api';
import { DASH } from '../lib/format';

/* ---------------------------------------------------------------- */

export function Card({
  title,
  subtitle,
  actions,
  children,
  className,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={clsx('card', className)}>
      {(title || actions) && (
        <header className="card-header">
          <div className="min-w-0">
            {title && <h2 className="truncate text-sm font-semibold text-slate-200">{title}</h2>}
            {subtitle && <p className="mt-0.5 truncate text-2xs text-slate-500">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = 'neutral',
  mono = true,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  tone?: 'neutral' | 'gain' | 'loss' | 'caution';
  mono?: boolean;
}) {
  const toneClass = {
    neutral: 'text-slate-100',
    gain: 'text-gain',
    loss: 'text-loss',
    caution: 'text-caution',
  }[tone];

  return (
    <div className="card p-4" title={hint}>
      <div className="label">{label}</div>
      <div className={clsx('mt-1.5 text-xl font-semibold tnum', toneClass, mono && 'font-mono')}>
        {value ?? DASH}
      </div>
      {hint && <div className="mt-1 text-2xs leading-snug text-slate-500">{hint}</div>}
    </div>
  );
}

export function Badge({
  children,
  tone = 'neutral',
  title,
}: {
  children: ReactNode;
  tone?: 'neutral' | 'gain' | 'loss' | 'caution' | 'accent';
  title?: string;
}) {
  const tones = {
    neutral: 'bg-terminal-800 text-slate-300 ring-terminal-700',
    gain: 'bg-gain/10 text-gain ring-gain/30',
    loss: 'bg-loss/10 text-loss ring-loss/30',
    caution: 'bg-caution/10 text-caution ring-caution/30',
    accent: 'bg-accent/10 text-accent ring-accent/30',
  };
  return (
    <span
      title={title}
      className={clsx(
        'inline-flex items-center rounded px-1.5 py-0.5 text-2xs font-medium ring-1 ring-inset',
        tones[tone],
      )}
    >
      {children}
    </span>
  );
}

/* ---------------------------------------------------------------- */

export function Loading({ rows = 3, label }: { rows?: number; label?: string }) {
  return (
    <div className="space-y-2" role="status" aria-live="polite">
      {label && (
        <div className="flex items-center gap-2 text-2xs text-slate-500">
          <RefreshCw className="h-3 w-3 animate-spin" />
          {label}
        </div>
      )}
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton h-8 w-full" />
      ))}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const isApi = error instanceof ApiError;
  const offline = isApi && error.status === 0;
  const message = error instanceof Error ? error.message : String(error);

  return (
    <div className="card border-loss/30 bg-loss/5 p-5">
      <div className="flex items-start gap-3">
        <ServerCrash className="mt-0.5 h-5 w-5 shrink-0 text-loss" />
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-loss">
            {offline ? 'Backend unreachable' : 'Request failed'}
          </h3>
          <p className="mt-1 break-words text-sm text-slate-300">{message}</p>
          {offline && (
            <pre className="mt-3 overflow-x-auto rounded bg-terminal-950 p-3 text-2xs text-slate-400">
              cd quant-trader{'\n'}
              .venv\Scripts\activate{'\n'}
              python -m app serve
            </pre>
          )}
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="mt-3 inline-flex items-center gap-1.5 rounded border border-terminal-600 px-2.5 py-1 text-2xs font-medium text-slate-300 transition hover:bg-terminal-800"
            >
              <RefreshCw className="h-3 w-3" />
              Retry
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

export function EmptyState({ title, detail, command }: { title: string; detail: string; command?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
      <Info className="h-6 w-6 text-slate-600" />
      <h3 className="text-sm font-medium text-slate-300">{title}</h3>
      <p className="max-w-md text-2xs leading-relaxed text-slate-500">{detail}</p>
      {command && (
        <code className="mt-1 rounded bg-terminal-950 px-2 py-1 text-2xs text-accent">{command}</code>
      )}
    </div>
  );
}

/**
 * Inline caveat.
 *
 * Used wherever a number is shown that a reader could reasonably over-trust:
 * a benchmark that is not the benchmark, a feature set that has not warmed up,
 * a price series the vendor carried forward.
 */
export function Caveat({ children, tone = 'caution' }: { children: ReactNode; tone?: 'caution' | 'neutral' }) {
  return (
    <div
      className={clsx(
        'flex items-start gap-2 rounded border px-3 py-2 text-2xs leading-relaxed',
        tone === 'caution'
          ? 'border-caution/25 bg-caution/5 text-caution'
          : 'border-terminal-700 bg-terminal-850 text-slate-400',
      )}
    >
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div className="min-w-0">{children}</div>
    </div>
  );
}
