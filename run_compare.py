#!/usr/bin/env python
"""Compare paper runs that live in separate sqlite ledgers.

    python run_compare.py data/bot.sqlite3 data/orb.sqlite3 data/vwap.sqlite3

Each ledger is read as-is (no engine is started, nothing is written): the equity
history supplies the curve, the closed trades supply the trade statistics. Curves
are resampled to calendar days for the volatility ratios, because a 60-second
poll prints thousands of highly serially-dependent points that flatter Sharpe;
an instrument mix (NSE + crypto) then reuses the book-wide 252 factor, so treat
Sharpe/Sortino as rough cross-run yardsticks, not precise values.

The table is a report, not a verdict. It ranks nothing by default — read it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

from stratbot.db import Database
from stratlab.backtest import BacktestResult, compute_metrics

REQUIRED_ROWS = 3   # below this a ledger cannot say anything at all


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare multiple paper-run ledgers.")
    parser.add_argument("databases", nargs="+", help="sqlite ledger paths to compare")
    parser.add_argument("--sort", default="net", choices=["net", "sharpe", "trades"],
                        help="column to sort the table by (net return by default)")
    args = parser.parse_args()
    missing = [p for p in args.databases if not Path(p).exists()]
    if missing:
        print(f"no such ledger file(s): {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)
    return args


def strategy_of(db: Database) -> str:
    """The strategy of the run, parsed from its last engine_start event."""
    for event in reversed(db.recent_risk_events(limit=400)):
        if event.get("kind") != "engine_start":
            continue
        match = re.search(r"strategy=([\w/\-\.]+)", event.get("detail", ""))
        return match.group(1) if match else "unknown"
    return "unknown"


def daily_curve(rows: list[dict]) -> pd.Series:
    index = pd.DatetimeIndex([pd.Timestamp(r["ts"]) for r in rows])
    return pd.Series([float(r["equity"]) for r in rows], index=index) \
        .sort_index().resample("1D").last().dropna()


def trade_rows(raw: list[dict]) -> list:
    from stratlab.backtest import Trade

    trades = []
    for row in raw:
        trades.append(Trade(
            symbol=row["symbol"],
            entry_date=pd.Timestamp(row["entry_ts"]),
            exit_date=pd.Timestamp(row["exit_ts"]),
            entry_price=float(row["entry_price"]),
            exit_price=float(row["exit_price"]),
            quantity=float(row["qty"]),
            pnl=float(row["pnl"]),
            return_pct=(float(row["pnl"]) / (float(row["entry_price"]) * float(row["qty"]))
                        * 100.0) if float(row["entry_price"]) * float(row["qty"]) else 0.0,
            costs=float(row["fees"]),
            bars_held=0,
        ))
    return trades


def examine(path: str) -> dict | None:
    db = Database(Path(path))
    rows = db.equity_history(limit=100_000)
    if len(rows) < REQUIRED_ROWS:
        print(f"{Path(path).name}: only {len(rows)} equity row(s), skipping",
              file=sys.stderr)
        return None
    equity = daily_curve(rows)
    raw_trades = db.closed_trades()

    curve = equity if len(equity) >= REQUIRED_ROWS else \
        pd.Series([float(r["equity"]) for r in rows],
                  index=pd.DatetimeIndex([pd.Timestamp(r["ts"]) for r in rows]))
    result = BacktestResult(
        symbol=Path(path).name,
        strategy=strategy_of(db),
        equity_curve=curve,
        trades=trade_rows(raw_trades),
        initial_cash=float(rows[0]["equity"]),
    )
    try:
        metrics = compute_metrics(result)
    except OverflowError:
        # A ledger spanning less than a day drives the CAGR exponent to 1/years
        # == billions, which overflows compute_metrics. Keep the run reportable
        # rather than crashing on its age; span-sensitive ratios drop out.
        equity = curve
        drawdown = float((equity / equity.cummax() - 1.0).min() * 100.0)
        total = (float(equity.iloc[-1]) / float(rows[0]["equity"]) - 1.0) * 100.0
        pnls = [float(t["pnl"]) for t in raw_trades]
        wins = [p for p in pnls if p > 0]
        gross_win, gross_loss = sum(wins), abs(sum(p for p in pnls if p <= 0))
        metrics = {
            "symbol": result.symbol,
            "strategy": result.strategy,
            "start": str(equity.index[0].date()),
            "end": str(equity.index[-1].date()),
            "total_return_pct": round(total, 2),
            "cagr_pct": None,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_drawdown_pct": round(drawdown, 2),
            "trades": len(raw_trades),
            "win_rate_pct": round(len(wins) / len(pnls) * 100.0, 1) if pnls else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "exposure_pct": 0.0,
            "end_equity": float(equity.iloc[-1]),
        }
        metrics["notes_overflow"] = True

    span_days = (curve.index[-1] - curve.index[0]).days
    metrics["label"] = Path(path).name
    metrics["span_days"] = span_days
    metrics["cycles"] = len(rows)
    closed_pnl = sum(float(t["pnl"]) for t in raw_trades)
    metrics["closed_pnl"] = round(closed_pnl, 2)
    metrics["fees"] = round(sum(float(t["fees"]) for t in raw_trades), 2)
    metrics["notes"] = "resampled daily" if len(equity) >= REQUIRED_ROWS \
        else "per-cycle (span too short for daily bars)"
    # A drawdown measured on the raw curve in addition, because a one-day
    # intraday crash can net out by the calendar-day close.
    raw_curve = pd.Series([float(r["equity"]) for r in rows],
                          index=pd.DatetimeIndex([pd.Timestamp(r["ts"]) for r in rows])
                          ).sort_index()
    metrics["max_intraday_dd_pct"] = round(
        float((raw_curve / raw_curve.cummax() - 1.0).min() * 100.0), 2)
    return metrics


def main() -> int:
    args = parse_args()
    reports = [m for m in (examine(path) for path in args.databases) if m is not None]
    if not reports:
        print("nothing to compare", file=sys.stderr)
        return 1

    ordered = [
        "label", "strategy", "cycles", "span_days", "total_return_pct",
        "max_drawdown_pct", "max_intraday_dd_pct", "sharpe", "sortino",
        "trades", "win_rate_pct", "closed_pnl", "fees", "end_equity", "notes",
    ]
    frame = pd.DataFrame(reports)
    frame = frame[[c for c in ordered if c in frame.columns]]
    if args.sort == "net":
        frame = frame.sort_values("total_return_pct", ascending=False)
    elif args.sort in frame.columns:
        frame = frame.sort_values(args.sort, ascending=False)

    print()
    print(frame.to_string(index=False, max_colwidth=22))
    print()
    print("Read each column independently: a run ahead on return may be ahead on "
          "drawdown too. Sharpe/Sortino use the book-wide 252 annualisation and a "
          "daily resample, so an NSE+crypto mixed book gets a blended factor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
