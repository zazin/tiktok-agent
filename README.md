# TikTok Agent

The device-side half of the TikTok system. It runs on a **computer with an Android phone connected via adb**, drains the **HiveMQ work topic** that [tiktok-pipeline](https://github.com/zazin/tiktok-pipeline) publishes to, and for each queued message: downloads the image from its public ImageKit URL, pushes it into the phone's gallery, **auto-posts it to TikTok** (caption + description straight from the message) by driving the on-device UI over adb, then reports the outcome (`posted`/`failed`) to the status topic and acks the message.

Auto-post is **on by default** — run `tiktok-agent --watch` and it posts new messages hands-free. **HiveMQ is the queue / source of truth** — there is no database or table in between; the agent discovers work by draining the topic and reports back by publishing to the status topic. A **persistent QoS-1 session** means messages published while the device is offline are queued by the broker and delivered on reconnect; a message is **acked only after it posts**, so anything unposted is redelivered.

```
tiktok-pipeline (server)          HiveMQ (MQTT)             tiktok-agent (this, computer + adb + phone)
  generate → upload to ImageKit     work topic         ◄──────  drain queued messages (persistent QoS-1, oldest first)
  → publish message  ─────────────► (Caption, Desc,            → download image from msg's ImageURL (ImageKit CDN)
    {id, ImageURL, ...}              ImageURL, id)              → adb push to gallery
                                    status topic       ──────►  → auto-post to TikTok (caption + description)
                                     {id, status}              → publish status + ack the message
```

The legacy **ImageKit-folder queue** is still available with `--source imagekit` (dedups `fileId`s against a local JSON state file and reads caption/description from ImageKit `customMetadata`).

### Message contract

The pipeline publishes one **QoS-1, retained-false** JSON message per post to the work topic:

```json
{"id": "uuid-or-post-id", "Caption": "...", "Description": "...",
 "ImageURL": "https://ik.imagekit.io/.../x.jpg", "ImagePath": "x.jpg",
 "CreatedAt": "2026-06-07T12:00:00Z"}
```

`id` is the required correlation key. The agent reports back on the status topic with `{"id": "...", "status": "posted"|"failed", "ts": <unix>}`.

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Orchestrator — poll → download → push → auto-post → report status (`--source`, `--catch-up`) |
| `hivemq_source.py` | Drain the work topic + publish/ack outcomes over MQTT (paho, also the `tiktok-hivemq` CLI) |
| `imagekit_source.py` | `download()` images (used by both sources) + list the ImageKit folder (legacy queue) |
| `adb_pusher.py` | Push an image to the phone gallery over adb (+ media scan) |
| `tiktok_poster.py` | Best-effort auto-post via adb UI automation (also the `tiktok-post` CLI) |
| `env_loader.py` | Zero-dependency `.env` loader |
| `agent_state.json` | Local record of processed `fileId`s — **`--source imagekit` only**, gitignored |

## Quick Start

Managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync

# .env (auto-loaded):
#   # HiveMQ source (default) — broker creds are required
#   HIVEMQ_HOST=xxxx.s1.eu.hivemq.cloud
#   HIVEMQ_USERNAME=...
#   HIVEMQ_PASSWORD=...
#   HIVEMQ_PORT=8883             # optional, defaults to 8883 (TLS)
#   HIVEMQ_TOPIC=tiktok/posts    # optional, work topic to drain
#   HIVEMQ_STATUS_TOPIC=tiktok/status   # optional, where outcomes are published
#   HIVEMQ_CLIENT_ID=tiktok-agent       # optional, stable id → persistent session
#   # ImageKit — still needed to download images (and for --source imagekit)
#   IMAGEKIT_PRIVATE_KEY=private_...
#   IMAGEKIT_URL_ENDPOINT=https://ik.imagekit.io/your_id

# Always-on: watch the HiveMQ queue and auto-post every new message, hands-free
uv run tiktok-agent --catch-up        # first: drain the existing backlog as posted (skip it)
uv run tiktok-agent --watch           # then: auto-posts each NEW message as it arrives

# One pass (also auto-posts queued messages by default)
uv run tiktok-agent --once

# Push to the phone only, leave messages pending (unacked → redelivered)
uv run tiktok-agent --watch --no-auto-post

# Inspect the HiveMQ queue (peek the backlog, no ack)
uv run tiktok-hivemq

# Legacy: use the ImageKit folder as the queue instead
uv run tiktok-agent --once --source imagekit --folder /tiktok
uv run tiktok-source --folder /tiktok          # inspect the ImageKit folder

# Post an image that is ALREADY on the phone (no download)
uv run tiktok-post --list                                   # see gallery images
uv run tiktok-post /sdcard/Pictures/foo.jpg                 # open composer (Phase 1)
uv run tiktok-post /sdcard/Pictures/foo.jpg --auto-post --from-imagekit   # caption+desc from ImageKit
uv run tiktok-post /sdcard/Pictures/foo.jpg --auto-post --caption "..." --description "..."
```

**Auto-post defaults to ON.** `tiktok-agent --once`/`--watch` will post; use `--no-auto-post`
for push-only. Because it posts every queued message, run `tiktok-agent --catch-up` once first
to clear the current backlog (drains and marks them `posted` without posting) — otherwise the
first run posts all of it.

**Caption + description** come straight from the message's `Caption` and `Description`
fields, combined into TikTok's single text field (caption first, description on the next line).
In `--source imagekit` mode they're read from ImageKit `customMetadata` instead; `tiktok-post`
can do the same lookup with `--from-imagekit`, or you can pass `--caption`/`--description`.

Typing uses `adb input text`, which can't enter emoji — emoji are stripped; the full emoji
caption still lives in the published message. (The pipeline stores no hashtags.)

### Status workflow

The agent publishes one of two outcomes to the status topic, owned jointly by both repos:

| Value | Set by | Meaning |
|-------|--------|---------|
| (queued) | pipeline | Freshly published to the work topic, ready to post. The agent drains these. |
| `posted` | agent | Successfully posted to TikTok; the work message is acked. |
| `failed` | agent | Posting failed, or auto-post stopped on an unrecognized screen (`needs_manual` — the composer is left open on the phone to finish by hand); the work message is acked (dropped). |

There is **no local state file** in HiveMQ mode — the broker's persistent session is the queue.
Trade-off: a crash between a successful post and the ack leaves the message unacked, so it is
redelivered and could be re-posted on the next poll.

## Requirements

- [uv](https://docs.astral.sh/uv/), Python 3.10+ (one dependency: `paho-mqtt` for HiveMQ; everything else is stdlib)
- `adb` (`brew install android-platform-tools`)
- An Android phone connected with **USB debugging** authorized, **unlocked**, with **TikTok installed and logged in**
- HiveMQ broker credentials (host + username/password) and ImageKit access to the public image URLs

## Auto-post caveat

Auto-post is **on by default** and **publishes real public posts** to the logged-in TikTok account. `tiktok_poster.py` drives TikTok's UI over adb in two phases:

1. **Phase 1:** a `SEND` intent opens TikTok's composer with the image attached. Reliable.
2. **Phase 2:** dumps the UI tree and taps Foto → Next → Post by label, typing the caption first. **Brittle** (breaks when TikTok changes its UI) and potentially against TikTok's Terms of Service. On any screen it doesn't recognize it stops and leaves the composer open (`needs_manual` → the message is marked `failed`).

Use `--no-auto-post` (agent) or omit `--auto-post` (tiktok-post) to only open the composer / push to the gallery and finish manually. Button labels and the package name are constants at the top of `tiktok_poster.py` — tune them when TikTok's UI shifts.

Use `--auto-post` at your own risk.
