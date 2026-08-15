import KSPlayer
import AVFoundation
import AppKit

/// `KSMEPlayer`, not `KSAVPlayer` or the SwiftUI `KSVideoPlayer` wrapper.
/// `KSAVPlayer` is a thin `MediaPlayerProtocol` shim around `AVPlayer` itself
/// (no FFmpeg involved), which would just re-measure AVFoundation a second
/// time under a different name. `KSMEPlayer` is KSPlayer's own demux/decode
/// pipeline through FFmpeg, the "second player type" the README's own
/// initialization example sets explicitly (`KSOptions.secondPlayerType =
/// KSMEPlayer.self`) and the only concrete type that engages the engine this
/// benchmark exists to compare against AetherEngine. Working at the
/// `MediaPlayerProtocol` level (not `KSVideoPlayer`) keeps the measurement on
/// KSPlayer's own engine, not its SwiftUI layer.
@MainActor
final class KSPlayerBackend: BenchBackend {
    /// KSPlayer exposes no runtime version API. This mirrors the exact
    /// version pinned in `project-ksplayer.yml` (`exactVersion: 2.3.4`); the
    /// version this benchmark actually publishes comes from
    /// `Package.resolved` via `orchestrate.py`, not this literal.
    static var engineVersion: String { "KSPlayer 2.3.4 (free GPL build)" }

    private var player: KSMEPlayer?
    private let container = NSView()
    private var loadedOutput: OutputInfo?
    private var readyContinuation: CheckedContinuation<Void, Error>?
    private var readyTimeoutTask: Task<Void, Never>?

    var view: NSView { container }
    var output: OutputInfo? { loadedOutput }
    private(set) var nominalFrameRate: Double = 0

    func load(_ url: URL) async throws {
        let options = KSOptions()
        let newPlayer = KSMEPlayer(url: url, options: options)
        newPlayer.delegate = self
        player = newPlayer

        if let playerView = newPlayer.view {
            playerView.frame = container.bounds
            playerView.autoresizingMask = [.width, .height]
            container.addSubview(playerView)
        }

        // KSMEPlayer has no async load API: readiness comes back through
        // MediaPlayerDelegate.readyToPlay (or .finish on a failed open),
        // fired once prepareToPlay's background probe completes. Bounded
        // with a manual deadline, not a structured-concurrency race against
        // the continuation itself: a CheckedContinuation is not cancellable,
        // so racing it inside a TaskGroup would leave the loser suspended
        // forever and deadlock the group's implicit teardown. This mirrors
        // VLCKitBackend's bounded parse wait, just implemented so the loser
        // is always the one to resume, never left dangling.
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            self.readyContinuation = continuation
            self.readyTimeoutTask = Task { [weak self] in
                try? await Task.sleep(for: .seconds(15))
                guard let self, let pending = self.readyContinuation else { return }
                self.readyContinuation = nil
                pending.resume(throwing: BackendError.noOutputDescription)
            }
            newPlayer.prepareToPlay()
        }

        let videoTracks = newPlayer.tracks(mediaType: .video)
        guard let videoTrack = videoTracks.first(where: { $0.isEnabled }) ?? videoTracks.first else {
            throw BackendError.noVideoTrack
        }

        // A zero or missing rate would zero out `expectedFrames` downstream
        // and silently disable the runner's validity gate, so this backend
        // refuses to report a session as loaded without one.
        let rate = Double(newPlayer.nominalFrameRate)
        guard rate > 0 else { throw BackendError.noOutputDescription }
        nominalFrameRate = rate

        // `naturalSize` (`MediaPlayerTrack`'s `formatDescription?.naturalSize`
        // under the hood) legitimately comes back zero for AV1 sources in this
        // free GPL build: confirmed empirically, not just from the README, by
        // isolating codec from container (see KSPlayerBackendTests). KSPlayer's
        // demuxer does find and describe the AV1 stream (one enabled video
        // track, correct nominal frame rate), it is specifically
        // CMVideoFormatDescriptionCreate over the AV1 codec parameters that
        // fails to produce a description, matching the README's "AV1 hardware
        // decoding: GPL no / LGPL yes" row. There is no other public accessor
        // for coded dimensions on MediaPlayerTrack, so refusing the session
        // here is the honest outcome, not a bug to route around.
        let size = newPlayer.naturalSize
        guard size.width > 0, size.height > 0 else { throw BackendError.noOutputDescription }

