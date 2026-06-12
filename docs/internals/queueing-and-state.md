# Internals — Queueing, state machine & dedup

Maintainer-facing. For the publisher/MQTT contracts see the parent [`docs/`](../README.md).

## State machine (HiveMQ source, default)

The broker's persistent session is the dedup. MQTT is push, so the two run modes
differ:

- **`--watch` → `watch(handler)` (event-driven, the normal mode):** holds one
  persistent subscription and dispatches each message to a handler the instant it
  arrives — **no poll interval**. `on_message` enqueues to a single worker thread (so
  the network thread stays responsive for keepalive while a post runs); the handler's
  return value drives the ack.
- **`--once` / `--catch-up` → `list_pending()` (one-shot drain):** connect → drain
  every queued QoS-1 message (publish order, oldest first) → process → ack the done
  ones → `close()` disconnects. The drain ends after a short idle window (`DRAIN_IDLE`,
  no new message for ~2s) capped by `DRAIN_HARD_CAP`.

Both modes use `manual_ack=True`, so paho sends the PUBACK only when the agent calls
`update_status()`. After acting on a message:

- `post()` returns `"posted"` → publish `posted` + **ack** (message dropped).
- `post()` returns `"needs_manual"`/`"composer_open"`/`"wrong_account"` or raises →
  the message's spool file is **kept** (its status recorded) and the broker message
  gets `failed` + **ack** (`failed` drops the broker copy just like a "posted" ack so
  it won't loop; on `needs_manual` TikTok is **force-stopped** — not left open — and
  the item stays spooled for `--retry`; `wrong_account` means the target account
  couldn't be confirmed active so **nothing was posted** (TikTok is force-stopped too)
  — re-attempt with `--retry` once the account is switchable). An empty `ImageURL` is
  acked-and-dropped **without** spooling (nothing to retry).
- `--no-auto-post` → message is **never acked** (pushed to phone only); on the next
  reconnect the broker redelivers it, so it is re-pushed until it is actually posted.

The persistent client is a module-level singleton in `hivemq_source.py`;
`_process_hivemq`/`_catch_up_hivemq` wrap their drain in `try/finally: close()`, and
`watch()` does the same around its blocking loop, so every run ends with a clean
disconnect.

`--catch-up` drains every queued message and marks it `posted` (publish + ack)
without posting. **Run it once before the first `--watch`** — auto-post is ON by
default, so otherwise the first poll posts the whole backlog.

## Local spool dir (`queue_posts/`)

Gitignored, override with `--store-dir`. The broker holds the live queue, but **every
received message is mirrored to its own JSON file the instant it arrives**
(`local_store.store`, written before the phone is touched), so nothing is lost if the
broker then drops it. One file per item (`<slug>-<hash>.json`, holding
`{key, payload: fields, status, error, attempts, ts}`).

On a successful post the file is **deleted** (`local_store.remove`); any other outcome
leaves it on disk — so `queue_posts/` accumulates exactly the items still needing
attention. `tiktok-agent --retry` re-runs download→push→post for each surviving file
**talking only to local disk** (no HiveMQ): on `posted` the file is deleted, otherwise
its status/attempts are updated. Run `--retry` by hand once the phone/UI is ready.

Trade-off: a crash between a successful post and the broker ack leaves the broker
message unacked, so it is redelivered and could be re-posted later (the local file is
already gone, so the redelivery just re-spools and re-posts).

The commenter mirrors this with `queue_comments/` (keyed by `PostURL`, payload
`{post_url, comment, account}`). Terminal outcomes delete the file — `commented`
(success) **and** `skipped_non_ascii` (can never be typed, so retrying is pointless);
`needs_manual`/`failed`/`wrong_account` keep it. `--dry-run` does **not** spool. The
reader is stateless (no spool, no `--retry`).

## Duplicate-message guard (`core/dedup_store.py`, default ON, ~1-day window)

The pipeline occasionally **republishes the same work message**; left unchecked the
device would post/comment twice. `dedup_store` keeps a small JSON of every item it
**actually completed** — `dedup_posts.json` keyed by post **`id`**,
`dedup_comments.json` keyed by `content_key(PostURL, Comment)` (a sha1, so a
*different* comment on the same post isn't blocked) — both gitignored.

On each received message, **before touching the phone**, a key seen within the TTL is
**acked-and-dropped without acting** and without spooling; the agent re-publishes the
already-achieved status (`posted`/`commented`) so **no new status string / no
cross-repo contract change** is introduced.

The key is recorded only at **success** (`posted`/`commented`) — never on receive —
which is deliberate: the redelivery paths **`--no-auto-post`** (never acked → broker
redelivers the same id) and **`--retry`** (local re-attempt of *failed* items) never
reach success, so they are **never deduped** and keep working; `--dry-run` (commenter)
likewise neither checks nor records. A crash after the success-record but before the
broker ack is covered too: the redelivered copy is recognized and dropped. `record()`
prunes entries older than the TTL on every write, so the files self-trim.

Window is `--dedup-ttl SECONDS` → `$DEDUP_TTL` → `86400` (1 day); disable with
**`--no-dedup`** (or `--dedup-ttl 0`); relocate with `--dedup-store` (per-CLI) or
`--posts-dedup-store`/`--comments-dedup-store` (`tiktok-watch-all`). Comment-reads are
**not** deduped (re-reading is idempotent). The legacy ImageKit source has its own
dedup (`agent_state.json`, below) and is unaffected.

## ImageKit source (`--source imagekit`, legacy)

`agent_state.json` (gitignored) is `{"processed": {fileId: {name, status, ts, ...}}}`.
An image is processed exactly once: anything whose `fileId` is already a key is
skipped. State is saved **after each item** so a crash mid-batch doesn't reprocess
completed work. `status` values: `posted`, `needs_manual`, `pushed`, `failed`,
`catch-up`. New images are processed **oldest-first** (`reversed(list_images())`, which
returns newest-first). Here `--catch-up` writes `status: "catch-up"` entries for the
folder.
