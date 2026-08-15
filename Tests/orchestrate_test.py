#!/usr/bin/env python3
"""Unit tests for Scripts/orchestrate.py.

Run with: python3 Tests/orchestrate_test.py (works from any working
directory and against a clean checkout; no root, no live powermetrics, and
no real player binaries needed. Everything that touches a real subprocess,
a real launch, or a real thermal reading is exercised by the dry run and
the forced-discard demonstration instead, both described in the task-9
report, not here.)
"""
import os
import sys
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


if __name__ == "__main__":
    unittest.main()
