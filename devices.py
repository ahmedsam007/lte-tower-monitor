"""Devices: who is on the home network, how busy each one is, and speed limits / blocks.

All devices sit behind the TP-Link TL-WR841HP (192.168.1.1), so that is where they are
seen and controlled. The D-Link only ever sees the TP-Link as one client.

What the TP-Link can tell us (measured 2026-11-17):
  * the DHCP client list (name the device reports, MAC, IP)
  * its Traffic Statistics, which count only what each device SENDS (upload).
    A 4 MB download on a test computer added 0.38 MB; a 1.5 MB upload added 2.3 MB.
    Exact per-device download is therefore not available. A device that downloads
    must send reply packets, so "packets sent per second" is used as the activity signal.
  * which devices are on its own Wi-Fi, and for each of them a counter of what the router
    sent TO it over Wi-Fi (its download). The page labels the column "Received Packets"; the
    number is packets x 256 (measured 2026-11-18: 200 packets to a phone raised it by ~51200
    whether they were 10 or 1400 bytes). So download is known in packets, not bytes, and the
    speed shown is an estimate (packets x DOWN_PKT_BYTES, capped by the whole-house download).
  * the computer running Tower Monitor knows its own exact download (psutil, or netstat -ibn on macOS).
  Devices on the Archer C20's Wi-Fi or on a cable have no download figure.

What it can change (only when the user presses a button on the page):
  * Bandwidth Control: a maximum download/upload speed per IP address
  * Access Control: block a device (by MAC) from the internet
  * Address Reservation: keeps a limited device on the same IP, so the limit stays on it

Optional: the Archer C20 access point (192.168.1.103) for its Wi-Fi client list.
"""
import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.environ.get("TOWER_MONITOR_DATA") or os.path.dirname(os.path.abspath(__file__))   # where data is kept
USAGE_PATH = os.path.join(HERE, "device_usage.json")    # daily totals per device
NAMES_PATH = os.path.join(HERE, "device_names.json")    # names the user typed
ACTION_LOG = os.path.join(HERE, "device_actions.log")   # every change made to the TP-Link

POLL = 10            # seconds between reads of the TP-Link
RULES_EVERY = 60     # seconds between reads of the limit / block lists
KEEP_DAYS = 35
HISTORY = 30         # samples kept per device for the 5-minute average (30 x 10 s)
BLOCK_PREFIX = "blk-"   # name of the TP-Link host entry and rule this page creates for a block

# Total link size given to the TP-Link's Bandwidth Control. It is set above anything the LTE
# link delivers, so it never slows the house as a whole; only the per-device maximums apply.
TOTAL_DOWN_KBPS = 20000
TOTAL_UP_KBPS = 10000

# The TP-Link (fw 3.15.2) stores the download maximum of a rule but does NOT enforce it
# (measured 2026-11-18: a 1000 kbit/s download rule on a test computer still let 3-6 Mbit/s through,
# with Line Type ADSL or Other and with totals 20000 or 8000). The upload maximum IS enforced
# (300 kbit/s cap -> 131 kbit/s). A download needs reply packets going up, so a low upload cap
# holds download down: measured upload cap 100 -> 1.4 Mbit/s down, 50 -> 0.3-0.45 Mbit/s,
# 25 -> downloads stall completely. So the upload cap is lowered to about download / 12,
# never below 50. Approximate: calls and other UDP streams are less affected.
MIN_UP_CAP_KBPS = 50
DOWN_PER_UP = 12


def upload_cap_for(down_kbps, up_kbps):
    """Upload maximum actually given to the router so the download stays near down_kbps."""
    return max(MIN_UP_CAP_KBPS, min(up_kbps, round(down_kbps / DOWN_PER_UP)))


WIFI_RX_SCALE = 256     # the TP-Link's per-station download counter is packets x 256
DOWN_PKT_BYTES = 1400    # assumed size of a download packet (full-size for video, updates, pages)


def norm_mac(m):
    m = re.sub(r"[^0-9A-Fa-f]", "", m or "").upper()
    return "-".join(m[i:i + 2] for i in range(0, 12, 2)) if len(m) == 12 else ""


