#!/usr/bin/env python3
"""
HiveMQ (MQTT) work queue for COMMENT-READ requests — the read half of the
comment-reply pipeline.

A backend that wants to reply to a post's comments first needs to SEE them, but it
has no TikTok API — only the device can read a post's comments (via the on-screen
UI). So the backend publishes a read-job here and this consumer drains it, scrapes
the post's comments on the phone, and publishes the list back; the backend then
generates replies and sends them to the commenter (`comment_source.py`).

Sibling of `comment_source.py`/`hivemq_source.py`: it runs as its own persistent
session with its own client-id so it never collides with the poster or commenter.
The durable-queue machinery is shared (`mqtt_queue.MqttWorkQueue`); this file only
supplies the read-specific config + message parsing and re-exports the queue API.

Round-trip:
  - read-job in  (this consumer drains it):
      {"PostURL": "https://www.tiktok.com/@u/video/123", "max": 10}
    ``max`` is optional (cap on how many comments to scrape; falls back to the
    consumer's default).
  - comment list out (this consumer publishes it; the backend subscribes):
      {"PostURL": "...", "comments": [{"author": "...", "text": "..."}],
       "count": N, "ts": 1700000000}

There is NO id field — MQTT acking uses the message's mid, and status is keyed by
PostURL.

Credentials (read from the environment / .env):
  - HIVEMQ_HOST / HIVEMQ_PORT / HIVEMQ_USERNAME / HIVEMQ_PASSWORD  — as hivemq_source
  - HIVEMQ_COMMENT_READ_TOPIC    Read-job topic (default "tiktok/comments-read")
  - HIVEMQ_COMMENT_LIST_TOPIC    Comment-list output topic (default "tiktok/comments-list")
  - HIVEMQ_COMMENT_READ_CLIENT_ID  Stable client id (default "tiktok-comment-reader")
  - HIVEMQ_TOPIC_PREFIX          Optional prefix prepended to the topics + client-id
"""

from __future__ import annotations

import json
from typing import Optional

import paho.mqtt.client as mqtt

from .mqtt_queue import HiveMQSourceError, MqttWorkQueue, make_config


DEFAULT_TOPIC = "tiktok/comments-read"
DEFAULT_LIST_TOPIC = "tiktok/comments-list"
DEFAULT_CLIENT_ID = "tiktok-comment-reader"


def _parse(message: mqtt.MQTTMessage, warn) -> Optional[dict]:
    """Parse a read-job into {post_url, max}, or None (acked + dropped) if invalid."""
    try:
        payload = json.loads(message.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        warn(f"invalid JSON ({e})", message)
        return None
    if not isinstance(payload, dict):
        warn("payload is not a JSON object", message)
        return None
    post_url = payload.get("PostURL")
    if not post_url:
        warn("missing required 'PostURL' field", message)
        return None
    max_comments = None
    raw_max = payload.get("max")
    if raw_max is not None:
        try:
            max_comments = int(raw_max)
        except (TypeError, ValueError):
            warn(f"ignoring non-integer 'max' for {post_url}", message)
    return {"post_url": str(post_url), "max": max_comments}


_QUEUE = MqttWorkQueue(
    make_config=make_config(
        topic_env="HIVEMQ_COMMENT_READ_TOPIC",
        topic_default=DEFAULT_TOPIC,
        status_env="HIVEMQ_COMMENT_LIST_TOPIC",
        status_default=DEFAULT_LIST_TOPIC,
        client_env="HIVEMQ_COMMENT_READ_CLIENT_ID",
        client_default=DEFAULT_CLIENT_ID,
        role="comment-reader",
    ),
    parse=_parse,
    key_of=lambda rec: rec["post_url"],
    status_key_name="PostURL",
    log_prefix="comments-read",
)


def list_pending(*, timeout: float = 30.0) -> list[dict]:
    """Drain the current read-job backlog into {post_url, max} records."""
    return _QUEUE.list_pending(timeout=timeout)


def publish_comments(post_url: str, body: dict, *, timeout: float = 10.0) -> None:
    """Publish a scraped comment list to the output topic and ack the read-job."""
    _QUEUE.publish_result(post_url, body, timeout=timeout)


def update_status(post_url: str, status: str, *, timeout: float = 10.0) -> None:
    """Publish {PostURL, status, ts} to the output topic and ack (used for catch-up)."""
    _QUEUE.update_status(post_url, status, timeout=timeout)


def close() -> None:
    """Disconnect; release any still-unacked read-jobs back to the broker."""
    _QUEUE.close()


def watch(handler) -> None:
    """Event-driven push subscription: dispatch each read-job to ``handler``."""
    _QUEUE.watch(handler)
