#!/usr/bin/env python3
"""CON alert - watches spots on a Conflict of Nations map and sends you a message
when foreign units enter one of them.

How it works, every INTERVAL seconds:
  1. Keeps the game running in its own invisible background window (the "viewer",
     viewer.swift), so no Safari tab is needed and nothing appears on screen.
  2. Takes a picture of that hidden game.
  3. Reads the game's own list of units from that hidden game: every army you can see, who owns
     it, whether you are at war with them, where it is on the map and what it is made of.
  4. Any army inside a spot is an alert unless it is yours or belongs to a country you share
     intelligence with; countries at war with you and neutral ones both count.
  5. Sends a message (Messages, email, WhatsApp) saying what it is, with a picture.

The code is split by job: monitor.py (the watch loop), game.py (hidden game window, reading the game),
predict.py, activity.py, history.py, delivery.py (Messages, email, WhatsApp), server.py (the page),
common.py (settings, files, log). Tests are in tests/.

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
  ./conalert test       send a test alert with a picture of your spots
  ./conalert alerts     show the record of alerts sent (kept in alerts.csv)
"""

import subprocess
import sys
import time


from common import (LOG, RECORD, STATUSFILE, TAILSCALE, UI_PIDFILE, UI_PORT, cmd_start, cmd_stop, running_pid,
                    spawn, tailscale_addresses)
from delivery import record_tail
from game import (HAS_MAP, viewer_call, viewer_js, viewer_pid, viewer_start, viewer_stop)
from monitor import (cmd_check, cmd_test, run)
from server import serve
def cmd_status():
    pid = running_pid()
    print(f"monitor running (pid {pid})" if pid else "monitor NOT running")
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
        cmd_check()
    elif cmd == "test":
        print(cmd_test())
    else:
        print(__doc__)
