"""Order-rate gate behaviour at execution time.

The gates still exist exactly as configured; what changes over the old
behaviour is observable classification (RATE_LIMITED, not REJECTED) and that
the cap counts orders that reached the broker, not rows that are merely queued.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest
from stubs import StubFeed

from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig, RiskLimits
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import Bar, OrderRequest, OrderStatus, Side, utcnow


class AlwaysLong:
    name = "always-long"

    def __init__(self, target: float = 1.0):
        self.target = target

    def position(self, history):
        return self.target


BIG = dict(
    max_daily_loss=10_000_000.0,
    max_total_exposure=10_000_000_000.0,
    max_position_value=10_000_000_000.0,
)


class Timeline:
    def __init__(self, pairs, symbol: str = "TEST"):
        base = pd.Timestamp(utcnow()).normalize()
        self.symbol = symbol
        self.bars = [
            Bar(symbol=symbol, ts=(base + pd.Timedelta(days=i)).to_pydatetime(),
                open=o, high=max(o, c), low=min(o, c), close=c)
            for i, (o, c) in enumerate(pairs)
        ]
        self.step = 0

    def current(self):
        return {self.symbol: self.bars[self.step]}

    def series(self) -> pd.DataFrame:
        seen = self.bars[: self.step + 1]
        return pd.DataFrame(
            {"open": [b.open for b in seen], "high": [b.high for b in seen],
             "low": [b.low for b in seen], "close": [b.close for b in seen],
             "volume": [b.volume for b in seen]},
            index=pd.DatetimeIndex([pd.Timestamp(b.ts).normalize() for b in seen]),
        )

    def advance(self):
        self.step = min(self.step + 1, len(self.bars) - 1)
        return self.step


def build(tmp_path, feed_bars, feed_history, *, limits=None, cash=500_000.0, target=1.0):
    db = Database(tmp_path / "rates.sqlite3")
    config = BotConfig(symbols=("TEST",), strategy_name="donchian-55/20",
                       initial_cash=cash, limits=limits or RiskLimits(**BIG))
    engine = Engine(config, db, broker=PaperBroker(cash))
    if target is not None:
        engine.strategies["TEST"] = AlwaysLong(target)
    engine.feed = StubFeed(feed_bars, feed_history)
    return engine, db


def all_orders(db) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM orders").fetchall()
    return [dict(r) for r in rows]


def events(db, kind: str) -> list[dict]:
    return [e for e in db.recent_risk_events(200) if e["kind"] == kind]


# ======================================================================
# 1. within the limits nothing changes
# ======================================================================
class TestWithinLimits:
    def test_orders_inside_the_hourly_cap_fill_normally(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline.current, timeline.series,
                           limits=RiskLimits(max_orders_per_hour=5, max_orders_per_day=100, **BIG))

        engine.cycle()          # signal
        timeline.advance()
        engine.cycle()          # fill

        assert len(db.recent_fills(50)) == 1
        assert [o for o in all_orders(db) if o["status"] == OrderStatus.RATE_LIMITED.value] == []


# ======================================================================
# 2. over the gate: classified, observable, terminal
# ======================================================================
class TestOverTheGate:
    def _seeded_entry_engine(self, tmp_path, hours_cap=2, day_cap=100, count=3):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)] * 2)
        limits = RiskLimits(max_orders_per_hour=hours_cap, max_orders_per_day=day_cap, **BIG)
        engine, db = build(tmp_path, timeline.current, timeline.series, limits=limits, target=0.0)
        base = timeline.bars[0].ts
        for i in range(count):
            request = OrderRequest(symbol="TEST", side=Side.BUY, qty=10.0,
                                   strategy="donchian-55/20",
                                   signal_bar_ts=base - timedelta(days=count - i),
                                   reference_price=100.0)
            db.record_order(request, OrderStatus.NEW, "seeded for this test")
        return engine, db

    def test_exceeding_the_hourly_cap_is_rate_limited_not_rejected(self, tmp_path):
        engine, db = self._seeded_entry_engine(tmp_path, hours_cap=2, count=3)

        engine.cycle()

        statuses = [o["status"] for o in all_orders(db)]
        assert statuses.count("FILLED") == 2, f"expected 2 fills inside the cap, got {statuses}"
        assert statuses.count("RATE_LIMITED") == 1, f"the excess was misclassified: {statuses}"
        rate = events(db, "order_rate_limited")
        assert len(rate) == 1
        assert "limits 2/h" in rate[0]["detail"]
        rejected = [o for o in all_orders(db) if o["status"] == "REJECTED"]
        assert rejected == [], "a volume-gate refusal was classified as a rejection"

    def test_exceeding_the_daily_cap_is_rate_limited(self, tmp_path):
        engine, db = self._seeded_entry_engine(tmp_path, hours_cap=100, day_cap=1, count=2)

        engine.cycle()

        statuses = [o["status"] for o in all_orders(db)]
        assert statuses.count("FILLED") == 1
        assert statuses.count("RATE_LIMITED") == 1
        assert "daily order rate limit reached" in events(db, "order_rate_limited")[0]["detail"]

    def test_a_rate_limited_intent_is_not_retried_within_the_same_bar(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline.current, timeline.series,
                           limits=RiskLimits(max_orders_per_hour=0, max_orders_per_day=100, **BIG),
                           target=1.0)

        engine.cycle()          # the entry intent is queued
        timeline.advance()      # the execution bar arrives, the hourly gate is full
        engine.cycle()          # refused, and the strategy re-signals on its own bar
        rows = all_orders(db)
        assert any(o["status"] == "RATE_LIMITED" for o in rows), f"no refusal: {rows}"

        # The decision bar did not change, so re-considering the intent must
        # hit the existing row ("already ...") rather than raise a new one.
        engine.cycle()
        engine.cycle()
        assert len(all_orders(db)) == len(rows), "the refusal produced duplicates"
        assert db.recent_fills(50) == []

    def test_decisions_line_names_the_gate(self, tmp_path):
        engine, db = self._seeded_entry_engine(tmp_path, hours_cap=0, count=1)
        result = engine.cycle()
        assert any("rate limited:" in decision for decision in result.decisions)


# ======================================================================
# 3. priority: protective exits ride through a full gate
# ======================================================================
class TestProtectivePriority:
    def test_a_stop_loss_fills_even_when_the_rate_cap_is_full(self, tmp_path):
        limits = RiskLimits(max_orders_per_hour=1, max_orders_per_day=100,
                            max_bar_age_days=10, **BIG)
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0), (105.0, 110.0),
                             (40.0, 40.0), (40.0, 40.0)])
        engine, db = build(tmp_path, timeline.current, timeline.series, limits=limits)

        engine.cycle()                                  # signal
        timeline.advance()
        engine.cycle()                                  # entry fills: 1 submission, cap full
        assert engine.broker.positions["TEST"].is_open()

        timeline.advance()                              # crash bars approaching
        engine.cycle()
        timeline.advance()
        engine.cycle()
        timeline.advance()                              # the completed crash bar
        engine.cycle()                                  # the stop rides through the full gate

        exits = [f for f in db.recent_fills(50) if f["side"] == "SELL"]
        assert len(exits) == 1, (
            "the protective stop was blocked by the entry-rate cap"
        )
        assert not engine.broker.positions["TEST"].is_open()


# ======================================================================
# 4. a full-universe burst is deterministic and bounded
# ======================================================================
def flat_history() -> pd.DataFrame:
    index = pd.date_range(end=pd.Timestamp(utcnow()).normalize(), periods=60, freq="D")
    closes = [100.0] * 60
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": 10.0}, index=index)


def test_a_41_symbol_burst_fills_in_queue_order_and_no_further(tmp_path):
    symbols = tuple(f"S{i:02d}" for i in range(41))
    db = Database(tmp_path / "burst.sqlite3")
    limits = RiskLimits(max_orders_per_hour=20, max_orders_per_day=100,
                        max_bar_age_days=10, **BIG)
    config = BotConfig(symbols=symbols, strategy_name="donchian-55/20",
                       initial_cash=500_000.0, limits=limits)
    engine = Engine(config, db, broker=PaperBroker(500_000.0))
    engine.strategies = {}            # no signal noise: only the seeded intents

    base = pd.Timestamp(utcnow())
    feed_bars = {
        s: Bar(symbol=s, ts=(base + pd.Timedelta(seconds=i)).to_pydatetime(),
               open=100.0, high=100.0, low=100.0, close=100.0)
        for i, s in enumerate(symbols)
    }
    engine.feed = StubFeed(lambda: feed_bars, lambda symbol: flat_history())

    for i, s in enumerate(symbols):
        request = OrderRequest(symbol=s, side=Side.BUY, qty=100.0,
                               strategy="donchian-55/20",
                               signal_bar_ts=(base - timedelta(days=1, minutes=41 - i)).to_pydatetime(),
                               reference_price=100.0)
        db.record_order(request, OrderStatus.NEW, "seeded for this test")

    engine.cycle()

    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM orders ORDER BY ts ASC, client_order_id ASC").fetchall()]
    queued_order = [r["client_order_id"] for r in rows]
    filled_ids = [f["client_order_id"] for f in db.recent_fills(100)]
    assert len(filled_ids) == 20, f"expected exactly 20 fills: {len(filled_ids)}"
    assert set(filled_ids) == set(queued_order[:20]), (
        "the fills did not respect the deterministic queue order"
    )
    rate_limited = [r for r in rows if r["status"] == "RATE_LIMITED"]
    assert len(rate_limited) == 21
    assert len(events(db, "order_rate_limited")) == 21

    engine.cycle()                     # a second cycle changes nothing
    assert len(db.recent_fills(100)) == 20, "a follow-up cycle filled behind the cap"
