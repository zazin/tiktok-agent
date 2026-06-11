"""Centralized logging setup, called once per CLI right after `load_env()`.

Configures the process-global root logger with two handlers:
  * a console `StreamHandler` (stderr) with the `LEVEL  message` format, so the
    terminal output operators watch is consistent and greppable, and stdout stays
    clean for the `--json` machine output some CLIs emit; and
  * a buffered, fail-safe `ESLogHandler` that ships every record to Elasticsearch
    Serverless (enabled by default; degrades to console-only when unconfigured or
    disabled via `ES_LOG_ENABLED`).

Because the root logger is process-global, configuring it once captures every
module's records (including the `core/*` libraries) — call sites just use
`logging.getLogger(__name__)`.

`bind_context()/clear_context()` attach ambient correlation fields (post `id`,
`PostURL`, device serial, outcome) to every record emitted within a flow, via a
`contextvars.ContextVar` + a `logging.Filter`, with no churn at deep call sites.
"""

from __future__ import annotations

import atexit
import contextvars
import logging
import os
import sys
import threading
from typing import Optional

from core import es_log_handler

_INITIALIZED = False
_LOCK = threading.Lock()

# Ambient per-flow context copied onto every LogRecord as `record.es_labels`.
_CONTEXT: "contextvars.ContextVar[dict]" = contextvars.ContextVar("es_log_context", default={})


class _ContextFilter(logging.Filter):
    """Copy the current ambient context dict onto each record as `es_labels`."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _CONTEXT.get()
        existing = getattr(record, "es_labels", None)
        if ctx or existing:
            merged = dict(ctx)
            if isinstance(existing, dict):
                merged.update(existing)
            record.es_labels = merged
        return True


def _derive_service() -> str:
    base = os.path.basename(sys.argv[0] or "")
    if not base or base in ("-c", "python", "python3") or base.endswith(".py"):
        return "tiktok-agent"
    return base


def setup_logging(service: Optional[str] = None, *, level: "int | str" = "INFO") -> None:
    """Install console + (optional) Elasticsearch handlers on the root logger.

    Idempotent: safe to call from every CLI and from `watch_all` (which drives
    three agents in one process) — only the first call configures handlers.
    Never raises into app code; ES wiring failures fall back to console-only.
    """
    global _INITIALIZED
    with _LOCK:
        if _INITIALIZED:
            return
        _INITIALIZED = True

        # Shorten WARNING → WARN so the level column stays 5 chars wide.
        logging.addLevelName(logging.WARNING, "WARN")

        svc = service or _derive_service()
        root = logging.getLogger()
        root.setLevel(level)

        handlers: list[logging.Handler] = []

        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)-5s %(message)s"))
        handlers.append(console)

        try:
            cfg = es_log_handler.make_config()
            if cfg is not None:
                es = es_log_handler.ESLogHandler(cfg, service=svc)
                es.setLevel(os.getenv("ES_LOG_LEVEL") or "INFO")
                handlers.append(es)
                atexit.register(es.close)
            elif es_log_handler._truthy(os.getenv("ES_LOG_ENABLED"), default=True):
                # Enabled but missing creds — warn once, run console-only. Not a blocker.
                print(
                    "[es-log] ES_LOG_URL/ES_API_KEY not set; logging to console only.",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as e:  # pragma: no cover - defensive
            print(f"[es-log] failed to init ES handler: {e}", file=sys.stderr, flush=True)

        ctx_filter = _ContextFilter()
        for h in handlers:
            h.addFilter(ctx_filter)

        root.handlers = handlers

        # paho-mqtt logs through the logging module; keep it quiet.
        logging.getLogger("paho").setLevel(logging.WARNING)


def bind_context(**fields) -> None:
    """Merge correlation fields into the ambient context for this flow/thread.

    Every subsequent log record (until clear_context()) carries them as `labels`
    in Elasticsearch. ContextVars are per-thread/per-context, so the three watcher
    threads and per-queue worker threads each keep their own context.
    """
    ctx = dict(_CONTEXT.get())
    ctx.update({k: v for k, v in fields.items() if v is not None})
    _CONTEXT.set(ctx)


def clear_context() -> None:
    _CONTEXT.set({})
