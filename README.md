# TikTok Agent

The device-side half of a two-repo TikTok system. It runs on a **computer with an
Android phone attached over adb**, drains a **HiveMQ work topic** that
[tiktok-pipeline](https://github.com/zazin/tiktok-pipeline) publishes to, and for each
queued message downloads the image, pushes it into the phone gallery, **auto-posts it to
TikTok** by driving the on-device UI over adb, then reports the outcome and acks the
message.

```
tiktok-pipeline (server)          HiveMQ (MQTT)             tiktok-agent (this, computer + adb + phone)
  generate → upload to ImageKit     work topic         ◄──────  drain queued messages (persistent QoS-1, oldest first)
  → publish message  ─────────────► (Caption, Desc,            → download image from msg's ImageURL (ImageKit CDN)
    {id, ImageURL, ...}              ImageURL, id)              → adb push to gallery
                                    status topic       ──────►  → auto-post to TikTok
                                     {id, status}              → publish status + ack the message
```

**HiveMQ is the queue / source of truth** — no database or REST API in between. The
agent discovers work by draining the topic and reports back on a status topic; a
**persistent QoS-1 session** queues messages while the device is offline and a message is
**acked only after it acts**, so anything unposted is redelivered. Besides posting, two
more independent consumers run the same way on their own topics: **comment-on-post**
(`tiktok-commenter`) and **comment-reader** (`tiktok-comment-reader`). A legacy
**ImageKit-folder queue** survives behind `--source imagekit`.

> **Publishing from a backend?** The MQTT contracts — connection/auth, delivery
> semantics, every topic, the message schema and status values — live in
> [`docs/`](docs/). This README does not restate them.
>
> **Changing the code?** Architecture, the `.env` reference, and the brittle
> device-automation flows live in [`docs/internals/`](docs/internals/) (indexed from
> [CLAUDE.md](CLAUDE.md)). CLI entry points sit at the top level; shared library modules
> live in `core/` — see [architecture.md](docs/internals/architecture.md).

## Requirements

- [uv](https://docs.astral.sh/uv/), Python 3.10+ (one dependency, `paho-mqtt`; everything else is stdlib)
- `adb` (`brew install android-platform-tools`)
- An Android phone connected with **USB debugging** authorized, **unlocked**, with **TikTok installed and logged in**
- HiveMQ broker credentials and ImageKit access to the public image URLs

## Setup

```bash
uv sync
```

Create a gitignored `.env` with at least your HiveMQ broker credentials (`HIVEMQ_HOST`,
`HIVEMQ_USERNAME`, `HIVEMQ_PASSWORD`) and ImageKit keys (`IMAGEKIT_PRIVATE_KEY`,
`IMAGEKIT_URL_ENDPOINT`). Every key — topic/client-id overrides, the dedup window, the
test-isolation prefix, and Elasticsearch logging — is documented in
[docs/internals/setup.md](docs/internals/setup.md).

## Run

**Auto-post is on by default** and **publishes real public posts** to the logged-in
account. Because a watch posts *every* queued message, run `--catch-up` once first to
skip the existing backlog (it drains and marks them `posted` without posting), then start
the watch:

```bash
uv run tiktok-agent --catch-up     # first: drain the existing backlog as posted (skip it)
uv run tiktok-agent --watch        # then: event-driven, auto-posts each NEW message instantly
uv run tiktok-agent --once         # one pass over the current backlog, then exit
uv run tiktok-agent --watch --no-auto-post   # push to the phone only, leave messages pending
```

Manual / inspection helpers:

```bash
uv run tiktok-hivemq                                        # peek the HiveMQ backlog (no ack)
uv run tiktok-post --list                                  # list images already on the phone
uv run tiktok-post /sdcard/Pictures/foo.jpg --auto-post --from-imagekit       # post an on-phone image (caption from ImageKit)
uv run tiktok-post /sdcard/Pictures/foo.jpg --auto-post --caption "..." --description "..."
uv run tiktok-post /sdcard/Pictures/foo.jpg --dry-run --from-imagekit         # type the fields but DON'T tap Post (inspect on device)
uv run tiktok-profile                                      # print the active TikTok @handle
uv run tiktok-agent --once --source imagekit --folder /tiktok   # legacy: poll the ImageKit folder instead
```

### Everything at once (`tiktok-watch-all`)

The poster, commenter and comment-reader are three independent consumers. They can run as
three separate processes, or **`tiktok-watch-all` runs all three watchers in one process**
(each on its own thread + persistent session), so one command drains every topic:

```bash
uv run tiktok-watch-all --catch-up    # first: drain EVERY backlog WITHOUT acting (skip it)
uv run tiktok-watch-all               # then: watch posts + comments + reads at once (Ctrl-C stops all)
uv run tiktok-watch-all --no-reads    # skip a feature (also --no-posts / --no-comments)
```

They all drive the **one** attached phone, and uiautomator can't run in parallel on a
single device — so a `core/device_lock.py` flock serializes the device flows: when the
phone is busy the next consumer waits its turn rather than interleaving. Running the
watchers separately or together is therefore safe.

### Comment & read (`tiktok-commenter`, `tiktok-comment-reader`)

Two more event-driven consumers, each on its own topic and client-id, started just like
the poster (`--catch-up` then `--watch`, plus `--once` / `--dry-run` / `--retry`):

```bash
uv run tiktok-commenter --catch-up && uv run tiktok-commenter --watch        # comment on / reply to posts
uv run tiktok-comment-reader --catch-up && uv run tiktok-comment-reader --watch   # scrape a post's comments
```

What each one expects in its message and reports back is the **publisher contract** — see
[docs/comment-on-post.md](docs/comment-on-post.md) and
[docs/read-comments.md](docs/read-comments.md).

## Auto-post caveat (read before `--auto-post`)

Auto-posting drives TikTok's UI over adb, which is **brittle** (it breaks when TikTok
changes its UI) and potentially against TikTok's Terms of Service. It runs in two phases:
a reliable `SEND` intent opens the composer with the image attached, then a best-effort
pass taps Next → Post by label and types the caption/description. On any screen it doesn't
recognize it **stops** (reported `needs_manual`) rather than tapping blindly, and it
**force-stops TikTok on the way out** so the app is never left half-open; the item stays
spooled for `--retry`. Use `--no-auto-post` (or omit `--auto-post` on `tiktok-post`) to
only open the composer and finish by hand.

The UI labels, the two-phase flow, and the title/description field handling are documented
in [docs/internals/device-automation.md](docs/internals/device-automation.md) — re-calibrate
there when TikTok's UI shifts. **Use `--auto-post` at your own risk.**
