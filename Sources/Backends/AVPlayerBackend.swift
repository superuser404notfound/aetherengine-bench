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

        let videoTracks = try await asset.loadTracks(withMediaType: .video)
        let audioTracks = try await asset.loadTracks(withMediaType: .audio)
        guard let video = videoTracks.first else { return }
        let descriptions = try await video.load(.formatDescriptions)
        guard let formatDescription = descriptions.first else { return }
        // Coded pixel dimensions, not `naturalSize`. `naturalSize` folds in the
        // pixel aspect ratio, so a fixture whose SAR compensates for an
        // odd-to-even height rounding (858:857 here) reports a display width a
        // couple of pixels off the decoder's actual output. The FFmpeg-backed
        // engines this benchmark also drives report coded dimensions, so this
        // keeps `OutputInfo.width/height` comparable across backends.
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
        loadedOutput = OutputInfo(width: Int(dimensions.width), height: Int(dimensions.height),
                                  bitDepth: depth, colorTransfer: transfer, audioChannels: channels)
    }

    func play() { player.play() }
    func stop() { player.pause(); player.replaceCurrentItem(with: nil) }

    var droppedFrames: Int {
        Int(item?.accessLog()?.events.last?.numberOfDroppedVideoFrames ?? 0)
    }

    /// AVPlayer exposes no presented-frame counter, so delivered frames are
    /// derived from elapsed playback time and the track's nominal frame rate.
    var deliveredFrames: Int {
        guard let item, let track = item.tracks.first(where: { $0.assetTrack?.mediaType == .video }),
              let fps = track.assetTrack.map({ Double($0.nominalFrameRate) }), fps > 0 else { return 0 }
        return Int(item.currentTime().seconds * fps) - droppedFrames
    }
}
