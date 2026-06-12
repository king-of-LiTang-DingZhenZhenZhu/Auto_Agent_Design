"""NMCF Three-Stage OTA -- PMOS-input three-stage Miller-compensated amplifier.

Reference inspiration:
  /Users/hnchen/Desktop/LLM_Task/AnalogGym/AnalogGym/Amplifier/spectre_netlist/Leung_NMCF_Pin_3

Topology:

  Stage 1:
      PMOS differential pair with PMOS tail and NMOS mirror load.

  Stage 2:
      NMOS common-source gain stage with PMOS current-source load.

  Stage 3:
      PMOS common-source output stage with NMOS current-source load.

  Compensation:
      Cc1 + Rz1 : Miller compensation between stage1_out and stage2_out
      Cc2        : Miller/output compensation between stage2_out and vout

Bias simplification:
  The external ibias pin provides a reference current.  A compact internal
  MOS bias network derives the PMOS tail bias, the stage-2 PMOS load bias,
  and the shared NMOS bias used by the NMOS mirror/load devices.

Port order: vip vin vout ibias vdd vss
"""

from __future__ import annotations

from topologies.base import BaseTopology, TopologyMeta
from models import CircuitFiles, ParamDef, ParamSpace


class NMCFThreeStageOTA(BaseTopology):
    """Three-stage OTA with nested Miller compensation.

    The first stage is PMOS-input, followed by an NMOS gain stage and a PMOS
    output stage.  Compared with the two-stage and folded-cascode topologies,
    this is the highest-gain and highest-complexity option in the current
    opamp library.
    """

    meta = TopologyMeta(
        name="nmcf_three_stage",
        display_name="NMCF Three-Stage OTA",
        description=(
            "Three-stage OTA with PMOS input differential pair, NMOS intermediate "
            "gain stage, PMOS output stage, and nested Miller compensation. "
            "Targeted at very high gain and heavy-load applications."
        ),
        min_gain_db=75,
        max_gain_db=115,
        min_bw_hz=5e5,
        max_bw_hz=6e8,
        typical_power_w=4e-3,
        complexity=4,
        escalation=None,
    )

    DEFAULT_PARAMS: dict[str, float] = {
        # Stage 1: PMOS input pair + PMOS tail
        "Wtail1": 18e-6,
        "Ltail1": 200e-9,
        "Wdiff1": 10e-6,
        "Ldiff1": 80e-9,
        "Wload1": 10e-6,
        "Lload1": 100e-9,
        # Stage 2: NMOS gain stage + PMOS load
        "Wgm2": 14e-6,
        "Lgm2": 80e-9,
        "Wload2": 16e-6,
        "Lload2": 120e-9,
        # Stage 3: PMOS output stage + NMOS load
        "Wgm3": 24e-6,
        "Lgm3": 100e-9,
        "Wload3": 12e-6,
        "Lload3": 180e-9,
        # Internal bias generator
        "Wbiasn": 4e-6,
        "Lbiasn": 200e-9,
        "Wbiasp": 8e-6,
        "Lbiasp": 200e-9,
        # Compensation
        "Cc1": 800e-15,
        "Rz1": 1000.0,
        "Cc2": 500e-15,
    }

    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        """Generate the DUT .cir subcircuit netlist."""
        p = dict(self.DEFAULT_PARAMS)
        if params:
            p.update(params)

        return _CIRCUIT_TEMPLATE.format(
            Wtail1=_fmt(p["Wtail1"]),
            Ltail1=_fmt(p["Ltail1"]),
            Wdiff1=_fmt(p["Wdiff1"]),
            Ldiff1=_fmt(p["Ldiff1"]),
            Wload1=_fmt(p["Wload1"]),
            Lload1=_fmt(p["Lload1"]),
            Wgm2=_fmt(p["Wgm2"]),
            Lgm2=_fmt(p["Lgm2"]),
            Wload2=_fmt(p["Wload2"]),
            Lload2=_fmt(p["Lload2"]),
            Wgm3=_fmt(p["Wgm3"]),
            Lgm3=_fmt(p["Lgm3"]),
            Wload3=_fmt(p["Wload3"]),
            Lload3=_fmt(p["Lload3"]),
            Wbiasn=_fmt(p["Wbiasn"]),
            Lbiasn=_fmt(p["Lbiasn"]),
            Wbiasp=_fmt(p["Wbiasp"]),
            Lbiasp=_fmt(p["Lbiasp"]),
            Cc1=_fmt(p["Cc1"]),
            Rz1=_fmt(p["Rz1"]),
            Cc2=_fmt(p["Cc2"]),
        )

    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        """Generate the testbench .sp file."""
        vdd = 0.8
        vcm = 0.3
        ibias = 40e-6
        cload = 10e-12

        if params:
            vdd = params.get("VDD", vdd)
            vcm = params.get("VCM", vcm)
            ibias = params.get("IBIAS", params.get("VBIAS", ibias))
            cload = params.get("CL", cload)

        if analysis_type == "tran":
            return _TB_TRAN_TEMPLATE.format(
                VDD=vdd,
                VCM=vcm,
                IBIAS=_fmt(ibias),
                CL=_fmt(cload),
                VHIGH=vcm + 0.15,
                VLOW=vcm - 0.15,
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
        """Return CircuitFiles with both AC and transient testbenches."""
        circuit_content = self.generate_circuit(params)
        tb_ac = self.generate_testbench(params, analysis_type="ac")
        tb_tran = self.generate_testbench(params, analysis_type="tran")
        circuit_name = CircuitFiles.extract_subckt_name(circuit_content)
        return CircuitFiles(
            circuit_netlist=circuit_content,
            testbenches=[tb_ac, tb_tran],
            circuit_name=circuit_name,
        )

    # ------------------------------------------------------------------
    # gm/Id support
    # ------------------------------------------------------------------

    def get_gmid_spec(self):
        """Return gm/Id spec for NMCF three-stage OTA — reduces 17 → 25 params.

        Three independent branch currents:
        - I_tail1: Stage 1 PMOS tail current
        - I_s2: Stage 2 NMOS gain + PMOS load bias current
        - I_s3: Stage 3 PMOS output + NMOS load bias current

        Bias transistors (Wbiasn, Wbiasp) remain at DEFAULT_PARAMS — not
        part of the gm/Id search space.  They are small fixed-size devices
        that generate vbiasn/vbiasp from the external ibias current.
        """
        from models import BranchCurrentSpec, GmidTopologySpec, TransistorSpec

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
                    model="pch_mac",
                    current_source="I_tail1", current_fraction=1.0,
                    gm_id_low=5, gm_id_high=20, gm_id_default=8,
                    L_low=100e-9, L_high=900e-9, L_default=200e-9,
                    Vds_estimate=0.3,
                ),
                # -- Stage 1: PMOS diff pair (each I_tail1/2) --
                TransistorSpec(
                    role="stage1_diff_pmos",
                    w_param="Wdiff1", l_param="Ldiff1",
                    model="pch_mac",
                    current_source="I_tail1", current_fraction=0.5,
                    gm_id_low=10, gm_id_high=24, gm_id_default=14,
                    L_low=60e-9, L_high=500e-9, L_default=80e-9,
                    Vds_estimate=0.25, multiplicity=2,
                ),
                # -- Stage 1: NMOS current mirror load (each I_tail1/2) --
                TransistorSpec(
                    role="stage1_load_nmos",
                    w_param="Wload1", l_param="Lload1",
                    model="nch_mac",
                    current_source="I_tail1", current_fraction=0.5,
                    gm_id_low=8, gm_id_high=24, gm_id_default=12,
                    L_low=60e-9, L_high=500e-9, L_default=100e-9,
                    Vds_estimate=0.3, multiplicity=2,
                ),
                # -- Stage 2: NMOS common-source gain stage (gate=s1_out) --
                TransistorSpec(
                    role="stage2_gain_nmos",
                    w_param="Wgm2", l_param="Lgm2",
                    model="nch_mac",
                    current_source="I_s2", current_fraction=1.0,
                    gm_id_low=10, gm_id_high=24, gm_id_default=15,
                    L_low=60e-9, L_high=500e-9, L_default=80e-9,
                    Vds_estimate=0.3,
                ),
                # -- Stage 2: PMOS current-source load (gate=vbiasp) --
                TransistorSpec(
                    role="stage2_load_pmos",
                    w_param="Wload2", l_param="Lload2",
                    model="pch_mac",
                    current_source="I_s2", current_fraction=1.0,
                    gm_id_low=5, gm_id_high=20, gm_id_default=8,
                    L_low=100e-9, L_high=900e-9, L_default=120e-9,
                    Vds_estimate=0.4,
                ),
                # -- Stage 3: PMOS common-source output (gate=s2_out) --
                TransistorSpec(
                    role="stage3_gain_pmos",
                    w_param="Wgm3", l_param="Lgm3",
                    model="pch_mac",
                    current_source="I_s3", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=22, gm_id_default=12,
                    L_low=60e-9, L_high=300e-9, L_default=100e-9,
                    Vds_estimate=0.55,
                ),
                # -- Stage 3: NMOS current-source load (gate=vbiasn=ibias) --
                TransistorSpec(
                    role="stage3_load_nmos",
                    w_param="Wload3", l_param="Lload3",
                    model="nch_mac",
                    current_source="I_s3", current_fraction=1.0,
                    gm_id_low=5, gm_id_high=20, gm_id_default=8,
                    L_low=100e-9, L_high=900e-9, L_default=180e-9,
                    Vds_estimate=0.35,
                ),
            ],
            pass_through_params=[
                ParamDef(
                    name="Cc1", low=0.05e-12, high=10e-12,
                    log_scale=True, unit="F",
                ),
                ParamDef(
                    name="Rz1", low=1.0, high=100e3,
                    log_scale=True, unit="Ohm",
                ),
                ParamDef(
                    name="Cc2", low=0.05e-12, high=10e-12,
                    log_scale=True, unit="F",
                ),
            ],
        )

    def get_default_params(self) -> dict[str, float]:
        return dict(self.DEFAULT_PARAMS)

    def get_param_space(self) -> ParamSpace:
        return ParamSpace(
            params=[
                ParamDef(
                    name="Wtail1", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Ltail1", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wdiff1", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Ldiff1", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wload1", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lload1", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wgm2", low=0.5e-6, high=120e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lgm2", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wload2", low=0.5e-6, high=120e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lload2", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wgm3", low=0.5e-6, high=150e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lgm3", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wload3", low=0.5e-6, high=150e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lload3", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wbiasn", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lbiasn", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wbiasp", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
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
                    name="Rz1", low=1.0, high=100e3,
                    log_scale=True, unit="Ohm",
                ),
                ParamDef(
                    name="Cc2", low=0.05e-12, high=10e-12,
                    log_scale=True, unit="F",
                ),
            ]
        )


