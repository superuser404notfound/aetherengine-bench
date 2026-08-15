#!/usr/bin/env python3
"""Unit tests for Scripts/render-table.py.

Run with: python3 Tests/render_table_test.py (works from any working
directory and against a clean checkout; no root, no live players, no
network. Tests/Fixtures/sample-results.json stands in for a real
Results/*.json, hand-built to exercise the honesty rules the renderer
exists to enforce: sentinel values never printed as measurements, a
cell with no valid run never printed as a number, servingPath surfaced
per fixture (it genuinely differs by fixture, see the file's own
comment), thermal/launch-failure visibility, and unresolved versions
named rather than hidden.
"""
import json
import os
import sys
import unittest
import importlib

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, "..", "Scripts"))
render_table = importlib.import_module("render-table")  # noqa: E402

FIXTURE_PATH = os.path.join(THIS_DIR, "Fixtures", "sample-results.json")


def load_fixture():
    with open(FIXTURE_PATH) as f:
        return json.load(f)


# Same shape as a trimmed real Results/*.json, small enough to hand-check
# the median arithmetic by eye (1400, 1410, 1420 -> 1410).
SAMPLE = {
    "machine": "Apple-M1", "date": "2026-08-20", "os": "26.5.2",
    "versions": {"aether": "6.26.0", "avplayer": "macOS 26.5.2", "vlckit": "4.0.0-alpha.21",
                 "ksplayer": "2.3.4", "mpv": "mpv 0.40.0"},
    "runs": [
        {"backend": "aether", "fixture": "hevc-4k-hdr10.mp4", "repeat": r,
         "discarded": False, "discardReason": "",
         "power": {"cpuPowerMw": 1400 + r * 10, "gpuPowerMw": 800, "anePowerMw": 0,
                   "eClusterResidency": 40, "pClusterResidency": 10, "throttled": False},
         "process": {"cpuPercentMean": 38.0, "cpuPercentMax": 44.0,
                     "rssMbMean": 210.0, "rssMbPeak": 260.0},
         "report": {"engineVersion": "6.26.0", "deliveredFrames": 1440,
                    "expectedFrames": 1440, "droppedFrames": 0},
         "droppedFramesReported": True}
        for r in range(3)
    ],
}


class RenderTests(unittest.TestCase):
    def test_uses_the_median_of_repeats(self):
        row = render_table.summarize(SAMPLE["runs"])["aether"]["hevc-4k-hdr10.mp4"]
        self.assertEqual(row["cpuPowerMw"], 1410)

    def test_discarded_runs_do_not_enter_the_median(self):
        runs = [dict(r) for r in SAMPLE["runs"]]
        runs[0] = dict(runs[0], discarded=True, discardReason="throttled")
        row = render_table.summarize(runs)["aether"]["hevc-4k-hdr10.mp4"]
        self.assertEqual(row["cpuPowerMw"], 1415)

    def test_a_cell_with_no_valid_run_renders_as_na_with_reason(self):
        runs = [dict(r, discarded=True, discardReason="frame gate: delivered 900 of 1440")
                for r in SAMPLE["runs"]]
        out = render_table.render({**SAMPLE, "runs": runs})
        self.assertIn("n/a", out)
        self.assertIn("frame gate", out)

    def test_header_names_machine_os_and_fixture(self):
        out = render_table.render(SAMPLE)
        self.assertIn("Apple-M1", out)
        self.assertIn("26.5.2", out)
        self.assertIn("60 s, median of 3", out)

    def test_versions_come_from_the_results_file(self):
        out = render_table.render(SAMPLE)
        self.assertIn("AetherEngine 6.26.0", out)
        self.assertIn("libmpv mpv 0.40.0", out)

    def test_an_unresolved_version_is_named_not_hidden(self):
        sample = {**SAMPLE, "versions": {"aether": "6.26.0"}}
        out = render_table.render(sample)
        self.assertIn("unresolved", out)


