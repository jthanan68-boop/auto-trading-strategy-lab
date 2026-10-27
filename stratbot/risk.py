"""Risk layer. Every order passes through check() before reaching a broker.

The strategy proposes an order; this module can only refuse, never amend. All
caps come from config.RiskLimits and are not reachable from strategy code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from stratlab.costs import CostModel

from .config import RiskLimits
from .db import SUBMITTED_STATUSES, Database
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
    def check_bar_freshness(self, bar: Bar,
                            session_live: bool | None = None) -> RiskDecision:
        """Is this bar recent enough to act on at all?

        Separate from check() because staleness is a property of the data, not of
        an order. An order waiting for its execution bar sits behind a stale
        signal bar forever, so the gate has to fire on the cycle that sees the
        stale bar, not only when an order happens to be submittable.

        Intraday limits judge the age in minutes and enforce only while
        `session_live` says this symbol's asset class has published a bar
        today: on an exchange holiday or a weekend the whole class prints
        nothing, and yesterday's bars are then correct rather than stale. When
        the class is quiet the day-based checks below stay in charge.
        """
        max_minutes = self.limits.max_bar_age_minutes
        if max_minutes is not None and session_live:
            age_minutes = bar.age_seconds / 60
            if age_minutes > max_minutes:
                self.halt(HaltReason.STALE_PRICE,
                          f"{bar.symbol} bar is {age_minutes:.0f} minutes old")
                return RiskDecision(False, "stale price data", HaltReason.STALE_PRICE)
            warn_minutes = self.limits.warn_bar_age_minutes
            if warn_minutes is not None and age_minutes > warn_minutes:
                self.db.log_risk_event(
                    "stale_bar_warning", f"{bar.symbol} bar is {age_minutes:.0f} minutes old"
                )
            return RiskDecision(True)
        age_days = bar.age_seconds / 86_400
        if age_days > self.limits.max_bar_age_days:
            self.halt(HaltReason.STALE_PRICE, f"{bar.symbol} bar is {age_days:.1f} days old")
            return RiskDecision(False, "stale price data", HaltReason.STALE_PRICE)
        if age_days > self.limits.warn_bar_age_days:
            self.db.log_risk_event(
                "stale_bar_warning", f"{bar.symbol} bar is {age_days:.1f} days old"
            )
        return RiskDecision(True)

    @staticmethod
    def _reduce_only(request: OrderRequest, account: AccountState) -> bool:
        """A sell of a symbol genuinely held: reducing the book is not new trading."""
        if request.side is not Side.SELL:
            return False
        held = account.positions.get(request.symbol)
        return held is not None and held.qty > 1e-9

    def check(
        self,
        request: OrderRequest,
        account: AccountState,
        bar: Bar,
        cost: CostModel | None = None,
        price: float | None = None,
    ) -> RiskDecision:
        """Gate one order. `price` overrides the valuation price for next-open fills.

        Defaults to bar.close so every existing caller is unaffected; a pending
        order fills at the execution bar's open and must be sized against that,
        not against a close the order never trades at.
        """
        # A halt must never trap an open position: reducing a held symbol still
        # goes through every other gate, new buying does not.
        if self.halted and not self._reduce_only(request, account):
            return RiskDecision(False, f"bot halted: {self.halt_reason.value}")

        if request.qty <= 0:
            return RiskDecision(False, "non-positive quantity")

        # NEW is the pending state an order sits in between its signal bar and its
        # execution bar, so it must still be allowed through. Anything else is
        # terminal or unresolved, and re-submitting it risks a second fill.
        existing_status = self.db.order_status(request.client_order_id)
        if existing_status is not None and existing_status != OrderStatus.NEW.value:
            return RiskDecision(False, f"duplicate order id, already {existing_status}")

        freshness = self.check_bar_freshness(bar)
        if not freshness.approved:
            return freshness

        day_pnl = self.db.day_pnl(account.equity)
        if day_pnl <= -abs(self.limits.max_daily_loss) and not self._reduce_only(request, account):
            self.halt(HaltReason.DAILY_LOSS, f"day P/L {day_pnl:.2f} breached limit")
            return RiskDecision(False, "daily loss limit reached", HaltReason.DAILY_LOSS)

        # The rate gates count orders that reached the broker, not rows that are
        # merely queued: an intent recorded in the ledger is not broker activity,
        # and counting it made a pending book larger than the cap block every
        # fill, stop out of order, for good.
        hour_ago = utcnow() - timedelta(hours=1)
        if self.db.count_orders_since(
                hour_ago, SUBMITTED_STATUSES) >= self.limits.max_orders_per_hour:
            return RiskDecision(False, "hourly order rate limit reached", HaltReason.ORDER_RATE)

        day_open = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        if self.db.count_orders_since(
                day_open, SUBMITTED_STATUSES) >= self.limits.max_orders_per_day:
            return RiskDecision(False, "daily order rate limit reached", HaltReason.ORDER_RATE)

        reference = bar.close if price is None else price
        if reference <= 0:
            return RiskDecision(False, "no execution price available")

        if request.qty * reference < self.limits.min_order_value:
            return RiskDecision(False, "order below minimum size")

        price = reference
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
            # Headroom on cost basis (the price actually paid), not the marked
            # value: marked value can sit below the cap while committed capital
            # has already reached it, and each dust refill would then leak
            # slippage past the cap.
            committed = sum(
                p.qty * p.avg_price
                for p in account.positions.values()
                if p.is_open() and p.qty > 0
            )
            exposure_headroom = self.limits.max_total_exposure - committed
            if exposure_headroom <= 1e-9:
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
        """Force exits for positions past the stop loss. Bypasses caps on purpose.

        The breach is judged against the close of the bar supplied, never against
        the marked position: the mark follows the live candle, so reading it here
        would let an unfinished bar stand in for a completed daily close. The
        caller decides which bars qualify, which is where the daily-versus-
        intraday policy is applied.
        """
        exits: list[OrderRequest] = []
        for symbol, position in account.positions.items():
            if not position.is_open():
                continue
            bar = bars.get(symbol)
            if bar is None or not position.avg_price:
                continue
            breach_pct = (bar.close - position.avg_price) / position.avg_price * 100
            if breach_pct <= -abs(self.limits.stop_loss_pct) * 100:
                exits.append(
                    OrderRequest(
                        symbol=symbol,
                        side=Side.SELL,
                        qty=position.qty,
                        strategy="risk-stop-loss",
                        signal_bar_ts=bar.ts,
                        reason=f"stop loss at {breach_pct:.2f}%",
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