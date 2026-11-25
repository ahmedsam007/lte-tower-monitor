#!/usr/bin/env python3
"""Tower Monitor: a live dashboard for the LTE modem inside a D-Link DWR-921.

It logs in to the router over SSH every few seconds, asks the modem which tower it is
on and which other towers it can see, and serves a web page with the result.
Reading never changes anything. The modem is only changed when the user presses
"Lock" or "Remove lock" on the page (see the lock / unlock section below).

It talks to the router over the local network, so it keeps working while the
internet is down.

Run:  python3 server.py              then open http://127.0.0.1:8765
      python3 server.py --dry-run    same page on port 8766; lock commands are not sent
"""
import atexit
import json
import os
import re
import shutil
import signal
import socket
import statistics
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))       # program files (pages, example config)
DATA = os.environ.get("TOWER_MONITOR_DATA") or HERE       # settings and logs; the Mac/Windows app sets its own folder
os.makedirs(DATA, exist_ok=True)
LOG_PATH = os.path.join(DATA, "tower_log.csv")
LOC_PATH = os.path.join(DATA, "locations.json")   # coordinates the user typed in; never sent anywhere
MAX_SAMPLES = 20000          # about 28 hours at one sample every 5 seconds
MAX_BUCKETS = 360            # points sent to the page per chart
GAP_SECONDS = 60             # a silence longer than this is reported as a gap


def stop(msg):
    print("\n" + msg + "\n", file=sys.stderr)
    sys.exit(1)


try:
    import paramiko
except ImportError:
    stop("A required package is missing. Start Tower Monitor with start-windows.bat or start-mac.command,\n"
         "or install the packages yourself:  python3 -m pip install --only-binary=:all: -r requirements.txt")

import devices
import firstrun


def is_set(v):
    """A config value counts only when the user has filled it in."""
    return bool(str(v or "").strip()) and str(v).strip() != "CHANGE_ME"


def load_config():
    path = os.path.join(DATA, "config.json")
    if not os.path.exists(path):
        shutil.copy(os.path.join(HERE, "config.example.json"), path)
        stop(f"config.json was created at:\n  {path}\n"
             "Open it in a text editor, put in your router's address and password, save it, "
             "and start Tower Monitor again.")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except ValueError as e:
        stop(f"config.json is not valid JSON ({e}).\nCheck the commas and quotes, "
             "or delete it and start again to get a fresh copy.")
    try:      # settings missing from an older or hand-edited config.json get the example's value
        with open(os.path.join(HERE, "config.example.json"), encoding="utf-8") as f:
            for k, v in json.load(f).items():
                cfg.setdefault(k, v)
    except (OSError, ValueError):
        pass
    if not is_set(cfg.get("password")):
        stop("Put the D-Link router's password in config.json (the \"password\" line), "
             "then start Tower Monitor again.")
    return cfg


CFG = load_config()
POLL = max(3, int(CFG.get("poll_seconds", 5)))
DRY_RUN = "--dry-run" in sys.argv          # for trying the buttons: modem-changing commands are not sent
ACTION_LOG = os.path.join(DATA, "lock_actions.log")
REVERT_AFTER = max(120, int(CFG.get("lock_revert_seconds", 300)))   # no internet for this long after a lock: undo it
RECOVER_WAIT = 300                          # how long to wait for the internet after removing a lock
RESTART_WAIT = 900                          # how long to wait for the user to restart the router
RESTART_ASK = ("Now restart the router: use Reboot on its page (http://%s) or unplug its power for 10 seconds. "
               % CFG["host"])
NO_SERVICE_AFTER = 100                      # modem answering but not registered for this long: the locked tower is unusable
ROUTER_HINT_AFTER = 75                      # modem registered but the router still has no address: suggest RECONNECT
RECONNECT_HINT = ("On the router's page (http://%s, WAN), tick the mobile connection and press RECONNECT. "
                  "If that does not help, restart the router." % CFG["host"])

# ---------------------------------------------------------------- SSH plumbing
# One SSH connection is kept open and reused, the way the router's own tools would; it is
# made again when it drops. paramiko does the same on Windows, macOS and Linux.
class AcceptAnyKey(paramiko.MissingHostKeyPolicy):
    # This router makes a new SSH identity key on every boot (it is kept in /tmp), so the
    # key cannot be pinned. The connection is only ever made to the LAN address in config.json.
    def missing_host_key(self, client, hostname, key):
        pass


SSH = {"client": None}


def ssh_close():
    c, SSH["client"] = SSH["client"], None
    if c:
        try:
            c.close()
        except Exception:
            pass


def ssh_client():
    c = SSH["client"]
    if c and c.get_transport() and c.get_transport().is_active():
        return c
    ssh_close()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(AcceptAnyKey())
    c.connect(CFG["host"], port=int(CFG.get("ssh_port", 22)), username=CFG.get("user", "admin"),
              password=CFG["password"], timeout=20, banner_timeout=30, auth_timeout=20,
              look_for_keys=False, allow_agent=False)
    c.get_transport().set_keepalive(5)
    SSH["client"] = c
    return c


def ssh_run(command, timeout):
    """Run one command on the router. Returns (output, None) or (None, reason).
    The caller must hold SSH_LOCK."""
    try:
        _in, out, _err = ssh_client().exec_command(command, timeout=timeout)
        ch = out.channel
        deadline = time.time() + timeout
        data = b""
        while True:
            if ch.recv_ready():
                data += ch.recv(65536)
            elif ch.eof_received or ch.closed:      # all output has arrived
                break
            elif time.time() > deadline:
                ch.close()
                ssh_close()
                return None, "The router did not answer in time."
            else:
                time.sleep(0.05)
        return data.decode("utf-8", "replace").replace("\r", ""), None
    except paramiko.AuthenticationException:
        ssh_close()
        return None, "The router rejected the username or password in config.json."
    except (paramiko.SSHException, socket.error, EOFError) as e:
        ssh_close()
        return None, f"Could not reach the router over SSH ({e or type(e).__name__})."

