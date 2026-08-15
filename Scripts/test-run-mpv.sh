#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

REPORT="$(mktemp -u "${TMPDIR:-/tmp}/mpv-bench-test-XXXXXX").json"
Scripts/run-mpv.sh "$PWD/Fixtures/h264-1080p.mp4" 2 5 "$REPORT"

python3 - "$REPORT" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))

assert d["backend"] == "mpv", d
assert d["engineVersion"].startswith("mpv"), d
assert d["fixture"] == "h264-1080p.mp4", d
assert "servingPath" not in d, d

assert d["output"]["width"] == 1920, d
assert d["output"]["height"] == 858, d
assert d["output"]["audioChannels"] == 2, d

assert d["expectedFrames"] == 120, d  # 5s measure * 24fps container-fps
assert d["deliveredFrames"] > 0, d
assert d["deliveredFrames"] <= d["expectedFrames"] + 24, d  # a frame or two of slack, not a runaway count
assert d["droppedFrames"] >= 0, d

assert d["startedAt"].endswith("Z"), d
assert d["endedAt"].endswith("Z"), d
from datetime import datetime
started = datetime.strptime(d["startedAt"], "%Y-%m-%dT%H:%M:%SZ")
ended = datetime.strptime(d["endedAt"], "%Y-%m-%dT%H:%M:%SZ")
assert ended > started, d

print("ok")
PY

rm -f "$REPORT"
