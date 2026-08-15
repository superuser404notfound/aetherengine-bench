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
    let deliveredFrames: Int
    let droppedFrames: Int
    let expectedFrames: Int
    let output: OutputInfo
    let startedAt: Date
    let endedAt: Date
}
