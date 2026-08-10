"""Evaluate passive PCell geometry through Virtuoso CDF callbacks.

This module provides an online alternative to a dense characterization LUT.
It writes only to a scratch OA library, executes the callbacks registered by
the PDK CDF, and reads a derived electrical-value parameter such as ``c``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from models import format_spice_value
from passive_mapping import (
    PassiveMappingConstraints,
    PassiveMappingError,
    PassiveMappingResult,
)
from pdk_profiles import PDKProfile, PassiveDeviceProfile, get_pdk_profile


@dataclass(frozen=True)
class CdfEvaluation:
    actual_value: float
    resolved_params: dict[str, object]
    parallel_units: int = 1


class CdfCfmomTargetMapper:
    """Batch target mapper for a finger-MOM capacitor with a derived CDF value."""

    backend_name = "virtuoso_cdf_callback"

    def __init__(
        self,
        *,
        profile: PDKProfile,
        device_name: str,
        work_dir: str | Path | None = None,
        virtuoso_bin: str = "virtuoso",
    ) -> None:
        self.profile = profile
        self.device_name = device_name
        self.work_dir = Path(
            work_dir
            or Path("/share/tmp/circuit_agent_cdf_mapping") / profile.name / device_name
        ).resolve()
        self.virtuoso_bin = virtuoso_bin

    def map_candidates(
        self,
        device_name: str,
        device: PassiveDeviceProfile,
        target_value: float,
        constraints: PassiveMappingConstraints,
    ) -> list[PassiveMappingResult]:
        if device_name != self.device_name:
            raise ValueError(
                f"CDF mapper for '{self.device_name}' cannot map '{device_name}'"
            )
        if device.kind != "capacitor":
            raise ValueError("Finger-MOM CDF mapper requires a capacitor device")
        if target_value <= 0 or not math.isfinite(target_value):
            raise ValueError("Target capacitance must be positive and finite")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        calibration_requests, calibration = self._calibration(device)
        reference_length = float(calibration_requests[0][device.length_parameter])
        assert device.max_length_m is not None
        estimated_max = max(item.actual_value for item in calibration) * (
            device.max_length_m / reference_length
        )
        max_parallel = constraints.max_parallel_units or device.max_parallel_units
        first_parallel = max(1, math.ceil(target_value / estimated_max))
        if first_parallel > max_parallel:
            raise PassiveMappingError(
                f"{device_name} target {target_value:g} exceeds the estimated "
                f"{max_parallel}-unit CDF range"
            )
        parallel = first_parallel
        unit_target = target_value / parallel
        seed_requests = _cfmom_target_candidates(
            unit_target,
            calibration_requests,
            calibration,
            device=device,
            reference_length=reference_length,
            candidate_finger_limit=1,
            length_radius=0,
        )
        seed_evaluations = self._evaluate(device, seed_requests)
        calibration_by_finger = {
            int(
                item.resolved_params.get(
                    device.finger_parameter, request[device.finger_parameter]
                )
            ): item
            for request, item in zip(calibration_requests, calibration)
        }
        requests: list[dict[str, object]] = []
        for request, seed in zip(seed_requests, seed_evaluations):
            nr = int(
                seed.resolved_params.get(
                    device.finger_parameter, request[device.finger_parameter]
                )
            )
            reference = calibration_by_finger[nr]
            seed_length = float(
                seed.resolved_params.get(
                    device.length_parameter, request[device.length_parameter]
                )
            )
            denominator = seed.actual_value - reference.actual_value
            if abs(denominator) > 1e-30 and seed_length != reference_length:
                corrected_length = reference_length + (
                    (unit_target - reference.actual_value)
                    * (seed_length - reference_length)
                    / denominator
                )
            else:
                corrected_length = seed_length * unit_target / seed.actual_value
            requests.extend(
                _cfmom_local_length_candidates(
                    device=device,
                    nr=nr,
                    center_length=corrected_length,
                    radius=3,
                )
            )
        evaluations = self._evaluate(device, requests)
        results: list[PassiveMappingResult] = []
        for request, evaluation in zip(requests, evaluations):
            actual = evaluation.actual_value * parallel
            params = evaluation.resolved_params or request
            unit_area = _cfmom_estimated_area(params)
            results.append(
                PassiveMappingResult(
                    device_kind="capacitor",
                    device_type=device_name,
                    target_value=target_value,
                    actual_value=actual,
                    relative_error=abs(actual - target_value) / target_value,
                    params=dict(params),
                    parallel_units=parallel,
                    unit_value=evaluation.actual_value,
                    unit_area_m2=unit_area,
                    area_m2=unit_area * parallel,
                    evaluator_backend=self.backend_name,
                    evaluator_metadata={
                        "derived_parameter": device.value_parameter or "c",
                        "callback_resolved": True,
                        "calibration_cache": str(self._cache_path),
                        "area_method": "estimated_cfmom_footprint",
                    },
                )
            )
        ordered = sorted(
            results,
            key=lambda item: (
                round(item.relative_error, 12),
                item.parallel_units,
                tuple(sorted((key, str(value)) for key, value in item.params.items())),
            ),
        )
        unique: list[PassiveMappingResult] = []
        signatures: set[tuple[object, ...]] = set()
        for result in ordered:
            signature = (
                result.parallel_units,
                tuple(
                    sorted((key, str(value)) for key, value in result.params.items())
                ),
            )
            if signature in signatures:
                continue
            signatures.add(signature)
            unique.append(result)
            if len(unique) >= constraints.candidate_limit:
                break
        tolerance = (
            constraints.tolerance
            if constraints.tolerance is not None
            else device.value_tolerance
        )
        if not unique or unique[0].relative_error > tolerance:
            detail = unique[0].relative_error if unique else math.inf
            raise PassiveMappingError(
                f"{device_name} cannot realize {target_value:g} through CDF within "
                f"{tolerance:.2%}; best error is {detail:.2%}"
            )
        return unique

    @property
    def _cache_path(self) -> Path:
        return self.work_dir / "calibration_cache.json"

    def _calibration(
        self,
        device: PassiveDeviceProfile,
    ) -> tuple[list[dict[str, object]], list[CdfEvaluation]]:
        assert device.min_length_m is not None
        requests = [
            _cfmom_params(device=device, nr=nr, lr=device.min_length_m)
            for nr in range(
                device.min_fingers,
                device.max_fingers + 1,
                device.finger_step,
            )
        ]
        fingerprint = _calibration_fingerprint(
            self.profile, self.device_name, device, requests
        )
        cached = _read_calibration_cache(self._cache_path, fingerprint, requests)
        if cached is not None:
            return requests, cached
        results = self._evaluate(device, requests)
        payload = {
            "version": 1,
            "fingerprint": fingerprint,
            "requests": requests,
            "results": [
                {
                    "actual_value": item.actual_value,
                    "resolved_params": item.resolved_params,
                }
                for item in results
            ],
        }
        staging = self._cache_path.with_suffix(".tmp")
        staging.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        staging.replace(self._cache_path)
        return requests, results

    def _evaluate(
        self,
        device: PassiveDeviceProfile,
        requests: Sequence[Mapping[str, object]],
    ) -> list[CdfEvaluation]:
        with tempfile.TemporaryDirectory(
            prefix="oa_run_", dir=self.work_dir
        ) as run_dir:
            return evaluate_cdf_geometries(
                device,
                requests,
                profile=self.profile,
                work_dir=run_dir,
                virtuoso_bin=self.virtuoso_bin,
                value_parameter=device.value_parameter or "c",
            )


def render_cdf_evaluation_probe(
    *,
    device: PassiveDeviceProfile,
    requests: Sequence[Mapping[str, object]],
    report_path: str | Path,
    scratch_lib: str,
    scratch_lib_path: str | Path,
    value_parameter: str = "c",
) -> str:
    """Render one replay that evaluates many geometries in one OA session."""
    if not requests:
        raise ValueError("CDF evaluation requires at least one geometry")
    cdf_names = _cdf_parameter_names(device)
    rendered_cases = []
    for request in requests:
        missing = [name for name in cdf_names if name not in request]
        if missing:
            raise ValueError(
                "CDF geometry is missing Spectre parameters: " + ", ".join(missing)
            )
        entries = "\n".join(
            f'        list("{_skill_escape(cdf_name)}" '
            f'"{_skill_escape(_cdf_value(request[spectre_name]))}")'
            for spectre_name, cdf_name in cdf_names.items()
        )
        rendered_cases.append(f"      list(\n{entries}\n      )")
    cases = "\n".join(rendered_cases)
    resolved_names = " ".join(f'"{_skill_escape(name)}"' for name in cdf_names.values())
    header = "index\tvalue\t" + "\t".join(cdf_names)
    return f"""/* Read-only PCell CDF value evaluation generated by Circuit Agent. */
