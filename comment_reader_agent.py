#!/usr/bin/env python3
"""
tiktok-comment-reader — the read half of the comment-reply pipeline.

A backend that wants to reply to a post's comments can't read them itself (there is
no TikTok API); only the device can, via the on-screen UI. This consumer drains a
dedicated HiveMQ read-job topic (default "tiktok/comments-read"), and for each
message — carrying a TikTok post URL — opens that post over adb, scrapes up to `max`
top-level comments, and publishes the list back on the comment-list topic (default
"tiktok/comments-list"). The backend subscribes to that, generates a positive reply
per comment, and sends each to the commenter (tiktok-commenter) as a reply-job.

Read-only: it never types or submits anything. It is idempotent (re-reading a post
just re-publishes its current comments), so there is no local spool / --retry — a
failed read is simply left unacked and redelivered by the broker.

Run modes (mirror comment_agent.py):
  - --watch     event-driven push: read each post the instant a read-job arrives
  - --once      drain the current backlog once and exit
  - --catch-up  drain the backlog and ack each WITHOUT reading (run once before a
                first --watch so it doesn't read the whole backlog)

The per-post comment cap is, in precedence order: the read-job's ``max`` field, then
--max, then $COMMENT_READ_MAX, then DEFAULT_MAX_COMMENTS.

Credentials (read from .env, auto-loaded):
  HIVEMQ_HOST, HIVEMQ_USERNAME, HIVEMQ_PASSWORD, HIVEMQ_COMMENT_READ_TOPIC, ...

Usage (CLI):
    python comment_reader_agent.py --once
    python comment_reader_agent.py --watch
    python comment_reader_agent.py --catch-up
    python comment_reader_agent.py --once --max 20
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional


DEFAULT_MAX_COMMENTS = 10


def _log(msg: str) -> None:
    print(msg, flush=True)


def _resolve_max(rec: dict, cli_max: Optional[int]) -> int:
    """Pick the comment cap: read-job 'max' → --max → $COMMENT_READ_MAX → default."""
    for candidate in (rec.get("max"), cli_max, os.getenv("COMMENT_READ_MAX")):
        if candidate is None:
            continue
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return DEFAULT_MAX_COMMENTS


def _result_body(post_url: str, comments: list[dict], *, error: Optional[str] = None) -> dict:
    body = {"PostURL": post_url, "comments": comments, "count": len(comments), "ts": int(time.time())}
    if error:
        body["error"] = error
    return body


def _read_and_build(rec: dict, *, serial, package, cli_max) -> dict:
    """Read one post's comments and return the result body to publish (never raises)."""
    from tiktok_commenter import read_comments, TikTokCommentError
    from core.adb_pusher import PhonePushError

    post_url = rec["post_url"]
    cap = _resolve_max(rec, cli_max)
    _log(f"Read {post_url} (max {cap})")
    try:
        status, comments = read_comments(post_url, max_comments=cap, serial=serial, package=package)
        _log(f"  tiktok: {status} — {len(comments)} comment(s)")
        if status == "read":
            return _result_body(post_url, comments)
        return _result_body(post_url, [], error=status)
    except (TikTokCommentError, PhonePushError) as e:
        _log(f"  FAILED {post_url}: {e}")
        return _result_body(post_url, [], error=f"failed: {e}")


def process_once(*, serial: Optional[str], package: Optional[str], cli_max: Optional[int]) -> int:
    """Drain the read-job backlog once: read each post and publish its comment list.

    Unacked jobs (a crash, or a publish failure) are released back to the broker by
    close() and redelivered next time. Returns the number processed.
    """
    from core.comment_read_source import list_pending, publish_comments, close, HiveMQSourceError

    try:
        try:
            records = list_pending()
        except HiveMQSourceError as e:
            _log(f"List failed: {e}")
            return 0

        _log(f"Poll: {len(records)} pending read-job(s) in HiveMQ")
        done = 0
        for rec in records:
            body = _read_and_build(rec, serial=serial, package=package, cli_max=cli_max)
            try:
                publish_comments(rec["post_url"], body)
            except HiveMQSourceError as e:
                _log(f"  publish failed for {rec['post_url']}: {e}")
            done += 1
        return done
    finally:
        close()


def _watch(*, serial: Optional[str], package: Optional[str], cli_max: Optional[int]) -> None:
    """Event-driven watch: read each post the instant a read-job arrives."""
    from core.comment_read_source import watch, publish_comments, HiveMQSourceError
    from core.adb_pusher import keep_awake

    def handler(rec: dict) -> Optional[str]:
        body = _read_and_build(rec, serial=serial, package=package, cli_max=cli_max)
        # Publish the list ourselves (richer than a status string) and ack via that;
        # return None so the shared watch loop doesn't also publish a status.
        try:
            publish_comments(rec["post_url"], body)
        except HiveMQSourceError as e:
            _log(f"  publish failed for {rec['post_url']}: {e}")
            return None  # leave unacked → redelivered
        return None

    _log("Watching HiveMQ comment-read topic (event-driven, push; Ctrl-C to stop)...")
    keep_awake(serial)  # keep the phone from sleeping while we idle between read-jobs
    try:
        watch(handler)
    except HiveMQSourceError as e:
        _log(f"Watch failed: {e}")
    _log("Stopped.")


def catch_up() -> int:
    """Drain the current read-job backlog and ack each WITHOUT reading."""
    from core.comment_read_source import list_pending, update_status, close, HiveMQSourceError

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
                update_status(post_url, "skipped")
                n += 1
            except HiveMQSourceError as e:
                _log(f"  catch-up failed for {post_url}: {e}")
        _log(f"Catch-up: marked {n} pending read-job(s) done (skipped without reading).")
        return n
    finally:
        close()


def _cli() -> int:
    from core.env_loader import load_env
    load_env()

    parser = argparse.ArgumentParser(
        description="Drain the HiveMQ comment-read queue, scrape each post's comments, and publish the list."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-o", "--once", action="store_true", help="Drain the current backlog once and exit (default)")
    mode.add_argument("-w", "--watch", action="store_true", help="Event-driven: stay subscribed and read each post instantly")
    parser.add_argument(
        "--catch-up",
        action="store_true",
        help="Drain the current backlog WITHOUT reading, then exit (run before a first --watch)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help=f"Default cap on comments scraped per post (read-job 'max' overrides; default {DEFAULT_MAX_COMMENTS})",
    )
    parser.add_argument("--serial", default=None, help="Target device serial (if multiple phones connected)")
    parser.add_argument("--package", default=None, help="Override TikTok package name")
    args = parser.parse_args()

    if args.catch_up:
        catch_up()
        return 0

    if args.watch:
        _watch(serial=args.serial, package=args.package, cli_max=args.max)
        return 0

    process_once(serial=args.serial, package=args.package, cli_max=args.max)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