def guess_kind(name):
    n = (name or "").lower()
    if re.search(r"iphone|android|galaxy|redmi|xiaomi|poco|oppo|vivo|huawei|honor|realme|pixel|oneplus|"
                 r"nokia|motorola|moto|infinix|tecno|-a\d\d|sm-|phone|\b2\d{6}[a-z]{1,3}\b|^m\d\d", n):
        return "Phone"
    if re.search(r"ipad|tab|pad", n):
        return "Tablet"
    if re.search(r"macbook|laptop|desktop|-pc|pc-|windows|thinkpad|imac|notebook|dell|hp-|lenovo", n):
        return "Computer"
    if re.search(r"tizen|webos|lgwebos|tv|bravia|roku|chromecast|firetv|android-tv|shield|mibox", n):
        return "TV"
    if re.search(r"archer|tp-link|router|extender|deco|mercusys", n):
        return "Wi-Fi access point"
    if re.search(r"playstation|ps4|ps5|xbox|nintendo|switch", n):
        return "Game console"
    return ""


# ------------------------------------------------------------------- TP-Link
class TPLink:
    """The TL-WR841HP's old web interface: HTTP Basic login, pages under /userRpm/,
    data in JavaScript arrays, settings changed with GET requests."""

    def __init__(self, host, user, password):
        self.host = host
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.lock = threading.Lock()   # its web server handles one request at a time

    def get(self, page, params=None):
        url = f"http://{self.host}/userRpm/{page}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": self.auth,
            "Referer": f"http://{self.host}/userRpm/MenuRpm.htm"})
        with self.lock:
            with urllib.request.urlopen(req, timeout=12) as r:
                return r.read().decode("latin1")

    @staticmethod
    def arrays(html):
        """Every `var name = new Array(...)` in the page, as Python lists."""
        out = {}
        for m in re.finditer(r"var (\w+) = new Array\((.*?)\);", html, re.S):
            items = re.findall(r'"((?:[^"\\]|\\.)*)"|(-?\d+)', m.group(2))
            vals = [s if s or not n else int(n) for s, n in items]
            out[m.group(1)] = vals[:-2] if vals[-2:] == [0, 0] else vals   # arrays end in 0,0
        return out

    # ---- reading
    def dhcp_clients(self):
        a = self.arrays(self.get("AssignedIpAddrListRpm.htm"))
        lst, n, w = a.get("DHCPDynList", []), *a.get("DHCPDynPara", [0, 4])[:2]
        return [{"name": lst[i * w], "mac": norm_mac(lst[i * w + 1]), "ip": lst[i * w + 2],
                 "lease": lst[i * w + 3]} for i in range(n)]

    def wifi_stations(self):
        """{mac: download counter} for the TP-Link's own Wi-Fi (see WIFI_RX_SCALE)."""
        a = self.arrays(self.get("WlanStationRpm.htm"))
        para, lst = a.get("wlanHostPara", [0, 1, 8, 0, 4]), a.get("hostList", [])
        w = para[4] if len(para) > 4 else 4
        out = {}
        for i in range(0, len(lst) - w + 1, w):
            rx = lst[i + 2] if w > 2 else None
            out[norm_mac(lst[i])] = rx if isinstance(rx, int) else None
        return out

    def statistics(self):
        """(enabled, rows). Row: ip, mac, total packets sent, total bytes sent."""
        html = self.get("SystemStatisticRpm.htm", {"Num_per_page": 100, "Goto_page": 1})
        a = self.arrays(html)
        rule = a.get("StatRulePara", [0, 10, 0, 5, 0, 13])
        enabled, n, w = rule[0] == 1, rule[4], rule[5]
        lst = a.get("statList", [])
        rows = []
        for i in range(n):
            r = lst[i * w:(i + 1) * w]
            if len(r) >= 5:
                rows.append({"ip": r[1], "mac": norm_mac(r[2]), "pkts": int(r[3]), "bytes": int(r[4])})
        return enabled, rows

    def enable_statistics(self):
        self.get("SystemStatisticRpm.htm", {"interval": 10, "Staton": "Enable"})

    def all_pages(self, page, parse):
        """Every page of a list, as (items, arrays of page 1).

        The paging fields differ per page (measured 2026-11-26): Bandwidth Control gives a
        page COUNT and answers page 2 with page 1 again; Address Reservation gives a
        "more pages" FLAG (8 per page, and there are more than 8). So pages are read until
        one is empty, repeats the previous one, or comes back with another page number.
        parse(arrays, page) returns (page number the router sent, items)."""
        items, prev, first = [], None, None
        for n in range(1, 21):
            a = self.arrays(self.get(page, {"Page": n}))
            if n == 1:
                first = a
            shown, rows = parse(a, n)
            same = [{k: v for k, v in r.items() if k != "page"} for r in rows]
            if (n > 1 and shown != n) or not rows or same == prev:
                break
            items += rows
            prev = same
        return items, first

    def reservations(self):
        def parse(a, n):
            para, lst = a.get("DHCPStaticPara", [n, 0, 0, 3]), a.get("dhcpList", [])
            w = para[3] or 3
            return para[0], [{"mac": norm_mac(lst[i]), "ip": lst[i + 1]}
                             for i in range(0, len(lst) - w + 1, w)]
        rows, _ = self.all_pages("FixMapCfgRpm.htm", parse)
        return {r["mac"]: r["ip"] for r in rows}

    def qos(self):
        """(enabled, {ip: {"id", "down", "up", "enabled"}}). Speeds in kbit/s."""
        cfg = self.arrays(self.get("QoSCfgRpm.htm")).get("QoSCfgPara", [0])

        def parse(a, n):
            para, lst = a.get("QoSRuleListParam", [n, 1, 0, 8]), a.get("QoSRuleListArray", [])
            count, w = para[2], para[3] or 8
            rows = []
            for i in range(count):
                r = lst[i * w:(i + 1) * w]
                ips = re.findall(r"\d+\.\d+\.\d+\.\d+", str(r[1]))
                if ips:
                    rows.append({"ip": ips[0], "id": r[0], "range": str(r[1]), "up": r[3],
                                 "down": r[5], "enabled": bool(r[7]), "page": n})
            return para[0], rows
        rows, _ = self.all_pages("QoSRuleListRpm.htm", parse)
        return cfg[0] == 1, {r["ip"]: r for r in rows}

    def access_control(self):
        """(enabled, deny_mode, hosts, rules) for Access Control."""
        def parse_rules(a, n):
            para, lst = a.get("access_rules_page_param", [n, 0, 0, 8]), a.get("access_rules_data_param", [])
            w = para[3] or 8
            return para[0], [{"name": lst[i * w], "host": lst[i * w + 4], "enabled": bool(lst[i * w + 7]),
                              "page": n, "pos": i}
                             for i in range(para[2]) if (i + 1) * w <= len(lst)]

        def parse_hosts(a, n):
            para, lst = a.get("hosts_lists_page_param", [n, 0, 0, 5]), a.get("hosts_lists_data_param", [])
            w = para[3] or 5
            return para[0], [{"name": lst[i * w + 1], "mac": norm_mac(lst[i * w + 4]) if lst[i * w] == 0 else "",
                              "page": n, "pos": i}
                             for i in range(para[2]) if (i + 1) * w <= len(lst)]
        rules, first = self.all_pages("AccessCtrlAccessRulesRpm.htm", parse_rules)
        hosts, _ = self.all_pages("AccessCtrlHostsListsRpm.htm", parse_hosts)
        glob = first.get("access_global_cfg_dyn_array", [0, 0])
        return glob[0] == 1, glob[1] == 0, hosts, rules

    # ---- changing (each one is a form the router's own pages submit)
    def add_reservation(self, mac, ip):
        self.get("FixMapCfgRpm.htm", {"Mac": mac, "Ip": ip, "State": 1, "Changed": 0,
                                      "SelIndex": 0, "Page": 1, "Save": "Save"})

    def enable_qos(self):
        self.get("QoSCfgRpm.htm", {"QoSCtrl": 1, "userWanType": 0, "up_bandWidth": TOTAL_UP_KBPS,
                                   "down_bandWidth": TOTAL_DOWN_KBPS, "Save": "Save"})

    def add_qos_rule(self, ip, down_kbps, up_kbps):
        self.get("QoSRuleListRpm.htm", {
            "enable": "true", "start_ip_addr": ip, "end_ip_addr": ip, "start_port": "", "end_port": "",
            "protocol": 0, "mode": 0, "min_up_band_width": 0, "max_up_band_width": up_kbps,
            "min_down_band_width": 0, "max_down_band_width": down_kbps,
            "curEditId": 0, "Page": 1, "Save": "Save"})

    def delete_qos_rule(self, rule):
        self.get("QoSRuleListRpm.htm", {"Del": rule["id"], "Page": rule["page"]})

    def enable_access_control(self):
        # defRule=0: "Deny the packets specified by any enabled access control policy";
        # everything not listed passes, so turning this on with no rules blocks nobody.
        self.get("AccessCtrlAccessRulesRpm.htm", {"enableCtrl": 1, "defRule": 0, "Page": 1})

    def add_block_host(self, name, mac):
        self.get("AccessCtrlHostsListsRpm.htm", {
            "addr_type": 0, "hosts_lists_name": name, "src_ip_start": "", "src_ip_end": "",
            "mac_addr": mac, "Changed": 0, "SelIndex": 0, "fromAdd": 0, "Page": 1, "Save": "Save"})

    def host_choices(self):
        """Host names in the order the add-rule form offers them (the form's option values)."""
        a = self.arrays(self.get("AccessCtrlAccessRulesRpm.htm", {"Add": "Add", "Page": 1}))
        return a.get("host_list_dyn_array", [])

    def add_block_rule(self, name, host_index):
        self.get("AccessCtrlAccessRulesRpm.htm", {
            "rule_name": name, "hosts_lists": host_index, "targets_lists": 255, "scheds_lists": 255,
            "enable": 1, "Changed": 0, "SelIndex": 0, "Page": 1, "Save": "Save"})

    def delete_rule(self, rule):
        self.get("AccessCtrlAccessRulesRpm.htm", {"Del": rule["pos"], "Page": rule["page"]})

    def delete_host(self, host):
        self.get("AccessCtrlHostsListsRpm.htm", {"Del": host["pos"], "Page": host["page"]})


