#!/usr/bin/env python3
"""Samples system power and per-process CPU/RSS around a benchmark run.

powermetrics on Apple Silicon reports system-wide package power, not per
process, so callers must subtract an idle baseline measured on the same
quiet machine before attributing any of this to one engine. Per-process
CPU and RSS come from ps and are properly attributable to a pid.

Every function here follows one rule: a parse that fails is raised, never
turned into a plausible-looking 0.0 or an empty aggregate. A missing or
malformed reading is exactly as informative as a crash, and far less
dangerous than a number that looks real but is not one.
"""
import os
import re
import subprocess
import time

POWERMETRICS_BLOCK_HEADER = "*** Sampled system activity"

# Canonical order, least to most severe, per `man powermetrics` / observed
# `**** Thermal pressure ****` output. Anything outside this set is treated
# as unrecognized rather than silently ranked, see _thermal_severity.
_THERMAL_LEVELS = ["Nominal", "Moderate", "Heavy", "Trapping", "Sleeping"]

# ps formats %cpu using the process locale's decimal separator (this machine
# runs de_DE.UTF-8, where ps prints "0,0" for zero percent, which float()
# rejects). Force the C locale on every subprocess call here so a comma
# decimal separator can never masquerade as a parse failure, or worse, get
# silently truncated by a permissive parser into the wrong number.
_C_LOCALE_ENV = dict(os.environ, LC_ALL="C", LC_NUMERIC="C")


def _thermal_severity(level):
    """Index into _THERMAL_LEVELS, or raise if powermetrics reported a word
    we don't recognize. Ranking an unknown level would either silently
    treat it as mild (if it sorts low) or drop a real excursion (if it
    sorts high); neither is acceptable, so an unrecognized level is loud."""
    try:
        return _THERMAL_LEVELS.index(level)
    except ValueError:
        raise ValueError(
            f"parse_powermetrics: unrecognized thermal pressure level {level!r}, "
            f"expected one of {_THERMAL_LEVELS}"
        )


def parse_powermetrics(text):
    """Parse one powermetrics sample block (the text between two
    POWERMETRICS_BLOCK_HEADER markers, or a synthetic block in tests).

    "CPU Power" is the one field every real cpu_power sample carries; its
    absence means the block is truncated or the wrong sampler was used, so
    that raises. GPU/ANE power and the two cluster residencies come back as
    None (never 0.0) when the block genuinely does not mention them, so a
    caller can tell "not reported" from "reported as zero".

    powermetrics never emits the literal word "throttled" in any sampler;
    the real signal is the `thermal` sampler's "Current pressure level:"
    line. thermalPressure is that raw level string, or None if this block
    has no thermal reading at all (e.g. the thermal sampler was not
    requested). throttled is derived as "a level was reported and it is not
    Nominal": it is False both when the machine is cool AND when we simply
    have no thermal data, so a caller that only checks throttled can be
    fooled into treating "unknown" as "cool". thermalPressure is what makes
    that distinction visible; check it, not just throttled, before trusting
    a run as thermally clean.
    """

    def find(pattern):
        m = re.search(pattern, text)
        return float(m.group(1)) if m else None

    cpu_power = find(r"CPU Power:\s*([\d.]+)\s*mW")
    if cpu_power is None:
        raise ValueError(
            "parse_powermetrics: no 'CPU Power' reading in this block; "
            "malformed or truncated powermetrics output"
        )

    thermal_match = re.search(r"Current pressure level:\s*(\w+)", text)
    thermal_pressure = thermal_match.group(1) if thermal_match else None
    if thermal_pressure is not None:
        _thermal_severity(thermal_pressure)  # raises on an unrecognized level

    return {
        "cpuPowerMw": cpu_power,
        "gpuPowerMw": find(r"GPU Power:\s*([\d.]+)\s*mW"),
        "anePowerMw": find(r"ANE Power:\s*([\d.]+)\s*mW"),
        "eClusterResidency": find(r"E-Cluster HW active residency:\s*([\d.]+)%"),
        "pClusterResidency": find(r"P-Cluster HW active residency:\s*([\d.]+)%"),
        "thermalPressure": thermal_pressure,
        "throttled": thermal_pressure is not None and thermal_pressure != "Nominal",
    }


def _mean_or_none(values):
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


