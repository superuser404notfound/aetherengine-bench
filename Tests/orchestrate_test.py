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


class FindPlayerPidTests(unittest.TestCase):
    def test_direct_child_binary(self):
        # sudo -u user AetherBench ...: one hop below the sudo monitor pid.
        table = [(100, 1, "launchd"), (200, 100, "sudo"), (201, 200, "AetherBench")]
        self.assertEqual(orchestrate.find_player_pid(200, "AetherBench", lambda: table), 201)

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
    def __init__(self, pid, exit_after=None):
        self.pid = pid
        self.returncode = None
        self._exit_after = exit_after
        self._polls = 0

    def poll(self):
        self._polls += 1
        if self._exit_after is not None and self._polls >= self._exit_after:
            self.returncode = 1
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
        pid, reason = orchestrate.spawn_and_settle(
            proc, "AetherBench", settle_seconds=5,
            table_fn=lambda: [], sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
            now_fn=lambda: clock["t"])
        self.assertIsNone(pid)
        self.assertIn("crashed during settle", reason)

    def test_never_forks_a_recognizable_player_is_reported(self):
        proc = FakeProc(pid=900)  # stays "alive" (poll() -> None) forever
        clock = {"t": 0.0}
        pid, reason = orchestrate.spawn_and_settle(
            proc, "AetherBench", settle_seconds=2, extra_grace=1,
            table_fn=lambda: [(900, 1, "sudo")],  # never gains a child
            sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
            now_fn=lambda: clock["t"])
        self.assertIsNone(pid)
        self.assertIn("never produced a recognizable player process", reason)

    def test_healthy_launch_resolves_the_player_pid(self):
        proc = FakeProc(pid=1000)
        clock = {"t": 0.0}
        table = [(1000, 1, "sudo"), (1001, 1000, "AetherBench")]
        pid, reason = orchestrate.spawn_and_settle(
            proc, "AetherBench", settle_seconds=2,
            table_fn=lambda: table,
            sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
            now_fn=lambda: clock["t"])
        self.assertEqual(pid, 1001)
        self.assertIsNone(reason)


class LaunchWithRetryTests(unittest.TestCase):
    def test_crash_then_success_counts_exactly_one_failure(self):
        fake_proc = FakeProc(pid=1)
        outcomes = [(None, "crashed during settle (exit 1)"), (42, None)]
        with mock.patch.object(orchestrate, "launch", return_value=fake_proc) as launch_mock, \
             mock.patch.object(orchestrate, "spawn_and_settle", side_effect=outcomes), \
             mock.patch("time.sleep"):
            failures = {"aether": 0}
            proc, pid, attempts, report_path, reason = orchestrate.launch_with_retry(
                "aether", "h264-1080p.mp4", "/fake/full/path/h264-1080p.mp4",
                lambda attempt: f"/tmp/fake-{attempt}.json",
                orchestrate.ProtocolConfig(max_launch_attempts=5), failures)
        self.assertEqual(attempts, 2)
        self.assertEqual(failures["aether"], 1)
        self.assertEqual(pid, 42)
        self.assertIsNotNone(proc)
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
                                return_value=(None, "crashed during settle (exit 1)")), \
             mock.patch("time.sleep"):
            failures = {"vlckit": 0}
            proc, pid, attempts, report_path, reason = orchestrate.launch_with_retry(
                "vlckit", "vp9.webm", "/fake/vp9.webm",
                lambda attempt: f"/tmp/fake-{attempt}.json",
                orchestrate.ProtocolConfig(max_launch_attempts=3), failures)
        self.assertIsNone(proc)
        self.assertIsNone(pid)
        self.assertEqual(attempts, 3)
        self.assertEqual(failures["vlckit"], 3)
        self.assertIn("crashed during settle", reason)


