"""Banba et al. current-summing CMOS bandgap for sub-1-V operation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from models import DesignTarget, ParamDef, ParamSpace, format_spice_value
from pdk_integration.profiles import get_pdk_profile_for_params, spectre_include_line
from system_decomposition import SystemDesignRequest, decompose_bandgap
from topologies.references.bandgap_ptat import BandgapPTAT
from topologies.base import ExecutableChildSpec, PassiveImplementation, TopologyMeta
from topologies.amplifiers.two_stage_ota import TwoStageOTA


class BanbaSub1VBandgap(BandgapPTAT):
    """Current-mode low-voltage BGR from Banba et al., JSSC May 1999."""

    PASSIVE_IMPLEMENTATIONS = (
        PassiveImplementation("R1dev", "resistor", "bandgap_resistor"),
        PassiveImplementation("R2dev", "resistor", "bandgap_resistor"),
        PassiveImplementation("R3dev", "resistor", "bandgap_resistor"),
        PassiveImplementation("R4dev", "resistor", "bandgap_resistor"),
        PassiveImplementation("RSTART_BIAS", "resistor", "startup_resistor"),
        PassiveImplementation("RRS_TOP", "resistor", "startup_resistor"),
        PassiveImplementation("RRS_BOTTOM", "resistor", "startup_resistor"),
        PassiveImplementation("C1dev", "capacitor", "compensation_capacitor"),
        PassiveImplementation("C2dev", "capacitor", "compensation_capacitor"),
        PassiveImplementation("CloadDev", "capacitor", "load_capacitor", "external"),
    )

    STARTUP_INTERNAL_SAVES = "save Xdut.vrs Xdut.sup Xdut.cmp_out"

    meta = TopologyMeta(
        name="banba_sub1v_bandgap",
        display_name="Banba Sub-1-V CMOS Bandgap",
        description=(
            "Current-summing CMOS bandgap with equal PMOS branches, a 1:N "
            "PNP pair, and independent PTAT/CTAT resistor scaling."
        ),
        min_gain_db=0,
        max_gain_db=0,
        min_gbw_hz=0,
        max_gbw_hz=0,
        typical_power_w=10e-6,
        complexity=5,
    )

    DEFAULT_PARAMS: dict[str, float] = {
        # R1 and R2 are constrained to the same R12.  For N=8, the first-order
        # zero-TC condition with dVBE/dT ~= -2 mV/C gives R12/R3 ~= 11.16.
        "R12": 2.063e6,
        "PTAT_WEIGHT": 11.1612,
        # R4/R12 ~= 0.412 gives about 0.515 V for VBE(27 C) ~= 0.65 V.
        "VREF_SCALE": 0.412,
        "DIODE_AREA_RATIO": 8,
        # Equal-size P1/P2/P3 current-mirror devices.
        "Wmirror_p": 6e-6,
        "Lmirror_p": 600e-9,
        # Frozen NMOS-input error-amplifier bias.
        "Iopbias": 2.2e-6,
        # Fig. 5 loop capacitor and compensation for the frozen OTA macro.
        "C1": 2e-12,
        "C2": 20e-12,
        # Boni circuit III: IX > IY plus VREF-detected automatic shutoff.
        "Wstart_y": 300e-9,
        "Wstart_x": 600e-9,
        "Lstart": 300e-9,
        "Rstart_ref": 100e3,
        "Rstart_bias": 100e3,
        "Wstart_bias_p": 1e-6,
        "Wstart_cmp_p": 1e-6,
        "Wstart_cmp_n": 500e-9,
        "Wstart_inv_p": 1e-6,
        "Wstart_inv_n": 500e-9,
        "Lstart_cmp": 300e-9,
        "Cload": 100e-15,
    }

    def generate_circuit(self, params: dict[str, Any] | None = None) -> str:
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)
        opamp_netlist = self._load_nmos_opamp_netlist(params)

        return _CIRCUIT_TEMPLATE.format(
            spectre_include=spectre_include_line(pdk),
            pmos_lvt_model=pdk.pmos_lvt_model,
            nmos_lvt_model=pdk.nmos_lvt_model,
            pnp_model=pdk.resolve_model("pnp"),
            R12=_fmt(p["R12"]),
            PTAT_WEIGHT=_fmt(p["PTAT_WEIGHT"]),
            VREF_SCALE=_fmt(p["VREF_SCALE"]),
            DIODE_AREA_RATIO=int(round(p["DIODE_AREA_RATIO"])),
            Wmirror_p=_fmt(p["Wmirror_p"]),
            Lmirror_p=_fmt(p["Lmirror_p"]),
            Iopbias=_fmt(p["Iopbias"]),
            C1=_fmt(p["C1"]),
            C2=_fmt(p["C2"]),
            Wstart_y=_fmt(p["Wstart_y"]),
            Wstart_x=_fmt(p["Wstart_x"]),
            Lstart=_fmt(p["Lstart"]),
            Rstart_ref=_fmt(p["Rstart_ref"]),
            Rstart_bias=_fmt(p["Rstart_bias"]),
            Wstart_bias_p=_fmt(p["Wstart_bias_p"]),
            Wstart_cmp_p=_fmt(p["Wstart_cmp_p"]),
            Wstart_cmp_n=_fmt(p["Wstart_cmp_n"]),
            Wstart_inv_p=_fmt(p["Wstart_inv_p"]),
            Wstart_inv_n=_fmt(p["Wstart_inv_n"]),
            Lstart_cmp=_fmt(p["Lstart_cmp"]),
            Cload=_fmt(p["Cload"]),
            opamp_netlist=opamp_netlist,
        )

    def generate_testbench(
        self,
        params: dict[str, Any] | None = None,
        analysis_type: str = "startup",
    ) -> str:
        """Reuse the established bandgap measurement contract."""
        testbench = super().generate_testbench(params, analysis_type).replace(
            "bandgap_ptat", self.meta.name
        )
        if analysis_type in ("startup", "tran", "sr"):
            testbench = testbench.replace(
                "width=20u period=40u", "width=200u period=400u"
            ).replace(
                "stop=10u maxstep=10n", "stop=100u maxstep=50n"
            )
        if analysis_type in ("temperature", "temp", "nonlinearity"):
            pdk = get_pdk_profile_for_params(params)
            temperatures = pdk.pvt_temperatures_c or (-40.0, 27.0, 125.0)
            low = min(temperatures)
            high = max(temperatures)
            testbench = testbench.replace(
                f"start={low} stop={high} step=1",
                f"start={high} stop={low} step=-1",
            )
        return testbench

    def get_default_params(self) -> dict[str, float]:
        return self._default_params_with_preset()

    def get_param_space(self) -> ParamSpace:
        return self._apply_param_space_overrides(ParamSpace(params=[
            ParamDef("R12", low=0.5e6, high=5e6, log_scale=True, unit="Ohm"),
            ParamDef(
                "PTAT_WEIGHT", low=8.0, high=15.0,
                log_scale=False, unit="x",
            ),
            ParamDef(
                "VREF_SCALE", low=0.3, high=0.5,
                log_scale=False, unit="x",
            ),
            ParamDef(
                "Lmirror_p", low=300e-9, high=1e-6,
                log_scale=True, unit="m",
            ),
        ]))

    def get_gmid_spec(self, targets: DesignTarget | None = None):
        """Use the explicit paper-level resistor/mirror search space."""
        return None

    def required_model_roles(self) -> tuple[str, ...]:
        return ("nmos", "pmos", "nmos_lvt", "pmos_lvt", "pnp")

    def critical_operating_point_instances(self) -> set[str]:
        return {
            "Xdut.P1", "Xdut.P2", "Xdut.P3",
            "Xdut.MX", "Xdut.MY", "Xdut.MSUP_P", "Xdut.MSUP_N",
        }

    def get_hierarchical_blocks(
        self,
        targets: DesignTarget | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[ExecutableChildSpec]:
        design = decompose_bandgap(SystemDesignRequest(
            system_type="bandgap",
            targets=targets or DesignTarget(),
        ))
        source = next(block.to_executable_child() for block in design.child_blocks())
        child_targets = replace(
            source.targets,
            topology_hint="two_stage_ota",
            custom_specs={
                **source.targets.custom_specs,
                "derived_from": self.meta.name,
                "input_common_mode_v": 0.7,
                "input_common_mode_min_v": 0.65,
                "input_common_mode_max_v": 0.75,
            },
        )
        pvt_targets = (
            replace(source.pvt_targets, topology_hint="two_stage_ota")
            if source.pvt_targets is not None
            else None
        )
        return [ExecutableChildSpec(
            block_id="opamp",
            topology_name="two_stage_ota",
            expected_subckt="two_stage_ota",
            ports=("vip", "vin", "vout", "ibias", "vdd", "vss"),
            targets=child_targets,
            pvt_targets=pvt_targets,
            sizing_policy="frozen_macro",
            netlist_param="opamp_netlist",
            results_param="opamp_results",
        )]

    def _load_nmos_opamp_netlist(
        self,
        params: dict[str, Any] | None = None,
    ) -> str:
        source = _get_optional_path(params, "opamp_netlist", "OPAMP_NETLIST")
        if source is not None and source.exists():
            return _sanitize_child_netlist(source.read_text(encoding="utf-8"))
        return _sanitize_child_netlist(TwoStageOTA().generate_circuit())


def _get_optional_path(
    params: dict[str, Any] | None,
    *names: str,
) -> Path | None:
    if not params:
        return None
    for name in names:
        value = params.get(name)
        if value:
            return Path(str(value)).expanduser()
    return None


def _sanitize_child_netlist(netlist: str) -> str:
    kept: list[str] = []
    for line in netlist.splitlines():
        stripped = line.strip()
        if stripped.startswith("simulator lang=") or stripped.startswith("include "):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _fmt(value: float) -> str:
    return format_spice_value(float(value))


_CIRCUIT_TEMPLATE = """\
// banba_sub1v_bandgap.cir -- Banba et al. sub-1-V current-mode BGR
simulator lang=spectre insensitive=yes

