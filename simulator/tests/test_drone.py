from aeronode_sim.drone import TURNAROUND_S, Mode
from aeronode_sim.fleet import build_fleet
from aeronode_sim.geo import haversine_m

HOME = (17.4474, 78.3762)


def fly(fleet, seconds, start=0.0):
    t = start
    for _ in range(int(seconds)):
        t += 1
        for d in fleet.values():
            d.step(t, 1.0, fleet)
    return t


def test_staggered_launch():
    fleet = build_fleet(3, *HOME)
    fly(fleet, 5)
    assert fleet["uav-01"].mode == Mode.MISSION
    assert fleet["uav-03"].mode == Mode.LANDED     # launches 24 s after start


def test_rtl_lands_at_home_and_relaunches():
    fleet = build_fleet(1, *HOME)
    d = fleet["uav-01"]
    t = fly(fleet, 60)
    assert d.apply_command("RTL", t)[0]
    for _ in range(200):
        t = fly(fleet, 1, t)
        if d.mode == Mode.LANDED:
            break
    assert d.mode == Mode.LANDED
    assert haversine_m(d.lat, d.lon, *HOME) < 5
    fly(fleet, TURNAROUND_S + 30, t)
    assert d.mode == Mode.MISSION


def test_reset_sensor_clears_spike_but_not_stuck():
    fleet = build_fleet(1, *HOME)
    d = fleet["uav-01"]
    d.inject_fault("sensor_spike", 0, None)
    d.inject_fault("sensor_stuck", 0, None)
    d.apply_command("RESET_SENSOR", 1)
    assert "sensor_spike" not in d.faults and "sensor_stuck" in d.faults
    d.apply_command("REBOOT", 2)
    assert "sensor_stuck" not in d.faults
    assert d.is_offline(3) and not d.is_offline(7)


def test_signal_loss_takes_drone_offline():
    d = build_fleet(1, *HOME)["uav-01"]
    d.inject_fault("signal_loss", 100, 10)
    assert d.is_offline(105) and not d.is_offline(111)
    assert d.apply_command("RTL", 105) == (False, "drone offline")


def test_battery_drop_and_flyaway():
    fleet = build_fleet(1, *HOME)
    d = fleet["uav-01"]
    t = fly(fleet, 40)
    before = d.battery
    d.inject_fault("battery_drop", t, None)
    assert before - d.battery >= 34
    d.inject_fault("flyaway", t, None)
    fly(fleet, 120, t)
    assert haversine_m(d.lat, d.lon, *HOME) > 1000


def test_unknown_inputs_rejected():
    d = build_fleet(1, *HOME)["uav-01"]
    assert d.apply_command("SELF_DESTRUCT", 0)[0] is False
    try:
        d.inject_fault("gremlins", 0, None)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
