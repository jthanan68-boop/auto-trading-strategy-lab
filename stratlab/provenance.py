"""Provenance and validation for cached market data.

Every cache read can now answer: which provider supplied it, when it was
fetched, how old the newest candle is, and whether that came from a real fetch
or from the file already on disk. Nothing here invents a fetch that did not
happen.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path

import pandas as pd

from .calendar import expected_last_bar, is_stale

PROVIDER = "yfinance.download"
MIN_HISTORY_ROWS = 31


class RefreshOutcome(str, Enum):
    CACHE_HIT = "cache_hit"                     # served from disk, no fetch attempted
    REFRESHED = "refreshed"                     # fetched and written this call
    REFRESH_FAILED_STALE_CACHE = "failed_stale_cache"
    REFRESH_FAILED_NO_CACHE = "failed_no_cache"
    REJECTED_INVALID = "rejected_invalid"       # fetched but failed validation


class RefreshTrigger(str, Enum):
    NONE = "none"                        # cache hit, nothing fetched
    EXPLICIT = "explicit"                # the caller asked for it
    INTERVAL = "interval"                # the cache file aged past the interval
    BEHIND_CALENDAR = "behind_calendar"  # a newer session should already exist


class CacheStatus(str, Enum):
    FRESH = "fresh"        # fetched this session, newest bar matches the calendar
    CACHED = "cached"      # from disk, not fetched this session
    STALE = "stale"        # from disk and behind the calendar
    ABSENT = "absent"


@dataclass
class Provenance:
    """What we actually know about the data behind a symbol."""

    symbol: str
    status: str
    provider: str
    path: str
    rows: int
    last_candle: str
    cache_age_days: float
    expected_last_candle: str
    stale: bool
    outcome: str
    fetched_at: str | None = None
    detail: str = ""
    trigger: str = RefreshTrigger.NONE.value

    def log_line(self) -> str:
        fetched = self.fetched_at or "never"
        return (
            f"{self.symbol:<10} status={self.status:<7} outcome={self.outcome:<24} "
            f"provider={self.provider} rows={self.rows} "
            f"last_candle={self.last_candle} expected={self.expected_last_candle} "
            f"age={self.cache_age_days:.2f}d trigger={self.trigger} fetched_at={fetched}"
            + (f" detail={self.detail}" if self.detail else "")
        )


@dataclass
class Sidecar:
    """Per-symbol JSON record kept beside the CSV."""

    symbol: str
    provider: str
    fetched_at: str | None
    last_candle: str | None
    rows: int
    outcome: str
    detail: str = ""
    history: list = field(default_factory=list)


def sidecar_path(csv_path: Path) -> Path:
    return csv_path.with_suffix(".meta.json")


def read_sidecar(csv_path: Path) -> dict | None:
    path = sidecar_path(csv_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_sidecar(csv_path: Path, record: Sidecar) -> None:
    path = sidecar_path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(record)
    payload["history"] = record.history[-20:]
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def atomic_write_text(path: Path, text: str, retries: int = 5) -> None:
    """Write via a temp file in the same directory, then rename.

    os.replace is atomic on the same filesystem, so a reader never observes a
    half-written file and an interrupted write leaves the previous file intact.

    Windows raises PermissionError (WinError 5) when another process holds the
    destination open. That is transient locking rather than a real failure, so it
    is retried briefly before giving up; the previous file is still intact either
    way.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(retries):
            try:
                os.replace(tmp_name, path)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def atomic_write_csv(path: Path, frame: pd.DataFrame) -> None:
    """Persist a cache frame with a named date column.

    The index has to be written out, not left implicit, or a reader given
    `index_col="date"` finds no such column and the cache looks corrupt.
    """
    out = frame.copy()
    out.index.name = "date"
    atomic_write_text(path, out.to_csv())


class ValidationError(ValueError):
    """The downloaded frame cannot be trusted, so the cache must not be replaced."""


def validate_frame(frame: pd.DataFrame, required: list[str], min_rows: int) -> pd.DataFrame:
    """Reject anything that would corrupt a working cache."""
    if frame is None or frame.empty:
        raise ValidationError("empty frame")
    if len(frame) < min_rows:
        raise ValidationError(f"only {len(frame)} rows, need {min_rows}")

    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValidationError(f"missing columns {missing}")

    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValidationError("index is not a DatetimeIndex")
    if frame.index.tz is not None:
        raise ValidationError("index must be timezone-naive dates")
    if frame.index.hasnans:
        raise ValidationError("index contains NaT")

    if not frame.index.is_monotonic_increasing:
        raise ValidationError("index is not sorted")
    if frame.index.duplicated().any():
        dupes = frame.index[frame.index.duplicated()].unique()
        raise ValidationError(f"duplicate dates {list(dupes[:5])}")

    numeric = frame[required].apply(pd.to_numeric, errors="coerce")
    if numeric.isna().any().any():
        raise ValidationError("NaN in OHLC columns")
    if not (numeric["high"] >= numeric["low"]).all():
        raise ValidationError("high below low")
    if (numeric[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValidationError("non-positive price")

    frame = frame.copy()
    frame[required] = numeric
    return frame


def build_provenance(
    symbol: str,
    csv_path: Path,
    frame: pd.DataFrame | None,
    outcome: RefreshOutcome,
    asset_class: str,
    fetched_at: datetime | None,
    detail: str = "",
    today: date | None = None,
    trigger: str = RefreshTrigger.NONE.value,
    provider: str = PROVIDER,
) -> Provenance:
    today = today or datetime.now(timezone.utc).date()
    expected = expected_last_bar(today, asset_class)

    if frame is None or frame.empty:
        return Provenance(
            symbol=symbol, status=CacheStatus.ABSENT.value, provider=provider,
            path=str(csv_path), rows=0, last_candle="none", cache_age_days=float("inf"),
            expected_last_candle=str(expected), stale=True,
            outcome=outcome.value, detail=detail, trigger=trigger,
            fetched_at=fetched_at.isoformat(timespec="seconds") if fetched_at else None,
        )

    last_candle = frame.index[-1].date()
    age = (today - last_candle).days
    stale = is_stale(last_candle, today, asset_class)
    if outcome is RefreshOutcome.REFRESHED:
        status = CacheStatus.FRESH.value if not stale else CacheStatus.STALE.value
    else:
        status = CacheStatus.STALE.value if stale else CacheStatus.CACHED.value

    return Provenance(
        symbol=symbol, status=status, provider=provider, path=str(csv_path),
        rows=len(frame), last_candle=str(last_candle), cache_age_days=float(age),
        expected_last_candle=str(expected), stale=stale, outcome=outcome.value,
        fetched_at=fetched_at.isoformat(timespec="seconds") if fetched_at else None,
        detail=detail, trigger=trigger,
    )