import { View, Text, ScrollView, RefreshControl, SafeAreaView } from "react-native";
import { useBot } from "../useBot";
import { money, signed } from "../format";
import { colors } from "../theme";

import styles from "../styles";
import { PositionsTable } from "../components/PositionsTable";

export function PositionsPage({ navigation }) {
  const bot = useBot();
  const { positions, status } = bot;
  const unrealised = status?.unrealised ?? 0;

  return (
    <SafeAreaView style={styles.safe}>
      <View style={styles.shell}>
        <ScrollView
          style={styles.content}
          refreshControl={<RefreshControl refreshing={bot.busy} onRefresh={bot.load} />}
        >
          <View style={styles.tape}>
            <View style={styles.tapePrimary}>
              <View style={styles.figure}>
                <Text style={styles.figureKey}>Cash</Text>
                <Text style={styles.figureValue}>{status ? money(status.cash) : "-"}</Text>
              </View>
              <View style={styles.figure}>
                <Text style={styles.figureKey}>Exposure</Text>
                <Text style={styles.figureValue}>{status ? money(status.exposure) : "-"}</Text>
              </View>
              <View style={styles.figure}>
                <Text style={styles.figureKey}>Unrealised</Text>
                <Text style={[styles.figureValue, status && (unrealised >= 0 ? styles.pos : styles.neg)]}>
                  {status ? signed(unrealised) : "-"}
                </Text>
              </View>
            </View>
            <View style={styles.tapeSecondary}>
              <Text><Text style={styles.k}>Open</Text> {positions.length}</Text>
              <Text><Text style={styles.k}>Equity</Text> {status ? money(status.equity) : "-"}</Text>
            </View>
          </View>

          <View style={styles.panel}>
            <View style={styles.panelHead}>
              <Text style={styles.panelTitle}>Open positions</Text>
            </View>
            <View style={styles.panelBodyFlush}>
              <PositionsTable positions={positions} />
            </View>
          </View>

          {positions.length === 0 && (
            <View style={styles.notePanel}>
              <Text style={styles.noteTitle}>Nothing on the book</Text>
              <Text style={styles.noteBody}>
                No symbol is held. A position only opens when a queued order reaches its execution bar,
                so a quiet book is normal between sessions rather than a fault.
              </Text>
            </View>
          )}
        </ScrollView>

        <View style={styles.tabbar} />
      </View>
    </SafeAreaView>
  );
}