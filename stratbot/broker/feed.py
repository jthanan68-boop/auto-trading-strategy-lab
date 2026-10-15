"""Live bar feed for the paper engine.

Yahoo's chart endpoint is free but delayed and rate-limited. It is adequate for
a paper run and explicitly not adequate for live trading, which is the whole
reason Phase 4 is gated.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..models import Bar

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SPACING = 2.0


def _to_bar(symbol: str, index, quote: dict) -> Bar | None:
    try:
        close = float(quote["close"][0])
        if close != close:  # NaN check that survives numpy semantics
            return None
        ts = pd.Timestamp(int(index[0]), unit="s", tz="UTC")
        return Bar(
            symbol=symbol,
            ts=ts.to_pydatetime(),
            open=float(quote["open"][0]),
            high=float(quote["high"][0]),
            low=float(quote["low"][0]),
            close=close,
            volume=float(quote["volume"][0] or 0.0),
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def fetch_bar(yahoo_symbol: str, symbol: str, retries: int = 3) -> Bar:
    """Latest daily bar, or raise if the feed cannot be trusted."""
    import requests

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(
                YAHOO_CHART.format(symbol=yahoo_symbol),
                params={"range": "5d", "interval": "1d"},
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            payload = response.json()["chart"]["result"][0]
            quote = payload["indicators"]["quote"][0]
            bar = _to_bar(symbol, payload["timestamp"], quote)
            if bar is None:
                raise ValueError("no usable bar in response")
            return bar
        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(YAHOO_SPACING * (attempt + 1))
    raise RuntimeError(f"{symbol}: feed unavailable ({last_error})")


def fetch_bars(mapping: dict[str, str], retries: int = 3) -> dict[str, Bar]:
    """Fetch several symbols, skipping rather than failing the whole cycle."""
    out: dict[str, Bar] = {}
    for symbol, yahoo_symbol in mapping.items():
        try:
            out[symbol] = fetch_bar(yahoo_symbol, symbol, retries=retries)
        except Exception:
            continue
    return out


def current_bars(symbols) -> dict[str, Bar]:
    """Latest bar per symbol, taken from the same series the strategy reads.

    The chart endpoint and the download endpoint disagree by several days for NSE
    names, and the chart copy is the staler of the two. Mixing them let the risk
    layer halt on data the strategy itself considered current, so both now come
    from the cached history.
    """
    from stratlab import data as price_data

    out: dict[str, Bar] = {}
    for symbol in symbols:
        instrument = price_data.BY_SYMBOL.get(symbol)
        if instrument is None:
            continue
        try:
            frame = price_data.load(instrument)
        except Exception:
            continue
        if frame.empty:
            continue
        last = frame.iloc[-1]
        out[symbol] = Bar(
            symbol=symbol,
            ts=pd.Timestamp(frame.index[-1]).to_pydatetime(),
            open=float(last["open"]),
            high=float(last["high"]),
            low=float(last["low"]),
            close=float(last["close"]),
            volume=float(last.get("volume", 0.0) or 0.0),
        )
    return out