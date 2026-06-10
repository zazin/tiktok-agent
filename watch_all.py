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
