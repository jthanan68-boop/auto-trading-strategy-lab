import type { RiskEvent } from "../api";

export function RiskLog({ events }: { events: RiskEvent[] }) {
  if (events.length === 0) return <div className="empty">No risk events recorded</div>;

  return (
    <div className="feed">
      {events.map((event) => (
        <div className="event" key={event.id}>
          <span className="event-time">
            {(() => {
              const d = new Date(event.ts);
              return isNaN(d.getTime()) ? "-" : d.toLocaleTimeString("en-IN", { timeStyle: "medium" });
            })()}
          </span>
          <span className="event-kind" style={{ color: tone(event.kind) }}>{event.kind}</span>
          <span className="event-detail">{event.detail}</span>
        </div>
      ))}
    </div>
  );
}

function tone(kind: string): string {
  if (kind.includes("halt") || kind.includes("loss") || kind.includes("mismatch") || kind.includes("down")) {
    return "#e2574c";
  }
  if (kind.includes("stop_loss") || kind.includes("rejected") || kind.includes("cap")) return "#d9a441";
  if (kind.includes("resumed") || kind.includes("start")) return "#3fb98c";
  return "#8c9c97";
}