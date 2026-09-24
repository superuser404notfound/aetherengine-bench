#!/usr/bin/env python3
"""Unit tests for Scripts/orchestrate.py.

Run with: python3 Tests/orchestrate_test.py (works from any working
directory and against a clean checkout; no root, no live powermetrics, and
no real player binaries needed. Everything that touches a real subprocess,
a real launch, or a real thermal reading is exercised by the dry run and
the forced-discard demonstration instead, both described in the task-9
report, not here.)
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, "..", "Scripts"))
import orchestrate  # noqa: E402


def make_report(**overrides):
    report = {
        "backend": "aether",
        "fixture": "h264-1080p.mp4",
        "deliveredFrames": 1500,
        "droppedFrames": 0,
        "expectedFrames": 1500,
        "startedAt": "2026-08-15T12:00:00Z",
        "endedAt": "2026-08-15T12:01:00Z",
        "output": {"width": 1920, "height": 1080, "bitDepth": 8,
                    "colorTransfer": "1", "audioChannels": 2},
    }
    report.update(overrides)
    return report


class ParseIso8601Tests(unittest.TestCase):
    def test_parses_swift_and_shell_written_timestamps_identically(self):
        # JSONEncoder.bench's .iso8601 strategy and run-mpv.sh's
        # time.strftime both write "yyyy-MM-dd'T'HH:mm:ss'Z'", no fractional
        # seconds. Both must parse to the exact same instant.
        a = orchestrate.parse_iso8601("2026-08-15T12:00:00Z")
        b = orchestrate.parse_iso8601("2026-08-15T12:00:00Z")
        self.assertEqual(a, b)
        self.assertEqual(a.utcoffset().total_seconds(), 0)

    def test_elapsed_seconds_between_two_timestamps(self):
        started = orchestrate.parse_iso8601("2026-08-15T12:00:00Z")
        ended = orchestrate.parse_iso8601("2026-08-15T12:01:05Z")
        self.assertEqual((ended - started).total_seconds(), 65.0)


class ValidateReportTests(unittest.TestCase):
    def test_valid_report_passes(self):
        self.assertIsNone(orchestrate.validate_report(make_report(), "aether", "h264-1080p.mp4"))

    def test_dropped_frames_sentinel_does_not_fail_validation(self):
        # -1 means "not reported" (KSPlayer's KSAVPlayer path), not a
        # negative measurement; only deliveredFrames is checked for sign.
        report = make_report(droppedFrames=-1)
        self.assertIsNone(orchestrate.validate_report(report, "aether", "h264-1080p.mp4"))

    def test_empty_report_fails(self):
        self.assertIsNotNone(orchestrate.validate_report({}, "aether", "h264-1080p.mp4"))
        self.assertIsNotNone(orchestrate.validate_report(None, "aether", "h264-1080p.mp4"))

    def test_missing_field_fails(self):
        report = make_report()
        del report["expectedFrames"]
        reason = orchestrate.validate_report(report, "aether", "h264-1080p.mp4")
        self.assertIn("expectedFrames", reason)

    def test_backend_mismatch_fails(self):
        # Guards against a stale or misrouted report file being read as this
        # run's result.
        reason = orchestrate.validate_report(make_report(backend="vlckit"), "aether", "h264-1080p.mp4")
        self.assertIsNotNone(reason)
        self.assertIn("backend mismatch", reason)

    def test_fixture_mismatch_fails(self):
        reason = orchestrate.validate_report(make_report(fixture="vp9.webm"), "aether", "h264-1080p.mp4")
        self.assertIsNotNone(reason)
        self.assertIn("fixture mismatch", reason)

    def test_non_positive_expected_frames_fails(self):
        reason = orchestrate.validate_report(make_report(expectedFrames=0), "aether", "h264-1080p.mp4")
        self.assertIsNotNone(reason)
        self.assertIn("expectedFrames", reason)

    def test_negative_delivered_frames_fails(self):
        reason = orchestrate.validate_report(make_report(deliveredFrames=-5), "aether", "h264-1080p.mp4")
        self.assertIsNotNone(reason)
        self.assertIn("deliveredFrames", reason)

    def test_ended_not_after_started_fails(self):
        reason = orchestrate.validate_report(
            make_report(startedAt="2026-08-15T12:01:00Z", endedAt="2026-08-15T12:01:00Z"),
            "aether", "h264-1080p.mp4")
        self.assertIsNotNone(reason)
        self.assertIn("endedAt", reason)

    def test_unparseable_timestamp_fails_loudly_not_silently(self):
        reason = orchestrate.validate_report(make_report(startedAt="not-a-date"), "aether", "h264-1080p.mp4")
        self.assertIsNotNone(reason)

    def test_null_required_field_fails_instead_of_raising(self):
        # A bare `f not in report` check would let a present-but-null field
        # through to `report["expectedFrames"] <= 0` and raise a TypeError
        # uncaught. No writer produces null today; this is defense in depth.
        reason = orchestrate.validate_report(make_report(expectedFrames=None), "aether", "h264-1080p.mp4")
        self.assertIsNotNone(reason)
        self.assertIn("expectedFrames", reason)

    def test_wrong_type_field_fails_instead_of_raising(self):
        # A field of the wrong type (string where a number is expected)
        # must become a discard reason too, not an uncaught TypeError.
        reason = orchestrate.validate_report(make_report(expectedFrames="120"), "aether", "h264-1080p.mp4")
        self.assertIsNotNone(reason)


class ComputeFrameGateTests(unittest.TestCase):
    def test_exact_match_passes(self):
        # 1500 delivered over exactly a 60s window at 25fps (measure=60).
        report = make_report(expectedFrames=1500, deliveredFrames=1500,
                              startedAt="2026-08-15T12:00:00Z", endedAt="2026-08-15T12:01:00Z")
        gate = orchestrate.compute_frame_gate(report, nominal_measure_seconds=60, threshold=0.95)
        self.assertAlmostEqual(gate["expectedFramesReal"], 1500.0)
        self.assertAlmostEqual(gate["ratio"], 1.0)
        self.assertTrue(gate["passed"])

    def test_overshoot_window_raises_recomputed_expectation_above_reported(self):
        # Task.sleep is a lower bound: a 61s real window against a nominal
        # 60s measure means the recomputed expectation is higher than what
        # the binary itself reported, which is exactly why the recomputed
        # value, not the reported one, is what the gate is evaluated against.
        report = make_report(expectedFrames=1500, deliveredFrames=1520,
                              startedAt="2026-08-15T12:00:00Z", endedAt="2026-08-15T12:01:01Z")
        gate = orchestrate.compute_frame_gate(report, nominal_measure_seconds=60, threshold=0.95)
        self.assertGreater(gate["expectedFramesReal"], report["expectedFrames"])
        self.assertEqual(gate["expectedFramesReported"], 1500)

    def test_ratio_in_observed_healthy_band_passes(self):
        # Measured deltas in this project land at 100-101% of the recomputed
        # expectation; both ends of that band must pass at the chosen
        # threshold.
        report = make_report(expectedFrames=1500, deliveredFrames=1500,
                              startedAt="2026-08-15T12:00:00Z", endedAt="2026-08-15T12:01:00Z")
        gate_100 = orchestrate.compute_frame_gate(report, 60, orchestrate.DEFAULT_GATE_THRESHOLD)
        self.assertTrue(gate_100["passed"])
        report_101 = make_report(expectedFrames=1500, deliveredFrames=1515,
                                  startedAt="2026-08-15T12:00:00Z", endedAt="2026-08-15T12:01:00Z")
        gate_101 = orchestrate.compute_frame_gate(report_101, 60, orchestrate.DEFAULT_GATE_THRESHOLD)
        self.assertTrue(gate_101["passed"])

    def test_starved_run_fails_gate(self):
        report = make_report(expectedFrames=1500, deliveredFrames=900,
                              startedAt="2026-08-15T12:00:00Z", endedAt="2026-08-15T12:01:00Z")
        gate = orchestrate.compute_frame_gate(report, 60, orchestrate.DEFAULT_GATE_THRESHOLD)
        self.assertFalse(gate["passed"])
        self.assertLess(gate["ratio"], orchestrate.DEFAULT_GATE_THRESHOLD)


# 500 mW is a plausible reading for a *measured run* under active decode
# load (EvaluateRunTests below uses these as the run's own power, never as
# a baseline candidate). Baseline-specific readings live near
# TakeCleanBaselineTests instead: a baseline is subject to a much lower
# magnitude ceiling (MAX_BASELINE_CPU_MW=250) that this value would fail.
HEALTHY_POWER = {"cpuPowerMw": 500.0, "gpuPowerMw": 100.0, "anePowerMw": 0.0,
                  "eClusterResidency": 30.0, "pClusterResidency": 10.0,
                  "thermalPressure": "Nominal", "throttled": False, "samples": 60}
THROTTLED_POWER = dict(HEALTHY_POWER, thermalPressure="Heavy", throttled=True)


class EvaluateRunTests(unittest.TestCase):
    def test_healthy_run_has_no_reasons(self):
        reasons, gate = orchestrate.evaluate_run(
            HEALTHY_POWER, make_report(), None, None, "aether", "h264-1080p.mp4",
            orchestrate.ProtocolConfig())
        self.assertEqual(reasons, [])
        self.assertTrue(gate["passed"])

    def test_throttling_alone_is_reported(self):
        reasons, gate = orchestrate.evaluate_run(
            THROTTLED_POWER, make_report(), None, None, "aether", "h264-1080p.mp4",
            orchestrate.ProtocolConfig())
        self.assertEqual(len(reasons), 1)
        self.assertIn("throttling", reasons[0])
        # The frame gate is independently healthy; throttling does not
        # suppress it from being computed and reported.
        self.assertTrue(gate["passed"])

    def test_throttling_and_failing_frame_gate_both_survive(self):
        # This is the bug this function exists to prevent: the original
        # brief pseudocode set discardReason twice, with the second check
        # silently overwriting the first if both applied.
        starved = make_report(expectedFrames=1500, deliveredFrames=100,
                               startedAt="2026-08-15T12:00:00Z", endedAt="2026-08-15T12:01:00Z")
        reasons, gate = orchestrate.evaluate_run(
            THROTTLED_POWER, starved, None, None, "aether", "h264-1080p.mp4",
            orchestrate.ProtocolConfig(measure=60))
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any("throttling" in r for r in reasons))
        self.assertTrue(any("frame gate" in r for r in reasons))
        self.assertFalse(gate["passed"])

    def test_missing_report_is_reported_and_skips_frame_gate(self):
        reasons, gate = orchestrate.evaluate_run(
            HEALTHY_POWER, None, "no report file produced", None, "aether", "h264-1080p.mp4",
            orchestrate.ProtocolConfig())
        self.assertEqual(len(reasons), 1)
        self.assertIn("report problem", reasons[0])
        self.assertIsNone(gate)

    def test_invalid_report_is_reported_and_skips_frame_gate(self):
        # e.g. a backend/fixture mismatch caught by validate_report.
        reasons, gate = orchestrate.evaluate_run(
            HEALTHY_POWER, make_report(backend="vlckit"), None, None,
            "aether", "h264-1080p.mp4", orchestrate.ProtocolConfig())
        self.assertEqual(len(reasons), 1)
        self.assertIn("report problem", reasons[0])
        self.assertIsNone(gate)

    def test_unreported_thermal_pressure_is_not_treated_as_cool(self):
        # sampler.py's own documented gotcha: throttled=False is ambiguous
        # between "cool" and "no thermal reading at all"; thermalPressure
        # is the only field that distinguishes them, so a None here must
        # not be published as a silently-clean run.
        unknown_thermal_power = dict(HEALTHY_POWER, thermalPressure=None, throttled=False)
        reasons, gate = orchestrate.evaluate_run(
            unknown_thermal_power, make_report(), None, None, "aether", "h264-1080p.mp4",
            orchestrate.ProtocolConfig())
        self.assertEqual(len(reasons), 1)
        self.assertIn("thermal pressure was never reported", reasons[0])
        self.assertTrue(gate["passed"])  # independent of the thermal gap

    def test_process_sampling_failure_combines_with_other_reasons(self):
        reasons, gate = orchestrate.evaluate_run(
            THROTTLED_POWER, make_report(), None, "pid 123 stopped responding",
            "aether", "h264-1080p.mp4", orchestrate.ProtocolConfig())
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any("throttling" in r for r in reasons))
        self.assertTrue(any("process sampling failed" in r for r in reasons))

    def test_unclean_baseline_is_reported_and_combines_with_other_reasons(self):
        reasons, gate = orchestrate.evaluate_run(
            HEALTHY_POWER, make_report(), None, None, "aether", "h264-1080p.mp4",
            orchestrate.ProtocolConfig(),
            baseline_error="machine reported throttling (level=Heavy) while idle")
        self.assertEqual(len(reasons), 1)
        self.assertIn("idle baseline was not clean", reasons[0])
        # The frame gate itself is unaffected: this run's own delivery was
        # fine, only the power figure derived from it is untrustworthy.
        self.assertTrue(gate["passed"])

    def test_mid_window_crash_reports_a_missing_report_alongside_the_process_failure(self):
        # task-11: VLCKit's own known startup instability (a real crash
        # mid-playback, four times in one session, once at 30 of 60
        # samples on hevc-4k-hdr10.mp4). The process dies before ever
        # writing a report, so load_report's own "no report file produced"
        # reaches here as load_error alongside sample_process's failure:
        # two reasons, not one, and the "report problem" one is what marks
        # this as a genuine crash rather than the sampler-lag timing
        # defect (see the next test) that this task actually fixes.
        reasons, gate = orchestrate.evaluate_run(
            HEALTHY_POWER, None,
            "no report file produced (player exited without writing one)",
            "pid 1234 stopped responding after 30 of 60 sample(s) needed, before the "
            "60.0s window elapsed (process does not exist (exited, or never started))",
            "vlckit", "hevc-4k-hdr10.mp4", orchestrate.ProtocolConfig())
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any("report problem" in r for r in reasons))
        self.assertTrue(any("process sampling failed" in r for r in reasons))
        self.assertIsNone(gate)  # no report at all, nothing to gate on

    def test_timing_defect_reports_only_the_process_failure_no_report_problem(self):
        # task-11: the exact live symptom this task fixes ("pid 6420
        # stopped responding after 59 of 60 sample(s) needed"). The player
        # wrote a full, valid report and would have exited cleanly; only
        # the sampler's own last read raced it. No "report problem" reason
        # here is what keeps this distinct, in the published discard
        # reason, from a genuine mid-window crash like the one above: this
        # run's engine behaved correctly, only the measurement infra did
        # not keep up.
        reasons, gate = orchestrate.evaluate_run(
            HEALTHY_POWER, make_report(backend="avplayer", fixture="h264-1080p.mp4"), None,
            "pid 6420 stopped responding after 59 of 60 sample(s) needed, before the "
            "60.0s window elapsed (process does not exist (exited, or never started))",
            "avplayer", "h264-1080p.mp4", orchestrate.ProtocolConfig())
        self.assertEqual(len(reasons), 1)
        self.assertNotIn("report problem", reasons[0])
        self.assertIn("process sampling failed", reasons[0])
        # A valid, gate-passing report is still on hand: this is what
        # keeps a run like this recorded as a real (if discarded) engine
        # result rather than indistinguishable from an actual crash.
        self.assertIsNotNone(gate)
        self.assertTrue(gate["passed"])


class DroppedFramesReportedTests(unittest.TestCase):
    def test_sentinel_is_false_not_a_number(self):
        self.assertFalse(orchestrate.dropped_frames_reported(make_report(droppedFrames=-1)))

    def test_real_zero_is_true(self):
        self.assertTrue(orchestrate.dropped_frames_reported(make_report(droppedFrames=0)))

    def test_no_report_is_none(self):
        self.assertIsNone(orchestrate.dropped_frames_reported(None))
        self.assertIsNone(orchestrate.dropped_frames_reported({}))


class BaselineSubtractTests(unittest.TestCase):
    def test_power_field_is_subtracted(self):
        self.assertEqual(orchestrate._baseline_subtract("cpuPowerMw", 500.0, 120.0), 380.0)

    def test_non_power_field_passes_through(self):
        self.assertEqual(orchestrate._baseline_subtract("eClusterResidency", 42.0, 10.0), 42.0)
        self.assertEqual(orchestrate._baseline_subtract("throttled", False, True), False)

    def test_none_propagates_instead_of_crashing_or_becoming_zero(self):
        self.assertIsNone(orchestrate._baseline_subtract("anePowerMw", None, 5.0))
        self.assertIsNone(orchestrate._baseline_subtract("anePowerMw", 5.0, None))


class NegativePowerReasonTests(unittest.TestCase):
    def test_clean_subtraction_has_no_reason(self):
        power = {"cpuPowerMw": 300.0}
        baseline = {"cpuPowerMw": 150.0}
        subtracted = {"cpuPowerMw": 150.0}
        self.assertIsNone(orchestrate.negative_power_reason(power, baseline, subtracted))

    def test_negative_field_names_the_baseline_and_raw_reading(self):
        # The exact live-observed defect: raw 118.2 mW, baseline 530.5 mW.
        power = {"cpuPowerMw": 118.2}
        baseline = {"cpuPowerMw": 530.5}
        subtracted = {"cpuPowerMw": -412.3}
        reason = orchestrate.negative_power_reason(power, baseline, subtracted)
        self.assertIsNotNone(reason)
        self.assertIn("cpuPowerMw", reason)
        self.assertIn("118.2", reason)
        self.assertIn("530.5", reason)
        self.assertIn("-412.3", reason)

    def test_none_values_are_not_flagged_as_negative(self):
        power = {"anePowerMw": None}
        baseline = {"anePowerMw": None}
        subtracted = {"anePowerMw": None}
        self.assertIsNone(orchestrate.negative_power_reason(power, baseline, subtracted))

    def test_non_power_fields_are_never_checked(self):
        # eClusterResidency etc. can be negative-looking in synthetic data
        # without meaning anything physically impossible; only *Mw fields
        # are power at all.
        power = {"eClusterResidency": 10.0}
        baseline = {"eClusterResidency": 50.0}
        subtracted = {"eClusterResidency": -40.0}
        self.assertIsNone(orchestrate.negative_power_reason(power, baseline, subtracted))

    def test_multiple_negative_fields_are_all_named(self):
        power = {"cpuPowerMw": 100.0, "gpuPowerMw": 50.0}
        baseline = {"cpuPowerMw": 300.0, "gpuPowerMw": 200.0}
        subtracted = {"cpuPowerMw": -200.0, "gpuPowerMw": -150.0}
        reason = orchestrate.negative_power_reason(power, baseline, subtracted)
        self.assertIn("cpuPowerMw", reason)
        self.assertIn("gpuPowerMw", reason)


class FindPlayerPidTests(unittest.TestCase):
    def test_direct_child_binary(self):
        # sudo -u user AetherBench ...: one hop below the sudo monitor pid.
        table = [(100, 1, "launchd"), (200, 100, "sudo"), (201, 200, "AetherBench")]
        self.assertEqual(orchestrate.find_player_pid(200, "AetherBench", lambda: table), 201)

    def test_player_is_the_popen_pid_without_a_sudo_wrapper(self):
        # A non-root session launches the binary directly, so the pid Popen
        # hands back is the player itself, not a parent of it.
        table = [(100, 1, "launchd"), (400, 100, "/path/to/AetherBench"), (401, 400, "VTDecoderXPCService")]
        self.assertEqual(orchestrate.find_player_pid(400, "AetherBench", lambda: table), 400)

    def test_mpv_two_hops_below_sudo(self):
        # sudo -u user run-mpv.sh: sudo -> bash (running the script) -> mpv,
        # backgrounded with `&` inside the script. Verified empirically that
        # sudo forks a monitor rather than exec-replacing itself.
        table = [(300, 1, "sudo"), (301, 300, "bash"), (302, 301, "mpv"), (303, 301, "sleep")]
        self.assertEqual(orchestrate.find_player_pid(300, "mpv", lambda: table), 302)

    def test_not_found_returns_none(self):
        table = [(400, 1, "sudo"), (401, 400, "bash")]
        self.assertIsNone(orchestrate.find_player_pid(400, "mpv", lambda: table))

    def test_does_not_match_the_sudo_wrapper_itself(self):
        # The bug this exists to prevent: sampling proc.pid directly reads
        # the sudo monitor's own near-idle stats, not the engine's.
        table = [(500, 1, "sudo"), (501, 500, "VLCBench")]
        pid = orchestrate.find_player_pid(500, "VLCBench", lambda: table)
        self.assertNotEqual(pid, 500)
        self.assertEqual(pid, 501)


class WaitForPlayerPidTests(unittest.TestCase):
    def test_resolves_once_the_table_catches_up(self):
        calls = {"n": 0}

        def table_fn():
            calls["n"] += 1
            if calls["n"] < 3:
                return [(600, 1, "sudo")]  # player hasn't forked yet
            return [(600, 1, "sudo"), (601, 600, "AVBench")]

        clock = {"t": 0.0}
        pid = orchestrate.wait_for_player_pid(
            600, "AVBench", deadline=100.0,
            table_fn=table_fn, sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
            now_fn=lambda: clock["t"])
        self.assertEqual(pid, 601)

    def test_gives_up_at_deadline(self):
        clock = {"t": 0.0}
        pid = orchestrate.wait_for_player_pid(
            700, "AVBench", deadline=1.0,
            table_fn=lambda: [(700, 1, "sudo")],
            sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
            now_fn=lambda: clock["t"])
        self.assertIsNone(pid)


class FakeProc:
    def __init__(self, pid, exit_after=None, exit_code=1):
        self.pid = pid
        self.returncode = None
        self._exit_after = exit_after
        self._exit_code = exit_code
        self._polls = 0

    def poll(self):
        self._polls += 1
        if self._exit_after is not None and self._polls >= self._exit_after:
            self.returncode = self._exit_code
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class SpawnAndSettleTests(unittest.TestCase):
    def test_crash_during_settle_is_reported_not_silently_retried(self):
        proc = FakeProc(pid=800, exit_after=1)
        clock = {"t": 0.0}
        pid, reason, refused = orchestrate.spawn_and_settle(
            proc, "AetherBench", settle_seconds=5,
            table_fn=lambda: [], sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
            now_fn=lambda: clock["t"])
        self.assertIsNone(pid)
        self.assertIn("crashed during settle", reason)
        self.assertFalse(refused)

    def test_refusal_exit_code_is_reported_and_flagged_distinctly(self):
        # exit_code proves spawn_and_settle actually branches on the exact
        # value, not just "any nonzero code is a crash".
        proc = FakeProc(pid=850, exit_after=1, exit_code=orchestrate.REFUSAL_EXIT_CODE)
        clock = {"t": 0.0}
        pid, reason, refused = orchestrate.spawn_and_settle(
            proc, "AVBench", settle_seconds=5,
            table_fn=lambda: [], sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
            now_fn=lambda: clock["t"])
        self.assertIsNone(pid)
        self.assertIn("refused this source", reason)
        self.assertNotIn("crashed", reason)
        self.assertTrue(refused)

    def test_never_forks_a_recognizable_player_is_reported(self):
        proc = FakeProc(pid=900)  # stays "alive" (poll() -> None) forever
        clock = {"t": 0.0}
        pid, reason, refused = orchestrate.spawn_and_settle(
            proc, "AetherBench", settle_seconds=2, extra_grace=1,
            table_fn=lambda: [(900, 1, "sudo")],  # never gains a child
            sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
            now_fn=lambda: clock["t"])
        self.assertIsNone(pid)
        self.assertIn("never produced a recognizable player process", reason)
        self.assertFalse(refused)

    def test_healthy_launch_resolves_the_player_pid(self):
        proc = FakeProc(pid=1000)
        clock = {"t": 0.0}
        table = [(1000, 1, "sudo"), (1001, 1000, "AetherBench")]
        pid, reason, refused = orchestrate.spawn_and_settle(
            proc, "AetherBench", settle_seconds=2,
            table_fn=lambda: table,
            sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
            now_fn=lambda: clock["t"])
        self.assertEqual(pid, 1001)
        self.assertIsNone(reason)
        self.assertFalse(refused)


class LaunchWithRetryTests(unittest.TestCase):
    def test_crash_then_success_counts_exactly_one_failure(self):
        fake_proc = FakeProc(pid=1)
        outcomes = [(None, "crashed during settle (exit 1)", False), (42, None, False)]
        with mock.patch.object(orchestrate, "launch", return_value=fake_proc) as launch_mock, \
             mock.patch.object(orchestrate, "spawn_and_settle", side_effect=outcomes), \
             mock.patch("time.sleep"):
            failures = {"aether": {"h264-1080p.mp4": 0}}
            proc, pid, attempts, report_path, reason, refused = orchestrate.launch_with_retry(
                "aether", "h264-1080p.mp4", "/fake/full/path/h264-1080p.mp4",
                lambda attempt: f"/tmp/fake-{attempt}.json",
                orchestrate.ProtocolConfig(max_launch_attempts=5), failures)
        self.assertEqual(attempts, 2)
        self.assertEqual(failures["aether"]["h264-1080p.mp4"], 1)
        self.assertEqual(pid, 42)
        self.assertIsNotNone(proc)
        self.assertFalse(refused)
        # Regression guard: launch() must receive the resolved fixture_path
        # (Fixtures/<name>, an absolute path), never the bare fixture name.
        # A bare name resolves against the player's own cwd, not Fixtures/,
        # and every backend fails to load it: caught only by an end-to-end
        # run, since a same-value fake_proc doesn't care what args it saw
        # unless the call is asserted here too.
        for call in launch_mock.call_args_list:
            self.assertEqual(call.args[1], "/fake/full/path/h264-1080p.mp4")

    def test_exhausting_all_attempts_is_reported_not_silently_dropped(self):
        fake_proc = FakeProc(pid=1)
        with mock.patch.object(orchestrate, "launch", return_value=fake_proc), \
             mock.patch.object(orchestrate, "spawn_and_settle",
                                return_value=(None, "crashed during settle (exit 1)", False)), \
             mock.patch("time.sleep"):
            failures = {"vlckit": {"vp9.webm": 0}}
            proc, pid, attempts, report_path, reason, refused = orchestrate.launch_with_retry(
                "vlckit", "vp9.webm", "/fake/vp9.webm",
                lambda attempt: f"/tmp/fake-{attempt}.json",
                orchestrate.ProtocolConfig(max_launch_attempts=3), failures)
        self.assertIsNone(proc)
        self.assertIsNone(pid)
        self.assertEqual(attempts, 3)
        self.assertEqual(failures["vlckit"]["vp9.webm"], 3)
        self.assertIn("crashed during settle", reason)
        self.assertFalse(refused)

    def test_failures_are_keyed_per_fixture_not_pooled_session_wide(self):
        # A renderer showing one session-wide count underneath every
        # fixture's table would re-bill every fixture an engine did not
        # fail on with failures that actually all happened elsewhere. The
        # source of truth has to distinguish them: two failing launches
        # against one fixture must not be visible under a different one.
        fake_proc = FakeProc(pid=1)
        failures = {"ksplayer": {"av1-10bit.mkv": 0, "hevc-4k-hdr10.mp4": 0}}
        with mock.patch.object(orchestrate, "launch", return_value=fake_proc), \
             mock.patch.object(orchestrate, "spawn_and_settle",
                                return_value=(None, "crashed during settle (exit 1)", False)), \
             mock.patch("time.sleep"):
            orchestrate.launch_with_retry(
                "ksplayer", "av1-10bit.mkv", "/fake/av1-10bit.mkv",
                lambda attempt: f"/tmp/fake-{attempt}.json",
                orchestrate.ProtocolConfig(max_launch_attempts=3), failures)
        self.assertEqual(failures["ksplayer"]["av1-10bit.mkv"], 3)
        self.assertEqual(failures["ksplayer"]["hevc-4k-hdr10.mp4"], 0)

    def test_refusal_is_recorded_once_and_never_retried(self):
        fake_proc = FakeProc(pid=1)
        with mock.patch.object(orchestrate, "launch", return_value=fake_proc) as launch_mock, \
             mock.patch.object(orchestrate, "spawn_and_settle",
                                return_value=(None, "refused this source (exit 3): "
                                                     "This media format is not supported.", True)), \
             mock.patch("time.sleep"):
            failures = {"avplayer": {"av1-10bit.mkv": 0}}
            proc, pid, attempts, report_path, reason, refused = orchestrate.launch_with_retry(
                "avplayer", "av1-10bit.mkv", "/fake/av1-10bit.mkv",
                lambda attempt: f"/tmp/fake-{attempt}.json",
                orchestrate.ProtocolConfig(max_launch_attempts=5), failures)
        self.assertIsNone(proc)
        self.assertIsNone(pid)
        self.assertEqual(attempts, 1)  # never retried
        self.assertTrue(refused)
        self.assertIn("This media format is not supported", reason)
        # A deterministic refusal is not a launch-reliability failure: it
        # must not be counted alongside genuine crashes, or an engine that
        # correctly and honestly declines a format it cannot play would
        # look less reliable than one that silently mispublishes a result.
        self.assertEqual(failures["avplayer"]["av1-10bit.mkv"], 0)
        self.assertEqual(launch_mock.call_count, 1)


class MeasureOnceGuardedSamplingTests(unittest.TestCase):
    """measure_once is the function the critical review finding was about:
    sample_power is documented to raise, and it was called unguarded here.
    These drive measure_once itself (not just the pieces it's built from)
    to prove a raise becomes a discarded record, not a propagated
    exception, and that the player is always terminated."""

    def test_power_sampling_failure_is_a_discarded_record_not_a_raise(self):
        fake_proc = FakeProc(pid=1)
        with mock.patch.object(orchestrate, "launch_with_retry",
                                return_value=(fake_proc, 999, 1, "/tmp/fake.json", None, False)), \
             mock.patch.object(orchestrate.sampler, "sample_power", side_effect=RuntimeError("powermetrics hung")), \
             mock.patch.object(orchestrate.sampler, "sample_process", return_value={"cpuPercentMean": 1.0}):
            record = orchestrate.measure_once("aether", "h264-1080p.mp4", 0, {"cpuPowerMw": 100.0},
                                               orchestrate.ProtocolConfig(), {"aether": 0}, pathlib.Path("/tmp"))
        self.assertTrue(record["discarded"])
        self.assertIn("power sampling failed", record["discardReason"])
        self.assertIsNone(record["power"])
        self.assertIsNone(record["report"])
        self.assertFalse(record["refused"])

    def test_power_sampling_failure_still_terminates_the_player(self):
        fake_proc = FakeProc(pid=1)
        with mock.patch.object(orchestrate, "launch_with_retry",
                                return_value=(fake_proc, 999, 1, "/tmp/fake.json", None, False)), \
             mock.patch.object(orchestrate.sampler, "sample_power", side_effect=RuntimeError("powermetrics hung")), \
             mock.patch.object(orchestrate.sampler, "sample_process", return_value={"cpuPercentMean": 1.0}), \
             mock.patch.object(orchestrate, "_terminate_player") as terminate_mock:
            orchestrate.measure_once("aether", "h264-1080p.mp4", 0, {"cpuPowerMw": 100.0},
                                      orchestrate.ProtocolConfig(), {"aether": 0}, pathlib.Path("/tmp"))
        terminate_mock.assert_called_once()
        self.assertEqual(terminate_mock.call_args.args[0], fake_proc)

    def test_healthy_path_still_terminates_the_player_with_the_normal_grace_period(self):
        fake_proc = FakeProc(pid=1)
        with mock.patch.object(orchestrate, "launch_with_retry",
                                return_value=(fake_proc, 999, 1, "/tmp/does-not-exist.json", None, False)), \
             mock.patch.object(orchestrate.sampler, "sample_power", return_value=dict(HEALTHY_POWER)), \
             mock.patch.object(orchestrate.sampler, "sample_process", return_value={"cpuPercentMean": 1.0}), \
             mock.patch.object(orchestrate, "_terminate_player") as terminate_mock:
            record = orchestrate.measure_once("aether", "h264-1080p.mp4", 0, {"cpuPowerMw": 100.0},
                                               orchestrate.ProtocolConfig(), {"aether": 0}, pathlib.Path("/tmp"))
        # task-11: short and bounded, not the player's own DEFAULT_LINGER_SECONDS.
        # The player is known to still be alive here by design (it lingers on
        # purpose) with its report already on disk, so there is nothing to wait
        # out; waiting the full linger here instead of signalling the player
        # would silently reintroduce, per run, the wall-clock cost the linger
        # fix exists to avoid.
        terminate_mock.assert_called_once_with(fake_proc, "aether", wait_timeout=orchestrate.TERMINATE_GRACE_SECONDS)
        # No report file existed at that path, so this record is discarded
        # for that reason, but the player is still cleanly terminated.
        self.assertTrue(record["discarded"])

    def test_baseline_error_zeroes_subtracted_power_but_keeps_raw(self):
        fake_proc = FakeProc(pid=1)
        with mock.patch.object(orchestrate, "launch_with_retry",
                                return_value=(fake_proc, 999, 1, "/tmp/does-not-exist.json", None, False)), \
             mock.patch.object(orchestrate.sampler, "sample_power", return_value=dict(HEALTHY_POWER)), \
             mock.patch.object(orchestrate.sampler, "sample_process", return_value={"cpuPercentMean": 1.0}), \
             mock.patch.object(orchestrate, "_terminate_player"):
            record = orchestrate.measure_once("aether", "h264-1080p.mp4", 0, {"cpuPowerMw": 100.0},
                                               orchestrate.ProtocolConfig(), {"aether": 0}, pathlib.Path("/tmp"),
                                               baseline_error="machine reported throttling while idle")
        self.assertIsNone(record["power"]["cpuPowerMw"])
        self.assertEqual(record["powerRaw"]["cpuPowerMw"], HEALTHY_POWER["cpuPowerMw"])
        self.assertIn("idle baseline was not clean", record["discardReason"])

    def test_negative_power_after_subtraction_is_discarded_with_baseline_named(self):
        # The exact defect this exists for: an unclean baseline (530.5 mW)
        # subtracted from a genuinely lower raw reading (118.2 mW) produces
        # a physically impossible negative number. take_clean_baseline's
        # own magnitude/stability checks are meant to catch this upstream,
        # but this is the last line of defense if a bad baseline still
        # reaches here (e.g. a smaller max_attempts, a borderline reading).
        fake_proc = FakeProc(pid=1)
        low_raw_power = dict(HEALTHY_POWER, cpuPowerMw=118.2)
        bad_baseline = {"cpuPowerMw": 530.5}
        with mock.patch.object(orchestrate, "launch_with_retry",
                                return_value=(fake_proc, 999, 1, "/tmp/does-not-exist.json", None, False)), \
             mock.patch.object(orchestrate.sampler, "sample_power", return_value=low_raw_power), \
             mock.patch.object(orchestrate.sampler, "sample_process", return_value={"cpuPercentMean": 1.0}), \
             mock.patch.object(orchestrate, "_terminate_player"):
            record = orchestrate.measure_once("aether", "h264-1080p.mp4", 0, bad_baseline,
                                               orchestrate.ProtocolConfig(), {"aether": 0}, pathlib.Path("/tmp"))
        self.assertTrue(record["discarded"])
        self.assertIn("physically impossible negative power", record["discardReason"])
        self.assertIn("530.5", record["discardReason"])
        self.assertLess(record["power"]["cpuPowerMw"], 0)  # not hidden, just flagged

    def test_refused_source_is_discarded_once_with_the_engine_named(self):
        with mock.patch.object(orchestrate, "launch_with_retry",
                                return_value=(None, None, 1, None,
                                               "refused this source (exit 3): "
                                               "This media format is not supported.", True)):
            record = orchestrate.measure_once("avplayer", "av1-10bit.mkv", 0, {"cpuPowerMw": 100.0},
                                               orchestrate.ProtocolConfig(), {"avplayer": {}}, pathlib.Path("/tmp"))
        self.assertTrue(record["discarded"])
        self.assertTrue(record["refused"])
        self.assertIn("avplayer", record["discardReason"])
        self.assertIn("refused this source", record["discardReason"])
        self.assertIn("This media format is not supported", record["discardReason"])
        self.assertEqual(record["launchAttempts"], 1)
        self.assertIsNone(record["process"])


class LaunchTests(unittest.TestCase):
    """task-11: cfg.linger has to actually reach the command line for
    every backend (that is the whole fix), the four Swift binaries via
    --linger and mpv via run-mpv.sh's fifth positional argument (its own
    flag-free argument style, see launch()'s own docstring)."""

    def test_linger_is_passed_to_a_swift_binary(self):
        with mock.patch.object(orchestrate, "demote", return_value=["sudo", "-u", "someone"]), \
             mock.patch("subprocess.Popen") as popen_mock, \
             mock.patch("builtins.open", mock.mock_open()):
            orchestrate.launch("aether", "/fake/x.mp4", "/tmp/out.json",
                                orchestrate.ProtocolConfig(linger=7.5), "/tmp/out.json.stderr.log")
        args = popen_mock.call_args.args[0]
        self.assertIn("--linger", args)
        self.assertEqual(args[args.index("--linger") + 1], "7.5")

    def test_default_protocol_config_lingers_by_default(self):
        # The orchestrator must not silently opt out of the fix: a caller
        # that does not think to pass --linger still gets
        # DEFAULT_LINGER_SECONDS, not 0 (BenchArguments' own default,
        # which exists only to keep direct/manual binary invocations
        # backward compatible, not to be the orchestrator's default too).
        with mock.patch.object(orchestrate, "demote", return_value=["sudo", "-u", "someone"]), \
             mock.patch("subprocess.Popen") as popen_mock, \
             mock.patch("builtins.open", mock.mock_open()):
            orchestrate.launch("aether", "/fake/x.mp4", "/tmp/out.json",
                                orchestrate.ProtocolConfig(), "/tmp/out.json.stderr.log")
        args = popen_mock.call_args.args[0]
        self.assertEqual(args[args.index("--linger") + 1], str(orchestrate.DEFAULT_LINGER_SECONDS))

    def test_linger_reaches_mpv_as_its_fifth_positional_argument(self):
        # run-mpv.sh has no --linger flag (it takes plain positional
        # arguments throughout: file, settle, measure, report), so cfg.linger
        # has to land as the fifth positional, right after report_path, or
        # mpv keeps racing the sampler exactly as the Swift binaries did.
        with mock.patch.object(orchestrate, "demote", return_value=["sudo", "-u", "someone"]), \
             mock.patch("subprocess.Popen") as popen_mock, \
             mock.patch("builtins.open", mock.mock_open()):
            orchestrate.launch("mpv", "/fake/x.mp4", "/tmp/out.json",
                                orchestrate.ProtocolConfig(linger=7.5), "/tmp/out.json.stderr.log")
        args = popen_mock.call_args.args[0]
        # run-mpv.sh <file> <settle> <measure> <report> <linger> <window_px>.
        # Asserted by position from the script path forward rather than from
        # the end, so appending a further argument cannot quietly move linger
        # into another slot without this failing.
        script_index = next(i for i, a in enumerate(args) if a.endswith("run-mpv.sh"))
        positional = args[script_index + 1:]
        self.assertEqual(positional[3], "/tmp/out.json")
        self.assertEqual(positional[4], "7.5")
        self.assertEqual(positional[5], orchestrate.WINDOW_PIXELS)

    def test_default_protocol_config_lingers_mpv_by_default_too(self):
        with mock.patch.object(orchestrate, "demote", return_value=["sudo", "-u", "someone"]), \
             mock.patch("subprocess.Popen") as popen_mock, \
             mock.patch("builtins.open", mock.mock_open()):
            orchestrate.launch("mpv", "/fake/x.mp4", "/tmp/out.json",
                                orchestrate.ProtocolConfig(), "/tmp/out.json.stderr.log")
        args = popen_mock.call_args.args[0]
        script_index = next(i for i, a in enumerate(args) if a.endswith("run-mpv.sh"))
        self.assertEqual(args[script_index + 5], str(orchestrate.DEFAULT_LINGER_SECONDS))


class DemoteTests(unittest.TestCase):
    def test_requires_sudo_user_rather_than_falling_back_to_user(self):
        # Under sudo, $USER is not reliably the invoking user (it can read
        # "root"); silently falling back to it would demote to root and
        # defeat the whole point.
        with mock.patch.object(orchestrate.os, "geteuid", return_value=0), \
                mock.patch.dict(os.environ, {"USER": "root"}, clear=False):
            os.environ.pop("SUDO_USER", None)
            with self.assertRaises(SystemExit):
                orchestrate.demote()

    def test_uses_sudo_user_when_present(self):
        with mock.patch.object(orchestrate.os, "geteuid", return_value=0), \
                mock.patch.dict(os.environ, {"SUDO_USER": "vincentherbst"}):
            self.assertEqual(orchestrate.demote(), ["sudo", "-u", "vincentherbst"])

    def test_non_root_session_needs_no_demotion(self):
        # Run as the invoking user (powermetrics grant present), the player
        # already runs as that user; a sudo -u here would prompt for a
        # password nobody is there to type.
        with mock.patch.object(orchestrate.os, "geteuid", return_value=501):
            self.assertEqual(orchestrate.demote(), [])


class CellKeyTests(unittest.TestCase):
    def test_file_arm_keeps_the_bare_fixture_name(self):
        # Sessions recorded before arms existed have no "arm" at all; their
        # launch-failure keys and cells must read exactly as before.
        self.assertEqual(orchestrate.cell_key("hevc-4k-hdr10.mkv", "file"), "hevc-4k-hdr10.mkv")

    def test_http_arm_is_its_own_cell(self):
        self.assertEqual(orchestrate.cell_key("hevc-4k-hdr10.mkv", "http"), "hevc-4k-hdr10.mkv@http")


def make_fake_clock():
    clock = {"t": 0.0}
    return clock, (lambda: clock["t"]), (lambda s: clock.__setitem__("t", clock["t"] + s))


class TerminatePlayerTests(unittest.TestCase):
    def test_already_exited_is_a_no_op(self):
        proc = FakeProc(pid=1, exit_after=1)
        proc.poll()  # make it "exited" before _terminate_player is called
        orchestrate._terminate_player(proc, "aether", wait_timeout=1)
        self.assertNotEqual(proc.returncode, -15)  # never escalated to terminate()

    def test_exits_gracefully_within_wait_timeout(self):
        # exit_after=1 makes the first poll() (inside _terminate_player)
        # observe the process as already exited: no escalation to
        # terminate()/kill() should happen.
        proc = FakeProc(pid=2, exit_after=1)
        orchestrate._terminate_player(proc, "aether", wait_timeout=1)
        self.assertEqual(proc.returncode, 1)  # exit_after's own code, never -15/-9

    def test_escalates_to_terminate_when_wait_times_out(self):
        class HangingProc(FakeProc):
            def wait(self, timeout=None):
                if self.returncode == -15:  # terminate() already called
                    return self.returncode
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        proc = HangingProc(pid=3)
        orchestrate._terminate_player(proc, "aether", wait_timeout=0.01)
        self.assertEqual(proc.returncode, -15)  # terminate() was called

    def test_no_failure_path_leaves_the_process_running(self):
        # Every branch (already exited, graceful exit, escalation) must
        # leave the process not-running by the time _terminate_player
        # returns: this is the property the "finally" wiring in
        # measure_once exists to guarantee.
        class NeverExitsProc(FakeProc):
            def wait(self, timeout=None):
                if self.returncode in (-15, -9):
                    return self.returncode
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        proc = NeverExitsProc(pid=4)
        orchestrate._terminate_player(proc, "aether", wait_timeout=0.01)
        self.assertIn(proc.returncode, (-15, -9))


class TakeCleanBaselineTests(unittest.TestCase):
    """Baseline candidates specifically, distinct from HEALTHY_POWER/
    THROTTLED_POWER above (which stand in for a measured *run's* power and
    would fail the magnitude ceiling here). Stability now requires two
    CONSECUTIVE agreeing readings, so acceptance never happens before the
    second attempt even when every reading is clean; every test below
    accounts for that rather than expecting a single good reading to be
    enough.
    """
    # ~150 mW: this machine's own observed genuine-idle floor.
    CLEAN = {"cpuPowerMw": 150.0, "gpuPowerMw": 5.0, "anePowerMw": 0.0,
             "eClusterResidency": 15.0, "pClusterResidency": 5.0,
             "thermalPressure": "Nominal", "throttled": False, "samples": 60}
    # Close to CLEAN (15 mW apart, under the 50 mW tolerance) but not
    # bit-identical, so the stability check is exercised as real
    # arithmetic, not an accidental object-equality match.
    CLEAN_AGREEING = dict(CLEAN, cpuPowerMw=165.0)
    # The real, live-observed reading right after a four-target xcodebuild:
    # Nominal thermal, a full 60 samples, and still 530.5 mW.
    BUSY_BUILD = dict(CLEAN, cpuPowerMw=530.5, thermalPressure="Nominal", throttled=False)

    def test_two_consecutive_agreeing_readings_are_accepted(self):
        cooldowns = {"n": 0}
        readings = [dict(self.CLEAN), dict(self.CLEAN_AGREEING)]
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: cooldowns.__setitem__("n", cooldowns["n"] + 1),
            sample_fn=lambda seconds: readings.pop(0))
        self.assertIsNone(error)
        self.assertEqual(baseline["cpuPowerMw"], self.CLEAN_AGREEING["cpuPowerMw"])
        self.assertEqual(cooldowns["n"], 1)  # one cooldown between the two readings

    def test_a_single_clean_reading_is_not_enough_on_its_own(self):
        # The magnitude ceiling alone would accept this; stability requires
        # a second, consecutive agreeing reading, so capping attempts at 1
        # must never accept even a perfectly clean single reading.
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: None,
            max_attempts=1, sample_fn=lambda seconds: dict(self.CLEAN))
        self.assertIsNotNone(error)
        self.assertIn("not yet stable", error)

    def test_retries_on_throttled_baseline_then_settles(self):
        attempts = [dict(THROTTLED_POWER), dict(self.CLEAN), dict(self.CLEAN_AGREEING)]
        cooldowns = {"n": 0}
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: cooldowns.__setitem__("n", cooldowns["n"] + 1),
            max_attempts=4, sample_fn=lambda seconds: attempts.pop(0))
        self.assertIsNone(error)
        self.assertEqual(cooldowns["n"], 2)

    def test_retries_on_unreported_thermal_pressure_then_settles(self):
        unknown = dict(self.CLEAN, thermalPressure=None, throttled=False)
        attempts = [unknown, dict(self.CLEAN), dict(self.CLEAN_AGREEING)]
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: None,
            max_attempts=4, sample_fn=lambda seconds: attempts.pop(0))
        self.assertIsNone(error)

    def test_retries_on_too_few_samples_then_settles(self):
        thin = dict(self.CLEAN, samples=2)
        attempts = [thin, dict(self.CLEAN), dict(self.CLEAN_AGREEING)]
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: None,
            max_attempts=4, sample_fn=lambda seconds: attempts.pop(0))
        self.assertIsNone(error)

    def test_rejects_a_baseline_over_the_magnitude_ceiling_then_settles(self):
        # The exact defect this exists for: Nominal thermal, a full sample
        # count, and still not a real idle reading. Thermal/sample checks
        # alone would have accepted BUSY_BUILD outright.
        attempts = [dict(self.BUSY_BUILD), dict(self.CLEAN), dict(self.CLEAN_AGREEING)]
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: None,
            max_attempts=4, sample_fn=lambda seconds: attempts.pop(0))
        self.assertIsNone(error)
        self.assertLess(baseline["cpuPowerMw"], orchestrate.MAX_BASELINE_CPU_MW)

    def test_two_readings_both_under_the_ceiling_but_disagreeing_are_not_accepted(self):
        # This is specifically what the stability check adds beyond the
        # magnitude ceiling: a machine cooling from a build can sit under
        # the ceiling while still trending downward (240 -> 150), and only
        # requiring two in a row to actually match catches that.
        declining_then_settled = [
            dict(self.CLEAN, cpuPowerMw=240.0),   # under the 250 mW ceiling
            dict(self.CLEAN, cpuPowerMw=150.0),   # also under it, but 90 mW away: not stable yet
            dict(self.CLEAN, cpuPowerMw=155.0),   # 5 mW from the previous: stable
        ]
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: None,
            max_attempts=5, sample_fn=lambda seconds: declining_then_settled.pop(0))
        self.assertIsNone(error)
        self.assertEqual(baseline["cpuPowerMw"], 155.0)

    def test_sample_power_raising_is_retried_not_propagated(self):
        # sample_power is documented to raise on a hung/failing
        # powermetrics; the baseline path must not let that raise out of
        # take_clean_baseline any more than measure_once lets it out for a
        # per-run sample.
        calls = {"n": 0}
        clean_reads = [dict(self.CLEAN), dict(self.CLEAN_AGREEING)]

        def flaky(seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("sudo powermetrics timed out")
            return clean_reads.pop(0)

        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: None, max_attempts=4, sample_fn=flaky)
        self.assertIsNone(error)
        self.assertEqual(calls["n"], 3)

    def test_exhausting_attempts_returns_last_baseline_and_error_not_raise(self):
        cooldowns = {"n": 0}
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: cooldowns.__setitem__("n", cooldowns["n"] + 1),
            max_attempts=3, sample_fn=lambda seconds: dict(THROTTLED_POWER))
        self.assertIsNotNone(error)
        self.assertIn("throttling", error)
        self.assertEqual(baseline["thermalPressure"], "Heavy")  # last attempt, not discarded
        self.assertEqual(cooldowns["n"], 2)  # cools down between attempts, not after the last one

    def test_every_attempt_raising_returns_empty_baseline_and_error(self):
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: None, max_attempts=2,
            sample_fn=lambda seconds: (_ for _ in ()).throw(RuntimeError("powermetrics gone")))
        self.assertIsNotNone(error)
        self.assertEqual(baseline, {})  # never subtracted: _baseline_subtract/measure_once
        # treat this the same as any other unclean baseline via baseline_error,
        # not by handing back a dict that looks like a real reading.


