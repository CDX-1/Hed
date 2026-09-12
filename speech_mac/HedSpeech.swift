// hed-speech: Apple's on-device SpeechAnalyzer (macOS 26+), shared between
// everything in Hed that wants to hear you.
//
// There is one microphone and one recognizer, switched on only from the island
// overlay's VOICE button, and several things that want its output: the overlay
// (voice typing into any app, audio waves) and the Chrome extension ("hed"
// browser commands). Running a
// recognizer per consumer would transcribe everything twice and type it twice,
// so the recognizer lives in a hub and consumers are clients of it:
//
//   hed-speech --hub      the overlay starts this. Owns the mic, serves a Unix
//                         socket (SOCKET_PATH), exits when its stdin closes.
//   hed-speech            (no args) Chrome native messaging host. Only relays
//                         to the hub; never listens on its own.
//   hed-speech --cli      print live transcripts as JSON lines (debugging)
//   hed-speech --file F   transcribe an audio file (testing without a mic)
//
// Client -> hub messages (JSON; newline-framed on the socket, length-prefixed
// on Chrome's stdio):
//   {"type":"listen","lang":"en-US","hints":["hey hed"],"levels":true}
//   {"type":"stop"}
//   {"type":"ping"}
//   {"type":"monitor"}                receive levels and transcripts without
//                                     keeping the recognizer running
//   {"type":"typing","active":true}   overlay is typing into the focused app
//   {"type":"consumed"}               a client just ran a transcript as a command
//
// Hub -> client messages:
//   {"type":"status","status":"listening"|"stopped"|"downloading"|...}
//   {"type":"result","text":"...","final":bool,"alts":[...],"start":sec}
//   {"type":"level","level":0..1}     only to clients that asked for levels
//   {"type":"typing","active":bool}   relayed from the overlay
//   {"type":"consumed"}               relayed from whoever ran a command
//   {"type":"error","error":"...","detail":"..."}

import AVFoundation
import Darwin
import Foundation
import Speech

let SOCKET_PATH = NSHomeDirectory() + "/Library/Application Support/Hed/speech.sock"

typealias Message = [String: Any]

func log(_ s: String) {
    FileHandle.standardError.write(Data(("[hed-speech] " + s + "\n").utf8))
}

func encode(_ obj: Message) -> Data? {
    try? JSONSerialization.data(withJSONObject: obj)
}

func decode(_ data: Data) -> Message? {
    (try? JSONSerialization.jsonObject(with: data)) as? Message
}

// MARK: - Clients

protocol Client: AnyObject {
    func send(_ obj: Message)
}

/// Chrome native messaging: native-endian UInt32 length, then JSON.
final class NativeStdio: Client {
    private let lock = NSLock()

    func send(_ obj: Message) {
        guard let data = encode(obj) else { return }
        lock.lock()
        defer { lock.unlock() }
        var len = UInt32(data.count)
        FileHandle.standardOutput.write(Data(bytes: &len, count: 4))
        FileHandle.standardOutput.write(data)
    }

    /// Blocks the calling thread; returns when Chrome closes the pipe.
    func readLoop(_ onMessage: (Message) -> Void) {
        func readExactly(_ n: Int) -> Data? {
            var data = Data()
            while data.count < n {
                let chunk = FileHandle.standardInput.readData(ofLength: n - data.count)
                if chunk.isEmpty { return nil }
                data.append(chunk)
            }
            return data
        }
        while let header = readExactly(4) {
            let len = header.withUnsafeBytes { Int($0.loadUnaligned(as: UInt32.self)) }
            guard let body = readExactly(len) else { return }
            if let msg = decode(body) { onMessage(msg) }
        }
    }
}

/// JSON lines on stdout.
final class LineStdout: Client {
    private let lock = NSLock()
    func send(_ obj: Message) {
        guard let data = encode(obj) else { return }
        lock.lock()
        defer { lock.unlock() }
        FileHandle.standardOutput.write(data + Data([0x0A]))
    }
}

