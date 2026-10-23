"""Market-data refresh lifecycle: cache hit, explicit refresh, failure, atomicity.

All network access is mocked. No broker is contacted and no order is placed.
Every test writes to a tmp_path cache so the real data/cache is untouched.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stratlab import data as price_data
from stratlab.calendar import expected_last_bar, is_stale
from stratlab.provenance import (
    MIN_HISTORY_ROWS,
    CacheStatus,
    RefreshOutcome,
    RefreshTrigger,
    ValidationError,
    atomic_write_csv,
    atomic_write_text,
    read_sidecar,
    validate_frame,
    write_sidecar,
)

INSTRUMENT = price_data.BY_SYMBOL["RELIANCE"]


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Redirect the cache to an isolated directory."""
    target = tmp_path / "cache"
    target.mkdir()
    monkeypatch.setattr(price_data, "CACHE_DIR", target)
    return target


def make_frame(rows: int = 60, start: str = "2024-01-01", end: str | None = None) -> pd.DataFrame:
    index = pd.date_range(start=start, periods=rows, freq="B")
    closes = np.linspace(100.0, 160.0, rows)
    return pd.DataFrame(
        {"open": closes, "high": closes * 1.01, "low": closes * 0.99,
         "close": closes, "volume": 1000.0},
        index=index,
    )


def stub_fetch(monkeypatch, frame: pd.DataFrame | None):
    """Replace the Yahoo call. None simulates a total download failure."""
    calls = {"n": 0}

    def fake_fetch(instrument, start, end, retries=3):
        calls["n"] += 1
        if frame is None:
            raise RuntimeError("simulated network failure")
        return frame.copy()

    monkeypatch.setattr(price_data, "_fetch", fake_fetch)
    return calls


# =====================================================================
# validation before replacement
# =====================================================================
class TestValidation:
    def test_valid_frame_passes(self):
        out = validate_frame(make_frame(), ["open", "high", "low", "close"], MIN_HISTORY_ROWS)
        assert len(out) == 60

    def test_empty_is_rejected(self):
        with pytest.raises(ValidationError, match="empty"):
            validate_frame(pd.DataFrame(), ["open", "high", "low", "close"], MIN_HISTORY_ROWS)

    def test_too_few_rows_is_rejected(self):
        with pytest.raises(ValidationError, match="rows"):
            validate_frame(make_frame(5), ["open", "high", "low", "close"], MIN_HISTORY_ROWS)

    def test_missing_column_is_rejected(self):
        frame = make_frame().drop(columns=["high"])
        with pytest.raises(ValidationError, match="missing columns"):
            validate_frame(frame, ["open", "high", "low", "close"], MIN_HISTORY_ROWS)

    def test_duplicate_dates_are_rejected(self):
        frame = make_frame()
        doubled = pd.concat([frame, frame.iloc[[-1]]]).sort_index()
        with pytest.raises(ValidationError, match="duplicate"):
            validate_frame(doubled, ["open", "high", "low", "close"], MIN_HISTORY_ROWS)

    def test_unsorted_index_is_rejected(self):
        frame = make_frame().iloc[::-1]
        with pytest.raises(ValidationError, match="not sorted"):
            validate_frame(frame, ["open", "high", "low", "close"], MIN_HISTORY_ROWS)

    def test_timezone_aware_index_is_rejected(self):
        frame = make_frame()
        frame.index = frame.index.tz_localize("UTC")
        with pytest.raises(ValidationError, match="timezone"):
            validate_frame(frame, ["open", "high", "low", "close"], MIN_HISTORY_ROWS)

    def test_nan_price_is_rejected(self):
        frame = make_frame()
        frame.iloc[5, frame.columns.get_loc("close")] = np.nan
        with pytest.raises(ValidationError, match="NaN"):
            validate_frame(frame, ["open", "high", "low", "close"], MIN_HISTORY_ROWS)

    def test_negative_price_is_rejected(self):
        frame = make_frame()
        frame.iloc[5, frame.columns.get_loc("close")] = -1.0
        with pytest.raises(ValidationError, match="non-positive"):
            validate_frame(frame, ["open", "high", "low", "close"], MIN_HISTORY_ROWS)

    def test_high_below_low_is_rejected(self):
        frame = make_frame()
        frame.iloc[5, frame.columns.get_loc("high")] = 1.0
        with pytest.raises(ValidationError, match="high below low"):
            validate_frame(frame, ["open", "high", "low", "close"], MIN_HISTORY_ROWS)


