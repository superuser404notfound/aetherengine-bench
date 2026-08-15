import AppKit

@MainActor
protocol BenchBackend: AnyObject {
    static var engineVersion: String { get }
    var view: NSView { get }
    func load(_ url: URL) async throws
    func play()
    func stop()
    var deliveredFrames: Int { get }
    var droppedFrames: Int { get }
    var output: OutputInfo? { get }
}
