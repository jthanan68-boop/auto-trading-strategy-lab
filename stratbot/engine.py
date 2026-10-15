"""The bot loop: read bars, ask the strategy, apply risk, simulate fills.

Order of operations is deliberate and not to be rearranged:
  reconcile -> risk gate -> strategy signal -> risk gate -> submit -> record

Reconciliation runs first so the bot never trades on a ledger it knows is wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from stratlab import data as price_data
from stratlab.strategies.library import BuyAndHold, DonchianBreakout, SmaCrossover

from .broker.feed import current_bars
from .broker.paper import BrokerUnavailable, PaperBroker
from .config import BotConfig
from .db import Database
from .models import (
    AccountState,
    Bar,
    HaltReason,
    OrderRequest,
    OrderStatus,
    Side,
    utcnow,
)
from .reconciler import reconcile
from .risk import RiskManager

STRATEGY_BUILDERS = {
    "donchian-55/20": DonchianBreakout,
    "sma-20/100": SmaCrossover,
    "buy-and-hold": BuyAndHold,
}


@dataclass
class CycleResult:
    cycle: int
    bars: dict[str, Bar]
    account: AccountState
    decisions: list[str]
    halted: bool
    halt_reason: str | None


class Engine:
    def __init__(self, config: BotConfig, db: Database, broker: PaperBroker | None = None):
        self.config = config
        self.db = db
        self.broker = broker or PaperBroker(config.initial_cash)
        self.risk = RiskManager(db, config.limits)
        from stratlab.costs import cost_model_for

        self.cost_model = cost_model_for("crypto" if "BTC" in config.symbols else "equity")
        self.strategies = {
            symbol: STRATEGY_BUILDERS[config.strategy_name]() for symbol in config.symbols
        }
        self.cycle_count = 0
        self.last_reconcile = 0.0
        self._seed()

    def _seed(self):
        account = self.broker.get_account()
        if self.db.get_state("day_start") is None:
            self.db.reset_day(account.equity)
            self.db.log_risk_event("engine_start", f"mode={self.config.is_paper}")

    # ---- helpers --------------------------------------------------------
    def _history(self, symbol: str) -> "pd.DataFrame":  # noqa: F821
        instrument = price_data.BY_SYMBOL[symbol]
        return price_data.load(instrument)

    def _record(self, request: OrderRequest, status: OrderStatus, reason: str = ""):
        self.db.record_order(request, status, reason)

    def _execute(self, request: OrderRequest, bar: Bar, bypass_risk: bool = False) -> str:
        if not bypass_risk:
            account = self.broker.get_account()
            decision = self.risk.check(request, account, bar, self.cost_model)
            if not decision.approved:
                self._record(request, OrderStatus.REJECTED, decision.reason)
                self.db.log_risk_event("order_rejected", f"{request.symbol} {request.side.value}: {decision.reason}")
                return f"rejected: {decision.reason}"
            if decision.allowed_qty is not None:
                request.qty = decision.allowed_qty

        self._record(request, OrderStatus.NEW)
        try:
            status, fill = self.broker.submit(request, bar.close)
        except BrokerUnavailable as exc:
            self.db.update_order_status(request.client_order_id, OrderStatus.UNKNOWN, str(exc))
            self.risk.halt(HaltReason.BROKER_DOWN, str(exc))
            return f"unknown: {exc}"

        if status is OrderStatus.UNKNOWN:
            self.risk.halt(HaltReason.UNKNOWN_ORDER, request.client_order_id)
            return "unknown"

        if fill is None:
            self.db.update_order_status(request.client_order_id, status)
            return f"{status.value.lower()}"

        self.db.record_fill(fill)
        self.db.update_order_status(request.client_order_id, OrderStatus.FILLED)
        self.db.upsert_position(self.broker.positions[request.symbol])

        return f"filled {fill.qty:.6f} @ {fill.price:.2f}"

    # ---- main cycle -----------------------------------------------------
    def cycle(self) -> CycleResult:
        self.cycle_count += 1
        decisions: list[str] = []

        try:
            bars = current_bars(self.config.symbols)
        except Exception as exc:
            self.risk.halt(HaltReason.BROKER_DOWN, f"feed down: {exc}")
            bars = {}

        if not bars:
            account = self.broker.get_account()
            return CycleResult(self.cycle_count, {}, account, ["no bars"], self.risk.halted, self._reason())

        prices = {s: b.close for s, b in bars.items()}
        self.broker.mark(prices)

        # 1. Reconcile before trusting the ledger.
        account = self.broker.get_account()
        mismatches = reconcile(self.db, account, self.risk)
        if mismatches:
            decisions.append(f"reconcile mismatch on {len(mismatches)} symbol(s), halted")
            self.db.record_equity(account.equity, account.cash, account.exposure, account.unrealised)
            return CycleResult(self.cycle_count, bars, account, decisions, True, self._reason())

        # 2. Daily loss check.
        if self.db.reset_day(account.equity):
            decisions.append("new trading day, P/L baseline reset")
        self._execute_stop_losses(account, bars, decisions)

        # 3. Strategy signals, each through the risk gate.
        if not self.risk.halted:
            account = self.broker.get_account()
            equity = account.equity if account.equity > 0 else 1.0

            # Collect every intent first. A strategy target is a weight for that
            # symbol, so five symbols all asking for 1.0 would request 500% of
            # equity; they have to be normalised against each other before any
            # order is sized, otherwise each symbol keeps bidding for the whole
            # account and re-issuing orders every cycle.
            intents: list[tuple[str, Bar, float, float]] = []
            for symbol, bar in bars.items():
                strategy = self.strategies.get(symbol)
                if strategy is None:
                    continue
                try:
                    history = self._history(symbol)
                    target = float(strategy.position(history))
                except Exception as exc:
                    decisions.append(f"{symbol}: strategy error {exc}")
                    continue

                if target != target:  # NaN means hold
                    continue

                held = account.positions.get(symbol)
                held_qty = held.qty if held else 0.0
                current_weight = held_qty * bar.close / equity
                intents.append((symbol, bar, min(max(target, 0.0), 1.0), current_weight))

            gross = sum(target for _, _, target, _ in intents)
            # The achievable ceiling is whichever binds first: the configured
            # gross limit, or whatever the total-exposure cap actually allows on
            # the current equity. Sizing to the larger of the two starves every
            # symbol past the cap, which re-issues the same blocked order forever.
            affordable = self.config.limits.max_total_exposure / equity
            gross_cap = min(self.config.max_gross_exposure, affordable)
            scale = 1.0
            if gross > gross_cap and gross > 0:
                scale = gross_cap / gross
                decisions.append(
                    f"targets requested {gross:.2f} of equity, scaled to "
                    f"{gross_cap:.2f} across {len(intents)} symbols"
                )

            for symbol, bar, target, current_weight in intents:
                scaled = target * scale
                if abs(scaled - current_weight) < 0.01:
                    continue

                side = Side.BUY if scaled > current_weight else Side.SELL
                qty = abs(scaled - current_weight) * equity / bar.close
                if side is Side.SELL:
                    qty = min(qty, current_weight * equity / bar.close)
                if qty <= 0:
                    continue

                request = OrderRequest(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    strategy=self.config.strategy_name,
                    signal_bar_ts=bar.ts,
                    reason=f"target {scaled:.4f} vs current {current_weight:.4f}",
                    reference_price=bar.close,
                )
                self.db.log_signal(
                    self.config.strategy_name, symbol, side.value, scaled, bar.close, bar.ts, request.reason
                )
                outcome = self._execute(request, bar)
                decisions.append(f"{symbol} {side.value} {qty:.6f} -> {outcome}")

        # 4. Unresolved orders halt the bot.
        self.risk.check_unknown_orders()

        # Re-mark after trading. Positions opened this cycle were marked at their
        # fill price, so snapshotting without this reports them as freshly bought
        # with zero unrealised P/L, which contradicts every later snapshot.
        account = self.broker.get_account()
        self.broker.mark(prices)
        account = self.broker.get_account()
        self.db.record_equity(account.equity, account.cash, account.exposure, account.unrealised)
        return CycleResult(self.cycle_count, bars, account, decisions, self.risk.halted, self._reason())

    def _execute_stop_losses(self, account: AccountState, bars: dict[str, Bar], decisions: list[str]):
        for request in self.risk.check_positions(account, bars):
            bar = bars[request.symbol]
            outcome = self._execute(request, bar, bypass_risk=True)
            decisions.append(f"STOP LOSS {request.symbol} -> {outcome}")
            self.db.log_risk_event("stop_loss", f"{request.symbol} at {request.reference_price:.2f}")

    def _reason(self) -> str | None:
        reason = self.risk.halt_reason
        return reason.value if reason else None

    # ---- operator actions ----------------------------------------------
    def emergency_stop(self) -> dict:
        """Halt, cancel, flatten. Must work even with the dashboard closed."""
        self.risk.halt(HaltReason.MANUAL, "operator pressed the stop button")
        cancelled = self.broker.cancel_all()
        account = self.broker.get_account()
        prices = {s: p.last_price for s, p in account.positions.items()}
        flattened: list[str] = []
        for request in self.broker.flatten(prices):
            bar = Bar(
                symbol=request.symbol,
                ts=utcnow(),
                open=request.reference_price,
                high=request.reference_price,
                low=request.reference_price,
                close=request.reference_price,
            )
            outcome = self._execute(request, bar, bypass_risk=True)
            flattened.append(f"{request.symbol}: {outcome}")
        self.db.record_equity(*[
            self.broker.get_account().equity,
            self.broker.get_account().cash,
            self.broker.get_account().exposure,
            self.broker.get_account().unrealised,
        ])
        return {"halted": True, "cancelled": cancelled, "flattened": flattened}

    def resume(self) -> dict:
        self.risk.resume()
        return {"halted": self.risk.halted}

    def run_forever(self, cycles: int | None = None, sleep_seconds: int | None = None, logger=None):
        interval = sleep_seconds if sleep_seconds is not None else self.config.poll_seconds
        emit = logger.info if logger else print
        count = 0
        while cycles is None or count < cycles:
            try:
                result = self.cycle()
                status = "HALTED" if result.halted else "ok"
                emit(
                    "cycle %s %s equity=%.2f exposure=%.2f",
                    result.cycle, status, result.account.equity, result.account.exposure,
                )
                for decision in result.decisions:
                    emit("    %s", decision)
            except Exception as exc:
                self.risk.halt(HaltReason.BROKER_DOWN, f"cycle error: {exc}")
                emit("cycle %s error: %s", count + 1, exc)
            count += 1
            if cycles is None or count < cycles:
                time.sleep(interval)