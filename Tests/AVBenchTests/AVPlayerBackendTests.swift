import Testing
import Foundation
import AVFoundation

@MainActor
@Test func avplayerBackendReportsOutputAfterLoad() async throws {
    let fixture = URL(fileURLWithPath: NSString(string: "~/Dev/aetherengine-bench/Fixtures/h264-1080p.mp4").expandingTildeInPath)
    try #require(FileManager.default.fileExists(atPath: fixture.path), "run Scripts/make-fixtures.sh first")

    let backend = AVPlayerBackend()
    try await backend.load(fixture)
    let output = try #require(backend.output)
    #expect(output.width == 1920)
    #expect(output.audioChannels == 2)
    backend.stop()
}

@MainActor
@Test func avplayerBackendThrowsOnSourceWithNoVideoTrack() async throws {
    let tempDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
    try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: tempDir) }

    // A short silent mono track, audio only. Written with AVAudioFile rather than
    // pulled from Fixtures/, which is reserved for the seven verified playback
    // fixtures every backend measures against.
    let audioOnly = tempDir.appendingPathComponent("audio-only.caf")
    let format = try #require(AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 1))
    let audioFile = try AVAudioFile(forWriting: audioOnly, settings: format.settings)
    let buffer = try #require(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4_410))
    buffer.frameLength = 4_410
    try audioFile.write(from: buffer)

    let backend = AVPlayerBackend()
    do {
        try await backend.load(audioOnly)
        Issue.record("expected load(_:) to throw for a source with no video track")
    } catch BackendError.noVideoTrack {
        // expected
    } catch {
        Issue.record("expected BackendError.noVideoTrack, got \(error)")
    }
}

// AVPlayer has no AV1 decoder at all on this platform: a deterministic
// capability gap, not a malfunction. Verified live before writing this
// classifier: asset.loadTracks(withMediaType:) throws a plain NSError for
// this fixture, AVFoundationErrorDomain code -11828 ("Cannot Open"),
// localizedFailureReason "This media format is not supported." classify()
// exists specifically to turn that into BackendError.unsupportedFormat so
// BenchRunner exits with the distinct REFUSAL code instead of the generic
// crash exit, and orchestrate.py records it once instead of retrying a
// launch that was never going to succeed. This is the one guarantee this
// test actually needs: whatever AVFoundation's exact wording is on a given
// OS release, it must not surface as .noVideoTrack/.noOutputDescription or
// any other case that reads as a crash to BenchRunner.
@MainActor
@Test func avplayerBackendRefusesAV1AsUnsupportedFormatNotACrash() async throws {
    let fixture = URL(fileURLWithPath: NSString(string: "~/Dev/aetherengine-bench/Fixtures/av1-10bit.mkv").expandingTildeInPath)
    try #require(FileManager.default.fileExists(atPath: fixture.path), "run Scripts/make-fixtures.sh first")

    let backend = AVPlayerBackend()
    do {
        try await backend.load(fixture)
        Issue.record("expected load(_:) to throw for an AV1 source AVPlayer cannot decode")
    } catch BackendError.unsupportedFormat(let reason) {
        #expect(!reason.isEmpty)
    } catch {
        Issue.record("expected BackendError.unsupportedFormat, got \(error) (a crash-shaped case here " +
                     "would mean this AV1 fixture is now being misreported as a malfunction, not a refusal)")
    }
}