# =====================================================================
# atomic writes
# =====================================================================
class TestAtomicWrite:
    def test_replaces_the_file(self, tmp_path):
        target = tmp_path / "x.csv"
        atomic_write_csv(target, make_frame(40))
        assert target.exists()
        assert len(pd.read_csv(target)) == 40

    def test_no_temp_file_is_left_behind(self, tmp_path):
        atomic_write_csv(tmp_path / "x.csv", make_frame(40))
        assert list(tmp_path.glob("*.tmp")) == []

    def test_interrupted_write_keeps_the_previous_file(self, tmp_path, monkeypatch):
        target = tmp_path / "x.csv"
        atomic_write_csv(target, make_frame(40))
        original = target.read_text()

        def boom(src, dst):
            raise OSError("simulated interrupt")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write_csv(target, make_frame(50))

        assert target.read_text() == original, "a failed write corrupted a usable cache"
        assert list(tmp_path.glob("*.tmp")) == [], "temp file was not cleaned up"

    def test_atomic_text_write(self, tmp_path):
        target = tmp_path / "note.txt"
        atomic_write_text(target, "hello")
        assert target.read_text() == "hello"


# =====================================================================
# cache hit vs explicit refresh
# =====================================================================
class TestCacheSemantics:
    def test_cache_hit_does_not_download(self, cache_dir, monkeypatch):
        frame = make_frame()
        atomic_write_csv(cache_dir / "RELIANCE.csv", frame)
        calls = stub_fetch(monkeypatch, frame)

        out, prov = price_data.load_with_provenance(INSTRUMENT, refresh=False)

        assert calls["n"] == 0, "a cache hit must not hit the network"
        assert prov.outcome == RefreshOutcome.CACHE_HIT.value
        assert prov.fetched_at is None, "a cache hit must never claim a fetch time"
        assert len(out) > 0

    def test_explicit_refresh_downloads(self, cache_dir, monkeypatch):
        frame = make_frame()
        atomic_write_csv(cache_dir / "RELIANCE.csv", frame)
        calls = stub_fetch(monkeypatch, frame)

        _, prov = price_data.load_with_provenance(INSTRUMENT, refresh=True)

        assert calls["n"] == 1
        assert prov.outcome == RefreshOutcome.REFRESHED.value
        assert prov.fetched_at is not None, "a real fetch must record when it happened"

    def test_refresh_interval_suppresses_redundant_downloads(self, cache_dir, monkeypatch):
        frame = make_frame()
        atomic_write_csv(cache_dir / "RELIANCE.csv", frame)
        calls = stub_fetch(monkeypatch, frame)

        for _ in range(5):
            price_data.load_with_provenance(INSTRUMENT, max_cache_age_minutes=60)
        assert calls["n"] == 0, "a fresh cache must not be re-downloaded every cycle"

    def test_interval_zero_forces_each_call(self, cache_dir, monkeypatch):
        frame = make_frame()
        atomic_write_csv(cache_dir / "RELIANCE.csv", frame)
        calls = stub_fetch(monkeypatch, frame)

        for _ in range(3):
            price_data.load_with_provenance(INSTRUMENT, max_cache_age_minutes=0)
        assert calls["n"] == 3

    def test_no_cache_and_no_refresh_is_absent(self, cache_dir, monkeypatch):
        stub_fetch(monkeypatch, make_frame())
        out, prov = price_data.load_with_provenance(INSTRUMENT, refresh=False)
        assert out.empty
        assert prov.status == CacheStatus.ABSENT.value
        assert prov.outcome == RefreshOutcome.REFRESH_FAILED_NO_CACHE.value

    def test_legacy_load_still_works(self, cache_dir, monkeypatch):
        frame = make_frame()
        stub_fetch(monkeypatch, frame)
        assert len(price_data.load(INSTRUMENT, refresh=True)) > 0


