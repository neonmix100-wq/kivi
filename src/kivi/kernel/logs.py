"""Append-only JSONL logs.

JSONL keeps logs greppable and streamable without a logging stack. One file
per UTC day; each line is a complete JSON object.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonlLogger:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path_for(self, moment: datetime | None = None) -> Path:
        moment = moment or datetime.now(UTC)
        return self.directory / f"kivi-{moment:%Y%m%d}.jsonl"

    def write(self, record: dict[str, Any]) -> Path:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        path = self.path_for()
        with self._lock, path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        return path

    def read(self, path: Path | None = None) -> list[dict[str, Any]]:
        path = path or self.path_for()
        if not path.is_file():
            return []
        records = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records
