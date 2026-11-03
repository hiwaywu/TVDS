#!/usr/bin/env python3
"""
expand.py - rebuild channels.json from a curated mainstream allowlist.

Unlike build.py (which trusts whatever iptv-org happens to list) this tool starts
from a list of the big English / Cantonese networks people actually watch, hunts
every candidate feed for those names across several playlists, probes each one,
and keeps the single best working copy per network.

  python expand.py                     # full rebuild + verify
  python expand.py --workers 64        # more parallel probes
  python expand.py --keep-existing     # keep a channel even if this run can't probe it
  python expand.py --dry-run           # report only, do not touch channels.json

Sources (all public, nothing re-hosted):
  * iptv-org country playlists  us / uk / ca / hk / zho / cn
  * iptv-org api streams        (per channel-id, for the names that matter)
  * hk-iptv                     (Cantonese: Jade / Pearl / TVB News / ViuTV / HOY / RTHK)
  * the current channels.json   (already-verified feeds keep their slot)
  * overrides.json              (hand-picked feeds always win)

Rules applied:
  * a stream must answer with a master playlist AND a >=20 KB segment, else dropped
  * >=720p unless --min-height says otherwise
  * H.264 preferred over HEVC (browsers cannot play HEVC in hls.js)
  * one entry per network: best copy wins, HEVC kept only when it is the only copy
  * radio / audio-only entries are never imported
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import UA, get, parse_master, first_segment  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache")
OUT = os.path.join(ROOT, "channels.json")
CACHE_JSON = os.path.join(CACHE, "probe_cache.json")
SEG_MIN = 20 * 1024
HLS_DIRECT = (".m3u8", ".m3u")

PLAYLISTS = {
    "us": "https://iptv-org.github.io/iptv/countries/us.m3u",
    "uk": "https://iptv-org.github.io/iptv/countries/uk.m3u",
    "ca": "https://iptv-org.github.io/iptv/countries/ca.m3u",
    "hk": "https://iptv-org.github.io/iptv/countries/hk.m3u",
    "zho": "https://iptv-org.github.io/iptv/languages/zho.m3u",
    "cn": "https://iptv-org.github.io/iptv/countries/cn.m3u",
    "hk-iptv": "https://raw.githubusercontent.com/ineed2underfit/hk-iptv/main/hk_live.m3u",
}
API_CH = "https://iptv-org.github.io/api/channels.json"
API_ST = "https://iptv-org.github.io/api/streams.json"

GROUP_ORDER = ["Hong Kong", "Cantonese", "New York", "US Cable", "Sports", "News", "UK", "Canada"]


# --------------------------------------------------------------------------- targets
# Each target: group, canonical display name, aliases (exact lowercase, or re:regex),
# and optional hand-found extras tried before the playlists.
def T(group, name, aliases, extra=(), regex=(), min_h=0):
    return {"group": group, "name": name, "aliases": [a.lower() for a in aliases],
            "regex": list(regex), "extra": list(extra), "min_h": min_h}


US = "US Cable"
SP = "Sports"
NW = "News"
UK = "UK"
CA = "Canada"
HK = "Hong Kong"
CT = "Cantonese"
NY = "New York"

TARGETS = [
    # ---------------------------------------------------------------- US news
    T(US, "CNN", ["cnn"], ["https://tvsen6.aynaott.com/cnn/index.m3u8",
                           "http://4.30.180.36:8420/cnn/index.m3u8?token=test",
                           "http://212.5.144.156:8080/cnn/tracks-v1a1/mono.m3u8"], [r"^cnn$"]),
    T(US, "HLN", ["hln"], ["http://23.237.104.106:8080/USA_HLN/tracks-v1a1/mono.m3u8"]),
    T(US, "CNN International", ["cnn international"],
      ["https://d3bp6dwmpbdajl.cloudfront.net/v1/master/3722c60a815c199d9c0ef36c5b73da68a62b09d1/cc-ury0meh5m4nzm/index.m3u8"]),
    T(US, "MSNBC", ["msnbc"], ["https://tvsen6.aynaott.com/msnbc/index.m3u8",
                               "http://4.30.180.36:8420/msnbc/index.m3u8?token=test"]),
    T(US, "Fox News Channel", ["fox news channel", "fox news", "fox news channel hd"],
      ["https://tvsen6.aynaott.com/foxnews/index.m3u8",
       "http://4.30.180.36:8420/foxnews/index.m3u8?token=test"]),
    T(US, "Fox Business Network", ["fox business network", "fox business"], regex=[r"^fox business"],
      extra=["https://tvsen6.aynaott.com/foxbusiness/index.m3u8"]),
    T(US, "CNBC", ["cnbc"], ["https://tvsen6.aynaott.com/cnbc/index.m3u8",
                             "http://4.30.180.36:8420/cnbc/index.m3u8?token=test",
                             "http://41.205.93.154/CNBC/tracks-v1a1/mono.m3u8"]),
    T(US, "Bloomberg TV", ["bloomberg tv", "bloomberg television"],
      ["http://23.237.104.106:8080/USA_BLOOMBERG/tracks-v1a1/mono.m3u8"]),
    T(US, "NewsNation", ["newsnation"], ["http://198.58.104.90:8989/newsnation/index.m3u8"]),
    T(US, "Newsmax TV", ["newsmax tv", "newsmax"]),
    T(US, "C-SPAN", ["c-span", "cspan"], regex=[r"^c-?span$"]),
    T(US, "Cheddar News", ["cheddar news", "cheddar"]),

    # ---------------------------------------------------------------- US sports
    T(SP, "ESPN", ["espn"], ["http://85.237.89.160:9590/usa-s/ESPN-HD/index.m3u8",
                             "http://4.30.180.36:8420/espn/index.m3u8?token=test"]),
    T(SP, "ESPN 2", ["espn 2", "espn2"], ["http://85.237.89.160:9590/usa-s/ESPN-2-HD/index.m3u8",
                                          "http://4.30.180.36:8420/espn2/index.m3u8?token=test"]),
    T(SP, "ESPNU", ["espnu"], ["http://85.237.89.160:9590/usa-s/ESPN-U-HD/index.m3u8",
                               "http://4.30.180.36:8420/espnu/index.m3u8?token=test"]),
    T(SP, "ESPN News", ["espn news", "espnews"], ["http://41.205.93.154/ESPNNEWS/tracks-v1a1/mono.m3u8"]),
    T(SP, "Fox Sports 1", ["fox sports 1", "fs1"], ["http://85.237.89.160:9590/usa-s/FOX-SPORTS-1/index.m3u8"]),
    T(SP, "Fox Sports 2", ["fox sports 2", "fs2"]),
    T(SP, "NBA TV", ["nba tv"], ["http://23.237.104.106:8080/USA_NBA/tracks-v1a1/mono.m3u8",
                                 "https://tvsen6.aynaott.com/nbatv/index.m3u8"]),
    T(SP, "MLB Network", ["mlb network", "mlb"]),
    T(SP, "NFL Network", ["nfl network"], ["http://198.58.104.90:8989/nfl/index.m3u8"]),
    T(SP, "NHL Network", ["nhl network"]),
    T(SP, "Golf Channel", ["golf channel"], ["https://tvsen6.aynaott.com/golfchannel/index.m3u8"]),
    T(SP, "Tennis Channel", ["tennis channel"], ["http://23.237.104.106:8080/USA_TENNIS/tracks-v1a1/mono.m3u8"]),
    T(SP, "Big Ten Network", ["big ten network"]),
    T(SP, "SEC Network", ["sec network"]),
    T(SP, "ACC Network", ["acc network"]),
    T(SP, "CBS Sports Network", ["cbs sports network"]),
    T(SP, "CBS Sports HQ", ["cbs sports hq"]),
    T(SP, "CBS Sports Golazo Network", ["cbs sports golazo network"]),

    # ---------------------------------------------------------------- US entertainment
    T(US, "TBS", ["tbs"], ["https://tvsen6.aynaott.com/tbs/index.m3u8",
                           "http://4.30.180.36:8420/tbs/index.m3u8?token=test"]),
    T(US, "TNT", ["tnt"], ["http://212.5.144.156:8080/tnt/tracks-v1a1/mono.m3u8"]),
    T(US, "truTV", ["trutv"], ["http://23.237.104.106:8080/USA_TRUTV/tracks-v1a1/mono.m3u8"]),
    T(US, "USA Network", ["usa network"], ["https://tvsen6.aynaott.com/usa/index.m3u8",
                                           "http://4.30.180.36:8420/usa/index.m3u8?token=test"]),
    T(US, "FX", ["fx"], ["https://tvsen7.aynascope.net/fx/index.m3u8",
                         "http://4.30.180.36:8420/fx/index.m3u8?token=test"]),
    T(US, "FXX", ["fxx"]),
    T(US, "FX Movie Channel", ["fx movie channel", "fxm"]),
    T(US, "SYFY", ["syfy"], ["http://23.237.104.106:8080/USA_SYFY/tracks-v1a1/mono.m3u8"]),
    T(US, "AMC", ["amc"], ["http://23.239.31.26:8989/amc/index.m3u8",
                           "https://tvsen6.aynaott.com/amc/index.m3u8"]),
    T(US, "BBC America", ["bbc america"], ["http://23.239.31.26:8989/bbcamerica/index.m3u8"]),
    T(US, "IFC", ["ifc"], ["http://23.237.104.106:8080/USA_IFC/tracks-v1a1/mono.m3u8"]),
    T(US, "Sundance TV", ["sundance tv", "sundance"]),
    T(US, "Freeform", ["freeform"], ["http://4.30.180.36:8420/freeform/index.m3u8?token=test"]),
    T(US, "HBO", ["hbo"], ["https://tvsen7.aynascope.net/hbo/index.m3u8"]),
    T(US, "HBO 2", ["hbo 2", "hbo2"], ["https://tvsen7.aynascope.net/hbo2/index.m3u8"]),
    T(US, "HBO Family", ["hbo family"], ["http://23.237.104.106:8080/USA_HBO_FAMILY/tracks-v1a1/mono.m3u8"]),
    T(US, "Cinemax", ["cinemax"]),
    T(US, "Showtime", ["showtime"], ["http://4.30.180.36:8420/showtime/index.m3u8?token=test"]),
    T(US, "Showtime Family Zone", ["showtime family zone"]),
    T(US, "Starz", ["starz"], ["http://4.30.180.36:8420/starz/index.m3u8?token=test"]),
    T(US, "Starz Comedy", ["starz comedy"], ["http://23.237.104.106:8080/USA_STARZ_COMEDY/tracks-v1a1/mono.m3u8"]),
    T(US, "Starz Cinema", ["starz cinema"], ["http://4.30.180.36:8420/starzcinema/index.m3u8?token=test"]),
    T(US, "Starz Kids & Family", ["starz kids & family", "starz kids and family"]),
    T(US, "Paramount Network", ["paramount network"], ["http://4.30.180.36:8420/paramount/index.m3u8?token=test"]),
    T(US, "Comedy Central", ["comedy central"], ["http://4.30.180.36:8420/comedycentral/index.m3u8?token=test"]),
    T(US, "MTV", ["mtv"], ["http://23.237.104.106:8080/USA_MTV/tracks-v1a1/mono.m3u8",
                           "https://tvsen6.aynaott.com/mtv/index.m3u8"]),
    T(US, "MTV2", ["mtv2", "mtv 2"]),
    T(US, "MTV Live", ["mtv live"]),
    T(US, "VH1", ["vh1"], ["http://23.237.104.106:8080/USA_VH1/tracks-v1a1/mono.m3u8"]),
    T(US, "BET", ["bet"]),
    T(US, "CMT", ["cmt"], ["http://23.237.104.106:8080/USA_CMT/tracks-v1a1/mono.m3u8"]),
    T(US, "TV Land", ["tv land"]),
    T(US, "Discovery Channel", ["discovery channel", "discovery"],
      ["http://212.5.144.156:8080/discovery/tracks-v1a1/mono.m3u8",
       "http://4.30.180.36:8420/discovery/index.m3u8?token=test"]),
    T(US, "TLC", ["tlc"], ["http://4.30.180.36:8420/tlc/index.m3u8?token=test"]),
    T(US, "Animal Planet", ["animal planet"],
      ["http://23.237.104.106:8080/USA_ANIMAL_PLANET/tracks-v1a1/mono.m3u8",
       "http://23.239.31.26:8989/animalplanet/index.m3u8"]),
    T(US, "Science Channel", ["science channel", "science"],
      ["http://23.237.104.106:8080/USA_SCIENCE/tracks-v1a1/mono.m3u8"]),
    T(US, "Investigation Discovery", ["investigation discovery"],
      ["http://212.5.144.156:8080/id/tracks-v1a1/mono.m3u8",
       "http://4.30.180.36:8420/investigationdiscovery/index.m3u8?token=test"]),
    T(US, "Destination America", ["destination america"]),
    T(US, "Cooking Channel", ["cooking channel"]),
    T(US, "Food Network", ["food network"],
      ["http://23.237.104.106:8080/USA_FOOD_NETWORK/tracks-v1a1/mono.m3u8",
       "http://212.5.144.156:8080/foodnetwork/tracks-v1a1/mono.m3u8"]),
    T(US, "HGTV", ["hgtv"], ["https://tvsen7.aynascope.net/hgtv/index.m3u8",
                             "http://4.30.180.36:8420/hgtv/index.m3u8?token=test"]),
    T(US, "Travel Channel", ["travel channel"]),
    T(US, "History", ["history", "history channel"],
      ["http://212.5.144.156:8080/history/tracks-v1a1/mono.m3u8",
       "http://4.30.180.36:8420/history/index.m3u8?token=test"]),
    T(US, "A&E", ["a&e", "aande"], ["http://23.239.31.26:8989/aande/index.m3u8"]),
    T(US, "Lifetime", ["lifetime"], ["http://4.30.180.36:8420/lifetime/index.m3u8?token=test"]),
    T(US, "LMN", ["lmn", "lifetime movie network", "lifetime movies"]),
    T(US, "Oxygen True Crime", ["oxygen true crime", "oxygen"]),
    T(US, "WE tv", ["we tv", "wetv"]),
    T(US, "OWN", ["own"], ["http://23.237.104.106:8080/USA_OWN/tracks-v1a1/mono.m3u8"]),
    T(US, "Bravo", ["bravo"], ["http://41.205.93.154/BRAVO/tracks-v1a1/mono.m3u8",
                               "http://4.30.180.36:8420/bravo/index.m3u8?token=test"]),
    T(US, "E!", ["e!"], ["http://23.237.104.106:8080/USA_E/tracks-v1a1/mono.m3u8"]),
    T(US, "Hallmark Channel", ["hallmark channel", "hallmark"],
      ["http://41.205.93.154/HALLMARK/tracks-v1a1/mono.m3u8",
       "http://4.30.180.36:8420/hallmark/index.m3u8?token=test"]),
    T(US, "Hallmark Movies & Mysteries", ["hallmark movies & mysteries", "hallmark mystery"]),
    T(US, "National Geographic", ["national geographic"],
      ["http://212.5.144.156:8080/natgeo/tracks-v1a1/mono.m3u8",
       "https://tvsen6.aynaott.com/natgeo/index.m3u8"]),
    T(US, "Nat Geo Wild", ["nat geo wild", "national geographic wild"]),
    T(US, "Cartoon Network", ["cartoon network"],
      ["http://212.5.144.156:8080/cartoon/tracks-v1a1/mono.m3u8",
       "http://4.30.180.36:8420/cartoon/index.m3u8?token=test"]),
    T(US, "Adult Swim", ["adult swim"]),
    T(US, "Nickelodeon", ["nickelodeon"], ["http://198.58.104.90:8989/nickelodeon/index.m3u8"]),
    T(US, "Nick Jr.", ["nick jr.", "nick jr"], ["http://212.5.144.156:8080/nickjr/tracks-v1a1/mono.m3u8"]),
    T(US, "Disney Channel", ["disney channel"],
      ["http://4.30.180.36:8420/disneychannel/index.m3u8?token=test"]),
    T(US, "Disney Junior", ["disney junior"],
      ["http://212.5.144.156:8080/disneyjr/tracks-v1a1/mono.m3u8"]),
    T(US, "Disney XD", ["disney xd"]),
    T(US, "PBS", ["pbs"], regex=[r"^pbs$"]),
    T(US, "PBS Kids", ["pbs kids"], regex=[r"^pbs kids$"]),
    T(US, "The Weather Channel", ["the weather channel", "weather channel"]),
    T(US, "AccuWeather NOW", ["accuweather now", "accuweather"]),
    T(US, "Court TV", ["court tv"]),
    T(US, "Reelz", ["reelz"], ["http://23.237.104.106:8080/USA_REELZ/tracks-v1a1/mono.m3u8"]),
    T(US, "AXS TV", ["axs tv", "axs"], ["http://23.239.31.26:8989/axstv/index.m3u8"]),
    T(US, "Revolt", ["revolt"]),
    T(US, "Bounce", ["bounce"]),
    T(US, "American Heroes Channel", ["american heroes channel", "american heroes"],
      ["http://23.237.104.106:8080/USA_AMERICAN_HEROES/index.m3u8"]),
    T(US, "Comet", ["comet"], ["https://fast-channels.sinclairstoryline.com/COMET/index.m3u8"]),
    T(US, "Grit", ["grit", "grit xtra"]),
    T(US, "MeTV", ["metv"]),
    T(US, "Cozi TV", ["cozi tv"]),
    T(US, "ION", ["ion", "ion television", "ion tv"]),

    # ---------------------------------------------------------------- New York locals
    T(NY, "ABC 7 WABC (New York)", ["abc 7 wabc", "abc 7", "wabc"], regex=[r"^abc 7\b"]),
    T(NY, "CBS 2 New York", ["cbs 2 new york", "wcbs", "cbs 2"], regex=[r"^cbs 2\b"]),
    T(NY, "NBC 4 New York", ["nbc 4 new york", "wnbc", "nbc 4"], regex=[r"^nbc 4\b"]),
    T(NY, "Fox 5 WNYW (New York)", ["fox 5 wnyw", "wnyw", "fox 5"], regex=[r"^fox 5\b"]),
    T(NY, "PIX 11 (New York)", ["pix 11", "wpix"], regex=[r"^pix 11\b"]),
    T(NY, "News 12 New York", ["news 12 new york", "news 12"]),
    T(NY, "CBS News New York", ["cbs news new york"]),

    # ---------------------------------------------------------------- national news
    T(NW, "ABC News Live", ["abc news live"]),
    T(NW, "NBC News NOW", ["nbc news now"]),
    T(NW, "CBS News 24/7", ["cbs news 24/7", "cbs news"]),
    T(NW, "Scripps News", ["scripps news"]),
    T(NW, "Bloomberg Originals", ["bloomberg originals"]),
    T(NW, "Yahoo Finance", ["yahoo finance"]),
    T(NW, "Al Jazeera English", ["al jazeera english", "al jazeera"]),
    T(NW, "DW English", ["dw english"],
      extra=["https://dwamdstream103.akamaized.net/hls/live/2015526/dwstream103/index.m3u8"]),
    T(NW, "France 24 English", ["france 24 english", "france 24"]),
    T(NW, "Euronews English", ["euronews english", "euronews"]),
    T(NW, "Sky News", ["sky news"], ["https://linear417-gb-hls1-prd-ak.cdn.skycdp.com/100e/Content/HLS_001_6_2/Live/channel(skynews)/index.m3u8"]),
    T(NW, "Sky News Australia", ["sky news australia"]),
    T(NW, "ABC News (Australia)", ["abc news (australia)", "abc news australia"]),
    T(NW, "CGTN", ["cgtn"]),
    T(NW, "NHK World Japan", ["nhk world japan", "nhk world"]),
    T(NW, "WION", ["wion"]),
    T(NW, "NDTV 24x7", ["ndtv 24x7"]),
    T(NW, "TRT World", ["trt world"]),
    T(NW, "Channel NewsAsia", ["channel newsasia", "cna"]),

    # ---------------------------------------------------------------- UK
    T(UK, "BBC One", ["bbc one"], regex=[r"^bbc one\b"]),
    T(UK, "BBC Two", ["bbc two"], regex=[r"^bbc two\b"]),
    T(UK, "BBC Three", ["bbc three"], regex=[r"^bbc three\b"]),
    T(UK, "BBC Four", ["bbc four"], regex=[r"^bbc four\b"]),
    T(UK, "BBC News", ["bbc news", "bbc news uk"], regex=[r"^bbc news\b"]),
    T(UK, "BBC Scotland", ["bbc scotland"]),
    T(UK, "CBBC", ["cbbc"]),
    T(UK, "CBeebies", ["cbeebies"]),
    T(UK, "ITV1", ["itv1", "itv 1", "itv"], regex=[r"^itv ?1?$"]),
    T(UK, "ITV2", ["itv2", "itv 2"]),
    T(UK, "ITV3", ["itv3", "itv 3"]),
    T(UK, "ITV4", ["itv4", "itv 4"]),
    T(UK, "ITVBe", ["itvbe", "itv be"]),
    T(UK, "Channel 4", ["channel 4"], regex=[r"^channel 4\b"]),
    T(UK, "E4", ["e4"]),
    T(UK, "Film4", ["film4"]),
    T(UK, "More4", ["more4"]),
    T(UK, "4seven", ["4seven", "4 seven"]),
    T(UK, "Channel 5", ["channel 5"], regex=[r"^channel 5\b"]),
    T(UK, "5USA", ["5usa"]),
    T(UK, "5STAR", ["5star"]),
    T(UK, "5Action", ["5action"]),
    T(UK, "Sky News Weather", ["sky news weather"]),
    T(UK, "Sky Sports News", ["sky sports news"]),
    T(UK, "Sky Sports Main Event", ["sky sports main event"]),
    T(UK, "Sky Sports Premier League", ["sky sports premier league"]),
    T(UK, "Sky Sports Football", ["sky sports football"]),
    T(UK, "Sky Sports Cricket", ["sky sports cricket"]),
    T(UK, "Sky Sports F1", ["sky sports f1"]),
    T(UK, "Sky Sports Golf", ["sky sports golf"]),
    T(UK, "Sky Arts", ["sky arts"]),
    T(UK, "Sky Atlantic", ["sky atlantic"]),
    T(UK, "Sky Max", ["sky max"]),
    T(UK, "Sky Witness", ["sky witness"]),
    T(UK, "Sky Crime", ["sky crime"]),
    T(UK, "Sky Comedy", ["sky comedy"]),
    T(UK, "Sky Documentaries", ["sky documentaries"]),
    T(UK, "Sky History", ["sky history"]),
    T(UK, "Sky Nature", ["sky nature"]),
    T(UK, "TNT Sports 1", ["tnt sports 1"]),
    T(UK, "TNT Sports 2", ["tnt sports 2"]),
    T(UK, "TNT Sports 3", ["tnt sports 3"]),
    T(UK, "TNT Sports 4", ["tnt sports 4"]),
    T(UK, "Premier Sports 1", ["premier sports 1"]),
    T(UK, "Dave", ["dave"]),
    T(UK, "Gold", ["gold"]),
    T(UK, "Alibi", ["alibi", "u&alibi"]),
    T(UK, "Drama", ["drama"]),
    T(UK, "Really", ["really"]),
    T(UK, "Quest", ["quest"]),
    T(UK, "Blaze", ["blaze"]),
    T(UK, "GB News", ["gb news"]),
    T(UK, "TalkTV", ["talktv", "talk tv"]),
    T(UK, "LBC News", ["lbc news"]),
    T(UK, "Sky Cinema Premiere", ["sky cinema premiere"]),
    T(UK, "BBC Food", ["bbc food"], ["https://d1e9r0b71zfwk7.cloudfront.net/playlist.m3u8"]),
    T(UK, "BBC Home & Garden", ["bbc home & garden", "bbc home"],
      ["https://d11r33s5i066xh.cloudfront.net/playlist.m3u8"]),

    # ---------------------------------------------------------------- Canada
    T(CA, "CTV", ["ctv"], regex=[r"^ctv( |$)"] ),
    T(CA, "CTV 2", ["ctv 2"]),
    T(CA, "CTV News Channel", ["ctv news channel", "ctv news"]),
    T(CA, "CP24", ["cp24"], ["https://tvsen6.aynaott.com/cp24/index.m3u8"]),
    T(CA, "CTV Life Channel", ["ctv life channel"]),
    T(CA, "Global", ["global", "global tv", "global television"]),
    T(CA, "Global News", ["global news"]),
    T(CA, "CityNews Toronto", ["citynews toronto"]),
    T(CA, "CityNews Vancouver", ["citynews vancouver"]),
    T(CA, "CityNews Calgary", ["citynews calgary"]),
    T(CA, "CityNews Edmonton", ["citynews edmonton"]),
    T(CA, "CBC", ["cbc"], regex=[r"^cbc television", r"^cbc$"]),
    T(CA, "CBC News Network", ["cbc news network"]),
    T(CA, "TSN 1", ["tsn1", "tsn 1"]),
    T(CA, "TSN 2", ["tsn2", "tsn 2"]),
    T(CA, "TSN 3", ["tsn3", "tsn 3"]),
    T(CA, "TSN 4", ["tsn4", "tsn 4"]),
    T(CA, "TSN 5", ["tsn5", "tsn 5"]),
    T(CA, "Sportsnet", ["sportsnet"], regex=[r"^sportsnet( |$)"]),
    T(CA, "Sportsnet 360", ["sportsnet 360"]),
    T(CA, "Sportsnet One", ["sportsnet one"]),
    T(CA, "Much", ["much"]),
    T(CA, "Crave", ["crave"], regex=[r"^crave( |$)"]),
    T(CA, "Showcase", ["showcase"]),
    T(CA, "W Network", ["w network"]),
    T(CA, "Slice", ["slice"]),
    T(CA, "HGTV Canada", ["hgtv canada"], regex=[r"^hgtv \(canada\)"]),
    T(CA, "Food Network Canada", ["food network canada"], regex=[r"^food network \(canada\)"]),
    T(CA, "History Canada", ["history canada"], regex=[r"^history \(canada\)"]),
    T(CA, "Discovery Canada", ["discovery canada"], regex=[r"^discovery \(canada\)"]),
    T(CA, "Cottage Life", ["cottage life"]),
    T(CA, "Love Nature", ["love nature"]),
    T(CA, "Treehouse", ["treehouse"]),
    T(CA, "YTV", ["ytv"]),
    T(CA, "Family Channel", ["family channel"]),
    T(CA, "BNN Bloomberg", ["bnn bloomberg"]),
    T(CA, "The Weather Network", ["the weather network", "weather network"]),
    T(CA, "CPAC", ["cpac"], regex=[r"^cpac english"]),

    # ---------------------------------------------------------------- Hong Kong
    T(HK, "TVB Jade", ["tvb jade", "jade", "翡翠台", "無綫翡翠台", "无线翡翠台"],
      regex=[r"^jade$"], extra=[]),
    T(HK, "TVB Plus", ["tvb plus", "tvbplus", "j2", "無綫plus", "tvb j2"]),
    T(HK, "TVB News", ["tvb news", "無綫新聞", "无线新闻", "無綫新聞台", "无线新闻台"],
      regex=[r"^news ?台?$"]),
    T(HK, "TVB Pearl", ["tvb pearl", "pearl", "明珠台"], regex=[r"^pearl$"]),
    T(HK, "TVB Finance & Sports", ["tvb finance & sports", "tvb finance", "無綫財經", "无线财经"]),
    T(HK, "TVB Jade North America", ["tvb jade north america", "翡翠台北美版"]),
    T(HK, "ViuTV", ["viutv", "viu tv", "viutv (1080i)"]),
    T(HK, "ViuTVsix", ["viutvsix", "viu tv six", "viutv six"]),
    T(HK, "HOY TV 77", ["hoy tv", "hoy tv 77", "hoy77", "hoy"],
      regex=[r"^hoy ?tv$"], extra=["https://hoytv-live-stream.hoy.tv/ch77/index.m3u8"]),
    T(HK, "HOY Info 78", ["hoy infotainment", "hoy info 78", "hoy78", "hoy 78"],
      extra=["https://hoytv-live-stream.hoy.tv/ch78/index.m3u8"]),
    T(HK, "HOY IBC 76", ["hoy ibc", "hoy76", "hoy 76"]),
    T(HK, "RTHK TV 31", ["rthk tv 31", "rthk31", "港台電視31", "港台电视31"],
      regex=[r"^rthk ?31$"], extra=["https://rthktv31-live.akamaized.net/hls/live/2036818/RTHKTV31/master.m3u8"]),
    T(HK, "RTHK TV 32", ["rthk tv 32", "rthk32", "港台電視32", "港台电视32"],
      regex=[r"^rthk ?32$"], extra=["https://rthktv32-live.akamaized.net/hls/live/2036819/RTHKTV32/master.m3u8"]),
    T(HK, "RTHK TV 33", ["rthk tv 33", "rthk33", "港台電視33", "港台电视33"],
      regex=[r"^rthk ?33$"]),
    T(HK, "RTHK TV 34", ["rthk tv 34", "rthk34", "港台電視34", "港台电视34"],
      regex=[r"^rthk ?34$"]),
    T(HK, "RTHK TV 35", ["rthk tv 35", "rthk35", "港台電視35", "港台电视35"],
      regex=[r"^rthk ?35$"]),
    T(HK, "i-CABLE News", ["i-cable news", "icable news", "有線新聞", "有线新闻"]),
    T(HK, "now News", ["now news", "now新聞", "now 新聞"]),
    T(HK, "Phoenix Hong Kong", ["phoenix hong kong", "鳳凰衛視香港台", "凤凰卫视香港台"]),
    T(HK, "TVB Entertainment News", ["tvb entertainment news", "無綫娛樂新聞"]),
    T(HK, "Celestial Movies", ["celestial movies", "天映頻道", "天映频道"]),

    # ---------------------------------------------------------------- Cantonese (Guangdong)
    T(CT, "GDTV Pearl River", ["gdtv pearl river", "广东珠江", "廣東珠江", "珠江台", "珠江频道"]),
    T(CT, "GZTV General", ["gztv general", "广州综合", "廣州綜合", "广州台"]),
    T(CT, "TVS-2 South", ["tvs-2 south", "南方卫视", "南方衛視", "tvs2"]),
    T(CT, "GD Sports", ["gd sports", "广东体育", "廣東體育"]),
    T(CT, "GD News", ["gd news", "广东新闻", "廣東新聞"]),
    T(CT, "Shenzhen TV", ["shenzhen tv", "深圳卫视", "深圳衛視"]),
    T(CT, "Macau Asia", ["macau asia", "澳亚卫视", "澳亞衛視"]),
    T(CT, "TDM Ou Mun", ["tdm ou mun", "澳门澳视", "澳視澳門", "澳门卫视"]),
]

# HK relay token guesses: cheap to try, they carry the TVB / ViuTV / HOY feeds.
HK_TOKEN_HOSTS = [
    "https://fast.hidns.vip/163189/{t}",
    "https://cdn6.cc.cd/163189/{t}",
    "https://10.fast.hidns.vip/163189/{t}/video/index.php",
    "http://php.jdshipin.com:8880/TVOD/iptv.php?id={t}",
]

HK_EXTRAS = {
    "TVB Jade": ["fct", "j1"],
    "TVB Plus": ["j2", "plus"],
    "TVB News": ["wxxw"],
    "TVB Pearl": ["mzt"],
    "TVB Finance & Sports": ["cwj", "fin"],
    "ViuTV": ["viu"],
    "ViuTVsix": ["viu6"],
    "HOY TV 77": ["hoy"],
    "HOY Info 78": ["hoy78"],
    "HOY IBC 76": ["hoy76"],
    "RTHK TV 33": ["rthk33"],
    "i-CABLE News": ["icable"],
    "now News": ["now", "nownews"],
}


def hk_urls(tokens):
    return [h.format(t=t) for h in HK_TOKEN_HOSTS for t in tokens]


for _t in TARGETS:
    _tok = HK_EXTRAS.get(_t["name"])
    if _tok:
        _t["extra"] = hk_urls(_tok) + _t["extra"]

RADIO = re.compile(r"""(?ix)
  \bradio\b|\bfm\b|\.mp3(\?|$)|\baudio\b|\bpodcast\b|^rthk\s*radio|
  \bradio\s*\d|classic\s*fm|bbc\s*radio|absolute\s*radio|heart\s*\d|capital\s*fm
