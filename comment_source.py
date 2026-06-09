#!/usr/bin/env python3
"""
HiveMQ (MQTT) as the work queue for COMMENT requests — source of truth.

Mirror of hivemq_source.py, but for a separate "leave a comment on a post" flow:
it subscribes to a dedicated comment topic (default "tiktok/comments") and reports
each comment's outcome on a comment status topic (default "tiktok/comment-status").

Kept as a self-contained module (rather than refactoring hivemq_source.py) so the
working posting consumer is untouched and the two run as independent persistent
sessions with their own client-ids. The MQTT plumbing here intentionally
duplicates hivemq_source.py — consistent with the repo's copy-don't-share
convention. If you change the durability semantics in one, change both.

Durability: persistent session (stable client-id, clean_session=False) + QoS 1 +
manual acknowledgement. A message is acked only after the agent reports a terminal
status via update_status; anything unacked is redelivered on reconnect.

Message contract (published by the producer, QoS 1, retained false):
  {"PostURL": "https://www.tiktok.com/@u/video/123", "Comment": "Nice!",
   "Account": "@handle"}
There is NO id field — MQTT acking uses the message's mid, and status is keyed by
PostURL. ``Account`` is optional — when set, the commenter switches to that TikTok
account before commenting (see tiktok_profile.py).

Credentials (read from the environment / .env):
  - HIVEMQ_HOST          Cluster host, e.g. xxxx.s1.eu.hivemq.cloud — required
  - HIVEMQ_PORT          Broker port (default 8883, TLS)
  - HIVEMQ_USERNAME      MQTT username — required
  - HIVEMQ_PASSWORD      MQTT password — required
  - HIVEMQ_COMMENT_TOPIC         Work topic (default "tiktok/comments")
  - HIVEMQ_COMMENT_STATUS_TOPIC  Status topic (default "tiktok/comment-status")
  - HIVEMQ_COMMENT_CLIENT_ID     Stable client id (default "tiktok-commenter")
  - HIVEMQ_TOPIC_PREFIX          Optional prefix prepended to the topics + client-id,
                                 e.g. "test/" (for local testing isolation)
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from typing import Callable, Optional

try:
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError as e:  # pragma: no cover - dependency guard
    raise ImportError(
        "paho-mqtt is required for the HiveMQ comment source. Run `uv sync` to install it."
    ) from e

# Reuse the same error type as the posting source — same failure domain.
from hivemq_source import HiveMQSourceError


DEFAULT_PORT = 8883
DEFAULT_TOPIC = "tiktok/comments"
DEFAULT_STATUS_TOPIC = "tiktok/comment-status"
DEFAULT_CLIENT_ID = "tiktok-commenter"

CONNECT_TIMEOUT = 15.0   # seconds to wait for CONNACK
DRAIN_IDLE = 2.0         # seconds of silence that ends a drain
DRAIN_HARD_CAP = 30.0    # absolute cap on one drain


class _Config:
    __slots__ = ("host", "port", "username", "password", "topic", "status_topic", "client_id")

    def __init__(self) -> None:
        host = os.getenv("HIVEMQ_HOST")
        if not host:
            raise HiveMQSourceError(
                "HIVEMQ_HOST env var is not set. "
                "Export it (or put it in .env) before running the commenter."
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
        # HIVEMQ_TOPIC_PREFIX (e.g. "test/") is prepended verbatim to the topics AND
        # the client-id, so a local test run uses a fully isolated queue + persistent
        # session and never touches production. Unset/empty → production behavior.
        prefix = os.getenv("HIVEMQ_TOPIC_PREFIX") or ""
        self.topic = prefix + (os.getenv("HIVEMQ_COMMENT_TOPIC") or DEFAULT_TOPIC)
        self.status_topic = prefix + (os.getenv("HIVEMQ_COMMENT_STATUS_TOPIC") or DEFAULT_STATUS_TOPIC)
        self.client_id = prefix + (os.getenv("HIVEMQ_COMMENT_CLIENT_ID") or DEFAULT_CLIENT_ID)


# ---- module-level persistent client state ------------------------------------

_client: Optional[mqtt.Client] = None
_config: Optional[_Config] = None
_lock = threading.Lock()
_buffer: list[mqtt.MQTTMessage] = []         # raw messages received since last drain
_pending_msgs: dict[str, mqtt.MQTTMessage] = {}  # post_url -> message awaiting ack
_last_rx = 0.0                                # monotonic time of last received message
_connected = threading.Event()
_connect_error: Optional[str] = None


def _on_connect(client, userdata, flags, reason_code, properties) -> None:
    global _connect_error
    if reason_code.is_failure:
        _connect_error = f"broker refused connection: {reason_code}"
    else:
        _connect_error = None
        client.subscribe(_config.topic, qos=1)
    _connected.set()


def _log_received(message) -> None:
    """Always log every message the moment it arrives, before any validation."""
    body = message.payload[:1000].decode("utf-8", "replace")
    print(
        f"comments: received on {message.topic!r} (qos={message.qos}, {len(message.payload)} bytes): {body}",
        flush=True,
    )


def _on_message(client, userdata, message) -> None:
    global _last_rx
    _log_received(message)
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
        manual_ack=True,            # we PUBACK ourselves, only after a comment completes
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
    """Wait for the queued backlog to arrive, then snapshot+clear the buffer."""
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


def _warn(reason: str, message: mqtt.MQTTMessage) -> None:
    """Log (to stderr) a malformed message that is about to be acked + dropped."""
    snippet = message.payload[:200].decode("utf-8", "replace")
    print(
        f"comments: dropping message on {message.topic!r}: {reason} — {snippet}",
        file=sys.stderr,
        flush=True,
    )


def _parse(message: mqtt.MQTTMessage) -> Optional[dict]:
    """Parse a comment message into {post_url, comment}, or None if invalid.

    A None return means the message failed validation and will be acked + dropped;
    the reason is logged via _warn so silent loss is debuggable.
    """
    try:
        payload = json.loads(message.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        _warn(f"invalid JSON ({e})", message)
        return None
    if not isinstance(payload, dict):
        _warn("payload is not a JSON object", message)
        return None
    post_url = payload.get("PostURL")
    comment = payload.get("Comment")
    if not post_url:
        _warn("missing required 'PostURL' field", message)
        return None
    if not comment:
        _warn(f"missing required 'Comment' field (PostURL={post_url})", message)
        return None
    account = payload.get("Account")  # optional target TikTok @handle
    return {"post_url": str(post_url), "comment": str(comment), "account": account}


def list_pending(*, timeout: float = DRAIN_HARD_CAP) -> list[dict]:
    """
    Drain the current comment backlog into records, oldest-first (publish order).

    Connects the persistent session (if not already), collects every queued QoS-1
    message, and returns valid ones as {post_url, comment} dicts. Invalid messages
    (bad JSON, or missing PostURL/Comment) are acked and dropped so they don't loop.

    The connection is kept open so update_status can ack messages; call close() at
    the end of the cycle.

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
            # Last write wins if the same PostURL appears twice in one drain; the
            # earlier message is left unacked and will be redelivered next time.
            _pending_msgs[rec["post_url"]] = msg
        records.append(rec)
    return records


