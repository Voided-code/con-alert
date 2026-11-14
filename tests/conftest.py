"""Every test runs with all of CON alert's files (state, logs, records, settings) moved to a temporary
folder, so tests never touch the real alert history or send anything."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import common, delivery, game, predict, activity, history, monitor, server  # noqa: E402

MODULES = [common, delivery, game, predict, activity, history, monitor, server]


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Point every file path that lives inside the project (or the alert pictures folder) at tmp_path."""
    real = [ROOT, common.ALERTS]
    for mod in MODULES:
        for name, value in list(vars(mod).items()):
            if isinstance(value, Path) and name.isupper():
                for base in real:
                    try:
                        rel = value.relative_to(base)
                    except ValueError:
                        continue
                    monkeypatch.setattr(mod, name, tmp_path / base.name / rel)
                    break
    (tmp_path / ROOT.name / "state").mkdir(parents=True)
    (tmp_path / ROOT.name / "logs").mkdir(parents=True)
    for mod in MODULES:                               # nothing may really be sent
        if hasattr(mod, "send"):
            monkeypatch.setattr(mod, "send", lambda *a, **k: pytest.fail("send() called without a test stub"))
    monkeypatch.setitem(predict._learnt, "at", 0)
    activity._orders.clear()
    activity._still.clear()
    history._hist.update({"file": None, "own": None, "pl": None})
    yield tmp_path


def enemy(id, x, y, **kw):
    """A foreign army as the game data gives it."""
    e = {"id": id, "o": kw.pop("o", 7), "nation": kw.pop("nation", "China"), "war": True, "x": x, "y": y,
         "units": kw.pop("units", [["Infantry", 2]]), "air": False, "sea": False, "radar": False}
    e.update(kw)
    return e
