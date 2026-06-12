"""High-level Virtuoso export helpers."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import DEFAULT_DEVICE_MAP, DeviceMap, DeviceMapEntry
from .parser import parse_netlist
from .skill_writer import write_skill


def export_from_results(
    results_path: str | Path,
    lib_name: str,
    cell_name: str | None = None,
    out_path: str | Path | None = None,
    device_map_path: str | Path | None = None,
) -> dict[str, Any]:
    """Export a Virtuoso SKILL script from an optimizer results.json file."""
    results_path = Path(results_path)
    result_data = json.loads(results_path.read_text(encoding="utf-8"))

    netlist_ref = result_data.get("netlist_file")
    if not netlist_ref:
        raise ValueError(f"results.json does not contain 'netlist_file': {results_path}")

    netlist_path = Path(netlist_ref)
    if not netlist_path.is_absolute():
        netlist_path = (results_path.parent / netlist_path).resolve()

    if cell_name is None:
        cell_name = _default_cell_name(result_data, netlist_path)

    if out_path is None:
        out_path = results_path.parent / "virtuoso" / "import_schematic.il"

    return export_netlist(
        netlist_path=netlist_path,
        lib_name=lib_name,
        cell_name=cell_name,
        out_path=out_path,
        device_map_path=device_map_path,
        results_path=results_path,
    )


def export_netlist(
    netlist_path: str | Path,
    lib_name: str,
    cell_name: str,
    out_path: str | Path,
    device_map_path: str | Path | None = None,
    results_path: str | Path | None = None,
) -> dict[str, Any]:
    """Export a final rendered DUT netlist to a Virtuoso SKILL script."""
    netlist_path = Path(netlist_path)
    out_path = Path(out_path)
    device_map = load_device_map(device_map_path)
    ir = parse_netlist(netlist_path)
    skill = write_skill(ir, device_map, lib_name=lib_name, cell_name=cell_name)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(skill, encoding="utf-8")

    report = {
        "skill_file": str(out_path),
        "results_file": str(results_path) if results_path else None,
        "netlist_file": str(netlist_path),
        "target_lib": lib_name,
        "target_cell": cell_name,
        "target_view": "schematic",
        "subckt_name": ir.subckt_name,
        "ports": ir.ports,
        "nets": len(ir.nets),
        "instances": len(ir.instances),
        "models": sorted({inst.model for inst in ir.instances}),
        "device_map_file": str(device_map_path) if device_map_path else None,
    }
    report_path = out_path.parent / "export_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return report


def load_device_map(path: str | Path | None = None) -> DeviceMap:
    """Load a device map JSON file and merge it over the defaults."""
    device_map = dict(DEFAULT_DEVICE_MAP)
    if not path:
        return device_map

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key, value in data.items():
        if not isinstance(value, dict):
            raise ValueError(f"Device map entry for {key!r} must be an object")
        device_map[key] = DeviceMapEntry(
            lib=value["lib"],
            cell=value["cell"],
            view=value.get("view", "symbol"),
            term_order=list(value.get("term_order", [])),
            param_map=dict(value.get("param_map", {})),
        )
    return device_map


def default_device_map_json() -> str:
    """Return the default device map as pretty JSON for users to customize."""
    return json.dumps(
        {name: asdict(entry) for name, entry in DEFAULT_DEVICE_MAP.items()},
        indent=2,
        ensure_ascii=False,
    )


def _default_cell_name(result_data: dict[str, Any], netlist_path: Path) -> str:
    project_name = str(result_data.get("project_name") or netlist_path.stem)
    clean = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in project_name)
    clean = clean.strip("_") or netlist_path.stem
    return f"{clean}_opt"
