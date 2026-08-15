// KSOptions.firstPlayerType/secondPlayerType are plain `static var` globals
// in KSPlayer 2.3.4 (tools-version 5.9, not Swift 6 strict concurrency
// throughout its public API), so reading them under this package's Swift 6
// language mode needs `@preconcurrency` to avoid a hard "not
// concurrency-safe" error over state KSPlayer itself never isolates.
@preconcurrency import KSPlayer
import AVFoundation
import AppKit

/// Entry point: mirrors KSPlayer's own selection, not a single pinned type.
/// `KSOptions.firstPlayerType` defaults to `KSAVPlayer` (a thin wrapper
/// around a real `AVPlayer`, no FFmpeg involved) and `secondPlayerType` to
/// `KSMEPlayer` (KSPlayer's own FFmpeg demux/decode engine). `KSPlayerLayer`,
/// KSPlayer's own host, only falls back to the second type when the first's
/// delegate reports `finish(player:error:)` with a non-nil error (confirmed
/// by reading `KSPlayerLayer.finish(player:error:)` directly, not inferred
/// from the README). Pinning `KSMEPlayer` unconditionally would measure
/// KSPlayer's FFmpeg fallback on every fixture, even ones a real host would
/// serve straight off AVPlayer, understating the one competitor closest to
/// AetherEngine. This backend reproduces the same two-step trial and records
/// which type actually served the session (`servingPath`): which engine
/// served which fixture is itself a result worth publishing.
@MainActor
final class KSPlayerBackend: BenchBackend {
    /// KSPlayer exposes no runtime version API. This mirrors the exact
    /// version pinned in `project-ksplayer.yml` (`exactVersion: 2.3.4`); the
    /// version this benchmark actually publishes comes from
    /// `Package.resolved` via `orchestrate.py`, not this literal.
    static var engineVersion: String { "KSPlayer 2.3.4 (free GPL build)" }

    private var player: (any MediaPlayerProtocol)?
    private(set) var servingPath: String?
    private let container = NSView()
    private var loadedOutput: OutputInfo?
    private var readyContinuation: CheckedContinuation<Bool, Never>?
    private var readyTimeoutTask: Task<Void, Never>?
    private var pendingCandidate: (any MediaPlayerProtocol)?

    var view: NSView { container }
    var output: OutputInfo? { loadedOutput }
    private(set) var nominalFrameRate: Double = 0

    func load(_ url: URL) async throws {
        let options = KSOptions()
        var candidates: [any MediaPlayerProtocol.Type] = [KSOptions.firstPlayerType]
        if let second = KSOptions.secondPlayerType {
            candidates.append(second)
        }

        var served: (any MediaPlayerProtocol)?
        var servedType: (any MediaPlayerProtocol.Type)?
        for type in candidates {
            let candidate = type.init(url: url, options: options)
            // View attached before prepareToPlay(), matching KSPlayerLayer's
            // own `player` didSet (view inserted into the live hierarchy,
            // delegate set, then prepareToPlay()). Not cosmetic: KSAVPlayer's
            // asset/track loading behaves differently when its view was never
            // part of a window during the attempt.
            if let candidateView = candidate.view {
                candidateView.frame = container.bounds
                candidateView.autoresizingMask = [.width, .height]
                container.addSubview(candidateView)
            }
            let ok = await attemptLoad(candidate)
            if ok {
                served = candidate
                servedType = type
                break
            }
            candidate.view?.removeFromSuperview()
            // Not shutting the failed candidate down explicitly: KSPlayerLayer's
            // own fallback (KSPlayerLayer.finish(player:error:)) doesn't either,
            // it just replaces the reference and lets ARC release it. Mirroring
            // that rather than introducing a teardown step their own host skips.
        }

        guard let served, let servedType else {
            // Neither the primary AVPlayer path nor the FFmpeg fallback could
            // even get a usable video track going for this source.
            throw BackendError.noVideoTrack
        }
        player = served
        servingPath = String(describing: servedType)

        let videoTracks = served.tracks(mediaType: .video)
        guard let videoTrack = videoTracks.first(where: { $0.isEnabled }) ?? videoTracks.first else {
            throw BackendError.noVideoTrack
        }

        // A zero or missing rate would zero out `expectedFrames` downstream
        // and silently disable the runner's validity gate, so this backend
        // refuses to report a session as loaded without one.
        let rate = Double(served.nominalFrameRate)
        guard rate > 0 else { throw BackendError.noOutputDescription }
        nominalFrameRate = rate

        // Coded dimensions via CMVideoFormatDescriptionGetDimensions, not
        // `naturalSize`: `naturalSize` is PAR-corrected, matching why
        // AVPlayerBackend and AetherBackend both read coded dimensions
        // instead. A non-square-SAR source (e.g. hevc-subs.mkv, 858:857)
        // would otherwise report a different height than the other
        // backends' coded number even though nothing about the decode
        // differs, and the runner's validity gate compares output
        // descriptions across engines.
        //
        // This is also where the AV1-in-the-free-GPL-build limitation
        // actually surfaces, confirmed by reading FFmpegAssetTrack's video
        // init: KSMEPlayer's own demuxer does find and describe an AV1
        // stream (an enabled video track, correct nominal frame rate) well
        // enough to fire readyToPlay, but CMVideoFormatDescriptionCreate
        // never produces a format description for it, so formatDescription
        // is nil here and there are no coded dimensions to read, matching
        // the README's "AV1 hardware decoding: GPL no / LGPL yes" row.
        // There is no other public accessor for coded dimensions on
        // MediaPlayerTrack, so refusing the session here is the honest
        // outcome, not a bug to route around.
        guard let formatDescription = videoTrack.formatDescription else {
            throw BackendError.noOutputDescription
        }
        let dimensions = CMVideoFormatDescriptionGetDimensions(formatDescription)
        guard dimensions.width > 0, dimensions.height > 0 else {
            throw BackendError.noOutputDescription
        }

        let audioTracks = served.tracks(mediaType: .audio)
        var channels = 0
        if let audioTrack = audioTracks.first(where: { $0.isEnabled }) ?? audioTracks.first,
           let basic = audioTrack.audioStreamBasicDescription {
            channels = Int(basic.mChannelsPerFrame)
        }

        loadedOutput = OutputInfo(
            width: Int(dimensions.width),
            height: Int(dimensions.height),
            // MediaPlayerTrack.bitDepth is a real per-track field (from the
            // decoded format description) on both KSAVPlayer's and
            // KSMEPlayer's track types, not the SDR/HDR heuristic
            // AVPlayerBackend and AetherBackend fall back to.
            bitDepth: Int(videoTrack.bitDepth),
            // Informational per engine only, never comparable across
            // backends, same caveat as the other three backends' colorTransfer.
            colorTransfer: videoTrack.transferFunction ?? "unreported",
            audioChannels: channels)
    }

