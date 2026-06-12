# Internals — Architecture & module flow

Maintainer-facing. For the publisher/MQTT contracts see the parent [`docs/`](../README.md).

## Layout: top-level CLIs + `core/` package

The console-script entry points (`agent.py`, `imagekit_source.py`,
`hivemq_source.py`, `tiktok_poster.py`, `comment_agent.py`, `comment_reader_agent.py`,
`watch_all.py`, `tiktok_profile.py`) and `tiktok_commenter.py` (a direct-run helper)
live at the **top level**. The shared, non-CLI library modules live in the **`core/`**
package: `core/mqtt_queue.py`, `core/tiktok_ui.py`, `core/imagekit_agent.py`,
`core/adb_pusher.py`, `core/comment_source.py`, `core/comment_read_source.py`,
`core/local_store.py`, `core/dedup_store.py`, `core/device_lock.py`,
`core/env_loader.py`, `core/logging_setup.py`, `core/es_log_handler.py`.

Top-level modules import them as `from core.x import …`; modules inside `core/`
import siblings with relative imports (`from .x import …`) and may still reach back
to top-level modules (e.g. `core/imagekit_agent.py` imports the top-level
`imagekit_source`/`tiktok_poster`).

The wheel `include` list in `pyproject.toml` must be updated by hand if a new
**top-level** `.py` module is added; the shared package is bundled wholesale via
`core/*.py`, so new modules **inside `core/`** are picked up automatically.

### `watch_all.py` (the `tiktok-watch-all` launcher)

A thin launcher (no logic of its own): it imports the three watch entrypoints
(`agent._watch_hivemq`, `comment_agent._watch`, `comment_reader_agent._watch`) and
runs each on a daemon thread so one `tiktok-watch-all` command drains all three
topics at once; the `core/device_lock.py` flock keeps their device flows serialized.

- `--retry` is a one-shot delegating to `agent._retry_posts` +
  `comment_agent._retry_comments` (reads are stateless, nothing to retry), honoring
  `--no-posts`/`--no-comments`.
- `--clear` is a one-shot delegating to `agent._clear_posts` +
  `comment_agent._clear_comments` (both wrapping `local_store.clear`) to delete the
  locally-spooled posts + comments **without** re-attempting — `--failed-only`
  restricts it to items marked `failed`, and `--no-posts`/`--no-comments` scope which
  spool is wiped.

The individual `tiktok-agent`/`tiktok-commenter` CLIs carry the same `--clear` /
`--failed-only` for a single spool.

## Module flow

The orchestrator is `agent.py::process_once`, which wires the other modules in
sequence. Each module is a single-responsibility seam with its own error type.

### Posting path (+ shared infra)

| Module | Role | Error type |
|--------|------|-----------|
| `agent.py` | Orchestrator (HiveMQ path + dispatch + CLI): receive/drain → download → push → (auto-post) → report status. `--watch` calls `_watch_hivemq` (event-driven); `--once`/`process_once` dispatch `_process_hivemq` (drain) or delegate to `imagekit_agent` on `--source imagekit` | — |
| `core/imagekit_agent.py` | Legacy `--source imagekit` orchestration (`process_imagekit`/`catch_up_imagekit`), split out of `agent.py`: dedups the ImageKit folder against the local `agent_state.json` | — |
| `hivemq_source.py` | Thin wiring for the **post** queue: post-specific config + `_parse`, re-exporting an `MqttWorkQueue` instance's API (`watch`/`list_pending`/`update_status`/`close`) as module functions | `HiveMQSourceError` (from `mqtt_queue`) |
| `core/mqtt_queue.py` | **Shared** durable MQTT machinery: `MqttWorkQueue` (persistent QoS-1 session, drain, event-driven `watch`, manual-ack) + `make_config`, parameterized by a `parse`/`key_of`/topic config. Used by all consumers | `HiveMQSourceError` |
| `imagekit_source.py` | `download()` (used by both sources) + list from ImageKit Media Management API | `ImageKitSourceError` |
| `core/adb_pusher.py` | `run_adb()` wrapper + push file to gallery + media scan | `PhonePushError` |
| `core/tiktok_ui.py` | **Shared** low-level adb/UI primitives (`dump_ui`, `find_tappable`, `tap`, `force_stop`, `wait_and_tap`, `input_line`, `screen_size`, …) used by the poster, commenter and profile flows | (raises `PhonePushError`) |
| `tiktok_poster.py` | Drive TikTok's UI over adb to post (post-specific flow/labels; primitives from `tiktok_ui`) | `TikTokPostError` |
| `tiktok_profile.py` | Read the active TikTok account and switch to a target `@handle` before acting (shared by poster + commenter; primitives from `tiktok_ui`) | `TikTokProfileError` |
| `core/local_store.py` | One-JSON-per-message spool dir (store on receive, delete on success); shared by both consumers | — |
| `core/dedup_store.py` | TTL-windowed "already done this" record (`seen`/`record`, 1-day default), keyed by post `id` / comment `content_key`; drops backend-republished duplicates. Shared by poster + commenter | — |
| `core/env_loader.py` | Zero-dep `.env` loader | — |
| `core/logging_setup.py` | `setup_logging()` (called once per CLI after `load_env()`): installs the console handler + the ES handler on the root logger; `bind_context`/`clear_context` attach per-flow correlation fields | — |
| `core/es_log_handler.py` | `ESLogHandler` — buffered, background-daemon, fail-safe `_bulk` shipper to Elasticsearch Serverless (stdlib `urllib`, `ApiKey` auth); `make_config()` lazy env factory | — |

