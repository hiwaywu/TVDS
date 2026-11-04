#!/usr/bin/env python3
"""
tvapp for Windows - one window, no browser tab, no Python on the target machine.

Starts the local proxy from server.py on 127.0.0.1, then opens a chromeless Edge
"app" window pointed at it. Closing the window shuts the server down again.

  tvapp.exe                    # normal launch
  tvapp.exe --port 9000        # different port
  tvapp.exe --browser          # force the default browser instead of the app window

Bundled assets (index.html, hls.min.js, channels.json, server.py) are read from the
PyInstaller extraction dir when frozen, or from the repo root when run from source.
"""

import argparse
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer

APP_NAME = "tvapp"
DEFAULT_PORT = 8777
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def asset_dir():
    """Where index.html / channels.json / server.py live (bundle dir when frozen)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def load_server(assets):
    """Import server.py from the bundle so it can be re-rooted at the assets."""
    if assets not in sys.path:
        sys.path.insert(0, assets)          # so `import epg` resolves when frozen
    path = os.path.join(assets, "server.py")
    spec = importlib.util.spec_from_file_location("tvds_server", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tvds_server"] = mod
    spec.loader.exec_module(mod)
    mod.ROOT = assets
    # a channels.json next to the exe (or one folder up) wins over the bundled copy,
    # so refreshing the catalogue is a file copy, not a rebuild
    here = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else assets
    roots = [here, os.path.dirname(here)]
    mod.EXTRA_ROOTS = [d for d in roots if d and os.path.isdir(d)]
    return mod


def port_in_use(port):
    """A real listener answers a connect(); a bound-but-idle socket does not."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", port)) == 0


def free_port(preferred):
    for port in [preferred] + list(range(preferred + 1, preferred + 12)):
        if port_in_use(port):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def find_edge():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    from shutil import which
    return which("msedge")


def read_port():
    try:
        with open(os.path.join(data_dir(), "port.txt"), encoding="utf-8") as fh:
            return int(fh.read().strip())
    except Exception:
        return None


def write_port(port):
    try:
        with open(os.path.join(data_dir(), "port.txt"), "w", encoding="utf-8") as fh:
            fh.write(str(port))
    except Exception:
        pass


def app_running():
    """(url, True) when a tvapp instance already answers on this machine."""
    for port in [read_port(), DEFAULT_PORT, DEFAULT_PORT + 1, DEFAULT_PORT + 2]:
        if not port:
            continue
        url = "http://127.0.0.1:%d/" % port
        try:
            with urllib.request.urlopen(url + "api/channels", timeout=1.2) as r:
                if r.status == 200:
                    return url, True
        except Exception:
            continue
    return None, False


