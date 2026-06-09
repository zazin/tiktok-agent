#!/usr/bin/env python3
"""
Check (and switch) the active TikTok account by driving the on-device UI over adb.

The device has multiple TikTok accounts logged in (TikTok's in-app account
switcher). A piece of work may target a specific account, so before posting or
commenting we must make sure the *right* account is active. This module:

  1. opens the Profile tab and reads the currently-active @handle,
  2. if it isn't the target, opens the account switcher and taps the target row,
  3. re-reads to CONFIRM the switch — and refuses (raises) if it can't, so the
     caller never posts to the wrong account.

Like tiktok_poster.py / tiktok_commenter.py this is inherently brittle (it depends
on TikTok's current UI, locale and A/B variant) and may be against TikTok's Terms
of Service. It fails SAFE: if the target can't be confirmed active it raises
TikTokProfileError and the caller declines to act.

The low-level UI helpers (`_dump_ui`, `_find_tappable`, `_tap`, …) are
intentionally COPIED from tiktok_poster.py — the project has no shared adb-UI util
module, so each feature carries its own primitives. This one module is shared by
BOTH consumers (poster and commenter) because the account check is identical.

The phone must be unlocked and TikTok installed + logged in (with the target
account already added to the in-app switcher).
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

# The bottom-nav "Profile" button (taps to the profile tab where the handle shows).
PROFILE_TAB_LABELS = ("Profile", "Profil", "Profilku", "Me")

# The control on the profile header that opens the account switcher — often the
# username/handle itself with a dropdown chevron, or a dedicated "Switch account".
SWITCH_OPEN_LABELS = (
    "Switch account",
    "Switch accounts",
    "Ganti akun",
    "Tukar akun",
    "Beralih akun",
    "Account",
    "Akun",
)

# How an @handle text node looks in the dump (profile header + switcher rows).
HANDLE_RE = re.compile(r"@[A-Za-z0-9._]{2,}")

# Per-step pacing: how long to wait for a screen, and retries while it loads.
STEP_DELAY = 2.5
STEP_RETRIES = 6


class TikTokProfileError(Exception):
    """Raised when the target account can't be confirmed active (so don't act)."""


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


def _tap(serial: Optional[str], x: int, y: int) -> None:
    run_adb(["shell", "input", "tap", str(x), str(y)], serial=serial)


def _wait_and_tap(
    labels: tuple[str, ...],
    serial: Optional[str],
    *,
    retries: int = STEP_RETRIES,
    delay: float = STEP_DELAY,
) -> bool:
    """Poll the UI until a node EXACTLY matching `labels` appears, then tap it. True if tapped."""
    for _ in range(retries):
        target = _find_tappable(_dump_ui(serial), labels)
        if target:
            _tap(serial, *target)
            time.sleep(delay)
            return True
        time.sleep(delay)
    return False


# ---- account helpers ---------------------------------------------------------

def _norm(handle: Optional[str]) -> str:
    """Normalize an @handle for comparison: strip a leading @ and lowercase."""
    return (handle or "").strip().lstrip("@").lower()


def _first_handle(xml: str) -> Optional[str]:
    """Return the first @handle found in a UI dump's text/content-desc, or None."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter("node"):
        for attr in (node.get("text"), node.get("content-desc")):
            if attr:
                m = HANDLE_RE.search(attr)
                if m:
                    return m.group(0)
    return None


def _find_handle_node(xml: str, target: str) -> Optional[tuple[int, int]]:
    """Find the center of the first node whose text/content-desc contains @<target>."""
    want = _norm(target)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter("node"):
        for attr in (node.get("text"), node.get("content-desc")):
            if not attr:
                continue
            for m in HANDLE_RE.finditer(attr):
                if _norm(m.group(0)) == want:
                    center = _center_of_bounds(node.get("bounds", ""))
                    if center:
                        return center
    return None


def _open_profile(serial: Optional[str], package: Optional[str]) -> None:
    """Make sure TikTok is foregrounded, then tap the Profile tab."""
    pkg = package or _installed_package(serial)
    if not pkg:
        raise TikTokProfileError(
            f"TikTok not found on device. Looked for: {', '.join(TIKTOK_PACKAGES)}"
        )
    # Bring TikTok to the foreground (no-op if already there) before navigating.
    run_adb(["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"], serial=serial)
    time.sleep(STEP_DELAY)
    if not _wait_and_tap(PROFILE_TAB_LABELS, serial):
        raise TikTokProfileError("could not find the Profile tab — calibrate PROFILE_TAB_LABELS")
    time.sleep(STEP_DELAY)


def current_account(serial: Optional[str] = None, package: Optional[str] = None) -> Optional[str]:
    """Open the Profile tab and return the active @handle (or None if unreadable)."""
    _open_profile(serial, package)
    return _first_handle(_dump_ui(serial))


def switch_account(target: str, *, serial: Optional[str] = None, package: Optional[str] = None) -> bool:
    """Open the account switcher and tap the row matching `target`. True if tapped.

    Assumes we are already on the profile tab. Opens the switcher via
    SWITCH_OPEN_LABELS, then taps the row whose @handle equals `target`.
    """
    # Open the account switcher from the profile header.
    if not _wait_and_tap(SWITCH_OPEN_LABELS, serial):
        return False
    time.sleep(STEP_DELAY)
    # Find and tap the target account's row.
    for _ in range(STEP_RETRIES):
        row = _find_handle_node(_dump_ui(serial), target)
        if row:
            _tap(serial, *row)
            time.sleep(STEP_DELAY)
            return True
        time.sleep(STEP_DELAY)
    return False


def ensure_account(target: str, *, serial: Optional[str] = None, package: Optional[str] = None) -> None:
    """Make sure `target` is the active TikTok account; switch to it if not.

    Reads the current handle on the Profile tab. If it already matches `target`,
    returns. Otherwise opens the account switcher, taps the target row, and
    re-reads to CONFIRM. Raises TikTokProfileError if the target can't be confirmed
    active (so the caller declines to post/comment to the wrong account).
    """
    want = _norm(target)
    if not want:
        return  # no target → nothing to enforce

    current = current_account(serial, package)
    if _norm(current) == want:
        return  # already on the right account

    if not switch_account(target, serial=serial, package=package):
        raise TikTokProfileError(
            f"could not switch to @{want} (current: {current or 'unknown'}) — "
            "is it added to the switcher? calibrate SWITCH_OPEN_LABELS"
        )

    # Confirm the switch actually landed on the target account.
    confirmed = _first_handle(_dump_ui(serial))
    if _norm(confirmed) != want:
        raise TikTokProfileError(
            f"switch did not confirm @{want} (now: {confirmed or 'unknown'})"
        )


def _cli() -> int:
    import argparse
    import sys
    from env_loader import load_env
    load_env()

    parser = argparse.ArgumentParser(
        description="Read or switch the active TikTok account over adb (calibration tool)."
    )
    parser.add_argument("account", nargs="?", help="Target @handle to switch to (omit to just read current)")
    parser.add_argument("--serial", default=None, help="Target device serial (if multiple phones)")
    parser.add_argument("--package", default=None, help="Override TikTok package name")
    args = parser.parse_args()

    try:
        if not args.account:
            handle = current_account(args.serial, args.package)
            print(f"Current account: {handle or 'unknown'}")
            return 0
        ensure_account(args.account, serial=args.serial, package=args.package)
    except (TikTokProfileError, PhonePushError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Active account is now @{_norm(args.account)}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
