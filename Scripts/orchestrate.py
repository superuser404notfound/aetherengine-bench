#!/usr/bin/env python3
"""Runs the full benchmark matrix under the measurement protocol.

Protocol, per cell: one cold throwaway run, then REPEATS measured runs.
Engine order rotates across the whole session (not just within one
fixture's block) so thermal drift cannot favor a fixed position. Every
fixture starts with an idle baseline that is subtracted from package power.
Runs are discarded, with a reason, whenever: the SoC reported throttling
during the window, the player never produced a usable report (missing,
unparseable, or for the wrong backend/fixture), or delivered fewer frames
than the frame gate allows. Launches that crash on startup are retried and
counted, never silently retried away or folded into a measured run.

This is the file everything downstream reads (the published comparison
table), so every discard rule here is a promise: a run that reaches
Results/*.json as "discarded": false really played the whole window
cleanly, and a run marked discarded really failed for the stated reason.
"""
import argparse
import dataclasses
import datetime
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sampler

ROOT = pathlib.Path(__file__).resolve().parent.parent

# One binary per engine: two engines vendoring their own FFmpeg cannot share
# a process, and KSPlayer lives in its own project for the same reason.
BINARIES = {
    "aether": ROOT / ".build/Build/Products/Release/AetherBench",
    "avplayer": ROOT / ".build/Build/Products/Release/AVBench",
    "vlckit": ROOT / ".build/Build/Products/Release/VLCBench",
    "ksplayer": ROOT / ".build-ks/Build/Products/Release/KSBench",
}

# What `ps`'s comm column reports for the real player process, used to find
# it under the sudo wrapper (see find_player_pid). mpv is a bash script
# (Scripts/run-mpv.sh) that backgrounds the actual `mpv` binary; the other
# four are compiled binaries invoked directly, so their own basename is
# the process we are looking for.
PLAYER_COMM = {name: path.name for name, path in BINARIES.items()}
PLAYER_COMM["mpv"] = "mpv"

DEFAULT_FIXTURES = [
    "hevc-4k-hdr10.mp4", "hevc-4k-hdr10.mkv", "h264-1080p.mp4", "av1-10bit.mkv", "vp9.webm",
    "hevc-subs.mkv", "eac3-51.mp4", "dv-p81.mp4",
]
BACKENDS = ["aether", "avplayer", "vlckit", "ksplayer", "mpv"]

REQUIRED_REPORT_FIELDS = (
    "deliveredFrames", "droppedFrames", "expectedFrames",
    "startedAt", "endedAt", "backend", "fixture",
)

# Frame gate threshold. Measured deltas across the five engines land at
# 100-101% of the recomputed real-elapsed-window expected count (see
# compute_frame_gate below); a gate set at or near 1.0 has already discarded
# a healthy AetherEngine run in this project. 0.95 leaves five points of
# margin below that observed floor for ordinary, non-throttled frame drops
# (a real if rare event even on a clean run) while still catching the
# failure modes this gate exists for: a stalled decoder, a renderer that
# silently stopped presenting, or a player that launched but never really
# started.
DEFAULT_GATE_THRESHOLD = 0.95

# libvlc asserts and aborts at startup roughly one launch in eight (its own
# GL pipeline, not something this repo can fix); KSPlayer has intermittent
# readiness non-determinism. 5 attempts fails an entire cell only on a
# roughly 1-in-32768 run of bad luck for the 1-in-8 case, while every
# crashed attempt along the way is still counted, never silently absorbed.
DEFAULT_MAX_LAUNCH_ATTEMPTS = 5

# Idle baseline duration, fixed regardless of --measure: this is what gets
# subtracted from every record in a fixture's block (up to REPEATS *
# len(backends) of them), so its own cleanliness matters more than its
# length tracking the run's.
BASELINE_SECONDS = 60
# Coarse floor, not a reconstruction of sampler.py's own
# n = seconds*1000/interval_ms math: a baseline that produced under half
# its nominal sample count is evidence powermetrics was interrupted or
# degraded partway through, not a real 60s-worth-of-idle reading.
MIN_BASELINE_SAMPLES = BASELINE_SECONDS // 2
DEFAULT_BASELINE_MAX_ATTEMPTS = 5

# This machine measures roughly 150 mW CPU at genuine rest; a reading right
# after a four-target xcodebuild still settling has been observed at 530 mW.
# 250 mW leaves about 100 mW of margin above the real floor for ordinary
# background activity (Spotlight, backup daemons, ...) while sitting well
# below a machine that is still visibly working. Thermal state and sample
# count alone do not catch this: a still-settling machine reports Nominal
# and a full complement of samples the whole time, magnitude is the only
# signal that actually distinguishes "idle" from "just finished building."
MAX_BASELINE_CPU_MW = 250.0
# Two consecutive readings must agree within this many mW before a baseline
# is accepted, on top of the magnitude ceiling above: a machine cooling
# from a build can sit under the ceiling while still trending downward
# (e.g. 240 -> 190 -> 155), and the ceiling alone would accept the first
# reading that happens to duck under it rather than waiting for the
# machine to actually finish settling. 50 mW is generous enough to accept
# this machine's own observed idle-to-idle swing (seen up to ~85 mW
# across unrelated 60s baseline samples) without accepting two points of
# a still-declining trend as if they were a plateau.
BASELINE_STABILITY_TOLERANCE_MW = 50.0

