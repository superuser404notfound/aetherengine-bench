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
