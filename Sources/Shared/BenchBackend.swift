import AppKit

/// Every way a backend can fail to produce a measurable session. Each one has to
/// be loud: a benchmark that reports zeroes instead of throwing publishes a
/// number that looks like a result.
enum BackendError: Error, CustomStringConvertible {
    case noVideoTrack
    case noOutputDescription
    case notImplemented(BackendKind)
    case wrongBinary(asked: BackendKind)

    var description: String {
        switch self {
        case .noVideoTrack: return "source has no usable video track"
        case .noOutputDescription: return "engine never reported an output format"
        case .notImplemented(let kind): return "backend \(kind.rawValue) is a stub, not implemented yet"
        case .wrongBinary(let kind): return "this binary does not carry backend \(kind.rawValue)"
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
}
