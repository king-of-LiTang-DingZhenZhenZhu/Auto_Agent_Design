"""Reusable route-template builders for native standard-cell style cells."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from analogskills.contracts import TopologyGraph
from analogskills.layout.stdcell_access import native_stdcell_terminal_xy
from analogskills.layout.stdcell_clusters import find_native_stdcell_sd_cluster
from analogskills.layout.stdcell_local_route import (
    NativeStdCellAccessCandidate,
    enumerate_native_stdcell_gate_access_candidates,
    enumerate_native_stdcell_sd_access_candidates,
)
from analogskills.layout.stdcell_topology import build_native_stdcell_route_bands, extract_native_stdcell_net_topology
from analogskills.pdk import PdkConfig

if TYPE_CHECKING:
    from .stdcell_primitives import NativeStdCellAccessCatalog, NativeStdCellFloorplan


@dataclass(frozen=True)
class NativeStdCellInputGateAccess:
    instance: str
    terminal: str
    default_xy: tuple[float, float]
    candidates: tuple[NativeStdCellAccessCandidate, ...] = ()


@dataclass(frozen=True)
class NativeStdCellInputRouteTemplate:
    net: str
    gate_points: tuple[tuple[float, float], ...]
    contact_xy: tuple[float, float]
    pin_xy: tuple[float, float]
    gate_accesses: tuple[NativeStdCellInputGateAccess, ...] = ()
    contact_x_candidates: tuple[float, ...] = ()
    gate_route_layer: str = "PO"
    collector_layers: tuple[str, ...] = ("M0", "M1", "M2")


@dataclass(frozen=True)
class NativeStdCellInternalRouteTemplate:
    net: str
    left_xy: tuple[float, float]
    right_xy: tuple[float, float]
    trunk_y: float
    route_style: str = "horizontal_bridge"
    bridge_x: float | None = None
    route_layer: str = "M1"
    via_defs: tuple[str, ...] = ("VIA0",)


@dataclass(frozen=True)
class NativeStdCellOutputRouteTemplate:
    net: str
    pin_xy: tuple[float, float]
    trunk_x: float
    trunk_bottom_y: float
    trunk_top_y: float
    pmos_bus_y: float
    pmos_points: tuple[tuple[float, float], ...]
    nmos_points: tuple[tuple[float, float], ...]
    trunk_layer: str = "M2"
    pmos_route_layer: str = "M1"
    nmos_via_defs: tuple[str, ...] = ("VIA0", "VIA1")
    pmos_via_defs: tuple[str, ...] = ("VIA0",)


@dataclass(frozen=True)
class NativeStdCellPowerRouteTemplate:
    net: str
    rail_y: float
    access_points: tuple[tuple[float, float], ...]
    route_style: str = "vertical_drops"
    bridge_x: float | None = None
    access_layer: str = "M1"
    rail_layer: str = "M2"
    access_via_defs: tuple[str, ...] = ("VIA0",)


@dataclass(frozen=True)
class NativeStdCellRouteTemplateSet:
    cell_bbox_um: tuple[float, float, float, float]
    input_templates: tuple[NativeStdCellInputRouteTemplate, ...]
    internal_template: NativeStdCellInternalRouteTemplate | None
    output_template: NativeStdCellOutputRouteTemplate
    power_templates: tuple[NativeStdCellPowerRouteTemplate, ...]
    topology: object
    color_by_net: dict[str, str] = field(default_factory=dict)
    color_by_segment: dict[tuple[str, str], str] = field(default_factory=dict)


def build_native_stdcell_route_templates(
    graph: TopologyGraph,
    floorplan: "NativeStdCellFloorplan",
    access_catalog: "NativeStdCellAccessCatalog",
    pdk: PdkConfig,
) -> NativeStdCellRouteTemplateSet:
    bands = build_native_stdcell_route_bands(floorplan, pdk)
    topology = extract_native_stdcell_net_topology(graph, available_pin_nets=set(floorplan.pin_columns))
    placement_by_device = {str(placement.name): placement for placement in floorplan.placements}

    def xy(instance: str, terminal: str) -> tuple[float, float]:
        return native_stdcell_terminal_xy(access_catalog, floorplan, pdk, instance, terminal)

    output_terminal_refs = tuple(
        (str(terminal_ref.device), str(terminal_ref.terminal))
        for terminal_ref in graph.nets[topology.output_net].terminals
        if terminal_ref.device in graph.devices and terminal_ref.terminal == "D"
    )
    internal_terms: tuple[tuple[str, str], ...] = ()
    if topology.internal_net:
        sd_cluster = find_native_stdcell_sd_cluster(graph, topology.internal_net, available_pin_nets=set(floorplan.pin_columns))
        internal_terms = tuple(sd_cluster.terminals) if sd_cluster is not None else ()

    input_templates: list[NativeStdCellInputRouteTemplate] = []
    for net_name in topology.input_nets:
        gate_devices = tuple(
            terminal_ref.device
            for terminal_ref in graph.nets[net_name].terminals
            if terminal_ref.device in graph.devices and terminal_ref.terminal == "G"
        )
        if not gate_devices:
            continue
        gate_accesses = tuple(
            NativeStdCellInputGateAccess(
                instance=str(device_name),
                terminal="G",
                default_xy=xy(device_name, "G"),
                candidates=enumerate_native_stdcell_gate_access_candidates(
                    access_catalog,
                    pdk,
                    instance=str(device_name),
                    terminal="G",
                    net=net_name,
                    target_y=bands.gate_y,
                ),
            )
            for device_name in gate_devices
        )
        gate_points = tuple(
            access.candidates[0].xy if access.candidates else access.default_xy
            for access in gate_accesses
        )
        gate_center_x = sum(point[0] for point in gate_points) / float(len(gate_points))
        # When the SMT floorplan already solved explicit boundary pin columns,
        # consume those columns directly instead of collapsing A/B back toward
        # the gate-centroid. Otherwise the template solve is not reflected in
        # the emitted routing geometry.
        if net_name in getattr(floorplan, "pin_columns", {}):
            pin_x = pdk.rules.snap_point_um((float(floorplan.pin_x(net_name)), 0.0))[0]
        else:
            pin_x = pdk.rules.snap_point_um((gate_center_x, 0.0))[0]
        contact_xy = pdk.rules.snap_point_um((pin_x, bands.gate_y))
        contact_x_candidates = _unique_snapped_xs(
            pdk,
            (
                pin_x,
                *(point[0] for point in gate_points),
                *(
                    candidate.xy[0]
                    for access in gate_accesses
                    for candidate in access.candidates
                ),
                gate_center_x,
            ),
        )
        input_templates.append(
            NativeStdCellInputRouteTemplate(
                net=net_name,
                gate_points=gate_points,
                contact_xy=contact_xy,
                pin_xy=contact_xy,
                gate_accesses=gate_accesses,
                contact_x_candidates=contact_x_candidates,
            )
        )

    internal_template = None
    if len(internal_terms) == 2:
        left_internal_xy = xy(internal_terms[0][0], internal_terms[0][1])
        right_internal_xy = xy(internal_terms[1][0], internal_terms[1][1])
        internal_route_layer = "M2" if str(getattr(pdk, "name", "")).lower() == "tsmcn7" else "M1"
        internal_trunk_y = (
            pdk.rules.snap_point_um((0.0, max(left_internal_xy[1], right_internal_xy[1])))[1]
            if internal_route_layer == "M2"
            else bands.internal_y
        )
        internal_template = NativeStdCellInternalRouteTemplate(
            net=topology.internal_net,
            left_xy=left_internal_xy,
            right_xy=right_internal_xy,
            trunk_y=internal_trunk_y,
            route_layer=internal_route_layer,
        )

    output_net = topology.output_net
    left_x, _, right_x, _ = floorplan.cell_bbox_um()
    del left_x
    # Keep the output pin near the right boundary, which matches compact
    # stdcell topology better and leaves the center channel available for
    # A/B gate-access escape.
    output_pin_x = pdk.rules.snap_point_um((right_x - 0.08, 0.0))[0]
    pmos_drain_points: list[tuple[float, float]] = []
    nmos_drain_points: list[tuple[float, float]] = []
    for device_name, terminal_name in output_terminal_refs:
        access_xy = _preferred_output_sd_access_xy(
            access_catalog,
            floorplan,
            pdk,
            instance=device_name,
            terminal=terminal_name,
            fallback_xy=xy(device_name, terminal_name),
        )
        if access_xy[1] >= float(floorplan.template.row_y_um["pmos"]):
            pmos_drain_points.append(access_xy)
        else:
            nmos_drain_points.append(access_xy)

    output_anchor_x = max([point[0] for point in (*pmos_drain_points, *nmos_drain_points)] or [output_pin_x])
    if str(getattr(pdk, "name", "")).lower() == "tsmcn7":
        # Reference 7nm stdcells keep the output collector as a horizontal
        # upper-metal bus and use lower-metal vertical branches from each
        # device access. Model trunk_x as the left edge of that bus.
        output_trunk_x = pdk.rules.snap_point_um((min(point[0] for point in (*pmos_drain_points, *nmos_drain_points)), 0.0))[0]
    else:
        # Reserve a dedicated right-side local channel for the output trunk so Z
        # does not share the same M1 column as the internal MID collector.
        access_clearance = max(
            float(floorplan.template.signal_width_um) + float(floorplan.template.boundary_pin_size_um),
            float(floorplan.template.device_pitch_um) * 0.25,
        )
        pin_stub_budget = max(
            float(floorplan.template.boundary_pin_size_um) * 3.0,
            float(floorplan.template.device_pitch_um) * 0.45,
        )
        preferred_output_trunk_x = max(output_anchor_x + access_clearance, output_pin_x - pin_stub_budget)
        output_trunk_x = pdk.rules.snap_point_um((min(preferred_output_trunk_x, right_x - 0.14), 0.0))[0]
    m2_signal_width = float(getattr(floorplan.template, "signal_width_um", 0.06))
    try:
        m2_spacing = float(pdk.rules.min_spacing_um("M2"))
    except Exception:
        m2_spacing = 0.04
    output_pin_y = max(
        float(bands.output_y),
        float(bands.gate_y) + m2_signal_width + m2_spacing,
    )
    output_pin_xy = pdk.rules.snap_point_um((output_pin_x, output_pin_y))
    # Reference 7nm stdcells keep the local output collector in the shared
    # center channel rather than hugging the PMOS drain row. When the local
    # collector moves to an upper-metal horizontal bus, keep the bus and pin on
    # the same track to avoid non-Manhattan fallback geometry.
    pmos_bus_y = output_pin_xy[1] if str(getattr(pdk, "name", "")).lower() == "tsmcn7" else pdk.rules.snap_point_um((0.0, bands.output_y))[1]
    trunk_bottom_y = min([output_pin_xy[1], *(point[1] for point in nmos_drain_points)] or [output_pin_xy[1]])
    trunk_top_y = max([output_pin_xy[1], pmos_bus_y])
    output_template = NativeStdCellOutputRouteTemplate(
        net=output_net,
        pin_xy=output_pin_xy,
        trunk_x=output_trunk_x,
        trunk_bottom_y=trunk_bottom_y,
        trunk_top_y=trunk_top_y,
        pmos_bus_y=pmos_bus_y,
        pmos_points=tuple(pmos_drain_points),
        nmos_points=tuple(nmos_drain_points),
        trunk_layer="M2" if str(getattr(pdk, "name", "")).lower() == "tsmcn7" else "M1",
    )

    power_templates: list[NativeStdCellPowerRouteTemplate] = []
    for net_name, terminal_name, rail_y in (
        ("VDD", "S", bands.vdd_y),
        ("VSS", "S", bands.vss_y),
    ):
        # In the N7 1X_h stack, M2 should stay horizontal-only. Local rail
        # drops therefore need to use M1 for vertical access and only touch
        # M2 at the final VIA1 landing on the rail.
        access_layer = "M1"
        access_via_defs = ("VIA0",)
        access_points = tuple(
            _preferred_power_sd_access_xy(
                access_catalog,
                floorplan,
                pdk,
                instance=str(terminal_ref.device),
                terminal=terminal_name,
                fallback_xy=xy(terminal_ref.device, terminal_name),
            )
            for terminal_ref in graph.nets[net_name].terminals
            if terminal_ref.device in graph.devices and terminal_ref.terminal == terminal_name
        )
        power_templates.append(
            NativeStdCellPowerRouteTemplate(
                net=net_name,
                rail_y=rail_y,
                access_points=access_points,
                access_layer=access_layer,
                access_via_defs=access_via_defs,
            )
        )

    return NativeStdCellRouteTemplateSet(
        cell_bbox_um=floorplan.cell_bbox_um(),
        input_templates=tuple(input_templates),
        internal_template=internal_template,
        output_template=output_template,
        power_templates=tuple(power_templates),
        topology=topology,
    )


def _unique_snapped_xs(
    pdk: PdkConfig,
    values: tuple[float, ...],
) -> tuple[float, ...]:
    seen: list[float] = []
    for value in values:
        snapped = float(pdk.rules.snap_point_um((float(value), 0.0))[0])
        if any(abs(existing - snapped) <= 1e-9 for existing in seen):
            continue
        seen.append(snapped)
    return tuple(seen)


def _preferred_output_sd_access_xy(
    access_catalog: "NativeStdCellAccessCatalog",
    floorplan: "NativeStdCellFloorplan",
    pdk: PdkConfig,
    *,
    instance: str,
    terminal: str,
    fallback_xy: tuple[float, float],
) -> tuple[float, float]:
    candidates = enumerate_native_stdcell_sd_access_candidates(
        access_catalog,
        floorplan,
        pdk,
        instance=instance,
        terminal=terminal,
        net="Z",
    )
    if not candidates:
        return tuple(float(v) for v in fallback_xy)
    return max(
        (tuple(float(v) for v in candidate.xy) for candidate in candidates),
        key=lambda xy: (xy[0], -abs(xy[1])),
    )


def _preferred_power_sd_access_xy(
    access_catalog: "NativeStdCellAccessCatalog",
    floorplan: "NativeStdCellFloorplan",
    pdk: PdkConfig,
    *,
    instance: str,
    terminal: str,
    fallback_xy: tuple[float, float],
) -> tuple[float, float]:
    candidates = enumerate_native_stdcell_sd_access_candidates(
        access_catalog,
        floorplan,
        pdk,
        instance=instance,
        terminal=terminal,
        net="POWER",
    )
    if not candidates:
        return tuple(float(v) for v in fallback_xy)
    cell_center_x = (float(floorplan.cell_bbox_um()[0]) + float(floorplan.cell_bbox_um()[2])) / 2.0
    return min(
        (tuple(float(v) for v in candidate.xy) for candidate in candidates),
        key=lambda xy: (abs(xy[0] - cell_center_x), abs(xy[1])),
    )
