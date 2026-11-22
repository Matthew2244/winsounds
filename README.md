# WinSounds

Sound schemes for macOS events: Windows 3.x to 11, the Plus! themes, classic
Mac OS alerts, Android sets, or any folder of sounds. A background agent plays
the current scheme's sound when you log in, connect a USB or Bluetooth device,
lock the screen, empty the Trash and so on. A CLI and a small settings window
control it, and both work well with VoiceOver.

**No audio ships with this repo.** You import sounds you already have with
`winsounds import <folder>`; they are copied into
`~/Library/Application Support/WinSounds/Schemes/`. Microsoft and Apple sounds
are theirs: personal use only. The repo holds code and filename mappings only.

## Install

    ./winsounds.py install          # builds the agent, starts it at login
    ln -s "$PWD/winsounds.py" ~/bin/winsounds
    ./build-app.sh                  # the settings window, into ~/Applications
    winsounds import "/path/to/sounds"

Needs the Xcode command line tools (`swiftc`, `/usr/bin/python3`) and ffmpeg
(`brew install ffmpeg`) for importing WMA, Ogg and odd WAV files. Nothing needs
admin rights.

## Commands

Every command prints one sentence or a short list, written once.

    winsounds status                 on or off, scheme, volume, agent
    winsounds list [--all] [--ids]   the schemes
    winsounds use <scheme>           any unique part of the name: "xp pro", "jungle"
    winsounds events [--all]         each event the Mac plays: on or off, and its file
    winsounds set <event> on|off     also: set usb off, set bluetooth off
    winsounds preview <event> [--scheme S] [--volume N] [--out file.wav]
    winsounds preview-scheme <scheme> [--events a,b,c] [--gap 0.8] [--out file.m4a]
    winsounds alert on|off           the scheme's Default Beep as the Mac alert sound
    winsounds volume N               0 to 100
    winsounds mute | unmute          silence sounds, keep settings
    winsounds off | on               off also puts back the Mac's alert sound
    winsounds import <folder>... [--dry-run [--verbose]]
    winsounds simulate <event> [--dry-run]   send a fake event through the running agent
    winsounds log [N]                ~/.winsounds.log
    winsounds install [--log-only] [--no-start] | uninstall | restart

`--out` renders to a file instead of the speakers (WAV, AIFF, M4A or MP3 by
extension, full volume unless `--volume`), for demos.

`alert on` copies the sound to `~/Library/Sounds/WinSounds - <scheme>.aiff`
and sets `com.apple.sound.beep.sound`, recording the previous value first.
`alert off` and `off` put it back exactly (or delete the key if it was unset).

## What the Mac triggers

`mappings/platforms.json` is the authority. On macOS:

| event | trigger |
|---|---|
| startup / login | agent start at login: startup within 15 min of boot, login otherwise; only if the console login is under 3 minutes old |
| shutdown | log out, restart, shut down: NSWorkspace will-power-off, then SIGTERM (best effort) |
| sleep / wake | IOKit system power; sleep is held up to 4 s so the sound finishes |
| lock / unlock | `com.apple.screenIsLocked` / `screenIsUnlocked` |
| device_connect / device_disconnect | `IOUSBHostDevice` arrivals and removals; Bluetooth HID devices (`IOHIDDevice` with Transport Bluetooth) |
| power_connected / power_disconnected, battery_low / battery_critical | IOPS power source changes, 10 % and 5 %; laptops and UPS only |
| network_up / network_down | NWPathMonitor, debounced 4 s, ignored for 20 s after wake |
| empty_trash | `~/.Trash` goes from having items to empty (entry count by `getattrlist`, so no Full Disk Access needed) |
| app_open / app_close | NSWorkspace, regular apps only. **Off by default** |
| new_mail | Mail's inbox unread count rises, polled only while Mail runs. **Off by default**; asks for Automation permission once |

Events with no sound of their own fall back (`fallback` in `events.json`): sleep
plays logout, wake plays login, power_connected plays device_connect, and so on.
`winsounds events` says "via logout" when that happens.

Bursts are coalesced: the same event within 2 s, or the same file within
1.5 s, plays once, and at most three sounds play at a time. Mute and off are
honoured at the moment of playing.

## The scheme format (platform-neutral)

Each imported scheme is a folder with its sounds and a `scheme.json`:

```json
{
  "format": 1,
  "id": "windows-xp-professional",
  "name": "Windows XP Professional",
  "source": "Microsoft Windows",
  "license": "Microsoft sounds. Personal use only; do not redistribute.",
  "events": {
    "startup": ["Windows XP Startup.wav", "tada.wav"],
    "device_connect": ["Windows XP Hardware Insert.wav"]
  },
  "unmapped": ["chimes.wav"],
  "files": {"Windows XP Startup.wav": "windows xp pro/Windows XP Startup.wav"},
  "also_in": ["Windows XP Home"],
  "order": 16, "hidden": false,
  "imported_from": "...", "import_root": "...", "imported": "2026-11-22T15:32:27",
  "signature": "sha1 of the sorted file hashes"
}
```

`events` maps a generic event id to one or more filenames in the folder, best
first. The other fields are bookkeeping. Another platform (an Android port)
needs only `name`, `source`, `license` and `events`.

