#!/usr/bin/env python3
"""Turns a Results/*.json file (Scripts/orchestrate.py's output) into the
markdown block published under the comparison table in AetherEngine's
README and mirrored on the docs website. This is the only part of the
whole benchmark a reader ever sees, so it has to be honest before it is
pretty:

- A cell whose runs were all discarded renders as n/a plus the reason.
  Never a number, never a blank a reader would skim past.
- Versions come from results["versions"] (derived by orchestrate.py from
  repository state), never from a string embedded in a backend or report.
  An unresolved version prints the word "unresolved", it is not hidden.
- Sentinels are not data. bitDepth 0, colorTransfer "unreported"/"unknown"
  all mean "this engine does not report it"; droppedFrames -1 means "not
  reported", which is not the same as zero drops. None of these print as
  if they were measurements.
- servingPath, where present, says which of an engine's own internal
  playback paths actually served a fixture (only KSPlayer sets it today,
  and which path wins is genuinely fixture-dependent on this machine, see
  Sources/Backends/KSPlayerBackend.swift). Read per cell, never assumed.
  Resolution, bit depth and colour transfer get the same treatment: read
  across every repeat, not just the first, and disclosed if they disagree.
- Launch failures and thermal state are part of the result: a partially
  discarded cell says so next to its numbers, a fully discarded cell says
  so instead of a number, and launch-failure counts are printed scoped to
  the fixture being rendered (orchestrate.py counts them per (backend,
  fixture)), never as one session-wide total sitting under every fixture's
  table, which would re-bill every fixture an engine did not fail on.
- The machine is named from results["machine"]/["machineModel"], not a
  hardcoded literal. The "fanless" claim (why the protocol has cooldowns
  and a throttling discard rule) only prints alongside a model identifier
  a reader can check, never as an unbacked assertion.
"""
import json
import statistics
import sys

# Match the spelling used in AetherEngine's own README comparison table
# ("How it compares"), not an invented label.
NAMES = {
    "aether": "AetherEngine",
    "ksplayer": "KSPlayer",
    "avplayer": "AVPlayer",
    "vlckit": "VLCKit",
    "mpv": "libmpv",
}

# bitDepth 0 (VLCKitBackend, mpv when neither pixelformat property resolved)
# and colorTransfer "unreported" (VLCKitBackend) / "unknown" (AVPlayerBackend's
# own fallback) all mean the same thing: the engine's API surface used here
# does not expose the value. Case-folded because nothing guarantees a future
# backend spells it identically.
_UNREPORTED_TRANSFERS = {"unreported", "unknown"}


def _is_unreported_transfer(value):
    return not value or str(value).strip().lower() in _UNREPORTED_TRANSFERS


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _unique(items):
    seen = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


def summarize(runs):
    """Groups runs by (backend, fixture) and reduces each group to the
    figures the table needs: the median over valid (non-discarded) runs
    only, plus enough about the discarded ones that a partial or total
    discard is never silently invisible downstream.
    """
    table = {}
    for run in runs:
        cell = table.setdefault(run["backend"], {}).setdefault(run["fixture"], {"_runs": []})
        cell["_runs"].append(run)

    for fixtures in table.values():
        for cell in fixtures.values():
            _finalize_cell(cell)
    return table


def _finalize_cell(cell):
    all_runs = cell.pop("_runs")
    valid = [r for r in all_runs if not r.get("discarded")]
    discarded = [r for r in all_runs if r.get("discarded")]

    cell["totalRuns"] = len(all_runs)
    cell["discardedCount"] = len(discarded)
    cell["discardedReasons"] = _unique(r.get("discardReason") for r in discarded)

    if not valid:
        cell["valid"] = False
        cell["reason"] = "; ".join(cell["discardedReasons"]) or "no valid run"
        return

    cell["valid"] = True

    def med(getter, ndigits=None):
        value = _median(getter(r) for r in valid)
        if value is None:
            return None
        return round(value, ndigits) if ndigits is not None else round(value)

    cell["cpuPowerMw"] = med(lambda r: (r.get("power") or {}).get("cpuPowerMw"))
    cell["gpuPowerMw"] = med(lambda r: (r.get("power") or {}).get("gpuPowerMw"))
    cell["cpuPercent"] = med(lambda r: (r.get("process") or {}).get("cpuPercentMean"), 1)
    cell["eResidency"] = med(lambda r: (r.get("power") or {}).get("eClusterResidency"), 1)
    cell["rssMb"] = med(lambda r: (r.get("process") or {}).get("rssMbMean"))
    cell["rssPeakMb"] = med(lambda r: (r.get("process") or {}).get("rssMbPeak"))

    cell["deliveredFrames"] = med(lambda r: (r.get("report") or {}).get("deliveredFrames"))
    cell["expectedFrames"] = med(lambda r: (r.get("report") or {}).get("expectedFrames"))

    # -1 is "not reported" (KSPlayer's KSAVPlayer path has no drop counter at
    # all), never a real negative drop count: excluded before the median,
    # never averaged in and never displayed as if it were a measurement.
    reporting_runs = [r for r in valid if r.get("droppedFramesReported")]
    cell["droppedFramesReported"] = bool(reporting_runs)
    cell["droppedFrames"] = (
        round(_median((r.get("report") or {}).get("droppedFrames") for r in reporting_runs))
        if reporting_runs else None
    )

    cell["servingPaths"] = _unique((r.get("report") or {}).get("servingPath") for r in valid)

    # Resolution, bit depth and colour transfer are properties of the file
    # and the engine, not of one particular repeat, so every valid run's
    # output is read, not just the first: if three repeats disagree, that
    # is a finding worth surfacing (a real non-determinism, or a
    # misconfigured repeat), the same treatment servingPath already gets
    # above, not something to silently take one sample of.
    outputs = [(r.get("report") or {}).get("output") or {} for r in valid]
    cell["resolutions"] = _unique(
        f"{o['width']}x{o['height']}" for o in outputs if o.get("width") and o.get("height"))
    cell["bitDepths"] = _unique(o.get("bitDepth") for o in outputs if o.get("bitDepth"))
    cell["colorTransfers"] = _unique(
        o.get("colorTransfer") for o in outputs if not _is_unreported_transfer(o.get("colorTransfer")))


