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
        self.assertEqual(cell["bitDepths"], [])

    def test_summarize_treats_unreported_color_transfer_as_not_reported(self):
        cell = render_table.summarize(self.data["runs"])["vlckit"]["hevc-4k-hdr10.mp4"]
        self.assertEqual(cell["colorTransfers"], [])

    def test_summarize_treats_unknown_color_transfer_as_not_reported(self):
        cell = render_table.summarize(self.data["runs"])["avplayer"]["hevc-4k-hdr10.mp4"]
        self.assertEqual(cell["colorTransfers"], [])

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

    def test_launch_failures_are_scoped_to_the_rendered_fixture(self):
        # KSPlayer's 15 launch failures in the fixture all belong to
        # av1-10bit.mkv (the free GPL build can never start decoding it,
        # every attempt crashes during settle); they must not be re-billed
        # underneath hevc-4k-hdr10.mp4's clean table. hevc-4k-hdr10.mp4 has
        # its own, much smaller, real failures (KSPlayer 1, VLCKit 1, from
        # a launchAttempts: 2 record each).
        out = render_table.render(self.data)
        line = next(l for l in out.splitlines() if l.startswith("Launch failures"))
        self.assertIn("on hevc-4k-hdr10.mp4", line)
        self.assertIn("KSPlayer 1,", line)
        self.assertIn("VLCKit 1", line)
        self.assertNotIn("15", line)

    def test_a_fixtures_launch_failures_do_not_leak_into_a_different_fixture(self):
        # Scoped to hevc-4k-hdr10.mp4's own "Launch failures" line, not the
        # whole block: libmpv's E-core residency ("15.0%") also contains
        # the digits "15", a false positive waiting to happen, not evidence
        # about launch failures (same lesson as the earlier "-1" fix).
        out_hevc = render_table.render(self.data)
        hevc_line = next(l for l in out_hevc.splitlines() if l.startswith("Launch failures"))
        self.assertNotIn("15", hevc_line)

        out_av1 = render_table.render(self.data, fixture="av1-10bit.mkv")
        av1_line = next(l for l in out_av1.splitlines() if l.startswith("Launch failures"))
        self.assertIn("on av1-10bit.mkv", av1_line)
        self.assertIn("KSPlayer 15", av1_line)

    def test_fixture_launch_failures_helper_is_directly_correct(self):
        # Whitebox check on the extracted helper, independent of markdown
        # string-scraping: the source of truth is orchestrate.py's nested
        # backend -> fixture -> count shape, read straight, not summed.
        raw = self.data["launchFailures"]
        self.assertEqual(render_table._fixture_launch_failures(raw, "hevc-4k-hdr10.mp4"),
                          [("KSPlayer", 1), ("VLCKit", 1)])
        self.assertEqual(render_table._fixture_launch_failures(raw, "av1-10bit.mkv"),
                          [("KSPlayer", 15)])
        self.assertEqual(render_table._fixture_launch_failures(raw, "h264-1080p.mp4"), [])

    def test_launch_failures_follow_the_tables_own_backend_order(self):
        # The fixture's raw launchFailures dict lists vlckit before ksplayer
        # (orchestrate.py's internal BACKENDS order); the rendered line must
        # follow the table's own README-matching order (KSPlayer before
        # VLCKit) instead of just echoing whatever the JSON happened to have.
        out = render_table.render(self.data)
        line = next(l for l in out.splitlines() if l.startswith("Launch failures"))
        self.assertLess(line.index("KSPlayer"), line.index("VLCKit"))

    def test_a_backend_never_run_against_a_fixture_says_so(self):
        out = render_table.render(self.data, fixture="h264-1080p.mp4")
        line = next(l for l in out.splitlines() if l.startswith("| **AetherEngine**"))
        self.assertIn("not run", line)

    def test_launch_failures_line_sits_directly_under_the_table_not_at_the_bottom(self):
        out = render_table.render(self.data)
        lines = out.splitlines()
        table_idx = next(i for i, l in enumerate(lines) if l.startswith("| **AetherEngine**"))
        launch_idx = next(i for i, l in enumerate(lines) if l.startswith("Launch failures"))
        frame_idx = next(i for i, l in enumerate(lines) if l.startswith("Frame delivery and output"))
        versions_idx = next(i for i, l in enumerate(lines) if l.startswith("Versions:"))
        self.assertLess(table_idx, launch_idx)
        self.assertLess(launch_idx, frame_idx)
        self.assertLess(launch_idx, versions_idx)


class MachineHeaderTests(unittest.TestCase):
    """The header names results["machine"] verbatim. It must not also carry
    a hardcoded chassis/thermal-design claim ("MacBook Air, fanless") that
    nothing in the schema backs and that would silently keep printing on a
    results file from different hardware.
    """

    def test_header_names_whatever_machine_the_results_file_says(self):
        sample = {**SAMPLE, "machine": "Apple-M2-Pro"}
        out = render_table.render(sample)
        self.assertIn("Measured on Apple-M2-Pro,", out)

    def test_header_does_not_hardcode_a_chassis_claim(self):
        out = render_table.render(SAMPLE)
        self.assertNotIn("MacBook Air", out)
        self.assertNotIn("fanless", out)


