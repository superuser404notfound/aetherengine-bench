#!/usr/bin/env bash
# Runs mpv (the CLI, which is libmpv with its own renderer, the way anyone
# actually consumes it) against a fixture and reports frame/output stats
# through the JSON IPC socket, in the same shape BenchReport.swift defines
# so the in-process backends and mpv can be read through one schema.
#
# Timing and delta semantics mirror Sources/Shared/BenchRunner.swift: settle,
# then stamp startedAt and snapshot counters, then measure, then stamp
# endedAt and snapshot counters again, reporting deltas over the measurement
# window only, then linger (see LINGER below) before mpv is allowed to exit.
set -euo pipefail

# Same value, same name, as Scripts/orchestrate.py's DEFAULT_LINGER_SECONDS
# and Sources/Shared/BenchArguments.swift's --linger default (there, 0; the
# Swift binaries are only ever run standalone through tests or a direct
# manual invocation, so their default stays a no-op). This script has no
# such test suite depending on an instant exit, and orchestrate.py's own
# launch() always passes --linger's shell equivalent explicitly (see below),
# so defaulting this one to the real protective value closes the same edge
# for any direct/manual invocation too, not just orchestrated ones. Nothing
# short of both files agreeing by convention can keep two literals in two
# different languages from drifting apart, so both are named identically
# and each points at the other; a mismatch here is a bug in this comment,
# not a config the code will discover for you.
DEFAULT_LINGER_SECONDS=5

URL="${1:?usage: run-mpv.sh <file> [settle=15] [measure=60] [report=/tmp/mpv.json] [linger=$DEFAULT_LINGER_SECONDS]}"
SETTLE="${2:-15}"
MEASURE="${3:-60}"
REPORT="${4:-/tmp/mpv.json}"
LINGER="${5:-$DEFAULT_LINGER_SECONDS}"
[ -f "$URL" ] || { echo "run-mpv.sh: no such file: $URL" >&2; exit 1; }

# Fixed window on a fixed display, matching BenchWindow.swift's default
# 1920x1080 on display index 0. Window size and display move GPU load more
# than any codec difference, so both are pinned the same way for mpv.
WINDOW_W=1920
WINDOW_H=1080

SOCK="$(mktemp -u "${TMPDIR:-/tmp}/mpv-bench-XXXXXX").sock"
LOG="$(mktemp -u "${TMPDIR:-/tmp}/mpv-bench-XXXXXX").log"
rm -f "$SOCK"

MPV_PID=""
cleanup() {
  if [ -n "$MPV_PID" ] && kill -0 "$MPV_PID" 2>/dev/null; then
    kill "$MPV_PID" 2>/dev/null || true
    wait "$MPV_PID" 2>/dev/null || true
  fi
  rm -f "$SOCK" "$LOG"
}
trap cleanup EXIT

mpv \
  --input-ipc-server="$SOCK" \
  --no-config \
  --geometry="${WINDOW_W}x${WINDOW_H}+0+0" --autofit="${WINDOW_W}x${WINDOW_H}" --screen=0 \
  --no-border --keep-open=no --pause=no \
  --osc=no --no-terminal --really-quiet \
  --sid=no \
  --hwdec=auto-safe \
  "$URL" >"$LOG" 2>&1 &
MPV_PID=$!

# Wait for the IPC socket to exist, bounded: a benchmark that hangs on a
# broken launch instead of failing is worse than one that fails loudly.
deadline=$((SECONDS + 10))
while [ ! -S "$SOCK" ]; do
  if ! kill -0 "$MPV_PID" 2>/dev/null; then
    echo "run-mpv.sh: mpv exited before opening its IPC socket, see $LOG" >&2
    exit 1
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "run-mpv.sh: timed out waiting for mpv's IPC socket at $SOCK" >&2
    exit 1
  fi
  sleep 0.1
done

# Reads one property over IPC. `nc -U` is fragile on its own: a response can
# be preceded by an unrelated event line, and a socket read that comes back
# empty must never be silently read as "0". This filters for the first
# genuine command reply (the "error" key is only ever present on command
# replies, never on events) and prints nothing if the property truly did not
# resolve, so callers can tell "unresolved" apart from a real zero.
ask() {
  printf '{ "command": ["get_property", "%s"] }\n' "$1" \
    | nc -U -w 2 "$SOCK" 2>/dev/null \
    | python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "error" not in obj:
        continue  # an event notification, not the reply to our query
    if obj["error"] == "success" and "data" in obj:
        print(obj["data"])
    break
'
}

# Fails the whole script the moment a required property comes back empty,
# in the current shell (not a subshell), so it cannot be swallowed by a
# command-substitution assignment the way `exit` inside ask() itself would be.
require() {
  if [ -z "$1" ]; then
    echo "run-mpv.sh: mpv property '$2' did not resolve over IPC (empty or unavailable), refusing to report a fabricated 0" >&2
    exit 1
  fi
}