# =====================================================================
# failure handling
# =====================================================================
class TestFailureHandling:
    def test_failed_download_keeps_a_usable_cache(self, cache_dir, monkeypatch):
        frame = make_frame()
        atomic_write_csv(cache_dir / "RELIANCE.csv", frame)
        stub_fetch(monkeypatch, None)

        out, prov = price_data.load_with_provenance(INSTRUMENT, refresh=True)

        assert len(out) == 60, "a failed download destroyed a good cache"
        assert prov.outcome == RefreshOutcome.REFRESH_FAILED_STALE_CACHE.value
        assert "download failed" in prov.detail

    def test_failed_download_does_not_claim_to_be_fresh(self, cache_dir, monkeypatch):
        atomic_write_csv(cache_dir / "RELIANCE.csv", make_frame())
        stub_fetch(monkeypatch, None)

        _, prov = price_data.load_with_provenance(INSTRUMENT, refresh=True)

        assert prov.status != CacheStatus.FRESH.value
        assert prov.status == CacheStatus.STALE.value

    def test_failed_download_with_no_cache_returns_empty(self, cache_dir, monkeypatch):
        stub_fetch(monkeypatch, None)
        out, prov = price_data.load_with_provenance(INSTRUMENT, refresh=True)
        assert out.empty
        assert prov.outcome == RefreshOutcome.REFRESH_FAILED_NO_CACHE.value

    def test_malformed_response_is_rejected_and_cache_kept(self, cache_dir, monkeypatch):
        frame = make_frame()
        atomic_write_csv(cache_dir / "RELIANCE.csv", frame)
        stub_fetch(monkeypatch, frame.iloc[:5])  # too few rows

        out, prov = price_data.load_with_provenance(INSTRUMENT, refresh=True)

        assert len(out) == 60
        assert prov.outcome == RefreshOutcome.REJECTED_INVALID.value
        assert "rejected" in prov.detail

    def test_cache_with_missing_columns_is_replaced(self, cache_dir, monkeypatch):
        good = make_frame()
        bad = good.drop(columns=["high"])
        atomic_write_csv(cache_dir / "RELIANCE.csv", bad)
        stub_fetch(monkeypatch, good)

        out, prov = price_data.load_with_provenance(INSTRUMENT, refresh=True)

        assert prov.outcome == RefreshOutcome.REFRESHED.value
        assert "high" in out.columns

    def test_corrupt_cache_file_is_survived(self, cache_dir, monkeypatch):
        (cache_dir / "RELIANCE.csv").write_text("not,a,valid,csv\n1,2,3,4\n", encoding="utf-8")
        stub_fetch(monkeypatch, make_frame())

        out, prov = price_data.load_with_provenance(INSTRUMENT, refresh=False)
        assert prov.outcome == RefreshOutcome.REFRESH_FAILED_NO_CACHE.value
        assert out.empty


# =====================================================================
# provenance sidecar
# =====================================================================
class TestSidecar:
    def test_refresh_writes_metadata(self, cache_dir, monkeypatch):
        stub_fetch(monkeypatch, make_frame())
        price_data.load_with_provenance(INSTRUMENT, refresh=True)

        record = read_sidecar(cache_dir / "RELIANCE.csv")
        assert record is not None
        assert record["provider"] == "yfinance.download"
        assert record["fetched_at"]
        assert record["last_candle"]
        assert record["outcome"] == RefreshOutcome.REFRESHED.value

    def test_metadata_records_the_latest_candle(self, cache_dir, monkeypatch):
        frame = make_frame()
        stub_fetch(monkeypatch, frame)
        _, prov = price_data.load_with_provenance(INSTRUMENT, refresh=True)
        record = read_sidecar(cache_dir / "RELIANCE.csv")
        assert record["last_candle"] == str(frame.index[-1].date()) == prov.last_candle

    def test_log_line_states_the_source(self):
        from stratlab.provenance import build_provenance

        prov = build_provenance("RELIANCE", Path("x.csv"), make_frame(),
                                RefreshOutcome.CACHE_HIT, "equity", None)
        line = prov.log_line()
        assert "yfinance" in line
        assert "cache_hit" in line
        assert "fetched_at=never" in line, "a cache hit must not imply a fetch"

    def test_log_line_names_the_refresh_trigger(self):
        from stratlab.provenance import build_provenance

        prov = build_provenance("RELIANCE", Path("x.csv"), make_frame(),
                                RefreshOutcome.REFRESHED, "equity",
                                datetime.now(timezone.utc),
                                trigger=RefreshTrigger.BEHIND_CALENDAR.value)
        assert "trigger=behind_calendar" in prov.log_line()

    def test_sidecar_keeps_recent_history(self, tmp_path):
        from stratlab.provenance import Sidecar

        path = tmp_path / "x.csv"
        path.touch()
        write_sidecar(path, Sidecar("X", "p", "t", "d", 1, "cache_hit", history=[]))
        for i in range(40):
            write_sidecar(path, Sidecar("X", "p", f"t{i}", "d", 1, "cache_hit",
                                        history=json.loads(
                                            (path.with_suffix(".meta.json")).read_text()
                                        )["history"] + [{"i": i}]))
        record = read_sidecar(path)
        assert len(record["history"]) <= 20, "history is unbounded"


