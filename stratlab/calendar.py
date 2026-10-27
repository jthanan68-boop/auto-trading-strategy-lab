"""Trading-calendar expectations per asset class.

Used to decide whether an old bar is a feed failure or simply a market that
was closed. Getting this wrong in the cautious direction halts a healthy bot on
every weekend; getting it wrong the other way trades on a dead feed.

Only the weekend calendar is modelled. Indian exchange holidays are not encoded
here, so a `holiday_grace_days` allowance covers them rather than pretending to
know the NSE holiday list.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pandas as pd

SATURDAY = 5
SUNDAY = 6
NSE_CLOSE_UTC_HOUR = 10  # 15:30 IST
# 09:15 IST open, expressed as minutes past midnight UTC on the session day.
NSE_OPEN_MINUTES_UTC = 3 * 60 + 45

INTRADAY_INTERVAL_MINUTES = {
    "1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1d": 1440,
}


def interval_minutes(interval: str) -> int:
    minutes = INTRADAY_INTERVAL_MINUTES.get(interval)
    if minutes is None:
        raise ValueError(f"unsupported interval: {interval}")
    return minutes


def floor_to_interval(ts: datetime, interval: str) -> datetime:
    """Snap a naive-UTC timestamp down to the interval grid anchored at midnight."""
    minutes = interval_minutes(interval)
    midnight = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (ts - midnight).total_seconds() // 60
    return midnight + timedelta(minutes=int(elapsed // minutes) * minutes)


def session_bounds(day: date, asset_class: str) -> tuple[datetime, datetime]:
    """Naive-UTC [start, end) cover of one session on `day`.

    Indian equities run 09:15-15:30 IST, stored here as UTC 03:45-10:00 to match
    the naive-UTC bar timestamps the caches keep. Crypto has no close; its
    session is simply the whole UTC day.
    """
    if asset_class == "crypto":
        start = datetime.combine(day, time.min)
        return start, start + timedelta(days=1)
    start = datetime.combine(day, time(hour=NSE_OPEN_MINUTES_UTC // 60,
                                       minute=NSE_OPEN_MINUTES_UTC % 60))
    return start, datetime.combine(day, time(hour=NSE_CLOSE_UTC_HOUR))


def session_state(now: datetime, asset_class: str) -> str:
    """Where `now` (naive UTC) sits relative to the session covering its own day."""
    start, end = session_bounds(now.date(), asset_class)
    if now < start:
        return "pre"
    if now >= end:
        return "post"
    return "active"


def is_nse_trading_weekday(day: date) -> bool:
    return not is_weekend(day)


def expected_last_intraday_bar(now: datetime, asset_class: str,
                               interval: str) -> datetime | None:
    """Most recent bar label a healthy feed should already have finished.

    None means the first bar of the session has not closed yet, so nothing is
    known to be missing. For equities, before the open and after the close this
    falls back to the previous weekday's final label, which keeps overnight and
    weekend reads from flagging a healthy session as stale.
    """
    minutes = interval_minutes(interval)
    if asset_class == "crypto":
        return floor_to_interval(now - timedelta(minutes=minutes), interval)

    start, end = session_bounds(now.date(), asset_class)
    last_label = end - timedelta(minutes=minutes)
    if now < start:
        previous = previous_weekday(now.date() - timedelta(days=1))
        return session_bounds(previous, asset_class)[1] - timedelta(minutes=minutes)
    if now >= end:
        if is_weekend(now.date()):
            previous = previous_weekday(now.date() - timedelta(days=1))
            return session_bounds(previous, asset_class)[1] - timedelta(minutes=minutes)
        return last_label
    first_done = start + timedelta(minutes=minutes)
    if now < first_done:
        return None
    return min(floor_to_interval(now - timedelta(minutes=minutes), interval), last_label)


def intraday_stale(last_bar: datetime, now: datetime, asset_class: str,
                   interval: str) -> bool:
    """True only when bar gaps cannot be explained by the calendar.

    Within a running session a short gap is the refresh path's problem, not
    proof of a dead feed: Yahoo is delayed ~15 minutes and an unmodelled
    exchange holiday publishes nothing for the whole "session". So staleness
    here means whole missing sessions - and even then only once today's session
    has started as well, which is what separates a dead feed from a holiday.
    """
    if asset_class == "crypto":
        expected = floor_to_interval(now - timedelta(minutes=interval_minutes(interval)),
                                     interval)
        return last_bar < expected - timedelta(minutes=interval_minutes(interval) * 12)
    if last_bar.date() == now.date():
        # A bar from today is itself proof the feed is alive, however stale it
        # may be inside the session; the engine's currency check does that job.
        return False
    # Whole-session counting only, weekends already excluded: two days of
    # missed weekday sessions covers a two-day exchange holiday plus a weekend,
    # while a genuinely dead feed still trips it within a few days.
    return bars_expected_since(last_bar.date(), now.date(), asset_class) >= 3


def bars_remaining_in_session(now: datetime, asset_class: str, interval: str) -> int:
    """Completed-bar slots still to come before the session ends."""
    minutes = interval_minutes(interval)
    if asset_class == "crypto":
        _, end = session_bounds(now.date(), asset_class)
        remaining = (end - now).total_seconds() / 60.0
        return max(0, int(remaining // minutes))
    start, end = session_bounds(now.date(), asset_class)
    if now >= end:
        return 0
    if now < start:
        return (end - start).total_seconds() // 60 // minutes
    remaining = (end - now).total_seconds() / 60.0
    return max(0, int(remaining // minutes))


def is_weekend(day: date) -> bool:
    return day.weekday() in (SATURDAY, SUNDAY)


def previous_weekday(day: date) -> date:
    cursor = day
    while is_weekend(cursor):
        cursor -= timedelta(days=1)
    return cursor


def expected_last_bar(
    today: date,
    asset_class: str,
    now_hour_utc: int = NSE_CLOSE_UTC_HOUR,
) -> date:
    """Latest date on which a completed daily bar should exist.

    Crypto trades every calendar day. Indian equities trade Mon-Fri and close at
    15:30 IST, which is 10:00 UTC, so before that hour a weekday has not produced
    a completed bar yet and the previous session is still the newest one.
    """
    if asset_class == "crypto":
        return today

    if is_weekend(today):
        return previous_weekday(today)
    if now_hour_utc < NSE_CLOSE_UTC_HOUR:
        return previous_weekday(today - timedelta(days=1))
    return today


def is_stale(
    last_bar: date,
    today: date,
    asset_class: str,
    holiday_grace_days: int = 4,
) -> bool:
    """True only when the gap exceeds what the calendar can explain.

    The grace window is what stops an unmodelled exchange holiday from being
    reported as a dead feed.
    """
    expected = expected_last_bar(today, asset_class)
    return (expected - last_bar).days > holiday_grace_days


def behind_calendar(last_bar: date, today: date, asset_class: str) -> bool:
    """True when a bar newer than `last_bar` should already have been published.

    This is the *refresh* trigger, deliberately stricter than is_stale: no grace
    window, because the question is "should today's data exist yet", not "is this
    feed dead". A flat refresh interval cannot answer that, which is how a
    dashboard ends up showing yesterday while the exchange shows today.
    """
    return last_bar < expected_last_bar(today, asset_class)


def session_end(bar_date: date, asset_class: str) -> datetime:
    """When the trading session covered by `bar_date` finished.

    Indian equities close at 15:30 IST. Crypto has no close; its UTC day simply
    rolls over at midnight.
    """
    if asset_class == "crypto":
        return datetime.combine(bar_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return datetime.combine(bar_date, time(NSE_CLOSE_UTC_HOUR), tzinfo=timezone.utc)


def session_closed(bar_date: date, asset_class: str, now: datetime | None = None) -> bool:
    """True once the session behind this bar is over, so its close is final."""
    now = now or datetime.now(timezone.utc)
    return now >= session_end(bar_date, asset_class)


def drop_unfinished(frame, asset_class: str, now: datetime | None = None,
                    interval: str = "1d"):
    """Remove the newest bar while it is still unfinished.

    Daily bars are unfinished while their session runs and Yahoo revises the
    candle. An intraday bar is unfinished until its own interval window has
    elapsed since its label, which both bars are assumed to carry as their
    OPEN timestamp.
    """
    if frame is None or frame.empty:
        return frame
    if interval != "1d":
        last = pd.Timestamp(frame.index[-1]).to_pydatetime()
        if now is None:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        if now >= last + timedelta(minutes=interval_minutes(interval)):
            return frame
        return frame.iloc[:-1]
    last = pd.Timestamp(frame.index[-1]).date()
    if session_closed(last, asset_class, now):
        return frame
    return frame.iloc[:-1]


def intraday_behind(last_bar: datetime, now: datetime, asset_class: str,
                    interval: str) -> bool:
    """True when a newer intraday bar should already exist.

    This is the refresh trigger, deliberately stricter than intraday_stale. For
    crypto the next label is always expected; for equities only while the
    session is actually running.
    """
    if asset_class == "crypto":
        expected = floor_to_interval(now - timedelta(minutes=interval_minutes(interval)),
                                     interval)
        return last_bar < expected
    if session_state(now, asset_class) != "active":
        return False
    expected = expected_last_intraday_bar(now, asset_class, interval)
    return expected is not None and last_bar < expected


def bars_expected_since(last_bar: date, today: date, asset_class: str) -> int:
    """How many sessions should have appeared between two dates."""
    expected = expected_last_bar(today, asset_class)
    if expected <= last_bar:
        return 0
    if asset_class == "crypto":
        return (expected - last_bar).days
    count = 0
    cursor = last_bar + timedelta(days=1)
    while cursor <= expected:
        if not is_weekend(cursor):
            count += 1
        cursor += timedelta(days=1)
    return count