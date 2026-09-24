# aetherengine-bench

Measures GPU power, CPU load and memory for five macOS playback engines, AetherEngine, AVPlayer, VLCKit, KSPlayer and libmpv, playing the same files under the same conditions.

**Conflict of interest, stated plainly:** this benchmark is written and run by the author of AetherEngine, one of the five engines being compared. The numbers it produces are published under the comparison table in [AetherEngine's README](https://github.com/superuser404notfound/AetherEngine). Nothing here is independently audited. What follows is the actual protocol, the actual fairness decisions and the actual gaps, so a reader, including a competing engine's author, can check the claims against the code and the committed raw data rather than take them on trust.

**AetherEngine does not win every row, and this document is not going to bury the rows where it loses.** On `hevc-4k-hdr10.mp4`, the one container AVPlayer can actually open (it refuses the same content in Matroska outright), AVPlayer uses less of everything: 1 mW GPU power, 1.7% of a core, 82 MB RSS and a 26 MB peak footprint from disk, against AetherEngine's 41 mW, 4.1% of a core, 343 MB and 321 MB, in the committed session (`Results/Apple-M1-2026-09-24-http-arm.json`). Much of AVPlayer's decode runs in a system service that no per-process column sees, but its own process is still the smallest here. On Matroska, VLCKit holds the least memory: 111 MB RSS against AetherEngine's 342 MB, and a lower peak footprint at both rates (281 against 323 MB at 38 Mbit/s, 314 against 681 MB at 90 Mbit/s). AetherEngine's peak footprint roughly doubles from the 38 to the 90 Mbit/s fixture, more than any other engine's (KSPlayer +35%, VLCKit +12%, libmpv +5%). Most of that is AVPlayer's own HLS buffer, which AetherEngine's native path feeds over a loopback and which therefore lives in the player process: measured with `footprint` during playback, the `CoreMedia Memory Pool` region went from 230 to 529 MB while AVPlayer held about 9 s ahead of and about 30 s behind the playhead. That buffer is sized in seconds, so its bytes follow the bitrate, and it stays bounded (flat at 470 to 530 MB over a 110 s session, the back buffer at 30 to 32 s). AVFoundation exposes no control over the back buffer. The engine's own share grew from 64 to 135 MB of malloc, a few fMP4 segments held in memory while they are muxed. AVPlayer playing a file directly buffers outside its process, which is why its own row reads 26 MB. Check any figure in this README, or in the table it generates, against that file directly.

## What is measured, and why

Each cell in the table is one (engine, fixture) pair. For every cell:

- **Warmup, then repeats.** The first run in a fixture's block is a cold throwaway (page cache, codec init, JIT/shader warmup) and is discarded unconditionally. It is followed by the configured number of measured repeats, currently 2 in the published session, and 2 is few, see "Known gaps" below.
- **15 s settle, then 60 s measurement.** The engine loads and starts playing, then 15 s pass before anything is sampled, so startup transients (buffering, initial autoexposure/tone-mapping settling, thread pool spin-up) are not counted as steady-state cost. Only the following 60 s window is measured.
- **Cooldowns between runs.** A fixed pause after every run, including the throwaway, lets the SoC's temperature relax before the next one starts, so one engine's run does not inherit thermal debt from the previous engine's run.
- **Engine order rotates across the whole session**, not just within one fixture's block. A fixed rotation offset per fixture would still let accumulated thermal drift over a multi-hour session slightly favor whichever engine always ran first at a given repeat index; folding the fixture index into the rotation offset avoids that.
- **An idle baseline is taken once per fixture block and subtracted from every power reading in that block.** `powermetrics` reports system-wide package power, not per-process, so what is published is baseline-subtracted, attributable to the run rather than to the machine sitting there. The baseline itself is validated, not just recorded: it is rejected and retried if the machine reports non-Nominal thermal pressure, if thermal pressure was never reported at all, if it carries too few samples, if its magnitude sits above a quiet-machine ceiling (measured on this machine: idle sits near 150 mW, but a reading taken right after this repo's own four-target Xcode build was 530.5 mW), or if two consecutive readings under that ceiling still disagree by more than a small tolerance, i.e. the machine is still cooling rather than actually settled.
- **Runs are discarded, with a machine-readable reason, when:** the SoC reported thermal throttling during the measurement window, or thermal pressure was never reported at all (a run this repo cannot confirm was thermally clean is not published as if it had been); the player never produced a usable report (missing, unparseable, or naming the wrong backend/fixture, defense against a stale file); it delivered fewer frames than the frame gate allows (currently 95% of the recomputed real-elapsed-window expected count, see the docstring for `DEFAULT_GATE_THRESHOLD` in `Scripts/orchestrate.py`, an engine cannot look efficient by silently dropping frames); or baseline subtraction produced a physically impossible negative power reading for a published column.
- **A crash during startup is retried up to 5 attempts, and every failed attempt is counted against that (backend, fixture) pair whether or not a later attempt in the same cell went on to succeed.** It is never silently retried away: the published table shows the count next to the fixture it happened on (`Scripts/orchestrate.py` counts launch failures per (backend, fixture), never as one session-wide total, so one engine's failures on a fixture it cannot play at all are never re-billed under every other fixture's clean table). A **deterministic refusal** is different and is not a launch failure at all: this is an engine correctly declining a source it was never going to support (`BenchExitCode.refused`, `Sources/Shared/BenchRunner.swift`, e.g. AVPlayer has no Matroska/WebM demuxer, KSPlayer's free GPL build gates AV1 behind a paid tier). It is recorded once, on the first attempt, never retried (retrying would waste attempts on an outcome that cannot change), and shown as that cell's own reason for publishing no numbers at all rather than folded into the launch-failure count.
- **Everything the table shows beyond the five numeric columns is read across every valid repeat, not just the first, and disclosed if repeats disagree.** Resolution, bit depth, colour transfer, and which of an engine's own internal playback paths served the fixture (`servingPath`, set only by KSPlayer today, see "Fairness decisions" below for why its two-step selection matters) are properties of the file and the engine, not of one particular repeat: three repeats disagreeing is a finding worth surfacing, not something to silently take one sample of, so the rendered block says "varies across repeats" instead. A field an engine's own API does not expose at all (`bitDepth: 0`, `colorTransfer: "unreported"` or `"unknown"`, `droppedFrames: -1`) renders as "not reported", never as a measurement of zero or an unknown transfer function name.

None of this is ceremony. Every one of these exists because it was observed to matter on this specific machine, most of them are cited with the actual number that motivated them in the source, mainly `Scripts/orchestrate.py` and `Scripts/sampler.py`, which are the ground truth if this document and the code ever disagree.

### Two ways in: from disk and over HTTP

Media servers deliver over HTTP, and an engine's network path is not its file path: AetherEngine, for one, reads a `file://` URL with one reader and an `http` URL with another that keeps a persistent range connection and a read window. A session can therefore run every fixture in two arms (`--arms file,http`):

- **disk**: the engine opens the fixture from `Fixtures/`, as every session before this one did.
- **HTTP**: the engine opens `http://127.0.0.1:<port>/<fixture>` from `Scripts/range-origin.py`, started by the orchestrator for the whole session. Every engine gets the same URL. The origin answers `bytes=a-b`, `bytes=a-` and `bytes=-n` with 206 and `Content-Range`, keeps connections alive like a media server, and is threaded. It is a link, not a memory copy: every response header waits a fixed latency, and one rate is shared across all connections on a virtual clock, so a second connection gets no free bandwidth. The defaults, 1000 Mbit/s and 20 ms (`--origin-mbps`, `--origin-latency-ms`), stand for a wired LAN to a media server. The session file records them under `origin`, and the rendered block prints them next to the numbers, because they are a variable of the HTTP rows.

Each (fixture, arm) pair is its own block with its own idle baseline, and the engine rotation advances across blocks exactly as it did across fixtures.

### Peak footprint

Alongside mean RSS from `ps`, every run records the player process's physical footprint through `proc_pid_rusage`: the mean over the window, and the kernel's lifetime maximum (`ri_lifetime_max_phys_footprint`, the "peak memory footprint" `/usr/bin/time -l` prints). Footprint is what the system's memory limit acts on, on iOS and tvOS a process is killed on it, and it is not RSS: it counts dirty and compressed memory and leaves out clean file-backed pages. The two can move independently, and AetherEngine's 7.15.2 fix to its HTTP read window is the measured case (peak footprint fell by 330 to 420 MB, mean RSS barely moved). The peak includes the load phase on purpose, since a process that peaks while opening a file is killed there.

## The machine

MacBook Air (M1), 4 efficiency + 4 performance cores; the macOS version of each session is in its results file (27.0 for the current one). It is **fanless**, which is load-bearing: sustained decode at 4K can push it into thermal throttling within a single measurement window, and there is no active cooling to pull it back down between runs. That is the entire reason this protocol has cooldowns and a throttling discard rule at all; on a machine with a fan neither would need to exist in this form. It also means the results describe this machine's thermal ceiling, not a generic "M1", and are not directly comparable to a fanned M1 laptop.

## Reproducing it

Tools, all available via Homebrew:

```bash
brew install xcodegen ffmpeg gpac mpv dovi_tool
```

You also need `curl`/`unzip`/`python3` (stdlib only, no pip installs) from the base system, and Xcode for `xcodebuild`. This repository was built with Xcode 26.6 (build 17F113); earlier versions are untested.

**1. Build the fixtures.** One CC-BY master (Blender Foundation's *Tears of Steel*, downloaded once, 6.4 GB) is cut into the eight benchmark files:

```bash
Scripts/make-fixtures.sh
Scripts/verify-fixtures.sh   # asserts codec/container/duration/HDR metadata actually match what each fixture claims
```

**2. Grant `powermetrics` root access without a password prompt.** `powermetrics` reads privileged hardware energy counters and refuses to run as a non-root user at all. The whole benchmark session therefore runs under `sudo`, but that alone is not enough: `Scripts/sampler.py` shells out to `sudo powermetrics` internally, once per measured run, for a multi-hour unattended session, and a plain `sudo` there would block on a password prompt nothing will ever answer. It needs its own passwordless grant, scoped narrowly to the one binary:

```bash
sudo visudo -f /etc/sudoers.d/aetherengine-bench
# add one line, with yourusername replaced by the output of `whoami`:
# yourusername ALL=(ALL) NOPASSWD: /usr/bin/powermetrics
```

**3. Build the four Swift binaries and run the session.** `Scripts/run-bench.sh` builds everything in Release, waits out a fixed cooldown so the build itself does not bias the first idle baseline, then runs the full matrix:

```bash
sudo Scripts/run-bench.sh
```

Under `sudo`, every player is launched demoted back to the invoking user via `sudo -u` (see `demote()` in `Scripts/orchestrate.py`), because a root-owned GPU-accelerated player is not how any of these five engines are actually used and would make the numbers unrepresentative. With the grant from step 2 in place the session can also run as the invoking user, `Scripts/run-bench.sh` without `sudo`: `powermetrics` is then the only privileged call, the players need no demotion at all, and an unattended session no longer needs an interactive root shell to start. Both modes launch the players as the same user. A shortened, clearly-labelled dry run for checking that the pipeline itself works is:

```bash
sudo Scripts/run-bench.sh --dry-run --settle 2 --measure 5 --cooldown 2 --repeats 1 --fixtures h264-1080p.mp4
```

Results land as a new timestamped file under `Results/`, written after every completed run so a killed session does not lose what it already measured.

**4. Render the table.**

```bash
python3 Scripts/render-table.py Results/<machine>-<date>.json
```

This is the markdown block published under the comparison table in AetherEngine's README.

## One binary per engine

Each engine is a separate command-line tool target, not a menu inside one app, and KSPlayer specifically lives in its own Xcode project (`project-ksplayer.yml` / `KSBench.xcodeproj`, built and orchestrated separately from `project.yml` / `AetherBench.xcodeproj`). Two reasons, one mechanical and one about the numbers:

- AetherEngine's FFmpegBuild and KSPlayer's FFmpegKit both declare SwiftPM binary targets under identical names. SwiftPM resolves per project, so the two cannot share a dependency graph, full stop, this is not a design choice either engine gets to opt out of.
- Even where that were not true, two engines vendoring their own copy of FFmpeg have no business sharing a process for a measurement like this: symbol collisions, differing build configurations, and shared global FFmpeg state would make it impossible to say with confidence which engine's numbers are being read.

Command-line tool targets, not app bundles, because there is nothing here that needs an app's lifecycle, only a window, playback, and a JSON report on exit.

## Fairness decisions

These are the decisions most likely to be challenged, each with the reasoning behind it:

- **Every engine is driven on its own primary path, not forced onto a fallback.** KSPlayer is exercised through its own `KSOptions.firstPlayerType` / `secondPlayerType` selection order, exactly as `KSPlayerLayer` performs it internally, rather than being pinned directly to `KSMEPlayer` (its FFmpeg-backed engine). On this machine, with this KSPlayer version, that selection lands on `KSMEPlayer` for every fixture anyway, but not because the AVPlayer-backed primary path (`KSAVPlayer`) was skipped: it is attempted first and observed to fail. Reading `KSAVPlayer`'s own source shows why, it rejects tracks that a real player could in fact play, via a deprecated synchronous `AVAssetTrack.isPlayable` check. That is stated here as an observed behavior of KSPlayer 2.3.4 on macOS 26.5.2, not as a criticism of the project; a different KSPlayer version or OS could behave differently, and if it does, this benchmark's own two-step selection will measure that different behavior automatically rather than needing to be updated by hand.
- **libmpv is measured with `--hwdec=auto-safe`, not mpv's own shipped default of software decode (`--hwdec=no`).** The other four engines all default to hardware decoding when it is available. Measuring mpv at its literal out-of-the-box default would compare mpv-without-a-flag against four engines that never needed one, scoring a flag omission as if it were engine inefficiency. `--hwdec=auto-safe` puts mpv on the same footing: hardware decode when the platform supports it.
- **KSPlayer is measured as the free GPL build.** Its paid LGPL tier, which lifts some of the free tier's format restrictions (AV1 among them, see `KSPlayerBackend.swift`'s own comments on where that surfaces), was not available for this benchmark and is not measured.
- **Frame delivery is gated so an engine cannot look efficient by dropping frames.** None of these five engines exposes the same kind of presented-frame counter. Where a real counter exists (VLCKit) it is used; where it does not (AetherEngine's native path, AVPlayer, KSPlayer, mpv) a proxy is used instead, elapsed playback time times nominal frame rate, minus real drops where those are counted. That proxy is labelled as a proxy everywhere it appears, in the code and in the rendered table's own caveat text, and it is used only as a pass/fail gate against the frame-gate threshold, never published as a quality figure to compare across engines.

## Known gaps

Stated without hedging, because a benchmark that only lists its own strengths is not trustworthy:

- **No tvOS or iOS numbers.** This machine and this measurement pipeline (`powermetrics`, a windowed macOS process, `sudo -u` demotion) do not run headless on Apple's TV or mobile platforms, and there is no automatable equivalent. AetherEngine's actual target platform is tvOS/iOS; every number in this repository is from macOS instead.
- **No Dolby Atmos.** An EAC3+JOC (Atmos) fixture cannot be legally produced for a public repository, there is no open encoder for it.
- **No PGS (bitmap) subtitles.** ffmpeg, which builds every fixture here, cannot encode PGS. The subtitle fixture carries SRT and ASS text tracks only.
- **KSPlayer's paid LGPL tier is not measured**, see above.
- **The HDR10 and Dolby Vision fixtures are synthetically signalled, not natively graded.** The source master is SDR; PQ/BT.2020 transfer characteristics, mastering-display metadata, and (for the Dolby Vision fixture) a generated RPU are applied on top of it. These fixtures exercise the decode and display-pipeline paths that real HDR/DV content would, but they are not real graded masters and should not be read as evidence about how any engine handles authored HDR content.
- **CPU package power is measured and recorded for every run, but not published in the rendered table.** It is the one power figure whose repeat-to-repeat spread, at sustained 4K load on this fanless machine, has been observed wide enough to exceed the differences between engines it would be used to show. In the committed session, AetherEngine's own repeats on `hevc-4k-hdr10.mkv` ranged from 65 to 291 mW, and VLCKit's on `hevc-4k-hdr10.mp4` ranged from 23 to 463 mW; a spread of that size on one engine's own repeated measurement of itself cannot support a cross-engine comparison. The underlying cause (the idle baseline is sampled once per fixture block, while the SoC's temperature drifts with load over the course of that block) scales with how demanding the fixture is: a light 1080p fixture's own spread can be small, AetherEngine measured 44 to 47 mW on `h264-1080p.mp4` in the same session, but the column is dropped for every fixture uniformly rather than kept where it happens to look stable. GPU power, CPU load and RSS did not show this problem on any of the eight fixtures in the committed session and are published instead. The raw CPU power numbers are still in `Results/`, for anyone who wants to look at them with that caveat in mind.
- **The HTTP arm is a modelled link on loopback, not a network.** It has latency and a shared rate, but no packet loss, no TCP congestion behaviour and no Wi-Fi. It separates an engine's network reader from its file reader; it does not predict a particular home network.
- **The origin's own CPU shows up in package power, not in any engine's process figures.** The idle baseline is taken with the origin idle, so on the HTTP rows the CPU package power (which is not published anyway, see above) includes the origin's work for however many bytes the engine pulled. GPU power, CPU load, RSS and footprint are unaffected.
- **The 90 Mbit/s fixture is re-encoded from the 38 Mbit/s one with light film grain added** (`Scripts/make-fixtures.sh`, step 2c), because the animation alone does not need that rate. Same frames, audio and HDR signalling, so the pair differs in bitrate only; it is still not a real remux.
- **Two repeats per cell in the published session.** That is few. A median of 2 is closer to "these two runs agreed" than to a statistically solid estimate, and it is the number actually in the committed session file, not a larger number quoted from elsewhere.

## Where the raw data is

`Results/*.json`, one file per session, named `<machdep.cpu.brand_string>-<date>[-dryrun].json`. Never overwritten by a later render, each session is its own file. Top level:

- `machine` / `machineModel` / `os`: `machdep.cpu.brand_string` (the SoC, e.g. "Apple M1"), `hw.model` (the actual chassis, e.g. "MacBookAir10,1", which is what the fanless claim is checked against), and the macOS version.
- `versions`: each engine's version, derived from repository/checkout state (git tag for AetherEngine, `Package.resolved` pins for VLCKit/KSPlayer, `sw_vers`/`mpv --version` for AVPlayer/libmpv), never typed by hand.
- `protocol`: the exact settle/measure/cooldown/repeats/gate-threshold/linger values this session ran with.
- `launchFailures`: crash counts, keyed by backend then fixture (`<fixture>@http` for the HTTP arm), counted whether or not a later retry succeeded.
- `arms`, `origin`, `fixtures`: which arms ran, the HTTP origin's link rate and latency, and each fixture's size and overall bitrate as ffprobe reads it.
- `runs`: one record per measured attempt (including discarded ones), each carrying:
  - `backend`, `fixture`, `arm` (`file` or `http`; absent in sessions recorded before arms existed, which were all `file`), `repeat`, `refused`, `launchAttempts`, `discarded`, `discardReason`.
  - `baseline`: the idle power/thermal reading subtracted for this run's fixture block.
  - `power`: baseline-subtracted package power and cluster residency (what a valid run's table figures come from); `powerRaw`: the same, unsubtracted.
  - `process`: per-process CPU% and RSS from `ps`, mean and peak, plus `footprintMbMean` and `footprintMbPeak` from `proc_pid_rusage` (None, never 0, when the kernel would not report them).
  - `report`: the player's own self-report, delivered/dropped/expected frames, resolution, bit depth, color transfer, serving path where applicable.
  - `frameGate`: the recomputed pass/fail evaluation behind the frame-delivery discard rule.

`Scripts/render-table.py` is the only code that turns this into the published table, and it is deliberately conservative about what it will print as a number versus what it prints as "not reported" or "n/a"; reading its module docstring alongside a `Results/*.json` file is the fastest way to check any published figure against the underlying measurement.

## License

**GPL-3.0** (see `LICENSE`). This repository builds `KSBench`, which links KSPlayer's free build, and KSPlayer's own `LICENSE` file (checked out at `2.3.4` via SwiftPM from [kingslay/KSPlayer](https://github.com/kingslay/KSPlayer)) is GNU GPL version 3, not version 2. KSPlayer's own FFmpegKit dependency ([kingslay/FFmpegKit](https://github.com/kingslay/FFmpegKit), pinned `6.1.4`) carries the identical GPLv3 text, so nothing about that pin narrows the requirement. GPLv3 and GPLv2 are mutually incompatible, so a work linking GPLv3 code cannot be distributed under GPLv2, and GPL-3.0 is therefore the license for this whole repository, not just for `KSBench`.

The other four engines, checked the same way, do not carry this obligation themselves, they just do not relax it either:

- **AetherEngine** ([superuser404notfound/AetherEngine](https://github.com/superuser404notfound/AetherEngine)) is LGPLv3 with an app-store distribution exception, per its own `LICENSE` file.
- **VLCKit** (via [virtualox/vlckit-spm](https://github.com/virtualox/vlckit-spm)) is LGPLv2.1, per that package's own README, which points to VLC's upstream `COPYING`.
- **libmpv** is invoked as an external process (`Scripts/run-mpv.sh` launches the standalone `mpv` binary over its own IPC socket), never linked into a binary this repository builds, so its own license terms have no bearing on this repository's.
- **AVPlayer** is an Apple system framework, not a dependency this repository vendors, checks out, or links, so it carries no license implication here either.
