import AppKit
import VLCKitSPM

@MainActor
final class VLCKitBackend: BenchBackend {
    static var engineVersion: String { VLCLibrary.shared().version }

    private let player: VLCMediaPlayer
    private let container = NSView()
    private let parser = VLCMediaParser.shared()
    private var media: VLCMedia?
    private var loadedOutput: OutputInfo?

    var view: NSView { container }
    var output: OutputInfo? { loadedOutput }

    private(set) var nominalFrameRate: Double = 0

    init() {
        player = VLCMediaPlayer()
        player.drawable = container
    }

    func load(_ url: URL) async throws {
        guard let media = VLCMedia(url: url) else { throw BackendError.noOutputDescription }
        self.media = media
        player.media = media

        // 4.0.0-alpha.21 has no `VLCMedia.parse(options:timeout:)`; parsing is
        // queued on a separate `VLCMediaParser` and observed through
        // `parsedStatus`. `.parse` here is `VLCMediaParse` (0x01), the only
        // option needed for track/dimension info: no cover art fetch.
        parser.queue(media, options: [.parse])
        // Bounded, not indefinite: an asynchronous parse that never reaches a
        // terminal status must fail the load rather than hang the benchmark.
        let deadline = Date().addingTimeInterval(15)
        while media.parsedStatus == .none || media.parsedStatus == .pending {
            guard Date() < deadline else { throw BackendError.noOutputDescription }
            try await Task.sleep(for: .milliseconds(50))
        }
        // `.skipped` is a real terminal state here, not a failure: local files
        // can have their track info available without a full parse pass.
        guard media.parsedStatus == .done || media.parsedStatus == .skipped else {
            throw BackendError.noOutputDescription
        }

        // Deliberately not `player.videoSize`: that reflects the live video
        // output and is only populated once `play()` has produced a frame, so
        // reading it here (before playback starts) would report a 0x0 output
        // on every load. The parsed track's own dimensions are available as
        // soon as parsing finishes, with no playback required.
        guard let videoTrack = media.videoTracks.first?.video else {
            throw BackendError.noVideoTrack
        }
        let rateNumerator = Double(videoTrack.frameRate)
        let rateDenominator = Double(videoTrack.frameRateDenominator)
        nominalFrameRate = rateDenominator > 0 ? rateNumerator / rateDenominator : rateNumerator
        // A zero or missing rate would zero out `expectedFrames` downstream and
        // silently disable the runner's validity gate, so refuse to report a
        // session as loaded without one.
        guard nominalFrameRate > 0 else { throw BackendError.noOutputDescription }

        let audioChannels = media.audioTracks.first?.audio?.channelsNumber ?? 0
        loadedOutput = OutputInfo(
            width: Int(videoTrack.width),
            height: Int(videoTrack.height),
            // Not reported by VLCKit 4.0.0a21's media/track API (no bit-depth
            // field on VLCMediaVideoTrack or VLCMediaStats). 0 is the sentinel
            // the other backends use for "not reported by this engine".
            bitDepth: 0,
            colorTransfer: "unreported",
            audioChannels: Int(audioChannels))
    }

    func play() { player.play() }
    func stop() { player.stop() }

    /// VLCKit counts real presented pictures via `VLCMedia.statistics`
    /// (`displayedPictures`/`lostPictures`), not a proxy: unlike AVPlayer and
    /// the AetherEngine native path, this is not derived from elapsed time.
    var deliveredFrames: Int { Int(media?.statistics.displayedPictures ?? 0) }
    var droppedFrames: Int { Int(media?.statistics.lostPictures ?? 0) }
}
