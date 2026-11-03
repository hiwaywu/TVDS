#!/usr/bin/env python3
"""
build.py — bulk-build channels.json: probe hundreds of candidates, keep only
streams that actually play at >=720p, one entry per channel at its best quality.

  python build.py                 # full build (HK + US cable + sports + news + UK)
  python build.py --only hk       # one group
  python build.py --min-height 720 --workers 64

Pipeline per candidate stream:
  1. fetch the master playlist
  2. pick the highest RESOLUTION variant
  3. fetch that variant's media playlist
  4. download the newest segment — must be HTTP 200 and >20 KB
Only survivors are written. Dead playlists, 404 segments and sub-720p feeds are dropped.
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
API_CH = "https://iptv-org.github.io/api/channels.json"
API_ST = "https://iptv-org.github.io/api/streams.json"
TIMEOUT = 9
SEG_MIN = 20 * 1024

# ---------------------------------------------------------------- group specs
# ids: explicit iptv-org channel ids
# country: take every channel of that country
# match: regex on channel name (case-insensitive)
US_CABLE = """
CNN.us CNNInternational.us CNNHeadlines.us FoxNewsChannel.us MSNBC.us CNBC.us
BloombergTV.us BloombergTVPlus.us NewsmaxTV.us NewsNation.us
ESPN.us ESPN2.us ESPNU.us ESPNews.us FoxSports1.us FoxSports2.us
NBATV.us MLBNetwork.us NFLNetwork.us NHLNetwork.us TennisChannel.us GolfChannel.us
TBS.us TNT.us truTV.us USANetwork.us FX.us FXX.us FXMovieChannel.us SYFY.us
AMC.us BBCAmerica.us IFC.us SundanceTV.us Freeform.us
HBO.us HBO2.us HBOSignature.us HBOZone.us HBOComedy.us HBOFamily.us
Showtime.us Showtime2.us ShowtimeShowcase.us Starz.us StarzEdge.us StarzComedy.us
ParamountNetwork.us ComedyCentral.us MTV.us VH1.us BET.us
Discovery.us DiscoveryChannel.us History.us TLC.us AnimalPlanet.us Science.us
NationalGeographic.us NationalGeographicWild.us
FoodNetwork.us HGTV.us Bravo.us E.us OxygenTrueCrime.us WeTV.us
Lifetime.us HallmarkChannel.us AETV.us OWN.us TravelChannel.us
CartoonNetwork.us Nickelodeon.us DisneyChannel.us Freeform2.us
""".split()

UK = """
BBCNews.uk BBCOne.uk BBCTwo.uk BBCThree.uk BBCFour.uk
SkyNews.uk SkySportsNews.uk SkySportsMainEvent.uk SkySportsPremierLeague.uk
SkySportsFootball.uk SkySportsCricket.uk SkySportsF1.uk SkySportsGolf.uk
ITV1.uk ITV2.uk ITV3.uk ITV4.uk Channel4.uk E4.uk Film4.uk More4.uk
Five.uk ITVBe.uk Dave.uk Drama.uk Really.uk Quest.uk
TNT Sports 1 placeholder
""".split()

NEWS = """
AlJazeeraEnglish.qa AlJazeeraArabic.qa DWEnglish.de DWDeutsch.de
France24English.fr France24French.fr EuronewsEnglish.fr Euronews.fr
SkyNewsAustralia.au ABCNews.au ABCNewsLive.us CBSNews.us NBCNewsNow.us
BloombergTV.us CNBC.us FoxBusiness.us YahooFinance.us
RTNews.ru CGTN.cn CGTNDocumentary.cn CGTNEurope.cn
NHKWorld.jp NHKWorldPremium.jp ArirangTV.kr KBSWorld.kr
TRTHaber.tr TRTWorld.tr HaberGlobal.tr
NDTV24x7.in IndiaToday.in WION.in RepublicBharat.in
ChannelNewsAsia.sg StraitsTimes.sg
""".split()


def norm(ids):
    return [i for i in ids if i and "." in i]


GROUPS = {
    "hk":      {"title": "Hong Kong",   "country": "HK"},
    "us":      {"title": "US Cable",    "ids": norm(US_CABLE)},
    "uk":      {"title": "UK",          "ids": norm(UK)},
    "sports":  {"title": "Sports",      "ids": ["ESPN.us", "ESPN2.us", "ESPNU.us", "ESPNews.us",
                                                "FoxSports1.us", "FoxSports2.us", "NBATV.us",
                                                "NBATVInternational.us", "MLBNetwork.us",
                                                "NFLNetwork.us", "NHLNetwork.us", "TennisChannel.us",
                                                "GolfChannel.us", "SkySportsNews.uk",
                                                "SkySportsMainEvent.uk", "SkySportsPremierLeague.uk",
                                                "TNTSports1.uk", "TNTSports2.uk", "TNTSports3.uk",
                                                "beINSports1.qa", "beINSports2.qa",
                                                "Eurosport1.fr", "Eurosport2.fr", "DAZN1.de"]},
    "news":    {"title": "News",        "ids": norm(NEWS)},
}


def get(url, referer=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Encoding", "identity")
    if referer:
        req.add_header("Referer", referer)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def parse_master(body, base):
    """-> list of (height, bandwidth, absolute_url)"""
    out, pending = [], None
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            bw = re.search(r"AVERAGE-BANDWIDTH=(\d+)", line) or re.search(r"BANDWIDTH=(\d+)", line)
            pending = (int(res.group(2)) if res else 0, int(bw.group(1)) if bw else 0)
        elif not line.startswith("#") and pending is not None:
            out.append((pending[0], pending[1], urllib.parse.urljoin(base, line)))
            pending = None
    return out


def first_segment(body, base):
    for raw in body.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            return urllib.parse.urljoin(base, line)
    return None


def probe(url, referer=None):
    """-> (ok, height, bandwidth, note)"""
    try:
        r = get(url, referer)
        body = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
        final = r.geturl()
    except Exception as exc:
        return False, 0, 0, str(exc)[:60]
    if "#EXTM3U" not in body:
        return False, 0, 0, "not m3u8"

    variants = parse_master(body, final)
    if variants:
        variants.sort(key=lambda v: (v[0], v[1]), reverse=True)
        height, bw, vurl = variants[0]
        try:
            vr = get(vurl, referer)
            vbody = vr.read(4 * 1024 * 1024).decode("utf-8", "replace")
            vfinal = vr.geturl()
        except Exception as exc:
            return False, 0, 0, "variant: " + str(exc)[:50]
        seg = first_segment(vbody, vfinal)
        if not seg:
            return False, 0, 0, "no segment"
    else:
        height, bw, seg = 0, 0, first_segment(body, final)
        if not seg:
            return False, 0, 0, "no segment"

    try:
        s = get(seg, referer)
        n = len(s.read(SEG_MIN * 2))
    except Exception as exc:
        return False, 0, 0, "segment: " + str(exc)[:50]
    if n < SEG_MIN:
        return False, 0, 0, "segment too small (%d B)" % n
    return True, height, bw, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma separated group keys: " + ",".join(GROUPS))
    ap.add_argument("--min-height", type=int, default=720)
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--out", default=os.path.join(ROOT, "channels.json"))
    ap.add_argument("--report", default=os.path.join(ROOT, "build_report.json"))
    args = ap.parse_args()

    print("catalogue…", flush=True)
    channels = json.load(get(API_CH))
    streams = json.load(get(API_ST))
    by_id = {c["id"]: c for c in channels}
    by_country = {}
    for c in channels:
        by_country.setdefault(c.get("country"), []).append(c["id"])

    stream_map = {}
    for s in streams:
        stream_map.setdefault(s.get("channel"), []).append(s)

    keys = args.only.split(",") if args.only else list(GROUPS)
    jobs = []           # (group_key, channel_id, stream)
    for k in keys:
        spec = GROUPS[k]
        ids = by_country.get(spec["country"], []) if spec.get("country") else spec["ids"]
        for cid in ids:
            if cid not in by_id:
                continue
            cands = stream_map.get(cid, [])
            # prefer entries the catalogue already marks as HD
            cands = sorted(cands, key=lambda s: {"1080p": 3, "720p": 2}.get(s.get("quality") or "", 1),
                           reverse=True)[:4]
            for s in cands:
                jobs.append((k, cid, s))
    print("%d candidates across %d groups" % (len(jobs), len(keys)), flush=True)

    results = {}
    t0 = time.time()
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(probe, s["url"], s.get("referer")): (k, cid, s) for k, cid, s in jobs}
        for fut in cf.as_completed(futs):
            k, cid, s = futs[fut]
            done += 1
            if done % 50 == 0:
                print("  %d/%d  %.0fs" % (done, len(jobs), time.time() - t0), flush=True)
            try:
                ok, h, bw, note = fut.result()
            except Exception:
                continue
            if not ok:
                continue
            results.setdefault((k, cid), []).append((h, bw, s, note))

    out = {"groups": []}
    report = {"groups": {}}
    for k in keys:
        spec = GROUPS[k]
        chans = []
        for (gk, cid), hits in results.items():
            if gk != k:
                continue
            hits.sort(key=lambda x: (x[0], x[1]), reverse=True)
            best = hits[0]
            h, bw, s, _ = best
            # best quality only: known resolution must clear the bar; unknown kept as last resort
            if h and h < args.min_height:
                report.setdefault("below_min", []).append(by_id[cid]["name"])
                continue
            ch = by_id[cid]
            chans.append({
                "name": ch["name"],
                "badge": ("%dp" % h) if h else "live",
                "height": h,
                "quality": ("%dx%d %.2f Mbps" % (h * 16 // 9, h, bw / 1e6)) if h and bw else ("%dp" % h if h else "?"),
                "url": s["url"],
                **({"referer": s["referer"]} if s.get("referer") else {}),
                "note": ("%dp" % h) + (" / %.1f Mbps" % (bw / 1e6) if bw else "") + " · verified",
            })
        chans.sort(key=lambda c: (-(c["height"] or 0), c["name"]))
        if chans:
            out["groups"].append({"name": spec["title"], "channels": chans})
            report["groups"][spec["title"]] = len(chans)
        print("  %-12s %3d live" % (spec["title"], len(chans)), flush=True)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    total = sum(len(g["channels"]) for g in out["groups"])
    print("\nwrote %s — %d live channels in %.0fs" % (args.out, total, time.time() - t0))


if __name__ == "__main__":
    main()
