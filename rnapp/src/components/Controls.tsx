import { View, Text } from "react-native";
import { styles } from "../styles";
import { money, signed } from "../format";

export function Controls({ bot, controlsEnabled }) {
  const halted = bot.status?.halted ?? false;
  return (
    <View style={styles.controls}>
      <View style={styles.controlsCopy}>
        <Text style={styles.controlsTitle}>Run a cycle</Text>
        <Text style={styles.controlsNote}>
          Reads the newest bar, asks the strategy, and queues anything new. It never fills on
          the bar that produced the decision.
        </Text>
      </View>
      <View style={styles.controlsActions}>
        <View style={[styles.btn, bot.busy || halted || !bot.status?.control_enabled || !controlsEnabled ? styles.btnDisabled : styles.btnPrimary]}>
          <Text style={[styles.btnText, bot.status?.control_enabled && controlsEnabled ? styles.btnPrimaryText : styles.btnText]}>
            Run one cycle
          </Text>
        </View>
        <View style={[styles.btn, bot.busy || !halted || !bot.status?.control_enabled || !controlsEnabled ? styles.btnDisabled : styles.btnPrimary]}>
          <Text style={[styles.btnText, bot.status?.control_enabled && controlsEnabled ? styles.btnPrimaryText : styles.btnText]}>
            Resume trading
          </Text>
        </View>
      </View>
    </View>
  );
}