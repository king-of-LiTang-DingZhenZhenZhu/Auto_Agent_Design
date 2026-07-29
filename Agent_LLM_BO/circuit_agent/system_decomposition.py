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


def _normalize_system_type(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
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


def decompose_system(request: SystemDesignRequest) -> SystemDesignSpec:
    key = _normalize_system_type(request.system_type)
    rule = _RULES.get(key)
    if rule is None:
        available = ", ".join(sorted(set(_RULES)))
        raise SystemDecompositionError(
            f"Unsupported system_type '{request.system_type}'. Available: {available}"
        )
    return rule(request)


@register_system_rule("bandgap", "bandgap_ptat", "ptat")
def decompose_bandgap(request: SystemDesignRequest) -> SystemDesignSpec:
    architecture = "opamp_assisted_pnp_bandgap"
    if request.architecture_hint and request.architecture_hint != architecture:
        raise SystemDecompositionError(
            f"Unsupported bandgap architecture_hint: {request.architecture_hint}"
        )

    opamp_targets, opamp_pvt, derivations = _derive_bandgap_opamp_targets(
        request.targets
    )
    core_targets = DesignTarget(
        vref_v=request.targets.vref_v,
        vref_tolerance_v=request.targets.vref_tolerance_v,
        tempco_ppm_per_c=request.targets.tempco_ppm_per_c,
        vref_temp_nonlinearity_v=request.targets.vref_temp_nonlinearity_v,
        psrr_db=request.targets.psrr_db,
        line_regulation_v_per_v=request.targets.line_regulation_v_per_v,
    )
    startup_targets = DesignTarget(
        startup_time_s=request.targets.startup_time_s
    )
    power_budget = request.targets.power_w
    blocks = (
        SystemBlockSpec(
            block_id="core",
            function="Generate and sum CTAT VBE with scaled PTAT DeltaVBE",
            implementation="parent_internal",
            targets=core_targets,
            operating_conditions={"device_family": "PNP", "trim": False},
        ),
        SystemBlockSpec(
            block_id="bias",
            function="Generate mirror currents for the reference core",
            implementation="parent_internal",
            dependencies=("core",),
            budget={
                "power_w": power_budget * 0.5
                if power_budget is not None
                else 1e-3
            },
        ),
        SystemBlockSpec(
            block_id="startup",
            function="Force the core away from the zero-current equilibrium",
            implementation="parent_internal",
            dependencies=("bias", "core"),
            targets=startup_targets,
        ),
        SystemBlockSpec(
            block_id="opamp",
            function="Regulate the two bandgap core sense nodes",
            implementation="hierarchical_child",
            candidate_topologies=(
                "two_stage_ota",
                "folded_cascode_two_stage",
            ),
            selected_topology="two_stage_ota",
            ports=("vip", "vin", "vout", "ibias", "vdd", "vss"),
            expected_subckt="two_stage_ota",
            dependencies=("bias", "core"),
            targets=opamp_targets,
            pvt_targets=opamp_pvt,
            derivations=derivations,
            operating_conditions={
                "load_cap_f": opamp_targets.load_cap_f,
                "input_common_mode_source": "bandgap core sense nodes",
                "output_load_source": "PMOS mirror gate and parasitics",
            },
            budget={"power_w": float(opamp_targets.power_w or 0.0)},
            sizing_policy="frozen_macro",
            netlist_param="opamp_netlist",
            results_param="opamp_results",
        ),
    )
    return SystemDesignSpec(
        system_type="bandgap",
        architecture=architecture,
        parent_topology="bandgap_ptat",
        request=request,
        blocks=blocks,
        connections=(
            SystemConnection("core", "opamp", "vinp/vinn", "sense imbalance"),
            SystemConnection("bias", "opamp", "ibias", "opamp bias current"),
            SystemConnection("opamp", "core", "vg", "mirror gate control"),
            SystemConnection("startup", "core", "startup current", "escape zero-current state"),
        ),
        rationale=(
            "The available parent topology implements an opamp-assisted PNP bandgap.",
            "The error amplifier is independently optimized and frozen before parent BO.",
        ),
        assumptions=(
            "Child and parent use the same PDK profile and voltage domain.",
            "The first version does not perform joint child-parent W/L optimization.",
            "Default opamp targets are conservative placeholders until loop-level extraction is available.",
        ),
        unresolved_requirements=_bandgap_unresolved_requirements(request.targets),
    )


def derive_bandgap_opamp_targets(
    targets: DesignTarget | None = None,
) -> DesignTarget:
    request = SystemDesignRequest(
        system_type="bandgap",
        targets=targets or DesignTarget(),
    )
    spec = decompose_bandgap(request)
    return next(block.targets for block in spec.blocks if block.block_id == "opamp")


@register_system_rule("ldo", "capless_ldo", "cap_less_ldo")
def decompose_ldo(request: SystemDesignRequest) -> SystemDesignSpec:
    architecture = "pmos_pass_capless_ldo"
    if request.architecture_hint and request.architecture_hint != architecture:
        raise SystemDecompositionError(
            f"Unsupported LDO architecture_hint: {request.architecture_hint}"
        )

    error_amp_targets, error_amp_pvt, derivations = _derive_ldo_error_amp_targets(
        request.targets
    )
    output_voltage = float(
        request.targets.custom_specs.get("output_voltage_v", 0.9)
    )
    input_voltage = float(
        request.targets.custom_specs.get("input_voltage_v", 1.8)
    )
    load_max = float(
        request.targets.custom_specs.get("load_current_max_a", 10e-3)
    )
    blocks = (
        SystemBlockSpec(
            block_id="reference",
            function="Provide the external feedback reference voltage",
            implementation="parent_internal",
            operating_conditions={
                "interface": "external vref port",
                "nominal_v": output_voltage / 2.0,
            },
        ),
        SystemBlockSpec(
            block_id="error_amp",
            function="Amplify feedback error and drive the PMOS pass gate",
            implementation="hierarchical_child",
            candidate_topologies=("two_stage_ota",),
            selected_topology="two_stage_ota",
            ports=("vip", "vin", "vout", "ibias", "vdd", "vss"),
            expected_subckt="two_stage_ota",
            dependencies=("reference", "feedback"),
            targets=error_amp_targets,
            pvt_targets=error_amp_pvt,
            derivations=derivations,
            operating_conditions={
                "supply_v": input_voltage,
                "load_cap_f": error_amp_targets.load_cap_f,
                "input_common_mode_v": output_voltage / 2.0,
            },
            budget={"power_w": float(error_amp_targets.power_w or 0.0)},
            sizing_policy="frozen_macro",
            netlist_param="error_amp_netlist",
            results_param="error_amp_results",
        ),
        SystemBlockSpec(
            block_id="pass_device",
            function="Source up to the specified load current from VIN to VOUT",
            implementation="parent_internal",
            dependencies=("error_amp",),
            operating_conditions={
                "polarity": "PMOS",
                "input_voltage_v": input_voltage,
                "output_voltage_v": output_voltage,
                "load_current_max_a": load_max,
            },
        ),
        SystemBlockSpec(
            block_id="feedback",
            function="Scale VOUT to the external reference voltage",
            implementation="parent_internal",
            dependencies=("pass_device", "reference"),
        ),
        SystemBlockSpec(
            block_id="compensation",
            function="Stabilize the LDO from zero load through full load",
            implementation="parent_internal",
            dependencies=("error_amp", "pass_device", "feedback"),
            operating_conditions={
                "load_cap_min_f": request.targets.custom_specs.get(
                    "load_cap_min_f", 1e-12
                ),
                "load_cap_max_f": request.targets.custom_specs.get(
                    "load_cap_max_f", 200e-12
                ),
            },
        ),
    )
    return SystemDesignSpec(
        system_type="ldo",
        architecture=architecture,
        parent_topology="capless_ldo",
        request=request,
        blocks=blocks,
        connections=(
            SystemConnection("reference", "error_amp", "vref", "regulation target"),
            SystemConnection("feedback", "error_amp", "vfb", "output sensing"),
            SystemConnection("error_amp", "pass_device", "vg", "gate control"),
            SystemConnection("pass_device", "feedback", "vout", "regulated output"),
            SystemConnection(
                "compensation",
                "error_amp",
                "frequency shaping",
                "loop stability",
            ),
        ),
        rationale=(
            "A PMOS high-side pass device supports 1.8 V to 0.9 V conversion without charge-pump gate drive.",
            "The error amplifier is optimized and frozen before parent-level LDO BO.",
            "The parent BO adjusts only pass-device, feedback, bleed, and compensation parameters.",
        ),
        assumptions=(
            "The active PDK profile provides verified 1.8 V IO NMOS/PMOS models and a matching gm/Id table.",
            "The first version receives a verified 0.45 V reference through an external port.",
            "LDR 30 uA/mA is interpreted as 30 uV/mA, equal to 0.03 V/A.",
            "Output-voltage tolerance defaults to plus or minus 10 mV.",
        ),
        unresolved_requirements=_ldo_unresolved_requirements(request),
    )


def derive_ldo_error_amp_targets(
    targets: DesignTarget | None = None,
) -> DesignTarget:
    request = SystemDesignRequest(
        system_type="ldo",
        targets=targets or DesignTarget(),
    )
    spec = decompose_ldo(request)
    return next(
        block.targets for block in spec.blocks if block.block_id == "error_amp"
    )


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


def _derive_bandgap_opamp_targets(
    targets: DesignTarget,
) -> tuple[DesignTarget, DesignTarget, tuple[TargetDerivation, ...]]:
    custom = dict(targets.custom_specs)
    power_budget = targets.power_w
    load_cap = targets.load_cap_f
    gain = float(custom.get("opamp_gain_db", 70.0))
    gbw = float(custom.get("opamp_gbw_hz", 10e6))
    pm = float(custom.get("opamp_pm_deg", 60.0))
    power = (
        float(custom.get("opamp_power_w", power_budget * 0.5))
        if power_budget is not None
        else float(custom.get("opamp_power_w", 1e-3))
    )
    child_load = float(custom.get("opamp_load_cap_f", load_cap or 1e-12))
    pvt_gain = float(custom.get("opamp_pvt_gain_db", min(gain, 60.0)))
    pvt_gbw = float(custom.get("opamp_pvt_gbw_hz", gbw * 0.5))
    pvt_pm = float(custom.get("opamp_pvt_pm_deg", min(pm, 55.0)))

    child = DesignTarget(
        gain_db=gain,
        bandwidth_hz=gbw,
        phase_margin_deg=pm,
        power_w=power,
        load_cap_f=child_load,
        topology_hint="two_stage_ota",
        custom_specs={
            "derived_from": "bandgap_ptat",
            "error_amplifier_role": "frozen_macro",
        },
    )
    pvt = DesignTarget(
        gain_db=pvt_gain,
        bandwidth_hz=pvt_gbw,
        phase_margin_deg=pvt_pm,
        power_w=power,
        load_cap_f=child_load,
        topology_hint="two_stage_ota",
    )
    assumptions = (
        "The bandgap loop is slow relative to the selected opamp GBW.",
        "The load estimate includes mirror-gate and compensation capacitance.",
    )
    derivations = (
        TargetDerivation(
            "gain_db",
            "bandgap loop regulation and Vref error budget",
            "default 60 dB PVT requirement plus 10 dB nominal margin",
            gain,
            pvt_gain,
            f"{gain - pvt_gain:g} dB nominal margin",
            assumptions,
        ),
        TargetDerivation(
            "bandwidth_hz",
            "startup and line-regulation settling",
            "default nominal GBW is 2x the PVT requirement",
            gbw,
            pvt_gbw,
            f"{gbw / pvt_gbw:g}x nominal margin" if pvt_gbw else "",
            assumptions,
        ),
        TargetDerivation(
            "phase_margin_deg",
            "closed-loop stability",
            "default 55 degree PVT requirement plus nominal margin",
            pm,
            pvt_pm,
            f"{pm - pvt_pm:g} degree nominal margin",
            assumptions,
        ),
        TargetDerivation(
            "power_w",
            "parent power budget",
            "allocate 50% of parent power to the child opamp",
            power,
            power,
            "hard allocation",
            ("The remaining budget covers core, bias, and startup branches.",),
        ),
        TargetDerivation(
            "load_cap_f",
            "parent mirror-gate load estimate",
            "use explicit child load override or parent load/default estimate",
            child_load,
            child_load,
            "operating condition",
            assumptions,
        ),
    )
    return child, pvt, derivations


def _derive_ldo_error_amp_targets(
    targets: DesignTarget,
) -> tuple[DesignTarget, DesignTarget, tuple[TargetDerivation, ...]]:
    custom = dict(targets.custom_specs)
    parent_gain = float(targets.gain_db or 60.0)
    parent_gbw = float(targets.bandwidth_hz or 1e6)
    parent_pm = float(targets.phase_margin_deg or 60.0)
    gain = float(custom.get("error_amp_gain_db", parent_gain + 10.0))
    gbw = float(custom.get("error_amp_gbw_hz", max(10e6, parent_gbw * 10.0)))
    pm = float(custom.get("error_amp_pm_deg", max(65.0, parent_pm + 5.0)))
    power = float(custom.get("error_amp_power_w", 100e-6))
    load_cap = float(custom.get("error_amp_load_cap_f", 5e-12))
    pvt_gain = float(custom.get("error_amp_pvt_gain_db", parent_gain))
    pvt_gbw = float(custom.get("error_amp_pvt_gbw_hz", max(5e6, parent_gbw * 5.0)))
    pvt_pm = float(custom.get("error_amp_pvt_pm_deg", parent_pm))

    nominal = DesignTarget(
        gain_db=gain,
        bandwidth_hz=gbw,
        phase_margin_deg=pm,
        power_w=power,
        load_cap_f=load_cap,
        topology_hint="two_stage_ota",
        custom_specs={
            "derived_from": "capless_ldo",
            "error_amplifier_role": "frozen_macro",
        },
    )
    pvt = DesignTarget(
        gain_db=pvt_gain,
        bandwidth_hz=pvt_gbw,
        phase_margin_deg=pvt_pm,
        power_w=power,
        load_cap_f=load_cap,
        topology_hint="two_stage_ota",
    )
    assumptions = (
        "The child load estimate includes PMOS pass-gate and compensation capacitance.",
        "Parent STB, not standalone child PM, is the final loop-stability criterion.",
    )
    derivations = (
        TargetDerivation(
            "gain_db",
            "LDO loop-gain requirement",
            "add 10 dB nominal child margin",
            gain,
            pvt_gain,
            f"{gain - pvt_gain:g} dB nominal margin",
            assumptions,
        ),
        TargetDerivation(
            "bandwidth_hz",
            "LDO loop GBW requirement",
            "target child GBW at least 10x parent loop GBW",
            gbw,
            pvt_gbw,
            f"{gbw / pvt_gbw:g}x nominal/PVT ratio",
            assumptions,
        ),
        TargetDerivation(
            "phase_margin_deg",
            "zero-load LDO stability requirement",
            "add 5 degree standalone nominal margin",
            pm,
            pvt_pm,
            f"{pm - pvt_pm:g} degree nominal margin",
            assumptions,
        ),
        TargetDerivation(
            "load_cap_f",
            "pass-gate and compensation loading",
            "use a conservative 5 pF default unless overridden",
            load_cap,
            load_cap,
            "operating condition",
            assumptions,
        ),
    )
    return nominal, pvt, derivations


def _bandgap_unresolved_requirements(targets: DesignTarget) -> tuple[str, ...]:
    unresolved = []
    if targets.vref_v is None:
        unresolved.append("vref_v is not specified")
    if targets.tempco_ppm_per_c is None:
        unresolved.append("tempco_ppm_per_c is not specified")
    if targets.startup_time_s is None:
        unresolved.append("startup_time_s is not specified")
    if targets.psrr_db is None:
        unresolved.append("psrr_db is not specified")
    if targets.line_regulation_v_per_v is None:
        unresolved.append("line_regulation_v_per_v is not specified")
    return tuple(unresolved)


def _ldo_unresolved_requirements(
    request: SystemDesignRequest,
) -> tuple[str, ...]:
    unresolved = []
    if not request.voltage_domain:
        unresolved.append("verified 1.8 V IO voltage_domain is not selected")
    if "load_regulation_interpretation" not in request.targets.custom_specs:
        unresolved.append("confirm that LDR means uV/mA rather than uA/mA")
    if "output_voltage_tolerance_v" not in request.targets.custom_specs:
        unresolved.append("confirm the allowed 0.9 V output tolerance")
    return tuple(unresolved)


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
