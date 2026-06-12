# Internals — Logging (centralized → Elasticsearch Serverless)

Maintainer-facing. For the publisher/MQTT contracts see the parent [`docs/`](../README.md).

All logging flows through Python's stdlib `logging` module, configured **once per
process** by `core/logging_setup.py::setup_logging(service)`, which every CLI calls on
the line right after `load_env()`. It installs two handlers on the **root** logger — so
every module's records (including `core/*`) are captured with no per-call-site wiring
beyond `logger = logging.getLogger(__name__)`:

- **Console** `StreamHandler` → **stderr**, format `"%(levelname)-5s %(message)s"`
  (e.g. `INFO  …`, `WARN  …`, `ERROR  …`; `WARNING` is renamed to `WARN` for a 5-char
  column). On stderr so the `--json`/listing **stdout** of the peek tools
  (`tiktok-hivemq --json`, `tiktok-source`, `tiktok-post --list`) stays clean.
- **`ESLogHandler`** (`core/es_log_handler.py`) → **Elasticsearch Serverless**,
  **enabled by default**. Buffered + shipped from a **background daemon thread** (bounded
  `queue.Queue`), bulk-`POST`ed to `{ES_LOG_URL}/_bulk` as NDJSON with the `create`
  action (required for data streams) and `Authorization: ApiKey <key>`, using only stdlib
  `urllib` (no new dependency). It is **non-blocking** (device/UI flows are never slowed)
  and **fail-safe**: an ES outage or missing creds drops/degrades to console-only with
  **one** stderr warning and **never raises into app code**. Flush on size
  (`ES_LOG_BATCH_SIZE`), interval (`ES_LOG_FLUSH_INTERVAL`), or process exit (`atexit` →
  `close()`). The handler never logs through `logging` itself (recursion guard —
  self-diagnostics go straight to stderr).

**Config** is read lazily from the env (`make_config()`, like `mqtt_queue`): enable flag
`ES_LOG_ENABLED` (default on; `0`/`false` disables), `ES_LOG_URL` + `ES_API_KEY`
(required to actually ship), `ES_LOG_INDEX` (data-stream, default
`logs-tiktok_agent-default`), `ES_LOG_LEVEL`, and the batch/flush/queue/timeout knobs.

**Document shape** (ECS-ish): `@timestamp`, `log.level`, `log.logger`, `message`,
`service.name` (the console-script name passed to `setup_logging`), `host.name`,
`process.pid`, `process.thread.name`, plus `error.type`/`error.message`/
`error.stack_trace` when a record carries exception info, and a `labels` object.
`labels` comes from an ambient `contextvars` context (`bind_context()`/`clear_context()`)
merged with any per-call `extra={"es_labels": {...}}`.

**Full HiveMQ payloads are stored verbatim in ES.** `core/mqtt_queue.py` logs both
**received** messages (`_log_received`) and **published** status/result messages
(`update_status`/`publish_result`) with the console line truncated but the **entire
payload** attached as `labels["mqtt.payload"]` (plus `mqtt.topic`, `mqtt.qos`,
`mqtt.direction`) — so every inbound and outbound MQTT body is queryable in ES.

When adding a module, just use `logging.getLogger(__name__)`; don't reconfigure the root
logger or add handlers (the `setup_logging` idempotency guard already protects the
`tiktok-watch-all` process, which drives three agents in one process).
