#!/usr/bin/env python3
"""
latency_scan.py - how long does each channel actually take to start playing?

A channel can pass the health check and still be useless if the first segment takes
10 seconds to arrive. This measures, per channel, three numbers that map to what the
player experiences:

  master   ms to fetch the master playlist
  variant  ms to fetch the chosen media playlist
  segment  ms to fetch the newest segment through the local proxy (the real stall)

Writes load_ms (segment) and status: ok|slow|dead into channels.json.

  python latency_scan.py                 # all channels
  python latency_scan.py --only TNT
  python latency_scan.py --slow 3000     # threshold for "slow", default 3000 ms
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

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "channels.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def get(url, referer=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        final = r.geturl()
    return int((time.perf_counter() - t0) * 1000), body, final


def measure(channel, proxy=None):
    url, ref = channel["url"], channel.get("referer")
    out = {"master": 0, "variant": 0, "segment": 0, "err": ""}
    try:
        ms, body, final = get(url, ref)
        out["master"] = ms
        text = body.decode("utf-8", "replace")
        if "#EXTINF" not in text:
            lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
            if not lines:
                out["err"] = "empty master"
                return out
            nxt = lines[0] if not lines[0].startswith("http") else lines[0]
            if not nxt.startswith("http"):
                nxt = urllib.parse.urljoin(final, nxt)
            ms, body, final = get(nxt, ref)
            out["variant"] = ms
            text = body.decode("utf-8", "replace")
        segs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        if not segs:
            out["err"] = "no segments"
            return out
        seg = segs[-1]
        if not seg.startswith("http"):
            seg = urllib.parse.urljoin(final, seg)
        # measure through the proxy when it is running: that is the path the player takes
        target = (proxy + "/proxy?u=" + urllib.parse.quote(seg, safe="") +
                  ("&r=" + urllib.parse.quote(ref, safe="") if ref else "")) if proxy else seg
        ms, body, _ = get(target, None if proxy else ref)
        out["segment"] = ms
        out["bytes"] = len(body)
        if len(body) < 20000:
            out["err"] = "segment too small"
    except Exception as exc:
        out["err"] = str(exc)[:60]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=PATH)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--slow", type=int, default=3000)
    ap.add_argument("--only")
    ap.add_argument("--proxy", default="http://127.0.0.1:8777")
    args = ap.parse_args()

    data = json.load(open(args.file, encoding="utf-8"))
    rows = [(g["name"], c) for g in data["groups"] for c in g["channels"]
            if not args.only or args.only.lower() in c["name"].lower()]
    print("measuring %d channels through %s" % (len(rows), args.proxy), flush=True)

    results = {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(measure, c, args.proxy): (g, c) for g, c in rows}
        for fut in cf.as_completed(futs):
            g, c = futs[fut]
            try:
                results[c["url"]] = fut.result()
            except Exception as exc:
                results[c["url"]] = {"master": 0, "variant": 0, "segment": 0, "err": str(exc)[:60]}

    slow, dead, fine = [], [], []
    for g in data["groups"]:
        for c in g["channels"]:
            r = results.get(c["url"])
            if r is None:
                continue
            total = r["master"] + r["variant"] + r["segment"]
            c["load_ms"] = total
            if r["err"] or total > 20000:
                c["status"] = "dead"
                dead.append((total, c["name"], r["err"]))
            elif r["segment"] > args.slow:
                c["status"] = "slow"
                slow.append((r["segment"], c["name"], total))
            else:
                c["status"] = "ok"
                fine.append((r["segment"], c["name"]))

    print("\n%-6s %-9s %-9s %-26s %s" % ("total", "master", "segment", "channel", "note"))
    for seg, name, total in sorted(slow, reverse=True)[:15]:
        print("%6d %9s %9d %-26s slow" % (total, "-", seg, name[:26]))
    for total, name, err in sorted(dead, reverse=True)[:10]:
        print("%6d %9s %9s %-26s %s" % (total, "-", "-", name[:26], err[:26]))

    print("\nfast %d   slow %d   dead %d   (%.0fs)" % (len(fine), len(slow), len(dead), time.time() - t0))
    if fine:
        vals = sorted(v for v, _ in fine)
        print("median segment %d ms, p90 %d ms" % (vals[len(vals) // 2], vals[int(len(vals) * 0.9) - 1]))

    with open(args.file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print("wrote", args.file)


if __name__ == "__main__":
    main()
