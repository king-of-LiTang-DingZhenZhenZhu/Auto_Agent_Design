"""Four-bit behavioral SAR ADC for end-to-end functional verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from models import CircuitFiles, DesignTarget, MetricGoal, ParamDef, ParamSpace
from pdk_integration.profiles import get_pdk_profile_for_params
from topologies.base import BaseTopology, TopologyMeta


class SARADCFunctional4Bit(BaseTopology):
    """Ideal unipolar SAR loop with one decision per comparison clock."""

    meta = TopologyMeta(
        name="sar_adc_functional_4bit",
        display_name="4-bit Functional SAR ADC",
        description=(
            "Behavioral sample/hold, ideal CDAC/comparator, and four-cycle "
            "successive-approximation controller for flow verification."
        ),
        min_gain_db=0,
        max_gain_db=0,
        min_gbw_hz=0,
        max_gbw_hz=0,
        typical_power_w=0,
        complexity=1,
    )

    DEFAULT_PARAMS: dict[str, float] = {
        "VREF": 0.9,
        "SAMPLE_RATE_HZ": 500e3,
    }

    def generate_circuit(self, params: dict[str, Any] | None = None) -> str:
        values = self._merge_params_with_preset(params)
        return _CIRCUIT_TEMPLATE.format(VREF=_fmt(values["VREF"]))

    def generate_testbench(
        self,
        params: dict[str, Any] | None = None,
        analysis_type: str = "adc_functional",
    ) -> str:
        if analysis_type not in {"adc_functional", "functional", "tran"}:
            raise ValueError(f"Unsupported SAR ADC analysis type: {analysis_type}")

        values = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)
        vdd = float(params.get("VDD", pdk.vdd) if params else pdk.vdd)
        vref = float(values["VREF"])
        sample_rate_hz = float(values["SAMPLE_RATE_HZ"])
        if vref <= 0 or sample_rate_hz <= 0:
            raise ValueError("VREF and SAMPLE_RATE_HZ must be positive")

        conversion_period = 1.0 / sample_rate_hz
        clock_period = conversion_period / 5.0
        clock_delay = 0.75 * clock_period
        tran_stop = 16.0 * conversion_period
        return _TESTBENCH_TEMPLATE.format(
            VDD=_fmt(vdd),
            VREF=_fmt(vref),
            CLOCK_DELAY=_fmt(clock_delay),
            CLOCK_WIDTH=_fmt(0.5 * clock_period),
            CLOCK_PERIOD=_fmt(clock_period),
            TRAN_STOP=_fmt(tran_stop),
            MAXSTEP=_fmt(clock_period / 50.0),
            VIN_WAVE=_pwl_input(vref, conversion_period),
            START_WAVE=_pwl_start(vdd, conversion_period),
        )

    def get_circuit_files(
        self,
        params: dict[str, Any] | None = None,
    ) -> CircuitFiles:
        circuit = self.generate_circuit(params)
        return CircuitFiles(
            circuit_netlist=circuit,
            testbenches=[self.generate_testbench(params)],
            circuit_name=CircuitFiles.extract_subckt_name(circuit),
            testbench_suffixes=["adc_functional"],
            auxiliary_files=self.get_auxiliary_files(params),
        )

    def write_project(
        self,
        project_dir: str | Path,
        targets: DesignTarget | None = None,
        params: dict[str, Any] | None = None,
        original_requirement: str = "",
    ) -> Path:
        generation_params = dict(params or {})
        if targets:
            custom = targets.custom_specs
            if "reference_voltage_v" in custom:
                generation_params["VREF"] = float(custom["reference_voltage_v"])
            if "sample_rate_hz" in custom:
                generation_params["SAMPLE_RATE_HZ"] = float(custom["sample_rate_hz"])
        return super().write_project(
            project_dir,
            targets=targets,
            params=generation_params,
            original_requirement=original_requirement,
        )

    def get_auxiliary_files(
        self,
        params: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        return {"sar_adc_functional_4bit.va": _VERILOG_A_MODEL}

    def get_default_params(self) -> dict[str, float]:
        return self._default_params_with_preset()

    def get_param_space(self) -> ParamSpace:
        vref = float(self.get_default_params()["VREF"])
        return ParamSpace(params=[
            ParamDef("VREF", low=vref, high=vref, log_scale=False, unit="V")
        ])

    def required_model_roles(self) -> tuple[str, ...]:
        return ()

    def supports_schematic_generation(self) -> bool:
        return False


def default_sar_adc_functional_targets(
    sample_rate_hz: float = 500e3,
) -> DesignTarget:
    """Return hard functional checks for the ideal four-bit conversion loop."""
    clock_period = 1.0 / sample_rate_hz / 5.0
    return DesignTarget(
        topology_hint="4-bit behavioral SAR ADC",
        custom_specs={
            "implementation_level": "behavioral",
            "resolution_bits": 4,
            "high_segment_bits": 2,
            "low_segment_bits": 2,
            "sample_rate_hz": sample_rate_hz,
            "reference_voltage_v": 0.9,
        },
        metric_goals={
            "conversion_success_rate": MetricGoal(
                constraint="min", target=1.0
            ),
            "max_code_error_lsb": MetricGoal(constraint="max", target=0.0),
            "missing_code_count": MetricGoal(constraint="max", target=0.0),
            "monotonicity_violation_count": MetricGoal(
                constraint="max", target=0.0
            ),
            "conversion_time_max_s": MetricGoal(
                constraint="max", target=4.0 * clock_period
            ),
        },
    )


def _pwl_input(vref: float, conversion_period: float) -> str:
    points = [(0.0, 0.5 * vref / 16.0)]
    for code in range(1, 16):
        boundary = code * conversion_period
        points.extend([
            (boundary, (code - 0.5) * vref / 16.0),
            (boundary + 0.02 * conversion_period, (code + 0.5) * vref / 16.0),
        ])
    return " ".join(f"{_fmt(time)} {_fmt(value)}" for time, value in points)


def _pwl_start(vdd: float, conversion_period: float) -> str:
    points = [(0.0, 0.0)]
    for code in range(16):
        base = code * conversion_period
        points.extend([
            (base + 0.05 * conversion_period, 0.0),
            (base + 0.055 * conversion_period, vdd),
            (base + 0.08 * conversion_period, vdd),
            (base + 0.085 * conversion_period, 0.0),
        ])
    return " ".join(f"{_fmt(time)} {_fmt(value)}" for time, value in points)


def _fmt(value: float) -> str:
    from models import format_spice_value

    return format_spice_value(float(value))


_CIRCUIT_TEMPLATE = """\
// sar_adc_functional_4bit.cir -- functional verification model only
simulator lang=spectre insensitive=yes

