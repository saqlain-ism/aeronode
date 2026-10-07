"""Simulated UAV with simple kinematics, a battery model and injectable faults.

The model is intentionally lightweight: it is not a flight-dynamics simulator,
it produces *believable telemetry* so the decision engine has something real to
react to, including failure modes that are hard to reproduce with hardware.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum

from .geo import bearing_deg, haversine_m, offset


class Mode(str, Enum):
    MISSION = "MISSION"
    HOLD = "HOLD"
    RTL = "RTL"            # return to launch
    LAND = "LAND"
    DESCEND = "DESCEND"    # drop to a safe altitude, then resume mission
    LANDED = "LANDED"


COMMANDS = {"RTL", "LAND", "HOLD", "RESUME", "DESCEND", "RESET_SENSOR", "REBOOT"}
FAULTS = {
    "battery_drop",     # cell failure: instant drop + faster drain
    "sensor_spike",     # noisy altimeter / thermistor; cleared by RESET_SENSOR
    "sensor_stuck",     # same symptoms, but only a REBOOT clears it
    "signal_loss",      # stops transmitting for `duration` seconds
    "flyaway",          # corrupted navigation: flies straight away from home
    "altitude_runaway", # bad altitude setpoint: keeps climbing
    "motor_overheat",   # motor temperature climbs steadily
    "intercept",        # flies towards another drone (collision scenario)
}

CRUISE_SPEED = 8.0      # m/s
CLIMB_RATE = 3.0        # m/s
DESCENT_RATE = 2.5      # m/s
WAYPOINT_RADIUS = 6.0   # m
TURNAROUND_S = 20.0     # time on the ground "recharging" before relaunch


@dataclass
class Fault:
    kind: str
    until: float | None          # None = until cleared by a command
    target: str | None = None    # used by "intercept"


@dataclass
class Drone:
    drone_id: str
    home_lat: float
    home_lon: float
    cruise_alt: float
    waypoints: list[tuple[float, float]]
    rng: random.Random = field(default_factory=random.Random)

    lat: float = 0.0
    lon: float = 0.0
    alt: float = 0.0
    battery: float = 100.0
    speed: float = 0.0
    heading: float = 0.0
    motor_temp: float = 32.0
    mode: Mode = Mode.LANDED
    launch_delay: float = 0.0     # staggered take-off, seconds after start
    wp_index: int = 0
    seq: int = 0
    faults: dict[str, Fault] = field(default_factory=dict)
    offline_until: float = 0.0
    ground_s: float = 0.0         # time spent on the pad since landing
    _descend_target: float = 0.0

    def __post_init__(self) -> None:
        self.lat, self.lon = self.home_lat, self.home_lon
        # Start on the pad; first launch happens after `launch_delay`.
        self.ground_s = TURNAROUND_S - self.launch_delay

    # ------------------------------------------------------------------ commands
    def apply_command(self, command: str, now: float) -> tuple[bool, str]:
        """Apply a command from the ground station. Returns (accepted, reason)."""
        command = command.upper()
        if command not in COMMANDS:
            return False, f"unknown command {command}"
        if self.is_offline(now) and command != "REBOOT":
            return False, "drone offline"

        if command == "RESET_SENSOR":
            if "sensor_spike" in self.faults:
                del self.faults["sensor_spike"]
                return True, "sensor recalibrated"
            if "sensor_stuck" in self.faults:
                return True, "sensor reset had no effect"
            return True, "sensor already healthy"

        if command == "REBOOT":
            # Flight controller restart: link drops briefly, transient faults clear.
            self.offline_until = now + 4.0
            for kind in ("sensor_spike", "sensor_stuck"):
                self.faults.pop(kind, None)
            return True, "rebooting"

        if command == "RTL":
            self.faults.pop("flyaway", None)            # RTL uses a fresh nav solution
        if command == "RESUME":
            if self.mode == Mode.LANDED:
                return False, "landed; waits for turnaround"
            self.mode = Mode.MISSION
            return True, "mission resumed"

        if self.mode == Mode.LANDED:
            return False, "already landed"
        if command == "DESCEND":
            self._descend_target = max(20.0, self.alt - 40.0)
            self.faults.pop("altitude_runaway", None)   # operator setpoint wins
        self.mode = Mode(command)
        return True, f"mode -> {command}"

    def inject_fault(self, kind: str, now: float, duration: float | None, target: str | None = None) -> None:
        if kind not in FAULTS:
            raise ValueError(f"unknown fault {kind}")
        until = now + duration if duration else None
        if kind == "battery_drop":
            self.battery = max(5.0, self.battery - 35.0)
        if kind == "signal_loss":
            self.offline_until = now + (duration or 10.0)
            return
        self.faults[kind] = Fault(kind, until, target)

    # ------------------------------------------------------------------ physics
    def is_offline(self, now: float) -> bool:
        return now < self.offline_until

    def _expire_faults(self, now: float) -> None:
        for kind in [k for k, f in self.faults.items() if f.until is not None and now >= f.until]:
            del self.faults[kind]

    def _move_towards(self, lat: float, lon: float, dt: float, speed: float = CRUISE_SPEED) -> float:
        dist = haversine_m(self.lat, self.lon, lat, lon)
        if dist < 0.5:
            self.speed = 0.0
            return dist
        self.heading = bearing_deg(self.lat, self.lon, lat, lon)
        step = min(speed * dt, dist)
        rad = math.radians(self.heading)
        self.lat, self.lon = offset(self.lat, self.lon, step * math.cos(rad), step * math.sin(rad))
        self.speed = step / dt
        return dist - step

    def _climb_to(self, target: float, dt: float) -> None:
        if self.alt < target:
            self.alt = min(target, self.alt + CLIMB_RATE * dt)
        elif self.alt > target:
            self.alt = max(target, self.alt - DESCENT_RATE * dt)

    def _fly_mission(self, dt: float, fleet: dict[str, Drone]) -> None:
        intercept = self.faults.get("intercept")
        if intercept and intercept.target in fleet and fleet[intercept.target].alt > 2:
            # Chase another drone, faster than cruise, matching its altitude.
            other = fleet[intercept.target]
            self._climb_to(other.alt, dt)
            self._move_towards(other.lat, other.lon, dt, speed=CRUISE_SPEED * 1.6)
            return
        if "flyaway" in self.faults:
            # Navigation fault: hold the outbound bearing and keep going.
            away = bearing_deg(self.home_lat, self.home_lon, self.lat, self.lon)
            far = offset(self.lat, self.lon, 1000 * math.cos(math.radians(away)), 1000 * math.sin(math.radians(away)))
            self._climb_to(self.cruise_alt, dt)
            self._move_towards(*far, dt)
            return
        if "altitude_runaway" in self.faults:
            self.cruise_alt += CLIMB_RATE * 0.8 * dt
        self._climb_to(self.cruise_alt, dt)
        remaining = self._move_towards(*self.waypoints[self.wp_index], dt)
        if remaining < WAYPOINT_RADIUS:
            self.wp_index = (self.wp_index + 1) % len(self.waypoints)

    def step(self, now: float, dt: float, fleet: dict[str, Drone]) -> None:
        self._expire_faults(now)
        prev_alt = self.alt

        if self.mode == Mode.LANDED:
            self.speed = 0.0
            self.alt = 0.0
            self.battery = min(100.0, self.battery + 4.0 * dt)  # pad charger
            self.ground_s += dt
            if self.ground_s >= TURNAROUND_S and self.battery >= 99:
                self.mode, self.wp_index = Mode.MISSION, 0
                self.faults.pop("battery_drop", None)
        elif self.mode == Mode.MISSION:
            self._fly_mission(dt, fleet)
        elif self.mode == Mode.HOLD:
            self.speed = 0.0
        elif self.mode == Mode.DESCEND:
            self.speed = 0.0
            self._climb_to(self._descend_target, dt)
            if abs(self.alt - self._descend_target) < 0.5:
                self.cruise_alt = self._descend_target
                self.mode = Mode.MISSION
        elif self.mode == Mode.RTL:
            remaining = self._move_towards(self.home_lat, self.home_lon, dt)
            if remaining < 2.0:
                self.mode = Mode.LAND
        elif self.mode == Mode.LAND:
            self.speed = 0.0
            self.alt = max(0.0, self.alt - DESCENT_RATE * dt)
            if self.alt == 0.0:
                self.mode, self.ground_s = Mode.LANDED, 0.0
                for kind in ("motor_overheat", "flyaway", "intercept", "altitude_runaway"):
                    self.faults.pop(kind, None)   # motors off / on the pad

        self._update_battery(dt, climb=max(0.0, self.alt - prev_alt))
        self._update_motor_temp(dt)

    def _update_battery(self, dt: float, climb: float) -> None:
        if self.mode == Mode.LANDED:
            return
        drain = 0.05 + 0.006 * self.speed + 0.02 * climb   # % per second (~17 min endurance)
        if "battery_drop" in self.faults:
            drain *= 3.0
        self.battery = max(0.0, self.battery - drain * dt)

    def _update_motor_temp(self, dt: float) -> None:
        target = 30.0 if self.mode == Mode.LANDED else 42.0 + 1.5 * self.speed
        if "motor_overheat" in self.faults:
            self.motor_temp += 1.2 * dt
        else:
            self.motor_temp += (target - self.motor_temp) * 0.1 * dt

    # ------------------------------------------------------------------ output
    def telemetry(self, now: float) -> dict:
        """Sensor readings as the flight controller would report them."""
        self.seq += 1
        n = self.rng.gauss
        alt = self.alt + n(0, 0.3)
        temp = self.motor_temp + n(0, 0.2)
        if "sensor_spike" in self.faults or "sensor_stuck" in self.faults:
            if self.rng.random() < 0.5:
                alt += self.rng.choice([-1, 1]) * self.rng.uniform(40, 90)
                temp += self.rng.uniform(15, 30)
        dist_home = haversine_m(self.lat, self.lon, self.home_lat, self.home_lon)
        signal = max(0.0, min(100.0, 100.0 - dist_home / 25.0 + n(0, 1.0)))
        return {
            "drone_id": self.drone_id,
            "ts": now,
            "seq": self.seq,
            "lat": round(self.lat, 7),
            "lon": round(self.lon, 7),
            "alt": round(max(0.0, alt), 2),
            "battery": round(self.battery, 2),
            "speed": round(self.speed + abs(n(0, 0.1)) if self.speed else 0.0, 2),
            "heading": round(self.heading, 1),
            "motor_temp": round(temp, 2),
            "signal": round(signal, 1),
            "mode": self.mode.value,
            "wp_index": self.wp_index,
        }
