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
  /** Share of recent bars reporting zero volume, in percent. */
  recent_zero_volume_pct: number;
  /** True when volume-derived features are unevaluable on recent bars. */
  volume_feed_degraded: boolean;
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
/* Phase 2: strategies, scanner, backtest                             */
/* ------------------------------------------------------------------ */

export interface StrategyInfo {
  name: string;
  version: string;
  description: string;
  default_params: Record<string, number | string | boolean>;
}

export interface ScanRow {
  symbol: string;
  market: string;
  currency: string;
  as_of: string;
  price: number | null;
  action: 'BUY' | 'SELL' | 'HOLD';
  score: number;
  /** Safe phrasing supplied by the backend. Use it verbatim; never invent wording. */
  score_description: string;
  trend_score: number | null;
  momentum_score: number | null;
  rsi: number | null;
  macd_hist: number | null;
  relative_volume: number | null;
  atr_pct: number | null;
  dist_52w_high_pct: number | null;
  return_20d: number | null;
  dollar_volume: number | null;
  stop_price: number | null;
  take_profit_price: number | null;
  risk_reward: number | null;
  bars_available: number;
  carried_forward_dropped: number;
  stale: boolean;
  volume_feed_degraded: boolean;
  recent_zero_volume_pct: number;
  tradable: boolean;
  blocked_reason: string;
  reasons: string[];
}

export interface ScanResponse {
  strategy: { name: string; version: string; description: string };
  scanned_at: string;
  disclaimer: string;
  n_total: number;
  n_returned: number;
  n_errors: number;
  errors: Array<{ symbol: string; market: string; error: string }>;
  rows: ScanRow[];
}

export interface TradeRow {
  symbol: string;
  market: string;
  currency: string;
  entry_date: string;
  entry_price: number;
  exit_date: string;
  exit_price: number;
  quantity: number;
  gross_pnl: number;
  commission: number;
  slippage_cost: number;
  pnl: number;
  pnl_pct: number;
  holding_period_days: number;
  bars_held: number;
  entry_reason: string;
  exit_reason: string;
  max_adverse_excursion_pct: number;
  max_favorable_excursion_pct: number;
}

export interface BacktestMetrics {
  initial_equity: number;
  final_equity: number;
  total_return_pct: number | null;
  cagr_pct: number | null;
  cagr_note?: string;
  annualised_volatility_pct: number | null;
  sharpe: number | null;
  sortino: number | null;
  calmar: number | null;
  max_drawdown_pct: number | null;
  peak_date: string | null;
  trough_date: string | null;
  recovery_date: string | null;
  drawdown_days: number | null;
  exposure_pct: number | null;
  turnover_pct: number | null;
  n_trades: number;
  n_wins: number;
  n_losses: number;
  win_rate_pct: number | null;
  profit_factor: number | null;
  average_win: number | null;
  average_loss: number | null;
  expectancy: number | null;
  best_trade: number | null;
  worst_trade: number | null;
  average_holding_days: number | null;
  average_bars_held: number | null;
  risk_free_rate_used: number;
  n_bars: number;
  sample_days: number;
  vs_benchmark?: {
    available: boolean;
    reason?: string;
    excess_total_return_pct: number | null;
    excess_cagr_pct: number | null;
    sharpe_difference: number | null;
    sortino_difference: number | null;
    drawdown_difference_pct: number | null;
    volatility_difference_pct: number | null;
  };
}

export interface BenchmarkMetrics extends Partial<BacktestMetrics> {
  available: boolean;
  reason?: string;
  label?: string;
  caveats?: string[];
  coverage_fraction?: number;
}

export interface BacktestResponse {
  label: string;
  market: string;
  split: string;
  /** What the reader needs to know about the partition. Rendered verbatim. */
  split_note: string;
  strategy: { name: string; version: string; params: Record<string, unknown> };
  cost_model: Record<string, number | string>;
  universe: string[];
  start_date: string;
  end_date: string;
  initial_capital: number;
  final_equity: number;
  n_trades: number;
  metrics: BacktestMetrics;
  benchmark: BenchmarkMetrics;
  rejected_entries: Record<string, number>;
  limitations: string[];
  disclaimer: string;
  equity_curve?: Array<{ ts: string; equity: number | null }>;
  drawdown_curve?: Array<{ ts: string; drawdown_pct: number | null }>;
  monthly_returns?: Array<{ period: string; return_pct: number | null }>;
  annual_returns?: Array<{ period: string; return_pct: number | null }>;
  trades?: TradeRow[];
}

export interface SplitInfo {
  split: string;
  start: string;
  end: string;
  readable_via_api: boolean;
  reason: string;
  note: string;
}

export interface ScanParams {
  market?: string;
  strategy?: string;
  action?: string;
  min_score?: number;
  sort?: string;
  limit?: number;
  tradable_only?: boolean;
}

export interface BacktestParams {
  market?: string;
  strategy?: string;
  split?: string;
  start?: string;
  end?: string;
  symbols?: string;
  capital?: number;
  include_trades?: boolean;
  include_equity?: boolean;
}

/* ------------------------------------------------------------------ */
/* Endpoints                                                          */
/* ------------------------------------------------------------------ */

export const api = {
  health: () => get<Health>('/api/health'),
  strategies: () => get<{ strategies: StrategyInfo[]; note: string }>('/api/strategies'),
  splits: () => get<{ splits: SplitInfo[] }>('/api/splits'),
  scan: (params: ScanParams = {}) =>
    get<ScanResponse>('/api/scan', {
      market: params.market,
      strategy: params.strategy,
      action: params.action,
      min_score: params.min_score,
      sort: params.sort,
      limit: params.limit,
      tradable_only: params.tradable_only ? 'true' : undefined,
    }),
  backtest: (params: BacktestParams = {}) =>
    get<BacktestResponse>('/api/backtest', {
      market: params.market,
      strategy: params.strategy,
      split: params.split,
      start: params.start,
      end: params.end,
      symbols: params.symbols,
      capital: params.capital,
      include_trades: params.include_trades === false ? 'false' : undefined,
      include_equity: params.include_equity === false ? 'false' : undefined,
    }),
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