/// One end of the hub socket, newline-framed. Used by both sides.
final class LineSocket: Client {
    private let fd: Int32
    private let lock = NSLock()
    private var closed = false

    init(fd: Int32) {
        self.fd = fd
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
    }

    func send(_ obj: Message) {
        guard var data = encode(obj) else { return }
        data.append(0x0A)
        lock.lock()
        defer { lock.unlock() }
        if closed { return }
        data.withUnsafeBytes { buf in
            var off = 0
            while off < buf.count {
                let n = Darwin.write(fd, buf.baseAddress! + off, buf.count - off)
                if n <= 0 { break }
                off += n
            }
        }
    }

    /// Reads lines on a background thread until the peer goes away.
    func start(onMessage: @escaping (Message) -> Void, onClose: @escaping () -> Void) {
        Thread.detachNewThread { [self] in
            var pending = Data()
            var buf = [UInt8](repeating: 0, count: 8192)
            while true {
                let n = Darwin.read(fd, &buf, buf.count)
                if n <= 0 { break }
                pending.append(buf, count: n)
                while let nl = pending.firstIndex(of: 0x0A) {
                    let line = pending[pending.startIndex..<nl]
                    pending.removeSubrange(pending.startIndex...nl)
                    if let msg = decode(Data(line)) { onMessage(msg) }
                }
            }
            close()
            onClose()
        }
    }

    func close() {
        lock.lock()
        defer { lock.unlock() }
        if closed { return }
        closed = true
        Darwin.close(fd)
    }
}

func unixAddress(_ path: String) -> sockaddr_un {
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    addr.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
    let bytes = Array(path.utf8)
    withUnsafeMutableBytes(of: &addr.sun_path) { raw in
        let n = min(bytes.count, raw.count - 1)
        for i in 0..<n { raw[i] = bytes[i] }
        raw[n] = 0
    }
    return addr
}

func connectToHub() -> LineSocket? {
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { return nil }
    var addr = unixAddress(SOCKET_PATH)
    let ok = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) == 0
        }
    }
    if !ok {
        close(fd)
        return nil
    }
    return LineSocket(fd: fd)
}

// MARK: - Recognizer

