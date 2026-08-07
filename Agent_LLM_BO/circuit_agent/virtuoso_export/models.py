"""Data models for Virtuoso schematic export."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pdk_profiles import PDKProfile, get_pdk_profile


DeviceKind = Literal["mos", "res", "cap"]


@dataclass(frozen=True)
class DeviceMapEntry:
    """Mapping from netlist model/kind to a Virtuoso symbol."""

    lib: str
    cell: str
    view: str = "symbol"
    term_order: list[str] = field(default_factory=list)
    param_map: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Instance:
    """A parsed schematic instance."""

    name: str
    kind: DeviceKind
    model: str
    nodes: list[str]
    params: dict[str, str]


@dataclass(frozen=True)
class SchematicIR:
    """Topology-neutral intermediate representation for schematic export."""

    subckt_name: str
    ports: list[str]
    instances: list[Instance]
    nets: list[str]


DeviceMap = dict[str, DeviceMapEntry]


def default_device_map(profile: PDKProfile | None = None) -> DeviceMap:
    """Build the default map from the active PDK at call time."""
    pdk = profile or get_pdk_profile()
    device_map: DeviceMap = {
        pdk.pmos_lvt_model: DeviceMapEntry(
            lib=pdk.virtuoso_tech_lib,
            cell=pdk.pmos_lvt_model,
            view="symbol",
            term_order=["D", "G", "S", "B"],
            param_map={"W": "w", "L": "l", "nf": "nf", "m": "m"},
        ),
        pdk.nmos_lvt_model: DeviceMapEntry(
            lib=pdk.virtuoso_tech_lib,
            cell=pdk.nmos_lvt_model,
            view="symbol",
            term_order=["D", "G", "S", "B"],
            param_map={"W": "w", "L": "l", "nf": "nf", "m": "m"},
        ),
        pdk.pmos_model: DeviceMapEntry(
            lib=pdk.virtuoso_tech_lib,
            cell=pdk.pmos_model,
            view="symbol",
            term_order=["D", "G", "S", "B"],
            param_map={"W": "w", "L": "l", "nf": "nf", "m": "m"},
        ),
        pdk.nmos_model: DeviceMapEntry(
            lib=pdk.virtuoso_tech_lib,
            cell=pdk.nmos_model,
            view="symbol",
            term_order=["D", "G", "S", "B"],
            param_map={"W": "w", "L": "l", "nf": "nf", "m": "m"},
        ),
        "res": DeviceMapEntry(
            lib="analogLib",
            cell="res",
            view="symbol",
            term_order=["PLUS", "MINUS"],
            param_map={"R": "r"},
        ),
        "cap": DeviceMapEntry(
            lib="analogLib",
            cell="cap",
            view="symbol",
            term_order=["PLUS", "MINUS"],
            param_map={"C": "c"},
        ),
    }
    for device in pdk.passive_devices.values():
        param_map = dict(device.parameter_map)
        param_map.setdefault(
            "W", param_map.get(device.width_parameter, device.width_parameter)
        )
        param_map.setdefault(
            "L", param_map.get(device.length_parameter, device.length_parameter)
        )
        if device.value_parameter:
            canonical = (
                "R" if device.kind == "resistor" and device.value_parameter.lower() == "r"
                else "C" if device.kind == "capacitor" and device.value_parameter.lower() == "c"
                else device.value_parameter
            )
            param_map.setdefault(
                canonical,
                param_map.get(device.value_parameter, device.value_parameter),
            )
        device_map[device.spectre_model] = DeviceMapEntry(
            lib=device.virtuoso_lib,
            cell=device.virtuoso_cell,
            view=device.virtuoso_view,
            term_order=list(device.term_order),
            param_map=param_map,
        )
    return device_map


DEFAULT_DEVICE_MAP: DeviceMap = default_device_map()
