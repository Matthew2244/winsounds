#!/usr/bin/python3
"""winsounds: sound schemes (Windows, Mac, Android, anything) for macOS events.

The CLI half. It imports sound folders into schemes, edits the config the
background agent (winsounds-agent) reads, previews sounds, installs the
alert sound, and installs the LaunchAgent. Every command prints its whole
answer once, so VoiceOver reads it in one pass.

Standard library only: /usr/bin/python3 runs it, no Homebrew needed.
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

VERSION = "1.0.0"
LABEL = "com.matthew.winsounds"
HOME = os.path.expanduser("~")
SUPPORT = os.path.join(HOME, "Library", "Application Support", "WinSounds")
SCHEMES = os.path.join(SUPPORT, "Schemes")
CONFIG = os.path.join(SUPPORT, "config.json")
STATE = os.path.join(SUPPORT, "state.json")
AGENT_STATE = os.path.join(SUPPORT, "agent.json")
SIMULATE = os.path.join(SUPPORT, "simulate.json")
QUIET_STOP = os.path.join(SUPPORT, "quiet-stop")
REPORT = os.path.join(SUPPORT, "import-report.txt")
LOG = os.path.join(HOME, ".winsounds.log")
REPO = os.path.dirname(os.path.realpath(__file__))
MAPPINGS = os.path.join(REPO, "mappings")
INSTALLED_MAPPINGS = os.path.join(SUPPORT, "mappings")
AGENT_BIN = os.path.join(HOME, "bin", "winsounds-agent")
PLIST = os.path.join(HOME, "Library", "LaunchAgents", LABEL + ".plist")
USER_SOUNDS = os.path.join(HOME, "Library", "Sounds")
ALERT_PREFIX = "WinSounds - "
BEEP_KEY = "com.apple.sound.beep.sound"
NOTIFY_RELOAD = LABEL + ".reload"
NOTIFY_SIMULATE = LABEL + ".simulate"
DEFAULT_SCHEME_HINT = "Windows XP Professional"

IGNORED_EXT = {".json", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".txt", ".md", ".ini", ".db", ".url", ".htm", ".html", ".pdf", ".theme", ".reg", ".mid", ".midi", ".raw", ".mk", ".rmi", ".ico", ".cur", ".ani", ".exe", ".dll", ".zip", ".scr"}
AUDIO_EXT = {".mov", ".wav", ".wma", ".mp3", ".m4a", ".aif", ".aiff", ".aifc", ".ogg",
             ".oga", ".opus", ".flac", ".caf", ".au", ".snd", ".mp2", ".aac"}
MANIFEST_NAMES = ("manifest.json", "scheme.json", "winsounds.json")
DETECTORS = {"usb": "USB devices", "bluetooth": "Bluetooth devices"}


# ---------------------------------------------------------------- utilities

def say(text):
    """Write the whole answer once."""
    sys.stdout.write(text.rstrip("\n") + "\n")
    sys.stdout.flush()


def fail(text, code=1):
    sys.stderr.write(text.rstrip("\n") + "\n")
    sys.exit(code)


def log(line):
    try:
        with open(LOG, "a") as f:
            f.write("%s cli %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def slug(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-") or "scheme"


def plural(n, word, many=None):
    return "%d %s" % (n, word if n == 1 else (many or word + "s"))


def mapping(name):
    """Mapping files: the repo copy wins; the installed copy is the fallback."""
    for base in (MAPPINGS, INSTALLED_MAPPINGS):
        data = load_json(os.path.join(base, name))
        if data is not None:
            return data
    fail("Missing mapping file %s." % name)


# ------------------------------------------------------------------ events

class Events:
    def __init__(self):
        data = mapping("events.json")
        self.list = data["events"]
        self.by_id = {e["id"]: e for e in self.list}
        self.exact = {}      # alias -> [(event, rank)]
        self.prefix = []     # (alias-without-star, event, rank)
        for e in self.list:
            for rank, a in enumerate(e.get("aliases", [])):
                if a.endswith("*"):
                    self.prefix.append((norm(a[:-1]), e["id"], rank))
                else:
                    self.exact.setdefault(norm(a), []).append((e["id"], rank))
        # names a manifest may use for an event: id, label, Windows name, aliases
        self.lookup = {}
        for e in self.list:
            for key in [e["id"], e.get("label", ""), e.get("windows", "")]:
                if key:
                    self.lookup.setdefault(norm(key), e["id"])
        for a, hits in self.exact.items():
            self.lookup.setdefault(a, hits[0][0])

    def event_for_name(self, name):
        return self.lookup.get(norm(name))

    def chain(self, event):
        """The event, then its fallbacks, breadth first, no repeats."""
        out, queue = [], [event]
        while queue:
            e = queue.pop(0)
            if e in out or e not in self.by_id:
                continue
            out.append(e)
            queue.extend(self.by_id[e].get("fallback", []))
        return out

    def label(self, event):
        return self.by_id.get(event, {}).get("label", event)

    def classify(self, stem, strip=()):
        """Map one filename stem to {event: (tier, rank)}.

        Tries the whole name, then the name with each known prefix removed
        (theme, folder name, 'Windows XP' ...). Tiers: exact, without trailing
        digits, without one trailing letter, then prefix aliases. The first
        tier that matches anything decides.
        """
        c = norm(stem)
        forms = [c]
        for p in strip:
            p = norm(p)
            if p and c.startswith(p) and len(c) > len(p):
                forms.append(c[len(p):])
        for f in list(forms):
            for p in RULES_STRIP:
                if f.startswith(p) and len(f) > len(p):
                    forms.append(f[len(p):])
                    break
        tiers = [forms,
                 [re.sub(r"\d+$", "", re.sub(r"\d+(k|khz|hz)$", "", f)) for f in forms],
                 [f[:-1] for f in forms if len(f) >= 5]]
        for tier, candidates in enumerate(tiers):
            hits = {}
            for f in candidates:
                for ev, rank in self.exact.get(f, []):
                    if ev not in hits or rank < hits[ev][1]:
                        hits[ev] = (tier, rank)
            if hits:
                return hits
        hits = {}
        for f in forms:
            for p, ev, rank in self.prefix:
                if p and f.startswith(p) and ev not in hits:
                    hits[ev] = (3, rank)
        if hits:
            return hits
        # truncated 8.3 names: "wo_excla" is the start of "exclamation"
        for f in forms:
            f = re.sub(r"\d+$", "", f)
            if len(f) < 4:
                continue
            for alias, lst in self.exact.items():
                if len(alias) > len(f) and alias.startswith(f):
                    for ev, rank in lst:
                        if ev not in hits or rank < hits[ev][1]:
                            hits[ev] = (4, rank)
        return hits


RULES = None
RULES_STRIP = []


def rules():
    global RULES, RULES_STRIP
    if RULES is None:
        RULES = mapping("rules.json")
        RULES_STRIP = [norm(p) for p in sorted(RULES.get("strip_name_prefixes", []), key=len, reverse=True)]
    return RULES


# ------------------------------------------------------------------ config

def platform_defaults():
    return mapping("platforms.json")["platforms"]["macos"]


def load_config():
    cfg = load_json(CONFIG, {}) or {}
    cfg.setdefault("scheme", "")
    cfg.setdefault("enabled", True)
    cfg.setdefault("muted", False)
    cfg.setdefault("volume", 60)
    cfg.setdefault("alert", False)
    toggles = cfg.setdefault("toggles", {})
    for ev, info in platform_defaults().items():
        toggles.setdefault(ev, bool(info.get("default", True)))
    for d in DETECTORS:
        toggles.setdefault(d, True)
    return cfg


def save_config(cfg, reload=True):
    save_json(CONFIG, cfg)
    if reload:
        notify(NOTIFY_RELOAD)


def notify(name):
    try:
        subprocess.run(["/usr/bin/notifyutil", "-p", name], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


# ----------------------------------------------------------------- schemes

def all_schemes():
    out = []
    try:
        names = os.listdir(SCHEMES)
    except OSError:
        return out
    for n in names:
        s = load_json(os.path.join(SCHEMES, n, "scheme.json"))
        if s and s.get("id"):
            s["_dir"] = os.path.join(SCHEMES, n)
            out.append(s)
    out.sort(key=lambda s: (s.get("order", 999), s.get("name", "").lower()))
    return out


def mac_events():
    return list(platform_defaults().keys())


def scheme_counts(s, ev):
    mapped = [e for e, f in s.get("events", {}).items() if f]
    mac = 0
    for e in mac_events():
        if resolve(s, e, ev)[0]:
            mac += 1
    return len(mapped), mac


def resolve(s, event, ev):
    """(file path, event actually used) for an event, following fallbacks."""
    for e in ev.chain(event):
        files = s.get("events", {}).get(e) or []
        for f in files:
            p = os.path.join(s["_dir"], f)
            if os.path.exists(p):
                return p, e
    return None, None


def find_scheme(query, schemes=None):
    schemes = schemes if schemes is not None else all_schemes()
    if not schemes:
        fail("No schemes are imported yet. Run: winsounds import <folder>.")
    q = query.strip().lower()
    qn, qs = norm(q), slug(q)
    for s in schemes:
        if s["id"] == qs or norm(s["name"]) == qn:
            return s
    words = [w for w in re.split(r"\s+", q) if w]
    hits = [s for s in schemes if all(w in s["name"].lower() or w in s["id"] for w in words)]
    if not hits:
        hits = [s for s in schemes if qn and qn in norm(s["name"])]
    visible = [s for s in hits if not s.get("hidden")] or hits
    if len(visible) == 1:
        return visible[0]
    if not visible:
        fail("No scheme matches %s. Try winsounds list." % query)
    # prefer an exact word-boundary match on the name start
    starts = [s for s in visible if s["name"].lower().startswith(q)]
    if len(starts) == 1:
        return starts[0]
    names = ", ".join(s["name"] for s in visible[:12])
    more = "" if len(visible) <= 12 else ", and %d more" % (len(visible) - 12)
    fail("%s matches %d schemes: %s%s. Be more specific." % (query, len(visible), names, more))


def current_scheme(cfg=None):
    cfg = cfg or load_config()
    if not cfg.get("scheme"):
        return None
    for s in all_schemes():
        if s["id"] == cfg["scheme"]:
            return s
    return None


# -------------------------------------------------------------- the agent

def agent_running():
    st = load_json(AGENT_STATE, {}) or {}
    pid = st.get("pid")
    if pid:
        try:
            os.kill(int(pid), 0)
            return int(pid)
        except (OSError, ValueError):
            pass
    return None


def launchd_loaded():
    r = subprocess.run(["/bin/launchctl", "print", "gui/%d/%s" % (os.getuid(), LABEL)],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return r.returncode == 0


# ---------------------------------------------------------------- playback

def afplay(path, volume, limit=None, wait=True):
    """Play through afplay, the proven path. A watchdog kills it if it runs
    5 seconds past its limit: a wedged audio system must never leave a
    stuck process or hang the command."""
    limit = limit or 30
    args = ["/usr/bin/afplay", "-v", "%.3f" % max(0.0, min(1.0, volume / 100.0)), "-t", str(limit), path]
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not wait:
        return p
    try:
        p.wait(timeout=limit + 5)
    except subprocess.TimeoutExpired:
        p.kill()
        log("afplay of %s did not finish; killed it (audio output not answering?)" % os.path.basename(path))
        fail("The sound did not finish playing, so it was stopped. The Mac's audio output may not be answering.")
    return p


def render(paths, out, volume, gap=0.8, limit=None, lead=0.0):
    """Write sounds to one file instead of the speakers: each trimmed to
    `limit` seconds, `gap` seconds of silence between, `lead` before the
    first. 44.1 kHz stereo; WAV, AIFF, M4A or MP3 by the extension."""
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    args = [ffmpeg, "-nostdin", "-v", "error", "-y"]
    for p in paths:
        if limit:
            args += ["-t", str(limit)]
        args += ["-i", p]
    parts, idx = [], 0
    filt = []
    gain = max(0.0, volume / 100.0)
    def silence(sec):
        nonlocal idx
        label = "s%d" % idx
        idx += 1
        filt.append("anullsrc=r=44100:cl=stereo,atrim=0:%.3f[%s]" % (sec, label))
        return "[%s]" % label
    if lead > 0:
        parts.append(silence(lead))
    for i in range(len(paths)):
        if i and gap > 0:
            parts.append(silence(gap))
        filt.append("[%d:a:0]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=%.3f[a%d]" % (i, gain, i))
        parts.append("[a%d]" % i)
    filt.append("%sconcat=n=%d:v=0:a=1[out]" % ("".join(parts), len(parts)))
    ext = os.path.splitext(out)[1].lower()
    codec = {".m4a": ["-c:a", "aac", "-b:a", "256k"], ".mp3": ["-c:a", "libmp3lame", "-b:a", "256k"],
             ".aiff": ["-c:a", "pcm_s16be"], ".aif": ["-c:a", "pcm_s16be"]}.get(ext, ["-c:a", "pcm_s16le"])
    args += ["-filter_complex", ";".join(filt), "-map", "[out]"] + codec + [out]
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        fail("Could not write %s: %s" % (out, r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "ffmpeg failed"))
    return out


# ------------------------------------------------------------------ import

class Candidate:
    """One scheme found while walking an import folder."""

    def __init__(self, path, rel, name, theme=None):
        self.path, self.rel, self.name, self.theme = path, rel, name, theme
        self.files = []          # absolute source paths
        self.forced = {}         # src -> event, from event-named folders
        self.manifest = {}
        self.events = {}         # event -> [src]
        self.unmapped = []
        self.theme_file = None
        self.also = []


def display_name(root, rel, cache):
    """A readable scheme name from a folder path. Levels listed in
    skip_in_names (Schemes, AOSP, Microsoft ...) are left out, the import
    root itself only names its own loose files, and standalone folders or
    full-path matches in folder_names start a fresh name."""
    key = rel
    if key in cache:
        return cache[key]
    r = rules()
    names = {k.lower(): v for k, v in r.get("folder_names", {}).items()}
    skip = {k.lower() for k in r.get("skip_in_names", [])}
    standalone = {k.lower() for k in r.get("standalone", [])}
    if rel == "":
        base = os.path.basename(os.path.normpath(root))
        name = names.get(base.lower(), base.title() if base.islower() else base)
        cache[key] = name
        return name
    name = ""
    parts = rel.split(os.sep)
    for i, part in enumerate(parts):
        path = "/".join(parts[:i + 1]).lower()
        low = part.lower()
        if path in names:
            own, fresh = names[path], True
        elif low in skip:
            continue
        elif low in names:
            own, fresh = names[low], low in standalone
        else:
            own, fresh = (part.title() if part.islower() else part), low in standalone
        if fresh or not name:
            name = own
        elif own.lower().startswith(name.lower()):
            name = own
        else:
            name = "%s %s" % (name, own)
    if not name:
        name = display_name(root, "", cache)
    cache[key] = name
    return name


def read_manifest(path):
    for n in MANIFEST_NAMES:
        p = os.path.join(path, n)
        if os.path.isfile(p):
            data = load_json(p)
            if isinstance(data, dict):
                return data
            if isinstance(data, list):
                return {"schemes": data}
    return None


def manifest_entries(man):
    """A manifest's 'schemes' as {subfolder name or relative path: entry}."""
    out = {}
    sch = man.get("schemes")
    if isinstance(sch, dict):
        for k, v in sch.items():
            if isinstance(v, dict):
                out[k] = v
    elif isinstance(sch, list):
        for v in sch:
            if isinstance(v, dict):
                key = v.get("path") or v.get("dir") or v.get("folder") or v.get("directory") or v.get("name")
                if key:
                    out[key] = v
    return out


