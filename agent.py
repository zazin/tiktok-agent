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
import logging
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
DEFAULT_DEDUP_PATH = "dedup_posts.json"

logger = logging.getLogger(__name__)


def _log(msg: str) -> None:
    logger.info(msg)


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
    from core.adb_pusher import push_to_phone
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
    dedup_path: Optional[Path] = None,
    dedup_ttl: int = 0,
) -> int:
    """Run one poll cycle. Returns the number of newly processed images."""
    if source == "hivemq":
        return _process_hivemq(
            serial=serial, auto_post=auto_post, dest_dir=dest_dir, store_path=store_path,
            dedup_path=dedup_path, dedup_ttl=dedup_ttl,
        )
    from core.imagekit_agent import process_imagekit
    return process_imagekit(
        folder=folder, state_path=state_path, serial=serial, auto_post=auto_post, dest_dir=dest_dir
    )


def _process_hivemq(
    *,
    serial: Optional[str],
    auto_post: bool,
    dest_dir: str,
    store_path: Path,
    dedup_path: Optional[Path] = None,
    dedup_ttl: int = 0,
) -> int:
    """Poll the HiveMQ queue: post each pending message and report its status.

    Drains the backlog, posts each message, and acks (via update_status) only the
    ones that reach a terminal state. Unacked messages (the --no-auto-post path,
    or a crash) are released back to the broker by close() and redelivered next
    poll, so they are effectively left pending until actually posted.

    Every received message is mirrored to its own JSON file in the local spool dir
    the instant it arrives (so nothing is lost if the broker then drops it). The file
    is deleted only on a successful post; a non-"posted" outcome or an error leaves it
    on disk (status recorded) to be re-attempted later with --retry.

    If dedup_path is set, an id we already posted within dedup_ttl seconds is
    dropped (ack as "posted") without re-posting; each successful post is recorded.
    """
    from core import dedup_store, local_store
    from hivemq_source import list_pending, update_status, close, HiveMQSourceError
    from imagekit_source import ImageKitSourceError
    from core.adb_pusher import PhonePushError
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

            if dedup_path is not None and dedup_store.seen(dedup_path, rec_id, ttl=dedup_ttl):
                _log(f"  SKIP {rec_id}: duplicate within {dedup_ttl}s window")
                _set_status(rec_id, "posted")  # ack-drop, no new status string
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
                    if dedup_path is not None:
                        dedup_store.record(dedup_path, rec_id, ttl=dedup_ttl)
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


def _watch_hivemq(
    *,
    serial: Optional[str],
    auto_post: bool,
    dest_dir: str,
    store_path: Path,
    dedup_path: Optional[Path] = None,
    dedup_ttl: int = 0,
) -> None:
    """Event-driven watch: react to each HiveMQ message the instant it arrives.

    Unlike the --once drain, this holds a persistent subscription (no poll
    interval) and dispatches each message to a handler that downloads, pushes, and
    (optionally) posts it. The handler's return value tells hivemq_source whether
    to publish a status + ack the message.

    As in the drain path, every message is mirrored to its own JSON file on receive;
    the file is deleted on a successful post and kept otherwise for --retry. With
    dedup_path set, an id posted within dedup_ttl seconds is dropped (ack "posted").
    """
    from core import dedup_store, local_store
    from hivemq_source import watch, HiveMQSourceError
    from imagekit_source import ImageKitSourceError
    from core.adb_pusher import PhonePushError, keep_awake
    from tiktok_poster import TikTokPostError

    def handler(rec: dict) -> Optional[str]:
        rec_id = rec.get("id")
        fields = rec.get("fields") or {}
        name = _name_for(rec_id, fields)
        _log(f"Message {rec_id}")
        if not fields.get("ImageURL"):
            _log(f"  SKIP {rec_id}: no ImageURL")
            return "failed"  # nothing to retry without a URL
        if dedup_path is not None and dedup_store.seen(dedup_path, rec_id, ttl=dedup_ttl):
            _log(f"  SKIP {rec_id}: duplicate within {dedup_ttl}s window")
            return "posted"  # ack-drop, no new status string
        # Always store on receive, before touching the phone, so a crash can't lose it.
        local_store.store(store_path, rec_id, fields)
        try:
            status = _post_record(fields, serial=serial, auto_post=auto_post, dest_dir=dest_dir)
            if not auto_post:
                _log(f"  pushed only (--no-auto-post); leaving {rec_id} pending")
                return None
            if status == "posted":
                local_store.remove(store_path, rec_id)  # success → delete the file
                if dedup_path is not None:
                    dedup_store.record(dedup_path, rec_id, ttl=dedup_ttl)
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
    keep_awake(serial)  # keep the phone from sleeping while we idle between messages
    try:
        watch(handler)
    except HiveMQSourceError as e:
        _log(f"Watch failed: {e}")
    _log("Stopped.")


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


