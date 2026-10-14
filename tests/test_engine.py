"""Paper broker, reconciler and ledger integrity."""

from __future__ import annotations

import pytest

from stratbot.broker.paper import PaperBroker
from stratbot.config import RiskLimits
from stratbot.db import Database
from stratbot.models import Bar, HaltReason, OrderRequest, OrderStatus, Position, Side, utcnow
from stratbot.reconciler import adopt_broker_state, reconcile
from stratbot.risk import RiskManager


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "broker.sqlite3")


def buy(symbol="BTC", qty=1.0, strategy="test", price_ref=100.0) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        strategy=strategy,
        signal_bar_ts=utcnow(),
        reference_price=price_ref,
    )


def test_buy_reduces_cash_and_creates_a_position():
    broker = PaperBroker(100_000.0)
    status, fill = broker.submit(buy(), 100.0)
    assert status is OrderStatus.FILLED
    assert fill is not None
    assert broker.cash < 100_000.0
    assert broker.positions["BTC"].qty == pytest.approx(1.0, rel=1e-3)


def test_slippage_always_moves_the_price_against_the_trader():
    broker = PaperBroker(100_000.0)
    _, buy_fill = broker.submit(buy(qty=1), 100.0)
    assert buy_fill.price > 100.0, "a buy must fill above the reference"


def test_resubmitting_the_same_order_id_does_not_double_fill():
    broker = PaperBroker(100_000.0)
    request = buy(qty=1.0)
    broker.submit(request, 100.0)
    qty_after_first = broker.positions["BTC"].qty
    status, fill = broker.submit(request, 100.0)
    assert fill is None, "a repeat must not produce a second fill"
    assert status is OrderStatus.FILLED, "the original outcome is returned"
    assert broker.positions["BTC"].qty == pytest.approx(qty_after_first)


def test_sell_cannot_go_below_zero():
    broker = PaperBroker(100_000.0)
    broker.submit(buy(qty=2.0), 100.0)
    # Ask to sell 5 when only 2 are held; the broker must clamp rather than go short.
    sell = OrderRequest(symbol="BTC", side=Side.SELL, qty=5.0, strategy="exit", signal_bar_ts=utcnow())
    broker.submit(sell, 100.0)
    assert broker.positions["BTC"].qty == pytest.approx(0.0, abs=1e-9)
    assert broker.positions["BTC"].qty >= 0


def test_selling_more_than_held_is_clamped_not_rejected():
    broker = PaperBroker(100_000.0)
    broker.submit(buy(qty=2.0), 100.0)
    sell = OrderRequest(symbol="BTC", side=Side.SELL, qty=5.0, strategy="exit", signal_bar_ts=utcnow())
    _, fill = broker.submit(sell, 100.0)
    assert fill is not None, "an overselling exit should still flatten what is held"
    assert fill.qty == pytest.approx(2.0, rel=1e-3)


def test_flatten_returns_every_open_position():
    broker = PaperBroker(100_000.0)
    broker.submit(buy("BTC", 1.0), 100.0)
    broker.submit(buy("ETH", 1.0), 100.0)
    requests = broker.flatten({"BTC": 100.0, "ETH": 100.0})
    assert {r.symbol for r in requests} == {"BTC", "ETH"}
    assert all(r.side is Side.SELL for r in requests)