def manifest_events(entry, ev, files_by_name):
    """{event: [src]} from a manifest entry, accepting several shapes."""
    out = {}
    raw = entry.get("events") or entry.get("mapping") or entry.get("sounds") or entry.get("files") or {}
    pairs = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            vals = v if isinstance(v, list) else [v]
            ext_key = os.path.splitext(str(k))[1].lower() in AUDIO_EXT
            for val in vals:
                if isinstance(val, dict):
                    val = val.get("file") or val.get("event") or val.get("path")
                if not isinstance(val, str):
                    continue
                pairs.append((val, k) if ext_key else (k, val))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                e = item.get("event") or item.get("events")
                f = item.get("file") or item.get("filename") or item.get("path")
                if isinstance(e, str) and isinstance(f, str):
                    pairs.append((e, f))
    for e, f in pairs:
        eid = e if e in ev.by_id else ev.event_for_name(e)
        if not eid:
            continue
        src = files_by_name.get(f.lower()) or files_by_name.get(os.path.basename(f).lower()) \
            or files_by_name.get(os.path.splitext(os.path.basename(f))[0].lower())
        if src:
            out.setdefault(eid, []).append(src)
    return out


def detect_themes(files, ev, dirname):
    """Split a folder's files into bundled themes: 'Jungle Default.wav' and
    friends, or camelCase 'winAquariumOpen.wav'. Returns {theme: [files]}."""
    r = rules()
    known = sorted(r.get("theme_prefixes", {}).items(), key=lambda kv: len(kv[0]), reverse=True)
    never = {w.lower() for w in r.get("not_theme_words", [])}
    groups, rest = {}, []
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0]
        hit = None
        for p, label in known:
            if (p[0].islower() and stem.startswith(p) and len(stem) > len(p) and stem[len(p)].isupper()) or \
               (stem.lower().startswith(p.lower() + " ")):
                hit = p
                break
        if hit:
            groups.setdefault(hit, []).append(f)
        else:
            rest.append(f)
    # generic: a first word shared by 4+ files, 3+ of which map to events
    words = {}
    for f in rest:
        stem = os.path.splitext(os.path.basename(f))[0]
        parts = stem.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() not in never and not parts[0].isdigit():
            words.setdefault(parts[0], []).append(f)
    for w, fs in words.items():
        if len(fs) < 4:
            continue
        mapped = sum(1 for f in fs if ev.classify(os.path.splitext(os.path.basename(f))[0], (w,)))
        if mapped >= 3:
            groups[w] = fs
            rest = [f for f in rest if f not in fs]
    return groups, rest


