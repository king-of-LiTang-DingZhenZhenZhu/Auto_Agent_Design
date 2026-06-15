"""5-Transistor OTA — PMOS-input single-stage operational transconductance amplifier.

Reference: Agent_LLM_BO/Spice_Scripts/Examples/5t_ota/5t_ota.cir

Topology:
    M5 (PMOS tail) sources current from VDD into the "tail" node.
    M1/M2 (PMOS diff pair) steer current to lout / vout.
    M3/M4 (NMOS current mirror) act as active load.
"""

from __future__ import annotations

import math

from topologies.base import BaseTopology, TopologyMeta
from models import CircuitFiles, ParamDef, ParamSpace


class FiveTOTA(BaseTopology):
    """5-Transistor OTA — PMOS diff pair + NMOS current-mirror load."""

    meta = TopologyMeta(
        name="5t_ota",
        display_name="5-Transistor OTA",
        description=(
            "Single-stage OTA with PMOS differential pair and NMOS "
            "current-mirror load.  Moderate gain (30-50 dB), high GBW."
        ),
        min_gain_db=25,
        max_gain_db=55,
        min_gbw_hz=1e6,
        max_gbw_hz=2e9,
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
        "Ldp": 130e-9,
        "Wcm": 8e-6,
        "Lcm": 130e-9,
        "VBIAS": 0.35,
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
        """Generate AC, slew-rate, or settling-time Spectre testbench."""
        # Bias / supply defaults
        vdd = 0.9
        cload = 500e-15

        if params:
            vdd = params.get("VDD", vdd)
            cload = params.get("CL", cload)
        vbias = (params or {}).get("VBIAS", self.DEFAULT_PARAMS["VBIAS"])

        # PMOS input needs VCM near VSS for adequate Vsg headroom
        vcm = vdd-0.75  # VDD=0.9V, PMOS input, VCM≈0.15V leaves Vsg≈0.6V

        if analysis_type in ("tran", "sr"):
            return _TB_SR_TEMPLATE.format(
                VDD=vdd, VCM=vcm, VBIAS=vbias, CL=_fmt(cload),
                VLOW=0, VHIGH=vcm + 0.15,
            )
        if analysis_type == "st":
            return _TB_ST_TEMPLATE.format(
                VDD=vdd, VCM=vcm, VBIAS=vbias, CL=_fmt(cload),
                VLOW=vcm, VHIGH=vcm + 10e-3,
            )
        return _TB_AC_TEMPLATE.format(
            VDD=vdd,
            VCM=vcm,
            VBIAS=vbias,
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

    # ------------------------------------------------------------------
    # gm/Id support
    # ------------------------------------------------------------------

    def get_gmid_spec(self, targets=None):
        """Return gm/Id spec for 5T OTA with a GBW-derived current lower bound.

        The topology has one branch current (I_tail) driving:
        - PMOS tail current source
        - PMOS diff pair (each side carries I_tail/2)
        - NMOS current mirror load (each side carries I_tail/2)

        VBIAS is derived from the tail PMOS lookup VGS and is not searched by BO.
        """
        from models import (
            BranchCurrentSpec,
            DerivedGateBiasSpec,
            GmidTopologySpec,
            TransistorSpec,
        )

        tail_current_low = 1e-6
        tail_current_high = 200e-6
        if (
            targets is not None
            and targets.bandwidth_hz is not None
            and targets.load_cap_f is not None
            and targets.bandwidth_hz > 0
            and targets.load_cap_f > 0
        ):
            gm_required = 2.0 * math.pi * targets.bandwidth_hz * targets.load_cap_f
            max_input_gmid = 24.0
            input_current_fraction = 0.5
            derived_min = gm_required / (max_input_gmid * input_current_fraction)
            tail_current_low = max(tail_current_low, derived_min)
            if tail_current_low > tail_current_high:
                raise ValueError(
                    "5T OTA GBW/CL target requires I_tail >= "
                    f"{tail_current_low:.3e} A, above the configured "
                    f"{tail_current_high:.3e} A upper bound"
                )

        return GmidTopologySpec(
            branch_currents=[
                BranchCurrentSpec(
                    name="I_tail",
                    low=tail_current_low,
                    high=tail_current_high,
                    default=max(40e-6, tail_current_low),
                ),
            ],
            transistors=[
                # -- PMOS tail current source (W ≤ 2.7µm/finger) --
                TransistorSpec(
                    role="tail_pmos",
                    w_param="Wtail", l_param="Ltail",
                    model="pch_mac",
                    current_source="I_tail", current_fraction=1.0,
                    gm_id_low=5, gm_id_high=22, gm_id_default=14,
                    L_low=120e-9, L_high=900e-9, L_default=200e-9,
                    Vds_estimate=0.2, max_per_finger=2.7e-6,
                ),
                # -- PMOS diff pair (each side carries I_tail / 2) --
                TransistorSpec(
                    role="diff_pair_pmos",
                    w_param="Wdp", l_param="Ldp",
                    model="pch_mac",
                    current_source="I_tail", current_fraction=0.5,
                    gm_id_low=10, gm_id_high=24, gm_id_default=18,
                    L_low=120e-9, L_high=500e-9, L_default=120e-9,
                    Vds_estimate=0.25, multiplicity=2, max_per_finger=2.7e-6,
                ),
                # -- NMOS current mirror load (each side carries I_tail / 2) --
                TransistorSpec(
                    role="mirror_nmos",
                    w_param="Wcm", l_param="Lcm",
                    model="nch_mac",
                    current_source="I_tail", current_fraction=0.5,
                    gm_id_low=8, gm_id_high=24, gm_id_default=18,
                    L_low=120e-9, L_high=500e-9, L_default=120e-9,
                    Vds_estimate=0.35, multiplicity=2, max_per_finger=2.7e-6,
                ),
            ],
            derived_gate_biases=[
                DerivedGateBiasSpec(
                    role="tail_pmos",
                    param_name="VBIAS",
                    supply_voltage=0.9,
                    low=0.05,
                    high=0.85,
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
                ParamDef(
                    name="Wtail", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Ltail", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wdp", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Ldp", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
                ParamDef(
                    name="Wcm", low=0.5e-6, high=50e-6,
                    log_scale=True, unit="m", max_per_finger=3e-6,
                ),
                ParamDef(
                    name="Lcm", low=30e-9, high=900e-9,
                    log_scale=True, unit="m",
                ),
            ]
        )


# ------------------------------------------------------------------
# Spectre-native templates (module-level constants)
# ------------------------------------------------------------------

_CIRCUIT_TEMPLATE = """\
// 5t_ota.cir -- Five-Transistor OTA (Spectre native syntax)
simulator lang=spectre insensitive=yes

include "/PDKS/TSMC28nm/models/spectre/toplevel.scs" section=top_tt

parameters Wtail={Wtail} Ltail={Ltail}
parameters Wdp={Wdp} Ldp={Ldp}
parameters Wcm={Wcm} Lcm={Lcm}
subckt ota_5t (vip vin vout vbias vdd vss)
// Tail current source (PMOS)
Mtail (tail vbias vdd vdd) pch_mac w=Wtail l=Ltail nf=1
// Differential pair (PMOS)
Mdp1 (lout vip tail vdd) pch_mac w=Wdp l=Ldp nf=1
Mdp2 (vout vin tail vdd) pch_mac w=Wdp l=Ldp nf=1
// Active load / current mirror (NMOS)
Mcm1 (lout lout vss vss) nch_mac w=Wcm l=Lcm nf=1
Mcm2 (vout lout vss vss) nch_mac w=Wcm l=Lcm nf=1
ends ota_5t
"""

_TB_AC_TEMPLATE = """\
// tb_ota_ac.scs -- 5T OTA differential AC analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} VBIAS={VBIAS} CL={CL}

// Power supply and bias
VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
VBIASsrc (vbias 0) vsource type=dc dc=VBIAS

// Original closed-loop AC stimulus and feedback network
VCMsrc (vcm 0) vsource type=dc dc=VCM
VIPsrc (vinp vcm) vsource type=dc dc=0 mag=1
Rfb (vout vinn) resistor r=1G
Cfb (vinn 0) capacitor c=1

// DUT and load
Xdut (vinp vinn vout vbias vdd vss) ota_5t
CLload (vout 0) capacitor c=CL

// Analyses
tempOption options temp=27
outOpts options rawfmt=psfascii
op1 dc oppoint=rawfile
opInfo info what=oppoint where=rawfile
ac1 ac start=1 stop=10G dec=20

save vout
save VDDsrc:p
"""

_TB_SR_TEMPLATE = """\
// tb_5t_ota_sr.scs -- Unity-gain large-signal slew-rate analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} VBIAS={VBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
VBIASsrc (vbias 0) vsource type=dc dc=VBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=2n rise=100p fall=100p width=50n period=100n
VFBsrc (vinn vout) vsource type=dc dc=0

Xdut (vinp vinn vout vbias vdd vss) ota_5t
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii
srTran tran stop=120n maxstep=10p

save vinp vout
"""

_TB_ST_TEMPLATE = """\
// tb_5t_ota_st.scs -- Unity-gain 0.1% settling-time analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} VBIAS={VBIAS} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=dc dc=VDD
VSSsrc (vss 0) vsource type=dc dc=0
VBIASsrc (vbias 0) vsource type=dc dc=VBIAS
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=5n rise=100p fall=100p width=50n period=100n
VFBsrc (vinn vout) vsource type=dc dc=0

Xdut (vinp vinn vout vbias vdd vss) ota_5t
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii
stTran tran stop=120n maxstep=10p

save vinp vout
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
