#!/usr/bin/env python3
"""
fix_heights.py - fill in the real resolution for channels whose manifest never
declares one.

Some feeds serve a bare media playlist (no #EXT-X-STREAM-INF, no RESOLUTION
attribute): Pluto TV, the turnerlive slates, the hidns relays, the :8989 boxes.
The manifest simply does not say "1920x1080" anywhere, so the harvest can only
label them "live". ffprobe reads the size off the actual video stream instead.

Two paths:
  1. ffprobe the playlist URL directly
  2. if the relay's segments have no file extension (ffmpeg's HLS demuxer
     rejects those), download one segment and probe the file as raw MPEG-TS

  python fix_heights.py                # fix channels.json in place
  python fix_heights.py --file x.json
"""

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _parse(text):
    line = (text or "").strip().splitlines()
    if not line:
        return None
    parts = [p.strip() for p in line[0].split(",") if p.strip()]
    nums, codec = [], ""
    for p in parts:
        if p.isdigit():
            nums.append(int(p))
        elif not codec:
            codec = p
    if len(nums) < 2 or not nums[0] or not nums[1]:
        return None
    return nums[0], nums[1], codec


def ffprobe_url(url, referer=None):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,codec_name",
           "-of", "csv=p=0",
           "-user_agent", UA,
           "-allowed_extensions", "ALL",
           "-rw_timeout", "12000000",
           "-probesize", "800000", "-analyzeduration", "3000000"]
    if referer:
        cmd += ["-headers", "Referer: %s\r\n" % referer]
    cmd += ["-i", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return None
    return _parse(out.stdout)


def ffprobe_file(path):
    cmd = ["ffprobe", "-v", "error", "-f", "mpegts",
           "-select_streams", "v:0",
           "-show_entries", "stream=width,height,codec_name",
           "-of", "csv=p=0", path]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None
    return _parse(out.stdout)


def probe_by_segment(url, referer=None):
    try:
        from build import get
        r = get(url, referer)
        body = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
        base = r.geturl()
    except Exception:
        return None
    if "#EXTM3U" not in body:
        return None
    segs = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")]
    if not segs:
        return None
    seg_url = urllib.parse.urljoin(base, segs[-1])
    try:
        data = get(seg_url, referer).read(8 * 1024 * 1024)
    except Exception:
        return None
    if len(data) < 1024:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".ts")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return ffprobe_file(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def resolve(url, referer=None):
    res = ffprobe_url(url, referer)
    if res:
        return res
    return probe_by_segment(url, referer)


def fix(path, workers=8):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    targets = [c for g in data["groups"] for c in g["channels"] if not c.get("height")]
    if not targets:
        print("nothing to fix - every channel already has a resolution")
        return 0

    print("probing %d channels with no declared resolution..." % len(targets), flush=True)
    fixed = 0
    dead = []
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(resolve, c["url"], c.get("referer")): c for c in targets}
        for fut in cf.as_completed(futs):
            c = futs[fut]
            try:
                res = fut.result()
            except Exception:
                res = None
            if not res:
                print("  %-30s -> unresolved" % c["name"][:30], flush=True)
                dead.append(c)
                continue
            w, h, codec = res
            c["height"] = h
            c["badge"] = "%dp" % h
            c["quality"] = "%dx%d" % (w, h)
            c["note"] = " | ".join(["%dp" % h,
                                    "HEVC" if codec in ("hevc", "h265") else "H.264"])
            fixed += 1
            print("  %-30s -> %dx%d" % (c["name"][:30], w, h), flush=True)

    # an entry we cannot even read a playlist from is dead weight - drop it
    removed = 0
    if dead:
        from build import get
        for c in dead:
            try:
                body = get(c["url"], c.get("referer")).read(65536).decode("utf-8", "replace")
            except Exception:
                body = ""
            if "#EXTM3U" not in body:
                for g in data["groups"]:
                    if c in g["channels"]:
                        g["channels"].remove(c)
                        removed += 1
                        print("  pruned dead: %s" % c["name"], flush=True)
                        break
        data["groups"] = [g for g in data["groups"] if g["channels"]]

    for g in data["groups"]:
        g["channels"].sort(key=lambda c: (-(c.get("height") or 0), c["name"].lower()))

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print("updated %d/%d in %s" % (fixed, len(targets), path))
    return fixed


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.path.join(ROOT, "channels.json"))
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    fix(args.file, args.workers)
