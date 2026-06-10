#!/usr/bin/env python3
"""
Legacy ImageKit-folder orchestration for tiktok-agent (`--source imagekit`).

Split out of agent.py: the default/primary path is the HiveMQ source (still in
agent.py); this module holds the older ImageKit-folder queue, which dedups
processed images against a local JSON state file (agent_state.json) instead of
relying on a broker session. Kept separate so the legacy path doesn't bloat the
main orchestrator — agent.py dispatches here when `--source imagekit` is passed.

State file shape: {"processed": {fileId: {name, status, ts, ...}}}. An image is
processed exactly once (its fileId becomes a key); state is saved after each item
so a crash mid-batch doesn't reprocess completed work.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Optional


def _log(msg: str) -> None:
    print(msg, flush=True)


def _load_state(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            _log(f"Warning: could not read state file {path}; starting fresh.")
    return {"processed": {}}


def _save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


def _caption_from_file(meta: dict) -> Optional[str]:
    """Best-effort caption: ImageKit customMetadata.caption, else first tag."""
    cm = meta.get("customMetadata") or {}
    if isinstance(cm, dict) and cm.get("caption"):
        return str(cm["caption"])
    tags = meta.get("tags") or []
    if isinstance(tags, list) and tags:
        return str(tags[0])
    return None


def _description_from_file(meta: dict) -> Optional[str]:
    """ImageKit customMetadata.description, if present."""
    cm = meta.get("customMetadata") or {}
    if isinstance(cm, dict) and cm.get("description"):
        return str(cm["description"])
    return None


def process_imagekit(
    *,
    folder: str,
    state_path: Path,
    serial: Optional[str],
    auto_post: bool,
    dest_dir: str,
) -> int:
    """Legacy poll cycle: dedup the ImageKit folder against the local state file."""
    from imagekit_source import list_images, download, ImageKitSourceError
    from .adb_pusher import push_to_phone, PhonePushError
    from tiktok_poster import post, TikTokPostError

    state = _load_state(state_path)
    processed = state.setdefault("processed", {})

    try:
        files = list_images(folder=folder)
    except ImageKitSourceError as e:
        _log(f"List failed: {e}")
        return 0

    # Oldest first so posting order matches creation order.
    new_files = [f for f in reversed(files) if f.get("fileId") not in processed]
    _log(f"Poll: {len(files)} in {folder}, {len(new_files)} new")

    done = 0
    for meta in new_files:
        file_id = meta.get("fileId")
        name = meta.get("name") or f"{file_id}.jpg"
        url = meta.get("url")
        if not file_id or not url:
            continue

        entry = {"name": name, "status": "pending", "ts": int(time.time())}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                local = download(url, Path(tmp) / name)
                remote = push_to_phone(str(local), dest_dir=dest_dir, serial=serial)
                _log(f"  pushed {name} -> {remote}")

                if auto_post:
                    caption = _caption_from_file(meta)
                    description = _description_from_file(meta)
                    status = post(
                        remote,
                        caption=caption,
                        description=description,
                        serial=serial,
                        auto_post=True,
                    )
                    entry["status"] = status
                    _log(f"  tiktok: {status}")
                else:
                    entry["status"] = "pushed"
        except (ImageKitSourceError, PhonePushError, TikTokPostError) as e:
            entry["status"] = "failed"
            entry["error"] = str(e)
            _log(f"  FAILED {name}: {e}")

        processed[file_id] = entry
        _save_state(state_path, state)  # persist after each item (crash-safe)
        done += 1

    return done


def catch_up_imagekit(*, folder: str, state_path: Path) -> int:
    """Record every current ImageKit-folder image as seen, WITHOUT posting it."""
    from imagekit_source import list_images, ImageKitSourceError

    state = _load_state(state_path)
    processed = state.setdefault("processed", {})
    try:
        files = list_images(folder=folder)
    except ImageKitSourceError as e:
        _log(f"Catch-up list failed: {e}")
        return 0

    n = 0
    for meta in files:
        fid = meta.get("fileId")
        if fid and fid not in processed:
            processed[fid] = {"name": meta.get("name"), "status": "catch-up", "ts": int(time.time())}
            n += 1
    _save_state(state_path, state)
    _log(f"Catch-up: marked {n} existing image(s) as seen ({len(files)} in {folder}).")
    return n
