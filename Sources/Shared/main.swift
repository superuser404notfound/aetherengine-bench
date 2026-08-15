import Foundation

// Placeholder entry point. Task 2 replaces this with the real harness
// (argument parsing, the shared --backend flag, playback + measurement).
// Each target compiles this same file under its own backend's active
// compilation condition, set per target in project.yml / project-ksplayer.yml.

#if BACKEND_AETHER
let backendName = "aether"
#elseif BACKEND_AVPLAYER
let backendName = "avplayer"
#elseif BACKEND_VLCKIT
let backendName = "vlckit"
#elseif BACKEND_KSPLAYER
let backendName = "ksplayer"
#else
#error("No BACKEND_* compilation condition set for this target")
#endif

print("AetherBench placeholder, backend: \(backendName)")
