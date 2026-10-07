from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aeronode.engine import DecisionEngine
from aeronode.models import Telemetry

RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "rules.yaml"
HOME = (17.4474, 78.3762)


@pytest.fixture
def rules() -> dict:
    return yaml.safe_load(RULES_PATH.read_text())


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Harness:
    """Engine wired to a fake clock and a command recorder."""

    def __init__(self, rules: dict) -> None:
        self.clock = FakeClock()
        self.commands: list[tuple[str, str]] = []
        self.engine = DecisionEngine(rules, self._send, clock=self.clock)
        self.seq: dict[str, int] = {}

    def _send(self, drone_id: str, command: str) -> str:
        self.commands.append((drone_id, command))
        return f"cmd-{len(self.commands)}"

    def frame(self, drone_id: str = "uav-01", advance: float = 1.0, **overrides) -> Telemetry:
        self.clock.t += advance
        self.seq[drone_id] = self.seq.get(drone_id, 0) + 1
        data = dict(drone_id=drone_id, ts=self.clock.t, seq=self.seq[drone_id], lat=HOME[0], lon=HOME[1],
                    alt=50.0, battery=90.0, speed=8.0, heading=0.0, motor_temp=50.0, signal=95.0,
                    mode="MISSION")
        data.update(overrides)
        t = Telemetry(**data)
        self.engine.process(t)
        return t

    def types(self, drone_id: str | None = None) -> list[str]:
        return [e.type for e in self.engine.events if drone_id is None or e.drone_id == drone_id]


@pytest.fixture
def h(rules) -> Harness:
    return Harness(rules)
