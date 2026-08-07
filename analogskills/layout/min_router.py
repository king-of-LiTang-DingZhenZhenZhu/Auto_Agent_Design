"""Conservative strap-style interconnect planning helpers."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace as dataclass_replace
import heapq
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from analogskills.layout.physical import analyze_plan_physical_connectivity, bbox_overlaps, path_segment_bboxes, via_landing_bboxes
from analogskills.pdk import PdkConfig

if TYPE_CHECKING:
    from analogskills.pcell.calibration import PCellCalibrationCache


@dataclass(frozen=True)
class StrapRouterConfig:
    """Configuration for the simple layer-per-net strap router.

    This router is deliberately small and auditable. It is useful for producing
    a first-pass connectivity proposal, but callers should always run the
    physical connectivity precheck on the merged layout before exporting GDS.
    """

    local_net_prefixes: tuple[str, ...] = ("r2_mid_",)
    local_same_row_um: float = 1.0
    local_max_span_um: float = 25.0
    route_layers: tuple[str, ...] = ()
    route_layer_strategy: str = "unique"
    route_layer_by_net: Mapping[str, str] = field(default_factory=dict)
    strap_lane_by_net: Mapping[str, int] = field(default_factory=dict)
    global_net_order: tuple[str, ...] = ()
    global_net_order_strategy: str = "name"
    drop_route_layer: str = ""
    drop_route_layers: tuple[str, ...] = ()
    fanout_on_drop_layer: bool = False
    gate_fanout_on_drop_layer: bool = False
    connect_to_existing_net: bool = False
    existing_net_target_limit: int = 12
    existing_net_fanout_search_steps: int = 12
    existing_net_fanout_y_search_steps: int = 1
    same_net_row_cluster_preroute: bool = False
    same_net_row_cluster_layer: str = "M1"
    same_net_row_cluster_nets: tuple[str, ...] = ()
    same_net_row_cluster_min_terms: int = 2
    same_net_row_cluster_max_span_um: float = 25.0
    same_net_row_cluster_y_tolerance_um: float = 0.02
    min_route_width_um: float = 0.1
    strap_y_start_um: float = 18.0
    strap_y_pitch_um: float = 0.5
    pin_origin_um: tuple[float, float] = (0.0, 20.0)
    pin_pitch_um: float = 0.5
    pin_size_um: float = 0.2
    pin_drop_x_start_um: float = -0.3
    pin_drop_x_pitch_um: float = -0.15
    gate_landing_size_um: float = 0.14
    contact_cut_size_um: float = 0.06
    gate_m1_landing_style: str = ""
    gate_contact_cut_enabled: bool = True
    gate_po_access_enabled: bool = False
    gate_po_access_mode: str = ""
    gate_po_enclosure_um: float = 0.0
    gate_po_landing_width_um: float = 0.0
    gate_po_landing_height_um: float = 0.0
    via_landing_margin_um: float = 0.0
    wide_metal_multicut_vias: bool = False
    wide_metal_multicut_via_defs: tuple[str, ...] = ()
    wide_metal_multicut_axis_by_via: Mapping[str, str] = field(default_factory=dict)
    dedupe_near_duplicate_vias: bool = True
    via_dedupe_tolerance_um: float = 0.02
    same_net_jog_fill_enabled: bool = False
    same_net_jog_fill_layers: tuple[str, ...] = ()
    same_net_jog_fill_via_defs: tuple[str, ...] = ()
    same_net_jog_fill_include_nonvia_pairs: bool = True
    same_net_jog_fill_max_side_um: float = 0.6
    same_net_jog_fill_min_overlap_um: float = 0.0
    same_net_jog_fill_path_stub_um: float = 0.0
    same_net_jog_fill_check_spacing: bool = True
    route_spacing_clearance_um_by_layer: Mapping[str, float] = field(default_factory=dict)
    route_spacing_clearance_shape_kinds: tuple[str, ...] = ()
    route_spacing_check_same_net: bool = False
    global_net_allowlist: tuple[str, ...] = ()
    terminal_allowlist_keys: tuple[str, ...] = ()
    max_global_nets: int = 0
    max_global_terminals: int = 0
    max_terminals_per_global_net: int = 0
    fanout_pitch_um: float = 0.2
    fanout_search_steps: int = 60
    fanout_y_search_steps: int = 0
    strap_landing_search_steps: int = 0
    repair_strap_landing_search_steps: int = 0
    repair_fanout_search_steps: int = 16
    repair_fanout_y_search_steps: int = 2
    repair_fanout_on_drop_layer: bool = False
    repair_gate_fanout_on_drop_layer: bool = False
    maze_escape_enabled: bool = False
    maze_escape_only_strap_blocked: bool = True
    maze_escape_search_steps: int = 8
    maze_escape_y_search_steps: int = 1
    maze_escape_landing_search_steps: int = 0
    maze_escape_pitch_um: float = 0.5
    maze_escape_window_um: float = 2.0
    maze_escape_max_expansions: int = 4096
    blocker_diagnostic_candidate_limit: int = 4
    blocker_diagnostic_sample_limit: int = 8
    blocker_diagnostic_conflicts_per_candidate: int = 4


@dataclass(frozen=True)
class StrapRouterResult:
    plan: Any
    physical_report: dict[str, object]
    local_nets: tuple[str, ...]
    global_nets: tuple[str, ...]
    layer_for_net: dict[str, str]
    route_layers_by_net: dict[str, tuple[str, ...]]
    skipped_terminals: tuple[dict[str, object], ...] = ()
    skipped_terminal_count: int = 0


@dataclass(frozen=True)
class _TerminalAccess:
    x: float
    y: float
    layer: str
    contact_layer: str = ""
    is_top_level_pin: bool = False
    instance: str = ""
    terminal: str = ""
    logical_name: str = ""
    gate_po_x_span_um: tuple[float, float] = ()


@dataclass(frozen=True)
class _OwnedShape:
    layer: str
    net: str
    bbox: tuple[float, float, float, float]
    kind: str = ""


@dataclass(frozen=True)
class _JogFillShape:
    layer: str
    net: str
    bbox: tuple[float, float, float, float]
    source_kind: str
    via_def: str = ""


def build_boundary_lvs_pins(
    top_level_nets: Sequence[str],
    pdk: PdkConfig | None = None,
    *,
    layer: str | None = None,
    origin_um: tuple[float, float] = (0.0, 20.0),
    pitch_um: float = 0.5,
    size_um: float = 0.2,
):
    """Create deterministic cell-boundary pins for Calibre-visible ports."""

    from analogskills.eda.oa import OaPin

    pin_layer = layer or (pdk.layer_map.metals[0] if pdk is not None else "M1")
    pins = []
    for idx, net in enumerate(top_level_nets):
        x0 = origin_um[0]
        y0 = origin_um[1] + idx * pitch_um
        bbox = (x0, y0, x0 + size_um, y0 + size_um)
        if pdk is not None:
            bbox = pdk.rules.snap_bbox_um(bbox, mode="outward")
        pins.append(OaPin(str(net), str(net), "inputOutput", pin_layer, bbox))
    return tuple(pins)


def build_strap_interconnect_plan(
    pcell_plan: Any,
    top_level_nets: Sequence[str],
    pdk: PdkConfig,
    *,
    lib: str = "work",
    cell: str = "interconnect",
    view: str = "layout",
    config: StrapRouterConfig | None = None,
    calibration_cache: PCellCalibrationCache | None = None,
):
    """Build a first-pass OA interconnect plan using straps and raw via cuts."""

    return build_strap_interconnect_result(
        pcell_plan,
        top_level_nets,
        pdk,
        lib=lib,
        cell=cell,
        view=view,
        config=config,
        calibration_cache=calibration_cache,
    ).plan


def build_strap_interconnect_result(
    pcell_plan: Any,
    top_level_nets: Sequence[str],
    pdk: PdkConfig,
    *,
    lib: str = "work",
    cell: str = "interconnect",
    view: str = "layout",
    config: StrapRouterConfig | None = None,
    calibration_cache: PCellCalibrationCache | None = None,
) -> StrapRouterResult:
    """Build straps and return the plan plus its standalone physical report."""

    from analogskills.eda.oa import OaCellView, OaPath, OaRect, OaWritePlan, snap_oa_write_plan_to_grid
    from analogskills.pcell import PCellTerminalAccessor, PCellTerminalRequiresTap

    cfg = config or StrapRouterConfig()
    accessor = PCellTerminalAccessor(pdk, calibration_cache=calibration_cache)
    terminals = _collect_terminals(pcell_plan, top_level_nets, pdk, cfg, accessor, PCellTerminalRequiresTap)
    terminals = _filter_terminals_for_route_scope(terminals, cfg)
    paths = []
    rects = []
    vias = []
    skipped_terminals: list[dict[str, object]] = []
    occupied: list[_OwnedShape] = list(_instance_terminal_owned_shapes(pcell_plan, pdk, accessor, cfg))
    routed_shapes_by_net: dict[str, list[_OwnedShape]] = {}
    min_w = pdk.rules.snap_dimension_um(cfg.min_route_width_um)
    half_w = min_w / 2.0

    local_nets = tuple(net for net in terminals if _is_local_net(net, terminals[net], cfg))
    single_nets = {net for net in terminals if len(terminals[net]) <= 1}

    for net in local_nets:
        a, b = terminals[net]
        y = (a.y + b.y) / 2.0
        x0, x1 = sorted((a.x, b.x))
        x0, y = _snap_pt(pdk, x0, y)
        x1, _ = _snap_pt(pdk, x1, y)
        path = OaPath("M1", "drawing", ((x0, y), (x1, y)), min_w, net)
        if _path_has_nonzero_length(path):
            paths.append(path)
            path_shapes = _path_owned_shapes(path)
            occupied.extend(path_shapes)
            routed_shapes_by_net.setdefault(str(net), []).extend(path_shapes)

    global_nets = _order_global_nets(
        tuple(net for net in terminals if net not in local_nets and net not in single_nets),
        cfg,
        terminals=terminals,
    )
    route_layers = _route_layers(pdk, cfg)
    route_layers_by_net = _assign_route_layers(global_nets, route_layers, cfg)
    layer_for_net = {net: layers[0] for net, layers in route_layers_by_net.items()}
    max_global_nets = max(0, int(getattr(cfg, "max_global_nets", 0) or 0))
    max_global_terminals = max(0, int(getattr(cfg, "max_global_terminals", 0) or 0))
    max_terminals_per_global_net = max(0, int(getattr(cfg, "max_terminals_per_global_net", 0) or 0))
    routed_global_nets = global_nets[:max_global_nets] if max_global_nets else global_nets
    budget_skipped_global_nets = global_nets[len(routed_global_nets) :]
    cluster_paths = _same_net_row_cluster_paths(pdk, terminals, routed_global_nets, cfg, min_w, occupied)
    for path in cluster_paths:
        paths.append(path)
        path_shapes = _path_owned_shapes(path)
        occupied.extend(path_shapes)
        routed_shapes_by_net.setdefault(str(path.net), []).extend(path_shapes)
    pin_drop_x = {
        str(net): cfg.pin_drop_x_start_um + idx * cfg.pin_drop_x_pitch_um
        for idx, net in enumerate(top_level_nets)
    }

    routed_global_terminal_count = 0
    budget_skipped_terminal_count = 0
    global_net_index = {str(net): idx for idx, net in enumerate(global_nets)}
    for idx, net in enumerate(routed_global_nets):
        layer = layer_for_net[net]
        strap_idx = int(global_net_index.get(str(net), idx))
        lane = int(cfg.strap_lane_by_net.get(str(net), strap_idx))
        strap_y = pdk.rules.snap_point_um((0.0, cfg.strap_y_start_um + lane * cfg.strap_y_pitch_um))[1]
        xs = [term.x for term in terminals[net]]
        if cfg.connect_to_existing_net:
            # Calibrated multifinger access can expose additional same-net bus
            # shapes beyond the terminal accessor's single representative pin.
            # Include their extent so a later existing-net stitch cannot land
            # outside the already-created strap.
            for shape in occupied:
                if str(shape.net) == str(net):
                    xs.extend((float(shape.bbox[0]), float(shape.bbox[2])))
        if net in pin_drop_x:
            xs.append(pin_drop_x[net])
        min_x, max_x = min(xs), max(xs)
        min_x, strap_y = _snap_pt(pdk, min_x, strap_y)
        max_x, strap_y = _snap_pt(pdk, max_x, strap_y)
        strap = OaPath(layer, "drawing", ((min_x, strap_y), (max_x, strap_y)), min_w, net)
        if _path_has_nonzero_length(strap):
            paths.append(strap)
            strap_shapes = _path_owned_shapes(strap)
            occupied.extend(strap_shapes)
            routed_shapes_by_net.setdefault(str(net), []).extend(strap_shapes)

        routed_terminals_this_net = 0
        for term in terminals[net]:
            budget_reason = ""
            budget_limit = 0
            if max_terminals_per_global_net and routed_terminals_this_net >= max_terminals_per_global_net:
                budget_reason = "max_terminals_per_global_net"
                budget_limit = max_terminals_per_global_net
            elif max_global_terminals and routed_global_terminal_count >= max_global_terminals:
                budget_reason = "max_global_terminals"
                budget_limit = max_global_terminals
            if budget_reason:
                skipped_terminals.append(
                    _routing_budget_skip_row(
                        pdk,
                        term,
                        net,
                        layer,
                        strap_y,
                        cfg,
                        reason=budget_reason,
                        limit=budget_limit,
                    )
                )
                budget_skipped_terminal_count += 1
                continue
            routed_global_terminal_count += 1
            routed_terminals_this_net += 1
            x, y, term_layer = term.x, term.y, term.layer
            pending_routed_shapes: list[_OwnedShape] = []
            if term.is_top_level_pin and net in pin_drop_x:
                pin_cx = cfg.pin_origin_um[0] + cfg.pin_size_um / 2.0
                pin_cy = term.y + cfg.pin_size_um / 2.0
                drop_x, pin_cy = _snap_pt(pdk, pin_drop_x[net], pin_cy)
                pin_path = OaPath("M1", "drawing", ((pin_cx, pin_cy), (drop_x, pin_cy)), min_w, net)
                if _path_has_nonzero_length(pin_path):
                    paths.append(pin_path)
                    pin_shapes = _path_owned_shapes(pin_path)
                    occupied.extend(pin_shapes)
                    pending_routed_shapes.extend(pin_shapes)
                x, y, term_layer = drop_x, pin_cy, "M1"
            else:
                x, y = _snap_pt(pdk, x, y)
            terminal_paths, terminal_rects, terminal_vias, skipped = _route_terminal_to_strap(
                pdk,
                term,
                net,
                layer,
                strap_y,
                (min_x, max_x),
                cfg,
                min_w,
                half_w,
                occupied,
                forced_xy=(x, y) if term.is_top_level_pin else None,
                forced_layer=term_layer if term.is_top_level_pin else None,
            )
            if skipped:
                terminal_paths, terminal_rects, terminal_vias, strap_repair_skip = _retry_terminal_to_strap_with_landing_repair(
                    pdk,
                    term,
                    net,
                    layer,
                    strap_y,
                    (min_x, max_x),
                    cfg,
                    min_w,
                    half_w,
                    occupied,
                    forced_xy=(x, y) if term.is_top_level_pin else None,
                    forced_layer=term_layer if term.is_top_level_pin else None,
                )
                if strap_repair_skip is None:
                    paths.extend(terminal_paths)
                    rects.extend(terminal_rects)
                    vias.extend(terminal_vias)
                    new_shapes = list(pending_routed_shapes)
                    for path in terminal_paths:
                        new_shapes.extend(_path_owned_shapes(path))
                    new_shapes.extend(_rect_owned_shapes(terminal_rects))
                    new_shapes.extend(_via_owned_shapes(terminal_vias, pdk))
                    occupied.extend(new_shapes)
                    routed_shapes_by_net.setdefault(str(net), []).extend(new_shapes)
                    continue
                skipped = {**skipped, **strap_repair_skip}
                terminal_paths, terminal_rects, terminal_vias, fanout_repair_skip = _retry_terminal_to_strap_with_drop_layer_fanout(
                    pdk,
                    term,
                    net,
                    layer,
                    strap_y,
                    (min_x, max_x),
                    cfg,
                    min_w,
                    half_w,
                    occupied,
                    forced_xy=(x, y) if term.is_top_level_pin else None,
                    forced_layer=term_layer if term.is_top_level_pin else None,
                )
                if fanout_repair_skip is None:
                    paths.extend(terminal_paths)
                    rects.extend(terminal_rects)
                    vias.extend(terminal_vias)
                    new_shapes = list(pending_routed_shapes)
                    for path in terminal_paths:
                        new_shapes.extend(_path_owned_shapes(path))
                    new_shapes.extend(_rect_owned_shapes(terminal_rects))
                    new_shapes.extend(_via_owned_shapes(terminal_vias, pdk))
                    occupied.extend(new_shapes)
                    routed_shapes_by_net.setdefault(str(net), []).extend(new_shapes)
                    continue
                skipped = {**skipped, **fanout_repair_skip}
                if _skip_suggests_maze_escape(skipped, cfg):
                    terminal_paths, terminal_rects, terminal_vias, maze_skip = _route_terminal_to_strap_with_maze_escape(
                        pdk,
                        term,
                        net,
                        layer,
                        strap_y,
                        (min_x, max_x),
                        cfg,
                        min_w,
                        half_w,
                        occupied,
                        forced_xy=(x, y) if term.is_top_level_pin else None,
                        forced_layer=term_layer if term.is_top_level_pin else None,
                    )
                else:
                    maze_reason = "disabled" if not bool(getattr(cfg, "maze_escape_enabled", False)) else "not_strap_escape_dominant"
                    terminal_paths, terminal_rects, terminal_vias, maze_skip = (), (), (), {"maze_escape_reason": maze_reason}
                if maze_skip is None:
                    paths.extend(terminal_paths)
                    rects.extend(terminal_rects)
                    vias.extend(terminal_vias)
                    new_shapes = list(pending_routed_shapes)
                    for path in terminal_paths:
                        new_shapes.extend(_path_owned_shapes(path))
                    new_shapes.extend(_rect_owned_shapes(terminal_rects))
                    new_shapes.extend(_via_owned_shapes(terminal_vias, pdk))
                    occupied.extend(new_shapes)
                    routed_shapes_by_net.setdefault(str(net), []).extend(new_shapes)
                    continue
                skipped = {**skipped, **maze_skip}
                if bool(getattr(cfg, "connect_to_existing_net", False)):
                    terminal_paths, terminal_rects, terminal_vias, existing_skip = _route_terminal_to_existing_net(
                        pdk,
                        term,
                        net,
                        cfg,
                        min_w,
                        half_w,
                        occupied,
                        tuple(routed_shapes_by_net.get(str(net), ()) or ()),
                    )
                    if existing_skip is None:
                        paths.extend(terminal_paths)
                        rects.extend(terminal_rects)
                        vias.extend(terminal_vias)
                        new_shapes = list(pending_routed_shapes)
                        for path in terminal_paths:
                            new_shapes.extend(_path_owned_shapes(path))
                        new_shapes.extend(_rect_owned_shapes(terminal_rects))
                        new_shapes.extend(_via_owned_shapes(terminal_vias, pdk))
                        occupied.extend(new_shapes)
                        routed_shapes_by_net.setdefault(str(net), []).extend(new_shapes)
                        continue
                    skipped = {**skipped, **existing_skip}
                skipped_terminals.append(skipped)
                continue
            paths.extend(terminal_paths)
            rects.extend(terminal_rects)
            vias.extend(terminal_vias)
            new_shapes = list(pending_routed_shapes)
            for path in terminal_paths:
                new_shapes.extend(_path_owned_shapes(path))
            new_shapes.extend(_rect_owned_shapes(terminal_rects))
            new_shapes.extend(_via_owned_shapes(terminal_vias, pdk))
            occupied.extend(new_shapes)
            routed_shapes_by_net.setdefault(str(net), []).extend(new_shapes)

    for net in budget_skipped_global_nets:
        layer = layer_for_net[net]
        strap_idx = int(global_net_index.get(str(net), len(routed_global_nets)))
        strap_y = pdk.rules.snap_point_um((0.0, cfg.strap_y_start_um + strap_idx * cfg.strap_y_pitch_um))[1]
        for term in terminals[net]:
            skipped_terminals.append(
                _routing_budget_skip_row(
                    pdk,
                    term,
                    net,
                    layer,
                    strap_y,
                    cfg,
                    reason="max_global_nets",
                    limit=max_global_nets,
                )
            )
            budget_skipped_terminal_count += 1

    nets = tuple(dict.fromkeys(net for net, pins in terminals.items() if pins))
    plan = OaWritePlan(
        OaCellView(str(getattr(pcell_plan, "metadata", {}).get("lib", lib)), cell, view, "maskLayout"),
        nets=nets,
        paths=tuple(paths),
        rects=tuple(rects),
        vias=tuple(vias),
    )
    plan = snap_oa_write_plan_to_grid(plan, pdk)
    if bool(getattr(cfg, "dedupe_near_duplicate_vias", True)):
        plan = _dedupe_near_duplicate_vias(plan, pdk, cfg)
    if bool(getattr(cfg, "same_net_jog_fill_enabled", False)):
        plan = fill_same_net_jog_rects(plan, pdk, cfg)
    physical_report = dict(analyze_plan_physical_connectivity(plan, pdk=pdk, include_instance_terminal_shorts=True))
    if skipped_terminals:
        skip_issues = tuple(
            f"unrouted terminal {row['net']} at ({float(row['x_um']):.6g},{float(row['y_um']):.6g}) on {row['layer']}: {row['reason']}"
            for row in skipped_terminals
        )
        physical_report["unrouted_terminal_issues"] = skip_issues
        physical_report["unrouted_terminal_count"] = len(skipped_terminals)
        physical_report["issues"] = tuple(physical_report.get("issues", ()) or ()) + skip_issues
        physical_report["passed"] = False
    else:
        physical_report["unrouted_terminal_issues"] = ()
        physical_report["unrouted_terminal_count"] = 0
    routing_budget_limited = bool(budget_skipped_global_nets or budget_skipped_terminal_count)
    physical_report["routing_budget"] = {
        "budget_limited": routing_budget_limited,
        "max_global_nets": max_global_nets,
        "max_global_terminals": max_global_terminals,
        "max_terminals_per_global_net": max_terminals_per_global_net,
        "global_net_count": len(global_nets),
        "routed_global_net_count": len(routed_global_nets),
        "budget_skipped_global_net_count": len(budget_skipped_global_nets),
        "routed_global_terminal_count": routed_global_terminal_count,
        "budget_skipped_terminal_count": budget_skipped_terminal_count,
        "budget_skipped_global_nets": tuple(str(net) for net in budget_skipped_global_nets[:50]),
    }
    return StrapRouterResult(
        plan=plan,
        physical_report=physical_report,
        local_nets=local_nets,
        global_nets=global_nets,
        layer_for_net=layer_for_net,
        route_layers_by_net=route_layers_by_net,
        skipped_terminals=tuple(skipped_terminals),
        skipped_terminal_count=len(skipped_terminals),
    )


def analyze_merged_physical_connectivity(
    base_plan: Any,
    *plans: Any,
    pdk: PdkConfig | None = None,
    terminal_accessor: Any | None = None,
) -> dict[str, object]:
    """Merge OA plans without snapping and run the physical short precheck."""

    if not plans:
        return analyze_plan_physical_connectivity(
            base_plan,
            pdk=pdk,
            include_instance_terminal_shorts=pdk is not None and bool(getattr(base_plan, "instances", ())),
            terminal_accessor=terminal_accessor,
        )
    from analogskills.eda.oa import merge_oa_write_plans

    merged = merge_oa_write_plans(base_plan, *plans, cellview=base_plan.cellview, snap_to_grid=False)
    include_instance_terminal_shorts = bool(getattr(base_plan, "instances", ())) or any(bool(getattr(plan, "instances", ())) for plan in plans)
    return analyze_plan_physical_connectivity(
        merged,
        pdk=pdk,
        include_instance_terminal_shorts=include_instance_terminal_shorts,
        terminal_accessor=terminal_accessor,
    )


def _instance_terminal_owned_shapes(
    pcell_plan: Any,
    pdk: PdkConfig,
    accessor: Any,
    cfg: StrapRouterConfig | None = None,
) -> tuple[_OwnedShape, ...]:
    owned: list[_OwnedShape] = []
    for instance in getattr(pcell_plan, "instances", ()):
        metadata = dict(getattr(instance, "metadata", {}) or {})
        for shape in tuple(metadata.get("routing_owned_shapes", ()) or ()):
            try:
                bbox = tuple(float(value) for value in shape["bbox_um"])
                if len(bbox) != 4:
                    continue
                owned.append(
                    _OwnedShape(
                        str(shape["layer"]),
                        str(shape["net"]),
                        bbox,
                        str(shape.get("kind", "instance_routing_owned")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        for terminal, net in sorted(getattr(instance, "connections", {}).items()):
            if not net:
                continue
            try:
                pin = accessor.select_terminal_pin(instance, terminal, require_lvs_safe=True)
            except ValueError:
                if pdk.name == "tsmcn7":
                    continue
                try:
                    pin = accessor.get_terminal_pin(instance, terminal)
                except Exception:
                    continue
            except Exception:
                continue
            bbox = pin.bbox_um or _synthetic_terminal_keepout_bbox(pdk, pin, cfg)
            if bbox is None:
                continue
            owned.append(_OwnedShape(pin.layer, str(net), bbox))
    return tuple(owned)


def _synthetic_terminal_keepout_bbox(
    pdk: PdkConfig,
    pin: Any,
    cfg: StrapRouterConfig | None = None,
) -> tuple[float, float, float, float] | None:
    """Reserve a minimal terminal-access window when calibration has no bbox.

    CRN28 reference-sized native PCells often rely on template terminal access
    points without calibrated pin rectangles.  If the router does not reserve
    those access windows, an earlier net can legally cross a later terminal and
    make the later terminal unroutable.  The synthetic bbox is intentionally
    small: it models only the landing/access keepout, not the whole native PCell
    metal shape.
    """

    xy = getattr(pin, "xy_um", None)
    if not xy:
        return None
    try:
        x, y = pdk.rules.snap_point_um((float(xy[0]), float(xy[1])))
    except (TypeError, ValueError, IndexError):
        return None
    route_w = float(getattr(cfg, "min_route_width_um", 0.0) or 0.0) if cfg is not None else 0.0
    contact = _contact_cut_size_um(pdk, cfg, str(getattr(pdk.layer_map, "contact", "") or "")) if cfg is not None else 0.0
    gate_landing = float(getattr(cfg, "gate_landing_size_um", 0.0) or 0.0) if cfg is not None else 0.0
    layer = str(getattr(pin, "layer", "") or "")
    if layer == str(getattr(pdk.layer_map, "gate", "")):
        po_w, po_h = _gate_po_landing_size_um(
            pdk,
            cfg or StrapRouterConfig(),
            contact,
            str(getattr(pin, "contact_layer", "") or getattr(pdk.layer_map, "contact", "")),
        )
        size = max(gate_landing, po_w, po_h, route_w, contact, 0.06)
    else:
        size = max(route_w, contact, 0.06)
    half = 0.5 * pdk.rules.snap_dimension_um(size)
    return pdk.rules.snap_bbox_um((x - half, y - half, x + half, y + half), mode="outward")


def _collect_terminals(
    pcell_plan: Any,
    top_level_nets: Sequence[str],
    pdk: PdkConfig,
    cfg: StrapRouterConfig,
    accessor: Any,
    requires_tap_error: type[Exception] | tuple[type[Exception], ...] = (),
) -> dict[str, list[_TerminalAccess]]:
    if isinstance(requires_tap_error, tuple):
        skip_errors: tuple[type[Exception], ...] = (KeyError, *requires_tap_error)
    else:
        skip_errors = (KeyError, requires_tap_error)
    terminals: dict[str, list[_TerminalAccess]] = {}
    for inst in getattr(pcell_plan, "instances", ()):
        for term, net in sorted(getattr(inst, "connections", {}).items()):
            if not net:
                continue
            try:
                pin = accessor.select_terminal_pin(inst, term, require_lvs_safe=True)
            except skip_errors:
                continue
            except ValueError:
                if pdk.name == "tsmcn7":
                    continue
                try:
                    pin = accessor.get_terminal_pin(inst, term)
                except skip_errors:
                    continue
            x, y = pin.xy_um
            logical = str(getattr(inst, "logical_name", "") or getattr(inst, "cell_name", "") or "")
            terminals.setdefault(str(net), []).append(
                _TerminalAccess(
                    x,
                    y,
                    pin.layer,
                    pin.contact_layer,
                    False,
                    str(getattr(inst, "name", "") or ""),
                    str(term),
                    logical,
                    _mos_gate_po_x_span_um(pdk, inst, pin, logical) if str(term).upper() == "G" else (),
                )
            )

    for idx, net in enumerate(top_level_nets):
        y = cfg.pin_origin_um[1] + idx * cfg.pin_pitch_um
        x, y = pdk.rules.snap_point_um((cfg.pin_origin_um[0], y))
        terminals.setdefault(str(net), []).append(_TerminalAccess(x, y, pdk.layer_map.metals[0], pdk.layer_map.contact, True, "", str(net)))
    return _dedupe_terminals_by_access(terminals, pdk)


def _dedupe_terminals_by_access(
    terminals: Mapping[str, Sequence[_TerminalAccess]],
    pdk: PdkConfig,
) -> dict[str, list[_TerminalAccess]]:
    deduped: dict[str, list[_TerminalAccess]] = {}
    for net, rows in terminals.items():
        seen: set[tuple[float, float, str, str, bool]] = set()
        unique_rows: list[_TerminalAccess] = []
        for row in rows:
            x, y = _snap_pt(pdk, row.x, row.y)
            key = (
                x,
                y,
                str(row.layer),
                str(row.contact_layer),
                bool(row.is_top_level_pin),
                str(getattr(row, "instance", "") or ""),
                str(getattr(row, "terminal", "") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            unique_rows.append(row)
        deduped[str(net)] = unique_rows
    return deduped


def _filter_terminals_for_route_scope(
    terminals: Mapping[str, Sequence[_TerminalAccess]],
    cfg: StrapRouterConfig,
) -> dict[str, list[_TerminalAccess]]:
    allowed_nets = {
        str(net).lower()
        for net in tuple(getattr(cfg, "global_net_allowlist", ()) or ())
        if str(net)
    }
    allowed_terminal_keys = {
        str(key)
        for key in tuple(getattr(cfg, "terminal_allowlist_keys", ()) or ())
        if str(key)
    }
    if not allowed_nets and not allowed_terminal_keys:
        return {str(net): list(rows) for net, rows in dict(terminals or {}).items()}
    scoped: dict[str, list[_TerminalAccess]] = {}
    for net, rows in dict(terminals or {}).items():
        text_net = str(net)
        if allowed_nets and text_net.lower() not in allowed_nets:
            continue
        kept: list[_TerminalAccess] = []
        for row in tuple(rows or ()):
            if allowed_terminal_keys and not bool(row.is_top_level_pin):
                key = _terminal_allowlist_key(text_net, row)
                if key not in allowed_terminal_keys:
                    continue
            kept.append(row)
        if kept:
            scoped[text_net] = kept
    return scoped


def _terminal_allowlist_key(net: str, row: _TerminalAccess) -> str:
    return "|".join(
        (
            str(net),
            str(getattr(row, "instance", "") or ""),
            str(getattr(row, "terminal", "") or ""),
        )
    )


def _routing_budget_skip_row(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    cfg: StrapRouterConfig,
    *,
    reason: str,
    limit: int,
) -> dict[str, object]:
    term_x, term_y = _snap_pt(pdk, term.x, term.y)
    drop_route_layer = _drop_route_layer(pdk, cfg, route_layer)
    return {
        "net": str(net),
        "instance": str(getattr(term, "instance", "") or ""),
        "terminal": str(getattr(term, "terminal", "") or ""),
        "x_um": term_x,
        "y_um": term_y,
        "layer": str(getattr(term, "layer", "") or ""),
        "route_layer": str(route_layer),
        "drop_route_layer": str(drop_route_layer),
        "strap_y_um": float(strap_y),
        "reason": "routing_budget_exhausted",
        "routing_budget_reason": str(reason),
        "routing_budget_limit": int(limit),
        "budget_limited": True,
        "blocker_stage_counts": {"routing_budget": 1},
    }


def _mos_gate_po_x_span_um(pdk: PdkConfig, inst: Any, pin: Any, logical_name: str) -> tuple[float, float]:
    logical = str(logical_name or "").lower()
    if logical not in {"nmos", "nfet", "nch", "nch_mac", "pmos", "pfet", "pch", "pch_mac"}:
        return ()
    params = getattr(inst, "params", {}) or {}
    length_um = _dimension_param_um(params, ("l", "L", "length", "length_um"))
    if length_um <= 0.0:
        return ()
    offset_um = _calibre_mos_access_nm_value(pdk, "gate_contact_x_offset_from_gate_left_nm", 0.0) * 1e-3
    if offset_um <= 0.0:
        offset_um = _template_gate_contact_x_offset_um(pdk, logical)
    if offset_um <= 0.0:
        return ()
    try:
        x = float(getattr(pin, "xy_um", ())[0])
    except (TypeError, ValueError, IndexError):
        return ()
    orient = str(getattr(inst, "orient", "R0") or "R0")
    if orient in {"R0", "MX"}:
        x0 = x - offset_um
        x1 = x0 + length_um
    elif orient in {"MY", "R180"}:
        x1 = x + offset_um
        x0 = x1 - length_um
    else:
        return ()
    x0, _ = _snap_pt(pdk, x0, 0.0)
    x1, _ = _snap_pt(pdk, x1, 0.0)
    lo, hi = sorted((x0, x1))
    if hi - lo <= 0.0:
        return ()
    return (lo, hi)


def _template_gate_contact_x_offset_um(pdk: PdkConfig, logical_name: str) -> float:
    try:
        template = pdk.pcell_template_for(logical_name)
    except Exception:
        return 0.0
    raw = (getattr(template, "terminal_access", {}) or {}).get("G", {})
    if not isinstance(raw, Mapping):
        return 0.0
    xy = raw.get("xy", ())
    try:
        return abs(float(tuple(xy)[0]))
    except (TypeError, ValueError, IndexError):
        return 0.0


def _dimension_param_um(params: Mapping[str, object], keys: Sequence[str]) -> float:
    for key in keys:
        if key not in params:
            continue
        try:
            return _dimension_token_um(params[key])
        except (TypeError, ValueError):
            continue
    return 0.0


def _dimension_token_um(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a dimension")
    if isinstance(value, str):
        text = value.strip().lower()
        if text.endswith("um"):
            return float(text[:-2])
        if text.endswith("u"):
            return float(text[:-1])
        if text.endswith("nm"):
            return float(text[:-2]) * 1e-3
        if text.endswith("n"):
            return float(text[:-1]) * 1e-3
        if text.endswith("m"):
            return float(text[:-1]) * 1e6
        number = float(text)
    else:
        number = float(value)
    # PCell parameters usually arrive in meters; already-micron numeric values
    # used by tests/configs are expected to be larger than one nanometer.
    return number * 1e6 if abs(number) < 1e-3 else number


def _is_local_net(net: str, terms: Sequence[_TerminalAccess], cfg: StrapRouterConfig) -> bool:
    if not any(net.startswith(prefix) for prefix in cfg.local_net_prefixes):
        return False
    if len(terms) != 2:
        return False
    a, b = terms
    return abs(a.y - b.y) < cfg.local_same_row_um and abs(a.x - b.x) < cfg.local_max_span_um


def _same_net_row_cluster_paths(
    pdk: PdkConfig,
    terminals: Mapping[str, Sequence[_TerminalAccess]],
    global_nets: Sequence[str],
    cfg: StrapRouterConfig,
    min_w: float,
    occupied: Sequence[_OwnedShape],
) -> tuple[Any, ...]:
    if not bool(getattr(cfg, "same_net_row_cluster_preroute", False)):
        return ()
    from analogskills.eda.oa import OaPath

    layer = str(getattr(cfg, "same_net_row_cluster_layer", "M1") or "M1")
    min_terms = max(2, int(getattr(cfg, "same_net_row_cluster_min_terms", 2) or 2))
    max_span = max(0.0, float(getattr(cfg, "same_net_row_cluster_max_span_um", 25.0) or 25.0))
    y_tol = max(float(getattr(cfg, "same_net_row_cluster_y_tolerance_um", 0.02) or 0.02), 1e-6)
    paths: list[OaPath] = []
    local_occupied = list(occupied)
    allowed_nets = tuple(str(net).lower() for net in tuple(getattr(cfg, "same_net_row_cluster_nets", ()) or ()) if str(net))
    for net in tuple(global_nets):
        if allowed_nets and str(net).lower() not in allowed_nets:
            continue
        rows = [
            _TerminalAccess(*_snap_pt(pdk, row.x, row.y), row.layer, row.contact_layer, row.is_top_level_pin)
            for row in tuple(terminals.get(net, ()) or ())
            if str(row.layer) == layer and not bool(row.is_top_level_pin)
        ]
        if len(rows) < min_terms:
            continue
        for cluster in _cluster_terminals_by_y(rows, y_tol):
            if len(cluster) < min_terms:
                continue
            xs = tuple(float(row.x) for row in cluster)
            x0, x1 = min(xs), max(xs)
            if x1 - x0 <= 1e-12 or x1 - x0 > max_span:
                continue
            y = sum(float(row.y) for row in cluster) / float(len(cluster))
            x0, y = _snap_pt(pdk, x0, y)
            x1, _ = _snap_pt(pdk, x1, y)
            path = OaPath(layer, "drawing", ((x0, y), (x1, y)), min_w, str(net))
            if not _path_has_nonzero_length(path):
                continue
            candidate_shapes = _path_owned_shapes(path)
            if _shapes_conflict(
                candidate_shapes,
                local_occupied,
                clearance_by_layer=_route_spacing_clearance_by_layer(cfg),
                clearance_shape_kinds=_route_spacing_clearance_shape_kinds(cfg),
                include_same_net_spacing=bool(getattr(cfg, "route_spacing_check_same_net", False)),
            ):
                continue
            paths.append(path)
            local_occupied.extend(candidate_shapes)
    return tuple(paths)


def _cluster_terminals_by_y(rows: Sequence[_TerminalAccess], y_tolerance_um: float) -> tuple[tuple[_TerminalAccess, ...], ...]:
    clusters: list[list[_TerminalAccess]] = []
    for row in sorted(tuple(rows), key=lambda item: (float(item.y), float(item.x))):
        if not clusters or abs(float(row.y) - float(clusters[-1][0].y)) > y_tolerance_um:
            clusters.append([row])
        else:
            clusters[-1].append(row)
    return tuple(tuple(cluster) for cluster in clusters)


def _route_layers(pdk: PdkConfig, cfg: StrapRouterConfig) -> tuple[str, ...]:
    if cfg.route_layers:
        return cfg.route_layers
    return tuple(pdk.layer_map.metals[1:])


def _assign_route_layers(
    global_nets: Sequence[str],
    route_layers: Sequence[str],
    cfg: StrapRouterConfig,
) -> dict[str, tuple[str, ...]]:
    layers = tuple(str(layer) for layer in route_layers if str(layer))
    if not layers:
        raise RuntimeError("No route layers are available for strap routing")
    strategy = str(getattr(cfg, "route_layer_strategy", "unique") or "unique").lower()
    nets = tuple(str(net) for net in global_nets)
    explicit = {
        net: str(cfg.route_layer_by_net[net])
        for net in nets
        if str(cfg.route_layer_by_net.get(net, ""))
    }
    if strategy == "unique":
        if len(nets) > len(layers):
            raise RuntimeError(f"Need {len(nets)} route layers but only {len(layers)} are available")
        return {net: (explicit.get(net, layers[idx]),) for idx, net in enumerate(nets)}
    if strategy == "cyclic":
        return {net: (explicit.get(net, layers[idx % len(layers)]),) for idx, net in enumerate(nets)}
    raise RuntimeError(f"Unknown strap route_layer_strategy {strategy!r}")


def _order_global_nets(
    global_nets: Sequence[str],
    cfg: StrapRouterConfig,
    *,
    terminals: Mapping[str, Sequence[_TerminalAccess]] | None = None,
) -> tuple[str, ...]:
    nets = tuple(str(net) for net in global_nets if str(net))
    preferred = tuple(str(net) for net in getattr(cfg, "global_net_order", ()) if str(net))
    if not preferred:
        strategy = str(getattr(cfg, "global_net_order_strategy", "name") or "name").strip().lower()
        if strategy in {"centroid_x", "spatial_x", "x"} and terminals is not None:
            return tuple(sorted(nets, key=lambda net: (_terminal_centroid_x(terminals.get(net, ())), net)))
        if strategy in {"fanout_desc", "terminal_count_desc", "high_fanout_first"} and terminals is not None:
            return tuple(sorted(nets, key=lambda net: (-len(tuple(terminals.get(net, ()) or ())), net)))
        if strategy in {"supply_fanout_desc", "power_fanout_desc", "supplies_first"} and terminals is not None:
            return tuple(sorted(nets, key=lambda net: (_supply_net_rank(net), -len(tuple(terminals.get(net, ()) or ())), net)))
        return tuple(sorted(nets))
    rank = {net: idx for idx, net in enumerate(preferred)}
    return tuple(sorted(nets, key=lambda net: (rank.get(net, len(rank)), net)))


def _terminal_centroid_x(terminals: Sequence[_TerminalAccess]) -> float:
    rows = tuple(terminals or ())
    if not rows:
        return 0.0
    return sum(float(row.x) for row in rows) / float(len(rows))


def _supply_net_rank(net: str) -> int:
    text = str(net or "").upper()
    if any(token in text for token in ("VDD", "VSS", "GND", "AVDD", "AGND", "DVDD", "DGND")):
        return 0
    return 1


def _access_landing_rects(
    pdk: PdkConfig,
    x: float,
    y: float,
    bottom_layer: str,
    top_layer: str,
    net: str,
    cfg: StrapRouterConfig,
    half_w: float,
    contact_layer: str,
    *,
    route_axis: str = "",
    route_width_um: float | None = None,
):
    from analogskills.eda.oa import OaRect

    rects = []
    x, y = _snap_pt(pdk, x, y)
    local_contact = contact_layer or pdk.layer_map.contact
    metals = list(pdk.layer_map.metals)
    metal0 = metals[0] if metals else ""
    if bottom_layer == pdk.layer_map.gate:
        rects.extend(
            _gate_contact_landing_rects(
                pdk,
                x,
                y,
                local_contact,
                net,
                cfg,
                route_axis=route_axis,
                route_width_um=route_width_um,
            )
        )
    elif bottom_layer == "MD" and metal0:
        # N7 source/drain access often lands on MD and requires a VD cut to reach M0.
        rects.append(_contact_cut_rect(pdk, x, y, local_contact, net, cfg))
        rects.append(OaRect(metal0, "drawing", (x - half_w, y - half_w, x + half_w, y + half_w), net))
    return tuple(rects)


def _gate_contact_landing_rects(
    pdk: PdkConfig,
    x: float,
    y: float,
    contact_layer: str,
    net: str,
    cfg: StrapRouterConfig,
    logical_name: str = "",
    gate_po_x_span_um: Sequence[float] = (),
    *,
    route_axis: str = "",
    route_width_um: float | None = None,
):
    """Return the calibrated PO/CO/M1 landing stack for a raw gate access."""

    from analogskills.eda.oa import OaRect

    x, y = _snap_pt(pdk, x, y)
    rects = []
    contact = contact_layer or pdk.layer_map.contact
    contact_size = _contact_cut_size_um(pdk, cfg, contact)
    po_width, po_height = _gate_po_landing_size_um(pdk, cfg, contact_size, contact)
    if po_width > 0.0 and po_height > 0.0:
        po_x0, po_y0, po_x1, po_y1 = _gate_po_landing_bbox_um(
            pdk,
            cfg,
            x,
            y,
            po_width,
            po_height,
            gate_po_x_span_um,
        )
        if _calibre_mos_access_bool_value(pdk, "gate_access_emit_implant_cover", False):
            rects.extend(_gate_contact_cover_rects(pdk, po_x0, po_x1, y, po_y1 - po_y0, logical_name))
        rects.append(
            OaRect(
                pdk.layer_map.gate,
                "drawing",
                (po_x0, po_y0, po_x1, po_y1),
                net,
                metadata={
                    "kind": "router_gate_po_contact_enclosure",
                    "access_mode": _gate_po_access_mode(cfg),
                },
            )
        )
    if pdk.layer_map.metals:
        m1_bbox = _gate_m1_landing_bbox_um(
            pdk,
            cfg,
            x,
            y,
            route_axis=route_axis,
            route_width_um=route_width_um,
        )
        rects.append(
            OaRect(
                pdk.layer_map.metals[0],
                "drawing",
                m1_bbox,
                net,
                metadata={
                    "kind": "router_gate_m1_contact_landing",
                    "landing_style": _gate_m1_landing_style(cfg),
                },
            )
        )
    if bool(getattr(cfg, "gate_contact_cut_enabled", True)):
        rects.append(_contact_cut_rect(pdk, x, y, contact, net, cfg))
    return tuple(rects)


def _gate_contact_cover_rects(
    pdk: PdkConfig,
    po_x0: float,
    po_x1: float,
    y: float,
    po_height_um: float,
    logical_name: str = "",
):
    from analogskills.eda.oa import OaRect

    logical = str(logical_name or "").lower()
    implant = ""
    include_pmetal = False
    if logical in {"nmos", "nfet", "nch", "nch_mac"}:
        implant = str(pdk.layer_map.implants.get("nplus", "NP") or "NP")
    elif logical in {"pmos", "pfet", "pch", "pch_mac"}:
        implant = str(pdk.layer_map.implants.get("pplus", "PP") or "PP")
        include_pmetal = True
    else:
        return ()
    x_margin = _calibre_mos_access_nm_value(pdk, "gate_implant_cover_x_margin_nm", 70.0) * 1e-3
    y_margin = _calibre_mos_access_nm_value(pdk, "gate_implant_cover_y_margin_nm", 70.0) * 1e-3
    po_half_h = 0.5 * float(po_height_um)
    rects = [
        OaRect(
            implant,
            "drawing",
            pdk.rules.snap_bbox_um(
                (po_x0 - x_margin, y - po_half_h - y_margin, po_x1 + x_margin, y + po_half_h + y_margin),
                mode="outward",
            ),
            "",
            metadata={"kind": "router_gate_implant_contact_cover", "logical_name": logical},
        )
    ]
    if include_pmetal:
        pmetal = str(pdk.layer_map.implants.get("pmetal", "") or "")
        if pmetal:
            pm_x_margin = _calibre_mos_access_nm_value(pdk, "gate_pmetal_cover_x_margin_nm", 120.0) * 1e-3
            pm_y_margin = _calibre_mos_access_nm_value(pdk, "gate_pmetal_cover_y_margin_nm", 120.0) * 1e-3
            rects.append(
                OaRect(
                    pmetal,
                    "drawing1",
                    pdk.rules.snap_bbox_um(
                        (
                            po_x0 - pm_x_margin,
                            y - po_half_h - pm_y_margin,
                            po_x1 + pm_x_margin,
                            y + po_half_h + pm_y_margin,
                        ),
                        mode="outward",
                    ),
                    "",
                    metadata={"kind": "router_gate_pmetal_contact_cover", "logical_name": logical},
                )
            )
    return tuple(rects)


def _gate_m1_landing_size_um(pdk: PdkConfig, cfg: StrapRouterConfig) -> float:
    configured = max(float(getattr(cfg, "gate_landing_size_um", 0.0) or 0.0), 0.0)
    metadata_value = _calibre_mos_access_nm_value(pdk, "gate_m1_landing_width_nm", 0.0) * 1e-3
    size = max(configured, metadata_value)
    try:
        size = max(size, float(pdk.rules.min_width_um(pdk.layer_map.metals[0])))
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        pass
    return pdk.rules.snap_dimension_um(max(size, 0.001))


def _gate_m1_landing_bbox_um(
    pdk: PdkConfig,
    cfg: StrapRouterConfig,
    x: float,
    y: float,
    *,
    route_axis: str = "",
    route_width_um: float | None = None,
) -> tuple[float, float, float, float]:
    """Return a Calibre-friendly M1 gate landing.

    A square contact landing is safe in isolation, but a narrower M1 escape
    path starting at its center creates sub-min-width ledges at the transition.
    The route-aligned style keeps the long landing dimension along the first
    M1 escape segment and matches the short dimension to the route width, so
    the landing/path union has no artificial notch at the gate access.
    """

    size = _gate_m1_landing_size_um(pdk, cfg)
    axis = str(route_axis or "").strip().lower()
    style = _gate_m1_landing_style(cfg)
    width = size
    height = size
    if style in {"route_aligned", "aligned_to_route", "directional"} and axis in {"x", "horizontal", "y", "vertical"}:
        route_w = _gate_m1_landing_route_width_um(pdk, cfg, route_width_um)
        if axis in {"x", "horizontal"}:
            width = size
            height = route_w
        else:
            width = route_w
            height = size
    half_w = 0.5 * pdk.rules.snap_dimension_um(max(width, 0.001))
    half_h = 0.5 * pdk.rules.snap_dimension_um(max(height, 0.001))
    return pdk.rules.snap_bbox_um((x - half_w, y - half_h, x + half_w, y + half_h), mode="outward")


def _gate_m1_landing_style(cfg: StrapRouterConfig) -> str:
    return str(getattr(cfg, "gate_m1_landing_style", "") or "").strip().lower()


def _gate_m1_landing_route_width_um(
    pdk: PdkConfig,
    cfg: StrapRouterConfig,
    route_width_um: float | None,
) -> float:
    configured = max(float(route_width_um or 0.0), float(getattr(cfg, "min_route_width_um", 0.0) or 0.0), 0.0)
    try:
        configured = max(configured, float(pdk.rules.min_width_um(pdk.layer_map.metals[0])))
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        pass
    contact_size = _contact_cut_size_um(pdk, cfg, getattr(pdk.layer_map, "contact", ""))
    contact_enclosure = _gate_m1_contact_enclosure_um(pdk)
    configured = max(configured, contact_size + 2.0 * contact_enclosure)
    return pdk.rules.snap_dimension_um(max(configured, 0.001))


def _gate_m1_contact_enclosure_um(pdk: PdkConfig) -> float:
    contact = str(getattr(pdk.layer_map, "contact", "") or "")
    metal0 = ""
    try:
        metal0 = str(tuple(pdk.layer_map.metals)[0])
    except (AttributeError, IndexError, TypeError):
        metal0 = ""
    for key in (f"{contact}_{metal0}", f"{metal0}_{contact}", "CO_M1", "M1_CO"):
        if not key.strip("_"):
            continue
        try:
            return pdk.rules.snap_dimension_um(max(float(pdk.rules.enclosure_um(key)), 0.0))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return 0.0


def _gate_po_landing_bbox_um(
    pdk: PdkConfig,
    cfg: StrapRouterConfig,
    x: float,
    y: float,
    po_width: float,
    po_height: float,
    gate_po_x_span_um: Sequence[float] = (),
) -> tuple[float, float, float, float]:
    mode = _gate_po_access_mode(cfg)
    if mode in {"contact_enclosure", "co_enclosure", "minimal_contact_enclosure"}:
        po_half_w = 0.5 * po_width
        po_half_h = 0.5 * po_height
        return pdk.rules.snap_bbox_um((x - po_half_w, y - po_half_h, x + po_half_w, y + po_half_h), mode="outward")

    span = tuple(float(value) for value in tuple(gate_po_x_span_um or ())[:2])
    if len(span) == 2 and abs(span[1] - span[0]) > 1e-12:
        po_x0, po_x1 = sorted(span)
    else:
        po_half_w = 0.5 * po_width
        po_x0, po_x1 = x - po_half_w, x + po_half_w
    po_half_h = 0.5 * po_height
    return pdk.rules.snap_bbox_um((po_x0, y - po_half_h, po_x1, y + po_half_h), mode="outward")


def _gate_po_landing_size_um(
    pdk: PdkConfig,
    cfg: StrapRouterConfig,
    contact_size_um: float,
    contact_layer: str,
) -> tuple[float, float]:
    if str(contact_layer) != str(getattr(pdk.layer_map, "contact", "") or ""):
        return 0.0, 0.0
    if not bool(getattr(cfg, "gate_po_access_enabled", False)):
        return 0.0, 0.0
    metadata_has_gate_po = _calibre_mos_access_has_key(pdk, "gate_po_extension_height_nm") or _calibre_mos_access_has_key(
        pdk,
        "gate_contact_po_enclosure_nm",
    )
    configured_w = max(float(getattr(cfg, "gate_po_landing_width_um", 0.0) or 0.0), 0.0)
    configured_h = max(float(getattr(cfg, "gate_po_landing_height_um", 0.0) or 0.0), 0.0)
    if not metadata_has_gate_po and configured_w <= 0.0 and configured_h <= 0.0:
        return 0.0, 0.0
    enclosure = _gate_po_enclosure_um(pdk, cfg)
    min_required = max(float(contact_size_um), 0.0) + 2.0 * enclosure
    if _gate_po_access_mode(cfg) in {"contact_enclosure", "co_enclosure", "minimal_contact_enclosure"}:
        try:
            min_required = max(min_required, float(pdk.rules.min_width_um(pdk.layer_map.gate)))
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        size = pdk.rules.snap_dimension_um(max(min_required, 0.001))
        return size, size
    metadata_width = _calibre_mos_access_nm_value(pdk, "gate_po_landing_width_nm", 0.0) * 1e-3
    metadata_height = _calibre_mos_access_nm_value(pdk, "gate_po_extension_height_nm", 0.0) * 1e-3
    gate_overlap = _calibre_mos_access_nm_value(pdk, "gate_po_overlap_nm", 0.0) * 1e-3
    if gate_overlap > 0.0:
        metadata_height = max(metadata_height, 2.0 * gate_overlap)
    width = max(configured_w, metadata_width, min_required)
    height = max(configured_h, metadata_height, min_required)
    try:
        width = max(width, float(pdk.rules.min_width_um(pdk.layer_map.gate)))
        height = max(height, float(pdk.rules.min_width_um(pdk.layer_map.gate)))
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return pdk.rules.snap_dimension_um(width), pdk.rules.snap_dimension_um(height)


def _gate_po_access_mode(cfg: StrapRouterConfig) -> str:
    return str(getattr(cfg, "gate_po_access_mode", "") or "span").strip().lower()


def _gate_po_enclosure_um(pdk: PdkConfig, cfg: StrapRouterConfig) -> float:
    configured = max(float(getattr(cfg, "gate_po_enclosure_um", 0.0) or 0.0), 0.0)
    if configured > 0.0:
        return pdk.rules.snap_dimension_um(configured)
    metadata_value = _calibre_mos_access_nm_value(pdk, "gate_contact_po_enclosure_nm", 0.0) * 1e-3
    if metadata_value > 0.0:
        return pdk.rules.snap_dimension_um(metadata_value)
    for key in (f"{pdk.layer_map.contact}_{pdk.layer_map.gate}", "CO_PO"):
        try:
            return pdk.rules.snap_dimension_um(float(pdk.rules.enclosure_um(key)))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return 0.0


def _calibre_mos_access_has_key(pdk: PdkConfig, key: str) -> bool:
    raw = _calibre_mos_access_metadata(pdk)
    return key in raw or key.replace("_nm", "") in raw


def _calibre_mos_access_nm_value(pdk: PdkConfig, key: str, default_nm: float) -> float:
    raw = _calibre_mos_access_metadata(pdk)
    value = raw.get(key, raw.get(key.replace("_nm", ""), default_nm))
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default_nm)
    return max(number, 0.0)


def _calibre_mos_access_bool_value(pdk: PdkConfig, key: str, default: bool) -> bool:
    raw = _calibre_mos_access_metadata(pdk)
    value = raw.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _calibre_mos_access_metadata(pdk: PdkConfig) -> Mapping[str, object]:
    metadata = getattr(pdk, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return {}
    calibre = metadata.get("calibre", {}) or {}
    if not isinstance(calibre, Mapping):
        return {}
    raw = calibre.get("mos_access", {}) or {}
    return raw if isinstance(raw, Mapping) else {}


def _contact_cut_rect(pdk: PdkConfig, x: float, y: float, contact_layer: str, net: str, cfg: StrapRouterConfig):
    from analogskills.eda.oa import OaRect

    layer = contact_layer or pdk.layer_map.contact
    size = _contact_cut_size_um(pdk, cfg, layer)
    half = size / 2.0
    metadata: dict[str, object] = {"kind": "router_contact_cut"}
    if layer == pdk.layer_map.contact:
        metadata.update(
            {
                "snap_mode": "exact_size_on_grid",
                "exact_width_um": size,
                "exact_height_um": size,
            }
        )
    return OaRect(layer, "drawing", (x - half, y - half, x + half, y + half), net, metadata=metadata)


def _contact_cut_size_um(pdk: PdkConfig, cfg: StrapRouterConfig | None, contact_layer: str) -> float:
    configured = float(getattr(cfg, "contact_cut_size_um", 0.0) or 0.0) if cfg is not None else 0.0
    if contact_layer == str(getattr(pdk.layer_map, "contact", "") or ""):
        try:
            return float(pdk.rules.min_width_um(contact_layer))
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    return max(configured, 0.0)


def _via_stack(
    pdk: PdkConfig,
    x: float,
    y: float,
    bottom_layer: str,
    top_layer: str,
    net: str,
    *,
    landing_margin_um: float | None = None,
    route_width_um: float | None = None,
    wide_metal_multicut_vias: bool = False,
    wide_metal_multicut_via_defs: Sequence[str] = (),
    wide_metal_multicut_axis_by_via: Mapping[str, str] | None = None,
):
    from analogskills.eda.oa import OaVia

    x, y = _snap_pt(pdk, x, y)
    if bottom_layer == top_layer:
        return ()
    metadata = {}
    if landing_margin_um is not None and float(landing_margin_um) > 0.0:
        metadata["landing_margin_um"] = float(landing_margin_um)
    metals = tuple(pdk.layer_map.metals)
    if bottom_layer not in metals or top_layer not in metals:
        return ()
    b_idx = metals.index(bottom_layer)
    t_idx = metals.index(top_layer)
    vias: list[OaVia] = []
    step = 1 if t_idx > b_idx else -1
    for idx in range(b_idx, t_idx, step):
        lower = metals[min(idx, idx + step)]
        upper = metals[max(idx, idx + step)]
        via_idx = idx if step > 0 else idx - 1
        via_rule = pdk.via_rule_for_layers(lower, upper)
        if via_rule is not None:
            multicut_enabled = _wide_metal_multicut_enabled_for_via(
                via_rule.via_def,
                wide_metal_multicut_vias=wide_metal_multicut_vias,
                wide_metal_multicut_via_defs=wide_metal_multicut_via_defs,
            )
            rows, cols = _route_via_array_shape(
                pdk,
                via_rule.via_def,
                via_rule.default_rows,
                via_rule.default_cols,
                via_rule.max_rows,
                via_rule.max_cols,
                net,
                (x, y),
                metadata,
                route_width_um=route_width_um,
                enabled=multicut_enabled,
                axis=_wide_metal_multicut_axis_for_via(via_rule.via_def, wide_metal_multicut_axis_by_via),
            )
            vias.append(
                OaVia(
                    via_rule.via_def,
                    (x, y),
                    net,
                    rows=rows,
                    cols=cols,
                    metadata=dict(metadata),
                )
            )
        elif 0 <= via_idx < len(pdk.layer_map.vias):
            via_def = pdk.layer_map.vias[via_idx]
            multicut_enabled = _wide_metal_multicut_enabled_for_via(
                via_def,
                wide_metal_multicut_vias=wide_metal_multicut_vias,
                wide_metal_multicut_via_defs=wide_metal_multicut_via_defs,
            )
            rows, cols = _route_via_array_shape(
                pdk,
                via_def,
                1,
                1,
                4,
                4,
                net,
                (x, y),
                metadata,
                route_width_um=route_width_um,
                enabled=multicut_enabled,
                axis=_wide_metal_multicut_axis_for_via(via_def, wide_metal_multicut_axis_by_via),
            )
            vias.append(OaVia(via_def, (x, y), net, rows=rows, cols=cols, metadata=dict(metadata)))
    return tuple(vias)


def _route_via_landing_margin_um(cfg: StrapRouterConfig, min_w: float) -> float:
    configured = max(0.0, float(getattr(cfg, "via_landing_margin_um", 0.0) or 0.0))
    return max(configured, 0.5 * max(float(min_w), 0.0))


def _wide_metal_multicut_enabled_for_via(
    via_def: str,
    *,
    wide_metal_multicut_vias: bool,
    wide_metal_multicut_via_defs: Sequence[str],
) -> bool:
    if not bool(wide_metal_multicut_vias):
        return False
    allowed = {str(item) for item in tuple(wide_metal_multicut_via_defs or ()) if str(item)}
    return not allowed or str(via_def) in allowed


def _wide_metal_multicut_axis_for_via(via_def: str, axis_by_via: Mapping[str, str] | None) -> str:
    if not isinstance(axis_by_via, Mapping):
        return ""
    axis = str(axis_by_via.get(str(via_def), "") or "").strip().lower()
    return axis if axis in {"x", "y", "square", "auto"} else ""


def _dedupe_near_duplicate_vias(plan: Any, pdk: PdkConfig, cfg: StrapRouterConfig):
    """Drop same-net/same-via stacks that occupy the same physical access point.

    The reference router can route several terminals of a high-fanout net to the
    same strap through nearly identical drop points.  When the drop points differ
    only by a few signoff-grid units, native cut-array streamout produces
    overlapping via cuts that Calibre reports as VIA*.W/R violations.  This
    cleanup is intentionally narrow: it only clusters identical via definitions
    on the same net within a small configured tolerance and keeps the largest
    cut array in the cluster.
    """

    vias = tuple(getattr(plan, "vias", ()) or ())
    if not vias:
        return plan
    tol = _via_dedupe_tolerance_um(pdk, cfg)
    if tol <= 0.0:
        return plan
    clusters: list[dict[str, object]] = []
    by_key: dict[tuple[str, str], list[int]] = {}
    for original_index, via in enumerate(vias):
        via_def = str(getattr(via, "via_def", "") or "")
        net = str(getattr(via, "net", "") or "")
        if not via_def:
            clusters.append({"first_index": original_index, "vias": [via], "xy": (0.0, 0.0), "key": ("", "")})
            continue
        try:
            x, y = _snap_pt(pdk, *tuple(getattr(via, "xy", (0.0, 0.0)))[:2])
        except (TypeError, ValueError):
            clusters.append({"first_index": original_index, "vias": [via], "xy": (0.0, 0.0), "key": (via_def, net)})
            continue
        key = (via_def, net)
        matched_cluster_idx: int | None = None
        for cluster_idx in by_key.get(key, ()):
            cx, cy = tuple(clusters[cluster_idx]["xy"])  # type: ignore[arg-type]
            if abs(float(cx) - x) <= tol + 1e-12 and abs(float(cy) - y) <= tol + 1e-12:
                matched_cluster_idx = cluster_idx
                break
        if matched_cluster_idx is None:
            clusters.append({"first_index": original_index, "vias": [via], "xy": (x, y), "key": key})
            by_key.setdefault(key, []).append(len(clusters) - 1)
        else:
            cluster_vias = clusters[matched_cluster_idx]["vias"]
            if isinstance(cluster_vias, list):
                cluster_vias.append(via)
    deduped = tuple(_preferred_via_from_cluster(tuple(cluster["vias"])) for cluster in sorted(clusters, key=lambda item: int(item["first_index"])))
    if len(deduped) == len(vias):
        return plan
    return dataclass_replace(plan, vias=deduped)


def fill_same_net_jog_rects(plan: Any, pdk: PdkConfig, cfg: StrapRouterConfig | None = None):
    """Add small same-net metal rectangles that remove Calibre-visible jog notches.

    This is a local geometry cleanup, not a router.  It targets cases where two
    same-net shapes already overlap on the same layer but their bboxes are
    slightly offset, so their streamout union creates a short jog/notch marker
    such as CRN28 ``G.4:M1i``.  The fill is restricted by layer/via config,
    maximum side length, positive overlap, and same-layer other-net spacing.
    """

    cfg = cfg or StrapRouterConfig()
    if not bool(getattr(cfg, "same_net_jog_fill_enabled", False)):
        return plan
    target_layers = {
        str(layer)
        for layer in tuple(getattr(cfg, "same_net_jog_fill_layers", ()) or ())
        if str(layer)
    }
    if not target_layers:
        target_layers = {"M1"}
    via_defs = {
        str(via_def)
        for via_def in tuple(getattr(cfg, "same_net_jog_fill_via_defs", ()) or ())
        if str(via_def)
    }
    include_nonvia_pairs = bool(getattr(cfg, "same_net_jog_fill_include_nonvia_pairs", True))
    max_side = max(float(getattr(cfg, "same_net_jog_fill_max_side_um", 0.0) or 0.0), 0.0)
    if max_side <= 0.0:
        return plan
    min_overlap = max(float(getattr(cfg, "same_net_jog_fill_min_overlap_um", 0.0) or 0.0), 0.0)
    path_stub = max(float(getattr(cfg, "same_net_jog_fill_path_stub_um", 0.0) or 0.0), 0.0)
    check_spacing = bool(getattr(cfg, "same_net_jog_fill_check_spacing", True))

    shapes = _same_net_jog_fill_shapes(plan, pdk, target_layers=target_layers, via_defs=via_defs)
    if not shapes:
        return plan

    by_layer_net: dict[tuple[str, str], list[_JogFillShape]] = {}
    by_layer: dict[str, list[_JogFillShape]] = {}
    for shape in shapes:
        by_layer_net.setdefault((shape.layer, shape.net), []).append(shape)
        by_layer.setdefault(shape.layer, []).append(shape)

    existing_same_net = tuple(shapes)
    fill_rects = []
    seen: set[tuple[str, str, tuple[float, float, float, float]]] = set()
    from analogskills.eda.oa import OaRect

    for (layer, net), group in by_layer_net.items():
        if len(group) < 2:
            continue
        for left_idx in range(len(group)):
            left = group[left_idx]
            for right in group[left_idx + 1 :]:
                if not _same_net_jog_fill_pair_allowed(left, right, via_defs=via_defs, include_nonvia_pairs=include_nonvia_pairs):
                    continue
                if not _bboxes_overlap_area(left.bbox, right.bbox, min_overlap_um=min_overlap):
                    continue
                for candidate in _same_net_jog_fill_candidate_bboxes(left, right, pdk, max_side_um=max_side, path_stub_um=path_stub):
                    if not _bbox_has_positive_area(candidate):
                        continue
                    if _bbox_width(candidate) > max_side + 1e-12 or _bbox_height(candidate) > max_side + 1e-12:
                        continue
                    if _bbox_contains_any_same_net(existing_same_net, candidate, layer, net):
                        continue
                    key = (layer, net, tuple(round(float(value), 9) for value in candidate))
                    if key in seen:
                        continue
                    if _same_net_jog_fill_conflicts_other_net(
                        candidate,
                        layer,
                        net,
                        tuple(by_layer.get(layer, ()) or ()),
                        pdk,
                        check_spacing=check_spacing,
                    ):
                        continue
                    seen.add(key)
                    fill_rects.append(
                        OaRect(
                            layer,
                            "drawing",
                            candidate,
                            net,
                            metadata={
                                "kind": "same_net_jog_fill",
                                "source": "analogskills.layout.min_router.fill_same_net_jog_rects",
                                "left_source_kind": left.source_kind,
                                "right_source_kind": right.source_kind,
                                "left_bbox": tuple(left.bbox),
                                "right_bbox": tuple(right.bbox),
                            },
                        )
                    )

    if not fill_rects:
        return plan
    return dataclass_replace(plan, rects=tuple(getattr(plan, "rects", ()) or ()) + tuple(fill_rects))


def _via_dedupe_tolerance_um(pdk: PdkConfig, cfg: StrapRouterConfig) -> float:
    configured = max(float(getattr(cfg, "via_dedupe_tolerance_um", 0.0) or 0.0), 0.0)
    grid = 0.0
    try:
        grid = max(grid, float(pdk.rules.grid_step_um))
    except (AttributeError, TypeError, ValueError):
        pass
    metadata = getattr(pdk, "metadata", {}) or {}
    if isinstance(metadata, Mapping):
        calibre = metadata.get("calibre", {}) or {}
        if isinstance(calibre, Mapping):
            try:
                grid = max(grid, float(calibre.get("grid_nm", 0.0) or 0.0) * 1e-3)
            except (TypeError, ValueError):
                pass
    return max(configured, grid)


def _preferred_via_from_cluster(vias: Sequence[Any]) -> Any:
    rows = tuple(vias or ())
    if not rows:
        raise ValueError("via cluster cannot be empty")
    return max(enumerate(rows), key=lambda item: (_via_cut_count(item[1]), _via_rows(item[1]) + _via_cols(item[1]), -item[0]))[1]


def _via_cut_count(via: Any) -> int:
    return _via_rows(via) * _via_cols(via)


def _same_net_jog_fill_shapes(
    plan: Any,
    pdk: PdkConfig,
    *,
    target_layers: set[str],
    via_defs: set[str],
) -> tuple[_JogFillShape, ...]:
    shapes: list[_JogFillShape] = []
    for rect in tuple(getattr(plan, "rects", ()) or ()):
        layer = str(getattr(rect, "layer", "") or "")
        net = str(getattr(rect, "net", "") or "")
        if not layer or not net or layer not in target_layers:
            continue
        try:
            bbox = _bbox_tuple(getattr(rect, "bbox", ()))
        except ValueError:
            continue
        shapes.append(_JogFillShape(layer, net, bbox, "rect"))
    for path in tuple(getattr(plan, "paths", ()) or ()):
        layer = str(getattr(path, "layer", "") or "")
        net = str(getattr(path, "net", "") or "")
        if not layer or not net or layer not in target_layers:
            continue
        width = float(getattr(path, "width", 0.0) or 0.0)
        for bbox in path_segment_bboxes(tuple(getattr(path, "points", ()) or ()), width):
            shapes.append(_JogFillShape(layer, net, _bbox_tuple(bbox), "path"))
    for via in tuple(getattr(plan, "vias", ()) or ()):
        via_def = str(getattr(via, "via_def", "") or "")
        net = str(getattr(via, "net", "") or "")
        if not via_def or not net:
            continue
        if via_defs and via_def not in via_defs:
            continue
        for layer, bbox in via_landing_bboxes(via, pdk):
            layer = str(layer)
            if layer in target_layers:
                shapes.append(_JogFillShape(layer, net, _bbox_tuple(bbox), "via_landing", via_def=via_def))
    return tuple(shapes)


def _same_net_jog_fill_pair_allowed(
    left: _JogFillShape,
    right: _JogFillShape,
    *,
    via_defs: set[str],
    include_nonvia_pairs: bool,
) -> bool:
    left_is_via = left.source_kind == "via_landing"
    right_is_via = right.source_kind == "via_landing"
    if left_is_via or right_is_via:
        if via_defs:
            via_names = {shape.via_def for shape in (left, right) if shape.source_kind == "via_landing"}
            if not via_names or any(via_name not in via_defs for via_name in via_names):
                return False
        return True
    return bool(include_nonvia_pairs)


def _same_net_jog_fill_candidate_bboxes(
    left: _JogFillShape,
    right: _JogFillShape,
    pdk: PdkConfig,
    *,
    max_side_um: float,
    path_stub_um: float,
) -> tuple[tuple[float, float, float, float], ...]:
    full_union = _snap_bbox_outward(pdk, _bbox_union(left.bbox, right.bbox))
    if _bbox_width(full_union) <= max_side_um + 1e-12 and _bbox_height(full_union) <= max_side_um + 1e-12:
        return (full_union,)
    candidates: list[tuple[float, float, float, float]] = []
    stub = max(float(path_stub_um), 0.0)
    if stub <= 0.0:
        return (full_union,)
    if left.source_kind == "path" and right.source_kind != "path":
        candidates.extend(_same_net_jog_fill_path_stub_candidates(left.bbox, right.bbox, pdk, stub_um=stub))
    elif right.source_kind == "path" and left.source_kind != "path":
        candidates.extend(_same_net_jog_fill_path_stub_candidates(right.bbox, left.bbox, pdk, stub_um=stub))
    deduped: list[tuple[float, float, float, float]] = []
    seen: set[tuple[float, float, float, float]] = set()
    for candidate in candidates:
        key = tuple(round(float(value), 9) for value in candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return tuple(deduped)


def _same_net_jog_fill_path_stub_candidates(
    path_bbox: tuple[float, float, float, float],
    landing_bbox: tuple[float, float, float, float],
    pdk: PdkConfig,
    *,
    stub_um: float,
) -> tuple[tuple[float, float, float, float], ...]:
    candidates: list[tuple[float, float, float, float]] = []
    path_w = _bbox_width(path_bbox)
    path_h = _bbox_height(path_bbox)
    if path_w >= path_h:
        y0 = min(path_bbox[1], landing_bbox[1])
        y1 = max(path_bbox[3], landing_bbox[3])
        if path_bbox[0] < landing_bbox[0] < path_bbox[2]:
            candidates.append(
                _snap_bbox_outward(
                    pdk,
                    (
                        max(path_bbox[0], landing_bbox[0] - stub_um),
                        y0,
                        landing_bbox[2],
                        y1,
                    ),
                )
            )
        if path_bbox[0] < landing_bbox[2] < path_bbox[2]:
            candidates.append(
                _snap_bbox_outward(
                    pdk,
                    (
                        landing_bbox[0],
                        y0,
                        min(path_bbox[2], landing_bbox[2] + stub_um),
                        y1,
                    ),
                )
            )
    else:
        x0 = min(path_bbox[0], landing_bbox[0])
        x1 = max(path_bbox[2], landing_bbox[2])
        if path_bbox[1] < landing_bbox[1] < path_bbox[3]:
            candidates.append(
                _snap_bbox_outward(
                    pdk,
                    (
                        x0,
                        max(path_bbox[1], landing_bbox[1] - stub_um),
                        x1,
                        landing_bbox[3],
                    ),
                )
            )
        if path_bbox[1] < landing_bbox[3] < path_bbox[3]:
            candidates.append(
                _snap_bbox_outward(
                    pdk,
                    (
                        x0,
                        landing_bbox[1],
                        x1,
                        min(path_bbox[3], landing_bbox[3] + stub_um),
                    ),
                )
            )
    return tuple(candidate for candidate in candidates if _bbox_has_positive_area(candidate))


def _same_net_jog_fill_conflicts_other_net(
    candidate: tuple[float, float, float, float],
    layer: str,
    net: str,
    layer_shapes: Sequence[_JogFillShape],
    pdk: PdkConfig,
    *,
    check_spacing: bool,
) -> bool:
    spacing = _layer_min_spacing_um(pdk, layer) if check_spacing else 0.0
    spacing_bbox = _inflate_bbox(candidate, spacing) if spacing > 0.0 else candidate
    for shape in layer_shapes:
        if shape.net == net:
            continue
        if bbox_overlaps(candidate, shape.bbox, include_touching=True):
            return True
        if spacing > 0.0 and bbox_overlaps(spacing_bbox, shape.bbox, include_touching=False):
            return True
    return False


def _bbox_contains_any_same_net(
    shapes: Sequence[_JogFillShape],
    bbox: tuple[float, float, float, float],
    layer: str,
    net: str,
) -> bool:
    return any(shape.layer == layer and shape.net == net and _bbox_contains(shape.bbox, bbox) for shape in shapes)


def _bboxes_overlap_area(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    min_overlap_um: float = 0.0,
) -> bool:
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    tol = max(float(min_overlap_um), 0.0) + 1e-12
    return (x1 - x0) > tol and (y1 - y0) > tol


def _bbox_union(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (min(left[0], right[0]), min(left[1], right[1]), max(left[2], right[2]), max(left[3], right[3]))


def _bbox_tuple(value: object) -> tuple[float, float, float, float]:
    try:
        x0, y0, x1, y1 = tuple(value)[:4]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("bbox must contain four coordinates")
    xlo, xhi = sorted((float(x0), float(x1)))
    ylo, yhi = sorted((float(y0), float(y1)))
    return (xlo, ylo, xhi, yhi)


def _bbox_contains(container: tuple[float, float, float, float], inner: tuple[float, float, float, float], *, tol_um: float = 1e-12) -> bool:
    tol = max(float(tol_um), 0.0)
    return (
        container[0] <= inner[0] + tol
        and container[1] <= inner[1] + tol
        and container[2] + tol >= inner[2]
        and container[3] + tol >= inner[3]
    )


def _bbox_width(bbox: tuple[float, float, float, float]) -> float:
    return float(bbox[2]) - float(bbox[0])


def _bbox_height(bbox: tuple[float, float, float, float]) -> float:
    return float(bbox[3]) - float(bbox[1])


def _bbox_has_positive_area(bbox: tuple[float, float, float, float]) -> bool:
    return _bbox_width(bbox) > 1e-12 and _bbox_height(bbox) > 1e-12


def _inflate_bbox(bbox: tuple[float, float, float, float], delta: float) -> tuple[float, float, float, float]:
    d = max(float(delta), 0.0)
    return (bbox[0] - d, bbox[1] - d, bbox[2] + d, bbox[3] + d)


def _layer_min_spacing_um(pdk: PdkConfig, layer: str) -> float:
    try:
        return max(float(pdk.rules.min_spacing_um(layer)), 0.0)
    except (AttributeError, KeyError, TypeError, ValueError):
        return 0.0


def _snap_bbox_outward(pdk: PdkConfig, bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    try:
        return _bbox_tuple(pdk.rules.snap_bbox_um(bbox, mode="outward"))
    except (AttributeError, TypeError, ValueError):
        return _bbox_tuple(bbox)


def _via_rows(via: Any) -> int:
    try:
        return max(int(getattr(via, "rows", 1) or 1), 1)
    except (TypeError, ValueError):
        return 1


def _via_cols(via: Any) -> int:
    try:
        return max(int(getattr(via, "cols", 1) or 1), 1)
    except (TypeError, ValueError):
        return 1


def _route_via_array_shape(
    pdk: PdkConfig,
    via_def: str,
    default_rows: int,
    default_cols: int,
    max_rows: int,
    max_cols: int,
    net: str,
    xy: tuple[float, float],
    metadata: Mapping[str, object],
    *,
    route_width_um: float | None = None,
    enabled: bool = False,
    axis: str = "",
) -> tuple[int, int]:
    from analogskills.eda.oa import OaVia

    rows = max(int(default_rows or 1), 1)
    cols = max(int(default_cols or 1), 1)
    if not enabled:
        return rows, cols
    max_rows = max(int(max_rows or rows), rows)
    max_cols = max(int(max_cols or cols), cols)
    probe = OaVia(via_def, xy, net, rows=rows, cols=cols, metadata=dict(metadata))
    landings = via_landing_bboxes(probe, pdk)
    max_landing_dim = 0.0
    for _layer, bbox in landings:
        max_landing_dim = max(max_landing_dim, float(bbox[2]) - float(bbox[0]), float(bbox[3]) - float(bbox[1]))
    effective_width = max_landing_dim + max(float(route_width_um or 0.0), 0.0)
    axis = str(axis or "").strip().lower()
    if effective_width > 0.44 and max_rows >= 2 and max_cols >= 2:
        if axis == "x":
            return rows, max(cols, 2)
        if axis == "y":
            return max(rows, 2), cols
        if axis == "square":
            return max(rows, 2), max(cols, 2)
        return max(rows, 2), max(cols, 2)
    if effective_width > 0.18:
        if axis == "x" and max_cols >= 2:
            return rows, max(cols, 2)
        if axis in {"y", "square"} and max_rows >= 2:
            return max(rows, 2), cols
        if max_rows >= 2:
            return max(rows, 2), cols
    return rows, cols


def _route_terminal_to_strap(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    strap_x_range: tuple[float, float],
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    occupied: Sequence[_OwnedShape],
    *,
    forced_xy: tuple[float, float] | None = None,
    forced_layer: str | None = None,
):
    term_x, term_y = _snap_pt(pdk, term.x, term.y)
    base_layer = forced_layer or term.layer
    drop_route_layer = _drop_route_layer(pdk, cfg, route_layer)
    if forced_xy is not None:
        drop_x, drop_y = forced_xy
        drop_layer = base_layer
        local_fanout_points = _fanout_path_options(term_x, term_y, drop_x, drop_y)[0]
        landing_x = drop_x if int(getattr(cfg, "strap_landing_search_steps", 0) or 0) <= 0 else _strap_landing_xs(pdk, cfg, drop_x, strap_x_range)[0]
        strap_points = _fanout_path_options(drop_x, drop_y, landing_x, strap_y)[0]
        selected = (drop_x, drop_y, drop_layer, drop_route_layer, local_fanout_points, strap_points, landing_x)
    else:
        selected = _select_drop_point(
            pdk,
            term,
            net,
            route_layer,
            strap_y,
            strap_x_range,
            cfg,
            min_w,
            half_w,
            occupied,
        )
        if selected is None:
            blocker_summary = _diagnose_drop_point_blockers(
                pdk,
                term,
                net,
                route_layer,
                strap_y,
                strap_x_range,
                cfg,
                min_w,
                half_w,
                occupied,
            )
            return (
                (),
                (),
                (),
                {
                    "net": str(net),
                    "instance": str(getattr(term, "instance", "") or ""),
                    "terminal": str(getattr(term, "terminal", "") or ""),
                    "x_um": term_x,
                    "y_um": term_y,
                    "layer": str(base_layer),
                    "route_layer": str(route_layer),
                    "drop_route_layer": str(drop_route_layer),
                    "strap_y_um": float(strap_y),
                    "reason": "no_conflict_free_drop_point",
                    **blocker_summary,
                },
            )
    return _emit_terminal_to_strap_route(
        pdk,
        term,
        net,
        route_layer,
        strap_y,
        cfg,
        min_w,
        half_w,
        selected,
        forced_layer=forced_layer,
        forced_xy=forced_xy,
    )


def _emit_terminal_to_strap_route(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    selected: tuple[float, float, str, str, tuple[tuple[float, float], ...], tuple[tuple[float, float], ...], float],
    *,
    forced_layer: str | None = None,
    forced_xy: tuple[float, float] | None = None,
):
    from analogskills.eda.oa import OaPath, OaRect, OaVia

    term_x, term_y = _snap_pt(pdk, term.x, term.y)
    base_layer = forced_layer or term.layer
    drop_x, drop_y, drop_layer, drop_route_layer, local_fanout_points, strap_points, landing_x = selected
    paths: list[OaPath] = []
    rects: list[OaRect] = []
    vias: list[OaVia] = []
    via_bottom = drop_layer
    local_path_layer = base_layer
    via_landing_margin = _route_via_landing_margin_um(cfg, min_w)
    if forced_xy is not None:
        via_bottom = base_layer
    elif base_layer == pdk.layer_map.gate:
        metal0 = pdk.layer_map.metals[0]
        gate_route_axis = _first_nonzero_segment_axis(local_fanout_points)
        if term.contact_layer in {"M0_PO", "M0_PO_VD"}:
            vias.append(OaVia(term.contact_layer, (term_x, term_y), net))
        else:
            rects.extend(
                _gate_contact_landing_rects(
                    pdk,
                    term_x,
                    term_y,
                    term.contact_layer or pdk.layer_map.contact,
                    net,
                    cfg,
                    str(getattr(term, "logical_name", "") or ""),
                    tuple(getattr(term, "gate_po_x_span_um", ()) or ()),
                    route_axis=gate_route_axis,
                    route_width_um=min_w,
                )
            )
        via_bottom = metal0
        local_path_layer = metal0
    elif base_layer in {"OD", "PDK", "NW"} and term.contact_layer in {"M0_SUB", "M0_NW"}:
        metal0 = pdk.layer_map.metals[0]
        vias.append(OaVia(term.contact_layer, (term_x, term_y), net))
        via_bottom = metal0
        local_path_layer = metal0

    effective_bottom = pdk.layer_map.metals[0] if via_bottom in {pdk.layer_map.gate, "MD"} else via_bottom
    fanout_on_drop = _terminal_fanout_on_drop_layer(pdk, cfg, term, effective_bottom, drop_route_layer)
    if fanout_on_drop:
        rects.extend(_access_landing_rects(pdk, term_x, term_y, via_bottom, drop_route_layer, net, cfg, half_w, term.contact_layer))
        vias.extend(
            _via_stack(
                pdk,
                term_x,
                term_y,
                effective_bottom,
                drop_route_layer,
                net,
                landing_margin_um=via_landing_margin,
                route_width_um=min_w,
                wide_metal_multicut_vias=bool(getattr(cfg, "wide_metal_multicut_vias", False)),
                wide_metal_multicut_via_defs=tuple(getattr(cfg, "wide_metal_multicut_via_defs", ()) or ()),
                wide_metal_multicut_axis_by_via=dict(getattr(cfg, "wide_metal_multicut_axis_by_via", {}) or {}),
            )
        )
        _append_nonzero_path(paths, OaPath(drop_route_layer, "drawing", local_fanout_points, min_w, net))
    else:
        _append_nonzero_path(paths, OaPath(local_path_layer, "drawing", local_fanout_points, min_w, net))
        rects.extend(_access_landing_rects(pdk, drop_x, drop_y, via_bottom, drop_route_layer, net, cfg, half_w, term.contact_layer))
        vias.extend(
            _via_stack(
                pdk,
                drop_x,
                drop_y,
                effective_bottom,
                drop_route_layer,
                net,
                landing_margin_um=via_landing_margin,
                route_width_um=min_w,
                wide_metal_multicut_vias=bool(getattr(cfg, "wide_metal_multicut_vias", False)),
                wide_metal_multicut_via_defs=tuple(getattr(cfg, "wide_metal_multicut_via_defs", ()) or ()),
                wide_metal_multicut_axis_by_via=dict(getattr(cfg, "wide_metal_multicut_axis_by_via", {}) or {}),
            )
        )
    _append_nonzero_path(paths, OaPath(drop_route_layer, "drawing", strap_points, min_w, net))
    vias.extend(
        _via_stack(
            pdk,
            landing_x,
            strap_y,
            drop_route_layer,
            route_layer,
            net,
            landing_margin_um=via_landing_margin,
            route_width_um=min_w,
            wide_metal_multicut_vias=bool(getattr(cfg, "wide_metal_multicut_vias", False)),
            wide_metal_multicut_via_defs=tuple(getattr(cfg, "wide_metal_multicut_via_defs", ()) or ()),
            wide_metal_multicut_axis_by_via=dict(getattr(cfg, "wide_metal_multicut_axis_by_via", {}) or {}),
        )
    )
    return tuple(paths), tuple(rects), tuple(vias), None


def _retry_terminal_to_strap_with_landing_repair(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    strap_x_range: tuple[float, float],
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    occupied: Sequence[_OwnedShape],
    *,
    forced_xy: tuple[float, float] | None = None,
    forced_layer: str | None = None,
):
    repair_steps = max(0, int(getattr(cfg, "repair_strap_landing_search_steps", 0) or 0))
    if repair_steps <= 0:
        return (), (), (), {"strap_landing_repair_reason": "disabled"}
    repair_fanout_steps = max(0, int(getattr(cfg, "repair_fanout_search_steps", 16) or 16))
    repair_fanout_y_steps = max(0, int(getattr(cfg, "repair_fanout_y_search_steps", 2) or 2))
    repair_cfg = dataclass_replace(
        cfg,
        fanout_search_steps=repair_fanout_steps,
        fanout_y_search_steps=repair_fanout_y_steps,
        strap_landing_search_steps=repair_steps,
        repair_strap_landing_search_steps=0,
    )
    paths, rects, vias, skipped = _route_terminal_to_strap(
        pdk,
        term,
        net,
        route_layer,
        strap_y,
        strap_x_range,
        repair_cfg,
        min_w,
        half_w,
        occupied,
        forced_xy=forced_xy,
        forced_layer=forced_layer,
    )
    if skipped is None:
        return paths, rects, vias, None
    return (), (), (), {"strap_landing_repair_reason": "no_conflict_free_landing_jog"}


def _retry_terminal_to_strap_with_drop_layer_fanout(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    strap_x_range: tuple[float, float],
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    occupied: Sequence[_OwnedShape],
    *,
    forced_xy: tuple[float, float] | None = None,
    forced_layer: str | None = None,
):
    if not bool(getattr(cfg, "repair_fanout_on_drop_layer", False)):
        return (), (), (), {"drop_layer_fanout_repair_reason": "disabled"}
    repair_cfg = dataclass_replace(
        cfg,
        fanout_on_drop_layer=True,
        gate_fanout_on_drop_layer=bool(getattr(cfg, "repair_gate_fanout_on_drop_layer", False)),
        fanout_search_steps=max(0, int(getattr(cfg, "repair_fanout_search_steps", 16) or 16)),
        fanout_y_search_steps=max(0, int(getattr(cfg, "repair_fanout_y_search_steps", 2) or 2)),
        repair_strap_landing_search_steps=0,
        repair_fanout_on_drop_layer=False,
        maze_escape_enabled=False,
    )
    paths, rects, vias, skipped = _route_terminal_to_strap(
        pdk,
        term,
        net,
        route_layer,
        strap_y,
        strap_x_range,
        repair_cfg,
        min_w,
        half_w,
        occupied,
        forced_xy=forced_xy,
        forced_layer=forced_layer,
    )
    if skipped is None:
        return paths, rects, vias, None
    return (), (), (), {"drop_layer_fanout_repair_reason": "no_conflict_free_drop_layer_fanout"}


def _route_terminal_to_strap_with_maze_escape(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    strap_x_range: tuple[float, float],
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    occupied: Sequence[_OwnedShape],
    *,
    forced_xy: tuple[float, float] | None = None,
    forced_layer: str | None = None,
):
    if not bool(getattr(cfg, "maze_escape_enabled", False)):
        return (), (), (), {"maze_escape_reason": "disabled"}
    selected = _select_maze_drop_point(
        pdk,
        term,
        net,
        route_layer,
        strap_y,
        strap_x_range,
        cfg,
        min_w,
        half_w,
        occupied,
        forced_xy=forced_xy,
        forced_layer=forced_layer,
    )
    if selected is None:
        return (), (), (), {"maze_escape_reason": "no_conflict_free_maze_path"}
    return _emit_terminal_to_strap_route(
        pdk,
        term,
        net,
        route_layer,
        strap_y,
        cfg,
        min_w,
        half_w,
        selected,
        forced_layer=forced_layer,
        forced_xy=forced_xy,
    )


def _skip_suggests_maze_escape(skipped: Mapping[str, object], cfg: StrapRouterConfig) -> bool:
    if not bool(getattr(cfg, "maze_escape_enabled", False)):
        return False
    if not bool(getattr(cfg, "maze_escape_only_strap_blocked", True)):
        return True
    raw = skipped.get("blocker_stage_counts", {}) or {}
    if not isinstance(raw, Mapping):
        return False
    strap_count = int(raw.get("strap_escape", 0) or 0)
    if strap_count <= 0:
        return False
    competing = max(
        int(raw.get("terminal_access", 0) or 0),
        int(raw.get("terminal_seed", 0) or 0),
        int(raw.get("local_fanout", 0) or 0),
    )
    return strap_count >= competing


def _select_maze_drop_point(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    strap_x_range: tuple[float, float],
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    occupied: Sequence[_OwnedShape],
    *,
    forced_xy: tuple[float, float] | None = None,
    forced_layer: str | None = None,
) -> tuple[float, float, str, str, tuple[tuple[float, float], ...], tuple[tuple[float, float], ...], float] | None:
    term_x, term_y = _snap_pt(pdk, term.x, term.y)
    base_layer = forced_layer or term.layer
    bottom_layer = base_layer if forced_xy is not None else pdk.layer_map.metals[0] if term.layer == pdk.layer_map.gate else term.layer
    x_steps = max(0, int(getattr(cfg, "maze_escape_search_steps", 8) or 0))
    y_steps = max(0, int(getattr(cfg, "maze_escape_y_search_steps", 1) or 0))
    landing_steps = max(0, int(getattr(cfg, "maze_escape_landing_search_steps", 0) or 0))
    occupied_by_layer = _occupied_shapes_by_layer(occupied)
    for drop_route_layer in _drop_route_layer_candidates(pdk, cfg, route_layer):
        candidate_drop_points: tuple[tuple[float, float], ...]
        if forced_xy is not None:
            candidate_drop_points = (_snap_pt(pdk, forced_xy[0], forced_xy[1]),)
        else:
            candidate_drop_points = tuple(
                _snap_pt(
                    pdk,
                    term_x + x_step * cfg.fanout_pitch_um,
                    term_y + y_step * cfg.fanout_pitch_um,
                )
                for x_step, y_step in _fanout_step_pairs(x_steps, y_steps)
            )
        for drop_x, drop_y in candidate_drop_points:
            landing_cfg = cfg if landing_steps == int(getattr(cfg, "strap_landing_search_steps", 0) or 0) else dataclass_replace(cfg, strap_landing_search_steps=landing_steps)
            landing_candidates = (
                (drop_x,)
                if landing_steps <= 0
                else _strap_landing_xs(pdk, landing_cfg, drop_x, strap_x_range)
            )
            for landing_x in landing_candidates:
                landing_x, snapped_strap_y = _snap_pt(pdk, landing_x, strap_y)
                for local_points in _fanout_path_options(term_x, term_y, drop_x, drop_y):
                    direct_strap_points = _fanout_path_options(drop_x, drop_y, landing_x, snapped_strap_y)[0]
                    precheck_shapes = [
                        shape
                        for stage, group in _fanout_candidate_shape_groups(
                            pdk,
                            term,
                            net,
                            route_layer,
                            strap_y,
                            cfg,
                            min_w,
                            half_w,
                            drop_x,
                            drop_y,
                            bottom_layer,
                            drop_route_layer,
                            landing_x,
                            local_points,
                            direct_strap_points,
                        )
                        if stage != "strap_escape"
                        for shape in group
                    ]
                    if _shapes_conflict_with_layer_index(
                        precheck_shapes,
                        occupied_by_layer,
                        clearance_by_layer=_route_spacing_clearance_by_layer(cfg),
                        clearance_shape_kinds=_route_spacing_clearance_shape_kinds(cfg),
                        include_same_net_spacing=bool(getattr(cfg, "route_spacing_check_same_net", False)),
                    ):
                        continue
                    strap_points = _route_drop_layer_maze_path(
                        pdk,
                        net,
                        drop_route_layer,
                        (drop_x, drop_y),
                        (landing_x, snapped_strap_y),
                        strap_x_range,
                        cfg,
                        min_w,
                        occupied_by_layer,
                    )
                    if strap_points is None:
                        continue
                    shapes = [
                        shape
                        for _stage, group in _fanout_candidate_shape_groups(
                            pdk,
                            term,
                            net,
                            route_layer,
                            strap_y,
                            cfg,
                            min_w,
                            half_w,
                            drop_x,
                            drop_y,
                            bottom_layer,
                            drop_route_layer,
                            landing_x,
                            local_points,
                            strap_points,
                        )
                        for shape in group
                    ]
                    if not _shapes_conflict_with_layer_index(
                        shapes,
                        occupied_by_layer,
                        clearance_by_layer=_route_spacing_clearance_by_layer(cfg),
                        clearance_shape_kinds=_route_spacing_clearance_shape_kinds(cfg),
                        include_same_net_spacing=bool(getattr(cfg, "route_spacing_check_same_net", False)),
                    ):
                        return drop_x, drop_y, bottom_layer, drop_route_layer, local_points, strap_points, landing_x
    return None


def _route_drop_layer_maze_path(
    pdk: PdkConfig,
    net: str,
    layer: str,
    start: tuple[float, float],
    goal: tuple[float, float],
    strap_x_range: tuple[float, float],
    cfg: StrapRouterConfig,
    min_w: float,
    occupied_by_layer: Mapping[str, Sequence[_OwnedShape]],
) -> tuple[tuple[float, float], ...] | None:
    start = _snap_pt(pdk, start[0], start[1])
    goal = _snap_pt(pdk, goal[0], goal[1])
    if abs(start[0] - goal[0]) <= 1e-12 and abs(start[1] - goal[1]) <= 1e-12:
        return (start,)
    step = max(float(getattr(cfg, "maze_escape_pitch_um", 0.5) or 0.5), float(min_w), 0.01)
    window = max(0.0, float(getattr(cfg, "maze_escape_window_um", 2.0) or 0.0))
    max_expansions = max(1, int(getattr(cfg, "maze_escape_max_expansions", 4096) or 4096))
    route_x0, route_x1 = (float(value) for value in strap_x_range)
    x_lo = max(min(start[0], goal[0]) - window, min(route_x0, route_x1) - window)
    x_hi = min(max(start[0], goal[0]) + window, max(route_x0, route_x1) + window)
    y_lo = min(start[1], goal[1]) - window
    y_hi = max(start[1], goal[1]) + window
    x_values = _maze_axis_values(pdk, start[0], goal[0], x_lo, x_hi, step, axis="x")
    y_values = _maze_axis_values(pdk, start[1], goal[1], y_lo, y_hi, step, axis="y")
    if not x_values or not y_values:
        return None
    try:
        start_key = (x_values.index(start[0]), y_values.index(start[1]))
        goal_key = (x_values.index(goal[0]), y_values.index(goal[1]))
    except ValueError:
        return None

    def point(key: tuple[int, int]) -> tuple[float, float]:
        return x_values[key[0]], y_values[key[1]]

    def heuristic(key: tuple[int, int]) -> float:
        px, py = point(key)
        return abs(px - goal[0]) + abs(py - goal[1])

    queue: list[tuple[float, int, tuple[int, int]]] = []
    heapq.heappush(queue, (heuristic(start_key), 0, start_key))
    came_from: dict[tuple[int, int], tuple[int, int] | None] = {start_key: None}
    cost_so_far: dict[tuple[int, int], float] = {start_key: 0.0}
    push_idx = 0
    expansions = 0
    while queue and expansions < max_expansions:
        _priority, _idx, current = heapq.heappop(queue)
        if current == goal_key:
            return _simplify_orthogonal_points(tuple(point(key) for key in _reconstruct_maze_keys(came_from, current)))
        expansions += 1
        cx, cy = current
        for nxt in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
            nx, ny = nxt
            if nx < 0 or nx >= len(x_values) or ny < 0 or ny >= len(y_values):
                continue
            if not _maze_segment_clear(pdk, net, layer, point(current), point(nxt), min_w, occupied_by_layer, cfg):
                continue
            new_cost = cost_so_far[current] + abs(point(current)[0] - point(nxt)[0]) + abs(point(current)[1] - point(nxt)[1])
            if nxt not in cost_so_far or new_cost < cost_so_far[nxt]:
                cost_so_far[nxt] = new_cost
                push_idx += 1
                heapq.heappush(queue, (new_cost + heuristic(nxt), push_idx, nxt))
                came_from[nxt] = current
    return None


def _maze_axis_values(
    pdk: PdkConfig,
    start_value: float,
    goal_value: float,
    lo: float,
    hi: float,
    step: float,
    *,
    axis: str,
) -> tuple[float, ...]:
    values = {float(start_value), float(goal_value)}
    count = max(1, int((float(hi) - float(lo)) / float(step)) + 3)
    origin = float(start_value)
    for idx in range(-count, count + 1):
        raw = origin + idx * float(step)
        if raw < float(lo) - 1e-12 or raw > float(hi) + 1e-12:
            continue
        snapped = _snap_pt(pdk, raw, 0.0)[0] if axis == "x" else _snap_pt(pdk, 0.0, raw)[1]
        if float(lo) - 1e-12 <= snapped <= float(hi) + 1e-12:
            values.add(snapped)
    return tuple(sorted(values))


def _maze_segment_clear(
    pdk: PdkConfig,
    net: str,
    layer: str,
    start: tuple[float, float],
    end: tuple[float, float],
    min_w: float,
    occupied_by_layer: Mapping[str, Sequence[_OwnedShape]],
    cfg: StrapRouterConfig,
) -> bool:
    del pdk
    if abs(start[0] - end[0]) <= 1e-12 and abs(start[1] - end[1]) <= 1e-12:
        return True
    candidate = SimpleNamespace(layer=layer, net=net, points=(start, end), width=min_w)
    return not _shapes_conflict_with_layer_index(
        _path_owned_shapes(candidate),
        occupied_by_layer,
        clearance_by_layer=_route_spacing_clearance_by_layer(cfg),
        clearance_shape_kinds=_route_spacing_clearance_shape_kinds(cfg),
        include_same_net_spacing=bool(getattr(cfg, "route_spacing_check_same_net", False)),
    )


def _reconstruct_maze_keys(
    came_from: Mapping[tuple[int, int], tuple[int, int] | None],
    current: tuple[int, int],
) -> tuple[tuple[int, int], ...]:
    path = [current]
    while came_from.get(current) is not None:
        current = came_from[current]  # type: ignore[index]
        path.append(current)
    return tuple(reversed(path))


def _simplify_orthogonal_points(points: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    deduped = list(_dedupe_consecutive_points(points))
    if len(deduped) <= 2:
        return tuple(deduped)
    simplified: list[tuple[float, float]] = [deduped[0]]
    for prev, current, nxt in zip(deduped, deduped[1:], deduped[2:]):
        same_x = abs(prev[0] - current[0]) <= 1e-12 and abs(current[0] - nxt[0]) <= 1e-12
        same_y = abs(prev[1] - current[1]) <= 1e-12 and abs(current[1] - nxt[1]) <= 1e-12
        if same_x or same_y:
            continue
        simplified.append(current)
    simplified.append(deduped[-1])
    return tuple(simplified)


def _route_terminal_to_existing_net(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    occupied: Sequence[_OwnedShape],
    targets: Sequence[_OwnedShape],
):
    from analogskills.eda.oa import OaPath

    metals = tuple(getattr(pdk.layer_map, "metals", ()) or ())
    target_rows = tuple(
        row
        for row in targets
        if str(getattr(row, "net", "") or "") == str(net) and str(getattr(row, "layer", "") or "") in metals
    )
    if not target_rows:
        return (), (), (), {"existing_net_route_reason": "no_routed_same_net_target"}

    term_x, term_y = _snap_pt(pdk, term.x, term.y)
    seed_rects, seed_vias, via_bottom, local_path_layer = _terminal_seed_artifacts(pdk, term, net, cfg, term_x, term_y, half_w)
    effective_bottom = pdk.layer_map.metals[0] if via_bottom in {pdk.layer_map.gate, "MD"} else via_bottom
    via_landing_margin = _route_via_landing_margin_um(cfg, min_w)
    sorted_targets = sorted(
        target_rows,
        key=lambda row: _bbox_manhattan_distance_to_point(row.bbox, term_x, term_y),
    )[: max(1, int(getattr(cfg, "existing_net_target_limit", 12) or 12))]
    x_steps = max(0, int(getattr(cfg, "existing_net_fanout_search_steps", 12) or 12))
    y_steps = max(0, int(getattr(cfg, "existing_net_fanout_y_search_steps", 1) or 1))
    occupied_by_layer = _occupied_shapes_by_layer(occupied)

    for target in sorted_targets:
        target_layer = str(target.layer)
        target_x, target_y = _target_point_on_shape(pdk, target.bbox, term_x, term_y)
        fanout_on_drop = _terminal_fanout_on_drop_layer(pdk, cfg, term, effective_bottom, target_layer)
        for x_step, y_step in _fanout_step_pairs(x_steps, y_steps):
            drop_x, drop_y = _snap_pt(
                pdk,
                term_x + x_step * cfg.fanout_pitch_um,
                term_y + y_step * cfg.fanout_pitch_um,
            )
            for local_points in _fanout_path_options(term_x, term_y, drop_x, drop_y):
                for target_points in _fanout_path_options(drop_x, drop_y, target_x, target_y):
                    route_axis = "" if fanout_on_drop else _first_nonzero_segment_axis(local_points)
                    seed_rects, seed_vias, via_bottom, local_path_layer = _terminal_seed_artifacts(
                        pdk,
                        term,
                        net,
                        cfg,
                        term_x,
                        term_y,
                        half_w,
                        route_axis=route_axis,
                        route_width_um=min_w,
                    )
                    candidate_paths: list[OaPath] = []
                    candidate_rects = list(seed_rects)
                    candidate_vias = list(seed_vias)
                    if fanout_on_drop:
                        candidate_rects.extend(_access_landing_rects(pdk, term_x, term_y, via_bottom, target_layer, net, cfg, half_w, term.contact_layer))
                        candidate_vias.extend(
                            _via_stack(
                                pdk,
                                term_x,
                                term_y,
                                effective_bottom,
                                target_layer,
                                net,
                                landing_margin_um=via_landing_margin,
                                route_width_um=min_w,
                                wide_metal_multicut_vias=bool(getattr(cfg, "wide_metal_multicut_vias", False)),
                                wide_metal_multicut_via_defs=tuple(getattr(cfg, "wide_metal_multicut_via_defs", ()) or ()),
                                wide_metal_multicut_axis_by_via=dict(getattr(cfg, "wide_metal_multicut_axis_by_via", {}) or {}),
                            )
                        )
                        _append_nonzero_path(candidate_paths, OaPath(target_layer, "drawing", local_points, min_w, net))
                    else:
                        _append_nonzero_path(candidate_paths, OaPath(local_path_layer, "drawing", local_points, min_w, net))
                        candidate_rects.extend(_access_landing_rects(pdk, drop_x, drop_y, via_bottom, target_layer, net, cfg, half_w, term.contact_layer))
                        candidate_vias.extend(
                            _via_stack(
                                pdk,
                                drop_x,
                                drop_y,
                                effective_bottom,
                                target_layer,
                                net,
                                landing_margin_um=via_landing_margin,
                                route_width_um=min_w,
                                wide_metal_multicut_vias=bool(getattr(cfg, "wide_metal_multicut_vias", False)),
                                wide_metal_multicut_via_defs=tuple(getattr(cfg, "wide_metal_multicut_via_defs", ()) or ()),
                                wide_metal_multicut_axis_by_via=dict(getattr(cfg, "wide_metal_multicut_axis_by_via", {}) or {}),
                            )
                        )
                    _append_nonzero_path(candidate_paths, OaPath(target_layer, "drawing", target_points, min_w, net))
                    candidate_shapes: list[_OwnedShape] = []
                    for path in candidate_paths:
                        candidate_shapes.extend(_path_owned_shapes(path))
                    candidate_shapes.extend(_rect_owned_shapes(candidate_rects))
                    candidate_shapes.extend(_via_owned_shapes(candidate_vias, pdk))
                    if not _shapes_conflict_with_layer_index(
                        candidate_shapes,
                        occupied_by_layer,
                        clearance_by_layer=_route_spacing_clearance_by_layer(cfg),
                        clearance_shape_kinds=_route_spacing_clearance_shape_kinds(cfg),
                        include_same_net_spacing=bool(getattr(cfg, "route_spacing_check_same_net", False)),
                    ):
                        return tuple(candidate_paths), tuple(candidate_rects), tuple(candidate_vias), None
    return (), (), (), {"existing_net_route_reason": "no_conflict_free_same_net_target"}


def _terminal_seed_artifacts(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    cfg: StrapRouterConfig,
    term_x: float,
    term_y: float,
    half_w: float,
    *,
    route_axis: str = "",
    route_width_um: float | None = None,
):
    from analogskills.eda.oa import OaRect, OaVia

    del half_w
    rects: list[OaRect] = []
    vias: list[OaVia] = []
    via_bottom = term.layer
    local_path_layer = term.layer
    if term.layer == pdk.layer_map.gate:
        metal0 = pdk.layer_map.metals[0]
        if term.contact_layer in {"M0_PO", "M0_PO_VD"}:
            vias.append(OaVia(term.contact_layer, (term_x, term_y), net))
        else:
            rects.extend(
                _gate_contact_landing_rects(
                    pdk,
                    term_x,
                    term_y,
                    term.contact_layer or pdk.layer_map.contact,
                    net,
                    cfg,
                    str(getattr(term, "logical_name", "") or ""),
                    tuple(getattr(term, "gate_po_x_span_um", ()) or ()),
                    route_axis=route_axis,
                    route_width_um=route_width_um,
                )
            )
        via_bottom = metal0
        local_path_layer = metal0
    elif term.layer in {"OD", "PDK", "NW"} and term.contact_layer in {"M0_SUB", "M0_NW"}:
        metal0 = pdk.layer_map.metals[0]
        vias.append(OaVia(term.contact_layer, (term_x, term_y), net))
        via_bottom = metal0
        local_path_layer = metal0
    return tuple(rects), tuple(vias), via_bottom, local_path_layer


def _select_drop_point(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    strap_x_range: tuple[float, float],
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    occupied: Sequence[_OwnedShape],
) -> tuple[float, float, str, str, tuple[tuple[float, float], ...], tuple[tuple[float, float], ...], float] | None:
    term_x, term_y = _snap_pt(pdk, term.x, term.y)
    bottom_layer = pdk.layer_map.metals[0] if term.layer == pdk.layer_map.gate else term.layer
    occupied_by_layer = _occupied_shapes_by_layer(occupied)
    for drop_route_layer in _drop_route_layer_candidates(pdk, cfg, route_layer):
        for x_step, y_step in _fanout_step_pairs(cfg.fanout_search_steps, cfg.fanout_y_search_steps):
            drop_x, drop_y = _snap_pt(
                pdk,
                term_x + x_step * cfg.fanout_pitch_um,
                term_y + y_step * cfg.fanout_pitch_um,
            )
            landing_candidates = (
                (drop_x,)
                if int(getattr(cfg, "strap_landing_search_steps", 0) or 0) <= 0
                else _strap_landing_xs(pdk, cfg, drop_x, strap_x_range)
            )
            for landing_x in landing_candidates:
                fanout_paths = _select_clear_fanout_path(
                    pdk,
                    term,
                    net,
                    route_layer,
                    strap_y,
                    cfg,
                    min_w,
                    half_w,
                    occupied,
                    drop_x,
                    drop_y,
                    bottom_layer,
                    drop_route_layer,
                    landing_x,
                    occupied_by_layer=occupied_by_layer,
                )
                if fanout_paths is not None:
                    local_fanout_points, strap_points = fanout_paths
                    return drop_x, drop_y, bottom_layer, drop_route_layer, local_fanout_points, strap_points, landing_x
    return None


def _candidate_is_clear(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    occupied: Sequence[_OwnedShape],
    drop_x: float,
    drop_y: float,
    bottom_layer: str,
    drop_route_layer: str | None = None,
    landing_x: float | None = None,
) -> bool:
    return (
        _select_clear_fanout_path(
            pdk,
            term,
            net,
            route_layer,
            strap_y,
            cfg,
            min_w,
            half_w,
            occupied,
            drop_x,
            drop_y,
            bottom_layer,
            drop_route_layer,
            landing_x,
        )
        is not None
    )


def _select_clear_fanout_path(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    occupied: Sequence[_OwnedShape],
    drop_x: float,
    drop_y: float,
    bottom_layer: str,
    drop_route_layer: str | None = None,
    landing_x: float | None = None,
    *,
    occupied_by_layer: Mapping[str, Sequence[_OwnedShape]] | None = None,
) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]] | None:
    term_x, term_y = _snap_pt(pdk, term.x, term.y)
    if landing_x is None:
        landing_x = drop_x
    landing_x, strap_y = _snap_pt(pdk, landing_x, strap_y)
    layer_index = occupied_by_layer if occupied_by_layer is not None else _occupied_shapes_by_layer(occupied)
    for local_points in _fanout_path_options(term_x, term_y, drop_x, drop_y):
        for strap_points in _fanout_path_options(drop_x, drop_y, landing_x, strap_y):
            shapes = [
                shape
                for _stage, group in _fanout_candidate_shape_groups(
                    pdk,
                    term,
                    net,
                    route_layer,
                    strap_y,
                    cfg,
                    min_w,
                    half_w,
                    drop_x,
                    drop_y,
                    bottom_layer,
                    drop_route_layer,
                    landing_x,
                    local_points,
                    strap_points,
                )
                for shape in group
            ]
            if not _shapes_conflict_with_layer_index(
                shapes,
                layer_index,
                clearance_by_layer=_route_spacing_clearance_by_layer(cfg),
                clearance_shape_kinds=_route_spacing_clearance_shape_kinds(cfg),
                include_same_net_spacing=bool(getattr(cfg, "route_spacing_check_same_net", False)),
            ):
                return local_points, strap_points
    return None


def _fanout_candidate_shape_groups(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    drop_x: float,
    drop_y: float,
    bottom_layer: str,
    drop_route_layer: str | None,
    landing_x: float | None,
    local_points: Sequence[tuple[float, float]],
    strap_points: Sequence[tuple[float, float]],
) -> tuple[tuple[str, tuple[_OwnedShape, ...]], ...]:
    from analogskills.eda.oa import OaPath

    term_x, term_y = _snap_pt(pdk, term.x, term.y)
    if landing_x is None:
        landing_x = drop_x
    landing_x, strap_y = _snap_pt(pdk, landing_x, strap_y)
    effective_drop_layer = drop_route_layer or _drop_route_layer(pdk, cfg, route_layer)
    effective_bottom = pdk.layer_map.metals[0] if bottom_layer in {pdk.layer_map.gate, "MD"} else bottom_layer
    fanout_on_drop = _terminal_fanout_on_drop_layer(pdk, cfg, term, effective_bottom, effective_drop_layer)
    via_landing_margin = _route_via_landing_margin_um(cfg, min_w)
    local_path_layer = term.layer
    if term.layer == pdk.layer_map.gate:
        local_path_layer = pdk.layer_map.metals[0]
    elif term.layer in {"OD", "PDK", "NW"} and term.contact_layer in {"M0_SUB", "M0_NW"}:
        local_path_layer = pdk.layer_map.metals[0]

    groups: list[tuple[str, tuple[_OwnedShape, ...]]] = []
    base_shapes = list(_terminal_base_owned_shapes(pdk, term, net, cfg, term_x, term_y))
    if term.layer in {"OD", "PDK", "NW"} and term.contact_layer in {"M0_SUB", "M0_NW"}:
        base_shapes.extend(
            _via_owned_shapes(
                (SimpleNamespace(via_def=term.contact_layer, xy=(term_x, term_y), net=net, rows=1, cols=1),),
                pdk,
            )
        )
    if base_shapes:
        groups.append(("terminal_seed", tuple(base_shapes)))

    if fanout_on_drop:
        access_shapes = tuple(
            _rect_owned_shapes(_access_landing_rects(pdk, term_x, term_y, bottom_layer, effective_drop_layer, net, cfg, half_w, term.contact_layer))
            + _via_owned_shapes(
                _via_stack(
                    pdk,
                    term_x,
                    term_y,
                    effective_bottom,
                    effective_drop_layer,
                    net,
                    landing_margin_um=via_landing_margin,
                    route_width_um=min_w,
                    wide_metal_multicut_vias=bool(getattr(cfg, "wide_metal_multicut_vias", False)),
                    wide_metal_multicut_via_defs=tuple(getattr(cfg, "wide_metal_multicut_via_defs", ()) or ()),
                    wide_metal_multicut_axis_by_via=dict(getattr(cfg, "wide_metal_multicut_axis_by_via", {}) or {}),
                ),
                pdk,
            )
        )
        local_shapes = _path_owned_shapes(OaPath(effective_drop_layer, "drawing", tuple(local_points), min_w, net))
    else:
        local_shapes = _path_owned_shapes(OaPath(local_path_layer, "drawing", tuple(local_points), min_w, net))
        access_shapes = tuple(
            _rect_owned_shapes(_access_landing_rects(pdk, drop_x, drop_y, bottom_layer, effective_drop_layer, net, cfg, half_w, term.contact_layer))
            + _via_owned_shapes(
                _via_stack(
                    pdk,
                    drop_x,
                    drop_y,
                    effective_bottom,
                    effective_drop_layer,
                    net,
                    landing_margin_um=via_landing_margin,
                    route_width_um=min_w,
                    wide_metal_multicut_vias=bool(getattr(cfg, "wide_metal_multicut_vias", False)),
                    wide_metal_multicut_via_defs=tuple(getattr(cfg, "wide_metal_multicut_via_defs", ()) or ()),
                    wide_metal_multicut_axis_by_via=dict(getattr(cfg, "wide_metal_multicut_axis_by_via", {}) or {}),
                ),
                pdk,
            )
        )
    if access_shapes:
        groups.append(("terminal_access", access_shapes))
    if local_shapes:
        groups.append(("local_fanout", local_shapes))

    strap_shapes = _path_owned_shapes(OaPath(effective_drop_layer, "drawing", tuple(strap_points), min_w, net))
    if strap_shapes:
        groups.append(("strap_escape", strap_shapes))
    strap_via_shapes = _via_owned_shapes(
        _via_stack(
            pdk,
            landing_x,
            strap_y,
            effective_drop_layer,
            route_layer,
            net,
            landing_margin_um=via_landing_margin,
            route_width_um=min_w,
            wide_metal_multicut_vias=bool(getattr(cfg, "wide_metal_multicut_vias", False)),
            wide_metal_multicut_via_defs=tuple(getattr(cfg, "wide_metal_multicut_via_defs", ()) or ()),
            wide_metal_multicut_axis_by_via=dict(getattr(cfg, "wide_metal_multicut_axis_by_via", {}) or {}),
        ),
        pdk,
    )
    if strap_via_shapes:
        groups.append(("strap_landing_via", strap_via_shapes))
    return tuple(groups)


def _diagnose_drop_point_blockers(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    route_layer: str,
    strap_y: float,
    strap_x_range: tuple[float, float],
    cfg: StrapRouterConfig,
    min_w: float,
    half_w: float,
    occupied: Sequence[_OwnedShape],
) -> dict[str, object]:
    candidate_limit = max(0, int(getattr(cfg, "blocker_diagnostic_candidate_limit", 4) or 0))
    sample_limit = max(0, int(getattr(cfg, "blocker_diagnostic_sample_limit", 8) or 0))
    conflicts_per_candidate = max(1, int(getattr(cfg, "blocker_diagnostic_conflicts_per_candidate", 4) or 4))
    if candidate_limit <= 0 or not occupied:
        return {
            "blocker_candidate_count": 0,
            "blocker_diagnostic_truncated": False,
            "blocker_layer_counts": {},
            "blocker_net_counts": {},
            "blocker_stage_counts": {},
            "blocker_samples": (),
        }

    term_x, term_y = _snap_pt(pdk, term.x, term.y)
    bottom_layer = pdk.layer_map.metals[0] if term.layer == pdk.layer_map.gate else term.layer
    layer_counts: Counter[str] = Counter()
    net_counts: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    occupied_by_layer = _occupied_shapes_by_layer(occupied)
    samples: list[dict[str, object]] = []
    checked = 0
    conflict_candidate_count = 0
    truncated = False

    for drop_route_layer in _drop_route_layer_candidates(pdk, cfg, route_layer):
        for x_step, y_step in _fanout_step_pairs(cfg.fanout_search_steps, cfg.fanout_y_search_steps):
            drop_x, drop_y = _snap_pt(
                pdk,
                term_x + x_step * cfg.fanout_pitch_um,
                term_y + y_step * cfg.fanout_pitch_um,
            )
            landing_candidates = (
                (drop_x,)
                if int(getattr(cfg, "strap_landing_search_steps", 0) or 0) <= 0
                else _strap_landing_xs(pdk, cfg, drop_x, strap_x_range)
            )
            for landing_x in landing_candidates:
                landing_x, snapped_strap_y = _snap_pt(pdk, landing_x, strap_y)
                for local_points in _fanout_path_options(term_x, term_y, drop_x, drop_y):
                    for strap_points in _fanout_path_options(drop_x, drop_y, landing_x, snapped_strap_y):
                        if checked >= candidate_limit:
                            truncated = True
                            break
                        checked += 1
                        candidate_has_conflict = False
                        remaining_conflicts = conflicts_per_candidate
                        for stage, shapes in _fanout_candidate_shape_groups(
                            pdk,
                            term,
                            net,
                            route_layer,
                            strap_y,
                            cfg,
                            min_w,
                            half_w,
                            drop_x,
                            drop_y,
                            bottom_layer,
                            drop_route_layer,
                            landing_x,
                            local_points,
                            strap_points,
                        ):
                            records = _shape_conflict_records(shapes, occupied_by_layer, stage=stage, limit=remaining_conflicts)
                            if not records:
                                continue
                            candidate_has_conflict = True
                            remaining_conflicts -= len(records)
                            for record in records:
                                layer_counts[str(record.get("occupied_layer", ""))] += 1
                                net_counts[str(record.get("occupied_net", ""))] += 1
                                stage_counts[str(record.get("stage", ""))] += 1
                                if len(samples) < sample_limit:
                                    samples.append(record)
                            if remaining_conflicts <= 0:
                                break
                        if candidate_has_conflict:
                            conflict_candidate_count += 1
                    if truncated:
                        break
                if truncated:
                    break
            if truncated:
                break
        if truncated:
            break

    return {
        "blocker_candidate_count": checked,
        "blocker_conflict_candidate_count": conflict_candidate_count,
        "blocker_diagnostic_truncated": truncated,
        "blocker_layer_counts": _sorted_counter_dict(layer_counts),
        "blocker_net_counts": _sorted_counter_dict(net_counts),
        "blocker_stage_counts": _sorted_counter_dict(stage_counts),
        "blocker_samples": tuple(samples),
    }


def _shape_conflict_records(
    candidates: Sequence[_OwnedShape],
    occupied_by_layer: Mapping[str, Sequence[_OwnedShape]],
    *,
    stage: str,
    limit: int,
) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    max_records = max(1, int(limit))
    for candidate in candidates:
        for shape in tuple(occupied_by_layer.get(str(candidate.layer), ()) or ()):
            if candidate.net != shape.net and bbox_overlaps(candidate.bbox, shape.bbox):
                records.append(
                    {
                        "stage": str(stage),
                        "candidate_layer": str(candidate.layer),
                        "candidate_net": str(candidate.net),
                        "candidate_bbox": tuple(float(value) for value in candidate.bbox),
                        "occupied_layer": str(shape.layer),
                        "occupied_net": str(shape.net),
                        "occupied_bbox": tuple(float(value) for value in shape.bbox),
                    }
                )
                if len(records) >= max_records:
                    return tuple(records)
    return tuple(records)


def _occupied_shapes_by_layer(occupied: Sequence[_OwnedShape]) -> dict[str, tuple[_OwnedShape, ...]]:
    grouped: dict[str, list[_OwnedShape]] = {}
    for shape in tuple(occupied or ()):
        layer = str(getattr(shape, "layer", "") or "")
        if not layer:
            continue
        grouped.setdefault(layer, []).append(shape)
    return {layer: tuple(shapes) for layer, shapes in grouped.items()}


def _sorted_counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {
        str(key): int(value)
        for key, value in sorted(counter.items(), key=lambda item: (-int(item[1]), str(item[0])))
        if key
    }


def _terminal_base_owned_shapes(
    pdk: PdkConfig,
    term: _TerminalAccess,
    net: str,
    cfg: StrapRouterConfig,
    term_x: float,
    term_y: float,
) -> tuple[_OwnedShape, ...]:
    if term.layer == pdk.layer_map.gate:
        if term.contact_layer in {"M0_PO", "M0_PO_VD"}:
            return _via_owned_shapes((SimpleNamespace(via_def=term.contact_layer, xy=(term_x, term_y), net=net, rows=1, cols=1),), pdk)
        return _rect_owned_shapes(
            _gate_contact_landing_rects(
                pdk,
                term_x,
                term_y,
                term.contact_layer or pdk.layer_map.contact,
                net,
                cfg,
                str(getattr(term, "logical_name", "") or ""),
                tuple(getattr(term, "gate_po_x_span_um", ()) or ()),
            )
        )
    return ()


def _fanout_path_options(
    term_x: float,
    term_y: float,
    drop_x: float,
    drop_y: float,
) -> tuple[tuple[tuple[float, float], ...], ...]:
    start = (float(term_x), float(term_y))
    end = (float(drop_x), float(drop_y))
    if abs(start[0] - end[0]) <= 1e-12 or abs(start[1] - end[1]) <= 1e-12:
        return (_dedupe_consecutive_points((start, end)),)
    horizontal_first = _dedupe_consecutive_points((start, (end[0], start[1]), end))
    vertical_first = _dedupe_consecutive_points((start, (start[0], end[1]), end))
    if horizontal_first == vertical_first:
        return (horizontal_first,)
    return (horizontal_first, vertical_first)


def _first_nonzero_segment_axis(points: Sequence[tuple[float, float]]) -> str:
    rows = tuple(points or ())
    for start, end in zip(rows, rows[1:]):
        dx = abs(float(end[0]) - float(start[0]))
        dy = abs(float(end[1]) - float(start[1]))
        if dx <= 1e-12 and dy <= 1e-12:
            continue
        return "x" if dx >= dy else "y"
    return ""


def _target_point_on_shape(
    pdk: PdkConfig,
    bbox: tuple[float, float, float, float],
    x: float,
    y: float,
) -> tuple[float, float]:
    x0, y0, x1, y1 = (float(value) for value in bbox)
    target_x = min(max(float(x), min(x0, x1)), max(x0, x1))
    target_y = min(max(float(y), min(y0, y1)), max(y0, y1))
    return _snap_pt(pdk, target_x, target_y)


def _strap_landing_xs(
    pdk: PdkConfig,
    cfg: StrapRouterConfig,
    drop_x: float,
    strap_x_range: tuple[float, float],
) -> tuple[float, ...]:
    x0, x1 = (float(value) for value in strap_x_range)
    lo, hi = min(x0, x1), max(x0, x1)
    candidates: list[float] = []
    for step in _fanout_steps(max(0, int(getattr(cfg, "strap_landing_search_steps", 0) or 0))):
        candidate_x = _snap_pt(pdk, float(drop_x) + step * float(cfg.fanout_pitch_um), 0.0)[0]
        if lo - 1e-12 <= candidate_x <= hi + 1e-12 and candidate_x not in candidates:
            candidates.append(candidate_x)
    if not candidates:
        clamped_x = min(max(float(drop_x), lo), hi)
        candidates.append(_snap_pt(pdk, clamped_x, 0.0)[0])
    return tuple(candidates)


def _bbox_manhattan_distance_to_point(
    bbox: tuple[float, float, float, float],
    x: float,
    y: float,
) -> float:
    x0, y0, x1, y1 = (float(value) for value in bbox)
    lo_x, hi_x = min(x0, x1), max(x0, x1)
    lo_y, hi_y = min(y0, y1), max(y0, y1)
    dx = 0.0 if lo_x <= float(x) <= hi_x else min(abs(float(x) - lo_x), abs(float(x) - hi_x))
    dy = 0.0 if lo_y <= float(y) <= hi_y else min(abs(float(y) - lo_y), abs(float(y) - hi_y))
    return dx + dy


def _drop_route_layer(pdk: PdkConfig, cfg: StrapRouterConfig, route_layer: str) -> str:
    return _drop_route_layer_candidates(pdk, cfg, route_layer)[0]


def _drop_route_layer_candidates(pdk: PdkConfig, cfg: StrapRouterConfig, route_layer: str) -> tuple[str, ...]:
    del pdk
    raw_layers = tuple(getattr(cfg, "drop_route_layers", ()) or ())
    if not raw_layers:
        raw_layers = (getattr(cfg, "drop_route_layer", "") or "",)
    layers: list[str] = []
    for raw in raw_layers:
        text = str(raw or "").strip()
        layer = route_layer if not text or text.lower() in {"same", "route", "strap"} else text
        if layer not in layers:
            layers.append(layer)
    return tuple(layers or (route_layer,))


def _fanout_on_drop_layer(pdk: PdkConfig, cfg: StrapRouterConfig, bottom_layer: str, drop_route_layer: str) -> bool:
    if not bool(getattr(cfg, "fanout_on_drop_layer", False)):
        return False
    metals = tuple(getattr(pdk.layer_map, "metals", ()) or ())
    return str(bottom_layer) in metals and str(drop_route_layer) in metals and str(bottom_layer) != str(drop_route_layer)


def _terminal_fanout_on_drop_layer(
    pdk: PdkConfig,
    cfg: StrapRouterConfig,
    term: _TerminalAccess,
    bottom_layer: str,
    drop_route_layer: str,
) -> bool:
    if _fanout_on_drop_layer(pdk, cfg, bottom_layer, drop_route_layer):
        return True
    if str(term.layer) != str(pdk.layer_map.gate):
        return False
    if not bool(getattr(cfg, "gate_fanout_on_drop_layer", False)):
        return False
    metals = tuple(getattr(pdk.layer_map, "metals", ()) or ())
    return str(bottom_layer) in metals and str(drop_route_layer) in metals and str(bottom_layer) != str(drop_route_layer)


def _fanout_steps(max_steps: int) -> tuple[int, ...]:
    steps = [0]
    for idx in range(1, max(0, int(max_steps)) + 1):
        steps.extend((idx, -idx))
    return tuple(steps)


def _fanout_step_pairs(x_steps: int, y_steps: int) -> tuple[tuple[int, int], ...]:
    pairs: list[tuple[int, int]] = []
    for y_step in _fanout_steps(max(0, int(y_steps))):
        for x_step in _fanout_steps(max(0, int(x_steps))):
            pairs.append((x_step, y_step))
    return tuple(pairs)


def _dedupe_consecutive_points(points: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    deduped: list[tuple[float, float]] = []
    for x, y in points:
        point = (float(x), float(y))
        if deduped and abs(deduped[-1][0] - point[0]) <= 1e-12 and abs(deduped[-1][1] - point[1]) <= 1e-12:
            continue
        deduped.append(point)
    return tuple(deduped)


def _append_nonzero_path(paths: list[Any], path: Any) -> None:
    if _path_has_nonzero_length(path):
        paths.append(path)


def _path_has_nonzero_length(path: Any) -> bool:
    points = tuple(getattr(path, "points", ()) or ())
    if len(points) < 2:
        return False
    for first, second in zip(points, points[1:]):
        if abs(float(first[0]) - float(second[0])) > 1e-12 or abs(float(first[1]) - float(second[1])) > 1e-12:
            return True
    return False


def _path_owned_shapes(path: Any) -> tuple[_OwnedShape, ...]:
    return tuple(
        _OwnedShape(path.layer, path.net, bbox, "path")
        for bbox in path_segment_bboxes(tuple(getattr(path, "points", ())), float(getattr(path, "width", 0.0) or 0.0))
        if getattr(path, "net", "")
    )


def _rect_owned_shapes(rects: Iterable[Any]) -> tuple[_OwnedShape, ...]:
    return tuple(_OwnedShape(rect.layer, rect.net, rect.bbox, "rect") for rect in rects if getattr(rect, "net", ""))


def _via_owned_shapes(vias: Iterable[Any], pdk: PdkConfig) -> tuple[_OwnedShape, ...]:
    owned: list[_OwnedShape] = []
    for via in vias:
        net = str(getattr(via, "net", "") or "")
        if not net:
            continue
        for layer, bbox in via_landing_bboxes(via, pdk):
            owned.append(_OwnedShape(layer, net, bbox, "via_landing"))
    return tuple(owned)


def _shapes_conflict(
    candidates: Sequence[_OwnedShape],
    occupied: Sequence[_OwnedShape],
    *,
    clearance_by_layer: Mapping[str, float] | None = None,
    clearance_shape_kinds: set[str] | tuple[str, ...] | None = None,
    include_same_net_spacing: bool = False,
) -> bool:
    for candidate in candidates:
        for shape in occupied:
            if _owned_shapes_conflict(
                candidate,
                shape,
                clearance_by_layer=clearance_by_layer,
                clearance_shape_kinds=clearance_shape_kinds,
                include_same_net_spacing=include_same_net_spacing,
            ):
                return True
    return False


def _shapes_conflict_with_layer_index(
    candidates: Sequence[_OwnedShape],
    occupied_by_layer: Mapping[str, Sequence[_OwnedShape]],
    *,
    clearance_by_layer: Mapping[str, float] | None = None,
    clearance_shape_kinds: set[str] | tuple[str, ...] | None = None,
    include_same_net_spacing: bool = False,
) -> bool:
    for candidate in candidates:
        for shape in tuple(occupied_by_layer.get(str(candidate.layer), ()) or ()):
            if _owned_shapes_conflict(
                candidate,
                shape,
                clearance_by_layer=clearance_by_layer,
                clearance_shape_kinds=clearance_shape_kinds,
                include_same_net_spacing=include_same_net_spacing,
            ):
                return True
    return False


def _owned_shapes_conflict(
    candidate: _OwnedShape,
    shape: _OwnedShape,
    *,
    clearance_by_layer: Mapping[str, float] | None = None,
    clearance_shape_kinds: set[str] | tuple[str, ...] | None = None,
    include_same_net_spacing: bool = False,
) -> bool:
    if str(candidate.layer) != str(shape.layer):
        return False
    same_net = str(candidate.net) == str(shape.net)
    if same_net and not bool(include_same_net_spacing):
        return False
    clearance = _route_spacing_clearance_for_layer(clearance_by_layer, str(candidate.layer))
    if clearance <= 0.0:
        return (not same_net) and bbox_overlaps(candidate.bbox, shape.bbox)
    allowed_kinds = {str(kind) for kind in tuple(clearance_shape_kinds or ()) if str(kind)}
    if allowed_kinds and str(getattr(candidate, "kind", "") or "") not in allowed_kinds and str(getattr(shape, "kind", "") or "") not in allowed_kinds:
        return (not same_net) and bbox_overlaps(candidate.bbox, shape.bbox)
    if bbox_overlaps(candidate.bbox, shape.bbox, include_touching=True):
        return not same_net
    gap = _bbox_spacing_um(candidate.bbox, shape.bbox)
    return gap < clearance - 1e-12


def _route_spacing_clearance_by_layer(cfg: StrapRouterConfig) -> dict[str, float]:
    raw = getattr(cfg, "route_spacing_clearance_um_by_layer", {}) or {}
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, float] = {}
    for key, value in raw.items():
        layer = str(key)
        if not layer:
            continue
        try:
            clearance = max(float(value), 0.0)
        except (TypeError, ValueError):
            continue
        if clearance > 0.0:
            result[layer] = clearance
    return result


def _route_spacing_clearance_shape_kinds(cfg: StrapRouterConfig) -> tuple[str, ...]:
    return tuple(
        str(kind)
        for kind in tuple(getattr(cfg, "route_spacing_clearance_shape_kinds", ()) or ())
        if str(kind)
    )


def _route_spacing_clearance_for_layer(clearance_by_layer: Mapping[str, float] | None, layer: str) -> float:
    # This is a hot path during fanout candidate search.  The caller already
    # normalizes the mapping; a runtime ``typing.Mapping`` instance check here
    # dominates routing time for dense analog blocks.
    if not clearance_by_layer:
        return 0.0
    try:
        return max(float(clearance_by_layer.get(str(layer), 0.0) or 0.0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _bbox_spacing_um(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    dx = max(float(right[0]) - float(left[2]), float(left[0]) - float(right[2]), 0.0)
    dy = max(float(right[1]) - float(left[3]), float(left[1]) - float(right[3]), 0.0)
    if dx <= 0.0:
        return dy
    if dy <= 0.0:
        return dx
    return (dx * dx + dy * dy) ** 0.5


def _snap_pt(pdk: PdkConfig, x: float, y: float) -> tuple[float, float]:
    return pdk.rules.snap_point_um((float(x), float(y)))
