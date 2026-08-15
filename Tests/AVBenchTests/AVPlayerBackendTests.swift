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
