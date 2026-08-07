"""PMOS-pass capacitor-less LDO with a frozen two-stage error amplifier."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from models import (
    CircuitFiles,
    DesignTarget,
    ParamDef,
    ParamSpace,
    format_spice_value,
    split_width,
)
from pdk_profiles import get_pdk_profile_for_params, spectre_include_line
from system_decomposition import SystemDesignRequest, decompose_ldo
from topologies.base import (
    BaseTopology,
    ExecutableChildSpec,
    PassiveImplementation,
    TopologyMeta,
)
from topologies.two_stage_ota import TwoStageOTA


class CaplessLDO(BaseTopology):
    """1.8 V to 0.9 V PMOS-pass LDO for 0-10 mA loads."""

    PASSIVE_IMPLEMENTATIONS = (
        PassiveImplementation("RgateDev", "resistor", "gate_resistor"),
        PassiveImplementation("RfbTop", "resistor", "feedback_resistor"),
        PassiveImplementation("RfbBottom", "resistor", "feedback_resistor"),
        PassiveImplementation("RbleedDev", "resistor", "bias_resistor"),
        PassiveImplementation("CffDev", "capacitor", "feedforward_capacitor"),
        PassiveImplementation("CcompDev", "capacitor", "compensation_capacitor"),
    )

    meta = TopologyMeta(
        name="capless_ldo",
        display_name="PMOS-Pass Capacitor-Less LDO",
        description=(
            "Hierarchical 1.8 V-to-0.9 V LDO with a PMOS pass device, "
            "resistive feedback, internal compensation, and a frozen "
            "two-stage OTA error amplifier."
        ),
        min_gain_db=55,
        max_gain_db=100,
        min_gbw_hz=1e5,
        max_gbw_hz=20e6,
        typical_power_w=200e-6,
        complexity=5,
    )

    DEFAULT_PARAMS: dict[str, float] = {
        "VIN": 1.8,
        "VOUT_TARGET": 0.9,
        "VREF": 0.45,
        "IBIAS": 20e-6,
        "Wpass": 1e-3,
        "Lpass": 500e-9,
        "feedback_ratio": 1.0,
        "Rfb_bottom": 1e6,
        "Rgate": 1e3,
        "Ccomp": 5e-12,
        "Cff": 1e-12,
        "Rbleed": 1e6,
        "CL_NOLOAD": 1e-12,
        "CL_TRANSIENT": 200e-12,
        "ILOAD_MAX": 10e-3,
        "LOAD_EDGE": 10e-9,
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
                f"capless_ldo requires a {required_voltage:g} V IO voltage "
                f"domain, but active domain "
                f"'{pdk.active_voltage_domain or 'default'}' uses "
                f"VDD={pdk.vdd:g} V. Configure PDK_PROFILE_FILE and select "
                "the 1.8 V IO domain with VOLTAGE_DOMAIN."
            )
        if max_device_voltage < required_voltage - 1e-9:
            return (
                f"capless_ldo applies {required_voltage:g} V to the pass "
                f"device, above the active domain limit "
                f"{max_device_voltage:g} V."
            )
        return None

    def generate_circuit(self, params: dict[str, Any] | None = None) -> str:
        self.require_available(params)
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile_for_params(params)
        error_amp_netlist = self._load_error_amp_netlist(params)
        rendered_width, nf_pass, m_pass = split_width(
            p["Wpass"],
            pdk.max_width_per_finger,
        )
        if params and "nf_Wpass" in params:
            rendered_width = p["Wpass"]
            nf_pass = int(params.get("nf_Wpass", nf_pass))
            m_pass = int(params.get("m_Wpass", m_pass))

        return _CIRCUIT_TEMPLATE.format(
            spectre_include=spectre_include_line(pdk),
            pmos_model=pdk.pmos_model,
            Wpass=_fmt(rendered_width),
            Lpass=_fmt(p["Lpass"]),
            nf_pass=nf_pass,
            m_pass=m_pass,
            feedback_ratio=_fmt(p["feedback_ratio"]),
            Rfb_bottom=_fmt(p["Rfb_bottom"]),
            Rgate=_fmt(p["Rgate"]),
            Ccomp=_fmt(p["Ccomp"]),
            Cff=_fmt(p["Cff"]),
            Rbleed=_fmt(p["Rbleed"]),
            error_amp_netlist=error_amp_netlist,
        )

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
                "IBIAS",
                "CL_NOLOAD",
                "CL_TRANSIENT",
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
        raise ValueError(f"Unsupported capless LDO analysis type: {analysis_type}")

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
        pdk = get_pdk_profile_for_params()
        return self._apply_param_space_overrides(
            ParamSpace(
                params=[
                    ParamDef(
                        "Wpass",
                        low=100e-6,
                        high=5e-3,
                        log_scale=True,
                        unit="m",
                        max_per_finger=pdk.max_width_per_finger,
                    ),
                    ParamDef(
                        "Lpass",
                        low=max(pdk.min_l, 300e-9),
                        high=1.2e-6,
                        log_scale=True,
                        unit="m",
                    ),
                    ParamDef(
                        "feedback_ratio",
                        low=0.8,
                        high=1.2,
                        log_scale=False,
                        unit="V/V",
                    ),
                    ParamDef(
                        "Rfb_bottom",
                        low=100e3,
                        high=10e6,
                        log_scale=True,
                        unit="Ohm",
                    ),
                    ParamDef(
                        "Rgate",
                        low=10,
                        high=20e3,
                        log_scale=True,
                        unit="Ohm",
                    ),
                    ParamDef(
                        "Ccomp",
                        low=100e-15,
                        high=50e-12,
                        log_scale=True,
                        unit="F",
                    ),
                    ParamDef(
                        "Cff",
                        low=10e-15,
                        high=20e-12,
                        log_scale=True,
                        unit="F",
                    ),
                    ParamDef(
                        "Rbleed",
                        low=100e3,
                        high=20e6,
                        log_scale=True,
                        unit="Ohm",
                    ),
                ]
            )
        )

    def get_hierarchical_blocks(
        self,
        targets: DesignTarget | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[ExecutableChildSpec]:
        design = decompose_ldo(
            SystemDesignRequest(
                system_type="ldo",
                targets=targets or default_ldo_targets(),
                voltage_domain=(
                    str(params["VOLTAGE_DOMAIN"])
                    if params and params.get("VOLTAGE_DOMAIN")
                    else None
                ),
            )
        )
        return [block.to_executable_child() for block in design.child_blocks()]

    def critical_operating_point_instances(self) -> set[str]:
        return {"Mpass"}

    def _load_error_amp_netlist(
        self,
        params: dict[str, Any] | None = None,
    ) -> str:
        source = _get_optional_path(
            params,
            "error_amp_netlist",
            "ERROR_AMP_NETLIST",
        )
        if source is not None and source.exists():
            return _sanitize_child_netlist(source.read_text(encoding="utf-8"))
        return _sanitize_child_netlist(TwoStageOTA().generate_circuit(params))


def default_ldo_targets() -> DesignTarget:
    """Return the provisional targets supplied for the first LDO version."""
    from models import MetricGoal

    return DesignTarget(
        gain_db=60,
        bandwidth_hz=1e6,
        phase_margin_deg=60,
        topology_hint="capless_ldo",
        custom_specs={
            "input_voltage_v": 1.8,
            "output_voltage_v": 0.9,
            "output_voltage_tolerance_v": 10e-3,
            "load_current_min_a": 0.0,
            "load_current_max_a": 10e-3,
            "load_cap_min_f": 1e-12,
            "load_cap_max_f": 200e-12,
            "load_edge_s": 10e-9,
            "load_regulation_interpretation": "30 uV/mA",
            "reference_interface": "external 0.45 V input",
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
        if stripped.startswith("simulator lang="):
            continue
        if stripped.startswith("include "):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _fmt(value: float) -> str:
    return format_spice_value(float(value))


_CIRCUIT_TEMPLATE = """\
// capless_ldo.cir -- PMOS-pass capacitor-less LDO
simulator lang=spectre insensitive=yes

