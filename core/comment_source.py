#!/usr/bin/env python3
"""
HiveMQ (MQTT) as the work queue for COMMENT requests — source of truth.

Sibling of hivemq_source.py for the separate "leave a comment on a post" flow: it
subscribes to a dedicated comment topic (default "tiktok/comments") and reports each
comment's outcome on a comment status topic (default "tiktok/comment-status").

It runs as its own persistent session with its own client-id, so it never collides
with the poster. The durable-queue machinery is shared (`mqtt_queue.MqttWorkQueue`);
this file only supplies the comment-specific config + message parsing and re-exports
the queue's API as module-level functions.

Message contract (published by the producer, QoS 1, retained false):
  {"PostURL": "https://www.tiktok.com/@u/video/123", "Comment": "Nice!",
   "Account": "@handle"}
There is NO id field — MQTT acking uses the message's mid, and status is keyed by
PostURL. ``Account`` is optional — when set, the commenter switches to that TikTok
account before commenting (see tiktok_profile.py).

Credentials (read from the environment / .env):
  - HIVEMQ_HOST / HIVEMQ_PORT / HIVEMQ_USERNAME / HIVEMQ_PASSWORD  — as hivemq_source
  - HIVEMQ_COMMENT_TOPIC         Work topic (default "tiktok/comments")
  - HIVEMQ_COMMENT_STATUS_TOPIC  Status topic (default "tiktok/comment-status")
  - HIVEMQ_COMMENT_CLIENT_ID     Stable client id (default "tiktok-commenter")
  - HIVEMQ_TOPIC_PREFIX          Optional prefix prepended to the topics + client-id
"""

from __future__ import annotations

import json
from typing import Optional

import paho.mqtt.client as mqtt

from .mqtt_queue import HiveMQSourceError, MqttWorkQueue, make_config


DEFAULT_TOPIC = "tiktok/comments"
DEFAULT_STATUS_TOPIC = "tiktok/comment-status"
DEFAULT_CLIENT_ID = "tiktok-commenter"


def _parse(message: mqtt.MQTTMessage, warn) -> Optional[dict]:
    """Parse a comment message into {post_url, comment, account}, or None if invalid.

    A None return means the message failed validation and will be acked + dropped;
    the reason is logged via ``warn`` so silent loss is debuggable.
    """
    try:
        payload = json.loads(message.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        warn(f"invalid JSON ({e})", message)
        return None
    if not isinstance(payload, dict):
        warn("payload is not a JSON object", message)
        return None
    post_url = payload.get("PostURL")
    comment = payload.get("Comment")
    if not post_url:
        warn("missing required 'PostURL' field", message)
        return None
    if not comment:
        warn(f"missing required 'Comment' field (PostURL={post_url})", message)
        return None
    account = payload.get("Account")  # optional target TikTok @handle
    return {"post_url": str(post_url), "comment": str(comment), "account": account}


_QUEUE = MqttWorkQueue(
    make_config=make_config(
        topic_env="HIVEMQ_COMMENT_TOPIC",
        topic_default=DEFAULT_TOPIC,
        status_env="HIVEMQ_COMMENT_STATUS_TOPIC",
        status_default=DEFAULT_STATUS_TOPIC,
        client_env="HIVEMQ_COMMENT_CLIENT_ID",
        client_default=DEFAULT_CLIENT_ID,
        role="commenter",
    ),
    parse=_parse,
    key_of=lambda rec: rec["post_url"],
    status_key_name="PostURL",
    log_prefix="comments",
)


def list_pending(*, timeout: float = 30.0) -> list[dict]:
    """Drain the current comment backlog into {post_url, comment, account} records."""
    return _QUEUE.list_pending(timeout=timeout)


def update_status(post_url: str, status: str, *, timeout: float = 10.0) -> None:
    """Publish {PostURL, status, ts} and ack the matching work message."""
    _QUEUE.update_status(post_url, status, timeout=timeout)


def close() -> None:
    """Disconnect; release any still-unacked messages back to the broker."""
    _QUEUE.close()


def watch(handler) -> None:
    """Event-driven push subscription: dispatch each comment message to ``handler``."""
    _QUEUE.watch(handler)
