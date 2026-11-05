# TVDS

Live TV, sourced and verified by script, served locally, and shipped as an Android app.

141 channels â€” Hong Kong / Cantonese, New York locals, US cable, sports, news, UK, Canada â€”
every one checked with a segment download before it earned a slot. The lineup is an
**allowlist of the big networks people actually watch**: CNN, Fox News, MSNBC, the ESPN and
Fox Sports families, HBO/Showtime/Starz, Turner, Discovery, NBCU/Paramount cable, the BBC,
ITV, Sky, CTV/CBC/CityNews, TVB Jade / Pearl / News, ViuTV, HOY, RTHK. Radio, shopping,
religious and "nobody watches this" feeds are filtered out by construction, not by hand.

A local proxy handles the CORS and Referer problems that make public relays unplayable in a
browser. The Android app is a 0.15 MB signed APK built with the SDK tools alone (no Gradle,
no Android Studio).

**Full write-up, including every bug and its fix: [SOP.md](SOP.md).**

---

## Full build log

[SOP_TVDS_Live_TV_Pipeline.md](SOP_TVDS_Live_TV_Pipeline.md) is the complete, re-runnable write-up:
sourcing rules, the verification gate, audio/bitrate measurement, the proxy and its latency
numbers, the player, the guide matching rules and their failure, packaging, publishing, and
every bug that cost a rebuild.

## Windows PC app

`release\tvapp.exe` â€” one file, 12 MB, **no Python and no browser tab**. Double-click it and a
chromeless app window opens on the channel list; close the window and the local proxy exits
with it.

```powershell
.\release\tvapp.exe                       # app window in Chrome (falls back to Edge/Firefox)
.\release\tvapp.exe --browser chrome      # pin the browser; remembered for next launch
.\release\tvapp.exe --browser edge        # Brave / Vivaldi / Firefox also accepted
.\release\tvapp.exe --browser default     # hand the URL to the system default browser
.\release\tvapp.exe --port 9000           # different port
```

Browser order for `auto` (the default): **Chrome â†’ Brave â†’ Vivaldi â†’ Edge â†’ Firefox**. Chromium
browsers get a real `--app=` window with a throwaway profile (so closing it ends the proxy);
Firefox has no app mode, so it opens `-kiosk` with its own profile (`--windowed` for a normal
window instead). If the requested browser is missing, the exe silently uses the next one
installed rather than dropping you into a browser tab.

First launch may raise *Windows protected your PC* (unsigned binary): **More info â†’ Run anyway**.

Rebuild it after changing the channel list:

```powershell
cd pc
powershell -ExecutionPolicy Bypass -File .\build-pc.ps1     # -> release\tvapp.exe
```

### No sound on one channel?

Nothing is wrong with the player â€” the metadata bar now tells you which case you hit:

| Pill | Meaning | What to do |
|---|---|---|
| `AAC` | normal, browser decodes it | nothing |
| `AC-3 Â· VLC` / `MP2 Â· VLC` (orange) | the feed's audio is Dolby / MPEG-1 audio, which Chromium has no decoder for | press **VLC** |
| `no audio` (red) | the relay publishes a **video-only** stream | pick another feed |

The audio codec of every channel is measured with ffprobe and stored as `"audio"` in
`channels.json` (`python audio_scan.py` refreshes it). Re-check with:

```powershell
python audio_scan.py --workers 10          # tag every channel
python audio_scan.py --unknown             # retry the ones that timed out
python audio_scan.py --drop-silent         # additionally remove video-only feeds
```

## Desktop (browser tab, source checkout)

