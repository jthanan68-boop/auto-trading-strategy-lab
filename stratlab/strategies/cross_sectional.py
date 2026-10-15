"""Cross-sectional rules: rank instruments against each other, not against time.

The diagnosis showed the single-instrument rules fail in rising markets because
they step aside at every dip. A cross-sectional momentum rule fixes that
structurally: it is always invested, and it ranks names against each other so
one name's direction cannot decide the outcome.

Momentum is measured over a lookback and the most recent `skip` bars are
excluded. Skipping is not decoration: short-horizon momentum in equities is
dominated by a one-month reversal, so including it flips the sign of the signal.
"""

from __future__ import annotations

import pandas as pd


def _momentum(close: pd.Series, lookback: int, skip: int) -> float | None:
    if len(close) < lookback + skip + 1:
        return None
    start = close.iloc[-(lookback + skip + 1)]
    end = close.iloc[-(skip + 1)]
    if start <= 0:
        return None
    return float(end / start - 1)


def cross_sectional_momentum(
    lookback: int = 126,
    skip: int = 21,
    top_n: int = 10,
    name: str | None = None,
):
    """Return a pick function suitable for portfolio.equal_weight_portfolio_backtest."""

    def pick(history: dict[str, pd.DataFrame]) -> list[tuple[str, float]]:
        scored: list[tuple[str, float]] = []
        for symbol, frame in history.items():
            if len(frame) < lookback + skip + 1:
                continue
            score = _momentum(frame["close"], lookback, skip)
            if score is not None:
                scored.append((symbol, score))
        if not scored:
            return []
        scored.sort(key=lambda item: item[1], reverse=True)
        chosen = scored[:top_n]
        weight = 1.0 / len(chosen)
        return [(symbol, weight) for symbol, _ in chosen]

    pick.name = name or f"xs-mom-{lookback}-{skip}-top{top_n}"
    return pick


def cross_sectional_low_vol(
    lookback: int = 126,
    top_n: int = 10,
    name: str | None = None,
):
    """Hold the calmest names. Aimed at the drawdown problem, not at upside."""

    def pick(history: dict[str, pd.DataFrame]) -> list[tuple[str, float]]:
        scored: list[tuple[str, float]] = []
        for symbol, frame in history.items():
            if len(frame) < lookback + 1:
                continue
            returns = frame["close"].pct_change().dropna()
            if len(returns) < lookback // 2:
                continue
            scored.append((symbol, float(returns.std())))
        if not scored:
            return []
        scored.sort(key=lambda item: item[1])
        chosen = scored[:top_n]
        weight = 1.0 / len(chosen)
        return [(symbol, weight) for symbol, _ in chosen]

    pick.name = name or f"xs-lowvol-{lookback}-top{top_n}"
    return pick