#!/usr/bin/env python3
"""
Push image file(s) to an Android phone over USB using adb.

Drops the file into the phone's storage (default: /sdcard/Pictures/) and runs a
media-scanner broadcast so it shows up in the Gallery/Photos app immediately.

Ported from the tiktok-pipeline repo's phone_uploader.py — the tiktok-agent runs
on a computer with the phone attached, so it owns the adb side of the flow.

Requirements:
  - adb installed (`brew install android-platform-tools`)
  - Phone connected via USB with USB debugging enabled
  - The "Allow USB debugging" prompt accepted on the phone
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


# Pictures shows up in Gallery; Download is easier to find in a file manager.
DEFAULT_DEST = "/sdcard/Pictures"


class PhonePushError(Exception):
    """Raised when an adb push to the phone fails."""


def run_adb(args: list[str], *, serial: Optional[str] = None, timeout: int = 120) -> str:
    """Run an adb command and return stdout. Raises PhonePushError on failure."""
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise PhonePushError(
            "adb not found. Install it with `brew install android-platform-tools`."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise PhonePushError(f"adb command timed out: {' '.join(cmd)}") from e

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise PhonePushError(f"adb {' '.join(args)} failed: {err}")
    return proc.stdout.strip()


def list_devices() -> list[str]:
    """Return serials of connected, authorized devices."""
    out = run_adb(["devices"])
    serials: list[str] = []
    for line in out.splitlines()[1:]:  # skip "List of devices attached"
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def ensure_device(serial: Optional[str]) -> str:
    """Resolve which device to use, or raise a helpful error."""
    serials = list_devices()
    if not serials:
        raise PhonePushError(
            "No authorized Android device found. Check that:\n"
            "  - the phone is plugged in via USB,\n"
            "  - USB debugging is enabled in Developer Options,\n"
            "  - you accepted the 'Allow USB debugging' prompt on the phone.\n"
            "Run `adb devices` to verify."
        )
    if serial:
        if serial not in serials:
            raise PhonePushError(
                f"Device {serial!r} not connected. Available: {', '.join(serials)}"
            )
        return serial
    if len(serials) > 1:
        raise PhonePushError(
            f"Multiple devices connected: {', '.join(serials)}. Pass a serial to pick one."
        )
    return serials[0]


def push_to_phone(
    image_path: str,
    *,
    dest_dir: str = DEFAULT_DEST,
    serial: Optional[str] = None,
    scan_media: bool = True,
) -> str:
    """
    Push a single image to the phone and return its remote path.

    Args:
        image_path: Local image file to push.
        dest_dir: Remote directory on the phone (e.g. /sdcard/Pictures).
        serial: Target device serial. Auto-detected if only one is connected.
        scan_media: If True, trigger a media-scan broadcast so the file
            appears in the Gallery immediately.

    Returns:
        The remote path the file was written to.

    Raises:
        PhonePushError: On any failure.
    """
    path = Path(image_path)
    if not path.is_file():
        raise PhonePushError(f"File not found: {path}")

    target = ensure_device(serial)
    remote = f"{dest_dir.rstrip('/')}/{path.name}"

    run_adb(["push", str(path), remote], serial=target)

    if scan_media:
        run_adb(
            [
                "shell",
                "am",
                "broadcast",
                "-a",
                "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                "-d",
                f"file://{remote}",
            ],
            serial=target,
        )

    return remote
