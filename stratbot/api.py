"""FastAPI surface for the paper engine.

Read-mostly. The only mutating routes are the operator controls, and they all
require the shared control token so a browser tab cannot halt or resume the bot.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import CONFIG, DB_PATH, TRADING_MODE
from .db import Database
from .engine import Engine
from .reconciler import adopt_broker_state, reconcile

CONTROL_TOKEN = os.environ.get("STRATBOT_CONTROL_TOKEN", "")


def require_control(x_control_token: str | None = Header(default=None)) -> None:
    if not CONTROL_TOKEN:
        raise HTTPException(503, "control token not configured; operator controls disabled")
    if x_control_token != CONTROL_TOKEN:
        raise HTTPException(403, "bad control token")


def create_app(engine: Engine | None = None, db: Database | None = None) -> FastAPI:
    db = db or Database(DB_PATH)
    engine = engine or Engine(CONFIG, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(
        title="Paper trading engine",
        version="0.1.0",
        lifespan=lifespan,
        description="Simulator only. This process cannot place a real order.",
    )

    # Only for `vite dev` on :5173. The built dashboard is served same-origin and never needs this.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Control-Token"],
    )

    @app.get("/api/status")
    def status():
        account = engine.broker.get_account()
        return {
            "mode": TRADING_MODE,
            "is_paper": engine.config.is_paper,
            "halted": engine.risk.halted,
            "halt_reason": engine.risk.halt_reason.value if engine.risk.halt_reason else None,
            "cycles": engine.cycle_count,
            "strategy": engine.config.strategy_name,
            "symbols": list(engine.config.symbols),
            "equity": round(account.equity, 2),
            "cash": round(account.cash, 2),
            "exposure": round(account.exposure, 2),
            "unrealised": round(account.unrealised, 2),
            "day_pnl": round(db.day_pnl(account.equity), 2),
            "limits": {
                "max_position_value": engine.config.limits.max_position_value,
                "max_total_exposure": engine.config.limits.max_total_exposure,
                "max_daily_loss": engine.config.limits.max_daily_loss,
                "stop_loss_pct": engine.config.limits.stop_loss_pct,
            },
            "control_enabled": bool(CONTROL_TOKEN),
        }

    @app.get("/api/positions")
    def positions():
        account = engine.broker.get_account()
        return [
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_price": round(p.avg_price, 4),
                "last_price": round(p.last_price, 4),
                "market_value": round(p.market_value, 2),
                "unrealised_pnl": round(p.unrealised_pnl, 2),
                "unrealised_pct": round(p.unrealised_pct, 3),
            }
            for p in account.positions.values()
            if p.is_open()
        ]

    @app.get("/api/equity")
    def equity():
        return db.equity_history()

    @app.get("/api/signals")
    def signals(limit: int = 100):
        return db.recent_signals(limit)

    @app.get("/api/orders")
    def orders(limit: int = 100):
        return db.recent_fills(limit)

    @app.get("/api/trades")
    def trades():
        return db.closed_trades()

    @app.get("/api/risk-events")
    def risk_events(limit: int = 100):
        return db.recent_risk_events(limit)

    @app.post("/api/emergency-stop", dependencies=[Depends(require_control)])
    def emergency_stop():
        return engine.emergency_stop()

    @app.post("/api/resume", dependencies=[Depends(require_control)])
    def resume():
        return engine.resume()

    @app.post("/api/cycle", dependencies=[Depends(require_control)])
    def cycle():
        result = engine.cycle()
        return {
            "cycle": result.cycle,
            "halted": result.halted,
            "halt_reason": result.halt_reason,
            "equity": round(result.account.equity, 2),
            "decisions": result.decisions,
        }

    @app.post("/api/adopt-broker-state", dependencies=[Depends(require_control)])
    def adopt():
        changed = adopt_broker_state(db, engine.broker.get_account())
        return {"adopted": changed}

    @app.get("/api/reconcile")
    def reconcile_status():
        mismatches = reconcile(db, engine.broker.get_account())
        return {"in_sync": not mismatches, "mismatches": [vars(m) for m in mismatches]}

    @app.get("/api/bars/{symbol}")
    def bars(symbol: str, limit: int = 180):
        """Recent daily OHLC for the chart, read from the local price cache."""
        from stratlab import data as price_data

        instrument = price_data.BY_SYMBOL.get(symbol.upper())
        if instrument is None:
            raise HTTPException(404, f"unknown symbol {symbol}")
        try:
            frame = price_data.load(instrument)
        except Exception as exc:
            raise HTTPException(503, f"price cache unavailable for {symbol}: {exc}")
        tail = frame.tail(max(1, min(limit, 1000)))
        return {
            "symbol": instrument.symbol,
            "bars": [
                {
                    "ts": index.isoformat(),
                    "open": round(float(row.open), 4),
                    "high": round(float(row.high), 4),
                    "low": round(float(row.low), 4),
                    "close": round(float(row.close), 4),
                }
                for index, row in tail.iterrows()
            ],
        }

    _mount_dashboard(app)
    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the built PWA from the same origin, so the UI needs no API host config."""
    dist = Path(__file__).resolve().parent.parent / "dashboard" / "dist"
    if not (dist / "index.html").exists():
        app.state.dashboard_built = False
        return

    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404, "unknown endpoint")
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    app.state.dashboard_built = True


app = create_app()