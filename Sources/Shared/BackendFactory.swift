import AppKit

/// Each binary compiles exactly one backend, because two engines vendoring their
/// own FFmpeg cannot share a process without one of them serving the other's
/// symbols. The flag stays uniform so the orchestrator's command line does not
/// have to know which binary carries what.
@MainActor
func makeBackend(for kind: BackendKind) throws -> BenchBackend {
    #if BACKEND_AETHER
    guard kind == .aether else { throw BackendError.wrongBinary(asked: kind) }
    return try AetherBackend()
    #elseif BACKEND_AVPLAYER
    guard kind == .avplayer else { throw BackendError.wrongBinary(asked: kind) }
    return AVPlayerBackend()
    #elseif BACKEND_VLCKIT
    guard kind == .vlckit else { throw BackendError.wrongBinary(asked: kind) }
    return VLCKitBackend()
    #elseif BACKEND_KSPLAYER
    guard kind == .ksplayer else { throw BackendError.wrongBinary(asked: kind) }
    return KSPlayerBackend()
    #else
    throw BackendError.wrongBinary(asked: kind)
    #endif
}
