"""Two-dimensional hierarchical SMT placement with routing feedback cuts."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None

from .hierarchical_smt import HierarchicalRouteCandidate, HierarchicalRouteDemand


@dataclass(frozen=True)
class HierarchicalPhysicalGroup2D:
    name: str
    width_tracks: int
    height_tracks: int
    allow_rotate: bool = False


@dataclass(frozen=True)
class HierarchicalRoutingCorridor2D:
    name: str
    source_group: str
    target_group: str
    orientation: str
    base_capacity_tracks: int = 0
    estimated_noncritical_tracks: int = 0
    fixed_reserved_tracks: int = 0
    pitch_sites: int = 1
    require_orthogonal_overlap: bool = True
    capacity_consumes_gap: bool = True
    channel_gap_sites: int = 0


@dataclass(frozen=True)
class HierarchicalPhysicalProblem2D:
    groups: tuple[HierarchicalPhysicalGroup2D, ...]
    corridors: tuple[HierarchicalRoutingCorridor2D, ...]
    critical_routes: tuple[HierarchicalRouteDemand, ...] = ()
    noncritical_routes: tuple[HierarchicalRouteDemand, ...] = ()
    placement_spacing_tracks: int = 0
    target_aspect_num: int = 1
    target_aspect_den: int = 1
    max_refinement_iterations: int = 8
    rule_metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class HierarchicalGroupPlacement2D:
    name: str
    x_tracks: int
    y_tracks: int
    width_tracks: int
    height_tracks: int
    rotated: bool = False

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (
            self.x_tracks,
            self.y_tracks,
            self.x_tracks + self.width_tracks,
            self.y_tracks + self.height_tracks,
        )


@dataclass(frozen=True)
class HierarchicalMasterSolution2D:
    placements: Mapping[str, HierarchicalGroupPlacement2D]
    corridor_capacity_tracks: Mapping[str, int]
    corridor_bboxes: Mapping[str, tuple[int, int, int, int]]
    critical_candidate_by_route: Mapping[str, str]
    critical_load_by_corridor: Mapping[str, int]
    total_width_tracks: int
    total_height_tracks: int


@dataclass(frozen=True)
class HierarchicalRoutingSubproblemResult2D:
    candidate_by_route: Mapping[str, str]
    actual_load_by_corridor: Mapping[str, int]
    overflow_by_corridor: Mapping[str, int]

    @property
    def passed(self) -> bool:
        return not self.overflow_by_corridor


@dataclass(frozen=True)
class HierarchicalRefinementIteration2D:
    iteration: int
    master: HierarchicalMasterSolution2D
    routing: HierarchicalRoutingSubproblemResult2D
    capacity_cuts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class HierarchicalPhysicalSolution2D:
    master: HierarchicalMasterSolution2D
    routing: HierarchicalRoutingSubproblemResult2D
    iterations: tuple[HierarchicalRefinementIteration2D, ...]
    converged: bool


def solve_hierarchical_physical_problem_2d(
    problem: HierarchicalPhysicalProblem2D,
) -> HierarchicalPhysicalSolution2D:
    _validate_problem_2d(problem)
    cuts: dict[str, int] = {}
    rows: list[HierarchicalRefinementIteration2D] = []
    last_master: HierarchicalMasterSolution2D | None = None
    last_routing: HierarchicalRoutingSubproblemResult2D | None = None
    for iteration in range(1, problem.max_refinement_iterations + 1):
        master = _solve_master_2d(problem, cuts)
        routing = _solve_subproblem_2d(problem, master)
        next_cuts = {
            name: max(master.corridor_capacity_tracks[name] + overflow, cuts.get(name, 0))
            for name, overflow in routing.overflow_by_corridor.items()
        }
        rows.append(HierarchicalRefinementIteration2D(iteration, master, routing, next_cuts))
        last_master, last_routing = master, routing
        if routing.passed:
            return HierarchicalPhysicalSolution2D(master, routing, tuple(rows), True)
        changed = False
        for name, required in next_cuts.items():
            if required > cuts.get(name, 0):
                cuts[name] = required
                changed = True
        if not changed:
            break
    assert last_master is not None and last_routing is not None
    return HierarchicalPhysicalSolution2D(last_master, last_routing, tuple(rows), False)


def _solve_master_2d(
    problem: HierarchicalPhysicalProblem2D,
    cuts: Mapping[str, int],
) -> HierarchicalMasterSolution2D:
    if z3 is None:  # pragma: no cover
        raise RuntimeError("z3-solver is required for hierarchical 2D SMT placement")
    opt = z3.Optimize()
    group_by_name = {group.name: group for group in problem.groups}
    x = {group.name: z3.Int(f"g2_x__{group.name}") for group in problem.groups}
    y = {group.name: z3.Int(f"g2_y__{group.name}") for group in problem.groups}
    rotated = {group.name: z3.Bool(f"g2_rot__{group.name}") for group in problem.groups}
    width = {
        group.name: z3.If(rotated[group.name], group.height_tracks, group.width_tracks)
        for group in problem.groups
    }
    height = {
        group.name: z3.If(rotated[group.name], group.width_tracks, group.height_tracks)
        for group in problem.groups
    }
    for group in problem.groups:
        opt.add(x[group.name] >= 0, y[group.name] >= 0)
        if not group.allow_rotate:
            opt.add(rotated[group.name] == z3.BoolVal(False))

    spacing = problem.placement_spacing_tracks
    for index, left in enumerate(problem.groups):
        for right in problem.groups[index + 1:]:
            opt.add(
                z3.Or(
                    x[left.name] + width[left.name] + spacing <= x[right.name],
                    x[right.name] + width[right.name] + spacing <= x[left.name],
                    y[left.name] + height[left.name] + spacing <= y[right.name],
                    y[right.name] + height[right.name] + spacing <= y[left.name],
                )
            )

    capacity = {corridor.name: z3.Int(f"g2_cap__{corridor.name}") for corridor in problem.corridors}
    for corridor in problem.corridors:
        minimum = max(
            corridor.base_capacity_tracks,
            corridor.estimated_noncritical_tracks + corridor.fixed_reserved_tracks,
            int(cuts.get(corridor.name, 0)),
        )
        opt.add(capacity[corridor.name] >= minimum)
        source, target = corridor.source_group, corridor.target_group
        capacity_span = capacity[corridor.name] * corridor.pitch_sites
        gap = capacity_span if corridor.capacity_consumes_gap else corridor.channel_gap_sites
        if corridor.orientation == "horizontal":
            opt.add(x[target] >= x[source] + width[source] + gap)
            if corridor.require_orthogonal_overlap:
                opt.add(y[source] < y[target] + height[target], y[target] < y[source] + height[source])
                if not corridor.capacity_consumes_gap:
                    opt.add(
                        y[source] + height[source] >= y[target] + capacity_span,
                        y[target] + height[target] >= y[source] + capacity_span,
                    )
        else:
            opt.add(y[target] >= y[source] + height[source] + gap)
            if corridor.require_orthogonal_overlap:
                opt.add(x[source] < x[target] + width[target], x[target] < x[source] + width[source])
                if not corridor.capacity_consumes_gap:
                    opt.add(
                        x[source] + width[source] >= x[target] + capacity_span,
                        x[target] + width[target] >= x[source] + capacity_span,
                    )

    choice_vars: dict[tuple[str, str], object] = {}
    for route in problem.critical_routes:
        route_vars = []
        for candidate in route.candidates:
            var = z3.Bool(f"g2_choice__{route.name}__{candidate.name}")
            choice_vars[(route.name, candidate.name)] = var
            route_vars.append(var)
        opt.add(z3.PbEq([(var, 1) for var in route_vars], 1))
    for corridor in problem.corridors:
        terms = [
            z3.If(choice_vars[(route.name, candidate.name)], route.track_demand, 0)
            for route in problem.critical_routes
            for candidate in route.candidates
            if corridor.name in candidate.corridors
        ]
        critical_load = z3.Sum(terms) if terms else z3.IntVal(0)
        opt.add(
            capacity[corridor.name]
            >= critical_load + corridor.estimated_noncritical_tracks + corridor.fixed_reserved_tracks
        )

    total_width = z3.Int("g2_total_width")
    total_height = z3.Int("g2_total_height")
    for group in problem.groups:
        opt.add(total_width >= x[group.name] + width[group.name])
        opt.add(total_height >= y[group.name] + height[group.name])
    aspect_error = z3.Int("g2_aspect_error")
    aspect_delta = total_width * problem.target_aspect_den - total_height * problem.target_aspect_num
    opt.add(aspect_error >= aspect_delta, aspect_error >= -aspect_delta)
    route_cost = z3.Sum(
        [
            z3.If(choice_vars[(route.name, candidate.name)], candidate.cost, 0)
            for route in problem.critical_routes
            for candidate in route.candidates
        ]
    ) if problem.critical_routes else z3.IntVal(0)
    opt.minimize(total_width + total_height)
    opt.minimize(aspect_error)
    opt.minimize(route_cost)
    opt.minimize(z3.Sum(tuple(capacity.values())) if capacity else z3.IntVal(0))
    if opt.check() != z3.sat:
        raise ValueError("hierarchical 2D physical master problem is unsatisfiable")
    model = opt.model()
    placements: dict[str, HierarchicalGroupPlacement2D] = {}
    for group in problem.groups:
        is_rotated = z3.is_true(model.eval(rotated[group.name], model_completion=True))
        placements[group.name] = HierarchicalGroupPlacement2D(
            group.name,
            model.eval(x[group.name], model_completion=True).as_long(),
            model.eval(y[group.name], model_completion=True).as_long(),
            group.height_tracks if is_rotated else group.width_tracks,
            group.width_tracks if is_rotated else group.height_tracks,
            is_rotated,
        )
    capacities = {name: model.eval(var, model_completion=True).as_long() for name, var in capacity.items()}
    selected = {
        route.name: next(
            candidate.name
            for candidate in route.candidates
            if z3.is_true(model.eval(choice_vars[(route.name, candidate.name)], model_completion=True))
        )
        for route in problem.critical_routes
    }
    critical_load = {corridor.name: 0 for corridor in problem.corridors}
    for route in problem.critical_routes:
        chosen = next(candidate for candidate in route.candidates if candidate.name == selected[route.name])
        for name in chosen.corridors:
            critical_load[name] += route.track_demand
    corridor_bboxes = {
        corridor.name: _corridor_bbox(corridor, placements, capacities[corridor.name])
        for corridor in problem.corridors
    }
    return HierarchicalMasterSolution2D(
        placements,
        capacities,
        corridor_bboxes,
        selected,
        critical_load,
        model.eval(total_width, model_completion=True).as_long(),
        model.eval(total_height, model_completion=True).as_long(),
    )


def _solve_subproblem_2d(
    problem: HierarchicalPhysicalProblem2D,
    master: HierarchicalMasterSolution2D,
) -> HierarchicalRoutingSubproblemResult2D:
    loads = dict(master.critical_load_by_corridor)
    for corridor in problem.corridors:
        loads[corridor.name] = loads.get(corridor.name, 0) + corridor.fixed_reserved_tracks
    selected: dict[str, str] = {}
    for route in sorted(problem.noncritical_routes, key=lambda item: (-item.track_demand, item.name)):
        candidate = min(
            route.candidates,
            key=lambda item: (
                sum(
                    max(loads.get(name, 0) + route.track_demand - master.corridor_capacity_tracks[name], 0)
                    for name in item.corridors
                ),
                item.cost,
                len(item.corridors),
                item.name,
            ),
        )
        selected[route.name] = candidate.name
        for name in candidate.corridors:
            loads[name] = loads.get(name, 0) + route.track_demand
    overflow = {
        name: load - master.corridor_capacity_tracks[name]
        for name, load in loads.items()
        if load > master.corridor_capacity_tracks[name]
    }
    return HierarchicalRoutingSubproblemResult2D(selected, loads, overflow)


def _corridor_bbox(
    corridor: HierarchicalRoutingCorridor2D,
    placements: Mapping[str, HierarchicalGroupPlacement2D],
    capacity: int,
) -> tuple[int, int, int, int]:
    source = placements[corridor.source_group]
    target = placements[corridor.target_group]
    if corridor.orientation == "horizontal":
        x0, x1 = source.bbox[2], target.bbox[0]
        y0 = max(source.bbox[1], target.bbox[1])
        return (x0, y0, x1, y0 + capacity * corridor.pitch_sites)
    y0, y1 = source.bbox[3], target.bbox[1]
    x0 = max(source.bbox[0], target.bbox[0])
    return (x0, y0, x0 + capacity * corridor.pitch_sites, y1)


def _validate_problem_2d(problem: HierarchicalPhysicalProblem2D) -> None:
    if not problem.groups or problem.max_refinement_iterations <= 0:
        raise ValueError("2D hierarchical problem requires groups and positive iterations")
    if problem.placement_spacing_tracks < 0 or min(problem.target_aspect_num, problem.target_aspect_den) <= 0:
        raise ValueError("2D spacing and target aspect must be valid")
    names = tuple(group.name for group in problem.groups)
    if len(set(names)) != len(names):
        raise ValueError("2D physical group names must be unique")
    if any(min(group.width_tracks, group.height_tracks) <= 0 for group in problem.groups):
        raise ValueError("2D physical group dimensions must be positive")
    known_groups = set(names)
    corridor_names = tuple(corridor.name for corridor in problem.corridors)
    if len(set(corridor_names)) != len(corridor_names):
        raise ValueError("2D corridor names must be unique")
    for corridor in problem.corridors:
        if corridor.orientation not in {"horizontal", "vertical"}:
            raise ValueError(f"corridor {corridor.name} has invalid orientation")
        if corridor.source_group not in known_groups or corridor.target_group not in known_groups:
            raise ValueError(f"corridor {corridor.name} references an unknown group")
        if corridor.pitch_sites <= 0 or corridor.channel_gap_sites < 0 or min(corridor.base_capacity_tracks, corridor.estimated_noncritical_tracks, corridor.fixed_reserved_tracks) < 0:
            raise ValueError(f"corridor {corridor.name} has invalid capacity")
    known_corridors = set(corridor_names)
    route_names: set[str] = set()
    for route in (*problem.critical_routes, *problem.noncritical_routes):
        if not route.name or route.name in route_names or route.track_demand <= 0 or not route.candidates:
            raise ValueError(f"invalid or duplicate 2D route demand {route.name!r}")
        route_names.add(route.name)
        for candidate in route.candidates:
            if not candidate.corridors or any(name not in known_corridors for name in candidate.corridors):
                raise ValueError(f"2D route candidate {candidate.name} references an unknown corridor")
