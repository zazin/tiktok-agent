# Contract — Comment on a post (`tiktok/comments`)

How a backend asks **tiktok-agent** to leave a comment on an existing TikTok post.
Publish one MQTT message with the post URL and the comment text; the agent opens the
post by URL, types the comment, submits it, and reports the outcome. **No AI** — the
exact comment text comes from the message.

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

> **No `id` and no `CreatedAt`.** Acking uses the MQTT message id internally; status
> is keyed by `PostURL`.

> **ASCII-only typing.** The comment is typed via `adb input text`, which **cannot
> enter emoji / non-ASCII** — those are stripped. If nothing typeable remains after
> stripping, the comment is **not** submitted and the status is `skipped_non_ascii`.
> Realistic scope is Latin-script text.

### Example

```json
{
  "PostURL": "https://www.tiktok.com/@captgani/video/7648864421841816852",
  "Comment": "Nice video!",
  "Account": "@captgani"
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
| `commented` | Comment typed and submitted successfully. |
| `needs_manual` | A screen wasn't recognized; the agent stopped rather than tap blindly. |
| `skipped_non_ascii` | Nothing typeable remained after stripping non-ASCII; not submitted. |
| `wrong_account` | The target `Account` couldn't be made active; **nothing was commented**. |
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
