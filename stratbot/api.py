"""FastAPI surface for the paper engine.

Read-mostly. The only mutating routes are the operator controls, and they all
require the shared control token so a browser tab cannot halt or resume the bot.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import CONFIG, DB_PATH, TRADING_MODE, BotConfig
from .db import Database
from .engine import Engine
from .models import HaltReason
from .reconciler import adopt_broker_state, reconcile
from .broker.quotes import QuoteService

CONTROL_TOKEN = os.environ.get("STRATBOT_CONTROL_TOKEN", "")


def require_control(x_control_token: str | None = Header(default=None)) -> None:
    if not CONTROL_TOKEN:
        raise HTTPException(503, "control token not configured; operator controls disabled")
    if x_control_token != CONTROL_TOKEN:
        raise HTTPException(403, "bad control token")


def create_app(engine: Engine | None = None, db: Database | None = None,
               config: BotConfig | None = None, trade: bool = False,
               poll_seconds: float | None = None,
               quotes: QuoteService | None = None) -> FastAPI:
    db = db or Database(DB_PATH)
    engine = engine or Engine(config or CONFIG, db, quotes=quotes)

    # One engine, one ledger. A separate CLI loop and a separate server would each
    # hold their own in-memory book and overwrite each other's positions, so the
    # polling loop lives inside this process and shares the engine.
    lock = threading.Lock()
    stop = threading.Event()
    logger = logging.getLogger("stratbot")

    def cycle_once() -> dict:
        with lock:
            result = engine.cycle()
        return {
            "cycle": result.cycle,
            "halted": result.halted,
            "halt_reason": result.halt_reason,
            "equity": round(result.account.equity, 2),
            "decisions": result.decisions,
        }

    def live_account():
        """The account marked with live quotes, so the numbers on screen are current.

        The recorded equity curve stays per cycle; this only reprices the open
        book for whoever is looking at it now.
        """
        live = engine.live_quotes()
        usable = {s: q.price for s, q in live.items() if q.usable}
        if usable:
            engine.broker.mark(usable)
        return engine.broker.get_account()

    def poll(stop_event: threading.Event) -> None:
        interval = poll_seconds if poll_seconds is not None else engine.config.poll_seconds
        while not stop_event.is_set():
            try:
                summary = cycle_once()
                logger.info(
                    "cycle %s %s equity=%.2f exposure=%.2f",
                    summary["cycle"],
                    "HALTED" if summary["halted"] else "ok",
                    summary["equity"],
                    engine.broker.get_account().exposure,
                )
            except Exception as exc:  # a dead loop is worse than a halted one
                engine.risk.halt(HaltReason.BROKER_DOWN, f"cycle error: {exc}")
                logger.error("cycle error: %s", exc)
            stop_event.wait(interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = None
        if trade:
            worker = threading.Thread(target=poll, args=(stop,), name="stratbot-cycle", daemon=True)
            worker.start()
            logger.info("trading loop started, polling every %ss",
                        poll_seconds if poll_seconds is not None else engine.config.poll_seconds)
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                worker.join(timeout=5)

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
        account = live_account()
        return {
            "mode": TRADING_MODE,
            "is_paper": engine.config.is_paper,
            "realtime": engine.quotes is not None,
            "halted": engine.risk.halted,
            "halt_reason": engine.risk.halt_reason.value if engine.risk.halt_reason else None,
            "cycles": engine.cycle_count,
            "strategy": engine.config.strategy_name,
            "symbols": list(engine.config.symbols),
            "equity": round(account.equity, 2),
            "cash": round(account.cash, 2),
            "exposure": round(account.exposure, 2),
            "market_value": round(account.market_value, 2),
            "unrealised": round(account.unrealised, 2),
            # Realised comes from flattened positions only. Opening a position
            # spends cash but realises nothing, so cash-minus-starting-cash would
            # report the whole notional as a loss.
            "realised": round(db.closed_pnl(), 2),
            "fees_paid": round(db.fees_paid(), 2),
            "position_cost": round(db.open_position_cost(), 2),
            "day_pnl": round(db.day_pnl(account.equity), 2),
            "limits": {
                "max_position_value": engine.config.limits.max_position_value,
                "max_total_exposure": engine.config.limits.max_total_exposure,
                "max_daily_loss": engine.config.limits.max_daily_loss,
                "stop_loss_pct": engine.config.limits.stop_loss_pct,
                "stop_loss_intraday": engine.config.stop_loss_intraday,
                "stop_loss_use_live_quotes": engine.config.stop_loss_use_live_quotes,
            },
            "control_enabled": bool(CONTROL_TOKEN),
        }

    @app.get("/api/positions")
    def positions():
        account = live_account()
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

    @app.get("/api/portfolio")
    def portfolio():
        """Portfolio risk-manager state for the dashboard."""
        pm = engine.portfolio
        return pm.get_state()

    @app.get("/api/quotes")
    def quotes_endpoint():
        """Live prices, with the source that produced them.

        The source and the age are part of the answer: a Binance print is
        live, a Yahoo intraday print for an NSE name is fifteen minutes late,
        and the operator is told which one they are looking at.
        """
        return [
            {
                "symbol": q.symbol,
                "price": round(q.price, 6),
                "ts": q.ts.isoformat(),
                "source": q.source,
                "delayed": q.delayed,
                "age_seconds": round(q.age_seconds, 1),
                "usable": q.usable,
            }
            for q in engine.live_quotes().values()
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

    @app.get("/api/pending-orders")
    def pending_orders(limit: int = 100):
        # Orders raised on a bar but not yet filled. Without this the dashboard
        # cannot show why the bot looks idle while it is actually holding work.
        return db.pending_orders()[:limit]

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
        return cycle_once()

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
            if frame.empty:
                # Intraday runs keep only the bar-interval cache; fold it to daily so a
                # symbol added after the last lab run still charts.
                interval = getattr(getattr(engine, "config", None), "bar_interval", "5m")
                frame = price_data.load(instrument, interval=interval)
                if not frame.empty:
                    frame = frame.resample("1D").agg(
                        {"open": "first", "high": "max", "low": "min", "close": "last"}
                    ).dropna(subset=["open", "high", "low", "close"])
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
        # index.html names the hashed bundle; caching it serves a stale UI after a rebuild.
        return FileResponse(dist / "index.html",
                            headers={"Cache-Control": "no-cache, must-revalidate"})

    app.state.dashboard_built = True


_default_app = None


def _default_quotes() -> QuoteService:
    """Quotes for the default universe, so a standalone server is live too."""
    from stratlab import data as price_data

    return QuoteService(price_data.BY_SYMBOL[symbol] for symbol in CONFIG.symbols)


def __getattr__(name: str):
    # An app built at import time writes to the live ledger on every import, so it is built on demand instead.
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app(quotes=_default_quotes())
        return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")