class SentinelTests(unittest.TestCase):
    """bitDepth: 0, colorTransfer: unreported/unknown, droppedFrames: -1 all
    mean 'this engine does not report it', never a measurement of zero or a
    literal transfer function name. Fixture cell: VLCKit on
    hevc-4k-hdr10.mp4 reports bitDepth 0 and colorTransfer 'unreported'
    (VLCKitBackend.swift hardcodes both); AVPlayer's colorTransfer sentinel
    is the separate word 'unknown' (AVPlayerBackend's own fallback).
    """

    def setUp(self):
        self.data = load_fixture()

    def test_summarize_treats_sentinel_bit_depth_as_not_reported(self):
        cell = render_table.summarize(self.data["runs"])["vlckit"]["hevc-4k-hdr10.mp4"]
        self.assertIsNone(cell["bitDepth"])

    def test_summarize_treats_unreported_color_transfer_as_not_reported(self):
        cell = render_table.summarize(self.data["runs"])["vlckit"]["hevc-4k-hdr10.mp4"]
        self.assertIsNone(cell["colorTransfer"])

    def test_summarize_treats_unknown_color_transfer_as_not_reported(self):
        cell = render_table.summarize(self.data["runs"])["avplayer"]["hevc-4k-hdr10.mp4"]
        self.assertIsNone(cell["colorTransfer"])

    def test_rendered_vlckit_line_says_not_reported_not_zero_bit(self):
        out = render_table.render(self.data)
        line = next(l for l in out.splitlines() if l.startswith("- **VLCKit**"))
        self.assertIn("not reported", line)
        self.assertNotIn("0-bit", line)

    def test_rendered_avplayer_line_does_not_print_the_word_unknown_as_a_value(self):
        out = render_table.render(self.data)
        line = next(l for l in out.splitlines() if l.startswith("- **AVPlayer**"))
        self.assertIn("not reported", line)
        self.assertNotIn("unknown", line)

    def test_dropped_frames_sentinel_never_prints_as_minus_one(self):
        # Scoped to KSPlayer's own detail line, not the whole block: mpv's
        # copyright string ("mplayer2 projects") also contains the digits
        # "1", so a whole-output substring check for "-1" is a false
        # positive waiting to happen, not evidence about the sentinel.
        out = render_table.render(self.data, fixture="h264-1080p.mp4")
        line = next(l for l in out.splitlines() if l.startswith("- **KSPlayer**"))
        self.assertIn("not reported", line)
        self.assertNotIn("-1", line)

    def test_summarize_marks_dropped_frames_as_unreported_for_ksavplayer_path(self):
        cell = render_table.summarize(self.data["runs"])["ksplayer"]["h264-1080p.mp4"]
        self.assertFalse(cell["droppedFramesReported"])
        self.assertIsNone(cell["droppedFrames"])


class ServingPathTests(unittest.TestCase):
    """servingPath says which of an engine's own internal paths actually
    served a fixture. Only KSPlayer sets it, and it is genuinely
    fixture-dependent on this machine (KSMEPlayer for the HDR fixture,
    KSAVPlayer for the plain H.264 one, per task 6/9's own findings), so
    the renderer must read it per cell, never assume one path project-wide.
    """

    def setUp(self):
        self.data = load_fixture()

    def test_serving_path_is_shown_when_present(self):
        out = render_table.render(self.data)
        line = next(l for l in out.splitlines() if l.startswith("- **KSPlayer**"))
        self.assertIn("KSMEPlayer", line)

    def test_serving_path_differs_by_fixture_not_hardcoded(self):
        out = render_table.render(self.data, fixture="h264-1080p.mp4")
        line = next(l for l in out.splitlines() if l.startswith("- **KSPlayer**"))
        self.assertIn("KSAVPlayer", line)

    def test_serving_path_is_absent_for_a_single_path_engine(self):
        out = render_table.render(self.data)
        line = next(l for l in out.splitlines() if l.startswith("- **AetherEngine**"))
        self.assertNotIn("Served via", line)


class ThermalAndLaunchFailureTests(unittest.TestCase):
    def setUp(self):
        self.data = load_fixture()

    def test_partial_thermal_discard_is_visible_near_the_numbers(self):
        # mpv: 1 of 3 repeats on hevc-4k-hdr10.mp4 discarded for throttling;
        # the median still comes from the other 2, but the discard must not
        # be invisible next to the clean-looking number.
        out = render_table.render(self.data)
        self.assertIn("1 of 3 repeat(s) discarded", out)
        self.assertIn("throttling", out)

    def test_fully_discarded_cell_names_the_real_launch_failure_reason(self):
        # KSPlayer's free GPL build cannot decode AV1 at all (no format
        # description ever produced); every attempt crashes during settle
        # and the cell exhausts its retry budget.
        out = render_table.render(self.data, fixture="av1-10bit.mkv")
        self.assertIn("n/a", out)
        self.assertIn("launch failed after 5 attempt(s)", out)

    def test_session_launch_failures_are_reported_near_the_numbers(self):
        out = render_table.render(self.data)
        self.assertIn("Launch failures", out)
        self.assertIn("KSPlayer 15", out)

    def test_a_backend_never_run_against_a_fixture_says_so(self):
        out = render_table.render(self.data, fixture="h264-1080p.mp4")
        line = next(l for l in out.splitlines() if l.startswith("| **AetherEngine**"))
        self.assertIn("not run", line)


class DryRunTests(unittest.TestCase):
    def test_dry_run_results_are_flagged_prominently(self):
        sample = {**SAMPLE, "dryRun": True, "note": "shortened validation run"}
        out = render_table.render(sample)
        self.assertIn("DRY RUN", out)

    def test_a_real_session_carries_no_dry_run_warning(self):
        out = render_table.render(SAMPLE)
        self.assertNotIn("DRY RUN", out)

    def test_a_note_that_already_says_not_published_data_is_not_doubled(self):
        # orchestrate.py's own note field already ends in "not published
        # data" (both run_session's real note and the harness-validation
        # one used during task 9's own dry run write it that way); the
        # renderer must not append the phrase a second time.
        sample = {**SAMPLE, "dryRun": True,
                  "note": "shortened validation run (see --settle/--measure), not published data"}
        out = render_table.render(sample)
        self.assertNotIn("not published data, not published data", out)


if __name__ == "__main__":
    unittest.main()
