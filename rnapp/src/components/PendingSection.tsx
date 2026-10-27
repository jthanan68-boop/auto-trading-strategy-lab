import { View, Text } from "react-native";
import { styles } from "../styles";
import { money, qty, shortDate } from "../format";
import { colors } from "../theme";

export function PendingSection({ bot }) {
  const { pending } = bot;
  if (pending.length === 0) {
    return (
      <View style={styles.waitingQuiet}>
        <Text style={styles.waitingTitle}>Nothing waiting</Text>
        <Text style={styles.waitingNote}>No queued orders. The bot holds no unexecuted intent.</Text>
      </View>
    );
  }

  return (
    <View style={styles.waiting}>
      <View style={styles.waitingHead}>
        <Text style={styles.waitingCount}>{pending.length}</Text>
        <Text style={styles.waitingTitle}>
          order{pending.length === 1 ? "" : "s"} decided, waiting for the next bar to open
        </Text>
      </View>
      <View style={styles.waitingRows}>
        {pending.map((order) => (
          <View key={order.client_order_id} style={styles.waitingRow}>
            <Text style={styles.waitingSym}>{order.symbol}</Text>
            <Text style={[styles.side, order.side === "BUY" ? styles.sideBuy : styles.sideSell]}>{order.side}</Text>
            <Text style={styles.waitingQty}>{qty(order.qty)}</Text>
            <Text style={styles.waitingArrow}>
              decided {shortDate(order.signal_bar_ts)}
              <Text style={styles.waitingDim}> → fills at the next open</Text>
            </Text>
            {order.execution_bar_ts && (
              <Text style={styles.waitingDim}>filled on {shortDate(order.execution_bar_ts)}</Text>
            )}
          </View>
        ))}
      </View>
    </View>
  );
}