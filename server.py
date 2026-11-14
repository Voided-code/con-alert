"""The web page server (ui.html, ui.css, ui.js and the /api/ endpoints)."""

import datetime as dt
import json
import os
import signal
import time
import uuid

import numpy as np

from common import (BLIND_REASONS, config_save, INTERVAL, LOG, PHONE, RECORD, ROOT, STATE, STATUSFILE, TAILSCALE,
                    UI_PIDFILE, UI_PORT, _read_json, cmd_start, cmd_stop, load_spots, log, running_pid,
                    save_spots, tailscale_addresses)
from delivery import (DELIVERY, email_confirm, email_off, email_setup, email_state, recipients,
                      recipients_save, record_tail, sends_to, wa_account, wa_load, wa_off, wa_setup, wa_state,
                      wa_template)
from game import (HAS_MAP, WORLD_JS, account_done, account_signin, account_signout, account_state,
                  asset_bytes, assets_info, fix_window, geo_bytes, province_data, switch_game, viewer_busy,
                  viewer_call, viewer_js, viewer_pid, viewer_start, world_data)
from predict import (KIND_TEXT, ghosts, learn, min_chance, reach_network)
from activity import activity_summary
from history import (history_at, history_index)
from monitor import cmd_test
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
            if not viewer_pid():
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
                "interval": INTERVAL, "phone": PHONE,
                "predictions": {"record": learn()["record"], "checks": learn().get("checks", 0), "hide_below": round(min_chance() * 100),
                                "minutes": learn()["minutes"], "err": learn()["err"], "kinds": KIND_TEXT},
                "sends_to": sends_to(), "email": email_state(), "whatsapp": wa_state(), "recipients": recipients(), "account": account_state(),
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
                new.append(spot)
            save_spots(new)
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
                elif path in ("/ui.css", "/ui.js"):              # the page's styles and code
                    self.reply(200, (ROOT / path[1:]).read_bytes(),
                               "text/css; charset=utf-8" if path.endswith(".css") else "text/javascript; charset=utf-8")
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
                elif self.path == "/api/account":
                    try:
                        act = body.get("action")
                        with lock:
                            self.reply(200, account_signin() if act == "signin" else account_done() if act == "done"
                                       else account_signout() if act == "signout" else switch_game(body.get("game_id")) if act == "game"
                                       else account_state())
                    except RuntimeError as ex:
                        self.reply(400, {"error": str(ex)})
                elif self.path == "/api/settings":           # settings changed on the page
                    try:
                        n = int(body["hide_below"])
                    except (KeyError, TypeError, ValueError):
                        self.reply(400, {"error": "Give a percentage."})
                    else:
                        if not 5 <= n <= 95:
                            self.reply(400, {"error": "Pick between 5% and 95%."})
                        else:
                            config_save(hide_below=n)
                            log(f"predictions now hidden below {n}%")
                            self.reply(200, {"hide_below": n})
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
