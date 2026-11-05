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
import calendar

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

# epgshare01 publishes rips of the real listing providers (zap2it / tvguide style), which
# carry per-channel ids such as "CMT.HD.us2". They are gzipped and much better behaved
# than epg.pw, so they are indexed first and epg.pw stays as the fallback.
SOURCES2 = {
    "US": "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz",
    "US-LOCAL": "https://epgshare01.online/epgshare01/epg_ripper_US_LOCALS1.xml.gz",
    "US-SPORT": "https://epgshare01.online/epgshare01/epg_ripper_US_SPORTS1.xml.gz",
    "GB": "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
    "CA": "https://epgshare01.online/epgshare01/epg_ripper_CA2.xml.gz",
    "HK": "https://epgshare01.online/epgshare01/epg_ripper_HK1.xml.gz",
}

# every file is indexed into the country bucket the matcher asks for
TAG_COUNTRY = {"US": "US", "US-LOCAL": "US", "US-SPORT": "US",
               "GB": "GB", "CA": "CA", "HK": "HK"}

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
    n = re.sub(r"[()\[\]]", " ", n)      # keep the words, drop only the brackets
    n = re.sub(r"\b(hd|sd|fhd|uhd|4k|1080p|720p|plus|\+1)\b", " ", n)
    n = re.sub(r"[\s_\-]+", " ", n)
    return n.strip()


def stamp(s):
    """XMLTV 'YYYYMMDDHHMMSS +0000' -> epoch seconds.

    calendar.timegm reads the 14 digits as UTC instead of local time, so this stays
    correct across a DST boundary (time.mktime - time.timezone silently shifted an hour
    twice a year). epg.pw writes +0000 on every entry, but an explicit offset is honoured
    if one ever appears.
    """
    try:
        raw = s.strip()
        base = calendar.timegm(time.strptime(raw[:14], "%Y%m%d%H%M%S"))
        parts = raw.split()
        if len(parts) > 1 and len(parts[1]) == 5:
            sign = 1 if parts[1][0] == "+" else -1
            base -= sign * (int(parts[1][1:3]) * 3600 + int(parts[1][3:5]) * 60)
        return base
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


def build(cc, path, gzipped=False):
    """Stream-parse one XMLTV file into {channels: {...}, programmes: {...}}."""
    now = time.time()
    lo, hi = now - WINDOW_BACK, now + WINDOW_FWD
    names, progs = {}, {}

    stream = gzip.open(path, "rb") if gzipped else open(path, "rb")
    try:
        ctx = ET.iterparse(stream, events=("end",))
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
    finally:
        stream.close()

    for cid in progs:
        progs[cid].sort(key=lambda p: p[0])
    return {"country": cc, "built": int(now), "channels": names, "programmes": progs}


def _merge(dst, src):
    """Fold one index into another, keeping the first (better) source for a given id."""
    for cid, names in src["channels"].items():
        dst["channels"].setdefault(cid, names)
    for cid, rows in src["programmes"].items():
        if cid not in dst["programmes"]:
            dst["programmes"][cid] = rows


def refresh(only=None, verbose=True):
    """epgshare01 first (real provider rips), then epg.pw as the fallback per country."""
    os.makedirs(CACHE, exist_ok=True)
    out = {}

    for tag, url in SOURCES2.items():
        base = TAG_COUNTRY.get(tag, tag)
        if only and base not in only:
            continue
        path = os.path.join(CACHE, tag + ".xml.gz")
        try:
            size = fetch(url, path)
        except Exception as exc:
            if verbose:
                print("  %-8s download failed: %s" % (tag, str(exc)[:60]))
            continue
        t0 = time.time()
        idx = build(base, path, gzipped=True)
        try:
            os.remove(path)
        except OSError:
            pass
        with open(os.path.join(CACHE, "share-" + tag + ".json"), "w", encoding="utf-8") as fh:
            json.dump(idx, fh, ensure_ascii=False, separators=(",", ":"))
        out[tag] = idx
        if verbose:
            print("  %-8s %6.1f MB -> %d channels, %d programmes (%.0fs)" % (
                tag, size / 1e6, len(idx["channels"]),
                sum(len(v) for v in idx["programmes"].values()), time.time() - t0))

    for cc, url in SOURCES.items():
        if only and cc not in only:
            continue
        path = os.path.join(CACHE, cc + ".xml")
        try:
            size = fetch(url, path)
        except Exception as exc:
            if verbose:
                print("  %-8s download failed: %s" % (cc, str(exc)[:60]))
            continue
        t0 = time.time()
        idx = build(cc, path)
        try:
            os.remove(path)
        except OSError:
            pass
        with open(os.path.join(CACHE, "pw-" + cc + ".json"), "w", encoding="utf-8") as fh:
            json.dump(idx, fh, ensure_ascii=False, separators=(",", ":"))
        out[cc] = idx
        if verbose:
            print("  %-8s %6.1f MB -> %d channels, %d programmes (%.0fs)" % (
                cc, size / 1e6, len(idx["channels"]),
                sum(len(v) for v in idx["programmes"].values()), time.time() - t0))
    return out


