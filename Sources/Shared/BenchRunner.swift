import AppKit

@MainActor
final class BenchRunner {
    private let arguments: BenchArguments
    init(arguments: BenchArguments) { self.arguments = arguments }

    func run() async {
        let backend: BenchBackend
        do { backend = try makeBackend(for: arguments.backend) } catch {
            FileHandle.standardError.write(
                "this binary does not carry backend \(arguments.backend.rawValue)\n".data(using: .utf8)!)
            exit(2)
        }
        _ = BenchWindow.make(size: arguments.windowSize, displayIndex: arguments.displayIndex, hosting: backend.view)
        do {
            try await backend.load(arguments.url)
            let started = Date()
            backend.play()
            try await Task.sleep(for: .seconds(arguments.settle + arguments.measure))
            let report = BenchReport(
                backend: arguments.backend.rawValue,
                engineVersion: type(of: backend).engineVersion,
                fixture: arguments.url.lastPathComponent,
                deliveredFrames: backend.deliveredFrames,
                droppedFrames: backend.droppedFrames,
                expectedFrames: 0,
                output: backend.output ?? OutputInfo(width: 0, height: 0, bitDepth: 0, colorTransfer: "none", audioChannels: 0),
                startedAt: started, endedAt: Date())
            try JSONEncoder.bench.encode(report).write(to: arguments.reportURL)
            backend.stop()
        } catch {
            FileHandle.standardError.write("bench failed: \(error)\n".data(using: .utf8)!)
            exit(1)
        }
    }
}
