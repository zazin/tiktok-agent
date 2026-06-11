#!/usr/bin/env python3
"""
Watch ALL HiveMQ work topics from one process / one command.

The three consumers (`tiktok-agent` posts, `tiktok-commenter` comments,
`tiktok-comment-reader` reads) normally run as three separate `uv run` processes.
This launcher starts all three event-driven watchers together in ONE process —
each on its own thread with its own persistent MQTT session (own client-id) — so a
single `tiktok-watch-all` command drains every topic at once.

Concurrency is safe by construction: the device is still driven strictly
one-flow-at-a-time by the cross-process `core/device_lock.py` (an `fcntl.flock`
that contends across threads in one process exactly as it does across processes).
While one feature is driving the phone the others block and wait their turn.

Each watcher runs on a daemon thread; the paho network threads and per-queue worker
threads are daemons too, so Ctrl-C in the main thread tears the whole process down
cleanly. Run `--catch-up` once first (it drains every backlog WITHOUT acting), then
start the watch — otherwise the first connect would post/comment the whole backlog.

`--retry` is a one-shot (not a watcher): it re-attempts every locally-spooled post and
comment against local disk only (no HiveMQ), then exits — the same as running
`tiktok-agent --retry` and `tiktok-commenter --retry` back-to-back. The comment-reader
is stateless (no spool), so there is nothing to retry for reads.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from core.env_loader import load_env

import agent
import comment_agent
import comment_reader_agent


def _log(msg: str) -> None:
    print(f"[watch-all] {msg}", flush=True)


def _dedup_config(args) -> tuple[Path | None, Path | None, int]:
    """Resolve (posts_dedup_path, comments_dedup_path, ttl) from the CLI args.

    Paths are None when --no-dedup is set (dedup off). TTL falls back to $DEDUP_TTL
    or 86400 (1 day) when --dedup-ttl isn't given. Shared by the watchers and --retry.
    """
    from core import dedup_store

    ttl = args.dedup_ttl if args.dedup_ttl is not None else dedup_store.default_ttl()
    if args.no_dedup:
        return None, None, ttl
    return Path(args.posts_dedup_store), Path(args.comments_dedup_store), ttl


def _catch_up_all() -> None:
    """Drain every backlog (posts, comments, read-jobs) WITHOUT acting, one feature
    at a time. Run once before the first watch so it only acts on new work."""
    _log("Catch-up: draining all backlogs without acting...")
    _log("posts:")
    agent._catch_up_hivemq()
    _log("comments:")
    comment_agent.catch_up()
    _log("read-jobs:")
    comment_reader_agent.catch_up()
    _log("Catch-up done. Now run `tiktok-watch-all` to start watching.")


def _clear_all(args) -> None:
    """Delete every locally-spooled post and comment that still needs attention
    (failed/needs_manual/wrong_account/etc.), then exit. Talks only to local disk —
    nothing is re-attempted. With --failed-only, keep anything not marked "failed".
    Honors --no-posts/--no-comments. Reads are stateless (no spool)."""
    label = "failed" if args.failed_only else "spooled"
    _log(f"Clear: deleting {label} local work (no HiveMQ, no re-attempt)...")
    if not args.no_posts:
        _log("posts:")
        agent._clear_posts(store_path=Path(args.posts_store_dir), failed_only=args.failed_only)
    if not args.no_comments:
        _log("comments:")
        comment_agent._clear_comments(store_path=Path(args.comments_store_dir), failed_only=args.failed_only)
    _log("Clear done.")


def _retry_all(args) -> None:
    """Re-attempt every locally-spooled post and comment (talks only to local disk,
    no HiveMQ), one feature at a time, then exit. The comment-reader is stateless and
    has no spool, so there is nothing to retry for reads. Honors --no-posts/--no-comments.
    The device_lock serializes the two flows just like the watchers."""
    _log("Retry: re-attempting locally-spooled work without HiveMQ...")
    posts_dedup, comments_dedup, dedup_ttl = _dedup_config(args)
    if not args.no_posts:
        _log("posts:")
        agent._retry_posts(
            serial=args.serial,
            auto_post=args.auto_post,
            dest_dir=args.dest,
            store_path=Path(args.posts_store_dir),
            dedup_path=posts_dedup,
            dedup_ttl=dedup_ttl,
        )
    if not args.no_comments:
        _log("comments:")
        comment_agent._retry_comments(
            serial=args.serial,
            package=args.package,
            store_path=Path(args.comments_store_dir),
            dedup_path=comments_dedup,
            dedup_ttl=dedup_ttl,
        )
    _log("Retry done.")


def _cli() -> int:
    load_env()

    parser = argparse.ArgumentParser(
        description="Watch all HiveMQ topics (posts + comments + comment-reads) in one process.",
    )
    parser.add_argument(
        "--catch-up",
        action="store_true",
        help="Drain every backlog WITHOUT acting, then exit (run once before the first watch)",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Re-attempt locally-spooled posts + comments (no HiveMQ), then exit (reads have no spool)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete locally-spooled posts + comments WITHOUT re-attempting, then exit (no HiveMQ)",
    )
    parser.add_argument(
        "--failed-only",
        action="store_true",
        help="With --clear, only delete items marked 'failed' (keep needs_manual/wrong_account/etc.)",
    )
    # Shared device options
    parser.add_argument("--serial", default=None, help="Target device serial (if multiple phones connected)")
    parser.add_argument("--package", default=None, help="Override TikTok package name (comments + reads)")
    # Posts (tiktok-agent)
    parser.add_argument("--dest", default="/sdcard/Pictures", help="Remote dir on the phone for pushed images (default: /sdcard/Pictures)")
    parser.add_argument(
        "--auto-post",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-post each new image to TikTok (default: on; --no-auto-post only pushes to the phone)",
    )
    parser.add_argument("--posts-store-dir", default=agent.DEFAULT_STORE_DIR, help=f"Spool dir for pending posts (default: {agent.DEFAULT_STORE_DIR})")
    # Comments (tiktok-commenter)
    parser.add_argument("--comments-store-dir", default=comment_agent.DEFAULT_STORE_DIR, help=f"Spool dir for pending comments (default: {comment_agent.DEFAULT_STORE_DIR})")
    # Duplicate-message guard (posts + comments; default on, 1-day window)
    parser.add_argument("--posts-dedup-store", default=agent.DEFAULT_DEDUP_PATH, help=f"JSON file remembering posted ids (default: {agent.DEFAULT_DEDUP_PATH})")
    parser.add_argument("--comments-dedup-store", default=comment_agent.DEFAULT_DEDUP_PATH, help=f"JSON file remembering commented (post,comment) pairs (default: {comment_agent.DEFAULT_DEDUP_PATH})")
    parser.add_argument("--dedup-ttl", type=int, default=None, help="Dedup window in seconds; a repeat within it is dropped (default: $DEDUP_TTL or 86400 = 1 day)")
    parser.add_argument("--no-dedup", action="store_true", help="Disable duplicate dropping for posts + comments")
    # Reads (tiktok-comment-reader)
    parser.add_argument("--max", type=int, default=None, help=f"Default cap on comments scraped per post (read-job 'max' overrides; default {comment_reader_agent.DEFAULT_MAX_COMMENTS})")
    # Feature toggles (default: all on)
    parser.add_argument("--no-posts", action="store_true", help="Don't watch the post topic")
    parser.add_argument("--no-comments", action="store_true", help="Don't watch the comment topic")
    parser.add_argument("--no-reads", action="store_true", help="Don't watch the comment-read topic")
    args = parser.parse_args()

    if args.catch_up:
        _catch_up_all()
        return 0

    if args.clear:
        _clear_all(args)
        return 0

    if args.retry:
        _retry_all(args)
        return 0

    posts_dedup, comments_dedup, dedup_ttl = _dedup_config(args)

    # Each feature's blocking event-driven watch, wrapped so a thread can run it.
    watchers: list[tuple[str, callable]] = []
    if not args.no_posts:
        watchers.append((
            "posts",
            lambda: agent._watch_hivemq(
                serial=args.serial,
                auto_post=args.auto_post,
                dest_dir=args.dest,
                store_path=Path(args.posts_store_dir),
                dedup_path=posts_dedup,
                dedup_ttl=dedup_ttl,
            ),
        ))
    if not args.no_comments:
        watchers.append((
            "comments",
            lambda: comment_agent._watch(
                serial=args.serial,
                package=args.package,
                dry_run=False,
                store_path=Path(args.comments_store_dir),
                dedup_path=comments_dedup,
                dedup_ttl=dedup_ttl,
            ),
        ))
    if not args.no_reads:
        watchers.append((
            "reads",
            lambda: comment_reader_agent._watch(
                serial=args.serial,
                package=args.package,
                cli_max=args.max,
            ),
        ))

    if not watchers:
        _log("Nothing to watch (all features disabled). Exiting.")
        return 0

    _log(
        "Starting watchers: "
        + ", ".join(name for name, _ in watchers)
        + " — device access is serialized; Ctrl-C to stop all."
    )
    threads: list[threading.Thread] = []
    for name, run in watchers:
        t = threading.Thread(target=run, name=f"watch-{name}", daemon=True)
        t.start()
        threads.append(t)

    # Block the main thread so the daemon watchers keep running; Ctrl-C ends them all.
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    _log("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
