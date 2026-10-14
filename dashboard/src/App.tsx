import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type Bar,
  type EquityPoint,
  type Fill,
  type Position,
  type RiskEvent,
  type Signal,
  type Status,
} from "./api";
import { EquityChart } from "./components/EquityChart";
import { PriceChart } from "./components/PriceChart";
import { RiskLog } from "./components/RiskLog";
import { Fills, Positions, Signals } from "./components/Tables";

type Tab = "positions" | "signals" | "fills" | "risk";

const REFRESH_MS = 5000;

function money(value: number): string {
  return value.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

function signed(value: number): string {
  return `${value >= 0 ? "+" : ""}${money(value)}`;
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [fills, setFills] = useState<Fill[]>([]);
  const [events, setEvents] = useState<RiskEvent[]>([]);
  const [bars, setBars] = useState<Record<string, Bar[]>>({});
  const [barsState, setBarsState] = useState<Record<string, "loading" | "ready" | "error">>({});
  const [symbol, setSymbol] = useState<string>("");
  const [tab, setTab] = useState<Tab>("positions");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [online, setOnline] = useState(false);
  const chartSymbol = useRef<string>("");

  const load = useCallback(async () => {
    try {
      const [s, p, e, sig, f, r] = await Promise.all([
        api.status(), api.positions(), api.equity(),
        api.signals(), api.fills(), api.riskEvents(),
      ]);
      setStatus(s); setPositions(p); setEquity(e); setSignals(sig);
      setFills(f); setEvents(r); setOnline(true); setError(null);
      if (!chartSymbol.current && s.symbols.length > 0) chartSymbol.current = s.symbols[0];
      if (!symbol && s.symbols.length > 0) setSymbol(s.symbols[0]);
    } catch (cause) {
      setOnline(false);
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [symbol]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (!symbol) return;
    setBarsState((previous) => ({ ...previous, [symbol]: "loading" }));
    api.bars(symbol)
      .then((payload) => {
        setBars((previous) => ({ ...previous, [symbol]: payload.bars }));
        setBarsState((previous) => ({ ...previous, [symbol]: "ready" }));
      })
      .catch(() => {
        setBars((previous) => ({ ...previous, [symbol]: [] }));
        setBarsState((previous) => ({ ...previous, [symbol]: "error" }));
      });
  }, [symbol]);

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

  const halted = status?.halted ?? false;
  const dayPnl = status?.day_pnl ?? 0;

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1 className="title">StratBot</h1>
          <p className="subtitle">{status?.strategy ?? "..."} &middot; {status?.cycles ?? 0} cycles</p>
        </div>
        <div className="badges">
          <span className={`badge ${status?.is_paper ? "paper" : "live"}`}>
            {status?.mode ?? "..."}
          </span>
          <span className={`badge ${halted ? "halted" : online ? "running" : ""}`}>
            {halted ? "HALTED" : online ? "RUNNING" : "OFFLINE"}
          </span>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      {halted && status?.halt_reason && (
        <div className="halt-banner">
          <span>Bot halted:</span>
          <strong>{status.halt_reason.replace(/_/g, " ")}</strong>
        </div>
      )}

      <section className="stats">
        <Stat label="Equity" value={status ? money(status.equity) : "-"} />
        <Stat label="Cash" value={status ? money(status.cash) : "-"} />
        <Stat
          label="Day P/L"
          value={status ? signed(dayPnl) : "-"}
          tone={status ? (dayPnl >= 0 ? "pos" : "neg") : ""}
        />
        <Stat
          label="Exposure"
          value={status ? money(status.exposure) : "-"}
          foot={status ? `of ${money(status.limits.max_total_exposure)}` : ""}
        />
        <Stat
          label="Unrealised"
          value={status ? signed(status.unrealised) : "-"}
          tone={status ? (status.unrealised >= 0 ? "pos" : "neg") : ""}
        />
        <Stat
          label="Daily limit"
          value={status ? money(status.limits.max_daily_loss) : "-"}
          foot={status ? `stop loss ${(status.limits.stop_loss_pct * 100).toFixed(0)}%` : ""}
        />
      </section>

      <div className="card">
        <div className="card-head">
          <h2 className="card-title">Controls</h2>
        </div>
        <div className="card-body">
          <div className="stop-row">
            <button
              className="btn btn-danger"
              disabled={busy || !status?.control_enabled}
              onClick={() => run(api.emergencyStop)}
            >
              EMERGENCY STOP
            </button>
            <button
              className="btn btn-quiet"
              disabled={busy || !halted || !status?.control_enabled}
              onClick={() => run(api.resume)}
            >
              Resume
            </button>
            <button
              className="btn btn-quiet"
              disabled={busy || !status?.control_enabled}
              onClick={() => run(api.runCycle)}
            >
              Run one cycle
            </button>
          </div>
          {!status?.control_enabled && (
            <p className="note">
              Operator controls are disabled. Set <code>STRATBOT_CONTROL_TOKEN</code> on the server and
              <code> VITE_CONTROL_TOKEN</code> in the build to enable the stop button. Until then the
              controls fail closed rather than acting without a token.
            </p>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2 className="card-title">Equity</h2>
        </div>
        <div className="card-body">
          {equity.length > 1 ? <EquityChart points={equity} /> : <div className="empty">Equity history builds up as the bot runs</div>}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2 className="card-title">Price &middot; {symbol}</h2>
          <div className="tabs">
            {(status?.symbols ?? []).map((name) => (
              <button
                key={name}
                className={`tab ${name === symbol ? "active" : ""}`}
                onClick={() => setSymbol(name)}
              >
                {name}
              </button>
            ))}
          </div>
        </div>
        <div className="card-body">
          {barsState[symbol] === "ready" && bars[symbol]?.length ? (
            <PriceChart bars={bars[symbol]} symbol={symbol} />
          ) : barsState[symbol] === "error" ? (
            <div className="empty">Could not load {symbol} price history. Run the strategy lab once to populate the cache.</div>
          ) : (
            <div className="empty">Loading {symbol} price history</div>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2 className="card-title">Ledger</h2>
          <div className="tabs">
            {(["positions", "signals", "fills", "risk"] as Tab[]).map((name) => (
              <button key={name} className={`tab ${name === tab ? "active" : ""}`} onClick={() => setTab(name)}>
                {name}
              </button>
            ))}
          </div>
        </div>
        <div className="card-body flush">
          {tab === "positions" && <Positions positions={positions} />}
          {tab === "signals" && <Signals signals={signals} />}
          {tab === "fills" && <Fills fills={fills} />}
          {tab === "risk" && <RiskLog events={events} />}
        </div>
      </div>

      <p className="note">
        Paper simulation only. This server cannot place a real order.
      </p>
    </div>
  );
}

function Stat({ label, value, tone = "", foot = "" }: { label: string; value: string; tone?: string; foot?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone}`}>{value}</div>
      {foot && <div className="stat-foot">{foot}</div>}
    </div>
  );
}