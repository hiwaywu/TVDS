#!/usr/bin/env python3
"""
audio_scan.py - does each channel actually carry an audio track, and can a browser
decode it?

Some relays publish video-only HLS: TVB's HEVC mount (10.fast.hidns.vip/163189/fct)
serves an .mp4 fMP4 stream with a single HEVC track and no audio at all, so the
picture plays and the sound is simply absent. Others carry AC-3, which hls.js /
Chromium has no decoder for. This script tags both cases in channels.json:

    "audio": "AAC" | "AC-3" | "none" | "?"

  python audio_scan.py                 # scan every channel, write the tags
  python audio_scan.py --drop-silent   # additionally remove channels with no audio
  python audio_scan.py --only "TVB"    # scan a subset by name

Probe order per channel:
  1. ffprobe the playlist directly (works for ordinary .m3u8 URLs)
  2. forced HLS demuxer with -allowed_extensions ALL (index.php style playlists)
  3. download EXT-X-MAP + first segment and ffprobe the fMP4 / MPEG-TS blob
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# browsers decode AAC/MP3/Opus in HLS; AC-3, E-AC-3, MP2 and DTS they do not
BROWSER_OK = {"aac", "mp3", "opus", "vorbis", "flac"}
LABEL = {"aac": "AAC", "mp3": "MP3", "opus": "Opus", "ac3": "AC-3", "eac3": "E-AC-3",
         "mp2": "MP2", "dts": "DTS", "truehd": "TrueHD"}


def ffprobe(args, timeout=25):
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return out.stdout.strip()


def probe_direct(url, referer=None):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
           "-of", "csv=p=0", "-user_agent", UA,
           "-rw_timeout", "12000000", "-probesize", "1500000", "-analyzeduration", "5000000"]
    if referer:
        cmd += ["-headers", "Referer: %s\r\n" % referer]
    cmd += ["-i", url]
    out = ffprobe(cmd)
    return parse(out)


def probe_hls_forced(url):
    cmd = ["ffprobe", "-v", "error", "-f", "hls", "-allowed_extensions", "ALL",
           "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0",
           "-user_agent", UA, "-rw_timeout", "12000000",
           "-probesize", "1500000", "-analyzeduration", "5000000", "-i", url]
    return parse(ffprobe(cmd))


def parse(out):
    if not out:
        return None
    types = {"video", "audio", "subtitle", "data", "attachment"}
    tracks = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",") if p.strip()]
        kind = next((p for p in parts if p in types), None)
        if not kind:
            continue
        codec = next((p for p in parts if p not in types and not p.replace(".", "").isdigit()), "")
        tracks.append((kind, codec))
    if not tracks:
        return None
    audio = [c for t, c in tracks if t == "audio"]
    video = [c for t, c in tracks if t == "video"]
    return audio, video


def _get(u, referer=None):
    req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "*/*"})
    if referer:
        req.add_header("Referer", referer)
    return urllib.request.urlopen(req, timeout=25)


def _resolve_media(url, referer=None, depth=0):
    """master -> best variant -> media playlist; returns (text, base_url)."""
    r = _get(url, referer)
    body = r.read(1 << 20).decode("utf-8", "replace")
    base = r.geturl()
    if "#EXT-X-STREAM-INF" in body and depth < 3:
        lines = [l.strip() for l in body.splitlines()]
        pick = None
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                nxt = lines[i + 1]
                if nxt and not nxt.startswith("#"):
                    pick = nxt
        if pick:
            return _resolve_media(urllib.parse.urljoin(base, pick), referer, depth + 1)
    return body, base


def probe_segment_blob(url, referer=None):
    """fMP4 / TS fallback: fetch EXT-X-MAP + a real media segment, probe the bytes."""
    try:
        body, base = _resolve_media(url, referer)
    except Exception:
        return None
    if "#EXTM3U" not in body:
        return None

    init, segs = None, []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-MAP"):
            m = re.search(r'URI="([^"]+)"', s)
            if m:
                init = urllib.parse.urljoin(base, m.group(1))
        elif s and not s.startswith("#"):
            # AMAGI and friends interleave ad beacons with no file extension; skip them
            path = urllib.parse.urlparse(s).path.lower()
            if "/beacon/" in s or re.search(r"\.(ts|m4s|mp4|jpg|jpeg|aac|m4a|m4v|cmfv|cmfa)$", path):
                segs.append(urllib.parse.urljoin(base, s))
            elif not re.search(r"\.(m3u8|php)(\?|$)", path):
                segs.append(urllib.parse.urljoin(base, s))
    if not segs:
        return None

    blob = b""
    try:
        if init:
            blob += _get(init, referer).read(2 << 20)
        for seg in reversed(segs[-3:]):
            try:
                blob += _get(seg, referer).read(6 << 20)
                break
            except Exception:
                continue
    except Exception:
        return None
    if len(blob) < 4096:
        return None

    fd, tmp = tempfile.mkstemp(suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        for fmt in ("mpegts", "mp4"):
            out = ffprobe(["ffprobe", "-v", "error", "-f", fmt, "-show_entries",
                           "stream=codec_type,codec_name", "-of", "csv=p=0", tmp])
            got = parse(out)
            if got:
                return got
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return None


def probe_audio_group(url, referer=None):
    """Separate #EXT-X-MEDIA audio renditions count as audio (ViuTV ships ch1/2/3)."""
    try:
        r = _get(url, referer)
        body = r.read(1 << 20).decode("utf-8", "replace")
    except Exception:
        return None
    if "TYPE=AUDIO" not in body:
        return None
    m = re.search(r'#EXT-X-STREAM-INF:[^\n]*CODECS="([^"]+)"', body)
    codecs = (m.group(1) if m else "").lower()
    codec = "aac"
    for cand, label in (("ec-3", "eac3"), ("ac-3", "ac3"), ("ac3", "ac3"),
                        ("mp4a", "aac"), ("opus", "opus")):
        if cand in codecs:
            codec = label
            break
    return ([codec], ["video"])


