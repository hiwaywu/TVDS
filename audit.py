#!/usr/bin/env python3
"""
audit.py - probe every channel as it exists right now, and record the verdict.

`Check` in the UI probes in the browser and forgets the result. This writes the result
into channels.json so the guide can grey out what is dead, and so you can prune the
catalogue with --prune instead of guessing.

  python audit.py                 # probe all, write status + checked_at
  python audit.py --prune         # also delete the channels that failed
  python audit.py --only "BBC"    # subset by name
  python audit.py --workers 24

Status values:
  ok      playlist + a media segment both answered
  dead    the feed refused, 404'd, or served a playlist with no working segment
  ?       the probe could not decide (timeout / DNS / TLS)
"""

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from expand import probe_stream  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "channels.json")
REPORT = os.path.join(ROOT, ".cache", "audit_report.json")


def probe(channel, attempts=2):
    last = None
    for _ in range(attempts):
        try:
            r = probe_stream(channel["url"], channel.get("referer"), 0)
        except Exception as exc:
            last = {"ok": False, "err": str(exc)[:60]}
        else:
            if r.get("ok"):
                return r
            last = r
        time.sleep(0.4)
    return last or {"ok": False, "err": "no answer"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=PATH)
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--only")
    ap.add_argument("--prune", action="store_true", help="delete channels that failed")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)

    jobs = [c for g in data["groups"] for c in g["channels"]
            if not args.only or args.only.lower() in c["name"].lower()]
    print("probing %d channels…" % len(jobs), flush=True)

    results, done = {}, 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(probe, c): c for c in jobs}
        for fut in cf.as_completed(futs):
            c = futs[fut]
            done += 1
            try:
                results[c["url"]] = fut.result()
            except Exception as exc:
                results[c["url"]] = {"ok": False, "err": str(exc)[:60]}
            if done % 25 == 0:
                print("  %d/%d  %.0fs" % (done, len(jobs), time.time() - t0), flush=True)

    dead, live, unknown = [], 0, []
    now = int(time.time())
    for g in data["groups"]:
        for c in g["channels"]:
            r = results.get(c["url"])
            if r is None:
                continue
            if r.get("ok"):
                c["status"] = "ok"
                c["checked_at"] = now
                live += 1
                if r.get("h") and not c.get("height"):
                    c["height"] = r["h"]
                    c["badge"] = "%dp" % r["h"]
            elif "segment" in (r.get("err") or "") or "HTTP" in (r.get("err") or "") or "not m3u8" in (r.get("err") or ""):
                c["status"] = "dead"
                c["checked_at"] = now
                c["status_note"] = (r.get("err") or "")[:60]
                dead.append((g["name"], c["name"], r.get("err") or ""))
            else:
                c["status"] = "?"
                c["checked_at"] = now
                unknown.append((g["name"], c["name"], r.get("err") or ""))

    if args.prune:
        cut = {c["name"] for g in data["groups"] for c in g["channels"] if c.get("status") == "dead"}
        for g in data["groups"]:
            g["channels"] = [c for c in g["channels"] if c.get("status") != "dead"]
        data["groups"] = [g for g in data["groups"] if g["channels"]]
        print("\npruned %d dead channels" % len(cut))

    total = sum(len(g["channels"]) for g in data["groups"])
    print("\nlive %d   dead %d   unknown %d   (of %d) in %.0fs" % (
        live, len(dead), len(unknown), len(jobs), time.time() - t0))
    for gname, name, err in dead:
        print("  DEAD  %-10s %-28s %s" % (gname, name[:28], err[:44]))

    if args.dry_run:
        print("dry run - nothing written")
        return

    with open(args.file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as fh:
        json.dump({"when": now, "live": live, "dead": dead, "unknown": unknown,
                   "total": total}, fh, indent=2, ensure_ascii=False)
    print("wrote %s and %s" % (args.file, REPORT))


if __name__ == "__main__":
    main()
