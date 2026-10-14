"""Phase 3 analysis logic: regimes, run summary, and the not-yet-conclusive gate."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stratbot.analysis import (
    Divergence,
    classify,
    compare_to_backtest,
    regime_coverage,
    summarise_run,
    verdict,
)
from stratbot.db import Database
from stratbot.models import Side, utcnow


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "analysis.sqlite3")


def test_a_steady_uptrend_classifies_as_up():
    prices = pd.Series(np.linspace(100, 200, 60))
    assert classify(prices).trend == "up"


def test_a_steady_downtrend_classifies_as_down():
    prices = pd.Series(np.linspace(200, 100, 60))
    assert classify(prices).trend == "down"


def test_a_choppy_flat_market_classifies_as_a_range():
    # Rises and falls repeatedly, ending near where it started.
    prices = pd.Series([100 + (10 if i % 2 else -10) for i in range(60)], dtype=float)
    assert classify(prices).trend == "range"


def test_short_series_is_not_confidently_classified():
    assert classify(pd.Series([100.0, 101.0])).label == "insufficient-data"


def test_regime_coverage_needs_history():
    assert regime_coverage(pd.Series(dtype=float), []) == {"insufficient-data": 0}


def test_regime_coverage_returns_buckets():
    prices = pd.Series(np.linspace(100, 250, 300))
    coverage = regime_coverage(prices, [])
    assert coverage
    assert all(isinstance(key, str) for key in coverage)


def test_divergence_flags_large_gaps_only():
    small = Divergence("BTC", "total_return_pct", 10.0, 12.0)
    large = Divergence("BTC", "total_return_pct", 10.0, 40.0)
    assert not small.material
    assert large.material


def test_compare_splits_material_from_noise():
    live = {"strategy": "donchian-55/20", "total_return_pct": 40.0, "trades": 5, "max_drawdown_pct": 20.0}
    rows = [
        # return 10 -> 40 is +300%, drawdown 12 -> 20 is +67%: both worth chasing.
        # trades 5 -> 5 is unchanged: noise.
        {"symbol": "BTC", "strategy": "donchian-55/20", "total_return_pct": 10.0,
         "trades": 5, "max_drawdown_pct": 12.0},
        # a different strategy must be ignored entirely
        {"symbol": "BTC", "strategy": "other", "total_return_pct": 999.0,
         "trades": 1, "max_drawdown_pct": 1.0},
    ]
    material, _ = compare_to_backtest(live, rows)

    assert {d.field for d in material} == {"total_return_pct", "max_drawdown_pct"}
    assert all(d.symbol == "BTC" for d in material)


def test_short_run_is_never_conclusive():
    text, reasons = verdict({"trades": 2, "total_return_pct": 50.0, "_days": 1.0}, {"up-low": 4}, 10.0)
    assert text == "NOT YET CONCLUSIVE"
    assert any("days" in r for r in reasons)
    assert any("trades" in r for r in reasons)


def test_long_run_with_enough_data_can_be_verdict_beating():
    run = {"trades": 30, "total_return_pct": 25.0, "_days": 40.0}
    text, _ = verdict(run, {"up-low": 4, "down-low": 3, "range-high": 2}, 10.0)
    assert text == "BEATING BUY-AND-HOLD"


def test_long_run_that_loses_to_benchmark_says_so():
    run = {"trades": 30, "total_return_pct": -8.0, "_days": 40.0}
    text, _ = verdict(run, {"up-low": 4, "down-low": 3, "range-high": 2}, 10.0)
    assert text == "NOT BEATING BUY-AND-HOLD"


def test_few_regimes_blocks_a_verdict():
    run = {"trades": 30, "total_return_pct": 25.0, "_days": 40.0}
    text, reasons = verdict(run, {"up-low": 40}, 10.0)
    assert text == "NOT YET CONCLUSIVE"
    assert any("regime" in r for r in reasons)


def test_missing_benchmark_is_inconclusive_not_a_pass():
    run = {"trades": 30, "total_return_pct": 25.0, "_days": 40.0}
    text, reasons = verdict(run, {"a": 1, "b": 1, "c": 1}, None)
    assert text == "INCONCLUSIVE"
    assert any("buy-and-hold" in r for r in reasons)


def test_summary_of_an_empty_run_is_not_a_crash(db):
    run = summarise_run(db, "test")
    assert run["trades"] == 0
    assert run["win_rate_pct"] == 0.0
    assert run["total_return_pct"] == 0.0


def test_summary_counts_wins_and_fees(db):
    from stratbot.models import utcnow as now

    for i, pnl in enumerate((100.0, -40.0, 60.0)):
        db.record_closed_trade("BTC", now(), now(), 100.0, 100.0 + pnl / 10, 1.0, pnl, 1.0)

    run = summarise_run(db, "test")
    assert run["trades"] == 3
    assert run["win_rate_pct"] == pytest.approx(200 / 3, rel=1e-3)
    assert run["total_fees"] == pytest.approx(3.0)
    assert run["profit_factor"] == pytest.approx(160.0 / 40.0, rel=1e-3)