    /// Bridges one candidate's MediaPlayerDelegate readiness signal into
    /// async/await with a bounded 15s deadline (matching VLCKitBackend's
    /// bounded parse wait), returning whether it became playable rather than
    /// throwing: a failed first candidate is an expected step of the
    /// selection, not a load failure. A CheckedContinuation is not
    /// cancellable, so the deadline is a manual Task racing to resume the
    /// same continuation, not a structured-concurrency TaskGroup race, which
    /// would deadlock the group's implicit teardown on the losing,
    /// un-cancellable side. `pendingCandidate` identity-checked in the
    /// delegate callbacks below so a late signal from an already-abandoned
    /// candidate can never resolve the next candidate's continuation.
    private func attemptLoad(_ candidate: any MediaPlayerProtocol) async -> Bool {
        await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
            pendingCandidate = candidate
            readyContinuation = continuation
            readyTimeoutTask = Task { [weak self] in
                try? await Task.sleep(for: .seconds(15))
                guard let self, let pending = self.readyContinuation else { return }
                self.readyContinuation = nil
                self.pendingCandidate = nil
                pending.resume(returning: false)
            }
            candidate.delegate = self
            candidate.prepareToPlay()
        }
    }

    func play() { player?.play() }

    func stop() {
        readyTimeoutTask?.cancel()
        readyTimeoutTask = nil
        readyContinuation?.resume(returning: false)
        readyContinuation = nil
        pendingCandidate = nil
        player?.shutdown()
    }

    /// Same elapsed-time-times-nominal-rate proxy as AVPlayerBackend and
    /// AetherBackend's native path: neither KSAVPlayer nor KSMEPlayer expose
    /// a public cumulative presented-frame counter (see `droppedFrames`
    /// below for why `dynamicInfo` cannot fill that gap either). Gate-only,
    /// never a quality number against a backend with a real counter
    /// (VLCKit). Subtracts `max(0, droppedFrames)`, not `droppedFrames`
    /// directly: when KSAVPlayer served the session, `droppedFrames` reads
    /// `-1` (not reported, see below), and an unknown drop count must not be
    /// treated as a negative one.
    var deliveredFrames: Int {
        guard let player, nominalFrameRate > 0 else { return 0 }
        return max(0, Int(player.currentPlaybackTime * nominalFrameRate) - max(0, droppedFrames))
    }

    /// `-1` (not reported), never a fabricated `0`, whenever `dynamicInfo`
    /// is nil. This is not a hypothetical fallback: `KSAVPlayer.dynamicInfo`
    /// is a hardcoded `public let dynamicInfo: DynamicInfo? = nil`
    /// (confirmed by reading `KSAVPlayer.swift` directly), so any session
    /// served by KSPlayer's primary AVPlayer path has no drop counter at
    /// all, not a counter that happens to read zero. `KSMEPlayer` backs
    /// `dynamicInfo` with a real `lazy var` of the non-optional type
    /// (confirmed never actually nil once that player exists), and
    /// `droppedVideoFrameCount` is live there, confirmed at its increment
    /// sites in `MEPlayerItem` (on genuine drop and flush events), not just
    /// its declaration.
    var droppedFrames: Int {
        guard let dynamicInfo = player?.dynamicInfo else { return -1 }
        return Int(dynamicInfo.droppedVideoFrameCount)
    }
}

extension KSPlayerBackend: MediaPlayerDelegate {
    func readyToPlay(player: some MediaPlayerProtocol) {
        guard let pendingCandidate, pendingCandidate === player else { return }
        readyTimeoutTask?.cancel()
        readyTimeoutTask = nil
        self.pendingCandidate = nil
        readyContinuation?.resume(returning: true)
        readyContinuation = nil
    }

    func changeLoadState(player: some MediaPlayerProtocol) {}
    func changeBuffering(player: some MediaPlayerProtocol, progress: Int) {}
    func playBack(player: some MediaPlayerProtocol, loopCount: Int) {}

    func finish(player: some MediaPlayerProtocol, error: Error?) {
        guard let pendingCandidate, pendingCandidate === player else { return }
        readyTimeoutTask?.cancel()
        readyTimeoutTask = nil
        self.pendingCandidate = nil
        readyContinuation?.resume(returning: false)
        readyContinuation = nil
    }
}
