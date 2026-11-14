"""The watch loop: every INTERVAL seconds read the game, update predictions and send alerts."""

import datetime as dt
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

from PIL import Image, ImageDraw

from common import (ALERTS, BLIND_NOTIFY_MINUTES, BLIND_REASONS, CLEAR_CHECKS, INTERVAL, PIDFILE,
                    RADAR_ALERTS, SKIP_AIRCRAFT, STALE_MINUTES, STATE, STATUSFILE, _read_json, game_id,
                    load_spots, log)
from delivery import (check_deliveries, record, send)
from game import (describe, enemy_picture, game_data, grab, inside, radar_text, relation_text, to_picture,
                  troop_spot, viewer_open_game, world_data)
from predict import update_tracks
from activity import track_orders
from history import history_save
# ---------------------------------------------------------------- main loop
def run():
    STATE.mkdir(exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))
    # keep the Mac and its display awake, otherwise Safari stops drawing the game
    subprocess.Popen(["caffeinate", "-dims", "-w", str(os.getpid())])
    log(f"monitor started (pid {os.getpid()}), checking every {INTERVAL}s")

    mem = {}                      # per-spot memory, keyed by spot id
    told_blind = set()            # spot ids we have texted "not watching" about
    clock = ["", time.time(), 0.0]  # game-clock picture, when it last changed, last reopen attempt
    had_data = None               # was the game's unit data readable on the last check
    last_beat = 0.0

    def stop(*_):
        log("monitor stopped")
        PIDFILE.unlink(missing_ok=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while True:
        now = time.time()
        try:
            spots = [s for s in load_spots() if s.get("enabled", True)]
            names = {s["id"]: s["name"] for s in spots}
            st, img = grab()
            if st == "ok":
                # the game clock (top left) ticks every minute; if it stops, the game has hung
                sig = hashlib.md5(img[84:124, 300:520].tobytes()).hexdigest()
                if sig != clock[0]:
                    clock[0], clock[1] = sig, now
                elif now - clock[1] > STALE_MINUTES * 60:
                    st, img = "stale", None
                    if now - clock[2] > 600:              # try reopening the game every 10 minutes
                        clock[2] = now
                        log("hidden game window: clock stopped, reopening the game")
                        viewer_open_game()
            data = game_data() if st == "ok" else None
            if st == "ok" and bool(data) != had_data:
                had_data = bool(data)
                log("reading units straight from the game" if data else "the game's unit data cannot be read")
            if data:
                update_tracks(data)
                try:
                    w = world_data()
                    if w:
                        history_save(w)
                except Exception as ex:
                    log(f"history save failed: {type(ex).__name__}: {ex}")
                try:
                    for ev in track_orders(data):
                        log(f"activity: {ev['kind']} order by owner {ev['o']} for army {ev['id']}" + (f" to {ev['dest']}" if ev.get("dest") else ""))
                except Exception as ex:
                    log(f"activity tracking failed: {type(ex).__name__}: {ex}")
            report = {}
            for spot in spots:
                if data and spot.get("army"):                 # follows a troop: foreign units within its sight
                    m = mem.get(spot["id"])
                    sig = ("army", spot["army"], spot.get("radius"))
                    if not m or m["sig"] != sig:
                        m = mem[spot["id"]] = {"sig": sig, "alert": False, "last_alert": 0.0, "clear": 0,
                                               "seen": set(), "blind_since": None, "state": None}
                    eff = troop_spot(spot, data)
                    if not eff:
                        if m["state"] != "gone":
                            log(f"[{spot['name']}] cannot watch: {BLIND_REASONS['gone']}")
                            m["state"] = "gone"
                        m["blind_since"] = m["blind_since"] or now
                        report[spot["id"]] = {"name": spot["name"], "state": "gone", "alert_active": m["alert"],
                                              "enemies": None, "mode": "data"}
                        continue
                    if m["state"] != "ok":
                        log(f"[{spot['name']}] watching (within {eff['circle'][2]:.0f} of the troop)")
                        m["state"] = "ok"
                    m["blind_since"] = None
                    n = watch_spot_data(eff, m, data, now)
                    report[spot["id"]] = {"name": spot["name"], "state": "ok", "alert_active": m["alert"],
                                          "enemies": n, "mode": "data"}
                    continue
                if data and spot.get("world"):
                    m = mem.get(spot["id"])
                    sig = ("world", tuple(spot["world"]))
                    if not m or m["sig"] != sig:
                        m = mem[spot["id"]] = {"sig": sig, "alert": False, "last_alert": 0.0, "clear": 0,
                                               "seen": set(), "blind_since": None, "state": None}
                    if m["state"] != "ok":
                        log(f"[{spot['name']}] watching")
                        m["state"] = "ok"
                    m["blind_since"] = None
                    n = watch_spot_data(spot, m, data, now)
                    report[spot["id"]] = {"name": spot["name"], "state": "ok", "alert_active": m["alert"],
                                          "enemies": n, "mode": "data"}
                    continue
                reason = st if st != "ok" else ("nodata" if not data else "gone")   # cannot watch it now
                m = mem.get(spot["id"])
                if not m or "seen" not in m:
                    m = mem[spot["id"]] = {"sig": None, "alert": False, "last_alert": 0.0, "clear": 0,
                                           "seen": set(), "blind_since": None, "state": None}
                if m["state"] != reason:
                    log(f"[{spot['name']}] cannot watch: {BLIND_REASONS.get(reason, reason)}")
                    m["state"] = reason
                m["blind_since"] = m["blind_since"] or now
                report[spot["id"]] = {"name": spot["name"], "state": reason, "alert_active": m["alert"],
                                      "enemies": None, "mode": "data"}

            # one text when spots have been unwatchable for a while, one when they are all back
            for gone in told_blind - set(names):
                told_blind.discard(gone)
            blind_long = {i for i in names if mem[i]["blind_since"]
                          and now - mem[i]["blind_since"] > BLIND_NOTIFY_MINUTES * 60}
            new = blind_long - told_blind
            if new:
                why = BLIND_REASONS.get(mem[next(iter(new))]["state"], "unknown reason")
                send(f"CON alert is NOT watching {', '.join(names[i] for i in sorted(new))}: {why}. "
                     "Open the CON alert page to fix or move the spot.")
                told_blind |= new
            back = {i for i in told_blind if not mem[i]["blind_since"]}
            if back:
                told_blind -= back
                if not told_blind:
                    send("CON alert is watching all its spots again.")

            check_deliveries()
            STATUSFILE.write_text(json.dumps({
                "time": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}", "capture": st, "spots": report}))
            if now - last_beat > 900:
                log("heartbeat: " + ("; ".join(
                    f"{r['name']}: {r['state']} enemies={r['enemies']}"
                    for r in report.values()) or "no enabled spots")
                    + (f" | {len(data['enemies'])} foreign groups visible on the whole map" if data else ""))
                last_beat = now
        except SystemExit:
            raise
        except Exception as ex:             # never let one bad cycle kill the watcher
            log(f"error: {type(ex).__name__}: {ex}")
        time.sleep(INTERVAL)


# Each foreign group is reported once: the first time it enters any of your spots or troop circles.
# After that it is remembered (state/alerted.json, kept per game, also across restarts) and not reported
# again, whether it stays, moves into another spot, or leaves and comes back. Only groups never reported
# before send a message.
ALERTED = STATE / "alerted.json"


def watch_spot_data(spot, m, data, now):
    """Data mode: text when a foreign army that has never been reported before is inside the spot (not yours,
    not a country you share intelligence with). Returns how many foreign armies are there."""
    here = [e for e in data["enemies"] if inside(e, spot, data["view"]["mapw"])
            and not (SKIP_AIRCRAFT and e["air"]) and (RADAR_ALERTS or not e.get("radar"))]
    if not here:
        m["clear"] += 1
        if m["clear"] >= CLEAR_CHECKS:
            if m["alert"]:
                log(f"[{spot['name']}] clear again")
            m["alert"], m["seen"] = False, set()
        return 0
    m["clear"] = 0
    m["alert"] = True
    alerted = _read_json(ALERTED, {})
    new = [e for e in here if str(e["id"]) not in alerted]
    if not new:
        return len(here)                                  # all already reported before: no message
    stamp = f"{dt.datetime.now():%Y%m%d-%H%M%S}"
    pic = enemy_picture(spot, new + [e for e in here if e not in new], ALERTS / f"{stamp}-{spot['id']}.jpg")
    known = len(here) - len(new)
    log(f"[{spot['name']}] new foreign units {'within sight of the troop' if spot.get('circle') else 'in spot'}: {describe(new)}")
    ok = send(f"CON ALERT: {spot['name']}: {describe(new)}"
              + (f" (also there, already reported: {known} group{'s' if known > 1 else ''})" if known else "")
              + f" (game {game_id()}, {dt.datetime.now():%-I:%M%p %a %-d %b}).", pic)
    record(spot["name"], [(e["nation"] or ("unknown" if e.get("radar") else ""), relation_text(e),
                           radar_text(e) if e.get("radar") else ", ".join(f"{n} {name}" for name, n in e["units"]),
                           "air" if e["air"] else "sea" if e["sea"] else "land",
                           round(e["x"]), round(e["y"]), e.get("to", ""), e.get("eta") or "",
                           e.get("sight", "")) for e in new],
           "new", ok, pic)
    for e in new:                                         # remembered: never reported again
        alerted[str(e["id"])] = {"t": round(now), "spot": spot["name"], "nation": e.get("nation", "")}
    tmp = ALERTED.with_suffix(".tmp")
    tmp.write_text(json.dumps(alerted))
    tmp.replace(ALERTED)
    m["seen"] |= {e["id"] for e in new}
    m["last_alert"] = now
    return len(here)


def cmd_check():
    """Look once now and print what is in each spot (sends nothing). Returns (image, [(spot, picture)])."""
    st, img = grab()
    if st != "ok":
        print("CANNOT WATCH:", BLIND_REASONS.get(st, st))
        return None, []
    data = game_data()
    if not data:
        print("CANNOT WATCH:", BLIND_REASONS["nodata"])
        return img, []
    print(f"game data: {data['armies']} armies visible, {len(data['enemies'])} not yours or an ally's "
          f"({sum(1 for x in data['enemies'] if x['war'])} at war, {sum(1 for x in data['enemies'] if not x['war'])} neutral)")
    watched = []
    for spot in load_spots():
        if spot.get("army"):
            name, spot = spot["name"], troop_spot(spot, data)
            if not spot:
                print(f"[{name}] CANNOT WATCH: {BLIND_REASONS['gone']}")
                continue
        if not spot.get("world"):
            continue
        watched.append(spot)
        here = [x for x in data["enemies"] if inside(x, spot, data["view"]["mapw"])]
        print(f"[{spot['name']}] " + (f"ENEMY: {describe(here)}" if here else "clear")
              + ("" if spot.get("enabled", True) else "  (disabled)"))
    # one picture of the current view with the spots that are in it outlined
    view, pix = data["view"], img.shape[1] / data["view"]["w"]
    pic = Image.fromarray(img)
    d = ImageDraw.Draw(pic)
    for spot in watched:                              # boxes as boxes, troop spots as their circle
        x, y = to_picture(spot["world"][0], spot["world"][1], view, pix)
        k = view["scale"] * pix
        box = [x, y, x + spot["world"][2] * k, y + spot["world"][3] * k]
        (d.ellipse if spot.get("circle") else d.rectangle)(box, outline=(255, 220, 0), width=4)
    path = STATE / "check-view.jpg"
    pic.resize((pic.width // 2, pic.height // 2)).save(path, quality=88)
    print("   picture:", path)
    return img, [(s_, path) for s_ in watched]


def cmd_test():
    _, seen = cmd_check()
    if not seen:
        if not load_spots():
            return "No test text was sent: you have no spots yet. Add one and press Save changes first."
        return "No test text was sent: none of your spots can be watched right now (see their status in the Spots list)."
    spot, pic = seen[0]
    ok = send(f"CON alert test: watching {len(seen)} spot(s), e.g. {spot['name']} (game {game_id()}). "
              "You will get a text like this, with a picture, when foreign units enter a spot.", pic)
    return "Test text sent." if ok else "Sending failed - see the log."
