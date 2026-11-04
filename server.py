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
import http.client
import io
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
EXTRA_ROOTS = []          # consulted before ROOT; the PC exe points this at its own folder
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


class _Upstream:
    """Thin adapter so the pooled connection looks like the urllib response the proxy
    already knows how to use."""
    __slots__ = ("conn", "resp", "url", "key", "_released")

    def __init__(self, conn, resp, url, key=None):
        self.conn, self.resp, self.url, self.key = conn, resp, url, key
        self._released = False

    def geturl(self):
        return self.url

    @property
    def status(self):
        return self.resp.status

    @property
    def headers(self):
        return self.resp.headers

    def read(self, n=None):
        return self.resp.read(n)

    def release(self):
        """Hand the socket back only when the body is fully consumed."""
        if self._released or self.key is None:
            return
        self._released = True
        try:
            drained = self.resp.isclosed()
        except Exception:
            drained = False
        if drained:
            _release(self.key, self.conn)
        else:
            _discard(self.key, self.conn)

    def close(self):
        try:
            self.resp.close()
        except Exception:
            pass
        if not self._released and self.key is not None:
            self._released = True
            _discard(self.key, self.conn)


# ---------------------------------------------------------------- connection pool
# A live HLS stream asks for a segment every few seconds, always from the same host.
# urllib opens a new TCP connection (and a new TLS handshake) for every one of them,
# which is a full round trip of pure latency before the first byte of video arrives.
# Keep a small pool of idle connections per (scheme, host, port) and hand them out.
_POOL = {}
_POOL_LOCK = threading.Lock()
_POOL_MAX_IDLE = 6


def _acquire(key, scheme, host, port):
    with _POOL_LOCK:
        idle = _POOL.get(key)
        if idle:
            return idle.pop()
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return cls(host, port, timeout=TIMEOUT)


def _release(key, conn):
    with _POOL_LOCK:
        idle = _POOL.setdefault(key, [])
        if len(idle) < _POOL_MAX_IDLE:
            idle.append(conn)
            return
    try:
        conn.close()
    except Exception:
        pass


def _discard(key, conn):
    with _POOL_LOCK:
        idle = _POOL.get(key)
        if idle and conn in idle:
            idle.remove(conn)
    try:
        conn.close()
    except Exception:
        pass


def fetch(url, referer=None, extra=None, method="GET", rng=None, _hops=0):
    """GET through a pooled keep-alive connection, following redirects manually."""
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    host = parts.hostname
    port = parts.port or (443 if scheme == "https" else 80)
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")

    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    if referer:
        headers["Referer"] = referer
    if rng:
        headers["Range"] = rng
    for k, v in (extra or {}).items():
        headers[k] = v

    key = (scheme, host, port)
    conn = _acquire(key, scheme, host, port)
    try:
        conn.putrequest(method, path, skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        resp = conn.getresponse()
    except Exception:
        _discard(key, conn)
        if _hops:
            raise
        return fetch(url, referer, extra, method, rng, _hops)   # one retry on a fresh socket

    location = resp.getheader("Location")
    if resp.status in (301, 302, 303, 307, 308) and location and _hops < 5:
        resp.read()
        _release(key, conn)
        return fetch(urllib.parse.urljoin(url, location), referer, extra, method, rng, _hops + 1)

    if resp.status >= 400:
        body = resp.read()
        _release(key, conn)
        raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, io.BytesIO(body))

    return _Upstream(conn, resp, url, key)


def prox(url, referer=None):
    q = {"u": url}
    if referer:
        q["r"] = referer
    return "/proxy?" + urllib.parse.urlencode(q)


# ---------------------------------------------------------------- TV guide
_EPG_LOCK = threading.Lock()
_EPG_CACHE = {}
_EPG_TS = 0.0
_EPG_TTL = 120                 # seconds a computed guide stays valid
_EPG_MAX_AGE = 12 * 3600       # rebuild the XMLTV indexes after this long


