import AppKit

/// Exit codes BenchRunner can produce beyond the default 0 for success.
/// Mirrored by orchestrate.py's REFUSAL_EXIT_CODE, which is the only one
/// of these three the orchestrator treats specially: a refusal is
/// recorded once with its reason and not retried, everything else
/// (including wrongBinary, which should never happen given how
/// orchestrate.py picks a binary per backend) is a crash and keeps the
/// existing retry-and-count behavior.
enum BenchExitCode {
    static let crashed: Int32 = 1
    static let wrongBinary: Int32 = 2
    static let refused: Int32 = 3
}

@MainActor
final class BenchRunner {
    private let arguments: BenchArguments
    init(arguments: BenchArguments) { self.arguments = arguments }

    func run() async {
        let backend: BenchBackend
        do { backend = try makeBackend(for: arguments.backend) } catch {
            FileHandle.standardError.write(
                "this binary does not carry backend \(arguments.backend.rawValue)\n".data(using: .utf8)!)
            exit(BenchExitCode.wrongBinary)
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
            // Report is written and started/endedAt are already fixed above;
            // lingering here cannot change what was measured or reported. It
            // exists purely so this process is still alive when the
            // orchestrator's sampler (whose own window starts and ends a
            // little later than this one, see BenchArguments.linger) takes
            // its last sample.
            if arguments.linger > 0 {
                try await Task.sleep(for: .seconds(arguments.linger))
            }
            backend.stop()
        } catch let error as BackendError {
            if case .unsupportedFormat(let reason) = error {
                // A deterministic capability gap, not a malfunction: distinct
                // exit code so orchestrate.py can record it once, with this
                // reason, instead of burning max_launch_attempts on an
                // outcome that was never going to change and instead of
                // publishing it as a crash, a false statement about the engine.
                FileHandle.standardError.write("bench refused: \(reason)\n".data(using: .utf8)!)
                exit(BenchExitCode.refused)
            }
            FileHandle.standardError.write("bench failed: \(error)\n".data(using: .utf8)!)
            exit(BenchExitCode.crashed)
        } catch {
            FileHandle.standardError.write("bench failed: \(error)\n".data(using: .utf8)!)
            exit(BenchExitCode.crashed)
        }
    }
}
