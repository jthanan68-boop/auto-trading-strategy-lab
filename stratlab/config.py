"""Configuration: universe, periods, and which strategies to compare."""

from __future__ import annotations

from . import data

# Signals are formed on these dates and filled at the next bar's open.
DEV_END = "2024-01-01"
DATA_START = "2018-01-01"
INITIAL_CASH = 100_000.0

DEFAULT_SYMBOLS = ["NIFTY50", "RELIANCE", "TCS", "INFY", "HDFCBANK", "ITC", "SBIN", "BTC", "ETH"]
# The full breadth set. Use `--full` so the quick default stays fast.
FULL_SYMBOLS = DEFAULT_SYMBOLS + [s for s in data.BROAD_SYMBOLS if s not in DEFAULT_SYMBOLS]


def build_strategies() -> list:
    """Strategies to compare, always led by the buy-and-hold benchmark."""
    from .strategies.library import (
        BuyAndHold,
        DonchianBreakout,
        BuyDipHold,
        RsiStrategy,
        SmaCrossover,
    )

    return [
        BuyAndHold(),
        SmaCrossover(20, 100),
        SmaCrossover(50, 200),
        RsiStrategy(14, 30, 70),
        DonchianBreakout(55, 20),
        BuyDipHold(60, 0.12),
    ]