{spectre_include}

parameters Wpass={Wpass} Lpass={Lpass}
parameters feedback_ratio={feedback_ratio} Rfb_bottom={Rfb_bottom}
parameters Rgate={Rgate} Ccomp={Ccomp} Cff={Cff} Rbleed={Rbleed}

subckt capless_ldo (vin vref vout ibias vss)
// Error amplifier polarity: low vfb drives vea/vg low and turns Mpass on.
XerrorAmp (vfb_ea vref vea ibias vin vss) two_stage_ota
RgateDev (vea vg) resistor r=Rgate
Mpass (vout vg vin vin) {pmos_model} w=Wpass l=Lpass nf={nf_pass} m={m_pass}

// All resistive and feed-forward feedback crosses Iloop before the OTA input.
RfbTop (vout vfb) resistor r=Rfb_bottom*feedback_ratio
RfbBottom (vfb vss) resistor r=Rfb_bottom
CffDev (vout vfb) capacitor c=Cff
Iloop (vfb vfb_ea) iprobe

// Internal compensation and a weak bleed path support zero-load operation.
CcompDev (vg vout) capacitor c=Ccomp
RbleedDev (vout vss) resistor r=Rbleed
ends capless_ldo

// ---- Frozen child error-amplifier macro ----
{error_amp_netlist}
"""


_TB_COMMON = """\
include "circuit.cir"

