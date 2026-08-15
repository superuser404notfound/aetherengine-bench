import AetherEngine
import AppKit
import Combine

/// `AetherEngine` exposes no runtime version API (checked against the whole
/// `Sources/AetherEngine` tree and `docs/api.md` at the pinned revision, not
/// just the docs). This literal is the release tag the pin in `project.yml`
/// resolves to: revision `73354ec8c6d20ca954b1874254b69fa19e2ffce5` is tag
/// `6.26.0`, confirmed with `git describe --tags` against that revision in
/// the AetherEngine checkout. Update by hand whenever that revision moves;
/// nothing derives it automatically because `Package.resolved` records the
/// revision, not the tag it corresponds to.
private let aetherEngineReleaseVersion = "6.26.0"

@MainActor
final class AetherBackend: BenchBackend {
    static var engineVersion: String { aetherEngineReleaseVersion }

    private let engine: AetherEngine
    private let surface = AetherPlayerView()
    private var cancellables = Set<AnyCancellable>()
    private var loadedOutput: OutputInfo?
    private var lastDropped = 0
    private var integratedFrames = 0.0
    private var sawSoftwareFps = false

    var view: NSView { surface }
    var output: OutputInfo? { loadedOutput }
    var droppedFrames: Int { lastDropped }

    /// `LiveTelemetry.observedFps` is populated only on the software path
    /// (`SoftwarePlaybackHost`); it is nil on native AVPlayer playback,
    /// which has no live fps counter to report. So delivered frames must be
    /// derived differently per path, never by one formula covering both:
    ///
    /// - Software: `observedFps` is a rate sampled at 1 Hz
    ///   (`diagnostics.liveTelemetry`'s documented cadence), so summing the
    ///   value once per tick approximates its time integral, i.e. frames
    ///   actually decoded.
    /// - Native: there is nothing to integrate, so this falls back to
    ///   elapsed playback time times the nominal rate, exactly like
    ///   `AVPlayerBackend`. It is optimistic (it cannot see frames AVPlayer
    ///   never admits to), which is why it is gated on `sawSoftwareFps`
    ///   rather than tried first: falling back to it on the software path
    ///   too would silently paper over the one thing this backend exists to
    ///   report honestly.
    ///
    /// Both branches subtract `lastDropped`, mirroring `AVPlayerBackend`.
    var deliveredFrames: Int {
        if sawSoftwareFps {
            return max(0, Int(integratedFrames) - lastDropped)
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
                if let fps = telemetry.observedFps, fps > 0 {
                    self.sawSoftwareFps = true
                    self.integratedFrames += fps
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
