import Testing
import Foundation

@Test func parsesAFullArgumentList() throws {
    let args = try BenchArguments.parse([
        "--backend", "vlckit", "--url", "/tmp/x.mkv",
        "--settle", "15", "--measure", "60",
        "--window", "1920x1080", "--display", "0",
        "--report", "/tmp/out.json",
    ])
    #expect(args.backend == .vlckit)
    #expect(args.url.path == "/tmp/x.mkv")
    #expect(args.settle == 15)
    #expect(args.measure == 60)
    #expect(args.windowSize == CGSize(width: 1920, height: 1080))
    #expect(args.reportURL.path == "/tmp/out.json")
}

@Test func rejectsAnUnknownBackend() {
    #expect(throws: BenchArguments.ParseError.self) {
        _ = try BenchArguments.parse(["--backend", "quicktime", "--url", "/tmp/x.mkv", "--report", "/tmp/o.json"])
    }
}

/// Each of these used to parse "successfully" into wrong arguments, which is the
/// failure mode that matters: the harness would run and publish a number.
@Test func refusesMalformedArgumentLists() {
    let base = ["--backend", "avplayer", "--url", "/tmp/x.mkv", "--report", "/tmp/o.json"]
    // A flag whose value was omitted must not swallow the next flag's name.
    #expect(throws: BenchArguments.ParseError.self) {
        _ = try BenchArguments.parse(["--backend", "avplayer", "--url", "--measure", "120",
                                      "--report", "/tmp/o.json"])
    }
    // A flag in final position has no value.
    #expect(throws: BenchArguments.ParseError.self) {
        _ = try BenchArguments.parse(base + ["--measure"])
    }
    // A non-numeric duration must refuse, not silently measure the default.
    #expect(throws: BenchArguments.ParseError.self) {
        _ = try BenchArguments.parse(base + ["--measure", "abc"])
    }
    // Zero and negative durations are not measurable.
    #expect(throws: BenchArguments.ParseError.self) {
        _ = try BenchArguments.parse(base + ["--settle", "0"])
    }
    // A repeated flag is ambiguous, so it is an error rather than last-wins.
    #expect(throws: BenchArguments.ParseError.self) {
        _ = try BenchArguments.parse(base + ["--measure", "60", "--measure", "90"])
    }
    // Unknown flags and stray positional arguments are errors.
    #expect(throws: BenchArguments.ParseError.self) {
        _ = try BenchArguments.parse(base + ["--verbose", "yes"])
    }
    #expect(throws: BenchArguments.ParseError.self) {
        _ = try BenchArguments.parse(base + ["extra.mkv"])
    }
    // A degenerate window is not a window.
    #expect(throws: BenchArguments.ParseError.self) {
        _ = try BenchArguments.parse(base + ["--window", "1920x0"])
    }
}

@Test func reportRoundTripsThroughJSON() throws {
    let report = BenchReport(
        backend: "aether", engineVersion: "6.26.0", fixture: "av1-10bit.mkv",
        deliveredFrames: 2160, droppedFrames: 0, expectedFrames: 2160,
        output: OutputInfo(width: 1920, height: 804, bitDepth: 10, colorTransfer: "bt709", audioChannels: 2),
        startedAt: Date(timeIntervalSince1970: 0), endedAt: Date(timeIntervalSince1970: 75))
    let data = try JSONEncoder.bench.encode(report)
    let back = try JSONDecoder.bench.decode(BenchReport.self, from: data)
    #expect(back.deliveredFrames == 2160)
    #expect(back.output.bitDepth == 10)
    // The shell runner and the Python renderer read this file too, so the dates
    // must be ISO 8601 text, not Foundation's default 2001-epoch double.
    let text = String(decoding: data, as: UTF8.self)
    #expect(text.contains("\"startedAt\" : \"1970-01-01T00:00:00Z\""))
}
