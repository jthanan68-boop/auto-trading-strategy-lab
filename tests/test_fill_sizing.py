"""Queued intent is the fill contract: no re-expansion to the raw ask.

Regression for the live-smoke defect: the signals loop stored the *raw*
strategy target in `_live_targets`, so the first fill of a multi-symbol book
re-sized to that raw weight bounded only by hard caps — concentrating the whole
exposure cap in one symbol, rejecting the others, and then paying a reduce round
trip to converge. Fill-time sizing must see the scaled intent the queue
recorded.

This file verifies that across both trading styles, deriving the expected
allocation in-test from the same cap formulas the engine uses.
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

SYMBOLS = ("BTC", "RELIANCE")
INITIAL_CASH = 500_000.0


class AlwaysFull:
    """Every symbol asks for the whole book, as ORB did in the defect session."""

    name = "always-full"

    def __init__(self, target: float = 1.0):
        self.target = target

    def position(self, history: pd.DataFrame) -> float:
        return self.target


class MultiFeed(StubFeed):
    """Timeline of per-symbol bar dicts, with per-symbol decision history."""

    def __init__(self, bars, base):
        super().__init__(bars, None)
        self._base = base

    def _frame(self, symbol: str, upto: int) -> pd.DataFrame:
        closes = pd.Series(
            [b[symbol].close for b in self._bars[: upto + 1]],
            index=pd.DatetimeIndex([b[symbol].ts for b in self._bars[: upto + 1]]),
            dtype=float,
        )
        return pd.DataFrame(
            {"open": closes, "high": closes + 0.2, "low": closes - 0.2,
             "close": closes, "volume": 1000.0}, index=closes.index,
        )

    def history(self, symbol: str) -> pd.DataFrame:
        return self._frame(symbol, self.step)


def two_bar_timeline(base, spacing: timedelta):
    """A signal bar, then an execution bar one cadence apart."""
    from stratbot.models import Bar

    bars = []
    for step in range(2):
        bars.append({
            symbol: Bar(symbol=symbol, ts=base + step * spacing,
                        open=100.0, high=100.2, low=99.8, close=100.0,
                        volume=1000.0)
            for symbol in SYMBOLS
        })
    return bars


@pytest.mark.parametrize("style", ["daily", "intraday"])
def test_scaled_intent_governs_the_fill(tmp_path, monkeypatch, style):
    # Shared cap arithmetic (same formula the engine applies):
    #   affordable = expose_cap / equity;  gross_cap = min(gross_limit, affordable)
    #   scale      = gross_cap / gross;    per-symbol weight = target * scale
    config = BotConfig(
        symbols=SYMBOLS, strategy_name="orb-15min", trading_style=style,
        bar_interval="5m",
    )
    equity = INITIAL_CASH
    affordable = config.limits.max_total_exposure / equity
    gross_cap = min(config.max_gross_exposure, affordable)
    scale = gross_cap / len(SYMBOLS)          # gross = 1.0 per symbol

    if style == "intraday":
        import stratbot.engine as engine_mod

        monkeypatch.setattr(
            Engine, "_remaining_bars", lambda self, bar: 30)
        monkeypatch.setattr(engine_mod, "session_state",
                            lambda now, asset_class: "active")

    db = Database(tmp_path / f"scaled-{style}.sqlite3")
    engine = Engine(config, db,
                    broker=PaperBroker(INITIAL_CASH, trading_style=style))
    if style == "daily":
        # Daily cadence: the execution bar is the next day's open.
        base = utcnow().replace(tzinfo=None, hour=3, minute=45, second=0,
                                microsecond=0)
        spacing = timedelta(days=1)
    else:
        # Intraday: bars young enough to pass the minutes-freshness gate.
        base = utcnow().replace(tzinfo=None) - timedelta(minutes=15)
        spacing = timedelta(minutes=5)
    engine.feed = MultiFeed(two_bar_timeline(base, spacing), base)
    for symbol in SYMBOLS:
        engine.strategies[symbol] = AlwaysFull()

    # Cycle 1: both intents queued at the scaled share of equity.
    result = engine.cycle()
    assert not result.halted
    queued = db.pending_orders()
    assert [row["symbol"] for row in queued] == sorted(SYMBOLS)
    for row in queued:
        assert row["side"] == Side.BUY.value
        assert row["target_weight"] == pytest.approx(scale, abs=1e-6)

    # Cycle 2: both fills land at the queued weight, not the raw 1.0 ask.
    engine.feed.advance()
    result = engine.cycle()
    assert not result.halted
    assert db.pending_orders() == []

    buy_events = [e for e in db.recent_risk_events()
                  if e["kind"] == "order_rejected"]
    assert buy_events == [], f"cap starvation rejections: {buy_events}"
    sells = [f for f in db.recent_fills(20) if f["side"] == Side.SELL.value]
    assert sells == [], "a corrective reduce fill ran within two cycles"

    fills = [f for f in db.recent_fills(20) if f["side"] == Side.BUY.value]
    assert len(fills) == len(SYMBOLS)
    account = engine.broker.get_account()
    for symbol, fill in ((f["symbol"], f) for f in fills):
        notional = fill["price"] * fill["qty"]
        # The fill is the scaled share of the book, plus slippage, not the raw
        # 1.0 ask the old code re-expanded to (that filled ~2x this band).
        assert notional / equity == pytest.approx(scale, abs=0.01), (
            f"{symbol} filled at weight {notional / equity:.4f}, "
            f"expected the queued {scale:.4f}"
        )
    # And the book never crosses the exposure cap doing it.
    assert account.exposure <= config.limits.max_total_exposure * 1.01
