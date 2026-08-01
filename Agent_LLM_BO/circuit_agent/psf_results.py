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

    stb_name = _analysis_name(testbench_content, "stb")
    if stb_name:
        stb_path = _find_analysis_file(raw_dir, stb_name, "stb")
        if stb_path:
            try:
                stb_psf = PSF(str(stb_path))
                gain_db, ugf_hz, phase_margin_deg = calculate_ac_metrics(
                    _signal_axis(
                        stb_psf,
                        ("loopGain", "loopgain", "loop_gain"),
                    )
                )
                result.gain_db = gain_db
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
                return result
            except Exception as exc:
                logger.warning("Failed to parse STB result %s: %s", stb_path, exc)

    ac_name = _analysis_name(testbench_content, "ac")
    if ac_name and ac_name.lower().startswith("ldopsr"):
        ac_path = _find_analysis_file(raw_dir, ac_name, "ac")
        if ac_path:
            try:
                ac_psf = PSF(str(ac_path))
                dc_psr_db = calculate_dc_psr_db(
                    _signal_axis(ac_psf, ("vout", "V(vout)", "/vout"))
                )
                result.raw_metrics["dc_psr_db"] = dc_psr_db
                return result
            except Exception as exc:
                logger.warning("Failed to parse LDO PSR result %s: %s", ac_path, exc)

    if ac_name and ac_name.lower().startswith("psrr"):
        ac_path = _find_analysis_file(raw_dir, ac_name, "ac")
        if ac_path:
            try:
                ac_psf = PSF(str(ac_path))
                psrr_db = calculate_psrr_db(
                    _signal_axis(ac_psf, ("vout", "V(vout)", "/vout"))
                )
                result.psrr_db = psrr_db
                result.raw_metrics["psrr_db"] = psrr_db
                op_name = _analysis_name(testbench_content, "dc")
                op_path = _find_analysis_file(raw_dir, op_name, "dc")
                if op_path:
                    op_psf = PSF(str(op_path))
                    power = abs(
                        _scalar_signal(
                            op_psf,
                            ("VDDsrc:p", "VDDsrc:pwr", "VDDsrc:power"),
                        )
                    )
                    result.power_w = power
                    result.raw_metrics["power_total"] = power
                return result
            except Exception as exc:
                logger.warning("Failed to parse PSRR result %s: %s", ac_path, exc)

    dc_name = _analysis_name(testbench_content, "dc")
    if dc_name and dc_name.lower().startswith("loadsweep"):
        dc_path = _find_analysis_file(raw_dir, dc_name, "dc")
        if dc_path:
            try:
                dc_psf = PSF(str(dc_path))
                output_voltage, load_regulation = calculate_load_regulation(
                    _signal_axis(dc_psf, ("vout", "V(vout)", "/vout"))
                )
                result.raw_metrics.update(
                    {
                        "output_voltage_v": output_voltage,
                        "load_regulation_v_per_a": load_regulation,
                    }
                )
                return result
            except Exception as exc:
                logger.warning(
                    "Failed to parse LDO load-regulation result %s: %s",
                    dc_path,
                    exc,
                )

    if dc_name and dc_name.lower().startswith("tempsweep"):
        dc_path = _find_analysis_file(raw_dir, dc_name, "dc")
        if dc_path:
            try:
                dc_psf = PSF(str(dc_path))
                vref, tempco, nonlinearity = calculate_temperature_metrics(
                    _signal_axis(dc_psf, ("vout", "V(vout)", "/vout"))
                )
                result.vref_v = vref
                result.tempco_ppm_per_c = tempco
                result.vref_temp_nonlinearity_v = nonlinearity
                result.raw_metrics.update(
                    {
                        "vref_v": vref,
                        "tempco_ppm_per_c": tempco,
                        "vref_temp_nonlinearity_v": nonlinearity,
                    }
                )
                return result
            except Exception as exc:
                logger.warning(
                    "Failed to parse temperature result %s: %s", dc_path, exc
                )

    if dc_name and dc_name.lower().startswith("linesweep"):
        dc_path = _find_analysis_file(raw_dir, dc_name, "dc")
        if dc_path:
            try:
                dc_psf = PSF(str(dc_path))
                line_regulation = calculate_line_regulation(
                    _signal_axis(dc_psf, ("vout", "V(vout)", "/vout"))
                )
                result.line_regulation_v_per_v = line_regulation
                result.raw_metrics["line_regulation_v_per_v"] = line_regulation
                return result
            except Exception as exc:
                logger.warning(
                    "Failed to parse line-regulation result %s: %s", dc_path, exc
                )

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
                if tran_name.lower().startswith("decision"):
                    time, clock = _signal_axis(
                        tran_psf, ("clk", "V(clk)", "/clk")
                    )
                    _outp_time, outp = _signal_axis(
                        tran_psf, ("outp", "V(outp)", "/outp")
                    )
                    _outn_time, outn = _signal_axis(
                        tran_psf, ("outn", "V(outn)", "/outn")
                    )
                    _vdd_time, vdd = _signal_axis(
                        tran_psf, ("vdd", "V(vdd)", "/vdd")
                    )
                    _power_time, supply_power = _signal_axis(
                        tran_psf,
                        ("VDDsrc:p", "VDDsrc:pwr", "VDDsrc:power"),
                    )
                    positive = "pos" in tran_name.lower()
                    margin, delay, energy, average_power = (
                        calculate_comparator_decision_metrics(
                            time,
                            clock,
                            outp,
                            outn,
                            vdd,
                            supply_power,
                            expect_positive=positive,
                        )
                    )
                    polarity = "positive" if positive else "negative"
                    result.raw_metrics.update({
                        f"decision_{polarity}_margin_v": margin,
                        f"propagation_delay_{polarity}_s": delay,
                    })
                    if positive:
                        result.raw_metrics["energy_per_decision_j"] = energy
                        result.power_w = average_power
                        result.raw_metrics["power_total"] = average_power
                    return result
                if tran_name.lower().startswith("startup"):
                    time, vdd = _signal_axis(
                        tran_psf, ("vdd", "V(vdd)", "/vdd")
                    )
                    vout_time, vout = _signal_axis(
                        tran_psf, ("vout", "V(vout)", "/vout")
                    )
                    if not np.array_equal(np.asarray(time), np.asarray(vout_time)):
                        raise ValueError("vdd and vout use different time axes")
                    success, startup_time, vref = calculate_startup_metrics(
                        time, vdd, vout
                    )
                    result.startup_success = success
                    result.startup_time_s = startup_time
                    result.vref_v = vref
                    result.raw_metrics.update(
                        {
                            "startup_success": float(success),
                            "startup_time_s": startup_time,
                            "vref_v": vref,
                        }
                    )
                    return result
                if tran_name.lower().startswith("loadtran"):
                    time, load_current = _signal_axis(
                        tran_psf,
                        (
                            "ILOADsrc:p",
                            "ILOADsrc:i",
                            "ILOADsrc",
                            "load_current",
                        ),
                    )
                    vout_time, vout = _signal_axis(
                        tran_psf, ("vout", "V(vout)", "/vout")
                    )
                    if not np.array_equal(np.asarray(time), np.asarray(vout_time)):
                        raise ValueError(
                            "load current and vout use different time axes"
                        )
                    overshoot, undershoot = calculate_load_transient_metrics(
                        time, load_current, vout
                    )
                    result.raw_metrics.update(
                        {
                            "overshoot_v": overshoot,
                            "undershoot_v": undershoot,
                        }
                    )
                    return result
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


