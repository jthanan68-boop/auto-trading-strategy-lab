"""Crypto second-venue fallback.

Yahoo is the primary source for every instrument; Binance spot is a fallback for
crypto only. Every test here simulates both venues - no HTTP request is made and
no real market data is involved. The frames below are fixtures, not prices.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stratlab import binance
from stratlab import data as price_data
from stratlab.provenance import RefreshOutcome, read_sidecar

BTC = price_data.BY_SYMBOL["BTC"]
RELIANCE = price_data.BY_SYMBOL["RELIANCE"]


def make_frame(rows: int = 60, start: str = "2024-01-01") -> pd.DataFrame:
    index = pd.date_range(start=start, periods=rows, freq="D")
    closes = np.linspace(30000.0, 40000.0, rows)
    return pd.DataFrame(
        {"open": closes, "high": closes * 1.01, "low": closes * 0.99,
         "close": closes, "volume": 10.0},
        index=index,
    )


def yahoo_down(monkeypatch, message: str = "simulated yahoo outage"):
    def fail(*args, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr("yfinance.download", fail)


def binance_serves(monkeypatch, frame: pd.DataFrame | None, calls: list | None = None):
    def fake(symbol, start, end, timeout=15.0):
        if calls is not None:
            calls.append((symbol, start, end))
        if frame is None:
            raise RuntimeError("simulated binance outage")
        return frame.copy()

    monkeypatch.setattr(binance, "daily_klines", fake)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    target = tmp_path / "cache"
    target.mkdir()
    monkeypatch.setattr(price_data, "CACHE_DIR", target)
    return target


class TestPairMapping:
    def test_crypto_quotes_against_usdt(self):
        assert binance.binance_pair("BTC") == "BTCUSDT"
        assert binance.binance_pair("eth") == "ETHUSDT"


class TestFallback:
    def test_crypto_is_served_by_binance_when_yahoo_fails(self, monkeypatch):
        yahoo_down(monkeypatch)
        binance_serves(monkeypatch, make_frame())

        frame = price_data._fetch(BTC, "2024-01-01", "2024-03-01", retries=1)

        assert list(frame.columns)[:4] == ["open", "high", "low", "close"]
        assert frame["volume"].iloc[0] == 10.0   # venue volume rides through
        assert frame.attrs["provider"] == binance.BINANCE_PROVIDER
        assert len(frame) == 60

    def test_equity_never_asks_binance(self, monkeypatch):
        yahoo_down(monkeypatch)

        def forbidden(*args, **kwargs):
            raise AssertionError("equity must not be routed to a crypto venue")

        monkeypatch.setattr(binance, "daily_klines", forbidden)

        with pytest.raises(RuntimeError) as err:
            price_data._fetch(RELIANCE, "2024-01-01", "2024-03-01", retries=1)

        assert "RELIANCE" in str(err.value)
        assert "simulated yahoo outage" in str(err.value)

    def test_both_venues_failing_names_both(self, monkeypatch):
        yahoo_down(monkeypatch)
        binance_serves(monkeypatch, None)

        with pytest.raises(RuntimeError) as err:
            price_data._fetch(BTC, "2024-01-01", "2024-03-01", retries=1)

        message = str(err.value)
        assert "yahoo failed" in message
        assert "simulated yahoo outage" in message
        assert "binance failed" in message
        assert "simulated binance outage" in message

    def test_a_binance_frame_is_held_to_the_same_validation(self, monkeypatch):
        yahoo_down(monkeypatch)
        binance_serves(monkeypatch, make_frame(rows=5))

        frame = price_data._fetch(BTC, "2024-01-01", "2024-03-01", retries=1)

        # _fetch does not validate; the caller does. A short frame must still reach
        # validate_frame rather than being silently stretched.
        assert len(frame) == 5


class TestProvenance:
    def test_the_venue_that_served_the_bars_is_recorded(self, cache_dir, monkeypatch):
        yahoo_down(monkeypatch)
        binance_serves(monkeypatch, make_frame())

        frame, prov = price_data.load_with_provenance(
            BTC, start="2024-01-01", refresh=True, today=pd.Timestamp("2024-02-29").date()
        )

        assert not frame.empty
        assert prov.outcome == RefreshOutcome.REFRESHED.value
        assert prov.provider == binance.BINANCE_PROVIDER, prov.provider
        assert prov.status != "absent"

    def test_the_sidecar_records_the_same_venue(self, cache_dir, monkeypatch):
        yahoo_down(monkeypatch)
        binance_serves(monkeypatch, make_frame())

        price_data.load_with_provenance(
            BTC, start="2024-01-01", refresh=True, today=pd.Timestamp("2024-02-29").date()
        )

        record = read_sidecar(cache_dir / "BTC.csv")
        assert record is not None
        assert record["provider"] == binance.BINANCE_PROVIDER

    def test_a_cache_hit_on_binance_data_still_reports_binance(self, cache_dir, monkeypatch):
        yahoo_down(monkeypatch)
        binance_serves(monkeypatch, make_frame())
        price_data.load_with_provenance(
            BTC, start="2024-01-01", refresh=True, today=pd.Timestamp("2024-02-29").date()
        )

        cached, prov = price_data.load_with_provenance(
            BTC, start="2024-01-01", refresh=False, today=pd.Timestamp("2024-02-29").date()
        )

        assert not cached.empty
        assert prov.outcome == RefreshOutcome.CACHE_HIT.value
        assert prov.provider == binance.BINANCE_PROVIDER, prov.provider

    def test_a_yahoo_served_cache_still_reports_yahoo(self, cache_dir, monkeypatch):
        def fake_fetch(instrument, start, end, retries=3):
            return make_frame()

        monkeypatch.setattr(price_data, "_fetch", fake_fetch)
        price_data.load_with_provenance(
            BTC, start="2024-01-01", refresh=True, today=pd.Timestamp("2024-02-29").date()
        )

        record = read_sidecar(cache_dir / "BTC.csv")
        assert record["provider"] == "yfinance.download"


class TestTicker:
    """The live ticker, which is a different payload from the klines."""

    class _Response:
        def __init__(self, payload: str):
            self.status = 200
            self._payload = payload

        def read(self) -> bytes:
            return self._payload.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _serve(self, monkeypatch, payload: str):
        def fake_urlopen(request, timeout=None):
            return TestTicker._Response(payload)

        monkeypatch.setattr(binance.urllib.request, "urlopen", fake_urlopen)

    def test_a_ticker_payload_becomes_a_price(self, monkeypatch):
        self._serve(monkeypatch, '{"symbol":"BTCUSDT","price":"83444.00000000"}')

        assert binance.ticker_price("BTC") == pytest.approx(83444.0)

    def test_an_error_payload_is_an_error_not_a_price(self, monkeypatch):
        self._serve(monkeypatch, '{"code":-1121,"msg":"Invalid symbol."}')

        with pytest.raises(RuntimeError, match="Invalid symbol"):
            binance.ticker_price("NOPE")


class TestKlinesPaging:
    def test_a_long_range_is_paged_until_exhausted(self, monkeypatch):
        calls = []

        def fake_request(params, timeout):
            calls.append(params)
            # Binance returns candles whose openTime is >= startTime, on day
            # boundaries, so the server's own paging never repeats a bar.
            day_ms = 86_400_000
            first = -(-params["startTime"] // day_ms) * day_ms
            span = min(binance._MAX_LIMIT, 2500 - (len(calls) - 1) * binance._MAX_LIMIT)
            return [
                [first + i * day_ms, "1", "2", "0.5", "1.5", "10",
                 0, "0", 0, "0", "0", "0"]
                for i in range(max(span, 0))
            ]

        monkeypatch.setattr(binance, "_request", fake_request)

        frame = binance.daily_klines("BTC", "2017-01-01", "2024-01-01")

        assert len(calls) == 3, f"expected three pages, made {len(calls)}"
        assert not frame.index.duplicated().any()
        assert frame.index.is_monotonic_increasing
        assert list(frame.columns) == ["open", "high", "low", "close", "volume"]

    def test_an_empty_page_is_an_error_not_an_empty_frame(self, monkeypatch):
        monkeypatch.setattr(binance, "_request", lambda params, timeout: [])

        with pytest.raises(RuntimeError, match="no klines"):
            binance.daily_klines("BTC", "2024-01-01", "2024-03-01")
