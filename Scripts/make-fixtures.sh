#!/usr/bin/env bash
# Builds every benchmark fixture from one CC-BY master (Tears of Steel, Blender Foundation).
# Master is 6.4 GB and downloaded once.
# The cut is 120 s starting at 4:00. Two constraints fix it there: the master is
# only 734 s long, so a late start silently produces a stub, and a run needs
# 15 s settle plus 60 s measure, so anything under 90 s ends mid-measurement.
set -euo pipefail
# Fixtures/ is gitignored (media is rebuilt, never committed), so on a fresh
# clone the directory does not exist yet. Create it before entering it.
mkdir -p "$(dirname "$0")/../Fixtures/master"
cd "$(dirname "$0")/../Fixtures"

BASE="https://download.blender.org/demo/movies/ToS"
CUT="-ss 240 -t 120"

if [ ! -f master/tearsofsteel_4k.mov ]; then
  curl -L -o master/tos4k.zip "$BASE/tearsofsteel_4k.mov.zip"
  unzip -o master/tos4k.zip -d master/
  mv master/*4k*.mov master/tearsofsteel_4k.mov
  rm -f master/tos4k.zip   # 6.4 GB, and the unpacked master is what we need
fi
if [ ! -f master/surround.ac3 ]; then
  curl -L -o master/surround.ac3 "$BASE/Surround-TOS_DVDSURROUND-Dolby%205.1.ac3"
fi
M=master/tearsofsteel_4k.mov
A=master/surround.ac3

# 1. H.264 1080p stereo. The baseline nothing can get wrong.
ffmpeg -y $CUT -i "$M" -vf scale=1920:-2 -c:v libx264 -preset medium -crf 20 \
  -pix_fmt yuv420p -an h264-1080p-video.mp4
ffmpeg -y -i h264-1080p-video.mp4 $CUT -i "$A" -map 0:v -map 1:a -c:v copy \
  -c:a aac -b:a 192k -ac 2 -shortest h264-1080p.mp4

# 2. HEVC 4K 10-bit with HDR10 signaling. Synthetic HDR: the master is SDR, the
#    PQ/BT.2020 signaling and mastering metadata are applied here.
#    Bitrate is targeted rather than left to CRF: the published cell claims
#    ~40 Mbps, and decode cost tracks bitrate directly, so the number has to be
#    a property of the fixture and not of how compressible this animation is.
ffmpeg -y $CUT -i "$M" -vf scale=3840:-2 -c:v libx265 -preset fast \
  -b:v 40M -maxrate 60M -bufsize 80M \
  -pix_fmt yuv420p10le \
  -x265-params "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400" \
  -an hevc-4k-hdr10-video.mp4
ffmpeg -y -i hevc-4k-hdr10-video.mp4 $CUT -i "$A" -map 0:v -map 1:a -c:v copy \
  -c:a aac -b:a 192k -ac 2 -shortest hevc-4k-hdr10.mp4

# 2b. HEVC 4K 10-bit HDR10 in Matroska container. Remux from MP4 with no
#     re-encode: same streams, different container. This is the case media
#     servers overwhelmingly serve; AVPlayer cannot open MKV at all, and
#     KSPlayer's free build gates MKV behind a paid tier.
ffmpeg -y -i hevc-4k-hdr10.mp4 -c:v copy -c:a copy hevc-4k-hdr10.mkv

# 3. AV1 10-bit in MKV. No hardware AV1 on M1, so this is the software race.
ffmpeg -y $CUT -i "$M" -vf scale=1920:-2 -c:v libsvtav1 -preset 8 -crf 30 \
  -pix_fmt yuv420p10le -c:a libopus -b:a 128k -ac 2 av1-10bit.mkv

# 4. VP9 in WebM. Second software case, different decoder lineage.
ffmpeg -y $CUT -i "$M" -vf scale=1920:-2 -c:v libvpx-vp9 -b:v 4M -row-mt 1 -deadline good -cpu-used 4 \
  -pix_fmt yuv420p -c:a libopus -b:a 128k -ac 2 vp9.webm

# 5. HEVC in MKV with SRT and ASS subtitles. Container breadth plus text subtitle cost.
#    ffmpeg cannot encode PGS, so bitmap subtitles are out of the public set.
printf '1\n00:00:02,000 --> 00:01:58,000\nBenchmark subtitle line\n\n' > subs.srt
ffmpeg -y -i subs.srt subs.ass
ffmpeg -y $CUT -i "$M" -i subs.srt -i subs.ass -vf scale=1920:-2 \
  -c:v libx265 -preset fast -crf 20 -pix_fmt yuv420p \
  -map 0:v -map 1 -map 2 -c:s copy -an hevc-subs.mkv

# 6. EAC3 5.1, transcoded from the real AC3 5.1 stem.
ffmpeg -y -i h264-1080p-video.mp4 $CUT -i "$A" -map 0:v -map 1:a -c:v copy \
  -c:a eac3 -b:a 640k -ac 6 -shortest eac3-51.mp4

# 7. Dolby Vision profile 8.1. RPU generated, injected, muxed with dvcC signaling.
ffmpeg -y -i hevc-4k-hdr10-video.mp4 -c:v copy -bsf:v hevc_mp4toannexb -f hevc dv-raw.hevc
cat > dv-gen.json <<'EOF'
{ "cm_version": "V29", "profile": "8.1", "length": 2160,
  "level6": { "max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
              "max_content_light_level": 1000, "max_frame_average_light_level": 400 } }
EOF
dovi_tool generate --json dv-gen.json --rpu-out dv-rpu.bin
dovi_tool inject-rpu -i dv-raw.hevc --rpu-in dv-rpu.bin -o dv-injected.hevc
MP4Box -add "dv-injected.hevc:dvp=8.1:hdlr=vide" -new dv-p81.mp4

rm -f h264-1080p-video.mp4 hevc-4k-hdr10-video.mp4 dv-raw.hevc dv-injected.hevc subs.srt subs.ass dv-gen.json dv-rpu.bin
echo "fixtures built"
