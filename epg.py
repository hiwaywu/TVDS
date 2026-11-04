#!/usr/bin/env python3
"""
epg.py - TV guide for the catalogue.

Sources are public XMLTV dumps from epg.pw, one file per country. They are big (US is
161 MB), so this module streams them with iterparse, keeps a 36-hour window, and writes a
compact JSON index that the server reads on every /api/epg request.

  python epg.py --refresh            # download + index HK, CA, US
  python epg.py --refresh --only CA
  python epg.py --query "CNN"        # what does the index say right now
"""

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
# the frozen PC build cannot write next to its bundled files, so the exe points this at
# %LOCALAPPDATA%\tvapp\epg through TVDS_EPG_CACHE
CACHE = os.environ.get("TVDS_EPG_CACHE") or os.path.join(ROOT, ".cache", "epg")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SOURCES = {
    "HK": "https://epg.pw/xmltv/epg_HK.xml",
    "CA": "https://epg.pw/xmltv/epg_CA.xml",
    "US": "https://epg.pw/xmltv/epg_US.xml",
    "GB": "https://epg.pw/xmltv/epg_GB.xml",
}

WINDOW_BACK = 6 * 3600          # keep programmes that started up to 6 h ago
WINDOW_FWD = 36 * 3600          # and up to 36 h ahead

# our name -> extra needles to look for in a guide's display-name
ALIASES = {
    "tvb jade": ["翡翠台", "jade"],
    "tvb pearl": ["明珠台", "pearl"],
    "tvb news": ["無綫新聞", "无线新闻", "tvb news"],
    "tvb jade north america": ["翡翠台北美", "jade"],
    "viutv": ["viutv", "viu"],
    "viutvsix": ["viutvsix", "viu six", "viu6"],
    "hoy tv 77": ["hoy", "77台"],
    "hoy info 78": ["hoy", "78台", "hoy infotainment"],
    "now news": ["now新聞", "now news"],
    "rthk tv 31": ["港台电视31", "港台電視31", "rthk 31"],
    "rthk tv 32": ["港台电视32", "港台電視32", "rthk 32"],
    "rthk tv 33": ["港台电视33", "港台電視33", "rthk 33"],
    "abc 7 wabc (new york)": ["wabc", "abc 7"],
    "cbs 2 new york": ["wcbs", "cbs 2"],
    "fox 5 wnyw (new york)": ["wnyw", "fox 5"],
    "cbs news new york": ["wcbs", "cbs news new york"],
    "news 12 new york": ["news 12", "news12"],
    "fox news channel": ["fox news"],
    "bbc news": ["bbc news", "bbc world news"],
    "the weather channel": ["weather channel"],
    "the weather network": ["weather network"],
    "ctv": ["ctv", "cfcf", "ctv toronto"],
    "ctv news channel": ["ctv news channel", "ctv news"],
    "citynews toronto": ["citytv toronto", "citynews"],
    "citynews vancouver": ["citytv vancouver", "citynews"],
    "citynews calgary": ["citytv calgary", "citynews"],
    "citynews edmonton": ["citytv edmonton", "citynews"],
    "cp24": ["cp24"],
    "cbc news network": ["cbc news network", "cbc news"],
    "global": ["global toronto", "global"],
}

# a channel's group tells us which country file to prefer when a name exists in several
GROUP_COUNTRY = {
    "Hong Kong": "HK", "US Cable": "US", "Sports": "US", "News": "US",
    "New York": "US", "UK": "GB", "Canada": "CA",
}


def norm(name):
    n = (name or "").lower()
    n = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", n)
    n = re.sub(r"\b(hd|sd|fhd|uhd|4k|1080p|720p|plus|\+1)\b", " ", n)
    n = re.sub(r"[\s_\-]+", " ", n)
    return n.strip()


def stamp(s):
    """XMLTV 'YYYYMMDDHHMMSS +0000' -> epoch seconds (UTC)."""
    try:
        return int(time.mktime(time.strptime(s.strip()[:14], "%Y%m%d%H%M%S")) - time.timezone)
    except Exception:
        return 0


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    os.replace(tmp, dest)
    return os.path.getsize(dest)


def build(cc, path):
    """Stream-parse one XMLTV file into {channels: {...}, programmes: {...}}."""
    now = time.time()
    lo, hi = now - WINDOW_BACK, now + WINDOW_FWD
    names, progs = {}, {}

    ctx = ET.iterparse(path, events=("end",))
    for _, el in ctx:
        if el.tag == "channel":
            cid = el.get("id")
            dn = [d.text.strip() for d in el.findall("display-name") if d.text]
            if cid and dn:
                names[cid] = dn
            el.clear()
        elif el.tag == "programme":
            cid = el.get("channel")
            start, stop = stamp(el.get("start", "")), stamp(el.get("stop", ""))
            title = el.findtext("title") or ""
            if cid and title and stop and start <= hi and stop >= lo:
                progs.setdefault(cid, []).append([start, stop, title.strip()])
            el.clear()

    for cid in progs:
        progs[cid].sort(key=lambda p: p[0])
    return {"country": cc, "built": int(now), "channels": names, "programmes": progs}


