"""Queued BUYs re-validate their setup on the newest completed bar.

A rate-limited or slow-refresh order can wait bars (even an hour) behind its
signal bar; filling it blindly would book an entry whose setup is gone. The
three intraday presets opted in via `fill_revalidate`, so the engine recomputes
the strategy on fresh history at fill time and cancels a dead entry instead.
Strategies without the flag (daily mode, buy-and-hold) fill exactly as before,
and a strategy error at fill time fails open so a queue is never stranded.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from stubs import StubFeed

from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import Bar, OrderRequest, OrderStatus, Side, utcnow

SYMBOL = "NTPC"
INITIAL_CASH = 500_000.0


class ScriptedLong:
    """Follows the strategy contract; the test toggles the setup alive/dead."""

    fill_revalidate = True

    def __init__(self):
        self.alive = True

    def position(self, history: pd.DataFrame) -> float:
        return 1.0 if self.alive else 0.0


class Unguarded(ScriptedLong):
    fill_revalidate = False


class RevalidateFeed(StubFeed):
    """Per-step bar dicts with decision history that grows with the run."""

    def __init__(self, bars):
        super().__init__(bars, None)

    def history(self, symbol: str) -> pd.DataFrame:
        upto = min(self.step, len(self._bars) - 1)
        closes = pd.Series(
            [b[symbol].close for b in self._bars[: upto + 1]],
            index=pd.DatetimeIndex([b[symbol].ts for b in self._bars[: upto + 1]]),
            dtype=float,
        )
        return pd.DataFrame(
            {"open": closes, "high": closes + 0.2, "low": closes - 0.2,
             "close": closes, "volume": 1000.0}, index=closes.index,
        )


def timeline(base: pd.Timestamp, spacing: timedelta) -> list[dict]:
    # Flat bars: only the scripted strategy answers, prices stay out of the way.
    return [
        {SYMBOL: Bar(symbol=SYMBOL, ts=base, open=100.0, high=100.2,
                     low=99.8, close=100.1, volume=1000.0)},
        {SYMBOL: Bar(symbol=SYMBOL, ts=base + spacing, open=100.1, high=100.3,
                     low=99.9, close=100.2, volume=1000.0)},
    ]


@pytest.fixture
def intraday_clock(monkeypatch):
    import stratbot.engine as engine_mod

    monkeypatch.setattr(Engine, "_remaining_bars", lambda self, bar: 30)
    monkeypatch.setattr(engine_mod, "session_state",
                        lambda now, asset_class: "active")


def build_engine(tmp_path, strategy: ScriptedLong, portfolio: bool = False) -> Engine:
    config = BotConfig(
        symbols=(SYMBOL,), strategy_name="orb-15min", trading_style="intraday",
        bar_interval="5m",
    )
    if portfolio:
        config = replace(config, portfolio=replace(
            config.portfolio, enabled=True))
    engine = Engine(
        config, Database(tmp_path / "revalidate.sqlite3"),
        broker=PaperBroker(INITIAL_CASH, trading_style="intraday"),
    )
    base = utcnow().replace(tzinfo=None) - timedelta(minutes=15)
    engine.feed = RevalidateFeed(timeline(base, timedelta(minutes=5)))
    engine.strategies[SYMBOL] = strategy
    return engine


@pytest.mark.usefixtures("intraday_clock")
def test_dead_setup_buy_is_cancelled_before_fill(tmp_path):
    engine = build_engine(tmp_path, ScriptedLong())

    result = engine.cycle()
    pending = engine.db.pending_orders()
    assert len(pending) == 1
    order_id = pending[0]["client_order_id"]
    assert pending[0]["side"] == Side.BUY.value

    # The setup dies between queue and execution bar; the re-validation must
    # bin the order instead of booking an entry into it.
    engine.strategies[SYMBOL].alive = False
    engine.feed.advance()
    result = engine.cycle()

    assert engine.db.pending_orders() == []
    assert engine.db.order_status(order_id) == OrderStatus.CANCELLED.value
    assert engine.db.recent_fills(20) == []
    assert engine.broker.positions == {}
    assert not result.halted


@pytest.mark.usefixtures("intraday_clock")
def test_live_setup_still_fills_normally(tmp_path):
    engine = build_engine(tmp_path, ScriptedLong())

    engine.cycle()
    engine.feed.advance()
    engine.cycle()

    fills = engine.db.recent_fills(20)
    assert len(fills) == 1 and fills[0]["side"] == Side.BUY.value
    assert engine.db.pending_orders() == []
    assert engine.broker.positions[SYMBOL].qty > 0


@pytest.mark.usefixtures("intraday_clock")
def test_without_the_flag_a_dead_setup_fills_as_before(tmp_path):
    # Daily-mode strategies opt out; their fill behavior must not change.
    engine = build_engine(tmp_path, Unguarded())

    engine.cycle()
    assert len(engine.db.pending_orders()) == 1
    engine.strategies[SYMBOL].alive = False
    engine.feed.advance()
    engine.cycle()

    fills = engine.db.recent_fills(20)
    assert len(fills) == 1 and fills[0]["side"] == Side.BUY.value
    assert not any("cancelled" in f.get("reason", "") for f in fills)
    assert all(r["status"] != OrderStatus.CANCELLED.value
               for r in engine.db.pending_orders())
    assert engine.broker.positions[SYMBOL].qty > 0


@pytest.mark.usefixtures("intraday_clock")
def test_portfolio_entry_is_not_vetoed_by_the_symbol_strategy(tmp_path):
    # In portfolio mode the entry comes from a portfolio target, so the
    # per-symbol strategy's opinion must not cancel it at fill time.
    engine = build_engine(tmp_path, ScriptedLong(), portfolio=True)
    engine.strategies[SYMBOL].alive = False
    bar = Bar(symbol=SYMBOL, ts=utcnow().replace(tzinfo=None), open=100.0,
              high=100.2, low=99.8, close=100.0, volume=1000.0)
    request = OrderRequest(
        symbol=SYMBOL, side=Side.BUY, qty=10.0, strategy="orb-15min",
        signal_bar_ts=bar.ts, target_weight=0.1,
    )
    engine.db.record_order(request, OrderStatus.NEW)

    engine._fill(request, bar, 100.0)

    assert engine.db.order_status(request.client_order_id) == OrderStatus.FILLED.value
    assert engine.broker.positions[SYMBOL].qty > 0
