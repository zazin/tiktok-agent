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
| `imagekit_agent.py` | Legacy `--source imagekit` orchestration (`process_imagekit`/`catch_up_imagekit`), split out of `agent.py`: dedups the ImageKit folder against the local `agent_state.json` | — |
| `hivemq_source.py` | `watch(handler)` (persistent push subscription) + `list_pending()`/`update_status()`/`close()` (one-shot drain), over MQTT (paho, persistent QoS-1 session) | `HiveMQSourceError` |
| `imagekit_source.py` | `download()` (used by both sources) + list from ImageKit Media Management API | `ImageKitSourceError` |
| `adb_pusher.py` | `run_adb()` wrapper + push file to gallery + media scan | `PhonePushError` |
| `tiktok_poster.py` | Drive TikTok's UI over adb to post | `TikTokPostError` |
| `tiktok_profile.py` | Read the active TikTok account and switch to a target `@handle` before acting (shared by poster + commenter) | `TikTokProfileError` |
| `local_store.py` | One-JSON-per-message spool dir (store on receive, delete on success); shared by both consumers | — |
| `env_loader.py` | Zero-dep `.env` loader | — |

The **comment-on-post** feature is a second, independent consumer (its own
orchestrator + console script `tiktok-commenter`, its own HiveMQ topic / client-id;
no image handling, no AI):

| Module | Role | Error type |
|--------|------|-----------|
| `comment_agent.py` | Orchestrator + CLI: drain the comment topic → open post by URL → submit comment → report status. `--watch` (event-driven) / `--once` / `--catch-up` | — |
| `comment_source.py` | Self-contained mirror of `hivemq_source.py` for the comment topic (own topic/client-id/status, `PostURL`+`Comment` parse, persistent QoS-1 session) | `HiveMQSourceError` (reused) |
| `tiktok_commenter.py` | Drive TikTok's UI over adb: deep-link open a post → open comment sheet → type + submit one comment | `TikTokCommentError` |

`run_adb()` in `adb_pusher.py` is the single chokepoint for **all** adb calls
(push, shell, intents, UI dumps, taps) — `tiktok_poster.py` and
`tiktok_commenter.py` import it rather than shelling out themselves. Touch device
interaction there.

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
@target` = switch) to re-calibrate in isolation. The low-level UI helpers are
**copied** from `tiktok_poster.py`, following the per-feature-duplication pattern;
unlike them this one module is **shared** by both consumers (the account check is
identical). It fails *safe*: an unconfirmed account never posts to the wrong one.

### Comment-on-post (`tiktok-commenter`, the other brittle part)

A second, independent consumer that **leaves a comment on an existing TikTok
post** — triggered by HiveMQ exactly like the poster, but on its **own topic**
(`tiktok/comments`, env `HIVEMQ_COMMENT_TOPIC`) drained by its **own persistent
session** (`HIVEMQ_COMMENT_CLIENT_ID=tiktok-commenter`, so it runs as a separate
process and never collides with `tiktok-agent`'s session). No image handling, no
AI — the exact comment text is in the message.

- **Message contract:** `{"PostURL": "...", "Comment": "...", "Account": "@handle"}`
  — **no `id`, no `CreatedAt`.** MQTT acking uses the message's `mid`, not the
  payload, so no `id` is needed; status is keyed by `PostURL`. `Account` is optional
  (switch to that TikTok account first, see Multi-account above). `_parse` drops +
  logs any message missing a non-empty `PostURL` or `Comment`.
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
  `STEP_DELAY`, `STEP_RETRIES`, `COMMENT_SUCCESS_KILL_DELAY`) — **guesses that must
  be calibrated on-device against a real `uiautomator dump`.** (The send button has
  **no** label constant — see the positional-tap calibration fact above.)
  The low-level UI helpers (`_dump_ui`, `_find_tappable`, `_input_line`, …) are
  intentionally **copied** from `tiktok_poster.py` (no shared util module), and
  `comment_source.py` likewise duplicates `hivemq_source.py`'s MQTT plumbing so the
  working poster consumer stays untouched.
- **ASCII limit (same as captions):** `adb input text` can't type non-ASCII/emoji;
  a comment with nothing typeable after stripping is **not** submitted
  (`skipped_non_ascii`). Realistic scope is Latin-script languages.
- **`--catch-up`:** drains the comment backlog and marks each `commented` (publish
  + ack) **without commenting** — run once before the first `--watch`.

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
  "Comment": "...", "Account": "@handle"}` to `tiktok/comments`
  (`HIVEMQ_COMMENT_TOPIC`); the agent reports `{PostURL, status, ts}` on
  `tiktok/comment-status` (`HIVEMQ_COMMENT_STATUS_TOPIC`). No `id` field; `Account`
  is optional. Renaming a field, changing the topic, or dropping to QoS 0 silently
  breaks the consumer.
- **ImageKit `ImageURL`:** the agent downloads the image from the public CDN URL the
  pipeline put in each message.
- **Legacy ImageKit coupling (`--source imagekit` only):** filename convention
  (`tiktok_<ts>.<ext>` on phone vs `tiktok_<ts>_<unique>.<ext>` on ImageKit, matched
  by `caption_from_imagekit`), `customMetadata` keys `caption`/`description`, and the
  adb/Basic auth scheme mirroring the pipeline's uploader.

Changing any of these in one repo silently breaks the other.