# vo-configured is mpv's own signal that a frame has actually been handed to
# the video output, i.e. that load succeeded and playback truly started, the
# same gate `backend.load()` throwing serves in the Swift backends.
deadline=$((SECONDS + 10))
while :; do
  configured=$(ask vo-configured)
  [ "$configured" = "True" ] && break
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "run-mpv.sh: mpv never reported vo-configured, see $LOG" >&2
    exit 1
  fi
  sleep 0.1
done

# The window must match, not just be asked for: read back what mpv actually
# rendered into rather than trusting --geometry/--autofit did the right thing.
OSD_W=$(ask osd-width); require "$OSD_W" osd-width
OSD_H=$(ask osd-height); require "$OSD_H" osd-height
if [ "$OSD_W" != "$WINDOW_W" ] || [ "$OSD_H" != "$WINDOW_H" ]; then
  echo "run-mpv.sh: mpv's window is ${OSD_W}x${OSD_H}, expected ${WINDOW_W}x${WINDOW_H}" >&2
  exit 1
fi

# Static output metadata is read once here, before the measurement window
# opens, mirroring the Swift backends' `load()` populating `OutputInfo` once
# up front rather than at settle-end. This is not just tidier: reading it
# here means the STARTED..ENDED bracket below contains only the two counter
# reads on both sides. Reading it after the settle sleep instead (as an
# earlier version of this script did) meant six IPC round trips landed
# between STARTED and the PT_START/DROPS_START snapshot while ENDED was
# followed immediately by PT_END/DROPS_END with none, an asymmetry measured
# at roughly 195ms vs 77ms that quietly biased deliveredFrames down by a
# few frames over a 15s window, a distortion none of the four in-process
# backends have since they read their counters as instant in-process calls.
WIDTH=$(ask width); require "$WIDTH" width
HEIGHT=$(ask height); require "$HEIGHT" height
FPS=$(ask container-fps); require "$FPS" container-fps
HWDEC_CURRENT=$(ask hwdec-current); require "$HWDEC_CURRENT" hwdec-current
PIXFMT=$(ask "video-params/pixelformat"); require "$PIXFMT" "video-params/pixelformat"
# Legitimately absent on the software path (no require): only populated once
# a hardware decoder has put the frame in an opaque hardware-surface format.
# See the bit-depth comment in the Python block below for why this matters.
HW_PIXFMT=$(ask "video-params/hw-pixelformat")
GAMMA=$(ask "video-params/gamma"); require "$GAMMA" "video-params/gamma"
# Audio channel count is the one property allowed to legitimately come back
# unavailable: a video with no audio track is a real state, not an IPC
# failure, and AVPlayerBackend reports 0 for the same case.
CHANNELS=$(ask "audio-params/channel-count"); CHANNELS="${CHANNELS:-0}"
VERSION=$(mpv --version | head -1)

# Settle first, exactly like BenchRunner: startedAt/endedAt must bound the
# measured seconds only, never the settling ones.
sleep "$SETTLE"
STARTED=$(date -u +%s)

# mpv exposes no presented-frame counter (checked --list-properties: no
# vo-passed-frame-count or equivalent exists in 0.41.0; estimated-frame-number
# is documented as "only an estimate" computed from the same two quantities
# used below). frame-drop-count IS a real counter though (frames the VO
# actually dropped under the default --framedrop=vo), so it is used as-is,
# not derived. playback-time is real media position (accounts for stalls,
# unlike wall-clock elapsed time), giving the same kind of proxy
# AVPlayerBackend and AetherBackend's native path already use for
# deliveredFrames: fps * elapsed media time, minus real drops, clamped to
# zero at each endpoint before taking the delta, exactly mirroring
# BenchRunner's arithmetic.
PT_START=$(ask playback-time); require "$PT_START" playback-time
DROPS_START=$(ask frame-drop-count); require "$DROPS_START" frame-drop-count

sleep "$MEASURE"
ENDED=$(date -u +%s)

PT_END=$(ask playback-time); require "$PT_END" playback-time
DROPS_END=$(ask frame-drop-count); require "$DROPS_END" frame-drop-count

python3 - "$REPORT" "$WIDTH" "$HEIGHT" "$FPS" "$HWDEC_CURRENT" "$PIXFMT" "$HW_PIXFMT" \
         "$GAMMA" "$CHANNELS" "$PT_START" "$DROPS_START" "$PT_END" "$DROPS_END" "$MEASURE" \
         "$STARTED" "$ENDED" "$(basename "$URL")" "$VERSION" <<'PY'
