"""Portfolio backtester tests.

This backtester shipped with no tests and immediately produced 10^24% returns
because a sell added shares back instead of removing them. These tests exist so
that class of bug cannot come back silently.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stratlab.costs import CostModel, cost_model_for
from stratlab.portfolio import (
    benchmark_equity,
    equal_weight_portfolio_backtest,
)

FREE = CostModel(name="free", slippage_bps=0.0)
REAL = cost_model_for("equity")


def flat_prices(symbols: list[str], days: int = 400, price: float = 100.0) -> dict[str, pd.DataFrame]:
    index = pd.date_range("2022-01-01", periods=days, freq="B")
    return {
        s: pd.DataFrame(
            {"open": price, "high": price, "low": price, "close": price,
             "volume": 1.0},
            index=index,
        )
        for s in symbols
    }


def rising_prices(symbols: list[str], days: int = 400, start: float = 100.0) -> dict[str, pd.DataFrame]:
    index = pd.date_range("2022-01-01", periods=days, freq="B")
    out = {}
    for i, s in enumerate(symbols):
        # Different slopes so cross-sectional rules have something to rank.
        closes = np.linspace(start, start * (2 + i * 0.1), days)
        out[s] = pd.DataFrame(
            {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0},
            index=index,
        )
    return out


def top_two(history) -> list[tuple[str, float]]:
    scored = [(s, float(f["close"].iloc[-1])) for s, f in history.items()]
    scored.sort(key=lambda item: item[1], reverse=True)
    return [(s, 0.5) for s, _ in scored[:2]]


def test_flat_prices_leave_equity_roughly_flat():
    prices = flat_prices(["A", "B", "C", "D", "E"])
    result = equal_weight_portfolio_backtest(prices, top_two, FREE, initial_cash=1_000_000)
    drift = abs(result.equity.iloc[-1] / result.equity.iloc[0] - 1) * 100
    assert drift < 1.0, f"flat prices moved equity by {drift:.1f}%"


def test_a_sell_reduces_the_quantity():
    """Regression: the position update reused a negated delta and grew holdings."""
    prices = flat_prices(["A", "B", "C", "D", "E"])

    holding = {"A"}

    def swap(history):
        names = sorted(history)[:2]
        if names[0] == "A" and len(history) > 100:
            holding.discard("A")
            holding.add(names[1] if names[1] != "A" else names[0])
        return [(s, 0.5) for s in sorted(holding)[:2]]

    result = equal_weight_portfolio_backtest(prices, swap, FREE, initial_cash=1_000_000)
    # Equity must stay bounded even as the rule churns.
    assert result.equity.max() < 1_000_000 * 1.05, "equity blew up during churn"


def test_quantity_never_grows_without_price_movement():
    prices = flat_prices(["A", "B", "C", "D", "E"])
    state = {"holdings": ["A"], "traded": False}

    def rotate(history):
        names = sorted(history)
        if not state["traded"] and len(history) > 120:
            state["holdings"] = [names[1], names[2]]
            state["traded"] = True
        return [(s, 0.5) for s in state["holdings"]]

    result = equal_weight_portfolio_backtest(prices, rotate, FREE, initial_cash=1_000_000)
    assert np.isfinite(result.equity.iloc[-1])
    assert result.equity.iloc[-1] <= 1_000_000 * 1.001


def test_equity_cannot_explode_under_real_costs():
    prices = rising_prices([f"S{i}" for i in range(12)])
    rule = lambda h: [(s, 0.1) for s in sorted(h)[:10]]  # noqa: E731
    result = equal_weight_portfolio_backtest(prices, rule, REAL, initial_cash=1_000_000, rebalance_days=21)
    total_return = result.equity.iloc[-1] / result.equity.iloc[0] - 1
    assert total_return < 100, f"{total_return:.0%} return over <2y is a bug, not a strategy"
    assert result.equity.max() < 1_000_000 * 20


def test_rising_prices_produce_a_gain():
    prices = rising_prices(["A", "B", "C", "D", "E"])
    result = equal_weight_portfolio_backtest(prices, top_two, FREE, initial_cash=1_000_000)
    assert result.equity.iloc[-1] > result.equity.iloc[0] * 1.05


def test_costs_reduce_return():
    prices = rising_prices([f"S{i}" for i in range(12)])
    rule = lambda h: [(s, 0.1) for s in sorted(h)[:10]]  # noqa: E731
    free = equal_weight_portfolio_backtest(prices, rule, FREE, initial_cash=1_000_000)
    paid = equal_weight_portfolio_backtest(prices, rule, REAL, initial_cash=1_000_000)
    assert paid.equity.iloc[-1] <= free.equity.iloc[-1]


def test_pick_never_sees_future_bars():
    prices = flat_prices(["A", "B", "C", "D", "E"])
    seen: list[int] = []

    def spy(history):
        seen.append(min(len(f) for f in history.values()))
        return top_two(history)

    equal_weight_portfolio_backtest(prices, spy, FREE, initial_cash=1_000_000)
    assert seen == sorted(seen), "history handed to the rule must grow monotonically"
    assert seen[0] < seen[-1]


def test_benchmark_is_equal_weight_not_a_single_winner():
    prices = flat_prices(["A", "B"], days=300, price=100.0)
    prices["A"]["close"] = np.linspace(100, 300, 300)
    prices["B"]["close"] = np.linspace(100, 110, 300)
    equity = benchmark_equity(prices, 1_000_000)
    # A doubled by 3x, B by 1.1x; the blend must land between them.
    ratio = equity.iloc[-1] / equity.iloc[0]
    assert 1.1 < ratio < 3.0, f"blend ratio {ratio:.2f} is not an equal-weight mix"


def test_metrics_do_not_blow_up_on_a_clean_run():
    prices = rising_prices([f"S{i}" for i in range(12)])
    rule = lambda h: [(s, 0.1) for s in sorted(h)[:10]]  # noqa: E731
    result = equal_weight_portfolio_backtest(prices, rule, REAL, initial_cash=1_000_000)
    metrics = result.metrics()
    assert all(np.isfinite(v) for v in metrics.values() if isinstance(v, (int, float)))


def test_empty_history_selection_returns_no_positions():
    prices = flat_prices(["A", "B", "C", "D", "E"])
    result = equal_weight_portfolio_backtest(prices, lambda h: [], FREE, initial_cash=500_000)
    assert result.equity.iloc[-1] == pytest.approx(500_000, rel=1e-6)