# tiktok-agent — integration docs

This folder is the **contract reference for publishers** — any backend that wants
the device-side agent to do work by sending it MQTT messages.

The agent has **no HTTP API**. It is driven entirely by **HiveMQ (MQTT)**: a
publisher drops a message on a work topic, the agent (running on a computer with an
Android phone attached) performs the action on TikTok over adb, and reports the
outcome on a status topic. HiveMQ is the queue and the source of truth — there is
no database or REST endpoint in between.

## Contracts — one file per feature

Each file is self-contained (connection/auth, delivery semantics, the work-message
schema, and the status message) — read only the one for the feature you publish.

- **[post-image.md](post-image.md)** — post an image to TikTok via `tiktok/posts`.
- **[comment-on-post.md](comment-on-post.md)** — comment on an existing post via
  `tiktok/comments`.

## At a glance

| Feature | Doc | Publish to | Message | Agent reports on |
|---------|-----|-----------|---------|------------------|
| **Post an image** | [post-image.md](post-image.md) | `tiktok/posts` | `{ id, Caption, Description, ImageURL, ImagePath, CreatedAt }` | `tiktok/status` → `{ id, status, ts }` |
| **Comment on a post** | [comment-on-post.md](comment-on-post.md) | `tiktok/comments` | `{ PostURL, Comment }` | `tiktok/comment-status` → `{ PostURL, status, ts }` |

Both are **QoS 1, retained = false**. See each file for required vs. optional fields
and the list of `status` values.

> ⚠️ These contracts are shared across two repositories. Renaming a field,
> changing a topic name, or dropping to QoS 0 silently breaks the agent. Treat any
> change here as a breaking API change and coordinate both sides.
