"""Clear a stuck halt in the ledger.

    python tools_clear_halt.py

Goes through RiskManager.resume, so the clear is recorded as a risk event
instead of quietly deleting the rows.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stratbot.db import Database
from stratbot.risk import RiskManager

LEDGER = Path(__file__).resolve().parent / "data" / "bot.sqlite3"


def main() -> int:
    db = Database(LEDGER)
    RiskManager(db).resume()
    print("halt cleared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
