import { View, Text, ScrollView, RefreshControl, SafeAreaView } from "react-native";
import { useBot } from "../useBot";
import { money, pct } from "../format";
import { colors } from "../theme";

import styles from "../styles";
import { RiskLog } from "../components/RiskLog";
import { controlEnabled } from "../api";

export function RiskPage({ navigation }) {
  const bot = useBot();
  const { status, events, busy } = bot;
  const controlsEnabled = controlEnabled();
  const limits = status?.limits;
  const cap = limits?.max_total_exposure ?? 0;
  const used = status && cap > 0 ? Math.min(100, (status.exposure / cap) * 100) : 0;
  const dayPnl = status?.day_pnl ?? 0;
  const lossLimit = limits?.max_daily_loss ?? 0;
  const lossUsed = lossLimit > 0 ? Math.min(100, (Math.max(0, -dayPnl) / lossLimit) * 100) : 0;

  return (
    <SafeAreaView style={styles.safe}>
      <View style={styles.shell}>
        <ScrollView
          style={styles.content}
          refreshControl={<RefreshControl refreshing={busy} onRefresh={bot.load} />}
        >
          <View style={styles.tape}>
            <View style={styles.tapePrimary}>
              <View style={styles.figure}>
                <Text style={styles.figureKey}>Exposure used</Text>
                <Text style={styles.figureValue}>
                  {status ? money(status.exposure) : "-"} <Text style={styles.figureOf}>of {money(cap)}</Text>
                </Text>
              </View>
              <View style={styles.figure}>
                <Text style={styles.figureKey}>Daily loss used</Text>
                <Text style={[styles.figureValue, dayPnl < 0 ? styles.neg : {}]}>
                  {status ? money(dayPnl) : "-"} <Text style={styles.figureOf}>limit {money(lossLimit)}</Text>
                </Text>
              </View>
              <View style={styles.figure}>
                <Text style={styles.figureKey}>Stop loss</Text>
                <Text style={styles.figureValue}>
                  {limits ? `${(limits.stop_loss_pct * 100).toFixed(0)}%` : "-"} <Text style={styles.figureOf}>per position</Text>
                </Text>
              </View>
            </View>
            <View style={styles.tapeSecondary}>
              <Text><Text style={styles.k}>Max position</Text> {limits ? money(limits.max_position_value) : "-"}</Text>
              <Text><Text style={styles.k}>Max day loss</Text> {limits ? money(limits.max_daily_loss) : "-"}</Text>
            </View>
          </View>

          <View style={styles.panel}>
            <View style={styles.panelHead}>
              <Text style={styles.panelTitle}>Risk events</Text>
            </View>
            <View style={styles.panelBodyFlush}>
              <RiskLog events={events} />
            </View>
          </View>

          <View style={styles.killzone}>
            <View style={styles.killzoneCopy}>
              <Text style={styles.killzoneTitle}>Halt and flatten</Text>
              <Text style={styles.killzoneNote}>
                Stops trading, cancels every queued order, and sells the whole book at the last price.
                Nothing here can be undone from the dashboard.
              </Text>
              {!status?.control_enabled && (
                <Text style={[styles.killzoneNote, styles.dim]}>
                  Disabled. The server was started without STRATBOT_CONTROL_TOKEN, so this
                  fails closed rather than acting without authorisation.
                </Text>
              )}
              {!controlsEnabled && status?.control_enabled && (
                <Text style={[styles.killzoneNote, styles.dim]}>
                  Disabled. This build was built without EXPO_PUBLIC_CONTROL_TOKEN, so
                  operator controls are unavailable in this UI.
                </Text>
              )}
            </View>
            <View style={[styles.btn, styles.btnKill, busy || !status?.control_enabled || !controlsEnabled ? styles.btnDisabled : {}]}>
              <Text style={[styles.btnText, styles.btnKillText]}>Halt and flatten</Text>
            </View>
          </View>
        </ScrollView>

        <View style={styles.tabbar} />
      </View>
    </SafeAreaView>
  );
}