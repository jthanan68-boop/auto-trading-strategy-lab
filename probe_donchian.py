"""Reproduce Finding 1: why does Donchian 55/20 emit no signal?

    python probe_donchian.py BTC ETH RELIANCE TCS INFY
"""

from __future__ import annotations

import sys

from stratlab import data
from stratlab.strategies.library import DonchianBreakout


def probe(symbol: str) -> None:
    instrument = data.BY_SYMBOL[symbol]
    frame = data.load(instrument)
    close = frame["close"]
    n = len(close)

    entry, exit_ = 55, 20
    print(f"--- {symbol} ({instrument.yfinance_symbol}) ---")
    print(f"  bars available      : {n}")
    print(f"  last bar            : {frame.index[-1].date()}")
    print(f"  warm-up needs       : {entry + 1} bars  -> {'OK' if n >= entry + 1 else 'INSUFFICIENT'}")

    if n < entry + 1:
        print("  result              : insufficient history\n")
        return

    strategy = DonchianBreakout(entry, exit_)
    target = strategy.position(frame)
    print(f"  strategy.position() : {target}")

    prior = close.iloc[:-1]
    entry_high = prior.iloc[-entry:].max()
    exit_low = prior.iloc[-exit_:].min()
    last = close.iloc[-1]

    print(f"  last close          : {last:,.2f}")
    print(f"  {entry}-day high (prior)   : {entry_high:,.2f}")
    print(f"  {exit_}-day low (prior)    : {exit_low:,.2f}")
    print(f"  in position         : {strategy._in_position}")

    if strategy._in_position:
        hit = last <= exit_low
        print(f"  exit condition      : close <= exit_low -> {hit}")
        print("  result              : HOLD (already long, no new entry)")
    else:
        entered = last >= entry_high
        margin = (last / entry_high - 1) * 100
        print(f"  entry condition     : close >= {entry}-day high -> {entered}")
        print(f"  short by            : {margin:+.2f}% ({(entry_high - last):,.2f})")
        print("  result              : FLAT, no breakout")
    print()


if __name__ == "__main__":
    symbols = sys.argv[1:] or ["BTC", "ETH", "RELIANCE", "TCS", "INFY"]
    for s in symbols:
        try:
            probe(s.upper())
        except Exception as exc:
            print(f"--- {s} --- FAILED: {exc}\n")