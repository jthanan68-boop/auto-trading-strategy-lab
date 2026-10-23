"""Market data loading with an on-disk cache.

Yahoo Finance is the free source; it covers NSE-listed equities as SYMBOL.NS,
Indian indices as ^NSEI/^BSESN, and crypto as SYMBOL-USD. Everything is cached to
CSV so repeated runs are offline and free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from . import binance
from .calendar import behind_calendar
from .provenance import (
    MIN_HISTORY_ROWS,
    PROVIDER,
    CacheStatus,
    Provenance,
    RefreshOutcome,
    RefreshTrigger,
    Sidecar,
    ValidationError,
    atomic_write_csv,
    build_provenance,
    read_sidecar,
    validate_frame,
    write_sidecar,
)

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
REQUIRED_COLUMNS = ["open", "high", "low", "close"]


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    asset_class: str  # "equity" | "index" | "crypto"
    yfinance_symbol: str
    annual_factor: int = 252

    @property
    def is_crypto(self) -> bool:
        return self.asset_class == "crypto"


UNIVERSE: list[Instrument] = [
    Instrument("NIFTY50", "NIFTY 50 index", "index", "^NSEI"),
    Instrument("RELIANCE", "Reliance Industries", "equity", "RELIANCE.NS"),
    Instrument("TCS", "Tata Consultancy Services", "equity", "TCS.NS"),
    Instrument("INFY", "Infosys", "equity", "INFY.NS"),
    Instrument("HDFCBANK", "HDFC Bank", "equity", "HDFCBANK.NS"),
    Instrument("ITC", "ITC Ltd", "equity", "ITC.NS"),
    Instrument("SBIN", "State Bank of India", "equity", "SBIN.NS"),
    Instrument("BTC", "Bitcoin", "crypto", "BTC-USD", annual_factor=365),
    Instrument("ETH", "Ethereum", "crypto", "ETH-USD", annual_factor=365),
]

def _equity(symbol: str, name: str) -> Instrument:
    return Instrument(symbol, name, "equity", f"{symbol}.NS")


# Widened after the Phase 0 verdict: 9 instruments produced only 3.6 out-of-sample
# trades per pair, too few to separate an edge from noise. Breadth, not more
# strategy variants, is the fix.
NIFTY_BREADTH: list[Instrument] = [
    _equity("ICICIBANK", "ICICI Bank"),
    _equity("HINDUNILVR", "Hindustan Unilever"),
    _equity("BHARTIARTL", "Bharti Airtel"),
    _equity("BAJFINANCE", "Bajaj Finance"),
    _equity("KOTAKBANK", "Kotak Mahindra Bank"),
    _equity("LT", "Larsen & Toubro"),
    _equity("AXISBANK", "Axis Bank"),
    _equity("HCLTECH", "HCL Technologies"),
    _equity("ASIANPAINT", "Asian Paints"),
    _equity("MARUTI", "Maruti Suzuki"),
    _equity("SUNPHARMA", "Sun Pharma"),
    _equity("TITAN", "Titan Company"),
    _equity("ULTRACEMCO", "UltraTech Cement"),
    _equity("NTPC", "NTPC"),
    _equity("POWERGRID", "Power Grid"),
    # TATAMOTORS is excluded: Yahoo returns an empty payload for TATAMOTORS.NS,
    # TATAMOTORS.BO and TATAMOTOR.NS alike, so it never produces a bar.
    _equity("TATASTEEL", "Tata Steel"),
    _equity("ADANIENT", "Adani Enterprises"),
    _equity("ADANIPORTS", "Adani Ports"),
    _equity("JSWSTEEL", "JSW Steel"),
    _equity("TECHM", "Tech Mahindra"),
    _equity("INDUSINDBK", "IndusInd Bank"),
    _equity("HINDALCO", "Hindalco"),
    _equity("BAJAJFINSV", "Bajaj Finserv"),
    _equity("SBILIFE", "SBI Life"),
    _equity("DRREDDY", "Dr Reddy's"),
    _equity("CIPLA", "Cipla"),
    _equity("EICHERMOT", "Eicher Motors"),
    _equity("APOLLOHOSP", "Apollo Hospitals"),
    _equity("GRASIM", "Grasim Industries"),
    _equity("BRITANNIA", "Britannia"),
    _equity("DIVISLAB", "Divi's Laboratories"),
    _equity("HEROMOTOCO", "Hero MotoCorp"),
    _equity("TATACONSUM", "Tata Consumer"),
]

UNIVERSE = UNIVERSE + NIFTY_BREADTH

BY_SYMBOL = {i.symbol: i for i in UNIVERSE}
BROAD_SYMBOLS = [i.symbol for i in NIFTY_BREADTH]


def _cache_path(instrument: Instrument) -> Path:
    return CACHE_DIR / f"{instrument.symbol}.csv"


def _normalise(raw: pd.DataFrame, instrument: Instrument) -> pd.DataFrame:
    frame = raw.copy()
    frame.columns = [str(c).lower().replace(" ", "_") for c in frame.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"{instrument.symbol}: missing columns {missing}")
    frame = frame[REQUIRED_COLUMNS]
    frame.index = pd.to_datetime(frame.index).tz_localize(None)
    frame.index.name = "date"
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame.astype(float).dropna(how="all")


def _fetch(instrument: Instrument, start: str, end: str, retries: int = 3) -> pd.DataFrame:
    import yfinance as yf

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            raw = yf.download(
                instrument.yfinance_symbol,
                start=start,
                end=end,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            if raw is None or raw.empty:
                raise ValueError("empty response")
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            return _normalise(raw, instrument)
        except Exception as exc:  # network flake, rate limit, or a bad symbol
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    return _fallback(instrument, start, end, last_error)


def _fallback(instrument: Instrument, start: str, end: str,
              cause: Exception) -> pd.DataFrame:
    """Second venue, for crypto only.

    Crypto is the one asset class with a key-free alternative to Yahoo, so an
    outage on BTC-USD stops costing the crypto half of the book its data.
    Equities have none, and the error they raise still names only Yahoo.
    """
    if not instrument.is_crypto:
        raise RuntimeError(
            f"{instrument.symbol} ({instrument.yfinance_symbol}): {cause}"
        ) from cause

    try:
        frame = _normalise(binance.daily_klines(instrument.symbol, start, end), instrument)
    except Exception as fallback_exc:
        raise RuntimeError(
            f"{instrument.symbol}: yahoo failed ({cause}); "
            f"binance failed ({fallback_exc})"
        ) from cause

    # Carried on the frame rather than by changing the return type: this
    # signature is load-bearing, the suite stubs it in ~30 places, and
    # provenance has to name the venue that actually served the bars.
    frame.attrs["provider"] = binance.BINANCE_PROVIDER
    return frame


def load(
    instrument: Instrument,
    start: str = "2018-01-01",
    end: str | None = None,
    refresh: bool = False,
) -> pd.DataFrame:
    """Return cached daily OHLC, fetching from Yahoo on first use.

    Kept for the backtest lab, where a long-lived cache is the intent. The
    trading engine uses load_with_provenance instead, which reports what
    actually happened rather than returning a bare frame.
    """
    return load_with_provenance(instrument, start=start, end=end, refresh=refresh)[0]


def _read_cache(path: Path, start: str) -> pd.DataFrame:
    cached = pd.read_csv(path, index_col="date", parse_dates=["date"])
    return cached[cached.index >= pd.Timestamp(start)]


def load_with_provenance(
    instrument: Instrument,
    start: str = "2018-01-01",
    end: str | None = None,
    refresh: bool = False,
    max_cache_age_minutes: int | None = None,
    today: date | None = None,
    calendar_min_gap_minutes: int = 5,
) -> tuple[pd.DataFrame, Provenance]:
    """Load daily OHLC and report exactly where it came from.

    A download happens when asked, when the cache is missing, when the cache file
    has aged past `max_cache_age_minutes`, or when the newest bar is behind the
    trading calendar. The last case is what keeps a flat refresh interval from
    leaving the dashboard showing yesterday while the exchange shows today.

    A failed download never destroys a working cache: the existing file is kept
    and the returned provenance is marked stale, leaving the caller to decide.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(instrument)
    end = end or (pd.Timestamp.today().normalize() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc)
    today = today or now.date()

    cached: pd.DataFrame | None = None
    cached_provider = PROVIDER
    if path.exists():
        try:
            cached = _read_cache(path, start)
        except (OSError, ValueError, pd.errors.ParserError):
            cached = None
        # The sidecar is the only record of which venue served this file.
        record = read_sidecar(path)
        if record and record.get("provider"):
            cached_provider = record["provider"]

    have_usable_cache = cached is not None and len(cached) >= MIN_HISTORY_ROWS
    file_age_minutes = (
        (now.timestamp() - path.stat().st_mtime) / 60.0 if path.exists() else float("inf")
    )

    trigger = RefreshTrigger.NONE
    if refresh:
        trigger = RefreshTrigger.EXPLICIT
    elif have_usable_cache:
        if max_cache_age_minutes is not None and file_age_minutes >= max_cache_age_minutes:
            trigger = RefreshTrigger.INTERVAL
        elif behind_calendar(cached.index[-1].date(), today, instrument.asset_class):
            # A session the calendar says should exist has not been published yet.
            # The gap floor stops a long silence from turning into a request per cycle.
            if file_age_minutes >= calendar_min_gap_minutes:
                trigger = RefreshTrigger.BEHIND_CALENDAR

    if trigger is RefreshTrigger.NONE:
        if not have_usable_cache:
            return (
                pd.DataFrame(),
                build_provenance(instrument.symbol, path, None,
                                 RefreshOutcome.REFRESH_FAILED_NO_CACHE,
                                 instrument.asset_class, None,
                                 "cache missing or too short and refresh not requested"),
            )
        return cached, build_provenance(
            instrument.symbol, path, cached, RefreshOutcome.CACHE_HIT,
            instrument.asset_class, None, today=today, trigger=trigger.value,
            provider=cached_provider,
        )

    try:
        fetched = _fetch(instrument, start, end)
        # Captured before validate_frame, which may copy and drop frame attrs.
        source = fetched.attrs.get("provider", PROVIDER)
        validated = validate_frame(fetched, REQUIRED_COLUMNS, MIN_HISTORY_ROWS)
    except ValidationError as exc:
        outcome = (RefreshOutcome.REJECTED_INVALID if have_usable_cache
                   else RefreshOutcome.REFRESH_FAILED_NO_CACHE)
        return (cached if have_usable_cache else pd.DataFrame()), build_provenance(
            instrument.symbol, path, cached, outcome, instrument.asset_class, now,
            f"rejected: {exc}", today, trigger.value, provider=cached_provider,
        )
    except Exception as exc:
        outcome = (RefreshOutcome.REFRESH_FAILED_STALE_CACHE if have_usable_cache
                   else RefreshOutcome.REFRESH_FAILED_NO_CACHE)
        return (cached if have_usable_cache else pd.DataFrame()), build_provenance(
            instrument.symbol, path, cached, outcome, instrument.asset_class, now,
            f"download failed: {exc}", today, trigger.value, provider=cached_provider,
        )

    combined = validated
    if have_usable_cache:
        merged = pd.concat([cached, validated])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        try:
            combined = validate_frame(merged, REQUIRED_COLUMNS, MIN_HISTORY_ROWS)
        except ValidationError as exc:
            return cached, build_provenance(
                instrument.symbol, path, cached, RefreshOutcome.REJECTED_INVALID,
                instrument.asset_class, now, f"rejected merge: {exc}", today, trigger.value,
                provider=cached_provider,
            )

    # Inside the same guard as the download: a write that cannot complete must
    # degrade this one symbol to a stale cache, not propagate and take the whole
    # universe down with it. The previous file is untouched either way.
    try:
        atomic_write_csv(path, combined)
    except Exception as exc:
        outcome = (RefreshOutcome.REFRESH_FAILED_STALE_CACHE if have_usable_cache
                   else RefreshOutcome.REFRESH_FAILED_NO_CACHE)
        return (cached if have_usable_cache else pd.DataFrame()), build_provenance(
            instrument.symbol, path, cached, outcome, instrument.asset_class, now,
            f"cache write failed: {exc}", today, trigger.value, provider=cached_provider,
        )

    result = combined[combined.index >= pd.Timestamp(start)]

    # The sidecar is advisory. Losing it must not discard a good cache, and the
    # record it holds is always re-derived from the frame on the next read, so a
    # failure here cannot make the data look fresher than it is.
    try:
        write_sidecar(path, Sidecar(
            symbol=instrument.symbol, provider=source,
            fetched_at=now.isoformat(timespec="seconds"),
            last_candle=str(combined.index[-1].date()), rows=len(combined),
            outcome=RefreshOutcome.REFRESHED.value, detail=trigger.value,
        ))
    except OSError:
        # Advisory record only. Provenance is re-derived from the frame on every
        # read, so a lost sidecar can never make stale data look fetched.
        pass

    return result, build_provenance(
        instrument.symbol, path, result, RefreshOutcome.REFRESHED,
        instrument.asset_class, now, today=today, trigger=trigger.value,
        provider=source,
    )


def refresh_all(
    instruments: list[Instrument],
    start: str = "2018-01-01",
    end: str | None = None,
    today: date | None = None,
) -> dict[str, Provenance]:
    """Refresh every instrument once and report each outcome independently."""
    return {
        i.symbol: load_with_provenance(i, start=start, end=end, refresh=True, today=today)[1]
        for i in instruments
    }


def split_periods(
    data: pd.DataFrame, dev_end: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological split: everything before dev_end is in-sample."""
    cutoff = pd.Timestamp(dev_end)
    dev = data[data.index < cutoff]
    test = data[data.index >= cutoff]
    return dev, test