def calculate_comparator_decision_metrics(
    time: Any,
    clock: Any,
    outp: Any,
    outn: Any,
    supply_voltage: Any,
    supply_power: Any,
    *,
    expect_positive: bool,
) -> tuple[float, float, float, float]:
    """Return decision margin, clock-to-decision delay, energy, and power.

    The decision margin is sampled near the end of the first evaluation phase.
    Delay is measured from the first rising clock midpoint to a differential
    output of half the supply. Energy is integrated over one complete clock
    period so that both evaluation and the following precharge are included.
    """
    t, clk, vp, vn, vdd, power = _matching_real_arrays(
        time, clock, outp, outn, supply_voltage, supply_power
    )
    if np.any(np.diff(t) <= 0):
        raise ValueError("Comparator transient time must be strictly increasing")

    clock_midpoint = 0.5 * (float(np.min(clk)) + float(np.max(clk)))
    rising_edges = np.flatnonzero(
        (clk[:-1] < clock_midpoint) & (clk[1:] >= clock_midpoint)
    )
    falling_edges = np.flatnonzero(
        (clk[:-1] >= clock_midpoint) & (clk[1:] < clock_midpoint)
    )
    if not rising_edges.size or not falling_edges.size:
        raise ValueError("Comparator clock needs rising and falling edges")

    rise_index = int(rising_edges[0])
    following_falls = falling_edges[falling_edges > rise_index]
    if not following_falls.size:
        raise ValueError("Comparator evaluation phase has no falling clock edge")
    fall_index = int(following_falls[0])
    if fall_index - rise_index < 3:
        raise ValueError("Comparator evaluation phase has too few samples")

    signed_difference = vp - vn if expect_positive else vn - vp
    tail_count = max(3, int(np.ceil(0.1 * (fall_index - rise_index))))
    decision_margin = float(np.median(
        signed_difference[fall_index - tail_count : fall_index + 1]
    ))

    clock_edge_time = _interpolate_midpoint_crossing(
        t[rise_index],
        t[rise_index + 1],
        clk[rise_index],
        clk[rise_index + 1],
        clock_midpoint,
    )
    decision_threshold = 0.5 * float(np.median(vdd[rise_index:fall_index + 1]))
    crossings = np.flatnonzero(
        signed_difference[rise_index + 1 : fall_index + 1]
        >= decision_threshold
    )
    if crossings.size:
        decision_index = rise_index + 1 + int(crossings[0])
        propagation_delay = max(0.0, float(t[decision_index] - clock_edge_time))
    else:
        propagation_delay = float(t[fall_index] - clock_edge_time)

    later_rises = rising_edges[rising_edges > rise_index]
    cycle_stop = int(later_rises[0] + 1) if later_rises.size else fall_index + 1
    cycle_time = t[rise_index:cycle_stop]
    cycle_power = np.abs(power[rise_index:cycle_stop])
    if cycle_time.size < 2:
        raise ValueError("Comparator energy window has too few samples")
    energy = float(np.trapezoid(cycle_power, cycle_time))
    period = float(cycle_time[-1] - cycle_time[0])
    average_power = energy / period if period > 0 else float("inf")
    return decision_margin, propagation_delay, energy, average_power