def parse_event_maps(root, ev):
    """Windows .Theme and .reg files anywhere under the root (including
    _theme-files): each becomes a group {basename lower: [events]}.
    A .reg can hold many schemes; each scheme key is its own group."""
    r = rules()
    table = {k.lower(): v for k, v in r.get("windows_app_events", {}).items()}
    groups = []
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".theme", ".reg"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                raw = open(path, "rb").read()
            except OSError:
                continue
            if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                text = raw.decode("utf-16", "replace")
            else:
                text = raw.decode("latin-1")
            maps = {}
            section = None
            for line in text.splitlines():
                line = line.strip()
                m = re.match(r"\[(?:HKEY_[A-Z_]+\\)?AppEvents\\Schemes\\Apps\\([^\\\]]+)\\([^\\\]]+)\\([^\\\]]+)\]", line, re.I)
                if m:
                    section = (m.group(2).lower(), m.group(3))
                    continue
                if line.startswith("["):
                    section = None
                    continue
                if not section:
                    continue
                m = re.match(r'(?:DefaultValue|@)\s*=\s*"?(.*?)"?\s*$', line, re.I)
                if not m or not m.group(1).strip():
                    continue
                event = table.get(section[0])
                if not event:
                    continue
                target = m.group(1).replace("\\\\", "\\")
                base = re.split(r"[\\/]", target)[-1].strip().lower()
                if not base:
                    continue
                key = section[1] if ext == ".reg" else ".current"
                maps.setdefault(key, {}).setdefault(base, [])
                if event not in maps[key][base]:
                    maps[key][base].append(event)
            for key, mp in maps.items():
                groups.append({"name": "%s %s" % (fn, key) if ext == ".reg" else fn, "map": mp})
    return groups


def prefer_formats(files):
    """One file per sound: when 'Lock.ogg' and 'Lock.m4a' (or a wav/ and
    ogg/ copy) both exist, keep the format listed first in rules.json."""
    pref = rules().get("format_preference", [])
    rank = {e: i for i, e in enumerate(pref)}
    best = {}
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0].lower()
        r = rank.get(os.path.splitext(f)[1].lower(), len(pref))
        if stem not in best or r < best[stem][0]:
            best[stem] = (r, f)
    keep = {f for _, f in best.values()}
    return [f for f in files if f in keep]


def walk_import(root, ev):
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        fail("%s is not a folder." % root)
    r = rules()
    theme_names = r.get("theme_prefixes", {})
    merge = {k.lower() for k in r.get("merge_folders", [])}
    event_folders = {k.lower(): v for k, v in r.get("event_folders", {}).items()}
    collect = {k.lower(): v for k, v in r.get("collect_folders", {}).items()}
    contains = {k.lower(): v for k, v in r.get("collect_folders_containing", {}).items()}
    names_cache = {}
    manifests = {}           # rel -> manifest entry for that folder
    candidates = {}          # rel -> Candidate (base scheme for the folder)
    extra = []               # theme candidates
    odd = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
        rel = os.path.relpath(dirpath, root)
        rel = "" if rel == "." else rel
        man = read_manifest(dirpath)
        if man:
            for k, entry in manifest_entries(man).items():
                key = os.path.normpath(os.path.join(rel, k)) if rel else os.path.normpath(k)
                manifests[key] = entry
            if not manifest_entries(man) and any(k in man for k in ("events", "mapping", "sounds", "name", "license")):
                manifests[rel] = man
        audio = []
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            full = os.path.join(dirpath, fn)
            if ext in AUDIO_EXT:
                audio.append(full)
            elif ext not in IGNORED_EXT:
                odd.append(os.path.join(rel, fn))
        if not audio:
            continue
        # where do these files belong? Walk up past format folders (ogg/,
        # wav/), category folders (effects/) and event folders (alarms/,
        # notifications/); a collect folder takes everything below it.
        parts = rel.split(os.sep) if rel else []
        scheme_parts, folder_event = list(parts), None
        for i, part in enumerate(parts):
            word = next((w for w in contains if w in part.lower()), None)
            if part.lower() in collect or word:
                scheme_parts = parts[:i + 1]
                folder_event = collect.get(part.lower()) or contains[word]
                break
        else:
            while scheme_parts:
                low = scheme_parts[-1].lower()
                hits = ev.exact.get(norm(low))
                if low in merge:
                    scheme_parts.pop()
                elif low in event_folders or (hits and "/".join(scheme_parts) not in manifests):
                    folder_event = folder_event or event_folders.get(low) or sorted(hits, key=lambda h: h[1])[0][0]
                    scheme_parts.pop()
                else:
                    break
        srel = os.sep.join(scheme_parts)
        if srel != rel or folder_event:
            parent = candidates.get(srel)
            if parent is None:
                parent = Candidate(os.path.join(root, srel), srel, display_name(root, srel, names_cache))
                candidates[srel] = parent
            if not parent.manifest:
                parent.manifest = manifests.get(srel) or manifests.get(rel) or {}
            for f in audio:
                parent.files.append(f)
                if folder_event:
                    parent.forced[f] = folder_event
            continue
        entry = manifests.get(rel) or {}
        base = os.path.basename(rel) if rel else ""
        groups, rest = detect_themes(audio, ev, base)
        dname = entry.get("name") or display_name(root, rel, names_cache)
        if len(groups) == 1 and not rest:
            (theme, fs), = groups.items()
            c = candidates.setdefault(rel, Candidate(dirpath, rel, dname))
            c.theme = theme
            c.files.extend(fs)
            c.manifest = c.manifest or entry
            continue
        for theme, fs in groups.items():
            label = theme_names.get(theme, theme)
            t = Candidate(dirpath, rel, "%s %s" % (dname, label), theme)
            t.files = fs
            t.manifest = entry
            extra.append(t)
        if rest:
            c = candidates.setdefault(rel, Candidate(dirpath, rel, dname))
            c.files.extend(rest)
            c.manifest = c.manifest or entry
    out = list(candidates.values()) + extra
    for c in out:
        c.files = prefer_formats(c.files)
    return root, out, odd, parse_event_maps(root, ev)


