"""Continuous-asset extensions: round-the-clock universe and the open it trades.

Crypto was the only class with no session and no weekend. Adding majors to it is
purely data, but moving the FX and futures opening range off the UTC rollover is
a strategy change, and these tests pin down that it stays anchored at the London
or New York open and that pre-anchor bars never leak into the range.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from stratbot.broker.paper import PaperBroker
from stratbot.portfolio import SECTOR_MAP
from stratlab import data
from stratlab.calendar import (
    opening_range_start,
    session_bounds,
)
from stratlab.costs import FX_SPOT, US_FUTURES, cost_model_for
from stratlab.strategies.intraday import OpeningRangeBreakout

NEW_CRYPTO = ["SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC", "BCH", "TRX"]
DAY = date(2026, 11, 4)


class TestOpeningRangeAnchor:
    def test_equity_keeps_the_session_open(self):
        assert opening_range_start(DAY, "equity") == session_bounds(DAY, "equity")[0]

    def test_crypto_keeps_the_utc_day_open(self):
        assert opening_range_start(DAY, "crypto") == datetime(2026, 11, 4, 0, 0)

    def test_fx_is_anchored_at_the_london_open(self):
        assert opening_range_start(DAY, "fx") == datetime(2026, 11, 4, 8, 0)

    @pytest.mark.parametrize("asset_class", ["commodity", "metal"])
    def test_futures_are_anchored_at_the_new_york_open(self, asset_class):
        assert opening_range_start(DAY, asset_class) == datetime(2026, 11, 4, 14, 0)

    def test_a_weekend_has_no_window_to_anchor_into(self):
        saturday = date(2026, 11, 7)
        start, end = session_bounds(saturday, "fx")
        assert end == start
        assert opening_range_start(saturday, "fx") == start


def frame(day: str, rows: list[tuple[str, float]]) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(f"{day}T{hhmm}") for hhmm, _ in rows])
    closes = [price for _, price in rows]
    return pd.DataFrame(
        {"open": closes, "high": [c + 0.5 for c in closes],
         "low": [c - 0.5 for c in closes], "close": closes, "volume": 1000.0},
        index=index,
    )


class TestOpeningRangeUsesTheAnchor:
    def test_overnight_bars_do_not_leak_into_an_fx_range(self):
        strat = OpeningRangeBreakout(range_minutes=15, asset_class="fx")
        # 00:00-07:55 prints a huge high; the range must come from 08:00-08:15.
        pre_anchor = [(f"{h:02d}:{m:02d}", 100.0) for h in range(0, 8) for m in (0, 30)]
        window = [("08:00", 100.0), ("08:05", 101.0), ("08:10", 100.5)]
        session = frame("2026-11-04", pre_anchor + window)
        bounds = strat._range_for(pd.Timestamp("2026-11-04"), session)
        assert bounds == (101.5, 99.5)

    def test_a_futures_range_ignores_the_morning_session(self):
        strat = OpeningRangeBreakout(range_minutes=15, asset_class="commodity")
        morning = [("09:00", 90.0), ("13:55", 91.0)]
        window = [("14:00", 100.0), ("14:05", 100.0), ("14:10", 100.0)]
        session = frame("2026-11-04", morning + window)
        bounds = strat._range_for(pd.Timestamp("2026-11-04"), session)
        assert bounds == (100.5, 99.5)

    def test_no_entry_before_the_anchor(self):
        strat = OpeningRangeBreakout(range_minutes=15, asset_class="fx")
        # A break at 07:30 is outside the session's tradable range window.
        session = frame("2026-11-04", [("07:00", 100.0), ("07:30", 200.0)])
        assert strat.position(session) == 0.0

    def test_an_equity_range_is_unchanged(self):
        strat = OpeningRangeBreakout(range_minutes=15, asset_class="equity")
        session = frame("2026-11-04", [("03:45", 100.0), ("03:50", 101.0),
                                       ("03:55", 100.5), ("04:00", 105.0)])
        assert strat._range_for(pd.Timestamp("2026-11-04"), session) == (101.5, 99.5)


class TestRoundTheClockUniverse:
    @pytest.mark.parametrize("symbol", NEW_CRYPTO)
    def test_each_new_major_resolves_with_a_sector(self, symbol):
        instrument = data.BY_SYMBOL[symbol]
        assert instrument.asset_class == "crypto"
        assert instrument.yfinance_symbol == f"{symbol}-USD"
        assert SECTOR_MAP[symbol] == "crypto"

    def test_crypto_keeps_its_spot_cost_model(self):
        assert cost_model_for("crypto") is cost_model_for(
            data.BY_SYMBOL["SOL"].asset_class)

    def test_fx_and_futures_do_not_pay_equity_costs(self):
        broker = PaperBroker(500_000.0)
        assert broker._cost_model("EURUSD") is FX_SPOT
        assert broker._cost_model("CL") is US_FUTURES
        assert broker._cost_model("RELIANCE") is cost_model_for("equity", market="market")

    def test_an_explicit_per_symbol_model_wins(self):
        explicit = cost_model_for("crypto")
        broker = PaperBroker(500_000.0, cost_models={"RELIANCE": explicit})
        assert broker._cost_model("RELIANCE") is explicit
