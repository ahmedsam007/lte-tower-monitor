"""Pages that work both during setup and on the dashboard: the activity log and Wi-Fi sharing.

    /log          activity log page        /api/log, /api/log/clear, /api/log/folder
    /hotspot      share a public Wi-Fi     /api/hotspot/status, /start, /stop, /open
"""
import json
import os
from urllib.parse import parse_qs, urlparse

import activity
import firstrun
import hotspot

HERE = os.path.dirname(os.path.abspath(__file__))
PAGES = {"/log": "log.html", "/hotspot": "hotspot.html"}


def _send(handler, code, ctype, body):
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _json(handler, obj, code=200):
    _send(handler, code, "application/json", json.dumps(obj, ensure_ascii=False).encode("utf-8"))


_I18N_JS = None


def i18n_js():
    """The English/Arabic tables of the dashboard, devices and setup pages, for the log and hotspot
    pages: the log shows those pages' messages (lock steps, router and setup errors) too."""
    global _I18N_JS
    if _I18N_JS is None:
        parts = []
        for name in ("index.html", "devices.html", "setup.html"):
            try:
                with open(os.path.join(HERE, name), encoding="utf-8") as f:
                    src = f.read()
                a = src.index("window.I18N = ")
                b = src.index("/* Language: English or Arabic.", a)
            except (OSError, ValueError):
                continue
            # Each table runs with its own stand-in "window", so it cannot touch the page.
            parts.append("try { (function (window) { " + src[a:b] + "\n;TM_I18N_PARTS.push(window.I18N || {}); })({}); }"
                         " catch (e) { console.warn('translations from " + name + "', e); }")
        _I18N_JS = ("window.TM_I18N_PARTS = [];\n" + "\n".join(parts)).encode("utf-8")
    return _I18N_JS


def handle_get(handler):
    """Serve the page or API answer and return True, or return False if the path is not ours."""
    u = urlparse(handler.path)
    if u.path == "/i18n.js":
        _send(handler, 200, "text/javascript; charset=utf-8", i18n_js())
        return True
    if u.path in PAGES:
        with open(os.path.join(HERE, PAGES[u.path]), "rb") as f:
            _send(handler, 200, "text/html; charset=utf-8", f.read())
        return True
    if not (u.path.startswith("/api/log") or u.path.startswith("/api/hotspot/")):
        return False
    if not firstrun.same_origin(handler.headers):
        _send(handler, 403, "text/plain", b"forbidden")
        return True
    if u.path == "/api/log":
        try:
            since = float(parse_qs(u.query).get("since", ["0"])[0])
        except ValueError:
            since = 0
        _json(handler, {"about": activity.about(), "entries": activity.entries(since)})
    elif u.path == "/api/hotspot/status":
        _json(handler, hotspot.api("status", {}))
    else:
        _send(handler, 404, "text/plain", b"not found")
    return True


def handle_post(handler):
    u = urlparse(handler.path)
    if not (u.path.startswith("/api/log/") or u.path.startswith("/api/hotspot/")):
        return False
    # Only this computer's own page, with a JSON body (a form on another site cannot send that).
    if (not firstrun.same_origin(handler.headers)
            or "application/json" not in (handler.headers.get("Content-Type") or "")):
        _send(handler, 403, "text/plain", b"forbidden")
        return True
    try:
        n = int(handler.headers.get("Content-Length") or 0)
        body = json.loads(handler.rfile.read(n) or b"{}") if n <= 4096 else {}
        if not isinstance(body, dict):
            body = {}
    except ValueError:
        body = {}
    if u.path == "/api/log/clear":
        activity.clear()
        _json(handler, {"ok": True})
    elif u.path == "/api/log/folder":
        _json(handler, {"ok": activity.open_folder()})
    elif u.path.startswith("/api/hotspot/"):
        _json(handler, hotspot.api(u.path[len("/api/hotspot/"):], body))
    else:
        _send(handler, 404, "text/plain", b"not found")
    return True
