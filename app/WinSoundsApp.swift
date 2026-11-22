// WinSounds settings window.
//
// A small SwiftUI app over the same files the CLI and agent use:
// ~/Library/Application Support/WinSounds/config.json and Schemes/*/scheme.json.
// Toggles and volume are written straight to config.json (the agent is told
// to reload); switching schemes and the alert sound go through the bundled
// `winsounds` CLI, so those rules live in one place.
//
// Built by build-app.sh into WinSounds.app. VoiceOver: every control has a
// label and, where useful, a value and a hint; the scheme list and the event
// list are plain lists you can arrow through; Return uses the selected scheme.

import AppKit
import SwiftUI

let support = FileManager.default.homeDirectoryForCurrentUser.path + "/Library/Application Support/WinSounds"
let configPath = support + "/config.json"

func readJSON(_ path: String) -> [String: Any]? {
    guard let d = FileManager.default.contents(atPath: path) else { return nil }
    return (try? JSONSerialization.jsonObject(with: d)) as? [String: Any]
}

func announce(_ text: String) {
    NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                         userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.medium.rawValue])
}

typealias DisclaimFn = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
let disclaim: DisclaimFn? = {
    guard let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "responsibility_spawnattrs_setdisclaim") else { return nil }
    return unsafeBitCast(sym, to: DisclaimFn.self)
}()

/// afplay as its own responsible process, as the agent does.
func spawnDisclaimed(_ argv: [String]) -> pid_t? {
    var attr: posix_spawnattr_t?
    posix_spawnattr_init(&attr)
    defer { posix_spawnattr_destroy(&attr) }
    _ = disclaim?(&attr, 1)
    var cargs = argv.map { strdup($0) } + [nil]
    defer { cargs.forEach { free($0) } }
    var pid: pid_t = 0
    return posix_spawn(&pid, argv[0], nil, &attr, &cargs, environ) == 0 ? pid : nil
}

struct Scheme: Identifiable, Hashable {
    let id: String
    let name: String
    let source: String
    let dir: String
    let events: [String: [String]]
    let hidden: Bool
    static func == (a: Scheme, b: Scheme) -> Bool { a.id == b.id }
    func hash(into h: inout Hasher) { h.combine(id) }
}

struct EventInfo: Identifiable {
    let id: String
    let label: String
    let fallback: [String]
}

final class Model: ObservableObject {
    @Published var schemes: [Scheme] = []
    @Published var selected: Scheme.ID?
    @Published var current = ""
    @Published var enabled = true
    @Published var muted = false
    @Published var alert = false
    @Published var volume: Double = 60
    @Published var toggles: [String: Bool] = [:]
    @Published var status = ""
    var events: [EventInfo] = []
    var macEvents: [String] = []
    var config: [String: Any] = [:]
    let playQueue = DispatchQueue(label: "preview")

    var cli: String {
        if let p = Bundle.main.path(forResource: "winsounds", ofType: "py") { return p }
        return FileManager.default.homeDirectoryForCurrentUser.path + "/bin/winsounds"
    }

    func mappingsDir() -> String {
        if let p = Bundle.main.resourcePath, FileManager.default.fileExists(atPath: p + "/mappings/events.json") { return p + "/mappings" }
        return support + "/mappings"
    }

    func load() {
        let m = mappingsDir()
        if let e = readJSON(m + "/events.json")?["events"] as? [[String: Any]] {
            events = e.compactMap { d in
                guard let id = d["id"] as? String else { return nil }
                return EventInfo(id: id, label: d["label"] as? String ?? id, fallback: d["fallback"] as? [String] ?? [])
            }
        }
        let mac = ((readJSON(m + "/platforms.json")?["platforms"] as? [String: Any])?["macos"] as? [String: Any]) ?? [:]
        macEvents = events.map(\.id).filter { mac[$0] != nil }
        var defaults: [String: Bool] = [:]
        for (k, v) in mac { defaults[k] = ((v as? [String: Any])?["default"] as? Bool) ?? true }
        defaults["usb"] = true
        defaults["bluetooth"] = true

        var list: [Scheme] = []
        let dir = support + "/Schemes"
        for n in (try? FileManager.default.contentsOfDirectory(atPath: dir)) ?? [] {
            guard let s = readJSON(dir + "/" + n + "/scheme.json"), let id = s["id"] as? String else { continue }
            var ev: [String: [String]] = [:]
            for (k, v) in s["events"] as? [String: Any] ?? [:] {
                if let l = v as? [String] { ev[k] = l } else if let x = v as? String { ev[k] = [x] }
            }
            if ev.isEmpty { continue }
            list.append(Scheme(id: id, name: s["name"] as? String ?? id, source: s["source"] as? String ?? "",
                               dir: dir + "/" + n, events: ev, hidden: s["hidden"] as? Bool ?? false))
        }
        let order: [String: Int] = Dictionary(uniqueKeysWithValues: list.map { ($0.id, (readJSON($0.dir + "/scheme.json")?["order"] as? Int) ?? 999) })
        schemes = list.filter { !$0.hidden }.sorted { (order[$0.id]!, $0.name.lowercased()) < (order[$1.id]!, $1.name.lowercased()) }

        config = readJSON(configPath) ?? [:]
        current = config["scheme"] as? String ?? ""
        enabled = config["enabled"] as? Bool ?? true
        muted = config["muted"] as? Bool ?? false
        alert = config["alert"] as? Bool ?? false
        volume = (config["volume"] as? NSNumber)?.doubleValue ?? 60
        var t = defaults
        for (k, v) in config["toggles"] as? [String: Any] ?? [:] { if let b = v as? Bool { t[k] = b } }
        toggles = t
        if selected == nil { selected = current.isEmpty ? schemes.first?.id : current }
    }

