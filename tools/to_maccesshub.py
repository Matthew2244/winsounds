#!/usr/bin/env python3
"""Build MaccessHub event soundpacks from imported WinSounds schemes.

MaccessHub (https://github.com/masonasons/MaccessHub) reads user soundpacks from
~/Library/Application Support/MaccessHub/Soundpacks/<Pack name>/: a plain folder
holding pack.json ({"name", "author", "description", "version"}) and one sound
file per event, named after the event id (application.launched.wav). Any of
wav, aiff, aif, caf, mp3, m4a, aac, flac. Packs are found when MaccessHub
starts (or when a pack is edited in its Settings), so quit and reopen it after
running this.

Each WinSounds scheme becomes one pack named after the scheme. A slot gets the
first candidate WinSounds event the scheme has a sound for, skipping sounds
too long for the slot. A slot with no sensible match stays empty. Audio is
copied from the WinSounds scheme folders into the pack folder, never into this
repo.

A pack this script wrote carries a hidden .winsounds-pack.json (MaccessHub skips
hidden files). Re-running rebuilds those packs, but leaves alone any pack whose
files were changed since (by hand, or in MaccessHub's Settings) unless --force,
and never touches a folder it did not write.

    tools/to_maccesshub.py                 build or update every pack
    tools/to_maccesshub.py --dry-run       say what would happen, write nothing
    tools/to_maccesshub.py --scheme "xp pro" --verbose
    tools/to_maccesshub.py --out DIR       write somewhere else (for testing)
    tools/to_maccesshub.py --remove        delete the packs this script wrote
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

GENERATOR = "winsounds/tools/to_maccesshub.py"
FORMAT = 1
MARKER = ".winsounds-pack.json"
SCHEMES = Path.home() / "Library/Application Support/WinSounds/Schemes"
PACKS = Path.home() / "Library/Application Support/MaccessHub/Soundpacks"
SUPPORTED = {"wav", "aiff", "aif", "caf", "mp3", "m4a", "aac", "flac"}

SHORT = 1.5   # menus, rows, spaces, windows, music: frequent, must be brief
MEDIUM = 4.0  # app open and quit
LONG = 8.0    # rare system events

# MaccessHub event id -> (WinSounds events, best first; longest allowed seconds).
# An empty list leaves the slot silent on purpose.
SLOTS = {
    # Applications. "launching" stays empty so an app opening sounds once.
    "application.launched": (["app_open", "restore"], MEDIUM),
    "application.terminated": (["app_close", "minimize"], MEDIUM),

    # System. MaccessHub's login/logout slots are left to WinSounds, which
    # plays the real Windows logon/logoff at the real moment.
    "system.willSleep": (["sleep", "logout"], LONG),
    "system.didWake": (["wake", "login"], LONG),
    "system.screenLocked": (["lock"], LONG),
    "system.screenUnlocked": (["unlock"], LONG),
    "system.spaceChanged": (["navigation"], SHORT),
    "display.connected": (["device_connect"], LONG),
    "display.disconnected": (["device_disconnect"], LONG),

    # Power
    "power.acPower": (["power_connected", "device_connect"], LONG),
    "power.batteryPower": (["power_disconnected", "device_disconnect"], LONG),
    "power.upsPower": (["power_disconnected", "device_disconnect"], LONG),
    "power.battery20Minutes": (["battery_low"], LONG),
    "power.battery10Minutes": (["battery_low"], LONG),
    "power.battery5Minutes": (["battery_critical", "battery_low"], LONG),

    # Disks
    "storage.mounted": (["device_connect"], LONG),
    "storage.unmounted": (["device_disconnect"], LONG),

    # USB and Bluetooth
    "usb.connected": (["device_connect"], LONG),
    "usb.disconnected": (["device_disconnect"], LONG),
    "bluetooth.connected": (["device_connect"], LONG),
    "bluetooth.disconnected": (["device_disconnect"], LONG),

    # Network. Only the link slots, so a Wi-Fi join is one sound, not two.
    "network.wifiConnected": (["network_up", "notification"], LONG),
    "network.wifiDisconnected": (["network_down", "device_fail"], LONG),
    "network.ethernetConnected": (["network_up", "notification"], LONG),
    "network.ethernetDisconnected": (["network_down", "device_fail"], LONG),

    # Windows
    "window.created": (["maximize", "restore"], SHORT),
    "window.minimized": (["minimize"], SHORT),
    "window.restored": (["restore", "maximize"], SHORT),

    # Menus and lists
    "ui.menuOpened": (["menu_popup"], SHORT),
    "ui.menuItemSelected": (["menu_command"], SHORT),
    "ui.rowExpanded": (["navigation"], SHORT),

    # Music
    "media.playing": (["restore", "maximize"], SHORT),
    "media.paused": (["minimize"], SHORT),

    # Microphone mute shortcut: Windows Speech Recognition's on and sleep sounds.
    "audio.inputUnmuted": (["speech_on"], SHORT),
    "audio.inputMuted": (["speech_off"], SHORT),
}

_durations = {}


def duration(path):
    """Length in seconds, or None if it can't be read."""
    key = str(path)
    if key in _durations:
        return _durations[key]
    d = None
    try:
        with wave.open(key) as w:
            if w.getframerate():
                d = w.getnframes() / w.getframerate()
    except Exception:
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", key],
                capture_output=True, text=True, timeout=20).stdout.strip()
            d = float(out) if out else None
        except Exception:
            d = None
    _durations[key] = d
    return d


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def folder_name(name):
    name = name.strip().replace("/", "-").replace(":", "-")
    return None if not name or name.startswith(".") else name


