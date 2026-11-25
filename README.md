# Tower Monitor

A live dashboard for a **D-Link DWR-921** 4G/LTE router (with its built-in BroadMobi BM806C modem), plus a page for seeing and controlling the devices on your home network.

It runs on your own computer (Windows or macOS), talks to your routers over your home network, and shows everything in your web browser. Nothing is sent to the internet. Because it only uses your home network, it keeps working while the internet is down. That's when you need it most.

## What it shows

**Tower page** (http://127.0.0.1:8765)

- Which cell tower the modem is connected to, and every other tower it can hear, with signal strength (RSRP, RSRQ, SINR)
- Charts of signal, tower switches, ping loss, and download/upload speed, for the last 15 minutes up to 24 hours
- A list of events: tower switches, internet going down or coming back, band changes
- **Tower lock** (optional): keeps the modem on one tower you choose, so it stops jumping to a worse one. If the locked tower doesn't work, the lock is removed again automatically. See [Tower lock](#tower-lock) before you use it.
- Distance to the tower, if you enter your location (or look the tower up on OpenCelliD with your own free key)

**Devices page** (http://127.0.0.1:8765/devices) needs a TP-Link router behind the D-Link:

- Every device on the network, how busy it is now, and how much it used today
- **Limit** a device's speed, or **Block** it from the internet, with one button. Undo works the same way.

## What you need

| | |
|---|---|
| Computer | Windows 10/11 or macOS, on the same network as the router |
| Python | 3.9 or newer from [python.org](https://www.python.org/downloads/) |
| Router | D-Link DWR-921 with **SSH turned on** (see below) |
| Devices page (optional) | TP-Link router with the older blue web interface (tested on TL-WR841HP, firmware 3.15.2). An Archer C20 access point is supported optionally for its Wi-Fi list. |

> Tested with one DWR-921 (firmware 4.0.2) and a BM806C modem. Other firmware or modems may answer differently. If a reading stays empty, please open an issue and include what the page shows.

## Install and start

1. **Download** this project: the green **Code** button, then **Download ZIP**. Unzip it somewhere you'll keep it, such as Documents.
2. **Install Python** if you don't have it, from [python.org](https://www.python.org/downloads/).
   *Windows:* tick **"Add python.exe to PATH"** on the first screen of the installer.
3. **Start it** by double-clicking:
   - Windows: `start-windows.bat`
   - macOS: `start-mac.command`. If macOS says it can't be opened, right-click it, choose **Open**, then **Open** again. Or, in Terminal: `sh start-mac.command`.

   The first start installs two small Python packages (this needs internet, one time only). It then creates `config.json` and stops.
4. **Open `config.json`** in a text editor (Notepad, TextEdit), put in your router's password, and save.
5. **Start it again.** Your browser opens the dashboard. Keep the black window open while you use it; closing it stops Tower Monitor.

## Turn on SSH on the DWR-921

Tower Monitor reads the modem through the router's SSH login. On the router's web page (usually http://192.168.0.1), find the remote access or administration settings and enable **SSH** for the local network (LAN). The menu name depends on the firmware version. The username and password are the ones you use to log in to the router's web page.

Only enable it for the LAN, never for the internet (WAN) side.

## config.json

| Setting | What it is |
|---|---|
| `host` | The DWR-921's address. Default `192.168.0.1` |
| `user`, `password` | The DWR-921 login |
| `at_port` | The modem's command port on the router. Leave as `/tmp/dev/ttyUSB1` unless readings stay empty |
| `poll_seconds` | How often to read the modem (minimum 3) |
| `listen_port` | The port for the dashboard (`8765`) |
| `opencellid_key`, `mcc`, `mnc` | Optional. A free key from [opencellid.org](https://opencellid.org) to find tower positions on the map, and your carrier's mobile country/network code. Leave the key empty to turn it off. When it's on, the tower's ID is sent to OpenCelliD. |
| `tplink` | Optional. Address and login of the TP-Link router, for the Devices page. Leave the password empty to switch the page off. |
| `archer` | Optional. Address and login of an Archer C20 access point. Leave the password empty if you don't have one. |
| `ssh_port` | Optional. Add this if your router's SSH isn't on port 22. |

`config.json` holds your passwords. It's listed in `.gitignore` so it never gets uploaded if you fork this project, but don't share the file itself.

## Tower lock

This is the only part that changes the modem's settings, and only when you press **Lock** or **Remove lock**.

1. You press **Lock** next to a tower. Tower Monitor stores that tower in the modem and switches the lock on.
2. The lock starts when the modem restarts, so the page asks you to **restart the router** (Reboot on its web page, or unplug it for 10 seconds).
3. After the restart, it watches the connection. If the modem can't get online on that tower within a few minutes, it **removes the lock by itself** and asks for one more restart.

Every step, with the modem's raw answers, is written to `lock_actions.log`.

To try the buttons without changing anything, run in dry-run mode (it uses port 8766, and no lock commands are sent):

```
start-windows.bat --dry-run          (Windows)
sh start-mac.command --dry-run       (macOS)
```

Don't run two copies at the same time against the same router: they garble each other's modem readings.

## Limits and blocks on the TP-Link

- **Block** adds an Access Control rule named `blk-<device>` on the TP-Link. The device stays on Wi-Fi but can't reach the internet.
- **Limit** gives the device a fixed address and a Bandwidth Control rule. On the tested TP-Link firmware, the router **ignores the download limit** and only enforces the upload limit. Tower Monitor therefore sets a low *upload* cap, which slows downloads indirectly. The download speed you choose is a target, kept only roughly. Video calls and games are affected less.
- Phones and tablets that use a **private (random) Wi-Fi address** look like a new device after reconnecting, so a limit or block stops applying to them. On that device, set the Wi-Fi network's private address to **Off** or **Fixed**.

## Your data stays on your computer

While it runs, Tower Monitor saves these files next to the program. They're all in `.gitignore`.

| File | Contains |
|---|---|
| `tower_log.csv` | Signal readings over time |
| `locations.json` | The location you typed in, and tower positions |
| `device_usage.json`, `device_names.json` | Daily usage per device, and the names you gave them |
| `lock_actions.log`, `device_actions.log` | Every change made to the modem or the TP-Link |

Delete them whenever you like; Tower Monitor starts fresh.

## Troubleshooting

- **"Router not reachable"**: check that the computer is on the router's network, SSH is enabled, and the password in `config.json` is right. If you started and stopped Tower Monitor several times quickly, the router may refuse new SSH logins for about 5 minutes; wait and it recovers.
- **The page loads but the readings are empty**: your modem may use a different command port. Try `/tmp/dev/ttyUSB2` for `at_port`.
- **Installing fails with "conflicting dependencies"**: the computer has a free-threaded Python (shown as 3.13t). The packages Tower Monitor needs don't support it. `start-windows.bat` skips it when a normal Python is also installed; otherwise install the normal Python from python.org.
- **Reinstall the packages**: delete the `.venv` folder and start again.
- **Run it by hand**: `python3 -m pip install --only-binary=:all: -r requirements.txt`, then `python3 server.py` (Windows: `py -3` instead of `python3`).

## License

MIT. See [LICENSE](LICENSE). Use it at your own risk: changing modem settings can leave you without internet until the router is restarted.
