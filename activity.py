"""When players give orders (are online), from changes in their units' orders and movement."""

import datetime as dt
import json
import math
import time


from common import (STATE, _read_json)
from predict import TRACKS
# ----------------------------------------------------------- player activity
# The game does not show when other players are online (their last login reads 0), but every new order a
# player gives one of their armies in your sight is a sign they were online then. The monitor compares the
# orders of every army in sight (foreign, allied and your own) between checks and writes each change to
# state/activity.jsonl:
#   new orders (not just the next leg of the same route), or orders cancelled before arriving.
# It also keeps state/activity-hours.json: which hours it was watching, so quiet hours are only claimed for
# hours that were actually watched.
ACTIVITY = STATE / "activity.jsonl"
ACT_HOURS = STATE / "activity-hours.json"
ACTIVITY_DAYS = 30
_orders = {}
_still = {}                                  # army id -> [x, y, checks standing still], for groups with hidden orders
ROUTES = STATE / "activity-routes.json"     # each army's last destination, to spot it resuming after a fight


def track_orders(data):
    """Compare each foreign army's orders with the last check, and note new ones."""
    now = time.time()
    events = []
    cur = {}
    routes = _read_json(ROUTES, {})                       # army id -> [x, y, time] of the last place it was sent
    routes_changed = False
    for e in data.get("enemies", []) + [f for f in data.get("friends", []) if "ord" in f]:
        if e.get("radar"):
            continue
        new = [tuple(c) for c in e.get("ord") or []]
        cur[e["id"]] = new
        moves = [c for c in new if len(c) == 3]
        prev_dest = routes.get(str(e["id"]))
        if moves and (not prev_dest or prev_dest[:2] != [moves[-1][1], moves[-1][2]]):
            routes[str(e["id"])] = [moves[-1][1], moves[-1][2], round(now)]   # where it is heading now
            routes_changed = True
        # its orders are hidden (enemies, neutrals): a group that stood still and starts moving was given an order
        if not new and not e.get("plane") and not e.get("air"):
            st = _still.get(e["id"])
            here = (e["x"], e["y"])
            if st and math.hypot(here[0] - st[0], here[1] - st[1]) > 3:
                if st[2] >= 2 and not e.get("fight"):    # was standing for a while, and not pushed by a fight
                    events.append({"t": round(now), "o": e.get("o"), "id": e["id"], "kind": "move", "at": e.get("at", ""),
                                   "pos": [round(e["x"]), round(e["y"])], "to": None, "dest": "", "seen_moving": True,
                                   "units": sum(n for _, n in e.get("units", [])), "u": e.get("units") or []})
                _still[e["id"]] = [here[0], here[1], 0]
            else:
                _still[e["id"]] = [here[0], here[1], (st[2] + 1) if st else 1]
        if e["id"] not in _orders:
            continue                                      # first time in sight: no way to tell what changed
        old = _orders[e["id"]]
        if new == old or (new and len(new) < len(old) and old[-len(new):] == new):
            continue                                      # same orders, or just moving on to the next leg
        if new and not moves:
            continue                                      # only waits (refuelling, landing, boarding): the game does these
        if moves and prev_dest and now - prev_dest[2] < 12 * 3600 and math.hypot(moves[-1][1] - prev_dest[0], moves[-1][2] - prev_dest[1]) < 6:
            continue                                      # same destination as before: it is carrying on, e.g. after winning a fight
        pl = e.get("plane") or {}
        if moves and pl and any(math.hypot(moves[-1][1] - q[0], moves[-1][2] - q[1]) < 6
                                for q in (pl.get("target"), pl.get("base")) if q):
            continue                                      # an aircraft going home or back to its patrol: automatic
        if not new:
            if any(c[0] == "ac" for c in old) or e.get("fight"):
                continue                                  # attack orders end by themselves; a fight stops a march
            last = next((c for c in reversed(old) if len(c) == 3), None)
            if not last or math.hypot(last[1] - e["x"], last[2] - e["y"]) < 6:
                continue                                  # arrived where it was going
            kind, to = "stopped", None
        else:
            kind, to = ("attack" if any(c[0] == "ac" for c in new) else "move"), (moves[-1][1:] if moves else None)
        events.append({"t": round(now), "o": e.get("o"), "id": e["id"], "kind": kind, "at": e.get("at", ""),
                       "pos": [round(e["x"]), round(e["y"])], "to": to, "dest": e.get("to", ""),
                       "units": sum(n for _, n in e.get("units", [])), "u": e.get("units") or []})
    _orders.clear()
    _orders.update(cur)
    for k in [k for k in _still if k not in cur]:
        del _still[k]
    if routes_changed:
        cut = now - 12 * 3600
        routes = {k: v for k, v in routes.items() if v[2] >= cut}
        tmp = ROUTES.with_suffix(".tmp")
        tmp.write_text(json.dumps(routes))
        tmp.replace(ROUTES)
    if events:
        with ACTIVITY.open("a") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
    hours = _read_json(ACT_HOURS, {})                      # this hour was watched, for the countries in sight
    key = f"{dt.datetime.now():%Y-%m-%d %H}"
    seen = {str(e.get("o")) for e in data.get("enemies", []) + data.get("friends", []) if not e.get("radar") and e.get("o") is not None}
    prev = hours.get(key) if isinstance(hours.get(key), list) else []
    if key not in hours or not seen <= set(prev):
        hours[key] = sorted(set(prev) | seen)
        cut = f"{dt.datetime.now() - dt.timedelta(days=ACTIVITY_DAYS):%Y-%m-%d %H}"
        hours = {k: v for k, v in hours.items() if k >= cut}
        tmp = ACT_HOURS.with_suffix(".tmp")
        tmp.write_text(json.dumps(hours))
        tmp.replace(ACT_HOURS)
    return events


