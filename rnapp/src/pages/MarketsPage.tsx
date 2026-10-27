import { View, Text, ScrollView, RefreshControl, SafeAreaView } from "react-native";
import { useBot } from "../useBot";
import { money, pct } from "../format";
import { colors } from "../theme";

import styles from "../styles";
import { SessionBar } from "../components/SessionBar";
import { PriceChartSection } from "../components/PriceChartSection";

function quote(bot, name) {
  const series = bot.bars[name];
  const live = bot.quotes[name];
  const last = series?.at(-1);
  const prev = series?.at(-2);
  const price = live?.usable ? live.price : last?.close;
  if (price === undefined) return null;
  const basis = prev?.close ?? price;
  return {
    price,
    change: basis === 0 ? 0 : ((price - basis) / basis) * 100,
    live: live?.usable ?? false,
    source: live?.source,
    age: live?.age_seconds,
    delayed: live?.delayed ?? false,
  };
}

function age(seconds) {
  if (seconds === undefined) return "";
  if (seconds < 90) return `${Math.round(seconds)}s old`;
  return `${Math.round(seconds / 60)}m old`;
}

export function MarketsPage({ navigation }) {
  const bot = useBot();
  const active = bot.bars[bot.symbol] ?? [];
  const shown = quote(bot, bot.symbol);

  return (
    <SafeAreaView style={styles.safe}>
      <View style={styles.shell}>
        <ScrollView
          style={styles.content}
          refreshControl={<RefreshControl refreshing={bot.busy} onRefresh={bot.load} />}
        >
          <SessionBar bars={active} symbol={bot.symbol} />

          <View style={styles.rail}>
            {(bot.status?.symbols ?? []).map((name) => {
              const q = quote(bot, name);
              const stale = bot.barsState[name] !== "ready";
              return (
                <View
                  key={name}
                  style={[styles.railItem, name === bot.symbol ? styles.railItemActive : {}]}
                >
                  <View style={styles.railSym}>
                    <Text style={styles.railSym}>{name}</Text>
                    {q?.live && <View style={styles.railDot} />}
                  </View>
                  {q ? (
                    <View style={{ alignItems: "flex-end" }}>
                      <Text style={styles.railPrice}>{money(q.price)}</Text>
                      <Text style={[styles.railChange, q.change >= 0 ? styles.railChangePos : styles.railChangeNeg]}>
                        {pct(q.change)}
                      </Text>
                    </View>
                  ) : (
                    <Text style={[styles.railPrice, styles.dim]}>{stale ? "loading" : "-"}</Text>
                  )}
                </View>
              );
            })}
          </View>

          <View style={styles.panel}>
            <View style={styles.panelHead}>
              <Text style={styles.panelTitle}>{bot.symbol}</Text>
              {shown && (
                <Text style={[styles.panelAside, styles.mono]}>
                  {money(shown.price)} <Text style={shown.change >= 0 ? styles.pos : styles.neg}>{pct(shown.change)}</Text> <Text style={styles.dim}>vs prior close</Text>
                </Text>
              )}
            </View>
            <View style={styles.panelBody}>
              {bot.barsState[bot.symbol] === "ready" && active.length ? (
                <PriceChartSection bars={active} symbol={bot.symbol} />
              ) : bot.barsState[bot.symbol] === "error" ? (
                <View style={styles.empty}>
                  <Text>No cached price history for {bot.symbol}. Run the strategy lab once to populate the cache.</Text>
                </View>
              ) : (
                <View style={styles.empty}><Text>Loading {bot.symbol} price history</Text></View>
              )}
            </View>
            <Text style={styles.panelFoot}>
              {shown?.live
                ? `Live price from ${shown.source}, ${age(shown.age)}${shown.delayed ? " (delayed by the venue)" : ""}. `
                : "No live quote; showing the newest daily close. "}
              Daily candles from Yahoo Finance. The newest candle is the current session and is still
              forming until the market closes.
            </Text>
          </View>
        </ScrollView>

        <View style={styles.tabbar} />
      </View>
    </SafeAreaView>
  );
}