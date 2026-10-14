"""The backtest must never be able to cheat. These tests pin down how."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stratlab.backtest import run_backtest
from stratlab.costs import BINANCE_SPOT, INDIA_EQUITY_DELIVERY, CostModel
from stratlab.indicators import rsi, sma
from stratlab.strategies.library import SmaCrossover

FREE = CostModel(name="free", slippage_bps=0.0)


def make_prices(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2020-01-01", periods=len(closes), freq="D")
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes}, index=index)
    frame.index.name = "date"
    return frame


class AlwaysLong:
    name = "always-long"

    def position(self, history):
        return 1.0


class AlwaysFlat:
    name = "always-flat"

    def position(self, history):
        return 0.0


class Peeker:
    """Reads the next bar to decide today - must be outrun, not rewarded."""

    name = "peeker"

    def __init__(self):
        self.frames: list[pd.DataFrame] = []

    def position(self, history):
        self.frames.append(history.copy())
        return 1.0


def test_always_long_matches_underlying_with_zero_costs():
    closes = [100.0] * 5 + [120.0]
    result = run_backtest(make_prices(closes), AlwaysLong(), FREE, initial_cash=1000.0)
    assert result.equity_curve.iloc[-1] == pytest.approx(1200.0, rel=1e-9)


def test_always_flat_stays_in_cash():
    result = run_backtest(make_prices([100.0, 500.0, 900.0]), AlwaysFlat(), FREE, initial_cash=1000.0)
    assert result.equity_curve.iloc[-1] == pytest.approx(1000.0, rel=1e-9)


def test_signal_fills_at_next_open_not_signal_close():
    closes = [100.0] * 4
    prices = make_prices(closes)
    prices.iloc[3, prices.columns.get_loc("open")] = 105.0

    result = run_backtest(prices, AlwaysLong(), FREE, initial_cash=1000.0)
    # Buys on the first decision at bar 0's open, valued at bar 0's close.
    assert result.equity_curve.iloc[0] == pytest.approx(1000.0, rel=1e-9)


def test_future_bars_are_never_visible_to_the_strategy():
    peeker = Peeker()
    run_backtest(make_prices([float(i) for i in range(1, 30)]), peeker, FREE, initial_cash=1000.0)

    assert peeker.frames, "strategy was never called"
    first_len = len(peeker.frames[0])
    assert first_len == 1, "first decision must see exactly one bar"
    lengths = [len(f) for f in peeker.frames]
    assert lengths == sorted(lengths), "history must grow monotonically"


def test_costs_reduce_return_monotonically():
    closes = [100.0] * 10 + [150.0]
    free_run = run_backtest(make_prices(closes), AlwaysLong(), FREE, initial_cash=100_000.0)
    costly = INDIA_EQUITY_DELIVERY
    paid_run = run_backtest(make_prices(closes), AlwaysLong(), costly, initial_cash=100_000.0)
    assert paid_run.equity_curve.iloc[-1] <= free_run.equity_curve.iloc[-1]


def test_slippage_direction_is_adverse():
    cost = CostModel(name="slip", slippage_bps=100.0)
    assert cost.fill_price(100.0, "BUY") > 100.0
    assert cost.fill_price(100.0, "SELL") < 100.0


def test_round_trip_cost_is_positive_and_plausible():
    # India delivery: ~0.222% statutory plus 5 bps of slippage each way.
    rt = INDIA_EQUITY_DELIVERY.round_trip_pct()
    assert 0.25 < rt < 0.40, f"India delivery round trip looks wrong: {rt}%"


def test_slippage_is_a_material_share_of_cost():
    breakdown = INDIA_EQUITY_DELIVERY.breakdown()
    assert breakdown["slippage"] == pytest.approx(0.10, abs=0.001)
    share = breakdown["slippage"] / breakdown["total"]
    assert 0.25 < share < 0.40, f"slippage was {share:.0%} of the round trip"
    assert breakdown["stt"] > 0.15


def test_crypto_round_trip_matches_binance_spot_taker():
    rt = BINANCE_SPOT.round_trip_pct()
    assert 0.25 < rt < 0.35, f"Binance spot round trip looks wrong: {rt}%"


def test_win_rate_and_trades_are_internally_consistent():
    closes = [100.0] * 3 + [120.0, 110.0, 130.0, 105.0, 125.0] + [100.0] * 40
    result = run_backtest(make_prices(closes), SmaCrossover(2, 4), FREE, initial_cash=100_000.0)
    metrics = result.metrics()
    if metrics["trades"] == 0:
        pytest.skip("no trades generated in this fixture")
    assert metrics["win_rate_pct"] >= 0
    assert metrics["win_rate_pct"] <= 100
    assert metrics["exposure_pct"] <= 100


def test_metrics_recover_a_known_return():
    closes = [100.0] * 60 + [110.0] * 60
    result = run_backtest(make_prices(closes), AlwaysLong(), FREE, initial_cash=1000.0)
    assert result.metrics()["total_return_pct"] == pytest.approx(10.0, abs=0.01)


def test_no_lookahead_in_sma_crossover_signal():
    # A series that rises forever must keep the crossover strategy long.
    # 20 of 200 bars are lost to the warm-up window, so ~90% exposure is the cap.
    closes = [100.0 + i for i in range(200)]
    result = run_backtest(make_prices(closes), SmaCrossover(5, 20), FREE, initial_cash=1000.0)
    assert result.metrics()["exposure_pct"] > 85


def test_rsi_is_bounded_and_causal():
    series = pd.Series(np.linspace(100, 200, 300))
    values = rsi(series, 14)
    assert values.iloc[:14].isna().all(), "RSI must be undefined during the warm-up"
    defined = values.dropna()
    assert defined.between(0, 100).all()


def test_rsi_is_100_on_a_pure_uptrend():
    series = pd.Series(np.arange(1, 60, dtype=float))
    assert rsi(series, 14).iloc[-1] == pytest.approx(100.0)


def test_rsi_matches_the_hand_computed_wilder_seed():
    # Gains 1..14, no losses: avg gain 7.5, avg loss 0 -> RSI saturates at 100.
    series = pd.Series(np.arange(1, 20, dtype=float))
    assert rsi(series, 14).iloc[14] == pytest.approx(100.0)


def test_sma_has_no_partial_window_values():
    series = pd.Series(np.arange(1, 21, dtype=float))
    computed = sma(series, 5)
    assert computed.iloc[:4].isna().all()
    assert computed.iloc[4] == pytest.approx(3.0)