{spectre_include}

parameters R12={R12} PTAT_WEIGHT={PTAT_WEIGHT} VREF_SCALE={VREF_SCALE}
parameters R3=R12/PTAT_WEIGHT R4=R12*VREF_SCALE DIODE_AREA_RATIO={DIODE_AREA_RATIO}
parameters Wmirror_p={Wmirror_p} Lmirror_p={Lmirror_p} Iopbias={Iopbias}
parameters C1={C1} C2={C2} Cload={Cload}
parameters Wstart_y={Wstart_y} Wstart_x={Wstart_x} Lstart={Lstart}
parameters Rstart_ref={Rstart_ref} Rstart_bias={Rstart_bias} Wstart_bias_p={Wstart_bias_p}
parameters Wstart_cmp_p={Wstart_cmp_p} Wstart_cmp_n={Wstart_cmp_n} Lstart_cmp={Lstart_cmp}
parameters Wstart_inv_p={Wstart_inv_p} Wstart_inv_n={Wstart_inv_n}

subckt banba_sub1v_bandgap (vref vdd vss)
// NMOS-input error amplifier forces Va=Vb. Port order: vip vin vout ibias vdd vss.
IOPBIASsrc (vdd opibias) isource type=dc dc=Iopbias
Xopamp (vb va vg opibias vdd vss) two_stage_ota

