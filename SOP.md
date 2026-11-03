# TVDS SOP — finding live streams, serving them locally, and shipping an Android app

A complete, reproducible record of building **TVDS**: a stream-sourcing pipeline, a
local web player with a caching proxy, and a signed Android APK — from an empty folder
to 141 verified channels playing on a Fold-class screen.

Written from the actual run. Every command, version, path, failure and fix here was used
on this machine; nothing is reconstructed from memory. Where something did not work, it
says so and gives the numbers.

**Target device:** Samsung Galaxy Z Fold 7 (inner display 2184×1968, ~374 dpi ≈ 934×842 dp,
Snapdragon 8 Elite, Android 15/16).
**Test environment:** Android 14 `google_apis` `x86_64` emulator, plus a purpose-built
`Fold7_Test` AVD (2184×1968 @ 374 dpi). Several issues were only visible on the wide
profile; they are marked **[fold]**.

---

## What was delivered, in order

1. **Verification gates for streams** — a four-stage check (HTTP → playlist → variant →
   segment → resolution → frame) that turned a 1,495-entry US playlist into 92 channels
   that actually play.
2. **A channel pipeline** — `build.py`, `import_m3u.py`, `finalize.py`, `fix_heights.py`,
   `final_pass.py`: harvest, verify, dedupe, force H.264 where a feed offers it, discover
   undeclared resolutions with ffprobe, filter to English/Chinese.
3. **141 verified channels** across Hong Kong (12), New York (5), US Cable (77), Sports (17),
   News (14), UK (7), Canada (9) — rebuilt from a mainstream-network allowlist (§4.9),
   not from whatever playlist happened to be lying around.
4. **A local web app** — `server.py` + `index.html`: an HLS proxy with playlist rewriting,
   CORS, per-stream Referer, a 192 MB segment cache and read-ahead prefetch.
5. **Quality locking and latency tuning** — MAX mode pins the top variant and re-pins after
   every switch; `liveSyncDurationCount: 1` puts playback one segment behind the edge.
6. **A desktop app** — Edge `--app=` window, PWA manifest, service worker, one-click
   `.bat`, desktop shortcut, detached server that survives closing the console.
7. **An Android app** — a 0.15 MB signed APK, WebView + hls.js, no Gradle, no Android Studio,
   built with the SDK tools directly; two-pane layout on foldables.
8. **Verified playback** — ABC 7 WABC 1920×1080 / 7.45 Mbps / 6.2 s latency; TVB News, ViuTV
   and NBA TV also confirmed live in the app.

---

## 1. Prerequisites & toolkit

| Tool | Exact version used | Purpose |
|---|---|---|
| Python | 3.13.15 | Pipeline, proxy server, all harvesting |
| JDK | 21.0.12.101 (Microsoft build) | `javac`, `jar`, `keytool` |
| Android SDK | `C:\Android\Sdk` | build-tools, platform, platform-tools, emulator |
| build-tools | **36.0.0** (34.0.0 present but unusable — see §9.1) | `aapt2`, `d8`, `zipalign`, `apksigner` |
| Android platform | `platforms/android-35/android.jar` | compile-time API |
| platform-tools | latest | `adb` |
| Emulator | 37.1.11.0 | `Pixel_Test` (phone), `Fold7_Test` (foldable) |
| System image | `system-images;android-34;google_apis;x86_64` | install-and-test loop |
| WebView | 113.0.5672.136 (in the API 34 image) | the Android player runtime |
| VLC | 3.x at `C:\Program Files (x86)\VideoLAN\VLC\vlc.exe` | HEVC / AC-3 escape hatch |
| Edge / Chrome | any recent | desktop app window (`--app=`) |
| ffmpeg / ffprobe | 9.0.2 (gyan.dev full build) | resolution probing, frame grabs |

One-time SDK setup used here (already present on this machine):

```powershell
$env:PATH = "C:\Android\Sdk\platform-tools;C:\Android\Sdk\build-tools\36.0.0;C:\Android\Sdk\emulator;$env:PATH"
```

---

## 2. Machine-specific traps (each one cost real time)

| Trap | Symptom | Fix |
|---|---|---|
| **PowerShell 5.1 reads BOM-less `.ps1` as ANSI** | `The string is missing the terminator: "` on a line that looks fine | Save scripts **ASCII-only** or UTF-8 **with BOM**; never put `—` `·` `“` in a `.ps1` |
| `Start-Process -ArgumentList` splits on spaces | `error: invalid flag: C:\AI` from `javac` | Build one quoted argument string and pass it as a single `-ArgumentList` |
| `javac @response-file` splits on spaces too | `Invalid filename: C:AI STUFF...` | Don't use response files; use the `Start-Process` method above |
| `.NET ReadAllText` ignores the shell's `cd` | `Could not find file 'C:\AI STUFF\Local\assets\index.html'` | Always pass absolute paths to .NET calls |
| `d8` 34.0.0 on JDK 21 | `NullPointerException: Cannot invoke "String.length()"` deep in R8 | Use **build-tools 36.0.0** `d8` |
| `d8` with many loose `.class` files | Same NPE | `jar cf classes.jar -C classes .` first, pass the jar |
| PowerShell prints native stderr as an error | Build "fails" with `exit code 1` although the APK was produced | Filter: `2>&1 \| Select-String 'signed\|failed'` |
| WebView `file://` → CDN | Silent CORS failure, endless spinner | `setAllowUniversalAccessFromFileURLs(true)` + `MIXED_CONTENT_ALWAYS_ALLOW` |

---

## 3. How streams are actually found

Four layers, ordered by yield. The first gets most of the list; the second gets the best
quality; the last two fill gaps.

### 3.1 Catalogue APIs

```powershell
# every channel + every stream, with quality and referrer hints
https://iptv-org.github.io/api/channels.json
https://iptv-org.github.io/api/streams.json
# pre-built playlists
https://iptv-org.github.io/iptv/countries/us.m3u
https://iptv-org.github.io/iptv/categories/news.m3u
```

```powershell
# Pluto TV's own catalogue - 466 channels, includes local-news FAST feeds
Invoke-RestMethod https://api.pluto.tv/v2/channels |
  Where-Object { $_.name -match 'New York|CBS|NBC|FOX' } |
  Select-Object _id, name
```

Pluto playback URL pattern that works without auth:
`https://jmp2.uk/plu-<channelId>.m3u8`

### 3.2 Station page scraping — the highest-quality source

The station's own CDN beats every relay. Two real examples from this run:

