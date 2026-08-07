"""Paper-style capacitor-free LDO with damping-factor-control compensation."""

from __future__ import annotations

from typing import Any

from models import (
    CircuitFiles,
    DesignTarget,
    MetricGoal,
    ParamDef,
    ParamSpace,
    format_spice_value,
    split_width,
)
from pdk_profiles import get_pdk_profile_for_params, spectre_include_line
from topologies.base import BaseTopology, PassiveImplementation, TopologyMeta


class DFCCaplessLDO(BaseTopology):
    """Monolithic PMOS-pass LDO based on the paper's Fig. 4."""

    PASSIVE_IMPLEMENTATIONS = (
        PassiveImplementation("RfbTop", "resistor", "feedback_resistor"),
        PassiveImplementation("RfbBottom", "resistor", "feedback_resistor"),
        PassiveImplementation("Cf1Dev", "capacitor", "feedforward_capacitor"),
        PassiveImplementation("Cm1Dev", "capacitor", "compensation_capacitor"),
        PassiveImplementation("Cm2Dev", "capacitor", "compensation_capacitor"),
    )

    meta = TopologyMeta(
        name="dfc_capless_ldo",
        display_name="DFC Capacitor-Free LDO",
        description=(
            "Paper-style monolithic capacitor-free LDO with a PMOS-input "
            "error amplifier, gain-enhanced second stage, PMOS pass device, "
            "and damping-factor-control frequency compensation."
        ),
        min_gain_db=55,
        max_gain_db=100,
        min_gbw_hz=1e5,
        max_gbw_hz=20e6,
        typical_power_w=500e-6,
        complexity=5,
    )

    DEFAULT_PARAMS: dict[str, float] = {
        "VIN": 1.8,
        "VOUT_TARGET": 0.9,
        "VREF": 0.1,
        "VB1": 1.0,
        "VB2": 0.6,
        "VB4": 0.8,
        "Wbiasp": 12e-6,
        "Lbiasp": 600e-9,
        "Wdiffp": 20e-6,
        "Ldiffp": 500e-9,
        "Wloadn": 8e-6,
        "Lloadn": 500e-9,
        "Wboostn": 5e-6,
        "Lboostn": 500e-9,
        "k_gm": 4,
        "Wstage2p": 16e-6,
        "Lstage2p": 500e-9,
        "Wdfc_sink": 4e-6,
        "Wdfc_tail": 4e-6,
        "Wdfc_pair": 8e-6,
        "Ldfcn": 500e-9,
        "Wdfcp": 8e-6,
        "Ldfcp": 500e-9,
        "Wpass": 2e-3,
        "Lpass": 500e-9,
        "feedback_ratio": 8.0,
        "Rfb_bottom": 100e3,
        "Ccomp_total": 11e-12,
        "cm1_fraction": 3.0 / 11.0,
        "cm2_remaining_fraction": 3.0 / 8.0,
        "CL": 200e-12,
        "ILOAD_MIN": 10e-3,
        "ILOAD_MAX": 100e-3,
        "LOAD_EDGE": 1e-6,
    }

    def availability_error(
        self,
        params: dict[str, Any] | None = None,
    ) -> str | None:
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)
        required_voltage = float(p["VIN"])
        domain = pdk.voltage_domains.get(pdk.active_voltage_domain)
        max_device_voltage = (
            domain.max_device_voltage
            if domain and domain.max_device_voltage is not None
            else pdk.vdd_max
        )
        if pdk.vdd < required_voltage - 1e-9:
            return (
                f"dfc_capless_ldo requires the {required_voltage:g} V IO "
                "domain. Select VOLTAGE_DOMAIN=io_1p8."
            )
        if max_device_voltage < required_voltage - 1e-9:
            return (
                f"dfc_capless_ldo applies {required_voltage:g} V, above the "
                f"active device-domain limit {max_device_voltage:g} V."
            )
        if (
            pdk.nmos_model != "nch_25ud18_mac"
            or pdk.pmos_model != "pch_25ud18_mac"
        ):
            return (
                "dfc_capless_ldo requires nch_25ud18_mac/pch_25ud18_mac. "
                "Select the TSMC28 io_1p8 voltage domain."
            )
        return None

    def generate_circuit(self, params: dict[str, Any] | None = None) -> str:
        self.require_available(params)
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)
        geometry: dict[str, str | int] = {}
        for width_name in (
            "Wbiasp",
            "Wdiffp",
            "Wloadn",
            "Wboostn",
            "Wstage2p",
            "Wdfc_sink",
            "Wdfc_tail",
            "Wdfc_pair",
            "Wdfcp",
            "Wpass",
        ):
            width, nf, multiplicity = split_width(
                float(p[width_name]),
                pdk.max_width_per_finger,
            )
            if params and f"nf_{width_name}" in params:
                width = float(p[width_name])
                nf = int(params[f"nf_{width_name}"])
                multiplicity = int(params.get(f"m_{width_name}", 1))
            geometry[width_name] = _fmt(width)
            geometry[f"nf_{width_name}"] = nf
            geometry[f"m_{width_name}"] = multiplicity

        values = {
            "spectre_include": spectre_include_line(pdk),
            "nmos_model": pdk.nmos_model,
            "pmos_model": pdk.pmos_model,
            "Lbiasp": _fmt(p["Lbiasp"]),
            "Ldiffp": _fmt(p["Ldiffp"]),
            "Lloadn": _fmt(p["Lloadn"]),
            "Lboostn": _fmt(p["Lboostn"]),
            "k_gm": int(p["k_gm"]),
            "Lstage2p": _fmt(p["Lstage2p"]),
            "Ldfcn": _fmt(p["Ldfcn"]),
            "Ldfcp": _fmt(p["Ldfcp"]),
            "Lpass": _fmt(p["Lpass"]),
            "feedback_ratio": _fmt(p["feedback_ratio"]),
            "Rfb_bottom": _fmt(p["Rfb_bottom"]),
            "Ccomp_total": _fmt(p["Ccomp_total"]),
            "cm1_fraction": _fmt(p["cm1_fraction"]),
            "cm2_remaining_fraction": _fmt(
                p["cm2_remaining_fraction"]
            ),
            **geometry,
        }
        return _CIRCUIT_TEMPLATE.format(**values)

    def generate_testbench(
        self,
        params: dict[str, Any] | None = None,
        analysis_type: str = "loop",
    ) -> str:
        self.require_available(params)
        p = self._merge_params_with_preset(params)
        values = {
            name: _fmt(p[name])
            for name in (
                "VIN",
                "VREF",
                "VB1",
                "VB2",
                "VB4",
                "CL",
                "ILOAD_MIN",
                "ILOAD_MAX",
                "LOAD_EDGE",
            )
        }
        if analysis_type in {"loop", "stb", "ac"}:
            return _TB_LOOP_TEMPLATE.format(**values)
        if analysis_type in {"load", "load_regulation", "dc"}:
            return _TB_LOAD_REGULATION_TEMPLATE.format(**values)
        if analysis_type in {"psr", "psrr"}:
            return _TB_PSR_TEMPLATE.format(**values)
        if analysis_type in {"load_transient", "tran"}:
            return _TB_LOAD_TRANSIENT_TEMPLATE.format(**values)
        raise ValueError(f"Unsupported DFC LDO analysis type: {analysis_type}")

    def get_circuit_files(
        self,
        params: dict[str, Any] | None = None,
    ) -> CircuitFiles:
        circuit = self.generate_circuit(params)
        return CircuitFiles(
            circuit_netlist=circuit,
            testbenches=[
                self.generate_testbench(params, "loop"),
                self.generate_testbench(params, "load_regulation"),
                self.generate_testbench(params, "psr"),
                self.generate_testbench(params, "load_transient"),
            ],
            circuit_name=CircuitFiles.extract_subckt_name(circuit),
            testbench_suffixes=["loop", "load", "psr", "load_tran"],
        )

    def get_default_params(self) -> dict[str, float]:
        return self._default_params_with_preset()

    def get_param_space(self) -> ParamSpace:
        pdk = get_pdk_profile_for_params({"VOLTAGE_DOMAIN": "io_1p8"})
        widths = [
            ("Wbiasp", 2e-6, 60e-6),
            ("Wdiffp", 2e-6, 100e-6),
            ("Wloadn", 1e-6, 60e-6),
            ("Wboostn", 1e-6, 40e-6),
            ("Wstage2p", 2e-6, 100e-6),
            ("Wdfc_sink", 0.5e-6, 30e-6),
            ("Wdfc_tail", 0.5e-6, 30e-6),
            ("Wdfc_pair", 1e-6, 60e-6),
            ("Wdfcp", 1e-6, 60e-6),
            ("Wpass", 200e-6, 8e-3),
        ]
        lengths = [
            "Lbiasp",
            "Ldiffp",
            "Lloadn",
            "Lboostn",
            "Lstage2p",
            "Ldfcn",
            "Ldfcp",
            "Lpass",
        ]
        params = [
            ParamDef(
                name,
                low=low,
                high=high,
                log_scale=True,
                unit="m",
                max_per_finger=pdk.max_width_per_finger,
            )
            for name, low, high in widths
        ]
        params.extend(
            ParamDef(
                name,
                low=max(pdk.min_l, 300e-9),
                high=1.2e-6,
                log_scale=True,
                unit="m",
            )
            for name in lengths
        )
        params.extend(
            [
                ParamDef("k_gm", 2, 12, False, "", value_type="int"),
                ParamDef("VB1", 0.5, 1.5, False, "V"),
                ParamDef("VB2", 0.2, 1.2, False, "V"),
                ParamDef("VB4", 0.2, 1.4, False, "V"),
                ParamDef("feedback_ratio", 7.5, 8.5, False, "V/V"),
                ParamDef("Rfb_bottom", 20e3, 2e6, True, "Ohm"),
                ParamDef("Ccomp_total", 2e-12, 11.8e-12, True, "F"),
                ParamDef("cm1_fraction", 0.15, 0.55, False, ""),
                ParamDef(
                    "cm2_remaining_fraction",
                    0.2,
                    0.7,
                    False,
                    "",
                ),
            ]
        )
        return self._apply_param_space_overrides(ParamSpace(params=params))

    def critical_operating_point_instances(self) -> set[str]:
        return {
            "M11",
            "M12",
            "M13",
            "M14",
            "M15",
            "M16",
            "M17",
            "M18",
            "M21",
            "M22",
            "M23",
            "M24",
            "Mpass",
            "MD1",
            "MD2",
            "MD3",
            "MD4",
            "MD5",
            "MD6",
            "MD7",
        }


