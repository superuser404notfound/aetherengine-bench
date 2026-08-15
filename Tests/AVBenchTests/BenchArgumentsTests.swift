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

@Test func reportRoundTripsThroughJSON() throws {
    let report = BenchReport(
        backend: "aether", engineVersion: "6.26.0", fixture: "av1-10bit.mkv",
        deliveredFrames: 2160, droppedFrames: 0, expectedFrames: 2160,
        output: OutputInfo(width: 1920, height: 804, bitDepth: 10, colorTransfer: "bt709", audioChannels: 2),
        startedAt: Date(timeIntervalSince1970: 0), endedAt: Date(timeIntervalSince1970: 75))
    let data = try JSONEncoder().encode(report)
    let back = try JSONDecoder().decode(BenchReport.self, from: data)
    #expect(back.deliveredFrames == 2160)
    #expect(back.output.bitDepth == 10)
}