def focus_window(title="tvapp - live channels"):
    """Bring an existing app window to the front; True when one was found."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hits = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title in buf.value:
                hits.append(hwnd)
                return False
        return True

    user32.EnumWindows(enum, 0)
    if not hits:
        return False
    hwnd = hits[0]
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


# Chromium-family browsers open a real app window with --app=; Firefox has no such
# mode, so it gets -kiosk (or a plain window with --windowed) and a throwaway profile.
CHROMIUM = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "brave": [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
    "vivaldi": [
        r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Vivaldi\Application\vivaldi.exe"),
    ],
}
FIREFOX = [
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
]
# what "auto" tries, in order
AUTO_ORDER = ["chrome", "brave", "vivaldi", "edge", "firefox"]


def find_browser(pref):
    """-> (kind, exe_path, name); kind is 'chromium' or 'firefox'."""
    order = AUTO_ORDER if pref in (None, "", "auto") else [pref]
    for name in order:
        if name == "firefox":
            for p in FIREFOX:
                if os.path.isfile(p):
                    return "firefox", p, name
            continue
        for p in CHROMIUM.get(name, []):
            if p and os.path.isfile(p):
                return "chromium", p, name
    return None, None, None


def firefox_profile():
    """Firefox needs a profile for -kiosk; pre-seed it so nothing blocks playback."""
    prof = os.path.join(data_dir(), "firefox-profile")
    os.makedirs(prof, exist_ok=True)
    userjs = os.path.join(prof, "user.js")
    prefs = (
        'user_pref("media.autoplay.default", 0);\n'
        'user_pref("media.autoplay.blocking_policy", 0);\n'
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("browser.startup.homepage_override.mstone", "ignore");\n'
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n'
        'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);\n'
        'user_pref("full-screen-api.allow-trusted-requests-only", false);\n'
    )
    if not os.path.isfile(userjs) or open(userjs, encoding="utf-8").read() != prefs:
        with open(userjs, "w", encoding="utf-8") as fh:
            fh.write(prefs)
    return prof


def read_pref():
    try:
        with open(os.path.join(data_dir(), "config.json"), encoding="utf-8") as fh:
            return json.load(fh).get("browser")
    except Exception:
        return None


def write_pref(name):
    try:
        with open(os.path.join(data_dir(), "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"browser": name}, fh)
    except Exception:
        pass


def wait_ready(url, timeout=20.0):
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url + "api/channels", timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def tkinter_shell(url, stop):
    """Fallback when no Edge is installed: a tiny always-there window."""
    import tkinter as tk

    root = tk.Tk()
    root.title("tvapp")
    root.geometry("420x150")
    root.resizable(False, False)
    tk.Label(root, text="tvapp is running", font=("Segoe UI", 14, "bold")).pack(pady=(18, 2))
    tk.Label(root, text=url, fg="#555").pack()
    row = tk.Frame(root)
    row.pack(pady=14)
    tk.Button(row, text="Open in browser", width=16,
              command=lambda: webbrowser.open(url)).pack(side="left", padx=6)
    tk.Button(row, text="Quit", width=10, command=root.destroy).pack(side="left", padx=6)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    stop.set()


class _Server(ThreadingHTTPServer):
    # Windows lets SO_REUSEADDR steal a port that another process is already
    # listening on, which is how you end up with two servers on 8777 and a window
    # showing the wrong catalogue.
    allow_reuse_address = False


def main():
    ap = argparse.ArgumentParser(description="tvapp - live TV desktop app")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--browser", nargs="?", const="auto", default=None,
                    choices=["auto", "chrome", "edge", "firefox", "brave", "vivaldi", "default"],
                    help="app-window browser; remembered for the next launch")
    ap.add_argument("--windowed", action="store_true",
                    help="Firefox only: open a normal window instead of kiosk")
    ap.add_argument("--no-window", action="store_true", help="serve only (used for testing)")
    ap.add_argument("--new", action="store_true",
                    help="start a second instance even if tvapp is already running")
    args = ap.parse_args()

    assets = asset_dir()
    os.chdir(assets)
    # the guide index is written to the user's data dir, not inside the read-only bundle
    os.environ.setdefault("TVDS_EPG_CACHE", os.path.join(data_dir(), "epg"))
    os.environ.setdefault("TVDS_DATA_DIR", data_dir())

    # single instance: if tvapp is already up, focus its window instead of starting a
    # second server and a second window
    if not args.new:
        running_url, up = app_running()
        if up:
            if focus_window():
                return 0
            edge_url = running_url                      # server alive, window was closed
            kind, exe, name = find_browser(read_pref() or "auto")
            if exe and kind == "chromium":
                subprocess.Popen([exe, "--app=" + edge_url,
                                  "--user-data-dir=" + os.path.join(data_dir(), "edge-handoff"),
                                  "--no-first-run", "--no-default-browser-check",
                                  "--window-size=1440,900"])
            else:
                webbrowser.open(edge_url)
            return 0

    srv_mod = load_server(assets)

    port = free_port(args.port)
    if not port:
        print("no free port near %d" % args.port, file=sys.stderr)
        return 1

    httpd = _Server(("127.0.0.1", port), srv_mod.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    url = "http://127.0.0.1:%d/" % port
    if not wait_ready(url):
        print("server did not answer on %s" % url, file=sys.stderr)
        return 2
    write_port(port)

    if args.no_window:
        print("serving", url, flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        httpd.shutdown()
        return 0

    # --browser takes an optional name: chrome / edge / firefox / brave / vivaldi /
    # default (system browser) / auto (first installed). Saved for next launch.
    pref = args.browser or read_pref() or "auto"
    if pref == "default":
        webbrowser.open(url)
        stop = threading.Event()
        tkinter_shell(url, stop)
        httpd.shutdown()
        return 0

    kind, exe, name = find_browser(pref)
    if not exe and pref not in ("auto", "default", None):
        kind, exe, name = find_browser("auto")      # asked for one that isn't installed
    if not exe:
        webbrowser.open(url)
        stop = threading.Event()
        tkinter_shell(url, stop)
        httpd.shutdown()
        return 0
    if args.browser:
        write_pref(name)

    if kind == "firefox":
        # no --app= in Firefox: kiosk is the closest thing to a chromeless window
        cmd = [exe, "-no-remote", "-profile", firefox_profile(),
               "-kiosk" if not args.windowed else "-new-window", url]
    else:
        # a fresh profile per launch guarantees a new browser process, so closing the
        # window really does end this server instance
        profile = os.path.join(data_dir(), "%s-profile-%d" % (name, os.getpid()))
        cmd = [
            exe,
            "--app=" + url,
            "--user-data-dir=" + profile,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,msEdgeSplitScreen",
            "--window-size=1440,900",
            "--autoplay-policy=no-user-gesture-required",
        ]

    started = time.time()
    proc = subprocess.Popen(cmd)
    proc.wait()                      # window closed -> shut the proxy down
    if time.time() - started < 3:
        # the browser handed the URL to an instance that was already running; keep the
        # proxy alive until the user closes the little control window.
        stop = threading.Event()
        tkinter_shell(url, stop)
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
