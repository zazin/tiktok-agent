# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The device-side half of a two-repo TikTok system. The **server** half lives in a
separate repo, [tiktok-pipeline](https://github.com/zazin/tiktok-pipeline): it
generates images, uploads them to ImageKit, and **publishes one MQTT message per post**
to a **HiveMQ work topic** (caption, description, the public ImageKit `ImageURL`, plus a
correlation `id`). **This repo** runs on a computer with an Android phone attached via
adb, drains that topic, and for each queued message: downloads the image → pushes it
into the phone gallery → auto-posts it to TikTok by driving the on-device UI over adb →
publishes the outcome (`posted`/`failed`) to a status topic and **acks** the message.

**HiveMQ is the queue / source of truth.** There is no server, database, or table
between the two halves — the agent discovers work by draining the work topic and reports
back by publishing to the status topic. Queue durability comes from a **persistent QoS-1
MQTT session** (stable client-id, `clean_session=False`): the broker queues messages
while the device is offline and redelivers on reconnect, and a message is **acked only
after it posts**, so anything unposted (failed, `--no-auto-post`, or a crash) is
redelivered. The two repos are coupled by the **message contract** (fields `id`,
`Caption`, `Description`, `ImageURL`, `ImagePath`, `CreatedAt`) — see
[docs/internals/setup.md](docs/internals/setup.md#cross-repo-coupling-easy-to-break).

Besides posting there are two more independent consumers: **comment-on-post**
(`tiktok-commenter`) and **comment-reader** (`tiktok-comment-reader`), each with its own
HiveMQ topic + client-id. A legacy **ImageKit folder queue** is still available via
`--source imagekit`.

## Documentation map

This file is the lean index. Deep dives live in `docs/`:

**Maintainer internals** (`docs/internals/` — read these before changing code):
- [architecture.md](docs/internals/architecture.md) — module flow tables, `core/`
  layout, the `run_adb` chokepoint, the cross-process device lock, shared vs.
  feature-specific.
- [queueing-and-state.md](docs/internals/queueing-and-state.md) — MQTT watch/drain run
  modes, manual-ack outcomes, the local spool dir, `--catch-up`, the duplicate-message
  guard, the legacy ImageKit state file.
- [device-automation.md](docs/internals/device-automation.md) — **the brittle parts**:
  auto-post two phases, caption handling (incl. the 90-char cap), multi-account
  switching, comment-on-post + comment-reader UI calibration facts.
- [logging.md](docs/internals/logging.md) — stdlib logging → Elasticsearch Serverless.
- [setup.md](docs/internals/setup.md) — runtime prerequisites, `.env` credentials,
  cross-repo coupling.

**Publisher contracts** (`docs/` — the MQTT message schemas a backend publishes):
[README.md](docs/README.md), [post-image.md](docs/post-image.md),
[comment-on-post.md](docs/comment-on-post.md), [read-comments.md](docs/read-comments.md).

## Commands

Managed with [uv](https://docs.astral.sh/uv/). One third-party dependency, `paho-mqtt`
(stdlib has no MQTT client); everything else is stdlib. There are **no tests, linter, or
build step** configured. The wheel `include` list in `pyproject.toml` must be updated by
hand if a new **top-level** `.py` module is added (modules **inside `core/`** are picked
up automatically).

```bash
uv sync

# Watch ALL features from one process (posts + comments + comment-reads):
uv run tiktok-watch-all --catch-up   # drain EVERY backlog WITHOUT acting (run once first)
uv run tiktok-watch-all              # event-driven: watch all three topics at once (Ctrl-C stops all)
uv run tiktok-watch-all --no-reads   # skip a feature (also --no-posts / --no-comments)
uv run tiktok-watch-all --retry      # one-shot: re-attempt spooled posts + comments, then exit
uv run tiktok-watch-all --clear      # one-shot: delete spooled posts + comments (--failed-only / --no-posts / --no-comments)

# Poster:
uv run tiktok-agent --catch-up     # drain current backlog WITHOUT posting (run once first)
uv run tiktok-agent --watch        # event-driven: auto-posts each message instantly
uv run tiktok-agent --once         # drain the current backlog once and exit
uv run tiktok-agent --watch --no-auto-post   # push to phone only, leave messages unacked
uv run tiktok-agent --retry                  # re-attempt posts still in queue_posts/ (no HiveMQ)
uv run tiktok-agent --clear                  # delete spooled posts WITHOUT re-attempting (--failed-only)
uv run tiktok-agent --watch --no-dedup       # disable the ~1-day duplicate-id guard (tune with --dedup-ttl SECONDS)
uv run tiktok-agent --once --source imagekit # legacy: poll the ImageKit folder instead

# Inspect / manual:
uv run tiktok-hivemq                         # peek the HiveMQ post backlog (no ack)
uv run tiktok-source --folder /tiktok        # inspect the legacy ImageKit queue
uv run tiktok-post --list                    # list images already on the phone
uv run tiktok-post /sdcard/Pictures/x.jpg --auto-post --from-imagekit   # post an on-phone image
uv run tiktok-post /sdcard/Pictures/x.jpg --dry-run --from-imagekit     # type caption but DON'T tap Post (inspect on device)
uv run tiktok-profile                        # print the active TikTok @handle
uv run tiktok-profile @target                # switch to a TikTok account

# Comment-on-post:
uv run tiktok-commenter --catch-up           # drain the comment backlog WITHOUT commenting (run once first)
uv run tiktok-commenter --watch              # event-driven: comment on each tiktok/comments message
uv run tiktok-commenter --once --dry-run     # drain once, log what it would comment, DON'T submit
uv run tiktok-commenter --retry              # re-attempt comments still in queue_comments/ (no HiveMQ)
uv run tiktok-commenter --clear              # delete spooled comments (--failed-only)

# Comment-reader:
uv run tiktok-comment-reader --catch-up      # drain the read-job backlog WITHOUT reading (run once first)
uv run tiktok-comment-reader --watch         # event-driven: scrape each tiktok/comments-read post
uv run tiktok-comment-reader --once --max 20 # drain once, scrape up to 20 comments/post, publish the lists
```

`tiktok_commenter.py` is also a direct-run helper for a single post (no MQTT):
```bash
uv run python tiktok_commenter.py <url> "<comment>"                                   # top-level comment
uv run python tiktok_commenter.py <url> "<reply>" --reply-to-author @who --reply-to-text "..."  # reply to a comment
```

## Where to make changes (quick map)

- **Any adb / device interaction** → `core/adb_pusher.py` (`run_adb`, the single
  chokepoint) and `core/tiktok_ui.py` (shared UI primitives).
- **TikTok UI broke** (a flow returns `needs_manual` / posts to the wrong place) → the
  constants block at the top of the relevant flow module (`tiktok_poster.py`,
  `tiktok_commenter.py`, `tiktok_profile.py`); re-calibrate against a real `uiautomator
  dump`. See [device-automation.md](docs/internals/device-automation.md).
- **MQTT / queue semantics** → `core/mqtt_queue.py` (shared); the thin per-feature
  wiring is `hivemq_source.py` / `core/comment_source.py` / `core/comment_read_source.py`.
- **Cross-repo message contract** → coordinate with tiktok-pipeline; see
  [setup.md](docs/internals/setup.md#cross-repo-coupling-easy-to-break). Renaming a
  field, changing a topic, or dropping to QoS 0 silently breaks the other repo.
- **Three watchers run as separate processes on one phone** but are safe together — the
  `core/device_lock.py` flock serializes their device flows. Don't nest those flows in
  one process (self-deadlock).
