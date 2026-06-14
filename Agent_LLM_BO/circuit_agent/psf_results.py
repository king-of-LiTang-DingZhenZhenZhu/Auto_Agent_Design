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
                time, vout = _signal_axis(
                    tran_psf, ("vout", "V(vout)", "/vout")
                )
                slew_rate = calculate_slew_rate(time, vout)
                result.slew_rate_v_per_s = slew_rate
                result.raw_metrics["slew_rate"] = slew_rate
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


def calculate_slew_rate(time: Any, voltage: Any) -> float:
    """Return the maximum absolute dV/dt from a transient waveform."""
    time_values = np.asarray(time, dtype=float)
    voltage_values = np.asarray(voltage, dtype=float)
    if time_values.size < 2 or voltage_values.size != time_values.size:
        raise ValueError("Transient result must contain matching time and voltage arrays")
    return float(np.max(np.abs(np.gradient(voltage_values, time_values))))


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
