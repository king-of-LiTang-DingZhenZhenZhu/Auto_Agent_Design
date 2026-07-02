"""Folded-Cascode Miller OTA -- PMOS-input folded cascode + PMOS second stage.

Reference: /Desktop/Knowleage_Base/01-电路拓扑/两级密勒补偿运放.md

Topology:

  First Stage (PMOS-input folded cascode):
      Mtailp     -- PMOS tail current source
      Mdiff1/2   -- PMOS differential pair
      Mfold1/2   -- NMOS folded-branch current sources
      Mcasn1/2   -- NMOS common-gate cascode devices
      Mmirr1/2   -- PMOS current-mirror devices
      Mcasp1/2   -- PMOS cascode devices

  Second Stage:
      Mcs        -- PMOS common-source amplifier (gate <- stage1_out)
      Mload      -- NMOS current-source load

  Compensation:
      Cc         -- Miller capacitor between stage1_out and vout
      Rz         -- Nulling resistor in series with Cc

Bias simplification:
  The external ibias pin accepts a reference current.  The subcircuit contains
  a compact MOS bias generator that creates the PMOS tail bias, PMOS cascode
  bias, NMOS cascode bias, and the shared NMOS current-source gate bias for the
  folded branches and second-stage load.

Port order: vip vin vout ibias vdd vss
"""

from __future__ import annotations

from topologies.base import BaseTopology, TopologyMeta
from models import CircuitFiles, ParamDef, ParamSpace
from pdk_profiles import get_pdk_profile, spectre_include_line


_FIXED_BIAS_PARAM_NAMES = {
    "Wbp_big", "nf_Wbp_big", "m_Wbp_big",
    "Wbp_small", "nf_Wbp_small", "m_Wbp_small",
    "Wbn_big", "nf_Wbn_big", "m_Wbn_big",
    "Wbn_small", "nf_Wbn_small", "m_Wbn_small",
}


def _bias_w(name: str) -> ParamDef:
    return ParamDef(
        name=name,
        low=1e-6,
        high=20e-6,
        log_scale=True,
        unit="m",
        max_per_finger=2.6e-6,
    )


def _bias_l(name: str) -> ParamDef:
    return ParamDef(
        name=name,
        low=300e-9,
        high=600e-9,
        log_scale=True,
        unit="m",
    )


