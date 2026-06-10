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

# Watch ALL features from one command (posts + comments + comment-reads in one process):
uv run tiktok-watch-all --catch-up   # drain EVERY backlog WITHOUT acting (run once first)
uv run tiktok-watch-all              # event-driven: watch all three topics at once (Ctrl-C stops all)
uv run tiktok-watch-all --no-reads   # skip a feature (also --no-posts / --no-comments)
uv run tiktok-watch-all --retry      # one-shot: re-attempt spooled posts + comments (no HiveMQ), then exit

uv run tiktok-agent --catch-up     # drain current backlog WITHOUT posting (run once first)
uv run tiktok-agent --watch        # event-driven: stays subscribed, auto-posts each message instantly
uv run tiktok-agent --once         # drain the current backlog once and exit
uv run tiktok-agent --watch --no-auto-post   # push to phone only, leave messages unacked
uv run tiktok-agent --retry                  # re-attempt posts still in queue_posts/ (no HiveMQ)
uv run tiktok-agent --once --source imagekit # legacy: poll the ImageKit folder instead
uv run tiktok-hivemq                         # inspect the HiveMQ queue (peek the backlog, no ack)
uv run tiktok-source --folder /tiktok        # inspect the legacy ImageKit queue
uv run tiktok-post --list                    # list images already on the phone
uv run tiktok-post /sdcard/Pictures/x.jpg --auto-post --from-imagekit   # post an on-phone image

# Comment-on-post (independent of the posting agent; see "Comment-on-post" below):
uv run tiktok-commenter --catch-up                 # drain the comment backlog WITHOUT commenting (run once first)
uv run tiktok-commenter --watch                    # event-driven: comment on each tiktok/comments message instantly
uv run tiktok-commenter --once --dry-run           # drain once, log what it would comment, DON'T submit
uv run tiktok-commenter --retry                    # re-attempt comments still in queue_comments/ (no HiveMQ)

