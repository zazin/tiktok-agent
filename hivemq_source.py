#!/usr/bin/env python3
"""
HiveMQ (MQTT) as the work queue for POST requests — the source of truth.

The tiktok-pipeline publishes one MQTT message per generated post to a work topic
(default "tiktok/posts"), carrying the caption, description, and the public ImageKit
image URL. This module subscribes to that topic (the queue) and reports each post's
outcome by publishing to a status topic (default "tiktok/status").

All the durable-queue machinery (persistent session + QoS 1 + manual ack, drain,
event-driven watch) lives in `mqtt_queue.MqttWorkQueue`; this file only supplies the
post-specific config + message parsing and re-exports the queue's API as module-level
functions for backward compatibility.

Message contract (published by the pipeline, QoS 1, retained false):
  {"id": "...", "Caption": "...", "Description": "...",
   "ImageURL": "https://ik.imagekit.io/.../x.jpg", "ImagePath": "x.jpg",
   "CreatedAt": "2026-06-07T12:00:00Z", "Account": "@handle"}
``id`` is required and is the correlation key echoed back in status messages.
``Account`` is optional — when set, the agent switches to that TikTok account
before posting (see tiktok_profile.py).

Credentials (read from the environment / .env):
  - HIVEMQ_HOST          Cluster host — required
  - HIVEMQ_PORT          Broker port (default 8883, TLS)
  - HIVEMQ_USERNAME      MQTT username — required
  - HIVEMQ_PASSWORD      MQTT password — required
  - HIVEMQ_TOPIC         Work topic to subscribe to (default "tiktok/posts")
  - HIVEMQ_STATUS_TOPIC  Topic to publish post outcomes to (default "tiktok/status")
  - HIVEMQ_CLIENT_ID     Stable client id → persistent session (default "tiktok-agent")
  - HIVEMQ_TOPIC_PREFIX  Optional prefix prepended to the topics + client-id

Usage (CLI):  python hivemq_source.py [--json]   # peek the backlog (no ack)
Usage (module): from hivemq_source import list_pending, update_status, close
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

import paho.mqtt.client as mqtt

from core.mqtt_queue import HiveMQSourceError, MqttWorkQueue, make_config

logger = logging.getLogger(__name__)


DEFAULT_TOPIC = "tiktok/posts"
DEFAULT_STATUS_TOPIC = "tiktok/status"
DEFAULT_CLIENT_ID = "tiktok-agent"


def _parse(message: mqtt.MQTTMessage, warn) -> Optional[dict]:
    """Parse a work message into a {id, fields} record, or None if invalid.

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
    rec_id = payload.get("id")
    if not rec_id:
        warn("missing required 'id' field", message)
        return None
    if not payload.get("ImageURL"):
        warn(f"missing 'ImageURL' (id={rec_id})", message)
        return None
    return {
        "id": str(rec_id),
        "fields": {
            "Caption": payload.get("Caption"),
            "Description": payload.get("Description"),
            "ImageURL": payload.get("ImageURL"),
            "ImagePath": payload.get("ImagePath"),
            "CreatedAt": payload.get("CreatedAt"),
            "Account": payload.get("Account"),  # optional target TikTok @handle
        },
    }


_QUEUE = MqttWorkQueue(
    make_config=make_config(
        topic_env="HIVEMQ_TOPIC",
        topic_default=DEFAULT_TOPIC,
        status_env="HIVEMQ_STATUS_TOPIC",
        status_default=DEFAULT_STATUS_TOPIC,
        client_env="HIVEMQ_CLIENT_ID",
        client_default=DEFAULT_CLIENT_ID,
        role="agent",
    ),
    parse=_parse,
    key_of=lambda rec: rec["id"],
    status_key_name="id",
    log_prefix="hivemq",
)


def list_pending(*, timeout: float = 30.0) -> list[dict]:
    """Drain the current backlog into {id, fields} records, oldest-first."""
    return _QUEUE.list_pending(timeout=timeout)


def update_status(record_id: str, status: str, *, timeout: float = 10.0) -> None:
    """Publish {id, status, ts} and ack the matching work message."""
    _QUEUE.update_status(record_id, status, timeout=timeout)


def close() -> None:
    """Disconnect; release any still-unacked messages back to the broker."""
    _QUEUE.close()


def watch(handler) -> None:
    """Event-driven push subscription: dispatch each message to ``handler``."""
    _QUEUE.watch(handler)


def _cli() -> int:
    from core.env_loader import load_env
    from core.logging_setup import setup_logging
    load_env()
    setup_logging("tiktok-hivemq")

    parser = argparse.ArgumentParser(
        description="Peek the HiveMQ work-topic backlog (does not acknowledge/consume)."
    )
    parser.add_argument("--json", action="store_true", help="Print full JSON instead of a summary")
    args = parser.parse_args()

    try:
        # list_pending() registers the messages for ack; since we never call
        # update_status and close() without acking, they stay queued for the agent.
        records = list_pending()
    except HiveMQSourceError as e:
        logger.error("Error: %s", e)
        return 1
    finally:
        # Reset the ack registry so close() can't ack anything we peeked.
        _QUEUE.clear_pending()
        close()

    if args.json:
        print(json.dumps(records, indent=2))
    else:
        print(f"{len(records)} pending message(s):")
        for rec in records:
            f = rec.get("fields", {})
            idea = f.get("Caption") or ""
            print(f"  {rec.get('id')}  {f.get('CreatedAt', '')}  {idea}  -> {f.get('ImageURL', '')}")

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
