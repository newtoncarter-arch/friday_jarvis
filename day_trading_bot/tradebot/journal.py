"""Append-only CSV journal of every signal, Grok verdict, and order."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ["time", "symbol", "side", "entry", "stop", "target", "qty", "event",
          "grok_confidence", "detail"]


class Journal:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("w", newline="") as f:
                csv.writer(f).writerow(FIELDS)

    def log(self, **row) -> None:
        row.setdefault("time", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with self.path.open("a", newline="") as f:
            csv.DictWriter(f, FIELDS, extrasaction="ignore").writerow(row)
