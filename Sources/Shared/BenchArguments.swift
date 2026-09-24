import Foundation

enum BackendKind: String, Codable, CaseIterable {
    case aether, avplayer, vlckit, ksplayer
}

/// Parsing is strict on purpose. A benchmark that starts with the wrong file, a
/// zero-length window or a silently defaulted duration produces numbers that
/// look exactly like real ones, and those numbers get published. Every
/// malformed input refuses to start instead.
struct BenchArguments {
    enum ParseError: Error, CustomStringConvertible {
        case missing(String)
        case badValue(String, String)
        case unknownFlag(String)
        case duplicateFlag(String)

        var description: String {
            switch self {
            case .missing(let flag): return "missing value or required flag: --\(flag)"
            case .badValue(let flag, let value): return "bad value for --\(flag): '\(value)'"
            case .unknownFlag(let token): return "unknown argument: \(token)"
            case .duplicateFlag(let flag): return "--\(flag) given more than once"
            }
        }
    }

    static let knownFlags: Set<String> = [
        "backend", "url", "settle", "measure", "window", "display", "report", "linger",
    ]

    let backend: BackendKind
    let url: URL
    let settle: TimeInterval
    let measure: TimeInterval
    let windowSize: CGSize
    let displayIndex: Int
    let reportURL: URL
    /// Seconds to keep playing, after the report is written, before exiting.
    /// Defaults to 0 (exit immediately, the historical behavior). Exists so
    /// the process under measurement outlives the orchestrator's sampler,
    /// whose own window starts and ends a little later than this binary's
    /// (process spawn, `sudo -u` privilege drop, first `ps` call): without a
    /// margin, the sampler's last sample can land after this process has
    /// already exited, discarding an otherwise-clean run. Never affects the
    /// report: startedAt/endedAt are stamped and the report is written
    /// before this linger begins.
    let linger: TimeInterval

    static func parse(_ argv: [String]) throws -> BenchArguments {
        var flags: [String: String] = [:]
        var i = 0
        while i < argv.count {
            let token = argv[i]
            guard token.hasPrefix("--") else { throw ParseError.unknownFlag(token) }
            let key = String(token.dropFirst(2))
            guard knownFlags.contains(key) else { throw ParseError.unknownFlag(token) }
            guard flags[key] == nil else { throw ParseError.duplicateFlag(key) }
            guard i + 1 < argv.count else { throw ParseError.missing(key) }
            let value = argv[i + 1]
            // A value that looks like a flag means the previous flag's value was
            // omitted. Consuming it would silently shift every later argument.
            guard !value.hasPrefix("--") else { throw ParseError.missing(key) }
            flags[key] = value
            i += 2
        }

        func need(_ key: String) throws -> String {
            guard let value = flags[key] else { throw ParseError.missing(key) }
            return value
        }
        func seconds(_ key: String, default fallback: TimeInterval) throws -> TimeInterval {
            guard let raw = flags[key] else { return fallback }
            guard let value = TimeInterval(raw), value > 0 else { throw ParseError.badValue(key, raw) }
            return value
        }

        let backendRaw = try need("backend")
        guard let backend = BackendKind(rawValue: backendRaw) else {
            throw ParseError.badValue("backend", backendRaw)
        }
        let windowRaw = flags["window"] ?? "1920x1080"
        let parts = windowRaw.split(separator: "x").compactMap { Int($0) }
        guard parts.count == 2, parts[0] > 0, parts[1] > 0 else {
            throw ParseError.badValue("window", windowRaw)
        }
        let displayRaw = flags["display"] ?? "0"
        guard let displayIndex = Int(displayRaw), displayIndex >= 0 else {
            throw ParseError.badValue("display", displayRaw)
        }

        // A path is a local file; an http(s) URL is the HTTP arm, served by
        // Scripts/range-origin.py. Media servers deliver over HTTP, and at
        // least AetherEngine reads the two through different code.
        let urlRaw = try need("url")
        let url: URL
        if urlRaw.hasPrefix("http://") || urlRaw.hasPrefix("https://") {
            guard let remote = URL(string: urlRaw), remote.host != nil else {
                throw ParseError.badValue("url", urlRaw)
            }
            url = remote
        } else {
            url = URL(fileURLWithPath: urlRaw)
        }

        return BenchArguments(
            backend: backend,
            url: url,
            settle: try seconds("settle", default: 15),
            measure: try seconds("measure", default: 60),
            windowSize: CGSize(width: parts[0], height: parts[1]),
            displayIndex: displayIndex,
            reportURL: URL(fileURLWithPath: try need("report")),
            linger: try seconds("linger", default: 0))
    }
}
