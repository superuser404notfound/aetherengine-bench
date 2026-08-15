#!/usr/bin/env bash
# Full benchmark session. Keeps the machine awake and the conditions fixed.
# Any arguments are forwarded to orchestrate.py, e.g. for a shortened dry
# run: sudo Scripts/run-bench.sh --dry-run --settle 2 --measure 5 --cooldown 2
#      --repeats 1 --fixtures h264-1080p.mp4
set -euo pipefail
cd "$(dirname "$0")/.."
[ "$(id -u)" -eq 0 ] || { echo "run: sudo Scripts/run-bench.sh"; exit 1; }
for scheme in AetherBench AVBench VLCBench; do
  xcodebuild -project AetherBench.xcodeproj -scheme "$scheme" \
    -configuration Release -derivedDataPath .build build
done
xcodebuild -project KSBench.xcodeproj -scheme KSBench \
  -configuration Release -derivedDataPath .build-ks build
caffeinate -dimsu python3 Scripts/orchestrate.py "$@"
