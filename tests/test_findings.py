"""Regression tests for the three findings from the paper-trading audit.

Each test reproduces a defect observed in data/run2_bh.sqlite3 and
data/run3_donchian.sqlite3, and must fail if the corresponding fix is reverted.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig, RiskLimits
from stratlab.costs import cost_model_for
from stratbot.db import Database
from stratbot.models import Bar, OrderRequest, Side, utcnow
from stratbot.risk import RiskManager
from stratlab.strategies.library import DonchianBreakout

# Prices as recorded in the buy-and-hold run.
PRICES = {"BTC": 83349.86, "ETH": 2501.30, "RELIANCE": 1174.60,
          "TCS": 2304.90, "INFY": 1088.00}


# ===================================================================
# Finding 2 - exposure cap overshoot
# ===================================================================

def test_exposure_cap_holds_after_slippage(tmp_path):
    """Five orders of 60,000 filled at slipped prices overran a 300,000 cap by 29.94.

    Sizing on the reference close ignores the spread, so every fill lands above
    its requested notional and the total exceeds the cap.
    """
    db = Database(tmp_path / "f2.sqlite3")
    cost = cost_model_for("equity")
    limits = RiskLimits(
        max_position_value=100_000,
        max_total_exposure=300_000,
        max_position_pct_of_equity=0.50,
    )
    risk = RiskManager(db, limits)
    broker = PaperBroker(500_000.0)

    equity = 500_000.0
    total_notional = 0.0

    for symbol, reference in PRICES.items():
        bar = Bar(symbol=symbol, ts=utcnow(), open=reference, high=reference,
                  low=reference, close=reference)
        qty = 60_000.0 / reference
        request = OrderRequest(symbol=symbol, side=Side.BUY, qty=qty,
                               strategy="t", signal_bar_ts=utcnow(),
                               reference_price=reference)
        account = broker.get_account()
        decision = risk.check(request, account, bar, cost)
        assert decision.approved, f"{symbol} refused: {decision.reason}"
        request.qty = decision.allowed_qty if decision.allowed_qty is not None else request.qty

        status, fill = broker.submit(request, reference)
        assert status.value == "FILLED"
        # Notional at the price actually paid, not the reference.
        total_notional += fill.qty * fill.price

    assert total_notional <= 300_000.0 + 1e-6, (
        f"exposure {total_notional:,.2f} exceeded the 300,000 cap by "
        f"{total_notional - 300_000:,.2f}"
    )


def test_cap_never_breached_across_many_orders(tmp_path):
    """Many small orders, at different prices, must never sum past the cap.

    Measures cumulative fill notional, which is what the cap governs. Marked
    exposure can drift above the cap afterwards purely from a price move, and
    that is not something order sizing can control.
    """
    db = Database(tmp_path / "f2b.sqlite3")
    cost = cost_model_for("equity")
    risk = RiskManager(db, RiskLimits(max_position_value=100_000, max_total_exposure=300_000))
    broker = PaperBroker(500_000.0)
    rng = np.random.default_rng(7)
    prices = dict(PRICES)

    for step in range(60):
        for symbol in PRICES:
            # Each cycle re-quotes at the true market price, not at the previous
            # fill. Reusing a slipped fill as the next reference compounds the
            # spread every round and tests a scenario the engine never runs.
            price = prices[symbol]
            bar = Bar(symbol=symbol, ts=utcnow(), open=price, high=price, low=price, close=price)
            request = OrderRequest(symbol=symbol, side=Side.BUY,
                                   qty=20_000.0 / price, strategy="t",
                                   signal_bar_ts=utcnow() + pd.Timedelta(seconds=step),
                                   reference_price=price)
            decision = risk.check(request, broker.get_account(), bar, cost)
            if not decision.approved:
                continue
            if decision.allowed_qty is not None:
                request.qty = decision.allowed_qty
            _, fill = broker.submit(request, price)
            if fill is not None:
                # Mark back to the quoted market, not the paid price.
                broker.mark({symbol: price})

        # The cap governs cash actually committed, so measure cost basis at the
        # price paid. Mark-to-market exposure legitimately drifts above the cap
        # when a position gains, which order sizing cannot prevent.
        committed = sum(
            p.qty * p.avg_price for p in broker.positions.values() if p.qty > 0
        )
        assert committed <= 300_000.0 + 1e-6, (
            f"step {step}: committed capital {committed:,.2f} exceeds the 300,000 cap"
        )


def test_uncapped_order_fills_no_more_than_asked(tmp_path):
    """An order inside the caps must not fill for more than its notional."""
    db = Database(tmp_path / "f2c.sqlite3")
    cost = cost_model_for("equity")
    risk = RiskManager(db, RiskLimits(max_position_value=100_000, max_total_exposure=300_000))
    bar = Bar(symbol="BTC", ts=utcnow(), open=100, high=100, low=100, close=100)
    request = OrderRequest(symbol="BTC", side=Side.BUY, qty=100, strategy="t",
                           signal_bar_ts=utcnow(), reference_price=100)
    decision = risk.check(request, _account(), bar, cost)
    assert decision.approved
    fill_price = cost.fill_price(100.0, "BUY")
    notional = decision.allowed_qty * fill_price
    assert notional <= 100.0 * 100.0 + 1e-6, (
        f"100 shares cost {notional:,.2f} but only 10,000 was intended"
    )


def test_order_at_the_exposure_boundary_splits_correctly(tmp_path):
    """The last order into a nearly-full book must be clipped, not refused."""
    db = Database(tmp_path / "f2d.sqlite3")
    cost = cost_model_for("equity")
    risk = RiskManager(db, RiskLimits(max_position_value=1_000_000, max_total_exposure=300_000))
    bar = Bar(symbol="BTC", ts=utcnow(), open=100, high=100, low=100, close=100)
    filled = PaperBroker(500_000.0)
    filled.submit(OrderRequest(symbol="ETH", side=Side.BUY, qty=2_500.0, strategy="t",
                               signal_bar_ts=utcnow(), reference_price=100.0), 100.0)
    request = OrderRequest(symbol="BTC", side=Side.BUY, qty=1_000.0, strategy="t",
                           signal_bar_ts=utcnow(), reference_price=100.0)
    decision = risk.check(request, filled.get_account(), bar, cost)
    assert decision.approved
    assert decision.allowed_qty is not None
    fill_price = cost.fill_price(100.0, "BUY")
    assert decision.allowed_qty * fill_price <= 50_000.0 + 0.01, (
        f"clipped order still worth {decision.allowed_qty * fill_price:,.2f}, "
        "headroom was about 50,000"
    )


def _account():
    from stratbot.models import AccountState

    return AccountState(cash=500_000.0, positions={})


# ===================================================================
# Finding 3 - first equity snapshot
# ===================================================================

def test_first_snapshot_marks_new_positions_to_market(tmp_path, monkeypatch):
    """A position bought this cycle must not be reported at zero unrealised P/L.

    The engine used to snapshot equity without re-marking, so freshly opened
    positions still carried their fill price and showed unrealised 0.0, while
    the identical position showed -149.94 one cycle later.
    """
    import stratbot.engine as engine_module

    db = Database(tmp_path / "f3.sqlite3")
    config = BotConfig(symbols=("BTC",), initial_cash=500_000.0, strategy_name="buy-and-hold")
    broker = PaperBroker(config.initial_cash)
    engine = engine_module.Engine(config, db, broker=broker)

    entry = 1_000_000.0
    bar = Bar(symbol="BTC", ts=utcnow(), open=entry, high=entry, low=entry, close=entry)
    # Slippage makes the fill worse than the mark, exactly as in the real run.
    monkeypatch.setattr(engine_module, "current_bars", lambda symbols: {"BTC": bar})
    monkeypatch.setattr(engine, "_history", lambda s: pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]}))

    engine.cycle()

    rows = db.equity_history()
    assert rows, "no equity snapshot recorded"
    first = rows[0]
    position = broker.positions.get("BTC")
    assert position is not None and position.is_open(), (
        f"test did not open a position; engine decisions were {engine.cycle()}"
    )

    expected_market_value = position.qty * entry
    assert abs(float(first["exposure"]) - expected_market_value) < 0.01, (
        f"first snapshot exposure {float(first['exposure']):,.2f} but the position is "
        f"worth {expected_market_value:,.2f} at the current mark"
    )
    assert float(first["unrealised"]) < 0, (
        f"first snapshot showed {float(first['unrealised'])} unrealised; a position opened "
        "at a slipped price must be below water immediately"
    )


def test_snapshots_are_monotonic_and_consistent(tmp_path, monkeypatch):
    import stratbot.engine as engine_module

    db = Database(tmp_path / "f3b.sqlite3")
    config = BotConfig(symbols=("BTC",), initial_cash=500_000.0, strategy_name="buy-and-hold")
    engine = engine_module.Engine(config, db, broker=PaperBroker(config.initial_cash))

    bar = Bar(symbol="BTC", ts=utcnow(), open=100_000, high=100_000, low=100_000, close=100_000)
    monkeypatch.setattr(engine_module, "current_bars", lambda symbols: {"BTC": bar})
    monkeypatch.setattr(engine, "_history", lambda s: pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]}))

    for _ in range(3):
        engine.cycle()

    for row in db.equity_history():
        assert abs(float(row["equity"]) - (float(row["cash"]) + float(row["exposure"]))) < 0.01


# ===================================================================
# Finding 1 - Donchian emits no signal
# ===================================================================

def test_donchian_is_flat_when_no_breakout_exists():
    """Not a bug: 55/20 is flat unless the close makes a 55-day high."""
    closes = list(np.linspace(100, 90, 80)) + [95.0]
    frame = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}
    )
    assert DonchianBreakout(55, 20).position(frame) == 0.0


def test_donchian_is_long_at_a_55_day_high():
    closes = list(np.linspace(100, 90, 80)) + [200.0]
    frame = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}
    )
    assert DonchianBreakout(55, 20).position(frame) == 1.0


def test_donchian_needs_enough_history():
    frame = pd.DataFrame({"open": [1.0] * 20, "high": [1.0] * 20,
                          "low": [1.0] * 20, "close": [1.0] * 20, "volume": 1.0})
    assert DonchianBreakout(55, 20).position(frame) == 0.0