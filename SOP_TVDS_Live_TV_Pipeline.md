# SOP — Building a live TV app from public HLS relays (147 verified → 123 playable)

> **Scope of this document.** Everything that was done, in order: what "a working channel" actually
> means, how the catalogue is sourced and verified, why most of the obvious candidates are junk, how
> audio and bitrate were measured instead of guessed, how the proxy and the player were built to keep
> latency down, how the TV guide is matched (and why fuzzy matching produced a Hong Kong schedule on
> HBO Family), how the thing was packaged into a double-click Windows app and an APK, and every bug
> that cost a rebuild along the way. Written so it can be re-run from scratch on another machine.
>
> **Target.** Public HLS feeds for US / UK / Canada / Hong Kong mainstream channels. No credentials,
> no TVE login, no scraping of subscription portals.
> **Outcome.** 123 channels that a browser can actually render (Hong Kong 11 · New York 3 · US Cable 66 ·
> Sports 14 · News 11 · UK 9 · Canada 9), 91 of them with TV-guide listings, shipped as a single-file
> Windows app and an Android APK. Ten further feeds are kept but marked VLC-only (HEVC / AC-3).
>
> **Read §19 first if you are debugging a black screen** — that chapter is the difference between
> "the URL answers" and "the browser can play it", and it is where a fifth of the catalogue used to
> fail silently.
> **Repo.** `https://github.com/hiwaywu/TVDS`

---

## 0. One-page timeline

| # | Step | Result |
|---|---|---|
| 1 | Start from `iptv-org` API + country playlists | 832 raw feeds for 257 wanted channels |
| 2 | Probe master playlist → variant → real segment ≥20 KB → `ffprobe` | 147 verifiable feeds; the rest were dead or fake |
| 3 | Rank candidates per network `(avc, brand, trusted host, height, bandwidth)` | one canonical URL per channel |
| 4 | Reject non-English path tags (`film4_hun`, `euronews-pl`) | HK/UK lists stop filling with foreign dubs |
| 5 | Measure the audio codec per channel | 19 are AC-3/E-AC-3/MP2 → silent in a browser, fine in VLC |
| 6 | Measure real bitrate from segment bytes ÷ `EXTINF` | uniform `1080p | H.264 | 4.4 Mbps | verified` |
| 7 | Resolve the 6 channels with no readable resolution | blob probe via `EXT-X-MAP` + first segment |
| 8 | Build the local proxy (CORS, Referer, playlist rewriting) | browser plays what a raw `fetch` cannot |
| 9 | Add keep-alive pooling + prefetch | ESPN 1304 ms → **410 ms**, BBC 182 → 92, CBC 142 → 90 |
| 10 | Build the player (hls.js, 1-segment live sync, 3 buffer modes) | latency floor = 2 × segment duration |
| 11 | Add the TV guide from `epg.pw` XMLTV | 126 matches — 25 of them **wrong** |
| 12 | Replace fuzzy matching with strict country-scoped matching | 101 matches, all correct |
| 13 | Add per-channel guide offset after CMT ran 6 h behind its listing | CMT aligned at `+360 min` |
| 14 | Package with PyInstaller onefile + external assets | 12 MB exe, hot-swappable catalogue |
| 15 | Kill 9.1 GB of leftover browser profiles per launch | scratch wiped on start and exit |
| 16 | Publish `v1.2.0` with both binaries | release page reflects the real build |

---

## 1. Prerequisites & toolkit

| Tool | Version used | Purpose |
|---|---|---|
| Windows | 11, PowerShell 7 | Everything below |
| Python | 3.13 | `expand.py`, `audio_scan.py`, `bitrate_scan.py`, `epg.py`, `recorder.py` |
| `ffprobe` / `ffmpeg` | 6.x on PATH | Real codec/resolution check; frame grabs for verifying a channel's identity |
| `curl.exe` | shipped with Windows | Fast status/size probes without a Python import |
| Node.js | 24 | `node --check` on the extracted page script before shipping any UI edit |
| PyInstaller | 6.x in `pc/` venv | Windows onefile build |
| Android SDK | build-tools 36, platform 35 | `aapt2`, `javac`, `d8`, `apksigner` for the APK |
| JDK | 21 | APK build |
| `gh` CLI | logged in as `hiwaywu` | Releases and pushes |

Machine-specific gotchas, collected so they are not rediscovered:

- **PowerShell heredocs do not exist.** `python - <<'EOF'` is a parse error. Write a `.py` file with
  the `write` tool, or use `python -c "..."` with single-quoted outer strings.
- **`Set-Content` round-trips mangle UTF-8.** Reading a file with `Get-Content -Raw` and writing it
  back turned `·` into `·` and `–` into `–` inside `index.html`. Rule adopted: **only** rewrite
  text files through Python with `io.open(..., encoding='utf-8', newline='')`.
- **`SO_REUSEADDR` on Windows lets two servers bind the same port.** A stale `pythonw server.py` and
  the new exe both answered on 8777, and the window showed the *old* catalogue. Fix: probe with a
  real `connect()` (`port_in_use`) and construct the server with `allow_reuse_address = False`.
- **Killing the PyInstaller parent leaves the child alive.** `Stop-Process` on the bootloader PID
  leaves a windowless server holding the port; every later launch then "hands off" to it. Kill by
  process name, and never write the port file from a test-mode instance.
- **Screenshots of windows behind other windows capture occlusion.** Use `PrintWindow` with
  `PW_RENDERFULLCONTENT`, or drive headless Edge with `--screenshot` and a `?ch=` deep link.

---

## 2. Phase 1 — What counts as "a working channel"

A 200 on the playlist URL means nothing. The gate that was adopted, run for every candidate:

```
master playlist (200, is m3u8)
  → pick a variant (RESOLUTION / AVERAGE-BANDWIDTH, highest first)
  → fetch that media playlist
  → fetch the LAST segment, require >= 20 KB
  → ffprobe it for real width/height/codec
```

Anything that fails a step is dropped, not "kept just in case". Two consequences worth stating:

- The catalogue only ever lists channels that answered a **segment** request, minutes before the
  build. It is a snapshot, and `audit.py` re-checks it later (§11).
- Feeds that pass the gate can still die between runs — one whole relay box
  (`198.58.104.90:8989`) was serving five channels in the morning and was unreachable by the
  afternoon, so those five plus a hard-404 ESPN News were pruned (147 → 141).

