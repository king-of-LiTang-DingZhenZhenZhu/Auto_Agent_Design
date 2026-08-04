from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

from .adapters import adapt_topology
from .schema import HandoffDevice, ImportedDesignHandoff


_SUFFIXES = {
    "t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3,
    "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15,
}
_MODEL_KIND = {
    "nch_mac": "nmos", "nch_lvt_mac": "nmos", "pch_mac": "pmos", "pch_lvt_mac": "pmos",
}


def build_imported_design_handoff(
    *,
    project_dir: str | Path,
    topology: str,
    final_netlist: str | Path,
    final_source: str,
    pvt_results: str | Path,
    schematic_ir: object,
    output_dir: str | Path | None = None,
) -> ImportedDesignHandoff:
    project = Path(project_dir).resolve()
    netlist = Path(final_netlist).resolve()
    pvt_path = Path(pvt_results).resolve()
    if not netlist.is_file():
        raise FileNotFoundError(f"final netlist not found: {netlist}")
    if not pvt_path.is_file():
        raise FileNotFoundError(f"PVT evidence not found: {pvt_path}")
    pvt_data = json.loads(pvt_path.read_text(encoding="utf-8"))
    if pvt_data.get("pvt_pass") is not True:
        raise ValueError("physical implementation requires real pvt_pass=true evidence")

    adapted = adapt_topology(topology, getattr(schematic_ir, "instances"), getattr(schematic_ir, "ports"))
    root = Path(output_dir) if output_dir is not None else project / "physical"
    input_dir = root / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    snapshot = input_dir / "final.cir"
    shutil.copy2(netlist, snapshot)

    devices = tuple(_handoff_device(item, adapted.device_roles) for item in getattr(schematic_ir, "instances"))
    all_nets = {str(net) for item in devices for net in item.nodes} | set(str(p) for p in getattr(schematic_ir, "ports"))
    net_roles = {net: adapted.net_roles.get(net, "internal") for net in sorted(all_nets)}
    mapping = {
        item.name: {"frontend_instance": item.name, "lvs_instances": [item.name], "oa_instances": [item.name]}
        for item in devices
    }
    handoff = ImportedDesignHandoff(
        project=project.name,
        topology=str(topology),
        subckt_name=str(getattr(schematic_ir, "subckt_name")),
        pdk="crn28hpcp",
        ports=tuple(str(item) for item in getattr(schematic_ir, "ports")),
        devices=devices,
        net_roles=net_roles,
        matched_groups=adapted.matched_groups,
        symmetry_groups=adapted.symmetry_groups,
        routing_constraints=adapted.routing_constraints,
        critical_nets=adapted.critical_nets,
        final_netlist=str(snapshot),
        final_netlist_sha256=_sha256(snapshot),
        final_source=str(final_source),
        pvt_results=str(pvt_path),
        pvt_results_sha256=_sha256(pvt_path),
        pvt_summary=dict(pvt_data.get("summary", {})),
        instance_mapping=mapping,
    )
    handoff.validate()
    return handoff


def _handoff_device(instance: object, roles: dict[str, str]) -> HandoffDevice:
    name = str(getattr(instance, "name"))
    kind = str(getattr(instance, "kind"))
    model = str(getattr(instance, "model"))
    nodes = tuple(str(item) for item in getattr(instance, "nodes"))
    if kind == "mos":
        if model.lower() not in _MODEL_KIND:
            raise ValueError(f"unsupported MOS model {model!r} for {name}")
        terminals = ("D", "G", "S", "B")
    elif kind in {"res", "cap"}:
        terminals = ("PLUS", "MINUS")
    else:
        raise ValueError(f"unsupported device kind {kind!r} for {name}")
    params: dict[str, float | int | str] = {}
    for key, value in dict(getattr(instance, "params", {})).items():
        if key in {"nf", "m"}:
            parsed = _parse_number(value)
            if not float(parsed).is_integer() or parsed <= 0:
                raise ValueError(f"{name} has invalid integer {key}={value!r}")
            params[key] = int(parsed)
        elif key in {"W", "L", "R", "C"}:
            params[key] = _parse_number(value)
        else:
            params[key] = str(value)
    params.setdefault("nf", 1) if kind == "mos" else None
    params.setdefault("m", 1) if kind == "mos" else None
    return HandoffDevice(name, kind, model, terminals, nodes, params, roles[name])


def _parse_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError(f"invalid numeric value: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    token = str(value).strip().lower()
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)(meg|[tgkmunpf])?", token)
    if not match:
        raise ValueError(f"unresolved or invalid engineering value: {value!r}")
    return float(match.group(1)) * _SUFFIXES.get(match.group(2) or "", 1.0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