# Comment-reader (read a post's comments back to the backend; see "Comment-reader" below):
uv run tiktok-comment-reader --catch-up            # drain the read-job backlog WITHOUT reading (run once first)
uv run tiktok-comment-reader --watch               # event-driven: scrape each tiktok/comments-read post instantly
uv run tiktok-comment-reader --once --max 20       # drain once, scrape up to 20 comments/post, publish the lists
```

`tiktok_commenter.py` is also a direct-run helper for a single post (no MQTT):
```bash
uv run python tiktok_commenter.py <url> "<comment>"                                   # top-level comment
uv run python tiktok_commenter.py <url> "<reply>" --reply-to-author @who --reply-to-text "..."  # reply to a comment
```

There are **no tests, linter, or build step** configured. The wheel `include`
list in `pyproject.toml` must be updated by hand if a new top-level `.py` module
is added (the shared library package is bundled wholesale via `core/*.py`, so new
modules **inside `core/`** are picked up automatically).

### Layout: top-level CLIs + `core/` package

The console-script entry points (`agent.py`, `imagekit_source.py`,
`hivemq_source.py`, `tiktok_poster.py`, `comment_agent.py`, `comment_reader_agent.py`,
`watch_all.py`, `tiktok_profile.py`) and `tiktok_commenter.py` (a direct-run helper)
live at the **top level**. `watch_all.py` is a thin launcher (no logic of its own):
it imports the three watch entrypoints (`agent._watch_hivemq`,
`comment_agent._watch`, `comment_reader_agent._watch`) and runs each on a daemon
thread so one `tiktok-watch-all` command drains all three topics at once; the
`core/device_lock.py` flock keeps their device flows serialized. Its `--retry` is a
one-shot that delegates to `agent._retry_posts` + `comment_agent._retry_comments`
(reads are stateless, nothing to retry), honoring `--no-posts`/`--no-comments`. The shared, non-CLI library modules live in the **`core/`** package:
`core/mqtt_queue.py`, `core/tiktok_ui.py`, `core/imagekit_agent.py`,
`core/adb_pusher.py`, `core/comment_source.py`, `core/comment_read_source.py`,
`core/local_store.py`, `core/device_lock.py`, `core/env_loader.py`. Top-level modules import them as `from core.x import …`;
modules inside `core/` import siblings with relative imports (`from .x import …`)
and may still reach back to top-level modules (e.g. `core/imagekit_agent.py`
imports the top-level `imagekit_source`/`tiktok_poster`).

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

# Comment-on-post (tiktok-commenter) — reuses the HiveMQ creds above
HIVEMQ_COMMENT_TOPIC=tiktok/comments          # optional, comment work topic to drain
HIVEMQ_COMMENT_STATUS_TOPIC=tiktok/comment-status  # optional, where comment outcomes are published
HIVEMQ_COMMENT_CLIENT_ID=tiktok-commenter     # optional, stable id → its own persistent session

# Local testing isolation (both consumers) — prepended verbatim to every topic AND
# client-id, so a test run uses a fully isolated queue + session. Unset in prod.
HIVEMQ_TOPIC_PREFIX=test/             # optional, e.g. "test/" → test/tiktok/posts

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
| `agent.py` | Orchestrator (HiveMQ path + dispatch + CLI): receive/drain → download → push → (auto-post) → report status. `--watch` calls `_watch_hivemq` (event-driven); `--once`/`process_once` dispatch `_process_hivemq` (drain) or delegate to `imagekit_agent` on `--source imagekit` | — |
| `core/imagekit_agent.py` | Legacy `--source imagekit` orchestration (`process_imagekit`/`catch_up_imagekit`), split out of `agent.py`: dedups the ImageKit folder against the local `agent_state.json` | — |
| `hivemq_source.py` | Thin wiring for the **post** queue: post-specific config + `_parse`, re-exporting an `MqttWorkQueue` instance's API (`watch`/`list_pending`/`update_status`/`close`) as module functions | `HiveMQSourceError` (from `mqtt_queue`) |
| `core/mqtt_queue.py` | **Shared** durable MQTT machinery: `MqttWorkQueue` (persistent QoS-1 session, drain, event-driven `watch`, manual-ack) + `make_config`, parameterized by a `parse`/`key_of`/topic config. Used by both consumers | `HiveMQSourceError` |
| `imagekit_source.py` | `download()` (used by both sources) + list from ImageKit Media Management API | `ImageKitSourceError` |
| `core/adb_pusher.py` | `run_adb()` wrapper + push file to gallery + media scan | `PhonePushError` |
| `core/tiktok_ui.py` | **Shared** low-level adb/UI primitives (`dump_ui`, `find_tappable`, `tap`, `force_stop`, `wait_and_tap`, `input_line`, `screen_size`, …) used by the poster, commenter and profile flows | (raises `PhonePushError`) |
| `tiktok_poster.py` | Drive TikTok's UI over adb to post (post-specific flow/labels; primitives from `tiktok_ui`) | `TikTokPostError` |
| `tiktok_profile.py` | Read the active TikTok account and switch to a target `@handle` before acting (shared by poster + commenter; primitives from `tiktok_ui`) | `TikTokProfileError` |
| `core/local_store.py` | One-JSON-per-message spool dir (store on receive, delete on success); shared by both consumers | — |
| `core/env_loader.py` | Zero-dep `.env` loader | — |

The **comment-on-post** feature is a second, independent consumer (its own
orchestrator + console script `tiktok-commenter`, its own HiveMQ topic / client-id;
no image handling, no AI):

| Module | Role | Error type |
|--------|------|-----------|
| `comment_agent.py` | Orchestrator + CLI: drain the comment topic → open post by URL → submit comment → report status. `--watch` (event-driven) / `--once` / `--catch-up` | — |
| `core/comment_source.py` | Thin wiring for the **comment** topic: comment-specific config + `_parse`, re-exporting its own `MqttWorkQueue` instance (own topic/client-id/status, `PostURL`+`Comment` parse) | `HiveMQSourceError` (from `mqtt_queue`) |
| `tiktok_commenter.py` | Drive TikTok's UI over adb: deep-link open a post → open comment sheet → type + submit one comment **or reply** (`reply_to`); also `read_comments` (scrape the sheet) + `parse_comment_rows`/`collect_comments` shared by the reader. Comment-specific flow/labels; primitives from `tiktok_ui` | `TikTokCommentError` |

The **comment-reader** is a third independent consumer — the read half of the
comment-reply loop. The backend can't read a post's comments (no TikTok API); only the
device can, via the UI. It drains read-jobs, scrapes each post's comments, and publishes
the list back so the backend can generate replies (sent to `tiktok-commenter` with `ReplyTo`):

| Module | Role | Error type |
|--------|------|-----------|
| `comment_reader_agent.py` | Orchestrator + CLI: drain `tiktok/comments-read` → `read_comments` → publish `{PostURL, comments, count, ts}` to `tiktok/comments-list`. `--watch`/`--once`/`--catch-up`. Read-only + idempotent: no local spool / `--retry` (a failed read is left unacked and redelivered) | — |
| `core/comment_read_source.py` | Thin wiring for the **read** topic: read-specific config + `_parse` (`PostURL`+optional `max`), own `MqttWorkQueue` (own topic/client-id, output topic = `comments-list`), re-exporting `list_pending`/`publish_comments`/`close`/`watch` | `HiveMQSourceError` (from `mqtt_queue`) |

`run_adb()` in `adb_pusher.py` is the single chokepoint for **all** adb calls
(push, shell, intents, UI dumps, taps). The shared `tiktok_ui.py` primitives import
it, and the poster/commenter/profile flows build on `tiktok_ui` rather than calling
`run_adb` directly (except for a few feature-specific intents/keyevents). Touch
device interaction in `tiktok_ui.py` / `adb_pusher.py`.

### One device, one flow at a time (`core/device_lock.py`)

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

**Shared vs. feature-specific.** Two modules hold the reusable machinery the four
feature modules used to duplicate: `tiktok_ui.py` (adb/UI primitives) and
`mqtt_queue.py` (durable MQTT work-queue). `hivemq_source.py` and
`comment_source.py` are now thin: each builds an `MqttWorkQueue` with its own
config/parse and re-exports `list_pending`/`update_status`/`close`/`watch`. The
poster/commenter/profile keep only their own screen labels and flow logic and
import the primitives from `tiktok_ui`. When TikTok's UI or the queue semantics
change, fix it once in the shared module.

### State machine (dedup)

**HiveMQ source (default):** the broker's persistent session is the dedup. MQTT
is push, so the two run modes differ:
- **`--watch` → `watch(handler)` (event-driven, the normal mode):** holds one
  persistent subscription and dispatches each message to a handler the instant it
  arrives — **no poll interval**. `on_message` enqueues to a single worker thread
  (so the network thread stays responsive for keepalive while a post runs); the
  handler's return value drives the ack.
- **`--once` / `--catch-up` → `list_pending()` (one-shot drain):** connect → drain
  every queued QoS-1 message (publish order, oldest first) → process → ack the done
  ones → `close()` disconnects. The drain ends after a short idle window
  (`DRAIN_IDLE`, no new message for ~2s) capped by `DRAIN_HARD_CAP`.

Both modes use `manual_ack=True`, so paho sends the PUBACK only when the agent
calls `update_status()`. After acting on a message:
- `post()` returns `"posted"` → publish `posted` + **ack** (message dropped)
- `post()` returns `"needs_manual"`/`"composer_open"`/`"wrong_account"` or raises →
  the message's spool file is **kept** (its status recorded) and the broker message
  gets `failed` + **ack** (`failed` drops the broker copy just like a "posted" ack so
  it won't loop; on `needs_manual` TikTok is **force-stopped** — not left open — and
  the item stays spooled for `--retry`; `wrong_account` means the target account
  couldn't be confirmed active so **nothing was posted** (TikTok is force-stopped
  too) — re-attempt with `--retry` once the account is switchable).
  An empty `ImageURL` is acked-and-dropped **without** spooling (nothing to retry).
- `--no-auto-post` → message is **never acked** (pushed to phone only); on the next
  reconnect the broker redelivers it, so it is re-pushed until it is actually posted

**Local spool dir (`queue_posts/`, gitignored, override with `--store-dir`):** the
broker holds the live queue, but **every received message is mirrored to its own JSON
file the instant it arrives** (`local_store.store`, written before the phone is
touched), so nothing is lost if the broker then drops it. One file per item
(`<slug>-<hash>.json`, holding `{key, payload: fields, status, error, attempts, ts}`).
On a successful post the file is **deleted** (`local_store.remove`); any other outcome
leaves it on disk — so `queue_posts/` accumulates exactly the items still needing
attention. `tiktok-agent --retry` re-runs download→push→post for each surviving file
**talking only to local disk** (no HiveMQ): on `posted` the file is deleted, otherwise
its status/attempts are updated. Run `--retry` by hand once the phone/UI is ready.
Trade-off: a crash between a successful post and the broker ack leaves the broker
message unacked, so it is redelivered and could be re-posted later (the local file is
already gone, so the redelivery just re-spools and re-posts).

The persistent client is a module-level singleton in `hivemq_source.py`;
`_process_hivemq`/`_catch_up_hivemq` wrap their drain in `try/finally: close()`,
and `watch()` does the same around its blocking loop, so every run ends with a
clean disconnect.

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
  screen it stops and returns `"needs_manual"` rather than tapping blindly. TikTok
  is then **`am force-stop`ped** — on success after waiting `POST_SUCCESS_KILL_DELAY`
  (so the upload finishes), and **immediately** on every error outcome
  (`needs_manual`/`wrong_account`) so the app is never left in a half-finished
  state; the item stays spooled for `--retry`. The **only** path that leaves TikTok
  open is `composer_open` (`--no-auto-post`), which is an explicit "push to phone,
  finish by hand" mode, not an error.

**When TikTok's UI changes, the constants at the top of `tiktok_poster.py` are the
tuning knobs:** `TIKTOK_PACKAGES`, `POST_FLOW_STEPS`, `CAPTION_HINTS`,
`STEP_DELAY`, `STEP_RETRIES`, `POST_SUCCESS_KILL_DELAY`.

### Caption handling

TikTok has a single text field. `build_post_text` combines the `Caption` (hook)
and `Description` message fields (or ImageKit `customMetadata.caption`/
`description` in legacy mode) into one string — caption first, description on the
next line. Typing uses `adb input text`, which **cannot enter emoji/non-ASCII**:
`_input_line` strips non-ASCII and quote chars, maps spaces to `%s`; newlines
become `KEYCODE_ENTER`. The full emoji caption still lives in the published
message. (Note: the pipeline does **not** store hashtags — captions/descriptions
are hashtag-free.)

### Multi-account (`tiktok_profile.py`, the third brittle part)

One device, multiple TikTok accounts logged into the **in-app account switcher**.
A message may carry an optional **`Account`** field (a TikTok `@handle`); when set,
the poster/commenter makes that account active **before** acting. Both
`tiktok_poster.post` and `tiktok_commenter.comment_on_post` call
`tiktok_profile.ensure_account(account, ...)` first (the poster before opening the
composer, the commenter before opening the post). The CLIs also accept `--account`.

`ensure_account` opens the Profile tab and reads the active `@handle`; if it already
matches (compared normalized — leading `@` stripped, lowercased) it returns,
otherwise it opens the account switcher, taps the target row, and **re-reads to
confirm**. If it can't confirm the target is active it raises `TikTokProfileError`,
which the posters map to the status **`"wrong_account"`** — **nothing is posted /
commented**, the spool file is kept, and the broker message is acked `failed` (so it
doesn't loop). Re-attempt with `--retry` once the account is switchable.

This is the **most fragile** flow (TikTok A/B-tests the profile header heavily) and
its selectors are **calibrated on a real device** (`com.ss.android.ugc.trill`, ID
locale) — re-verify with `uiautomator dump` if the UI shifts. Three calibration
facts drove the implementation, encoded in the constants/helpers at the top of
`tiktok_profile.py`:
- **The home feed blocks `uiautomator dump`** (it returns the launcher window
  behind it), so the profile is opened by **deep link** (`PROFILE_DEEPLINKS`,
  `snssdk1233://profile`), **not** by tapping the bottom-nav tab. Other TikTok
  screens (profile, search, composer, comment sheet) dump fine.
- The active handle is read from a `@name` **TEXT** node (`HANDLE_RE` requires a
  letter so untranslated `content-desc="@2131…"` resource refs aren't mistaken for
  it). The **account switcher is opened by tapping the display-NAME button** just
  above the handle (`_find_switch_trigger` anchors on the handle node).
- In the "Beralih akun" sheet each account row's **content-desc is the bare handle
  without `@`** (e.g. `captgani`), matched normalized by `_find_account_row`.

Use `uv run tiktok-profile` (no arg = print the current `@handle`; `tiktok-profile
@target` = switch) to re-calibrate in isolation. The low-level UI helpers come from
the shared `tiktok_ui.py`; this module adds only the profile-header / account-switcher
selectors and is itself **shared** by both consumers (the account check is identical).
It fails *safe*: an unconfirmed account never posts to the wrong one.

### Comment-on-post (`tiktok-commenter`, the other brittle part)

A second, independent consumer that **leaves a comment on an existing TikTok
post** — triggered by HiveMQ exactly like the poster, but on its **own topic**
(`tiktok/comments`, env `HIVEMQ_COMMENT_TOPIC`) drained by its **own persistent
session** (`HIVEMQ_COMMENT_CLIENT_ID=tiktok-commenter`, so it runs as a separate
process and never collides with `tiktok-agent`'s session). No image handling, no
AI — the exact comment text is in the message.

- **Message contract:** `{"PostURL": "...", "Comment": "...", "Account": "@handle",
  "ReplyTo": {"author": "@someone", "text": "their comment"}}`
  — **no `id`, no `CreatedAt`.** MQTT acking uses the message's `mid`, not the
  payload, so no `id` is needed; status is keyed by `PostURL`. `Account` is optional
  (switch to that TikTok account first, see Multi-account above). **`ReplyTo` is
  optional** — when set (must carry a non-empty `author`), the comment is submitted as a
  **reply** to that existing comment instead of a top-level comment; `ReplyTo.text` (a
  substring of the target comment) only disambiguates when one author has several
  comments. Absent `ReplyTo` = top-level comment (unchanged behavior). `_parse` drops +
  logs any message missing a non-empty `PostURL` or `Comment`.
- **Reply flow (`reply_to`):** after opening the comment sheet, `comment_on_post` finds
  the target comment row (matching author + optional text substring, scrolling the sheet
  as needed via `parse_comment_rows`/`_find_and_tap_reply`) and taps its **Balas/Reply**
  button — which auto-focuses the input ("Membalas <author>") — then types + sends exactly
  like a top-level comment. If the target can't be found it returns the new status
  **`comment_not_found`** (force-stop, kept for `--retry`) without submitting.
- **Flow (`comment_agent.py` → `tiktok_commenter.comment_on_post`):** deep-link
  open the post (`am start -a android.intent.action.VIEW -d <url> -p <pkg>`, the
  reliable part) → **pause the video** (`_pause_video`) → tap the comment icon (its
  content-desc embeds a live count, so matched by **substring** via
  `_find_partial`/`_wait_and_tap_partial`) → focus the input field (capturing its
  bounds) → type → hide keyboard → **tap send positionally** → wait
  `COMMENT_SUCCESS_KILL_DELAY` then `am force-stop` TikTok.
- **Two calibration facts (the part that breaks silently, learned on-device):**
  TikTok loops the post video, which keeps the UI perpetually **non-idle** so
  `uiautomator dump` fails with *"could not get idle state"* and returns a **stale**
  tree — so the very first selector lookup matches nothing and you get
  `needs_manual` even though the controls are on screen. `_pause_video` fixes this:
  it taps the video center to pause, then **confirms a dump actually succeeds**
  (retrying — the post is still loading for the first moment, so an early tap is a
  no-op), returning as soon as it does (a second tap would *resume* playback).
  Second: once the comment input is **focused**, its blinking cursor keeps the UI
  non-idle again, so the **send button can't be found by dumping** — it's tapped
  **positionally** at the right end of the input row (`SEND_BTN_X_FRAC` × width, at
  the input field's captured vertical center) after hiding the keyboard. The
  `PAUSE_TAP_*`/`SEND_BTN_X_FRAC` fractions are the geometry knobs.
- **Statuses (all ack-drop so a stuck UI doesn't loop):** `commented` (success),
  `needs_manual` (an unrecognized screen — stop, don't tap blindly),
  `skipped_non_ascii` (nothing typeable after stripping — not submitted),
  `wrong_account` (target `Account` couldn't be confirmed active — not submitted),
  `failed` (open/adb error). On success TikTok is force-stopped after
  `COMMENT_SUCCESS_KILL_DELAY`; on every **error** outcome
  (`needs_manual`/`skipped_non_ascii`/`wrong_account`) it is force-stopped
  **immediately** so it's never left open. `--dry-run` opens + focuses the input and
  logs the comment but **never submits**, leaves the message unacked, and is the one
  path that leaves the app open (for inspection).
- **Local spool dir (mirrors the poster):** every received comment is mirrored to its
  own JSON file in `queue_comments/` on receive (keyed by `PostURL`, payload
  `{post_url, comment, account}`, via the shared `local_store.py`), before the phone is
  touched.
  Terminal outcomes delete the file — `commented` (success) **and** `skipped_non_ascii`
  (can never be typed, so retrying is pointless); `needs_manual`/`failed`/`wrong_account`
  keep it.
  `--dry-run` does **not** spool. `tiktok-commenter --retry` re-attempts each surviving
  file locally (no HiveMQ). Override the dir with `--store-dir`.
- **Same brittleness + defensiveness as the poster:** the UI labels live in the
  constants block at the top of `tiktok_commenter.py` (`COMMENT_OPEN_SUBSTRINGS`,
  `COMMENT_INPUT_HINTS`, `PAUSE_TAP_X_FRAC`/`PAUSE_TAP_Y_FRAC`, `SEND_BTN_X_FRAC`,
  `COMMENT_SUCCESS_KILL_DELAY`; `STEP_DELAY`/`STEP_RETRIES` are imported from
  `tiktok_ui`) — **guesses that must be calibrated on-device against a real
  `uiautomator dump`.** (The send button has **no** label constant — see the
  positional-tap calibration fact above.)
  The low-level UI helpers (`dump_ui`, `find_tappable`, `input_line`, …) come from the
  shared `tiktok_ui.py`, and the MQTT plumbing from the shared `mqtt_queue.py`;
  `tiktok_commenter.py`/`comment_source.py` carry only the comment-specific labels,
  flow and config.
- **ASCII limit (same as captions):** `adb input text` can't type non-ASCII/emoji;
  a comment with nothing typeable after stripping is **not** submitted
  (`skipped_non_ascii`). Realistic scope is Latin-script languages.
- **`--catch-up`:** drains the comment backlog and marks each `commented` (publish
  + ack) **without commenting** — run once before the first `--watch`.

### Comment-reader (`tiktok-comment-reader`, the read half of the reply loop)

A backend that generates **reply** text first needs to *read* the post's existing
comments — but it has no TikTok API, so only the device can see them (via the UI). This
third consumer closes that gap: it drains read-jobs, scrapes a post's comments on the
phone, and publishes the list back; the backend then generates a positive reply per
comment and sends each to `tiktok-commenter` as a reply-job (`ReplyTo`). The full
round-trip is **read-job → comment-list → reply-job**, all over HiveMQ (the only channel
between the two repos).

- **Read-job contract (in):** `{"PostURL": "...", "max": 10}` on `tiktok/comments-read`
  (`HIVEMQ_COMMENT_READ_TOPIC`), drained by its **own** persistent session
  (`HIVEMQ_COMMENT_READ_CLIENT_ID=tiktok-comment-reader`). `max` is optional.
- **Comment-list contract (out):** `{"PostURL": "...", "comments": [{"author": "...",
  "text": "..."}], "count": N, "ts": ...}` published to `tiktok/comments-list`
  (`HIVEMQ_COMMENT_LIST_TOPIC`); the backend subscribes here. On a read failure the same
  message is published with `comments: []` and an `error` field.
- **Comment cap (`max`):** precedence is the read-job's `max` → `--max` → `$COMMENT_READ_MAX`
  → `DEFAULT_MAX_COMMENTS` (10). TikTok's sheet defaults to a **top/relevance sort**, so the
  first N scraped are the most-engaged comments. The scrape stops at the cap **or** after
  `SCROLL_STABLE_PASSES` swipes with no new rows (end of thread).
- **Scraping (the calibrated, brittle part — `parse_comment_rows`):** with the video
  paused the comment sheet **is** dumpable. Each row is anchored on its **Balas/Reply**
  button (matched by label, stable); the author (`ROW_AUTHOR_ID`/`id/title` text) and body
  (`ROW_TEXT_ID`/`id/enp` text) are the id nodes falling **between the previous Reply
  button and this one** (so "Lihat N balasan" and adjacent rows can't be mis-attributed).
  These resource-id leaf names are **obfuscated and WILL drift** — re-verify with a real
  `uiautomator dump` if rows stop parsing. Three calibration facts: (1) **image/sticker
  comments have no text node** → skipped (can't be replied to); (2) a row whose author
  **scrolled partly off** resolves to no author → dropped (re-captured on an adjacent
  pass); (3) **nested replies are excluded** — collapsed reply threads ("Lihat N
  balasan") aren't scraped at all, and replies shown *inline* (e.g. a just-posted one)
  are dropped by **indentation**: a row whose author sits more than
  `REPLY_INDENT_TOLERANCE` px right of the left-most (top-level) author column is a
  reply, not a top-level comment. So only top-level comments are returned.
  `collect_comments` scrolls (`_swipe_sheet`), deduping by `(author, text)`.
- **Stateless + idempotent:** no local spool, no `--retry` — re-reading a post just
  re-publishes its current comments, and a failed read is left unacked so the broker
  redelivers it. All stateful policy (which posts to read, which comments deserve a reply,
  dedup of already-replied) lives on the **backend** — the correlation key is
  `(author + text)` since TikTok exposes no stable comment id.
- **Tuning knobs:** `DEFAULT_MAX_COMMENTS` (in `comment_reader_agent.py`); and in
  `tiktok_commenter.py`: `ROW_AUTHOR_ID`/`ROW_TEXT_ID`/`REPLY_BTN_LABELS`,
  `SHEET_CONTENT_MIN_X`, `REPLY_INDENT_TOLERANCE`, `SCROLL_*` (swipe geometry + loop bounds).

## Cross-repo coupling (easy to break)

- **MQTT message contract (primary):** the pipeline publishes (QoS 1, retained
  false) and the agent drains JSON messages on the work topic with fields `id`,
  `Caption`, `Description`, `ImageURL`, `ImagePath`, `CreatedAt`, and optional
  `Account` (a TikTok `@handle` to post as). `id` is the required correlation key
  echoed back in status messages (`{id, status, ts}` on the status topic). Both
  sides must agree on the topic names (`HIVEMQ_TOPIC` / `HIVEMQ_STATUS_TOPIC`) and
  the QoS-1/persistent-session semantics. Renaming a field, changing the topics, or
  dropping to QoS 0 silently breaks the agent. (`HIVEMQ_TOPIC_PREFIX` prepends a
  prefix to the topics + client-id for local-test isolation — keep it unset in prod
  on both repos.)
- **Comment message contract (`tiktok-commenter`):** a producer publishes (QoS 1,
  retained false) JSON `{"PostURL": "https://www.tiktok.com/@u/video/<id>",
  "Comment": "...", "Account": "@handle", "ReplyTo": {"author": "@u", "text": "..."}}`
  to `tiktok/comments` (`HIVEMQ_COMMENT_TOPIC`); the agent reports `{PostURL, status,
  ts}` on `tiktok/comment-status` (`HIVEMQ_COMMENT_STATUS_TOPIC`). No `id` field;
  `Account` and `ReplyTo` are optional (`ReplyTo` present = post a reply to that comment).
  Renaming a field, changing the topic, or dropping to QoS 0 silently breaks the consumer.
- **Comment-reader contract (`tiktok-comment-reader`):** the backend publishes read-jobs
  `{"PostURL": "...", "max": 10}` (QoS 1) to `tiktok/comments-read`
  (`HIVEMQ_COMMENT_READ_TOPIC`); the device publishes the scraped list `{"PostURL", "comments":
  [{"author", "text"}], "count", "ts"}` to `tiktok/comments-list` (`HIVEMQ_COMMENT_LIST_TOPIC`),
  which the backend subscribes to. The correlation key tying a listed comment to its reply-job
  is `(author + text)` — TikTok exposes no stable comment id. Renaming a field, changing a
  topic, or dropping to QoS 0 silently breaks the loop.
- **ImageKit `ImageURL`:** the agent downloads the image from the public CDN URL the
  pipeline put in each message.
- **Legacy ImageKit coupling (`--source imagekit` only):** filename convention
  (`tiktok_<ts>.<ext>` on phone vs `tiktok_<ts>_<unique>.<ext>` on ImageKit, matched
  by `caption_from_imagekit`), `customMetadata` keys `caption`/`description`, and the
  adb/Basic auth scheme mirroring the pipeline's uploader.

Changing any of these in one repo silently breaks the other.
