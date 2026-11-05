import type { Order } from "../api";

function qty(value: number): string {
  return value < 1 ? value.toFixed(6) : value.toFixed(4);
}

const DAY = new Intl.DateTimeFormat("en-GB", {
  day: "numeric", month: "short", timeZone: "UTC",
});

function barDate(stamp: string | null): string {
  if (!stamp) return "--";
  const d = new Date(stamp.endsWith("Z") ? stamp : `${stamp}Z`);
  return isNaN(d.getTime()) ? "--" : DAY.format(d);
}

/**
 * Orders raised on a bar but not yet filled.
 *
 * This is the panel that answers "why is nothing happening": every row is work
 * the bot has committed to and is waiting on a later bar to execute against.
 */
export function PendingOrders({ orders }: { orders: Order[] }) {
  if (orders.length === 0) {
    return (
      <div className="waiting quiet">
        <span className="waiting-title">Nothing waiting</span>
        <span className="waiting-note muted">
          No queued orders. The bot holds no unexecuted intent.
        </span>
      </div>
    );
  }

  return (
    <div className="waiting">
      <div className="waiting-head">
        <span className="waiting-count">{orders.length}</span>
        <span className="waiting-title">
          order{orders.length === 1 ? "" : "s"} decided, waiting for the next bar to open
        </span>
      </div>
      <div className="waiting-rows">
        {orders.map((order) => (
          <div className="waiting-row" key={order.client_order_id}>
            <span className="sym">{order.symbol}</span>
            <span className={order.side === "BUY" ? "side-buy" : "side-sell"}>{order.side}</span>
            <span className="waiting-qty">{qty(order.qty)}</span>
            <span className="waiting-arrow">
              decided {barDate(order.signal_bar_ts)}
              <span className="dim"> → fills at the next open</span>
            </span>
            {order.execution_bar_ts && (
              <span className="muted">filled on {barDate(order.execution_bar_ts)}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}