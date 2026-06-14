"""Extract circuit metrics from Spectre PSF ASCII result files."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import numpy as np

from models import SimResult

logger = logging.getLogger(__name__)
_warned_missing_dependency = False


def parse_psf_results(raw_dir: Path, testbench_content: str) -> SimResult | None:
    """Read the analyses declared by a testbench from a Spectre raw directory.

    Returns None when psf_utils is unavailable or no matching PSF result can be
    read. The caller can then fall back to legacy text measurement parsing.
    """
    if not raw_dir.exists():
        return None

    global _warned_missing_dependency
    try:
        from psf_utils import PSF
    except ImportError:
        if not _warned_missing_dependency:
            logger.warning(
                "psf_utils is not installed; cannot read PSF ASCII results. "
                "Install dependencies from requirements.txt."
            )
            _warned_missing_dependency = True
        return None

    result = SimResult(converged=True)
    found_metrics = False

    ac_name = _analysis_name(testbench_content, "ac")
    if ac_name:
        ac_path = _find_analysis_file(raw_dir, ac_name, "ac")
        if ac_path:
            try:
                ac_psf = PSF(str(ac_path))
                gain_db, ugf_hz, phase_margin_deg = calculate_ac_metrics(
                    _signal_axis(ac_psf, ("vout", "V(vout)", "/vout"))
                )
                result.gain_db = gain_db
                # bandwidth_hz is retained as a legacy field name. This value
                # is the first 0 dB crossing (UGF, used as GBW), not -3 dB BW.
                result.bandwidth_hz = ugf_hz
                result.unity_gain_freq_hz = ugf_hz
                result.phase_margin_deg = phase_margin_deg
                result.raw_metrics.update(
                    {
                        "gain_dc": gain_db,
                        "gbw_hz": ugf_hz,
                        "phase_margin": phase_margin_deg,
                    }
                )
                found_metrics = True
            except Exception as exc:
                logger.warning("Failed to parse AC PSF result %s: %s", ac_path, exc)

        dc_name = _analysis_name(testbench_content, "dc")
        dc_path = _find_analysis_file(raw_dir, dc_name, "dc") if dc_name else None
        if dc_path:
            try:
                dc_psf = PSF(str(dc_path))
                power = _scalar_signal(
                    dc_psf,
                    ("VDDsrc:p", "VDDsrc:pwr", "VDDsrc:power"),
                )
                result.power_w = abs(power)
                result.raw_metrics["power_total"] = abs(power)
                found_metrics = True
            except Exception as exc:
                logger.warning("Failed to parse DC power from %s: %s", dc_path, exc)

    tran_name = _analysis_name(testbench_content, "tran")
    if tran_name:
        tran_path = _find_analysis_file(raw_dir, tran_name, "tran")
        if tran_path:
            try:
                tran_psf = PSF(str(tran_path))
                time, vinp = _signal_axis(
                    tran_psf, ("vinp", "V(vinp)", "/vinp")
                )
                vout_time, vout = _signal_axis(
                    tran_psf, ("vout", "V(vout)", "/vout")
                )
                if not np.array_equal(np.asarray(time), np.asarray(vout_time)):
                    raise ValueError("vinp and vout use different transient time axes")
                if tran_name.lower().startswith("st"):
                    rise_st, fall_st, settling_time = calculate_settling_times(
                        time, vinp, vout, tolerance=0.001
                    )
                    result.settling_time_s = settling_time
                    result.raw_metrics.update(
                        {
                            "settling_time_rise": rise_st,
                            "settling_time_fall": fall_st,
                            "settling_time": settling_time,
                            "settling_tolerance": 0.001,
                        }
                    )
                else:
                    sr_positive, sr_negative, slew_rate = calculate_slew_rates(
                        time, vinp, vout
                    )
                    result.slew_rate_positive_v_per_s = sr_positive
                    result.slew_rate_negative_v_per_s = sr_negative
                    result.slew_rate_v_per_s = slew_rate
                    result.raw_metrics.update(
                        {
                            "slew_rate_positive": sr_positive,
                            "slew_rate_negative": sr_negative,
                            "slew_rate": slew_rate,
                        }
                    )
                found_metrics = True
            except Exception as exc:
                logger.warning(
                    "Failed to parse transient PSF result %s: %s", tran_path, exc
                )

    return result if found_metrics else None


def calculate_ac_metrics(
    axis_and_values: tuple[Any, Any],
) -> tuple[float, float | None, float | None]:
    """Calculate low-frequency gain, first 0 dB crossing and phase margin."""
    frequency = np.asarray(axis_and_values[0], dtype=float)
    response = np.asarray(axis_and_values[1], dtype=complex)
    if frequency.size < 2 or response.size != frequency.size:
        raise ValueError("AC result must contain matching frequency and response arrays")

    order = np.argsort(frequency)
    frequency = frequency[order]
    response = response[order]
    magnitude = np.abs(response)
    if np.any(magnitude <= 0):
        magnitude = np.maximum(magnitude, np.finfo(float).tiny)

    gain_db = 20.0 * np.log10(magnitude)
    phase_deg = np.unwrap(np.angle(response)) * 180.0 / np.pi
    gain_dc = float(gain_db[0])

    crossing_indices = np.where((gain_db[:-1] >= 0.0) & (gain_db[1:] < 0.0))[0]
    if not crossing_indices.size:
        return gain_dc, None, None

    index = int(crossing_indices[0])
    log_frequency = np.log10(frequency)
    ugf_log = _linear_crossing(
        gain_db[index],
        gain_db[index + 1],
        log_frequency[index],
        log_frequency[index + 1],
    )
    ugf_hz = float(10.0**ugf_log)
    phase_at_ugf = float(
        np.interp(ugf_log, log_frequency, phase_deg)
    )
    while phase_at_ugf > 0.0:
        phase_at_ugf -= 360.0
    phase_margin_deg = 180.0 + phase_at_ugf

    return gain_dc, ugf_hz, phase_margin_deg


def calculate_slew_rates(
    time: Any,
    input_voltage: Any,
    output_voltage: Any,
) -> tuple[float, float, float]:
    """Calculate SR+, SR- and their worst case inside the output 10-90% range.

    Input midpoint crossings split the waveform into rising and falling
    response windows. The output slope is considered only while vout is
    between 10% and 90% of the input step, excluding unrelated spikes and
    ringing outside the large-signal transition region.
    """
    time_values = np.asarray(time, dtype=float)
    input_values = np.asarray(input_voltage, dtype=float)
    output_values = np.asarray(output_voltage, dtype=float)
    if (
        time_values.size < 3
        or input_values.size != time_values.size
        or output_values.size != time_values.size
    ):
        raise ValueError(
            "Transient result must contain matching time, input and output arrays"
        )
    if np.any(np.diff(time_values) <= 0):
        raise ValueError("Transient time axis must be strictly increasing")

    input_min = float(np.min(input_values))
    input_max = float(np.max(input_values))
    midpoint = 0.5 * (input_min + input_max)
    low_samples = input_values[input_values < midpoint]
    high_samples = input_values[input_values >= midpoint]
    if not low_samples.size or not high_samples.size:
        raise ValueError("Transient input does not contain both low and high levels")

    low_level = float(np.median(low_samples))
    high_level = float(np.median(high_samples))
    step = high_level - low_level
    if step <= 0:
        raise ValueError("Transient input step amplitude must be positive")

    low_10 = low_level + 0.1 * step
    high_90 = low_level + 0.9 * step
    rising_edges = np.where(
        (input_values[:-1] < midpoint) & (input_values[1:] >= midpoint)
    )[0]
    falling_edges = np.where(
        (input_values[:-1] >= midpoint) & (input_values[1:] < midpoint)
    )[0]
    all_edges = np.sort(np.concatenate((rising_edges, falling_edges)))
    derivative = np.gradient(output_values, time_values)

    positive_slopes: list[float] = []
    negative_slopes: list[float] = []
    for edge_index in all_edges:
        next_edges = all_edges[all_edges > edge_index]
        stop_index = int(next_edges[0] + 1) if next_edges.size else time_values.size
        indices = np.arange(edge_index, stop_index)
        in_output_range = (
            (output_values[indices] >= low_10)
            & (output_values[indices] <= high_90)
        )
        indices = indices[in_output_range]
        if not indices.size:
            continue

        if edge_index in rising_edges:
            slopes = derivative[indices]
            slopes = slopes[slopes > 0]
            if slopes.size:
                positive_slopes.append(float(np.max(slopes)))
        else:
            slopes = derivative[indices]
            slopes = slopes[slopes < 0]
            if slopes.size:
                negative_slopes.append(float(abs(np.min(slopes))))

    if not positive_slopes:
        raise ValueError("No valid rising 10-90% output transition found")
    if not negative_slopes:
        raise ValueError("No valid falling 90-10% output transition found")

    sr_positive = max(positive_slopes)
    sr_negative = max(negative_slopes)
    return sr_positive, sr_negative, min(sr_positive, sr_negative)


def calculate_settling_times(
    time: Any,
    input_voltage: Any,
    output_voltage: Any,
    tolerance: float = 0.001,
) -> tuple[float, float, float]:
    """Calculate rise, fall, and worst-case settling time.

    Each input midpoint crossing starts a response window. The settled output
    value is the median of the final 10% of that window. Settling is reached
    after the last sample outside ``tolerance * input_step`` from that value.
    """
    time_values = np.asarray(time, dtype=float)
    input_values = np.asarray(input_voltage, dtype=float)
    output_values = np.asarray(output_voltage, dtype=float)
    if (
        time_values.size < 10
        or input_values.size != time_values.size
        or output_values.size != time_values.size
    ):
        raise ValueError(
            "Transient result must contain matching time, input and output arrays"
        )
    if np.any(np.diff(time_values) <= 0):
        raise ValueError("Transient time axis must be strictly increasing")
    if tolerance <= 0:
        raise ValueError("Settling tolerance must be positive")

    input_min = float(np.min(input_values))
    input_max = float(np.max(input_values))
    midpoint = 0.5 * (input_min + input_max)
    low_samples = input_values[input_values < midpoint]
    high_samples = input_values[input_values >= midpoint]
    if not low_samples.size or not high_samples.size:
        raise ValueError("Transient input does not contain both low and high levels")

    input_step = float(np.median(high_samples) - np.median(low_samples))
    if input_step <= 0:
        raise ValueError("Transient input step amplitude must be positive")
    error_band = tolerance * input_step

    rising_edges = np.where(
        (input_values[:-1] < midpoint) & (input_values[1:] >= midpoint)
    )[0]
    falling_edges = np.where(
        (input_values[:-1] >= midpoint) & (input_values[1:] < midpoint)
    )[0]
    all_edges = np.sort(np.concatenate((rising_edges, falling_edges)))
    rise_times: list[float] = []
    fall_times: list[float] = []

    for edge_index in all_edges:
        next_edges = all_edges[all_edges > edge_index]
        stop_index = int(next_edges[0] + 1) if next_edges.size else time_values.size
        start_index = int(edge_index + 1)
        if stop_index - start_index < 10:
            continue

        window_length = stop_index - start_index
        tail_start = stop_index - max(5, int(window_length * 0.1))
        final_value = float(np.median(output_values[tail_start:stop_index]))
        error = np.abs(output_values[start_index:stop_index] - final_value)
        outside = np.where(error > error_band)[0]
        settle_index = start_index if not outside.size else start_index + int(outside[-1]) + 1
        if settle_index >= stop_index:
            continue

        edge_time = _interpolate_midpoint_crossing(
            time_values[edge_index],
            time_values[edge_index + 1],
            input_values[edge_index],
            input_values[edge_index + 1],
            midpoint,
        )
        settling_time = float(time_values[settle_index] - edge_time)
        if edge_index in rising_edges:
            rise_times.append(settling_time)
        else:
            fall_times.append(settling_time)

    if not rise_times:
        raise ValueError("No valid rising-edge settling response found")
    if not fall_times:
        raise ValueError("No valid falling-edge settling response found")

    rise_settling = max(rise_times)
    fall_settling = max(fall_times)
    return rise_settling, fall_settling, max(rise_settling, fall_settling)


def _analysis_name(testbench_content: str, analysis_type: str) -> str | None:
    pattern = rf"(?m)^\s*(\w+)\s+{re.escape(analysis_type)}\b"
    match = re.search(pattern, testbench_content, re.IGNORECASE)
    return match.group(1) if match else None


def _find_analysis_file(
    raw_dir: Path, analysis_name: str | None, suffix: str
) -> Path | None:
    if not analysis_name or not raw_dir.exists():
        return None
    exact = raw_dir / f"{analysis_name}.{suffix}"
    if exact.exists():
        return exact
    matches = list(raw_dir.rglob(f"{analysis_name}.{suffix}"))
    return matches[0] if matches else None


def _signal_axis(psf: Any, candidates: tuple[str, ...]) -> tuple[Any, Any]:
    signal = _get_signal(psf, candidates)
    return signal.abscissa, signal.ordinate


def _scalar_signal(psf: Any, candidates: tuple[str, ...]) -> float:
    signal = _get_signal(psf, candidates)
    values = np.asarray(signal.ordinate)
    if values.size == 0:
        raise ValueError("PSF signal contains no values")
    return float(np.real(values.reshape(-1)[0]))


def _get_signal(psf: Any, candidates: tuple[str, ...]) -> Any:
    available = list(psf.all_signals())
    normalized = {_normalize_signal_name(str(name)): name for name in available}
    for candidate in candidates:
        actual_name = normalized.get(_normalize_signal_name(candidate))
        if actual_name is not None:
            return psf.get_signal(actual_name)
    raise KeyError(
        f"None of {candidates} found in PSF signals: "
        f"{', '.join(str(name) for name in available[:20])}"
    )


def _normalize_signal_name(name: str) -> str:
    return name.strip().lower().replace("/", "").replace("(", "").replace(")", "")


def _linear_crossing(y0: float, y1: float, x0: float, x1: float) -> float:
    if y1 == y0:
        return x0
    return x0 + (0.0 - y0) * (x1 - x0) / (y1 - y0)


def _interpolate_midpoint_crossing(
    t0: float, t1: float, v0: float, v1: float, midpoint: float
) -> float:
    if v1 == v0:
        return t0
    return t0 + (midpoint - v0) * (t1 - t0) / (v1 - v0)
