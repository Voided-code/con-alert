#!/usr/bin/env python3
"""CON alert - watches spots on a Conflict of Nations map and texts you
when a red (enemy) ship shows up in one of them.

How it works, every INTERVAL seconds:
  1. Keeps the game running in its own invisible background window (the "viewer",
     viewer.swift), so no Safari tab is needed and nothing appears on screen.
  2. Takes a picture of that hidden game.
  3. Reads the game's own list of units from that hidden game: every army you can see, who owns
     it, whether you are at war with them, where it is on the map and what it is made of.
  4. Any army inside a spot is an alert unless it is yours or belongs to a country you share
     intelligence with; countries at war with you and neutral ones both count.
  5. Sends an iMessage saying what it is, with a picture.
  If the game's data cannot be read (for example after a game update), it falls back to the older
  way: looking for red enemy markers in pictures and asking Claude what they are.

Usage (through the ./conalert wrapper):
  ./conalert ui         open the web page: a map of the whole game world drawn from the game's
                        data, on which you draw, move and resize the spots
  ./conalert ui-stop    stop the web page server
  ./conalert viewer show|hide|restart|stop|status
                        the hidden game window: show it (to log in), hide it again, ...
  ./conalert start      start watching in the background
  ./conalert stop       stop watching
  ./conalert status     is it running, and what did it last see
  ./conalert check      look once right now and print the result (sends nothing)
  ./conalert test       send a test text + picture of a spot
  ./conalert alerts     show the record of alerts sent (kept in alerts.csv)
"""

import csv
import datetime as dt
import fcntl
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# ----------------------------------------------------------------- settings
# Your own details live in config.json next to this file (it is not shared; see config.example.json):
#   {"phone": "+61400000000", "country_code": "+61", "game_id": "12345678", "user_id": "123456789"}
_CONFIG = json.loads((Path(__file__).resolve().parent / "config.json").read_text()) \
    if (Path(__file__).resolve().parent / "config.json").exists() else {}
PHONE = _CONFIG.get("phone", "")                # main number for alerts (iMessage)
COUNTRY_CODE = _CONFIG.get("country_code", "+61")  # put in front of numbers typed the local way (04... becomes +614...)
GAME_ID = str(_CONFIG.get("game_id", ""))       # the game to watch (the gameID in the game's address)
USER_ID = str(_CONFIG.get("user_id", ""))       # your Conflict of Nations user id (the uid in the game's address)
GAME_URL = f"https://www.conflictnations.com/play.php?bust=1&uid={USER_ID}&gameID={GAME_ID}&source=browser-desktop"
INTERVAL = 20                   # seconds between checks
MIN_RED_PIXELS = 250            # red pixels inside a 64x64 patch that count as a unit marker (a whole marker is ~600)
REALERT_MINUTES = 30            # remind again if the ship is still there after this long
CLEAR_CHECKS = 6                # clear checks in a row before a new sighting counts as new
BLIND_NOTIFY_MINUTES = 10       # text once if a spot cannot be seen for this long
MATCH_MIN = 0.45                # how well the view must match a spot's reference (0..1); see below for the viewer
CLASSIFY_MODEL = "sonnet"       # model used to read what the red unit is
RADAR_ALERTS = True             # also alert on radar-only contacts (something your radar sees but cannot identify)
SKIP_AIRCRAFT = False           # True: do not text when the only enemies in a spot are aircraft
RECLASSIFY_MINUTES = 5          # re-ask while a red non-ship stays in a spot
UI_PORT = 8765                  # web page: http://127.0.0.1:8765
TAILSCALE = True                # also serve the page to your other devices on your Tailscale network
SOURCE = "viewer"               # "viewer": hidden background game window. "safari": a visible Safari tab
DETECT = "data"                 # "data": read the game's own list of units (exact). "picture": look at pictures
STALE_MINUTES = 6               # the hidden game's clock must change at least this often
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
REFDIR = ROOT / "reference"
SPOTS_FILE = ROOT / "spots.json"
STATE = ROOT / "state"
ALERTS = Path.home() / "Pictures" / "CON alert"   # Messages can only attach files from here
LOG = ROOT / "logs" / "monitor.log"
RECORD = ROOT / "alerts.csv"     # one row per foreign army in every alert sent (opens in Excel / Numbers)
RECORD_COLUMNS = ["time", "spot", "country", "relation", "units", "where", "map_x", "map_y",
                  "heading_for", "arrives_in_min", "sight", "kind", "text_sent", "picture"]
PIDFILE = STATE / "monitor.pid"
UI_PIDFILE = STATE / "ui.pid"
STATUSFILE = STATE / "status.json"
SCALE = 4                       # matching is done on a 1/4-size edge map
MAP_TOP = 88 if SOURCE == "viewer" else 192   # picture rows above this are toolbars, not map
VIEWER_BIN = ROOT / "conviewer"
VIEWER_PID = STATE / "viewer.pid"
CLIENT_URL_FILE = STATE / "client_url.txt"
MIN_PATCH = (924, 876)          # smallest reference picture kept around a spot (w, h)
if SOURCE == "viewer":
    # The hidden game animates its water, which blurs small reference pictures. Its map only moves
    # when told to, so use nearly the whole view as the reference: a right view scores 0.3-0.9,
    # a wrong one under 0.2.
    MIN_PATCH = (2624, 1416)
    MATCH_MIN = 0.30
MIN_ZONE = 80                   # smallest spot box side, in screenshot pixels
# test hooks: CON_ALERT_DRYRUN=1 logs instead of texting, CON_ALERT_IMAGE=file replaces the screenshot
DRY_RUN = bool(os.environ.get("CON_ALERT_DRYRUN"))
TEST_IMAGE = os.environ.get("CON_ALERT_IMAGE")
INTERVAL = int(os.environ.get("CON_ALERT_INTERVAL", INTERVAL))

FIND_WINDOW = """
if application "Safari" is not running then return "nosafari"
tell application "Safari"
  repeat with w in windows
    try
      set u to URL of current tab of w
      if u contains "conflictnations.com" and u contains "gameID=%s" then
        if miniaturized of w then return "minimized"
        return "ok " & (id of w)
      end if
    end try
  end repeat
  repeat with w in windows
    repeat with t in tabs of w
      try
        if (URL of t) contains "gameID=%s" then return "hidden"
      end try
    end repeat
  end repeat
  return "notab"
end tell
""" % (GAME_ID, GAME_ID)

# Puts the game where the monitor can see it: un-minimises it, brings its tab to the front of its
# window (moving the CON alert page to a window of its own if that is what covers it), or opens it.
FIX_WINDOW = """
tell application "Safari"
  repeat with w in windows
    set gi to 0
    repeat with i from 1 to (count of tabs of w)
      try
        if (URL of tab i of w) contains "gameID=%(gid)s" then set gi to i
      end try
    end repeat
    if gi > 0 then
      if miniaturized of w then set miniaturized of w to false
      set ci to index of current tab of w
      if ci is not gi then
        set cu to ""
        try
          set cu to URL of current tab of w
        end try
        set current tab of w to tab gi of w
        if cu contains ":%(port)s" then
          close tab ci of w
          make new document with properties {URL:"http://127.0.0.1:%(port)s/"}
        end if
      end if
      return "The game tab is visible again."
    end if
  end repeat
  make new document with properties {URL:"%(url)s"}
  return "Opened the game in its own window."
end tell
""" % {"gid": GAME_ID, "port": UI_PORT, "url": GAME_URL}

# The file reference has to be made OUTSIDE the "tell Messages" block: made inside it, Messages
# sends an empty bubble instead of the picture.
SEND = """
on run argv
  -- number, text, "iMessage" or "SMS", and optionally a picture
  set pic to missing value
  if (count of argv) > 3 then set pic to POSIX file (item 4 of argv)
  tell application "Messages"
    if (item 3 of argv) is "SMS" then
      set svc to 1st account whose service type = SMS
    else
      set svc to 1st account whose service type = iMessage
    end if
    set p to participant (item 1 of argv) of svc
    if (item 2 of argv) is not "" then send (item 2 of argv) to p
    if pic is not missing value then send pic to p
  end tell
end run
"""

BLIND_REASONS = {
    "nosafari": "Safari is not running",
    "notab": "the game tab is not open in Safari",
    "hidden": "the game tab is behind another tab in its Safari window",
    "minimized": "the Safari game window is minimised",
    "nocapture": "the screen could not be captured (Mac locked or display asleep?)",
    "moved": "the map is panned or zoomed away from the spot",
    "noviewer": "the background game window could not be started",
    "login": "the background game is not logged in",
    "stale": "the background game has stopped updating",
    "covered": "a game pop-up is covering the map and will not close",
    "loading": "the background game is loading",
    "nodata": "the game's unit data cannot be read",
    "gone": "the troop it follows is no longer there (destroyed, merged into another group, or out of sight)",
}


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    with LOG.open("a") as f:
        f.write(line + "\n")
    if sys.stdout.isatty():
        print(line)


# -------------------------------------------------------------------- spots
# A spot is {"id", "name", "enabled", "ref": picture of the map around the spot,
#            "zone": [x, y, w, h] of the watched box inside that picture}.
def load_spots():
    try:
        return json.loads(SPOTS_FILE.read_text())["spots"]
    except FileNotFoundError:
        spots = []
        if (REFDIR / "baseline_frame.png").exists():      # the original single spot
            spots = [{"id": "colombo", "name": "South of Colombo", "enabled": True,
                      "ref": "reference/baseline_frame.png", "zone": [222, 198, 480, 480]}]
        save_spots(spots)
        return spots


def save_spots(spots):
    tmp = SPOTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"spots": spots}, indent=2))
    tmp.replace(SPOTS_FILE)                               # atomic: the monitor reads this file


_ref_cache = {}


def ref_edges(spot):
    """Edge map of a spot's reference picture, plus the picture's (w, h)."""
    path = ROOT / spot["ref"]
    if spot["ref"] not in _ref_cache:
        ref = np.asarray(Image.open(path).convert("RGB"))
        _ref_cache[spot["ref"]] = (edge_map(ref).astype(np.float64), (ref.shape[1], ref.shape[0]))
    return _ref_cache[spot["ref"]]


def clamp_rect(rect, w, h):
    """Keep a box inside the map part of a screenshot and at least MIN_ZONE big."""
    x, y, rw, rh = [int(round(v)) for v in rect]
    rw, rh = max(MIN_ZONE, min(rw, w)), max(MIN_ZONE, min(rh, h - MAP_TOP))
    return [max(0, min(x, w - rw)), max(MAP_TOP, min(y, h - rh)), rw, rh]