class MeasureOnceGuardedSamplingTests(unittest.TestCase):
    """measure_once is the function the critical review finding was about:
    sample_power is documented to raise, and it was called unguarded here.
    These drive measure_once itself (not just the pieces it's built from)
    to prove a raise becomes a discarded record, not a propagated
    exception, and that the player is always terminated."""

    def test_power_sampling_failure_is_a_discarded_record_not_a_raise(self):
        fake_proc = FakeProc(pid=1)
        with mock.patch.object(orchestrate, "launch_with_retry",
                                return_value=(fake_proc, 999, 1, "/tmp/fake.json", None)), \
             mock.patch.object(orchestrate.sampler, "sample_power", side_effect=RuntimeError("powermetrics hung")), \
             mock.patch.object(orchestrate.sampler, "sample_process", return_value={"cpuPercentMean": 1.0}):
            record = orchestrate.measure_once("aether", "h264-1080p.mp4", 0, {"cpuPowerMw": 100.0},
                                               orchestrate.ProtocolConfig(), {"aether": 0}, pathlib.Path("/tmp"))
        self.assertTrue(record["discarded"])
        self.assertIn("power sampling failed", record["discardReason"])
        self.assertIsNone(record["power"])
        self.assertIsNone(record["report"])

    def test_power_sampling_failure_still_terminates_the_player(self):
        fake_proc = FakeProc(pid=1)
        with mock.patch.object(orchestrate, "launch_with_retry",
                                return_value=(fake_proc, 999, 1, "/tmp/fake.json", None)), \
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
                                return_value=(fake_proc, 999, 1, "/tmp/does-not-exist.json", None)), \
             mock.patch.object(orchestrate.sampler, "sample_power", return_value=dict(HEALTHY_POWER)), \
             mock.patch.object(orchestrate.sampler, "sample_process", return_value={"cpuPercentMean": 1.0}), \
             mock.patch.object(orchestrate, "_terminate_player") as terminate_mock:
            record = orchestrate.measure_once("aether", "h264-1080p.mp4", 0, {"cpuPowerMw": 100.0},
                                               orchestrate.ProtocolConfig(), {"aether": 0}, pathlib.Path("/tmp"))
        terminate_mock.assert_called_once_with(fake_proc, "aether", wait_timeout=60)
        # No report file existed at that path, so this record is discarded
        # for that reason, but the player is still cleanly terminated.
        self.assertTrue(record["discarded"])

    def test_baseline_error_zeroes_subtracted_power_but_keeps_raw(self):
        fake_proc = FakeProc(pid=1)
        with mock.patch.object(orchestrate, "launch_with_retry",
                                return_value=(fake_proc, 999, 1, "/tmp/does-not-exist.json", None)), \
             mock.patch.object(orchestrate.sampler, "sample_power", return_value=dict(HEALTHY_POWER)), \
             mock.patch.object(orchestrate.sampler, "sample_process", return_value={"cpuPercentMean": 1.0}), \
             mock.patch.object(orchestrate, "_terminate_player"):
            record = orchestrate.measure_once("aether", "h264-1080p.mp4", 0, {"cpuPowerMw": 100.0},
                                               orchestrate.ProtocolConfig(), {"aether": 0}, pathlib.Path("/tmp"),
                                               baseline_error="machine reported throttling while idle")
        self.assertIsNone(record["power"]["cpuPowerMw"])
        self.assertEqual(record["powerRaw"]["cpuPowerMw"], HEALTHY_POWER["cpuPowerMw"])
        self.assertIn("idle baseline was not clean", record["discardReason"])


class DemoteTests(unittest.TestCase):
    def test_requires_sudo_user_rather_than_falling_back_to_user(self):
        # Under sudo, $USER is not reliably the invoking user (it can read
        # "root"); silently falling back to it would demote to root and
        # defeat the whole point.
        with mock.patch.dict(os.environ, {"USER": "root"}, clear=False):
            os.environ.pop("SUDO_USER", None)
            with self.assertRaises(SystemExit):
                orchestrate.demote()

    def test_uses_sudo_user_when_present(self):
        with mock.patch.dict(os.environ, {"SUDO_USER": "vincentherbst"}):
            self.assertEqual(orchestrate.demote(), ["sudo", "-u", "vincentherbst"])


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
    def test_clean_on_first_attempt_needs_no_retry(self):
        cooldowns = {"n": 0}
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: cooldowns.__setitem__("n", cooldowns["n"] + 1),
            sample_fn=lambda seconds: dict(HEALTHY_POWER))
        self.assertIsNone(error)
        self.assertEqual(baseline["thermalPressure"], "Nominal")
        self.assertEqual(cooldowns["n"], 0)

    def test_retries_on_throttled_baseline_then_succeeds(self):
        attempts = [dict(THROTTLED_POWER), dict(HEALTHY_POWER)]
        cooldowns = {"n": 0}
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: cooldowns.__setitem__("n", cooldowns["n"] + 1),
            max_attempts=3, sample_fn=lambda seconds: attempts.pop(0))
        self.assertIsNone(error)
        self.assertEqual(cooldowns["n"], 1)

    def test_retries_on_unreported_thermal_pressure(self):
        unknown = dict(HEALTHY_POWER, thermalPressure=None, throttled=False)
        attempts = [unknown, dict(HEALTHY_POWER)]
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: None,
            max_attempts=3, sample_fn=lambda seconds: attempts.pop(0))
        self.assertIsNone(error)

    def test_retries_on_too_few_samples(self):
        thin = dict(HEALTHY_POWER, samples=2)
        attempts = [thin, dict(HEALTHY_POWER)]
        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: None,
            max_attempts=3, sample_fn=lambda seconds: attempts.pop(0))
        self.assertIsNone(error)

    def test_sample_power_raising_is_retried_not_propagated(self):
        # sample_power is documented to raise on a hung/failing
        # powermetrics; the baseline path must not let that raise out of
        # take_clean_baseline any more than measure_once lets it out for a
        # per-run sample.
        calls = {"n": 0}

        def flaky(seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("sudo powermetrics timed out")
            return dict(HEALTHY_POWER)

        baseline, error = orchestrate.take_clean_baseline(
            orchestrate.ProtocolConfig(), cool_and_retry=lambda: None, max_attempts=3, sample_fn=flaky)
        self.assertIsNone(error)
        self.assertEqual(calls["n"], 2)

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


if __name__ == "__main__":
    unittest.main()
