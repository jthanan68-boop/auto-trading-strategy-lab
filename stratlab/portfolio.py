"""Portfolio backtester for rules that hold several instruments at once.

The single-instrument engine in backtest.py cannot evaluate a cross-sectional
rule: ranking forty names against each other and holding the best ten is a
different object, with shared capital and periodic rebalancing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import CostModel

TRADING_DAYS = 252


@dataclass
class Position:
    symbol: str
    qty: float
    price: float
    entry_date: pd.Timestamp


@dataclass
class PortfolioResult:
    equity: pd.Series
    trades: int = 0
    rebalances: int = 0
    initial_cash: float = 0.0
    target_log: list[dict] = field(default_factory=list)

    def metrics(self, annual_factor: int = TRADING_DAYS) -> dict:
        equity = self.equity.dropna()
        if len(equity) < 2:
            return {"trades": 0, "rebalances": self.rebalances}

        start = float(self.initial_cash or equity.iloc[0])
        end = float(equity.iloc[-1])
        total_return = (end / start - 1) * 100
        years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
        cagr = ((end / start) ** (1 / years) - 1) * 100 if end > 0 else -100.0

        returns = equity.pct_change().dropna()
        vol = returns.std(ddof=1) * math.sqrt(annual_factor) * 100 if len(returns) > 1 else 0.0
        sharpe = (returns.mean() / returns.std(ddof=1)) * math.sqrt(annual_factor) if returns.std(ddof=1) else 0.0
        drawdown = equity / equity.cummax() - 1
        max_drawdown = float(drawdown.min() * 100)

        return {
            "total_return_pct": round(total_return, 2),
            "cagr_pct": round(cagr, 2),
            "ann_vol_pct": round(vol, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "trades": self.trades,
            "rebalances": self.rebalances,
            "end_equity": round(end, 2),
        }


def equal_weight_portfolio_backtest(
    prices: dict[str, pd.DataFrame],
    pick: "callable",
    cost: CostModel,
    initial_cash: float = 1_000_000.0,
    rebalance_days: int = 21,
    top_n: int = 10,
    label: str = "portfolio",
) -> PortfolioResult:
    """Hold `top_n` equal-weight positions chosen by `pick`, rebalanced on a schedule.

    `pick(history) -> list[(symbol, weight)]` is called on each rebalance date with
    bars up to and including that date only, so a cross-sectional rule cannot see
    the future either.
    """
    usable = {s: df for s, df in prices.items() if len(df) > 30}
    if not usable:
        raise ValueError("no usable price series")

    calendar = sorted(set().union(*(set(df.index) for df in usable.values())))
    calendar = pd.DatetimeIndex(calendar)

    cash = float(initial_cash)
    holdings: dict[str, Position] = {}
    equity_rows: list[float] = []
    trades = 0
    rebalances = 0
    target_log: list[dict] = []

    def last_close(symbol: str, when: pd.Timestamp) -> float | None:
        frame = usable[symbol]
        row = frame.loc[frame.index <= when]
        return float(row["close"].iloc[-1]) if len(row) else None

    def mark_value(when: pd.Timestamp) -> float:
        total = cash
        for symbol, position in holdings.items():
            price = last_close(symbol, when)
            if price is None:
                price = position.price
            total += position.qty * price
        return total

    for index, when in enumerate(calendar):
        due = index % rebalance_days == 0

        if due:
            # Signal uses only bars up to and including `when`.
            history = {
                symbol: df[df.index <= when].copy() for symbol, df in usable.items()
            }
            picks = pick(history) or []
            wanted = {symbol for symbol, _ in picks[:top_n]}

            target_value = mark_value(when) / max(len(wanted), 1) if wanted else 0.0

            for symbol in list(holdings):
                if symbol not in wanted:
                    price = last_close(symbol, when) or holdings[symbol].price
                    sell_price = cost.fill_price(price, "SELL")
                    qty = holdings.pop(symbol).qty
                    notional = qty * sell_price
                    cash += notional - cost.charges("SELL", notional)
                    trades += 1

            for symbol in wanted:
                price = last_close(symbol, when)
                if price is None or price <= 0:
                    continue
                existing = holdings.get(symbol)
                current_qty = existing.qty if existing else 0.0
                buy_price = cost.fill_price(price, "BUY")
                target_qty = target_value / buy_price

                # Keep the delta signed: the position update below needs the sign
                # to survive. Negating it for the sell path and reusing it here
                # makes every sale add shares instead of removing them.
                delta = target_qty - current_qty
                if abs(delta) * buy_price < 100.0:
                    continue

                if delta > 0:
                    delta = min(delta, max(cash / (buy_price * 1.03), 0.0))
                    if delta * buy_price < 100.0:
                        continue
                    notional = delta * buy_price
                    fees = cost.charges("BUY", notional)
                    if notional + fees > cash:
                        continue
                    cash -= notional + fees
                    fill_price = buy_price
                else:
                    trade_qty = -delta
                    sell_price = cost.fill_price(price, "SELL")
                    notional = trade_qty * sell_price
                    cash += notional - cost.charges("SELL", notional)
                    fill_price = sell_price

                held_qty = current_qty + delta
                if existing is not None:
                    existing.qty = held_qty
                    existing.price = fill_price
                else:
                    holdings[symbol] = Position(symbol, held_qty, fill_price, when)
                trades += 1

            rebalances += 1
            target_log.append({"date": when, "holdings": sorted(holdings)})

        equity_rows.append(mark_value(when))

    equity = pd.Series(equity_rows, index=calendar, name="equity")
    return PortfolioResult(
        equity=equity,
        trades=trades,
        rebalances=rebalances,
        initial_cash=initial_cash,
        target_log=target_log,
    )


def benchmark_equity(prices: dict[str, pd.DataFrame], initial_cash: float = 1_000_000.0) -> pd.Series:
    """Equal-weight buy-and-hold across the same universe, for a fair comparison."""
    usable = {s: df for s, df in prices.items() if len(df) > 30}
    if not usable:
        return pd.Series(dtype=float)

    calendar = pd.DatetimeIndex(sorted(set().union(*(set(df.index) for df in usable.values()))))
    per_symbol = initial_cash / len(usable)
    ratios = []
    for symbol, frame in usable.items():
        series = frame["close"].reindex(calendar).ffill()
        base = series.dropna()
        if base.empty:
            ratios.append(pd.Series(1.0, index=calendar))
            continue
        ratios.append(series / base.iloc[0] * per_symbol)
    stacked = pd.concat(ratios, axis=1).sum(axis=1)
    stacked.index.name = "equity"
    return stacked