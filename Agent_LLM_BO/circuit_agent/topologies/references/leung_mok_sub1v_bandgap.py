"""Leung-Mok sub-1-V CMOS bandgap from IEEE JSSC, April 2002.

This topology implements the complete Fig. 3 architecture: the divided-input
bandgap core, MS1-MS4 startup circuit, RSB/MSB forward-body-bias generator, and
the self-biased PMOS-input amplifier with QA16/QA17 dc level shifting.
"""

from __future__ import annotations

from typing import Any

from models import DesignTarget, ParamDef, ParamSpace, format_spice_value
from pdk_integration.profiles import get_pdk_profile_for_params, spectre_include_line
from topologies.references.bandgap_ptat import (
    BandgapPTAT,
    _TB_LINE_TEMPLATE,
    _TB_PSRR_TEMPLATE,
    _TB_STARTUP_TEMPLATE,
    _TB_TEMPERATURE_TEMPLATE,
)
from topologies.base import ExecutableChildSpec, PassiveImplementation, TopologyMeta


class LeungMokSub1VBandgap(BandgapPTAT):
    """603-mV bandgap with no low-threshold MOS requirement."""

    PASSIVE_IMPLEMENTATIONS = (
        PassiveImplementation("R2A1", "resistor", "bandgap_resistor"),
        PassiveImplementation("R2A2", "resistor", "bandgap_resistor"),
        PassiveImplementation("R1Dev", "resistor", "bandgap_resistor"),
        PassiveImplementation("R2B1", "resistor", "bandgap_resistor"),
        PassiveImplementation("R2B2", "resistor", "bandgap_resistor"),
        PassiveImplementation("R3Dev", "resistor", "bandgap_resistor"),
        PassiveImplementation("RSBDev", "resistor", "startup_resistor"),
        PassiveImplementation("CcompDev", "capacitor", "compensation_capacitor"),
        PassiveImplementation("CloadDev", "capacitor", "load_capacitor", "external"),
    )

    STARTUP_INTERNAL_SAVES = ""

    meta = TopologyMeta(
        name="leung_mok_sub1v_bandgap",
        display_name="Leung-Mok Sub-1-V Bandgap",
        description=(
            "Sub-1-V divided-input CMOS bandgap with forward-biased PMOS "
            "bodies, BJT level shifting, and autonomous startup."
        ),
        min_gain_db=0,
        max_gain_db=0,
        min_gbw_hz=0,
        max_gbw_hz=0,
        typical_power_w=18e-6,
        complexity=5,
    )

    DEFAULT_PARAMS: dict[str, float] = {
        # Fig. 2 first-order ratios: R2/R1 ~= 5.5 and R3/R2 ~= 0.48.
        "R1": 50e3,
        "R2_HIGH": 250e3,
        "R2_LOW": 25e3,
        "R3": 132e3,
        "RSB": 300e3,
        "BJT_AREA_RATIO": 64,
        "Ccomp": 2e-12,
        "Cload": 100e-15,
        # Bandgap mirror and startup devices.
        "Wcore_p": 10e-6,
        "Lcore_p": 500e-9,
        "Wstart_p": 2e-6,
        "Wstart_n": 300e-9,
        "Lstart_n": 600e-9,
        # Self-bias and low-voltage amplifier.
        "Wbias_p": 4e-6,
        "Wbias_n": 2e-6,
        "Lbias": 500e-9,
        "Wamp_p": 4e-6,
        "Wdiff_p": 20e-6,
        "Wamp_n": 4e-6,
        "Lamp": 500e-9,
        "Ldiff_p": 500e-9,
    }

    def generate_circuit(self, params: dict[str, Any] | None = None) -> str:
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)
        values = {name: _fmt(p[name]) for name in self.DEFAULT_PARAMS}
        values["BJT_AREA_RATIO"] = int(round(p["BJT_AREA_RATIO"]))
        return _CIRCUIT_TEMPLATE.format(
            spectre_include=spectre_include_line(pdk),
            nmos_model=pdk.nmos_model,
            pmos_model=pdk.pmos_model,
            pnp_model=pdk.resolve_model("pnp"),
            **values,
        )

    def generate_testbench(
        self,
        params: dict[str, Any] | None = None,
        analysis_type: str = "startup",
    ) -> str:
        pdk = get_pdk_profile_for_params(params)
        p = self._merge_params_with_preset(params)
        defaults = self._testbench_defaults_with_preset(
            {
                "VDD": 1.0,
                "VDD_MIN": max(0.98, pdk.vdd_min),
                "VDD_MAX": pdk.vdd_max,
                "TEMP_MIN": 0.0,
                "TEMP_MAX": 100.0,
                "CL": p["Cload"],
            }
        )
        vdd = defaults["VDD"]
        vdd_min = defaults["VDD_MIN"]
        vdd_max = defaults["VDD_MAX"]
        temp_min = defaults["TEMP_MIN"]
        temp_max = defaults["TEMP_MAX"]
        cload = defaults["CL"]
        if params:
            vdd = params.get("VDD", vdd)
            vdd_min = params.get("VDD_MIN", vdd_min)
            vdd_max = params.get("VDD_MAX", vdd_max)
            temp_min = params.get("TEMP_MIN", temp_min)
            temp_max = params.get("TEMP_MAX", temp_max)
            cload = params.get("CL", cload)

        if analysis_type in ("startup", "tran", "sr"):
            rendered = _TB_STARTUP_TEMPLATE.format(
                VDD=vdd,
                CL=_fmt(cload),
                STARTUP_INTERNAL_SAVES=self.STARTUP_INTERNAL_SAVES,
            )
        elif analysis_type in ("temperature", "temp", "nonlinearity"):
            rendered = _TB_TEMPERATURE_TEMPLATE.format(
                VDD=vdd,
                CL=_fmt(cload),
                TEMP_MIN=temp_min,
                TEMP_MAX=temp_max,
            )
        elif analysis_type in ("line", "line_regulation"):
            line_step = max((vdd_max - vdd_min) / 20.0, 1e-3)
            rendered = _TB_LINE_TEMPLATE.format(
                VDD=vdd,
                VDD_MIN=vdd_min,
                VDD_MAX=vdd_max,
                VDD_STEP=line_step,
                CL=_fmt(cload),
            )
        elif analysis_type in ("psrr", "ac"):
            rendered = _TB_PSRR_TEMPLATE.format(VDD=vdd, CL=_fmt(cload))
        else:
            raise ValueError(
                f"Unsupported Leung-Mok bandgap analysis type: {analysis_type}"
            )
        return rendered.replace("bandgap_ptat", self.meta.name).replace(
            "Bandgap/PTAT", "Leung-Mok Sub-1-V Bandgap"
        )

    def get_default_params(self) -> dict[str, float]:
        return self._default_params_with_preset()

    def get_param_space(self) -> ParamSpace:
        return self._apply_param_space_overrides(ParamSpace(params=[
            ParamDef("R1", 10e3, 200e3, log_scale=True, unit="Ohm"),
            ParamDef("R2_HIGH", 50e3, 1e6, log_scale=True, unit="Ohm"),
            ParamDef("R2_LOW", 2e3, 100e3, log_scale=True, unit="Ohm"),
            ParamDef("R3", 20e3, 500e3, log_scale=True, unit="Ohm"),
            ParamDef("RSB", 50e3, 1e6, log_scale=True, unit="Ohm"),
            ParamDef(
                "BJT_AREA_RATIO", 16, 128,
                log_scale=False, unit="x", value_type="int",
            ),
            ParamDef("Ccomp", 0.1e-12, 20e-12, log_scale=True, unit="F"),
            _width("Wcore_p", 1e-6, 100e-6),
            ParamDef("Lcore_p", 200e-9, 1e-6, log_scale=True, unit="m"),
            _width("Wstart_p", 0.5e-6, 20e-6),
            _width("Wstart_n", 0.2e-6, 5e-6),
            ParamDef("Lstart_n", 200e-9, 1.2e-6, log_scale=True, unit="m"),
            _width("Wbias_p", 0.5e-6, 50e-6),
            _width("Wbias_n", 0.5e-6, 50e-6),
            ParamDef("Lbias", 200e-9, 1e-6, log_scale=True, unit="m"),
            _width("Wamp_p", 0.5e-6, 100e-6),
            _width("Wdiff_p", 1e-6, 200e-6),
            _width("Wamp_n", 0.5e-6, 100e-6),
            ParamDef("Lamp", 200e-9, 1e-6, log_scale=True, unit="m"),
            ParamDef("Ldiff_p", 200e-9, 1e-6, log_scale=True, unit="m"),
        ]))

    def get_gmid_spec(self, targets: DesignTarget | None = None):
        # The body-forward-biased, BJT-level-shifted amplifier needs coupled
        # branch-current constraints that the current generic sizer cannot express.
        return None

    def required_model_roles(self) -> tuple[str, ...]:
        return ("nmos", "pmos", "pnp")

    def critical_operating_point_instances(self) -> set[str]:
        return {
            "Xdut.M1", "Xdut.M2", "Xdut.M3",
            "Xdut.MA05", "Xdut.MA08", "Xdut.MA09",
            "Xdut.MA12", "Xdut.MA13", "Xdut.MA14", "Xdut.MA15",
            "Xdut.MSB",
        }

    def get_hierarchical_blocks(
        self,
        targets: DesignTarget | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[ExecutableChildSpec]:
        return []


def _width(name: str, low: float, high: float) -> ParamDef:
    return ParamDef(
        name, low, high, log_scale=True, unit="m", max_per_finger=2.6e-6,
    )


def _fmt(value: float) -> str:
    return format_spice_value(float(value))


_CIRCUIT_TEMPLATE = """\
// leung_mok_sub1v_bandgap.cir -- Leung and Mok, JSSC April 2002, Fig. 3
simulator lang=spectre insensitive=yes

{spectre_include}

parameters R1={R1} R2_HIGH={R2_HIGH} R2_LOW={R2_LOW} R3={R3} RSB={RSB}
parameters BJT_AREA_RATIO={BJT_AREA_RATIO} Ccomp={Ccomp} Cload={Cload}
parameters Wcore_p={Wcore_p} Lcore_p={Lcore_p}
parameters Wstart_p={Wstart_p} Wstart_n={Wstart_n} Lstart_n={Lstart_n}
parameters Wbias_p={Wbias_p} Wbias_n={Wbias_n} Lbias={Lbias}
parameters Wamp_p={Wamp_p} Wdiff_p={Wdiff_p} Wamp_n={Wamp_n}
parameters Lamp={Lamp} Ldiff_p={Ldiff_p}

subckt leung_mok_sub1v_bandgap (vref vdd vss)
// Fig. 2 bandgap core. The amplifier enforces n1=n2 and therefore n3=n4.
M1 (n3 vg vdd vb) {pmos_model} w=Wcore_p l=Lcore_p nf=1
M2 (n4 vg vdd vb) {pmos_model} w=Wcore_p l=Lcore_p nf=1
M3 (vref vg vdd vb) {pmos_model} w=Wcore_p l=Lcore_p nf=1
R2A1 (n3 n1) resistor r=R2_HIGH
R2A2 (n1 vss) resistor r=R2_LOW
R1Dev (n3 q1_e) resistor r=R1
Q1 (vss vss q1_e) {pnp_model} m=BJT_AREA_RATIO
R2B1 (n4 n2) resistor r=R2_HIGH
R2B2 (n2 vss) resistor r=R2_LOW
Q2 (vss vss n4) {pnp_model} m=1
R3Dev (vref vss) resistor r=R3
CcompDev (vg n3) capacitor c=Ccomp

// MS1-MS4 startup circuit. MS1/MS2 form the sensing inverter.
MS1 (nstart vg vdd vb) {pmos_model} w=Wcore_p l=Lcore_p nf=1
MS2 (nstart vg vss vss) {nmos_model} w=Wstart_n l=Lstart_n nf=1
MS3 (n4 nstart vdd vb) {pmos_model} w=Wstart_p l=Lcore_p nf=1
MS4 (nbias_n nstart vdd vb) {pmos_model} w=Wstart_p l=Lcore_p nf=1

// Forward-body-bias generator. RSB/MSB establish vb below vdd.
RSBDev (vdd vb) resistor r=RSB
MA01 (nbias_n vg vdd vb) {pmos_model} w=Wbias_p l=Lbias nf=1
MA02 (nbias_n nbias_n vss vss) {nmos_model} w=Wbias_n l=Lbias nf=1
MA03 (pbias nbias_n vss vss) {nmos_model} w=Wbias_n l=Lbias nf=1
MSB (vb nbias_n vss vss) {nmos_model} w=Wbias_n l=Lbias nf=1

// Fig. 3 self-biased low-voltage amplifier.
MA04 (pbias pbias vdd vb) {pmos_model} w=Wamp_p l=Lamp nf=1
MA05 (ndiff_tail pbias vdd vb) {pmos_model} w=Wamp_p l=Lamp nf=1
MA06 (pcas_l pbias vdd vb) {pmos_model} w=Wamp_p l=Lamp nf=1
MA07 (pcas_r pbias vdd vb) {pmos_model} w=Wamp_p l=Lamp nf=1

// MA08/MA09 are the only PMOS devices whose bodies remain at vdd.
MA08 (ndiff_l n1 ndiff_tail vdd) {pmos_model} w=Wdiff_p l=Ldiff_p nf=1
MA09 (ndiff_r n2 ndiff_tail vdd) {pmos_model} w=Wdiff_p l=Ldiff_p nf=1

// QA16/QA17 provide the dc level shift into the NMOS mirror inputs.
QA16 (vss nbase_l ndiff_l) {pnp_model} m=1
QA17 (vss nbase_r ndiff_r) {pnp_model} m=1
MA10 (nbase_l nbase_l vss vss) {nmos_model} w=Wamp_n l=Lamp nf=1
MA11 (nbase_r nbase_r vss vss) {nmos_model} w=Wamp_n l=Lamp nf=1
MA12 (nmirror_l nbase_l vss vss) {nmos_model} w=Wamp_n l=Lamp nf=1
MA13 (vg nbase_r vss vss) {nmos_model} w=Wamp_n l=Lamp nf=1

// Cascoded PMOS mirror converts the differential currents to vg.
MA14 (nmirror_l nmirror_l pcas_l vb) {pmos_model} w=Wamp_p l=Lamp nf=1
MA15 (vg nmirror_l pcas_r vb) {pmos_model} w=Wamp_p l=Lamp nf=1

CloadDev (vref vss) capacitor c=Cload
ends leung_mok_sub1v_bandgap
"""
