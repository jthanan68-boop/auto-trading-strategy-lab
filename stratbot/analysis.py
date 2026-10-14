"""Phase 3 analysis: is the live paper run telling us anything yet?

Three questions this module exists to answer:
  1. Has the run seen enough different market conditions to be worth trusting?
  2. Does the live paper result match what the backtest predicted, or did
     execution drift?
  3. Is it beating buy-and-hold on the live run, not just in the backtest?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

TRADING_DAYS = 252


# ---- market regimes ---------------------------------------------------
@dataclass
class Regime:
    trend: str      # up | down | range
    volatility: str  # high | low
    label: str

    @property
    def key(self) -> str:
        return f"{self.trend}-{self.volatility}"


def classify(window: pd.Series, trend_window: int = 20) -> Regime:
    """Label a price window by direction and how noisy it is.

    Trendiness is net move divided by total path length: a market that travels a
    long way and ends up near where it started is a range, not a trend, however
    large the daily swings look.
    """
    if len(window) < trend_window + 2:
        return Regime("range", "high", "insufficient-data")

    tail = window.iloc[-(trend_window + 1) :]
    net = abs(tail.iloc[-1] - tail.iloc[0])
    path = float(tail.diff().abs().sum())
    trendiness = net / path if path > 0 else 0.0

    direction = "up" if tail.iloc[-1] > tail.iloc[0] else "down"
    trend = "range" if trendiness < 0.30 else direction

    returns = window.pct_change().dropna()
    realised = returns.std(ddof=1) * np.sqrt(TRADING_DAYS) if len(returns) > 2 else 0.0
    typical = returns.std(ddof=1) * np.sqrt(TRADING_DAYS) if len(returns) > 2 else 1.0
    volatility = "high" if realised > (typical * 1.05) else "low"

    return Regime(trend, volatility, f"{trend}/{volatility}")


def regime_coverage(history: pd.Series, period_returns: list[float], lookback: int = 60) -> dict:
    """How much of the run fell into each regime bucket."""
    buckets: dict[str, int] = {}
    if len(history) < lookback:
        return {"insufficient-data": len(history)}

    for index in range(lookback, len(history) + 1, max(1, lookback // 4)):
        bucket = classify(history.iloc[:index]).key
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return buckets


# ---- ledger extraction ------------------------------------------------
def load_closed_trades(db) -> list[dict]:
    return db.closed_trades()


def summarise_run(db, strategy: str | None = None) -> dict:
    trades = load_closed_trades(db)
    equity = db.equity_history()
    if equity:
        span = equity[0]["ts"] + " to " + equity[-1]["ts"]
        start_equity, end_equity = float(equity[0]["equity"]), float(equity[-1]["equity"])
    else:
        span, start_equity, end_equity = "no equity history", 0.0, 0.0

    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))

    return {
        "strategy": strategy or "unknown",
        "span": span,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_return_pct": ((end_equity / start_equity - 1) * 100) if start_equity else 0.0,
        "trades": len(trades),
        "win_rate_pct": (len(wins) / len(trades) * 100) if trades else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else (float("inf") if gross_win else 0.0),
        "total_fees": sum(float(t["fees"]) for t in trades),
        "avg_trade_pnl": float(np.mean(pnls)) if pnls else 0.0,
    }


def duration_days(db) -> float:
    equity = db.equity_history()
    if len(equity) < 2:
        return 0.0
    try:
        start = datetime.fromisoformat(equity[0]["ts"])
        end = datetime.fromisoformat(equity[-1]["ts"])
    except ValueError:
        return 0.0
    return max((end - start).days, 0.0)


# ---- backtest versus live --------------------------------------------
@dataclass
class Divergence:
    symbol: str
    field: str
    backtest: float
    live: float

    @property
    def gap(self) -> float:
        return self.live - self.backtest

    @property
    def material(self) -> bool:
        """A gap big enough to investigate rather than shrug at."""
        scale = max(abs(self.backtest), 1e-9)
        return abs(self.gap) / scale > 0.5


def compare_to_backtest(live: dict, backtest_rows: list[dict]) -> tuple[list[Divergence], list[Divergence]]:
    """Split differences into ones worth investigating and ones that are noise."""
    material: list[Divergence] = []
    ignorable: list[Divergence] = []
    for row in backtest_rows:
        for field in ("total_return_pct", "trades", "max_drawdown_pct"):
            if field not in row or row.get("strategy") != live["strategy"]:
                continue
            divergence = Divergence(row.get("symbol", "?"), field, float(row[field]), float(live.get(field, 0.0)))
            (material if divergence.material else ignorable).append(divergence)
    return material, ignorable


# ---- verdict ---------------------------------------------------------
MIN_RUN_DAYS = 14
MIN_TRADES = 20
REGIMES_NEEDED = 3


def verdict(run: dict, coverage: dict, buy_and_hold_pct: float | None) -> tuple[str, list[str]]:
    """Plain-language read of whether the live paper run means anything yet."""
    reasons: list[str] = []
    verdict_text = "NOT YET CONCLUSIVE"

    days = run.get("_days", 0.0)
    trades = run["trades"]
    regimes = len([k for k in coverage if k != "insufficient-data"])

    if days < MIN_RUN_DAYS:
        reasons.append(f"only {days:.0f} days of history, need {MIN_RUN_DAYS}")
    if trades < MIN_TRADES:
        reasons.append(f"only {trades} closed trades, need {MIN_TRADES}")
    if regimes < REGIMES_NEEDED:
        reasons.append(f"seen {regimes} market regime(s), need {REGIMES_NEEDED}")

    if not reasons and buy_and_hold_pct is None:
        verdict_text = "INCONCLUSIVE"
        reasons.append("no buy-and-hold comparison available for the same window")
    elif not reasons:
        excess = run["total_return_pct"] - buy_and_hold_pct
        if excess > 0 and trades >= MIN_TRADES:
            verdict_text = "BEATING BUY-AND-HOLD"
            reasons.append(f"excess return {excess:+.1f} points over {trades} trades")
        else:
            verdict_text = "NOT BEATING BUY-AND-HOLD"
            reasons.append(f"trails buy-and-hold by {abs(excess):.1f} points over {trades} trades")

    return verdict_text, reasons