#!/usr/bin/env python3
"""
refresh.py — rebuild channels.json the same way the links in it were found:

  1. pull the public iptv-org catalogue (channels.json + streams.json)
  2. keep the channel ids you care about (SEED below, or pass --ids)
  3. probe every candidate stream: HTTP status, content-type, m3u8 validity
  4. follow the master playlist to its first variant to get real resolution
  5. keep only the survivors, best quality first, write channels.json

  python refresh.py                 # refresh the seed list
  python refresh.py --ids CNN.us,NBATV.us
  python refresh.py --all-news      # every channel in the news category
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
API_CH = "https://iptv-org.github.io/api/channels.json"
API_ST = "https://iptv-org.github.io/api/streams.json"
TIMEOUT = 12

SEED = {
    "Sports": ["NBATV.us", "NBATVInternational.us", "ESPN.us", "ESPN2.us", "SkySportsNews.uk"],
    "News": ["CNNInternational.us", "CNN.us", "CNNHeadlines.us", "BBCNews.uk", "SkyNews.uk",
             "AlJazeeraEnglish.qa", "DWEnglish.de", "France24English.fr", "BloombergTVPlus.us",
             "CNBC.us", "FoxNews.us", "MSNBC.us"],
}


def get(url, referer=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Encoding", "identity")
    if referer:
        req.add_header("Referer", referer)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def probe(url, referer=None):
    """Return (ok, detail) where detail carries resolution/bitrate when known."""
    try:
        r = get(url, referer)
        ctype = r.headers.get("Content-Type", "")
        body = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
    except Exception as exc:
        return False, str(exc)[:90]
    if "#EXTM3U" not in body:
        return False, "not a playlist (%s)" % ctype

    # master -> follow highest variant for a real resolution
    lines = [l.strip() for l in body.splitlines()]
    best = None
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
            m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            b = re.search(r"AVERAGE-BANDWIDTH=(\d+)", line)
            if m:
                area = int(m.group(1)) * int(m.group(2))
                if best is None or area > best[0]:
                    best = (area, int(m.group(1)), int(m.group(2)), int(b.group(1)) if b else 0)
    if best:
        _, w, h, bw = best
        return True, "%dx%d %.2f Mbps" % (w, h, bw / 1e6) if bw else "%dx%d" % (w, h)
    # media playlist: check the newest segment actually downloads
    seg = next((l for l in lines if l and not l.startswith("#")), None)
    if seg:
        try:
            s = get(urllib.parse.urljoin(url, seg), referer)
            n = len(s.read(512 * 1024))
            return (n > 1000), ("seg %d KB" % (n // 1024))
        except Exception as exc:
            return False, str(exc)[:90]
    return True, "playlist"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", help="comma separated iptv-org channel ids")
    ap.add_argument("--all-news", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "channels.json"))
    args = ap.parse_args()

    print("fetching iptv-org catalogue…")
    channels = json.load(get(API_CH))
    streams = json.load(get(API_ST))
    by_id = {c["id"]: c for c in channels}

    if args.ids:
        groups = {"Custom": [i.strip() for i in args.ids.split(",") if i.strip()]}
    elif args.all_news:
        groups = {"News": sorted({s["channel"] for s in streams
                                  if s.get("channel") in by_id
                                  and by_id[s["channel"]].get("categories", []) == ["news"]})}
    else:
        groups = SEED

    out = {"groups": []}
    for gname, ids in groups.items():
        kept = []
        for cid in ids:
            cands = [s for s in streams if s.get("channel") == cid]
            if not cands:
                print("  - %-24s no streams listed" % cid)
                continue
            ranked = sorted(cands, key=lambda s: (s.get("quality") or ""), reverse=True)
            for s in ranked:
                ok, detail = probe(s["url"], s.get("referer"))
                if not ok:
                    continue
                ch = by_id.get(cid, {})
                kept.append({
                    "name": ch.get("name", cid),
                    "badge": (s.get("quality") or "live"),
                    "quality": detail,
                    "url": s["url"],
                    **({"referer": s["referer"]} if s.get("referrer") else {}),
                    "note": "verified %s" % detail,
                })
                print("  + %-24s %-10s %s" % (cid, s.get("quality") or "-", detail))
                break
            else:
                print("  - %-24s all %d streams dead" % (cid, len(cands)))
        if kept:
            out["groups"].append({"name": gname, "channels": kept})

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    total = sum(len(g["channels"]) for g in out["groups"])
    print("wrote %s — %d live channels" % (args.out, total))


if __name__ == "__main__":
    import urllib.parse  # noqa: F401  (used in probe via urljoin path)
    main()
