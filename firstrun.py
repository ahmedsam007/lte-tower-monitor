"""First-run setup for Tower Monitor, shown as a page instead of editing config.json.

It finds the D-Link on the network, checks the password the user types, switches the
router's SSH on through the router's own web interface if it is off, tests the SSH
login and the modem, and then writes config.json.

The only change it can make to the router is switching SSH on for the local network,
the same change as ticking SSH on the router's System > Telnet/SSH page. Everything
stays on the local network; nothing is sent to the internet.
"""
import concurrent.futures
import errno
import http.cookiejar
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("TOWER_MONITOR_DATA") or HERE
CONFIG_PATH = os.path.join(DATA, "config.json")
COMMON_ADDRESSES = ["192.168.0.1", "192.168.1.1", "192.168.8.1", "192.168.0.254", "192.168.1.254",
                    "192.168.2.1", "10.0.0.1", "10.0.0.138"]
SSH_PAGE = "/admin/index.html#/system/telnet"     # where SSH is switched on by hand
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # keep Windows from flashing a console window


def _filled(v):
    return bool(str(v or "").strip()) and str(v).strip() != "CHANGE_ME"


def read_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def needed():
    """True when there is no usable config.json yet."""
    cfg = read_config()
    return not (cfg and _filled(cfg.get("password")) and _filled(cfg.get("host")))


# ----------------------------------------------------------------- finding the router
def gateways():
    """Default-gateway addresses from the routing table (macOS and Windows)."""
    try:
        out = subprocess.run(["netstat", "-rn"], capture_output=True, text=True, timeout=6,
                             creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] in ("default", "0.0.0.0"):
            for p in parts[1:3]:
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", p) and p != "0.0.0.0" and p not in found:
                    found.append(p)
    return found


def _get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(200000).decode("utf-8", "replace")


def probe(ip):
    """What answers at this address: {"ip", "model"} for a D-Link, {"ip"} for another web page, or None."""
    try:
        text = _get(f"http://{ip}/autoconf.js")
        m = re.search(r'DLINK_FACTORY_NAME:\s*"([^"]+)"', text)
        if m:
            return {"ip": ip, "model": m.group(1)}
    except (urllib.error.HTTPError,):
        pass
    except (OSError, ValueError):
        return None
    try:
        _get(f"http://{ip}/")
        return {"ip": ip}
    except urllib.error.HTTPError:
        return {"ip": ip}
    except (OSError, ValueError):
        return None


def find_routers():
    cfg = read_config() or {}
    candidates = []
    for ip in ([cfg.get("host")] if _filled(cfg.get("host")) else []) + gateways() + COMMON_ADDRESSES:
        if ip not in candidates:
            candidates.append(ip)
    with concurrent.futures.ThreadPoolExecutor(len(candidates)) as pool:
        results = [r for r in pool.map(probe, candidates) if r]
    dlinks = [r for r in results if r.get("model")]
    dlinks.sort(key=lambda r: (not r["model"].upper().startswith("DWR"), candidates.index(r["ip"])))
    others = [r["ip"] for r in results if not r.get("model")]
    return {"routers": dlinks, "others": others, "saved_host": cfg.get("host") if _filled(cfg.get("host")) else None}


# ------------------------------------------------------- the D-Link's web interface
class WebLoginError(Exception):
    pass


