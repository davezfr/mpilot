from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class LockedJsonStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._lock_depth = 0
        self._lock_handle = None

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"watches": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self._quarantine_corrupt_file()
            return {"watches": []}
        watches = payload.get("watches")
        if not isinstance(watches, list):
            return {"watches": []}
        return {"watches": watches}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)

    def _quarantine_corrupt_file(self) -> None:
        target = self.path.with_suffix(self.path.suffix + ".corrupt")
        if target.exists():
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            target = self.path.with_suffix(self.path.suffix + f".corrupt-{timestamp}")
        with contextlib.suppress(OSError):
            self.path.replace(target)

    @contextlib.contextmanager
    def _store_lock(self):
        with self._lock:
            if self._lock_depth == 0:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.path.with_suffix(self.path.suffix + ".lock").open("a+")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                except Exception:
                    handle.close()
                    raise
                self._lock_handle = handle
            self._lock_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._lock_depth -= 1
                if self._lock_depth == 0:
                    handle = self._lock_handle
                    self._lock_handle = None
                    if handle is not None:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_UN)
                        finally:
                            handle.close()
