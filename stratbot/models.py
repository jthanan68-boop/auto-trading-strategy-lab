"""Core value objects shared by the engine, broker adapters and database."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(ts: datetime) -> datetime:
    """Normalise a bar timestamp for comparison.

    Replayed bars carry naive timestamps from pandas; the ledger stores ISO
    text. Bar.age_seconds already reads naive as UTC, so comparison uses the
    same convention rather than raising on mixed awareness.
    """
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    NEW = "NEW"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    # Deliberately distinct from REJECTED: we do not know, and must not retry.
    UNKNOWN = "UNKNOWN"
    # Distinct from REJECTED too: the order was valid, the risk volume gate is
    # full for the window. Terminal like REJECTED - the engine does not retry
    # it; the strategy re-signals on its next decision bar if it still wants it.
    RATE_LIMITED = "RATE_LIMITED"


class HaltReason(str, Enum):
    MANUAL = "manual_kill_switch"
    DAILY_LOSS = "daily_loss_limit"
    POSITION_CAP = "position_cap"
    EXPOSURE_CAP = "exposure_cap"
    ORDER_RATE = "order_rate_limit"
    STALE_PRICE = "stale_price"
    UNKNOWN_ORDER = "unknown_order_status"
    RECONCILE_MISMATCH = "reconcile_mismatch"
    BROKER_DOWN = "broker_unavailable"


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def age_seconds(self) -> float:
        # Replayed bars carry naive timestamps from pandas; normalise before
        # comparing, or the subtraction raises on mixed awareness.
        now = utcnow()
        ts = self.ts if self.ts.tzinfo else self.ts.replace(tzinfo=now.tzinfo)
        return (now - ts).total_seconds()


@dataclass
class OrderRequest:
    symbol: str
    side: Side
    qty: float
    strategy: str
    signal_bar_ts: datetime
    reason: str = ""
    reference_price: float = 0.0
    # Weight of equity the strategy asked for when it raised this intent. Held so
    # the order can be re-sized at the execution price one bar later, instead of
    # carrying a quantity that was computed against a stale close.
    target_weight: float | None = None

    @property
    def client_order_id(self) -> str:
        """Deterministic so a retry after a network blip cannot double fill."""
        payload = f"{self.symbol}|{self.side.value}|{self.strategy}|{self.signal_bar_ts.isoformat()}"
        digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
        return f"{self.strategy}-{self.symbol}-{digest}"


@dataclass
class Fill:
    client_order_id: str
    ts: datetime
    symbol: str
    side: Side
    qty: float
    price: float
    fees: float = 0.0

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    opened_at: datetime | None = None
    last_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price

    @property
    def cost_basis(self) -> float:
        return self.qty * self.avg_price

    @property
    def unrealised_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def unrealised_pct(self) -> float:
        basis = self.cost_basis
        return (self.unrealised_pnl / basis * 100.0) if basis else 0.0

    def is_open(self) -> bool:
        return abs(self.qty) > 1e-9


@dataclass
class AccountState:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def market_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.positions.values())

    @property
    def equity(self) -> float:
        return self.cash + self.market_value

    @property
    def unrealised(self) -> float:
        return sum(p.unrealised_pnl for p in self.positions.values())

    @property
    def realised_today(self) -> float:
        """Not derivable from a snapshot.

        Realised P/L belongs to the ledger: an open position's cost is
        capital converted into an asset, never a loss. The authoritative
        figure is Database.closed_pnl(), which counts only positions that
        were actually flattened."""
        return 0.0