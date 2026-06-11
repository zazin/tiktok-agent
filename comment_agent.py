#!/usr/bin/env python3
"""
tiktok-commenter — bridge between the HiveMQ comment queue and a connected phone.

Independent of the posting agent (agent.py): it drains a dedicated HiveMQ comment
topic (default "tiktok/comments"), and for each message — carrying a TikTok post
URL and a comment string — opens that post over adb and submits the comment, then
reports the outcome on the comment status topic and acks the message.

There is no AI and no image handling here: the exact comment text comes in the
message.

Run modes (mirror agent.py's hivemq path):
  - --watch     event-driven push: stay subscribed, comment on each message instantly
  - --once      drain the current backlog once and exit
  - --catch-up  drain the backlog and mark each done WITHOUT commenting (run once
                before the first --watch so it doesn't comment on the whole backlog)

Credentials (read from .env, auto-loaded):
  HIVEMQ_HOST, HIVEMQ_USERNAME, HIVEMQ_PASSWORD, HIVEMQ_COMMENT_TOPIC, ...

Usage (CLI):
    python comment_agent.py --once --dry-run     # log what it would comment, don't submit
    python comment_agent.py --once
    python comment_agent.py --watch
    python comment_agent.py --catch-up
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional


DEFAULT_STORE_DIR = "queue_comments"
DEFAULT_DEDUP_PATH = "dedup_comments.json"

# Outcomes that resolve a message for good (its spool file is deleted): a successful
# comment, or one that can never be typed at all. needs_manual/failed are kept for retry.
_TERMINAL = ("commented", "skipped_non_ascii")

logger = logging.getLogger(__name__)


def _log(msg: str) -> None:
    logger.info(msg)


def _resolve(store_path: Path, post_url: str, status: str, error: Optional[str] = None) -> None:
    """After acting on a stored comment: delete its file if terminal, else record outcome."""
    from core import local_store

    if status in _TERMINAL:
        local_store.remove(store_path, post_url)
    else:
        local_store.mark(store_path, post_url, status, error=error)
        _log(f"  kept {post_url} ({status}) in {store_path} for retry")


def process_once(
    *,
    serial: Optional[str],
    package: Optional[str],
    dry_run: bool,
    store_path: Path,
    dedup_path: Optional[Path] = None,
    dedup_ttl: int = 0,
) -> int:
    """Drain the comment backlog once: comment on each message and report status.

    Unacked messages (a crash, or a status-publish failure) are released back to the
    broker by close() and redelivered next time. Every received message is mirrored to
    its own JSON file in the local spool dir on receive; the file is deleted once the
    comment lands (or can never be typed) and kept on needs_manual/failed for --retry.
    Returns the number processed.

    If dedup_path is set, a (post_url, comment) we already commented within dedup_ttl
    seconds is dropped (ack "commented") without re-commenting; each success is recorded.
    """
    from core import dedup_store, local_store
    from core.comment_source import list_pending, update_status, close, HiveMQSourceError
    from tiktok_commenter import comment_on_post, TikTokCommentError
    from core.adb_pusher import PhonePushError

    try:
        try:
            records = list_pending()
        except HiveMQSourceError as e:
            _log(f"List failed: {e}")
            return 0

        _log(f"Poll: {len(records)} pending comment(s) in HiveMQ")

        def _set_status(post_url: str, status: str) -> None:
            try:
                update_status(post_url, status)
            except HiveMQSourceError as e:
                _log(f"  status update failed for {post_url} -> {status}: {e}")

        done = 0
        for rec in records:
            post_url = rec["post_url"]
            comment = rec["comment"]
            account = rec.get("account")
            reply_to = rec.get("reply_to")
            _log(f"{'Reply' if reply_to else 'Comment'} on {post_url}")
            dkey = dedup_store.content_key(post_url, comment)
            if not dry_run and dedup_path is not None and dedup_store.seen(dedup_path, dkey, ttl=dedup_ttl):
                _log(f"  SKIP {post_url}: duplicate comment within {dedup_ttl}s window")
                _set_status(post_url, "commented")  # ack-drop, no new status string
                done += 1
                continue
            # Always store on receive (except dry-run), before touching the phone.
            if not dry_run:
                local_store.store(
                    store_path,
                    post_url,
                    {"post_url": post_url, "comment": comment, "account": account, "reply_to": reply_to},
                )
            try:
                status = comment_on_post(
                    post_url, comment, serial=serial, package=package, dry_run=dry_run,
                    account=account, reply_to=reply_to,
                )
                _log(f"  tiktok: {status}")
                if not dry_run:
                    _resolve(store_path, post_url, status)
                    if status == "commented" and dedup_path is not None:
                        dedup_store.record(dedup_path, dkey, ttl=dedup_ttl)
                    _set_status(post_url, status)
            except (TikTokCommentError, PhonePushError) as e:
                _log(f"  FAILED {post_url}: {e}")
                if not dry_run:
                    _resolve(store_path, post_url, "failed", error=str(e))
                    _set_status(post_url, "failed")
            done += 1

        return done
    finally:
        close()


def _watch(
    *,
    serial: Optional[str],
    package: Optional[str],
    dry_run: bool,
    store_path: Path,
    dedup_path: Optional[Path] = None,
    dedup_ttl: int = 0,
) -> None:
    """Event-driven watch: comment on each HiveMQ message the instant it arrives."""
    from core import dedup_store, local_store
    from core.comment_source import watch, HiveMQSourceError
    from tiktok_commenter import comment_on_post, TikTokCommentError
    from core.adb_pusher import PhonePushError, keep_awake

    def handler(rec: dict) -> Optional[str]:
        post_url = rec["post_url"]
        comment = rec["comment"]
        account = rec.get("account")
        reply_to = rec.get("reply_to")
        _log(f"{'Reply' if reply_to else 'Comment'} on {post_url}")
        dkey = dedup_store.content_key(post_url, comment)
        if not dry_run and dedup_path is not None and dedup_store.seen(dedup_path, dkey, ttl=dedup_ttl):
            _log(f"  SKIP {post_url}: duplicate comment within {dedup_ttl}s window")
            return "commented"  # ack-drop, no new status string
        # Always store on receive (except dry-run), before touching the phone.
        if not dry_run:
            local_store.store(
                store_path,
                post_url,
                {"post_url": post_url, "comment": comment, "account": account, "reply_to": reply_to},
            )
        try:
            status = comment_on_post(
                post_url, comment, serial=serial, package=package, dry_run=dry_run,
                account=account, reply_to=reply_to,
            )
            _log(f"  tiktok: {status}")
            # In dry-run, leave the message unacked (return None) so it isn't consumed.
            if dry_run:
                return None
            _resolve(store_path, post_url, status)
            if status == "commented" and dedup_path is not None:
                dedup_store.record(dedup_path, dkey, ttl=dedup_ttl)
            return status
        except (TikTokCommentError, PhonePushError) as e:
            _log(f"  FAILED {post_url}: {e}")
            if dry_run:
                return None
            _resolve(store_path, post_url, "failed", error=str(e))
            return "failed"

    _log("Watching HiveMQ comment topic (event-driven, push; Ctrl-C to stop)...")
    keep_awake(serial)  # keep the phone from sleeping while we idle between messages
    try:
        watch(handler)
    except HiveMQSourceError as e:
        _log(f"Watch failed: {e}")
    _log("Stopped.")


def _retry_comments(
    *,
    serial: Optional[str],
    package: Optional[str],
    store_path: Path,
    dedup_path: Optional[Path] = None,
    dedup_ttl: int = 0,
) -> int:
    """Re-attempt every comment still in the local spool dir (one JSON per item, no HiveMQ).

    On a terminal outcome (commented, or skipped_non_ascii) the file is deleted;
    needs_manual/failed keep the file (status and attempts updated). Returns the number
    that actually commented. A successful one is recorded for dedup (never checked here —
    retries are known-pending items you asked to re-run).
    """
    from core import dedup_store, local_store
    from tiktok_commenter import comment_on_post, TikTokCommentError
    from core.adb_pusher import PhonePushError

    entries = local_store.items(store_path)
    _log(f"Retry: {len(entries)} stored comment(s) in {store_path}")

    succeeded = 0
    for entry in entries:
        post_url = entry["key"]
        payload = entry.get("payload") or {}
        comment = payload.get("comment")
        account = payload.get("account")
        reply_to = payload.get("reply_to")
        if not post_url or not comment:
            _log(f"  SKIP {post_url}: missing post_url/comment in stored payload")
            continue
        local_store.store(store_path, post_url, payload)  # bump attempts for this retry
        attempt = int(entry.get("attempts", 0)) + 1
        # Retry skips HiveMQ, so mqtt_queue never logs the payload — attach it here
        # (verbatim, like _log_received) so retried items stay queryable in ES.
        logger.info(
            "Retry %s (attempt %s)",
            post_url,
            attempt,
            extra={
                "es_labels": {
                    "PostURL": post_url,
                    "mqtt.direction": "retry",
                    "mqtt.payload": json.dumps(payload, default=str),
                    "retry.attempt": attempt,
                }
            },
        )
        try:
            status = comment_on_post(
                post_url, comment, serial=serial, package=package, dry_run=False,
                account=account, reply_to=reply_to,
            )
            _log(f"  tiktok: {status}")
            _resolve(store_path, post_url, status)
            if status == "commented":
                if dedup_path is not None:
                    dedup_store.record(dedup_path, dedup_store.content_key(post_url, comment), ttl=dedup_ttl)
                succeeded += 1
        except (TikTokCommentError, PhonePushError) as e:
            _log(f"  FAILED {post_url}: {e}")
            _resolve(store_path, post_url, "failed", error=str(e))

    _log(f"Retry: {succeeded} commented, {len(local_store.items(store_path))} still stored.")
    return succeeded


def _clear_comments(*, store_path: Path, failed_only: bool) -> int:
    """Delete spooled comments WITHOUT re-attempting (talks only to local disk).

    By default removes every surviving file; with failed_only, only those marked
    "failed". Returns the number deleted.
    """
    from core import local_store

    statuses = {"failed"} if failed_only else None
    removed = local_store.clear(store_path, statuses)
    _log(f"Clear: removed {removed} {'failed ' if failed_only else ''}comment(s) from {store_path}")
    return removed


def catch_up() -> int:
    """Drain the current comment backlog and mark each 'commented' without acting."""
    from core.comment_source import list_pending, update_status, close, HiveMQSourceError

    try:
        try:
            records = list_pending()
        except HiveMQSourceError as e:
            _log(f"Catch-up list failed: {e}")
            return 0

        n = 0
        for rec in records:
            post_url = rec["post_url"]
            try:
                update_status(post_url, "commented")
                n += 1
            except HiveMQSourceError as e:
                _log(f"  catch-up failed for {post_url}: {e}")
        _log(f"Catch-up: marked {n} pending comment(s) done (skipped without commenting).")
        return n
    finally:
        close()


def _cli() -> int:
    from core.env_loader import load_env
    from core.logging_setup import setup_logging
    load_env()
    setup_logging("tiktok-commenter")

    parser = argparse.ArgumentParser(
        description="Drain the HiveMQ comment queue and comment on each TikTok post over adb."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-o", "--once", action="store_true", help="Drain the current backlog once and exit (default)")
    mode.add_argument("-w", "--watch", action="store_true", help="Event-driven: stay subscribed and comment on each message instantly")
    parser.add_argument(
        "--catch-up",
        action="store_true",
        help="Drain the current backlog WITHOUT commenting, then exit (run before a first --watch)",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Re-attempt every comment still in the local spool dir, then exit (no HiveMQ)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete spooled comments WITHOUT re-attempting, then exit (no HiveMQ)",
    )
    parser.add_argument(
        "--failed-only",
        action="store_true",
        help="With --clear, only delete items marked 'failed' (keep needs_manual/wrong_account/etc.)",
    )
    parser.add_argument(
        "--store-dir",
        default=DEFAULT_STORE_DIR,
        help=f"Local spool dir holding one JSON per pending comment (default: {DEFAULT_STORE_DIR})",
    )
    parser.add_argument(
        "--dedup-store",
        default=DEFAULT_DEDUP_PATH,
        help=f"JSON file remembering commented (post,comment) pairs to drop duplicates (default: {DEFAULT_DEDUP_PATH})",
    )
    parser.add_argument(
        "--dedup-ttl",
        type=int,
        default=None,
        help="Dedup window in seconds; a repeated (post,comment) within it is dropped (default: $DEDUP_TTL or 86400 = 1 day)",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Disable duplicate-comment dropping (submit every message even if it repeats)",
    )
    parser.add_argument("--serial", default=None, help="Target device serial (if multiple phones connected)")
    parser.add_argument("--package", default=None, help="Override TikTok package name")
    parser.add_argument("--dry-run", action="store_true", help="Open posts and log the comment, but do NOT submit (messages left pending)")
    args = parser.parse_args()

    from core import dedup_store

    store_path = Path(args.store_dir)
    dedup_path = None if args.no_dedup else Path(args.dedup_store)
    dedup_ttl = args.dedup_ttl if args.dedup_ttl is not None else dedup_store.default_ttl()

    if args.catch_up:
        catch_up()
        return 0

    if args.clear:
        _clear_comments(store_path=store_path, failed_only=args.failed_only)
        return 0

    if args.retry:
        _retry_comments(
            serial=args.serial, package=args.package, store_path=store_path,
            dedup_path=dedup_path, dedup_ttl=dedup_ttl,
        )
        return 0

    if args.watch:
        _watch(
            serial=args.serial, package=args.package, dry_run=args.dry_run, store_path=store_path,
            dedup_path=dedup_path, dedup_ttl=dedup_ttl,
        )
        return 0

    process_once(
        serial=args.serial, package=args.package, dry_run=args.dry_run, store_path=store_path,
        dedup_path=dedup_path, dedup_ttl=dedup_ttl,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
