"""Modified StrongARM dynamic comparator from Razavi, Fig. 1(b)."""

from __future__ import annotations

from typing import Any

from models import (
    CircuitFiles,
    DesignTarget,
    MetricGoal,
    ParamDef,
    ParamSpace,
    format_spice_value,
)
from pdk_integration.profiles import get_pdk_profile_for_params, spectre_include_line
from topologies.base import BaseTopology, TopologyMeta


class StrongARMLatch(BaseTopology):
    """Four-switch StrongARM latch with rail-to-rail differential outputs."""

    meta = TopologyMeta(
        name="strongarm_latch",
        display_name="Modified StrongARM Latch Comparator",
        description=(
            "Clocked NMOS input pair, cross-coupled NMOS/PMOS latch, tail "
            "switch, and four PMOS precharge devices from Razavi Fig. 1(b)."
        ),
        min_gain_db=0,
        max_gain_db=0,
        min_gbw_hz=0,
        max_gbw_hz=0,
        typical_power_w=100e-6,
        complexity=3,
    )

    DEFAULT_PARAMS: dict[str, float] = {
        "Winput_n": 2e-6,
        "Linput_n": 120e-9,
        "Wtail_n": 2e-6,
        "Ltail_n": 120e-9,
        "Wlatch_n": 1e-6,
        "Llatch_n": 120e-9,
        "Wlatch_p": 2e-6,
        "Llatch_p": 120e-9,
        "Wpre_p": 1e-6,
        "Lpre_p": 120e-9,
    }

    def generate_circuit(self, params: dict[str, Any] | None = None) -> str:
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)
        return _CIRCUIT_TEMPLATE.format(
            spectre_include=spectre_include_line(pdk),
            nmos_model=pdk.nmos_model,
            pmos_model=pdk.pmos_model,
            Winput_n=_fmt(p["Winput_n"]),
            Linput_n=_fmt(p["Linput_n"]),
            Wtail_n=_fmt(p["Wtail_n"]),
            Ltail_n=_fmt(p["Ltail_n"]),
            Wlatch_n=_fmt(p["Wlatch_n"]),
            Llatch_n=_fmt(p["Llatch_n"]),
            Wlatch_p=_fmt(p["Wlatch_p"]),
            Llatch_p=_fmt(p["Llatch_p"]),
            Wpre_p=_fmt(p["Wpre_p"]),
            Lpre_p=_fmt(p["Lpre_p"]),
        )

    def generate_testbench(
        self,
        params: dict[str, Any] | None = None,
        analysis_type: str = "decision_pos",
    ) -> str:
        pdk = get_pdk_profile_for_params(params)
        defaults = self._testbench_defaults_with_preset({
            "VCM": 0.5 * pdk.vdd,
            "VDIFF": 10e-3,
            "CL": 5e-15,
            "CLOCK_DELAY": 1e-9,
            "CLOCK_RISE": 10e-12,
            "CLOCK_HIGH": 2e-9,
            "CLOCK_PERIOD": 4e-9,
            "TRAN_STOP": 9e-9,
            "MAXSTEP": 1e-12,
        })
        values = dict(defaults)
        values["VDD"] = pdk.vdd
        if params:
            for name in (*defaults, "VDD"):
                if name in params:
                    values[name] = params[name]

        if analysis_type in {"decision_pos", "positive", "tran"}:
            analysis_name = "decisionPosTran"
            vdiff = abs(float(values["VDIFF"]))
        elif analysis_type in {"decision_neg", "negative"}:
            analysis_name = "decisionNegTran"
            vdiff = -abs(float(values["VDIFF"]))
        else:
            raise ValueError(
                f"Unsupported StrongARM analysis type: {analysis_type}"
            )

        return _TB_DECISION_TEMPLATE.format(
            analysis_name=analysis_name,
            polarity="positive" if vdiff > 0 else "negative",
            VDD=_fmt(values["VDD"]),
            VCM=_fmt(values["VCM"]),
            VDIFF=_fmt(vdiff),
            CL=_fmt(values["CL"]),
            CLOCK_DELAY=_fmt(values["CLOCK_DELAY"]),
            CLOCK_RISE=_fmt(values["CLOCK_RISE"]),
            CLOCK_HIGH=_fmt(values["CLOCK_HIGH"]),
            CLOCK_PERIOD=_fmt(values["CLOCK_PERIOD"]),
            TRAN_STOP=_fmt(values["TRAN_STOP"]),
            MAXSTEP=_fmt(values["MAXSTEP"]),
        )

    def get_circuit_files(
        self,
        params: dict[str, Any] | None = None,
    ) -> CircuitFiles:
        circuit = self.generate_circuit(params)
        return CircuitFiles(
            circuit_netlist=circuit,
            testbenches=[
                self.generate_testbench(params, "decision_pos"),
                self.generate_testbench(params, "decision_neg"),
            ],
            circuit_name=CircuitFiles.extract_subckt_name(circuit),
            testbench_suffixes=["decision_pos", "decision_neg"],
        )

    def get_default_params(self) -> dict[str, float]:
        return self._default_params_with_preset()

    def get_param_space(self) -> ParamSpace:
        return self._apply_param_space_overrides(ParamSpace(params=[
            ParamDef(
                "Winput_n", low=0.5e-6, high=30e-6,
                log_scale=True, unit="m", max_per_finger=2.6e-6,
            ),
            ParamDef(
                "Wtail_n", low=0.5e-6, high=30e-6,
                log_scale=True, unit="m", max_per_finger=2.6e-6,
            ),
            ParamDef(
                "Wlatch_n", low=0.5e-6, high=30e-6,
                log_scale=True, unit="m", max_per_finger=2.6e-6,
            ),
            ParamDef(
                "Wlatch_p", low=0.5e-6, high=40e-6,
                log_scale=True, unit="m", max_per_finger=2.6e-6,
            ),
            ParamDef(
                "Wpre_p", low=0.5e-6, high=30e-6,
                log_scale=True, unit="m", max_per_finger=2.6e-6,
            ),
            ParamDef(
                "Linput_n", low=120e-9, high=500e-9,
                log_scale=True, unit="m",
            ),
            ParamDef(
                "Llatch_n", low=120e-9, high=500e-9,
                log_scale=True, unit="m",
            ),
            ParamDef(
                "Llatch_p", low=120e-9, high=500e-9,
                log_scale=True, unit="m",
            ),
        ]))

    def get_gmid_spec(self, targets: DesignTarget | None = None):
        """Dynamic regeneration is not represented by the DC gm/Id flow."""
        return None

    def required_model_roles(self) -> tuple[str, ...]:
        return ("nmos", "pmos")

    def critical_operating_point_instances(self) -> set[str]:
        """Reset-state DC regions are not valid dynamic-comparator criteria."""
        return set()


