"""Hierarchical PNP bandgap/PTAT reference topology."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from models import CircuitFiles, DesignTarget, ParamDef, ParamSpace, format_spice_value
from pdk_profiles import get_pdk_profile, get_pdk_profile_for_params, spectre_include_line
from system_decomposition import (
    SystemDesignRequest,
    decompose_bandgap,
    derive_bandgap_opamp_targets,
)
from topologies.base import BaseTopology, ExecutableChildSpec, TopologyMeta
from topologies.two_stage_ota import TwoStageOTA


class BandgapPTAT(BaseTopology):
    """PNP bandgap core with a frozen two-stage OTA error amplifier."""

    STARTUP_INTERNAL_SAVES = "save Xdut.vrs Xdut.sup Xdut.cmp_out"

    meta = TopologyMeta(
        name="bandgap_ptat",
        display_name="Bandgap/PTAT Reference",
        description=(
            "Hierarchical PNP bandgap/PTAT core with MOS startup, PMOS current "
            "mirrors, poly resistors, and a frozen two-stage OTA."
        ),
        min_gain_db=0,
        max_gain_db=0,
        min_gbw_hz=0,
        max_gbw_hz=0,
        typical_power_w=100e-6,
        complexity=5,
    )

    DEFAULT_PARAMS: dict[str, float] = {
        # Boni startup circuit III. Keep legacy keys as the external parameter
        # API: large maps to MX (IX), small maps to MY (IY).
        "Wstart_small": 300e-9,
        "Wstart_large": 600e-9,
        "Lstart_n": 300e-9,
        "Rstart_ref": 100e3,
        "Rstart_bias": 100e3,
        "Wstart_bias_p": 1e-6,
        "Wstart_cmp_p": 1e-6,
        "Wstart_cmp_n": 500e-9,
        "Wstart_inv_p": 1e-6,
        "Wstart_inv_n": 500e-9,
        "Lstart_cmp": 300e-9,
        # PMOS bandgap mirrors.
        "Wmirror_p": 6e-6,
        "Lmirror_p": 600e-9,
        "MREF_RATIO": 4,
        # PNP area ratio and poly-resistor geometry.
        "BJT_AREA_RATIO": 8,
        "R0_SEG_L": 10e-6,
        "R0_SEG_W": 2e-6,
        "R1_SEG_L": 10e-6,
        "R1_SEG_W": 2e-6,
        # Frozen child bias and output load.
        "Iopbias": 20e-6,
        "Cload": 100e-15,
    }

    def generate_circuit(self, params: dict[str, Any] | None = None) -> str:
        """Generate a hierarchical Spectre-native bandgap/PTAT netlist."""
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)
        opamp_netlist = self._load_opamp_netlist(params)

        return _CIRCUIT_TEMPLATE.format(
            spectre_include=spectre_include_line(pdk),
            nmos_model=pdk.nmos_model,
            pmos_model=pdk.pmos_model,
            pnp_model=pdk.resolve_model("pnp"),
            resistor_model=pdk.resolve_model("resistor_poly"),
            Wstart_small=_fmt(p["Wstart_small"]),
            Wstart_large=_fmt(p["Wstart_large"]),
            Lstart_n=_fmt(p["Lstart_n"]),
            Rstart_ref=_fmt(p["Rstart_ref"]),
            Rstart_bias=_fmt(p["Rstart_bias"]),
            Wstart_bias_p=_fmt(p["Wstart_bias_p"]),
            Wstart_cmp_p=_fmt(p["Wstart_cmp_p"]),
            Wstart_cmp_n=_fmt(p["Wstart_cmp_n"]),
            Wstart_inv_p=_fmt(p["Wstart_inv_p"]),
            Wstart_inv_n=_fmt(p["Wstart_inv_n"]),
            Lstart_cmp=_fmt(p["Lstart_cmp"]),
            Wmirror_p=_fmt(p["Wmirror_p"]),
            Lmirror_p=_fmt(p["Lmirror_p"]),
            MREF_RATIO=int(round(p["MREF_RATIO"])),
            Iopbias=_fmt(p["Iopbias"]),
            BJT_AREA_RATIO=int(round(p["BJT_AREA_RATIO"])),
            R0_SEG_L=_fmt(p["R0_SEG_L"]),
            R0_SEG_W=_fmt(p["R0_SEG_W"]),
            R1_SEG_L=_fmt(p["R1_SEG_L"]),
            R1_SEG_W=_fmt(p["R1_SEG_W"]),
            Cload=_fmt(p["Cload"]),
            opamp_netlist=opamp_netlist,
        )

    def generate_testbench(
        self,
        params: dict[str, Any] | None = None,
        analysis_type: str = "startup",
    ) -> str:
        """Generate startup, PSRR, temperature, or line-regulation analysis."""
        pdk = get_pdk_profile_for_params(params)
        p = self._merge_params_with_preset(params)
        tb_defaults = self._testbench_defaults_with_preset({"CL": p["Cload"]})
        vdd = pdk.vdd
        cload = tb_defaults["CL"]
        vdd_min = pdk.vdd_min
        vdd_max = pdk.vdd_max
        temperatures = pdk.pvt_temperatures_c or (-40.0, 27.0, 125.0)

        if params:
            vdd = params.get("VDD", vdd)
            cload = params.get("CL", cload)
            vdd_min = params.get("VDD_MIN", vdd_min)
            vdd_max = params.get("VDD_MAX", vdd_max)

        if analysis_type in ("startup", "tran", "sr"):
            return _TB_STARTUP_TEMPLATE.format(
                VDD=vdd,
                CL=_fmt(cload),
                STARTUP_INTERNAL_SAVES=self.STARTUP_INTERNAL_SAVES,
            )
        if analysis_type in ("temperature", "temp", "nonlinearity"):
            return _TB_TEMPERATURE_TEMPLATE.format(
                VDD=vdd,
                CL=_fmt(cload),
                TEMP_MIN=min(temperatures),
                TEMP_MAX=max(temperatures),
            )
        if analysis_type in ("line", "line_regulation"):
            line_step = max((vdd_max - vdd_min) / 20.0, 1e-3)
            return _TB_LINE_TEMPLATE.format(
                VDD=vdd,
                VDD_MIN=vdd_min,
                VDD_MAX=vdd_max,
                VDD_STEP=line_step,
                CL=_fmt(cload),
            )
        if analysis_type in ("psrr", "ac"):
            return _TB_PSRR_TEMPLATE.format(VDD=vdd, CL=_fmt(cload))
        raise ValueError(f"Unsupported bandgap analysis type: {analysis_type}")

    def get_circuit_files(
        self, params: dict[str, Any] | None = None
    ) -> CircuitFiles:
        circuit_content = self.generate_circuit(params)
        return CircuitFiles(
            circuit_netlist=circuit_content,
            testbenches=[
                self.generate_testbench(params, "startup"),
                self.generate_testbench(params, "psrr"),
                self.generate_testbench(params, "temperature"),
                self.generate_testbench(params, "line_regulation"),
            ],
            circuit_name=CircuitFiles.extract_subckt_name(circuit_content),
            testbench_suffixes=["startup", "psrr", "temperature", "line"],
        )

    def get_default_params(self) -> dict[str, float]:
        return self._default_params_with_preset()

    def get_param_space(self) -> ParamSpace:
        return self._apply_param_space_overrides(ParamSpace(params=[
            ParamDef("R0_SEG_L", low=1e-6, high=20e-6, log_scale=True, unit="m"),
            ParamDef("R1_SEG_L", low=1e-6, high=20e-6, log_scale=True, unit="m"),
            ParamDef(
                "Lmirror_p", low=400e-9, high=800e-9,
                log_scale=True, unit="m",
            ),
        ]))

    def get_gmid_spec(self, targets: DesignTarget | None = None):
        """Size the PMOS mirror from fixed nominal branch current."""
        from models import DerivedBranchCurrentSpec, GmidTopologySpec, TransistorSpec

        pdk = get_pdk_profile()
        pass_through_space = self._apply_param_space_overrides(ParamSpace(params=[
            ParamDef("R0_SEG_L", low=1e-6, high=20e-6, log_scale=True, unit="m"),
            ParamDef("R1_SEG_L", low=1e-6, high=20e-6, log_scale=True, unit="m"),
        ]))
        return GmidTopologySpec(
            derived_branch_currents=[
                DerivedBranchCurrentSpec(
                    name="I_mirror",
                    unit_current=20e-6,
                    multiplier_offset=1.0,
                ),
            ],
            transistors=[
                TransistorSpec(
                    role="mirror_pmos",
                    w_param="Wmirror_p",
                    l_param="Lmirror_p",
                    model=pdk.pmos_model,
                    current_source="I_mirror",
                    current_fraction=1.0,
                    gm_id_low=12,
                    gm_id_high=18,
                    gm_id_default=15,
                    L_low=400e-9,
                    L_high=800e-9,
                    L_default=600e-9,
                    Vds_estimate=0.3,
                    multiplicity=3,
                ),
            ],
            pass_through_params=pass_through_space.params,
        )

    def required_model_roles(self) -> tuple[str, ...]:
        return (
            "nmos", "pmos", "pnp", "resistor_poly",
            "nmos_lvt", "pmos_lvt",
        )

    def critical_operating_point_instances(self) -> set[str]:
        return {
            "Xdut.M10", "Xdut.M11", "Xdut.M12",
            "Xdut.MX", "Xdut.MY", "Xdut.MSUP_P", "Xdut.MSUP_N",
        }

    def derive_opamp_targets(
        self,
        targets: DesignTarget | None = None,
    ) -> DesignTarget:
        """Derive child targets through the system decomposition layer."""
        return derive_bandgap_opamp_targets(targets)

    def get_hierarchical_blocks(
        self,
        targets: DesignTarget | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[ExecutableChildSpec]:
        design = decompose_bandgap(
            SystemDesignRequest(
                system_type="bandgap",
                targets=targets or DesignTarget(),
            )
        )
        return [block.to_executable_child() for block in design.child_blocks()]

    def _load_opamp_netlist(self, params: dict[str, Any] | None = None) -> str:
        source = _get_optional_path(params, "opamp_netlist", "OPAMP_NETLIST")
        if source is not None and source.exists():
            return _sanitize_child_netlist(source.read_text(encoding="utf-8"))
        return _sanitize_child_netlist(TwoStageOTA().generate_circuit())

def _get_optional_path(
    params: dict[str, Any] | None,
    *names: str,
) -> Path | None:
    if not params:
        return None
    for name in names:
        value = params.get(name)
        if value:
            return Path(str(value)).expanduser()
    return None


def _sanitize_child_netlist(netlist: str) -> str:
    """Keep child subckts while removing duplicate top-level setup lines."""
    kept: list[str] = []
    for line in netlist.splitlines():
        stripped = line.strip()
        if stripped.startswith("simulator lang="):
            continue
        if stripped.startswith("include "):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _fmt(value: float) -> str:
    return format_spice_value(float(value))


_CIRCUIT_TEMPLATE = """\
// bandgap_ptat.cir -- Hierarchical PNP Bandgap/PTAT (Spectre native syntax)
simulator lang=spectre insensitive=yes