_CIRCUIT_TEMPLATE = """\
* nmcf_three_stage.cir -- NMCF Three-Stage OTA (Python-generated)
* Stage 1: PMOS input pair + PMOS tail + NMOS mirror load
* Stage 2: NMOS common-source gain stage + PMOS current-source load
* Stage 3: PMOS common-source output stage + NMOS current-source load
* Compensation: nested Miller (Cc1 + Rz1, Cc2)
* Bias: internal MOS bias generator from external ibias pin
.lib '/PDKS/TSMC28nm/models/hspice/toplevel.l' TOP_TT
.options redefinedparams=ignore
.param Wtail1={Wtail1} Ltail1={Ltail1} Wdiff1={Wdiff1} Ldiff1={Ldiff1}
.param Wload1={Wload1} Lload1={Lload1} Wgm2={Wgm2} Lgm2={Lgm2}
.param Wload2={Wload2} Lload2={Lload2} Wgm3={Wgm3} Lgm3={Lgm3}
.param Wload3={Wload3} Lload3={Lload3}
.param Wbiasn={Wbiasn} Lbiasn={Lbiasn} Wbiasp={Wbiasp} Lbiasp={Lbiasp}
.param Cc1={Cc1} Rz1={Rz1} Cc2={Cc2}

.subckt nmcf_three_stage vip vin vout ibias vdd vss
* --- Bias generator ---
* External Ibias enters ibias pin → Mbn1 diode to vss generates vbiasn
* Mbp1 (PMOS diode) + Mbn2 (NMOS mirror) generate vbiasp
Mbn1 ibias  ibias  vss vss nch_mac W='Wbiasn' L='Lbiasn' nf=1
Mbn2 vbiasp ibias  vss vss nch_mac W='Wbiasn' L='Lbiasn' nf=1
Mbp1 vbiasp vbiasp vdd vdd pch_mac W='Wbiasp' L='Lbiasp' nf=1
* --- Stage 1: PMOS input diff pair + NMOS mirror load ---
Mtail1  tail     vbiasp  vdd vdd pch_mac W='Wtail1' L='Ltail1' nf=1
Mdiff1a s1_mirr  vip     tail vdd pch_mac W='Wdiff1' L='Ldiff1' nf=1
Mdiff1b s1_out   vin     tail vdd pch_mac W='Wdiff1' L='Ldiff1' nf=1
Mload1a s1_mirr  s1_mirr vss vss nch_mac W='Wload1' L='Lload1' nf=1
Mload1b s1_out   s1_mirr vss vss nch_mac W='Wload1' L='Lload1' nf=1
* --- Stage 2: NMOS CS gain + PMOS current-source load ---
Mgm2   s2_out s1_out vss vss nch_mac W='Wgm2' L='Lgm2' nf=1
Mload2 s2_out vbiasp vdd vdd pch_mac W='Wload2' L='Lload2' nf=1
* --- Stage 3: PMOS CS output + NMOS current-source load ---
Mgm3   vout s2_out vdd vdd pch_mac W='Wgm3' L='Lgm3' nf=1
Mload3 vout ibias  vss vss nch_mac W='Wload3' L='Lload3' nf=1
* --- Nested Miller compensation ---
Rz1 s1_out n_rz1 R='Rz1'
Cc1 n_rz1  s2_out C='Cc1'
Cc2 s2_out vout   C='Cc2'
.ends nmcf_three_stage
"""

