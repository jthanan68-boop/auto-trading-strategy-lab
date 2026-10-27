import { View, Text } from "react-native";
import { styles } from "../styles";
import { shortDate } from "../format";

export function RiskLog({ events }) {
  if (events.length === 0) {
    return <View style={styles.empty}><Text>No risk events recorded</Text></View>;
  }

  return (
    <View style={styles.feed}>
      {events.map((event) => (
        <View key={event.id} style={styles.event}>
          <Text style={styles.eventTime}>{shortDate(event.ts)}</Text>
          <Text style={[styles.eventKind, { color: tone(event.kind) }]}>{event.kind}</Text>
          <Text style={styles.eventDetail}>{event.detail}</Text>
        </View>
      ))}
    </View>
  );
}

function tone(kind) {
  if (kind.includes("halt") || kind.includes("loss") || kind.includes("mismatch") || kind.includes("down")) {
    return "#e2574c";
  }
  if (kind.includes("stop_loss") || kind.includes("rejected") || kind.includes("cap")) return "#d9a441";
  if (kind.includes("resumed") || kind.includes("start")) return "#3fb98c";
  return "#8c9c97";
}