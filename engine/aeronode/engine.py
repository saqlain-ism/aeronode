"""The decision engine: telemetry in, autonomous decisions out.

Per frame the pipeline is:

    validate -> sequence check -> link/mode bookkeeping
      -> glitch detection (untrusted readings replaced by trend estimate)
      -> battery forecast -> safety rules -> sensor recovery
      -> fleet separation check -> act (escalate-only) -> record + alert

Everything here is synchronous and deterministic given a clock, which is what
makes it unit-testable without MQTT, InfluxDB or n8n running.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .anomaly import AnomalyDetector
from .geo import haversine_m
from .models import SATISFIED_BY, Action, Decision, Event, Severity, Telemetry
from .predictor import BatteryForecast, BatteryPredictor
from .proximity import Position, find_conflicts, separated
from .recovery import RecoveryManager
from .rules import Frame, evaluate

log = logging.getLogger("aeronode.engine")

CommandSender = Callable[[str, str], str]   # (drone_id, command) -> command id


@dataclass
class DroneState:
    drone_id: str
    last: Telemetry | None = None
    last_seen: float = 0.0
    link_up: bool = True
    link_lost_at: float | None = None
    mode: str | None = None
    commanded: Action = Action.NONE
    commanded_rule: str | None = None
    commanded_at: float = 0.0
    held_for: str | None = None          # drone we're yielding to (proximity)
    quiet_until: float = 0.0             # expected silence (we rebooted it)
    clear_since: float | None = None
    forecast: BatteryForecast | None = None
    glitch_frames: int = 0
    frames: int = 0
    lost_frames: int = 0
    stale_frames: int = 0
    last_fired: dict[str, float] = field(default_factory=dict)

    def snapshot(self) -> dict:
        t = self.last
        return {
            "drone_id": self.drone_id,
            "link_up": self.link_up,
            "last_seen": self.last_seen,
            "mode": self.mode,
            "commanded": self.commanded.name,
            "commanded_by": self.commanded_rule,
            "holding_for": self.held_for,
            "telemetry": t.model_dump() if t else None,
            "battery_forecast": vars(self.forecast) if self.forecast else None,
            "stats": {"frames": self.frames, "lost": self.lost_frames, "stale": self.stale_frames,
                      "glitches": self.glitch_frames},
        }


class DecisionEngine:
    def __init__(self, rules: dict, send_command: CommandSender, storage=None, notifier=None,
                 clock: Callable[[], float] = time.time) -> None:
        self.send_command = send_command
        self.storage = storage
        self.notifier = notifier
        self.clock = clock
        self.lock = threading.RLock()
        self.drones: dict[str, DroneState] = {}
        self.events: deque[Event] = deque(maxlen=1000)
        self.event_counts: Counter[str] = Counter()
        self.latencies_ms: deque[float] = deque(maxlen=2000)
        self.frame_times: deque[float] = deque(maxlen=5000)
        self.invalid_frames = 0
        self.configure(rules)

    # ------------------------------------------------------------------ setup
    def configure(self, rules: dict) -> None:
        with self.lock:
            self.rules = rules
            self.home = (rules["home"]["lat"], rules["home"]["lon"])
            a = rules["anomaly"]
            self.detector = AnomalyDetector(a["metrics"], a["window"], a["min_samples"], a["z_threshold"],
                                            a["min_residual"], a.get("confirm_shift", 3))
            self.reboot_grace_s = rules["recovery"].get("reboot_grace_s", 10)
            p = rules.get("predictive_battery", {})
            self.predictor = BatteryPredictor(p.get("reserve_pct", 10), p.get("safety_factor", 1.3),
                                              p.get("window_s", 60), p.get("min_samples", 15),
                                              p.get("cruise_speed", 8.0), p.get("descent_rate", 2.5))
            r = rules["recovery"]
            self.recovery = RecoveryManager(r["trigger_count"], r["trigger_window_s"], r["verify_s"],
                                            r["healthy_streak"], r["ladder"])
            d = rules["decisions"]
            self.cooldown_s = d["cooldown_s"]
            self.resend_after_s = d["resend_after_s"]

    # ------------------------------------------------------------------ events
    def emit(self, drone_id: str, type_: str, severity: Severity, message: str, action: str | None = None,
             value: float | None = None, details: dict | None = None) -> Event:
        event = Event(drone_id, type_, severity, message, action, value, details or {}, ts=self.clock())
        self.events.append(event)
        self.event_counts[type_] += 1
        level = {Severity.INFO: logging.INFO, Severity.WARNING: logging.WARNING, Severity.CRITICAL: logging.ERROR}
        log.log(level[severity], "[%s] %s: %s", drone_id, type_, message)
        if self.storage:
            self.storage.event(event)
        if self.notifier:
            self.notifier.notify(event)
        return event

    def _cooled_down(self, state: DroneState, key: str, now: float) -> bool:
        last = state.last_fired.get(key)
        if last is not None and now - last < self.cooldown_s:
            return False
        state.last_fired[key] = now
        return True

    # ------------------------------------------------------------------ actions
    def _command(self, state: DroneState, command: str, reason: str, now: float) -> None:
        cmd_id = self.send_command(state.drone_id, command)
        self.emit(state.drone_id, "COMMAND_SENT", Severity.INFO, f"{command} ({reason})", command,
                  details={"command_id": cmd_id, "reason": reason})

    def _act(self, state: DroneState, action: Action, rule: str, now: float) -> None:
        """Escalate-only actuation with re-send if the drone doesn't comply."""
        if action == Action.NONE:
            return
        mode = state.mode or ""
        if mode in SATISFIED_BY[action]:
            if action > state.commanded:
                state.commanded, state.commanded_rule, state.commanded_at = action, rule, now
            return
        if action < state.commanded:
            return                                    # something more drastic is already in progress
        if action == state.commanded and now - state.commanded_at < self.resend_after_s:
            return                                    # give the drone time to comply
        resend = action == state.commanded
        state.commanded, state.commanded_rule, state.commanded_at = action, rule, now
        self._command(state, action.name, f"{'re-send, ' if resend else ''}{rule}", now)

    def _handle(self, state: DroneState, d: Decision, now: float) -> None:
        if self._cooled_down(state, d.event_type, now):
            self.emit(state.drone_id, d.event_type, d.severity, d.message,
                      d.action.name if d.action else None, d.value, d.details)
        self._act(state, d.action, d.rule, now)

    # ------------------------------------------------------------------ pipeline
    def process(self, t: Telemetry, received_at: float | None = None) -> None:
        started = time.perf_counter()
        with self.lock:
            now = self.clock()
            state = self.drones.setdefault(t.drone_id, DroneState(t.drone_id))

            # Sequence check: drop stale/duplicate frames, count gaps.
            if state.last is not None:
                if t.seq <= state.last.seq:
                    state.stale_frames += 1
                    return
                state.lost_frames += max(0, t.seq - state.last.seq - 1)

            state.frames += 1
            state.last_seen = received_at or now
            self.frame_times.append(now)

            if not state.link_up:
                down = now - (state.link_lost_at or now)
                state.link_up, state.link_lost_at = True, None
                self.detector.reset(t.drone_id)
                self.predictor.reset(t.drone_id)
                self.emit(t.drone_id, "LINK_RESTORED", Severity.INFO, f"telemetry back after {down:.0f}s",
                          value=round(down, 1), details={"downtime_s": round(down, 1)})

            self._on_mode(state, t.mode, now)
            state.last = t

            # 1. Don't act on bad data: detect glitches and substitute estimates.
            check = self.detector.check(t.drone_id, t.ts, {"alt": t.alt, "motor_temp": t.motor_temp,
                                                           "battery": t.battery})
            trusted = {"alt": t.alt, "motor_temp": t.motor_temp, "battery": t.battery}
            for a in check.anomalies:
                trusted[a.metric] = self.detector.expected(t.drone_id, a.metric) or a.expected
            glitchy = bool(check.anomalies)
            if glitchy:
                state.glitch_frames += 1
                if self._cooled_down(state, "SENSOR_GLITCH", now):
                    a = check.anomalies[0]
                    self.emit(t.drone_id, "SENSOR_GLITCH", Severity.WARNING,
                              f"{a.metric}={a.value:.1f} looks wrong ({a.reason}); using {trusted[a.metric]:.1f}",
                              value=a.value, details={"metrics": [x.metric for x in check.anomalies]})
            for s in check.shifts:
                if s.metric == "battery" and s.delta < 0:
                    self.emit(t.drone_id, "BATTERY_SUDDEN_DROP", Severity.CRITICAL,
                              f"battery fell {abs(s.delta):.0f}% at once ({s.old_level:.0f}% -> {s.new_level:.0f}%)",
                              value=s.new_level, details={"from": s.old_level, "to": s.new_level})
                else:
                    self.emit(t.drone_id, "SENSOR_LEVEL_SHIFT", Severity.INFO,
                              f"{s.metric} moved {s.old_level} -> {s.new_level} and stayed there")

            # 2. Battery forecast + per-drone rules.
            dist = haversine_m(t.lat, t.lon, *self.home)
            if "battery" not in {a.metric for a in check.anomalies}:
                state.forecast = self.predictor.update(t.drone_id, t.ts, trusted["battery"], dist, trusted["alt"])
                if state.forecast and self.storage:
                    self.storage.forecast(t.drone_id, t.ts, state.forecast.seconds_to_reserve,
                                          state.forecast.seconds_to_home, state.forecast.drain_pct_per_s)
            frame = Frame(t.drone_id, t.mode, trusted["alt"], trusted["battery"], trusted["motor_temp"],
                          t.signal, dist, state.forecast)
            for decision in evaluate(frame, self.rules):
                self._handle(state, decision, now)

            # 3. Self-healing for persistent sensor faults.
            out = self.recovery.on_frame(t.drone_id, t.ts, glitchy)
            if out:
                self.emit(t.drone_id, out.event_type, out.severity, out.message,
                          out.command or (out.action.name if out.action else None), details=out.details)
                if out.command:
                    self._command(state, out.command, "recovery", now)
                    if out.command == "REBOOT":
                        # We caused this outage: don't page anyone about the link.
                        self.detector.reset(t.drone_id)
                        state.quiet_until = now + self.reboot_grace_s
                self._act(state, out.action, "recovery", now)

            # 4. Fleet separation.
            self._check_separation(state, now)

            if self.storage:
                self.storage.telemetry(t, dist, glitchy)
        self.latencies_ms.append((time.perf_counter() - started) * 1000)

    def _on_mode(self, state: DroneState, mode: str, now: float) -> None:
        if mode == state.mode:
            return
        previous, state.mode = state.mode, mode
        if previous is None:
            return
        self.emit(state.drone_id, "MODE_CHANGED", Severity.INFO, f"{previous} -> {mode}",
                  details={"from": previous, "to": mode})
        if mode == "MISSION":
            # Relaunch, finished descent or resumed: previous orders are done.
            state.commanded, state.commanded_rule, state.held_for = Action.NONE, None, None
            if previous == "LANDED":
                self.predictor.reset(state.drone_id)
                self.detector.reset(state.drone_id)
                self.recovery.reset(state.drone_id)
        elif mode == "LANDED":
            # On the pad the physics change (charging, motors off): start fresh baselines.
            state.commanded, state.commanded_rule, state.held_for = Action.NONE, None, None
            self.recovery.reset(state.drone_id)
            self.detector.reset(state.drone_id)
            self.predictor.reset(state.drone_id)

    def _positions(self) -> dict[str, Position]:
        return {
            s.drone_id: Position(s.drone_id, s.last.lat, s.last.lon, s.last.alt, s.last.speed, s.last.heading,
                                 s.mode or "")
            for s in self.drones.values() if s.last is not None and s.link_up
        }

    def _check_separation(self, state: DroneState, now: float) -> None:
        cfg = self.rules["proximity"]
        positions = self._positions()
        for c in find_conflicts(list(positions.values()), cfg["horizontal_m"], cfg["vertical_m"]):
            if state.drone_id not in c.pair:
                continue
            yielder = self.drones[c.yielding]
            other = c.b if c.a == c.yielding else c.a
            # Both drones see the same conflict; key the cooldown on the yielder so it's reported once.
            if self._cooled_down(yielder, f"PROXIMITY:{'-'.join(c.pair)}", now):
                self.emit(c.yielding, "PROXIMITY_CONFLICT", Severity.CRITICAL,
                          f"{c.horizontal_m:.0f} m from {other} ({c.vertical_m:.0f} m vertical) - holding position",
                          "HOLD", c.horizontal_m, {"other": other, "yielding": c.yielding})
            yielder.held_for = other
            yielder.clear_since = None
            self._act(yielder, Action.HOLD, "proximity", now)

        # Release a held drone once it has been clear for a while.
        if state.held_for and state.commanded == Action.HOLD and state.commanded_rule == "proximity":
            other = positions.get(state.held_for)
            me = positions.get(state.drone_id)
            if other is None or me is None or separated(me, other, cfg["horizontal_m"] * cfg["clear_factor"],
                                                        cfg["vertical_m"] * cfg["clear_factor"]):
                state.clear_since = state.clear_since or now
                if now - state.clear_since >= cfg["resume_after_s"]:
                    self.emit(state.drone_id, "PROXIMITY_CLEARED", Severity.INFO,
                              f"clear of {state.held_for} - resuming mission", "RESUME")
                    state.held_for, state.clear_since = None, None
                    state.commanded, state.commanded_rule = Action.NONE, None
                    self._command(state, "RESUME", "proximity cleared", now)
            else:
                state.clear_since = None

    # ------------------------------------------------------------------ watchdog
    def check_links(self) -> None:
        """Called periodically: flag drones that stopped talking."""
        timeout = self.rules["watchdog"]["link_timeout_s"]
        with self.lock:
            now = self.clock()
            for state in self.drones.values():
                if now < state.quiet_until:
                    continue
                if state.link_up and state.last_seen and now - state.last_seen > timeout:
                    state.link_up, state.link_lost_at = False, state.last_seen
                    self.emit(state.drone_id, "LINK_LOST", Severity.CRITICAL,
                              f"no telemetry for {now - state.last_seen:.0f}s (last mode {state.mode})",
                              details={"last_position": {"lat": state.last.lat, "lon": state.last.lon,
                                                         "alt": state.last.alt} if state.last else None})

    # ------------------------------------------------------------------ introspection
    def ack(self, payload: dict) -> None:
        with self.lock:
            if not payload.get("accepted"):
                self.emit(str(payload.get("drone_id")), "COMMAND_REJECTED", Severity.WARNING,
                          f"{payload.get('command')} rejected: {payload.get('reason')}", payload.get("command"))

    def metrics(self) -> dict:
        with self.lock:
            now = self.clock()
            lat = sorted(self.latencies_ms)
            recent = [t for t in self.frame_times if now - t <= 10]
            return {
                "drones": len(self.drones),
                "drones_online": sum(1 for s in self.drones.values() if s.link_up),
                "frames_per_s": round(len(recent) / 10, 2),
                "latency_ms_p50": round(statistics.median(lat), 3) if lat else 0.0,
                "latency_ms_p95": round(lat[int(len(lat) * 0.95) - 1], 3) if lat else 0.0,
                "latency_ms_max": round(lat[-1], 3) if lat else 0.0,
                "invalid_frames": self.invalid_frames,
                "events_total": sum(self.event_counts.values()),
            }
