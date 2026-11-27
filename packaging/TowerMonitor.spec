# PyInstaller recipe for the Tower Monitor app (macOS .app; Windows .exe folder).
# Build:  pyinstaller --noconfirm packaging/TowerMonitor.spec
# The app holds Python and every package, so users install nothing.
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, ROOT)
from version import VERSION as _SOURCE_VERSION  # noqa: E402
VERSION = os.environ.get("TOWER_MONITOR_VERSION") or _SOURCE_VERSION

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, f), ".") for f in
           ("index.html", "devices.html", "setup.html", "log.html", "hotspot.html", "about.js", "config.example.json")],
    hiddenimports=["server", "devices", "archer", "firstrun", "activity", "extras", "hotspot", "version"],
    excludes=["tkinter", "unittest", "pydoc_data", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon_mac = os.path.join(SPECPATH, "icon.icns")
icon_win = os.path.join(SPECPATH, "icon.ico")

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Tower Monitor",
    console=False,                 # no black window
    icon=icon_win if sys.platform == "win32" and os.path.exists(icon_win) else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Tower Monitor")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Tower Monitor.app",
        icon=icon_mac if os.path.exists(icon_mac) else None,
        bundle_identifier="io.github.lte-tower-monitor",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "Tower Monitor",
            "NSHumanReadableCopyright": "Developed by Ahmed Abuammouna · ahmedsam.com",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.utilities",
            # macOS 15 asks before an app may talk to devices on the home network.
            "NSLocalNetworkUsageDescription":
                "Tower Monitor connects to your router on your home network to read the modem's signal.",
            # The window shows pages served by Tower Monitor itself on 127.0.0.1.
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
