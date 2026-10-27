"""Square-off window, restart recovery and forced-exit accounting.

Calendar is patched deterministically; the feed is a StubFeed, so nothing here
touches the network or a real broker.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pandas as pd
import pytest

from stubs import StubFeed

from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig, intraday_limits
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import Bar, OrderRequest, Side, utcnow

SYMBOL = "RELIANCE"
INITIAL_CASH = 500_000.0


def base_ts() -> datetime:
    """Tests anchor timestamps to the real day so freshness gates stay sane."""
    return pd.Timestamp(utcnow()).normalize().to_pydatetime()


def line(prices: list[float], start: datetime, minutes: int = 5) -> pd.DataFrame:
    index = pd.DatetimeIndex(
        [start + timedelta(minutes=minutes * i) for i in range(len(prices))])
    closes = pd.Series(prices, index=index, dtype=float)
    return pd.DataFrame(
        {"open": closes, "high": closes + 0.2, "low": closes - 0.2,
         "close": closes, "volume": 1000.0},
        index=index,
    )


def bar_from(ts: datetime, price: float) -> Bar:
    return Bar(symbol=SYMBOL, ts=ts, open=price, high=price + 0.2,
               low=price - 0.2, close=price, volume=1000.0)


def intraday_config() -> BotConfig:
    return BotConfig(
        symbols=(SYMBOL,), strategy_name="orb-15min", trading_style="intraday",
        bar_interval="5m", limits=intraday_limits(),
    )


def engine_with(db: Database, bars_fn, history, broker=None,
                target: float | None = None) -> Engine:
    engine = Engine(intraday_config(), db,
                    broker=broker or PaperBroker(INITIAL_CASH, trading_style="intraday"))
    engine.feed = StubFeed(bars_fn, history)
    if target is not None:
        engine.strategies[SYMBOL] = type(
            "FixedTarget", (), {"position": staticmethod(lambda h: target)})()
    return engine


def open_position(broker: PaperBroker, price: float = 100.0):
    broker.submit(OrderRequest(
        symbol=SYMBOL, side=Side.BUY, qty=100.0, strategy="test",
        signal_bar_ts=utcnow(), reference_price=price,
    ), price)


# =====================================================================
# square-off window
# =====================================================================
class TestSquareOff:
    def _bars_at(self, minutes_after_midnight: int, price: float):
        return lambda: {SYMBOL: bar_from(
            base_ts() + timedelta(minutes=minutes_after_midnight), price)}

    @pytest.fixture
    def session_live(self, monkeypatch):
        """Session running, with a controllable number of bars left."""
        state = {"remaining": 4}

        def fake_remaining(self, bar: Bar) -> int:
            return state["remaining"]

        monkeypatch.setattr(Engine, "_remaining_bars", fake_remaining)
        import stratbot.engine as engine_mod

        monkeypatch.setattr(engine_mod, "session_state",
                            lambda now, asset_class: "active")
        return state

    def test_queues_a_square_off_inside_the_window(self, tmp_path, session_live):
        session_live["remaining"] = 2     # buffer is 3 -> inside the window
        db = Database(tmp_path / "so.sqlite3")
        broker = PaperBroker(INITIAL_CASH, trading_style="intraday")
        open_position(broker)
        db.upsert_position(broker.positions[SYMBOL])
        engine = engine_with(db, self._bars_at(560, 101.0),
                             line([100.0, 101.0], base_ts() + timedelta(minutes=540)),
                             broker=broker, target=1.0)

        engine.cycle()

        queued = [row for row in db.pending_orders()
                  if row["strategy"] == "square-off"]
        assert len(queued) == 1
        assert queued[0]["side"] == Side.SELL.value
        # every strategy entry for the symbol had to step aside
        strategy_rows = [row for row in db.pending_orders()
                         if row["strategy"] not in
                         ("square-off", "risk-stop-loss", "kill-switch")]
        assert strategy_rows == []

    def test_no_square_off_while_bars_remain(self, tmp_path, session_live):
        session_live["remaining"] = 10    # well outside the 3-bar buffer
        db = Database(tmp_path / "so.sqlite3")
        broker = PaperBroker(INITIAL_CASH, trading_style="intraday")
        open_position(broker)
        db.upsert_position(broker.positions[SYMBOL])
        engine = engine_with(db, self._bars_at(560, 101.0),
                             line([100.0, 101.0], base_ts() + timedelta(minutes=540)),
                             broker=broker, target=1.0)

        engine.cycle()

        assert [row for row in db.pending_orders()
                if row["strategy"] == "square-off"] == []

    def test_square_off_fills_at_the_next_open_and_books_the_trade(
            self, tmp_path, session_live):
        session_live["remaining"] = 2
        db = Database(tmp_path / "so.sqlite3")
        broker = PaperBroker(INITIAL_CASH, trading_style="intraday")
        open_position(broker)                       # bought 100 @ ~100.05
        db.upsert_position(broker.positions[SYMBOL])
        engine = engine_with(db, self._bars_at(560, 101.0),
                             line([100.0, 101.0], base_ts() + timedelta(minutes=540)),
                             broker=broker, target=1.0)
        engine.cycle()                              # queues the square-off

        engine.feed = StubFeed(
            self._bars_at(585, 101.5),
            line([100.0, 101.0, 101.5], base_ts() + timedelta(minutes=540)))
        engine.cycle()                              # next open fills the exit

        filled = db.pending_orders()
        assert filled == []
        assert broker.positions[SYMBOL].qty <= 1e-9
        trades = db.closed_trades()
        assert len(trades) == 1
        assert trades[0]["symbol"] == SYMBOL
        assert trades[0]["exit_price"] == pytest.approx(101.5, rel=2e-3)
        assert trades[0]["pnl"] > 0                 # bought ~100, sold ~101.5

    def test_entries_blocked_inside_the_window(self, tmp_path, session_live):
        session_live["remaining"] = 2
        db = Database(tmp_path / "so.sqlite3")
        engine = engine_with(db, self._bars_at(560, 101.0),
                             line([100.0, 101.0], base_ts() + timedelta(minutes=540)),
                             target=1.0)            # wants a fresh long

        result = engine.cycle()

        buys = [row for row in db.pending_orders() if row["side"] == Side.BUY.value]
        assert buys == []
        assert any("square-off window" in d for d in result.decisions)

    def test_reducing_always_has_a_path(self, tmp_path, session_live):
        session_live["remaining"] = 2
        db = Database(tmp_path / "so.sqlite3")
        broker = PaperBroker(INITIAL_CASH, trading_style="intraday")
        open_position(broker)
        db.upsert_position(broker.positions[SYMBOL])
        engine = engine_with(db, self._bars_at(560, 101.0),
                             line([100.0, 101.0], base_ts() + timedelta(minutes=540)),
                             broker=broker, target=0.0)     # wants flat

        engine.cycle()

        # the square-off owns the reducing exit; the bot is not stuck
        queued = [row for row in db.pending_orders() if row["strategy"] == "square-off"]
        assert len(queued) == 1

    def test_daily_style_never_squares_off(self, tmp_path):
        db = Database(tmp_path / "so.sqlite3")
        config = replace(intraday_config(), trading_style="daily")
        broker = PaperBroker(INITIAL_CASH)
        open_position(broker)
        db.upsert_position(broker.positions[SYMBOL])
        engine = Engine(config, db, broker=broker)
        engine.feed = StubFeed(
            self._bars_at(585, 101.0),
            line([100.0, 101.0], base_ts() + timedelta(minutes=540)))
        engine.strategies[SYMBOL] = type(
            "FixedTarget", (), {"position": staticmethod(lambda h: 1.0)})()

        engine.cycle()
        assert [row for row in db.pending_orders()
                if row["strategy"] == "square-off"] == []


# =====================================================================
# restart recovery: out-of-session book
# =====================================================================
class TestOutOfSessionFlatten:
    def _engine(self, tmp_path, broker=None):
        db = Database(tmp_path / "rec.sqlite3")
        engine = Engine(intraday_config(), db,
                        broker=broker or PaperBroker(INITIAL_CASH, trading_style="intraday"))
        engine.feed = StubFeed(
            self._bars_at(620, 101.0),
            line([100.0, 101.0], base_ts() + timedelta(minutes=540)))
        engine.strategies[SYMBOL] = type(
            "FixedTarget", (), {"position": staticmethod(lambda h: 0.0)})()
        return engine, db

    @staticmethod
    def _bars_at(minutes_after_midnight: int, price: float):
        return lambda: {SYMBOL: bar_from(
            base_ts() + timedelta(minutes=minutes_after_midnight), price)}

    def test_flattens_after_the_close(self, tmp_path, monkeypatch):
        import stratbot.engine as engine_mod

        monkeypatch.setattr(engine_mod, "session_state",
                            lambda now, asset_class: "post")
        broker = PaperBroker(INITIAL_CASH, trading_style="intraday")
        open_position(broker)
        engine, db = self._engine(tmp_path, broker)
        db.upsert_position(broker.positions[SYMBOL])    # the real flow upserts fills

        result = engine.cycle()
        db.upsert_position(broker.positions[SYMBOL])    # keep the ledger in step

        assert broker.positions[SYMBOL].qty <= 1e-9
        events = db.recent_risk_events()
        assert any(e["kind"] == "forced_exit_out_of_session" for e in events)
        assert db.closed_trades()                   # _execute books the round trip
        assert any("FORCED EXIT" in d for d in result.decisions)

    def test_flattens_a_position_carried_into_a_new_session(self, tmp_path, monkeypatch):
        import stratbot.engine as engine_mod

        monkeypatch.setattr(engine_mod, "session_state",
                            lambda now, asset_class: "active")
        broker = PaperBroker(INITIAL_CASH, trading_style="intraday")
        open_position(broker)
        broker.positions[SYMBOL].opened_at = utcnow() - timedelta(days=1)
        engine, db = self._engine(tmp_path, broker)
        db.upsert_position(broker.positions[SYMBOL])

        engine.cycle()
        db.upsert_position(broker.positions[SYMBOL])

        assert broker.positions[SYMBOL].qty <= 1e-9
        events = db.recent_risk_events()
        assert any("carried into a new session" in e["detail"]
                   for e in events if e["kind"] == "forced_exit_out_of_session")

    def test_touches_nothing_inside_a_fresh_session(self, tmp_path, monkeypatch):
        import stratbot.engine as engine_mod

        monkeypatch.setattr(engine_mod, "session_state",
                            lambda now, asset_class: "active")
        broker = PaperBroker(INITIAL_CASH, trading_style="intraday")
        open_position(broker)
        broker.positions[SYMBOL].opened_at = utcnow()   # opened this session
        engine, db = self._engine(tmp_path, broker)
        db.upsert_position(broker.positions[SYMBOL])

        engine.cycle()

        assert broker.positions[SYMBOL].qty > 0
        assert [e for e in db.recent_risk_events()
                if e["kind"] == "forced_exit_out_of_session"] == []


# =====================================================================
# forced-exit accounting through _execute
# =====================================================================
class TestExecuteAccounting:
    def _engine(self, tmp_path):
        db = Database(tmp_path / "exec.sqlite3")
        engine = Engine(intraday_config(), db,
                        PaperBroker(INITIAL_CASH, trading_style="intraday"))
        return engine, db

    def test_round_trip_books_closed_trade_and_fees(self, tmp_path):
        engine, db = self._engine(tmp_path)
        bar = bar_from(base_ts() + timedelta(minutes=560), 100.0)
        engine._execute(OrderRequest(
            symbol=SYMBOL, side=Side.BUY, qty=100.0, strategy="test",
            signal_bar_ts=bar.ts, reference_price=100.0,
        ), bar, bypass_risk=True)
        qty = engine.broker.positions[SYMBOL].qty
        sell_bar = bar_from(base_ts() + timedelta(minutes=585), 100.0)
        engine._execute(OrderRequest(
            symbol=SYMBOL, side=Side.SELL, qty=qty, strategy="test",
            signal_bar_ts=sell_bar.ts, reference_price=100.0,
        ), sell_bar, bypass_risk=True)

        trades = db.closed_trades()
        assert len(trades) == 1
        assert trades[0]["fees"] > 0
        assert trades[0]["pnl"] < 0     # slippage twice on a flat round trip
