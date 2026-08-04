"""Hierarchical placement/critical-routing master with routing feedback cuts."""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Mapping, Sequence

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None


@dataclass(frozen=True)
class HierarchicalPhysicalGroup:
    name: str
    width_tracks: int
    height_tracks: int = 1


@dataclass(frozen=True)
class HierarchicalRoutingCorridor:
    name: str
    source_group: str
    target_group: str
    base_capacity_tracks: int = 0
    estimated_noncritical_tracks: int = 0
    fixed_reserved_tracks: int = 0
    pitch_sites: int = 1


@dataclass(frozen=True)
class HierarchicalRouteCandidate:
    name: str
    corridors: tuple[str, ...]
    cost: int = 0


@dataclass(frozen=True)
class HierarchicalRouteDemand:
    name: str
    track_demand: int
    candidates: tuple[HierarchicalRouteCandidate, ...]
    critical: bool = False


@dataclass(frozen=True)
class HierarchicalPhysicalProblem:
    groups: tuple[HierarchicalPhysicalGroup, ...]
    corridors: tuple[HierarchicalRoutingCorridor, ...]
    critical_routes: tuple[HierarchicalRouteDemand, ...] = ()
    noncritical_routes: tuple[HierarchicalRouteDemand, ...] = ()
    max_refinement_iterations: int = 8


@dataclass(frozen=True)
class HierarchicalMasterSolution:
    group_x_tracks: Mapping[str, int]
    corridor_capacity_tracks: Mapping[str, int]
    critical_candidate_by_route: Mapping[str, str]
    critical_load_by_corridor: Mapping[str, int]
    total_width_tracks: int


@dataclass(frozen=True)
class HierarchicalRoutingSubproblemResult:
    candidate_by_route: Mapping[str, str]
    actual_load_by_corridor: Mapping[str, int]
    overflow_by_corridor: Mapping[str, int]

    @property
    def passed(self) -> bool:
        return not self.overflow_by_corridor


@dataclass(frozen=True)
class HierarchicalRefinementIteration:
    iteration: int
    master: HierarchicalMasterSolution
    routing: HierarchicalRoutingSubproblemResult
    capacity_cuts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class HierarchicalPhysicalSolution:
    master: HierarchicalMasterSolution
    routing: HierarchicalRoutingSubproblemResult
    iterations: tuple[HierarchicalRefinementIteration, ...]
    converged: bool


def solve_hierarchical_physical_problem(
    problem: HierarchicalPhysicalProblem,
) -> HierarchicalPhysicalSolution:
    """Alternate an SMT master with a non-critical routing subproblem."""

    _validate_problem(problem)
    capacity_cuts: dict[str, int] = {}
    rows: list[HierarchicalRefinementIteration] = []
    last_master: HierarchicalMasterSolution | None = None
    last_routing: HierarchicalRoutingSubproblemResult | None = None
    for iteration in range(1, problem.max_refinement_iterations + 1):
        master = _solve_master(problem, capacity_cuts)
        routing = _solve_noncritical_subproblem(problem, master)
        cuts = {
            corridor: max(master.corridor_capacity_tracks[corridor] + overflow, capacity_cuts.get(corridor, 0))
            for corridor, overflow in routing.overflow_by_corridor.items()
        }
        rows.append(HierarchicalRefinementIteration(iteration, master, routing, cuts))
        last_master = master
        last_routing = routing
        if routing.passed:
            return HierarchicalPhysicalSolution(master, routing, tuple(rows), True)
        changed = False
        for corridor, required in cuts.items():
            if required > capacity_cuts.get(corridor, 0):
                capacity_cuts[corridor] = required
                changed = True
        if not changed:
            break
    assert last_master is not None and last_routing is not None
    return HierarchicalPhysicalSolution(last_master, last_routing, tuple(rows), False)


