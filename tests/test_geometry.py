from game import inside, troop_spot

MAPW = 15393


def test_box_spot():
    spot = {"world": [100, 100, 50, 50]}
    assert inside({"x": 120, "y": 120}, spot, MAPW)
    assert not inside({"x": 160, "y": 120}, spot, MAPW)
    assert not inside({"x": 120, "y": 151}, spot, MAPW)


def test_box_across_the_map_edge():
    spot = {"world": [MAPW - 20, 100, 50, 50]}            # wraps from the right edge to the left
    assert inside({"x": MAPW - 5, "y": 120}, spot, MAPW)
    assert inside({"x": 10, "y": 120}, spot, MAPW)
    assert not inside({"x": 40, "y": 120}, spot, MAPW)


def test_circle_across_the_map_edge():
    spot = {"circle": [5, 100, 30]}
    assert inside({"x": MAPW - 10, "y": 100}, spot, MAPW)
    assert not inside({"x": MAPW - 40, "y": 100}, spot, MAPW)


def test_troop_spot_follows_the_troop():
    data = {"friends": [{"id": 1, "x": 500, "y": 600, "sight": 40}]}
    s = troop_spot({"id": "a", "name": "Near 1st", "army": 1}, data)
    assert s["circle"] == [500, 600, 40.0]
    s = troop_spot({"id": "a", "name": "Near 1st", "army": 1, "radius": 99}, data)
    assert s["circle"][2] == 99
    assert troop_spot({"id": "a", "name": "Near 2nd", "army": 2}, data) is None
