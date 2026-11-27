"""Share a public Wi-Fi (the kind with a login card) with other devices, from this computer.

Windows: Mobile Hotspot can share the Wi-Fi this computer is on, over the same Wi-Fi card
(when the card supports it, as most do). This module reads Windows' own internet check and
switches Mobile Hotspot on and off through the same Windows API the Settings app uses,
via Windows PowerShell 5.1 (no administrator needed).

macOS: Internet Sharing cannot send a Wi-Fi connection back out over the same Wi-Fi card;
it can share it to devices on a cable (Ethernet, USB, Thunderbolt). This module checks for
internet / a sign-in page and opens the right System Settings pages.
"""
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import activity

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_PS_LOCK = threading.Lock()
_LAST_STATE = {}

# Windows PowerShell 5.1 (not PowerShell 7) can call Windows Runtime APIs directly.
_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$action = '__ACTION__'
$out = [ordered]@{ ok = $true }
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
  function Await($op, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    if (-not $task.Wait(30000)) { throw 'Windows did not answer in time.' }
    $task.Result
  }
  $null = [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType = WindowsRuntime]
  $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType = WindowsRuntime]
  $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult, Windows.Networking.NetworkOperators, ContentType = WindowsRuntime]

  $prof = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
  if ($prof) {
    $out.level = $prof.GetNetworkConnectivityLevel().ToString()
    $out.wlan = [bool]$prof.IsWlanConnectionProfile
    if ($prof.IsWlanConnectionProfile) {
      try { $out.ssid = $prof.WlanConnectionProfileDetails.GetConnectedSsid() } catch { }
    }
    try {
      $out.capability = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::GetTetheringCapabilityFromConnectionProfile($prof).ToString()
    } catch { $out.capability = 'Unknown' }
    try {
      $m = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($prof)
      $type = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]
      if ($action -eq 'start') {
        $r = Await ($m.StartTetheringAsync()) $type
        $out.result = $r.Status.ToString(); $out.message = $r.AdditionalErrorMessage
      } elseif ($action -eq 'stop') {
        $r = Await ($m.StopTetheringAsync()) $type
        $out.result = $r.Status.ToString(); $out.message = $r.AdditionalErrorMessage
      }
      $out.state = $m.TetheringOperationalState.ToString()
      $out.clients = $m.ClientCount
      $out.max_clients = $m.MaxClientCount
      $c = $m.GetCurrentAccessPointConfiguration()
      $out.hotspot_ssid = $c.Ssid
      $out.hotspot_password = $c.Passphrase
    } catch { $out.manager_error = $_.Exception.Message }
  } else {
    $out.level = 'None'
  }
} catch {
  $out.ok = $false
  $out.error = $_.Exception.Message
}
$out | ConvertTo-Json -Compress
"""


def _powershell(action="status"):
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       r"System32\WindowsPowerShell\v1.0\powershell.exe")
    script = _PS_SCRIPT.replace("__ACTION__", action)
    with _PS_LOCK:
        try:
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                "-EncodedCommand", encoded], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=60, creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "error": f"Could not run Windows PowerShell ({e})."}
    lines = [x for x in (p.stdout or "").splitlines() if x.strip().startswith("{")]
    if not lines:
        return {"ok": False, "error": ((p.stderr or "").strip() or "Windows PowerShell gave no answer.")[:2000]}
    try:
        return json.loads(lines[-1])
    except ValueError:
        return {"ok": False, "error": lines[-1][:2000]}


# Windows' own internet check (the same one behind the globe icon on the taskbar).
_LEVEL = {"InternetAccess": "ready", "ConstrainedInternetAccess": "signIn",
          "LocalAccess": "limited", "None": "disconnected"}


def _probe_mac():
    """Apple's own captive-portal check page: "Success" means the internet works."""
    try:
        req = urllib.request.Request("http://captive.apple.com/hotspot-detect.html",
                                     headers={"User-Agent": "CaptiveNetworkSupport"})
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read(4096).decode("utf-8", "replace")
        return "ready" if "Success" in body else "signIn"
    except OSError:
        return "disconnected"


