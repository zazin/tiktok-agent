# TikTok Agent

The device-side half of the TikTok system. It runs on a **computer with an Android phone connected via adb**, watches the ImageKit `/tiktok` folder that [tiktok-pipeline](https://github.com/zazin/tiktok-pipeline) uploads to, and for each new image: downloads it, pushes it into the phone's gallery, and **auto-posts it to TikTok** (caption + description pulled from the image's ImageKit metadata) by driving the on-device UI over adb.

Auto-post is **on by default** — run `tiktok-agent --watch` and it posts new images hands-free. ImageKit itself is the queue — there is no Firebase or server in between.

```
tiktok-pipeline (server)            ImageKit /tiktok            tiktok-agent (this, computer + adb + phone)
  generate → upload  ───────────►  folder of images  ◄────────  poll list (Media Mgmt API)
                                                                 → download new (dedup by fileId)
                                                                 → adb push to gallery
                                                                 → auto-post to TikTok (caption + description)
```

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Orchestrator — poll → download → push → auto-post → record (`--catch-up` to seed state) |
| `imagekit_source.py` | List + download images from the ImageKit folder |
| `adb_pusher.py` | Push an image to the phone gallery over adb (+ media scan) |
| `tiktok_poster.py` | Best-effort auto-post via adb UI automation (also the `tiktok-post` CLI) |
| `env_loader.py` | Zero-dependency `.env` loader |
| `agent_state.json` | Local record of processed `fileId`s (gitignored) |

## Quick Start

Managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync

# .env (auto-loaded):
#   IMAGEKIT_PRIVATE_KEY=private_...
#   IMAGEKIT_URL_ENDPOINT=https://ik.imagekit.io/your_id

# Always-on: watch the queue and auto-post every new image, hands-free
uv run tiktok-agent --catch-up        # first: mark the existing backlog as seen
uv run tiktok-agent --watch           # then: auto-posts each NEW image as it arrives

# One pass (also auto-posts new images by default)
uv run tiktok-agent --once

# Push to the phone only, don't post
uv run tiktok-agent --watch --no-auto-post

# Just inspect the ImageKit queue
uv run tiktok-source --folder /tiktok

# Post an image that is ALREADY on the phone (no download)
uv run tiktok-post --list                                   # see gallery images
uv run tiktok-post /sdcard/Pictures/foo.jpg                 # open composer (Phase 1)
uv run tiktok-post /sdcard/Pictures/foo.jpg --auto-post --from-imagekit   # caption+desc from ImageKit
uv run tiktok-post /sdcard/Pictures/foo.jpg --auto-post --caption "..." --description "..."
```

**Auto-post defaults to ON.** `tiktok-agent --once`/`--watch` will post; use `--no-auto-post`
for push-only. Because it posts every *unseen* image, run `tiktok-agent --catch-up` once first
to mark the current backlog as seen — otherwise the first run posts all of it.

**Caption + description** are read from the image's ImageKit custom metadata and combined into
TikTok's single text field (caption first, description on the next line). `tiktok-post` can do the
same lookup with `--from-imagekit`, or you can pass `--caption`/`--description` explicitly.

Typing uses `adb input text`, which can't enter emoji — emoji are stripped (text + hashtags
kept); the full emoji caption still lives in the ImageKit metadata.

## Requirements

- [uv](https://docs.astral.sh/uv/), Python 3.10+ (no third-party deps — stdlib only)
- `adb` (`brew install android-platform-tools`)
- An Android phone connected with **USB debugging** authorized, **unlocked**, with **TikTok installed and logged in**
- ImageKit credentials (same account the pipeline uploads to)

## Auto-post caveat

Auto-post is **on by default** and **publishes real public posts** to the logged-in TikTok account. `tiktok_poster.py` drives TikTok's UI over adb in two phases:

1. **Phase 1:** a `SEND` intent opens TikTok's composer with the image attached. Reliable.
2. **Phase 2:** dumps the UI tree and taps Foto → Next → Post by label, typing the caption first. **Brittle** (breaks when TikTok changes its UI) and potentially against TikTok's Terms of Service. On any screen it doesn't recognize it stops and leaves the composer open (`needs_manual`).

Use `--no-auto-post` (agent) or omit `--auto-post` (tiktok-post) to only open the composer / push to the gallery and finish manually. Button labels and the package name are constants at the top of `tiktok_poster.py` — tune them when TikTok's UI shifts.

Use `--auto-post` at your own risk.