def common_prefix(stems):
    """The start every stem shares ('The 60's USA ', '7B', 'wo_'), if any."""
    if len(stems) < 3:
        return ""
    p = os.path.commonprefix([s.lower() for s in stems])
    if len(p) < 2 or any(len(s) <= len(p) + 2 for s in stems):
        return ""
    return p


def classify_candidate(c, ev, event_maps=()):
    stems = [os.path.splitext(os.path.basename(f))[0] for f in c.files if f not in c.forced]
    strip = [c.theme] if c.theme else []
    folder = os.path.basename(c.path)
    strip += [folder, c.name, common_prefix(stems)]
    best = {}   # event -> [(key, src)]
    for f in c.files:
        if f in c.forced:
            e = c.forced[f]
            stem_n = norm(os.path.splitext(os.path.basename(f))[0])
            pref = [norm(x) for x in rules().get("preferred_first", [])]
            key = (-1, pref.index(stem_n) if stem_n in pref else len(pref), 0, 0, os.path.basename(f).lower())
            best.setdefault(e, []).append((key, f))
            continue
        stem = os.path.splitext(os.path.basename(f))[0]
        hits = ev.classify(stem, strip)
        if not hits:
            c.unmapped.append(f)
            continue
        branded = 0 if (c.theme or norm(stem).startswith("windows")) else 1
        m = re.search(r"(\d+)$", norm(stem))
        num = int(m.group(1)) if m else 0
        for e, (tier, rank) in hits.items():
            best.setdefault(e, []).append(((rank, tier, branded, num, stem.lower()), f))
    for e, lst in best.items():
        lst.sort()
        c.events[e] = [f for _, f in lst]
    # a Windows .Theme / .reg that names most of these files wins
    by_base = {os.path.basename(f).lower(): f for f in c.files}
    top, top_score, top_hits = None, 0.0, 0
    for g in event_maps:
        hits = sum(1 for b in g["map"] if b in by_base)
        score = hits / max(1, len(c.files)) + hits / max(1, len(g["map"])) / 100.0
        if hits >= 3 and score > top_score:
            top, top_score, top_hits = g, score, hits
    # only when the map covers at least half the folder: a theme naming
    # ding, chord and tada must not take over a whole Windows XP folder
    if top and top_hits >= 0.5 * len(c.files):
        c.theme_file = top["name"]
        mapped = {}
        for b, events in top["map"].items():
            if b in by_base:
                for e in events:
                    mapped.setdefault(e, []).append(by_base[b])
        for e, fs in mapped.items():
            c.events[e] = fs + [x for x in c.events.get(e, []) if x not in fs]
    if c.manifest:
        by_name = {}
        for f in c.files:
            b = os.path.basename(f)
            by_name[b.lower()] = f
            by_name.setdefault(os.path.splitext(b)[0].lower(), f)
        man = manifest_events(c.manifest, ev, by_name)
        for e, fs in man.items():
            c.events[e] = fs + [x for x in c.events.get(e, []) if x not in fs]
    used = {f for fs in c.events.values() for f in fs}
    c.unmapped = [f for f in c.files if f not in used]


def file_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def order_of(name):
    lst = rules().get("order", [])
    best = None
    for i, prefix in enumerate(lst):
        if name.lower().startswith(prefix.lower()):
            if best is None or len(prefix) > len(lst[best]):
                best = i
    return best if best is not None else len(lst)


def guess(table, text, default):
    t = text.lower()
    for k, v in table.items():
        if k.lower() in t:
            return v
    return default


NATIVE_EXT = {".wav", ".aif", ".aiff", ".aifc", ".mp3", ".m4a", ".caf"}
PCM_EXT = {".mov", ".au", ".snd"}     # decoded to PCM WAV


def target_ext(src):
    """What a source becomes: kept as is when the Mac plays it natively,
    PCM WAV for QuickTime and Unix audio, AAC for WMA, Ogg, Opus, FLAC."""
    ext = os.path.splitext(src)[1].lower()
    if ext in NATIVE_EXT:
        return ext
    if ext in PCM_EXT:
        return ".wav"
    return ".m4a"


def wav_is_plain_pcm(path):
    """True for an ordinary PCM WAV (format 1, or extensible PCM). Old
    Windows files are often ADPCM, GSM or MP3 inside a WAV; those get
    decoded instead of copied."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return False
    i = 12
    while i + 8 <= len(head):
        cid, size = head[i:i + 4], int.from_bytes(head[i + 4:i + 8], "little")
        if cid == b"fmt ":
            tag = int.from_bytes(head[i + 8:i + 10], "little")
            bits = int.from_bytes(head[i + 22:i + 24], "little")
            if tag == 0xFFFE and i + 40 <= len(head):
                tag = int.from_bytes(head[i + 32:i + 34], "little")
            return tag in (1, 3) and bits in (8, 16, 24, 32)
        i += 8 + size + (size & 1)
    return False


def dest_names(files):
    """Unique, readable names for a scheme's files."""
    out, used = {}, set()
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0].strip() or "sound"
        stem = stem.replace("/", "-").replace(":", "-")
        ext = target_ext(f)
        name = stem + ext
        i = 2
        while name.lower() in used:
            name = "%s (%d)%s" % (stem, i, ext)
            i += 1
        used.add(name.lower())
        out[f] = name
    return out


def convert(src, dest):
    """Copy what plays natively; otherwise convert with ffmpeg. The
    originals are only ever read."""
    ext = os.path.splitext(src)[1].lower()
    tmp = dest + ".part" + os.path.splitext(dest)[1]
    if ext in NATIVE_EXT and (ext != ".wav" or wav_is_plain_pcm(src)):
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dest)
            return None
        except OSError as e:
            return [str(e)]
    if dest.endswith(".m4a"):
        codec = ["-c:a", "aac", "-b:a", "192k"]
    else:
        codec = ["-c:a", "pcm_s16le"]
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-vn",
                        "-map", "0:a:0"] + codec + [tmp],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 64:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return (r.stderr or "ffmpeg failed").strip().splitlines()[-1:] or ["ffmpeg failed"]
    os.replace(tmp, dest)
    return None


def allow_materialize():
    """Let reads download online-only files (Dropbox, iCloud, OneDrive).

    A process can inherit an I/O policy that forbids materializing
    'dataless' File Provider files; reads then fail with EDEADLK
    ("Resource deadlock avoided"). Turn it on for this process and its
    children (ffmpeg)."""
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        # IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_PROCESS, ON
        libc.setiopolicy_np(3, 0, 2)
    except (OSError, AttributeError):
        pass