@available(macOS 26.0, *)
actor Recognizer {
    private let emit: @Sendable (Message) -> Void
    private let emitLevel: @Sendable (Float) -> Void
    private var analyzer: SpeechAnalyzer?
    private var engine: AVAudioEngine?
    private var input: AsyncStream<AnalyzerInput>.Continuation?
    private var resultsTask: Task<Void, Never>?
    private var configObserver: NSObjectProtocol?
    private(set) var lang: String?

    init(emit: @escaping @Sendable (Message) -> Void,
         emitLevel: @escaping @Sendable (Float) -> Void) {
        self.emit = emit
        self.emitLevel = emitLevel
    }

    private func fail(_ error: String, _ detail: String) {
        log("\(error): \(detail)")
        emit(["type": "error", "error": error, "detail": detail])
    }

    /// Start listening in `lang`. A no-op if already doing exactly that.
    func listen(lang: String, hints: [String]) async {
        if analyzer != nil, lang == self.lang { return }
        await stop(report: false)

        guard let locale = await SpeechTranscriber.supportedLocale(
            equivalentTo: Locale(identifier: lang))
        else { return fail("language-not-supported", "no on-device model for \(lang)") }

        guard await micAllowed() else {
            return fail("not-allowed", "microphone access is denied")
        }

        // Alternatives help the wake word: "hed" is not a dictionary word, so
        // the right spelling is often the second or third guess.
        let t = SpeechTranscriber(
            locale: locale, transcriptionOptions: [],
            reportingOptions: [.volatileResults, .fastResults, .alternativeTranscriptions],
            attributeOptions: [])

        do {
            if await AssetInventory.status(forModules: [t]) != .installed,
               let request = try await AssetInventory.assetInstallationRequest(supporting: [t]) {
                emit(["type": "status", "status": "downloading", "lang": lang])
                try await request.downloadAndInstall()
            }
        } catch {
            return fail("model-install-failed", String(describing: error))
        }

        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [t]) else {
            return fail("no-audio-format", "SpeechAnalyzer offered no compatible audio format")
        }

        let a = SpeechAnalyzer(
            modules: [t], options: .init(priority: .userInitiated, modelRetention: .processLifetime))
        let (stream, cont) = AsyncStream<AnalyzerInput>.makeStream(bufferingPolicy: .unbounded)
        do {
            if !hints.isEmpty {
                let ctx = AnalysisContext()
                ctx.contextualStrings[.general] = hints
                try await a.setContext(ctx)
            }
            try await a.prepareToAnalyze(in: format)
            try await a.start(inputSequence: stream)
        } catch {
            return fail("start-failed", String(describing: error))
        }

        analyzer = a
        input = cont
        self.lang = lang

        let emit = self.emit
        resultsTask = Task {
            do {
                for try await r in t.results {
                    emit([
                        "type": "result",
                        "text": String(r.text.characters),
                        "final": r.isFinal,
                        "alts": r.alternatives.map { String($0.characters) },
                        "start": r.range.start.seconds.isFinite ? r.range.start.seconds : 0,
                    ])
                }
            } catch is CancellationError {
            } catch {
                emit(["type": "error", "error": "results-failed", "detail": String(describing: error)])
            }
        }

        do {
            try startEngine(target: format)
        } catch {
            fail("audio-capture", String(describing: error))
            await stop(report: false)
            return
        }

        // Plugging in AirPods or switching the input device reconfigures the
        // engine and silently stops the tap. Rebuild it on the new format.
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: nil, queue: nil
        ) { [weak self] _ in
            Task { await self?.restartEngine(target: format) }
        }

        emit(["type": "status", "status": "listening", "lang": lang, "locale": locale.identifier])
    }

    var isListening: Bool { analyzer != nil }

    func stop(report: Bool = true) async {
        if let o = configObserver { NotificationCenter.default.removeObserver(o) }
        configObserver = nil
        if let e = engine {
            e.inputNode.removeTap(onBus: 0)
            e.stop()
        }
        engine = nil
        input?.finish()
        input = nil
        if let a = analyzer { await a.cancelAndFinishNow() }
        analyzer = nil
        resultsTask?.cancel()
        resultsTask = nil
        lang = nil
        if report { emit(["type": "status", "status": "stopped"]) }
    }

    private func restartEngine(target: AVAudioFormat) {
        guard let e = engine else { return }
        log("audio configuration changed; restarting capture")
        e.inputNode.removeTap(onBus: 0)
        e.stop()
        engine = nil
        do {
            try startEngine(target: target)
        } catch {
            fail("audio-capture", String(describing: error))
        }
    }

    private func startEngine(target: AVAudioFormat) throws {
        guard let cont = input else { return }
        let e = AVAudioEngine()
        let node = e.inputNode
        let natural = node.outputFormat(forBus: 0)
        guard natural.sampleRate > 0, natural.channelCount > 0 else {
            throw NSError(domain: "hed-speech", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "no input device"])
        }
        guard let converter = AVAudioConverter(from: natural, to: target) else {
            throw NSError(domain: "hed-speech", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "cannot convert \(natural) to \(target)"])
        }
        let ratio = target.sampleRate / natural.sampleRate
        let meter = LevelMeter(emit: emitLevel)
        // HED_SPEECH_DUMP=/path.caf records exactly what the analyzer hears.
        let dump = ProcessInfo.processInfo.environment["HED_SPEECH_DUMP"].flatMap {
            try? AVAudioFile(forWriting: URL(fileURLWithPath: $0), settings: target.settings,
                             commonFormat: target.commonFormat, interleaved: target.isInterleaved)
        }
        log("capture: \(natural) -> analyzer: \(target)")

        node.installTap(onBus: 0, bufferSize: 1024, format: natural) { buffer, _ in
            meter.feed(buffer)
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
            guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
            var fed = false
            var err: NSError?
            converter.convert(to: out, error: &err) { _, status in
                if fed {
                    status.pointee = .noDataNow
                    return nil
                }
                fed = true
                status.pointee = .haveData
                return buffer
            }
            if let err { log("convert failed: \(err)") }
            if err == nil, out.frameLength > 0 {
                try? dump?.write(from: out)
                cont.yield(AnalyzerInput(buffer: out))
            }
        }
        e.prepare()
        try e.start()
        engine = e
    }

    private func micAllowed() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        default: return false
        }
    }
}

