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
    "hevc-4k-hdr10.mp4", "h264-1080p.mp4", "av1-10bit.mkv", "vp9.webm",
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


@dataclasses.dataclass(frozen=True)
class ProtocolConfig:
    settle: float = 15
    measure: float = 60
    cooldown: float = 60
    repeats: int = 3
    max_launch_attempts: int = DEFAULT_MAX_LAUNCH_ATTEMPTS
    gate_threshold: float = DEFAULT_GATE_THRESHOLD


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
    missing = [f for f in REQUIRED_REPORT_FIELDS if f not in report]
    if missing:
        return f"report missing required field(s): {', '.join(missing)}"
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
    try:
        started, ended = parse_iso8601(report["startedAt"]), parse_iso8601(report["endedAt"])
    except Exception as exc:
        return f"unparseable startedAt/endedAt ({exc.__class__.__name__}: {exc})"
    if ended <= started:
        return f"endedAt ({report['endedAt']}) not after startedAt ({report['startedAt']})"
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


def spawn_and_settle(proc, comm_name, settle_seconds, extra_grace=3.0, poll_interval=0.2,
                      sleep_fn=time.sleep, now_fn=time.time, table_fn=_process_table):
    """proc is an already-started Popen for `sudo -u user <player> ...`.
    Waits out the settle window, watching for two distinct failure modes: the
    sudo wrapper exiting early (a crash during startup, e.g. VLCKit's known
    GL assert) and the wrapper staying alive but never forking a recognizable
    player process (a hang before the window even opens). Returns
    (player_pid, failure_reason); exactly one of the two is not None.
    """
    deadline = now_fn() + settle_seconds
    player_pid = None
    while now_fn() < deadline:
        if proc.poll() is not None:
            return None, f"crashed during settle (exit {proc.returncode})"
        if player_pid is None:
            player_pid = find_player_pid(proc.pid, comm_name, table_fn)
        sleep_fn(poll_interval)
    if proc.poll() is not None:
        return None, f"crashed during settle (exit {proc.returncode})"
    if player_pid is None:
        player_pid = wait_for_player_pid(proc.pid, comm_name, now_fn() + extra_grace,
                                          poll_interval, table_fn, sleep_fn, now_fn)
    if player_pid is None:
        return None, f"never produced a recognizable player process ({comm_name})"
    return player_pid, None


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


def launch(backend, fixture_path, report_path, cfg):
    if backend == "mpv":
        return subprocess.Popen(demote() + [str(ROOT / "Scripts/run-mpv.sh"), fixture_path,
                                             str(cfg.settle), str(cfg.measure), report_path])
    return subprocess.Popen(demote() + [str(BINARIES[backend]), "--backend", backend, "--url", fixture_path,
                                         "--settle", str(cfg.settle), "--measure", str(cfg.measure),
                                         "--window", "1920x1080", "--display", "0",
                                         "--report", report_path])


def launch_with_retry(backend, fixture, fixture_path, report_path_for_attempt, cfg, launch_failures):
    """Retries a launch that crashes before the player ever came up, up to
    cfg.max_launch_attempts times. Every crashed attempt increments
    launch_failures[backend], counted whether or not a later attempt
    succeeds: silently retrying until it works would flatter an engine with
    a flaky launcher, and this repo's whole point is to publish the real
    number.

    report_path_for_attempt(attempt) returns a fresh path per attempt, and
    any stale file at that path is removed before spawning: a crashed
    attempt's partial (or leftover, from a previous session) report must
    never be read as this attempt's result.
    """
    comm_name = PLAYER_COMM[backend]
    attempts = 0
    reason = None
    while attempts < cfg.max_launch_attempts:
        attempts += 1
        report_path = report_path_for_attempt(attempts)
        if os.path.exists(report_path):
            os.remove(report_path)
        proc = launch(backend, fixture_path, report_path, cfg)
        player_pid, reason = spawn_and_settle(proc, comm_name, cfg.settle)
        if player_pid is not None:
            return proc, player_pid, attempts, report_path, None
        launch_failures[backend] = launch_failures.get(backend, 0) + 1
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        print(f"    {backend}: launch attempt {attempts}/{cfg.max_launch_attempts} failed ({reason})")
        if attempts < cfg.max_launch_attempts:
            time.sleep(1)
    return None, None, attempts, None, reason


