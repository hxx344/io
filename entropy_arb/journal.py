"""Durable cycle checkpoint and process lock (Windows and Unix)."""
from __future__ import annotations
import json
import os
from pathlib import Path


class CycleJournal:
    def __init__(self, filename: str):
        self.path = Path(filename)
        self.lock = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(str(self.path) + ".lock", "a+b")
        self.lock.seek(0, os.SEEK_END)
        if self.lock.tell() == 0:
            self.lock.write(b"0")
            self.lock.flush()
        self.lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close()
            self.lock = None
            raise RuntimeError(f"Cycle already running: {self.path}") from exc

    def check_clean(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise RuntimeError(f"Unreadable cycle state: {self.path}") from exc
        if not isinstance(data, dict) or data.get("state") != "IDLE":
            raise RuntimeError(f"Unfinished cycle in {self.path}; reconcile Entropy orders/position "
                               "before archiving this checkpoint and restarting")

    def save(self, payload: dict):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def close(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None
