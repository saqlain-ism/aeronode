from aeronode.geo import offset
from aeronode.models import Action
from aeronode.proximity import Position, find_conflicts, separated
from aeronode.recovery import RecoveryManager

HOME = (17.4474, 78.3762)


def manager():
    return RecoveryManager(trigger_count=3, trigger_window_s=10, verify_s=10, healthy_streak=5,
                           ladder=["RESET_SENSOR", "REBOOT"])


def run(m, pattern, start=0):
    """pattern: string of 'g' (glitchy) / '.' (clean), one frame per second."""
    outs = []
    for i, c in enumerate(pattern):
        out = m.on_frame("d1", start + i, c == "g")
        if out:
            outs.append((start + i, out))
    return outs


# ---------------------------------------------------------------- recovery
def test_isolated_glitches_do_not_start_recovery():
    m = manager()
    assert run(m, "g.....g.....g.....") == []


def test_recovery_succeeds_after_first_step():
    m = manager()
    outs = run(m, "ggg" + "." * 6)
    assert [o.event_type for _, o in outs] == ["RECOVERY_STARTED", "RECOVERY_SUCCEEDED"]
    assert outs[0][1].command == "RESET_SENSOR"
    assert outs[1][1].details["fixed_by"] == "RESET_SENSOR"
    assert m.status("d1")["phase"] == "idle"


def test_recovery_escalates_then_succeeds():
    m = manager()
    outs = run(m, "ggg" + "g" * 11 + "." * 6)
    types = [o.event_type for _, o in outs]
    assert types == ["RECOVERY_STARTED", "RECOVERY_ESCALATED", "RECOVERY_SUCCEEDED"]
    assert outs[1][1].command == "REBOOT"
    assert outs[2][1].details["attempts"] == 2


def test_recovery_gives_up_and_returns_home():
    m = manager()
    outs = run(m, "g" * 40)
    final = outs[-1][1]
    assert final.event_type == "RECOVERY_FAILED"
    assert final.action == Action.RTL
    assert m.status("d1")["phase"] == "failed"
    assert run(m, "g" * 10, start=100) == []   # stays failed, no command spam


# ---------------------------------------------------------------- proximity
def pos(drone_id, north=0.0, east=0.0, alt=50.0, speed=0.0, heading=0.0, mode="MISSION"):
    lat, lon = offset(*HOME, north, east)
    return Position(drone_id, lat, lon, alt, speed, heading, mode)


def test_vertical_separation_avoids_conflict():
    assert find_conflicts([pos("a"), pos("b", north=10, alt=70)], 30, 12) == []


def test_conflict_yielder_is_the_one_closing_in():
    a = pos("a", north=0, speed=12, heading=0)          # flying north, towards b
    b = pos("b", north=20, speed=8, heading=90)         # crossing east
    conflicts = find_conflicts([a, b], 30, 12)
    assert len(conflicts) == 1
    assert conflicts[0].yielding == "a"


def test_ground_drones_are_ignored():
    assert find_conflicts([pos("a", alt=0, mode="LANDED"), pos("b", alt=0, mode="LANDED")], 30, 12) == []


def test_separated():
    assert separated(pos("a"), pos("b", north=70), 60, 24)
    assert not separated(pos("a"), pos("b", north=20), 60, 24)
