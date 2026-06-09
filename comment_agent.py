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
import sys
from pathlib import Path
from typing import Optional


DEFAULT_STORE_DIR = "queue_comments"

# Outcomes that resolve a message for good (its spool file is deleted): a successful
# comment, or one that can never be typed at all. needs_manual/failed are kept for retry.
_TERMINAL = ("commented", "skipped_non_ascii")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _resolve(store_path: Path, post_url: str, status: str, error: Optional[str] = None) -> None:
    """After acting on a stored comment: delete its file if terminal, else record outcome."""
    import local_store

    if status in _TERMINAL:
        local_store.remove(store_path, post_url)
    else:
        local_store.mark(store_path, post_url, status, error=error)
        _log(f"  kept {post_url} ({status}) in {store_path} for retry")


def process_once(*, serial: Optional[str], package: Optional[str], dry_run: bool, store_path: Path) -> int:
    """Drain the comment backlog once: comment on each message and report status.

    Unacked messages (a crash, or a status-publish failure) are released back to the
    broker by close() and redelivered next time. Every received message is mirrored to
    its own JSON file in the local spool dir on receive; the file is deleted once the
    comment lands (or can never be typed) and kept on needs_manual/failed for --retry.
    Returns the number processed.
    """
    import local_store
    from comment_source import list_pending, update_status, close, HiveMQSourceError
    from tiktok_commenter import comment_on_post, TikTokCommentError
    from adb_pusher import PhonePushError

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
            _log(f"Comment on {post_url}")
            # Always store on receive (except dry-run), before touching the phone.
            if not dry_run:
                local_store.store(store_path, post_url, {"post_url": post_url, "comment": comment})
            try:
                status = comment_on_post(
                    post_url, comment, serial=serial, package=package, dry_run=dry_run
                )
                _log(f"  tiktok: {status}")
                if not dry_run:
                    _resolve(store_path, post_url, status)
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


def _watch(*, serial: Optional[str], package: Optional[str], dry_run: bool, store_path: Path) -> None:
    """Event-driven watch: comment on each HiveMQ message the instant it arrives."""
    import local_store
    from comment_source import watch, HiveMQSourceError
    from tiktok_commenter import comment_on_post, TikTokCommentError
    from adb_pusher import PhonePushError

    def handler(rec: dict) -> Optional[str]:
        post_url = rec["post_url"]
        comment = rec["comment"]
        _log(f"Comment on {post_url}")
        # Always store on receive (except dry-run), before touching the phone.
        if not dry_run:
            local_store.store(store_path, post_url, {"post_url": post_url, "comment": comment})
        try:
            status = comment_on_post(
                post_url, comment, serial=serial, package=package, dry_run=dry_run
            )
            _log(f"  tiktok: {status}")
            # In dry-run, leave the message unacked (return None) so it isn't consumed.
            if dry_run:
                return None
            _resolve(store_path, post_url, status)
            return status
        except (TikTokCommentError, PhonePushError) as e:
            _log(f"  FAILED {post_url}: {e}")
            if dry_run:
                return None
            _resolve(store_path, post_url, "failed", error=str(e))
            return "failed"

    _log("Watching HiveMQ comment topic (event-driven, push; Ctrl-C to stop)...")
    try:
        watch(handler)
    except HiveMQSourceError as e:
        _log(f"Watch failed: {e}")
    _log("Stopped.")


def _retry_comments(*, serial: Optional[str], package: Optional[str], store_path: Path) -> int:
    """Re-attempt every comment still in the local spool dir (one JSON per item, no HiveMQ).

    On a terminal outcome (commented, or skipped_non_ascii) the file is deleted;
    needs_manual/failed keep the file (status and attempts updated). Returns the number
    that actually commented.
    """
    import local_store
    from tiktok_commenter import comment_on_post, TikTokCommentError
    from adb_pusher import PhonePushError

    entries = local_store.items(store_path)
    _log(f"Retry: {len(entries)} stored comment(s) in {store_path}")

    succeeded = 0
    for entry in entries:
        post_url = entry["key"]
        payload = entry.get("payload") or {}
        comment = payload.get("comment")
        if not post_url or not comment:
            _log(f"  SKIP {post_url}: missing post_url/comment in stored payload")
            continue
        local_store.store(store_path, post_url, payload)  # bump attempts for this retry
        _log(f"Retry {post_url} (attempt {int(entry.get('attempts', 0)) + 1})")
        try:
            status = comment_on_post(post_url, comment, serial=serial, package=package, dry_run=False)
            _log(f"  tiktok: {status}")
            _resolve(store_path, post_url, status)
            if status == "commented":
                succeeded += 1
        except (TikTokCommentError, PhonePushError) as e:
            _log(f"  FAILED {post_url}: {e}")
            _resolve(store_path, post_url, "failed", error=str(e))

    _log(f"Retry: {succeeded} commented, {len(local_store.items(store_path))} still stored.")
    return succeeded


def catch_up() -> int:
    """Drain the current comment backlog and mark each 'commented' without acting."""
    from comment_source import list_pending, update_status, close, HiveMQSourceError

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
    from env_loader import load_env
    load_env()

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
        "--store-dir",
        default=DEFAULT_STORE_DIR,
        help=f"Local spool dir holding one JSON per pending comment (default: {DEFAULT_STORE_DIR})",
    )
    parser.add_argument("--serial", default=None, help="Target device serial (if multiple phones connected)")
    parser.add_argument("--package", default=None, help="Override TikTok package name")
    parser.add_argument("--dry-run", action="store_true", help="Open posts and log the comment, but do NOT submit (messages left pending)")
    args = parser.parse_args()

    store_path = Path(args.store_dir)

    if args.catch_up:
        catch_up()
        return 0

    if args.retry:
        _retry_comments(serial=args.serial, package=args.package, store_path=store_path)
        return 0

    if args.watch:
        _watch(serial=args.serial, package=args.package, dry_run=args.dry_run, store_path=store_path)
        return 0

    process_once(serial=args.serial, package=args.package, dry_run=args.dry_run, store_path=store_path)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
