# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The device-side half of a two-repo TikTok system. The **server** half lives in a
separate repo, [tiktok-pipeline](https://github.com/zazin/tiktok-pipeline): it
generates images and uploads them to the ImageKit `/tiktok` folder. **This repo**
runs on a computer with an Android phone attached via adb, polls that same ImageKit
folder, and for each new image: downloads it → pushes it into the phone gallery →
auto-posts it to TikTok by driving the on-device UI over adb.

**ImageKit is the queue.** There is no server, database, or message broker between
the two halves — the agent discovers work purely by listing the folder and
deduping `fileId`s against a local JSON state file. The two repos are coupled by a
filename convention (see Cross-repo coupling below) and by ImageKit
`customMetadata` keys (`caption`, `description`).

## Commands

Managed with [uv](https://docs.astral.sh/uv/). Stdlib only — no third-party deps.

```bash
uv sync

# Three console scripts (defined in pyproject.toml [project.scripts]):
uv run tiktok-agent --catch-up     # mark current backlog as seen WITHOUT posting (run once first)
uv run tiktok-agent --watch        # poll loop, auto-posts each NEW image (default interval 60s)
uv run tiktok-agent --once         # single poll cycle
uv run tiktok-agent --watch --no-auto-post   # push to phone only, don't post
uv run tiktok-source --folder /tiktok        # inspect the ImageKit queue
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
IMAGEKIT_PRIVATE_KEY=private_...
IMAGEKIT_URL_ENDPOINT=https://ik.imagekit.io/your_id
```
Real environment variables always win over `.env`. ImageKit auth is HTTP Basic
with the private key as username and an empty password.

### Runtime prerequisites (not Python)

- `adb` on PATH (`brew install android-platform-tools`)
- Phone connected, USB debugging authorized, **unlocked**, TikTok installed + logged in

## Architecture / module flow

The orchestrator is `agent.py::process_once`, which wires the other modules in
sequence. Each module is a single-responsibility seam with its own error type:

| Module | Role | Error type |
|--------|------|-----------|
| `agent.py` | Orchestrator: poll → dedup → download → push → (auto-post) → record state | — |
| `imagekit_source.py` | List + download from ImageKit Media Management API | `ImageKitSourceError` |
| `adb_pusher.py` | `run_adb()` wrapper + push file to gallery + media scan | `PhonePushError` |
| `tiktok_poster.py` | Drive TikTok's UI over adb to post | `TikTokPostError` |
| `env_loader.py` | Zero-dep `.env` loader | — |

`run_adb()` in `adb_pusher.py` is the single chokepoint for **all** adb calls
(push, shell, intents, UI dumps, taps) — `tiktok_poster.py` imports it rather than
shelling out itself. Touch device interaction there.

### State machine (dedup)

`agent_state.json` (gitignored) is `{"processed": {fileId: {name, status, ts, ...}}}`.
An image is processed exactly once: anything whose `fileId` is already a key is
skipped. State is saved **after each item** so a crash mid-batch doesn't reprocess
completed work. `status` values: `posted`, `needs_manual`, `pushed`, `failed`,
`catch-up`. New images are processed **oldest-first** (`reversed(list_images())`,
which returns newest-first) so post order matches creation order.

`--catch-up` writes `status: "catch-up"` entries for the entire current folder
without posting. **This must be run once before the first `--watch`** — auto-post
is ON by default, so otherwise the first poll posts the whole backlog.

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

TikTok has a single text field. `build_post_text` combines ImageKit
`customMetadata.caption` (hook + hashtags) and `customMetadata.description` into
one string — caption first, description on the next line. Typing uses
`adb input text`, which **cannot enter emoji/non-ASCII**: `_input_line` strips
non-ASCII and quote chars, maps spaces to `%s`; newlines become `KEYCODE_ENTER`.
The full emoji caption still lives in ImageKit metadata.

## Cross-repo coupling (easy to break)

- **Filename convention:** the pipeline pushes `tiktok_<ts>.<ext>` to the phone but
  uploads `tiktok_<ts>_<unique>.<ext>` to ImageKit. `caption_from_imagekit` relies
  on this — it matches an on-phone file's base name against ImageKit names with
  `ik_base == base or ik_base.startswith(base + "_")`.
- **Metadata keys:** `caption` and `description` under ImageKit `customMetadata`.
- **adb auth scheme** in `imagekit_source.py` mirrors the pipeline's uploader.

Changing any of these in one repo silently breaks the other.
