# TikTok Agent

The device-side half of the TikTok system. It runs on a **computer with an Android phone connected via adb**, drains the **HiveMQ work topic** that [tiktok-pipeline](https://github.com/zazin/tiktok-pipeline) publishes to, and for each queued message: downloads the image from its public ImageKit URL, pushes it into the phone's gallery, **auto-posts it to TikTok** (caption + description straight from the message) by driving the on-device UI over adb, then reports the outcome (`posted`/`failed`) to the status topic and acks the message.

Auto-post is **on by default** — run `tiktok-agent --watch` and it posts new messages hands-free. `--watch` is **event-driven**: it holds a persistent MQTT subscription and reacts to each message the instant it's published (no poll interval). **HiveMQ is the queue / source of truth** — there is no database or table in between; the agent discovers work by draining the topic and reports back by publishing to the status topic. A **persistent QoS-1 session** means messages published while the device is offline are queued by the broker and delivered on reconnect; a message is **acked only after it posts**, so anything unposted is redelivered.

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

> **Publishing from another backend?** See [`docs/`](docs/) for the full MQTT
> contract reference — connection/auth, both work topics (`tiktok/posts` and
> `tiktok/comments`), exact field schemas, status messages, and QoS/ack semantics.

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Orchestrator — poll → download → push → auto-post → report status (`--source`, `--catch-up`) |
| `hivemq_source.py` | Drain the work topic + publish/ack outcomes over MQTT (paho, also the `tiktok-hivemq` CLI) |
| `imagekit_source.py` | `download()` images (used by both sources) + list the ImageKit folder (legacy queue) |
| `adb_pusher.py` | Push an image to the phone gallery over adb (+ media scan) |
| `tiktok_poster.py` | Best-effort auto-post via adb UI automation (also the `tiktok-post` CLI) |
| `comment_agent.py` | Comment-on-post orchestrator + `tiktok-commenter` CLI (independent of posting) |
| `comment_source.py` | Drain the comment topic + publish/ack comment outcomes over MQTT (mirror of `hivemq_source.py`) |
| `tiktok_commenter.py` | adb UI automation to open a post by URL and submit a comment |
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
uv run tiktok-agent --watch           # then: event-driven, auto-posts each NEW message the instant it arrives

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
| `failed` | agent | Posting failed, or auto-post stopped on an unrecognized screen (`needs_manual`); TikTok is force-stopped (not left open) and the item stays spooled for `--retry`; the work message is acked (dropped). |

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
2. **Phase 2:** dumps the UI tree and taps Foto → Next → Post by label, typing the caption first. **Brittle** (breaks when TikTok changes its UI) and potentially against TikTok's Terms of Service. On any screen it doesn't recognize it stops (`needs_manual` → the message is marked `failed`). It **force-stops the TikTok app** on the way out — on success after waiting a few seconds for the upload to finish, and immediately on any error (`needs_manual`/`wrong_account`) so the app is never left half-open; the item stays spooled for `--retry`. Only `--no-auto-post` (which just opens the composer) leaves TikTok open, by design.

Use `--no-auto-post` (agent) or omit `--auto-post` (tiktok-post) to only open the composer / push to the gallery and finish manually. Button labels and the package name are constants at the top of `tiktok_poster.py` — tune them when TikTok's UI shifts.

Use `--auto-post` at your own risk.

## Comment on a post (`tiktok-commenter`)

A separate, independent capability: **leave a comment on an existing TikTok post**.
It is triggered by HiveMQ just like the poster, but on its **own topic**
(`tiktok/comments`) drained by its **own persistent session**, so it runs as a
separate process alongside `tiktok-agent`. There is **no AI** — the exact comment
text comes in the message.

Each message is QoS-1 JSON with just two fields (no `id`):

```json
{"PostURL": "https://www.tiktok.com/@captgani/video/7648864421841816852",
 "Comment": "Nice video!"}
```

For each message the agent opens the post by URL over adb, opens the comment sheet,
types the comment, and submits it — then reports `{PostURL, status, ts}` on
`tiktok/comment-status` and acks the message.

```bash
# .env (reuses the HiveMQ creds; all three optional with the defaults shown):
#   HIVEMQ_COMMENT_TOPIC=tiktok/comments
#   HIVEMQ_COMMENT_STATUS_TOPIC=tiktok/comment-status
#   HIVEMQ_COMMENT_CLIENT_ID=tiktok-commenter

uv run tiktok-commenter --catch-up          # first: drain the existing backlog WITHOUT commenting (skip it)
uv run tiktok-commenter --watch             # then: event-driven, comments on each NEW message instantly
uv run tiktok-commenter --once              # drain the current backlog once and exit
uv run tiktok-commenter --once --dry-run    # open posts + log the comment, but DON'T submit (use while tuning)
```

Statuses (all drop the message so it won't loop): `commented`, `needs_manual` (an
unrecognized screen — it stops rather than tap blindly), `skipped_non_ascii`
(nothing typeable after stripping emoji/non-ASCII — `adb input text` can't enter
those), `wrong_account`, `failed`. TikTok is force-stopped on the way out — after a
short delay on success, and immediately on any error — so it's never left open; the
item stays spooled for `--retry` (except `--dry-run`, which leaves it open for
inspection).

**Same brittleness caveat as auto-post:** opening the post by deep-link is reliable,
but finding the comment icon → input → send depends on TikTok's current UI. Those
labels are constants at the top of `tiktok_commenter.py`
(`COMMENT_OPEN_SUBSTRINGS`, `COMMENT_INPUT_HINTS`, `COMMENT_SEND_LABELS`) and must
be calibrated on-device against a real `uiautomator dump` — run `--dry-run` first
and tune. Use at your own risk (UI automation may be against TikTok's ToS).
