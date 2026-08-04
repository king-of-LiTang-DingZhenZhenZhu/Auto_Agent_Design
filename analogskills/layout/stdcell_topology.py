from __future__ import annotations

from dataclasses import dataclass

from analogskills.contracts import TopologyGraph
from analogskills.pdk import PdkConfig


@dataclass(frozen=True)
class NativeStdCellNetTopology:
    input_nets: tuple[str, ...]
    output_net: str
    internal_net: str


@dataclass(frozen=True)
class NativeStdCellRouteBands:
    gate_y: float
    internal_y: float
    output_y: float
    vdd_y: float
    vss_y: float


def extract_native_stdcell_net_topology(
    graph: TopologyGraph,
    *,
    available_pin_nets: set[str],
) -> NativeStdCellNetTopology:
    input_nets = tuple(
        name
        for name, role in graph.pins.items()
        if str(role.value) == "input" and name in available_pin_nets
    )
    output_nets = tuple(
        name
        for name, role in graph.pins.items()
        if str(role.value) == "output" and name in available_pin_nets
    )
    if len(output_nets) != 1:
        raise RuntimeError(f"native stdcell route synthesis expects exactly one output pin, found {output_nets}")
    internal_nets = tuple(
        name
        for name, net in graph.nets.items()
        if str(net.role.value) == "internal"
    )
    if len(internal_nets) > 1:
        raise RuntimeError(f"native stdcell route synthesis currently supports at most one internal net, found {internal_nets}")
    return NativeStdCellNetTopology(
        input_nets=input_nets,
        output_net=output_nets[0],
        internal_net=internal_nets[0] if internal_nets else "",
    )


def build_native_stdcell_route_bands(floorplan: object, pdk: PdkConfig) -> NativeStdCellRouteBands:
    template = getattr(floorplan, "template")
    return NativeStdCellRouteBands(
        gate_y=pdk.rules.snap_point_um((0.0, template.band_y_um["gate"]))[1],
        internal_y=pdk.rules.snap_point_um((0.0, template.band_y_um["internal"]))[1],
        output_y=pdk.rules.snap_point_um((0.0, template.band_y_um["output"]))[1],
        vdd_y=pdk.rules.snap_point_um((0.0, template.rail_y_um["VDD"]))[1],
        vss_y=pdk.rules.snap_point_um((0.0, template.rail_y_um["VSS"]))[1],
    )
