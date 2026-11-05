"""Intraday strategies: same position(history) contract, same-day exits.

The engine's square-off closes anything still open before the session ends, so
these presets only manage entries and rule-based exits inside a session. All
reading happens on completed bars only; the feed withholds the forming one.

Naive-UTC is the working timezone: caches strip tz, NSE bars sit at 03:45-10:00
UTC and crypto is the full UTC day. Everything below uses calendar.session_bounds
so equity and crypto share one code path.
"""

from __future__ import annotations

import pandas as pd

from ..calendar import opening_range_start, session_bounds
from ..indicators import sma


def _session_day(ts: pd.Timestamp, asset_class: str) -> pd.Timestamp:
    """The calendar day whose session owns this naive-UTC bar timestamp."""
    if asset_class != "equity":
        return ts.normalize()
    day = ts.normalize()
    if ts < pd.Timestamp(session_bounds(day.date(), asset_class)[0]):
        day -= pd.Timedelta(days=1)
        # A bar cannot fall on a weekend session; guard anyway for odd feeds.
        while day.weekday() >= 5:
            day -= pd.Timedelta(days=1)
    return day


def _session_window(history: pd.DataFrame, day: pd.Timestamp,
                    asset_class: str) -> pd.DataFrame:
    """History rows inside the session owned by `day`."""
    start = pd.Timestamp(session_bounds(day.date(), asset_class)[0])
    end = pd.Timestamp(session_bounds(day.date(), asset_class)[1])
    return history[(history.index >= start) & (history.index < end)]


class OpeningRangeBreakout:
    """Buy the break above the session's opening range, exit below its low.

    The range is the first `range_minutes` of the session, built exclusively
    from completed bars. An entry decided on bar t fills at bar t+1's open, as
    everywhere else in this codebase. A bar whose own close would define the
    range never acts: acting on it would be reading the range it belongs to.
    """

    # Queued entries re-validate on the newest completed bar before filling.
    fill_revalidate = True

    def __init__(self, range_minutes: int = 15, interval: str = "5m",
                 asset_class: str = "equity", params: dict | None = None):
        self.range_minutes = range_minutes
        self.interval = interval
        self.asset_class = asset_class
        self.name = f"orb-{range_minutes}min"
        self.params = params or {"range_minutes": range_minutes, "interval": interval}
        self._in_position = False
        self._position_day = None

    def _range_for(self, day: pd.Timestamp, session: pd.DataFrame):
        start = pd.Timestamp(opening_range_start(day.date(), self.asset_class))
        offset = session.index - start
        # Bars before the anchor are not part of the range. A continuous session
        # starts at UTC midnight, so an unbounded "before the anchor" test would
        # pull the whole overnight run into an FX or futures range.
        window = session[(offset >= pd.Timedelta(0))
                         & (offset < pd.Timedelta(minutes=self.range_minutes))]
        if len(window) == 0:
            return None
        return float(window["high"].max()), float(window["low"].min())

    def position(self, history: pd.DataFrame) -> float:
        if history is None or len(history) == 0:
            return 0.0
        bar_ts = history.index[-1]
        bar = history.iloc[-1]
        day = _session_day(bar_ts, self.asset_class)
        # A position never survives its session: engine square-off closes it,
        # so state carried across the day boundary would signal a phantom hold.
        if self._in_position and day != self._position_day:
            self._in_position = False
        session = _session_window(history, day, self.asset_class)
        range_bounds = self._range_for(day, session)
        if range_bounds is None:
            return 1.0 if self._in_position else 0.0
        range_high, range_low = range_bounds

        if self._in_position:
            if bar["close"] < range_low:
                self._in_position = False
                return 0.0
            return 1.0

        start = pd.Timestamp(opening_range_start(day.date(), self.asset_class))
        if (bar_ts - start) < pd.Timedelta(minutes=self.range_minutes):
            return 0.0
        if bar["close"] > range_high:
            self._in_position = True
            self._position_day = day
            return 1.0
        return 0.0


class VwapDip:
    """Buy a discount to the day's VWAP, exit back at the VWAP line.

    VWAP is computed on completed bars of the current session from typical
    price (H+L+C)/3 weighted by volume.
    """

    # Queued entries re-validate on the newest completed bar before filling.
    fill_revalidate = True

    def __init__(self, band_pct: float = 0.001, interval: str = "5m",
                 asset_class: str = "equity", params: dict | None = None):
        self.band_pct = band_pct
        self.interval = interval
        self.asset_class = asset_class
        self.name = f"vwap-dip-{band_pct * 100:.1f}%"
        self.params = params or {"band_pct": band_pct, "interval": interval}
        self._in_position = False
        self._position_day = None

    def _vwap(self, session: pd.DataFrame) -> float | None:
        if len(session) == 0:
            return None
        typical = (session["high"] + session["low"] + session["close"]) / 3.0
        volume = session.get("volume")
        if volume is None or float(volume.sum()) <= 0:
            return float(session["close"].iloc[-1])
        return float((typical * volume).sum() / volume.sum())

    def position(self, history: pd.DataFrame) -> float:
        if history is None or len(history) == 0:
            return 0.0
        bar_ts = history.index[-1]
        bar = history.iloc[-1]
        day = _session_day(bar_ts, self.asset_class)
        if self._in_position and day != self._position_day:
            self._in_position = False
        vwap = self._vwap(_session_window(history, day, self.asset_class))
        if vwap is None:
            return 0.0

        if self._in_position:
            if bar["close"] >= vwap:
                self._in_position = False
                return 0.0
            return 1.0

        if bar["close"] <= vwap * (1.0 - self.band_pct):
            self._in_position = True
            self._position_day = day
            return 1.0
        return 0.0


class IntradaySmaCross:
    """The 'graph up, buy' rule at intraday speed, on completed bars."""

    # Queued entries re-validate on the newest completed bar before filling.
    fill_revalidate = True

    def __init__(self, fast: int = 24, slow: int = 72, interval: str = "5m",
                 params: dict | None = None):
        self.fast = fast
        self.slow = slow
        self.name = f"sma-intraday-{fast}/{slow}"
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


def intraday_presets(asset_class: str = "equity") -> list:
    """The three intraday presets plus the buy-and-hold comparison baseline."""
    from .library import BuyAndHold

    return [
        OpeningRangeBreakout(asset_class=asset_class),
        VwapDip(asset_class=asset_class),
        IntradaySmaCross(),
        BuyAndHold(),
    ]
