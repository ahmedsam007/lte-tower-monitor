"""Minimal client for TP-Link 'GDPR-encrypted' web UI (Archer C20 v5+ style).

Login and data requests are AES-128-CBC encrypted (random key/iv made of digits),
signed with a 512-bit RSA key fetched from /cgi/getParm (no padding, 64-byte chunks).
AES uses the `cryptography` package (installed with paramiko); without it, the openssl
command line tool is used.
"""
import base64, hashlib, random, re, subprocess, time, urllib.request, http.cookiejar

try:
    from cryptography.hazmat.primitives import padding as _pad
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = None


def _aes(data: bytes, key: str, iv: str, decrypt=False) -> bytes:
    if Cipher is not None:
        c = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode()))
        if decrypt:
            d = c.decryptor()
            u = _pad.PKCS7(128).unpadder()
            return u.update(d.update(data) + d.finalize()) + u.finalize()
        p = _pad.PKCS7(128).padder()
        e = c.encryptor()
        return e.update(p.update(data) + p.finalize()) + e.finalize()
    cmd =["openssl", "enc", "-aes-128-cbc", "-K", key.encode().hex(), "-iv", iv.encode().hex()]
    if decrypt:
        cmd.append("-d")
    p = subprocess.run(cmd, input=data, capture_output=True, timeout=10)
    if p.returncode:
        raise RuntimeError("openssl: " + p.stderr.decode(errors="replace"))
    return p.stdout


def _rsa_nopad(text: str, nn: str, ee: str) -> str:
    n, e = int(nn, 16), int(ee, 16)
    out = ""
    raw = text.encode()
    for i in range(0, len(raw), 64):
        chunk = raw[i:i + 64].ljust(64, b"\0")
        out += format(pow(int.from_bytes(chunk, "big"), e, n), "0128x")
    return out


class ArcherClient:
    def __init__(self, host, user, password, timeout=10):
        self.host, self.user, self.password, self.timeout = host, user, password, timeout
        self.base = f"http://{host}"
        self.token = None

    def _open(self, path, body=None, headers=None):
        h = {"Referer": self.base + "/", "Content-Type": "text/plain"}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=body, headers=h,
                                     method="POST" if body is not None else "GET")
        with self.opener.open(req, timeout=self.timeout) as r:
            return r.read()

    def login(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        parm = self._open("/cgi/getParm", b"").decode()
        self.ee = re.search(r'ee="(\w+)"', parm).group(1)
        self.nn = re.search(r'nn="(\w+)"', parm).group(1)
        self.seq = int(re.search(r'seq="(\d+)"', parm).group(1))
        self.hash = hashlib.md5((self.user + self.password).encode()).hexdigest()
        stamp = str(int(time.time() * 1000))
        self.key = (stamp + str(random.random() * 1e9))[:16]
        self.iv = (stamp + str(random.random() * 1e9))[:16]
        body = ("8\r\n[/cgi/login#0,0,0,0,0,0#0,0,0,0,0,0]0,2\r\n"
                f"username={self.user}\r\npassword={self.password}\r\n")
        resp = self._post_gdpr(body, login=True)
        if "$.ret=0" not in resp:
            raise RuntimeError("Archer login failed: " + resp[:200])
        page = self._open("/").decode(errors="replace")
        m = re.search(r'var token="([^"]+)"', page)
        self.token = m.group(1) if m else None  # Archer C20 v5 firmware has no token

    def _post_gdpr(self, plain, login=False):
        data = base64.b64encode(_aes(plain.encode(), self.key, self.iv)).decode()
        s = self.seq + len(data)
        if login:
            sig_text = f"key={self.key}&iv={self.iv}&h={self.hash}&s={s}"
        else:
            sig_text = f"h={self.hash}&s={s}"
        sign = _rsa_nopad(sig_text, self.nn, self.ee)
        body = f"sign={sign}\r\ndata={data}\r\n".encode()
        headers = {"TokenID": self.token} if self.token else {}
        raw = self._open("/cgi_gdpr", body, headers).decode().strip()
        return _aes(base64.b64decode(raw), self.key, self.iv, decrypt=True).decode(errors="replace")

    def query(self, actions):
        """actions: list of (act_type, oid, attrs_list[, stack[, pstack]]). Returns the
        decrypted response text (parse with parse_objects)."""
        heads, blocks = [], []
        for i, a in enumerate(actions):
            t, oid, attrs = a[:3]
            stack = a[3] if len(a) > 3 else "0,0,0,0,0,0"
            pstack = a[4] if len(a) > 4 else "0,0,0,0,0,0"
            heads.append(str(t))
            blocks.append(f"[{oid}#{stack}#{pstack}]{i},{len(attrs)}\r\n" +
                          "".join(a + "\r\n" for a in attrs))
        plain = "&".join(heads) + "\r\n" + "".join(blocks)
        return self._post_gdpr(plain)

    def logout(self):
        try:
            self.query([(8, "/cgi/logout", [])])
        except Exception:
            pass
        self.token = None


def parse_objects(text):
    """Parse '[1,0,0,0,0,0]0\\nkey=val\\n...' response into list of (stackid, idx, dict)."""
    out, cur = [], None
    for line in text.splitlines():
        m = re.match(r"\[([\d,]+)\](\d+)$", line)
        if m:
            cur = {}
            out.append((m.group(1), int(m.group(2)), cur))
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k] = v
    return out


def _wifi_clients(self):
    """MAC addresses (AA-BB-CC-DD-EE-FF) of devices on the Archer's Wi-Fi, both bands."""
    radios = parse_objects(self.query([(5, "LAN_WLAN", [])]))
    macs = set()
    for stack, _, _ in radios:
        self.query([(7, "ACT_WLAN_UPDATE_ASSOC", [], stack)])
        r = self.query([(6, "LAN_WLAN_ASSOC_DEV", ["AssociatedDeviceMACAddress"], "0,0,0,0,0,0", stack)])
        for _, _, d in parse_objects(r):
            m = re.sub(r"[^0-9A-Fa-f]", "", d.get("AssociatedDeviceMACAddress", "")).upper()
            if len(m) == 12:
                macs.add("-".join(m[i:i + 2] for i in range(0, 12, 2)))
    return sorted(macs)


ArcherClient.wifi_clients = _wifi_clients
