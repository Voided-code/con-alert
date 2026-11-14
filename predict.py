"""Where out-of-sight units probably are now, how well that has worked, and the movement network."""

import datetime as dt
import json
import math
import time


from common import (STATE, _read_json, config, log)
from game import (viewer_call, viewer_pid)
# ------------------------------------------------------------ predictions
# Where foreign armies that went out of sight probably are now. The monitor remembers each foreign army's
# last sighting and its planned route (state/tracks.json); predict() moves it along that route to now.
# Each guess gets an uncertainty radius, and is dropped when it is not good enough:
#   - not seen for more than PREDICT_HOURS (aircraft are followed to their home airbase, which they
#     must land at; only an aircraft whose airbase is unknown is dropped after 20 minutes)
#   - uncertainty larger than PREDICT_MAX_ERROR map units (on the page: a percentage of that, 100% = hidden)
#   - the guessed spot is inside the sight of one of your or your allies' groups (it would be seen there)
TRACKS = STATE / "tracks.json"
PREDICT_HOURS = 6
PREDICT_MAX_ERROR = 70
PREDICT_MIN_CHANCE = 0.4         # predictions with a chance below this are hidden (changeable in Settings)


def min_chance():
    """The chance below which a prediction is hidden: as set on the page (Settings), else PREDICT_MIN_CHANCE."""
    try:
        return min(0.95, max(0.05, float(config()["hide_below"]) / 100))
    except Exception:
        return PREDICT_MIN_CHANCE
# How fast the chance "it is still doing what it was doing" falls, in minutes: after that many minutes unseen
# the chance is down to about 37% (it is 100% at the moment it leaves sight). Rough values from how the game
# is usually played; the record of past predictions can tune them later.
CHANCE_MINUTES = {"parked": 600, "standing": 480, "patrol": 300, "flying": 240}        # aircraft: hidden when the chance it is where predicted drops below this


def motion(old, e, now):
    """How a group is moving, from its last two sightings: {"dir": compass, "v": units/min, "deg"} or None.
    For countries you do not share intelligence with this is all there is: the game shows where their
    groups are, not where they are going."""
    if not old or not old.get("pos") or now - old.get("seen", 0) > 5 * 60000 or e.get("plane"):
        return old.get("mv") if old and now - old.get("seen", 0) <= 5 * 60000 else None
    dx, dy = e["x"] - old["pos"][0], e["y"] - old["pos"][1]
    dmin = max(0.2, (now - old["seen"]) / 60000)
    dist = math.hypot(dx, dy)
    if dist < 1.5:
        return None if dmin > 1.5 else old.get("mv")      # no clear move yet: keep the last reading briefly
    deg = (math.degrees(math.atan2(dx, -dy)) + 360) % 360  # 0 = north (up on the map)
    names = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]
    return {"dir": names[round(deg / 45) % 8], "deg": round(deg), "v": round(dist / dmin, 2)}


# ------------------------------------------------------- checking itself
# Every time a group that was out of sight comes back into view, the monitor checks what it had predicted
# for it against where it really is, and writes that down (state/prediction-checks.jsonl). From that record:
#   - how fast the "still doing the same thing" chance falls, per kind of behaviour, is learnt (once there
#     are 8 or more checks of that kind), instead of the starting guesses in CHANCE_MINUTES;
#   - how far off route predictions really end up is learnt, and their error circles are scaled to match;
#   - the page shows how often each kind of prediction turned out right.
CHECKS = STATE / "prediction-checks.jsonl"
_learnt = {"at": 0, "minutes": dict(CHANCE_MINUTES), "err": 1.0, "record": {}}


def predict_kind(t):
    """Which behaviour a prediction for this track is based on."""
    p = t.get("plane") or {}
    if p.get("base"):
        if not p.get("flying"):
            return "parked"
        if p.get("homing"):
            return "homing"
        return "patrol" if p.get("st") in ("patrolling", "attacking") else "flying"
    if len([k for k in (t.get("knots") or []) if len(k) == 3]) >= 2:
        return "route"
    if (t.get("move") or {}).get("fixed"):
        return "fixed"
    if t.get("mv"):
        return "moving"
    return "standing"


KIND_TEXT = {"parked": "aircraft parked at base", "homing": "aircraft flying home", "patrol": "aircraft on patrol or attacking",
             "flying": "aircraft on a mission", "route": "groups following their orders", "fixed": "buildings and batteries",
             "moving": "groups seen moving", "standing": "groups standing still"}