def calculate_startup_metrics(
    time: Any,
    supply_voltage: Any,
    output_voltage: Any,
    tolerance: float = 0.01,
) -> tuple[bool, float, float]:
    """Return startup success, settling time, and final Vref."""
    time_array, supply, output = _matching_real_arrays(
        time, supply_voltage, output_voltage
    )
    tail_count = max(3, int(np.ceil(output.size * 0.05)))
    final_vref = float(np.median(output[-tail_count:]))
    final_supply = float(np.median(supply[-tail_count:]))
    start_candidates = np.flatnonzero(supply >= 0.01 * final_supply)
    if start_candidates.size == 0:
        return False, float(time_array[-1] - time_array[0]), final_vref

    start_index = int(start_candidates[0])
    error_limit = max(abs(final_vref) * tolerance, 1e-6)
    outside = np.flatnonzero(
        np.abs(output[start_index:] - final_vref) > error_limit
    )
    settle_index = start_index if outside.size == 0 else start_index + int(outside[-1]) + 1
    settled = settle_index < output.size
    startup_time = float(
        time_array[min(settle_index, output.size - 1)] - time_array[start_index]
    )
    escaped_zero_state = final_vref > max(0.1, 0.1 * final_supply)
    return bool(settled and escaped_zero_state), startup_time, final_vref


def calculate_psrr_db(axis_and_values: tuple[Any, Any]) -> float:
    """Return worst-case PSRR for a 1 V AC ripple applied at VDD."""
    _axis, response = axis_and_values
    magnitude = np.abs(np.asarray(response, dtype=complex))
    magnitude = magnitude[np.isfinite(magnitude)]
    if magnitude.size == 0:
        raise ValueError("PSRR response is empty")
    magnitude = np.maximum(magnitude, np.finfo(float).tiny)
    return float(np.min(-20.0 * np.log10(magnitude)))


def calculate_dc_psr_db(axis_and_values: tuple[Any, Any]) -> float:
    """Return the near-DC supply-to-output transfer in dB."""
    frequency = np.asarray(axis_and_values[0], dtype=float)
    response = np.asarray(axis_and_values[1], dtype=complex)
    if frequency.size == 0 or response.size != frequency.size:
        raise ValueError("PSR result must contain matching frequency and response")
    finite = np.isfinite(frequency) & np.isfinite(response)
    if not np.any(finite):
        raise ValueError("PSR response is empty")
    frequency = frequency[finite]
    response = response[finite]
    index = int(np.argmin(frequency))
    magnitude = max(abs(response[index]), np.finfo(float).tiny)
    return float(20.0 * np.log10(magnitude))


def calculate_load_regulation(
    axis_and_values: tuple[Any, Any],
) -> tuple[float, float]:
    """Return no-load output voltage and peak-to-peak load regulation."""
    load_current, output = _matching_real_arrays(*axis_and_values)
    span = float(np.ptp(load_current))
    if span <= 0:
        raise ValueError("Load-current sweep must span more than one value")
    no_load_index = int(np.argmin(np.abs(load_current)))
    return float(output[no_load_index]), float(np.ptp(output) / span)