# How long each of the four Swift binaries keeps playing, after writing its
# report, before exiting (Sources/Shared/BenchArguments.swift's --linger).
# Exists because the sampler's own window starts and ends slightly later
# than the player's own settle+measure window (process spawn, `sudo -u`
# privilege drop, and the sampler's first `ps` call all cost time the
# player's side does not pay), and without margin the sampler's last
# sample can land after the player has already exited, discarding an
# otherwise-clean run: this is what a four-hour session on this project
# actually hit (AVPlayer 1 of 15 runs usable, VLCKit 11/15, AetherEngine
# 13/15, KSPlayer 9/15, every discard reading "stopped responding after
# N-1 of N sample(s) needed"). Measured on this machine (task-11):
# launching AVBench (the worst-hit engine) through the real `sudo -u`
# demotion path with a concurrent `sudo powermetrics` sampler running
# (mirroring measure_once exactly, not a simplification), and clocking the
# moment the process actually exits against the idealized settle+measure
# deadline, shows only a 0.28-0.43s margin across five clean-machine
# trials, comfortably positive but well under the sampler's own 1s sample
# interval and with visibly no room to spare once a real multi-hour
# session adds thermal drift and background load neither this quick test
# nor a fresh machine can reproduce. 5s is roughly 10x that clean-machine
# margin and several sample intervals of headroom, while still costing no
# wall-clock time in the common case: see the short, linger-aware grace
# period _terminate_player is called with below, which signals the player
# the moment sampling ends instead of waiting the linger out.
DEFAULT_LINGER_SECONDS = 5.0

# _terminate_player's graceful-wait budget on the success path. Bounded and
# short on purpose, not sized to DEFAULT_LINGER_SECONDS: the player is
# already known to still be alive (it is lingering by design) by the time
# _terminate_player is called, and its report is already on disk (written
# before the linger sleep began), so there is nothing to gain by waiting
# it out, only wall-clock cost. This covers the ordinary teardown overhead
# observed even with no linger at all (~0.3-0.4s between a report being
# written and the process actually exiting, task-11's own measurement)
# with margin, then escalates to SIGTERM.
TERMINATE_GRACE_SECONDS = 3.0

# Mirrors Swift's BenchExitCode.refused (Sources/Shared/BenchRunner.swift):
# a BackendError.unsupportedFormat throw, a deterministic "this engine does
# not support this source" refusal (AVPlayer has no AV1 decoder at all,
# KSPlayer's free GPL build gates AV1 behind a paid tier), not a crash.
# Recorded once, with its reason, and never retried: retrying would waste
# max_launch_attempts on an outcome that will never change, and publishing
# it as a crash would be a false statement about the engine.
REFUSAL_EXIT_CODE = 3


@dataclasses.dataclass(frozen=True)
class ProtocolConfig:
    settle: float = 15
    measure: float = 60
    cooldown: float = 60
    repeats: int = 3
    max_launch_attempts: int = DEFAULT_MAX_LAUNCH_ATTEMPTS
    gate_threshold: float = DEFAULT_GATE_THRESHOLD
    linger: float = DEFAULT_LINGER_SECONDS