def check_prediction(old, e, now):
    """A group came back into sight: how good was the prediction for it?"""
    gap = (now - old.get("seen", now)) / 60000
    if gap < 3 or gap > PREDICT_HOURS * 60:
        return
    try:
        x, y, err, note, _, chance = predict(old, now, 15393)
    except Exception:
        return
    dx = abs(e["x"] - x) % 15393
    dist = math.hypot(min(dx, 15393 - dx), e["y"] - y)
    kind = predict_kind(old)
    if chance is None:                                     # a distance guess: right if inside its circle
        hit = dist <= max(err, 8)
    else:                                                  # a chance guess: right if it was there (or in its patrol area)
        hit = dist <= max(err, 25 if old.get("plane") else 12)
    rec = {"t": round(now / 1000), "kind": kind, "min": round(gap, 1), "hit": hit, "dist": round(dist, 1),
           "err": round(err, 1), "chance": round(chance, 3) if chance is not None else None, "o": old.get("o")}
    with CHECKS.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    _learnt["at"] = 0                                      # learn again with this one


def learn():
    """Re-read the record (at most once a minute) and update the learnt values."""
    if time.time() - _learnt["at"] < 60:
        return _learnt
    _learnt["at"] = time.time()
    recs = []
    try:
        for line in CHECKS.read_text().splitlines()[-5000:]:
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    minutes, record = dict(CHANCE_MINUTES), {}
    for kind in KIND_TEXT:
        rs = [r for r in recs if r["kind"] == kind]
        if rs:
            record[kind] = {"n": len(rs), "hit": sum(1 for r in rs if r["hit"])}
        base = {"homing": "flying", "moving": "standing"}.get(kind, kind)
        ch = [r for r in rs if r.get("chance") is not None and r["min"] > 0]
        if len(ch) >= 8 and base in CHANCE_MINUTES:
            # the rate that best explains which ones were still there: chance = exp(-minutes / rate)
            def score(rate):
                s = 0.0
                for r in ch:
                    p = min(0.999, max(0.001, math.exp(-r["min"] / rate)))
                    s += math.log(p) if r["hit"] else math.log(1 - p)
                return s
            grid = [20 * 1.15 ** i for i in range(40)]
            minutes[kind] = round(max(grid, key=score))
    routes = sorted(r["dist"] / max(r["err"], 1) for r in recs if r["kind"] == "route" and r.get("chance") is None)
    err = 1.0
    if len(routes) >= 8:                                  # scale route error circles so about 8 in 10 land inside
        err = min(3.0, max(0.5, routes[int(len(routes) * 0.8)]))
    _learnt.update({"minutes": minutes, "err": err, "record": record, "checks": len(recs)})
    return _learnt


def chance_minutes():
    """How fast the chance falls, per behaviour: learnt from past checks where there are enough of them."""
    m = dict(learn()["minutes"])
    m.setdefault("homing", m.get("flying", CHANCE_MINUTES["flying"]))
    m.setdefault("moving", 60)
    return m