class FoldedCascodeOTA(BaseTopology):
    """Folded-cascode two-stage OTA.

    PMOS input pair -> folded NMOS cascodes -> PMOS cascode mirror ->
    PMOS common-source second stage.  Compared with the 5T first stage used by
    TwoStageOTA, the folded-cascode first stage has higher output resistance
    and is intended for high-gain, high-GBW targets.
    """

    meta = TopologyMeta(
        name="folded_cascode",
        display_name="Folded-Cascode OTA",
        description=(
            "Two-stage OTA with a PMOS-input folded-cascode first stage, "
            "PMOS common-source second stage, and Miller compensation. "
            "High gain (60-85 dB), higher GBW than a basic two-stage OTA."
        ),
        min_gain_db=50,
        max_gain_db=85,
        min_gbw_hz=10e6,
        max_gbw_hz=1e9,
        typical_power_w=3e-3,
        complexity=3,
        escalation="nmcf_three_stage",
    )

    DEFAULT_PARAMS: dict[str, float] = {
        # PMOS input differential pair
        "Wdiffp": 12e-6,
        "Ldiffp": 80e-9,
        # Second-stage PMOS common-source amplifier
        "Wcs": 30e-6,
        # Bias-ratio current mirrors
        "m_half_unit": 2,
        "m_load_ratio": 2,
        # Internal reference-bias generator
        "Lbias": 400e-9,
        "Wbp_big": 4.8e-6,
        "nf_Wbp_big": 4,
        "m_Wbp_big": 1,
        "Wbp_small": 1.2e-6,
        "nf_Wbp_small": 1,
        "m_Wbp_small": 1,
        "Wbn_big": 4.8e-6,
        "nf_Wbn_big": 4,
        "m_Wbn_big": 1,
        "Wbn_small": 1.2e-6,
        "nf_Wbn_small": 1,
        "m_Wbn_small": 1,
        # Compensation
        "Cc": 250e-15,
        "Rz": 1000.0,
    }

    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        """Generate the DUT .cir subcircuit netlist."""
        p = dict(self.DEFAULT_PARAMS)
        if params:
            p.update(params)
        pdk = get_pdk_profile()
        p["m_tail_unit"] = 2 * int(round(p["m_half_unit"]))
        p["m_load_unit"] = (
            int(round(p["m_half_unit"])) * int(round(p["m_load_ratio"]))
        )

        return _CIRCUIT_TEMPLATE.format(
            spectre_include=spectre_include_line(pdk),
            pmos_lvt_model=pdk.pmos_lvt_model,
            nmos_lvt_model=pdk.nmos_lvt_model,
            Wdiffp=_fmt(p["Wdiffp"]),
            Ldiffp=_fmt(p["Ldiffp"]),
            Wcs=_fmt(p["Wcs"]),
            Lbias=_fmt(p["Lbias"]),
            nf_Wbp_big=int(round(p.get("nf_Wbp_big", 1))),
            m_Wbp_big=int(round(p.get("m_Wbp_big", 1))),
            nf_Wbp_small=int(round(p.get("nf_Wbp_small", 1))),
            m_Wbp_small=int(round(p.get("m_Wbp_small", 1))),
            nf_Wbn_big=int(round(p.get("nf_Wbn_big", 1))),
            m_Wbn_big=int(round(p.get("m_Wbn_big", 1))),
            nf_Wbn_small=int(round(p.get("nf_Wbn_small", 1))),
            m_Wbn_small=int(round(p.get("m_Wbn_small", 1))),
            m_half_unit=int(round(p["m_half_unit"])),
            m_load_ratio=int(round(p["m_load_ratio"])),
            Wbp_big=_fmt(p["Wbp_big"]),
            Wbp_small=_fmt(p["Wbp_small"]),
            Wbn_big=_fmt(p["Wbn_big"]),
            Wbn_small=_fmt(p["Wbn_small"]),
            Cc=_fmt(p["Cc"]),
            Rz=_fmt(p["Rz"]),
        )

    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        """Generate the Spectre-native testbench .scs file."""
        pdk = get_pdk_profile()
        vdd = pdk.vdd
        vcm = 0.4
        ibias = 20e-6
        cload = 1e-12

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
                VHIGH=vcm + 0.2,
                VLOW=vcm - 0.2,
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

    def get_default_params(self) -> dict[str, float]:
        params = dict(self.DEFAULT_PARAMS)
        for name in _FIXED_BIAS_PARAM_NAMES:
            params.pop(name, None)
        return params

    def get_param_space(self) -> ParamSpace:
        return ParamSpace(
            params=[
                ParamDef(
                    name="Wdiffp", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Ldiffp", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wcs", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lbias", low=300e-9, high=600e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="m_half_unit", low=2, high=6,
                    log_scale=False, unit="x", value_type="int",
                ),
                ParamDef(
                    name="m_load_ratio", low=2, high=8,
                    log_scale=False, unit="x", value_type="int",
                ),
                ParamDef(
                    name="Cc", low=0.1e-12, high=5e-12,
                    log_scale=True, unit="F",
                ),
                ParamDef(
                    name="Rz", low=1.0, high=100e3,
                    log_scale=True, unit="Ohm",
                ),
            ]
        )

    def get_gmid_spec(self, targets=None):
        """Return gm/Id spec for folded cascode — reduces 26 → 17 params."""
        from models import DerivedBranchCurrentSpec, GmidTopologySpec, TransistorSpec
        pdk = get_pdk_profile()

        return GmidTopologySpec(
            derived_branch_currents=[
                DerivedBranchCurrentSpec(
                    name="I_tail",
                    unit_current=20e-6,
                    multiplier_param="m_half_unit",
                    multiplier_scale=2.0,
                ),
                DerivedBranchCurrentSpec(
                    name="I_fold",
                    unit_current=20e-6,
                    multiplier_param="m_half_unit",
                    multiplier_scale=2.0,
                ),
                DerivedBranchCurrentSpec(
                    name="I_cs",
                    unit_current=20e-6,
                    multiplier_param="m_half_unit",
                    multiplier_scale=1.0,
                    extra_param="m_load_ratio",
                    extra_mode="multiply",
                ),
            ],
            transistors=[
                # -- PMOS diff pair (each side carries I_tail / 2) --
                TransistorSpec(
                    role="diff_pair_pmos",
                    w_param="Wdiffp", l_param="Ldiffp",
                    model=pdk.pmos_lvt_model,
                    current_source="I_tail", current_fraction=0.5,
                    gm_id_low=10, gm_id_high=15, gm_id_default=12,
                    L_low=60e-9, L_high=500e-9, L_default=80e-9,
                    Vds_estimate=0.25, Vbs=-0.2, multiplicity=2,
                ),
                # -- Second-stage PMOS common-source amplifier --
                TransistorSpec(
                    role="cs_pmos",
                    w_param="Wcs", l_param="Lbias",
                    model=pdk.pmos_lvt_model,
                    current_source="I_cs", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=15, gm_id_default=12,
                    L_low=300e-9, L_high=600e-9, L_default=400e-9,
                    Vds_estimate=0.6,
                ),
            ],
            pass_through_params=[
                ParamDef(
                    name="m_half_unit", low=2, high=6,
                    log_scale=False, unit="x", value_type="int",
                ),
                ParamDef(
                    name="m_load_ratio", low=2, high=8,
                    log_scale=False, unit="x", value_type="int",
                ),
                ParamDef(
                    name="Lbias", low=300e-9, high=600e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Cc", low=0.1e-12, high=5e-12,
                    log_scale=True, unit="F",
                ),
                ParamDef(
                    name="Rz", low=100, high=10e3,
                    log_scale=True, unit="Ohm",
                ),
            ],
            fixed_params={
                name: self.DEFAULT_PARAMS[name]
                for name in (
                    "Wbp_big",
                    "nf_Wbp_big", "m_Wbp_big",
                    "Wbp_small",
                    "nf_Wbp_small", "m_Wbp_small",
                    "Wbn_big",
                    "nf_Wbn_big", "m_Wbn_big",
                    "Wbn_small",
                    "nf_Wbn_small", "m_Wbn_small",
                )
            },
            fixed_width_scale_param="Lbias",
            fixed_width_scale_reference=400e-9,
        )


