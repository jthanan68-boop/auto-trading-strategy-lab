#!/usr/bin/env python
"""Test the hypothesis the diagnosis generated: invest across many instruments at once.

    python run_xs.py                 # dev/test split, same boundary as run_lab.py
    python run_xs.py --dev-end 2025-01-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from stratlab import config, data
from stratlab.costs import cost_model_for
from stratlab.portfolio import benchmark_equity, equal_weight_portfolio_backtest
from stratlab.strategies.cross_sectional import (
    cross_sectional_low_vol,
    cross_sectional_momentum,
)

OUT_DIR = Path(__file__).resolve().parent / "out"
CASH = 1_000_000.0


def build_rules() -> list:
    return [
        cross_sectional_momentum(lookback=126, skip=21, top_n=10),
        cross_sectional_momentum(lookback=63, skip=21, top_n=10),
        cross_sectional_momentum(lookback=252, skip=21, top_n=10),
        cross_sectional_momentum(lookback=126, skip=0, top_n=10),
        cross_sectional_momentum(lookback=126, skip=21, top_n=5),
        cross_sectional_low_vol(lookback=126, top_n=10),
    ]


def load_universe(symbols: list[str]) -> dict[str, pd.DataFrame]:
    prices: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        instrument = data.BY_SYMBOL.get(symbol)
        if instrument is None:
            continue
        try:
            prices[symbol] = data.load(instrument)
        except Exception as exc:
            print(f"  skipped {symbol}: {exc}", file=sys.stderr)
    return prices


def evaluate(name: str, window: dict[str, pd.DataFrame], cost, label: str) -> dict | None:
    if len(window) < 12:
        return None
    bars = min(len(frame) for frame in window.values())
    if bars < 260:
        return None
    benchmark = benchmark_equity(window, CASH)
    base = benchmark.iloc[0] if len(benchmark) else CASH

    result = equal_weight_portfolio_backtest(
        window, build_rules_pick[name], cost, initial_cash=CASH, rebalance_days=21
    )
    metrics = result.metrics()
    bench_return = (benchmark.iloc[-1] / base - 1) * 100
    metrics["benchmark_return_pct"] = round(bench_return, 2)
    metrics["excess_pct"] = round(metrics["total_return_pct"] - bench_return, 2)
    metrics["benchmark_max_dd_pct"] = round(
        float((benchmark / benchmark.cummax() - 1).min() * 100), 2
    )
    metrics["period"] = label
    metrics["rule"] = name
    metrics["instruments"] = len(window)
    return metrics


build_rules_pick: dict = {}


def main() -> int:
    global build_rules_pick

    parser = argparse.ArgumentParser(description="Cross-sectional portfolio backtest.")
    parser.add_argument("--dev-end", default=config.DEV_END)
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    symbols = args.symbols.split(",") if args.symbols else (
        config.FULL_SYMBOLS if args.full else config.DEFAULT_SYMBOLS
    )

    build_rules_pick = {rule.name: rule for rule in build_rules()}
    cost = cost_model_for("equity")

    print(f"loading {len(symbols)} instruments ...")
    prices = load_universe(symbols)
    if len(prices) < 12:
        print(f"need at least 12 instruments for a cross-sectional rule, got {len(prices)}")
        return 1
    print(f"loaded {len(prices)}")

    cutoff = pd.Timestamp(args.dev_end)
    windows = {
        "dev": {s: d[d.index < cutoff] for s, d in prices.items()},
        "test": {s: d[d.index >= cutoff] for s, d in prices.items()},
    }

    rows: list[dict] = []
    for label, window in windows.items():
        print(f"\n=== {label} ===")
        for name in build_rules_pick:
            metrics = evaluate(name, window, cost, label)
            if metrics is None:
                print(f"  {name:<28} not enough history")
                continue
            rows.append(metrics)
            print(
                f"  {name:<28} ret {metrics['total_return_pct']:>8.2f}%"
                f"  dd {metrics['max_drawdown_pct']:>7.2f}%"
                f"  trades {metrics['trades']:>4}"
                f"  vs B&H {metrics['excess_pct']:>+7.2f} pts"
            )

    if not rows:
        return 1

    frame = pd.DataFrame(rows)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "cross_sectional.csv"
    frame.to_csv(path, index=False)

    test = frame[frame["period"] == "test"]
    print("\n" + "=" * 68)
    print("OUT-OF-SAMPLE VERDICT")
    print("=" * 68)
    if test.empty:
        print("no out-of-sample rows")
        return 0

    beaten = (test["excess_pct"] > 0).sum()
    print(f"rules beating equal-weight buy-and-hold: {beaten} of {len(test)}")
    print(f"median excess: {test['excess_pct'].median():+.2f} points")
    print(f"mean trades per rule: {test['trades'].mean():.0f}")
    best = test.nlargest(1, "excess_pct").iloc[0]
    print(f"best: {best['rule']} {best['excess_pct']:+.2f} points "
          f"(ret {best['total_return_pct']:+.2f}%, dd {best['max_drawdown_pct']:.2f}%)")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())