/// Loudness for the overlay's waves: peak RMS over ~50 ms, mapped from a
/// -55..-10 dBFS window onto 0...1. Only touched from the audio tap thread.
final class LevelMeter: @unchecked Sendable {
    private let emit: @Sendable (Float) -> Void
    private var peak: Float = 0
    private var last = Date.distantPast

    init(emit: @escaping @Sendable (Float) -> Void) { self.emit = emit }

    func feed(_ buffer: AVAudioPCMBuffer) {
        guard let data = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
        let n = Int(buffer.frameLength)
        var sum: Float = 0
        for i in 0..<n { sum += data[i] * data[i] }
        let db = 20 * log10(max(sqrt(sum / Float(n)), 1e-7))
        peak = max(peak, min(max((db + 55) / 45, 0), 1))
        let now = Date()
        if now.timeIntervalSince(last) >= 0.05 {
            emit(peak)
            peak = 0
            last = now
        }
    }
}

// MARK: - Hub

/// Fans one recognizer out to any number of clients. Each client says whether
/// it wants audio; the recognizer runs while at least one does, in the language
/// most recently asked for.
@available(macOS 26.0, *)
final class Hub: @unchecked Sendable {
    private struct Member {
        let client: Client
        var wants = false
        var lang = "en-US"
        var hints: [String] = []
        var levels = false
        var monitor = false
        var askedAt = 0
    }

    private let q = DispatchQueue(label: "hed-speech.hub")
    private var members: [ObjectIdentifier: Member] = [:]
    private var tick = 0
    private var typing = false
    private var listening = false
    private var target: String?
    private var applied: String?
    private var applying = false
    private lazy var recognizer = Recognizer(
        emit: { [weak self] msg in self?.onRecognizer(msg) },
        emitLevel: { [weak self] level in self?.onLevel(level) })

    var isEmpty: Bool { q.sync { members.isEmpty } }

    func add(_ client: Client) {
        q.async { [self] in
            members[ObjectIdentifier(client)] = Member(client: client)
            if typing { client.send(["type": "typing", "active": true]) }
        }
    }

    func remove(_ client: Client) {
        q.async { [self] in
            members.removeValue(forKey: ObjectIdentifier(client))
            reconcile()
        }
    }

    func handle(_ msg: Message, from client: Client) {
        q.async { [self] in
            let key = ObjectIdentifier(client)
            if members[key] == nil { members[key] = Member(client: client) }
            switch msg["type"] as? String {
            case "listen":
                tick += 1
                members[key]!.wants = true
                members[key]!.lang = msg["lang"] as? String ?? "en-US"
                members[key]!.hints = msg["hints"] as? [String] ?? []
                members[key]!.levels = msg["levels"] as? Bool ?? false
                members[key]!.askedAt = tick
                if listening, applied == members[key]!.lang {
                    client.send(["type": "status", "status": "listening", "lang": applied!])
                } else if !listening, applied == members[key]!.lang {
                    applied = nil // last attempt failed; asking again means retry
                }
                reconcile()
            case "stop":
                members[key]!.wants = false
                reconcile()
                client.send(["type": "status", "status": "stopped"])
            case "monitor":
                members[key]!.monitor = true
                if listening, let applied {
                    client.send(["type": "status", "status": "listening", "lang": applied])
                }
            case "ping":
                client.send(["type": "pong", "available": SpeechTranscriber.isAvailable,
                             "typing": typing])
            case "typing":
                typing = msg["active"] as? Bool ?? false
                broadcast(["type": "typing", "active": typing], except: key)
            case "consumed":
                broadcast(["type": "consumed"], except: key)
            default:
                break
            }
        }
    }

