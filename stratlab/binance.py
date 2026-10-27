"""Binance spot daily klines, used as a fallback for crypto when Yahoo fails.

Yahoo stays the primary source for every instrument. Crypto is the one asset
class here that also has a second venue reachable with no account and no API
key, so a Yahoo outage - or a false "possibly delisted" on BTC-USD, which is
what actually happened - no longer takes the crypto half of the book offline.

Equities deliberately keep a single provider. There is no comparable key-free
NSE source; a scrape of nseindia.com would trade a working feed for one that
breaks on holidays, throttling and markup changes. Adding a paid equity source
is a separate decision, not something to smuggle in behind a fallback.

Uses only the standard library on purpose: this is a fallback, and a fallback
that adds a dependency to the primary path is not a fallback.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

BINANCE_PROVIDER = "binance.klines"
TICKER_PROVIDER = "binance.ticker"
_BASE_URL = "https://api.binance.com/api/v3/klines"
_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
# Binance caps a klines call at 1000 rows; a multi-year history needs paging.
_MAX_LIMIT = 1000

_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"}
# The app names intervals the Yahoo way; Binance spells hourly bars "1h".
_YAHOO_TO_BINANCE = {"60m": "1h", "90m": "1h", "1h": "1h"}


def binance_pair(symbol: str) -> str:
    """BTC -> BTCUSDT. Binance quotes spot against USDT."""
    return f"{symbol.upper()}USDT"


def ticker_price(symbol: str, timeout: float = 10.0) -> float:
    """The current spot print for one crypto symbol.

    The klines are a close; this is the live price. No account, no API key.
    """
    pair = binance_pair(symbol)
    query = urllib.parse.urlencode({"symbol": pair})
    request = urllib.request.Request(
        f"{_TICKER_URL}?{query}",
        headers={"User-Agent": "stratbot/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict) and "code" in payload:
        raise RuntimeError(f"{payload.get('code')}: {payload.get('msg')}")
    if not isinstance(payload, dict) or "price" not in payload:
        raise RuntimeError(f"unexpected ticker payload: {payload}")
    return float(payload["price"])


def _request(params: dict, timeout: float) -> list:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{_BASE_URL}?{query}",
        headers={"User-Agent": "stratbot/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    # Binance reports application errors as a JSON object with an error code.
    if isinstance(payload, dict):
        raise RuntimeError(f"{payload.get('code')}: {payload.get('msg')}")
    return payload


def daily_klines(symbol: str, start: str, end: str, timeout: float = 15.0) -> pd.DataFrame:
    """Daily bars for one crypto symbol, indexed by UTC date.

    Raises rather than returning a partial frame: the caller treats a failed
    download as "keep the existing cache", so a short frame would be worse than
    an exception.
    """
    frame = klines(symbol, "1d", start, end, timeout=timeout)
    frame.index = frame.index.normalize()
    return frame


def klines(symbol: str, interval: str, start: str, end: str,
           timeout: float = 15.0) -> pd.DataFrame:
    """Intraday or daily bars for one crypto symbol, naïve-UTC timestamps.

    A UTC-day interval keeps the whole timestamp (it is already a midnight);
    daily_klines is the one that normalises to dates.
    """
    if interval not in _INTERVALS:
        interval = _YAHOO_TO_BINANCE.get(interval, "1d")
    pair = binance_pair(symbol)
    start_ms = int(pd.Timestamp(start).tz_localize("UTC").timestamp() * 1000)
    # yfinance treats `end` as exclusive; Binance treats endTime as inclusive.
    end_ms = int(pd.Timestamp(end).tz_localize("UTC").timestamp() * 1000) - 1

    rows: list = []
    cursor = start_ms
    while cursor < end_ms:
        page = _request({
            "symbol": pair,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": _MAX_LIMIT,
        }, timeout)
        if not page:
            break
        rows.extend(page)
        last_open = page[-1][0]
        if last_open + 1 <= cursor:
            break           # no forward progress; refuse to spin on the same bar
        cursor = last_open + 1
        if len(page) < _MAX_LIMIT:
            break           # short page means the range is exhausted

    if not rows:
        raise RuntimeError(f"no klines returned for {pair} between {start} and {end}")

    frame = pd.DataFrame({
        "open": [float(r[1]) for r in rows],
        "high": [float(r[2]) for r in rows],
        "low": [float(r[3]) for r in rows],
        "close": [float(r[4]) for r in rows],
        "volume": [float(r[5]) for r in rows],
    }, index=pd.to_datetime([r[0] for r in rows], unit="ms").tz_localize(None))
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame.index.name = "date"
    return frame
