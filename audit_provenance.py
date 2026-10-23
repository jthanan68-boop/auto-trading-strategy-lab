"""Provenance audit: what price does each symbol actually come from, and how old?

Read-only. Does not modify configuration or place orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from stratlab import data as price_data

TARGETS = ["BTC", "ETH", "RELIANCE", "TCS", "INFY"]
now = datetime.now(timezone.utc)

print("=" * 96)
print("MARKET-DATA PROVENANCE AUDIT  (read-only)")
print("=" * 96)
print(f"audited at (local): {datetime.now().isoformat(timespec='seconds')}")
print(f"audited at (UTC)  : {now.isoformat(timespec='seconds')}")
print()

rows = []
for symbol in TARGETS:
    instrument = price_data.BY_SYMBOL[symbol]
    path = price_data.CACHE_DIR / f"{symbol}.csv"
    frame = price_data.load(instrument)

    last_date = frame.index[-1]
    last_row = frame.iloc[-1]
    age_days = (now - pd.Timestamp(last_date).to_pydatetime().replace(tzinfo=timezone.utc)).total_seconds() / 86400

    rows.append(
        {
            "symbol": symbol,
            "yahoo_symbol": instrument.yfinance_symbol,
            "asset_class": instrument.asset_class,
            "bars": len(frame),
            "first": str(frame.index[0].date()),
            "last_candle_date": str(last_date.date()),
            "close": round(float(last_row["close"]), 4),
            "open": round(float(last_row["open"]), 4),
            "high": round(float(last_row["high"]), 4),
            "low": round(float(last_row["low"]), 4),
            "age_days": round(age_days, 2),
            "cache_mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "cache_bytes": path.stat().st_size,
        }
    )

print(f"{'symbol':<10}{'yahoo symbol':<16}{'class':<10}{'bars':>6}{'first':>12}{'last candle':>14}"
      f"{'close':>13}{'age(d)':>9}")
print("-" * 96)
for r in rows:
    print(f"{r['symbol']:<10}{r['yahoo_symbol']:<16}{r['asset_class']:<10}{r['bars']:>6}"
          f"{r['first']:>12}{r['last_candle_date']:>14}{r['close']:>13,.2f}{r['age_days']:>9.2f}")

print()
print("OHLC of the newest stored candle (provenance of the exact bar consumed):")
for r in rows:
    print(f"  {r['symbol']:<10} O={r['open']:>12,.2f}  H={r['high']:>12,.2f}  "
          f"L={r['low']:>12,.2f}  C={r['close']:>12,.2f}")

print()
print("cache file provenance:")
for r in rows:
    print(f"  {r['symbol']:<10} {r['cache_bytes']:>7} bytes  last written {r['cache_mtime']}")

print()
print("=" * 96)
print("PROVIDENCE NOTES")
print("=" * 96)
print("  provider      : Yahoo Finance via yfinance.download (history),")
print("                  and query1.finance.yahoo.com/v8/finance/chart (unused in current path)")
print("  storage       : plain CSV in data/cache/<SYMBOL>.csv")
print("  provenance col: NONE. The CSV schema is date,open,high,low,close,volume.")
print("                  There is no provider id, fetch timestamp, or source field,")
print("                  so per-bar provenance cannot be recovered from the file alone.")
print("  adjust mode   : auto_adjust=True (dividend/split adjusted closes)")

print()
print("  Indices are timestamp-naive dates taken from the yfinance daily frame.")
print("  A bar is stamped with the DATE, not the session open/close time, so the")
print("  1-day resolution and the tz-naive index mean the exact exchange timezone")
print("  and session time are not recoverable from the stored data.")

print()
maxage = max(r["age_days"] for r in rows)
print(f"  oldest bar age: {maxage:.2f} days")
print(f"  newest bar age: {min(r['age_days'] for r in rows):.2f} days")
print(f"  staleness halt threshold (config.RiskLimits.max_bar_age_days): 4 days")
print(f"  warn threshold (warn_bar_age_days): 2 days")