def _infer_repeats(table, fixture):
    counts = [cell.get("totalRuns", 0) for fixtures in table.values()
              for fx, cell in fixtures.items() if fx == fixture]
    return max(counts) if counts else None


def _fmt_mw(value):
    return "not reported" if value is None else f"{value} mW"


def _fmt_pct(value):
    return "not reported" if value is None else f"{value:.1f}%"


def _fmt_mb(value):
    return "not reported" if value is None else f"{value} MB"


def _short_version(value):
    """mpv --version's first line is 'mpv vX.Y.Z Copyright (C) ...'; the
    copyright banner is not part of the version and has no place in a
    version column. Cuts at the first "Copyright" (case-insensitive) if
    present, leaves anything else untouched rather than guessing at a
    format it doesn't recognize."""
    if not value:
        return value
    idx = value.lower().find(" copyright")
    return value[:idx].strip() if idx != -1 else value


def _fixture_launch_failures(raw_launch_failures, fixture):
    """[(display name, count)] for backends with a nonzero launch-failure
    count on this specific fixture, in table (README) order. raw_launch_failures
    is results["launchFailures"], keyed backend -> fixture -> count by
    orchestrate.py; scoping here (not a session-wide sum) is what stops one
    engine's failures on a fixture it cannot play at all from being re-billed
    under every other fixture's clean table.

    Guards against the pre-fix flat {backend: int} shape (a session-wide
    count, from before launch failures were keyed per fixture): a non-dict
    value means this results file predates that keying, so there is no
    trustworthy per-fixture count to read out of it. Treated as "no data
    for this fixture" rather than crashing on int.get(), and rather than
    fabricating an unscoped number next to a table that claims to be
    scoped to one fixture, which is the exact confusion the keying fix
    exists to prevent.
    """
    counts = []
    for b, name in NAMES.items():
        value = raw_launch_failures.get(b)
        if isinstance(value, dict):
            n = value.get(fixture, 0)
            if n:
                counts.append((name, n))
    return counts


def _one_or_varies(values, empty_text, varies_label):
    if not values:
        return empty_text
    if len(values) == 1:
        return str(values[0])
    return f"{varies_label} across repeats ({', '.join(str(v) for v in values)}, see raw results)"


def _format_detail(name, cell):
    if cell["deliveredFrames"] is not None and cell["expectedFrames"] is not None:
        frames = f"{cell['deliveredFrames']}/{cell['expectedFrames']} delivered/expected"
    else:
        frames = "delivered/expected frames not reported"

    if cell["droppedFramesReported"]:
        dropped = f"dropped {cell['droppedFrames']}"
    else:
        dropped = "dropped frames not reported by this engine"

    res = _one_or_varies(cell.get("resolutions") or [], "resolution not reported", "resolution varies")

    bit_depths = cell.get("bitDepths") or []
    transfers = cell.get("colorTransfers") or []
    if not bit_depths and not transfers:
        output_desc = "bit depth and color transfer not reported by this engine"
    else:
        bit_text = (f"{bit_depths[0]}-bit" if len(bit_depths) == 1
                    else _one_or_varies(bit_depths, "bit depth not reported by this engine",
                                         "bit depth varies"))
        transfer_text = _one_or_varies(transfers, "color transfer not reported by this engine",
                                       "color transfer varies")
        output_desc = f"{bit_text}, {transfer_text}"

    line = f"- **{name}**: {frames}, {dropped}, {res}, {output_desc}."
    paths = cell.get("servingPaths") or []
    if len(paths) == 1:
        line += f" Served via **{paths[0]}**."
    elif len(paths) > 1:
        line += f" Served via different paths across repeats ({', '.join(paths)}, see raw results)."
    return line


