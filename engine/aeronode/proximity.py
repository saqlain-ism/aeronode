"""Fleet-level separation monitoring.

Checks every pair of airborne drones. When two get too close, the one closing
in faster is told to HOLD (the other keeps flying away from the conflict).
Once separation is restored for a while, the held drone is released.
"""

from __future__ import annotations

from dataclasses import dataclass

from .geo import bearing_deg, closing_speed, haversine_m

AIRBORNE = {"MISSION", "HOLD", "DESCEND", "RTL", "LAND"}


@dataclass
class Position:
    drone_id: str
    lat: float
    lon: float
    alt: float
    speed: float
    heading: float
    mode: str


@dataclass
class Conflict:
    a: str
    b: str
    horizontal_m: float
    vertical_m: float
    yielding: str          # drone that should hold

    @property
    def pair(self) -> tuple[str, str]:
        return tuple(sorted((self.a, self.b)))  # type: ignore[return-value]


def find_conflicts(positions: list[Position], horizontal_m: float, vertical_m: float) -> list[Conflict]:
    airborne = [p for p in positions if p.mode in AIRBORNE and p.alt > 2.0]
    conflicts = []
    for i, a in enumerate(airborne):
        for b in airborne[i + 1:]:
            h = haversine_m(a.lat, a.lon, b.lat, b.lon)
            v = abs(a.alt - b.alt)
            if h < horizontal_m and v < vertical_m:
                closing_a = closing_speed(a.speed, a.heading, bearing_deg(a.lat, a.lon, b.lat, b.lon))
                closing_b = closing_speed(b.speed, b.heading, bearing_deg(b.lat, b.lon, a.lat, a.lon))
                yielding = a.drone_id if closing_a >= closing_b else b.drone_id
                conflicts.append(Conflict(a.drone_id, b.drone_id, round(h, 1), round(v, 1), yielding))
    return conflicts


def separated(a: Position, b: Position, horizontal_m: float, vertical_m: float) -> bool:
    return haversine_m(a.lat, a.lon, b.lat, b.lon) >= horizontal_m or abs(a.alt - b.alt) >= vertical_m
