"""Two-stage Miller OTA with PMOS-input 5T first stage and NMOS CS second stage.

Topology:
    First stage:
        Mtail  - PMOS tail current source
        Mdiff1/Mdiff2 - PMOS differential pair
        Mmirr1/Mmirr2 - NMOS current-mirror load

    Bias:
        Mbias - PMOS diode driven by an external ideal bias current

    Second stage:
        Mcs    - NMOS common-source amplifier
        Mload  - PMOS current-source load

    Compensation:
        Rz + Cc - Miller compensation from first-stage output to vout

Port order: vip vin vout ibias vdd vss
"""

from __future__ import annotations

import math

from models import CircuitFiles, ParamDef, ParamSpace, format_spice_value
from pdk_profiles import get_pdk_profile, get_pdk_profile_for_params, spectre_include_line
from topologies.base import BaseTopology, TopologyMeta


class PMOSInputTwoStageOTA(BaseTopology):
    """PMOS-input 5T OTA followed by an NMOS common-source second stage."""

    meta = TopologyMeta(
        name="pmos_input_two_stage_ota",
        display_name="PMOS-Input Two-Stage Miller OTA",
        description=(
            "Two-stage OTA with PMOS differential input pair, NMOS current-mirror "
            "first-stage load, NMOS common-source second stage, PMOS load, and "
            "Miller compensation. Uses LVT MOS models."
        ),
        min_gain_db=45,
        max_gain_db=80,
        min_gbw_hz=10e6,
        max_gbw_hz=5e8,
        typical_power_w=1e-3,
        complexity=2,
        escalation="folded_cascode",
    )

    DEFAULT_PARAMS: dict[str, float] = {
        "Wbias": 5e-6,
        "Lbias": 200e-9,
        "m_tail_unit": 2,
        "ratio_load_tail": 2,
        "Wdiff": 10e-6,
        "Ldiff": 120e-9,
        "Wmirr": 6e-6,
        "Lmirr": 200e-9,
        "Wcs": 20e-6,
        "Cc": 500e-15,
        "Rz": 1000.0,
        "IBIAS": 20e-6,
    }

    def required_model_roles(self) -> tuple[str, ...]:
        return ("nmos_lvt", "pmos_lvt")

    def critical_operating_point_instances(self) -> set[str]:
        return {
            "Mdiff1",
            "Mdiff2",
            "Mmirr1",
            "Mmirr2",
            "Mtail",
            "Mcs",
            "Mload",
            "Mbias",
        }

    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        """Generate the DUT .cir subcircuit netlist."""
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)

        return _CIRCUIT_TEMPLATE.format(
            spectre_include=spectre_include_line(pdk),
            nmos_lvt_model=pdk.nmos_lvt_model,
            pmos_lvt_model=pdk.pmos_lvt_model,
            Wbias=_fmt(p["Wbias"]),
            Lbias=_fmt(p["Lbias"]),
            m_tail_unit=int(p["m_tail_unit"]),
            ratio_load_tail=int(p["ratio_load_tail"]),
            Wdiff=_fmt(p["Wdiff"]),
            Ldiff=_fmt(p["Ldiff"]),
            Wmirr=_fmt(p["Wmirr"]),
            Lmirr=_fmt(p["Lmirr"]),
            Wcs=_fmt(p["Wcs"]),
            Cc=_fmt(p["Cc"]),
            Rz=_fmt(p["Rz"]),
        )

    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        """Generate AC, slew-rate, or settling-time Spectre testbench."""
        pdk = get_pdk_profile_for_params(params)
        p = self._merge_params_with_preset(params)
        tb_defaults = self._testbench_defaults_with_preset(
            {
                "VCM": pdk.vdd - 0.35,
                "IBIAS": p.get("IBIAS", 20e-6),
                "CL": 2e-12,
            }
        )
        vdd = pdk.vdd
        vcm = tb_defaults["VCM"]
        ibias = tb_defaults["IBIAS"]
        cload = tb_defaults["CL"]

        if params:
            vdd = params.get("VDD", vdd)
            vcm = params.get("VCM", vcm)
            ibias = params.get("IBIAS", ibias)
            cload = params.get("CL", cload)

        if analysis_type in ("tran", "sr"):
            return _TB_SR_TEMPLATE.format(
                VDD=vdd,
                VCM=vcm,
                IBIAS=_fmt(ibias),
                CL=_fmt(cload),
                VLOW=vcm - 0.2,
                VHIGH=vcm + 0.2,
            )
        if analysis_type == "st":
            return _TB_ST_TEMPLATE.format(
                VDD=vdd,
                VCM=vcm,
                IBIAS=_fmt(ibias),
                CL=_fmt(cload),
                VLOW=vcm,
                VHIGH=vcm + 10e-3,
            )
        return _TB_AC_TEMPLATE.format(
            VDD=vdd,
            VCM=vcm,
            IBIAS=_fmt(ibias),
            CL=_fmt(cload),
        )

    def get_circuit_files(
        self, params: dict[str, float] | None = None
    ) -> CircuitFiles:
        """Return AC, slew-rate, and 0.1% settling-time testbenches."""
        circuit_content = self.generate_circuit(params)
        return CircuitFiles(
            circuit_netlist=circuit_content,
            testbenches=[
                self.generate_testbench(params, "ac"),
                self.generate_testbench(params, "sr"),
                self.generate_testbench(params, "st"),
            ],
            circuit_name=CircuitFiles.extract_subckt_name(circuit_content),
        )

    def get_gmid_spec(self, targets=None):
        """Return gm/Id spec for PMOS-input two-stage OTA.

        The first-stage PMOS tail and second-stage PMOS load share the
        voltage generated by pulling ``IBIAS`` through a diode-connected PMOS.
        """
        from models import DerivedBranchCurrentSpec, GmidTopologySpec, TransistorSpec

        pdk = get_pdk_profile()
        unit_current = 20e-6
        pass_through_space = self._apply_param_space_overrides(ParamSpace(params=[
            ParamDef(
                name="Cc", low=0.1e-12, high=5e-12,
                log_scale=True, unit="F",
            ),
            ParamDef(
                name="Rz", low=100, high=5e3,
                log_scale=True, unit="Ohm",
            ),
        ]))

        tail_current_low = 50e-6
        if (
            targets is not None
            and targets.bandwidth_hz is not None
            and targets.load_cap_f is not None
            and targets.bandwidth_hz > 0
            and targets.load_cap_f > 0
        ):
            compensation_estimate = 0.5 * targets.load_cap_f
            gm_required = (
                2.0 * math.pi * targets.bandwidth_hz * compensation_estimate
            )
            single_input_current = gm_required / 18.0
            tail_current_low = max(tail_current_low, 2.0 * single_input_current)
        tail_multiplier_min = max(1, math.ceil(tail_current_low / unit_current))
        tail_multiplier_high = max(8, 4 * tail_multiplier_min)

        return GmidTopologySpec(
            derived_branch_currents=[
                DerivedBranchCurrentSpec(
                    name="IBIAS",
                    unit_current=unit_current,
                    multiplier_offset=1.0,
                ),
                DerivedBranchCurrentSpec(
                    name="I_tail",
                    unit_current=unit_current,
                    multiplier_param="m_tail_unit",
                    multiplier_scale=1.0,
                ),
                DerivedBranchCurrentSpec(
                    name="I_cs",
                    unit_current=unit_current,
                    multiplier_param="m_tail_unit",
                    multiplier_scale=1.0,
                    extra_param="ratio_load_tail",
                    extra_mode="multiply",
                ),
            ],
            transistors=[
                TransistorSpec(
                    role="bias_pmos",
                    w_param="Wbias", l_param="Lbias",
                    model=pdk.pmos_lvt_model,
                    current_source="IBIAS", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=18, gm_id_default=12,
                    L_low=120e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.2,
                ),
                TransistorSpec(
                    role="diff_pair_pmos",
                    w_param="Wdiff", l_param="Ldiff",
                    model=pdk.pmos_lvt_model,
                    current_source="I_tail", current_fraction=0.5,
                    gm_id_low=10, gm_id_high=20, gm_id_default=16,
                    L_low=120e-9, L_high=600e-9, L_default=180e-9,
                    Vds_estimate=0.25, Vbs=-0.2, multiplicity=2,
                ),
                TransistorSpec(
                    role="mirror_nmos",
                    w_param="Wmirr", l_param="Lmirr",
                    model=pdk.nmos_lvt_model,
                    current_source="I_tail", current_fraction=0.5,
                    gm_id_low=5, gm_id_high=15, gm_id_default=12,
                    L_low=120e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.35, multiplicity=2,
                ),
                TransistorSpec(
                    role="cs_nmos",
                    w_param="Wcs", l_param="Lbias",
                    model=pdk.nmos_lvt_model,
                    current_source="I_cs", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=18, gm_id_default=12,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.45,
                ),
            ],
            pass_through_params=[
                ParamDef(
                    name="m_tail_unit", low=tail_multiplier_min, high=tail_multiplier_high,
                    log_scale=False, unit="x", value_type="int",
                ),
                ParamDef(
                    name="ratio_load_tail", low=1, high=4,
                    log_scale=False, unit="x", value_type="int",
                ),
                *pass_through_space.params,
            ],
        )

    def get_default_params(self) -> dict[str, float]:
        return self._default_params_with_preset()

    def get_param_space(self) -> ParamSpace:
        return self._apply_param_space_overrides(ParamSpace(
            params=[
                ParamDef(
                    name="Wbias", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lbias", low=120e-9, high=600e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="m_tail_unit", low=1, high=16,
                    log_scale=False, unit="x", value_type="int",
                ),
                ParamDef(
                    name="ratio_load_tail", low=1, high=4,
                    log_scale=False, unit="x", value_type="int",
                ),
                ParamDef(
                    name="Wdiff", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Ldiff", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wmirr", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lmirr", low=120e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wcs", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Cc", low=0.1e-12, high=5e-12,
                    log_scale=True, unit="F",
                ),
                ParamDef(
                    name="Rz", low=100, high=5e3,
                    log_scale=True, unit="Ohm",
                ),
            ]
        ))


