#!/usr/bin/env python
"""Strategy lab entry point.

    python run_lab.py                     # full universe, dev + out-of-sample
    python run_lab.py --symbols BTC,TCS   # a subset
    python run_lab.py --refresh           # bypass the cache and refetch
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import pandas as pd

from stratlab import config, data, report
from stratlab.backtest import run_backtest
from stratlab.costs import cost_model_for
from stratlab.strategies.library import DonchianBreakout, RsiStrategy, SmaCrossover

OUT_DIR = Path(__file__).resolve().parent / "out"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest strategies vs buy-and-hold.")
    parser.add_argument(
        "--symbols",
        default=",".join(config.DEFAULT_SYMBOLS),
        help="comma-separated instrument codes",
    )
    parser.add_argument("--start", default=config.DATA_START)
    parser.add_argument("--dev-end", default=config.DEV_END)
    parser.add_argument("--cash", type=float, default=config.INITIAL_CASH)
    parser.add_argument("--refresh", action="store_true", help="refetch and rebuild the cache")
    return parser.parse_args()


def fresh_strategy(strategy, bars: pd.DataFrame):
    """Stateful strategies must restart per symbol and per split.

    Reusing one instance across the dev/test split would leak in-sample state
    into the out-of-sample run and inflate the unseen result.
    """
    if isinstance(strategy, SmaCrossover):
        return SmaCrossover(**strategy.params)
    if isinstance(strategy, RsiStrategy):
        return RsiStrategy(**strategy.params)
    if isinstance(strategy, DonchianBreakout):
        return DonchianBreakout(**strategy.params)
    from stratlab.strategies.library import BuyAndHold, BuyDipHold

    if isinstance(strategy, BuyDipHold):
        return BuyDipHold(**strategy.params)
    return BuyAndHold()


def evaluate(symbol: str, frame: pd.DataFrame, dev_end: str, cash: float) -> list[dict]:
    instrument = data.BY_SYMBOL[symbol]
    cost = cost_model_for(instrument.asset_class)
    dev, test = data.split_periods(frame, dev_end)
    rows: list[dict] = []

    for label, window in (("dev", dev), ("test", test)):
        if len(window) < 30:
            continue
        for template in config.build_strategies():
            result = run_backtest(
                window,
                fresh_strategy(template, window),
                cost,
                initial_cash=cash,
                symbol=symbol,
            )
            metrics = result.metrics(annual_factor=instrument.annual_factor)
            metrics["period"] = label
            metrics["strategy"] = f"{template.name} [{label}]"
            rows.append(metrics)
    return rows


def main() -> int:
    args = parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    unknown = [s for s in symbols if s not in data.BY_SYMBOL]
    if unknown:
        print(f"unknown symbols: {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(sorted(data.BY_SYMBOL))}", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    curves: dict[str, pd.Series] = {}
    failed: list[str] = []

    for symbol in symbols:
        instrument = data.BY_SYMBOL[symbol]
        print(f"\n=== {symbol} ({instrument.yfinance_symbol}) ===")
        try:
            frame = data.load(instrument, start=args.start, refresh=args.refresh)
        except Exception as exc:
            print(f"  data load failed: {exc}")
            failed.append(symbol)
            continue

        print(f"  {len(frame)} bars, {frame.index[0].date()} to {frame.index[-1].date()}")
        try:
            rows = evaluate(symbol, frame, args.dev_end, args.cash)
        except Exception:
            traceback.print_exc()
            failed.append(symbol)
            continue

        all_rows.extend(rows)
        for row in rows:
            if row["period"] == "test":
                print(
                    f"  {row['strategy']:<34} ret {row['total_return_pct']:>8.2f}%"
                    f"  dd {row['max_drawdown_pct']:>7.2f}%"
                    f"  trades {row['trades']:>3}"
                )

        template = config.build_strategies()[0]
        cost = cost_model_for(instrument.asset_class)
        curves[f"{symbol}"] = run_backtest(
            frame, template, cost, initial_cash=args.cash, symbol=symbol
        ).equity_curve

    if not all_rows:
        print("no results produced", file=sys.stderr)
        return 1

    frame = pd.DataFrame(all_rows)
    ordered = [
        "symbol", "period", "strategy", "total_return_pct", "cagr_pct", "ann_vol_pct",
        "sharpe", "sortino", "max_drawdown_pct", "trades", "win_rate_pct",
        "profit_factor", "exposure_pct", "end_equity",
    ]
    frame = frame[[c for c in ordered if c in frame.columns]]
    csv_path = OUT_DIR / "results.csv"
    frame.to_csv(csv_path, index=False)

    round_trip_notes = []
    for asset_class in ("equity", "crypto"):
        cost = cost_model_for(asset_class)
        split = cost.breakdown()
        parts = [f"statutory {split['statutory']}%", f"slippage {split['slippage']}%"]
        if split["stt"]:
            parts.append(f"of which STT {split['stt']}%")
        round_trip_notes.append(
            f"**{cost.name}**: {split['total']}% per round trip ({', '.join(parts)})"
        )

    report.write_report(
        OUT_DIR / "report.md", all_rows, symbols, args.dev_end, round_trip_notes
    )
    report.plot_equity_curves(
        OUT_DIR / "equity_curves.png", curves, "Buy-and-hold growth of 100 by symbol"
    )

    print(f"\nwrote {csv_path}")
    print(f"wrote {OUT_DIR / 'report.md'}")
    print(f"wrote {OUT_DIR / 'equity_curves.png'}")
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())