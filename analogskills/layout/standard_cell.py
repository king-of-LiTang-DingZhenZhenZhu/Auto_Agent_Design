"""Constraint-driven standard-cell search with domain pruning and Z3 solve."""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from math import inf
import os
from typing import Any, Mapping, Sequence

from analogskills.contracts import (
    LayoutConstraintSet,
    StandardCellDeviceClusterConstraint,
    StandardCellDeviceConstraint,
    StandardCellInternalNetClusterConstraint,
    StandardCellNetConstraint,
    StandardCellPinGroupConstraint,
    TopologyGraph,
)
from analogskills.env import get_env

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - fallback is covered by DFS tests
    z3 = None


@dataclass(frozen=True)
class StandardCellProblem:
    graph_name: str
    rows: tuple[str, ...]
    columns: tuple[int, ...]
    device_order: tuple[str, ...]
    device_constraints: tuple[StandardCellDeviceConstraint, ...]
    net_constraints: tuple[StandardCellNetConstraint, ...]
    device_clusters: tuple[StandardCellDeviceClusterConstraint, ...] = ()
    pin_groups: tuple[StandardCellPinGroupConstraint, ...] = ()
    internal_net_clusters: tuple[StandardCellInternalNetClusterConstraint, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class StandardCellSolution:
    device_columns: tuple[tuple[str, int], ...]
    net_tracks: tuple[tuple[str, int], ...]
    width_columns: int
    cost: float
    device_orientations: tuple[tuple[str, str], ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def device_column_map(self) -> dict[str, int]:
        return {name: column for name, column in self.device_columns}

    def device_orientation_map(self) -> dict[str, str]:
        return {name: orient for name, orient in self.device_orientations}

    def net_track_map(self) -> dict[str, int]:
        return {name: track for name, track in self.net_tracks}


@dataclass(frozen=True)
class StandardCellSolveStats:
    backend: str = "dfs"
    device_states_visited: int = 0
    routing_states_visited: int = 0
    pruned_states: int = 0
    feasible_solutions: int = 0
    prune_reasons: tuple[tuple[str, int], ...] = ()
    domain_reduction_count: int = 0
    reduced_device_domains: tuple[tuple[str, tuple[int, ...]], ...] = ()
    reduced_track_domains: tuple[tuple[str, tuple[int, ...]], ...] = ()
    reduced_pin_domains: tuple[tuple[str, tuple[int, ...]], ...] = ()
    solver_checks: int = 0
    branch_nodes: int = 0
    bound_updates: int = 0
    incumbent_cost: float | None = None


@dataclass(frozen=True)
class StandardCellSolveResult:
    problem: StandardCellProblem
    solutions: tuple[StandardCellSolution, ...]
    stats: StandardCellSolveStats


@dataclass(frozen=True)
class StandardCellRouteConfig:
    route_layers: tuple[str, ...] = ()
    net_route_layer_overrides: Mapping[str, str] = field(default_factory=dict)
    gate_local_templates: tuple[str, ...] = ()
    gate_route_layer: str = ""
    signal_route_layer: str = ""
    output_route_layer: str = ""
    rail_route_layer: str = ""
    min_route_width_um: float = 0.1
    gate_landing_size_um: float = 0.14
    contact_cut_size_um: float = 0.06
    breakout_margin_um: float = 0.02
    pin_origin_um: tuple[float, float] = (0.0, 20.0)
    pin_pitch_um: float = 0.5
    pin_size_um: float = 0.2
    pin_layer_overrides: Mapping[str, str] = field(default_factory=dict)
    pin_y_overrides: Mapping[str, float] = field(default_factory=dict)
    pin_y_candidates_um: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    trunk_y_overrides: Mapping[str, float] = field(default_factory=dict)
    pin_column_positions: Mapping[str, Mapping[int, float]] = field(default_factory=dict)
    top_track_base_y_um: float = 20.0
    internal_track_base_y_um: float = 1.0
    output_track_base_y_um: float = 2.0
    track_pitch_um: float = 0.3


@dataclass(frozen=True)
class StandardCellRouteResult:
    plan: Any
    physical_report: dict[str, object]
    boundary_pins: tuple[Any, ...] = ()
    trunk_y_by_net: Mapping[str, float] = field(default_factory=dict)
    layer_by_net: Mapping[str, str] = field(default_factory=dict)
    local_template_by_net: Mapping[str, str] = field(default_factory=dict)
    option_summary_by_net: Mapping[str, Mapping[str, object]] = field(default_factory=dict)


@dataclass(frozen=True)
class _RouteAnchor:
    net: str
    x: float
    y: float
    layer: str
    bbox: tuple[float, float, float, float] | None = None
    contact_layer: str = ""
    source: str = ""
    access_priority: int = 50
    lvs_safe: bool = True
    terminal_name: str = ""
    is_top_level_pin: bool = False


@dataclass(frozen=True)
class _RouteShape:
    layer: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class _RouteOption:
    index: int
    route_layer: str
    trunk_y: float
    pin_layer: str
    cost: int
    local_template: str = "direct_stack"
    anchors: tuple[_RouteAnchor, ...] = ()
    top_pin_xy: tuple[float, float] | None = None
    top_pin_column: int | None = None
    bridge_x: float | None = None
    shapes: tuple[_RouteShape, ...] = ()


@dataclass(frozen=True)
class _PrunedDomains:
    device_domains: Mapping[str, tuple[int, ...]]
    track_domains: Mapping[str, tuple[int, ...]]
    pin_domains: Mapping[str, tuple[int, ...]]
    prune_counts: Mapping[str, int]
    reduction_count: int
    unsat_reason: str = ""


def build_standard_cell_problem(
    graph: TopologyGraph,
    constraints: LayoutConstraintSet | None = None,
    *,
    max_columns: int | None = None,
) -> StandardCellProblem:
    active = constraints or getattr(graph, "layout_constraints", None) or LayoutConstraintSet()
    std = active.standard_cell
    if std is None:
        raise ValueError("standard-cell constraints are required")
    rows = tuple(std.rows) or tuple(dict.fromkeys(item.row for item in std.device_constraints))
    if not rows:
        raise ValueError("standard-cell constraints must define rows")
    resolved_max_columns = int(max_columns or std.max_columns or 0)
    if resolved_max_columns <= 0:
        domains = [item.allowed_columns for item in std.device_constraints if item.allowed_columns]
        resolved_max_columns = 1 + max((max(domain) for domain in domains), default=-1)
    if resolved_max_columns <= 0:
        raise ValueError("standard-cell constraints must define max_columns or device domains")
    columns = tuple(range(resolved_max_columns))
    device_constraints = tuple(std.device_constraints)
    if not device_constraints:
        raise ValueError("standard-cell constraints must define device constraints")
    device_order = tuple(
        item.device
        for item in sorted(
            device_constraints,
            key=lambda item: (
                len(_device_domain(item, columns)),
                len(item.order_before) + len(item.adjacent_to),
                item.device,
            ),
        )
    )
    net_constraints = tuple(
        sorted(
            std.net_constraints,
            key=lambda item: (
                len(_track_domain(item, columns)),
                item.pin_order_index if item.pin_order_index is not None else 10_000,
                item.net,
            ),
        )
    )
    return StandardCellProblem(
        graph_name=graph.name,
        rows=rows,
        columns=columns,
        device_order=device_order,
        device_constraints=device_constraints,
        net_constraints=net_constraints,
        device_clusters=tuple(std.device_clusters),
        pin_groups=tuple(std.pin_groups),
        internal_net_clusters=tuple(std.internal_net_clusters),
        metadata={
            "rail_nets": tuple(std.rail_nets),
            "compact_style": std.compact_style,
            "device_terminal_nets": {
                str(device.name): {
                    str(terminal.terminal): str(net.name)
                    for net in graph.nets.values()
                    for terminal in net.terminals
                    if terminal.device == device.name
                }
                for device in graph.devices.values()
            },
        },
    )


def solve_standard_cell(
    problem: StandardCellProblem,
    *,
    max_solutions: int = 1,
    backend: str = "auto",
) -> StandardCellSolveResult:
    if max_solutions <= 0:
        raise ValueError("max_solutions must be positive")
    selected_backend = _select_backend(backend)
    domains = _prune_domains(problem)
    base_stats = {
        "backend": selected_backend,
        "pruned_states": sum(int(count) for count in domains.prune_counts.values()),
        "prune_reasons": tuple(sorted((str(name), int(count)) for name, count in domains.prune_counts.items() if int(count) > 0)),
        "domain_reduction_count": domains.reduction_count,
        "reduced_device_domains": tuple(sorted((name, tuple(values)) for name, values in domains.device_domains.items())),
        "reduced_track_domains": tuple(sorted((name, tuple(values)) for name, values in domains.track_domains.items())),
        "reduced_pin_domains": tuple(sorted((name, tuple(values)) for name, values in domains.pin_domains.items())),
    }
    if domains.unsat_reason:
        stats = StandardCellSolveStats(feasible_solutions=0, **base_stats)
        return StandardCellSolveResult(problem=problem, solutions=(), stats=stats)
    if selected_backend == "z3":
        return _solve_standard_cell_z3(problem, domains, max_solutions=max_solutions, base_stats=base_stats)
    return _solve_standard_cell_dfs(problem, domains, max_solutions=max_solutions, base_stats=base_stats)


def _select_backend(backend: str) -> str:
    normalized = str(backend or "auto").strip().lower()
    if normalized not in {"auto", "z3", "dfs"}:
        raise ValueError(f"unsupported standard-cell solver backend: {backend!r}")
    if normalized == "auto":
        return "z3" if z3 is not None else "dfs"
    if normalized == "z3" and z3 is None:
        raise RuntimeError("z3 backend requested but z3-solver is not installed")
    return normalized


def synthesize_standard_cell_route_result(
    graph: TopologyGraph,
    pcell_plan: Any,
    solution: StandardCellSolution,
    pdk: Any,
    *,
    lib: str = "work",
    cell: str = "standard_cell",
    view: str = "layout",
    config: StandardCellRouteConfig | None = None,
    calibration_cache: Any | None = None,
) -> StandardCellRouteResult:
    import time
    from analogskills.eda.oa import OaCellView, OaPath, OaPin, OaRect, OaVia, OaWritePlan, snap_oa_write_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity, bbox_overlaps, path_segment_bboxes, via_landing_bboxes
    from analogskills.pcell import PCellTerminalAccessor, PCellTerminalRequiresTap

    cfg = config or StandardCellRouteConfig()
    route_debug = str(get_env("STD_ROUTE_DEBUG", "") or "").strip().lower() in {"1", "true", "yes", "on"}
    route_debug_t0 = time.monotonic()

    def _route_debug(stage: str, **payload: object) -> None:
        if not route_debug:
            return
        elapsed = time.monotonic() - route_debug_t0
        details = " ".join(f"{key}={value}" for key, value in payload.items())
        print(f"[std-route] t={elapsed:.2f}s stage={stage} {details}".rstrip())

    accessor = PCellTerminalAccessor(pdk, calibration_cache=calibration_cache)
    route_layers = tuple(cfg.route_layers) or tuple(getattr(pdk, "preferred_signal_layers", ()) or getattr(pdk.layer_map, "metals", ()))
    if not route_layers:
        raise RuntimeError("standard-cell route synthesis requires at least one routing layer")
    std = getattr(graph.layout_constraints, "standard_cell", None)
    net_constraints = {item.net: item for item in getattr(std, "net_constraints", ())} if std is not None else {}
    rail_nets = set(getattr(std, "rail_nets", ())) if std is not None else set()
    track_by_net = solution.net_track_map()
    pin_columns_raw = solution.metadata.get("pin_columns", ())
    if isinstance(pin_columns_raw, Mapping):
        pin_column_by_net = {str(name): int(value) for name, value in pin_columns_raw.items()}
    else:
        pin_column_by_net = {str(name): int(value) for name, value in tuple(pin_columns_raw or ())}

    top_level_nets = {str(net) for net in getattr(graph, "pins", {})}

    def layer_min_width(layer: str) -> float:
        try:
            return pdk.rules.snap_dimension_um(max(float(cfg.min_route_width_um), float(pdk.rules.min_width_um(layer))))
        except Exception:
            return pdk.rules.snap_dimension_um(float(cfg.min_route_width_um))

    def layer_min_spacing(layer: str) -> float:
        try:
            return pdk.rules.snap_dimension_um(float(pdk.rules.min_spacing_um(layer)))
        except Exception:
            return layer_min_width(layer)

    def layer_min_area_um2(layer: str) -> float:
        try:
            area_nm2 = float(getattr(pdk.rules, "min_area_nm2", {}).get(layer, 0) or 0)
        except Exception:
            area_nm2 = 0.0
        return area_nm2 * 1e-6 if area_nm2 > 0.0 else 0.0

    def layer_legal_square_size(layer: str) -> float:
        width = layer_min_width(layer)
        spacing = layer_min_spacing(layer)
        area = layer_min_area_um2(layer)
        area_size = area ** 0.5 if area > 0.0 else 0.0
        local_layers = {str(pdk.layer_map.contact), str(pdk.layer_map.gate), "M0", "MD", "OD"}
        heuristic = max(width, spacing) if layer in local_layers else width + spacing
        return pdk.rules.snap_dimension_um(max(width, spacing, area_size, heuristic))

    def layer_route_width(layer: str) -> float:
        metals = tuple(str(name) for name in getattr(pdk.layer_map, "metals", ()))
        if layer in metals:
            metal_idx = metals.index(layer)
            if metal_idx >= 4:
                return pdk.rules.snap_dimension_um(max(layer_min_width(layer), 0.08))
            if metal_idx >= 2:
                return pdk.rules.snap_dimension_um(max(layer_min_width(layer), 0.06))
        return layer_min_width(layer)

    def grow_bbox_to_min_size(layer: str, bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = bbox
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        size = layer_legal_square_size(layer)
        width = max(abs(x1 - x0), size)
        height = max(abs(y1 - y0), size)
        grown = (cx - width / 2.0, cy - height / 2.0, cx + width / 2.0, cy + height / 2.0)
        return pdk.rules.snap_bbox_um(grown, mode="outward")

    def grow_bbox_to_target_size(layer: str, bbox: tuple[float, float, float, float], target_size: float) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = bbox
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        size = pdk.rules.snap_dimension_um(max(target_size, layer_min_width(layer)))
        width = max(abs(x1 - x0), size)
        height = max(abs(y1 - y0), size)
        grown = (cx - width / 2.0, cy - height / 2.0, cx + width / 2.0, cy + height / 2.0)
        return pdk.rules.snap_bbox_um(grown, mode="outward")

    coarse_pitch = max(layer_min_width(layer) for layer in route_layers)

    def via_landing_rects(via_def: str, xy: tuple[float, float], net: str) -> tuple[Any, ...]:
        dummy = OaVia(via_def, pdk.rules.snap_point_um(xy), net)
        rects_out: list[Any] = []
        base_access_vias = {"VIA0", str(pdk.layer_map.contact), "M0_PO", "M0_PO_VD", "M0_NW", "M0_SUB"}
        for layer, bbox in via_landing_bboxes(dummy, pdk):
            if via_def in base_access_vias:
                target = max(layer_min_width(layer), layer_min_spacing(layer))
                shaped_bbox = grow_bbox_to_target_size(layer, bbox, target)
            else:
                shaped_bbox = grow_bbox_to_min_size(layer, bbox)
            rects_out.append(OaRect(layer, "drawing", shaped_bbox, net))
        return tuple(rects_out)

    def pin_y_candidates_for(net: str) -> tuple[float, ...]:
        explicit = cfg.pin_y_candidates_um.get(net)
        if explicit:
            return tuple(
                dict.fromkeys(
                    pdk.rules.snap_point_um((0.0, float(value)))[1]
                    for value in explicit
                )
            )
        y = float(cfg.pin_y_overrides.get(net, cfg.pin_origin_um[1]))
        return (pdk.rules.snap_point_um((0.0, y))[1],)

    def pin_center_for(net: str, pin_column: int | None = None, pin_y: float | None = None) -> tuple[float, float]:
        resolved_pin_column = int(pin_column if pin_column is not None else pin_column_by_net.get(net, 0))
        net_positions = cfg.pin_column_positions.get(net, {})
        if resolved_pin_column in net_positions:
            x = float(net_positions[resolved_pin_column])
        else:
            x = float(cfg.pin_origin_um[0] + resolved_pin_column * cfg.pin_pitch_um)
        y = float(pin_y if pin_y is not None else cfg.pin_y_overrides.get(net, cfg.pin_origin_um[1]))
        return pdk.rules.snap_point_um((x, y))

    def pin_column_candidates(net: str) -> tuple[int, ...]:
        selected = int(pin_column_by_net.get(net, 0))
        positions = cfg.pin_column_positions.get(net, {})
        if side_for(net) in {"top", "right", "rail"} and positions:
            return tuple(sorted(int(column) for column in positions))
        constraint = net_constraints.get(net)
        if constraint is not None and constraint.allowed_pin_columns:
            allowed = tuple(sorted(dict.fromkeys(int(item) for item in constraint.allowed_pin_columns)))
            if allowed:
                return allowed
        if positions:
            return tuple(sorted(int(column) for column in positions))
        return (selected,)

    def top_pin_xy_candidates(net: str) -> tuple[tuple[int, tuple[float, float]], ...]:
        if net not in top_level_nets:
            return ()
        side = side_for(net)
        columns = pin_column_candidates(net) if side in {"top", "right", "rail"} else (int(pin_column_by_net.get(net, 0)),)
        ys = pin_y_candidates_for(net)
        candidates: list[tuple[int, tuple[float, float]]] = []
        seen_xy: set[tuple[float, float]] = set()
        for column in columns:
            for y in ys:
                xy = pin_center_for(net, column, y)
                if xy in seen_xy:
                    continue
                seen_xy.add(xy)
                candidates.append((column, xy))
        return tuple(candidates)

    def side_for(net: str) -> str:
        constraint = net_constraints.get(net)
        if net in rail_nets:
            return "rail"
        if constraint is None:
            return "internal"
        return str(constraint.pin_side or "internal")

    def ordered_layer_domain(net: str) -> tuple[str, ...]:
        if net in cfg.net_route_layer_overrides:
            return (str(cfg.net_route_layer_overrides[net]),)
        side = side_for(net)
        ordered = tuple(str(layer) for layer in route_layers)
        if side == "rail":
            low = str(cfg.rail_route_layer or ordered[0])
            rest = tuple(layer for layer in ordered if layer != low)
            return (low, *rest)
        if side == "top":
            high = str(cfg.gate_route_layer or ordered[-1])
            rest = tuple(layer for layer in reversed(ordered) if layer != high)
            return (high, *rest)
        if side == "right":
            high = str(cfg.output_route_layer or ordered[-1])
            rest = tuple(layer for layer in reversed(ordered) if layer != high)
            return (high, *rest)
        low = str(cfg.signal_route_layer or ordered[0])
        rest = tuple(layer for layer in ordered if layer != low)
        return (low, *rest)

    def pin_layer_for(net: str, route_layer: str) -> str:
        if net in cfg.pin_layer_overrides:
            return str(cfg.pin_layer_overrides[net])
        side = side_for(net)
        if side == "top":
            return str(cfg.gate_route_layer or route_layers[-1])
        if side == "rail":
            return str(cfg.rail_route_layer or route_layers[0])
        if side == "right":
            return str(cfg.output_route_layer or route_layers[-1])
        return route_layer

    def metal_pad(layer: str, center_xy: tuple[float, float], net: str, *, size: float | None = None) -> Any:
        base = float(size if size is not None else cfg.pin_size_um)
        base = max(base, layer_legal_square_size(layer))
        half = base / 2.0
        x, y = center_xy
        bbox = pdk.rules.snap_bbox_um((x - half, y - half, x + half, y + half), mode="outward")
        return OaRect(layer, "drawing", bbox, net)

    def via_stack(x: float, y: float, bottom_layer: str, top_layer: str, net: str) -> tuple[Any, ...]:
        if bottom_layer == top_layer:
            return ()
        metals = tuple(pdk.layer_map.metals)
        if bottom_layer not in metals or top_layer not in metals:
            return ()
        lower_idx = metals.index(bottom_layer)
        upper_idx = metals.index(top_layer)
        step = 1 if upper_idx > lower_idx else -1
        stack: list[Any] = []
        for idx in range(lower_idx, upper_idx, step):
            lower = metals[min(idx, idx + step)]
            upper = metals[max(idx, idx + step)]
            via_rule = pdk.via_rule_for_layers(lower, upper)
            if via_rule is not None:
                stack.append(OaVia(via_rule.via_def, pdk.rules.snap_point_um((x, y)), net, rows=via_rule.default_rows, cols=via_rule.default_cols))
            else:
                via_name = pdk.layer_map.vias[min(idx, idx + step)]
                stack.append(OaVia(via_name, pdk.rules.snap_point_um((x, y)), net))
        return tuple(stack)

    def stack_landing_rects(x: float, y: float, bottom_layer: str, top_layer: str, net: str) -> tuple[Any, ...]:
        rects_out: list[Any] = []
        for via in via_stack(x, y, bottom_layer, top_layer, net):
            rects_out.extend(via_landing_rects(via.via_def, via.xy, net))
        return tuple(rects_out)

    def route_anchor_from_pin(net: str, pin: Any) -> _RouteAnchor:
        x, y = pdk.rules.snap_point_um(tuple(float(v) for v in pin.xy_um))
        bbox = None
        try:
            pin_bbox = getattr(pin, "bbox_um", None)
            if pin_bbox is not None:
                bbox = pdk.rules.snap_bbox_um(tuple(float(v) for v in pin_bbox), mode="outward")
        except Exception:
            bbox = None
        return _RouteAnchor(
            str(net),
            x,
            y,
            str(pin.layer),
            bbox,
            str(getattr(pin, "contact_layer", "") or ""),
            str(getattr(pin, "source", "") or ""),
            int(getattr(pin, "access_priority", 50) or 50),
            bool(getattr(pin, "lvs_safe", True)),
            str(getattr(pin, "terminal", "") or ""),
            False,
        )

    def effective_anchor_bottom_layer(anchor: _RouteAnchor) -> str:
        metals = tuple(pdk.layer_map.metals)
        metal0 = metals[0] if metals else ""
        if anchor.layer == pdk.layer_map.gate:
            return metal0
        if anchor.layer == "MD":
            return metal0
        if anchor.layer in {"OD", "PDK", "NW"} and anchor.contact_layer:
            return metal0
        return anchor.layer

    def is_shared_gate_escape_net(anchors: tuple[_RouteAnchor, ...]) -> bool:
        return bool(anchors) and all(anchor.terminal_name == "G" for anchor in anchors)

    def shared_gate_escape_x(anchors: tuple[_RouteAnchor, ...]) -> float:
        if not anchors:
            return 0.0
        avg_x = sum(anchor.x for anchor in anchors) / len(anchors)
        return pdk.rules.snap_point_um((avg_x, 0.0))[0]

    def shared_gate_escape_y(anchors: tuple[_RouteAnchor, ...]) -> float:
        if not anchors:
            return 0.0
        return pdk.rules.snap_point_um((0.0, max(anchor.y for anchor in anchors)))[1]

    def shared_gate_escape_x_for_template(
        anchors: tuple[_RouteAnchor, ...],
        top_pin_xy: tuple[float, float] | None,
        local_template: str,
        escape_x_override: float | None = None,
    ) -> float:
        if not anchors:
            return 0.0
        if escape_x_override is not None:
            return pdk.rules.snap_point_um((escape_x_override, 0.0))[0]
        if local_template in {"po_shared_trunk", "top_bridge"}:
            return shared_gate_escape_x(anchors)
        if top_pin_xy is not None:
            return pdk.rules.snap_point_um((top_pin_xy[0], 0.0))[0]
        return shared_gate_escape_x(anchors)

    def gate_local_template_domain(anchors: tuple[_RouteAnchor, ...]) -> tuple[str, ...]:
        if not is_shared_gate_escape_net(anchors):
            return ("direct_stack",)
        requested = tuple(str(name) for name in (cfg.gate_local_templates or ("po_shared_trunk", "top_bridge", "m0_shared_collector", "direct_stack")))
        contact_layers = {anchor.contact_layer for anchor in anchors if anchor.contact_layer}
        contact_layer = next(iter(contact_layers), "")
        available: list[str] = []
        for name in requested:
            if name == "po_shared_trunk":
                if all(anchor.layer == pdk.layer_map.gate for anchor in anchors) and contact_layer:
                    available.append(name)
            elif name == "top_bridge":
                if all(anchor.layer == pdk.layer_map.gate for anchor in anchors) and contact_layer:
                    available.append(name)
            elif name == "m0_shared_collector":
                if all(effective_anchor_bottom_layer(anchor) == pdk.layer_map.metals[0] for anchor in anchors):
                    available.append(name)
            elif name == "direct_stack":
                available.append(name)
        return tuple(dict.fromkeys(available)) or ("direct_stack",)

    def net_local_template_domain(net: str, anchors: tuple[_RouteAnchor, ...]) -> tuple[str, ...]:
        if is_shared_gate_escape_net(anchors):
            return gate_local_template_domain(anchors)
        side = side_for(net)
        if side == "right":
            return ("direct_stack", "right_bridge")
        if side == "rail":
            return ("direct_stack", "rail_bridge")
        if side == "internal" and len(anchors) >= 2:
            return ("direct_stack", "internal_bridge")
        return ("direct_stack",)

    def bridge_escape_x(net: str, anchors: tuple[_RouteAnchor, ...]) -> float:
        if not anchors:
            return 0.0
        side = side_for(net)
        if side == "right":
            return pdk.rules.snap_point_um((max(anchor.x for anchor in anchors), 0.0))[0]
        if side == "rail":
            return pdk.rules.snap_point_um((min(anchor.x for anchor in anchors), 0.0))[0]
        return pdk.rules.snap_point_um((anchors[0].x, 0.0))[0]

    def bridge_x_candidates(
        net: str,
        anchors: tuple[_RouteAnchor, ...],
        top_pin_xy: tuple[float, float] | None,
    ) -> tuple[float | None, ...]:
        if is_shared_gate_escape_net(anchors):
            values = [shared_gate_escape_x(anchors)]
            values.extend(anchor.x for anchor in anchors)
            if top_pin_xy is not None:
                values.append(float(top_pin_xy[0]))
            snapped = [
                pdk.rules.snap_point_um((float(value), 0.0))[0]
                for value in values
            ]
            dedup: list[float] = []
            for value in snapped:
                if value in dedup:
                    continue
                dedup.append(value)
            return tuple(dedup) if dedup else (None,)
        side = side_for(net)
        if side not in {"right", "rail", "internal"} or not anchors:
            return (None,)
        base = bridge_escape_x(net, anchors)
        step = float(cfg.track_pitch_um)
        values = [base]
        if side == "right":
            values.append(base + step)
        elif side == "rail":
            values.append(base - step)
        else:
            values.extend((base - step, base + step))
        snapped = [
            pdk.rules.snap_point_um((float(value), 0.0))[0]
            for value in values
        ]
        dedup: list[float] = []
        for value in snapped:
            if value in dedup:
                continue
            dedup.append(value)
        return tuple(dedup) if dedup else (None,)

    def breakout_anchor_candidates(inst: Any, terminal: str, net: str) -> tuple[_RouteAnchor, ...]:
        preferred_layers: Sequence[str]
        if str(terminal) == "G":
            preferred_layers = ("PO",)
        else:
            preferred_layers = tuple(pdk.layer_map.metals[:1]) + ("MD", "OD")
        try:
            pins = accessor.get_terminal_pins(inst, terminal, preferred_layers=preferred_layers)
        except (KeyError, ValueError, PCellTerminalRequiresTap):
            return ()
        anchors: list[_RouteAnchor] = []
        seen: set[tuple[str, float, float, str]] = set()
        for pin in pins:
            x, y = pin.xy_um
            if pin.bbox_um is not None:
                x0, y0, x1, y1 = pin.bbox_um
                cx, cy = pin.xy_um
                margin = max(float(cfg.breakout_margin_um), 0.0)
                if terminal == "S":
                    x = x0 + margin
                    y = cy
                elif terminal == "D":
                    x = x1 - margin
                    y = cy
                else:
                    x = cx
                    y = y1 - margin if terminal == "G" else cy
            snapped_pin = pin
            if (x, y) != pin.xy_um:
                from dataclasses import replace as _dc_replace
                snapped_pin = _dc_replace(pin, xy_um=pdk.rules.snap_point_um((x, y)))
            anchor = route_anchor_from_pin(net, snapped_pin)
            key = (anchor.layer, anchor.x, anchor.y, anchor.source)
            if key in seen:
                continue
            seen.add(key)
            anchors.append(anchor)
        return tuple(anchors)

    terminal_access_domains: dict[str, tuple[tuple[_RouteAnchor, ...], ...]] = {str(net): () for net in graph.nets}
    for inst in getattr(pcell_plan, "instances", ()):
        for terminal, net in sorted(getattr(inst, "connections", {}).items()):
            if not net:
                continue
            anchors = breakout_anchor_candidates(inst, str(terminal), str(net))
            if not anchors:
                continue
            terminal_access_domains[str(net)] = (*terminal_access_domains.get(str(net), ()), anchors)

    terminal_accesses: dict[str, tuple[_RouteAnchor, ...]] = {
        net: tuple(anchor for domain in terminal_access_domains.get(net, ()) for anchor in domain)
        for net in tuple(str(net) for net in graph.nets)
    }
    metal0 = pdk.layer_map.metals[0]

    top_pin_anchors = {
        net: tuple(
            _RouteAnchor(net, xy[0], xy[1], "", None, "", "", 0, True, net, True)
            for _, xy in top_pin_xy_candidates(net)
        )
        for net in top_level_nets
    }

    span_by_net: dict[str, tuple[float, float]] = {}
    for net in graph.nets:
        net_name = str(net)
        xs = [anchor.x for anchor in terminal_accesses.get(net_name, ())]
        if net_name in top_pin_anchors:
            xs.extend(anchor.x for anchor in top_pin_anchors[net_name])
        if not xs:
            xs.append(0.0)
        span_by_net[net_name] = (min(xs), max(xs))

    rough_neighbors: dict[str, set[str]] = {str(net): set() for net in graph.nets}
    for left_idx, left in enumerate(tuple(str(net) for net in graph.nets)):
        left_span = span_by_net[left]
        for right in tuple(str(net) for net in graph.nets)[left_idx + 1 :]:
            right_span = span_by_net[right]
            if left_span[0] <= right_span[1] + coarse_pitch and right_span[0] <= left_span[1] + coarse_pitch:
                rough_neighbors[left].add(right)
                rough_neighbors[right].add(left)

    def candidate_trunk_ys(net: str) -> tuple[float, ...]:
        if net in cfg.trunk_y_overrides:
            return (pdk.rules.snap_point_um((0.0, float(cfg.trunk_y_overrides[net])))[1],)
        side = side_for(net)
        degree = len(rough_neighbors.get(net, ()))
        preferred_track = int(track_by_net.get(net, 0))
        if side == "rail":
            base = float(cfg.pin_y_overrides.get(net, cfg.internal_track_base_y_um))
            return (pdk.rules.snap_point_um((0.0, base))[1],)
        if side == "top":
            base = max(
                float(cfg.top_track_base_y_um),
                max((anchor.y for anchor in terminal_accesses.get(net, ())), default=float(cfg.top_track_base_y_um)),
            )
            count = min(10, max(5, degree + 4))
            offsets = tuple(preferred_track + idx for idx in range(count))
        elif side == "right":
            base = float(cfg.output_track_base_y_um)
            count = min(10, max(5, degree + 3))
            offsets = tuple(preferred_track + idx for idx in range(count))
        else:
            base = float(cfg.internal_track_base_y_um)
            count = min(5, max(2, degree + 1))
            offsets = tuple(preferred_track + idx for idx in range(count))
        return tuple(
            dict.fromkeys(
                pdk.rules.snap_point_um((0.0, base + offset * cfg.track_pitch_um))[1]
                for offset in offsets
            )
        )

    def option_shapes(
        net: str,
        anchors: tuple[_RouteAnchor, ...],
        route_layer: str,
        trunk_y: float,
        pin_layer: str,
        top_pin_xy: tuple[float, float] | None,
        local_template: str,
        bridge_x: float | None,
    ) -> tuple[_RouteShape, ...]:
        route_width = layer_route_width(route_layer)
        shapes: list[_RouteShape] = []
        metal0 = pdk.layer_map.metals[0]
        shared_gate_escape = is_shared_gate_escape_net(anchors) and local_template != "direct_stack"
        bridge_template = local_template in {"top_bridge", "right_bridge", "rail_bridge"}
        internal_bridge_template = local_template == "internal_bridge"
        escape_x = shared_gate_escape_x_for_template(anchors, top_pin_xy, local_template, bridge_x) if shared_gate_escape else 0.0
        escape_y = shared_gate_escape_y(anchors) if shared_gate_escape else 0.0
        xs = [escape_x] if shared_gate_escape else [anchor.x for anchor in anchors]
        if top_pin_xy is not None and not bridge_template:
            xs.append(top_pin_xy[0])
        if xs and not (shared_gate_escape and local_template == "top_bridge") and not internal_bridge_template:
            x0 = pdk.rules.snap_point_um((min(xs), trunk_y))[0]
            x1 = pdk.rules.snap_point_um((max(xs), trunk_y))[0]
            if abs(x1 - x0) >= route_width - 1e-12:
                shapes.extend(tuple(_RouteShape(route_layer, bbox) for bbox in path_segment_bboxes(((x0, trunk_y), (x1, trunk_y)), route_width)))
        for anchor in anchors:
            if anchor.bbox is not None:
                shapes.append(_RouteShape(anchor.layer, anchor.bbox))
            if anchor.contact_layer and local_template != "po_shared_trunk":
                for rect in via_landing_rects(anchor.contact_layer, (anchor.x, anchor.y), net):
                    shapes.append(_RouteShape(rect.layer, rect.bbox))
            if shared_gate_escape:
                if local_template == "po_shared_trunk":
                    po_width = layer_route_width(pdk.layer_map.gate)
                    if abs(anchor.x - escape_x) >= po_width - 1e-12:
                        shapes.extend(tuple(_RouteShape(pdk.layer_map.gate, bbox) for bbox in path_segment_bboxes(((anchor.x, anchor.y), (escape_x, anchor.y)), po_width)))
                else:
                    m0_width = layer_route_width(metal0)
                    if abs(anchor.x - escape_x) >= m0_width - 1e-12:
                        shapes.extend(tuple(_RouteShape(metal0, bbox) for bbox in path_segment_bboxes(((anchor.x, anchor.y), (escape_x, anchor.y)), m0_width)))
            elif internal_bridge_template and bridge_x is not None:
                if abs(anchor.x - bridge_x) >= route_width - 1e-12:
                    shapes.extend(tuple(_RouteShape(route_layer, bbox) for bbox in path_segment_bboxes(((anchor.x, anchor.y), (bridge_x, anchor.y)), route_width)))
            else:
                if abs(anchor.y - trunk_y) >= route_width - 1e-12:
                    shapes.extend(tuple(_RouteShape(route_layer, bbox) for bbox in path_segment_bboxes(((anchor.x, anchor.y), (anchor.x, trunk_y)), route_width)))
                bottom_layer = effective_anchor_bottom_layer(anchor)
                if bottom_layer != route_layer:
                    for rect in stack_landing_rects(anchor.x, anchor.y, bottom_layer, route_layer, net):
                        shapes.append(_RouteShape(rect.layer, rect.bbox))
        if shared_gate_escape:
            y0 = min(anchor.y for anchor in anchors)
            y1 = max(anchor.y for anchor in anchors)
            if local_template == "po_shared_trunk":
                po_width = layer_route_width(pdk.layer_map.gate)
                if abs(y1 - y0) >= po_width - 1e-12:
                    shapes.extend(tuple(_RouteShape(pdk.layer_map.gate, bbox) for bbox in path_segment_bboxes(((escape_x, y0), (escape_x, y1)), po_width)))
                if anchors and anchors[0].contact_layer:
                    for rect in via_landing_rects(anchors[0].contact_layer, (escape_x, escape_y), net):
                        shapes.append(_RouteShape(rect.layer, rect.bbox))
                if metal0 != route_layer:
                    for rect in stack_landing_rects(escape_x, escape_y, metal0, route_layer, net):
                        shapes.append(_RouteShape(rect.layer, rect.bbox))
            else:
                m0_width = layer_route_width(metal0)
                if abs(y1 - y0) >= m0_width - 1e-12:
                    shapes.extend(tuple(_RouteShape(metal0, bbox) for bbox in path_segment_bboxes(((escape_x, y0), (escape_x, y1)), m0_width)))
                if metal0 != route_layer:
                    for rect in stack_landing_rects(escape_x, escape_y, metal0, route_layer, net):
                        shapes.append(_RouteShape(rect.layer, rect.bbox))
            if local_template != "top_bridge" and abs(escape_y - trunk_y) >= route_width - 1e-12:
                shapes.extend(tuple(_RouteShape(route_layer, bbox) for bbox in path_segment_bboxes(((escape_x, escape_y), (escape_x, trunk_y)), route_width)))
        if internal_bridge_template and bridge_x is not None and anchors:
            y0 = min(anchor.y for anchor in anchors)
            y1 = max(anchor.y for anchor in anchors)
            if abs(y1 - y0) >= route_width - 1e-12:
                shapes.extend(tuple(_RouteShape(route_layer, bbox) for bbox in path_segment_bboxes(((bridge_x, y0), (bridge_x, y1)), route_width)))
        if top_pin_xy is not None:
            px, py = top_pin_xy
            pin_rect = metal_pad(pin_layer, (px, py), net, size=cfg.pin_size_um)
            shapes.append(_RouteShape(pin_layer, pin_rect.bbox))
            if bridge_template and anchors:
                bx = escape_x if shared_gate_escape and local_template == "top_bridge" else (bridge_x if bridge_x is not None else bridge_escape_x(net, anchors))
                if local_template == "top_bridge":
                    if abs(py - escape_y) >= route_width - 1e-12:
                        shapes.extend(tuple(_RouteShape(route_layer, bbox) for bbox in path_segment_bboxes(((bx, escape_y), (bx, py)), route_width)))
                elif abs(py - trunk_y) >= route_width - 1e-12:
                    shapes.extend(tuple(_RouteShape(route_layer, bbox) for bbox in path_segment_bboxes(((bx, py), (bx, trunk_y)), route_width)))
                if abs(px - bx) >= route_width - 1e-12:
                    shapes.extend(tuple(_RouteShape(route_layer, bbox) for bbox in path_segment_bboxes(((bx, py), (px, py)), route_width)))
            elif abs(py - trunk_y) >= route_width - 1e-12:
                shapes.extend(tuple(_RouteShape(route_layer, bbox) for bbox in path_segment_bboxes(((px, py), (px, trunk_y)), route_width)))
            if pin_layer != route_layer:
                for rect in stack_landing_rects(px, py, pin_layer, route_layer, net):
                    shapes.append(_RouteShape(rect.layer, rect.bbox))
        return tuple(shapes)

    def trim_option_domain(net: str, options: list[_RouteOption]) -> tuple[_RouteOption, ...]:
        side = side_for(net)
        grouped: dict[str, list[_RouteOption]] = {}
        for option in options:
            grouped.setdefault(option.local_template, []).append(option)
        trimmed: list[_RouteOption] = []
        for template in sorted(grouped):
            template_options = grouped[template]
            if side in {"top", "right", "rail"}:
                buckets: dict[tuple[int | None, str, float | None], list[_RouteOption]] = {}
                for option in template_options:
                    key = (
                        option.top_pin_column,
                        option.route_layer,
                        None if option.top_pin_xy is None else float(option.top_pin_xy[1]),
                        option.bridge_x,
                    )
                    buckets.setdefault(key, []).append(option)
                kept: list[_RouteOption] = []
                for key in sorted(
                    buckets,
                    key=lambda item: (
                        item[0] if item[0] is not None else -1,
                        item[1],
                        item[2] if item[2] is not None else -1.0,
                        item[3] if item[3] is not None else -1.0,
                    ),
                ):
                    bucket_kept = sorted(
                        buckets[key],
                        key=lambda option: (
                            option.cost,
                            option.trunk_y,
                            len(option.anchors),
                        ),
                    )[:(8 if side == "top" else 12 if side == "right" else 8)]
                    kept.extend(bucket_kept)
                kept = sorted(
                    kept,
                    key=lambda option: (
                        option.cost,
                        option.route_layer,
                        option.trunk_y,
                        option.top_pin_column if option.top_pin_column is not None else -1,
                        len(option.anchors),
                    ),
                )[:(96 if side == "top" else 256 if side == "right" else 96)]
            else:
                kept = sorted(
                    template_options,
                    key=lambda option: (
                        option.cost,
                        option.route_layer,
                        option.trunk_y,
                        option.top_pin_column if option.top_pin_column is not None else -1,
                        len(option.anchors),
                    ),
                )[:64]
            trimmed.extend(kept)
        return tuple(trimmed)

    raw_option_domains: dict[str, tuple[_RouteOption, ...]] = {}
    option_domains: dict[str, tuple[_RouteOption, ...]] = {}
    for net in tuple(str(net) for net in graph.nets):
        preferred_layers = ordered_layer_domain(net)
        preferred_ys = candidate_trunk_ys(net)
        pin_candidates = top_pin_xy_candidates(net) if net in top_level_nets else ((None, None),)
        options: list[_RouteOption] = []
        access_domains = terminal_access_domains.get(net, ())
        anchor_combos = tuple(product(*access_domains)) if access_domains else ((),)
        selected_pin_column = int(pin_column_by_net.get(net, 0))
        selected_top_pin_candidate = (
            (selected_pin_column, pin_center_for(net, selected_pin_column))
            if net in top_level_nets
            else (None, None)
        )
        for anchor_rank, anchors in enumerate(anchor_combos):
            anchor_tuple = tuple(anchors)
            anchor_cost = sum(anchor.access_priority for anchor in anchor_tuple)
            unsafe_cost = sum(0 if anchor.lvs_safe else 1000 for anchor in anchor_tuple)
            x_span_cost = 0
            if anchor_tuple:
                x_span_cost = int(round((max(anchor.x for anchor in anchor_tuple) - min(anchor.x for anchor in anchor_tuple)) * 1000.0))
            template_domain = net_local_template_domain(net, anchor_tuple)
            for template_rank, local_template in enumerate(template_domain):
                template_penalty = {
                    "po_shared_trunk": 0,
                    "top_bridge": 5,
                    "right_bridge": 10,
                    "rail_bridge": 10,
                    "internal_bridge": 10,
                    "direct_stack": 50,
                    "m0_shared_collector": 200,
                }.get(local_template, 100)
                net_side = side_for(net)
                if net_side in {"top", "right", "rail"}:
                    candidate_domain = pin_candidates
                else:
                    candidate_domain = (selected_top_pin_candidate,)
                for layer_rank, layer in enumerate(preferred_layers):
                    for pin_column, top_pin_xy in candidate_domain:
                        if local_template == "top_bridge" and top_pin_xy is not None:
                            trunk_domain = (top_pin_xy[1],)
                        elif local_template == "internal_bridge":
                            trunk_domain = (preferred_ys[0],)
                        else:
                            trunk_domain = preferred_ys
                        for y_rank, trunk_y in enumerate(trunk_domain):
                            pin_layer = layer if local_template in {"po_shared_trunk", "top_bridge", "right_bridge", "rail_bridge"} and side_for(net) in {"top", "right", "rail"} else pin_layer_for(net, layer)
                            if local_template == "po_shared_trunk":
                                via_cost = 1 if layer != metal0 else 0
                            elif local_template == "m0_shared_collector":
                                via_cost = 1 if layer != metal0 else 0
                            else:
                                via_cost = sum(1 for anchor in anchor_tuple if effective_anchor_bottom_layer(anchor) != layer)
                            if top_pin_xy is not None and pin_layer != layer:
                                via_cost += 1
                            pin_shift_cost = 0 if pin_column is None else abs(int(pin_column) - selected_pin_column) * 25
                            bridge_domain = (
                                bridge_x_candidates(net, anchor_tuple, top_pin_xy)
                                if local_template in {"po_shared_trunk", "top_bridge", "right_bridge", "rail_bridge", "internal_bridge"}
                                else (None,)
                            )
                            for bridge_rank, bridge_x in enumerate(bridge_domain):
                                bridge_cost = 0
                                if bridge_x is not None and top_pin_xy is not None:
                                    bridge_cost = int(round(abs(float(top_pin_xy[0]) - float(bridge_x)) * 100.0))
                                cost = (
                                    anchor_rank * 5
                                    + anchor_cost
                                    + unsafe_cost
                                    + x_span_cost
                                    + template_rank * 25
                                    + template_penalty
                                    + layer_rank * 100
                                    + y_rank * 10
                                    + via_cost
                                    + pin_shift_cost
                                    + bridge_rank * 5
                                    + bridge_cost
                                )
                                shapes = option_shapes(net, anchor_tuple, layer, trunk_y, pin_layer, top_pin_xy, local_template, bridge_x)
                                options.append(_RouteOption(len(options), layer, trunk_y, pin_layer, cost, local_template, anchor_tuple, top_pin_xy, pin_column, bridge_x, shapes))
        if not options:
            raise RuntimeError(f"standard-cell detailed route synthesis found no options for net {net}")
        raw_option_domains[net] = tuple(options)
        option_domains[net] = trim_option_domain(net, options)
    _route_debug(
        "domains",
        summary={
            net: {
                "raw": len(raw_option_domains[net]),
                "trimmed": len(option_domains[net]),
            }
            for net in tuple(str(net) for net in graph.nets)
        },
    )

    option_by_index: dict[str, dict[int, _RouteOption]] = {
        net: {option.index: option for option in options}
        for net, options in option_domains.items()
    }
    pair_conflicts: dict[tuple[str, str], dict[int, set[int]]] = {}
    net_names = tuple(str(net) for net in graph.nets)
    for idx, left in enumerate(net_names):
        for right in net_names[idx + 1 :]:
            conflict_map: dict[int, set[int]] = {}
            for left_option in option_domains[left]:
                conflicts: set[int] = set()
                for right_option in option_domains[right]:
                    if any(
                        left_shape.layer == right_shape.layer and bbox_overlaps(left_shape.bbox, right_shape.bbox)
                        for left_shape in left_option.shapes
                        for right_shape in right_option.shapes
                    ):
                        conflicts.add(right_option.index)
                if conflicts:
                    conflict_map[left_option.index] = conflicts
            pair_conflicts[(left, right)] = conflict_map
    _route_debug(
        "pair-conflicts",
        pairs=len(pair_conflicts),
        nonempty=sum(1 for mapping in pair_conflicts.values() if mapping),
    )

    active_domains: dict[str, set[int]] = {net: {option.index for option in options} for net, options in option_domains.items()}
    trimmed_sizes = {net: len(options) for net, options in option_domains.items()}
    relaxed_nets: set[str] = set()

    template_pruned: dict[str, set[str]] = {}
    changed = True
    while changed:
        changed = False
        for net in net_names:
            if side_for(net) != "top":
                continue
            neighbor_nets = tuple(
                other
                for other in rough_neighbors.get(net, ())
                if side_for(other) in {"right", "rail"}
            )
            if not neighbor_nets:
                continue
            template_to_indices: dict[str, set[int]] = {}
            for option_idx in active_domains[net]:
                option = option_by_index[net].get(option_idx)
                if option is None:
                    continue
                template_to_indices.setdefault(option.local_template, set()).add(option_idx)
            if len(template_to_indices) <= 1:
                continue
            for template, indices in tuple(template_to_indices.items()):
                template_supported = True
                for other in neighbor_nets:
                    conflict_map = pair_conflicts.get((net, other), {}) if net < other else {}
                    reverse_conflict_map = pair_conflicts.get((other, net), {}) if net > other else {}
                    has_support = False
                    for option_idx in indices:
                        if net < other:
                            blocked = conflict_map.get(option_idx, set())
                            if active_domains[other] - blocked:
                                has_support = True
                                break
                        else:
                            blocked = {
                                other_idx
                                for other_idx, right_conflicts in reverse_conflict_map.items()
                                if option_idx in right_conflicts
                            }
                            if active_domains[other] - blocked:
                                has_support = True
                                break
                    if not has_support:
                        template_supported = False
                        break
                if template_supported:
                    continue
                removable = set(indices)
                if removable and len(active_domains[net] - removable) > 0:
                    active_domains[net] -= removable
                    template_pruned.setdefault(net, set()).add(template)
                    changed = True

    option_summary_by_net: dict[str, dict[str, object]] = {}
    for net in net_names:
        all_options = raw_option_domains[net]
        active = active_domains[net]
        total_by_template: dict[str, int] = {}
        active_by_template: dict[str, int] = {}
        blocked_by_neighbor: dict[tuple[str, str], int] = {}
        for option in all_options:
            total_by_template[option.local_template] = total_by_template.get(option.local_template, 0) + 1
            if option.index in active:
                active_by_template[option.local_template] = active_by_template.get(option.local_template, 0) + 1
                continue
            for other in net_names:
                if other == net:
                    continue
                if net < other:
                    conflict_map = pair_conflicts.get((net, other), {})
                    if option.index not in option_by_index.get(net, {}):
                        blocked = active_domains[other]
                    else:
                        blocked = active_domains[other] - conflict_map.get(option.index, set())
                else:
                    reverse_conflict_map = pair_conflicts.get((other, net), {})
                    if option.index not in option_by_index.get(net, {}):
                        blocked = active_domains[other]
                    else:
                        blocked_indices = {
                            left_idx
                            for left_idx, right_conflicts in reverse_conflict_map.items()
                            if option.index in right_conflicts
                        }
                        blocked = active_domains[other] - blocked_indices
                if not blocked:
                    key = (option.local_template, other)
                    blocked_by_neighbor[key] = blocked_by_neighbor.get(key, 0) + 1
        option_summary_by_net[net] = {
            "total_options": len(all_options),
            "trimmed_options": trimmed_sizes.get(net, len(all_options)),
            "active_options": len(active),
            "total_by_template": tuple(sorted(total_by_template.items())),
            "active_by_template": tuple(sorted(active_by_template.items())),
            "blocked_by_neighbor": tuple(sorted(((template, neighbor, count) for (template, neighbor), count in blocked_by_neighbor.items()), key=lambda item: (item[0], item[1], item[2]))),
            "relaxed": net in relaxed_nets,
            "template_pruned": tuple(sorted(template_pruned.get(net, ()))),
        }

    def template_pair_support(left: str, right: str) -> tuple[tuple[str, str, int], ...]:
        if left == right:
            return ()
        left_options = tuple(option_by_index.get(left, {}).get(idx) for idx in sorted(active_domains[left]))
        right_options = tuple(option_by_index.get(right, {}).get(idx) for idx in sorted(active_domains[right]))
        left_options = tuple(option for option in left_options if option is not None)
        right_options = tuple(option for option in right_options if option is not None)
        if left < right:
            conflict_map = pair_conflicts.get((left, right), {})
        else:
            conflict_map = {}
            reverse = pair_conflicts.get((right, left), {})
            for right_idx, left_conflicts in reverse.items():
                for left_idx in left_conflicts:
                    conflict_map.setdefault(left_idx, set()).add(right_idx)
        counts: dict[tuple[str, str], int] = {}
        for left_option in left_options:
            blocked = conflict_map.get(left_option.index, set())
            for right_option in right_options:
                if right_option.index in blocked:
                    continue
                key = (left_option.local_template, right_option.local_template)
                counts[key] = counts.get(key, 0) + 1
        return tuple(
            sorted(
                ((left_template, right_template, count) for (left_template, right_template), count in counts.items()),
                key=lambda item: (item[0], item[1], item[2]),
            )
        )

    selected_options: dict[str, _RouteOption] = {}
    if z3 is not None:
        solver = z3.Optimize()
        vars_by_net = {net: z3.Int(f"route_option_{net}") for net in net_names}
        for net in net_names:
            solver.add(z3.Or([vars_by_net[net] == option_idx for option_idx in sorted(active_domains[net])]))
        for left_idx, left in enumerate(net_names):
            for right in net_names[left_idx + 1 :]:
                conflict_map = pair_conflicts.get((left, right), {})
                for left_option_idx in sorted(active_domains[left]):
                    allowed = sorted(active_domains[right] - conflict_map.get(left_option_idx, set()))
                    if not allowed:
                        solver.add(vars_by_net[left] != left_option_idx)
                        continue
                    solver.add(
                        z3.Implies(
                            vars_by_net[left] == left_option_idx,
                            z3.Or([vars_by_net[right] == option_idx for option_idx in allowed]),
                        )
                    )
        cost_terms = []
        for net in net_names:
            var = vars_by_net[net]
            for option in option_domains[net]:
                if option.index not in active_domains[net]:
                    continue
                cost_terms.append(z3.If(var == option.index, option.cost, 0))
        total_cost = z3.Sum(cost_terms) if cost_terms else z3.IntVal(0)
        solver.minimize(total_cost)
        _route_debug(
            "z3-start",
            domains={net: len(active_domains[net]) for net in net_names},
        )
        status = solver.check()
        best_model = solver.model() if status == z3.sat else None
        best_cost: int | None = None
        if best_model is not None:
            best_cost = best_model.eval(total_cost, model_completion=True).as_long()
        _route_debug("z3-done", sat=best_model is not None, best_cost=best_cost)
        if best_model is None or best_cost is None:
            active_summary = {
                net: {
                    "total": option_summary_by_net.get(net, {}).get("total_options", 0),
                    "trimmed": option_summary_by_net.get(net, {}).get("trimmed_options", 0),
                    "active": option_summary_by_net.get(net, {}).get("active_options", 0),
                    "total_by_template": option_summary_by_net.get(net, {}).get("total_by_template", ()),
                    "active_by_template": option_summary_by_net.get(net, {}).get("active_by_template", ()),
                    "blocked_by_neighbor": option_summary_by_net.get(net, {}).get("blocked_by_neighbor", ()),
                    "template_pruned": option_summary_by_net.get(net, {}).get("template_pruned", ()),
                }
                for net in net_names
            }
            pair_summary = {
                f"{left}-{right}": template_pair_support(left, right)
                for left, right in (("A", "Z"), ("A", "VDD"), ("B", "Z"), ("B", "VDD"), ("A", "B"))
                if left in active_domains and right in active_domains
            }
            raise RuntimeError(
                "standard-cell detailed route synthesis could not find a conflict-free option assignment: "
                f"{active_summary}; pair_support={pair_summary}"
            )
        for net in net_names:
            selected_idx = best_model.eval(vars_by_net[net], model_completion=True).as_long()
            selected_options[net] = option_by_index[net][selected_idx]
    else:
        for net in net_names:
            selected_options[net] = min(
                (option_by_index[net][idx] for idx in active_domains[net]),
                key=lambda option: (option.cost, option.route_layer, option.trunk_y),
            )

    layer_by_net: dict[str, str] = {}
    trunk_y_by_net: dict[str, float] = {}
    local_template_by_net: dict[str, str] = {}
    boundary_pins: list[Any] = []
    rects: list[Any] = []
    paths: list[Any] = []
    vias: list[Any] = []

    def access_landing_rects(anchor: _RouteAnchor, net: str) -> tuple[Any, ...]:
        x, y = anchor.x, anchor.y
        rect_list: list[Any] = []
        contact_layer = anchor.contact_layer or pdk.layer_map.contact
        contact_size = layer_legal_square_size(contact_layer) if contact_layer in getattr(pdk.rules, "min_width_nm", {}) else pdk.rules.snap_dimension_um(float(cfg.contact_cut_size_um))
        contact_half = contact_size / 2.0
        m0_size = layer_legal_square_size(pdk.layer_map.metals[0])
        m0_half = m0_size / 2.0
        if anchor.layer == pdk.layer_map.gate:
            gate_half = float(cfg.gate_landing_size_um) / 2.0
            metal0 = pdk.layer_map.metals[0]
            gate_bbox = pdk.rules.snap_bbox_um((x - gate_half, y - gate_half, x + gate_half, y + gate_half), mode="outward")
            rect_list.append(OaRect(metal0, "drawing", grow_bbox_to_min_size(metal0, gate_bbox), net))
            rect_list.append(OaRect(contact_layer, "drawing", pdk.rules.snap_bbox_um((x - contact_half, y - contact_half, x + contact_half, y + contact_half), mode="outward"), net))
        elif anchor.layer == "MD":
            rect_list.append(OaRect(contact_layer, "drawing", pdk.rules.snap_bbox_um((x - contact_half, y - contact_half, x + contact_half, y + contact_half), mode="outward"), net))
            rect_list.append(OaRect(pdk.layer_map.metals[0], "drawing", pdk.rules.snap_bbox_um((x - m0_half, y - m0_half, x + m0_half, y + m0_half), mode="outward"), net))
        elif anchor.layer in {"OD", "PDK", "NW"} and anchor.contact_layer:
            rect_list.append(OaRect(anchor.contact_layer, "drawing", pdk.rules.snap_bbox_um((x - contact_half, y - contact_half, x + contact_half, y + contact_half), mode="outward"), net))
            rect_list.append(OaRect(pdk.layer_map.metals[0], "drawing", pdk.rules.snap_bbox_um((x - m0_half, y - m0_half, x + m0_half, y + m0_half), mode="outward"), net))
        return tuple(rect_list)

    for net in net_names:
        option = selected_options[net]
        route_layer = option.route_layer
        trunk_y = option.trunk_y
        route_width = layer_route_width(route_layer)
        metal0 = pdk.layer_map.metals[0]
        shared_gate_escape = is_shared_gate_escape_net(option.anchors) and option.local_template != "direct_stack"
        bridge_template = option.local_template in {"top_bridge", "right_bridge", "rail_bridge"}
        escape_x = shared_gate_escape_x_for_template(option.anchors, option.top_pin_xy, option.local_template, option.bridge_x) if shared_gate_escape else 0.0
        escape_y = shared_gate_escape_y(option.anchors) if shared_gate_escape else 0.0
        layer_by_net[net] = route_layer
        trunk_y_by_net[net] = trunk_y
        local_template_by_net[net] = option.local_template
        xs = [escape_x] if shared_gate_escape else [anchor.x for anchor in option.anchors]
        if option.top_pin_xy is not None and not bridge_template:
            px, py = option.top_pin_xy
            xs.append(px)
            pin_rect = metal_pad(option.pin_layer, option.top_pin_xy, net, size=cfg.pin_size_um)
            rects.append(pin_rect)
            boundary_pins.append(OaPin(net, net, "inputOutput", option.pin_layer, pin_rect.bbox))
            if option.pin_layer != route_layer:
                stack = via_stack(px, py, option.pin_layer, route_layer, net)
                vias.extend(stack)
                for via in stack:
                    rects.extend(via_landing_rects(via.via_def, via.xy, net))
            if abs(py - trunk_y) >= route_width - 1e-12:
                paths.append(OaPath(route_layer, "drawing", ((px, py), (px, trunk_y)), route_width, net))
        for anchor in option.anchors:
            if not shared_gate_escape:
                rects.extend(access_landing_rects(anchor, net))
            bottom_layer = anchor.layer
            if bottom_layer == pdk.layer_map.gate:
                if option.local_template != "po_shared_trunk" and anchor.contact_layer in {"M0_PO", "M0_PO_VD"}:
                    vias.append(OaVia(anchor.contact_layer, (anchor.x, anchor.y), net))
                    rects.extend(via_landing_rects(anchor.contact_layer, (anchor.x, anchor.y), net))
                bottom_layer = pdk.layer_map.metals[0]
            elif bottom_layer == "MD":
                bottom_layer = pdk.layer_map.metals[0]
            elif bottom_layer in {"OD", "PDK", "NW"} and anchor.contact_layer in {"M0_SUB", "M0_NW"}:
                vias.append(OaVia(anchor.contact_layer, (anchor.x, anchor.y), net))
                rects.extend(via_landing_rects(anchor.contact_layer, (anchor.x, anchor.y), net))
                bottom_layer = pdk.layer_map.metals[0]
            if shared_gate_escape:
                if option.local_template == "po_shared_trunk":
                    po_width = layer_route_width(pdk.layer_map.gate)
                    if abs(anchor.x - escape_x) >= po_width - 1e-12:
                        paths.append(OaPath(pdk.layer_map.gate, "drawing", ((anchor.x, anchor.y), (escape_x, anchor.y)), po_width, net))
                else:
                    m0_width = layer_route_width(metal0)
                    if abs(anchor.x - escape_x) >= m0_width - 1e-12:
                        paths.append(OaPath(metal0, "drawing", ((anchor.x, anchor.y), (escape_x, anchor.y)), m0_width, net))
            else:
                stack = via_stack(anchor.x, anchor.y, bottom_layer, route_layer, net)
                vias.extend(stack)
                for via in stack:
                    rects.extend(via_landing_rects(via.via_def, via.xy, net))
                if abs(anchor.y - trunk_y) >= route_width - 1e-12:
                    paths.append(OaPath(route_layer, "drawing", ((anchor.x, anchor.y), (anchor.x, trunk_y)), route_width, net))
        if shared_gate_escape and option.anchors:
            y0 = min(anchor.y for anchor in option.anchors)
            y1 = max(anchor.y for anchor in option.anchors)
            if option.local_template == "po_shared_trunk":
                po_width = layer_route_width(pdk.layer_map.gate)
                if abs(y1 - y0) >= po_width - 1e-12:
                    paths.append(OaPath(pdk.layer_map.gate, "drawing", ((escape_x, y0), (escape_x, y1)), po_width, net))
                contact_layer = next((anchor.contact_layer for anchor in option.anchors if anchor.contact_layer), "")
                if contact_layer:
                    vias.append(OaVia(contact_layer, (escape_x, escape_y), net))
                    rects.extend(via_landing_rects(contact_layer, (escape_x, escape_y), net))
            else:
                m0_width = layer_route_width(metal0)
                if abs(y1 - y0) >= m0_width - 1e-12:
                    paths.append(OaPath(metal0, "drawing", ((escape_x, y0), (escape_x, y1)), m0_width, net))
            stack = via_stack(escape_x, escape_y, metal0, route_layer, net)
            vias.extend(stack)
            for via in stack:
                rects.extend(via_landing_rects(via.via_def, via.xy, net))
            if abs(escape_y - trunk_y) >= route_width - 1e-12:
                paths.append(OaPath(route_layer, "drawing", ((escape_x, escape_y), (escape_x, trunk_y)), route_width, net))
        if len(xs) >= 2 and not (shared_gate_escape and option.local_template == "top_bridge"):
            x0 = pdk.rules.snap_point_um((min(xs), trunk_y))[0]
            x1 = pdk.rules.snap_point_um((max(xs), trunk_y))[0]
            if abs(x1 - x0) >= route_width - 1e-12:
                paths.append(OaPath(route_layer, "drawing", ((x0, trunk_y), (x1, trunk_y)), route_width, net))
        if option.top_pin_xy is not None and bridge_template:
            px, py = option.top_pin_xy
            pin_rect = metal_pad(option.pin_layer, option.top_pin_xy, net, size=cfg.pin_size_um)
            rects.append(pin_rect)
            boundary_pins.append(OaPin(net, net, "inputOutput", option.pin_layer, pin_rect.bbox))
            if option.pin_layer != route_layer:
                stack = via_stack(px, py, option.pin_layer, route_layer, net)
                vias.extend(stack)
                for via in stack:
                    rects.extend(via_landing_rects(via.via_def, via.xy, net))
            if option.anchors:
                bx = escape_x if shared_gate_escape and option.local_template == "top_bridge" else (option.bridge_x if option.bridge_x is not None else bridge_escape_x(net, option.anchors))
                if option.local_template == "top_bridge":
                    if abs(py - escape_y) >= route_width - 1e-12:
                        paths.append(OaPath(route_layer, "drawing", ((bx, escape_y), (bx, py)), route_width, net))
                elif abs(py - trunk_y) >= route_width - 1e-12:
                    paths.append(OaPath(route_layer, "drawing", ((bx, py), (bx, trunk_y)), route_width, net))
                if abs(px - bx) >= route_width - 1e-12:
                    paths.append(OaPath(route_layer, "drawing", ((bx, py), (px, py)), route_width, net))

    plan = OaWritePlan(
        OaCellView(str(getattr(pcell_plan, "metadata", {}).get("lib", lib)), cell, view, "maskLayout"),
        nets=tuple(str(net) for net in graph.nets),
        pins=tuple(boundary_pins),
        rects=tuple(rects),
        paths=tuple(paths),
        vias=tuple(vias),
    )
    plan = snap_oa_write_plan_to_grid(plan, pdk)
    physical_report = analyze_plan_physical_connectivity(plan, pdk=pdk, include_via_landing_shorts=True)
    return StandardCellRouteResult(
        plan=plan,
        physical_report=physical_report,
        boundary_pins=tuple(boundary_pins),
        trunk_y_by_net=trunk_y_by_net,
        layer_by_net=layer_by_net,
        local_template_by_net=local_template_by_net,
        option_summary_by_net=option_summary_by_net,
    )


def _solve_standard_cell_dfs(
    problem: StandardCellProblem,
    domains: _PrunedDomains,
    *,
    max_solutions: int,
    base_stats: Mapping[str, object],
) -> StandardCellSolveResult:
    by_device = {item.device: item for item in problem.device_constraints}
    by_net = {item.net: item for item in problem.net_constraints}
    ordered_nets = tuple(item.net for item in problem.net_constraints if domains.track_domains.get(item.net))
    device_states = 0
    routing_states = 0
    best_cost = inf
    solutions: list[StandardCellSolution] = []

    def append_solution(placement: dict[str, int], tracks: dict[str, int], pins: dict[str, int]) -> None:
        nonlocal best_cost
        fixed_orientations = {
            item.device: _orientation_domain(item)[0]
            for item in problem.device_constraints
            if len(_orientation_domain(item)) == 1
        }
        solution = _build_solution(problem, placement, fixed_orientations, tracks, pins)
        solutions.append(solution)
        solutions.sort(key=lambda item: (item.cost, item.width_columns, len(item.net_tracks)))
        del solutions[max_solutions:]
        if solutions:
            best_cost = min(best_cost, solutions[0].cost)

    def route_nets(placement: dict[str, int], net_index: int, tracks: dict[str, int], pins: dict[str, int]) -> None:
        nonlocal routing_states
        if net_index >= len(ordered_nets):
            append_solution(placement, tracks, pins)
            return
        net = ordered_nets[net_index]
        track_domain = domains.track_domains.get(net, ())
        pin_domain = domains.pin_domains.get(net, problem.columns)
        for pin_column in pin_domain:
            if _pin_assignment_conflict(net, pin_column, pins, by_net):
                continue
            pins[net] = pin_column
            if not _pin_order_assignment_feasible(problem, pins):
                pins.pop(net, None)
                continue
            if not _pin_group_assignment_feasible(problem, pins):
                pins.pop(net, None)
                continue
            for track in track_domain:
                routing_states += 1
                if _track_conflict(net, track, tracks, by_net):
                    continue
                tracks[net] = track
                if not _internal_net_cluster_assignment_feasible(problem, tracks):
                    tracks.pop(net, None)
                    continue
                lower_bound = _lower_bound_cost(problem, placement, tracks, pins)
                if lower_bound > best_cost:
                    tracks.pop(net, None)
                    continue
                route_nets(placement, net_index + 1, tracks, pins)
                tracks.pop(net, None)
            pins.pop(net, None)

    def place_devices(index: int, placement: dict[str, int]) -> None:
        nonlocal device_states
        if index >= len(problem.device_order):
            route_nets(placement, 0, {}, {})
            return
        device = problem.device_order[index]
        constraint = by_device[device]
        for column in domains.device_domains.get(device, ()):
            device_states += 1
            placement[device] = column
            reason = _placement_violation(device, placement, by_device)
            if reason is None and not _device_cluster_assignment_feasible(problem, placement):
                reason = "cluster_span"
            if reason is not None:
                placement.pop(device, None)
                continue
            lower_bound = _width_lower_bound(problem, placement, domains.device_domains) * 100
            if lower_bound > best_cost:
                placement.pop(device, None)
                continue
            place_devices(index + 1, placement)
            placement.pop(device, None)

    place_devices(0, {})
    stats = StandardCellSolveStats(
        device_states_visited=device_states,
        routing_states_visited=routing_states,
        feasible_solutions=len(solutions),
        **base_stats,
    )
    return StandardCellSolveResult(problem=problem, solutions=tuple(solutions), stats=stats)


def _solve_standard_cell_z3(
    problem: StandardCellProblem,
    domains: _PrunedDomains,
    *,
    max_solutions: int,
    base_stats: Mapping[str, object],
) -> StandardCellSolveResult:
    assert z3 is not None
    seed_stats = {key: value for key, value in base_stats.items() if key not in {"pruned_states", "prune_reasons"}}
    bundle = _build_z3_solver(problem, domains)
    solver = bundle["solver"]
    device_vars = bundle["device_vars"]
    orient_vars = bundle["orient_vars"]
    orient_domains = bundle["orient_domains"]
    orient_index_maps = bundle["orient_index_maps"]
    track_vars = bundle["track_vars"]
    pin_vars = bundle["pin_vars"]
    width_expr = bundle["width_expr"]
    track_expr = bundle["track_expr"]
    pin_cost_expr = bundle["pin_cost_expr"]
    total_cost_expr = bundle["total_cost_expr"]
    stats_map = {
        "device_states_visited": 0,
        "routing_states_visited": 0,
        "solver_checks": 0,
        "branch_nodes": 0,
        "bound_updates": 0,
    }
    branch_prunes: dict[str, int] = {}
    solutions: list[StandardCellSolution] = []
    seen: set[tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...], tuple[tuple[str, int], ...]]] = set()

    def cost_limit() -> int | None:
        if len(solutions) >= max_solutions and solutions:
            return int(solutions[-1].cost)
        return None

    def add_solution(model: object, source: str) -> None:
        solution = _solution_from_z3_model(
            problem,
            model,
            device_vars,
            orient_vars,
            orient_domains,
            orient_index_maps,
            track_vars,
            pin_vars,
            width_expr,
            track_expr,
            pin_cost_expr,
            total_cost_expr,
        )
        signature = (
            tuple(solution.device_columns),
            tuple(solution.device_orientations),
            tuple(solution.net_tracks),
            tuple(solution.metadata.get("pin_columns", ())),
        )
        if signature in seen:
            return
        previous_best = solutions[0].cost if solutions else None
        seen.add(signature)
        solutions.append(
            StandardCellSolution(
                device_columns=solution.device_columns,
                net_tracks=solution.net_tracks,
                width_columns=solution.width_columns,
                cost=solution.cost,
                device_orientations=solution.device_orientations,
                metadata={**solution.metadata, "source": source},
            )
        )
        solutions.sort(key=lambda item: (item.cost, item.width_columns, len(item.net_tracks)))
        del solutions[max_solutions:]
        if solutions and (previous_best is None or solutions[0].cost < previous_best):
            stats_map["bound_updates"] += 1

    def branch(
        assigned_devices: dict[str, int],
        assigned_orients: dict[str, str],
        assigned_tracks: dict[str, int],
        assigned_pins: dict[str, int],
    ) -> None:
        stats_map["branch_nodes"] += 1
        limit = cost_limit()
        lower_bound = _partial_lower_bound(problem, domains, assigned_devices, assigned_tracks, assigned_pins)
        if limit is not None and (
            lower_bound > limit or (max_solutions <= 1 and lower_bound >= limit)
        ):
            branch_prunes["cost_bound"] = branch_prunes.get("cost_bound", 0) + 1
            return
        solver.push()
        if limit is not None:
            solver.add(_z3_cost_bound_expr(total_cost_expr, limit, max_solutions=max_solutions))
        stats_map["solver_checks"] += 1
        status = solver.check()
        if status != z3.sat:
            branch_prunes["z3_infeasible"] = branch_prunes.get("z3_infeasible", 0) + 1
            solver.pop()
            return
        model = solver.model()
        choice = _select_branch_variable(
            problem,
            domains,
            orient_domains,
            assigned_devices,
            assigned_orients,
            assigned_tracks,
            assigned_pins,
            model,
        )
        if choice is None:
            add_solution(model, "branch")
            solver.pop()
            return
        kind, name, values = choice
        solver.pop()
        for value in values:
            if kind == "device":
                stats_map["device_states_visited"] += 1
                assigned_devices[name] = value
                solver.push()
                solver.add(device_vars[name] == value)
                branch(assigned_devices, assigned_orients, assigned_tracks, assigned_pins)
                solver.pop()
                assigned_devices.pop(name, None)
            elif kind == "orient":
                stats_map["device_states_visited"] += 1
                assigned_orients[name] = value
                solver.push()
                solver.add(orient_vars[name] == orient_index_maps[name][value])
                branch(assigned_devices, assigned_orients, assigned_tracks, assigned_pins)
                solver.pop()
                assigned_orients.pop(name, None)
            elif kind == "track":
                stats_map["routing_states_visited"] += 1
                assigned_tracks[name] = value
                solver.push()
                solver.add(track_vars[name] == value)
                branch(assigned_devices, assigned_orients, assigned_tracks, assigned_pins)
                solver.pop()
                assigned_tracks.pop(name, None)
            else:
                stats_map["routing_states_visited"] += 1
                assigned_pins[name] = value
                solver.push()
                solver.add(pin_vars[name] == value)
                branch(assigned_devices, assigned_orients, assigned_tracks, assigned_pins)
                solver.pop()
                assigned_pins.pop(name, None)

    stats_map["solver_checks"] += 1
    if solver.check() == z3.sat:
        add_solution(solver.model(), "warm_start")
        branch({}, {}, {}, {})

    combined_prunes = {str(name): int(count) for name, count in dict(base_stats.get("prune_reasons", ())).items()}
    for name, count in branch_prunes.items():
        combined_prunes[name] = combined_prunes.get(name, 0) + int(count)
    stats = StandardCellSolveStats(
        feasible_solutions=len(solutions),
        incumbent_cost=solutions[0].cost if solutions else None,
        pruned_states=int(base_stats.get("pruned_states", 0) or 0) + sum(int(count) for count in branch_prunes.values()),
        prune_reasons=tuple(sorted((name, count) for name, count in combined_prunes.items() if count > 0)),
        **stats_map,
        **seed_stats,
    )
    return StandardCellSolveResult(problem=problem, solutions=tuple(solutions), stats=stats)


def _build_z3_solver(problem: StandardCellProblem, domains: _PrunedDomains) -> dict[str, object]:
    assert z3 is not None
    solver = z3.Solver()
    device_vars = {name: z3.Int(f"dev_{name}") for name in problem.device_order}
    orient_domains = {
        item.device: _orientation_domain(item)
        for item in problem.device_constraints
    }
    orient_index_maps = {
        name: {orient: idx for idx, orient in enumerate(domain)}
        for name, domain in orient_domains.items()
    }
    orient_vars = {name: z3.Int(f"orient_{name}") for name in problem.device_order}
    track_vars = {item.net: z3.Int(f"track_{item.net}") for item in problem.net_constraints if domains.track_domains.get(item.net)}
    pin_vars = {item.net: z3.Int(f"pin_{item.net}") for item in problem.net_constraints if domains.pin_domains.get(item.net)}

    for name, var in device_vars.items():
        solver.add(z3.Or([var == value for value in domains.device_domains[name]]))
    for name, var in orient_vars.items():
        solver.add(z3.Or([var == value for value in orient_index_maps[name].values()]))
    for name, var in track_vars.items():
        solver.add(z3.Or([var == value for value in domains.track_domains[name]]))
    for name, var in pin_vars.items():
        solver.add(z3.Or([var == value for value in domains.pin_domains[name]]))

    row_bounds: dict[str, Mapping[str, object]] = {}
    for row in problem.rows:
        row_devices = [item.device for item in problem.device_constraints if item.row == row]
        if row_devices:
            _, row_bounds[row] = _z3_span_expr(solver, tuple(device_vars[name] for name in row_devices), f"row_{row}")
        for idx, left in enumerate(row_devices):
            for right in row_devices[idx + 1 :]:
                solver.add(device_vars[left] != device_vars[right])

    for constraint in problem.device_constraints:
        dev_var = device_vars[constraint.device]
        for other in constraint.order_before:
            if other in device_vars:
                solver.add(dev_var < device_vars[other])
        for other in constraint.adjacent_to:
            if other in device_vars:
                solver.add(z3.Abs(dev_var - device_vars[other]) == 1)

    for idx, left in enumerate(problem.net_constraints):
        for right in problem.net_constraints[idx + 1 :]:
            if left.net in track_vars and right.net in track_vars and _nets_require_track_separation(left, right):
                solver.add(track_vars[left.net] != track_vars[right.net])
            if left.net in pin_vars and right.net in pin_vars and _nets_require_pin_separation(left, right):
                solver.add(pin_vars[left.net] != pin_vars[right.net])

    for _, group in _ordered_pin_groups(problem.net_constraints).items():
        ordered = sorted(group, key=lambda item: (item.pin_order_index, item.net))
        for left, right in zip(ordered, ordered[1:]):
            if left.net in pin_vars and right.net in pin_vars:
                solver.add(pin_vars[left.net] < pin_vars[right.net])

    for cluster in problem.device_clusters:
        if cluster.max_span <= 0:
            continue
        vars_ = tuple(device_vars[name] for name in cluster.devices if name in device_vars)
        if len(vars_) >= 2:
            cluster_span_expr, _ = _z3_span_expr(solver, vars_, f"cluster_{cluster.name}")
            solver.add(cluster_span_expr <= int(cluster.max_span))

    for group in problem.pin_groups:
        if group.max_span <= 0:
            continue
        vars_ = tuple(pin_vars[name] for name in group.nets if name in pin_vars)
        if len(vars_) >= 2:
            group_span_expr, _ = _z3_span_expr(solver, vars_, f"pin_group_{group.name}")
            solver.add(group_span_expr <= int(group.max_span))

    for cluster in problem.internal_net_clusters:
        vars_ = tuple(track_vars[name] for name in cluster.nets if name in track_vars)
        if len(vars_) >= 2 and cluster.max_track_span > 0:
            track_cluster_span_expr, _ = _z3_span_expr(solver, vars_, f"track_cluster_{cluster.name}")
            solver.add(track_cluster_span_expr <= int(cluster.max_track_span))
        if cluster.ordered:
            ordered = tuple(name for name in cluster.nets if name in track_vars)
            for left, right in zip(ordered, ordered[1:]):
                solver.add(track_vars[left] < track_vars[right])

    width_expr, _ = _z3_span_expr(solver, tuple(device_vars.values()), "width")
    track_expr, _ = _z3_span_expr(solver, tuple(track_vars.values()), "track")
    diffusion_break_terms = []
    shared_rail_terms = []
    rail_nets = {str(item) for item in tuple(problem.metadata.get("rail_nets", ()) or ())}
    row_devices = {
        row: tuple(item.device for item in problem.device_constraints if item.row == row)
        for row in problem.rows
    }
    for row, names in row_devices.items():
        for left in names:
            for right in names:
                if left == right:
                    continue
                adjacent = device_vars[right] == device_vars[left] + 1
                share_terms = []
                share_rail_terms = []
                for left_orient in orient_domains[left]:
                    left_right_net = _orientation_side_net(problem, left, left_orient, side="right")
                    if not left_right_net:
                        continue
                    for right_orient in orient_domains[right]:
                        right_left_net = _orientation_side_net(problem, right, right_orient, side="left")
                        if left_right_net != right_left_net:
                            continue
                        share_terms.append(
                            z3.And(
                                orient_vars[left] == orient_index_maps[left][left_orient],
                                orient_vars[right] == orient_index_maps[right][right_orient],
                            )
                        )
                        if left_right_net in rail_nets:
                            share_rail_terms.append(
                                z3.And(
                                    orient_vars[left] == orient_index_maps[left][left_orient],
                                    orient_vars[right] == orient_index_maps[right][right_orient],
                                )
                            )
                share_ok = z3.Or(share_terms) if share_terms else z3.BoolVal(False)
                share_rail = z3.Or(share_rail_terms) if share_rail_terms else z3.BoolVal(False)
                diffusion_break_terms.append(z3.If(z3.And(adjacent, z3.Not(share_ok)), 1, 0))
                shared_rail_terms.append(z3.If(z3.And(adjacent, share_rail), 1, 0))

    pin_cost_terms = []
    pin_span_terms = []
    device_anchor_terms = []
    left_boundary = problem.columns[0] if problem.columns else 0
    right_boundary = problem.columns[-1] if problem.columns else 0
    for constraint in problem.device_constraints:
        row_bound = row_bounds.get(constraint.row, {})
        row_min = row_bound.get("min")
        row_max = row_bound.get("max")
        if constraint.boundary_anchor == "left" and row_min is not None:
            device_anchor_terms.append(device_vars[constraint.device] - row_min)
        elif constraint.boundary_anchor == "right" and row_max is not None:
            device_anchor_terms.append(row_max - device_vars[constraint.device])
    for constraint in problem.net_constraints:
        if constraint.net not in pin_vars:
            continue
        pin_var = pin_vars[constraint.net]
        if constraint.pin_side == "left":
            pin_cost_terms.append(z3.Abs(pin_var - left_boundary))
        elif constraint.pin_side == "right":
            pin_cost_terms.append(z3.Abs(pin_var - right_boundary))
    for side, group in _ordered_pin_groups(problem.net_constraints).items():
        vars_ = tuple(pin_vars[item.net] for item in sorted(group, key=lambda item: (item.pin_order_index, item.net)) if item.net in pin_vars)
        if len(vars_) >= 2:
            span_expr, _ = _z3_span_expr(solver, vars_, f"pin_side_{side}")
            pin_span_terms.append(span_expr)
    for group in problem.pin_groups:
        vars_ = tuple(pin_vars[name] for name in group.nets if name in pin_vars)
        if len(vars_) >= 2:
            span_expr, _ = _z3_span_expr(solver, vars_, f"pin_group_cost_{group.name}")
            pin_span_terms.append(span_expr)
    diffusion_break_expr = z3.Int("diffusion_break_cost")
    solver.add(diffusion_break_expr == z3.Sum(diffusion_break_terms) if diffusion_break_terms else diffusion_break_expr == 0)
    shared_rail_expr = z3.Int("shared_rail_cost")
    solver.add(shared_rail_expr == z3.Sum(shared_rail_terms) if shared_rail_terms else shared_rail_expr == 0)
    device_anchor_expr = z3.Int("device_anchor_cost")
    solver.add(device_anchor_expr == z3.Sum(device_anchor_terms) if device_anchor_terms else device_anchor_expr == 0)
    pin_span_expr = z3.Int("pin_span_cost")
    solver.add(pin_span_expr == z3.Sum(pin_span_terms) if pin_span_terms else pin_span_expr == 0)
    pin_cost_expr = z3.Int("pin_cost")
    solver.add(
        pin_cost_expr
        == (
            (z3.Sum(pin_cost_terms) if pin_cost_terms else z3.IntVal(0))
            + pin_span_expr * 4
            + device_anchor_expr * 2
        )
    )
    total_cost_expr = z3.Int("total_cost")
    solver.add(total_cost_expr == width_expr * 1000 + diffusion_break_expr * 200 + shared_rail_expr * 40 + track_expr * 20 + pin_cost_expr)
    return {
        "solver": solver,
        "device_vars": device_vars,
        "orient_vars": orient_vars,
        "orient_domains": orient_domains,
        "orient_index_maps": orient_index_maps,
        "track_vars": track_vars,
        "pin_vars": pin_vars,
        "width_expr": width_expr,
        "track_expr": track_expr,
        "pin_cost_expr": pin_cost_expr,
        "total_cost_expr": total_cost_expr,
    }


def _solution_from_z3_model(
    problem: StandardCellProblem,
    model: object,
    device_vars: Mapping[str, object],
    orient_vars: Mapping[str, object],
    orient_domains: Mapping[str, tuple[str, ...]],
    orient_index_maps: Mapping[str, Mapping[str, int]],
    track_vars: Mapping[str, object],
    pin_vars: Mapping[str, object],
    width_expr: object,
    track_expr: object,
    pin_cost_expr: object,
    total_cost_expr: object,
) -> StandardCellSolution:
    assert z3 is not None
    placement = {name: model.eval(var, model_completion=True).as_long() for name, var in device_vars.items()}
    reverse_orient_maps = {
        name: {index: orient for orient, index in index_map.items()}
        for name, index_map in orient_index_maps.items()
    }
    orientations = {
        name: reverse_orient_maps[name][model.eval(var, model_completion=True).as_long()]
        for name, var in orient_vars.items()
    }
    tracks = {name: model.eval(var, model_completion=True).as_long() for name, var in track_vars.items()}
    pins = {name: model.eval(var, model_completion=True).as_long() for name, var in pin_vars.items()}
    return _build_solution(
        problem,
        placement,
        orientations,
        tracks,
        pins,
        overrides={
            "width_columns": model.eval(width_expr, model_completion=True).as_long(),
            "track_span": model.eval(track_expr, model_completion=True).as_long(),
            "pin_access_cost": model.eval(pin_cost_expr, model_completion=True).as_long(),
            "cost": model.eval(total_cost_expr, model_completion=True).as_long(),
        },
    )


def _z3_cost_bound_expr(total_cost_expr: object, limit: int, *, max_solutions: int) -> object:
    assert z3 is not None
    if max_solutions <= 1:
        return total_cost_expr < int(limit)
    return total_cost_expr <= int(limit)


def _select_branch_variable(
    problem: StandardCellProblem,
    domains: _PrunedDomains,
    orient_domains: Mapping[str, tuple[str, ...]],
    assigned_devices: Mapping[str, int],
    assigned_orients: Mapping[str, str],
    assigned_tracks: Mapping[str, int],
    assigned_pins: Mapping[str, int],
    model: object,
) -> tuple[str, str, tuple[int, ...]] | None:
    candidates: list[tuple[tuple[int, int, int, str], str, str, tuple[int, ...]]] = []
    device_cluster_degree = {
        name: sum(1 for cluster in problem.device_clusters if name in cluster.devices)
        for name in problem.device_order
    }
    pin_group_degree = {
        name: sum(1 for group in problem.pin_groups if name in group.nets)
        for name in (constraint.net for constraint in problem.net_constraints)
    }
    internal_track_cluster_degree = {
        name: sum(1 for cluster in problem.internal_net_clusters if name in cluster.nets)
        for name in (constraint.net for constraint in problem.net_constraints)
    }
    for constraint in problem.device_constraints:
        if constraint.device in assigned_devices:
            continue
        domain = domains.device_domains[constraint.device]
        if len(domain) <= 1:
            continue
        degree = len(constraint.order_before) + len(constraint.adjacent_to) + device_cluster_degree.get(constraint.device, 0)
        candidates.append(((0, len(domain), -degree, constraint.device), "device", constraint.device, _model_ordered_values(domain, model, f"dev_{constraint.device}")))
        if constraint.device not in assigned_orients:
            orient_domain = orient_domains.get(constraint.device, ())
            if len(orient_domain) > 1:
                orient_degree = degree + (1 if constraint.boundary_anchor else 0)
                candidates.append(
                    (
                        (1, len(orient_domain), -orient_degree, constraint.device),
                        "orient",
                        constraint.device,
                        _model_ordered_labels(
                            orient_domain,
                            model,
                            f"orient_{constraint.device}",
                            {orient: idx for idx, orient in enumerate(orient_domain)},
                        ),
                    )
                )
    for constraint in problem.net_constraints:
        if constraint.net not in assigned_pins:
            pin_domain = domains.pin_domains.get(constraint.net, ())
            if len(pin_domain) > 1:
                degree = (2 if constraint.pin_order_index is not None else 0) + pin_group_degree.get(constraint.net, 0)
                candidates.append(((2, len(pin_domain), -degree, constraint.net), "pin", constraint.net, _model_ordered_values(pin_domain, model, f"pin_{constraint.net}")))
        if constraint.net not in assigned_tracks:
            track_domain = domains.track_domains.get(constraint.net, ())
            if len(track_domain) > 1:
                degree = len(constraint.avoid_nets) + (1 if constraint.pin_side != "internal" else 0) + internal_track_cluster_degree.get(constraint.net, 0)
                candidates.append(((3, len(track_domain), -degree, constraint.net), "track", constraint.net, _model_ordered_values(track_domain, model, f"track_{constraint.net}")))
    if not candidates:
        return None
    _, kind, name, values = min(candidates, key=lambda item: item[0])
    return (kind, name, values)


def _model_ordered_values(domain: tuple[int, ...], model: object, var_name: str) -> tuple[int, ...]:
    assert z3 is not None
    preferred = model.eval(z3.Int(var_name), model_completion=True).as_long()
    return tuple(sorted(domain, key=lambda value: (abs(value - preferred), value)))


def _model_ordered_labels(
    domain: tuple[str, ...],
    model: object,
    var_name: str,
    index_map: Mapping[str, int],
) -> tuple[str, ...]:
    assert z3 is not None
    preferred = model.eval(z3.Int(var_name), model_completion=True).as_long()
    return tuple(sorted(domain, key=lambda value: (abs(index_map[value] - preferred), index_map[value], value)))


def _partial_lower_bound(
    problem: StandardCellProblem,
    domains: _PrunedDomains,
    assigned_devices: Mapping[str, int],
    assigned_tracks: Mapping[str, int],
    assigned_pins: Mapping[str, int],
) -> float:
    width_bound = _width_lower_bound(problem, assigned_devices, _override_domains(domains.device_domains, assigned_devices))
    track_bound = _track_lower_bound(domains.track_domains, assigned_tracks)
    pin_bound = _pin_cost_lower_bound(problem, domains.pin_domains, assigned_pins)
    return float(width_bound * 100 + track_bound * 10 + pin_bound)


def _override_domains(domains_map: Mapping[str, tuple[int, ...]], assigned: Mapping[str, int]) -> dict[str, tuple[int, ...]]:
    merged = {str(name): tuple(values) for name, values in domains_map.items()}
    for name, value in assigned.items():
        merged[str(name)] = (int(value),)
    return merged


def _track_lower_bound(track_domains: Mapping[str, tuple[int, ...]], assigned_tracks: Mapping[str, int]) -> int:
    if not track_domains:
        return 0
    effective = _override_domains(track_domains, assigned_tracks)
    minima = [min(values) for values in effective.values() if values]
    maxima = [max(values) for values in effective.values() if values]
    if not minima or not maxima:
        return 0
    return max(maxima) - min(minima) + 1


def _pin_cost_lower_bound(
    problem: StandardCellProblem,
    pin_domains: Mapping[str, tuple[int, ...]],
    assigned_pins: Mapping[str, int],
) -> int:
    if not problem.columns:
        return 0
    left_boundary = problem.columns[0]
    right_boundary = problem.columns[-1]
    effective = _override_domains(pin_domains, assigned_pins)
    constraint_by_net = {constraint.net: constraint for constraint in problem.net_constraints}
    cost = 0
    for net, domain in effective.items():
        if not domain:
            continue
        constraint = constraint_by_net.get(net)
        if constraint is None:
            continue
        if constraint.pin_side == "left":
            cost += min(abs(value - left_boundary) for value in domain)
        elif constraint.pin_side == "right":
            cost += min(abs(value - right_boundary) for value in domain)
    return cost


def _prune_domains(problem: StandardCellProblem) -> _PrunedDomains:
    device_domains = {item.device: _device_domain(item, problem.columns) for item in problem.device_constraints}
    track_domains = {item.net: _track_domain(item, problem.columns) for item in problem.net_constraints}
    pin_domains = {item.net: _pin_domain(item, problem.columns) for item in problem.net_constraints}
    prune_counts: dict[str, int] = {}
    reduction_count = 0
    by_device = {item.device: item for item in problem.device_constraints}
    ordered_pin_groups = _ordered_pin_groups(problem.net_constraints)

    def prune(reason: str, count: int = 1) -> None:
        prune_counts[reason] = prune_counts.get(reason, 0) + count

    changed = True
    while changed:
        changed = False
        for constraint in problem.device_constraints:
            domain = device_domains[constraint.device]
            if not domain:
                return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "empty_device_domain")
            for other_name in constraint.order_before:
                other = device_domains.get(other_name, ())
                if not other:
                    return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "empty_device_domain")
                allowed = tuple(value for value in domain if value < max(other))
                if allowed != domain:
                    prune("order_prune", len(domain) - len(allowed))
                    reduction_count += len(domain) - len(allowed)
                    device_domains[constraint.device] = allowed
                    domain = allowed
                    changed = True
                    if not domain:
                        return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "order_domain_empty")
            for other_name in constraint.adjacent_to:
                other = set(device_domains.get(other_name, ()))
                if not other:
                    return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "empty_device_domain")
                allowed = tuple(value for value in domain if (value - 1 in other) or (value + 1 in other))
                if allowed != domain:
                    prune("adjacency_prune", len(domain) - len(allowed))
                    reduction_count += len(domain) - len(allowed)
                    device_domains[constraint.device] = allowed
                    domain = allowed
                    changed = True
                    if not domain:
                        return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "adjacency_domain_empty")

        for constraint in problem.device_constraints:
            domain = device_domains[constraint.device]
            for other_name, other_constraint in by_device.items():
                if constraint.device not in other_constraint.order_before:
                    continue
                other = device_domains.get(other_name, ())
                if not other:
                    return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "empty_device_domain")
                allowed = tuple(value for value in domain if value > min(other))
                if allowed != domain:
                    prune("reverse_order_prune", len(domain) - len(allowed))
                    reduction_count += len(domain) - len(allowed)
                    device_domains[constraint.device] = allowed
                    changed = True
                    if not allowed:
                        return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "order_domain_empty")

        for side, items in ordered_pin_groups.items():
            ordered = sorted(items, key=lambda item: (item.pin_order_index, item.net))
            for left, right in zip(ordered, ordered[1:]):
                left_domain = pin_domains[left.net]
                right_domain = pin_domains[right.net]
                if not left_domain or not right_domain:
                    return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "empty_pin_domain")
                left_allowed = tuple(value for value in left_domain if value < max(right_domain))
                right_allowed = tuple(value for value in right_domain if value > min(left_domain))
                if left_allowed != left_domain:
                    prune("pin_order_prune", len(left_domain) - len(left_allowed))
                    reduction_count += len(left_domain) - len(left_allowed)
                    pin_domains[left.net] = left_allowed
                    changed = True
                    if not left_allowed:
                        return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "pin_order_domain_empty")
                if right_allowed != right_domain:
                    prune("pin_order_prune", len(right_domain) - len(right_allowed))
                    reduction_count += len(right_domain) - len(right_allowed)
                    pin_domains[right.net] = right_allowed
                    changed = True
                    if not right_allowed:
                        return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "pin_order_domain_empty")

        for cluster in problem.device_clusters:
            if cluster.max_span <= 0:
                continue
            for device in cluster.devices:
                domain = device_domains.get(device, ())
                if not domain:
                    return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "empty_device_domain")
                allowed = tuple(
                    value
                    for value in domain
                    if _device_cluster_value_feasible(cluster, device, value, device_domains)
                )
                if allowed != domain:
                    prune("cluster_span_prune", len(domain) - len(allowed))
                    reduction_count += len(domain) - len(allowed)
                    device_domains[device] = allowed
                    changed = True
                    if not allowed:
                        return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "cluster_span_domain_empty")

        for group in problem.pin_groups:
            if group.max_span <= 0:
                continue
            for net in group.nets:
                domain = pin_domains.get(net, ())
                if not domain:
                    return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "empty_pin_domain")
                allowed = tuple(
                    value
                    for value in domain
                    if _pin_group_value_feasible(group, net, value, pin_domains)
                )
                if allowed != domain:
                    prune("pin_group_span_prune", len(domain) - len(allowed))
                    reduction_count += len(domain) - len(allowed)
                    pin_domains[net] = allowed
                    changed = True
                    if not allowed:
                        return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "pin_group_domain_empty")

        for cluster in problem.internal_net_clusters:
            if cluster.max_track_span > 0:
                for net in cluster.nets:
                    domain = track_domains.get(net, ())
                    if not domain:
                        return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "empty_track_domain")
                    allowed = tuple(
                        value
                        for value in domain
                        if _internal_net_cluster_value_feasible(cluster, net, value, track_domains)
                    )
                    if allowed != domain:
                        prune("internal_track_cluster_span_prune", len(domain) - len(allowed))
                        reduction_count += len(domain) - len(allowed)
                        track_domains[net] = allowed
                        changed = True
                        if not allowed:
                            return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "internal_track_cluster_domain_empty")
            if cluster.ordered:
                ordered_nets = tuple(net for net in cluster.nets if net in track_domains)
                for left, right in zip(ordered_nets, ordered_nets[1:]):
                    left_domain = track_domains[left]
                    right_domain = track_domains[right]
                    if not left_domain or not right_domain:
                        return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "empty_track_domain")
                    left_allowed = tuple(value for value in left_domain if value < max(right_domain))
                    right_allowed = tuple(value for value in right_domain if value > min(left_domain))
                    if left_allowed != left_domain:
                        prune("internal_track_order_prune", len(left_domain) - len(left_allowed))
                        reduction_count += len(left_domain) - len(left_allowed)
                        track_domains[left] = left_allowed
                        changed = True
                        if not left_allowed:
                            return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "internal_track_order_domain_empty")
                    if right_allowed != right_domain:
                        prune("internal_track_order_prune", len(right_domain) - len(right_allowed))
                        reduction_count += len(right_domain) - len(right_allowed)
                        track_domains[right] = right_allowed
                        changed = True
                        if not right_allowed:
                            return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "internal_track_order_domain_empty")

    for idx, left in enumerate(problem.net_constraints):
        for right in problem.net_constraints[idx + 1 :]:
            if _nets_require_track_separation(left, right):
                left_track = track_domains.get(left.net, ())
                right_track = track_domains.get(right.net, ())
                if len(left_track) == 1 and left_track == right_track:
                    prune("track_conflict", 1)
                    return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "fixed_track_conflict")
            if _nets_require_pin_separation(left, right):
                left_pin = pin_domains.get(left.net, ())
                right_pin = pin_domains.get(right.net, ())
                if len(left_pin) == 1 and left_pin == right_pin:
                    prune("pin_conflict", 1)
                    return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count, "fixed_pin_conflict")

    return _PrunedDomains(device_domains, track_domains, pin_domains, prune_counts, reduction_count)


