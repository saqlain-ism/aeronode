"""Streaming sensor-glitch detection.

Telemetry legitimately changes all the time (take-off, landing, a motor
warming up), so comparing a reading with a rolling *mean* raises false alarms
on every manoeuvre. Instead each reading is compared with a short-horizon
**trend prediction** (last value + recent slope x dt):

    residual = reading - predicted

A reading is anomalous when its residual is far outside what we normally see
(|residual| > max(z_threshold x std(residuals), min_residual)).

Two failure shapes are then told apart:

* **Glitch** - a noisy sensor produces readings that are wrong *and*
  inconsistent with each other. These are reported as anomalies and kept out
  of the baseline, so bad data can't poison it.
* **Level shift** - a real, sudden change (a battery cell dropping out)
  produces readings that are off-trend but *agree with each other*. After
  `confirm_shift[metric]` consistent readings the detector accepts the new
  level and reports a shift instead of a fault. Metrics that physically can't
  jump (altitude, temperature) get a much higher count, so a run of similar
  glitches is never mistaken for reality.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Anomaly:
    metric: str
    value: float
    expected: float
    zscore: float | None
    reason: str


@dataclass
class LevelShift:
    metric: str
    old_level: float
    new_level: float

    @property
    def delta(self) -> float:
        return self.new_level - self.old_level


@dataclass
class CheckResult:
    anomalies: list[Anomaly] = field(default_factory=list)
    shifts: list[LevelShift] = field(default_factory=list)


class RollingStats:
    """Mean / std over a fixed window, O(1) per update."""

    def __init__(self, size: int) -> None:
        self.values: deque[float] = deque(maxlen=size)
        self._sum = 0.0
        self._sq = 0.0

    def push(self, x: float) -> None:
        if len(self.values) == self.values.maxlen:
            old = self.values[0]
            self._sum -= old
            self._sq -= old * old
        self.values.append(x)
        self._sum += x
        self._sq += x * x

    def __len__(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float:
        return self._sum / len(self.values) if self.values else 0.0

    @property
    def std(self) -> float:
        n = len(self.values)
        if n < 2:
            return 0.0
        var = (self._sq - self._sum * self._sum / n) / (n - 1)
        return math.sqrt(max(var, 0.0))


@dataclass
class _Track:
    history: deque  # recent accepted (ts, value)
    residuals: RollingStats
    suspects: list[tuple[float, float]] = field(default_factory=list)  # consecutive off-trend readings


class AnomalyDetector:
    TREND_POINTS = 5
    MAX_EXTRAPOLATE_S = 5.0

    def __init__(self, metrics: list[str], window: int, min_samples: int, z_threshold: float,
                 min_residual: dict[str, float], confirm_shift: dict[str, int] | int = 3,
                 default_confirm: int = 15) -> None:
        self.metrics = metrics
        self.window = window
        self.min_samples = min_samples
        self.z_threshold = z_threshold
        self.min_residual = min_residual
        if isinstance(confirm_shift, int):
            confirm_shift = {m: confirm_shift for m in metrics}
        self.confirm_shift = confirm_shift
        self.default_confirm = default_confirm
        self._tracks: dict[tuple[str, str], _Track] = {}

    def reset(self, drone_id: str) -> None:
        """Forget a drone's baseline (after a reboot, link loss or relaunch)."""
        for key in [k for k in self._tracks if k[0] == drone_id]:
            del self._tracks[key]

    def _predict(self, track: _Track, ts: float) -> float:
        hist = track.history
        t_last, x_last = hist[-1]
        if len(hist) < 2:
            return x_last
        t0, x0 = hist[0]
        slope = (x_last - x0) / (t_last - t0) if t_last > t0 else 0.0
        return x_last + slope * min(ts - t_last, self.MAX_EXTRAPOLATE_S)

    def check(self, drone_id: str, ts: float, values: dict[str, float]) -> CheckResult:
        result = CheckResult()
        for metric in self.metrics:
            if metric not in values:
                continue
            x = float(values[metric])
            key = (drone_id, metric)
            track = self._tracks.get(key)
            if track is None:
                track = _Track(deque(maxlen=self.TREND_POINTS), RollingStats(self.window))
                track.history.append((ts, x))
                self._tracks[key] = track
                continue

            predicted = self._predict(track, ts)
            residual = x - predicted
            floor = self.min_residual.get(metric, 1.0)
            limit = max(self.z_threshold * track.residuals.std, floor)
            warmed_up = len(track.residuals) >= self.min_samples

            if not warmed_up or abs(residual) <= limit:
                track.residuals.push(residual)
                track.history.append((ts, x))
                track.suspects.clear()
                continue

            # Off-trend. Is it a glitch, or the start of a real level shift?
            if track.suspects and abs(x - track.suspects[-1][1]) > floor:
                track.suspects.clear()            # inconsistent with previous suspect
            track.suspects.append((ts, x))

            if len(track.suspects) >= self.confirm_shift.get(metric, self.default_confirm):
                old_level = track.history[-1][1]
                track.history.clear()
                track.history.extend(track.suspects)
                track.suspects.clear()
                result.shifts.append(LevelShift(metric, round(old_level, 2), round(x, 2)))
                continue

            z = residual / track.residuals.std if track.residuals.std > 0 else None
            result.anomalies.append(Anomaly(
                metric=metric,
                value=x,
                expected=round(predicted, 2),
                zscore=round(z, 1) if z is not None else None,
                reason=f"off-trend by {residual:+.1f} (limit {limit:.1f})",
            ))
        return result

    def expected(self, drone_id: str, metric: str) -> float | None:
        """Best estimate of the true value when the current reading is untrusted."""
        track = self._tracks.get((drone_id, metric))
        return track.history[-1][1] if track and track.history else None
