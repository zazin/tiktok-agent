#!/usr/bin/env python3
"""
Leave a comment on an existing TikTok post by driving the on-device UI over adb.

Given a post URL (e.g. https://www.tiktok.com/@user/video/1234567890) and a
comment string, this:

  1. opens the post in TikTok via a deep-link VIEW intent (reliable),
  2. PAUSES the video (a single tap on the video) — TikTok plays it on a loop,
     which keeps the UI perpetually non-idle so `uiautomator dump` fails with
     "could not get idle state" and returns a STALE tree; pausing lets the UI
     settle so dumps reflect the real screen,
  3. opens the comment sheet (taps the comment icon — its content-desc carries a
     live count, so we match on a SUBSTRING),
  4. focuses the comment input field (capturing its bounds first), types the
     comment, and submits it. Once the field is focused the blinking cursor keeps
     the UI non-idle (dump fails again), so the send button is tapped
     POSITIONALLY — at the right end of the input row — rather than by dumping.

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
from tiktok_profile import ensure_account, TikTokProfileError


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

# The submit control has no reliable label (its content-desc is an untranslated
# resource ref like "@2131888575") AND the focused input field keeps the UI
# non-idle so `uiautomator dump` fails — so it can't be found by dumping at all.
# It is instead tapped POSITIONALLY at the right end of the input row (see
# comment_on_post / SEND_BTN_X_FRAC below).

# Per-step pacing: how long to wait for a screen, and retries while it loads.
STEP_DELAY = 2.5
STEP_RETRIES = 6

# Geometry knobs (fractions of screen size) for the two taps that can't be
# resolved by `uiautomator dump` (a playing video / focused input keeps the UI
# non-idle, so dump fails):
#   - PAUSE_TAP_*: where to tap to pause the looping video. The video fills the
#     upper-middle; the comment controls are on the right rail, so a center tap
#     pauses without hitting them.
#   - SEND_BTN_X_FRAC: the comment send button sits at the right end of the input
#     row; tap this fraction of the width at the input field's vertical center.
PAUSE_TAP_X_FRAC = 0.5
PAUSE_TAP_Y_FRAC = 0.40
SEND_BTN_X_FRAC = 0.91

# After a successful comment, wait this long (so the submit lands) before
# force-stopping TikTok.
COMMENT_SUCCESS_KILL_DELAY = 8.0


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


def _bounds_of(bounds: str) -> Optional[tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return None
    return tuple(int(v) for v in m.groups())  # type: ignore[return-value]


def _tap(serial: Optional[str], x: int, y: int) -> None:
    run_adb(["shell", "input", "tap", str(x), str(y)], serial=serial)


def _force_stop(package: str, serial: Optional[str]) -> None:
    """Force-stop TikTok (best-effort; never fatal)."""
    try:
        run_adb(["shell", "am", "force-stop", package], serial=serial)
    except PhonePushError:
        pass


def _screen_size(serial: Optional[str]) -> tuple[int, int]:
    """Return the device (width, height) in pixels from `wm size`."""
    out = run_adb(["shell", "wm", "size"], serial=serial)
    m = re.search(r"(\d+)x(\d+)", out)
    if not m:
        raise TikTokCommentError(f"could not parse screen size from: {out!r}")
    return int(m.group(1)), int(m.group(2))


def _pause_video(serial: Optional[str]) -> bool:
    """
    Pause the looping video so the UI goes idle and is dumpable. True if confirmed.

    TikTok plays the post on a loop, which keeps the UI perpetually non-idle so
    `uiautomator dump` fails ("could not get idle state") and returns a stale
    tree. A tap on the video toggles play/pause; on a freshly-opened (playing)
    post this pauses it, letting the UI settle. The comment controls live on the
    right rail, so this center tap won't hit them.

    The post is still loading for the first moment after the deep link, so a single
    early tap is a no-op (the video then plays on). We therefore tap, then confirm
    a dump SUCCEEDS (output contains "dumped to" rather than "could not get idle
    state"); if not, we wait and tap again. We return as soon as a dump succeeds —
    tapping again after a successful pause would re-RESUME playback.
    """
    w, h = _screen_size(serial)
    x, y = int(w * PAUSE_TAP_X_FRAC), int(h * PAUSE_TAP_Y_FRAC)
    for _ in range(STEP_RETRIES):
        _tap(serial, x, y)
        time.sleep(1.0)
        out = run_adb(
            ["shell", "uiautomator", "dump", "/sdcard/window_dump.xml"], serial=serial
        )
        if "dumped to" in out.lower():
            return True
        time.sleep(1.0)
    return False


def _find_bounds(xml: str, labels: tuple[str, ...]) -> Optional[tuple[int, int, int, int]]:
    """Return the (x1,y1,x2,y2) bounds of the first node whose text/desc matches a label."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    wanted = {l.lower() for l in labels}
    for node in root.iter("node"):
        text = (node.get("text") or "").strip().lower()
        desc = (node.get("content-desc") or "").strip().lower()
        if text in wanted or desc in wanted:
            b = _bounds_of(node.get("bounds", ""))
            if b:
                return b
    return None


def _wait_for_bounds(
    labels: tuple[str, ...],
    serial: Optional[str],
    *,
    retries: int = STEP_RETRIES,
    delay: float = STEP_DELAY,
) -> Optional[tuple[int, int, int, int]]:
    """Poll the UI until a node matching `labels` appears; return its bounds (not a tap)."""
    for _ in range(retries):
        b = _find_bounds(_dump_ui(serial), labels)
        if b:
            return b
        time.sleep(delay)
    return None


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
    account: Optional[str] = None,
) -> str:
    """
    Open the post at `url` and submit `text` as a comment.

    If `account` is given, make sure that TikTok account is active first (switching
    via the in-app switcher); if it can't be confirmed, return "wrong_account"
    WITHOUT opening the post, so we never comment as the wrong account.

    Returns one of:
      - "commented"          — comment typed and submitted,
      - "dry_run"            — sheet + input reached; logged the comment, did NOT submit,
      - "skipped_non_ascii"  — nothing typeable after ASCII-stripping (not submitted),
      - "wrong_account"      — target account couldn't be confirmed active (not submitted),
      - "needs_manual"       — a screen wasn't recognized; left as-is for a human.

    Raises:
        TikTokCommentError: If opening the post itself fails (no TikTok).
    """
    if account:
        try:
            ensure_account(account, serial=serial, package=package)
        except TikTokProfileError as e:
            print(f"  wrong account: {e}", flush=True)
            return "wrong_account"

    pkg = open_post(url, serial=serial, package=package)

    # TikTok loops the video, keeping the UI non-idle so `uiautomator dump` fails
    # and returns a stale tree — pause it first so every dump below is real.
    if not _pause_video(serial):
        return "needs_manual"

    # Open the comment sheet (icon's content-desc embeds a count → substring match).
    if not _wait_and_tap_partial(COMMENT_OPEN_SUBSTRINGS, serial):
        return "needs_manual"

    # Locate the comment input field and capture its bounds BEFORE focusing it:
    # once focused, the blinking cursor keeps the UI non-idle (dump fails), so we
    # derive the send-button position from this geometry instead of re-dumping.
    input_bounds = _wait_for_bounds(COMMENT_INPUT_HINTS, serial)
    if not input_bounds:
        return "needs_manual"
    ix1, iy1, ix2, iy2 = input_bounds
    input_cx, input_cy = (ix1 + ix2) // 2, (iy1 + iy2) // 2
    _tap(serial, input_cx, input_cy)
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

    # Hide the keyboard (so the send button drops back to the input row), then tap
    # send positionally — it sits at the right end of that row. We can't dump here
    # (the focused field blocks it), so this tap is geometric, anchored to the
    # input field's vertical center.
    run_adb(["shell", "input", "keyevent", "4"], serial=serial)  # BACK → hide IME
    time.sleep(1.0)
    width, _ = _screen_size(serial)
    _tap(serial, int(width * SEND_BTN_X_FRAC), input_cy)
    time.sleep(STEP_DELAY)

    # Commented — wait for the submit to land, then close TikTok so it isn't left
    # running (mirrors the poster's post-success force-stop).
    time.sleep(COMMENT_SUCCESS_KILL_DELAY)
    _force_stop(pkg, serial)
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
    parser.add_argument("--account", default=None, help="Target TikTok @handle to switch to before commenting")
    parser.add_argument("--dry-run", action="store_true", help="Open + focus input and log, but do NOT submit")
    args = parser.parse_args()

    try:
        status = comment_on_post(
            args.url,
            args.comment,
            serial=args.serial,
            package=args.package,
            dry_run=args.dry_run,
            account=args.account,
        )
    except (TikTokCommentError, PhonePushError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Result: {status}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
