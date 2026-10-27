"""One complete intraday lifecycle driven through the real engine.cycle() chain.

StubFeed serves a 4-bar timeline; the calendar hooks are monkeypatched so the
test clock never depends on the wall clock. The run: signal -> next-open fill ->
hold -> square-off queued inside the window -> next-open exit -> ledger books
the round trip with intraday fees. Nothing touches the network.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from stubs import StubFeed

from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig, intraday_limits
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import Side, utcnow

SYMBOL = "RELIANCE"
INITIAL_CASH = 500_000.0


def timeline():
    base = utcnow().replace(tzinfo=None) - timedelta(minutes=15)
    bars = []
    for i, price in enumerate((100.0, 101.0, 102.0, 101.5)):
        ts = base + timedelta(minutes=5 * i)
        bars.append({SYMBOL: _bar(SYMBOL, ts, price)})
    return bars, _history(bars)


def _bar(symbol, ts, price):
    # Import late keeps the model constructors out of the module import path.
    from stratbot.models import Bar

    return Bar(symbol=symbol, ts=ts, open=price, high=price + 0.2,
               low=price - 0.2, close=price, volume=1000.0)


def _history(bars):
    index = pd.DatetimeIndex([b[SYMBOL].ts for b in bars])
    closes = pd.Series([b[SYMBOL].close for b in bars], index=index, dtype=float)
    return pd.DataFrame(
        {"open": closes, "high": closes + 0.2, "low": closes - 0.2,
         "close": closes, "volume": 1000.0},
        index=index,
    )


class AlwaysLong:
    name = "fixed-long"

    def __init__(self, target: float = 1.0):
        self.target = target

    def position(self, history: pd.DataFrame) -> float:
        return self.target


@pytest.fixture
def active_session(monkeypatch):
    import stratbot.engine as engine_mod

    state = {"remaining": 30}
    monkeypatch.setattr(
        Engine, "_remaining_bars", lambda self, bar: state["remaining"])
    monkeypatch.setattr(engine_mod, "session_state",
                        lambda now, asset_class: "active")
    return state


def build_engine(tmp_path, broker=None) -> Engine:
    config = BotConfig(
        symbols=(SYMBOL,), strategy_name="orb-15min", trading_style="intraday",
        bar_interval="5m", limits=intraday_limits(),
    )
    db = Database(tmp_path / "lifecycle.sqlite3")
    engine = Engine(config, db,
                    broker=broker or PaperBroker(INITIAL_CASH, trading_style="intraday"))
    bars, _ = timeline()
    feed = StubFeed(bars, None)

    def history_fn() -> pd.DataFrame:
        # The decision history grows with the timeline: a static full timeline
        # would let the engine "decide" on bars that have not happened yet and
        # then wait forever for a bar newer than its own signal.
        return _history(bars[: feed.step + 1])

    feed._history = history_fn
    engine.feed = feed
    engine.strategies[SYMBOL] = AlwaysLong()
    return engine


class TestFullIntradayLifecycle:
    def test_signal_fill_hold_squareoff_exit(self, tmp_path, active_session):
        engine = build_engine(tmp_path)

        # 1: the signal forms on bar t and queues a BUY for t+1's open.
        result = engine.cycle()
        assert not result.halted
        buys = [r for r in engine.db.pending_orders() if r["side"] == Side.BUY.value]
        assert len(buys) == 1
        assert engine.broker.positions == {}

        # 2: the next bar arrives; the BUY fills at its open, never bar t's close.
        engine.feed.advance()
        result = engine.cycle()
        fills = engine.db.recent_fills(10)
        assert fills
        buy_fill = [f for f in fills if f["side"] == Side.BUY.value][0]
        assert 100.0 < buy_fill["price"] <= 101.2   # the t+1 open zone
        assert engine.broker.positions[SYMBOL].qty > 0
        fills_before = len(fills)

        # 3: the close nears; the square-off window opens and queues the exit.
        active_session["remaining"] = 2             # buffer is 3 bars
        engine.feed.advance()
        result = engine.cycle()
        square_offs = [r for r in engine.db.pending_orders()
                       if r["strategy"] == "square-off"]
        assert len(square_offs) == 1
        assert square_offs[0]["side"] == Side.SELL.value
        # No new exposure was raised on top of the position while it was open.
        assert len(engine.db.recent_fills(10)) == fills_before

        # 4: the next bar's open executes the square-off.
        engine.feed.advance()
        result = engine.cycle()
        assert engine.db.pending_orders() == []
        assert engine.broker.positions[SYMBOL].qty <= 1e-9

        # The ledger booked the round trip, with fees, and the audit trail says why.
        trades = engine.db.closed_trades()
        assert len(trades) == 1
        assert trades[0]["entry_ts"] < trades[0]["exit_ts"]
        equity_rows = engine.db.equity_history()
        assert len(equity_rows) >= 4
        events = [e["kind"] for e in engine.db.recent_risk_events()]
        assert "square_off" in events
        assert not result.halted
