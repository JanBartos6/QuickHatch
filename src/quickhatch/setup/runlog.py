"""Structured run artifact writer for remote setup."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class SetupRunLogger:
    """Append-only JSONL logger for remote setup runs."""

    def __init__(self, run_id: str, *, root: Path | None = None) -> None:
        self.run_id = run_id
        self.root = root or (Path.home() / ".quickhatch" / "setup-runs")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{run_id}.jsonl"

    def record(self, event_type: str, **payload: Any) -> None:
        entry = {
            "timestamp": time.time(),
            "run_id": self.run_id,
            "event_type": event_type,
            **payload,
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            # Best-effort debugging aid — never let logging break the install.
            pass
