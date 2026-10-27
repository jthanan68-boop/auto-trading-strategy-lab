import { View, Text } from "react-native";
import { styles } from "../styles";
import { money, qty } from "../format";

export function PositionsTable({ positions }) {
  if (positions.length === 0) {
    return <View style={styles.empty}><Text>No open positions</Text></View>;
  }

  return (
    <View style={styles.table}>
      <View style={styles.tableHeader}>
        <Text style={[styles.th, styles.thLeft]}>Symbol</Text>
        <Text style={styles.th}>Qty</Text>
        <Text style={styles.th}>Avg</Text>
        <Text style={styles.th}>Last</Text>
        <Text style={styles.th}>Value</Text>
        <Text style={styles.th}>P/L</Text>
      </View>
      {positions.map((position) => (
        <View key={position.symbol} style={styles.tableRow}>
          <Text style={[styles.td, styles.tdLeft, styles.sym]}>{position.symbol}</Text>
          <Text style={styles.td}>{qty(position.qty)}</Text>
          <Text style={styles.td}>{position.avg_price.toFixed(2)}</Text>
          <Text style={styles.td}>{position.last_price.toFixed(2)}</Text>
          <Text style={styles.td}>{money(position.market_value)}</Text>
          <Text style={[styles.td, position.unrealised_pnl >= 0 ? styles.pos : styles.neg]}>
            {money(position.unrealised_pnl)} ({position.unrealised_pct.toFixed(2)}%)
          </Text>
        </View>
      ))}
    </View>
  );
}