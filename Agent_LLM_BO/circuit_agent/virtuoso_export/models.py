"""Data models for Virtuoso schematic export."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pdk_profiles import get_pdk_profile


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


_PDK = get_pdk_profile()


DEFAULT_DEVICE_MAP: DeviceMap = {
    _PDK.pmos_lvt_model: DeviceMapEntry(
        lib=_PDK.virtuoso_tech_lib,
        cell=_PDK.pmos_lvt_model,
        view="symbol",
        term_order=["D", "G", "S", "B"],
        param_map={"W": "w", "L": "l", "nf": "nf", "m": "m"},
    ),
    _PDK.nmos_lvt_model: DeviceMapEntry(
        lib=_PDK.virtuoso_tech_lib,
        cell=_PDK.nmos_lvt_model,
        view="symbol",
        term_order=["D", "G", "S", "B"],
        param_map={"W": "w", "L": "l", "nf": "nf", "m": "m"},
    ),
    _PDK.pmos_model: DeviceMapEntry(
        lib=_PDK.virtuoso_tech_lib,
        cell=_PDK.pmos_model,
        view="symbol",
        term_order=["D", "G", "S", "B"],
        param_map={"W": "w", "L": "l", "nf": "nf", "m": "m"},
    ),
    _PDK.nmos_model: DeviceMapEntry(
        lib=_PDK.virtuoso_tech_lib,
        cell=_PDK.nmos_model,
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