---

## 3. Phase 2 — Sourcing, and why most sources are junk

`iptv-org` is an **index**, not a host. Two things are used from it:

| Source | What it gives |
|---|---|
| `https://iptv-org.github.io/api/streams.json` | every known stream URL, per channel id |
| `streams/us.m3u`, `uk.m3u`, `ca.m3u`, `hk.m3u`, `zho.m3u` | the same, grouped by country |

Plus `hk-iptv` (a Cantonese-focused community playlist) and a hand-curated list of relay mounts.

Measured provenance of the final catalogue (147 at the time):

```
final URLs listed by iptv-org : 108 / 147
only in our own sources       :  39
```

The 39 are the Cantonese feeds (`php.jdshipin.com`, `fast.hidns.vip`, `r.jdshipin.com`) and a set of
relay tokens found by probing — iptv-org's HK list only carried Jade, HOY and RTHK.

**Host concentration is the real risk.** Of the 80 US cable rows, 48 sat on two boxes:

```
23.237.104.106:8080   28 channels
4.30.180.36:8420      20
```

If those two go dark, 60 % of US cable disappears. That is a property of the sources, not of the app,
and the only mitigation is that a re-run repopulates from whatever is alive.

### 3.1 Language filtering

Cantonese/Chinese lists are full of foreign dubs: `film4_hun`, `euronews-pl`, `discovery_tur`. The
filter rejects a path segment that matches a language code — **with an exclusion list**, because
`est_2.m3u8` is PBS *Eastern*, not Estonian. Without the exclusion, PBS vanished from the US list.

---

## 4. Phase 3 — The allowlist pipeline (`expand.py`)

Everything is driven by a target table, not by "whatever the playlist says":

```python
T(group, name, aliases, extra=(), regex=(), min_h=0)
```

- `aliases` — names the network appears under (call letters, rebrands, `BBC News HD` vs `BBC News`)
- `extra` — URLs to probe directly, for feeds that only exist as a relay token
- `regex` — narrows a noisy alias (e.g. only `ESPN2`, not `ESPN2 College`)
- `min_h` — per-channel height floor

Pipeline:

1. Load the existing `channels.json` (so a rebuild never loses what already worked).
2. Collect candidates from every source for each target.
3. Probe with a **cache** (`.cache/probe_cache.json`) so a re-run costs seconds, and `--explain NAME`
   prints why a candidate won or lost.
4. Rank surviving candidates per target:
   `(avc first, brand hint, trusted host, height, bandwidth)` — H.264 beats HEVC because a browser
   cannot decode HEVC in HLS here; a URL that mentions the network beats an anonymous relay path.
5. Emit `channels.json` + `tvds.m3u`.

Two flags that matter operationally: `--dry-run` and `--keep-existing`. Without `--keep-existing` a
transient probe failure silently drops a channel; with it, an existing row survives a failed re-probe.

### 4.1 The bug that made every rebuild lossy

`expand.py` rewrote `channels.json` from scratch, discarding `audio` and `measured_mbps`. After one
rebuild, HOY TV 77 regressed from `1080p` to a bare `live` badge because its measured fields were
gone. Fixed: the rebuild now carries those fields forward from the previous catalogue, matched by
`(group, name)` with a name-only fallback.

---

## 5. Phase 4 — Audio: why a channel is silent in the browser

Chinese/HK feeds and several US cable relays publish **AC-3**, **E-AC-3** or **MP2**. Browsers decode
**AAC, MP3, Opus**; they do not decode AC-3. The result looks like a broken stream.

`audio_scan.py` probes in escalating order, because a plain `ffprobe` on a relay URL often returns
nothing:

1. direct `ffprobe` of the stream URL
2. `ffprobe -f hls -allowed_extensions ALL`
3. read the master's `#EXT-X-MEDIA:TYPE=AUDIO` group
4. concatenate `#EXT-X-MAP` init + first segment into a temp blob, probe as `mpegts`, then as `mp4`

Result is stored per channel as `audio: AAC | AC-3 | E-AC-3 | MP2 | ?` and surfaced in the UI as a
pill. When the codec is not browser-safe the row hands off to **VLC** instead of pretending.

Two bugs cost a full re-run each:

- The CSV parser assumed `codec_name` came first; `ffprobe` emits `h264,video`. Fixed with an
  order-agnostic parse — before that, all 142 channels reported `?`.
- `--unknown` used `results.get(name, '?')`, which **overwrote** tags on channels that were already
  measured. Fixed to preserve the existing tag.

---

## 6. Phase 5 — Bitrate and resolution, measured rather than trusted

`RESOLUTION` in a master playlist is a claim. Two measurements make the rows honest:

- **Bitrate** (`bitrate_scan.py`): sum of the actual segment bytes ÷ sum of `EXTINF` durations, over
  several segments. Falls back to `AVERAGE-BANDWIDTH` when the media playlist is unreadable.
- **Resolution** (`fix_heights.py`): `ffprobe` the variant; when the variant is a `.php`/`.jpg`-named
  fMP4 mount (very common on the relay boxes), build a blob from `#EXT-X-MAP` + first segment and read
  the real `width×height` from that.

Every row ends up with the same shape:

```
1080p | H.264 | 4.4 Mbps | verified
```

Uniformity matters for the user: mixed formats ("live", "HD", "1080p") read as sloppiness, and
`live` was briefly used as a fallback for a *missing* height — which is exactly the sort of lie the
pipeline is supposed to avoid. 146 of 147 rows matched the pattern; the last one (a 360p weather feed
whose probe failed) was pinned explicitly to `360p` rather than left as `live`.

---

## 7. Phase 6 — The proxy (`server.py`)

The browser cannot fetch these streams directly: no CORS headers, some hosts require a `Referer`,
and playlists reference **relative** segment paths that must keep pointing at the right host.

What the proxy does:

- `GET /proxy?u=<url>&r=<referer>` — fetch upstream with a UA, optional Referer, follow redirects
  **manually** (so relative redirects resolve and the final URL is known)
