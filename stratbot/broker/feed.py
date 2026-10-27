"""Live bar feed for the paper engine.

Yahoo's chart endpoint is free but delayed and rate-limited. It is adequate for
a paper run and explicitly not adequate for live trading, which is the whole
reason Phase 4 is gated.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from ..models import Bar
from stratlab.calendar import behind_calendar, drop_unfinished

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


class MarketDataFeed:
    """Cache-aware bar feed that reports provenance and can refresh explicitly.

    The engine trades only on symbols this feed marks usable. A symbol whose
    refresh failed is reported and skipped rather than silently served as a
    current price.

    Two views of the same series, because a forming candle is partly fact:

    * ``bars()`` serves the newest available bar, forming or not. Its **open** is
      a real traded price from the moment the session began, so it is a legitimate
      execution price for an order decided on an earlier bar.
    * ``history()`` and ``closed_bars()`` drop a still-forming bar entirely. Its
      high, low, close and volume are still being revised, so a strategy must
      never see them.

    Mixing the two up is how a strategy ends up deciding on information that did
    not exist at the time the decision was taken.
    """

    def __init__(self, symbols, refresh_interval_minutes: int = 60,
                 refresh_on_start: bool = True, start: str = "2018-01-01",
                 include_unfinished: bool = False,
                 max_calendar_retries: int = 3,
                 interval: str = "1d",
                 days_history: int = 180):
        from stratlab import data as price_data

        self.price_data = price_data
        self.symbols = tuple(symbols)
        self.refresh_interval_minutes = refresh_interval_minutes
        self.refresh_on_start = refresh_on_start
        # Intraday framing: a short warm window replaces the multi-year daily
        # window; bars are fetched at `interval` seconds-apart candles.
        self.interval = interval
        self.days_history = days_history
        if interval == "1d":
            self.start = start
        else:
            from datetime import timedelta

            self.start = (datetime.now(timezone.utc) - timedelta(days=days_history)) \
                .strftime("%Y-%m-%d")
        self.include_unfinished = include_unfinished
        # Caps how often a session the calendar expects but the provider has not
        # published can be retried. Without it, an exchange holiday would mean a
        # download attempt on every single cycle.
        self.max_calendar_retries = max_calendar_retries
        self.provenance: dict = {}
        self.closed: dict = {}
        self.deferred: dict = {}
        self._behind: dict = {}
        self._last_candle: dict = {}
        # Default to the shared logger rather than None: the server builds this
        # feed internally, so a caller that forgets to attach a logger would
        # otherwise silence every provenance line exactly when it is needed most.
        self.logger = logging.getLogger("stratbot")

    def instruments(self) -> list:
        return [self.price_data.BY_SYMBOL[s] for s in self.symbols
                if s in self.price_data.BY_SYMBOL]

    def _strip_forming(self, instrument, frame):
        """Drop the still-forming bar and record which one it was."""
        if self.include_unfinished or frame is None or frame.empty:
            return frame
        if self.interval == "1d":
            newest = pd.Timestamp(frame.index[-1]).date()
        else:
            newest = pd.Timestamp(frame.index[-1]).to_pydatetime()
        trimmed = drop_unfinished(frame, instrument.asset_class,
                                  interval=self.interval)
        if len(trimmed) != len(frame):
            self.deferred[instrument.symbol] = str(newest)
        else:
            self.deferred.pop(instrument.symbol, None)
        return trimmed

    def _calendar_gap(self, instrument) -> float:
        """Minutes to allow before the behind-calendar trigger fires again.

        Decided from what the provider last published, before this cycle's fetch.
        Once the retry budget for a still-missing session is spent the trigger is
        suppressed until the normal interval elapses, which is what stops an
        unmodelled exchange holiday from becoming a request every cycle.
        """
        if self.interval != "1d":
            # Intraday refresh pacing is handled inside load_with_provenance
            # against the session calendar, using timestamps not dates.
            return 0.0
        prior = self._last_candle.get(instrument.symbol)
        if not prior or prior == "none":
            return 0.0
        try:
            behind = behind_calendar(
                date.fromisoformat(prior), datetime.now(timezone.utc).date(),
                instrument.asset_class,
            )
        except ValueError:
            return 0.0
        if not behind:
            self._behind.pop(instrument.symbol, None)
            return 0.0
        self._behind[instrument.symbol] = self._behind.get(instrument.symbol, 0) + 1
        if self._behind[instrument.symbol] >= self.max_calendar_retries:
            self.logger.info(
                "market_data: %s expected session still missing after %d checks, "
                "falling back to the refresh interval",
                instrument.symbol, self._behind[instrument.symbol],
            )
            return float("inf")
        return 0.0

    def _load(self, instrument, force_refresh: bool):
        interval = None if force_refresh else self.refresh_interval_minutes
        frame, prov = self.price_data.load_with_provenance(
            instrument, start=self.start, refresh=force_refresh,
            max_cache_age_minutes=interval,
            calendar_min_gap_minutes=(0.0 if force_refresh
                                      else self._calendar_gap(instrument)),
            interval=self.interval,
        )
        self.provenance[instrument.symbol] = prov
        self._last_candle[instrument.symbol] = prov.last_candle
        return frame, prov

    @staticmethod
    def _to_bar(symbol: str, frame) -> Bar:
        last = frame.iloc[-1]
        return Bar(
            symbol=symbol,
            ts=pd.Timestamp(frame.index[-1]).to_pydatetime(),
            open=float(last["open"]), high=float(last["high"]),
            low=float(last["low"]), close=float(last["close"]),
            volume=float(last.get("volume", 0.0) or 0.0),
        )

    def bars(self, force_refresh: bool = False) -> dict[str, Bar]:
        """Newest bar per symbol, forming ones included.

        The forming bar's open is a real price and may execute a pending order;
        its close is provisional and must not be read as a decision input.
        """
        from stratlab.provenance import CacheStatus

        out: dict[str, Bar] = {}
        self.provenance = {}

        for instrument in self.instruments():
            frame, prov = self._load(instrument, force_refresh)

            if not frame.empty:
                self._strip_forming(instrument, frame)  # only to update `deferred`
            self._log(prov)

            if frame.empty:
                continue
            # A stale or absent symbol must not be presented as a current price.
            if prov.status in (CacheStatus.STALE.value, CacheStatus.ABSENT.value):
                continue

            out[instrument.symbol] = self._to_bar(instrument.symbol, frame)
        return out

    def closed_bars(self) -> dict[str, Bar]:
        """Newest *finished* bar per symbol. This is what a decision is made on."""
        out: dict[str, Bar] = {}
        self.closed = {}
        for instrument in self.instruments():
            frame = self._strip_forming(instrument, self._history_frame(instrument))
            if frame is None or frame.empty:
                continue
            self.closed[instrument.symbol] = self._to_bar(instrument.symbol, frame)
            out[instrument.symbol] = self.closed[instrument.symbol]
        return out

    def _history_frame(self, instrument):
        frame, prov = self.price_data.load_with_provenance(
            instrument, start=self.start, refresh=False,
            max_cache_age_minutes=self.refresh_interval_minutes,
            interval=self.interval,
        )
        self.provenance[instrument.symbol] = prov
        self._last_candle[instrument.symbol] = prov.last_candle
        return frame

    def history(self, symbol: str):
        """Series a strategy may read: the forming bar is removed."""
        instrument = self.price_data.BY_SYMBOL.get(symbol)
        if instrument is None:
            raise KeyError(symbol)
        return self._strip_forming(instrument, self._history_frame(instrument))

    def _log(self, prov) -> None:
        deferred = self.deferred.get(prov.symbol)
        note = f" | {deferred} still forming: open tradable, close withheld" if deferred else ""
        self.logger.info("market_data: %s%s", prov.log_line(), note)