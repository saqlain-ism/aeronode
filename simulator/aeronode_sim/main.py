"""Simulator entrypoint: steps the fleet and speaks MQTT.

Topics
  publish   aeronode/telemetry/<drone_id>   telemetry JSON, TICK_HZ per drone
  publish   aeronode/ack/<drone_id>         command acknowledgements
  subscribe aeronode/command/<drone_id>     {"command": "RTL", "id": "..."}
  subscribe aeronode/fault/<drone_id>       {"type": "sensor_spike", "duration": 30}
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import time

import paho.mqtt.client as mqtt

from .fleet import build_fleet

log = logging.getLogger("aeronode.sim")


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class Simulator:
    def __init__(self) -> None:
        self.host = env("MQTT_HOST", "localhost")
        self.port = int(env("MQTT_PORT", "1883"))
        self.tick_hz = float(env("TICK_HZ", "1"))
        self.fleet = build_fleet(
            count=int(env("DRONE_COUNT", "4")),
            home_lat=float(env("HOME_LAT", "17.4474")),
            home_lon=float(env("HOME_LON", "78.3762")),
            seed=int(env("SEED", "7")),
        )
        # MQTT callbacks run on the network thread; the sim loop owns drone
        # state, so incoming messages are queued and applied between ticks.
        self.inbox: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.running = True
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="aeronode-simulator")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        log.info("connected to mqtt://%s:%s (%s)", self.host, self.port, reason_code)
        client.subscribe([("aeronode/command/+", 1), ("aeronode/fault/+", 1)])

    def _on_message(self, client, userdata, msg) -> None:
        try:
            self.inbox.put((msg.topic, json.loads(msg.payload)))
        except json.JSONDecodeError:
            log.warning("ignoring malformed payload on %s", msg.topic)

    def _drain_inbox(self, now: float) -> None:
        while not self.inbox.empty():
            topic, payload = self.inbox.get_nowait()
            _, kind, drone_id = topic.split("/", 2)
            drone = self.fleet.get(drone_id)
            if drone is None:
                continue
            if kind == "command":
                command = str(payload.get("command", ""))
                accepted, reason = drone.apply_command(command, now)
                log.info("%s command %s -> %s (%s)", drone_id, command, accepted, reason)
                ack = {"drone_id": drone_id, "command": command, "id": payload.get("id"),
                       "accepted": accepted, "reason": reason, "mode": drone.mode.value, "ts": now}
                self.client.publish(f"aeronode/ack/{drone_id}", json.dumps(ack), qos=1)
            elif kind == "fault":
                try:
                    drone.inject_fault(payload["type"], now, payload.get("duration"), payload.get("target"))
                    log.warning("%s fault injected: %s", drone_id, payload)
                except (KeyError, ValueError) as exc:
                    log.warning("bad fault request %s: %s", payload, exc)

    def run(self) -> None:
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()
        dt = 1.0 / self.tick_hz
        log.info("simulating %d drones at %.1f Hz", len(self.fleet), self.tick_hz)
        next_tick = time.monotonic()
        while self.running:
            now = time.time()
            self._drain_inbox(now)
            for drone in self.fleet.values():
                drone.step(now, dt, self.fleet)
                if not drone.is_offline(now) and self.client.is_connected():
                    payload = json.dumps(drone.telemetry(now))
                    self.client.publish(f"aeronode/telemetry/{drone.drone_id}", payload, qos=0)
            next_tick += dt
            time.sleep(max(0.0, next_tick - time.monotonic()))
        self.client.loop_stop()
        self.client.disconnect()

    def stop(self, *_: object) -> None:
        self.running = False


def main() -> None:
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sim = Simulator()
    signal.signal(signal.SIGTERM, sim.stop)
    signal.signal(signal.SIGINT, sim.stop)
    sim.run()


if __name__ == "__main__":
    main()
