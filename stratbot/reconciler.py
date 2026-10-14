"""Reconciler: the bot's ledger versus the broker's actual state.

A divergence means the bot believes something the broker does not. The only
safe response is to stop, not to try and self-heal.
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import Database
from .models import AccountState, HaltReason, Position
from .risk import RiskManager


@dataclass
class Mismatch:
    symbol: str
    ledger_qty: float
    broker_qty: float

    @property
    def delta(self) -> float:
        return abs(self.ledger_qty - self.broker_qty)


def reconcile(
    db: Database, broker_account: AccountState, risk: RiskManager | None = None
) -> list[Mismatch]:
    ledger = db.positions()
    mismatches: list[Mismatch] = []

    for symbol in set(ledger) | set(broker_account.positions):
        ledger_qty = ledger[symbol].qty if symbol in ledger else 0.0
        broker_qty = broker_account.positions[symbol].qty if symbol in broker_account.positions else 0.0
        # 1e-9 tolerance absorbs float drift from partial fills.
        if abs(ledger_qty - broker_qty) > 1e-9:
            mismatches.append(Mismatch(symbol, ledger_qty, broker_qty))

    if mismatches and risk is not None:
        detail = ", ".join(f"{m.symbol}: ledger {m.ledger_qty:.6f} vs broker {m.broker_qty:.6f}" for m in mismatches)
        risk.halt(HaltReason.RECONCILE_MISMATCH, detail)

    return mismatches


def adopt_broker_state(db: Database, broker_account: AccountState) -> int:
    """Overwrite the ledger with broker truth. Only ever called by an operator."""
    changed = 0
    symbols = set(db.positions()) | set(broker_account.positions)
    for symbol in symbols:
        ledger = db.positions().get(symbol)
        broker = broker_account.positions.get(symbol)
        ledger_qty = ledger.qty if ledger else 0.0
        broker_qty = broker.qty if broker else 0.0
        if abs(ledger_qty - broker_qty) <= 1e-9:
            continue
        source = broker or Position(symbol=symbol)
        db.upsert_position(
            Position(
                symbol=symbol,
                qty=broker_qty,
                avg_price=source.avg_price,
                opened_at=source.opened_at,
                last_price=source.last_price,
            )
        )
        db.log_risk_event("adopt_broker_state", f"{symbol} set to {broker_qty:.6f}")
        changed += 1
    return changed