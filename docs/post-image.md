# Contract — Post an image (`tiktok/posts`)

How a backend asks **tiktok-agent** to post an image to TikTok. Generate an image,
host it at a public URL, and publish one MQTT message; the agent downloads it,
pushes it to the phone, posts it, and reports the outcome.

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
publisher client-id that does **not** collide with the agent's (`tiktok-agent`), or
the broker will disconnect the agent.

---

## Delivery semantics

- **Publish at QoS 1** (at-least-once) with **`retain = false`**.
- The agent runs a **persistent QoS-1 session** (`clean_session = false`), so
  messages published **while the device is offline are queued by the broker** and
  delivered on reconnect. You do not need to retry or buffer — just publish.
- The agent **acks a message only after it acts on it**. A crash before the ack
  means the broker redelivers, so the agent may **act again** — this is
  **at-least-once, not exactly-once**. De-duplicate downstream if exact-once matters.

---

## Work message

- **Topic:** `tiktok/posts` (agent env `HIVEMQ_TOPIC`)
- **QoS:** 1 · **retain:** false · **payload:** UTF-8 JSON object

| Field | Type | Required | Description |
|-------|------|:--------:|-------------|
| `id` | string | **yes** | Correlation key, echoed back in the status message. Use a UUID or your own post id. A message without a non-empty `id` is **dropped**. |
| `ImageURL` | string (URL) | **yes** | Public image URL the agent downloads (no auth). A message without a non-empty `ImageURL` is **dropped**. |
| `Caption` | string | no | The hook line. Goes first in TikTok's single text field. |
| `Description` | string | no | Appended after the caption on a new line. |
| `ImagePath` | string | no | Suggested filename on the phone; if omitted the agent derives one from `ImageURL`. |
| `Account` | string (`@handle`) | no | TikTok account to post as. The agent switches to it via the in-app account switcher **before** posting; if it can't confirm the account is active it reports `wrong_account` and **does not post**. Omit to post as whatever account is currently active. |
| `CreatedAt` | string (ISO-8601) | no | Informational only. |

> **ASCII-only typing.** Captions are typed via `adb input text`, which **cannot
> enter emoji / non-ASCII**; those characters are stripped before typing (the full
> original text remains in the message). The pipeline stores **no hashtags**.

### Example

```json
{
  "id": "9f1c2e7a-3b44-4f0a-9c21-1d6b8e0a4f55",
  "Caption": "Vespa Sprint 150 review",
  "Description": "Honest take on the new ABS tech.",
  "ImageURL": "https://ik.imagekit.io/your_id/tiktok_1733570400.jpg",
  "ImagePath": "tiktok_1733570400.jpg",
  "Account": "@captgani",
  "CreatedAt": "2026-06-07T12:00:00Z"
}
```

---

## Status message

After acting, the agent publishes a status message (QoS 1) keyed by `id`.
Subscribing is optional.

- **Topic:** `tiktok/status` (agent env `HIVEMQ_STATUS_TOPIC`)

```json
{ "id": "9f1c2e7a-...", "status": "posted", "ts": 1733570460 }
```

| `status` | Meaning |
|----------|---------|
| `posted` | Posted to TikTok successfully. |
| `failed` | Posting failed, or auto-post stopped on an unrecognized screen, or the target `Account` couldn't be made active (`wrong_account` — **nothing was posted**), or `ImageURL` was empty. The message is dropped (not retried). |

`ts` is a Unix epoch (seconds, integer).

---

## Breaking changes

Coordinate both repos for any of: renaming/removing/retyping a field above;
changing the topic names (`tiktok/posts` / `tiktok/status`, configurable via
`HIVEMQ_TOPIC` / `HIVEMQ_STATUS_TOPIC` — both sides must agree); publishing at
QoS 0 or with `retain = true`. Adding a new **optional** field or a new `status`
value is non-breaking (treat unknown fields/statuses defensively).
