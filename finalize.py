#!/usr/bin/env python3
"""
finalize.py — merge every harvested json, keep only real channels, resolve the
codec, prefer H.264 for in-browser playback, and write the final channels.json.

  python finalize.py

Reads:  main.json  hk2.json  us_imp.json  uk_imp.json  ca_imp.json
Writes: channels.json
"""

import concurrent.futures as cf
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import get  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "channels.json")
SEP = " | "

NOISE = re.compile(
    r"\s*(\((?:1080p?|720p?|2160p?|4k|uhd|hd|sd|fhd|hq|backup|alt|geo[- ]?blocked)\)"
    r"|\[(?:not 24/7|geo[- ]?blocked|24/7|1080p?|720p?|4k|hd|sd|backup)\])",
    re.I)


def clean(name):
    prev = None
    n = name
    while prev != n:
        prev = n
        n = NOISE.sub("", n)
    n = re.sub(r"\s{2,}", " ", n).strip(" -|")
    return n or name


US_KEEP = re.compile(r"""^(
  ESPN|ESPN2|ESPNU|ESPNews|ESPN Deportes|Fox Sports 1|Fox Sports 2|FS1|FS2|
  NBA TV|MLB Network|NFL Network|NHL Network|Golf Channel|Tennis Channel|
  Big Ten Network|SEC Network|ACC Network|Pac-12|CBS Sports Network|NBC Sports|
  YES Network|MSG|SportsNet|Altitude|Stadium|
  CNN|HLN|Fox News|Fox Business|MSNBC|CNBC|Bloomberg|Newsmax|NewsNation|C-SPAN|Cheddar|
  HBO|Cinemax|Showtime|Starz|Encore|MGM\+|Epix|
  AMC|BBC America|IFC|Sundance|FXM|FX|FXX|Freeform|TNT|TBS|truTV|USA Network|
  Syfy|Comedy Central|Paramount Network|MTV|VH1|BET|Nickelodeon|Cartoon Network|Adult Swim|
  Disney|Discovery|History|TLC|Animal Planet|Science|Investigation Discovery|ID\b|
  National Geographic|Nat Geo|Food Network|HGTV|Bravo|E!|Oxygen|WE tv|Lifetime|Hallmark|
  A&E|OWN|Travel Channel|Cooking Channel|Destination America|American Heroes|
  Reelz|AXS|Revolt|TV One|Bounce|Grit|Court TV|Justice|Pop|
  Telemundo|Univision|Galavision|TUDN|Fox Deportes|
  AccuWeather|Weather|NASA|PBS
)\b""", re.I | re.X)

UK_KEEP = re.compile(r"""^(
  BBC|ITV|Channel 4|Channel 5|S4C|STV|UTV|Sky|Dave|Gold|Alibi|Drama|Really|Quest|
  E4|Film4|More4|4seven|5USA|5STAR|Blaze|GB News|Talk|LBC|TNT Sports|Premier Sports|
  Comedy Central|MTV|VH1|Discovery|History|TLC|Animal Planet|National Geographic|
  Food Network|HGTV|E!|Sky Arts|Sky Crime|Sky Comedy|Sky Documentaries|Sky History|
  Sky Nature|Sky News|Sky Sports|Sky Cinema|Nickelodeon|Cartoon Network|
  Together|That's|Kerrang|Magic|PBS America|NBC News
)\b""", re.I | re.X)

CA_KEEP = re.compile(r"""^(
  TSN|Sportsnet|CBC|CTV|Global|CityTV|Much|Crave|Showcase|Treehouse|YTV|Teletoon|
  CTV News|CTV News Channel|BNN|Business News Network|The Weather Network|
  HGTV Canada|Food Network Canada|W Network|Slice|History Canada|Discovery Canada|
  OMNI|TVA|V|Noovo|RDS|TVA Sports|Super Channel|Hollywood Suite|OutTV
)\b""", re.I | re.X)

# Hong Kong: collapse the duplicates the playlists ship (5 copies of Jade, 3 of RTHK 31)
HK_ALIAS = [
    (r"4K", "TVB Jade 4K"),
    (r"北美|North America", "TVB Jade North America"),
    (r"明珠|Pearl", "TVB Pearl"),
    (r"無線新聞|无线新闻|TVB News", "TVB News"),
    (r"財經體育|财经体育|Finance", "TVB Finance & Sports"),
    (r"\bJ2\b", "TVB J2"),
    (r"ViuTVsix|VIUTVSIX", "ViuTVsix"),
    (r"ViuTV|VIUTV", "ViuTV"),
    (r"HOY ?(?:資訊|Info)|HOY ?78", "HOY Info 78"),
    (r"HOY ?76", "HOY 76"),
    (r"HOY", "HOY TV 77"),
    (r"31", "RTHK TV 31"),
    (r"32", "RTHK TV 32"),
    (r"33", "RTHK TV 33"),
    (r"有線新聞|有线新闻|i-CABLE", "i-CABLE News"),
    (r"now ?新聞|now News", "now News"),
    (r"翡翠|Jade", "TVB Jade"),
]


def canon_hk(name):
    for pat, target in HK_ALIAS:
        if re.search(pat, name, re.I):
            return target
    return name


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("groups", [])
    except Exception:
        return []


