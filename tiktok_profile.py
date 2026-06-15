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

The low-level UI helpers (`dump_ui`, `find_tappable`, `tap`, …) live in the shared
`tiktok_ui` module. This module (the account check, shared by BOTH the poster and
commenter) keeps only the profile-header / account-switcher specific logic.

The phone must be unlocked and TikTok installed + logged in (with the target
account already added to the in-app switcher).
"""

from __future__ import annotations

import logging
import re
import shlex
import time
import xml.etree.ElementTree as ET
from typing import Optional

from core.adb_pusher import run_adb, PhonePushError
from core.tiktok_ui import (
    TIKTOK_PACKAGES,
    STEP_DELAY,
    STEP_RETRIES,
    installed_package as _installed_package,
    dump_ui as _dump_ui,
    center_of_bounds as _center_of_bounds,
    bounds_of as _bounds_of,
    tap as _tap,
)


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

# (Per-step pacing STEP_DELAY/STEP_RETRIES and the low-level UI primitives are
# shared via tiktok_ui.)

# Selecting an account row needs a touch with a REAL press duration: an instant
# `input tap` closes the sheet but is silently ignored by TikTok's switch handler
# (verified on com.ss.android.ugc.trill — the row's click detector wants a DOWN→UP
# gap). A same-point `input swipe` of this many ms supplies that. Kept well under the
# long-press threshold so it doesn't open the row's context menu instead.
ROW_PRESS_MS = 150


logger = logging.getLogger(__name__)


def _press_row(serial: Optional[str], x: int, y: int) -> None:
    """Tap an account-switcher row with a real press duration (see ROW_PRESS_MS).

    Unlike the shared instant `tap`, this issues a same-point `input swipe` so the
    touch lingers long enough for TikTok to register the switch.
    """
    run_adb(
        ["shell", "input", "swipe", str(x), str(y), str(x), str(y), str(ROW_PRESS_MS)],
        serial=serial,
    )


class TikTokProfileError(Exception):
    """Raised when the target account can't be confirmed active (so don't act)."""


# ---- account helpers ---------------------------------------------------------

def _norm(handle: Optional[str]) -> str:
    """Normalize an @handle for comparison: strip a leading @ and lowercase."""
    return (handle or "").strip().lstrip("@").lower()


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


def _open_profile(serial: Optional[str], package: Optional[str]) -> str:
    """Open the current user's own profile via a deep link and return its @handle.

    The home feed blocks `uiautomator dump`, so we can't reliably tap the bottom-nav
    Profile tab; a VIEW intent on snssdk1233://profile lands on the profile directly.

    After firing the intent the header doesn't render instantly — a cold/slow profile
    load, a still-waking device, or the feed not yet dismissed can leave the first dump
    without a readable handle. So we POLL (up to STEP_RETRIES, like the other UI flows)
    for the header to appear rather than dumping once; a single shot here produced
    spurious "wrong_account" failures on retry bursts. Returns the handle so the caller
    needn't re-dump (which is itself a flakiness point). Raises if no deep link lands.
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
                ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(uri), "-p", pkg],
                serial=serial,
            )
        except PhonePushError as e:
            last_err = e
            continue  # this deep link couldn't even be fired; try the next one
        # Poll for the header handle to render before giving up on this deep link.
        for _ in range(STEP_RETRIES):
            time.sleep(STEP_DELAY)
            handle = _first_handle(_dump_ui(serial))
            if handle:
                return handle
    raise TikTokProfileError(
        f"could not open the profile via deep link ({', '.join(PROFILE_DEEPLINKS)})"
        + (f": {last_err}" if last_err else "")
    )


def current_account(serial: Optional[str] = None, package: Optional[str] = None) -> Optional[str]:
    """Open the profile (deep link) and return the active @handle.

    Raises TikTokProfileError if no deep link reaches a readable profile header.
    """
    return _open_profile(serial, package)


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
    # Find and select the target account's row. Matched by handle on a FRESH dump
    # each pass, so the order of rows in the sheet (which TikTok may reshuffle) never
    # matters. Selected with a real press, not an instant tap (see _press_row).
    for _ in range(STEP_RETRIES):
        row = _find_account_row(_dump_ui(serial), target)
        if row:
            _press_row(serial, *row)
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
    from core.env_loader import load_env
    from core.logging_setup import setup_logging
    load_env()
    setup_logging("tiktok-profile")

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
            logger.info("Current account: %s", handle or 'unknown')
            return 0
        ensure_account(args.account, serial=args.serial, package=args.package)
    except (TikTokProfileError, PhonePushError) as e:
        logger.error("Error: %s", e)
        return 1

    logger.info("Active account is now @%s", _norm(args.account))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