def _build_solution(
    problem: StandardCellProblem,
    placement: Mapping[str, int],
    orientations: Mapping[str, str],
    tracks: Mapping[str, int],
    pins: Mapping[str, int],
    *,
    overrides: Mapping[str, object] | None = None,
) -> StandardCellSolution:
    overrides = dict(overrides or {})
    width = int(overrides.get("width_columns", _width_columns(placement)))
    track_span = int(overrides.get("track_span", _track_span(tracks)))
    pin_cost = int(overrides.get("pin_access_cost", _pin_access_cost(problem, pins)))
    cost = float(overrides.get("cost", width * 100 + track_span * 10 + pin_cost))
    return StandardCellSolution(
        device_columns=tuple(sorted((str(name), int(column)) for name, column in placement.items())),
        device_orientations=tuple(sorted((str(name), str(orient)) for name, orient in orientations.items())),
        net_tracks=tuple(sorted((str(name), int(track)) for name, track in tracks.items())),
        width_columns=width,
        cost=cost,
        metadata={
            "track_span": track_span,
            "pin_access_cost": pin_cost,
            "pin_columns": tuple(sorted((str(name), int(column)) for name, column in pins.items())),
            **overrides,
        },
    )


def _device_domain(constraint: StandardCellDeviceConstraint, columns: tuple[int, ...]) -> tuple[int, ...]:
    if constraint.fixed_column is not None:
        return (int(constraint.fixed_column),)
    if constraint.allowed_columns:
        return tuple(sorted(dict.fromkeys(int(item) for item in constraint.allowed_columns)))
    return columns