    var selectedScheme: Scheme? { schemes.first { $0.id == selected } }
    var currentName: String { schemes.first { $0.id == current }?.name ?? "none" }

    func resolve(_ event: String, in s: Scheme) -> (String, String)? {
        var seen: [String] = []
        var queue = [event]
        while !queue.isEmpty {
            let e = queue.removeFirst()
            if seen.contains(e) { continue }
            seen.append(e)
            for f in s.events[e] ?? [] where FileManager.default.fileExists(atPath: s.dir + "/" + f) {
                return (s.dir + "/" + f, e)
            }
            queue += events.first { $0.id == e }?.fallback ?? []
        }
        return nil
    }

    func label(_ id: String) -> String {
        switch id {
        case "usb": return "USB devices"
        case "bluetooth": return "Bluetooth devices"
        default: return events.first { $0.id == id }?.label ?? id
        }
    }

    // MARK: writing

    func save() {
        config["enabled"] = enabled
        config["muted"] = muted
        config["volume"] = Int(volume.rounded())
        config["toggles"] = toggles
        if let d = try? JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys]) {
            let tmp = configPath + ".tmp"
            FileManager.default.createFile(atPath: tmp, contents: d)
            _ = rename(tmp, configPath)
        }
        notifyAgent()
    }

    func notifyAgent() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/notifyutil")
        p.arguments = ["-p", "com.matthew.winsounds.reload"]
        try? p.run()
    }

    @discardableResult
    func runCLI(_ args: [String]) -> String {
        let p = Process()
        let out = Pipe()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [cli] + args
        p.standardOutput = out
        p.standardError = out
        do { try p.run() } catch { return "Could not run winsounds: \(error.localizedDescription)" }
        p.waitUntilExit()
        let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func say(_ s: String) {
        status = s
        announce(s)
    }

    func use(_ s: Scheme) {
        let r = runCLI(["use", s.id])
        load()
        say(r)
    }

    func setEnabled(_ on: Bool) {
        let r = runCLI([on ? "on" : "off"])
        load()
        say(r)
    }

    func setAlert(_ on: Bool) {
        let r = runCLI(["alert", on ? "on" : "off"])
        load()
        say(r)
    }

    func preview(_ event: String, in s: Scheme) {
        guard let (path, used) = resolve(event, in: s) else {
            say("\(s.name) has no sound for \(label(event)).")
            return
        }
        let vol = Float(volume / 100)
        let file = (path as NSString).lastPathComponent
        say("Playing \(file)\(used == event ? "" : ", via \(label(used))").")
        // after the announcement; afplay in a child process, killed if it
        // hangs, so a wedged audio device can never freeze the window
        playQueue.asyncAfter(deadline: .now() + 0.6) { self.play(path, vol, limit: 15) }
    }

    /// One afplay at a time, waited for on the preview queue, killed after
    /// limit + 5 seconds.
    func play(_ path: String, _ vol: Float, limit: Double) {
        let argv = ["/usr/bin/afplay", "-v", String(format: "%.3f", vol), "-t", String(format: "%.0f", limit), path]
        guard let pid = spawnDisclaimed(argv) else { return }
        let deadline = Date().addingTimeInterval(limit + 5)
        var status: Int32 = 0
        while Date() < deadline {
            if waitpid(pid, &status, WNOHANG) == pid { return }
            Thread.sleep(forTimeInterval: 0.05)
        }
        kill(pid, SIGKILL)
        waitpid(pid, &status, 0)
        DispatchQueue.main.async { self.say("The sound did not finish, so it was stopped. The audio output may not be answering.") }
    }

    func previewScheme(_ s: Scheme) {
        let picks = ["startup", "login", "default_beep", "exclamation", "critical_stop", "notification",
                     "device_connect", "empty_trash", "shutdown"].compactMap { e -> String? in
            guard let f = s.events[e]?.first else { return nil }
            return s.dir + "/" + f
        }
        let unique = Array(NSOrderedSet(array: picks)).compactMap { $0 as? String }.prefix(6)
        say("Playing \(unique.count) sounds from \(s.name).")
        let vol = Float(volume / 100)
        playQueue.asyncAfter(deadline: .now() + 2.0) {
            for path in unique {
                self.play(path, vol, limit: 8)
                Thread.sleep(forTimeInterval: 0.8)
            }
        }
    }
}

