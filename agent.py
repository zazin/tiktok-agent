#!/usr/bin/env python3
"""
tiktok-agent — bridge between the ImageKit queue and a connected Android phone.

On each poll it:
  1. lists the ImageKit /tiktok folder (the pipeline's output),
  2. finds images whose fileId is not yet in the local state file,
  3. downloads each new image,
  4. adb-pushes it into the phone gallery,
  5. (optional) auto-posts it to TikTok over adb,
  6. records the fileId so it is never processed twice.

Run it once, or as a watch loop on the device-connected computer.

Credentials (read from .env, auto-loaded):
  - IMAGEKIT_PRIVATE_KEY

Usage (CLI):
    python agent.py --once
    python agent.py --once --auto-post
    python agent.py --watch --interval 60
    python agent.py --once --folder /tiktok --serial 827b946
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional


DEFAULT_FOLDER = "/tiktok"
DEFAULT_STATE = "agent_state.json"
DEFAULT_INTERVAL = 60


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


def process_once(
    *,
    folder: str,
    state_path: Path,
    serial: Optional[str],
    auto_post: bool,
    dest_dir: str,
) -> int:
    """Run one poll cycle. Returns the number of newly processed images."""
    from imagekit_source import list_images, download, ImageKitSourceError
    from adb_pusher import push_to_phone, PhonePushError
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
                    status = post(remote, caption=caption, serial=serial, auto_post=True)
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


def _cli() -> int:
    from env_loader import load_env
    load_env()

    parser = argparse.ArgumentParser(
        description="Poll ImageKit, push new images to a connected phone, optionally auto-post to TikTok."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Process new images once and exit (default)")
    mode.add_argument("--watch", action="store_true", help="Keep polling on an interval until interrupted")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"Watch poll interval in seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--folder", default=DEFAULT_FOLDER, help=f"ImageKit folder to watch (default: {DEFAULT_FOLDER})")
    parser.add_argument("--state", default=DEFAULT_STATE, help=f"Path to the processed-state JSON (default: {DEFAULT_STATE})")
    parser.add_argument("--serial", default=None, help="Target device serial (if multiple phones connected)")
    parser.add_argument("--dest", default="/sdcard/Pictures", help="Remote dir on the phone (default: /sdcard/Pictures)")
    parser.add_argument("--auto-post", action="store_true", help="Attempt to auto-post to TikTok via adb UI automation (brittle, opt-in)")
    args = parser.parse_args()

    state_path = Path(args.state)
    kw = dict(
        folder=args.folder,
        state_path=state_path,
        serial=args.serial,
        auto_post=args.auto_post,
        dest_dir=args.dest,
    )

    if args.watch:
        _log(f"Watching {args.folder} every {args.interval}s (Ctrl-C to stop)...")
        try:
            while True:
                process_once(**kw)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            _log("Stopped.")
            return 0
    else:
        process_once(**kw)
        return 0


if __name__ == "__main__":
    sys.exit(_cli())
