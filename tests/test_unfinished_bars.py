"""Unfinished candles, bounded retries, cache-write safety, symbol selection.

Everything here is deterministic: time is injected, the network is mocked, and
no broker is contacted.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from stratlab import data as price_data
from stratlab.calendar import (
    NSE_CLOSE_UTC_HOUR,
    drop_unfinished,
    session_closed,
    session_end,
)
from stratlab.provenance import (
    RefreshOutcome,
    RefreshTrigger,
    atomic_write_csv,
    atomic_write_text,
    read_sidecar,
)

EQUITY = price_data.BY_SYMBOL["RELIANCE"]
CRYPTO = price_data.BY_SYMBOL["BTC"]


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    target = tmp_path / "cache"
    target.mkdir()
    monkeypatch.setattr(price_data, "CACHE_DIR", target)
    return target


def series_ending(last: str, rows: int = 60) -> pd.DataFrame:
    end = pd.Timestamp(last)
    index = pd.date_range(end=end, periods=rows, freq="B")
    closes = [100.0 + i * 0.5 for i in range(rows)]
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0},
        index=index,
    )


def stub_fetch(monkeypatch, frame: pd.DataFrame | None):
    calls = {"n": 0}

    def fake(instrument, start, end, retries=3):
        calls["n"] += 1
        if frame is None:
            raise RuntimeError("simulated failure")
        return frame.copy()

    monkeypatch.setattr(price_data, "_fetch", fake)
    return calls


# =====================================================================
# 1. the forming candle is not a fact yet
# =====================================================================
class TestUnfinishedCandles:
    def test_equity_bar_is_open_until_1530_ist(self):
        bar_date = date(2026, 10, 16)
        # 09:45 IST == 04:15 UTC
        during = datetime(2026, 10, 16, 4, 15, tzinfo=timezone.utc)
        after = datetime(2026, 10, 16, 10, 1, tzinfo=timezone.utc)

        assert not session_closed(bar_date, "equity", during)
        assert session_closed(bar_date, "equity", after)

    def test_the_close_instant_is_ten_utc(self):
        bar_date = date(2026, 10, 16)
        assert session_end(bar_date, "equity") == datetime(
            2026, 10, 16, NSE_CLOSE_UTC_HOUR, tzinfo=timezone.utc)

    def test_crypto_bar_is_open_until_the_utc_day_rolls(self):
        bar_date = date(2026, 10, 16)
        assert not session_closed(bar_date, "crypto", datetime(2026, 10, 16, 4, 15, tzinfo=timezone.utc))
        assert session_closed(bar_date, "crypto", datetime(2026, 10, 17, 0, 1, tzinfo=timezone.utc))

    def test_a_closed_bar_is_kept(self):
        frame = series_ending("2026-10-15")
        now = datetime(2026, 10, 16, 10, 1, tzinfo=timezone.utc)
        assert len(drop_unfinished(frame, "equity", now)) == len(frame)

    def test_a_forming_bar_is_dropped(self):
        frame = series_ending("2026-10-16")
        now = datetime(2026, 10, 16, 4, 15, tzinfo=timezone.utc)
        trimmed = drop_unfinished(frame, "equity", now)

        assert len(trimmed) == len(frame) - 1
        assert trimmed.index[-1] == frame.index[-2], "the forming bar is still there"

    def test_the_feed_withholds_a_forming_bar_from_bars(self, cache_dir, monkeypatch):
        from stratbot.broker.feed import MarketDataFeed

        atomic_write_csv(cache_dir / "RELIANCE.csv", series_ending("2026-10-16"))
        monkeypatch.setattr(
            "stratlab.calendar.session_end",
            lambda bar_date, asset_class: datetime(2026, 10, 17, tzinfo=timezone.utc),
        )
        # "now" must fall before that end for the bar to count as forming.
        monkeypatch.setattr(
            "stratlab.calendar.session_closed",
            lambda bar_date, asset_class, now=None: False,
        )

        feed = MarketDataFeed(("RELIANCE",), refresh_interval_minutes=0)
        bars = feed.bars()

        # The forming bar IS served, because its open is a real traded price an
        # order decided on 15 Oct can execute against right now.
        assert "RELIANCE" in bars
        assert pd.Timestamp(bars["RELIANCE"].ts) == pd.Timestamp("2026-10-16")
        assert feed.deferred.get("RELIANCE") == "2026-10-16", (
            "a forming bar was served without saying so"
        )

    def test_the_feed_withholds_a_forming_bar_from_history(self, cache_dir, monkeypatch):
        from stratbot.broker.feed import MarketDataFeed

        atomic_write_csv(cache_dir / "RELIANCE.csv", series_ending("2026-10-16"))
        monkeypatch.setattr(
            "stratlab.calendar.session_closed",
            lambda bar_date, asset_class, now=None: False,
        )

        feed = MarketDataFeed(("RELIANCE",), refresh_interval_minutes=0)
        history = feed.history("RELIANCE")

        assert pd.Timestamp(history.index[-1]) == pd.Timestamp("2026-10-15"), (
            "the strategy still reads the forming candle"
        )

    def test_the_opt_out_serves_the_forming_bar(self, cache_dir, monkeypatch):
        from stratbot.broker.feed import MarketDataFeed

        atomic_write_csv(cache_dir / "RELIANCE.csv", series_ending("2026-10-16"))
        monkeypatch.setattr(
            "stratlab.calendar.session_closed",
            lambda bar_date, asset_class, now=None: False,
        )

        feed = MarketDataFeed(("RELIANCE",), refresh_interval_minutes=0,
                              include_unfinished=True)
        assert "RELIANCE" in feed.bars()

    def test_a_closed_bar_is_still_served(self, cache_dir, monkeypatch):
        from stratbot.broker.feed import MarketDataFeed

        atomic_write_csv(cache_dir / "RELIANCE.csv", series_ending("2026-10-15"))
        monkeypatch.setattr(
            "stratlab.calendar.session_closed",
            lambda bar_date, asset_class, now=None: True,
        )

        feed = MarketDataFeed(("RELIANCE",), refresh_interval_minutes=0)
        assert "RELIANCE" in feed.bars()
        assert feed.deferred == {}


# =====================================================================
# 2. a cache write failure degrades one symbol, not the universe
# =====================================================================
class TestCacheWriteSafety:
    def test_a_failed_write_keeps_the_previous_cache(self, cache_dir, monkeypatch):
        atomic_write_csv(cache_dir / "RELIANCE.csv", series_ending("2026-10-15"))
        before = (cache_dir / "RELIANCE.csv").read_text()
        stub_fetch(monkeypatch, series_ending("2026-10-16"))

        def boom(path, frame):
            raise OSError("disk full")

        monkeypatch.setattr(price_data, "atomic_write_csv", boom)

        out, prov = price_data.load_with_provenance(EQUITY, refresh=True)

        assert (cache_dir / "RELIANCE.csv").read_text() == before, "the cache was damaged"
        assert len(out) == 60
        assert prov.outcome == RefreshOutcome.REFRESH_FAILED_STALE_CACHE.value
        assert "cache write failed" in prov.detail

    def test_a_write_failure_does_not_raise(self, cache_dir, monkeypatch):
        atomic_write_csv(cache_dir / "RELIANCE.csv", series_ending("2026-10-15"))
        stub_fetch(monkeypatch, series_ending("2026-10-16"))
        monkeypatch.setattr(
            price_data, "atomic_write_csv",
            lambda path, frame: (_ for _ in ()).throw(OSError("disk full")),
        )

        # A write failure used to propagate and halt every symbol in the cycle.
        out, prov = price_data.load_with_provenance(EQUITY, refresh=True)
        assert not out.empty

    def test_a_failed_write_never_claims_a_fetch(self, cache_dir, monkeypatch):
        atomic_write_csv(cache_dir / "RELIANCE.csv", series_ending("2026-10-15"))
        stub_fetch(monkeypatch, series_ending("2026-10-16"))
        monkeypatch.setattr(
            price_data, "atomic_write_csv",
            lambda path, frame: (_ for _ in ()).throw(OSError("disk full")),
        )

        _, prov = price_data.load_with_provenance(EQUITY, refresh=True)

        assert prov.status != "fresh"
        assert prov.outcome != RefreshOutcome.REFRESHED.value

    def test_a_lost_sidecar_does_not_discard_the_cache(self, cache_dir, monkeypatch):
        atomic_write_csv(cache_dir / "RELIANCE.csv", series_ending("2026-10-15"))
        stub_fetch(monkeypatch, series_ending("2026-10-16"))

        def sidecar_boom(path, record):
            raise OSError("sidecar unwritable")

        monkeypatch.setattr(price_data, "write_sidecar", sidecar_boom)

        out, prov = price_data.load_with_provenance(EQUITY, refresh=True)

        assert len(out) == 61, "a good cache was thrown away over a sidecar"
        assert pd.Timestamp(out.index[-1]) == pd.Timestamp("2026-10-16")
        assert prov.outcome == RefreshOutcome.REFRESHED.value

    def test_provenance_is_derived_from_the_frame_not_the_sidecar(self, cache_dir, monkeypatch):
        """A stale sidecar must never make old data look freshly fetched."""
        atomic_write_csv(cache_dir / "RELIANCE.csv", series_ending("2026-10-15"))
        price_data.write_sidecar(
            cache_dir / "RELIANCE.csv",
            price_data.Sidecar("RELIANCE", "yfinance.download", "2026-10-16T09:00:00+00:00",
                               "2026-10-16", 999, "refreshed"),
        )
        calls = stub_fetch(monkeypatch, series_ending("2026-10-15"))

        _, prov = price_data.load_with_provenance(EQUITY, refresh=False,
                                                 max_cache_age_minutes=60,
                                                 today=date(2026, 10, 15))

        assert prov.last_candle == "2026-10-15", "provenance copied the sidecar"
        assert prov.rows == 60, "row count came from the sidecar, not the file"
        assert prov.fetched_at is None
        assert calls["n"] == 0

    def test_replace_is_retried_when_the_destination_is_locked(self, tmp_path, monkeypatch):
        target = tmp_path / "x.txt"
        atomic_write_text(target, "first")

        real_replace = os.replace
        attempts = {"n": 0}

        def flaky(src, dst):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise PermissionError(13, "file is being used by another process")
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", flaky)
        atomic_write_text(target, "second", retries=5)

        assert target.read_text() == "second"
        assert attempts["n"] == 3, "the lock was not retried"

    def test_a_write_gives_up_cleanly_and_keeps_the_old_file(self, tmp_path, monkeypatch):
        target = tmp_path / "x.txt"
        atomic_write_text(target, "original")

        monkeypatch.setattr(
            os, "replace",
            lambda src, dst: (_ for _ in ()).throw(PermissionError(13, "locked")),
        )
        with pytest.raises(PermissionError):
            atomic_write_text(target, "replacement", retries=2)

        assert target.read_text() == "original"
        assert list(tmp_path.glob("*.tmp")) == [], "a temp file was left behind"


# =====================================================================
# 3. a missing session cannot be retried forever
# =====================================================================
class TestBoundedCalendarRetries:
    def test_retry_budget_is_exhausted_then_falls_back(self, cache_dir, monkeypatch):
        from stratbot.broker.feed import MarketDataFeed

        # The provider keeps publishing the same old bar, so the calendar
        # expectation is never met. This is what an exchange holiday looks like.
        atomic_write_csv(cache_dir / "RELIANCE.csv", series_ending("2026-10-14"))
        stub_fetch(monkeypatch, series_ending("2026-10-14"))
        monkeypatch.setattr(
            "stratlab.calendar.expected_last_bar",
            lambda today, asset_class, now_hour_utc=NSE_CLOSE_UTC_HOUR: date(2026, 10, 16),
        )

        feed = MarketDataFeed(("RELIANCE",), refresh_interval_minutes=99999,
                              max_calendar_retries=3)

        # The first call has no prior reading to judge, then the budget is spent.
        for _ in range(4):
            feed.bars()
        assert feed._behind.get("RELIANCE", 0) >= 3, "the budget was never spent"

        fetch_calls = {"n": 0}
        real_fetch = price_data._fetch

        def counted(*args, **kwargs):
            fetch_calls["n"] += 1
            return real_fetch(*args, **kwargs)

        monkeypatch.setattr(price_data, "_fetch", counted)
        feed.bars()
        assert fetch_calls["n"] == 0, "kept fetching a session the provider never published"

    def test_the_budget_resets_when_the_provider_catches_up(self, cache_dir, monkeypatch):
        from stratbot.broker.feed import MarketDataFeed

        atomic_write_csv(cache_dir / "RELIANCE.csv", series_ending("2026-10-15"))
        stub_fetch(monkeypatch, series_ending("2026-10-15"))

        feed = MarketDataFeed(("RELIANCE",), refresh_interval_minutes=0)
        feed.bars()

        assert feed._behind.get("RELIANCE", 0) == 0


# =====================================================================
# 4. --symbols narrows the universe without changing the default
# =====================================================================
class TestSymbolSelection:
    def test_default_universe_is_unchanged(self):
        from stratbot.config import CONFIG

        assert CONFIG.symbols == ("BTC", "ETH", "RELIANCE", "TCS", "INFY")

    def test_a_subset_resolves(self):
        import run_bot

        assert run_bot._parse_symbols("RELIANCE") == ("RELIANCE",)
        assert run_bot._parse_symbols("reliance, infy") == ("RELIANCE", "INFY")

    def test_an_unknown_symbol_is_refused(self):
        import run_bot

        with pytest.raises(SystemExit) as caught:
            run_bot._parse_symbols("RELIANCE,TATAMOTORS")
        assert "TATAMOTORS" in str(caught.value)

    def test_blank_input_is_refused(self):
        import run_bot

        with pytest.raises(SystemExit):
            run_bot._parse_symbols(" , ")

    def test_a_single_symbol_engine_builds_and_cycles(self, tmp_path, monkeypatch):
        from stratbot.broker.paper import PaperBroker
        from stratbot.config import BotConfig
        from stratbot.db import Database
        from stratbot.engine import Engine
        from tests.stubs import StubFeed

        monkeypatch.setattr(price_data, "CACHE_DIR", tmp_path / "cache")
        (tmp_path / "cache").mkdir()
        atomic_write_csv(tmp_path / "cache" / "RELIANCE.csv", series_ending("2026-10-15"))

        db = Database(tmp_path / "one.sqlite3")
        config = BotConfig(symbols=("RELIANCE",), initial_cash=500_000.0)
        engine = Engine(config, db, broker=PaperBroker(500_000.0))
        engine.feed = StubFeed(lambda: {}, lambda: series_ending("2026-10-15"))

        engine.cycle()  # must not raise on a universe of one
        assert engine.pending_orders() == []