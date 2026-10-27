import { View, Text } from "react-native";
import { styles } from "../styles";

export function HaltBanner({ status }) {
  if (!status?.halted || !status?.halt_reason) return null;

  return (
    <View style={styles.halt}>
      <Text style={styles.haltKey}>Trading halted</Text>
      <Text style={styles.haltReason}>{status.halt_reason.replace(/_/g, " ")}</Text>
      <Text style={styles.haltNote}>
        No new orders will be raised. Queued work stays queued until you resume.
      </Text>
    </View>
  );
}