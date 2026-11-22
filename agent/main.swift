// winsounds-agent: plays the current sound scheme on Mac events.
//
// A background LaunchAgent (com.matthew.winsounds). It reads
// ~/Library/Application Support/WinSounds/config.json and the chosen
// scheme's scheme.json, listens for system events, and plays sounds through
// one afplay child per sound, so they mix, never block, and a wedged
// audio system can only hang a child the watchdog kills. The `winsounds` CLI edits the
// config and pokes this process over Darwin notifications.
//
// Build: swiftc -O -o build/winsounds-agent agent/main.swift
// Flags: --dry-run (log instead of playing), --no-login-sound, --foreground

import AppKit
import AudioToolbox
import Foundation
import IOKit
import IOKit.ps
import IOKit.pwr_mgt
import Network
import notify

let agentVersion = "1.0.0"
let label = "com.matthew.winsounds"
let home = FileManager.default.homeDirectoryForCurrentUser.path
let support = home + "/Library/Application Support/WinSounds"
let configPath = support + "/config.json"
let agentStatePath = support + "/agent.json"
let simulatePath = support + "/simulate.json"
let quietStopPath = support + "/quiet-stop"
let eventsPath = support + "/mappings/events.json"
let logPath = home + "/.winsounds.log"
// test hooks: another player binary (to test the watchdog), another Trash
let playerPath = ProcessInfo.processInfo.environment["WINSOUNDS_PLAYER"] ?? "/usr/bin/afplay"
let trashDir = ProcessInfo.processInfo.environment["WINSOUNDS_TRASH_DIR"] ?? (home + "/.Trash")

let args = Set(CommandLine.arguments.dropFirst())
let dryRunAll = args.contains("--dry-run")

// MARK: - logging

let logQueue = DispatchQueue(label: "log")
let stampFormat: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return f
}()

func log(_ s: String) {
    let line = "\(stampFormat.string(from: Date())) agent \(s)\n"
    logQueue.async {
        if let h = FileHandle(forWritingAtPath: logPath) {
            h.seekToEndOfFile()
            h.write(line.data(using: .utf8)!)
            h.closeFile()
        } else {
            FileManager.default.createFile(atPath: logPath, contents: line.data(using: .utf8))
        }
    }
}

func readJSON(_ path: String) -> [String: Any]? {
    guard let d = FileManager.default.contents(atPath: path),
          let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return nil }
    return o
}

func writeJSON(_ path: String, _ obj: [String: Any]) {
    guard let d = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]) else { return }
    let tmp = path + ".tmp"
    FileManager.default.createFile(atPath: tmp, contents: d)
    _ = rename(tmp, path)
}

// MARK: - no Core Audio HAL calls
//
// The agent deliberately makes no Core Audio HAL calls of its own: no mute or
// volume queries, no device listeners. On 2026-11-22 two sounds started
// right after those queries (on a PreSonus StudioLive 64S as the default
// output) each left coreaudiod unable to start any new output stream, for
// every process, until it was restarted. Playback is afplay only.

// MARK: - spawning afplay

typealias DisclaimFn = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
let disclaim: DisclaimFn? = {
    guard let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "responsibility_spawnattrs_setdisclaim") else { return nil }
    return unsafeBitCast(sym, to: DisclaimFn.self)
}()

/// posix_spawn with responsibility disclaimed when the system offers it.
func spawnDisclaimed(_ argv: [String]) -> pid_t? {
    var attr: posix_spawnattr_t?
    posix_spawnattr_init(&attr)
    defer { posix_spawnattr_destroy(&attr) }
    _ = disclaim?(&attr, 1)
    var actions: posix_spawn_file_actions_t?
    posix_spawn_file_actions_init(&actions)
    defer { posix_spawn_file_actions_destroy(&actions) }
    posix_spawn_file_actions_addopen(&actions, 1, "/dev/null", O_WRONLY, 0)
    posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0)
    var cargs = argv.map { strdup($0) } + [nil]
    defer { cargs.forEach { free($0) } }
    var pid: pid_t = 0
    let r = posix_spawn(&pid, argv[0], &actions, &attr, &cargs, environ)
    return r == 0 ? pid : nil
}

