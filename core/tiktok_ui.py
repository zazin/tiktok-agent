#!/usr/bin/env python3
"""
tiktok_ui — shared low-level adb / UI-automation primitives for the TikTok flows.

The three TikTok-driving modules (`tiktok_poster.py`, `tiktok_commenter.py`,
`tiktok_profile.py`) all drive the on-device UI the same way: dump the UI tree
(`uiautomator dump`), find a node by its text / content-desc, and tap its center.
Those primitives used to be COPIED into each module; they now live here so the
three features share one implementation.

Everything routes through `adb_pusher.run_adb`, the single chokepoint for adb
calls. These helpers are intentionally generic (no post/comment/profile knowledge):
each feature keeps its own screen labels and flow logic and imports these as the
building blocks.

The phone must be unlocked and TikTok installed + logged in for any of this to work.
"""

from __future__ import annotations

import re
import shlex
import time
import xml.etree.ElementTree as ET
from typing import Optional

from .adb_pusher import run_adb, PhonePushError


# TikTok package names: global app, then the older/alt package as fallback.
TIKTOK_PACKAGES = ("com.zhiliaoapp.musically", "com.ss.android.ugc.trill")

# Per-step pacing: how long to wait for a screen, and retries while it loads.
STEP_DELAY = 2.5
STEP_RETRIES = 6


def installed_package(serial: Optional[str]) -> Optional[str]:
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


def dump_ui(serial: Optional[str]) -> str:
    """Dump the current UI hierarchy XML and return it as text."""
    run_adb(["shell", "uiautomator", "dump", "/sdcard/window_dump.xml"], serial=serial)
    return run_adb(["shell", "cat", "/sdcard/window_dump.xml"], serial=serial)


def center_of_bounds(bounds: str) -> Optional[tuple[int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return None
    x1, y1, x2, y2 = (int(v) for v in m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def bounds_of(bounds: str) -> Optional[tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return None
    return tuple(int(v) for v in m.groups())  # type: ignore[return-value]


def find_tappable(xml: str, labels: tuple[str, ...]) -> Optional[tuple[int, int]]:
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
            center = center_of_bounds(node.get("bounds", ""))
            if center:
                return center
    return None


def find_partial(xml: str, substrings: tuple[str, ...]) -> Optional[tuple[int, int]]:
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
            center = center_of_bounds(node.get("bounds", ""))
            if center:
                return center
    return None


def find_bounds(xml: str, labels: tuple[str, ...]) -> Optional[tuple[int, int, int, int]]:
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
            b = bounds_of(node.get("bounds", ""))
            if b:
                return b
    return None


def tap(serial: Optional[str], x: int, y: int) -> None:
    run_adb(["shell", "input", "tap", str(x), str(y)], serial=serial)


def swipe(
    serial: Optional[str],
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    duration_ms: int = 400,
) -> None:
    """Swipe from (x1,y1) to (x2,y2) over `duration_ms` (e.g. to scroll a list)."""
    run_adb(
        ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
        serial=serial,
    )


def force_stop(package: Optional[str], serial: Optional[str]) -> None:
    """Force-stop TikTok (best-effort; never fatal).

    If `package` is None (e.g. an error before we resolved which package opened),
    stop every known TikTok package — force-stopping a non-running one is a no-op.
    """
    targets = [package] if package else list(TIKTOK_PACKAGES)
    for pkg in targets:
        try:
            run_adb(["shell", "am", "force-stop", pkg], serial=serial)
        except PhonePushError:
            pass


def screen_size(serial: Optional[str]) -> tuple[int, int]:
    """Return the device (width, height) in pixels from `wm size`."""
    out = run_adb(["shell", "wm", "size"], serial=serial)
    m = re.search(r"(\d+)x(\d+)", out)
    if not m:
        raise PhonePushError(f"could not parse screen size from: {out!r}")
    return int(m.group(1)), int(m.group(2))


def wait_and_tap(
    labels: tuple[str, ...],
    serial: Optional[str],
    *,
    retries: int = STEP_RETRIES,
    delay: float = STEP_DELAY,
) -> bool:
    """Poll the UI until a node EXACTLY matching `labels` appears, then tap it. True if tapped."""
    for _ in range(retries):
        target = find_tappable(dump_ui(serial), labels)
        if target:
            tap(serial, *target)
            time.sleep(delay)
            return True
        time.sleep(delay)
    return False


def wait_and_tap_partial(
    substrings: tuple[str, ...],
    serial: Optional[str],
    *,
    retries: int = STEP_RETRIES,
    delay: float = STEP_DELAY,
) -> bool:
    """Poll the UI until a node whose label CONTAINS a substring appears, then tap it."""
    for _ in range(retries):
        target = find_partial(dump_ui(serial), substrings)
        if target:
            tap(serial, *target)
            time.sleep(delay)
            return True
        time.sleep(delay)
    return False


def wait_for_bounds(
    labels: tuple[str, ...],
    serial: Optional[str],
    *,
    retries: int = STEP_RETRIES,
    delay: float = STEP_DELAY,
) -> Optional[tuple[int, int, int, int]]:
    """Poll the UI until a node matching `labels` appears; return its bounds (not a tap)."""
    for _ in range(retries):
        b = find_bounds(dump_ui(serial), labels)
        if b:
            return b
        time.sleep(delay)
    return None


def ascii_for_input(line: str) -> str:
    """ASCII-fold a line the way `adb input text` requires (spaces kept as spaces).

    `adb input text` can't enter emoji/non-ASCII — those are stripped. Quote chars
    confuse the shell, so they're removed; runs of whitespace collapse to one space.
    Returns "" when nothing typeable remains (so a caller can skip a blank submit).
    """
    ascii_only = line.encode("ascii", "ignore").decode()
    ascii_only = re.sub(r"[\"'`]", "", ascii_only)
    return re.sub(r"[ \t]+", " ", ascii_only).strip()


def input_line(line: str, serial: Optional[str]) -> bool:
    """Type one line into the focused field via `adb input text`. True if anything typed.

    Spaces are encoded as %s (adb's escape). Returns False if nothing typeable
    remains after ASCII-folding, so the caller can avoid submitting a blank value.
    """
    safe = ascii_for_input(line).replace(" ", "%s")
    if not safe:
        return False
    # `adb shell` re-parses its arguments through the device's /system/bin/sh, so
    # any shell metacharacter in the text — ()#&;|<>$`*?~ etc. — would be interpreted
    # and break the command (e.g. "(Hada Labo)" → "syntax error: unexpected '('").
    # Quote it so the device shell passes it through literally; `input` still expands
    # the %s placeholders back into spaces inside the quoted token.
    run_adb(["shell", "input", "text", shlex.quote(safe)], serial=serial)
    return True
