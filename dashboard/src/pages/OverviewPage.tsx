import { api, controlEnabled } from "../api";
import { EquityChart } from "../components/EquityChart";
import { PendingOrders } from "../components/PendingOrders";
import { SessionBar } from "../components/SessionBar";
import { money, signed, type Bot } from "../useBot";

export function OverviewPage({ bot }: { bot: Bot }) {
  const { status, equity, portfolio, bars, barsState, symbol, pending } = bot;
  const halted = status?.halted ?? false;
  const dayPnl = status?.day_pnl ?? 0;
  const cap = status?.limits.max_total_exposure ?? 0;
  const used = status && cap > 0 ? Math.min(100, (status.exposure / cap) * 100) : 0;
  const activeBars = bars[symbol] ?? [];
  const controlsEnabled = controlEnabled();

  return (
    <>
      <SessionBar bars={activeBars} symbol={symbol} />

      <section className="tape">
        <div className="tape-primary">
          <div className="figure">
            <div className="figure-key">Equity</div>
            <div className="figure-value">{status ? money(status.equity) : "-"}</div>
          </div>
          <div className="figure">
            <div className="figure-key">
              Exposure
              <span className="figure-meter" aria-hidden="true">
                <span className="figure-meter-fill" style={{ width: `${used}%` }} />
              </span>
            </div>
            <div className="figure-value">
              {status ? money(status.exposure) : "-"}
              <span className="figure-of">of {money(cap)}</span>
            </div>
          </div>
          <div className="figure">
            <div className="figure-key">Day P/L</div>
            <div className={`figure-value ${status ? (dayPnl >= 0 ? "pos" : "neg") : ""}`}>
              {status ? signed(dayPnl) : "-"}
            </div>
          </div>
        </div>
        <div className="tape-secondary">
          <span><span className="k">Cash</span> {status ? money(status.cash) : "-"}</span>
          <span>
            <span className="k">Unrealised</span>{" "}
            <span className={status ? (status.unrealised >= 0 ? "pos" : "neg") : ""}>
              {status ? signed(status.unrealised) : "-"}
            </span>
          </span>
          <span><span className="k">Queued</span> {pending.length}</span>
          <span><span className="k">Fills</span> {bot.fills.length}</span>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Waiting</h2>
          {pending.length > 0 && (
            <span className="panel-aside muted">
              nothing fills until the next daily bar publishes
            </span>
          )}
        </div>
        <PendingOrders orders={pending} />
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Equity</h2>
        </div>
        <div className="panel-body">
          {equity.length > 1 ? (
            <EquityChart points={equity} />
          ) : (
            <div className="empty">
              No equity history yet. It begins after the first cycle that changes the book.
            </div>
          )}
        </div>
      </section>

      {portfolio && portfolio.enabled && (
        <section className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Portfolio</h2>
          </div>
          <div className="panel-body">
            <div className="portfolio-grid">
              <div className="portfolio-row">
                <span className="portfolio-key">Mode</span>
                <span className="portfolio-value">Risk-managed</span>
              </div>
              <div className="portfolio-row">
                <span className="portfolio-key">Holdings</span>
                <span className="portfolio-value">top {portfolio.top_n} by momentum</span>
              </div>
              <div className="portfolio-row">
                <span className="portfolio-key">Rebalance</span>
                <span className="portfolio-value">{portfolio.rebalance_cadence.replace("_", " ")}</span>
              </div>
              <div className="portfolio-row">
                <span className="portfolio-key">Last rebalanced</span>
                <span className="portfolio-value">
                  {portfolio.last_rebalance ? new Date(portfolio.last_rebalance).toLocaleString("en-IN", { dateStyle: "short", timeStyle: "medium" }) : "never"}
                </span>
              </div>
              <div className="portfolio-row">
                <span className="portfolio-key">Vol target</span>
                <span className="portfolio-value">${(portfolio.vol_target * 100).toFixed(0)}% per position</span>
              </div>
              <div className="portfolio-row">
                <span className="portfolio-key">Max position</span>
                <span className="portfolio-value">${(portfolio.max_position_pct * 100).toFixed(0)}% of equity</span>
              </div>
              <div className="portfolio-row">
                <span className="portfolio-key">Max sector</span>
                <span className="portfolio-value">${(portfolio.max_sector_pct * 100).toFixed(0)}% of equity</span>
              </div>
              <div className="portfolio-row">
                <span className="portfolio-key">Trailing stop</span>
                <span className="portfolio-value">${(portfolio.trailing_stop_pct * 100).toFixed(0)}% from high</span>
              </div>
              <div className="portfolio-row">
                <span className="portfolio-key">Take profit</span>
                <span className="portfolio-value">${(portfolio.take_profit_pct * 100).toFixed(0)}% from entry</span>
              </div>
              <div className="portfolio-row">
                <span className="portfolio-key">MDD breaker</span>
                <span className="portfolio-value">${(portfolio.mdd_circuit_breaker_pct * 100).toFixed(0)}% drawdown</span>
              </div>
              {portfolio.targets && Object.keys(portfolio.targets).length > 0 && (
                <div className="portfolio-row">
                  <span className="portfolio-key">Target weights</span>
                  <span className="portfolio-value">
                    {Object.entries(portfolio.targets as Record<string, number>)
                      .map(([s, w]: [string, number]) => `${s}: ${(w * 100).toFixed(1)}%`)
                      .join("  ")}
                  </span>
                </div>
              )}
              {portfolio.scores && portfolio.scores.length > 0 && (
                <div className="portfolio-row">
                  <span className="portfolio-key">Momentum rank</span>
                  <span className="portfolio-value">
                    {portfolio.scores.map(([s, sc]: [string, number]) => `${s}: ${(sc * 100).toFixed(1)}%`).join("  ")}
                  </span>
                </div>
              )}
            </div>
          </div>
        </section>
      )}

      <section className="controls">
        <div className="controls-copy">
          <h2 className="controls-title">Run a cycle</h2>
          <p className="controls-note">
            Reads the newest bar, asks the strategy, and queues anything new. It never fills on
            the bar that produced the decision.
          </p>
        </div>
        <div className="controls-actions">
          <button
            className="btn"
            disabled={bot.busy || halted || !status?.control_enabled || !controlsEnabled}
            onClick={() => bot.run(api.runCycle)}
          >
            Run one cycle
          </button>
          <button
            className="btn"
            disabled={bot.busy || !halted || !status?.control_enabled || !controlsEnabled}
            onClick={() => bot.run(api.resume)}
          >
            Resume trading
          </button>
        </div>
      </section>

      {barsState[symbol] === "loading" && <p className="sr-only">Loading market data</p>}
    </>
  );
}