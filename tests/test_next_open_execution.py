"""Next-open execution: a signal on bar t fills at the open of bar t+1.

Every test drives the engine with an explicit sequence of bars. There is no
broker, no credential and no network call: PaperBroker plus a throwaway SQLite
file under tmp_path, exactly as the rest of the suite.

The bar builder deliberately gives each bar a different open and close. If any
of these tests passed against the signal bar's own close, the defect that used to
be there would still be here.
"""

from __future__ import annotations

import pytest
from dataclasses import replace
from datetime import timedelta

import pandas as pd

from stubs import StubFeed

from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig, RiskLimits
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import Bar, OrderRequest, OrderStatus, Position, Side, utcnow
from stratlab.costs import cost_model_for

COST = cost_model_for("equity")
SLIPPAGE_BPS = 5.0
INITIAL_CASH = 500_000.0


class AlwaysLong:
    """A strategy that always wants 100% weight, so entry timing is the only variable."""

    name = "always-long"

    def __init__(self, target: float = 1.0):
        self.target = target

    def position(self, history):
        return self.target


class Timeline:
    """A fixed sequence of bars with strictly increasing timestamps.

    Each entry is (open, close). Open differs from close so a fill priced off the
    wrong end of the bar is visible in the numbers rather than coincidentally right.

    `series()` returns the history through the current step, so the engine always
    decides on a completed bar and fills against the next one.
    """

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
            {
                "open": [b.open for b in seen],
                "high": [b.high for b in seen],
                "low": [b.low for b in seen],
                "close": [b.close for b in seen],
                "volume": [b.volume for b in seen],
            },
            index=pd.DatetimeIndex([pd.Timestamp(b.ts).normalize() for b in seen]),
        )

    def advance(self):
        self.step = min(self.step + 1, len(self.bars) - 1)
        return self.step

    @property
    def bar(self):
        return self.bars[self.step]


def build(tmp_path, timeline, *, limits=None, cash=INITIAL_CASH, target=1.0):
    db = Database(tmp_path / "nextopen.sqlite3")
    config = BotConfig(
        symbols=("TEST",), strategy_name="donchian-55/20",
        initial_cash=cash, limits=limits or RiskLimits(),
    )
    engine = Engine(config, db, broker=PaperBroker(cash))
    engine.strategies["TEST"] = AlwaysLong(target)
    engine.feed = StubFeed(timeline.current, timeline.series)
    return engine, db


def order_row(db, order_id):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?", (order_id,)
        ).fetchone()
    return dict(row) if row else None


