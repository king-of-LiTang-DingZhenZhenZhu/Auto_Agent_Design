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

  Bias design: an external ideal current source drives an internal
  diode-connected NMOS.  The diode node ``ibias`` biases M5 and M7.

Port order: vip vin vout ibias vdd vss
"""

from __future__ import annotations

import math

from topologies.base import BaseTopology, TopologyMeta
from models import CircuitFiles, ParamDef, ParamSpace, format_spice_value
from pdk_profiles import get_pdk_profile, get_pdk_profile_for_params, spectre_include_line


class TwoStageOTA(BaseTopology):
    """Two-stage Miller-compensated OTA.

    NMOS diff pair → PMOS mirror → PMOS CS second stage → NMOS load.
    An external IBIAS current drives a diode-connected NMOS bias device.
    The resulting ibias voltage drives M5 and M7.
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

    # ------------------------------------------------------------------
    # Default parameters (SI units)
    # ------------------------------------------------------------------
    DEFAULT_PARAMS: dict[str, float] = {
        # First stage — NMOS tail current
        "Wbias": 5e-6,
        "Lbias": 200e-9,
        "m_tail_unit": 2,
        "ratio_load_tail": 2,
        # First stage — NMOS diff pair
        "Wdiff": 10e-6,
        "Ldiff": 60e-9,
        # First stage — PMOS current mirror
        "Wmirr": 5e-6,
        "Lmirr": 100e-9,
        # Second stage — PMOS common-source
        "Wcs": 20e-6,
        # Compensation
        "Cc": 500e-15,
        "Rz": 1000.0,
        # External ideal bias current into the internal NMOS diode.
        "IBIAS": 20e-6,
    }

    # ------------------------------------------------------------------
    # generate_circuit
    # ------------------------------------------------------------------
    def generate_circuit(self, params: dict[str, float] | None = None) -> str:
        """Generate the DUT .cir subcircuit netlist."""
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)

        return _CIRCUIT_TEMPLATE.format(
            spectre_include=spectre_include_line(pdk),
            nmos_model=pdk.nmos_model,
            pmos_model=pdk.pmos_model,
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
            params: Override defaults (VDD, VCM, IBIAS, CL).
            analysis_type: "ac", "sr" (or legacy "tran"), or "st".
        """
        pdk = get_pdk_profile_for_params(params)
        p = self._merge_params_with_preset(params)
        tb_defaults = self._testbench_defaults_with_preset(
            {
                "VCM": 0.7,
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
            ibias = params.get("IBIAS", params.get("I_tail", ibias))
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
    # gm/Id 模式启动后，BO 搜索的是：
    # I_tail
    # gm_id_xxx
    # L_xxx
    # ratio_load_tail
    # Cc
    # Rz
    # 然后 GmidSizer 用这个 L 去查 gm/Id table，算出 W
    # ------------------------------------------------------------------

    def get_gmid_spec(self, targets=None):
        """Return the gm/Id spec for the two-stage OTA.

        One fixed unit bias current plus integer mirror multipliers:
        - IBIAS: unit current through the diode NMOS
        - m_tail_unit: Mtail/Mbias current ratio
        - ratio_load_tail: Mload/Mtail current ratio

        Transistor roles:
        - bias_nmos (unit diode NMOS, gate/drain=ibias)
        - diff_pair_nmos (NMOS input pair, each I_tail/2)
        - mirror_pmos (PMOS current mirror load, each I_tail/2)
        - cs_pmos (second-stage PMOS CS amplifier)

        Mtail and Mload share W/L/nf with Mbias in the netlist; only m changes.
        """
        from models import DerivedBranchCurrentSpec, GmidTopologySpec, TransistorSpec
        pdk = get_pdk_profile()
        unit_current = 20e-6
        pass_through_space = self._apply_param_space_overrides(ParamSpace(params=[
            ParamDef(
                name="Cc", low=0.1e-12, high=1e-12,
                log_scale=True, unit="F",
            ),
            ParamDef(
                name="Rz", low=100, high=2e3,
                log_scale=True, unit="Ohm",
            ),
        ]))

        tail_current_low = 20e-6
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
            single_input_current = gm_required / 15.0
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
                # -- Unit NMOS diode for external current reference --
                TransistorSpec(
                    role="bias_nmos",
                    w_param="Wbias", l_param="Lbias",
                    model=pdk.nmos_model,
                    current_source="IBIAS", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=15, gm_id_default=10,
                    L_low=200e-9, L_high=600e-9, L_default=200e-9,
                    Vds_estimate=0.2,
                ),
                # -- First stage: NMOS diff pair (each I_tail/2) --
                TransistorSpec(
                    role="diff_pair_nmos",
                    w_param="Wdiff", l_param="Ldiff",
                    model=pdk.nmos_model,
                    current_source="I_tail", current_fraction=0.5,
                    gm_id_low=10, gm_id_high=20, gm_id_default=12,
                    L_low=60e-9, L_high=500e-9, L_default=60e-9,
                    Vds_estimate=0.25, Vbs=-0.3, multiplicity=2,
                ),
                # -- First stage: PMOS current mirror load (each I_tail/2) --
                TransistorSpec(
                    role="mirror_pmos",
                    w_param="Wmirr", l_param="Lmirr",
                    model=pdk.pmos_model,
                    current_source="I_tail", current_fraction=0.5,
                    gm_id_low=8, gm_id_high=15, gm_id_default=10,
                    L_low=60e-9, L_high=500e-9, L_default=100e-9,
                    Vds_estimate=0.3, multiplicity=2,
                ),
                # -- Second stage: PMOS common-source amplifier --
                TransistorSpec(
                    role="cs_pmos",
                    w_param="Wcs", l_param="Lbias",
                    model=pdk.pmos_model,
                    current_source="I_cs", current_fraction=1.0,
                    gm_id_low=8, gm_id_high=15, gm_id_default=12,
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

    # ------------------------------------------------------------------
    # get_default_params
    # ------------------------------------------------------------------
    def get_default_params(self) -> dict[str, float]:
        return self._default_params_with_preset()

    # ------------------------------------------------------------------
    # get_param_space: 这是 非 gm/Id 普通物理参数模式下的 BO 搜索范围
    # ------------------------------------------------------------------
    def get_param_space(self) -> ParamSpace:
        return self._apply_param_space_overrides(ParamSpace(
            params=[
                # --- Unit NMOS diode and current-mirror multipliers ---
                ParamDef(
                    name="Wbias", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lbias", low=200e-9, high=600e-9,
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
                # --- First stage: diff pair ---
                ParamDef(
                    name="Wdiff", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Ldiff", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- First stage: current mirror ---
                ParamDef(
                    name="Wmirr", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                ParamDef(
                    name="Lmirr", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- Second stage: PMOS CS amp ---
                ParamDef(
                    name="Wcs", low=0.5e-6, high=200e-6,
                    log_scale=True, unit="m", max_per_finger=2.6e-6,
                ),
                # --- Compensation ---
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


# ------------------------------------------------------------------
# Spectre-native templates
# ------------------------------------------------------------------

_CIRCUIT_TEMPLATE = """\
// two_stage_ota.cir -- Two-Stage Miller OTA (Spectre native syntax)
simulator lang=spectre insensitive=yes

{spectre_include}

parameters Wbias={Wbias} Lbias={Lbias} m_tail_unit={m_tail_unit} ratio_load_tail={ratio_load_tail}
parameters m_load_unit=m_tail_unit*ratio_load_tail
parameters Wdiff={Wdiff} Ldiff={Ldiff}
parameters Wmirr={Wmirr} Lmirr={Lmirr} Wcs={Wcs}
parameters Cc={Cc} Rz={Rz}

subckt two_stage_ota (vip vin vout ibias vdd vss)
// Bias generator: external current into diode-connected NMOS
Mbias (ibias ibias vss vss) {nmos_model} w=Wbias l=Lbias nf=1
// First stage: NMOS differential pair
Mdiff1 (n_mirr vin n_tail vss) {nmos_model} w=Wdiff l=Ldiff nf=1
Mdiff2 (n_s1 vip n_tail vss) {nmos_model} w=Wdiff l=Ldiff nf=1
// First stage: PMOS current mirror load
Mmirr1 (n_mirr n_mirr vdd vdd) {pmos_model} w=Wmirr l=Lmirr nf=1
Mmirr2 (n_s1 n_mirr vdd vdd) {pmos_model} w=Wmirr l=Lmirr nf=1
// First stage: NMOS tail current source
Mtail (n_tail ibias vss vss) {nmos_model} w=Wbias l=Lbias nf=1 m=m_tail_unit
// Second stage
Mcs (vout n_s1 vdd vdd) {pmos_model} w=Wcs l=Lbias nf=1
Mload (vout ibias vss vss) {nmos_model} w=Wbias l=Lbias nf=1 m=m_load_unit
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

parameters VDD={VDD} VCM={VCM} IBIAS={IBIAS} CL={CL}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (vdd ibias) isource type=dc dc=IBIAS
VCMsrc (vcm 0) vsource type=dc dc=VCM
VIPsrc (vinp vcm) vsource type=dc dc=0 mag=1
Rfb (vout vinn) resistor r=1G
Cfb (vinn 0) capacitor c=1

Xdut (vinp vinn vout ibias vdd vss) two_stage_ota
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
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

parameters VDD={VDD} VCM={VCM} IBIAS={IBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (vdd ibias) isource type=dc dc=IBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=2n rise=100p fall=100p width=50n period=100n
VFBsrc (vin vout) vsource type=dc dc=0

Xdut (vinp vin vout ibias vdd vss) two_stage_ota
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
srTran tran stop=200n maxstep=10p

save vinp vout
"""

_TB_ST_TEMPLATE = """\
// tb_two_stage_ota_st.scs -- Unity-gain 0.1% settling-time analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} IBIAS={IBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (vdd ibias) isource type=dc dc=IBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=5n rise=100p fall=100p width=80n period=160n
VFBsrc (vin vout) vsource type=dc dc=0

Xdut (vinp vin vout ibias vdd vss) two_stage_ota
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
stTran tran stop=180n maxstep=10p

save vinp vout
"""


# ------------------------------------------------------------------
# internal helpers
# ------------------------------------------------------------------

def _fmt(value: float) -> str:
    """Format a float with SPICE engineering suffix (u, n, p, f, k)."""
    return format_spice_value(value)
