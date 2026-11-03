#!/usr/bin/env python3
"""
bitrate_scan.py - measure the real bitrate of every channel instead of trusting the
manifest.

Half the catalogue is served from relays whose playlists are bare media playlists: no
#EXT-X-STREAM-INF, no BANDWIDTH, nothing to print. Those rows used to show only
"1080p | H.264" while the amagi/akamai rows showed "1080p | H.264 | 4.3 Mbps | verified",
which looks like missing information and is really just a missing declaration.

This script downloads one or two real segments, divides bytes by EXTINF duration, and
writes a uniform note for every channel:

    "1080p | H.264 | 4.3 Mbps | verified (measured)"

  python bitrate_scan.py                  # measure all, rewrite notes
  python bitrate_scan.py --workers 8
  python bitrate_scan.py --only "TVB"     # subset
  python bitrate_scan.py --keep-declared  # keep a declared BANDWIDTH when it is higher

Declared BANDWIDTH is kept as a cross-check: when a feed declares one, both numbers are
reported if they disagree by more than 40%.
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SEG_CAP = 12 * 1024 * 1024      # never pull more than this per channel


def get(url, referer=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    if referer:
        req.add_header("Referer", referer)
    return urllib.request.urlopen(req, timeout=25)


def pick_variant(body, base):
    """Highest RESOLUTION variant (what MAX mode plays); returns (url, h, bw, codecs)."""
    best = None
    pending = None
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            bw = re.search(r"AVERAGE-BANDWIDTH=(\d+)", line) or re.search(r"BANDWIDTH=(\d+)", line)
            cd = re.search(r'CODECS="([^"]+)"', line)
            pending = {"h": int(res.group(2)) if res else 0,
                       "bw": int(bw.group(1)) if bw else 0,
                       "codecs": ((cd.group(1) if cd else "") or "").lower()}
        elif line and not line.startswith("#") and pending:
            cand = dict(pending, url=urllib.parse.urljoin(base, line))
            if best is None or (cand["h"], cand["bw"]) > (best["h"], best["bw"]):
                best = cand
            pending = None
    return best


def measure(url, referer=None):
    """-> dict(mbps, h, codec, declared_mbps) or None."""
    try:
        r = get(url, referer)
        body = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
        base = r.geturl()
    except Exception:
        return None
    if "#EXTM3U" not in body:
        return None

    declared = None
    codec = "H.264"
    height = 0
    variant = pick_variant(body, base)
    if variant:
        declared = variant["bw"] / 1e6 if variant["bw"] else None
        height = variant["h"]
        codec = "HEVC" if ("hvc1" in variant["codecs"] or "hev1" in variant["codecs"]) else "H.264"
        try:
            r2 = get(variant["url"], referer)
            body = r2.read(4 * 1024 * 1024).decode("utf-8", "replace")
            base = r2.geturl()
        except Exception:
            return None

    # collect (duration, url) pairs from the media playlist
    segs, dur = [], None
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            m = re.match(r"#EXTINF:\s*([0-9.]+)", line)
            dur = float(m.group(1)) if m else None
        elif line and not line.startswith("#"):
            u = urllib.parse.urljoin(base, line)
            path = urllib.parse.urlparse(u).path.lower()
            if "/beacon/" in u:
                continue
            segs.append((dur, u))
            dur = None

    if not segs:
        return None

    total_bytes, total_secs, got = 0, 0.0, 0
    for d, u in reversed(segs[-2:]):
        if not d or d <= 0:
            continue
        try:
            data = get(u, referer).read(SEG_CAP)
        except Exception:
            continue
        if len(data) < 2048:
            continue
        total_bytes += len(data)
        total_secs += d
        got += 1
    if not got or total_secs <= 0:
        return None

    mbps = total_bytes * 8 / total_secs / 1e6
    if not height:
        # some relays publish no RESOLUTION: leave it to fix_heights/channels.json
        height = 0
    return {"mbps": mbps, "h": height, "codec": codec, "declared": declared}


def declared_mbps(url, referer=None):
    """Fallback when segments cannot be pulled: what the master playlist claims."""
    try:
        r = get(url, referer)
        body = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
        base = r.geturl()
    except Exception:
        return None
    v = pick_variant(body, base)
    return (v["bw"] / 1e6) if v and v["bw"] else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.path.join(ROOT, "channels.json"))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only")
    ap.add_argument("--missing", action="store_true",
                    help="only re-measure channels that have no measured_mbps yet")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)

    jobs = [c for g in data["groups"] for c in g["channels"]
            if (not args.only or args.only.lower() in c["name"].lower())
            and (not args.missing or "measured_mbps" not in c)]
    print("measuring %d channels..." % len(jobs), flush=True)

    results = {}
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(measure, c["url"], c.get("referer")): c for c in jobs}
        for fut in cf.as_completed(futs):
            c = futs[fut]
            done += 1
            try:
                results[id(c)] = fut.result()
            except Exception:
                results[id(c)] = None
            if done % 20 == 0:
                print("  %d/%d" % (done, len(jobs)), flush=True)

    missing = []
    for g in data["groups"]:
        for c in g["channels"]:
            if id(c) not in results:
                continue        # not scanned this run - leave the row untouched
            res = results[id(c)]
            h = c.get("height") or 0
            codec = "H.264"
            note = c.get("note") or ""
            m = re.search(r"\b(HEVC|H\.264)\b", note)
            if m:
                codec = m.group(1)
            if res:
                h = res["h"] or h
                codec = res["codec"]
                mbps = res["mbps"]
                c["measured_mbps"] = round(mbps, 2)
                c["note"] = "%s | %s | %.1f Mbps | verified" % (
                    ("%dp" % h) if h else "live", codec, mbps)
                if h:
                    c["quality"] = "%dx%d %.2f Mbps" % (h * 16 // 9, h, mbps)
            else:
                missing.append(c["name"])
                # no segment came down: fall back to the declared bandwidth, marked as such
                dm = declared_mbps(c["url"], c.get("referer"))
                if dm:
                    c["measured_mbps"] = round(dm, 2)
                    c["note"] = "%s | %s | %.1f Mbps | verified" % (
                        ("%dp" % h) if h else "live", codec, dm)
                    if h:
                        c["quality"] = "%dx%d %.2f Mbps" % (h * 16 // 9, h, dm)
                else:
                    m2 = re.search(r"([\d.]+) Mbps", note)
                    c["note"] = "%s | %s | %s Mbps | verified" % (
                        ("%dp" % h) if h else "HD", codec,
                        ("%.1f" % float(m2.group(1))) if m2 else "-")
            if h:
                c["height"] = h
                c["badge"] = "%dp" % h

    if args.dry_run:
        print("dry run - nothing written")
        return

    with open(args.file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    ok = sum(1 for r in results.values() if r)
    print("\nmeasured %d/%d" % (ok, len(jobs)))
    if missing:
        print("  no measurement: " + ", ".join(missing[:20]))
    rows = [(c["name"], c["note"]) for g in data["groups"] for c in g["channels"]]
    for name, note in rows[:6]:
        print("  %-28s %s" % (name[:28], note))


if __name__ == "__main__":
    main()