- rewrite every URI inside an m3u8 (segments, keys, `EXT-X-MAP`) into `/proxy?u=…`
- stream segments through with a bounded in-memory LRU (192 MB, ~40 channels' worth)
- CORS headers on everything, so hls.js in a page served from `127.0.0.1` can use it

### 7.1 The latency work that actually paid

The first version opened a **new TCP + TLS connection per segment**. HLS is sequential, so a
handshake sat in front of every 6 s chunk. The fix: a pool of idle connections keyed by
`(scheme, host, port)`, returned only after the body is drained.

| Feed | Before | After |
|---|---|---|
| ESPN (slow TLS host) | 1304 ms | **410 ms** |
| BBC News | 182 ms | **92 ms** |
| CBC News Network | 142 ms | **90 ms** |
| CNN | 169 ms | 139 ms |
| HOY TV 77 | 164 ms | 142 ms |
| Channel 5 | 185 ms | 142 ms |

Plus a prefetch queue (depth 4, 4 persistent workers) so the next segments are already local when the
player asks.

### 7.2 Endpoints

| Path | Purpose |
|---|---|
| `/api/channels` | the catalogue (prefers the copy next to the exe) |
| `/api/epg` | guide payload, `?full=1` adds the programme list for the grid |
| `/api/guide-shift` | set a per-channel guide offset (`min=-30`, `off`, `clear`) |
| `/rec/live`, `/rec/status`, `/rec/<key>/<file>` | local recorder (see §11) |
| `/probe`, `/open` | single-channel health check; hand a stream to VLC |

### 7.3 Inside the probe, the cache and the pool

`expand.probe_stream(url, referer, min_height)` is the single source of truth for "does this work".
It returns a dict, and every caller depends on those exact keys:

```python
{"ok": True|False, "h": 1080, "bw": 4_400_000, "codec": "avc",
 "final": "https://…", "err": ""}
```

Sequence inside it:

1. `fetch(url)` with a desktop UA and optional `Referer`; follow redirects **manually** so `final` is
   the URL that actually served the bytes.
2. If the body has no `#EXTINF`, it is a master: parse `#EXT-X-STREAM-INF` lines, keep
   `RESOLUTION`/`AVERAGE-BANDWIDTH`, resolve relative URIs against `final`, pick the tallest variant
   that satisfies `min_height` (fall back to the tallest available).
3. Take the **last** segment listed (most likely to still exist) and fetch it. Fewer than 20 KB is a
   failure — that catches placeholder playlists and error pages that return `200`.
4. `ffprobe` the segment for `codec_name`/`width×height`, which is the only trustworthy source for
   both fields.

Failures are cached too, with a short TTL, so a re-run does not re-punish a dead host 40 times:

```json
{"ok": false, "err": "segment: HTTP Error 404: Not Found"}
```

The cache is keyed by URL only (not by `min_height`) because the probe fetches the tallest variant
regardless; the height filter is applied by the caller. That is why `--explain NAME` can say "cache
says dead" and `--no-cache` exists for the case where a box came back.

The HTTP pool:

| Setting | Value | Why |
|---|---|---|
| `_POOL_MAX_IDLE` | 6 | one socket per host in flight is enough; more just holds dead peers |
| idle timeout | implicit (closed on error) | relays drop connections aggressively |
| prefetch depth | 4 segments | covers a 6 s-segment playlist's next ~24 s |
| prefetch workers | 4 | more workers starve the player's own request |
| segment cache | 192 MB LRU | ~40 channels' worth; `X-Cache: HIT` on a rewind |

`/api/stats` reports `cache_items`, `cache_mb`, `hits`, `misses`, `prefetched` — the numbers used in
§7.1 were read from it, not guessed.


---

## 8. Phase 7 — The player

hls.js with latency as the design constraint:

```js
{ lowLatencyMode: true, liveSyncDurationCount: 1, maxLiveSyncPlaybackRate: 1.5,
  maxBufferLength: 6, backBufferLength: 4 }
```

**The floor is 2 × segment duration.** A live HLS player must hold the segment *after* the one on
screen; with 6 s segments that is ~12 s behind wall clock, with 10 s segments ~20 s. LL-HLS
(`EXT-X-PART`) would break that, and none of these relays publish it. Measured on this catalogue:

```
window (history retained)   channels
  30 min                    DW English, CityNews Vancouver / Edmonton / Calgary
  10 min                    Adult Swim
   5 min                    PBS Kids
  ~2 min                    GB News, NBC News NOW, Cheddar, TalkTV
   ~60 s                    BBC News, CNN Intl, CBS News, Weather Channel, …
   ~12 s                    HBO Family, AMC, A&E, TLC, USA Network, …
median 33 s
```

Three buffer modes exist because the user's question ("if I pause, does it keep downloading?") has a
real answer:

| Mode | Position | Buffer | Pausing buys |
|---|---|---|---|
| Live | 1 segment behind | 6 s | almost nothing |
| Smooth | ~30 s behind | 30 s | instant resume |
| Deep | ~30 s behind | 5 min | keeps pulling while paused, up to 5 min |

Deep cannot exceed the feed's retained window — that is the whole reason §11 exists.

Other behaviour that had to be fixed because it looked like a player bug:

- **Letterboxing vs stretching.** `object-fit: contain` plus shrink-wrapping the element to the
  stream's aspect ratio from `loadedmetadata`; without the shrink-wrap, native captions land in the
  black bar instead of on the picture.
- **Captions.** Feeds ship WebVTT; hls.js renders them by default with no control. `CC` cycles
  off → track 1 → track 2, and `video::cue` pins the size (20 px, 30 px fullscreen) so a
  broadcaster's styling cannot fill the frame. `?cc=0` forces a track for testing.
- **Fullscreen.** The browser's own video-fullscreen path ignores page layout and stretches non-16:9
  feeds; the handler redirects it to the stage element.

### 8.1 The hls.js events that matter

| Event | What is done with it |
|---|---|
| `MANIFEST_PARSED` | start playback, fill the resolution/bitrate pills, pin MAX quality, call `applyCC()` |
| `LEVEL_SWITCHED` | re-pin the highest level when the user is in MAX mode (hls.js still tries to downshift) |
| `LEVEL_UPDATED` | measure the real window for the DVR button: `lastFrag.end - firstFrag.start` |
| `SUBTITLE_TRACKS_UPDATED` | refresh the CC button so it enables only when tracks exist |
| `ERROR` | 3 automatic retries with backoff, then an overlay that names the failing detail |

Quality pinning is deliberate: ABR on a relay that stalls will happily drop you to 480p and stay
there. MAX forces the top variant and re-asserts it on every level change; `a` (or the button) flips
to AUTO for a channel whose link cannot carry the top profile.

### 8.2 UI geometry, because it is all hand-built

The guide is not a component library — it is arithmetic over a time window:

```js
EPG_WIN_MIN   = 240      // 4 hours visible
EPG_PX_PER_MIN = 5       // 1 hour = 300 px
window.start   = floor(now / 1800) * 1800 - 1800   // one 30-min slot of context on the left
left(t)        = (t - start) / 60 * PX_PER_MIN
width(a, b)    = max(26, (b - a) / 60 * PX_PER_MIN - 2)   // 26 px floor keeps the title legible
```

Consequences that had to be handled explicitly:

- the channel column is `position: sticky; left: 0` inside each row, so vertical scrolling is free and
  horizontal scrolling keeps names visible — no JS scroll synchronisation
- the row header is `position: sticky; top: 0`, and needs a higher `z-index` than the column
- a programme that started before the window is clipped at `x = 0` and gets a `‹` prefix plus flat
  left corners, otherwise it looks like a rendering bug
- the "now" line is one absolutely-positioned element inside the scrolling content, at
  `left: calc(var(--chw) + x(now))`; the sticky column covers it when scrolled, which is the correct
  layering without any clipping work
- **the whole row is the click target** — the channel cell, the gaps between blocks and the empty
  track all tune that channel. Only making the blocks clickable meant a channel with no listings
  could not be opened at all.

The player is two screens in one document, switched by a body class, with history state so the
**mouse back button** works:

```js
openPlayer(i)  → body.mode-player, history.pushState({v:'player', ch:i}, '#ch='+i)
openGuide()    → body.mode-guide,  history.pushState({v:'guide'}, '#guide')
popstate       → re-enter whichever state the browser restored
Esc / Backspace→ history.back()
```

Controls auto-hide after 3.2 s of no mouse movement (suppressed while the channel drawer is open, and
under `?ctl=1` for screenshots).


---

## 9. Phase 8 — The TV guide, and how it went wrong

Source: `epg.pw` XMLTV dumps, one file per country. Raw sizes and what they compile to:

| Country | Download | Channels | Programmes kept (36 h window) |
|---|---|---|---|
| HK | 19.7 MB | 237 | 11 241 |
| CA | 23.6 MB | 696 | 29 136 |
| US | 161.5 MB | 5 542 | 287 627 |
| GB | 20.3 MB | 755 | 36 840 |

`epg.py` streams each file with `iterparse`, keeps `[now-6h, now+36h]`, and writes a compact JSON
index (~16 MB total). The server rebuilds stale indexes in the background and serves `/api/epg`.

### 9.1 The matching bug

The first matcher was fuzzy: exact name, then aliases, then **substring containment across all
countries**. Coverage looked great (126/147) and was wrong. `HBO Family` exists in the **Hong Kong**
index and not the US one, so it picked up a Cantonese schedule (麻街 = Sesame Street).

Replaced with:

1. a channel only takes listings from **its own group's country** (US Cable → US, UK → GB, Canada →
   CA, Hong Kong → HK)
