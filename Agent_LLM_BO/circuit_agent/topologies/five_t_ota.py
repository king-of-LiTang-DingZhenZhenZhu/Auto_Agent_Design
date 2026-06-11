"""5-Transistor OTA — PMOS-input single-stage operational transconductance amplifier.

Reference: Agent_LLM_BO/Spice_Scripts/Examples/5t_ota/5t_ota.cir

Topology:
    M5 (PMOS tail) sources current from VDD into the "tail" node.
    M1/M2 (PMOS diff pair) steer current to lout / vout.
    M3/M4 (NMOS current mirror) act as active load.
"""

from __future__ import annotations

from topologies.base import BaseTopology, TopologyMeta
from models import ParamDef, ParamSpace


class FiveTOTA(BaseTopology):
    """5-Transistor OTA — PMOS diff pair + NMOS current-mirror load."""

    meta = TopologyMeta(
        name="5t_ota",
        display_name="5-Transistor OTA",
        description=(
            "Single-stage OTA with PMOS differential pair and NMOS "
            "current-mirror load.  Moderate gain (30-50 dB), high bandwidth."
        ),
        min_gain_db=25,
        max_gain_db=55,
        min_bw_hz=1e6,
        max_bw_hz=2e9,
        typical_power_w=500e-6,
        complexity=1,
        escalation="two_stage_ota",
    )

    # ------------------------------------------------------------------
    # Default parameters (SI units, rendered with SPICE suffixes)
    # ------------------------------------------------------------------
    DEFAULT_PARAMS: dict[str, float] = {
        "Wtail": 3e-6,
        "Ltail": 200e-9,
        "Wdp": 5e-6,
        "Ldp": 60e-9,
        "Wcm": 8e-6,
        "Lcm": 100e-9,
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
            Wdp=_fmt(p["Wdp"]),
            Ldp=_fmt(p["Ldp"]),
            Wcm=_fmt(p["Wcm"]),
            Lcm=_fmt(p["Lcm"]),
        )

    # ------------------------------------------------------------------
    # generate_testbench
    # ------------------------------------------------------------------
    def generate_testbench(
        self,
        params: dict[str, float] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        """Generate the AC testbench .sp file (closed-loop method)."""
        # Bias / supply defaults
        vdd = 0.9
        vbias = 0.5
        cload = 500e-15

        if params:
            vdd = params.get("VDD", vdd)
            vbias = params.get("VBIAS", vbias)
            cload = params.get("CL", cload)

        vcm = vdd / 2.0

        return _TB_TEMPLATE.format(
            VDD=vdd,
            VCM=vcm,
            VBIAS=vbias,
            CL=_fmt(cload),
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
                ParamDef(
                    name="Wtail", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Ltail", low=30e-9, high=1e-6,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wdp", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Ldp", low=30e-9, high=1e-6,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wcm", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lcm", low=30e-9, high=1e-6,
                    log_scale=True, unit="m",
                ),
            ]
        )


# ------------------------------------------------------------------
# SPICE templates (module-level constants)
# ------------------------------------------------------------------

_CIRCUIT_TEMPLATE = """\
* 5t_ota.cir -- Five-Transistor OTA (Python-generated, PMOS-input)
.lib '/PDKS/TSMC28nm/models/hspice/toplevel.l' TOP_TT
.options redefinedparams=ignore
.param Wtail={Wtail} Ltail={Ltail} Wdp={Wdp} Ldp={Ldp} Wcm={Wcm} Lcm={Lcm}

.subckt ota_5t vip vin vout vbias vdd vss
* --- Tail current source (PMOS) ---
Mtail tail vbias vdd vdd pch_mac W='Wtail' L='Ltail' nf=1
* --- Differential pair (PMOS) ---
Mdp1 lout vip tail vdd pch_mac W='Wdp' L='Ldp' nf=1
Mdp2 vout vin tail vdd pch_mac W='Wdp' L='Ldp' nf=1
* --- Active load / current mirror (NMOS) ---
Mcm1 lout lout vss vss nch_mac W='Wcm' L='Lcm' nf=1
Mcm2 vout lout vss vss nch_mac W='Wcm' L='Lcm' nf=1
.ends ota_5t
"""

_TB_TEMPLATE = """\
* tb_ota_ac.sp -- 5T OTA AC Analysis (Closed-Loop Method, Python-generated)
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
Xdut vinp vinn vout vbias vdd vss ota_5t
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


# ------------------------------------------------------------------
# internal helpers
# ------------------------------------------------------------------

def _fmt(value: float) -> str:
    """Format a float with SPICE engineering suffix (u, n, p, f)."""
    abs_v = abs(value)
    if abs_v >= 1e-3:
        return f"{value:.6g}"
    elif abs_v >= 1e-6:
        return f"{value * 1e6:.6g}u"
    elif abs_v >= 1e-9:
        return f"{value * 1e9:.6g}n"
    elif abs_v >= 1e-12:
        return f"{value * 1e12:.6g}p"
    else:
        return f"{value * 1e15:.6g}f"
