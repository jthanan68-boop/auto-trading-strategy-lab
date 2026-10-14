"""Market data loading with an on-disk cache.

Yahoo Finance is the free source; it covers NSE-listed equities as SYMBOL.NS,
Indian indices as ^NSEI/^BSESN, and crypto as SYMBOL-USD. Everything is cached to
CSV so repeated runs are offline and free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

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

BY_SYMBOL = {i.symbol: i for i in UNIVERSE}


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
    raise RuntimeError(
        f"{instrument.symbol} ({instrument.yfinance_symbol}): {last_error}"
    ) from last_error


def load(
    instrument: Instrument,
    start: str = "2018-01-01",
    end: str | None = None,
    refresh: bool = False,
) -> pd.DataFrame:
    """Return cached daily OHLC, fetching from Yahoo on first use."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(instrument)
    end = end or (pd.Timestamp.today().normalize() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    if path.exists() and not refresh:
        cached = pd.read_csv(path, index_col="date", parse_dates=["date"])
        requested_start = pd.Timestamp(start)
        cached = cached[cached.index >= requested_start]
        if len(cached) > 30:
            return cached
        frame = _fetch(instrument, start, end)
    else:
        frame = _fetch(instrument, start, end)

    combined = frame
    if path.exists():
        previous = pd.read_csv(path, index_col="date", parse_dates=["date"])
        combined = pd.concat([previous, frame])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()

    combined.to_csv(path)
    return combined[combined.index >= pd.Timestamp(start)]


def split_periods(
    data: pd.DataFrame, dev_end: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological split: everything before dev_end is in-sample."""
    cutoff = pd.Timestamp(dev_end)
    dev = data[data.index < cutoff]
    test = data[data.index >= cutoff]
    return dev, test