class WriteResultsTests(unittest.TestCase):
    def test_writes_valid_json_matching_payload(self):
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d) / "results.json"
            orchestrate.write_results(out, {"runs": [], "machine": "test"})
            with open(out) as f:
                data = json.load(f)
            self.assertEqual(data, {"runs": [], "machine": "test"})

    def test_incremental_rewrites_reflect_growth(self):
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d) / "results.json"
            runs = []
            payload = {"runs": runs}
            orchestrate.write_results(out, payload)
            runs.append({"backend": "aether"})
            orchestrate.write_results(out, payload)
            runs.append({"backend": "mpv"})
            orchestrate.write_results(out, payload)
            with open(out) as f:
                data = json.load(f)
            self.assertEqual(len(data["runs"]), 2)

    def test_no_leftover_tmp_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = pathlib.Path(d) / "results.json"
            orchestrate.write_results(out, {"runs": []})
            tmp = out.with_suffix(out.suffix + ".tmp")
            self.assertFalse(tmp.exists())  # os.replace consumed it


class PurgeEngineTempTests(unittest.TestCase):
    def test_removes_engine_caches_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "aether-segments" / "x").mkdir(parents=True)
            (root / "aether-segments" / "x" / "seg1.m4s").write_bytes(b"a" * 1000)
            (root / "aether-software-packets-1234").mkdir()
            (root / "aether-software-packets-1234" / "p").write_bytes(b"b" * 500)
            (root / "keep-me").write_bytes(b"c")
            self.assertEqual(orchestrate.purge_engine_temp(root), 1500)
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["keep-me"])

    def test_missing_dir_is_zero_not_an_error(self):
        self.assertEqual(orchestrate.purge_engine_temp(None), 0)
        self.assertEqual(orchestrate.purge_engine_temp(pathlib.Path("/nonexistent/x")), 0)


if __name__ == "__main__":
    unittest.main()
