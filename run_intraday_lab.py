#!/usr/bin/env python
"""Offline intraday strategy comparison.

    python run_intraday_lab.py                # the 5-symbol universe, 5m bars, ~40 days
    python run_intraday_lab.py --interval 15m # coarser bars
    python run_intraday_lab.py --refresh      # refetch intraday bars

Runs the three intraday presets plus a buy-and-hold baseline over cached intraday
bars, on a chronological dev/test split, and ranks the results. It deliberately
does NOT pick a winner: the ranking tables go to `out/intraday_results.csv` and
`out/intraday_report.md`, and the choice is the operator's.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import timedelta
from pathlib import Path

import pandas as pd

from stratlab import data
from stratlab.backtest import run_backtest
from stratlab.calendar import interval_minutes
from stratlab.costs import INTRADAY, cost_model_for
from stratlab.strategies.intraday import (
    IntradaySmaCross,
    OpeningRangeBreakout,
    VwapDip,
    intraday_presets,
)
from stratlab.strategies.library import BuyAndHold

OUT_DIR = Path(__file__).resolve().parent / "out"
DEFAULT_SYMBOLS = ["BTC", "ETH", "RELIANCE", "TCS", "INFY"]
MIN_TRADES = 20          # below this a candidate split is not evidence
BASELINE = BuyAndHold().name


def parse_args() -> argparse.Namespace:
    today = pd.Timestamp.today().normalize()
    # Yahoo keeps only ~59 days of intraday history; data.load clamps anyway.
    parser = argparse.ArgumentParser(description="Rank intraday presets offline.")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                        help=f"comma-separated instrument codes (default: {','.join(DEFAULT_SYMBOLS)})")
    parser.add_argument("--interval", default="5m", help="bar interval (5m, 15m, ...)")
    parser.add_argument("--days", type=int, default=40, help="calendar days of history to load")
    parser.add_argument("--cash", type=float, default=100_000.0, help="starting equity per backtest")
    parser.add_argument("--refresh", action="store_true", help="refetch and rebuild the cache")
    parser.add_argument("--dev-frac", type=float, default=0.6,
                        help="share of bars given to the in-sample dev split (0 < x < 1)")
    args = parser.parse_args()
    args._start = (today - pd.Timedelta(days=args.days)).strftime("%Y-%m-%d")
    return args


def intraday_annual_factor(asset_class: str, interval: str) -> int:
    """Bars per year at this interval, used only to annualise volatility ratios.

    Crypto compounds every minute of every day of the year; NSE compounds only
    during the 375-minute session on its 252 trading days. A daily-style 252
    factor would inflate intraday Sharpe by an order of magnitude.
    """
    minutes = interval_minutes(interval)
    if asset_class == "crypto":
        return int(365 * 24 * 60 / minutes)
    return int(252 * 375 / minutes)


def fresh_intraday(template, asset_class: str, interval: str):
    """Stateful presets must restart per symbol and per split.

    Reusing one instance across the dev/test split would leak in-session state
    into the out-of-sample run and inflate the unseen result.
    """
    if isinstance(template, BuyAndHold):
        return BuyAndHold()
    params = dict(template.params)
    params["interval"] = interval
    if isinstance(template, (OpeningRangeBreakout, VwapDip)):
        return type(template)(**params, asset_class=asset_class)
    return type(template)(**params)


def split_chronological(frame: pd.DataFrame, dev_frac: float
                        ) -> tuple[pd.DataFrame, pd.DataFrame]:
    cut = int(round(len(frame) * dev_frac))
    return frame.iloc[:cut], frame.iloc[cut:]


def evidence(trades: int, strategy: str) -> str:
    if strategy == BASELINE:
        return "baseline"
    if trades < MIN_TRADES:
        return f"insufficient evidence (<{MIN_TRADES} trades)"
    return f"ok ({trades} trades)"


def evaluate(symbol: str, frame: pd.DataFrame, interval: str, cash: float,
             dev_frac: float) -> list[dict]:
    instrument = data.BY_SYMBOL[symbol]
    cost = cost_model_for(instrument.asset_class, market=INTRADAY)
    annual = intraday_annual_factor(instrument.asset_class, interval)
    dev, test = split_chronological(frame, dev_frac)
    rows: list[dict] = []

    for label, window in (("dev", dev), ("test", test)):
        if len(window) < 30:
            continue
        for template in intraday_presets(instrument.asset_class):
            result = run_backtest(
                window,
                fresh_intraday(template, instrument.asset_class, interval),
                cost,
                initial_cash=cash,
                symbol=symbol,
            )
            metrics = result.metrics(annual_factor=annual)
            metrics["period"] = label
            metrics["interval"] = interval
            metrics["evidence"] = evidence(metrics["trades"], metrics["strategy"])
            rows.append(metrics)
    return rows


def aggregate(rows: list[dict], period: str, min_trades: int = MIN_TRADES) -> list[dict]:
    """Per-strategy table across symbols, ranked by mean total return.

    Mean across symbols, trades summed: a formula as simple as the numbers are
    few. An aggregate inherits the evidence gate from its parts.
    """
    frame = pd.DataFrame([r for r in rows if r["period"] == period])
    if frame.empty:
        return []
    # compute_metrics renders non-finite profit factors as the string "inf";
    # the mean of unreadable numbers is unreadable, so they drop out.
    frame["profit_factor"] = pd.to_numeric(frame["profit_factor"], errors="coerce")
    table = frame.groupby("strategy").agg(
        symbols=("symbol", "nunique"),
        total_trades=("trades", "sum"),
        mean_return_pct=("total_return_pct", "mean"),
        mean_sharpe=("sharpe", "mean"),
        mean_sortino=("sortino", "mean"),
        worst_drawdown_pct=("max_drawdown_pct", "min"),
        mean_win_rate_pct=("win_rate_pct", "mean"),
        mean_profit_factor=("profit_factor", "mean"),
    ).reset_index()
    is_baseline = table["strategy"] == BASELINE
    table["evidence"] = [
        "baseline" if base else
        (f"insufficient evidence (<{min_trades} trades)" if trades < min_trades else f"ok ({trades} trades)")
        for base, trades in zip(is_baseline, table["total_trades"])
    ]
    return table.sort_values("mean_return_pct", ascending=False).to_dict("records")


def cost_notes() -> list[str]:
    notes = []
    for asset_class in ("equity", "crypto"):
        cost = cost_model_for(asset_class, market=INTRADAY)
        split = cost.breakdown()
        parts = [f"statutory {split['statutory']}%", f"slippage {split['slippage']}%"]
        if split["stt"]:
            parts.append(f"of which STT {split['stt']}%")
        notes.append(
            f"**{cost.name}**: {split['total']}% per round trip ({', '.join(parts)})"
        )
    return notes


def write_report(path: Path, rows: list[dict], args, symbols: list[str],
                 bar_counts: dict[str, int], failed: list[str]) -> None:
    dev_prog = round(args.dev_frac * 100)
    test_prog = 100 - dev_prog
    annual_eq = intraday_annual_factor("equity", args.interval)
    annual_crypto = intraday_annual_factor("crypto", args.interval)
    lines: list[str] = []
    lines.append("# Intraday strategy comparison (offline)")
    lines.append("")
    lines.append(
        f"Interval `{args.interval}`, cash {args.cash:,.0f}, history from "
        f"{args._start} ({args.days} days requested). Split: chronological — "
        f"dev = first {dev_prog}% of bars (in-sample), test = last {test_prog}% "
        "(out-of-sample). Symbols: " + ", ".join(symbols)
        + (f". Failed to load: {', '.join(failed)}" if failed else "") + "."
    )
    lines.append("")
    lines.append("Costs per round trip, intraday market, 100-unit notional:")
    lines.extend(f"- {note}" for note in cost_notes())
    lines.append("")
    lines.append(
        f"Annualisation: {_fmt_factor(annual_crypto)} bars/year for crypto, "
        f"{_fmt_factor(annual_eq)} for NSE equity at `{args.interval}`. "
        "Volatility-based ratios (Sharpe/Sortino) are not comparable across asset "
        "classes, only across strategies on the same symbol."
    )
    lines.append("")
    lines.append("Bar counts loaded: " + ", ".join(
        f"{symbol} {bar_counts.get(symbol, 0)}" for symbol in symbols))
    lines.append("")
    lines.append(f"Evidence gate: a candidate split with fewer than {MIN_TRADES} "
                 "round trips is marked **insufficient evidence** and should not "
                 "drive the choice. The buy-and-hold baseline is exempt: it exists "
                 "to be beaten, not to win.")
    lines.append("")

    for period, heading in (("test", "Test split (out-of-sample)"),
                            ("dev", "Dev split (in-sample, informational)")):
        lines.append(f"## {heading}")
        lines.append("")
        table = aggregate(rows, period)
        if not table:
            lines.append("_no runs produced results_")
            lines.append("")
            continue
        if period == "test":
            lines.append("Ranked by mean total return across symbols. Ranking only — "
                         "this report does not pick a winner.")
            lines.append("")
        lines.append("| rank | strategy | symbols | trades | mean return % | "
                     "mean Sharpe | mean Sortino | worst DD % | mean win % | "
                     "mean PF | evidence |")
        lines.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for i, row in enumerate(table, start=1):
            pf = row["mean_profit_factor"]
            pf_txt = f"{pf:.2f}" if pd.notna(pf) else "-"
            lines.append(
                f"| {i} | {row['strategy']} | {row['symbols']} | {row['total_trades']} | "
                f"{row['mean_return_pct']:.2f} | {row['mean_sharpe']:.2f} | "
                f"{row['mean_sortino']:.2f} | {row['worst_drawdown_pct']:.2f} | "
                f"{row['mean_win_rate_pct']:.1f} | {pf_txt} | "
                f"{row['evidence']} |"
            )
        lines.append("")
        per_symbol = [r for r in rows if r["period"] == period]
        lines.append("### Per symbol, ranked by total return")
        lines.append("")
        lines.append("| symbol | strategy | return % | trades | win % | PF | "
                     "max DD % | Sharpe | Sortino | exposure % | evidence |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for row in sorted(per_symbol,
                          key=lambda r: (r["symbol"], -r["total_return_pct"])):
            lines.append(
                f"| {row['symbol']} | {row['strategy']} | {row['total_return_pct']:.2f} | "
                f"{row['trades']} | {row['win_rate_pct']:.1f} | "
                f"{row['profit_factor']} | {row['max_drawdown_pct']:.2f} | "
                f"{row['sharpe']:.2f} | {row['sortino']:.2f} | "
                f"{row['exposure_pct']:.1f} | {row['evidence']} |"
            )
        lines.append("")

    lines.append("The report ranks and marks evidence strength only. It does not "
                 "select a default strategy; that decision — and the wording of any "
                 "future default flip — belongs to the operator reading this file.")
    path.write_text("\n".join(lines), encoding="utf-8")


def _fmt_factor(factor: int) -> str:
    return f"{factor:,}"


def main() -> int:
    args = parse_args()
    if not 0.0 < args.dev_frac < 1.0:
        print("--dev-frac must be between 0 and 1", file=sys.stderr)
        return 2
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    unknown = [s for s in symbols if s not in data.BY_SYMBOL]
    if unknown:
        print(f"unknown symbols: {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(sorted(data.BY_SYMBOL))}", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    bar_counts: dict[str, int] = {}
    failed: list[str] = []

    for symbol in symbols:
        instrument = data.BY_SYMBOL[symbol]
        print(f"\n=== {symbol} ({instrument.yfinance_symbol}, {args.interval}) ===")
        try:
            frame = data.load(instrument, start=args._start, interval=args.interval,
                              refresh=args.refresh)
            if frame.empty and not args.refresh:
                # No usable cache yet: a comparison lab bootstraps its own
                # cache rather than demanding --refresh on the very first run.
                print("  cache empty; fetching once")
                frame = data.load(instrument, start=args._start,
                                  interval=args.interval, refresh=True)
        except Exception as exc:
            print(f"  data load failed: {exc}")
            failed.append(symbol)
            continue
        if frame.empty:
            print("  cache empty and refresh unavailable; skipped")
            failed.append(symbol)
            continue

        bar_counts[symbol] = len(frame)
        print(f"  {len(frame)} bars, {frame.index[0]} to {frame.index[-1]}")
        try:
            rows = evaluate(symbol, frame, args.interval, args.cash, args.dev_frac)
        except Exception:
            traceback.print_exc()
            failed.append(symbol)
            continue

        all_rows.extend(rows)
        for row in rows:
            if row["period"] == "test":
                print(
                    f"  {row['strategy']:<24} ret {row['total_return_pct']:>8.2f}%"
                    f"  trades {row['trades']:>3}  {row['evidence']}"
                )

    if not all_rows:
        print("no results produced", file=sys.stderr)
        return 1

    frame = pd.DataFrame(all_rows)
    ordered = [
        "symbol", "period", "interval", "strategy", "total_return_pct", "cagr_pct",
        "ann_vol_pct", "sharpe", "sortino", "max_drawdown_pct", "trades",
        "win_rate_pct", "profit_factor", "avg_bars_held", "exposure_pct",
        "end_equity", "evidence",
    ]
    frame = frame[[c for c in ordered if c in frame.columns]]
    csv_path = OUT_DIR / "intraday_results.csv"
    frame.to_csv(csv_path, index=False)

    report_path = OUT_DIR / "intraday_report.md"
    write_report(report_path, all_rows, args, symbols, bar_counts, failed)

    print(f"\nwrote {csv_path}")
    print(f"wrote {report_path}")
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