def _worst_thermal_pressure(levels):
    """Worst (most severe) level across a window, or None if none of the
    blocks carried a thermal reading at all. A run that dips out of Nominal
    even briefly must stay identifiable in the aggregate, so this is a max
    by severity, not a mean or a last-value."""
    present = [l for l in levels if l is not None]
    if not present:
        return None
    return max(present, key=_thermal_severity)


def _aggregate_power_output(raw_output):
    """Split raw `powermetrics` stdout into per-sample blocks, parse each,
    and average. Pure function of the text, so it is testable without root
    or a live powermetrics call; sample_power is a thin wrapper that feeds
    it real subprocess output."""
    blocks_text = [b for b in raw_output.split(POWERMETRICS_BLOCK_HEADER) if "CPU Power" in b]
    if not blocks_text:
        raise RuntimeError(
            "sample_power: powermetrics produced no parseable samples "
            "(no 'CPU Power' block found in its output); check sudo access "
            "to /usr/bin/powermetrics"
        )
    blocks = [parse_powermetrics(b) for b in blocks_text]
    worst_pressure = _worst_thermal_pressure([b["thermalPressure"] for b in blocks])
    return {
        "cpuPowerMw": _mean_or_none([b["cpuPowerMw"] for b in blocks]),
        "gpuPowerMw": _mean_or_none([b["gpuPowerMw"] for b in blocks]),
        "anePowerMw": _mean_or_none([b["anePowerMw"] for b in blocks]),
        "eClusterResidency": _mean_or_none([b["eClusterResidency"] for b in blocks]),
        "pClusterResidency": _mean_or_none([b["pClusterResidency"] for b in blocks]),
        "thermalPressure": worst_pressure,
        "throttled": worst_pressure is not None and worst_pressure != "Nominal",
        "samples": len(blocks),
    }