def make_spot(img, rect, name, enabled=True, spot_id=None):
    """Create a spot from a box drawn on a screenshot: saves the reference picture."""
    h, w = img.shape[:2]
    x, y, rw, rh = clamp_rect(rect, w, h)
    pw = min(w, max(MIN_PATCH[0], rw + 200))
    ph = min(h - MAP_TOP, max(MIN_PATCH[1], rh + 200))
    px = max(0, min(w - pw, x + rw // 2 - pw // 2))
    py = max(MAP_TOP, min(h - ph, y + rh // 2 - ph // 2))
    px -= px % SCALE                                      # line up with the 1/4-size matching grid
    py -= py % SCALE
    spot_id = spot_id or uuid.uuid4().hex[:8]
    REFDIR.mkdir(exist_ok=True)
    ref = f"reference/{spot_id}-{int(time.time())}.png"
    Image.fromarray(img[py:py + ph, px:px + pw]).save(ROOT / ref)
    return {"id": spot_id, "name": name, "enabled": enabled, "ref": ref,
            "zone": [x - px, y - py, rw, rh]}


# ----------------------------------------------------- hidden game window
# Finds the game client address (with its login tokens) in the Safari tab, if one is open.
SAFARI_SOURCE = """
if application "Safari" is not running then return ""
tell application "Safari"
  repeat with w in windows
    repeat with t in tabs of w
      try
        if (URL of t) contains "gameID=%s" then return source of t
      end try
    end repeat
  end repeat
end tell
return ""
""" % GAME_ID

HAS_MAP = "document.getElementById('map_canvas') ? 'yes' : 'no'"
# Game pop-ups (news, offers, tips) cover the map and block it from moving: click their close buttons.
# Returns "<closed> <still open>".
POPUP_JS = """(function(){
  const sel = '.popup_widget__close, .close_window_button, .close_button, .button_close_wrapper, ' +
              '.sharedbuttons-button_tinyclose, .info_message_content__close_button';
  let n = 0;
  for (const b of document.querySelectorAll(sel)) {
    const r = b.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || b.closest('.intel_drawer, #leftSideButtons, #RightSideButtons')) continue;
    for (const t of ['mousedown', 'mouseup', 'click'])
      b.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, view:window, button:0}));
    n++;
  }
  const box = document.getElementById('popupsContainer');
  const top = document.elementFromPoint(innerWidth/2, innerHeight/2);
  const covered = (box && box.children.length > 0) || !top || top.id !== 'map_canvas';
  return n + ' ' + (covered ? 1 : 0);
})()"""
FRAME_JS = "(function(){ if (!document.getElementById('map_canvas')) return 'nomap'; return 'map ' + %s; })()" % POPUP_JS
VP = "hup.ui.mapWidget._viewport"    # the game's own map camera: rect = visible part of the map, scale = zoom
VIEW_GET_JS = ("(function(){ const v = %s; return JSON.stringify({x: v.rect.x, y: v.rect.y, scale: v.scale, "
               "w: v.screenWidth, h: v.screenHeight, mapw: v.totalWidth}); })()" % VP)
# Everything the monitor needs, in one read of the game's own state.
# The game's diplomacy levels: -2 war, -1 ceasefire, 0 embargo, 1 peace, 3 right of way,
# 4 shared map, 6 shared intelligence, 7 army command.
# Left out: your own armies, and those of countries you share intelligence with (in either
# direction) or that are in your coalition. Everyone else's is reported, at war or neutral.
GAME_JS = """(function(){
  try {
    const gs = hup.gameState, v = %s;
    const ps = gs.getPlayerState(), fa = gs.getForeignAffairsState();
    const me = ps.getPlayerByUserID(hup.user.id).playerID;
    const armies = gs.getArmyState().getArmiesArray();
    const enemies = [], friends = [];
    // where an army is going: its waypoints, the name of the last one, and minutes until it arrives
    const pathOf = (a, p) => { const pts = []; try { for (const c of a.commands || []) { const t = c && c.targetPos;
        if (t && isFinite(t.x) && isFinite(t.y)) { const x = Math.round(t.x), y = Math.round(t.y), n = pts.length;
          if (n ? (pts[n - 2] !== x || pts[n - 1] !== y) : Math.hypot(x - p.x, y - p.y) > 3) pts.push(x, y); } } } catch (e) {} return pts; };
    const etaOf = a => { try { const t = a.getEstimatedArrivalTime(); return t > Date.now() ? Math.round((t - Date.now()) / 60000) : null; } catch (e) { return null; } };
    const destOf = (a, pts) => { if (!pts.length) return ''; try { return String(gs.getMapState().getLocationNameByPoint({x: pts[pts.length - 2], y: pts[pts.length - 1]}) || '').trim(); } catch (e) { return ''; } };
    const sightOf = (a, air) => { try { return Math.round(air ? Math.max(a.getAirViewWidth() || 0, a.viewWidth || 0) : a.viewWidth || 0); } catch (e) { return Math.round(a.viewWidth || 0); } };
    // a radar-only contact: the game knows something is there (from your radar units) but not what or whose.
    // It has no units, only signatures: [[kind, size]]. Size 10 is a small signature, 30 a large one.
    const RADAR_KIND = {1: 'land units', 2: 'aircraft', 3: 'ships', 4: 'submarine', 5: 'helicopters'};
    const radarOf = a => { try { if (!a.isRadarContact()) return null; const m = a.getRadarSignatureFeature().signatureSizeMap || {};
        return Object.keys(m).map(k => [RADAR_KIND[k] || 'unknown kind', Math.round(m[k] || 0)]); } catch (e) { return null; } };
    for (const a of armies) {
      if (!a || a.removed) continue;
      const rc = radarOf(a);
      const known = !rc || (a.ownerID > 0 && (() => { try { return !!ps.getPlayer(a.ownerID); } catch (e) { return false; } })());
      const own = !rc && a.ownerID === me;
      const mine = own ? 9 : known ? fa.relations.getRelation(me, a.ownerID) : 0, theirs = own ? 9 : known ? fa.relations.getRelation(a.ownerID, me) : 0;
      const friend = !rc && (own || mine >= 6 || theirs >= 6 || fa.getPlayersRelation(me, a.ownerID) === 3);
      const war = mine <= -2 || theirs <= -2;
      let air = !!a.airplane;             // also troops being flown: they are where the flight is
      try { air = air || !!a.isFlying(); } catch (e) {}
      if (rc) air = rc.some(s => s[0] === 'aircraft' || s[0] === 'helicopters');
      let p = null;
      try { p = air ? a.getAirPosition() : a.getPosition(); } catch (e) {}
      if (!p || !isFinite(p.x) || !isFinite(p.y)) p = a.position;
      if (friend) {
        let ord = [], at = '', units = [], to = '';
        try { ord = (a.commands || []).filter(Boolean).map(c => c.targetPos && isFinite(c.targetPos.x)
                ? [String(c['@c'] || ''), Math.round(c.targetPos.x), Math.round(c.targetPos.y)] : [String(c['@c'] || '')]); } catch (e) {}
        try { at = String(a.getLocation().name || '').trim(); } catch (e) {}
        try { units = a.units.map(u => [u.getUnitType().getUnitName(), u.size]); } catch (e) {}
        try { to = destOf(a, pathOf(a, p)); } catch (e) {}
        friends.push({id: a.unitUID, o: a.ownerID, x: p.x, y: p.y, sight: sightOf(a, air), own, ord, at, units, to, fight: (() => { try { return !!(a.battle || a.isFighting() || a.fightStatus || (a.lastDamageTakenTime && Date.now() - a.lastDamageTakenTime < 600000)); } catch (e) { return false; } })()});
        continue;
      }
      let nation = '', units = [];
      if (known) try { nation = ps.getPlayer(a.ownerID).defaultNationName; } catch (e) {}
      try { units = a.units.map(u => [u.getUnitType().getUnitName(), u.size]); } catch (e) {}
      const pts = pathOf(a, p);
      // its planned timeline: [x, y, time] points from now to arrival. The first leg's timing comes from the
      // game; the rest is spread over the remaining legs by distance up to the overall arrival time.
      // its timeline [x, y, time]: the game gives every leg of a route its own speed (already set for that leg's
      // terrain, roads and whose land it is), so each leg takes length / speed. Waits (boarding a ship, landing)
      // take whatever is left up to the game's own arrival time.
      const knots = [[p.x, p.y, Date.now()]], W = v.totalWidth || 15393;
      const unwrap = (x, px) => x + Math.round((px - x) / W) * W;
      try {
        const eta = a.getEstimatedArrivalTime(), raw = [];
        let T = Date.now(), px = p.x, py = p.y, first = true;
        for (const c of a.commands || []) {
          if (!c) continue;
          if (c['@c'] === 'wc') { raw.push('wait'); continue; }
          if (!c.targetPos || !isFinite(c.targetPos.x)) continue;
          const x = unwrap(c.targetPos.x, px), y = c.targetPos.y;
          if (first && c.arrivaltime > Date.now()) T = c.arrivaltime;
          else if (c.speed > 0) T += Math.hypot(x - px, y - py) / c.speed * 60000;
          else continue;
          first = false;
          raw.push([x, y, T]); px = x; py = y;
        }
        const waits = raw.filter(r => r === 'wait').length, moves = raw.filter(r => r !== 'wait');
        if (moves.length) {
          const end = moves[moves.length - 1][2], t1 = moves[0][2];
          let pause = 0, scale = 1;
          if (eta > 0 && waits && eta > end) pause = (eta - end) / waits;
          else if (eta > t1 && Math.abs(eta - end) > 60000 && end > t1) scale = (eta - t1) / (end - t1);   // trust the game's arrival time
          let shift = 0;
          for (const r of raw) {
            if (r === 'wait') { shift += pause; if (knots.length > 1 || pause) knots.push([knots[knots.length - 1][0], knots[knots.length - 1][1], knots[knots.length - 1][2] + pause]); continue; }
            const t = r[2] === t1 ? t1 : t1 + (r[2] - t1) * scale;
            knots.push([r[0], r[1], t + shift]);
          }
        }
      } catch (e) {}
      // for a group standing still: the game's speed table for its slowest unit, to work out where it could go
      let move = null;
      if (!a.airplane && !rc) try {
        const tbl = {}, types = a.units.map(u => u.getUnitType()), keys = new Set();
        const sea = !!a.onSea || !!a.ship || types.every(ut => { try { return ut.isShip(); } catch (e) { return false; } });
        for (const ut of types) for (const k in (ut.speeds || {})) keys.add(k);
        for (const ut of types) {               // the slowest unit sets the pace; a terrain it does not list uses its default
          const s = ut.speeds || {}, d = s[sea ? '2' : '0'] ?? s['0'] ?? 0;
          for (const k of keys) { const v = k in s ? s[k] : d; tbl[k] = k in tbl ? Math.min(tbl[k], v) : v; }
        }
        const here = gs.getMapState().getLocation(a.getLocationID());
        move = Object.values(tbl).some(v => v > 0) ? {tbl, ff: Math.min(...types.map(ut => ut.foreignSpeedFactor || 1)), tt: here ? here.terrainType : null,
                own: !!here && here.ownerID === a.ownerID, cls: sea ? 'sea' : 'land'} : {fixed: true};
      } catch (e) {}
      let spd = 0;
      try { spd = air ? a.getAirSpeed() : a.getSpeedOnCurrentTerrain(); } catch (e) {}
      // planes and helicopters belong to an airbase: they fly from it and must land there again
      let plane = null;
      if (a.airplane) try {
        const f = a.airParams && a.airParams.airField, lt = a.airParams && a.airParams.launchTarget;
        if (f && f.capital) plane = {base: [Math.round(f.capital.x), Math.round(f.capital.y)], baseName: String(f.name || '').trim(),
          flying: !!a.isFlying(), st: String(a.getStatus() || ''), fuel: Math.round(a.getMaxFlightTime() || 0),
          patrol: Math.round(a.getPatrolRadius() || 0), target: lt && isFinite(lt.x) ? [Math.round(lt.x), Math.round(lt.y)] : null,
          reach: Math.round(a.range || 0), refuel: Math.round(a.getRefuelTime() || 0),   // seconds it needs after landing
          // the time it must turn for home (fuel), as the game has it
          mustReturn: (() => { const t = a.airParams && a.airParams.maxFlightTime; return t > Date.now() ? Math.round(t) : null; })(),
          refuelEnd: (() => { try { if (!a.isRefueling()) return null; const w = a.getNextCommand(); return Date.now() + w.remainingTime(Date.now()); } catch (e) { return null; } })()};
      } catch (e) {}
      // its orders as a list, to notice when its player gives new ones: [kind, x, y] per order
      let ord = [];
      try { ord = (a.commands || []).filter(Boolean).map(c => c.targetPos && isFinite(c.targetPos.x)
              ? [String(c['@c'] || ''), Math.round(c.targetPos.x), Math.round(c.targetPos.y)] : [String(c['@c'] || '')]); } catch (e) {}
      let at = '';
      try { at = String(a.getLocation().name || '').trim(); } catch (e) {}
      enemies.push({id: a.unitUID, o: a.ownerID, nation, war, x: p.x, y: p.y, plane, move, ord, at, fight: (() => { try { return !!(a.battle || a.isFighting() || a.fightStatus || (a.lastDamageTakenTime && Date.now() - a.lastDamageTakenTime < 600000)); } catch (e) { return false; } })(), knots: knots.length > 1 ? knots.map(k => [Math.round(k[0]), Math.round(k[1]), Math.round(k[2])]) : [],
                    spd: Math.round((spd || 0) * 100) / 100, sea: !!a.onSea || (!!rc && rc.some(s => s[0] === 'ships' || s[0] === 'submarine')),
                    ship: !!a.ship, air, units, radar: rc,
                    to: destOf(a, pts), eta: pts.length ? etaOf(a) : null, sight: sightOf(a, air),
                    range: a.airplane ? (() => { try { return Math.round(a.getPatrolRadius() || 0); } catch (e) { return 0; } })() : Math.round(a.range || 0)});
    }
    return JSON.stringify({ok: true, me, armies: armies.length, enemies, friends,
      view: {x: v.rect.x, y: v.rect.y, scale: v.scale, w: v.screenWidth, h: v.screenHeight, mapw: v.totalWidth}});
  } catch (e) { return JSON.stringify({ok: false, error: String(e)}); }
})()""" % VP


def viewer_pid():
    try:
        pid = int(VIEWER_PID.read_text())
        os.kill(pid, 0)
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
        return pid if "conviewer" in cmd else None
    except Exception:
        return None


def viewer_call(cmd, timeout=15, **kw):
    """Send one command to the hidden game window and wait for its answer (None if it does not answer)."""
    i = uuid.uuid4().hex[:10]
    STATE.mkdir(exist_ok=True)
    tmp = STATE / f".viewer-{i}.tmp"
    tmp.write_text(json.dumps({"cmd": cmd, **kw}))
    tmp.replace(STATE / f"viewer-req-{i}.json")
    res = STATE / f"viewer-res-{i}.json"
    end = time.time() + timeout
    while time.time() < end:
        if res.exists():
            try:
                out = json.loads(res.read_text())
                res.unlink(missing_ok=True)
                return out
            except Exception:
                pass
        time.sleep(0.004)
    (STATE / f"viewer-req-{i}.json").unlink(missing_ok=True)
    return None


def viewer_js(code):
    r = viewer_call("js", code=code)
    return r.get("result") if r else None


def viewer_wait(test, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if test():
            return True
        time.sleep(0.5)
    return False


def safari_client_url():
    """The game client address from an open Safari game tab ("" if there is none). Saved for later."""
    try:
        src = subprocess.run(["osascript", "-e", SAFARI_SOURCE], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""
    m = re.search(r'<iframe[^>]*src="([^"]*con-client[^"]*)"', src)
    if not m:
        return ""
    url = html.unescape(m.group(1))
    CLIENT_URL_FILE.write_text(url)
    CLIENT_URL_FILE.chmod(0o600)              # it contains login tokens
    return url


# The game always opens on the home country and forgets where the map was, so the view is
# written down whenever it is moved from the web page, and set again after each reopen.
def view_get():
    """The hidden game's map view: {x, y, scale, w, h, mapw}, or None if the game is not ready."""
    try:
        return json.loads(viewer_js(VIEW_GET_JS))
    except Exception:
        return None


def view_set(x, y, scale=None):
    """Put the top-left of the view at map position (x, y), optionally changing the zoom first."""
    zoom = f"v.setZoom({float(scale)!r}); " if scale else ""
    viewer_js(f"(function(){{ const v = {VP}; {zoom}v.rect.x = {float(x)!r}; v.rect.y = {float(y)!r}; return 'ok'; }})()")


def _small_gray(path):
    im = Image.open(path).convert("L")
    a = np.asarray(im.resize((im.width // 8, im.height // 8)), dtype=np.float32)
    return a[MAP_TOP // 8 + 12:]                  # below the top bars and the clock


def viewer_settle(seconds=40):
    """Wait until the hidden game's picture stops changing (loading finished, zoom animation over)."""
    path = STATE / f"settle-{uuid.uuid4().hex[:8]}.tiff"
    prev, end = None, time.time() + seconds
    try:
        while time.time() < end:
            if not (viewer_call("snapshot", path=str(path)) or {}).get("ok"):
                return False
            cur = _small_gray(path)
            if prev is not None and prev.shape == cur.shape and cur.std() > 3 \
                    and np.abs(cur - prev).mean() < 0.6:
                return True
            prev = cur
            time.sleep(1.5)
        return False
    finally:
        path.unlink(missing_ok=True)


def close_popups():
    """Dismiss game pop-ups in the hidden window. Returns True if the map is left uncovered."""
    info = viewer_call("info")
    if not info or info["shown"]:                 # someone is using the window: leave their dialogs alone
        return True
    for _ in range(3):
        closed, covered = (viewer_js(POPUP_JS) or "0 0").split()
        if covered == "0":
            return True
        if closed == "0":
            return False
        time.sleep(1.2)
    return False


def viewer_busy():
    """True while some process is opening the game or putting the map back."""
    with open(STATE / "viewer-open.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError:
            return True


def viewer_open_game():
    """Point the hidden window at the game and put the map back. Returns "ok" or "login"."""
    with open(STATE / "viewer-open.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        return _open_game()


def _open_game():
    def loaded():
        r = viewer_call("info")
        return bool(r) and not r["loading"]

    def try_client(url):
        viewer_call("load", url=url)
        return viewer_wait(lambda: viewer_js(HAS_MAP) == "yes", 60)

    # 1. the viewer's own login, if someone has logged in inside it
    viewer_call("load", url=GAME_URL)
    time.sleep(1)
    viewer_wait(loaded, 30)
    if "play.php" in ((viewer_call("info") or {}).get("url") or ""):
        src = viewer_js("(document.querySelector('iframe[src*=\"con-client\"]') || {}).src || ''")
        if src and try_client(src):
            log("hidden game window: opened the game with its own login")
            return "ok"
    # 2. borrow the session of a Safari game tab (fresh if it is open, else the last one saved)
    for url in (safari_client_url(), CLIENT_URL_FILE.read_text() if CLIENT_URL_FILE.exists() else ""):
        if url and try_client(url):
            log("hidden game window: opened the game with the session from Safari")
            return "ok"
    log("hidden game window: not logged in")
    return "login"


def viewer_start():
    """Start the hidden game window if it is not running. Returns "ok", "login" or "noviewer"."""
    STATE.mkdir(exist_ok=True)
    with open(STATE / "viewer.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)          # the monitor and the web page may both try at once
        if viewer_pid():
            return "ok"
        src = ROOT / "viewer.swift"
        if not VIEWER_BIN.exists() or VIEWER_BIN.stat().st_mtime < src.stat().st_mtime:
            r = subprocess.run(["swiftc", "-O", str(src), "-o", str(VIEWER_BIN)], capture_output=True, text=True)
            if r.returncode != 0:
                log(f"could not build the viewer: {r.stderr[-300:]}")
                return "noviewer"
        for old in STATE.glob("viewer-re[qs]-*.json"):
            old.unlink(missing_ok=True)
        LOG.parent.mkdir(exist_ok=True)
        out = open(LOG.parent / "viewer.out", "a")
        p = subprocess.Popen([str(VIEWER_BIN), str(STATE), "about:blank"], stdout=out, stderr=out,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        VIEWER_PID.write_text(str(p.pid))
        if not viewer_wait(lambda: viewer_call("info", timeout=2), 15):
            return "noviewer"
        log(f"hidden game window started (pid {p.pid})")
        return viewer_open_game()


def viewer_stop():
    pid = viewer_pid()
    if not pid:
        return "hidden game window is not running"
    os.kill(pid, signal.SIGTERM)
    VIEWER_PID.unlink(missing_ok=True)
    return f"hidden game window stopped (pid {pid})"


_last_reopen = 0.0


_shown = False                    # is the hidden window currently on screen (someone using it)?


def grab_viewer(attempt=0):
    """Picture of the hidden game. Returns ("ok", image) or (reason, None)."""
    global _last_reopen, _shown
    if not viewer_pid():
        st = viewer_start()
        if st != "ok":
            return st, None
    if viewer_busy():
        return "loading", None
    path = STATE / f"snap-{uuid.uuid4().hex[:8]}.tiff"
    # pop-ups are left alone while the window is on screen: someone is using it
    r = viewer_call("snapshot", path=str(path), js=HAS_MAP if _shown else FRAME_JS)
    try:
        if not r:                                 # hung: restart it on the next check
            viewer_stop()
            return "noviewer", None
        _shown = bool(r.get("shown"))
        res = (r.get("result") or "").split()
        if not res or res[0] in ("nomap", "no"):
            if attempt == 0 and time.time() - _last_reopen > 300:
                _last_reopen = time.time()
                if viewer_open_game() == "ok":
                    return grab_viewer(1)
            return "login", None
        if len(res) == 3 and res[2] == "1":       # something covers the map
            if res[1] != "0" and attempt < 3:     # close buttons were clicked: look again
                time.sleep(1.0)
                return grab_viewer(attempt + 1)
            return "covered", None
        if not r.get("ok"):
            return "nocapture", None
        img = np.asarray(Image.open(path).convert("RGB"))
    except Exception:
        return "nocapture", None
    finally:
        path.unlink(missing_ok=True)
    return "ok", img


# ------------------------------------------------------------- game data
# The shape of the whole map: every land province (outline, name, main town) and the map size.
# It never changes during a game, so it is read once and kept in state/geo-<map>.json.
GEO_JS = """(function(){
  try {
    const m = hup.gameState.getMapState().map;
    const land = [];
    for (const l of m.locations) {
      if (!l || !l.borders || !('ownerID' in l)) continue;          // sea areas and rivers have no owner
      land.push({id: l.locationID, n: l.name, c: [Math.round(l.capital.x), Math.round(l.capital.y)],
                 city: (l.provinceLevel || 0) >= 5, b: l.borders.map(a => Array.from(a))});
    }
    return JSON.stringify({ok: true, id: m.mapID, w: m.width, h: m.height, land});
  } catch (e) { return JSON.stringify({ok: false, error: String(e)}); }
})()"""
# What changes: who owns each province (same order as GEO_JS), every army in sight, and how each
# country stands to you: me, ally (shared intelligence or coalition), war, or neutral.
WORLD_JS = """(function(){
  try {
    const gs = hup.gameState, ps = gs.getPlayerState(), fa = gs.getForeignAffairsState(), as = gs.getArmyState();
    const me = ps.getPlayerByUserID(hup.user.id).playerID, now = Date.now();
    const T = f => { try { return f(); } catch (e) { return null; } };
    const num = (v, d) => { v = Number(v); d = d || 1; return (v === v && isFinite(v)) ? Math.round(v * d) / d : null; };
    const relOf = id => { if (id === me) return 'me'; if (!(id > 0)) return 'none';
      const a = fa.relations.getRelation(me, id), b = fa.relations.getRelation(id, me);
      if (a >= 6 || b >= 6 || fa.getPlayersRelation(me, id) === 3) return 'ally';
      return (a <= -2 || b <= -2) ? 'war' : 'neutral'; };
    // per country: nation, relation, player name, victory points, computer player?, capital province, coalition, flag picture
    const players = {};
    const addPlayer = id => { if (id in players) return; const p = T(() => ps.getPlayer(id));
      players[id] = [p ? p.defaultNationName || '' : '', relOf(id), p ? T(() => p.getName()) || '' : '', p ? p.vps || 0 : 0,
                     p ? !!(p.computerPlayer || p.nativeComputer) : false, p ? p.capitalID || 0 : 0,
                     p ? T(() => { const t = p.getTeam(); return t ? String(t.name || t.getName()) : ''; }) || '' : '',
                     p ? String(T(() => p.getFlagImageURL()) || '') : '']; };
    const owners = [], battles = [];
    for (const l of gs.getMapState().map.locations) {
      if (!l || !l.borders || !('ownerID' in l)) continue;
      if (l.lastBattle > 1e12 && now - l.lastBattle < 3600e3) battles.push([owners.length, Math.round((now - l.lastBattle) / 60000)]);
      owners.push(l.ownerID);
      addPlayer(l.ownerID);
    }
    // where an army is going: its waypoints (orders that point at the spot it already stands on are not a
    // route), the name of the last one, and minutes until it arrives
    // "how" says how each leg is travelled: 0 by land, 1 by sea, 2 by air
    const pathOf = (a, p, how) => { const pts = []; try { for (const c of a.commands || []) { const t = c && c.targetPos;
        if (t && isFinite(t.x) && isFinite(t.y)) { const x = Math.round(t.x), y = Math.round(t.y), n = pts.length;
          if (n ? (pts[n - 2] !== x || pts[n - 1] !== y) : Math.hypot(x - p.x, y - p.y) > 3) { pts.push(x, y);
            how.push(a.airplane || c.inAir || T(() => c.isFlyingCommand()) ? 2 : c.isOnWater ? 1 : 0); } } } } catch (e) {} return pts; };
    const etaOf = a => { try { const t = a.getEstimatedArrivalTime(); return t > now ? Math.round((t - now) / 60000) : null; } catch (e) { return null; } };
    const destOf = (a, pts) => { if (!pts.length) return ''; try { return String(gs.getMapState().getLocationNameByPoint({x: pts[pts.length - 2], y: pts[pts.length - 1]}) || '').trim(); } catch (e) { return ''; } };
    const sightOf = (a, air) => { try { return Math.round(air ? Math.max(a.getAirViewWidth() || 0, a.viewWidth || 0) : a.viewWidth || 0); } catch (e) { return Math.round(a.viewWidth || 0); } };
    // aircraft, and troops being flown somewhere, are where the flight is, not at the airfield they left
    const flies = a => !!a.airplane || !!T(() => a.isFlying());
    const posOf = a => { let p = null; try { p = flies(a) ? a.getAirPosition() : a.getPosition(); } catch (e) {}
      if (!p || !isFinite(p.x) || !isFinite(p.y)) p = a.position; return p; };
    // which simple symbol to draw for a unit type
    // a radar-only contact: the game knows something is there (from your radar units) but not what or whose.
    // It has no units, only signatures: [[kind, size]]. Size 10 is a small signature, 30 a large one.
    const RADAR_KIND = {1: 'land units', 2: 'aircraft', 3: 'ships', 4: 'submarine', 5: 'helicopters'};
    const radarOf = a => { try { if (!a.isRadarContact()) return null; const m = a.getRadarSignatureFeature().signatureSizeMap || {};
        return Object.keys(m).map(k => [RADAR_KIND[k] || 'unknown kind', Math.round(m[k] || 0)]); } catch (e) { return null; } };
    const kindOf = t => { const id = String(t.identifier || '').toLowerCase();
      if (T(() => t.isMissile()) || /missile|icbm/.test(id)) return 'missile';
      if (T(() => t.isAirplane())) return /heli|gunship/.test(id) ? 'heli' : /uav|drone/.test(id) ? 'uav' : /bomber/.test(id) ? 'bomber' : /transport|awacs|patrol|tanker/.test(id) ? 'plane' : 'fighter';
      if (T(() => t.isShip())) return /sub/.test(id) ? 'sub' : /carrier/.test(id) ? 'carrier' : 'ship';
      if (/artillery|howitzer|mlrs|rocket/.test(id)) return 'art';
      if (/(^|_)sam(_|$)|anti_air|antiair|air_defen|flak/.test(id)) return 'aa';
      if (/radar/.test(id)) return 'radar';
      if (/battery|bunker|fortif/.test(id)) return 'fort';
      if (/tank|armored|armoured|fighting_vehicle|ifv|afv|apc|amphib/.test(id)) return 'armor';
      if (/recon|vehicle/.test(id)) return 'recon';
      return 'inf'; };
    const armies = [];
    for (const a of as.getArmiesArray()) {
      if (!a || a.removed) continue;
      const rc = radarOf(a);
      const air = rc ? rc.some(s => s[0] === 'aircraft' || s[0] === 'helicopters') : flies(a), p = posOf(a);
      if (!p) continue;
      addPlayer(a.ownerID);
      const units = []; let kind = 'inf', most = -1, total = 0;
      T(() => { for (const u of a.units) { const t = u.getUnitType(), k = kindOf(t);
        units.push([u.size, String(t.getUnitName()), k, num(T(() => u.getHitPoints()), 10), num(T(() => u.getMaxHitPoints()), 10), t.set || 0]);
        total += u.size; if (u.size > most) { most = u.size; kind = k; } } });
      const st = String(T(() => a.getStatus()) || '');
      const how = [], pts = pathOf(a, p, how);
      let atk = null, patrol = 0;
      if (st === 'patrolling' && a.airplane) patrol = num(T(() => a.getPatrolRadius())) || 0;
      else if (a.attackUnitId > 0 || st === 'attacking' || st === 'bombarding' || st === 'bombing') {
        const t = a.attackUnitId > 0 ? T(() => as.getArmy(a.attackUnitId)) : null;
        const seen = !!(t && !t.removed);
        const tp = (seen ? posOf(t) : null) || a.attackPosition;
        atk = [seen ? t.unitUID : 0, tp && isFinite(tp.x) ? Math.round(tp.x) : null, tp && isFinite(tp.y) ? Math.round(tp.y) : null];
      }
      const by = (T(() => a.battle.attackerUIDs) || []).filter(id => id !== a.unitUID);
      armies.push({id: a.unitUID, x: Math.round(p.x), y: Math.round(p.y), o: a.ownerID, rc,
        w: air ? 'air' : (a.onSea || (rc && rc.some(s => s[0] === 'ships' || s[0] === 'submarine'))) ? 'sea' : 'land',
        name: String(T(() => a.getLongName()) || ''), st, k: kind, n: total, u: units,
        hp: num(a.hitPoints, 10), mhp: num(a.maxHitPoints, 10), str: num(T(() => a.getStrength()), 10), def: num(T(() => a.getDefence()), 10),
        mor: num(T(() => a.getMoralePercent())), kills: a.kills || 0, ent: !!T(() => a.isEntrenched()),
        sight: sightOf(a, air), ds: Math.round(a.detailedViewWidth || 0),
        // aircraft (planes and helicopters): the game's "range" is how far it may fly from its airbase; it hits
        // what is inside its strike/patrol circle around where it flies. Others: their weapon range.
        rng: a.airplane ? Math.round(T(() => a.getPatrolRadius()) || 0) : Math.round(a.range || 0),
        reach: a.airplane ? Math.round(a.range || 0) : 0,
        base: a.airplane ? T(() => { const f = a.airParams.airField; return [Math.round(f.capital.x), Math.round(f.capital.y), String(f.name || '').trim()]; }) : null,
        aa: num(T(() => a.getMaxAntiAirRange())) || 0, radar: num(T(() => a.getMaxRadarRange())) || 0,
        pts, how, eta: pts.length ? etaOf(a) : null, dest: destOf(a, pts),
        spd: num(T(() => air ? a.getAirSpeed() : a.getSpeedOnCurrentTerrain()), 100) || 0,
        ter: String(T(() => a.getTerrainName()) || ''), loc: String(T(() => a.getLocation().name) || '').trim(),
        atk, by, patrol,
        next: a.nextAttackTime > now ? Math.round((a.nextAttackTime - now) / 60000) : null,
        dmg: a.lastDamageTakenTime > 1e12 && now - a.lastDamageTakenTime < 86400e3 ? Math.round((now - a.lastDamageTakenTime) / 60000) : null});
    }
    // every province with an airbase: where aircraft can be based [x, y, owner, name]
    const airfields = [];
    for (const l of gs.getMapState().map.locations) {
      if (!l || !l.borders || !('ownerID' in l)) continue;
      if (T(() => l.hasAirfield())) airfields.push([Math.round(l.capital.x), Math.round(l.capital.y), l.ownerID, String(l.name || '').trim()]);
    }
    return JSON.stringify({ok: true, me, players, owners, battles, armies, airfields});
  } catch (e) { return JSON.stringify({ok: false, error: String(e)}); }
})()"""
# Where the game keeps its own marker pictures (unit symbols by number, the explosion), so the page's map
# can show the same ones. The file names carry a version code, so they are read from the running game.
ASSETS_JS = r"""(function(){
  try {
    const base = location.href.split('?')[0].replace(/[^\/]*$/, '');
    const icons = {}; let boom = '';
    for (const r of performance.getEntriesByType('resource')) {
      if (!r.name.startsWith(base)) continue;
      const p = r.name.slice(base.length).split('?')[0];
      const m = /^images-map\/army_info\/icon_unit_class_(\d+)\.[0-9a-f]+\.png$/.exec(p);
      if (m) icons[m[1]] = p;
      if (/^images-map\/damage_received\.[0-9a-f]+\.png$/.test(p)) boom = p;
    }
    return JSON.stringify({ok: true, base, icons, boom});
  } catch (e) { return JSON.stringify({ok: false}); }
})()"""
ASSET_OK = re.compile(r"^(images-map/[\w@.\-/]+\.png|images/flags/countryFlagsByName/[\w.\-]+\.png)$")
_assets = {}
# Everything known about one province (asked for when it is clicked on the page's map).
PROVINCE_JS = """(function(){
  try {
    const gs = hup.gameState, now = Date.now();
    const l = gs.getMapState().map.locations.find(l => l && l.locationID === LOC_ID);
    if (!l || !('ownerID' in l)) return JSON.stringify({ok: false, error: 'no such province'});
    const T = f => { try { return f(); } catch (e) { return null; } };
    const num = (v, d) => { v = Number(v); d = d || 1; return (v === v && isFinite(v)) ? Math.round(v * d) / d : null; };
    const buildings = (T(() => l.getUpgrades()) || []).filter(u => !T(() => u.hasInvisibleFeature()))
      .map(u => [String(T(() => u.getName()) || ''), T(() => u.getLevel()) || 0, num(T(() => u.getConditionPercent())), !!T(() => u.isConstructing())]);
    const resource = T(() => { const e = gs.getMod().resourceEntries, r = e[l.productionType] || (e.get && e.get(l.productionType));
      return String(r.name || r.resourceName || r.getName()); });
    return JSON.stringify({ok: true, id: l.locationID, name: String(l.name || '').trim(), owner: l.ownerID,
      type: T(() => l.getProvinceTypeName()), state: T(() => l.getProvinceStateNameForOwner()), terrain: T(() => l.getTerrainName()),
      morale: num(l.morale), pop: num(l.population), vp: num(T(() => l.getTotalVictoryPoints()), 100),
      res: num(l.production), tax: num(l.taxProduction), resource,
      coastal: !!l.coastal, airfield: !!T(() => l.hasAirfield()), capital: !!T(() => l.isCapital()), core: !!T(() => l.isOwnersCoreProvince()),
      lastBattle: l.lastBattle > 1e12 ? Math.round((now - l.lastBattle) / 60000) : null, buildings});
  } catch (e) { return JSON.stringify({ok: false, error: String(e)}); }
})()"""


def geo_bytes():
    """The map's shape as JSON bytes (read from the game once, then from disk). None if unavailable."""
    cached = sorted(STATE.glob("geo-*.json"), key=lambda p: p.stat().st_mtime)
    if cached:
        return cached[-1].read_bytes()
    r = viewer_call("js", code=GEO_JS, timeout=40) if viewer_pid() else None
    try:
        text = r["result"]
        d = json.loads(text)
        if not d.get("ok"):
            return None
        (STATE / f"geo-{d['id']}.json").write_text(text)
        return text.encode()
    except Exception:
        return None


def assets_info():
    """{base, icons: {number: path}, boom}: asked from the game once, remembered on disk in case a later
    game session no longer lists them."""
    if _assets.get("icons"):
        return _assets
    saved = STATE / "assets.json"
    try:
        d = json.loads(viewer_js(ASSETS_JS))
    except Exception:
        d = {}
    if d.get("ok") and len(d.get("icons", {})) >= 5 and str(d.get("base", "")).startswith("https://"):
        saved.write_text(json.dumps(d))
    else:
        try:
            d = json.loads(saved.read_text())
        except Exception:
            d = {}
    _assets.clear()
    _assets.update(d)
    return _assets


def asset_bytes(path):
    """One of the game's public pictures (a unit symbol or a flag), fetched once and kept in state/assets."""
    if not ASSET_OK.match(path) or ".." in path:
        return None
    f = STATE / "assets" / path.replace("/", "__")
    if f.exists():
        return f.read_bytes()
    base = assets_info().get("base", "")
    if not base.startswith("https://"):
        return None
    import urllib.request
    try:
        req = urllib.request.Request(base + path, headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) > 2_000_000:
        return None
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(data)
    return data


def province_data(loc_id):
    try:
        return json.loads(viewer_js(PROVINCE_JS.replace("LOC_ID", str(int(loc_id)))))
    except Exception:
        return {"ok": False, "error": "The game did not answer."}


def world_data():
    try:
        d = json.loads(viewer_js(WORLD_JS))
        return d if d.get("ok") else None
    except Exception:
        return None


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
PREDICT_MIN_CHANCE = 0.4
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
        mv = motion(old, e, now)
        tracks[str(e["id"])] = {"mv": mv, "o": e.get("o"), "nation": e.get("nation", ""), "war": e.get("war"), "units": e.get("units", []),
                                "air": e.get("air"), "sea": e.get("sea"), "spd": e.get("spd", 0), "knots": e.get("knots", []),
                                "pos": [round(e["x"]), round(e["y"])], "seen": now, "to": e.get("to", ""), "plane": e.get("plane"),
                                "move": e.get("move")}
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
    chance = stay(el, CHANCE_MINUTES["patrol"])
    area = p.get("patrol") or 50
    x0, y0 = t["pos"]
    arrive = t["seen"] + d(x0, y0, cx, cy) / v * 60000      # still on its way to the spot when last seen?
    turn = p.get("mustReturn") or (t["seen"] + fuel - leg)  # when it must head home (unknown: assume it just set off)
    guess = "" if p.get("mustReturn") else " (when it took off is not known, so this time is a guess)"
    if now < arrive:
        f = (now - t["seen"]) / max(1.0, arrive - t["seen"])
        return x0 + (((cx - x0) + mapw / 2) % mapw - mapw / 2) * f, y0 + (cy - y0) * f, 0, f"flying to its patrol spot", turn, chance
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
            return bx + (((cx - bx) + mapw / 2) % mapw - mapw / 2) * f, by + (cy - by) * f, 0, f"refuelled; flying back out to its spot", None, chance
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
    fuel = (p.get("fuel") or 14400) / 60                   # minutes it can stay in the air at most
    refuel = (p.get("refuel") or 900) / 60                 # minutes it refuels after landing
    back_by = t["seen"] + fuel * 60000
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
                chance_then * stay(el - landed_after - refuel, CHANCE_MINUTES["parked"]))
    if p.get("refuelEnd") and p["refuelEnd"] > t["seen"]:  # it was refuelling when last seen
        return parked((p["refuelEnd"] - t["seen"]) / 60000 - refuel, 1.0, "was refuelling")
    if not p.get("flying"):
        return bx, by, 0, f"on the ground at its airbase {name} when last seen", None, stay(el, CHANCE_MINUTES["parked"])
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
        return x0 + dx * f, y0 + (by - y0) * f, 0, f"flying back to its airbase {name}", None, stay(el, CHANCE_MINUTES["flying"])
    return parked(extra + home, stay(extra + home, CHANCE_MINUTES["flying"]), "back")


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
                        0.5 * math.exp(-el / 60))
            return x % mapw, y, 0, note, None, math.exp(-el / CHANCE_MINUTES["standing"])
        err = 4 + 0.5 * spd * el
    if plane:                                             # an aircraft on its way out to its target: on its route or not
        return x % mapw, y, 0, note, None, math.exp(-el / CHANCE_MINUTES["flying"])
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
    owner_of = {}                                         # location id -> owner, for the foreign-land slowdown
    try:
        land = json.loads((sorted(STATE.glob("geo-*.json"), key=lambda p: p.stat().st_mtime)[-1]).read_text())["land"]
        owner_of = {p["id"]: o for p, o in zip(land, world.get("owners", []))}
    except Exception:
        pass
    rel = {str(k): v[1] for k, v in (world.get("players") or {}).items()}
    eyes = [(a["x"], a["y"], a.get("sight") or 0) for a in world.get("armies", [])
            if rel.get(str(a.get("o"))) in ("me", "ally") and not a.get("rc")]
    out = []
    for tid, t in tracks.items():
        if tid in visible:
            continue
        el = (now - t["seen"]) / 60000
        if el < 0.5 or el > PREDICT_HOURS * 60 or (t.get("air") and not (t.get("plane") or {}).get("base") and el > 20):
            continue                                      # (aircraft whose airbase is unknown: only briefly)
        x, y, err, note, back_by, chance = predict(t, now, mapw)
        if chance is not None and chance < PREDICT_MIN_CHANCE:
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
            if chance < PREDICT_MIN_CHANCE * 0.75:
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
                    "note": note, "units": t.get("units", []), "air": t.get("air"), "sea": t.get("sea"),
                    "last": t.get("pos"), "to": t.get("to", ""),
                    "base": (t.get("plane") or {}).get("baseName", ""), "back_by": back_by})
    return out


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
                                   "units": sum(n for _, n in e.get("units", []))})
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
                       "units": sum(n for _, n in e.get("units", []))})
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
    out = {}
    for ev in evs:
        o = str(ev.get("o"))
        s = out.setdefault(o, {"o": ev.get("o"), "orders": 0, "hours": [0] * 24, "days": set(), "last": 0,
                               "dest": {}, "routes": {}, "kinds": {}})
        when = dt.datetime.fromtimestamp(ev["t"])
        s["orders"] += 1
        s["hours"][when.hour] += 1
        s["days"].add(when.date())
        s["last"] = max(s["last"], ev["t"])
        s["kinds"][ev["kind"]] = s["kinds"].get(ev["kind"], 0) + 1
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
                    "shuttles": sorted(shuttles, key=lambda x: -x[2])[:3], "kinds": s["kinds"]})
    res.sort(key=lambda r: -r["orders"])
    return {"players": res, "watched": watched, "events": len(evs)}


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
    new_file = _hist["file"] != name or not name.exists()
    if new_file or world.get("owners") != _hist["own"]:
        rec["own"] = world.get("owners")
    if new_file or world.get("players") != _hist["pl"]:
        rec["pl"] = world.get("players")
    if new_file and world.get("airfields"):
        rec["af"] = world["airfields"]
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
    own = pl = af = None
    best = None
    for rec in _history_lines(path):
        own = rec.get("own", own)
        pl = rec.get("pl", pl)
        af = rec.get("af", af)
        if best is None or abs(rec["t"] - t) < abs(best["t"] - t):
            best = dict(rec, own=own, pl=pl, af=af)
        elif rec["t"] > t:
            break
    if not best:
        return {"ok": False, "error": "No history saved yet."}
    return {"ok": True, "t": best["t"], "world": {"armies": best["a"], "owners": best["own"], "players": best["pl"],
                                                  "airfields": best.get("af") or [], "battles": [], "me": None}}


def game_data():
    """Read the enemy armies and the map view straight from the game. None if that is not possible."""
    try:
        d = json.loads(viewer_js(GAME_JS))
        return d if d.get("ok") else None
    except Exception:
        return None


def in_world(e, world, mapw):
    """Is an army inside a spot's map rectangle? (the map wraps around left to right)"""
    x, y, w, h = world
    return (e["x"] - x) % mapw <= w and y <= e["y"] <= y + h


def inside(e, spot, mapw):
    """Is an army in a spot? A spot is a map rectangle, or (when it follows a troop) a circle round it."""
    c = spot.get("circle")
    if c:
        dx = abs(e["x"] - c[0]) % mapw
        dx = min(dx, mapw - dx)
        return dx * dx + (e["y"] - c[1]) ** 2 <= c[2] ** 2
    return in_world(e, spot["world"], mapw)


def troop_spot(spot, data):
    """A spot that follows one of your (or an ally's) groups, as the circle that group can see - or None
    when that group is no longer there."""
    f = next((a for a in data.get("friends", []) if a["id"] == spot["army"]), None)
    if not f:
        return None
    r = float(spot.get("radius") or f.get("sight") or 0)
    return {**spot, "circle": [f["x"], f["y"], r], "world": [f["x"] - r, f["y"] - r, 2 * r, 2 * r]}


def to_picture(x, y, view, pix):
    """Map position -> picture pixel, for the given view."""
    return ((x - view["x"]) % view["mapw"]) * view["scale"] * pix, (y - view["y"]) * view["scale"] * pix


def to_world(rect, view, pix):
    """Picture rectangle [x, y, w, h] -> map rectangle."""
    k = view["scale"] * pix
    return [(view["x"] + rect[0] / k) % view["mapw"], view["y"] + rect[1] / k, rect[2] / k, rect[3] / k]


def eta_text(minutes):
    """90 -> '1h 30m'"""
    if not minutes:
        return ""
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def radar_text(e):
    """'large aircraft signature' for a radar-only contact"""
    return " and ".join(f"{'small' if size <= 10 else 'large'} {kind}" for kind, size in e.get("radar") or []) + " signature"


def relation_text(e):
    if e.get("radar") and not e.get("nation"):
        return "radar contact, country unknown"
    return ("radar contact, " if e.get("radar") else "") + ("at war" if e.get("war", True) else "neutral")


def describe(enemies):
    """'China (at war): 1 Corvette at sea, heading for Colombo (arrives in 3h 20m); India (neutral): ...'"""
    parts = []
    for e in enemies:
        units = radar_text(e) if e.get("radar") else ", ".join(f"{n} {name}" for name, n in e["units"]) or "units"
        where = "in the air" if e["air"] else "at sea" if e["sea"] else "on land"
        if e.get("to"):
            where += f", heading for {e['to']}" + (f" (arrives in {eta_text(e['eta'])})" if e.get("eta") else "")
        who = e["nation"] or ("Unidentified" if e.get("radar") else "Unknown country")
        parts.append(f"{who} ({relation_text(e)}): {units} {where}")
    text = "; ".join(parts[:4])
    return text + (f"; and {len(parts) - 4} more groups" if len(parts) > 4 else "")


def enemy_picture(spot, enemies, path):
    """Picture centred on the first enemy, with the spot outlined and the enemy circled.
    Moves the hidden map there for a moment if the enemy is not in view, then puts it back."""
    info, view = viewer_call("info"), view_get()
    if not info or not view:
        return None
    pix, e = info["scale"], enemies[0]
    px, py = to_picture(e["x"], e["y"], view, pix)
    W, H = view["w"] * pix, view["h"] * pix
    moved = not (300 < px < W - 300 and MAP_TOP + 250 < py < H - 250)
    try:
        if moved:
            view_set(e["x"] - view["w"] / view["scale"] / 2, e["y"] - view["h"] / view["scale"] / 2)
            time.sleep(0.8)
        st, img = grab_viewer()
        now = view_get()
        if st != "ok" or not now:
            return None
        px, py = [int(v) for v in to_picture(e["x"], e["y"], now, pix)]
        zx, zy = [int(v) for v in to_picture(spot["world"][0], spot["world"][1], now, pix)]
        k = now["scale"] * pix
        cw, ch = min(img.shape[1], 1300), min(img.shape[0] - MAP_TOP, 1000)
        x0 = max(0, min(img.shape[1] - cw, px - cw // 2))
        y0 = max(MAP_TOP, min(img.shape[0] - ch, py - ch // 2))
        obs = {"pos": (x0, y0), "size": (cw, ch),
               "det": {"zone": (zx, zy, int(spot["world"][2] * k), int(spot["world"][3] * k)), "spot": (px, py)}}
        return spot_picture(img, obs, path)
    except Exception as ex:
        log(f"picture failed: {type(ex).__name__}: {ex}")
        return None
    finally:
        if moved:
            view_set(view["x"], view["y"])


# ------------------------------------------------------------------ capture
# ------------------------------------------------------------------ capture
def find_window():
    """Returns ("ok", window_id) or (reason, None)."""
    try:
        out = subprocess.run(["osascript", "-e", FIND_WINDOW], capture_output=True,
                             text=True, timeout=20).stdout.strip()
    except subprocess.TimeoutExpired:
        return "nosafari", None
    if out.startswith("ok "):
        return "ok", int(out.split()[1])
    return (out or "nosafari"), None


def fix_window():
    if SOURCE == "viewer":
        if viewer_start() == "ok" and viewer_js(HAS_MAP) == "yes":
            return "The background game is running."
        if viewer_open_game() == "ok":
            return "The background game is running again."
        viewer_call("show")
        return "Log in to the game in the window that just opened, then close that window."
    r = subprocess.run(["osascript", "-e", FIX_WINDOW], capture_output=True, text=True, timeout=30)
    return r.stdout.strip() or f"Could not fix it: {r.stderr.strip()}"


def grab():
    """Picture of the game. Returns ("ok", image) or (reason, None)."""
    if TEST_IMAGE:
        return "ok", np.asarray(Image.open(TEST_IMAGE).convert("RGB"))
    return grab_viewer() if SOURCE == "viewer" else grab_safari()


def grab_safari():
    """Screenshot the Safari game window (works while it is behind other windows)."""
    st, win = find_window()
    if st != "ok":
        return st, None
    STATE.mkdir(exist_ok=True)
    path = STATE / f"capture-{os.getpid()}.png"
    try:
        subprocess.run(["screencapture", "-x", "-o", "-l", str(win), "-t", "png", str(path)],
                       capture_output=True, timeout=20)
        img = np.asarray(Image.open(path).convert("RGB"))
    except Exception:
        return "nocapture", None
    finally:
        path.unlink(missing_ok=True)
    if img.mean() < 4:          # all black: display asleep / locked
        return "nocapture", None
    return "ok", img


# ---------------------------------------------------------- locate the spot
def edge_map(img):
    g = img.astype(np.float32).mean(2)
    h, w = g.shape
    g = g[:h // SCALE * SCALE, :w // SCALE * SCALE]
    g = g.reshape(h // SCALE, SCALE, w // SCALE, SCALE).mean((1, 3))
    return np.abs(np.diff(g, axis=1))[:-1] + np.abs(np.diff(g, axis=0))[:, :-1]


def _box_sums(a, h, w):
    s = np.zeros((a.shape[0] + 1, a.shape[1] + 1), np.float64)
    s[1:, 1:] = a.cumsum(0).cumsum(1)
    return s[h:, w:] - s[:-h, w:] - s[h:, :-w] + s[:-h, :-w]


def edge_maps(img):
    """Edge maps of a screenshot at four half-step offsets, so a pan that does not
    land on the 1/4-size grid still matches well. Returns [(dx, dy, edges), ...]."""
    half = SCALE // 2
    return [(dx, dy, edge_map(img[dy:, dx:]).astype(np.float64)) for dy in (0, half) for dx in (0, half)]


def locate(maps, template):
    """Find a reference picture inside a screenshot. maps is edge_maps(screenshot).
    Returns (x, y, score) in screenshot pixels, or None if the view does not match."""
    best = None
    for dx, dy, e in maps:
        hit = _locate(e, template)
        if hit and (best is None or hit[2] > best[2]):
            best = (hit[0] + dx, hit[1] + dy, hit[2])
        if best and best[2] >= 0.6:               # clearly right: no need to try the other offsets
            break
    return best if best and best[2] >= MATCH_MIN else None


def _locate(e, template):
    h, w = template.shape
    if e.shape[0] < h or e.shape[1] < w:
        return None
    t0 = template - template.mean()
    corr = np.fft.irfft2(np.fft.rfft2(e) * np.conj(np.fft.rfft2(t0, s=e.shape)), s=e.shape)
    corr = corr[:e.shape[0] - h + 1, :e.shape[1] - w + 1]
    var = _box_sums(e * e, h, w) - _box_sums(e, h, w) ** 2 / (h * w)
    ncc = corr / (np.sqrt((t0 * t0).sum()) * np.sqrt(np.maximum(var, 1e-6)))
    y, x = np.unravel_index(np.argmax(ncc), ncc.shape)
    return int(x) * SCALE, int(y) * SCALE, float(ncc[y, x])


# ------------------------------------------------------------- red detection
def red_mask(rgb):
    """Pixels in the strong red of enemy unit markers (about RGB 154, 22, 28)."""
    a = rgb.astype(np.float32) / 255
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(2), a.min(2)
    d = mx - mn
    sat = np.where(mx > 0, d / np.maximum(mx, 1e-6), 0)
    # hue within ~18 degrees of pure red: red is the largest channel and green/blue are close together
    near_red = (r >= mx - 1e-6) & (np.abs(g - b) <= 0.3 * np.maximum(d, 1e-6))
    return near_red & (sat > 0.55) & (mx > 0.40)


def red_blobs(mask):
    """Connected patches of red in a mask: [(pixels, x0, y0, x1, y1), ...], biggest first."""
    m = mask.copy()
    out = []
    for y, x in zip(*np.nonzero(m)):
        if not m[y, x]:
            continue
        stack, n = [(y, x)], 0
        m[y, x] = False
        x0 = x1 = x
        y0 = y1 = y
        while stack:
            cy, cx = stack.pop()
            n += 1
            x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
            for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                if 0 <= ny < m.shape[0] and 0 <= nx < m.shape[1] and m[ny, nx]:
                    m[ny, nx] = False
                    stack.append((ny, nx))
        out.append((n, int(x0), int(y0), int(x1) + 1, int(y1) + 1))
    return sorted(out, reverse=True)


def detect(img, zone):
    """Look for an enemy unit inside a box (x, y, w, h) of the picture. Enemies are drawn in two ways:
      zoomed out: a red square about 40 px across with a white diamond in the middle
      zoomed in:  solid red plates about 67 x 45 px (unit icon on the left, a count on the right),
                  stacked under the owner's flag
    Both give a patch of strong red at least 24 px in each direction. Thinner red (the 19 px stripe
    of your own Philippine flag, lines) is ignored. What kind of unit it is, is decided by classify().
    Returns dict(count, zone, spot=(x, y) or None) in picture pixels."""
    zx, zy, zw, zh = zone
    crop = img[zy:zy + zh, zx:zx + zw]
    m = red_mask(crop)
    if m.sum() > 80000:                           # a view full of red: too much to sort through
        return {"count": 0, "zone": zone, "spot": None}
    c16 = crop.astype(np.int16)
    green = (c16[..., 1] > c16[..., 0] + 25) & (c16[..., 1] > c16[..., 2] + 25)
    for n, x0, y0, x1, y1 in red_blobs(m):
        if n < MIN_RED_PIXELS:
            break
        w, h = x1 - x0, y1 - y0
        if min(w, h) < 24:
            continue                              # thin: a flag stripe or a line
        if h < 34 and green[y1:y1 + 60, max(0, x0 - 20):x1 + 20].sum() > 400:
            continue                              # a red band of a flag sitting on friendly green plates
        return {"count": n, "zone": zone, "spot": (zx + (x0 + x1) // 2, zy + (y0 + y1) // 2)}
    return {"count": 0, "zone": zone, "spot": None}


def observe(img, e, spot):
    """Look at one spot in a screenshot. e is edge_maps(img)."""
    try:
        template, size = ref_edges(spot)
    except Exception:
        return {"state": "moved"}
    pos = locate(e, template)
    if pos is None:
        return {"state": "moved"}
    zx, zy, zw, zh = spot["zone"]
    det = detect(img, (pos[0] + zx, pos[1] + zy, zw, zh))
    return {"state": "ok", "pos": pos[:2], "size": size, "score": pos[2], "det": det}


# ------------------------------------------------------------------ pictures
def spot_picture(img, obs, path):
    """Picture of the area around a spot: box outlined, sighting circled, zoom inset."""
    fx, fy = obs["pos"]
    fw, fh = obs["size"]
    det = obs["det"]
    pic = Image.fromarray(img[fy:fy + fh, fx:fx + fw]).convert("RGB")
    d = ImageDraw.Draw(pic)
    zx, zy, zw, zh = det["zone"]
    d.rectangle([zx - fx, zy - fy, zx - fx + zw, zy - fy + zh], outline=(255, 220, 0), width=2)
    if det["spot"]:
        sx, sy = det["spot"][0] - fx, det["spot"][1] - fy
        d.ellipse([sx - 48, sy - 48, sx + 48, sy + 48], outline=(255, 40, 40), width=4)
        zoom = zoom_crop(img, det["spot"], 70, 4).resize((280, 280))
        ix = 12 if sx > fw // 2 else fw - 292
        pic.paste(zoom, (ix, 12))
        d.rectangle([ix, 12, ix + 280, 292], outline=(255, 40, 40), width=3)
    path.parent.mkdir(parents=True, exist_ok=True)
    pic.save(path, quality=90)
    return path


def zoom_crop(img, spot, half, factor):
    x, y = spot
    h, w = img.shape[:2]
    x0, y0 = max(0, min(w - 2 * half, x - half)), max(0, min(h - 2 * half, y - half))
    crop = Image.fromarray(img[y0:y0 + 2 * half, x0:x0 + 2 * half])
    return crop.resize((2 * half * factor, 2 * half * factor), Image.LANCZOS)


# ------------------------------------------------------------- what is it
def classify(img, spot_xy):
    """Ask Claude to confirm the red thing is an enemy unit and to say what it is.
    Returns (verdict, summary): verdict is ENEMY, AIRCRAFT (only aircraft), NONE or UNKNOWN."""
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    if not Path(claude).exists():
        return "UNKNOWN", ""
    path = STATE / "classify.png"
    zoom_crop(img, spot_xy, 150, 3).save(path)
    prompt = (
        f"Read the image file {path}. It is an enlarged crop from the map of the strategy game "
        "Conflict of Nations. Enemy units are drawn in RED, in one of two ways. Zoomed out: a red square "
        "marker with a white diamond in the middle and a dark silhouette of the unit type inside it. "
        "Zoomed in: solid red plates stacked under a flag, each with a small white box on the left holding "
        "a silhouette of the unit type and a number on the right (how many; the digits are slanted, and a 1 "
        "has a small flag at the top that can look like a 7); 3D models of the units, or of "
        "the boat carrying them, may be drawn nearby. Units crossing the sea ride boats but their plates "
        "still show tank or infantry silhouettes. Pink diamonds with crossed rifles only mark fighting and "
        "are not units. GREEN, blue or other coloured markers and plates are friendly or neutral, not "
        "enemies, even when the flag above them has red in it; ignore them. Small grey ship-shaped symbols "
        "where thin grey lines meet are map waypoints, not units; ignore them too.\n"
        "Tell silhouettes apart carefully: a TANK is a short low body with a turret on top and one gun "
        "barrel sticking out sideways; INFANTRY is a helmet; a SHIP is a long hull with a pointed bow and "
        "a tall superstructure or masts rising from the deck; a PLANE has wings.\n"
        "First write one short line per RED marker or plate: what its silhouette shows, the number on it "
        "if any, and whether it sits over sea (blue water) or over land. Then finish with exactly these "
        "two lines:\n"
        "SUMMARY: <what the enemy has there in at most 12 words, for example: 1 tank and 2 infantry at sea>\n"
        "VERDICT: <one word>\n"
        "where the word is ENEMY if there is at least one red enemy unit that is not an aircraft, "
        "AIRCRAFT if the only red enemy units are planes, helicopters, drones or missiles, "
        "or NONE if there is no red enemy marker or plate at all."
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_JOB"))}
    try:
        raw = subprocess.run(
            [claude, "-p", prompt, "--model", CLASSIFY_MODEL, "--allowedTools", "Read",
             "--no-session-persistence"],
            capture_output=True, text=True, timeout=150, cwd=str(STATE), env=env).stdout
    except Exception as e:
        log(f"classify failed: {e}")
        return "UNKNOWN", ""
    m = re.search(r"SUMMARY:\s*(.+)", raw)
    summary = m.group(1).strip().strip("<>.*")[:120] if m else ""
    tail = raw.upper().rsplit("VERDICT:", 1)[-1]
    for word in ("ENEMY", "AIRCRAFT", "NONE"):
        if word in tail:
            return word, summary
    log(f"classify: unexpected reply {raw[-160:]!r}")
    return "UNKNOWN", summary


# -------------------------------------------------------------------- record
def record(spot_name, rows, kind, sent, picture):
    """Append what an alert was about to alerts.csv.
    rows = [(country, relation, units, where, x, y, heading_for, arrives_in_min, sight), ...]"""
    try:
        new_file = not RECORD.exists()
        with RECORD.open("a", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(RECORD_COLUMNS)
            stamp = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"
            for row in rows:
                w.writerow([stamp, spot_name, *row, kind,
                            "dry run" if DRY_RUN else "yes" if sent else "FAILED", str(picture or "")])
    except Exception as ex:
        log(f"could not write {RECORD.name}: {type(ex).__name__}: {ex}")


def record_tail(n=10):
    """The last n rows of alerts.csv as dicts, newest first."""
    try:
        with RECORD.open(newline="") as f:
            return list(csv.DictReader(f))[-n:][::-1]
    except Exception:
        return []


# ------------------------------------------------------------------- message
# ------------------------------------------------------------------- email
# Alerts can go by email instead of iMessage, sent through your own mail account's outgoing server. email.json (readable only by you)
# holds {"from", "password", "to", "host", "port", "on"}; the password never goes in the log or to the page.
EMAIL_FILE = ROOT / "email.json"
MAIL_SERVERS = {"gmail.com": ("smtp.gmail.com", 465), "googlemail.com": ("smtp.gmail.com", 465),
                "icloud.com": ("smtp.mail.me.com", 587), "me.com": ("smtp.mail.me.com", 587), "mac.com": ("smtp.mail.me.com", 587),
                "outlook.com": ("smtp-mail.outlook.com", 587), "hotmail.com": ("smtp-mail.outlook.com", 587),
                "live.com": ("smtp-mail.outlook.com", 587), "yahoo.com": ("smtp.mail.yahoo.com", 465),
                "yahoo.com.au": ("smtp.mail.yahoo.com", 465), "fastmail.com": ("smtp.fastmail.com", 465)}


def email_load():
    try:
        return json.loads(EMAIL_FILE.read_text())
    except Exception:
        return {}


def email_save(d):
    tmp = EMAIL_FILE.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.write_text(json.dumps(d))
    os.chmod(tmp, 0o600)
    tmp.replace(EMAIL_FILE)


def email_state():
    d = email_load()
    return {"on": bool(d.get("on")), "pending": bool(d.get("from") and not d.get("on")),
            "from": d.get("from", ""), "to": d.get("to", ""), "host": d.get("host", "")}


def email_send(d, text, image=None, to=None):
    """Send one email to the list `to` (with the picture attached if given). Raises RuntimeError with the reason."""
    import smtplib
    import ssl
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = f"CON alert <{d['from']}>"
    msg["To"] = ", ".join(to or [d["to"]])
    msg["Subject"] = (text.split(": ", 2)[0] + ": " + text.split(": ", 2)[1] if text.startswith("CON ALERT: ") and text.count(": ") >= 2
                      else text)[:110]
    msg.set_content(text)
    if image and Path(image).exists():
        msg.add_attachment(Path(image).read_bytes(), maintype="image", subtype="jpeg", filename=Path(image).name)
    host, port, local = d["host"], int(d["port"]), d["host"] in ("127.0.0.1", "localhost")
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            elif not local:
                s.close()
                raise RuntimeError("That mail server does not offer an encrypted connection, so the password was not sent")
        with s:
            try:
                # one login attempt only: after a second refused attempt Gmail just hangs up, which hides the reason
                s.ehlo_or_helo_if_needed()                # the server's list of login methods comes with its greeting
                if "PLAIN" in s.esmtp_features.get("auth", "").upper().split():
                    s.user, s.password = d["from"], d["password"]
                    s.auth("PLAIN", s.auth_plain)
                else:
                    s.login(d["from"], d["password"])
            except smtplib.SMTPAuthenticationError as ex:
                said = " ".join(re.sub(r"https?://\S+|\b\d\.\d\.\d+\b|\S+ - gsmtp", "", ex.smtp_error.decode("utf-8", "replace")).split())
                said = said.replace("For more information, go to", "").strip(" .")
                app = "application-specific password" in said.lower() or "app password" in said.lower()
                raise RuntimeError(("The mail server wants an app password, not your normal password. " if app else
                                    "The mail server did not accept that address and password. Gmail, iCloud and Yahoo need an "
                                    "app password made for this (16 letters), not your normal password. ")
                                   + (f'It said: "{said[:140]}".' if said else "")) from None
            except smtplib.SMTPServerDisconnected:
                raise RuntimeError("The mail server hung up during the login, which usually means it did not accept the "
                                   "address and password. Gmail, iCloud and Yahoo need an app password, not your normal one.") from None
            s.send_message(msg)
    except RuntimeError:
        raise
    except smtplib.SMTPRecipientsRefused:
        raise RuntimeError("The mail server refused the address the email was going to. Check it for typing mistakes.") from None
    except smtplib.SMTPResponseException as ex:
        said = " ".join(str(ex.smtp_error.decode("utf-8", "replace") if isinstance(ex.smtp_error, bytes) else ex.smtp_error).split())
        raise RuntimeError(f'The mail server refused the message. It said: "{said[:160]}".') from None
    except smtplib.SMTPException as ex:
        raise RuntimeError(f"The mail server {host} ended the conversation unexpectedly ({type(ex).__name__}). "
                           "If this is not Gmail, iCloud or Yahoo, check the server and port.") from None
    except (OSError, ssl.SSLError) as ex:
        raise RuntimeError(f"The mail server {host} could not be reached ({type(ex).__name__})") from None


def email_setup(sender, password, to="", host="", port=""):
    """Step 1: remember the account and send a test email. Alerts only switch to email after email_confirm()."""
    ok = r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+"
    sender, to, password = str(sender or "").strip(), str(to or "").strip() or str(sender or "").strip(), str(password or "")
    if not re.fullmatch(ok, sender) or not re.fullmatch(ok, to):
        raise RuntimeError("That does not look like an email address.")
    if not password.strip():
        raise RuntimeError("The password is missing.")
    if re.fullmatch(r"\s*(\w{4}\s+){3}\w{4}\s*", password):       # app passwords are shown in groups of four
        password = "".join(password.split())
    host = str(host or "").strip() or MAIL_SERVERS.get(sender.rsplit("@", 1)[1].lower(), ("", 0))[0]
    if not host:
        raise RuntimeError("I do not know the outgoing mail server for that address. Fill in the server (and port) "
                           "from your mail provider's help page, for example smtp.example.com and 587.")
    try:
        port = int(port or MAIL_SERVERS.get(sender.rsplit("@", 1)[1].lower(), ("", 587))[1])
    except ValueError:
        raise RuntimeError("The port must be a number, usually 465 or 587.") from None
    d = {"from": sender, "password": password, "to": to, "host": host, "port": port, "on": False}
    try:
        email_send(d, "CON alert test. If you can read this email, alerts can reach you here.")
    except RuntimeError as ex:
        log(f"email setup failed at {host}:{port}: {ex}")
        raise
    email_save(d)
    return email_state()


def email_confirm():
    d = email_load()
    if not d.get("from"):
        raise RuntimeError("Email is not set up yet.")
    d["on"] = True
    email_save(d)
    r = recipients()
    if d["to"].lower() not in [e.lower() for e in r["emails"]]:      # the tested address starts the list
        recipients_save(r["phones"], r["emails"] + [d["to"]], r["sms"], r["whatsapp"], r["paused"])
    log("email: switched on")
    return email_state()


def email_off():
    was = email_load().get("on")
    EMAIL_FILE.unlink(missing_ok=True)
    r, a = recipients(), recipients(active=True)
    if not a["phones"] and not (wa_load().get("token") and a["whatsapp"]):       # never leave alerts with nowhere to go
        recipients_save([PHONE], r["emails"], r["sms"], r["whatsapp"], r["paused"])
    if was:
        log("email: switched off")
    return email_state()


# ---------------------------------------------------------------- WhatsApp
# Alerts can also go out as WhatsApp messages through Meta's official service (the "Cloud API"), from the
# business number of your Meta app. whatsapp.json (readable only by you) holds {"token", "phone_id",
# "from", "waba": business account id, "template", "lang", "tpl_status"}. The token never goes in the log.
#
# WhatsApp's rule: a business number may only write freely to someone who wrote to it in the last 24
# hours. At any other time only a pre-approved "template" message is allowed. So alerts are sent as the
# template below (made automatically, approved by Meta), and the picture is sent after it as a normal
# message, which arrives only when that 24-hour window is open.
WA_FILE = ROOT / "whatsapp.json"
WA_API = "https://graph.facebook.com"
WA_TEMPLATE = "con_alert"
WA_TEMPLATE_TEXT = "CON alert: {{1}}. Open the CON alert map for details."


def wa_number(p):
    """WhatsApp needs the full international number: 0432... (typed the local way) becomes +61432..."""
    digits = re.sub(r"\D", "", str(p))
    if str(p).strip().startswith("+0") or (digits.startswith("0") and not str(p).strip().startswith("+")):
        digits = COUNTRY_CODE.lstrip("+") + digits.lstrip("0")
    return "+" + digits


def wa_load():
    return _read_json(WA_FILE, {})


def wa_save(d):
    tmp = WA_FILE.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.write_text(json.dumps(d))
    os.chmod(tmp, 0o600)
    tmp.replace(WA_FILE)


def wa_call(token, path, fields=None, method="GET", upload=None, timeout=30):
    """One call to Meta's service. Returns its answer; raises RuntimeError with Meta's own reason."""
    import urllib.error
    import urllib.parse
    import urllib.request
    url, headers, data = f"{WA_API}/{path}", {"Authorization": f"Bearer {token}"}, None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(fields or {})
    elif upload:
        b = uuid.uuid4().hex
        parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in (fields or {}).items()]
        p = Path(upload)
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{p.name}"\r\n'
                     f'Content-Type: image/jpeg\r\n\r\n'.encode() + p.read_bytes() + b"\r\n")
        parts.append(f"--{b}--\r\n".encode())
        data, headers["Content-Type"] = b"".join(parts), f"multipart/form-data; boundary={b}"
    else:
        data, headers["Content-Type"] = json.dumps(fields or {}).encode(), "application/json"
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=timeout).read())
    except urllib.error.HTTPError as ex:
        try:
            err = json.loads(ex.read()).get("error", {})
        except Exception:
            err = {}
        if err.get("code") == 190:
            raise RuntimeError("the WhatsApp access token is wrong or has expired. Generate a new one on Meta's page and connect again") from None
        if err.get("code") == 131030:
            raise RuntimeError("that number is not on your Meta app's list of allowed recipients. Add it under Manage recipients on Meta's page") from None
        said = err.get("error_user_msg") or (err.get("error_data") or {}).get("details") or err.get("message") or f"error {ex.code}"
        raise RuntimeError(f'Meta said: "{" ".join(str(said).split())[:220]}"') from None
    except Exception as ex:
        raise RuntimeError(f"Meta's WhatsApp service could not be reached ({type(ex).__name__})") from None


def wa_state():
    d = wa_load()
    st = d.get("tpl_status", "")
    exp = int(d.get("expires") or 0)
    return {"on": bool(d.get("token")), "from": d.get("from", ""), "template": st,
            "ready": st == "APPROVED", "note": d.get("tpl_error", "") if d.get("waba") else "",
            "need_account": bool(d.get("token")) and not d.get("waba"),
            "expires": f"{dt.datetime.fromtimestamp(exp):%-d %b %Y}" if exp else "",
            "expires_soon": bool(exp) and exp - time.time() < 7 * 86400}


def wa_template(d):
    """Make sure the alert template exists in the business account, and note whether Meta has approved it."""
    d.pop("tpl_error", None)
    try:
        if not d.get("waba"):
            raise RuntimeError("the business account behind that number could not be found, so the alert template could not be made")
        got = wa_call(d["token"], f"{d['waba']}/message_templates", {"name": WA_TEMPLATE, "fields": "name,status,language"})
        rows = [t for t in got.get("data", []) if t.get("name") == WA_TEMPLATE]
        if rows:
            d["tpl_status"], d["lang"] = str(rows[0].get("status", "")).upper(), rows[0].get("language", "en_US")
        else:
            wa_call(d["token"], f"{d['waba']}/message_templates", method="POST", fields={
                "name": WA_TEMPLATE, "category": "UTILITY", "language": "en_US",
                "components": [{"type": "BODY", "text": WA_TEMPLATE_TEXT,
                                "example": {"body_text": [["Near 2nd UAV Squadron: China (at war): 1 Corvette at sea"]]}}]})
            d["tpl_status"], d["lang"] = "PENDING", "en_US"
            log("WhatsApp: asked Meta to approve the alert template")
        d["template"] = WA_TEMPLATE
    except RuntimeError as ex:
        d["tpl_error"] = str(ex)
    d["tpl_checked"] = time.time()
    wa_save(d)


def wa_setup(token, phone_id):
    token, phone_id = "".join(str(token or "").split()), re.sub(r"\D", "", str(phone_id or ""))
    if len(token) < 50:
        raise RuntimeError("That does not look like an access token. Press Generate API token on Meta's page and copy the whole long line.")
    if not phone_id:
        raise RuntimeError('The phone number ID is missing. It is the long number shown as "Phone number ID" on Meta\'s page.')
    try:
        info = wa_call(token, phone_id, {"fields": "display_phone_number,verified_name"})
    except RuntimeError as ex:
        log(f"WhatsApp setup failed: {ex}")
        raise RuntimeError("Could not connect: " + str(ex) + ".") from None
    d = {"token": token, "phone_id": phone_id, "from": info.get("display_phone_number", ""), "waba": "",
         "template": "", "lang": "en_US", "tpl_status": ""}
    try:                                                  # when the token runs out, and which business account it can manage
        about = wa_call(token, "debug_token", {"input_token": token}).get("data", {})
        d["expires"] = int(about.get("expires_at") or 0)
        for want in ("whatsapp_business_management", "whatsapp_business_messaging"):
            ids = [i for g in about.get("granular_scopes", []) if g.get("scope") == want for i in g.get("target_ids") or []]
            if ids:
                d["waba"] = str(ids[0])
                break
        if not d["waba"]:                                 # a system user with the account assigned to it
            got = wa_call(token, "me/assigned_whatsapp_business_accounts", {"fields": "id"}).get("data", [])
            if got:
                d["waba"] = str(got[0]["id"])
    except RuntimeError:
        pass
    if not d["waba"]:                                     # the sending number's health report names its account
        try:
            parts = wa_call(token, phone_id, {"fields": "health_status"}).get("health_status", {}).get("entities", [])
            d["waba"] = next((str(e["id"]) for e in parts if e.get("entity_type") == "WABA" and e.get("id")), "")
        except RuntimeError:
            pass
    wa_save(d)
    wa_template(d)
    log(f"WhatsApp: connected, sending from {d['from']}")
    return wa_state()


def wa_account(waba):
    """The business account id, typed in by hand when Meta would not tell which one the number belongs to."""
    d, waba = wa_load(), re.sub(r"\D", "", str(waba or ""))
    if not d.get("token"):
        raise RuntimeError("WhatsApp is not connected yet.")
    if not waba:
        raise RuntimeError("The WhatsApp Business Account ID is a long number.")
    try:
        nums = wa_call(d["token"], f"{waba}/phone_numbers", {"fields": "id,display_phone_number"}).get("data", [])
    except RuntimeError as ex:
        if "nonexisting field" in str(ex) or "does not exist" in str(ex):
            raise RuntimeError("That is not a WhatsApp Business Account ID. It may be the app ID or the phone number ID; "
                               "the account ID is a different long number.") from None
        raise RuntimeError(f"That account ID did not work: {ex}.") from None
    if d["phone_id"] not in [str(n.get("id")) for n in nums]:
        raise RuntimeError("That account exists but your sending number is not in it. Check that it is the WhatsApp Business Account ID, not the app ID or the phone number ID.")
    d["waba"] = waba
    wa_save(d)
    wa_template(d)
    return wa_state()


def wa_off():
    WA_FILE.unlink(missing_ok=True)
    r, a = recipients(), recipients(active=True)
    if not a["phones"] and not (email_load().get("on") and a["emails"]):       # never leave alerts with nowhere to go
        recipients_save([PHONE], r["emails"], r["sms"], r["whatsapp"], r["paused"])
    log("WhatsApp: switched off")
    return wa_state()


def wa_send(d, text, image, numbers):
    """Send to each number. Returns the numbers Meta accepted the message for."""
    if d.get("tpl_status") != "APPROVED" and time.time() - d.get("tpl_checked", 0) > 300:
        wa_template(d)                                    # has Meta approved the template by now?
    approved = d.get("template") and d.get("tpl_status") == "APPROVED"
    if d.get("expires") and 0 < d["expires"] - time.time() < 3 * 86400 and time.time() - d.get("warned", 0) > 86400:
        log(f"WhatsApp: the access token runs out on {dt.datetime.fromtimestamp(d['expires']):%-d %b}. Make a new one on Meta's page and connect again.")
        d["warned"] = time.time()
        wa_save(d)
    media = None
    if image and Path(image).exists():
        try:
            media = wa_call(d["token"], f"{d['phone_id']}/media", method="POST", upload=image, timeout=60,
                            fields={"messaging_product": "whatsapp", "type": "image/jpeg"}).get("id")
        except RuntimeError as ex:
            log(f"WhatsApp: the picture could not be uploaded ({ex})")
    body = " ".join(text.split())
    short = re.sub(r"^CON ALERT:\s*", "", body).rstrip(".")[:900]
    ok, results = [], {}
    for n in numbers:
        msg = {"messaging_product": "whatsapp", "to": wa_number(n).lstrip("+")}
        try:
            if approved:
                wa_call(d["token"], f"{d['phone_id']}/messages", method="POST", fields={**msg, "type": "template", "template": {
                    "name": d["template"], "language": {"code": d.get("lang", "en_US")},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": short}]}]}})
                if media:                                 # only arrives if they wrote to the number in the last 24 hours
                    try:
                        wa_call(d["token"], f"{d['phone_id']}/messages", method="POST", fields={**msg, "type": "image", "image": {"id": media}})
                    except RuntimeError:
                        pass
            elif media:
                wa_call(d["token"], f"{d['phone_id']}/messages", method="POST", fields={**msg, "type": "image", "image": {"id": media, "caption": body[:1000]}})
            else:
                wa_call(d["token"], f"{d['phone_id']}/messages", method="POST", fields={**msg, "type": "text", "text": {"body": body[:4000]}})
            ok.append(n)
            results[f"wa:{n}"] = {"state": "ok", "why": "", "how": "WhatsApp", "at": f"{dt.datetime.now():%H:%M}"}
        except RuntimeError as ex:
            log(f"WHATSAPP FAILED to {n}: {ex}")
            results[f"wa:{n}"] = {"state": "failed", "why": str(ex), "how": "WhatsApp", "at": f"{dt.datetime.now():%H:%M}"}
    try:
        STATE.mkdir(exist_ok=True)
        DELIVERY.write_text(json.dumps({**_read_json(DELIVERY, {}), **results}))
    except Exception:
        pass
    return ok


# -------------------------------------------------------------- recipients
# Who gets each alert: recipients.json = {"phones": [...], "sms": [...], "emails": [...]}. Numbers (or
# Apple ID addresses) get an iMessage, except the numbers also listed under "sms": those get a plain
# carrier text through the iPhone (for phones without iMessage; no picture). Email addresses get an email
# once the email account above is switched on.
# Without the file it is just the PHONE setting at the top, as before.
RECIPIENTS_FILE = ROOT / "recipients.json"
ADDRESS = r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+"


def recipients(active=False):
    """The lists as saved; with active=True, without the entries that are switched off."""
    try:
        d = json.loads(RECIPIENTS_FILE.read_text())
    except Exception:
        d = {}
    phones = d.get("phones") if isinstance(d.get("phones"), list) else [PHONE]
    emails = d.get("emails") if isinstance(d.get("emails"), list) else []
    sms = d.get("sms") if isinstance(d.get("sms"), list) else []
    wa = d.get("whatsapp") if isinstance(d.get("whatsapp"), list) else []
    paused = d.get("paused") if isinstance(d.get("paused"), dict) else {}
    out = {"phones": [str(p) for p in phones], "sms": [str(p) for p in sms if p in phones], "emails": [str(e) for e in emails],
           "whatsapp": [str(p) for p in wa],
           "paused": {k: [str(x) for x in paused.get(k, []) if x in lst] for k, lst in (("phones", phones), ("emails", emails), ("whatsapp", wa))}}
    if active:
        for k in ("phones", "emails", "whatsapp"):
            out[k] = [x for x in out[k] if x not in out["paused"][k]]
        out["sms"] = [x for x in out["sms"] if x in out["phones"]]
    return out


def recipients_save(phones, emails, sms=None, whatsapp=None, paused=None):
    """Check and store both lists. There must always be somewhere for an alert to go."""
    clean_p, clean_e = [], []
    for p in phones or []:
        p = str(p).strip()
        if not re.fullmatch(ADDRESS, p):                  # a phone number: keep + and digits
            digits = re.sub(r"[\s().-]", "", p)
            if not re.fullmatch(r"\+?\d{8,15}", digits):
                raise RuntimeError(f'"{p}" is not a phone number. Write it with the country code, like +61 400 000 000.')
            p = digits
        if p not in clean_p:
            clean_p.append(p)
    for e in emails or []:
        e = str(e).strip()
        if not re.fullmatch(ADDRESS, e):
            raise RuntimeError(f'"{e}" is not an email address.')
        if e.lower() not in [x.lower() for x in clean_e]:
            clean_e.append(e)
    clean_w = []
    for p in whatsapp or []:
        digits = re.sub(r"[\s().-]", "", str(p))
        if not re.fullmatch(r"\+?\d{8,15}", digits):
            raise RuntimeError(f'"{p}" is not a phone number. Write it with the country code, like +61 400 000 000.')
        digits = wa_number(digits)
        if digits not in clean_w:
            clean_w.append(digits)
    if len(clean_p) > 20 or len(clean_e) > 20 or len(clean_w) > 20:
        raise RuntimeError("That is too many: at most 20 of each.")
    paused = paused if isinstance(paused, dict) else {}
    norm = lambda p: re.sub(r"[\s().-]", "", str(p))
    off = {"phones": [p for p in clean_p if p in [norm(x) if "@" not in str(x) else str(x).strip() for x in paused.get("phones", [])]],
           "emails": [e for e in clean_e if e.lower() in [str(x).strip().lower() for x in paused.get("emails", [])]],
           "whatsapp": [p for p in clean_w if p in [wa_number(norm(x)) for x in paused.get("whatsapp", [])]]}
    on_p, on_e, on_w = ([x for x in lst if x not in off[k]] for k, lst in (("phones", clean_p), ("emails", clean_e), ("whatsapp", clean_w)))
    if not on_p and not (email_load().get("on") and on_e) and not (wa_load().get("token") and on_w):
        raise RuntimeError("Keep at least one number or address switched on, or alerts would have nowhere to go.")
    as_sms = [re.sub(r"[\s().-]", "", str(p)) for p in sms or []]
    as_sms = [p for p in clean_p if p in as_sms and "@" not in p]          # only numbers on the list can be SMS
    tmp = RECIPIENTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"phones": clean_p, "sms": as_sms, "emails": clean_e, "whatsapp": clean_w, "paused": off}, indent=2))
    tmp.replace(RECIPIENTS_FILE)
    return recipients()


def sends_to():
    r, parts = recipients(active=True), []
    im, sm = [p for p in r["phones"] if p not in r["sms"]], r["sms"]
    if im:
        parts.append(f"iMessage to {im[0]}" if len(im) == 1 else f"iMessage to {len(im)} numbers")
    if sm:
        parts.append(f"SMS to {sm[0]}" if len(sm) == 1 else f"SMS to {len(sm)} numbers")
    if email_load().get("on") and r["emails"]:
        parts.append(f"email to {r['emails'][0]}" if len(r["emails"]) == 1 else f"email to {len(r['emails'])} addresses")
    if wa_load().get("token") and r["whatsapp"]:
        parts.append(f"WhatsApp to {len(r['whatsapp'])} number{'s' if len(r['whatsapp']) > 1 else ''}")
    return " and ".join(parts) or f"iMessage to {PHONE}"


# Messages accepts a text even when it then fails to go out, so "sent" is checked afterwards: each text is
# noted in state/delivery-pending.json, and a little later the monitor looks in the Mac's own Messages
# records to see whether it really left (SMS) or was delivered (iMessage). The outcome per number is kept
# in state/delivery.json for the page.
DELIVERY_PENDING = STATE / "delivery-pending.json"
DELIVERY = STATE / "delivery.json"
CHAT_DB = Path.home() / "Library/Messages/chat.db"


def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def note_sent(to, sms):
    pending = _read_json(DELIVERY_PENDING, [])
    pending.append({"to": to, "sms": bool(sms), "at": time.time()})
    STATE.mkdir(exist_ok=True)
    DELIVERY_PENDING.write_text(json.dumps(pending[-60:]))


def check_deliveries(wait=45):
    """Look up texts sent at least `wait` seconds ago. Logs the ones that did not get through."""
    pending = _read_json(DELIVERY_PENDING, [])
    due = [p for p in pending if time.time() - p["at"] >= wait]
    if not due:
        return
    import sqlite3
    results = _read_json(DELIVERY, {})
    try:
        db = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True, timeout=5)
    except Exception:
        db = None
    for p in due:
        rows = []
        if db:
            try:
                rows = db.execute(
                    "select m.service, m.is_sent, m.is_delivered, m.error from message m join handle h on h.rowid = m.handle_id "
                    "where m.is_from_me = 1 and (h.id = ? or h.id like ?) and m.date / 1000000000 + 978307200 >= ?",
                    (p["to"], "%" + re.sub(r"\D", "", p["to"])[-9:] if "@" not in p["to"] else p["to"], p["at"] - 3)).fetchall()
                # (Messages stores numbers with the country code, so a number typed as 04... is matched by its last digits)
            except Exception:
                rows = []
        how = "SMS" if p["sms"] else "iMessage"
        if not rows:
            continue                                      # cannot tell (no access to the records): say nothing
        if any(r[3] for r in rows):
            state, why = "failed", f"Messages could not send it (error {next(r[3] for r in rows if r[3])})"
        elif p["sms"]:
            state, why = ("ok", "") if all(r[1] for r in rows) else ("failed", "it never left the Mac")
        else:
            state, why = ("ok", "") if all(r[2] for r in rows) else ("unconfirmed", "not delivered yet: the phone may be off, or it has no iMessage")
        if state != "ok" and results.get(p["to"], {}).get("state") != state:
            log(f"NOT DELIVERED: {how} to {p['to']}: {why}")
        results[p["to"]] = {"state": state, "why": why, "how": how, "at": f"{dt.datetime.fromtimestamp(p['at']):%H:%M}"}
    if db:
        db.close()
    DELIVERY.write_text(json.dumps(results))
    DELIVERY_PENDING.write_text(json.dumps([p for p in _read_json(DELIVERY_PENDING, []) if p not in due]))


def imessage(to, text, image=None, sms=False):
    """A text through the Mac's Messages app: an iMessage (with the picture), or a plain carrier SMS."""
    args = ["osascript", "-e", SEND, to, text, "SMS" if sms else "iMessage"]
    if image and not sms:
        image = Path(image)
        if image.parent != ALERTS:              # Messages cannot read files outside ~/Pictures
            ALERTS.mkdir(parents=True, exist_ok=True)
            image = Path(shutil.copy(image, ALERTS / image.name))
        args.append(str(image))
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        log(f"SEND FAILED ({'SMS' if sms else 'iMessage'} to {to}): {r.stderr.strip()}")
        return False
    note_sent(to, sms)
    return True


def send(text, image=None):
    """Send to everyone on both lists. True if at least one of them got it."""
    if DRY_RUN:
        log(f"[dry run] would send: {text}" + (f"  [{Path(image).name}]" if image else ""))
        return True
    r, em, done = recipients(active=True), email_load(), []
    pic = f"  [{Path(image).name}]" if image else ""
    if em.get("on") and r["emails"]:
        try:
            email_send(em, text, image, r["emails"])
            done.append(f"email to {len(r['emails'])}")
        except RuntimeError as ex:
            log(f"EMAIL FAILED ({ex})")
    wa = wa_load()
    if wa.get("token") and r["whatsapp"]:
        got = wa_send(wa, text, image, r["whatsapp"])
        if got:
            done.append(f"WhatsApp to {len(got)}")
    phones = list(r["phones"])
    if not done and not phones:                           # nothing else got through
        log(f"sending by iMessage to {PHONE} instead")
        phones = [PHONE]
    got = [p for p in phones if imessage(p, text, image, sms=p in r["sms"])]
    n_sms = len([p for p in got if p in r["sms"]])
    if len(got) - n_sms:
        done.append(f"iMessage to {len(got) - n_sms}")
    if n_sms:
        done.append(f"SMS to {n_sms}")
    if not done:
        return False
    log(f"sent ({', '.join(done)}): {text}{pic}")
    return True


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
            if st == "ok" and SOURCE == "viewer" and not TEST_IMAGE:
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
            e = edge_maps(img) if st == "ok" else None
            data = game_data() if st == "ok" and DETECT == "data" and SOURCE == "viewer" and not TEST_IMAGE else None
            if DETECT == "data" and st == "ok" and not TEST_IMAGE and bool(data) != had_data:
                had_data = bool(data)
                log("reading units straight from the game" if data else
                    "the game's unit data cannot be read: falling back to looking at pictures")
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
                if data and not spot.get("world") and spot.get("ref"):   # a spot made before data mode: convert it once
                    o = observe(img, e, spot)
                    if o["state"] == "ok":
                        spot["world"] = to_world(list(o["det"]["zone"]), data["view"], img.shape[1] / data["view"]["w"])
                        save_spots([{**x, "world": spot["world"]} if x["id"] == spot["id"] else x for x in load_spots()])
                        log(f"[{spot['name']}] now tied to the map itself")
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
                if not spot.get("ref"):                       # made on the page's map: no picture fallback
                    reason = st if st != "ok" else "nodata"
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
                    continue
                sig = (spot["ref"], tuple(spot["zone"]))
                m = mem.get(spot["id"])
                if not m or m["sig"] != sig:              # new or moved spot: start fresh
                    m = mem[spot["id"]] = {"sig": sig, "alert": False, "last_alert": 0.0, "clear": 0,
                                           "nonship": (0.0, ""), "blind_since": None, "state": None}
                obs = observe(img, e, spot) if st == "ok" else {"state": st}
                if obs["state"] != m["state"]:
                    log(f"[{spot['name']}] " + ("watching" if obs["state"] == "ok"
                                                else f"cannot watch: {BLIND_REASONS.get(obs['state'], obs['state'])}"))
                    m["state"] = obs["state"]
                if obs["state"] != "ok":
                    m["blind_since"] = m["blind_since"] or now
                else:
                    m["blind_since"] = None
                    obs, _ = watch_spot(spot, m, obs, img, now)
                report[spot["id"]] = {
                    "name": spot["name"], "state": obs["state"], "alert_active": m["alert"],
                    "red_pixels": obs.get("det", {}).get("count"), "match": round(obs.get("score", 0), 2)}

            # the hidden map is not where it should be (a pop-up swallowed a move, the game reloaded
            # itself, ...): reopen the game, which puts the map back on the watched view
            lost = [i for i in names if mem[i]["state"] == "moved" and mem[i]["blind_since"]
                    and now - mem[i]["blind_since"] > 120]
            if SOURCE == "viewer" and spots and len(lost) == len(spots) and now - clock[2] > 600:
                clock[2] = now
                log("hidden game window: no spot is in view, reopening the game to put the map back")
                viewer_open_game()

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
                    f"{r['name']}: {r['state']} " + (f"enemies={r['enemies']}" if r.get("mode") == "data"
                                                    else f"red={r['red_pixels']} match={r['match']}")
                    for r in report.values()) or "no enabled spots")
                    + (f" | {len(data['enemies'])} foreign groups visible on the whole map" if data else ""))
                last_beat = now
        except SystemExit:
            raise
        except Exception as ex:             # never let one bad cycle kill the watcher
            log(f"error: {type(ex).__name__}: {ex}")
        time.sleep(INTERVAL)


def watch_spot_data(spot, m, data, now):
    """Data mode: text when an army is inside the spot that is not yours and not from a country you
    share intelligence with (at war or neutral). Returns how many are there."""
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
    ids = {e["id"] for e in here}
    new = ids - m["seen"]
    if new or now - m["last_alert"] > REALERT_MINUTES * 60:
        here.sort(key=lambda e: e["id"] not in new)       # picture the newcomer
        stamp = f"{dt.datetime.now():%Y%m%d-%H%M%S}"
        pic = enemy_picture(spot, here, ALERTS / f"{stamp}-{spot['id']}.jpg")
        log(f"[{spot['name']}] foreign units {'within sight of the troop' if spot.get('circle') else 'in spot'}: {describe(here)}")
        ok = send(f"CON ALERT: {spot['name']}: {describe(here)}{'' if new else ' - still there'} "
                  f"(game {GAME_ID}, {dt.datetime.now():%-I:%M%p %a %-d %b}).", pic)
        record(spot["name"], [(e["nation"] or ("unknown" if e.get("radar") else ""), relation_text(e),
                               radar_text(e) if e.get("radar") else ", ".join(f"{n} {name}" for name, n in e["units"]),
                               "air" if e["air"] else "sea" if e["sea"] else "land",
                               round(e["x"]), round(e["y"]), e.get("to", ""), e.get("eta") or "",
                               e.get("sight", "")) for e in here],
               "new" if new else "still there", ok, pic)
        m["seen"] |= ids
        m["alert"], m["last_alert"] = True, now
    return len(here)


def watch_spot(spot, m, obs, img, now):
    """Handle one visible spot: confirm a sighting, have it read, text if it is an enemy unit."""
    det = obs["det"]
    if det["spot"]:
        time.sleep(3)                           # must still be there 3 s later
        st, img = grab()
        obs = observe(img, edge_maps(img), spot) if st == "ok" else {"state": st}
        det = obs.get("det") or {"spot": None, "count": 0}
    if not det["spot"]:
        m["clear"] += 1
        if m["clear"] >= CLEAR_CHECKS:
            if m["alert"]:
                log(f"[{spot['name']}] clear again")
            m["alert"], m["nonship"] = False, (0.0, "")
        return obs, img
    m["clear"] = 0
    due = (not m["alert"]) or now - m["last_alert"] > REALERT_MINUTES * 60
    skip = m["nonship"][1] and now - m["nonship"][0] < RECLASSIFY_MINUTES * 60
    if due and not skip:
        verdict, summary = classify(img, det["spot"])
        stamp = f"{dt.datetime.now():%Y%m%d-%H%M%S}"
        pic = spot_picture(img, obs, ALERTS / f"{stamp}-{spot['id']}-{verdict.lower()}.jpg")
        log(f"[{spot['name']}] red seen ({det['count']} px), verdict {verdict}: {summary}")
        if verdict == "NONE" or (verdict == "AIRCRAFT" and SKIP_AIRCRAFT):
            m["nonship"] = (now, verdict)       # red, but not an enemy unit to report: no text
        else:
            what = f"Enemy at {spot['name']}" + (f": {summary}" if summary else " (could not read what it is)")
            again = " - still there" if m["alert"] else ""
            ok = send(f"CON ALERT: {what}{again} "
                      f"(game {GAME_ID}, {dt.datetime.now():%-I:%M%p %a %-d %b}).", pic)
            record(spot["name"], [("", "red marker seen in picture", summary or "not read", "", "", "", "", "", "")],
                   "still there" if m["alert"] else "new", ok, pic)
            m["alert"], m["last_alert"], m["nonship"] = True, now, (0.0, "")
    return obs, img


# ----------------------------------------------------------------- commands
def running_pid(pidfile=PIDFILE):
    try:
        pid = int(pidfile.read_text())
        os.kill(pid, 0)
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
        return pid if "con_alert" in cmd else None
    except Exception:
        return None


def spawn(command, outfile):
    LOG.parent.mkdir(exist_ok=True)
    out = open(LOG.parent / outfile, "a")
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), command],
                            stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)


def cmd_start():
    if running_pid():
        return f"already running (pid {running_pid()})"
    p = spawn("run", "monitor.out")
    time.sleep(1.5)
    return f"started (pid {p.pid}). Log: {LOG}"


def cmd_stop(pidfile=PIDFILE):
    pid = running_pid(pidfile)
    if not pid:
        return "not running"
    os.kill(pid, signal.SIGTERM)
    return f"stopped (pid {pid})"


def cmd_status():
    pid = running_pid()
    print(f"monitor running (pid {pid})" if pid else "monitor NOT running")
    if SOURCE == "viewer":
        v = viewer_pid()
        print(f"hidden game window running (pid {v})" if v else "hidden game window NOT running")
    ui = running_pid(UI_PIDFILE)
    print(f"web page running at http://127.0.0.1:{UI_PORT} (pid {ui})" if ui else "web page not running")
    if ui and TAILSCALE:
        for a in tailscale_addresses()[:2]:
            print(f"   on your other Tailscale devices: http://{a}:{UI_PORT}")
    if STATUSFILE.exists():
        print("last check:", STATUSFILE.read_text())
    if LOG.exists():
        print("--- last log lines ---")
        print("\n".join(LOG.read_text().splitlines()[-8:]))


def cmd_check(do_classify=False):
    st, img = grab()
    if st != "ok":
        print("CANNOT WATCH:", BLIND_REASONS.get(st, st))
        return None, []
    e = edge_maps(img)
    seen = []
    data = game_data() if DETECT == "data" and SOURCE == "viewer" and not TEST_IMAGE else None
    if data:
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
                print(f"[{spot['name']}] not yet tied to the map (the monitor converts it on its next check)")
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
    for spot in load_spots():
        obs = observe(img, e, spot)
        if obs["state"] != "ok":
            print(f"[{spot['name']}] CANNOT WATCH: {BLIND_REASONS['moved']}")
            continue
        det = obs["det"]
        pic = spot_picture(img, obs, STATE / f"check-{spot['id']}.jpg")
        print(f"[{spot['name']}] match {obs['score']:.2f}, red pixels: {det['count']}"
              f" -> {'RED MARKER at ' + str(det['spot']) if det['spot'] else 'clear'}"
              + ("" if spot.get("enabled", True) else "  (disabled)"))
        if det["spot"] and do_classify:
            print("   verdict: %s (%s)" % classify(img, det["spot"]))
        print("   picture:", pic)
        seen.append((spot, pic))
    return img, seen


def cmd_test():
    _, seen = cmd_check()
    if not seen:
        if not load_spots():
            return "No test text was sent: you have no spots yet. Add one and press Save changes first."
        return "No test text was sent: none of your spots can be watched right now (see their status in the Spots list)."
    spot, pic = seen[0]
    ok = send(f"CON alert test: watching {len(seen)} spot(s), e.g. {spot['name']} (game {GAME_ID}). "
              "You will get a text like this, with a picture, when foreign units enter a spot.", pic)
    return "Test text sent." if ok else "Sending failed - see the log."


def cmd_ui():
    if not running_pid(UI_PIDFILE):
        spawn("serve", "ui.out")
        time.sleep(1.5)
    url = f"http://127.0.0.1:{UI_PORT}/"
    # a NEW Safari window: opening it as a tab in the game's window would hide the game from the monitor
    subprocess.run(["osascript", "-e",
                    f'tell application "Safari" to make new document with properties {{URL:"{url}"}}'],
                   capture_output=True)
    return f"web page: {url}"


# ------------------------------------------------------------------- web UI
def tailscale_addresses():
    """This Mac's Tailscale IP and names ([] if Tailscale is not running)."""
    ts = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    try:
        ip = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=10).stdout.split()[0]
        me = json.loads(subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=10).stdout)["Self"]
        name = (me.get("DNSName") or "").rstrip(".")
        return [a for a in (ip, name, name.split(".")[0]) if a]
    except Exception:
        return []


def serve():
    import gzip
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    STATE.mkdir(exist_ok=True)
    UI_PIDFILE.write_text(str(os.getpid()))
    lock = threading.Lock()
    hosts = {f"127.0.0.1:{UI_PORT}", f"localhost:{UI_PORT}"}
    tailnet = tailscale_addresses() if TAILSCALE else []
    hosts |= {f"{a}:{UI_PORT}" for a in tailnet}

    def api_world():
        """Live data for the page's map: armies, province owners, relations, the saved spots."""
        st = "ok"
        with lock:
            if SOURCE != "viewer":
                st = "nodata"
            elif not viewer_pid():
                st = viewer_start()
            elif viewer_busy():
                st = "loading"
            data = world_data() if st == "ok" else None
            if st == "ok" and not data:
                st = "nodata" if viewer_js(HAS_MAP) == "yes" else "login"
            art = assets_info() if st == "ok" else {}
            if data:
                try:
                    data["ghosts"] = ghosts(data)
                except Exception as ex:
                    log(f"predictions failed: {type(ex).__name__}: {ex}")
                    data["ghosts"] = []
        return {"state": st, "message": BLIND_REASONS.get(st, ""), "world": data,
                "art": {"icons": art.get("icons", {}), "boom": art.get("boom", "")},
                "spots": [{"id": s["id"], "name": s["name"], "enabled": s.get("enabled", True),
                           "world": s.get("world"), "army": s.get("army"), "radius": s.get("radius")}
                          for s in load_spots()]}

    def api_monitor():
        status = {}
        try:
            status = json.loads(STATUSFILE.read_text())
        except Exception:
            pass
        tail = LOG.read_text().splitlines()[-14:] if LOG.exists() else []
        return {"running": bool(running_pid()), "status": status, "log": tail, "alerts": record_tail(8),
                "record_file": str(RECORD),
                "interval": INTERVAL, "phone": PHONE, "min_red": MIN_RED_PIXELS,
                "sends_to": sends_to(), "email": email_state(), "whatsapp": wa_state(), "recipients": recipients(),
                "delivery": _read_json(DELIVERY, {})}

    def api_save(body):
        """Spots are rectangles in map coordinates, [x, y, width, height], or follow a troop ("army": its id)."""
        with lock:
            old = {s["id"]: s for s in load_spots()}
            new = []
            for item in body["spots"]:
                keep = old.get(item.get("id"))
                if item.get("army"):                      # follows a troop instead of being a box on the map
                    spot = {"id": keep["id"] if keep else uuid.uuid4().hex[:8],
                            "name": (str(item.get("name") or "").strip() or "Near a troop")[:60],
                            "enabled": bool(item.get("enabled", True)), "army": int(item["army"])}
                    try:
                        if float(item.get("radius") or 0) > 0:
                            spot["radius"] = float(item["radius"])
                    except (TypeError, ValueError):
                        pass
                    new.append(spot)
                    continue
                w = item.get("world")
                if not (isinstance(w, list) and len(w) == 4):
                    continue
                w = [float(v) for v in w]
                if not all(np.isfinite(w)) or w[2] <= 0 or w[3] <= 0:
                    continue
                keep = old.get(item.get("id"))
                spot = {"id": keep["id"] if keep else uuid.uuid4().hex[:8],
                        "name": (str(item.get("name") or "").strip() or "Spot")[:60],
                        "enabled": bool(item.get("enabled", True)), "world": w}
                if keep and keep.get("ref") and keep.get("world") and max(abs(a - b) for a, b in zip(keep["world"], w)) < 0.01:
                    spot["ref"], spot["zone"] = keep["ref"], keep["zone"]      # unmoved: keep its picture fallback
                new.append(spot)
            save_spots(new)
            used = {s.get("ref") for s in new}
            for s in old.values():                        # drop reference pictures no longer used
                if s.get("ref") and s["ref"] not in used:
                    (ROOT / s["ref"]).unlink(missing_ok=True)
            return {"ok": True, "count": len(new)}

    packed_cache = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, body, ctype="application/json", keep=None, cache="no-store"):
            if not isinstance(body, bytes):
                body = json.dumps(body, separators=(",", ":")).encode()
            packed = len(body) > 1500 and "gzip" in (self.headers.get("Accept-Encoding") or "")
            if packed:
                if keep and packed_cache.get(keep, (None,))[0] == len(body):
                    body = packed_cache[keep][1]
                else:
                    n, body = len(body), gzip.compress(body, 6 if keep else 3)
                    if keep:
                        packed_cache[keep] = (n, body)
            self.send_response(code)
            if packed:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            # local page only: refuse other sites calling this server from the browser
            origin = self.headers.get("Origin")
            ok = self.headers.get("Host") in hosts and (not origin or origin.split("//")[-1] in hosts)
            if not ok:
                self.reply(403, {"error": "forbidden"})
            return ok

        def do_GET(self):
            if not self.allowed():
                return
            path = self.path.split("?")[0]
            try:
                if path == "/":
                    self.reply(200, (ROOT / "ui.html").read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/world":
                    self.reply(200, api_world())
                elif path == "/api/geo":
                    with lock:
                        g = geo_bytes()
                    self.reply(200, g, keep="geo") if g else self.reply(503, {"error": "The map is not available yet: the background game is not running."})
                elif path == "/api/monitor":
                    self.reply(200, api_monitor())
                elif path == "/api/asset":
                    from urllib.parse import parse_qs, urlparse
                    with lock:
                        a = asset_bytes((parse_qs(urlparse(self.path).query).get("p") or [""])[0])
                    self.reply(200, a, "image/png", cache="max-age=604800") if a else self.reply(404, {"error": "not found"})
                elif path == "/api/reachdemo":                  # page-only example: a made-up group's reachable roads
                    from urllib.parse import parse_qs, urlparse
                    q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                    sea = q.get("sea") == "1"
                    t = {"o": -1, "pos": [float(q.get("x", 0)), float(q.get("y", 0))], "spd": 2 if sea else 1.0,
                         "move": {"tbl": {"2": 1.5, "20": 2} if sea else {"0": 1, "3": 2.5, "12": 0.33, "13": 0.5, "14": 0.5, "15": 0.33, "16": 0.5, "21": 0.5},
                                  "ff": 1 if sea else 0.66, "tt": 20 if sea else 10, "own": False, "cls": "sea" if sea else "land"}}
                    with lock:
                        segs = reach_network(t, float(q.get("min", 60)), {})
                    self.reply(200, {"net": [round(v, 1) for v in segs]})
                elif path == "/apple-touch-icon.png":           # the icon for "Add to Home Screen" on a phone
                    f = ROOT / "state" / "apple-touch-icon.png"
                    if not f.exists():
                        from PIL import Image as _I, ImageDraw as _D
                        im = _I.new("RGB", (180, 180), (16, 22, 29)); d = _D.Draw(im)
                        d.ellipse([28, 28, 152, 152], outline=(245, 197, 24), width=8)
                        d.ellipse([58, 58, 122, 122], outline=(150, 125, 30), width=5)
                        d.pieslice([28, 28, 152, 152], -90, -30, fill=(90, 78, 30))
                        d.line([90, 90, 144, 59], fill=(245, 197, 24), width=8)
                        d.ellipse([105, 100, 131, 126], fill=(255, 93, 93))
                        im.save(f)
                    self.reply(200, f.read_bytes(), "image/png", cache="max-age=604800")
                elif path == "/api/history":
                    from urllib.parse import parse_qs, urlparse
                    q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                    self.reply(200, history_at(int(float(q["t"]))) if q.get("t") else history_index())
                elif path == "/api/activity":
                    w = None
                    try:
                        w = json.loads(viewer_js(WORLD_JS)) if viewer_pid() else None
                    except Exception:
                        w = None
                    self.reply(200, activity_summary((w or {}).get("players") or {}))
                elif path == "/api/province":
                    with lock:
                        self.reply(200, province_data(self.path.split("id=")[-1].split("&")[0]))
                else:
                    self.reply(404, {"error": "not found"})
            except Exception as ex:
                self.reply(500, {"error": f"{type(ex).__name__}: {ex}"})

        def do_POST(self):
            if not self.allowed():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/spots":
                    self.reply(200, api_save(body))
                elif self.path == "/api/monitor":
                    msg = cmd_start() if body.get("action") == "start" else cmd_stop()
                    time.sleep(0.5)
                    self.reply(200, {"message": msg, **api_monitor()})
                elif self.path == "/api/fix":
                    self.reply(200, {"message": fix_window()})
                elif self.path == "/api/viewer":
                    viewer_start()
                    act = body.get("action")
                    r = viewer_call(act if act in ("show", "hide", "toggle") else "hide") or {}
                    self.reply(200, {"message": "ok", "shown": bool(r.get("shown"))})
                elif self.path == "/api/test":
                    self.reply(200, {"message": cmd_test()})
                elif self.path == "/api/recipients":
                    try:
                        self.reply(200, recipients_save(body.get("phones"), body.get("emails"), body.get("sms"), body.get("whatsapp"), body.get("paused")))
                    except RuntimeError as ex:
                        self.reply(400, {"error": str(ex)})
                elif self.path == "/api/whatsapp":
                    try:
                        act = body.get("action")
                        if act == "setup":
                            self.reply(200, wa_setup(body.get("token"), body.get("phone_id")))
                        elif act == "off":
                            self.reply(200, wa_off())
                        elif act == "account":
                            self.reply(200, wa_account(body.get("waba")))
                        else:
                            if wa_load().get("token"):
                                wa_template(wa_load())
                            self.reply(200, wa_state())
                    except RuntimeError as ex:
                        self.reply(400, {"error": str(ex)})
                elif self.path == "/api/email":
                    try:
                        act = body.get("action")
                        self.reply(200, email_confirm() if act == "confirm" else email_off() if act == "off"
                                   else email_setup(body.get("from"), body.get("password"), body.get("to"), body.get("host"), body.get("port"))
                                   if act == "setup" else email_state())
                    except RuntimeError as ex:
                        self.reply(400, {"error": str(ex)})
                elif self.path == "/api/clientlog":
                    with open(LOG.parent / "ui.out", "a") as f:
                        f.write(f"{dt.datetime.now():%H:%M:%S} page: {json.dumps(body)[:2000]}\n")
                    self.reply(200, {"ok": True})
                else:
                    self.reply(404, {"error": "not found"})
            except Exception as ex:
                self.reply(500, {"error": f"{type(ex).__name__}: {ex}"})

    def stop(*_):
        UI_PIDFILE.unlink(missing_ok=True)
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if tailnet:                               # a second listener on the Tailscale address only (not the local network)
        try:
            threading.Thread(target=ThreadingHTTPServer((tailnet[0], UI_PORT), Handler).serve_forever, daemon=True).start()
        except OSError as ex:
            print(f"could not listen on Tailscale address {tailnet[0]}: {ex}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", UI_PORT), Handler).serve_forever()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        run()
    elif cmd == "serve":
        serve()
    elif cmd == "start":
        print(cmd_start())
    elif cmd == "stop":
        print(cmd_stop())
    elif cmd == "ui":
        print(cmd_ui())
    elif cmd == "viewer":
        sub = sys.argv[2] if len(sys.argv) > 2 else "status"
        if sub == "show":
            print(viewer_start())
            viewer_call("show")
        elif sub == "hide":
            viewer_call("hide")
        elif sub == "stop":
            print(viewer_stop())
        elif sub == "restart":
            print(viewer_stop())
            time.sleep(1)
            print(viewer_start())
        else:
            info = viewer_call("info", timeout=3) if viewer_pid() else None
            print("not running" if not info else
                  f"running: {'shown' if info['shown'] else 'hidden'}, game loaded: {viewer_js(HAS_MAP)}, "
                  f"restarts after crashes: {info['crashes']}, page: {info['url'][:70]}")
    elif cmd == "alerts":
        rows = record_tail(int(sys.argv[2]) if len(sys.argv) > 2 else 20)
        print(f"{RECORD}  ({'no alerts recorded yet' if not rows else 'newest first'})")
        for r in rows:
            print(f"{r['time']}  {r['spot']}: {r['country']} ({r['relation']}): {r['units']} [{r['where']}] {r['kind']}, text {r['text_sent']}")
    elif cmd == "ui-stop":
        print(cmd_stop(UI_PIDFILE))
    elif cmd == "status":
        cmd_status()
    elif cmd == "check":
        cmd_check("--classify" in sys.argv)
    elif cmd == "test":
        print(cmd_test())
    else:
        print(__doc__)