# Every command below only reads. The AT line asks for: serving cell, neighbour cells,
# per-cell RSRP and RSRQ, the stored lock list, registration state, operator and the modem's
# allowed-band setting.
# @@DEV reads the mobile link's byte counters next to the router's clock, for the speed figures.
REMOTE = (
    "echo @@AT; "
    "printf 'AT+BMTCELLINFO;+BMNEICELLINFO;$QCRSRP?;$QCRSRQ?;+BMSETCELLID;+CEREG?;+COPS?;+BMBANDPREF?\\r' | "
    f"socat -t2 -T7 - file:{CFG['at_port']},raw,echo=0,b115200 2>&1; "
    "echo @@WAN; ifconfig unet0 2>&1; "
    "echo @@DEV; cat /proc/uptime; grep unet0: /proc/net/dev 2>&1; "
    "echo @@PING; ping -c 2 -W 2 1.1.1.1 2>&1; ping -c 1 -W 2 8.8.8.8 2>&1; "
    "echo @@UP; cat /proc/uptime; echo @@END"
)


atexit.register(ssh_close)


SSH_LOCK = threading.Lock()   # one command at a time: two readers on the modem's AT port garble each other


def run_remote():
    """Returns (text, None) on success or (None, reason) on failure."""
    with SSH_LOCK:
        out, err = ssh_run(REMOTE, 45)
    if err:
        return None, err
    if "@@END" not in out:
        return None, "No answer from the router."
    return out, None


# -------------------------------------------------------------------- parsing
def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


REG_TEXT = {0: "Not registered", 1: "Registered", 2: "Searching", 3: "Registration denied",
            4: "Unknown", 5: "Registered (roaming)"}
ACT_TEXT = {0: "GSM", 2: "3G", 7: "LTE"}


def parse(text, now):
    sec, cur = {}, None
    for line in text.split("\n"):
        if line.startswith("@@"):
            cur = line[2:].strip()
            sec[cur] = []
        elif cur:
            sec[cur].append(line)
    at = "\n".join(sec.get("AT", []))
    s = {"t": now, "modem_ok": "+BMTCELLINFO" in at}

    info = {}
    for m in re.finditer(r"^([A-Z_ ]+):(-?[\d.]+)\s*$", at, re.M):
        info[m.group(1).strip()] = _num(m.group(2))
    pci = info.get("PCI")
    cell_id = info.get("CELL_ID")
    serving = None
    if s["modem_ok"] and pci is not None:
        serving = {
            "pci": int(pci),
            "earfcn": int(info["EARFCN"]) if info.get("EARFCN") is not None else None,
            "band": int(info["ACTIVE BAND"]) if info.get("ACTIVE BAND") is not None else None,
            "cell_id": int(cell_id) if cell_id is not None else None,
            "enb": int(cell_id) >> 8 if cell_id is not None else None,
            "sector": int(cell_id) & 0xFF if cell_id is not None else None,
            "tac": int(info["TAC"]) if info.get("TAC") is not None else None,
            "rsrp": info.get("RSRP"), "rsrq": info.get("RSRQ"), "sinr": info.get("SINR"),
        }
    s["serving"] = serving

    towers = {}

    def tower(p, e):
        return towers.setdefault((int(p), int(e)), {"pci": int(p), "earfcn": int(e),
                                                    "rsrp": None, "rsrq": None})

    for m in re.finditer(r"pci:(\d+),rsrq:(-?\d+),rsrp:(-?\d+),rssi:(-?\d+),srxlev:(-?\d+),earfcn:(\d+)", at):
        t = tower(m.group(1), m.group(6))
        t["rsrq"], t["rsrp"] = _num(m.group(2)), _num(m.group(3))
    # The $QC answers carry one decimal place, so they win over the neighbour list.
    for key, field in (("$QCRSRP:", "rsrp"), ("$QCRSRQ:", "rsrq")):
        i = at.find(key)
        if i < 0:
            continue
        j = len(at)
        for stop in ("$QCRSRQ:", "+BMSETCELLID", "+CEREG", "+COPS", "\nOK"):
            k = at.find(stop, i + len(key))
            if k > 0:
                j = min(j, k)
        for m in re.finditer(r'(\d+),(\d+),"(-?[\d.]+)"', at[i:j]):
            tower(m.group(1), m.group(2))[field] = _num(m.group(3))
    if serving:
        t = tower(serving["pci"], serving["earfcn"] or 0)
        if t["rsrp"] is None:
            t["rsrp"] = serving["rsrp"]
        if t["rsrq"] is None:
            t["rsrq"] = serving["rsrq"]
        serving["rsrp_precise"] = t["rsrp"]
    tl = sorted(towers.values(), key=lambda x: (x["rsrp"] is None, -(x["rsrp"] or -999)))
    for t in tl:
        t["serving"] = bool(serving and t["pci"] == serving["pci"] and t["earfcn"] == serving["earfcn"])
    s["towers"] = tl

    lock = []
    m = re.search(r"\+BMSETCELLID:([\d, ]+)", at)
    if m:
        nums = [int(x) for x in m.group(1).replace(" ", "").split(",") if x.isdigit()]
        lock = [[nums[i], nums[i + 1]] for i in range(0, len(nums) - 1, 2) if nums[i] or nums[i + 1]]
    s["lock_list"] = lock
    s["lock_known"] = bool(m)

    m = re.search(r"\+CEREG:\s*\d+,(\d+)", at)
    s["reg"] = REG_TEXT.get(int(m.group(1)), "Unknown") if m else None
    m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]*)"(?:,(\d+))?', at)
    s["operator"] = m.group(1) if m else None
    s["act"] = ACT_TEXT.get(int(m.group(2)), None) if m and m.group(2) else None

    # +BMBANDPREF: <2G/3G mask>, <LTE mask>, <TD-SCDMA mask>. LTE bit n allows band n+1 (0x4 = band 3 only).
    m = re.search(r"\+BMBANDPREF:\s*0x[0-9A-Fa-f]+\s*,\s*(0x[0-9A-Fa-f]+)", at)
    s["lte_band_mask"] = m.group(1).lower() if m else None

    ping = "\n".join(sec.get("PING", []))
    sent = sum(int(n) for n in re.findall(r"(\d+) packets transmitted", ping))
    recv = sum(int(n) for n in re.findall(r"(\d+) packets received", ping))
    s["ping_loss"] = round(1 - recv / sent, 2) if sent else None
    s["ping_sent"] = sent
    s["ping_times"] = [float(x) for x in re.findall(r"time=([\d.]+) ?ms", ping)]   # reply times, ms
    s["internet"] = recv > 0
    s["ping_ok"] = recv > 0          # this reading alone; "internet" is smoothed by the poller
    m = re.search(r"inet addr:(\S+)", "\n".join(sec.get("WAN", [])))
    s["wan_ip"] = m.group(1) if m else None
    up = " ".join(sec.get("UP", [])).split()
    s["uptime_s"] = _num(up[0]) if up else None
    # Bytes received and sent on the mobile link since the router started, with the router's clock.
    dev = "\n".join(sec.get("DEV", []))
    m = re.search(r"^\s*([\d.]+)\s+[\d.]+\s*$", dev, re.M)
    c = re.search(r"unet0:\s*(\d+)(?:\s+\d+){7}\s+(\d+)", dev)
    s["counters"] = (float(m.group(1)), int(c.group(1)), int(c.group(2))) if m and c else None
    s["down_mbps"] = s["up_mbps"] = None      # filled in by the poller from two readings
    return s


