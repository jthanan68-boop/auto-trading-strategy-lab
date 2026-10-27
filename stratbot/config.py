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
    """Hard caps enforced by risk.RiskManager on every order.

    Scaled for ₹5,000 starting capital (1/100 of original ₹500,000).
    """

    max_position_value: float = 1_000_000.0      # was 100_000_000.0
    max_total_exposure: float = 2_500.0          # was 300_000.0, reduced to 50% of capital
    max_daily_loss: float = 50.0                 # was 10_000.0, reduced to 1% of capital
    stop_loss_pct: float = 0.03                  # 3% for ₹5K capital (was 8%)
    max_orders_per_hour: int = 120
    max_orders_per_day: int = 500
    max_position_pct_of_equity: float = 0.05      # 5% per position max
    # Don't let cap-shaving produce a dust order that costs more in fees than it risks.
    min_order_value: float = 0.0
    # The strategy reads DAILY candles, so the newest bar is hours old by design
    # and only becomes unusable once the feed itself stops publishing. Judge
    # staleness in days against the expected bar frequency, not in hours.
    max_bar_age_days: int = 4
    # Warn (without halting) once a bar is this old, so a lagging feed is visible
    # in the log instead of silently trading on yesterday's price.
    warn_bar_age_days: float = 2.0
    # OrderConfirmation/timeout budgets stay at the end; see fields below.
    # Intraday style judges bar age in MINUTES against these budgets while the
    # symbol's asset class is publishing bars today (risk.check_bar_freshness).
    # None keeps the day-based checks above in charge, which is daily mode.
    max_bar_age_minutes: int | None = None
    warn_bar_age_minutes: float | None = None
    # If an order's fate is unknown this long, stop rather than risk a double fill.
    order_confirmation_timeout_seconds: int = 30
    reconcile_interval_seconds: int = 300


def intraday_limits() -> RiskLimits:
    """Fresh limits for the intraday session profile.

    Only the stop and the freshness gates change: a book held for minutes dies
    on a 1% adverse move rather than an 8% one, and bar currency is judged in
    minutes rather than days. max_position_value, max_total_exposure,
    max_daily_loss, max_position_pct_of_equity and the 20/hour + 100/day order
    caps are carried through at their configured values on purpose.
    """
    return RiskLimits(
        stop_loss_pct=0.01, max_bar_age_minutes=120, warn_bar_age_minutes=30,
    )


@dataclass(frozen=True)
class BotConfig:
    symbols: tuple[str, ...] = ("RELIANCE", "TCS", "INFY", "HDFCBANK", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK")
    strategy_name: str = "orb-15min"
    initial_cash: float = 5_000.0
    # Symbols' target weights are summed and scaled down to this before any order
    # is sized, so N strategies each asking for 100% cannot request N * 100%.
    max_gross_exposure: float = 1.0
    poll_seconds: int = 60
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
    refresh_interval_minutes: int = 5
    refresh_on_start: bool = True
    # Stochastic slippage on paper fills. None keeps the deterministic
    # cost-model fill that the historical tests pin exactly.
    execution_model: ExecutionModel | None = None
    # "daily" keeps the original multi-week behavior; "intraday" trades inside a
    # single session and squares off before it closes.
    trading_style: str = "intraday"
    # Bar interval for intraday mode ("5m", "15m", "30m", "60m"). Daily mode
    # always reads 1d bars regardless of this value.
    bar_interval: str = "5m"
    # How many days of intraday history the feed keeps warm. Must stay well
    # inside Yahoo's ~60-day intraday window; ORB and VWAP only need one session.
    days_history: int = 12
    # Forced exits begin this many completed bars before the session ends, so a
    # queued square-off still fills at an open inside the same session.
    square_off_buffer_bars: int = 3
    limits: RiskLimits = field(default_factory=RiskLimits)

    @property
    def is_paper(self) -> bool:
        return TRADING_MODE == "paper"


CONFIG = BotConfig()