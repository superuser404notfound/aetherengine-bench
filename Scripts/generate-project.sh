#!/usr/bin/env bash
# Two projects on purpose: KSPlayer's FFmpegKit and AetherEngine's FFmpegBuild
# declare binary targets with identical names, and SwiftPM resolves per project.
set -euo pipefail
cd "$(dirname "$0")/.."
xcodegen generate --spec project.yml
xcodegen generate --spec project-ksplayer.yml
xcodebuild -project AetherBench.xcodeproj -resolvePackageDependencies
xcodebuild -project KSBench.xcodeproj -resolvePackageDependencies
