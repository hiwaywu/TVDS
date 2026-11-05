#!/usr/bin/env python3
"""
playability_scan.py - can a browser actually play this, or is it a black screen?

The health check only proved that a segment arrives. A black picture means hls.js got
bytes it cannot decode: HEVC, an audio-only feed, or an fMP4 mount whose init segment we
do not serve. This does what the player does - fetch through /proxy, walk to a segment,
download it, ffprobe it - and records:

  play: "ok"       h264 video + browser-safe audio   (plays in the app)
        "vlc"      video is HEVC, or audio is AC-3/E-AC-3/MP2   (VLC only)
        "broken"   no video stream at all, or the segment will not parse

  python playability_scan.py --workers 10
  python playability_scan.py --only TNT
  python playability_scan.py --drop-broken
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "channels.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis"}


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


def probe_bytes(data, suffix=".ts"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=40)
        kinds = []
        for line in out.stdout.splitlines():
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) >= 2:
                kinds.append((parts[1], parts[0]))      # (type, codec)
            elif len(parts) == 1:
                kinds.append(("?", parts[0]))
        return kinds
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def scan(channel, proxy):
    url = channel["url"]
    ref = channel.get("referer")
    base = proxy + "/proxy?u=" + urllib.parse.quote(url, safe="")
    if ref:
        base += "&r=" + urllib.parse.quote(ref, safe="")
    try:
        body, _ = fetch(base)
    except Exception as exc:
        return {"play": "broken", "why": "proxy: " + str(exc)[:48]}

    text = body.decode("utf-8", "replace")
    if "#EXTINF" not in text:
        # master playlist rewritten by the proxy: follow the first variant
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        if not lines:
            return {"play": "broken", "why": "empty master"}
        nxt = lines[0] if lines[0].startswith("http") else proxy + lines[0]
        try:
            body, _ = fetch(nxt)
        except Exception as exc:
            return {"play": "broken", "why": "variant: " + str(exc)[:48]}
        text = body.decode("utf-8", "replace")

    segs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not segs:
        return {"play": "broken", "why": "no segments"}
    seg = segs[-1] if segs[-1].startswith("http") else proxy + segs[-1]

    try:
        data, ctype = fetch(seg)
    except Exception as exc:
        return {"play": "broken", "why": "segment: " + str(exc)[:48]}
    if len(data) < 20000:
        return {"play": "broken", "why": "segment %d B" % len(data)}

    suffix = ".m4s" if "mp4" in (ctype or "") or b"ftyp" in data[:64] else ".ts"
    kinds = probe_bytes(data, suffix)
    video = [c for t, c in kinds if t == "video"]
    audio = [c for t, c in kinds if t == "audio"]

    if not video:
        return {"play": "broken", "why": "no video stream" + (" (audio only)" if audio else "")}
    if "hevc" in video or "h265" in video:
        return {"play": "vlc", "why": "HEVC video"}
    if audio and not (set(audio) & BROWSER_AUDIO):
        return {"play": "vlc", "why": "audio " + "/".join(sorted(set(audio)))}
    return {"play": "ok", "why": "%s + %s" % (video[0], (audio[0] if audio else "no audio"))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=PATH)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--only")
    ap.add_argument("--proxy", default="http://127.0.0.1:8777")
    ap.add_argument("--drop-broken", action="store_true")
    args = ap.parse_args()

    data = json.load(open(args.file, encoding="utf-8"))
    rows = [(g["name"], c) for g in data["groups"] for c in g["channels"]
            if not args.only or args.only.lower() in c["name"].lower()]
    print("checking %d channels through %s" % (len(rows), args.proxy), flush=True)

    out = {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(scan, c, args.proxy): (g, c) for g, c in rows}
        for fut in cf.as_completed(futs):
            g, c = futs[fut]
            try:
                out[c["url"]] = fut.result()
            except Exception as exc:
                out[c["url"]] = {"play": "broken", "why": str(exc)[:48]}

    ok = vlc = broken = 0
    report = {"broken": [], "vlc": []}
    for g in data["groups"]:
        for c in g["channels"]:
            r = out.get(c["url"])
            if not r:
                continue
            c["play"] = r["play"]
            c["play_note"] = r["why"]
            if r["play"] == "ok":
                ok += 1
            elif r["play"] == "vlc":
                vlc += 1
                report["vlc"].append((g["name"], c["name"], r["why"]))
            else:
                broken += 1
                report["broken"].append((g["name"], c["name"], r["why"]))

    print("\nplayable %d   vlc-only %d   broken %d   (%.0fs)" % (ok, vlc, broken, time.time() - t0))
    for gname, name, why in report["broken"]:
        print("  BROKEN %-10s %-26s %s" % (gname, name[:26], why[:40]))
    for gname, name, why in report["vlc"][:8]:
        print("  VLC    %-10s %-26s %s" % (gname, name[:26], why[:40]))

    if args.drop_broken:
        before = sum(len(g["channels"]) for g in data["groups"])
        for g in data["groups"]:
            g["channels"] = [c for c in g["channels"] if c.get("play") != "broken"]
        data["groups"] = [g for g in data["groups"] if g["channels"]]
        after = sum(len(g["channels"]) for g in data["groups"])
        print("dropped %d broken channels (%d -> %d)" % (before - after, before, after))

    with open(args.file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print("wrote", args.file)


if __name__ == "__main__":
    main()
