#!/usr/bin/env python3
"""
import_m3u.py — pull any external m3u playlist, probe every entry, keep the best
working copy of each channel name, and merge it into channels.json as a group.

  python import_m3u.py --url https://.../hk_live.m3u --group "Hong Kong"
  python import_m3u.py --url <playlist> --group Sports --min-height 720 --replace
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import probe, get  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def parse_m3u(text):
    entries, name, logo = [], None, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r",([^,]*)$", line)
            name = (m.group(1).strip() if m else "unknown")
            lg = re.search(r'logo="([^"]+)"', line)
            logo = lg.group(1) if lg else None
        elif line.startswith("#"):
            continue
        elif name:
            entries.append({"name": name, "url": line, "logo": logo})
            name = None
    return entries


def clean(name):
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"(?i)\s*(hd|fhd|uhd|1080p|720p|4k|高清|超清|蓝光)\s*$", "", name)
    return name.strip() or "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--group", default="Imported")
    ap.add_argument("--out", default=os.path.join(ROOT, "channels.json"))
    ap.add_argument("--min-height", type=int, default=720)
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--replace", action="store_true", help="replace the group instead of merging")
    args = ap.parse_args()

    print("fetching playlist…", flush=True)
    text = get(args.url).read().decode("utf-8", "replace")
    entries = parse_m3u(text)
    print("%d entries" % len(entries), flush=True)

    hits = {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(probe, e["url"]): e for e in entries}
        done = 0
        for fut in cf.as_completed(futs):
            e = futs[fut]
            done += 1
            if done % 40 == 0:
                print("  %d/%d %.0fs" % (done, len(entries), time.time() - t0), flush=True)
            try:
                ok, h, bw, _ = fut.result()
            except Exception:
                continue
            if not ok:
                continue
            if h and h < args.min_height:
                continue
            key = clean(e["name"])
            hits.setdefault(key, []).append((h, bw, e))

    chans = []
    for key, lst in hits.items():
        lst.sort(key=lambda x: (x[0], x[1]), reverse=True)
        h, bw, e = lst[0]
        chans.append({
            "name": key,
            "badge": ("%dp" % h) if h else "live",
            "height": h,
            "quality": ("%dx%d %.2f Mbps" % (h * 16 // 9, h, bw / 1e6)) if h and bw else ("%dp" % h if h else "?"),
            "url": e["url"],
            "note": ("%dp" % h if h else "live") + (" / %.1f Mbps" % (bw / 1e6) if bw else "") + " · verified",
            **({"logo": e["logo"]} if e.get("logo") else {}),
        })
    chans.sort(key=lambda c: (-(c["height"] or 0), c["name"]))

    try:
        with open(args.out, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {"groups": []}

    groups = [g for g in data.get("groups", []) if g["name"] != args.group] if args.replace \
        else [g for g in data.get("groups", []) if g["name"] != args.group]
    # merge with an existing same-name group if present and not replacing
    if not args.replace:
        old = [g for g in data.get("groups", []) if g["name"] == args.group]
        if old:
            seen = {c["url"] for c in chans}
            chans = chans + [c for c in old[0]["channels"] if c["url"] not in seen]
            chans.sort(key=lambda c: (-(c.get("height") or 0), c["name"]))
    groups.append({"name": args.group, "channels": chans})
    data["groups"] = groups

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print("\n%s: %d live channels -> %s" % (args.group, len(chans), args.out))
    for c in chans[:40]:
        print("  %-28s %-10s %s" % (c["name"][:28], c["badge"], c["url"][:70]))


if __name__ == "__main__":
    main()
