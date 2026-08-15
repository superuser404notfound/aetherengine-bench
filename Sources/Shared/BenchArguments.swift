import Foundation

enum BackendKind: String, Codable, CaseIterable {
    case aether, avplayer, vlckit, ksplayer
}

struct BenchArguments {
    enum ParseError: Error { case missing(String), badValue(String, String) }

    let backend: BackendKind
    let url: URL
    let settle: TimeInterval
    let measure: TimeInterval
    let windowSize: CGSize
    let displayIndex: Int
    let reportURL: URL

    static func parse(_ argv: [String]) throws -> BenchArguments {
        var flags: [String: String] = [:]
        var i = 0
        while i < argv.count - 1 {
            if argv[i].hasPrefix("--") { flags[String(argv[i].dropFirst(2))] = argv[i + 1]; i += 2 }
            else { i += 1 }
        }
        func need(_ key: String) throws -> String {
            guard let v = flags[key] else { throw ParseError.missing(key) }
            return v
        }
        let backendRaw = try need("backend")
        guard let backend = BackendKind(rawValue: backendRaw) else {
            throw ParseError.badValue("backend", backendRaw)
        }
        let windowRaw = flags["window"] ?? "1920x1080"
        let parts = windowRaw.split(separator: "x").compactMap { Int($0) }
        guard parts.count == 2 else { throw ParseError.badValue("window", windowRaw) }

        return BenchArguments(
            backend: backend,
            url: URL(fileURLWithPath: try need("url")),
            settle: TimeInterval(flags["settle"] ?? "15") ?? 15,
            measure: TimeInterval(flags["measure"] ?? "60") ?? 60,
            windowSize: CGSize(width: parts[0], height: parts[1]),
            displayIndex: Int(flags["display"] ?? "0") ?? 0,
            reportURL: URL(fileURLWithPath: try need("report")))
    }
}
