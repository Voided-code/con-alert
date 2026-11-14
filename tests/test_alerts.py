"""A foreign group is reported once ever: the first time it enters any spot."""
import json

import pytest

import monitor
from conftest import enemy

SPOT_A = {"id": "a", "name": "Spot A", "world": [0, 0, 100, 100]}
SPOT_B = {"id": "b", "name": "Spot B", "world": [500, 0, 100, 100]}


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(monitor, "send", lambda text, pic=None: out.append(text) or True)
    monkeypatch.setattr(monitor, "enemy_picture", lambda *a, **k: None)
    return out


def fresh():
    return {"alert": False, "last_alert": 0.0, "clear": 0, "seen": set()}


def data(*enemies):
    return {"enemies": list(enemies), "view": {"mapw": 15393}}


def test_one_message_per_group(sent):
    ma, mb = fresh(), fresh()
    one = enemy(1, 50, 50)
    assert monitor.watch_spot_data(SPOT_A, ma, data(one), 0) == 1
    assert len(sent) == 1 and "Spot A" in sent[0] and "China" in sent[0]
    monitor.watch_spot_data(SPOT_A, ma, data(one), 20)                     # still there
    assert len(sent) == 1
    moved = enemy(1, 550, 50)                                            # into another spot
    monitor.watch_spot_data(SPOT_A, ma, data(moved), 40)
    monitor.watch_spot_data(SPOT_B, mb, data(moved), 40)
    assert len(sent) == 1


def test_a_new_group_is_reported_and_mentions_the_known_one(sent):
    m = fresh()
    monitor.watch_spot_data(SPOT_A, m, data(enemy(1, 50, 50)), 0)
    monitor.watch_spot_data(SPOT_A, m, data(enemy(1, 50, 50), enemy(2, 60, 60, nation="Russia")), 20)
    assert len(sent) == 2
    assert "Russia" in sent[1] and "China" not in sent[1]
    assert "already reported: 1 group" in sent[1]


def test_leaving_and_coming_back_is_not_new(sent):
    m = fresh()
    monitor.watch_spot_data(SPOT_A, m, data(enemy(1, 50, 50)), 0)
    for i in range(monitor.CLEAR_CHECKS + 2):
        monitor.watch_spot_data(SPOT_A, m, data(enemy(1, 900, 900)), 20 * (i + 1))
    assert m["alert"] is False
    monitor.watch_spot_data(SPOT_A, m, data(enemy(1, 50, 50)), 999)
    assert len(sent) == 1


def test_remembered_after_a_restart(sent):
    monitor.watch_spot_data(SPOT_A, fresh(), data(enemy(1, 50, 50)), 0)
    assert "1" in json.loads(monitor.ALERTED.read_text())
    monitor.watch_spot_data(SPOT_A, fresh(), data(enemy(1, 50, 50)), 60)   # new memory, as after a restart
    assert len(sent) == 1


def test_radar_contacts_count(sent, monkeypatch):
    blip = enemy(5, 50, 50, radar=[["land", 10]], nation="", units=[])
    monkeypatch.setattr(monitor, "RADAR_ALERTS", True)
    assert monitor.watch_spot_data(SPOT_A, fresh(), data(blip), 0) == 1
    monkeypatch.setattr(monitor, "RADAR_ALERTS", False)
    assert monitor.watch_spot_data(SPOT_A, fresh(), data(enemy(6, 50, 50, radar=[["ships", 30]], units=[])), 0) == 0
    assert len(sent) == 1


def test_alert_is_recorded(sent):
    monitor.watch_spot_data(SPOT_A, fresh(), data(enemy(1, 50, 50)), 0)
    import delivery
    rows = delivery.RECORD.read_text().splitlines()
    assert len(rows) == 2 and "Spot A" in rows[1]