def classify(url, referer=None):
    for fn in (lambda: probe_direct(url, referer),
               lambda: probe_hls_forced(url),
               lambda: probe_audio_group(url, referer),
               lambda: probe_segment_blob(url, referer)):
        try:
            res = fn()
        except Exception:
            res = None
        if res:
            audio, video = res
            if not video:
                continue
            if not audio:
                return "none", ""
            codec = audio[0].lower()
            return LABEL.get(codec, codec.upper()), ("browser" if codec in BROWSER_OK else "vlc-only")
    return "?", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=os.path.join(ROOT, "channels.json"))
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--only", help="only channels whose name contains this")
    ap.add_argument("--drop-silent", action="store_true",
                    help="remove channels that carry no audio track at all")
    ap.add_argument("--unknown", action="store_true",
                    help="only re-scan channels whose audio is still unknown")
    args = ap.parse_args()

    with open(args.file, encoding="utf-8") as fh:
        data = json.load(fh)

    jobs = []
    for g in data["groups"]:
        for c in g["channels"]:
            if args.only and args.only.lower() not in c["name"].lower():
                continue
            if args.unknown and c.get("audio") not in (None, "?"):
                continue
            jobs.append(c)

    print("probing audio on %d channels..." % len(jobs), flush=True)
    results = {}
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(classify, c["url"], c.get("referer")): c for c in jobs}
        done = 0
        for fut in cf.as_completed(futs):
            c = futs[fut]
            done += 1
            try:
                label, play = fut.result()
            except Exception:
                label, play = "?", ""
            results[id(c)] = (label, play)
            if done % 20 == 0 or label in ("none", "AC-3", "E-AC-3", "MP2"):
                print("  %-30s %-6s %s" % (c["name"][:30], label, play), flush=True)

    dropped = []
    for g in data["groups"]:
        keep = []
        for c in g["channels"]:
            label, play = results.get(id(c), (c.get("audio") or "?", ""))
            c["audio"] = label
            if label == "none" and args.drop_silent:
                dropped.append((g["name"], c["name"]))
                continue
            if label == "none":
                c["note"] = (c.get("note", "") + " | no audio track").strip(" |")
            elif play == "vlc-only" and "VLC" not in (c.get("note") or ""):
                c["note"] = ((c.get("note") or "") + " | " + label + " audio - use VLC").strip(" |")
            if play == "browser" and label not in ("?", "none"):
                c["note"] = (re.sub(r"\s*\| [A-Z0-9-]+ audio - use VLC", "", (c.get("note") or ""))
                             .strip(" |"))
            keep.append(c)
        g["channels"] = keep
    data["groups"] = [g for g in data["groups"] if g["channels"]]

    with open(args.file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)

    silent = [c["name"] for g in data["groups"] for c in g["channels"] if c.get("audio") == "none"]
    vlc = [c["name"] for g in data["groups"] for c in g["channels"] if c.get("audio") in ("AC-3", "E-AC-3", "MP2")]
    unknown = sum(1 for g in data["groups"] for c in g["channels"] if c.get("audio") == "?")
    print("\nnone: %d  vlc-only: %d  unknown: %d  dropped: %d" % (len(silent), len(vlc), unknown, len(dropped)))
    if silent:
        print("  no audio: " + ", ".join(silent))
    if vlc:
        print("  needs VLC: " + ", ".join(vlc))
    if dropped:
        print("  removed: " + ", ".join(n for _, n in dropped))


if __name__ == "__main__":
    main()
