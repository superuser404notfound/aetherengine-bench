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
            // -1 means "not reported by this engine/path" (see
            // KSPlayerBackend.droppedFrames), not zero. A plain subtraction
            // would turn that sentinel into a fabricated delta (e.g.
            // -1 - -1 = 0, clamped to 0 by max(0, ...)), which reads exactly
            // like a real zero-drops measurement. If either endpoint is
            // unreported, the whole window is unreported.
            let droppedFrames = (backend.droppedFrames < 0 || dropsAtStart < 0)
                ? -1 : max(0, backend.droppedFrames - dropsAtStart)
            let report = BenchReport(
                backend: arguments.backend.rawValue,
                engineVersion: type(of: backend).engineVersion,
                fixture: arguments.url.lastPathComponent,
                deliveredFrames: max(0, backend.deliveredFrames - framesAtStart),
                droppedFrames: droppedFrames,
                expectedFrames: Int(arguments.measure * backend.nominalFrameRate),
                output: output,
                startedAt: started, endedAt: ended,
                servingPath: backend.servingPath)
            try JSONEncoder.bench.encode(report).write(to: arguments.reportURL)
            backend.stop()
        } catch {
            FileHandle.standardError.write("bench failed: \(error)\n".data(using: .utf8)!)
            exit(1)
        }
    }
}
