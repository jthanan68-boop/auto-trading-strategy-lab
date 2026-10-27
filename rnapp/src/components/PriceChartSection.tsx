import { View, Text } from "react-native";
import { styles } from "../styles";
import { money, qty } from "../format";

export function PriceChartSection({ bars, symbol }) {
  if (!bars || bars.length === 0) {
    return <View style={styles.empty}><Text>No cached price history for {symbol}</Text></View>;
  }
  return (
    <View style={styles.chart}>
      <Text style={styles.muted}>Price chart (SVG coming)</Text>
    </View>
  );
}