"""Intraday strategy presets: ORB, VWAP dip, SMA cross.

Synthetic completed 5-minute bars only. These are rule tests, not market
opinions: the presets are unproven and the comparison lab judges them.

All labels are naive UTC: the NSE session runs 03:45-10:00, so the opening
three 5-minute bars are 03:45/03:50/03:55.
"""

from __future__ import annotations

import pandas as pd

from stratlab.strategies.intraday import (
    IntradaySmaCross,
    OpeningRangeBreakout,
    VwapDip,
    _session_day,
)

DAY = "2026-10-14"


def bars(rows: list[tuple[str, float]], day: str = DAY) -> pd.DataFrame:
    """(hh:mm, close) rows -> completed 5-min history for one session."""
    index = pd.DatetimeIndex([pd.Timestamp(f"{day}T{hhmm}") for hhmm, _ in rows])
    closes = [price for _, price in rows]
    return pd.DataFrame(
        {"open": closes, "high": [c + 0.2 for c in closes],
         "low": [c - 0.2 for c in closes], "close": closes, "volume": 1000.0},
        index=index,
    )


def utc_bar_index(n: int, start: str = "03:45") -> pd.DatetimeIndex:
    return pd.date_range(f"{DAY}T{start}", periods=n, freq="5min")


def closes_frame(index: pd.DatetimeIndex, closes: pd.Series) -> pd.DataFrame:
    closes = closes.to_numpy()  # avoid index alignment against the bar index
    return pd.DataFrame(
        {"open": closes, "high": closes + 0.2, "low": closes - 0.2,
         "close": closes, "volume": 1000.0},
        index=index,
    )


OPENING = [("03:45", 100.0), ("03:50", 101.0), ("03:55", 100.5)]


class TestOpeningRangeBreakout:
    def test_no_action_inside_the_range_window(self):
        strat = OpeningRangeBreakout(range_minutes=15)
        assert strat.position(bars(OPENING)) == 0.0

    def test_buys_above_the_range_after_it_completes(self):
        strat = OpeningRangeBreakout(range_minutes=15)
        strat.position(bars(OPENING))
        strat._in_position = False
        assert strat.position(bars(OPENING + [("04:00", 101.5)])) == 1.0

    def test_stays_out_below_the_range_high(self):
        strat = OpeningRangeBreakout(range_minutes=15)
        strat.position(bars(OPENING))
        strat._in_position = False
        assert strat.position(bars(OPENING + [("04:00", 100.8)])) == 0.0

    def test_exits_below_the_range_low(self):
        strat = OpeningRangeBreakout(range_minutes=15)
        strat._in_position = True
        strat._position_day = pd.Timestamp(DAY)
        below = bars(OPENING + [("04:00", 101.5), ("04:05", 99.5)])
        assert strat.position(below) == 0.0

    def test_state_resets_across_sessions(self):
        strat = OpeningRangeBreakout(range_minutes=15)
        strat._in_position = True
        strat._position_day = pd.Timestamp("2026-10-13")
        assert strat.position(bars(OPENING, day="2026-10-14")) == 0.0
        assert strat._in_position is False

    def test_session_day_maps_pre_open_bars_back(self):
        assert _session_day(pd.Timestamp(f"{DAY}T03:40"), "equity") \
            == pd.Timestamp("2026-10-13")
        assert _session_day(pd.Timestamp(f"{DAY}T04:00"), "equity") \
            == pd.Timestamp(DAY)
        assert _session_day(pd.Timestamp(f"{DAY}T05:00"), "crypto") \
            == pd.Timestamp(DAY)


class TestVwapDip:
    def test_buys_a_dip_below_vwap(self):
        strat = VwapDip(band_pct=0.001)
        rows = [("03:45", 110.0), ("03:50", 109.0), ("03:55", 108.0),
                ("04:00", 103.0)]
        assert strat.position(bars(rows)) == 1.0

    def test_stays_out_near_vwap(self):
        strat = VwapDip(band_pct=0.001)
        rows = [("03:45", 110.0), ("03:50", 109.0), ("03:55", 108.0),
                ("04:00", 109.0)]
        assert strat.position(bars(rows)) == 0.0

    def test_exits_at_the_vwap_line(self):
        strat = VwapDip(band_pct=0.001)
        strat._in_position = True
        strat._position_day = pd.Timestamp(DAY)
        rows = [("03:45", 110.0), ("03:50", 103.0), ("03:55", 103.5),
                ("04:00", 109.0)]
        assert strat.position(bars(rows)) == 0.0

    def test_holds_while_below_the_line(self):
        strat = VwapDip(band_pct=0.001)
        strat._in_position = True
        strat._position_day = pd.Timestamp(DAY)
        rows = [("03:45", 110.0), ("03:50", 103.0), ("03:55", 104.0),
                ("04:00", 104.5)]
        assert strat.position(bars(rows)) == 1.0


class TestIntradaySmaCross:
    def test_rising_series_wins(self):
        strat = IntradaySmaCross(fast=6, slow=20)
        n = 40
        closes = 100.0 + pd.Series(range(n), dtype=float).mul(0.5)
        history = closes_frame(utc_bar_index(n), closes)
        assert strat.position(history) == 1.0

    def test_short_history_stays_out(self):
        strat = IntradaySmaCross(fast=6, slow=72)
        closes = 100.0 + pd.Series(range(20), dtype=float).mul(0.5)
        history = closes_frame(utc_bar_index(20), closes)
        assert strat.position(history) == 0.0

    def test_falling_series_stays_out(self):
        strat = IntradaySmaCross(fast=6, slow=20)
        n = 40
        closes = 140.0 - pd.Series(range(n), dtype=float).mul(0.5)
        history = closes_frame(utc_bar_index(n), closes)
        assert strat.position(history) == 0.0


class TestNoLookahead:
    def test_presets_only_see_completed_bars(self):
        """Stateful presets behave identically on the same completed prefix."""
        a = OpeningRangeBreakout(range_minutes=15)
        b = OpeningRangeBreakout(range_minutes=15)
        base = bars(OPENING + [("04:00", 101.5), ("04:05", 100.9)])
        assert a.position(base) == b.position(base)

    def test_range_ignores_future_highs(self):
        strat = OpeningRangeBreakout(range_minutes=15)
        strat.position(bars(OPENING))
        strat._in_position = False
        # A later high belongs to a later bar, never to the completed range.
        assert strat.position(bars(OPENING + [("04:00", 100.8)])) == 0.0
