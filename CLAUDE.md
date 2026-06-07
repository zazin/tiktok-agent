# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The device-side half of a two-repo TikTok system. The **server** half lives in a
separate repo, [tiktok-pipeline](https://github.com/zazin/tiktok-pipeline): it
generates images, uploads them to ImageKit, and **publishes one MQTT message per
post** to a **HiveMQ work topic** (caption, description, and the public ImageKit
`ImageURL`, plus a correlation `id`). **This repo** runs on a computer with an
Android phone attached via adb, drains that topic, and for each queued message:
downloads the image from its `ImageURL` → pushes it into the phone gallery →
auto-posts it to TikTok by driving the on-device UI over adb → publishes the
outcome (`posted`/`failed`) to a status topic and **acks** the message.

**HiveMQ is the queue / source of truth.** There is no server, database, or
table between the two halves — the agent discovers work by draining the
work topic and reports back by publishing to the status topic. Queue durability
comes from a **persistent QoS-1 MQTT session** (stable client-id,
`clean_session=False`): the broker queues messages while the device is offline and
redelivers them on reconnect, and a message is **acked only after it posts**, so
anything unposted (failed, `--no-auto-post`, or a crash) is redelivered. The two
repos are coupled by the **message contract** (see Cross-repo coupling below):
JSON fields `id`, `Caption`, `Description`, `ImageURL`, `ImagePath`, `CreatedAt`.

The legacy **ImageKit folder queue** is still available via `--source imagekit`;
it dedups `fileId`s against the local `agent_state.json` and reads caption/
description from ImageKit `customMetadata`. ImageKit images are downloaded the
same way in both modes (public CDN URL, no auth).

## Commands

Managed with [uv](https://docs.astral.sh/uv/). One third-party dependency,
`paho-mqtt` (for HiveMQ/MQTT — stdlib has no MQTT client); everything else is
stdlib.

```bash
uv sync

# Console scripts (defined in pyproject.toml [project.scripts]):
uv run tiktok-agent --catch-up     # drain current backlog WITHOUT posting (run once first)
uv run tiktok-agent --watch        # poll loop, auto-posts each NEW message (default interval 60s)
uv run tiktok-agent --once         # single poll cycle
uv run tiktok-agent --watch --no-auto-post   # push to phone only, leave messages unacked
uv run tiktok-agent --once --source imagekit # legacy: poll the ImageKit folder instead
uv run tiktok-hivemq                         # inspect the HiveMQ queue (peek the backlog, no ack)
uv run tiktok-source --folder /tiktok        # inspect the legacy ImageKit queue
uv run tiktok-post --list                    # list images already on the phone
uv run tiktok-post /sdcard/Pictures/x.jpg --auto-post --from-imagekit   # post an on-phone image
```

There are **no tests, linter, or build step** configured. The wheel `include`
list in `pyproject.toml` must be updated by hand if a new top-level `.py` module
is added.

### Credentials

`.env` (gitignored, auto-loaded by every CLI via `env_loader.load_env()` at
startup) needs:
```
# HiveMQ source (default) — MQTT over TLS, username/password auth
HIVEMQ_HOST=xxxx.s1.eu.hivemq.cloud   # cluster host (required)
HIVEMQ_USERNAME=...                   # required
HIVEMQ_PASSWORD=...                   # required
HIVEMQ_PORT=8883                      # optional, defaults to 8883 (TLS)
HIVEMQ_TOPIC=tiktok/posts             # optional, work topic to drain
HIVEMQ_STATUS_TOPIC=tiktok/status     # optional, where outcomes are published
HIVEMQ_CLIENT_ID=tiktok-agent         # optional, stable id → persistent session

# ImageKit — still needed to download images (and for --source imagekit)
IMAGEKIT_PRIVATE_KEY=private_...
IMAGEKIT_URL_ENDPOINT=https://ik.imagekit.io/your_id
```
Real environment variables always win over `.env`. HiveMQ auth is TLS +
username/password (port 8883); ImageKit auth is HTTP Basic with the private key as
username and an empty password. (Image downloads hit the public ImageKit CDN URL
and need no auth.)

The `HIVEMQ_CLIENT_ID` must be **stable** — it keys the persistent session that
holds the offline backlog. Running `tiktok-hivemq` (inspect) reuses the same
client-id, so doing so while the agent watches briefly bumps the agent off the
broker until its next reconnect.

### Runtime prerequisites (not Python)

- `adb` on PATH (`brew install android-platform-tools`)
- Phone connected, USB debugging authorized, **unlocked**, TikTok installed + logged in

## Architecture / module flow

The orchestrator is `agent.py::process_once`, which wires the other modules in
sequence. Each module is a single-responsibility seam with its own error type:

| Module | Role | Error type |
|--------|------|-----------|
| `agent.py` | Orchestrator: poll → download → push → (auto-post) → report status. `process_once` dispatches to `_process_hivemq` (default) or `_process_imagekit` on `--source` | — |
| `hivemq_source.py` | `list_pending()` (drain) + `update_status()` (publish + ack) + `close()` over MQTT (paho, persistent QoS-1 session) | `HiveMQSourceError` |
| `imagekit_source.py` | `download()` (used by both sources) + list from ImageKit Media Management API | `ImageKitSourceError` |
| `adb_pusher.py` | `run_adb()` wrapper + push file to gallery + media scan | `PhonePushError` |
| `tiktok_poster.py` | Drive TikTok's UI over adb to post | `TikTokPostError` |
| `env_loader.py` | Zero-dep `.env` loader | — |

`run_adb()` in `adb_pusher.py` is the single chokepoint for **all** adb calls
(push, shell, intents, UI dumps, taps) — `tiktok_poster.py` imports it rather than
shelling out itself. Touch device interaction there.

### State machine (dedup)

**HiveMQ source (default):** the broker's persistent session is the dedup. A poll
cycle = connect → `list_pending()` drains every queued QoS-1 message (publish
order, oldest first) → process → ack the done ones → `close()` disconnects. The
client uses `manual_ack=True`, so paho sends the PUBACK only when the agent calls
`update_status()`. After acting on a message the agent calls `update_status()`:
- `post()` returns `"posted"` → publish `posted` + **ack** (message dropped)
- `post()` returns `"needs_manual"`, raises, or `ImageURL` is empty → publish
  `failed` + **ack** (`failed` drops the message just like a "posted" ack so it
  won't loop; on `needs_manual` the composer is still open on the phone to finish
  by hand)
- `--no-auto-post` → message is **never acked** (pushed to phone only); `close()`
  releases it back to the broker, so it is redelivered on the next poll and
  re-pushed until it is actually posted

There is **no local state file** in this mode (the broker holds the queue).
Trade-off: a crash between a successful post and the ack leaves the message
unacked, so it is redelivered and could be re-posted on the next poll.

The drain ends after a short idle window (`DRAIN_IDLE`, no new message for ~2s)
capped by `DRAIN_HARD_CAP`. The persistent client is a module-level singleton in
`hivemq_source.py`; `_process_hivemq` and `_catch_up_hivemq` wrap their work in
`try/finally: close()` so each poll cycle is a clean connect/disconnect.

`--catch-up` drains every queued message and marks it `posted` (publish + ack)
without posting. **Run it once before the first `--watch`** — auto-post is ON by
default, so otherwise the first poll posts the whole backlog.

**ImageKit source (`--source imagekit`, legacy):** `agent_state.json` (gitignored)
is `{"processed": {fileId: {name, status, ts, ...}}}`. An image is processed
exactly once: anything whose `fileId` is already a key is skipped. State is saved
**after each item** so a crash mid-batch doesn't reprocess completed work.
`status` values: `posted`, `needs_manual`, `pushed`, `failed`, `catch-up`. New
images are processed **oldest-first** (`reversed(list_images())`, which returns
newest-first). Here `--catch-up` writes `status: "catch-up"` entries for the
folder.

### Auto-post: two phases (the brittle part)

`tiktok_poster.py::post` is deliberately split because UI automation is fragile and
arguably against TikTok's ToS:

- **Phase 1 (`open_in_tiktok`, always runs, reliable):** fires an
  `ACTION_SEND` intent to open TikTok's composer with the image attached. Requires
  resolving the `/sdcard/...` path to a MediaStore `content://` URI
  (`_resolve_content_uri`) — file:// URIs are blocked by scoped storage. The lookup
  matches on `_display_name` (filename), **not** `_data` (full path), because `_data`
  WHERE clauses return nothing on scoped-storage/MIUI devices. If `auto_post=False`,
  returns `"composer_open"` and stops here.
- **Phase 2 (opt-in `auto_post=True`, brittle):** walks `POST_FLOW_STEPS`, an
  **ordered** list of per-screen button labels (English + Indonesian). For each
  screen it dumps the UI tree (`uiautomator dump`), finds a node whose
  text/content-desc matches a label, and taps its center. The order matters so an
  ambiguous label on a later screen can't be tapped early. On any **unrecognized**
  screen it stops and returns `"needs_manual"`, leaving the composer open rather
  than tapping blindly.

**When TikTok's UI changes, the constants at the top of `tiktok_poster.py` are the
tuning knobs:** `TIKTOK_PACKAGES`, `POST_FLOW_STEPS`, `CAPTION_HINTS`,
`STEP_DELAY`, `STEP_RETRIES`.

### Caption handling

TikTok has a single text field. `build_post_text` combines the `Caption` (hook)
and `Description` message fields (or ImageKit `customMetadata.caption`/
`description` in legacy mode) into one string — caption first, description on the
next line. Typing uses `adb input text`, which **cannot enter emoji/non-ASCII**:
`_input_line` strips non-ASCII and quote chars, maps spaces to `%s`; newlines
become `KEYCODE_ENTER`. The full emoji caption still lives in the published
message. (Note: the pipeline does **not** store hashtags — captions/descriptions
are hashtag-free.)

## Cross-repo coupling (easy to break)

- **MQTT message contract (primary):** the pipeline publishes (QoS 1, retained
  false) and the agent drains JSON messages on the work topic with fields `id`,
  `Caption`, `Description`, `ImageURL`, `ImagePath`, `CreatedAt`. `id` is the
  required correlation key echoed back in status messages
  (`{id, status, ts}` on the status topic). Both sides must agree on the topic
  names (`HIVEMQ_TOPIC` / `HIVEMQ_STATUS_TOPIC`) and the QoS-1/persistent-session
  semantics. Renaming a field, changing the topics, or dropping to QoS 0 silently
  breaks the agent.
- **ImageKit `ImageURL`:** the agent downloads the image from the public CDN URL the
  pipeline put in each message.
- **Legacy ImageKit coupling (`--source imagekit` only):** filename convention
  (`tiktok_<ts>.<ext>` on phone vs `tiktok_<ts>_<unique>.<ext>` on ImageKit, matched
  by `caption_from_imagekit`), `customMetadata` keys `caption`/`description`, and the
  adb/Basic auth scheme mirroring the pipeline's uploader.

Changing any of these in one repo silently breaks the other.
