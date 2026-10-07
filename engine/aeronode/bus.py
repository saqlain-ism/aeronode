"""MQTT transport.

The paho network thread only parses and enqueues; a dedicated worker thread
runs the decision pipeline. That keeps the broker connection responsive even
if a frame takes longer to process, and gives us a queue depth to monitor
(back-pressure is visible instead of silent).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from .engine import DecisionEngine
from .models import Telemetry

log = logging.getLogger("aeronode.bus")

TELEMETRY_TOPIC = "aeronode/telemetry/+"
ACK_TOPIC = "aeronode/ack/+"


class MqttBus:
    def __init__(self, host: str, port: int, max_queue: int = 10_000) -> None:
        self.host, self.port = host, port
        self.engine: DecisionEngine | None = None
        self.queue: queue.Queue[tuple[str, bytes, float] | None] = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"aeronode-engine-{uuid.uuid4().hex[:6]}")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self._worker = threading.Thread(target=self._work, name="decision-worker", daemon=True)

    @property
    def connected(self) -> bool:
        return self.client.is_connected()

    def start(self, engine: DecisionEngine) -> None:
        self.engine = engine
        self._worker.start()
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()
        self.queue.put(None)
        self._worker.join(timeout=5)

    # ---------------------------------------------------------------- inbound
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        log.info("connected to mqtt://%s:%s (%s)", self.host, self.port, reason_code)
        client.subscribe([(TELEMETRY_TOPIC, 0), (ACK_TOPIC, 1)])

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        log.warning("mqtt disconnected (%s) - will reconnect", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            self.queue.put_nowait((msg.topic, msg.payload, time.time()))
        except queue.Full:
            self.dropped += 1

    def _work(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            topic, payload, received_at = item
            try:
                data = json.loads(payload)
                if topic.startswith("aeronode/telemetry/"):
                    self.engine.process(Telemetry.model_validate(data), received_at)
                elif topic.startswith("aeronode/ack/"):
                    self.engine.ack(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                self.engine.invalid_frames += 1
                log.warning("invalid message on %s: %s", topic, str(exc).splitlines()[0])
            except Exception:  # never let one bad frame kill the worker
                log.exception("error processing message on %s", topic)

    # ---------------------------------------------------------------- outbound
    def send_command(self, drone_id: str, command: str) -> str:
        cmd_id = uuid.uuid4().hex[:8]
        payload = json.dumps({"command": command, "id": cmd_id, "ts": time.time()})
        self.client.publish(f"aeronode/command/{drone_id}", payload, qos=1)
        return cmd_id

    def inject_fault(self, drone_id: str, fault: dict) -> None:
        self.client.publish(f"aeronode/fault/{drone_id}", json.dumps(fault), qos=1)
