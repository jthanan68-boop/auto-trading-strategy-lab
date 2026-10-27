import * as React from "react";
import { NavigationContainer } from "@react-navigation/native";
import { createBottomTabNavigator } from "@react-navigation/bottom-tabs";
import { StyleSheet, Text, View } from "react-native";

import { OverviewPage } from "./src/pages/OverviewPage";
import { MarketsPage } from "./src/pages/MarketsPage";
import { OrdersPage } from "./src/pages/OrdersPage";
import { PositionsPage } from "./src/pages/PositionsPage";
import { RiskPage } from "./src/pages/RiskPage";
import { colors } from "./src/theme";

const Tab = createBottomTabNavigator();

function TabIcon({ name, focused }: { name: string; focused: boolean }) {
  const glyphs: Record<string, string> = {
    overview: "01",
    markets: "02",
    orders: "03",
    positions: "04",
    risk: "05",
  };
  return (
    <View style={styles.tab}>
      <Text style={[styles.tabGlyph, focused && styles.tabGlyphActive]}>{glyphs[name] || "?"}</Text>
      <Text style={[styles.tabLabel, focused && styles.tabLabelActive]}>{name}</Text>
    </View>
  );
}

export default function App() {
  return (
    <NavigationContainer>
      <Tab.Navigator
        screenOptions={({ route }) => ({
          tabBarShowLabel: false,
          tabBarActiveTintColor: colors.brass,
          tabBarInactiveTintColor: colors.muted,
          tabBarStyle: {
            backgroundColor: colors.surface,
            borderTopWidth: 1,
            borderTopColor: colors.border,
            paddingBottom: 5,
            height: 60,
          },
        })}
      >
        <Tab.Screen
          name="overview"
          component={OverviewPage}
          options={{ tabBarIcon: () => <TabIcon name="overview" focused={true} /> }}
        />
        <Tab.Screen
          name="markets"
          component={MarketsPage}
          options={{ tabBarIcon: () => <TabIcon name="markets" focused={false} /> }}
        />
        <Tab.Screen
          name="orders"
          component={OrdersPage}
          options={{ tabBarIcon: () => <TabIcon name="orders" focused={false} /> }}
        />
        <Tab.Screen
          name="positions"
          component={PositionsPage}
          options={{ tabBarIcon: () => <TabIcon name="positions" focused={false} /> }}
        />
        <Tab.Screen
          name="risk"
          component={RiskPage}
          options={{ tabBarIcon: () => <TabIcon name="risk" focused={false} /> }}
        />
      </Tab.Navigator>
    </NavigationContainer>
  );
}

const styles = StyleSheet.create({
  tab: { flexDirection: "column", alignItems: "center", gap: 2 },
  tabGlyph: { fontSize: 9, opacity: 0.7, fontFamily: "monospace", color: colors.muted },
  tabGlyphActive: { color: colors.brass },
  tabLabel: { fontSize: 10, color: colors.muted },
  tabLabelActive: { color: colors.brass },
});