        let audioTracks = newPlayer.tracks(mediaType: .audio)
        var channels = 0
        if let audioTrack = audioTracks.first(where: { $0.isEnabled }) ?? audioTracks.first,
           let basic = audioTrack.audioStreamBasicDescription {
            channels = Int(basic.mChannelsPerFrame)
        }

        loadedOutput = OutputInfo(
            width: Int(size.width),
            height: Int(size.height),
            // MediaPlayerTrack.bitDepth is a real per-track field (from the
            // decoded format description), not the SDR/HDR heuristic
            // AVPlayerBackend and AetherBackend fall back to.
            bitDepth: Int(videoTrack.bitDepth),
            // Informational per engine only, never comparable across
            // backends, same caveat as the other three backends' colorTransfer.
            colorTransfer: videoTrack.transferFunction ?? "unreported",
            audioChannels: channels)
    }

    func play() { player?.play() }

    func stop() {
        readyTimeoutTask?.cancel()
        readyTimeoutTask = nil
        readyContinuation?.resume(throwing: CancellationError())
        readyContinuation = nil
        player?.shutdown()
    }

    /// KSMEPlayer's public surface has no cumulative presented-frame counter
    /// the way VLCKit's `VLCMedia.statistics.displayedPictures` is one:
    /// `MEPlayerItem` ticks a real per-frame counter internally
    /// (`videoDisplayCount`, incremented in `setVideo(time:position:)` on
    /// every frame handed to the display timebase), but that property is not
    /// `public`, so it is unreachable from outside the KSPlayer module. The
    /// only public derivative, `dynamicInfo.displayFPS`, is a periodically
    /// recomputed rate (reset every >1s window inside MEPlayerItem itself),
    /// not a counter, so integrating it would double-count exactly the way
    /// AetherBackend's own doc comment describes for
    /// `LiveTelemetry.observedFps`. This uses the same proxy AVPlayerBackend
    /// (its only option) and AetherBackend's native path use instead: elapsed
    /// playback time times the nominal rate, minus the real drops KSPlayer
    /// admits to. Gate-only, never a quality number against a backend with a
    /// real counter (VLCKit).
    var deliveredFrames: Int {
        guard let player, nominalFrameRate > 0 else { return 0 }
        return max(0, Int(player.currentPlaybackTime * nominalFrameRate) - droppedFrames)
    }

    /// `DynamicInfo.droppedVideoFrameCount` is a real, live public counter,
    /// confirmed by reading the increment sites in `MEPlayerItem`
    /// (`getVideoOutputRender`, on genuine drop and flush events), not just
    /// its declaration. `dynamicInfo` is declared `DynamicInfo?` by
    /// `MediaPlayerProtocol`, but `MEPlayerItem` backs it with a `lazy var`
    /// of the non-optional type, so it is never actually nil once a player
    /// exists; `?? 0` here covers only the case of no player having been
    /// loaded yet, not a missing counter.
    var droppedFrames: Int {
        Int(player?.dynamicInfo?.droppedVideoFrameCount ?? 0)
    }
}

extension KSPlayerBackend: MediaPlayerDelegate {
    func readyToPlay(player: some MediaPlayerProtocol) {
        readyTimeoutTask?.cancel()
        readyTimeoutTask = nil
        readyContinuation?.resume()
        readyContinuation = nil
    }

    func changeLoadState(player: some MediaPlayerProtocol) {}
    func changeBuffering(player: some MediaPlayerProtocol, progress: Int) {}
    func playBack(player: some MediaPlayerProtocol, loopCount: Int) {}

    func finish(player: some MediaPlayerProtocol, error: Error?) {
        guard let pending = readyContinuation else { return }
        readyTimeoutTask?.cancel()
        readyTimeoutTask = nil
        readyContinuation = nil
        pending.resume(throwing: error ?? BackendError.noOutputDescription)
    }
}
