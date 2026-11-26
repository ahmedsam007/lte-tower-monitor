#!/usr/bin/env python3
"""Start Tower Monitor: the setup page on the first start, then the dashboard.

In the Mac/Windows app it shows everything in its own window and keeps its settings and
logs in the user's application-data folder. Started as a script (start-mac.command,
start-windows.bat, or `python3 app.py`) it uses the web browser and keeps the files
next to the program, as before.

    python3 app.py              normal start
    python3 app.py --dry-run    try the buttons without changing the modem (port 8766)
"""
import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser

APP_NAME = "Tower Monitor"
FROZEN = getattr(sys, "frozen", False)      # running from the packaged app


def data_dir():
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "darwin":
        return os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
    return os.path.expanduser("~/.tower-monitor")


os.environ.setdefault("TOWER_MONITOR_DATA", data_dir())
os.makedirs(os.environ["TOWER_MONITOR_DATA"], exist_ok=True)

import firstrun  # noqa: E402  (needs TOWER_MONITOR_DATA set first)

DRY_RUN = "--dry-run" in sys.argv


def listen_port():
    cfg = firstrun.read_config() or {}
    try:
        port = int(cfg.get("listen_port", 8765))
    except (TypeError, ValueError):
        port = 8765
    return port + (1 if DRY_RUN else 0)


def answering(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url + "/api/setup/status", timeout=timeout) as r:
            return "running" in json.loads(r.read())
    except (OSError, ValueError):
        return False


def run_backend(show):
    """Setup if needed, then the dashboard. `show(url)` puts a page in front of the user."""
    port = listen_port()
    base = f"http://127.0.0.1:{port}"
    if firstrun.needed():
        firstrun.run_until_done(port, on_ready=show)
        port = listen_port()          # the saved settings may name another port
        base = f"http://127.0.0.1:{port}"
    else:
        threading.Thread(target=_show_when_up, args=(base, show), daemon=True).start()
    if "--no-open" not in sys.argv:
        sys.argv.append("--no-open")  # this file opens the page, not server.py
    import server
    server.main()


def _show_when_up(base, show):
    for _ in range(100):
        if answering(base, 0.5):
            return show(base + "/")
        time.sleep(0.2)
    show(base + "/")


def main_browser():
    """Script start: pages open in the web browser."""
    base = f"http://127.0.0.1:{listen_port()}"
    if answering(base):
        print(f"Tower Monitor is already running at {base}")
        webbrowser.open(base + "/")
        return
    def show(url):
        print(f"Tower Monitor: {url}", flush=True)
        if "--no-open" not in sys.argv:
            webbrowser.open(url)
    try:
        run_backend(show)
    except KeyboardInterrupt:
        print("\nStopped.")


def main_window(webview):
    """App start: pages show in Tower Monitor's own window. Closing the window quits."""
    base = f"http://127.0.0.1:{listen_port()}"
    window = webview.create_window(APP_NAME, html=LOADING, width=1220, height=860, min_size=(420, 560),
                                   text_select=True)

    def show(url):
        window.load_url(url)

    def backend():
        try:
            run_backend(show)
        except SystemExit as e:              # server.py stops with a message on a bad config.json
            window.load_html(error_page(str(e.code) if e.code not in (None, 0, 1) else LAST_ERROR[0]))
        except Exception as e:  # noqa: BLE001
            window.load_html(error_page(f"Tower Monitor could not start: {e}"))

    if answering(base):              # another copy is already serving: just show it
        threading.Thread(target=lambda: show(base + "/"), daemon=True).start()
    else:
        threading.Thread(target=backend, daemon=True).start()
    webview.start(private_mode=False)   # keep page storage between launches, such as the chosen language
    sys.exit(0)        # window closed: atexit handlers save the day's usage and close the SSH session


LOADING = """<!doctype html><meta charset="utf-8"><style>
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
font:15px system-ui,-apple-system,"Segoe UI",sans-serif;background:#f9f9f7;color:#52514e}
@media (prefers-color-scheme:dark){body{background:#0d0d0d;color:#c3c2b7}}</style>
<body>Starting Tower Monitor…</body>"""

LAST_ERROR = ["Tower Monitor stopped. Close it and open it again."]


def error_page(msg):
    import html
    return (LOADING.replace("Starting Tower Monitor…", "")
            .replace("<body>", "<body><div style='max-width:520px;padding:16px;white-space:pre-wrap'>"
                     + html.escape(msg) + "</div>"))


def _capture_stop_messages():
    """server.stop() prints its reason to stderr before exiting; keep it for the window."""
    class Tee:
        def __init__(self, inner):
            self.inner = inner

        def write(self, s):
            if s.strip():
                LAST_ERROR[0] = s.strip()
            try:
                return self.inner.write(s) if self.inner else len(s)
            except (OSError, ValueError):
                return len(s)

        def flush(self):
            try:
                self.inner and self.inner.flush()
            except (OSError, ValueError):
                pass
    sys.stderr = Tee(sys.stderr)


if __name__ == "__main__":
    try:
        import webview   # pywebview: only in the packaged app (or if you install it yourself)
    except ImportError:
        webview = None
    if webview and "--browser" not in sys.argv:
        _capture_stop_messages()
        main_window(webview)
    else:
        main_browser()
