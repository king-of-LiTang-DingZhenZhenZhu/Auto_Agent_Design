"""Capacity-aware negotiated-congestion routing for multiple two-terminal nets."""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Mapping, Sequence

from .routing import Grid, Point, RoutedNet, route_astar_costed, route_length


@dataclass(frozen=True)
class NegotiatedRouteRequest:
    net: str
    source: Point
    target: Point
    layer: str = "M1"
    priority: int = 10
    width_nm: int | None = None


@dataclass(frozen=True)
class NegotiatedRoutingConfig:
    max_iterations: int = 30
    point_capacity: int = 1
    present_congestion_cost: float = 8.0
    history_congestion_cost: float = 2.0
    bend_cost: float = 0.2
    obstacle_proximity_cost: float = 0.0


@dataclass(frozen=True)
class NegotiatedRoutingIteration:
    iteration: int
    overflow_point_count: int
    total_overflow: int
    max_occupancy: int
    total_length: float
    total_bends: int


@dataclass(frozen=True)
class NegotiatedRoutingResult:
    routes: tuple[RoutedNet, ...]
    converged: bool
    iterations: tuple[NegotiatedRoutingIteration, ...]
    unresolved_points: Mapping[Point, tuple[str, ...]] = field(default_factory=dict)

    @property
    def best_iteration(self) -> NegotiatedRoutingIteration | None:
        if not self.iterations:
            return None
        return min(
            self.iterations,
            key=lambda item: (
                item.total_overflow,
                item.overflow_point_count,
                item.total_length,
                item.total_bends,
                item.iteration,
            ),
        )


def route_negotiated_congestion(
    grid: Grid,
    requests: Sequence[NegotiatedRouteRequest],
    *,
    config: NegotiatedRoutingConfig | None = None,
) -> NegotiatedRoutingResult:
    """Route multiple nets while iteratively penalizing overused grid points.

    Every iteration rips up all signal routes, routes them again in deterministic
    criticality order, and raises the historical price of congested points.  The
    best iteration is returned even when the grid is fundamentally unroutable.
    """

    cfg = config or NegotiatedRoutingConfig()
    ordered = _validate_and_order_requests(grid, requests, cfg)
    if not ordered:
        return NegotiatedRoutingResult((), True, ())

    history_costs: dict[Point, float] = {}
    iteration_rows: list[NegotiatedRoutingIteration] = []
    best_routes: tuple[RoutedNet, ...] = ()
    best_unresolved: dict[Point, tuple[str, ...]] = {}
    best_key: tuple[float, ...] | None = None
    converged = False

    for iteration in range(1, cfg.max_iterations + 1):
        usage: dict[Point, list[str]] = {}
        routed: list[RoutedNet] = []
        for request in ordered:
            point_costs = _negotiated_point_costs(
                grid,
                usage,
                history_costs,
                request,
                cfg,
            )
            points = route_astar_costed(
                grid,
                request.source,
                request.target,
                bend_cost=cfg.bend_cost,
                spacing_cost=cfg.obstacle_proximity_cost,
                point_costs=point_costs,
            )
            route = RoutedNet.from_points(
                request.net,
                points,
                layer=request.layer,
                width_nm=request.width_nm,
            )
            routed.append(route)
            for point in _owned_route_points(route):
                usage.setdefault(point, []).append(request.net)

        unresolved = _overflow_owners(usage, cfg.point_capacity)
        row = _iteration_metrics(iteration, routed, usage, cfg.point_capacity)
        iteration_rows.append(row)
        key = (
            float(row.total_overflow),
            float(row.overflow_point_count),
            row.total_length,
            float(row.total_bends),
            float(iteration),
        )
        if best_key is None or key < best_key:
            best_key = key
            best_routes = tuple(routed)
            best_unresolved = unresolved
        if row.total_overflow == 0:
            converged = True
            break
        for point, owners in unresolved.items():
            overflow = max(len(owners) - cfg.point_capacity, 0)
            history_costs[point] = history_costs.get(point, 0.0) + cfg.history_congestion_cost * overflow

    return NegotiatedRoutingResult(
        routes=best_routes,
        converged=converged,
        iterations=tuple(iteration_rows),
        unresolved_points=best_unresolved,
    )


