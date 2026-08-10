"""Map ideal DUT resistors/capacitors to characterized PDK devices."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from models import format_spice_value
from passive_devices.mapping import (
    CapacitorMapper,
    PassiveDeviceEvaluator,
    PassiveMappingError,
    PassiveMappingConstraints,
    PassiveMappingResult,
    ResistorMapper,
    build_passive_evaluator,
    map_passive,
    map_passive_candidates,
)
from pdk_integration.profiles import PDKProfile, PassiveDeviceProfile
from topologies.base import PassiveImplementation
from virtuoso_schematic_generation.parser import parse_netlist


@dataclass(frozen=True)
class PassiveCandidate:
    achieved_value: float
    unit_value: float
    params: dict[str, object]
    series_units: int = 1
    parallel_units: int = 1
    unit_area_m2: float | None = None


@dataclass(frozen=True)
class PassiveRealization:
    instance: str
    kind: str
    role: str
    device: str
    target_value: float
    achieved_value: float
    relative_error: float
    series_units: int
    parallel_units: int
    total_area_m2: float | None
    params: dict[str, object]
    mapping_mode: str
    evaluator_backend: str = ""
    evaluator_metadata: dict[str, object] | None = None
    matching: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        if self.kind == "resistor":
            data.update(target_R=self.target_value, actual_R=self.achieved_value)
        elif self.kind == "capacitor":
            data.update(target_C=self.target_value, actual_C=self.achieved_value)
        return data


def map_ideal_netlist_passives(
    netlist_text: str,
    profile: PDKProfile,
    *,
    device_types: Mapping[str, str] | None = None,
    constraints: Mapping[
        str, PassiveMappingConstraints | Mapping[str, object]
    ] | None = None,
    evaluators: Mapping[str, PassiveDeviceEvaluator] | None = None,
) -> tuple[str, list[PassiveRealization]]:
    """Recognize and map every ideal R/C in the first DUT subcircuit.

    Topology-driven flows should continue to use :func:`realize_passives` so
    external/testbench intent is explicit.  This generic entry point supports
    imported ideal DUTs that do not yet have ``PassiveImplementation`` roles.
    """

    ir = parse_netlist(netlist_text, profile=profile)
    replacements: dict[str, list[str]] = {}
    records: list[PassiveRealization] = []
    for instance in ir.instances:
        if instance.model not in {"res", "cap"}:
            continue
        kind = "resistor" if instance.kind == "res" else "capacitor"
        value_key = "R" if instance.kind == "res" else "C"
        target = _parse_engineering_value(instance.params.get(value_key, ""))
        requested_type = (device_types or {}).get(instance.name)
        result = map_passive(
            kind,
            target,
            requested_type,
            (constraints or {}).get(instance.name),
            profile=profile,
            evaluators=evaluators,
        )
        device = profile.passive_devices[result.device_type]
        candidate = _candidate_from_mapping(result)
        replacements[instance.name] = _render_instances(
            instance.name, instance.nodes, device, candidate
        )
        records.append(
            _realization_from_mapping(
                instance=instance.name,
                role="auto_selected",
                device=device,
                result=result,
            )
        )
    return _replace_instance_lines(netlist_text, replacements), records


def realize_passives(
    netlist_text: str,
    implementations: Iterable[PassiveImplementation],
    profile: PDKProfile,
    candidate_ranks: dict[str, int] | None = None,
    evaluators: Mapping[str, PassiveDeviceEvaluator] | None = None,
) -> tuple[str, list[PassiveRealization]]:
    """Return a PDK-passive DUT netlist and its realization audit records."""

    specs = {item.instance: item for item in implementations}
    ir = parse_netlist(netlist_text)
    ideal_instances = {
        instance.name: instance
        for instance in ir.instances
        if instance.kind in {"res", "cap"}
    }
    undeclared = sorted(set(ideal_instances) - set(specs))
    if undeclared:
        raise ValueError(
            "DUT contains ideal passives without implementation metadata: "
            + ", ".join(undeclared)
        )

    replacements: dict[str, list[str]] = {}
    records: list[PassiveRealization] = []
    for name, spec in specs.items():
        if spec.realization in {"external", "testbench"}:
            continue
        if spec.realization == "pdk":
            _validate_existing_pdk_instance(netlist_text, spec, profile)
            continue
        instance = ideal_instances.get(name)
        if instance is None:
            # Some topology variants conditionally omit a declared passive.
            continue
        actual_kind = "resistor" if instance.kind == "res" else "capacitor"
        if actual_kind != spec.kind:
            raise ValueError(
                f"Passive {name} is {actual_kind}, metadata declares {spec.kind}"
            )
        device_name = profile.passive_role_map.get(spec.role)
        if not device_name:
            raise ValueError(
                f"PDK '{profile.name}' has no passive_role_map entry for "
                f"'{spec.role}' required by {name}"
            )
        try:
            device = profile.passive_devices[device_name]
        except KeyError as exc:
            raise ValueError(
                f"Passive role '{spec.role}' references unknown device '{device_name}'"
            ) from exc
        if device.kind != spec.kind:
            raise ValueError(
                f"PDK device '{device_name}' is {device.kind}, but {name} requires {spec.kind}"
            )
        value_key = "R" if instance.kind == "res" else "C"
        target = _parse_engineering_value(instance.params.get(value_key, ""))
        mapped_candidates = map_passive_candidates(
            spec.kind,
            target,
            device_name,
            profile=profile,
            evaluator=(evaluators or {}).get(device_name),
        )
        candidates = [_candidate_from_mapping(item) for item in mapped_candidates]
        rank = (candidate_ranks or {}).get(name, 0)
        if rank >= len(candidates):
            raise ValueError(f"Passive {name} has no realization candidate rank {rank}")
        candidate = candidates[rank]
        error = abs(candidate.achieved_value - target) / target
        if error > device.value_tolerance:
            raise ValueError(
                f"{name} target {target:g} cannot be realized by '{device_name}' "
                f"within {device.value_tolerance:.2%}; best error is {error:.2%}"
            )
        replacements[name] = _render_instances(
            name=name,
            nodes=instance.nodes,
            device=device,
            candidate=candidate,
        )
        total_area = (
            candidate.unit_area_m2
            * candidate.series_units
            * candidate.parallel_units
            if candidate.unit_area_m2 is not None
            else None
        )
        records.append(
            _realization_from_mapping(
                instance=name,
                role=spec.role,
                device=device,
                result=mapped_candidates[rank],
                rendered_params=candidate.params,
                total_area_m2=total_area,
            )
        )

    return _replace_instance_lines(netlist_text, replacements), records


def realize_project_passives(
    results_path: str | Path,
    *,
    simulate: bool = False,
    profile: PDKProfile | None = None,
    evaluators: Mapping[str, PassiveDeviceEvaluator] | None = None,
) -> dict[str, Any]:
    """Realize a BO/review netlist and optionally run its nominal testbenches."""
    from config import settings
    from pdk_integration.profiles import get_pdk_profile
    from pvt_simulation import _load_targets
    from simulator import Simulator
    from topologies import get_topology
    from virtuoso_schematic_generation.exporter import select_pre_realization_netlist

    results_path = Path(results_path).resolve()
    project = results_path.parent
    result_data = json.loads(results_path.read_text(encoding="utf-8"))
    topology_name = str(
        result_data.get("topology_name") or result_data.get("topology") or ""
    )
    selected_netlist, source = select_pre_realization_netlist(results_path, result_data)
    if not topology_name:
        topology_name = parse_netlist(selected_netlist).subckt_name
    topology_name = {"ota_5t": "5t_ota"}.get(topology_name, topology_name)
    topology = get_topology(topology_name)
    implementations = topology.passive_implementations()
    output_dir = project / "passive_realization"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "passive_realization.json"

    if not any(item.realization in {"on_chip", "pdk"} for item in implementations):
        report = {
            "status": "not_required",
            "required": False,
            "verified": True,
            "nominal_pass": True,
            "source": source,
            "source_netlist": str(selected_netlist),
            "netlist_file": str(selected_netlist),
            "realizations": [],
        }
        _write_json(report_path, report)
        return report

    snapshot = project / "pdk_profile_used.json"
    pdk = profile or (
        get_pdk_profile(str(snapshot)) if snapshot.exists() else get_pdk_profile()
    )
    try:
        realized_text, records = realize_passives(
            selected_netlist.read_text(encoding="utf-8"),
            implementations,
            pdk,
            evaluators=evaluators,
        )
    except Exception as exc:
        report = {
            "status": "blocked",
            "required": True,
            "verified": False,
            "nominal_pass": False,
            "source": source,
            "source_netlist": str(selected_netlist),
            "netlist_file": None,
            "realizations": [],
            "error_type": _mapping_error_type(exc),
            "error": str(exc),
        }
        _write_json(report_path, report)
        return report

    realized_netlist = output_dir / "circuit.cir"
    realized_netlist.write_text(realized_text, encoding="utf-8")
    report: dict[str, Any] = {
        "status": "unverified",
        "required": True,
        "verified": False,
        "nominal_pass": None,
        "source": source,
        "source_netlist": str(selected_netlist),
        "netlist_file": str(realized_netlist),
        "pdk_profile": pdk.to_dict(),
        "realizations": [record.to_dict() for record in records],
    }
    if not simulate:
        _write_json(report_path, report)
        return report

    testbenches = _find_testbenches(project, selected_netlist)
    if not testbenches:
        report.update(status="blocked", nominal_pass=False, error="No nominal testbenches found")
        _write_json(report_path, report)
        return report
    simulator = Simulator(settings)
    targets = _load_targets(project, results_path)
    sim_result = _run_nominal(
        simulator, output_dir, realized_text, testbenches
    )
    nominal_pass, target_status = (
        targets.is_satisfied(sim_result) if targets else (False, {})
    )
    attempts: list[dict[str, object]] = []
    if not nominal_pass:
        source_text = selected_netlist.read_text(encoding="utf-8")
        for record in records:
            for rank in (1, 2):
                try:
                    candidate_text, candidate_records = realize_passives(
                        source_text,
                        implementations,
                        pdk,
                        candidate_ranks={record.instance: rank},
                        evaluators=evaluators,
                    )
                except ValueError:
                    break
                candidate_record = next(
                    item for item in candidate_records if item.instance == record.instance
                )
                device = pdk.passive_devices[candidate_record.device]
                if candidate_record.relative_error > device.value_tolerance:
                    break
                candidate_dir = output_dir / "candidates" / f"{record.instance}_rank_{rank}"
                candidate_result = _run_nominal(
                    simulator, candidate_dir, candidate_text, testbenches
                )
                candidate_pass, candidate_status = (
                    targets.is_satisfied(candidate_result) if targets else (False, {})
                )
                attempts.append(
                    {
                        "instance": record.instance,
                        "rank": rank,
                        "netlist_file": str(candidate_dir / "circuit.cir"),
                        "nominal_pass": bool(candidate_pass),
                        "target_status": candidate_status,
                        "simulation_result": candidate_result.to_result_dict(targets=targets),
                    }
                )
                if candidate_pass:
                    shutil.copy2(candidate_dir / "circuit.cir", realized_netlist)
                    sim_result = candidate_result
                    nominal_pass = True
                    target_status = candidate_status
                    records = candidate_records
                    break
            if nominal_pass:
                break
    report.update(
        status="pass" if nominal_pass else "fail",
        verified=bool(nominal_pass),
        nominal_pass=bool(nominal_pass),
        target_status=target_status,
        simulation_result=sim_result.to_result_dict(targets=targets),
        local_adjustment_attempts=attempts,
        realizations=[record.to_dict() for record in records],
    )
    _write_json(report_path, report)
    return report


def _find_testbenches(project: Path, selected_netlist: Path) -> list[Path]:
    local = sorted(selected_netlist.parent.glob("tb*.scs"))
    if local:
        return local
    return sorted((project / "simulation").glob("tb_circuit*.scs"))


def _run_nominal(simulator, run_dir: Path, netlist_text: str, testbenches: list[Path]):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "circuit.cir").write_text(netlist_text, encoding="utf-8")
    tb_paths: list[Path] = []
    for index, source_tb in enumerate(testbenches):
        target_tb = run_dir / ("tb.scs" if index == 0 else f"tb_{index}.scs")
        text = source_tb.read_text(encoding="utf-8")
        text = re.sub(
            r'(?m)^\s*include\s+"[^"]*circuit\.cir"',
            'include "circuit.cir"',
            text,
            count=1,
        )
        target_tb.write_text(text, encoding="utf-8")
        tb_paths.append(target_tb)
    return simulator.run_all_testbenches(tb_paths, run_dir)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _mapping_error_type(error: Exception) -> str:
    if isinstance(error, PassiveMappingError):
        return "mapping_no_solution_or_configuration"
    if isinstance(error, ValueError):
        return "mapping_input_error"
    return "pdk_evaluator_error"


def solve_passive(
    target_value: float,
    device: PassiveDeviceProfile,
) -> PassiveCandidate:
    """Solve the nearest legal PDK implementation for one electrical value."""

    return solve_passive_candidates(target_value, device)[0]


def solve_passive_candidates(
    target_value: float,
    device: PassiveDeviceProfile,
) -> list[PassiveCandidate]:
    """Compatibility wrapper over the PDK-evaluator based mapping engine."""

    mapper_cls = ResistorMapper if device.kind == "resistor" else CapacitorMapper
    mapped = mapper_cls(
        "device", device, build_passive_evaluator("device", device)
    ).map(target_value)
    return [_candidate_from_mapping(item) for item in mapped]


def _candidate_from_mapping(result: PassiveMappingResult) -> PassiveCandidate:
    return PassiveCandidate(
        achieved_value=result.actual_value,
        unit_value=result.unit_value or result.actual_value,
        params={
            key: format_spice_value(value) if isinstance(value, float) else value
            for key, value in result.params.items()
        },
        series_units=result.series_units,
        parallel_units=result.parallel_units,
        unit_area_m2=result.unit_area_m2,
    )


def _realization_from_mapping(
    *,
    instance: str,
    role: str,
    device: PassiveDeviceProfile,
    result: PassiveMappingResult,
    rendered_params: dict[str, object] | None = None,
    total_area_m2: float | None = None,
) -> PassiveRealization:
    return PassiveRealization(
        instance=instance,
        kind=result.device_kind,
        role=role,
        device=result.device_type,
        target_value=result.target_value,
        achieved_value=result.actual_value,
        relative_error=result.relative_error,
        series_units=result.series_units,
        parallel_units=result.parallel_units,
        total_area_m2=(result.area_m2 if total_area_m2 is None else total_area_m2),
        params=rendered_params or _candidate_from_mapping(result).params,
        mapping_mode=device.mapping_mode,
        evaluator_backend=result.evaluator_backend,
        evaluator_metadata=result.evaluator_metadata,
        matching=result.matching,
    )


def _render_instances(
    name: str,
    nodes: list[str],
    device: PassiveDeviceProfile,
    candidate: PassiveCandidate,
) -> list[str]:
    rendered: list[str] = []
    for parallel in range(candidate.parallel_units):
        for series in range(candidate.series_units):
            instance_name = (
                name
                if candidate.parallel_units == candidate.series_units == 1
                else f"{name}__p{parallel + 1}_s{series + 1}"
            )
            left = nodes[0] if series == 0 else f"__pr_{name}_p{parallel + 1}_s{series}"
            right = (
                nodes[1]
                if series == candidate.series_units - 1
                else f"__pr_{name}_p{parallel + 1}_s{series + 1}"
            )
            params = " ".join(
                f"{key}={_format_parameter(value)}"
                for key, value in candidate.params.items()
            )
            rendered.append(
                f"{instance_name} ({left} {right}) {device.spectre_model} {params}".rstrip()
            )
    return rendered


def _replace_instance_lines(
    netlist_text: str,
    replacements: dict[str, list[str]],
) -> str:
    output: list[str] = []
    replaced: set[str] = set()
    for line in netlist_text.splitlines():
        match = re.match(r"^\s*(\S+)\s+\(", line)
        name = match.group(1) if match else ""
        if name in replacements:
            output.extend(replacements[name])
            replaced.add(name)
        else:
            output.append(line)
    missing = sorted(set(replacements) - replaced)
    if missing:
        raise ValueError("Unable to replace passive instance(s): " + ", ".join(missing))
    return "\n".join(output) + ("\n" if netlist_text.endswith("\n") else "")


def _validate_existing_pdk_instance(
    netlist_text: str,
    spec: PassiveImplementation,
    profile: PDKProfile,
) -> None:
    pattern = re.compile(
        rf"(?m)^\s*{re.escape(spec.instance)}\s+\([^)]+\)\s+(\S+)"
    )
    match = pattern.search(netlist_text)
    if not match:
        return
    model = match.group(1)
    configured = [
        name
        for name, device in profile.passive_devices.items()
        if device.kind == spec.kind and device.spectre_model == model
    ]
    if not configured:
        raise ValueError(
            f"Existing PDK passive {spec.instance} uses model '{model}', but the "
            "profile has no matching passive_devices entry"
        )


def _parse_engineering_value(raw: str) -> float:
    match = re.fullmatch(
        r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
        r"(meg|[fpnumkgt]?)\s*",
        raw,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError(f"Cannot resolve passive value '{raw}'")
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
        "t": 1e12,
    }
    return float(match.group(1)) * scales[match.group(2).lower()]


def _format_parameter(value: object) -> str:
    if isinstance(value, float):
        return format_spice_value(value)
    return str(value)
