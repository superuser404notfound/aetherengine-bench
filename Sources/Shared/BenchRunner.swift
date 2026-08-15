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
            backend.play()
            // Settle first, then open the measurement window. The sampler runs
            // against exactly this window, so startedAt/endedAt must bound the
            // measured seconds only, never the settling ones.
            try await Task.sleep(for: .seconds(arguments.settle))
            let started = Date()
            let framesAtStart = backend.deliveredFrames
            let dropsAtStart = backend.droppedFrames
            try await Task.sleep(for: .seconds(arguments.measure))
            let ended = Date()
            guard let output = backend.output else { throw BackendError.noOutputDescription }
            let report = BenchReport(
                backend: arguments.backend.rawValue,
                engineVersion: type(of: backend).engineVersion,
                fixture: arguments.url.lastPathComponent,
                deliveredFrames: max(0, backend.deliveredFrames - framesAtStart),
                droppedFrames: max(0, backend.droppedFrames - dropsAtStart),
                expectedFrames: Int(arguments.measure * backend.nominalFrameRate),
                output: output,
                startedAt: started, endedAt: ended)
            try JSONEncoder.bench.encode(report).write(to: arguments.reportURL)
            backend.stop()
        } catch {
            FileHandle.standardError.write("bench failed: \(error)\n".data(using: .utf8)!)
            exit(1)
        }
    }
}