2. the guide display name must equal ours, allowing only harmless suffixes (`HD`, `East`, `West`,
   `Canada`, …)
3. explicit pins in `guide_map.json` for the cases where the provider spells it differently
   (`WABC-DT`, `WCBS-DT`, `WNYW-DT`, `ION Television`, `Nick Jr`, `Hallmark Mystery`)
4. **no fallback**: no match means "no listings"

Result: **101/141**, every one the right channel.

### 9.2 Provider clock errors

CMT listed *The King of Queens* / *Cheers* while the feed showed *The Golden Girls*. A burned-in CMT
logo in a captured frame proved the channel was right and the **timing** was wrong — the feed ran
about six hours behind the published schedule. Neither fuzzy matching nor rematching fixes that.

So: `guide_offset.json` + a `⏱ Sync` control in the player, `−30 / +30 / −1 h / +1 h` accumulating per
channel, plus `Hide these listings` and `Reset`. CMT is pinned at `+360 min`.

### 9.3 The index, on disk

`epg.py` compiles each country into one JSON file (`.cache/epg/<CC>.json`, and
`%LOCALAPPDATA%\tvapp\epg` in the packaged app):

```json
{"country": "US", "built": 1793771517,
 "channels":   {"465107": ["CMT HD"], "464745": ["HBO East"]},
 "programmes": {"465107": [[1793754000, 1793768400, "The King of Queens"], …]}}
```

Times are epoch seconds; the provider's `+0000` strings are converted with a UTC parse
(`calendar.timegm`), never a local-time one — a DST boundary would silently shift every listing by an
hour. Programmes are sorted per channel so `now`/`next` are a linear scan. A 36-hour window keeps the
files ~12.5 MB for the US and ~0.6 MB for Hong Kong.

`/api/epg` caches its computed payload for 120 s; `/api/guide-shift` clears that cache so a nudge is
visible on the next poll instead of two minutes later.

### 9.4 Worked example — adding one channel end to end

Adding **American Heroes Channel** (it was missing from the catalogue):

```powershell
# 1. find a candidate URL and check it by hand before touching any code
curl.exe -s -o NUL -w "%{http_code}`n" http://23.237.104.106:8080/USA_AMERICAN_HEROES/index.m3u8
python -c "import sys; sys.path.insert(0,'.'); from expand import probe_stream;
print(probe_stream('http://23.237.104.106:8080/USA_AMERICAN_HEROES/index.m3u8', None, 720))"
# -> {'ok': True, 'h': 1080, 'codec': 'avc', …}

# 2. add the target (extra= is for feeds that exist only as a bare mount)
#    T(US, "American Heroes Channel", ["american heroes channel", "american heroes"],
#      ["http://23.237.104.106:8080/USA_AMERICAN_HEROES/index.m3u8"])

# 3. rebuild the catalogue and tag the new row
python expand.py --workers 48
python audio_scan.py          # its audio came back AAC, so it is browser-safe
python bitrate_scan.py

