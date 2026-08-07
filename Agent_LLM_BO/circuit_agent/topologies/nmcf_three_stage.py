"""NMCF three-stage OTA based on Leung and Mok Fig. 1(h).

Reference inspiration:
  /Users/hnchen/Desktop/LLM_Task/AnalogGym/AnalogGym/Amplifier/spectre_netlist/Leung_NMCF_Pin_3

Topology:

  Stage 1:
      PMOS differential pair with PMOS tail and NMOS mirror load.

  Stage 2:
      PMOS common-source gain device drives an NMOS current mirror.
      A PMOS current-source load converts the mirrored current to voltage.

  Output stage:
      NMOS common-source output device driven by stage 2.
      PMOS feedforward transconductance device driven by stage 1.
      Together they form the push-pull output stage used in Fig. 7(b).

  Compensation:
      Cc1 : stage1_out to vout
      Cc2 : stage2_out to vout

Bias simplification:
  The external ibias pin provides a reference current.  A compact internal
  MOS bias network derives the PMOS tail bias, the stage-2 PMOS load bias,
  and the shared NMOS bias used by the NMOS mirror/load devices.

Port order: vip vin vout ibias vdd vss
"""

from __future__ import annotations

from topologies.base import BaseTopology, PassiveImplementation, TopologyMeta
from models import CircuitFiles, ParamDef, ParamSpace, format_spice_value
from pdk_profiles import get_pdk_profile, get_pdk_profile_for_params, spectre_include_line


