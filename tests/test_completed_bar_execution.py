"""Signal on a completed bar, fill at the real open of the next session.

The distinction this file pins down: a daily candle is published at the open and
revised until the close. Its **open** is a price that was actually traded, so it
can execute an order decided earlier. Its **high, low, close and volume** are
still being written, so a strategy may never read them.

Every test here is deterministic: no network, no broker, no wall-clock reliance
for the bar timestamps.
"""

from __future__ import annotations

import pandas as pd
import pytest
from dataclasses import replace
from datetime import timedelta

from stubs import StubFeed

from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig, RiskLimits
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import Bar, OrderStatus, utcnow
from stratlab.costs import cost_model_for

COST = cost_model_for("equity")
SLIPPAGE_BPS = 5.0
INITIAL_CASH = 500_000.0
SYMBOL = "TEST"


class RecordingStrategy:
    """Always long, and it records every history frame it is shown."""

    name = "recording-long"

    def __init__(self, target: float = 1.0):
        self.target = target
        self.seen: list[pd.DataFrame] = []

    def position(self, history):
        self.seen.append(history.copy())
        return self.target


class Session:
    """Bars on a daily cadence, timestamped the way a daily feed timestamps them.

    `closed_through` marks how many of the presented bars have finished. A bar
    beyond that is forming: its open is tradable, its close is not a fact.
    """

    def __init__(self, pairs, closed_through: int | None = None, symbol: str = SYMBOL):
        base = pd.Timestamp(utcnow()).normalize()
        self.symbol = symbol
        self.rows = list(pairs)
        self.bars = [
            Bar(symbol=symbol, ts=(base + pd.Timedelta(days=i)).to_pydatetime(),
                open=o, high=max(o, c) * 1.02, low=min(o, c) * 0.98, close=c)
            for i, (o, c) in enumerate(pairs)
        ]
        self.closed_through = len(self.bars) if closed_through is None else closed_through
        self.step = 0

    def _visible(self) -> list[Bar]:
        return self.bars[: self.step + 1]

    def bars_now(self) -> dict[str, Bar]:
        return {self.symbol: self.bars[self.step]}

    def series(self) -> pd.DataFrame:
        seen = self.bars[: min(self.step + 1, self.closed_through)]
        return pd.DataFrame(
            {
                "open": [b.open for b in seen], "high": [b.high for b in seen],
                "low": [b.low for b in seen], "close": [b.close for b in seen],
                "volume": [1.0] * len(seen),
            },
            index=pd.DatetimeIndex([pd.Timestamp(b.ts).normalize() for b in seen]),
        )

    def advance(self):
        self.step = min(self.step + 1, len(self.bars) - 1)
        return self.step

    @property
    def bar(self) -> Bar:
        return self.bars[self.step]


def build(tmp_path, session, *, limits=None, cash=INITIAL_CASH, target=1.0):
    db = Database(tmp_path / "sess.sqlite3")
    config = BotConfig(symbols=(SYMBOL,), strategy_name="donchian-55/20",
                       initial_cash=cash, limits=limits or RiskLimits())
    engine = Engine(config, db, broker=PaperBroker(cash))
    engine.strategies[SYMBOL] = RecordingStrategy(target)
    engine.feed = StubFeed(session.bars_now, session.series)
    return engine, db


def order_row(db, order_id) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE client_order_id=?",
                           (order_id,)).fetchone()
    return dict(row)


def only_pending(db):
    rows = db.pending_orders()
    assert len(rows) == 1, f"expected one queued order, got {len(rows)}"
    return rows[0]


