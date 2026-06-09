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

# ---- UI selectors — calibrated on-device (com.ss.android.ugc.trill, ID locale) -
# Re-verify with a real `uiautomator dump` if TikTok's UI shifts. Discovered facts:
#  * The HOME FEED blocks `uiautomator dump` (it returns the launcher behind it), so
#    we open the profile by DEEP LINK rather than by tapping the bottom-nav tab.
#  * The profile header shows the handle as a "@name" TEXT node, and the display
#    NAME just above it is a clickable button — that button opens the account sheet.
#  * In the "Beralih akun" (Switch account) sheet, each account row is a clickable
#    node whose CONTENT-DESC is the bare handle WITHOUT a leading "@" (e.g. "captgani").

# Deep links that open the current user's own profile (no feed dump needed). Tried
# in order until one lands on the profile screen.
PROFILE_DEEPLINKS = (
    "snssdk1233://profile",
    "snssdk1233://user/profile",
)

# A profile-screen handle: a "@name" TEXT node. Require at least one LETTER so we
# don't match untranslated resource refs like content-desc="@2131894056".
HANDLE_RE = re.compile(r"@[A-Za-z0-9._]*[A-Za-z][A-Za-z0-9._]*")

# Fallback only: the bottom-nav "Profile" button, if a deep link ever fails AND a
# dump is available (it isn't on the feed). Kept for manual/diagnostic use.
PROFILE_TAB_LABELS = ("Profil", "Profile", "Profilku", "Me")

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


def _bounds_of(bounds: str) -> Optional[tuple[int, int, int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return None
    return tuple(int(v) for v in m.groups())  # type: ignore[return-value]


def _first_handle(xml: str) -> Optional[str]:
    """Return the active @handle from the profile header (a "@name" TEXT node).

    Only `text` is scanned (content-desc carries untranslated "@1234" resource refs),
    and HANDLE_RE requires a letter, so resource refs can't be mistaken for a handle.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter("node"):
        text = node.get("text") or ""
        m = HANDLE_RE.search(text)
        if m:
            return m.group(0)
    return None


def _find_account_row(xml: str, target: str) -> Optional[tuple[int, int]]:
    """In the switch-account sheet, find the row for `target` (matched WITHOUT '@').

    Each row is a clickable node whose content-desc (or text) is the bare handle,
    e.g. content-desc="captgani". Compared normalized (leading '@' stripped, lowered).
    """
    want = _norm(target)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        for attr in (node.get("content-desc"), node.get("text")):
            if attr and _norm(attr) == want:
                center = _center_of_bounds(node.get("bounds", ""))
                if center:
                    return center
    return None


def _find_switch_trigger(xml: str) -> Optional[tuple[int, int]]:
    """Find the profile-header button that opens the account sheet (the display NAME).

    It's the clickable node directly ABOVE the "@handle" text node, sharing its left
    edge — i.e. the bold display name. We anchor on the handle (already located to
    read the current account) so we don't depend on knowing the display name text.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None

    handle_box: Optional[tuple[int, int, int, int]] = None
    for node in root.iter("node"):
        if HANDLE_RE.search(node.get("text") or ""):
            handle_box = _bounds_of(node.get("bounds", ""))
            if handle_box:
                break
    if handle_box is None:
        return None

    hx1, hy1, _, _ = handle_box
    best: Optional[tuple[int, int, int]] = None  # (y2, cx, cy) — closest above
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        text = (node.get("text") or "").strip()
        if not text or text.startswith("@"):  # skip the handle itself / empty nodes
            continue
        b = _bounds_of(node.get("bounds", ""))
        if not b:
            continue
        x1, y1, x2, y2 = b
        if abs(x1 - hx1) <= 40 and y2 <= hy1 + 10:  # shares left edge, sits above
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            if best is None or y2 > best[0]:  # nearest above the handle
                best = (y2, cx, cy)
    return (best[1], best[2]) if best else None


def _open_profile(serial: Optional[str], package: Optional[str]) -> None:
    """Open the current user's own profile via a deep link (works even from the feed).

    The home feed blocks `uiautomator dump`, so we can't reliably tap the bottom-nav
    Profile tab; a VIEW intent on snssdk1233://profile lands on the profile directly.
    """
    pkg = package or _installed_package(serial)
    if not pkg:
        raise TikTokProfileError(
            f"TikTok not found on device. Looked for: {', '.join(TIKTOK_PACKAGES)}"
        )
    last_err = None
    for uri in PROFILE_DEEPLINKS:
        try:
            run_adb(
                ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", uri, "-p", pkg],
                serial=serial,
            )
            time.sleep(STEP_DELAY)
            # Confirm we actually reached a profile (the header handle is readable).
            if _first_handle(_dump_ui(serial)):
                return
        except PhonePushError as e:
            last_err = e
    raise TikTokProfileError(
        f"could not open the profile via deep link ({', '.join(PROFILE_DEEPLINKS)})"
        + (f": {last_err}" if last_err else "")
    )


def current_account(serial: Optional[str] = None, package: Optional[str] = None) -> Optional[str]:
    """Open the profile (deep link) and return the active @handle (or None)."""
    _open_profile(serial, package)
    return _first_handle(_dump_ui(serial))


def switch_account(target: str, *, serial: Optional[str] = None, package: Optional[str] = None) -> bool:
    """Open the account sheet and tap the row matching `target`. True if tapped.

    Assumes the profile screen is open. Taps the display-name button to open the
    "Beralih akun" sheet, then taps the row whose bare handle equals `target`.
    """
    trigger = _find_switch_trigger(_dump_ui(serial))
    if not trigger:
        return False
    _tap(serial, *trigger)
    time.sleep(STEP_DELAY)
    # Find and tap the target account's row.
    for _ in range(STEP_RETRIES):
        row = _find_account_row(_dump_ui(serial), target)
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

    # current_account left us on the profile screen, so switch_account can proceed.
    if not switch_account(target, serial=serial, package=package):
        raise TikTokProfileError(
            f"could not switch to @{want} (current: {current or 'unknown'}) — "
            "is it added to the in-app account switcher?"
        )

    # After a switch TikTok reloads to the feed (which blocks dumps), so re-open the
    # profile by deep link before reading the handle to CONFIRM the switch landed.
    confirmed = current_account(serial, package)
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