# =====================================================================
# trading calendar
# =====================================================================
class TestCalendar:
    def test_weekend_is_not_a_feed_failure(self):
        saturday = date(2026, 10, 10)
        assert expected_last_bar(saturday, "equity") == date(2026, 10, 9)

    def test_crypto_expects_every_calendar_day(self):
        saturday = date(2026, 10, 10)
        assert expected_last_bar(saturday, "crypto") == saturday

    def test_recent_friday_bar_on_a_monday_is_not_stale(self):
        friday = date(2026, 10, 9)
        assert not is_stale(friday, date(2026, 10, 12), "equity")

    def test_equity_bar_from_last_year_is_stale(self):
        assert is_stale(date(2025, 10, 9), date(2026, 10, 15), "equity")

    def test_crypto_bar_from_last_year_is_stale(self):
        assert is_stale(date(2025, 10, 14), date(2026, 10, 15), "crypto")

    def test_holiday_grace_prevents_false_failure(self):
        # 3 sessions back is inside the unmodelled-holiday window.
        assert not is_stale(date(2026, 10, 12), date(2026, 10, 15), "equity", holiday_grace_days=4)


# =====================================================================
# session-aware refresh: a flat interval cannot see a market opening
# =====================================================================
class TestBehindCalendarRefresh:
    """A cache file can be minutes old and still be a whole session behind."""

    def _write(self, cache_dir, last_date, rows=60):
        end = pd.Timestamp(last_date)
        start = end - pd.tseries.offsets.BDay(rows - 1)
        frame = make_frame(rows, start=start.date().isoformat())
        frame.index = pd.date_range(end=end, periods=rows, freq="B")
        atomic_write_csv(cache_dir / "RELIANCE.csv", frame)
        return frame

    def test_a_missing_session_forces_a_refresh_despite_a_fresh_file(
        self, cache_dir, monkeypatch
    ):
        # Cache stops at Wednesday, today is Friday and the session has closed,
        # so Thursday's bar is missing even though the file is minutes old.
        self._write(cache_dir, "2026-10-14")
        calls = stub_fetch(monkeypatch, make_frame())

        _, prov = price_data.load_with_provenance(
            INSTRUMENT, max_cache_age_minutes=60, today=date(2026, 10, 15),
            calendar_min_gap_minutes=0,
        )

        assert calls["n"] == 1, "a behind-calendar cache was served without checking upstream"
        assert prov.outcome == RefreshOutcome.REFRESHED.value
        assert prov.trigger == RefreshTrigger.BEHIND_CALENDAR.value

    def test_an_up_to_date_cache_is_not_re_downloaded(self, cache_dir, monkeypatch):
        self._write(cache_dir, "2026-10-15")
        calls = stub_fetch(monkeypatch, make_frame())

        _, prov = price_data.load_with_provenance(
            INSTRUMENT, max_cache_age_minutes=60, today=date(2026, 10, 15),
        )

        assert calls["n"] == 0, "an up-to-date cache was re-downloaded"
        assert prov.outcome == RefreshOutcome.CACHE_HIT.value
        assert prov.trigger == RefreshTrigger.NONE.value
        assert prov.fetched_at is None

    def test_the_gap_floor_stops_a_request_per_cycle(self, cache_dir, monkeypatch):
        self._write(cache_dir, "2026-10-01")
        calls = stub_fetch(monkeypatch, make_frame())

        price_data.load_with_provenance(
            INSTRUMENT, max_cache_age_minutes=None, today=date(2026, 10, 15),
            calendar_min_gap_minutes=9999,  # file is far younger than the floor
        )

        assert calls["n"] == 0, "refreshed on every cycle while waiting for a missing bar"

    def test_an_explicit_refresh_still_reports_its_own_trigger(self, cache_dir, monkeypatch):
        self._write(cache_dir, "2026-10-15")
        calls = stub_fetch(monkeypatch, make_frame())

        _, prov = price_data.load_with_provenance(INSTRUMENT, refresh=True)

        assert calls["n"] == 1
        assert prov.trigger == RefreshTrigger.EXPLICIT.value

    def test_the_interval_trigger_is_reported(self, cache_dir, monkeypatch):
        self._write(cache_dir, "2026-10-15")
        calls = stub_fetch(monkeypatch, make_frame())

        _, prov = price_data.load_with_provenance(
            INSTRUMENT, max_cache_age_minutes=0, today=date(2026, 10, 15),
        )

        assert calls["n"] == 1
        assert prov.trigger == RefreshTrigger.INTERVAL.value

    def test_the_sidecar_records_the_trigger(self, cache_dir, monkeypatch):
        self._write(cache_dir, "2026-10-14")
        stub_fetch(monkeypatch, make_frame())

        price_data.load_with_provenance(
            INSTRUMENT, today=date(2026, 10, 15), calendar_min_gap_minutes=0,
        )

        record = read_sidecar(cache_dir / "RELIANCE.csv")
        assert record["detail"] == RefreshTrigger.BEHIND_CALENDAR.value

    def test_a_weekend_is_not_treated_as_a_missing_session(self, cache_dir, monkeypatch):
        # Saturday: the expected newest bar is still Friday, so Friday's data is current.
        self._write(cache_dir, "2026-10-09")  # the Friday before Sat 10 Oct
        calls = stub_fetch(monkeypatch, make_frame())

        price_data.load_with_provenance(
            INSTRUMENT, max_cache_age_minutes=None, today=date(2026, 10, 10),
            calendar_min_gap_minutes=0,
        )

        assert calls["n"] == 0, "a weekend was treated as a missing session"

    def test_crypto_expectation_uses_the_utc_day(self, cache_dir, monkeypatch):
        crypto = price_data.BY_SYMBOL["BTC"]
        end = pd.Timestamp("2026-10-15")
        frame = make_frame(60, start=(end - pd.tseries.offsets.BDay(59)).date().isoformat())
        frame.index = pd.date_range(end=end, periods=60, freq="B")
        atomic_write_csv(cache_dir / "BTC.csv", frame)
        calls = stub_fetch(monkeypatch, make_frame())

        price_data.load_with_provenance(
            crypto, max_cache_age_minutes=None, today=date(2026, 10, 16),
            calendar_min_gap_minutes=0,
        )

        assert calls["n"] == 1, "crypto did not refresh on the new UTC day"


