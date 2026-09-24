#!/usr/bin/env bash
# Full benchmark session. Keeps the machine awake and the conditions fixed.
# Any arguments are forwarded to orchestrate.py, e.g. for a shortened dry
# run: sudo Scripts/run-bench.sh --dry-run --settle 2 --measure 5 --cooldown 2
#      --repeats 1 --fixtures h264-1080p.mp4
set -euo pipefail
cd "$(dirname "$0")/.."
# Root, or the powermetrics NOPASSWD grant (README step 2): with the grant the
# session runs as the invoking user and needs no interactive root shell.
[ "$(id -u)" -eq 0 ] || sudo -n powermetrics --samplers thermal -n 1 -i 100 >/dev/null 2>&1 \
  || { echo "run: sudo Scripts/run-bench.sh, or install the powermetrics grant (README step 2)"; exit 1; }
# One scheme for all three tools, not three builds: see AllBench in project.yml.
xcodebuild -project AetherBench.xcodeproj -scheme AllBench \
  -configuration Release -derivedDataPath .build build
xcodebuild -project KSBench.xcodeproj -scheme KSBench \
  -configuration Release -derivedDataPath .build-ks build

# Four back-to-back xcodebuild invocations leave the machine well above
# idle (measured live: 530.5 mW CPU right after, against this machine's
# genuine ~150 mW floor). take_clean_baseline's own magnitude/stability
# retry loop is the real backstop and keeps retrying regardless, but
# starting it the instant the build finishes wastes its first attempts on
# a machine that is obviously still busy, and 15 records get subtracted
# against whatever it settles on. This just gives it a head start.
QUIET_PERIOD_SECONDS=120
echo "run-bench.sh: build finished, cooling down ${QUIET_PERIOD_SECONDS}s before the first idle baseline"
sleep "$QUIET_PERIOD_SECONDS"

caffeinate -dimsu python3 Scripts/orchestrate.py "$@"
