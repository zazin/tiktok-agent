# CLAUDE.md

Guidance for Claude Code when **changing code** in this repository. This file is the
maintainer index: architecture invariants, where things live, and the gotchas to respect
when editing. It deliberately does **not** repeat usage — for what the system does and how
to run it (install, commands, message contract, caveats) read [README.md](README.md), and
for the MQTT schemas a backend publishes read [docs/](docs/).

## Invariants to preserve when editing

This is the device-side half of a two-repo system (server half:
[tiktok-pipeline](https://github.com/zazin/tiktok-pipeline)). It drains a HiveMQ work
topic, posts to TikTok over adb, and acks. The non-obvious constraints a change must not
break:

- **HiveMQ is the queue / source of truth** — no server, database, or table between the
  two halves. Durability rests on a **persistent QoS-1 session** (stable client-id,
  `clean_session=False`) plus **manual ack only after the post succeeds**: anything
  unposted (failed, `--no-auto-post`, or a crash) is redelivered. Don't drop QoS or
  switch to auto-ack.
- **The cross-repo message contract** (`id`, `Caption`, `Description`, `ImageURL`,
  `ImagePath`, `CreatedAt`) couples the two repos. Renaming a field, changing a topic, or
  dropping to QoS 0 silently breaks tiktok-pipeline — coordinate first. See
  [setup.md](docs/internals/setup.md#cross-repo-coupling-easy-to-break).
- **Three independent consumers share one phone** — poster, comment-on-post
  (`tiktok-commenter`), comment-reader (`tiktok-comment-reader`), each with its own topic
  + client-id. A legacy **ImageKit folder queue** survives behind `--source imagekit`.

## Dev environment

Managed with [uv](https://docs.astral.sh/uv/). One third-party dependency, `paho-mqtt`
(stdlib has no MQTT client); everything else is stdlib. There are **no tests, linter, or
build step** configured. The wheel `include` list in `pyproject.toml` must be updated by
hand if a new **top-level** `.py` module is added (modules **inside `core/`** are picked
up automatically).

`tiktok_commenter.py` doubles as a direct-run helper for a single post (no MQTT) — handy
when calibrating the comment UI:

```bash
uv run python tiktok_commenter.py <url> "<comment>"                                              # top-level comment
uv run python tiktok_commenter.py <url> "<reply>" --reply-to-author @who --reply-to-text "..."   # reply to a comment
```

## Documentation map

Deep dives live in `docs/`:

**Maintainer internals** (`docs/internals/` — read these before changing code):
- [architecture.md](docs/internals/architecture.md) — module flow tables, `core/`
  layout, the `run_adb` chokepoint, the cross-process device lock, shared vs.
  feature-specific.
- [queueing-and-state.md](docs/internals/queueing-and-state.md) — MQTT watch/drain run
  modes, manual-ack outcomes, the local spool dir, `--catch-up`, the duplicate-message
  guard, the legacy ImageKit state file.
- [device-automation.md](docs/internals/device-automation.md) — **the brittle parts**:
  auto-post two phases, caption handling (separate title + description fields,
  90 / 4000-char caps), multi-account switching, comment-on-post + comment-reader UI
  calibration facts.
- [logging.md](docs/internals/logging.md) — stdlib logging → Elasticsearch Serverless.
- [setup.md](docs/internals/setup.md) — runtime prerequisites, `.env` credentials,
  cross-repo coupling.

**Publisher contracts** (`docs/` — the MQTT message schemas a backend publishes):
[README.md](docs/README.md), [post-image.md](docs/post-image.md),
[comment-on-post.md](docs/comment-on-post.md), [read-comments.md](docs/read-comments.md).

For the full command reference (every `uv run` entry point and flag), see
[README.md](README.md).

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