def update_tracks(data):
    """After a successful read: remember every foreign army in sight, drop old ones."""
    now = time.time() * 1000
    tracks = _read_json(TRACKS, {})
    seen = set()
    for e in data.get("enemies", []):
        if e.get("radar"):
            continue                                      # radar blips are not identified, nothing to follow
        seen.add(str(e["id"]))
        old = tracks.get(str(e["id"])) or {}
        if old:
            try:
                check_prediction(old, e, now)
            except Exception as ex:
                log(f"prediction check failed: {type(ex).__name__}: {ex}")
        mv = motion(old, e, now)
        plane = dict(e["plane"]) if e.get("plane") else None
        trail = [p for p in (old.get("trail") or []) if now - p[2] < 3 * 60000] + [[round(e["x"]), round(e["y"]), now]]
        trail = trail[-8:]
        if plane and plane.get("base") and len(trail) >= 4:
            # a straight line heading for its own airbase: it is on its way home (to refuel)
            (x0, y0, _), (x1, y1, _) = trail[0], trail[-1]
            step = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(trail, trail[1:]))
            straight = step > 6 and math.hypot(x1 - x0, y1 - y0) > 0.9 * step
            bx, by = plane["base"]
            to_base = math.atan2(by - y1, bx - x1)
            heading = math.atan2(y1 - y0, x1 - x0)
            diff = abs((heading - to_base + math.pi) % (2 * math.pi) - math.pi)
            plane["homing"] = bool(straight and diff < math.radians(30) and math.hypot(bx - x1, by - y1) > 8)
        tracks[str(e["id"])] = {"mv": mv, "o": e.get("o"), "nation": e.get("nation", ""), "war": e.get("war"), "units": e.get("units", []),
                                "air": e.get("air"), "sea": e.get("sea"), "spd": e.get("spd", 0), "knots": e.get("knots", []),
                                "pos": [round(e["x"]), round(e["y"])], "seen": now, "to": e.get("to", ""), "plane": plane, "trail": trail,
                                "move": e.get("move"), "hp": e.get("hp"), "mhp": e.get("mhp"), "uhp": e.get("uhp"),
                                "fight": e.get("fight")}
    eyes = [(f["x"], f["y"], f.get("sight") or 0) for f in data.get("friends", [])]
    mapw = (data.get("view") or {}).get("mapw") or 15393
    for k in [k for k, t in tracks.items() if k not in seen and now - t["seen"] < 90e3
              and not submerged(t) and in_sight(eyes, t["pos"][0], t["pos"][1], mapw, 0.8)]:
        del tracks[k]                                     # vanished in plain view: destroyed or merged
    tracks = {k: t for k, t in tracks.items() if now - t["seen"] < PREDICT_HOURS * 3600e3}
    try:
        tmp = TRACKS.with_suffix(".tmp")
        tmp.write_text(json.dumps(tracks))
        tmp.replace(TRACKS)                               # atomic: the page server reads this file
    except Exception:
        pass
    return seen


def in_sight(eyes, x, y, mapw, share=0.9):
    """Is the point well inside the sight circle of one of the given groups [(x, y, sight)]?"""
    for ex, ey, r in eyes:
        dx = abs(ex - x) % mapw
        if min(dx, mapw - dx) ** 2 + (ey - y) ** 2 < (r * share) ** 2:
            return True
    return False


def submerged(t):
    """Submarines can vanish from sight without leaving: ordinary sight does not rule them out."""
    return any("submarine" in str(u[0]).lower() for u in t.get("units", []))


def plane_cycle(t, el, spd, mapw, stay):
    """An aircraft patrolling or attacking a spot keeps doing so until its player gives a new order: when its
    fuel runs low the game flies it home, refuels it and sends it back to the same spot by itself (seen in the
    game: a refuelling plane has its patrol order queued up after the refuel). Walk that cycle to now."""
    p = t["plane"]
    bx, by = p["base"]
    cx, cy = p["target"]
    name = p.get("baseName") or "its airbase"
    clock = lambda ms: f"{dt.datetime.fromtimestamp(ms / 1000):%H:%M}"
    v = max(spd, 1.0)
    d = lambda ax, ay, bx_, by_: math.hypot(((bx_ - ax) + mapw / 2) % mapw - mapw / 2, by_ - ay)
    leg = d(cx, cy, bx, by) / v * 60000                    # base <-> its spot, in ms
    fuel = (p.get("fuel") or 14400) * 1000
    refuel = (p.get("refuel") or 900) * 1000
    now = t["seen"] + el * 60000
    chance = stay(el, chance_minutes()["patrol"])
    area = p.get("patrol") or 50
    x0, y0 = t["pos"]
    arrive = t["seen"] + d(x0, y0, cx, cy) / v * 60000      # still on its way to the spot when last seen?
    turn = p.get("mustReturn") or (t["seen"] + fuel - leg)  # when it must head home (unknown: assume it just set off)
    guess = "" if p.get("mustReturn") else " (when it took off is not known, so this time is a guess)"
    if p.get("homing"):                                   # seen flying straight for its base: it was already going home
        chance = stay(el, chance_minutes()["homing"])
        leg_home = d(x0, y0, bx, by) / v * 60000
        home = t["seen"] + leg_home
        if now < home:
            f = (now - t["seen"]) / max(1.0, leg_home)
            return x0 + (((bx - x0) + mapw / 2) % mapw - mapw / 2) * f, y0 + (by - y0) * f, 0, f"was flying home to {name} when last seen", None, chance
        ready = home + refuel
        if now < ready:
            return bx, by, 0, f"was flying home when last seen; refuelling at {name} until {clock(ready)}, then back out to its spot", None, chance
        back = ready + leg
        if now < back:
            f = (now - ready) / max(1.0, leg)
            return bx + (((cx - bx) + mapw / 2) % mapw - mapw / 2) * f, by + (cy - by) * f, 0, "refuelled; flying back out to its spot", None, chance
        turn = ready + fuel - leg
        arrive = 0
    if now < arrive:
        f = (now - t["seen"]) / max(1.0, arrive - t["seen"])
        return x0 + (((cx - x0) + mapw / 2) % mapw - mapw / 2) * f, y0 + (cy - y0) * f, 0, "flying to its patrol spot", turn, chance
    for _ in range(50):                                   # each pass: patrol, fly home, refuel, fly back out
        if now < turn:
            return cx, cy, area, f"{'attacking' if p.get('st') == 'attacking' else 'patrolling'} over its spot until about {clock(turn)}, when it must fly home to {name} to refuel{guess}", turn, chance
        home = turn + leg
        if now < home:
            f = (now - turn) / max(1.0, leg)
            return cx + (((bx - cx) + mapw / 2) % mapw - mapw / 2) * f, cy + (by - cy) * f, 0, f"flying home to {name} to refuel", None, chance
        ready = home + refuel
        if now < ready:
            return bx, by, 0, f"refuelling at its airbase {name} until {clock(ready)}, then back out to its spot", None, chance
        back = ready + leg
        if now < back:
            f = (now - ready) / max(1.0, leg)
            return bx + (((cx - bx) + mapw / 2) % mapw - mapw / 2) * f, by + (cy - by) * f, 0, "refuelled; flying back out to its spot", None, chance
        turn = ready + fuel - leg                          # a full tank again
    return cx, cy, area, "patrolling over its spot", None, chance