# =====================================================================
# the decision sees a completed bar; the execution uses the next open
# =====================================================================
class TestCompletedBarSignalNextOpenFill:
    def test_a_signal_is_queued_and_does_not_fill_on_its_own_bar(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 150.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()

        assert db.recent_fills() == [], "filled on the bar that raised the signal"
        assert only_pending(db)["status"] == OrderStatus.NEW.value

    def test_the_fill_uses_the_next_bars_open_not_its_close(self, tmp_path):
        # Next bar opens at 150 and closes at 160: the two are far enough apart
        # that picking the wrong one cannot pass by accident.
        session = Session([(100.0, 100.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        session.advance()
        engine.cycle()

        fill = db.recent_fills()[0]
        assert float(fill["price"]) == pytest.approx(150.0 * 1.0005, rel=1e-9)
        assert float(fill["price"]) != pytest.approx(160.0 * 1.0005, rel=1e-9)

    def test_signal_and_execution_bars_are_recorded_and_ordered(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        queued = only_pending(db)
        assert queued["execution_bar_ts"] is None

        session.advance()
        engine.cycle()
        filled = order_row(db, queued["client_order_id"])

        assert filled["status"] == OrderStatus.FILLED.value
        signal_ts = pd.Timestamp(filled["signal_bar_ts"])
        exec_ts = pd.Timestamp(filled["execution_bar_ts"])
        assert exec_ts > signal_ts
        assert signal_ts == pd.Timestamp(session.bars[0].ts)
        assert exec_ts == pd.Timestamp(session.bars[1].ts)

    def test_the_signal_bar_is_the_last_completed_bar_not_the_execution_bar(self, tmp_path):
        """The forming bar's close must not become the decision's timestamp."""
        session = Session([(100.0, 100.0), (150.0, 160.0)], closed_through=1)
        engine, db = build(tmp_path, session)

        engine.cycle()                    # bar 1 is forming at this point
        queued = only_pending(db)

        assert pd.Timestamp(queued["signal_bar_ts"]) == pd.Timestamp(session.bars[0].ts), (
            "the order was tagged with the forming execution bar"
        )


# =====================================================================
# the strategy must not see the execution bar's unfinished numbers
# =====================================================================
class TestStrategyCannotSeeTheFuture:
    def test_history_excludes_the_forming_bar(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0)], closed_through=1)
        engine, db = build(tmp_path, session)

        session.advance()                  # bar 1 now exists but has not closed
        engine.cycle()

        seen = engine.strategies[SYMBOL].seen[-1]
        assert pd.Timestamp(seen.index[-1]) == pd.Timestamp(session.bars[0].ts), (
            "the strategy was shown the forming candle"
        )

    def test_no_forming_close_high_low_or_volume_reaches_the_strategy(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0)], closed_through=1)
        engine, db = build(tmp_path, session)

        session.advance()
        engine.cycle()

        seen = engine.strategies[SYMBOL].seen[-1]
        forming = session.bars[1]
        for column in ("open", "high", "low", "close"):
            assert float(seen[column].iloc[-1]) != float(getattr(forming, column)), (
                f"the forming {column} leaked into strategy history"
            )
        assert forming.close not in set(seen["close"].astype(float))

    def test_the_fill_still_happens_at_the_forming_bars_open(self, tmp_path):
        """Withholding the close must not withhold the open."""
        session = Session([(100.0, 100.0), (150.0, 160.0)], closed_through=1)
        engine, db = build(tmp_path, session)

        engine.cycle()
        session.advance()
        engine.cycle()

        fill = db.recent_fills()[0]
        assert float(fill["price"]) == pytest.approx(150.0 * 1.0005, rel=1e-9)

    def test_a_strategy_decision_is_unchanged_by_the_forming_bars_price(self, tmp_path):
        """A runaway intraday move must not rewrite yesterday's decision."""
        quiet = Session([(100.0, 100.0), (150.0, 160.0)], closed_through=1)
        crash = Session([(100.0, 100.0), (10.0, 12.0)], closed_through=1)

        engine_a, db_a = build(tmp_path / "a", quiet)
        engine_b, db_b = build(tmp_path / "b", crash)

        engine_a.cycle(); engine_b.cycle()

        target_a = pd.Timestamp(order_row(db_a, db_a.pending_orders()[0]["client_order_id"])
                                ["signal_bar_ts"])
        target_b = pd.Timestamp(order_row(db_b, db_b.pending_orders()[0]["client_order_id"])
                                ["signal_bar_ts"])
        assert target_a == target_b
        assert db_a.pending_orders()[0]["qty"] == pytest.approx(
            db_b.pending_orders()[0]["qty"], rel=1e-9)

    def test_the_feed_exposes_both_views_of_a_forming_session(self, tmp_path, monkeypatch):
        """One series, two readers: forming open tradable, forming close withheld."""
        from stratbot.broker.feed import MarketDataFeed
        from stratlab import data as price_data
        from stratlab.provenance import atomic_write_csv

        cache = tmp_path / "cache"
        cache.mkdir()
        monkeypatch.setattr(price_data, "CACHE_DIR", cache)

        forming = pd.Timestamp(utcnow()).normalize().tz_localize(None)
        closes = [100.0 + i for i in range(60)]
        frame = pd.DataFrame(
            {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0},
            index=pd.date_range(end=forming, periods=60, freq="B"),
        )
        atomic_write_csv(cache / "RELIANCE.csv", frame)

        # Force "the newest bar has not closed".
        monkeypatch.setattr(
            "stratlab.calendar.session_closed",
            lambda bar_date, asset_class, now=None: False,
        )

        feed = MarketDataFeed(("RELIANCE",), refresh_interval_minutes=999)
        served = feed.bars()["RELIANCE"]
        decided = feed.closed_bars()["RELIANCE"]
        history = feed.history("RELIANCE")

        assert pd.Timestamp(served.ts) == forming, (
            "the forming bar must reach the executor"
        )
        assert pd.Timestamp(decided.ts) < forming, (
            "the decision bar must be a finished one"
        )
        assert pd.Timestamp(history.index[-1]) == forming - pd.tseries.offsets.BDay(1)
        assert feed.deferred.get("RELIANCE") == str(forming.date())


# =====================================================================
# gaps, missing bars, repeated cycles
# =====================================================================
class TestGapsAndMissingBars:
    def test_a_gap_up_fills_at_the_worse_open(self, tmp_path):
        session = Session([(100.0, 100.0), (140.0, 145.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        session.advance()
        engine.cycle()

        assert float(db.recent_fills()[0]["price"]) == pytest.approx(140.0 * 1.0005, rel=1e-9)

    def test_a_gap_down_fills_at_the_better_open(self, tmp_path):
        session = Session([(100.0, 100.0), (80.0, 85.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        session.advance()
        engine.cycle()

        # Still a buy, so slippage is upward even though the open gapped down.
        assert float(db.recent_fills()[0]["price"]) == pytest.approx(80.0 * 1.0005, rel=1e-9)

    def test_repeated_cycles_on_one_bar_never_fill(self, tmp_path):
        session = Session([(100.0, 100.0)])
        engine, db = build(tmp_path, session)

        for _ in range(8):
            engine.cycle()

        assert db.recent_fills() == [], "filled without a later bar"
        assert only_pending(db)["execution_bar_ts"] is None

    def test_a_missing_next_bar_leaves_the_order_queued(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session)
        engine.cycle()

        # The symbol stops reporting entirely.
        engine.feed = StubFeed(lambda: {}, session.series)
        engine.cycle()

        assert db.recent_fills() == []
        assert only_pending(db)["status"] == OrderStatus.NEW.value

    def test_no_fill_uses_a_price_from_the_signal_bar(self, tmp_path):
        session = Session([(100.0, 137.5)])
        engine, db = build(tmp_path, session)

        for _ in range(4):
            engine.cycle()

        assert db.recent_fills() == []
        assert engine.broker.cash == INITIAL_CASH

    def test_a_filled_order_is_not_refilled_by_later_cycles(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0), (150.0, 160.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        for _ in range(5):
            session.advance()
            engine.cycle()

        ids = [f["client_order_id"] for f in db.recent_fills(100)]
        assert len(ids) == len(set(ids)), f"an order id filled twice: {ids}"


# =====================================================================
# restart with an order in flight
# =====================================================================
class TestRestart:
    def test_a_queued_order_survives_and_fills_at_the_next_open(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session)
        engine.cycle()

        queued_id = only_pending(db)["client_order_id"]
        assert db.recent_fills() == []

        reopened = Database(db.path)
        config = BotConfig(symbols=(SYMBOL,), strategy_name="donchian-55/20",
                           initial_cash=INITIAL_CASH)
        revived = Engine(config, reopened, broker=PaperBroker(INITIAL_CASH))
        revived.strategies[SYMBOL] = RecordingStrategy()
        session.advance()
        revived.feed = StubFeed(session.bars_now, session.series)

        revived.cycle()

        fills = reopened.recent_fills()
        assert len(fills) == 1
        assert fills[0]["client_order_id"] == queued_id
        assert float(fills[0]["price"]) == pytest.approx(150.0 * 1.0005, rel=1e-9)

    def test_a_restart_does_not_refill_a_finished_order(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session)
        engine.cycle()
        session.advance()
        engine.cycle()
        assert len(db.recent_fills()) == 1

        reopened = Database(db.path)
        config = BotConfig(symbols=(SYMBOL,), strategy_name="donchian-55/20",
                           initial_cash=INITIAL_CASH)
        revived = Engine(config, reopened, broker=PaperBroker(INITIAL_CASH))
        revived.strategies[SYMBOL] = RecordingStrategy()
        revived.feed = StubFeed(session.bars_now, session.series)

        for _ in range(3):
            revived.cycle()

        ids = [f["client_order_id"] for f in reopened.recent_fills(100)]
        assert len(ids) == len(set(ids))


# =====================================================================
# risk, slippage and fees still bind at the execution price
# =====================================================================
class TestRiskAndCostsAtExecution:
    def test_slippage_is_applied_exactly_once(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        session.advance()
        engine.cycle()

        price = float(db.recent_fills()[0]["price"])
        assert price == pytest.approx(150.0 * (1 + SLIPPAGE_BPS / 10_000), rel=1e-9)
        assert price < 150.0 * (1 + 2 * SLIPPAGE_BPS / 10_000), "slippage charged twice"

    def test_a_sell_slips_down_once(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0), (140.0, 145.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        session.advance()
        engine.cycle()                       # entry fills at the open of bar 1
        engine.strategies[SYMBOL].target = 0.0
        engine.cycle()                       # exit signalled on bar 1
        session.advance()
        engine.cycle()                       # exit fills at the open of bar 2

        sells = [f for f in db.recent_fills(50) if f["side"] == "SELL"]
        assert len(sells) == 1
        assert float(sells[0]["price"]) == pytest.approx(140.0 * 0.9995, rel=1e-9)

    def test_fees_are_charged_once_on_the_notional(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        session.advance()
        engine.cycle()

        fill = db.recent_fills()[0]
        notional = float(fill["qty"]) * float(fill["price"])
        assert float(fill["fees"]) == pytest.approx(COST.charges("BUY", notional), rel=1e-9)

    def test_cash_identity_holds(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        session.advance()
        engine.cycle()

        fill = db.recent_fills()[0]
        spent = float(fill["qty"]) * float(fill["price"]) + float(fill["fees"])
        assert INITIAL_CASH - engine.broker.cash == pytest.approx(spent, abs=1e-6)

    def test_a_cap_tightened_after_the_signal_still_binds_at_the_fill(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        engine.risk.limits = replace(engine.risk.limits,
                                     max_position_value=50_000.0,
                                     max_total_exposure=50_000.0)
        session.advance()
        engine.cycle()

        fill = db.recent_fills()[0]
        assert float(fill["qty"]) * float(fill["price"]) <= 50_000.0 + 1e-6

    def test_quantity_is_measured_at_the_execution_open_not_the_signal_close(self, tmp_path):
        limits = RiskLimits(max_position_value=100_000.0, max_total_exposure=100_000.0)
        session = Session([(100.0, 100.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session, limits=limits)

        engine.cycle()
        session.advance()
        engine.cycle()

        fill = db.recent_fills()[0]
        assert float(fill["qty"]) * float(fill["price"]) <= 100_000.0 + 1e-6
        assert float(fill["qty"]) < 100_000.0 / 100.0, (
            "the quantity was sized against the signal close"
        )


# =====================================================================
# the exit path obeys the same rule
# =====================================================================
class TestExitTiming:
    def test_the_exit_waits_for_the_next_bar_open(self, tmp_path):
        session = Session([(100.0, 100.0), (110.0, 112.0), (130.0, 135.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        session.advance()
        engine.cycle()                      # entry fills at 110

        engine.strategies[SYMBOL].target = 0.0
        engine.cycle()                      # exit signalled on this bar
        assert [f for f in db.recent_fills(50) if f["side"] == "SELL"] == [], (
            "the exit filled on the bar that raised it"
        )

        session.advance()
        engine.cycle()
        sells = [f for f in db.recent_fills(50) if f["side"] == "SELL"]
        assert len(sells) == 1
        assert float(sells[0]["price"]) == pytest.approx(130.0 * 0.9995, rel=1e-9)

    def test_the_kill_switch_cancels_queued_orders(self, tmp_path):
        session = Session([(100.0, 100.0), (150.0, 160.0)])
        engine, db = build(tmp_path, session)

        engine.cycle()
        assert only_pending(db)

        engine.emergency_stop()
        assert db.pending_orders() == []

        session.advance()
        engine.cycle()
        assert db.recent_fills() == [], "a cancelled order filled after the halt"