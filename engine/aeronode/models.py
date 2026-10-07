"""Core data types shared across the engine."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum, IntEnum

from pydantic import BaseModel, Field


class Telemetry(BaseModel):
    """One telemetry frame as published by a drone."""

    drone_id: str = Field(min_length=1, max_length=64)
    ts: float
    seq: int = Field(ge=0)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    alt: float
    battery: float = Field(ge=0, le=100)
    speed: float = Field(ge=0)
    heading: float = 0.0
    motor_temp: float
    signal: float = Field(ge=0, le=100)
    mode: str
    wp_index: int = 0


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "warning": 1, "critical": 2}[self.value]


class Action(IntEnum):
    """Autonomous actions, ordered by how drastic they are.

    The engine only ever *escalates*: a pending LAND is never downgraded to an
    RTL by a later, milder rule.
    """

    NONE = 0
    DESCEND = 1
    HOLD = 2
    RTL = 3
    LAND = 4


# Flight modes that already satisfy an action (no need to re-command).
SATISFIED_BY: dict[Action, set[str]] = {
    Action.DESCEND: {"DESCEND", "RTL", "LAND", "LANDED"},
    Action.HOLD: {"HOLD", "RTL", "LAND", "LANDED"},
    Action.RTL: {"RTL", "LAND", "LANDED"},
    Action.LAND: {"LAND", "LANDED"},
}


@dataclass
class Decision:
    """A rule's verdict on one telemetry frame."""

    rule: str
    event_type: str
    severity: Severity
    message: str
    action: Action = Action.NONE
    value: float | None = None
    details: dict = field(default_factory=dict)


@dataclass
class Event:
    """Something that happened, as recorded and broadcast by the engine."""

    drone_id: str
    type: str
    severity: Severity
    message: str
    action: str | None = None
    value: float | None = None
    details: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data