def activity_summary(players):
    """Per foreign country: when its player gives orders, and the movements it repeats."""
    cut = time.time() - ACTIVITY_DAYS * 86400
    evs = []
    try:
        for line in ACTIVITY.read_text().splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("t", 0) >= cut:
                evs.append(ev)
    except FileNotFoundError:
        pass
    hrs = _read_json(ACT_HOURS, {})
    def watched_for(o=None):
        """how many days each hour of the day was watched (with that country's units in sight)"""
        w = [0] * 24
        for k, v in hrs.items():
            try:
                if o is None or not isinstance(v, list) or o in v:
                    w[int(k[-2:])] += 1
            except ValueError:
                pass
        return w
    watched = watched_for()
    tracks = _read_json(TRACKS, {})                       # what older orders (saved without unit types) moved
    out = {}
    for ev in evs:
        o = str(ev.get("o"))
        s = out.setdefault(o, {"o": ev.get("o"), "orders": 0, "hours": [0] * 24, "days": set(), "last": 0,
                               "dest": {}, "routes": {}, "kinds": {}, "recent": {}})
        when = dt.datetime.fromtimestamp(ev["t"])
        s["orders"] += 1
        s["hours"][when.hour] += 1
        s["days"].add(when.date())
        s["last"] = max(s["last"], ev["t"])
        s["kinds"][ev["kind"]] = s["kinds"].get(ev["kind"], 0) + 1
        u = ev.get("u") or (tracks.get(str(ev.get("id"))) or {}).get("units") or []
        s["recent"].setdefault(when.hour, []).append(
            {"t": ev["t"], "kind": ev["kind"], "id": ev.get("id"), "u": u, "n": ev.get("units"), "at": ev.get("at", ""),
             "dest": ev.get("dest", ""), "pos": ev.get("pos"), "to": ev.get("to"), "seen": bool(ev.get("seen_moving"))})
        dest, at = (ev.get("dest") or "").strip(), (ev.get("at") or "").strip()
        if dest:
            s["dest"][dest] = s["dest"].get(dest, 0) + 1
            if at and at != dest:
                r = f"{at} → {dest}"
                s["routes"][r] = s["routes"].get(r, 0) + 1
    res = []
    for o, s in out.items():
        h = s["hours"]
        # the busiest 4-hour window of the day (it may wrap past midnight)
        best = max(range(24), key=lambda i: sum(h[(i + j) % 24] for j in range(4)))
        in_win = sum(h[(best + j) % 24] for j in range(4))
        mine = watched_for(o)
        quiet = [i for i in range(24) if h[i] == 0 and mine[i] >= 2]
        shuttles = []
        for r, n in s["routes"].items():
            a, b = r.split(" → ")
            back = s["routes"].get(f"{b} → {a}", 0)
            if back and a < b:
                shuttles.append([a, b, n + back])
        p = players.get(o) or players.get(int(o)) if isinstance(players, dict) else None
        res.append({"o": s["o"], "nation": (p or [""])[0], "player": (p or ["", "", ""])[2] if p and len(p) > 2 else "",
                    "rel": (p or ["", "none"])[1], "orders": s["orders"], "days": len(s["days"]), "hours": h,
                    "last_min": round((time.time() - s["last"]) / 60),
                    "busy": [best, (best + 4) % 24, round(in_win / s["orders"] * 100)] if s["orders"] >= 5 else None,
                    "quiet": quiet, "watched": mine, "dest": sorted(([k, v] for k, v in s["dest"].items() if v >= 2), key=lambda x: -x[1])[:5],
                    "routes": sorted(([k, v] for k, v in s["routes"].items() if v >= 2), key=lambda x: -x[1])[:5],
                    "shuttles": sorted(shuttles, key=lambda x: -x[2])[:3], "kinds": s["kinds"],
                    # the latest orders in each hour of the day, newest first: what was moved, from where to where
                    "recent": {str(h): sorted(v, key=lambda x: -x["t"])[:8] for h, v in s["recent"].items()}})
    res.sort(key=lambda r: -r["orders"])
    return {"players": res, "watched": watched, "events": len(evs)}
