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

The low-level UI helpers (`dump_ui`, `find_tappable`, `input_line`, …) live in the
shared `tiktok_ui` module (used by the poster, commenter and profile flows). Only
the comment-specific screen labels and flow (e.g. `_pause_video`) live here.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from typing import Optional

from core.adb_pusher import run_adb, PhonePushError
from core.device_lock import device_lock
from tiktok_profile import ensure_account, TikTokProfileError
from core.tiktok_ui import (
    TIKTOK_PACKAGES,
    STEP_DELAY,
    STEP_RETRIES,
    installed_package as _installed_package,
    dump_ui as _dump_ui,
    tap as _tap,
    swipe as _swipe,
    force_stop as _force_stop,
    screen_size as _screen_size,
    bounds_of as _bounds_of,
    wait_and_tap_partial as _wait_and_tap_partial,
    wait_for_bounds as _wait_for_bounds,
    ascii_for_input as _ascii_for_input,
    input_line as _input_line,
)


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

# (Per-step pacing STEP_DELAY/STEP_RETRIES and the low-level UI primitives are
# shared via tiktok_ui.)

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

# ---- comment-ROW selectors (for reading comments / replying to a specific one) ---
# Calibrated on-device (com.ss.android.ugc.trill, ID locale). These resource-id leaf
# names are obfuscated/minified and WILL drift across TikTok versions — re-verify with
# a real `uiautomator dump` of an open comment sheet if rows stop parsing.
#   - ROW_AUTHOR_ID: the commenter's display name TEXT node (one per comment row)
#   - ROW_TEXT_ID:   the comment body TEXT node (ABSENT for image/sticker comments)
ROW_AUTHOR_ID = "title"
ROW_TEXT_ID = "enp"
# The per-row Reply control is matched by its LABEL (stable) rather than its id.
REPLY_BTN_LABELS = ("balas", "reply")
# Comment-sheet content is indented; nodes left of this x belong to the video behind
# the sheet (e.g. the post caption, also id/title) — ignored so they aren't mistaken
# for a commenter's name.
SHEET_CONTENT_MIN_X = 100
# Nested replies are indented further right than top-level comments (e.g. author x1
# ~174 for top-level vs ~241 for a reply on a 1080px device). A row whose author sits
# more than this many px right of the left-most (top-level) author on screen is a
# reply, not a top-level comment, and is excluded — we read/reply to top-level only.
REPLY_INDENT_TOLERANCE = 40

# Scrolling the comment sheet to read more (geometry + loop bounds), calibrated on-device.
SCROLL_X_FRAC = 0.5
SCROLL_FROM_Y_FRAC = 0.72
SCROLL_TO_Y_FRAC = 0.40
SCROLL_SETTLE = 1.5          # seconds to let the sheet settle after a swipe
SCROLL_STABLE_PASSES = 3     # consecutive zero-new-row passes that mark the end
SCROLL_MAX_PASSES = 40       # hard cap on swipes (very long threads)


class TikTokCommentError(Exception):
    """Raised when commenting on a post fails irrecoverably (e.g. no TikTok)."""


# ---- comment-row parsing / reading (shared by the reader + the reply flow) --------

def _norm_handle(s: str) -> str:
    """Normalize a @handle / display name for comparison (strip @, lowercase, trim)."""
    return s.strip().lstrip("@").strip().lower()


