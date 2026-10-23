"""Regression tests for the paper-trading correctness fixes.

Everything here is deterministic and synthetic. These bars are fixtures, not a
market data feed: no test is evidence of a real fill, and none touch a live feed
or broker.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from stubs import StubFeed

import run_bot
from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig, RiskLimits
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import Bar, Position, utcnow
from stratbot.single_instance import InstanceAlreadyRunning, InstanceLock, lock_path_for


class Long:
    """A strategy that always wants `target` of equity in one symbol."""

    name = "long"

    def __init__(self, target: float = 1.0):
        self.target = target

    def position(self, history):
        return self.target


class Steps:
    """A daily feed that separates the live candle from the finished ones.

    Mirrors the real MarketDataFeed contract: `history()` hands back only
    completed bars, while `bars()` also carries the forming bar, whose high,
    low, close and volume are still moving. `forming` optionally overrides the
    close the live candle is currently showing at a given step, which is how a
    breach that recovers before the close gets modelled.
    """

    def __init__(self, rows, forming: dict[int, float] | None = None,
                 symbol: str = "TEST"):
        base = pd.Timestamp(utcnow()).normalize()
        self.symbol = symbol
        self.forming = forming or {}
        self.bars = [
            Bar(symbol=symbol, ts=(base + pd.Timedelta(days=i)).to_pydatetime(),
                open=o, high=max(o, c), low=min(o, c), close=c)
            for i, (o, c) in enumerate(rows)
        ]
        self.step = 0

    def live(self) -> Bar:
        bar = self.bars[self.step]
        shown = self.forming.get(self.step)
        if shown is None:
            return bar
        return Bar(symbol=bar.symbol, ts=bar.ts, open=bar.open,
                   high=max(bar.open, shown), low=min(bar.open, shown), close=shown)

    def bars_now(self):
        return {self.symbol: self.live()}

    def series(self):
        """Completed bars only: the forming bar is deliberately withheld."""
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


def build(tmp_path, steps, *, limits=None, cash=500_000.0, target=1.0, **cfg):
    db = Database(tmp_path / "fix.sqlite3")
    config = BotConfig(symbols=("TEST",), strategy_name="donchian-55/20",
                       initial_cash=cash, limits=limits or RiskLimits(), **cfg)
    engine = Engine(config, db, broker=PaperBroker(cash))
    engine.strategies["TEST"] = Long(target)
    engine.feed = StubFeed(steps.bars_now, steps.series)
    return engine, db


def run(engine, steps, cycles):
    """Advance the feed and cycle, returning every decision line produced."""
    lines = []
    for _ in range(cycles):
        lines.extend(engine.cycle().decisions)
        steps.advance()
    return lines


FLAT = (100.0, 100.0)
#  bar:      0     1     2      3      4
UP = [FLAT, FLAT, (100.0, 100.0), FLAT, (120.0, 120.0)]
DOWN = [FLAT, FLAT, (100.0, 100.0), FLAT, (80.0, 80.0)]
#  A book large enough to be exited: the 20% mark-down on a default-sized
#  position would otherwise trip max_daily_loss and block the very exit under test.
NO_HALT = RiskLimits(max_daily_loss=10_000_000.0)


def orders_for(db, strategy: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE strategy = ? ORDER BY ts", (strategy,)
        ).fetchall()
    return [dict(r) for r in rows]



# =====================================================================
# 1. stop-loss data policy
# =====================================================================
class TestStopLossDataPolicy:
    def test_the_default_policy_is_completed_bars(self):
        assert BotConfig().stop_loss_intraday is False, (
            "a daily-bar engine must not advertise intraday stop protection"
        )

    def test_a_forming_candle_cannot_trigger_a_stop(self, tmp_path):
        # At step 3 the live candle reads -20% while the newest completed bar is flat.
        steps = Steps([FLAT] * 6, forming={3: 80.0})
        engine, db = build(tmp_path, steps, target=0.2)

        run(engine, steps, 4)                      # entry fills on step 2

        held = engine.broker.positions["TEST"]
        assert held.is_open(), "no position to protect"
        assert held.unrealised_pct < -15, "the forming candle was not marked down"
        assert db.pending_orders() == [], (
            "an unfinished candle triggered a stop under the daily-bar policy"
        )
        assert db.closed_trades() == [], "a stop was booked from an unfinished bar"

    def test_a_breach_that_recovers_before_the_close_does_not_trigger(self, tmp_path):
        # The live candle dips to 80 at step 3 but that session closes at 100.
        steps = Steps([FLAT] * 6, forming={3: 80.0})
        engine, db = build(tmp_path, steps, target=0.2)

        run(engine, steps, 5)                      # step 4 completes bar 3 at 100

        assert engine.broker.positions["TEST"].is_open(), "the position was stopped out"
        assert db.pending_orders() == [], (
            "a stop fired on an intrabar breach the daily close never confirmed"
        )
        assert db.closed_trades() == []

    def test_a_completed_bar_does_trigger_a_stop(self, tmp_path):
        # Bar 3 genuinely closed at 80, so the completed close breaches.
        steps = Steps([FLAT, FLAT, FLAT, (100.0, 80.0), FLAT])
        engine, db = build(tmp_path, steps, target=0.2)

        run(engine, steps, 5)                      # step 4 completes bar 3

        stops = orders_for(db, "risk-stop-loss")
        assert len(stops) == 1, "a completed daily loss did not trigger the stop"
        assert stops[0]["side"] == "SELL"

    def test_the_stop_records_its_trigger_bar_and_price(self, tmp_path):
        steps = Steps([FLAT, FLAT, FLAT, (100.0, 80.0), FLAT])
        engine, db = build(tmp_path, steps, target=0.2)

        run(engine, steps, 5)

        stop = orders_for(db, "risk-stop-loss")[0]
        assert pd.Timestamp(stop["signal_bar_ts"]) == pd.Timestamp(steps.bars[3].ts), (
            "the stop was tagged with the wrong trigger bar"
        )
        # The reference is the completed close, never the live mark.
        assert float(stop["reference_price"]) == pytest.approx(80.0), (
            "the stop was priced off something other than the completed close"
        )

        detail = [e["detail"] for e in db.recent_risk_events(20)
                  if e["kind"] == "stop_loss"][0]
        assert "policy=completed bar" in detail
        assert f"trigger_bar={pd.Timestamp(steps.bars[3].ts).date()}" in detail

    def test_the_stop_executes_at_a_later_bar(self, tmp_path):
        steps = Steps([FLAT, FLAT, FLAT, (100.0, 80.0), (75.0, 75.0)])
        engine, db = build(tmp_path, steps, target=0.2)

        run(engine, steps, 5)

        exits = [f for f in db.recent_fills(20) if f["side"] == "SELL"]
        assert len(exits) == 1, f"expected exactly one stop-out, got {len(exits)}"
        # Filled near bar 4's open of 75, not at the 80 close that raised the stop.
        assert 74.0 <= float(exits[0]["price"]) < 80.0, (
            f"the exit filled at {exits[0]['price']}, not near the following open"
        )
        assert not engine.broker.positions["TEST"].is_open()
        assert len(db.closed_trades()) == 1, "the stop-out booked no round trip"

    def test_the_intraday_policy_is_opt_in_and_recorded(self, tmp_path):
        steps = Steps([FLAT] * 6, forming={3: 80.0})
        engine, db = build(tmp_path, steps, target=0.2, stop_loss_intraday=True)

        run(engine, steps, 4)

        stops = orders_for(db, "risk-stop-loss")
        assert len(stops) == 1, "opting in did not enable the forming-candle trigger"
        assert pd.Timestamp(stops[0]["signal_bar_ts"]) == pd.Timestamp(steps.bars[3].ts)
        assert float(stops[0]["reference_price"]) == pytest.approx(80.0)
        detail = [e["detail"] for e in db.recent_risk_events(20)
                  if e["kind"] == "stop_loss"][0]
        assert "policy=forming candle" in detail

    def test_a_flat_position_is_never_stopped(self, tmp_path):
        # avg_price is set so only the is_open() guard can prevent the exit.
        steps = Steps([FLAT, FLAT, (20.0, 20.0), FLAT])
        engine, db = build(tmp_path, steps, target=0.0)
        engine.broker.positions["TEST"] = Position(
            symbol="TEST", qty=0.0, avg_price=100.0, last_price=20.0,
        )

        run(engine, steps, 3)

        assert orders_for(db, "risk-stop-loss") == [], (
            "a position with no quantity produced a stop-loss order"
        )


# =====================================================================
# 2. single-instance protection
# =====================================================================
_ACQUIRE_SNIPPET = (
    "import sys;sys.path.insert(0,'.');"
    "from stratbot.single_instance import InstanceLock,InstanceAlreadyRunning;"
    "\ntry:\n    InstanceLock(sys.argv[1]).acquire()\n    print('ACQUIRED')\n"
    "except InstanceAlreadyRunning:\n    print('REFUSED')\n"
)
_CRASH_SNIPPET = (
    "import sys;sys.path.insert(0,'.');"
    "from stratbot.single_instance import InstanceLock;"
    "InstanceLock(sys.argv[1]).acquire();"
    "print('ACQUIRED', flush=True);"
    "import os;os._exit(9)"
)
_MESSAGE_SNIPPET = (
    "import sys;sys.path.insert(0,'.');"
    "from stratbot.single_instance import InstanceLock,InstanceAlreadyRunning;"
    "\ntry:\n    InstanceLock(sys.argv[1]).acquire()\n"
    "except InstanceAlreadyRunning as exc:\n    print(exc)\n"
)


class TestSingleInstance:
    def test_the_lock_is_scoped_to_the_database_path(self, tmp_path):
        assert lock_path_for(tmp_path / "a.sqlite3").name == "a.sqlite3.lock"
        assert lock_path_for(tmp_path / "b.sqlite3").name == "b.sqlite3.lock"
        assert lock_path_for(tmp_path / "a.sqlite3") != lock_path_for(tmp_path / "b.sqlite3")

    def test_a_second_process_is_refused(self, tmp_path):
        db = tmp_path / "solo.sqlite3"
        holder = InstanceLock(db).acquire()
        try:
            child = subprocess.run(
                [sys.executable, "-c", _ACQUIRE_SNIPPET, str(db)],
                capture_output=True, text=True, cwd=str(Path.cwd()),
            )
            assert "REFUSED" in child.stdout, (
                f"a second process acquired the lock: {child.stdout} {child.stderr}"
            )
        finally:
            holder.release()

    def test_two_databases_can_be_held_at_once(self, tmp_path):
        first = InstanceLock(tmp_path / "one.sqlite3").acquire()
        second = InstanceLock(tmp_path / "two.sqlite3").acquire()
        try:
            assert first.held and second.held, "unrelated databases blocked each other"
        finally:
            first.release()
            second.release()

    def test_release_allows_reacquisition(self, tmp_path):
        db = tmp_path / "cycle.sqlite3"
        first = InstanceLock(db).acquire()
        assert first.held
        first.release()
        assert not first.held

        second = InstanceLock(db).acquire()
        assert second.held, "the lock could not be re-taken after release"
        second.release()

    def test_release_is_idempotent(self, tmp_path):
        lock = InstanceLock(tmp_path / "twice.sqlite3").acquire()
        lock.release()
        lock.release()

    def test_a_crash_does_not_leave_a_stale_lock(self, tmp_path):
        """The kernel drops an advisory lock when the process dies.

        A PID-file design leaves this behind and needs manual cleanup.
        """
        db = tmp_path / "crash.sqlite3"
        child = subprocess.run(
            [sys.executable, "-c", _CRASH_SNIPPET, str(db)],
            capture_output=True, text=True, cwd=str(Path.cwd()),
        )
        assert "ACQUIRED" in child.stdout, child.stderr
        assert child.returncode == 9, "the child did not die hard"

        recovered = InstanceLock(db).acquire()
        assert recovered.held, "a hard crash left a permanently stale lock"
        recovered.release()

    def test_a_refused_instance_reports_clearly(self, tmp_path):
        db = tmp_path / "msg.sqlite3"
        holder = InstanceLock(db).acquire()
        try:
            child = subprocess.run(
                [sys.executable, "-c", _MESSAGE_SNIPPET, str(db)],
                capture_output=True, text=True, cwd=str(Path.cwd()),
            )
            assert "already holds" in child.stdout
            assert "msg.sqlite3" in child.stdout
        finally:
            holder.release()

    def test_a_second_bot_instance_exits_before_touching_anything(self, tmp_path, monkeypatch):
        """End to end: the refusal happens before the ledger is even opened."""
        db_path = tmp_path / "second.sqlite3"
        holder = InstanceLock(db_path).acquire()
        try:
            monkeypatch.setattr(
                sys, "argv",
                ["run_bot.py", "--db", str(db_path), "--cycles", "1"],
            )
            assert run_bot.main() == 2, "the second bot instance was not refused"
        finally:
            holder.release()

        assert not db_path.exists(), (
            "the refused instance created the ledger; the lock is taken too late"
        )

    def test_the_real_ledger_is_writable_after_a_refusal(self, tmp_path):
        db_path = tmp_path / "recover.sqlite3"
        InstanceLock(db_path).acquire().release()

        Database(db_path).log_risk_event("probe", "after release")
        assert InstanceLock(db_path).acquire().held


# =====================================================================
# 3. restart telemetry
# =====================================================================
class TestRestartTelemetry:
    def test_every_start_is_recorded(self, tmp_path):
        db = Database(tmp_path / "restarts.sqlite3")
        config = BotConfig(symbols=("TEST",), initial_cash=500_000.0)

        for _ in range(4):
            Engine(config, db, broker=PaperBroker(500_000.0))

        starts = [e for e in db.recent_risk_events(50) if e["kind"] == "engine_start"]
        assert len(starts) == 4, f"recorded {len(starts)} of 4 restarts"

    def test_the_daily_baseline_is_not_re_initialised(self, tmp_path):
        db = Database(tmp_path / "baseline.sqlite3")
        config = BotConfig(symbols=("TEST",), initial_cash=500_000.0)

        Engine(config, db, broker=PaperBroker(500_000.0))
        db.set_state("day_start_equity", 123_456.0)
        day_start = db.get_state("day_start")

        Engine(config, db, broker=PaperBroker(500_000.0))

        assert db.get_state("day_start") == day_start
        assert db.get_state("day_start_equity") == 123_456.0, (
            "a restart reset the day's P/L baseline"
        )

    def test_telemetry_names_the_configuration(self, tmp_path):
        db = Database(tmp_path / "detail.sqlite3")
        Engine(BotConfig(symbols=("TEST",), initial_cash=250_000.0),
               db, broker=PaperBroker(250_000.0))

        detail = [e["detail"] for e in db.recent_risk_events(10)
                  if e["kind"] == "engine_start"][0]
        assert "symbols=TEST" in detail
        assert "capital=250000.00" in detail
        assert "strategy=" in detail


# =====================================================================
# 4. feasible target weights
# =====================================================================
CAPPED = RiskLimits(max_position_value=100_000.0, max_total_exposure=300_000.0)


class TestFeasibleTargets:
    def test_an_unreachable_target_is_reported_with_the_limiting_rule(self, tmp_path):
        # Target 1.0 on 500k equity against a 100k position cap: only 0.2 is reachable.
        steps = Steps([FLAT] * 6)
        engine, db = build(tmp_path, steps, limits=CAPPED, target=1.0)

        joined = " ".join(run(engine, steps, 3))

        assert "unreachable" in joined, f"no diagnostic: {joined}"
        assert "max_position_value" in joined, f"the limiting rule was not named: {joined}"

    def test_the_effective_target_is_recorded_not_the_request(self, tmp_path):
        steps = Steps([FLAT] * 6)
        engine, db = build(tmp_path, steps, limits=CAPPED, target=1.0)

        run(engine, steps, 2)
        order = db.pending_orders()[0]
        signal = db.recent_signals(10)[-1]

        assert float(order["target_weight"]) == pytest.approx(0.2, abs=1e-6), (
            "the impossible target was stored instead of the achievable one"
        )
        assert float(signal["weight"]) == pytest.approx(0.2, abs=1e-6), (
            "the signal recorded the requested weight, not the achievable one"
        )
        # The existing diagnostic carries all three: requested, effective, and why.
        assert "requested 0.6000" in signal["reason"], signal["reason"]
        assert "max_position_value" in signal["reason"], signal["reason"]

    def test_an_unachievable_request_is_not_re_emitted_every_poll(self, tmp_path):
        steps = Steps([FLAT] * 8)
        engine, db = build(tmp_path, steps, limits=CAPPED, target=1.0)

        run(engine, steps, 3)                      # entry fills, capped at 0.2
        assert engine.broker.positions["TEST"].is_open()

        signals = len(db.recent_signals(200))
        for _ in range(6):
            engine.cycle()                         # same bar, polled repeatedly
        assert len(db.recent_signals(200)) == signals, (
            "the same impossible request was logged again on an unchanged bar"
        )
        assert db.pending_orders() == [], "queued an order the cap forbids"

    def test_a_new_completed_bar_still_re_evaluates(self, tmp_path):
        steps = Steps([FLAT] * 8)
        engine, db = build(tmp_path, steps, limits=CAPPED, target=0.05)

        run(engine, steps, 3)                      # a small entry fills

        engine.strategies["TEST"].target = 0.15    # a genuinely new decision
        before = len(db.recent_signals(200))
        run(engine, steps, 3)

        assert len(db.recent_signals(200)) > before, (
            "a real change on a new completed bar was suppressed"
        )

    def test_a_reduction_is_never_capped(self, tmp_path):
        """Going down, the position cap cannot bind: the whole position exits."""
        steps = Steps([FLAT] * 8)
        engine, db = build(tmp_path, steps, limits=CAPPED, target=1.0)

        run(engine, steps, 3)
        engine.strategies["TEST"].target = 0.0
        lines = run(engine, steps, 1)

        exits = [o for o in db.pending_orders() if o["side"] == "SELL"]
        assert exits, f"no exit was queued: {lines}"
        assert float(exits[0]["target_weight"]) == pytest.approx(0.0, abs=1e-9)
        assert "unreachable" not in " ".join(lines), "an exit was reported as capped"

    def test_duplicate_protection_is_untouched(self, tmp_path):
        steps = Steps([FLAT] * 8)
        engine, db = build(tmp_path, steps, target=0.5)

        run(engine, steps, 5)

        fills = db.recent_fills(100)
        assert len(fills) == 1, f"one entry became {len(fills)} fills"
        assert db.order_status(fills[0]["client_order_id"]) == "FILLED"


# =====================================================================
# 5. P&L reporting semantics
# =====================================================================
class TestProfitAndLossSemantics:
    def test_opening_a_position_realises_nothing(self, tmp_path):
        steps = Steps(UP)
        engine, db = build(tmp_path, steps, target=1.0)

        run(engine, steps, 3)

        assert engine.broker.positions["TEST"].is_open()
        assert engine.broker.cash < 500_000.0, "the fill should have moved cash"
        assert db.closed_pnl() == 0.0, "opening a position was reported as realised"
        assert db.closed_trades() == [], "an open position was booked as a closed trade"

    def test_the_components_are_reported_separately(self, tmp_path):
        steps = Steps(UP)
        engine, db = build(tmp_path, steps, target=1.0)

        run(engine, steps, 3)
        account = engine.broker.get_account()
        position = engine.broker.positions["TEST"]
        realised, fees = db.closed_pnl(), db.fees_paid()

        assert realised == 0.0
        assert fees > 0.0
        assert db.open_position_cost() == pytest.approx(
            position.qty * position.avg_price, rel=1e-6
        )
        assert account.equity == pytest.approx(account.cash + account.market_value, abs=0.01)
        # Equity fell by fees and by the mark, never by the notional deployed.
        assert 500_000.0 - account.equity == pytest.approx(fees - account.unrealised, abs=0.01), (
            "equity moved by something other than fees and unrealised"
        )

    def test_marking_down_does_not_realise(self, tmp_path):
        steps = Steps([FLAT, FLAT, (100.0, 100.0), (70.0, 70.0), FLAT])
        engine, db = build(tmp_path, steps, target=1.0)

        run(engine, steps, 4)
        account = engine.broker.get_account()

        assert account.unrealised < 0, "the position was not marked down"
        assert db.closed_pnl() == 0.0, "a mark-down was booked as realised"
        assert db.closed_trades() == []
        assert account.equity == pytest.approx(account.cash + account.market_value, abs=0.01)

    def test_closing_realises_the_round_trip_once(self, tmp_path):
        steps = Steps(UP)
        engine, db = build(tmp_path, steps, target=1.0)

        run(engine, steps, 3)
        position = engine.broker.positions["TEST"]
        entry_qty, entry_price = position.qty, position.avg_price

        engine.strategies["TEST"].target = 0.0
        run(engine, steps, 2)

        assert not engine.broker.positions["TEST"].is_open()
        trades = db.closed_trades()
        assert len(trades) == 1, f"expected one closed trade, got {len(trades)}"

        trade = trades[0]
        gross = (float(trade["exit_price"]) - float(trade["entry_price"])) * float(trade["qty"])
        assert float(trade["entry_price"]) == pytest.approx(entry_price, rel=1e-9)
        assert float(trade["qty"]) == pytest.approx(entry_qty, rel=1e-9)
        assert float(trade["pnl"]) == pytest.approx(gross - float(trade["fees"]), abs=0.01)
        assert float(trade["pnl"]) > 0, "a rising round trip booked a loss"
        assert db.closed_pnl() == pytest.approx(float(trade["pnl"]), abs=1e-6)

    def test_cash_reconciles_after_a_full_round_trip(self, tmp_path):
        steps = Steps(UP)
        engine, db = build(tmp_path, steps, target=1.0)

        run(engine, steps, 3)
        engine.strategies["TEST"].target = 0.0
        run(engine, steps, 2)

        account = engine.broker.get_account()
        realised, fees = db.closed_pnl(), db.fees_paid()

        assert account.market_value == 0.0
        assert account.unrealised == 0.0
        assert account.equity == pytest.approx(account.cash, abs=0.01)
        # With a flat book, cash moved only by the realised result, already net of fees.
        assert 500_000.0 + realised == pytest.approx(account.cash, abs=0.01), (
            f"cash does not reconcile: {account.cash:,.2f} vs "
            f"{500_000.0 + realised:,.2f} (fees {fees:,.2f})"
        )

    def test_a_losing_round_trip_reconciles_too(self, tmp_path):
        steps = Steps(DOWN)
        engine, db = build(tmp_path, steps, limits=NO_HALT, target=1.0)

        run(engine, steps, 3)
        engine.strategies["TEST"].target = 0.0
        run(engine, steps, 2)

        account = engine.broker.get_account()
        realised = db.closed_pnl()

        assert realised < 0, "a falling round trip booked a profit"
        assert 500_000.0 + realised == pytest.approx(account.cash, abs=0.01)
        assert account.equity == pytest.approx(account.cash, abs=0.01)
