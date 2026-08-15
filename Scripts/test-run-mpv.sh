#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

check_common() { # report-path
  python3 - "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))

assert d["backend"] == "mpv", d
assert d["engineVersion"].startswith("mpv"), d
assert "servingPath" not in d, d

assert d["expectedFrames"] > 0, d
assert d["deliveredFrames"] > 0, d
assert d["deliveredFrames"] <= d["expectedFrames"] + 24, d  # a second or so of slack, not a runaway count
assert d["droppedFrames"] >= 0, d

assert d["startedAt"].endswith("Z"), d
assert d["endedAt"].endswith("Z"), d
from datetime import datetime
started = datetime.strptime(d["startedAt"], "%Y-%m-%dT%H:%M:%SZ")
ended = datetime.strptime(d["endedAt"], "%Y-%m-%dT%H:%M:%SZ")
assert ended > started, d
PY
}

# h264-1080p.mp4: hardware-decoded (VideoToolbox) but genuinely 8-bit, so
# video-params/pixelformat resolves to the opaque "videotoolbox" placeholder
# while hw-pixelformat says "nv12". Exercises the hw-pixelformat fallback
# path on the 8-bit side.
REPORT_H264="$(mktemp -u "${TMPDIR:-/tmp}/mpv-bench-test-XXXXXX").json"
Scripts/run-mpv.sh "$PWD/Fixtures/h264-1080p.mp4" 2 5 "$REPORT_H264"
check_common "$REPORT_H264"
python3 - "$REPORT_H264" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["fixture"] == "h264-1080p.mp4", d
assert d["output"]["width"] == 1920, d
assert d["output"]["height"] == 858, d
assert d["output"]["audioChannels"] == 2, d
assert d["output"]["bitDepth"] == 8, d
assert d["expectedFrames"] == 120, d  # 5s measure * 24fps container-fps
print("ok: h264-1080p.mp4 (hardware, 8-bit)")
PY
rm -f "$REPORT_H264"

# hevc-4k-hdr10.mp4: hardware-decoded AND genuinely 10-bit. This is the
# fixture that exposed the bug where video-params/pixelformat alone reports
# "videotoolbox" (not an FFmpeg pixel format) under hardware decode, which
# silently fell through to a default of 8. Asserting bitDepth == 10 here is
# what actually exercises the video-params/hw-pixelformat ("p010") path.
REPORT_HDR="$(mktemp -u "${TMPDIR:-/tmp}/mpv-bench-test-XXXXXX").json"
Scripts/run-mpv.sh "$PWD/Fixtures/hevc-4k-hdr10.mp4" 2 5 "$REPORT_HDR"
check_common "$REPORT_HDR"
python3 - "$REPORT_HDR" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["fixture"] == "hevc-4k-hdr10.mp4", d
assert d["output"]["width"] == 3840, d
assert d["output"]["bitDepth"] == 10, d  # the regression this test guards against
assert d["output"]["colorTransfer"] == "pq", d
assert d["expectedFrames"] == 120, d  # 5s measure * 24fps container-fps
print("ok: hevc-4k-hdr10.mp4 (hardware, 10-bit)")
PY
rm -f "$REPORT_HDR"

echo "ok"
