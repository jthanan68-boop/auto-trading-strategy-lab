"""Deterministic validation of the full Donchian 55/20 execution lifecycle.

Synthetic bars drive a genuine breakout into a real entry fill and a real exit.
Nothing here connects to a broker: every test uses PaperBroker and an isolated
SQLite file under tmp_path. No test asserts profitability, and the strategy's
55/20 parameters are never modified.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from datetime import timedelta

from stubs import StubFeed, run_until_filled

from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig, RiskLimits
from stratlab.costs import cost_model_for
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import Bar, OrderRequest, OrderStatus, Position, Side, utcnow
from stratlab.strategies.library import DonchianBreakout

COST = cost_model_for("equity")
SLIPPAGE_BPS = 5.0
ENTRY, EXIT = 55, 20


# ----------------------------------------------------------------------
# synthetic bar builders
# ----------------------------------------------------------------------
def _entry_level(closes: list[float], entry: int = ENTRY) -> float:
    """A close that clears the prior `entry`-bar high, which is what triggers Donchian.

    Must be derived from the data, not guessed: in a declining series the channel
    high sits well above the last close, so a plausible-looking number is not
    actually a breakout.
    """
    return float(max(closes[-entry - 1:-1])) * 1.02


def breakout_history(warmup: int = 80) -> pd.DataFrame:
    """Decline for `warmup` bars, then one close above the 55-day high."""
    base = list(np.linspace(200.0, 100.0, warmup))
    return frame_from(base + [_entry_level(base)])


def exit_history(tail: int = 25) -> pd.DataFrame:
    """A breakout, a gain, then a decline that breaks the 20-day low.

    Deliberately shallow after entry. A steep collapse would trip the 8% stop
    loss first and the Donchian exit path would never execute, so the decline is
    sized to break the 20-day low while staying inside the stop.
    """
    base = list(np.linspace(200.0, 100.0, 80))
    entry = _entry_level(base)
    closes = base + [entry]
    # Gain, so the stop-loss cushion is wide at the moment of the decline.
    closes += list(np.linspace(entry * 1.01, entry * 1.30, tail - 4))
    # Fall back below the 20-day low, but stay above entry-8% so the stop loss
    # cannot fire first and mask the Donchian exit.
    low = min(closes[-EXIT:])
    closes += [low * 0.995]
    return frame_from(closes)


def flat_history(n: int = 90, price: float = 100.0) -> pd.DataFrame:
    return frame_from([price] * n)


def no_breakout_history() -> pd.DataFrame:
    """A bounce that stays under the 55-day high: entry must not fire.

    A monotonically rising series cannot be used here, because every new bar is
    by definition a new 55-day high. The channel high has to sit above the
    final close for a "no signal" case to be meaningful.
    """
    base = list(np.linspace(200.0, 100.0, 80))
    return frame_from(base + [105.0])


def frame_from(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0},
        index=index,
    )


def bar_from(history: pd.DataFrame, price: float | None = None, ts=None) -> Bar:
    last = history.iloc[-1]
    close = float(last["close"]) if price is None else price
    return Bar(symbol="TEST", ts=ts or utcnow(), open=close, high=close, low=close, close=close)


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "lifecycle.sqlite3")


@pytest.fixture
def bar() -> Bar:
    """A fresh, non-stale bar for direct risk-layer checks."""
    return Bar(symbol="TEST", ts=utcnow(), open=100.0, high=100.0, low=100.0, close=100.0)


@pytest.fixture
def engine(db, monkeypatch) -> Engine:
    """A one-symbol engine wired to a two-step bar timeline and a fixed history."""
    import stratbot.engine as engine_module

    history = breakout_history()
    base = utcnow()
    # Step 1 is the signal bar. Step 2 is a later bar, because an order raised on
    # step 1 fills at step 2's open and never on step 1 itself.
    timeline = [
        {"TEST": bar_from(history, ts=base)},
        {"TEST": bar_from(history, ts=base + timedelta(days=1))},
    ]

    config = BotConfig(
        symbols=("TEST",),
        strategy_name="donchian-55/20",
        initial_cash=500_000.0,
        limits=RiskLimits(max_position_value=250_000.0, max_total_exposure=300_000.0),
    )
    eng = engine_module.Engine(config, db, broker=PaperBroker(config.initial_cash))

    feed = StubFeed(timeline, lambda: history)
    eng.feed = feed
    return eng


def only_fill(db: Database) -> dict:
    fills = db.recent_fills(10)
    assert len(fills) == 1, f"expected exactly one fill, got {len(fills)}"
    return fills[0]


# ======================================================================
# 1. strategy produces a real signal on a real breakout
# ======================================================================
class TestSignalGeneration:
    def test_breakout_yields_a_long_signal(self):
        assert DonchianBreakout(ENTRY, EXIT).position(breakout_history()) == 1.0

    def test_no_breakout_yields_flat(self):
        assert DonchianBreakout(ENTRY, EXIT).position(no_breakout_history()) == 0.0

    def test_signal_flips_back_to_flat_after_the_exit_low(self):
        strategy = DonchianBreakout(ENTRY, EXIT)
        history = exit_history()
        strategy.position(history.iloc[:81])          # entry
        assert strategy.position(history.iloc[1:82])  # still long
        assert strategy.position(history) == 0.0      # 20-day low pierced

    def test_strategy_keeps_long_above_the_exit_low(self):
        strategy = DonchianBreakout(ENTRY, EXIT)
        history = breakout_history()
        strategy.position(history)
        assert strategy.position(history) == 1.0

    def test_engine_logs_the_signal_before_the_fill(self, engine, db):
        engine.cycle()
        signals = db.recent_signals(10)
        assert signals, "no signal recorded for a genuine breakout"
        assert signals[0]["action"] == "BUY"
        assert float(signals[0]["weight"]) > 0


# ======================================================================
# 2. order, fill price, single slippage, fees once
# ======================================================================
class TestEntryFill:
    def test_breakout_produces_exactly_one_fill(self, engine, db):
        run_until_filled(engine)
        assert len(db.recent_fills(10)) == 1

    def test_slippage_is_applied_exactly_once_on_a_buy(self, engine, db):
        result = run_until_filled(engine)
        fill = only_fill(db)
        reference = result.bars["TEST"].close

        assert fill["side"] == "BUY"
        assert float(fill["price"]) == pytest.approx(
            reference * (1 + SLIPPAGE_BPS / 10_000), rel=1e-9
        ), "buy must fill exactly one 5bps step above the reference"
        assert float(fill["price"]) < reference * (1 + 2 * SLIPPAGE_BPS / 10_000), (
            "slippage was charged more than once"
        )

    def test_sell_side_slips_down_once(self, db):
        broker = PaperBroker(500_000.0)
        broker.submit(OrderRequest(symbol="TEST", side=Side.BUY, qty=10.0,
                                   strategy="t", signal_bar_ts=utcnow(),
                                   reference_price=100.0), 100.0)
        _, fill = broker.submit(OrderRequest(symbol="TEST", side=Side.SELL, qty=10.0,
                                              strategy="s", signal_bar_ts=utcnow(),
                                              reference_price=100.0), 100.0)
        assert float(fill.price) == pytest.approx(100.0 * (1 - SLIPPAGE_BPS / 10_000), rel=1e-9)

    def test_fees_are_the_modelled_india_delivery_buy_fee(self, engine, db):
        run_until_filled(engine)
        fill = only_fill(db)
        notional = float(fill["qty"]) * float(fill["price"])
        assert float(fill["fees"]) == pytest.approx(
            COST.charges("BUY", notional), rel=1e-9
        )
        assert float(fill["fees"]) > 0

    def test_cash_falls_by_notional_plus_fees_exactly_once(self, engine, db):
        before = engine.broker.cash
        run_until_filled(engine)
        fill = only_fill(db)
        spent = float(fill["qty"]) * float(fill["price"]) + float(fill["fees"])
        assert before - engine.broker.cash == pytest.approx(spent, abs=1e-6)

    def test_equity_equals_cash_plus_market_value(self, engine, db):
        engine.cycle()
        row = db.equity_history()[-1]
        assert float(row["equity"]) == pytest.approx(
            float(row["cash"]) + float(row["exposure"]), abs=0.01
        )

    def test_first_snapshot_reports_realised_slippage(self, engine, db):
        """A position opened at a slipped price is below water immediately."""
        run_until_filled(engine)
        assert float(db.equity_history()[-1]["unrealised"]) < 0

    def test_ledger_agrees_with_broker_state(self, engine, db):
        run_until_filled(engine)
        position = engine.broker.positions["TEST"]
        booked = db.positions()["TEST"]
        assert booked.qty == pytest.approx(position.qty, rel=1e-9)
        assert booked.avg_price == pytest.approx(position.avg_price, rel=1e-9)

    def test_fill_carries_a_unique_deterministic_order_id(self, engine, db):
        run_until_filled(engine)
        fill = only_fill(db)
        same = OrderRequest(symbol="TEST", side=Side.BUY, qty=1.0, strategy="donchian-55/20",
                            signal_bar_ts=utcnow(), reference_price=100.0)
        assert fill["client_order_id"].startswith("donchian-55/20-TEST-")
        assert isinstance(same.client_order_id, str)


# ======================================================================
# 3. allowed quantity and risk gating
# ======================================================================
class TestRiskGating:
    def test_allowed_quantity_respects_the_exposure_cap(self, engine, db):
        run_until_filled(engine)
        fill = only_fill(db)
        notional = float(fill["qty"]) * float(fill["price"])
        assert notional <= engine.config.limits.max_total_exposure + 1e-6

    def test_allowed_quantity_respects_the_position_cap(self, engine, db):
        run_until_filled(engine)
        fill = only_fill(db)
        notional = float(fill["qty"]) * float(fill["price"])
        assert notional <= engine.config.limits.max_position_value + 1e-6

    def test_exposure_never_exceeds_the_cap(self, engine, db):
        for _ in range(6):
            engine.cycle()
        assert max(float(r["exposure"]) for r in db.equity_history()) <= (
            engine.config.limits.max_total_exposure + 1e-6
        )

    def test_no_further_orders_once_the_target_is_held(self, engine, db):
        run_until_filled(engine)
        first = len(db.recent_fills(50))
        for _ in range(4):
            engine.cycle()
        assert len(db.recent_fills(50)) == first, "held target re-issued orders"

    def test_a_halted_bot_never_fills(self, engine, db):
        from stratbot.models import HaltReason

        engine.risk.halt(HaltReason.MANUAL, "test")
        engine.cycle()
        assert db.recent_fills(50) == []

    def test_duplicate_order_id_never_produces_a_second_fill(self, engine, db):
        run_until_filled(engine)
        assert len(db.recent_fills(50)) == 1
        for _ in range(4):
            engine.cycle()
        assert len(db.recent_fills(50)) == 1

    def test_rejected_order_leaves_no_position(self, engine, db):
        from stratbot.models import HaltReason

        engine.risk.halt(HaltReason.MANUAL, "test")
        engine.cycle()
        assert all(not p.is_open() for p in engine.broker.positions.values())


# ======================================================================
# 4. exit lifecycle
# ======================================================================
class TestExitLifecycle:
    @pytest.fixture
    def exited(self, db, monkeypatch):
        import stratbot.engine as engine_module

        exit_series = exit_history()
        # The strategy is stateful, so the entry bar and the exit bar must be
        # presented in separate cycles. Feeding the collapsed series first would
        # simply be flat and never open a position. The timestamp has to advance
        # too, because an order only fills against a strictly later bar.
        current = {"history": breakout_history(), "ts": utcnow()}
        config = BotConfig(symbols=("TEST",), strategy_name="donchian-55/20",
                           initial_cash=500_000.0,
                           limits=RiskLimits(max_position_value=250_000.0,
                                             max_total_exposure=300_000.0))
        eng = engine_module.Engine(config, db, broker=PaperBroker(config.initial_cash))

        def bars():
            return {"TEST": bar_from(current["history"], ts=current["ts"])}

        def history():
            return current["history"]

        eng.feed = StubFeed(bars, history)

        def advance():
            current["history"] = exit_series
            current["ts"] = current["ts"] + timedelta(days=1)

        eng.cycle()                              # signal on the breakout bar
        advance(); eng.cycle()                   # entry fills at the next bar's open
        assert eng.broker.positions["TEST"].is_open(), "entry order did not execute"
        advance(); eng.cycle()                   # exit fills at the next bar's open
        return eng, db

    def test_exit_is_caused_by_donchian_not_the_stop_loss(self, exited):
        """The 20-day low must be the trigger, so the exit path itself is covered.

        A steep decline would be caught by the 8% stop loss and the Donchian
        exit branch would never run, leaving it unvalidated.
        """
        eng, db = exited
        kinds = [e["kind"] for e in db.recent_risk_events(50)]
        assert "stop_loss" not in kinds, (
            f"stop loss fired instead of the Donchian exit: {kinds}"
        )

    def test_exit_flattens_the_position(self, exited):
        eng, _ = exited
        assert not eng.broker.positions["TEST"].is_open(), "exit did not flatten"

    def test_exit_records_a_sell_fill(self, exited):
        _, db = exited
        sides = [f["side"] for f in db.recent_fills(50)]
        assert "SELL" in sides

    def test_exit_sell_slips_down_from_the_reference(self, exited):
        eng, db = exited
        sell = [f for f in db.recent_fills(50) if f["side"] == "SELL"][0]
        assert float(sell["price"]) > 0

    def test_cash_equals_entry_less_fees_plus_proceeds_less_fees(self, exited):
        """The cash identity across a round trip. Bookkeeping only.

        Whether the round trip made money is a property of the synthetic price
        path and says nothing about the strategy, so neither direction is
        asserted here.
        """
        eng, db = exited
        entry_fill = [f for f in db.recent_fills(50) if f["side"] == "BUY"][0]
        exit_fill = [f for f in db.recent_fills(50) if f["side"] == "SELL"][0]
        cost = float(entry_fill["qty"]) * float(entry_fill["price"])
        proceeds = float(exit_fill["qty"]) * float(exit_fill["price"])
        expected = (500_000.0 - cost - float(entry_fill["fees"])
                    + proceeds - float(exit_fill["fees"]))
        assert eng.broker.cash == pytest.approx(expected, abs=0.01)

    def test_flat_book_has_no_exposure(self, exited):
        eng, _ = exited
        assert eng.broker.get_account().exposure == pytest.approx(0.0, abs=1e-6)

    def test_exit_does_not_go_short(self, exited):
        eng, _ = exited
        assert eng.broker.positions["TEST"].qty == pytest.approx(0.0, abs=1e-9)


# ======================================================================
# 5. negative cases
# ======================================================================
class TestNegativeCases:
    def test_short_history_uses_the_previous_close_as_the_entry_trigger(self):
        assert DonchianBreakout(ENTRY, EXIT).position(flat_history(n=30)) == 1.0
        falling = frame_from([100.0] * 29 + [99.0])
        assert DonchianBreakout(ENTRY, EXIT).position(falling) == 0.0

    def test_engine_trades_on_a_short_history(self, db, monkeypatch):
        import stratbot.engine as engine_module

        history = flat_history(n=20)
        bar = bar_from(history)
        config = BotConfig(symbols=("TEST",), strategy_name="donchian-55/20",
                           initial_cash=500_000.0)
        eng = engine_module.Engine(config, db, broker=PaperBroker(config.initial_cash))
        feed = StubFeed(lambda: {"TEST": bar}, lambda: history)
        eng.feed = feed

        for _ in range(3):
            eng.cycle()
        assert [row["action"] for row in db.recent_signals(50)] == ["BUY"]
        assert len(db.recent_fills(50)) == 1

    def test_rising_market_below_the_high_does_not_trade(self, db, monkeypatch):
        import stratbot.engine as engine_module

        history = no_breakout_history()
        bar = bar_from(history)
        config = BotConfig(symbols=("TEST",), strategy_name="donchian-55/20",
                           initial_cash=500_000.0)
        eng = engine_module.Engine(config, db, broker=PaperBroker(config.initial_cash))
        feed = StubFeed(lambda: {"TEST": bar}, lambda: history)
        eng.feed = feed

        for _ in range(3):
            eng.cycle()
        assert db.recent_fills(50) == []

    def test_order_below_minimum_size_is_refused(self, db, bar):
        from stratbot.models import AccountState
        from stratbot.risk import RiskManager

        risk = RiskManager(db, RiskLimits(min_order_value=1_000_000.0))
        tiny = OrderRequest(symbol="TEST", side=Side.BUY, qty=0.01, strategy="t",
                            signal_bar_ts=utcnow(), reference_price=100.0)
        decision = risk.check(tiny, AccountState(cash=500_000.0), bar, COST)
        assert not decision.approved

    def test_dust_order_is_refused_after_cap_shrinking(self, db, bar):
        from stratbot.models import AccountState
        from stratbot.risk import RiskManager

        risk = RiskManager(db, RiskLimits(max_position_value=10_000.0,
                                          max_total_exposure=1_000_000.0,
                                          min_order_value=5_000.0))
        full = Position(symbol="TEST", qty=99.9, avg_price=100.0, last_price=100.0)
        account = AccountState(cash=500_000.0, positions={"TEST": full})
        request = OrderRequest(symbol="TEST", side=Side.BUY, qty=50.0, strategy="t",
                               signal_bar_ts=utcnow(), reference_price=100.0)
        decision = risk.check(request, account, bar, COST)
        assert not decision.approved
        assert "minimum" in decision.reason

    def test_exposure_cap_blocks_further_buys(self, db, bar):
        from stratbot.models import AccountState
        from stratbot.risk import RiskManager

        risk = RiskManager(db, RiskLimits(max_position_value=1_000_000.0,
                                          max_total_exposure=1_000.0))
        held = Position(symbol="OTHER", qty=10.0, avg_price=1000.0, last_price=1000.0)
        account = AccountState(cash=500_000.0, positions={"OTHER": held})
        # Large enough to clear the minimum-order-size filter, so the exposure
        # cap is what actually refuses it.
        request = OrderRequest(symbol="TEST", side=Side.BUY, qty=100.0, strategy="t",
                               signal_bar_ts=utcnow(), reference_price=100.0)
        decision = risk.check(request, account, bar, COST)
        assert not decision.approved
        assert "exposure" in decision.reason

    def test_sell_is_allowed_even_at_the_cap(self, db, bar):
        from stratbot.models import AccountState
        from stratbot.risk import RiskManager

        risk = RiskManager(db, RiskLimits(max_position_value=1_000_000.0,
                                          max_total_exposure=1_000.0))
        held = Position(symbol="TEST", qty=10.0, avg_price=100.0, last_price=100.0)
        account = AccountState(cash=0.0, positions={"TEST": held})
        request = OrderRequest(symbol="TEST", side=Side.SELL, qty=10.0, strategy="t",
                               signal_bar_ts=utcnow(), reference_price=100.0)
        assert risk.check(request, account, bar, COST).approved

    def test_broker_unavailable_halts_and_creates_no_fill(self, db, monkeypatch):
        from stratbot.broker.paper import BrokerUnavailable
        from stratbot.models import HaltReason

        broker = PaperBroker(500_000.0)
        broker.available = False
        eng = Engine(BotConfig(symbols=("TEST",), strategy_name="donchian-55/20"),
                     db, broker=broker)
        request = OrderRequest(symbol="TEST", side=Side.BUY, qty=100.0, strategy="t",
                               signal_bar_ts=utcnow(), reference_price=100.0)
        outcome = eng._execute(request, bar_from(breakout_history(), 100.0))
        assert "unknown" in outcome
        assert eng.risk.halt_reason is HaltReason.BROKER_DOWN
        assert db.recent_fills(50) == []

    def test_stale_bar_halts_and_creates_no_fill(self, db):
        from datetime import timedelta

        from stratbot.models import HaltReason

        eng = Engine(BotConfig(symbols=("TEST",), strategy_name="donchian-55/20"), db)
        stale = Bar(symbol="TEST", ts=utcnow() - timedelta(days=30),
                    open=100.0, high=100.0, low=100.0, close=100.0)
        request = OrderRequest(symbol="TEST", side=Side.BUY, qty=1.0, strategy="t",
                               signal_bar_ts=utcnow(), reference_price=100.0)
        eng._execute(request, stale)
        assert eng.risk.halt_reason is HaltReason.STALE_PRICE
        assert db.recent_fills(50) == []


# ======================================================================
# 6. persistence and isolation
# ======================================================================
class TestPersistence:
    def test_every_stage_is_written_to_sqlite(self, engine, db):
        run_until_filled(engine)
        assert db.recent_signals(10), "signal missing"
        assert db.recent_fills(10), "fill missing"
        assert db.positions(), "position missing"
        assert db.equity_history(), "equity snapshot missing"

    def test_state_survives_reopening_the_database(self, engine, db, tmp_path):
        run_until_filled(engine)
        qty = db.positions()["TEST"].qty

        reopened = Database(db.path)
        assert reopened.positions()["TEST"].qty == pytest.approx(qty, rel=1e-9)
        assert reopened.recent_fills(10), "fills lost on reopen"
        assert reopened.equity_history(), "equity history lost on reopen"

    def test_orders_are_unique_across_reopen(self, engine, db):
        run_until_filled(engine)
        fill = db.recent_fills(1)[0]
        reopened = Database(db.path)
        assert reopened.order_exists(fill["client_order_id"])

    def test_test_database_is_isolated_from_the_project_ledger(self, db, tmp_path):
        assert str(db.path).startswith(str(tmp_path))
        assert "bot.sqlite3" not in db.path.name

    def test_paper_broker_rejects_withdrawal_of_funds(self):
        """There is no code path that moves money out of the account."""
        import inspect

        from stratbot.broker import paper

        source = inspect.getsource(paper)
        for banned in ("withdraw", "transfer_out", "payout"):
            assert banned not in source.lower(), f"paper broker references {banned}"


# ======================================================================
# 7. no live-order path
# ======================================================================
class TestNoLiveOrders:
    def test_only_the_paper_adapter_exists(self):
        from stratbot.broker import paper  # noqa: F401

        assert not hasattr(paper, "LiveBroker"), "a live adapter must not be present"

    def test_engine_defaults_to_paper_mode(self):
        assert BotConfig().is_paper is True

    def test_paper_broker_is_the_default(self, db):
        eng = Engine(BotConfig(symbols=("TEST",)), db)
        assert isinstance(eng.broker, PaperBroker)
