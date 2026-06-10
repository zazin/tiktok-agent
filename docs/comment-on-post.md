# Contract — Comment on a post (`tiktok/comments`)

How a backend asks **tiktok-agent** to leave a comment — or a **reply to an existing
comment** — on a TikTok post. Publish one MQTT message with the post URL and the
comment text; the agent opens the post by URL, types the comment, submits it, and
reports the outcome. **No AI** — the exact comment text comes from the message.

> To **reply** to an existing comment you need its author handle (and ideally a snippet
> of its text). Get those from the comment-reader pipeline — see
> [read-comments.md](./read-comments.md) — then echo them back here in `ReplyTo`.

The agent has **no HTTP API** — this is MQTT only.

- [Connection & auth](#connection--auth)
- [Delivery semantics](#delivery-semantics)
- [Work message](#work-message)
- [Status message](#status-message)
- [Breaking changes](#breaking-changes)

---

## Connection & auth

Connect to the **same HiveMQ Cloud broker** as the agent.

| Setting | Value |
|---------|-------|
| Protocol | MQTT **v3.1.1** |
| Transport | **TLS** (system CA certs) |
| Host | your HiveMQ cluster, e.g. `xxxx.s1.eu.hivemq.cloud` |
| Port | **8883** |
| Auth | username + password |

A publisher only needs to `PUBLISH` — no persistent session required. Use a unique
publisher client-id that does **not** collide with the agent's (`tiktok-commenter`),
or the broker will disconnect the agent.

---

## Delivery semantics

- **Publish at QoS 1** (at-least-once) with **`retain = false`**.
- The agent runs a **persistent QoS-1 session** (`clean_session = false`), so
  messages published **while the device is offline are queued by the broker** and
  delivered on reconnect. You do not need to retry or buffer — just publish.
- The agent **acks a message only after it acts on it**. A crash before the ack
  means the broker redelivers, so the agent may **act again** — this is
  **at-least-once, not exactly-once**. Make comment text safe to repeat; de-duplicate
  downstream if exact-once matters.

---

## Work message

- **Topic:** `tiktok/comments` (agent env `HIVEMQ_COMMENT_TOPIC`)
- **QoS:** 1 · **retain:** false · **payload:** UTF-8 JSON object

| Field | Type | Required | Description |
|-------|------|:--------:|-------------|
| `PostURL` | string (URL) | **yes** | Full TikTok post URL, e.g. `https://www.tiktok.com/@user/video/<id>`. Correlation key for the status message. A message without a non-empty `PostURL` is **dropped**. |
| `Comment` | string | **yes** | The exact comment text to submit. A message without a non-empty `Comment` is **dropped**. |
| `Account` | string (`@handle`) | no | TikTok account to comment as. The agent switches to it via the in-app account switcher **before** opening the post; if it can't confirm the account is active it reports `wrong_account` and **does not comment**. Omit to comment as whatever account is currently active. |
| `ReplyTo` | object | no | When present, submit `Comment` as a **reply to an existing comment** instead of a top-level comment. Must carry a non-empty `author`; see below. Omit for a normal top-level comment (unchanged behavior). |

`ReplyTo` (optional) — identifies the comment to reply to:

| Field | Type | Required | Description |
|-------|------|:--------:|-------------|
| `author` | string (`@handle`) | **yes** | Handle of the comment's author (the `author` value from a `comments-list` message). Matched case-insensitively, leading `@` ignored. |
| `text` | string | no | A substring of the target comment's text. Only needed to disambiguate when the same author has **several** comments on the post; matched ASCII-folded. |

The agent opens the comment sheet, finds the matching **top-level** comment (scrolling
as needed), and taps its Reply button before typing. If it can't find the target it
reports `comment_not_found` and **does not submit**. (Replies can only target top-level
comments, not replies-to-replies.)

> **No `id` and no `CreatedAt`.** Acking uses the MQTT message id internally; status
> is keyed by `PostURL`.

> **ASCII-only typing.** The comment is typed via `adb input text`, which **cannot
> enter emoji / non-ASCII** — those are stripped. If nothing typeable remains after
> stripping, the comment is **not** submitted and the status is `skipped_non_ascii`.
> Realistic scope is Latin-script text.

### Examples

Top-level comment:

```json
{
  "PostURL": "https://www.tiktok.com/@captgani/video/7648864421841816852",
  "Comment": "Nice video!",
  "Account": "@captgani"
}
```

Reply to an existing comment:

```json
{
  "PostURL": "https://www.tiktok.com/@captgani/video/7648864421841816852",
  "Comment": "Makasih kak!",
  "Account": "@captgani",
  "ReplyTo": { "author": "user210320127", "text": "makin plenger" }
}
```

---

## Status message

After acting, the agent publishes a status message (QoS 1) keyed by `PostURL`.
Subscribing is optional.

- **Topic:** `tiktok/comment-status` (agent env `HIVEMQ_COMMENT_STATUS_TOPIC`)

```json
{ "PostURL": "https://www.tiktok.com/@captgani/video/7648864421841816852", "status": "commented", "ts": 1733570460 }
```

| `status` | Meaning |
|----------|---------|
| `commented` | Comment (or reply) typed and submitted successfully. |
| `needs_manual` | A screen wasn't recognized; the agent stopped rather than tap blindly. |
| `skipped_non_ascii` | Nothing typeable remained after stripping non-ASCII; not submitted. |
| `wrong_account` | The target `Account` couldn't be made active; **nothing was commented**. |
| `comment_not_found` | `ReplyTo` was set but the target comment wasn't found in the sheet; **nothing was submitted**. |
| `failed` | Could not open the post / adb error. |

`ts` is a Unix epoch (seconds, integer). **All** statuses (including the non-success
ones) cause the work message to be dropped, so a stuck UI does not loop forever. If
you need the comment posted after a `needs_manual` / `failed`, publish a new message.

---

## Breaking changes

Coordinate both repos for any of: renaming/removing/retyping a field above;
changing the topic names (`tiktok/comments` / `tiktok/comment-status`, configurable
via `HIVEMQ_COMMENT_TOPIC` / `HIVEMQ_COMMENT_STATUS_TOPIC` — both sides must agree);
publishing at QoS 0 or with `retain = true`. Adding a new **optional** field or a new
`status` value is non-breaking (treat unknown fields/statuses defensively).
