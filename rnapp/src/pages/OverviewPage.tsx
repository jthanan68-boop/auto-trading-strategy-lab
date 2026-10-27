import { View, Text, ScrollView, RefreshControl, SafeAreaView } from "react-native";
import { useBot } from "../useBot";
import { money, signed, shortDate, pct } from "../format";
import { colors } from "../theme";

import styles from "../styles";
import { Tape } from "../components/Tape";
import { PendingSection } from "../components/PendingSection";
import { EquityChartSection } from "../components/EquityChartSection";
import { Controls } from "../components/Controls";
import { ErrorBanner } from "../components/ErrorBanner";
import { HaltBanner } from "../components/HaltBanner";
import { SessionBar } from "../components/SessionBar";
import { PriceChartSection } from "../components/PriceChartSection";
import { PositionsTable } from "../components/PositionsTable";
import { FillsTable } from "../components/FillsTable";
import { RiskLog } from "../components/RiskLog";
import { controlEnabled } from "../api";

export function OverviewPage({ navigation }) {
  const bot = useBot();
  const controlsEnabled = controlEnabled();
  const activeBars = bot.bars[bot.symbol] ?? [];

  return (
    <SafeAreaView style={styles.safe}>
      <View style={styles.shell}>
        <ScrollView
          style={styles.content}
          refreshControl={<RefreshControl refreshing={bot.busy} onRefresh={bot.load} />}
        >
          <ErrorBanner error={bot.error} />
          <HaltBanner status={bot.status} />

          <SessionBar bars={activeBars} symbol={bot.symbol} />

          <Tape bot={bot} />

          <View style={styles.panel}>
            <View style={styles.panelHead}>
              <Text style={styles.panelTitle}>Waiting</Text>
              {bot.pending.length > 0 && (
                <Text style={styles.panelAside}>nothing fills until the next daily bar publishes</Text>
              )}
            </View>
            <View style={styles.panelBody}>
              <PendingSection bot={bot} />
            </View>
          </View>

          <View style={styles.panel}>
            <View style={styles.panelHead}>
              <Text style={styles.panelTitle}>Equity</Text>
            </View>
            <View style={styles.panelBody}>
              <EquityChartSection points={bot.equity} />
            </View>
          </View>

          <View style={styles.panel}>
            <Controls bot={bot} controlsEnabled={controlsEnabled} />
          </View>
        </ScrollView>

        <View style={styles.tabbar} />
      </View>
    </SafeAreaView>
  );
}