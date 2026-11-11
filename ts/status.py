"""A small file the watcher keeps current, so the page can say what it is doing right now."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

FILE = Path(os.environ.get("TS_STATUS", Path(__file__).resolve().parent.parent / "data" / "status.json"))
_lock = threading.Lock()
_state: dict = {}


def update(**fields) -> None:
    with _lock:
        _state.update(fields, written=time.time())
        tmp = FILE.with_suffix(f".{os.getpid()}.tmp")
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(_state))
        tmp.replace(FILE)


def read() -> dict:
    try:
        return json.loads(FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
