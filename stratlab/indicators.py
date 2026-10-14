"""Technical indicators, all causal: the value at bar t uses only bars <= t."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window=window, min_periods=window).mean()


def ema(close: pd.Series, window: int) -> pd.Series:
    return close.ewm(span=window, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI, seeded with the original simple mean then smoothed recursively.

    Written as an explicit loop because pandas' ewm seeds from the first value,
    not from the mean of the first `period` values, which shifts every bar.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0).to_numpy(dtype=float)
    loss = (-delta).clip(lower=0.0).to_numpy(dtype=float)
    n = len(close)
    out = np.full(n, np.nan)

    if n < period + 1:
        return pd.Series(out, index=close.index, name="rsi")

    seed = slice(1, period + 1)
    if np.isnan(gain[seed]).any() or np.isnan(loss[seed]).any():
        return pd.Series(out, index=close.index, name="rsi")

    avg_gain = float(gain[seed].mean())
    avg_loss = float(loss[seed].mean())

    def score(ag: float, al: float) -> float:
        if al == 0 and ag == 0:
            return 50.0
        if al == 0:
            return 100.0
        if ag == 0:
            return 0.0
        return 100.0 - (100.0 / (1.0 + ag / al))

    out[period] = score(avg_gain, avg_loss)
    for i in range(period + 1, n):
        if np.isnan(gain[i]) or np.isnan(loss[i]):
            continue
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        out[i] = score(avg_gain, avg_loss)

    return pd.Series(out, index=close.index, name="rsi")


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return true_range(high, low, close).ewm(alpha=1.0 / period, adjust=False).mean()