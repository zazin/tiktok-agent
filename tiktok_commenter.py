#!/usr/bin/env python3
"""
Leave a comment on an existing TikTok post by driving the on-device UI over adb.

Given a post URL (e.g. https://www.tiktok.com/@user/video/1234567890) and a
comment string, this:

  1. opens the post in TikTok via a deep-link VIEW intent (reliable),
  2. opens the comment sheet (taps the comment icon — its content-desc carries a
     live count, so we match on a SUBSTRING),
  3. focuses the comment input field, types the comment, and submits it.

Like tiktok_poster.py this is inherently brittle (it depends on TikTok's current
UI, locale and A/B variant) and may be against TikTok's Terms of Service. It is
defensive: on any screen it doesn't recognize it STOPS and returns
"needs_manual" rather than tapping blindly.

The phone must be unlocked and TikTok installed + logged in.

The low-level UI helpers (`_dump_ui`, `_find_tappable`, `_input_line`, …) are
intentionally COPIED from tiktok_poster.py — the project has no shared adb-UI util
module, so each feature carries its own primitives.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Optional

from adb_pusher import run_adb, PhonePushError


# TikTok package names: global app, then the older/alt package as fallback.
TIKTOK_PACKAGES = ("com.zhiliaoapp.musically", "com.ss.android.ugc.trill")

# ---- UI selectors — GUESSES that must be calibrated on-device ----------------
# Open these screens with a real `uiautomator dump` and fill in the actual
# text / content-desc values. English + Indonesian variants included.

# The comment icon on the video screen. Its content-desc usually embeds a live
# count ("Read or add comments. 12 comments"), so this is matched as a SUBSTRING.
COMMENT_OPEN_SUBSTRINGS = (
    "tambahkan komentar",
    "add comments",
    "read or add comment",
    "lihat komentar",
)

# The comment input field inside the sheet (tapped to focus before typing).
COMMENT_INPUT_HINTS = (
    "Add comment",
    "Add comment...",
    "Add a comment",
    "Tambahkan komentar",
    "Tambahkan komentar...",
    "Say something nice",
)

# The submit control (often an icon → matched on text AND content-desc).
COMMENT_SEND_LABELS = ("Post", "Send", "Kirim", "Posting")

# Per-step pacing: how long to wait for a screen, and retries while it loads.
STEP_DELAY = 2.5
STEP_RETRIES = 6


class TikTokCommentError(Exception):
    """Raised when commenting on a post fails irrecoverably (e.g. no TikTok)."""


# ---- low-level primitives (copied from tiktok_poster.py) ---------------------

def _installed_package(serial: Optional[str]) -> Optional[str]:
    """Return the first TikTok package actually installed on the device."""
    try:
        out = run_adb(["shell", "pm", "list", "packages"], serial=serial)
    except PhonePushError:
        return None
    installed = {line.replace("package:", "").strip() for line in out.splitlines()}
    for pkg in TIKTOK_PACKAGES:
        if pkg in installed:
            return pkg
    return None


def _dump_ui(serial: Optional[str]) -> str:
    """Dump the current UI hierarchy XML and return it as text."""
    run_adb(["shell", "uiautomator", "dump", "/sdcard/window_dump.xml"], serial=serial)
    return run_adb(["shell", "cat", "/sdcard/window_dump.xml"], serial=serial)


def _center_of_bounds(bounds: str) -> Optional[tuple[int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return None
    x1, y1, x2, y2 = (int(v) for v in m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _find_tappable(xml: str, labels: tuple[str, ...]) -> Optional[tuple[int, int]]:
    """Find the center of the first node whose text/content-desc EXACTLY matches a label."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    wanted = {l.lower() for l in labels}
    for node in root.iter("node"):
        text = (node.get("text") or "").strip().lower()
        desc = (node.get("content-desc") or "").strip().lower()
        if text in wanted or desc in wanted:
            center = _center_of_bounds(node.get("bounds", ""))
            if center:
                return center
    return None


