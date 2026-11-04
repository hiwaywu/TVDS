#!/usr/bin/env python3
"""
recorder.py - local timeshift recorder.

The sources keep ~33 s of history and then delete it, which caps every player-side
trick (pause, DVR, seek-back). This module pulls the live stream itself, writes the
segments to disk and publishes its own sliding playlist, so the app owns a window that
is as long as the disk allows - not as long as the relay allows.

Used by server.py through /rec/live, /rec/<key>/<file>. Nothing here talks to the player.
"""

import hashlib
import os
import re
import threading
import time
import urllib.parse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class Recorder(threading.Thread):
    daemon = True

    def __init__(self, key, url, referer=None, span_seconds=1800, max_bytes=2_500_000_000,
                 fetch=None, root=None):
        super().__init__(name="rec-%s" % key)
        self.key = key
        self.url = url                # as given (may be a master playlist)
        self.media_url = url          # resolved media playlist
        self.referer = referer
        self.span = span_seconds
        self.max_bytes = max_bytes
        self.fetch = fetch            # server.fetch(url, referer) -> file-like with .read()
        self.dir = os.path.join(root, key)
        self.segs = []                # [{seq, dur, name, bytes}]
        self.init = None              # {"name": ..., "bytes": ...}
        self.key_line = None          # raw #EXT-X-KEY line, URI rewritten absolute
        self.tdur = 6
        self.lock = threading.Lock()
        self.stopped = False
        self.dropped = 0
        self.error = ""
        self.started = time.time()
        self.last_access = self.started
        os.makedirs(self.dir, exist_ok=True)

    def touch(self):
        self.last_access = time.time()

    # ---------------------------------------------------------------- helpers
    def _get(self, url):
        r = self.fetch(url, self.referer)
        try:
            return r.read().decode("utf-8", "replace"), r.geturl() if hasattr(r, "geturl") else url
        finally:
            try:
                r.release()
            except Exception:
                pass

    def _resolve_media(self):
        text, final = self._get(self.url)
        for _ in range(3):
            if "#EXTINF" in text:
                self.media_url = final
                return text
            best, best_h = None, -1
            for line in text.splitlines():
                if line.startswith("#EXT-X-STREAM-INF"):
                    m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                    h = int(m.group(2)) if m else 0
                    if h > best_h:
                        best_h = h
            lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
            if not lines:
                raise RuntimeError("empty playlist")
            nxt = lines[0] if best is None else lines[min(len(lines) - 1, 0)]
            if not nxt.startswith("http"):
                nxt = urllib.parse.urljoin(final, nxt)
            text, final = self._get(nxt)
        raise RuntimeError("no media playlist")

    def _store(self, url, name):
        r = self.fetch(url, self.referer)
        try:
            data = r.read()
        finally:
            try:
                r.release()
            except Exception:
                pass
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return len(data)

    def _ext(self):
        # hls.js picks its demuxer from the URI extension, so keep it honest:
        # fMP4 playlists carry #EXT-X-MAP, everything else here is MPEG-TS
        return ".m4s" if self.init else ".ts"

    # ---------------------------------------------------------------- loop
    def run(self):
        seen = set()
        while not self.stopped:
            try:
                text = self._resolve_media()
            except Exception as exc:
                self.error = str(exc)[:80]
                time.sleep(3)
                continue

            base = self.media_url
            m = re.search(r"#EXT-X-TARGETDURATION:\s*([0-9.]+)", text)
            if m:
                self.tdur = max(1, int(float(m.group(1))))

            key_line = next((l for l in text.splitlines() if l.startswith("#EXT-X-KEY")), None)
            if key_line:
                km = re.search(r'URI="([^"]+)"', key_line)
                if km:
                    kurl = km.group(1)
                    if not kurl.startswith("http"):
                        kurl = urllib.parse.urljoin(base, kurl)
                    self.key_line = key_line.replace(km.group(1), "/rec-key?u=" + urllib.parse.quote(kurl))
                else:
                    self.key_line = None
            else:
                self.key_line = None

            init_line = next((l for l in text.splitlines() if l.startswith("#EXT-X-MAP")), None)
            if init_line:
                im = re.search(r'URI="([^"]+)"', init_line)
                if im:
                    iurl = im.group(1)
                    if not iurl.startswith("http"):
                        iurl = urllib.parse.urljoin(base, iurl)
                    name = "init-%s%s" % (hashlib.sha1(iurl.encode()).hexdigest()[:8], ".m4s")
                    if not os.path.exists(os.path.join(self.dir, name)):
                        try:
                            n = self._store(iurl, name)
                            self.init = {"name": name, "bytes": n}
                        except Exception:
                            pass
                    elif self.init is None:
                        self.init = {"name": name, "bytes": 0}

            entries, dur, uri = [], None, None
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("#EXTINF:"):
                    dur = float(line.split(":")[1].split(",")[0])
                elif line and not line.startswith("#"):
                    uri = line
                    entries.append((dur or 0.0, uri))
                    dur = None

            newest = None
            for dur, uri in entries:
                full = uri if uri.startswith("http") else urllib.parse.urljoin(base, uri)
                seq = hashlib.sha1(full.encode()).hexdigest()[:16]
                if seq in seen:
                    newest = seq
                    continue
                name = "seg-%s%s" % (seq, self._ext())
                try:
                    size = self._store(full, name)
                except Exception:
                    continue
                with self.lock:
                    self.segs.append({"seq": seq, "dur": dur, "name": name, "bytes": size,
                                      "at": time.time()})
                seen.add(seq)
                newest = seq

            self._prune()
            time.sleep(max(0.8, self.tdur / 2.0))

    def _prune(self):
        with self.lock:
            cut = time.time() - self.span
            keep = [s for s in self.segs if s["at"] >= cut]
            total = sum(s["bytes"] for s in keep)
            while keep and total > self.max_bytes:
                total -= keep[0]["bytes"]
                keep.pop(0)
            drop = [s for s in self.segs if s not in keep]
            self.segs = keep
            self.dropped += len(drop)
        for s in drop:
            try:
                os.remove(os.path.join(self.dir, s["name"]))
            except OSError:
                pass

    # ---------------------------------------------------------------- output
    def playlist(self, wait=0.0):
        """Live playlist over the retained window. wait: seconds to wait for first segments,
        because hls.js gives up on an empty playlist and never retries it."""
        end = time.time() + wait
        while wait and time.time() < end:
            with self.lock:
                if len(self.segs) >= 2:
                    break
            time.sleep(0.25)
        with self.lock:
            segs = list(self.segs)
            dropped = self.dropped
        if not segs:
            return "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:%d\n#EXT-X-MEDIA-SEQUENCE:%d\n" % (
                self.tdur, dropped)
        out = ["#EXTM3U", "#EXT-X-VERSION:3",
               "#EXT-X-TARGETDURATION:%d" % self.tdur,
               "#EXT-X-MEDIA-SEQUENCE:%d" % dropped,
               "#EXT-X-DISCONTINUITY-SEQUENCE:0"]
        # absolute, because a relative name would resolve against /rec/live and lose the key
        base = "/rec/%s/" % self.key
        if self.init:
            out.append('#EXT-X-MAP:URI="%s%s"' % (base, self.init["name"]))
        if self.key_line:
            out.append(self.key_line)
        for s in segs:
            # each recorded fragment carries its own timeline, so mark them discontinuous;
            # otherwise hls.js sees non-monotonic PTS and never appends anything
            out.append("#EXT-X-DISCONTINUITY")
            out.append("#EXTINF:%.3f," % (s["dur"] or self.tdur))
            out.append(base + s["name"])
        return "\n".join(out) + "\n"

    def status(self):
        with self.lock:
            segs = list(self.segs)
        return {
            "key": self.key,
            "segments": len(segs),
            "seconds": round(sum(s["dur"] for s in segs), 1),
            "bytes": sum(s["bytes"] for s in segs),
            "est_bytes": sum(s["bytes"] for s in segs),
            "error": self.error,
            "uptime": round(time.time() - self.started, 1),
            "media": self.media_url,
        }

    def stop(self):
        self.stopped = True
