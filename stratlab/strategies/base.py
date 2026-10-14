"""Strategy contract.

A strategy maps a bar history to a target position weight. It must never look
forward: only `history` up to and including the current bar may be read.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd


class Strategy(Protocol):
    name: str
    params: dict

    def position(self, history: pd.DataFrame) -> float:
        """Target position weight in [0, 1] for the bar just closed."""
        ...


def crossed_above(fast: pd.Series, slow: pd.Series) -> pd.Series:
    return (fast > slow) & (fast.shift(1) <= slow.shift(1))


def crossed_below(fast: pd.Series, slow: pd.Series) -> pd.Series:
    return (fast < slow) & (fast.shift(1) >= slow.shift(1))