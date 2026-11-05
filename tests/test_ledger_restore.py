"""Restart accounting: cash rebuilt from the fill ledger, not from cost basis.

A restart used to refund the entry fees of every open position and drop realised
profit out of equity, because it started from the full capital and subtracted
only what the positions cost. Replaying the signed fill legs is the arithmetic
the broker actually applied, so a restart is a no-op on equity.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from stubs import StubFeed

from stratbot.config import BotConfig
from stratbot.db import Database, backup_database
from stratbot.engine import Engine
from stratbot.models import Fill, Position, Side, utcnow

INITIAL_CASH = 100_000.0


def build_engine(tmp_path: Path, db: Database) -> Engine:
    config = BotConfig(symbols=("TEST",), strategy_name="donchian-55/20",
                       initial_cash=INITIAL_CASH)
    return Engine(config, db, feed=StubFeed({}, {}))


def fill(db: Database, side: Side, qty: float, price: float, fees: float,
         tag: str) -> None:
    db.record_fill(Fill(
        client_order_id=tag, ts=utcnow(), symbol="TEST", side=side,
        qty=qty, price=price, fees=fees,
    ))


class TestCashFlow:
    def test_a_buy_spends_notional_plus_fees(self, tmp_path):
        db = Database(tmp_path / "ledger.sqlite3")
        fill(db, Side.BUY, 10, 100.0, 1.5, "b1")
        assert db.cash_flow() == pytest.approx(-1001.5)

    def test_a_sell_returns_notional_less_fees(self, tmp_path):
        db = Database(tmp_path / "ledger.sqlite3")
        fill(db, Side.SELL, 10, 110.0, 1.5, "s1")
        assert db.cash_flow() == pytest.approx(1098.5)

    def test_an_empty_ledger_moves_no_cash(self, tmp_path):
        db = Database(tmp_path / "ledger.sqlite3")
        assert db.cash_flow() == 0.0


class TestRestartAccounting:
    def test_realised_profit_survives_a_restart(self, tmp_path):
        db = Database(tmp_path / "ledger.sqlite3")
        fill(db, Side.BUY, 10, 100.0, 1.0, "b1")
        fill(db, Side.SELL, 10, 110.0, 1.0, "s1")
        engine = build_engine(tmp_path, db)
        assert engine.broker.cash == pytest.approx(INITIAL_CASH + 98.0)
        assert "TEST" not in engine.broker.positions

    def test_an_open_position_does_not_refund_its_entry_fees(self, tmp_path):
        db = Database(tmp_path / "ledger.sqlite3")
        fill(db, Side.BUY, 10, 100.0, 1.0, "b1")
        db.upsert_position(Position(symbol="TEST", qty=10.0, avg_price=100.0,
                                    opened_at=utcnow(), entry_fees=1.0))
        engine = build_engine(tmp_path, db)
        # The old path would have started at 100_000 - 1_000 and refunded the fee.
        assert engine.broker.cash == pytest.approx(INITIAL_CASH - 1001.0)
        assert engine.broker.positions["TEST"].qty == 10.0

    def test_a_restored_position_keeps_its_entry_leg_for_the_exit(self, tmp_path):
        db = Database(tmp_path / "ledger.sqlite3")
        fill(db, Side.BUY, 10, 100.0, 1.0, "b1")
        db.upsert_position(Position(symbol="TEST", qty=10.0, avg_price=100.0,
                                    opened_at=utcnow(), entry_fees=1.0))
        engine = build_engine(tmp_path, db)
        avg_price, qty, _, entry_fees = engine._open_lots["TEST"]
        assert (avg_price, qty, entry_fees) == (100.0, 10.0, 1.0)


class TestRestoredPositions:
    def test_a_new_entry_does_not_inherit_a_flat_rows_old_open_time(self):
        # A flat row in the ledger can keep the previous lot's opened_at. The
        # restart guard flattens anything opened before this session's start, so
        # a re-entry that inherited that time was sold on the very next cycle.
        from stratbot.broker.paper import PaperBroker

        broker = PaperBroker(INITIAL_CASH)
        old = datetime(2026, 11, 3, 5, 55, tzinfo=timezone.utc)
        broker.positions["TEST"] = Position(symbol="TEST", qty=0.0, opened_at=old)

        broker._apply_fill("TEST", 10.0, 100.0, True, fees=1.0)

        opened = broker.positions["TEST"].opened_at
        assert opened is not None and opened > old
        assert broker.positions["TEST"].entry_fees == pytest.approx(1.0)


class TestEntryFeePersistence:
    def test_entry_fees_round_trip_through_the_ledger(self, tmp_path):
        db = Database(tmp_path / "ledger.sqlite3")
        db.upsert_position(Position(symbol="TEST", qty=5.0, avg_price=20.0,
                                    opened_at=utcnow(), entry_fees=2.25))
        reopened = Database(tmp_path / "ledger.sqlite3")
        assert reopened.positions()["TEST"].entry_fees == pytest.approx(2.25)

    def test_an_older_file_gains_the_column(self, tmp_path):
        path = tmp_path / "old.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE positions (symbol TEXT PRIMARY KEY, qty REAL NOT NULL DEFAULT 0,"
            " avg_price REAL NOT NULL DEFAULT 0, opened_at TEXT, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO positions (symbol, qty, avg_price, updated_at)"
            " VALUES ('TEST', 3, 10, '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        db = Database(path)
        with db.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(positions)")}
        assert "entry_fees" in columns
        assert db.positions()["TEST"].entry_fees == 0.0


class TestLedgerBackup:
    def test_a_snapshot_copies_the_ledger_and_prunes_old_ones(self, tmp_path, monkeypatch):
        path = tmp_path / "ledger.sqlite3"
        db = Database(path)
        fill(db, Side.BUY, 1, 10.0, 0.0, "b1")

        clock = {"now": datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)}

        def tick():
            clock["now"] += timedelta(seconds=1)
            return clock["now"]

        monkeypatch.setattr("stratbot.db.utcnow", tick)
        for _ in range(3):
            assert backup_database(path, keep=2) is not None

        backups = sorted((tmp_path / "backups").glob("ledger-*.sqlite3"))
        assert len(backups) == 2
        latest = sqlite3.connect(backups[-1])
        try:
            count = latest.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        finally:
            latest.close()
        assert count == 1

    def test_a_missing_ledger_is_not_an_error(self, tmp_path):
        assert backup_database(tmp_path / "absent.sqlite3") is None