def _orientation_domain(constraint: StandardCellDeviceConstraint) -> tuple[str, ...]:
    if constraint.fixed_orientation:
        return (str(constraint.fixed_orientation),)
    if constraint.allowed_orientations:
        return tuple(dict.fromkeys(str(item) for item in constraint.allowed_orientations))
    return ("R0", "MY")


def _orientation_side_net(
    problem: StandardCellProblem,
    device: str,
    orient: str,
    *,
    side: str,
) -> str:
    device_terminal_nets = dict(problem.metadata.get("device_terminal_nets", {}) or {})
    terminal_nets = dict(device_terminal_nets.get(device, {}) or {})
    source_net = str(terminal_nets.get("S", "") or "")
    drain_net = str(terminal_nets.get("D", "") or "")
    if not source_net or not drain_net:
        return ""
    if orient in {"MY", "R180"}:
        left_net, right_net = drain_net, source_net
    else:
        left_net, right_net = source_net, drain_net
    return left_net if side == "left" else right_net


def _track_domain(constraint: StandardCellNetConstraint, columns: tuple[int, ...]) -> tuple[int, ...]:
    if constraint.fixed_track is not None:
        return (int(constraint.fixed_track),)
    if constraint.allowed_tracks:
        return tuple(sorted(dict.fromkeys(int(item) for item in constraint.allowed_tracks)))
    return columns


