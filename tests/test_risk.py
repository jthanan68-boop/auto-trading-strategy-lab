"""Risk limits are the last line of defence. Each cap gets a test that it holds."""

from __future__ import annotations

from datetime import timedelta

import pytest

from stratbot.config import RiskLimits
from stratbot.db import Database
from stratbot.models import (
    AccountState,
    Bar,
    HaltReason,
    OrderRequest,
    OrderStatus,
    Position,
    Side,
    utcnow,
)
from stratbot.risk import RiskManager


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "risk.sqlite3")


@pytest.fixture
def bar() -> Bar:
    return Bar(symbol="BTC", ts=utcnow(), open=100, high=100, low=100, close=100, volume=1)


def account(equity=1_000_000, positions=None, cash=None) -> AccountState:
    return AccountState(
        cash=cash if cash is not None else equity,
        positions=positions or {},
    )


def order(symbol="BTC", side=Side.BUY, qty=10.0, bar_ts=None) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=side,
        qty=qty,
        strategy="test",
        signal_bar_ts=bar_ts or utcnow(),
    )


def test_clean_order_is_approved(db, bar):
    risk = RiskManager(db, RiskLimits())
    decision = risk.check(order(), account(), bar)
    assert decision.approved


def test_halted_bot_refuses_everything(db, bar):
    risk = RiskManager(db, RiskLimits())
    risk.halt(HaltReason.MANUAL, "test")
    decision = risk.check(order(), account(), bar)
    assert not decision.approved
    assert "halted" in decision.reason


def test_duplicate_order_id_is_refused(db, bar):
    risk = RiskManager(db, RiskLimits())
    request = order()
    db.record_order(request, OrderStatus.FILLED)
    decision = risk.check(request, account(), bar)
    assert not decision.approved
    assert "duplicate" in decision.reason


def test_same_inputs_produce_the_same_order_id():
    ts = utcnow()
    assert order(bar_ts=ts).client_order_id == order(bar_ts=ts).client_order_id
    assert order(side=Side.BUY, bar_ts=ts).client_order_id != order(side=Side.SELL, bar_ts=ts).client_order_id


def test_position_cap_blocks_but_does_not_halt(db, bar):
    """Reaching the cap is normal. Halting would latch the bot off for good."""
    limits = RiskLimits(max_position_value=10_000, max_total_exposure=1_000_000)
    risk = RiskManager(db, limits)
    existing = Position(symbol="BTC", qty=100, avg_price=100, last_price=100)
    decision = risk.check(order(qty=10), account(positions={"BTC": existing}), bar)
    assert not decision.approved
    assert decision.halt is None
    assert not risk.halted, "a full position must not stop the bot trading"


def test_a_capped_bot_can_still_sell(db, bar):
    limits = RiskLimits(max_position_value=10_000, max_total_exposure=1_000_000)
    risk = RiskManager(db, limits)
    existing = Position(symbol="BTC", qty=100, avg_price=100, last_price=100)
    risk.check(order(qty=10), account(positions={"BTC": existing}), bar)
    exit_order = order(side=Side.SELL, qty=100)
    decision = risk.check(exit_order, account(positions={"BTC": existing}), bar)
    assert decision.approved, "being at the cap must never block a sell"


def test_a_loss_halt_still_lets_the_book_be_sold(db, bar):
    """Daily-loss halting locks new direction, not the exit door."""
    risk = RiskManager(db, RiskLimits(max_daily_loss=1_000))
    db.reset_day(equity=100_000)
    held = Position(symbol="BTC", qty=100, avg_price=100, last_price=100)
    book = AccountState(cash=88_000, positions={"BTC": held})
    sell = risk.check(order(side=Side.SELL, qty=100), book, bar)
    buy = risk.check(order(), book, bar)
    assert sell.approved, "a halted bot must still be able to exit a held position"
    assert not buy.approved
    assert buy.halt is HaltReason.DAILY_LOSS


def test_exposure_cap_blocks_but_does_not_halt(db, bar):
    limits = RiskLimits(max_position_value=1_000_000, max_total_exposure=10_000)
    risk = RiskManager(db, limits)
    held = Position(symbol="ETH", qty=10, avg_price=1000, last_price=1000)
    decision = risk.check(order(qty=10), account(positions={"ETH": held}), bar)
    assert not decision.approved
    assert decision.halt is None
    assert not risk.halted


def test_exposure_cap_is_enforced_across_symbols(db, bar):
    limits = RiskLimits(max_position_value=1_000_000, max_total_exposure=10_000)
    risk = RiskManager(db, limits)
    held = Position(symbol="ETH", qty=10, avg_price=1000, last_price=1000)
    decision = risk.check(order(symbol="BTC", qty=10), account(positions={"ETH": held}), bar)
    assert not decision.approved