def predict_plane(t, el, spd, mapw):
    """Aircraft (and helicopters) fly from their home airbase and must land there again, and after landing they
    refuel before they can fly again. Once out of sight an aircraft is either exactly where its last orders put
    it (on patrol, flying home, refuelling or parked at its base) or somewhere else entirely on a new mission -
    there is no "a bit off". So instead of a distance it gets a chance of being there.
    Returns (x, y, area, note, back_by, chance); area is its patrol circle, else 0."""
    p = t["plane"]
    bx, by = p["base"]
    name = p.get("baseName") or "its airbase"
    x0, y0 = t["pos"]
    dx = ((bx - x0) + mapw / 2) % mapw - mapw / 2          # shortest way, the map wraps around
    home = math.hypot(dx, by - y0) / max(spd, 1.0)         # minutes it needs to fly home
    refuel = (p.get("refuel") or 900) / 60                 # minutes it refuels after landing
    clock = lambda ms: f"{dt.datetime.fromtimestamp(ms / 1000):%H:%M}"
    # chance it has NOT been given new orders since: 100% when it leaves sight, falling the longer it is unseen.
    # While it refuels it cannot take off, so the clock stops for that time.
    stay = lambda minutes, scale: math.exp(-max(0.0, minutes) / scale)
    def parked(landed_after, chance_then, how):
        """It landed `landed_after` minutes after last seen: refuelling, then parked."""
        ready = t["seen"] + (landed_after + refuel) * 60000
        if el < landed_after + refuel:
            return bx, by, 0, f"{how} at its airbase {name}; refuelling until {clock(ready)}", None, chance_then
        return (bx, by, 0, f"{how} at its airbase {name}; refuelled and ready again since {clock(ready)}", None,
                chance_then * stay(el - landed_after - refuel, chance_minutes()["parked"]))
    if p.get("refuelEnd") and p["refuelEnd"] > t["seen"]:  # it was refuelling when last seen
        return parked((p["refuelEnd"] - t["seen"]) / 60000 - refuel, 1.0, "was refuelling")
    if not p.get("flying"):
        return bx, by, 0, f"on the ground at its airbase {name} when last seen", None, stay(el, chance_minutes()["parked"])
    k = [q for q in (t.get("knots") or []) if len(q) == 3]
    extra = 0.0                                            # minutes spent on its planned route before turning home
    if len(k) >= 2 and k[-1][2] > t["seen"] and p.get("st") != "patrolling":
        now_ms = t["seen"] + el * 60000
        if now_ms <= k[-1][2]:
            return None                                   # still on its way out: follow its plan (predict() below)
        x0, y0 = k[-1][0], k[-1][1]                       # plan done: from where it ended, it flies home
        extra = (k[-1][2] - t["seen"]) / 60000
        dx = ((bx - x0) + mapw / 2) % mapw - mapw / 2
        home = math.hypot(dx, by - y0) / max(spd, 1.0)
    if p.get("st") in ("patrolling", "attacking") and p.get("target"):
        return plane_cycle(t, el, spd, mapw, stay)
    flown = el - extra
    if flown < home:                                       # flying home after its mission
        f = flown / home
        return x0 + dx * f, y0 + (by - y0) * f, 0, f"flying back to its airbase {name}", None, stay(el, chance_minutes()["flying"])
    return parked(extra + home, stay(extra + home, chance_minutes()["flying"]), "back")


