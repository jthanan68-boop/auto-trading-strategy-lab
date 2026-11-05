import { useEffect, useState } from "react";
import type { Bar } from "../api";

const CRYPTO = new Set(["BTC", "ETH", "USDT"]);

function atUtc(date: Date, dayOffset: number, hour: number, minute = 0): Date {
  return new Date(Date.UTC(
    date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate() + dayOffset, hour, minute,
  ));
}

// Indian equities close at 15:30 IST, which is 10:00 UTC. Crypto has no close;
// its UTC day simply rolls over at midnight.
function sessionEnd(barDate: Date, crypto: boolean): Date {
  return crypto ? atUtc(barDate, 1, 0) : atUtc(barDate, 0, 10);
}

function nextSessionOpen(barDate: Date, crypto: boolean): Date {
  if (crypto) return sessionEnd(barDate, true);
  const open = atUtc(barDate, 1, 3, 45); // 09:15 IST, skipping the weekend
  while (open.getUTCDay() === 0 || open.getUTCDay() === 6) {
    open.setUTCDate(open.getUTCDate() + 1);
  }
  return open;
}

function countdown(target: Date, now: number): string {
  const seconds = Math.max(0, Math.round((target.getTime() - now) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours >= 24) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  return `${hours}h ${String(minutes).padStart(2, "0")}m`;
}

const DAY = new Intl.DateTimeFormat("en-GB", {
  day: "numeric", month: "short", timeZone: "UTC",
});

/**
 * The newest bar is the unit of time for this whole system: a decision is made
 * on one daily candle and executed on the next. That single fact is the most
 * common reason the dashboard looks idle, so it gets a permanent home instead
 * of being buried in a log.
 */
export function SessionBar({ bars, symbol }: { bars: Bar[]; symbol: string }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);

  const newest = bars.at(-1);
  if (!newest) {
    return (
      <div className="session">
        <span className="session-key">Session</span>
        <span className="session-text muted">No bar loaded for {symbol}</span>
      </div>
    );
  }

  const stamp = newest.ts.endsWith("Z") ? newest.ts : `${newest.ts}Z`;
  const barDate = new Date(stamp);
  if (isNaN(barDate.getTime())) {
    return (
      <div className="session">
        <span className="session-key">Session</span>
        <span className="session-text muted">Invalid bar timestamp for {symbol}</span>
      </div>
    );
  }
  const crypto = CRYPTO.has(symbol);
  const end = sessionEnd(barDate, crypto);
  const forming = now < end.getTime();

  return (
    <div className={`session ${forming ? "forming" : "closed"}`}>
      <span className="session-key">Session</span>
      <span className="session-date">{DAY.format(barDate)}</span>
      <span className={`session-state ${forming ? "is-forming" : "is-closed"}`}>
        {forming ? "still forming" : "closed"}
      </span>
      <span className="session-rule" aria-hidden="true" />
      <span className="session-text muted">
        {forming
          ? `closes in ${countdown(end, now)}`
          : `next bar in ${countdown(nextSessionOpen(barDate, crypto), now)}`}
      </span>
      <span className="session-text dim">
        {crypto ? "24h" : "NSE"} · a decision fills on the next bar's open
      </span>
    </div>
  );
}