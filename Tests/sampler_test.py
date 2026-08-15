#!/usr/bin/env python3
"""Unit tests for Scripts/sampler.py.

Run with: python3 Tests/sampler_test.py (works from any working directory
and against a clean checkout; no root and no live powermetrics access
needed, everything here runs against recorded/synthetic text or a real
short-lived subprocess this test spawns itself).
"""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, "..", "Scripts"))
import sampler  # noqa: E402

FIXTURE_PATH = os.path.join(THIS_DIR, "Fixtures", "powermetrics_sample.txt")


def load_fixture():
    with open(FIXTURE_PATH) as f:
        return f.read()


class ParsePowermetricsTests(unittest.TestCase):
    def test_parses_powermetrics_block(self):
        text = """
CPU Power: 1423 mW
GPU Power: 812 mW
ANE Power: 0 mW
E-Cluster HW active residency: 42.31%
P-Cluster HW active residency: 18.02%
"""
        parsed = sampler.parse_powermetrics(text)
        self.assertEqual(parsed["cpuPowerMw"], 1423.0)
        self.assertEqual(parsed["gpuPowerMw"], 812.0)
        self.assertAlmostEqual(parsed["eClusterResidency"], 42.31)
        self.assertFalse(parsed["throttled"])

    def test_detects_throttling(self):
        parsed = sampler.parse_powermetrics("CPU Power: 900 mW\npackage idle exit: throttled\n")
        self.assertTrue(parsed["throttled"])

    def test_missing_cpu_power_raises_not_zero(self):
        # No "CPU Power" line at all: this is malformed/truncated powermetrics
        # output, not a legitimate zero-watt reading. Must raise, never return
        # a dict with cpuPowerMw = 0.0.
        with self.assertRaises(ValueError):
            sampler.parse_powermetrics("GPU Power: 500 mW\nANE Power: 0 mW\n")

    def test_missing_optional_fields_are_none_not_zero(self):
        # GPU/ANE power and cluster residencies were genuinely absent from
        # this block. That must come back as None, distinguishable from a
        # real 0.0 reading, never silently coerced to 0.0.
        parsed = sampler.parse_powermetrics("CPU Power: 900 mW\n")
        self.assertIsNone(parsed["gpuPowerMw"])
        self.assertIsNone(parsed["anePowerMw"])
        self.assertIsNone(parsed["eClusterResidency"])
        self.assertIsNone(parsed["pClusterResidency"])

    def test_parses_recorded_multiblock_sample(self):
        # Real `sudo powermetrics --samplers cpu_power -i 1000 -n 3` capture
        # from this machine (Tests/Fixtures/powermetrics_sample.txt), MacBookAir10,1
        # (M1). Each block carries the per-frequency parenthesised breakdown
        # after the residency percentage (e.g. "23.02% (600 MHz: 13% ...)"),
        # and dozens of per-CPU lines the parser must not confuse with the
        # cluster-level lines it actually wants.
        raw = load_fixture()
        blocks_text = [
            b for b in raw.split(sampler.POWERMETRICS_BLOCK_HEADER) if "CPU Power" in b
        ]
        self.assertEqual(len(blocks_text), 3)
        parsed = [sampler.parse_powermetrics(b) for b in blocks_text]
        self.assertEqual([p["cpuPowerMw"] for p in parsed], [221.0, 4980.0, 124.0])
        self.assertEqual([p["gpuPowerMw"] for p in parsed], [1.0, 1.0, 1.0])
        self.assertEqual([p["anePowerMw"] for p in parsed], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(parsed[0]["eClusterResidency"], 23.02)
        self.assertAlmostEqual(parsed[1]["pClusterResidency"], 90.11)
        self.assertFalse(any(p["throttled"] for p in parsed))


class AggregatePowerOutputTests(unittest.TestCase):
    """Exercises sampler._aggregate_power_output, the pure function that
    sample_power hands its subprocess stdout to. Covered directly so the
    averaging/sentinel logic is tested without needing root or a live
    powermetrics call."""

    def test_aggregates_recorded_multiblock_sample(self):
        raw = load_fixture()
        result = sampler._aggregate_power_output(raw)
        self.assertEqual(result["samples"], 3)
        self.assertAlmostEqual(result["cpuPowerMw"], (221.0 + 4980.0 + 124.0) / 3)
        self.assertAlmostEqual(result["gpuPowerMw"], (1.0 + 1.0 + 1.0) / 3)
        self.assertEqual(result["anePowerMw"], 0.0)
        self.assertFalse(result["throttled"])

    def test_no_parseable_blocks_raises(self):
        with self.assertRaises(RuntimeError):
            sampler._aggregate_power_output("nothing useful in here\n")

    def test_throttled_true_if_any_block_reports_it(self):
        raw = (
            sampler.POWERMETRICS_BLOCK_HEADER + " (a)\nCPU Power: 900 mW\n"
            + sampler.POWERMETRICS_BLOCK_HEADER + " (b)\nCPU Power: 950 mW\npackage idle exit: throttled\n"
        )
        result = sampler._aggregate_power_output(raw)
        self.assertTrue(result["throttled"])

    def test_optional_field_missing_from_every_block_is_none_not_zero(self):
        raw = (
            sampler.POWERMETRICS_BLOCK_HEADER + " (a)\nCPU Power: 900 mW\n"
            + sampler.POWERMETRICS_BLOCK_HEADER + " (b)\nCPU Power: 950 mW\n"
        )
        result = sampler._aggregate_power_output(raw)
        self.assertIsNone(result["gpuPowerMw"])
        self.assertIsNone(result["anePowerMw"])


class SamplePowerCommandTests(unittest.TestCase):
    """sample_power's own subprocess wiring, checked with subprocess.run
    mocked out. Confirms it shells out to `sudo powermetrics` (the binary
    the sudoers NOPASSWD grant is scoped to) with a closed stdin (so a
    missing grant fails fast instead of hanging on a password prompt that
    will never come in an unattended run), and that a nonzero exit raises
    with the real stderr surfaced rather than being swallowed."""

    def test_invokes_sudo_powermetrics_with_closed_stdin(self):
        raw = load_fixture()
        with mock.patch.object(sampler.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["sudo", "powermetrics"], returncode=0, stdout=raw, stderr=""
            )
            result = sampler.sample_power(3, interval_ms=1000)
        args, kwargs = run.call_args
        cmd = args[0]
        self.assertEqual(cmd[0:2], ["sudo", "powermetrics"])
        self.assertIn("-n", cmd)
        self.assertEqual(cmd[cmd.index("-n") + 1], "3")
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(result["samples"], 3)

    def test_nonzero_exit_raises_with_stderr_not_a_default_dict(self):
        with mock.patch.object(sampler.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["sudo", "powermetrics"], returncode=1, stdout="",
                stderr="sudo: a password is required\n",
            )
            with self.assertRaises(RuntimeError) as ctx:
                sampler.sample_power(3)
        self.assertIn("password is required", str(ctx.exception))

    def test_non_positive_seconds_raises(self):
        with self.assertRaises(ValueError):
            sampler.sample_power(0)
        with self.assertRaises(ValueError):
            sampler.sample_power(-1)

    def test_sub_interval_duration_raises_instead_of_asking_for_zero_samples(self):
        # seconds=0.4 with the default 1000ms interval would compute n=0,
        # i.e. ask powermetrics for zero samples. That must be refused up
        # front rather than run and then hit the "no parseable samples" path.
        with mock.patch.object(sampler.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                sampler.sample_power(0.4)
        run.assert_not_called()


class AggregateProcessTests(unittest.TestCase):
    def test_aggregates_ps_samples(self):
        agg = sampler.aggregate_process([(10.0, 100.0), (20.0, 200.0), (30.0, 150.0)])
        self.assertEqual(agg["cpuPercentMean"], 20.0)
        self.assertEqual(agg["cpuPercentMax"], 30.0)
        self.assertEqual(agg["rssMbPeak"], 200.0)

    def test_empty_samples_raises_not_a_zero_aggregate(self):
        with self.assertRaises(ValueError):
            sampler.aggregate_process([])


class ReadPsLocaleTests(unittest.TestCase):
    """Regression test: on a machine whose locale uses a comma decimal
    separator (this dev machine is de_DE.UTF-8), ps prints "%cpu" as "0,0"
    rather than "0.0", which float() rejects outright. That is not a
    hypothetical, it reproduced on first run here. The fix forces LC_ALL=C
    on the ps subprocess; this test checks the env is actually passed
    rather than relying on whatever locale happens to run the suite."""

    def test_ps_subprocess_forces_c_locale(self):
        with mock.patch.object(sampler.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["ps"], returncode=0, stdout="  0.0   2592 S\n", stderr=""
            )
            sampler._read_ps(os.getpid())
        _, kwargs = run.call_args
        env = kwargs.get("env")
        self.assertIsNotNone(env, "ps must run with an explicit env forcing a C locale")
        self.assertEqual(env.get("LC_ALL"), "C")
        self.assertEqual(env.get("LC_NUMERIC"), "C")


class ReadPsZombieTests(unittest.TestCase):
    """Regression test, mocked and deterministic: a killed-but-unreaped
    child stays in the process table as a zombie and ps keeps answering
    "0.0 cpu, 0 rss, state Z" for it, reproduced live against a real
    subprocess in this environment. That is a dead process, not an idle
    one; _read_ps must raise on the 'Z' state rather than handing back a
    (0.0, 0.0) tuple that looks exactly like a legitimate quiet sample."""

    def test_zombie_state_raises_instead_of_reading_as_idle(self):
        with mock.patch.object(sampler.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["ps"], returncode=0, stdout="  0.0      0 Z\n", stderr=""
            )
            with self.assertRaises(RuntimeError) as ctx:
                sampler._read_ps(12345)
        self.assertIn("zombie", str(ctx.exception))


class SampleProcessTests(unittest.TestCase):
    """Real subprocess.Popen/ps calls, no root required: these spawn and
    control their own short-lived child so the happy path, the never-
    existed-pid path and the exits-mid-window path are all exercised
    against the genuine ps binary rather than a mock. Running on this
    machine's actual de_DE.UTF-8 locale is itself part of the coverage,
    see ReadPsLocaleTests for why that matters."""

    def test_samples_a_real_process(self):
        child = subprocess.Popen(["sleep", "5"])
        try:
            result = sampler.sample_process(child.pid, 2, interval=0.5)
        finally:
            child.kill()
            child.wait()
        self.assertGreaterEqual(result["cpuPercentMean"], 0.0)
        self.assertGreater(result["rssMbMean"], 0.0)

    def test_pid_that_never_existed_raises(self):
        child = subprocess.Popen(["sleep", "0.01"])
        pid = child.pid
        child.wait()
        time.sleep(0.3)  # let the zombie clear so the pid is truly gone
        with self.assertRaises(RuntimeError):
            sampler.sample_process(pid, 1, interval=0.2)

    def test_process_exiting_mid_window_raises_not_partial_success(self):
        child = subprocess.Popen(["sleep", "5"])

        def kill_soon():
            time.sleep(0.6)
            child.kill()

        import threading
        threading.Thread(target=kill_soon, daemon=True).start()
        with self.assertRaises(RuntimeError):
            sampler.sample_process(child.pid, 3, interval=0.3)
        child.wait()

    def test_non_positive_seconds_raises(self):
        with self.assertRaises(ValueError):
            sampler.sample_process(os.getpid(), 0)


if __name__ == "__main__":
    unittest.main()
