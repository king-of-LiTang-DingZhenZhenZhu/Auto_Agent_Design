"""Two-Stage Miller-Compensated OTA — NMOS-input + PMOS common-source second stage.

Reference: /Desktop/Knowleage_Base/01-电路拓扑/两级密勒补偿运放.md

Topology (方案A — 五管OTA第一级 + 共源第二级):

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
            "compensation (Cc + Rz).  High gain (55-85 dB), moderate bandwidth."
        ),
        min_gain_db=50,
        max_gain_db=90,
        min_bw_hz=1e5,
        max_bw_hz=5e8,
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
        """Generate the testbench .sp file.

        Args:
            params: Override defaults (VDD, VCM, VBIAS, CL).
            analysis_type: "ac" → AC closed-loop; "tran" → unity-gain buffer.
        """
        vdd = 1.0       # NMOS input needs ~0.75V ICMR min → VDD=1.0V
        vcm = 0.6
        vbias = 0.5     # shared bias for M5 (tail) and M7 (load)
        cload = 2e-12   # 2 pF (typical for ADC driver)

        if params:
            vdd = params.get("VDD", vdd)
            vcm = params.get("VCM", vcm)
            vbias = params.get("VBIAS", vbias)
            cload = params.get("CL", cload)

        if analysis_type == "tran":
            return _TB_TRAN_TEMPLATE.format(
                VDD=vdd,
                VCM=vcm,
                VBIAS=vbias,
                CL=_fmt(cload),
                VHIGH=vcm + 0.2,
                VLOW=vcm - 0.2,
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
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Ltail", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- First stage: diff pair ---
                ParamDef(
                    name="Wdiff", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Ldiff", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- First stage: current mirror ---
                ParamDef(
                    name="Wmirr", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lmirr", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- Second stage: PMOS CS amp ---
                ParamDef(
                    name="Wcs", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lcs", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- Second stage: NMOS load ---
                ParamDef(
                    name="Wload", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lload", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                # --- Compensation ---
                ParamDef(
                    name="Cc", low=0.01e-12, high=10e-12,
                    log_scale=True, unit="F",
                ),
                ParamDef(
                    name="Rz", low=1.0, high=100e3,
                    log_scale=True, unit="Ohm",
                ),
            ]
        )


# ------------------------------------------------------------------
# SPICE templates
# ------------------------------------------------------------------

_CIRCUIT_TEMPLATE = """\
* two_stage_ota.cir — Two-Stage Miller OTA (Python-generated, NMOS-input)
* First stage: NMOS diff pair + PMOS current mirror (5T OTA)
* Second stage: PMOS common-source + NMOS current-source load
* Compensation: Cc + Rz (Miller with nulling resistor)
.lib '/PDKS/TSMC28nm/models/hspice/toplevel.l' TOP_TT
.options redefinedparams=ignore
.param Wtail={Wtail} Ltail={Ltail} Wdiff={Wdiff} Ldiff={Ldiff}
.param Wmirr={Wmirr} Lmirr={Lmirr} Wcs={Wcs} Lcs={Lcs}
.param Wload={Wload} Lload={Lload} Cc={Cc} Rz={Rz}

.subckt two_stage_ota vip vin vout vb vdd vss
* --- First Stage: NMOS differential pair ---
Mdiff1 n_mirr vip n_tail vss nch_mac W='Wdiff' L='Ldiff' nf=1
Mdiff2 n_s1   vin n_tail vss nch_mac W='Wdiff' L='Ldiff' nf=1
* --- First Stage: PMOS current mirror load ---
Mmirr1 n_mirr n_mirr vdd vdd pch_mac W='Wmirr' L='Lmirr' nf=1
Mmirr2 n_s1   n_mirr vdd vdd pch_mac W='Wmirr' L='Lmirr' nf=1
* --- First Stage: NMOS tail current source ---
Mtail n_tail vb vss vss nch_mac W='Wtail' L='Ltail' nf=1
* --- Second Stage: PMOS common-source amplifier ---
Mcs vout n_s1 vdd vdd pch_mac W='Wcs' L='Lcs' nf=1
* --- Second Stage: NMOS current-source load ---
Mload vout vb vss vss nch_mac W='Wload' L='Lload' nf=1
* --- Miller compensation: Rz in series with Cc ---
Rz n_s1 n_rz R='Rz'
Cc n_rz vout C='Cc'
.ends two_stage_ota
"""

# Closed-loop AC testbench (same method as 5T OTA)
_TB_AC_TEMPLATE = """\
* tb_two_stage_ac.sp — Two-Stage OTA AC Analysis (Closed-Loop Method)
.include "circuit.cir"

* --- Power supply ---
VDD vdd 0 DC {VDD}
VSS vss 0 DC 0
Vbias vbias 0 DC {VBIAS}

* --- Input stimulus ---
Vcm vcm 0 DC {VCM}
Vinp vinp vcm DC 0 AC 1
Vinn vinn 0  DC 0

* --- Closed-loop feedback for DC stability ---
Rfb vout vinn 1G
Cfb vinn 0 1

* --- DUT ---
Xdut vinp vinn vout vbias vdd vss two_stage_ota
CL vout 0 {CL}

* --- Analysis ---
.op
.ac dec 20 1 10g
.temp 27

* --- Measurements ---
.meas ac gain_dc find vdb(vout) at=1k
.meas ac phase_dc find vp(vout) at=1k
.meas ac gbw_hz when vdb(vout)=0 cross=1
.meas ac phase_at_ugf find vp(vout) when vdb(vout)=0 cross=1
.meas dc power_total PARAM='-I(Vdd)*{VDD}'

.end
"""

# Unity-gain buffer transient testbench (slew rate + settling time)
_TB_TRAN_TEMPLATE = """\
* tb_two_stage_tran.sp — Two-Stage OTA Transient Analysis (Unity-Gain Buffer)
.include "circuit.cir"

* --- Power supply ---
VDD vdd 0 DC {VDD}
VSS vss 0 DC 0
Vbias vbias 0 DC {VBIAS}

* --- Unity-gain buffer: vout feeds back to vin ---
Vcm vcm 0 DC {VCM}
Vinp vinp vcm DC 0 PULSE({VLOW} {VHIGH} 2n 100p 100p 50n 100n)

* --- Feedback (buffer) ---
Vfb vin vout DC 0

* --- DUT ---
Xdut vinp vin vout vbias vdd vss two_stage_ota
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