def load():
    """Merge every index into one bucket per country.

    epgshare01 files are loaded first so their (better) values win; an id that already
    exists keeps the first copy, and epg.pw only fills the gaps.
    """
    idx = {}
    if not os.path.isdir(CACHE):
        return idx

    def priority(fn):
        return (0 if fn.startswith("share-") else 1, fn)

    for fn in sorted(os.listdir(CACHE), key=priority):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(CACHE, fn), encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        cc = d.get("country", fn[:-5])
        if cc in idx:
            _merge(idx[cc], d)
        else:
            idx[cc] = d
    return idx


def _needles(channel_name):
    key = norm(channel_name)
    out = {key}
    for alias in ALIASES.get(key, []):
        out.add(norm(alias))
    return {n for n in out if n}


MAP_FILE = os.path.join(ROOT, "guide_map.json")
# writable location for user tweaks; the frozen build points this at %LOCALAPPDATA%\tvapp
DATA_DIR = os.environ.get("TVDS_DATA_DIR") or ROOT
OFFSET_FILE = os.path.join(DATA_DIR, "guide_offset.json")


def load_offsets():
    """{channel: minutes} - a per-channel nudge; None means 'hide these listings'."""
    try:
        with open(OFFSET_FILE, encoding="utf-8") as fh:
            return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    except Exception:
        return {}


def set_offset(name, value):
    """value: int minutes, None to hide listings, 'clear' to forget the channel."""
    data = load_offsets()
    if value == "clear":
        data.pop(name, None)
    elif value is None:
        data[name] = None
    else:
        data[name] = int(value)
    try:
        os.makedirs(os.path.dirname(OFFSET_FILE), exist_ok=True)
        with open(OFFSET_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
    except Exception:
        pass
    return data

# suffixes that may follow a channel name in a guide and still mean the same channel
SAFE_SUFFIX = {"hd", "sd", "fhd", "east", "west", "eastern", "pacific", "us", "tv",
               "canada", "canadian", "feed", "channel", "network"}


def load_map():
    try:
        with open(MAP_FILE, encoding="utf-8") as fh:
            return {k.lower(): v for k, v in json.load(fh).items() if not k.startswith("_")}
    except Exception:
        return {}


def _strict_eq(a, b):
    """Same channel when the guide name is our name plus only harmless suffixes."""
    if a == b:
        return True
    if not b.startswith(a):
        return False
    for tail in b[len(a):].split():
        if tail not in SAFE_SUFFIX:
            return False
    return True


def guide(channels, idx=None, full=False):
    """channels: [(group, name)] -> {name: {now, next, src, matched, progs?}}

    Matching is deliberately strict and country-scoped: a channel only ever takes listings
    from its own country's index, and only when the guide's display name is the same name
    (bar an "HD"/"East" style suffix) or an explicit alias. Loose substring matching used
    to put a Hong Kong schedule on HBO Family, which is worse than no guide at all.
    """
    idx = idx if idx is not None else load()
    now = time.time()
    lo = now - 3600
    hi = now + 6 * 3600
    pins = load_map()
    offsets = load_offsets()

    per_country = {}
    for cc, d in idx.items():
        table = per_country.setdefault(cc, {})
        for cid, names in d.get("channels", {}).items():
            for dn in names:
                table.setdefault(norm(dn), []).append((cid, dn))

    out = {}
    for group, name in channels:
        want = GROUP_COUNTRY.get(group)
        table = per_country.get(want) if want else None
        if not table:
            continue

        pick = None
        pin = pins.get(name.lower())
        if pin:
            for key, cands in table.items():
                if key == norm(pin):
                    pick = cands[0]
                    break
        if not pick:
            for needle in _needles(name):
                best, best_extra = None, 99.0
                for key, cands in table.items():
                    if _strict_eq(needle, key):
                        extra = len(key.split()) - len(needle.split())
                        if "pacific" in key or "west" in key:
                            extra += 0.5          # a US viewer wants the East feed
                        elif "east" in key:
                            extra -= 0.5
                        if extra < best_extra:
                            best, best_extra = cands[0], extra
                            if extra == 0:
                                break
                if best:
                    pick = best
                    break
        if not pick:
            continue

        cid, matched = pick
        rows = idx[want]["programmes"].get(cid) or []

        # a per-channel nudge, when the provider's clock does not line up with the feed
        shift = offsets.get(name, 0)
        if shift is None:
            continue
        shift = int(shift) * 60 if shift else 0
        if shift:
            rows = [[r[0] + shift, r[1] + shift, r[2]] for r in rows]

        cur = next((r for r in rows if r[0] <= now < r[1]), None)
        nxt = next((r for r in rows if r[0] > now), None)
        if not (cur or nxt):
            continue
        rec = {
            "src": want,
            "matched": matched,
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