MAX_MBPS = 300      # more than this modem can carry: the counters were reset, not a real speed


def speeds(prev, cur):
    """Download and upload speed in Mbit/s between two counter readings, or (None, None)."""
    if not prev or not cur:
        return None, None
    dt = cur[0] - prev[0]
    if not 1 <= dt <= 120:          # router restarted, or too long a silence to call it "now"
        return None, None
    out = []
    for a, b in ((prev[1], cur[1]), (prev[2], cur[2])):
        d = b - a
        if d < 0 and a < 2 ** 32:   # a 32-bit counter went past its end and started again
            d += 2 ** 32
        mbps = d * 8 / dt / 1e6
        if d < 0 or mbps > MAX_MBPS:
            return None, None
        out.append(round(mbps, 3))
    return out[0], out[1]


# ---------------------------------------------------------------------- state
LOCK = threading.Lock()
SAMPLES = deque(maxlen=MAX_SAMPLES)   # compact rows: (t, serving_pci, earfcn, rsrp, rsrq, sinr, net, {pci: rsrp}, ping_loss, down_mbps, up_mbps)
EVENTS = deque(maxlen=300)
SLOTS = {}                            # pci -> colour slot, in order first seen; never reassigned
STATE = {"latest": None, "router_ok": None, "error": None, "last_ok": None, "last_switch": None}
KNOWN = {}                            # (pci, earfcn) -> last reading; lets a tower be locked while it is out of sight
PINGS = deque(maxlen=60)              # (t, (pci, earfcn) connected or None, [reply times in ms], packets sent)
PING_WINDOW = 60                      # seconds of test packets behind the ping shown for the connected tower


def ping_summary(now):
    """Median reply time over the last minute on the tower connected now (called with LOCK held).

    Only readings taken on that same tower count, so a switch starts the figure afresh.
    """
    sv = (STATE["latest"] or {}).get("serving")
    if not sv:
        return None
    key, times, sent, oldest = (sv["pci"], sv["earfcn"]), [], 0, None
    for t, k, ms, n in reversed(PINGS):
        if now - t > PING_WINDOW or k != key:
            break
        times += ms
        sent += n
        oldest = t
    if not sent:
        return None
    return {"ms": round(statistics.median(times)) if times else None,
            "min": round(min(times)) if times else None, "max": round(max(times)) if times else None,
            "replies": len(times), "sent": sent, "span_s": round(now - oldest)}


def remember(pci, earfcn, rsrp, t, served=False):
    k = KNOWN.setdefault((pci, earfcn), {"rsrp": rsrp, "t": t, "served": False})
    k.update(rsrp=rsrp, t=t, served=k["served"] or bool(served))


