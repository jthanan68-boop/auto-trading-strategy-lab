"""The lock in front of live trading.

Phase 4 is not implemented, deliberately: the strategy has no measured edge, and
a live adapter cannot be tested against a real broker from here. What *can* be
built now is the gate that stops a live adapter from ever being switched on by
accident.

The gate requires three independent factors. Any one missing means locked, and
every failure path is written to fail closed, because the cost of unlocking by
accident is real money.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ENV_FLAG = "STRATBOT_ALLOW_LIVE"
MARKER_NAME = "LIVE_UNLOCK"
EXPECTED_FLAG = "yes-do-i-really-want-this"


@dataclass
class GateResult:
    unlocked: bool
    factors: dict[str, bool]
    missing: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        if self.unlocked:
            return "all unlock factors present"
        return "locked, missing: " + ", ".join(self.missing)


@dataclass
class LiveGate:
    """Three-factor unlock for live trading.

    Deliberately inconvenient. Any single missing factor keeps it locked, so a
    stray env var, a leftover marker file, or a curious flag cannot combine into
    an accidental unlock on their own.
    """

    project_dir: Path
    cli_flag: bool = False

    @property
    def marker_path(self) -> Path:
        return self.project_dir / "data" / MARKER_NAME

    def check(self) -> GateResult:
        factors = {
            "env_flag": os.environ.get(ENV_FLAG) == EXPECTED_FLAG,
            "marker_file": self.marker_path.is_file(),
            "cli_flag": bool(self.cli_flag),
        }
        missing = [name for name, present in factors.items() if not present]
        return GateResult(unlocked=not missing, factors=factors, missing=missing)

    # ---- operator actions ----------------------------------------------
    def create_marker(self) -> Path:
        """Writing the marker is still not enough on its own."""
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(
            f"Created {datetime.now(timezone.utc).isoformat()}\n"
            "This file alone does not enable live trading. All three factors are required.\n",
            encoding="utf-8",
        )
        return self.marker_path

    def remove_marker(self) -> bool:
        if self.marker_path.is_file():
            self.marker_path.unlink()
            return True
        return False

    def describe(self) -> str:
        result = self.check()
        lines = ["live trading gate", f"  state: {'UNLOCKED' if result.unlocked else 'LOCKED'}", ""]
        for name, present in result.factors.items():
            lines.append(f"  [{'x' if present else ' '}] {name}")
        lines.append("")
        lines.append(f"  {ENV_FLAG} must equal '{EXPECTED_FLAG}'")
        lines.append(f"  {self.marker_path} must exist")
        lines.append("  --live must be passed on the command line")
        return "\n".join(lines)