from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
from typing import Iterator


class JsonStore:
    """JSON file persistence with file locking."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(".lock")

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    @contextmanager
    def locked(self) -> Iterator[dict]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.touch(exist_ok=True)
        with self._lock_path.open("r") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                data = self.load()
                yield data
                self.save(data)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
