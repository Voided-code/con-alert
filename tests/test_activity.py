"""Orders count as a player being online, except the ones the game gives by itself."""
import activity
from conftest import enemy


def check(*enemies, friends=()):
    return activity.track_orders({"enemies": list(enemies), "friends": list(friends)})


def test_first_sight_tells_nothing():
    assert check(enemy(1, 0, 0, ord=[["gc", 50, 50]])) == []


def test_new_move_order():
    check(enemy(1, 0, 0, ord=[]))
    ev = check(enemy(1, 0, 0, ord=[["gc", 80, 80]]))
    assert [e["kind"] for e in ev] == ["move"] and ev[0]["to"] == (80, 80)
    assert activity.ACTIVITY.read_text().count("\n") == 1


def test_moving_on_to_the_next_leg_is_not_an_order():
    check(enemy(1, 0, 0, ord=[["gc", 20, 20], ["gc", 80, 80]]))
    assert check(enemy(1, 20, 20, ord=[["gc", 80, 80]])) == []


def test_refuel_waits_are_automatic():
    check(enemy(1, 0, 0, ord=[]))
    assert check(enemy(1, 0, 0, ord=[["wc"]])) == []


def test_attack_order():
    check(enemy(1, 0, 0, ord=[]))
    assert [e["kind"] for e in check(enemy(1, 0, 0, ord=[["ac", 30, 30]]))] == ["attack"]


def test_attack_ending_is_not_a_stop():
    check(enemy(1, 0, 0, ord=[["ac", 30, 30]]))
    assert check(enemy(1, 10, 10, ord=[])) == []


def test_stopped_short_of_destination():
    check(enemy(1, 0, 0, ord=[["gc", 100, 100]]))
    assert [e["kind"] for e in check(enemy(1, 40, 40, ord=[]))] == ["stopped"]


def test_carrying_on_after_a_fight():
    check(enemy(1, 0, 0, ord=[["gc", 100, 100]]))
    check(enemy(1, 30, 30, ord=[], fight=True))             # stopped by a fight: not a stop order
    assert check(enemy(1, 30, 30, ord=[["gc", 100, 100]])) == []   # same destination again: automatic


def test_aircraft_returning_to_base_is_automatic():
    plane = {"base": [500, 500], "target": [100, 100]}
    check(enemy(1, 100, 100, ord=[["gc", 100, 100]], plane=plane, air=True))
    assert check(enemy(1, 100, 100, ord=[["gc", 500, 500]], plane=plane, air=True)) == []


def test_hidden_orders_seen_as_starting_to_move():
    for _ in range(3):                                      # standing still for three checks
        check(enemy(1, 0, 0))
    ev = check(enemy(1, 20, 0))
    assert len(ev) == 1 and ev[0]["seen_moving"]


def test_pushed_by_a_fight_is_not_an_order():
    for _ in range(3):
        check(enemy(1, 0, 0))
    assert check(enemy(1, 20, 0, fight=True)) == []


def test_radar_blips_are_ignored():
    check(enemy(1, 0, 0, radar=[["land", 10]]))
    assert check(enemy(1, 0, 0, radar=[["land", 10]], ord=[["gc", 5, 5]])) == []
