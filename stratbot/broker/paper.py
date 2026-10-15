"""Broker adapters.

Only PaperBroker exists in Phase 1. There is no live adapter, no credential
reader and no code path that could reach a real broker. Adding one later means
writing a new class here, not loosening this one.
"""

from __future__ import annotations

from typing import Protocol

from stratlab.costs import cost_model_for

from ..models import (
    AccountState,
    Fill,
    OrderRequest,
    OrderStatus,
    Position,
    utcnow,
)


class BrokerUnavailable(RuntimeError):
    pass


class BrokerAdapter(Protocol):
    name: str

    def get_account(self) -> AccountState: ...
    def submit(self, request: OrderRequest, bar_close: float) -> tuple[OrderStatus, Fill | None]: ...
    def cancel_all(self) -> int: ...


class PaperBroker:
    """Simulated fills at the next bar open, with slippage and statutory costs."""

    name = "paper"

    def __init__(self, initial_cash: float = 500_000.0):
        self.cash = float(initial_cash)
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, OrderStatus] = {}
        self.available = True

    # ---- account --------------------------------------------------------
    def get_account(self) -> AccountState:
        return AccountState(cash=self.cash, positions=dict(self.positions))

    def mark(self, prices: dict[str, float]):
        for symbol, position in self.positions.items():
            if symbol in prices:
                position.last_price = prices[symbol]

    # ---- orders ---------------------------------------------------------
    def submit(self, request: OrderRequest, bar_close: float) -> tuple[OrderStatus, Fill | None]:
        if not self.available:
            raise BrokerUnavailable("paper broker marked unavailable")

        order_id = request.client_order_id
        if order_id in self.orders:
            # Idempotency: a repeat returns the original outcome, never a second fill.
            status = self.orders[order_id]
            return status, None

        cost = cost_model_for("crypto" if request.symbol in {"BTC", "ETH"} else "equity")
        reference = bar_close if bar_close > 0 else request.reference_price
        # Slippage is applied once, here. The risk layer sizes quantities at the
        # fill price rather than the reference, so charging it again downstream
        # made every fill ~0.05% dearer than the exposure cap allowed.
        price = cost.fill_price(reference, request.side.value)

        if request.side.value == "BUY":
            notional = request.qty * price
            fees = cost.charges("BUY", notional)
            if notional + fees > self.cash:
                # The buffer must exceed the fee rate, which is ~0.22% buy-side for
                # India delivery. A 0.1% buffer leaves cash a few paise negative.
                affordable = 0.0
                for buffer in (1.01, 1.03, 1.06):
                    affordable = self.cash / (price * buffer)
                    notional = affordable * price
                    fees = cost.charges("BUY", notional)
                    if notional + fees <= self.cash:
                        break
                if affordable <= 0:
                    self.orders[order_id] = OrderStatus.REJECTED
                    return OrderStatus.REJECTED, None
                request.qty = affordable
            self.cash -= notional + fees
            self._apply_fill(request.symbol, request.qty, price, add=True)
        else:
            position = self.positions.get(request.symbol)
            if position is None or position.qty < request.qty:
                sellable = position.qty if position else 0.0
                if sellable <= 0:
                    self.orders[order_id] = OrderStatus.REJECTED
                    return OrderStatus.REJECTED, None
                request.qty = sellable
            notional = request.qty * price
            fees = cost.charges("SELL", notional)
            self.cash += notional - fees
            self._apply_fill(request.symbol, request.qty, price, add=False)

        fill = Fill(
            client_order_id=order_id,
            ts=utcnow(),
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            price=price,
            fees=fees,
        )
        self.orders[order_id] = OrderStatus.FILLED
        return OrderStatus.FILLED, fill

    def _apply_fill(self, symbol: str, qty: float, price: float, add: bool):
        position = self.positions.get(symbol) or Position(symbol=symbol)
        if add:
            total = position.cost_basis + qty * price
            position.qty += qty
            position.avg_price = total / position.qty if position.qty else 0.0
            position.opened_at = position.opened_at or utcnow()
        else:
            position.qty -= qty
        position.last_price = price
        self.positions[symbol] = position

    def cancel_all(self) -> int:
        cancelled = 0
        for order_id, status in list(self.orders.items()):
            if status is OrderStatus.NEW:
                self.orders[order_id] = OrderStatus.CANCELLED
                cancelled += 1
        return cancelled

    def flatten(self, prices: dict[str, float]) -> list[OrderRequest]:
        requests: list[OrderRequest] = []
        from ..models import Side

        for symbol, position in list(self.positions.items()):
            if not position.is_open:
                continue
            price = prices.get(symbol, position.last_price)
            requests.append(
                OrderRequest(
                    symbol=symbol,
                    side=Side.SELL,
                    qty=position.qty,
                    strategy="kill-switch",
                    signal_bar_ts=utcnow(),
                    reason="emergency stop",
                    reference_price=price,
                )
            )
        return requests