import AetherEngine
import AppKit
import Combine

@MainActor
final class AetherBackend: BenchBackend {
    /// `AetherEngine.version` exists since 7.3.0 and is checked against the
    /// README and CHANGELOG by the engine's own tests, so a standalone report
    /// names the build it ran. The published version still comes from
    /// `orchestrate.py` (`git describe` on the SwiftPM checkout).
    static var engineVersion: String { AetherEngine.version }

    private let engine: AetherEngine
    private let surface = AetherPlayerView()
    private var cancellables = Set<AnyCancellable>()
    private var loadedOutput: OutputInfo?
    private var lastDropped = 0
    private var isSoftwarePath = false

    var view: NSView { surface }
    var output: OutputInfo? { loadedOutput }
    var droppedFrames: Int { lastDropped }

    /// `LiveTelemetry.observedFps` is populated only on the software path
    /// (`SoftwarePlaybackHost`) and nil on native AVPlayer playback, which
    /// has no live fps counter at all. It is a reliable path discriminator
    /// (`isSoftwarePath`), but not a reliable frame count: it is a 10-second
    /// *rolling average* (`LiveTelemetrySampler`'s `RollingWindow<Int64>`),
    /// so integrating it double-counts the startup ramp for as long as that
    /// ramp sits inside the window, undercounting a short measure window by
    /// a wide margin. `engine.softwareHostFramesEnqueued` is the engine's
    /// own exact monotonic counter for the same signal (frames the SW host
    /// enqueued into `AVSampleBufferDisplayLayer`, zero on native / pre-start,
    /// restarts at zero on a new `load()`), so the software branch reads
    /// that directly instead of integrating a rate.
    ///
    /// The native path has no equivalent counter, so it falls back to
    /// elapsed playback time times the nominal rate, exactly like
    /// `AVPlayerBackend`. It is optimistic (it cannot see frames AVPlayer
    /// never admits to); the doc comment there states the same limitation,
    /// this is a gate-only proxy, never a quality number to compare against
    /// an engine with a real counter.
    ///
    /// Both branches subtract `lastDropped`.
    var deliveredFrames: Int {
        if isSoftwarePath {
            return max(0, engine.softwareHostFramesEnqueued - lastDropped)
        }
        return max(0, Int(engine.currentTime * nominalFrameRate) - lastDropped)
    }

    private(set) var nominalFrameRate: Double = 0

    init() throws {
        engine = try AetherEngine()
        engine.bind(view: surface)
        engine.diagnostics.$liveTelemetry
            .compactMap { $0 }
            .sink { [weak self] telemetry in
                guard let self else { return }
                // droppedFrameCount is populated on both paths (access log
                // on native, render synchronizer on software), but a
                // telemetry tick's field is optional per-sample; a nil tick
                // keeps the last known count rather than resetting to 0.
                self.lastDropped = telemetry.droppedFrameCount ?? self.lastDropped
                // observedFps is only ever non-nil on the software path, so
                // its presence (not its value) is what selects the branch.
                if telemetry.observedFps != nil {
                    self.isSoftwarePath = true
                }
            }
            .store(in: &cancellables)
    }

    func load(_ url: URL) async throws {
        guard let probe = try await engine.load(url: url) else {
            throw BackendError.noOutputDescription
        }
        // A zero or missing rate would zero out `expectedFrames` downstream
        // and silently disable the runner's validity gate, so this backend
        // refuses to report a session as loaded without one.
        guard let rate = probe.videoFrameRate, rate > 0 else {
            throw BackendError.noOutputDescription
        }
        nominalFrameRate = rate
        loadedOutput = OutputInfo(
            width: Int(probe.videoWidth),
            height: Int(probe.videoHeight),
            bitDepth: Self.bitDepth(for: probe.videoFormat),
            // Informational per engine only, never comparable across
            // backends: AVPlayerBackend puts a raw AVFoundation transfer-
            // function tag (e.g. "SMPTE_ST_2084_PQ") in this same field,
            // this backend puts a VideoFormat case name (e.g. "hdr10").
            // Same field name, different kind of string.
            colorTransfer: String(describing: probe.videoFormat),
            audioChannels: probe.audioTracks.first?.channels ?? 0)
    }

    func play() { engine.play() }
    func stop() { engine.stop() }

    /// `SourceProbe` carries no bit-depth field, `videoFormat` is the
    /// nearest signal the engine exposes. HDR10 / HDR10+ / Dolby Vision /
    /// HLG all imply a 10-bit (or deeper) source at the container level;
    /// SDR could be 8 or 10-bit and the probe does not say which, so this
    /// reports the conservative 8, the same approximation `AVPlayerBackend`
    /// makes from the PQ / HLG transfer function.
    private static func bitDepth(for format: VideoFormat) -> Int {
        switch format {
        case .sdr: return 8
        case .hdr10, .hdr10Plus, .dolbyVision, .hlg: return 10
        }
    }
}