// MARK: - the agent

final class Agent: NSObject {
    var config: [String: Any] = [:]
    var scheme: [String: Any] = [:]
    var schemeDir = ""
    var fallbacks: [String: [String]] = [:]
    var players: [pid_t] = []
    var exitSources: [pid_t: DispatchSourceProcess] = [:]
    var playbackDisabled = false
    var lastEvent: [String: Date] = [:]
    var lastFile: [String: Date] = [:]
    var recent: [String] = []
    var started = Date()
    var lastWake = Date.distantPast
    var shutdownPlayed = false
    var trashCount: Int? = nil
    var trashReadable = true
    var trashSource: DispatchSourceFileSystemObject?
    var netStatus: NWPath.Status? = nil
    var netAnnounced: NWPath.Status? = nil
    var netWork: DispatchWorkItem?
    let netMonitor = NWPathMonitor()
    var powerOnAC: Bool? = nil
    var batteryLowFired = false
    var batteryCriticalFired = false
    var mailUnread: Int? = nil
    var mailTimer: Timer?
    var notifyPort: IONotificationPortRef?
    var iterators: [io_iterator_t] = []
    var rootPort: io_connect_t = 0
    var powerNotifier: io_object_t = 0
    var powerNotifyPort: IONotificationPortRef?
    var notifyTokens: [Int32] = []
    var signalSources: [DispatchSourceSignal] = []
    var lastSimulated: [String: Any] = [:]

    // MARK: config

    func toggle(_ key: String) -> Bool {
        (config["toggles"] as? [String: Any])?[key] as? Bool ?? true
    }

    var enabled: Bool { config["enabled"] as? Bool ?? true }
    var muted: Bool { config["muted"] as? Bool ?? false }
    var volume: Float { Float((config["volume"] as? NSNumber)?.doubleValue ?? 60) / 100 }

    func loadEvents() {
        guard let e = readJSON(eventsPath), let list = e["events"] as? [[String: Any]] else {
            log("cannot read \(eventsPath); fallbacks off")
            return
        }
        for item in list {
            if let id = item["id"] as? String {
                fallbacks[id] = item["fallback"] as? [String] ?? []
            }
        }
    }

    func reload(reason: String) {
        config = readJSON(configPath) ?? [:]
        let id = config["scheme"] as? String ?? ""
        schemeDir = support + "/Schemes/" + id
        scheme = readJSON(schemeDir + "/scheme.json") ?? [:]
        loadEvents()
        let name = scheme["name"] as? String ?? "no scheme"
        log("config loaded (\(reason)): \(name), \(enabled ? "on" : "off")\(muted ? ", muted" : ""), volume \(Int(volume * 100))")
        updateMailPolling()
        writeState()
    }

    func chain(_ event: String) -> [String] {
        var out: [String] = []
        var queue = [event]
        while !queue.isEmpty {
            let e = queue.removeFirst()
            if out.contains(e) { continue }
            out.append(e)
            queue.append(contentsOf: fallbacks[e] ?? [])
        }
        return out
    }

    func resolve(_ event: String) -> (String, String)? {
        let events = scheme["events"] as? [String: Any] ?? [:]
        for e in chain(event) {
            let files: [String]
            if let l = events[e] as? [String] { files = l } else if let s = events[e] as? String { files = [s] } else { continue }
            for f in files {
                let p = schemeDir + "/" + f
                if FileManager.default.fileExists(atPath: p) { return (p, e) }
            }
        }
        return nil
    }

    // MARK: playing

