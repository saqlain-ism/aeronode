"""Runtime settings (environment) and decision rules (YAML)."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _env(name: str, default: str) -> Callable[[], str]:
    return lambda: os.environ.get(name, default)


DEFAULT_RULES = str(Path(__file__).resolve().parents[2] / "config" / "rules.yaml")


@dataclass(frozen=True)
class Settings:
    """Read from the environment when instantiated (not at import time)."""

    mqtt_host: str = field(default_factory=_env("MQTT_HOST", "localhost"))
    mqtt_port: int = field(default_factory=lambda: int(os.environ.get("MQTT_PORT", "1883")))
    influx_url: str = field(default_factory=_env("INFLUX_URL", "http://localhost:8086"))
    influx_token: str = field(default_factory=_env("INFLUX_TOKEN", ""))
    influx_org: str = field(default_factory=_env("INFLUX_ORG", "aeronode"))
    influx_bucket: str = field(default_factory=_env("INFLUX_BUCKET", "telemetry"))
    n8n_webhook_url: str = field(default_factory=_env("N8N_WEBHOOK_URL", ""))
    notify_min_severity: str = field(default_factory=_env("NOTIFY_MIN_SEVERITY", "warning"))
    rules_path: str = field(default_factory=_env("RULES_PATH", DEFAULT_RULES))
    api_key: str = field(default_factory=_env("API_KEY", ""))


def load_rules(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        rules = yaml.safe_load(fh)
    required = {"home", "battery", "geofence", "altitude", "motor_temp", "proximity", "anomaly", "recovery", "watchdog", "decisions"}
    missing = required - rules.keys()
    if missing:
        raise ValueError(f"rules file missing sections: {sorted(missing)}")
    return rules