parameters VREF={VREF}
ahdl_include "sar_adc_functional_4bit.va"

subckt sar_adc_functional_4bit (vin start clk eoc d3 d2 d1 d0 vdac vin_sampled vdd vss)
Aadc (vin start clk eoc d3 d2 d1 d0 vdac vin_sampled vdd vss) sar_adc_functional_4bit_core vref=VREF
ends sar_adc_functional_4bit
"""


_TESTBENCH_TEMPLATE = """\
// Exhaustive 16-code functional test
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VREF={VREF}
VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
VREFmetric (vref_metric 0) vsource type=dc dc=VREF
VINsrc (vin 0) vsource type=pwl wave=[{VIN_WAVE}]
STARTsrc (start 0) vsource type=pwl wave=[{START_WAVE}]
CLKsrc (clk 0) vsource type=pulse val0=0 val1=VDD delay={CLOCK_DELAY} rise=1n fall=1n width={CLOCK_WIDTH} period={CLOCK_PERIOD}

Xdut (vin start clk eoc d3 d2 d1 d0 vdac vin_sampled vdd 0) sar_adc_functional_4bit

outOpts options rawfmt=psfascii
adcFunctionalTran tran stop={TRAN_STOP} maxstep={MAXSTEP}

save vin start clk eoc d3 d2 d1 d0 vdac vin_sampled vdd vref_metric
"""


_VERILOG_A_MODEL = r"""`include "constants.vams"
`include "disciplines.vams"

module sar_adc_functional_4bit_core(
    vin, start, clk, eoc, d3, d2, d1, d0, vdac, vin_sampled, vdd, vss
);
    input vin, start, clk, vdd, vss;
    output eoc, d3, d2, d1, d0, vdac, vin_sampled;
    electrical vin, start, clk, eoc, d3, d2, d1, d0;
    electrical vdac, vin_sampled, vdd, vss;

    parameter real vref = 0.9 from (0:inf);
    parameter real transition_time = 1n from (0:inf);

    integer code;
    integer bit_index;
    integer busy;
    integer eoc_state;
    integer trial_code;
    real sampled_input;

    analog begin
        @(initial_step) begin
            code = 0;
            bit_index = 3;
            busy = 0;
            eoc_state = 0;
            sampled_input = 0.0;
        end

        @(cross(V(start, vss) - 0.5 * V(vdd, vss), +1)) begin
            sampled_input = V(vin, vss);
            if (sampled_input < 0.0)
                sampled_input = 0.0;
            if (sampled_input > vref)
                sampled_input = vref;
            code = 0;
            bit_index = 3;
            busy = 1;
            eoc_state = 0;
        end

        @(cross(V(clk, vss) - 0.5 * V(vdd, vss), +1)) begin
            if (busy != 0) begin
                if (bit_index == 3)
                    trial_code = code + 8;
                else if (bit_index == 2)
                    trial_code = code + 4;
                else if (bit_index == 1)
                    trial_code = code + 2;
                else
                    trial_code = code + 1;
                if (sampled_input >= vref * trial_code / 16.0)
                    code = trial_code;
                if (bit_index == 0) begin
                    busy = 0;
                    eoc_state = 1;
                end else begin
                    bit_index = bit_index - 1;
                end
            end
        end

        V(eoc, vss) <+ transition(eoc_state * V(vdd, vss), 0, transition_time);
        V(d3, vss) <+ transition(((code / 8) % 2) * V(vdd, vss), 0, transition_time);
        V(d2, vss) <+ transition(((code / 4) % 2) * V(vdd, vss), 0, transition_time);
        V(d1, vss) <+ transition(((code / 2) % 2) * V(vdd, vss), 0, transition_time);
        V(d0, vss) <+ transition((code % 2) * V(vdd, vss), 0, transition_time);
        V(vdac, vss) <+ transition(vref * code / 16.0, 0, transition_time);
        V(vin_sampled, vss) <+ transition(sampled_input, 0, transition_time);
    end
endmodule
"""
