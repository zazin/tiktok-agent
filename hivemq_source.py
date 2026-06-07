#!/usr/bin/env python3
"""
HiveMQ (MQTT) as the work queue — the source of truth.

The tiktok-pipeline publishes one MQTT message per generated post to a work topic
(default "tiktok/posts"), carrying the caption, description, and the public
ImageKit image URL. This module subscribes to that topic (the queue) and reports
each post's outcome by publishing to a status topic (default "tiktok/status").

A durable, at-least-once work queue on top of fire-and-forget pub/sub is built
from a **persistent session + QoS 1 + manual acknowledgement**:

  - The client uses a stable client-id and ``clean_session=False``, so the broker
    queues messages while the device is offline and redelivers them on reconnect.
  - Each message is QoS 1 and is **acked only after** the agent reports a terminal
    status (``posted``/``failed``) via ``update_status``. Anything still unacked
    (the ``--no-auto-post`` path, or a crash before the post finishes) is
    redelivered on the next connect, so it stays pending until actually posted.

A poll cycle = one connect → drain queued messages → process → ack the done ones →
disconnect (via ``close()``). Disconnecting releases the unacked messages back to
the broker so they are redelivered next cycle.

Auth is HiveMQ Cloud standard: TLS on port 8883 with a username/password.

Credentials (read from the environment / .env):
  - HIVEMQ_HOST          Cluster host, e.g. xxxx.s1.eu.hivemq.cloud — required
  - HIVEMQ_PORT          Broker port (default 8883, TLS)
  - HIVEMQ_USERNAME      MQTT username — required
  - HIVEMQ_PASSWORD      MQTT password — required
  - HIVEMQ_TOPIC         Work topic to subscribe to (default "tiktok/posts")
  - HIVEMQ_STATUS_TOPIC  Topic to publish post outcomes to (default "tiktok/status")
  - HIVEMQ_CLIENT_ID     Stable client id → persistent session (default "tiktok-agent")

Message contract (published by the pipeline, QoS 1, retained false):
  {"id": "...", "Caption": "...", "Description": "...",
   "ImageURL": "https://ik.imagekit.io/.../x.jpg", "ImagePath": "x.jpg",
   "CreatedAt": "2026-06-07T12:00:00Z"}
``id`` is required and is the correlation key used when reporting status back.

Usage (CLI):
    python hivemq_source.py            # peek the current backlog (no ack)
    python hivemq_source.py --json

Usage (as a module):
    from hivemq_source import list_pending, update_status, close
    try:
        for rec in list_pending():
            ...
            update_status(rec["id"], "posted")
    finally:
        close()
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Optional

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError as e:  # pragma: no cover - dependency guard
    raise ImportError(
        "paho-mqtt is required for the HiveMQ source. Run `uv sync` to install it."
    ) from e


DEFAULT_PORT = 8883
DEFAULT_TOPIC = "tiktok/posts"
DEFAULT_STATUS_TOPIC = "tiktok/status"
DEFAULT_CLIENT_ID = "tiktok-agent"

CONNECT_TIMEOUT = 15.0   # seconds to wait for CONNACK
DRAIN_IDLE = 2.0         # seconds of silence that ends a drain
DRAIN_HARD_CAP = 30.0    # absolute cap on one drain


class HiveMQSourceError(Exception):
    """Raised when connecting to, reading from, or writing to HiveMQ fails."""


class _Config:
    __slots__ = ("host", "port", "username", "password", "topic", "status_topic", "client_id")

    def __init__(self) -> None:
        host = os.getenv("HIVEMQ_HOST")
        if not host:
            raise HiveMQSourceError(
                "HIVEMQ_HOST env var is not set. "
                "Export it (or put it in .env) before running the agent."
            )
        username = os.getenv("HIVEMQ_USERNAME")
        password = os.getenv("HIVEMQ_PASSWORD")
        if not username or not password:
            raise HiveMQSourceError(
                "HIVEMQ_USERNAME and HIVEMQ_PASSWORD must both be set."
            )
        self.host = host
        self.port = int(os.getenv("HIVEMQ_PORT") or DEFAULT_PORT)
        self.username = username
        self.password = password
        self.topic = os.getenv("HIVEMQ_TOPIC") or DEFAULT_TOPIC
        self.status_topic = os.getenv("HIVEMQ_STATUS_TOPIC") or DEFAULT_STATUS_TOPIC
        self.client_id = os.getenv("HIVEMQ_CLIENT_ID") or DEFAULT_CLIENT_ID


# ---- module-level persistent client state ------------------------------------

_client: Optional[mqtt.Client] = None
_config: Optional[_Config] = None
_lock = threading.Lock()
_buffer: list[mqtt.MQTTMessage] = []        # raw messages received since last drain
_pending_msgs: dict[str, mqtt.MQTTMessage] = {}  # id -> message awaiting ack
_last_rx = 0.0                               # monotonic time of last received message
_connected = threading.Event()
_connect_error: Optional[str] = None


def _on_connect(client, userdata, flags, reason_code, properties) -> None:
    global _connect_error
    if reason_code.is_failure:
        _connect_error = f"broker refused connection: {reason_code}"
    else:
        _connect_error = None
        # Subscribe on every (re)connect; for a fresh session the broker has no
        # stored subscription yet, so this is what starts delivery.
        client.subscribe(_config.topic, qos=1)
    _connected.set()


def _on_message(client, userdata, message) -> None:
    global _last_rx
    with _lock:
        _buffer.append(message)
        _last_rx = time.monotonic()


def _ensure_connected() -> mqtt.Client:
    """Connect the persistent client if needed; return it."""
    global _client, _config, _connect_error
    if _client is not None:
        return _client

    _config = _Config()
    _connected.clear()
    _connect_error = None

    client = mqtt.Client(
        CallbackAPIVersion.VERSION2,
        client_id=_config.client_id,
        clean_session=False,        # persistent session: broker queues while offline
        protocol=mqtt.MQTTv311,
        manual_ack=True,            # we PUBACK ourselves, only after a post completes
    )
    client.username_pw_set(_config.username, _config.password)
    client.tls_set()                # default system CA certs (HiveMQ Cloud uses TLS)
    client.on_connect = _on_connect
    client.on_message = _on_message

    try:
        client.connect(_config.host, _config.port, keepalive=60)
    except (OSError, mqtt.WebsocketConnectionError) as e:
        raise HiveMQSourceError(f"Failed to connect to {_config.host}:{_config.port}: {e}") from e

    client.loop_start()
    if not _connected.wait(CONNECT_TIMEOUT):
        client.loop_stop()
        raise HiveMQSourceError(f"Timed out connecting to {_config.host}:{_config.port}")
    if _connect_error:
        client.loop_stop()
        raise HiveMQSourceError(_connect_error)

    _client = client
    return client


def _drain(*, idle: float = DRAIN_IDLE, hard_cap: float = DRAIN_HARD_CAP) -> list[mqtt.MQTTMessage]:
    """Wait for the queued backlog to arrive, then snapshot+clear the buffer.

    Returns once no new message has arrived for ``idle`` seconds (or ``hard_cap``
    elapses). The broker delivers a persistent session's queued messages right
    after the subscription is active, so the idle gap reliably marks the end.
    """
    global _last_rx
    with _lock:
        _buffer.clear()
        _last_rx = 0.0
    start = time.monotonic()
    while True:
        time.sleep(0.1)
        now = time.monotonic()
        with _lock:
            last = _last_rx
        if now - start >= hard_cap:
            break
        marker = last or start
        if now - marker >= idle:
            break
    with _lock:
        msgs = list(_buffer)
        _buffer.clear()
    return msgs


def _parse(message: mqtt.MQTTMessage) -> Optional[dict]:
    """Parse a work message into a {id, fields} record, or None if invalid."""
    try:
        payload = json.loads(message.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    rec_id = payload.get("id")
    if not rec_id or not payload.get("ImageURL"):
        return None
    return {
        "id": str(rec_id),
        "fields": {
            "Caption": payload.get("Caption"),
            "Description": payload.get("Description"),
            "ImageURL": payload.get("ImageURL"),
            "ImagePath": payload.get("ImagePath"),
            "CreatedAt": payload.get("CreatedAt"),
        },
    }


def list_pending(*, timeout: float = DRAIN_HARD_CAP) -> list[dict]:
    """
    Drain the current MQTT backlog into records, oldest-first (publish order).

    Connects the persistent session (if not already), collects every queued QoS-1
    message, and returns valid ones as {id, fields: {...}} dicts. Invalid messages
    (bad JSON, or missing id/ImageURL) are acked and dropped so they don't loop.

    The connection is kept open so ``update_status`` can ack messages; call
    ``close()`` at the end of the poll cycle.

    Raises:
        HiveMQSourceError: On connection failure.
    """
    client = _ensure_connected()
    msgs = _drain(hard_cap=timeout)

    records: list[dict] = []
    with _lock:
        _pending_msgs.clear()
    for msg in msgs:
        rec = _parse(msg)
        if rec is None:
            client.ack(msg.mid, msg.qos)  # drop the malformed message
            continue
        with _lock:
            _pending_msgs[rec["id"]] = msg
        records.append(rec)
    return records


def update_status(record_id: str, status: str, *, timeout: float = 10.0) -> None:
    """
    Report a post's outcome: publish {id, status, ts} to the status topic and ack
    the corresponding work message (so the broker won't redeliver it).

    Both "posted" and "failed" ack-drop the message (a terminal "failed" should
    not loop forever). A message whose id is unknown here (already acked, or from
    a previous session) is published-only.

    Raises:
        HiveMQSourceError: If not connected or the publish fails.
    """
    if _client is None or _config is None:
        raise HiveMQSourceError("update_status called before a successful list_pending/connect")

    body = json.dumps({"id": record_id, "status": status, "ts": int(time.time())})
    info = _client.publish(_config.status_topic, body, qos=1)
    try:
        info.wait_for_publish(timeout)
    except (ValueError, RuntimeError) as e:
        raise HiveMQSourceError(f"Failed to publish status for {record_id}: {e}") from e

    with _lock:
        msg = _pending_msgs.pop(record_id, None)
    if msg is not None:
        _client.ack(msg.mid, msg.qos)


def close() -> None:
    """
    Disconnect the persistent client and release any still-unacked messages back
    to the broker (they are redelivered on the next connect). Safe to call when
    not connected.
    """
    global _client
    if _client is None:
        return
    try:
        _client.disconnect()
        _client.loop_stop()
    finally:
        _client = None
        _connected.clear()
        with _lock:
            _buffer.clear()
            _pending_msgs.clear()


def _cli() -> int:
    from env_loader import load_env
    load_env()

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
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        # Reset the ack registry so close() can't ack anything we peeked.
        with _lock:
            _pending_msgs.clear()
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
