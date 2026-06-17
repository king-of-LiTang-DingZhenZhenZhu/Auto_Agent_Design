"""Two-Stage Miller-Compensated OTA — NMOS-input + PMOS common-source second stage.

Reference: /Desktop/Knowleage_Base/01-电路拓扑/两级密勒补偿运放.md

Topology (方案A — 五管OTA NMOS 输入第一级 + 共源第二级):

  First Stage (NMOS-input 5T OTA):
      M1/M2 — NMOS differential pair
      M3/M4 — PMOS current-mirror load (diode-connected M3)
      M5    — NMOS tail current source (Vb)

  Second Stage:
      M6    — PMOS common-source amplifier (gate ← stage1_out)
      M7    — NMOS current-source load (Vb)

  Compensation:
      Cc    — Miller capacitor between stage1_out and vout
      Rz    — Nulling resistor in series with Cc (pushes RHP zero to LHP)

  偏置设计：M5（第一级尾电流管）和 M7（第二级负载管）共用 Vb，
  减少偏置电路开销。

Port order: vip vin vout vb vdd vss
"""

from __future__ import annotations

import math

from topologies.base import BaseTopology, TopologyMeta
from models import CircuitFiles, ParamDef, ParamSpace


class TwoStageOTA(BaseTopology):
    """Two-stage Miller-compensated OTA.

    NMOS diff pair → PMOS mirror → PMOS CS second stage → NMOS load.
    第一级尾电流管(M5)与第二级负载管(M7)共用偏置 Vb。
    Suitable for Vcm ~0.4-0.6 V with VDD=1.0 V (TSMC 28nm core devices).
    """

    meta = TopologyMeta(
        name="two_stage_ota",
        display_name="Two-Stage Miller OTA",
        description=(
            "Two-stage OTA with NMOS differential pair, PMOS current-mirror "
            "first stage, PMOS common-source second stage, and Miller "
            "compensation (Cc + Rz).  High gain (55-85 dB), moderate GBW."
        ),
        min_gain_db=45,
        max_gain_db= 80,
        min_gbw_hz=10e6,
        max_gbw_hz=5e8,
        typical_power_w=1e-3,
        complexity=2,
        escalation="folded_cascode",  # future
    )

    # ------------------------------------------------------------------
    # Default parameters (SI units)
    # ------------------------------------------------------------------
    DEFAULT_PARAMS: dict[str, float] = {
        # First stage — NMOS tail current
        "Wtail": 5e-6,
        "Ltail": 200e-9,
        # First stage — NMOS diff pair
        "Wdiff": 10e-6,
        "Ldiff": 60e-9,
        # First stage — PMOS current mirror
        "Wmirr": 5e-6,
        "Lmirr": 100e-9,
        # Second stage — PMOS common-source
        "Wcs": 20e-6,
        "Lcs": 100e-9,
        # Second stage — NMOS current-source load
        "Wload": 10e-6,
        "Lload": 200e-9,
        # Compensation
        "Cc": 500e-15,
        "Rz": 1000.0,
    }

    # ------------------------------------------------------------------
    # generate_circuit
    # ------------------------------------------------------------------
    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        """Generate the DUT .cir subcircuit netlist."""
        p = dict(self.DEFAULT_PARAMS)
        if params:
            p.update(params)

        return _CIRCUIT_TEMPLATE.format(
            Wtail=_fmt(p["Wtail"]),
            Ltail=_fmt(p["Ltail"]),
            Wdiff=_fmt(p["Wdiff"]),
            Ldiff=_fmt(p["Ldiff"]),
            Wmirr=_fmt(p["Wmirr"]),
            Lmirr=_fmt(p["Lmirr"]),
            Wcs=_fmt(p["Wcs"]),
            Lcs=_fmt(p["Lcs"]),
            Wload=_fmt(p["Wload"]),
            Lload=_fmt(p["Lload"]),
            Cc=_fmt(p["Cc"]),
            Rz=_fmt(p["Rz"]),
        )

    # ------------------------------------------------------------------
    # generate_testbench
    # ------------------------------------------------------------------
    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        """Generate the Spectre-native testbench .scs file.

        Args:
            params: Override defaults (VDD, VCM, VBIAS, CL).
            analysis_type: "ac", "sr" (or legacy "tran"), or "st".
        """
        vdd = 1.0       # NMOS input needs ~0.75V ICMR min → VDD=1.0V
        vcm = 0.7
        vbias = 0.55     # shared bias for M5 (tail) and M7 (load)
        cload = 2e-12   # 2 pF (typical for ADC driver)

        if params:
            vdd = params.get("VDD", vdd)
            vcm = params.get("VCM", vcm)
            vbias = params.get("VBIAS", vbias)
            cload = params.get("CL", cload)

        if analysis_type in ("tran", "sr"):
            return _TB_SR_TEMPLATE.format(
                VDD=vdd,
                VCM=vcm,
                VBIAS=vbias,
                CL=_fmt(cload),
                VHIGH=vcm + 0.2,
                VLOW=vcm - 0.2,
            )
        if analysis_type == "st":
            return _TB_ST_TEMPLATE.format(
                VDD=vdd, VCM=vcm, VBIAS=vbias, CL=_fmt(cload),
                VHIGH=vcm + 10e-3, VLOW=vcm,
            )
        return _TB_AC_TEMPLATE.format(
            VDD=vdd,
            VCM=vcm,
            VBIAS=vbias,
            CL=_fmt(cload),
        )

    # ------------------------------------------------------------------
    # get_circuit_files (override — two testbenches)
    # ------------------------------------------------------------------
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
        """Return the gm/Id spec for the two-stage OTA.

        One independent branch current plus one integer mirror ratio:
        - I_tail: first-stage NMOS tail current
        - ratio_load_tail: Mload/Mtail mirror ratio, deriving I_cs

        Transistor roles:
        - tail_nmos (NMOS tail current source, gate=vb)
        - diff_pair_nmos (NMOS input pair, each I_tail/2)
        - mirror_pmos (PMOS current mirror load, each I_tail/2)
        - cs_pmos (second-stage PMOS CS amplifier)
        - load_nmos (second-stage NMOS current-source load, gate=vb)

        VBIAS is derived from the tail NMOS lookup VGS. M5 and M7 share this
        gate voltage, so Spectre determines M7's final operating point.
        """
        from models import (
            BranchCurrentSpec,
            CurrentMirrorRatioSpec,
            DerivedGateBiasSpec,
            GmidTopologySpec,
            TransistorSpec,
        )

        tail_current_low = 1e-6
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
            single_input_current = gm_required / 24.0
            tail_current_low = max(tail_current_low, 2.0 * single_input_current)
            if tail_current_low > 200e-6:
                raise ValueError(
                    "Two-stage OTA GBW/CL estimate requires I_tail >= "
                    f"{tail_current_low:.3e} A, above the 200 uA upper bound"
                )

        return GmidTopologySpec(
            branch_currents=[
                BranchCurrentSpec(
                    name="I_tail",
                    low=tail_current_low,
                    high=200e-6,
                    default=max(15e-6, tail_current_low),
                ),
            ],
            transistors=[
                # -- First stage: NMOS tail current source --
                TransistorSpec(
                    role="tail_nmos",
                    w_param="Wtail", l_param="Ltail",
                    model="nch_mac",
                    current_source="I_tail", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=15, gm_id_default=10,
                    L_low=100e-9, L_high=900e-9, L_default=200e-9,
                    Vds_estimate=0.2,
                ),
                # -- First stage: NMOS diff pair (each I_tail/2) --
                TransistorSpec(
                    role="diff_pair_nmos",
                    w_param="Wdiff", l_param="Ldiff",
                    model="nch_mac",
                    current_source="I_tail", current_fraction=0.5,
                    gm_id_low=8, gm_id_high=15, gm_id_default=10,
                    L_low=60e-9, L_high=500e-9, L_default=60e-9,
                    Vds_estimate=0.25, Vbs=-0.3, multiplicity=2,
                ),
                # -- First stage: PMOS current mirror load (each I_tail/2) --
                TransistorSpec(
                    role="mirror_pmos",
                    w_param="Wmirr", l_param="Lmirr",
                    model="pch_mac",
                    current_source="I_tail", current_fraction=0.5,
                    gm_id_low=8, gm_id_high=15, gm_id_default=10,
                    L_low=60e-9, L_high=500e-9, L_default=100e-9,
                    Vds_estimate=0.3, multiplicity=2,
                ),
                # -- Second stage: PMOS common-source amplifier --
                TransistorSpec(
                    role="cs_pmos",
                    w_param="Wcs", l_param="Lcs",
                    model="pch_mac",
                    current_source="I_cs", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=15, gm_id_default=12,
                    L_low=60e-9, L_high=300e-9, L_default=100e-9,
                    Vds_estimate=0.45,
                ),
                # -- Second stage: NMOS current-source load --
                TransistorSpec(
                    role="load_nmos",
                    w_param="Wload", l_param="Lload",
                    model="nch_mac",
                    current_source="I_cs", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=15, gm_id_default=10,
                    L_low=100e-9, L_high=900e-9, L_default=200e-9,
                    Vds_estimate=0.4,
                ),
            ],
            current_mirrors=[
                CurrentMirrorRatioSpec(
                    reference_role="tail_nmos",
                    output_role="load_nmos",
                    ratio_param="ratio_load_tail",
                    ratio_low=1,
                    ratio_high=3,
                    ratio_default=2,
                    share_length=True,
                    derived_current_name="I_cs",
                ),
            ],
            pass_through_params=[
                ParamDef(
                    name="Cc", low=0.1e-12, high=10e-12,
                    log_scale=True, unit="F",
                ),
                ParamDef(
                    name="Rz", low=100, high=5e3,
                    log_scale=True, unit="Ohm",
                ),
            ],
            derived_gate_biases=[
                DerivedGateBiasSpec(
                    role="tail_nmos",
                    param_name="VBIAS",
                    supply_voltage=0.0,
                    device_type="nmos",
                    low=0.5,
                    high=0.95,
                ),
            ],
        )

    # ------------------------------------------------------------------
    # get_default_params
    # ------------------------------------------------------------------
    def get_default_params(self) -> dict[str, float]:
        return dict(self.DEFAULT_PARAMS)

    # ------------------------------------------------------------------
    # get_param_space
    # ------------------------------------------------------------------
    def get_param_space(self) -> ParamSpace:
        return ParamSpace(
            params=[
                # --- First stage: tail current ---
                ParamDef(
                    name="Wtail", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=2.7e-6,
                ),
                ParamDef(
                    name="Ltail", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- First stage: diff pair ---
                ParamDef(
                    name="Wdiff", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=2.7e-6,
                ),
                ParamDef(
                    name="Ldiff", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- First stage: current mirror ---
                ParamDef(
                    name="Wmirr", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=2.7e-6,
                ),
                ParamDef(
                    name="Lmirr", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- Second stage: PMOS CS amp ---
                ParamDef(
                    name="Wcs", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=2.7e-6,
                ),
                ParamDef(
                    name="Lcs", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- Second stage: NMOS load ---
                ParamDef(
                    name="Wload", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=2.7e-6,
                ),
                ParamDef(
                    name="Lload", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- Compensation ---
                ParamDef(
                    name="Cc", low=0.1e-12, high=10e-12,
                    log_scale=True, unit="F",
                ),
                ParamDef(
                    name="Rz", low=100, high=5e3,
                    log_scale=True, unit="Ohm",
                ),
                # Shared NMOS gate bias for Mtail and Mload in non-gm/Id mode.
                ParamDef(
                    name="VBIAS", low=0.5, high=0.85,
                    log_scale=False, unit="V",
                ),
            ]
        )


# ------------------------------------------------------------------
# Spectre-native templates
# ------------------------------------------------------------------

_CIRCUIT_TEMPLATE = """\
// two_stage_ota.cir -- Two-Stage Miller OTA (Spectre native syntax)
simulator lang=spectre insensitive=yes

include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_tt

parameters Wtail={Wtail} Ltail={Ltail} Wdiff={Wdiff} Ldiff={Ldiff}
parameters Wmirr={Wmirr} Lmirr={Lmirr} Wcs={Wcs} Lcs={Lcs}
parameters Wload={Wload} Lload={Lload} Cc={Cc} Rz={Rz}

subckt two_stage_ota (vip vin vout vb vdd vss)
// First stage: NMOS differential pair
Mdiff1 (n_mirr vin n_tail vss) nch_mac w=Wdiff l=Ldiff nf=1
Mdiff2 (n_s1 vip n_tail vss) nch_mac w=Wdiff l=Ldiff nf=1
// First stage: PMOS current mirror load
Mmirr1 (n_mirr n_mirr vdd vdd) pch_mac w=Wmirr l=Lmirr nf=1
Mmirr2 (n_s1 n_mirr vdd vdd) pch_mac w=Wmirr l=Lmirr nf=1
// First stage: NMOS tail current source
Mtail (n_tail vb vss vss) nch_mac w=Wtail l=Ltail nf=1
// Second stage
Mcs (vout n_s1 vdd vdd) pch_mac w=Wcs l=Lcs nf=1
Mload (vout vb vss vss) nch_mac w=Wload l=Lload nf=1
// Miller compensation
Rz (n_s1 n_rz) resistor r=Rz
Cc (n_rz vout) capacitor c=Cc
ends two_stage_ota
"""

# Differential AC testbench
_TB_AC_TEMPLATE = """\
// tb_two_stage_ota_ac.scs -- Two-Stage OTA differential AC analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} VBIAS={VBIAS} CL={CL}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
VBIASsrc (vbias 0) vsource type=dc dc=VBIAS
VCMsrc (vcm 0) vsource type=dc dc=VCM
VIPsrc (vinp vcm) vsource type=dc dc=0 mag=1
Rfb (vout vinn) resistor r=1G
Cfb (vinn 0) capacitor c=1

Xdut (vinp vinn vout vbias vdd vss) two_stage_ota
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii
op1 dc oppoint=rawfile
opInfo info what=oppoint where=rawfile
ac1 ac start=1 stop=10G dec=20

save vout
save VDDsrc:p
"""

# Unity-gain large-signal slew-rate testbench
_TB_SR_TEMPLATE = """\
// tb_two_stage_ota_sr.scs -- Unity-gain large-signal slew-rate analysis
// 对于 SR 的测量，不能把输出目标推到电路根本到不了的位置，比如 0.9V
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} VBIAS={VBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
VBIASsrc (vbias 0) vsource type=dc dc=VBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=2n rise=100p fall=100p width=50n period=100n
VFBsrc (vin vout) vsource type=dc dc=0

Xdut (vinp vin vout vbias vdd vss) two_stage_ota
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii
srTran tran stop=200n maxstep=10p

save vinp vout
"""

_TB_ST_TEMPLATE = """\
// tb_two_stage_ota_st.scs -- Unity-gain 0.1% settling-time analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} VBIAS={VBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
VBIASsrc (vbias 0) vsource type=dc dc=VBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=5n rise=100p fall=100p width=80n period=160n
VFBsrc (vin vout) vsource type=dc dc=0

Xdut (vinp vin vout vbias vdd vss) two_stage_ota
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii
stTran tran stop=180n maxstep=10p

save vinp vout
"""


# ------------------------------------------------------------------
# internal helpers
# ------------------------------------------------------------------

def _fmt(value: float) -> str:
    """Format a float with SPICE engineering suffix (u, n, p, f, k)."""
    abs_v = abs(value)
    if abs_v >= 1e3:
        return f"{value * 1e-3:.6g}k"
    elif abs_v >= 1e-3:
        return f"{value:.6g}"
    elif abs_v >= 1e-6:
        return f"{value * 1e6:.6g}u"
    elif abs_v >= 1e-9:
        return f"{value * 1e9:.6g}n"
    elif abs_v >= 1e-12:
        return f"{value * 1e12:.6g}p"
    else:
        return f"{value * 1e15:.6g}f"