class DlinkWeb:
    """The DWR-921's own web interface (JSON-RPC at /cpe), used only to read and switch on SSH."""

    def __init__(self, host):
        self.host = host
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.token = None

    def _post(self, path, obj, timeout=20):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Cookie"] = "; ".join([f"DMSD-Access-Token={self.token}"] +
                                          [f"{c.name}={c.value}" for c in self.jar])
        req = urllib.request.Request(f"http://{self.host}{path}", json.dumps(obj).encode(), headers)
        with self.opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def login(self, user, password):
        try:
            res = self._post("/login?", {"login": user, "password": password, "staysigned": False})
        except urllib.error.HTTPError as e:
            raise WebLoginError("unsupported") from e
        except ValueError as e:
            raise WebLoginError("unsupported") from e
        err = res.get("error")
        if err:
            wait = err.get("banTimeRemain")
            left = err.get("tryCountRemain")
            if wait:
                raise WebLoginError(f"The router paused logins after wrong passwords. "
                                    f"Wait {int(wait)} seconds, then try again.")
            msg = "The router says the username or password is wrong."
            if isinstance(left, int):
                msg += f" {left} tries left before the router pauses logins for a while."
            raise WebLoginError(msg)
        token = (res.get("result") or {}).get("AccessToken")
        if not token:
            raise WebLoginError("unsupported")
        self.token = token

    def rpc(self, method, params):
        res = self._post("/cpe", {"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
        if "error" in res:
            raise RuntimeError(res["error"].get("message") or "router error")
        return res.get("result") or {}

    def ssh_settings(self):
        """{"Enable": bool, "Port": int}. The router's private SSH key comes in the same answer; it is dropped here."""
        params = self.rpc("GetParameterValues", {"ParameterNames": ["Device.Services.SSH."]}).get("ParameterList", [])
        out = {}
        for p in params:
            name = p.get("Name", "").rsplit(".", 1)[-1]
            if name in ("Enable", "Port", "Status"):
                out[name] = p.get("Value")
        return out

    def enable_ssh(self, port):
        # The same change the router's System > Telnet/SSH page sends when SSH is ticked.
        self.rpc("SetParameterValues", {"ParameterList": [
            {"Name": "Device.Services.SSH.Enable", "Value": True},
            {"Name": "Device.Services.SSH.Port", "Value": int(port)}], "NeedSaveConfig": True})


def port_open(host, port, timeout=3):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------- SSH check
def ssh_check(host, port, user, password):
    """Log in over SSH and find the modem's command port. Returns (at_port, None) or (None, message)."""
    try:
        import paramiko
    except ImportError:
        return None, "A required package (paramiko) is missing."

    class AcceptAnyKey(paramiko.MissingHostKeyPolicy):
        # The router makes a new SSH key on every boot, so it cannot be pinned.
        def missing_host_key(self, client, hostname, key):
            pass

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(AcceptAnyKey())
    try:
        c.connect(host, port=int(port), username=user, password=password, timeout=20, banner_timeout=30,
                  auth_timeout=20, look_for_keys=False, allow_agent=False)
        _in, out, _err = c.exec_command("ls /tmp/dev/ /dev/ 2>/dev/null", timeout=20)
        names = set(out.read().decode("utf-8", "replace").split())
    except paramiko.AuthenticationException:
        return None, "SSH is on, but the router did not accept this username and password for SSH."
    except Exception as e:  # noqa: BLE001 (any network problem ends the same way for the user)
        return None, f"Could not log in over SSH ({e or type(e).__name__})."
    finally:
        c.close()
    for dev in ("ttyUSB1", "ttyUSB2"):     # ttyUSB1 first: the router's own tools poll ttyUSB2
        if dev in names:
            return f"/tmp/dev/{dev}", None
    return "/tmp/dev/ttyUSB1", None


# ----------------------------------------------------------------------- connect
def connect(body):
    host = str(body.get("host") or "").strip()
    user = str(body.get("user") or "admin").strip() or "admin"
    password = str(body.get("password") or "")
    if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
        return {"ok": False, "step": "host", "message": "Type the router's address, for example 192.168.0.1."}
    if not password:
        return {"ok": False, "step": "password", "message": "Type the router's password."}
    steps = []
    page = f"http://{host}{SSH_PAGE}"
    ssh_port = 22
    web = DlinkWeb(host)
    web_ok = False
    try:
        web.login(user, password)
        web_ok = True
        steps.append("Logged in to the router")
    except WebLoginError as e:
        if str(e) != "unsupported":
            return {"ok": False, "step": "password", "message": str(e), "steps": steps}
    except OSError:
        if not port_open(host, 22):
            return {"ok": False, "step": "host", "steps": steps,
                    "message": f"Nothing answers at {host}. Check the address, and that this computer is "
                               "connected to the router's Wi-Fi or cable."}

    if web_ok:
        try:
            ssh = web.ssh_settings()
            ssh_port = int(ssh.get("Port") or 22)
            if not ssh.get("Enable"):
                web.enable_ssh(ssh_port)
                steps.append("Switched SSH on")
                for _ in range(20):
                    if port_open(host, ssh_port, 2):
                        break
                    time.sleep(1)
            else:
                steps.append("SSH is already on")
        except (RuntimeError, OSError, ValueError):
            pass   # fall through: the port check below says whether SSH is usable

    if not port_open(host, ssh_port, 4):
        return {"ok": False, "step": "ssh_off", "steps": steps, "router_page": page,
                "message": "SSH is off on the router, and Tower Monitor could not switch it on by itself."}

    at_port, err = ssh_check(host, ssh_port, user, password)
    if err:
        return {"ok": False, "step": "ssh_login", "message": err, "steps": steps, "router_page": page}
    steps.append("Connected to the modem")

    cfg = read_config() or {}
    if not cfg:
        try:
            with open(os.path.join(HERE, "config.example.json"), encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
    cfg.update({"host": host, "user": user, "password": password, "at_port": cfg.get("at_port") or at_port})
    if ssh_port != 22:
        cfg["ssh_port"] = ssh_port
    else:
        cfg.pop("ssh_port", None)
    os.makedirs(DATA, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    if os.name != "nt":
        os.chmod(CONFIG_PATH, 0o600)     # it holds the router password
    steps.append("Saved")
    return {"ok": True, "steps": steps}


# ------------------------------------------------------------------------ HTTP
def page():
    with open(os.path.join(HERE, "setup.html"), "rb") as f:
        return f.read()


def same_origin(headers):
    """Only this computer's own page may use the setup API: not other web pages, not other machines."""
    local = ("127.0.0.1", "localhost")
    host = (headers.get("Host") or "").split(":")[0]
    origin = headers.get("Origin")
    return host in local and (not origin or urlparse(origin).hostname in local)


def api(path, body, running):
    """Handle /api/setup/<path>. Returns a dict to send as JSON."""
    if path == "status":
        return {"running": running, "needed": needed(), "data_dir": DATA}
    if path == "find":
        return find_routers()
    if path == "connect":
        return connect(body)
    return {"ok": False, "message": "unknown"}


def local_server(port, handler, *, reuse=None):
    """Bind to localhost. The launcher can reuse an existing monitor or use a free port."""
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as e:
        address_in_use = (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE))
        if reuse is None or e.errno not in address_in_use:
            raise
        if reuse(port):
            return None
        return ThreadingHTTPServer(("127.0.0.1", 0), handler)


def run_until_done(port, on_ready=None, *, reuse=None):
    """Serve setup until config.json is saved and return the port used for setup."""
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = urlparse(self.path).path
            if p.startswith("/api/setup/"):
                if not same_origin(self.headers):
                    return self._json({"ok": False, "message": "not allowed"}, 403)
                return self._json(api(p[len("/api/setup/"):], {}, False))
            body = page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            p = urlparse(self.path).path
            if not p.startswith("/api/setup/") or not same_origin(self.headers):
                return self._json({"ok": False, "message": "not allowed"}, 403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n < 65536 else {}
            except ValueError:
                body = {}
            res = api(p[len("/api/setup/"):], body, False)
            self._json(res)
            if p.endswith("/connect") and res.get("ok"):
                done.set()

    srv = local_server(port, Handler, reuse=reuse)
    if srv is None:
        return None
    with srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            if on_ready:
                on_ready(f"http://127.0.0.1:{srv.server_port}/setup")
            done.wait()
            time.sleep(0.5)          # let the last answer reach the page
        finally:
            srv.shutdown()
    return srv.server_port


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    import webbrowser
    run_until_done(port, on_ready=webbrowser.open)
    print("Saved", CONFIG_PATH)
