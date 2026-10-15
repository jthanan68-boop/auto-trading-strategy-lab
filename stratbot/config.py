"""Phase 1 configuration and the hard risk limits.

Risk limits live here and nowhere else. The strategy proposes; this module
vetoes. Nothing in strategies/ may raise a limit, because nothing in
strategies/ is allowed to talk to the broker directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("STRATBOT_DB", BASE_DIR / "data" / "bot.sqlite3"))

# Phase 1 is simulator-only. There is no broker credential in this codebase and
# no code path that reads one.
TRADING_MODE = "paper"


@dataclass(frozen=True)
class RiskLimits:
    """Hard caps enforced by risk.RiskManager on every order."""

    max_position_value: float = 100_000.0
    max_total_exposure: float = 300_000.0
    max_daily_loss: float = 10_000.0
    stop_loss_pct: float = 0.08
    max_orders_per_hour: int = 20
    max_orders_per_day: int = 100
    max_position_pct_of_equity: float = 0.50
    # Don't let cap-shaving produce a dust order that costs more in fees than it risks.
    min_order_value: float = 500.0
    # The strategy reads DAILY candles, so the newest bar is hours old by design
    # and only becomes unusable once the feed itself stops publishing. Judge
    # staleness in days against the expected bar frequency, not in hours.
    max_bar_age_days: int = 4
    # Warn (without halting) once a bar is this old, so a lagging feed is visible
    # in the log instead of silently trading on yesterday's price.
    warn_bar_age_days: float = 2.0
    # If an order's fate is unknown this long, stop rather than risk a double fill.
    order_confirmation_timeout_seconds: int = 30
    reconcile_interval_seconds: int = 300


@dataclass(frozen=True)
class BotConfig:
    symbols: tuple[str, ...] = ("BTC", "ETH", "RELIANCE", "TCS", "INFY")
    strategy_name: str = "donchian-55/20"
    initial_cash: float = 500_000.0
    # Symbols' target weights are summed and scaled down to this before any order
    # is sized, so N strategies each asking for 100% cannot request N * 100%.
    max_gross_exposure: float = 1.0
    poll_seconds: int = 300
    limits: RiskLimits = field(default_factory=RiskLimits)

    @property
    def is_paper(self) -> bool:
        return TRADING_MODE == "paper"


CONFIG = BotConfig()