""")

# hosts that have been serving this catalogue reliably for months; a 720p feed on
# one of them beats a 1080p feed on a random box that will be gone next week
TRUSTED = re.compile(r"""(?ix)
  amagi\.tv|akamaized\.net|cloudfront\.net|pluto\.tv|jmp2\.uk|wurl\.com|
  cbsivideo\.com|pbs\.org|pbskids\.org|turnerlive\.warnermediacdn\.com|
  uplynk\.com|klowdtv\.com|nhl|twc-tv\.com|bbc|skycdp\.com|hoy\.tv|
  23\.237\.104\.106|212\.5\.144\.156|85\.237\.89\.160|23\.239\.31\.26|
  198\.58\.104\.90|4\.30\.180\.36|41\.205\.93\.154|tvsen6\.aynaott\.com|
  tvsen7\.aynascope\.net|tvsen5\.aynascope\.net|hidns\.vip|cc\.cd|jdshipin\.com|
  163189\.(?:xyz|vip)|indevs\.in|kylintv\.tv|airspace-cdn\.cbsivideo\.com|
  citynewsregional\.akamaized\.net|simplestreamcdn\.com|cbsnews\.com
""")


def trusted(url):
    return 1 if TRUSTED.search(url or "") else 0


def brand_hint(name, url):
    """1 when the feed URL itself carries the channel name (espn-2-hd, /cnn/, ...)."""
    key = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return 1 if key and key in re.sub(r"[^a-z0-9]", "", (url or "").lower()) else 0


# feeds tagged with a non-English language in the path (film4_hun, euronews-pl ...)
# are the wrong audio track - the catalogue is English / Cantonese only
BAD_LANG = set("""
hun rom ron pol cze ces slo slk rus tur ara per fas hin urd ben tam tel mal kan kor jpn
tha vie ind msa fil deu ger fra fre ita spa por nld dut swe nor dan fin ell gre heb ukr
srp hrv bul lit lav kat aze kaz uzb bel alb mkd sqi
pl ro cz sk hu ru tr ar fa hi ur bn ta te ml kn ko ja th vi ms de fr es pt nl sv da
fi el he hr bg lt lv et
""".split())

LANG_TOKEN = re.compile(r"(?i)(?:^|[_\-/])([a-z]{2,3})(?=[_\-./]|$)")


def wrong_language(url):
    """True when the feed path is tagged with a language other than English."""
    path = (url or "").split("?")[0]
    path = re.sub(r"^[a-z]+://[^/]+", "", path, flags=re.I)
    for m in LANG_TOKEN.finditer(path):
        if m.group(1).lower() in BAD_LANG:
            return True
    return False

QUALITY_TAG = re.compile(r"""(?ix)
  \s*\((?:1080p?|720p?|576p?|540p?|480p?|360p?|2160p?|4k|uhd|hd|sd|fhd|hq|1080i|720i|576i|360i)\)|
  \s*\[(?:not 24/7|geo-?blocked|24/7|1080p?|720p?|4k|hd|sd|backup)\]