def _pin_domain(constraint: StandardCellNetConstraint, columns: tuple[int, ...]) -> tuple[int, ...]:
    if constraint.allowed_pin_columns:
        return tuple(sorted(dict.fromkeys(int(item) for item in constraint.allowed_pin_columns)))
    return columns


def _device_cluster_value_feasible(
    cluster: StandardCellDeviceClusterConstraint,
    target_device: str,
    target_value: int,
    device_domains: Mapping[str, tuple[int, ...]],
) -> bool:
    domains = [tuple(values) for name, values in device_domains.items() if name in cluster.devices and name != target_device]
    return _window_feasible_for_value(int(target_value), domains, int(cluster.max_span))


def _pin_group_value_feasible(
    group: StandardCellPinGroupConstraint,
    target_net: str,
    target_value: int,
    pin_domains: Mapping[str, tuple[int, ...]],
) -> bool:
    domains = [tuple(values) for name, values in pin_domains.items() if name in group.nets and name != target_net]
    return _window_feasible_for_value(int(target_value), domains, int(group.max_span))


def _internal_net_cluster_value_feasible(
    cluster: StandardCellInternalNetClusterConstraint,
    target_net: str,
    target_value: int,
    track_domains: Mapping[str, tuple[int, ...]],
) -> bool:
    if cluster.max_track_span <= 0:
        return True
    domains = [tuple(values) for name, values in track_domains.items() if name in cluster.nets and name != target_net]
    return _window_feasible_for_value(int(target_value), domains, int(cluster.max_track_span))


