"""Shared settings, file locations, the log, saved spots and starting/stopping the monitor."""

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


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
CONFIG_FILE = Path(__file__).resolve().parent / "config.json"


def config():
    """The settings as saved now (the page can change them while the tool runs)."""
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def config_save(**changes):
    d = config()
    d.update({k: v for k, v in changes.items() if v is not None})
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(CONFIG_FILE)
    return d


def game_id():
    return str(config().get("game_id", GAME_ID) or "")


def game_url():
    """The game's address. The user id is added once it is known (it is read from the game after signing in)."""
    c = config()
    uid = f"&uid={c['user_id']}" if c.get("user_id") else ""
    return f"https://www.conflictnations.com/play.php?bust=1{uid}&gameID={c.get('game_id', GAME_ID)}&source=browser-desktop"
INTERVAL = 20                   # seconds between checks
CLEAR_CHECKS = 6                # clear checks in a row before a new sighting counts as new
BLIND_NOTIFY_MINUTES = 10       # text once if a spot cannot be seen for this long
RADAR_ALERTS = True             # also alert on radar-only contacts (something your radar sees but cannot identify)
SKIP_AIRCRAFT = False           # True: do not text when the only enemies in a spot are aircraft
UI_PORT = 8765                  # web page: http://127.0.0.1:8765
TAILSCALE = True                # also serve the page to your other devices on your Tailscale network
STALE_MINUTES = 6               # the hidden game's clock must change at least this often
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
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
MAP_TOP = 88                    # picture rows above this are toolbars, not map
VIEWER_BIN = ROOT / "conviewer"
VIEWER_PID = STATE / "viewer.pid"
CLIENT_URL_FILE = STATE / "client_url.txt"
# test hooks: CON_ALERT_DRYRUN=1 logs instead of texting, CON_ALERT_IMAGE=file replaces the screenshot
DRY_RUN = bool(os.environ.get("CON_ALERT_DRYRUN"))
INTERVAL = int(os.environ.get("CON_ALERT_INTERVAL", INTERVAL))

BLIND_REASONS = {
    "nocapture": "the screen could not be captured (Mac locked or display asleep?)",
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
# A spot is {"id", "name", "enabled"} plus either "world": [x, y, w, h], a box in map coordinates,
# or "army": the id of one of your troops (watched within its sight, "radius" overriding it).
def load_spots():
    try:
        return json.loads(SPOTS_FILE.read_text())["spots"]
    except FileNotFoundError:
        return []


def save_spots(spots):
    tmp = SPOTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"spots": spots}, indent=2))
    tmp.replace(SPOTS_FILE)                               # atomic: the monitor reads this file


def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


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
    return subprocess.Popen([sys.executable, str(ROOT / "con_alert.py"), command],
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