def sample_power(seconds, interval_ms=1000):
    """Run `sudo powermetrics --samplers cpu_power,thermal` for
    approximately `seconds` and return the averaged package power / cluster
    residency, plus the worst thermal pressure level seen.

    This is system-wide, not per process: subtract an idle baseline
    captured on the same quiet machine before attributing it to a player.

    Shells out to `sudo powermetrics` specifically (not `sudo python3 ...`),
    matching the sudoers NOPASSWD grant that is scoped to the
    /usr/bin/powermetrics binary. stdin is closed so a missing or
    misconfigured grant fails immediately with sudo's own error instead of
    hanging on a password prompt that will never arrive in an unattended
    run; nothing here works around that gate. `timeout` bounds the call so a
    hung powermetrics cannot stall an unattended multi-hour session
    indefinitely; the deadline is seconds + 30 to leave headroom for
    process startup and its own final flush.
    """
    if seconds <= 0:
        raise ValueError(f"sample_power: seconds must be positive, got {seconds}")
    try:
        interval_ms_int = int(interval_ms)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"sample_power: interval_ms must be an integer number of milliseconds, got {interval_ms!r}"
        ) from e
    if interval_ms_int != interval_ms:
        raise ValueError(
            f"sample_power: interval_ms must be a whole number of milliseconds, got {interval_ms!r}"
        )
    if interval_ms_int <= 0:
        raise ValueError(f"sample_power: interval_ms must be positive, got {interval_ms}")
    interval_ms = interval_ms_int

    n = int(seconds * 1000 / interval_ms)
    if n < 1:
        raise ValueError(
            f"sample_power: seconds={seconds} and interval_ms={interval_ms} "
            "would request fewer than one powermetrics sample"
        )

    try:
        result = subprocess.run(
            ["sudo", "powermetrics", "--samplers", "cpu_power,thermal", "-i", str(interval_ms), "-n", str(n)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=_C_LOCALE_ENV,
            timeout=seconds + 30,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"sample_power: `sudo powermetrics` did not finish within {seconds + 30}s (hung?)"
        ) from e
    if result.returncode != 0:
        raise RuntimeError(
            "sample_power: `sudo powermetrics` exited "
            f"{result.returncode}: {result.stderr.strip()!r}. Check whether "
            "the powermetrics sudoers NOPASSWD entry is installed for this user."
        )
    return _aggregate_power_output(result.stdout)


def aggregate_process(samples):
    """samples is a list of (cpuPercent, rssMb) tuples. Raises on an empty
    list instead of dividing by zero into a number that looks measured."""
    if not samples:
        raise ValueError("aggregate_process: no samples to aggregate")
    cpus = [c for c, _ in samples]
    rss = [r for _, r in samples]
    return {
        "cpuPercentMean": sum(cpus) / len(cpus),
        "cpuPercentMax": max(cpus),
        "rssMbMean": sum(rss) / len(rss),
        "rssMbPeak": max(rss),
    }


def _read_ps(pid):
    """One ps reading for pid: (cpuPercent, rssMb), or None if ps could not
    find the process at all (nonzero exit or empty stdout). A non-empty
    line that fails to parse into the expected fields is a different,
    louder problem (unexpected ps output shape) and raises immediately
    rather than being folded into the "process not found" case.

    Also asks ps for `state`: a killed-but-not-yet-reaped child stays in
    the process table as a zombie ('Z') and keeps answering with a row
    (0.0 cpu, 0 rss, still responds to os.kill(pid, 0)) until its parent
    calls wait() on it. Left unchecked that reads as a real, if idle,
    measurement instead of what it actually is, the process has already
    exited. A zombie row raises immediately rather than being averaged in.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "%cpu=,rss=,state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=_C_LOCALE_ENV,
            timeout=10,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"sample_process: `ps` did not respond within 10s for pid {pid} (hung?)") from e
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        return None
    parts = out.split()
    if len(parts) != 3:
        raise RuntimeError(f"sample_process: unexpected ps output for pid {pid}: {out!r}")
    cpu_str, rss_kb_str, state = parts
    if state.startswith("Z"):
        raise RuntimeError(
            f"sample_process: pid {pid} is a zombie (already exited, not yet "
            "reaped by its parent); not a live reading"
        )
    try:
        cpu = float(cpu_str)
        rss_kb = float(rss_kb_str)
    except ValueError as e:
        raise RuntimeError(f"sample_process: unparseable ps output for pid {pid}: {out!r}") from e
    return cpu, rss_kb / 1024.0


def _diagnose_pid_absence(pid):
    """Best-effort explanation for why _read_ps came back empty, using
    os.kill(pid, 0) to distinguish "gone" from "exists but not ours to see"."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "process does not exist (exited, or never started)"
    except PermissionError:
        return "process exists but is not signalable by this sampler (likely owned by another user)"
    except OverflowError:
        return "pid is out of the valid range"
    else:
        return "process exists per os.kill, but ps returned no row for it"


def sample_process(pid, seconds, interval=1.0):
    """Sample %cpu and RSS for pid roughly once per `interval` seconds over
    a window of `seconds`, and aggregate.

    A pid that never produces a reading, or one that stops responding
    partway through the window (the player crashed, or orchestrate.py's
    `sudo -u` player process died mid-measurement), raises rather than
    returning an aggregate built from a shorter window than the caller
    asked for. A short, silently-truncated window would understate the
    real cost and look exactly like a clean full-window measurement.
    """
    if seconds <= 0:
        raise ValueError(f"sample_process: seconds must be positive, got {seconds}")
    if interval <= 0:
        raise ValueError(f"sample_process: interval must be positive, got {interval}")
    if interval > seconds:
        raise ValueError(
            f"sample_process: interval ({interval}s) must not exceed seconds "
            f"({seconds}s), or the sampling window would silently overshoot "
            "what the caller asked for"
        )
    try:
        pid = int(pid)
    except (TypeError, ValueError) as e:
        raise ValueError(f"sample_process: pid must be an int, got {pid!r}") from e
    if pid <= 0:
        raise ValueError(f"sample_process: pid must be positive, got {pid}")

    samples = []
    deadline = time.time() + seconds
    while True:
        reading = _read_ps(pid)
        if reading is None:
            reason = _diagnose_pid_absence(pid)
            if samples:
                raise RuntimeError(
                    f"sample_process: pid {pid} stopped responding after "
                    f"{len(samples)} sample(s), before the {seconds}s window "
                    f"elapsed ({reason})"
                )
            raise RuntimeError(f"sample_process: pid {pid} produced no reading ({reason})")
        samples.append(reading)
        if time.time() >= deadline:
            break
        time.sleep(interval)
    return aggregate_process(samples)
