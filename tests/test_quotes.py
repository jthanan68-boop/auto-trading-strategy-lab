"""Live quotes: where the price comes from, and what happens when it cannot.

No HTTP request is made and no real market data is involved. The prices below
are fixtures.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from stratbot.broker.quotes import (
    CRYPTO_SOURCE,
    EQUITY_SOURCE,
    Quote,
    QuoteService,
)
from stratbot.config import BotConfig
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import Bar, OrderRequest, Side, utcnow
from stratlab import binance
from stratlab import data as price_data

BTC = price_data.BY_SYMBOL["BTC"]
RELIANCE = price_data.BY_SYMBOL["RELIANCE"]


def make_quote(symbol: str, price: float, age_seconds: float = 0.0,
               source: str = CRYPTO_SOURCE, delayed: bool = False,
               max_age: float = 120.0) -> Quote:
    return Quote(
        symbol=symbol,
        price=price,
        ts=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        source=source,
        delayed=delayed,
        max_age_seconds=max_age,
    )


class FakeQuotes:
    """A quote service that answers from memory."""

    def __init__(self, quotes: dict[str, Quote] | None = None, error: Exception | None = None):
        self._quotes = quotes or {}
        self._error = error
        self.calls = 0

    def quotes(self, force: bool = False) -> dict[str, Quote]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return dict(self._quotes)


def history_frame(rows: int = 60) -> pd.DataFrame:
    index = pd.date_range(start="2024-01-01", periods=rows, freq="D")
    closes = np.linspace(30000.0, 40000.0, rows)
    return pd.DataFrame(
        {"open": closes, "high": closes * 1.01, "low": closes * 0.99,
         "close": closes, "volume": 10.0},
        index=index,
    )


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "quotes.sqlite3")


class TestSources:
    def test_crypto_uses_the_binance_ticker(self, monkeypatch):
        monkeypatch.setattr(binance, "ticker_price", lambda symbol, timeout=10.0: 61234.5)

        service = QuoteService([BTC])
        quotes = service.quotes()

        assert quotes["BTC"].price == pytest.approx(61234.5)
        assert quotes["BTC"].source == CRYPTO_SOURCE
        assert quotes["BTC"].delayed is False

    def test_an_aged_equity_print_is_flagged_delayed_and_refused(self, monkeypatch):
        stamp = datetime.now(timezone.utc) - timedelta(minutes=14)
        monkeypatch.setattr(
            "stratbot.broker.quotes.yahoo_intraday_last",
            lambda symbol, timeout=8.0: (1211.75, stamp),
        )

        service = QuoteService([RELIANCE])
        quotes = service.quotes()

        assert quotes["RELIANCE"].price == pytest.approx(1211.75)
        assert quotes["RELIANCE"].source == EQUITY_SOURCE
        assert quotes["RELIANCE"].delayed is True
        assert quotes["RELIANCE"].usable is False

    def test_a_fresh_equity_print_is_not_flagged_delayed(self, monkeypatch):
        stamp = datetime.now(timezone.utc) - timedelta(seconds=10)
        monkeypatch.setattr(
            "stratbot.broker.quotes.yahoo_intraday_last",
            lambda symbol, timeout=8.0: (1211.75, stamp),
        )

        quote = QuoteService([RELIANCE]).quotes()["RELIANCE"]

        assert quote.delayed is False
        assert quote.usable is True

    def test_a_publisher_stamp_in_the_future_is_refused(self, monkeypatch):
        from stratbot.broker import quotes as quotes_module

        future = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"chart": {"result": [{
                    "timestamp": [future],
                    "indicators": {"quote": [{"close": [100.0]}]},
                }]}}

        monkeypatch.setattr(quotes_module.requests, "get",
                            lambda *args, **kwargs: FakeResponse())

        with pytest.raises(RuntimeError, match="future"):
            quotes_module.yahoo_intraday_last("RELIANCE.NS")

    def test_an_equity_is_never_routed_to_a_crypto_venue(self, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError("an equity must not be priced by a crypto venue")

        monkeypatch.setattr(binance, "ticker_price", forbidden)
        monkeypatch.setattr(
            "stratbot.broker.quotes.yahoo_intraday_last",
            lambda symbol, timeout=8.0: (1211.75, datetime.now(timezone.utc)),
        )

        quotes = QuoteService([RELIANCE]).quotes()

        assert quotes["RELIANCE"].source == EQUITY_SOURCE


class TestFailure:
    def test_one_failing_provider_does_not_cost_the_others_their_quote(self, monkeypatch):
        def only_btc(symbol, timeout=10.0):
            if symbol != "BTC":
                raise RuntimeError("simulated outage")
            return 61234.5

        def yahoo_down(symbol, timeout=8.0):
            raise RuntimeError("simulated outage")

        monkeypatch.setattr(binance, "ticker_price", only_btc)
        monkeypatch.setattr("stratbot.broker.quotes.yahoo_intraday_last", yahoo_down)

        quotes = QuoteService([BTC, RELIANCE]).quotes()

        assert "BTC" in quotes
        assert "RELIANCE" not in quotes

    def test_a_failed_fetch_leaves_the_previous_quote_out_of_use(self, monkeypatch):
        calls = {"n": 0}

        def flaky(symbol, timeout=10.0):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("simulated outage")
            return 61234.5

        monkeypatch.setattr(binance, "ticker_price", flaky)
        service = QuoteService([BTC])

        assert service.quotes()["BTC"].price == pytest.approx(61234.5)
        assert service.quotes(force=True) == {}


class TestFreshness:
    def test_a_quote_older_than_its_max_age_is_not_usable(self):
        stale = make_quote("BTC", 61234.5, age_seconds=600, max_age=120.0)
        fresh = make_quote("BTC", 61234.5, age_seconds=5, max_age=120.0)

        assert stale.usable is False
        assert fresh.usable is True

    def test_a_delayed_equity_quote_is_still_usable_within_its_own_window(self):
        quote = make_quote("RELIANCE", 1211.75, age_seconds=14 * 60,
                           source=EQUITY_SOURCE, delayed=True, max_age=3600.0)

        assert quote.usable is True

    def test_the_cache_avoids_a_second_request_inside_the_window(self, monkeypatch):
        calls = []

        def counted(symbol, timeout=10.0):
            calls.append(symbol)
            return 61234.5

        monkeypatch.setattr(binance, "ticker_price", counted)
        service = QuoteService([BTC], crypto_ttl=60.0)

        service.quotes()
        service.quotes()
        service.quotes()

        assert calls == ["BTC"]


class TestMarking:
    def _engine(self, db, feed, quotes):
        engine = Engine(BotConfig(symbols=("BTC",), initial_cash=100_000.0),
                        db, feed=feed, quotes=quotes)
        return engine

    def _bar(self, close: float = 100.0) -> Bar:
        return Bar(symbol="BTC", ts=utcnow(), open=close, high=close, low=close, close=close)

    def test_an_open_position_is_marked_with_the_live_quote(self, db):
        from tests.stubs import StubFeed

        bar = self._bar(100.0)
        feed = StubFeed({"BTC": bar}, history_frame())
        engine = self._engine(db, feed, FakeQuotes({"BTC": make_quote("BTC", 123.0)}))

        request = OrderRequest(symbol="BTC", side=Side.BUY, qty=1.0, strategy="t",
                               signal_bar_ts=utcnow())
        engine.broker.submit(request, 100.0)
        db.upsert_position(engine.broker.positions["BTC"])

        engine.cycle()

        assert engine.broker.positions["BTC"].last_price == pytest.approx(123.0)

    def test_an_unusable_quote_leaves_the_daily_bar_in_charge(self, db):
        from tests.stubs import StubFeed

        bar = self._bar(100.0)
        feed = StubFeed({"BTC": bar}, history_frame())
        stale = make_quote("BTC", 123.0, age_seconds=600, max_age=120.0)
        engine = self._engine(db, feed, FakeQuotes({"BTC": stale}))

        request = OrderRequest(symbol="BTC", side=Side.BUY, qty=1.0, strategy="t",
                               signal_bar_ts=utcnow())
        engine.broker.submit(request, 100.0)
        db.upsert_position(engine.broker.positions["BTC"])

        engine.cycle()

        assert engine.broker.positions["BTC"].last_price == pytest.approx(100.0)

    def test_the_engine_works_without_a_quote_service(self, db):
        from tests.stubs import StubFeed

        bar = self._bar(100.0)
        feed = StubFeed({"BTC": bar}, history_frame())
        engine = self._engine(db, feed, None)

        assert engine.live_quotes() == {}
        result = engine.cycle()
        assert result.halted is False

    def test_a_broken_quote_service_never_breaks_the_cycle(self, db):
        from tests.stubs import StubFeed

        bar = self._bar(100.0)
        feed = StubFeed({"BTC": bar}, history_frame())
        engine = self._engine(db, feed, FakeQuotes(error=RuntimeError("simulated outage")))

        assert engine.live_quotes() == {}
        result = engine.cycle()
        assert result.halted is False


class TestApi:
    def _client(self, tmp_path, quotes):
        from fastapi.testclient import TestClient

        from stratbot.api import create_app
        from stratbot.broker.paper import PaperBroker

        db = Database(tmp_path / "api.sqlite3")
        config = BotConfig(symbols=("BTC",), initial_cash=500_000.0)
        engine = Engine(config, db, broker=PaperBroker(config.initial_cash), quotes=quotes)
        return TestClient(create_app(engine=engine, db=db))

    def test_the_api_reports_the_live_quotes(self, tmp_path):
        quotes = FakeQuotes({"BTC": make_quote("BTC", 61234.5)})

        with self._client(tmp_path, quotes) as client:
            body = client.get("/api/quotes").json()

            assert body[0]["symbol"] == "BTC"
            assert body[0]["price"] == pytest.approx(61234.5)
            assert body[0]["source"] == CRYPTO_SOURCE
            assert body[0]["usable"] is True
            assert client.get("/api/status").json()["realtime"] is True

    def test_the_api_says_when_there_is_no_quote_service(self, tmp_path):
        with self._client(tmp_path, None) as client:
            assert client.get("/api/status").json()["realtime"] is False
            assert client.get("/api/quotes").json() == []