def render(results, fixture="hevc-4k-hdr10.mp4"):
    table = summarize(results.get("runs", []))
    protocol = results.get("protocol") or {}
    measure_s = protocol.get("measure")
    if measure_s is None:
        measure_s = 60
    repeats = protocol.get("repeats") or _infer_repeats(table, fixture) or 3
    machine = results.get("machine", "unknown machine")
    # machdep.cpu.brand_string names the SoC, not the chassis: a MacBook Air
    # and a MacBook Pro can share a chip but not a thermal design.
    # machineModel (sysctl hw.model, e.g. "MacBookAir10,1") identifies the
    # actual machine, when the results file carries it.
    machine_model = results.get("machineModel")
    machine_desc = f"{machine} ({machine_model})" if machine_model else machine
    os_version = results.get("os", "unknown OS")

    lines = []
    if results.get("dryRun"):
        # orchestrate.py's own note field already ends in "not published
        # data" by convention (run_session/the harness-validation note
        # both write it that way); appending the same phrase again here
        # would read as "..., not published data, not published data.".
        note = results.get("note") or "shortened validation timings, not published data"
        lines += [f"**DRY RUN: {note}.**", ""]

    lines.append(
        f"Measured on {machine_desc}, macOS {os_version}, "
        f"{fixture}, windowed 1920x1080, {measure_s:.0f} s, median of {repeats}.")
    if machine_model:
        # "Fanless" only prints when there is a model identifier a reader
        # can check it against: an unbacked chassis/thermal-design claim
        # is exactly what the previous fix round removed from this header.
        # It also explains why the protocol has cooldowns and a throttling
        # discard rule, so it belongs next to the numbers, not buried in
        # the generic caveat list at the bottom.
        lines.append(
            f"{machine_model} is fanless: sustained decode can reach thermal pressure on this "
            "rig, which is why the protocol includes cooldowns between runs and discards any "
            "window recorded while the SoC reported throttling.")

    lines += [
        "",
        "| | CPU power | GPU power | CPU load | on E-cores | RSS | peak RSS |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    footnotes = []
    detail_lines = []
    for backend, name in NAMES.items():
        cell = table.get(backend, {}).get(fixture)
        if not cell:
            lines.append(f"| **{name}** | not run | | | | | |")
            continue
        if not cell["valid"]:
            lines.append(f"| **{name}** | n/a ({cell['reason']}) | | | | | |")
            continue

        marker = ""
        if cell["discardedCount"]:
            footnotes.append(
                f"[{len(footnotes) + 1}] **{name}**, {fixture}: {cell['discardedCount']} of "
                f"{cell['totalRuns']} repeat(s) discarded and excluded from the figures above "
                f"({'; '.join(cell['discardedReasons'])}).")
            marker = f" [{len(footnotes)}]"

        lines.append(
            f"| **{name}**{marker} | {_fmt_mw(cell['cpuPowerMw'])} | {_fmt_mw(cell['gpuPowerMw'])} | "
            f"{_fmt_pct(cell['cpuPercent'])} of a core | {_fmt_pct(cell['eResidency'])} | "
            f"{_fmt_mb(cell['rssMb'])} | {_fmt_mb(cell['rssPeakMb'])} |")
        detail_lines.append(_format_detail(name, cell))

    lines.append("")
    if footnotes:
        lines.extend(footnotes)
        lines.append("")

    # Placed directly under the table it concerns, not down by the
    # versions/method notes, and scoped to this fixture only (see
    # _fixture_launch_failures's own docstring for why a session-wide sum
    # would misattribute failures to fixtures that never had any).
    launch_failures = _fixture_launch_failures(results.get("launchFailures") or {}, fixture)
    if launch_failures:
        fails = ", ".join(f"{name} {n}" for name, n in launch_failures)
        lines += [
            f"Launch failures on {fixture} (the process crashed or never became ready on a "
            "launch attempt; every failed attempt is counted, whether or not a later attempt "
            "in the same cell went on to succeed. A deterministic refusal, an engine correctly "
            "declining a source it was never going to support, is not counted here: it shows "
            f"once, in the cell's own n/a reason, on the first attempt): {fails}.",
            "",
        ]

    if detail_lines:
        lines.append(
            "Frame delivery and output (median delivered/expected frames, dropped frames; "
            "resolution, bit depth and HDR transfer are informational per engine and are never "
            "comparable across engines; a value that disagreed across repeats is shown as "
            "'varies across repeats' rather than one repeat picked silently):")
        lines.extend(detail_lines)
        lines.append("")

    # Versions come from the results file, where orchestrate.py derived them
    # from repository state. Never from a string typed by hand into a backend.
    resolved = results.get("versions", {})
    versions = ", ".join(f"{name} {_short_version(resolved.get(key, 'unresolved'))}"
                         for key, name in NAMES.items())
    lines.append(f"Versions: {versions}.")

    lines += [
        "",
        "Power figures are package power with an idle baseline subtracted, so they are "
        "attributable to the run and not to the machine.",
        "libmpv is measured with --hwdec=auto-safe (hardware decode). mpv's own shipped "
        "default is software decode (--hwdec=no); measuring that default would score a flag "
        "omission as engine inefficiency rather than a real difference between engines.",
        "Method and raw results: https://github.com/superuser404notfound/aetherengine-bench",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        print(render(json.load(f)))
