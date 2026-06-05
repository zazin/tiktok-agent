# TikTok Agent

The device-side half of the TikTok system. It runs on a **computer with an Android phone connected via adb**, polls the **Airtable `Posts` table** that [tiktok-pipeline](https://github.com/zazin/tiktok-pipeline) writes to, and for each `pending` record: downloads the image from its public ImageKit URL, pushes it into the phone's gallery, **auto-posts it to TikTok** (caption + description straight from the Airtable record) by driving the on-device UI over adb, then flips the record's `Status` to `posted`/`failed`.

Auto-post is **on by default** — run `tiktok-agent --watch` and it posts new records hands-free. **Airtable is the queue / source of truth** — there is no Firebase or server in between; the agent discovers work by listing `pending` rows and reports back by updating `Status`.

```
tiktok-pipeline (server)          Airtable `Posts`          tiktok-agent (this, computer + adb + phone)
  generate → upload to ImageKit     one row per post   ◄──────  list pending (Status="pending", oldest first)
  → write Airtable row  ──────────► (Caption, Desc,            → download image from row's ImageURL (ImageKit CDN)
    Status="pending"                 ImageURL, Status)         → adb push to gallery
                                                                → auto-post to TikTok (caption + description)
                                                                → PATCH Status = posted / failed
```

The legacy **ImageKit-folder queue** is still available with `--source imagekit` (dedups `fileId`s against a local JSON state file and reads caption/description from ImageKit `customMetadata`).

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Orchestrator — poll → download → push → auto-post → update status (`--source`, `--catch-up`) |
| `airtable_source.py` | List `pending` rows + update `Status` via the Airtable REST API (also the `tiktok-airtable` CLI) |
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
#   # Airtable source (default) — only the API key is required
#   AIRTABLE_API_KEY=pat...
#   AIRTABLE_BASE_ID=app...    # optional, defaults to the base id baked into airtable_source.py
#   AIRTABLE_TABLE_NAME=Posts  # optional, defaults to "Posts"
#   # ImageKit — still needed to download images (and for --source imagekit)
#   IMAGEKIT_PRIVATE_KEY=private_...
#   IMAGEKIT_URL_ENDPOINT=https://ik.imagekit.io/your_id

# Always-on: watch the Airtable queue and auto-post every new pending row, hands-free
uv run tiktok-agent --catch-up        # first: flip the existing pending backlog to posted (skip it)
uv run tiktok-agent --watch           # then: auto-posts each NEW pending row as it arrives

# One pass (also auto-posts pending rows by default)
uv run tiktok-agent --once

# Push to the phone only, leave rows pending
uv run tiktok-agent --watch --no-auto-post

# Inspect the Airtable queue (pending rows)
uv run tiktok-airtable

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
for push-only. Because it posts every `pending` row, run `tiktok-agent --catch-up` once first
to clear the current backlog (flips them to `posted` without posting) — otherwise the first run
posts all of it.

**Caption + description** come straight from the Airtable record's `Caption` and `Description`
fields, combined into TikTok's single text field (caption first, description on the next line).
In `--source imagekit` mode they're read from ImageKit `customMetadata` instead; `tiktok-post`
can do the same lookup with `--from-imagekit`, or you can pass `--caption`/`--description`.

Typing uses `adb input text`, which can't enter emoji — emoji are stripped; the full emoji
caption still lives in the Airtable record. (The pipeline stores no hashtags.)

### Status workflow

The `Status` single-select has three values, owned jointly by both repos:

| Value | Set by | Meaning |
|-------|--------|---------|
| `pending` | pipeline | Freshly written, ready to post. The agent queries for this. |
| `posted` | agent | Successfully posted to TikTok. |
| `failed` | agent | Posting failed, or auto-post stopped on an unrecognized screen (`needs_manual` — the composer is left open on the phone to finish by hand). |

There is **no local state file** in Airtable mode. Trade-off: a crash between a successful post
and the `Status` update leaves the row `pending`, so it could be re-posted on the next poll.

## Requirements

- [uv](https://docs.astral.sh/uv/), Python 3.10+ (no third-party deps — stdlib only)
- `adb` (`brew install android-platform-tools`)
- An Android phone connected with **USB debugging** authorized, **unlocked**, with **TikTok installed and logged in**
- Airtable credentials (a PAT with `data.records:read` + `data.records:write`) and ImageKit access to the public image URLs

## Auto-post caveat

Auto-post is **on by default** and **publishes real public posts** to the logged-in TikTok account. `tiktok_poster.py` drives TikTok's UI over adb in two phases:

1. **Phase 1:** a `SEND` intent opens TikTok's composer with the image attached. Reliable.
2. **Phase 2:** dumps the UI tree and taps Foto → Next → Post by label, typing the caption first. **Brittle** (breaks when TikTok changes its UI) and potentially against TikTok's Terms of Service. On any screen it doesn't recognize it stops and leaves the composer open (`needs_manual` → the Airtable row is marked `failed`).

Use `--no-auto-post` (agent) or omit `--auto-post` (tiktok-post) to only open the composer / push to the gallery and finish manually. Button labels and the package name are constants at the top of `tiktok_poster.py` — tune them when TikTok's UI shifts.

Use `--auto-post` at your own risk.
