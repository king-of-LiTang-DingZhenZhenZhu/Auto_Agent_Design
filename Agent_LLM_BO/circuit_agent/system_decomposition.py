"""Machine-executable system architecture selection and block decomposition."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from models import DesignTarget, parse_metric_goals


class SystemDecompositionError(ValueError):
    """Raised when a system request cannot produce a valid block graph."""


@dataclass(frozen=True)
class SystemDesignRequest:
    system_type: str
    targets: DesignTarget
    original_requirement: str = ""
    architecture_hint: str = ""
    voltage_domain: str | None = None
    constraints: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SystemDesignRequest":
        target_data = dict(data.get("targets", {}))
        topology_hint = str(data.get("topology_hint", ""))
        system_type = str(
            data.get("system_type")
            or data.get("topology_name")
            or topology_hint
        )
        if not system_type:
            raise SystemDecompositionError("system_type is required")
        return cls(
            system_type=system_type,
            targets=_target_from_data(
                target_data,
                topology_hint=topology_hint,
                custom_specs=dict(data.get("custom_specs", {})),
                metric_goals=data.get(
                    "metric_goals", target_data.get("metric_goals")
                ),
            ),
            original_requirement=str(data.get("original_requirement", "")),
            architecture_hint=str(data.get("architecture_hint", "")),
            voltage_domain=(
                str(data["voltage_domain"])
                if data.get("voltage_domain")
                else None
            ),
            constraints=dict(data.get("system_constraints", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_type": self.system_type,
            "original_requirement": self.original_requirement,
            "architecture_hint": self.architecture_hint,
            "voltage_domain": self.voltage_domain,
            "system_constraints": self.constraints,
            **_target_payload(self.targets),
        }


@dataclass(frozen=True)
class TargetDerivation:
    metric: str
    source: str
    rule: str
    nominal_value: float
    pvt_value: float | None = None
    margin: str = ""
    assumptions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "source": self.source,
            "rule": self.rule,
            "nominal_value": self.nominal_value,
            "pvt_value": self.pvt_value,
            "margin": self.margin,
            "assumptions": list(self.assumptions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TargetDerivation":
        return cls(
            metric=str(data["metric"]),
            source=str(data["source"]),
            rule=str(data["rule"]),
            nominal_value=float(data["nominal_value"]),
            pvt_value=(
                float(data["pvt_value"])
                if data.get("pvt_value") is not None
                else None
            ),
            margin=str(data.get("margin", "")),
            assumptions=tuple(str(item) for item in data.get("assumptions", [])),
        )


@dataclass(frozen=True)
class SystemBlockSpec:
    block_id: str
    function: str
    implementation: str
    candidate_topologies: tuple[str, ...] = ()
    selected_topology: str | None = None
    ports: tuple[str, ...] = ()
    expected_subckt: str = ""
    dependencies: tuple[str, ...] = ()
    targets: DesignTarget = field(default_factory=DesignTarget)
    pvt_targets: DesignTarget = field(default_factory=DesignTarget)
    derivations: tuple[TargetDerivation, ...] = ()
    operating_conditions: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, float] = field(default_factory=dict)
    sizing_policy: str = "parent_internal"
    netlist_param: str = ""
    results_param: str = ""

    def __post_init__(self) -> None:
        if self.implementation not in {"parent_internal", "hierarchical_child"}:
            raise SystemDecompositionError(
                f"Unknown implementation for '{self.block_id}': "
                f"{self.implementation}"
            )
        if self.selected_topology and self.candidate_topologies:
            if self.selected_topology not in self.candidate_topologies:
                raise SystemDecompositionError(
                    f"Selected topology '{self.selected_topology}' is not a "
                    f"candidate for block '{self.block_id}'"
                )
        if self.implementation == "hierarchical_child":
            required = (
                self.selected_topology,
                self.expected_subckt,
                self.ports,
                self.netlist_param,
                self.results_param,
            )
            if not all(required):
                raise SystemDecompositionError(
                    f"Hierarchical block '{self.block_id}' has incomplete bindings"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "function": self.function,
            "implementation": self.implementation,
            "candidate_topologies": list(self.candidate_topologies),
            "selected_topology": self.selected_topology,
            "ports": list(self.ports),
            "expected_subckt": self.expected_subckt,
            "dependencies": list(self.dependencies),
            "targets": _target_payload(self.targets),
            "pvt_targets": _target_payload(self.pvt_targets),
            "derivations": [item.to_dict() for item in self.derivations],
            "operating_conditions": self.operating_conditions,
            "budget": self.budget,
            "sizing_policy": self.sizing_policy,
            "netlist_param": self.netlist_param,
            "results_param": self.results_param,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SystemBlockSpec":
        return cls(
            block_id=str(data["block_id"]),
            function=str(data["function"]),
            implementation=str(data["implementation"]),
            candidate_topologies=tuple(
                str(item) for item in data.get("candidate_topologies", [])
            ),
            selected_topology=(
                str(data["selected_topology"])
                if data.get("selected_topology")
                else None
            ),
            ports=tuple(str(item) for item in data.get("ports", [])),
            expected_subckt=str(data.get("expected_subckt", "")),
            dependencies=tuple(str(item) for item in data.get("dependencies", [])),
            targets=_target_from_payload(data.get("targets", {})),
            pvt_targets=_target_from_payload(data.get("pvt_targets", {})),
            derivations=tuple(
                TargetDerivation.from_dict(item)
                for item in data.get("derivations", [])
            ),
            operating_conditions=dict(data.get("operating_conditions", {})),
            budget={
                str(name): float(value)
                for name, value in data.get("budget", {}).items()
            },
            sizing_policy=str(data.get("sizing_policy", "parent_internal")),
            netlist_param=str(data.get("netlist_param", "")),
            results_param=str(data.get("results_param", "")),
        )

    def to_executable_child(self):
        if self.implementation != "hierarchical_child":
            raise SystemDecompositionError(
                f"Block '{self.block_id}' is not a hierarchical child"
            )
        from topologies.base import ExecutableChildSpec

        return ExecutableChildSpec(
            block_id=self.block_id,
            topology_name=str(self.selected_topology),
            expected_subckt=self.expected_subckt,
            ports=self.ports,
            targets=self.targets,
            pvt_targets=self.pvt_targets,
            sizing_policy=self.sizing_policy,
            netlist_param=self.netlist_param,
            results_param=self.results_param,
        )

    def to_hierarchical_block(self):
        """Backward-compatible alias for the old conversion method name."""
        return self.to_executable_child()


@dataclass(frozen=True)
class SystemConnection:
    source: str
    target: str
    signal: str
    purpose: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "signal": self.signal,
            "purpose": self.purpose,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SystemConnection":
        return cls(
            source=str(data["source"]),
            target=str(data["target"]),
            signal=str(data["signal"]),
            purpose=str(data.get("purpose", "")),
        )


@dataclass(frozen=True)
class SystemDesignSpec:
    system_type: str
    architecture: str
    parent_topology: str
    request: SystemDesignRequest
    blocks: tuple[SystemBlockSpec, ...]
    connections: tuple[SystemConnection, ...]
    rationale: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    unresolved_requirements: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        block_ids = [block.block_id for block in self.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise SystemDecompositionError("Block IDs must be unique")
        known = set(block_ids)
        for block in self.blocks:
            missing = set(block.dependencies) - known
            if missing:
                raise SystemDecompositionError(
                    f"Block '{block.block_id}' has unknown dependencies: "
                    f"{sorted(missing)}"
                )
        for edge in self.connections:
            if edge.source not in known or edge.target not in known:
                raise SystemDecompositionError(
                    f"Connection references unknown block: {edge.source}->{edge.target}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "system_type": self.system_type,
            "architecture": self.architecture,
            "parent_topology": self.parent_topology,
            "request": self.request.to_dict(),
            "blocks": [block.to_dict() for block in self.blocks],
            "connections": [edge.to_dict() for edge in self.connections],
            "rationale": list(self.rationale),
            "assumptions": list(self.assumptions),
            "unresolved_requirements": list(self.unresolved_requirements),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SystemDesignSpec":
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            system_type=str(data["system_type"]),
            architecture=str(data["architecture"]),
            parent_topology=str(data["parent_topology"]),
            request=SystemDesignRequest.from_dict(dict(data["request"])),
            blocks=tuple(
                SystemBlockSpec.from_dict(item) for item in data.get("blocks", [])
            ),
            connections=tuple(
                SystemConnection.from_dict(item)
                for item in data.get("connections", [])
            ),
            rationale=tuple(str(item) for item in data.get("rationale", [])),
            assumptions=tuple(str(item) for item in data.get("assumptions", [])),
            unresolved_requirements=tuple(
                str(item) for item in data.get("unresolved_requirements", [])
            ),
        )

    def child_blocks(self) -> tuple[SystemBlockSpec, ...]:
        return tuple(
            block
            for block in self.blocks
            if block.implementation == "hierarchical_child"
        )


DecompositionRule = Callable[[SystemDesignRequest], SystemDesignSpec]
_RULES: dict[str, DecompositionRule] = {}
_BUILTIN_RULES_LOADED = False


def _normalize_system_type(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if "sar" in normalized and "adc" in normalized:
        return "sar_adc"
    if "successive_approximation" in normalized and "adc" in normalized:
        return "sar_adc"
    if "bandgap" in normalized:
        return "bandgap"
    if "ldo" in normalized:
        return "ldo"
    if normalized == "ptat":
        return "ptat"
    return normalized


def register_system_rule(*system_types: str):
    def decorator(rule: DecompositionRule) -> DecompositionRule:
        for system_type in system_types:
            _RULES[_normalize_system_type(system_type)] = rule
        return rule

    return decorator


def _load_builtin_rules() -> None:
    global _BUILTIN_RULES_LOADED
    if _BUILTIN_RULES_LOADED:
        return
    from system_architectures import bandgap, ldo, sar_adc  # noqa: F401

    _BUILTIN_RULES_LOADED = True


def decompose_system(request: SystemDesignRequest) -> SystemDesignSpec:
    _load_builtin_rules()
    key = _normalize_system_type(request.system_type)
    rule = _RULES.get(key)
    if rule is None:
        available = ", ".join(sorted(set(_RULES)))
        raise SystemDecompositionError(
            f"Unsupported system_type '{request.system_type}'. Available: {available}"
        )
    return rule(request)


def write_system_project(
    request: SystemDesignRequest,
    project_dir: str | Path,
) -> tuple[Path, SystemDesignSpec]:
    spec = decompose_system(request)
    from topologies import get_topology

    project = get_topology(spec.parent_topology).write_project(
        project_dir,
        targets=request.targets,
        params={"VOLTAGE_DOMAIN": request.voltage_domain}
        if request.voltage_domain
        else None,
        original_requirement=request.original_requirement,
    )
    design_path = project / "system_design.json"
    design_path.write_text(
        json.dumps(spec.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    requirements_path = project / "requirements.json"
    requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
    requirements["system_type"] = spec.system_type
    requirements["system_architecture"] = spec.architecture
    requirements["system_design"] = "system_design.json"
    requirements_path.write_text(
        json.dumps(requirements, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    hierarchy_path = project / "hierarchy.json"
    if spec.child_blocks() and not hierarchy_path.exists():
        raise SystemDecompositionError(
            f"Parent topology '{spec.parent_topology}' did not generate hierarchy.json"
        )
    return project, spec


def _target_payload(targets: DesignTarget) -> dict[str, Any]:
    requirements = targets.to_requirements_dict()
    return {
        "targets": requirements["targets"],
        "topology_hint": targets.topology_hint,
        "custom_specs": targets.custom_specs,
        "metric_goals": requirements["metric_goals"],
    }


def _target_from_payload(payload: dict[str, Any]) -> DesignTarget:
    data = dict(payload.get("targets", {}))
    return _target_from_data(
        data,
        topology_hint=str(payload.get("topology_hint", "")),
        custom_specs=dict(payload.get("custom_specs", {})),
        metric_goals=payload.get("metric_goals", data.get("metric_goals")),
    )


def _target_from_data(
    data: dict[str, Any],
    *,
    topology_hint: str = "",
    custom_specs: dict[str, Any] | None = None,
    metric_goals: Any = None,
) -> DesignTarget:
    return DesignTarget(
        gain_db=data.get("gain_db"),
        bandwidth_hz=data.get("bandwidth_hz", data.get("gbw_hz")),
        phase_margin_deg=data.get("phase_margin_deg"),
        power_w=data.get("power_w"),
        load_cap_f=data.get("load_cap_f"),
        slew_rate_v_per_s=data.get("slew_rate_v_per_s"),
        settling_time_s=data.get("settling_time_s"),
        vref_v=data.get("vref_v"),
        vref_tolerance_v=data.get("vref_tolerance_v") or 10e-3,
        tempco_ppm_per_c=data.get("tempco_ppm_per_c"),
        vref_temp_nonlinearity_v=data.get("vref_temp_nonlinearity_v"),
        psrr_db=data.get("psrr_db"),
        line_regulation_v_per_v=data.get("line_regulation_v_per_v"),
        startup_time_s=data.get("startup_time_s"),
        topology_hint=topology_hint,
        custom_specs=dict(custom_specs or {}),
        metric_goals=parse_metric_goals(metric_goals),
    )



# Thin wrappers preserve the original public API without creating a circular
# import when callers import a concrete system_architectures module directly.
def decompose_bandgap(request: SystemDesignRequest) -> SystemDesignSpec:
    from system_architectures.bandgap import decompose_bandgap as rule

    return rule(request)


def derive_bandgap_opamp_targets(
    targets: DesignTarget | None = None,
) -> DesignTarget:
    from system_architectures.bandgap import derive_bandgap_opamp_targets as derive

    return derive(targets)


def decompose_ldo(request: SystemDesignRequest) -> SystemDesignSpec:
    from system_architectures.ldo import decompose_ldo as rule

    return rule(request)


def derive_ldo_error_amp_targets(
    targets: DesignTarget | None = None,
) -> DesignTarget:
    from system_architectures.ldo import derive_ldo_error_amp_targets as derive

    return derive(targets)


def decompose_sar_adc(request: SystemDesignRequest) -> SystemDesignSpec:
    from system_architectures.sar_adc import decompose_sar_adc as rule

    return rule(request)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decompose a system-level circuit request into an executable block graph."
    )
    parser.add_argument("--requirements", required=True)
    parser.add_argument("--output")
    parser.add_argument("--project")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.output and not args.project:
        raise SystemExit("At least one of --output or --project is required")
    data = json.loads(Path(args.requirements).read_text(encoding="utf-8"))
    request = SystemDesignRequest.from_dict(data)
    spec = decompose_system(request)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(spec.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    if args.project:
        write_system_project(request, args.project)


if __name__ == "__main__":
    main()
