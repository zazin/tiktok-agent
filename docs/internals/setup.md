# Internals — Setup, credentials & cross-repo coupling

Maintainer-facing. For the publisher/MQTT contracts see the parent [`docs/`](../README.md).

## Runtime prerequisites (not Python)

- `adb` on PATH (`brew install android-platform-tools`)
- Phone connected, USB debugging authorized, **unlocked**, TikTok installed + logged in

## Credentials (`.env`)

`.env` (gitignored, auto-loaded by every CLI via `env_loader.load_env()` at startup)
needs:

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
HIVEMQ_COMMENT_TOPIC=tiktok/comments               # optional, comment work topic
HIVEMQ_COMMENT_STATUS_TOPIC=tiktok/comment-status  # optional, comment outcomes
HIVEMQ_COMMENT_CLIENT_ID=tiktok-commenter          # optional, its own persistent session

# Comment-reader (tiktok-comment-reader)
HIVEMQ_COMMENT_READ_TOPIC=tiktok/comments-read     # optional, read-job topic
HIVEMQ_COMMENT_LIST_TOPIC=tiktok/comments-list     # optional, scraped-list output topic
HIVEMQ_COMMENT_READ_CLIENT_ID=tiktok-comment-reader # optional, its own persistent session
COMMENT_READ_MAX=10                                # optional, default comments/post cap

# Local testing isolation (all consumers) — prepended verbatim to every topic AND
# client-id, so a test run uses a fully isolated queue + session. Unset in prod.
HIVEMQ_TOPIC_PREFIX=test/             # optional, e.g. "test/" → test/tiktok/posts

# Duplicate-message guard (poster + commenter)
DEDUP_TTL=86400                       # optional, dedup window in seconds (default 1 day; --dedup-ttl wins; --no-dedup disables)

# ImageKit — still needed to download images (and for --source imagekit)
IMAGEKIT_PRIVATE_KEY=private_...
IMAGEKIT_URL_ENDPOINT=https://ik.imagekit.io/your_id

# Logging → Elasticsearch (Serverless) — ON by default. See logging.md.
ES_LOG_URL=https://your-project.es.us-east-1.aws.elastic.cloud
ES_API_KEY=...                        # base64 ApiKey string (used verbatim in the auth header)
# ES_LOG_ENABLED=1                    # optional, set 0/false to disable ES shipping (console-only)
# ES_LOG_INDEX=logs-tiktok_agent-default  # optional, data-stream name
# ES_LOG_LEVEL=INFO                   # optional, min level shipped
# ES_LOG_BATCH_SIZE / ES_LOG_FLUSH_INTERVAL / ES_LOG_QUEUE_MAX / ES_LOG_TIMEOUT  # optional tuning
```

Real environment variables always win over `.env`. HiveMQ auth is TLS +
username/password (port 8883); ImageKit auth is HTTP Basic with the private key as
username and an empty password. (Image downloads hit the public ImageKit CDN URL and
need no auth.)

The `HIVEMQ_CLIENT_ID` must be **stable** — it keys the persistent session that holds
the offline backlog. Running `tiktok-hivemq` (inspect) reuses the same client-id, so
doing so while the agent watches briefly bumps the agent off the broker until its next
reconnect.

## Cross-repo coupling (easy to break)

The server half lives in [tiktok-pipeline](https://github.com/zazin/tiktok-pipeline).
The two repos are coupled **only** by the MQTT message contracts below — changing any of
these in one repo silently breaks the other. The full publisher-facing schemas live in
[`docs/post-image.md`](../post-image.md), [`docs/comment-on-post.md`](../comment-on-post.md),
and [`docs/read-comments.md`](../read-comments.md).

- **Post contract (primary):** the pipeline publishes (QoS 1, retained false) and the
  agent drains JSON on the work topic with fields `id`, `Caption`, `Description`,
  `ImageURL`, `ImagePath`, `CreatedAt`, and optional `Account` (a TikTok `@handle`). `id`
  is the required correlation key echoed back in status messages (`{id, status, ts}` on
  the status topic). Both sides must agree on the topic names (`HIVEMQ_TOPIC` /
  `HIVEMQ_STATUS_TOPIC`) and the QoS-1/persistent-session semantics.
- **Comment contract (`tiktok-commenter`):** a producer publishes (QoS 1) JSON
  `{"PostURL": "...", "Comment": "...", "Account": "@handle", "ReplyTo": {"author":
  "@u", "text": "..."}}` to `tiktok/comments` (`HIVEMQ_COMMENT_TOPIC`); the agent reports
  `{PostURL, status, ts}` on `tiktok/comment-status` (`HIVEMQ_COMMENT_STATUS_TOPIC`). No
  `id` field; `Account` and `ReplyTo` are optional (`ReplyTo` present = post a reply).
- **Comment-reader contract:** the backend publishes read-jobs `{"PostURL": "...",
  "max": 10}` (QoS 1) to `tiktok/comments-read` (`HIVEMQ_COMMENT_READ_TOPIC`); the device
  publishes the scraped list `{"PostURL", "comments": [{"author", "text"}], "count",
  "ts"}` to `tiktok/comments-list` (`HIVEMQ_COMMENT_LIST_TOPIC`). The correlation key
  tying a listed comment to its reply-job is `(author + text)` — TikTok exposes no stable
  comment id.
- **ImageKit `ImageURL`:** the agent downloads the image from the public CDN URL the
  pipeline put in each message.
- **Legacy ImageKit coupling (`--source imagekit` only):** filename convention
  (`tiktok_<ts>.<ext>` on phone vs `tiktok_<ts>_<unique>.<ext>` on ImageKit, matched by
  `caption_from_imagekit`), `customMetadata` keys `caption`/`description`, and the
  adb/Basic auth scheme mirroring the pipeline's uploader.

`HIVEMQ_TOPIC_PREFIX` prepends a prefix to the topics + client-id for local-test
isolation — keep it unset in prod on both repos.