# 4. publish: copy the catalogue next to the exe — no rebuild needed
Copy-Item channels.json release\channels.json -Force
```

Then `⏱` nothing, `CC` nothing — the guide either has it or shows `no listings`, and `Check` in the UI
(or `audit.py`) is what says whether it still works tomorrow.

### 9.5 Worked example — removing what died

```powershell
python audit.py --workers 20            # prints live / dead / unknown and writes status into channels.json
python audit.py --workers 20 --prune    # deletes the dead rows
python make_m3u.py                      # keep tvds.m3u in step
Copy-Item channels.json release\channels.json -Force
```

`Hide offline` in the guide does the same thing visually without touching the file, and it is the
better default when a relay may come back: a pruned channel still returns on the next full
`expand.py` run, because the allowlist kept the target.


---

## 10. Phase 9 — Packaging (`pc/app.py`, `pc/build-pc.ps1`)

PyInstaller `--onefile --windowed`. The non-obvious parts:

- **`server.py` ships as data, not as a module.** PyInstaller never analyses its imports, so every
  runtime import must be declared: `--hidden-import queue` (the prefetch queue), `http.client`, `io`,
  and later `xml.etree.ElementTree` + `xml.parsers.expat` for the guide. Without `queue` the exe
  started and died silently, with no port and no window.
- **Assets live next to the exe and win over the bundle.** `EXTRA_ROOTS` is consulted before
  `_MEIPASS`, so `channels.json`, `index.html`, `epg.py`, `guide_map.json` are hot-swappable — a
  catalogue refresh is a file copy, not a rebuild. `server.py` and `recorder.py` are *not* in that
  set: they are imported from the bundle, so changing them **does** require a rebuild.
- **One instance.** A port file (`%LOCALAPPDATA%\tvapp\port.txt`) plus a live `/api/channels` probe
  identifies a running app; the second launch restores and focuses the existing window instead of
  starting another server and window. Test mode (`--no-window`) deliberately does **not** write the
  port file — doing so made a double-click hand off to a windowless server.
- **Scratch data is deleted, not accumulated.** Every launch used to leave a `chrome-profile-<pid>`
  directory behind: **9.1 GB** after a couple of days of testing. `purge_scratch()` now runs when the
  app owns the run (after the single-instance check, never before it — otherwise it deletes a live
  window's profile) and again on exit.

---

## 11. Phase 10 — Health checks and the recorder

`audit.py` is the CLI counterpart of the UI's `Check` button: it probes every channel, writes
`status: ok|dead|?` plus `checked_at` into `channels.json`, and can prune. A run over 147 channels
takes ~20 s at 20 workers:

```
live 141   dead 1   unknown 5
  DEAD  Sports  ESPN News   HTTP 404
```

The five "unknown" were timeouts on a box that no longer answered ping — treated as dead and pruned.

`recorder.py` is the attempt to stop depending on the source's retention: it pulls the stream itself,
writes segments to disk, publishes a sliding playlist capped at 30 min / 2.5 GB, and serves them back.
**Verified working as a downloader** — recorded segments probe as `h264,1920x1080 + aac`, 6.7 MB per
10 s — and **not yet usable as a player**, because hls.js loads the synthesized playlist, sees the
fragments, and never appends a frame (buffer stays 0, no error raised). Attempts that did not fix it:

- absolute `/rec/<key>/<file>` URLs instead of relative names
- `.ts`/`.m4s` extensions instead of `.bin` (hls.js picks its demuxer from the extension)
- blocking `/rec/live` until two segments exist (hls.js does not retry an empty playlist)
- `#EXT-X-DISCONTINUITY` before every fragment
- disabling `lowLatencyMode` for this source

Conclusion recorded for the next attempt: passthrough TS segments each carry their own timeline, so
they must be **remuxed into one continuous MP4** server-side rather than handed back byte-for-byte.
The UI button was removed instead of shipping a control that does nothing.

---

## 12. Phase 11 — The Android build

`android/build-apk.ps1` does it without Gradle: `aapt2 link` → `javac` → `d8` → add `classes.dex` →
`apksigner`. Assets are three files copied into `assets/`: `channels.json`, `hls.min.js`,
`index.html`.

The shape of the app, because it constrains what can be fixed in it:

| Piece | Detail |
|---|---|
| `MainActivity.java` | one full-screen `WebView`; hardware acceleration on; keeps the screen awake during playback |
| `Bridge.java` | `@JavascriptInterface` surface — what the page uses instead of browser-only APIs (open a stream externally, read the build tag) |
| `AndroidManifest.xml` | `INTERNET`, `WAKE_LOCK`, `usesCleartextTraffic=true` — **mandatory**, most of these relays are plain `http://` |
| `assets/index.html` | the whole mobile UI: channel list grouped by category, the same hls.js config, a details sheet |
| signing | a debug keystore generated on first build, `apksigner` with the v1+v2 schemes |

Two traps:

- `android/assets/channels.json` is a **copy**, so rebuilding the APK is required after every
  catalogue change — the shipped APK sat a day stale with 147 channels (including the six dead ones)
  until it was rebuilt.
- The Android UI is a separate, simpler layout. The desktop redesign (guide grid, player screen,
  buffer modes, DVR) is not in it; porting means editing `android/assets/index.html`, no Java changes.

Verification that does not need a device:

```powershell
# what is actually inside the APK you are about to hand over
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory((Resolve-Path release\tvapp.apk), "$env:TEMP\apkcheck")
python -c "import json;d=json.load(open(r'$env:TEMP\apkcheck\assets\channels.json',encoding='utf-8'));print(sum(len(g['channels']) for g in d['groups']))"
```


---

## 13. Phase 12 — Publishing

`gh release upload … --clobber` replaces an asset but **does not move the release's date** — GitHub
kept showing "2 days ago" while the binaries were new. The correct move is a new release:

```powershell
gh release create v1.2.0 release/tvapp.exe release/tvapp.apk `
  --title "TVDS 1.2 — 141 channels, TV guide grid, PC + Android" `
  --notes-file notes.md --target main
gh release delete v1.1.0 --yes --cleanup-tag
```

Binaries are **not** committed to git (12 MB exe, and the APK intermediate): `.gitignore` covers
`release/tvapp.exe`, `android/tvapp.apk`, `pc/tvapp.ico`. The repo carries source, `channels.json`,
`tvds.m3u`, and `release/channels.json` + `release/index.html`.

