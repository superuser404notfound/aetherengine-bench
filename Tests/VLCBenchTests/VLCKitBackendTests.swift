import Testing
import Foundation
import AppKit

// VLCKit's macOS video output backend refuses to create a display without a
// live NSApplication and an on-screen drawable ("cannot create video output
// window without NSApplication", observed directly from libvlc's own log).
// The real VLCBench binary satisfies this via `main.swift` + `BenchWindow`;
// a bare XCTest bundle does neither, so these tests reproduce the same
// hosting BenchRunner does rather than working around the requirement.
@MainActor
private func hostOnScreen(_ backend: VLCKitBackend) {
    NSApplication.shared.setActivationPolicy(.regular)
    _ = BenchWindow.make(size: CGSize(width: 960, height: 540), displayIndex: 0, hosting: backend.view)
}

@MainActor
@Test func vlckitBackendPlaysAndCountsFrames() async throws {
    let fixture = URL(fileURLWithPath: NSString(string: "~/Dev/aetherengine-bench/Fixtures/av1-10bit.mkv").expandingTildeInPath)
    try #require(FileManager.default.fileExists(atPath: fixture.path), "run Scripts/make-fixtures.sh first")

    let backend = VLCKitBackend()
    hostOnScreen(backend)
    try await backend.load(fixture)
    #expect(backend.nominalFrameRate > 0)
    let output = try #require(backend.output)
    #expect(output.width == 1920)
    #expect(output.audioChannels == 2)

    backend.play()
    try await Task.sleep(for: .seconds(6))
    #expect(backend.deliveredFrames > 0)
    backend.stop()
}

@MainActor
@Test func vlckitBackendReportsOutputForHEVCFixture() async throws {
    let fixture = URL(fileURLWithPath: NSString(string: "~/Dev/aetherengine-bench/Fixtures/hevc-4k-hdr10.mp4").expandingTildeInPath)
    try #require(FileManager.default.fileExists(atPath: fixture.path), "run Scripts/make-fixtures.sh first")

    let backend = VLCKitBackend()
    hostOnScreen(backend)
    try await backend.load(fixture)
    let output = try #require(backend.output)
    #expect(output.width == 3840)
    #expect(output.audioChannels == 2)
    backend.stop()
}

// The specific risk with this alpha SDK is a statistics accessor that
// silently reads zero forever, which looks exactly like a perfect engine in
// a single before/after check. Sampling four times across the play window
// and requiring the counter to move more than once catches a counter that
// jumps once (e.g. on first frame) and then sits still, which a bare
// `deliveredFrames > 0` assertion at the end would miss entirely.
@MainActor
@Test func vlckitBackendDeliveredFramesCounterIsLiveNotConstant() async throws {
    let fixture = URL(fileURLWithPath: NSString(string: "~/Dev/aetherengine-bench/Fixtures/av1-10bit.mkv").expandingTildeInPath)
    try #require(FileManager.default.fileExists(atPath: fixture.path), "run Scripts/make-fixtures.sh first")

    let backend = VLCKitBackend()
    hostOnScreen(backend)
    try await backend.load(fixture)
    backend.play()

    var samples: [Int] = []
    for _ in 0..<4 {
        try await Task.sleep(for: .seconds(2))
        samples.append(backend.deliveredFrames)
    }
    backend.stop()

    // Monotonic non-decreasing across the whole window.
    for i in 1..<samples.count {
        #expect(samples[i] >= samples[i - 1], "deliveredFrames went backwards: \(samples)")
    }
    // At least two distinct increases, so a counter that only jumps once
    // (e.g. at first frame, then never again) cannot pass this test.
    let increases = zip(samples, samples.dropFirst()).filter { $1 > $0 }.count
    #expect(increases >= 2, "deliveredFrames advanced at most once, samples: \(samples)")
}
