"""Hierarchical bandgap/PTAT reference topology.

First version of this topology is intentionally staged:

1. A folded-cascode opamp is optimized and verified as a child macro.
2. The bandgap/PTAT top level freezes that macro and only optimizes
   system-level parameters such as resistor ratios, bias current, pass-device
   size, and compensation/load values.

The opamp macro uses the folded-cascode port order:
    vip vin vout ibias vdd vss
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from models import CircuitFiles, DesignTarget, ParamDef, ParamSpace, format_spice_value
from pdk_profiles import get_pdk_profile, spectre_include_line
from topologies.base import BaseTopology, TopologyMeta
from topologies.folded_cascode import FoldedCascodeOTA


class BandgapPTAT(BaseTopology):
    """Bandgap/PTAT system-level topology with frozen folded-cascode macro."""

    meta = TopologyMeta(
        name="bandgap_ptat",
        display_name="Bandgap/PTAT Reference",
        description=(
            "Hierarchical bandgap/PTAT reference. The folded-cascode error "
            "amplifier is treated as a frozen child macro; BO searches only "
            "bandgap-level resistor, current, pass-device, and compensation "
            "parameters."
        ),
        min_gain_db=0,
        max_gain_db=0,
        min_gbw_hz=0,
        max_gbw_hz=0,
        typical_power_w=1e-3,
        complexity=5,
    )

    DEFAULT_PARAMS: dict[str, float] = {
        # System-level resistor network and biasing.
        "Rptat": 100e3,
        "Rctat": 100e3,
        "Rtop": 80e3,
        "Rbot": 40e3,
        "Ibias": 20e-6,
        "Iopbias": 20e-6,
        "BJT_AREA_RATIO": 8,
        # Top-level pass device and load/compensation.
        "Wpass": 20e-6,
        "Lpass": 500e-9,
        "Ccomp": 1e-12,
        "Cload": 1e-12,
    }

    def generate_circuit(self, params: dict[str, Any] | None = None) -> str:
        """Generate a hierarchical Spectre-native bandgap/PTAT netlist."""
        p = self._merge_params_with_preset(params)
        pdk = get_pdk_profile()
        opamp_netlist = self._load_opamp_netlist(params)

        return _CIRCUIT_TEMPLATE.format(
            spectre_include=spectre_include_line(pdk),
            pmos_model=pdk.pmos_model,
            Rptat=_fmt(p["Rptat"]),
            Rctat=_fmt(p["Rctat"]),
            Rtop=_fmt(p["Rtop"]),
            Rbot=_fmt(p["Rbot"]),
            Ibias=_fmt(p["Ibias"]),
            Iopbias=_fmt(p["Iopbias"]),
            BJT_AREA_RATIO=int(round(p["BJT_AREA_RATIO"])),
            Wpass=_fmt(p["Wpass"]),
            Lpass=_fmt(p["Lpass"]),
            Ccomp=_fmt(p["Ccomp"]),
            Cload=_fmt(p["Cload"]),
            opamp_netlist=opamp_netlist,
        )

    def generate_testbench(
        self,
        params: dict[str, Any] | None = None,
        analysis_type: str = "ac",
    ) -> str:
        """Generate nominal testbenches compatible with the existing pipeline.

        Bandgap-specific metrics such as Vref/tempco/line regulation need a
        dedicated parser; the AC/SR/ST forms here keep first-version projects
        compatible with the current BO/dry-run infrastructure.
        """
        pdk = get_pdk_profile()
        p = self._merge_params_with_preset(params)
        tb_defaults = self._testbench_defaults_with_preset(
            {
                "VCM": pdk.vdd / 2,
                "CL": p["Cload"],
            }
        )
        vdd = pdk.vdd
        vcm = tb_defaults["VCM"]
        cload = tb_defaults["CL"]

        if params:
            vdd = params.get("VDD", vdd)
            vcm = params.get("VCM", vcm)
            cload = params.get("CL", cload)

        if analysis_type in ("tran", "sr"):
            return _TB_SR_TEMPLATE.format(
                VDD=vdd,
                VCM=vcm,
                CL=_fmt(cload),
                VLOW=max(0.0, vcm - 0.1),
                VHIGH=min(vdd, vcm + 0.1),
            )
        if analysis_type == "st":
            return _TB_ST_TEMPLATE.format(
                VDD=vdd,
                VCM=vcm,
                CL=_fmt(cload),
                VLOW=vcm,
                VHIGH=min(vdd, vcm + 10e-3),
            )
        return _TB_AC_TEMPLATE.format(VDD=vdd, VCM=vcm, CL=_fmt(cload))

    def get_circuit_files(
        self, params: dict[str, Any] | None = None
    ) -> CircuitFiles:
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

    def write_project(
        self,
        project_dir: str | Path,
        targets: DesignTarget | None = None,
        params: dict[str, Any] | None = None,
        original_requirement: str = "",
    ) -> Path:
        out = super().write_project(
            project_dir,
            targets=targets,
            params=params,
            original_requirement=original_requirement,
        )
        self._write_child_block_metadata(out, targets, params)
        return out

    def get_default_params(self) -> dict[str, float]:
        return self._default_params_with_preset()

    def get_param_space(self) -> ParamSpace:
        return self._apply_param_space_overrides(ParamSpace(params=[
            ParamDef("Rptat", low=10e3, high=1e6, log_scale=True, unit="Ohm"),
            ParamDef("Rctat", low=10e3, high=1e6, log_scale=True, unit="Ohm"),
            ParamDef("Rtop", low=10e3, high=2e6, log_scale=True, unit="Ohm"),
            ParamDef("Rbot", low=10e3, high=2e6, log_scale=True, unit="Ohm"),
            ParamDef("Ibias", low=1e-6, high=200e-6, log_scale=True, unit="A"),
            ParamDef("Iopbias", low=1e-6, high=200e-6, log_scale=True, unit="A"),
            ParamDef(
                "BJT_AREA_RATIO",
                low=2,
                high=32,
                log_scale=False,
                unit="x",
                value_type="int",
            ),
            ParamDef(
                "Wpass",
                low=1e-6,
                high=200e-6,
                log_scale=True,
                unit="m",
                max_per_finger=2.6e-6,
            ),
            ParamDef("Lpass", low=200e-9, high=2e-6, log_scale=True, unit="m"),
            ParamDef("Ccomp", low=100e-15, high=10e-12, log_scale=True, unit="F"),
            ParamDef("Cload", low=100e-15, high=20e-12, log_scale=True, unit="F"),
        ]))

    def required_model_roles(self) -> tuple[str, ...]:
        return ("pmos", "nmos_lvt", "pmos_lvt")

    def critical_operating_point_instances(self) -> set[str]:
        return {"Mpass"}

    def derive_opamp_targets(
        self,
        targets: DesignTarget | None = None,
    ) -> DesignTarget:
        """Derive first-pass folded-cascode requirements for the child opamp."""
        custom = dict(targets.custom_specs) if targets else {}
        power_budget = targets.power_w if targets and targets.power_w else None
        load_cap = targets.load_cap_f if targets and targets.load_cap_f else None
        return DesignTarget(
            gain_db=float(custom.get("opamp_gain_db", 70.0)),
            bandwidth_hz=float(custom.get("opamp_gbw_hz", 10e6)),
            phase_margin_deg=float(custom.get("opamp_pm_deg", 60.0)),
            power_w=float(custom.get("opamp_power_w", power_budget * 0.5))
            if power_budget
            else float(custom.get("opamp_power_w", 1e-3)),
            load_cap_f=float(custom.get("opamp_load_cap_f", load_cap or 1e-12)),
            topology_hint="folded_cascode",
            custom_specs={
                "derived_from": "bandgap_ptat",
                "error_amplifier_role": "frozen_macro",
            },
        )

    def _load_opamp_netlist(self, params: dict[str, Any] | None = None) -> str:
        source = _get_optional_path(params, "opamp_netlist", "OPAMP_NETLIST")
        if source is not None and source.exists():
            return _sanitize_child_netlist(source.read_text(encoding="utf-8"))
        return _sanitize_child_netlist(FoldedCascodeOTA().generate_circuit())

    def _write_child_block_metadata(
        self,
        project_dir: Path,
        targets: DesignTarget | None,
        params: dict[str, Any] | None,
    ) -> None:
        child_dir = project_dir / "child_blocks" / "folded_cascode_opamp"
        child_dir.mkdir(parents=True, exist_ok=True)

        opamp_netlist_path = _get_optional_path(params, "opamp_netlist", "OPAMP_NETLIST")
        if opamp_netlist_path is not None and opamp_netlist_path.exists():
            shutil.copyfile(opamp_netlist_path, child_dir / "circuit.cir")
            opamp_source = str(opamp_netlist_path)
        else:
            (child_dir / "circuit.cir").write_text(
                FoldedCascodeOTA().generate_circuit(),
                encoding="utf-8",
            )
            opamp_source = "fallback:folded_cascode_default"

        opamp_results_path = _get_optional_path(
            params,
            "opamp_results",
            "OPAMP_RESULTS",
        )
        if opamp_results_path is not None and opamp_results_path.exists():
            shutil.copyfile(opamp_results_path, child_dir / "source_results.json")
            results_source = str(opamp_results_path)
        else:
            (child_dir / "source_results.json").write_text(
                json.dumps(
                    {
                        "source": opamp_source,
                        "note": (
                            "No folded-cascode results.json was supplied; "
                            "the bandgap project uses the available child "
                            "netlist as a frozen macro."
                        ),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            results_source = None

        req_path = project_dir / "requirements.json"
        if not req_path.exists():
            return

        req = json.loads(req_path.read_text(encoding="utf-8"))
        req["hierarchical_blocks"] = {
            "opamp": {
                "topology": "folded_cascode",
                "ports": ["vip", "vin", "vout", "ibias", "vdd", "vss"],
                "netlist_source": opamp_source,
                "results_source": results_source,
                "local_netlist": str(child_dir / "circuit.cir"),
                "local_results": str(child_dir / "source_results.json"),
                "sizing_policy": "frozen_macro",
                "derived_targets": self.derive_opamp_targets(
                    targets
                ).to_requirements_dict()["targets"],
            }
        }
        req_path.write_text(
            json.dumps(req, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
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
    """Keep child subckts while removing duplicate top-level setup lines."""
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
// bandgap_ptat.cir -- Hierarchical Bandgap/PTAT reference (Spectre native syntax)
simulator lang=spectre insensitive=yes

{spectre_include}

parameters Rptat={Rptat} Rctat={Rctat} Rtop={Rtop} Rbot={Rbot}
parameters Ibias={Ibias} Iopbias={Iopbias} BJT_AREA_RATIO={BJT_AREA_RATIO}
parameters Wpass={Wpass} Lpass={Lpass} Ccomp={Ccomp} Cload={Cload}
parameters VCTAT=700m VPTAT=26m*ln(BJT_AREA_RATIO)

subckt bandgap_ptat (vref vdd vss)
// First-version PTAT/CTAT scaffold. Real PDK-specific BJT devices can replace
// these idealized sources once the bandgap parser and BJT profile are added.
IBIASsrc (vdd ibias) isource type=dc dc=Ibias
IOPBIASsrc (vdd opibias) isource type=dc dc=Iopbias
VctatSrc (nctat vss) vsource type=dc dc=VCTAT
VptatSrc (nptat vss) vsource type=dc dc=VPTAT
RptatDev (nptat nsense) resistor r=Rptat
RctatDev (nctat nsense) resistor r=Rctat
RtopDev (vref nfb) resistor r=Rtop
RbotDev (nfb vss) resistor r=Rbot

// Folded-cascode error amplifier macro. Port order: vip vin vout ibias vdd vss
Xopamp (nsense nfb vctrl opibias vdd vss) folded_cascode

// PMOS pass device controlled by the error amplifier.
Mpass (vref vctrl vdd vdd) {pmos_model} w=Wpass l=Lpass nf=1
CcompDev (vctrl vss) capacitor c=Ccomp
CloadDev (vref vss) capacitor c=Cload
Rleak (vref vss) resistor r=1G
ends bandgap_ptat

// ---- Frozen child opamp macro ----
{opamp_netlist}
"""


