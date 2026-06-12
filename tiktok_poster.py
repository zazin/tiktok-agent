#!/usr/bin/env python3
"""
Auto-post an image to TikTok by driving the on-device UI over adb.

This is inherently brittle (it depends on TikTok's current UI) and may be against
TikTok's Terms of Service. It is therefore split into two phases:

  Phase 1 (reliable, always run): fire an ACTION_SEND intent so TikTok opens its
    upload/caption composer with the image attached. The human can finish posting.

  Phase 2 (best-effort, opt-in via auto_post=True): repeatedly dump the UI tree
    (`uiautomator dump`), look for Next/Post controls by text, and tap them. On
    any screen it doesn't recognize, it STOPS and leaves the composer open
    (status "needs_manual") rather than tapping blindly.

The phone must be unlocked and TikTok installed + logged in.
"""

from __future__ import annotations

import logging
import re
import time
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
    find_tappable as _find_tappable,
    tap as _tap,
    force_stop as _force_stop,
    wait_and_tap as _wait_and_tap,
    input_line as _input_line,
)


# The post flow is an ORDERED sequence of screen-advances. Each entry lists the
# equivalent button labels (English + Indonesian) for one screen. We advance
# through them in order so an ambiguous label on a later screen can't be tapped
# early. Discovered on com.ss.android.ugc.trill; tune here if TikTok's UI shifts.
POST_FLOW_STEPS: tuple[tuple[str, ...], ...] = (
    ("Foto", "Photo"),                              # share sheet → post as a photo
    ("Berikutnya", "Next", "Selanjutnya"),          # editor → next
    ("Posting", "Post", "Posting sekarang", "Kirim"),  # final → publish
)

# Text fields where a caption/title can be typed (tapped before the final post).
CAPTION_HINTS = (
    "Tambahkan judul yang menarik",
    "Tambahkan deskripsi",
    "Add a title",
    "Add caption",
    "Tell viewers about your post",
)

# After a successful post, wait this long (so the upload finishes) before
# force-stopping TikTok. (Per-step pacing STEP_DELAY/STEP_RETRIES and the
# low-level UI primitives are shared via tiktok_ui.)
POST_SUCCESS_KILL_DELAY = 8.0

# Cap the number of hashtags in the on-device caption text; extras beyond this
# (counting left-to-right) are dropped. The published MQTT message keeps them all.
MAX_HASHTAGS = 5
_HASHTAG_RE = re.compile(r"#\w+", re.UNICODE)

# TikTok's single text field caps the caption at ~90 chars on-device; anything
# beyond is silently dropped (so a long caption swallows the whole description).
# We pre-truncate the combined text to this length (at a word boundary) so the
# result is predictable and the post still succeeds. The MQTT message keeps the
# full text.
MAX_POST_CHARS = 90


logger = logging.getLogger(__name__)


class TikTokPostError(Exception):
    """Raised when the auto-post flow fails."""


def _resolve_content_uri(remote_path: str, serial: Optional[str]) -> Optional[str]:
    """
    Resolve a /sdcard/... file to its MediaStore content:// URI so it can be
    shared to another app (file:// URIs are blocked by scoped storage).

    Matches on `_display_name` (the filename) rather than `_data` (the full
    path): on scoped-storage / MIUI devices a `_data` WHERE clause often returns
    nothing even though the file is indexed. Returns None if not indexed yet.
    """
    name = remote_path.rsplit("/", 1)[-1]
    # Escape single quotes in the filename for the SQL-ish WHERE clause.
    safe = name.replace("'", "''")
    out = run_adb(
        [
            "shell", "content", "query",
            "--uri", "content://media/external/images/media",
            "--projection", "_id",
            "--where", f"\"_display_name='{safe}'\"",
        ],
        serial=serial,
    )
    ids = re.findall(r"_id=(\d+)", out)
    if not ids:
        return None
    # If the same filename appears more than once, the most recently inserted
    # row (highest _id) is the one we just pushed.
    return f"content://media/external/images/media/{max(ids, key=int)}"


def open_in_tiktok(
    remote_path: str,
    *,
    serial: Optional[str] = None,
    package: Optional[str] = None,
) -> str:
    """
    Phase 1 — open TikTok's composer with the image attached via a SEND intent.

    Returns the package used. Raises TikTokPostError if TikTok isn't installed
    or the image can't be resolved to a shareable URI.
    """
    pkg = package or _installed_package(serial)
    if not pkg:
        raise TikTokPostError(
            f"TikTok not found on device. Looked for: {', '.join(TIKTOK_PACKAGES)}"
        )

    uri = _resolve_content_uri(remote_path, serial)
    if not uri:
        raise TikTokPostError(
            f"Image {remote_path} is not yet indexed in MediaStore — "
            "push it with media scan enabled first."
        )

    run_adb(
        [
            "shell", "am", "start",
            "-a", "android.intent.action.SEND",
            "-t", "image/*",
            "--eu", "android.intent.extra.STREAM", uri,
            "--grant-read-uri-permission",
            "-p", pkg,
        ],
        serial=serial,
    )
    return pkg


