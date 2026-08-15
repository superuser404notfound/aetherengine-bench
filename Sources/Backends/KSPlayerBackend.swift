// Placeholder stub, until Task 6 replaces it with the real KSPlayer backend.
import AppKit

@MainActor
final class KSPlayerBackend: BenchBackend {
    static var engineVersion: String { "stub" }
    let view = NSView()
    func load(_ url: URL) async throws { throw BackendError.notImplemented(.ksplayer) }
    func play() {}
    func stop() {}
    var deliveredFrames: Int { 0 }
    var droppedFrames: Int { 0 }
    var nominalFrameRate: Double { 0 }
    var output: OutputInfo? { nil }
}
