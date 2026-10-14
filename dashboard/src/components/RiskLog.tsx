import type { RiskEvent } from "../api";

export function RiskLog({ events }: { events: RiskEvent[] }) {
  if (events.length === 0) return <div className="empty">No risk events recorded</div>;

  return (
    <div className="feed">
      {events.map((event) => (
        <div className="event" key={event.id}>
          <span className="event-time">
            {new Date(event.ts).toLocaleTimeString("en-IN", { timeStyle: "medium" })}
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
    return "#ef5350";
  }
  if (kind.includes("stop_loss") || kind.includes("rejected") || kind.includes("cap")) return "#f0b90b";
  if (kind.includes("resumed") || kind.includes("start")) return "#26a69a";
  return "#8b93a7";
}