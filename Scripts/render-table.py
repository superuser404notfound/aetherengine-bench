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
- A refusal (an engine deterministically declining a source it was never
  going to play, orchestrate.py's REFUSAL_EXIT_CODE, never retried) is not
  a launch failure and renders in the reader's own terms: which fixture
  was declined and the engine's own reported reason, never the exit code
  or the "bench refused:" wrapper the harness writes to stderr, and never
  the backend name a second time (the row already carries it). See
  _humanize_refusal_reason. Every other discard reason, a crash, a frame
  gate miss, a thermal discard, is untouched, it already reads in plain
  terms.
- This block is deliberately short: the prose behind every rule it only
  gestures at (launch failure vs refusal, why CPU power is dropped, the
  mpv hwdec fairness call, what "varies across repeats" or "not reported"
  means) lives in this repository's own README instead of being repeated
  here, and the block links to it once at the bottom rather than
  scattering a pointer after each shortened line. Whichever wording a
  reader actually needs a moment's proof for, the README and the raw
  Results/*.json underneath it are one click away, never the excuse for
  padding what gets pasted into another project's README.
- The machine is named from results["machine"]/["machineModel"], not a
  hardcoded literal. The "fanless" claim (why the protocol has cooldowns
  and a throttling discard rule) only prints alongside a model identifier
  a reader can check, never as an unbacked assertion.
- CPU package power is measured (every run's raw and baseline-subtracted
  value is still in Results/) but never rendered as a column: at
  sustained 4K load its repeat-to-repeat spread has been wide enough to
  exceed the differences between engines it would be used to show, so a
  number would mislead more than inform. The method note that explains
  this cites the actual observed spread for the fixture being rendered
  (see _cpu_power_spread_evidence), not an unbacked assertion.
- A run discarded for nothing but a negative CPU power reading is valid
  for the columns this table still publishes: the discard rule that
  flagged it checks a metric nobody sees any more. Rehabilitated
  narrowly, see _rehabilitate_cpu_power_only_discard; a run whose GPU
  power (a published column) was also physically impossible, or that
  failed for any other reason at all, stays fully discarded. Whatever
  discard reason a reader does see is stripped of the raw arithmetic
  (_humanize_discard_reason): a published table names which field was
  wrong, Results/ has the numbers.
- The default fixture is hevc-4k-hdr10.mkv, the headline, because media
  servers serve MKV; hevc-4k-hdr10.mp4 is rendered as its named second
  block, never as what a bare `render-table.py Results/foo.json`
  produces.
"""
import json
import re
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


# negative_power_reason() in Scripts/orchestrate.py joins every *Mw field
# that subtracted to a negative value with "; " inside one parenthesised
# reason; evaluate_run() then joins that reason with any other discard
# reason (thermal, frame gate, a crash, ...) using the very same "; ". A
# run whose entire discardReason is this pattern, with cpuPowerMw as the
# ONLY offending field, was discarded for a reason that no longer applies
# to anything this table publishes (see _rehabilitate_cpu_power_only_
# discard); anything else, a second offending field or a second reason
# riding along, must not match, since a run whose gpuPowerMw was also
# physically impossible (a column still published) has to stay discarded.
_CPU_POWER_ONLY_NEGATIVE_RE = re.compile(
    r"^physically impossible negative power after baseline subtraction \(cpuPowerMw = raw .+ mW\)$")

# The raw arithmetic negative_power_reason() prints ("cpuPowerMw = raw
# 68.86666666666666 - baseline 79.55 = -10.7 mW") is precise on purpose,
# for Results/. It has no place in a table meant for a reader: this keeps
# which field(s) went negative and drops the numbers, which are still in
# the committed session file for anyone who wants them.
_NEGATIVE_POWER_ARITHMETIC_RE = re.compile(
    r"\w+ = raw -?[\d.]+ - baseline -?[\d.]+ = -?[\d.]+ mW")


def _is_cpu_power_only_negative_discard(reason):
    if not reason or "; " in reason:
        return False
    return bool(_CPU_POWER_ONLY_NEGATIVE_RE.match(reason))


def _humanize_discard_reason(reason):
    if not reason:
        return reason
    return _NEGATIVE_POWER_ARITHMETIC_RE.sub(lambda m: m.group(0).split(" = raw ")[0], reason)


# launch_with_retry() (Scripts/orchestrate.py) does not retry a run whose
# exit code is REFUSAL_EXIT_CODE: a deterministic "this engine will never
# play this source", not a fault (see "What is measured, and why" in the
# README for the retried-crash counterpart it is distinct from).
# measure_once() then writes that run's discardReason as "<backend>:
# refused this source (exit N)[: bench refused: <engine's own message>]"
# (see launch_reason's own comment; the "bench refused: <message>" tail is
# only present when stderr capture actually got a line, so it is
# optional). The backend name (the row's own label), the exit code, and
# the harness's "bench refused:" wrapper are this benchmark's own
# plumbing, a reader gets no use out of them. Anything not shaped like a
# refusal (a crash, a frame gate miss, a thermal discard) is returned
# unchanged, this only ever rewrites the one shape it recognizes.
_REFUSAL_RE = re.compile(
    r"^\w+: refused this source \(exit \d+\)(?::\s*bench refused:\s*(?P<message>.+))?$")


def _humanize_refusal_reason(fixture, reason):
    m = _REFUSAL_RE.match(reason or "")
    if not m:
        return reason
    message = m.group("message")
    return f"refuses {fixture}: {message}" if message else f"refuses {fixture}"


def _rehabilitate_cpu_power_only_discard(run):
    """A run discarded only because its CPU package power went negative
    after baseline subtraction is not a bad measurement for anything this
    renderer still publishes. GPU power, CPU load (process.cpuPercentMean)
    and RSS are sampled independently of the power-subtraction check that
    flagged it, and the table has no CPU power column for that check to
    matter to any more (see the module docstring). Rehabilitated to valid
    for every other field, with its own cpuPowerMw blanked to None so a
    physically impossible, discarded reading never feeds
    _cpu_power_spread_evidence: a value the run itself was thrown out for
    is not a real data point about how noisy CPU power is.

    Narrow on purpose: see _is_cpu_power_only_negative_discard for exactly
    which runs qualify. Anything else, frame gate, thermal, a crash, a
    missing report, a second offending field, is untouched and stays
    discarded for every column.
    """
    if not run.get("discarded") or not _is_cpu_power_only_negative_discard(run.get("discardReason")):
        return run
    power = dict(run.get("power") or {})
    power["cpuPowerMw"] = None
    return {**run, "discarded": False, "power": power}


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
    all_runs = [_rehabilitate_cpu_power_only_discard(r) for r in cell.pop("_runs")]
    valid = [r for r in all_runs if not r.get("discarded")]
    discarded = [r for r in all_runs if r.get("discarded")]
    # Every run in a cell shares the same fixture, cells are grouped by
    # (backend, fixture) in summarize(); read off any run rather than
    # threading fixture through as a second argument.
    fixture = all_runs[0]["fixture"] if all_runs else None

    cell["totalRuns"] = len(all_runs)
    cell["discardedCount"] = len(discarded)
    cell["discardedReasons"] = _unique(
        _humanize_refusal_reason(fixture, _humanize_discard_reason(r.get("discardReason")))
        for r in discarded)

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

    # cpuPowerMw is kept (median, and the raw per-repeat samples below) even
    # though it is not a table column: it is not published as a measurement
    # (see the module docstring), but the raw samples are what backs the
    # "not published, and here is why" note with a real number instead of
    # an assertion.
    cell["cpuPowerMw"] = med(lambda r: (r.get("power") or {}).get("cpuPowerMw"))
    cpu_power_samples = [(r.get("power") or {}).get("cpuPowerMw") for r in valid]
    cell["cpuPowerSamplesMw"] = [round(v) for v in cpu_power_samples if v is not None]

    cell["gpuPowerMw"] = med(lambda r: (r.get("power") or {}).get("gpuPowerMw"))
    cell["cpuPercent"] = med(lambda r: (r.get("process") or {}).get("cpuPercentMean"), 1)
    cell["rssMb"] = med(lambda r: (r.get("process") or {}).get("rssMbMean"))

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
    # The surface the engine rendered into, read back from the player itself.
    # A GPU figure only means something against the pixel count that produced
    # it, and these are not equal by construction: AppKit sizes windows in
    # points and renders at the backing scale, mpv's geometry is in pixels.
    cell["renderPixels"] = _unique(
        r.get("report", {}).get("renderPixels") for r in valid
        if r.get("report", {}).get("renderPixels"))
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

    surface = _one_or_varies(cell.get("renderPixels") or [],
                             "render surface not reported",
                             "render surface varies across repeats")
    line = f"- **{name}**: {frames}, {dropped}, {res}, {output_desc}, rendered into {surface}."
    paths = cell.get("servingPaths") or []
    if len(paths) == 1:
        line += f" Served via **{paths[0]}**."
    elif len(paths) > 1:
        line += f" Served via different paths across repeats ({', '.join(paths)}, see raw results)."
    return line


def _cpu_power_spread_evidence(table, fixture):
    """Finds the (display name, low mW, high mW, spread) for whichever
    backend had the widest run-to-run swing in raw CPU package power on
    this fixture, among cells with at least two valid samples to take a
    spread from at all. This is what lets the "CPU power is not published"
    note cite a real, checkable number instead of an unbacked assertion,
    and picking the widest (not the first, not a fixed backend) means the
    example is always this fixture's own worst case, not a cherry-pick:
    on hevc-4k-hdr10.mp4 that happens to be VLCKit, on the .mkv twin it is
    AetherEngine's own repeats that swing widest, so the note is not
    quietly excluding the engine it is meant to sell.
    """
    best = None
    for backend, name in NAMES.items():
        cell = table.get(backend, {}).get(fixture)
        if not cell or not cell.get("valid"):
            continue
        samples = cell.get("cpuPowerSamplesMw") or []
        if len(samples) < 2:
            continue
        lo, hi = min(samples), max(samples)
        spread = hi - lo
        if best is None or spread > best[3]:
            best = (name, lo, hi, spread)
    return best


def _cpu_power_limitation_note(table, fixture):
    """The stated limitation for the column this renderer deliberately does
    not draw. Backed by the actual observed spread for the fixture being
    rendered when there is one (see _cpu_power_spread_evidence), rather
    than only asserting the noise exists; the per-fixture example is the
    only number in this note, so it never asserts a magnitude the fixture
    actually being rendered might not back up (44 to 47 mW for AetherEngine
    on a 1080p fixture, versus hundreds of mW on the 4K ones, in the
    session this was written against, see the README's "Known gaps" for
    both figures side by side). The full reasoning (why the idle baseline
    drifts, why the column is dropped uniformly rather than kept where it
    happens to look stable) lives there too; this stays to one sentence of
    why plus the one number that backs it for this fixture.
    """
    evidence = _cpu_power_spread_evidence(table, fixture)
    example = ""
    if evidence:
        name, lo, hi, _spread = evidence
        example = f" For example, on {fixture} {name} measured {lo} to {hi} mW of CPU "
        example += "package power across its own repeats"
        if lo > 0:
            example += f", a {hi / lo:.1f}x range"
        example += "."
    return (
        "CPU package power was measured for every run but is not published above: the idle "
        "baseline drifts with load enough that one engine's own repeats can disagree more "
        f"than the column would be used to show between engines.{example} Recorded for every "
        "run in Results/; see \"Known gaps\" in the README for the full reasoning."
    )


def render(results, fixture="hevc-4k-hdr10.mkv"):
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

    # The window is described by what the players reported rendering into, not
    # by the size that was requested. Those differ: AppKit takes points and
    # renders at the backing scale. A hardcoded "1920x1080" here once described
    # a 3840x2160 surface for four engines and a 1920x1080 one for the fifth.
    surfaces = _unique(
        r.get("report", {}).get("renderPixels")
        for r in results.get("runs", [])
        if r.get("fixture") == fixture and not r.get("discarded")
        and r.get("report", {}).get("renderPixels"))
    if len(surfaces) == 1:
        window_desc = f"windowed, {surfaces[0]} px rendered"
    elif surfaces:
        window_desc = ("windowed, render surface NOT equal across engines ("
                       + ", ".join(sorted(surfaces)) + "), so the GPU column is not comparable")
    else:
        window_desc = "windowed, render surface not recorded by this session"
    lines.append(
        f"Measured on {machine_desc}, macOS {os_version}, "
        f"{fixture}, {window_desc}, {measure_s:.0f} s, median of {repeats}.")
    if machine_model:
        # "Fanless" only prints when there is a model identifier a reader
        # can check it against: an unbacked chassis/thermal-design claim
        # is exactly what the previous fix round removed from this header.
        # It also explains why the protocol has cooldowns and a throttling
        # discard rule, so it belongs next to the numbers, not buried in
        # the generic caveat list at the bottom.
        lines.append(
            f"{machine_model} is fanless: sustained decode can reach thermal pressure, which is "
            "why the protocol has cooldowns between runs and discards throttled windows.")

    lines += [
        "",
        "| | GPU power | CPU load | RSS | Plays |",
        "| --- | --- | --- | --- | --- |",
    ]

    footnotes = []
    detail_lines = []
    for backend, name in NAMES.items():
        cell = table.get(backend, {}).get(fixture)
        if not cell:
            lines.append(f"| **{name}** | | | | not run |")
            continue
        if not cell["valid"]:
            lines.append(f"| **{name}** | | | | n/a ({cell['reason']}) |")
            continue

        marker = ""
        if cell["discardedCount"]:
            footnotes.append(
                f"[{len(footnotes) + 1}] **{name}**, {fixture}: {cell['discardedCount']} of "
                f"{cell['totalRuns']} repeat(s) discarded and excluded from the figures above "
                f"({'; '.join(cell['discardedReasons'])}).")
            marker = f" [{len(footnotes)}]"

        lines.append(
            f"| **{name}**{marker} | {_fmt_mw(cell['gpuPowerMw'])} | "
            f"{_fmt_pct(cell['cpuPercent'])} of a core | {_fmt_mb(cell['rssMb'])} | Yes |")
        detail_lines.append(_format_detail(name, cell))

    lines.append("")
    if footnotes:
        lines.extend(footnotes)
        lines.append("")

    # Placed directly under the table it concerns, not down by the
    # versions/method notes, and scoped to this fixture only (see
    # _fixture_launch_failures's own docstring for why a session-wide sum
    # would misattribute failures to fixtures that never had any). What
    # counts as a launch failure versus a refusal, and why the two are
    # counted so differently, is spelled out in the README rather than
    # here, see the module docstring.
    launch_failures = _fixture_launch_failures(results.get("launchFailures") or {}, fixture)
    if launch_failures:
        fails = ", ".join(f"{name} {n}" for name, n in launch_failures)
        lines += [
            f"Launch failures on {fixture} (crashes that were retried, not refusals, see the "
            f"README): {fails}.",
            "",
        ]

    if detail_lines:
        lines.append(
            "Frame delivery and output (median; resolution, bit depth and HDR transfer are "
            "informational per engine only, shown as 'varies across repeats' when they "
            "disagree, see the README):")
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
        _cpu_power_limitation_note(table, fixture),
        "libmpv is measured with --hwdec=auto-safe (hardware decode), not mpv's own "
        "software-decode default, see \"Fairness decisions\" in the README.",
        # A session assembled from more than one run has to say so where the
        # numbers are read, not only inside the results file.
        *( [results["mergedFrom"]["note"]]
           if (results.get("mergedFrom") or {}).get("note") else [] ),
        "Method and raw results: https://github.com/superuser404notfound/aetherengine-bench",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        print(render(json.load(f)))
