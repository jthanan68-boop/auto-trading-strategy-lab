import { View, Text } from "react-native";
import { styles } from "../styles";
import { shortDate } from "../format";

const CRYPTO = new Set(["BTC", "ETH", "USDT"]);

function atUtc(date, dayOffset, hour, minute = 0) {
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate() + dayOffset, hour, minute));
}

function sessionEnd(barDate, crypto) {
  return crypto ? atUtc(barDate, 1, 0) : atUtc(barDate, 0, 10);
}

function nextSessionOpen(barDate, crypto) {
  if (crypto) return sessionEnd(barDate, true);
  const open = atUtc(barDate, 1, 3, 45);
  while (open.getUTCDay() === 0 || open.getUTCDay() === 6) {
    open.setUTCDate(open.getUTCDate() + 1);
  }
  return open;
}

function countdown(target, now) {
  const seconds = Math.max(0, Math.round((target.getTime() - now) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours >= 24) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  return `${hours}h ${String(minutes).padStart(2, "0")}m`;
}

const DAY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });

export function SessionBar({ bars, symbol }) {
  const [now, setNow] = new Date().getTime();
  const newest = bars?.at(-1);
  if (!newest) {
    return (
      <View style={styles.session}>
        <Text style={styles.sessionKey}>Session</Text>
        <Text style={[styles.sessionText, styles.muted]}>No bar loaded for {symbol}</Text>
      </View>
    );
  }

  const stamp = newest.ts.endsWith("Z") ? newest.ts : `${newest.ts}Z`;
  const barDate = new Date(stamp);
  const crypto = CRYPTO.has(symbol);
  const end = sessionEnd(barDate, crypto);
  const forming = now < end.getTime();

  return (
    <View style={[styles.session, forming ? styles.sessionForming : styles.sessionClosed]}>
      <Text style={styles.sessionKey}>Session</Text>
      <Text style={styles.sessionDate}>{DAY.format(barDate)}</Text>
      <Text style={[styles.sessionState, forming ? styles.sessionStateForming : styles.sessionStateClosed]}>
        {forming ? "still forming" : "closed"}
      </Text>
      <View style={styles.sessionRule} />
      <Text style={[styles.sessionText, styles.muted]}>
        {forming ? `closes in ${countdown(end, now)}` : `next bar in ${countdown(nextSessionOpen(barDate, crypto), now)}`}
      </Text>
      <Text style={[styles.sessionText, styles.dim]}>
        {crypto ? "24h" : "NSE"} · a decision fills on the next bar's open
      </Text>
    </View>
  );
}