def default_dfc_ldo_targets() -> DesignTarget:
    """Return the first reproduction targets for the paper topology."""

    return DesignTarget(
        gain_db=60,
        bandwidth_hz=1e6,
        phase_margin_deg=60,
        load_cap_f=200e-12,
        topology_hint="dfc_capless_ldo",
        custom_specs={
            "paper_figure": "Fig. 4",
            "input_voltage_v": 1.8,
            "output_voltage_v": 0.9,
            "output_voltage_tolerance_v": 10e-3,
            "reference_voltage_v": 0.1,
            "bias_interface": "external VB1/VB2/VB4 voltage sources",
            "load_current_min_a": 10e-3,
            "load_current_max_a": 100e-3,
            "load_cap_max_f": 200e-12,
            "load_edge_s": 1e-6,
            "required_voltage_domain": "tsmc28/io_1p8",
            "required_models": "nch_25ud18_mac/pch_25ud18_mac",
        },
        metric_goals={
            "output_voltage_v": MetricGoal(
                constraint="target",
                target=0.9,
                tolerance=10e-3,
            ),
            "load_regulation_v_per_a": MetricGoal(
                constraint="max",
                target=0.03,
            ),
            "dc_psr_db": MetricGoal(
                constraint="max",
                target=-62.0,
            ),
            "overshoot_v": MetricGoal(
                constraint="max",
                target=0.25,
            ),
            "undershoot_v": MetricGoal(
                constraint="max",
                target=0.28,
            ),
        },
    )


