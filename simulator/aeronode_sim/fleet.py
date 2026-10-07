"""Builds a fleet of drones flying distinct patrol patterns around one launch site."""

from __future__ import annotations

import math
import random

from .drone import Drone
from .geo import offset


def patrol_route(home_lat: float, home_lon: float, radius_m: float, points: int, phase: float) -> list[tuple[float, float]]:
    """Polygon of waypoints on a circle around home."""
    route = []
    for i in range(points):
        angle = phase + 2 * math.pi * i / points
        route.append(offset(home_lat, home_lon, radius_m * math.cos(angle), radius_m * math.sin(angle)))
    return route


def build_fleet(count: int, home_lat: float, home_lon: float, seed: int = 7) -> dict[str, Drone]:
    rng = random.Random(seed)
    fleet: dict[str, Drone] = {}
    for i in range(count):
        drone_id = f"uav-{i + 1:02d}"
        # Separate drones vertically (15 m layers) and by patrol radius so the
        # healthy fleet never conflicts; collisions only happen via fault injection.
        fleet[drone_id] = Drone(
            drone_id=drone_id,
            home_lat=home_lat,
            home_lon=home_lon,
            cruise_alt=45.0 + 15.0 * i,
            waypoints=patrol_route(home_lat, home_lon, 250.0 + 110.0 * i, 6, phase=i * 0.7),
            launch_delay=12.0 * i,
            rng=random.Random(rng.random()),
        )
    return fleet
