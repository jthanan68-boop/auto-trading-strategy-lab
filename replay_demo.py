#!/usr/bin/env python
"""Replay history through the paper broker to prove the order path end to end.

The hour-long live run placed no orders because no symbol is at a 55-day high,
so it produced no evidence that fills, fees, stop-losses or position accounting
work. This replays real bars through the real PaperBroker and RiskManager and
writes the resulting fills to the ledger, which is exactly the evidence that run
was missing.

    python replay_demo.py --symbol BTC --days 400
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig, RiskLimits
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import Bar, OrderRequest, Side, utcnow
from stratlab.strategies.library import DonchianBreakout


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay history to exercise the order path.")
    parser.add_argument("--symbol", default="BTC")
    parser.add_argument("--days", type=int, default=400, help="trading days to replay")
    parser.add_argument("--db", default="data/replay_demo.sqlite3")
    parser.add_argument("--start-cash", type=float, default=500_000.0)
    return parser.parse_args()


def main() -> int:
    from stratlab import data as price_data

    args = parse_args()
    instrument = price_data.BY_SYMBOL[args.symbol.upper()]
    frame = price_data.load(instrument)
    window = frame.iloc[-args.days :].reset_index(drop=False)
    window.columns = [str(c).lower() for c in window.columns]
    window = window.set_index("date")

    print("=" * 70)
    print(f"REPLAY DEMO - {instrument.symbol} ({instrument.yfinance_symbol})")
    print("=" * 70)
    print(f"{len(window)} bars, {window.index[0].date()} to {window.index[-1].date()}")

    db = Database(Path(args.db))
    # bot_state must be cleared too: a halt latched by a previous run would
    # otherwise reject every order before the first one is even placed.
    for table in ("fills", "orders", "signals", "positions", "closed_trades",
                  "risk_events", "bot_state", "equity"):
        with db.connect() as conn:
            conn.execute(f"DELETE FROM {table}")

    config = BotConfig(
        symbols=(instrument.symbol,),
        initial_cash=args.start_cash,
        limits=RiskLimits(stop_loss_pct=0.10),
    )
    broker = PaperBroker(args.start_cash)
    engine = Engine(config, db, broker=broker)

    strategy = DonchianBreakout(55, 20)

    fills: list[dict] = []
    stop_losses: list[str] = []

    for index in range(1, len(window)):
        history = window.iloc[: index + 1]
        bar = Bar(
            symbol=instrument.symbol,
            # Historical bars are days old, so the risk layer would reject every
            # order as stale. This replay is exercising the order path, so each bar
            # is stamped "now" as it would be during a live session.
            ts=utcnow(),
            open=float(window["open"].iloc[index]),
            high=float(window["high"].iloc[index]),
            low=float(window["low"].iloc[index]),
            close=float(window["close"].iloc[index]),
            volume=float(window["volume"].iloc[index]) if "volume" in window else 0.0,
        )
        broker.mark({instrument.symbol: bar.close})

        target = float(strategy.position(history))
        if target != target:
            continue

        account = broker.get_account()
        position = account.positions.get(instrument.symbol)
        held = position.qty if position else 0.0
        weight = (held * bar.close / account.equity) if account.equity else 0.0

        # Risk-managed stop loss, checked every bar regardless of the signal.
        for exit_request in engine.risk.check_positions(account, {instrument.symbol: bar}):
            outcome = engine._execute(exit_request, bar, bypass_risk=True)
            stop_losses.append(
                f"{exit_request.side.value} {exit_request.qty:.6f} -> {outcome}"
            )

        if abs(target - weight) < 0.02:
            continue

        side = Side.BUY if target > weight else Side.SELL
        qty = abs(target - weight) * broker.get_account().equity / bar.close
        if side is Side.SELL:
            qty = min(qty, held)
        if qty <= 0:
            continue

        request = OrderRequest(
            symbol=instrument.symbol,
            side=side,
            qty=qty,
            strategy="replay-donchian-55/20",
            signal_bar_ts=bar.ts,
            reason=f"target {target:.2f} vs held {weight:.2f}",
            reference_price=bar.close,
        )
        db.log_signal(
            "replay-donchian-55/20", instrument.symbol, side.value, target, bar.close, bar.ts, request.reason
        )
        before = len(db.recent_fills(999))
        outcome = engine._execute(request, bar)
        after = db.recent_fills(999)
        if len(after) > before:
            fill = after[0]
            fills.append(fill)
            print(
                f"  {fill['ts'][:19]}  {fill['side']:<4} {float(fill['qty']):>10.6f} "
                f"@ {float(fill['price']):>10.2f}  fees {float(fill['fees']):>7.2f}  -> {outcome}"
            )

    account = broker.get_account()
    print()
    print("-" * 70)
    print("RESULT")
    print("-" * 70)
    print(f"fills            : {len(fills)}")
    print(f"stop-loss exits  : {len(stop_losses)}")
    for line in stop_losses:
        print(f"    {line}")
    print(f"closed trades    : {len(db.closed_trades())}")
    print(f"total fees paid  : {sum(float(f['fees']) for f in fills):.2f}")
    print(f"start equity     : {args.start_cash:,.2f}")
    print(f"end equity       : {account.equity:,.2f}")
    print(f"return           : {(account.equity / args.start_cash - 1) * 100:+.2f}%")
    print(f"cash             : {account.cash:,.2f}")
    print(f"open positions   : {{k: round(v.qty, 6) for k, v in ...}}")
    for symbol, position in account.positions.items():
        if position.is_open():
            print(f"    {symbol}: {position.qty:.6f} @ avg {position.avg_price:.2f} "
                  f"(unrealised {position.unrealised_pnl:+.2f})")
    print()
    print(f"ledger: {Path(args.db).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())