def cmd_import(args):
    allow_materialize()
    ev = Events()
    if not shutil.which("ffmpeg"):
        os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")
        if not shutil.which("ffmpeg"):
            fail("ffmpeg is needed to convert sounds. Install it with Homebrew: brew install ffmpeg.")
    overrides = (mapping("overrides.json") or {}).get("schemes", {})
    r = rules()
    report, summary = [], []
    total_kept = total_dupes = total_errors = 0
    existing = all_schemes()
    for folder in args.folders:
        root, cands, odd, event_maps = walk_import(folder, ev)
        for c in cands:
            classify_candidate(c, ev, event_maps)
        cands = [c for c in cands if c.files]
        cands.sort(key=lambda c: (order_of(c.name), c.rel.lower(), c.name.lower()))
        # read every file once: hashes find duplicate schemes, and reading
        # is what pulls an online-only Dropbox file down
        allfiles = sorted({f for c in cands for f in c.files})
        hashes, errors = {}, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(file_hash, f): f for f in allfiles}
            for fut in concurrent.futures.as_completed(futs):
                f = futs[fut]
                try:
                    hashes[f] = fut.result()
                except OSError as e:
                    errors[f] = str(e)
        # schemes already imported from another folder count as seen, so the
        # same sounds found twice are imported once
        seen = {}
        for s in existing:
            if s.get("import_root") != root and s.get("signature"):
                seen[s["signature"]] = s["name"]
        kept = []
        for c in cands:
            c.files = [f for f in c.files if f in hashes]
            for e in list(c.events):
                c.events[e] = [f for f in c.events[e] if f in hashes]
                if not c.events[e]:
                    del c.events[e]
            c.unmapped = [f for f in c.unmapped if f in hashes]
            sig = hashlib.sha1("\n".join(sorted(hashes[f] for f in c.files)).encode()).hexdigest() if c.files else ""
            c.signature = sig
            if sig and sig in seen:
                first = seen[sig]
                if isinstance(first, str):
                    report.append("%s: same sounds as the already imported %s, skipped." % (c.name, first))
                else:
                    first.also.append(c.name)
                total_dupes += 1
                continue
            seen[sig] = c
            kept.append(c)
        # two schemes with one name (two Jungles): number the later ones
        counts = {}
        taken = {x["name"].lower() for x in existing if x.get("import_root") != root}
        for c in kept:
            if c.name.lower() in taken:
                c.name = "%s (%s)" % (c.name, display_name(root, "", {}))
            k = c.name.lower()
            counts[k] = counts.get(k, 0) + 1
            if counts[k] > 1:
                c.name = "%s %d" % (c.name, counts[k])
        # ids: unique across this import and against schemes from other roots
        others = {s["id"] for s in existing if s.get("import_root") != root}
        ids = set(others)
        for c in kept:
            base = slug(c.name)
            cid, i = base, 2
            while cid in ids:
                cid = "%s-%d" % (base, i)
                i += 1
            ids.add(cid)
            c.id = cid
        if args.dry_run:
            lines = []
            for c in kept:
                lines.append("%s: %s, %s unmapped%s%s" % (
                    c.name, plural(len(c.events), "event"), len(c.unmapped),
                    (" (same as: %s)" % ", ".join(c.also)) if c.also else "",
                    (" [map: %s]" % c.theme_file) if c.theme_file else ""))
                if args.verbose:
                    for e, fs in c.events.items():
                        lines.append("    %s = %s" % (e, ", ".join(os.path.basename(f) for f in fs[:3])))
                    if c.unmapped:
                        lines.append("    unmapped: " + ", ".join(os.path.basename(f) for f in c.unmapped[:30]))
            summary.append("%s: %s would be imported, %d duplicates folded in, %d unreadable." % (
                root, plural(len(kept), "scheme"), total_dupes, len(errors)))
            total_dupes = 0
            report.extend(lines)
            continue
        # replace what an earlier import of this same root wrote
        for s in existing:
            if s.get("import_root") == root:
                shutil.rmtree(s["_dir"], ignore_errors=True)
        jobs = []
        for c in kept:
            c.dir = os.path.join(SCHEMES, c.id)
            shutil.rmtree(c.dir, ignore_errors=True)
            os.makedirs(c.dir, exist_ok=True)
            c.dest = dest_names(c.files)
            for f in c.files:
                jobs.append((c, f))
        failures = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
            futs = {pool.submit(convert, f, os.path.join(c.dir, c.dest[f])): (c, f) for c, f in jobs}
            for fut in concurrent.futures.as_completed(futs):
                c, f = futs[fut]
                err = fut.result()
                if err:
                    failures[(c.id, f)] = err[0]
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        for c in kept:
            ok = [f for f in c.files if (c.id, f) not in failures]
            events = {}
            for e in sorted(c.events, key=lambda e: list(ev.by_id).index(e) if e in ev.by_id else 999):
                fs = [c.dest[f] for f in c.events[e] if f in ok]
                if fs:
                    events[e] = fs
            ov = overrides.get(c.id, {})
            name = ov.get("name", c.name)
            for e, val in (ov.get("events") or {}).items():
                vals = val if isinstance(val, list) else [val]
                resolved = []
                for v in vals:
                    for f in ok:
                        if v.lower() in (os.path.basename(f).lower(), c.dest[f].lower()):
                            resolved.append(c.dest[f])
                            break
                if resolved or vals == []:
                    events[e] = resolved
                if not resolved and vals:
                    report.append("Override for %s %s names a file that is not there: %s" % (c.id, e, vals))
            used = {f for fs in events.values() for f in fs}
            source = (c.manifest.get("source") or c.manifest.get("origin")) if c.manifest else None
            url = c.manifest.get("url") if c.manifest else None
            if source and re.match(r"https?://", source):
                url, source = url or source, None
            source = source or guess(r.get("source_by_name", {}), c.name, display_name(root, "", {}))
            lic = (c.manifest.get("license") or c.manifest.get("licence")) if c.manifest else None
            lic = lic or guess(r.get("license_by_source", {}), "%s %s" % (c.name, source), r.get("default_license", ""))
            doc = {
                "format": 1,
                "id": c.id,
                "name": name,
                "source": source,
                "license": lic,
                "order": order_of(name),
                "hidden": bool(ov.get("hide", False)),
                "theme_prefix": c.theme,
                "imported_from": c.path,
                "import_root": root,
                "imported": stamp,
                "also_in": c.also,
                "events": events,
                "unmapped": sorted(c.dest[f] for f in ok if c.dest[f] not in used),
                "files": {c.dest[f]: os.path.relpath(f, root) for f in ok},
            }
            if url:
                doc["url"] = url
            if c.theme_file:
                doc["event_map_from"] = c.theme_file
            doc["signature"] = c.signature
            save_json(os.path.join(c.dir, "scheme.json"), doc)
            total_kept += 1
            report.append("%s [%s]: %s; %d unmapped%s%s" % (
                name, c.id, plural(len(events), "event"), len(doc["unmapped"]),
                (": " + ", ".join(doc["unmapped"][:40]) + (" ..." if len(doc["unmapped"]) > 40 else "")) if doc["unmapped"] else "",
                ("; same sounds as " + ", ".join(c.also)) if c.also else ""))
        for (cid, f), err in sorted(failures.items()):
            report.append("Could not convert %s (%s): %s" % (os.path.relpath(f, root), cid, err))
        for f, err in sorted(errors.items()):
            report.append("Could not read %s: %s" % (os.path.relpath(f, root), err))
        for f in odd:
            report.append("Not audio, skipped: %s" % f)
        total_errors += len(failures) + len(errors)
        summary.append("%s: %s imported, %d duplicates folded in, %d files could not be read or converted." % (
            os.path.basename(root), plural(len(kept), "scheme"), total_dupes, len(failures) + len(errors)))
        total_dupes = 0
        existing = all_schemes()
    if args.dry_run:
        say("Dry run, nothing copied. " + " ".join(summary) + "\n" + "\n".join(report))
        return
    install_mappings()
    old = load_json(REPORT + ".json", {}) or {}
    with open(REPORT, "w") as f:
        f.write("WinSounds import, %s\n\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        f.write("\n".join(summary) + "\n\n" + "\n".join(report) + "\n")
    cfg = load_config()
    if not cfg.get("scheme") or not current_scheme(cfg):
        try:
            cfg["scheme"] = find_scheme(DEFAULT_SCHEME_HINT)["id"]
        except SystemExit:
            sch = [s for s in all_schemes() if s.get("events")]
            cfg["scheme"] = sch[0]["id"] if sch else ""
        save_config(cfg)
    else:
        notify(NOTIFY_RELOAD)
    log("import %s: %s" % (", ".join(args.folders), " ".join(summary)))
    say(" ".join(summary) + " Report: %s." % REPORT)


def install_mappings():
    os.makedirs(INSTALLED_MAPPINGS, exist_ok=True)
    for n in ("events.json", "platforms.json", "rules.json", "overrides.json"):
        src = os.path.join(MAPPINGS, n)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(INSTALLED_MAPPINGS, n))