_TB_AC_TEMPLATE = """\
// tb_bandgap_ptat_ac.scs -- Bandgap/PTAT nominal AC-compatible analysis
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} CL={CL}

VDDsrc (vdd 0) vsource type=dc dc=VDD mag=1
VSSsrc (vss 0) vsource type=dc dc=0
VCMsrc (vcm 0) vsource type=dc dc=VCM
VIPsrc (vinp vcm) vsource type=dc dc=0 mag=1
Rfb (vout vinn) resistor r=1G
Cfb (vinn 0) capacitor c=1

Xdut (vout vdd vss) bandgap_ptat
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
op1 dc oppoint=rawfile
opInfo info what=oppoint where=rawfile
ac1 ac start=1 stop=20G dec=20

save vout
save VDDsrc:p
"""


_TB_SR_TEMPLATE = """\
// tb_bandgap_ptat_sr.scs -- Bandgap/PTAT startup-style transient scaffold
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=pulse val0=0 val1=VDD delay=2n rise=1n fall=1n width=200n period=400n
VSSsrc (vss 0) vsource type=dc dc=0
VCMsrc (vcm 0) vsource type=dc dc=VCM
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=5n rise=100p fall=100p width=50n period=100n

Xdut (vout vdd vss) bandgap_ptat
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
srTran tran stop=500n maxstep=20p

save vinp vout
save VDDsrc:p
"""


_TB_ST_TEMPLATE = """\
// tb_bandgap_ptat_st.scs -- Bandgap/PTAT settling scaffold
simulator lang=spectre insensitive=yes

include "circuit.cir"

parameters VDD={VDD} VCM={VCM} CL={CL}
parameters VLOW={VLOW} VHIGH={VHIGH}

VDDsrc (vdd 0) vsource type=pulse val0=0 val1=VDD delay=2n rise=1n fall=1n width=200n period=400n
VSSsrc (vss 0) vsource type=dc dc=0
VCMsrc (vcm 0) vsource type=dc dc=VCM
VIPsrc (vinp 0) vsource type=pulse val0=VLOW val1=VHIGH delay=5n rise=100p fall=100p width=50n period=100n

Xdut (vout vdd vss) bandgap_ptat
CLload (vout 0) capacitor c=CL

tempOption options temp=27
outOpts options rawfmt=psfascii soft_bin=allmodels
stTran tran stop=500n maxstep=20p

save vinp vout
save VDDsrc:p
"""