def parse_iso8601(value):
    """Both writers speak the same shape: no fractional seconds, always a
    literal trailing 'Z' (JSONEncoder.bench's .iso8601 strategy on the Swift
    side, and run-mpv.sh's `time.strftime(...Z...)` on the shell side).
    datetime.fromisoformat only accepts a bare 'Z' from Python 3.11 onward;
    normalizing it by hand keeps this working on whatever Python 3 the
    machine actually has, standard library only."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(value)


def load_report(report_path):
    """Returns (report_dict_or_None, error_or_None). Distinguishes "no file"
    from "file exists but is not valid JSON" so the discard reason says
    which one actually happened, instead of a generic empty-dict fallback
    that a zero-valued expectedFrames could sail straight through."""
    if not os.path.exists(report_path):
        return None, "no report file produced (player exited without writing one)"
    try:
        with open(report_path) as f:
            return json.load(f), None
    except Exception as exc:
        return None, f"report file exists but failed to parse ({exc.__class__.__name__}: {exc})"


def validate_report(report, expected_backend, expected_fixture):
    """None if the report is usable, otherwise the reason it is not.

    The backend/fixture cross-check exists because report paths are unique
    per attempt (see measure_once), but defense in depth is cheap: if a
    report somehow ended up describing a different run than the one that
    was launched, that is exactly the kind of silent mislabeling that must
    never reach the results file marked valid.
    """
    if not report:
        return "empty report"
    # report.get(f) is None catches both "key absent" and "key present but
    # null": no writer produces the latter today, but a bare `f not in
    # report` check would let a null field through to the arithmetic below
    # and raise a TypeError uncaught, exactly the "one exception loses the
    # whole session" failure mode this repo cannot afford anywhere in the
    # per-cell path.
    missing = [f for f in REQUIRED_REPORT_FIELDS if report.get(f) is None]
    if missing:
        return f"report missing or null required field(s): {', '.join(missing)}"
    try:
        if report["backend"] != expected_backend:
            return (f"report backend mismatch: launched {expected_backend!r}, "
                    f"report says {report['backend']!r} (stale report file?)")
        if report["fixture"] != expected_fixture:
            return (f"report fixture mismatch: launched {expected_fixture!r}, "
                    f"report says {report['fixture']!r} (stale report file?)")
        if report["expectedFrames"] <= 0:
            return f"report has non-positive expectedFrames ({report['expectedFrames']})"
        if report["deliveredFrames"] < 0:
            return f"report has negative deliveredFrames ({report['deliveredFrames']})"
        started, ended = parse_iso8601(report["startedAt"]), parse_iso8601(report["endedAt"])
        if ended <= started:
            return f"endedAt ({report['endedAt']}) not after startedAt ({report['startedAt']})"
    except Exception as exc:
        # Belt and suspenders beyond the null check above: a field of the
        # wrong type (a string where a number is expected, say) must become
        # a discard reason too, never an uncaught exception that takes the
        # whole session down with it.
        return f"unexpected error validating report ({exc.__class__.__name__}: {exc})"
    return None


def compute_frame_gate(report, nominal_measure_seconds, threshold):
    """expectedFrames as written by the binaries is nominal_measure_seconds *
    nominalFrameRate: Task.sleep(for:) is a lower bound, so the real
    settle+measure window this report's own startedAt/endedAt bracket is
    slightly longer than nominal, and both writer paths therefore *overshoot*
    that nominal expectation by about one percent. Recomputing expectedFrames
    from the report's own elapsed window (rather than trusting the binary's
    nominal-measure-based field) is the precise version, and that is what the
    gate is evaluated against; the reported value is kept in the record for
    reference (expectedFramesReported) but never used for the decision.

    validate_report has already guaranteed expectedFrames > 0 and
    endedAt > startedAt before this is called, so nominal_frame_rate and
    elapsed are both guaranteed positive here.
    """
    reported_expected = report["expectedFrames"]
    delivered = report["deliveredFrames"]
    started, ended = parse_iso8601(report["startedAt"]), parse_iso8601(report["endedAt"])
    elapsed = (ended - started).total_seconds()
    nominal_frame_rate = reported_expected / nominal_measure_seconds
    expected_real = elapsed * nominal_frame_rate
    ratio = delivered / expected_real if expected_real > 0 else 0.0
    return {
        "deliveredFrames": delivered,
        "expectedFramesReported": reported_expected,
        "expectedFramesReal": expected_real,
        "expectedFramesUsed": "real elapsed window (endedAt - startedAt) * nominalFrameRate",
        "nominalMeasureSeconds": nominal_measure_seconds,
        "elapsedSeconds": elapsed,
        "ratio": ratio,
        "threshold": threshold,
        "passed": ratio >= threshold,
    }


def dropped_frames_reported(report):
    """True if droppedFrames is a real measurement, False if it is the -1
    "not reported" sentinel (KSPlayer's KSAVPlayer path, see
    BenchRunner.swift), None if there is no report to ask at all. -1 must
    never be conflated with a real zero anywhere downstream."""
    if not report:
        return None
    return report.get("droppedFrames", -1) != -1


def _process_table():
    """One snapshot of every process on the system as (pid, ppid, comm)
    tuples. No special privilege needed to see our own descendants."""
    out = subprocess.run(["ps", "-axo", "pid,ppid,comm"], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            try:
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
            except ValueError:
                continue
    return rows


def find_player_pid(root_pid, comm_name, table_fn=_process_table):
    """BFS the process tree rooted at root_pid for the first descendant whose
    comm matches comm_name.

    root_pid is the pid Popen() hands back for `sudo -u user cmd`, and that
    is sudo's own monitor process, not cmd: verified empirically that sudo
    forks rather than exec-replacing itself, so the real player is one hop
    down for a compiled binary and two hops down for mpv (sudo -> bash
    running run-mpv.sh -> mpv, backgrounded with `&` inside that script).
    Sampling root_pid directly would silently measure the near-idle
    sudo/bash wrapper instead of the engine under test: sample_process
    would not raise, it would just return a real, wrong number, averaged
    into a published per-engine figure.
    """
    rows = table_fn()
    frontier = {root_pid}
    seen = set()
    while frontier:
        next_frontier = set()
        for pid, ppid, comm in rows:
            if ppid in frontier and pid not in seen:
                seen.add(pid)
                if os.path.basename(comm) == comm_name:
                    return pid
                next_frontier.add(pid)
        frontier = next_frontier
    return None


def wait_for_player_pid(root_pid, comm_name, deadline, poll_interval=0.2,
                         table_fn=_process_table, sleep_fn=time.sleep, now_fn=time.time):
    """Polls find_player_pid until it resolves or deadline (a time.time()-style
    timestamp) passes. The player may not have forked/exec'd yet at the first
    check."""
    while True:
        pid = find_player_pid(root_pid, comm_name, table_fn)
        if pid is not None:
            return pid
        if now_fn() >= deadline:
            return None
        sleep_fn(poll_interval)


def _read_stderr_tail(stderr_path, max_bytes=4000):
    """Best-effort last line of a launch attempt's captured stderr, or "" on
    any problem reading it (never raises: this is a diagnostic nicety, not
    load-bearing for the refused/crashed distinction, which is decided by
    exit code alone). BenchRunner writes exactly one line ("bench refused:
    ..." or "bench failed: ...") for its own errors; a genuine crash (e.g.
    VLCKit's assert aborting the process before Swift's own catch block
    ever runs) can leave unrelated engine debug output instead, in which
    case this is only ever a bonus, not the mechanism that tells refused
    apart from crashed.
    """
    if not stderr_path:
        return ""
    try:
        with open(stderr_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        lines = [line for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()]
        return lines[-1].strip() if lines else ""
    except Exception:
        return ""


def _exit_failure_reason(returncode, stderr_path):
    """The crash phrasing ("crashed during settle (exit N)") is unchanged
    from before this existed, other callers/tests match on that exact
    substring. Refusals get their own distinct phrasing so a reader (and
    launch_with_retry, see below) never has to infer which happened from
    the exit code alone."""
    tail = _read_stderr_tail(stderr_path)
    if returncode == REFUSAL_EXIT_CODE:
        base = f"refused this source (exit {returncode})"
    else:
        base = f"crashed during settle (exit {returncode})"
    return f"{base}: {tail}" if tail else base


def spawn_and_settle(proc, comm_name, settle_seconds, stderr_path=None, extra_grace=3.0, poll_interval=0.2,
                      sleep_fn=time.sleep, now_fn=time.time, table_fn=_process_table):
    """proc is an already-started Popen for `sudo -u user <player> ...`.
    Waits out the settle window, watching for three distinct outcomes: the
    sudo wrapper exiting early with REFUSAL_EXIT_CODE (a deterministic,
    honest "this engine cannot play this source", not a fault), exiting
    early with any other code (a crash during startup, e.g. VLCKit's known
    GL assert), or staying alive but never forking a recognizable player
    process (a hang before the window even opens). Returns
    (player_pid, failure_reason, refused); refused is only ever True
    alongside a non-None failure_reason and a None player_pid.
    """
    deadline = now_fn() + settle_seconds
    player_pid = None
    while now_fn() < deadline:
        if proc.poll() is not None:
            return None, _exit_failure_reason(proc.returncode, stderr_path), proc.returncode == REFUSAL_EXIT_CODE
        if player_pid is None:
            player_pid = find_player_pid(proc.pid, comm_name, table_fn)
        sleep_fn(poll_interval)
    if proc.poll() is not None:
        return None, _exit_failure_reason(proc.returncode, stderr_path), proc.returncode == REFUSAL_EXIT_CODE
    if player_pid is None:
        player_pid = wait_for_player_pid(proc.pid, comm_name, now_fn() + extra_grace,
                                          poll_interval, table_fn, sleep_fn, now_fn)
    if player_pid is None:
        return None, f"never produced a recognizable player process ({comm_name})", False
    return player_pid, None, False


def _baseline_subtract(key, value, baseline_value):
    """Only *Mw power fields are baseline-relative; residencies, the thermal
    string, sample counts and the throttled flag are reported as-is. None
    propagates rather than raising or silently becoming 0.0: a Mac that
    does not report ANE power at all must not look identical to one that
    reports exactly zero."""
    if not key.endswith("Mw"):
        return value
    if value is None or baseline_value is None:
        return None
    return value - baseline_value


def _terminate_player(proc, backend, wait_timeout=TERMINATE_GRACE_SECONDS):
    """Always called once a measured window is over or abandoned, success
    or failure alike: no path through measure_once may return with the
    player still alive. Tries a graceful wait first, escalating to SIGTERM
    and finally SIGKILL only if that does not clear the process in time.

    The graceful wait is short and bounded, not a wait for the player's own
    natural exit: on the success path the player is, by design, still
    deliberately alive here (see DEFAULT_LINGER_SECONDS), and its report
    was already written to disk before it started lingering, so there is
    nothing left to wait for. Waiting out the linger instead of signalling
    the player would silently add DEFAULT_LINGER_SECONDS of wall-clock
    cost to every single run in a session, exactly what the linger fix
    exists to avoid paying. wait_timeout is shortened further by the
    caller when the window was abandoned early (a sampler failure) rather
    than completed normally, so a doomed cell does not sit around waiting
    for a player that was never going to produce a trustworthy report
    anyway.
    """
    if proc.poll() is not None:
        return
    try:
        proc.wait(timeout=wait_timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    print(f"    {backend}: player did not exit within {wait_timeout}s, sending SIGTERM")
    try:
        proc.terminate()
        proc.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    print(f"    {backend}: player still alive after SIGTERM, killing")
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:
        pass


def take_clean_baseline(cfg, cool_and_retry, max_attempts=DEFAULT_BASELINE_MAX_ATTEMPTS,
                         seconds=BASELINE_SECONDS, sample_fn=None):
    """Takes and validates an idle-power baseline before it gets subtracted
    into every record of a fixture's block (up to REPEATS * len(backends)
    of them). A baseline recorded on a machine that had not finished
    cooling from the previous fixture's block silently corrupts all of
    them without a single one tripping discarded on its own; a fanless
    MacBook Air after a 4K run, or right after this project's own
    four-target xcodebuild, makes this a realistic scenario, not a corner
    case (measured live: 530.5 mW right after a build, against a genuine
    idle floor around 150 mW on the same machine).

    Thermal state and sample count are checked first (throttled, or with
    unreported thermal pressure, the same ambiguity evaluate_run guards
    against for measured runs), but neither catches a machine that is
    thermally Nominal yet still measurably busy: a still-settling machine
    reports Nominal and a full complement of samples the whole time.
    Magnitude is the only signal that actually distinguishes "idle" from
    "just finished building", so two further checks specifically target
    it:
      - MAX_BASELINE_CPU_MW: a ceiling between the genuine idle floor and
        what this machine measures while still settling.
      - BASELINE_STABILITY_TOLERANCE_MW: two CONSECUTIVE readings, both
        already under the ceiling, must agree within this tolerance. The
        ceiling alone would accept the first reading that happens to duck
        under it while the machine is still trending downward (e.g.
        240 -> 190 -> 155 mW); requiring back-to-back agreement is what
        actually confirms it has settled rather than just gotten lucky
        once. Any unclean or unstable reading resets this chain: a good
        reading, a bad one, then another good one does not count as two
        in a row.
    A raise from sample_fn itself (a hung or failing powermetrics) is
    caught here too, per the same rule as measure_once: a sampler failure
    becomes a documented, retried condition, never a session-ending
    exception.

    Retries up to max_attempts times, calling cool_and_retry() between
    attempts to cool down further.

    Returns (baseline, error). error is None when baseline is clean,
    stable, and ready to subtract. When every attempt fails to produce
    one, baseline is the last reading actually obtained (or {} if
    sample_fn never returned one at all) and error names why: callers
    must not subtract this baseline, only record what happened.
    """
    sample_fn = sample_fn or sampler.sample_power
    baseline = {}
    error = None
    previous_cpu_mw = None  # last reading that passed every check except stability
    for attempt in range(1, max_attempts + 1):
        try:
            baseline = sample_fn(seconds)
        except Exception as exc:
            error = f"sample_power raised: {exc}"
            baseline = {}
            previous_cpu_mw = None
        else:
            cpu_mw = baseline.get("cpuPowerMw")
            if baseline["throttled"]:
                error = f"machine reported throttling (level={baseline['thermalPressure']}) while idle"
                previous_cpu_mw = None
            elif baseline["thermalPressure"] is None:
                error = "thermal pressure was never reported during the idle baseline window"
                previous_cpu_mw = None
            elif baseline.get("samples", 0) < MIN_BASELINE_SAMPLES:
                error = (f"idle baseline carried only {baseline.get('samples')} sample(s), "
                         f"expected at least {MIN_BASELINE_SAMPLES}")
                previous_cpu_mw = None
            elif cpu_mw is None or cpu_mw > MAX_BASELINE_CPU_MW:
                error = (f"idle baseline cpuPowerMw={cpu_mw} exceeds the {MAX_BASELINE_CPU_MW:.0f} mW "
                         "quiet-machine ceiling (machine still busy, e.g. settling from a build?)")
                previous_cpu_mw = None
            elif previous_cpu_mw is not None and abs(cpu_mw - previous_cpu_mw) <= BASELINE_STABILITY_TOLERANCE_MW:
                return baseline, None
            else:
                if previous_cpu_mw is None:
                    delta_text = "first reading under the ceiling, need a consecutive match"
                else:
                    delta_text = f"{abs(cpu_mw - previous_cpu_mw):.1f} mW from the previous reading"
                error = (f"idle baseline cpuPowerMw={cpu_mw:.1f} not yet stable ({delta_text}, "
                         f"tolerance {BASELINE_STABILITY_TOLERANCE_MW:.0f} mW)")
                previous_cpu_mw = cpu_mw
        print(f"    idle baseline attempt {attempt}/{max_attempts} not clean ({error})")
        if attempt < max_attempts:
            cool_and_retry()
    return baseline, error


def launch(backend, fixture_path, report_path, cfg, stderr_path):
    """stderr_path captures the player's stderr (BenchRunner's own "bench
    refused: .../bench failed: ..." line, for the four Swift binaries) so
    _exit_failure_reason can read it back once the process has exited,
    instead of it only ever scrolling past live in the console. The file
    handle is opened for the duration of the Popen call only: the child
    inherits a dup of the fd at exec time, so closing this process's own
    copy right after does not affect it.

    --linger is passed to the four Swift binaries only (see
    DEFAULT_LINGER_SECONDS): mpv has no equivalent flag, run-mpv.sh is out
    of scope for task-11's fix, a known follow-up if the same discard ever
    shows up under mpv.
    """
    with open(stderr_path, "w") as stderr_file:
        if backend == "mpv":
            return subprocess.Popen(demote() + [str(ROOT / "Scripts/run-mpv.sh"), fixture_path,
                                                 str(cfg.settle), str(cfg.measure), report_path],
                                     stderr=stderr_file)
        return subprocess.Popen(demote() + [str(BINARIES[backend]), "--backend", backend, "--url", fixture_path,
                                             "--settle", str(cfg.settle), "--measure", str(cfg.measure),
                                             "--window", "1920x1080", "--display", "0",
                                             "--report", report_path, "--linger", str(cfg.linger)],
                                 stderr=stderr_file)


def launch_with_retry(backend, fixture, fixture_path, report_path_for_attempt, cfg, launch_failures):
    """Retries a launch that crashes before the player ever came up, up to
    cfg.max_launch_attempts times. Every crashed attempt increments
    launch_failures[backend][fixture], counted whether or not a later
    attempt succeeds: silently retrying until it works would flatter an
    engine with a flaky launcher, and this repo's whole point is to publish
    the real number.

    A REFUSAL_EXIT_CODE exit is different in kind, not degree, and is
    handled entirely separately: it is a deterministic "this engine does
    not support this source" (AVPlayer has no AV1 decoder, KSPlayer's free
    build gates AV1 behind a paid tier), so retrying would waste every
    remaining attempt on an outcome that was never going to change, and it
    is not counted in launch_failures at all, which exists to measure
    launch *reliability*: mixing in every fixture an engine correctly and
    honestly declines would understate how reliable its launcher actually
    is. Returns immediately on the first refusal, attempts stays at 1.

    Keyed by (backend, fixture), not just backend: a renderer that shows a
    session-wide count underneath one fixture's table would re-bill every
    fixture an engine did not fail on with failures that actually all
    happened on a different, harder fixture, silently multiplying the
    apparent failure rate for every fixture except the one that earned it.

    report_path_for_attempt(attempt) returns a fresh path per attempt, and
    any stale file at that path (report and captured stderr alike) is
    removed before spawning: a crashed attempt's partial (or leftover, from
    a previous session) report must never be read as this attempt's result.

    Returns (proc, player_pid, attempts, report_path, reason, refused).
    """
    comm_name = PLAYER_COMM[backend]
    attempts = 0
    reason = None
    while attempts < cfg.max_launch_attempts:
        attempts += 1
        report_path = report_path_for_attempt(attempts)
        stderr_path = report_path + ".stderr.log"
        for stale in (report_path, stderr_path):
            if os.path.exists(stale):
                os.remove(stale)
        proc = launch(backend, fixture_path, report_path, cfg, stderr_path)
        player_pid, reason, refused = spawn_and_settle(proc, comm_name, cfg.settle, stderr_path=stderr_path)
        if player_pid is not None:
            return proc, player_pid, attempts, report_path, None, False
        if refused:
            print(f"    {backend}: refused this source, not retrying ({reason})")
            return None, None, attempts, None, reason, True
        per_backend = launch_failures.setdefault(backend, {})
        per_backend[fixture] = per_backend.get(fixture, 0) + 1
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        print(f"    {backend}: launch attempt {attempts}/{cfg.max_launch_attempts} failed ({reason})")
        if attempts < cfg.max_launch_attempts:
            time.sleep(1)
    return None, None, attempts, None, reason, False


def negative_power_reason(power, baseline, subtracted_power):
    """None if every *Mw field subtracted to a physically sane (non-negative)
    value or stayed None; otherwise a reason string naming the offending
    field(s) together with the raw reading and the baseline that produced
    an impossible negative wattage.

    take_clean_baseline's magnitude and stability checks exist to keep a
    baseline like this from ever being accepted in the first place, but
    this is the last line of defense for whatever slips past them (a
    smaller max_attempts, a genuinely borderline reading): negative power
    consumption cannot happen, and a subtraction that produces one means
    the baseline itself was wrong, not that the engine generated power.
    """
    offenders = [
        f"{k} = raw {power.get(k)} - baseline {baseline.get(k)} = {v:.1f} mW"
        for k, v in subtracted_power.items() if k.endswith("Mw") and v is not None and v < 0
    ]
    if not offenders:
        return None
    return "physically impossible negative power after baseline subtraction (" + "; ".join(offenders) + ")"


def evaluate_run(power, report, load_error, process_error, backend, fixture, cfg,
                  baseline_error=None, negative_power=None):
    """Every discard reason that applies to one measured window, combined
    rather than the last one overwriting the rest: a run that was both
    thermally throttled AND failed the frame gate must say so, not report
    only whichever check happened to run last. Returns (reasons, frame_gate)
    where reasons is a (possibly empty) list of strings and frame_gate is
    the compute_frame_gate() dict, or None if there was no valid report to
    gate on.

    negative_power is negative_power_reason()'s own return value (a string
    naming the offending field(s), or None): passed in rather than
    recomputed here so this stays a pure combiner of reasons already
    decided elsewhere, matching baseline_error's own contract.
    """
    reasons = []
    if negative_power:
        reasons.append(negative_power)
    if baseline_error:
        # take_clean_baseline already refused to hand back an unclean
        # baseline for subtraction (measure_once zeroes the subtracted
        # power fields when this is set); this is what makes that fact
        # visible on every record in the affected fixture's block instead
        # of a bad number quietly appearing 15 times with discarded: false.
        reasons.append(f"idle baseline was not clean: {baseline_error}")
    if power["throttled"]:
        reasons.append(f"SoC reported throttling during the window (level={power['thermalPressure']})")
    elif power["thermalPressure"] is None:
        # sampler.py's own contract: throttled is False both when the
        # machine is cool AND when the thermal sampler produced no reading
        # at all, and thermalPressure is the only thing that tells the two
        # apart. A window this repo cannot confirm was thermally clean must
        # not be published as if it had been.
        reasons.append("thermal pressure was never reported during the window "
                        "(cannot confirm this run was thermally clean)")

    frame_gate = None
    if load_error:
        reasons.append(f"report problem: {load_error}")
    else:
        problem = validate_report(report, backend, fixture)
        if problem:
            reasons.append(f"report problem: {problem}")
        else:
            frame_gate = compute_frame_gate(report, cfg.measure, cfg.gate_threshold)
            if not frame_gate["passed"]:
                reasons.append(
                    f"frame gate: delivered {frame_gate['deliveredFrames']} of "
                    f"{frame_gate['expectedFramesReal']:.1f} recomputed-expected frames "
                    f"({frame_gate['ratio'] * 100:.1f}%), threshold {cfg.gate_threshold * 100:.0f}%")

    if process_error:
        reasons.append(f"process sampling failed: {process_error}")

    return reasons, frame_gate


def measure_once(backend, fixture, repeat, baseline, cfg, launch_failures, report_dir, baseline_error=None):
    fixture_path = str(ROOT / "Fixtures" / fixture)
    stamp = int(time.time() * 1000)

    def report_path_for_attempt(attempt):
        return str(report_dir / f"bench-{backend}-{pathlib.Path(fixture).stem}-{repeat}-{attempt}-{stamp}.json")

    proc, player_pid, attempts, report_path, launch_reason, refused = launch_with_retry(
        backend, fixture, fixture_path, report_path_for_attempt, cfg, launch_failures)

    record = {"backend": backend, "fixture": fixture, "repeat": repeat,
              "baseline": baseline, "launchAttempts": attempts, "refused": refused}
    if proc is None:
        if refused:
            # launch_reason already reads "refused this source (exit N): <why>"
            # (see _exit_failure_reason); naming the engine here is enough,
            # repeating "does not support this source" would be redundant.
            discard_reason = f"{backend}: {launch_reason}"
        else:
            discard_reason = f"launch failed after {attempts} attempt(s): {launch_reason}"
        record.update(power=None, powerRaw=None, process=None, report=None, frameGate=None,
                       droppedFramesReported=None, discarded=True, discardReason=discard_reason)
        return record

    results = {}

    def proc_worker():
        try:
            results["process"] = sampler.sample_process(player_pid, cfg.measure)
        except Exception as exc:
            results["processError"] = str(exc)

    t = threading.Thread(target=proc_worker)
    t.start()
    power = None
    power_error = None
    try:
        # sample_power is documented to raise, not return a plausible zero,
        # on a hung or failing powermetrics. Unguarded, that raise would
        # propagate out of measure_once, out of main()'s loop, and abort
        # the whole multi-hour session, discarding every record already
        # collected in memory. Guarded here, it becomes exactly what every
        # other failure mode in this file already is: one discarded record
        # with a reason, and the session keeps going.
        power = sampler.sample_power(cfg.measure)
    except Exception as exc:
        power_error = str(exc)
    finally:
        # No path out of this block, success or exception, may leave the
        # player running: t.join() first so proc_worker's ps reads never
        # race a process this call is about to terminate out from under
        # them, then terminate. Both grace periods are short (see
        # TERMINATE_GRACE_SECONDS): the success path does not wait out the
        # player's own linger, and a window abandoned early because power
        # sampling failed gets an even shorter one, since there is no
        # report worth waiting for at all.
        t.join()
        _terminate_player(proc, backend, wait_timeout=TERMINATE_GRACE_SECONDS if power_error is None else 5)

    if power_error is not None:
        record.update(power=None, powerRaw=None, process=results.get("process"), report=None,
                       frameGate=None, droppedFramesReported=None, discarded=True,
                       discardReason=f"power sampling failed: {power_error}")
        return record

    if baseline_error:
        subtracted_power = {k: None for k in power}
        negative_reason = None
    else:
        subtracted_power = {k: _baseline_subtract(k, power[k], baseline.get(k)) for k in power}
        negative_reason = negative_power_reason(power, baseline, subtracted_power)

    report, load_error = load_report(report_path)
    reasons, frame_gate = evaluate_run(power, report, load_error, results.get("processError"),
                                        backend, fixture, cfg, baseline_error, negative_reason)

    record.update({
        "power": subtracted_power,
        "powerRaw": power,
        "process": results.get("process"),
        "report": report,
        "droppedFramesReported": dropped_frames_reported(report),
        "frameGate": frame_gate,
        "discarded": bool(reasons),
        "discardReason": "; ".join(reasons),
    })
    return record


def engine_versions():
    """Reads every engine's version from the repository state, not from
    strings maintained by hand in four places. AetherEngine has no runtime
    version API, so its tag comes from the SwiftPM checkout the build
    already produced. A published table names versions, so these must be
    derived, never typed."""
    def run(cmd, cwd=None):
        try:
            return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                   check=True).stdout.strip()
        except Exception as exc:
            return f"unresolved ({exc.__class__.__name__})"

    checkout = ROOT / ".build/SourcePackages/checkouts/AetherEngine"

    def pinned(resolved_path, name):
        try:
            with open(resolved_path) as f:
                data = json.load(f)
            for pin in data.get("pins", []):
                if pin.get("identity", "").lower() == name:
                    state = pin.get("state", {})
                    return state.get("version") or state.get("revision", "")[:12]
        except Exception:
            pass
        return "unresolved"

    mpv_version_output = run(["mpv", "--version"])
    return {
        "aether": run(["git", "describe", "--tags", "--always"], cwd=checkout),
        "avplayer": "macOS " + run(["sw_vers", "-productVersion"]),
        "vlckit": pinned(ROOT / "AetherBench.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved",
                          "vlckit-spm"),
        "ksplayer": pinned(ROOT / "KSBench.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved",
                            "ksplayer"),
        "mpv": mpv_version_output.splitlines()[0] if mpv_version_output else "unresolved",
    }


def demote():
    """Every player is launched as the invoking user, never as root: root
    running a GPU-accelerated GUI player is not how any of these five
    engines are actually used, and would make the numbers unrepresentative
    even before considering the risk of a root-owned player process.

    SUDO_USER is required explicitly rather than falling back to $USER: this
    process itself runs as root (see main()'s geteuid check), and under
    `sudo`, $USER is not reliably the invoking user, it can read "root".
    Falling back to it would silently demote to root, defeating the whole
    point and doing so quietly.
    """
    target = os.environ.get("SUDO_USER")
    if not target:
        sys.exit("orchestrate.py: SUDO_USER is not set; refusing to guess who to "
                  "run the player as. Run this via `sudo Scripts/run-bench.sh`.")
    return ["sudo", "-u", target]


def write_results(out_path, payload):
    """Atomic write: to a temp file in the same directory, then os.replace
    over the real path. A reader, or this process getting killed mid-write,
    never sees a torn/partial JSON file; the previous good write survives
    on disk until the new one has landed completely. Called after every
    completed record (see run_session), not once at the very end, so a
    killed or crashed session leaves behind everything it had already
    measured, never nothing.
    """
    out_path = pathlib.Path(out_path)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, out_path)


def run_session(cfg, fixtures, backends, output_dir, dry_run=False, report_dir=pathlib.Path("/tmp")):
    """Runs the full matrix and returns the path it wrote. Split out of
    main() so it can be driven without argparse/the root check, e.g. by a
    caller that has already set up its own environment.
    """
    for f in fixtures:
        if not (ROOT / "Fixtures" / f).exists():
            print(f"warning: fixture does not exist on disk: {f} (runs against it will "
                  f"be recorded as discarded launch failures, not silently skipped)")

    # Keyed by (backend, fixture): a session-wide-only count would force the
    # renderer to either show one number under every fixture's table (see
    # launch_with_retry's docstring for why that misattributes failures) or
    # drop the information entirely. Nested by backend then fixture because
    # JSON object keys must be strings, not tuples.
    launch_failures = {b: {f: 0 for f in fixtures} for b in backends}
    records = []

    machine = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True).stdout.strip().replace(" ", "-")
    # machdep.cpu.brand_string ("Apple M1") names the SoC, not the chassis:
    # a MacBook Air and a MacBook Pro can share a chip but not a thermal
    # design, and the fanless Air is why this protocol has cooldowns and a
    # throttling discard rule at all. hw.model ("MacBookAir10,1") is the
    # one field that actually identifies the physical machine.
    machine_model = subprocess.run(["sysctl", "-n", "hw.model"],
                                    capture_output=True, text=True).stdout.strip()
    versions = engine_versions()
    date = time.strftime("%Y-%m-%d")
    os_version = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True).stdout.strip()
    out_dir = pathlib.Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-dryrun" if dry_run else ""
    out = out_dir / f"{machine}-{date}{suffix}.json"

    # payload["runs"] IS records and payload["launchFailures"] IS
    # launch_failures (same list/dict objects, not copies), so mutating
    # either and re-calling write_results(out, payload) always serializes
    # the latest state; nothing here needs to be reassembled per write.
    payload = {
        "machine": machine, "machineModel": machine_model, "date": date, "versions": versions,
        "os": os_version, "protocol": dataclasses.asdict(cfg),
        "launchFailures": launch_failures, "runs": records,
    }
    if dry_run:
        payload["dryRun"] = True
        payload["note"] = "shortened validation run (see --settle/--measure/--cooldown/--repeats), not published data"

    write_results(out, payload)
    print(f"results file: {out}")

    for fixture_index, fixture in enumerate(fixtures):
        print(f"=== {fixture}: idle baseline")
        baseline, baseline_error = take_clean_baseline(cfg, cool_and_retry=lambda: time.sleep(cfg.cooldown))
        if baseline_error:
            print(f"  WARNING: {fixture}'s idle baseline never became clean after retries "
                  f"({baseline_error}); this fixture's power figures will be marked unusable, "
                  f"not silently subtracted")
        for repeat in range(cfg.repeats):
            # Rotation offset advances across the WHOLE session (fixture_index
            # folded in), not just within one fixture's repeats: with a fixed
            # `repeat % len(backends)` offset, the same backend always starts
            # first at a given repeat index in every fixture's block, so
            # accumulated thermal drift across the full multi-hour session
            # would still slightly favor that backend's position.
            offset = (fixture_index * cfg.repeats + repeat) % len(backends)
            order = backends[offset:] + backends[:offset]
            for backend in order:
                if repeat == 0:
                    print(f"  {backend}: cold throwaway run")
                    measure_once(backend, fixture, -1, baseline, cfg, launch_failures, report_dir, baseline_error)
                    write_results(out, payload)
                    time.sleep(cfg.cooldown)
                print(f"  {backend}: repeat {repeat}")
                records.append(measure_once(backend, fixture, repeat, baseline, cfg, launch_failures,
                                             report_dir, baseline_error))
                write_results(out, payload)
                time.sleep(cfg.cooldown)

    print(f"wrote {out}")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settle", type=float, default=15.0)
    parser.add_argument("--measure", type=float, default=60.0)
    parser.add_argument("--cooldown", type=float, default=60.0)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-launch-attempts", type=int, default=DEFAULT_MAX_LAUNCH_ATTEMPTS)
    parser.add_argument("--gate-threshold", type=float, default=DEFAULT_GATE_THRESHOLD)
    parser.add_argument("--linger", type=float, default=DEFAULT_LINGER_SECONDS,
                         help="seconds the four Swift binaries keep playing after writing "
                              "their report, so this machine's sudo -u/sampler startup lag "
                              "never races the player's own exit (see DEFAULT_LINGER_SECONDS)")
    parser.add_argument("--fixtures", type=str, default=",".join(DEFAULT_FIXTURES),
                         help="comma-separated fixture filenames under Fixtures/")
    parser.add_argument("--backends", type=str, default=",".join(BACKENDS),
                         help="comma-separated backend names to include")
    parser.add_argument("--output-dir", type=str, default=str(ROOT / "Results"))
    parser.add_argument("--dry-run", action="store_true",
                         help="tag the output file and its contents as a shortened "
                              "validation run, never to be read as published data")
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("run under sudo: powermetrics requires it")

    cfg = ProtocolConfig(settle=args.settle, measure=args.measure, cooldown=args.cooldown,
                          repeats=args.repeats, max_launch_attempts=args.max_launch_attempts,
                          gate_threshold=args.gate_threshold, linger=args.linger)
    fixtures = [f for f in args.fixtures.split(",") if f]
    backends = [b for b in args.backends.split(",") if b]
    run_session(cfg, fixtures, backends, args.output_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
