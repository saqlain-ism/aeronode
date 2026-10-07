"""InfluxDB writer.

Writes are batched in the background, so a slow or unavailable database never
blocks decision making - the engine keeps flying the fleet and simply counts
the points it couldn't store.
"""

from __future__ import annotations

import logging
import threading

from .models import Event, Telemetry

log = logging.getLogger("aeronode.storage")

try:
    from influxdb_client import InfluxDBClient, Point, WriteOptions
except ImportError:  # pragma: no cover - optional for unit tests
    InfluxDBClient = None  # type: ignore[assignment]


class Storage:
    def __init__(self, url: str, token: str, org: str, bucket: str) -> None:
        self.bucket = bucket
        self.org = org
        self.written = 0
        self.failed = 0
        self._lock = threading.Lock()
        self._client = None
        self._write = None
        if not token or InfluxDBClient is None:
            log.warning("InfluxDB disabled (no token configured) - telemetry will not be stored")
            return
        self._client = InfluxDBClient(url=url, token=token, org=org, timeout=5_000)
        self._write = self._client.write_api(
            write_options=WriteOptions(batch_size=500, flush_interval=1_000, retry_interval=2_000, max_retries=3),
            success_callback=self._ok, error_callback=self._err, retry_callback=None,
        )

    @property
    def enabled(self) -> bool:
        return self._write is not None

    def _ok(self, conf, data: str) -> None:
        with self._lock:
            self.written += data.count("\n") + 1

    def _err(self, conf, data: str, exc: Exception) -> None:
        with self._lock:
            self.failed += data.count("\n") + 1
        log.warning("influx write failed: %s", exc)

    def healthy(self) -> bool:
        if not self._client:
            return False
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def _put(self, point) -> None:
        if self._write is not None:
            self._write.write(bucket=self.bucket, org=self.org, record=point)

    def telemetry(self, t: Telemetry, distance_home_m: float, glitchy: bool) -> None:
        if not self.enabled:
            return
        self._put(
            Point("telemetry").tag("drone_id", t.drone_id).tag("mode", t.mode)
            .field("lat", t.lat).field("lon", t.lon).field("alt", t.alt)
            .field("battery", t.battery).field("speed", t.speed).field("heading", t.heading)
            .field("motor_temp", t.motor_temp).field("signal", t.signal)
            .field("distance_home", round(distance_home_m, 1)).field("glitch", int(glitchy))
            .field("seq", t.seq).time(int(t.ts * 1e9))
        )

    def forecast(self, drone_id: str, ts: float, seconds_to_reserve: float, seconds_to_home: float,
                 drain: float) -> None:
        if not self.enabled:
            return
        self._put(
            Point("battery_forecast").tag("drone_id", drone_id)
            .field("seconds_to_reserve", seconds_to_reserve).field("seconds_to_home", seconds_to_home)
            .field("drain_pct_per_s", drain).time(int(ts * 1e9))
        )

    def event(self, e: Event) -> None:
        if not self.enabled:
            return
        p = (Point("events").tag("drone_id", e.drone_id).tag("type", e.type)
             .tag("severity", e.severity.value).tag("action", e.action or "NONE")
             .field("message", e.message).time(int(e.ts * 1e9)))
        if e.value is not None:
            p = p.field("value", float(e.value))
        self._put(p)

    def engine_metrics(self, ts: float, metrics: dict) -> None:
        if not self.enabled:
            return
        p = Point("engine").time(int(ts * 1e9))
        for k, v in metrics.items():
            if isinstance(v, (int, float)):
                p = p.field(k, float(v))
        self._put(p)

    def close(self) -> None:
        if self._write is not None:
            self._write.close()
        if self._client is not None:
            self._client.close()