```powershell
# ABC 7 New York: page source carries CloudFront CMAF manifests
$c = (Invoke-WebRequest 'https://abc7ny.com/watch/' -UseBasicParsing -Headers @{'User-Agent'='Mozilla/5.0'}).Content
[regex]::Matches($c,'https?://[^"''\s<>]+\.m3u8[^"''\s<>]*') | ForEach-Object { $_.Value }
# -> https://d3ip9j8kiqtf8w.cloudfront.net/out/v1/9adf.../ADHOC-WABC-02-cmaf-manifest/ADHOC-WABC-02-index.m3u8

# CBS 2 New York
# https://www.cbsnews.com/newyork/live/
# -> https://newyo9h67ea3.airspace-cdn.cbsivideo.com/live/manifest.m3u8
```

### 3.3 Community playlists (Hong Kong)

`https://raw.githubusercontent.com/ineed2underfit/hk-iptv/main/hk_live.m3u` — auto-updated
daily, 30 entries covering TVB Jade / Pearl / News, ViuTV, HOY, RTHK. Import and verify it
with `import_m3u.py` (§4.2).

### 3.4 Relay hunting

Search `<channel> m3u8`, then trust only what passes the gates. Relays look like
`http://23.237.104.106:8080/USA_NBA/index.m3u8`, `http://85.237.89.160:9590/usa-s/ESPN-HD/index.m3u8`.
They are someone's box; expect them to die within weeks.

Standing hosts, re-verified on every `expand.py` run (2026-11-03):

```
http://23.237.104.106:8080/USA_<TOKEN>/index.m3u8      US cable, ~30 live tokens
http://212.5.144.156:8080/<token>/tracks-v1a1/mono.m3u8 cnn, tnt, discovery, history, id, cartoon ...
http://85.237.89.160:9590/usa-s/<NAME>-HD/index.m3u8   ESPN family, FS1, NBA, NFL, NHL, MLB, golf
http://23.239.31.26:8989/<token>/index.m3u8            a&e, amc, animal planet, axs, bbc america ...
http://198.58.104.90:8989/<token>/index.m3u8           natgeo, nick, nfl, mtv2, oxygen, newsnation
http://4.30.180.36:8420/<token>/index.m3u8?token=test  msnbc, usa, tbs, hgtv, tlc, fx, hallmark ...
https://tvsen6.aynaott.com|tvsen7.aynascope.net/<ch>/  foxnews, cnbc, hbo, hgtv, golf (432p mounts)
http://41.205.93.154/<TOKEN>/tracks-v1a1/mono.m3u8     bravo, cnbc, hallmark, espnnews
http://190.93.224.42/<TOKEN>/index.m3u8                Canadian: GLOBAL, HISTORY
https://cdn.qd.je/163189/<token>                       UK: ch5
https://fast.hidns.vip/163189/<tok> | cdn6.cc.cd/163189/<tok> | php.jdshipin.com/TVOD/iptv.php?id=<tok>
                                                       Cantonese: fct, mzt, wxxw, viu, viu6, j1, hoy78
```

Two of the `:8989` boxes serve an Apache **directory index** — `GET /` returns the whole token
list, so no guessing is needed there:

```powershell
curl.exe -s http://198.58.104.90:8989/ | Select-String 'href="'
```

The others answer 404 on `/`, `player_api.php` and `get.php`, so their tokens have to be
guessed from the channel name (`USA_<CHANNEL>`, `<channel>/tracks-v1a1/mono.m3u8`). A guess
costs one small GET; a hit is verified like any other candidate.

Geo-blocking is the wall for the UK majors: every BBC One/Two/Three/Four and iPlayer URL in
the iptv-org UK list answers **403** from a US address (`vs-cmaf-pushb-uk-live.akamaized.net`,
`vs-hls-push-uk-live.akamaized.net`), and `Channel 4`/`ITV2-4`/`E4` only exist at 360p behind
the same block. Any UK-mainland lineup needs a UK egress; from here only BBC News, ITV1,
Channel 5, GB News, TalkTV, CBeebies and the Sky weather loop verify.


### 3.5 The verification gates (this is the actual work)

A `200 OK` means nothing. Every candidate must pass all of:

```
1. GET master playlist        -> 200 and body starts with #EXTM3U
2. pick highest RESOLUTION    -> from #EXT-X-STREAM-INF, else highest BANDWIDTH
3. GET that variant playlist  -> 200, again #EXTM3U
4. GET the newest segment     -> 200 and > 20 KB
5. ffprobe the stream         -> real width/height/codec
6. frame grab + eyeball       -> is this the channel it claims to be?
7. EXT-X-PROGRAM-DATE-TIME    -> is it live, or a slate?
```

Step 6 is not optional. It is what caught these mislabels:

| Claimed | Actually | Evidence |
|---|---|---|
| "CNN US 1080p" (`212.5.144.156:8080`) | CNN **International**, HEVC, 1.4 Mbps | frame showed the same Newsroom feed, 5:10 AM GMT bug |
| "CNN Fast UK" / `tvsen6.aynascope.net` | **clip loops**, not the live channel | frames showed `CNN.com` bug, no live clock |
| `turnerlive.warnermediacdn.com/.../cnn_slate/` | off-air slate | `EXT-X-PUBLISHED-TIME` months stale |

Step 7 example, NBA TV, live:

```
seglen            6.2 s
playlist start    12.8 s old
last segment ended 6.7 s ago   -> genuinely live
```

---

## 4. The channel pipeline

Two generations live in this repo.

**Current (use this):** `expand.py` — the network allowlist is the input, the harvest feeds it.
See §4.9.

```
expand.py     -> channels.json    (allowlist -> sources -> probe -> best copy per network)
fix_heights.py-> channels.json    (ffprobe real resolution where undeclared)
make_m3u.py   -> tvds.m3u
```

**First generation**, kept because it is still the cleanest way to pull in one external
playlist or to re-run the catalogue harvest from scratch. Five scripts, run in order; each
writes JSON that the next one consumes.

```
build.py      -> main.json        (catalogue harvest, id/country/regex groups)
import_m3u.py -> hk2.json, us_imp.json, uk_imp.json, ca_imp.json
finalize.py   -> channels.json    (merge, dedupe, codec preference, overrides)
fix_heights.py-> channels.json    (ffprobe real resolution where undeclared)
final_pass.py -> channels.json    (NYC/Paramount additions + language filter)
```

### 4.1 `build.py` — catalogue harvest

```powershell
python build.py --only us,uk,sports,news --out main.json --workers 48
```

