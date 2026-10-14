"""API surface tests. Uses an in-process client so no server is left running."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from stratbot.api import create_app
from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.models import HaltReason, OrderRequest, Side, utcnow

TOKEN = "test-control-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATBOT_CONTROL_TOKEN", TOKEN)
    import stratbot.api as api_module

    api_module.CONTROL_TOKEN = TOKEN
    db = Database(tmp_path / "api.sqlite3")
    config = BotConfig(symbols=("BTC",), initial_cash=500_000.0)
    engine = Engine(config, db, broker=PaperBroker(config.initial_cash))
    app = create_app(engine=engine, db=db)
    with TestClient(app) as test_client:
        test_client.engine = engine
        yield test_client


def test_status_reports_paper_mode(client):
    body = client.get("/api/status").json()
    assert body["is_paper"] is True
    assert body["mode"] == "paper"
    assert body["halted"] is False
    assert "max_daily_loss" in body["limits"]


def test_status_never_leaks_a_secret(client):
    text = client.get("/api/status").text.lower()
    for token in ("secret", "password", "api_key", "credential"):
        assert token not in text


def test_control_routes_require_the_token(client):
    assert client.post("/api/emergency-stop").status_code == 403
    assert client.post("/api/resume").status_code == 403
    assert client.post("/api/cycle").status_code == 403


def test_emergency_stop_works_with_the_token(client):
    engine = client.engine
    engine.broker.submit(
        OrderRequest(symbol="BTC", side=Side.BUY, qty=1.0, strategy="t", signal_bar_ts=utcnow()),
        100.0,
    )
    client.engine.db.upsert_position(engine.broker.positions["BTC"])

    response = client.post("/api/emergency-stop", headers={"X-Control-Token": TOKEN})
    assert response.status_code == 200
    assert response.json()["halted"] is True
    assert engine.risk.halted
    assert not engine.broker.positions["BTC"].is_open()


def test_emergency_stop_works_even_with_no_positions(client):
    response = client.post("/api/emergency-stop", headers={"X-Control-Token": TOKEN})
    assert response.status_code == 200
    assert response.json()["flattened"] == []


def test_resume_clears_the_halt(client):
    client.engine.risk.halt(HaltReason.MANUAL, "test")
    response = client.post("/api/resume", headers={"X-Control-Token": TOKEN})
    assert response.json()["halted"] is False
    assert not client.engine.risk.halted


def test_read_endpoints_all_return_valid_json(client):
    for path in ("/api/status", "/api/positions", "/api/equity", "/api/signals",
                 "/api/orders", "/api/trades", "/api/risk-events", "/api/reconcile"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.json() is not None, path


def test_reconcile_endpoint_reports_in_sync(client):
    assert client.get("/api/reconcile").json()["in_sync"] is True


def test_control_is_disabled_when_no_token_is_configured(tmp_path, monkeypatch):
    import importlib

    import stratbot.api as api_module

    importlib.reload(api_module)
    monkeypatch.setattr(api_module, "CONTROL_TOKEN", "")
    db = Database(tmp_path / "notoken.sqlite3")
    app = api_module.create_app(db=db)
    with TestClient(app) as client:
        response = client.post("/api/emergency-stop")
        assert response.status_code == 503, "controls must fail closed"