def _type_caption(text: str, serial: Optional[str]) -> bool:
    """
    Tap the caption/title field and type `text`. Best-effort; True if typed.

    The caption + description are typed in a single pass with the line break
    flattened to a space. TikTok's caption box is one field whose soft-keyboard
    ENTER fires an IME action (Done/Next-style) that steals focus — so pressing
    KEYCODE_ENTER between "caption" and "description" left the description typed
    into a defocused field (it never landed). Typing it as one run keeps the whole
    text in the field; the published MQTT message still carries the original
    newline-separated text.
    """
    field = _find_tappable(_dump_ui(serial), CAPTION_HINTS)
    if not field:
        return False
    _tap(serial, *field)
    time.sleep(1.0)

    _input_line(text.replace("\n", " "), serial)
    time.sleep(0.3)

    # Dismiss the keyboard so it doesn't cover the Post button.
    run_adb(["shell", "input", "keyevent", "111"], serial=serial)  # KEYCODE_ESCAPE
    time.sleep(0.5)
    return True


def _limit_hashtags(text: str, limit: int = MAX_HASHTAGS) -> str:
    """Keep only the first `limit` hashtags; drop the rest (collapsing the gap)."""
    seen = 0

    def _repl(match: "re.Match[str]") -> str:
        nonlocal seen
        seen += 1
        return match.group(0) if seen <= limit else ""

    capped = _HASHTAG_RE.sub(_repl, text)
    # Tidy whitespace left where dropped hashtags used to be.
    return re.sub(r"[ \t]{2,}", " ", capped).strip()


def build_post_text(caption: Optional[str], description: Optional[str]) -> str:
    """
    Combine caption + description into TikTok's single text field.

    TikTok has only one text box, so the caption (hook + hashtags) goes first and
    the description follows on a new line. Either may be empty. Hashtags across the
    combined text are capped at MAX_HASHTAGS (extras dropped left-to-right); the
    published MQTT message still carries the full text.
    """
    cap = (caption or "").strip()
    desc = (description or "").strip()
    combined = f"{cap}\n{desc}" if cap and desc else (cap or desc)
    return _truncate_post_text(_limit_hashtags(combined))