def asset_bytes(name):
    """Read a bundled asset, preferring the copy next to the exe."""
    for base in list(EXTRA_ROOTS) + [ROOT]:
        path = os.path.join(base, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                return fh.read()
    return None


_EPG_MOD = None


def epg_module():
    """Load epg.py by path: in the frozen build it ships as a data file, not a module."""
    global _EPG_MOD
    if _EPG_MOD is not None:
        return _EPG_MOD
    import importlib.util
    for base in list(EXTRA_ROOTS) + [ROOT]:
        path = os.path.join(base, "epg.py")
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("tvds_epg", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["tvds_epg"] = mod
            spec.loader.exec_module(mod)
            _EPG_MOD = mod
            return mod
        except Exception:
            return None
    return None


def epg_payload():
    global _EPG_CACHE, _EPG_TS
    now = time.time()
    with _EPG_LOCK:
        if _EPG_CACHE and now - _EPG_TS < _EPG_TTL:
            return _EPG_CACHE
    mod = epg_module()
    payload = {"generated": int(now), "channels": {}, "covered": 0, "total": 0,
               "ready": bool(mod and mod.index_age() is not None)}
    if mod:
        try:
            raw = asset_bytes("channels.json")
            data = json.loads(raw.decode("utf-8")) if raw else {"groups": []}
            chans = [(g["name"], c["name"]) for g in data.get("groups", [])
                     for c in g.get("channels", [])]
            guide = mod.guide(chans)
            payload.update(channels=guide, covered=len(guide), total=len(chans))
        except Exception:
            pass
    with _EPG_LOCK:
        _EPG_CACHE, _EPG_TS = payload, now
    return payload


def start_epg_refresh():
    """Pull and index the XMLTV files in the background if they are stale or missing."""
    def worker():
        mod = epg_module()
        if not mod:
            return
        try:
            age = mod.index_age()
            if age is None or age > _EPG_MAX_AGE:
                mod.refresh(verbose=False)
                with _EPG_LOCK:
                    _EPG_TS = 0.0           # force the next request to recompute
        except Exception:
            pass
    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- segment cache
# HLS is strictly sequential: the player asks for segment N, then N+1. Fetching
# one segment ahead in the background turns the next request into a local hit,
# which is what removes the stall at the live edge (it does not lower latency,
# it stops the buffer from emptying).
CACHE = collections.OrderedDict()
CACHE_LOCK = threading.Lock()
CACHE_BUDGET = 192 * 1024 * 1024     # total bytes kept
CACHE_ITEM_MAX = 16 * 1024 * 1024    # skip anything bigger
PREFETCH_DEPTH = 4                   # segments to warm after each playlist
PREFETCH_WORKERS = 4                 # persistent workers, so their sockets stay warm
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


_PREFETCH_Q = queue.Queue(maxsize=64)
_PREFETCH_STARTED = False
_PREFETCH_LOCK = threading.Lock()


def _prefetch_worker():
    """Long-lived worker: its keep-alive socket survives between segments."""
    while True:
        u, referer = _PREFETCH_Q.get()
        try:
            if cache_get(u) is None:
                r = fetch(u, referer)
                ctype = r.headers.get("Content-Type", "application/octet-stream")
                body = r.read(CACHE_ITEM_MAX)
                if body and body.lstrip()[:7] != b"#EXTM3U":     # never cache playlists
                    cache_put(u, body, ctype)
                    STATS["prefetched"] += 1
                r.release()
        except Exception:
            pass
        finally:
            _PREFETCH_Q.task_done()


def prefetch(urls, referer=None):
    global _PREFETCH_STARTED
    if not _PREFETCH_STARTED:
        with _PREFETCH_LOCK:
            if not _PREFETCH_STARTED:
                for _ in range(PREFETCH_WORKERS):
                    threading.Thread(target=_prefetch_worker, daemon=True).start()
                _PREFETCH_STARTED = True
    for u in urls[:PREFETCH_DEPTH]:
        try:
            _PREFETCH_Q.put_nowait((u, referer))
        except queue.Full:
            return


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
        # EXTRA_ROOTS lets the packaged exe serve a channels.json that sits next to the
        # .exe, so a catalogue update does not need a rebuild
        data = asset_bytes(name)
        if data is None:
            return self._send(404, "not found", "text/plain")
        return self._send(200, data, ctype, {"Cache-Control": "no-cache"})

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
        if u.path == "/api/epg":
            return self._send(200, json.dumps(epg_payload()), "application/json",
                              {"Cache-Control": "no-store"})
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
            r.release()
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
            r.close()
            return
        if keep and len(buf) > 1024:
            cache_put(target, bytes(buf), ctype)
        r.release()          # socket goes back to the pool only once the body is drained


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
    start_epg_refresh()
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