def _solve_master(
    problem: HierarchicalPhysicalProblem,
    capacity_cuts: Mapping[str, int],
) -> HierarchicalMasterSolution:
    if z3 is None:  # pragma: no cover
        raise RuntimeError("z3-solver is required for hierarchical SMT placement")
    optimizer = z3.Optimize()
    group_by_name = {group.name: group for group in problem.groups}
    corridor_by_name = {corridor.name: corridor for corridor in problem.corridors}
    x_vars = {group.name: z3.Int(f"group_x__{group.name}") for group in problem.groups}
    capacity_vars = {corridor.name: z3.Int(f"corridor_capacity__{corridor.name}") for corridor in problem.corridors}
    optimizer.add(x_vars[problem.groups[0].name] == 0)
    for group in problem.groups:
        optimizer.add(x_vars[group.name] >= 0)
    for corridor in problem.corridors:
        capacity = capacity_vars[corridor.name]
        minimum = max(
            corridor.base_capacity_tracks,
            corridor.estimated_noncritical_tracks + corridor.fixed_reserved_tracks,
            int(capacity_cuts.get(corridor.name, 0)),
        )
        optimizer.add(capacity >= minimum)
        source = group_by_name[corridor.source_group]
        optimizer.add(
            x_vars[corridor.target_group]
            >= x_vars[corridor.source_group] + source.width_tracks + capacity * corridor.pitch_sites
        )

    choice_vars: dict[tuple[str, str], object] = {}
    for route in problem.critical_routes:
        choices = []
        for candidate in route.candidates:
            var = z3.Bool(f"critical_choice__{route.name}__{candidate.name}")
            choice_vars[(route.name, candidate.name)] = var
            choices.append(var)
        optimizer.add(z3.PbEq([(choice, 1) for choice in choices], 1))

    for corridor in problem.corridors:
        critical_terms = []
        for route in problem.critical_routes:
            for candidate in route.candidates:
                if corridor.name in candidate.corridors:
                    critical_terms.append(z3.If(choice_vars[(route.name, candidate.name)], route.track_demand, 0))
        critical_load = z3.Sum(critical_terms) if critical_terms else z3.IntVal(0)
        optimizer.add(
            capacity_vars[corridor.name]
            >= critical_load + corridor.estimated_noncritical_tracks + corridor.fixed_reserved_tracks
        )

    right_edges = [x_vars[group.name] + group.width_tracks for group in problem.groups]
    total_width = z3.Int("total_layout_width")
    for edge in right_edges:
        optimizer.add(total_width >= edge)
    route_cost_terms = [
        z3.If(choice_vars[(route.name, candidate.name)], candidate.cost, 0)
        for route in problem.critical_routes
        for candidate in route.candidates
    ]
    optimizer.minimize(total_width)
    optimizer.minimize(z3.Sum(route_cost_terms) if route_cost_terms else z3.IntVal(0))
    optimizer.minimize(z3.Sum(tuple(capacity_vars.values())) if capacity_vars else z3.IntVal(0))
    if optimizer.check() != z3.sat:
        raise ValueError("hierarchical physical master problem is unsatisfiable")
    model = optimizer.model()
    x_values = {name: model.eval(var, model_completion=True).as_long() for name, var in x_vars.items()}
    capacity_values = {name: model.eval(var, model_completion=True).as_long() for name, var in capacity_vars.items()}
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
        candidate = next(candidate for candidate in route.candidates if candidate.name == selected[route.name])
        for corridor in candidate.corridors:
            critical_load[corridor] += route.track_demand
    return HierarchicalMasterSolution(
        x_values,
        capacity_values,
        selected,
        critical_load,
        model.eval(total_width, model_completion=True).as_long(),
    )


def _solve_noncritical_subproblem(
    problem: HierarchicalPhysicalProblem,
    master: HierarchicalMasterSolution,
) -> HierarchicalRoutingSubproblemResult:
    loads = dict(master.critical_load_by_corridor)
    for corridor in problem.corridors:
        loads[corridor.name] = loads.get(corridor.name, 0) + corridor.fixed_reserved_tracks
    selected: dict[str, str] = {}
    ordered_routes = sorted(problem.noncritical_routes, key=lambda route: (-route.track_demand, route.name))
    for route in ordered_routes:
        candidate = min(
            route.candidates,
            key=lambda item: (
                _candidate_overflow_cost(item, route.track_demand, loads, master.corridor_capacity_tracks),
                item.cost,
                len(item.corridors),
                item.name,
            ),
        )
        selected[route.name] = candidate.name
        for corridor in candidate.corridors:
            loads[corridor] = loads.get(corridor, 0) + route.track_demand
    overflow = {
        corridor: load - master.corridor_capacity_tracks[corridor]
        for corridor, load in loads.items()
        if load > master.corridor_capacity_tracks[corridor]
    }
    return HierarchicalRoutingSubproblemResult(selected, loads, overflow)


def _candidate_overflow_cost(
    candidate: HierarchicalRouteCandidate,
    demand: int,
    loads: Mapping[str, int],
    capacities: Mapping[str, int],
) -> int:
    return sum(max(loads.get(corridor, 0) + demand - capacities[corridor], 0) for corridor in candidate.corridors)


def _validate_problem(problem: HierarchicalPhysicalProblem) -> None:
    if not problem.groups or problem.max_refinement_iterations <= 0:
        raise ValueError("hierarchical problem requires groups and positive iterations")
    group_names = tuple(group.name for group in problem.groups)
    if len(set(group_names)) != len(group_names):
        raise ValueError("physical group names must be unique")
    if any(group.width_tracks <= 0 or group.height_tracks <= 0 for group in problem.groups):
        raise ValueError("physical group dimensions must be positive")
    corridor_names = tuple(corridor.name for corridor in problem.corridors)
    if len(set(corridor_names)) != len(corridor_names):
        raise ValueError("routing corridor names must be unique")
    known_groups = set(group_names)
    for corridor in problem.corridors:
        if corridor.source_group not in known_groups or corridor.target_group not in known_groups:
            raise ValueError(f"corridor {corridor.name} references an unknown group")
        if min(corridor.base_capacity_tracks, corridor.estimated_noncritical_tracks, corridor.fixed_reserved_tracks) < 0:
            raise ValueError(f"corridor {corridor.name} capacities must be non-negative")
    known_corridors = set(corridor_names)
    route_names: set[str] = set()
    for route in (*problem.critical_routes, *problem.noncritical_routes):
        if not route.name or route.name in route_names or route.track_demand <= 0 or not route.candidates:
            raise ValueError(f"invalid or duplicate route demand {route.name!r}")
        route_names.add(route.name)
        for candidate in route.candidates:
            if not candidate.corridors or any(corridor not in known_corridors for corridor in candidate.corridors):
                raise ValueError(f"route candidate {candidate.name} references an unknown corridor")
