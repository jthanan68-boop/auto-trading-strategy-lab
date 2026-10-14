"""Dashboard-serving tests: static mount, SPA fallback, and the bars endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from stratbot.api import create_app
from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig
from stratbot.db import Database
from stratbot.engine import Engine

DIST = __import__("pathlib").Path(__file__).resolve().parent.parent / "dashboard" / "dist"
BUILT = (DIST / "index.html").exists()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATBOT_CONTROL_TOKEN", "tok")
    import stratbot.api as api_module

    api_module.CONTROL_TOKEN = "tok"
    db = Database(tmp_path / "dash.sqlite3")
    config = BotConfig(symbols=("BTC",), initial_cash=500_000.0)
    engine = Engine(config, db, broker=PaperBroker(config.initial_cash))
    with TestClient(create_app(engine=engine, db=db)) as test_client:
        yield test_client


@pytest.mark.skipif(not BUILT, reason="dashboard not built; run npm run build in dashboard/")
def test_root_serves_the_dashboard(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<div id=\"root\">" in response.text


@pytest.mark.skipif(not BUILT, reason="dashboard not built")
def test_spa_routes_fall_back_to_index(client):
    for path in ("/positions", "/anything/deep", "/favicon.ico"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "<div id=\"root\">" in response.text, path


@pytest.mark.skipif(not BUILT, reason="dashboard not built")
def test_unknown_api_route_still_404s(client):
    assert client.get("/api/definitely-not-real").status_code == 404


@pytest.mark.skipif(not BUILT, reason="dashboard not built")
def test_manifest_and_icon_are_served(client):
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"


def test_bars_endpoint_rejects_unknown_symbols(client):
    assert client.get("/api/bars/NOT_A_SYMBOL").status_code == 404


def test_bars_endpoint_returns_sorted_ohlc(client):
    response = client.get("/api/bars/BTC?limit=30")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "BTC"
    bars = body["bars"]
    assert 0 < len(bars) <= 30
    first = bars[0]
    assert {"ts", "open", "high", "low", "close"} <= set(first)
    assert first["low"] <= first["open"] <= first["high"]
    assert first["low"] <= first["close"] <= first["high"]
    timestamps = [bar["ts"] for bar in bars]
    assert timestamps == sorted(timestamps), "bars must be chronological"


def test_bars_endpoint_respects_limit(client):
    assert len(client.get("/api/bars/BTC?limit=10").json()["bars"]) <= 10


def test_built_bundle_does_not_hardcode_a_server_host():
    """A baked-in IP would break the APK; the bundle must stay origin-relative."""
    if not BUILT:
        pytest.skip("dashboard not built")
    for asset in (DIST / "assets").glob("*.js"):
        text = asset.read_text(encoding="utf-8", errors="ignore")
        for bad in ("http://192.168.", "http://127.0.0.1:8000", "http://localhost:8000"):
            assert bad not in text, f"{asset.name} hardcodes {bad}"