"""REST API and process wiring.

Run with:  uvicorn aeronode.api:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .bus import MqttBus
from .config import Settings, load_rules
from .engine import DecisionEngine
from .models import Severity
from .notifier import Notifier
from .storage import Storage

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("aeronode.api")

COMMANDS = Literal["RTL", "LAND", "HOLD", "RESUME", "DESCEND", "RESET_SENSOR", "REBOOT"]
FAULTS = Literal["battery_drop", "sensor_spike", "sensor_stuck", "signal_loss", "flyaway",
                 "motor_overheat", "altitude_runaway", "intercept"]


class CommandRequest(BaseModel):
    command: COMMANDS


class FaultRequest(BaseModel):
    type: FAULTS
    duration: float | None = Field(default=None, gt=0, le=600, description="seconds; omit for until-cleared")
    target: str | None = Field(default=None, description="other drone, for 'intercept'")


class Runtime:
    """Long-lived components, created at startup."""

    def __init__(self) -> None:
        self.settings = Settings()
        self.storage = Storage(self.settings.influx_url, self.settings.influx_token,
                               self.settings.influx_org, self.settings.influx_bucket)
        self.notifier = Notifier(self.settings.n8n_webhook_url, self.settings.notify_min_severity)
        self.bus = MqttBus(self.settings.mqtt_host, self.settings.mqtt_port)
        self.engine = DecisionEngine(load_rules(self.settings.rules_path), self.bus.send_command,
                                     self.storage, self.notifier)
        self.started = time.time()
        self._stop = threading.Event()

    def start(self) -> None:
        self.notifier.start()
        self.bus.start(self.engine)
        threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.bus.stop()
        self.notifier.stop()
        self.storage.close()

    def metrics(self) -> dict:
        return {
            **self.engine.metrics(),
            "queue_depth": self.bus.queue.qsize(),
            "queue_dropped": self.bus.dropped,
            "influx_written": self.storage.written,
            "influx_failed": self.storage.failed,
            "alerts_sent": self.notifier.sent,
            "alerts_failed": self.notifier.failed,
            "alerts_dropped": self.notifier.dropped,
            "uptime_s": round(time.time() - self.started, 1),
        }

    def _housekeeping(self) -> None:
        interval = self.engine.rules["watchdog"]["check_interval_s"]
        last_metrics = 0.0
        while not self._stop.wait(interval):
            self.engine.check_links()
            if time.time() - last_metrics >= 5:
                last_metrics = time.time()
                self.storage.engine_metrics(last_metrics, self.metrics())


runtime: Runtime | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runtime
    runtime = Runtime()
    runtime.start()
    yield
    runtime.stop()


app = FastAPI(title="AeroNode Decision Engine", version="1.0.0", lifespan=lifespan,
              description="Real-time telemetry processing and autonomous decisions for a UAV fleet.")


def rt() -> Runtime:
    if runtime is None:
        raise HTTPException(503, "engine not started")
    return runtime


def require_key(x_api_key: str | None = Header(default=None), r: Runtime = Depends(rt)) -> None:
    if r.settings.api_key and x_api_key != r.settings.api_key:
        raise HTTPException(401, "invalid or missing X-API-Key")


@app.get("/health", tags=["ops"])
def health(r: Runtime = Depends(rt)) -> dict:
    checks = {
        "mqtt": r.bus.connected,
        "influxdb": r.storage.healthy() if r.storage.enabled else None,
        "n8n_configured": r.notifier.enabled,
    }
    ok = checks["mqtt"] and checks["influxdb"] is not False
    return {"status": "ok" if ok else "degraded", "checks": checks}


@app.get("/metrics", tags=["ops"])
def metrics(r: Runtime = Depends(rt)) -> dict:
    return r.metrics()


@app.get("/drones", tags=["fleet"])
def list_drones(r: Runtime = Depends(rt)) -> list[dict]:
    with r.engine.lock:
        return [s.snapshot() | {"recovery": r.engine.recovery.status(s.drone_id)}
                for s in sorted(r.engine.drones.values(), key=lambda s: s.drone_id)]


@app.get("/drones/{drone_id}", tags=["fleet"])
def get_drone(drone_id: str, r: Runtime = Depends(rt)) -> dict:
    with r.engine.lock:
        state = r.engine.drones.get(drone_id)
        if state is None:
            raise HTTPException(404, f"unknown drone {drone_id}")
        return state.snapshot() | {"recovery": r.engine.recovery.status(drone_id)}


@app.get("/events", tags=["fleet"])
def events(limit: int = Query(50, ge=1, le=1000), drone_id: str | None = None,
           severity: Literal["info", "warning", "critical"] | None = None,
           since: float | None = Query(None, description="unix timestamp; only newer events"),
           r: Runtime = Depends(rt)) -> list[dict]:
    with r.engine.lock:
        items = list(r.engine.events)
    if drone_id:
        wanted = set(drone_id.split(","))
        items = [e for e in items if e.drone_id in wanted]
    if since is not None:
        items = [e for e in items if e.ts >= since]
    if severity:
        items = [e for e in items if e.severity.value == severity]
    return [e.to_dict() for e in reversed(items[-limit:])]


@app.post("/drones/{drone_id}/command", tags=["control"], dependencies=[Depends(require_key)])
def command(drone_id: str, body: CommandRequest, r: Runtime = Depends(rt)) -> dict:
    with r.engine.lock:
        if drone_id not in r.engine.drones:
            raise HTTPException(404, f"unknown drone {drone_id}")
        r.engine.emit(drone_id, "MANUAL_COMMAND", Severity.INFO, f"operator sent {body.command}", body.command)
    cmd_id = r.bus.send_command(drone_id, body.command)
    return {"sent": body.command, "command_id": cmd_id}


@app.post("/drones/{drone_id}/fault", tags=["chaos"], dependencies=[Depends(require_key)])
def inject_fault(drone_id: str, body: FaultRequest, r: Runtime = Depends(rt)) -> dict:
    """Chaos testing: ask the simulator to break something on purpose."""
    if body.type == "intercept" and not body.target:
        raise HTTPException(422, "intercept needs a target drone")
    r.bus.inject_fault(drone_id, body.model_dump(exclude_none=True))
    return {"injected": body.type, "drone_id": drone_id}


@app.get("/config/rules", tags=["ops"])
def get_rules(r: Runtime = Depends(rt)) -> dict:
    return r.engine.rules


@app.post("/config/reload", tags=["ops"], dependencies=[Depends(require_key)])
def reload_rules(r: Runtime = Depends(rt)) -> dict:
    try:
        r.engine.configure(load_rules(r.settings.rules_path))
    except (OSError, ValueError) as exc:
        raise HTTPException(400, f"rules not reloaded: {exc}") from exc
    return {"reloaded": True}