`mappings/` holds the data that drives everything, all plain JSON meant to be
edited by hand:

- **`events.json`**: the generic events (`startup`, `login`, `logout`,
  `shutdown`, `sleep`, `wake`, `lock`, `unlock`, `device_connect`,
  `device_disconnect`, `device_fail`, `battery_low`, `battery_critical`,
  `power_connected`, `power_disconnected`, `network_up`, `network_down`,
  `default_beep`, `asterisk`, `exclamation`, `critical_stop`, `question`,
  `program_error`, `notification`, `new_mail`, `message`, `reminder`,
  `empty_trash`, `app_open`, `app_close`, `ringtone`, `alarm`, `camera` and
  more). Each has a label, the Windows name, filename aliases and fallbacks.
- **`platforms.json`**: which events each OS can trigger and what triggers
  them, with on-by-default flags. `macos` and `android` so far.
- **`rules.json`**: import rules. Folder names to scheme names, bundled theme
  prefixes (`Jungle`, `Musica`, `winAquarium`), folders to merge (`ogg/`,
  `effects/`), event folders (`ringtones/`, `alarms/`), Windows registry event
  names, format preference, sort order, licence guesses.
- **`overrides.json`**: hand fixes by scheme id, applied on every import (so a
  re-import keeps them). Rename, set an event's files, or hide a scheme.

## How import works

`winsounds import <folder>` walks the folder. Each folder of sounds is a
scheme; its files are matched to events by filename:

1. A `manifest.json` (a list of `{path, name, source, license, events}`, or one
   object per folder) gives names, licences and explicit mappings.
2. Windows `.Theme` and `.reg` files anywhere under the folder are parsed for
   their AppEvents entries. One that names at least half a folder's files
   maps it, which handles Plus! themes with 8.3 names like `7Bastrix.wav`.
3. Otherwise filenames are normalised (lowercase, letters and digits) and
   matched against the aliases, trying the whole name, the name without
   prefixes ("Windows XP", the theme, the folder name, whatever every file
   shares), without trailing digits or `48k`, without one trailing letter
   (Windows CE's `defaultm.wav`), prefix aliases, and finally as a truncated
   8.3 name (`wo_excla` is the start of `exclamation`).
4. Folders named for an event (`notifications/`, `windows start/`) give
   every file inside that event; format folders (`ogg/`, `wav/`) merge up.
5. When one sound exists in several formats, one is kept (`format_preference`).
6. Schemes with identical sounds are imported once, listed under `also_in`,
   even across separate imports.

Files the Mac plays natively (PCM WAV, AIFF, MP3, M4A) are copied untouched;
ADPCM and other odd WAVs and QuickTime audio become PCM WAV; WMA, Ogg, Opus and
FLAC become AAC. The originals are only ever read. Online-only Dropbox or
iCloud files are downloaded by the read (the importer turns on the
materialise-dataless-files I/O policy, which some parent processes switch off).

The import report (`~/Library/Application Support/WinSounds/import-report.txt`)
lists every scheme, its mapped events and every unmapped file.

## Files

    ~/bin/winsounds                      the CLI (symlink to winsounds.py)
    ~/bin/winsounds-agent                the agent binary (a copy, not a link)
    ~/Library/LaunchAgents/com.matthew.winsounds.plist
    ~/Library/Application Support/WinSounds/
        config.json      scheme, enabled, muted, volume, alert, toggles
        state.json       the alert sound's previous value
        agent.json       the agent's pid, recent sounds, playback state
        Schemes/<id>/    sounds plus scheme.json
        mappings/        a copy of mappings/ for the agent
    ~/Applications/WinSounds.app         the settings window
    ~/.winsounds.log                     CLI and agent log

The CLI tells the agent to reload with the Darwin notification
`com.matthew.winsounds.reload`; `simulate` uses `com.matthew.winsounds.simulate`.

## Playback, and the coreaudiod lesson

The agent plays every sound by spawning `/usr/bin/afplay`, with **process
responsibility disclaimed**, and it runs as a **plain run loop, not an
NSApplication**. That's deliberate. On 2026-11-22 the first version (an
`NSApplication` with activation policy `.prohibited`) wedged coreaudiod three
times: twice with AVAudioPlayer in the agent, and once with a plain `afplay` child.
Each time, every process on the Mac lost the ability to start a new sound
until coreaudiod was restarted. `afplay` from a shell, or from ordinary launchd
script jobs, never did. Dropping AppKit's application object and disclaiming
responsibility for the child fixed it. The agent also makes no Core Audio HAL
calls (no mute or volume queries, no device listeners): if the output is
muted, the sound plays into the muted output and is not heard.

A watchdog kills any `afplay` still running 5 s past its sound's length and
turns playback off (logged, shown by `status`) instead of piling up stuck
processes. `winsounds restart` turns it back on. The CLI previews and the
settings app use the same afplay-with-timeout path.

Test hooks: `WINSOUNDS_PLAYER` swaps the player binary (point it at a script
that hangs to exercise the watchdog), and `WINSOUNDS_TRASH_DIR` watches another
folder as the Trash. `winsounds-agent --dry-run` logs "would play" and plays
nothing, and `winsounds install --log-only` runs the agent that way.