_CIRCUIT_TEMPLATE = """\
// pmos_input_two_stage_ota.cir -- PMOS-input two-stage Miller OTA
simulator lang=spectre insensitive=yes

{spectre_include}

parameters Wbias={Wbias} Lbias={Lbias} m_tail_unit={m_tail_unit} ratio_load_tail={ratio_load_tail}
parameters m_load_unit=m_tail_unit*ratio_load_tail
parameters Wdiff={Wdiff} Ldiff={Ldiff}
parameters Wmirr={Wmirr} Lmirr={Lmirr} Wcs={Wcs}
parameters Cc={Cc} Rz={Rz}

subckt pmos_input_two_stage_ota (vip vin vout ibias vdd vss)
// Bias generator: external current pulled from diode-connected PMOS
Mbias (ibias ibias vdd vdd) {pmos_lvt_model} w=Wbias l=Lbias nf=1
// First stage: PMOS differential pair with NMOS current-mirror load
Mdiff1 (n_mirr vip n_tail vdd) {pmos_lvt_model} w=Wdiff l=Ldiff nf=1
Mdiff2 (n_s1 vin n_tail vdd) {pmos_lvt_model} w=Wdiff l=Ldiff nf=1
Mmirr1 (n_mirr n_mirr vss vss) {nmos_lvt_model} w=Wmirr l=Lmirr nf=1
Mmirr2 (n_s1 n_mirr vss vss) {nmos_lvt_model} w=Wmirr l=Lmirr nf=1
Mtail (n_tail ibias vdd vdd) {pmos_lvt_model} w=Wbias l=Lbias nf=1 m=m_tail_unit
// Second stage: NMOS common-source amplifier with PMOS current-source load
Mcs (vout n_s1 vss vss) {nmos_lvt_model} w=Wcs l=Lbias nf=1
Mload (vout ibias vdd vdd) {pmos_lvt_model} w=Wbias l=Lbias nf=1 m=m_load_unit
// Miller compensation
Rz (n_s1 n_rz) resistor r=Rz
Cc (n_rz vout) capacitor c=Cc
ends pmos_input_two_stage_ota
"""

