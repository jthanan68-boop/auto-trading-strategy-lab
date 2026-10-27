import { View, Text } from "react-native";
import { styles } from "../styles";
import { money, qty, shortDate } from "../format";

export function FillsTable({ fills }) {
  if (fills.length === 0) {
    return <View style={styles.empty}><Text>No fills yet</Text></View>;
  }

  return (
    <View style={styles.table}>
      <View style={styles.tableHeader}>
        <Text style={[styles.th, styles.thLeft]}>Time</Text>
        <Text style={[styles.th, styles.thLeft]}>Symbol</Text>
        <Text style={styles.th}>Side</Text>
        <Text style={styles.th}>Qty</Text>
        <Text style={styles.th}>Price</Text>
        <Text style={styles.th}>Fees</Text>
      </View>
      {fills.map((fill) => (
        <View key={fill.id} style={styles.tableRow}>
          <Text style={[styles.td, styles.tdLeft]}>{shortDate(fill.ts)}</Text>
          <Text style={[styles.td, styles.tdLeft, styles.sym]}>{fill.symbol}</Text>
          <Text style={[styles.td, fill.side === "BUY" ? styles.sideBuy : styles.sideSell]}>{fill.side}</Text>
          <Text style={styles.td}>{qty(fill.qty)}</Text>
          <Text style={styles.td}>{fill.price.toFixed(2)}</Text>
          <Text style={styles.td}>{fill.fees.toFixed(2)}</Text>
        </View>
      ))}
    </View>
  );
}