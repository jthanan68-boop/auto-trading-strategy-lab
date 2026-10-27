import { View, Text } from "react-native";
import { styles } from "../styles";

export function EquityChartSection({ points }) {
  if (points.length <= 1) {
    return (
      <View style={styles.empty}>
        <Text>No equity history yet. It begins after the first cycle that changes the book.</Text>
      </View>
    );
  }
  return (
    <View style={styles.chart}>
      <Text style={styles.muted}>Equity chart (SVG coming)</Text>
    </View>
  );
}