def test_no_live_credentials_are_readable_anywhere():
    import pathlib

    forbidden = ("api_key", "api_secret", "access_token", "apiKey", "API_SECRET", "KITE_API")
    root = pathlib.Path(__file__).resolve().parent.parent
    # Scans source packages only: this test file necessarily contains the tokens.
    for package in ("stratbot", "stratlab"):
        for path in (root / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for token in forbidden:
                assert token not in text, f"{path.name} appears to reference {token}"


def test_reconcile_passes_when_ledger_matches_broker(db):
    broker = PaperBroker(100_000.0)
    _, fill = broker.submit(buy(), 100.0)
    db.upsert_position(broker.positions["BTC"])
    assert reconcile(db, broker.get_account()) == []


def test_reconcile_detects_a_phantom_position_and_halts(db):
    broker = PaperBroker(100_000.0)
    db.upsert_position(Position(symbol="GHOST", qty=5, avg_price=10, opened_at=utcnow()))
    risk = RiskManager(db, RiskLimits())
    mismatches = reconcile(db, broker.get_account(), risk)
    assert len(mismatches) == 1
    assert mismatches[0].symbol == "GHOST"
    assert risk.halt_reason is HaltReason.RECONCILE_MISMATCH


def test_reconcile_detects_a_missing_position(db):
    broker = PaperBroker(100_000.0)
    broker.submit(buy(), 100.0)
    mismatches = reconcile(db, broker.get_account())
    assert len(mismatches) == 1
    assert mismatches[0].ledger_qty == 0.0
    assert mismatches[0].broker_qty > 0


def test_adopt_broker_state_repairs_the_ledger(db):
    broker = PaperBroker(100_000.0)
    broker.submit(buy(), 100.0)
    assert reconcile(db, broker.get_account())
    changed = adopt_broker_state(db, broker.get_account())
    assert changed == 1
    assert reconcile(db, broker.get_account()) == []


def test_ledger_captures_signal_order_and_fill(db):
    from stratbot.models import Fill

    request = buy(qty=1.0)
    db.log_signal("test", "BTC", "BUY", 0.5, 100.0, utcnow(), "because")
    db.record_order(request, OrderStatus.NEW)
    db.record_fill(Fill(request.client_order_id, utcnow(), "BTC", Side.BUY, 1.0, 100.0, 0.3))
    db.update_order_status(request.client_order_id, OrderStatus.FILLED)

    assert db.recent_signals()[0]["reason"] == "because"
    assert db.recent_fills()[0]["fees"] == pytest.approx(0.3)
    assert db.open_orders() == [], "a filled order must not still look open"


def test_order_starts_new_and_can_be_resolved(db):
    request = buy()
    db.record_order(request, OrderStatus.NEW)
    assert [o["client_order_id"] for o in db.open_orders()] == [request.client_order_id]
    db.update_order_status(request.client_order_id, OrderStatus.FILLED)
    assert db.open_orders() == []


def test_daily_baseline_resets_once_per_day(db):
    assert db.reset_day(100_000.0) is True
    assert db.reset_day(99_000.0) is False, "must not reset twice on the same day"
    assert db.day_pnl(99_000.0) == pytest.approx(-1_000.0)


def test_emergency_stop_halts_cancels_and_flattens(db):
    from stratbot.config import BotConfig
    from stratbot.engine import Engine

    engine = Engine(BotConfig(symbols=("BTC",), initial_cash=100_000.0), db)
    request = OrderRequest(symbol="BTC", side=Side.BUY, qty=1.0, strategy="t", signal_bar_ts=utcnow())
    engine.broker.submit(request, 100.0)
    db.upsert_position(engine.broker.positions["BTC"])
    assert engine.broker.positions["BTC"].is_open()

    result = engine.emergency_stop()

    assert result["halted"] is True
    assert engine.risk.halted
    assert len(result["flattened"]) == 1
    assert not engine.broker.positions["BTC"].is_open(), "position must be closed"


def test_engine_never_trades_while_halted(db):
    from stratbot.config import BotConfig
    from stratbot.engine import Engine

    engine = Engine(BotConfig(symbols=("BTC",), initial_cash=100_000.0), db)
    engine.risk.halt(HaltReason.MANUAL, "test")
    bar = Bar(symbol="BTC", ts=utcnow(), open=100, high=100, low=100, close=100)
    request = OrderRequest(symbol="BTC", side=Side.BUY, qty=1.0, strategy="t", signal_bar_ts=utcnow())
    outcome = engine._execute(request, bar)
    assert "rejected" in outcome
    assert engine.broker.positions.get("BTC") is None