# =====================================================================
# the feed refuses to present stale data as current
# =====================================================================
class TestFeedRefusesStale:
    def test_stale_symbol_is_not_served(self, cache_dir, monkeypatch):
        from stratbot.broker.feed import MarketDataFeed

        old = make_frame(60, start="2020-01-01", end="2020-04-01")
        atomic_write_csv(cache_dir / "RELIANCE.csv", old)
        stub_fetch(monkeypatch, None)

        feed = MarketDataFeed(("RELIANCE",), refresh_interval_minutes=0)
        bars = feed.bars()

        assert bars == {}, "a stale symbol was served as a current price"
        assert feed.provenance["RELIANCE"].status == CacheStatus.STALE.value

    def test_fresh_symbol_is_served(self, cache_dir, monkeypatch):
        from stratbot.broker.feed import MarketDataFeed
        from stratlab.calendar import expected_last_bar

        today = date.today()
        # End the series on the date the calendar says a bar should exist,
        # otherwise the fixture is genuinely stale and correctly refused.
        end = pd.Timestamp(expected_last_bar(today, "equity"))
        start = end - pd.tseries.offsets.BDay(59)
        recent = make_frame(60, start=start.date().isoformat())
        recent.index = pd.date_range(end=end, periods=60, freq="B")
        atomic_write_csv(cache_dir / "RELIANCE.csv", recent)
        calls = stub_fetch(monkeypatch, recent)

        feed = MarketDataFeed(("RELIANCE",), refresh_interval_minutes=0)
        bars = feed.bars()

        assert "RELIANCE" in bars, (
            f"status={feed.provenance['RELIANCE'].status} "
            f"last={feed.provenance['RELIANCE'].last_candle} "
            f"expected={feed.provenance['RELIANCE'].expected_last_candle}"
        )
        assert feed.provenance["RELIANCE"].status == CacheStatus.FRESH.value
        assert calls["n"] == 1

    def test_engine_halts_when_no_symbol_is_usable(self, cache_dir, monkeypatch, tmp_path):
        from stratbot.broker.paper import PaperBroker
        from stratbot.config import BotConfig
        from stratbot.db import Database
        from stratbot.engine import Engine

        atomic_write_csv(cache_dir / "RELIANCE.csv",
                         make_frame(60, start="2020-01-01", end="2020-04-01"))
        monkeypatch.setattr(price_data, "_fetch", lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("no network")))

        db = Database(tmp_path / "h.sqlite3")
        config = BotConfig(symbols=("RELIANCE",), initial_cash=500_000.0)
        engine = Engine(config, db, broker=PaperBroker(config.initial_cash))

        result = engine.cycle()

        assert result.halted
        assert result.bars == {}
        assert db.recent_fills(50) == [], "traded without usable data"