class MpvMethodNoteTests(unittest.TestCase):
    def test_run_mpv_sh_still_carries_the_hwdec_override_the_note_claims(self):
        # The published claim ("measured with --hwdec=auto-safe") must not
        # silently drift from the actual measurement if run-mpv.sh's flag
        # ever changes without this renderer's method note being updated.
        run_mpv_path = os.path.join(THIS_DIR, "..", "Scripts", "run-mpv.sh")
        with open(run_mpv_path) as f:
            contents = f.read()
        self.assertIn("--hwdec=auto-safe", contents)
        self.assertIn("--hwdec=auto-safe", render_table.render(SAMPLE))

    def test_mpv_version_is_trimmed_of_its_copyright_banner(self):
        sample = {**SAMPLE, "versions": {**SAMPLE["versions"],
                  "mpv": "mpv v0.41.0 Copyright (C) 2000-2025 mpv/MPlayer/mplayer2 projects"}}
        out = render_table.render(sample)
        self.assertIn("libmpv mpv v0.41.0", out)
        self.assertNotIn("Copyright", out)


class OutputConsistencyTests(unittest.TestCase):
    """Resolution, bit depth and colour transfer are properties of the file
    and the engine, not of one particular repeat: summarize() must read
    every valid run's output, not just the first, the same treatment
    servingPath already gets. Three repeats disagreeing is a finding to
    surface, not something to silently take one sample of.
    """

    @staticmethod
    def _runs(outputs):
        return [
            {"backend": "aether", "fixture": "hevc-4k-hdr10.mp4", "repeat": i,
             "discarded": False, "discardReason": "",
             "power": {"cpuPowerMw": 1000, "gpuPowerMw": 500, "eClusterResidency": 10},
             "process": {"cpuPercentMean": 10.0, "rssMbMean": 100.0, "rssMbPeak": 110.0},
             "droppedFramesReported": True,
             "report": {"deliveredFrames": 100, "expectedFrames": 100, "droppedFrames": 0,
                        "output": output}}
            for i, output in enumerate(outputs)
        ]

    def test_consistent_output_across_repeats_renders_the_single_value(self):
        outputs = [{"width": 3840, "height": 1714, "bitDepth": 10, "colorTransfer": "pq"}] * 3
        cell = render_table.summarize(self._runs(outputs))["aether"]["hevc-4k-hdr10.mp4"]
        self.assertEqual(cell["resolutions"], ["3840x1714"])
        self.assertEqual(cell["bitDepths"], [10])
        self.assertEqual(cell["colorTransfers"], ["pq"])

    def test_divergent_resolution_across_repeats_is_disclosed_not_hidden(self):
        outputs = [{"width": 3840, "height": 1714, "bitDepth": 10, "colorTransfer": "pq"},
                   {"width": 3840, "height": 1716, "bitDepth": 10, "colorTransfer": "pq"},
                   {"width": 3840, "height": 1714, "bitDepth": 10, "colorTransfer": "pq"}]
        cell = render_table.summarize(self._runs(outputs))["aether"]["hevc-4k-hdr10.mp4"]
        self.assertEqual(sorted(cell["resolutions"]), ["3840x1714", "3840x1716"])
        line = render_table._format_detail("AetherEngine", cell)
        self.assertIn("resolution varies across repeats", line)
        self.assertIn("3840x1714", line)
        self.assertIn("3840x1716", line)

    def test_divergent_bit_depth_across_repeats_is_disclosed_not_hidden(self):
        outputs = [{"width": 1920, "height": 1080, "bitDepth": 8, "colorTransfer": "bt709"},
                   {"width": 1920, "height": 1080, "bitDepth": 10, "colorTransfer": "bt709"}]
        cell = render_table.summarize(self._runs(outputs))["aether"]["hevc-4k-hdr10.mp4"]
        self.assertEqual(sorted(cell["bitDepths"]), [8, 10])
        line = render_table._format_detail("AetherEngine", cell)
        self.assertIn("bit depth varies across repeats", line)

    def test_divergent_color_transfer_across_repeats_is_disclosed_not_hidden(self):
        outputs = [{"width": 1920, "height": 1080, "bitDepth": 8, "colorTransfer": "bt709"},
                   {"width": 1920, "height": 1080, "bitDepth": 8, "colorTransfer": "smpte170m"}]
        cell = render_table.summarize(self._runs(outputs))["aether"]["hevc-4k-hdr10.mp4"]
        self.assertEqual(sorted(cell["colorTransfers"]), ["bt709", "smpte170m"])
        line = render_table._format_detail("AetherEngine", cell)
        self.assertIn("color transfer varies across repeats", line)


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