parameters VIN={VIN} VREF={VREF} IBIAS={IBIAS}
parameters CL_NOLOAD={CL_NOLOAD} CL_TRANSIENT={CL_TRANSIENT}
parameters ILOAD_MAX={ILOAD_MAX} LOAD_EDGE={LOAD_EDGE}

VINsrc (vin 0) vsource type=dc dc=VIN
VREFsrc (vref 0) vsource type=dc dc=VREF
VSSsrc (vss 0) vsource type=dc dc=0
IBIASsrc (vin ibias) isource type=dc dc=IBIAS

Xdut (vin vref vout ibias vss) capless_ldo
"""


_TB_LOOP_TEMPLATE = """\
// tb_capless_ldo_loop.scs -- zero-load loop stability
simulator lang=spectre insensitive=yes

{common}
ILOADsrc (vout 0) isource type=dc dc=0
CLload (vout 0) capacitor c=CL_NOLOAD

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
op1 dc oppoint=rawfile
opInfo info what=oppoint where=rawfile
ldoLoopStb stb start=1 stop=100M dec=30 probe=Xdut.Iloop

save vout
save VINsrc:p
""".replace("{common}", _TB_COMMON)


_TB_LOAD_REGULATION_TEMPLATE = """\
// tb_capless_ldo_load.scs -- 0-to-10mA DC load regulation
simulator lang=spectre insensitive=yes

{common}
parameters ILOAD=0
ILOADsrc (vout 0) isource type=dc dc=ILOAD
CLload (vout 0) capacitor c=CL_TRANSIENT

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
loadSweep dc param=ILOAD start=0 stop=ILOAD_MAX step=100u

save vout
""".replace("{common}", _TB_COMMON)


_TB_PSR_TEMPLATE = """\
// tb_capless_ldo_psr.scs -- near-DC supply transfer
simulator lang=spectre insensitive=yes

{common}
ILOADsrc (vout 0) isource type=dc dc=0
CLload (vout 0) capacitor c=CL_NOLOAD

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
// tb_capless_ldo_load_tran.scs -- 10ns full-load steps
simulator lang=spectre insensitive=yes

{common}
ILOADsrc (vout 0) isource type=pulse val0=0 val1=ILOAD_MAX delay=5u rise=LOAD_EDGE fall=LOAD_EDGE width=5u period=12u
CLload (vout 0) capacitor c=CL_TRANSIENT

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
loadTran tran stop=18u maxstep=1n

save vout
save ILOADsrc:p
""".replace("{common}", _TB_COMMON)