def predict(t, now_ms, mapw):
    """Where a track should be at now_ms: (x, y, error radius, note, back_by), or None when it cannot be told."""
    el = (now_ms - t["seen"]) / 60000                     # minutes since last seen
    spd = max(0.05, float(t.get("spd") or 0))             # map units per minute
    plane = bool(t.get("plane") and t["plane"].get("base"))
    if plane:
        r = predict_plane(t, el, spd, mapw)
        if r:
            x, y, err, note, back_by, chance = r
            return x % mapw, y, err, note, back_by, chance
    k = [p for p in (t.get("knots") or []) if len(p) == 3]
    if len(k) >= 2 and k[-1][2] > t["seen"]:
        if now_ms <= k[-1][2]:
            i = max(j for j in range(len(k) - 1) if k[j][2] <= now_ms) if k[0][2] <= now_ms else 0
            a, b = k[i], k[i + 1]
            f = 0 if b[2] <= a[2] else min(1, max(0, (now_ms - a[2]) / (b[2] - a[2])))
            x, y = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
            note = f"on its way to {t.get('to') or 'its destination'}"
            after = 0
        else:
            x, y = k[-1][0], k[-1][1]
            note = f"should have arrived at {t.get('to') or 'its destination'}"
            after = (now_ms - k[-1][2]) / 60000
        # distance travelled along the plan since it was last seen: each leg's timing is the game's own, so only
        # a small allowance (3%) for slow-downs such as fights; and its orders may have changed (grows with time)
        travelled, px, py = 0.0, t["pos"][0], t["pos"][1]
        for p in k[1:]:
            if p[2] > now_ms:
                break
            travelled += math.hypot(p[0] - px, p[1] - py)
            px, py = p[0], p[1]
        travelled += math.hypot(x - px, y - py)
        err = 3 + 0.03 * travelled + 0.15 * el + 0.3 * spd * after
    else:
        x, y = t["pos"]
        note = "standing still when last seen"
        if (t.get("move") or {}).get("fixed"):            # a building or battery: cannot move
            return x % mapw, y, 3, "cannot move: still here", None, None
        if t.get("move") and t["move"].get("tbl"):        # still there, or moved along the roads / sea lanes
            if t.get("mv"):                               # it was moving: where to is not shown for enemies
                return (x % mapw, y, 0, f"was moving {t['mv']['dir']} when last seen; where to is not known", None,
                        0.5 * math.exp(-el / chance_minutes()["moving"]))
            return x % mapw, y, 0, note, None, math.exp(-el / chance_minutes()["standing"])
        err = 4 + 0.5 * spd * el
    if plane:                                             # an aircraft on its way out to its target: on its route or not
        return x % mapw, y, 0, note, None, math.exp(-el / chance_minutes()["flying"])
    return x % mapw, y, err, note, None, None


# ------------------------------------------------- the movement network
# Every road, track, rail link and sea lane on the map, as the game has them: about 30,000 short links between
# points. Read from the game once and kept in state/net-<map>.json. Used to show where a group that stood
# still could have got to since.
NET_JS = """(function(){
  try {
    const ms = hup.gameState.getMapState(), locs = [], idx = {}, segs = [];
    for (const l of ms.map.locations) {
      if (!l || !l.connections) continue;
      idx[l.locationID] = locs.length; locs.push([l.locationID, l.terrainType]);
      for (const c of l.connections) {
        try { const a = c.getP1(), b = c.getP2();
          segs.push(Math.round(a.x * 2) / 2, Math.round(a.y * 2) / 2, Math.round(b.x * 2) / 2, Math.round(b.y * 2) / 2, c.getTerrainClass(), idx[l.locationID]);
        } catch (e) {}
      }
    }
    return JSON.stringify({ok: true, id: ms.map.mapID, locs, segs});
  } catch (e) { return JSON.stringify({ok: false, error: String(e)}); }
})()"""
_net = {}


