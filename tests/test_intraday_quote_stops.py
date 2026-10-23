"""Intraday stop protection from live quotes.

No HTTP request is made and no real market data is involved. The quotes are
fixtures; the daily bars are synthetic exactly as in the other engine suites.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest
from stubs import StubFeed

from stratbot.broker.paper import PaperBroker
from stratbot.broker.quotes import CRYPTO_SOURCE, Quote
from stratbot.config import BotConfig, RiskLimits
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import Bar, OrderRequest, OrderStatus, Position, Side, utcnow


def make_quote(symbol: str, price: float, ts=None, age_seconds: float = 0.0,
               max_age: float = 3600.0) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        ts=ts or utcnow() - timedelta(seconds=age_seconds),
        source=CRYPTO_SOURCE,
        delayed=False,
        max_age_seconds=max_age,
    )


class FakeQuotes:
    """A quote service that answers from memory."""

    def __init__(self, quotes: dict[str, Quote] | None = None, error: Exception | None = None):
        self._quotes = quotes or {}
        self._error = error
        self.calls = 0

    def quotes(self, force: bool = False) -> dict[str, Quote]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return dict(self._quotes)


class Long:
    """A strategy that always wants `target` of equity in one symbol."""

    name = "long"

    def __init__(self, target: float = 1.0):
        self.target = target

    def position(self, history):
        return self.target


FLAT = (100.0, 100.0)
NO_HALT = RiskLimits(max_daily_loss=10_000_000.0, max_total_exposure=10_000_000.0)


class Steps:
    """A daily feed that separates the live candle from the finished ones."""

    def __init__(self, rows, symbol: str = "TEST"):
        base = pd.Timestamp(utcnow()).normalize()
        self.symbol = symbol
        self.bars = [
            Bar(symbol=symbol, ts=(base + pd.Timedelta(days=i)).to_pydatetime(),
                open=o, high=max(o, c), low=min(o, c), close=c)
            for i, (o, c) in enumerate(rows)
        ]
        self.step = 0

    def bars_now(self):
        bar = self.bars[self.step]
        return {self.symbol: bar}

    def series(self):
        done = self.bars[: self.step]
        return pd.DataFrame(
            {"open": [b.open for b in done], "high": [b.high for b in done],
             "low": [b.low for b in done], "close": [b.close for b in done],
             "volume": [1.0] * len(done)},
            index=pd.DatetimeIndex([pd.Timestamp(b.ts).normalize() for b in done]),
        )

    def advance(self):
        self.step = min(self.step + 1, len(self.bars) - 1)
        return self.step


def build(tmp_path, steps, *, limits=None, target=1.0, **cfg):
    db = Database(tmp_path / "quote_stops.sqlite3")
    config = BotConfig(symbols=("TEST",), strategy_name="donchian-55/20",
                       initial_cash=500_000.0, limits=limits or NO_HALT, **cfg)
    engine = Engine(config, db, broker=PaperBroker(500_000.0))
    engine.strategies["TEST"] = Long(target)
    engine.feed = StubFeed(steps.bars_now, steps.series)
    return engine, db


def stops_for(db) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM orders WHERE strategy='risk-stop-loss'").fetchall()
    return [dict(r) for r in rows]


def risk_events(db, kind: str) -> list[dict]:
    return [e for e in db.recent_risk_events(200) if e["kind"] == kind]


def fill_position(tmp_path, steps, *, limits=None, target=1.0, **cfg):
    """Cycle through an empty, one-bar and two-bar history so the entry queued
    on the first completed bar fills at the next bar's open: an open book at
    avg 100, with steps 3 and beyond still to come."""
    engine, db = build(tmp_path, steps, limits=limits, target=target, **cfg)
    engine.cycle()
    steps.advance()
    engine.cycle()          # the entry signal is queued
    steps.advance()
    engine.cycle()          # the entry fills
    assert engine.broker.positions["TEST"].is_open(), "no position to protect"
    return engine, db


# ======================================================================
# 1. the trigger
# ======================================================================
class TestQuoteTrigger:
    def test_a_fresh_quote_breach_triggers_before_the_daily_close(self, tmp_path):
        # The completed bars stay flat; only the live quote is down 20%.
        engine, db = fill_position(tmp_path, Steps([FLAT] * 6), target=0.2)
        engine.quotes = FakeQuotes({"TEST": make_quote("TEST", 80.0)})

        engine.cycle()

        stops = stops_for(db)
        assert len(stops) == 1, "a fresh quote breach did not trigger the stop"
        assert stops[0]["side"] == "SELL"
        assert float(stops[0]["reference_price"]) == pytest.approx(80.0), (
            "the stop was not priced off the quote"
        )
        detail = risk_events(db, "stop_loss")[0]["detail"]
        assert "policy=live quote" in detail

    def test_a_quote_at_the_stop_boundary_triggers(self, tmp_path):
        engine, db = fill_position(tmp_path, Steps([FLAT] * 6), target=0.2)
        engine.quotes = FakeQuotes({"TEST": make_quote("TEST", 92.0)})  # exactly -8%

        engine.cycle()

        assert len(stops_for(db)) == 1, "a breach at the stop boundary did not trigger"

    def test_a_quote_that_has_not_breached_triggers_nothing(self, tmp_path):
        engine, db = fill_position(tmp_path, Steps([FLAT] * 6), target=0.2)
        engine.quotes = FakeQuotes({"TEST": make_quote("TEST", 93.0)})  # -7%

        engine.cycle()

        assert stops_for(db) == []
        assert engine.broker.positions["TEST"].is_open()

    def test_the_completed_bar_still_triggers_when_the_quote_is_fine(self, tmp_path):
        # The quote is healthy, so the completed-bar stop must fire on its own.
        steps = Steps([FLAT, FLAT, FLAT, (100.0, 80.0), FLAT])
        engine, db = fill_position(tmp_path, steps, target=0.2)
        engine.quotes = FakeQuotes({"TEST": make_quote("TEST", 100.0)})
        engine.cycle()
        steps.advance()
        engine.cycle()
        steps.advance()                          # bar 3 (close 80) is completed
        engine.cycle()
        stops = stops_for(db)
        assert len(stops) == 1
        assert "policy=completed bar" in risk_events(db, "stop_loss")[0]["detail"]

    def test_the_quote_flag_can_be_disabled(self, tmp_path):
        engine, db = fill_position(tmp_path, Steps([FLAT] * 6), target=0.2,
                                   stop_loss_use_live_quotes=False)
        engine.quotes = FakeQuotes({"TEST": make_quote("TEST", 80.0)})

        engine.cycle()

        assert stops_for(db) == [], "quotes raised a stop with the flag disabled"
        assert engine.broker.positions["TEST"].is_open()


# ======================================================================
# 2. unusable quotes
# ======================================================================
class TestUnusableQuotes:
    def _no_stop_and_one_event(self, tmp_path, quotes):
        engine, db = fill_position(tmp_path, Steps([FLAT] * 6), target=0.2)
        engine.quotes = FakeQuotes(quotes)
        engine.cycle()
        assert stops_for(db) == [], "an unusable quote produced a stop"
        assert engine.broker.positions["TEST"].is_open()
        events = risk_events(db, "quote_unavailable")
        assert len(events) == 1, "a missing quote was treated as safe silently"
        assert "TEST" in events[0]["detail"]
        return engine, db

    def test_a_missing_quote_is_reported_once(self, tmp_path):
        engine, db = self._no_stop_and_one_event(
            tmp_path, {"OTHER": make_quote("OTHER", 80.0)})   # TEST has no quote at all
        # A second cycle must not repeat the event for the same symbol.
        engine.cycle()
        assert len(risk_events(db, "quote_unavailable")) == 1

    def test_a_stale_quote_is_reported_once(self, tmp_path):
        self._no_stop_and_one_event(
            tmp_path, {"TEST": make_quote("TEST", 80.0, age_seconds=9999, max_age=3600.0)})

    def test_a_non_positive_price_is_reported_once(self, tmp_path):
        self._no_stop_and_one_event(tmp_path, {"TEST": make_quote("TEST", 0.0)})

    def test_a_broken_quote_service_does_not_halt_or_stop(self, tmp_path):
        engine, db = fill_position(tmp_path, Steps([FLAT] * 6), target=0.2)
        engine.quotes = FakeQuotes(error=RuntimeError("simulated outage"))

        result = engine.cycle()

        assert result.halted is False
        assert stops_for(db) == []


# ======================================================================
# 3. once, and then only once
# ======================================================================
class TestOnceOnly:
    def _pending_stop_engine(self, tmp_path, steps):
        engine, db = fill_position(tmp_path, steps, target=0.2)
        # A quote timestamped at the forming bar keeps the stop pending: the
        # execution bar is not strictly later than the signal, so the engine
        # has not filled it yet. max_age is widened because the forming bar's
        # own timestamp is hours old by the time the cycle runs.
        forming_ts = steps.bars[steps.step].ts
        engine.quotes = FakeQuotes(
            {"TEST": make_quote("TEST", 80.0, ts=forming_ts, max_age=86_400.0)})
        engine.cycle()
        return engine, db

    def test_repeated_polling_does_not_duplicate_the_stop(self, tmp_path):
        engine, db = self._pending_stop_engine(tmp_path, Steps([FLAT] * 6))
        engine.cycle()          # same breach again while the stop is still pending
        assert len(stops_for(db)) == 1, "the stop was queued twice"

    def test_a_pending_entry_is_pre_empted_by_the_quote_stop(self, tmp_path):
        steps = Steps([FLAT] * 6)
        engine, db = build(tmp_path, steps, target=0.0)   # strategy wants nothing
        held = Position(symbol="TEST", qty=500.0, avg_price=100.0, last_price=100.0)
        engine.broker.positions["TEST"] = held
        db.upsert_position(held)
        base = pd.Timestamp(utcnow()).normalize()
        entry = OrderRequest(symbol="TEST", side=Side.BUY, qty=500.0,
                             strategy="donchian-55/20",
                             signal_bar_ts=(base - timedelta(days=1)).to_pydatetime(),
                             reference_price=100.0)
        db.record_order(entry, OrderStatus.NEW, "seeded for this test")
        forming_ts = steps.bars[0].ts
        engine.quotes = FakeQuotes(
            {"TEST": make_quote("TEST", 80.0, ts=forming_ts, max_age=86_400.0)})

        engine.cycle()

        pending = db.pending_orders()
        assert len(pending) == 1
        assert pending[0]["strategy"] == "risk-stop-loss", (
            f"a queued entry survived the stop: {pending[0]['strategy']}"
        )
        assert db.order_status(entry.client_order_id) == OrderStatus.CANCELLED.value
        with db.connect() as conn:
            row = conn.execute(
                "SELECT reason FROM orders WHERE client_order_id=?", (entry.client_order_id,)
            ).fetchone()
        assert "pre-empted by stop loss" in row["reason"]

    def test_a_strategy_opinion_cannot_cancel_the_quote_stop(self, tmp_path):
        engine, db = self._pending_stop_engine(tmp_path, Steps([FLAT] * 6))
        engine.strategies["TEST"].target = 1.0      # the strategy still wants in
        engine.cycle()
        stops = [r for r in db.pending_orders() if r["strategy"] == "risk-stop-loss"]
        assert len(stops) == 1, "a strategy opinion cancelled the protective stop"


# ======================================================================
# 4. gap handling: never at the quote price
# ======================================================================
class TestGapExecution:
    def test_the_stop_fills_at_the_next_bar_open_not_the_quote(self, tmp_path):
        # Bar 3 opens 25% under the 8% stop that bar 2's quote raised.
        steps = Steps([FLAT, FLAT, FLAT, (75.0, 75.0), FLAT])
        engine, db = fill_position(tmp_path, steps, target=0.2)
        avg = engine.broker.positions["TEST"].avg_price
        forming_ts = steps.bars[2].ts
        engine.quotes = FakeQuotes(
            {"TEST": make_quote("TEST", 92.0, ts=forming_ts, max_age=86_400.0)})
        engine.cycle()                  # quote breach queued, not yet executed
        assert engine.broker.positions["TEST"].is_open()

        steps.advance()
        engine.cycle()                  # next bar's open is 75: the gap

        exits = [f for f in db.recent_fills(50) if f["side"] == "SELL"]
        assert len(exits) == 1, "the stop did not execute at a later bar"
        # Slippage is allowed to shave the print downward, never above the open.
        assert 74.9 <= float(exits[0]["price"]) <= 75.0, (
            f"the stop filled at {exits[0]['price']}, not near the following open"
        )
        assert not engine.broker.positions["TEST"].is_open()
        breach = (float(exits[0]["price"]) - avg) / avg
        assert breach <= -NO_HALT.stop_loss_pct, (
            "the realised loss stayed inside the stop: the gap was not modelled"
        )
