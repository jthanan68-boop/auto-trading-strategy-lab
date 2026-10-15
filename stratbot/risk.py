"""Risk layer. Every order passes through check() before reaching a broker.

The strategy proposes an order; this module can only refuse, never amend. All
caps come from config.RiskLimits and are not reachable from strategy code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from stratlab.costs import CostModel

from .config import RiskLimits
from .db import Database
from .models import (
    AccountState,
    Bar,
    HaltReason,
    OrderRequest,
    OrderStatus,
    Side,
    Position,
    utcnow,
)


@dataclass
class RiskDecision:
    approved: bool
    reason: str = ""
    halt: HaltReason | None = None
    # Set when the caller should shrink the order rather than just refuse it.
    allowed_qty: float | None = None


class RiskManager:
    def __init__(self, db: Database, limits: RiskLimits | None = None):
        self.db = db
        self.limits = limits or RiskLimits()

    # ---- state ---------------------------------------------------------
    @property
    def halted(self) -> bool:
        return bool(self.db.get_state("halted", False))

    @property
    def halt_reason(self) -> HaltReason | None:
        raw = self.db.get_state("halt_reason")
        return HaltReason(raw) if raw else None

    def halt(self, reason: HaltReason | str, detail: str = ""):
        # Coerce defensively: a crash inside the halt path would mean the bot
        # fails to stop, which is the worst possible outcome here.
        if not isinstance(reason, HaltReason):
            try:
                reason = HaltReason(reason)
            except ValueError:
                reason = HaltReason.BROKER_DOWN
        self.db.set_state("halted", True)
        self.db.set_state("halt_reason", reason.value)
        self.db.log_risk_event(reason.value, detail)

    def resume(self):
        self.db.set_state("halted", False)
        self.db.set_state("halt_reason", None)
        self.db.log_risk_event("resumed", "halt cleared by operator")

    # ---- pre-trade checks ----------------------------------------------
    def check(
        self,
        request: OrderRequest,
        account: AccountState,
        bar: Bar,
        cost: CostModel | None = None,
    ) -> RiskDecision:
        if self.halted:
            return RiskDecision(False, f"bot halted: {self.halt_reason.value}")

        if request.qty <= 0:
            return RiskDecision(False, "non-positive quantity")

        if self.db.order_exists(request.client_order_id):
            return RiskDecision(False, "duplicate order id, already submitted")

        age_days = bar.age_seconds / 86_400
        if age_days > self.limits.max_bar_age_days:
            self.halt(HaltReason.STALE_PRICE, f"{request.symbol} bar is {age_days:.1f} days old")
            return RiskDecision(False, "stale price data", HaltReason.STALE_PRICE)

        if age_days > self.limits.warn_bar_age_days:
            self.db.log_risk_event(
                "stale_bar_warning", f"{request.symbol} bar is {age_days:.1f} days old"
            )

        day_pnl = self.db.day_pnl(account.equity)
        if day_pnl <= -abs(self.limits.max_daily_loss):
            self.halt(HaltReason.DAILY_LOSS, f"day P/L {day_pnl:.2f} breached limit")
            return RiskDecision(False, "daily loss limit reached", HaltReason.DAILY_LOSS)

        hour_ago = utcnow() - timedelta(hours=1)
        if self.db.count_orders_since(hour_ago) >= self.limits.max_orders_per_hour:
            return RiskDecision(False, "hourly order rate limit reached", HaltReason.ORDER_RATE)

        day_open = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        if self.db.count_orders_since(day_open) >= self.limits.max_orders_per_day:
            return RiskDecision(False, "daily order rate limit reached", HaltReason.ORDER_RATE)

        if request.qty * bar.close < self.limits.min_order_value:
            return RiskDecision(False, "order below minimum size")

        price = bar.close
        existing = account.positions.get(request.symbol, Position(symbol=request.symbol))
        equity = account.equity if account.equity > 0 else 1.0

        if request.side is Side.BUY:
            cap = min(
                self.limits.max_position_value,
                equity * self.limits.max_position_pct_of_equity,
            )
            current_value = abs(existing.market_value)
            headroom = cap - current_value
            # Sitting at the cap is the cap working, not a fault. Refuse the order
            # but keep trading: halting here would latch the bot off permanently
            # the first time any position reached its limit.
            if headroom <= 0:
                self.db.log_risk_event(
                    "order_rejected", f"{request.symbol} at position cap ({current_value:,.0f})"
                )
                return RiskDecision(False, "position value cap reached")

            # Size against the price the order will actually fill at. Sizing on the
            # reference close and filling slipped overshoots the cap by the spread
            # on every fill, which is how exposure reached 300,029.94 on a
            # 300,000 cap.
            # Defaults to zero slippage when no cost model is supplied, which
            # keeps sizing conservative rather than optimistic.
            fill_price = cost.fill_price(price, "BUY") if cost else price
            exposure_headroom = self.limits.max_total_exposure - account.exposure
            if exposure_headroom <= 0:
                self.db.log_risk_event("order_rejected", "total exposure cap reached")
                return RiskDecision(False, "total exposure cap reached")

            # Cap the notional at the caller's intended notional, measured on the
            # reference close, then convert to a quantity at the fill price. Using
            # fill price in this min() would let the cap be exceeded by the spread,
            # because the requested quantity already embeds the reference price.
            # Hard ceiling: the quantity returned must satisfy
            #   current exposure + qty * fill_price <= max_total_exposure
            # computed at the price actually paid. Deriving it from the cap
            # headroom alone leaked a few rupees per order, which compounded into
            # a 20-point breach across a full book.
            requested_value = request.qty * price
            affordable_value = min(headroom, requested_value, self.limits.max_total_exposure)
            allowed_value = min(affordable_value, exposure_headroom)
            allowed_qty = max(0.0, allowed_value) / fill_price
            if allowed_value < self.limits.min_order_value:
                return RiskDecision(False, "remaining headroom below minimum order size")

            # Always hand back the fill-priced quantity. The caller sized its
            # request on the reference close, so passing the original quantity
            # through would fill more than asked for on every order that happens
            # to fit under the caps.
            return RiskDecision(True, "sized to fill price", allowed_qty=allowed_qty)

        return RiskDecision(True)

    # ---- position monitoring -------------------------------------------
    def check_positions(self, account: AccountState, bars: dict[str, Bar]) -> list[OrderRequest]:
        """Force exits for positions past the stop loss. Bypasses caps on purpose."""
        exits: list[OrderRequest] = []
        for symbol, position in account.positions.items():
            if not position.is_open:
                continue
            bar = bars.get(symbol)
            if bar is None:
                continue
            if position.unrealised_pct <= -abs(self.limits.stop_loss_pct) * 100:
                exits.append(
                    OrderRequest(
                        symbol=symbol,
                        side=Side.SELL,
                        qty=position.qty,
                        strategy="risk-stop-loss",
                        signal_bar_ts=bar.ts,
                        reason=f"stop loss at {position.unrealised_pct:.2f}%",
                        reference_price=bar.close,
                    )
                )
        return exits

    def check_unknown_orders(self) -> list[str]:
        """Orders we cannot resolve must halt the bot rather than be retried."""
        unknown = [
            o["client_order_id"]
            for o in self.db.open_orders()
            if o["status"] == OrderStatus.UNKNOWN.value
        ]
        if unknown:
            self.halt(HaltReason.UNKNOWN_ORDER, f"{len(unknown)} unresolved orders")
        return unknown