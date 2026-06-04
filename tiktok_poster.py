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


# TikTok package names: global app, then the older/alt package as fallback.
TIKTOK_PACKAGES = ("com.zhiliaoapp.musically", "com.ss.android.ugc.trill")

# Button labels we look for, in the order we try to advance the composer.
ADVANCE_LABELS = ("Next", "Post", "Selanjutnya", "Posting", "Kirim")

# Per-step UI dump/tap pacing and overall Phase-2 attempt budget.
STEP_DELAY = 2.0
MAX_ADVANCE_STEPS = 6


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
    Returns None if it isn't indexed yet.
    """
    out = run_adb(
        [
            "shell", "content", "query",
            "--uri", "content://media/external/images/media",
            "--projection", "_id",
            "--where", f"\"_data='{remote_path}'\"",
        ],
        serial=serial,
    )
    m = re.search(r"_id=(\d+)", out)
    if not m:
        return None
    return f"content://media/external/images/media/{m.group(1)}"


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


def post(
    remote_path: str,
    *,
    caption: Optional[str] = None,
    serial: Optional[str] = None,
    package: Optional[str] = None,
    auto_post: bool = False,
) -> str:
    """
    Post an already-pushed image to TikTok.

    Phase 1 always opens the composer. If auto_post is False, returns "composer_open".
    If auto_post is True, attempts to advance through Next/Post screens and returns
    "posted" on success or "needs_manual" if a screen wasn't recognized.

    Raises:
        TikTokPostError: If Phase 1 itself fails (no TikTok / unshareable image).
    """
    open_in_tiktok(remote_path, serial=serial, package=package)
    time.sleep(STEP_DELAY)

    if not auto_post:
        return "composer_open"

    # Phase 2 — best-effort: repeatedly find and tap an advance control.
    advanced = 0
    for _ in range(MAX_ADVANCE_STEPS):
        xml = _dump_ui(serial)
        target = _find_tappable(xml, ADVANCE_LABELS)
        if not target:
            # Nothing recognizable to tap — stop and leave it for the human.
            return "posted" if advanced else "needs_manual"
        _tap(serial, *target)
        advanced += 1
        time.sleep(STEP_DELAY)

    return "posted" if advanced else "needs_manual"
