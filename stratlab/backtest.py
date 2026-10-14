"""Backtest engine.

Execution rule, and the single most important detail in the file: a signal
computed on the close of bar t is filled at the OPEN of bar t+1, never at the
close of bar t. Filling at the bar's own close means trading on information that
was not available when the signal was formed, which flatters every result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import CostModel

TRADING_DAYS = 252
CALENDAR_DAYS = 365


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    return_pct: float
    costs: float
    bars_held: int


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    equity_curve: pd.Series
    trades: list[Trade] = field(default_factory=list)
    exposure_pct: float = 0.0
    bars: int = 0
    initial_cash: float = 0.0

    def metrics(self, annual_factor: int = TRADING_DAYS) -> dict:
        return compute_metrics(self, annual_factor)


def _execute(
    cash: float,
    quantity: float,
    reference_price: float,
    target_weight: float,
    cost: CostModel,
) -> tuple[float, float, float, float]:
    """Move the book to target_weight, returning (cash, quantity, traded_notional, fees)."""
    side = "BUY" if target_weight > 0 else "SELL"
    fill = cost.fill_price(reference_price, side)
    equity_ref = cash + quantity * reference_price
    target_value = equity_ref * target_weight
    delta_value = target_value - quantity * reference_price

    if delta_value > 0:
        wanted = delta_value / fill
        affordable = max(cash / fill, 0.0)
        qty = min(wanted, affordable)
        if qty <= 0:
            return cash, quantity, 0.0, 0.0
        notional = qty * fill
        fees = cost.charges("BUY", notional)
        if notional + fees > cash:
            qty = max(cash / (fill * 1.01), 0.0)
            notional = qty * fill
            fees = cost.charges("BUY", notional)
        return cash - notional - fees, quantity + qty, notional, fees

    if delta_value < 0 and quantity > 0:
        wanted = -delta_value / fill
        qty = min(wanted, quantity)
        if qty <= 0:
            return cash, quantity, 0.0, 0.0
        notional = qty * fill
        fees = cost.charges("SELL", notional)
        return cash + notional - fees, quantity - qty, notional, fees

    return cash, quantity, 0.0, 0.0


def run_backtest(
    data: pd.DataFrame,
    strategy,
    cost: CostModel,
    initial_cash: float = 100_000.0,
    symbol: str = "",
) -> BacktestResult:
    """Replay bars through a strategy. `data` needs open/high/low/close columns."""
    frame = data.dropna(subset=["open", "close"]).reset_index(drop=False)
    if len(frame) < 3:
        raise ValueError("need at least 3 bars to backtest")

    index_col = frame.columns[0]
    frame = frame.set_index(index_col)
    index = pd.DatetimeIndex(frame.index)

    cash = initial_cash
    quantity = 0.0
    weight = 0.0
    entry: dict | None = None
    trades: list[Trade] = []
    total_fees = 0.0
    exposure_bars = 0
    equity_rows: list[float] = []
    equity_index: list[pd.Timestamp] = []

    opens = frame["open"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    n = len(frame)

    for i in range(n - 1):
        history = frame.iloc[: i + 1]
        try:
            target = float(strategy.position(history))
        except Exception:  # a broken strategy must not silently look profitable
            raise

        if math.isnan(target):
            target = weight  # NaN means "hold whatever we have"
        target = min(max(target, 0.0), 1.0)

        if abs(target - weight) < 1e-9:
            exposure_bars += 1 if weight > 0 else 0
            equity_rows.append(cash + quantity * closes[i])
            equity_index.append(index[i])
            continue

        next_open = opens[i + 1]
        new_cash, new_qty, notional, fees = _execute(
            cash, quantity, next_open, target, cost
        )
        total_fees += fees

        was_long = weight > 0
        side = "BUY" if target > weight else "SELL"

        if side == "BUY" and not was_long and notional > 0:
            entry = {
                "date": index[i + 1],
                "price": cost.fill_price(next_open, "BUY"),
                "quantity": notional / cost.fill_price(next_open, "BUY"),
                "costs": fees,
                "bar": i + 1,
            }
        elif side == "SELL" and was_long and entry is not None:
            exit_price = cost.fill_price(next_open, "SELL")
            exit_qty = entry["quantity"]
            proceeds = exit_price * exit_qty
            pnl = proceeds - entry["price"] * exit_qty - entry["costs"] - fees
            basis = entry["price"] * exit_qty
            trades.append(
                Trade(
                    symbol=symbol,
                    entry_date=entry["date"],
                    exit_date=index[i + 1],
                    entry_price=entry["price"],
                    exit_price=exit_price,
                    quantity=exit_qty,
                    pnl=pnl,
                    return_pct=(pnl / basis * 100.0) if basis else 0.0,
                    costs=entry["costs"] + fees,
                    bars_held=(i + 1) - entry["bar"],
                )
            )
            entry = None

        cash, quantity, weight = new_cash, new_qty, target

        if weight > 0:
            exposure_bars += 1
        equity_rows.append(cash + quantity * closes[i + 1])
        equity_index.append(index[i + 1])

    # The loop stops one bar early so that bar t+1's open is always available,
    # which leaves the final close unrecorded. Without this the equity curve
    # silently ignores the last price move and understates the return.
    if not equity_index or equity_index[-1] != index[-1]:
        if weight > 0:
            exposure_bars += 1
        equity_rows.append(cash + quantity * closes[-1])
        equity_index.append(index[-1])

    equity = pd.Series(equity_rows, index=pd.DatetimeIndex(equity_index), name="equity")

    if entry is not None:
        last_close = closes[-1]
        exit_price = last_close
        exit_qty = entry["quantity"]
        pnl = exit_price * exit_qty - entry["price"] * exit_qty - entry["costs"]
        basis = entry["price"] * exit_qty
        trades.append(
            Trade(
                symbol=symbol,
                entry_date=entry["date"],
                exit_date=index[-1],
                entry_price=entry["price"],
                exit_price=exit_price,
                quantity=exit_qty,
                pnl=pnl,
                return_pct=(pnl / basis * 100.0) if basis else 0.0,
                costs=entry["costs"],
                bars_held=(n - 1) - entry["bar"],
            )
        )

    if len(equity) == 0:
        equity = pd.Series([initial_cash], index=index[:1], name="equity")

    return BacktestResult(
        symbol=symbol,
        strategy=strategy.name,
        equity_curve=equity,
        trades=trades,
        exposure_pct=(exposure_bars / len(equity) * 100.0) if len(equity) else 0.0,
        bars=len(equity),
        initial_cash=initial_cash,
    )


def compute_metrics(result: BacktestResult, annual_factor: int = TRADING_DAYS) -> dict:
    equity = result.equity_curve.dropna()
    if len(equity) < 2:
        return {"trades": 0}

    start_equity = float(result.initial_cash or equity.iloc[0])
    end_equity = float(equity.iloc[-1])
    total_return = (end_equity / start_equity - 1.0) * 100.0

    span_days = (equity.index[-1] - equity.index[0]).days
    years = max(span_days / 365.25, 1e-9)
    cagr = ((end_equity / start_equity) ** (1 / years) - 1.0) * 100.0 if end_equity > 0 else -100.0

    returns = equity.pct_change().dropna()
    ann_vol = returns.std(ddof=1) * math.sqrt(annual_factor) * 100 if len(returns) > 1 else 0.0
    sharpe = (returns.mean() / returns.std(ddof=1)) * math.sqrt(annual_factor) if returns.std(ddof=1) else 0.0

    downside = returns[returns < 0]
    downside_dev = downside.std(ddof=1) if len(downside) > 1 else np.nan
    sortino = (returns.mean() / downside_dev) * math.sqrt(annual_factor) if downside_dev and not np.isnan(downside_dev) else 0.0

    drawdown = equity / equity.cummax() - 1.0
    max_drawdown = float(drawdown.min() * 100.0)
    calmar = cagr / abs(max_drawdown) if max_drawdown else 0.0

    trades = result.trades
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    avg_bars = float(np.mean([t.bars_held for t in trades])) if trades else 0.0

    return {
        "symbol": result.symbol,
        "strategy": result.strategy,
        "start": str(equity.index[0].date()),
        "end": str(equity.index[-1].date()),
        "total_return_pct": round(total_return, 2),
        "cagr_pct": round(cagr, 2),
        "ann_vol_pct": round(ann_vol, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "calmar": round(calmar, 2),
        "trades": len(trades),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2) if np.isfinite(profit_factor) else "inf",
        "avg_bars_held": round(avg_bars, 1),
        "exposure_pct": round(result.exposure_pct, 1),
        "end_equity": round(end_equity, 2),
    }