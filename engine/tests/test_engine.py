from aeronode.geo import offset

from .conftest import HOME


def warm_up(h, n=3, drone="uav-01", **kw):
    """A few frames: below the glitch detector's warm-up, so step changes in
    the test are taken at face value. Tests about glitches pass n=20."""
    for _ in range(n):
        h.frame(drone, **kw)


def test_low_battery_sends_rtl_once(h):
    warm_up(h, battery=90)
    for _ in range(5):
        h.frame(battery=20)
    assert h.commands == [("uav-01", "RTL")]
    assert h.types().count("BATTERY_LOW") == 1          # cooldown de-duplicates the alert


def test_actions_only_escalate(h):
    warm_up(h, motor_temp=50)
    h.frame(motor_temp=90)                               # -> LAND
    h.frame(motor_temp=90, battery=89)
    # a milder rule firing afterwards must not downgrade LAND to RTL
    h.frame(motor_temp=90, lat=offset(*HOME, 1100, 0)[0])
    assert [c for _, c in h.commands] == ["LAND"]


def test_no_command_when_drone_already_complies(h):
    warm_up(h, battery=90, mode="RTL")
    h.frame(battery=20, mode="RTL")
    assert h.commands == []


def test_command_is_resent_if_drone_ignores_it(h, rules):
    warm_up(h)
    h.frame(battery=20)
    for _ in range(rules["decisions"]["resend_after_s"] + 1):
        h.frame(battery=20)                              # still MISSION, ignoring us
    assert [c for _, c in h.commands] == ["RTL", "RTL"]
    assert "re-send" in [e for e in h.engine.events if e.type == "COMMAND_SENT"][-1].message


def test_new_mission_clears_previous_orders(h):
    warm_up(h)
    h.frame(battery=20)
    h.frame(battery=20, mode="RTL")
    h.frame(battery=20, mode="LANDED", alt=0)
    h.frame(battery=100, mode="MISSION")
    assert h.engine.drones["uav-01"].commanded.name == "NONE"


def test_stale_and_lost_frames(h):
    warm_up(h, n=3)
    state = h.engine.drones["uav-01"]
    h.seq["uav-01"] = 1                                 # replay an old frame
    h.frame()
    assert state.stale_frames == 1
    h.seq["uav-01"] = 10                                # jump ahead: 3 -> 11 means 7 lost
    h.frame()
    assert state.lost_frames == 7


def test_glitchy_altitude_does_not_trigger_descend(h):
    warm_up(h, n=20, alt=50)
    for i in range(3):
        h.frame(alt=140.0 if i % 2 == 0 else 50.0)
    assert "SENSOR_GLITCH" in h.types()
    assert "ALTITUDE_LIMIT" not in h.types()
    assert h.commands == []


def test_persistent_glitches_trigger_sensor_reset(h):
    warm_up(h, n=20, alt=50)
    for i in range(6):
        h.frame(alt=150.0 + 20 * (i % 2) if i % 2 == 0 else -30.0)
    assert ("uav-01", "RESET_SENSOR") in h.commands
    assert "RECOVERY_STARTED" in h.types()


def test_link_lost_and_restored(h):
    warm_up(h, n=3)
    h.clock.t += 10
    h.engine.check_links()
    assert h.types()[-1] == "LINK_LOST"
    assert not h.engine.drones["uav-01"].link_up
    h.frame()
    assert "LINK_RESTORED" in h.types()
    restored = [e for e in h.engine.events if e.type == "LINK_RESTORED"][0]
    assert restored.details["downtime_s"] >= 10


def test_proximity_hold_then_resume(h, rules):
    # uav-02 parked 15 m north of uav-01 at the same altitude; uav-01 flying at it.
    north_lat, north_lon = offset(*HOME, 15, 0)
    h.frame("uav-02", lat=north_lat, lon=north_lon, speed=0)
    h.frame("uav-01", speed=10, heading=0, advance=0)
    assert ("uav-01", "HOLD") in h.commands
    assert "PROXIMITY_CONFLICT" in h.types("uav-01")

    # uav-02 moves 200 m away; after resume_after_s in the clear, uav-01 resumes
    far_lat, far_lon = offset(*HOME, 200, 0)
    for _ in range(rules["proximity"]["resume_after_s"] + 2):
        h.frame("uav-02", lat=far_lat, lon=far_lon, advance=0.5)
        h.frame("uav-01", mode="HOLD", speed=0, advance=0.5)
    assert ("uav-01", "RESUME") in h.commands
    assert "PROXIMITY_CLEARED" in h.types("uav-01")


def test_metrics_shape(h):
    warm_up(h, n=5)
    m = h.engine.metrics()
    assert m["drones"] == 1 and m["drones_online"] == 1
    assert m["latency_ms_p95"] >= m["latency_ms_p50"] >= 0


def test_reboot_we_ordered_is_not_a_lost_link(h):
    warm_up(h, n=20, alt=50)
    # stubborn glitches: reset, then reboot
    for i in range(40):
        h.frame(alt=150.0 if i % 2 == 0 else 50.0)
        if ("uav-01", "REBOOT") in h.commands:
            break
    assert ("uav-01", "REBOOT") in h.commands
    h.clock.t += 7                      # drone silent while rebooting
    h.engine.check_links()
    assert "LINK_LOST" not in h.types()
    h.clock.t += 10                     # but a longer silence is still caught
    h.engine.check_links()
    assert "LINK_LOST" in h.types()