def _fmt(value: float) -> str:
    return format_spice_value(float(value))


_CIRCUIT_TEMPLATE = """\
// dfc_capless_ldo.cir -- Fig. 4 damping-factor-control capacitor-free LDO
simulator lang=spectre insensitive=yes

{spectre_include}

parameters Wbiasp={Wbiasp} Lbiasp={Lbiasp} nf_Wbiasp={nf_Wbiasp} m_Wbiasp={m_Wbiasp}
parameters Wdiffp={Wdiffp} Ldiffp={Ldiffp} nf_Wdiffp={nf_Wdiffp} m_Wdiffp={m_Wdiffp}
parameters Wloadn={Wloadn} Lloadn={Lloadn} nf_Wloadn={nf_Wloadn} m_Wloadn={m_Wloadn}
parameters Wboostn={Wboostn} Lboostn={Lboostn} nf_Wboostn={nf_Wboostn} m_Wboostn={m_Wboostn}
parameters k_gm={k_gm}
parameters Wstage2p={Wstage2p} Lstage2p={Lstage2p} nf_Wstage2p={nf_Wstage2p} m_Wstage2p={m_Wstage2p}
parameters Wdfc_sink={Wdfc_sink} Wdfc_tail={Wdfc_tail} Wdfc_pair={Wdfc_pair} Ldfcn={Ldfcn}
parameters nf_Wdfc_sink={nf_Wdfc_sink} m_Wdfc_sink={m_Wdfc_sink}
parameters nf_Wdfc_tail={nf_Wdfc_tail} m_Wdfc_tail={m_Wdfc_tail}
parameters nf_Wdfc_pair={nf_Wdfc_pair} m_Wdfc_pair={m_Wdfc_pair}
parameters Wdfcp={Wdfcp} Ldfcp={Ldfcp} nf_Wdfcp={nf_Wdfcp} m_Wdfcp={m_Wdfcp}
parameters Wpass={Wpass} Lpass={Lpass} nf_Wpass={nf_Wpass} m_Wpass={m_Wpass}
parameters feedback_ratio={feedback_ratio} Rfb_bottom={Rfb_bottom}
parameters Ccomp_total={Ccomp_total}
parameters cm1_fraction={cm1_fraction}
parameters cm2_remaining_fraction={cm2_remaining_fraction}
parameters Cm1=Ccomp_total*cm1_fraction
parameters Cm2=Ccomp_total*(1-cm1_fraction)*cm2_remaining_fraction
parameters Cf1=Ccomp_total*(1-cm1_fraction)*(1-cm2_remaining_fraction)

subckt dfc_capless_ldo (vin vref vb1 vb2 vb4 vout vss)
// PMOS-input first stage: M11 bias, M12/M13 pair, M14/M15 mirror load.
M11 (ntail vb1 vin vin) {pmos_model} w=Wbiasp l=Lbiasp nf=nf_Wbiasp m=m_Wbiasp
M12 (nleft vfb_ea ntail vin) {pmos_model} w=Wdiffp l=Ldiffp nf=nf_Wdiffp m=m_Wdiffp
M13 (n_stage1 vref ntail vin) {pmos_model} w=Wdiffp l=Ldiffp nf=nf_Wdiffp m=m_Wdiffp
M14 (nleft nleft vss vss) {nmos_model} w=Wloadn l=Lloadn nf=nf_Wloadn m=m_Wloadn
M15 (n_stage1 nleft vss vss) {nmos_model} w=Wloadn l=Lloadn nf=nf_Wloadn m=m_Wloadn

// M17/M18 produce the inverted small-signal replica n_stage1_inv.
M16 (n_stage1_inv vb1 vin vin) {pmos_model} w=Wbiasp l=Lbiasp nf=nf_Wbiasp m=m_Wbiasp
M17 (n_stage1_inv n_stage1 vss vss) {nmos_model} w=Wboostn l=Lboostn nf=nf_Wboostn m=m_Wboostn
M18 (n_stage1_inv n_stage1_inv vss vss) {nmos_model} w=Wboostn l=Lboostn nf=nf_Wboostn m=m_Wboostn

// Gain-enhanced second stage. M21/M22 are k_gm times M17/M18.
M21 (n_stage2_mirror n_stage1 vss vss) {nmos_model} w=Wboostn l=Lboostn nf=nf_Wboostn m=k_gm*m_Wboostn
M22 (n_gate n_stage1_inv vss vss) {nmos_model} w=Wboostn l=Lboostn nf=nf_Wboostn m=k_gm*m_Wboostn
M23 (n_stage2_mirror n_stage2_mirror vin vin) {pmos_model} w=Wstage2p l=Lstage2p nf=nf_Wstage2p m=m_Wstage2p
M24 (n_gate n_stage2_mirror vin vin) {pmos_model} w=Wstage2p l=Lstage2p nf=nf_Wstage2p m=m_Wstage2p
Mpass (vout n_gate vin vin) {pmos_model} w=Wpass l=Lpass nf=nf_Wpass m=m_Wpass

// Cross-coupled damping-factor-control network MD1-MD7.
MD1 (n_dfc_out n_stage1 vss vss) {nmos_model} w=Wdfc_sink l=Ldfcn nf=nf_Wdfc_sink m=m_Wdfc_sink
MD2 (n_dfc_tail vb2 vss vss) {nmos_model} w=Wdfc_tail l=Ldfcn nf=nf_Wdfc_tail m=m_Wdfc_tail
MD3 (n_dfc_diode7 n_dfc_out n_dfc_tail vss) {nmos_model} w=Wdfc_pair l=Ldfcn nf=nf_Wdfc_pair m=m_Wdfc_pair
MD4 (n_dfc_diode6 vb4 n_dfc_tail vss) {nmos_model} w=Wdfc_pair l=Ldfcn nf=nf_Wdfc_pair m=m_Wdfc_pair
MD5 (n_dfc_out n_dfc_diode6 vin vin) {pmos_model} w=Wdfcp l=Ldfcp nf=nf_Wdfcp m=m_Wdfcp
MD6 (n_dfc_diode6 n_dfc_diode6 vin vin) {pmos_model} w=Wdfcp l=Ldfcp nf=nf_Wdfcp m=m_Wdfcp
MD7 (n_dfc_diode7 n_dfc_diode7 vin vin) {pmos_model} w=Wdfcp l=Ldfcp nf=nf_Wdfcp m=m_Wdfcp

// Complete resistive/feed-forward feedback path crosses the STB probe.
RfbTop (vout vfb) resistor r=Rfb_bottom*feedback_ratio
RfbBottom (vfb vss) resistor r=Rfb_bottom
Cf1Dev (vout vfb) capacitor c=Cf1
Iloop (vfb vfb_ea) iprobe

// Cm1 is the global Miller path; Cm2 is the DFC path.
Cm1Dev (n_stage1 vout) capacitor c=Cm1
Cm2Dev (n_stage1 n_dfc_out) capacitor c=Cm2
ends dfc_capless_ldo
"""


