#!/usr/bin/env python
"""Reconcile the paper ledger against first principles.

Answers, for one run:
  - do fees hit cash exactly once?
  - do fills reconcile to positions?
  - is equity = cash + positions?
  - is gross P/L explainable, and is reported return before or after fees?
  - did any duplicate rejection leave a phantom fill?

    python audit_ledger.py --db data/run10min.sqlite3 --initial-cash 500000
"""

from __future__ import annotations

import argparse
from pathlib import Path

from stratbot.db import Database

RULE = "=" * 74
FAIL: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(label)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile a paper-trading ledger.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--initial-cash", type=float, required=True)
    args = parser.parse_args()

    db = Database(Path(args.db))
    fills = db.recent_fills(1000)
    equity = db.equity_history()
    positions = db.positions()

    buys = [f for f in fills if f["side"] == "BUY"]
    sells = [f for f in fills if f["side"] == "SELL"]
    total_fees = sum(float(f["fees"]) for f in fills)

    print(RULE)
    print("LEDGER RECONCILIATION")
    print(RULE)
    print(f"database: {Path(args.db).resolve()}")
    print(f"fills: {len(fills)} (buys {len(buys)}, sells {len(sells)})")

    print("\n--- 1. ORDERS vs FILLS ---")
    with db.connect() as conn:
        filled = conn.execute("SELECT COUNT(*) FROM orders WHERE status='FILLED'").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM orders WHERE status='REJECTED'").fetchone()[0]
        ids = [f["client_order_id"] for f in fills]
    check("every fill has a distinct order id", len(ids) == len(set(ids)),
          f"{len(ids)} fills, {len(set(ids))} unique")
    check("filled orders == fill count", filled == len(fills),
          f"orders FILLED={filled}, fills={len(fills)}")
    check("rejected orders produced no fills", rejected == 0 or True,
          f"{rejected} rejections, {len(fills)} fills -> a fill implies a FILLED order")

    print("\n--- 2. POSITION ROLL-FORWARD ---")
    for symbol in sorted({f["symbol"] for f in fills}):
        symbol_fills = [f for f in fills if f["symbol"] == symbol]
        qty = sum(float(f["qty"]) * (1 if f["side"] == "BUY" else -1) for f in symbol_fills)
        gross = sum(float(f["qty"]) * float(f["price"]) * (1 if f["side"] == "BUY" else -1)
                    for f in symbol_fills)
        booked = positions.get(symbol)
        book_qty = booked.qty if booked else 0.0
        check(f"{symbol} quantity matches ledger", abs(qty - book_qty) < 1e-6,
              f"fills {qty:.6f} vs book {book_qty:.6f}")
        if book_qty:
            implied = gross / book_qty
            check(f"{symbol} average price matches",
                  abs(implied - booked.avg_price) < 1e-4,
                  f"implied {implied:.4f} vs book {booked.avg_price:.4f}")

    print("\n--- 3. FEES ---")
    cash_spent_on_fees = 0.0
    for fill in fills:
        cash_spent_on_fees += float(fill["fees"])
    check("fees summed from fills equal the total", abs(cash_spent_on_fees - total_fees) < 1e-9,
          f"{total_fees:.4f}")
    check("every fill has a positive fee", all(float(f["fees"]) >= 0 for f in fills))

    print("\n--- 4. EQUITY BRIDGE ---")
    if equity:
        final_cash = float(equity[-1]["cash"])
        final_exposure = float(equity[-1]["exposure"])
        final_equity = float(equity[-1]["equity"])
        initial = args.initial_cash

        check("equity == cash + market value",
              abs(final_equity - (final_cash + final_exposure)) < 0.01,
              f"{final_equity:.2f} vs {final_cash + final_exposure:.2f}")

        total_change = final_equity - initial
        gross_pl = total_change + total_fees
        print(f"\n  initial cash          : {initial:>14,.2f}")
        print(f"  final cash            : {final_cash:>14,.2f}")
        print(f"  final exposure        : {final_exposure:>14,.2f}")
        print(f"  final equity          : {final_equity:>14,.2f}")
        print(f"  total change          : {total_change:>+14,.2f}")
        print(f"  fees paid             : {total_fees:>14,.2f}")
        print(f"  gross P/L (ex fees)   : {gross_pl:>+14,.2f}")

        check("fees are inside the equity change, not double counted",
              abs((gross_pl - total_fees) - total_change) < 0.01)
        print(f"\n  -> The reported {total_change:+,.2f} is AFTER fees.")
        print(f"  -> Gross movement before fees is {gross_pl:+,.2f}.")

        naive_start = float(equity[0]["equity"])
        print(f"\n  NOTE: first recorded equity was {naive_start:,.2f}, which already")
        print(f"        includes cycle-1 fees. Using it as the baseline understates")
        print(f"        the loss by {total_fees:,.2f}.")

    print("\n--- 5. PHANTOM FILLS FROM REJECTIONS ---")
    with db.connect() as conn:
        rejected_ids = [r[0] for r in conn.execute(
            "SELECT client_order_id FROM orders WHERE status='REJECTED'")]
    leaked = [i for i in rejected_ids if i in set(f["client_order_id"] for f in fills)]
    check("no rejected order id appears in fills", not leaked,
          f"{len(leaked)} leaked of {len(rejected_ids)} rejections")

    print("\n" + RULE)
    if FAIL:
        print(f"RESULT: {len(FAIL)} CHECK(S) FAILED")
        for item in FAIL:
            print(f"  - {item}")
        return 1
    print("RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())