    // on q
    private func broadcast(_ msg: Message, except: ObjectIdentifier? = nil,
                           where pick: (Member) -> Bool = { _ in true }) {
        for (k, m) in members where k != except && pick(m) {
            m.client.send(msg)
        }
    }

    private func onRecognizer(_ msg: Message) {
        q.async { [self] in
            if msg["type"] as? String == "status" {
                let s = msg["status"] as? String
                if s == "listening" { listening = true }
                if s == "stopped" { listening = false }
            }
            if msg["type"] as? String == "error" { listening = false }
            broadcast(msg) { $0.wants || $0.monitor }
        }
    }

    private func onLevel(_ level: Float) {
        q.async { [self] in
            broadcast(["type": "level", "level": Double(level)]) { ($0.wants && $0.levels) || $0.monitor }
        }
    }

    // on q. Drive the recognizer toward `target` one step at a time, so a
    // quick listen/stop/listen can never apply out of order.
    private func reconcile() {
        let wanting = members.values.filter(\.wants)
        let newest = wanting.max { $0.askedAt < $1.askedAt }
        target = newest?.lang
        let hints = Array(Set(wanting.flatMap(\.hints)))
        if applying { return }
        applying = true
        Task { [self] in
            while true {
                let next: String?? = q.sync {
                    if target == applied {
                        applying = false
                        return .none
                    }
                    return .some(target)
                }
                guard case .some(let lang) = next else { break }
                if let lang {
                    await recognizer.listen(lang: lang, hints: hints)
                } else {
                    await recognizer.stop()
                }
                q.sync { applied = lang }
            }
        }
    }
}

// MARK: - Modes

@available(macOS 26.0, *)
func runHub() {
    if let existing = connectToHub() {
        existing.close()
        log("a hub is already running at \(SOCKET_PATH)")
        exit(0)
    }
    try? FileManager.default.createDirectory(
        atPath: (SOCKET_PATH as NSString).deletingLastPathComponent,
        withIntermediateDirectories: true)
    unlink(SOCKET_PATH)

    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    var addr = unixAddress(SOCKET_PATH)
    let bound = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) == 0
        }
    }
    guard fd >= 0, bound, listen(fd, 16) == 0 else {
        log("cannot listen on \(SOCKET_PATH): \(String(cString: strerror(errno)))")
        exit(1)
    }
    chmod(SOCKET_PATH, 0o600)
    log("hub listening on \(SOCKET_PATH)")

    let hub = Hub()
    Thread.detachNewThread {
        while true {
            let cfd = accept(fd, nil, nil)
            if cfd < 0 { continue }
            let conn = LineSocket(fd: cfd)
            hub.add(conn)
            conn.start(onMessage: { hub.handle($0, from: conn) },
                       onClose: { hub.remove(conn) })
        }
    }

    // The overlay holds our stdin open for as long as it lives.
    Thread.detachNewThread {
        while !FileHandle.standardInput.readData(ofLength: 1024).isEmpty {}
        unlink(SOCKET_PATH)
        exit(0)
    }
}

/// Chrome's native host: a relay to the hub and nothing more. It never opens
/// the microphone itself - voice is switched on from the island overlay, which
/// owns the hub - so when there is no hub it just says so and keeps looking.
@available(macOS 26.0, *)
final class NativeBridge: @unchecked Sendable {
    private let chrome = NativeStdio()
    private let lock = NSLock()
    private var link: LineSocket?
    // Subscriptions to replay whenever the hub (re)appears.
    private var sticky: [String: Message] = [:]

