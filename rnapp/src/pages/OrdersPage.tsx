import { View, Text, ScrollView, RefreshControl, SafeAreaView } from "react-native";
import { useBot } from "../useBot";
import { colors } from "../theme";

import styles from "../styles";
import { PendingSection } from "../components/PendingSection";
import { FillsTable } from "../components/FillsTable";

export function OrdersPage({ navigation }) {
  const bot = useBot();
  const executed = bot.fills.length > 0;

  return (
    <SafeAreaView style={styles.safe}>
      <View style={styles.shell}>
        <ScrollView
          style={styles.content}
          refreshControl={<RefreshControl refreshing={bot.busy} onRefresh={bot.load} />}
        >
          <View style={styles.panel}>
            <View style={styles.panelHead}>
              <Text style={styles.panelTitle}>Queued</Text>
              <Text style={styles.panelAside}>decided on one bar, executed on the next</Text>
            </View>
            <View style={styles.panelBody}>
              <PendingSection bot={bot} />
            </View>
          </View>

          <View style={styles.panel}>
            <View style={styles.panelHead}>
              <Text style={styles.panelTitle}>Executed</Text>
              <Text style={styles.panelAside}>
                {executed ? "filled at the opening price of the bar after the decision" : "no fills recorded"}
              </Text>
            </View>
            <View style={styles.panelBodyFlush}>
              <FillsTable fills={bot.fills} />
            </View>
          </View>

          <View style={styles.notePanel}>
            <Text style={styles.noteTitle}>How an order travels</Text>
            <View style={styles.flow}>
              <View>
                <Text style={styles.flowStep}>decide</Text>
                <Text style={[styles.noteBody, styles.muted]}>The strategy reads the newest closed bar and produces a target weight.</Text>
              </View>
              <View>
                <Text style={styles.flowStep}>queue</Text>
                <Text style={[styles.noteBody, styles.muted]}>The order is written to the ledger as pending, tagged with that bar.</Text>
              </View>
              <View>
                <Text style={styles.flowStep}>execute</Text>
                <Text style={[styles.noteBody, styles.muted]}>It fills only when a strictly newer bar exists, priced at that bar's open.</Text>
              </View>
              <View>
                <Text style={styles.flowStep}>re-check</Text>
                <Text style={[styles.noteBody, styles.muted]}>Limits and quantity are re-evaluated at the execution price, not the decision price.</Text>
              </View>
            </View>
          </View>
        </ScrollView>

        <View style={styles.tabbar} />
      </View>
    </SafeAreaView>
  );
}