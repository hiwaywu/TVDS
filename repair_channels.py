#!/usr/bin/env python3
"""
repair_channels.py - test every channel properly, and fix the ones that can be fixed.

Black screens were mostly our own choosing: a relay often publishes several variants
(HEVC 1080 + H.264 720), and the pipeline ranked by height, so it stored the HEVC one.
A browser cannot decode HEVC without platform support, so the picture stays black.

For every channel this does, in order:

  1. fetch the playlist (direct, with the channel's Referer)
  2. if it is a master, enumerate the variants and their CODECS
  3. keep an H.264 variant when one exists, prefer the tallest one          <-- the fix
  4. download a real segment from the chosen variant and ffprobe it
  5. classify: play=ok | vlc (HEVC/AC-3/MP2 only) | broken (nothing decodable)
  6. rewrite the channel's url/badge when a better variant was found
  7. record load_ms and status (ok / slow / dead)

  python repair_channels.py                 # test + fix, write channels.json
  python repair_channels.py --drop          # also delete what stayed broken
  python repair_channels.py --only TNT
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
SLOW_MS = 4000


def fetch(url, referer=None, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.geturl(), int((time.perf_counter() - t0) * 1000)


def probe(data, suffix=".ts"):
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
             "-of", "csv=p=0", path], capture_output=True, text=True, timeout=40)
        v, a, h = None, None, 0
        for line in out.stdout.splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 2 and parts[1] == "video":
                v = parts[0]
                if len(parts) >= 4 and parts[3].isdigit():
                    h = int(parts[3])
            elif len(parts) >= 2 and parts[1] == "audio" and not a:
                a = parts[0]
        return v, a, h
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def variants_from_master(text, base):
    """[(height, codecs, url)] in the order the master lists them."""
    out, pending = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            h = 0
            m = re.search(r"RESOLUTION=\d+x(\d+)", line)
            if m:
                h = int(m.group(1))
            c = re.search(r'CODECS="([^"]+)"', line)
            pending = (h, (c.group(1).lower() if c else ""))
        elif line and not line.startswith("#") and pending:
            url = line if line.startswith("http") else urllib.parse.urljoin(base, line)
            out.append((pending[0], pending[1], url))
            pending = None
    return out


def is_avc(codecs, url=""):
    if codecs:
        return "avc" in codecs or "avc1" in codecs
    return True                      # unknown codecs: assume AVC until probed


def test_url(url, referer):
    """-> dict(ok, play, why, height, codecs_of_segment, avc_variant, ms)"""
    res = {"ok": False, "play": "broken", "why": "", "height": 0, "ms": 0, "url": url}
    try:
        body, final, ms = fetch(url, referer)
    except Exception as exc:
        res["why"] = "master: " + str(exc)[:44]
        return res
    res["ms"] = ms
    text = body.decode("utf-8", "replace")

    if "#EXTINF" not in text:
        vs = variants_from_master(text, final)
        if not vs:
            res["why"] = "empty master"
            return res
        avc = [v for v in vs if is_avc(v[1], v[2])]
        pool = avc or vs
        pool.sort(key=lambda v: v[0], reverse=True)
        chosen = pool[0]
        res["avc_variant"] = bool(avc)
        res["url"] = chosen[2]
        res["height"] = chosen[0]
        # remember whether we had to skip a taller, non-H.264 variant
        tallest = max(v[0] for v in vs) if vs else chosen[0]
        res["downgraded"] = bool(avc) and chosen[0] < tallest
        try:
            body, final, ms2 = fetch(chosen[2], referer)
            res["ms"] += ms2
            text = body.decode("utf-8", "replace")
        except Exception as exc:
            res["why"] = "variant: " + str(exc)[:44]
            return res

    segs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not segs:
        res["why"] = "no segments"
        return res
    seg = segs[-1]
    if not seg.startswith("http"):
        seg = urllib.parse.urljoin(final, seg)
    try:
        data, _, ms3 = fetch(seg, referer, timeout=30)
        res["ms"] += ms3
    except Exception as exc:
        res["why"] = "segment: " + str(exc)[:44]
        return res
    if len(data) < 20000:
        res["why"] = "segment %d B" % len(data)
        return res

    # fMP4: the init segment carries the codec description, so prepend it
    init = b""
    m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', text)
    if m:
        init_url = m.group(1)
        if not init_url.startswith("http"):
            init_url = urllib.parse.urljoin(final, init_url)
        try:
            init, _, ms4 = fetch(init_url, referer, timeout=20)
            res["ms"] += ms4
        except Exception:
            init = b""

    suffix = ".m4s" if (init or b"ftyp" in data[:64]) else ".ts"
    v, a, h = probe(init + data, suffix)
    if h:
        res["height"] = h
    if not v:
        res["why"] = "no video stream" + (" (audio only)" if a else "")
        return res
    res["ok"] = True
    if v in ("hevc", "h265"):
        res["play"], res["why"] = "vlc", "HEVC video"
    elif a and a not in BROWSER_AUDIO:
        res["play"], res["why"] = "vlc", "audio " + a
    else:
        res["play"], res["why"] = "ok", "%s%s%s" % (v, " + " + a if a else "", "" if h == 0 else " %dp" % h)
    return res


def work(channel):
    url, ref = channel["url"], channel.get("referer")
    best = test_url(url, ref)
    # a stored URL that is a bare variant may hide a better sibling: retry via the parent
    # only when the result is bad and the URL looks like a variant (…/mono.m3u8, …/index.m3u8)
    if best["play"] != "ok" and "/tracks-" in url:
        parent = url.split("/tracks-")[0] + "/index.m3u8"
        alt = test_url(parent, ref)
        if alt["play"] == "ok" or (alt["ok"] and best["play"] == "broken"):
            best = alt
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=PATH)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only")
    ap.add_argument("--drop", action="store_true")
    args = ap.parse_args()

    data = json.load(open(args.file, encoding="utf-8"))
    rows = [(g["name"], c) for g in data["groups"] for c in g["channels"]
            if not args.only or args.only.lower() in c["name"].lower()]
    print("testing %d channels" % len(rows), flush=True)

    results, done = {}, 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(work, c): (g, c) for g, c in rows}
        for fut in cf.as_completed(futs):
            g, c = futs[fut]
            done += 1
            try:
                results[c["url"]] = fut.result()
            except Exception as exc:
                results[c["url"]] = {"ok": False, "play": "broken", "why": str(exc)[:44], "ms": 0}
            if done % 25 == 0:
                print("  %d/%d  %.0fs" % (done, len(rows), time.time() - t0), flush=True)

    fixed, ok, vlc, broken, slow = 0, 0, 0, 0, 0
    for g in data["groups"]:
        for c in g["channels"]:
            r = results.get(c["url"])
            if not r:
                continue
            c["play"] = r["play"]
            c["play_note"] = r["why"]
            c["load_ms"] = r.get("ms", 0)
            if r["play"] == "ok":
                ok += 1
                c["status"] = "slow" if c["load_ms"] > SLOW_MS else "ok"
                slow += 1 if c["status"] == "slow" else 0
            elif r["play"] == "vlc":
                vlc += 1
                c["status"] = "vlc"
            else:
                broken += 1
                c["status"] = "dead"
            # point the channel at the variant we actually verified
            if (r.get("downgraded") and r.get("url") and r["url"] != c["url"]
                    and r["play"] in ("ok", "vlc")):
                c["url"] = r["url"]
                fixed += 1
            if r.get("height"):
                c["height"] = r["height"]
                c["badge"] = "%dp" % r["height"]

    print("\nplayable %d (of which slow %d) · vlc-only %d · broken %d · urls fixed %d  in %.0fs"
          % (ok, slow, vlc, broken, fixed, time.time() - t0))
    for g in data["groups"]:
        for c in g["channels"]:
            if c.get("play") != "ok":
                print("  %-5s %-10s %-26s %s" % ((c.get("play") or "").upper(), g["name"],
                                                 c["name"][:26], (c.get("play_note") or "")[:40]))

    if args.drop:
        before = sum(len(g["channels"]) for g in data["groups"])
        for g in data["groups"]:
            g["channels"] = [c for c in g["channels"] if c.get("play") != "broken"]
        data["groups"] = [g for g in data["groups"] if g["channels"]]
        after = sum(len(g["channels"]) for g in data["groups"])
        print("dropped %d broken (%d -> %d)" % (before - after, before, after))

    with open(args.file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print("wrote", args.file)


if __name__ == "__main__":
    main()
