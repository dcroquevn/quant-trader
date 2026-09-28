/**
 * Display formatting.
 *
 * The rule throughout: a missing value renders as an em dash, never as zero.
 * "0.00%" and "unknown" are entirely different claims, and conflating them in a
 * research tool is how someone ends up acting on an indicator that has not warmed
 * up yet.
 */

const DASH = '—';

export function num(
  value: number | null | undefined,
  digits = 2,
  options: Intl.NumberFormatOptions = {},
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return value.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    ...options,
  });
}

/** Price, at the currency's conventional precision. */
export function price(value: number | null | undefined, currency: string): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  // CLP quotes run into the tens of thousands and trade in whole pesos; two
  // decimals would imply precision the market does not offer.
  const digits = currency === 'CLP' ? 0 : 2;
  return value.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  const sign = value > 0 ? '+' : '';
  return `${sign}${num(value, digits)}%`;
}

export function compact(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return value.toLocaleString('en-US', { notation: 'compact', maximumFractionDigits: 1 });
}

export function date(value: string | null | undefined): string {
  if (!value) return DASH;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return DASH;
  return parsed.toISOString().slice(0, 10);
}

/** Tailwind colour class for a signed number. */
export function signClass(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return 'text-slate-500';
  if (value > 0) return 'text-gain';
  if (value < 0) return 'text-loss';
  return 'text-slate-400';
}

export function marketFlag(code: string): string {
  if (code === 'USA') return '\u{1F1FA}\u{1F1F8}';
  if (code === 'CHILE') return '\u{1F1E8}\u{1F1F1}';
  return '';
}

export { DASH };