def plan_scheme(scheme_dir):
    """Returns (scheme dict, {slot: (source Path, winsounds event, seconds)}, skipped notes)."""
    scheme = json.loads((scheme_dir / "scheme.json").read_text())
    events = scheme.get("events", {})
    chosen, notes = {}, []
    for slot, (candidates, limit) in SLOTS.items():
        for ev in candidates:
            for fname in events.get(ev, []):
                src = scheme_dir / fname
                ext = src.suffix.lower().lstrip(".")
                if not src.is_file() or ext not in SUPPORTED:
                    continue
                secs = duration(src)
                if secs is None:
                    notes.append(f"{slot}: {fname} unreadable")
                    continue
                if secs > limit:
                    notes.append(f"{slot}: {fname} is {secs:.1f} s, over {limit:g} s")
                    continue
                chosen[slot] = (src, ev, secs)
                break
            if slot in chosen:
                break
    return scheme, chosen, notes


def read_marker(pack_dir):
    try:
        return json.loads((pack_dir / MARKER).read_text())
    except Exception:
        return None


def untouched(pack_dir, marker):
    """True when the pack's files are exactly the ones this script last wrote."""
    recorded = marker.get("files", {})
    present = {p.name for p in pack_dir.iterdir() if not p.name.startswith(".")}
    if present != set(recorded) | {"pack.json"}:
        return False
    return all(sha1(pack_dir / n) == h for n, h in recorded.items())


def write_pack(dest, scheme, chosen):
    tmp = dest.with_name("." + dest.name + ".building")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    files, mapping = {}, {}
    for slot, (src, ev, secs) in sorted(chosen.items()):
        name = f"{slot}.{src.suffix.lower().lstrip('.')}"
        shutil.copy2(src, tmp / name)
        files[name] = sha1(tmp / name)
        mapping[slot] = {"event": ev, "file": src.name, "seconds": round(secs, 2)}
    licence = scheme.get("license") or "Personal use only."
    manifest = {
        "name": dest.name,
        "author": scheme.get("source") or "WinSounds",
        "description": f"From the WinSounds scheme {scheme.get('name')}. {licence}",
        "version": "1.0",
    }
    (tmp / "pack.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    marker = {"generator": GENERATOR, "format": FORMAT, "scheme": scheme.get("id"),
              "signature": scheme.get("signature"), "license": licence,
              "files": files, "mapping": mapping}
    (tmp / MARKER).write_text(json.dumps(marker, indent=1, sort_keys=True) + "\n")
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)