_TB_COMMON = """\
include "circuit.cir"

parameters VIN={VIN} VREF={VREF} VB1={VB1} VB2={VB2} VB4={VB4}
parameters CL={CL} ILOAD_MIN={ILOAD_MIN} ILOAD_MAX={ILOAD_MAX}
parameters LOAD_EDGE={LOAD_EDGE}

VINsrc (vin 0) vsource type=dc dc=VIN
VREFsrc (vref 0) vsource type=dc dc=VREF
VB1src (vb1 0) vsource type=dc dc=VB1
VB2src (vb2 0) vsource type=dc dc=VB2
VB4src (vb4 0) vsource type=dc dc=VB4
VSSsrc (vss 0) vsource type=dc dc=0

Xdut (vin vref vb1 vb2 vb4 vout vss) dfc_capless_ldo
"""


_TB_LOOP_TEMPLATE = """\
// tb_dfc_capless_ldo_loop.scs -- worst-case minimum-load loop stability
simulator lang=spectre insensitive=yes

{common}
ILOADsrc (vout 0) isource type=dc dc=ILOAD_MIN
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
op1 dc oppoint=rawfile
opInfo info what=oppoint where=rawfile
ldoLoopStb stb start=1 stop=100M dec=30 probe=Xdut.Iloop

save vout
save VINsrc:p
""".replace("{common}", _TB_COMMON)


