# aetherengine-bench

Measures GPU power, CPU load and memory for five macOS playback engines, AetherEngine, AVPlayer, VLCKit, KSPlayer and libmpv, playing the same files under the same conditions.

**Conflict of interest, stated plainly:** this benchmark is written and run by the author of AetherEngine, one of the five engines being compared. The numbers it produces are published under the comparison table in [AetherEngine's README](https://github.com/superuser404notfound/AetherEngine). Nothing here is independently audited. What follows is the actual protocol, the actual fairness decisions and the actual gaps, so a reader, including a competing engine's author, can check the claims against the code and the committed raw data rather than take them on trust.

**AetherEngine does not win every row, and this document is not going to bury the row where it loses.** On `hevc-4k-hdr10.mp4`, the one fixture AVPlayer can actually open (it refuses the same content in an MKV container outright), AVPlayer uses less of everything: 2 mW GPU power, 1.7% of a core, 81 MB RSS, against AetherEngine's 66 mW, 5.3% of a core, 317 MB, in the committed session (`Results/Apple-M1-2026-08-16.json`). Check any figure in this README, or in the table it generates, against that file directly.

## What is measured, and why

Each cell in the table is one (engine, fixture) pair. For every cell:

- **Warmup, then repeats.** The first run in a fixture's block is a cold throwaway (page cache, codec init, JIT/shader warmup) and is discarded unconditionally. It is followed by the configured number of measured repeats, currently 2 in the published session, and 2 is few, see "Known gaps" below.
- **15 s settle, then 60 s measurement.** The engine loads and starts playing, then 15 s pass before anything is sampled, so startup transients (buffering, initial autoexposure/tone-mapping settling, thread pool spin-up) are not counted as steady-state cost. Only the following 60 s window is measured.
- **Cooldowns between runs.** A fixed pause after every run, including the throwaway, lets the SoC's temperature relax before the next one starts, so one engine's run does not inherit thermal debt from the previous engine's run.
- **Engine order rotates across the whole session**, not just within one fixture's block. A fixed rotation offset per fixture would still let accumulated thermal drift over a multi-hour session slightly favor whichever engine always ran first at a given repeat index; folding the fixture index into the rotation offset avoids that.
- **An idle baseline is taken once per fixture block and subtracted from every power reading in that block.** `powermetrics` reports system-wide package power, not per-process, so what is published is baseline-subtracted, attributable to the run rather than to the machine sitting there. The baseline itself is validated, not just recorded: it is rejected and retried if the machine reports non-Nominal thermal pressure, if thermal pressure was never reported at all, if it carries too few samples, if its magnitude sits above a quiet-machine ceiling (measured on this machine: idle sits near 150 mW, but a reading taken right after this repo's own four-target Xcode build was 530.5 mW), or if two consecutive readings under that ceiling still disagree by more than a small tolerance, i.e. the machine is still cooling rather than actually settled.
- **Runs are discarded, with a machine-readable reason, when:** the SoC reported thermal throttling during the measurement window, or thermal pressure was never reported at all (a run this repo cannot confirm was thermally clean is not published as if it had been); the player never produced a usable report (missing, unparseable, or naming the wrong backend/fixture, defense against a stale file); it delivered fewer frames than the frame gate allows (currently 95% of the recomputed real-elapsed-window expected count, see the docstring for `DEFAULT_GATE_THRESHOLD` in `Scripts/orchestrate.py`, an engine cannot look efficient by silently dropping frames); or baseline subtraction produced a physically impossible negative power reading for a published column. A crash during startup is retried up to 5 attempts and every failed attempt is still counted (see "Fairness decisions" below), it is never silently retried away.

None of this is ceremony. Every one of these exists because it was observed to matter on this specific machine, most of them are cited with the actual number that motivated them in the source, mainly `Scripts/orchestrate.py` and `Scripts/sampler.py`, which are the ground truth if this document and the code ever disagree.

## The machine

MacBook Air (M1), 4 efficiency + 4 performance cores, macOS 26.5.2. It is **fanless**, which is load-bearing: sustained decode at 4K can push it into thermal throttling within a single measurement window, and there is no active cooling to pull it back down between runs. That is the entire reason this protocol has cooldowns and a throttling discard rule at all; on a machine with a fan neither would need to exist in this form. It also means the results describe this machine's thermal ceiling, not a generic "M1", and are not directly comparable to a fanned M1 laptop.

## Reproducing it

Tools, all available via Homebrew:

```bash
brew install xcodegen ffmpeg gpac mpv dovi_tool
```

You also need Xcode 26+ (for `xcodebuild`) and `curl`/`unzip`/`python3` (stdlib only, no pip installs) from the base system.

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

Root is required for the whole session, not only for the `powermetrics` calls: every player is then launched demoted back to the invoking user via `sudo -u` (see `demote()` in `Scripts/orchestrate.py`), because a root-owned GPU-accelerated player is not how any of these five engines are actually used and would make the numbers unrepresentative. A shortened, clearly-labelled dry run for checking that the pipeline itself works is:

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
- **CPU package power is measured and recorded for every run, but not published in the rendered table.** It is the one power figure whose repeat-to-repeat spread, at sustained 4K load on this fanless machine, has been observed wide enough to exceed the differences between engines it would be used to show. In the committed session, AetherEngine's own repeats on `hevc-4k-hdr10.mkv` ranged from 65 to 291 mW, and VLCKit's on `hevc-4k-hdr10.mp4` ranged from 23 to 463 mW; a spread of that size on one engine's own repeated measurement of itself cannot support a cross-engine comparison. GPU power, CPU load and RSS did not show this problem on any of the eight fixtures in the committed session and are published instead. The raw CPU power numbers are still in `Results/`, for anyone who wants to look at them with that caveat in mind.
- **Two repeats per cell in the published session.** That is few. A median of 2 is closer to "these two runs agreed" than to a statistically solid estimate, and it is the number actually in the committed session file, not a larger number quoted from elsewhere.

## Where the raw data is

`Results/*.json`, one file per session, named `<machdep.cpu.brand_string>-<date>[-dryrun].json`. Never overwritten by a later render, each session is its own file. Top level:

- `machine` / `machineModel` / `os`: `machdep.cpu.brand_string` (the SoC, e.g. "Apple M1"), `hw.model` (the actual chassis, e.g. "MacBookAir10,1", which is what the fanless claim is checked against), and the macOS version.
- `versions`: each engine's version, derived from repository/checkout state (git tag for AetherEngine, `Package.resolved` pins for VLCKit/KSPlayer, `sw_vers`/`mpv --version` for AVPlayer/libmpv), never typed by hand.
- `protocol`: the exact settle/measure/cooldown/repeats/gate-threshold/linger values this session ran with.
- `launchFailures`: crash counts, keyed by backend then fixture, counted whether or not a later retry succeeded.
- `runs`: one record per measured attempt (including discarded ones), each carrying:
  - `backend`, `fixture`, `repeat`, `refused`, `launchAttempts`, `discarded`, `discardReason`.
  - `baseline`: the idle power/thermal reading subtracted for this run's fixture block.
  - `power`: baseline-subtracted package power and cluster residency (what a valid run's table figures come from); `powerRaw`: the same, unsubtracted.
  - `process`: per-process CPU% and RSS from `ps`, mean and peak.
  - `report`: the player's own self-report, delivered/dropped/expected frames, resolution, bit depth, color transfer, serving path where applicable.
  - `frameGate`: the recomputed pass/fail evaluation behind the frame-delivery discard rule.

`Scripts/render-table.py` is the only code that turns this into the published table, and it is deliberately conservative about what it will print as a number versus what it prints as "not reported" or "n/a"; reading its module docstring alongside a `Results/*.json` file is the fastest way to check any published figure against the underlying measurement.

## License

GPLv2, see `LICENSE`.
