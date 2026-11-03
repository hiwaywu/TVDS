#!/usr/bin/env python3
"""
tvapp — local IPTV surf app.

Why a proxy: most public HLS relays send no CORS headers and many demand a
specific Referer/User-Agent, so a browser page cannot fetch them directly.
This server fetches upstream, rewrites playlists so every child URL comes back
through /proxy, and stamps permissive CORS on everything.

Run:  python server.py [--port 8777] [--open]
"""

import argparse
import collections
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT = 20
MAX_BYTES = 64 * 1024 * 1024


def find_vlc():
    import shutil
    exe = shutil.which("vlc")
    if exe:
        return exe
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "VideoLAN", "VLC", "vlc.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "VideoLAN", "VLC", "vlc.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "vlc", "vlc.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def fetch(url, referer=None, extra=None, method="GET", rng=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    req.add_header("Accept-Encoding", "identity")
    if referer:
        req.add_header("Referer", referer)
    if rng:
        req.add_header("Range", rng)
    for k, v in (extra or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def prox(url, referer=None):
    q = {"u": url}
    if referer:
        q["r"] = referer
    return "/proxy?" + urllib.parse.urlencode(q)


# ---------------------------------------------------------------- segment cache
# HLS is strictly sequential: the player asks for segment N, then N+1. Fetching
# one segment ahead in the background turns the next request into a local hit,
# which is what removes the stall at the live edge (it does not lower latency,
# it stops the buffer from emptying).
CACHE = collections.OrderedDict()
CACHE_LOCK = threading.Lock()
CACHE_BUDGET = 192 * 1024 * 1024     # total bytes kept
CACHE_ITEM_MAX = 16 * 1024 * 1024    # skip anything bigger
PREFETCH_DEPTH = 3                   # segments to warm after each playlist
PREFETCH_SLOTS = threading.Semaphore(4)
STATS = {"hits": 0, "misses": 0, "prefetched": 0}


def cache_get(url):
    with CACHE_LOCK:
        item = CACHE.get(url)
        if item is None:
            STATS["misses"] += 1
            return None
        CACHE.move_to_end(url)
        STATS["hits"] += 1
        return item


def cache_put(url, body, ctype):
    if not body or len(body) > CACHE_ITEM_MAX:
        return
    with CACHE_LOCK:
        CACHE[url] = (body, ctype, time.time())
        CACHE.move_to_end(url)
        total = sum(len(v[0]) for v in CACHE.values())
        while total > CACHE_BUDGET and len(CACHE) > 1:
            _, old = CACHE.popitem(last=False)
            total -= len(old[0])


def prefetch(urls, referer=None):
    def worker(u):
        with PREFETCH_SLOTS:
            if cache_get(u) is not None:
                return
            try:
                r = fetch(u, referer)
                ctype = r.headers.get("Content-Type", "application/octet-stream")
                body = r.read(CACHE_ITEM_MAX)
            except Exception:
                return
            if body:
                # never cache playlists: they must be rewritten on every request alive
                if body.lstrip()[:7] == b"#EXTM3U":
                    return
                cache_put(u, body, ctype)
                STATS["prefetched"] += 1

    for u in urls[:PREFETCH_DEPTH]:
        threading.Thread(target=worker, args=(u,), daemon=True).start()


def segments_from_playlist(text, base):
    """Absolute upstream URLs of the media segments, newest last."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(urllib.parse.urljoin(base, s))
    return out


def rewrite_playlist(body, base, referer):
    """Rewrite every URI in an m3u8 so it flows back through this proxy."""
    out = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            if 'URI="' in s:
                head, _, rest = s.partition('URI="')
                uri, _, tail = rest.partition('"')
                out.append(head + 'URI="' + prox(urllib.parse.urljoin(base, uri), referer) + '"' + tail)
            else:
                out.append(s)
            continue
        out.append(prox(urllib.parse.urljoin(base, s), referer))
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "tvapp/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "*")

    def _send(self, code, body, ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _static(self, name, ctype):
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            return self._send(404, "not found", "text/plain")
        with open(path, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype, {"Cache-Control": "no-cache"})

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)

        if u.path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if u.path == "/hls.min.js":
            return self._static("hls.min.js", "application/javascript")
        if u.path == "/manifest.json":
            return self._static("manifest.json", "application/manifest+json")
        if u.path == "/icon-512.png":
            return self._static("icon-512.png", "image/png")
        if u.path == "/sw.js":
            return self._static("sw.js", "application/javascript")
        if u.path == "/api/channels":
            return self._static("channels.json", "application/json")
        if u.path == "/api/stats":
            with CACHE_LOCK:
                size = sum(len(v[0]) for v in CACHE.values())
                n = len(CACHE)
            return self._send(200, json.dumps({"cache_items": n, "cache_mb": round(size / 1048576, 1),
                                               **STATS}), "application/json")
        if u.path == "/api/probe":
            target = (qs.get("u") or [""])[0]
            if not target:
                return self._send(400, '{"ok":false}', "application/json")
            try:
                r = fetch(target)
                ok = r.status == 200
                ctype = r.headers.get("Content-Type", "")
                return self._send(200, json.dumps({"ok": ok, "status": r.status, "type": ctype}),
                                  "application/json")
            except Exception as exc:
                return self._send(200, json.dumps({"ok": False, "error": str(exc)}), "application/json")
        if u.path == "/open":
            target = (qs.get("u") or [""])[0]
            referer = (qs.get("r") or [None])[0]
            dry = (qs.get("dry") or ["0"])[0] == "1"
            if not target:
                return self._send(400, '{"ok":false,"error":"missing u"}', "application/json")
            vlc = find_vlc()
            if not vlc:
                return self._send(200, json.dumps({"ok": False, "error": "VLC not found"}),
                                  "application/json")
            if not dry:
                args = [vlc, "--network-caching=2000"]
                if referer:
                    args.append("--http-referrer=" + referer)
                args.append(target)
                try:
                    subprocess.Popen(args, close_fds=True)
                except Exception as exc:
                    return self._send(200, json.dumps({"ok": False, "error": str(exc)}),
                                      "application/json")
            return self._send(200, json.dumps({"ok": True, "player": vlc}), "application/json")
        if u.path == "/proxy":
            target = (qs.get("u") or [""])[0]
            referer = (qs.get("r") or [None])[0]
            if not target:
                return self._send(400, "missing u", "text/plain")
            return self._proxy(target, referer)

        return self._send(404, "not found", "text/plain")

    def _proxy(self, target, referer):
        rng = self.headers.get("Range")

        # local hit: the prefetcher already pulled this segment
        if not rng:
            hit = cache_get(target)
            if hit:
                body, ctype, _ = hit
                return self._send(200, body, ctype, {"X-Cache": "HIT"})

        try:
            r = fetch(target, referer=referer, rng=rng)
        except urllib.error.HTTPError as exc:
            return self._send(exc.code, f"upstream {exc.code}", "text/plain")
        except Exception as exc:
            return self._send(502, f"upstream error: {exc}", "text/plain")

        final = r.geturl()
        ctype = r.headers.get("Content-Type", "application/octet-stream")
        is_playlist = ("mpegurl" in ctype.lower()) or final.split("?")[0].endswith(".m3u8")

        # sniff the first bytes: many relays serve playlists as octet-stream from
        # redirect endpoints that carry no .m3u8 suffix at all
        head = r.read(64 * 1024)
        if head.lstrip()[:7] == b"#EXTM3U":
            is_playlist = True

        if is_playlist:
            raw = (head + r.read(4 * 1024 * 1024)).decode("utf-8", "replace")
            body = rewrite_playlist(raw, final, referer)
            segs = segments_from_playlist(raw, final)
            if segs:
                prefetch(segs[-PREFETCH_DEPTH:], referer)
            return self._send(200, body, "application/vnd.apple.mpegurl",
                              {"Cache-Control": "no-cache"})

        code = r.status if rng else 200
        length = r.headers.get("Content-Length")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Cache", "MISS")
        if length and not head:
            self.send_header("Content-Length", length)
        elif rng and r.headers.get("Content-Range"):
            self.send_header("Content-Range", r.headers["Content-Range"])
        else:
            self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        sent = 0
        buf = bytearray()
        keep = not rng
        try:
            if head:
                self.wfile.write(head)
                sent += len(head)
                if keep:
                    buf += head
            while sent < MAX_BYTES:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
                if keep and len(buf) + len(chunk) <= CACHE_ITEM_MAX:
                    buf += chunk
                else:
                    keep = False
        except (BrokenPipeError, ConnectionResetError):
            return
        if keep and len(buf) > 1024:
            cache_put(target, bytes(buf), ctype)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"port {args.port} unavailable: {exc}", file=sys.stderr)
        sys.exit(1)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"tvapp running at {url}   (Ctrl+C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