_TB_AC_TEMPLATE = """\
* tb_nmcf_three_stage_ac.sp -- NMCF Three-Stage OTA AC Analysis
.include "circuit.cir"

* --- Power supply ---
VDD vdd 0 DC {VDD}
VSS vss 0 DC 0
Iibias vdd ibias DC {IBIAS}

* --- Input stimulus ---
Vcm vcm 0 DC {VCM}
Vinp vinp vcm DC 0 AC 1
Vinn vinn 0  DC 0

* --- Closed-loop feedback for DC stability ---
Rfb vout vinn 1G
Cfb vinn 0 1

* --- DUT ---
Xdut vinp vinn vout ibias vdd vss nmcf_three_stage
CL vout 0 {CL}

* --- Analysis ---
.op
.ac dec 20 1 20g
.temp 27

* --- Measurements ---
.meas ac gain_dc find vdb(vout) at=1k
.meas ac phase_dc find vp(vout) at=1k
.meas ac gbw_hz when vdb(vout)=0 cross=1
.meas ac phase_at_ugf find vp(vout) when vdb(vout)=0 cross=1
.meas dc power_total PARAM='-I(Vdd)*{VDD}'

.end
"""

_TB_TRAN_TEMPLATE = """\
* tb_nmcf_three_stage_tran.sp -- NMCF Three-Stage OTA Transient Analysis
.include "circuit.cir"

* --- Power supply ---
VDD vdd 0 DC {VDD}
VSS vss 0 DC 0
Iibias vdd ibias DC {IBIAS}

* --- Unity-gain buffer: vout feeds back to vin ---
Vcm vcm 0 DC {VCM}
Vinp vinp vcm DC 0 PULSE({VLOW} {VHIGH} 2n 100p 100p 50n 100n)

* --- Feedback (buffer) ---
Vfb vin vout DC 0

* --- DUT ---
Xdut vinp vin vout ibias vdd vss nmcf_three_stage
CL vout 0 {CL}

* --- Analysis ---
.tran 10p 100n
.temp 27

* --- Measurements ---
.meas tran slew_rate_rise MAX deriv(v(vout)) from=2n to=10n
.meas tran slew_rate_fall MIN deriv(v(vout)) from=2n to=10n
.meas tran settling_rise TRIG v(vinp) VAL={VHIGH} RISE=1
+   TARG v(vout) VAL={VHIGH}*0.999 RISE=1
.meas tran settling_fall TRIG v(vinp) VAL={VLOW} FALL=1
+   TARG v(vout) VAL={VLOW}*0.999 FALL=1

.end
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
