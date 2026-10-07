import pytest

from aeronode.models import Action, Severity
from aeronode.predictor import BatteryForecast, BatteryPredictor, linear_slope
from aeronode.rules import Frame, evaluate


def frame(**kw) -> Frame:
    data = dict(drone_id="d1", mode="MISSION", alt=50.0, battery=80.0, motor_temp=50.0, signal=90.0,
                distance_home_m=300.0)
    data.update(kw)
    return Frame(**data)


def actions(decisions):
    return {d.event_type: d.action for d in decisions}


# ---------------------------------------------------------------- predictor
def test_linear_slope():
    assert linear_slope([(0, 10), (1, 8), (2, 6)]) == pytest.approx(-2.0)
    assert linear_slope([(1, 5)]) is None
    assert linear_slope([(1, 5), (1, 6)]) is None


def make_predictor():
    return BatteryPredictor(reserve_pct=10, safety_factor=1.3, window_s=60, min_samples=5,
                            cruise_speed=8.0, descent_rate=2.5)


def test_forecast_requires_enough_samples():
    p = make_predictor()
    for i in range(4):
        assert p.update("d1", i, 80 - 0.1 * i, 500, 50) is None


def test_far_drone_with_fast_drain_must_return():
    p = make_predictor()
    fc = None
    for i in range(10):  # draining 0.5 %/s, 40% left -> 60s to reserve; home is 900/8+50/2.5 = 132s
        fc = p.update("d1", i, 45 - 0.5 * i, 900, 50)
    assert fc.must_return
    assert fc.drain_pct_per_s == pytest.approx(0.5)


def test_nearby_drone_does_not_need_to_return():
    p = make_predictor()
    fc = None
    for i in range(10):
        fc = p.update("d1", i, 45 - 0.1 * i, 50, 20)
    assert not fc.must_return


def test_charging_produces_no_forecast():
    p = make_predictor()
    fc = None
    for i in range(10):
        fc = p.update("d1", i, 50 + i, 0, 0)
    assert fc is None


# ---------------------------------------------------------------- rules
def test_healthy_frame_triggers_nothing(rules):
    assert evaluate(frame(), rules) == []


@pytest.mark.parametrize("battery,event,action", [
    (30, "BATTERY_WARNING", Action.NONE),
    (20, "BATTERY_LOW", Action.RTL),
    (8, "BATTERY_CRITICAL", Action.LAND),
])
def test_battery_thresholds(rules, battery, event, action):
    assert actions(evaluate(frame(battery=battery), rules))[event] == action


def test_geofence(rules):
    assert actions(evaluate(frame(distance_home_m=900), rules))["GEOFENCE_WARNING"] == Action.NONE
    assert actions(evaluate(frame(distance_home_m=1100), rules))["GEOFENCE_BREACH"] == Action.RTL
    # already heading home: no nagging warning
    assert "GEOFENCE_WARNING" not in actions(evaluate(frame(distance_home_m=900, mode="RTL"), rules))


def test_altitude_ceiling(rules):
    assert actions(evaluate(frame(alt=130), rules))["ALTITUDE_LIMIT"] == Action.DESCEND


def test_motor_temperature(rules):
    assert actions(evaluate(frame(motor_temp=75), rules))["MOTOR_TEMP_HIGH"] == Action.NONE
    d = [x for x in evaluate(frame(motor_temp=90), rules) if x.event_type == "MOTOR_TEMP_CRITICAL"][0]
    assert d.action == Action.LAND and d.severity == Severity.CRITICAL


def test_landed_drone_is_left_alone(rules):
    assert evaluate(frame(mode="LANDED", battery=5, alt=0, motor_temp=90), rules) == []


def test_predictive_rule(rules):
    fc = BatteryForecast(drain_pct_per_s=0.5, seconds_to_reserve=60, seconds_to_home=132, must_return=True)
    assert actions(evaluate(frame(forecast=fc), rules))["BATTERY_PREDICTIVE_RTL"] == Action.RTL
    # only while on mission - an RTL drone is already doing the right thing
    assert "BATTERY_PREDICTIVE_RTL" not in actions(evaluate(frame(forecast=fc, mode="RTL"), rules))
