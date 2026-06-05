# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The device-side half of a two-repo TikTok system. The **server** half lives in a
separate repo, [tiktok-pipeline](https://github.com/zazin/tiktok-pipeline): it
generates images, uploads them to ImageKit, and writes one **Airtable `Posts`**
record per post (caption, description, and the public ImageKit `ImageURL`) with
`Status = "pending"`. **This repo** runs on a computer with an Android phone
attached via adb, polls that Airtable table, and for each pending record:
downloads the image from its `ImageURL` → pushes it into the phone gallery →
auto-posts it to TikTok by driving the on-device UI over adb → flips the record's
`Status` to `posted`/`failed`.

**Airtable is the queue / source of truth.** There is no server, database, or
message broker between the two halves — the agent discovers work by listing
`pending` rows and reports back by updating `Status` (no local dedup state). The
two repos are coupled by the Airtable schema (see Cross-repo coupling below) —
fields `Caption`, `Description`, `ImageURL`, `ImagePath`, `Status`, `CreatedAt`.
The schema reference lives at `tiktok-pipeline/docs/airtable.md`.

The legacy **ImageKit folder queue** is still available via `--source imagekit`;
it dedups `fileId`s against the local `agent_state.json` and reads caption/
description from ImageKit `customMetadata`. ImageKit images are downloaded the
same way in both modes (public CDN URL, no auth).

## Commands

Managed with [uv](https://docs.astral.sh/uv/). Stdlib only — no third-party deps.

```bash
uv sync

# Console scripts (defined in pyproject.toml [project.scripts]):
uv run tiktok-agent --catch-up     # skip current pending backlog WITHOUT posting (run once first)
uv run tiktok-agent --watch        # poll loop, auto-posts each NEW pending record (default interval 60s)
uv run tiktok-agent --once         # single poll cycle
uv run tiktok-agent --watch --no-auto-post   # push to phone only, leave rows pending
uv run tiktok-agent --once --source imagekit # legacy: poll the ImageKit folder instead
uv run tiktok-airtable                       # inspect the Airtable queue (pending rows)
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
# Airtable source (default) — REST API, Bearer auth
AIRTABLE_API_KEY=pat...           # PAT with data.records:read + data.records:write (required)
AIRTABLE_BASE_ID=app...           # optional, defaults to DEFAULT_BASE_ID in airtable_source.py
AIRTABLE_TABLE_NAME=Posts         # optional, defaults to "Posts"

# ImageKit — still needed to download images (and for --source imagekit)
IMAGEKIT_PRIVATE_KEY=private_...
IMAGEKIT_URL_ENDPOINT=https://ik.imagekit.io/your_id
```
Real environment variables always win over `.env`. Airtable auth is a Bearer PAT;
ImageKit auth is HTTP Basic with the private key as username and an empty password.
(Image downloads hit the public ImageKit CDN URL and need no auth.)

### Runtime prerequisites (not Python)

- `adb` on PATH (`brew install android-platform-tools`)
- Phone connected, USB debugging authorized, **unlocked**, TikTok installed + logged in

## Architecture / module flow

The orchestrator is `agent.py::process_once`, which wires the other modules in
sequence. Each module is a single-responsibility seam with its own error type:

| Module | Role | Error type |
|--------|------|-----------|
| `agent.py` | Orchestrator: poll → download → push → (auto-post) → update status. `process_once` dispatches to `_process_airtable` (default) or `_process_imagekit` on `--source` | — |
| `airtable_source.py` | `list_pending()` + `update_status()` against the Airtable REST API (Bearer PAT) | `AirtableSourceError` |
| `imagekit_source.py` | `download()` (used by both sources) + list from ImageKit Media Management API | `ImageKitSourceError` |
| `adb_pusher.py` | `run_adb()` wrapper + push file to gallery + media scan | `PhonePushError` |
| `tiktok_poster.py` | Drive TikTok's UI over adb to post | `TikTokPostError` |
| `env_loader.py` | Zero-dep `.env` loader | — |

`run_adb()` in `adb_pusher.py` is the single chokepoint for **all** adb calls
(push, shell, intents, UI dumps, taps) — `tiktok_poster.py` imports it rather than
shelling out itself. Touch device interaction there.

### State machine (dedup)

**Airtable source (default):** Airtable itself is the dedup. `list_pending()`
returns only `Status == "pending"` rows, sorted `CreatedAt` ascending (oldest
first), so post order matches creation order. After acting on a record the agent
calls `update_status()`:
- `post()` returns `"posted"` → `Status = "posted"`
- `post()` returns `"needs_manual"`, raises, or `ImageURL` is empty → `Status = "failed"`
  (the 3-state schema has no "manual" state; `failed` drops the row out of the
  `pending` query so it won't loop; on `needs_manual` the composer is still open on
  the phone to finish by hand)
- `--no-auto-post` → row is **left `pending`** (pushed to phone only; re-runs will
  re-push until it is actually posted)

There is **no local state file** in this mode (per the source-of-truth design).
Trade-off: a crash between a successful post and the `update_status` call leaves
the row `pending`, so it could be re-posted on the next poll.

`--catch-up` flips every current `pending` row to `posted` without posting.
**Run it once before the first `--watch`** — auto-post is ON by default, so
otherwise the first poll posts the whole backlog.

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
and `Description` Airtable fields (or ImageKit `customMetadata.caption`/
`description` in legacy mode) into one string — caption first, description on the
next line. Typing uses `adb input text`, which **cannot enter emoji/non-ASCII**:
`_input_line` strips non-ASCII and quote chars, maps spaces to `%s`; newlines
become `KEYCODE_ENTER`. The full emoji caption still lives in Airtable. (Note: the
pipeline does **not** store hashtags in Airtable — captions/descriptions are
hashtag-free.)

## Cross-repo coupling (easy to break)

- **Airtable schema (primary):** the pipeline writes and the agent reads the
  `Posts` table fields `Caption`, `Description`, `ImageURL`, `ImagePath`,
  `ImageKitFileId`, `Status`, `CreatedAt`. The `Status` single-select must keep its
  three values (`pending`/`posted`/`failed`). Authoritative reference:
  `tiktok-pipeline/docs/airtable.md` (and `airtable_migrate.py::DESIRED_FIELDS` in
  the pipeline repo). Renaming a field or changing the `Status` values silently
  breaks the agent.
- **ImageKit `ImageURL`:** the agent downloads the image from the public CDN URL the
  pipeline stored on each record.
- **Legacy ImageKit coupling (`--source imagekit` only):** filename convention
  (`tiktok_<ts>.<ext>` on phone vs `tiktok_<ts>_<unique>.<ext>` on ImageKit, matched
  by `caption_from_imagekit`), `customMetadata` keys `caption`/`description`, and the
  adb/Basic auth scheme mirroring the pipeline's uploader.

Changing any of these in one repo silently breaks the other.