def net():
    """The network as {nodes: [(x, y)], adj: [[(node, length, class, terrain, location id)]]}, or None."""
    if _net.get("adj"):
        return _net
    cached = sorted(STATE.glob("net-*.json"), key=lambda p: p.stat().st_mtime)
    d = None
    if cached:
        try:
            d = json.loads(cached[-1].read_text())
        except Exception:
            d = None
    if not d and viewer_pid():
        try:
            text = viewer_call("js", code=NET_JS, timeout=60)["result"]
            d = json.loads(text)
            if d.get("ok"):
                (STATE / f"net-{d['id']}.json").write_text(text)
        except Exception:
            d = None
    if not d or not d.get("ok"):
        return None
    locs, s = d["locs"], d["segs"]
    W = 15393
    try:
        W = int(json.loads((sorted(STATE.glob("geo-*.json"), key=lambda p: p.stat().st_mtime)[-1]).read_text())["w"])
    except Exception:
        pass
    key, nodes, adj = {}, [], []
    def node(x, y):
        x = round(x % W, 1)                               # the map wraps: the same point on both edges is one point
        k = (x, y)
        if k not in key:
            key[k] = len(nodes)
            nodes.append(k)
            adj.append([])
        return key[k]
    for i in range(0, len(s) - 5, 6):
        x1, y1, x2, y2, cls, li = s[i:i + 6]
        if li is None or (x1 == x2 and y1 == y2):
            continue
        a, b = node(x1, y1), node(x2, y2)
        ln = math.hypot(x2 - x1, y2 - y1)                 # stored unwrapped, so this is the true length
        loc_id, terrain = locs[li]
        adj[a].append((b, ln, cls, terrain, loc_id))
        adj[b].append((a, ln, cls, terrain, loc_id))
    _net.update({"nodes": nodes, "adj": adj, "w": W})
    return _net


def reach_network(t, minutes, owner_of, max_segs=2500):
    """Where a group standing at t["pos"] could have got to in `minutes`, moving along the network with the
    game's own speeds for its unit types (terrain, foreign land), calibrated to its speed when last seen.
    Returns a flat list of links reached [x1, y1, x2, y2, ...] (the last one cut where its time ran out)."""
    g, m = net(), t.get("move") or {}
    if not g or not m.get("tbl") or minutes <= 0:
        return []
    tbl, ff, sea = m["tbl"], m.get("ff") or 1, m.get("cls") == "sea"
    usable = (2,) if sea else (0, 3)
    def speed(cls, terrain, loc_id):
        if cls == 3 and "3" in tbl:
            v = tbl["3"]
        else:
            v = tbl.get(str(terrain), tbl.get("2" if sea else "0", 0))
        if not sea and owner_of.get(loc_id, t.get("o")) != t.get("o"):
            v *= ff                                      # slower in someone else's land
        return v
    here = tbl.get(str(m.get("tt")), tbl.get("2" if sea else "0", 0)) * (1 if (m.get("own") or sea) else ff)
    factor = min(1.5, max(0.3, (t.get("spd") or here) / here)) if here else 1   # its own condition (morale etc.)
    x0, y0 = t["pos"]
    nodes, adj, W = g["nodes"], g["adj"], g.get("w", 15393)
    wdx = lambda a, b: (a - b + W / 2) % W - W / 2       # shortest way across the map edge
    start = sorted((math.hypot(wdx(nx, x0), ny - y0), i) for i, (nx, ny) in enumerate(nodes)
                   if abs(wdx(nx, x0)) < 25 and abs(ny - y0) < 25 and any(e[2] in usable for e in adj[i]))[:3]
    if not start:
        return []
    import heapq
    best, heap = {}, []
    v0 = (here * factor) or 1                             # its speed on the ground it stands on
    for d0, i in start:                                   # getting onto the network takes time too
        t0 = d0 / v0
        if t0 <= minutes and t0 < best.get(i, 1e18):
            best[i] = t0
            heapq.heappush(heap, (t0, i))
    out, done = [], set()
    while heap and len(out) < max_segs * 4:
        tm, i = heapq.heappop(heap)
        if tm > best.get(i, 1e18) or i in done:
            continue
        done.add(i)
        for j, ln, cls, terrain, loc_id in adj[i]:
            if cls not in usable:
                continue
            v = speed(cls, terrain, loc_id) * factor
            if v <= 0:
                continue
            need = ln / v
            (xa, ya), (xb, yb) = nodes[i], nodes[j]
            xb = xa + wdx(xb, xa)                         # draw across the map edge the short way
            if tm + need <= minutes:
                if tm + need < best.get(j, 1e18):
                    best[j] = tm + need
                    heapq.heappush(heap, (tm + need, j))
                out += [xa, ya, xb, yb]
            else:                                        # only part of this link
                f = (minutes - tm) / need
                out += [xa, ya, round(xa + (xb - xa) * f, 1), round(ya + (yb - ya) * f, 1)]
    return out


