#!/usr/bin/env python
"""Phase 3 analysis: compare the live paper run against buy-and-hold and the backtest.

    python analyse_run.py                    # analyse the default database
    python analyse_run.py --db data/bot.sqlite3

Run this periodically during a multi-week paper run. It answers whether the run
has lasted long enough, seen enough market conditions, and beaten buy-and-hold.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

import pandas as pd

from stratbot.analysis import (
    classify,
    compare_to_backtest,
    duration_days,
    regime_coverage,
    summarise_run,
    verdict,
)
from stratbot.config import CONFIG, DB_PATH
from stratbot.db import Database

OUT_DIR = "out"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyse the live paper run.")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--out", default=OUT_DIR)
    parser.add_argument("--backtest-csv", default="out/results.csv")
    return parser.parse_args()


def buy_and_hold_over(db, symbols: tuple[str, ...], start: str | None) -> dict:
    """Benchmark the same window the live run covered, not the whole history."""
    from stratlab import data as price_data

    rows = []
    for symbol in symbols:
        try:
            frame = price_data.load(price_data.BY_SYMBOL[symbol])
        except Exception:
            continue
        window = frame
        if start:
            window = frame[frame.index >= pd.Timestamp(start)]
        if len(window) < 2:
            continue
        # Split-adjusted closes make this a total-return comparison.
        ret = (window["close"].iloc[-1] / window["close"].iloc[0] - 1) * 100
        drawdown = float(((window["close"] / window["close"].cummax()) - 1).min() * 100)
        rows.append({"symbol": symbol, "return_pct": round(ret, 2), "max_dd_pct": round(drawdown, 2)})

    if not rows:
        return {}
    return {
        "per_symbol": rows,
        "mean_return_pct": round(sum(r["return_pct"] for r in rows) / len(rows), 2),
        "worst_symbol": min(rows, key=lambda r: r["return_pct"])["symbol"],
    }


def main() -> int:
    args = parse_args()
    db = Database(args.db)

    run = summarise_run(db, CONFIG.strategy_name)
    days = duration_days(db)
    run["_days"] = days

    equity = db.equity_history()
    start = equity[0]["ts"][:10] if equity else None

    coverage: dict[str, int] = {"no-equity-history": 1}
    if start:
        from stratlab import data as price_data

        try:
            frame = price_data.load(price_data.BY_SYMBOL[CONFIG.symbols[0]])
            recent = frame[frame.index >= pd.Timestamp(start)]
            coverage = regime_coverage(recent["close"], [])
        except Exception as exc:
            coverage = {"price-cache-unavailable": 1}
            print(f"  note: regime coverage unavailable ({exc})")

    benchmark = buy_and_hold_over(db, CONFIG.symbols, start)
    bh_return = benchmark.get("mean_return_pct") if benchmark else None

    text, reasons = verdict(run, coverage, bh_return)

    print("=" * 66)
    print("PHASE 3 - LIVE PAPER RUN")
    print("=" * 66)
    print(f"strategy      {run['strategy']}")
    print(f"window        {run['span']}  ({days:.0f} days)")
    print(f"equity        {run['start_equity']:,.0f} -> {run['end_equity']:,.0f}")
    print(f"return        {run['total_return_pct']:+.2f}%")
    print(f"closed trades {run['trades']}   win rate {run['win_rate_pct']:.1f}%")
    print(f"fees paid     {run['total_fees']:,.2f}")
    print(f"profit factor {run['profit_factor']:.2f}" if run["profit_factor"] != float("inf") else "profit factor inf")

    print("\nmarket conditions seen")
    for bucket, count in sorted(coverage.items(), key=lambda kv: -kv[1]):
        print(f"  {bucket:<16} {count}")

    if benchmark:
        print("\nbuy-and-hold over the same window")
        for row in benchmark["per_symbol"]:
            print(f"  {row['symbol']:<10} {row['return_pct']:+7.2f}%   max dd {row['max_dd_pct']:>7.2f}%")
        print(f"  {'MEAN':<10} {benchmark['mean_return_pct']:+7.2f}%")

    print(f"\nVERDICT: {text}")
    for reason in reasons:
        print(f"  - {reason}")

    material = []
    try:
        import csv

        with open(args.backtest_csv, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        material, _ = compare_to_backtest(run, rows)
    except FileNotFoundError:
        rows = []

    if material:
        print("\nbacktest vs live (differences worth investigating)")
        for item in material:
            print(f"  {item.symbol:<12} {item.field:<20} backtest {item.backtest:>10.2f}  live {item.live:>10.2f}")

    from pathlib import Path

    Path(args.out).mkdir(parents=True, exist_ok=True)
    report = Path(args.out) / "phase3_report.txt"
    report.write_text(
        "\n".join(
            [
                f"generated {datetime.now().isoformat(timespec='seconds')}",
                f"strategy {run['strategy']}",
                f"window {run['span']} ({days:.0f} days)",
                f"return {run['total_return_pct']:+.2f}%",
                f"trades {run['trades']} win_rate {run['win_rate_pct']:.1f}%",
                f"buy_and_hold {bh_return if bh_return is not None else 'n/a'}",
                f"regimes {coverage}",
                f"VERDICT {text}",
                *[f"  - {r}" for r in reasons],
            ]
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())