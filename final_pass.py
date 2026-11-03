#!/usr/bin/env python3
"""
final_pass.py - last pass over channels.json:

  1. add the New York local stations that actually verify
  2. add Paramount Network (UFC/CBS carriage) to US Cable
  3. drop every channel that is not English or Chinese

Language comes from the iptv-org catalogue when the channel is listed there,
otherwise from a name heuristic, so nothing Spanish/Turkish/Korean/Arabic/
Portuguese/French/German slips through.

  python final_pass.py
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import get  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "channels.json")
API_CH = "https://iptv-org.github.io/api/channels.json"

KEEP_LANGS = {"eng", "zho"}

# verified 2026-11-02 - these are the only NYC feeds that answered a segment request
NYC_ADD = [
    {"group": "New York", "name": "ABC 7 WABC (New York)",
     "url": "https://d3ip9j8kiqtf8w.cloudfront.net/out/v1/9adf1cf1816e45b2a90e98e6cf436fa8/ADHOC-WABC-02-cmaf-manifest/ADHOC-WABC-02-index.m3u8",
     "badge": "1080p", "height": 1080, "quality": "1920x1080", "codec": "H.264",
     "note": "1080p | H.264 | ABC 7 New York live O&O"},
    {"group": "New York", "name": "CBS 2 New York (live)",
     "url": "https://newyo9h67ea3.airspace-cdn.cbsivideo.com/live/manifest.m3u8",
     "badge": "720p", "height": 720, "quality": "1280x720", "codec": "H.264",
     "note": "720p | H.264 | WCBS-TV live O&O"},
    {"group": "New York", "name": "Fox 5 WNYW (New York)",
     "url": "https://cdn-uw2-prod.tsv2.amagi.tv/linear/amg00488-foxdigital-wnyw-lgus/playlist.m3u8",
     "badge": "720p", "height": 720, "quality": "1280x720", "codec": "H.264",
     "note": "720p | H.264 | live local Fox O&O"},
    {"group": "New York", "name": "CBS News New York (WCBS 2)",
     "url": "https://jmp2.uk/plu-5dc48170e280c80009a861ab.m3u8",
     "badge": "720p", "height": 720, "quality": "1280x720", "codec": "H.264",
     "note": "720p | H.264 | CBS New York news service (Pluto feed)"},
    {"group": "New York", "name": "News 12 New York",
     "url": "https://jmp2.uk/plu-5fff49cfb5cd4f0007c2b0dc.m3u8",
     "badge": "720p", "height": 720, "quality": "1280x720", "codec": "H.264",
     "note": "720p | H.264 | News 12 New York (Pluto feed)"},
    {"group": "News", "name": "ABC News Live",
     "url": "https://jmp2.uk/plu-6508be683a0d700008c534e4.m3u8",
     "badge": "720p", "height": 720, "quality": "1280x720", "codec": "H.264",
     "note": "720p | H.264 | ABC News Live (Pluto feed, browser safe)"},
    {"group": "US Cable", "name": "Paramount Network",
     "url": "http://23.237.104.106:8080/USA_PARAMOUNT_NETWORK/index.m3u8",
     "badge": "1080p", "height": 1080, "quality": "1920x1080", "codec": "H.264",
     "note": "1080p | H.264 | Paramount carriage"},
]

# obvious non-target languages by name - used when the catalogue has no entry
FOREIGN = re.compile(r"""(
  espa|spanish|telemundo|univision|galavision|tudn|deportes|latino|latin\s+america|
  mexic|argentin|colombia|chile|peru|venez|ecuador|
  turk|haber|\btrt\b|\batv\b|\bshow\s+tv\b|\bstar\s+tv\b|
  korea|\bkbs\b|\bmbc\b|\bsbs\b|arirang|\bjtbc\b|\btvn\b|
  arab|alarabiya|rotana|abu\s+dhabi|dubai|
  portug|brasil|brazil|\bglobo\b|\brecord\b|
  deutsch|german|\bard\b|\bzdf\b|\brtl\b|sat\.1|prosieben|
  francais|french|\btf1\b|france\s*2|\bm6\b|canal\+|
  italia|italian|\brai\b|mediaset|
  hindi|tamil|telugu|urdu|bangla|thai|viet|indonesia|malay|filipino|tagalog|
  russ|\bntv\b|
  polsk|poland|\btvp\b|nederland|dutch|svensk|swedish|norsk|norwegian|dansk|danish|suomi|finnish
)""", re.I | re.X)


# channels whose name looks English but whose feed is not (Hungarian Eurosport etc.)
EXPLICIT_DROP = {"eurosport 2", "eurosport 1"}


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    data = load(PATH)

    # --- 1/2: additions
    added = 0
    for ch in NYC_ADD:
        grp = next((g for g in data["groups"] if g["name"] == ch["group"]), None)
        if grp is None:
            grp = {"name": ch["group"], "channels": []}
            data["groups"].append(grp)
        # same name already present (e.g. the HEVC ABC News Live) -> replace it
        grp["channels"] = [c for c in grp["channels"] if c["name"].lower() != ch["name"].lower()]
        if any(c["url"] == ch["url"] for c in grp["channels"]):
            continue
        grp["channels"].append({k: v for k, v in ch.items() if k != "group"})
        added += 1
    print("added %d channels (NYC + Paramount)" % added)

    # --- 3: language filter
    try:
        catalogue = json.load(get(API_CH))
    except Exception:
        catalogue = []
    langs = {}
    for c in catalogue:
        nm = (c.get("name") or "").strip().lower()
        if nm:
            langs[nm] = set(c.get("languages") or [])

    kept, dropped = [], []
    for g in data["groups"]:
        keep = []
        for c in g["channels"]:
            nm = c["name"].lower()
            nm_clean = re.sub(r"\s*\(.*?\)\s*$", "", nm).strip()
            if nm in EXPLICIT_DROP:
                dropped.append((g["name"], c["name"], "explicit: feed is not English"))
                continue
            l = langs.get(nm) or langs.get(nm_clean)
            if l:
                ok = bool(l & KEEP_LANGS)
                why = ",".join(sorted(l))
            else:
                ok = not FOREIGN.search(nm)
                why = "unknown -> name heuristic"
            if ok:
                keep.append(c)
            else:
                dropped.append((g["name"], c["name"], why))
        g["channels"] = keep
        kept.append((g["name"], len(keep)))

    data["groups"] = [g for g in data["groups"] if g["channels"]]

    order = ["Hong Kong", "New York", "US Cable", "Sports", "News", "UK", "Canada"]
    data["groups"].sort(key=lambda g: (order.index(g["name"]) if g["name"] in order else 99, g["name"]))
    for g in data["groups"]:
        g["channels"].sort(key=lambda c: (-(c.get("height") or 0), c["name"].lower()))

    with open(PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    print("\ndropped %d non-English/non-Chinese channels:" % len(dropped))
    for gname, name, why in dropped:
        print("  %-10s %-32s (%s)" % (gname, name[:32], why))
    print("\nremaining: %d channels" % sum(len(g["channels"]) for g in data["groups"]))
    for g in data["groups"]:
        print("  %-12s %3d" % (g["name"], len(g["channels"])))


if __name__ == "__main__":
    main()