# ---------------------------------------------------------------- commands

def cmd_list(args):
    ev = Events()
    cfg = load_config()
    schemes = all_schemes()
    if not schemes:
        say("No schemes yet. Run: winsounds import <folder>.")
        return
    shown = [s for s in schemes if args.all or (not s.get("hidden") and s.get("events"))]
    lines = []
    for i, s in enumerate(shown, 1):
        n, mac = scheme_counts(s, ev)
        cur = ", current" if s["id"] == cfg.get("scheme") else ""
        ident = " [%s]" % s["id"] if args.ids else ""
        lines.append("%d. %s%s: %s, %d the Mac plays%s" % (i, s["name"], ident, plural(n, "event"), mac, cur))
    hidden = len(schemes) - len(shown)
    tail = (" %d more with no matched events; winsounds list --all shows them." % hidden) if hidden else ""
    say("%s.%s\n%s" % (plural(len(shown), "scheme"), tail, "\n".join(lines)))


def cmd_use(args):
    ev = Events()
    s = find_scheme(" ".join(args.scheme))
    cfg = load_config()
    cfg["scheme"] = s["id"]
    save_config(cfg)
    n, mac = scheme_counts(s, ev)
    extra = ""
    if cfg.get("alert") and cfg.get("enabled"):
        extra = " " + alert_install(s, ev, dry=False)
    say("Now using %s: %s, %d the Mac plays.%s" % (s["name"], plural(n, "event"), mac, extra))


def cmd_status(args):
    ev = Events()
    cfg = load_config()
    s = current_scheme(cfg)
    pid = agent_running()
    parts = []
    parts.append("WinSounds is %s" % ("on" if cfg["enabled"] else "off"))
    if cfg["muted"]:
        parts.append("muted")
    parts.append("scheme %s" % (s["name"] if s else "none"))
    parts.append("volume %d" % cfg["volume"])
    parts.append("alert sound %s" % ("on" if cfg["alert"] else "off"))
    if pid:
        parts.append("agent running, pid %d" % pid)
    elif launchd_loaded():
        parts.append("agent loaded but not running")
    elif os.path.exists(PLIST):
        parts.append("agent installed but not started (winsounds install starts it)")
    else:
        parts.append("agent not installed")
    text = ", ".join(parts) + "."
    if s:
        n, mac = scheme_counts(s, ev)
        text += " The scheme maps %s, %d the Mac plays." % (plural(n, "event"), mac)
    st = load_json(AGENT_STATE, {}) or {}
    last = (st.get("recent") or [])[-1:] if pid else []
    if last:
        text += " Last sound: %s." % last[0]
    off = [e for e, v in cfg["toggles"].items() if not v]
    if off:
        text += " Off: %s." % ", ".join(sorted(off))
    if st.get("playback_disabled") and pid:
        text += " Playback is off because the audio output stopped answering; run winsounds restart once sound works."
    if not st.get("trash_readable", True) and pid:
        text += " The agent cannot read the Trash, so emptying it plays nothing; give winsounds-agent Full Disk Access to fix that."
    say(text)


def cmd_events(args):
    ev = Events()
    cfg = load_config()
    s = find_scheme(args.scheme) if args.scheme else current_scheme(cfg)
    if not s:
        fail("No scheme chosen. Run: winsounds use <scheme>.")
    lines = []
    if args.all:
        for e in ev.list:
            files = s.get("events", {}).get(e["id"])
            if files:
                more = " (+%d more)" % (len(files) - 1) if len(files) > 1 else ""
                lines.append("%s: %s%s" % (e["id"], files[0], more))
        say("%s maps %s.\n%s" % (s["name"], plural(len(lines), "event"), "\n".join(lines)))
        return
    for e in mac_events():
        on = cfg["toggles"].get(e, True)
        path, used = resolve(s, e, ev)
        if path:
            via = "" if used == e else " via %s" % used
            what = "%s%s" % (os.path.basename(path), via)
        else:
            what = "no sound in this scheme"
        lines.append("%s: %s, %s" % (e, "on" if on else "off", what))
    for d, label in DETECTORS.items():
        lines.append("%s: %s (%s)" % (d, "on" if cfg["toggles"].get(d, True) else "off", label))
    say("%s, events the Mac plays:\n%s" % (s["name"], "\n".join(lines)))


def cmd_set(args):
    ev = Events()
    cfg = load_config()
    name = args.event.lower().replace("-", "_")
    if name not in ev.by_id and name not in DETECTORS:
        eid = ev.event_for_name(args.event)
        if not eid:
            fail("Unknown event %s. winsounds events lists them." % args.event)
        name = eid
    cfg["toggles"][name] = args.state == "on"
    save_config(cfg)
    note = ""
    if name in ev.by_id and name not in platform_defaults():
        note = " The Mac never triggers that one on its own, so this only matters for previews and fallbacks."
    say("%s is %s.%s" % (name, args.state, note))


def cmd_preview(args):
    ev = Events()
    cfg = load_config()
    s = find_scheme(args.scheme) if args.scheme else current_scheme(cfg)
    if not s:
        fail("No scheme chosen. Run: winsounds use <scheme>.")
    name = args.event.lower().replace("-", "_")
    if name not in ev.by_id:
        name = ev.event_for_name(args.event) or name
    if name not in ev.by_id:
        # maybe it is a file name in the scheme
        for f in s.get("files", {}):
            if norm(os.path.splitext(f)[0]) == norm(args.event):
                path = os.path.join(s["_dir"], f)
                if not args.dry_run:
                    afplay(path, args.volume if args.volume is not None else cfg["volume"], limit=15)
                say("%s %s from %s." % ("Would play" if args.dry_run else "Played", f, s["name"]))
                return
        fail("Unknown event %s." % args.event)
    path, used = resolve(s, name, ev)
    if not path:
        say("%s has no sound for %s." % (s["name"], name))
        return
    via = "" if used == name else ", via %s" % used
    if args.out:
        out = render([path], os.path.abspath(os.path.expanduser(args.out)),
                     args.volume if args.volume is not None else 100)
        say("Wrote %s for %s%s to %s." % (os.path.basename(path), name, via, out))
        return
    if args.dry_run:
        say("Would play %s for %s%s." % (os.path.basename(path), name, via))
        return
    afplay(path, args.volume if args.volume is not None else cfg["volume"], limit=15)
    say("Played %s for %s%s." % (os.path.basename(path), name, via))


HIGHLIGHTS = ["startup", "login", "default_beep", "exclamation", "critical_stop", "notification",
              "device_connect", "device_disconnect", "empty_trash", "new_mail", "logout", "shutdown"]