def update_status(post_url: str, status: str, *, timeout: float = 10.0) -> None:
    """
    Report a comment's outcome: publish {PostURL, status, ts} to the comment status
    topic and ack the corresponding work message (so the broker won't redeliver it).

    All terminal statuses (commented/failed/skipped_non_ascii/needs_manual) ack-drop
    the message so a stuck UI doesn't re-fire forever. A post_url not tracked here
    (already acked, or from a previous session) is published-only.

    Raises:
        HiveMQSourceError: If not connected or the publish fails.
    """
    if _client is None or _config is None:
        raise HiveMQSourceError("update_status called before a successful list_pending/connect")

    body = json.dumps({"PostURL": post_url, "status": status, "ts": int(time.time())})
    info = _client.publish(_config.status_topic, body, qos=1)
    try:
        info.wait_for_publish(timeout)
    except (ValueError, RuntimeError) as e:
        raise HiveMQSourceError(f"Failed to publish status for {post_url}: {e}") from e

    with _lock:
        msg = _pending_msgs.pop(post_url, None)
    if msg is not None:
        _client.ack(msg.mid, msg.qos)


def close() -> None:
    """
    Disconnect the persistent client and release any still-unacked messages back to
    the broker (redelivered on the next connect). Safe to call when not connected.
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


def watch(handler: Callable[[dict], Optional[str]]) -> None:
    """
    Stay connected and dispatch each comment message to `handler` the moment it
    arrives — the event-driven (push) counterpart to list_pending(). No poll
    interval: MQTT pushes, so the agent reacts instantly.

    handler(record) is called on a single worker thread (messages processed one at
    a time, in order) and returns either:
      - a terminal status string → published to the status topic and the message is
        acked (consumed), or
      - None → the message is left unacked and redelivered on the next reconnect.

    Blocks until KeyboardInterrupt. Auto-reconnects (persistent QoS-1 session).

    Raises:
        HiveMQSourceError: On initial connection failure.
    """
    global _client, _config, _connect_error

    if _client is not None:
        close()
    _config = _Config()
    work: "queue.Queue[dict]" = queue.Queue()
    stop = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties) -> None:
        global _connect_error
        if reason_code.is_failure:
            _connect_error = f"broker refused connection: {reason_code}"
        else:
            _connect_error = None
            client.subscribe(_config.topic, qos=1)  # renewed on every reconnect
        _connected.set()

    def on_message(client, userdata, message) -> None:
        _log_received(message)
        rec = _parse(message)
        if rec is None:
            client.ack(message.mid, message.qos)  # drop malformed
            return
        with _lock:
            _pending_msgs[rec["post_url"]] = message
        work.put(rec)

    client = mqtt.Client(
        CallbackAPIVersion.VERSION2,
        client_id=_config.client_id,
        clean_session=False,
        protocol=mqtt.MQTTv311,
        manual_ack=True,
    )
    client.username_pw_set(_config.username, _config.password)
    client.tls_set()
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.on_connect = on_connect
    client.on_message = on_message

    _connected.clear()
    _connect_error = None
    try:
        client.connect(_config.host, _config.port, keepalive=60)
    except (OSError, mqtt.WebsocketConnectionError) as e:
        raise HiveMQSourceError(f"Failed to connect to {_config.host}:{_config.port}: {e}") from e

    def worker() -> None:
        while not stop.is_set():
            try:
                rec = work.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                status = handler(rec)
            except Exception:  # handler is expected to handle its own errors; be defensive
                status = "failed"
            if status:
                try:
                    update_status(rec["post_url"], status)
                except HiveMQSourceError:
                    pass
            work.task_done()

    t = threading.Thread(target=worker, name="comments-worker", daemon=True)
    t.start()
    client.loop_start()
    _client = client

    if not _connected.wait(CONNECT_TIMEOUT):
        stop.set()
        close()
        raise HiveMQSourceError(f"Timed out connecting to {_config.host}:{_config.port}")
    if _connect_error:
        err = _connect_error
        stop.set()
        close()
        raise HiveMQSourceError(err)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        close()
