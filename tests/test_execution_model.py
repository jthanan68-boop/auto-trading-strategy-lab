"""The stochastic execution model layered onto the paper broker's fills.

The default broker must stay exactly as it was: historical tests pin its fill
price, so the model is opt-in by construction rather than by accident.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import timedelta

import pytest

from stratlab.costs import cost_model_for
from stratbot.broker.paper import ExecutionModel, PaperBroker
from stratbot.models import OrderRequest, Side, utcnow

SLIP_BPS = cost_model_for("equity").slippage_bps


def make_request(side: Side = Side.BUY, qty: float = 1.0, bar: int = 0) -> OrderRequest:
    # A distinct bar timestamp per request: the client order id is derived from
    # it, so identical timestamps are treated as retries.
    return OrderRequest(
        symbol="TEST", side=side, qty=qty, strategy="test",
        signal_bar_ts=utcnow() + timedelta(minutes=bar), reference_price=100.0,
    )


def buy_fill_price(execution_price: float = 100.0, qty: float = 1.0, cash: float = 100_000.0,
                   model: ExecutionModel | None = None) -> float:
    broker = PaperBroker(cash, execution_model=model)
    _, fill = broker.submit(make_request(qty=qty), execution_price)
    assert fill is not None
    return fill.price


def base_buy_price(execution_price: float = 100.0) -> float:
    return cost_model_for("equity").fill_price(execution_price, "BUY")


def test_default_broker_fill_is_exactly_the_cost_model_price():
    assert buy_fill_price() == pytest.approx(base_buy_price(), rel=1e-12)


def test_modelled_fills_leave_the_cost_model_price():
    model = ExecutionModel(noise_bps=5.0, seed=7)
    assert buy_fill_price(model=model) != pytest.approx(base_buy_price(), rel=1e-9)


def test_the_same_seed_reproduces_the_same_fills():
    model = ExecutionModel(noise_bps=5.0, seed=42)
    prices_a = [buy_fill_price(model=model) for _ in range(10)]
    prices_b = [buy_fill_price(model=model) for _ in range(10)]
    assert prices_a == prices_b


def test_noise_alone_stays_within_the_noise_band():
    """With the worse-fill event off, a buy never drifts more than a few sigma."""
    model = ExecutionModel(noise_bps=5.0, worse_fill_probability=0.0, seed=42)
    for _ in range(50):
        drifted_bps = (buy_fill_price(model=model) / 100.0 - 1) * 10_000
        assert abs(drifted_bps - SLIP_BPS) < 25, f"noise drifted {drifted_bps:.1f} bps"


def test_some_fills_are_materially_worse_than_the_cost_model():
    """With noise off, only the worse-fill event moves the price: 20% of fills."""
    model = ExecutionModel(noise_bps=0.0, worse_fill_probability=0.2, worse_fill_bps=15.0, seed=1)
    broker = PaperBroker(10_000_000.0, execution_model=model)
    base = base_buy_price()
    worse_count = 0
    for bar in range(100):
        _, fill = broker.submit(make_request(qty=10.0, bar=bar), 100.0)
        if fill.price > base * (1 + 10.0 / 10_000):
            worse_count += 1
    assert 10 < worse_count < 30, f"{worse_count} of 100 fills were worse than 10 bps"


def test_sells_fill_at_or_below_the_reference():
    model = ExecutionModel(noise_bps=0.0, worse_fill_probability=0.0, seed=3)
    broker = PaperBroker(100_000.0, execution_model=model)
    broker.submit(make_request(qty=2.0), 100.0)
    _, fill = broker.submit(make_request(side=Side.SELL, qty=1.0), 100.0)
    assert fill.price == pytest.approx(100.0 * (1 - SLIP_BPS / 10_000), rel=1e-12)


def test_a_fatter_model_never_overspends_the_broker():
    model = ExecutionModel(noise_bps=10.0, worse_fill_probability=0.2, worse_fill_bps=20.0, seed=11)
    broker = PaperBroker(100_000.0, execution_model=model)
    broker.submit(make_request(qty=10_000_000), 100.0)
    assert broker.cash >= 0
    assert broker.positions["TEST"].qty > 0


def test_a_repeated_order_id_still_returns_no_second_fill():
    model = ExecutionModel(noise_bps=5.0, worse_fill_probability=0.2, seed=5)
    broker = PaperBroker(100_000.0, execution_model=model)
    request = make_request()
    broker.submit(request, 100.0)
    qty = broker.positions["TEST"].qty
    _, fill = broker.submit(request, 100.0)
    assert fill is None
    assert broker.positions["TEST"].qty == pytest.approx(qty)


def test_the_engine_wires_the_config_model_into_its_broker(tmp_path):
    from stratbot.config import CONFIG
    from stratbot.db import Database
    from stratbot.engine import Engine

    model = ExecutionModel(seed=5)
    engine = Engine(dc_replace(CONFIG, execution_model=model), Database(tmp_path / "t.sqlite3"))
    assert engine.broker.execution_model is model


def test_the_default_config_keeps_the_deterministic_fill(tmp_path):
    from stratbot.config import CONFIG
    from stratbot.db import Database
    from stratbot.engine import Engine

    engine = Engine(CONFIG, Database(tmp_path / "t.sqlite3"))
    assert engine.broker.execution_model is None