```powershell
# one click â€” prefers release\tvapp.exe when it exists
tvapp.bat
# or
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Open <http://127.0.0.1:8777>. Keys: `â†‘`/`â†“` channel, `f` fullscreen, `m` mute, `r` reload,
`a` toggle MAX/AUTO quality, `c` cycle captions (off â†’ track 1 â†’ track 2 â†’ off). The **VLC**
button hands a stream to VLC for HEVC / AC-3 feeds.

Captions: off by default. The **CC** button in the bar (or `c`) turns the feed's WebVTT
renditions on; `video::cue` pins them at 20px so a broadcaster's own styling cannot blow them
up to fill the frame. `?cc=0` in the URL forces the first track on for a given channel.

## TV guide

**Guide** in the bar (or `g`) opens a channel-grid EPG: channel column pinned on the left,
a 4-hour time ruler across the top, programme blocks on each row with the current one
highlighted. Click any block to tune that channel; **Now** scrolls back to the present. The
playing channel's current programme also shows in the bottom bar.

Listings come from public XMLTV dumps (epg.pw, one file per country: HK / CA / US / GB).
`epg.py` streams them with `iterparse`, keeps a 36-hour window and writes compact JSON
indexes; the server refreshes them in the background once a day and serves `/api/epg`.

```powershell
python epg.py --refresh                 # rebuild the indexes now (~220 MB download, 14 s)
python epg.py --query "CNN"             # what the index says for a channel
```

Coverage is **126 of 147** channels: Hong Kong 9/12, New York 3/5, US Cable 73/80,
Sports 17/17, News 9/14, UK 6/9, Canada 9/10. Relay-only feeds and FAST channels with no
published schedule show `â€”` â€” the guide never invents a programme.

Requirements: Python 3.8+ on PATH, and a browser with hls.js support (any Chromium/Firefox).
VLC is optional.

## Android

```powershell
adb install -r release\tvapp.apk
```

Or copy `release\tvapp.apk` to the phone and tap it (allow *Install unknown apps*).
Android 5.0+, 0.15 MB, package `com.tvapp.player`.

Deep link straight into a stream:

```powershell
adb shell am start -n com.tvapp.player/.MainActivity --es play "https://.../index.m3u8"
```

Rebuild the APK after changing the channel list (it embeds `channels.json`):

```powershell
cd android
powershell -ExecutionPolicy Bypass -File .\build-apk.ps1     # needs JDK 21 + Android SDK
copy .\tvapp.apk ..\release\tvapp.apk -Force
```

## Updating the channel list â€” one command

Relays rot and channels get added, so the whole catalogue is rebuilt from the allowlist in
`expand.py`:

```powershell
python expand.py --workers 64        # probe every candidate, keep the best copy per network
python fix_heights.py                # ffprobe the feeds that never declare a resolution
python make_m3u.py                   # regenerate tvds.m3u for TiviMate / VLC / Kodi
```

`expand.py` pulls the iptv-org country playlists (us / uk / ca / hk / zho / cn), the iptv-org
API, the [hk-iptv](https://github.com/ineed2underfit/hk-iptv) Cantonese playlist and the
current `channels.json`, matches every entry against the allowlist, probes each feed
(master playlist â†’ variant â†’ real segment â‰¥ 20 KB), and writes one entry per network.

Useful flags:

```powershell
python expand.py --dry-run --show     # report only, list what would ship
python expand.py --explain "MSNBC"    # why a channel was picked or dropped, feed by feed
python expand.py --min-height 720 --fallback-height 432
python expand.py --keep-existing      # keep an already-listed feed this run could not reach
```

Adding a channel is one `T(...)` line in the allowlist at the top of `expand.py`, then a
re-run. Results are cached in `.cache/probe_cache.json`, so a second run takes seconds.

## Layout

| Path | What |
|---|---|
| `server.py` | proxy (playlist rewriting, CORS, Referer, 192 MB segment cache + read-ahead), `/api/*`, `/open` â†’ VLC |
| `index.html` | desktop UI â€” hls.js, MAX quality lock, latency HUD |
| `channels.json` | the 142-channel catalogue |
| `expand.py` | **the current pipeline**: allowlist â†’ probe â†’ best copy per network |
| `audio_scan.py` | ffprobe every channel for its audio track (`AAC` / `AC-3` / `none`) |
| `build.py`, `import_m3u.py`, `finalize.py`, `fix_heights.py`, `final_pass.py` | first-generation harvest pipeline; still used for `fix_heights` and for importing any single external playlist |
| `overrides.json` | hand-picked streams that beat the automatic pick |
| `tvds.m3u` | the catalogue as a standard playlist |
| `pc/` | Windows app source (`app.py`) + `build-pc.ps1` |
| `release/tvapp.exe` | the PC app â€” one file, no Python needed |
| `android/` | manifest, `MainActivity`, mobile UI, `build-apk.ps1` |
| `release/tvapp.apk` | signed Android build |

## Sources

`iptv-org` catalogue APIs and country playlists, the hk-iptv Cantonese playlist, the Pluto TV
channel API, station pages (`abc7ny.com/watch`, `cbsnews.com/newyork/live`), broadcaster HLS
endpoints (BBC, Sky, PBS, NHL, Tennis Channel, CBC/Corus) and the public relays that
`expand.py` re-verifies on every run. Nothing here re-hosts a stream â€” the player points at
whatever the broadcaster or relay already serves.

No affiliation with any broadcaster. For personal use.

