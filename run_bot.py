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
from dataclasses import replace
from pathlib import Path

from stratbot.broker.feed import MarketDataFeed
from stratbot.broker.quotes import QuoteService
from stratbot.config import CONFIG, DB_PATH, BotConfig
from stratbot.db import Database
from stratbot.engine import Engine
from stratbot.single_instance import InstanceAlreadyRunning, InstanceLock

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


def _parse_symbols(raw: str) -> tuple[str, ...]:
    """Narrow the universe, refusing anything the strategy lab does not know.

    A silently ignored typo would look like a strategy that stopped working, so
    an unknown symbol is an error rather than a shrug.
    """
    from stratlab.data import BY_SYMBOL

    names = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    unknown = [name for name in names if name not in BY_SYMBOL]
    if unknown:
        raise SystemExit(
            f"unknown symbol(s): {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(BY_SYMBOL))}"
        )
    if not names:
        raise SystemExit("--symbols was given but resolved to an empty list")
    return names


def _quote_service(config: BotConfig) -> QuoteService:
    """Live quotes for the traded symbols.

    The engine marks open positions with these. A decision still reads a
    completed daily bar and a fill still happens at the next bar's open.
    """
    from stratlab import data as price_data

    return QuoteService(price_data.BY_SYMBOL[symbol] for symbol in config.symbols)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper trading engine (simulator only).")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite path")
    parser.add_argument("--cycles", type=int, default=1, help="cycles to run then exit")
    parser.add_argument("--sleep", type=int, default=0, help="seconds between cycles")
    parser.add_argument("--strategy", default=CONFIG.strategy_name,
                        choices=["donchian-55/20", "sma-20/100", "buy-and-hold"],
                        help="strategy to run")
    parser.add_argument("--serve", action="store_true", help="start the FastAPI server")
    parser.add_argument("--forever", action="store_true", help="run until stopped")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--refresh", action="store_true",
                        help="download fresh candles for every symbol at startup")
    parser.add_argument("--refresh-interval", type=int, default=60,
                        help="minutes between automatic cache refreshes (default 60)")
    parser.add_argument("--refresh-only", action="store_true",
                        help="refresh the data, print provenance, then exit without trading")
    parser.add_argument("--capital", type=float, default=None,
                        help="override starting paper cash (default: the configured value)")
    parser.add_argument("--trade", action="store_true",
                        help="with --serve, also run the trading loop in the same process")
    parser.add_argument("--poll", type=int, default=None,
                        help="seconds between cycles when trading in the server (default 300)")
    parser.add_argument("--symbols", default=None,
                        help="comma-separated subset of the universe, e.g. RELIANCE,INFY "
                             "(default: all five)")
    parser.add_argument("--no-realtime", action="store_true",
                        help="mark positions from the daily bar close instead of live quotes")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = setup_logging(args.verbose)

    # Taken before anything opens the database or touches the cache, so a second
    # instance is refused without having written a single record.
    lock = InstanceLock(args.db)
    try:
        lock.acquire()
    except InstanceAlreadyRunning as exc:
        logger.error("%s", exc)
        return 2

    config = replace(
        CONFIG,
        strategy_name=args.strategy,
        refresh_interval_minutes=args.refresh_interval,
        **({"initial_cash": args.capital} if args.capital is not None else {}),
        **({"symbols": _parse_symbols(args.symbols)} if args.symbols else {}),
    )

    quotes = None if args.no_realtime else _quote_service(config)

    if args.serve:
        if not os.environ.get("STRATBOT_CONTROL_TOKEN"):
            logger.warning(
                "STRATBOT_CONTROL_TOKEN is unset, so emergency stop and resume are disabled. "
                "Set it before exposing this server to a network."
            )
        from stratbot.api import create_app

        import uvicorn

        logger.info("realtime quotes: %s", "off" if quotes is None else "on")
        db = Database(args.db)
        try:
            uvicorn.run(
                create_app(db=db, config=config, trade=args.trade, poll_seconds=args.poll,
                           quotes=quotes),
                host=args.host, port=args.port, log_level="info",
            )
        finally:
            lock.release()
        return 0

    db = Database(Path(args.db))
    feed = MarketDataFeed(config.symbols, refresh_interval_minutes=args.refresh_interval)
    feed.logger = logger
    engine = Engine(config, db, feed=feed, quotes=quotes)

    logger.info("provider=yfinance.download source=cache/%s refresh_interval=%dm",
                "data/cache", args.refresh_interval)
    if args.refresh or args.refresh_only:
        logger.info("refreshing market data at startup")
        engine.refresh_data(logger=logger)

    if args.refresh_only:
        logger.info("refresh-only: no trading performed")
        for symbol, prov in sorted(engine.feed.provenance.items()):
            logger.info("provenance: %s", prov.log_line())
        return 0

    logger.info("mode=paper strategy=%s symbols=%s", config.strategy_name, ",".join(config.symbols))
    logger.info("realtime quotes: %s", "off" if quotes is None else "on")
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
    try:
        engine.run_forever(cycles=cycles, sleep_seconds=args.sleep, logger=logger)
    finally:
        # Release on a clean exit; a crash is covered by the kernel dropping the
        # lock when the process dies, so there is no stale file to clear by hand.
        lock.release()

    account = engine.broker.get_account()
    logger.info("equity=%.2f cash=%.2f exposure=%.2f", account.equity, account.cash, account.exposure)
    logger.info("halted=%s reason=%s", engine.risk.halted, engine.risk.halt_reason.value if engine.risk.halt_reason else "-")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())