import { PendingOrders } from "../components/PendingOrders";
import { Fills } from "../components/Tables";
import { type Bot } from "../useBot";

export function OrdersPage({ bot }: { bot: Bot }) {
  const { pending, fills } = bot;
  const executed = fills.length > 0;

  return (
    <>
      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Queued</h2>
          <span className="panel-aside muted">
            decided on one bar, executed on the next
          </span>
        </div>
        <PendingOrders orders={pending} />
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Executed</h2>
          <span className="panel-aside muted">
            {executed ? "filled at the opening price of the bar after the decision" : "no fills recorded"}
          </span>
        </div>
        <div className="panel-body flush">
          <Fills fills={fills} />
        </div>
      </section>

      <section className="note-panel">
        <h2 className="note-title">How an order travels</h2>
        <ol className="flow">
          <li>
            <span className="flow-step">decide</span>
            The strategy reads the newest closed bar and produces a target weight.
          </li>
          <li>
            <span className="flow-step">queue</span>
            The order is written to the ledger as pending, tagged with that bar.
          </li>
          <li>
            <span className="flow-step">execute</span>
            It fills only when a strictly newer bar exists, priced at that bar's open.
          </li>
          <li>
            <span className="flow-step">re-check</span>
            Limits and quantity are re-evaluated at the execution price, not the decision price.
          </li>
        </ol>
      </section>
    </>
  );
}