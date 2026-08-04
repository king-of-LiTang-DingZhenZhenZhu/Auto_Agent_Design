"""Coupled differential/bus routing and current-aware power mesh synthesis."""
from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush
from math import ceil, hypot, isfinite
from typing import Mapping, Sequence

from .routing import Grid, Point, RoutedNet, route_length


@dataclass(frozen=True)
class CoupledRouteResult:
    routes: tuple[RoutedNet, ...]
    anchor_path: tuple[Point, ...]
    spacing: tuple[float, ...]
    total_length: float
    total_bends: int


@dataclass(frozen=True)
class PowerMeshSpec:
    bbox: tuple[int, int, int, int]
    nets: tuple[str, ...] = ("VDD", "VSS")
    horizontal_layer: str = "M1"
    vertical_layer: str = "M2"
    horizontal_pitch: int = 4
    vertical_pitch: int = 4
    min_width_nm: int = 200
    current_ma: Mapping[str, float] | None = None
    current_density_ma_per_um: float = 5.0
    via_current_ma_per_cut: float = 2.0
    min_redundant_straps_per_net: int = 2


@dataclass(frozen=True)
class PowerMeshVia:
    net: str
    point: Point
    lower_layer: str
    upper_layer: str
    cuts: int


@dataclass(frozen=True)
class PowerMeshResult:
    routes: tuple[RoutedNet, ...]
    vias: tuple[PowerMeshVia, ...]
    widths_nm: Mapping[str, int]
    strap_counts: Mapping[str, int]
    issues: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.issues


def route_coupled_differential_pair(
    grid: Grid,
    pos_source: Point,
    pos_target: Point,
    neg_source: Point,
    neg_target: Point,
    *,
    net_names: tuple[str, str] = ("DP", "DN"),
    layer: str = "M1",
    width_nm: int | None = None,
    bend_cost: float = 0.2,
    point_costs: Mapping[Point, float] | None = None,
) -> CoupledRouteResult:
    """Route a differential pair as one rigid two-track object.

    Both wires take identical steps, so spacing, topology, bend count, and path
    length remain matched by construction rather than by post-route padding.
    """

    return route_coupled_bundle(
        grid,
        (pos_source, neg_source),
        (pos_target, neg_target),
        net_names,
        layer=layer,
        width_nm=width_nm,
        bend_cost=bend_cost,
        point_costs=point_costs,
    )


def route_coupled_bus(
    grid: Grid,
    sources: Sequence[Point],
    targets: Sequence[Point],
    net_names: Sequence[str],
    *,
    layer: str = "M2",
    width_nm: int | None = None,
    bend_cost: float = 0.2,
    point_costs: Mapping[Point, float] | None = None,
) -> CoupledRouteResult:
    """Route an ordered bus as a rigid bundle without crossings or lane swaps."""

    return route_coupled_bundle(
        grid,
        sources,
        targets,
        net_names,
        layer=layer,
        width_nm=width_nm,
        bend_cost=bend_cost,
        point_costs=point_costs,
    )


