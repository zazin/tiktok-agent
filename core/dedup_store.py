#!/usr/bin/env python3
"""
dedup_store — a TTL-windowed "already done this" record on local disk.

The server half (tiktok-pipeline) occasionally republishes the same work message
to HiveMQ. Because the broker is the only queue and the device acts on every
message it drains, a duplicate means a double TikTok post / double comment. This
module remembers the key of every item we **actually completed** for a window
(default 1 day) so a repeat within that window can be dropped without acting again.

    success → record(path, key, ttl=...)        # remember it (now)
    receive → seen(path, key, ttl=...) → bool   # repeat within the window? → drop

Recording happens at **success** (not on receive) on purpose: the existing
redelivery contracts — ``--no-auto-post`` (never acked → broker redelivers the same
id) and ``--retry`` (local re-attempt of failed items) — never reach success, so
they are never deduped and keep working.

One JSON file per stream (posts vs comments), shape::

    {"seen": {"<key>": 1700000000, "...": 1700000123}}

Values are Unix seconds (``int(time.time())``). Entries older than the TTL are
ignored by ``seen`` and pruned by ``record``, so the file self-trims and a key
forgotten after the window can be processed again. Mirrors the load/prune/save
style of ``core/imagekit_agent.py``'s ``agent_state.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


DEFAULT_TTL = 86400  # 1 day, in seconds
DEFAULT_POST_PATH = "dedup_posts.json"
DEFAULT_COMMENT_PATH = "dedup_comments.json"


def default_ttl() -> int:
    """The dedup window in seconds: ``$DEDUP_TTL`` if set and valid, else 1 day."""
    raw = os.getenv("DEDUP_TTL")
    if raw is None:
        return DEFAULT_TTL
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_TTL


def content_key(*parts: str) -> str:
    """Stable key (sha1 hex) for a message from its identifying parts.

    Used where the payload has no id field — e.g. a comment is keyed on
    ``content_key(post_url, comment)`` so a *different* comment on the same post
    isn't mistaken for a duplicate.
    """
    h = hashlib.sha1()
    for part in parts:
        h.update((part or "").encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    seen = data.get("seen") if isinstance(data, dict) else None
    return seen if isinstance(seen, dict) else {}


def _save(path: Path, seen: dict) -> None:
    path.write_text(json.dumps({"seen": seen}, indent=2))


def seen(path: Path, key: str, *, ttl: int) -> bool:
    """True if ``key`` was recorded within the last ``ttl`` seconds.

    ``ttl <= 0`` disables dedup (always False). Read-only: it does not write, so it
    stays cheap on the hot receive path (``record`` does the pruning).
    """
    if ttl <= 0 or not key:
        return False
    ts = _load(path).get(key)
    if not isinstance(ts, (int, float)):
        return False
    return (int(time.time()) - int(ts)) < ttl


def record(path: Path, key: str, *, ttl: int) -> None:
    """Remember ``key`` as completed now, pruning entries older than ``ttl``.

    ``ttl <= 0`` is a no-op (dedup disabled). Pruning on every write keeps the file
    from growing without bound.
    """
    if ttl <= 0 or not key:
        return
    now = int(time.time())
    store = _load(path)
    fresh = {k: ts for k, ts in store.items() if isinstance(ts, (int, float)) and (now - int(ts)) < ttl}
    fresh[key] = now
    _save(path, fresh)
