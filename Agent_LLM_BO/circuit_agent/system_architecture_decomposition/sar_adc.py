"""SAR ADC system decomposition rule."""

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

@register_system_rule(
    "sar_adc",
    "sar-adc",
    "successive_approximation_adc",
    "successive approximation adc",
)
def decompose_sar_adc(request: SystemDesignRequest) -> SystemDesignSpec:
    """Build either the physical SAR budget or the 4-bit functional route."""
    architecture = "single_ended_segmented_charge_redistribution"
    if request.architecture_hint and request.architecture_hint != architecture:
        raise SystemDecompositionError(
            f"Unsupported SAR ADC architecture_hint: {request.architecture_hint}"
        )

    custom = dict(request.targets.custom_specs)
    implementation_level = str(
        custom.get("implementation_level", "physical")
    ).strip().lower()
    if implementation_level not in {"physical", "behavioral"}:
        raise SystemDecompositionError(
            "SAR ADC implementation_level must be 'physical' or 'behavioral'"
        )
    behavioral = implementation_level == "behavioral"
    resolution_bits = int(custom.get("resolution_bits", 4 if behavioral else 12))
    if behavioral and resolution_bits != 4:
        raise SystemDecompositionError(
            "Behavioral SAR ADC validation currently supports exactly 4 bits"
        )
    sample_rate_hz = float(custom.get("sample_rate_hz", 500e3))
    reference_voltage_v = float(
        custom.get("reference_voltage_v", 0.9 if behavioral else 2.5)
    )
    high_segment_bits = int(custom.get("high_segment_bits", 2 if behavioral else 6))
    low_segment_bits = int(
        custom.get("low_segment_bits", resolution_bits - high_segment_bits)
    )
    if resolution_bits < 2:
        raise SystemDecompositionError("SAR ADC resolution_bits must be at least 2")
    if sample_rate_hz <= 0 or reference_voltage_v <= 0:
        raise SystemDecompositionError(
            "SAR ADC sample_rate_hz and reference_voltage_v must be positive"
        )
    if high_segment_bits + low_segment_bits != resolution_bits:
        raise SystemDecompositionError(
            "SAR ADC high_segment_bits + low_segment_bits must equal resolution_bits"
        )

    lsb_v = reference_voltage_v / (2**resolution_bits)
    conversion_period_s = 1.0 / sample_rate_hz
    serial_clock_hz = float(
        custom.get(
            "serial_clock_hz",
            (5 if behavioral else 30) * sample_rate_hz,
        )
    )
    comparison_clock_hz = float(
        custom.get(
            "comparison_clock_hz",
            serial_clock_hz if behavioral else 0.5 * serial_clock_hz,
        )
    )
    if serial_clock_hz <= 0 or comparison_clock_hz <= 0:
        raise SystemDecompositionError("SAR ADC clocks must be positive")
    comparison_period_s = 1.0 / comparison_clock_hz
    unit_cap_f = float(custom.get("unit_cap_f", 100e-15))
    unit_cap_count = int(
        custom.get(
            "unit_cap_count",
            2**high_segment_bits + 2**low_segment_bits,
        )
    )
    if unit_cap_f <= 0 or unit_cap_count <= 0:
        raise SystemDecompositionError(
            "SAR ADC unit_cap_f and unit_cap_count must be positive"
        )
    total_power_w = float(request.targets.power_w or custom.get("power_w", 1.2e-3))
    comparator_power_w = float(custom.get("comparator_power_w", 600e-6))
    reference_buffer_power_w = float(custom.get("reference_buffer_power_w", 300e-6))
    cdac_power_w = float(custom.get("cdac_power_w", 100e-6))
    sar_logic_power_w = float(custom.get("sar_logic_power_w", 100e-6))
    clock_power_w = float(
        custom.get(
            "clock_power_w",
            max(
                total_power_w
                - comparator_power_w
                - reference_buffer_power_w
                - cdac_power_w
                - sar_logic_power_w,
                0.0,
            ),
        )
    )
    allocated_power_w = (
        comparator_power_w
        + reference_buffer_power_w
        + cdac_power_w
        + sar_logic_power_w
        + clock_power_w
    )
    if total_power_w <= 0 or allocated_power_w > total_power_w + 1e-18:
        raise SystemDecompositionError(
            "SAR ADC block power budgets exceed the top-level power budget"
        )

    comparator_targets = DesignTarget(
        power_w=comparator_power_w,
        topology_hint="offset-calibrated multistage comparator",
        custom_specs={
            "input_common_mode_v": 0.5 * reference_voltage_v,
            "input_resolution_v": float(
                custom.get("comparator_input_resolution_v", 0.2e-3)
            ),
            "offset_correction_range_v": float(
                custom.get("comparator_offset_correction_range_v", 10e-3)
            ),
            "input_referred_noise_v_rms": float(
                custom.get("comparator_input_noise_v_rms", 110e-6)
            ),
            "comparison_clock_hz": comparison_clock_hz,
        },
        metric_goals={
            "input_resolution_v": {
                "constraint": "max",
                "target": float(
                    custom.get("comparator_input_resolution_v", 0.2e-3)
                ),
            },
            "offset_correction_range_v": {
                "constraint": "min",
                "target": float(
                    custom.get("comparator_offset_correction_range_v", 10e-3)
                ),
            },
            "input_referred_noise_v_rms": {
                "constraint": "max",
                "target": float(
                    custom.get("comparator_input_noise_v_rms", 110e-6)
                ),
            },
            "propagation_delay_worst_s": {
                "constraint": "max",
                "target": 0.5 * comparison_period_s,
            },
        },
    )
    comparator_pvt_targets = DesignTarget(
        power_w=comparator_power_w,
        topology_hint="offset-calibrated multistage comparator",
        metric_goals={
            "input_resolution_v": {
                "constraint": "max",
                "target": 0.5 * lsb_v,
            },
            "offset_correction_range_v": {
                "constraint": "min",
                "target": float(
                    comparator_targets.custom_specs[
                        "offset_correction_range_v"
                    ]
                ),
            },
            "input_referred_noise_v_rms": {
                "constraint": "max",
                "target": min(0.5 * lsb_v, 150e-6),
            },
            "propagation_delay_worst_s": {
                "constraint": "max",
                "target": 0.75 * comparison_period_s,
            },
        },
    )
    comparator_derivations = (
        TargetDerivation(
            metric="input_resolution_v",
            source="12-bit CDAC LSB and thesis comparator result",
            rule="use the demonstrated 0.2 mV decision level, below 0.5 LSB",
            nominal_value=float(
                comparator_targets.custom_specs["input_resolution_v"]
            ),
            pvt_value=0.5 * lsb_v,
            margin=f"{0.5 * lsb_v:g} V half-LSB ceiling",
            assumptions=("Offset calibration is enabled before conversion.",),
        ),
        TargetDerivation(
            metric="propagation_delay_worst_s",
            source="SAR bit-cycle timing budget",
            rule="allocate at most half of one comparison clock period",
            nominal_value=0.5 * comparison_period_s,
            pvt_value=0.75 * comparison_period_s,
            margin="remaining half-cycle is reserved for CDAC settling and logic",
        ),
    )
    if behavioral:
        comparator_targets = DesignTarget(
            topology_hint="ideal behavioral comparator",
            custom_specs={"comparison_clock_hz": comparison_clock_hz},
        )
        comparator_pvt_targets = DesignTarget(
            topology_hint="ideal behavioral comparator"
        )
        comparator_derivations = ()

    if behavioral:
        rationale = (
            "A four-cycle ideal SAR loop provides the smallest end-to-end conversion check.",
            "A 2+2 logical split preserves the segmented-CDAC interface without modeling physical capacitors.",
            "Straight-binary code centers exercise all 16 output codes at 500 kS/s.",
        )
        assumptions = (
            "The sample/hold, CDAC, comparator, reference, and logic are ideal behavioral blocks.",
            "Power, noise, mismatch, DNL/INL, SNDR/ENOB, and PVT are outside this validation route.",
            "Passing this route does not qualify the transistor-level SAR architecture.",
        )
    else:
        rationale = (
            "A charge-redistribution SAR avoids a residue amplifier and suits the low-power, moderate-speed target.",
            "A symmetric 6+6 split reduces the ideal 12-bit binary array from 4096 to 128 unit capacitors.",
            "Thermometer coding the top three bits improves high-code monotonicity.",
            "The paper's single-ended interface is retained for traceability; a differential redesign is preferred for a new tapeout.",
        )
        assumptions = (
            "Defaults reproduce the thesis target: 2.5 V, 12 bit, 500 kS/s, and 1.2 mW.",
            "The 15 MHz serial clock and divide-by-two comparison clock provide 30 serial clocks per conversion.",
            "The power allocation is a first-pass budget and must be rebalanced after block characterization.",
            "CDAC parasitics and bridge-cap correction must be extracted before final linearity signoff.",
        )

    blocks = (
        SystemBlockSpec(
            block_id="sampling_switch",
            function="Track the single-ended rail-to-rail input onto the CDAC",
            implementation="parent_internal",
            ports=("vin", "sample", "cdac_top", "vdd_a", "vss_a"),
            operating_conditions={
                "input_range_v": [0.0, reference_voltage_v],
                "sample_rate_hz": sample_rate_hz,
                "settling_error_limit_v": 0.25 * lsb_v,
            },
        ),
        SystemBlockSpec(
            block_id="cdac",
            function="Sample and perform charge-redistribution DAC trials",
            implementation="parent_internal",
            ports=("cdac_top", "vin", "vref", "vcm", "code", "enable"),
            dependencies=("sampling_switch",),
            operating_conditions={
                "segmentation_bits": [high_segment_bits, low_segment_bits],
                "thermometer_coded_msb_bits": int(
                    custom.get("thermometer_coded_msb_bits", 0 if behavioral else 3)
                ),
                "unit_cap_f": unit_cap_f,
                "unit_cap_count": unit_cap_count,
                "effective_input_cap_f": float(
                    custom.get("effective_input_cap_f", 6.5e-12)
                ),
                "layout": (
                    "not modeled"
                    if behavioral
                    else "common-centroid unit-cap array with grounded dummies"
                ),
            },
            budget={
                "power_w": cdac_power_w,
                "ktc_noise_v_rms": float(custom.get("cdac_ktc_noise_v_rms", 25e-6)),
                "settling_error_v": 0.25 * lsb_v,
            },
        ),
        SystemBlockSpec(
            block_id="comparator",
            function=(
                "Resolve each CDAC trial with an ideal behavioral decision"
                if behavioral
                else "Resolve each CDAC trial with offset calibration"
            ),
            implementation="parent_internal",
            candidate_topologies=(
                ()
                if behavioral
                else (
                    "offset_calibrated_three_preamplifier_latch",
                    "strongarm_latch",
                )
            ),
            ports=("cdac_top", "vcm", "compare", "decision", "vdd_a", "vss_a"),
            dependencies=("cdac", "reference_buffer"),
            targets=comparator_targets,
            pvt_targets=comparator_pvt_targets,
            derivations=comparator_derivations,
            operating_conditions={
                "clock_hz": comparison_clock_hz,
                "input_common_mode_v": 0.5 * reference_voltage_v,
                "kickback_sensitive_source_cap_f": float(
                    custom.get("effective_input_cap_f", 6.5e-12)
                ),
            },
            budget=(
                {}
                if behavioral
                else {
                    "power_w": comparator_power_w,
                    "input_noise_v_rms": float(
                        comparator_targets.custom_specs["input_referred_noise_v_rms"]
                    ),
                    "offset_correction_range_v": float(
                        comparator_targets.custom_specs["offset_correction_range_v"]
                    ),
                }
            ),
            sizing_policy=("parent_internal" if behavioral else "new_topology_required"),
        ),
        SystemBlockSpec(
            block_id="reference_buffer",
            function="Provide VCM and recharge the CDAC/reference network",
            implementation="parent_internal",
            ports=("vref", "vcm", "cdac_ref", "vdd_a", "vss_a"),
            operating_conditions={
                "reference_voltage_v": reference_voltage_v,
                "common_mode_v": 0.5 * reference_voltage_v,
                "load_cap_f": float(custom.get("effective_input_cap_f", 6.5e-12)),
                "settling_time_s": 0.5 * comparison_period_s,
            },
            budget={"power_w": reference_buffer_power_w},
        ),
        SystemBlockSpec(
            block_id="sar_logic",
            function=(
                "Run the four-step straight-binary search"
                if behavioral
                else "Run the 12-step binary search and 3-bit thermometer decode"
            ),
            implementation="parent_internal",
            ports=("decision", "code", "enable", "serial_data", "clock"),
            dependencies=("comparator",),
            operating_conditions={
                "resolution_bits": resolution_bits,
                "serial_clock_hz": serial_clock_hz,
                "comparison_clock_hz": comparison_clock_hz,
                "clocks_per_conversion": int(
                    custom.get("clocks_per_conversion", 5 if behavioral else 30)
                ),
                "output_format": "serial",
            },
            budget={"power_w": sar_logic_power_w},
        ),
        SystemBlockSpec(
            block_id="clock_powerdown",
            function="Sequence calibration/sample/compare/readout and gate idle power",
            implementation="parent_internal",
            ports=("cs_n", "sclk", "enable", "compare", "powerdown"),
            dependencies=("sar_logic",),
            operating_conditions={
                "conversion_period_s": conversion_period_s,
                "powerdown_supported": True,
            },
            budget={"power_w": clock_power_w},
        ),
    )
    return SystemDesignSpec(
        system_type="sar_adc",
        architecture=architecture,
        parent_topology=(
            "sar_adc_functional_4bit"
            if behavioral
            else "sar_adc_segmented_cdac"
        ),
        request=request,
        blocks=blocks,
        connections=(
            SystemConnection("sampling_switch", "cdac", "sampled charge", "sample VIN"),
            SystemConnection("sar_logic", "cdac", "trial code", "binary search"),
            SystemConnection("reference_buffer", "cdac", "VREF/VCM", "DAC references"),
            SystemConnection("cdac", "comparator", "residue", "trial decision input"),
            SystemConnection("reference_buffer", "comparator", "VCM", "comparison reference"),
            SystemConnection("comparator", "sar_logic", "decision", "accept/reject bit"),
            SystemConnection("sar_logic", "clock_powerdown", "conversion state", "sequencing"),
            SystemConnection("clock_powerdown", "sampling_switch", "sample", "track/hold control"),
            SystemConnection("clock_powerdown", "comparator", "compare/calibrate", "phase control"),
        ),
        rationale=rationale,
        assumptions=assumptions,
        unresolved_requirements=_sar_adc_unresolved_requirements(request),
    )


def _sar_adc_unresolved_requirements(
    request: SystemDesignRequest,
) -> tuple[str, ...]:
    implementation_level = str(
        request.targets.custom_specs.get("implementation_level", "physical")
    ).strip().lower()
    if implementation_level == "behavioral":
        return (
            "behavioral validation only; transistor-level SAR parent topology is not implemented",
            "ADC DNL/INL and dynamic SNDR/ENOB testbenches are not implemented",
        )
    unresolved = [
        "sar_adc_segmented_cdac parent topology is not implemented",
        "offset-calibrated multistage comparator topology is not implemented",
        "ADC static/dynamic code-domain testbenches and metric parsers are not implemented",
    ]
    if not request.voltage_domain:
        unresolved.append("verified 2.5 V analog/digital voltage domains are not selected")
    if "reference_source" not in request.constraints:
        unresolved.append("external VREF/VCM source accuracy and drive are not specified")
    return tuple(unresolved)
