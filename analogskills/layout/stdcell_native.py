"""Native standard-cell pipeline for advanced-node complementary CMOS gates.

This module is intentionally separate from ``analogskills.layout.standard_cell``.
It models a standard cell as a fixed-height template with:

- explicit placement columns for transistor rows
- explicit pin columns for top/boundary pins
- template-owned rail, gate, internal, and output routing bands
- real PCell terminal-access extraction after placement

The first implementation targets 7nm-native complementary CMOS gates and is
used to migrate NAND2 away from the legacy abstract standard-cell flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Mapping

from analogskills.contracts import TopologyGraph
from analogskills.env import get_env
from analogskills.layout.stdcell_carriers import NativeStdCellCarrier
from analogskills.layout.stdcell_primitives import (
    NativeStdCellAccessCatalog,
    NativeStdCellFloorplan,
    NativeStdCellTemplate,
    build_n7_native_stdcell_template,
    build_native_stdcell_floorplan,
)
from analogskills.layout.stdcell_route_templates import build_native_stdcell_route_templates
from analogskills.layout.stdcell_smt import NativeStdCellPlacementProblem, NativeStdCellPlacementSolution, build_native_stdcell_placement_problem, solve_native_stdcell_placement
from analogskills.pdk import PdkConfig


@dataclass(frozen=True)
class NativeStdCellRouteResult:
    plan: Any
    boundary_pins: tuple[Any, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)


def extract_native_stdcell_access_catalog(
    pcell_plan: object,
    pdk: PdkConfig,
    *,
    calibration_cache: object | None = None,
) -> NativeStdCellAccessCatalog:
    from analogskills.pcell import PCellTerminalAccessor

    accessor = PCellTerminalAccessor(pdk, calibration_cache=calibration_cache)
    pins_by_key: dict[tuple[str, str], tuple[PCellPin, ...]] = {}
    breakout_by_key: dict[tuple[str, str], PCellPin] = {}
    for instance in tuple(getattr(pcell_plan, "instances", ())):
        for terminal, preferred_layers in (
            ("G", ("PO",)),
            ("S", ("M0", "MD", "OD")),
            ("D", ("M0", "MD", "OD")),
        ):
            key = (str(instance.name), str(terminal))
            pins = accessor.get_terminal_pins(instance, terminal, preferred_layers=preferred_layers)
            if not pins:
                continue
            pins_by_key[key] = tuple(pins)
            breakout_by_key[key] = accessor.select_terminal_breakout(
                instance,
                terminal,
                require_lvs_safe=True,
                preferred_layers=preferred_layers,
            )
    return NativeStdCellAccessCatalog(
        pins_by_instance_terminal=pins_by_key,
        breakout_by_instance_terminal=breakout_by_key,
    )


def extract_native_stdcell_access_catalog_from_primitive_carriers(
    floorplan: NativeStdCellFloorplan,
    carriers: tuple[NativeStdCellCarrier, ...],
    *,
    nfin_by_model: Mapping[str, int] | None = None,
    pcell_plan: object | None = None,
    pdk: PdkConfig | None = None,
    calibration_cache: object | None = None,
) -> NativeStdCellAccessCatalog:
    if not carriers:
        raise ValueError("primitive carrier access extraction requires at least one carrier")
    return NativeStdCellAccessCatalog.from_primitive_clusters(
        floorplan,
        carriers,
        nfin_by_model=nfin_by_model,
        pcell_plan=pcell_plan,
        pdk=pdk,
        calibration_cache=calibration_cache,
    )


def synthesize_n7_native_cmos_route_plan(
    graph: TopologyGraph,
    pcell_plan: object,
    floorplan: NativeStdCellFloorplan,
    access_catalog: NativeStdCellAccessCatalog,
    pdk: PdkConfig,
    *,
    lib: str,
    cell: str,
) -> NativeStdCellRouteResult:
    from analogskills.layout.physical import analyze_plan_physical_connectivity

    inst_map = {str(inst.name): inst for inst in getattr(pcell_plan, "instances", ())}
    required = tuple(sorted(graph.devices))
    missing = tuple(name for name in required if name not in inst_map)
    if missing:
        raise RuntimeError(f"native stdcell route plan missing placed instances: {', '.join(missing)}")
    base_route_templates = build_native_stdcell_route_templates(graph, floorplan, access_catalog, pdk)
    route_templates = base_route_templates
    route_template_source = "baseline"
    disable_detailed_route = _env_flag("N7_DISABLE_DETAILED_STDCELL_ROUTE", False)
    detailed_route_metadata: Mapping[str, object]
    if disable_detailed_route:
        detailed_route_metadata = {
            "attempted": False,
            "candidate_available": False,
            "applied": False,
            "disabled_by_env": True,
        }
    else:
        route_templates, detailed_route_metadata = _refine_route_templates_with_detailed_smt(
            graph,
            floorplan,
            access_catalog,
            pdk,
            base_route_templates,
        )
        if detailed_route_metadata.get("candidate_available"):
            candidate_plan, candidate_boundary_pins = _build_native_route_plan_from_templates(
                graph,
                floorplan,
                route_templates,
                pdk,
                lib=lib,
                cell=cell,
            )
            candidate_precheck = analyze_plan_physical_connectivity(
                candidate_plan,
                pdk=pdk,
                include_via_landing_shorts=True,
            )
            detailed_route_metadata = {
                **dict(detailed_route_metadata),
                "route_precheck_passed": bool(candidate_precheck.get("passed")),
                "route_precheck_issues": tuple(candidate_precheck.get("issues", ())),
            }
            if candidate_precheck.get("passed"):
                plan = candidate_plan
                boundary_pins = candidate_boundary_pins
                route_template_source = "detailed_smt"
                detailed_route_metadata = {**dict(detailed_route_metadata), "applied": True}
            else:
                route_templates = base_route_templates
                detailed_route_metadata = {**dict(detailed_route_metadata), "applied": False, "fallback_reason": "route_precheck_failed"}
        else:
            detailed_route_metadata = {**dict(detailed_route_metadata), "applied": False}

    if route_template_source == "baseline":
        plan, boundary_pins = _build_native_route_plan_from_templates(
            graph,
            floorplan,
            base_route_templates,
            pdk,
            lib=lib,
            cell=cell,
        )

    topology = route_templates.topology
    if route_template_source == "baseline":
        topology = base_route_templates.topology
        route_templates = base_route_templates
    else:
        topology = route_templates.topology
    input_nets = topology.input_nets
    output_net = topology.output_net
    internal_net = topology.internal_net
    return NativeStdCellRouteResult(
        plan=plan,
        boundary_pins=tuple(boundary_pins),
        metadata={
            "cell_bbox_um": floorplan.cell_bbox_um(),
            "template": floorplan.template.name,
            "input_nets": input_nets,
            "output_net": output_net,
            "internal_net": internal_net,
            "route_template_source": route_template_source,
            "detailed_route": detailed_route_metadata,
        },
    )


def _env_flag(name: str, default: bool) -> bool:
    raw = get_env(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _build_native_route_plan_from_templates(
    graph: TopologyGraph,
    floorplan: NativeStdCellFloorplan,
    route_templates: object,
    pdk: PdkConfig,
    *,
    lib: str,
    cell: str,
) -> tuple[object, tuple[object, ...]]:
    from analogskills.eda import OaCellView, OaPath, OaPin, OaRect, OaVia, OaWritePlan

    bbox = floorplan.cell_bbox_um()
    left_x, _, right_x, _ = bbox
    topology = route_templates.topology
    signal_w = pdk.rules.snap_dimension_um(floorplan.template.signal_width_um)
    rail_w = pdk.rules.snap_dimension_um(floorplan.template.rail_width_um)
    pin_half = floorplan.template.boundary_pin_size_um / 2.0
    default_gate_pin_layer = floorplan.template.pin_layers.get("A", "M2")
    output_pin_layer = floorplan.template.pin_layers.get("Z", "M2")

    # Keep stream-out pin markers contained inside the real route metal.
    # Reference stdcells rely on labels over existing metal rather than
    # standalone pin blocks, so the OA pin bbox here should be smaller than
    # the host route shape.
    pin_meta_half_x = pdk.rules.snap_dimension_um(min(pin_half, signal_w / 3.0))
    pin_meta_half_y = pdk.rules.snap_dimension_um(min(pin_half, signal_w / 3.0))

    def pin_bbox(
        center_xy: tuple[float, float],
        *,
        half_x: float = pin_meta_half_x,
        half_y: float = pin_meta_half_y,
    ) -> tuple[float, float, float, float]:
        x, y = center_xy
        return pdk.rules.snap_bbox_um((x - half_x, y - half_y, x + half_x, y + half_y), mode="nearest")

    def pad_rect(layer: str, center_xy: tuple[float, float], net: str, *, size: float = 0.08, color_role: str = "") -> OaRect:
        x, y = center_xy
        half = size / 2.0
        return OaRect(
            layer,
            "drawing",
            pdk.rules.snap_bbox_um((x - half, y - half, x + half, y + half), mode="nearest"),
            net,
            _shape_color(layer, net, color_role=color_role),
        )

    def poly_contact_head_rect(center_xy: tuple[float, float], net: str) -> OaRect:
        x, y = center_xy
        # Keep the PO landing head as tight as possible around the vertical
        # gate stem so center-channel gate access matches compact stdcell
        # topology instead of widening into neighboring FEOL keepouts.
        half_x = pdk.rules.snap_dimension_um(floorplan.template.gate_poly_width_um) / 2.0
        half_y = 0.027
        return OaRect(
            "PO",
            "drawing",
            pdk.rules.snap_bbox_um((x - half_x, y - half_y, x + half_x, y + half_y), mode="nearest"),
            net,
        )

    def _shape_color(layer: str, net: str, *, color_role: str = "") -> str:
        if layer not in {"M0", "M1", "M2"}:
            return ""
        if color_role:
            segment_color = str(getattr(route_templates, "color_by_segment", {}).get((net, color_role), ""))
            if segment_color:
                return segment_color
        return str(getattr(route_templates, "color_by_net", {}).get(net, ""))

    def add_via_with_pads(
        via_def: str,
        center_xy: tuple[float, float],
        net: str,
        *,
        lower_layer: str | None,
        upper_layer: str | None,
        lower_size: float = 0.08,
        upper_size: float = 0.08,
        lower_role: str = "",
        upper_role: str = "",
    ) -> None:
        vias.append(OaVia(via_def, center_xy, net))
        if via_def in {"VIA0", "VIA1"}:
            if via_def == "VIA1" and upper_layer and upper_role in {"pin", "pin_landing"}:
                rects.append(pad_rect(upper_layer, center_xy, net, size=upper_size, color_role=upper_role))
            return
        if via_def == "M0_PO" and lower_layer == "PO":
            return
        elif lower_layer:
            rects.append(pad_rect(lower_layer, center_xy, net, size=lower_size, color_role=lower_role))
        if upper_layer:
            rects.append(pad_rect(upper_layer, center_xy, net, size=upper_size, color_role=upper_role))

    input_nets = topology.input_nets
    output_net = topology.output_net
    internal_net = topology.internal_net

    def _compact_points(*points: tuple[float, float]) -> tuple[tuple[float, float], ...]:
        compact: list[tuple[float, float]] = []
        for point in points:
            if compact and abs(compact[-1][0] - point[0]) <= 1e-9 and abs(compact[-1][1] - point[1]) <= 1e-9:
                continue
            compact.append(point)
        return tuple(compact)

    def _append_path_if_nonzero(
        layer: str,
        points: tuple[tuple[float, float], ...],
        width: float,
        net: str,
        *,
        color_role: str = "",
    ) -> None:
        compact = _compact_points(*points)
        if len(compact) < 2:
            return
        if all(abs(left[0] - right[0]) <= 1e-9 and abs(left[1] - right[1]) <= 1e-9 for left, right in zip(compact, compact[1:])):
            return
        color = _shape_color(layer, net, color_role=color_role)
        canonical_points = min(compact, tuple(reversed(compact)))
        path_key = (
            str(layer),
            str(net),
            str(color),
            f"{float(width):.9f}",
            tuple((round(float(x), 9), round(float(y), 9)) for x, y in canonical_points),
        )
        if path_key in seen_path_keys:
            return
        segment_keys: list[tuple[str, str, str, str, tuple[float, float], tuple[float, float]]] = []
        for left, right in zip(compact, compact[1:]):
            if abs(left[0] - right[0]) <= 1e-9 and abs(left[1] - right[1]) <= 1e-9:
                continue
            left_xy = (round(float(left[0]), 9), round(float(left[1]), 9))
            right_xy = (round(float(right[0]), 9), round(float(right[1]), 9))
            ordered_left, ordered_right = min((left_xy, right_xy), (right_xy, left_xy))
            segment_keys.append((str(layer), str(net), str(color), f"{float(width):.9f}", ordered_left, ordered_right))
        if segment_keys and all(key in seen_segment_keys for key in segment_keys):
            return
        seen_path_keys.add(path_key)
        for key in segment_keys:
            seen_segment_keys.add(key)
        paths.append(OaPath(layer, "drawing", compact, width, net, color))

    def _append_via_stack(
        center_xy: tuple[float, float],
        net: str,
        via_defs: tuple[str, ...],
    ) -> None:
        for via_def in via_defs:
            add_via_with_pads(via_def, center_xy, net, lower_layer=None, upper_layer=None)

    def _append_input_pin_metal(
        center_xy: tuple[float, float],
        net: str,
        layer: str,
    ) -> None:
        del center_xy, net, layer
        # The VIA1 upper landing already emits M2 geometry for the input pin.
        # Adding an extra horizontal M2 bar here causes artificial A/B shorts
        # in compact templates where adjacent inputs intentionally sit close.
        return

    def _extend_vertical_segment(
        start_xy: tuple[float, float],
        end_xy: tuple[float, float],
        *,
        min_length_um: float,
        extend_from: str,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        if abs(start_xy[0] - end_xy[0]) > 1e-9:
            return start_xy, end_xy
        current_length = abs(end_xy[1] - start_xy[1])
        if current_length <= 1e-9:
            return start_xy, end_xy
        if current_length + 1e-9 >= min_length_um:
            return start_xy, end_xy
        delta = min_length_um - current_length
        if extend_from == "start":
            direction = -1.0 if start_xy[1] < end_xy[1] else 1.0
            adjusted_start = (start_xy[0], pdk.rules.snap_point_um((start_xy[0], start_xy[1] + direction * delta))[1])
            return adjusted_start, end_xy
        direction = 1.0 if end_xy[1] > start_xy[1] else -1.0
        adjusted_end = (end_xy[0], pdk.rules.snap_point_um((end_xy[0], end_xy[1] + direction * delta))[1])
        return start_xy, adjusted_end

    rail_inset = rail_w / 2.0
    rail_left_x = left_x + rail_inset
    rail_right_x = right_x - rail_inset
    paths: list[OaPath] = [
        OaPath("M2", "drawing", ((rail_left_x, power_template.rail_y), (rail_right_x, power_template.rail_y)), rail_w, power_template.net, _shape_color("M2", power_template.net, color_role="rail"))
        for power_template in route_templates.power_templates
    ]
    seen_path_keys: set[tuple[str, str, str, str, tuple[tuple[float, float], ...]]] = set()
    seen_segment_keys: set[tuple[str, str, str, str, tuple[float, float], tuple[float, float]]] = set()
    vias: list[OaVia] = []
    rects: list[OaRect] = []
    boundary_pins: list[OaPin] = []

    for input_template in route_templates.input_templates:
        input_pin_layer = floorplan.template.pin_layers.get(input_template.net, default_gate_pin_layer)
        gate_points = tuple(input_template.gate_points)
        trunk_x = input_template.contact_xy[0]
        if gate_points:
            trunk_bottom_y = min([input_template.contact_xy[1], *(point[1] for point in gate_points)])
            trunk_top_y = max([input_template.contact_xy[1], *(point[1] for point in gate_points)])
            _append_path_if_nonzero(
                input_template.gate_route_layer,
                ((trunk_x, trunk_bottom_y), (trunk_x, trunk_top_y)),
                pdk.rules.snap_dimension_um(floorplan.template.gate_poly_width_um),
                input_template.net,
            )
        for gate_xy in input_template.gate_points:
            if abs(gate_xy[0] - trunk_x) <= 1e-9:
                continue
            _append_path_if_nonzero(
                input_template.gate_route_layer,
                (gate_xy, (trunk_x, gate_xy[1])),
                pdk.rules.snap_dimension_um(floorplan.template.gate_poly_width_um),
                input_template.net,
            )
        _append_input_pin_metal(input_template.pin_xy, input_template.net, input_pin_layer)
        add_via_with_pads("M0_PO", input_template.contact_xy, input_template.net, lower_layer="PO", upper_layer="M0", lower_size=0.08, upper_size=0.08)
        add_via_with_pads("VIA0", input_template.contact_xy, input_template.net, lower_layer="M0", upper_layer="M1", lower_size=0.08, upper_size=0.08, upper_role="contact")
        add_via_with_pads("VIA1", input_template.contact_xy, input_template.net, lower_layer="M1", upper_layer="M2", lower_size=0.08, upper_size=0.08, lower_role="contact", upper_role="pin")
        boundary_pins.append(
            OaPin(
                input_template.net,
                input_template.net,
                "inputOutput",
                input_pin_layer,
                pin_bbox(input_template.pin_xy),
                emit_draw_rect=False,
            )
        )

    internal_template = route_templates.internal_template
    if internal_template is not None:
        if internal_template.route_layer == "M2" and internal_template.route_style == "horizontal_bridge":
            bus_left_x = min(internal_template.left_xy[0], internal_template.right_xy[0])
            bus_right_x = max(internal_template.left_xy[0], internal_template.right_xy[0])
            left_branch_points = _extend_vertical_segment(
                internal_template.left_xy,
                (internal_template.left_xy[0], internal_template.trunk_y),
                min_length_um=0.10,
                extend_from="start",
            )
            right_branch_points = _extend_vertical_segment(
                internal_template.right_xy,
                (internal_template.right_xy[0], internal_template.trunk_y),
                min_length_um=0.10,
                extend_from="start",
            )
            _append_path_if_nonzero("M1", left_branch_points, signal_w, internal_template.net, color_role="left_branch")
            _append_path_if_nonzero("M1", right_branch_points, signal_w, internal_template.net, color_role="right_branch")
            _append_path_if_nonzero("M2", ((bus_left_x, internal_template.trunk_y), (bus_right_x, internal_template.trunk_y)), signal_w, internal_template.net, color_role="bridge")
            add_via_with_pads("VIA0", internal_template.left_xy, internal_template.net, lower_layer=None, upper_layer="M1", upper_role="left_branch")
            add_via_with_pads("VIA0", internal_template.right_xy, internal_template.net, lower_layer=None, upper_layer="M1", upper_role="right_branch")
            add_via_with_pads("VIA1", (internal_template.left_xy[0], internal_template.trunk_y), internal_template.net, lower_layer="M1", upper_layer="M2", lower_role="left_branch", upper_role="bridge")
            add_via_with_pads("VIA1", (internal_template.right_xy[0], internal_template.trunk_y), internal_template.net, lower_layer="M1", upper_layer="M2", lower_role="right_branch", upper_role="bridge")
        elif internal_template.route_style == "shared_vertical_bridge" and internal_template.bridge_x is not None:
            bridge_x = internal_template.bridge_x
            _append_path_if_nonzero(internal_template.route_layer, (internal_template.left_xy, (bridge_x, internal_template.left_xy[1])), signal_w, internal_template.net, color_role="left_branch")
            _append_path_if_nonzero(internal_template.route_layer, (internal_template.right_xy, (bridge_x, internal_template.right_xy[1])), signal_w, internal_template.net, color_role="right_branch")
            _append_path_if_nonzero(
                internal_template.route_layer,
                ((bridge_x, min(internal_template.left_xy[1], internal_template.right_xy[1])), (bridge_x, max(internal_template.left_xy[1], internal_template.right_xy[1]))),
                signal_w,
                internal_template.net,
                color_role="bridge",
            )
        else:
            _append_path_if_nonzero(internal_template.route_layer, (internal_template.left_xy, (internal_template.left_xy[0], internal_template.trunk_y)), signal_w, internal_template.net, color_role="left_branch")
            _append_path_if_nonzero(internal_template.route_layer, (internal_template.right_xy, (internal_template.right_xy[0], internal_template.trunk_y)), signal_w, internal_template.net, color_role="right_branch")
            _append_path_if_nonzero(
                internal_template.route_layer,
                ((internal_template.left_xy[0], internal_template.trunk_y), (internal_template.right_xy[0], internal_template.trunk_y)),
                signal_w,
                internal_template.net,
                color_role="bridge",
            )
        if internal_template.route_layer != "M0" and not (internal_template.route_layer == "M2" and internal_template.route_style == "horizontal_bridge"):
            add_via_with_pads("VIA0", internal_template.left_xy, internal_template.net, lower_layer=None, upper_layer=internal_template.route_layer, upper_role="left_branch")
            add_via_with_pads("VIA0", internal_template.right_xy, internal_template.net, lower_layer=None, upper_layer=internal_template.route_layer, upper_role="right_branch")

    output_template = route_templates.output_template
    output_local_layer = output_template.trunk_layer
    output_pin_layer = floorplan.template.pin_layers.get(output_template.net, output_pin_layer)
    output_bus_y = output_template.pmos_bus_y
    if output_local_layer == "M2":
        bus_left_x = min(output_template.trunk_x, *(point[0] for point in (*output_template.nmos_points, *output_template.pmos_points)))
        _append_path_if_nonzero(
            output_local_layer,
            ((bus_left_x, output_bus_y), output_template.pin_xy),
            signal_w,
            output_template.net,
            color_role="trunk",
        )
        for access_xy in output_template.nmos_points:
            nmos_route_points = _compact_points(
                access_xy,
                (access_xy[0], output_bus_y),
            )
            _append_path_if_nonzero("M1", nmos_route_points, signal_w, output_template.net, color_role="nmos_branch")
            add_via_with_pads("VIA0", access_xy, output_template.net, lower_layer=None, upper_layer="M1", upper_role="nmos_branch")
            add_via_with_pads("VIA1", (access_xy[0], output_bus_y), output_template.net, lower_layer="M1", upper_layer="M2", lower_role="nmos_branch", upper_role="trunk")
        for access_xy in output_template.pmos_points:
            pmos_route_points = _compact_points(
                access_xy,
                (access_xy[0], output_bus_y),
            )
            _append_path_if_nonzero("M1", pmos_route_points, signal_w, output_template.net, color_role="pmos_branch")
            add_via_with_pads("VIA0", access_xy, output_template.net, lower_layer=None, upper_layer="M1", upper_role="pmos_branch")
            add_via_with_pads("VIA1", (access_xy[0], output_bus_y), output_template.net, lower_layer="M1", upper_layer="M2", lower_role="pmos_branch", upper_role="trunk")
    else:
        _append_path_if_nonzero(
            output_local_layer,
            ((output_template.trunk_x, output_template.trunk_bottom_y), (output_template.trunk_x, output_template.trunk_top_y)),
            signal_w,
            output_template.net,
            color_role="trunk",
        )
        pin_tap_xy = (output_template.trunk_x, output_template.pin_xy[1])
        if output_pin_layer == "M2":
            add_via_with_pads("VIA1", pin_tap_xy, output_template.net, lower_layer="M1", upper_layer="M2", lower_role="trunk", upper_role="pin_landing")
            if abs(output_template.pin_xy[0] - output_template.trunk_x) > 1e-6:
                _append_path_if_nonzero("M2", (pin_tap_xy, output_template.pin_xy), signal_w, output_template.net, color_role="pin_stub")
        elif output_template.pin_xy != pin_tap_xy:
            _append_path_if_nonzero(output_local_layer, (pin_tap_xy, output_template.pin_xy), signal_w, output_template.net, color_role="pin_stub")
        for access_xy in output_template.nmos_points:
            nmos_route_points = _compact_points(
                access_xy,
                (access_xy[0], output_bus_y),
                (output_template.trunk_x, output_bus_y),
            )
            _append_path_if_nonzero(output_local_layer, nmos_route_points, signal_w, output_template.net, color_role="nmos_branch")
            add_via_with_pads("VIA0", access_xy, output_template.net, lower_layer=None, upper_layer="M1", upper_role="nmos_branch")
        for access_xy in output_template.pmos_points:
            pmos_route_points = _compact_points(
                access_xy,
                (access_xy[0], output_bus_y),
                (output_template.trunk_x, output_bus_y),
            )
            _append_path_if_nonzero(output_local_layer, pmos_route_points, signal_w, output_template.net, color_role="pmos_branch")
            add_via_with_pads("VIA0", access_xy, output_template.net, lower_layer=None, upper_layer="M1", upper_role="pmos_branch")
    boundary_pins.append(
        OaPin(
            output_template.net,
            output_template.net,
            "inputOutput",
            output_pin_layer,
            pin_bbox(output_template.pin_xy),
            emit_draw_rect=False,
        )
    )

    rail_pin_boxes: dict[str, tuple[float, float, float, float]] = {}
    for power_template in route_templates.power_templates:
        if power_template.route_style == "shared_drop" and power_template.bridge_x is not None and power_template.access_points:
            branch_y = power_template.access_points[0][1]
            rail_tap_xy = (power_template.bridge_x, power_template.rail_y)
            branch_base_xy = (power_template.bridge_x, branch_y)
            for access_xy in power_template.access_points:
                _append_path_if_nonzero(power_template.access_layer, (access_xy, branch_base_xy), signal_w, power_template.net, color_role="drop")
                _append_via_stack(access_xy, power_template.net, power_template.access_via_defs)
            if abs(branch_y - power_template.rail_y) > 1e-9:
                _append_path_if_nonzero(power_template.access_layer, (branch_base_xy, rail_tap_xy), signal_w, power_template.net, color_role="shared_drop")
            if power_template.access_layer != power_template.rail_layer:
                add_via_with_pads("VIA1", rail_tap_xy, power_template.net, lower_layer=power_template.access_layer, upper_layer=power_template.rail_layer, lower_role="shared_drop", upper_role="rail")
        else:
            for access_xy in power_template.access_points:
                rail_tap_xy = (access_xy[0], power_template.rail_y)
                drop_points = _extend_vertical_segment(
                    access_xy,
                    rail_tap_xy,
                    min_length_um=0.10,
                    extend_from="end",
                )
                _append_path_if_nonzero(power_template.access_layer, drop_points, signal_w, power_template.net, color_role="drop")
                _append_via_stack(access_xy, power_template.net, power_template.access_via_defs)
                if power_template.access_layer != power_template.rail_layer:
                    add_via_with_pads("VIA1", rail_tap_xy, power_template.net, lower_layer=power_template.access_layer, upper_layer=power_template.rail_layer, lower_role="drop", upper_role="rail")
        rail_pin_center_x = pdk.rules.snap_point_um((left_x + 0.18, power_template.rail_y))[0]
        rail_pin_boxes[power_template.net] = pin_bbox((rail_pin_center_x, power_template.rail_y))
    boundary_pins.extend(
        (
            OaPin("VDD", "VDD", "inputOutput", floorplan.template.pin_layers.get("VDD", "M2"), rail_pin_boxes["VDD"], emit_draw_rect=False),
            OaPin("VSS", "VSS", "inputOutput", floorplan.template.pin_layers.get("VSS", "M2"), rail_pin_boxes["VSS"], emit_draw_rect=False),
        )
    )

    plan = OaWritePlan(
        OaCellView(lib, cell, "layout", "maskLayout"),
        nets=tuple(sorted(set(graph.nets) | set(graph.pins))),
        rects=tuple(rects),
        paths=tuple(paths),
        vias=tuple(vias),
    )
    return plan, tuple(boundary_pins)


def _refine_route_templates_with_detailed_smt(
    graph: TopologyGraph,
    floorplan: NativeStdCellFloorplan,
    access_catalog: NativeStdCellAccessCatalog,
    pdk: PdkConfig,
    route_templates: object,
) -> tuple[object, Mapping[str, object]]:
    try:
        from analogskills.layout.stdcell_detailed_smt import (
            build_native_stdcell_detailed_route_problem,
            project_native_stdcell_detailed_route_solution,
            solve_native_stdcell_detailed_route_problem,
        )
    except Exception as exc:
        return route_templates, {"attempted": False, "applied": False, "reason": f"import_failed:{exc}"}

    try:
        problem = build_native_stdcell_detailed_route_problem(
            graph,
            floorplan,
            access_catalog,
            pdk,
        )
        result = solve_native_stdcell_detailed_route_problem(problem)
    except Exception as exc:
        return route_templates, {"attempted": True, "candidate_available": False, "reason": f"solve_failed:{exc}"}

    metadata = {
        "attempted": True,
        "candidate_available": bool(result.solution is not None),
        "sat": bool(result.stats.sat),
        "backend": result.stats.backend,
        "anchor_variables": int(result.stats.anchor_variables),
        "trunk_variables": int(result.stats.trunk_variables),
        "color_variables": int(result.stats.color_variables),
        "pair_conflict_pairs": int(result.stats.pair_conflict_pairs),
        "trunk_conflict_pairs": int(result.stats.trunk_conflict_pairs),
        "scoped_nets": tuple(problem.scoped_nets),
    }
    if result.solution is None:
        return route_templates, metadata
    projected = project_native_stdcell_detailed_route_solution(problem, result.solution)
    return projected, {
        **metadata,
        "cost": float(result.solution.cost),
        "color_choices": tuple(sorted(result.solution.color_map().items())),
        "segment_color_choices": tuple(sorted(result.solution.segment_color_map().items())),
    }


def build_n7_native_boundary_markers(
    template: NativeStdCellTemplate,
    pdk: PdkConfig,
    *,
    lib: str,
    cell: str,
) -> OaWritePlan:
    from analogskills.eda import OaCellView, OaRect, OaWritePlan

    bbox = pdk.rules.snap_bbox_um(template.cell_bbox_um(), mode="outward")
    rects = (
        OaRect("prBoundary", "boundary", bbox, ""),
        OaRect("chipBoundary", "chipBoundary", bbox, ""),
    )
    return OaWritePlan(OaCellView(lib, cell, "layout", "maskLayout"), rects=rects)


def build_n7_native_nwell_plan(
    floorplan: NativeStdCellFloorplan,
    pdk: PdkConfig,
    *,
    lib: str,
    cell: str,
) -> OaWritePlan:
    from analogskills.eda import OaCellView, OaRect, OaWritePlan

    nwell_layer = pdk.layer_map.wells.get("nwell", "NW")
    left_x, _, right_x, top_y = floorplan.cell_bbox_um()
    pmos_y = float(floorplan.template.row_y_um["pmos"])
    bbox = pdk.rules.snap_bbox_um((left_x, pmos_y - 0.18, right_x, top_y), mode="outward")
    return OaWritePlan(
        OaCellView(lib, cell, "layout", "maskLayout"),
        rects=(OaRect(nwell_layer, "drawing", bbox, ""),),
    )
