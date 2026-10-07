"""Automatic recovery from sensor faults.

When a drone keeps producing glitchy readings the engine walks an escalation
ladder - by default RESET_SENSOR, then REBOOT - verifying after each step.
If the drone is still unhealthy after the last step it is brought home (RTL),
because a drone that can't trust its own sensors shouldn't stay on mission.

    IDLE --(N glitches in window)--> STEP 0 --(still bad)--> STEP 1 --> ... --> FAILED (RTL)
      ^                                 |                       |
      +---------(healthy streak)--------+-----------------------+
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from .models import Action, Severity


class Phase(str, Enum):
    IDLE = "idle"
    RECOVERING = "recovering"
    FAILED = "failed"


@dataclass
class RecoveryOutput:
    event_type: str
    severity: Severity
    message: str
    command: str | None = None
    action: Action = Action.NONE
    details: dict = field(default_factory=dict)


@dataclass
class _DroneRecovery:
    phase: Phase = Phase.IDLE
    glitches: deque = field(default_factory=deque)
    step: int = 0
    step_started: float = 0.0
    started: float = 0.0
    healthy: int = 0
    attempts: int = 0


class RecoveryManager:
    def __init__(self, trigger_count: int, trigger_window_s: float, verify_s: float,
                 healthy_streak: int, ladder: list[str]) -> None:
        self.trigger_count = trigger_count
        self.trigger_window_s = trigger_window_s
        self.verify_s = verify_s
        self.healthy_streak = healthy_streak
        self.ladder = ladder
        self._state: dict[str, _DroneRecovery] = {}

    def status(self, drone_id: str) -> dict:
        s = self._state.get(drone_id, _DroneRecovery())
        return {"phase": s.phase.value, "step": self.ladder[s.step] if s.phase == Phase.RECOVERING else None,
                "attempts": s.attempts}

    def reset(self, drone_id: str) -> None:
        self._state.pop(drone_id, None)

    def on_frame(self, drone_id: str, ts: float, glitchy: bool) -> RecoveryOutput | None:
        s = self._state.setdefault(drone_id, _DroneRecovery())

        if s.phase == Phase.FAILED:
            return None

        if s.phase == Phase.IDLE:
            if not glitchy:
                return None
            s.glitches.append(ts)
            while s.glitches and ts - s.glitches[0] > self.trigger_window_s:
                s.glitches.popleft()
            if len(s.glitches) < self.trigger_count:
                return None
            s.phase, s.step, s.step_started, s.started, s.healthy = Phase.RECOVERING, 0, ts, ts, 0
            s.attempts = 1
            s.glitches.clear()
            return RecoveryOutput("RECOVERY_STARTED", Severity.WARNING,
                                  f"{self.trigger_count} sensor glitches in {self.trigger_window_s:.0f}s - trying {self.ladder[0]}",
                                  command=self.ladder[0])

        # RECOVERING
        s.healthy = 0 if glitchy else s.healthy + 1
        if s.healthy >= self.healthy_streak:
            mttr = ts - s.started
            step = self.ladder[s.step]
            s.phase, s.healthy = Phase.IDLE, 0
            return RecoveryOutput("RECOVERY_SUCCEEDED", Severity.INFO,
                                  f"healthy again after {step} ({s.attempts} step(s), {mttr:.0f}s)",
                                  details={"mttr_s": round(mttr, 1), "fixed_by": step, "attempts": s.attempts})
        if ts - s.step_started < self.verify_s or s.healthy > 0:
            return None    # give the current step time to work

        if s.step + 1 < len(self.ladder):
            s.step += 1
            s.step_started = ts
            s.attempts += 1
            return RecoveryOutput("RECOVERY_ESCALATED", Severity.WARNING,
                                  f"{self.ladder[s.step - 1]} did not help - trying {self.ladder[s.step]}",
                                  command=self.ladder[s.step])

        s.phase = Phase.FAILED
        return RecoveryOutput("RECOVERY_FAILED", Severity.CRITICAL,
                              f"still faulty after {', '.join(self.ladder)} - bringing it home",
                              action=Action.RTL, details={"attempts": s.attempts})
