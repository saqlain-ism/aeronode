"""Predictive battery failsafe.

A fixed "RTL below 25%" threshold is wrong in both directions: a drone hovering
next to home can safely fly much lower, while a drone 900 m out in a headwind
may not make it back from 30%. Instead we:

1. fit a least-squares line to recent (time, battery) samples to get the
   current drain rate,
2. estimate time until the battery hits the reserve level,
3. estimate time needed to get home and land,
4. trigger RTL when time-to-reserve < safety_factor x time-to-home.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


def linear_slope(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of y over x. None if undefined."""
    n = len(points)
    if n < 2:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    if sxx == 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    return sxy / sxx


@dataclass
class BatteryForecast:
    drain_pct_per_s: float
    seconds_to_reserve: float
    seconds_to_home: float
    must_return: bool


class BatteryPredictor:
    def __init__(self, reserve_pct: float, safety_factor: float, window_s: float, min_samples: int,
                 cruise_speed: float, descent_rate: float) -> None:
        self.reserve_pct = reserve_pct
        self.safety_factor = safety_factor
        self.window_s = window_s
        self.min_samples = min_samples
        self.cruise_speed = cruise_speed
        self.descent_rate = descent_rate
        self._samples: dict[str, deque[tuple[float, float]]] = {}

    def reset(self, drone_id: str) -> None:
        self._samples.pop(drone_id, None)

    def update(self, drone_id: str, ts: float, battery: float, distance_home_m: float,
               alt_m: float) -> BatteryForecast | None:
        samples = self._samples.setdefault(drone_id, deque())
        samples.append((ts, battery))
        while samples and ts - samples[0][0] > self.window_s:
            samples.popleft()
        if len(samples) < self.min_samples:
            return None

        slope = linear_slope(list(samples))
        if slope is None or slope >= 0:      # charging or flat: nothing to predict
            return None
        drain = -slope
        to_reserve = max(0.0, (battery - self.reserve_pct) / drain)
        to_home = distance_home_m / self.cruise_speed + alt_m / self.descent_rate
        return BatteryForecast(
            drain_pct_per_s=round(drain, 4),
            seconds_to_reserve=round(to_reserve, 1),
            seconds_to_home=round(to_home, 1),
            must_return=to_reserve < self.safety_factor * to_home,
        )