def _window_feasible_for_value(target_value: int, other_domains: list[tuple[int, ...]], max_span: int) -> bool:
    if max_span <= 0:
        return True
    if not other_domains:
        return True
    start_min = target_value - max_span + 1
    start_max = target_value
    for start in range(start_min, start_max + 1):
        end = start + max_span - 1
        if target_value < start or target_value > end:
            continue
        if all(any(start <= candidate <= end for candidate in domain) for domain in other_domains):
            return True
    return False


def _row_occupancy_conflict(
    target: StandardCellDeviceConstraint,
    placement: Mapping[str, int],
    by_device: Mapping[str, StandardCellDeviceConstraint],
) -> bool:
    target_column = placement[target.device]
    for other, other_column in placement.items():
        if other == target.device or other_column != target_column:
            continue
        if by_device[other].row == target.row:
            return True
    return False


def _placement_violation(
    device: str,
    placement: Mapping[str, int],
    by_device: Mapping[str, StandardCellDeviceConstraint],
) -> str | None:
    current = by_device[device]
    if _row_occupancy_conflict(current, placement, by_device):
        return "row_overlap"
    for other in current.order_before:
        if other in placement and placement[device] >= placement[other]:
            return "order_before"
    for other in current.adjacent_to:
        if other in placement and abs(placement[device] - placement[other]) != 1:
            return "adjacency"
    for other_name, other in by_device.items():
        if device not in other.order_before:
            continue
        if other_name in placement and placement[other_name] >= placement[device]:
            return "order_before"
    for other_name, other in by_device.items():
        if device not in other.adjacent_to:
            continue
        if other_name in placement and abs(placement[other_name] - placement[device]) != 1:
            return "adjacency"
    return None


