#!/usr/bin/env python
"""Paper trading engine entry point.

    python run_bot.py --cycles 3          # run three cycles against the live feed
    python run_bot.py --serve             # start the API and dashboard on :8000
    python run_bot.py --db test.sqlite3   # use a throwaway database
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from stratbot.config import CONFIG, DB_PATH
from stratbot.db import Database
from stratbot.engine import Engine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paper trading engine (simulator only).")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite path")
    parser.add_argument("--cycles", type=int, default=1, help="cycles to run then exit")
    parser.add_argument("--sleep", type=int, default=0, help="seconds between cycles")
    parser.add_argument("--serve", action="store_true", help="start the FastAPI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.serve:
        if not os.environ.get("STRATBOT_CONTROL_TOKEN"):
            print(
                "warning: STRATBOT_CONTROL_TOKEN is unset, so emergency stop and resume\n"
                "         are disabled. Set it before exposing this server to a network.",
                file=sys.stderr,
            )
        from stratbot.api import create_app

        import uvicorn

        db = Database(args.db)
        uvicorn.run(create_app(db=db), host=args.host, port=args.port)
        return 0

    config = CONFIG
    db = Database(Path(args.db))
    engine = Engine(config, db)

    print(f"mode=paper  strategy={config.strategy_name}  symbols={','.join(config.symbols)}")
    print(f"limits: max_position={config.limits.max_position_value:,.0f} "
          f"max_exposure={config.limits.max_total_exposure:,.0f} "
          f"max_daily_loss={config.limits.max_daily_loss:,.0f} "
          f"stop_loss={config.limits.stop_loss_pct:.1%}")
    print(f"db: {Path(args.db).resolve()}\n")

    engine.run_forever(cycles=args.cycles, sleep_seconds=args.sleep)

    account = engine.broker.get_account()
    print(f"\nequity   {account.equity:,.2f}")
    print(f"cash      {account.cash:,.2f}")
    print(f"exposure  {account.exposure:,.2f}")
    print(f"positions {sum(1 for p in account.positions.values() if p.is_open())} open")
    print(f"halted    {engine.risk.halted} ({engine.risk.halt_reason.value if engine.risk.halt_reason else '-'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())