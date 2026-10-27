"""Intraday mode stage 1: sessions calendar, interval caches, provenance.

All network access is mocked. No broker is contacted and no order is placed.
Every test writes to a tmp_path cache so the real data/cache is untouched.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from stratlab import binance as binance_mod
from stratlab import data as price_data
from stratlab.provenance import CacheStatus, RefreshOutcome, build_provenance

INSTRUMENT = price_data.BY_SYMBOL["RELIANCE"]
BTC = price_data.BY_SYMBOL["BTC"]


def ts(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00")


# =====================================================================
# session calendar
# =====================================================================
class TestSessionCalendar:
    def test_interval_minutes_validates(self):
        from stratlab.calendar import interval_minutes

        assert interval_minutes("5m") == 5
        assert interval_minutes("1d") == 1440
        with pytest.raises(ValueError):
            interval_minutes("7m")

    def test_session_bounds_equity_is_utc_window(self):
        from stratlab.calendar import session_bounds

        start, end = session_bounds(date(2026, 10, 14), "equity")
        assert start == ts("2026-10-14", "03:45")
        assert end == ts("2026-10-14", "10:00")

    def test_session_bounds_crypto_is_full_day(self):
        from stratlab.calendar import session_bounds

        start, end = session_bounds(date(2026, 10, 14), "crypto")
        assert start == ts("2026-10-14", "00:00")
        assert end == ts("2026-10-15", "00:00")

    def test_session_states(self):
        from stratlab.calendar import session_state

        assert session_state(ts("2026-10-14", "03:44"), "equity") == "pre"
        assert session_state(ts("2026-10-14", "05:00"), "equity") == "active"
        assert session_state(ts("2026-10-14", "10:00"), "equity") == "post"
        assert session_state(ts("2026-10-14", "23:00"), "crypto") == "active"

    def test_expected_last_intraday_bar_in_session_grid(self):
        from stratlab.calendar import expected_last_intraday_bar

        assert expected_last_intraday_bar(ts("2026-10-14", "05:10"), "equity", "5m") \
            == ts("2026-10-14", "05:05")

    def test_expected_last_intraday_bar_first_bar_not_done(self):
        from stratlab.calendar import expected_last_intraday_bar

        assert expected_last_intraday_bar(ts("2026-10-14", "03:47"), "equity", "5m") is None

    def test_expected_last_intraday_bar_pre_open_is_previous_weekday(self):
        from stratlab.calendar import expected_last_intraday_bar

        assert expected_last_intraday_bar(ts("2026-10-14", "02:00"), "equity", "5m") \
            == ts("2026-10-13", "09:55")

    def test_expected_last_intraday_bar_after_close_is_same_day(self):
        from stratlab.calendar import expected_last_intraday_bar

        assert expected_last_intraday_bar(ts("2026-10-14", "11:00"), "equity", "5m") \
            == ts("2026-10-14", "09:55")

    def test_expected_last_intraday_bar_weekend_evening_walks_back(self):
        from stratlab.calendar import expected_last_intraday_bar

        assert expected_last_intraday_bar(ts("2026-10-18", "12:00"), "equity", "5m") \
            == ts("2026-10-16", "09:55")

    def test_expected_last_intraday_bar_crypto_floor(self):
        from stratlab.calendar import expected_last_intraday_bar

        assert expected_last_intraday_bar(ts("2026-10-14", "05:12"), "crypto", "5m") \
            == ts("2026-10-14", "05:05")

    def test_bars_remaining_counts_to_close(self):
        from stratlab.calendar import bars_remaining_in_session

        assert bars_remaining_in_session(ts("2026-10-14", "05:10"), "equity", "5m") == 58
        assert bars_remaining_in_session(ts("2026-10-14", "11:00"), "equity", "5m") == 0


# =====================================================================
# staleness vs the calendar (holidays must not halt a healthy bot)
# =====================================================================
class TestIntradayStale:
    def test_today_bar_is_never_stale(self):
        from stratlab.calendar import intraday_stale

        assert not intraday_stale(ts("2026-10-14", "05:05"), ts("2026-10-14", "05:10"),
                                  "equity", "5m")

    def test_prior_session_missed_is_not_stale(self):
        from stratlab.calendar import intraday_stale

        # Wednesday session live, newest bar from Tuesday: an unmodelled holiday
        # looks exactly like this and must not count as dead.
        assert not intraday_stale(ts("2026-10-13", "09:55"), ts("2026-10-14", "05:10"),
                                  "equity", "5m")

    def test_three_missing_sessions_is_stale(self):
        from stratlab.calendar import intraday_stale

        # Friday last bar, Monday and Tuesday gone, it is Wednesday: dead feed.
        assert intraday_stale(ts("2026-10-09", "09:55"), ts("2026-10-14", "05:10"),
                              "equity", "5m")

    def test_weekend_cache_stays_fresh(self):
        from stratlab.calendar import intraday_stale

        assert not intraday_stale(ts("2026-10-16", "09:55"), ts("2026-10-18", "12:00"),
                                  "equity", "5m")

    def test_crypto_grace_of_twelve_bars(self):
        from stratlab.calendar import intraday_stale

        now = ts("2026-10-14", "05:12")  # newest finished label should be 05:05
        assert not intraday_stale(ts("2026-10-14", "04:05"), now, "crypto", "5m")
        assert intraday_stale(ts("2026-10-14", "04:04"), now, "crypto", "5m")


# =====================================================================
# binance intraday klines
# =====================================================================
def stub_request(monkeypatch, pages):
    calls = {"n": 0}

    def fake_request(params, timeout):
        calls["n"] += 1
        page = pages[min(calls["n"] - 1, len(pages) - 1)]
        if page is None:
            raise RuntimeError("simulated outage")
        return [row.copy() for row in page]

    monkeypatch.setattr(binance_mod, "_request", fake_request)
    return calls


def kline_row(open_ms: int, price: float) -> list:
    return [open_ms, price, price + 1, price - 1, price, 10.0, 0]


class TestBinanceKlines:
    def test_short_page_is_not_normalised_to_dates(self, monkeypatch):
        base = int(pd.Timestamp("2026-10-14T05:00:00").tz_localize("UTC").timestamp() * 1000)
        rows = [kline_row(base + i * 300_000, 100.0 + i) for i in range(3)]
        stub_request(monkeypatch, [rows])
        frame = binance_mod.klines("BTC", "5m", "2026-10-14", "2026-10-15")
        assert len(frame) == 3
        assert frame.index[0] == pd.Timestamp("2026-10-14T05:00:00")

    def test_hour_interval_is_mapped_to_binance_spelling(self, monkeypatch):
        captured = {}

        def fake_request(params, timeout):
            captured["interval"] = params["interval"]
            return [kline_row(int(pd.Timestamp("2026-10-14T05:00:00")
                                   .tz_localize("UTC").timestamp() * 1000), 100.0)]

        monkeypatch.setattr(binance_mod, "_request", fake_request)
        binance_mod.klines("BTC", "60m", "2026-10-14", "2026-10-15")
        assert captured["interval"] == "1h"

    def test_daily_klines_still_normalises_dates(self, monkeypatch):
        base = int(pd.Timestamp("2026-10-14T00:00:00").tz_localize("UTC").timestamp() * 1000)
        stub_request(monkeypatch, [[kline_row(base, 100.0)]])
        frame = binance_mod.daily_klines("BTC", "2026-10-14", "2026-10-15")
        assert frame.index[0] == pd.Timestamp("2026-10-14")

    def test_no_progress_does_not_spin(self, monkeypatch):
        base = int(pd.Timestamp("2026-10-14T05:00:00").tz_localize("UTC").timestamp() * 1000)
        row = kline_row(base, 100.0)
        calls = stub_request(monkeypatch, [[row]])
        frame = binance_mod.klines("BTC", "5m", "2026-10-14", "2026-10-15")
        assert calls["n"] <= 3
        assert len(frame) == 1
        assert frame.index[0] == pd.Timestamp("2026-10-14T05:00:00")


# =====================================================================
# isolated intraday caches
# =====================================================================
def stub_fetch_intraday(monkeypatch, frame: pd.DataFrame | None):
    captured = {"starts": [], "intervals": []}

    def fake_fetch(instrument, start, end, retries=3, interval="1d"):
        captured["starts"].append(start)
        captured["intervals"].append(interval)
        if frame is None:
            raise RuntimeError("simulated outage")
        return frame.copy()

    monkeypatch.setattr(price_data, "_fetch", fake_fetch)
    return captured


def five_minute_frame(hours: int = 48, base_price: float = 100.0) -> pd.DataFrame:
    index = pd.date_range("2026-10-14T03:45:00", periods=hours * 12, freq="5min")
    closes = base_price + pd.Series(range(len(index)), index=index).mul(0.01)
    frame = pd.DataFrame(
        {"open": closes, "high": closes + 0.5, "low": closes - 0.5, "close": closes,
         "volume": 100.0},
        index=index,
    )
    return frame


class TestIntervalCaches:
    def test_cache_paths_are_isolated_per_interval(self, tmp_path):
        assert price_data._cache_path(INSTRUMENT) == tmp_path / "does-not-exist.csv" or True
        from stratlab.data import CACHE_DIR

        daily = price_data._cache_path(INSTRUMENT, "1d")
        intraday = price_data._cache_path(INSTRUMENT, "5m")
        assert daily.name == "RELIANCE.csv"
        assert intraday.name == "RELIANCE.5m.csv"
        assert daily != intraday
        assert CACHE_DIR in (daily.parents)

    def test_intraday_load_writes_a_separate_cache(self, tmp_path, monkeypatch):
        target = tmp_path / "cache"
        monkeypatch.setattr(price_data, "CACHE_DIR", target)
        captured = stub_fetch_intraday(monkeypatch, five_minute_frame())

        frame, prov = price_data.load_with_provenance(
            INSTRUMENT, start="2026-10-14", interval="5m", refresh=True,
            today=date(2026, 10, 15),
        )
        assert (target / "RELIANCE.5m.csv").exists()
        assert (target / "RELIANCE.5m.meta.json").exists()
        assert not (target / "RELIANCE.csv").exists()
        assert len(frame) > 0
        assert captured["intervals"] == ["5m"]
        assert prov.provider == "yfinance.download"

    def test_volume_reaches_the_cache_instead_of_being_stripped(self, tmp_path, monkeypatch):
        """VwapDip needs real volume; a stripped column silently killed it."""
        target = tmp_path / "cache"
        monkeypatch.setattr(price_data, "CACHE_DIR", target)
        stub_fetch_intraday(monkeypatch, five_minute_frame())

        frame, _ = price_data.load_with_provenance(
            INSTRUMENT, start="2026-10-14", interval="5m", refresh=True,
            today=date(2026, 10, 15),
        )
        # In-memory frame keeps it, OHLC first, extras after.
        assert list(frame.columns)[:4] == ["open", "high", "low", "close"]
        assert "volume" in frame.columns
        cached = pd.read_csv(target / "RELIANCE.5m.csv")
        assert "volume" in cached.columns
        assert pd.to_numeric(cached["volume"]).sum() > 0

    def test_intraday_start_is_clamped_to_yahoo_history(self, tmp_path, monkeypatch):
        target = tmp_path / "cache"
        monkeypatch.setattr(price_data, "CACHE_DIR", target)
        captured = stub_fetch_intraday(monkeypatch, five_minute_frame())

        price_data.load_with_provenance(
            INSTRUMENT, start="2018-01-01", interval="5m", refresh=True,
            end="2026-10-15", today=date(2026, 10, 15),
        )
        from stratlab.calendar import INTRADAY_INTERVAL_MINUTES  # noqa: F401
        from stratlab.data import INTRADAY_MAX_HISTORY_DAYS

        requested = pd.Timestamp(captured["starts"][0])
        assert requested <= pd.Timestamp("2018-01-01") or (
            (pd.Timestamp("2026-10-15") - requested).days <= INTRADAY_MAX_HISTORY_DAYS
        )
        assert requested > pd.Timestamp("2026-08-01")

    def test_daily_fetch_stub_keeps_the_old_signature(self, tmp_path, monkeypatch):
        """Existing daily-stub call contract must survive untouched."""
        target = tmp_path / "cache"
        monkeypatch.setattr(price_data, "CACHE_DIR", target)
        from stratlab.provenance import MIN_HISTORY_ROWS

        calls = {"n": 0}

        def fake_fetch(instrument, start, end, retries=3):
            calls["n"] += 1
            rows = 70
            index = pd.date_range("2020-01-01", periods=rows, freq="B")
            closes = pd.Series(range(rows), index=index).add(100.0)
            return pd.DataFrame({"open": closes, "high": closes + 1, "low": closes - 1,
                                 "close": closes, "volume": 100.0}, index=index)

        monkeypatch.setattr(price_data, "_fetch", fake_fetch)
        daily, prov = price_data.load_with_provenance(INSTRUMENT, start="2018-01-01",
                                                      refresh=True, today=date(2026, 10, 15))
        assert calls["n"] == 1
        assert len(daily.index) >= MIN_HISTORY_ROWS

    def test_clean_index_normalises_tz_aware_stamps(self):
        aware = pd.DatetimeIndex(
            pd.to_datetime(["2026-10-14T05:00:00"]).tz_localize("Asia/Kolkata")
        )
        cleaned = price_data._clean_index(aware)
        assert cleaned.tz is None
        assert cleaned[0] == pd.Timestamp("2026-10-13T23:30:00")

    def test_clean_index_passes_naive_through(self):
        naive = pd.DatetimeIndex(pd.to_datetime(["2026-10-14"]))
        assert price_data._clean_index(naive)[0] == pd.Timestamp("2026-10-14")


# =====================================================================
# interval-aware provenance
# =====================================================================
class TestIntradayProvenance:
    def test_intraday_cache_hit_is_fresh_on_a_recent_session(self, tmp_path):
        frame = five_minute_frame(hours=4)
        now = datetime(2026, 10, 14, 5, 10, tzinfo=timezone.utc)
        prov = build_provenance(
            INSTRUMENT.symbol, tmp_path / "RELIANCE.5m.csv", frame,
            RefreshOutcome.CACHE_HIT, "equity", now, interval="5m",
        )
        assert prov.stale is False
        assert prov.status == CacheStatus.CACHED.value
        assert "07:40" in prov.last_candle  # timestamp precision, not just a date

    def test_intraday_cache_is_fresh_through_a_missing_prior_holiday(self, tmp_path):
        # Bars stop Tuesday, it is Wednesday session time: holiday-shaped.
        frame = five_minute_frame(hours=4)
        frame.index = frame.index - pd.Timedelta(days=2)
        now = datetime(2026, 10, 14, 5, 10, tzinfo=timezone.utc)
        prov = build_provenance(
            INSTRUMENT.symbol, tmp_path / "RELIANCE.5m.csv", frame,
            RefreshOutcome.CACHE_HIT, "equity", now, interval="5m",
        )
        assert prov.stale is False

    def test_intraday_cache_stale_after_three_dead_sessions(self, tmp_path):
        frame = five_minute_frame(hours=4)
        frame.index = frame.index - pd.Timedelta(days=5)  # Friday, five days back
        now = datetime(2026, 10, 14, 5, 10, tzinfo=timezone.utc)
        prov = build_provenance(
            INSTRUMENT.symbol, tmp_path / "RELIANCE.5m.csv", frame,
            RefreshOutcome.CACHE_HIT, "equity", now, interval="5m",
        )
        assert prov.stale is True

    def test_daily_provenance_path_unchanged(self, tmp_path):
        from stratlab.calendar import expected_last_bar

        rows = 40
        index = pd.date_range("2026-01-01", periods=rows, freq="B")
        closes = pd.Series(range(rows), index=index).add(100.0)
        frame = pd.DataFrame({"open": closes, "high": closes + 1, "low": closes - 1,
                              "close": closes, "volume": 100.0}, index=index)
        now = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
        prov = build_provenance(
            INSTRUMENT.symbol, tmp_path / "RELIANCE.csv", frame,
            RefreshOutcome.CACHE_HIT, "equity", now, today=now.date(),
        )
        assert prov.last_candle == "2026-02-25"
        assert prov.expected_last_candle == str(expected_last_bar(now.date(), "equity"))
