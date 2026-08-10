"""LDO system decomposition rule."""

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