---

## 14. Appendix A — Recipes

```powershell
# full catalogue refresh (the order matters)
python expand.py --workers 64        # sources -> verification -> ranking
python fix_heights.py                # blob probe for php/jpg fMP4 mounts
python audio_scan.py                 # AAC / AC-3 / MP2 per channel
python bitrate_scan.py               # measured Mbps -> uniform notes
python make_m3u.py                   # tvds.m3u

# health check + prune
python audit.py --workers 20
python audit.py --workers 20 --prune

# guide
python epg.py --refresh               # ~220 MB download, 14 s, 4 countries
python epg.py --query "CNN"

# publish to the running app (no rebuild needed for these)
Copy-Item channels.json  release\channels.json  -Force
Copy-Item index.html     release\index.html     -Force
Copy-Item guide_map.json release\guide_map.json -Force

# rebuild the Windows app (needed after server.py / recorder.py / app.py changes)
cd pc; .\build-pc.ps1

# rebuild the APK (needed after any channels.json change)
Copy-Item release\channels.json android\assets\channels.json -Force
cd android; .\build-apk.ps1; Copy-Item tvapp.apk ..\release\tvapp.apk -Force
```

### A.1 Verify one channel by hand, without the app

```powershell
$u = "http://23.237.104.106:8080/USA_CNN/index.m3u8"

# 1. does the master answer at all
curl.exe -s -o NUL -w "master %{http_code} %{size_download} bytes`n" --max-time 15 $u

# 2. what variants does it offer
curl.exe -s --max-time 15 $u | Select-String -Pattern 'RESOLUTION|EXT-X-STREAM-INF'

# 3. take a real segment (not the first line - the last one is the freshest)
curl.exe -s --max-time 15 $u | Select-Object -Last 3

# 4. is the picture actually decodable
ffprobe -v error -show_entries stream=codec_name,width,height -of csv=p=0 "<variant-or-segment>"

# 5. grab a frame when you need to know *which* channel it really is
ffmpeg -hide_banner -loglevel error -ss 6 -i "$u" -frames:v 1 -y "$env:TEMP\frame.jpg"
```

Step 5 is how the CMT mystery was settled: the frame carried CMT's own bug in the corner, which
proved the channel mapping was right and the *schedule* was wrong.

### A.2 Latency measurement, repeatable

```powershell
# cold vs warm, same segment, through the proxy
$b = "http://127.0.0.1:8777"
$seg = "$b/proxy?u=" + [uri]::EscapeDataString("<segment url>")
1..4 | ForEach-Object {
  $t = Measure-Command { curl.exe -s -o NUL $seg }
  "pull {0}: {1} ms" -f $_, [math]::Round($t.TotalMilliseconds)
}
curl.exe -s "$b/api/stats"
```

The `X-Cache` header on the first `curl.exe -i` tells you whether the pool and the prefetcher are
actually warm; `{"hits": n, "prefetched": m}` from `/api/stats` is the corroboration.

### A.3 Measure a feed's retained history (decides what DVR can do)

```python
import re, urllib.request
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131"}
text = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read().decode()
dur = sum(float(x) for x in re.findall(r"#EXTINF:\s*([0-9.]+)", text))
print(len(re.findall(r"#EXTINF", text)), "segments", dur, "seconds")
```

Median across the 141-channel catalogue: **33 seconds**. That number is why "pause and it keeps
downloading" cannot work past half a minute on most feeds, and why only five channels get a useful
DVR.


---

## 15. Appendix B — Failure signatures, and what they mean

| Symptom | Real cause | Fix |
|---|---|---|
| Playlist returns 200, picture never starts | the "playlist" is a master whose variants are dead | probe a real segment, not the master |
| Channel loads, no sound | AC-3 / E-AC-3 / MP2 audio | VLC handoff; `audio_scan.py` to tag it |
| Picture stretched in fullscreen | native video fullscreen bypasses page CSS | `object-fit: contain` + redirect to stage fullscreen |
| Captions 100 px tall | provider styling scaled to the frame | `video::cue { font-size }` + shrink-wrapped element |
| Guide shows a foreign-language schedule | fuzzy match across countries | strict, country-scoped matching |
| Guide names right shows, times wrong | provider clock offset (CMT ≈ +6 h) | `⏱ Sync` per channel → `guide_offset.json` |
| Two windows, old channel list | two servers on one port (`SO_REUSEADDR`) | `allow_reuse_address = False`, `port_in_use()` connect test |
| exe starts, no window, no port | missing `--hidden-import` for a data-shipped module | add the import, rebuild |
| Double-click opens nothing | port file points at a dead test server | `--no-window` must not write the port file |
| Disk filling up | one browser profile per launch | `purge_scratch()` on start and exit |
| GitHub release looks old after upload | asset replacement ≠ new release | `gh release create` a new tag |

---

## 16. Appendix C — File map

| File | Role |
|---|---|
| `expand.py` | allowlist pipeline: targets, probing, ranking, cache, `--explain` |
| `audit.py` | probe every channel, write `status`, prune |
| `audio_scan.py` | audio codec tagging (4-stage probe) |
| `bitrate_scan.py` | measured bitrate from segment bytes |
| `fix_heights.py` | real resolution, incl. blob probe for exotic mounts |
| `epg.py` | XMLTV fetch/parse, strict matching, offsets, `/api/epg` payload |
| `recorder.py` | local timeshift recorder (engine verified, player hook parked) |
| `guide_map.json` | channel → exact guide display-name pins |
| `guide_offset.json` | channel → minutes offset (or `null` = hide listings) |
| `server.py` | proxy, playlist rewriting, cache, keep-alive pool, prefetch, endpoints |
| `index.html` | guide (home) + player (second screen), hls.js config, CC, DVR, sync |
| `channels.json` | the catalogue (141 rows, each with badge/note/audio) |
| `tvds.m3u` | same catalogue as a playlist for other players |
| `pc/app.py`, `pc/build-pc.ps1` | single-instance Windows launcher + PyInstaller build |
| `android/` | WebView player, `build-apk.ps1`, assets copy |
| `release/` | shipped artefacts + the external assets the exe prefers |

---

## 17. Standing and maintenance

- The catalogue is a **snapshot**. Re-run `expand.py` + `audit.py` when channels start failing; a
  relay box dying takes several channels with it at once.
- The guide is honest, not complete: 101/141 have listings, and the matched provider name is shown on
  every row so a wrong link is visible. Unmatched channels show `no listings`.
- Provider clocks drift for a minority of feeds; `⏱ Sync` is the correction, not a code change.
- `/tmp`-style scratch (browser profiles, recorder segments) is deleted on start and exit; nothing
  timeshifted survives a restart by design.
- Known gaps, documented rather than papered over: BBC One/Two/Three/Four, ITV2-4, Channel 4/E4 (403
  from a US IP; the only alternatives are DASH `.mpd` or a private box), NBC 4 / PIX 11 / NY1 (TVE),
  TSN/Sportsnet (relay offline), and the Cantonese Guangdong channels (no working public feed).

---

---

# Part II — the second pass (what the first version got wrong)

The chapters above describe the build as it stood when the app first worked. Everything below is
what happened after actually *using* it: a catalogue where a fifth of the channels showed a black
rectangle, and a TV guide that was confidently wrong on most rows. Both had a single root cause of
the same shape — the pipeline verified that bytes arrived, not that the bytes were usable.

## 19. Why 19 channels showed a black screen

### 19.1 The gap

The health gate (§2) proved: master answers → variant answers → a segment of ≥ 20 KB arrives. All
true for a channel that renders nothing, because a browser cannot decode:

- **HEVC** (`hvc1`) — hls.js in Chrome gets nothing usable through MSE
- **AC-3 / E-AC-3 / MP2** audio — picture plays, sound does not
- an **fMP4** segment read without its init segment — ffprobe sees audio only
- an **audio-only** feed

So the catalogue carried channels that passed every check and could not be watched. The fix is a
second gate that asks the only question that matters: *can a browser play this?*

### 19.2 `playability_scan.py`

For every channel it does what the player does — go through `/proxy`, walk to a media playlist,
download a segment, `ffprobe` it — and records `play: ok | vlc | broken` plus the reason.

The first run produced **111 ok / 13 vlc / 17 broken**, and some of those "broken" were channels
that demonstrably worked. Two false-negative traps, both instructive:

| Trap | Symptom | Fix |
|---|---|---|
| **fMP4 without its init segment** | `no video stream` on channels that play fine | fetch `#EXT-X-MAP`, prepend those bytes before probing |
| **one bad segment** | a single ad slate / half-written chunk condemns the channel | sample newest, middle and oldest; retry once on timeout; best verdict wins |

