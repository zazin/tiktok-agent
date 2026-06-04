# TikTok Agent

The device-side half of the TikTok system. It runs on a **computer with an Android phone connected via adb**, watches the ImageKit `/tiktok` folder that [tiktok-pipeline](https://github.com/zazin/tiktok-pipeline) uploads to, and for each new image: downloads it, pushes it into the phone's gallery, and (optionally) auto-posts it to TikTok by driving the on-device UI over adb.

ImageKit itself is the queue — there is no Firebase or server in between.

```
tiktok-pipeline (server)            ImageKit /tiktok            tiktok-agent (this, computer + adb + phone)
  generate → upload  ───────────►  folder of images  ◄────────  poll list (Media Mgmt API)
                                                                 → download new (dedup by fileId)
                                                                 → adb push to gallery
                                                                 → optional auto-post to TikTok
```

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Orchestrator — poll → download → push → (auto-post) → record |
| `imagekit_source.py` | List + download images from the ImageKit folder |
| `adb_pusher.py` | Push an image to the phone gallery over adb (+ media scan) |
| `tiktok_poster.py` | Best-effort auto-post via adb UI automation |
| `env_loader.py` | Zero-dependency `.env` loader |
| `agent_state.json` | Local record of processed `fileId`s (gitignored) |

## Quick Start

Managed with [uv](https://docs.astral.sh/uv/).

```bash
uv sync

# .env (auto-loaded):
#   IMAGEKIT_PRIVATE_KEY=private_...
#   IMAGEKIT_URL_ENDPOINT=https://ik.imagekit.io/your_id

# One pass: download + push new images to the phone (no posting)
uv run tiktok-agent --once

# Keep watching every 60s
uv run tiktok-agent --watch --interval 60

# Also attempt to auto-post to TikTok (brittle, opt-in)
uv run tiktok-agent --once --auto-post

# Just inspect the ImageKit queue
uv run tiktok-source --folder /tiktok
```

## Requirements

- [uv](https://docs.astral.sh/uv/), Python 3.10+ (no third-party deps — stdlib only)
- `adb` (`brew install android-platform-tools`)
- An Android phone connected with **USB debugging** authorized, **unlocked**, with **TikTok installed and logged in**
- ImageKit credentials (same account the pipeline uploads to)

## Auto-post caveat

`tiktok_poster.py` drives TikTok's UI over adb in two phases:

1. **Phase 1 (always):** a `SEND` intent opens TikTok's composer with the image attached. Reliable; you tap Post.
2. **Phase 2 (`--auto-post`):** repeatedly dumps the UI tree and taps Next/Post by label. **Brittle** (breaks when TikTok changes its UI) and potentially against TikTok's Terms of Service. On any screen it doesn't recognize it stops and leaves the composer open (`needs_manual`).

Use `--auto-post` at your own risk.
