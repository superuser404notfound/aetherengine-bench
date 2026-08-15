import AppKit

/// Every way a backend can fail to produce a measurable session. Each one has to
/// be loud: a benchmark that reports zeroes instead of throwing publishes a
/// number that looks like a result.
enum BackendError: Error, CustomStringConvertible {
    case noVideoTrack
    case noOutputDescription
    case notImplemented(BackendKind)
    case wrongBinary(asked: BackendKind)
    /// A deterministic "this engine does not support this source" refusal,
    /// not a malfunction: AVPlayer has no AV1 decoder at all, KSPlayer's
    /// free GPL build gates AV1 behind a paid tier. Kept distinct from
    /// every other BackendError case so BenchRunner can exit with a
    /// distinct code (see REFUSAL_EXIT_CODE in orchestrate.py) instead of
    /// the generic crash exit, and the orchestrator can record it once,
    /// with its reason, instead of retrying a launch that was never going
    /// to succeed and instead of publishing a real capability gap as a
    /// crash, a false statement about the engine.
    case unsupportedFormat(String)

    var description: String {
        switch self {
        case .noVideoTrack: return "source has no usable video track"
        case .noOutputDescription: return "engine never reported an output format"
        case .notImplemented(let kind): return "backend \(kind.rawValue) is a stub, not implemented yet"
        case .wrongBinary(let kind): return "this binary does not carry backend \(kind.rawValue)"
        case .unsupportedFormat(let reason): return "engine refuses this source: \(reason)"
        }
    }
}

@MainActor
protocol BenchBackend: AnyObject {
    static var engineVersion: String { get }
    var view: NSView { get }
    func load(_ url: URL) async throws
    func play()
    func stop()
    var deliveredFrames: Int { get }
    var droppedFrames: Int { get }
    /// The source's nominal frame rate, used to compute how many frames the
    /// measurement window should have contained.
    var nominalFrameRate: Double { get }
    var output: OutputInfo? { get }
    /// Which concrete engine/path actually served the loaded session, for
    /// backends that pick between more than one internal implementation the
    /// way a real host of that engine would (e.g. KSPlayer trying its
    /// primary AVPlayer path before falling back to its own FFmpeg engine).
    /// Which engine served which fixture is itself a result worth
    /// publishing, not just an implementation detail. Defaults to nil for
    /// backends with a single playback path.
    var servingPath: String? { get }
}

extension BenchBackend {
    var servingPath: String? { nil }
}