def cmd_preview_scheme(args):
    ev = Events()
    cfg = load_config()
    s = find_scheme(" ".join(args.scheme))
    picks, seen = [], set()
    wanted = HIGHLIGHTS
    if args.events:
        wanted = [ev.event_for_name(x) or x.strip() for x in args.events.split(",") if x.strip()]
        args.count = max(args.count, len(wanted))
    for e in wanted:
        files = s.get("events", {}).get(e)
        if files and files[0] not in seen:
            seen.add(files[0])
            picks.append((e, os.path.join(s["_dir"], files[0])))
    if not picks:
        for e, files in s.get("events", {}).items():
            if files and files[0] not in seen:
                seen.add(files[0])
                picks.append((e, os.path.join(s["_dir"], files[0])))
    picks = picks[:args.count]
    if not picks:
        say("%s has no mapped sounds to preview." % s["name"])
        return
    names = ", ".join(ev.label(e).lower() for e, _ in picks)
    if args.out:
        out = render([p for _, p in picks], os.path.abspath(os.path.expanduser(args.out)),
                     args.volume if args.volume is not None else 100, gap=args.gap, limit=args.limit)
        say("Wrote %s from %s to %s: %s." % (plural(len(picks), "sound"), s["name"], out, names))
        return
    if args.dry_run:
        say("Would play %s from %s: %s." % (plural(len(picks), "sound"), s["name"], names))
        return
    # announce once, wait so VoiceOver finishes, then play with gaps
    say("Playing %s from %s: %s." % (plural(len(picks), "sound"), s["name"], names))
    try:
        time.sleep(args.wait)
        vol = args.volume if args.volume is not None else cfg["volume"]
        for i, (e, p) in enumerate(picks):
            if i:
                time.sleep(args.gap)
            afplay(p, vol, limit=args.limit)
    except KeyboardInterrupt:
        pass


def cmd_volume(args):
    cfg = load_config()
    if args.level is None:
        say("Volume is %d." % cfg["volume"])
        return
    v = max(0, min(100, args.level))
    cfg["volume"] = v
    save_config(cfg)
    say("Volume is %d." % v)


def cmd_flag(key, value, sentence):
    def run(args):
        cfg = load_config()
        cfg[key] = value
        save_config(cfg)
        extra = ""
        if key == "enabled":
            s = current_scheme(cfg)
            if not value:
                extra = " " + alert_restore(dry=False, quiet=True)
            elif cfg.get("alert") and s:
                extra = " " + alert_install(s, Events(), dry=False)
        say((sentence + extra).strip())
    return run


# ----------------------------------------------------------- alert sound

def defaults_read_beep():
    r = subprocess.run(["/usr/bin/defaults", "read", "-g", BEEP_KEY],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def alert_install(s, ev, dry):
    path, used = resolve(s, "default_beep", ev)
    if not path:
        return "%s has no alert sound, so the alert is unchanged." % s["name"]
    dest = os.path.join(USER_SOUNDS, "%s%s.aiff" % (ALERT_PREFIX, re.sub(r"[/:]", "-", s["name"])))
    if dry:
        return "Would set the alert sound to %s from %s." % (os.path.basename(path), s["name"])
    state = load_json(STATE, {}) or {}
    if not state.get("alert_installed"):
        state["alert_previous"] = defaults_read_beep()
    os.makedirs(USER_SOUNDS, exist_ok=True)
    tmp = dest + ".part.aiff"
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path, "-c:a", "pcm_s16be", tmp],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                       env=dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")))
    if r.returncode != 0:
        return "Could not convert the alert sound: %s" % r.stderr.strip()
    os.replace(tmp, dest)
    for f in os.listdir(USER_SOUNDS):
        if f.startswith(ALERT_PREFIX) and os.path.join(USER_SOUNDS, f) != dest:
            try:
                os.remove(os.path.join(USER_SOUNDS, f))
            except OSError:
                pass
    subprocess.run(["/usr/bin/defaults", "write", "-g", BEEP_KEY, "-string", dest], check=False)
    state["alert_installed"] = True
    state["alert_path"] = dest
    save_json(STATE, state)
    log("alert sound set to %s (previous %s)" % (dest, state.get("alert_previous")))
    return "The alert sound is now %s from %s." % (os.path.basename(path), s["name"])