import json, re, sys, time

(report, w, h, fps, hwdec_current, pixfmt, hw_pixfmt, gamma, channels,
 pt_start, drops_start, pt_end, drops_end, measure,
 started, ended, fixture, version) = sys.argv[1:19]

fps = float(fps)
measure = float(measure)
pt_start, pt_end = float(pt_start), float(pt_end)
drops_start, drops_end = int(drops_start), int(drops_end)

# Same clamp-then-delta arithmetic as BenchRunner.swift: each endpoint's
# delivered-frame estimate is clamped to zero before the window delta is
# taken, so a session that has not produced anything yet never contributes
# a phantom negative.
def delivered_at(playback_time, drops):
    return max(0, int(playback_time * fps) - drops)

delivered = max(0, delivered_at(pt_end, drops_end) - delivered_at(pt_start, drops_start))
dropped = max(0, drops_end - drops_start)
expected = int(measure * fps)

# Bit depth. Under hardware decode (--hwdec=auto-safe), mpv leaves the frame
# in an opaque hardware-surface object and `video-params/pixelformat` reports
# the hwdec API's own name ("videotoolbox" on macOS), not an FFmpeg pixel
# format; the real underlying surface format lives one property over, in
# `video-params/hw-pixelformat` (e.g. "p010" for 10-bit HDR10/DV content,
# "nv12" for 8-bit). Verified live: hevc-4k-hdr10.mp4 and dv-p81.mp4 both
# report pixelformat=videotoolbox while hw-pixelformat correctly says p010;
# parsing pixelformat alone in that case silently produced bitDepth=8 for
# genuinely 10-bit sources, a false cross-engine difference at the validity
# gate (AetherEngine reports 10 for the same fixtures). hw-pixelformat is
# only populated once hardware decode actually put a frame on a surface, so
# it is preferred whenever it resolved; on the software path (no hardware
# decode, hw-pixelformat legitimately never resolves) pixelformat itself is
# already the real FFmpeg name and is used directly.
#
# FFmpeg pixel-format naming puts an explicit bit-depth digit run after the
# "p" only for >8-bit formats (yuv420p10le, p010, yuv444p16le); its absence
# means 8-bit (yuv420p, nv12), a real determination, not a guess. If neither
# property ever yielded more than the opaque hwdec-API name (the value to
# parse still equals hwdec-current itself), there is nothing real to parse:
# report the cross-backend "not reported" sentinel (0, the same one
# VLCKitBackend uses for its own unreported bitDepth) instead of defaulting
# to 8. Whichever value is used, it stays informational only, like
# AVPlayerBackend's and AetherBackend's colorTransfer/bitDepth: not meant to
# be compared byte for byte against another backend's vocabulary.
effective_pixfmt = hw_pixfmt if hw_pixfmt else pixfmt
if effective_pixfmt == hwdec_current:
    bit_depth = 0
else:
    m = re.search(r'p(\d+)(?:le|be)?$', effective_pixfmt)
    bit_depth = int(m.group(1)) if m else 8

stamp = lambda secs: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(secs)))

report_obj = {
    "backend": "mpv",
    "engineVersion": version,
    "fixture": fixture,
    "deliveredFrames": delivered,
    "droppedFrames": dropped,
    "expectedFrames": expected,
    "output": {
        "width": int(w),
        "height": int(h),
        "bitDepth": bit_depth,
        "colorTransfer": gamma,
        "audioChannels": int(channels),
    },
    "startedAt": stamp(started),
    "endedAt": stamp(ended),
    # servingPath is omitted, not written as null: mpv has exactly one
    # playback path, unlike the backends that pick between more than one.
}

with open(report, "w") as f:
    json.dump(report_obj, f, indent=2, sort_keys=True)
    f.write("\n")
PY

echo "run-mpv.sh: wrote $REPORT"

# STARTED/ENDED and the report are already fixed above; lingering here
# cannot change what was measured or reported. It exists purely so this
# script (and mpv, backgrounded under it) is still alive when the
# orchestrator's sampler takes its last sample: the sampler's own window
# starts and ends slightly later than this script's (process spawn, sudo -u
# privilege drop, the sampler's first `ps` call all cost time this side
# does not pay), and without margin the sampler's last sample can land
# after mpv has already exited, discarding an otherwise-clean run (the
# same defect Sources/Shared/BenchRunner.swift's own --linger exists for).
# The orchestrator terminates this script the moment its own sampling
# window closes (see orchestrate.py's TERMINATE_GRACE_SECONDS) rather than
# waiting this sleep out, so it costs no wall-clock time in the common
# case; the `cleanup` EXIT trap above fires on that termination exactly as
# it does on a normal, un-terminated exit, killing mpv either way.
sleep "$LINGER"