def refresh(only=None, verbose=True):
    os.makedirs(CACHE, exist_ok=True)
    out = {}
    for cc, url in SOURCES.items():
        if only and cc not in only:
            continue
        path = os.path.join(CACHE, cc + ".xml")
        try:
            size = fetch(url, path)
        except Exception as exc:
            if verbose:
                print("  %-2s download failed: %s" % (cc, str(exc)[:60]))
            continue
        t0 = time.time()
        idx = build(cc, path)
        os.replace(path, path + ".done") if False else None
        try:
            os.remove(path)
        except OSError:
            pass
        with open(os.path.join(CACHE, cc + ".json"), "w", encoding="utf-8") as fh:
            json.dump(idx, fh, ensure_ascii=False, separators=(",", ":"))
        out[cc] = idx
        if verbose:
            print("  %-2s %6.1f MB -> %d channels, %d programmes (%.0fs)" % (
                cc, size / 1e6, len(idx["channels"]),
                sum(len(v) for v in idx["programmes"].values()), time.time() - t0))
    return out


def load():
    idx = {}
    if not os.path.isdir(CACHE):
        return idx
    for fn in os.listdir(CACHE):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(CACHE, fn), encoding="utf-8") as fh:
                    d = json.load(fh)
                idx[d.get("country", fn[:-5])] = d
            except Exception:
                continue
    return idx


def _needles(channel_name):
    key = norm(channel_name)
    out = {key}
    for alias in ALIASES.get(key, []):
        out.add(norm(alias))
    return {n for n in out if n}


def guide(channels, idx=None, full=False):
    """channels: [(group, name)] -> {name: {now, next, src, progs?}}

    full=True also returns the programme list inside the visible window, which is what
    the grid view draws.
    """
    idx = idx if idx is not None else load()
    now = time.time()
    lo = now - 3600
    hi = now + 6 * 3600
    table = {}
    for cc, d in idx.items():
        for cid, names in d.get("channels", {}).items():
            for dn in names:
                table.setdefault(norm(dn), []).append((cc, cid))

    def candidates(needles):
        out = []
        for needle in needles:
            out.extend(table.get(needle, []))
        if out:
            return out
        for needle in needles:
            if len(needle) < 3:
                continue
            for key, cands in table.items():
                if needle in key or (len(key) > 3 and key in needle):
                    out.extend(cands)
        return out

    out = {}
    for group, name in channels:
        cands = candidates(_needles(name))
        if not cands:
            continue
        want = GROUP_COUNTRY.get(group)
        pick = next((c for c in cands if c[0] == want), cands[0])
        cc, cid = pick
        rows = idx[cc]["programmes"].get(cid) or []
        cur = next((r for r in rows if r[0] <= now < r[1]), None)
        nxt = next((r for r in rows if r[0] > now), None)
        if not (cur or nxt):
            continue
        rec = {
            "src": cc,
            "now": {"title": cur[2], "start": cur[0], "stop": cur[1]} if cur else None,
            "next": {"title": nxt[2], "start": nxt[0], "stop": nxt[1]} if nxt else None,
        }
        if full:
            rec["progs"] = [[r[0], r[1], r[2]] for r in rows if r[1] > lo and r[0] < hi][:40]
        out[name] = rec
    return out


def index_age():
    """Seconds since the newest index was built, or None when there is none."""
    try:
        stamps = [os.path.getmtime(os.path.join(CACHE, f)) for f in os.listdir(CACHE)
                  if f.endswith(".json")]
    except OSError:
        return None
    return (time.time() - max(stamps)) if stamps else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--only", help="comma separated country codes")
    ap.add_argument("--query")
    args = ap.parse_args()

    if args.refresh:
        only = set(args.only.split(",")) if args.only else None
        t0 = time.time()
        print("refreshing guide…", flush=True)
        refresh(only)
        print("done in %.0fs" % (time.time() - t0))
        return

    if args.query:
        data = json.load(open(os.path.join(ROOT, "channels.json"), encoding="utf-8"))
        chans = [(g["name"], c["name"]) for g in data["groups"] for c in g["channels"]]
        g = guide(chans)
        hits = {k: v for k, v in g.items() if args.query.lower() in k.lower()}
        print(json.dumps(hits, indent=2, ensure_ascii=False))
        print("matched %d of %d channels overall" % (len(g), len(chans)))


if __name__ == "__main__":
    main()