_TB_LOAD_REGULATION_TEMPLATE = """\
// tb_dfc_capless_ldo_load.scs -- paper 10-to-100mA load sweep
simulator lang=spectre insensitive=yes

{common}
parameters ILOAD=ILOAD_MIN
ILOADsrc (vout 0) isource type=dc dc=ILOAD
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
loadSweep dc param=ILOAD start=ILOAD_MIN stop=ILOAD_MAX step=1m

save vout
""".replace("{common}", _TB_COMMON)


_TB_PSR_TEMPLATE = """\
// tb_dfc_capless_ldo_psr.scs -- near-DC supply transfer at minimum load
simulator lang=spectre insensitive=yes

{common}
ILOADsrc (vout 0) isource type=dc dc=ILOAD_MIN
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
ldoPsrAC ac start=1m stop=1 dec=10

save vout
""".replace(
    "{common}",
    _TB_COMMON.replace(
        "VINsrc (vin 0) vsource type=dc dc=VIN",
        "VINsrc (vin 0) vsource type=dc dc=VIN mag=1",
    ),
)


_TB_LOAD_TRANSIENT_TEMPLATE = """\
// tb_dfc_capless_ldo_load_tran.scs -- paper 10-to-100mA load steps
simulator lang=spectre insensitive=yes

{common}
ILOADsrc (vout 0) isource type=pulse val0=ILOAD_MIN val1=ILOAD_MAX delay=5u rise=LOAD_EDGE fall=LOAD_EDGE width=5u period=12u
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
loadTran tran stop=18u maxstep=1n

save vout
save ILOADsrc:i
""".replace("{common}", _TB_COMMON)
