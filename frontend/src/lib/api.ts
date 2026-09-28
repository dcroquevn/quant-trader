/**
 * Typed client for the quant-trader API.
 *
 * All requests go to same-origin `/api/*`; Vite proxies them to FastAPI in dev.
 * That keeps the backend URL out of the frontend entirely.
 *
 * Errors are surfaced, never swallowed into an empty state. A chart that renders
 * blank because a request failed is indistinguishable from one that renders blank
 * because there is genuinely no data, and those need different reactions.
 */

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function get<T>(
  path: string,
  params?: Record<string, string | number | undefined>,
): Promise<T> {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== '') url.searchParams.set(key, String(value));
    }
  }

  let response: Response;
  try {
    response = await fetch(url.toString(), { headers: { Accept: 'application/json' } });
  } catch (cause) {
    throw new ApiError(
      'Cannot reach the backend. Start it with: python -m app serve',
      0,
      String(cause),
    );
  }

  if (!response.ok) {
    let detail: string | undefined;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body?.detail;
    } catch {
      detail = undefined;
    }
    throw new ApiError(detail ?? `Request failed (${response.status})`, response.status, detail);
  }
  return response.json() as Promise<T>;
}

/* ------------------------------------------------------------------ */
/* Types -- mirror backend/app/api/main.py                            */
/* ------------------------------------------------------------------ */

export interface Health {
  status: string;
  version: string;
  phase: string;
  paper_trading: boolean;
  live_trading: boolean;
  live_trading_implemented: boolean;
}

export interface BenchmarkInfo {
  symbol: string;
  kind: string;
  currency: string;
  available: boolean;
  caveats: string[];
}

export interface Market {
  code: string;
  name: string;
  flag: string;
  currency: string;
  exchange: string;
  timezone: string;
  trading_hours: string;
  benchmark: BenchmarkInfo | null;
}

export interface Asset {
  symbol: string;
  name: string;
  market: string;
  sector: string;
  currency: string;
  asset_class: string;
  provider_symbol: string;
  notes: string;
}

export interface UniverseResponse {
  verification_date: string;
  count: number;
  assets: Asset[];
}

export interface CoverageRow {
  symbol: string;
  market: string;
  provider_symbol: string;
  bars: number;
  first_bar: string | null;
  last_bar: string | null;
}

export interface CoverageResponse {
  timeframe: string;
  total_bars: number;
  assets_with_data: number;
  assets_without_data: number;
  rows: CoverageRow[];
}

export interface Bar {
  ts: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
}

export interface BarsResponse {
  symbol: string;
  market: string;
  currency: string;
  timeframe: string;
  bars_available: number;
  bars_returned: number;
  adjusted: boolean;
  source: string;
  bars: Bar[];
}

export type FeatureMap = Record<string, number | boolean | null>;

export type FeatureHistoryRow = Record<string, number | boolean | string | null>;

export interface FeaturesResponse {
  symbol: string;
  market: string;
  currency: string;
  timeframe: string;
  as_of: string;
  bars_available: number;
  /** Trailing fabricated bars removed before computing. as_of reflects the trim. */
  carried_forward_dropped: number;
  complete: boolean;
  note: string;
  features: FeatureMap;
  disclaimer: string;
  history?: FeatureHistoryRow[];
}

export interface AuditResponse {
  symbol: string;
  market: string;
  timeframe: string;
  stored_bars: number;
  first_bar: string | null;
  last_bar: string | null;
  missing_weekdays: string[];
  longest_gap_sessions: number;
  duplicate_timestamps: string[];
  is_stale: boolean;
  stale_by_days: number;
  /** Trailing bars the vendor carried forward: flat OHLC with zero volume. */
  stale_quote_run: number;
  /** Count at or above which the backend considers the tail worth flagging. */
  stale_quote_run_threshold: number;
  summary: string;
  caveat: string;
}

export interface Limitation {
  id: string;
  severity: 'high' | 'medium' | 'low';
  title: string;
  detail: string;
}

export interface LimitationsResponse {
  limitations: Limitation[];
  instrument_notes: Array<{ symbol: string; market: string; note: string }>;
}

export interface ProviderRow {
  provider: string;
  markets: string;
  timeframes: string;
  api_key_required: string;
  adjusted_prices: string;
  cost: string;
  notes: string;
}

/* ------------------------------------------------------------------ */
/* Endpoints                                                          */
/* ------------------------------------------------------------------ */

export const api = {
  health: () => get<Health>('/api/health'),
  markets: () => get<Market[]>('/api/markets'),
  providers: () => get<{ providers: ProviderRow[]; all_free: boolean }>('/api/providers'),
  universe: (market?: string) => get<UniverseResponse>('/api/universe', { market }),
  coverage: (timeframe = '1D') => get<CoverageResponse>('/api/coverage', { timeframe }),
  limitations: () => get<LimitationsResponse>('/api/limitations'),
  bars: (symbol: string, market?: string, limit = 500) =>
    get<BarsResponse>(`/api/assets/${encodeURIComponent(symbol)}/bars`, { market, limit }),
  features: (symbol: string, market?: string, history = 0) =>
    get<FeaturesResponse>(`/api/assets/${encodeURIComponent(symbol)}/features`, {
      market,
      history,
    }),
  audit: (symbol: string, market?: string) =>
    get<AuditResponse>(`/api/assets/${encodeURIComponent(symbol)}/audit`, { market }),
};