def plane_alternatives(t, chance, near, world, mapw):
    """Where else an aircraft could be when it is not where predicted, with rough chances that add up to
    100% together with `chance`. A guess from how the game works, not from any data about the player:
      - on patrol when last seen: may already have been called home to its base
      - may have been moved to another of its country's airbases (the three nearest to its home base)
      - otherwise on some new mission, anywhere it can reach from its base"""
    p = t["plane"]
    bx, by = p["base"]
    rest, alts = 1.0 - chance, []
    if p.get("st") in ("patrolling", "attacking") and p.get("flying") and p.get("target"):
        if near:                                          # predicted at its base: it may already be back out
            tx, ty = p["target"]
            alts.append({"where": "already back out over its patrol spot", "x": tx, "y": ty, "share": 0.5})
        else:                                             # predicted at its spot: it may have gone home early
            alts.append({"where": f"gone home early to its airbase {p.get('baseName') or ''}".strip(), "x": bx, "y": by, "share": 0.5})
    def dist(x, y):
        dx = abs(x - bx) % mapw
        return math.hypot(min(dx, mapw - dx), y - by)
    others = sorted((a for a in (world.get("airfields") or [])
                     if a[2] == t.get("o") and dist(a[0], a[1]) > 5 and a[3] != p.get("baseName")), key=lambda a: dist(a[0], a[1]))[:3]
    if others:
        weights = [1 / max(50.0, dist(a[0], a[1])) for a in others]
        left = 1.0 - sum(x["share"] for x in alts)
        for a, w in zip(others, weights):
            alts.append({"where": f"moved to its airbase {a[3]}", "x": a[0], "y": a[1], "share": left * 0.4 * w / sum(weights)})
    used = sum(x["share"] for x in alts)
    alts.append({"where": f"on a new mission, anywhere within {p.get('reach') or 'its range'} of {p.get('baseName') or 'its base'}",
                 "x": None, "y": None, "share": 1.0 - used})
    return [{"where": a["where"], "x": a["x"], "y": a["y"], "pct": round(rest * a["share"] * 100)} for a in alts if rest * a["share"] >= 0.005]


_reach_cache = {}


def owners_by_location(owners):
    """Location id -> owner, from the province owners list (in the map's land order)."""
    try:
        land = json.loads((sorted(STATE.glob("geo-*.json"), key=lambda p: p.stat().st_mtime)[-1]).read_text())["land"]
        return {p["id"]: o for p, o in zip(land, owners or [])}
    except Exception:
        return {}


def reach_again(gid, rn, owners, when):
    """The roads / sea lanes of a saved prediction, worked out again from what it was based on (for the history)."""
    return reach_cached(f"h{gid}-{when}", {"pos": rn["pos"], "move": rn["move"], "spd": rn.get("spd"), "o": rn.get("o")},
                        rn["el"], owners_by_location(owners))


def reach_cached(tid, t, el, owner_of):
    """reach_network, worked out at most once a minute per group (the page asks every few seconds)."""
    key = (tid, int(el))
    if key not in _reach_cache:
        if len(_reach_cache) > 200:
            _reach_cache.clear()
        try:
            _reach_cache[key] = [round(v, 1) for v in reach_network(t, el, owner_of)]
        except Exception as ex:
            log(f"reach failed: {type(ex).__name__}: {ex}")
            _reach_cache[key] = []
    return _reach_cache[key]