def _find_partial(xml: str, substrings: tuple[str, ...]) -> Optional[tuple[int, int]]:
    """Find the center of the first node whose text/content-desc CONTAINS a substring.

    Used for controls whose label embeds dynamic content (e.g. the comment icon's
    content-desc carries the comment count), where an exact match won't hit.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    wanted = [s.lower() for s in substrings]
    for node in root.iter("node"):
        text = (node.get("text") or "").strip().lower()
        desc = (node.get("content-desc") or "").strip().lower()
        haystack = f"{text}\x00{desc}"
        if any(s in haystack for s in wanted):
            center = _center_of_bounds(node.get("bounds", ""))
            if center:
                return center
    return None


def _tap(serial: Optional[str], x: int, y: int) -> None:
    run_adb(["shell", "input", "tap", str(x), str(y)], serial=serial)


def _wait_and_tap(
    labels: tuple[str, ...],
    serial: Optional[str],
    *,
    retries: int = STEP_RETRIES,
    delay: float = STEP_DELAY,
) -> bool:
    """Poll the UI until a node EXACTLY matching `labels` appears, then tap it."""
    for _ in range(retries):
        target = _find_tappable(_dump_ui(serial), labels)
        if target:
            _tap(serial, *target)
            time.sleep(delay)
            return True
        time.sleep(delay)
    return False


def _wait_and_tap_partial(
    substrings: tuple[str, ...],
    serial: Optional[str],
    *,
    retries: int = STEP_RETRIES,
    delay: float = STEP_DELAY,
) -> bool:
    """Poll the UI until a node whose label CONTAINS a substring appears, then tap it."""
    for _ in range(retries):
        target = _find_partial(_dump_ui(serial), substrings)
        if target:
            _tap(serial, *target)
            time.sleep(delay)
            return True
        time.sleep(delay)
    return False


def _ascii_for_input(line: str) -> str:
    """ASCII-fold a line the way `adb input text` requires (preview helper)."""
    ascii_only = line.encode("ascii", "ignore").decode()
    ascii_only = re.sub(r"[\"'`]", "", ascii_only)
    return re.sub(r"[ \t]+", " ", ascii_only).strip()


def _input_line(line: str, serial: Optional[str]) -> bool:
    """Type one line into the focused field via `adb input text`. True if anything typed.

    `adb input text` can't enter emoji/non-ASCII — those are stripped. Quotes
    confuse the shell; spaces are encoded as %s. Returns False if nothing typeable
    remains after stripping (so the caller can avoid submitting a blank comment).
    """
    safe = _ascii_for_input(line).replace(" ", "%s")
    if not safe:
        return False
    run_adb(["shell", "input", "text", safe], serial=serial)
    return True


# ---- comment flow ------------------------------------------------------------

def open_post(
    url: str,
    *,
    serial: Optional[str] = None,
    package: Optional[str] = None,
) -> str:
    """
    Open a TikTok post by URL via a deep-link VIEW intent (the reliable phase).

    Returns the package used. Raises TikTokCommentError if TikTok isn't installed.
    """
    pkg = package or _installed_package(serial)
    if not pkg:
        raise TikTokCommentError(
            f"TikTok not found on device. Looked for: {', '.join(TIKTOK_PACKAGES)}"
        )

    run_adb(
        [
            "shell", "am", "start",
            "-a", "android.intent.action.VIEW",
            "-d", url,
            "-p", pkg,
        ],
        serial=serial,
    )
    time.sleep(STEP_DELAY)
    return pkg


def comment_on_post(
    url: str,
    text: str,
    *,
    serial: Optional[str] = None,
    package: Optional[str] = None,
    dry_run: bool = False,
) -> str:
    """
    Open the post at `url` and submit `text` as a comment.

    Returns one of:
      - "commented"          — comment typed and submitted,
      - "dry_run"            — sheet + input reached; logged the comment, did NOT submit,
      - "skipped_non_ascii"  — nothing typeable after ASCII-stripping (not submitted),
      - "needs_manual"       — a screen wasn't recognized; left as-is for a human.

    Raises:
        TikTokCommentError: If opening the post itself fails (no TikTok).
    """
    open_post(url, serial=serial, package=package)

    # Open the comment sheet (icon's content-desc embeds a count → substring match).
    if not _wait_and_tap_partial(COMMENT_OPEN_SUBSTRINGS, serial):
        return "needs_manual"

    # Focus the comment input field.
    if not _wait_and_tap(COMMENT_INPUT_HINTS, serial):
        return "needs_manual"
    time.sleep(1.0)

    typeable = _ascii_for_input(text)
    if not typeable:
        # adb input can't type this (e.g. all emoji/non-Latin); don't submit blank.
        return "skipped_non_ascii"

    if dry_run:
        print(f"  [dry-run] would comment {typeable!r} on {url}", flush=True)
        return "dry_run"

    _input_line(text, serial)
    time.sleep(0.5)

    if not _wait_and_tap(COMMENT_SEND_LABELS, serial):
        return "needs_manual"

    # Dismiss the keyboard so the next run starts from a clean screen.
    run_adb(["shell", "input", "keyevent", "111"], serial=serial)  # KEYCODE_ESCAPE
    time.sleep(0.5)
    return "commented"


def _cli() -> int:
    import argparse
    import sys
    from env_loader import load_env
    load_env()

    parser = argparse.ArgumentParser(
        description="Comment on a TikTok post by URL over adb (single shot, no MQTT)."
    )
    parser.add_argument("url", help="TikTok post URL, e.g. https://www.tiktok.com/@u/video/123")
    parser.add_argument("comment", help="The comment text to submit")
    parser.add_argument("--serial", default=None, help="Target device serial (if multiple phones)")
    parser.add_argument("--package", default=None, help="Override TikTok package name")
    parser.add_argument("--dry-run", action="store_true", help="Open + focus input and log, but do NOT submit")
    args = parser.parse_args()

    try:
        status = comment_on_post(
            args.url,
            args.comment,
            serial=args.serial,
            package=args.package,
            dry_run=args.dry_run,
        )
    except (TikTokCommentError, PhonePushError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Result: {status}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