def pick_avc(url, referer=None):
    """Resolve the variant list; prefer the best H.264 variant, else the best HEVC one."""
    try:
        r = get(url, referer)
        body = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
        final = r.geturl()
    except Exception:
        return None
    if "#EXTM3U" not in body:
        return None
    rows, pending = [], None
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            bw = re.search(r"AVERAGE-BANDWIDTH=(\d+)", line) or re.search(r"BANDWIDTH=(\d+)", line)
            cd = re.search(r'CODECS="([^"]+)"', line)
            codecs = (cd.group(1) if cd else "").lower()
            pending = {"h": int(res.group(2)) if res else 0,
                       "bw": int(bw.group(1)) if bw else 0,
                       "hevc": ("hvc1" in codecs or "hev1" in codecs)}
        elif line and not line.startswith("#") and pending:
            pending["url"] = urllib.parse.urljoin(final, line)
            rows.append(pending)
            pending = None
    if not rows:
        return {"url": url, "height": 0, "bw": 0, "hevc": False}
    rows.sort(key=lambda v: (v["h"], v["bw"]), reverse=True)
    avc = [v for v in rows if not v["hevc"]]
    best = rows[0]
    if best["hevc"] and avc:
        return {"url": avc[0]["url"], "height": avc[0]["h"], "bw": avc[0]["bw"], "hevc": False}
    return {"url": best["url"], "height": best["h"], "bw": best["bw"], "hevc": best["hevc"]}


def main():
    groups = {}
    for fn in ("main.json", "hk2.json", "us_imp.json", "uk_imp.json", "ca_imp.json"):
        for g in load(os.path.join(ROOT, fn)):
            groups.setdefault(g["name"], []).extend(g["channels"])

    staged = {}
    for gname, chans in groups.items():
        for c in chans:
            name = clean(c["name"])
            if gname == "Hong Kong":
                name = canon_hk(name)
            elif gname == "US Cable":
                if not US_KEEP.match(name):
                    continue
            elif gname == "UK":
                if not UK_KEEP.match(name):
                    continue
            elif gname == "Canada":
                if not CA_KEEP.match(name):
                    continue
            staged.setdefault(gname, []).append({**c, "name": name, "height": c.get("height") or 0})

    # 0) apply manual overrides - verified picks that beat the harvested copy
    ov_path = os.path.join(ROOT, "overrides.json")
    if os.path.exists(ov_path):
        with open(ov_path, encoding="utf-8") as fh:
            for ov in json.load(fh):
                lst = staged.setdefault(ov["group"], [])
                hit = next((c for c in lst if c["name"].lower() == ov["name"].lower()), None)
                if hit:
                    hit["url"] = ov["url"]
                    hit["_pinned"] = True
                else:
                    lst.append({"name": ov["name"], "url": ov["url"], "badge": "live",
                                "height": 0, "quality": "", "note": ov.get("note", ""),
                                "_pinned": True})
        print("overrides applied", flush=True)

    # 1) resolve codec + real resolution for every candidate
    allch = [(g, c) for g, lst in staged.items() for c in lst]
    print("codec check on %d candidates..." % len(allch), flush=True)
    with cf.ThreadPoolExecutor(max_workers=32) as pool:
        futs = {pool.submit(pick_avc, c["url"], c.get("referer")): (g, c) for g, c in allch}
        for fut in cf.as_completed(futs):
            g, c = futs[fut]
            try:
                res = fut.result()
            except Exception:
                res = None
            if not res:
                c["codec"] = "?"
                c["_bw"] = 0
                continue
            c["url"] = res["url"]
            h, bw = res["height"], res["bw"]
            if h:
                c["height"] = h
                c["badge"] = "%dp" % h
                c["quality"] = ("%dx%d %.2f Mbps" % (h * 16 // 9, h, bw / 1e6)) if bw else ("%dp" % h)
            c["codec"] = "HEVC" if res["hevc"] else "H.264"
            c["_bw"] = bw

    # 2) one entry per (group, name, codec); duplicate URLs dropped
    final = {}
    for gname, chans in staged.items():
        chans.sort(key=lambda c: (bool(c.get("_pinned")), c.get("height") or 0, c.get("_bw") or 0), reverse=True)
        seen, seen_urls, keep = set(), set(), []
        for c in chans:
            key = (c["name"].lower(), c.get("codec"))
            if key in seen or c["url"] in seen_urls:
                continue
            seen.add(key)
            seen_urls.add(c["url"])
            keep.append(c)
        # both codecs for the same channel? tag the HEVC copy so the browser copy stays obvious
        by_name = {}
        for c in keep:
            by_name.setdefault(c["name"].lower(), []).append(c)
        for lst in by_name.values():
            if len(lst) > 1:
                for c in lst:
                    if c.get("codec") == "HEVC":
                        c["name"] = c["name"] + " (HEVC)"
        for c in keep:
            bits = [c.get("badge", "live"), c.get("codec", "?")]
            if c.get("_bw"):
                bits.append("%.1f Mbps" % (c["_bw"] / 1e6))
            c["note"] = SEP.join(bits)
            c.pop("_bw", None)
            c.pop("_pinned", None)
        final[gname] = keep

    order = ["Hong Kong", "US Cable", "Sports", "News", "UK", "Canada"]
    out = {"groups": []}
    for gname in order + [g for g in final if g not in order]:
        lst = final.get(gname) or []
        if not lst:
            continue
        lst.sort(key=lambda c: (-(c.get("height") or 0), c["name"].lower()))
        out["groups"].append({"name": gname, "channels": lst})

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)

    total = sum(len(g["channels"]) for g in out["groups"])
    print("wrote %s - %d channels" % (OUT, total))
    for g in out["groups"]:
        print("  %-12s %3d" % (g["name"], len(g["channels"])))

    # some feeds publish no RESOLUTION attribute at all - read the real size off
    # the video stream with ffprobe and drop anything that turns out to be dead
    try:
        import fix_heights
        fix_heights.fix(OUT)
    except Exception as exc:
        print("height fix skipped: %s" % exc)


if __name__ == "__main__":
    main()