""")


def norm(name):
    n = QUALITY_TAG.sub(" ", name or "")
    n = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", n)          # drop every parenthetical
    n = re.sub(r"(?i)\s+(hd|sd|fhd|uhd|4k|1080p|720p)$", "", n)
    n = re.sub(r"\s+", " ", n).strip(" -|·")
    return n.lower()


def parse_playlist(text):
    out, name, grp, logo = [], None, None, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r",([^,]*)$", line)
            name = m.group(1).strip() if m else ""
            g = re.search(r'group-title="([^"]*)"', line)
            grp = g.group(1) if g else ""
            lg = re.search(r'logo="([^"]+)"', line)
            logo = lg.group(1) if lg else None
        elif not line.startswith("#") and name:
            out.append({"name": name, "url": line, "group": grp, "logo": logo})
            name = None
    return out


# --------------------------------------------------------------------------- probe
def probe_stream(url, referer=None, min_height=720):
    """-> dict(ok, h, bw, codec, final, err). H.264 variant preferred at equal height."""
    if RADIO.search(url):
        return {"ok": False, "err": "radio"}
    try:
        r = get(url, referer)
        body = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
        final = r.geturl()
    except Exception as exc:
        return {"ok": False, "err": str(exc)[:60]}
    if "#EXTM3U" not in body:
        return {"ok": False, "err": "not m3u8"}

    variants = parse_master(body, final)
    codec = "?"
    if variants:
        # parse codecs ourselves: parse_master drops the CODECS attribute
        rows = _variants(body, final)
        good = [v for v in rows if not v["h"] or v["h"] >= min_height] or rows
        avc = sorted([v for v in good if v["avc"]], key=lambda v: (v["h"], v["bw"]), reverse=True)
        hevc = sorted([v for v in good if not v["avc"]], key=lambda v: (v["h"], v["bw"]), reverse=True)
        # H.264 first: hls.js cannot decode HEVC. Only fall back when no AVC copy clears the bar.
        pick = (avc or hevc)[0]
        codec = "avc" if pick["avc"] else "hevc"
        h, bw, seg_url = pick["h"], pick["bw"], None
        try:
            vr = get(pick["url"], referer)
            vbody = vr.read(4 * 1024 * 1024).decode("utf-8", "replace")
            seg_url = first_segment(vbody, vr.geturl())
        except Exception as exc:
            return {"ok": False, "err": "variant: " + str(exc)[:50]}
        if not seg_url:
            return {"ok": False, "err": "no segment"}
    else:
        h, bw = 0, 0
        seg_url = first_segment(body, final)
        if not seg_url:
            return {"ok": False, "err": "no segment"}

    try:
        s = get(seg_url, referer)
        n = len(s.read(SEG_MIN * 2))
    except Exception as exc:
        return {"ok": False, "err": "segment: " + str(exc)[:50]}
    if n < SEG_MIN:
        return {"ok": False, "err": "segment small (%d B)" % n}
    return {"ok": True, "h": h, "bw": bw, "codec": codec, "final": url, "err": ""}


def _variants(body, base):
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
                       "avc": not ("hvc1" in codecs or "hev1" in codecs or "h265" in codecs)}
        elif line and not line.startswith("#") and pending:
            pending["url"] = urllib.parse.urljoin(base, line)
            rows.append(pending)
            pending = None
    return rows


# --------------------------------------------------------------------------- sources
def fetch_text(url, name):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        return open(path, encoding="utf-8", errors="replace").read()
    try:
        text = get(url).read().decode("utf-8", "replace")
    except Exception as exc:
        if os.path.exists(path):
            print("  ! %s unreachable (%s), using cached copy" % (name, str(exc)[:50]))
            return open(path, encoding="utf-8", errors="replace").read()
        print("  ! %s unreachable: %s" % (name, str(exc)[:60]))
        return ""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return text


def collect_candidates():
    cands = []
    for key, url in PLAYLISTS.items():
        text = fetch_text(url, key + ".m3u")
        for e in parse_playlist(text):
            e["src"] = key
            cands.append(e)
    # api streams, only for channel names that can match a target
    try:
        chans = json.loads(fetch_text(API_CH, "api_channels.json"))
        streams = json.loads(fetch_text(API_ST, "api_streams.json"))
    except Exception:
        chans, streams = [], []
    by_id = {c["id"]: c for c in chans}
    wanted = {a for t in TARGETS for a in t["aliases"]}
    names = {}
    for c in chans:
        if norm(c.get("name") or "") in wanted:
            names[c["id"]] = c.get("name") or c["id"]
    for s in streams:
        cid = s.get("channel")
        if cid in names and s.get("url"):
            cands.append({"name": names[cid], "url": s["url"], "group": "",
                          "logo": None, "referer": s.get("referer"), "src": "api"})
    return cands


def load_existing(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    out = []
    for g in data.get("groups", []):
        for c in g.get("channels", []):
            if c.get("url"):
                out.append({"name": c["name"], "url": c["url"], "group": g["name"],
                            "logo": c.get("logo"), "referer": c.get("referer"),
                            "src": "existing", "height": c.get("height") or 0,
                            "badge": c.get("badge"), "note": c.get("note")})
    return out


def match_targets(cands):
    """-> {target index: [(candidate, referer)]}"""
    hits = {i: [] for i in range(len(TARGETS))}
    for c in cands:
        nm = norm(c["name"])
        if not nm:
            continue
        for i, t in enumerate(TARGETS):
            if nm in t["aliases"] or nm == norm(t["name"]):
                hits[i].append(c)
            else:
                for rx in t["regex"]:
                    if re.search(rx, nm, re.I):
                        hits[i].append(c)
                        break
    return hits


def load_overrides():
    """overrides.json: hand-pinned feeds, keyed by lowercased channel name."""
    try:
        with open(os.path.join(ROOT, "overrides.json"), encoding="utf-8") as fh:
            rows = json.load(fh)
    except Exception:
        return {}
    return {r["name"].strip().lower(): r["url"] for r in rows if r.get("name") and r.get("url")}


def build_target_urls(cands, existing_only=False):
    """candidate list per target: overrides + extras + existing + playlist hits"""
    hits = match_targets(cands)
    ov = load_overrides()
    plan = {}
    for i, t in enumerate(TARGETS):
        seen, urls = set(), []

        def push(url, referer=None, logo=None, src=""):
            if not url or url in seen:
                return
            if RADIO.search(url) or wrong_language(url):
                return
            seen.add(url)
            urls.append({"url": url, "referer": referer, "logo": logo, "src": src})

        pin = ov.get(t["name"].strip().lower())
        if pin:
            push(pin, src="override")
        for u in t["extra"]:
            push(u, src="extra")
        for c in hits[i]:
            push(c["url"], c.get("referer"), c.get("logo"), c.get("src"))
        plan[i] = urls
    return plan, hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--min-height", type=int, default=720)
    ap.add_argument("--fallback-height", type=int, default=432,
                    help="when nothing clears --min-height, accept the best feed at or above this")
    ap.add_argument("--workers", type=int, default=48)
    ap.add_argument("--keep-existing", action="store_true",
                    help="keep an already-listed feed when this run cannot probe it")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", action="store_true", help="list every picked channel")
    ap.add_argument("--explain", help="print every probed feed and its failure reason for a channel name")
    ap.add_argument("--report", default=os.path.join(CACHE, "expand_report.json"))
    args = ap.parse_args()

    os.makedirs(CACHE, exist_ok=True)
    t0 = time.time()
    print("collecting sources…", flush=True)
    cands = collect_candidates()
    cands += load_existing(args.out)
    existing_urls = {c["url"] for c in cands if c.get("src") == "existing"}
    print("%d candidate entries" % len(cands), flush=True)

    plan, hits = build_target_urls(cands)
    todo = sum(len(v) for v in plan.values())
    print("%d targets, %d unique feeds to probe" % (len(TARGETS), todo), flush=True)

    cache = {}
    if os.path.exists(CACHE_JSON):
        try:
            cache = json.load(open(CACHE_JSON, encoding="utf-8"))
        except Exception:
            cache = {}

    flat = []
    for i, urls in plan.items():
        for u in urls:
            flat.append((i, u))

    results = {}
    probed = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {}
        for i, u in flat:
            key = u["url"]
            cached = cache.get(key)
            if cached is not None:
                results.setdefault(i, []).append((u, cached))
                continue
            futs[pool.submit(probe_stream, u["url"], u.get("referer"), args.min_height)] = (i, u)
        for fut in cf.as_completed(futs):
            i, u = futs[fut]
            probed += 1
            if probed % 40 == 0:
                print("  probed %d/%d  %.0fs" % (probed, len(futs), time.time() - t0), flush=True)
            try:
                res = fut.result()
            except Exception as exc:
                res = {"ok": False, "err": str(exc)[:60]}
            cache[u["url"]] = res
            results.setdefault(i, []).append((u, res))

    json.dump(cache, open(CACHE_JSON, "w", encoding="utf-8"), indent=0)

    # ---- choose one feed per target
    picked, missing, report = [], [], {"kept": [], "dropped": [], "groups": {}, "detail": {}}
    for i, t in enumerate(TARGETS):
        floor = t.get("min_h") or args.min_height
        rows, diag = [], []
        for u, res in results.get(i, []):
            diag.append({"url": u["url"], "ok": bool(res.get("ok")), "h": res.get("h"),
                         "codec": res.get("codec"), "bw": res.get("bw"), "err": res.get("err", "")})
            if not res.get("ok"):
                continue
            h = res.get("h") or 0
            if h and h < floor:
                diag[-1]["err"] = "below %dp" % floor
                continue
            avc = res.get("codec") != "hevc"
            rows.append((avc, brand_hint(t["name"], u["url"]), trusted(u["url"]), h,
                         res.get("bw") or 0, u, res))
        report["detail"][t["name"]] = diag
        if not rows:
            # second pass: take the best feed at or above the fallback floor
            for u, res in results.get(i, []):
                if not res.get("ok"):
                    continue
                h = res.get("h") or 0
                if h and h >= max(args.fallback_height, 1):
                    rows.append((res.get("codec") != "hevc", brand_hint(t["name"], u["url"]),
                                 trusted(u["url"]), h, res.get("bw") or 0, u, res))
            if rows:
                for d in diag:
                    if d["ok"] and d["h"] and d["h"] < floor:
                        d["err"] = "took as fallback (%dp)" % d["h"]
        if rows:
            rows.sort(key=lambda r: (r[0], r[1], r[2], r[3], r[4]), reverse=True)
            avc, _hint, _t, h, bw, u, res = rows[0]
            picked.append({
                "group": t["group"], "name": t["name"], "h": h, "bw": bw,
                "codec": "H.264" if avc else "HEVC", "url": u["url"],
                "referer": u.get("referer"), "logo": u.get("logo"), "src": u["src"],
            })
            report["kept"].append({"name": t["name"], "group": t["group"], "h": h,
                                   "src": u["src"], "url": u["url"]})
            continue
        # nothing verified in this run: fall back to an existing entry if allowed
        keep = None
        if args.keep_existing:
            for u, res in results.get(i, []):
                if u["src"] == "existing" and u["url"] in existing_urls:
                    keep = u
                    break
        if keep:
            old = next((c for c in cands if c["url"] == keep["url"]), {})
            picked.append({"group": t["group"], "name": t["name"], "h": old.get("height") or 0,
                           "bw": 0, "codec": "?", "url": keep["url"], "referer": keep.get("referer"),
                           "logo": keep.get("logo"), "src": "existing-unverified"})
            report["kept"].append({"name": t["name"], "h": 0, "src": "existing-unverified",
                                   "url": keep["url"]})
        else:
            missing.append(t["name"])
            report["dropped"].append({"name": t["name"], "group": t["group"],
                                      "reason": "no verified feed"})

    # dedupe identical URLs inside a group (one feed, two brand names would confuse the UI)
    seen_urls = {}
    clean = []
    for p in picked:
        k = (p["group"], p["url"])
        if k in seen_urls:
            report["dropped"].append({"name": p["name"], "group": p["group"],
                                      "reason": "duplicate of " + seen_urls[k]})
            continue
        seen_urls[k] = p["name"]
        clean.append(p)

    groups = {g: [] for g in GROUP_ORDER}
    for p in clean:
        ch = {
            "name": p["name"],
            "badge": ("%dp" % p["h"]) if p["h"] else "live",
            "height": p["h"],
            "quality": ("%dx%d %.2f Mbps" % (p["h"] * 16 // 9, p["h"], p["bw"] / 1e6))
                       if p["h"] and p["bw"] else (("%dp" % p["h"]) if p["h"] else "?"),
            "url": p["url"],
            **({"referer": p["referer"]} if p.get("referer") else {}),
            **({"logo": p["logo"]} if p.get("logo") else {}),
            "note": " | ".join([x for x in [("%dp" % p["h"]) if p["h"] else "live",
                                            p["codec"], ("%.1f Mbps" % (p["bw"] / 1e6)) if p["bw"] else ""]
                                if x] + ["verified"]),
        }
        groups[p["group"]].append(ch)

    out = {"groups": []}
    for g in GROUP_ORDER:
        lst = groups.get(g) or []
        lst.sort(key=lambda c: (-(c.get("height") or 0), c["name"].lower()))
        if lst:
            out["groups"].append({"name": g, "channels": lst})
            report["groups"][g] = len(lst)
            print("  %-10s %3d" % (g, len(lst)), flush=True)
            if args.show:
                for c in lst:
                    print("      %-32s %-6s %-8s %s" % (c["name"][:32], c["badge"],
                                                        c["note"].split(" | ")[1] if " | " in c["note"] else "",
                                                        c["url"][:70]), flush=True)

    total = sum(len(g["channels"]) for g in out["groups"])

    # keep what the scanners measured: a rebuild must not throw away the audio codec,
    # the measured bitrate or a resolution that fix_heights had to discover
    prev = {}
    try:
        with open(args.out, encoding="utf-8") as fh:
            for g in json.load(fh).get("groups", []):
                for c in g.get("channels", []):
                    prev[(g["name"], c["name"])] = c
                    prev.setdefault(("", c["name"]), c)
    except Exception:
        prev = {}
    if prev:
        for g in out["groups"]:
            for c in g["channels"]:
                old = prev.get((g["name"], c["name"])) or prev.get(("", c["name"]))
                if not old:
                    continue
                for k in ("audio", "measured_mbps"):
                    if k in old:
                        c[k] = old[k]
                if not c.get("height") and old.get("height"):
                    c["height"] = old["height"]
                    c["badge"] = old.get("badge") or c["badge"]

    print("\n%d channels verified in %.0fs (%d feeds probed)" % (total, time.time() - t0, probed))
    if missing:
        print("no verified feed for: %s" % ", ".join(missing[:40]))

    report["total"] = total
    json.dump(report, open(args.report, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    if args.explain:
        pat = args.explain.lower()
        for name, diag in report["detail"].items():
            if pat not in name.lower():
                continue
            print("\n== %s" % name)
            for d in diag:
                print("   %-5s %-5s %-5s %-92s %s" % (
                    "ok" if d["ok"] else "dead", d["h"] or "-", d["codec"] or "-",
                    d["url"][:92], d["err"]))
        print("")

    if args.dry_run:
        print("dry run - %s untouched" % args.out)
        return
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
