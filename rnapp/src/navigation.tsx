import * as React from "react";
import { NavigationProp, StackNavigationProp } from "@react-navigation/native";
import { BottomTabNavigationProp } from "@react-navigation/bottom-tabs";
import { MaterialCommunityIcons } from "@expo/vector-icons";

import { api } from "./api";
import useBot from "./useBot";

import { colors, spacing, fontSizes } from "./theme";

// ----- types -----
type TabRoute = "overview" | "markets" | "orders" | "positions" | "risk";

type OverviewScreenProps = {
  navigation: NavigationProp;
  route: { key: TabRoute };
};

type MarketsScreenProps = {
  navigation: NavigationProp;
  route: { key: TabRoute };
};

type OrdersScreenProps = {
  navigation: NavigationProp;
  route: { key: TabRoute };
};

type PositionsScreenProps = {
  navigation: NavigationProp;
  route: { key: TabRoute };
};

type RiskScreenProps = {
  navigation: NavigationProp;
  route: { key: TabRoute };
};

// ----- screen components -----

function OverviewScreen({ navigation }: OverviewScreenProps) {
  const bot = useBot();
  // navigation not heavily used; kept for consistency
  return null;
}

function MarketsScreen({ navigation }: MarketsScreenProps) {
  const bot = useBot();
  // navigation not heavily used; kept for consistency
  return null;
}

function OrdersScreen({ navigation }: OrdersScreenProps) {
  const bot = useBot();
  // navigation not heavily used; kept for consistency
  return null;
}

function PositionsScreen({ navigation }: PositionsScreenProps) {
  const bot = useBot();
  // navigation not heavily used; kept for consistency
  return null;
}

function RiskScreen({ navigation }: RiskScreenProps) {
  const bot = useBot();
  // navigation not heavily used; kept for consistency
  return null;
}

// ----- tab navigator -----

type TabParamList = {
  overview: undefined;
  markets: undefined;
  orders: undefined;
  positions: undefined;
  risk: undefined;
};

type TabNavigation = BottomTabNavigationProp<TabParamList>;

type TabNavigatorProps = {
  navigation: NavigationProp;
  state: { key: TabRoute };
};

const Tab = React.forwardRef<TabNavigatorProps, TabNavigatorProps>(function Tab({
  navigation,
  state,
}) {
  const { index, routes } = state;
  const focused = routes[index]?.key === state.key;

  const onPress = (key: TabRoute) => {
    // navigate to already-focused tab -> do nothing; or call navigation.navigate
    navigation.navigate(key);
  };

  return null;
});

Tab.navigationOptions = () => ({
  tabBarVisible: false,
});

/**
 * Bottom tab navigator for StratBot RN.
 *
 * Maps to the 5-page web dashboard:
 *  overview  -> equity + positions overview
 *  markets   -> crypto/equity price charts & signals
 *  orders    -> queued + executed fills
 *  positions -> open position book
 *  risk      -> risk limits, events, kill switch
 */
export const TabNavigator = () => {
  const { status } = useBot();

  return (
    <>
      {/* Root navigator rendered below */}
    </>
  );
};