After both fixes the verdicts are stable and repeatable.

### 19.3 `repair_channels.py` — fixing rather than deleting

The other half of the problem was our own doing: many relays publish **both** an HEVC 1080 variant
and an H.264 720 one, and the ranking in §4 ordered by height, so the pipeline stored the HEVC
variant. Chrome then showed nothing.

The repair pass parses the master, enumerates `#EXT-X-STREAM-INF` entries with their `CODECS`,
prefers an H.264 variant, and re-points the channel **only when it had to skip a taller non-H.264
rendition** (otherwise the master stays, which keeps ABR intact). Result: Discovery Channel went
from `no video stream` to `h264 1080p` without touching the source list.

### 19.4 Outcome — 147 → 123

```
playable  112      (H.264 + AAC/MP3 - plays in the app)
vlc-only   10      (real feed, codec a browser cannot decode)
broken     19      (404 / timeout / audio-only)   -> dropped
```

The ten VLC-only channels, with the reason the scan found:

| Channel | Reason |
|---|---|
| RTHK TV 33 | HEVC video |
| Travel Channel | HEVC video |
| TNT | HEVC video |
| American Heroes Channel | audio ac3 |
| IFC | audio ac3 |
| Lifetime | audio ac3 |
| Paramount Network | audio ac3 |
| Starz Comedy | audio ac3 |
| ION | audio ac3 |
| Bounce | audio eac3 |
| CBeebies | audio mp2 |

The app now says so instead of showing a black rectangle: the guide row reads `VLC · HEVC video`, and
opening one shows *"This feed needs VLC"* with the handoff button.

Slow-but-alive feeds, measured (segment fetch, > 4 s): **Fox Sports 2 14.1 s**, Fox Business 7.0,
Love Nature 6.5, CP24 5.6, HBO 2 4.8, Hallmark Movies 4.5. Worth knowing before blaming the app.

### 19.5 TNT, as a worked case

Reported as "dead or just slow to load". It was neither:

```
stored URL  /tnt/tracks-v1a1/mono.m3u8   → 404      (a variant path that no longer exists)
/tnt/index.m3u8                          → 200      (the live master)
master CODECS="hvc1.2.2.L120.90,mp4a.40.2"          (one variant, HEVC, no H.264 alternative)
```

So the URL was repaired and the channel restored — but flagged **VLC-only**, because the only mount
is HEVC. Six other candidate mounts were probed for an H.264 TNT; all 404 or offline. The allowlist
in `expand.py` was updated to the working mount so a rebuild cannot reintroduce the dead path.

## 20. Guide data, take two — the provider was wrong, not the clock

The complaint was precise: *"mad off, none matches"*. The obvious suspect is a timezone bug, so that
was settled with data before touching anything.

### 20.1 Timezone: checked, and cleared

```
programmes sampled: 439,487
offsets: {'+0000': 439,487}        # every entry is UTC
naive=2026-11-05 00:00  stated_utc=2026-11-05 00:00   same
```

Our conversion matched the stated offset exactly. A latent bug was still fixed: the parser used
`time.mktime(...) - time.timezone`, which is only right while the machine is on standard time — at a
DST boundary every listing would shift an hour. It now uses `calendar.timegm` (and honours an
explicit offset if the provider ever writes one).

### 20.2 The provider switch

epg.pw's *content* is the problem: CMT listed as *Cheers* while the feed ran a Christmas film, and
dozens of rows disagreed with reality. **epgshare01** publishes rips of the real listing providers
(zap2it / tvguide style) with per-channel ids such as `CMT.HD.us2`, and is now the primary source:

