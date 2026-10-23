import { useHashRoute } from "./router";
import { useBot } from "./useBot";
import { OverviewPage } from "./pages/OverviewPage";
import { MarketsPage } from "./pages/MarketsPage";
import { OrdersPage } from "./pages/OrdersPage";
import { PositionsPage } from "./pages/PositionsPage";
import { RiskPage } from "./pages/RiskPage";

const PAGES = [
  { id: "overview", label: "Overview", glyph: "01" },
  { id: "markets", label: "Markets", glyph: "02" },
  { id: "orders", label: "Orders", glyph: "03" },
  { id: "positions", label: "Positions", glyph: "04" },
  { id: "risk", label: "Risk", glyph: "05" },
] as const;

export default function App() {
  const bot = useBot();
  const { route, navigate } = useHashRoute("overview");
  const page = PAGES.find((entry) => entry.id === route) ?? PAGES[0];
  const halted = bot.status?.halted ?? false;
  const queued = bot.pending.length;

  const Body = () => {
    switch (page.id) {
      case "markets": return <MarketsPage bot={bot} />;
      case "orders": return <OrdersPage bot={bot} />;
      case "positions": return <PositionsPage bot={bot} />;
      case "risk": return <RiskPage bot={bot} />;
      default: return <OverviewPage bot={bot} />;
    }
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <div>
            <h1 className="wordmark">StratBot</h1>
            <p className="brand-sub">paper engine</p>
          </div>
        </div>

        <nav className="nav" aria-label="Pages">
          {PAGES.map((entry) => (
            <button
              key={entry.id}
              className={`nav-item ${entry.id === page.id ? "on" : ""}`}
              onClick={() => navigate(entry.id)}
              aria-current={entry.id === page.id ? "page" : undefined}
            >
              <span className="nav-glyph" aria-hidden="true">{entry.glyph}</span>
              <span className="nav-label">{entry.label}</span>
              {entry.id === "orders" && queued > 0 && (
                <span className="nav-badge">{queued}</span>
              )}
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="rail-state">
            <span className={`chip ${bot.status?.is_paper ? "chip-paper" : "chip-live"}`}>
              {bot.status?.mode ?? "..."}
            </span>
            <span className={`chip ${halted ? "chip-halt" : bot.online ? "chip-live-ok" : "chip-off"}`}>
              {halted ? "halted" : bot.online ? "running" : "offline"}
            </span>
            <span className={`chip ${bot.status?.realtime ? "chip-live-ok" : "chip-off"}`}>
              {bot.status?.realtime ? "live prices" : "daily close"}
            </span>
          </div>
          <p className="sidebar-meta mono">
            {bot.status?.strategy ?? "connecting"}
            <br />
            {bot.status?.cycles ?? 0} cycles
          </p>
        </div>
      </aside>

      <div className="stage">
        <header className="topbar">
          <h2 className="topbar-title">{page.label}</h2>
          <p className="topbar-note">
            {page.id === "overview" && "Is the engine safe, and what is it waiting for?"}
            {page.id === "markets" && "Daily candles, current session included."}
            {page.id === "orders" && "Every decision, from signal bar to execution bar."}
            {page.id === "positions" && "What the book actually holds."}
            {page.id === "risk" && "Limits, halts, and the switch to stop everything."}
          </p>
        </header>

        <main className="content">
          {bot.error && <div className="error" role="alert">{bot.error}</div>}

          {halted && bot.status?.halt_reason && (
            <div className="halt" role="status">
              <span className="halt-key">Trading halted</span>
              <span className="halt-reason">{bot.status.halt_reason.replace(/_/g, " ")}</span>
              <span className="halt-note">
                No new orders will be raised. Queued work stays queued until you resume.
              </span>
            </div>
          )}

          <Body />

          <footer className="colophon">
            <span>Paper simulation. This process cannot place a real order.</span>
            <span className="dim">Data: yfinance daily candles, Binance live crypto quotes</span>
          </footer>
        </main>
      </div>

      <nav className="tabbar" aria-label="Pages">
        {PAGES.map((entry) => (
          <button
            key={entry.id}
            className={`tab ${entry.id === page.id ? "on" : ""}`}
            onClick={() => navigate(entry.id)}
            aria-current={entry.id === page.id ? "page" : undefined}
          >
            <span className="tab-glyph" aria-hidden="true">{entry.glyph}</span>
            <span className="tab-label">{entry.label}</span>
            {entry.id === "orders" && queued > 0 && (
              <span className="nav-badge">{queued}</span>
            )}
          </button>
        ))}
      </nav>
    </div>
  );
}