def status(action="status"):
    """What the hotspot page shows. action "start"/"stop" switches Windows Mobile Hotspot."""
    if os.name == "nt":
        r = _powershell(action)
        out = {"platform": "windows", "can_control": True}
        if not r.get("ok"):
            out.update(internet="unknown", error=r.get("error"))
            if action != "status":
                activity.add("hotspot", "Could not use Mobile Hotspot", "problem", r.get("error"))
            return out
        state = r.get("state")
        out.update(
            internet=_LEVEL.get(r.get("level"), "unknown"),
            wifi=bool(r.get("wlan")), ssid=r.get("ssid") or None,
            capability=r.get("capability"),
            hotspot={"on": state == "On", "state": state, "clients": r.get("clients"),
                     "max_clients": r.get("max_clients"), "ssid": r.get("hotspot_ssid"),
                     "password": r.get("hotspot_password")} if state else None,
            manager_error=r.get("manager_error"),
        )
        if action != "status":
            result = r.get("result") or r.get("manager_error") or "?"
            ok = r.get("result") == "Success"
            words = "Mobile Hotspot turned on" if action == "start" else "Mobile Hotspot turned off"
            if ok:
                activity.add("hotspot", words, "change",
                             f"shared from: {out['ssid'] or ('Wi-Fi' if out['wifi'] else 'a cable')}")
            else:
                activity.add("hotspot", "Windows refused to switch Mobile Hotspot", "problem",
                             f"{action}: {result} {r.get('message') or ''}".strip())
            out["result"] = {"ok": ok, "status": result, "message": r.get("message") or ""}
    elif sys.platform == "darwin":
        out = {"platform": "mac", "can_control": False, "internet": _probe_mac()}
    else:
        out = {"platform": "other", "can_control": False, "internet": "unknown"}
    _note_internet(out.get("internet"))
    return out


def _note_internet(state):
    if not state or _LAST_STATE.get("internet") == state:
        return
    first = "internet" not in _LAST_STATE
    _LAST_STATE["internet"] = state
    text = {"ready": "Internet works on this connection",
            "signIn": "This Wi-Fi needs you to sign in",
            "limited": "Connected, but no internet yet",
            "disconnected": "Not connected to a network",
            "unknown": "Could not check the internet"}.get(state, state)
    if first and state == "ready":
        level = "info"
    else:
        level = "change" if state == "ready" else "problem"
    activity.add("hotspot", text, level, quiet_key=f"internet:{state}")


SETTINGS = {
    "windows": {
        "wifi": "ms-settings:network-wifi",
        "hotspot": "ms-settings:network-mobilehotspot",
        "signin": "http://www.msftconnecttest.com/redirect",
    },
    "mac": {
        "wifi": "x-apple.systempreferences:com.apple.wifi-settings-extension",
        "hotspot": "x-apple.systempreferences:com.apple.Sharing-Settings.extension",
        "signin": "http://captive.apple.com/",
    },
}


def open_page(what):
    """Open Wi-Fi settings, hotspot/sharing settings, or the public Wi-Fi's sign-in page."""
    plat = "windows" if os.name == "nt" else "mac" if sys.platform == "darwin" else None
    target = (SETTINGS.get(plat) or {}).get(what)
    if not target:
        return False
    try:
        if os.name == "nt":
            os.startfile(target)  # noqa: S606  (a fixed Settings page or Microsoft's check page)
        else:
            subprocess.Popen(["open", target])
        return True
    except OSError as e:
        activity.add("hotspot", "Could not open Settings", "problem", f"{what}: {e}")
        return False


def api(path, body):
    """Handle /api/hotspot/<path>."""
    if path == "status":
        return status()
    if path in ("start", "stop"):
        if os.name != "nt":
            return {"ok": False}
        return status(path)
    if path == "open":
        what = str(body.get("what") or "")
        if what not in ("wifi", "hotspot", "signin"):
            return {"ok": False}
        return {"ok": open_page(what)}
    return {"ok": False}


if __name__ == "__main__":
    t = time.time()
    print(json.dumps(status(sys.argv[1] if len(sys.argv) > 1 else "status"), indent=1))
    print(f"{time.time() - t:.1f} s")