def parse_comment_rows(xml: str) -> list[dict]:
    """Parse an open comment sheet's UI dump into rows.

    Returns a list of ``{author, text, reply_xy}`` in top-to-bottom screen order:
      - ``author``   commenter display name (``ROW_AUTHOR_ID`` text node),
      - ``text``     comment body (``ROW_TEXT_ID``) or None for an image/sticker comment,
      - ``reply_xy`` (x, y) center of this row's Reply button (to tap to reply).

    Each row is anchored on its Reply button (label in ``REPLY_BTN_LABELS``); the author
    and text are the matching id nodes that fall BETWEEN the previous Reply button and
    this one (so a row's parts can't be stolen by an adjacent row). A row whose author
    can't be resolved (it scrolled partly off the top) is dropped — it is captured
    cleanly on an adjacent scroll position. **Nested replies** (indented right of the
    top-level author column by more than ``REPLY_INDENT_TOLERANCE``) are excluded — only
    top-level comments are returned.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []

    authors: list[tuple[int, int, str]] = []   # (y_top, x1, text)
    texts: list[tuple[int, str]] = []           # (y_top, text)
    replies: list[tuple[int, tuple[int, int]]] = []  # (y_top, center)
    for node in root.iter("node"):
        b = _bounds_of(node.get("bounds", ""))
        if not b:
            continue
        x1, y1, x2, y2 = b
        rid = (node.get("resource-id") or "").rsplit("/", 1)[-1]
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        label = (text or desc).lower()
        if label in REPLY_BTN_LABELS:
            replies.append((y1, ((x1 + x2) // 2, (y1 + y2) // 2)))
        elif rid == ROW_AUTHOR_ID and text and x1 >= SHEET_CONTENT_MIN_X:
            authors.append((y1, x1, text))
        elif rid == ROW_TEXT_ID and text and x1 >= SHEET_CONTENT_MIN_X:
            texts.append((y1, text))

    # The top-level author column is the left-most author indent on screen; rows whose
    # author sits further right are nested replies (skipped).
    base_x = min((x for (_, x, _) in authors), default=0)
    top_level_max_x = base_x + REPLY_INDENT_TOLERANCE

    replies.sort()
    rows: list[dict] = []
    prev_reply_y = -1
    for ry, rxy in replies:
        author = next(
            ((x, t) for (y, x, t) in reversed(authors) if prev_reply_y < y <= ry), None
        )
        prev_reply_y_for_next = ry
        if author is None:
            prev_reply_y = prev_reply_y_for_next
            continue  # partial row (author scrolled off) — captured on another pass
        author_x, author_text = author
        if author_x > top_level_max_x:
            prev_reply_y = prev_reply_y_for_next
            continue  # nested reply, not a top-level comment
        body = next((t for (y, t) in reversed(texts) if prev_reply_y < y <= ry), None)
        rows.append({"author": author_text, "text": body, "reply_xy": rxy})
        prev_reply_y = prev_reply_y_for_next
    return rows


def _swipe_sheet(serial: Optional[str]) -> None:
    """Scroll the open comment sheet up by ~one screen to load more comments."""
    w, h = _screen_size(serial)
    x = int(w * SCROLL_X_FRAC)
    _swipe(serial, x, int(h * SCROLL_FROM_Y_FRAC), x, int(h * SCROLL_TO_Y_FRAC))
    time.sleep(SCROLL_SETTLE)


def collect_comments(serial: Optional[str], *, max_comments: int) -> list[dict]:
    """Scroll the open comment sheet, scraping text comments until capped or exhausted.

    Returns up to ``max_comments`` ``{author, text}`` dicts (image/sticker comments,
    which have no text and can't be replied to, are skipped), deduped by (author, text)
    and kept in first-seen order — which, since TikTok defaults to a top/relevance sort,
    is roughly most-engaged first. Stops at the cap or after ``SCROLL_STABLE_PASSES``
    swipes with no new rows of any kind.
    """
    seen: set[tuple[str, Optional[str]]] = set()
    out: list[dict] = []
    stable = 0
    for _ in range(SCROLL_MAX_PASSES):
        new_any = 0
        for row in parse_comment_rows(_dump_ui(serial)):
            key = (_norm_handle(row["author"]), row["text"])
            if key in seen:
                continue
            seen.add(key)
            new_any += 1
            if row["text"]:
                out.append({"author": row["author"], "text": row["text"]})
                if len(out) >= max_comments:
                    return out[:max_comments]
        stable = stable + 1 if new_any == 0 else 0
        if stable >= SCROLL_STABLE_PASSES:
            break
        _swipe_sheet(serial)
    return out[:max_comments]


def _find_and_tap_reply(serial: Optional[str], author: str, text: Optional[str]) -> bool:
    """Scroll the open sheet to the comment matching author(+text) and tap its Reply. True if tapped.

    Matches a row whose author equals ``author`` (normalized) and, when ``text`` is
    given, whose body contains the ASCII-folded ``text`` (TikTok may truncate long
    comments, so a substring match is used). Returns False if not found within the
    scroll budget.
    """
    want_author = _norm_handle(author)
    want_text = _ascii_for_input(text or "").lower()
    stable = 0
    for _ in range(SCROLL_MAX_PASSES):
        rows = parse_comment_rows(_dump_ui(serial))
        for row in rows:
            if _norm_handle(row["author"]) != want_author:
                continue
            if want_text:
                body = _ascii_for_input(row["text"] or "").lower()
                if want_text not in body:
                    continue
            _tap(serial, *row["reply_xy"])
            time.sleep(STEP_DELAY)
            return True
        stable = stable + 1 if not rows else 0
        if stable >= SCROLL_STABLE_PASSES:
            break
        _swipe_sheet(serial)
    return False


# ---- comment-specific UI helpers (generic primitives live in tiktok_ui) ------

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


def read_comments(
    url: str,
    *,
    max_comments: int,
    serial: Optional[str] = None,
    package: Optional[str] = None,
) -> tuple[str, list[dict]]:
    """
    Open the post at `url` and scrape up to `max_comments` top-level text comments.

    Read-only (it never types or submits). Returns ``(status, comments)`` where status
    is "read" on success and "needs_manual" if a screen wasn't recognized; `comments`
    is a list of ``{author, text}`` (image/sticker comments, which can't be replied to,
    are skipped). TikTok is force-stopped on the way out either way.

    Raises:
        TikTokCommentError: If opening the post itself fails (no TikTok).
    """
    # Serialize the whole flow against the single device — only one consumer
    # process may drive the phone at a time (see core/device_lock.py).
    with device_lock(serial):
        pkg = open_post(url, serial=serial, package=package)

        # Pause the looping video so the sheet is dumpable (see _pause_video).
        if not _pause_video(serial):
            _force_stop(pkg, serial)
            return "needs_manual", []

        # Open the comment sheet (icon's content-desc embeds a count → substring match).
        if not _wait_and_tap_partial(COMMENT_OPEN_SUBSTRINGS, serial):
            _force_stop(pkg, serial)
            return "needs_manual", []

        comments = collect_comments(serial, max_comments=max_comments)
        _force_stop(pkg, serial)
        return "read", comments


def comment_on_post(
    url: str,
    text: str,
    *,
    serial: Optional[str] = None,
    package: Optional[str] = None,
    dry_run: bool = False,
    account: Optional[str] = None,
    reply_to: Optional[dict] = None,
) -> str:
    """
    Open the post at `url` and submit `text` as a comment.

    If `reply_to` (``{"author": "@handle", "text": "..."}``) is given, this submits
    `text` as a REPLY to that existing comment instead of a top-level comment: it finds
    the matching comment row in the sheet (scrolling as needed) and taps its Reply
    button before typing. If the target comment can't be found, returns
    "comment_not_found" WITHOUT submitting. When `reply_to` is None the behavior is
    unchanged — a top-level comment.

    If `account` is given, make sure that TikTok account is active first (switching
    via the in-app switcher); if it can't be confirmed, return "wrong_account"
    WITHOUT opening the post, so we never comment as the wrong account.

    Returns one of:
      - "commented"          — comment/reply typed and submitted,
      - "dry_run"            — sheet + input reached; logged the comment, did NOT submit,
      - "skipped_non_ascii"  — nothing typeable after ASCII-stripping (not submitted),
      - "wrong_account"      — target account couldn't be confirmed active (not submitted),
      - "comment_not_found"  — reply target comment wasn't found in the sheet (not submitted),
      - "needs_manual"       — a screen wasn't recognized.

    On every error outcome (wrong_account / needs_manual / skipped_non_ascii /
    comment_not_found) TikTok is force-stopped so it isn't left open; the message stays
    spooled for --retry. "dry_run" is the exception — it leaves the app open for inspection.

    Raises:
        TikTokCommentError: If opening the post itself fails (no TikTok).
    """
    # Serialize the whole flow against the single device — only one consumer
    # process may drive the phone at a time (see core/device_lock.py).
    with device_lock(serial):
        if account:
            try:
                ensure_account(account, serial=serial, package=package)
            except TikTokProfileError as e:
                print(f"  wrong account: {e}", flush=True)
                _force_stop(package, serial)  # don't leave TikTok open on an error
                return "wrong_account"

        pkg = open_post(url, serial=serial, package=package)

        # TikTok loops the video, keeping the UI non-idle so `uiautomator dump` fails
        # and returns a stale tree — pause it first so every dump below is real.
        if not _pause_video(serial):
            _force_stop(pkg, serial)
            return "needs_manual"

        # Open the comment sheet (icon's content-desc embeds a count → substring match).
        if not _wait_and_tap_partial(COMMENT_OPEN_SUBSTRINGS, serial):
            _force_stop(pkg, serial)
            return "needs_manual"

        # Locate the comment input field and capture its bounds BEFORE focusing it:
        # once focused, the blinking cursor keeps the UI non-idle (dump fails), so we
        # derive the send-button position from this geometry instead of re-dumping. The
        # field sits at the bottom of the sheet with the keyboard down; hiding the keyboard
        # later returns it here, so this captured Y is the send-button row in both modes.
        input_bounds = _wait_for_bounds(COMMENT_INPUT_HINTS, serial)
        if not input_bounds:
            _force_stop(pkg, serial)
            return "needs_manual"
        ix1, iy1, ix2, iy2 = input_bounds
        input_cx, input_cy = (ix1 + ix2) // 2, (iy1 + iy2) // 2

        if reply_to:
            # Reply mode: find the target comment row and tap its Reply button, which
            # auto-focuses the input ("Membalas <author>"); no separate input tap needed.
            if not _find_and_tap_reply(serial, reply_to.get("author", ""), reply_to.get("text")):
                print(f"  reply target not found: {reply_to}", flush=True)
                _force_stop(pkg, serial)
                return "comment_not_found"
        else:
            # Top-level comment: focus the bottom input field directly.
            _tap(serial, input_cx, input_cy)
            time.sleep(1.0)

        typeable = _ascii_for_input(text)
        if not typeable:
            # adb input can't type this (e.g. all emoji/non-Latin); don't submit blank.
            _force_stop(pkg, serial)
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
    from core.env_loader import load_env
    load_env()

    parser = argparse.ArgumentParser(
        description="Comment on a TikTok post by URL over adb (single shot, no MQTT)."
    )
    parser.add_argument("url", help="TikTok post URL, e.g. https://www.tiktok.com/@u/video/123")
    parser.add_argument("comment", help="The comment text to submit")
    parser.add_argument("--serial", default=None, help="Target device serial (if multiple phones)")
    parser.add_argument("--package", default=None, help="Override TikTok package name")
    parser.add_argument("--account", default=None, help="Target TikTok @handle to switch to before commenting")
    parser.add_argument("--reply-to-author", default=None, help="Reply to the comment by this @handle (instead of a top-level comment)")
    parser.add_argument("--reply-to-text", default=None, help="Disambiguate the target comment by a substring of its text")
    parser.add_argument("--dry-run", action="store_true", help="Open + focus input and log, but do NOT submit")
    args = parser.parse_args()

    reply_to = None
    if args.reply_to_author:
        reply_to = {"author": args.reply_to_author, "text": args.reply_to_text}

    try:
        status = comment_on_post(
            args.url,
            args.comment,
            serial=args.serial,
            package=args.package,
            dry_run=args.dry_run,
            account=args.account,
            reply_to=reply_to,
        )
    except (TikTokCommentError, PhonePushError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Result: {status}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