def _ordered_pin_groups(constraints: tuple[StandardCellNetConstraint, ...]) -> dict[str, list[StandardCellNetConstraint]]:
    grouped: dict[str, list[StandardCellNetConstraint]] = {}
    for constraint in constraints:
        if constraint.pin_order_index is None:
            continue
        grouped.setdefault(constraint.pin_side, []).append(constraint)
    return grouped


def _pin_order_assignment_feasible(problem: StandardCellProblem, pins: Mapping[str, int]) -> bool:
    for side, group in _ordered_pin_groups(problem.net_constraints).items():
        ordered = sorted(group, key=lambda item: (item.pin_order_index, item.net))
        for left, right in zip(ordered, ordered[1:]):
            if left.net in pins and right.net in pins and pins[left.net] >= pins[right.net]:
                return False
    return True


def _device_cluster_assignment_feasible(problem: StandardCellProblem, placement: Mapping[str, int]) -> bool:
    for cluster in problem.device_clusters:
        if cluster.max_span <= 0:
            continue
        assigned = [placement[name] for name in cluster.devices if name in placement]
        if len(assigned) >= 2 and max(assigned) - min(assigned) + 1 > int(cluster.max_span):
            return False
    return True


def _pin_group_assignment_feasible(problem: StandardCellProblem, pins: Mapping[str, int]) -> bool:
    for group in problem.pin_groups:
        if group.max_span <= 0:
            continue
        assigned = [pins[name] for name in group.nets if name in pins]
        if len(assigned) >= 2 and max(assigned) - min(assigned) + 1 > int(group.max_span):
            return False
    return True