// Fig. 2 equal-size PMOS branches: I1=I2=I3.
P1 (va vg vdd vdd) {pmos_lvt_model} l=Lmirror_p w=Wmirror_p nf=1 m=1
P2 (vb vg vdd vdd) {pmos_lvt_model} l=Lmirror_p w=Wmirror_p nf=1 m=1
P3 (vref vg vdd vdd) {pmos_lvt_model} l=Lmirror_p w=Wmirror_p nf=1 m=1

// R1=R2. Q1 and QN implement the 1:N diode-area ratio.
R1dev (va vss) resistor r=R12
Q1 (vss vss va) {pnp_model} m=1
R2dev (vb vss) resistor r=R12
R3dev (vb vdn) resistor r=R3
QN (vss vss vdn) {pnp_model} m=DIODE_AREA_RATIO
R4dev (vref vss) resistor r=R4

// Fig. 5 loop stabilization.
C1dev (va vss) capacitor c=C1
C2dev (vg vdd) capacitor c=C2

// Boni circuit III. X is the opamp minus input (va), Y is its plus input
// (vb), and IX > IY drives the core away from the zero-current equilibrium.
MX (va sup vdd vdd) {pmos_lvt_model} l=Lstart w=Wstart_x nf=1 m=1
MY (vb sup vdd vdd) {pmos_lvt_model} l=Lstart w=Wstart_y nf=1 m=1

// Raw-supply-derived bias and rough VRS = 0.9R/(R+0.9R) * VBE reference.
MSTART_BIAS (start_bias start_bias vdd vdd) {pmos_lvt_model} l=Lstart_cmp w=Wstart_bias_p nf=1 m=1
RSTART_BIAS (start_bias vss) resistor r=Rstart_bias
MSTART_VRS (vrs_diode start_bias vdd vdd) {pmos_lvt_model} l=Lstart_cmp w=Wstart_bias_p nf=1 m=1
QRS (vss vss vrs_diode) {pnp_model} m=1
RRS_TOP (vrs_diode vrs) resistor r=Rstart_ref
RRS_BOTTOM (vrs vss) resistor r=0.9*Rstart_ref

// VREF detector: SUP is low during startup and rises to VDD after VREF > VRS,
// switching MX and MY off so they do not perturb the settled reference.
MSTART_CMP (cmp_tail start_bias vdd vdd) {pmos_lvt_model} l=Lstart_cmp w=Wstart_bias_p nf=1 m=1
MCMP_RS (cmp_left vrs cmp_tail vdd) {pmos_lvt_model} l=Lstart_cmp w=Wstart_cmp_p nf=1 m=1
MCMP_REF (cmp_out vref cmp_tail vdd) {pmos_lvt_model} l=Lstart_cmp w=Wstart_cmp_p nf=1 m=1
MCMP_NL (cmp_left cmp_left vss vss) {nmos_lvt_model} l=Lstart_cmp w=Wstart_cmp_n nf=1 m=1
MCMP_NR (cmp_out cmp_left vss vss) {nmos_lvt_model} l=Lstart_cmp w=Wstart_cmp_n nf=1 m=1
MSUP_P (sup cmp_out vdd vdd) {pmos_lvt_model} l=Lstart_cmp w=Wstart_inv_p nf=1 m=1
MSUP_N (sup cmp_out vss vss) {nmos_lvt_model} l=Lstart_cmp w=Wstart_inv_n nf=1 m=1
CloadDev (vref vss) capacitor c=Cload
ends banba_sub1v_bandgap

// ---- Frozen NMOS-input child opamp macro ----
{opamp_netlist}
"""
