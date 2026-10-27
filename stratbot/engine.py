"""The bot loop: read bars, ask the strategy, apply risk, simulate fills.

Order of operations is deliberate and not to be rearranged:
  reconcile -> freshness -> stop losses -> drain pending -> signals -> resolve

Reconciliation runs first so the bot never trades on a ledger it knows is wrong.

Execution follows the backtest: a signal formed on the close of bar t is queued
as a pending order and only ever filled at the OPEN of the bar after it. Filling
on the signal bar's own close trades on a price the decision could not have used.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from stratlab import data as price_data
from stratlab.calendar import (
    bars_remaining_in_session,
    interval_minutes,
    session_bounds,
    session_state,
)
from stratlab.strategies.intraday import IntradaySmaCross, OpeningRangeBreakout, VwapDip
from stratlab.strategies.library import BuyAndHold, DonchianBreakout, SmaCrossover

from .broker.feed import MarketDataFeed, current_bars
from .broker.paper import BrokerUnavailable, PaperBroker
from .broker.quotes import Quote, QuoteService
from .config import BotConfig
from .db import Database, _parse
from .models import (
    AccountState,
    Bar,
    Fill,
    HaltReason,
    OrderRequest,
    OrderStatus,
    Side,
    as_utc,
    utcnow,
)
from .reconciler import reconcile
from .risk import RiskManager

logger = logging.getLogger("stratbot")

STRATEGY_BUILDERS = {
    "donchian-55/20": DonchianBreakout,
    "sma-20/100": SmaCrossover,
    "buy-and-hold": BuyAndHold,
    "orb-15min": OpeningRangeBreakout,
    "vwap-dip": VwapDip,
    "sma-intraday-24/72": IntradaySmaCross,
}

# Daily-style presets read daily bars; intraday presets read the configured bar
# interval. PASSING asset_class matters for intraday symbols: NSE sessions end
# at 15:30 IST while crypto runs on the whole UTC day.
INTRADAY_STRATEGIES = {"orb-15min", "vwap-dip", "sma-intraday-24/72"}

# A stop loss, a kill switch or a session square-off must not be blocked by a
# cap it exists to respect. These execute immediately at the presented price
# rather than waiting a bar.
IMMEDIATE_STRATEGIES = {"risk-stop-loss", "kill-switch", "square-off"}


@dataclass
class CycleResult:
    cycle: int
    bars: dict[str, Bar]
    account: AccountState
    decisions: list[str]
    halted: bool
    halt_reason: str | None


class Engine:
    def __init__(self, config: BotConfig, db: Database, broker: PaperBroker | None = None,
                 feed: MarketDataFeed | None = None, quotes: QuoteService | None = None):
        self.config = config
        self.db = db
        self.broker = broker or PaperBroker(
            config.initial_cash, execution_model=config.execution_model,
            trading_style=config.trading_style,
        )
        # Reload the book from the ledger on startup so a restart cannot
        # create a paper-vs-ledger divergence that trips reconcile.
        try:
            restored = self.db.positions()
        except Exception as exc:
            logger.error("could not read the open book from the ledger: %s", exc)
            restored = {}
        for symbol, position in restored.items():
            self.broker.positions[symbol] = position
            # The broker starts on the whole capital, so a position carried over
            # from an earlier run has to bring the cash it already cost with it.
            # Without this, equity counts the position twice.
            if position.is_open():
                self.broker.cash -= position.cost_basis
        self.risk = RiskManager(db, config.limits)
        from stratlab.costs import cost_model_for

        if config.trading_style == "intraday":
            from stratlab.costs import INTRADAY as INTRADAY_MARKET

            # Session trades carry intraday STT (sell-side only) and a smaller
            # stamp duty; a crypto member keeps the spot model, which is the
            # same object for both markets.
            has_crypto = any(
                (price_data.BY_SYMBOL.get(s) is not None
                 and price_data.BY_SYMBOL[s].asset_class == "crypto")
                for s in config.symbols
            )
            self.cost_model = cost_model_for(
                "crypto" if has_crypto else "equity", market=INTRADAY_MARKET,
            )
        else:
            self.cost_model = cost_model_for("crypto" if "BTC" in config.symbols else "equity")
        self.feed = feed or MarketDataFeed(
            config.symbols, refresh_interval_minutes=config.refresh_interval_minutes,
            **({"interval": config.bar_interval, "days_history": config.days_history}
               if config.trading_style == "intraday" else {}),
        )
        # Optional. Without it the book is marked from the daily bars alone,
        # which is what the bot did before live quotes existed.
        self.quotes = quotes
        self.strategies = {
            symbol: STRATEGY_BUILDERS[config.strategy_name](
                **({"asset_class": price_data.BY_SYMBOL[symbol].asset_class}
                   if (config.strategy_name in INTRADAY_STRATEGIES
                       and symbol in price_data.BY_SYMBOL) else {}),
            )
            for symbol in config.symbols
        }
        self.cycle_count = 0
        self.last_reconcile = 0.0
        self._open_lots: dict = {}
        # The stance each strategy last computed, cleared to None when it is
        # holding. position() is stateful, so _fill must read this instead of
        # calling the strategy a second time in the same cycle.
        self._live_targets: dict[str, float | None] = {}
        # One quote_unavailable event per symbol per process: an open position
        # whose live quote is missing or stale is reported once, because the
        # same gap repeated at every poll would flood the ledger with noise.
        self._quote_missing_reported: set[str] = set()
        self._seed()

    def _seed(self):
        account = self.broker.get_account()
        if self.db.get_state("day_start") is None:
            self.db.reset_day(account.equity)
        # Telemetry is independent of the daily baseline. Gating this on day_start
        # meant a restart on an existing trading day recorded nothing at all, so
        # the ledger showed one engine_start no matter how many times it restarted.
        self.db.log_risk_event(
            "engine_start",
            f"mode={self.config.is_paper} symbols={','.join(self.config.symbols)} "
            f"capital={self.config.initial_cash:.2f} strategy={self.config.strategy_name}",
        )
        # Restored positions bring their entry cost so a later close - including
        # a forced one, because a crash can restart mid-session - still books
        # its round trip instead of vanishing with P/L of zero entry basis.
        for symbol, position in self.broker.positions.items():
            if position.is_open() and symbol not in self._open_lots:
                self._open_lots[symbol] = (
                    position.avg_price, position.qty, position.opened_at, 0.0,
                )

    # ---- helpers --------------------------------------------------------
    def _history(self, symbol: str) -> "pd.DataFrame":  # noqa: F821
        return self.feed.history(symbol)

    def _closed_bar(self, symbol: str, history: "pd.DataFrame") -> Bar | None:
        """The last finished bar for this symbol, as a Bar.

        Built from the history the strategy was just given, so the timestamp on
        the order is provably the bar the decision was taken on.
        """
        if history is None or len(history) == 0:
            return None
        row = history.iloc[-1]
        return Bar(
            symbol=symbol,
            ts=pd.Timestamp(history.index[-1]).to_pydatetime(),
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row.get("volume", 0.0) or 0.0),
        )

    def refresh_data(self, logger=None) -> dict:
        """Force a download for every symbol and report each outcome."""
        if logger is not None:
            self.feed.logger = logger
        bars = self.feed.bars(force_refresh=True)
        if not bars:
            self.risk.halt(HaltReason.BROKER_DOWN, "refresh produced no usable symbols")
        return self.feed.provenance

    def live_quotes(self) -> dict[str, Quote]:
        """Live quotes for the traded symbols, if a quote service is attached.

        A quote failure is never fatal: the daily bar remains the fallback and
        the bot keeps trading on the evidence it always had.
        """
        if self.quotes is None:
            return {}
        try:
            return self.quotes.quotes()
        except Exception as exc:
            logger.warning("live quotes unavailable: %s", exc)
            return {}

    @staticmethod
    def _mark_prices(bars: dict[str, Bar], quotes: dict[str, Quote]) -> dict[str, float]:
        """Price per symbol for marking: the live quote when it is fresh, else the bar close.

        Marking only. The decision still reads a completed daily bar and the
        fill still happens at the next bar's open, so a live price never turns
        into a decision the backtest could not have made.
        """
        prices = {symbol: bar.close for symbol, bar in bars.items()}
        for symbol, quote in quotes.items():
            if quote.usable:
                prices[symbol] = quote.price
        return prices

    def _record(self, request: OrderRequest, status: OrderStatus, reason: str = ""):
        self.db.record_order(request, status, reason)

    # ---- pending orders -------------------------------------------------
    def pending_orders(self) -> list[dict]:
        """Signalled but unfilled orders, as stored in the ledger."""
        return self.db.pending_orders()

    def _pending_request(self, row: dict) -> OrderRequest:
        """Rebuild an order from its ledger row.

        client_order_id is derived, not stored independently, so the rebuilt
        request must hash back to the same id or the fill will be deduplicated
        against the wrong row.
        """
        return OrderRequest(
            symbol=row["symbol"],
            side=Side(row["side"]),
            qty=float(row["qty"]),
            strategy=row["strategy"],
            signal_bar_ts=_parse(row["signal_bar_ts"]),
            reason=row["reason"],
            reference_price=float(row["reference_price"]),
            target_weight=row["target_weight"],
        )

    def _queue(self, request: OrderRequest) -> str:
        """Record an intent without filling it. It waits for a later bar."""
        existing = self.db.order_status(request.client_order_id)
        if existing is not None:
            return f"already {existing}"
        self._record(request, OrderStatus.NEW, "pending next-bar execution")
        return "pending"

    def _size_for_execution(self, request: OrderRequest, account: AccountState, price: float) -> float:
        """Quantity that moves the book to the weight the strategy asked for.

        Measured at the execution price, not the signal close: a gap between the
        two would otherwise silently change the size the strategy chose.
        """
        if request.target_weight is None or price <= 0:
            return request.qty
        equity = account.equity if account.equity > 0 else 1.0
        held = account.positions.get(request.symbol)
        held_qty = held.qty if held else 0.0
        current_weight = held_qty * price / equity
        delta = request.target_weight - current_weight
        if request.side is Side.BUY:
            return max(0.0, delta) * equity / price
        return min(max(0.0, -delta) * equity / price, held_qty)

    def _live_target(self, symbol: str) -> float | None:
        """The freshest stance the signals step computed, or None to keep the
        recorded intent.

        A queued order carries the intent of the bar that raised it, but the
        strategy may have flipped on a later completed bar. Sizing at execution
        to the current target keeps a fresh decision from waiting behind a
        stale same-side order. None (no computation yet, e.g. right after a
        restart) falls back to the recorded intent, so never call this in a way
        that runs the strategy a second time per cycle - stateful strategies
        advance on every call.
        """
        return self._live_targets.get(symbol)

    def _fill(self, request: OrderRequest, bar: Bar, price: float) -> str:
        """Fill a pending order at the execution bar's open."""
        bypass_risk = request.strategy in IMMEDIATE_STRATEGIES
        if not bypass_risk and request.side is Side.BUY:
            strategy = self.strategies.get(request.symbol)
            if getattr(strategy, "fill_revalidate", False):
                # Rechecks the setup on the newest completed bar; fail open on error.
                try:
                    intent = float(strategy.position(self._history(request.symbol)))
                except Exception:
                    intent = 1.0
                if intent <= 0.0:
                    self.db.update_order_status(
                        request.client_order_id, OrderStatus.CANCELLED,
                        "entry invalidated before fill")
                    return "cancelled: entry setup died before fill"
        account = self.broker.get_account()
        if not bypass_risk and request.target_weight is not None:
            wanted = self._live_target(request.symbol)
            if wanted is not None:
                request.target_weight = wanted
        request.qty = self._size_for_execution(request, account, price)

        if request.qty <= 0:
            self.db.update_order_status(
                request.client_order_id, OrderStatus.CANCELLED, "target already reached"
            )
            return "cancelled: target already reached"

        if not bypass_risk:
            decision = self.risk.check(request, account, bar, self.cost_model, price=price)
            if not decision.approved:
                # A volume-gate refusal gets its own status: the order was fine,
                # the window is full. It stays terminal (no retry machinery) and
                # the strategy re-signals it on its next decision bar if wanted.
                if decision.halt is HaltReason.ORDER_RATE:
                    self.db.update_order_status(
                        request.client_order_id, OrderStatus.RATE_LIMITED, decision.reason
                    )
                    self.db.log_risk_event(
                        "order_rate_limited",
                        f"{request.symbol} {request.side.value}: {decision.reason} "
                        f"(limits {self.risk.limits.max_orders_per_hour}/h, "
                        f"{self.risk.limits.max_orders_per_day}/day)",
                    )
                    return f"rate limited: {decision.reason}"
                self.db.update_order_status(
                    request.client_order_id, OrderStatus.REJECTED, decision.reason
                )
                self.db.log_risk_event(
                    "order_rejected", f"{request.symbol} {request.side.value}: {decision.reason}"
                )
                return f"rejected: {decision.reason}"
            if decision.allowed_qty is not None:
                request.qty = decision.allowed_qty

        try:
            status, fill = self.broker.submit(request, price)
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
        self.db.mark_order_executed(request.client_order_id, bar.ts, fill.qty)
        self.db.update_order_status(request.client_order_id, OrderStatus.FILLED)
        position = self.broker.positions[request.symbol]
        self.db.upsert_position(position)
        if request.side is Side.BUY and position.is_open():
            self._track_lot(request, fill)
        self._record_close(request, position, fill)

        return f"filled {fill.qty:.6f} @ {fill.price:.2f}"

    def _record_close(self, request: OrderRequest, position, fill: Fill) -> None:
        """Book a round trip when a position actually reaches zero.

        Without this the ledger has no realised P/L at all: `closed_trades`
        existed from the start and was never written to. A position opening is
        not a result, so nothing is recorded until the quantity is gone.
        """
        if position.is_open():
            return
        closed = self._open_lots.pop(request.symbol, None)
        if closed is None:
            return
        entry_price, entry_qty, entry_ts, entry_fees = closed
        exit_qty = fill.qty
        entry_value = entry_price * min(exit_qty, entry_qty)
        exit_value = fill.price * exit_qty
        pnl = exit_value - entry_value - fill.fees - entry_fees
        self.db.record_closed_trade(
            symbol=request.symbol,
            entry_ts=entry_ts,
            exit_ts=fill.ts,
            entry_price=entry_price,
            exit_price=fill.price,
            qty=exit_qty,
            pnl=pnl,
            fees=fill.fees + entry_fees,
        )
        self._open_lots.pop(request.symbol, None)

    def _track_lot(self, request: OrderRequest, fill: Fill) -> None:
        """Remember what a still-open position cost, so a close can be booked."""
        existing = self._open_lots.get(request.symbol)
        if existing is None or request.side is Side.BUY:
            self._open_lots[request.symbol] = (
                fill.price, fill.qty, fill.ts, fill.fees,
            ) if existing is None else (
                existing[0], existing[1] + fill.qty, existing[2],
                existing[3] + fill.fees,
            )

    def _drain_pending(self, bars: dict[str, Bar], decisions: list[str]) -> None:
        """Fill orders whose execution bar has arrived; leave the rest queued."""
        for row in self.pending_orders():
            symbol = row["symbol"]
            bar = bars.get(symbol)
            if bar is None:
                continue
            request = self._pending_request(row)
            if as_utc(bar.ts) <= as_utc(request.signal_bar_ts):
                # Same bar, or one that still precedes the signal: no execution bar.
                continue
            decisions.append(
                f"PENDING {symbol} {request.side.value} signal_bar="
                f"{_parse(row['signal_bar_ts']).date()} -> exec_bar={bar.ts.date()}"
            )
            decisions.append(
                f"  {self._fill(request, bar, bar.open)} (open {bar.open:.2f})"
            )

    def _execute(self, request: OrderRequest, bar: Bar, bypass_risk: bool = False) -> str:
        """Immediate fill at the presented price.

        Forced exits (operator kill switch, out-of-session flatten) also land
        here, so the fill books lots and round trips exactly like _fill: an
        emergency path that skipped the accounting would leave a position the
        ledger shows as open and a realised P/L of zero forever.
        """
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
            status, fill = self.broker.submit(request, bar.open)
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
        position = self.broker.positions[request.symbol]
        self.db.upsert_position(position)
        if request.side is Side.BUY and position.is_open():
            self._track_lot(request, fill)
        self._record_close(request, position, fill)

        return f"filled {fill.qty:.6f} @ {fill.price:.2f}"

    def _freshness_gates(self, bars: dict[str, Bar]) -> dict[str, bool]:
        """Per-symbol strictness for minutes-based bar freshness.

        A minutes-old bar proves a dead feed only while the same asset class is
        publishing today. On an exchange holiday or a weekend the equity class
        legitimately prints nothing, and halting the book over bars that are
        yesterday's would stop trading every holiday. Strictness is therefore
        granted class-wide when any member shows a bar stamped today, and
        withheld until the feed brings the day's bars in.
        """
        today = utcnow().replace(tzinfo=None).date()
        live: dict[str, bool] = {}
        for symbol, bar in bars.items():
            instrument = price_data.BY_SYMBOL.get(symbol)
            if instrument is None:
                continue
            if as_utc(bar.ts).date() == today:
                live[instrument.asset_class] = True
        gates: dict[str, bool] = {}
        for symbol in bars:
            instrument = price_data.BY_SYMBOL.get(symbol)
            if instrument is not None:
                gates[symbol] = bool(live.get(instrument.asset_class, False))
        return gates

    # ---- main cycle -----------------------------------------------------
    def cycle(self) -> CycleResult:
        self.cycle_count += 1
        decisions: list[str] = []

        try:
            bars = self.feed.bars()
        except Exception as exc:
            self.risk.halt(HaltReason.BROKER_DOWN, f"feed down: {exc}")
            bars = {}

        for symbol, prov in self.feed.provenance.items():
            if symbol not in bars:
                decisions.append(f"{symbol} unusable: {prov.outcome} ({prov.detail or prov.status})")

        if not bars:
            account = self.broker.get_account()
            self.risk.halt(HaltReason.BROKER_DOWN, "no usable price data")
            return CycleResult(self.cycle_count, {}, account,
                               decisions + ["no bars"], True, self._reason())

        quotes = self.live_quotes()
        if quotes:
            logger.info("quotes: %s", ", ".join(
                f"{symbol}={quote.price:.2f} {quote.source}" for symbol, quote in sorted(quotes.items())
            ))
        prices = self._mark_prices(bars, quotes)
        self.broker.mark(prices)

        # 1. Reconcile before trusting the ledger.
        account = self.broker.get_account()
        mismatches = reconcile(self.db, account, self.risk)
        if mismatches:
            decisions.append(f"reconcile mismatch on {len(mismatches)} symbol(s), halted")
            self.db.record_equity(account.equity, account.cash, account.exposure, account.unrealised)
            return CycleResult(self.cycle_count, bars, account, decisions, True, self._reason())

        # 1b. Session-end guarantees run ahead of every data gate: a stale or
        # quiet feed must never be able to trap an exit inside a dead book.
        if self.config.trading_style == "intraday":
            self._flatten_out_of_session(account, bars, decisions)
            account = self.broker.get_account()

        # 2. Bar freshness. Checked before anything is queued, because an order
        # waiting for its execution bar sits behind a stale signal bar forever
        # and would otherwise never reach the gate that would refuse it.
        gates = self._freshness_gates(bars) if self.config.trading_style == "intraday" else {}
        for symbol, bar in bars.items():
            if gates:
                freshness = self.risk.check_bar_freshness(
                    bar, session_live=gates.get(symbol, True))
            else:
                freshness = self.risk.check_bar_freshness(bar)
            if not freshness.approved:
                decisions.append(f"{symbol}: {freshness.reason}")
        if self.risk.halted:
            self.db.record_equity(account.equity, account.cash, account.exposure, account.unrealised)
            return CycleResult(self.cycle_count, bars, account, decisions, True, self._reason())

        # 3. Daily loss check — halt if the day's loss breaches the cap.
        day_loss = self.db.day_pnl(account.equity)
        if day_loss <= -self.config.limits.max_daily_loss:
            self.risk.halt(HaltReason.DAILY_LOSS,
                           f"daily loss {-day_loss:.2f} >= cap {self.config.limits.max_daily_loss:.2f}")
            account = self.broker.get_account()
            self.db.record_equity(account.equity, account.cash, account.exposure, account.unrealised)
            return CycleResult(self.cycle_count, bars, account,
                               decisions + ["daily loss limit reached"], True, self._reason())

        if self.db.reset_day(account.equity):
            decisions.append("new trading day, P/L baseline reset")

        # 4. Stop losses are evaluated before anything is filled, because marking
        # has already repriced the book and a queued entry must not fill into the
        # same gap that just tripped the stop. Which bar may trigger is explicit:
        # completed bars under the daily model, the live candle only when opted in,
        # and a fresh live quote whenever one exists (priority high to low:
        # quote, forming candle, completed bar).
        if not self.risk.halted:
            account = self.broker.get_account()
            self._queue_stop_losses(account, bars, decisions,
                                    self._closed_bars_for(bars), quotes=quotes)

        # 5. Fill anything whose execution bar has arrived. This runs before new
        # signals so an order signalled on bar t is filled on bar t+1's open and
        # only then does the strategy re-decide on t+1, matching run_backtest.
        self._drain_pending(bars, decisions)

        # 6. Signals. These are queued, never filled on the bar that raised them.
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
                    self._live_targets[symbol] = None
                    continue

                # The decision is made on the last *finished* bar, so that is the
                # bar the order is tagged with. `bar` from bars() may still be
                # forming; its open is a real price, but its close is not a fact
                # and must never feed a strategy or size an intent.
                decision_bar = self._closed_bar(symbol, history)
                if decision_bar is None:
                    continue

                held = account.positions.get(symbol)
                held_qty = held.qty if held else 0.0
                current_weight = held_qty * decision_bar.close / equity
                intents.append(
                    (symbol, decision_bar, min(max(target, 0.0), 1.0), current_weight)
                )

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

            pending = {row["symbol"]: row for row in self.pending_orders()}
            reported: set[str] = set()
            window_reported: set[str] = set()
            for symbol, bar, target, current_weight in intents:
                requested = target * scale
                # Fill-time sizing must see the scaled intent, not the raw ask.
                # Storing the raw weight (as this once did, above) made the
                # first fill re-expand to it bounded only by hard caps, which
                # concentrated the whole exposure cap in one symbol and starved
                # the rest until the reduce cycle walked it back.
                self._live_targets[symbol] = min(max(requested, 0.0), 1.0)
                if (self.config.trading_style == "intraday"
                        and requested > current_weight + 1e-9
                        and self._in_square_off_window(bar)):
                    # New exposure with the close bearing down would strand a
                    # fresh position across the session boundary if the exit
                    # failed to fill. Reducing is always allowed.
                    if symbol not in window_reported:
                        window_reported.add(symbol)
                        decisions.append(
                            f"{symbol}: no new entries inside the square-off window")
                    continue
                if requested < current_weight:
                    # Reducing a position: the position cap never binds going down.
                    side, achievable, limit = Side.SELL, requested, None
                else:
                    side = Side.BUY
                    achievable, limit = self._achievable_weight(requested, equity)

                if abs(achievable - current_weight) < 0.01:
                    # Already at the most the limits permit. Asking again would
                    # re-log the same impossible request on every poll for ever.
                    continue

                if limit and symbol not in reported:
                    reported.add(symbol)
                    decisions.append(
                        f"{symbol} target {requested:.4f} unreachable: {limit}; "
                        f"effective {achievable:.4f}"
                    )

                queued = pending.get(symbol)
                if queued is not None:
                    if queued["strategy"] in IMMEDIATE_STRATEGIES:
                        # A risk-forced exit owns this symbol until it executes.
                        # Letting a strategy opinion cancel it would invert the
                        # whole point of having a stop.
                        continue
                    if queued["side"] == side.value:
                        # Already acting in this direction on this bar.
                        continue
                    # The queued intent points the other way, so it is wrong now.
                    # Leaving it would book an exit against an entry on one bar.
                    self.db.update_order_status(
                        queued["client_order_id"], OrderStatus.CANCELLED,
                        "superseded by an opposite-side intent",
                    )

                qty = abs(achievable - current_weight) * equity / bar.close
                if side is Side.SELL:
                    qty = min(qty, current_weight * equity / bar.close)
                if qty <= 0:
                    continue

                note = f"target {achievable:.4f} vs current {current_weight:.4f}"
                if limit:
                    note += f" (requested {requested:.4f}, capped by {limit})"
                request = OrderRequest(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    strategy=self.config.strategy_name,
                    signal_bar_ts=bar.ts,
                    reason=note,
                    reference_price=bar.close,
                    target_weight=achievable,
                )
                self.db.log_signal(
                    self.config.strategy_name, symbol, side.value, achievable,
                    bar.close, bar.ts, request.reason,
                )
                decisions.append(f"{symbol} {side.value} {qty:.6f} -> {self._queue(request)}")

        # 7. Unresolved orders halt the bot.
        self.risk.check_unknown_orders()

        # Re-mark after trading. Positions opened this cycle were marked at their
        # fill price, so snapshotting without this reports them as freshly bought
        # with zero unrealised P/L, which contradicts every later snapshot.
        account = self.broker.get_account()
        self.broker.mark(prices)
        account = self.broker.get_account()
        self.db.record_equity(account.equity, account.cash, account.exposure, account.unrealised)
        return CycleResult(self.cycle_count, bars, account, decisions, self.risk.halted, self._reason())

    def _achievable_weight(self, requested: float, equity: float) -> tuple[float, str | None]:
        """The most of `requested` the position cap will actually permit.

        Returns the weight and, when the cap binds, the rule responsible. Without
        this a strategy can ask for a weight it can never hold and the engine
        re-requests the same impossible order on every poll for ever.
        """
        limits = self.config.limits
        cap_value = min(limits.max_position_value,
                        equity * limits.max_position_pct_of_equity)
        cap_weight = cap_value / equity if equity > 0 else 0.0
        if requested > cap_weight:
            rule = ("max_position_value" if limits.max_position_value
                    <= equity * limits.max_position_pct_of_equity
                    else "max_position_pct_of_equity")
            return cap_weight, rule
        return requested, None

    def _quote_trigger_bars(self, account: AccountState,
                            quotes: dict[str, Quote]) -> tuple[dict[str, Bar], list[str]]:
        """Intraday stop triggers built from fresh quotes, for open positions only.

        A quote bar is a stand-in Bar whose close is the quote price, so the
        existing check_positions math runs unchanged. A missing or unusable
        quote is never treated as proof a position is safe: it comes back as
        missing, and the completed daily bar keeps running the stop as always.
        """
        triggers: dict[str, Bar] = {}
        missing: list[str] = []
        for symbol, position in account.positions.items():
            if not position.is_open():
                continue
            quote = quotes.get(symbol)
            if quote is not None and quote.usable:
                price = float(quote.price)
                triggers[symbol] = Bar(
                    symbol=symbol, ts=quote.ts, open=price, high=price,
                    low=price, close=price, volume=0.0,
                )
            else:
                missing.append(symbol)
        return triggers, missing

    def _closed_bars_for(self, symbols) -> dict[str, Bar]:
        """Newest completed bar per symbol, from the history the strategy reads."""
        out: dict[str, Bar] = {}
        for symbol in symbols:
            try:
                history = self._history(symbol)
            except Exception:
                continue
            bar = self._closed_bar(symbol, history)
            if bar is not None:
                out[symbol] = bar
        return out

    def _queue_stop_request(self, request: OrderRequest, policy: str, decisions: list[str]):
        """Queue one breached stop request, suppressing duplicates."""
        existing = [r for r in self.pending_orders() if r["symbol"] == request.symbol]
        if any(r["strategy"] == request.strategy for r in existing):
            return          # this position is already queued to be stopped out
        # A queued entry for this symbol is pre-empted by the stop; leaving it
        # in place would buy and then sell at the same open.
        for row in existing:
            self.db.update_order_status(
                row["client_order_id"], OrderStatus.CANCELLED, "pre-empted by stop loss"
            )
        outcome = self._queue(request)
        decisions.append(f"STOP LOSS {request.symbol} -> {outcome} (on {policy})")
        self.db.log_risk_event(
            "stop_loss",
            f"{request.symbol} at {request.reference_price:.2f} "
            f"trigger_bar={pd.Timestamp(request.signal_bar_ts).date()} policy={policy}",
        )

    def _queue_stop_losses(self, account: AccountState, bars: dict[str, Bar],
                           decisions: list[str], closed: dict[str, Bar],
                           quotes: dict[str, Quote] | None = None):
        """Queue forced exits. A stop signalled on bar t fills at bar t+1's open.

        Which bar may trigger a stop is a policy decision, not an accident. The
        completed daily bar keeps running the stop exactly as before; a fresh
        live quote is an ADDITIONAL trigger on top of it, never a replacement,
        so a recovering intraday price cannot mask what the daily close
        already confirmed. Neither trigger fills intraday: a stop triggered
        inside a day still queues and fills at a later open, so it is
        protective scheduling, not a guarantee about the realised loss.
        """
        intraday = self.config.stop_loss_intraday
        base_bars = bars if intraday else closed
        base_policy = "forming candle" if intraday else "completed bar"

        quote_triggers: dict[str, Bar] = {}
        if self.config.stop_loss_use_live_quotes and quotes is not None:
            quote_triggers, missing = self._quote_trigger_bars(account, quotes)
            for symbol in missing:
                if symbol in self._quote_missing_reported:
                    continue
                self._quote_missing_reported.add(symbol)
                self.db.log_risk_event(
                    "quote_unavailable",
                    f"{symbol} open position has no usable live quote; "
                    f"the {base_policy} stop stays in charge",
                )
                decisions.append(
                    f"{symbol}: no usable live quote, stop stays on the {base_policy}"
                )

        if quote_triggers:
            for request in self.risk.check_positions(account, quote_triggers):
                self._queue_stop_request(request, "live quote", decisions)
        for request in self.risk.check_positions(account, base_bars):
            self._queue_stop_request(request, base_policy, decisions)

        # 4b. Same-session square-off. After stops, so a stop owns the exit
        # when both would fire; before new signals, so no entry is raised
        # into the closing window.
        if not self.risk.halted and self.config.trading_style == "intraday":
            self._queue_square_offs(account, bars, decisions)

    # ---- session end ----------------------------------------------------
    def _remaining_bars(self, bar: Bar) -> int:
        """Completed-bar slots still ahead of this bar before the session ends.

        Bar labels are open-anchored, so the slot a bar belongs to closes one
        interval after its label; measuring from there is what keeps the count
        honest for forming and finished bars alike.
        """
        instrument = price_data.BY_SYMBOL.get(bar.symbol)
        if instrument is None:
            # An untracked symbol has no session rule; refusing to assess one
            # means no square-off is ever invented for it.
            return 1_000_000
        grid_now = as_utc(bar.ts).replace(tzinfo=None) + timedelta(
            minutes=interval_minutes(self.config.bar_interval))
        return bars_remaining_in_session(
            grid_now, instrument.asset_class, self.config.bar_interval)

    def _in_square_off_window(self, bar: Bar) -> bool:
        return self._remaining_bars(bar) <= self.config.square_off_buffer_bars

    def _queue_square_offs(self, account: AccountState, bars: dict[str, Bar],
                           decisions: list[str]):
        """Queue same-session exits as the close nears.

        A queued exit fills at the next bar's open, so the window opens
        square_off_buffer_bars before the session ends: with 5-minute bars the
        exit lands ~15 minutes before the close, inside the same session. The
        order is tagged square-off and joins IMMEDIATE_STRATEGIES, so order
        caps and a halt can never strand a position over the close.
        """
        for symbol, position in sorted(account.positions.items()):
            if not position.is_open():
                continue
            bar = bars.get(symbol)
            if bar is None:
                continue
            remaining = self._remaining_bars(bar)
            if remaining > self.config.square_off_buffer_bars:
                continue
            existing = self.pending_orders()
            if any(row["symbol"] == symbol
                   and row["strategy"] in IMMEDIATE_STRATEGIES
                   for row in existing):
                continue      # a stop or square-off already owns this exit
            for row in existing:
                if row["symbol"] == symbol:
                    self.db.update_order_status(
                        row["client_order_id"], OrderStatus.CANCELLED,
                        "pre-empted by square-off",
                    )
            request = OrderRequest(
                symbol=symbol, side=Side.SELL, qty=position.qty,
                strategy="square-off", signal_bar_ts=bar.ts,
                reason=f"session ends in {remaining} bar(s)",
                reference_price=bar.close,
            )
            outcome = self._queue(request)
            decisions.append(
                f"SQUARE-OFF {symbol} -> {outcome} (session ends in {remaining} bar(s))")
            self.db.log_risk_event(
                "square_off",
                f"{symbol} qty={position.qty:.6f} ref={bar.close:.2f} "
                f"session ends in {remaining} bar(s)",
            )

    def _flatten_out_of_session(self, account: AccountState,
                                bars: dict[str, Bar], decisions: list[str]) -> bool:
        """Immediate exits for positions that must not live past their session.

        This is the restart guarantee: a crash between the square-off queue and
        its fill, or a cycle that simply lands after the close, flattens at the
        newest known price instead of carrying the book overnight. It runs
        before freshness and stops so no data gate can ever trap an exit.
        """
        now = utcnow().replace(tzinfo=None)
        flattened = False
        for symbol, position in sorted(account.positions.items()):
            if not position.is_open():
                continue
            instrument = price_data.BY_SYMBOL.get(symbol)
            if instrument is None:
                continue
            asset_class = instrument.asset_class
            state = session_state(now, asset_class)
            if state == "active":
                # Inside a session a still-open position is normal trading.
                # Only a position opened in an EARLIER session has to go, which
                # is the restart case: it survived the close it should have
                # died in and would now trade on stale context.
                start, _ = session_bounds(now.date(), asset_class)
                if not (position.opened_at
                        and as_utc(position.opened_at) < as_utc(start)):
                    continue
                reason = "carried into a new session by a restart"
            else:
                reason = f"session {state} with the book still open"
            bar = bars.get(symbol)
            price = bar.open if bar is not None else position.last_price
            exec_bar = bar or Bar(symbol=symbol, ts=now, open=price,
                                  high=price, low=price, close=price, volume=0.0)
            request = OrderRequest(
                symbol=symbol, side=Side.SELL, qty=position.qty,
                strategy="square-off", signal_bar_ts=exec_bar.ts,
                reason=reason, reference_price=price,
            )
            outcome = self._execute(request, exec_bar, bypass_risk=True)
            self.db.log_risk_event(
                "forced_exit_out_of_session", f"{symbol} {reason} -> {outcome}")
            decisions.append(f"FORCED EXIT {symbol}: {reason} -> {outcome}")
            flattened = True
        return flattened

    def _reason(self) -> str | None:
        reason = self.risk.halt_reason
        return reason.value if reason else None

    # ---- operator actions ----------------------------------------------
    def emergency_stop(self) -> dict:
        """Halt, cancel, flatten. Must work even with the dashboard closed."""
        self.risk.halt(HaltReason.MANUAL, "operator pressed the stop button")
        cancelled = self.broker.cancel_all()
        # Queued orders have never reached the broker, so broker.cancel_all cannot
        # see them. They must be cancelled here or they would fill after the halt.
        cancelled += self.db.cancel_pending_orders("cancelled by kill switch")
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