# -*- coding: utf-8 -*-
"""Per-channel guide nudge.

epg.pw's timings are not reliable for every channel (CMT ran Golden Girls while its
schedule said Cheers). Rather than pretend, a channel can be shifted by whole minutes,
or have its listings switched off, from the player.

File format:  {"CMT": -30, "HBO Family": 0, "Some Channel": null}
  null  -> listings hidden for that channel
  0     -> no shift (same as absent)
"""
import io
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "guide_offset.json")


def load():
    try:
        with io.open(PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception:
        return {}


def save(data):
    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    with io.open(PATH, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2, ensure_ascii=False, sort_keys=True)
    return clean


def set_shift(name, minutes):
    """minutes: int (shift), None (hide listings), or 'clear' (forget the channel)."""
    data = load()
    if minutes == "clear":
        data.pop(name, None)
    elif minutes is None:
        data[name] = None
    else:
        data[name] = int(minutes)
    return save(data)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("minutes", help="integer, 'off' to hide, 'clear' to reset")
    a = ap.parse_args()
    value = "clear" if a.minutes == "clear" else (None if a.minutes in ("off", "hide") else int(a.minutes))
    print(json.dumps(set_shift(a.name, value), indent=2, ensure_ascii=False))
