# Contract — Read a post's comments (`tiktok/comments-read` → `tiktok/comments-list`)

How a backend reads the **existing comments** on a TikTok post so it can generate
replies. The backend has **no TikTok API** — only the device can see a post's comments,
by scraping the on-screen UI. So this is a request/response over MQTT:

1. Backend **publishes a read-job** (a post URL) to `tiktok/comments-read`.
2. The **comment-reader** (`tiktok-comment-reader`, a separate consumer with its own
   client-id) opens the post on the phone, scrapes up to `max` **top-level** comments,
   and **publishes the list** to `tiktok/comments-list`.
3. Backend consumes the list, generates a reply per comment, and sends each back via
   the [comment-on-post](./comment-on-post.md) contract using `ReplyTo`.

This is the **read half** of the comment-reply loop. It is **read-only** (it never types
or submits) and **idempotent** (re-reading a post just re-publishes its current
comments). **No AI** lives here.

The agent has **no HTTP API** — this is MQTT only.

- [Connection & auth](#connection--auth)
- [Delivery semantics](#delivery-semantics)
- [Read-job message](#read-job-message)
- [Comment-list message](#comment-list-message)
- [End-to-end example](#end-to-end-example)
- [What is and isn't scraped](#what-is-and-isnt-scraped)
- [Correlation & dedup (backend's job)](#correlation--dedup-backends-job)
- [Breaking changes](#breaking-changes)

---

## Connection & auth

Connect to the **same HiveMQ Cloud broker** as everything else.

| Setting | Value |
|---------|-------|
| Protocol | MQTT **v3.1.1** |
| Transport | **TLS** (system CA certs) |
| Host | your HiveMQ cluster, e.g. `xxxx.s1.eu.hivemq.cloud` |
| Port | **8883** |
| Auth | username + password |

The backend needs to **publish** read-jobs and **subscribe** to comment-lists. Use a
backend client-id that does **not** collide with the reader's
(`tiktok-comment-reader`) or the other consumers (`tiktok-agent`, `tiktok-commenter`),
or the broker will disconnect the colliding session.

---

## Delivery semantics

- **Publish read-jobs at QoS 1** with **`retain = false`**.
- The reader runs a **persistent QoS-1 session** (`clean_session = false`), so read-jobs
  published **while the device is offline are queued by the broker** and delivered on
  reconnect. Just publish — no client-side retry/buffer needed.
- The reader **acks a read-job only after it publishes the comment-list** (or an `error`
  result). A crash before the ack means the broker redelivers, so the post may be
  **read again** and the list **re-published** — **at-least-once, not exactly-once**.
  Because reading is read-only and idempotent, a duplicate list is harmless; key off
  `PostURL` and take the latest.
- The comment-list is published at **QoS 1**. Subscribe with a **persistent session**
  (your own stable client-id, `clean_session = false`) if you want lists buffered while
  your backend is down.

---

## Read-job message

- **Topic:** `tiktok/comments-read` (agent env `HIVEMQ_COMMENT_READ_TOPIC`)
- **QoS:** 1 · **retain:** false · **payload:** UTF-8 JSON object

| Field | Type | Required | Description |
|-------|------|:--------:|-------------|
| `PostURL` | string (URL) | **yes** | Full TikTok post URL, e.g. `https://www.tiktok.com/@user/video/<id>`. Correlation key for the comment-list. A message without a non-empty `PostURL` is **dropped**. |
| `max` | integer | no | Cap on how many top-level comments to scrape. Falls back to the reader's default (`COMMENT_READ_MAX`, default **10**) when omitted. TikTok's sheet is **top/relevance-sorted**, so the first N are the most-engaged comments. |

### Example

```json
{ "PostURL": "https://www.tiktok.com/@ardabily55/video/7631479842029997320", "max": 10 }
```

---

## Comment-list message

- **Topic:** `tiktok/comments-list` (agent env `HIVEMQ_COMMENT_LIST_TOPIC`)
- **QoS:** 1 · **retain:** false · **payload:** UTF-8 JSON object · **one per read-job**

| Field | Type | Description |
|-------|------|-------------|
| `PostURL` | string (URL) | The post these comments are from (echoes the read-job; the correlation key). |
| `comments` | array of objects | The scraped **top-level** comments, in screen order (≈ most-engaged first). Each is `{ "author": string, "text": string }`. Empty `[]` if there are none, or on a read error. |
| `count` | integer | `comments.length`, for convenience. |
| `ts` | integer | Unix epoch seconds when the list was published. |
| `error` | string | **Present only on failure** — the reader couldn't read the post. Then `comments` is `[]`. Values: `needs_manual` (a screen wasn't recognized) or `failed: <detail>` (couldn't open the post / adb error). |

Each comment object:

| Field | Type | Description |
|-------|------|-------------|
| `author` | string | The commenter's display handle (e.g. `user210320127`). Use this as `ReplyTo.author` when replying. |
| `text` | string | The comment body. Use a substring as `ReplyTo.text` to disambiguate. May be a **prefix** if TikTok truncated a long comment. |

### Example (success)

```json
{
  "PostURL": "https://www.tiktok.com/@ardabily55/video/7631479842029997320",
  "comments": [
    { "author": "user210320127", "text": "tapi ko aku makin plenger aja ya" },
    { "author": "Mas Akbarr", "text": "makin kesini makin ga guna ni aplikasi" }
  ],
  "count": 2,
  "ts": 1781055659
}
```

### Example (read failure)

```json
{
  "PostURL": "https://www.tiktok.com/@ardabily55/video/7631479842029997320",
  "comments": [],
  "count": 0,
  "ts": 1781055660,
  "error": "needs_manual"
}
```

---

## End-to-end example

```
backend ──▶ tiktok/comments-read   { "PostURL": "...", "max": 10 }
reader  ──▶ tiktok/comments-list   { "PostURL": "...", "comments": [{author,text}, ...], "count": N, "ts": ... }
backend  (generates a positive reply per comment)
backend ──▶ tiktok/comments        { "PostURL": "...", "Comment": "...", "ReplyTo": {author,text}, "Account": "@..." }
commenter──▶ tiktok/comment-status { "PostURL": "...", "status": "commented", "ts": ... }
```

The reply step is the [comment-on-post](./comment-on-post.md) contract — `ReplyTo`
carries the `author` (and optional `text`) straight from a `comments-list` entry.

---

## What is and isn't scraped

- **Top-level comments only.** Replies (nested under "Lihat N balasan" / shown indented)
  are **excluded** — you can only reply to top-level comments.
- **Text comments only.** Image / sticker / GIF comments have no text and are **skipped**
  (you can't generate a meaningful reply to them).
- **Capped.** At most `max` comments per post (default 10). The rest of the thread is not
  scraped — scale `max` per post if you need more, but replying to many comments on one
  post is slow (one phone, sequential) and looks spammy.
- **Possibly truncated.** A very long comment may come back as a visible **prefix**.
  Enough for reply context, but don't treat `text` as the verbatim full comment.

---

## Correlation & dedup (backend's job)

TikTok exposes **no stable comment id** in the UI, so the only key tying a listed comment
to the reply you send back is **`(author + text)`**. The reader and commenter are
**stateless** — all stateful policy lives on the backend:

- **Which posts to read** — the backend decides and publishes the read-jobs.
- **Which comments to reply to** — the backend filters the list (skip spam, your own
  account, etc.). The reader returns *everything* top-level; it does not judge.
- **Avoid double-replying** — the backend must remember the `(PostURL, author, text)` it
  already replied to and not re-issue a reply. Re-reading a post will list the same
  comments again (and now also your own replies are filtered out for being nested), so
  without backend dedup you would reply repeatedly.

---

## Breaking changes

Coordinate both repos for any of: renaming/removing/retyping a field above; changing the
topic names (`tiktok/comments-read` / `tiktok/comments-list`, configurable via
`HIVEMQ_COMMENT_READ_TOPIC` / `HIVEMQ_COMMENT_LIST_TOPIC` — both sides must agree);
publishing read-jobs at QoS 0 or with `retain = true`; or changing the `(author + text)`
correlation key. Adding a new **optional** read-job field or a new comment-object field is
non-breaking (treat unknown fields defensively).