def _validate_and_order_requests(
    grid: Grid,
    requests: Sequence[NegotiatedRouteRequest],
    config: NegotiatedRoutingConfig,
) -> tuple[NegotiatedRouteRequest, ...]:
    if grid.width <= 0 or grid.height <= 0:
        raise ValueError("grid dimensions must be positive")
    if config.max_iterations <= 0:
        raise ValueError("max_iterations must be positive")
    if config.point_capacity <= 0:
        raise ValueError("point_capacity must be positive")
    for value, name in (
        (config.present_congestion_cost, "present_congestion_cost"),
        (config.history_congestion_cost, "history_congestion_cost"),
        (config.bend_cost, "bend_cost"),
        (config.obstacle_proximity_cost, "obstacle_proximity_cost"),
    ):
        if not isfinite(value) or value < 0.0:
            raise ValueError(f"{name} must be finite and non-negative")

    seen: set[str] = set()
    validated: list[NegotiatedRouteRequest] = []
    for request in requests:
        if not request.net:
            raise ValueError("route request net must not be empty")
        if request.net in seen:
            raise ValueError(f"duplicate route request for net {request.net}")
        seen.add(request.net)
        for label, point in (("source", request.source), ("target", request.target)):
            if not _point_in_grid(grid, point):
                raise ValueError(f"{request.net} {label} {point} is outside the routing grid")
            if point in grid.obstacles:
                raise ValueError(f"{request.net} {label} {point} is blocked")
        validated.append(request)
    return tuple(
        sorted(
            validated,
            key=lambda item: (
                item.priority,
                -(abs(item.target[0] - item.source[0]) + abs(item.target[1] - item.source[1])),
                item.net,
            ),
        )
    )


def _negotiated_point_costs(
    grid: Grid,
    usage: Mapping[Point, Sequence[str]],
    history_costs: Mapping[Point, float],
    request: NegotiatedRouteRequest,
    config: NegotiatedRoutingConfig,
) -> dict[Point, float]:
    costs: dict[Point, float] = {}
    points = set(history_costs) | set(usage)
    for point in points:
        if point in {request.source, request.target} or point in grid.obstacles:
            continue
        occupancy = len(usage.get(point, ()))
        present_overflow = max(occupancy - config.point_capacity + 1, 0)
        cost = history_costs.get(point, 0.0) + config.present_congestion_cost * present_overflow
        if cost > 0.0:
            costs[point] = cost
    return costs


def _owned_route_points(route: RoutedNet) -> tuple[Point, ...]:
    # Endpoints are resources too.  Different nets sharing a pin must be modeled
    # as one electrical net before entering this router.
    return tuple(dict.fromkeys(route.points))


def _overflow_owners(
    usage: Mapping[Point, Sequence[str]],
    capacity: int,
) -> dict[Point, tuple[str, ...]]:
    return {
        point: tuple(owners)
        for point, owners in sorted(usage.items())
        if len(owners) > capacity
    }


def _iteration_metrics(
    iteration: int,
    routes: Sequence[RoutedNet],
    usage: Mapping[Point, Sequence[str]],
    capacity: int,
) -> NegotiatedRoutingIteration:
    occupancies = tuple(len(owners) for owners in usage.values())
    return NegotiatedRoutingIteration(
        iteration=iteration,
        overflow_point_count=sum(count > capacity for count in occupancies),
        total_overflow=sum(max(count - capacity, 0) for count in occupancies),
        max_occupancy=max(occupancies, default=0),
        total_length=sum(route_length(route.points) for route in routes),
        total_bends=sum(_route_bend_count(route.points) for route in routes),
    )


def _route_bend_count(points: Sequence[Point]) -> int:
    directions = [
        (right[0] - left[0], right[1] - left[1])
        for left, right in zip(points, points[1:])
    ]
    return sum(left != right for left, right in zip(directions, directions[1:]))


def _point_in_grid(grid: Grid, point: Point) -> bool:
    return 0 <= point[0] < grid.width and 0 <= point[1] < grid.height