    /// Play the scheme's sound for an event. Returns a one-sentence result.
    @discardableResult
    func fire(_ event: String, detail: String = "", dryRun: Bool = false, force: Bool = false) -> String {
        let what = detail.isEmpty ? event : "\(event) (\(detail))"
        func done(_ s: String) -> String {
            log("\(what): \(s)")
            return s
        }
        if !enabled { return done("WinSounds is off, nothing played.") }
        if muted { return done("WinSounds is muted, nothing played.") }
        if !toggle(event) { return done("\(event) is turned off, nothing played.") }
        guard let (path, used) = resolve(event) else {
            return done("the scheme has no sound for \(event).")
        }
        let file = (path as NSString).lastPathComponent
        let via = used == event ? "" : " via \(used)"
        let now = Date()
        // coalesce bursts: a hub full of devices, a flapping network
        if !force, let t = lastEvent[event], now.timeIntervalSince(t) < 2.0 {
            return done("coalesced with the same event \(String(format: "%.1f", now.timeIntervalSince(t)))s ago.")
        }
        if !force, let t = lastFile[path], now.timeIntervalSince(t) < 1.5 {
            return done("coalesced, \(file) just played.")
        }
        players.removeAll { kill($0, 0) != 0 }
        if !force && players.count >= 3 {
            return done("dropped, three sounds already playing.")
        }
        if playbackDisabled {
            return done("skipped, playback is off since the audio output stopped answering; run winsounds restart once sound works.")
        }
        lastEvent[event] = now
        lastFile[path] = now
        if dryRun || dryRunAll {
            remember("\(event): \(file) (dry run)")
            return done("would play \(file)\(via).")
        }
        // One afplay child per sound: the simplest proven path. Spawned with
        // responsibility disclaimed, so macOS treats afplay as its own
        // process, not as audio from this agent. If audio still wedges, only
        // the child hangs; the watchdog kills it and turns playback off.
        let length = fileDuration(path)
        let argv = [playerPath, "-v", String(format: "%.3f", volume), "-t", String(format: "%.1f", min(length, 30) + 0.5), path]
        guard let pid = spawnDisclaimed(argv) else { return done("could not start afplay.") }
        let src = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit, queue: .main)
        src.setEventHandler {
            var status: Int32 = 0
            waitpid(pid, &status, 0)
            self.players.removeAll { $0 == pid }
            self.exitSources[pid]?.cancel()
            self.exitSources[pid] = nil
        }
        exitSources[pid] = src
        src.resume()
        players.append(pid)
        let limit = min(length, 30) + 5
        DispatchQueue.main.asyncAfter(deadline: .now() + limit) {
            guard self.players.contains(pid) else { return }
            kill(pid, SIGKILL)
            self.playbackDisabled = true
            log("watchdog: \(file) was still playing after \(Int(limit))s; killed it and turned playback off (audio output not answering?). Run winsounds restart once sound works.")
            self.writeState()
        }
        remember("\(event): \(file)")
        return done("played \(file)\(via), \(String(format: "%.1f", length))s.")
    }

    /// Length from the file itself (AudioFile), without touching any device.
    func fileDuration(_ path: String) -> TimeInterval {
        var af: AudioFileID?
        guard AudioFileOpenURL(URL(fileURLWithPath: path) as CFURL, .readPermission, 0, &af) == noErr, let f = af else { return 5 }
        defer { AudioFileClose(f) }
        var d: Float64 = 0
        var size = UInt32(MemoryLayout<Float64>.size)
        guard AudioFileGetProperty(f, kAudioFilePropertyEstimatedDuration, &size, &d) == noErr, d > 0 else { return 5 }
        return d
    }

    func duration(of event: String) -> TimeInterval {
        guard let (path, _) = resolve(event) else { return 0 }
        return fileDuration(path)
    }

    func remember(_ s: String) {
        recent.append("\(stampFormat.string(from: Date())) \(s)")
        if recent.count > 20 { recent.removeFirst(recent.count - 20) }
        writeState()
    }

    func writeState() {
        var st: [String: Any] = [
            "pid": Int(getpid()),
            "version": agentVersion,
            "started": stampFormat.string(from: started),
            "scheme": config["scheme"] as? String ?? "",
            "recent": recent,
            "trash_readable": trashReadable,
            "dry_run": dryRunAll,
            "playback_disabled": playbackDisabled,
        ]
        if !lastSimulated.isEmpty { st["last_simulated"] = lastSimulated }
        writeJSON(agentStatePath, st)
    }

    /// Spin the run loop while a sound plays, for the moments the process
    /// or the Mac is about to go away.
    func waitForPlayback(max: TimeInterval) {
        let until = Date().addingTimeInterval(max)
        while Date() < until && !players.isEmpty {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
    }

    // MARK: start

    func start() {
        try? FileManager.default.createDirectory(atPath: support, withIntermediateDirectories: true)
        reload(reason: "start")
        log("winsounds-agent \(agentVersion) started, pid \(getpid())\(dryRunAll ? ", dry run" : "")")
        listenForCLI()
        watchWorkspace()
        watchScreenLock()
        watchSleep()
        watchUSBAndHID()
        watchPower()
        watchNetwork()
        watchTrash()
        watchSignals()
        if !args.contains("--no-login-sound") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { self.loginSound() }
        }
    }

    /// Plays at login: Startup if the Mac booted in the last 15 minutes,
    /// Logon otherwise. Only when the console login is under 3 minutes old,
    /// so a crash restart or a reinstall never replays it.
    func loginSound() {
        guard let login = consoleLoginTime() else {
            log("login sound skipped: no console login found")
            return
        }
        let age = Date().timeIntervalSince(login)
        if age > 180 {
            log("login sound skipped: logged in \(Int(age / 60)) minutes ago")
            return
        }
        let uptime = ProcessInfo.processInfo.systemUptime
        fire(uptime < 900 ? "startup" : "login", detail: "uptime \(Int(uptime))s")
    }

    func consoleLoginTime() -> Date? {
        let me = NSUserName()
        var best: Date?
        setutxent()
        while let p = getutxent() {
            let e = p.pointee
            guard e.ut_type == USER_PROCESS else { continue }
            let user = withUnsafePointer(to: e.ut_user) { $0.withMemoryRebound(to: CChar.self, capacity: 256) { String(cString: $0) } }
            let line = withUnsafePointer(to: e.ut_line) { $0.withMemoryRebound(to: CChar.self, capacity: 32) { String(cString: $0) } }
            guard user == me, line == "console" else { continue }
            let t = Date(timeIntervalSince1970: TimeInterval(e.ut_tv.tv_sec))
            if best == nil || t > best! { best = t }
        }
        endutxent()
        return best
    }

    // MARK: CLI channel

    func listenForCLI() {
        var t: Int32 = 0
        notify_register_dispatch(label + ".reload", &t, DispatchQueue.main) { _ in self.reload(reason: "winsounds") }
        notifyTokens.append(t)
        notify_register_dispatch(label + ".simulate", &t, DispatchQueue.main) { _ in self.simulate() }
        notifyTokens.append(t)
    }

    func simulate() {
        guard let s = readJSON(simulatePath), let event = s["event"] as? String else { return }
        let dry = s["dry_run"] as? Bool ?? false
        let result: String
        switch event {
        case "trash_check":
            checkTrash(simulated: true)
            result = "checked the Trash"
        default:
            result = fire(event, detail: "simulated", dryRun: dry, force: false)
        }
        lastSimulated = ["at": s["at"] ?? 0, "event": event, "result": "Simulated \(event): \(result)"]
        writeState()
    }

    // MARK: workspace: apps, logout

    func watchWorkspace() {
        let nc = NSWorkspace.shared.notificationCenter
        nc.addObserver(forName: NSWorkspace.didLaunchApplicationNotification, object: nil, queue: .main) { n in
            guard self.toggle("app_open"),
                  let app = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  app.activationPolicy == .regular, app.processIdentifier != getpid() else { return }
            self.fire("app_open", detail: app.localizedName ?? "app")
        }
        nc.addObserver(forName: NSWorkspace.didTerminateApplicationNotification, object: nil, queue: .main) { n in
            guard self.toggle("app_close"),
                  let app = n.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  app.activationPolicy == .regular else { return }
            self.fire("app_close", detail: app.localizedName ?? "app")
        }
        nc.addObserver(forName: NSWorkspace.willPowerOffNotification, object: nil, queue: .main) { _ in
            self.playShutdown(reason: "log out or power off")
        }
    }

    func playShutdown(reason: String) {
        if shutdownPlayed { return }
        shutdownPlayed = true
        fire("shutdown", detail: reason, force: true)
        waitForPlayback(max: 6)
    }

    func watchSignals() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler {
                if sig == SIGHUP { self.reload(reason: "SIGHUP"); return }
                // a SIGTERM at logout is the shutdown moment; one from
                // `winsounds install/restart/uninstall` is not
                var quiet = sig == SIGINT
                if let a = try? FileManager.default.attributesOfItem(atPath: quietStopPath),
                   let m = a[.modificationDate] as? Date, Date().timeIntervalSince(m) < 30 {
                    quiet = true
                    try? FileManager.default.removeItem(atPath: quietStopPath)
                }
                if !quiet { self.playShutdown(reason: "SIGTERM") }
                log("stopping (\(sig == SIGTERM ? "SIGTERM" : "SIGINT")\(quiet ? ", quiet" : ""))")
                logQueue.sync {}
                exit(0)
            }
            src.resume()
            signalSources.append(src)
        }
    }

    // MARK: screen lock

    func watchScreenLock() {
        let dc = DistributedNotificationCenter.default()
        dc.addObserver(forName: .init("com.apple.screenIsLocked"), object: nil, queue: .main) { _ in self.fire("lock") }
        dc.addObserver(forName: .init("com.apple.screenIsUnlocked"), object: nil, queue: .main) { _ in self.fire("unlock") }
    }

    // MARK: sleep / wake (IOKit, so sleep waits for the sound)

    func watchSleep() {
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        rootPort = IORegisterForSystemPower(refcon, &powerNotifyPort, { refcon, _, type, arg in
            let me = Unmanaged<Agent>.fromOpaque(refcon!).takeUnretainedValue()
            me.powerMessage(type: type, arg: arg)
        }, &powerNotifier)
        if rootPort == 0 {
            log("could not register for sleep notifications")
            return
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), IONotificationPortGetRunLoopSource(powerNotifyPort).takeUnretainedValue(), .defaultMode)
    }

    func powerMessage(type: UInt32, arg: UnsafeMutableRawPointer?) {
        let canSleep: UInt32 = 0xE000_0270, willSleep: UInt32 = 0xE000_0280, poweredOn: UInt32 = 0xE000_0300
        let token = Int(bitPattern: arg)
        switch type {
        case canSleep:
            IOAllowPowerChange(rootPort, token)
        case willSleep:
            fire("sleep")
            let hold = min(duration(of: "sleep"), 4)
            DispatchQueue.main.asyncAfter(deadline: .now() + hold + 0.2) {
                IOAllowPowerChange(self.rootPort, token)
            }
        case poweredOn:
            lastWake = Date()
            // the audio device needs a moment after wake
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { self.fire("wake") }
        default:
            break
        }
    }

    // MARK: USB and Bluetooth HID (IOKit registry, no permissions needed)

    func watchUSBAndHID() {
        guard let port = IONotificationPortCreate(kIOMainPortDefault) else { return }
        notifyPort = port
        CFRunLoopAddSource(CFRunLoopGetMain(), IONotificationPortGetRunLoopSource(port).takeUnretainedValue(), .defaultMode)
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        func add(_ cls: String, _ type: String, _ cb: @escaping IOServiceMatchingCallback) {
            var it: io_iterator_t = 0
            let kr = IOServiceAddMatchingNotification(port, type, IOServiceMatching(cls), cb, refcon, &it)
            if kr != KERN_SUCCESS { log("cannot watch \(cls): \(kr)"); return }
            iterators.append(it)
            // arm the notification; devices already present are not events
            while case let o = IOIteratorNext(it), o != 0 { IOObjectRelease(o) }
        }
        add("IOUSBHostDevice", kIOFirstMatchNotification) { r, it in
            Unmanaged<Agent>.fromOpaque(r!).takeUnretainedValue().usb(it, attached: true)
        }
        add("IOUSBHostDevice", kIOTerminatedNotification) { r, it in
            Unmanaged<Agent>.fromOpaque(r!).takeUnretainedValue().usb(it, attached: false)
        }
        add("IOHIDDevice", kIOFirstMatchNotification) { r, it in
            Unmanaged<Agent>.fromOpaque(r!).takeUnretainedValue().hid(it, attached: true)
        }
        add("IOHIDDevice", kIOTerminatedNotification) { r, it in
            Unmanaged<Agent>.fromOpaque(r!).takeUnretainedValue().hid(it, attached: false)
        }
        log("watching USB and Bluetooth devices")
    }

    func prop(_ o: io_object_t, _ key: String) -> String? {
        guard let v = IORegistryEntryCreateCFProperty(o, key as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue() else { return nil }
        return v as? String ?? (v as? NSNumber)?.stringValue
    }

    func usb(_ it: io_iterator_t, attached: Bool) {
        var names: [String] = []
        while case let o = IOIteratorNext(it), o != 0 {
            names.append(prop(o, "USB Product Name") ?? prop(o, "kUSBProductString") ?? "USB device")
            IOObjectRelease(o)
        }
        guard !names.isEmpty, toggle("usb") else { return }
        fire(attached ? "device_connect" : "device_disconnect", detail: "USB " + names.joined(separator: ", "))
    }

    func hid(_ it: io_iterator_t, attached: Bool) {
        var names: [String] = []
        while case let o = IOIteratorNext(it), o != 0 {
            let transport = prop(o, "Transport") ?? ""
            if transport.lowercased().contains("bluetooth") {
                names.append(prop(o, "Product") ?? "Bluetooth device")
            }
            IOObjectRelease(o)
        }
        guard !names.isEmpty, toggle("bluetooth") else { return }
        fire(attached ? "device_connect" : "device_disconnect", detail: "Bluetooth " + Array(Set(names)).joined(separator: ", "))
    }

    // MARK: power adapter, battery, UPS

    func watchPower() {
        let refcon = Unmanaged.passUnretained(self).toOpaque()
        if let src = IOPSNotificationCreateRunLoopSource({ r in
            Unmanaged<Agent>.fromOpaque(r!).takeUnretainedValue().powerChanged(initial: false)
        }, refcon)?.takeRetainedValue() {
            CFRunLoopAddSource(CFRunLoopGetMain(), src, .defaultMode)
        }
        powerChanged(initial: true)
    }

    func powerChanged(initial: Bool) {
        guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue() else { return }
        let providing = IOPSGetProvidingPowerSourceType(info)?.takeUnretainedValue() as String? ?? kIOPMACPowerKey
        let onAC = providing == kIOPMACPowerKey
        var percent: Int? = nil
        if let list = IOPSCopyPowerSourcesList(info)?.takeRetainedValue() as? [CFTypeRef] {
            for ps in list {
                guard let d = IOPSGetPowerSourceDescription(info, ps)?.takeUnretainedValue() as? [String: Any] else { continue }
                if let cur = d[kIOPSCurrentCapacityKey] as? Int, let max = d[kIOPSMaxCapacityKey] as? Int, max > 0 {
                    percent = cur * 100 / max
                }
            }
        }
        if initial {
            powerOnAC = onAC
            log("power: \(providing)\(percent.map { ", \($0)%" } ?? ", no battery")")
            return
        }
        if let was = powerOnAC, was != onAC {
            fire(onAC ? "power_connected" : "power_disconnected", detail: providing)
        }
        powerOnAC = onAC
        if let p = percent, !onAC {
            if p <= 5 && !batteryCriticalFired {
                batteryCriticalFired = true
                fire("battery_critical", detail: "\(p)%")
            } else if p <= 10 && !batteryLowFired {
                batteryLowFired = true
                fire("battery_low", detail: "\(p)%")
            }
        }
        if let p = percent, p > 12 || onAC { batteryLowFired = false; if p > 7 || onAC { batteryCriticalFired = false } }
    }

    // MARK: network

    func watchNetwork() {
        netMonitor.pathUpdateHandler = { path in
            DispatchQueue.main.async { self.networkUpdate(path.status) }
        }
        netMonitor.start(queue: DispatchQueue(label: "net"))
    }

    func networkUpdate(_ status: NWPath.Status) {
        if netStatus == nil {
            netStatus = status
            netAnnounced = status
            log("network: \(status == .satisfied ? "up" : "down") at start")
            return
        }
        netStatus = status
        netWork?.cancel()
        // debounce: only a change that holds for 4 seconds counts
        let w = DispatchWorkItem {
            guard let s = self.netStatus, s != self.netAnnounced else { return }
            let up = s == .satisfied
            let down = s == .unsatisfied
            guard up || down else { return }
            self.netAnnounced = s
            if Date().timeIntervalSince(self.lastWake) < 20 {
                log("network \(up ? "up" : "down") right after wake, not announced")
                return
            }
            self.fire(up ? "network_up" : "network_down")
        }
        netWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 4, execute: w)
    }

    // MARK: Trash

    /// Items in the Trash. Listing ~/.Trash needs Full Disk Access, but
    /// the directory's entry count (getattrlist) and a stat of .DS_Store do
    /// not, so this works from a plain LaunchAgent.
    func trashItems() -> Int? {
        let p = trashDir
        if let items = try? FileManager.default.contentsOfDirectory(atPath: p) {
            return items.filter { $0 != ".DS_Store" && $0 != ".localized" }.count
        }
        var al = attrlist(bitmapcount: u_short(ATTR_BIT_MAP_COUNT), reserved: 0, commonattr: 0, volattr: 0,
                          dirattr: attrgroup_t(ATTR_DIR_ENTRYCOUNT), fileattr: 0, forkattr: 0)
        var buf = [UInt8](repeating: 0, count: 64)
        guard getattrlist(p, &al, &buf, buf.count, 0) == 0 else { return nil }
        var n = Int(buf.withUnsafeBytes { $0.load(fromByteOffset: 4, as: UInt32.self) })
        var st = stat()
        for hidden in [".DS_Store", ".localized"] where stat(p + "/" + hidden, &st) == 0 { n -= 1 }
        return max(0, n)
    }

    func watchTrash() {
        trashCount = trashItems()
        trashReadable = trashCount != nil
        if !trashReadable {
            log("cannot read ~/.Trash (Full Disk Access needed); empty_trash will not play")
        }
        let fd = open(trashDir, O_EVTONLY)
        if fd >= 0 {
            let src = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .link, .rename], queue: .main)
            src.setEventHandler { self.checkTrash(simulated: false) }
            src.setCancelHandler { close(fd) }
            src.resume()
            trashSource = src
        }
        // a slow poll as well, in case the directory event is missed
        Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in self.checkTrash(simulated: false) }
        writeState()
    }

    func checkTrash(simulated: Bool) {
        let n = trashItems()
        if n == nil {
            if trashReadable { log("lost read access to ~/.Trash") }
            trashReadable = false
            return
        }
        trashReadable = true
        if let was = trashCount, was > 0, n == 0 {
            fire("empty_trash", detail: "\(was) item\(was == 1 ? "" : "s")")
        }
        trashCount = n
    }

    // MARK: Mail (off by default)

    func updateMailPolling() {
        let want = toggle("new_mail") && enabled
        if want && mailTimer == nil {
            mailTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { _ in self.pollMail() }
            log("watching Mail for new messages")
        } else if !want, let t = mailTimer {
            t.invalidate()
            mailTimer = nil
            mailUnread = nil
        }
    }

    func pollMail() {
        // never launch Mail; only ask while it is already running
        guard NSWorkspace.shared.runningApplications.contains(where: { $0.bundleIdentifier == "com.apple.mail" }) else {
            mailUnread = nil
            return
        }
        let script = NSAppleScript(source: "tell application id \"com.apple.mail\" to get unread count of inbox")
        var err: NSDictionary?
        let r = script?.executeAndReturnError(&err)
        guard err == nil, let n = r?.int32Value else { return }
        if let was = mailUnread, Int(n) > was {
            fire("new_mail", detail: "\(Int(n) - was) new")
        }
        mailUnread = Int(n)
    }
}

// MARK: - main

// No NSApplication: a plain run loop. The agent must not register as an
// app with the window server; audio started on its behalf by an AppKit
// agent is what wedged coreaudiod on 2026-11-22.
let agent = Agent()
agent.start()
log("responsibility disclaim for afplay: \(disclaim == nil ? "not available" : "on")")
RunLoop.main.run()
