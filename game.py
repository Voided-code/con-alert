"""The hidden game window (viewer.swift), the account, and reading the game's own data."""

import fcntl
import html
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid

import numpy as np
from PIL import Image, ImageDraw

from common import (CLIENT_URL_FILE, LOG, MAP_TOP, ROOT, STATE, VIEWER_BIN, VIEWER_PID, _read_json, cmd_start,
                    cmd_stop, config, config_save, game_id, game_url, log, running_pid)
# ----------------------------------------------------- hidden game window
# Finds the game client address (with its login tokens) in the Safari tab, if one is open.
SAFARI_SOURCE = """
on run argv
if application "Safari" is not running then return ""
tell application "Safari"
  repeat with w in windows
    repeat with t in tabs of w
      try
        if (URL of t) contains "gameID=" & (item 1 of argv) then return source of t
      end try
    end repeat
  end repeat
end tell
return ""
end run
"""

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
      let hp = null, mhp = null, uhp = [];
      try { hp = Math.round(a.hitPoints * 10) / 10; mhp = Math.round(a.maxHitPoints * 10) / 10;
            uhp = a.units.map(u => [u.size, Math.round(u.getHitPoints() * 10) / 10, Math.round(u.getMaxHitPoints() * 10) / 10]); } catch (e) {}
      enemies.push({id: a.unitUID, o: a.ownerID, nation, war, x: p.x, y: p.y, plane, move, ord, at, hp, mhp, uhp, fight: (() => { try { return !!(a.battle || a.isFighting() || a.fightStatus || (a.lastDamageTakenTime && Date.now() - a.lastDamageTakenTime < 600000)); } catch (e) { return false; } })(), knots: knots.length > 1 ? knots.map(k => [Math.round(k[0]), Math.round(k[1]), Math.round(k[2])]) : [],
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
        src = subprocess.run(["osascript", "-e", SAFARI_SOURCE, game_id()], capture_output=True, text=True, timeout=20).stdout
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

    # 1. the viewer's own login (saved in the window once you sign in there; it stays until you sign out)
    viewer_call("load", url=game_url())
    time.sleep(1)
    viewer_wait(loaded, 30)
    if "play.php" in ((viewer_call("info") or {}).get("url") or ""):
        src = viewer_js("(document.querySelector('iframe[src*=\"con-client\"]') || {}).src || ''")
        if src and try_client(src):
            log("hidden game window: opened the game with its own login")
            remember_account("own")
            return "ok"
    # 2. borrow the session of a Safari game tab (fresh if it is open, else the last one saved) -
    #    but not after you logged out on the page, until you log in again
    if (STATE / "logged-out").exists():
        log("hidden game window: logged out")
        return "login"
    for url in (safari_client_url(), CLIENT_URL_FILE.read_text() if CLIENT_URL_FILE.exists() else ""):
        if url and f"gameID={game_id()}" in url and try_client(url):
            log("hidden game window: opened the game with the session from Safari")
            remember_account("safari")
            return "ok"
    log("hidden game window: not logged in")
    return "login"


# ------------------------------------------------------------------ account
# You sign in to Conflict of Nations once, in the background game window itself (on the game's own site:
# your password never passes through this tool). The window keeps that login like a browser does, until you
# sign out. The game id can be changed from the page; each game's spots, history and records are kept apart.
ACCOUNT_JS = """(function(){ try { const ps = hup.gameState.getPlayerState(), p = ps.getPlayerByUserID(hup.user.id);
  return JSON.stringify({uid: String(hup.user.id), name: p ? String(p.userName || p.name || '') : '', nation: p ? String(p.nationName || '') : '',
                         game: String(hup.gameServer && hup.gameServer.gameID || '')}); } catch (e) { return ''; } })()"""
GAME_FILES = ["spots.json", "state/alerted.json", "state/tracks.json", "state/activity.jsonl", "state/activity-hours.json",
              "state/activity-routes.json", "state/history", "state/client_url.txt"]


def remember_account(how):
    """After the game opened: note who is signed in and how, and keep the user id for next time."""
    try:
        a = json.loads(viewer_js(ACCOUNT_JS) or "{}")
    except Exception:
        a = {}
    a["how"], a["at"] = how, round(time.time())
    (STATE / "account.json").write_text(json.dumps(a))
    if a.get("uid") and a["uid"] != str(config().get("user_id", "")):
        config_save(user_id=a["uid"])


def account_state():
    a = _read_json(STATE / "account.json", {})
    c = config()
    return {"game_id": str(c.get("game_id", "")), "uid": a.get("uid") or str(c.get("user_id", "")), "name": a.get("name", ""),
            "nation": a.get("nation", ""), "how": a.get("how", ""), "signing_in": signing_in()}


def signing_in():
    """A log-in is under way only while its window is on screen (and for at most half an hour)."""
    s = _read_json(STATE / "signing-in.json", {})
    if not s:
        return False
    info = viewer_call("info", timeout=3) if viewer_pid() else None
    if time.time() - s.get("at", 0) > 1800 or not (info and info.get("shown")):
        (STATE / "signing-in.json").unlink(missing_ok=True)   # left half-way: forget it
        return False
    return True


def account_signin():
    """Show the game's own sign-in page in the background window."""
    viewer_start()
    (STATE / "signing-in.json").write_text(json.dumps({"at": round(time.time())}))
    viewer_call("load", url="https://www.conflictnations.com/")
    viewer_call("show")
    return account_state()


def account_done():
    """Signed in: hide the window again and open the game with that login."""
    (STATE / "signing-in.json").unlink(missing_ok=True)
    (STATE / "logged-out").unlink(missing_ok=True)
    viewer_call("hide")
    r = viewer_open_game()
    st = account_state()
    st["result"] = r
    return st


def account_signout():
    viewer_start()
    viewer_call("signout", timeout=30)
    CLIENT_URL_FILE.unlink(missing_ok=True)
    (STATE / "account.json").unlink(missing_ok=True)
    (STATE / "logged-out").write_text(str(round(time.time())))   # do not fall back on Safari's login either
    log("signed out of the game in the background window")
    return account_state()


def switch_game(new_id):
    """Watch a different game: put the current game's spots, history and records aside (they come back if you
    switch back), then open the new game."""
    new_id = re.sub(r"\D", "", str(new_id or ""))
    if not new_id:
        raise RuntimeError("The game id is the number after gameID= in the game's address.")
    old_id = game_id()
    if new_id == old_id:
        return account_state()
    keep = STATE / "games"
    for name in GAME_FILES:                               # this game's files aside...
        f = ROOT / name
        if f.exists():
            dest = keep / old_id / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
            shutil.move(str(f), str(dest))
    for name in GAME_FILES:                               # ...and the new game's back, if it was watched before
        f = keep / new_id / name
        if f.exists():
            (ROOT / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(ROOT / name))
    from activity import _orders, _still                # (imported here: those modules import this one)
    from history import _hist
    _orders.clear(); _still.clear(); _hist.update({"file": None, "own": None, "pl": None})
    config_save(game_id=new_id)
    log(f"switched from game {old_id} to game {new_id}")
    running = running_pid()
    if running:
        cmd_stop()
    r = viewer_open_game() if viewer_pid() else viewer_start()
    if running:
        cmd_start()
    st = account_state()
    st["result"] = r
    return st


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
    // healing per province (hit points a day): from its terrain and buildings such as a military hospital
    const heal = [];
    let li = 0;
    for (const l of gs.getMapState().map.locations) {
      if (!l || !l.borders || !('ownerID' in l)) continue;
      const r = T(() => l.getHealingRate()) || 0;
      if (r > 0) heal.push([li, Math.round(r * 100) / 100]);
      li++;
    }
    return JSON.stringify({ok: true, me, players, owners, battles, armies, airfields, heal});
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
    # zoom so the picture shows the spot and the enemy closely: the box (or troop circle) and the enemy
    # fill about half the picture; never further out than the whole region nor closer than the game allows
    sx_, sy_, sw, sh = spot["world"]
    ex_ = sx_ + ((e["x"] - sx_ + 7696) % 15393 - 7696)    # the enemy on the same side of the map edge
    x0_, x1_ = min(sx_, ex_), max(sx_ + sw, ex_)
    y0_, y1_ = min(sy_, e["y"]), max(sy_ + sh, e["y"])
    want = min(620 / max(30.0, (x1_ - x0_) * 1.5), 470 / max(30.0, (y1_ - y0_) * 1.5))
    want = max(0.45, min(2.5, want))
    cx, cy = (x0_ + x1_) / 2, (y0_ + y1_) / 2
    moved = True
    try:
        st, img0 = grab_viewer()                          # the map as it was, to check the game redraws it
        for attempt in range(2):
            view_set(0, 0, want)                          # zoom first: the visible area depends on it
            time.sleep(0.3)
            z = view_get() or view
            view_set(cx - z["w"] / z["scale"] / 2, cy - (z["h"] + MAP_TOP / pix) / z["scale"] / 2)
            time.sleep(1.5)
            st, img = grab_viewer()
            if st != "ok" or img0 is None or img.shape != img0.shape or \
                    np.abs(img[MAP_TOP + 100:].astype(np.int16)[::8, ::8] - img0[MAP_TOP + 100:].astype(np.int16)[::8, ::8]).mean() > 2:
                break
            # the picture did not change although the map was moved: the game has stopped drawing its map
            log("alert picture: the hidden game's map is not redrawing, reopening the game")
            viewer_open_game()
            time.sleep(2)
        now = view_get()
        if st != "ok" or not now:
            return None
        px, py = [int(v) for v in to_picture(e["x"], e["y"], now, pix)]
        zx, zy = [int(v) for v in to_picture(spot["world"][0], spot["world"][1], now, pix)]
        k = now["scale"] * pix
        mx, my = [int(v) for v in to_picture(cx, cy, now, pix)]   # the middle of spot and enemy
        cw, ch = min(img.shape[1], 1300), min(img.shape[0] - MAP_TOP, 1000)
        x0 = max(0, min(img.shape[1] - cw, mx - cw // 2))
        y0 = max(MAP_TOP, min(img.shape[0] - ch, my - ch // 2))
        obs = {"pos": (x0, y0), "size": (cw, ch),
               "det": {"zone": (zx, zy, int(spot["world"][2] * k), int(spot["world"][3] * k)), "spot": (px, py),
                       "circle": bool(spot.get("circle"))}}      # a spot that follows a troop is its sight circle
        return spot_picture(img, obs, path)
    except Exception as ex:
        log(f"picture failed: {type(ex).__name__}: {ex}")
        return None
    finally:
        if moved:                                         # back to the zoom and place it had
            view_set(0, 0, view["scale"])
            time.sleep(0.2)
            view_set(view["x"], view["y"])


# ------------------------------------------------------------------ capture
def fix_window():
    if viewer_start() == "ok" and viewer_js(HAS_MAP) == "yes":
        return "The background game is running."
    if viewer_open_game() == "ok":
        return "The background game is running again."
    viewer_call("show")
    return "Log in to the game in the window that just opened, then close that window."


grab = grab_viewer               # picture of the hidden game: ("ok", image) or (reason, None)


# ------------------------------------------------------------------ pictures
def spot_picture(img, obs, path):
    """Picture of the area around a spot: box outlined, sighting circled, zoom inset."""
    fx, fy = obs["pos"]
    fw, fh = obs["size"]
    det = obs["det"]
    pic = Image.fromarray(img[fy:fy + fh, fx:fx + fw]).convert("RGB")
    d = ImageDraw.Draw(pic)
    zx, zy, zw, zh = det["zone"]
    (d.ellipse if det.get("circle") else d.rectangle)([zx - fx, zy - fy, zx - fx + zw, zy - fy + zh], outline=(255, 220, 0), width=3)
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