def matches(scheme, query):
    q = query.lower()
    return q in scheme.name.lower() or q in json.loads((scheme / "scheme.json").read_text()).get("name", "").lower()


def main():
    ap = argparse.ArgumentParser(description="Build MaccessHub event soundpacks from WinSounds schemes.")
    ap.add_argument("--schemes", type=Path, default=SCHEMES, help="WinSounds Schemes folder")
    ap.add_argument("--out", type=Path, default=PACKS, help="MaccessHub user soundpacks folder")
    ap.add_argument("--scheme", action="append", default=[], help="only schemes whose name or id contains this (repeatable)")
    ap.add_argument("--min-slots", type=int, default=3, help="skip schemes that fill fewer slots than this (default 3)")
    ap.add_argument("--dry-run", action="store_true", help="say what would happen, write nothing")
    ap.add_argument("--force", action="store_true", help="rebuild packs even if their files were changed since")
    ap.add_argument("--remove", action="store_true", help="delete every pack this script wrote, nothing else")
    ap.add_argument("--verbose", action="store_true", help="list each pack's slots and skipped sounds")
    ap.add_argument("--json", action="store_true", help="print a machine-readable summary")
    args = ap.parse_args()

    out = args.out.expanduser()
    lines = []

    if args.remove:
        n = 0
        for d in sorted(out.iterdir()) if out.is_dir() else []:
            m = read_marker(d) if d.is_dir() else None
            if m and m.get("generator") == GENERATOR:
                n += 1
                if not args.dry_run:
                    shutil.rmtree(d)
        print(f"{'Would remove' if args.dry_run else 'Removed'} {n} WinSounds packs from {out}.")
        return 0

    dirs = sorted(p for p in args.schemes.expanduser().iterdir() if (p / "scheme.json").is_file())
    if args.scheme:
        dirs = [d for d in dirs if any(matches(d, q) for q in args.scheme)]
    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)

    built, kept, skipped, conflicts, summary = [], [], [], [], []
    taken = set()
    for d in dirs:
        scheme, chosen, notes = plan_scheme(d)
        name = folder_name(scheme.get("name") or d.name)
        if name in taken:
            name = f"{name} ({scheme.get('id')})"
        taken.add(name)
        if len(chosen) < args.min_slots:
            skipped.append(f"{name} ({len(chosen)} slots)")
            continue
        dest = out / name
        marker = read_marker(dest) if dest.exists() else None
        if dest.exists() and not (marker and marker.get("generator") == GENERATOR):
            conflicts.append(name)
            continue
        if marker and not args.force and not untouched(dest, marker):
            kept.append(name)
            continue
        if not args.dry_run:
            write_pack(dest, scheme, chosen)
        built.append(name)
        summary.append({"pack": name, "scheme": scheme.get("id"),
                        "slots": {s: c[1] for s, c in sorted(chosen.items())}})
        if args.verbose:
            lines.append(f"{name}: {len(chosen)} slots")
            for s, (src, ev, secs) in sorted(chosen.items()):
                lines.append(f"  {s} <- {ev} ({src.name}, {secs:.2f} s)")
            for n in notes:
                lines.append(f"  skipped {n}")

    if args.json:
        print(json.dumps({"built": summary, "kept": kept, "skipped": skipped,
                          "conflicts": conflicts}, indent=1))
        return 0
    verb = "Would build" if args.dry_run else "Built"
    lines.append(f"{verb} {len(built)} packs in {out}.")
    if kept:
        lines.append(f"Left {len(kept)} changed since last time (use --force): {', '.join(kept)}.")
    if conflicts:
        lines.append(f"Not touched, a pack of that name already exists: {', '.join(conflicts)}.")
    if skipped:
        lines.append(f"Skipped {len(skipped)} with too few event sounds: {', '.join(skipped)}.")
    if built and not args.dry_run:
        lines.append("Quit and reopen MaccessHub to see them.")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