{spectre_include}

parameters Wstart_y={Wstart_small} Wstart_x={Wstart_large} Lstart={Lstart_n}
parameters Rstart_ref={Rstart_ref} Rstart_bias={Rstart_bias} Wstart_bias_p={Wstart_bias_p}
parameters Wstart_cmp_p={Wstart_cmp_p} Wstart_cmp_n={Wstart_cmp_n} Lstart_cmp={Lstart_cmp}
parameters Wstart_inv_p={Wstart_inv_p} Wstart_inv_n={Wstart_inv_n}
parameters Wmirror_p={Wmirror_p} Lmirror_p={Lmirror_p} MREF_RATIO={MREF_RATIO}
parameters BJT_AREA_RATIO={BJT_AREA_RATIO} Iopbias={Iopbias} Cload={Cload}
parameters R0_SEG_L={R0_SEG_L} R0_SEG_W={R0_SEG_W}
parameters R1_SEG_L={R1_SEG_L} R1_SEG_W={R1_SEG_W}

subckt bandgap_ptat (vref vdd vss)
// Frozen two-stage OTA. Port order: vip vin vout ibias vdd vss
IOPBIASsrc (vdd opibias) isource type=dc dc=Iopbias
Xopamp (vinp vinn vg opibias vdd vss) two_stage_ota

