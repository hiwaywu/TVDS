# TVDS

Live TV, sourced and verified by script, served locally, and shipped as an Android app.

116 channels — Hong Kong, New York locals, US cable, sports, news, UK, Canada — every one
checked with a segment download before it earned a slot. A local proxy handles the CORS and
Referer problems that make public relays unplayable in a browser. The Android app is a
0.15 MB signed APK built with the SDK tools alone (no Gradle, no Android Studio).

**Full write-up, including every bug and its fix: [SOP.md](SOP.md).**

---

## Desktop

```powershell
# one click
tvapp.bat                      # starts the detached server, opens an app window
# or
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Open <http://127.0.0.1:8777>. Keys: `↑`/`↓` channel, `f` fullscreen, `m` mute, `r` reload,
`a` toggle MAX/AUTO quality. The **VLC** button hands a stream to VLC for HEVC / AC-3 feeds.

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

Rebuild the APK after changing the channel list:

```powershell
cd android
powershell -ExecutionPolicy Bypass -File .\build-apk.ps1     # needs JDK 21 + Android SDK
```

## Refreshing the channel list

Relays rot. Rebuild the catalogue, then rebuild the APK if you want it on the phone:

```powershell
python build.py --only us,uk,sports,news
python import_m3u.py --url https://raw.githubusercontent.com/ineed2underfit/hk-iptv/main/hk_live.m3u --group "Hong Kong" --out hk2.json
python import_m3u.py --url https://iptv-org.github.io/iptv/countries/us.m3u --group "US Cable" --out us_imp.json --workers 64
python finalize.py
python final_pass.py
```

## Layout

| Path | What |
|---|---|
| `server.py` | proxy (playlist rewriting, CORS, Referer, 192 MB segment cache + read-ahead), `/api/*`, `/open` → VLC |
| `index.html` | desktop UI — hls.js, MAX quality lock, latency HUD |
| `channels.json` | the 116-channel catalogue |
| `build.py`, `import_m3u.py`, `finalize.py`, `fix_heights.py`, `final_pass.py` | harvest → verify → filter pipeline |
| `overrides.json` | hand-picked streams that beat the automatic pick |
| `android/` | manifest, `MainActivity`, mobile UI, `build-apk.ps1` |
| `release/tvapp.apk` | signed Android build |

## Sources

`iptv-org` catalogue and country playlists, the Pluto TV channel API, station pages
(`abc7ny.com/watch`, `cbsnews.com/newyork/live`) and the auto-updated
[hk-iptv](https://github.com/ineed2underfit/hk-iptv) playlist. Nothing here re-hosts a
stream — the player points at whatever the broadcaster or relay already serves.

No affiliation with any broadcaster. For personal use.
