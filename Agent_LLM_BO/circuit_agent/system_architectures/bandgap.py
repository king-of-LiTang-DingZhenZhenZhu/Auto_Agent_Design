"""Bandgap system decomposition rule."""

from models import DesignTarget
from system_decomposition import (
    SystemBlockSpec,
    SystemConnection,
    SystemDecompositionError,
    SystemDesignRequest,
    SystemDesignSpec,
    TargetDerivation,
    register_system_rule,
)

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
