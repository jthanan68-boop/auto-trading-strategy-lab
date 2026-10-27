import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type Bar,
  type EquityPoint,
  type Fill,
  type Order,
  type Position,
  type Quote,
  type RiskEvent,
  type Signal,
  type Status,
} from "./api";

export const REFRESH_MS = 5000;

export function useBot() {
  const [status, setStatus] = useState<Status | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [fills, setFills] = useState<Fill[]>([]);
  const [pending, setPending] = useState<Order[]>([]);
  const [events, setEvents] = useState<RiskEvent[]>([]);
  const [bars, setBars] = useState<Record<string, Bar[]>>({});
  const [barsState, setBarsState] = useState<Record<string, "loading" | "ready" | "error">>({});
  const [quotes, setQuotes] = useState<Record<string, Quote>>({});
  const [symbol, setSymbol] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [online, setOnline] = useState(false);
  const requested = useRef(new Set<string>());

  const load = useCallback(async () => {
    try {
      const [s, p, e, sig, f, r, queued] = await Promise.all([
        api.status(), api.positions(), api.equity(),
        api.signals(), api.fills(), api.riskEvents(), api.pendingOrders(),
      ]);
      setStatus(s); setPositions(p); setEquity(e); setSignals(sig);
      setFills(f); setEvents(r); setPending(queued);
      // Quotes are a nicety: a failure here must not take the dashboard down.
      const rows = await api.quotes().catch(() => [] as Quote[]);
      setQuotes(Object.fromEntries(rows.map((row) => [row.symbol, row])));
      setOnline(true); setError(null);
      setSymbol((current) => current || s.symbols[0] || "");
    } catch (cause) {
      setOnline(false);
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  // Every symbol is fetched, not just the selected one, so the watchlist can show a
  // real price and a real change instead of "loading" on four of five rows.
  // The key is a string and the in-flight names live in a ref: depending on the
  // array itself would re-run this on every render and spin.
  const symbolKey = (status?.symbols ?? []).join(",");
  useEffect(() => {
    if (!symbolKey) return;
    for (const name of symbolKey.split(",")) {
      if (requested.current.has(name)) continue;
      requested.current.add(name);
      setBarsState((previous) => ({ ...previous, [name]: "loading" }));
      api.bars(name)
        .then((payload) => {
          setBars((previous) => ({ ...previous, [name]: payload.bars }));
          setBarsState((previous) => ({ ...previous, [name]: "ready" }));
        })
        .catch(() => {
          setBars((previous) => ({ ...previous, [name]: [] }));
          setBarsState((previous) => ({ ...previous, [name]: "error" }));
        });
    }
  }, [symbolKey]);

  async function run<T>(action: () => Promise<T>): Promise<T | null> {
    setBusy(true);
    setError(null);
    try {
      const result = await action();
      await load();
      return result;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      return null;
    } finally {
      setBusy(false);
    }
  }

  return {
    status, positions, equity, signals, fills, pending, events,
    bars, barsState, quotes, symbol, setSymbol, error, busy, online, load, run,
  };
}

export type Bot = ReturnType<typeof useBot>;

export function money(value: number): string {
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export function signed(value: number): string {
  return `${value >= 0 ? "+" : ""}${money(value)}`;
}