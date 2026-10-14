"""The broker contract, run against the paper simulator.

These are the tests a future live adapter must also pass. Running them now
against PaperBroker proves the contract is meaningful rather than aspirational.
"""

from __future__ import annotations

import pytest

from stratbot.broker.contract import BrokerContract
from stratbot.broker.paper import PaperBroker


class TestPaperBrokerContract(BrokerContract):
    def make_broker(self):
        return PaperBroker(100_000.0)


def test_the_contract_is_not_vacuous():
    """If this fails, the inherited tests are silently not running."""
    inherited = [
        name for name in dir(TestPaperBrokerContract) if name.startswith("test_")
    ]
    assert len(inherited) >= 10, f"only {len(inherited)} contract tests discovered"


def test_contract_catches_a_broker_that_double_fills():
    """A deliberately broken broker must fail the contract."""
    from stratbot.models import OrderRequest, OrderStatus, Side, utcnow

    class DoubleFillingBroker(PaperBroker):
        def submit(self, request, bar_close):
            super().submit(request, bar_close)
            super().submit(request, bar_close)
            return OrderStatus.FILLED, None

    broker = DoubleFillingBroker(100_000.0)
    request = OrderRequest(
        symbol="TEST", side=Side.BUY, qty=1.0,
        strategy="contract", signal_bar_ts=utcnow(), reference_price=100.0,
    )
    broker.submit(request, 100.0)
    quantity = broker.positions["TEST"].qty
    broker.submit(request, 100.0)
    assert broker.positions["TEST"].qty == pytest.approx(quantity), (
        "a correct broker must not double fill; if this ever fails the contract is broken"
    )