### Comment-on-post (second, independent consumer)

Its own orchestrator + console script `tiktok-commenter`, its own HiveMQ
topic / client-id; no image handling, no AI.

| Module | Role | Error type |
|--------|------|-----------|
| `comment_agent.py` | Orchestrator + CLI: drain the comment topic → open post by URL → submit comment → report status. `--watch` / `--once` / `--catch-up` | — |
| `core/comment_source.py` | Thin wiring for the **comment** topic: comment-specific config + `_parse`, re-exporting its own `MqttWorkQueue` instance (own topic/client-id/status, `PostURL`+`Comment` parse) | `HiveMQSourceError` (from `mqtt_queue`) |
| `tiktok_commenter.py` | Drive TikTok's UI over adb: deep-link open a post → open comment sheet → type + submit one comment **or reply** (`reply_to`); also `read_comments` (scrape the sheet) + `parse_comment_rows`/`collect_comments` shared by the reader | `TikTokCommentError` |

### Comment-reader (third independent consumer — the read half of the reply loop)

| Module | Role | Error type |
|--------|------|-----------|
| `comment_reader_agent.py` | Orchestrator + CLI: drain `tiktok/comments-read` → `read_comments` → publish `{PostURL, comments, count, ts}` to `tiktok/comments-list`. `--watch`/`--once`/`--catch-up`. Read-only + idempotent: no local spool / `--retry` (a failed read is left unacked and redelivered) | — |
| `core/comment_read_source.py` | Thin wiring for the **read** topic: read-specific config + `_parse` (`PostURL`+optional `max`), own `MqttWorkQueue` (own topic/client-id, output topic = `comments-list`), re-exporting `list_pending`/`publish_comments`/`close`/`watch` | `HiveMQSourceError` (from `mqtt_queue`) |

## `run_adb()` — the single adb chokepoint

`run_adb()` in `adb_pusher.py` is the single chokepoint for **all** adb calls (push,
shell, intents, UI dumps, taps). The shared `tiktok_ui.py` primitives import it, and
the poster/commenter/profile flows build on `tiktok_ui` rather than calling `run_adb`
directly (except for a few feature-specific intents/keyevents). **Touch device
interaction in `tiktok_ui.py` / `adb_pusher.py`.**

## Shared vs. feature-specific

Two modules hold the reusable machinery the feature modules used to duplicate:
`tiktok_ui.py` (adb/UI primitives) and `mqtt_queue.py` (durable MQTT work-queue).
`hivemq_source.py` and `comment_source.py` are now thin: each builds an
`MqttWorkQueue` with its own config/parse and re-exports
`list_pending`/`update_status`/`close`/`watch`. The poster/commenter/profile keep only
their own screen labels and flow logic and import the primitives from `tiktok_ui`.
When TikTok's UI or the queue semantics change, fix it once in the shared module.

## One device, one flow at a time (`core/device_lock.py`)

The three consumers (`tiktok-agent`, `tiktok-commenter`, `tiktok-comment-reader`)
run as **separate OS processes** but drive the **one** attached phone — and
uiautomator **cannot run in parallel** on a single device (two overlapping
`uiautomator dump`/tap flows give stale UI trees, dropped taps, and spurious
`needs_manual`). Each process already handles its own messages one-at-a-time (a
single worker thread), but nothing in-process can coordinate across the three.

`core/device_lock.py::device_lock(serial)` is a cross-process **`fcntl.flock`**
(exclusive) held for the **whole device flow**. The three device-flow entrypoints —
`tiktok_poster.post`, `tiktok_commenter.comment_on_post`, `tiktok_commenter.read_comments`
— wrap their entire body in `with device_lock(serial):`, so every run mode
(`--watch`/`--once`/`--retry`/direct-run; `--catch-up` never touches the device) is
covered and the lock spans `ensure_account` + the post-success force-stop delays.
**Running all three watchers together is therefore safe.** When the device is busy a
consumer **blocks (waits its turn)** rather than dropping work — it logs `[device]
busy — waiting for the phone…` and proceeds once the holder finishes. The lock is
keyed by `serial` (one lock file per device; default → a single shared lock under
`$TMPDIR`, override the dir with `$TIKTOK_DEVICE_LOCK_DIR`) and is released
automatically by the OS if the holding process dies, so a crash can't wedge the
device. **Don't nest** these three flows within one process — a single `flock` per
flow would self-deadlock.
