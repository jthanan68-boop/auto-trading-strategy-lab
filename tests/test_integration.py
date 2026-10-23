"""End-to-end proof that the engine actually places orders through the full chain.

The live feed legitimately produces no trades (nothing is at a 55-day high on a
given day), so these tests drive synthetic bars instead of trusting chance.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from datetime import timedelta

from stubs import RaisingFeed, StubFeed, run_until_filled

from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig, RiskLimits
from stratbot.db import Database
from stratbot.models import Bar, OrderStatus, Side, utcnow
from stratbot.risk import RiskManager


def rising_history(bars: int = 120, start: float = 100.0) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=bars, freq="D")
    closes = np.linspace(start, start * 2, bars)
    return pd.DataFrame(
        {
            "open": closes, "high": closes, "low": closes, "close": closes,
            "volume": np.ones(bars),
        },
        index=index,
    )


@pytest.fixture
def engine(db_and_engine):
    return db_and_engine


@pytest.fixture
def db_and_engine(tmp_path, monkeypatch):
    import stratbot.engine as engine_module

    db = Database(tmp_path / "e2e.sqlite3")
    config = BotConfig(symbols=("BTC",), initial_cash=500_000.0)
    engine = engine_module.Engine(config, db, broker=PaperBroker(config.initial_cash))

    history = rising_history()
    base = utcnow()
    # Two bars: the signal bar, then a later one to execute against. An order
    # raised on the first never fills on that same bar.
    timeline = [
        {"BTC": Bar(symbol="BTC", ts=base, open=200, high=200, low=200, close=200)},
        {"BTC": Bar(symbol="BTC", ts=base + timedelta(days=1),
                    open=200, high=200, low=200, close=200)},
    ]

    feed = StubFeed(timeline, lambda: history)
    engine.feed = feed
    return engine


def test_a_rising_market_actually_produces_a_buy(db_and_engine):
    engine = db_and_engine
    result = run_until_filled(engine)

    assert not result.halted
    assert any("BUY" in d for d in result.decisions), f"expected a buy, got {result.decisions}"

    position = engine.broker.positions.get("BTC")
    assert position is not None and position.qty > 0
    assert engine.broker.cash < 500_000.0


def test_every_stage_is_recorded_in_the_ledger(db_and_engine):
    engine = db_and_engine
    run_until_filled(engine)

    assert engine.db.recent_signals(), "signal not logged"
    assert engine.db.recent_fills(), "fill not logged"
    assert engine.db.equity_history(), "equity not recorded"
    assert engine.db.positions(), "position not written"


def test_ledger_and_broker_stay_in_sync_after_a_trade(db_and_engine):
    engine = db_and_engine
    engine.cycle()
    engine.cycle()

    from stratbot.reconciler import reconcile

    assert reconcile(engine.db, engine.broker.get_account()) == []


def test_engine_halts_and_flattens_on_emergency_stop(db_and_engine):
    engine = db_and_engine
    run_until_filled(engine)
    assert engine.broker.positions["BTC"].is_open()

    result = engine.emergency_stop()

    assert engine.risk.halted
    assert not engine.broker.positions["BTC"].is_open()
    assert result["flattened"]


def test_halted_engine_produces_no_new_orders(db_and_engine):
    from stratbot.models import HaltReason

    engine = db_and_engine
    run_until_filled(engine)
    before = len(engine.db.recent_fills())
    engine.risk.halt(HaltReason.MANUAL, "test")
    engine.cycle()
    assert len(engine.db.recent_fills()) == before


def test_daily_loss_limit_stops_further_buying(tmp_path, monkeypatch):
    import stratbot.engine as engine_module
    from datetime import timedelta

    db = Database(tmp_path / "loss.sqlite3")
    config = BotConfig(
        symbols=("BTC",),
        initial_cash=500_000.0,
        limits=RiskLimits(max_daily_loss=1.0),
    )
    engine = engine_module.Engine(config, db, broker=PaperBroker(config.initial_cash))
    base = utcnow()
    timeline = [
        {"BTC": Bar(symbol="BTC", ts=base, open=200, high=200, low=200, close=200)},
        {"BTC": Bar(symbol="BTC", ts=base + timedelta(days=1),
                    open=200, high=200, low=200, close=200)},
    ]
    engine.feed = StubFeed(timeline, lambda: rising_history())

    engine.db.set_state("day_start", utcnow().date().isoformat())
    engine.db.set_state("day_start_equity", 10_000_000.0)  # implies a huge loss already

    # The limit is enforced when the order would execute, one bar after the signal.
    result = run_until_filled(engine)

    assert result.halted
    assert result.halt_reason == "daily_loss_limit"
    # The signal may still be attempted and refused; what must not happen is a fill.
    assert engine.db.recent_fills() == []
    assert not engine.broker.positions.get("BTC")


def test_stale_feed_data_stops_trading(tmp_path, monkeypatch):
    import stratbot.engine as engine_module
    from datetime import timedelta

    db = Database(tmp_path / "stale.sqlite3")
    config = BotConfig(symbols=("BTC",), initial_cash=500_000.0)
    engine = engine_module.Engine(config, db, broker=PaperBroker(config.initial_cash))

    stale = Bar(symbol="BTC", ts=utcnow() - timedelta(days=5), open=200, high=200, low=200, close=200)
    feed = StubFeed(lambda: {"BTC": stale}, lambda: rising_history())
    engine.feed = feed
    result = engine.cycle()

    assert result.halted
    assert result.halt_reason == "stale_price"
    assert engine.db.recent_fills() == []
    assert not engine.broker.positions.get("BTC")


def test_empty_feed_response_halts_rather_than_trading_blindly(tmp_path, monkeypatch):
    import stratbot.engine as engine_module

    db = Database(tmp_path / "empty.sqlite3")
    config = BotConfig(symbols=("BTC",), initial_cash=500_000.0)
    engine = engine_module.Engine(config, db, broker=PaperBroker(config.initial_cash))

    engine.feed = RaisingFeed(RuntimeError("feed down"))
    result = engine.cycle()
    assert result.halted
    assert result.bars == {}


def test_duplicate_signals_do_not_stack_positions(db_and_engine):
    engine = db_and_engine
    run_until_filled(engine)
    first_qty = engine.broker.positions["BTC"].qty
    for _ in range(3):
        engine.cycle()
    # The strategy is already long, so repeated cycles must not keep buying.
    assert engine.broker.positions["BTC"].qty == pytest.approx(first_qty, rel=1e-6)
