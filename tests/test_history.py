import history


def test_saves_and_finds_a_moment(monkeypatch):
    monkeypatch.setattr(history, "ghosts", lambda world: [{"id": 9, "x": 1, "y": 2, "net": [1, 2, 3]}])
    world = {"armies": [{"id": 1, "x": 10, "y": 20, "o": 3, "name": "1st Infantry", "empty": ""}],
             "owners": [3, 3, 4], "players": {"3": ["China", "enemy"]}}
    history.history_save(world)
    history.history_save(dict(world, armies=[{"id": 1, "x": 11, "y": 20, "o": 3}]))
    lines = next(history.HISTORY.glob("*.jsonl")).read_text().splitlines()
    assert '"own"' in lines[0] and '"own"' not in lines[1]  # owners only written again when they change
    times = history.history_index()["times"]
    assert len(times) == 2
    got = history.history_at(times[-1])
    assert got["ok"] and got["world"]["owners"] == [3, 3, 4]
    assert got["world"]["ghosts"] == [{"id": 9, "x": 1, "y": 2}]  # predictions kept, without their network
    assert got["world"]["armies"][0]["x"] in (10, 11)


def test_nothing_saved_yet():
    assert history.history_at(0)["ok"] is False


def test_roads_of_a_standing_prediction_are_drawn_again(monkeypatch):
    rn = {"pos": [100, 200], "move": {"tbl": {"0": 1.0}}, "spd": 1.0, "o": 7, "el": 30.0}
    monkeypatch.setattr(history, "ghosts", lambda world: [{"id": 4, "x": 100, "y": 200, "sure": 70,
                                                           "net": [1, 2, 3, 4] * 500, "rn": rn}])
    asked = []
    monkeypatch.setattr(history, "reach_again", lambda gid, r, owners, when: asked.append((gid, r, owners)) or [9, 9, 8, 8])
    history.history_save({"armies": [], "owners": [7, 7]})
    saved = next(history.HISTORY.glob("*.jsonl")).read_text()
    assert '"net"' not in saved and '"rn"' in saved          # the small recipe is saved, not the long list of lines
    got = history.history_at(history.history_index()["times"][0])
    assert got["world"]["ghosts"][0]["net"] == [9, 9, 8, 8]
    assert asked == [(4, rn, [7, 7])]
