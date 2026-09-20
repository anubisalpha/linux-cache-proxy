"""Reads the worker-pool status snapshot the supervisor writes for the web UI."""
import json
import time
from typing import Optional

from . import config


def read_worker_status(now: Optional[float] = None) -> dict:
    """The supervisor's latest snapshot, plus `available`: False if the file
    is missing/unreadable or older than ~3 sample intervals, i.e. the proxy
    isn't running and the numbers would be stale."""
    now = time.time() if now is None else now
    try:
        data = json.loads(config.WORKER_STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {"available": False, "workers": []}
    age = now - data.get("ts", 0)
    # Floor of 15s so a fast test interval doesn't flap between samples.
    data["available"] = age <= max(15.0, 3 * config.SAMPLE_INTERVAL)
    data["age"] = age
    data.setdefault("workers", [])
    return data