def ghosts(world):
    """Predicted positions of foreign armies that are out of sight, for the page's map (only good guesses)."""
    tracks = _read_json(TRACKS, {})
    if not tracks or not world:
        return []
    try:
        if time.time() - TRACKS.stat().st_mtime > 120:   # the monitor is stopped or not reading: out of date
            return []
    except OSError:
        return []
    now, mapw = time.time() * 1000, 15393
    try:
        mapw = int(json.loads((sorted(STATE.glob("geo-*.json"), key=lambda p: p.stat().st_mtime)[-1]).read_text())["w"])
    except Exception:
        pass
    visible = {str(a.get("id")) for a in world.get("armies", [])}
    owner_of = owners_by_location(world.get("owners"))   # for the foreign-land slowdown
    rel = {str(k): v[1] for k, v in (world.get("players") or {}).items()}
    eyes = [(a["x"], a["y"], a.get("sight") or 0) for a in world.get("armies", [])
            if rel.get(str(a.get("o"))) in ("me", "ally") and not a.get("rc")]
    out, hide_below = [], min_chance()
    for tid, t in tracks.items():
        if tid in visible:
            continue
        el = (now - t["seen"]) / 60000
        if el < 0.5 or el > PREDICT_HOURS * 60 or (t.get("air") and not (t.get("plane") or {}).get("base") and el > 20):
            continue                                      # (aircraft whose airbase is unknown: only briefly)
        x, y, err, note, back_by, chance = predict(t, now, mapw)
        kind = predict_kind(t)
        if chance is None and kind == "route":
            err *= learn()["err"]
        if chance is not None and chance < hide_below:
            continue                                      # aircraft: too likely to be off on another mission
        if chance is None and err > PREDICT_MAX_ERROR:
            continue
        alts = None
        if not submerged(t) and in_sight(eyes, x, y, mapw):
            base = (t.get("plane") or {}).get("base")
            if not base or chance is None:
                continue                                  # it would be seen there, so it is not there
            # an aircraft: not where expected (your units would see it), so take the next most likely place
            first = "where its orders put it" if " over its spot" not in note else "over its patrol spot"
            alts = [a for a in plane_alternatives(t, chance, False, world, mapw)
                    if a["x"] is not None and not in_sight(eyes, a["x"], a["y"], mapw)]
            rest = [a for a in plane_alternatives(t, chance, False, world, mapw) if a["x"] is None]
            if not alts:
                continue
            total = sum(a["pct"] for a in alts + rest) or 1
            top = max(alts, key=lambda a: a["pct"])
            x, y, err = top["x"], top["y"], 0
            chance = top["pct"] / total * (1 - (1 - chance) * 0.3)     # renormalised over the places still possible
            note = f"not {first}, since your units would see it there; most likely {top['where']}"
            alts = [dict(a, pct=round(a["pct"] / total * 100)) for a in alts + rest if a is not top]
            if chance < hide_below * 0.75:
                continue
        out.append({"id": int(tid), "o": t.get("o"), "x": round(x), "y": round(y), "err": round(err), "ago": round(el),
                    "unc": max(1, min(99, round(err / PREDICT_MAX_ERROR * 100))),   # uncertainty: at 100% it is hidden
                    "sure": round(chance * 100) if chance is not None else None,      # aircraft: chance it is there
                    "reach": (t.get("plane") or {}).get("reach"), "base_xy": (t.get("plane") or {}).get("base"),
                    "else": alts if alts is not None else (plane_alternatives(t, chance, (x, y) == tuple(t["plane"]["base"]), world, mapw)
                             if chance is not None and (t.get("plane") or {}).get("base") else
                             [{"where": ("carried on " + t["mv"]["dir"] + " along the highlighted roads or sea lanes") if t.get("mv")
                               else "moved somewhere along the roads or sea lanes it could reach (highlighted)", "x": None, "y": None,
                               "pct": round((1 - chance) * 100)}] if chance is not None and round((1 - chance) * 100) > 0 else []),
                    "net": (reach_cached(tid, t, el, owner_of) if chance is not None and not (t.get("plane") or {}).get("base") else []),
                    # what the roads / sea lanes were worked out from, so the history can draw them again
                    "rn": ({"pos": t["pos"], "move": t["move"], "spd": t.get("spd"), "o": t.get("o"), "el": round(el, 1)}
                           if chance is not None and not (t.get("plane") or {}).get("base") and (t.get("move") or {}).get("tbl") else None),
                    "note": note, "units": t.get("units", []), "air": t.get("air"), "sea": t.get("sea"),
                    "last": t.get("pos"), "to": t.get("to", ""),
                    "base": (t.get("plane") or {}).get("baseName", ""), "back_by": back_by,
                    "kind": KIND_TEXT.get(kind, kind), "record": learn()["record"].get(kind),
                    "hp": t.get("hp"), "mhp": t.get("mhp"), "uhp": t.get("uhp"), "fight": t.get("fight")})
    return out
