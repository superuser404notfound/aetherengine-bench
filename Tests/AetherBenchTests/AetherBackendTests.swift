import Testing
import Foundation

// AV1 without hardware decode dispatches AetherEngine onto its software path
// (`SoftwarePlaybackHost`), where `LiveTelemetry.observedFps` is populated.
// This is the path `deliveredFrames` derives from `observedFps` integration.
@MainActor
@Test func aetherBackendPlaysAndCountsFramesOnSoftwarePath() async throws {
    let fixture = URL(fileURLWithPath: NSString(string: "~/Dev/aetherengine-bench/Fixtures/av1-10bit.mkv").expandingTildeInPath)
    try #require(FileManager.default.fileExists(atPath: fixture.path), "run Scripts/make-fixtures.sh first")

    let backend = try AetherBackend()
    try await backend.load(fixture)
    backend.play()
    try await Task.sleep(for: .seconds(6))
    #expect(backend.deliveredFrames > 0)
    let output = try #require(backend.output)
    // Coded width; the fixture's SAR compensates an odd-to-even height
    // rounding (858 coded, verified with `aetherctl probe`), so only width
    // is asserted here, matching AVPlayerBackendTests' own fixture note.
    #expect(output.width == 1920)
    backend.stop()
}

// HEVC always dispatches onto AetherEngine's native AVPlayer path, where
// `LiveTelemetry.observedFps` is nil (AVPlayer exposes no live fps counter).
// This is the path `deliveredFrames` derives from elapsed time instead.
@MainActor
@Test func aetherBackendPlaysAndCountsFramesOnNativePath() async throws {
    let fixture = URL(fileURLWithPath: NSString(string: "~/Dev/aetherengine-bench/Fixtures/hevc-4k-hdr10.mp4").expandingTildeInPath)
    try #require(FileManager.default.fileExists(atPath: fixture.path), "run Scripts/make-fixtures.sh first")

    let backend = try AetherBackend()
    try await backend.load(fixture)
    backend.play()
    try await Task.sleep(for: .seconds(6))
    #expect(backend.deliveredFrames > 0)
    let output = try #require(backend.output)
    #expect(output.bitDepth == 10)
    backend.stop()
}
