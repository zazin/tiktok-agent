"""
Cross-process serialization of the single attached Android device.

This repo runs up to three independent consumer PROCESSES (`tiktok-agent`,
`tiktok-commenter`, `tiktok-comment-reader`) that all drive the same one phone
over adb/uiautomator. uiautomator cannot run in parallel on one device: two
overlapping `uiautomator dump`/tap flows produce stale UI trees, dropped taps and
non-deterministic failures.

Each consumer already processes its own messages one-at-a-time (a single worker
thread), but nothing stops the three SEPARATE processes from touching the device
at once. An in-process `threading.Lock` can't help across processes; an
`fcntl.flock` on a shared file can. It is host-local, which is exactly right since
a USB-attached phone is always on one host, and the OS releases it automatically if
the holding process dies — so a crash can never wedge the device permanently.

Wrap each full device flow in `with device_lock(serial): ...`. When the device is
busy the next consumer BLOCKS (waits its turn) rather than dropping work.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from typing import Callable, Iterator, Optional


def _lock_path(serial: Optional[str]) -> str:
    """Shared lock file for `serial` (one lock per device; default = a single lock)."""
    base = os.environ.get("TIKTOK_DEVICE_LOCK_DIR") or tempfile.gettempdir()
    return os.path.join(base, f"tiktok-agent-device-{serial or 'default'}.lock")


@contextmanager
def device_lock(
    serial: Optional[str] = None, *, log: Callable[[str], None] = print
) -> Iterator[None]:
    """
    Hold an exclusive cross-process lock on the device for the whole `with` body.

    Tries to acquire without blocking first so we only log "waiting" when actually
    contended, then falls back to a blocking acquire (the strict one-by-one queue).
    """
    path = _lock_path(serial)
    f = open(path, "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log(f"[device] busy — waiting for the phone (serial={serial or 'default'})…")
            fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()
