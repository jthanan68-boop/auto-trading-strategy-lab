"""Long-only strategies expressed as target position weights."""

from __future__ import annotations

import pandas as pd

from ..indicators import rsi, sma


class BuyAndHold:
    name = "buy-and-hold"

    def __init__(self, params: dict | None = None):
        self.params = params or {}

    def position(self, history: pd.DataFrame) -> float:
        return 1.0


class SmaCrossover:
    """Long when the fast SMA is above the slow SMA.

    This is the literal 'graph goes up, invest' rule, included as the baseline
    the other strategies have to beat.
    """

    def __init__(self, fast: int = 20, slow: int = 100, params: dict | None = None):
        self.fast = fast
        self.slow = slow
        self.name = f"sma-{fast}/{slow}"
        self.params = params or {"fast": fast, "slow": slow}

    def position(self, history: pd.DataFrame) -> float:
        close = history["close"]
        if len(close) < self.slow + 1:
            return 0.0
        fast = sma(close, self.fast)
        slow = sma(close, self.slow)
        if pd.isna(slow.iloc[-1]) or pd.isna(fast.iloc[-1]):
            return 0.0
        return 1.0 if fast.iloc[-1] > slow.iloc[-1] else 0.0


class RsiStrategy:
    """Mean reversion: buy oversold, exit overbought."""

    def __init__(
        self,
        period: int = 14,
        buy_below: float = 30.0,
        sell_above: float = 70.0,
        params: dict | None = None,
    ):
        self.period = period
        self.buy_below = buy_below
        self.sell_above = sell_above
        self.name = f"rsi-{period}-{int(buy_below)}/{int(sell_above)}"
        self.params = params or {
            "period": period,
            "buy_below": buy_below,
            "sell_above": sell_above,
        }

    def position(self, history: pd.DataFrame) -> float:
        value = rsi(history["close"], self.period).iloc[-1]
        if pd.isna(value):
            return 0.0
        if value < self.buy_below:
            return 1.0
        if value > self.sell_above:
            return 0.0
        return float("nan")  # signal: keep whatever position we already hold


class DonchianBreakout:
    """Buy an N-day high, exit on an M-day low.

    This is trend following done properly: it buys continuation, and it holds
    through noise instead of reacting to every wiggle.

    A series shorter than the entry window has no channel to break, so the
    previous close stands in for it.
    """

    def __init__(self, entry: int = 55, exit: int = 20, params: dict | None = None):
        self.entry = entry
        self.exit = exit
        self.name = f"donchian-{entry}/{exit}"
        self.params = params or {"entry": entry, "exit": exit}
        self._in_position = False

    def position(self, history: pd.DataFrame) -> float:
        close = history["close"]
        if len(close) < 2:
            return 0.0
        if len(close) < self.entry + 1:
            # Too short for the channel, so the previous close is the trigger.
            last = close.iloc[-1]
            if last >= close.iloc[-2]:
                self._in_position = True
                return 1.0
            self._in_position = False
            return 0.0
        prior = close.iloc[:-1]
        entry_high = prior.iloc[-self.entry :].max()
        exit_low = prior.iloc[-self.exit :].min()
        last = close.iloc[-1]

        if self._in_position:
            if last <= exit_low:
                self._in_position = False
                return 0.0
            return 1.0

        if last >= entry_high:
            self._in_position = True
            return 1.0
        return 0.0


class BuyDipHold:
    """Enter once on a pullback, then hold until the end of the sample."""

    def __init__(self, lookback: int = 60, drop_pct: float = 0.12, params: dict | None = None):
        self.lookback = lookback
        self.drop_pct = drop_pct
        self.name = f"buy-dip-{lookback}-{int(drop_pct * 100)}pct"
        self.params = params or {"lookback": lookback, "drop_pct": drop_pct}
        self._entered = False

    def position(self, history: pd.DataFrame) -> float:
        if self._entered:
            return 1.0
        close = history["close"]
        if len(close) < self.lookback + 1:
            return 0.0
        window = close.iloc[-self.lookback :]
        recent = window.iloc[-5:]
        peak = window.max()
        if peak > 0 and (peak - recent.min()) / peak >= self.drop_pct:
            self._entered = True
            return 1.0
        return 0.0