struct ContentView: View {
    @ObservedObject var model: Model

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Schemes").font(.headline).accessibilityAddTraits(.isHeader)
                List(model.schemes, selection: $model.selected) { s in
                    HStack {
                        Text(s.name)
                        Spacer()
                        if s.id == model.current {
                            Text("Current").foregroundStyle(.secondary)
                        }
                    }
                    .tag(s.id)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(s.id == model.current ? "\(s.name), current scheme" : s.name)
                }
                .accessibilityLabel("Schemes")
                .accessibilityHint("Arrow to a scheme, then press Return to use it.")
                .frame(minWidth: 260)
                HStack {
                    Button("Use This Scheme") { if let s = model.selectedScheme { model.use(s) } }
                        .keyboardShortcut(.defaultAction)
                        .disabled(model.selectedScheme == nil || model.selectedScheme?.id == model.current)
                        .accessibilityHint("Makes the selected scheme the one the Mac plays.")
                    Button("Preview Scheme") { if let s = model.selectedScheme { model.previewScheme(s) } }
                        .keyboardShortcut("p", modifiers: [.command, .shift])
                        .accessibilityHint("Plays up to six of the selected scheme's sounds, with pauses.")
                }
            }
            .padding()
            .frame(width: 320)

            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    Text("WinSounds").font(.title2).bold().accessibilityAddTraits(.isHeader)
                    Toggle("Sounds on", isOn: Binding(get: { model.enabled }, set: { model.setEnabled($0) }))
                        .accessibilityHint("Turning this off also puts back the Mac's own alert sound.")
                    Toggle("Mute", isOn: Binding(get: { model.muted }, set: { model.muted = $0; model.save(); model.say($0 ? "Muted." : "Unmuted.") }))
                    HStack {
                        Text("Volume")
                        Slider(value: Binding(get: { model.volume }, set: { model.volume = $0 }), in: 0...100, step: 5) { editing in
                            if !editing { model.save() }
                        }
                        .accessibilityLabel("Volume")
                        .accessibilityValue("\(Int(model.volume)) percent")
                        Text("\(Int(model.volume))").monospacedDigit().frame(width: 32, alignment: .trailing).accessibilityHidden(true)
                    }
                    Toggle("Use the scheme's beep as the Mac alert sound", isOn: Binding(get: { model.alert }, set: { model.setAlert($0) }))
                        .accessibilityHint("Off puts back the alert sound you had before.")

                    if let s = model.selectedScheme {
                        Divider()
                        Text(s.id == model.current ? "Events in \(s.name), the current scheme" : "Events in \(s.name)")
                            .font(.headline).accessibilityAddTraits(.isHeader)
                        Text("Each switch turns the event on or off for every scheme.")
                            .font(.callout).foregroundStyle(.secondary)
                        ForEach(model.macEvents + ["usb", "bluetooth"], id: \.self) { e in
                            EventRow(model: model, scheme: s, event: e)
                        }
                    }
                    if !model.status.isEmpty {
                        Divider()
                        Text(model.status).font(.callout).textSelection(.enabled)
                            .accessibilityLabel("Last result: \(model.status)")
                    }
                }
                .padding()
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(minWidth: 820, minHeight: 560)
        .onAppear { model.load() }
    }
}

struct EventRow: View {
    @ObservedObject var model: Model
    let scheme: Scheme
    let event: String

    var body: some View {
        let detector = event == "usb" || event == "bluetooth"
        let r = detector ? nil : model.resolve(event, in: scheme)
        let file = r.map { ($0.0 as NSString).lastPathComponent }
        let via = r.flatMap { $0.1 == event ? nil : model.label($0.1) }
        let detail = detector ? "Detects connects and disconnects" :
            (file.map { via == nil ? $0 : "\($0), via \(via!)" } ?? "No sound in this scheme")
        HStack {
            Toggle(isOn: Binding(get: { model.toggles[event] ?? true },
                                 set: { model.toggles[event] = $0; model.save(); model.say("\(model.label(event)) \($0 ? "on" : "off").") })) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.label(event))
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                }
            }
            .accessibilityLabel("\(model.label(event)), \(detail)")
            Spacer()
            if !detector {
                Button("Preview") { model.preview(event, in: scheme) }
                    .disabled(file == nil)
                    .accessibilityLabel("Preview \(model.label(event))")
            }
        }
    }
}

@main
struct WinSoundsApp: App {
    @StateObject var model = Model()
    var body: some Scene {
        WindowGroup("WinSounds") {
            ContentView(model: model)
        }
        .commands {
            CommandGroup(after: .newItem) {
                Button("Reload Schemes") { model.load() }.keyboardShortcut("r")
            }
        }
    }
}
