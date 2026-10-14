"""SQLite ledger for signals, orders, fills, positions, equity and risk events.

Every state change is written before it is acted on, so a crash leaves a record
rather than a mystery. Re-runnable schema: every object is IF NOT EXISTS.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Bar, Fill, OrderRequest, OrderStatus, Position, Side, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    action        TEXT NOT NULL,
    weight        REAL NOT NULL,
    price         REAL NOT NULL,
    bar_ts        TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             REAL NOT NULL,
    reference_price REAL NOT NULL DEFAULT 0,
    strategy        TEXT NOT NULL,
    status          TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    ts              TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             REAL NOT NULL,
    price           REAL NOT NULL,
    fees            REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);

CREATE TABLE IF NOT EXISTS positions (
    symbol     TEXT PRIMARY KEY,
    qty        REAL NOT NULL DEFAULT 0,
    avg_price  REAL NOT NULL DEFAULT 0,
    opened_at  TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity (
    ts            TEXT PRIMARY KEY,
    equity        REAL NOT NULL,
    cash          REAL NOT NULL,
    exposure      REAL NOT NULL,
    unrealised    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS closed_trades (
    id         INTEGER PRIMARY KEY,
    symbol     TEXT NOT NULL,
    entry_ts   TEXT NOT NULL,
    exit_ts    TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    qty        REAL NOT NULL,
    pnl        REAL NOT NULL,
    fees       REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_events (
    id     INTEGER PRIMARY KEY,
    ts     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_risk_ts ON risk_events(ts);
"""

STATE_KEYS = ("halted", "halt_reason", "day_start_equity", "day_start")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    # ---- signals -------------------------------------------------------
    def log_signal(self, strategy, symbol, action, weight, price, bar_ts, reason=""):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO signals (ts,symbol,strategy,action,weight,price,bar_ts,reason)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (utcnow().isoformat(), symbol, strategy, action, weight, price, bar_ts.isoformat(), reason),
            )

    def recent_signals(self, limit=100) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- orders --------------------------------------------------------
    def order_exists(self, client_order_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM orders WHERE client_order_id=?", (client_order_id,)
            ).fetchone()
        return row is not None

    def record_order(self, request: OrderRequest, status: OrderStatus, reason: str = ""):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO orders"
                " (client_order_id,ts,symbol,side,qty,reference_price,strategy,status,reason)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    request.client_order_id, utcnow().isoformat(), request.symbol,
                    request.side.value, request.qty, request.reference_price,
                    request.strategy, status.value, reason,
                ),
            )

    def update_order_status(self, client_order_id: str, status: OrderStatus, reason: str = ""):
        with self.connect() as conn:
            conn.execute(
                "UPDATE orders SET status=?, reason=? WHERE client_order_id=?",
                (status.value, reason, client_order_id),
            )

    def open_orders(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status IN (?,?)",
                (OrderStatus.NEW.value, OrderStatus.UNKNOWN.value),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- fills ---------------------------------------------------------
    def record_fill(self, fill: Fill):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO fills (client_order_id,ts,symbol,side,qty,price,fees)"
                " VALUES (?,?,?,?,?,?,?)",
                (fill.client_order_id, fill.ts.isoformat(), fill.symbol,
                 fill.side.value, fill.qty, fill.price, fill.fees),
            )

    def recent_fills(self, limit=100) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def count_orders_since(self, since: datetime) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM orders WHERE ts>=?", (since.isoformat(),)).fetchone()
        return int(row["n"])

    # ---- positions -----------------------------------------------------
    def upsert_position(self, position: Position):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO positions (symbol,qty,avg_price,opened_at,updated_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET"
                " qty=excluded.qty, avg_price=excluded.avg_price,"
                " opened_at=excluded.opened_at, updated_at=excluded.updated_at",
                (position.symbol, position.qty, position.avg_price,
                 _iso(position.opened_at), utcnow().isoformat()),
            )

    def positions(self) -> dict[str, Position]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM positions").fetchall()
        out: dict[str, Position] = {}
        for row in rows:
            out[row["symbol"]] = Position(
                symbol=row["symbol"],
                qty=row["qty"],
                avg_price=row["avg_price"],
                opened_at=_parse(row["opened_at"]),
            )
        return out

    # ---- equity --------------------------------------------------------
    def record_equity(self, equity: float, cash: float, exposure: float, unrealised: float):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO equity (ts,equity,cash,exposure,unrealised) VALUES (?,?,?,?,?)",
                (utcnow().isoformat(), equity, cash, exposure, unrealised),
            )

    def equity_history(self, limit=2000) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM equity ORDER BY ts LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- closed trades -------------------------------------------------
    def record_closed_trade(self, symbol, entry_ts, exit_ts, entry_price, exit_price, qty, pnl, fees):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO closed_trades"
                " (symbol,entry_ts,exit_ts,entry_price,exit_price,qty,pnl,fees)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (symbol, _iso(entry_ts), _iso(exit_ts), entry_price, exit_price, qty, pnl, fees),
            )

    def closed_trades(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM closed_trades ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    # ---- bot state -----------------------------------------------------
    def set_state(self, key: str, value):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO bot_state (key,value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def get_state(self, key: str, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def log_risk_event(self, kind: str, detail: str = ""):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO risk_events (ts,kind,detail) VALUES (?,?,?)",
                (utcnow().isoformat(), kind, detail),
            )

    def recent_risk_events(self, limit=100) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def reset_day(self, equity: float):
        today = utcnow().date().isoformat()
        if self.get_state("day_start") != today:
            self.set_state("day_start", today)
            self.set_state("day_start_equity", equity)
            return True
        return False

    def day_pnl(self, equity: float) -> float:
        start = self.get_state("day_start_equity")
        return equity - start if start is not None else 0.0