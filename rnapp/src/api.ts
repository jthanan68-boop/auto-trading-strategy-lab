/**
 * API client for React Native.
 *
 * Set EXPO_PUBLIC_API_URL to point at your FastAPI server (e.g. http://192.168.1.10:8000).
 * If unset, defaults to empty string (same-origin when served from the API).
 */

const BASE = process.env.EXPO_PUBLIC_API_URL ?? "";

function headers(control = false): Record<string, string> {
  const base: Record<string, string> = { "Content-Type": "application/json" };
  if (control) {
    const token = process.env.EXPO_PUBLIC_CONTROL_TOKEN;
    if (token) {
      base["X-Control-Token"] = token;
    }
  }
  return base;
}

export function controlEnabled(): boolean {
  return !!process.env.EXPO_PUBLIC_CONTROL_TOKEN;
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

const controlSafe = async <T>(
  path: string
): Promise<T | { success: false; reason: string }> => {
  const token = process.env.EXPO_PUBLIC_CONTROL_TOKEN;
  if (!token) {
    return { success: false, reason: "operator controls disabled: EXPO_PUBLIC_CONTROL_TOKEN not set" } as T;
  }
  return request<T>(path, { method: "POST", headers: headers(true) });
};

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

  emergencyStop: () =>
    controlSafe<{ halted: boolean; cancelled: number; flattened: string[] }>("/api/emergency-stop"),
  resume: () =>
    controlSafe<{ halted: boolean }>("/api/resume"),
  runCycle: () =>
    controlSafe<{ cycle: number; halted: boolean; equity: number; decisions: string[] }>("/api/cycle"),
};