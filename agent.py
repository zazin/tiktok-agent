#!/usr/bin/env python3
"""
tiktok-agent — bridge between the work queue and a connected Android phone.

The default source of truth is a HiveMQ (MQTT) work topic the tiktok-pipeline
publishes to. On each poll it:
  1. drains the queued MQTT messages (oldest first, persistent QoS-1 session),
  2. downloads each message's image from its ImageKit ImageURL,
  3. adb-pushes it into the phone gallery,
  4. (optional) auto-posts it to TikTok over adb,
  5. reports the outcome ("posted" / "failed") to the status topic and acks the
     message so it isn't redelivered.

Pass --source imagekit to use the legacy ImageKit-folder queue instead, which
dedups against a local JSON state file.

Run it once, or as a watch loop on the device-connected computer.

Credentials (read from .env, auto-loaded):
  - HIVEMQ_HOST, HIVEMQ_USERNAME, HIVEMQ_PASSWORD, HIVEMQ_TOPIC, ...  (hivemq source)
  - IMAGEKIT_PRIVATE_KEY                                              (imagekit source)

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
DEFAULT_SOURCE = "hivemq"
DEFAULT_STORE_DIR = "queue_posts"


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


def _name_for(rec_id: Optional[str], fields: dict) -> str:
    """Best-effort local filename for a message: ImagePath, else URL tail, else id."""
    url = fields.get("ImageURL")
    return fields.get("ImagePath") or (url.rsplit("/", 1)[-1] if url else None) or f"{rec_id}.jpg"


def _post_record(fields: dict, *, serial: Optional[str], auto_post: bool, dest_dir: str) -> str:
    """Download → push → (optionally) post one message's image.

    Returns post()'s status ("posted"/"needs_manual"/"composer_open"), or "pushed"
    when auto_post is off. Lets ImageKit/PhonePush/TikTokPost errors propagate so the
    caller decides how to report/park them. Shared by the drain, watch, and retry paths.
    """
    from imagekit_source import download
    from adb_pusher import push_to_phone
    from tiktok_poster import post

    rec_id = fields.get("id")
    name = _name_for(rec_id, fields)
    with tempfile.TemporaryDirectory() as tmp:
        local = download(fields["ImageURL"], Path(tmp) / name)
        remote = push_to_phone(str(local), dest_dir=dest_dir, serial=serial)
        _log(f"  pushed {name} -> {remote}")
        if not auto_post:
            return "pushed"
        status = post(
            remote,
            caption=fields.get("Caption"),
            description=fields.get("Description"),
            serial=serial,
            auto_post=True,
            account=fields.get("Account"),  # switch to this @handle first (if set)
        )
        _log(f"  tiktok: {status}")
        return status


def process_once(
    *,
    source: str,
    folder: str,
    state_path: Path,
    serial: Optional[str],
    auto_post: bool,
    dest_dir: str,
    store_path: Path,
) -> int:
    """Run one poll cycle. Returns the number of newly processed images."""
    if source == "hivemq":
        return _process_hivemq(
            serial=serial, auto_post=auto_post, dest_dir=dest_dir, store_path=store_path
        )
    return _process_imagekit(
        folder=folder, state_path=state_path, serial=serial, auto_post=auto_post, dest_dir=dest_dir
    )


def _process_hivemq(*, serial: Optional[str], auto_post: bool, dest_dir: str, store_path: Path) -> int:
    """Poll the HiveMQ queue: post each pending message and report its status.

    Drains the backlog, posts each message, and acks (via update_status) only the
    ones that reach a terminal state. Unacked messages (the --no-auto-post path,
    or a crash) are released back to the broker by close() and redelivered next
    poll, so they are effectively left pending until actually posted.

    Every received message is mirrored to its own JSON file in the local spool dir
    the instant it arrives (so nothing is lost if the broker then drops it). The file
    is deleted only on a successful post; a non-"posted" outcome or an error leaves it
    on disk (status recorded) to be re-attempted later with --retry.
    """
    import local_store
    from hivemq_source import list_pending, update_status, close, HiveMQSourceError
    from imagekit_source import ImageKitSourceError
    from adb_pusher import PhonePushError
    from tiktok_poster import TikTokPostError

    try:
        try:
            records = list_pending()
        except HiveMQSourceError as e:
            _log(f"List failed: {e}")
            return 0

        _log(f"Poll: {len(records)} pending in HiveMQ")

        def _set_status(rec_id: str, status: str) -> None:
            try:
                update_status(rec_id, status)
            except HiveMQSourceError as e:
                _log(f"  status update failed for {rec_id} -> {status}: {e}")

        done = 0
        for rec in records:
            rec_id = rec.get("id")
            fields = rec.get("fields") or {}
            if not rec_id:
                continue
            name = _name_for(rec_id, fields)

            if not fields.get("ImageURL"):
                _log(f"  SKIP {rec_id}: no ImageURL")
                _set_status(rec_id, "failed")  # nothing to retry without a URL
                done += 1
                continue

            # Always store on receive, before touching the phone, so a crash can't lose it.
            local_store.store(store_path, rec_id, fields)
            try:
                status = _post_record(fields, serial=serial, auto_post=auto_post, dest_dir=dest_dir)
                if not auto_post:
                    _log(f"  pushed only (--no-auto-post); leaving {rec_id} pending")
                elif status == "posted":
                    local_store.remove(store_path, rec_id)  # success → delete the file
                    _set_status(rec_id, "posted")
                else:
                    local_store.mark(store_path, rec_id, status)
                    _log(f"  kept {rec_id} ({status}) in {store_path} for retry")
                    _set_status(rec_id, "failed")
            except (ImageKitSourceError, PhonePushError, TikTokPostError) as e:
                _log(f"  FAILED {name}: {e}")
                local_store.mark(store_path, rec_id, "failed", error=str(e))
                _log(f"  kept {rec_id} in {store_path} for retry")
                _set_status(rec_id, "failed")

            done += 1

        return done
    finally:
        close()


def _watch_hivemq(*, serial: Optional[str], auto_post: bool, dest_dir: str, store_path: Path) -> None:
    """Event-driven watch: react to each HiveMQ message the instant it arrives.

    Unlike the --once drain, this holds a persistent subscription (no poll
    interval) and dispatches each message to a handler that downloads, pushes, and
    (optionally) posts it. The handler's return value tells hivemq_source whether
    to publish a status + ack the message.

    As in the drain path, every message is mirrored to its own JSON file on receive;
    the file is deleted on a successful post and kept otherwise for --retry.
    """
    import local_store
    from hivemq_source import watch, HiveMQSourceError
    from imagekit_source import ImageKitSourceError
    from adb_pusher import PhonePushError
    from tiktok_poster import TikTokPostError

    def handler(rec: dict) -> Optional[str]:
        rec_id = rec.get("id")
        fields = rec.get("fields") or {}
        name = _name_for(rec_id, fields)
        _log(f"Message {rec_id}")
        if not fields.get("ImageURL"):
            _log(f"  SKIP {rec_id}: no ImageURL")
            return "failed"  # nothing to retry without a URL
        # Always store on receive, before touching the phone, so a crash can't lose it.
        local_store.store(store_path, rec_id, fields)
        try:
            status = _post_record(fields, serial=serial, auto_post=auto_post, dest_dir=dest_dir)
            if not auto_post:
                _log(f"  pushed only (--no-auto-post); leaving {rec_id} pending")
                return None
            if status == "posted":
                local_store.remove(store_path, rec_id)  # success → delete the file
                return "posted"
            local_store.mark(store_path, rec_id, status)
            _log(f"  kept {rec_id} ({status}) in {store_path} for retry")
            return "failed"
        except (ImageKitSourceError, PhonePushError, TikTokPostError) as e:
            _log(f"  FAILED {name}: {e}")
            local_store.mark(store_path, rec_id, "failed", error=str(e))
            _log(f"  kept {rec_id} in {store_path} for retry")
            return "failed"

    _log("Watching HiveMQ topic (event-driven, push; Ctrl-C to stop)...")
    try:
        watch(handler)
    except HiveMQSourceError as e:
        _log(f"Watch failed: {e}")
    _log("Stopped.")


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


def _catch_up_hivemq() -> int:
    """Drain the current HiveMQ backlog and mark each 'posted' without posting it."""
    from hivemq_source import list_pending, update_status, close, HiveMQSourceError

    try:
        try:
            records = list_pending()
        except HiveMQSourceError as e:
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
            except HiveMQSourceError as e:
                _log(f"  catch-up failed for {rec_id}: {e}")
        _log(f"Catch-up: marked {n} pending message(s) as posted (skipped without posting).")
        return n
    finally:
        close()


def _retry_posts(*, serial: Optional[str], auto_post: bool, dest_dir: str, store_path: Path) -> int:
    """Re-attempt every post still sitting in the local spool dir (one JSON per item).

    Talks only to local disk (no HiveMQ): re-downloads, re-pushes, and re-posts each
    surviving file. On "posted" the file is deleted; otherwise it stays (its status and
    attempts count are updated). Returns the number that succeeded.
    """
    import local_store
    from imagekit_source import ImageKitSourceError
    from adb_pusher import PhonePushError
    from tiktok_poster import TikTokPostError

    entries = local_store.items(store_path)
    _log(f"Retry: {len(entries)} stored post(s) in {store_path}")

    succeeded = 0
    for entry in entries:
        rec_id = entry["key"]
        fields = entry.get("payload") or {}
        name = _name_for(rec_id, fields)
        if not fields.get("ImageURL"):
            _log(f"  SKIP {rec_id}: no ImageURL in stored payload")
            continue
        local_store.store(store_path, rec_id, fields)  # bump attempts for this retry
        _log(f"Retry {rec_id} (attempt {int(entry.get('attempts', 0)) + 1})")
        try:
            status = _post_record(fields, serial=serial, auto_post=auto_post, dest_dir=dest_dir)
            if status == "posted":
                local_store.remove(store_path, rec_id)
                succeeded += 1
            else:
                local_store.mark(store_path, rec_id, status)
                _log(f"  still {status}; kept in {store_path}")
        except (ImageKitSourceError, PhonePushError, TikTokPostError) as e:
            _log(f"  FAILED {name}: {e}")
            local_store.mark(store_path, rec_id, "failed", error=str(e))

    _log(f"Retry: {succeeded} posted, {len(local_store.items(store_path))} still stored.")
    return succeeded


def catch_up(*, source: str, folder: str, state_path: Path) -> int:
    """
    Skip the current backlog WITHOUT posting it.

    Run this once before starting an always-on auto-posting watch so the daemon
    only posts items added from now on. For the hivemq source this drains and
    marks every current pending message "posted"; for imagekit it records each
    fileId as seen.
    Returns the number of items newly marked.
    """
    if source == "hivemq":
        return _catch_up_hivemq()

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
    mode.add_argument("-o", "--once", action="store_true", help="Process new images once and exit (default)")
    mode.add_argument("-w", "--watch", action="store_true", help="Keep running until interrupted (hivemq: event-driven push; imagekit: poll on --interval)")
    parser.add_argument(
        "--source",
        choices=("hivemq", "imagekit"),
        default=DEFAULT_SOURCE,
        help=f"Work queue to read from (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"Watch poll interval in seconds (imagekit source only; default: {DEFAULT_INTERVAL})")
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
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Re-attempt every post still in the local spool dir, then exit (no HiveMQ)",
    )
    parser.add_argument(
        "--store-dir",
        default=DEFAULT_STORE_DIR,
        help=f"Local spool dir holding one JSON per pending post (default: {DEFAULT_STORE_DIR})",
    )
    args = parser.parse_args()

    state_path = Path(args.state)
    store_path = Path(args.store_dir)

    if args.catch_up:
        catch_up(source=args.source, folder=args.folder, state_path=state_path)
        return 0

    if args.retry:
        _retry_posts(
            serial=args.serial, auto_post=args.auto_post, dest_dir=args.dest, store_path=store_path
        )
        return 0

    kw = dict(
        source=args.source,
        folder=args.folder,
        state_path=state_path,
        serial=args.serial,
        auto_post=args.auto_post,
        dest_dir=args.dest,
        store_path=store_path,
    )

    if args.watch:
        if args.source == "hivemq":
            # MQTT is push: hold a persistent subscription and react instantly.
            # No --interval (that only applies to the imagekit folder poll).
            _watch_hivemq(
                serial=args.serial, auto_post=args.auto_post, dest_dir=args.dest, store_path=store_path
            )
            return 0
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