def default_strongarm_targets() -> DesignTarget:
    """Conservative characterization targets for the default 0.9 V setup."""
    return DesignTarget(
        power_w=100e-6,
        topology_hint="StrongARM latch comparator",
        custom_specs={
            "input_common_mode_v": 0.45,
            "input_difference_v": 10e-3,
            "clock_period_s": 4e-9,
            "load_cap_f": 5e-15,
        },
        metric_goals={
            "decision_positive_margin_v": MetricGoal(
                constraint="min", target=0.45
            ),
            "decision_negative_margin_v": MetricGoal(
                constraint="min", target=0.45
            ),
            "propagation_delay_positive_s": MetricGoal(
                constraint="max", target=1e-9
            ),
            "propagation_delay_negative_s": MetricGoal(
                constraint="max", target=1e-9
            ),
            "energy_per_decision_j": MetricGoal(
                constraint="max", target=200e-15,
                objective="minimize",
            ),
        },
    )


def _fmt(value: float) -> str:
    return format_spice_value(float(value))


_CIRCUIT_TEMPLATE = """\
// strongarm_latch.cir -- Modified StrongARM latch, Razavi Fig. 1(b)
simulator lang=spectre insensitive=yes

{spectre_include}

parameters Winput_n={Winput_n} Linput_n={Linput_n}
parameters Wtail_n={Wtail_n} Ltail_n={Ltail_n}
parameters Wlatch_n={Wlatch_n} Llatch_n={Llatch_n}
parameters Wlatch_p={Wlatch_p} Llatch_p={Llatch_p}
parameters Wpre_p={Wpre_p} Lpre_p={Lpre_p}

// Port polarity: vip > vin resolves outp high and outn low.
subckt strongarm_latch (vip vin clk outp outn vdd vss)
// M1-M2: clocked NMOS input pair; M7: tail switch.
M1 (p vip ntail vss) {nmos_model} w=Winput_n l=Linput_n nf=1 m=1
M2 (q vin ntail vss) {nmos_model} w=Winput_n l=Linput_n nf=1 m=1
M7 (ntail clk vss vss) {nmos_model} w=Wtail_n l=Ltail_n nf=1 m=1

// M3-M4 and M5-M6: cross-coupled regenerative pairs.
M3 (outn outp p vss) {nmos_model} w=Wlatch_n l=Llatch_n nf=1 m=1
M4 (outp outn q vss) {nmos_model} w=Wlatch_n l=Llatch_n nf=1 m=1
M5 (outn outp vdd vdd) {pmos_model} w=Wlatch_p l=Llatch_p nf=1 m=1
M6 (outp outn vdd vdd) {pmos_model} w=Wlatch_p l=Llatch_p nf=1 m=1

// S1-S4 precharge P, Q, X(outn), and Y(outp) when clk is low.
S1 (p clk vdd vdd) {pmos_model} w=Wpre_p l=Lpre_p nf=1 m=1
S2 (q clk vdd vdd) {pmos_model} w=Wpre_p l=Lpre_p nf=1 m=1
S3 (outn clk vdd vdd) {pmos_model} w=Wpre_p l=Lpre_p nf=1 m=1
S4 (outp clk vdd vdd) {pmos_model} w=Wpre_p l=Lpre_p nf=1 m=1
ends strongarm_latch
"""


_TB_DECISION_TEMPLATE = """\
// StrongARM {polarity}-input decision, delay, and energy characterization
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} VDIFF={VDIFF} CL={CL}
parameters CLOCK_DELAY={CLOCK_DELAY} CLOCK_RISE={CLOCK_RISE}
parameters CLOCK_HIGH={CLOCK_HIGH} CLOCK_PERIOD={CLOCK_PERIOD}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
VIPsrc (vip 0) vsource type=dc dc=VCM+VDIFF/2
VINsrc (vin 0) vsource type=dc dc=VCM-VDIFF/2
CLKsrc (clk 0) vsource type=pulse val0=0 val1=VDD delay=CLOCK_DELAY rise=CLOCK_RISE fall=CLOCK_RISE width=CLOCK_HIGH period=CLOCK_PERIOD

Xdut (vip vin clk outp outn vdd vss) strongarm_latch
CloadP (outp 0) capacitor c=CL
CloadN (outn 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
{analysis_name} tran stop={TRAN_STOP} maxstep={MAXSTEP}

save vip vin clk outp outn vdd
save VDDsrc:p
"""
