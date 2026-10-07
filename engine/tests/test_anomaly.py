import random

from aeronode.anomaly import AnomalyDetector, RollingStats


def detector(**kw):
    cfg = dict(metrics=["alt", "battery"], window=30, min_samples=10, z_threshold=5.0,
               min_residual={"alt": 10.0, "battery": 4.0}, confirm_shift={"battery": 3})
    cfg.update(kw)
    return AnomalyDetector(**cfg)


def feed(det, values, metric="alt", start=0.0):
    results = []
    for i, v in enumerate(values):
        results.append(det.check("d1", start + i, {metric: v}))
    return results


def test_rolling_stats_matches_naive():
    rs = RollingStats(5)
    data = [3.0, 7.0, 1.0, 9.0, 4.0, 6.0, 2.0]
    for x in data:
        rs.push(x)
    window = data[-5:]
    mean = sum(window) / 5
    var = sum((x - mean) ** 2 for x in window) / 4
    assert abs(rs.mean - mean) < 1e-9
    assert abs(rs.std - var ** 0.5) < 1e-9


def test_steady_climb_and_descent_are_not_anomalies():
    det = detector()
    rng = random.Random(1)
    climb = [3.0 * i + rng.gauss(0, 0.3) for i in range(20)]
    cruise = [60 + rng.gauss(0, 0.3) for _ in range(20)]
    descent = [60 - 2.5 * i + rng.gauss(0, 0.3) for i in range(24)]
    results = feed(det, climb + cruise + descent)
    assert not any(r.anomalies for r in results)


def test_spike_is_flagged_and_excluded_from_baseline():
    det = detector()
    feed(det, [50.0 + 0.1 * (i % 3) for i in range(15)])
    r = det.check("d1", 15, {"alt": 130.0})
    assert len(r.anomalies) == 1
    assert r.anomalies[0].metric == "alt"
    # the bad value must not become the new "expected" value
    assert abs(det.expected("d1", "alt") - 50.0) < 1.0
    # and normal readings afterwards are fine
    assert not det.check("d1", 16, {"alt": 50.1}).anomalies


def test_consistent_jump_is_accepted_as_level_shift():
    det = detector()
    feed(det, [90.0 - 0.1 * i for i in range(15)], metric="battery")
    results = [det.check("d1", 15 + i, {"battery": 55.0 - 0.3 * i}) for i in range(4)]
    assert results[0].anomalies and results[1].anomalies
    assert results[2].shifts and results[2].shifts[0].delta < -30
    assert not results[3].anomalies and not results[3].shifts


def test_inconsistent_glitches_never_become_a_shift():
    det = detector()
    feed(det, [50.0] * 15)
    shifts = []
    for i, v in enumerate([120, 50, -10, 50, 130, 50, 5, 50]):
        shifts += det.check("d1", 15 + i, {"alt": float(v)}).shifts
    assert not shifts


def test_altitude_cannot_shift_from_a_few_similar_glitches():
    det = detector()
    feed(det, [90.0] * 15)
    results = [det.check("d1", 15 + i, {"alt": 12.0 + i}) for i in range(5)]
    assert all(r.anomalies for r in results)
    assert not any(r.shifts for r in results)


def test_no_judgement_during_warmup():
    det = detector()
    r = feed(det, [50, 50, 50, 140])
    assert not any(x.anomalies for x in r)


def test_reset_forgets_baseline():
    det = detector()
    feed(det, [50.0] * 15)
    det.reset("d1")
    assert det.expected("d1", "alt") is None
