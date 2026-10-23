"""Phase 1 configuration and the hard risk limits.

Risk limits live here and nowhere else. The strategy proposes; this module
vetoes. Nothing in strategies/ may raise a limit, because nothing in
strategies/ is allowed to talk to the broker directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from stratbot.broker.paper import ExecutionModel

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("STRATBOT_DB", BASE_DIR / "data" / "bot.sqlite3"))

# Phase 1 is simulator-only. There is no broker credential in this codebase and
# no code path that reads one.
TRADING_MODE = "paper"


@dataclass(frozen=True)
class RiskLimits:
    """Hard caps enforced by risk.RiskManager on every order."""

    max_position_value: float = 100_000_000.0
    max_total_exposure: float = 300_000.0
    max_daily_loss: float = 10_000.0
    stop_loss_pct: float = 0.08
    max_orders_per_hour: int = 20
    max_orders_per_day: int = 100
    max_position_pct_of_equity: float = 1.0
    # Don't let cap-shaving produce a dust order that costs more in fees than it risks.
    min_order_value: float = 0.0
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
    # Stop-loss data policy. False (default) evaluates stops on completed daily
    # bars only, because this engine fills at the next bar's open and has no
    # intraday execution model: a stop triggered by a forming candle would be
    # queued and then filled at a later open anyway, so it would claim intraday
    # protection while providing none. True opts into triggering on the live
    # candle, which records the trigger price and signal bar explicitly but
    # still executes at the next bar's open.
    stop_loss_intraday: bool = False
    # Intraday protection from live quotes. When True (default), an open
    # position is also checked against a fresh live quote every cycle, so a
    # breach does not have to wait for the daily candle to close. The trigger is
    # intraday; execution is deliberately not: the stop still queues and fills
    # at the next bar's open like every other order, so a realised loss can
    # exceed 8% across a gap. Quotes are marked/stale-aware: only a quote the
    # QuoteService itself calls usable (positive price, fresh enough) triggers;
    # anything else leaves the daily-bar stop in charge.
    stop_loss_use_live_quotes: bool = True
    # How often cached market data may be re-downloaded. The engine reads daily
    # candles, so refreshing more often than this is wasted requests; the feed
    # also refuses to present a stale symbol as a current price.
    refresh_interval_minutes: int = 60
    refresh_on_start: bool = True
    # Stochastic slippage on paper fills. None keeps the deterministic
    # cost-model fill that the historical tests pin exactly.
    execution_model: ExecutionModel | None = None
    limits: RiskLimits = field(default_factory=RiskLimits)

    @property
    def is_paper(self) -> bool:
        return TRADING_MODE == "paper"


CONFIG = BotConfig()