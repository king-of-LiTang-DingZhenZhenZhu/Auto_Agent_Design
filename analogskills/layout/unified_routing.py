"""Unified negotiated routing for scalar nets, differential pairs, and buses."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .routing import Grid, Point, RoutedNet, route_astar_costed, route_length
from .structured_routing import route_coupled_bundle


@dataclass(frozen=True)
class UnifiedRouteGroup:
    name: str
    nets: tuple[str, ...]
    sources: tuple[Point, ...]
    targets: tuple[Point, ...]
    kind: str = "scalar"
    layer: str = "M1"
    priority: int = 10
    width_nm: int | None = None


@dataclass(frozen=True)
class UnifiedRoutingConfig:
    max_iterations: int = 30
    point_capacity: int = 1
    present_congestion_cost: float = 8.0
    history_congestion_cost: float = 2.0
    bend_cost: float = 0.2


@dataclass(frozen=True)
class UnifiedRoutingIteration:
    iteration: int
    overflow_resource_count: int
    total_overflow: int
    max_occupancy: int
    total_length: float
    group_order: tuple[str, ...]


@dataclass(frozen=True)
class UnifiedRoutingResult:
    routes: tuple[RoutedNet, ...]
    converged: bool
    iterations: tuple[UnifiedRoutingIteration, ...]
    unresolved_resources: Mapping[tuple[str, Point], tuple[str, ...]] = field(default_factory=dict)
    fixed_route_count: int = 0


def route_unified_groups(
    grid: Grid,
    groups: Sequence[UnifiedRouteGroup],
    *,
    fixed_routes: Sequence[RoutedNet] = (),
    config: UnifiedRoutingConfig | None = None,
) -> UnifiedRoutingResult:
    cfg = config or UnifiedRoutingConfig()
    ordered = _validate_groups(grid, groups, cfg)
    fixed = tuple(fixed_routes)
    history: dict[tuple[str, Point], float] = {}
    rows: list[UnifiedRoutingIteration] = []
    best_routes: tuple[RoutedNet, ...] = ()
    best_unresolved: dict[tuple[str, Point], tuple[str, ...]] = {}
    best_key: tuple[float, ...] | None = None
    converged = False

    for iteration in range(1, cfg.max_iterations + 1):
        usage = _route_usage(fixed)
        routed: list[RoutedNet] = []
        for group in ordered:
            point_costs = _group_point_costs(grid, group, usage, history, cfg)
            group_routes = _route_group(grid, group, point_costs, cfg)
            routed.extend(group_routes)
            _add_routes_to_usage(usage, group_routes)
        unresolved = _overflow_resources(usage, cfg.point_capacity)
        total_overflow = sum(max(len(owners) - cfg.point_capacity, 0) for owners in unresolved.values())
        row = UnifiedRoutingIteration(
            iteration=iteration,
            overflow_resource_count=len(unresolved),
            total_overflow=total_overflow,
            max_occupancy=max((len(owners) for owners in usage.values()), default=0),
            total_length=sum(route_length(route.points) for route in routed),
            group_order=tuple(group.name for group in ordered),
        )
        rows.append(row)
        key = (float(total_overflow), float(len(unresolved)), row.total_length, float(iteration))
        if best_key is None or key < best_key:
            best_key = key
            best_routes = tuple(routed)
            best_unresolved = unresolved
        if total_overflow == 0:
            converged = True
            break
        for resource, owners in unresolved.items():
            overflow = max(len(owners) - cfg.point_capacity, 0)
            history[resource] = history.get(resource, 0.0) + cfg.history_congestion_cost * overflow

    return UnifiedRoutingResult(
        routes=best_routes,
        converged=converged,
        iterations=tuple(rows),
        unresolved_resources=best_unresolved,
        fixed_route_count=len(fixed),
    )


def _route_group(
    grid: Grid,
    group: UnifiedRouteGroup,
    point_costs: Mapping[Point, float],
    config: UnifiedRoutingConfig,
) -> tuple[RoutedNet, ...]:
    if group.kind == "scalar":
        points = route_astar_costed(
            grid,
            group.sources[0],
            group.targets[0],
            bend_cost=config.bend_cost,
            point_costs=point_costs,
        )
        return (RoutedNet.from_points(group.nets[0], points, layer=group.layer, width_nm=group.width_nm),)
    return route_coupled_bundle(
        grid,
        group.sources,
        group.targets,
        group.nets,
        layer=group.layer,
        width_nm=group.width_nm,
        bend_cost=config.bend_cost,
        point_costs=point_costs,
    ).routes


def _validate_groups(
    grid: Grid,
    groups: Sequence[UnifiedRouteGroup],
    config: UnifiedRoutingConfig,
) -> tuple[UnifiedRouteGroup, ...]:
    if config.max_iterations <= 0 or config.point_capacity <= 0:
        raise ValueError("routing iterations and capacity must be positive")
    valid_kinds = {"scalar", "differential", "bus"}
    seen_groups: set[str] = set()
    seen_nets: set[str] = set()
    rows: list[UnifiedRouteGroup] = []
    for group in groups:
        if not group.name or group.name in seen_groups:
            raise ValueError(f"route group names must be unique: {group.name!r}")
        seen_groups.add(group.name)
        if group.kind not in valid_kinds:
            raise ValueError(f"unsupported route group kind {group.kind}")
        if not group.nets or not (len(group.nets) == len(group.sources) == len(group.targets)):
            raise ValueError(f"route group {group.name} has inconsistent terminals")
        if group.kind == "scalar" and len(group.nets) != 1:
            raise ValueError(f"scalar route group {group.name} must contain one net")
        if group.kind == "differential" and len(group.nets) != 2:
            raise ValueError(f"differential route group {group.name} must contain two nets")
        for net in group.nets:
            if net in seen_nets:
                raise ValueError(f"net {net} belongs to multiple route groups")
            seen_nets.add(net)
        for point in (*group.sources, *group.targets):
            if not (0 <= point[0] < grid.width and 0 <= point[1] < grid.height):
                raise ValueError(f"route group {group.name} terminal {point} is outside the grid")
            if point in grid.obstacles:
                raise ValueError(f"route group {group.name} terminal {point} is blocked")
        rows.append(group)
    return tuple(sorted(rows, key=lambda group: (group.priority, -len(group.nets), group.name)))


def _group_point_costs(
    grid: Grid,
    group: UnifiedRouteGroup,
    usage: Mapping[tuple[str, Point], set[str]],
    history: Mapping[tuple[str, Point], float],
    config: UnifiedRoutingConfig,
) -> dict[Point, float]:
    endpoints = set(group.sources) | set(group.targets)
    points = {point for layer, point in set(usage) | set(history) if layer == group.layer}
    costs: dict[Point, float] = {}
    for point in points:
        if point in endpoints or point in grid.obstacles:
            continue
        resource = (group.layer, point)
        occupancy = len(usage.get(resource, set()))
        present = max(occupancy - config.point_capacity + 1, 0)
        cost = history.get(resource, 0.0) + config.present_congestion_cost * present
        if cost > 0.0:
            costs[point] = cost
    return costs


def _route_usage(routes: Sequence[RoutedNet]) -> dict[tuple[str, Point], set[str]]:
    usage: dict[tuple[str, Point], set[str]] = {}
    _add_routes_to_usage(usage, routes)
    return usage


def _add_routes_to_usage(
    usage: dict[tuple[str, Point], set[str]],
    routes: Sequence[RoutedNet],
) -> None:
    for route in routes:
        for point in _rasterized_route_points(route):
            usage.setdefault((route.layer, point), set()).add(route.net)


def _rasterized_route_points(route: RoutedNet) -> tuple[Point, ...]:
    points: list[Point] = []
    for start, stop in zip(route.points, route.points[1:]):
        dx = stop[0] - start[0]
        dy = stop[1] - start[1]
        if abs(dx) > 1e-12 and abs(dy) > 1e-12:
            raise ValueError(f"route {route.net} contains a non-Manhattan segment")
        steps = int(round(abs(dx) + abs(dy)))
        if steps == 0:
            points.append(start)
            continue
        sx = 0.0 if abs(dx) <= 1e-12 else dx / abs(dx)
        sy = 0.0 if abs(dy) <= 1e-12 else dy / abs(dy)
        points.extend((start[0] + index * sx, start[1] + index * sy) for index in range(steps + 1))
    if len(route.points) == 1:
        points.append(route.points[0])
    return tuple(dict.fromkeys(points))


def _overflow_resources(
    usage: Mapping[tuple[str, Point], set[str]],
    capacity: int,
) -> dict[tuple[str, Point], tuple[str, ...]]:
    return {
        resource: tuple(sorted(owners))
        for resource, owners in sorted(usage.items())
        if len(owners) > capacity
    }
