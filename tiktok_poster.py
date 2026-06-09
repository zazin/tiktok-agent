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

import re
import time
import xml.etree.ElementTree as ET
from typing import Optional

from adb_pusher import run_adb, PhonePushError
from tiktok_profile import ensure_account, TikTokProfileError


# TikTok package names: global app, then the older/alt package as fallback.
TIKTOK_PACKAGES = ("com.zhiliaoapp.musically", "com.ss.android.ugc.trill")

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

# Per-step pacing: how long to wait for a screen, and retries while it loads.
STEP_DELAY = 2.5
STEP_RETRIES = 6

# After a successful post, wait this long (so the upload finishes) before
# force-stopping TikTok.
POST_SUCCESS_KILL_DELAY = 8.0


class TikTokPostError(Exception):
    """Raised when the auto-post flow fails."""


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
    """Find the center of the first node whose text/content-desc matches a label."""
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


def _tap(serial: Optional[str], x: int, y: int) -> None:
    run_adb(["shell", "input", "tap", str(x), str(y)], serial=serial)


def _force_stop(package: str, serial: Optional[str]) -> None:
    """Force-stop TikTok (best-effort; never fatal)."""
    try:
        run_adb(["shell", "am", "force-stop", package], serial=serial)
    except PhonePushError:
        pass


def _wait_and_tap(
    labels: tuple[str, ...],
    serial: Optional[str],
    *,
    retries: int = STEP_RETRIES,
    delay: float = STEP_DELAY,
) -> bool:
    """Poll the UI until a node matching `labels` appears, then tap it. True if tapped."""
    for _ in range(retries):
        target = _find_tappable(_dump_ui(serial), labels)
        if target:
            _tap(serial, *target)
            time.sleep(delay)
            return True
        time.sleep(delay)
    return False


def _input_line(line: str, serial: Optional[str]) -> None:
    """Type one line into the focused field via `adb input text`."""
    # `adb input text` can't type emoji/non-ASCII — strip those (the full text
    # still lives in ImageKit metadata). Quotes confuse the shell; spaces -> %s.
    ascii_only = line.encode("ascii", "ignore").decode()
    ascii_only = re.sub(r"[\"'`]", "", ascii_only)
    safe = re.sub(r"[ \t]+", " ", ascii_only).strip().replace(" ", "%s")
    if safe:
        run_adb(["shell", "input", "text", safe], serial=serial)


def _type_caption(text: str, serial: Optional[str]) -> bool:
    """
    Tap the caption/title field and type `text`. Best-effort; True if typed.

    Newlines in `text` are entered as real line breaks (KEYCODE_ENTER), so a
    combined "caption\\ndescription" lands on separate lines in the post.
    """
    field = _find_tappable(_dump_ui(serial), CAPTION_HINTS)
    if not field:
        return False
    _tap(serial, *field)
    time.sleep(1.0)

    for i, line in enumerate(text.split("\n")):
        if i > 0:
            run_adb(["shell", "input", "keyevent", "66"], serial=serial)  # ENTER -> newline
            time.sleep(0.3)
        _input_line(line, serial)
        time.sleep(0.3)

    # Dismiss the keyboard so it doesn't cover the Post button.
    run_adb(["shell", "input", "keyevent", "111"], serial=serial)  # KEYCODE_ESCAPE
    time.sleep(0.5)
    return True


def build_post_text(caption: Optional[str], description: Optional[str]) -> str:
    """
    Combine caption + description into TikTok's single text field.

    TikTok has only one text box, so the caption (hook + hashtags) goes first and
    the description follows on a new line. Either may be empty.
    """
    cap = (caption or "").strip()
    desc = (description or "").strip()
    if cap and desc:
        return f"{cap}\n{desc}"
    return cap or desc


def post(
    remote_path: str,
    *,
    caption: Optional[str] = None,
    description: Optional[str] = None,
    serial: Optional[str] = None,
    package: Optional[str] = None,
    auto_post: bool = False,
    account: Optional[str] = None,
) -> str:
    """
    Post an already-pushed image to TikTok.

    If `account` is given, make sure that TikTok account is active first (switching
    via the in-app switcher); if it can't be confirmed, return "wrong_account"
    WITHOUT opening the composer, so we never post to the wrong account.

    Phase 1 always opens the composer. If auto_post is False, returns "composer_open".
    If auto_post is True, attempts to advance through Next/Post screens and returns
    "posted" on success or "needs_manual" if a screen wasn't recognized.

    The caption and description are combined (see build_post_text) into TikTok's
    single text field — caption first, description on the next line.

    Raises:
        TikTokPostError: If Phase 1 itself fails (no TikTok / unshareable image).
    """
    if account:
        try:
            ensure_account(account, serial=serial, package=package)
        except TikTokProfileError as e:
            print(f"  wrong account: {e}", flush=True)
            return "wrong_account"

    pkg = open_in_tiktok(remote_path, serial=serial, package=package)
    time.sleep(STEP_DELAY)

    if not auto_post:
        return "composer_open"

    post_text = build_post_text(caption, description)

    # Phase 2 — walk the ordered post flow. The final step is the actual publish;
    # type the post text (if any) on the screen just before it.
    last_idx = len(POST_FLOW_STEPS) - 1
    for idx, labels in enumerate(POST_FLOW_STEPS):
        if idx == last_idx and post_text:
            _type_caption(post_text, serial)  # best-effort, never fatal
        if not _wait_and_tap(labels, serial):
            # A screen we didn't recognize — stop and leave it for the human.
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
    from env_loader import load_env
    load_env()

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
            print(f"From ImageKit — no metadata found for {args.path.rsplit('/', 1)[-1]}", file=sys.stderr)

    try:
        status = post(
            args.path,
            caption=caption,
            description=description,
            serial=args.serial,
            package=args.package,
            auto_post=args.auto_post,
            account=args.account,
        )
    except TikTokPostError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Result: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