class NMCFThreeStageOTA(BaseTopology):
    PASSIVE_IMPLEMENTATIONS = (
        PassiveImplementation("Cc1", "capacitor", "compensation_capacitor"),
        PassiveImplementation("Cc2", "capacitor", "compensation_capacitor"),
    )
    """Three-stage NMCF OTA with a stage-1-to-output feedforward path.

    The PMOS input stage drives a PMOS/NMOS-mirror intermediate stage.  An
    NMOS output device and PMOS feedforward device form the push-pull output.
    This is the highest-gain and highest-complexity option in the current
    opamp library.
    """

    meta = TopologyMeta(
        name="nmcf_three_stage",
        display_name="NMCF Three-Stage OTA",
        description=(
            "Three-stage OTA with a PMOS input stage, PMOS/NMOS-mirror "
            "intermediate stage, push-pull NMCF output, and nested Miller "
            "compensation for high gain and heavy loads."
        ),
        min_gain_db=75,
        max_gain_db=115,
        min_gbw_hz=5e5,
        max_gbw_hz=6e8,
        typical_power_w=4e-3,
        complexity=4,
        escalation=None,
    )

    def critical_operating_point_instances(self) -> set[str]:
        return {
            "Mtail1",
            "Mdiff1a",
            "Mdiff1b",
            "Mload1a",
            "Mload1b",
            "Mgm2",
            "Mmirror2a",
            "Mmirror2b",
            "Msource2",
            "Mgm3",
            "Mgmf2",
        }

    DEFAULT_PARAMS: dict[str, float] = {
        # Stage 1: PMOS input pair + PMOS tail
        "Wtail1": 18e-6,
        "Ltail1": 200e-9,
        "Wdiff1": 10e-6,
        "Ldiff1": 80e-9,
        "Wload1": 10e-6,
        "Lload1": 100e-9,
        # Stage 2: PMOS gm2 + NMOS mirror + PMOS source load
        "Wgm2": 14e-6,
        "Lgm2": 80e-9,
        "Wmirror2": 10e-6,
        "Lmirror2": 200e-9,
        "Wsource2": 16e-6,
        "Lsource2": 200e-9,
        # Push-pull output: NMOS gmL + PMOS feedforward gmf2
        "Wgm3": 24e-6,
        "Lgm3": 100e-9,
        "Wgmf2": 24e-6,
        "Lgmf2": 100e-9,
        # Internal bias generator
        "Wbiasn": 4e-6,
        "Lbiasn": 200e-9,
        "Wbiasp": 8e-6,
        "Lbiasp": 200e-9,
        # Compensation
        "Cc1": 800e-15,
        "Cc2": 500e-15,
    }

    def required_model_roles(self) -> tuple[str, ...]:
        return ("nmos_lvt", "pmos_lvt")

    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        """Generate the DUT .cir subcircuit netlist."""
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)

        return _CIRCUIT_TEMPLATE.format(
            spectre_include=spectre_include_line(pdk),
            nmos_lvt_model=pdk.nmos_lvt_model,
            pmos_lvt_model=pdk.pmos_lvt_model,
            Wtail1=_fmt(p["Wtail1"]),
            Ltail1=_fmt(p["Ltail1"]),
            Wdiff1=_fmt(p["Wdiff1"]),
            Ldiff1=_fmt(p["Ldiff1"]),
            Wload1=_fmt(p["Wload1"]),
            Lload1=_fmt(p["Lload1"]),
            Wgm2=_fmt(p["Wgm2"]),
            Lgm2=_fmt(p["Lgm2"]),
            Wmirror2=_fmt(p["Wmirror2"]),
            Lmirror2=_fmt(p["Lmirror2"]),
            Wsource2=_fmt(p["Wsource2"]),
            Lsource2=_fmt(p["Lsource2"]),
            Wgm3=_fmt(p["Wgm3"]),
            Lgm3=_fmt(p["Lgm3"]),
            Wgmf2=_fmt(p["Wgmf2"]),
            Lgmf2=_fmt(p["Lgmf2"]),
            Wbiasn=_fmt(p["Wbiasn"]),
            Lbiasn=_fmt(p["Lbiasn"]),
            Wbiasp=_fmt(p["Wbiasp"]),
            Lbiasp=_fmt(p["Lbiasp"]),
            Cc1=_fmt(p["Cc1"]),
            Cc2=_fmt(p["Cc2"]),
        )

    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        """Generate the Spectre-native testbench .scs file."""
        pdk = get_pdk_profile_for_params(params)
        tb_defaults = self._testbench_defaults_with_preset(
            {
                "VCM": 0.3,
                "IBIAS": 40e-6,
                "CL": 10e-12,
            }
        )
        vdd = pdk.vdd
        vcm = tb_defaults["VCM"]
        ibias = tb_defaults["IBIAS"]
        cload = tb_defaults["CL"]

        if params:
            vdd = params.get("VDD", vdd)
            vcm = params.get("VCM", vcm)
            ibias = params.get("IBIAS", params.get("VBIAS", ibias))
            cload = params.get("CL", cload)

        if analysis_type in ("tran", "sr"):
            return _TB_SR_TEMPLATE.format(
                VDD=vdd,
                VCM=vcm,
                IBIAS=_fmt(ibias),
                CL=_fmt(cload),
                VHIGH=vcm + 0.15,
                VLOW=vcm - 0.15,
            )
        if analysis_type == "st":
            return _TB_ST_TEMPLATE.format(
                VDD=vdd, VCM=vcm, IBIAS=_fmt(ibias), CL=_fmt(cload),
                VHIGH=vcm + 10e-3, VLOW=vcm,
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
        tb_ac = self.generate_testbench(params, analysis_type="ac")
        tb_sr = self.generate_testbench(params, analysis_type="sr")
        tb_st = self.generate_testbench(params, analysis_type="st")
        circuit_name = CircuitFiles.extract_subckt_name(circuit_content)
        return CircuitFiles(
            circuit_netlist=circuit_content,
            testbenches=[tb_ac, tb_sr, tb_st],
            circuit_name=circuit_name,
        )

    # ------------------------------------------------------------------
    # gm/Id support
    # ------------------------------------------------------------------

    def get_gmid_spec(self, targets=None):
        """Return the gm/Id sizing contract for the NMCF signal path."""
        from models import BranchCurrentSpec, GmidTopologySpec, TransistorSpec

        pdk = get_pdk_profile()
        pass_through_space = self._apply_param_space_overrides(ParamSpace(params=[
            ParamDef(
                name="Cc1", low=0.05e-12, high=10e-12,
                log_scale=True, unit="F",
            ),
            ParamDef(
                name="Cc2", low=0.05e-12, high=10e-12,
                log_scale=True, unit="F",
            ),
        ]))

        return GmidTopologySpec(
            branch_currents=[
                BranchCurrentSpec(
                    name="I_tail1", low=1e-6, high=200e-6, default=25e-6,
                ),
                BranchCurrentSpec(
                    name="I_s2", low=1e-6, high=300e-6, default=30e-6,
                ),
                BranchCurrentSpec(
                    name="I_s3", low=1e-6, high=500e-6, default=50e-6,
                ),
            ],
            transistors=[
                # -- Stage 1: PMOS tail current source (gate=vbiasp) --
                TransistorSpec(
                    role="stage1_tail_pmos",
                    w_param="Wtail1", l_param="Ltail1",
                    model=pdk.pmos_lvt_model,
                    current_source="I_tail1", current_fraction=1.0,
                    gm_id_low=5, gm_id_high=20, gm_id_default=8,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.2,
                ),
                # -- Stage 1: PMOS diff pair (each I_tail1/2) --
                TransistorSpec(
                    role="stage1_diff_pmos",
                    w_param="Wdiff1", l_param="Ldiff1",
                    model=pdk.pmos_lvt_model,
                    current_source="I_tail1", current_fraction=0.5,
                    gm_id_low=10, gm_id_high=24, gm_id_default=14,
                    L_low=60e-9, L_high=500e-9, L_default=80e-9,
                    Vds_estimate=0.25, Vbs=-0.2, multiplicity=2,
                ),
                # -- Stage 1: NMOS current mirror load (each I_tail1/2) --
                TransistorSpec(
                    role="stage1_load_nmos",
                    w_param="Wload1", l_param="Lload1",
                    model=pdk.nmos_lvt_model,
                    current_source="I_tail1", current_fraction=0.5,
                    gm_id_low=8, gm_id_high=24, gm_id_default=12,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.3, multiplicity=2,
                ),
                # -- Stage 2: PMOS common-source gain device --
                TransistorSpec(
                    role="stage2_gain_pmos",
                    w_param="Wgm2", l_param="Lgm2",
                    model=pdk.pmos_lvt_model,
                    current_source="I_s2", current_fraction=1.0,
                    gm_id_low=10, gm_id_high=24, gm_id_default=15,
                    L_low=60e-9, L_high=500e-9, L_default=80e-9,
                    Vds_estimate=0.3,
                ),
                # -- Stage 2: NMOS current mirror --
                TransistorSpec(
                    role="stage2_mirror_nmos",
                    w_param="Wmirror2", l_param="Lmirror2",
                    model=pdk.nmos_lvt_model,
                    current_source="I_s2", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=20, gm_id_default=12,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.3, multiplicity=2,
                ),
                # -- Stage 2: PMOS current-source load at s2_out --
                TransistorSpec(
                    role="stage2_source_pmos",
                    w_param="Wsource2", l_param="Lsource2",
                    model=pdk.pmos_lvt_model,
                    current_source="I_s2", current_fraction=1.0,
                    gm_id_low=5, gm_id_high=20, gm_id_default=8,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.4,
                ),
                # -- Output stage: NMOS gmL driven by s2_out --
                TransistorSpec(
                    role="stage3_gain_nmos",
                    w_param="Wgm3", l_param="Lgm3",
                    model=pdk.nmos_lvt_model,
                    current_source="I_s3", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=22, gm_id_default=12,
                    L_low=60e-9, L_high=300e-9, L_default=100e-9,
                    Vds_estimate=0.45,
                ),
                # -- Feedforward PMOS gmf2 driven directly by s1_out --
                TransistorSpec(
                    role="feedforward_gain_pmos",
                    w_param="Wgmf2", l_param="Lgmf2",
                    model=pdk.pmos_lvt_model,
                    current_source="I_s3", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=22, gm_id_default=12,
                    L_low=60e-9, L_high=300e-9, L_default=100e-9,
                    Vds_estimate=0.45,
                ),
            ],
            pass_through_params=pass_through_space.params,
        )

    def get_default_params(self) -> dict[str, float]:
        return self._default_params_with_preset()

    def get_param_space(self) -> ParamSpace:
        return self._apply_param_space_overrides(ParamSpace(
            params=[
                ParamDef(
                    name="Wtail1", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Ltail1", low=200e-9, high=600e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wdiff1", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Ldiff1", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wload1", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lload1", low=200e-9, high=600e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wgm2", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lgm2", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wmirror2", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lmirror2", low=200e-9, high=600e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wsource2", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lsource2", low=200e-9, high=600e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wgm3", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lgm3", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wgmf2", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lgmf2", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wbiasn", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lbiasn", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wbiasp", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lbiasp", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Cc1", low=0.05e-12, high=10e-12,
                    log_scale=True, unit="F",
                ),
                ParamDef(
                    name="Cc2", low=0.05e-12, high=10e-12,
                    log_scale=True, unit="F",
                ),
            ]
        ))


_CIRCUIT_TEMPLATE = """\
// nmcf_three_stage.cir -- NMCF Three-Stage OTA (Spectre native syntax)
simulator lang=spectre insensitive=yes

{spectre_include}

parameters Wtail1={Wtail1} Ltail1={Ltail1} Wdiff1={Wdiff1} Ldiff1={Ldiff1}
parameters Wload1={Wload1} Lload1={Lload1} Wgm2={Wgm2} Lgm2={Lgm2}
parameters Wmirror2={Wmirror2} Lmirror2={Lmirror2} Wsource2={Wsource2} Lsource2={Lsource2}
parameters Wgm3={Wgm3} Lgm3={Lgm3} Wgmf2={Wgmf2} Lgmf2={Lgmf2}
parameters Wbiasn={Wbiasn} Lbiasn={Lbiasn} Wbiasp={Wbiasp} Lbiasp={Lbiasp}
parameters Cc1={Cc1} Cc2={Cc2}

subckt nmcf_three_stage (vip vin vout ibias vdd vss)
// Bias generator
Mbn1 (ibias ibias vss vss) {nmos_lvt_model} w=Wbiasn l=Lbiasn nf=1
Mbn2 (vbiasp ibias vss vss) {nmos_lvt_model} w=Wbiasn l=Lbiasn nf=1
Mbp1 (vbiasp vbiasp vdd vdd) {pmos_lvt_model} w=Wbiasp l=Lbiasp nf=1

// Stage 1: PMOS input differential pair and NMOS mirror load
Mtail1 (tail vbiasp vdd vdd) {pmos_lvt_model} w=Wtail1 l=Ltail1 nf=1
Mdiff1a (s1_mirr vin tail vdd) {pmos_lvt_model} w=Wdiff1 l=Ldiff1 nf=1
Mdiff1b (s1_out vip tail vdd) {pmos_lvt_model} w=Wdiff1 l=Ldiff1 nf=1
Mload1a (s1_mirr s1_mirr vss vss) {nmos_lvt_model} w=Wload1 l=Lload1 nf=1
Mload1b (s1_out s1_mirr vss vss) {nmos_lvt_model} w=Wload1 l=Lload1 nf=1

// Stage 2: PMOS gm2, NMOS current mirror, and PMOS source load
Mgm2 (s2_mirr s1_out vdd vdd) {pmos_lvt_model} w=Wgm2 l=Lgm2 nf=1
Mmirror2a (s2_mirr s2_mirr vss vss) {nmos_lvt_model} w=Wmirror2 l=Lmirror2 nf=1
Mmirror2b (s2_out s2_mirr vss vss) {nmos_lvt_model} w=Wmirror2 l=Lmirror2 nf=1
Msource2 (s2_out vbiasp vdd vdd) {pmos_lvt_model} w=Wsource2 l=Lsource2 nf=1

// Push-pull output: serial gmL path plus stage-1 feedforward gmf2 path
Mgm3 (vout s2_out vss vss) {nmos_lvt_model} w=Wgm3 l=Lgm3 nf=1
Mgmf2 (vout s1_out vdd vdd) {pmos_lvt_model} w=Wgmf2 l=Lgmf2 nf=1

// NMCF compensation from Fig. 1(h)
Cc1 (s1_out vout) capacitor c=Cc1
Cc2 (s2_out vout) capacitor c=Cc2
ends nmcf_three_stage
"""

_TB_AC_TEMPLATE = """\
// tb_nmcf_three_stage_ac.scs -- NMCF Three-Stage OTA differential AC analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} IBIAS={IBIAS} CL={CL}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (vdd ibias) isource type=dc dc=IBIAS
VCMsrc (vcm 0) vsource type=dc dc=VCM
VIPsrc (vinp vcm) vsource type=dc dc=0 mag=1
Rfb (vout vinn) resistor r=1G
Cfb (vinn 0) capacitor c=1

Xdut (vinp vinn vout ibias vdd vss) nmcf_three_stage
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
op1 dc oppoint=rawfile
opInfo info what=oppoint where=rawfile
ac1 ac start=1 stop=20G dec=20

save vout
save VDDsrc:p
"""

_TB_SR_TEMPLATE = """\
// tb_nmcf_three_stage_sr.scs -- Unity-gain large-signal slew-rate analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} IBIAS={IBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (vdd ibias) isource type=dc dc=IBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=2n rise=100p fall=100p width=50n period=100n
VFBsrc (vin vout) vsource type=dc dc=0

Xdut (vinp vin vout ibias vdd vss) nmcf_three_stage
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
srTran tran stop=120n maxstep=10p

save vinp vout
"""

_TB_ST_TEMPLATE = """\
// tb_nmcf_three_stage_st.scs -- Unity-gain 0.1% settling-time analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} IBIAS={IBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (vdd ibias) isource type=dc dc=IBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=5n rise=100p fall=100p width=50n period=100n
VFBsrc (vin vout) vsource type=dc dc=0

Xdut (vinp vin vout ibias vdd vss) nmcf_three_stage
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
stTran tran stop=120n maxstep=10p

save vinp vout
"""


def _fmt(value: float) -> str:
    """Format a float with SPICE engineering suffix (u, n, p, f, k)."""
    return format_spice_value(value)