def route_coupled_bundle(
    grid: Grid,
    sources: Sequence[Point],
    targets: Sequence[Point],
    net_names: Sequence[str],
    *,
    layer: str = "M1",
    width_nm: int | None = None,
    bend_cost: float = 0.2,
    point_costs: Mapping[Point, float] | None = None,
) -> CoupledRouteResult:
    if not sources or not (len(sources) == len(targets) == len(net_names)):
        raise ValueError("sources, targets, and net_names must have the same non-zero length")
    if len(set(net_names)) != len(net_names):
        raise ValueError("bundle net names must be unique")
    if not isfinite(bend_cost) or bend_cost < 0.0:
        raise ValueError("bend_cost must be finite and non-negative")

    source_anchor = _point(sources[0])
    target_anchor = _point(targets[0])
    source_offsets = tuple(_subtract(_point(point), source_anchor) for point in sources)
    target_offsets = tuple(_subtract(_point(point), target_anchor) for point in targets)
    if source_offsets != target_offsets:
        raise ValueError("coupled routing requires identical source and target lane offsets")
    _validate_bundle_pose(grid, source_anchor, source_offsets, "source")
    _validate_bundle_pose(grid, target_anchor, source_offsets, "target")

    penalties = {_point(point): float(cost) for point, cost in (point_costs or {}).items()}
    source_state = (source_anchor, None)
    frontier: list[tuple[float, int, tuple[Point, Point | None]]] = [(0.0, 0, source_state)]
    serial = 1
    came_from: dict[tuple[Point, Point | None], tuple[Point, Point | None] | None] = {source_state: None}
    costs: dict[tuple[Point, Point | None], float] = {source_state: 0.0}
    target_state: tuple[Point, Point | None] | None = None

    while frontier:
        _, _, state = heappop(frontier)
        anchor, direction = state
        if anchor == target_anchor:
            target_state = state
            break
        for delta in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)):
            next_anchor = _add(anchor, delta)
            if not _bundle_pose_is_legal(grid, next_anchor, source_offsets):
                continue
            step_cost = float(len(source_offsets))
            if direction is not None and direction != delta:
                step_cost += bend_cost * len(source_offsets)
            step_cost += sum(penalties.get(_add(next_anchor, offset), 0.0) for offset in source_offsets)
            next_state = (next_anchor, delta)
            new_cost = costs[state] + step_cost
            if next_state in costs and new_cost >= costs[next_state] - 1e-12:
                continue
            costs[next_state] = new_cost
            came_from[next_state] = state
            heuristic = len(source_offsets) * (
                abs(target_anchor[0] - next_anchor[0]) + abs(target_anchor[1] - next_anchor[1])
            )
            heappush(frontier, (new_cost + heuristic, serial, next_state))
            serial += 1

    if target_state is None:
        raise ValueError("no coupled bundle route found")
    anchor_path = _reconstruct_anchor_path(came_from, target_state)
    routes = tuple(
        RoutedNet.from_points(
            str(net),
            tuple(_add(anchor, offset) for anchor in anchor_path),
            layer=layer,
            width_nm=width_nm,
        )
        for net, offset in zip(net_names, source_offsets)
    )
    return CoupledRouteResult(
        routes=routes,
        anchor_path=anchor_path,
        spacing=_adjacent_lane_spacings(sources),
        total_length=sum(route_length(route.points) for route in routes),
        total_bends=sum(_bend_count(route.points) for route in routes),
    )


def synthesize_power_mesh(spec: PowerMeshSpec) -> PowerMeshResult:
    """Build an interleaved two-layer power mesh sized from current budgets."""

    x0, y0, x1, y1 = spec.bbox
    if x1 <= x0 or y1 <= y0:
        raise ValueError("power mesh bbox must have positive area")
    if not spec.nets or len(set(spec.nets)) != len(spec.nets):
        raise ValueError("power mesh nets must be non-empty and unique")
    if spec.horizontal_pitch <= 0 or spec.vertical_pitch <= 0:
        raise ValueError("power mesh pitches must be positive")
    if spec.current_density_ma_per_um <= 0.0 or spec.via_current_ma_per_cut <= 0.0:
        raise ValueError("power mesh current capacities must be positive")
    if spec.min_width_nm <= 0 or spec.min_redundant_straps_per_net <= 0:
        raise ValueError("power mesh width and redundancy must be positive")

    horizontal_coords = _mesh_coordinates(y0, y1, spec.horizontal_pitch, len(spec.nets) * spec.min_redundant_straps_per_net)
    vertical_coords = _mesh_coordinates(x0, x1, spec.vertical_pitch, len(spec.nets) * spec.min_redundant_straps_per_net)
    horizontal_by_net = _assign_mesh_coordinates(horizontal_coords, spec.nets)
    vertical_by_net = _assign_mesh_coordinates(vertical_coords, spec.nets)
    strap_counts = {
        net: len(horizontal_by_net.get(net, ())) + len(vertical_by_net.get(net, ()))
        for net in spec.nets
    }
    currents = {net: max(float((spec.current_ma or {}).get(net, 0.0)), 0.0) for net in spec.nets}
    widths_nm = {
        net: max(
            spec.min_width_nm,
            int(ceil(1000.0 * currents[net] / (spec.current_density_ma_per_um * max(strap_counts[net], 1)))),
        )
        for net in spec.nets
    }

    routes: list[RoutedNet] = []
    for net in spec.nets:
        for y in horizontal_by_net.get(net, ()):
            routes.append(RoutedNet.from_points(net, ((x0, y), (x1, y)), layer=spec.horizontal_layer, width_nm=widths_nm[net]))
        for x in vertical_by_net.get(net, ()):
            routes.append(RoutedNet.from_points(net, ((x, y0), (x, y1)), layer=spec.vertical_layer, width_nm=widths_nm[net]))

    vias: list[PowerMeshVia] = []
    for net in spec.nets:
        intersections = max(len(horizontal_by_net.get(net, ())) * len(vertical_by_net.get(net, ())), 1)
        cuts = max(1, int(ceil(currents[net] / (spec.via_current_ma_per_cut * intersections))))
        for y in horizontal_by_net.get(net, ()):
            for x in vertical_by_net.get(net, ()):
                vias.append(PowerMeshVia(net, (float(x), float(y)), spec.horizontal_layer, spec.vertical_layer, cuts))

    issues: list[str] = []
    for net in spec.nets:
        if len(horizontal_by_net.get(net, ())) < spec.min_redundant_straps_per_net:
            issues.append(f"net {net} lacks horizontal strap redundancy")
        if len(vertical_by_net.get(net, ())) < spec.min_redundant_straps_per_net:
            issues.append(f"net {net} lacks vertical strap redundancy")
        if currents[net] > 0.0 and not any(via.net == net for via in vias):
            issues.append(f"net {net} lacks mesh vias")
    return PowerMeshResult(tuple(routes), tuple(vias), widths_nm, strap_counts, tuple(issues))


