#!/usr/bin/env python3
"""
local_store — a one-file-per-message spool directory on local disk.

In HiveMQ mode there is no local state: the broker's persistent QoS-1 session is
the only queue, and a failed post/comment is acked (dropped) so it doesn't loop
forever — which also means the work item is lost. This module mirrors **every**
received message to its own JSON file the moment it arrives, so nothing is lost:

    receive → store(dir, key, payload)   # write <slug>-<hash>.json
    posted  → remove(dir, key)           # delete the file
    failed  → mark(dir, key, status)     # leave the file, record the outcome

So a success leaves no trace and the directory accumulates exactly the items that
still need attention (one JSON per item). Re-attempt them later with ``--retry``,
which iterates the surviving files.

It is deliberately generic (a keyed spool, no domain knowledge) so both the poster
(``agent.py``) and the commenter (``comment_agent.py``) share it — like the adb/UI
primitives (``tiktok_ui.py``) and the MQTT work-queue (``mqtt_queue.py``).

Each file holds::

    {"key": "<id or url>", "payload": {...}, "status": "pending",
     "error": null, "attempts": 1, "ts": 1700000000}

The poster keys by message id and stores the message ``fields`` as the payload;
the commenter keys by post URL and stores ``{post_url, comment}``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional


DEFAULT_POST_DIR = "queue_posts"
DEFAULT_COMMENT_DIR = "queue_comments"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _filename(key: str) -> str:
    """Deterministic, filesystem-safe filename for a key (so remove/mark can find it)."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:60].strip("_") or "item"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}.json"


def _path(directory: Path, key: str) -> Path:
    return directory / _filename(key)


def _read(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def store(directory: Path, key: str, payload: dict) -> None:
    """Write/refresh the spool file for one message, bumping its attempts count.

    Called the instant a message is received (and again at the start of each retry),
    so the work item is on disk before we touch the phone — a crash mid-post leaves it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = _path(directory, key)
    prev = _read(path) or {}
    path.write_text(
        json.dumps(
            {
                "key": key,
                "payload": payload,
                "status": "pending",
                "error": None,
                "attempts": int(prev.get("attempts", 0)) + 1,
                "ts": int(time.time()),
            },
            indent=2,
        )
    )


def mark(directory: Path, key: str, status: str, error: Optional[str] = None) -> None:
    """Record a non-success outcome on an existing spool file (the file stays on disk)."""
    path = _path(directory, key)
    entry = _read(path)
    if entry is None:
        return
    entry["status"] = status
    entry["error"] = error
    entry["ts"] = int(time.time())
    path.write_text(json.dumps(entry, indent=2))


def remove(directory: Path, key: str) -> None:
    """Delete a message's spool file (e.g. after a successful post). No-op if absent."""
    _path(directory, key).unlink(missing_ok=True)


def items(directory: Path) -> list[dict]:
    """Return every surviving spool entry, oldest-first (for the retry loop)."""
    if not directory.is_dir():
        return []
    entries = []
    for path in directory.glob("*.json"):
        entry = _read(path)
        if entry is not None and entry.get("key"):
            entries.append(entry)
    return sorted(entries, key=lambda e: e.get("ts", 0))
