"""Per-drone safety rules.

Each rule is a small pure function: (frame, context, config) -> Decision | None.
Keeping them pure makes them trivial to unit-test and to reason about; all
state (cooldowns, escalation, recovery) lives in the engine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import Action, Decision, Severity
from .predictor import BatteryForecast


@dataclass
class Frame:
    """Telemetry values the rules are allowed to trust (glitches already replaced)."""

    drone_id: str
    mode: str
    alt: float
    battery: float
    motor_temp: float
    signal: float
    distance_home_m: float
    forecast: BatteryForecast | None = None


Rule = Callable[[Frame, dict], "Decision | None"]

AIRBORNE = {"MISSION", "HOLD", "DESCEND", "RTL", "LAND"}


def battery_rule(f: Frame, cfg: dict) -> Decision | None:
    c = cfg["battery"]
    if f.mode == "LANDED":
        return None
    if f.battery < c["land_below"]:
        return Decision("battery", "BATTERY_CRITICAL", Severity.CRITICAL,
                        f"battery {f.battery:.0f}% below {c['land_below']}% - landing now", Action.LAND, f.battery)
    if f.battery < c["rtl_below"]:
        return Decision("battery", "BATTERY_LOW", Severity.WARNING,
                        f"battery {f.battery:.0f}% below {c['rtl_below']}% - returning home", Action.RTL, f.battery)
    if f.battery < c["warn_below"]:
        return Decision("battery", "BATTERY_WARNING", Severity.INFO,
                        f"battery {f.battery:.0f}%", Action.NONE, f.battery)
    return None


def predictive_battery_rule(f: Frame, cfg: dict) -> Decision | None:
    c = cfg.get("predictive_battery", {})
    if not c.get("enabled") or f.forecast is None or f.mode not in {"MISSION", "HOLD", "DESCEND"}:
        return None
    fc = f.forecast
    if not fc.must_return:
        return None
    return Decision(
        "predictive_battery", "BATTERY_PREDICTIVE_RTL", Severity.WARNING,
        f"reserve in {fc.seconds_to_reserve:.0f}s but home is {fc.seconds_to_home:.0f}s away - returning early",
        Action.RTL, f.battery,
        {"drain_pct_per_s": fc.drain_pct_per_s, "seconds_to_reserve": fc.seconds_to_reserve,
         "seconds_to_home": fc.seconds_to_home},
    )


def geofence_rule(f: Frame, cfg: dict) -> Decision | None:
    c = cfg["geofence"]
    if f.mode not in AIRBORNE:
        return None
    radius = c["radius_m"]
    if f.distance_home_m > radius:
        return Decision("geofence", "GEOFENCE_BREACH", Severity.CRITICAL,
                        f"{f.distance_home_m:.0f} m from home, fence is {radius} m - returning home",
                        Action.RTL, f.distance_home_m)
    if f.distance_home_m > radius * c["warn_ratio"] and f.mode not in {"RTL", "LAND"}:
        return Decision("geofence", "GEOFENCE_WARNING", Severity.WARNING,
                        f"{f.distance_home_m:.0f} m from home, approaching {radius} m fence",
                        Action.NONE, f.distance_home_m)
    return None


def altitude_rule(f: Frame, cfg: dict) -> Decision | None:
    ceiling = cfg["altitude"]["max_m"]
    if f.mode in AIRBORNE and f.alt > ceiling:
        return Decision("altitude", "ALTITUDE_LIMIT", Severity.WARNING,
                        f"altitude {f.alt:.0f} m above {ceiling} m ceiling - descending", Action.DESCEND, f.alt)
    return None


def motor_temp_rule(f: Frame, cfg: dict) -> Decision | None:
    c = cfg["motor_temp"]
    if f.mode == "LANDED":
        return None
    if f.motor_temp > c["land_above"]:
        return Decision("motor_temp", "MOTOR_TEMP_CRITICAL", Severity.CRITICAL,
                        f"motor {f.motor_temp:.0f} C above {c['land_above']} C - landing", Action.LAND, f.motor_temp)
    if f.motor_temp > c["warn_above"]:
        return Decision("motor_temp", "MOTOR_TEMP_HIGH", Severity.WARNING,
                        f"motor {f.motor_temp:.0f} C", Action.NONE, f.motor_temp)
    return None


def signal_rule(f: Frame, cfg: dict) -> Decision | None:
    limit = cfg.get("signal", {}).get("warn_below")
    if limit is not None and f.mode in AIRBORNE and f.signal < limit:
        return Decision("signal", "SIGNAL_WEAK", Severity.WARNING,
                        f"link quality {f.signal:.0f}%", Action.NONE, f.signal)
    return None


RULES: list[Rule] = [
    battery_rule,
    predictive_battery_rule,
    geofence_rule,
    altitude_rule,
    motor_temp_rule,
    signal_rule,
]


def evaluate(frame: Frame, cfg: dict) -> list[Decision]:
    return [d for rule in RULES if (d := rule(frame, cfg)) is not None]
