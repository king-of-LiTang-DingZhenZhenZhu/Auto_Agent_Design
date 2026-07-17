"""Lightweight design-quality audit before PVT verification."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from review_optimization import parse_parameter_values, parse_spice_value
from topologies import get_topology


@dataclass(frozen=True)
class AuditFinding:
    code: str
    severity: str
    message: str
    evidence: dict[str, Any]


def run_design_audit(
    project: str | Path,
    results_path: str | Path,
    netlist_path: str | Path | None,
    topology_name: str = "",
) -> dict[str, Any]:
    """Audit a nominally passing design and write JSON/Markdown reports."""
    project_path = Path(project)
    result_file = Path(results_path)
    results = json.loads(result_file.read_text(encoding="utf-8"))
    findings: list[AuditFinding] = []

    findings.extend(_audit_operating_point(results))
    findings.extend(_audit_power_opportunity(project_path, results))

    resolved_netlist = Path(netlist_path) if netlist_path else None
    if resolved_netlist is None or not resolved_netlist.exists():
        findings.append(
            AuditFinding(
                code="missing_final_netlist",
                severity="blocker",
                message="The final netlist is unavailable, so device geometry cannot be audited.",
                evidence={"netlist": str(resolved_netlist) if resolved_netlist else None},
            )
        )
    else:
        netlist_text = resolved_netlist.read_text(encoding="utf-8")
        findings.extend(_audit_device_geometry(netlist_text))
        findings.extend(_audit_parameter_bounds(netlist_text, topology_name))

    blocker_count = sum(item.severity == "blocker" for item in findings)
    warning_count = sum(item.severity == "warning" for item in findings)
    status = "block" if blocker_count else "warn" if warning_count else "pass"
    output_dir = project_path / "design_audit"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "design_audit.json"
    markdown_path = output_dir / "design_audit.md"
    report = {
        "status": status,
        "blocker_count": blocker_count,
        "warning_count": warning_count,
        "topology": topology_name,
        "results_file": str(result_file.resolve()),
        "netlist_file": str(resolved_netlist.resolve()) if resolved_netlist else None,
        "findings": [asdict(item) for item in findings],
        "json_file": str(json_path.resolve()),
        "report_file": str(markdown_path.resolve()),
    }

    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    return report


def _audit_operating_point(results: dict[str, Any]) -> list[AuditFinding]:
    status = results.get("operating_point_status") or {}
    critical_linear = status.get("critical_linear") or []
    critical_near_edge = status.get("critical_near_edge") or []
    min_margin = status.get("min_margin_v")
    findings: list[AuditFinding] = []
    if critical_linear:
        findings.append(
            AuditFinding(
                code="critical_mos_linear",
                severity="blocker",
                message="Critical signal-path MOS devices are in the linear region.",
                evidence={"instances": critical_linear},
            )
        )
    if critical_near_edge:
        findings.append(
            AuditFinding(
                code="critical_mos_near_edge",
                severity="warning",
                message="Critical MOS devices have limited saturation margin.",
                evidence={"instances": critical_near_edge, "min_margin_v": min_margin},
            )
        )
    return findings


def _audit_device_geometry(netlist_text: str) -> list[AuditFinding]:
    params = parse_parameter_values(netlist_text)
    devices: list[dict[str, Any]] = []
    for line in netlist_text.splitlines():
        stripped = line.strip()
        if not stripped or not stripped[0].lower() == "m":
            continue
        fields = {
            name.lower(): value
            for name, value in re.findall(
                r"\b(w|l|nf|m)\s*=\s*'?([^\s')]+)'?", line, re.IGNORECASE
            )
        }
        width = _resolve_value(fields.get("w"), params)
        length = _resolve_value(fields.get("l"), params)
        if width is None or length is None or length <= 0:
            continue
        fingers = _resolve_value(fields.get("nf"), params) or 1.0
        multiplier = _resolve_value(fields.get("m"), params) or 1.0
        effective_width = width * multiplier
        devices.append(
            {
                "instance": stripped.split(maxsplit=1)[0],
                "width_m": width,
                "length_m": length,
                "nf": fingers,
                "m": multiplier,
                "effective_width_m": effective_width,
                "aspect_ratio": effective_width / length,
            }
        )

    findings: list[AuditFinding] = []
    large = [item for item in devices if item["width_m"] > 100e-6]
    narrow_ratio = [item for item in devices if item["aspect_ratio"] < 0.5]
    total_width = sum(item["effective_width_m"] for item in devices)
    if large:
        findings.append(
            AuditFinding(
                code="very_large_mos_width",
                severity="warning",
                message="One or more MOS devices have W greater than 100 um.",
                evidence={"devices": large},
            )
        )
    if narrow_ratio:
        findings.append(
            AuditFinding(
                code="unusual_narrow_mos",
                severity="warning",
                message="One or more MOS devices have effective W/L below 0.5.",
                evidence={"devices": narrow_ratio},
            )
        )
    if total_width >= 5e-3:
        findings.append(
            AuditFinding(
                code="large_total_gate_width",
                severity="warning",
                message="The summed effective MOS width is at least 5 mm; review area and parasitics.",
                evidence={"total_effective_width_m": total_width},
            )
        )
    return findings


def _audit_parameter_bounds(
    netlist_text: str, topology_name: str
) -> list[AuditFinding]:
    if not topology_name:
        return []
    try:
        bounds = {item.name: item for item in get_topology(topology_name).get_param_space().params}
    except ValueError:
        return []
    params = parse_parameter_values(netlist_text)
    near_bounds: list[dict[str, Any]] = []
    for name, value in params.items():
        bound = bounds.get(name)
        if bound is None or not math.isfinite(bound.low) or not math.isfinite(bound.high):
            continue
        span = bound.high - bound.low
        if span <= 0:
            continue
        low_distance = (value - bound.low) / span
        high_distance = (bound.high - value) / span
        if low_distance <= 0.02 or high_distance <= 0.02:
            near_bounds.append(
                {
                    "parameter": name,
                    "value": value,
                    "low": bound.low,
                    "high": bound.high,
                    "near": "low" if low_distance <= high_distance else "high",
                }
            )
    if not near_bounds:
        return []
    return [
        AuditFinding(
            code="parameters_near_search_bounds",
            severity="warning",
            message="Optimized parameters are within 2% of a search-space boundary.",
            evidence={"parameters": near_bounds},
        )
    ]


def _audit_power_opportunity(
    project: Path, results: dict[str, Any]
) -> list[AuditFinding]:
    targets = _load_targets(project)
    metrics = results.get("metrics") or {}
    power = metrics.get("power_w")
    power_target = targets.get("power_w")
    if power is None or power_target in (None, 0) or power < 0.7 * power_target:
        return []

    generous_margins: dict[str, float] = {}
    for name in ("gain_db", "bandwidth_hz", "phase_margin_deg", "slew_rate_v_per_s"):
        actual = metrics.get("gbw_hz") if name == "bandwidth_hz" else metrics.get(name)
        target = targets.get(name)
        if actual is not None and target not in (None, 0) and actual >= 1.2 * target:
            generous_margins[name] = actual / target - 1.0
    settling = metrics.get("settling_time_s")
    settling_target = targets.get("settling_time_s")
    if settling is not None and settling_target not in (None, 0) and settling <= 0.8 * settling_target:
        generous_margins["settling_time_s"] = 1.0 - settling / settling_target
    if not generous_margins:
        return []
    return [
        AuditFinding(
            code="power_reduction_opportunity",
            severity="warning",
            message="Power is close to its limit while at least one performance metric has over 20% margin.",
            evidence={
                "power_w": power,
                "power_target_w": power_target,
                "performance_margins": generous_margins,
            },
        )
    ]


def _load_targets(project: Path) -> dict[str, Any]:
    for path in (project / "optimization_log.json", project / "requirements.json"):
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        targets = data.get("targets")
        if isinstance(targets, dict):
            return targets
    return {}


def _resolve_value(raw: str | None, params: dict[str, float]) -> float | None:
    if raw is None:
        return None
    if raw in params:
        return params[raw]
    try:
        return parse_spice_value(raw)
    except ValueError:
        return None


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Design Audit Report",
        "",
        f"- Status: `{report['status']}`",
        f"- Blockers: `{report['blocker_count']}`",
        f"- Warnings: `{report['warning_count']}`",
        f"- Netlist: `{report['netlist_file']}`",
        "",
        "## Findings",
        "",
    ]
    if not report["findings"]:
        lines.append("- No design-quality risks were detected by the current rules.")
    for finding in report["findings"]:
        lines.extend(
            [
                f"### {finding['code']}",
                "",
                f"- Severity: `{finding['severity']}`",
                f"- Summary: {finding['message']}",
                f"- Evidence: `{json.dumps(finding['evidence'], ensure_ascii=False)}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Interpretation",
            "",
            "- `blocker`: stop before PVT and prepare Agent Review.",
            "- `warning`: continue to PVT, but preserve the finding for later optimization.",
            "- Geometry thresholds are conservative heuristics, not foundry design rules.",
        ]
    )
    return "\n".join(lines) + "\n"