def load_locations():
    try:
        with open(LOC_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return {"me": d.get("me"), "towers": d.get("towers") or {}}
    except (OSError, ValueError):
        return {"me": None, "towers": {}}


LOC = load_locations()


def latlon(o):
    """Validate a {lat, lon} pair; None clears it."""
    if o is None:
        return None
    lat, lon = float(o["lat"]), float(o["lon"])
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("out of range")
    return {"lat": round(lat, 6), "lon": round(lon, 6)}


def save_locations():
    with open(LOC_PATH, "w", encoding="utf-8") as f:
        json.dump(LOC, f, indent=1)


# Optional: look up a tower's position on OpenCelliD. This only runs when the user has put
# their own key in config.json, because it sends the tower's ID to that outside service.
OCID_KEY = (CFG.get("opencellid_key") or "").strip() if is_set(CFG.get("opencellid_key")) else ""
LOOKUP = {}   # cell_id -> {"state": "pending" | "found" | "not_found" | "error", "text": str}


def lookup_tower(cell_id, tac):
    q = urllib.parse.urlencode({"key": OCID_KEY, "mcc": CFG.get("mcc", 0), "mnc": CFG.get("mnc", 0),
                                "lac": tac, "cellid": cell_id, "radio": "LTE", "format": "json"})
    try:
        try:
            # OpenCelliD turns away Python's default client name, so send a plain generic one.
            req = urllib.request.Request("https://opencellid.org/cell/get?" + q,
                                         headers={"User-Agent": "tower-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            d = json.loads(e.read().decode("utf-8", "replace") or "{}")
        if "lat" in d and "lon" in d:
            pos = latlon(d)
            pos["source"] = "opencellid"
            if isinstance(d.get("range"), (int, float)):
                pos["range_m"] = int(d["range"])
            with LOCK:
                LOC["towers"][str(cell_id)] = pos
                save_locations()
                LOOKUP[cell_id] = {"state": "found", "text": ""}
            return
        code = d.get("code")
        if code == 1:
            res = {"state": "not_found", "text": "OpenCelliD has no record of this tower. Enter its location by hand."}
        elif code == 2:
            res = {"state": "error", "text": "OpenCelliD did not accept the key in config.json."}
        else:
            res = {"state": "error", "text": f"OpenCelliD answered: {str(d.get('error', 'unknown error'))[:80]}"}
    except (OSError, ValueError, KeyError, TypeError):
        res = {"state": "error", "text": "Could not reach OpenCelliD. It will be tried again when Tower Monitor restarts."}
    with LOCK:
        LOOKUP[cell_id] = res


def maybe_lookup(s):
    """Called with LOCK held, once per new reading."""
    sv = s.get("serving") or {}
    cid, tac = sv.get("cell_id"), sv.get("tac")
    if not OCID_KEY or cid is None or tac is None or cid in LOOKUP or str(cid) in LOC["towers"]:
        return
    LOOKUP[cid] = {"state": "pending", "text": "Looking this tower up on OpenCelliD…"}
    threading.Thread(target=lookup_tower, args=(cid, tac), daemon=True).start()


def add_event(t, kind, text):
    EVENTS.append({"t": t, "kind": kind, "text": text})


def ingest(row):
    """Add one compact row to history and record any change worth an event."""
    t, spci, _e, _rsrp, _rsrq, _sinr, net, tw, _pl, _dn, _up = row
    for p in ([spci] if spci is not None else []) + sorted(tw):
        SLOTS.setdefault(p, len(SLOTS))
    if SAMPLES:
        pt, ppci, *_rest = SAMPLES[-1]
        pnet = SAMPLES[-1][6]
        if t - pt > GAP_SECONDS:
            add_event(t, "gap", f"No readings for {round((t - pt) / 60)} min (monitor stopped or router unreachable)")
        if spci is not None and ppci is not None and spci != ppci:
            add_event(t, "switch", f"Switched tower: PCI {ppci} to PCI {spci}")
            STATE["last_switch"] = t
        if net != pnet and net is not None and pnet is not None:
            add_event(t, "net_up" if net else "net_down", "Internet is back" if net else "Internet went down")
    if STATE["last_switch"] is None:
        STATE["last_switch"] = t
    SAMPLES.append(row)


def to_row(s):
    sv = s.get("serving") or {}
    tw = {t["pci"]: t["rsrp"] for t in s.get("towers", []) if t["rsrp"] is not None}
    rsrp = sv.get("rsrp_precise", sv.get("rsrp"))
    return (s["t"], sv.get("pci"), sv.get("earfcn"), rsrp, sv.get("rsrq"), sv.get("sinr"),
            s.get("internet"), tw, s.get("ping_loss"), s.get("down_mbps"), s.get("up_mbps"))


LOG_HEADER = ("epoch,serving_pci,earfcn,rsrp,rsrq,sinr,internet,towers,ping_loss,down_mbps,up_mbps,"
              "band,lte_band_mask")


def band_names(mask):
    """'0x4' -> 'B3'; '0x5' -> 'B1, B3'."""
    try:
        v = int(mask, 16)
    except (TypeError, ValueError):
        return "unknown"
    names = [f"B{i + 1}" for i in range(v.bit_length()) if v >> i & 1]
    return ", ".join(names) if len(names) <= 8 else f"{len(names)} bands"


def band_events(s):
    """Add an event when the modem's band setting or the band in use changes. Called under LOCK."""
    t, mask = s["t"], s.get("lte_band_mask")
    sv = s.get("serving") or {}
    band = sv.get("band") if sv.get("pci") else None     # PCI 0 = no cell, its band field means nothing
    if mask:
        old = STATE.get("band_mask")
        if old and old != mask:
            add_event(t, "band_setting", f"Modem band setting changed: {band_names(old)} ({old}) to "
                                         f"{band_names(mask)} ({mask})")
        STATE["band_mask"] = mask
    if band:
        old = STATE.get("band")
        if old and old != band:
            allowed = f", band setting {band_names(mask)}" if mask else ""
            add_event(t, "band", f"Changed band: B{old} to B{band} (PCI {sv.get('pci')}, "
                                 f"EARFCN {sv.get('earfcn')}{allowed})")
        STATE["band"] = band


def upgrade_log_header():
    """A log started by an older version has a shorter first line; name the newer columns in it."""
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            first = f.readline().rstrip("\n")
            if not first.startswith("epoch,") or first == LOG_HEADER:
                return
            rest = f.read()
        tmp = LOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(LOG_HEADER + "\n" + rest)
        os.replace(tmp, LOG_PATH)
    except OSError:
        pass


def log_row(row, band=None, lte_band_mask=None):
    t, spci, e, rsrp, rsrq, sinr, net, tw, pl, dn, up = row
    fields = [f"{t:.0f}", spci, e, rsrp, rsrq, sinr, "" if net is None else int(net),
              "|".join(f"{p}:{v}" for p, v in sorted(tw.items())), pl, dn, up, band, lte_band_mask]
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        if new:
            f.write(LOG_HEADER + "\n")
        f.write(",".join("" if x is None else str(x) for x in fields) + "\n")


def load_log():
    if not os.path.exists(LOG_PATH):
        return
    with open(LOG_PATH, encoding="utf-8") as f:
        lines = deque(f, maxlen=MAX_SAMPLES)
    for line in lines:
        p = line.rstrip("\n").split(",")
        if len(p) < 8 or not p[0].replace(".", "").isdigit():
            continue
        tw = {}
        for item in p[7].split("|"):
            if ":" in item:
                a, b = item.split(":", 1)
                v = _num(b)
                if a.isdigit() and v is not None:
                    tw[int(a)] = v
        if p[2]:   # the log keeps one frequency per row, the connected tower's; neighbours listed with it share it
            for a, v in tw.items():
                remember(a, int(p[2]), v, float(p[0]), served=p[1] == str(a))
        ingest((float(p[0]), int(p[1]) if p[1] else None, int(p[2]) if p[2] else None,
                _num(p[3]), _num(p[4]), _num(p[5]), None if p[6] == "" else p[6] == "1", tw,
                _num(p[8]) if len(p) > 8 else None,
                _num(p[9]) if len(p) > 10 else None, _num(p[10]) if len(p) > 10 else None))


def poller():
    while True:
        started = time.time()
        text, err = run_remote()
        now = time.time()
        with LOCK:
            if text is None:
                # The router is slow to accept a new SSH session, so one miss is not an outage.
                STATE["fails"] = STATE.get("fails", 0) + 1
                if STATE["fails"] >= 2:
                    if STATE["router_ok"] is not False:
                        add_event(now, "router_down", "Router not reachable from this computer")
                    STATE["router_ok"], STATE["error"] = False, err
            else:
                STATE["fails"] = 0
                if STATE["router_ok"] is False:
                    add_event(now, "router_up", "Router reachable again")
                try:
                    s = parse(text, now)
                    # This link loses a lot of test packets even while it works, so call it
                    # offline only when three checks in a row get no reply at all.
                    STATE["misses"] = 0 if s["internet"] else STATE.get("misses", 0) + 1
                    s["internet"] = STATE["misses"] < 3
                    s["down_mbps"], s["up_mbps"] = speeds(STATE.get("counters"), s["counters"])
                    STATE["counters"] = s["counters"]
                    STATE.update(router_ok=True, error=None, last_ok=now, latest=s)
                    sv = s["serving"]
                    PINGS.append((now, (sv["pci"], sv["earfcn"]) if sv else None, s["ping_times"], s["ping_sent"]))
                    maybe_lookup(s)
                    for t in s["towers"]:
                        if t["rsrp"] is not None:
                            remember(t["pci"], t["earfcn"], t["rsrp"], now, served=t["serving"])
                    if s["modem_ok"]:
                        row = to_row(s)
                        ingest(row)
                        band_events(s)
                        sv = s["serving"] or {}
                        if not DRY_RUN:
                            log_row(row, sv.get("band") if sv.get("pci") else None, s["lte_band_mask"])
                except Exception as e:  # one odd reading must never stop the monitor
                    print(f"Skipped a reading: {e!r}", file=sys.stderr)
        time.sleep(max(1.0, POLL - (time.time() - started)))


# ------------------------------------------------------------- lock / unlock
# The only code that changes the modem. It runs when the user presses a button on the page:
#   lock:    AT+BMSETCELLID=1,<pci>,<earfcn>   store the one allowed tower
#            AT+BMLOCKCELL=1                    switch the lock on
#   unlock:  AT+BMLOCKCELL=0, then empty the list
# Either only takes effect when the modem starts again, so the page then asks the user to restart
# the router and waits for it. After the restart it follows the normal readings; if a locked modem
# cannot connect, it switches the lock off again and asks for one more restart. Every step is shown
# on the page and written to lock_actions.log, with the modem's raw answers, for later study.
ACTION = {"state": "idle", "kind": None, "target": None, "started": None, "ended": None,
          "steps": [], "result": None}


def at(cmd, write=False):
    """Send one AT line to the modem and return its answer ('' when there is none)."""
    if not re.fullmatch(r"AT[A-Z0-9+=,;?]*", cmd):
        raise ValueError("unexpected AT command")
    if write and DRY_RUN:
        return "OK (dry run, not sent)"
    remote = f"printf '{cmd}\\r' | socat -t3 -T8 - file:{CFG['at_port']},raw,echo=0,b115200 2>&1"
    with SSH_LOCK:
        out, _err = ssh_run(remote, 40)
    return (out or "").strip()


def at_ok(answer):
    return bool(re.search(r"(^|\n)OK\b", answer)) and "ERROR" not in answer


def at_retry(cmd, tries=6, gap=10):
    """For commands sent while the modem may still be restarting."""
    answer = ""
    for i in range(tries):
        answer = at(cmd, write=True)
        if at_ok(answer):
            break
        if i < tries - 1:
            time.sleep(gap)
    return answer


def lock_list_from(answer):
    m = re.search(r"\+BMSETCELLID:([\d, ]+)", answer)
    if not m:
        return None
    nums = [int(x) for x in m.group(1).replace(" ", "").split(",") if x.isdigit()]
    return [[nums[i], nums[i + 1]] for i in range(0, len(nums) - 1, 2) if nums[i] or nums[i + 1]]


def step(text, detail=None):
    now = time.time()
    with LOCK:
        ACTION["steps"].append({"t": now, "text": text})
    line = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + ("  DRY RUN  " if DRY_RUN else "  ") + text
    if detail:
        line += "   [modem: " + " | ".join(x for x in detail.split("\n") if x.strip()) + "]"
    try:
        with open(ACTION_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def finish(state, result, detail=None):
    step(result, detail)
    with LOCK:
        ACTION.update(state=state, result=result, ended=time.time())
        add_event(time.time(), "lock", result)


def watch(target, since, seconds):
    """Follow the readings after a modem restart, reporting each change as a step.

    target is (pci, earfcn), or None when any tower will do.
    Returns "ok", "other_tower" (online, but not on the target), "no_service" (a locked modem
    that answers but cannot register) or "timeout".
    """
    seen, good, last_t, other_since = {}, 0, None, None
    answering_since, registered_since, ever_registered = None, None, False
    while time.time() < since + seconds:
        time.sleep(3)
        with LOCK:
            s = STATE["latest"]
            router_ok = STATE["router_ok"]
        if router_ok is False and seen.get("router") != "down":
            seen["router"] = "down"
            step("This computer cannot reach the router right now")
        if not s or s["t"] == last_t or s["t"] < since + (5 if DRY_RUN else 20):
            continue                      # nothing new yet, or a reading from before the restart
        last_t = s["t"]
        if router_ok and seen.get("router") == "down":
            seen["router"] = "up"
            step("The router is reachable again")
        sv = s.get("serving")
        facts = {
            "modem": "The modem is answering again" if s["modem_ok"] else "The modem is still restarting",
            "reg": f"Network registration: {s['reg']}" if s.get("reg") else None,
            "tower": f"Connected to PCI {sv['pci']} (EARFCN {sv['earfcn']})" if sv else None,
            "wan": ("The router has a mobile-network address" if s.get("wan_ip")
                    else "The router has no mobile-network address yet"),
            "net": "Test packets are getting through" if s.get("ping_ok") else None,
        }
        for k, v in facts.items():
            if v and seen.get(k) != v:
                seen[k] = v
                step(v)
        registered = bool(s["modem_ok"] and sv and (s.get("reg") or "").startswith("Registered"))
        if s["modem_ok"] and answering_since is None:
            answering_since = s["t"]
        ever_registered = ever_registered or registered
        registered_since = (registered_since or s["t"]) if registered else None
        if target and not ever_registered and answering_since and s["t"] - answering_since >= NO_SERVICE_AFTER:
            return "no_service"
        if (registered and not s.get("wan_ip") and s["t"] - registered_since >= ROUTER_HINT_AFTER
                and not seen.get("hint")):
            seen["hint"] = True
            step("The modem is connected, but the router has not picked the connection up. " + RECONNECT_HINT)
        good = good + 1 if (s["modem_ok"] and sv and s.get("ping_ok")) else 0
        if good >= 2:
            if target is None or (sv["pci"], sv["earfcn"]) == target:
                return "ok"
            other_since = other_since or s["t"]
            if s["t"] - other_since >= 45:
                return "other_tower"
        else:
            other_since = None
    return "timeout"


def diagnose():
    """One sentence on where the connection is stuck, for the action log and the page."""
    answer = at("AT+CEREG?;+CGATT?;+CGPADDR")
    with LOCK:
        s = STATE["latest"] or {}
    reg = re.search(r"\+CEREG:\s*\d+,(\d+)", answer)
    att = re.search(r"\+CGATT:\s*(\d)", answer)
    registered = bool(reg) and reg.group(1) in ("1", "5")
    attached = bool(att) and att.group(1) == "1"
    if not answer or "CEREG" not in answer:
        text = "The modem is not answering."
    elif not registered:
        text = "The modem could not register on the network with this lock."
    elif not attached:
        text = "The modem registered on the network but got no data connection."
    elif not s.get("wan_ip"):
        text = "The modem is connected, but the router did not reconnect to it after the modem restart."
    else:
        text = "The modem and router look connected, but no test packets get through."
    step("Diagnosis: " + text, answer)
    return text


def wait_for_router_restart(since, online=True):
    """Ask for a router restart and wait for it. Returns the time the router came up, or None."""
    step(RESTART_ASK + ("The internet keeps working until you do. Waiting for the restart…" if online
                        else "That brings the internet back. Waiting for the restart…"))
    if DRY_RUN:
        return since
    while time.time() < since + RESTART_WAIT:
        time.sleep(3)
        with LOCK:
            s = STATE["latest"]
        if s and s["t"] > since and s.get("uptime_s") is not None and s["t"] - s["uptime_s"] > since:
            step("The router has restarted")
            return s["t"] - s["uptime_s"]
    return None


def lock_off_commands():
    """Switch the lock off and empty the list. Returns the modem's answer to the switch-off."""
    step("Switching the lock off")
    r = at_retry("AT+BMLOCKCELL=0")
    if at_ok(r):
        r2 = at("AT+BMSETCELLID=1,0,0", write=True)
        step("Emptying the list of allowed towers", r2)
    return r


def do_unlock(auto=False, why=""):
    r = lock_off_commands()
    if not at_ok(r):
        return finish("failed", why + "The modem did not accept the command to switch the lock off. "
                      "Restart the router, then press Remove lock again.", r)
    up = wait_for_router_restart(time.time(), online=not auto)
    if up is None:
        return finish("failed" if auto else "done", why + "The lock is switched off in the modem's settings. "
                      "The modem only notices at its next start, so it stays on the locked tower until the router "
                      "is restarted.")
    if watch(None, up, RECOVER_WAIT) == "ok":
        if auto:
            return finish("failed", why + "The lock was removed and the internet is back. "
                          "The modem chooses the tower by itself again.")
        return finish("done", "Lock removed. The modem chooses the tower by itself again.")
    diagnose()
    finish("failed", why + "The lock is switched off, but the internet has not come back. " + RECONNECT_HINT)


def do_lock(pci, earfcn):
    name = f"PCI {pci} (EARFCN {earfcn})"
    step(f"Storing {name} as the only allowed tower")
    r = at(f"AT+BMSETCELLID=1,{pci},{earfcn}", write=True)
    if not at_ok(r):
        return finish("failed", "The modem refused the tower list. Nothing was changed.", r)
    r = at("AT+BMSETCELLID")
    stored = lock_list_from(r)
    if not DRY_RUN and (not stored or stored[0] != [pci, earfcn]):
        return finish("failed", "The modem did not store the tower, so the lock was not switched on.", r)
    extra = [x for x in (stored or [])[1:]] if not DRY_RUN else []
    if extra:
        step("Note: the list also still allows " + ", ".join(f"PCI {a} (EARFCN {b})" for a, b in extra), r)
    step("Switching the lock on")
    r = at("AT+BMLOCKCELL=1", write=True)
    if not at_ok(r):
        return finish("failed", "The modem refused to switch the lock on. The tower list was stored, "
                      "but the modem still chooses the tower by itself.", r)
    step("The lock is stored in the modem. It starts when the modem starts next", r)
    # The modem must restart for the lock to apply. Restarting only the modem (AT+CFUN=1,1) makes this
    # router lose its mobile interface until the router itself is rebooted (seen 2026-11-13), so the
    # whole router is restarted instead, which brings the modem up cleanly.
    up = wait_for_router_restart(time.time())
    if up is None:
        return finish("done", f"{name} is stored and the lock is switched on, but the router was not restarted. "
                      "The lock starts at the next router restart; until then nothing changes.")
    result = watch((pci, earfcn), up, REVERT_AFTER)
    with LOCK:
        latest = STATE["latest"] or {}
    kept = [pci, earfcn] in (latest.get("lock_list") or []) or DRY_RUN
    if result == "ok" and kept:
        probe = at("AT+BMLOCKCELL=1", write=True)   # asks for what is already set; the answer is kept for study
        return finish("done", f"Online on {name}, and the modem kept the lock through the restart. The modem "
                      "does not report whether the lock is active, so the proof is that it stays on this tower "
                      "from now on.", probe)
    if result in ("ok", "other_tower") and not kept:
        at("AT+BMLOCKCELL=0", write=True)
        return finish("failed", "The internet is back, but the modem forgot the allowed tower when it restarted, "
                      "so it is not locked. This modem does not keep the lock through a router restart.")
    if result == "other_tower":
        sv = latest.get("serving") or {}
        r = lock_off_commands()
        return finish("failed", f"The modem came back online on PCI {sv.get('pci')} instead of PCI {pci}, so the "
                      "lock did not take effect. The lock was switched off again; the internet is working.", r)
    if result == "no_service":
        step(f"The modem has found no usable signal from PCI {pci} for {NO_SERVICE_AFTER} seconds")
        diagnose()
        return do_unlock(auto=True, why=f"PCI {pci} cannot be used from here: with the lock on, the modem could not "
                         "connect to it (its signal is too weak or too noisy at this spot). ")
    step(f"No internet {round(REVERT_AFTER / 60)} minutes after the restart")
    why = diagnose() + " "
    do_unlock(auto=True, why=f"Locking to PCI {pci} did not work: " + why)


def run_action(kind, target):
    try:
        if kind == "lock":
            do_lock(*target)
        else:
            do_unlock()
    except Exception as e:   # never leave the page stuck on "working"
        finish("failed", f"Tower Monitor hit an unexpected error ({e!r}). Check the Tower lock tile; "
               "if the internet stays down, press Remove lock or restart the router.")


def start_action(kind, target=None):
    """Returns None when started, or the reason it was not."""
    with LOCK:
        if ACTION["state"] == "running":
            return "A lock change is already in progress."
        if STATE["router_ok"] is not True:
            return "The router is not reachable right now."
        ACTION.update(state="running", kind=kind, started=time.time(), ended=None, steps=[], result=None,
                      target={"pci": target[0], "earfcn": target[1]} if target else None)
        add_event(time.time(), "lock", f"Lock to PCI {target[0]} requested" if target else "Remove lock requested")
    threading.Thread(target=run_action, args=(kind, target), daemon=True).start()
    return None


# ------------------------------------------------------------------- snapshot
def snapshot(range_s):
    with LOCK:
        now = time.time()
        t0 = now - range_s
        width = max(range_s / MAX_BUCKETS, POLL * 3)   # wide enough that no bucket sits empty between readings
        n = int(range_s / width)
        acc = [None] * n
        rows = [r for r in SAMPLES if r[0] >= t0]
        switches, net_known, net_up, share, losses = 0, 0, 0, {}, []
        downs, ups = [], []
        prev = None
        for r in rows:
            t, spci, _e, _rsrp, rsrq, sinr, net, tw, pl, dn, up = r
            if pl is not None:
                losses.append(pl)
            i = min(n - 1, int((t - t0) / width))
            b = acc[i]
            if b is None:
                b = acc[i] = {"s": None, "net": None, "sinr": [], "rsrq": [], "r": {}, "down": [], "up": []}
            b["s"] = spci if spci is not None else b["s"]
            if net is not None:
                b["net"] = net if b["net"] is None else (b["net"] and net)
                net_known += 1
                net_up += 1 if net else 0
            if sinr is not None:
                b["sinr"].append(sinr)
            if rsrq is not None:
                b["rsrq"].append(rsrq)
            if dn is not None and up is not None:
                b["down"].append(dn)
                b["up"].append(up)
                downs.append(dn)
                ups.append(up)
            for p, v in tw.items():
                b["r"].setdefault(p, []).append(v)
            if spci is not None:
                share[spci] = share.get(spci, 0) + 1
                if prev is not None and prev != spci:
                    switches += 1
                prev = spci

        def avg(xs, digits=1):
            return round(sum(xs) / len(xs), digits) if xs else None

        hist = []
        for i, b in enumerate(acc):
            t = round(t0 + (i + 0.5) * width, 1)
            if b is None:
                hist.append({"t": t, "s": None, "net": None, "sinr": None, "rsrq": None, "r": {},
                             "down": None, "up": None})
            else:
                hist.append({"t": t, "s": b["s"], "net": b["net"], "sinr": avg(b["sinr"]),
                             "rsrq": avg(b["rsrq"]), "r": {str(p): avg(v) for p, v in b["r"].items()},
                             "down": avg(b["down"], 2), "up": avg(b["up"], 2)})
        total = sum(share.values())
        # Weak towers flicker in and out of the modem's list. While connected, keep the ones seen in the
        # last 15 minutes pickable; with no service, offer the last day's, towers that worked before first.
        in_sight = {(t["pci"], t["earfcn"]) for t in ((STATE["latest"] or {}).get("towers") or [])}
        horizon, cap = (900, 4) if (STATE["latest"] or {}).get("serving") else (86400, 8)
        known = sorted(({"pci": k[0], "earfcn": k[1], **v} for k, v in KNOWN.items()
                        if k not in in_sight and now - v["t"] < horizon),
                       key=lambda x: (not x["served"], -x["rsrp"]))[:cap]
        return {
            "now": now, "poll_seconds": POLL, "range": range_s, "host": CFG["host"],
            "router": {"ok": STATE["router_ok"], "error": STATE["error"], "last_ok": STATE["last_ok"]},
            "latest": STATE["latest"],
            "locations": LOC,
            "lookup": {"enabled": bool(OCID_KEY), **(LOOKUP.get(((STATE["latest"] or {}).get("serving") or {}).get("cell_id")) or {})},
            "slots": {str(p): i for p, i in SLOTS.items()},
            "history": hist,
            "stats": {
                "samples": len(rows), "switches": switches,
                "net_up_pct": round(100 * net_up / net_known, 1) if net_known else None,
                "ping_loss_pct": round(100 * sum(losses) / len(losses)) if losses else None,
                "down_avg": avg(downs, 2), "down_max": round(max(downs), 2) if downs else None,
                "up_avg": avg(ups, 2), "up_max": round(max(ups), 2) if ups else None,
                "tower_share": {str(p): round(100 * c / total, 1) for p, c in share.items()} if total else {},
                "since_switch_s": (now - STATE["last_switch"]) if STATE["last_switch"] else None,
            },
            "events": [e for e in reversed(EVENTS)][:100],
            "known_towers": known,
            "ping": ping_summary(now),
            "action": dict(ACTION, steps=list(ACTION["steps"])),
            "dry_run": DRY_RUN, "revert_after": REVERT_AFTER,
        }


# ----------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        elif u.path == "/setup":
            self._send(200, "text/html; charset=utf-8", firstrun.page())
        elif u.path.startswith("/api/setup/"):
            if not firstrun.same_origin(self.headers):
                return self._send(403, "text/plain", b"forbidden")
            self._send(200, "application/json", json.dumps(firstrun.api(u.path[len("/api/setup/"):], {}, True)).encode())
        elif u.path == "/devices":
            with open(os.path.join(HERE, "devices.html"), "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        elif u.path == "/api/devices":
            snap = devices.snapshot()
            with LOCK:
                last = STATE.get("latest") or {}
                cutoff = time.time() - 120
                recent = [r for r in SAMPLES if r[0] >= cutoff]
                losses = [r[8] for r in recent if r[8] is not None]
                speed = next((r for r in reversed(recent) if r[9] is not None and r[0] >= cutoff + 60), None)
            snap["link"] = {"down_mbps": speed[9] if speed else None, "up_mbps": speed[10] if speed else None,
                            "ping_loss": round(100 * sum(losses) / len(losses)) if losses else None}
            self._send(200, "application/json", json.dumps(snap).encode())
        elif u.path == "/api/status":
            try:
                rng = int(parse_qs(u.query).get("range", ["900"])[0])
            except ValueError:
                rng = 900
            rng = min(max(rng, 300), 86400)
            self._send(200, "application/json", json.dumps(snapshot(rng)).encode())
        else:
            self._send(404, "text/plain", b"not found")


    def do_POST(self):
        # Only the page itself (same machine, same origin, JSON body) may save locations or change the lock.
        host = (self.headers.get("Host") or "").split(":")[0]
        path = urlparse(self.path).path
        if path.startswith("/api/setup/"):          # "Change router settings" from the dashboard
            if not firstrun.same_origin(self.headers):
                return self._send(403, "text/plain", b"forbidden")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n <= 65536 else {}
            except ValueError:
                body = {}
            res = firstrun.api(path[len("/api/setup/"):], body, True)
            return self._send(200, "application/json", json.dumps(res).encode())
        if path not in ("/api/locations", "/api/lock", "/api/unlock", "/api/device") or host not in ("127.0.0.1", "localhost"):
            return self._send(404, "text/plain", b"not found")
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return self._send(403, "text/plain", b"forbidden")
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, "text/plain", b"json only")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                raise ValueError("too large")
            body = json.loads(self.rfile.read(n))
            if path == "/api/device":
                return self.device_action(body)
            if path != "/api/locations":
                target = None
                if path == "/api/lock":
                    target = (int(body["pci"]), int(body["earfcn"]))
                    if not (0 <= target[0] <= 503 and 0 < target[1] <= 70000):
                        raise ValueError("out of range")
                busy = start_action("lock" if target else "unlock", target)
                if busy:
                    return self._send(409, "text/plain; charset=utf-8", busy.encode())
                return self._send(200, "application/json", b'{"ok": true}')
            with LOCK:
                if "me" in body:
                    LOC["me"] = latlon(body["me"])
                if "tower" in body:
                    cid = str(int(body["tower"]["cell_id"]))
                    pos = latlon(body["tower"].get("pos"))
                    if pos is None:
                        LOC["towers"].pop(cid, None)
                    else:
                        LOC["towers"][cid] = pos
                save_locations()
                out = json.dumps(LOC).encode()
        except (ValueError, KeyError, TypeError):
            return self._send(400, "text/plain", b"bad request")
        self._send(200, "application/json", out)

    def device_action(self, body):
        mac = devices.norm_mac(str(body.get("mac", "")))
        kind = body.get("action")
        if not mac or kind not in ("limit", "unlimit", "block", "unblock", "rename"):
            raise ValueError("bad action")
        if kind == "rename":
            devices.set_name(mac, str(body.get("name", "")))
            return self._send(200, "application/json", b'{"ok": true}')
        down = up = None
        if kind == "limit":
            down, up = int(body["down_kbps"]), int(body["up_kbps"])
            if not (64 <= down <= 100000 and 50 <= up <= 100000):
                raise ValueError("out of range")
        if DRY_RUN:
            return self._send(409, "text/plain; charset=utf-8", b"Dry run: the TP-Link is not changed.")
        busy = devices.run_action(kind, mac, down, up)
        if busy:
            return self._send(409, "text/plain; charset=utf-8", busy.encode())
        self._send(200, "application/json", b'{"ok": true}')


def house_down_bps():
    """Latest whole-house download (D-Link mobile link) in bit/s, or None if not fresh."""
    with LOCK:
        cutoff = time.time() - 60
        r = next((r for r in reversed(SAMPLES) if r[0] >= cutoff and r[9] is not None), None)
    return r[9] * 1e6 if r else None


def main():
    if threading.current_thread() is threading.main_thread():   # the app runs this in a thread
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))   # so the SSH session is closed on kill too
    if not DRY_RUN:
        upgrade_log_header()
    load_log()
    threading.Thread(target=poller, daemon=True).start()
    devices.start(CFG, house_down=house_down_bps)
    atexit.register(lambda: devices.POLLER and devices.POLLER.save_now())
    port = int(CFG.get("listen_port", 8765)) + (1 if DRY_RUN else 0)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Tower Monitor is running at {url}  (press Ctrl+C to stop)"
          + ("\nDRY RUN: lock commands are shown but not sent to the modem; nothing is logged." if DRY_RUN else ""))
    if "--no-open" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
