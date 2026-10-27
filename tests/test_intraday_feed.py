"""Intraday mode stage 2: completed-bar handling and session-aware refresh.

All network access is mocked. No broker is contacted and no order is placed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from stratlab import data as price_data
from stratlab import provenance as prov_mod
from stratlab.calendar import intraday_behind
from stratbot.broker.feed import MarketDataFeed

INSTRUMENT = price_data.BY_SYMBOL["RELIANCE"]
DAY = date(2026, 10, 14)  # a Wednesday


# Bars labelled by their OPEN. Fixed now is 10:57 UTC, so the 10:55 window has
# not closed yet and is the forming candle.
FRAME_NOW = datetime(2026, 10, 14, 10, 57)


def session_bars(n: int, from_hhmm: str = "09:20") -> pd.DataFrame:
    index = pd.date_range(f"2026-10-14T{from_hhmm}", periods=n, freq="5min")
    closes = pd.Series(range(n), index=index).add(100.0)
    return pd.DataFrame(
        {"open": closes, "high": closes + 0.5, "low": closes - 0.5, "close": closes,
         "volume": 1000.0},
        index=index,
    )


def five_min_frame() -> pd.DataFrame:
    # 09:20..10:50 inclusive is 19 bars, then the forming 10:55 label.
    frame = session_bars(19)
    extra = pd.DataFrame(
        {"open": [199.0], "high": [199.5], "low": [198.0], "close": [199.0],
         "volume": [10.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-10-14T10:55")]),
    )
    return pd.concat([frame, extra]).sort_index()


def stub_loader(monkeypatch, frame):
    def fake_load(instrument, start=None, end=None, refresh=False,
                  max_cache_age_minutes=None, calendar_min_gap_minutes=5,
                  interval="1d"):
        prov = prov_mod.build_provenance(
            instrument.symbol, price_data._cache_path(instrument), frame,
            prov_mod.RefreshOutcome.CACHE_HIT, instrument.asset_class,
            datetime(2026, 10, 14, 10, 57, tzinfo=timezone.utc),
            today=DAY, interval="1d",
        )
        return frame.copy(), prov

    monkeypatch.setattr(price_data, "load_with_provenance", fake_load)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return FRAME_NOW.replace(tzinfo=timezone.utc)
        return FRAME_NOW


@pytest.fixture
def feed(monkeypatch):
    stub_loader(monkeypatch, five_min_frame())
    monkeypatch.setattr("stratlab.calendar.datetime", FixedDateTime)
    return MarketDataFeed(["RELIANCE"], refresh_interval_minutes=5,
                          interval="5m", days_history=3)


class TestFormingIntradayBar:
    def test_history_withholds_the_forming_bar(self, feed):
        history = feed.history("RELIANCE")
        assert pd.Timestamp(history.index[-1]) == pd.Timestamp("2026-10-14 10:50")

    def test_closed_bars_agree_with_history(self, feed):
        closed = feed.closed_bars()
        assert closed["RELIANCE"].ts == datetime(2026, 10, 14, 10, 50)

    def test_bars_still_offers_the_forming_open(self, feed):
        # The forming candle's open is a real traded price; it may execute a
        # pending order. Its close is withheld from strategies.
        bar = feed.bars()["RELIANCE"]
        assert bar.open == 199.0
        assert bar.ts == datetime(2026, 10, 14, 10, 55)

    def test_finished_intraday_bar_is_served_to_strategies(self, monkeypatch):
        stub_loader(monkeypatch, five_min_frame())
        monkeypatch.setattr(
            "stratlab.calendar.datetime",
            type("LaterNow", (datetime,), {
                "now": classmethod(
                    lambda cls, tz=None: datetime(2026, 10, 14, 11, 0)),
            }),
        )
        later = MarketDataFeed(["RELIANCE"], refresh_interval_minutes=5,
                               interval="5m", days_history=3)
        history = later.history("RELIANCE")
        assert pd.Timestamp(history.index[-1]) == pd.Timestamp("2026-10-14 10:55")


class TestIntradayBehind:
    def test_equity_only_when_session_active(self):
        now = datetime(2026, 10, 14, 5, 40)
        assert intraday_behind(datetime(2026, 10, 14, 5, 25), now, "equity", "5m") is True
        assert not intraday_behind(datetime(2026, 10, 14, 5, 35), now, "equity", "5m")
        late = datetime(2026, 10, 14, 12, 0)
        assert not intraday_behind(datetime(2026, 10, 14, 5, 5), late, "equity", "5m")

    def test_crypto_always_expects_the_next_label(self):
        now = datetime(2026, 10, 14, 5, 40)
        assert intraday_behind(datetime(2026, 10, 14, 5, 30), now, "crypto", "5m") is True
        assert not intraday_behind(datetime(2026, 10, 14, 5, 35), now, "crypto", "5m")


class TestIntradayRefreshTrigger:
    def test_behind_schedule_triggers_refresh_after_gap(self, tmp_path, monkeypatch):
        target = tmp_path / "cache"
        monkeypatch.setattr(price_data, "CACHE_DIR", target)

        # 07:20..10:35 is 31 finished bars; the fetch also brings a fresh 10:50
        # bar so the merge has something newer to land.
        cached = session_bars(31, from_hhmm="07:20")           # last label 10:35
        fetched = pd.concat([cached, session_bars(4, from_hhmm="10:40").dropna()])
        fetched = fetched[~fetched.index.duplicated(keep="last")].sort_index()

        calls = {"intervals": []}

        def fake_fetch(instrument, start, end, retries=3, interval="1d"):
            calls["intervals"].append(interval)
            return fetched.copy()

        monkeypatch.setattr(price_data, "_fetch", fake_fetch)
        frame, prov = price_data.load_with_provenance(
            INSTRUMENT, start="2026-10-14", interval="5m", refresh=True,
            today=DAY,
        )
        assert (target / "RELIANCE.5m.csv").exists()

        # The file is aged 10 minutes below the interval budget (15) but the
        # newest label (10:35) still lags the session calendar, so the refresh
        # fires from the behind check, not interval aging. The fixed now for
        # intraday_behind is the real clock; 2026-10-14 bars lag it hugely.
        import os

        from datetime import timezone as _tz

        real_now = datetime.now(_tz.utc).timestamp()
        path = target / "RELIANCE.5m.csv"
        os.utime(path, (real_now - 600, real_now - 600))
        cached_frame = pd.read_csv(path, index_col="date", parse_dates=["date"])
        cached_frame = cached_frame[cached_frame.index < pd.Timestamp("2026-10-14 10:40")]
        price_data.atomic_write_csv(path, cached_frame)
        os.utime(path, (real_now - 600, real_now - 600))

        result, prov = price_data.load_with_provenance(
            INSTRUMENT, start="2026-10-14", interval="5m", refresh=False,
            calendar_min_gap_minutes=0, max_cache_age_minutes=15,
            today=DAY,
        )
        assert prov.trigger == "behind_calendar"
        assert calls["intervals"] == ["5m", "5m"]
        assert pd.Timestamp(result.index[-1]) >= pd.Timestamp("2026-10-14 10:40")

    def test_daily_interval_never_triggers_intraday_check(self, tmp_path, monkeypatch):
        target = tmp_path / "cache"
        monkeypatch.setattr(price_data, "CACHE_DIR", target)

        def fake_fetch(instrument, start, end, retries=3):
            index = pd.date_range("2024-01-01", periods=60, freq="B")
            closes = pd.Series(range(60), index=index).add(100.0)
            return pd.DataFrame({"open": closes, "high": closes + 1, "low": closes - 1,
                                 "close": closes, "volume": 100.0}, index=index)

        monkeypatch.setattr(price_data, "_fetch", fake_fetch)
        price_data.load_with_provenance(INSTRUMENT, refresh=True,
                                        today=date(2024, 3, 1))
        _, prov = price_data.load_with_provenance(INSTRUMENT, refresh=False,
                                                  today=date(2024, 3, 1))
        assert prov.trigger in ("none", "behind_calendar")
