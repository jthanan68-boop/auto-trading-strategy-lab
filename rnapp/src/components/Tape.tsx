import { View, Text, StyleSheet } from "react-native";
import { styles } from "../styles";
import { money, signed } from "../format";
import { colors } from "../theme";

export function Tape({ bot }) {
  const { status, pending } = bot;
  const halted = status?.halted ?? false;
  const dayPnl = status?.day_pnl ?? 0;
  const cap = status?.limits.max_total_exposure ?? 0;
  const used = status && cap > 0 ? Math.min(100, (status.exposure / cap) * 100) : 0;

  return (
    <View style={styles.tape}>
      <View style={styles.tapePrimary}>
        <View style={styles.figure}>
          <Text style={styles.figureKey}>Equity</Text>
          <Text style={styles.figureValue}>{status ? money(status.equity) : "-"}</Text>
        </View>
        <View style={styles.figure}>
          <Text style={styles.figureKey}>Exposure</Text>
          <Text style={styles.figureValue}>
            {status ? money(status.exposure) : "-"} <Text style={styles.figureOf}>of {money(cap)}</Text>
          </Text>
        </View>
        <View style={styles.figure}>
          <Text style={styles.figureKey}>Day P/L</Text>
          <Text style={[styles.figureValue, status && (dayPnl >= 0 ? styles.pos : styles.neg)]}>
            {status ? signed(dayPnl) : "-"}
          </Text>
        </View>
      </View>
      <View style={styles.tapeSecondary}>
        <Text><Text style={styles.k}>Cash</Text> {status ? money(status.cash) : "-"}</Text>
        <Text><Text style={styles.k}>Unrealised</Text> <Text style={status && (status.unrealised >= 0 ? styles.pos : styles.neg)}>{status ? signed(status.unrealised) : "-"}</Text></Text>
        <Text><Text style={styles.k}>Queued</Text> {pending.length}</Text>
        <Text><Text style={styles.k}>Fills</Text> {bot.fills.length}</Text>
      </View>
    </View>
  );
}