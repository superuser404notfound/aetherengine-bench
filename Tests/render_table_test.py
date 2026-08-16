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
        {"backend": "aether", "fixture": "hevc-4k-hdr10.mkv", "repeat": r,
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
        row = render_table.summarize(SAMPLE["runs"])["aether"]["hevc-4k-hdr10.mkv"]
        self.assertEqual(row["cpuPowerMw"], 1410)

    def test_discarded_runs_do_not_enter_the_median(self):
        runs = [dict(r) for r in SAMPLE["runs"]]
        runs[0] = dict(runs[0], discarded=True, discardReason="throttled")
        row = render_table.summarize(runs)["aether"]["hevc-4k-hdr10.mkv"]
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
    hevc-4k-hdr10.mkv reports bitDepth 0 and colorTransfer 'unreported'
    (VLCKitBackend.swift hardcodes both); AVPlayer's colorTransfer sentinel
    is the separate word 'unknown' (AVPlayerBackend's own fallback).
    """

    def setUp(self):
        self.data = load_fixture()

    def test_summarize_treats_sentinel_bit_depth_as_not_reported(self):
        cell = render_table.summarize(self.data["runs"])["vlckit"]["hevc-4k-hdr10.mkv"]
        self.assertEqual(cell["bitDepths"], [])

    def test_summarize_treats_unreported_color_transfer_as_not_reported(self):
        cell = render_table.summarize(self.data["runs"])["vlckit"]["hevc-4k-hdr10.mkv"]
        self.assertEqual(cell["colorTransfers"], [])

    def test_summarize_treats_unknown_color_transfer_as_not_reported(self):
        cell = render_table.summarize(self.data["runs"])["avplayer"]["hevc-4k-hdr10.mkv"]
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
        # mpv: 1 of 3 repeats on hevc-4k-hdr10.mkv discarded for throttling;
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
        # underneath hevc-4k-hdr10.mkv's clean table. hevc-4k-hdr10.mkv has
        # its own, much smaller, real failures (KSPlayer 1, VLCKit 1, from
        # a launchAttempts: 2 record each).
        out = render_table.render(self.data)
        line = next(l for l in out.splitlines() if l.startswith("Launch failures"))
        self.assertIn("on hevc-4k-hdr10.mkv", line)
        self.assertIn("KSPlayer 1,", line)
        self.assertIn("VLCKit 1", line)
        self.assertNotIn("15", line)

    def test_a_fixtures_launch_failures_do_not_leak_into_a_different_fixture(self):
        # Scoped to hevc-4k-hdr10.mkv's own "Launch failures" line, not the
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
        self.assertEqual(render_table._fixture_launch_failures(raw, "hevc-4k-hdr10.mkv"),
                          [("KSPlayer", 1), ("VLCKit", 1)])
        self.assertEqual(render_table._fixture_launch_failures(raw, "av1-10bit.mkv"),
                          [("KSPlayer", 15)])
        self.assertEqual(render_table._fixture_launch_failures(raw, "h264-1080p.mp4"), [])

    def test_old_flat_launch_failures_shape_does_not_crash(self):
        # Pre-fix results files stored launchFailures as a flat
        # {backend: int} session-wide count, not {backend: {fixture:
        # int}}. Reproduces the reviewer's exact repro: a plain int value
        # must not raise AttributeError on int.get(), and since there is
        # no trustworthy per-fixture count to read out of an old-shape
        # file, it must not be fabricated as this fixture's own either.
        old_shape = {"vlckit": 2, "ksplayer": 0}
        self.assertEqual(render_table._fixture_launch_failures(old_shape, "hevc-4k-hdr10.mkv"), [])
        # render() end to end against the same old-shape data: reaching
        # this assertion at all is the proof it did not raise.
        sample = {**SAMPLE, "launchFailures": old_shape}
        out = render_table.render(sample)
        self.assertIn("| **AetherEngine** |", out)

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
    """The header names results["machine"] verbatim, and must not also carry
    a hardcoded chassis/thermal-design claim ("MacBook Air, fanless") that
    nothing in the schema backs. The fanless fact is methodologically real
    (it is why the protocol has cooldowns and a throttling discard rule)
    and still needs a home, so it is backed by results["machineModel"]
    (sysctl hw.model, e.g. "MacBookAir10,1") instead: printed only when
    that field is present, so the claim is always checkable against real
    data, never a leftover literal on a results file from other hardware.
    """

    def test_header_names_whatever_machine_the_results_file_says(self):
        sample = {**SAMPLE, "machine": "Apple-M2-Pro"}
        out = render_table.render(sample)
        self.assertIn("Measured on Apple-M2-Pro,", out)

    def test_header_does_not_hardcode_a_chassis_claim(self):
        # SAMPLE carries no machineModel, so nothing about chassis or
        # thermal design may appear: there is no field to back it with.
        out = render_table.render(SAMPLE)
        self.assertNotIn("MacBook Air", out)
        self.assertNotIn("fanless", out)

    def test_header_includes_the_machine_model_when_present(self):
        sample = {**SAMPLE, "machineModel": "MacBookAir10,1"}
        out = render_table.render(sample)
        self.assertIn("Measured on Apple-M1 (MacBookAir10,1),", out)

    def test_fanless_note_is_backed_by_the_model_identifier_when_present(self):
        sample = {**SAMPLE, "machineModel": "MacBookAir10,1"}
        out = render_table.render(sample)
        self.assertIn("MacBookAir10,1 is fanless", out)
        self.assertIn("cooldowns", out)


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
            {"backend": "aether", "fixture": "hevc-4k-hdr10.mkv", "repeat": i,
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
        cell = render_table.summarize(self._runs(outputs))["aether"]["hevc-4k-hdr10.mkv"]
        self.assertEqual(cell["resolutions"], ["3840x1714"])
        self.assertEqual(cell["bitDepths"], [10])
        self.assertEqual(cell["colorTransfers"], ["pq"])

    def test_divergent_resolution_across_repeats_is_disclosed_not_hidden(self):
        outputs = [{"width": 3840, "height": 1714, "bitDepth": 10, "colorTransfer": "pq"},
                   {"width": 3840, "height": 1716, "bitDepth": 10, "colorTransfer": "pq"},
                   {"width": 3840, "height": 1714, "bitDepth": 10, "colorTransfer": "pq"}]
        cell = render_table.summarize(self._runs(outputs))["aether"]["hevc-4k-hdr10.mkv"]
        self.assertEqual(sorted(cell["resolutions"]), ["3840x1714", "3840x1716"])
        line = render_table._format_detail("AetherEngine", cell)
        self.assertIn("resolution varies across repeats", line)
        self.assertIn("3840x1714", line)
        self.assertIn("3840x1716", line)

    def test_divergent_bit_depth_across_repeats_is_disclosed_not_hidden(self):
        outputs = [{"width": 1920, "height": 1080, "bitDepth": 8, "colorTransfer": "bt709"},
                   {"width": 1920, "height": 1080, "bitDepth": 10, "colorTransfer": "bt709"}]
        cell = render_table.summarize(self._runs(outputs))["aether"]["hevc-4k-hdr10.mkv"]
        self.assertEqual(sorted(cell["bitDepths"]), [8, 10])
        line = render_table._format_detail("AetherEngine", cell)
        self.assertIn("bit depth varies across repeats", line)

    def test_divergent_color_transfer_across_repeats_is_disclosed_not_hidden(self):
        outputs = [{"width": 1920, "height": 1080, "bitDepth": 8, "colorTransfer": "bt709"},
                   {"width": 1920, "height": 1080, "bitDepth": 8, "colorTransfer": "smpte170m"}]
        cell = render_table.summarize(self._runs(outputs))["aether"]["hevc-4k-hdr10.mkv"]
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


class CpuPowerColumnDroppedTests(unittest.TestCase):
    """CPU package power was too noisy at sustained 4K load to publish (see
    task notes), so the table lost that column entirely: GPU power, CPU
    load and RSS stayed, plus a Plays column for whether the engine
    produced a valid run at all. This must be a real column change, not
    just a relabel, so these check the header and row shape directly.
    """

    def test_table_header_drops_cpu_power_and_gains_plays(self):
        out = render_table.render(SAMPLE)
        self.assertIn("| | GPU power | CPU load | RSS | Plays |", out)
        self.assertNotIn("| CPU power |", out)
        self.assertNotIn("peak RSS", out)
        self.assertNotIn("on E-cores", out)

    def test_valid_cell_row_has_no_cpu_power_value_and_ends_in_yes(self):
        # AetherEngine's median cpuPowerMw is 1410 (see
        # test_uses_the_median_of_repeats); that number must not leak into
        # the row now that the column is gone, even though the same value
        # is expected to appear elsewhere, in the limitation note's own
        # evidence sentence.
        out = render_table.render(SAMPLE)
        line = next(l for l in out.splitlines() if l.startswith("| **AetherEngine**"))
        self.assertNotIn("1410 mW", line)
        self.assertIn("800 mW", line)  # gpuPowerMw, unchanged
        self.assertTrue(line.rstrip().endswith("| Yes |"))

    def test_invalid_cell_puts_the_na_reason_in_the_plays_column(self):
        runs = [dict(r, discarded=True, discardReason="frame gate: delivered 900 of 1440")
                for r in SAMPLE["runs"]]
        out = render_table.render({**SAMPLE, "runs": runs})
        line = next(l for l in out.splitlines() if l.startswith("| **AetherEngine**"))
        self.assertRegex(line, r"^\| \*\*AetherEngine\*\* \| \| \| \| n/a \(.*\) \|$")

    def test_not_run_cell_puts_the_label_in_the_plays_column(self):
        out = render_table.render(SAMPLE, fixture="h264-1080p.mp4")
        line = next(l for l in out.splitlines() if l.startswith("| **AetherEngine**"))
        self.assertRegex(line, r"^\| \*\*AetherEngine\*\* \| \| \| \| not run \|$")


class CpuPowerLimitationNoteTests(unittest.TestCase):
    """The dropped column gets a stated limitation, backed by the real
    observed spread rather than an unbacked assertion (see
    _cpu_power_spread_evidence's own docstring for why the widest spread
    on the fixture, not a fixed backend, is what gets cited).
    """

    def setUp(self):
        self.data = load_fixture()

    def test_note_explains_why_the_column_is_missing(self):
        out = render_table.render(SAMPLE)
        self.assertIn("CPU package power was measured for every run but is not published", out)
        self.assertIn("Results/", out)

    def test_note_cites_the_widest_observed_spread_as_evidence(self):
        # sample-results.json's hevc-4k-hdr10.mkv: AetherEngine 1400-1420
        # (spread 20), KSPlayer 1630-1650 (spread 20), VLCKit 2190-2210
        # (spread 20), AVPlayer 895-905 (spread 10), mpv 3000-3010 after
        # its throttled repeat is discarded (spread 10). AetherEngine wins
        # the three-way tie only because NAMES lists it first and the
        # picker requires a strictly wider spread to replace the
        # incumbent; whitebox-checked directly against the helper so this
        # doesn't silently start asserting an accident of dict order.
        table = render_table.summarize(self.data["runs"])
        evidence = render_table._cpu_power_spread_evidence(table, "hevc-4k-hdr10.mkv")
        self.assertEqual(evidence, ("AetherEngine", 1400, 1420, 20))

        out = render_table.render(self.data)
        self.assertIn("on hevc-4k-hdr10.mkv AetherEngine measured 1400 to 1420 mW", out)

    def test_note_does_not_assert_a_magnitude_the_cited_fixture_contradicts(self):
        # A light fixture's own real spread can be small (the task this was
        # written for measured AetherEngine at 44-47 mW on a 1080p
        # fixture, versus hundreds of mW on the 4K ones); the general
        # sentence must not hardcode "hundreds of milliwatts" or "4K load"
        # as if that held for every fixture the note is ever attached to,
        # since that would sit right next to a small cited number and
        # contradict it.
        light_cpu_mw = [44, 45, 47]
        light = {**SAMPLE, "runs": [
            {**r, "power": {**r["power"], "cpuPowerMw": light_cpu_mw[r["repeat"]]}}
            for r in SAMPLE["runs"]]}
        out = render_table.render(light)
        self.assertNotIn("hundreds of milliwatts", out)
        self.assertNotIn("4K load", out)
        self.assertIn("AetherEngine measured 44 to 47 mW", out)

    def test_note_falls_back_gracefully_with_no_spread_evidence(self):
        # A cell with only one valid repeat has nothing to take a spread
        # from; the note must still say CPU power is unpublished, just
        # without inventing a number to back it.
        sample = {**SAMPLE, "runs": [SAMPLE["runs"][0]]}
        out = render_table.render(sample)
        self.assertIn("CPU package power was measured for every run but is not published", out)
        self.assertNotIn("For example", out)

    def test_note_sits_in_the_method_notes_near_the_other_power_caveat(self):
        out = render_table.render(SAMPLE)
        lines = out.splitlines()
        package_power_idx = next(
            i for i, l in enumerate(lines) if l.startswith("Power figures are package power"))
        cpu_note_idx = next(
            i for i, l in enumerate(lines)
            if l.startswith("CPU package power was measured for every run"))
        versions_idx = next(i for i, l in enumerate(lines) if l.startswith("Versions:"))
        self.assertLess(versions_idx, package_power_idx)
        self.assertLess(package_power_idx, cpu_note_idx)


class DefaultFixtureTests(unittest.TestCase):
    """hevc-4k-hdr10.mkv is the headline (media servers serve MKV), so the
    no-argument CLI invocation (python3 Scripts/render-table.py
    Results/foo.json), which has no way to pass a fixture, must produce
    that block, not its MP4 twin. render()'s own default parameter is
    the only thing deciding this: a two-fixture dataset with visibly
    different numbers on each is enough to prove which one actually
    comes out when the caller passes no fixture at all.
    """

    @staticmethod
    def _two_fixture_data():
        def run(fixture, gpu_mw):
            return {
                "backend": "aether", "fixture": fixture, "repeat": 0,
                "discarded": False, "discardReason": "",
                "power": {"cpuPowerMw": 100, "gpuPowerMw": gpu_mw, "anePowerMw": 0,
                          "eClusterResidency": 10, "pClusterResidency": 5, "throttled": False},
                "process": {"cpuPercentMean": 5.0, "cpuPercentMax": 6.0,
                            "rssMbMean": 200.0, "rssMbPeak": 210.0},
                "report": {"deliveredFrames": 100, "expectedFrames": 100, "droppedFrames": 0},
                "droppedFramesReported": True,
            }
        return {
            "machine": "Apple-M1", "os": "26.5.2",
            "versions": {"aether": "6.26.0", "avplayer": "x", "vlckit": "x",
                         "ksplayer": "x", "mpv": "x"},
            "runs": [run("hevc-4k-hdr10.mkv", 111), run("hevc-4k-hdr10.mp4", 222)],
        }

    def test_no_argument_render_produces_the_mkv_headline(self):
        out = render_table.render(self._two_fixture_data())
        self.assertIn("hevc-4k-hdr10.mkv", out.splitlines()[0])
        self.assertIn("111 mW", out)
        self.assertNotIn("222 mW", out)

    def test_mp4_twin_is_still_reachable_by_name(self):
        out = render_table.render(self._two_fixture_data(), fixture="hevc-4k-hdr10.mp4")
        self.assertIn("hevc-4k-hdr10.mp4", out.splitlines()[0])
        self.assertIn("222 mW", out)
        self.assertNotIn("111 mW", out)


class NegativePowerRehabilitationTests(unittest.TestCase):
    """A run discarded only because its own CPU package power went
    negative after baseline subtraction is not a bad measurement for
    anything the table still publishes, now that CPU power itself is not
    a column (see CpuPowerLimitationNoteTests). The two real records this
    was written against are AVPlayer's two hevc-4k-hdr10.mp4 repeats: one
    discarded for cpuPowerMw alone (must come back), one discarded for
    both cpuPowerMw and gpuPowerMw together (must not, since gpuPowerMw
    is a published column and that run's own reading of it was also
    physically impossible).
    """

    @staticmethod
    def _avplayer_run(repeat, discard_reason, gpu_mw):
        return {
            "backend": "avplayer", "fixture": "hevc-4k-hdr10.mkv", "repeat": repeat,
            "discarded": bool(discard_reason), "discardReason": discard_reason or "",
            "power": {"cpuPowerMw": -10.68, "gpuPowerMw": gpu_mw, "anePowerMw": 0.0,
                      "eClusterResidency": 26.65, "pClusterResidency": 3.02, "throttled": False},
            "process": {"cpuPercentMax": 2.7, "cpuPercentMean": 1.66,
                        "rssMbMean": 80.87, "rssMbPeak": 81.08},
            "droppedFramesReported": True,
            "report": {"backend": "avplayer", "deliveredFrames": 1440, "droppedFrames": 0,
                       "expectedFrames": 1440,
                       "output": {"width": 3840, "height": 1714, "bitDepth": 8,
                                  "colorTransfer": "unknown", "audioChannels": 2}},
        }

    CPU_ONLY_REASON = ("physically impossible negative power after baseline subtraction "
                        "(cpuPowerMw = raw 68.86666666666666 - baseline 79.55 = -10.7 mW)")
    CPU_AND_GPU_REASON = ("physically impossible negative power after baseline subtraction "
                          "(cpuPowerMw = raw 45.9 - baseline 79.55 = -33.6 mW; "
                          "gpuPowerMw = raw 0.016666666666666666 - baseline 0.6 = -0.6 mW)")

    def test_cpu_power_only_negative_run_is_rehabilitated(self):
        # Direction one: the discard rule that fired checks a metric
        # nobody publishes any more, so the run is not a bad measurement
        # for the columns that remain.
        runs = [self._avplayer_run(0, self.CPU_ONLY_REASON, gpu_mw=2.25)]
        cell = render_table.summarize(runs)["avplayer"]["hevc-4k-hdr10.mkv"]
        self.assertTrue(cell["valid"])
        self.assertEqual(cell["discardedCount"], 0)
        self.assertEqual(cell["gpuPowerMw"], 2)
        self.assertEqual(cell["cpuPercent"], 1.7)
        self.assertEqual(cell["rssMb"], 81)

    def test_rehabilitated_runs_cpu_power_is_excluded_from_the_evidence(self):
        # The rehabilitated run's own cpuPowerMw is a physically
        # impossible, discarded reading. It must never appear in
        # cpuPowerSamplesMw or be cited as if it were real noise evidence.
        runs = [self._avplayer_run(0, self.CPU_ONLY_REASON, gpu_mw=2.25)]
        cell = render_table.summarize(runs)["avplayer"]["hevc-4k-hdr10.mkv"]
        self.assertEqual(cell["cpuPowerSamplesMw"], [])

    def test_cpu_and_gpu_negative_run_stays_fully_discarded(self):
        # Direction two: gpuPowerMw is a published column, and this run's
        # own reading of it was also physically impossible, so
        # rehabilitating it would publish a number the run was thrown out
        # for. Only this one run exists in the cell, so a cell that stayed
        # discarded renders as n/a, not a number.
        runs = [self._avplayer_run(0, self.CPU_AND_GPU_REASON, gpu_mw=-0.58)]
        cell = render_table.summarize(runs)["avplayer"]["hevc-4k-hdr10.mkv"]
        self.assertFalse(cell["valid"])
        self.assertEqual(cell["discardedCount"], 1)

    def test_cpu_power_negative_combined_with_an_unrelated_reason_stays_discarded(self):
        # A second, unrelated discard reason riding along (frame gate,
        # thermal, a crash) must also block rehabilitation, not just a
        # second offending power field. This is the "; " continuation
        # from evaluate_run() joining reasons, not from
        # negative_power_reason() joining offenders.
        combined = self.CPU_ONLY_REASON + ("; frame gate: delivered 900 of 1440.0 "
                                            "recomputed-expected frames (62.5%), threshold 95%")
        runs = [self._avplayer_run(0, combined, gpu_mw=2.25)]
        cell = render_table.summarize(runs)["avplayer"]["hevc-4k-hdr10.mkv"]
        self.assertFalse(cell["valid"])

    def test_the_real_two_repeat_avplayer_cell_ends_up_with_one_valid_run(self):
        # Both real records together: repeat 0 (cpuPowerMw alone) comes
        # back, repeat 1 (cpuPowerMw and gpuPowerMw together) does not, so
        # the cell is valid overall (Yes, not n/a), backed by exactly one
        # of its two repeats, with the other named in a footnote.
        runs = [self._avplayer_run(0, self.CPU_ONLY_REASON, gpu_mw=2.25),
                self._avplayer_run(1, self.CPU_AND_GPU_REASON, gpu_mw=-0.58)]
        cell = render_table.summarize(runs)["avplayer"]["hevc-4k-hdr10.mkv"]
        self.assertTrue(cell["valid"])
        self.assertEqual(cell["totalRuns"], 2)
        self.assertEqual(cell["discardedCount"], 1)
        self.assertEqual(cell["gpuPowerMw"], 2)

        table = {"avplayer": {"hevc-4k-hdr10.mkv": cell}}
        out_row = (f"| **AVPlayer** | {render_table._fmt_mw(cell['gpuPowerMw'])} | "
                   f"{render_table._fmt_pct(cell['cpuPercent'])} of a core | "
                   f"{render_table._fmt_mb(cell['rssMb'])} | Yes |")
        self.assertIn("2 mW", out_row)

    def test_discard_reason_shown_to_a_reader_has_no_raw_arithmetic(self):
        # The remaining, still-discarded repeat's reason must name which
        # field(s) were physically impossible without the raw floats
        # negative_power_reason() prints for Results/.
        runs = [self._avplayer_run(0, self.CPU_ONLY_REASON, gpu_mw=2.25),
                self._avplayer_run(1, self.CPU_AND_GPU_REASON, gpu_mw=-0.58)]
        cell = render_table.summarize(runs)["avplayer"]["hevc-4k-hdr10.mkv"]
        reasons = "; ".join(cell["discardedReasons"])
        self.assertIn("cpuPowerMw", reasons)
        self.assertIn("gpuPowerMw", reasons)
        self.assertNotIn("45.9", reasons)
        self.assertNotIn("79.55", reasons)
        self.assertNotIn(" raw ", reasons)

    def test_a_non_power_discard_reason_is_untouched_by_humanizing(self):
        reason = ("frame gate: delivered 1200 of 1440.0 recomputed-expected frames "
                  "(83.3%), threshold 95%")
        self.assertEqual(render_table._humanize_discard_reason(reason), reason)

    def test_end_to_end_render_shows_avplayer_as_playing_with_real_numbers(self):
        # The full pipeline, not just summarize(): render() on the real
        # two-repeat shape must show AVPlayer as Yes with its rehabilitated
        # GPU power, CPU load and RSS, a footnote naming the still-discarded
        # repeat in human terms, and no trace of the -10.7/-33.6/-0.6 raw
        # subtraction results anywhere in the block.
        runs = [self._avplayer_run(0, self.CPU_ONLY_REASON, gpu_mw=2.25),
                self._avplayer_run(1, self.CPU_AND_GPU_REASON, gpu_mw=-0.58)]
        data = {**SAMPLE, "runs": SAMPLE["runs"] + runs}
        out = render_table.render(data)
        line = next(l for l in out.splitlines() if l.startswith("| **AVPlayer**"))
        self.assertTrue(line.rstrip().endswith("| Yes |"))
        self.assertIn("2 mW", line)
        self.assertIn("1 of 2 repeat(s) discarded", out)
        self.assertNotIn("-10.7", out)
        self.assertNotIn("-33.6", out)
        self.assertNotIn("-0.6 mW", out)


if __name__ == "__main__":
    unittest.main()