| File | Size | Channels | Programmes kept |
|---|---|---|---|
| `epg_ripper_US2` | 6.6 MB | 791 | 37 027 |
| `epg_ripper_US_LOCALS1` | 58.1 MB | 4 471 | 259 267 |
| `epg_ripper_US_SPORTS1` | 0.9 MB | 298 | 2 484 |
| `epg_ripper_UK1` | 2.8 MB | 487 | 19 017 |
| `epg_ripper_CA2` | 6.6 MB | 696 | 28 795 |
| `epg_ripper_HK1` | 0.6 MB | 143 | 7 088 |

epg.pw stays as the fallback that fills gaps. Both are merged into one bucket per country.

### 20.3 Three bugs that made wrong matches look right

1. **Both providers wrote the same index file.** `US.json` from epgshare was overwritten by
   `US.json` from epg.pw, so the "new" data was never actually read. Files are now `share-*.json`
   and `pw-*.json`, merged with epgshare first.
2. **`international` was in the "harmless suffix" list.** `CNN` therefore matched `CNN International`
   instead of `CNN HD`. Removed, and candidates are now ranked by *fewest extra tokens*, so an exact
   name always wins over an embellished one.
3. **`norm()` deleted parenthesised text.** `HBO (Pacific)` normalised to plain `hbo`, matched exactly,
   and beat `HBO East` — which is why film times looked wrong on HBO. Brackets are now replaced with
   spaces (the words are kept), and East is preferred over West/Pacific by a small weight.

Result on the same catalogue: **91/123** channels with listings, and the rows that matter read
correctly — `HBO East · The Gilded Age`, `CNN HD · Live: Election Night in America`,
`Cinemax HD · Open Water`, `Starz HD · Replicas`.

### 20.4 CMT: a feed that is not on the published schedule

A frame grab settled it: at 03:49 UTC the "CMT" mount was showing a **black-and-white Christmas
film**, while epg.pw said *Cheers* and epgshare said *CMT Music*. CMT's bug is burned into the
corner, so the channel is right and the relay is not running the published schedule.

No provider can fix that. `⏱ Sync` (per-channel offset in `guide_offset.json`) is the only tool, and
the stale +6 h offset that had been applied while chasing this was cleared, so the channel can be
aligned from scratch if it matters.

## 21. Fullscreen: container, not the video element

The player fullscreens `#stage`, never the `<video>`. Handing the element to the platform player
(the old Android path, `video.webkitEnterFullscreen()`) ignores `object-fit` and stretches a 16:9
feed on a 16:10 screen. What the implementation does now:

```js
enterFull()  -> stage.requestFullscreen()
             -> screen.orientation.lock('landscape')      // where supported
             -> Android.setOrientation('landscape')       // WebView cannot rotate itself
exitFull()   -> document.exitFullscreen() + unlock + orientation back to the user setting
```

- `body.fs` reveals an explicit **✕ exit full** button, because controls auto-hide after 3 s and
  native fullscreen has no visible exit otherwise
- `Esc` and `f` both leave/enter; the button label flips between `⛶` and `⛶ Exit`
- `#stage:fullscreen video { width:100%; height:100%; object-fit:contain }` — the picture letterboxes,
  it never distorts

## 22. Release engineering, and the trap in it

Two rules learned the hard way:

1. **`gh release upload --clobber` does not move the release's date.** GitHub kept showing
   "2 days ago" while the binaries were new, because `published_at` belongs to the release, not the
   asset. The correct move is a new tag (`gh release create v1.3.0 … --target main`) and
   `gh release delete v1.2.0 --yes --cleanup-tag` for the superseded one.
2. **Both binaries must be rebuilt, not just the catalogue.** `server.py`, `epg.py` and `recorder.py`
   are data-shipped and therefore **baked into the exe**; changing them without a rebuild ships a
   stale engine. `channels.json` / `index.html` are hot-swappable next to the exe, but everything
   else needs `pc/build-pc.ps1`. The Android APK additionally embeds its own copy of
   `channels.json`, so a catalogue change means a rebuild there too.

Verification after publishing is done against the API, not the filesystem:

```powershell
curl.exe -s http://127.0.0.1:8777/api/channels | python -c "import json,sys; d=json.load(sys.stdin); print(sum(len(g['channels']) for g in d['groups']))"
curl.exe -s http://127.0.0.1:8777/api/epg | python -c "import json,sys; d=json.load(sys.stdin); print(d['covered'], '/', d['total'])"
```

`v1.3.0` ships: `tvapp.exe` 12,401,505 B, `tvapp.apk` 156,243 B, 123 channels, guide 91/123.

## 23. Changelog, part II

| When | Change |
|---|---|
| Nov 5 | `playability_scan.py`: every channel probed for decodability through the proxy |
| Nov 5 | fMP4 init-segment fix; multi-segment sampling with retry |
| Nov 5 | `repair_channels.py`: prefer H.264 variants; 108 channels re-pointed, later restricted to real downgrades |
| Nov 5 | 19 broken channels dropped, TNT restored as VLC-only → **123 channels** |
| Nov 5 | Guide switched to **epgshare01** primary, epg.pw fallback (91/123) |
| Nov 5 | Fixed: index filename collision, `international` suffix, bracket-stripping in `norm()` |
| Nov 5 | `stamp()` moved to `calendar.timegm` (DST-proof) |
| Nov 5 | Fullscreen targets the stage, landscape lock, explicit exit button |
| Nov 5 | `latency_scan.py`: per-channel start latency, six slow feeds flagged |
| Nov 5 | Release `v1.3.0` cut with both rebuilt binaries; `v1.2.0` removed |

---

## 24. Changelog, part I (the first build)

| Date | Change |
|---|---|
| Nov 3 | Catalogue built to 147 channels; proxy, player, APK |
| Nov 3 | Keep-alive pool + prefetch; measured latency table |
| Nov 4 | Guide added, then fixed to strict country-scoped matching (HBO Family was taking a Hong Kong schedule; 101 correct rows after the fix) |
| Nov 4 | Two-screen UI: guide as home, player as a second screen with back navigation |
| Nov 4 | Buffer modes Live/Smooth/Deep; DVR bounded by the feed's real window |
| Nov 4 | `audit.py`, dead-channel pruning (147 → 141), `Hide offline` |
| Nov 5 | Recorder engine written and verified; player hook parked |
| Nov 5 | Scratch cleanup (9.1 GB of profiles purged), single-instance launch |
| Nov 5 | `v1.2.0` published with current exe + APK |