def _mesh_coordinates(start: int, stop: int, pitch: int, minimum_count: int) -> tuple[int, ...]:
    coords = list(range(start, stop + 1, pitch))
    if coords[-1] != stop:
        coords.append(stop)
    if len(coords) < minimum_count:
        span = stop - start
        coords = [int(round(start + index * span / max(minimum_count - 1, 1))) for index in range(minimum_count)]
    return tuple(dict.fromkeys(coords))


def _assign_mesh_coordinates(coords: Sequence[int], nets: Sequence[str]) -> dict[str, tuple[int, ...]]:
    assigned: dict[str, list[int]] = {net: [] for net in nets}
    for index, coord in enumerate(coords):
        assigned[nets[index % len(nets)]].append(int(coord))
    return {net: tuple(values) for net, values in assigned.items()}


def _reconstruct_anchor_path(
    came_from: Mapping[tuple[Point, Point | None], tuple[Point, Point | None] | None],
    target_state: tuple[Point, Point | None],
) -> tuple[Point, ...]:
    path: list[Point] = []
    state: tuple[Point, Point | None] | None = target_state
    while state is not None:
        path.append(state[0])
        state = came_from[state]
    return tuple(reversed(path))


def _validate_bundle_pose(grid: Grid, anchor: Point, offsets: Sequence[Point], label: str) -> None:
    if not _bundle_pose_is_legal(grid, anchor, offsets):
        raise ValueError(f"bundle {label} pose is outside the grid, blocked, or overlapping")


def _bundle_pose_is_legal(grid: Grid, anchor: Point, offsets: Sequence[Point]) -> bool:
    points = tuple(_add(anchor, offset) for offset in offsets)
    return (
        len(set(points)) == len(points)
        and all(0 <= point[0] < grid.width and 0 <= point[1] < grid.height for point in points)
        and not any(point in grid.obstacles for point in points)
    )


def _adjacent_lane_spacings(points: Sequence[Point]) -> tuple[float, ...]:
    return tuple(hypot(right[0] - left[0], right[1] - left[1]) for left, right in zip(points, points[1:]))


def _bend_count(points: Sequence[Point]) -> int:
    directions = tuple(_subtract(right, left) for left, right in zip(points, points[1:]))
    return sum(left != right for left, right in zip(directions, directions[1:]))


def _point(point: Point) -> Point:
    return (float(point[0]), float(point[1]))


def _add(left: Point, right: Point) -> Point:
    return (left[0] + right[0], left[1] + right[1])


def _subtract(left: Point, right: Point) -> Point:
    return (left[0] - right[0], left[1] - right[1])
