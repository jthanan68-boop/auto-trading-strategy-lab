"""Rewrite the day baseline in the ledger.

    python tools_fix_daystart.py 2026-10-21 500000

The values go through Database.set_state, so they are JSON encoded. A bare
value written straight into bot_state cannot be read back by get_state, and
the read failure stops the trading loop.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stratbot.db import Database

LEDGER = Path(__file__).resolve().parent / "data" / "bot.sqlite3"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise SystemExit("usage: tools_fix_daystart.py <YYYY-MM-DD> <day_start_equity>")
    day, equity = argv[1], float(argv[2])
    db = Database(LEDGER)
    db.set_state("day_start", day)
    db.set_state("day_start_equity", equity)
    print(f"day_start={day} day_start_equity={equity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
