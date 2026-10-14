"""The contract every broker adapter must satisfy.

Any adapter - paper or live - inherits BrokerContract and its tests. An adapter
that cannot pass these is not fit to trade with, and the live gate should not
care about it.

The intent is that when someone eventually writes a live adapter, the same
suite that proves the paper simulator behaves correctly also proves the live
one does. Untested live code is the worst kind of code.
"""

from __future__ import annotations

import pytest

from ..models import Bar, OrderRequest, OrderStatus, Side, utcnow


class BrokerContract:
    """Mixin supplying the behavioural tests every adapter must pass."""

    def make_broker(self):
        raise NotImplementedError

    def make_bar(self, close: float = 100.0) -> Bar:
        return Bar(symbol="TEST", ts=utcnow(), open=close, high=close, low=close, close=close)

    def buy(self, qty: float = 1.0, strategy: str = "contract", ts=None) -> OrderRequest:
        return OrderRequest(
            symbol="TEST",
            side=Side.BUY,
            qty=qty,
            strategy=strategy,
            signal_bar_ts=ts or utcnow(),
            reference_price=100.0,
        )

    def sell(self, qty: float = 1.0, strategy: str = "contract", ts=None) -> OrderRequest:
        return OrderRequest(
            symbol="TEST",
            side=Side.SELL,
            qty=qty,
            strategy=strategy,
            signal_bar_ts=ts or utcnow(),
            reference_price=100.0,
        )

    # ---- account --------------------------------------------------------
    def test_account_starts_flat(self):
        account = self.make_broker().get_account()
        assert account.cash > 0
        assert all(not p.is_open() for p in account.positions.values())
        assert account.equity == pytest.approx(account.cash)

    def test_equity_equals_cash_plus_market_value(self):
        broker = self.make_broker()
        broker.submit(self.buy(), 100.0)
        account = broker.get_account()
        assert account.equity == pytest.approx(account.cash + account.market_value)

    # ---- orders ---------------------------------------------------------
    def test_buy_fills_and_creates_a_position(self):
        broker = self.make_broker()
        status, fill = broker.submit(self.buy(), 100.0)
        assert status is OrderStatus.FILLED
        assert fill is not None
        assert broker.positions["TEST"].qty > 0

    def test_buy_reduces_cash(self):
        broker = self.make_broker()
        before = broker.cash
        broker.submit(self.buy(), 100.0)
        assert broker.cash < before

    def test_sell_reduces_the_position(self):
        broker = self.make_broker()
        broker.submit(self.buy(qty=2.0), 100.0)
        broker.submit(self.sell(qty=1.0), 100.0)
        assert broker.positions["TEST"].qty == pytest.approx(1.0, rel=1e-3)

    def test_selling_more_than_held_never_goes_short(self):
        broker = self.make_broker()
        broker.submit(self.buy(qty=1.0), 100.0)
        broker.submit(self.sell(qty=10.0), 100.0)
        assert broker.positions["TEST"].qty >= 0

    def test_repeating_an_order_id_does_not_double_fill(self):
        broker = self.make_broker()
        request = self.buy(qty=1.0)
        broker.submit(request, 100.0)
        quantity = broker.positions["TEST"].qty
        _, fill = broker.submit(request, 100.0)
        assert fill is None
        assert broker.positions["TEST"].qty == pytest.approx(quantity)

    def test_never_spends_more_than_it_holds(self):
        broker = self.make_broker()
        broker.submit(self.buy(qty=10_000_000), 100.0)
        assert broker.cash >= 0, "broker overspent"

    def test_flatten_returns_every_open_position(self):
        broker = self.make_broker()
        broker.submit(self.buy(qty=1.0), 100.0)
        requests = broker.flatten({"TEST": 100.0})
        assert len(requests) == 1
        assert requests[0].side is Side.SELL

    def test_mark_updates_market_value(self):
        broker = self.make_broker()
        broker.submit(self.buy(qty=1.0), 100.0)
        before = broker.get_account().market_value
        broker.mark({"TEST": 200.0})
        assert broker.get_account().market_value > before