Groups are declared as explicit ids, a country code, or a name regex. Every candidate is
probed with the gates above (up to 4 per channel, HD first). Result of this run:

```
190 candidates across 4 groups
  US Cable      37 live
  UK             1 live
  Sports         9 live
  News          17 live
```

### 4.2 `import_m3u.py` — any external playlist

```powershell
python import_m3u.py --url https://iptv-org.github.io/iptv/countries/us.m3u `
                     --group "US Cable" --out us_imp.json --workers 64
```

- 1,495 candidates in, **1,113 live** out (before name filtering).
- Keeps the best working copy per channel name; `--min-height 720` drops SD.

### 4.3 `finalize.py` — merge, dedupe, codec

```powershell
python finalize.py
```

- Merges `main.json hk2.json us_imp.json uk_imp.json ca_imp.json`.
- Applies `overrides.json` (hand-picked URLs that beat the harvested copy).
- **Codec pass:** for each channel, parse `CODECS="..."` per variant; if the top variant is
  HEVC and an H.264 variant exists, **switch to H.264** so it plays in a browser. If only
  HEVC exists, the entry is kept and tagged `(HEVC)`.
- **Dedupe** by `(group, name, codec)`; duplicate URLs dropped.
- Then calls `fix_heights.fix()` automatically.

### 4.4 `fix_heights.py` — resolutions that aren't declared

Pluto, turnerlive and the `:8989` relays serve a bare media playlist with **no
`RESOLUTION` attribute**. The manifest simply never says "1920x1080", so the harvest can
only label them `live`. This script asks the actual video stream:

```
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,codec_name
        -of csv=p=0 -user_agent ... -allowed_extensions ALL -rw_timeout 12000000 URL
```

Two-stage, because some relays break ffprobe:

1. probe the playlist directly;
2. if that fails (**segments with no file extension** — `https://fast.hidns.vip/api/hoy/188424`),
   download one segment and probe the file as raw MPEG-TS.

This run: 35 undefined → **33 resolved**, 2 pruned as dead (404). HK channels went from
`live` to `1080p`.

Parsing gotcha: ffprobe prints `codec_name,width,height` regardless of the order you ask
for, so parse the **numeric fields**, not fixed positions.

### 4.5 `final_pass.py` — curated additions + language filter

```powershell
python final_pass.py
```

- Adds the verified NYC/Paramount entries (§4.6).
- Replaces same-name entries so a browser-safe copy beats an HEVC duplicate.
- **Language filter:** catalogue `languages` where known, else a name heuristic; keeps only
  `eng` / `zho`. Dropped **21 channels**: Telemundo ×5, Univision ×2, Galavision ×2,
  CNN en Español, MTV en Español, MTV Flow Latino, Nickelodeon en español, Newsmax Spanish,
  Telemundo Telenovelas, TRT World, TRT Haber, Haber Global, KBS World, Arirang TV,
  Eurosport 2 (Hungarian feed).

Heuristic gotcha worth remembering: `re.X` (verbose) **ignores whitespace in the pattern**,
so `ort ` became `ort` and ate `Sp**ort**s 1` and `N**orth** America`. Fixed with `\b`
word boundaries.

### 4.6 `overrides.json`

Hand-pinned URLs that beat the automatic pick — TVB Jade (H.264 1080p instead of HEVC-only),
ESPN/ESPN 2 (720p59.94 H.264 instead of 1 Mbps HEVC), NBA TV (1080p59.94), CNN International
(1080p50 5.6 Mbps), BBC News, ABC 7, CBS 2.

### 4.7 `channels.json` schema

```json
{ "groups": [ { "name": "New York", "channels": [
  { "name": "ABC 7 WABC (New York)",
    "badge": "1080p", "height": 1080, "quality": "1920x1080",
    "codec": "H.264", "url": "https://.../ADHOC-WABC-02-index.m3u8",
    "note": "1080p | H.264 | ABC 7 New York live O&O" } ] } ] }
```

`referer` is optional and is forwarded to the upstream by the proxy.

### 4.8 Final lineup

```
Hong Kong 12   TVB Jade, TVB Jade North America, TVB Pearl, TVB News, ViuTV, ViuTVsix,
               HOY TV 77, HOY Info 78, now News, RTHK TV 31/32/33
New York   5   ABC 7 WABC (1080p), CBS 2 (1080p), Fox 5 WNYW (1080p), CBS News NY, News 12 NY
US Cable  77   CNN, HLN, MSNBC, Fox News, Fox Business, CNBC, Bloomberg, Newsmax, NewsNation,
               Cheddar, PBS, PBS Kids, Weather, AccuWeather, Court TV, ION, Bounce, Grit,
               AXS, Revolt, Adult Swim, TBS, TNT, truTV, USA, FX, SYFY, AMC, IFC, Sundance,
               BBC America, Freeform, HBO / HBO 2 / HBO Family, Showtime / Showtime Family,
               Starz / Starz Cinema / Starz Comedy / Starz Kids, Cinemax, Paramount,
               Comedy Central, MTV / MTV2 / MTV Live, VH1, BET, CMT, Discovery, TLC,
               Animal Planet, Science, ID, Destination America, Cooking, Food, HGTV,
               Travel, History, A&E, Lifetime, WE tv, OWN, Bravo, E!, Hallmark /
               Hallmark Movies & Mysteries, National Geographic, Nat Geo Wild,
               Cartoon Network, Nickelodeon, Nick Jr., Disney Channel / Junior / XD,
               TV Land-free set (absent: FXX, FXM, LMN, Oxygen, Reelz, C-SPAN, MeTV)
Sports    17   ESPN, ESPN 2, ESPNU, ESPN News, FS1, FS2, NBA TV, MLB Network, NFL Network,
               NHL Network, Golf, Tennis, Big Ten, SEC, ACC, CBS Sports HQ, CBS Golazo
News      14   ABC News Live, NBC News NOW, CBS News 24/7, Scripps, Bloomberg Originals,
               DW, France 24, Euronews, Al Jazeera, TRT World, CGTN, CNA, WION, NDTV 24x7
UK         7   BBC News (1080p), ITV1 (720p), Channel 5, CBeebies, GB News, TalkTV,
               Sky News Weather   (BBC One/Two/Three/Four, C4, E4, ITV2-4: 403 from US IPs)
Canada     9   CBC News Network (1080p), Global (1080p), CTV, CityNews Toronto / Vancouver /
               Calgary / Edmonton, The Weather Network, Love Nature
```

