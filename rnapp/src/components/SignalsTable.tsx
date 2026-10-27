import { View, Text } from "react-native";
import { styles } from "../styles";
import { shortDate, money } from "../format";

export function SignalsTable({ signals }) {
  if (signals.length === 0) {
    return <View style={styles.empty}><Text>No signals yet</Text></View>;
  }

  return (
    <View style={styles.table}>
      <View style={styles.tableHeader}>
        <Text style={[styles.th, styles.thLeft]}>Time</Text>
        <Text style={[styles.th, styles.thLeft]}>Symbol</Text>
        <Text style={styles.th}>Side</Text>
        <Text style={styles.th}>Price</Text>
        <Text style={styles.th}>Weight</Text>
        <Text style={[styles.th, styles.thLeft]}>Reason</Text>
      </View>
      {signals.map((signal) => (
        <View key={signal.id} style={styles.tableRow}>
          <Text style={[styles.td, styles.tdLeft]}>{shortDate(signal.ts)}</Text>
          <Text style={[styles.td, styles.tdLeft, styles.sym]}>{signal.symbol}</Text>
          <Text style={[styles.td, signal.action === "BUY" ? styles.sideBuy : styles.sideSell]}>{signal.action}</Text>
          <Text style={styles.td}>{signal.price.toFixed(2)}</Text>
          <Text style={styles.td}>{signal.weight.toFixed(2)}</Text>
          <Text style={[styles.td, styles.tdLeft, styles.reason]}>{signal.reason}</Text>
        </View>
      ))}
    </View>
  );
}