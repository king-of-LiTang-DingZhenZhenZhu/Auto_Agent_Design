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


def _bias_w(name: str) -> ParamDef:
    return ParamDef(
        name=name,
        low=0.2e-6,
        high=5e-6,
        log_scale=True,
        unit="m",
        max_per_finger=5e-6,
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
    and is intended for high-gain, high-bandwidth targets.
    """

    meta = TopologyMeta(
        name="folded_cascode",
        display_name="Folded-Cascode OTA",
        description=(
            "Two-stage OTA with a PMOS-input folded-cascode first stage, "
            "PMOS common-source second stage, and Miller compensation. "
            "High gain (60-85 dB), higher bandwidth than a basic two-stage OTA."
        ),
        min_gain_db=60,
        max_gain_db=85,
        min_bw_hz=1e6,
        max_bw_hz=1e9,
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
        "Lcs": 120e-9,
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
            Lcs=_fmt(p["Lcs"]),
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
        """Generate the testbench .sp file."""
        vdd = 1.0
        vcm = 0.45
        ibias = 20e-6
        cload = 1e-12

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
                VHIGH=vcm + 0.2,
                VLOW=vcm - 0.2,
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

    def get_default_params(self) -> dict[str, float]:
        return dict(self.DEFAULT_PARAMS)

    def get_param_space(self) -> ParamSpace:
        return ParamSpace(
            params=[
                ParamDef(
                    name="Wtailp", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Ltailp", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wdiffp", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Ldiffp", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wfoldn", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lfoldn", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wcasn", low=0.5e-6, high=80e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lcasn", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wmirrp", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lmirrp", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wcasp", low=0.5e-6, high=100e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lcasp", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wcs", low=0.5e-6, high=150e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lcs", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wload", low=0.5e-6, high=150e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lload", low=30e-9, high=900e-9,
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


_CIRCUIT_TEMPLATE = """\
* folded_cascode.cir -- Folded-Cascode Miller OTA (Python-generated)
* First stage: PMOS input pair + NMOS folded cascodes + PMOS cascode mirror
* Second stage: PMOS common-source + NMOS current-source load
* Compensation: Cc + Rz (Miller with nulling resistor)
.lib '/PDKS/TSMC28nm/models/hspice/toplevel.l' TOP_TT
.options redefinedparams=ignore
.param Wtailp={Wtailp} Ltailp={Ltailp} Wdiffp={Wdiffp} Ldiffp={Ldiffp}
.param Wfoldn={Wfoldn} Lfoldn={Lfoldn} Wcasn={Wcasn} Lcasn={Lcasn}
.param Wmirrp={Wmirrp} Lmirrp={Lmirrp} Wcasp={Wcasp} Lcasp={Lcasp}
.param Wcs={Wcs} Lcs={Lcs} Wload={Wload} Lload={Lload}
.param Wbp_big={Wbp_big} Lbp_big={Lbp_big} Wbp_small={Wbp_small} Lbp_small={Lbp_small}
.param Wbn_big={Wbn_big} Lbn_big={Lbn_big} Wbn_small={Wbn_small} Lbn_small={Lbn_small}
.param Cc={Cc} Rz={Rz}

.subckt folded_cascode vip vin vout Iref vdd vss
* --- Bias ---
M7 VB1 VB2 net4 vdd pch_lvt_mac W='Wbp_big' L='Lbp_big' nf=1
M6 net4 VB1 vdd vdd pch_lvt_mac W='Wbp_big' L='Lbp_big' nf=1
M4 VB2 VB2 vdd vdd pch_lvt_mac W='Wbp_small' L='Lbp_small' nf=1
M2 VB4 Iref vdd vdd pch_lvt_mac W='Wbp_big' L='Lbp_big' nf=1
M1 VB3 Iref vdd vdd pch_lvt_mac W='Wbp_big' L='Lbp_big' nf=1
M0 Iref Iref vdd vdd pch_lvt_mac W='Wbp_big' L='Lbp_big' nf=1
M13 net6 VB4 vss vss nch_lvt_mac W='Wbn_big' L='Lbn_big' nf=1
M12 VB1 VB3 net6 vss nch_lvt_mac W='Wbn_big' L='Lbn_big' nf=1
M11 net2 VB4 vss vss nch_lvt_mac W='Wbn_big' L='Lbn_big' nf=1
M10 VB2 VB3 net2 vss nch_lvt_mac W='Wbn_big' L='Lbn_big' nf=1
M9 net3 VB4 vss vss nch_lvt_mac W='Wbn_big' L='Lbn_big' nf=1
M8 VB4 VB3 net3 vss nch_lvt_mac W='Wbn_big' L='Lbn_big' nf=1
M5 VB3 VB3 vss vss nch_lvt_mac W='Wbn_small' L='Lbn_small' nf=1

* --- PMOS input differential pair ---
Mtailp ntail VB1 vdd vdd pch_lvt_mac W='Wtailp' L='Ltailp' nf=1
Mdiff1 nfold_l vip ntail vdd pch_lvt_mac W='Wdiffp' L='Ldiffp' nf=1
Mdiff2 nfold_r vin ntail vdd pch_lvt_mac W='Wdiffp' L='Ldiffp' nf=1

* --- NMOS folded branches and common-gate cascodes ---
Mfold1 nfold_l VB4 vss vss nch_lvt_mac W='Wfoldn' L='Lfoldn' nf=1
Mfold2 nfold_r VB4 vss vss nch_lvt_mac W='Wfoldn' L='Lfoldn' nf=1
Mcasn1 pmirr VB3 nfold_l vss nch_lvt_mac W='Wcasn' L='Lcasn' nf=1
Mcasn2 nstage1 VB3 nfold_r vss nch_lvt_mac W='Wcasn' L='Lcasn' nf=1

* --- PMOS Low Voltage cascode current mirror load ---
Mmirr1 npm_l pmirr vdd vdd pch_lvt_mac W='Wmirrp' L='Lmirrp' nf=1
Mmirr2 npm_r pmirr vdd vdd pch_lvt_mac W='Wmirrp' L='Lmirrp' nf=1
Mcasp1 pmirr VB2 npm_l vdd pch_lvt_mac W='Wcasp' L='Lcasp' nf=1
Mcasp2 nstage1 VB2 npm_r vdd pch_lvt_mac W='Wcasp' L='Lcasp' nf=1

* --- Second Stage: PMOS common-source amplifier + NMOS load ---
Mcs vout nstage1 vdd vdd pch_lvt_mac W='Wcs' L='Lcs' nf=1
Mload vout VB4 vss vss nch_lvt_mac W='Wload' L='Lload' nf=1

* --- Miller compensation: Rz in series with Cc ---
Rz nstage1 n_rz R='Rz'
Cc n_rz vout C='Cc'
.ends folded_cascode
"""

_TB_AC_TEMPLATE = """\
* tb_folded_cascode_ac.sp -- Folded-Cascode OTA AC Analysis
.include "circuit.cir"

* --- Power supply ---
VDD vdd 0 DC {VDD}
VSS vss 0 DC 0
Iibias Iref vss DC {IBIAS}

* --- Input stimulus ---
Vcm vcm 0 DC {VCM}
Vinp vinp vcm DC 0 AC 1
Vinn vinn 0  DC 0

* --- Closed-loop feedback for DC stability ---
Rfb vout vinn 1G
Cfb vinn 0 1

* --- DUT ---
Xdut vinp vinn vout ibias vdd vss folded_cascode
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
* tb_folded_cascode_tran.sp -- Folded-Cascode OTA Transient Analysis
.include "circuit.cir"

* --- Power supply ---
VDD vdd 0 DC {VDD}
VSS vss 0 DC 0
Iibias Iref vss DC {IBIAS}

* --- Unity-gain buffer: vout feeds back to vin ---
Vcm vcm 0 DC {VCM}
Vinp vinp vcm DC 0 PULSE({VLOW} {VHIGH} 2n 100p 100p 50n 100n)

* --- Feedback (buffer) ---
Vfb vin vout DC 0

* --- DUT ---
Xdut vinp vin vout ibias vdd vss folded_cascode
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
