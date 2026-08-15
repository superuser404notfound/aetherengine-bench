#!/usr/bin/env bash
# Asserts every fixture is actually the thing the benchmark claims it is.
set -uo pipefail
cd "$(dirname "$0")/../Fixtures"
fail=0
check() { # file, ffprobe query, expected substring
  local got
  got=$(ffprobe -v error -select_streams "$2" -show_entries "$3" -of csv=p=0 "$1" 2>/dev/null | head -1)
  if [[ "$got" != *"$4"* ]]; then echo "FAIL $1: $3 = '$got', expected '$4'"; fail=1
  else echo "ok   $1: $4"; fi
}
# A run plays 15 s settle plus 60 s measure. A fixture shorter than that ends
# mid-measurement and every engine then reports a partial window, which reads
# like an efficiency win. Duration is checked first, for every fixture.
check_duration() { # file
  local got
  got=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null | cut -d. -f1)
  if [ -z "$got" ] || [ "$got" -lt 100 ]; then
    echo "FAIL $1: duration ${got:-none} s, need at least 100 s"; fail=1
  else echo "ok   $1: ${got} s"; fi
}
for f in h264-1080p.mp4 hevc-4k-hdr10.mp4 av1-10bit.mkv vp9.webm hevc-subs.mkv eac3-51.mp4 dv-p81.mp4; do
  check_duration "$f"
done
check h264-1080p.mp4    v:0 stream=codec_name          h264
check h264-1080p.mp4    v:0 stream=width               1920
check hevc-4k-hdr10.mp4 v:0 stream=codec_name          hevc
check hevc-4k-hdr10.mp4 v:0 stream=width               3840
check hevc-4k-hdr10.mp4 v:0 stream=pix_fmt             yuv420p10le
check hevc-4k-hdr10.mp4 v:0 stream=color_transfer      smpte2084
check av1-10bit.mkv     v:0 stream=codec_name          av1
check av1-10bit.mkv     v:0 stream=pix_fmt             yuv420p10le
check vp9.webm          v:0 stream=codec_name          vp9
check hevc-subs.mkv     s:0 stream=codec_name          subrip
check hevc-subs.mkv     s:1 stream=codec_name          ass
check eac3-51.mp4       a:0 stream=codec_name          eac3
check eac3-51.mp4       a:0 stream=channels            6
check dv-p81.mp4        v:0 stream=codec_tag_string    dvh1
exit $fail
