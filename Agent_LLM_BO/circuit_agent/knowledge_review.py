"""Knowledge-driven first-order diagnostics for BO review candidates."""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any


DEFAULT_KNOWLEDGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "knowledge_base"
    / "circuit_design_relations.json"
)
THERMAL_VOLTAGE_27C = 25.852e-3


def build_knowledge_analysis(
    topology_name: str,
    history: dict[str, Any],
    records: list[dict[str, Any]],
    workspace: str | Path,
    knowledge_path: str | Path = DEFAULT_KNOWLEDGE_PATH,
) -> dict[str, Any]:
    knowledge = json.loads(Path(knowledge_path).read_text(encoding="utf-8"))
    profile = (knowledge.get("topologies") or {}).get(topology_name)
    if not profile:
        return {
            "topology": topology_name,
            "status": "no_structured_knowledge",
            "run_analyses": [],
            "relations": [],
        }

    relations = [
        {"id": relation_id, **knowledge["relations"][relation_id]}
        for relation_id in profile.get("relations", [])
        if relation_id in knowledge.get("relations", {})
    ]
    root = Path(workspace)
    run_analyses = []
    for record in records:
        iteration = int(record.get("iteration", 0))
        run_dir = root / f"run_{iteration:03d}"
        params = _collect_params(record, run_dir)
        if profile.get("domain") == "opamp":
            analysis = _analyze_opamp_run(
                record, run_dir, params, profile, history.get("targets") or {}
            )
        else:
            analysis = _analyze_bandgap_run(record, params, profile)
        run_analyses.append(analysis)

    return {
        "topology": topology_name,
        "domain": profile.get("domain"),
        "architecture": profile.get("architecture"),
        "status": "ok",
        "relations": relations,
        "limitations": profile.get("limitations", []),
        "run_analyses": run_analyses,
    }


