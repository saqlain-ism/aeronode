"""API smoke tests. MQTT/InfluxDB/n8n are not running: the engine must still
start and serve requests in a degraded state."""

import pytest
from fastapi.testclient import TestClient

from aeronode import api
from aeronode.models import Telemetry


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "127.0.0.1")
    monkeypatch.setenv("MQTT_PORT", "1")          # nothing listens here
    monkeypatch.delenv("INFLUX_TOKEN", raising=False)
    with TestClient(api.app) as c:
        yield c


def test_health_reports_degraded_without_broker(client):
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["checks"]["mqtt"] is False


def test_drones_and_events(client):
    api.runtime.engine.process(Telemetry(drone_id="uav-01", ts=1.0, seq=1, lat=17.4474, lon=78.3762, alt=50,
                                         battery=10, speed=5, heading=0, motor_temp=50, signal=90, mode="MISSION"))
    drones = client.get("/drones").json()
    assert drones[0]["drone_id"] == "uav-01"
    assert client.get("/drones/uav-01").json()["commanded"] == "LAND"
    assert client.get("/drones/nope").status_code == 404
    events = client.get("/events", params={"severity": "critical"}).json()
    assert any(e["type"] == "BATTERY_CRITICAL" for e in events)


def test_validation(client):
    assert client.post("/drones/uav-01/fault", json={"type": "gremlins"}).status_code == 422
    assert client.post("/drones/uav-01/fault", json={"type": "intercept"}).status_code == 422
    assert client.post("/drones/uav-01/fault", json={"type": "sensor_spike"}).status_code == 200


def test_metrics(client):
    m = client.get("/metrics").json()
    assert {"frames_per_s", "latency_ms_p95", "queue_depth", "uptime_s"} <= m.keys()


def test_events_filters(client):
    eng = api.runtime.engine
    for i, drone in enumerate(["uav-01", "uav-02", "uav-03"]):
        eng.clock = lambda i=i: 1000.0 + i
        eng.emit(drone, "TEST", api.Severity.INFO, "x")
    got = client.get("/events", params={"drone_id": "uav-01,uav-03"}).json()
    assert {e["drone_id"] for e in got} == {"uav-01", "uav-03"}
    got = client.get("/events", params={"since": 1001.0}).json()
    assert {e["drone_id"] for e in got} == {"uav-02", "uav-03"}
