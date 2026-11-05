import { api, controlEnabled } from "../api";
import { RiskLog } from "../components/RiskLog";
import { money, type Bot } from "../useBot";

export function RiskPage({ bot }: { bot: Bot }) {
  const { status, events, busy } = bot;
  const controlsEnabled = controlEnabled();
  const limits = status?.limits;
  const cap = limits?.max_total_exposure ?? 0;
  const used = status && cap > 0 ? Math.min(100, (status.exposure / cap) * 100) : 0;
  const dayPnl = status?.day_pnl ?? 0;
  const lossLimit = limits?.max_daily_loss ?? 0;
  const lossAmount = Math.max(0, -dayPnl);
  const lossUsed = lossLimit > 0
    ? Math.min(100, (lossAmount / lossLimit) * 100)
    : 0;

  return (
    <>
      <section className="tape">
        <div className="tape-primary">
          <div className="figure">
            <div className="figure-key">
              Exposure used
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
            <div className="figure-key">
              Daily loss used
              <span className="figure-meter" aria-hidden="true">
                <span className={`figure-meter-fill ${lossUsed > 0 ? "hot" : ""}`}
                      style={{ width: `${lossUsed}%` }} />
              </span>
            </div>
            <div className={`figure-value ${lossAmount > 0 ? "neg" : ""}`}>
              {status ? money(lossAmount) : "-"}
              <span className="figure-of">limit {money(lossLimit)}</span>
            </div>
          </div>
          <div className="figure">
            <div className="figure-key">Stop loss</div>
            <div className="figure-value">
              {limits ? `${(limits.stop_loss_pct * 100).toFixed(0)}%` : "-"}
              <span className="figure-of">per position</span>
            </div>
          </div>
        </div>
        <div className="tape-secondary">
          <span>
            <span className="k">Max position</span> {limits ? money(limits.max_position_value) : "-"}
          </span>
          <span>
            <span className="k">Max day loss</span> {limits ? money(limits.max_daily_loss) : "-"}
          </span>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Risk events</h2>
        </div>
        <div className="panel-body flush">
          <RiskLog events={events} />
        </div>
      </section>

      <section className="killzone">
        <div className="killzone-copy">
          <h2 className="killzone-title">Halt and flatten</h2>
          <p className="killzone-note">
            Stops trading, cancels every queued order, and sells the whole book at the last price.
            Nothing here can be undone from the dashboard.
          </p>
          {!status?.control_enabled && (
            <p className="killzone-note dim">
              Disabled. The server was started without <code>STRATBOT_CONTROL_TOKEN</code>, so this
              fails closed rather than acting without authorisation.
            </p>
          )}
          {!controlsEnabled && status?.control_enabled && (
            <p className="killzone-note dim">
              Disabled. This browser build was built without <code>VITE_CONTROL_TOKEN</code>, so
              operator controls are unavailable in this UI.
            </p>
          )}
        </div>
        <button
          className="btn btn-kill"
          disabled={busy || !status?.control_enabled || !controlsEnabled}
          onClick={() => bot.run(api.emergencyStop)}
        >
          Halt and flatten
        </button>
      </section>
    </>
  );
}