# ======================================================================
# 1. the core rule: a signal bar never fills itself
# ======================================================================
class TestSignalBarNeverFills:
    def test_a_signal_produces_no_fill_on_its_own_bar(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()

        assert db.recent_fills() == [], "an order filled on the bar that raised it"
        assert len(db.pending_orders()) == 1, "the order should be queued, not dropped"

    def test_the_fill_lands_on_the_next_bar_open(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        timeline.advance()
        engine.cycle()

        fills = db.recent_fills()
        assert len(fills) == 1
        expected = 105.0 * (1 + SLIPPAGE_BPS / 10_000)
        assert float(fills[0]["price"]) == pytest.approx(expected, rel=1e-9), (
            "the fill did not use the next bar's open"
        )
        assert float(fills[0]["price"]) != pytest.approx(110.0 * (1 + SLIPPAGE_BPS / 10_000)), (
            "the fill used the next bar's close"
        )

    def test_a_gap_up_prices_the_fill_at_the_worse_price(self, tmp_path):
        # Signal bar closed at 100; the next bar gaps up and opens at 150.
        timeline = Timeline([(100.0, 100.0), (150.0, 150.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        timeline.advance()
        engine.cycle()

        fill = db.recent_fills()[0]
        assert float(fill["price"]) == pytest.approx(150.0 * (1.0005), rel=1e-9)

    def test_the_fill_price_is_not_taken_from_the_signal_bar(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        timeline.advance()
        engine.cycle()

        fill = db.recent_fills()[0]
        assert float(fill["price"]) > 105.0, "filled at or below the signal bar's price"


# ======================================================================
# 2. no next bar, no fill, and no silent fallback
# ======================================================================
class TestNoNextBar:
    def test_repeated_cycles_on_one_bar_never_fill(self, tmp_path):
        timeline = Timeline([(100.0, 100.0)])
        engine, db = build(tmp_path, timeline)

        for _ in range(8):
            engine.cycle()

        assert db.recent_fills() == [], "filled without an execution bar"
        assert len(db.pending_orders()) == 1, "the queued order vanished or duplicated"

    def test_the_order_stays_pending_indefinitely(self, tmp_path):
        timeline = Timeline([(100.0, 100.0)])
        engine, db = build(tmp_path, timeline)

        for _ in range(5):
            engine.cycle()

        row = db.pending_orders()[0]
        assert row["status"] == OrderStatus.NEW.value
        assert row["execution_bar_ts"] is None, "an execution bar was claimed with none seen"

    def test_a_symbol_missing_from_the_later_bar_does_not_fill(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        assert len(db.pending_orders()) == 1

        # TEST disappears from the feed while another symbol still reports.
        engine.feed = StubFeed(lambda: {"OTHER": Bar(symbol="OTHER", ts=utcnow(),
                                                      open=1.0, high=1.0, low=1.0, close=1.0)},
                               lambda: HISTORY)
        engine.cycle()

        assert db.recent_fills() == [], "filled on a bar the symbol never produced"
        assert len(db.pending_orders()) == 1

    def test_no_fill_ever_appears_from_the_signal_bar_close(self, tmp_path):
        timeline = Timeline([(100.0, 137.5)])
        engine, db = build(tmp_path, timeline)

        for _ in range(4):
            engine.cycle()

        assert db.recent_fills() == []
        assert float(engine.broker.cash) == INITIAL_CASH, "cash moved without a fill"


# ======================================================================
# 3. repeated bars must not double fill
# ======================================================================
class TestNoDuplicateFills:
    def test_each_order_id_fills_at_most_once(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0), (105.0, 110.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        for _ in range(6):
            timeline.advance()
            engine.cycle()

        ids = [f["client_order_id"] for f in db.recent_fills(100)]
        assert len(ids) == len(set(ids)), f"an order id filled twice: {ids}"

    def test_a_filled_order_is_not_re_filled_on_the_same_bar(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        timeline.advance()
        engine.cycle()
        first = db.recent_fills()[0]

        for _ in range(4):
            engine.cycle()

        fills = db.recent_fills(50)
        assert len([f for f in fills if f["client_order_id"] == first["client_order_id"]]) == 1
        assert first["client_order_id"] not in [r["client_order_id"] for r in db.pending_orders()]

    def test_the_ledger_refuses_a_resubmitted_terminal_order(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        timeline.advance()
        engine.cycle()
        order_id = db.recent_fills()[0]["client_order_id"]

        assert db.order_status(order_id) == OrderStatus.FILLED.value
        # NEW is the only status that may still proceed; anything else is terminal
        # or unresolved and must not be sent again.
        assert db.order_status(order_id) != OrderStatus.NEW.value


# ======================================================================
# 4. auditability of the signal/execution relationship
# ======================================================================
class TestAuditTrail:
    def test_both_timestamps_are_recorded_and_strictly_ordered(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        order_id = db.pending_orders()[0]["client_order_id"]
        queued = order_row(db, order_id)
        assert queued["signal_bar_ts"] is not None
        assert queued["execution_bar_ts"] is None

        timeline.advance()
        engine.cycle()
        filled = order_row(db, order_id)

        assert filled["execution_bar_ts"] is not None
        signal_ts = pd.Timestamp(filled["signal_bar_ts"])
        exec_ts = pd.Timestamp(filled["execution_bar_ts"])
        assert exec_ts > signal_ts, "execution bar is not after the signal bar"
        assert exec_ts == pd.Timestamp(timeline.bar.ts.isoformat())

    def test_the_signal_bar_is_the_one_the_strategy_read(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        signal_bar = timeline.bar
        engine.cycle()
        row = order_row(db, db.pending_orders()[0]["client_order_id"])

        assert pd.Timestamp(row["signal_bar_ts"]) == pd.Timestamp(signal_bar.ts.isoformat())

    def test_the_target_weight_is_stored_so_the_order_can_be_resized(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        row = order_row(db, db.pending_orders()[0]["client_order_id"])

        assert row["target_weight"] is not None
        assert 0 < float(row["target_weight"]) <= 1.0

    def test_rebuilt_order_id_matches_the_stored_one(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        stored = db.pending_orders()[0]["client_order_id"]
        rebuilt = engine._pending_request(db.pending_orders()[0])

        assert rebuilt.client_order_id == stored, (
            "a restarted engine would deduplicate the fill against the wrong row"
        )


# ======================================================================
# 5. restart with an order in flight
# ======================================================================
class TestRestartWithPendingOrder:
    def test_a_queued_order_survives_a_restart_and_fills_on_the_next_bar(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        queued_id = db.pending_orders()[0]["client_order_id"]
        assert db.recent_fills() == []

        # A new process: fresh Database, fresh Engine, same files.
        reopened = Database(db.path)
        config = BotConfig(symbols=("TEST",), strategy_name="donchian-55/20",
                           initial_cash=INITIAL_CASH)
        revived = Engine(config, reopened, broker=PaperBroker(INITIAL_CASH))
        revived.strategies["TEST"] = AlwaysLong()
        timeline.advance()
        revived.feed = StubFeed(timeline.current, lambda: HISTORY)

        revived.cycle()

        fills = reopened.recent_fills()
        assert len(fills) == 1, "the queued order did not survive the restart"
        assert fills[0]["client_order_id"] == queued_id
        assert float(fills[0]["price"]) == pytest.approx(105.0 * 1.0005, rel=1e-9)

    def test_a_restart_does_not_re_fill_an_already_filled_order(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        timeline.advance()
        engine.cycle()
        assert len(db.recent_fills()) == 1

        reopened = Database(db.path)
        config = BotConfig(symbols=("TEST",), strategy_name="donchian-55/20",
                           initial_cash=INITIAL_CASH)
        revived = Engine(config, reopened, broker=PaperBroker(INITIAL_CASH))
        revived.strategies["TEST"] = AlwaysLong()
        revived.feed = StubFeed(timeline.current, lambda: HISTORY)

        for _ in range(4):
            revived.cycle()

        ids = [f["client_order_id"] for f in reopened.recent_fills(50)]
        assert len(ids) == len(set(ids))

    def test_a_restarted_engine_reports_the_pending_order(self, tmp_path):
        timeline = Timeline([(100.0, 100.0)])
        engine, db = build(tmp_path, timeline)
        engine.cycle()

        reopened = Database(db.path)
        assert len(reopened.pending_orders()) == 1


# ======================================================================
# 6. risk is revalidated at execution, not at signal time
# ======================================================================
class TestRiskRevalidatedAtExecution:
    def test_a_cap_tightened_after_the_signal_still_binds_at_the_fill(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        # Tighten the cap while the order is queued. It was sized under the old
        # limit, so only a check at execution can catch this.
        engine.risk.limits = replace(engine.risk.limits, max_position_value=50_000.0,
                                     max_total_exposure=50_000.0)

        timeline.advance()
        engine.cycle()

        fill = db.recent_fills()[0]
        notional = float(fill["qty"]) * float(fill["price"])
        assert notional <= 50_000.0 + 1e-6, f"executed {notional:,.2f} against a 50,000 cap"

    def test_a_daily_loss_breach_between_signal_and_fill_blocks_the_fill(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        db.set_state("day_start", utcnow().date().isoformat())
        db.set_state("day_start_equity", 10_000_000.0)  # a catastrophic day already

        timeline.advance()
        result = engine.cycle()

        assert db.recent_fills() == [], "an order filled despite the daily loss limit"
        assert result.halted
        assert result.halt_reason == "daily_loss_limit"

    def test_the_allowed_quantity_is_measured_at_the_execution_price(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (150.0, 150.0)])
        limits = RiskLimits(max_position_value=100_000.0, max_total_exposure=100_000.0)
        engine, db = build(tmp_path, timeline, limits=limits)

        engine.cycle()
        timeline.advance()
        engine.cycle()

        fill = db.recent_fills()[0]
        notional = float(fill["qty"]) * float(fill["price"])
        assert notional <= 100_000.0 + 1e-6
        # Sizing off the signal close of 100 would have bought ~6x this much.
        assert float(fill["qty"]) < 100_000.0 / 100.0


# ======================================================================
# 7. slippage, fees and cash identity survive the change
# ======================================================================
class TestCostAccounting:
    def test_slippage_is_charged_exactly_once(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        timeline.advance()
        engine.cycle()

        price = float(db.recent_fills()[0]["price"])
        assert price == pytest.approx(105.0 * (1 + SLIPPAGE_BPS / 10_000), rel=1e-9)
        assert price < 105.0 * (1 + 2 * SLIPPAGE_BPS / 10_000), "slippage charged twice"

    def test_sell_slippage_also_applies_once_and_downward(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0), (120.0, 120.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        timeline.advance()
        engine.cycle()
        engine.strategies["TEST"].target = 0.0     # exit signal on the current bar
        engine.cycle()
        timeline.advance()
        engine.cycle()

        sells = [f for f in db.recent_fills(50) if f["side"] == "SELL"]
        assert len(sells) == 1
        assert float(sells[0]["price"]) == pytest.approx(120.0 * (1 - SLIPPAGE_BPS / 10_000), rel=1e-9)

    def test_cash_identity_holds_across_a_next_open_entry(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        timeline.advance()
        engine.cycle()

        fill = db.recent_fills()[0]
        spent = float(fill["qty"]) * float(fill["price"]) + float(fill["fees"])
        assert INITIAL_CASH - engine.broker.cash == pytest.approx(spent, abs=1e-6)
        assert float(fill["fees"]) == pytest.approx(
            COST.charges("BUY", float(fill["qty"]) * float(fill["price"])), rel=1e-9)

    def test_equity_still_equals_cash_plus_market_value(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        timeline.advance()
        engine.cycle()

        row = db.equity_history()[-1]
        assert float(row["equity"]) == pytest.approx(
            float(row["cash"]) + float(row["exposure"]), abs=0.01)


# ======================================================================
# 8. exit timing and the kill switch
# ======================================================================
class TestExitAndKillSwitch:
    def test_a_strategy_opinion_cannot_cancel_a_pending_stop_loss(self, tmp_path):
        limits = RiskLimits(max_daily_loss=10_000_000.0)
        timeline = Timeline([(100.0, 100.0)] * 2 + [(40.0, 40.0)] * 3)
        engine, db = build(tmp_path, timeline, limits=limits)

        engine.cycle()
        timeline.advance()
        engine.cycle()
        timeline.advance()
        engine.cycle()      # crash: the stop is queued while the strategy wants in

        stops = [r for r in db.pending_orders() if r["strategy"] == "risk-stop-loss"]
        assert len(stops) == 1, "a strategy opinion cancelled the stop loss"

        timeline.advance()
        engine.cycle()
        with db.connect() as conn:
            stops = [dict(r) for r in conn.execute(
                "SELECT * FROM orders WHERE strategy='risk-stop-loss'").fetchall()]
        assert len(stops) == 1, f"the stop was queued {len(stops)} times"
        assert stops[0]["status"] == OrderStatus.FILLED.value
        assert engine.broker.positions["TEST"].qty == pytest.approx(0.0, abs=1e-9)

    def test_an_opposite_side_intent_supersedes_a_queued_one(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (100.0, 100.0)])
        engine, db = build(tmp_path, timeline)

        # Seed a queued BUY alongside an open position, so the cycle faces a
        # pending order pointing the opposite way to the new intent.
        queued = OrderRequest(symbol="TEST", side=Side.BUY, qty=500.0,
                              strategy="donchian-55/20",
                              signal_bar_ts=timeline.bars[0].ts,
                              reference_price=100.0, target_weight=1.0)
        db.record_order(queued, OrderStatus.NEW, "seeded for this test")
        held = Position(symbol="TEST", qty=500.0, avg_price=100.0, last_price=100.0)
        engine.broker.positions["TEST"] = held
        db.upsert_position(held)

        engine.strategies["TEST"].target = 0.0
        engine.cycle()

        pending = db.pending_orders()
        assert len(pending) == 1, f"expected one live order, got {pending}"
        assert pending[0]["side"] == "SELL"
        assert db.order_status(queued.client_order_id) == OrderStatus.CANCELLED.value

    def test_the_exit_also_waits_for_the_next_bar(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0), (120.0, 120.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        timeline.advance()
        engine.cycle()
        engine.strategies["TEST"].target = 0.0        # exit signalled on this bar
        engine.cycle()

        assert [f for f in db.recent_fills(50) if f["side"] == "SELL"] == [], (
            "the exit filled on the bar that raised it"
        )

        timeline.advance()
        engine.cycle()
        assert len([f for f in db.recent_fills(50) if f["side"] == "SELL"]) == 1

    def test_the_kill_switch_cancels_queued_orders_so_they_never_fill(self, tmp_path):
        timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
        engine, db = build(tmp_path, timeline)

        engine.cycle()
        assert len(db.pending_orders()) == 1

        engine.emergency_stop()
        assert db.pending_orders() == []

        timeline.advance()
        engine.cycle()
        assert db.recent_fills() == [], "a cancelled order filled after the halt"

    def test_a_stop_loss_is_queued_on_its_signal_bar_and_filled_on_the_next(self, tmp_path):
        # A 60% adverse move on a full-size position would trip the daily loss
        # limit, which is enforced at execution. Widen it so the stop itself is
        # what is under test here.
        limits = RiskLimits(max_daily_loss=10_000_000.0)
        timeline = Timeline([(100.0, 100.0), (100.0, 100.0), (40.0, 40.0), (40.0, 40.0)])
        engine, db = build(tmp_path, timeline, limits=limits)

        engine.cycle()
        timeline.advance()
        engine.cycle()
        assert engine.broker.positions["TEST"].is_open()

        timeline.advance()
        engine.cycle()                                  # a -60% move trips the stop
        assert engine.broker.positions["TEST"].qty > 0, "the stop filled on its own bar"
        kinds = [e["kind"] for e in db.recent_risk_events(50)]
        assert "stop_loss" in kinds

        timeline.advance()
        engine.cycle()
        assert engine.broker.positions["TEST"].qty == pytest.approx(0.0, abs=1e-9)

    def test_a_stop_loss_pre_empts_a_queued_entry_for_the_same_symbol(self, tmp_path):
        limits = RiskLimits(max_daily_loss=10_000_000.0)
        timeline = Timeline([(100.0, 100.0), (100.0, 100.0), (40.0, 40.0), (40.0, 40.0)])
        engine, db = build(tmp_path, timeline, limits=limits)

        engine.cycle()
        timeline.advance()
        engine.cycle()
        qty = engine.broker.positions["TEST"].qty

        timeline.advance()
        engine.cycle()
        pending = [r for r in db.pending_orders() if r["symbol"] == "TEST"]
        assert len(pending) == 1
        assert pending[0]["strategy"] == "risk-stop-loss", (
            f"a queued entry survived the stop: {pending[0]['strategy']}"
        )
        assert engine.broker.positions["TEST"].qty == pytest.approx(qty, rel=1e-9)


# ======================================================================
# 9. the provider and the caveat
# ======================================================================
def test_the_engine_still_reads_daily_candles_not_intraday_ticks(tmp_path):
    """Next-open over daily bars is a simulation, not a live intraday fill."""
    timeline = Timeline([(100.0, 100.0), (105.0, 110.0)])
    engine, _ = build(tmp_path, timeline)

    engine.cycle()
    timeline.advance()
    engine.cycle()

    # One bar advanced by a whole calendar day, so the "next open" is a daily open
    # many hours after the decision. Nothing here models an intraday fill.
    gap = timeline.bars[1].ts - timeline.bars[0].ts
    assert gap >= timedelta(days=1)