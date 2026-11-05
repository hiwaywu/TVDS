#!/usr/bin/env python3
"""
guide_audit.py - how trustworthy is each channel's guide?

Two independent providers (epgshare01 rips vs epg.pw) are consulted for the same channel.
If they disagree about what is on right now, at least one is wrong, and showing it to the
user is worse than showing nothing.

  python guide_audit.py                 # report only
  python guide_audit.py --apply         # write guide_confidence + hide the bad ones
"""

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import epg  # noqa: E402

PATH = os.path.join(ROOT, "channels.json")


def provider_index(prefix):
    """Load only the indexes written by one provider family."""
    idx = {}
    if not os.path.isdir(epg.CACHE):
        return idx
    for fn in sorted(os.listdir(epg.CACHE)):
        if not fn.endswith(".json") or not fn.startswith(prefix):
            continue
        try:
            d = json.load(open(os.path.join(epg.CACHE, fn), encoding="utf-8"))
        except Exception:
            continue
        cc = d.get("country", fn[:-5])
        if cc in idx:
            epg._merge(idx[cc], d)
        else:
            idx[cc] = d
    return idx


def now_for(channel_name, group, idx):
    g = epg.guide([(group, channel_name)], idx=idx)
    rec = g.get(channel_name)
    return (rec["now"]["title"] if rec and rec.get("now") else None,
            rec["matched"] if rec else None)


def norm_title(t):
    return "".join(ch for ch in (t or "").lower() if ch.isalnum())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=PATH)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    share = provider_index("share-")
    pw = provider_index("pw-")
    print("indexes: share=%s pw=%s" % (
        {k: len(v["channels"]) for k, v in share.items()},
        {k: len(v["channels"]) for k, v in pw.items()}))

    data = json.load(open(args.file, encoding="utf-8"))
    rows = [(g["name"], c) for g in data["groups"] for c in g["channels"]]

    same = one = none = disagree = 0
    conflicted = []
    for gname, c in rows:
        a, a_match = now_for(c["name"], gname, share)
        b, b_match = now_for(c["name"], gname, pw)
        if a and b:
            if norm_title(a) == norm_title(b):
                same += 1
                c["guide_confidence"] = "agreed"
            else:
                disagree += 1
                c["guide_confidence"] = "conflict"
                conflicted.append((gname, c["name"], a, b))
        elif a or b:
            one += 1
            c["guide_confidence"] = "single"
        else:
            none += 1
            c["guide_confidence"] = "none"

    print("\nagreed %d · single-source %d · conflict %d · none %d   (of %d)"
          % (same, one, disagree, none, len(rows)))
    for gname, name, a, b in conflicted[:25]:
        print("  CONFLICT %-10s %-22s share=%-28s pw=%s" % (gname, name[:22], a[:28], b[:28]))

    if args.apply:
        for gname, c in rows:
            if c.get("guide_confidence") == "conflict":
                c["guide_hidden"] = True
        json.dump(data, open(args.file, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        print("\napplied: conflicting listings marked hidden")
    elif args.apply is False:
        print("\n(report only - pass --apply to hide conflicting listings)")


if __name__ == "__main__":
    main()
