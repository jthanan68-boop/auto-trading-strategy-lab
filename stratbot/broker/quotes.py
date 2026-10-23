"""Live quotes for the paper book.

The engine decides on completed daily bars and fills at the next bar's open.
That is the evidence base and it does not change. What changes here is the mark:
the price an open position is valued at, and the price the dashboard shows.

One source per asset class, because they are not the same kind of price:

* crypto - Binance's spot ticker. Free, no account, no key, and genuinely live.
* equities - Yahoo's one-minute chart. Free, no key, and delayed by roughly a
  quarter of an hour for NSE names. There is no key-free live NSE source, so a
  delayed price is the honest best available and is labelled as such.

Every quote is optional. A provider that fails, is slow, or returns a price too
old to trust leaves the daily bar in charge, exactly as it was before.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from stratlab import binance

from .feed import YAHOO_CHART

logger = logging.getLogger("stratbot")

CRYPTO_SOURCE = "binance.ticker"
EQUITY_SOURCE = "yahoo.intraday"

# How old a quote may be before it is refused. A crypto print is seconds old,
# so two minutes is already a broken feed. Yahoo's NSE data runs a quarter of
# an hour behind, so an hour is still a live session rather than a stale close.
CRYPTO_MAX_AGE_SECONDS = 120.0
EQUITY_MAX_AGE_SECONDS = 3600.0


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    ts: datetime
    source: str
    delayed: bool
    max_age_seconds: float

    @property
    def age_seconds(self) -> float:
        now = datetime.now(timezone.utc)
        ts = self.ts if self.ts.tzinfo else self.ts.replace(tzinfo=timezone.utc)
        return max(0.0, (now - ts).total_seconds())

    @property
    def usable(self) -> bool:
        """Is this price fresh enough to value a position with?"""
        return self.price > 0 and self.age_seconds <= self.max_age_seconds


def yahoo_intraday_last(yahoo_symbol: str, timeout: float = 8.0) -> tuple[float, datetime]:
    """The newest one-minute close Yahoo publishes for a symbol, and when it printed.

    NSE names arrive here about fifteen minutes late. The price is real, it is
    just late, and the quote says so instead of pretending otherwise.
    """
    response = requests.get(
        YAHOO_CHART.format(symbol=yahoo_symbol),
        params={"range": "1d", "interval": "1m"},
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    payload = response.json()["chart"]["result"][0]
    quote = payload["indicators"]["quote"][0]
    closes = quote.get("close") or []
    stamps = payload.get("timestamp") or []
    for stamp, close in zip(reversed(stamps), reversed(closes)):
        if close is None:
            continue
        return float(close), datetime.fromtimestamp(int(stamp), tz=timezone.utc)
    raise RuntimeError(f"{yahoo_symbol}: no intraday print in the response")


class QuoteService:
    """Live quotes with a short cache.

    The dashboard polls every few seconds, so without a cache every poll would
    be a burst of provider requests. Each symbol is fetched on its own: one
    provider failing must not cost the others their quote.
    """

    def __init__(self, instruments, crypto_ttl: float = 10.0, equity_ttl: float = 60.0,
                 timeout: float = 8.0):
        self.instruments = {i.symbol: i for i in instruments}
        self.crypto_ttl = crypto_ttl
        self.equity_ttl = equity_ttl
        self.timeout = timeout
        self._cache: dict[str, Quote] = {}
        self._fetched: dict[str, float] = {}
        self._locks = {symbol: threading.Lock() for symbol in self.instruments}

    def quotes(self, force: bool = False) -> dict[str, Quote]:
        symbols = list(self.instruments)
        if not symbols:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
            found = list(pool.map(lambda symbol: self._quote_for(symbol, force), symbols))
        return {s: q for s, q in zip(symbols, found) if q is not None}

    def _quote_for(self, symbol: str, force: bool) -> Quote | None:
        instrument = self.instruments[symbol]
        ttl = self.crypto_ttl if instrument.is_crypto else self.equity_ttl
        with self._locks[symbol]:
            cached = self._cache.get(symbol)
            if cached is not None and not force:
                if time.monotonic() - self._fetched.get(symbol, 0.0) < ttl:
                    return cached
            try:
                quote = self._fetch(symbol, instrument)
            except Exception as exc:
                logger.warning("live quote unavailable for %s: %s", symbol, exc)
                return None
            self._cache[symbol] = quote
            self._fetched[symbol] = time.monotonic()
            return quote

    def _fetch(self, symbol: str, instrument) -> Quote:
        if instrument.is_crypto:
            price = binance.ticker_price(symbol, timeout=self.timeout)
            return Quote(symbol=symbol, price=price, ts=datetime.now(timezone.utc),
                         source=CRYPTO_SOURCE, delayed=False,
                         max_age_seconds=CRYPTO_MAX_AGE_SECONDS)
        price, ts = yahoo_intraday_last(instrument.yfinance_symbol, timeout=self.timeout)
        return Quote(symbol=symbol, price=price, ts=ts, source=EQUITY_SOURCE,
                     delayed=True, max_age_seconds=EQUITY_MAX_AGE_SECONDS)