def _internal_net_cluster_assignment_feasible(problem: StandardCellProblem, tracks: Mapping[str, int]) -> bool:
    for cluster in problem.internal_net_clusters:
        assigned = [tracks[name] for name in cluster.nets if name in tracks]
        if cluster.max_track_span > 0 and len(assigned) >= 2:
            if max(assigned) - min(assigned) + 1 > int(cluster.max_track_span):
                return False
        if cluster.ordered:
            ordered = [tracks[name] for name in cluster.nets if name in tracks]
            for left, right in zip(ordered, ordered[1:]):
                if left >= right:
                    return False
    return True


def _z3_span_expr(
    optimizer: object,
    vars_: tuple[object, ...],
    prefix: str,
) -> tuple[object, Mapping[str, object]]:
    assert z3 is not None
    if not vars_:
        return (z3.IntVal(0), {})
    min_var = z3.Int(f"{prefix}_min")
    max_var = z3.Int(f"{prefix}_max")
    for var in vars_:
        optimizer.add(min_var <= var)
        optimizer.add(max_var >= var)
    optimizer.add(z3.Or([min_var == var for var in vars_]))
    optimizer.add(z3.Or([max_var == var for var in vars_]))
    return (max_var - min_var + 1, {"min": min_var, "max": max_var})