    func run() {
        chrome.send(["type": "hub", "connected": false])
        attachToHub()
        Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in
            self?.attachToHub()
        }
        Thread.detachNewThread { [self] in
            chrome.readLoop { self.fromChrome($0) }
            // Chrome closed the port: the extension reloaded or was disabled.
            exit(0)
        }
    }

    private func fromChrome(_ msg: Message) {
        guard let type = msg["type"] as? String else { return }
        if type == "ping" {
            lock.lock()
            let connected = link != nil
            lock.unlock()
            chrome.send(["type": "pong", "available": SpeechTranscriber.isAvailable])
            chrome.send(["type": "hub", "connected": connected])
            return
        }
        lock.lock()
        if type == "monitor" { sticky["monitor"] = msg }
        let link = self.link
        lock.unlock()
        link?.send(msg)
    }

    private func attachToHub() {
        lock.lock()
        let attached = link != nil
        lock.unlock()
        if attached { return }
        guard let conn = connectToHub() else { return }

        lock.lock()
        link = conn
        let replay = Array(sticky.values)
        lock.unlock()

        log("relaying through the hub")
        chrome.send(["type": "hub", "connected": true])
        conn.start(
            onMessage: { [chrome] in chrome.send($0) },
            onClose: { [weak self] in self?.detachFromHub() })
        for m in replay { conn.send(m) }
    }

    private func detachFromHub() {
        lock.lock()
        link = nil
        lock.unlock()
        log("hub went away")
        chrome.send(["type": "hub", "connected": false])
        chrome.send(["type": "status", "status": "stopped"])
    }
}

@available(macOS 26.0, *)
func transcribeFile(_ path: String, lang: String) async {
    let out = LineStdout()
    guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: lang)) else {
        out.send(["type": "error", "error": "language-not-supported", "detail": lang])
        return
    }
    let t = SpeechTranscriber(
        locale: locale, transcriptionOptions: [],
        reportingOptions: [.alternativeTranscriptions], attributeOptions: [])
    do {
        if await AssetInventory.status(forModules: [t]) != .installed,
           let req = try await AssetInventory.assetInstallationRequest(supporting: [t]) {
            out.send(["type": "status", "status": "downloading", "lang": lang])
            try await req.downloadAndInstall()
        }
        let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
        let a = SpeechAnalyzer(modules: [t])
        let ctx = AnalysisContext()
        ctx.contextualStrings[.general] = ["hey hed", "Hed"]
        try await a.setContext(ctx)
        let collect = Task {
            for try await r in t.results {
                out.send(["type": "result", "text": String(r.text.characters), "final": r.isFinal,
                          "alts": r.alternatives.map { String($0.characters) }])
            }
        }
        if let end = try await a.analyzeSequence(from: file) {
            try await a.finalizeAndFinish(through: end)
        } else {
            await a.cancelAndFinishNow()
        }
        _ = try await collect.value
    } catch {
        out.send(["type": "error", "error": "file-failed", "detail": String(describing: error)])
    }
}

// MARK: - Main

guard #available(macOS 26.0, *) else {
    NativeStdio().send(["type": "error", "error": "unsupported-os",
                        "detail": "SpeechAnalyzer needs macOS 26 or later"])
    exit(1)
}

signal(SIGPIPE, SIG_IGN)
let args = CommandLine.arguments
// Kept at top level: a hub that goes out of scope takes the audio engine with it.
nonisolated(unsafe) var cliHub: AnyObject?

if let i = args.firstIndex(of: "--file"), i + 1 < args.count {
    let lang = i + 2 < args.count ? args[i + 2] : "en-US"
    Task {
        await transcribeFile(args[i + 1], lang: lang)
        exit(0)
    }
} else if args.contains("--hub") {
    runHub()
} else if let i = args.firstIndex(of: "--cli") {
    let lang = i + 1 < args.count ? args[i + 1] : "en-US"
    let out = LineStdout()
    let msg: Message = ["type": "listen", "lang": lang, "hints": ["hey hed", "Hed"]]
    if let conn = connectToHub() {
        conn.start(onMessage: { out.send($0) }, onClose: { exit(0) })
        conn.send(msg)
    } else {
        cliHub = Hub()
        (cliHub as? Hub)?.handle(msg, from: out)
    }
    signal(SIGINT) { _ in exit(0) }
} else {
    let bridge = NativeBridge()
    bridge.run()
    withExtendedLifetime(bridge) { RunLoop.main.run() }
}

RunLoop.main.run()