_CIRCUIT_TEMPLATE = """\
// folded_cascode.cir -- Folded-Cascode Miller OTA (Spectre native syntax)
simulator lang=spectre insensitive=yes

{spectre_include}

parameters Wdiffp={Wdiffp} Ldiffp={Ldiffp} Wcs={Wcs}
parameters Lbias={Lbias} Lbias_ref=400n

parameters nf_Wbp_big={nf_Wbp_big} m_Wbp_big={m_Wbp_big} nf_Wbp_small={nf_Wbp_small} m_Wbp_small={m_Wbp_small}
parameters nf_Wbn_big={nf_Wbn_big} m_Wbn_big={m_Wbn_big} nf_Wbn_small={nf_Wbn_small} m_Wbn_small={m_Wbn_small}
parameters Wbp_big={Wbp_big}*Lbias/Lbias_ref Wbp_small={Wbp_small}*Lbias/Lbias_ref
parameters Wbn_big={Wbn_big}*Lbias/Lbias_ref Wbn_small={Wbn_small}*Lbias/Lbias_ref
parameters m_half_unit={m_half_unit} m_load_ratio={m_load_ratio}
parameters m_tail_unit=2*m_half_unit m_load_unit=m_half_unit*m_load_ratio
parameters Cc={Cc} Rz={Rz}

subckt folded_cascode (vip vin vout ibias vdd vss)
// Internal bias generator
M2 (ibias ibias vdd vdd) {pmos_lvt_model} l=Lbias w=Wbp_big m=m_Wbp_big nf=nf_Wbp_big 

M1 (VB4 ibias vdd vdd) {pmos_lvt_model} l=Lbias w=Wbp_big m=m_Wbp_big nf=nf_Wbp_big 

M0 (VB3 ibias vdd vdd) {pmos_lvt_model} l=Lbias w=Wbp_big m=m_Wbp_big nf=nf_Wbp_big 

M52 (net7 VB1 vdd vdd) {pmos_lvt_model} l=Lbias w=Wbp_big m=m_Wbp_big nf=nf_Wbp_big 

M3 (VB2 VB2 net5 vdd) {pmos_lvt_model} l=Lbias w=Wbp_big m=m_Wbp_big nf=nf_Wbp_big 

M6 (net5 VB2 vdd vdd) {pmos_lvt_model} l=Lbias w=Wbp_small m=m_Wbp_small nf=nf_Wbp_small 

M7 (VB1 VB2 net7 vdd) {pmos_lvt_model} l=Lbias w=Wbp_big m=m_Wbp_big nf=nf_Wbp_big 

M25 (net2 VB3 vss vss) {nmos_lvt_model} l=Lbias w=Wbn_small m=m_Wbn_small nf=nf_Wbn_small 

M9 (VB4 VB3 net3 vss) {nmos_lvt_model} l=Lbias w=Wbn_big m=m_Wbn_big nf=nf_Wbn_big 

M13 (VB3 VB3 net2 vss) {nmos_lvt_model} l=Lbias w=Wbn_big m=m_Wbn_big nf=nf_Wbn_big 

M8 (net3 VB4 vss vss) {nmos_lvt_model} l=Lbias w=Wbn_big m=m_Wbn_big nf=nf_Wbn_big 

M10 (VB1 VB3 net6 vss) {nmos_lvt_model} l=Lbias w=Wbn_big m=m_Wbn_big nf=nf_Wbn_big 

M11 (net6 VB4 vss vss) {nmos_lvt_model} l=Lbias w=Wbn_big m=m_Wbn_big nf=nf_Wbn_big 

M12 (VB2 VB3 net4 vss) {nmos_lvt_model} l=Lbias w=Wbn_big m=m_Wbn_big nf=nf_Wbn_big 

M4 (net4 VB4 vss vss) {nmos_lvt_model} l=Lbias w=Wbn_big m=m_Wbn_big nf=nf_Wbn_big 

// PMOS input differential pair
Mtailp (ntail VB1 vdd vdd) {pmos_lvt_model} w=Wbp_big l=Lbias nf=nf_Wbp_big m=m_tail_unit*m_Wbp_big
Mdiff1 (nfold_l vin ntail vdd) {pmos_lvt_model} w=Wdiffp l=Ldiffp nf=1
Mdiff2 (nfold_r vip ntail vdd) {pmos_lvt_model} w=Wdiffp l=Ldiffp nf=1

// NMOS folded branches and common-gate cascodes
Mfold1 (nfold_l VB4 vss vss) {nmos_lvt_model} w=Wbn_big l=Lbias nf=nf_Wbn_big m=m_tail_unit*m_Wbn_big
Mfold2 (nfold_r VB4 vss vss) {nmos_lvt_model} w=Wbn_big l=Lbias nf=nf_Wbn_big m=m_tail_unit*m_Wbn_big
Mcasn1 (pmirr VB3 nfold_l vss) {nmos_lvt_model} w=Wbn_big l=Lbias nf=nf_Wbn_big m=m_half_unit*m_Wbn_big
Mcasn2 (nstage1 VB3 nfold_r vss) {nmos_lvt_model} w=Wbn_big l=Lbias nf=nf_Wbn_big m=m_half_unit*m_Wbn_big

// PMOS low-voltage cascode current-mirror load
Mmirr1 (npm_l pmirr vdd vdd) {pmos_lvt_model} w=Wbp_big l=Lbias nf=nf_Wbp_big m=m_half_unit*m_Wbp_big
Mmirr2 (npm_r pmirr vdd vdd) {pmos_lvt_model} w=Wbp_big l=Lbias nf=nf_Wbp_big m=m_half_unit*m_Wbp_big
Mcasp1 (pmirr VB2 npm_l vdd) {pmos_lvt_model} w=Wbp_big l=Lbias nf=nf_Wbp_big m=m_half_unit*m_Wbp_big
Mcasp2 (nstage1 VB2 npm_r vdd) {pmos_lvt_model} w=Wbp_big l=Lbias nf=nf_Wbp_big m=m_half_unit*m_Wbp_big

// Second stage and Miller compensation
Mcs (vout nstage1 vdd vdd) {pmos_lvt_model} w=Wcs l=Lbias nf=1
Mload (vout VB4 vss vss) {nmos_lvt_model} w=Wbn_big l=Lbias nf=nf_Wbn_big m=m_load_unit*m_Wbn_big
Rz (nstage1 n_rz) resistor r=Rz
Cc (n_rz vout) capacitor c=Cc
ends folded_cascode
"""

