// Placeholder stub, until Task 4 replaces it with the real AetherEngine backend.
import AppKit

@MainActor
final class AetherBackend: BenchBackend {
    static var engineVersion: String { "stub" }
    let view = NSView()
    func load(_ url: URL) async throws { throw BackendError.notImplemented(.aether) }
    func play() {}
    func stop() {}
    var deliveredFrames: Int { 0 }
    var droppedFrames: Int { 0 }
    var nominalFrameRate: Double { 0 }
    var output: OutputInfo? { nil }
}
