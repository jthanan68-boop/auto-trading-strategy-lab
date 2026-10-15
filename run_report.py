#!/usr/bin/env python
"""Dump a complete, readable record of a paper-trading run into one file.

    python run_report.py --db data/run10min.sqlite3 --log logs/engine.log --out out/run_report.txt
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from stratbot.db import Database

RULE = "=" * 78


def money(value: float) -> str:
    return f"{value:,.2f}"


def section(title: str) -> list[str]:
    return ["", RULE, title, RULE, ""]


def main() -> int:
    parser = argparse.ArgumentParser(description="Consolidated paper-trading run report.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--log", default="logs/engine.log")
    parser.add_argument("--out", default="out/run_report.txt")
    args = parser.parse_args()

    db = Database(Path(args.db))
    equity = db.equity_history()
    fills = db.recent_fills(1000)
    signals = db.recent_signals(1000)
    events = db.recent_risk_events(1000)
    positions = db.positions()
    trades = db.closed_trades()

    start = equity[0]["equity"] if equity else 0.0
    end = equity[-1]["equity"] if equity else 0.0
    total_fees = sum(float(f["fees"]) for f in fills)
    buys = [f for f in fills if f["side"] == "BUY"]
    sells = [f for f in fills if f["side"] == "SELL"]

    lines: list[str] = []
    lines.append(RULE)
    lines.append("PAPER TRADING RUN - CONSOLIDATED REPORT")
    lines.append(RULE)
    lines.append(f"generated   : {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"database    : {Path(args.db).resolve()}")
    lines.append(f"mode        : PAPER (simulation only, cannot place a real order)")
    lines.append(f"window      : {equity[0]['ts'][:19]}  ->  {equity[-1]['ts'][:19]}" if equity else "window      : n/a")
    lines.append(f"cycles      : {len(equity)}")

    lines += section("1. RESULT SUMMARY")
    if equity:
        # The first recorded equity already includes cycle-1 fees, so using it as
        # the baseline understates the loss. Recover the true opening balance from
        # the cash the ledger started with.
        opening = float(equity[0]["cash"]) + sum(
            float(f["qty"]) * float(f["price"]) + float(f["fees"]) for f in fills
        )
        lines.append(f"  opening balance     : {money(opening)}   (derived, includes cycle-1 fees)")
    lines.append(f"  first recorded eq   : {money(start)}")
    lines.append(f"  end equity          : {money(end)}")
    lines.append(f"  change              : {end - opening:+,.2f}   (from opening balance, AFTER fees)")
    lines.append(f"  return              : {(end / opening - 1) * 100:+.2f}%" if opening else "  return              : n/a")
    lines.append(f"  fees paid           : {money(total_fees)}")
    lines.append(f"  gross P/L ex fees   : {(end - opening) + total_fees:+,.2f}")
    lines.append(f"  cash                : {money(equity[-1]['cash']) if equity else 'n/a'}")
    lines.append(f"  exposure            : {money(equity[-1]['exposure']) if equity else 'n/a'}")
    lines.append(f"  unrealised P/L      : {money(equity[-1]['unrealised']) if equity else 'n/a'}")
    lines.append(f"  fills               : {len(fills)}  (buys {len(buys)}, sells {len(sells)})")
    lines.append(f"  total fees paid     : {money(total_fees)}")
    lines.append(f"  closed trades       : {len(trades)}")
    lines.append(f"  halted              : {db.get_state('halted')}")
    lines.append(f"  halt reason         : {db.get_state('halt_reason')}")

    lines += section("2. FILLS (real simulated orders with fees)")
    if not fills:
        lines.append("  none")
    for fill in fills:
        notional = float(fill["qty"]) * float(fill["price"])
        lines.append(
            f"  {fill['ts'][:19]}  {fill['side']:<4} {fill['symbol']:<10} "
            f"qty {float(fill['qty']):>12.6f} @ {float(fill['price']):>12.2f}  "
            f"notional {money(notional):>14}  fee {float(fill['fees']):>7.2f}"
        )
        lines.append(f"      id {fill['client_order_id']}")

    lines += section("3. OPEN POSITIONS")
    open_positions = {k: v for k, v in positions.items() if v.is_open()}
    if not open_positions:
        lines.append("  none open")
    else:
        # The ledger persists quantity and average price but not the last price,
        # so mark positions with current bars before valuing them.
        from stratbot.broker.feed import current_bars

        marks = current_bars(list(open_positions))
        for symbol, position in open_positions.items():
            bar = marks.get(symbol)
            if bar is not None:
                position.last_price = bar.close
            lines.append(
                f"  {symbol:<10} qty {position.qty:>12.6f}  avg {position.avg_price:>12.2f}  "
                f"last {position.last_price:>12.2f}  value {money(position.market_value):>14}  "
                f"unrealised {position.unrealised_pnl:>+10.2f} ({position.unrealised_pct:+.2f}%)"
            )
        total_unrealised = sum(p.unrealised_pnl for p in open_positions.values())
        total_value = sum(p.market_value for p in open_positions.values())
        lines.append(f"  {'TOTAL':<10} {'':<12} {'':<13} {'':<12} {money(total_value):>14}  "
                     f"unrealised {total_unrealised:>+10.2f}")

    lines += section("4. SIGNALS")
    if not signals:
        lines.append("  none")
    for signal in signals:
        lines.append(
            f"  {signal['ts'][:19]}  {signal['symbol']:<10} {signal['action']:<5} "
            f"target {float(signal['weight']):.2f} @ {float(signal['price']):.2f}  {signal['reason']}"
        )

    lines += section("5. RISK-EVENT LEDGER")
    if not events:
        lines.append("  none")
    counts: dict[str, int] = {}
    for event in events:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    lines.append("  summary:")
    for kind, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {kind:<26} x{count}")
    lines.append("  detail:")
    for event in events:
        lines.append(f"    {event['ts'][:19]}  {event['kind']:<22} {event['detail']}")

    lines += section("6. EQUITY CURVE")
    for row in equity:
        lines.append(
            f"  {row['ts'][:19]}  equity {money(float(row['equity'])):>14}  "
            f"cash {money(float(row['cash'])):>14}  exposure {money(float(row['exposure'])):>12}  "
            f"unrealised {money(float(row['unrealised'])):>10}"
        )

    log_path = Path(args.log)
    if log_path.exists():
        lines += section("7. ENGINE LOG (verbatim)")
        lines.extend(log_path.read_text(encoding="utf-8", errors="ignore").splitlines())

    lines += section("END OF REPORT")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    print(f"wrote {out.resolve()}")
    print(f"  fills {len(fills)} | fees {money(total_fees)} | "
          f"return {(end / start - 1) * 100:+.2f}% | halted {db.get_state('halted')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())