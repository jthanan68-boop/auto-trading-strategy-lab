import type { Fill, Position, Signal } from "../api";

function money(value: number): string {
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function qty(value: number): string {
  return value < 1 ? value.toFixed(6) : value.toFixed(4);
}

export function Positions({ positions }: { positions: Position[] }) {
  if (positions.length === 0) {
    return <div className="empty">No open positions</div>;
  }
  return (
    <table>
      <thead>
        <tr>
          <th>Symbol</th><th>Qty</th><th>Avg</th><th>Last</th><th>Value</th><th>P/L</th>
        </tr>
      </thead>
      <tbody>
        {positions.map((position) => (
          <tr key={position.symbol}>
            <td className="sym">{position.symbol}</td>
            <td>{qty(position.qty)}</td>
            <td>{position.avg_price.toFixed(2)}</td>
            <td>{position.last_price.toFixed(2)}</td>
            <td>{money(position.market_value)}</td>
            <td className={position.unrealised_pnl >= 0 ? "pos" : "neg"}>
              {money(position.unrealised_pnl)} ({position.unrealised_pct.toFixed(2)}%)
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Signals({ signals }: { signals: Signal[] }) {
  if (signals.length === 0) return <div className="empty">No signals yet</div>;
  return (
    <table>
      <thead>
        <tr><th>Time</th><th>Symbol</th><th>Side</th><th>Price</th><th>Weight</th><th>Reason</th></tr>
      </thead>
      <tbody>
        {signals.map((signal) => (
          <tr key={signal.id}>
            <td className="muted">{(() => { const d = new Date(signal.ts); return isNaN(d.getTime()) ? "-" : d.toLocaleString("en-IN", { dateStyle: "short", timeStyle: "medium" }) })()}</td>
            <td className="sym">{signal.symbol}</td>
            <td className={signal.action === "BUY" ? "side-buy" : "side-sell"}>{signal.action}</td>
            <td>{signal.price.toFixed(2)}</td>
            <td>{signal.weight.toFixed(2)}</td>
            <td className="reason">{signal.reason}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Fills({ fills }: { fills: Fill[] }) {
  if (fills.length === 0) return <div className="empty">No fills yet</div>;
  return (
    <table>
      <thead>
        <tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Fees</th></tr>
      </thead>
      <tbody>
        {fills.map((fill) => (
          <tr key={fill.id}>
            <td className="muted">{(() => { const d = new Date(fill.ts); return isNaN(d.getTime()) ? "-" : d.toLocaleString("en-IN", { dateStyle: "short", timeStyle: "medium" }) })()}</td>
            <td className="sym">{fill.symbol}</td>
            <td className={fill.side === "BUY" ? "side-buy" : "side-sell"}>{fill.side}</td>
            <td>{qty(fill.qty)}</td>
            <td>{fill.price.toFixed(2)}</td>
            <td className="muted">{fill.fees.toFixed(2)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}