def calculate_load_transient_metrics(
    time: Any,
    load_current: Any,
    output_voltage: Any,
) -> tuple[float, float]:
    """Return worst load-release overshoot and load-step undershoot."""
    time_values, load_values, output = _matching_real_arrays(
        time, load_current, output_voltage
    )
    if np.any(np.diff(time_values) <= 0):
        raise ValueError("Transient time axis must be strictly increasing")

    midpoint = 0.5 * (float(np.min(load_values)) + float(np.max(load_values)))
    rising_edges = np.flatnonzero(
        (load_values[:-1] < midpoint) & (load_values[1:] >= midpoint)
    )
    falling_edges = np.flatnonzero(
        (load_values[:-1] >= midpoint) & (load_values[1:] < midpoint)
    )
    if not rising_edges.size or not falling_edges.size:
        raise ValueError("Load transient needs both rising and falling edges")

    all_edges = np.sort(np.concatenate((rising_edges, falling_edges)))
    overshoots: list[float] = []
    undershoots: list[float] = []
    for edge_index in all_edges:
        previous_edge = all_edges[all_edges < edge_index]
        start = int(previous_edge[-1] + 1) if previous_edge.size else 0
        pre_window = output[start : edge_index + 1]
        if pre_window.size < 2:
            continue
        pre_count = max(2, int(np.ceil(pre_window.size * 0.1)))
        baseline = float(np.median(pre_window[-pre_count:]))

        next_edge = all_edges[all_edges > edge_index]
        stop = int(next_edge[0] + 1) if next_edge.size else output.size
        response = output[edge_index + 1 : stop]
        if response.size < 2:
            continue
        if edge_index in rising_edges:
            undershoots.append(max(0.0, baseline - float(np.min(response))))
        else:
            tail_count = max(2, int(np.ceil(response.size * 0.1)))
            settled = float(np.median(response[-tail_count:]))
            overshoots.append(max(0.0, float(np.max(response)) - settled))

    if not overshoots or not undershoots:
        raise ValueError("No valid load-step response windows found")
    return max(overshoots), max(undershoots)


def calculate_temperature_metrics(
    axis_and_values: tuple[Any, Any],
) -> tuple[float, float, float]:
    """Return Vref at 27 C, peak-to-peak tempco, and linear-fit residual."""
    temperature, vref = _matching_real_arrays(*axis_and_values)
    span = float(np.ptp(temperature))
    if span <= 0:
        raise ValueError("Temperature sweep must span more than one value")
    nominal_index = int(np.argmin(np.abs(temperature - 27.0)))
    nominal_vref = float(vref[nominal_index])
    if abs(nominal_vref) <= np.finfo(float).tiny:
        raise ValueError("Nominal Vref is zero")
    tempco = float(np.ptp(vref) / abs(nominal_vref) / span * 1e6)
    fit = np.polyval(np.polyfit(temperature, vref, 1), temperature)
    nonlinearity = float(np.max(np.abs(vref - fit)))
    return nominal_vref, tempco, nonlinearity


def calculate_line_regulation(axis_and_values: tuple[Any, Any]) -> float:
    """Return absolute peak-to-peak Vref change divided by VDD span."""
    supply, vref = _matching_real_arrays(*axis_and_values)
    span = float(np.ptp(supply))
    if span <= 0:
        raise ValueError("VDD sweep must span more than one value")
    return float(np.ptp(vref) / span)


def _matching_real_arrays(*values: Any) -> tuple[np.ndarray, ...]:
    arrays = tuple(np.asarray(value, dtype=float) for value in values)
    if not arrays or arrays[0].size < 2:
        raise ValueError("Metric calculation needs at least two samples")
    if any(array.shape != arrays[0].shape for array in arrays[1:]):
        raise ValueError("Metric arrays must have matching shapes")
    if any(not np.all(np.isfinite(array)) for array in arrays):
        raise ValueError("Metric arrays contain non-finite values")
    order = np.argsort(arrays[0])
    return tuple(array[order] for array in arrays)


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
    # psf_utils stores the sweep axis (frequency, time) on the PSF object,
    # not on individual signals (signal.abscissa is always None).
    sweep = psf.get_sweep()
    return sweep.abscissa, signal.ordinate


def _scalar_signal(psf: Any, candidates: tuple[str, ...]) -> float:
    signal = _get_signal(psf, candidates)
    values = np.asarray(signal.ordinate)
    if values.size == 0:
        raise ValueError("PSF signal contains no values")
    return float(np.real(values.reshape(-1)[0]))


def _get_signal(psf: Any, candidates: tuple[str, ...]) -> Any:
    available = list(psf.all_signals())
    # available contains Signal objects; use .name attribute for comparison
    # and store signal name (str) as the lookup key for psf.get_signal()
    normalized = {
        _normalize_signal_name(sig.name): sig.name for sig in available
    }
    for candidate in candidates:
        matched_name = normalized.get(_normalize_signal_name(candidate))
        if matched_name is not None:
            return psf.get_signal(matched_name)
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