// Boni startup circuit III: IX > IY forces the core away from its zero-current
// state. SUP rises after VREF exceeds the rough VRS threshold, switching both
// PMOS injection devices off. MB is unnecessary because Iopbias is supplied
// independently from the raw supply.
MX (vinn sup vdd vdd) {pmos_model} l=Lstart w=Wstart_x nf=1 m=1
MY (vinp sup vdd vdd) {pmos_model} l=Lstart w=Wstart_y nf=1 m=1

// Raw-supply-derived mirror bias. It produces no startup current while VDD=0.
MSTART_BIAS (start_bias start_bias vdd vdd) {pmos_model} l=Lstart_cmp w=Wstart_bias_p nf=1 m=1
RSTART_BIAS (start_bias vss) resistor r=Rstart_bias

// Rough diode-derived reference: VRS = 0.9R/(R+0.9R) * VBE.
MSTART_VRS (vrs_diode start_bias vdd vdd) {pmos_model} l=Lstart_cmp w=Wstart_bias_p nf=1 m=1
QRS (vss vss vrs_diode) {pnp_model} m=1
RRS_TOP (vrs_diode vrs) resistor r=Rstart_ref
RRS_BOTTOM (vrs vss) resistor r=0.9*Rstart_ref

// Low-voltage VREF detector. When VREF < VRS, cmp_out is high and the
// following inverter pulls SUP low. Once VREF > VRS, SUP is driven to VDD.
MSTART_CMP (cmp_tail start_bias vdd vdd) {pmos_model} l=Lstart_cmp w=Wstart_bias_p nf=1 m=1
MCMP_RS (cmp_left vrs cmp_tail vdd) {pmos_model} l=Lstart_cmp w=Wstart_cmp_p nf=1 m=1
MCMP_REF (cmp_out vref cmp_tail vdd) {pmos_model} l=Lstart_cmp w=Wstart_cmp_p nf=1 m=1
MCMP_NL (cmp_left cmp_left vss vss) {nmos_model} l=Lstart_cmp w=Wstart_cmp_n nf=1 m=1
MCMP_NR (cmp_out cmp_left vss vss) {nmos_model} l=Lstart_cmp w=Wstart_cmp_n nf=1 m=1
MSUP_P (sup cmp_out vdd vdd) {pmos_model} l=Lstart_cmp w=Wstart_inv_p nf=1 m=1
MSUP_N (sup cmp_out vss vss) {nmos_model} l=Lstart_cmp w=Wstart_inv_n nf=1 m=1