def evaluate_run(power, report, load_error, process_error, backend, fixture, cfg):
    """Every discard reason that applies to one measured window, combined
    rather than the last one overwriting the rest: a run that was both
    thermally throttled AND failed the frame gate must say so, not report
    only whichever check happened to run last. Returns (reasons, frame_gate)
    where reasons is a (possibly empty) list of strings and frame_gate is
    the compute_frame_gate() dict, or None if there was no valid report to
    gate on.
    """
    reasons = []
    if power["throttled"]:
        reasons.append(f"SoC reported throttling during the window (level={power['thermalPressure']})")

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


def measure_once(backend, fixture, repeat, baseline, cfg, launch_failures, report_dir):
    fixture_path = str(ROOT / "Fixtures" / fixture)
    stamp = int(time.time() * 1000)

    def report_path_for_attempt(attempt):
        return str(report_dir / f"bench-{backend}-{pathlib.Path(fixture).stem}-{repeat}-{attempt}-{stamp}.json")

    proc, player_pid, attempts, report_path, launch_reason = launch_with_retry(
        backend, fixture, fixture_path, report_path_for_attempt, cfg, launch_failures)

    record = {"backend": backend, "fixture": fixture, "repeat": repeat,
              "baseline": baseline, "launchAttempts": attempts}
    if proc is None:
        record.update(power=None, powerRaw=None, process=None, report=None, frameGate=None,
                       droppedFramesReported=None, discarded=True,
                       discardReason=f"launch failed after {attempts} attempt(s): {launch_reason}")
        return record

    results = {}

    def proc_worker():
        try:
            results["process"] = sampler.sample_process(player_pid, cfg.measure)
        except Exception as exc:
            results["processError"] = str(exc)

    t = threading.Thread(target=proc_worker)
    t.start()
    power = sampler.sample_power(cfg.measure)
    t.join()
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        print(f"    {backend}: player did not exit within 60s of the measurement window closing, killing")
        proc.kill()

    report, load_error = load_report(report_path)
    reasons, frame_gate = evaluate_run(power, report, load_error, results.get("processError"),
                                        backend, fixture, cfg)

    record.update({
        "power": {k: _baseline_subtract(k, power[k], baseline.get(k)) for k in power},
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settle", type=float, default=15.0)
    parser.add_argument("--measure", type=float, default=60.0)
    parser.add_argument("--cooldown", type=float, default=60.0)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-launch-attempts", type=int, default=DEFAULT_MAX_LAUNCH_ATTEMPTS)
    parser.add_argument("--gate-threshold", type=float, default=DEFAULT_GATE_THRESHOLD)
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
                          gate_threshold=args.gate_threshold)
    fixtures = [f for f in args.fixtures.split(",") if f]
    backends = [b for b in args.backends.split(",") if b]
    for f in fixtures:
        if not (ROOT / "Fixtures" / f).exists():
            print(f"warning: fixture does not exist on disk: {f} (runs against it will "
                  f"be recorded as discarded launch failures, not silently skipped)")

    report_dir = pathlib.Path("/tmp")
    launch_failures = {b: 0 for b in backends}
    records = []
    for fixture_index, fixture in enumerate(fixtures):
        print(f"=== {fixture}: idle baseline")
        baseline = sampler.sample_power(60)
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
                    measure_once(backend, fixture, -1, baseline, cfg, launch_failures, report_dir)
                    time.sleep(cfg.cooldown)
                print(f"  {backend}: repeat {repeat}")
                records.append(measure_once(backend, fixture, repeat, baseline, cfg, launch_failures, report_dir))
                time.sleep(cfg.cooldown)

    machine = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                              capture_output=True, text=True).stdout.strip().replace(" ", "-")
    versions = engine_versions()
    date = time.strftime("%Y-%m-%d")
    out_dir = pathlib.Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-dryrun" if args.dry_run else ""
    out = out_dir / f"{machine}-{date}{suffix}.json"
    payload = {
        "machine": machine, "date": date, "versions": versions,
        "os": subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True).stdout.strip(),
        "protocol": dataclasses.asdict(cfg),
        "launchFailures": launch_failures,
        "runs": records,
    }
    if args.dry_run:
        payload["dryRun"] = True
        payload["note"] = "shortened validation run (see --settle/--measure/--cooldown/--repeats), not published data"
    with open(out, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
