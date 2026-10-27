import { View, Text } from "react-native";
import { styles } from "../styles";

export function ErrorBanner({ error }) {
  if (!error) return null;
  return (
    <View style={styles.error}>
      <Text style={styles.mono}>{error}</Text>
    </View>
  );
}