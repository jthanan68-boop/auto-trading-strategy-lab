"""Shared test doubles for injecting market data.

The engine reads prices through an injected MarketDataFeed rather than a
module-level function, so tests supply a stub at that seam instead of patching
a symbol in the engine module.
"""

from __future__ import annotations

import pandas as pd

from stratbot.broker.feed import MarketDataFeed


class StubFeed:
    """A feed that serves bars and history, optionally as a timeline.

    Accepts callables so a test can swap the bars mid-run, and accepts a list so
    it can walk a sequence of bars. The timeline matters because execution is
    next-open: an order queued on bar t only fills once a later bar appears, so a
    feed pinned to one timestamp can never produce a fill at all. `advance()`
    moves to the next entry and clamps at the last, so a test that does not drive
    the timeline still sees a stable final bar.
    """

    def __init__(self, bars, history=None, provenance: dict | None = None):
        self._bars = bars
        self._history = history
        self.provenance = provenance or {}
        self.refresh_calls = 0
        self.step = 0

    def _current(self, source):
        value = source[self.step] if isinstance(source, list) else source
        return value() if callable(value) else value

    def bars(self, force_refresh: bool = False) -> dict:
        if force_refresh:
            self.refresh_calls += 1
        return dict(self._current(self._bars))

    def history(self, symbol: str) -> pd.DataFrame:
        source = self._current(self._history)
        if source is None:
            raise KeyError(symbol)
        return source

    def advance(self):
        if isinstance(self._bars, list):
            self.step = min(self.step + 1, len(self._bars) - 1)
        return self.step

    def instruments(self) -> list:
        return []


def run_until_filled(engine, max_cycles: int = 4):
    """Cycle until the order queued on the previous bar has executed.

    A signal now takes one cycle to raise and the next to fill, so a test that
    means "let the bot act" needs both. Stops on the first new fill so an
    assertion of exactly one fill still holds.
    """
    before = len(engine.db.recent_fills(500))
    result = engine.cycle()
    for _ in range(max_cycles - 1):
        if len(engine.db.recent_fills(500)) > before:
            break
        if hasattr(engine.feed, "advance"):
            engine.feed.advance()
        result = engine.cycle()
    return result


class RaisingFeed:
    """A feed that always fails, to exercise the startup-with-no-data path."""

    def __init__(self, error: Exception | None = None):
        self.provenance: dict = {}
        self._error = error or RuntimeError("feed unavailable")

    def bars(self, force_refresh: bool = False) -> dict:
        raise self._error

    def history(self, symbol: str) -> pd.DataFrame:
        raise self._error


def feed_for(monkeypatch_free_bar_map, history: pd.DataFrame | None = None) -> StubFeed:
    return StubFeed(monkeypatch_free_bar_map, history)
