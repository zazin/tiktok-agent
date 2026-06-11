"""Fail-safe, non-blocking Elasticsearch log handler (stdlib `urllib` only).

Ships every `logging` record to Elastic Cloud **Serverless** via the `_bulk`
endpoint (`create` action into a data stream, `ApiKey` auth). All work happens on
a background daemon thread fed by a bounded queue, so application/device flows are
never slowed or blocked, and any ES outage degrades gracefully to dropped records
(never an exception into app code).

Config is read lazily from the environment via `make_config()` (mirrors the
`make_config`/`_Config` pattern in `core/mqtt_queue.py`).

IMPORTANT: this module must NEVER log through the `logging` module — its own
diagnostics would re-enter `emit()` and recurse. All self-diagnostics go straight
to `sys.stderr`, guarded by a one-shot flag.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

DEFAULT_INDEX = "logs-tiktok_agent-default"
DEFAULT_BATCH_SIZE = 200
DEFAULT_FLUSH_INTERVAL = 5.0
DEFAULT_QUEUE_MAX = 10_000
DEFAULT_TIMEOUT = 10.0

_SENTINEL = object()  # close signal posted onto the queue


def _truthy(value: Optional[str], *, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default


class _Config:
    __slots__ = (
        "url",
        "api_key",
        "index",
        "batch_size",
        "flush_interval",
        "queue_max",
        "timeout",
    )

    def __init__(self) -> None:
        self.url = (os.getenv("ES_LOG_URL") or "").rstrip("/")
        self.api_key = os.getenv("ES_API_KEY") or ""
        self.index = os.getenv("ES_LOG_INDEX") or DEFAULT_INDEX
        self.batch_size = _int_env("ES_LOG_BATCH_SIZE", DEFAULT_BATCH_SIZE)
        self.flush_interval = _float_env("ES_LOG_FLUSH_INTERVAL", DEFAULT_FLUSH_INTERVAL)
        self.queue_max = _int_env("ES_LOG_QUEUE_MAX", DEFAULT_QUEUE_MAX)
        self.timeout = _float_env("ES_LOG_TIMEOUT", DEFAULT_TIMEOUT)


def make_config() -> Optional[_Config]:
    """Build an ES config from the environment, or return None to disable shipping.

    Shipping is ON by default; it is disabled when `ES_LOG_ENABLED` is falsy, or
    when the required `ES_LOG_URL`/`ES_API_KEY` are missing. A missing-creds case
    is logged once (to stderr) by the caller — it is never a blocker.
    """
    if not _truthy(os.getenv("ES_LOG_ENABLED"), default=True):
        return None
    cfg = _Config()
    if not cfg.url or not cfg.api_key:
        return None
    return cfg


class ESLogHandler(logging.Handler):
    """A `logging.Handler` that bulk-ships records to Elasticsearch off-thread."""

    def __init__(self, config: _Config, *, service: str) -> None:
        super().__init__()
        self._config = config
        self._service = service
        self._host = socket.gethostname()
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=config.queue_max)
        self._dropped = 0
        self._warned_once = False
        self._closed = False
        self._worker = threading.Thread(
            target=self._run, name="es-log-shipper", daemon=True
        )
        self._worker.start()

    # ---- logging.Handler API -------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        # Build the doc in the calling thread (cheap) so the worker only does I/O.
        # Must never raise into the app.
        try:
            doc = self._to_doc(record)
            self._queue.put_nowait(doc)
        except queue.Full:
            self._dropped += 1
        except Exception:  # pragma: no cover - defensive
            pass

    def flush(self) -> None:
        # The worker flushes on its own interval; explicit flushing happens via
        # close() (atexit). Nothing to force here without blocking the caller.
        pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:
            # Queue jammed; the daemon thread will be torn down at interpreter exit.
            pass
        self._worker.join(timeout=self._config.flush_interval + self._config.timeout)
        super().close()

    # ---- document shaping ----------------------------------------------------

    def _to_doc(self, record: logging.LogRecord) -> dict:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        doc = {
            "@timestamp": ts.isoformat().replace("+00:00", "Z"),
            "log.level": record.levelname.lower(),
            "log.logger": record.name,
            "message": record.getMessage(),
            "service.name": self._service,
            "host.name": self._host,
            "process.pid": record.process,
            "process.thread.name": record.threadName,
        }
        if record.exc_info:
            exc_type = record.exc_info[0]
            doc["error.type"] = getattr(exc_type, "__name__", str(exc_type))
            doc["error.message"] = str(record.exc_info[1])
            doc["error.stack_trace"] = self._format_exception(record.exc_info)
        labels = getattr(record, "es_labels", None)
        if isinstance(labels, dict) and labels:
            doc["labels"] = labels
        return doc

    @staticmethod
    def _format_exception(exc_info) -> str:
        return logging.Formatter().formatException(exc_info)

    # ---- background worker ---------------------------------------------------

    def _run(self) -> None:
        batch: list[dict] = []
        cfg = self._config
        while True:
            timeout = cfg.flush_interval if batch else None
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                # Flush interval elapsed with a partial batch.
                if batch:
                    self._flush(batch)
                    batch = []
                continue
            if item is _SENTINEL:
                # Drain anything still queued, then final-flush and stop.
                self._drain_into(batch)
                if batch:
                    self._flush(batch)
                return
            batch.append(item)  # type: ignore[arg-type]
            if len(batch) >= cfg.batch_size:
                self._flush(batch)
                batch = []

    def _drain_into(self, batch: list[dict]) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _SENTINEL:
                continue
            batch.append(item)  # type: ignore[arg-type]

    # ---- ES bulk ship --------------------------------------------------------

    def _flush(self, batch: list[dict]) -> None:
        if not batch:
            return
        action = json.dumps({"create": {"_index": self._config.index}})
        lines = []
        for doc in batch:
            lines.append(action)
            lines.append(json.dumps(doc, default=str))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        req = urllib.request.Request(
            f"{self._config.url}/_bulk",
            data=body,
            method="POST",
            headers={
                "Authorization": f"ApiKey {self._config.api_key}",
                "Content-Type": "application/x-ndjson",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._config.timeout) as r:
                resp = json.loads(r.read() or b"{}")
            if resp.get("errors"):
                self._warn(self._first_item_error(resp))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            self._warn(f"HTTP {e.code} from _bulk: {detail}")
        except (urllib.error.URLError, socket.timeout) as e:
            self._warn(f"network error shipping logs: {e}")
        except Exception as e:  # pragma: no cover - defensive backstop
            self._warn(f"unexpected error shipping logs: {e}")

    @staticmethod
    def _first_item_error(resp: dict) -> str:
        for item in resp.get("items", []):
            create = item.get("create", {})
            if create.get("status", 200) >= 400:
                return f"_bulk partial failure: {create.get('error')}"
        return "_bulk reported errors"

    def _warn(self, msg: str) -> None:
        """Emit one stderr warning per outage; never recurse through logging."""
        if self._warned_once:
            return
        self._warned_once = True
        print(f"[es-log] {msg} (further log-ship errors suppressed)", file=sys.stderr, flush=True)
