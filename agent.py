#!/usr/bin/env python3
"""
tiktok-agent — bridge between the work queue and a connected Android phone.

The default source of truth is an Airtable "Posts" table the tiktok-pipeline
writes to (see tiktok-pipeline/docs/airtable.md). On each poll it:
  1. lists Airtable records with Status == "pending" (oldest first),
  2. downloads each record's image from its ImageKit ImageURL,
  3. adb-pushes it into the phone gallery,
  4. (optional) auto-posts it to TikTok over adb,
  5. flips the record's Status to "posted" / "failed" so it isn't reprocessed.

Pass --source imagekit to use the legacy ImageKit-folder queue instead, which
dedups against a local JSON state file.

Run it once, or as a watch loop on the device-connected computer.

Credentials (read from .env, auto-loaded):
  - AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME  (airtable source)
  - IMAGEKIT_PRIVATE_KEY                                     (imagekit source)

Usage (CLI):
    python agent.py --once
    python agent.py --once --no-auto-post
    python agent.py --watch --interval 60
    python agent.py --once --source imagekit --folder /tiktok --serial 827b946
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
DEFAULT_SOURCE = "airtable"


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


def process_once(
    *,
    source: str,
    folder: str,
    state_path: Path,
    serial: Optional[str],
    auto_post: bool,
    dest_dir: str,
) -> int:
    """Run one poll cycle. Returns the number of newly processed images."""
    if source == "airtable":
        return _process_airtable(serial=serial, auto_post=auto_post, dest_dir=dest_dir)
    return _process_imagekit(
        folder=folder, state_path=state_path, serial=serial, auto_post=auto_post, dest_dir=dest_dir
    )


def _process_airtable(*, serial: Optional[str], auto_post: bool, dest_dir: str) -> int:
    """Poll the Airtable queue: post each pending record and flip its Status."""
    from airtable_source import list_pending, update_status, AirtableSourceError
    from imagekit_source import download, ImageKitSourceError
    from adb_pusher import push_to_phone, PhonePushError
    from tiktok_poster import post, TikTokPostError

    try:
        records = list_pending()
    except AirtableSourceError as e:
        _log(f"List failed: {e}")
        return 0

    _log(f"Poll: {len(records)} pending in Airtable")

    def _set_status(rec_id: str, status: str) -> None:
        try:
            update_status(rec_id, status)
        except AirtableSourceError as e:
            _log(f"  status update failed for {rec_id} -> {status}: {e}")

    done = 0
    for rec in records:
        rec_id = rec.get("id")
        fields = rec.get("fields") or {}
        if not rec_id:
            continue
        url = fields.get("ImageURL")
        name = fields.get("ImagePath") or (url.rsplit("/", 1)[-1] if url else None) or f"{rec_id}.jpg"

        if not url:
            _log(f"  SKIP {rec_id}: no ImageURL")
            _set_status(rec_id, "failed")
            done += 1
            continue

        try:
            with tempfile.TemporaryDirectory() as tmp:
                local = download(url, Path(tmp) / name)
                remote = push_to_phone(str(local), dest_dir=dest_dir, serial=serial)
                _log(f"  pushed {name} -> {remote}")

                if not auto_post:
                    _log(f"  pushed only (--no-auto-post); leaving {rec_id} pending")
                else:
                    status = post(
                        remote,
                        caption=fields.get("Caption"),
                        description=fields.get("Description"),
                        serial=serial,
                        auto_post=True,
                    )
                    _log(f"  tiktok: {status}")
                    _set_status(rec_id, "posted" if status == "posted" else "failed")
        except (ImageKitSourceError, PhonePushError, TikTokPostError) as e:
            _log(f"  FAILED {name}: {e}")
            _set_status(rec_id, "failed")

        done += 1

    return done


def _process_imagekit(
    *,
    folder: str,
    state_path: Path,
    serial: Optional[str],
    auto_post: bool,
    dest_dir: str,
) -> int:
    """Legacy poll cycle: dedup the ImageKit folder against the local state file."""
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


def _catch_up_airtable() -> int:
    """Flip every current pending Airtable row to 'posted' without posting it."""
    from airtable_source import list_pending, update_status, AirtableSourceError

    try:
        records = list_pending()
    except AirtableSourceError as e:
        _log(f"Catch-up list failed: {e}")
        return 0

    n = 0
    for rec in records:
        rec_id = rec.get("id")
        if not rec_id:
            continue
        try:
            update_status(rec_id, "posted")
            n += 1
        except AirtableSourceError as e:
            _log(f"  catch-up failed for {rec_id}: {e}")
    _log(f"Catch-up: marked {n} pending record(s) as posted (skipped without posting).")
    return n


def catch_up(*, source: str, folder: str, state_path: Path) -> int:
    """
    Skip the current backlog WITHOUT posting it.

    Run this once before starting an always-on auto-posting watch so the daemon
    only posts items added from now on. For the airtable source this flips every
    current pending row to "posted"; for imagekit it records each fileId as seen.
    Returns the number of items newly marked.
    """
    if source == "airtable":
        return _catch_up_airtable()

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


def _cli() -> int:
    from env_loader import load_env
    load_env()

    parser = argparse.ArgumentParser(
        description="Poll the work queue, push new images to a connected phone, and auto-post them to TikTok."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Process new images once and exit (default)")
    mode.add_argument("--watch", action="store_true", help="Keep polling on an interval until interrupted")
    parser.add_argument(
        "--source",
        choices=("airtable", "imagekit"),
        default=DEFAULT_SOURCE,
        help=f"Work queue to read from (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"Watch poll interval in seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--folder", default=DEFAULT_FOLDER, help=f"ImageKit folder to watch (imagekit source only; default: {DEFAULT_FOLDER})")
    parser.add_argument("--state", default=DEFAULT_STATE, help=f"Path to the processed-state JSON (imagekit source only; default: {DEFAULT_STATE})")
    parser.add_argument("--serial", default=None, help="Target device serial (if multiple phones connected)")
    parser.add_argument("--dest", default="/sdcard/Pictures", help="Remote dir on the phone (default: /sdcard/Pictures)")
    parser.add_argument(
        "--auto-post",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-post each new image to TikTok (default: on; use --no-auto-post to only push to the phone)",
    )
    parser.add_argument(
        "--catch-up",
        action="store_true",
        help="Skip the current backlog WITHOUT posting, then exit (run before an always-on watch)",
    )
    args = parser.parse_args()

    state_path = Path(args.state)

    if args.catch_up:
        catch_up(source=args.source, folder=args.folder, state_path=state_path)
        return 0

    kw = dict(
        source=args.source,
        folder=args.folder,
        state_path=state_path,
        serial=args.serial,
        auto_post=args.auto_post,
        dest_dir=args.dest,
    )

    if args.watch:
        where = args.folder if args.source == "imagekit" else "Airtable"
        _log(f"Watching {where} every {args.interval}s (Ctrl-C to stop)...")
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
