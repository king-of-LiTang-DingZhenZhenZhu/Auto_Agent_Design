"""Characterize the TSMC28 ``cfmom_2t`` model into a PVT lookup table."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


MODEL_PATH = Path(
    "/share/home/chenhaonan/PDKS/TSMC28nm/models/spectre/toplevel.scs"
)
DEFAULT_WORK_DIR = Path("/share/tmp/tsmc28_cfmom_2t_characterization")
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[3]
    / "PDK_Info_Json/characterization/tsmc28_cfmom_2t_lut.json"
)
PROCESS_CORNERS = ("tt", "ss", "ff", "fs", "sf")
TEMPERATURES_C = (-40.0, 27.0, 125.0)
FREQUENCY_HZ = 1.0e6
SIMULATION_CHUNK_SIZE = 300

MIN_NR = 6
MAX_NR = 288
NR_STEP = 2
MIN_LR_M = 1.0e-6
MAX_LR_M = 40.0e-6
LR_GRID_M = 0.01e-6
FIXED_W_M = 0.05e-6
FIXED_S_M = 0.05e-6
FIXED_STM = 1
FIXED_SPM = 8

_CURRENT_RE = re.compile(
    r'^"(?P<name>Vg\d+:p)"\s+\([^\s]+\s+(?P<imag>[^)]+)\)$'
)


def legal_geometry(nr: int, lr_m: float) -> dict[str, float | int]:
    return {
        "nr": nr,
        "lr": lr_m,
        "w": FIXED_W_M,
        "s": FIXED_S_M,
        "stm": FIXED_STM,
        "spm": FIXED_SPM,
        "multi": 1,
    }


def select_geometries(
    calibration_f: dict[int, float],
    *,
    target_min_f: float = 10e-15,
    target_ratio: float = 1.01,
) -> list[dict[str, float | int]]:
    if target_min_f <= 0 or target_ratio <= 1:
        raise ValueError("Target minimum must be positive and ratio must exceed one")
    max_value = calibration_f[MAX_NR] * (MAX_LR_M / MIN_LR_M)
    targets: list[float] = []
    target = target_min_f
    while target < max_value:
        targets.append(target)
        target *= target_ratio
    targets.append(max_value)

    selected: dict[tuple[int, int], dict[str, float | int]] = {}
    for target in targets:
        ranked: list[tuple[float, int, float]] = []
        for nr, value_at_min_lr in calibration_f.items():
            lr_m = target / value_at_min_lr * MIN_LR_M
            lr_steps = round(lr_m / LR_GRID_M)
            lr_m = lr_steps * LR_GRID_M
            if not MIN_LR_M <= lr_m <= MAX_LR_M:
                continue
            predicted = value_at_min_lr * lr_m / MIN_LR_M
            error = abs(predicted - target) / target
            ranked.append((error, nr, lr_m))
        for _, nr, lr_m in sorted(ranked)[:2]:
            selected[(nr, round(lr_m / LR_GRID_M))] = legal_geometry(nr, lr_m)
    return sorted(selected.values(), key=lambda item: (float(item["lr"]), int(item["nr"])))


def render_netlist(
    geometries: list[dict[str, float | int]],
    *,
    section: str,
    temperature_c: float,
) -> str:
    lines = [
        "simulator lang=spectre",
        "global 0",
        "",
        f'include "{MODEL_PATH}" section=top_{section}',
        "",
    ]
    save_names: list[str] = []
    for index, params in enumerate(geometries):
        tag = f"g{index:04d}"
        source = f"V{tag}"
        save_names.append(f"{source}:p")
        lines.append(f"{source} (n_{tag} 0) vsource dc=0 mag=1")
        lines.append(
            f"X{tag} (n_{tag} 0) cfmom_2t "
            f"nr={params['nr']} lr={_spectre_length(float(params['lr']))} "
            f"w={_spectre_length(float(params['w']))} "
            f"s={_spectre_length(float(params['s']))} "
            f"stm={params['stm']} spm={params['spm']} multi=1 mismatchflag=0"
        )
    lines.extend(
        [
            "",
            f"simulatorOptions options temp={temperature_c:g} rawfmt=psfascii",
            "save " + " ".join(save_names),
            "ac ac start=1M stop=1.01M lin=2",
            "",
        ]
    )
    return "\n".join(lines)


def parse_capacitances(raw_file: Path, count: int) -> list[float]:
    values: dict[int, float] = {}
    in_values = False
    at_first_frequency = False
    for line in raw_file.read_text(encoding="utf-8").splitlines():
        if line == "VALUE":
            in_values = True
            continue
        if not in_values:
            continue
        if line.startswith('"freq"'):
            if at_first_frequency:
                break
            at_first_frequency = True
            continue
        if not at_first_frequency:
            continue
        match = _CURRENT_RE.match(line)
        if match is None:
            continue
        name = match.group("name")
        index = int(name[2:6])
        imag_current = float(match.group("imag"))
        values[index] = abs(imag_current) / (2.0 * math.pi * FREQUENCY_HZ)
    missing = [index for index in range(count) if index not in values]
    if missing:
        raise RuntimeError(f"Missing AC currents for geometry indices: {missing[:10]}")
    return [values[index] for index in range(count)]


def characterize(work_dir: Path, output_path: Path) -> dict[str, object]:
    work_dir.mkdir(parents=True, exist_ok=True)
    calibration_geometries = [
        legal_geometry(nr, MIN_LR_M)
        for nr in range(MIN_NR, MAX_NR + 1, NR_STEP)
    ]
    calibration_values = _simulate(
        work_dir,
        "calibration_tt_27",
        calibration_geometries,
        section="tt",
        temperature_c=27.0,
    )
    calibration = {
        int(params["nr"]): value
        for params, value in zip(calibration_geometries, calibration_values)
    }
    geometries = select_geometries(calibration)

    pvt_values: dict[tuple[str, float], list[float]] = {}
    for corner in PROCESS_CORNERS:
        for temperature_c in TEMPERATURES_C:
            label = f"{corner}_{temperature_c:g}".replace("-", "m")
            pvt_values[(corner, temperature_c)] = _simulate(
                work_dir,
                label,
                geometries,
                section=corner,
                temperature_c=temperature_c,
            )

    nominal = pvt_values[("tt", 27.0)]
    points = []
    for index, (params, value) in enumerate(zip(geometries, nominal)):
        characteristics = [
            {
                "corner": corner,
                "temperature_c": temperature_c,
                "capacitance_f": pvt_values[(corner, temperature_c)][index],
            }
            for corner in PROCESS_CORNERS
            for temperature_c in TEMPERATURES_C
        ]
        points.append(
            {
                "value": value,
                "params": params,
                "estimated_area_m2": _estimated_area(params),
                "pvt": characteristics,
            }
        )
    points.sort(key=lambda point: float(point["value"]))
    result: dict[str, object] = {
        "version": "tsmc28-cln28hpcp-v1d0-2p2a-cfmom2t-ac-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "device": "cfmom_2t",
        "pdk_version": "CLN28HPC+_v1.0_2p2a_20150612",
        "model_path": str(MODEL_PATH),
        "method": "AC imag(I)/(2*pi*f)",
        "frequency_hz": FREQUENCY_HZ,
        "dc_bias_v": 0.0,
        "corner": "tt",
        "temperature_c": 27.0,
        "nominal_corner": "tt",
        "nominal_temperature_c": 27.0,
        "process_corners": list(PROCESS_CORNERS),
        "temperatures_c": list(TEMPERATURES_C),
        "cdf_to_spectre": {
            "Wfinger": "w",
            "Sfinger": "s",
            "Lfinger": "lr",
            "Nfinger": "nr",
            "StartMn": "stm",
            "StopMn": "spm",
            "m": "multi",
        },
        "legal_geometry": {
            "w_m": {"min": 0.05e-6, "max": 0.075e-6, "step": 0.005e-6},
            "s_m": {"min": 0.05e-6, "max": 0.24e-6, "step": 0.005e-6},
            "lr_m": {"min": MIN_LR_M, "max": MAX_LR_M, "step": LR_GRID_M},
            "nr": {"min": MIN_NR, "max": MAX_NR, "step": NR_STEP},
            "stm": {"min": 1, "max": 6, "step": 1},
            "spm": {"min": 3, "max": 8, "step": 1},
            "minimum_stacked_metal_layers": 3,
        },
        "sample_family": {
            "w_m": FIXED_W_M,
            "s_m": FIXED_S_M,
            "stm": FIXED_STM,
            "spm": FIXED_SPM,
            "target_spacing_ratio": 1.01,
            "geometries_per_target": 2,
        },
        "coverage": {
            "unit_min_f": min(float(point["value"]) for point in points),
            "unit_max_f": max(float(point["value"]) for point in points),
            "mapped_target_min_f": 10e-15,
            "mapped_target_max_f_with_16_parallel": max(
                float(point["value"]) for point in points
            ) * 16,
        },
        "points": points,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _simulate(
    work_dir: Path,
    label: str,
    geometries: list[dict[str, float | int]],
    *,
    section: str,
    temperature_c: float,
) -> list[float]:
    # Keep one netlist basename so LSF compute nodes can reuse the AHDL cache
    # precompiled on mn01 (the compute image lacks Cadence's 32-bit compiler loader).
    netlist = work_dir / "cfmom_2t_characterization.scs"
    values: list[float] = []
    for chunk_index, start in enumerate(range(0, len(geometries), SIMULATION_CHUNK_SIZE)):
        chunk = geometries[start : start + SIMULATION_CHUNK_SIZE]
        chunk_label = f"{label}_part{chunk_index:02d}"
        raw_dir = work_dir / f"{chunk_label}.raw"
        log_path = work_dir / f"{chunk_label}.log"
        netlist.write_text(
            render_netlist(chunk, section=section, temperature_c=temperature_c),
            encoding="ascii",
        )
        completed = subprocess.run(
            ["spectre", str(netlist), "-raw", str(raw_dir), "+log", str(log_path)],
            cwd=work_dir,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Spectre failed for {chunk_label}; see {log_path}")
        values.extend(parse_capacitances(raw_dir / "ac.ac", len(chunk)))
    return values


def _estimated_area(params: dict[str, float | int]) -> float:
    width = int(params["nr"]) * (float(params["w"]) + float(params["s"]))
    length = float(params["lr"]) + 2 * max(0.09e-6, float(params["s"])) + 0.198e-6
    return width * length


def _spectre_length(value_m: float) -> str:
    return f"{value_m / 1e-6:.12g}u"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = characterize(args.work_dir.resolve(), args.output.resolve())
    coverage = result["coverage"]
    print(f"LUT: {args.output.resolve()}")
    print(f"Points: {len(result['points'])}")
    print(
        "Coverage: "
        f"{coverage['unit_min_f']:.6g} F .. "
        f"{coverage['mapped_target_max_f_with_16_parallel']:.6g} F"
    )


if __name__ == "__main__":
    main()