def _truncate_post_text(text: str, limit: int = MAX_POST_CHARS) -> str:
    """Cap `text` at `limit` chars, cutting at the last word boundary if possible.

    TikTok's caption field drops anything past ~MAX_POST_CHARS, so we truncate
    ourselves to keep the cut clean (no mid-word chop) rather than letting the
    device silently swallow the overflow.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    # Prefer cutting at the last whitespace so we don't slice a word/hashtag in half,
    # unless that would throw away most of the text.
    cut = head.rsplit(None, 1)[0] if " " in head.strip() else head
    if len(cut) < limit // 2:
        cut = head
    return cut.rstrip()


def post(
    remote_path: str,
    *,
    caption: Optional[str] = None,
    description: Optional[str] = None,
    serial: Optional[str] = None,
    package: Optional[str] = None,
    auto_post: bool = False,
    account: Optional[str] = None,
    dry_run: bool = False,
) -> str:
    """
    Post an already-pushed image to TikTok.

    If `account` is given, make sure that TikTok account is active first (switching
    via the in-app switcher); if it can't be confirmed, return "wrong_account"
    WITHOUT opening the composer, so we never post to the wrong account.

    If dry_run is True, walk the post flow and type the caption exactly as a real
    post would, but STOP before the final Post tap — leaving the composer open with
    the (possibly truncated) caption visible for inspection. Returns "dry_run" and
    never publishes.

    Phase 1 always opens the composer. If auto_post is False, returns "composer_open"
    (left open on purpose for manual finishing). If auto_post is True, attempts to
    advance through Next/Post screens and returns "posted" on success or
    "needs_manual" if a screen wasn't recognized. On every error outcome
    ("wrong_account"/"needs_manual") TikTok is force-stopped so it isn't left open;
    the message stays spooled for --retry.

    The caption and description are combined (see build_post_text) into TikTok's
    single text field — caption first, description on the next line.

    Raises:
        TikTokPostError: If Phase 1 itself fails (no TikTok / unshareable image).
    """
    # Serialize the whole flow against the single device — only one consumer
    # process may drive the phone at a time (see core/device_lock.py).
    with device_lock(serial):
        if account:
            try:
                ensure_account(account, serial=serial, package=package)
            except TikTokProfileError as e:
                logger.warning("  wrong account: %s", e)
                _force_stop(package, serial)  # don't leave TikTok open on an error
                return "wrong_account"

        pkg = open_in_tiktok(remote_path, serial=serial, package=package)
        time.sleep(STEP_DELAY)

        if not auto_post and not dry_run:
            return "composer_open"

        post_text = build_post_text(caption, description)

        # Phase 2 — walk the ordered post flow. The final step is the actual publish;
        # type the post text (if any) on the screen just before it.
        last_idx = len(POST_FLOW_STEPS) - 1
        for idx, labels in enumerate(POST_FLOW_STEPS):
            if idx == last_idx and post_text:
                _type_caption(post_text, serial)  # best-effort, never fatal
            if idx == last_idx and dry_run:
                # Caption typed, on the final screen — stop here WITHOUT tapping Post,
                # leaving the composer open so the on-device text can be inspected.
                logger.info("  dry-run: caption typed, leaving composer open (NOT posting)")
                return "dry_run"
            if not _wait_and_tap(labels, serial):
                # A screen we didn't recognize — force-stop TikTok rather than leaving
                # the composer open; the message stays spooled for --retry.
                _force_stop(pkg, serial)
                return "needs_manual"

        # Posted successfully — wait for the upload to finish, then close TikTok so
        # it isn't left running.
        time.sleep(POST_SUCCESS_KILL_DELAY)
        _force_stop(pkg, serial)
        return "posted"


def caption_from_imagekit(image_path: str, *, folder: str = "/tiktok") -> tuple[Optional[str], Optional[str]]:
    """
    Look up (caption, description) for an on-phone image by matching its base
    filename against the ImageKit folder listing.

    The pipeline pushes `tiktok_<ts>.<ext>` to the phone and uploads
    `tiktok_<ts>_<unique>.<ext>` to ImageKit, so the ImageKit name shares the
    phone file's base name. Returns (None, None) if no match / lookup fails.
    """
    from imagekit_source import list_images, ImageKitSourceError

    name = image_path.rsplit("/", 1)[-1]
    base = name.rsplit(".", 1)[0]
    try:
        files = list_images(folder=folder, limit=200)  # newest first
    except ImageKitSourceError:
        return None, None

    for f in files:
        ik_base = (f.get("name") or "").rsplit(".", 1)[0]
        if ik_base == base or ik_base.startswith(base + "_"):
            cm = f.get("customMetadata") or {}
            return cm.get("caption"), cm.get("description")
    return None, None


def list_gallery(serial: Optional[str] = None, folder: str = "/sdcard/Pictures") -> list[str]:
    """List image file paths already on the phone under `folder`."""
    try:
        out = run_adb(["shell", "ls", "-1", folder], serial=serial)
    except PhonePushError:
        return []
    exts = (".jpg", ".jpeg", ".png", ".webp")
    return [
        f"{folder.rstrip('/')}/{line.strip()}"
        for line in out.splitlines()
        if line.strip().lower().endswith(exts)
    ]


def _cli() -> int:
    import argparse
    import sys
    from core.env_loader import load_env
    from core.logging_setup import setup_logging
    load_env()
    setup_logging("tiktok-post")

    parser = argparse.ArgumentParser(
        description="Post an image that is ALREADY on the phone to TikTok (no download)."
    )
    parser.add_argument("path", nargs="?", help="On-device image path, e.g. /sdcard/Pictures/foo.jpg")
    parser.add_argument("--list", action="store_true", help="List gallery images on the phone and exit")
    parser.add_argument("--gallery", default="/sdcard/Pictures", help="Gallery folder to list/pick from (default: /sdcard/Pictures)")
    parser.add_argument("--caption", default=None, help="Caption text (hook + hashtags; used by auto-post phase)")
    parser.add_argument("--description", default=None, help="Description appended after the caption on a new line")
    parser.add_argument("--from-imagekit", action="store_true", help="Auto-fetch caption + description from the image's ImageKit metadata")
    parser.add_argument("--folder", default="/tiktok", help="ImageKit folder to look up metadata in (with --from-imagekit, default: /tiktok)")
    parser.add_argument("--serial", default=None, help="Target device serial (if multiple phones)")
    parser.add_argument("--package", default=None, help="Override TikTok package name")
    parser.add_argument("--account", default=None, help="Target TikTok @handle to switch to before posting")
    parser.add_argument("--auto-post", action="store_true", help="Drive Next/Post automatically (brittle; actually publishes)")
    parser.add_argument("--dry-run", action="store_true", help="Walk the flow and type the caption, but STOP before the final Post tap (leaves composer open for inspection; never publishes)")
    args = parser.parse_args()

    if args.list:
        imgs = list_gallery(args.serial, args.gallery)
        print(f"{len(imgs)} image(s) in {args.gallery}:")
        for p in imgs:
            print(f"  {p}")
        return 0

    if not args.path:
        parser.error("provide an on-device image path, or use --list to see options")

    caption, description = args.caption, args.description
    if args.from_imagekit:
        fcap, fdesc = caption_from_imagekit(args.path, folder=args.folder)
        # Explicit flags win; otherwise use what ImageKit returned.
        caption = caption or fcap
        description = description or fdesc
        if fcap or fdesc:
            print(f"From ImageKit — caption: {caption!r}")
            print(f"             description: {(description or '')[:70]!r}")
        else:
            logger.warning("From ImageKit — no metadata found for %s", args.path.rsplit('/', 1)[-1])

    try:
        status = post(
            args.path,
            caption=caption,
            description=description,
            serial=args.serial,
            package=args.package,
            auto_post=args.auto_post,
            account=args.account,
            dry_run=args.dry_run,
        )
    except TikTokPostError as e:
        logger.error("Error: %s", e)
        return 1

    logger.info("Result: %s", status)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
