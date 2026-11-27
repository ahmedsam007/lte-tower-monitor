"""Activity log: what Tower Monitor did and what went wrong, in plain words.

Kept in activity_log.jsonl in the settings folder, one JSON object per line, for 14 days
(at most 20,000 entries). The /log page shows it and can copy a report to send to whoever
helps you. Passwords are never written here.

Each entry: {"t": epoch, "area": "app" | "setup" | "router" | "lock" | "devices" | "hotspot",
             "level": "info" | "change" | "problem", "text": "...", "detail": "..." (optional)}
The page translates "text" with the same English/Arabic table as the other pages.
"""
import json
import os
import platform
import sys
import threading
import time
import traceback
from collections import deque

from version import VERSION

DATA = os.environ.get("TOWER_MONITOR_DATA") or os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(DATA, "activity_log.jsonl")
KEEP_SECONDS = 14 * 86400
MAX_ENTRIES = 20000
REPEAT_QUIET = 600          # the same problem again within 10 min is counted, not written again
AREAS = ("app", "setup", "router", "lock", "devices", "hotspot")

_LOCK = threading.Lock()
_ENTRIES = deque(maxlen=MAX_ENTRIES)
_LAST = {}                  # (area, text) -> [time last written, times seen since]
_loaded = False


def _load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    cutoff = time.time() - KEEP_SECONDS
    kept = []
    try:
        with open(PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and isinstance(e.get("t"), (int, float)) and e["t"] >= cutoff:
                    kept.append(e)
    except OSError:
        return
    _ENTRIES.extend(kept[-MAX_ENTRIES:])
    if len(kept) > MAX_ENTRIES or len(kept) < _count_lines():
        _rewrite()


def _count_lines():
    try:
        with open(PATH, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _rewrite():
    try:
        os.makedirs(DATA, exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in _ENTRIES:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, PATH)
    except OSError:
        pass


def add(area, text, level="info", detail=None, quiet_key=None):
    """Record one thing. Repeats of the same problem within 10 minutes are only counted."""
    now = time.time()
    entry = {"t": round(now, 1), "area": area if area in AREAS else "app", "level": level, "text": str(text)}
    if detail:
        entry["detail"] = str(detail)[:8000]
    with _LOCK:
        _load()
        if level == "problem":
            key = (entry["area"], quiet_key or entry["text"])
            last = _LAST.get(key)
            if last and now - last[0] < REPEAT_QUIET:
                last[1] += 1
                return
            if last and last[1]:
                entry["repeats"] = last[1]       # how often it happened since it was last written
            _LAST[key] = [now, 0]
        _ENTRIES.append(entry)
        try:
            os.makedirs(DATA, exist_ok=True)
            with open(PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if len(_ENTRIES) == MAX_ENTRIES and _count_lines() > MAX_ENTRIES * 1.2:
            _rewrite()


def resolved(area, quiet_key):
    """A problem went away: the next time it happens it is written at once."""
    with _LOCK:
        _LAST.pop((area, quiet_key), None)


def entries(since=0):
    with _LOCK:
        _load()
        return [e for e in _ENTRIES if e["t"] > since]


def clear():
    with _LOCK:
        _load()
        _ENTRIES.clear()
        _LAST.clear()
        try:
            open(PATH, "w", encoding="utf-8").close()
        except OSError:
            pass
    add("app", "Activity log cleared")


def about():
    """Facts for the top of a copied report (no passwords, no addresses outside the home network)."""
    frozen = getattr(sys, "frozen", False)
    system = platform.system()
    if system == "Darwin":
        system_text = "macOS " + (platform.mac_ver()[0] or "?")
    elif system == "Windows":
        rel, ver = platform.release(), platform.version()
        try:
            build = int(ver.split(".")[-1])
            if rel == "10" and build >= 22000:
                rel = "11"
        except ValueError:
            pass
        system_text = f"Windows {rel} ({ver})"
    else:
        system_text = f"{system} {platform.release()}"
    return {
        "version": VERSION,
        "system": system_text,
        "machine": platform.machine(),
        "python": platform.python_version(),
        "kind": "app" if frozen else "script",
        "data_dir": DATA,
    }


def started():
    a = about()
    add("app", f"Tower Monitor {a['version']} started", "info",
        f"{a['system']} · {a['machine']} · Python {a['python']} · {a['kind']}")


def problem_from_exception(area, text, exc=None):
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else traceback.format_exc()
    add(area, text, "problem", detail.strip())


def catch_crashes():
    """Unexpected errors anywhere (main code or a background thread) go into the log."""
    old_hook = sys.excepthook

    def hook(kind, value, tb):
        if not issubclass(kind, (KeyboardInterrupt, SystemExit)):
            add("app", "Tower Monitor hit an unexpected error", "problem",
                "".join(traceback.format_exception(kind, value, tb)).strip())
        old_hook(kind, value, tb)
    sys.excepthook = hook

    old_thread_hook = threading.excepthook

    def thread_hook(args):
        if not issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            name = args.thread.name if args.thread else "?"
            add("app", "Tower Monitor hit an unexpected error", "problem",
                f"thread {name}\n" + "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                                     args.exc_traceback)).strip())
        old_thread_hook(args)
    threading.excepthook = thread_hook


def open_folder():
    """Show the settings folder (with activity_log.jsonl) in Explorer or Finder."""
    import subprocess
    try:
        if os.name == "nt":
            os.startfile(DATA)  # noqa: S606  (a local folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", PATH if os.path.exists(PATH) else DATA])
        else:
            subprocess.Popen(["xdg-open", DATA])
        return True
    except OSError:
        return False