_TB_AC_TEMPLATE = """\
// tb_pmos_input_two_stage_ota_ac.scs -- Differential AC analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} IBIAS={IBIAS} CL={CL}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (ibias vss) isource type=dc dc=IBIAS
VCMsrc (vcm 0) vsource type=dc dc=VCM
VIPsrc (vinp vcm) vsource type=dc dc=0 mag=1
Rfb (vout vinn) resistor r=1G
Cfb (vinn 0) capacitor c=1

Xdut (vinp vinn vout ibias vdd vss) pmos_input_two_stage_ota
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
op1 dc oppoint=rawfile
opInfo info what=oppoint where=rawfile
ac1 ac start=1 stop=10G dec=20

save vout
save VDDsrc:p
"""

_TB_SR_TEMPLATE = """\
// tb_pmos_input_two_stage_ota_sr.scs -- Unity-gain slew-rate analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} IBIAS={IBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (ibias vss) isource type=dc dc=IBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=2n rise=100p fall=100p width=50n period=100n
VFBsrc (vin vout) vsource type=dc dc=0

Xdut (vinp vin vout ibias vdd vss) pmos_input_two_stage_ota
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
srTran tran stop=200n maxstep=10p

save vinp vout
"""

_TB_ST_TEMPLATE = """\
// tb_pmos_input_two_stage_ota_st.scs -- Unity-gain settling-time analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} IBIAS={IBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (ibias vss) isource type=dc dc=IBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=5n rise=100p fall=100p width=80n period=160n
VFBsrc (vin vout) vsource type=dc dc=0

Xdut (vinp vin vout ibias vdd vss) pmos_input_two_stage_ota
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
stTran tran stop=180n maxstep=10p

save vinp vout
"""


def _fmt(value: float) -> str:
    return format_spice_value(value)
