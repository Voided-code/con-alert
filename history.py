"""A saved copy of every check, so the page can go back in time."""

import datetime as dt
import json
import shutil
import time


from common import (STATE, log)
from predict import (ghosts, reach_again)
# ------------------------------------------------------------------ history
# Every check is saved, so the page can go back in time: state/history/<date>-<hour>.jsonl, one line per
# check: {"t": seconds, "a": [armies as the page draws them], "own": province owners when they changed,
# "pl": countries when they changed}. The first line of each hour file always carries owners and countries,
# so any moment can be rebuilt from its own hour file. Hours older than the current one are gzipped; files
# older than HISTORY_DAYS are deleted.
HISTORY = STATE / "history"
HISTORY_DAYS = 7
_hist = {"file": None, "own": None, "pl": None}
ARMY_KEYS = ("id", "x", "y", "o", "w", "name", "st", "k", "n", "u", "hp", "mhp", "str", "def", "mor", "kills", "sight", "rng",
             "pts", "how", "eta", "dest", "spd", "loc", "atk", "by", "patrol", "next", "rc", "reach", "base")


def history_save(world):
    """Add this check to the history (called by the monitor after each successful read)."""
    import gzip
    HISTORY.mkdir(exist_ok=True)
    now = dt.datetime.now()
    name = HISTORY / f"{now:%Y-%m-%d-%H}.jsonl"
    rec = {"t": round(time.time()), "a": [{k: a[k] for k in ARMY_KEYS if a.get(k) not in (None, "", [], 0)} for a in world.get("armies", [])]}
    try:
        g = ghosts(world)
        if g:
            rec["g"] = [{k: v for k, v in x.items() if k != "net" and v not in (None, "", [])} for x in g]
    except Exception as ex:
        log(f"history: predictions not saved: {type(ex).__name__}: {ex}")
    new_file = _hist["file"] != name or not name.exists()
    if new_file or world.get("owners") != _hist["own"]:
        rec["own"] = world.get("owners")
    if new_file or world.get("players") != _hist["pl"]:
        rec["pl"] = world.get("players")
    if new_file and world.get("airfields"):
        rec["af"] = world["airfields"]
    if new_file and world.get("heal"):
        rec["hl"] = world["heal"]
    with name.open("a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    _hist.update({"file": name, "own": world.get("owners"), "pl": world.get("players")})
    if new_file:                                          # tidy up: compress finished hours, drop old ones
        cut = f"{now - dt.timedelta(days=HISTORY_DAYS):%Y-%m-%d-%H}"
        for old in HISTORY.glob("*.jsonl"):
            if old != name:
                with open(old, "rb") as fi, gzip.open(str(old) + ".gz", "wb") as fo:
                    shutil.copyfileobj(fi, fo)
                old.unlink()
        for old in HISTORY.glob("*.jsonl.gz"):
            if old.name[:13] < cut:
                old.unlink()


def _history_lines(path):
    import gzip
    try:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception:
        return


def history_index():
    """The saved moments: {"times": [seconds, ...]} (only the times, read quickly)."""
    times = []
    for path in sorted(HISTORY.glob("*.jsonl*")) if HISTORY.exists() else []:
        for rec in _history_lines(path):
            times.append(rec.get("t"))
    return {"times": [t for t in times if t]}


def history_at(t):
    """The world as saved at the check closest to time t (seconds), rebuilt from its hour file."""
    when = dt.datetime.fromtimestamp(t)
    files = sorted(HISTORY.glob("*.jsonl*")) if HISTORY.exists() else []
    key = f"{when:%Y-%m-%d-%H}"
    path = next((p for p in files if p.name.startswith(key)), None)
    if not path:                                          # nothing that hour: the nearest hour that has something
        earlier = [p for p in files if p.name[:13] <= key]
        path = earlier[-1] if earlier else (files[0] if files else None)
    if not path:
        return {"ok": False, "error": "No history saved yet."}
    own = pl = af = hl = None
    best = None
    for rec in _history_lines(path):
        own = rec.get("own", own)
        pl = rec.get("pl", pl)
        af = rec.get("af", af)
        hl = rec.get("hl", hl)
        if best is None or abs(rec["t"] - t) < abs(best["t"] - t):
            best = dict(rec, own=own, pl=pl, af=af, hl=hl)
        elif rec["t"] > t:
            break
    if not best:
        return {"ok": False, "error": "No history saved yet."}
    for g in best.get("g") or []:                        # predictions of groups that stood still: their roads again
        if g.get("rn") and not g.get("net"):
            g["net"] = reach_again(g.get("id"), g["rn"], best["own"], best["t"])
    return {"ok": True, "t": best["t"], "world": {"armies": best["a"], "owners": best["own"], "players": best["pl"],
                                                  "airfields": best.get("af") or [], "heal": best.get("hl") or [],
                                                  "ghosts": best.get("g") or [], "battles": [], "me": None}}
