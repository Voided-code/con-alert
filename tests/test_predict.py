"""Where out-of-sight groups probably are."""
import json
import math

import pytest

import predict

MAPW = 15393
MIN = 60000
T0 = 1_800_000_000_000                                   # when the group was last seen (ms)


def track(**kw):
    t = {"pos": [0, 0], "seen": T0, "spd": 10, "knots": [], "units": [["Infantry", 1]]}
    t.update(kw)
    return t


def at(t, minutes):
    return predict.predict(t, T0 + minutes * MIN, MAPW)


def test_follows_its_route_on_the_game_timings():
    t = track(knots=[[0, 0, T0], [100, 0, T0 + 10 * MIN]], to="Manila")
    x, y, err, note, _, chance = at(t, 5)
    assert (round(x), round(y)) == (50, 0) and "on its way to Manila" in note
    assert chance is None and err < 10
    x, y, err, note, _, _ = at(t, 30)
    assert (x, y) == (100, 0) and "should have arrived" in note


def test_buildings_do_not_move():
    x, y, err, note, _, _ = at(track(move={"fixed": True}), 120)
    assert (x, y, err) == (0, 0, 3) and "cannot move" in note


def test_standing_group_chance_starts_at_100_percent():
    t = track(move={"tbl": True})
    assert at(t, 1)[5] > 0.99                              # just out of sight: still there
    assert at(t, 60)[5] > at(t, 600)[5]                    # less sure the longer it is unseen
    assert at(t, 480)[5] == pytest.approx(math.exp(-1), rel=0.01)


def test_parked_aircraft_stays_at_base():
    t = track(air=True, pos=[5, 5], plane={"base": [5, 5], "flying": False})
    x, y, err, note, _, chance = at(t, 30)
    assert (x, y) == (5, 5) and "on the ground" in note and chance > 0.9


PATROL = {"base": [0, 0], "target": [600, 0], "flying": True, "st": "patrolling",
          "fuel": 180 * 60, "refuel": 10 * 60, "patrol": 50, "baseName": "Clark"}
# 3 hours of fuel, an hour's flight each way: patrol until 120, home at 180, refuelled at 190, back out at 250


@pytest.mark.parametrize("minutes, where, says", [
    (30, (600, 0), "patrolling over its spot"),
    (150, (300, 0), "flying home to Clark"),               # halfway home
    (185, (0, 0), "refuelling at its airbase Clark"),
    (220, (300, 0), "flying back out"),                    # halfway back out
    (280, (600, 0), "patrolling over its spot"),           # and patrolling again
])
def test_patrolling_aircraft_cycle(minutes, where, says):
    t = track(air=True, pos=[600, 0], plane=dict(PATROL))
    x, y, area, note, _, chance = at(t, minutes)
    assert (round(x), round(y)) == where and says in note
    assert 0 < chance <= 1


def test_aircraft_seen_flying_home():
    t = track(air=True, pos=[300, 0], plane=dict(PATROL, homing=True))
    x, y, _, note, _, _ = at(t, 15)
    assert (round(x), round(y)) == (150, 0) and "flying home" in note
    x, y, _, note, _, _ = at(t, 35)
    assert (x, y) == (0, 0) and "refuelling" in note


def test_learns_how_fast_the_chance_falls():
    recs = [{"t": 0, "kind": "standing", "min": 60, "hit": i < 2, "dist": 0, "err": 0, "chance": 0.9}
            for i in range(10)]                            # mostly gone after an hour: falls much faster than guessed
    predict.CHECKS.write_text("".join(json.dumps(r) + "\n" for r in recs))
    learnt = predict.learn()
    assert learnt["record"]["standing"] == {"n": 10, "hit": 2}
    assert learnt["minutes"]["standing"] < predict.CHANCE_MINUTES["standing"] / 4


def test_checks_a_prediction_when_the_group_comes_back():
    t = track(knots=[[0, 0, T0], [100, 0, T0 + 10 * MIN]])
    predict.check_prediction(t, {"x": 100, "y": 0}, T0 + 20 * MIN)
    rec = json.loads(predict.CHECKS.read_text())
    assert rec["kind"] == "route" and rec["hit"] and rec["min"] == 20
    predict.check_prediction(t, {"x": 100, "y": 0}, T0 + 1 * MIN)    # back too soon to count
    assert predict.CHECKS.read_text().count("\n") == 1


def test_hide_below_comes_from_settings(monkeypatch):
    monkeypatch.setattr(predict, "config", lambda: {})
    assert predict.min_chance() == predict.PREDICT_MIN_CHANCE
    monkeypatch.setattr(predict, "config", lambda: {"hide_below": 25})
    assert predict.min_chance() == 0.25
    monkeypatch.setattr(predict, "config", lambda: {"hide_below": 500})   # kept to a sensible range
    assert predict.min_chance() == 0.95