def alert_restore(dry, quiet=False):
    state = load_json(STATE, {}) or {}
    if not state.get("alert_installed"):
        return "" if quiet else "The alert sound was not changed by WinSounds, so there is nothing to restore."
    prev = state.get("alert_previous")
    if dry:
        return "Would restore the alert sound to %s." % (prev or "the system default")
    if prev:
        subprocess.run(["/usr/bin/defaults", "write", "-g", BEEP_KEY, "-string", prev], check=False)
    else:
        subprocess.run(["/usr/bin/defaults", "delete", "-g", BEEP_KEY], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for f in os.listdir(USER_SOUNDS):
            if f.startswith(ALERT_PREFIX):
                os.remove(os.path.join(USER_SOUNDS, f))
    except OSError:
        pass
    state["alert_installed"] = False
    state.pop("alert_path", None)
    save_json(STATE, state)
    log("alert sound restored to %s" % prev)
    return "The alert sound is back to %s." % (os.path.basename(prev) if prev else "the system default")


def cmd_alert(args):
    ev = Events()
    cfg = load_config()
    if args.state is None:
        state = load_json(STATE, {}) or {}
        cur = defaults_read_beep()
        say("Alert option is %s. The alert sound is %s." % (
            "on" if cfg["alert"] else "off", os.path.basename(cur) if cur else "the system default"))
        return
    if args.state == "on":
        s = current_scheme(cfg)
        if not s:
            fail("No scheme chosen. Run: winsounds use <scheme>.")
        msg = alert_install(s, ev, args.dry_run)
        if not args.dry_run:
            cfg["alert"] = True
            save_config(cfg)
        say(msg)
    else:
        msg = alert_restore(args.dry_run)
        if not args.dry_run:
            cfg["alert"] = False
            save_config(cfg)
        say(msg)


# ------------------------------------------------------- simulate / log

def cmd_simulate(args):
    ev = Events()
    name = args.event.lower().replace("-", "_")
    if name not in ev.by_id and name not in ("usb_connect", "usb_disconnect"):
        name = ev.event_for_name(args.event) or name
    if name not in ev.by_id:
        fail("Unknown event %s." % args.event)
    pid = agent_running()
    if not pid:
        fail("The agent is not running, so there is nothing to simulate against. Run: winsounds install.")
    save_json(SIMULATE, {"event": name, "dry_run": bool(args.dry_run), "at": time.time()})
    notify(NOTIFY_SIMULATE)
    # wait for the agent to log the result
    deadline = time.time() + 3
    answer = None
    while time.time() < deadline:
        time.sleep(0.2)
        st = load_json(AGENT_STATE, {}) or {}
        last = st.get("last_simulated") or {}
        if last.get("at") and abs(last.get("at", 0) - load_json(SIMULATE, {}).get("at", -1)) < 0.001:
            answer = last.get("result")
            break
    say(answer or "Sent %s to the agent; it did not answer within 3 seconds. See winsounds log." % name)


def cmd_log(args):
    try:
        with open(LOG) as f:
            lines = f.readlines()[-args.lines:]
    except OSError:
        say("No log yet.")
        return
    say("".join(lines).rstrip("\n") or "The log is empty.")


# ------------------------------------------------------- install / build

PLIST_TEXT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>{agent}</string>{extra}</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>{home}/.winsounds-agent.out</string>
  <key>StandardErrorPath</key><string>{home}/.winsounds-agent.out</string>
</dict>
</plist>
"""


def build_agent():
    src = os.path.join(REPO, "agent", "main.swift")
    out_dir = os.path.join(REPO, "build")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "winsounds-agent")
    r = subprocess.run(["/usr/bin/swiftc", "-O", "-o", out, src],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        fail("The agent did not compile:\n" + r.stdout)
    return out


def bootout():
    # a quiet-stop marker tells the agent this SIGTERM is not a logout
    try:
        open(QUIET_STOP, "w").close()
    except OSError:
        pass
    subprocess.run(["/bin/launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if not launchd_loaded():
            break
        time.sleep(0.2)


def cmd_install(args):
    built = build_agent() if not args.no_build else os.path.join(REPO, "build", "winsounds-agent")
    install_mappings()
    os.makedirs(os.path.dirname(AGENT_BIN), exist_ok=True)
    bootout()
    tmp = AGENT_BIN + ".new"
    shutil.copy2(built, tmp)
    os.replace(tmp, AGENT_BIN)
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    extra = "<string>--dry-run</string>" if args.log_only else ""
    with open(PLIST, "w") as f:
        f.write(PLIST_TEXT.format(label=LABEL, agent=AGENT_BIN, home=HOME, extra=extra))
    if args.no_start:
        say("Built and copied the agent and wrote %s, but did not start it." % PLIST)
        return
    subprocess.run(["/bin/launchctl", "enable", "gui/%d/%s" % (os.getuid(), LABEL)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cfg = load_config()
    save_config(cfg, reload=False)
    r = subprocess.run(["/bin/launchctl", "bootstrap", "gui/%d" % os.getuid(), PLIST],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        fail("launchctl bootstrap failed: %s" % r.stdout.strip())
    for _ in range(25):
        time.sleep(0.2)
        if agent_running():
            break
    pid = agent_running()
    log("installed agent, pid %s" % pid)
    say("Installed. The agent %s and starts at every login.%s" % (
        "is running, pid %d," % pid if pid else "did not report in yet; see winsounds log,",
        " Log-only mode: it logs what it would play and plays nothing." if args.log_only else ""))


def cmd_uninstall(args):
    bootout()
    subprocess.run(["/bin/launchctl", "disable", "gui/%d/%s" % (os.getuid(), LABEL)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.remove(PLIST)
    except OSError:
        pass
    msg = alert_restore(dry=False, quiet=True)
    log("uninstalled agent")
    say(("The agent is stopped and will not start at login. Schemes and settings are kept in %s. %s" % (SUPPORT, msg)).strip())


def cmd_restart(args):
    bootout()
    subprocess.run(["/bin/launchctl", "bootstrap", "gui/%d" % os.getuid(), PLIST],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(25):
        time.sleep(0.2)
        if agent_running():
            break
    pid = agent_running()
    say("Agent restarted, pid %d." % pid if pid else "The agent did not come back; see winsounds log.")


# -------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(prog="winsounds", description="Sound schemes for macOS events: Windows, Mac, Android, any folder of sounds.")
    p.add_argument("--version", action="version", version="winsounds " + VERSION)
    p.add_argument("--status", action="store_true", help="same as the status command")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("list", help="list the imported schemes")
    a.add_argument("--all", action="store_true", help="include schemes with no matched events")
    a.add_argument("--ids", action="store_true", help="show each scheme's id, for overrides.json")
    a.set_defaults(func=cmd_list)

    a = sub.add_parser("use", help="switch to a scheme (any unique part of its name)")
    a.add_argument("scheme", nargs="+", help="scheme name or id, e.g. xp pro")
    a.set_defaults(func=cmd_use)

    a = sub.add_parser("status", help="one sentence: on or off, scheme, volume, agent")
    a.set_defaults(func=cmd_status)

    a = sub.add_parser("events", help="each event the Mac plays: on or off, and its sound")
    a.add_argument("--all", action="store_true", help="every event the scheme maps, Mac or not")
    a.add_argument("--scheme", help="another scheme instead of the current one")
    a.set_defaults(func=cmd_events)

    a = sub.add_parser("set", help="turn one event, or the usb or bluetooth detector, on or off")
    a.add_argument("event", help="event id such as app_open, or usb, bluetooth")
    a.add_argument("state", choices=["on", "off"], help="on or off")
    a.set_defaults(func=cmd_set)

    a = sub.add_parser("preview", help="play one event's sound (or a file in the scheme)")
    a.add_argument("event", help="event id, such as startup or device_connect")
    a.add_argument("--scheme", help="another scheme instead of the current one")
    a.add_argument("--volume", type=int, help="0 to 100, this time only")
    a.add_argument("--dry-run", action="store_true", help="say what would play, play nothing")
    a.add_argument("--out", help="write the sound to this file (.wav, .aiff, .m4a, .mp3) instead of playing it; full volume unless --volume")
    a.set_defaults(func=cmd_preview)

    a = sub.add_parser("preview-scheme", help="play a scheme's highlights with pauses between")
    a.add_argument("scheme", nargs="+", help="scheme name or id")
    a.add_argument("--count", type=int, default=6, help="how many sounds, default 6")
    a.add_argument("--gap", type=float, default=0.8, help="seconds between sounds, default 0.8")
    a.add_argument("--wait", type=float, default=2.5, help="seconds to wait after announcing, default 2.5")
    a.add_argument("--limit", type=int, default=8, help="longest seconds per sound, default 8")
    a.add_argument("--volume", type=int, help="0 to 100, this time only")
    a.add_argument("--dry-run", action="store_true", help="say what would play, play nothing")
    a.add_argument("--out", help="write the highlights, with the gaps, to this file instead of playing them")
    a.add_argument("--events", help="comma-separated events to use instead of the highlights, in order")
    a.set_defaults(func=cmd_preview_scheme)

    a = sub.add_parser("alert", help="use the scheme's Default Beep as the Mac alert sound, or restore the old one")
    a.add_argument("state", nargs="?", choices=["on", "off"], help="on or off; leave out to hear the current state")
    a.add_argument("--dry-run", action="store_true", help="say what would change, change nothing")
    a.set_defaults(func=cmd_alert)

    a = sub.add_parser("volume", help="set or read the volume, 0 to 100")
    a.add_argument("level", nargs="?", type=int, help="0 to 100")
    a.set_defaults(func=cmd_volume)

    sub.add_parser("mute", help="silence every sound, keep everything else").set_defaults(
        func=cmd_flag("muted", True, "WinSounds is muted."))
    sub.add_parser("unmute", help="undo mute").set_defaults(
        func=cmd_flag("muted", False, "WinSounds is unmuted."))
    sub.add_parser("off", help="turn WinSounds off, restoring the Mac alert sound").set_defaults(
        func=cmd_flag("enabled", False, "WinSounds is off."))
    sub.add_parser("on", help="turn WinSounds back on").set_defaults(
        func=cmd_flag("enabled", True, "WinSounds is on."))

    a = sub.add_parser("import", help="scan folders for sound schemes and copy them in")
    a.add_argument("folders", nargs="+", help="folders to scan, e.g. a C:\\Windows\\Media copy")
    a.add_argument("--dry-run", action="store_true", help="show what would be imported, copy nothing")
    a.add_argument("--verbose", action="store_true", help="with --dry-run, list every event and unmapped file")
    a.set_defaults(func=cmd_import)

    a = sub.add_parser("simulate", help="send a fake event through the running agent")
    a.add_argument("event", help="event id, such as device_connect")
    a.add_argument("--dry-run", action="store_true", help="the agent logs what it would play, plays nothing")
    a.set_defaults(func=cmd_simulate)

    a = sub.add_parser("log", help="the last lines of ~/.winsounds.log")
    a.add_argument("lines", nargs="?", type=int, default=15, help="how many lines, default 15")
    a.set_defaults(func=cmd_log)

    a = sub.add_parser("install", help="build the agent and start it at login")
    a.add_argument("--no-build", action="store_true", help="install the already-built agent")
    a.add_argument("--log-only", action="store_true", help="the agent logs what it would play and plays nothing")
    a.add_argument("--no-start", action="store_true", help="copy the agent and write the LaunchAgent, but do not start it")
    a.set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="stop the agent and remove it from login").set_defaults(func=cmd_uninstall)
    sub.add_parser("restart", help="restart the agent").set_defaults(func=cmd_restart)

    args = p.parse_args()
    if args.status and not args.cmd:
        return cmd_status(args)
    if not args.cmd:
        return cmd_status(args)
    args.func(args)


if __name__ == "__main__":
    rules()
    main()