def _retry_posts(
    *,
    serial: Optional[str],
    auto_post: bool,
    dest_dir: str,
    store_path: Path,
    dedup_path: Optional[Path] = None,
    dedup_ttl: int = 0,
) -> int:
    """Re-attempt every post still sitting in the local spool dir (one JSON per item).

    Talks only to local disk (no HiveMQ): re-downloads, re-pushes, and re-posts each
    surviving file. On "posted" the file is deleted; otherwise it stays (its status and
    attempts count are updated). Returns the number that succeeded.

    Retries are never deduped (they're known-failed items you asked to re-run), but a
    successful one is recorded so a later broker redelivery of the same id is dropped.
    """
    from core import dedup_store, local_store
    from imagekit_source import ImageKitSourceError
    from core.adb_pusher import PhonePushError
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
                if dedup_path is not None:
                    dedup_store.record(dedup_path, rec_id, ttl=dedup_ttl)
                succeeded += 1
            else:
                local_store.mark(store_path, rec_id, status)
                _log(f"  still {status}; kept in {store_path}")
        except (ImageKitSourceError, PhonePushError, TikTokPostError) as e:
            _log(f"  FAILED {name}: {e}")
            local_store.mark(store_path, rec_id, "failed", error=str(e))

    _log(f"Retry: {succeeded} posted, {len(local_store.items(store_path))} still stored.")
    return succeeded


def _clear_posts(*, store_path: Path, failed_only: bool) -> int:
    """Delete spooled posts WITHOUT re-attempting (talks only to local disk).

    By default removes every surviving file; with failed_only, only those marked
    "failed". Returns the number deleted.
    """
    from core import local_store

    statuses = {"failed"} if failed_only else None
    removed = local_store.clear(store_path, statuses)
    _log(f"Clear: removed {removed} {'failed ' if failed_only else ''}post(s) from {store_path}")
    return removed


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

    from core.imagekit_agent import catch_up_imagekit
    return catch_up_imagekit(folder=folder, state_path=state_path)


def _cli() -> int:
    from core.env_loader import load_env
    from core.logging_setup import setup_logging
    load_env()
    setup_logging("tiktok-agent")

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
        "--clear",
        action="store_true",
        help="Delete spooled posts WITHOUT re-attempting, then exit (no HiveMQ)",
    )
    parser.add_argument(
        "--failed-only",
        action="store_true",
        help="With --clear, only delete items marked 'failed' (keep needs_manual/wrong_account/etc.)",
    )
    parser.add_argument(
        "--store-dir",
        default=DEFAULT_STORE_DIR,
        help=f"Local spool dir holding one JSON per pending post (default: {DEFAULT_STORE_DIR})",
    )
    parser.add_argument(
        "--dedup-store",
        default=DEFAULT_DEDUP_PATH,
        help=f"JSON file remembering posted ids to drop duplicate messages (default: {DEFAULT_DEDUP_PATH}; hivemq source only)",
    )
    parser.add_argument(
        "--dedup-ttl",
        type=int,
        default=None,
        help="Dedup window in seconds; a repeated id within it is dropped (default: $DEDUP_TTL or 86400 = 1 day)",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Disable duplicate-message dropping (post every message even if its id repeats)",
    )
    args = parser.parse_args()

    from core import dedup_store

    state_path = Path(args.state)
    store_path = Path(args.store_dir)
    dedup_path = None if args.no_dedup else Path(args.dedup_store)
    dedup_ttl = args.dedup_ttl if args.dedup_ttl is not None else dedup_store.default_ttl()

    if args.catch_up:
        catch_up(source=args.source, folder=args.folder, state_path=state_path)
        return 0

    if args.clear:
        _clear_posts(store_path=store_path, failed_only=args.failed_only)
        return 0

    if args.retry:
        _retry_posts(
            serial=args.serial, auto_post=args.auto_post, dest_dir=args.dest, store_path=store_path,
            dedup_path=dedup_path, dedup_ttl=dedup_ttl,
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
        dedup_path=dedup_path,
        dedup_ttl=dedup_ttl,
    )

    if args.watch:
        if args.source == "hivemq":
            # MQTT is push: hold a persistent subscription and react instantly.
            # No --interval (that only applies to the imagekit folder poll).
            _watch_hivemq(
                serial=args.serial, auto_post=args.auto_post, dest_dir=args.dest, store_path=store_path,
                dedup_path=dedup_path, dedup_ttl=dedup_ttl,
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
