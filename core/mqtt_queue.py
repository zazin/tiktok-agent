#!/usr/bin/env python3
"""
mqtt_queue — a durable HiveMQ (MQTT) work queue, shared by both consumers.

The tiktok-pipeline (and the comment producer) publish one MQTT message per unit of
work to a topic; this turns that fire-and-forget pub/sub into a **durable,
at-least-once work queue** via a **persistent session + QoS 1 + manual ack**:

  - The client uses a stable client-id and ``clean_session=False``, so the broker
    queues messages while the device is offline and redelivers them on reconnect.
  - Each message is QoS 1 and is **acked only after** a terminal status is reported
    via ``update_status``. Anything still unacked is redelivered on the next
    connect, so it stays pending until actually handled.

Two consumption modes:
  - ``watch(handler)`` — event-driven (push): stay connected and react to each
    message the instant it arrives. No poll interval.
  - ``list_pending()`` + ``update_status()`` + ``close()`` — a one-shot drain of the
    current backlog (connect → drain → process → ack the done ones → disconnect).

Auth is HiveMQ Cloud standard: TLS on port 8883 with a username/password.

The poster (`hivemq_source.py`) and the commenter (`comment_source.py`) each hold
their **own** ``MqttWorkQueue`` instance — separate topics, client-ids, persistent
sessions and parse rules — so they never collide. What they share is this one
implementation of the durability/connection/drain machinery.

Each instance is parameterized by:
  - ``make_config()``   → reads env into a `_Config` (host/port/creds/topics/client-id)
  - ``parse(msg, warn)``→ message → record dict (or None to ack-drop; logs via `warn`)
  - ``key_of(record)``  → the ack key for a record (id / PostURL); also the status key
  - ``status_key_name`` → JSON field name for the key in published status messages
  - ``log_prefix``      → prefix for the per-message log lines (e.g. "hivemq")
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
        "paho-mqtt is required for the HiveMQ source. Run `uv sync` to install it."
    ) from e


DEFAULT_PORT = 8883

CONNECT_TIMEOUT = 15.0   # seconds to wait for CONNACK
DRAIN_IDLE = 2.0         # seconds of silence that ends a drain
DRAIN_HARD_CAP = 30.0    # absolute cap on one drain


class HiveMQSourceError(Exception):
    """Raised when connecting to, reading from, or writing to HiveMQ fails."""


class _Config:
    __slots__ = ("host", "port", "username", "password", "topic", "status_topic", "client_id")

    def __init__(
        self,
        *,
        topic_env: str,
        topic_default: str,
        status_env: str,
        status_default: str,
        client_env: str,
        client_default: str,
        role: str,
    ) -> None:
        host = os.getenv("HIVEMQ_HOST")
        if not host:
            raise HiveMQSourceError(
                "HIVEMQ_HOST env var is not set. "
                f"Export it (or put it in .env) before running the {role}."
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
        self.topic = prefix + (os.getenv(topic_env) or topic_default)
        self.status_topic = prefix + (os.getenv(status_env) or status_default)
        self.client_id = prefix + (os.getenv(client_env) or client_default)


def make_config(
    *,
    topic_env: str,
    topic_default: str,
    status_env: str,
    status_default: str,
    client_env: str,
    client_default: str,
    role: str,
) -> Callable[[], _Config]:
    """Return a zero-arg factory that builds a `_Config` from the environment.

    The factory is called lazily (at connect time, like the original `_Config()`)
    so env/.env changes are picked up and import never reads credentials.
    """
    def factory() -> _Config:
        return _Config(
            topic_env=topic_env,
            topic_default=topic_default,
            status_env=status_env,
            status_default=status_default,
            client_env=client_env,
            client_default=client_default,
            role=role,
        )
    return factory


class MqttWorkQueue:
    """A persistent, QoS-1, manually-acked MQTT work queue for one topic/client-id."""

    def __init__(
        self,
        *,
        make_config: Callable[[], _Config],
        parse: Callable[[mqtt.MQTTMessage, Callable[[str, mqtt.MQTTMessage], None]], Optional[dict]],
        key_of: Callable[[dict], str],
        status_key_name: str,
        log_prefix: str,
    ) -> None:
        self._make_config = make_config
        self._parse_fn = parse
        self._key_of = key_of
        self._status_key_name = status_key_name
        self._log_prefix = log_prefix

        self._client: Optional[mqtt.Client] = None
        self._config: Optional[_Config] = None
        self._lock = threading.Lock()
        self._buffer: list[mqtt.MQTTMessage] = []        # raw msgs since last drain
        self._pending_msgs: dict[str, mqtt.MQTTMessage] = {}  # key -> msg awaiting ack
        self._last_rx = 0.0                              # monotonic time of last rx
        self._connected = threading.Event()
        self._connect_error: Optional[str] = None

    # ---- logging -------------------------------------------------------------

    def _log_received(self, message: mqtt.MQTTMessage) -> None:
        """Always log every message the moment it arrives, before any validation."""
        body = message.payload[:1000].decode("utf-8", "replace")
        print(
            f"{self._log_prefix}: received on {message.topic!r} "
            f"(qos={message.qos}, {len(message.payload)} bytes): {body}",
            flush=True,
        )

    def _warn(self, reason: str, message: mqtt.MQTTMessage) -> None:
        """Log (to stderr) a malformed message that is about to be acked + dropped."""
        snippet = message.payload[:200].decode("utf-8", "replace")
        print(
            f"{self._log_prefix}: dropping message on {message.topic!r}: {reason} — {snippet}",
            file=sys.stderr,
            flush=True,
        )

    def _parse(self, message: mqtt.MQTTMessage) -> Optional[dict]:
        return self._parse_fn(message, self._warn)

    # ---- connection (one-shot drain path) ------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            self._connect_error = f"broker refused connection: {reason_code}"
        else:
            self._connect_error = None
            # Subscribe on every (re)connect; for a fresh session the broker has no
            # stored subscription yet, so this is what starts delivery.
            client.subscribe(self._config.topic, qos=1)
        self._connected.set()

    def _on_message(self, client, userdata, message) -> None:
        self._log_received(message)
        with self._lock:
            self._buffer.append(message)
            self._last_rx = time.monotonic()

    def _ensure_connected(self) -> mqtt.Client:
        """Connect the persistent client if needed; return it."""
        if self._client is not None:
            return self._client

        self._config = self._make_config()
        self._connected.clear()
        self._connect_error = None

        client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=self._config.client_id,
            clean_session=False,        # persistent session: broker queues while offline
            protocol=mqtt.MQTTv311,
            manual_ack=True,            # we PUBACK ourselves, only after work completes
        )
        client.username_pw_set(self._config.username, self._config.password)
        client.tls_set()                # default system CA certs (HiveMQ Cloud uses TLS)
        client.on_connect = self._on_connect
        client.on_message = self._on_message

        try:
            client.connect(self._config.host, self._config.port, keepalive=60)
        except (OSError, mqtt.WebsocketConnectionError) as e:
            raise HiveMQSourceError(
                f"Failed to connect to {self._config.host}:{self._config.port}: {e}"
            ) from e

        client.loop_start()
        if not self._connected.wait(CONNECT_TIMEOUT):
            client.loop_stop()
            raise HiveMQSourceError(f"Timed out connecting to {self._config.host}:{self._config.port}")
        if self._connect_error:
            client.loop_stop()
            raise HiveMQSourceError(self._connect_error)

        self._client = client
        return client

    def _drain(self, *, idle: float = DRAIN_IDLE, hard_cap: float = DRAIN_HARD_CAP) -> list[mqtt.MQTTMessage]:
        """Wait for the queued backlog to arrive, then snapshot+clear the buffer.

        Returns once no new message has arrived for ``idle`` seconds (or ``hard_cap``
        elapses). The broker delivers a persistent session's queued messages right
        after the subscription is active, so the idle gap reliably marks the end.
        """
        with self._lock:
            self._buffer.clear()
            self._last_rx = 0.0
        start = time.monotonic()
        while True:
            time.sleep(0.1)
            now = time.monotonic()
            with self._lock:
                last = self._last_rx
            if now - start >= hard_cap:
                break
            marker = last or start
            if now - marker >= idle:
                break
        with self._lock:
            msgs = list(self._buffer)
            self._buffer.clear()
        return msgs

    def list_pending(self, *, timeout: float = DRAIN_HARD_CAP) -> list[dict]:
        """
        Drain the current MQTT backlog into records, oldest-first (publish order).

        Connects the persistent session (if not already), collects every queued QoS-1
        message, and returns valid ones as record dicts. Invalid messages (bad JSON,
        or missing required fields) are acked and dropped so they don't loop.

        The connection is kept open so ``update_status`` can ack messages; call
        ``close()`` at the end of the poll cycle.

        Raises:
            HiveMQSourceError: On connection failure.
        """
        client = self._ensure_connected()
        msgs = self._drain(hard_cap=timeout)

        records: list[dict] = []
        with self._lock:
            self._pending_msgs.clear()
        for msg in msgs:
            rec = self._parse(msg)
            if rec is None:
                client.ack(msg.mid, msg.qos)  # drop the malformed message
                continue
            with self._lock:
                # Last write wins if the same key appears twice in one drain; the
                # earlier message is left unacked and redelivered next time.
                self._pending_msgs[self._key_of(rec)] = msg
            records.append(rec)
        return records

    def update_status(self, key: str, status: str, *, timeout: float = 10.0) -> None:
        """
        Report an outcome: publish {<key field>, status, ts} to the status topic and
        ack the corresponding work message (so the broker won't redeliver it).

        Terminal statuses ack-drop the message so a stuck item doesn't loop forever.
        A key not tracked here (already acked, or from a previous session) is
        published-only.

        Raises:
            HiveMQSourceError: If not connected or the publish fails.
        """
        if self._client is None or self._config is None:
            raise HiveMQSourceError("update_status called before a successful list_pending/connect")

        body = json.dumps({self._status_key_name: key, "status": status, "ts": int(time.time())})
        info = self._client.publish(self._config.status_topic, body, qos=1)
        try:
            info.wait_for_publish(timeout)
        except (ValueError, RuntimeError) as e:
            raise HiveMQSourceError(f"Failed to publish status for {key}: {e}") from e

        with self._lock:
            msg = self._pending_msgs.pop(key, None)
        if msg is not None:
            self._client.ack(msg.mid, msg.qos)

    def publish_result(self, key: str, body: dict, *, timeout: float = 10.0) -> None:
        """
        Publish a caller-supplied JSON ``body`` to the output (status) topic and ack
        the work message keyed by ``key`` — like ``update_status`` but for a richer
        result payload (e.g. a comment-reader publishing the scraped comment list,
        not just a {key, status} pair).

        Raises:
            HiveMQSourceError: If not connected or the publish fails.
        """
        if self._client is None or self._config is None:
            raise HiveMQSourceError("publish_result called before a successful list_pending/connect")

        info = self._client.publish(self._config.status_topic, json.dumps(body), qos=1)
        try:
            info.wait_for_publish(timeout)
        except (ValueError, RuntimeError) as e:
            raise HiveMQSourceError(f"Failed to publish result for {key}: {e}") from e

        with self._lock:
            msg = self._pending_msgs.pop(key, None)
        if msg is not None:
            self._client.ack(msg.mid, msg.qos)

    def close(self) -> None:
        """
        Disconnect the persistent client and release any still-unacked messages back
        to the broker (redelivered on the next connect). Safe to call when not
        connected.
        """
        if self._client is None:
            return
        try:
            self._client.disconnect()
            self._client.loop_stop()
        finally:
            self._client = None
            self._connected.clear()
            with self._lock:
                self._buffer.clear()
                self._pending_msgs.clear()

    def clear_pending(self) -> None:
        """Forget every tracked-for-ack message without acking it (peek/inspect tools)."""
        with self._lock:
            self._pending_msgs.clear()

    # ---- event-driven watch --------------------------------------------------

    def watch(self, handler: Callable[[dict], Optional[str]]) -> None:
        """
        Stay connected and dispatch each message to ``handler`` the moment it arrives
        — the event-driven (push) counterpart to ``list_pending()``. No poll interval.

        ``handler(record)`` is called on a single worker thread (so messages are
        processed one at a time, in order) and returns either:
          - a terminal status string → published to the status topic + acked, or
          - None → the message is left unacked and redelivered on the next reconnect.

        Incoming messages are handed to the worker via a queue so the network thread
        stays responsive (keepalive pings keep flowing) even while work is running.
        Blocks until KeyboardInterrupt. Auto-reconnects (persistent QoS-1 session).

        Raises:
            HiveMQSourceError: On initial connection failure.
        """
        if self._client is not None:
            self.close()
        self._config = self._make_config()
        work: "queue.Queue[dict]" = queue.Queue()
        stop = threading.Event()

        def on_connect(client, userdata, flags, reason_code, properties) -> None:
            if reason_code.is_failure:
                self._connect_error = f"broker refused connection: {reason_code}"
            else:
                self._connect_error = None
                client.subscribe(self._config.topic, qos=1)  # renewed on every reconnect
            self._connected.set()

        def on_message(client, userdata, message) -> None:
            self._log_received(message)
            rec = self._parse(message)
            if rec is None:
                client.ack(message.mid, message.qos)  # drop malformed
                return
            with self._lock:
                self._pending_msgs[self._key_of(rec)] = message
            work.put(rec)

        client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=self._config.client_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
            manual_ack=True,
        )
        client.username_pw_set(self._config.username, self._config.password)
        client.tls_set()
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = on_connect
        client.on_message = on_message

        self._connected.clear()
        self._connect_error = None
        try:
            client.connect(self._config.host, self._config.port, keepalive=60)
        except (OSError, mqtt.WebsocketConnectionError) as e:
            raise HiveMQSourceError(
                f"Failed to connect to {self._config.host}:{self._config.port}: {e}"
            ) from e

        def worker() -> None:
            while not stop.is_set():
                try:
                    rec = work.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    status = handler(rec)
                except Exception:  # handler handles its own errors; be defensive
                    status = "failed"
                if status:
                    try:
                        self.update_status(self._key_of(rec), status)
                    except HiveMQSourceError:
                        pass
                work.task_done()

        t = threading.Thread(target=worker, name=f"{self._log_prefix}-worker", daemon=True)
        t.start()
        client.loop_start()
        self._client = client

        if not self._connected.wait(CONNECT_TIMEOUT):
            stop.set()
            self.close()
            raise HiveMQSourceError(f"Timed out connecting to {self._config.host}:{self._config.port}")
        if self._connect_error:
            err = self._connect_error
            stop.set()
            self.close()
            raise HiveMQSourceError(err)

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            self.close()
