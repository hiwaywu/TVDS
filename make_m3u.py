#!/usr/bin/env python3
"""
make_m3u.py - export channels.json as a standard IPTV playlist.

Works in TiviMate, OTT Navigator, IPTV Smarters, VLC, Kodi, and in tvapp's own
"Import playlist" flow.

  python make_m3u.py                 -> tvds.m3u
  python make_m3u.py --out x.m3u
"""

import argparse
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))


def tvg_id(name):
    s = re.sub(r"[^A-Za-z0-9]+", "", name)[:24]
    return s or "ch"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", default=os.path.join(ROOT, "channels.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "tvds.m3u"))
    args = ap.parse_args()

    with open(args.channels, encoding="utf-8") as fh:
        data = json.load(fh)

    lines = ['#EXTM3U']
    count = 0
    for group in data["groups"]:
        for ch in group["channels"]:
            name = ch["name"]
            logo = ch.get("logo", "")
            quality = ch.get("badge", "")
            note = ch.get("note", "")
            attrs = [
                'tvg-id="%s"' % tvg_id(name),
                'tvg-name="%s"' % name.replace('"', ""),
                'group-title="%s"' % group["name"],
            ]
            if logo:
                attrs.append('tvg-logo="%s"' % logo)
            if ch.get("referer"):
                # custom header hint; players that support it will use it
                attrs.append('http-referrer="%s"' % ch["referer"])
            lines.append("#EXTINF:-1 %s,%s" % (" ".join(attrs), name))
            if note:
                lines.append("#EXTGRP:%s" % group["name"])
            lines.append(ch["url"])
            count += 1

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print("wrote %s - %d channels, %.1f KB" % (
        args.out, count, os.path.getsize(args.out) / 1024))


if __name__ == "__main__":
    main()
