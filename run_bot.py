#!/usr/bin/env python
"""Paper trading engine entry point.

    python run_bot.py --cycles 3          # run three cycles against the live feed
    python run_bot.py --serve             # start the API and dashboard on :8000
    python run_bot.py --db test.sqlite3   # use a throwaway database
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from stratbot.config import CONFIG, DB_PATH
from stratbot.db import Database
from stratbot.engine import Engine

LOG_DIR = Path(__file__).resolve().parent / "logs"


def setup_logging(verbose: bool) -> logging.Logger:
    """File logging matters here: a multi-week run outlives any one terminal."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("stratbot")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(
        LOG_DIR / "engine.log", maxBytes=5_000_000, backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    return logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper trading engine (simulator only).")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite path")
    parser.add_argument("--cycles", type=int, default=1, help="cycles to run then exit")
    parser.add_argument("--sleep", type=int, default=0, help="seconds between cycles")
    parser.add_argument("--serve", action="store_true", help="start the FastAPI server")
    parser.add_argument("--forever", action="store_true", help="run until stopped")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = setup_logging(args.verbose)

    if args.serve:
        if not os.environ.get("STRATBOT_CONTROL_TOKEN"):
            logger.warning(
                "STRATBOT_CONTROL_TOKEN is unset, so emergency stop and resume are disabled. "
                "Set it before exposing this server to a network."
            )
        from stratbot.api import create_app

        import uvicorn

        db = Database(args.db)
        uvicorn.run(create_app(db=db), host=args.host, port=args.port, log_level="info")
        return 0

    config = CONFIG
    db = Database(Path(args.db))
    engine = Engine(config, db)

    logger.info("mode=paper strategy=%s symbols=%s", config.strategy_name, ",".join(config.symbols))
    logger.info(
        "limits: max_position=%.0f max_exposure=%.0f max_daily_loss=%.0f stop_loss=%.1f%%",
        config.limits.max_position_value,
        config.limits.max_total_exposure,
        config.limits.max_daily_loss,
        config.limits.stop_loss_pct * 100,
    )
    logger.info("db: %s", Path(args.db).resolve())
    logger.info("log: %s", LOG_DIR / "engine.log")

    cycles = None if args.forever else args.cycles
    engine.run_forever(cycles=cycles, sleep_seconds=args.sleep, logger=logger)

    account = engine.broker.get_account()
    logger.info("equity=%.2f cash=%.2f exposure=%.2f", account.equity, account.cash, account.exposure)
    logger.info("halted=%s reason=%s", engine.risk.halted, engine.risk.halt_reason.value if engine.risk.halt_reason else "-")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())