// PMOS bandgap mirrors.
M12 (vref vg vdd vdd) {pmos_model} l=Lmirror_p w=Wmirror_p nf=3 m=MREF_RATIO
M11 (vinp vg vdd vdd) {pmos_model} l=Lmirror_p w=Wmirror_p nf=3 m=1
M10 (vinn vg vdd vdd) {pmos_model} l=Lmirror_p w=Wmirror_p nf=3 m=1

// PNP terminal order is (collector base emitter). Q1's emitter is vinn;
// Q0's emitter is net15 and uses the larger area BJT_AREA_RATIO.
Q1 (vss vss vinn) {pnp_model} m=1
Q0 (vss vss net15) {pnp_model} m=BJT_AREA_RATIO

// Four-section output resistor R1.
R1_1 (vref r1_1) {resistor_model} l=R1_SEG_L w=R1_SEG_W m=1 multi=(1)
R1_2 (r1_1 r1_2) {resistor_model} l=R1_SEG_L w=R1_SEG_W m=1 multi=(1)
R1_3 (r1_2 r1_3) {resistor_model} l=R1_SEG_L w=R1_SEG_W m=1 multi=(1)
R1_4 (r1_3 vss) {resistor_model} l=R1_SEG_L w=R1_SEG_W m=1 multi=(1)

// Two-section PTAT resistor R0.
R0_1 (vinp r0_1) {resistor_model} l=R0_SEG_L w=R0_SEG_W m=1 multi=(1)
R0_2 (r0_1 net15) {resistor_model} l=R0_SEG_L w=R0_SEG_W m=1 multi=(1)

CloadDev (vref vss) capacitor c=Cload
ends bandgap_ptat

// ---- Frozen child opamp macro ----
{opamp_netlist}
"""


_TB_STARTUP_TEMPLATE = """\
// tb_bandgap_ptat_startup.scs -- Power-on startup analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} CL={CL}

VDDsrc (vdd 0) vsource type=pulse val0=0 val1=VDD delay=0 rise=1u fall=1u width=20u period=40u
VSSsrc (vss 0) vsource type=dc dc=0

Xdut (vout vdd vss) bandgap_ptat
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
startupTran tran stop=10u maxstep=10n

save vdd vout
{STARTUP_INTERNAL_SAVES}
save VDDsrc:p
"""


_TB_PSRR_TEMPLATE = """\
// tb_bandgap_ptat_psrr.scs -- Supply-ripple rejection analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} CL={CL}

VDDsrc (vdd 0) vsource type=dc dc=VDD mag=1
VSSsrc (vss 0) vsource type=dc dc=0

Xdut (vout vdd vss) bandgap_ptat
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
op1 dc oppoint=rawfile
opInfo info what=oppoint where=rawfile
psrrAC ac start=1 stop=100M dec=20

save vout
save VDDsrc:p
"""


_TB_TEMPERATURE_TEMPLATE = """\
// tb_bandgap_ptat_temperature.scs -- Vref temperature nonlinearity analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} CL={CL}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0

Xdut (vout vdd vss) bandgap_ptat
CLload (vout 0) capacitor c=CL

outOpts options rawfmt=psfascii soft_bin=allmodels
tempSweep dc param=temp start={TEMP_MIN} stop={TEMP_MAX} step=1

save vout
save VDDsrc:p
"""


_TB_LINE_TEMPLATE = """\
// tb_bandgap_ptat_line.scs -- DC line-regulation analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VDD_MIN={VDD_MIN} VDD_MAX={VDD_MAX} VDD_STEP={VDD_STEP} CL={CL}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0

Xdut (vout vdd vss) bandgap_ptat
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
lineSweep dc param=VDD start=VDD_MIN stop=VDD_MAX step=VDD_STEP

save vout
save VDDsrc:p
"""