def _nets_require_track_separation(left: StandardCellNetConstraint, right: StandardCellNetConstraint) -> bool:
    return bool(left.pin_side == right.pin_side or right.net in left.avoid_nets or left.net in right.avoid_nets)


def _nets_require_pin_separation(left: StandardCellNetConstraint, right: StandardCellNetConstraint) -> bool:
    return bool(left.pin_side == right.pin_side and left.pin_side != "internal")


def _pin_assignment_conflict(
    net: str,
    pin_column: int,
    assigned: Mapping[str, int],
    by_net: Mapping[str, StandardCellNetConstraint],
) -> bool:
    current = by_net[net]
    for other, other_column in assigned.items():
        if other == net or other_column != pin_column:
            continue
        if _nets_require_pin_separation(current, by_net[other]):
            return True
    return False


def _track_conflict(
    net: str,
    track: int,
    assigned: Mapping[str, int],
    by_net: Mapping[str, StandardCellNetConstraint],
) -> bool:
    current = by_net[net]
    for other, other_track in assigned.items():
        if other_track != track or other == net:
            continue
        if _nets_require_track_separation(current, by_net[other]):
            return True
    return False


def _width_lower_bound(
    problem: StandardCellProblem,
    placement: Mapping[str, int],
    device_domains: Mapping[str, tuple[int, ...]],
) -> int:
    assigned = tuple(placement.values())
    if not assigned:
        return 1
    current_min = min(assigned)
    current_max = max(assigned)
    for constraint in problem.device_constraints:
        if constraint.device in placement:
            continue
        domain = device_domains[constraint.device]
        current_min = min(current_min, min(domain))
        current_max = max(current_max, max(domain))
    return current_max - current_min + 1


def _width_columns(placement: Mapping[str, int]) -> int:
    if not placement:
        return 0
    values = tuple(placement.values())
    return max(values) - min(values) + 1


def _track_span(tracks: Mapping[str, int]) -> int:
    if not tracks:
        return 0
    values = tuple(tracks.values())
    return max(values) - min(values) + 1


def _pin_access_cost(problem: StandardCellProblem, pins: Mapping[str, int]) -> int:
    cost = 0
    for constraint in problem.net_constraints:
        pin_column = pins.get(constraint.net)
        if pin_column is None:
            continue
        if constraint.pin_side == "left":
            cost += abs(pin_column - problem.columns[0])
        elif constraint.pin_side == "right":
            cost += abs(pin_column - problem.columns[-1])
    return cost


def _lower_bound_cost(
    problem: StandardCellProblem,
    placement: Mapping[str, int],
    tracks: Mapping[str, int],
    pins: Mapping[str, int],
) -> float:
    return float(_width_columns(placement) * 100 + max(1, _track_span(tracks)) * 10 + _pin_access_cost(problem, pins))
