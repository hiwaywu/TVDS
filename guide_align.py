#!/usr/bin/env python3
"""
guide_align.py - find the systematic clock offset between the two guide providers.

They disagree on "what is on now" for 88 of 102 channels, and the pairs look like adjacent
programmes in the same block ("Expedition X" / "Ghost Adventures"). That is the signature
of a time shift, not of bad data. This measures it: for each channel, slide one provider's
timeline against the other and keep the offset that lines the most programme titles up.

If one offset dominates, it is a provider-wide clock bug and can be corrected once; if the
offsets are scattered, the providers are simply listing different feeds and the guide
should stay hidden for those channels.

  python guide_align.py
"""

import argparse
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import epg  # noqa: E402

PATH = os.path.join(ROOT, "channels.json")
STEPS = range(-4 * 3600, 4 * 3600 + 1, 900)      # -4h .. +4h in 15-minute steps


def provider_index(prefix):
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


def rows_for(channel_name, group, idx):
    """All programmes of the channel the matcher would pick, as [(start, stop, title)]."""
    per = {}
    for cc, d in idx.items():
        for cid, names in d.get("channels", {}).items():
            for dn in names:
                per.setdefault(epg.norm(dn), []).append((cc, cid))
    want = epg.GROUP_COUNTRY.get(group)
    table = {k: v for k, v in per.items() if not want or any(c == want for c, _ in v)}
    pick = None
    for needle in epg._needles(channel_name):
        best, best_extra = None, 99.0
        for key, cands in table.items():
            if epg._strict_eq(needle, key):
                extra = len(key.split()) - len(needle.split())
                if extra < best_extra:
                    best, best_extra = cands[0], extra
        if best:
            pick = best
            break
    if not pick:
        return []
    cc, cid = pick
    return idx[cc]["programmes"].get(cid) or []


def best_offset(a_rows, b_rows):
    if not a_rows or not b_rows:
        return None, 0
    a_titles = {epg.norm(r[2]) for r in a_rows}
    best, hits = 0, -1
    for d in STEPS:
        score = 0
        for start, stop, title in b_rows:
            if epg.norm(title) in a_titles:
                shifted = start + d
                if any(r[0] <= shifted < r[1] for r in a_rows):
                    score += 1
        if score > hits:
            best, hits = d, score
    return best, hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=PATH)
    args = ap.parse_args()

    share = provider_index("share-")
    pw = provider_index("pw-")
    data = json.load(open(args.file, encoding="utf-8"))

    votes = collections.Counter()
    per_channel = []
    for g in data["groups"]:
        for c in g["channels"]:
            a = share and rows_for(c["name"], g["name"], share)
            b = pw and rows_for(c["name"], g["name"], pw)
            d, hits = best_offset(a, b)
            if d is None or hits < 3:
                continue
            votes[d] += 1
            per_channel.append((d, hits, c["name"]))

    print("channels with enough overlap:", len(per_channel))
    for d, n in votes.most_common(8):
        print("  offset %+5d min  on %d channels" % (d // 60, n))
    print()
    for d, hits, name in sorted(per_channel, key=lambda x: -x[1])[:15]:
        print("  %-24s %+4d min  (%d titles overlap)" % (name[:24], d // 60, hits))

    if votes:
        modal, count = votes.most_common(1)[0]
        print("\nmodal offset: %+d min across %d/%d channels" % (modal // 60, count, len(per_channel)))
        if count >= len(per_channel) * 0.5:
            print("=> provider-wide shift; correcting by shifting the fallback index")
            with open(os.path.join(ROOT, ".cache", "guide_shift.json"), "w") as fh:
                json.dump({"seconds": modal, "applies_to": "pw"}, fh)
        else:
            print("=> offsets are scattered: providers list different feeds, so no single fix")


if __name__ == "__main__":
    main()