# ------------------------------------------------------------------- state
LOCK = threading.Lock()
STATE = {"devices": {}, "error": None, "updated": None, "stats_on": None,
         "qos_on": None, "ac_on": None, "ac_deny_mode": None, "archer": None}
PREV = {}          # mac -> (t, bytes, pkts)
HIST = {}          # mac -> list of (t, up_bps, pps)
DPREV = {}         # mac -> (t, counter, src)   download counters (Wi-Fi units or this computer's bytes)
DHIST = {}         # mac -> list of (t, down_bps, down_pps or None, src)
RULES = {"qos": {}, "blocked": {}, "reserved": {}, "t": 0}
ACTION = {"busy": False, "text": "", "ok": None, "t": None}


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


USAGE = _load(USAGE_PATH, {})     # {"YYYY-MM-DD": {mac: {"up": bytes, "pkts": n, "active_s": s}}}
NAMES = _load(NAMES_PATH, {})     # {mac: "name typed by the user"}


def log_action(text):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + text
    with open(ACTION_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def today():
    return time.strftime("%Y-%m-%d")


def add_usage(mac, up_bytes, pkts, active_s):
    day = USAGE.setdefault(today(), {})
    u = day.setdefault(mac, {"up": 0, "pkts": 0, "active_s": 0})
    u["up"] += up_bytes
    u["pkts"] += pkts
    u["active_s"] += active_s


def add_down_usage(mac, down_bytes, src):
    day = USAGE.setdefault(today(), {})
    u = day.setdefault(mac, {"up": 0, "pkts": 0, "active_s": 0})
    u["down"] = u.get("down", 0) + int(down_bytes)
    if src == "estimate":
        u["down_est"] = True


def local_counters():
    """{mac: bytes received} for this computer's own network interfaces (exact)."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil:
        try:
            res = {}
            io = psutil.net_io_counters(pernic=True)
            for nic, addrs in psutil.net_if_addrs().items():
                for a in addrs:
                    if a.family == psutil.AF_LINK and nic in io:
                        mac = norm_mac(a.address)
                        if mac and mac != "00-00-00-00-00-00":
                            res[mac] = res.get(mac, 0) + io[nic].bytes_recv
            return res
        except Exception:
            return {}
    try:   # macOS without psutil
        out = subprocess.run(["netstat", "-ibn"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    res = {}
    for line in out.splitlines():
        f = line.split()
        # Name Mtu <Link#n> Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
        if len(f) >= 10 and f[2].startswith("<Link#") and re.fullmatch(r"[0-9a-fA-F:]{11,17}", f[3]):
            mac = norm_mac("".join(p.zfill(2) for p in f[3].split(":")))
            if mac and f[6].isdigit():
                res[mac] = res.get(mac, 0) + int(f[6])
    return res


def prune_usage():
    days = sorted(USAGE)
    for d in days[:-KEEP_DAYS]:
        USAGE.pop(d, None)


# ------------------------------------------------------------------- polling
class Poller:
    def __init__(self, cfg, house_down=None):
        self.house_down = house_down      # returns the whole-house download in bit/s, or None
        tp = cfg.get("tplink") or {}
        self.tp = TPLink(tp.get("host", "192.168.1.1"), tp.get("user", "admin"), tp.get("password", ""))
        self.archer_cfg = cfg.get("archer") or None
        self.archer = None
        self.archer_next = 0
        self.archer_fail = 0
        self.last_save = 0

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            t0 = time.time()
            try:
                self.poll_once()
                with LOCK:
                    STATE["error"] = None
            except Exception as e:   # router unreachable, wrong password, page changed ...
                with LOCK:
                    STATE["error"] = f"Could not read the TP-Link: {e}"
            time.sleep(max(1.0, POLL - (time.time() - t0)))

    def refresh_rules(self):
        qos_on, qos = self.tp.qos()
        ac_on, deny_mode, hosts, rules = self.tp.access_control()
        reserved = self.tp.reservations()
        blocked = {}
        for h in hosts:
            if h["name"].startswith(BLOCK_PREFIX) and h["mac"]:
                rule = next((r for r in rules if r["host"] == h["name"]), None)
                blocked[h["mac"]] = bool(rule and rule["enabled"] and ac_on and deny_mode)
        with LOCK:
            RULES.update(qos=qos, blocked=blocked, reserved=reserved, t=time.time(),
                         hosts=hosts, rules=rules)
            STATE.update(qos_on=qos_on, ac_on=ac_on, ac_deny_mode=deny_mode)

    def read_archer(self):
        """Wi-Fi clients of the Archer C20. Its login is encrypted; it is optional,
        so failures only back off and are shown on the page."""
        if not self.archer_cfg or time.time() < self.archer_next:
            return None
        try:
            import archer
            if self.archer is None:
                self.archer = archer.ArcherClient(self.archer_cfg.get("host", "192.168.1.103"),
                                                  self.archer_cfg.get("user", "admin"),
                                                  self.archer_cfg.get("password", ""))
                self.archer.login()
            macs = self.archer.wifi_clients()
            self.archer_fail = 0
            return {"ok": True, "macs": macs}
        except Exception as e:
            self.archer_fail += 1
            if self.archer is not None:
                try:
                    self.archer.logout()
                except Exception:
                    pass
            self.archer = None
            self.archer_next = time.time() + min(1800, 120 * self.archer_fail)
            return {"ok": False, "error": str(e)[:200]}

    def update_download(self, now, wifi, local, house_bps, devs):
        """Download rate per device (called with LOCK held).
        This computer: exact bytes from its own interface. TP-Link Wi-Fi devices: packets received
        x DOWN_PKT_BYTES; if those estimates add up to more than the whole-house download leaves
        after the exact ones, they are scaled down to fit."""
        rows = []   # (mac, bps, pps, src, dt)
        sources = {m: ("measured", c) for m, c in local.items() if m in devs}
        for m, c in wifi.items():
            if m and c is not None and m not in sources:
                sources[m] = ("estimate", c)
        for m, (src, c) in sources.items():
            prev = DPREV.get(m)
            DPREV[m] = (now, c, src)
            if not prev or prev[2] != src:
                continue
            dt, dc = now - prev[0], c - prev[1]
            if dc < 0 or not 0 < dt < 120:        # counter reset (reconnect, restart) or a long gap
                continue
            if src == "measured":
                rows.append([m, dc * 8 / dt, None, src, dt])
            else:
                pps = dc / WIFI_RX_SCALE / dt
                rows.append([m, pps * DOWN_PKT_BYTES * 8, pps, src, dt])
        if house_bps is not None:
            exact = sum(r[1] for r in rows if r[3] == "measured")
            est = sum(r[1] for r in rows if r[3] == "estimate")
            room = max(0.0, house_bps - exact)
            if est > room:
                for r in rows:
                    if r[3] == "estimate":
                        r[1] *= room / est
        for m, bps, pps, src, dt in rows:
            h = DHIST.setdefault(m, [])
            h.append((now, bps, pps, src))
            del h[:-HISTORY]
            add_down_usage(m, bps * dt / 8, src)

    def poll_once(self):
        now = time.time()
        clients = self.tp.dhcp_clients()
        wifi = self.tp.wifi_stations()
        stats_on, stats = self.tp.statistics()
        local = local_counters()
        house = None
        if self.house_down:
            try:
                house = self.house_down()
            except Exception:
                house = None
        if now - RULES["t"] > RULES_EVERY:
            self.refresh_rules()
        arch = self.read_archer()

        with LOCK:
            if arch is not None:
                STATE["archer"] = arch
            archer_macs = set((STATE.get("archer") or {}).get("macs") or [])
            devs = {}
            for c in clients:
                if c["mac"]:
                    devs[c["mac"]] = {"mac": c["mac"], "ip": c["ip"], "reported_name": c["name"],
                                      "lease": c["lease"]}
            by_ip = {d["ip"]: m for m, d in devs.items()}
            for s in stats:
                if not s["mac"] or s["mac"] == "00-00-00-00-00-00":   # the router did not learn it yet
                    s["mac"] = by_ip.get(s["ip"], "")
                    if not s["mac"]:
                        continue
                d = devs.setdefault(s["mac"], {"mac": s["mac"], "ip": s["ip"], "reported_name": "",
                                               "lease": ""})
                d["ip"] = s["ip"]
                prev = PREV.get(s["mac"])
                PREV[s["mac"]] = (now, s["bytes"], s["pkts"])
                if prev:
                    dt = now - prev[0]
                    db = s["bytes"] - prev[1]
                    dp = s["pkts"] - prev[2]
                    if db < 0 or dp < 0:            # counters were reset (router restart or Reset All)
                        db, dp = s["bytes"], s["pkts"]
                    if 0 < dt < 120:
                        up_bps, pps = db * 8 / dt, dp / dt
                        h = HIST.setdefault(s["mac"], [])
                        h.append((now, up_bps, pps))
                        del h[:-HISTORY]
                        add_usage(s["mac"], db, dp, dt if pps >= 2 else 0)
            self.update_download(now, wifi, local, house, devs)
            for mac, d in devs.items():
                h = [x for x in HIST.get(mac, []) if now - x[0] < 330]
                last = h[-1] if h and now - h[-1][0] < 3 * POLL else None
                d["up_bps"] = last[1] if last else 0
                d["pps"] = last[2] if last else 0
                d["up_bps_5m"] = sum(x[1] for x in h) / len(h) if h else 0
                d["pps_5m"] = sum(x[2] for x in h) / len(h) if h else 0
                u = USAGE.get(today(), {}).get(mac, {})
                d["up_today"] = u.get("up", 0)
                d["pkts_today"] = u.get("pkts", 0)
                d["active_today_s"] = u.get("active_s", 0)
                d["down_today"] = u.get("down", 0)
                dh = [x for x in DHIST.get(mac, []) if now - x[0] < 330]
                dlast = dh[-1] if dh and now - dh[-1][0] < 3 * POLL else None
                d["down_bps"] = dlast[1] if dlast else (0 if dh else None)
                d["down_pps"] = dlast[2] if dlast else None
                d["down_bps_5m"] = sum(x[1] for x in dh) / len(dh) if dh else None
                d["down_src"] = dh[-1][3] if dh else None   # "measured" | "estimate" | None
                d["name"] = NAMES.get(mac) or d["reported_name"] or d["ip"]
                d["kind"] = guess_kind(d["reported_name"] or d["name"])
                # The statistics keep a row for a device after it leaves, so "online" means it is
                # on one of the Wi-Fi lists or sent something in the last 5 minutes.
                d["online"] = mac in wifi or mac in archer_macs or any(x[2] > 0 for x in h)
                d["wifi"] = ("TP-Link Wi-Fi" if mac in wifi else
                             "Archer Wi-Fi" if mac in archer_macs else "")
                q = RULES["qos"].get(d["ip"])
                d["limit"] = ({"down": q["down"], "up": q["up"]} if q and q["enabled"]
                              and STATE.get("qos_on") else None)
                d["blocked"] = RULES["blocked"].get(mac, False)
                d["reserved_ip"] = RULES["reserved"].get(mac)
            STATE.update(devices=devs, updated=now, stats_on=stats_on)
            if now - self.last_save > 60:
                prune_usage()
                _save(USAGE_PATH, USAGE)
                self.last_save = now

        if not stats_on:
            # Statistics are switched off (for example after a TP-Link reset); they are needed
            # for the usage figures and turning them on does not affect anyone's connection.
            self.tp.enable_statistics()
            log_action("Traffic Statistics were off on the TP-Link; switched them on")

    def save_now(self):
        with LOCK:
            _save(USAGE_PATH, USAGE)


POLLER = None
NOT_SET_UP = ('The TP-Link router is not set up. To use this page, fill in the "tplink" part of '
              "config.json and restart Tower Monitor.")


def _filled(v):
    return bool(str(v or "").strip()) and str(v).strip() != "CHANGE_ME"


def start(cfg, house_down=None):
    """Start reading the TP-Link, if config.json has its password. Returns the poller or None."""
    global POLLER
    cfg = dict(cfg)
    if not _filled((cfg.get("tplink") or {}).get("password")):
        with LOCK:
            STATE["error"] = NOT_SET_UP
        return None
    if not _filled((cfg.get("archer") or {}).get("password")):
        cfg["archer"] = None          # the Archer is optional
    POLLER = Poller(cfg, house_down)
    POLLER.start()
    return POLLER


def snapshot():
    with LOCK:
        devs = sorted(STATE["devices"].values(),
                      key=lambda d: (-d.get("online", False), -d.get("pps_5m", 0), d.get("name", "").lower()))
        history = {d: {m: dict(v) for m, v in u.items()} for d, u in USAGE.items()}
        return {"devices": devs, "error": STATE["error"], "updated": STATE["updated"],
                "stats_on": STATE["stats_on"], "qos_on": STATE["qos_on"], "ac_on": STATE["ac_on"],
                "archer": {k: v for k, v in (STATE.get("archer") or {}).items() if k != "macs"} or None,
                "action": dict(ACTION), "usage_days": history, "names": dict(NAMES),
                "not_set_up": POLLER is None,
                "archer_set_up": bool(POLLER and POLLER.archer_cfg)}


# ------------------------------------------------------------------- actions
def _find_device(mac):
    with LOCK:
        return dict(STATE["devices"].get(mac) or {})


def _ensure_reserved(tp, mac, ip):
    res = tp.reservations()
    if mac in res:
        if res[mac] != ip:
            raise RuntimeError(f"this device has a reserved address {res[mac]} but is using {ip}; "
                               "restart its Wi-Fi first")
        return
    if ip in res.values():
        raise RuntimeError(f"{ip} is reserved for another device")
    tp.add_reservation(mac, ip)
    log_action(f"reserved {ip} for {mac}")


def do_limit(mac, down_kbps, up_kbps):
    tp = POLLER.tp
    d = _find_device(mac)
    ip = d.get("ip")
    if not ip:
        raise RuntimeError("device not found")
    _ensure_reserved(tp, mac, ip)
    asked_up = up_kbps
    up_kbps = upload_cap_for(down_kbps, up_kbps)
    qos_on, rules = tp.qos()
    if ip in rules:
        tp.delete_qos_rule(rules[ip])
    tp.add_qos_rule(ip, down_kbps, up_kbps)
    if not qos_on:
        tp.enable_qos()
        log_action(f"Bandwidth Control switched on (total {TOTAL_DOWN_KBPS}/{TOTAL_UP_KBPS} kbit/s)")
    qos_on, rules = tp.qos()
    r = rules.get(ip)
    if not (qos_on and r and r["enabled"] and int(r["down"]) == down_kbps and int(r["up"]) == up_kbps):
        raise RuntimeError(f"the TP-Link did not keep the limit (read back: {r}, control on: {qos_on})")
    log_action(f"limit {d.get('name')} {mac} {ip}: download {down_kbps} kbit/s (held by upload cap), "
               f"upload {up_kbps} kbit/s (asked {asked_up})")
    return (f"{d.get('name')} is now limited to about {down_kbps/1000:g} Mbit/s down "
            f"(upload capped at {up_kbps} kbit/s, which is what holds the download).")


def do_unlimit(mac):
    tp = POLLER.tp
    d = _find_device(mac)
    _, rules = tp.qos()
    r = rules.get(d.get("ip"))
    if not r:
        return f"{d.get('name')} had no limit."
    tp.delete_qos_rule(r)
    _, rules = tp.qos()
    if d.get("ip") in rules:
        raise RuntimeError("the TP-Link still lists the limit")
    log_action(f"removed limit {d.get('name')} {mac} {d.get('ip')}")
    return f"Limit removed from {d.get('name')}."


def do_block(mac):
    tp = POLLER.tp
    d = _find_device(mac)
    name = BLOCK_PREFIX + mac.replace("-", "")
    ac_on, deny_mode, hosts, rules = tp.access_control()
    if ac_on and not deny_mode:
        raise RuntimeError("Access Control on the TP-Link is in 'allow only listed' mode; "
                           f"this page will not change that. Change it on http://{tp.host} first.")
    if not any(h["name"] == name for h in hosts):
        tp.add_block_host(name, mac)
    choices = tp.host_choices()
    if name not in choices:
        raise RuntimeError("the TP-Link did not save the device entry")
    if not any(r["name"] == name for r in rules):
        tp.add_block_rule(name, choices.index(name))
    if not ac_on:
        tp.enable_access_control()
        log_action("Access Control switched on (mode: block only the listed devices)")
    ac_on, deny_mode, hosts, rules = tp.access_control()
    r = next((r for r in rules if r["name"] == name), None)
    if not (ac_on and deny_mode and r and r["enabled"] and r["host"] == name):
        raise RuntimeError(f"the block did not take effect (read back: {r}, control on: {ac_on})")
    log_action(f"block {d.get('name')} {mac} {d.get('ip')}")
    return f"{d.get('name')} is blocked from the internet."


def do_unblock(mac):
    tp = POLLER.tp
    d = _find_device(mac)
    name = BLOCK_PREFIX + mac.replace("-", "")
    _, _, hosts, rules = tp.access_control()
    r = next((r for r in rules if r["name"] == name), None)
    if r:
        tp.delete_rule(r)
    _, _, hosts, rules = tp.access_control()
    h = next((h for h in hosts if h["name"] == name), None)
    if h:
        tp.delete_host(h)
    _, _, hosts, rules = tp.access_control()
    if any(r["name"] == name for r in rules):
        raise RuntimeError("the TP-Link still lists the block rule")
    log_action(f"unblock {d.get('name')} {mac} {d.get('ip')}")
    return f"{d.get('name')} can use the internet again."


def set_name(mac, name):
    name = (name or "").strip()[:40]
    with LOCK:
        if name:
            NAMES[mac] = name
        else:
            NAMES.pop(mac, None)
        _save(NAMES_PATH, NAMES)
        if mac in STATE["devices"]:
            d = STATE["devices"][mac]
            d["name"] = name or d["reported_name"] or d["ip"]


def run_action(kind, mac, down=None, up=None):
    if POLLER is None:
        return NOT_SET_UP
    with LOCK:
        if ACTION["busy"]:
            return "Another change is still running."
        ACTION.update(busy=True, text="Working…", ok=None, t=time.time())

    def work():
        try:
            if kind == "limit":
                msg = do_limit(mac, down, up)
            elif kind == "unlimit":
                msg = do_unlimit(mac)
            elif kind == "block":
                msg = do_block(mac)
            elif kind == "unblock":
                msg = do_unblock(mac)
            else:
                raise RuntimeError("unknown action")
            ok = True
        except Exception as e:
            msg, ok = f"Failed: {e}", False
            log_action(f"{kind} {mac} FAILED: {e}")
        try:
            POLLER.refresh_rules()
        except Exception:
            pass
        with LOCK:
            ACTION.update(busy=False, text=msg, ok=ok, t=time.time())

    threading.Thread(target=work, daemon=True).start()
    return None