procedure(boCdfSetAndRun(cdfObj name value)
  let((param callback)
    param = cdfFindParamByName(cdfObj name)
    unless(param error("Missing CDF parameter: %s\\n" name))
    param~>value = value
    callback = param~>callback
    when(callback && callback != "" evalstring(callback))
  )
)

let((cv master inst cdfObj out cases case pair param valueParam index paramNames)
  unless(ddGetObj("{_skill_escape(scratch_lib)}")
    ddCreateLib("{_skill_escape(scratch_lib)}"
      "{_skill_escape(str(Path(scratch_lib_path).resolve()))}")
  )
  cv = dbOpenCellViewByType("{_skill_escape(scratch_lib)}"
    "cdf_probe" "schematic" "schematic" "w")
  unless(cv error("Unable to create scratch schematic\\n"))
  master = dbOpenCellViewByType("{_skill_escape(device.virtuoso_lib)}"
    "{_skill_escape(device.virtuoso_cell)}"
    "{_skill_escape(device.virtuoso_view)}" "" "r")
  unless(master error("Unable to open passive PCell master\\n"))
  inst = dbCreateParamInst(cv master "Iprobe" list(0 0) "R0" 1 nil)
  unless(inst error("Unable to create passive PCell instance\\n"))
  cdfObj = cdfGetInstCDF(inst)
  unless(cdfObj error("Unable to get instance CDF\\n"))
  out = outfile("{_skill_escape(str(Path(report_path).resolve()))}" "w")
  fprintf(out "{_skill_escape(header)}\\n")
  cases = list(
{cases}
  )
  paramNames = list({resolved_names})
  index = 0
  foreach(case cases
    cdfgData = cdfObj
    foreach(pair case boCdfSetAndRun(cdfObj car(pair) cadr(pair)))
    valueParam = cdfFindParamByName(cdfObj "{_skill_escape(value_parameter)}")
    unless(valueParam error("Missing derived CDF value parameter\\n"))
    fprintf(out "%d\\t%s" index valueParam~>value)
    foreach(param paramNames
      fprintf(out "\\t%s" cdfFindParamByName(cdfObj param)~>value)
    )
    fprintf(out "\\n")
    cdfgData = nil
    index = index + 1
  )
  close(out)
  dbClose(master)
  dbPurge(cv)
  printf("CDF passive evaluation complete: %d cases\\n" index)
)
exit()
"""


def parse_cdf_evaluation_report(
    report_path: str | Path,
    device: PassiveDeviceProfile,
) -> list[CdfEvaluation]:
    """Parse the tab-separated output written by the generated replay."""
    path = Path(report_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        raise RuntimeError(f"CDF evaluation report has no results: {path}")
    header = lines[0].split("\t")
    expected = ["index", "value", *_cdf_parameter_names(device)]
    if header != expected:
        raise RuntimeError(
            f"Unexpected CDF evaluation header {header!r}; expected {expected!r}"
        )
    results: list[CdfEvaluation] = []
    for expected_index, line in enumerate(lines[1:]):
        fields = line.split("\t")
        if len(fields) != len(header):
            raise RuntimeError(f"Malformed CDF evaluation row: {line}")
        if int(fields[0]) != expected_index:
            raise RuntimeError("CDF evaluation rows are not ordered")
        resolved = {
            spectre_name: _parse_cdf_parameter(value)
            for spectre_name, value in zip(header[2:], fields[2:])
        }
        results.append(
            CdfEvaluation(
                actual_value=_parse_engineering_value(fields[1]),
                resolved_params=resolved,
            )
        )
    return results


def evaluate_cdf_geometries(
    device: PassiveDeviceProfile,
    requests: Sequence[Mapping[str, object]],
    *,
    profile: PDKProfile | None = None,
    work_dir: str | Path | None = None,
    virtuoso_bin: str = "virtuoso",
    value_parameter: str = "c",
    timeout_s: float = 180.0,
) -> list[CdfEvaluation]:
    """Run one Virtuoso replay and return CDF estimates for all requests."""
    pdk = profile or get_pdk_profile()
    temporary = None
    if work_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="cdf_passive_", dir="/share/tmp")
        root = Path(temporary.name)
    else:
        root = Path(work_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
    try:
        report = root / "cdf_evaluation.tsv"
        replay = root / "evaluate.il"
        log = root / "virtuoso.log"
        scratch_path = root / "oa"
        scratch_lib = "boCdfPassiveProbe"
        if report.exists():
            report.unlink()
        (root / "cds.lib").write_text(
            f"DEFINE {pdk.virtuoso_tech_lib} {pdk.virtuoso_pdk_lib_path}\n",
            encoding="ascii",
        )
        replay.write_text(
            render_cdf_evaluation_probe(
                device=device,
                requests=requests,
                report_path=report,
                scratch_lib=scratch_lib,
                scratch_lib_path=scratch_path,
                value_parameter=value_parameter,
            ),
            encoding="ascii",
        )
        command = [
            virtuoso_bin,
            "-replay",
            str(replay),
            "-log",
            str(log),
        ]
        process = subprocess.Popen(
            command,
            cwd=root,
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                return_code = process.poll()
                report_complete = _report_row_count(report) == len(requests)
                replay_complete = _log_contains(log, "CDF passive evaluation complete:")
                if report_complete and (replay_complete or return_code is not None):
                    results = parse_cdf_evaluation_report(report, device)
                    if len(results) != len(requests):
                        raise RuntimeError(
                            "CDF evaluation returned "
                            f"{len(results)} results for {len(requests)} requests; "
                            f"see {report}"
                        )
                    return results
                if return_code is not None:
                    row_count = _report_row_count(report)
                    if row_count >= 0:
                        raise RuntimeError(
                            "CDF evaluation returned "
                            f"{row_count} results for {len(requests)} requests; "
                            f"see {report}"
                        )
                    detail = _last_log_error(log)
                    raise RuntimeError(
                        f"Virtuoso CDF evaluation failed with code {return_code}: "
                        f"{detail or f'see {log}'}"
                    )
                time.sleep(0.1)
            detail = _last_log_error(log)
            raise RuntimeError(
                f"Virtuoso CDF evaluation timed out after {timeout_s:g}s: "
                f"{detail or f'see {log}'}"
            )
        finally:
            _stop_process(process)
    finally:
        if temporary is not None:
            temporary.cleanup()


def map_cfmom_with_cdf(
    target_f: float,
    *,
    profile: PDKProfile | None = None,
    device_name: str = "finger_mom_2t",
    work_dir: str | Path,
    virtuoso_bin: str = "virtuoso",
) -> CdfEvaluation:
    """Map a target through the production CDF target mapper."""
    pdk = profile or get_pdk_profile()
    device = pdk.passive_devices[device_name]
    mapper = CdfCfmomTargetMapper(
        profile=pdk,
        device_name=device_name,
        work_dir=work_dir,
        virtuoso_bin=virtuoso_bin,
    )
    result = mapper.map_candidates(
        device_name,
        device,
        target_f,
        PassiveMappingConstraints(),
    )
    selected = result[0]
    return CdfEvaluation(
        actual_value=selected.actual_value,
        resolved_params=selected.params,
        parallel_units=selected.parallel_units,
    )


def _cfmom_target_candidates(
    target_f: float,
    calibration_requests: Sequence[Mapping[str, object]],
    calibration: Sequence[CdfEvaluation],
    *,
    device: PassiveDeviceProfile | None = None,
    reference_length: float = 1e-6,
    candidate_finger_limit: int | None = None,
    length_radius: int = 3,
) -> list[dict[str, object]]:
    """Predict local length-grid candidates from 1 um CDF calibration."""
    if len(calibration_requests) != len(calibration):
        raise ValueError("Calibration request/result lengths differ")
    grid = device.geometry_grid_m if device else 10e-9
    min_length = device.min_length_m if device else 1e-6
    max_length = device.max_length_m if device else 40e-6
    assert grid is not None and min_length is not None and max_length is not None
    minimum_steps = math.ceil((min_length / grid) - 1e-12)
    maximum_steps = math.floor((max_length / grid) + 1e-12)
    predictions: list[tuple[float, int, int]] = []
    for request, result in zip(calibration_requests, calibration):
        if result.actual_value <= 0:
            continue
        nr = int(result.resolved_params.get("nr", request["nr"]))
        estimate = target_f / result.actual_value * reference_length
        center = round(estimate / grid)
        center = min(max(center, minimum_steps), maximum_steps)
        width = float(request.get("w", 50e-9))
        spacing = float(request.get("s", width))
        footprint_width = nr * (width + spacing)
        footprint_length = center * grid
        aspect = max(
            footprint_width / footprint_length,
            footprint_length / footprint_width,
        )
        predictions.append((aspect, nr, center))
    if candidate_finger_limit is not None:
        if candidate_finger_limit < 1:
            raise ValueError("candidate_finger_limit must be positive")
        predictions = sorted(predictions)[:candidate_finger_limit]
    candidates: dict[tuple[int, int], dict[str, object]] = {}
    for _, nr, center in predictions:
        for offset in range(-length_radius, length_radius + 1):
            steps = min(max(center + offset, minimum_steps), maximum_steps)
            candidates[(nr, steps)] = _cfmom_params(
                device=device, nr=nr, lr=steps * grid
            )
    return list(candidates.values())


def _cfmom_local_length_candidates(
    *,
    device: PassiveDeviceProfile,
    nr: int,
    center_length: float,
    radius: int,
) -> list[dict[str, object]]:
    assert device.geometry_grid_m is not None
    assert device.min_length_m is not None and device.max_length_m is not None
    grid = device.geometry_grid_m
    minimum_steps = math.ceil((device.min_length_m / grid) - 1e-12)
    maximum_steps = math.floor((device.max_length_m / grid) + 1e-12)
    center = round(center_length / grid)
    candidates: dict[int, dict[str, object]] = {}
    for offset in range(-radius, radius + 1):
        steps = min(max(center + offset, minimum_steps), maximum_steps)
        candidates[steps] = _cfmom_params(
            device=device,
            nr=nr,
            lr=steps * grid,
        )
    return list(candidates.values())


def _cfmom_params(
    *,
    nr: int,
    lr: float,
    device: PassiveDeviceProfile | None = None,
) -> dict[str, object]:
    if device is None:
        return {
            "nr": nr,
            "lr": lr,
            "w": 50e-9,
            "s": 50e-9,
            "stm": 1,
            "spm": 6,
            "multi": 1,
        }
    params = dict(device.fixed_parameters)
    params[device.finger_parameter] = nr
    params[device.length_parameter] = lr
    return params


def _cfmom_estimated_area(params: Mapping[str, object]) -> float:
    nr = int(params["nr"])
    width = float(params["w"])
    spacing = float(params["s"])
    length = float(params["lr"])
    footprint_width = nr * (width + spacing)
    footprint_length = length + 2 * max(90e-9, spacing) + 198e-9
    return footprint_width * footprint_length


def _calibration_fingerprint(
    profile: PDKProfile,
    device_name: str,
    device: PassiveDeviceProfile,
    requests: Sequence[Mapping[str, object]],
) -> str:
    identity = {
        "profile": profile.name,
        "pdk_library_path": profile.virtuoso_pdk_lib_path,
        "device_name": device_name,
        "virtuoso": [
            device.virtuoso_lib,
            device.virtuoso_cell,
            device.virtuoso_view,
        ],
        "parameter_map": device.parameter_map,
        "value_parameter": device.value_parameter,
        "requests": requests,
    }
    encoded = json.dumps(identity, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_calibration_cache(
    path: Path,
    fingerprint: str,
    requests: Sequence[Mapping[str, object]],
) -> list[CdfEvaluation] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("fingerprint") != fingerprint or raw.get("requests") != list(
            requests
        ):
            return None
        results = [
            CdfEvaluation(
                actual_value=float(item["actual_value"]),
                resolved_params=dict(item["resolved_params"]),
            )
            for item in raw["results"]
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return results if len(results) == len(requests) else None


def _cdf_parameter_names(device: PassiveDeviceProfile) -> dict[str, str]:
    names = {
        spectre_name: device.parameter_map.get(spectre_name, spectre_name)
        for spectre_name in (
            device.finger_parameter,
            device.length_parameter,
            device.width_parameter,
            "s",
            "stm",
            "spm",
            device.multiplier_parameter,
        )
        if spectre_name
    }
    return names


def _cdf_value(value: object) -> str:
    if isinstance(value, float):
        return format_spice_value(value)
    return str(value)


def _parse_cdf_parameter(value: str) -> object:
    text = value.strip().strip('"')
    try:
        parsed = _parse_engineering_value(text)
    except ValueError:
        return text
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    return parsed


def _parse_engineering_value(value: str) -> float:
    text = value.strip().strip('"').lower()
    match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)" r"(meg|[tgkmunpf])?",
        text,
    )
    if match is None:
        raise ValueError(f"Invalid engineering value: {value}")
    suffixes = {
        None: 1.0,
        "t": 1e12,
        "g": 1e9,
        "meg": 1e6,
        "k": 1e3,
        "m": 1e-3,
        "u": 1e-6,
        "n": 1e-9,
        "p": 1e-12,
        "f": 1e-15,
    }
    return float(match.group(1)) * suffixes[match.group(2)]


def _last_log_error(path: Path) -> str:
    if not path.exists():
        return ""
    errors = [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if "*Error*" in line or "\\e " in line
    ]
    return errors[-1] if errors else ""


def _log_contains(path: Path, marker: str) -> bool:
    if not path.exists():
        return False
    try:
        return marker in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _report_row_count(path: Path) -> int:
    if not path.exists():
        return -1
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return -1
    return max(len(lines) - 1, 0)


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _skill_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="Target capacitance, e.g. 250f")
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("/share/tmp/cfmom_cdf_mapping"),
    )
    parser.add_argument("--virtuoso-bin", default="virtuoso")
    args = parser.parse_args()
    target = _parse_engineering_value(args.target)
    result = map_cfmom_with_cdf(
        target,
        work_dir=args.work_dir,
        virtuoso_bin=args.virtuoso_bin,
    )
    error = (result.actual_value - target) / target
    print(f"target_f={target:.12g}")
    print(f"cdf_value_f={result.actual_value:.12g}")
    print(f"relative_error={error:.6%}")
    print(f"parallel_units={result.parallel_units}")
    print("params=" + repr(result.resolved_params))


if __name__ == "__main__":
    main()