_TB_AC_TEMPLATE = """\
// tb_folded_cascode_ac.scs -- Folded-Cascode OTA differential AC analysis
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

Xdut (vinp vinn vout ibias vdd vss) folded_cascode
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
// tb_folded_cascode_sr.scs -- Unity-gain large-signal slew-rate analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} IBIAS={IBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (ibias vss) isource type=dc dc=IBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=2n rise=100p fall=100p width=50n period=100n
VFBsrc (vin vout) vsource type=dc dc=0

Xdut (vinp vin vout ibias vdd vss) folded_cascode
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
srTran tran stop=120n maxstep=10p

save vinp vout
"""

_TB_ST_TEMPLATE = """\
// tb_folded_cascode_st.scs -- Unity-gain 0.1% settling-time analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} IBIAS={IBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (ibias vss) isource type=dc dc=IBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=5n rise=100p fall=100p width=50n period=100n
VFBsrc (vin vout) vsource type=dc dc=0

Xdut (vinp vin vout ibias vdd vss) folded_cascode
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
stTran tran stop=120n maxstep=10p

save vinp vout
"""


def _fmt(value: float) -> str:
    """Format a float with SPICE engineering suffix (u, n, p, f, k)."""
    abs_v = abs(value)
    if abs_v >= 1e3:
        return f"{value * 1e-3:.6g}k"
    if abs_v >= 1e-3:
        return f"{value:.6g}"
    if abs_v >= 1e-6:
        return f"{value * 1e6:.6g}u"
    if abs_v >= 1e-9:
        return f"{value * 1e9:.6g}n"
    if abs_v >= 1e-12:
        return f"{value * 1e12:.6g}p"
    return f"{value * 1e15:.6g}f"
