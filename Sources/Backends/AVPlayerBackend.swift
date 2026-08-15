import AVFoundation
import AVKit
import AppKit

@MainActor
final class AVPlayerBackend: BenchBackend {
    static var engineVersion: String { ProcessInfo.processInfo.operatingSystemVersionString }

    private let player = AVPlayer()
    private let playerView = AVPlayerView()
    private var item: AVPlayerItem?
    private var loadedOutput: OutputInfo?

    var view: NSView { playerView }
    var output: OutputInfo? { loadedOutput }

    init() {
        playerView.player = player
        playerView.controlsStyle = .none
        playerView.videoGravity = .resizeAspect
    }

    func load(_ url: URL) async throws {
        let asset = AVURLAsset(url: url)
        let item = AVPlayerItem(asset: asset)
        self.item = item
        player.replaceCurrentItem(with: item)

        let videoTracks: [AVAssetTrack]
        let audioTracks: [AVAssetTrack]
        do {
            videoTracks = try await asset.loadTracks(withMediaType: .video)
            audioTracks = try await asset.loadTracks(withMediaType: .audio)
        } catch {
            throw Self.classify(error)
        }
        // No usable video track means there is nothing to measure. Returning
        // quietly here would let the runner play silence for the full window and
        // write a report full of zeroes that reads like a real measurement.
        guard let video = videoTracks.first else { throw BackendError.noVideoTrack }
        let descriptions = try await video.load(.formatDescriptions)
        // Coded pixel dimensions, not `naturalSize`. `naturalSize` folds in the
        // pixel aspect ratio, so a fixture whose SAR compensates for an
        // odd-to-even height rounding (858:857 here) reports a display width a
        // couple of pixels off the decoder's actual output. The FFmpeg-backed
        // engines this benchmark also drives report coded dimensions, so this
        // keeps `OutputInfo.width/height` comparable across backends.
        guard let formatDescription = descriptions.first else { throw BackendError.noOutputDescription }
        let dimensions = CMVideoFormatDescriptionGetDimensions(formatDescription)
        let ext = CMFormatDescriptionGetExtensions(formatDescription) as? [String: Any] ?? [:]
        let transfer = (ext[kCVImageBufferTransferFunctionKey as String] as? String) ?? "unknown"
        let depth = transfer.contains("2084") || transfer.contains("HLG") ? 10 : 8
        var channels = 0
        if let audio = audioTracks.first,
           let desc = try await audio.load(.formatDescriptions).first,
           let basic = CMAudioFormatDescriptionGetStreamBasicDescription(desc) {
            channels = Int(basic.pointee.mChannelsPerFrame)
        }
        nominalFrameRate = Double(try await video.load(.nominalFrameRate))
        guard nominalFrameRate > 0 else { throw BackendError.noOutputDescription }
        loadedOutput = OutputInfo(width: Int(dimensions.width), height: Int(dimensions.height),
                                  bitDepth: depth, colorTransfer: transfer, audioChannels: channels)
    }

    /// AVFoundation throws a plain NSError, not a typed BackendError, when it
    /// recognizes a container but has no decoder for the codec inside it:
    /// verified live against this repo's own av1-10bit.mkv fixture,
    /// AVFoundationErrorDomain code -11828, the named AVError.Code
    /// .fileFormatNotRecognized (confirmed against AVError.h). That is
    /// AVPlayer correctly and deterministically refusing a format it was
    /// never going to support (it has no AV1 decoder at all), not a crash;
    /// see BackendError.unsupportedFormat for why that distinction matters
    /// downstream.
    ///
    /// Matched on the numeric code, not on localizedFailureReason text: an
    /// earlier version of this classifier looked for "not supported" in
    /// that string, which is what NSError localizes for the process's
    /// current locale. On a German-locale machine it reads "Das
    /// Medienformat wird nicht unterstuetzt." instead, the substring match
    /// misses it, and the raw NSError falls through unclassified, exactly
    /// the failure this classifier exists to prevent (reproduced live, not
    /// theoretical). The numeric code is stable across locales. decoderNotFound
    /// and formatUnsupported are included alongside fileFormatNotRecognized
    /// since both are also named specifically for a missing codec/format
    /// capability and could plausibly be what a different OS release reports
    /// for the same AV1-in-MKV gap; genuine decode-failure codes
    /// (decodeFailed, invalidSourceMedia, undecodableMediaData, ...) are
    /// deliberately not in this set, since those mean a format AVPlayer does
    /// support failed to decode, a real malfunction, not a capability gap.
    /// Any other thrown error is a genuine, unclassified failure and passes
    /// through unchanged, still a crash as far as this binary is concerned.
    private static let unsupportedFormatCodes: Set<Int> = [
        AVError.Code.fileFormatNotRecognized.rawValue,
        AVError.Code.decoderNotFound.rawValue,
        AVError.Code.formatUnsupported.rawValue,
    ]

    private static func classify(_ error: Error) -> Error {
        let nsError = error as NSError
        guard nsError.domain == AVFoundationErrorDomain,
              unsupportedFormatCodes.contains(nsError.code)
        else { return error }
        return BackendError.unsupportedFormat(nsError.localizedFailureReason ?? nsError.localizedDescription)
    }

    func play() { player.play() }
    func stop() { player.pause(); player.replaceCurrentItem(with: nil) }

    var droppedFrames: Int {
        Int(item?.accessLog()?.events.last?.numberOfDroppedVideoFrames ?? 0)
    }

    private(set) var nominalFrameRate: Double = 0

    /// AVPlayer exposes no presented-frame counter, so this is a proxy: elapsed
    /// playback time times the nominal rate, minus the drops AVFoundation admits
    /// to. It cannot see frames lost below AVFoundation, so it is optimistic by
    /// construction. It is used only for the validity gate (did this session play
    /// roughly the expected amount), never as a quality number against engines
    /// that count real frames.
    var deliveredFrames: Int {
        guard let item, nominalFrameRate > 0 else { return 0 }
        return max(0, Int(item.currentTime().seconds * nominalFrameRate) - droppedFrames)
    }
}