Every entry carries its real resolution (ffprobe-confirmed, not the manifest's claim), its
codec, and its measured bitrate. Anything under 480p that survived is a genuinely scarce
network (CBS News NY, News 12, ABC News Live, BET, Weather Network at 360p; Fox Business and
FS2 at 432p) and is labelled as such in the badge.

### 4.9 `expand.py` — the allowlist rebuild (current pipeline)

The first-generation pipeline (§4.1–§4.5) started from whatever the iptv-org catalogue
happened to list, which is how a 116-channel file ended up with `PBS KMOS-TV`, `That's 70s`,
`TSN The Ocho` and eight overlapping BET/MTV Pluto channels. `expand.py` inverts that: the
network list is the input, and the harvest exists only to feed it.

```powershell
python expand.py --workers 64        # rebuild channels.json
python expand.py --dry-run --show    # report + list, touch nothing
python expand.py --explain "MSNBC"   # per-feed verdicts for one channel
```

**Allowlist.** `TARGETS` at the top of the file, one `T(group, name, aliases, extra=(), regex=(), min_h=0)`
per network. `aliases` are normalised names (parentheticals, `(1080p)`, `[Geo-blocked]`, trailing
`HD` stripped) so `CNN`, `CNN HD`, `CNN (United States) (720p)` and `CNN (HEVC)` all land on the
same target; `regex` covers families like `^bbc one\b`; `extra` holds hand-found URLs.

**Sources.** iptv-org country playlists (us/uk/ca/hk/zho/cn), the iptv-org API (channel id →
streams, so names come from the catalogue rather than from a random playlist label), the
hk-iptv playlist, the previous `channels.json` (already-verified feeds keep competing) and
`overrides.json`. Downloads are cached for 6 h in `.cache/`.

**Probe.** Same gates as §3.5, implemented once in `probe_stream()`:

```
1. GET the playlist, reject anything without #EXTM3U
2. parse #EXT-X-STREAM-INF ourselves (RESOLUTION + CODECS + BANDWIDTH)
3. drop variants below --min-height, prefer AVC over HEVC at any height
4. GET the chosen variant, then its first segment -> must be 200 and >= 20 KB
```

**Ranking** per target, best first:

```
(avc, brand_hint, trusted, height, bandwidth)
```

* `avc` — hls.js cannot decode HEVC, so an H.264 720p feed beats an HEVC 1080p one.
* `brand_hint` — does the URL itself carry the channel name (`/cnn/`, `espn-2-hd`)? This is
  what stops a mislabelled playlist entry (`"India Today"` pointing at an OnDemandKorea
  manifest) from winning.
* `trusted` — host allowlist (amagi, akamaized, cloudfront, turnerlive, the standing relay
  boxes, hidns/jdshipin). A 720p feed on a known host beats a 1080p feed on a box that will be
  gone next week.

**Filters.** Radio/audio-only URLs, and any feed whose path carries a non-English language tag
(`film4_hun`, `euronews-pl`, `…/est_2.m3u8` is *not* Estonian, hence the token-boundary check
and the timezone false-positive exclusion).

**Fallback.** If nothing clears `--min-height 720`, the best feed at or above
`--fallback-height 432` is taken instead, and the badge tells the truth. Below that the
channel is dropped rather than padded with a 360p potato — which is why `FXX`, `FXM`, `LMN`,
`Oxygen`, `Reelz`, `C-SPAN`, `CBS Sports Network`, `MeTV` and `Cozi` are simply absent from
the current build: no working feed cleared the bar today.

**Output.** One entry per network, deduped by URL inside a group, groups emitted in
`GROUP_ORDER`, then `note` is composed as `NNNp | codec | bitrate | verified`. The probe cache
means a no-op re-run finishes in seconds:

```
$ python expand.py --workers 64
142 channels verified in 2s (0 feeds probed)     # all 828 candidates cached
```

### 4.10 `audio_scan.py` — "the picture plays but there is no sound"

A verified stream is not necessarily an audible one, and that is exactly how a viewer ends up
staring at a silent channel blaming the player. Three distinct cases, all found in this
catalogue:

| Case | Evidence | Fix |
|---|---|---|
| **video-only relay** | `https://10.fast.hidns.vip/163189/fct/video/index.php` (the old "TVB Jade (HEVC)" slot): the media playlist has no `#EXT-X-MEDIA`, and the fMP4 init+segment contain one `hevc` track and **zero audio tracks** | switch to a feed that carries audio — the H.264 `r.jdshipin.com/qrfbg` copy is `h264 + aac ×2` |
| **Dolby / MPEG-1 audio** | 18 channels expose `ac-3` / `eac3` / `mp2` (AMC, A&E, IFC, Sundance, BBC America, Cinemax, Lifetime, Bounce, ION, MTV2, MTV Live, Starz Comedy, Nat Geo Wild, NewsNation, ACC Network, NFL Network, CBeebies) | no browser decoder exists — the **VLC** button is the only path, so the UI says so |
| **separate audio rendition** | ViuTV's master has `#EXT-X-MEDIA:TYPE=AUDIO` ch1/ch2/ch3 pointing at `…/viu/audio/index.php`; the video variant alone looks silent | hls.js picks the default rendition; the scanner must read the master, not just the variant |

`audio_scan.py` walks the playlist the same way the player does and tags `channels.json`:

```powershell
python audio_scan.py --workers 10      # full sweep, writes "audio": AAC | AC-3 | MP2 | none | ?
python audio_scan.py --unknown         # retry the timeouts (slow boxes)
python audio_scan.py --drop-silent     # remove channels with no audio track at all
```

Probe order per channel, because ffmpeg refuses a lot of these relays:

```
1. ffprobe <url>                                   plain .m3u8, works for most broadcasters
2. ffprobe -f hls -allowed_extensions ALL <url>    index.php style playlists
3. master scan for #EXT-X-MEDIA TYPE=AUDIO         catches ViuTV-style renditions
4. GET EXT-X-MAP + a real media segment, then      .jpg-named fMP4/TS, AMAGI ad beacons,
   ffprobe -f mpegts / -f mp4 the bytes            jdshipin index.php mounts
```

Gotcha that cost a run: `-of csv=p=0` prints **codec_name first, then codec_type**
(`h264,video`), not the order the fields were requested in — the first parser assumed
type-first and reported "?" for all 142 channels.

---

## 5. The desktop app

### 5.1 Why a proxy exists

Public relays send **no CORS headers**, and several demand a **Referer**. A browser page
cannot fetch them directly. `server.py` fetches upstream, rewrites every child URL back
through itself, and stamps permissive CORS on everything.

```
GET /                 -> index.html
GET /hls.min.js       -> player engine
GET /api/channels     -> channels.json
GET /api/probe?u=     -> {ok,status,type}
GET /api/stats        -> {cache_items,cache_mb,hits,misses,prefetched}
GET /open?u=&r=       -> launch VLC with the stream
GET /proxy?u=&r=      -> the actual stream
```

### 5.2 Playlist rewriting

```python
# every non-comment line and every URI="..." attribute becomes a /proxy URL
def rewrite_playlist(body, base, referer):
    out = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            if 'URI="' in s:
                head, _, rest = s.partition('URI="')
                uri, _, tail = rest.partition('"')
                out.append(head + 'URI="' + prox(urllib.parse.urljoin(base, uri), referer) + '"' + tail)
            else:
                out.append(s)
            continue
        out.append(prox(urllib.parse.urljoin(base, s), referer))
```

`urljoin` against the **post-redirect** URL is what makes relative paths like
`../ADHOC-WABC-02-mp-channel_video_1_5228638.mp4` resolve correctly.

### 5.3 Content sniffing beats Content-Type

Several relays serve a playlist as `application/octet-stream` from a redirect endpoint with
no `.m3u8` suffix. The proxy reads the first 64 KB and checks the magic bytes:

```python
head = r.read(64 * 1024)
if head.lstrip()[:7] == b"#EXTM3U":
    is_playlist = True
```

### 5.4 Segment cache + read-ahead

HLS is strictly sequential, so parallelism cannot lower latency — but it can stop the
buffer emptying. After serving a media playlist the proxy warms the newest segments in the
background:

```python
CACHE_BUDGET   = 192 MB      # LRU, keyed by upstream URL
PREFETCH_DEPTH = 3           # segments
PREFETCH_SLOTS = 4           # concurrent fetches
```

Measured on CNN International, a 4.24 MB segment:

```
X-Cache: HIT
time=0.004298s  bytes=4244852
{"cache_items":29,"cache_mb":29.0,"hits":103,"misses":66,"prefetched":29}
```

4.3 ms from cache versus ~250–400 ms from CloudFront. **Playlists are never cached** — they
must be rewritten on every request (that bug is §9.4).

### 5.5 Player configuration

```js
hls = new Hls({
  lowLatencyMode: true,
  maxBufferLength: 8, backBufferLength: 6,
  liveSyncDurationCount: 1,         // default 3
  liveMaxLatencyDurationCount: 2,   // jump forward instead of drifting
  maxLiveSyncPlaybackRate: 1.5,
  capLevelToPlayerSize: false,
  defaultAudioCodec: 'mp4a.40.2'    // skip AC-3 renditions the browser can't decode
});
```

MAX quality is **pinned**, not preferred:

```js
hls.currentLevel = bestLevel();               // manual mode: ABR disabled
hls.on(Hls.Events.LEVEL_SWITCHED, ...)        // any downswitch is corrected immediately
```

`bestLevel()` sorts by height, then by **bitrate** when masters omit `RESOLUTION`
(Pluto/FAST feeds do).

### 5.6 The latency floor (do the arithmetic before promising "lag-free")

```
NBA TV, live:   seglen 6.2 s,  program-date-time 12.8 s old,  last segment ended 6.7 s ago
liveSyncDurationCount = 1  ->  play one 6.2 s segment behind the edge
=> ~12-13 s behind wall clock
```

`liveSyncDurationCount` cannot go below 1, so the floor is **2 × segment duration**.
Pluto's ~10 s segments land near 20 s. The only way past that is LL-HLS (`EXT-X-PART`), and
none of these relays publish it.

Above that floor, every avoided round trip is removed latency, and the biggest one was free:
the proxy used to open a **new TCP + TLS connection for every segment**. The segment fetch is
sequential by nature (HLS gives you one 6 s chunk at a time), so a handshake ahead of each one
is dead time before the first byte of video. `server.py` now keeps a pool of idle
`(scheme, host, port)` connections and hands them back only after the body is drained
(`_Upstream.release()`), plus four long-lived prefetch workers whose sockets stay warm.

Measured on this machine, same segment, 4 pulls each, median:

```
ESPN (slow TLS host)   1304 ms -> 410 ms   (~0.9 s saved per segment)
BBC News                182 ms ->  92 ms
CBC News Network        142 ms ->  90 ms
CNN                     169 ms -> 139 ms
HOY TV 77               164 ms -> 142 ms
Channel 5               185 ms -> 142 ms
```

Player side matches it: `liveSyncDurationCount: 1`, `maxBufferLength: 6`,
`backBufferLength: 4`, prefetch depth 4 into a 192 MB cache. The `mLat` pill shows the real
`hls.latency`, and its tooltip names the segment length, because that is the part of the
delay no player can remove.

Gotcha: `server.py` is bundled into the exe as **data**, so PyInstaller never scans its
imports. `import queue` (the prefetch queue) was missing from the frozen build and the exe
died silently at startup; `build-pc.ps1` now passes `--hidden-import queue` (plus
`http.client`, `io`).

### 5.7 Desktop app window

```powershell
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app="http://127.0.0.1:8777/" --window-size=1440,900
```

plus `manifest.json`, `icon-512.png` (generated with `System.Drawing`), `sw.js` caching only
the shell. `tvapp.bat` checks Python → assets → channels.json → port → starts the server
**detached via `Win32_Process.Create`** → waits for `200` → opens the app window.

---

## 6. The Android app

### 6.1 Why a WebView, not MediaPlayer or ExoPlayer

The framework player was tried first and **fails on modern station streams**:

```
MediaPlayer: Error (1,-1007)     <- ABC 7's CMAF/fMP4 HLS
MediaPlayer: Error (1,-2147483648) <- HK relays with .jpg-named TS segments
```

`-1007` is `ERROR_MALFORMED`: MediaPlayer handles MPEG-TS HLS, not CMAF. ExoPlayer would
work but needs Gradle and ~10 MB of AndroidX. The WebView runs the **same hls.js build as
the desktop app**, which already handles both, and ships in a 0.15 MB APK.

### 6.2 Assets

```
android/assets/index.html    mobile UI (list + fullscreen player)
android/assets/hls.min.js    copied from the desktop tree by build-apk.ps1
android/assets/channels.json same catalogue, embedded
```

WebView settings that make `file://` behave like a browser on a server:

```java
s.setAllowFileAccessFromFileURLs(true);
s.setAllowUniversalAccessFromFileURLs(true);              // file:// may XHR any CDN
s.setMixedContentMode(MIXED_CONTENT_ALWAYS_ALLOW);        // half the relays are http://
s.setMediaPlaybackRequiresUserGestures(false);
s.setSupportZoom(false); s.setTextZoom(100);
```

A JS bridge exposes the escape hatch for feeds WebView can't decode:

```java
web.addJavascriptInterface(new Bridge(), "Android");      // Android.openExternal(url) -> VLC chooser
```

### 6.3 Building without Gradle or Android Studio

`android/build-apk.ps1` is the whole build system:

```
1  assets            copy channels.json + hls.min.js from the repo root
2  keystore          keytool -genkeypair (debug.keystore, pass android)
3  aapt2 link        resources + manifest + assets -> app-unsigned.apk
4  javac             -source 8 -target 8 -classpath android.jar -d classes   (Start-Process, one quoted arg string)
5  jar + d8          classes.jar -> dexout/classes.dex  (d8 from build-tools 36.0.0)
6  zip               classes.dex inserted into the APK
7  zipalign -f 4  +  apksigner sign
```

Result: `tvapp.apk`, **0.15 MB**, `com.tvapp.player`, minSdk 21, targetSdk 34, signed and
verified. Full build takes ~15 s.

### 6.4 Manifest essentials

```xml
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.WAKE_LOCK"/>
<application android:usesCleartextTraffic="true" ...>
  <activity android:name=".MainActivity"
            android:theme="@android:style/Theme.Material.NoActionBar"
            android:configChanges="orientation|screenSize|keyboardHidden|screenLayout|smallestScreenSize|density"
            android:exported="true"> ... </activity>
```

`cleartextTraffic` is required — many relays are plain `http://`.
`configChanges` is what stops a fold/unfold from restarting playback.

### 6.5 Two-pane layout **[fold]**

The mobile UI switches to list-left / player-right above a width breakpoint:

```css
@media (min-width:840px){
  #list{ right:auto; width:370px; border-right:1px solid var(--line) }
  #player{ left:370px }
  #bar{ width:370px }
}
```

**840 px, not 900**: the Fold 7 inner screen in *portrait* is 1968 px ÷ (374/160) = **842 dp**.
A 900 px breakpoint silently fell back to single column on the exact device this is for.

### 6.6 Deep link

```powershell
adb shell am start -n com.tvapp.player/.MainActivity --es play "https://.../index.m3u8"
```

`MainActivity` reads the extra and loads `index.html?play=<encoded>`; the page parses it and
starts playback. This is also how the app was tested without fighting touch coordinates.

---

## 7. Fold 7 profile emulator **[fold]**

```powershell
$avd = 'C:\Android\Sdk\cmdline-tools\latest\bin\avdmanager.bat'
'no' | & $avd create avd -n Fold7_Test `
        -k "system-images;android-34;google_apis;x86_64" `
        -d "8in Foldable" --force
```

Then match the hardware (the generated config uses `key = value` spacing — a regex written
for `key=value` silently does nothing):

```
hw.lcd.width   = 2184
hw.lcd.height  = 1968
hw.lcd.density = 374
hw.ramSize     = 4096
hw.keyboard    = yes
```

```powershell
emulator -avd Fold7_Test -no-window -no-audio -no-boot-anim -no-snapshot -gpu swiftshader_indirect
adb shell wm size 2184x1968
adb shell wm density 374
adb shell wm size; adb shell wm density      # verify: Override size 2184x1968 / Override density 374
```

Running two emulators on the same AVD fails with
`FATAL | Running multiple emulators with the same AVD` — add `-read-only` if you genuinely
need a second instance.

---

## 8. Verification evidence

| Check | Command | Result |
|---|---|---|
| Desktop server up | `curl http://127.0.0.1:8777/api/channels` | `200` |
| Proxy chain, CNN | master → variant → segment | `200 video/MP2T 3434760` |
| Proxy chain, WABC (CMAF) | master → variant → segment | `200 video/mp4 3715936` |
| Cache works | second GET of same segment | `X-Cache: HIT`, `0.0043 s` |
| Cache stats | `/api/stats` | `hits 103 / misses 66 / prefetched 29` |
| Desktop playback | headless screenshot | ABC 7 `1920x1080 7.45 Mbps live 20.4s` |
| APK builds | `build-apk.ps1` | `signed -> tvapp.apk (0.15 MB)` |
| APK installs | `adb install -r` | `Success` |
| App lists channels | screenshot | 141 ch, 7 groups |
| App plays (phone profile) | `?ch=9` deep link | ABC 7 `1920x1080 7.45 Mbps 6.2s` |
| App plays (fold profile) | tap TVB News / ViuTV | video in right pane, `9.0s` latency |
| Fold two-pane | screenshot at 934×842 dp | list 370 px left, player right |
| HEVC on emulator | TVB Jade (HEVC) | **fails** — software decode only; hardware device expected to pass |
| APK signature | `apksigner verify --print-certs` | `CN=Android Debug`, SHA-256 `6b80c9b4…` |

---

## 9. Bug log — symptom → cause → fix

### 9.1 `d8` crashes on JDK 21
**Symptom** `NullPointerException: Cannot invoke "String.length()"` in `R8_8.2.2`, exit 1.
**Cause** build-tools **34.0.0**'s bundled R8 is incompatible with JDK 21.
**Fix** use `build-tools\36.0.0\d8.bat`. Also pass a single `classes.jar`, not loose `.class` files.

### 9.2 `javac` mangles paths with spaces
**Symptom** `error: invalid flag: C:\AI`, then `Invalid filename: C:AI STUFF...`.
**Cause** PowerShell native-arg quoting, and `@response-file` splits on whitespace.
**Fix** one quoted argument string through `Start-Process -ArgumentList`.

### 9.3 `.ps1` parse error on a UTF-8 file
**Symptom** `The string is missing the terminator: "` pointing at a harmless line.
**Cause** PowerShell 5.1 reads BOM-less scripts as ANSI; an em dash became `â€"`.
**Fix** ASCII-only scripts, saved with UTF-8 BOM.

### 9.4 Cached playlists served raw
**Symptom** WABC opened, then "stream failed"; variant body contained unrewritten `../x.mp4`.
**Cause** the prefetcher cached **variant playlists** as opaque bytes; the cache hit bypassed
rewriting.
**Fix** never cache anything starting with `#EXTM3U`.

### 9.5 `hls.js` not found in the APK
**Symptom** endless spinner; logcat `AndroidProtocolHandler: Unable to open asset URL: file:///android_asset/hls.js`.
**Cause** HTML referenced `hls.js`, the asset is `hls.min.js`.
**Fix** corrected the `src`. A missing engine fails silently — always grep logcat.

### 9.6 Crash on launch (API 30+)
**Symptom** instant close; `NullPointerException … DecorView.getWindowInsetsController() on a null object reference`.
**Cause** `goImmersive()` called in `onCreate` **before** `setContentView`.
**Fix** call it after the content view, and wrap in `try/catch` — immersive is cosmetic.

### 9.7 Two-pane never triggered on the Fold
**Symptom** single-column list on a 2184 px wide device.
**Cause** breakpoint 900 px vs 842 dp of portrait inner screen.
**Fix** `@media (min-width:840px)`.

### 9.8 Language filter ate English channels
**Symptom** "Fox Sports 1" and "TVB Jade North America" dropped as non-English.
**Cause** `re.X` ignores whitespace, so `ort ` matched `Sp-ort-s` / `N-orth`.
**Fix** `\b` boundaries in every pattern.

### 9.9 ffprobe "cannot find resolution" on good feeds
**Symptom** 35 channels stuck at `live`.
**Cause** (a) ffprobe prints `codec,width,height` regardless of requested order;
(b) HLS demuxer rejects segments with no file extension.
**Fix** parse numeric fields; fall back to downloading a segment and probing it as MPEG-TS.

### 9.10 Server died when its console closed
**Symptom** Firefox "Unable to connect to 127.0.0.1:8777".
**Cause** `Start-Process` keeps the child in the parent's job object.
**Fix** spawn through `Win32_Process.Create` (`Start-Detached` in `start.ps1`) — verified
orphaned (`parent PID` no longer exists) and still serving.

---

## 10. Running it

### Desktop

```powershell
# one click
C:\AI STUFF\Local\tvapp.bat          # or the Desktop shortcut "tvapp"

# or explicitly
powershell -ExecutionPolicy Bypass -File .\start.ps1            # start / reuse
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Restart   # clean restart
.\stop-tvapp.cmd                                                # stop
```

Then http://127.0.0.1:8777 (the bat opens it as an app window).

### Android

```powershell
adb install -r release\tvapp.apk
adb shell am start -n com.tvapp.player/.MainActivity
```

### Rebuilding the catalogue

```powershell
python build.py --only us,uk,sports,news
python import_m3u.py --url https://raw.githubusercontent.com/ineed2underfit/hk-iptv/main/hk_live.m3u --group "Hong Kong" --out hk2.json
python import_m3u.py --url https://iptv-org.github.io/iptv/countries/us.m3u --group "US Cable" --out us_imp.json --workers 64
python finalize.py
python final_pass.py
python ..\android\build-apk.ps1        # only if the APK should carry the new list
```

---

## 11. Known limitations (measured, not guessed)

| Limitation | Detail |
|---|---|
| **HEVC in the browser/WebView** | TVB Jade 4K, TVB Pearl. Software-decode emulator fails; hardware phones usually pass. VLC button exists for the rest |
| **AC-3 audio** | Paramount Network carries AC-3 renditions; WebView has no decoder → use the Player button |
| **Referer-gated relays** | WebView cannot set per-request headers; those fail on Android but work on desktop (proxy injects the Referer) |
| **Latency** | floor is 2 × segment duration: ~12 s on 6 s segments, ~20 s on Pluto's 10 s segments. No LL-HLS anywhere in this set |
| **Relay churn** | IP-box relays die in weeks. `finalize.py` + `final_pass.py` regenerate; expect to run them monthly |
| **NY1 / NBC 4 / PIX 11** | no free public HLS exists — Spectrum login, 403 scraping, dead endpoints respectively |
| **UFC** | Paramount **Network** is free; UFC live events are on Paramount+, token-authenticated, no public HLS |

---

## 12. Repo layout

```
TVDS/
├─ SOP.md                    this document
├─ README.md                 quick start
├─ server.py                 proxy + static + /open VLC + /api/*
├─ index.html                desktop UI
├─ hls.min.js                hls.js 1.5.20 (vendored, offline-safe)
├─ channels.json             141 verified channels
├─ manifest.json, sw.js, icon-512.png
├─ start.ps1, tvapp.bat, stop-tvapp.cmd
├─ build.py, import_m3u.py, finalize.py, fix_heights.py, final_pass.py, refresh.py
├─ overrides.json            hand-picked streams that beat the harvest
├─ android/
│  ├─ AndroidManifest.xml
│  ├─ build-apk.ps1          aapt2 + javac + d8 + zipalign + apksigner
│  ├─ src/com/tvapp/player/MainActivity.java
│  └─ assets/index.html      mobile UI
└─ release/tvapp.apk         signed build (0.15 MB)
```

---

## 13. Playlist in, playlist out (the M3U workflow)

Everything a normal IPTV app understands, plus the same file in both directions.

### 13.1 Formats accepted

Standard extended M3U / M3U8:

```
#EXTM3U
#EXTINF:-1 tvg-id="BBCNews" tvg-name="BBC News" tvg-logo="https://..." group-title="UK",BBC News
https://example/bbc.m3u8
```

The parser in `android/assets/index.html` (`parseM3U`) handles:

- `#EXTINF` attributes in any order — `tvg-id`, `tvg-name`, `tvg-logo`, `group-title`
- the display name after the **last** comma (names containing commas survive)
- `#EXTGRP:` lines as a group fallback
- bare URL lines with no `#EXTINF` (name falls back to the last path segment)
- CRLF or LF, blank lines, unknown `#` directives
- de-duplication by URL

### 13.2 Import — three ways

| Route | How | Where it lands |
|---|---|---|
| **File** | ⤓ button → *Import playlist* → file picker | `localStorage['tvds.pls']` (raw text) |
| **URL** | same menu → *paste a playlist URL* | fetched with `fetch()`, then parsed |
| **Deep link** | `adb shell am start ... --es play "<url>"` | single stream, no list change |

Native side (`Bridge.pickPlaylist` → `MainActivity.openPlaylistPicker`):

```java
Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
i.addCategory(Intent.CATEGORY_OPENABLE);
i.setType("*/*");
i.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{
        "audio/x-mpegurl", "application/x-mpegurl", "application/vnd.apple.mpegurl",
        "text/plain", "text/*", "application/octet-stream"});
startActivityForResult(i, REQ_PLAYLIST);
```

The result is read as UTF-8, escaped by `jsString()`, and handed to the page:

```java
web.evaluateJavascript("window.tvImport(" + jsString(text) + ")", null);
```

`window.tvImport` parses, stores, flips the source chip to **Imported**, and re-renders.
The list survives app restarts because it lives in `localStorage`.

### 13.3 Export

*Export playlist* writes the current list (`#EXTM3U` + `#EXTINF` + URL per channel) and puts
it on the **clipboard** via `Bridge.copy`. Paste it into a file, into TiviMate / OTT Navigator,
or into another phone.

Command-line equivalent, from the repo:

```powershell
python make_m3u.py            # channels.json -> tvds.m3u (141 channels, 27 KB)
```

### 13.4 The file that ships in the repo

`tvds.m3u` — 141 channels with `tvg-id`, `tvg-name`, `tvg-logo`, `group-title`, and
`http-referrer` where the stream needs one. Open it directly in any IPTV player, or import it
into tvapp.

---

## 14. The player (what "a real player" ended up meaning)

### 14.1 Screen layout

```
list view                     player view
├─ header  tvds  [⤓] [⋯]       ├─ top   ‹  title  ☆  ⋯
├─ search                      ├─ video (zoomable)
├─ chips All/★Fav/Recent/Imported
└─ groups → channels           ├─ HUD  res · bitrate · latency · zoom
   (logo, name, note,          └─ bar  ☰ Channels | ⟳ orientation | HD quality | ⤢ Player
    badge, ☆)
```

### 14.2 Controls, and when they hide

- Tap the picture → controls appear, auto-hide after **6.5 s**.
- Tap again → hide immediately.
- Any control interaction calls `bumpUI()`, which re-arms the timer.
- Taps that **start on the control overlay are ignored by the toggle handler** — otherwise
  every button press would hide the sheet it just opened (§15.2).

### 14.3 Gestures

| Gesture | Action |
|---|---|
| Pinch (2 fingers) | zoom 1× → 4×, free scaling, clamped to the frame |
| Drag (1 finger, zoomed) | pan, clamped to the scaled bounds |
| Double tap | play / pause |
| Single tap | show / hide controls |
| Long press | suppressed (`contextmenu` prevented) — no browser link preview |

Zoom is a CSS transform on a wrapper around `<video>`:

```js
els.wrap.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + zoom + ')';
```

Pan limits are `(width × (zoom − 1)) / 2`, so the picture can never be dragged off-screen.
The HUD shows the live factor (`1.0x … 4.0x`).

### 14.4 Orientation

Three states, cycled from the bar or picked in the sheet: **Auto / Landscape / Portrait**.

```java
if ("landscape".equals(mode)) m.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
else if ("portrait".equals(mode)) m.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT);
else m.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
```

Fixed `LANDSCAPE`/`PORTRAIT` — **not** `SENSOR_*`. Sensor variants need an accelerometer,
so they do nothing on an emulator and nothing on a device lying flat on a table. The choice
persists in `localStorage['tvds.orient']` and is re-applied on next launch.

### 14.5 Quality

`MAX` pins `hls.currentLevel` to the best variant and re-pins on every `LEVEL_SWITCHED`;
`AUTO` hands control back to ABR (`currentLevel = -1`). Persisted as `tvds.qual`.

### 14.6 Favourites and recents

- ☆ on any row (or in the sheet) toggles a favourite; stored as `name|url` in `tvds.favs`.
- Every play pushes onto `tvds.recents` (last 30, de-duplicated).
- The chips at the top filter: All / ★ Favourites / Recent / Imported.

### 14.7 The ⋯ sheet

Quality, Orientation, *Open in another player* (VLC/MX via `Bridge.openExternal`),
*Add to favourites*, *Copy stream URL*, *Import playlist*, *Export playlist*, *Close player*.

### 14.8 Deep link

```powershell
adb shell am start -n com.tvapp.player/.MainActivity --es play "https://.../index.m3u8"
```

### 14.9 Self-test hook

`--es test 1` loads `index.html?selftest=1`, which after 2.5 s clicks the Landscape chip
programmatically and logs each step to logcat:

```
$ adb logcat -s tvapp:I
console LOG: ST chips=4
console LOG: ST seg=orient
console LOG: ST btn=landscape
setOrientation(landscape)
console LOG: ST after=landscape
```

This exists because scripted `adb input tap` kept missing the sheet while it was animating —
the self-test proves the JS → bridge → Java chain without depending on tap coordinates.

---

## 15. Bug log, second pass (player work)

### 15.1 `addJavascriptInterface` silently exposes nothing
**Symptom** Every button that needed native (`orientation`, `import`, `VLC`) did nothing.
No exception anywhere. `console.log` showed the handler running.
**Cause** the interface object was an instance of a **private inner class**. Newer WebView
builds skip non-public classes when they reflect over `@JavascriptInterface` methods.
**Fix** `Bridge` became a **public top-level class** holding a `WeakReference<MainActivity>`.
Diagnosis trick that found it in one shot:

```java
web.evaluateJavascript("(typeof Android) + '|' + Android.ping()", v -> Log.i(TAG, "bridge=" + v));
// before: "undefined"   after: "object|ok"
```

### 15.2 Taps on controls closed the controls
**Symptom** Opening the sheet and tapping a chip did nothing; the sheet vanished instead.
**Cause** the player's tap handler listened on the whole surface, so `touchend` from a button
ran `hideUI()` (which also closes the sheet) **before** the click was dispatched; the element
was already `pointer-events:none`.
**Fix** record whether the touch *started* inside `#ui` and skip the toggle in that case:

```js
tapInUI = !!(e.target.closest && e.target.closest('#ui'));
tapStart = tapInUI ? null : { x: ..., y: ..., t: Date.now() };
```

### 15.3 Long-press opened a URL preview over the video
**Symptom** Holding the picture showed a share/link chip with the stream URL.
**Cause** WebView's default link/text behaviour on a media element.
**Fix** `-webkit-touch-callout:none; user-select:none` on `#player`, plus
`document.addEventListener('contextmenu', preventDefault)` while the player is open.

### 15.4 `SCREEN_ORIENTATION_SENSOR_LANDSCAPE` never rotated
**Symptom** Landscape looked selected, display stayed portrait.
**Cause** sensor variants depend on the accelerometer; the emulator reports none.
**Fix** fixed `LANDSCAPE` / `PORTRAIT` for explicit choices, `UNSPECIFIED` for Auto.
`adb shell screencap` then reports `2400x1080`.

