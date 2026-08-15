import AppKit

let arguments = try BenchArguments.parse(Array(CommandLine.arguments.dropFirst()))
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let runner = BenchRunner(arguments: arguments)
Task { @MainActor in await runner.run(); app.terminate(nil) }
app.run()
