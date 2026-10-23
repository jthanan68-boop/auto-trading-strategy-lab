"""The server runs the trading loop in the same process as the dashboard.

Two engines sharing one SQLite ledger would each keep their own in-memory book
and overwrite each other's positions, so the loop has to live inside the app.
These tests pin that it starts, ticks, and stops cleanly.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from stratbot.api import create_app
from stratbot.broker.paper import PaperBroker
from stratbot.config import BotConfig
from stratbot.db import Database
from stratbot.engine import Engine


class StubFeed:
    """Reports no usable data, so a cycle halts instead of touching the network."""

    provenance: dict = {}

    def bars(self, force_refresh: bool = False) -> dict:
        return {}

    def history(self, symbol: str):
        raise KeyError(symbol)


@pytest.fixture
def engine(tmp_path) -> Engine:
    db = Database(tmp_path / "loop.sqlite3")
    config = BotConfig(symbols=("TEST",), initial_cash=500_000.0)
    eng = Engine(config, db, broker=PaperBroker(config.initial_cash))
    eng.feed = StubFeed()
    return eng


def test_the_loop_runs_cycles_while_the_server_is_up(engine):
    app = create_app(engine=engine, db=engine.db, trade=True, poll_seconds=0.05)
    with TestClient(app):
        deadline = time.time() + 5
        while engine.cycle_count < 2 and time.time() < deadline:
            time.sleep(0.05)
    assert engine.cycle_count >= 2, "the polling loop never ticked"


def test_the_loop_stops_when_the_server_shuts_down(engine):
    app = create_app(engine=engine, db=engine.db, trade=True, poll_seconds=0.05)
    with TestClient(app):
        time.sleep(0.2)
        before = engine.cycle_count
    time.sleep(0.4)
    assert engine.cycle_count - before <= 1, "the worker kept running after shutdown"


def test_no_loop_runs_when_trading_is_off(engine):
    app = create_app(engine=engine, db=engine.db, trade=False)
    with TestClient(app):
        time.sleep(0.3)
    assert engine.cycle_count == 0


def test_a_cycle_error_halts_rather_than_killing_the_loop(tmp_path):
    db = Database(tmp_path / "boom.sqlite3")
    config = BotConfig(symbols=("TEST",), initial_cash=500_000.0)
    eng = Engine(config, db, broker=PaperBroker(config.initial_cash))

    class Exploding:
        provenance: dict = {}

        def bars(self, force_refresh: bool = False) -> dict:
            raise RuntimeError("feed down")

        def history(self, symbol: str):
            raise RuntimeError("feed down")

    eng.feed = Exploding()
    app = create_app(engine=eng, db=db, trade=True, poll_seconds=0.05)
    with TestClient(app):
        time.sleep(0.3)
    assert eng.risk.halted, "a broken feed did not halt the bot"
    assert db.get_state("halt_reason") == "broker_unavailable"


def test_status_and_pending_orders_are_served(engine):
    app = create_app(engine=engine, db=engine.db)
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 200
        response = client.get("/api/pending-orders")
        assert response.status_code == 200
        assert isinstance(response.json(), list)