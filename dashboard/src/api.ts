/**
 * API client.
 *
 * Requests are relative by default so the dashboard works same-origin with zero
 * configuration, both when FastAPI serves it and inside an APK webview. VITE_API_URL
 * only needs setting when the UI is hosted separately from the API.
 */

const BASE = import.meta.env.VITE_API_URL ?? "";

function headers(control = false): Record<string, string> {
  const base: Record<string, string> = { "Content-Type": "application/json" };
  if (control) {
    const token = import.meta.env.VITE_CONTROL_TOKEN;
    if (!token) throw new Error("VITE_CONTROL_TOKEN is not set, operator controls are disabled");
    base["X-Control-Token"] = token;
  }
  return base;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status} ${path}: ${detail.slice(0, 160)}`);
  }
  return response.json() as Promise<T>;
}

const get = <T,>(path: string) => request<T>(path);

const control = <T,>(path: string) =>
  request<T>(path, { method: "POST", headers: headers(true) });

export interface Limits {
  max_position_value: number;
  max_total_exposure: number;
  max_daily_loss: number;
  stop_loss_pct: number;
}

export interface Status {
  mode: string;
  is_paper: boolean;
  realtime: boolean;
  halted: boolean;
  halt_reason: string | null;
  cycles: number;
  strategy: string;
  symbols: string[];
  equity: number;
  cash: number;
  exposure: number;
  unrealised: number;
  day_pnl: number;
  limits: Limits;
  control_enabled: boolean;
}

export interface Position {
  symbol: string;
  qty: number;
  avg_price: number;
  last_price: number;
  market_value: number;
  unrealised_pnl: number;
  unrealised_pct: number;
}

export interface EquityPoint {
  ts: string;
  equity: number;
  cash: number;
  exposure: number;
  unrealised: number;
}

export interface Signal {
  id: number;
  ts: string;
  symbol: string;
  strategy: string;
  action: string;
  weight: number;
  price: number;
  bar_ts: string;
  reason: string;
}

export interface Fill {
  id: number;
  client_order_id: string;
  ts: string;
  symbol: string;
  side: string;
  qty: number;
  price: number;
  fees: number;
}

export interface Order {
  client_order_id: string;
  ts: string;
  symbol: string;
  side: string;
  qty: number;
  reference_price: number;
  strategy: string;
  status: string;
  reason: string;
  signal_bar_ts: string | null;
  target_weight: number | null;
  execution_bar_ts: string | null;
}

export interface RiskEvent {
  id: number;
  ts: string;
  kind: string;
  detail: string;
}

export interface Bar {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface Quote {
  symbol: string;
  price: number;
  ts: string;
  source: string;
  delayed: boolean;
  age_seconds: number;
  usable: boolean;
}

export const api = {
  status: () => get<Status>("/api/status"),
  positions: () => get<Position[]>("/api/positions"),
  quotes: () => get<Quote[]>("/api/quotes"),
  equity: () => get<EquityPoint[]>("/api/equity"),
  signals: () => get<Signal[]>("/api/signals"),
  fills: () => get<Fill[]>("/api/orders"),
  pendingOrders: () => get<Order[]>("/api/pending-orders"),
  riskEvents: () => get<RiskEvent[]>("/api/risk-events"),
  bars: (symbol: string, limit = 180) =>
    get<{ symbol: string; bars: Bar[] }>(`/api/bars/${encodeURIComponent(symbol)}?limit=${limit}`),

  emergencyStop: () => control<{ halted: boolean; cancelled: number; flattened: string[] }>("/api/emergency-stop"),
  resume: () => control<{ halted: boolean }>("/api/resume"),
  runCycle: () => control<{ cycle: number; halted: boolean; equity: number; decisions: string[] }>("/api/cycle"),
};