import { Positions } from "../components/Tables";
import { money, signed, type Bot } from "../useBot";

export function PositionsPage({ bot }: { bot: Bot }) {
  const { positions, status } = bot;
  const unrealised = status?.unrealised ?? 0;

  return (
    <>
      <section className="tape">
        <div className="tape-primary">
          <div className="figure">
            <div className="figure-key">Cash</div>
            <div className="figure-value">{status ? money(status.cash) : "-"}</div>
          </div>
          <div className="figure">
            <div className="figure-key">Exposure</div>
            <div className="figure-value">{status ? money(status.exposure) : "-"}</div>
          </div>
          <div className="figure">
            <div className="figure-key">Unrealised</div>
            <div className={`figure-value ${status ? (unrealised >= 0 ? "pos" : "neg") : ""}`}>
              {status ? signed(unrealised) : "-"}
            </div>
          </div>
        </div>
        <div className="tape-secondary">
          <span><span className="k">Open</span> {positions.length}</span>
          <span><span className="k">Equity</span> {status ? money(status.equity) : "-"}</span>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Open positions</h2>
        </div>
        <div className="panel-body flush">
          <Positions positions={positions} />
        </div>
      </section>

      {positions.length === 0 && (
        <section className="note-panel">
          <h2 className="note-title">Nothing on the book</h2>
          <p className="note-body">
            No symbol is held. A position only opens when a queued order reaches its execution bar,
            so a quiet book is normal between sessions rather than a fault.
          </p>
        </section>
      )}
    </>
  );
}