def test_order_is_shrunk_rather_than_refused_when_there_is_headroom(db, bar):
    limits = RiskLimits(max_position_value=5_000, max_total_exposure=1_000_000)
    risk = RiskManager(db, limits)
    decision = risk.check(order(qty=100), account(), bar)
    assert decision.approved
    assert decision.allowed_qty == pytest.approx(50.0)


def test_tiny_order_is_refused_up_front(db, bar):
    limits = RiskLimits(min_order_value=500)
    risk = RiskManager(db, limits)
    decision = risk.check(order(qty=1), account(), bar)  # 1 unit at 100 = 100 notional
    assert not decision.approved
    assert "order below minimum size" in decision.reason


def test_cap_shaved_to_dust_is_refused(db, bar):
    # 10 left of headroom cannot become an order worth placing.
    limits = RiskLimits(max_position_value=10_000, max_total_exposure=1_000_000, min_order_value=500)
    risk = RiskManager(db, limits)
    existing = Position(symbol="BTC", qty=99.99, avg_price=100, last_price=100)
    decision = risk.check(order(qty=10), account(positions={"BTC": existing}), bar)
    assert not decision.approved
    assert "remaining headroom below minimum order size" in decision.reason


def test_stale_bar_halts_the_bot(db):
    risk = RiskManager(db, RiskLimits(max_bar_age_days=4))
    stale = Bar(symbol="BTC", ts=utcnow() - timedelta(days=6), open=1, high=1, low=1, close=100)
    decision = risk.check(order(), account(), stale)
    assert not decision.approved
    assert decision.halt is HaltReason.STALE_PRICE
    assert risk.halt_reason is HaltReason.STALE_PRICE


def test_a_daily_bar_a_few_hours_old_is_not_stale(db):
    """Daily candles are hours old by design; only a dead feed should halt."""
    risk = RiskManager(db, RiskLimits(max_bar_age_days=4))
    recent = Bar(symbol="BTC", ts=utcnow() - timedelta(hours=10), open=1, high=1, low=1, close=100)
    decision = risk.check(order(qty=10), account(), recent)
    assert decision.approved, "a 10-hour-old daily bar must still be tradeable"
    assert not risk.halted


def test_an_old_bar_warns_without_halting(db):
    risk = RiskManager(db, RiskLimits(max_bar_age_days=4, warn_bar_age_days=2.0))
    lagging = Bar(symbol="BTC", ts=utcnow() - timedelta(days=3), open=1, high=1, low=1, close=100)
    decision = risk.check(order(qty=10), account(), lagging)
    assert decision.approved
    assert not risk.halted
    kinds = [e["kind"] for e in db.recent_risk_events()]
    assert "stale_bar_warning" in kinds


def test_daily_loss_limit_halts_the_bot(db, bar):
    limits = RiskLimits(max_daily_loss=1_000)
    risk = RiskManager(db, limits)
    db.reset_day(equity=100_000)
    decision = risk.check(order(), account(equity=98_000), bar)
    assert not decision.approved
    assert decision.halt is HaltReason.DAILY_LOSS


def test_hourly_rate_limit_blocks(db, bar):
    limits = RiskLimits(max_orders_per_hour=3)
    risk = RiskManager(db, limits)
    for i in range(3):
        db.record_order(order(bar_ts=utcnow() - timedelta(minutes=i)), OrderStatus.FILLED)
    decision = risk.check(order(), account(), bar)
    assert not decision.approved
    assert decision.halt is HaltReason.ORDER_RATE


def test_stop_loss_forces_an_exit(db):
    limits = RiskLimits(stop_loss_pct=0.08)
    risk = RiskManager(db, limits)
    position = Position(symbol="BTC", qty=2, avg_price=100, last_price=85)
    acct = account(positions={"BTC": position})
    bars = {"BTC": Bar(symbol="BTC", ts=utcnow(), open=85, high=85, low=85, close=85)}
    exits = risk.check_positions(acct, bars)
    assert len(exits) == 1
    assert exits[0].side is Side.SELL
    assert exits[0].qty == 2
    assert exits[0].strategy == "risk-stop-loss"


def test_no_stop_loss_when_position_is_profitable(db):
    risk = RiskManager(db, RiskLimits(stop_loss_pct=0.08))
    position = Position(symbol="BTC", qty=2, avg_price=100, last_price=120)
    bars = {"BTC": Bar(symbol="BTC", ts=utcnow(), open=120, high=120, low=120, close=120)}
    assert risk.check_positions(account(positions={"BTC": position}), bars) == []


def test_unknown_orders_halt_the_bot(db):
    risk = RiskManager(db, RiskLimits())
    request = order()
    db.record_order(request, OrderStatus.UNKNOWN, "timeout")
    unknown = risk.check_unknown_orders()
    assert unknown == [request.client_order_id]
    assert risk.halt_reason is HaltReason.UNKNOWN_ORDER


def test_resume_clears_the_halt(db):
    risk = RiskManager(db, RiskLimits())
    risk.halt(HaltReason.MANUAL)
    risk.resume()
    assert not risk.halted
    assert risk.halt_reason is None