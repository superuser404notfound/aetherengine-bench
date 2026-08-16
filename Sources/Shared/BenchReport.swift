import Foundation

struct OutputInfo: Codable, Equatable {
    let width: Int
    let height: Int
    let bitDepth: Int
    let colorTransfer: String
    let audioChannels: Int
}

struct BenchReport: Codable {
    let backend: String
    let engineVersion: String
    let fixture: String
    /// The surface the engine actually rendered into, in pixels. GPU cost scales
    /// with pixels, and a size requested in points is not the pixel count on a
    /// Retina display, so this is measured rather than assumed and published
    /// alongside the numbers it explains. nil only for a writer that cannot
    /// determine it.
    let renderPixels: String?
    let deliveredFrames: Int
    let droppedFrames: Int
    let expectedFrames: Int
    let output: OutputInfo
    let startedAt: Date
    let endedAt: Date
    /// Which concrete engine/path served this session, for backends that
    /// pick between more than one (see `BenchBackend.servingPath`). nil for
    /// backends with a single playback path.
    let servingPath: String?
}

/// This JSON is written by Swift and by a shell script, and read by Python, so
/// the date format cannot be left to a default. Foundation's default encodes a
/// bare seconds-since-2001 double, which neither of the other two would produce
/// or recognise. ISO 8601 is the one format all three speak.
extension JSONEncoder {
    static var bench: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }
}

extension JSONDecoder {
    static var bench: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}