def write_knowledge_analysis(
    analysis: dict[str, Any], output_dir: str | Path
) -> tuple[Path, Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "knowledge_analysis.json"
    markdown_path = destination / "knowledge_analysis.md"
    json_path.write_text(
        json.dumps(analysis, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(render_knowledge_markdown(analysis), encoding="utf-8")
    return json_path, markdown_path


def render_knowledge_markdown(analysis: dict[str, Any]) -> str:
    lines = [
        "# Knowledge-Driven Circuit Analysis",
        "",
        f"- Topology: `{analysis.get('topology')}`",
        f"- Domain: `{analysis.get('domain', 'unknown')}`",
        f"- Architecture: `{analysis.get('architecture', 'unknown')}`",
        f"- Status: `{analysis.get('status')}`",
        "",
        "## Applicable Relations",
        "",
    ]
    relations = analysis.get("relations", [])
    if relations:
        for relation in relations:
            lines.append(f"- `{relation['id']}`: `{relation.get('equation')}`")
            for assumption in relation.get("assumptions", []):
                lines.append(f"  - Assumption: {assumption}")
            for tradeoff in relation.get("tradeoffs", []):
                lines.append(f"  - Tradeoff: {tradeoff}")
    else:
        lines.append("- No structured relation is registered for this topology.")

    lines.extend(["", "## Top-run Diagnostics", ""])
    for run in analysis.get("run_analyses", []):
        lines.append(
            f"### Iteration {run.get('iteration')} (reward {run.get('reward')})"
        )
        for key, value in run.get("derived", {}).items():
            lines.append(f"- {key}: `{_format_value(value)}`")
        for diagnosis in run.get("diagnoses", []):
            lines.append(
                f"- Diagnosis [{diagnosis['confidence']}]: {diagnosis['message']}"
            )
        for unavailable in run.get("unavailable", []):
            lines.append(f"- Unavailable: {unavailable}")
        lines.append("")

    limitations = analysis.get("limitations", [])
    if limitations:
        lines.extend(["## Topology Limitations", ""])
        lines.extend(f"- {item}" for item in limitations)
        lines.append("")
    lines.extend([
        "## Review Rule",
        "",
        "Use these first-order relations as physical priors. Compare them with BO parameter effects and Spectre results. If they disagree, request a local perturbation experiment instead of assuming either source is causal.",
    ])
    return "\n".join(lines) + "\n"


def _analyze_opamp_run(
    record: dict[str, Any],
    run_dir: Path,
    params: dict[str, float],
    profile: dict[str, Any],
    targets: dict[str, Any],
) -> dict[str, Any]:
    result = record.get("result") or {}
    cap_name = profile.get("capacitance_parameter")
    capacitance = _number(params.get(cap_name))
    input_gm = _average_device_gm(
        run_dir / "diagnostics" / "dc_operating_points.csv",
        set(profile.get("input_instances", [])),
    )
    target_gbw = _number(targets.get("bandwidth_hz"))
    measured_gbw = _number(result.get("bandwidth_hz", result.get("gbw_hz")))
    measured_pm = _number(result.get("phase_margin_deg"))
    target_pm = _number(targets.get("phase_margin_deg"))
    derived: dict[str, float | str] = {}
    unavailable: list[str] = []
    diagnoses: list[dict[str, str]] = []

    if capacitance is not None:
        derived[f"{cap_name}_F"] = capacitance
    else:
        unavailable.append(f"{cap_name} needed for first-order GBW relation")
    if input_gm is not None:
        derived["measured_input_gm_S"] = input_gm
    else:
        unavailable.append("input-pair gm from dc_operating_points.csv")

    if capacitance and input_gm is not None:
        predicted_gbw = input_gm / (2 * math.pi * capacitance)
        derived["first_order_predicted_gbw_hz"] = predicted_gbw
        if measured_gbw:
            derived["predicted_to_measured_gbw_ratio"] = predicted_gbw / measured_gbw
    if capacitance and target_gbw:
        required_gm = 2 * math.pi * target_gbw * capacitance
        derived["input_gm_required_for_target_S"] = required_gm
        if input_gm is not None:
            ratio = input_gm / required_gm
            derived["measured_to_required_gm_ratio"] = ratio
            if ratio < 0.9:
                diagnoses.append({
                    "confidence": "medium",
                    "message": (
                        "input gm is below the first-order GBW requirement; "
                        "review input-stage gm/Id, current, and compensation capacitance"
                    ),
                })

    if measured_gbw and measured_pm is not None and 0 < measured_pm < 90:
        p2_ratio = math.tan(math.radians(measured_pm))
        derived["two_pole_estimated_p2_over_ugf"] = p2_ratio
        derived["two_pole_estimated_p2_hz"] = measured_gbw * p2_ratio
        if target_pm is not None and 0 < target_pm < 90:
            required_ratio = math.tan(math.radians(target_pm))
            derived["p2_over_ugf_required_for_target_pm"] = required_ratio
            if p2_ratio < required_ratio:
                diagnoses.append({
                    "confidence": "low",
                    "message": (
                        "two-pole estimate places p2 too close to UGF for the PM target; "
                        "check second-stage gm, load capacitance, Cc/Rz, and nearby zeros"
                    ),
                })
    elif measured_pm is None:
        unavailable.append("phase margin needed to estimate p2/UGF")

    return {
        "iteration": int(record.get("iteration", 0)),
        "reward": record.get("reward"),
        "derived": derived,
        "diagnoses": diagnoses,
        "unavailable": unavailable,
    }


def _analyze_bandgap_run(
    record: dict[str, Any],
    params: dict[str, float],
    profile: dict[str, Any],
) -> dict[str, Any]:
    area_ratio = _number(params.get("BJT_AREA_RATIO"))
    rptat = _number(params.get("Rptat"))
    rctat = _number(params.get("Rctat"))
    derived: dict[str, float] = {}
    diagnoses: list[dict[str, str]] = []
    unavailable = [
        "temperature sweep Vref(T) needed for tempco diagnosis",
        "line sweep needed for line-regulation diagnosis",
        "real PDK BJT operating points needed for physical DeltaVBE validation",
    ]
    if area_ratio and area_ratio > 1:
        derived["delta_vbe_27c_first_order_V"] = (
            THERMAL_VOLTAGE_27C * math.log(area_ratio)
        )
    if rptat and rctat:
        derived["rctat_over_rptat"] = rctat / rptat
    diagnoses.append({
        "confidence": "high",
        "message": (
            "room-temperature Vref alone cannot validate bandgap compensation; "
            "tempco requires Vref(T), and residual curvature must not be treated as a simple ratio error"
        ),
    })
    for limitation in profile.get("limitations", []):
        diagnoses.append({"confidence": "high", "message": limitation})
    return {
        "iteration": int(record.get("iteration", 0)),
        "reward": record.get("reward"),
        "derived": derived,
        "diagnoses": diagnoses,
        "unavailable": unavailable,
    }


def _collect_params(record: dict[str, Any], run_dir: Path) -> dict[str, float]:
    params: dict[str, float] = {}
    for field in ("params", "physical_params"):
        values = record.get(field)
        if isinstance(values, dict):
            for name, value in values.items():
                number = _number(value)
                if number is not None:
                    params[name] = number
    for path in (run_dir / "circuit.cir", *sorted(run_dir.glob("tb*.scs"))):
        if path.exists():
            params.update(_parse_numeric_parameters(path.read_text(encoding="utf-8")))
    return params


def _parse_numeric_parameters(text: str) -> dict[str, float]:
    params: dict[str, float] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not re.match(r"^(parameters|\.param)\b", stripped, re.IGNORECASE):
            continue
        for name, raw in re.findall(r"(\w+)\s*=\s*([^\s]+)", stripped):
            value = _parse_spice_number(raw)
            if value is not None:
                params[name] = value
    return params


def _parse_spice_number(raw: str) -> float | None:
    match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
        r"(meg|[fpnumkg])?",
        raw.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    scales = {
        "": 1.0,
        "f": 1e-15,
        "p": 1e-12,
        "n": 1e-9,
        "u": 1e-6,
        "m": 1e-3,
        "k": 1e3,
        "meg": 1e6,
        "g": 1e9,
    }
    return float(match.group(1)) * scales[(match.group(2) or "").lower()]


def _average_device_gm(csv_path: Path, instances: set[str]) -> float | None:
    if not csv_path.exists() or not instances:
        return None
    values = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            leaf = re.split(r"[./:]", (row.get("instance") or ""))[-1]
            gm = _number(row.get("gm"))
            if leaf in instances and gm is not None:
                values.append(abs(gm))
    return sum(values) / len(values) if values else None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)
