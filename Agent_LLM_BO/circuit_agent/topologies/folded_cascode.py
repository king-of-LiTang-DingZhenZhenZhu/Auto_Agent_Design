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

import math

from topologies.base import BaseTopology, TopologyMeta
from models import CircuitFiles, ParamDef, ParamSpace


def _bias_w(name: str) -> ParamDef:
    return ParamDef(
        name=name,
        low=0.2e-6,
        high=5e-6,
        log_scale=True,
        unit="m",
        max_per_finger=2.6e-6,
    )


def _bias_l(name: str) -> ParamDef:
    return ParamDef(
        name=name,
        low=30e-9,
        high=500e-9,
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
        min_gain_db=60,
        max_gain_db=85,
        min_gbw_hz=1e6,
        max_gbw_hz=1e9,
        typical_power_w=2e-3,
        complexity=3,
        escalation="nmcf_three_stage",
    )

    DEFAULT_PARAMS: dict[str, float] = {
        # PMOS tail current source
        "Wtailp": 20e-6,
        "Ltailp": 200e-9,
        # PMOS input differential pair
        "Wdiffp": 12e-6,
        "Ldiffp": 80e-9,
        # NMOS folded branch current sources
        "Wfoldn": 10e-6,
        "Lfoldn": 200e-9,
        # NMOS common-gate cascode devices
        "Wcasn": 8e-6,
        "Lcasn": 120e-9,
        # PMOS current mirror devices
        "Wmirrp": 12e-6,
        "Lmirrp": 200e-9,
        # PMOS cascode mirror devices
        "Wcasp": 12e-6,
        "Lcasp": 120e-9,
        # Second-stage PMOS common-source amplifier
        "Wcs": 30e-6,
        # Second-stage NMOS current-source load
        "Wload": 15e-6,
        "Lload": 200e-9,
        # Internal reference-bias generator
        "Wbp_big": 2.4e-6,
        "Lbp_big": 400e-9,
        "Wbp_small": 0.8e-6,
        "Lbp_small": 400e-9,
        "Wbn_big": 1.2e-6,
        "Lbn_big": 400e-9,
        "Wbn_small": 0.4e-6,
        "Lbn_small": 400e-9,
        # Compensation
        "Cc": 250e-15,
        "Rz": 1000.0,
    }

    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        """Generate the DUT .cir subcircuit netlist."""
        p = dict(self.DEFAULT_PARAMS)
        if params:
            p.update(params)

        return _CIRCUIT_TEMPLATE.format(
            Wtailp=_fmt(p["Wtailp"]),
            Ltailp=_fmt(p["Ltailp"]),
            Wdiffp=_fmt(p["Wdiffp"]),
            Ldiffp=_fmt(p["Ldiffp"]),
            Wfoldn=_fmt(p["Wfoldn"]),
            Lfoldn=_fmt(p["Lfoldn"]),
            Wcasn=_fmt(p["Wcasn"]),
            Lcasn=_fmt(p["Lcasn"]),
            Wmirrp=_fmt(p["Wmirrp"]),
            Lmirrp=_fmt(p["Lmirrp"]),
            Wcasp=_fmt(p["Wcasp"]),
            Lcasp=_fmt(p["Lcasp"]),
            Wcs=_fmt(p["Wcs"]),
            Wload=_fmt(p["Wload"]),
            Lload=_fmt(p["Lload"]),
            Wbp_big=_fmt(p["Wbp_big"]),
            Lbp_big=_fmt(p["Lbp_big"]),
            Wbp_small=_fmt(p["Wbp_small"]),
            Lbp_small=_fmt(p["Lbp_small"]),
            Wbn_big=_fmt(p["Wbn_big"]),
            Lbn_big=_fmt(p["Lbn_big"]),
            Wbn_small=_fmt(p["Wbn_small"]),
            Lbn_small=_fmt(p["Lbn_small"]),
            Cc=_fmt(p["Cc"]),
            Rz=_fmt(p["Rz"]),
        )

    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        """Generate the Spectre-native testbench .scs file."""
        vdd = 1.0
        vcm = 0.45
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
        return dict(self.DEFAULT_PARAMS)

    def get_param_space(self) -> ParamSpace:
        return ParamSpace(
            params=[
                ParamDef(
                    name="Wtailp", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Ltailp", low=200e-9, high=600e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wdiffp", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Ldiffp", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wfoldn", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lfoldn", low=200e-9, high=600e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wcasn", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lcasn", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wmirrp", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lmirrp", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wcasp", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lcasp", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wcs", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Wload", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lload", low=200e-9, high=600e-9,
                    log_scale=True, unit="m",
                ),
                # --- Internal bias generator ---
                _bias_w("Wbp_big"),
                _bias_l("Lbp_big"),
                _bias_w("Wbp_small"),
                _bias_l("Lbp_small"),
                _bias_w("Wbn_big"),
                _bias_l("Lbn_big"),
                _bias_w("Wbn_small"),
                _bias_l("Lbn_small"),
                ParamDef(
                    name="Cc", low=0.01e-12, high=5e-12,
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
        from models import BranchCurrentSpec, GmidTopologySpec, TransistorSpec

        tail_current_low = 1e-6
        folded_branch_current_low = 1e-6
        second_stage_current_low = 1e-6
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
            single_input_current = gm_required / 22.0

            # Current-budget heuristic:
            # input pair = 2x, two folded sides = 2x + 2x,
            # second stage = 4x, hence total minimum = 10x.
            tail_current_low = max(tail_current_low, 2.0 * single_input_current)
            folded_branch_current_low = max(
                folded_branch_current_low, 2.0 * single_input_current
            )
            second_stage_current_low = max(
                second_stage_current_low, 4.0 * single_input_current
            )

            bounds = (
                ("I_tail", tail_current_low, 200e-6),
                ("I_fold", folded_branch_current_low, 200e-6),
                ("I_cs", second_stage_current_low, 500e-6),
            )
            for name, lower, upper in bounds:
                if lower > upper:
                    raise ValueError(
                        "Folded-cascode GBW/CL estimate requires "
                        f"{name} >= {lower:.3e} A, above the "
                        f"{upper:.3e} A upper bound"
                    )

        return GmidTopologySpec(
            branch_currents=[
                # Tail current for PMOS diff pair
                BranchCurrentSpec(
                    name="I_tail",
                    low=tail_current_low,
                    high=200e-6,
                    default=max(20e-6, tail_current_low),
                ),
                # NMOS folded-branch DC current (per side)
                BranchCurrentSpec(
                    name="I_fold",
                    low=folded_branch_current_low,
                    high=200e-6,
                    default=max(15e-6, folded_branch_current_low),
                ),
                # Second-stage PMOS CS bias current
                BranchCurrentSpec(
                    name="I_cs",
                    low=second_stage_current_low,
                    high=500e-6,
                    default=max(40e-6, second_stage_current_low),
                ),
            ],
            transistors=[
                # -- PMOS tail current source --
                TransistorSpec(
                    role="tail_pmos",
                    w_param="Wtailp", l_param="Ltailp",
                    model="pch_mac",
                    current_source="I_tail", current_fraction=1.0,
                    gm_id_low=5, gm_id_high=20, gm_id_default=8,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.2,
                ),
                # -- PMOS diff pair (each side carries I_tail / 2) --
                TransistorSpec(
                    role="diff_pair_pmos",
                    w_param="Wdiffp", l_param="Ldiffp",
                    model="pch_mac",
                    current_source="I_tail", current_fraction=0.5,
                    gm_id_low=10, gm_id_high=22, gm_id_default=14,
                    L_low=60e-9, L_high=500e-9, L_default=80e-9,
                    Vds_estimate=0.25, Vbs=-0.3, multiplicity=2,
                ),
                # -- NMOS folded-branch current sources --
                TransistorSpec(
                    role="fold_nmos",
                    w_param="Wfoldn", l_param="Lfoldn",
                    model="nch_mac",
                    current_source="I_fold", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=22, gm_id_default=12,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.25, multiplicity=2,
                ),
                # -- NMOS common-gate cascode devices --
                TransistorSpec(
                    role="cas_nmos",
                    w_param="Wcasn", l_param="Lcasn",
                    model="nch_mac",
                    current_source="I_fold", current_fraction=1.0,
                    gm_id_low=10, gm_id_high=24, gm_id_default=15,
                    L_low=80e-9, L_high=500e-9, L_default=120e-9,
                    Vds_estimate=0.35, Vbs=-0.3, multiplicity=2,
                ),
                # -- PMOS current mirror devices --
                TransistorSpec(
                    role="mirr_pmos",
                    w_param="Wmirrp", l_param="Lmirrp",
                    model="pch_mac",
                    current_source="I_fold", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=20, gm_id_default=12,
                    L_low=100e-9, L_high=900e-9, L_default=200e-9,
                    Vds_estimate=0.3, multiplicity=2,
                ),
                # -- PMOS cascode mirror devices --
                TransistorSpec(
                    role="casp_pmos",
                    w_param="Wcasp", l_param="Lcasp",
                    model="pch_mac",
                    current_source="I_fold", current_fraction=1.0,
                    gm_id_low=10, gm_id_high=24, gm_id_default=16,
                    L_low=80e-9, L_high=500e-9, L_default=120e-9,
                    Vds_estimate=0.3, Vbs=-0.3, multiplicity=2,
                ),
                # -- Second-stage NMOS current-source load --
                TransistorSpec(
                    role="load_nmos",
                    w_param="Wload", l_param="Lload",
                    model="nch_mac",
                    current_source="I_cs", current_fraction=1.0,
                    gm_id_low=5, gm_id_high=20, gm_id_default=8,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.4,
                ),
                # -- Second-stage PMOS common-source amplifier --
                TransistorSpec(
                    role="cs_pmos",
                    w_param="Wcs", l_param="Lload",
                    model="pch_mac",
                    current_source="I_cs", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=22, gm_id_default=12,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.6,
                ),
            ],
            pass_through_params=[
                ParamDef(
                    name="Cc", low=0.01e-12, high=5e-12,
                    log_scale=True, unit="F",
                ),
                ParamDef(
                    name="Rz", low=1.0, high=100e3,
                    log_scale=True, unit="Ohm",
                ),
            ],
        )


_CIRCUIT_TEMPLATE = """\
// folded_cascode.cir -- Folded-Cascode Miller OTA (Spectre native syntax)
simulator lang=spectre insensitive=yes

include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_tt

parameters Wtailp={Wtailp} Ltailp={Ltailp} Wdiffp={Wdiffp} Ldiffp={Ldiffp}
parameters Wfoldn={Wfoldn} Lfoldn={Lfoldn} Wcasn={Wcasn} Lcasn={Lcasn}
parameters Wmirrp={Wmirrp} Lmirrp={Lmirrp} Wcasp={Wcasp} Lcasp={Lcasp}
parameters Wcs={Wcs} Wload={Wload} Lload={Lload}
parameters Wbp_big={Wbp_big} Lbp_big={Lbp_big} Wbp_small={Wbp_small} Lbp_small={Lbp_small}
parameters Wbn_big={Wbn_big} Lbn_big={Lbn_big} Wbn_small={Wbn_small} Lbn_small={Lbn_small}
parameters Cc={Cc} Rz={Rz}

subckt folded_cascode (vip vin vout ibias vdd vss)
// Internal bias generator
M7 (VB1 VB2 net4 vdd) pch_lvt_mac w=Wbp_big l=Lbp_big nf=1
M6 (net4 VB1 vdd vdd) pch_lvt_mac w=Wbp_big l=Lbp_big nf=1
M4 (VB2 VB2 vdd vdd) pch_lvt_mac w=Wbp_small l=Lbp_small nf=1
M2 (VB4 ibias vdd vdd) pch_lvt_mac w=Wbp_big l=Lbp_big nf=1
M1 (VB3 ibias vdd vdd) pch_lvt_mac w=Wbp_big l=Lbp_big nf=1
M0 (ibias ibias vdd vdd) pch_lvt_mac w=Wbp_big l=Lbp_big nf=1
M13 (net6 VB4 vss vss) nch_lvt_mac w=Wbn_big l=Lbn_big nf=1
M12 (VB1 VB3 net6 vss) nch_lvt_mac w=Wbn_big l=Lbn_big nf=1
M11 (net2 VB4 vss vss) nch_lvt_mac w=Wbn_big l=Lbn_big nf=1
M10 (VB2 VB3 net2 vss) nch_lvt_mac w=Wbn_big l=Lbn_big nf=1
M9 (net3 VB4 vss vss) nch_lvt_mac w=Wbn_big l=Lbn_big nf=1
M8 (VB4 VB3 net3 vss) nch_lvt_mac w=Wbn_big l=Lbn_big nf=1
M5 (VB3 VB3 vss vss) nch_lvt_mac w=Wbn_small l=Lbn_small nf=1

// PMOS input differential pair
Mtailp (ntail VB1 vdd vdd) pch_lvt_mac w=Wtailp l=Ltailp nf=1
Mdiff1 (nfold_l vip ntail vdd) pch_lvt_mac w=Wdiffp l=Ldiffp nf=1
Mdiff2 (nfold_r vin ntail vdd) pch_lvt_mac w=Wdiffp l=Ldiffp nf=1

// NMOS folded branches and common-gate cascodes
Mfold1 (nfold_l VB4 vss vss) nch_lvt_mac w=Wfoldn l=Lfoldn nf=1
Mfold2 (nfold_r VB4 vss vss) nch_lvt_mac w=Wfoldn l=Lfoldn nf=1
Mcasn1 (pmirr VB3 nfold_l vss) nch_lvt_mac w=Wcasn l=Lcasn nf=1
Mcasn2 (nstage1 VB3 nfold_r vss) nch_lvt_mac w=Wcasn l=Lcasn nf=1

// PMOS low-voltage cascode current-mirror load
Mmirr1 (npm_l pmirr vdd vdd) pch_lvt_mac w=Wmirrp l=Lmirrp nf=1
Mmirr2 (npm_r pmirr vdd vdd) pch_lvt_mac w=Wmirrp l=Lmirrp nf=1
Mcasp1 (pmirr VB2 npm_l vdd) pch_lvt_mac w=Wcasp l=Lcasp nf=1
Mcasp2 (nstage1 VB2 npm_r vdd) pch_lvt_mac w=Wcasp l=Lcasp nf=1

// Second stage and Miller compensation
Mcs (vout nstage1 vdd vdd) pch_lvt_mac w=Wcs l=Lload nf=1
Mload (vout VB4 vss vss) nch_lvt_mac w=Wload l=Lload nf=1
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
