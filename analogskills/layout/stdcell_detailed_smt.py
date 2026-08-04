"""Detailed-route SMT problem builder for native standard-cell local regions."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import product
from typing import TYPE_CHECKING, Mapping

from analogskills.contracts import TopologyGraph
from analogskills.layout.physical import BBox, bbox_overlaps
from analogskills.layout.stdcell_local_route import (
    NativeStdCellAccessCandidate,
    enumerate_native_stdcell_gate_access_candidates,
    enumerate_native_stdcell_sd_access_candidates,
)
from analogskills.layout.stdcell_route_templates import NativeStdCellRouteTemplateSet, build_native_stdcell_route_templates

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None

if TYPE_CHECKING:
    from analogskills.pdk import PdkConfig
    from .stdcell_primitives import NativeStdCellAccessCatalog, NativeStdCellFloorplan


@dataclass(frozen=True)
class NativeStdCellDetailedRouteAnchor:
    net: str
    role: str
    instance: str = ""
    terminal: str = ""
    default_xy: tuple[float, float] = (0.0, 0.0)
    candidates: tuple[NativeStdCellAccessCandidate, ...] = ()
    fixed: bool = False


@dataclass(frozen=True)
class NativeStdCellDetailedRouteNet:
    net: str
    role: str
    route_layer: str
    trunk_y_candidates: tuple[float, ...] = ()
    trunk_x_candidates: tuple[float, ...] = ()
    bridge_x_candidates: tuple[float | None, ...] = ()
    color_candidates: tuple[str, ...] = ()
    color_roles: tuple[str, ...] = ()
    anchors: tuple[NativeStdCellDetailedRouteAnchor, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeStdCellDetailedRouteProblem:
    graph_name: str
    nets: tuple[NativeStdCellDetailedRouteNet, ...]
    route_templates: NativeStdCellRouteTemplateSet
    scoped_nets: tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def net_map(self) -> dict[str, NativeStdCellDetailedRouteNet]:
        return {item.net: item for item in self.nets}


@dataclass(frozen=True)
class NativeStdCellDetailedRouteSolution:
    access_choices: tuple[tuple[str, str, str, int], ...]
    trunk_x_choices: tuple[tuple[str, float], ...]
    trunk_y_choices: tuple[tuple[str, float], ...]
    cost: float
    bridge_x_choices: tuple[tuple[str, float | None], ...] = ()
    color_choices: tuple[tuple[str, str], ...] = ()
    segment_color_choices: tuple[tuple[str, str, str], ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def access_choice_map(self) -> dict[tuple[str, str, str], int]:
        return {(inst, term, net): int(index) for inst, term, net, index in self.access_choices}

    def trunk_x_map(self) -> dict[str, float]:
        return {net: float(value) for net, value in self.trunk_x_choices}

    def trunk_y_map(self) -> dict[str, float]:
        return {net: float(value) for net, value in self.trunk_y_choices}

    def bridge_x_map(self) -> dict[str, float]:
        return {net: float(value) for net, value in self.bridge_x_choices if value is not None}

    def color_map(self) -> dict[str, str]:
        result = {net: str(value) for net, value in self.color_choices}
        for net, _role, value in self.segment_color_choices:
            result.setdefault(str(net), str(value))
        return result

    def segment_color_map(self) -> dict[tuple[str, str], str]:
        return {(net, role): str(value) for net, role, value in self.segment_color_choices}


@dataclass(frozen=True)
class NativeStdCellDetailedRouteSolveStats:
    backend: str
    sat: bool
    anchor_variables: int
    trunk_variables: int
    color_variables: int
    pair_conflict_pairs: int
    trunk_conflict_pairs: int
    solver_checks: int = 0


@dataclass(frozen=True)
class NativeStdCellDetailedRouteSolveResult:
    problem: NativeStdCellDetailedRouteProblem
    solution: NativeStdCellDetailedRouteSolution | None
    stats: NativeStdCellDetailedRouteSolveStats


@dataclass(frozen=True)
class _RouteStateOption:
    net: str
    access_choices: tuple[tuple[str, str, str, int], ...]
    trunk_x_index: int = 0
    trunk_y_index: int = 0
    bridge_x_index: int = 0
    cost: int = 0
    shapes: tuple[tuple[str, BBox, str], ...] = ()


def build_native_stdcell_detailed_route_problem(
    graph: TopologyGraph,
    floorplan: "NativeStdCellFloorplan",
    access_catalog: "NativeStdCellAccessCatalog",
    pdk: "PdkConfig",
    *,
    scoped_nets: tuple[str, ...] | None = None,
) -> NativeStdCellDetailedRouteProblem:
    route_templates = build_native_stdcell_route_templates(graph, floorplan, access_catalog, pdk)
    placement_by_device = {str(placement.name): placement for placement in floorplan.placements}
    if scoped_nets is None:
        topology = route_templates.topology
        scoped_nets = tuple(
            net
            for net in (
                *getattr(topology, "input_nets", ()),
                getattr(topology, "internal_net", None),
                getattr(topology, "output_net", None),
                "VDD",
                "VSS",
            )
            if isinstance(net, str) and net in graph.nets
        )
    net_problems: list[NativeStdCellDetailedRouteNet] = []

    for input_template in route_templates.input_templates:
        if input_template.net not in scoped_nets:
            continue
        input_anchors: list[NativeStdCellDetailedRouteAnchor] = [
            NativeStdCellDetailedRouteAnchor(
                net=input_template.net,
                role="boundary_pin",
                default_xy=input_template.pin_xy,
                fixed=True,
            )
        ]
        for gate_access in input_template.gate_accesses:
            input_anchors.append(
                _gate_anchor_with_candidates(
                    access_catalog,
                    pdk,
                    instance=gate_access.instance,
                    terminal=gate_access.terminal,
                    net=input_template.net,
                    default_xy=gate_access.default_xy,
                    target_y=input_template.contact_xy[1],
                )
            )
        net_problems.append(
            NativeStdCellDetailedRouteNet(
                net=input_template.net,
                role="input",
                route_layer=input_template.collector_layers[-1],
                trunk_x_candidates=(
                    (input_template.contact_xy[0],)
                    if _is_n7_pdk(pdk)
                    else (input_template.contact_x_candidates or (input_template.contact_xy[0],))
                ),
                trunk_y_candidates=(input_template.contact_xy[1],),
                color_candidates=_color_candidates_for_layer(input_template.collector_layers[-1], pdk),
                color_roles=_color_roles_for_net_role("input"),
                anchors=tuple(input_anchors),
                metadata={
                    "template": "gate_collector",
                    "collector_layers": input_template.collector_layers,
                    "gate_route_layer": input_template.gate_route_layer,
                },
            )
        )

    internal_template = route_templates.internal_template
    if internal_template is not None and internal_template.net in scoped_nets:
        internal_terms = _ordered_sd_net_terminals(graph, internal_template.net)
        net_problems.append(
            NativeStdCellDetailedRouteNet(
                net=internal_template.net,
                role="internal",
                route_layer=internal_template.route_layer,
                trunk_y_candidates=(internal_template.trunk_y,),
                bridge_x_candidates=(
                    (None,)
                    if internal_template.route_layer == "M2"
                    else _preferred_internal_bridge_x_candidates(
                        internal_template.left_xy,
                        internal_template.right_xy,
                    )
                ),
                color_candidates=_color_candidates_for_layer(internal_template.route_layer, pdk),
                color_roles=_color_roles_for_net_role("internal"),
                anchors=(
                    _sd_anchor_with_candidates(
                        access_catalog,
                        floorplan,
                        pdk,
                        instance=internal_terms[0][0],
                        terminal=internal_terms[0][1],
                        net=internal_template.net,
                        default_xy=internal_template.left_xy,
                    ),
                    _sd_anchor_with_candidates(
                        access_catalog,
                        floorplan,
                        pdk,
                        instance=internal_terms[1][0],
                        terminal=internal_terms[1][1],
                        net=internal_template.net,
                        default_xy=internal_template.right_xy,
                    ),
                ),
                metadata={
                    "template": "internal_bridge",
                    "route_styles": ("horizontal_bridge", "shared_vertical_bridge"),
                },
            )
        )

    output_template = route_templates.output_template
    if output_template.net in scoped_nets:
        output_anchors: list[NativeStdCellDetailedRouteAnchor] = [
            NativeStdCellDetailedRouteAnchor(
                net=output_template.net,
                role="boundary_pin",
                default_xy=output_template.pin_xy,
                fixed=True,
            )
        ]
        for device, terminal in _ordered_net_terminals(graph, output_template.net, terminal_name="D"):
            output_anchors.append(
                _sd_anchor_with_candidates(
                    access_catalog,
                    floorplan,
                    pdk,
                    instance=device,
                    terminal=terminal,
                    net=output_template.net,
                    default_xy=_lookup_output_anchor_xy(output_template, graph, device),
                )
            )
        net_problems.append(
            NativeStdCellDetailedRouteNet(
                net=output_template.net,
                role="output",
                route_layer=output_template.trunk_layer,
                trunk_x_candidates=_preferred_output_trunk_x_candidates(output_template.trunk_x, tuple(output_anchors)),
                trunk_y_candidates=(
                    (float(output_template.pin_xy[1]),)
                    if output_template.trunk_layer == "M2"
                    else _preferred_output_trunk_y_candidates(
                        output_template.pin_xy[1],
                        output_template.pmos_bus_y,
                        tuple(output_anchors),
                    )
                ),
                color_candidates=_color_candidates_for_layer(output_template.trunk_layer, pdk),
                color_roles=_color_roles_for_net_role("output"),
                anchors=tuple(output_anchors),
                metadata={"template": "output_trunk", "pmos_route_layer": output_template.pmos_route_layer},
            )
        )

    for power_template in route_templates.power_templates:
        if power_template.net not in scoped_nets:
            continue
        anchors = [
            NativeStdCellDetailedRouteAnchor(
                net=power_template.net,
                role="boundary_pin",
                default_xy=(floorplan.cell_bbox_um()[0] + 0.24, power_template.rail_y),
                fixed=True,
            )
        ]
        for device, terminal in _ordered_net_terminals(graph, power_template.net, terminal_name="S"):
            default_xy = next((xy for xy in power_template.access_points if True), None)
            del default_xy
            anchors.append(
                _sd_anchor_with_candidates(
                    access_catalog,
                    floorplan,
                    pdk,
                    instance=device,
                    terminal=terminal,
                    net=power_template.net,
                    default_xy=_terminal_default_xy(access_catalog, device, terminal),
                )
            )
        net_problems.append(
            NativeStdCellDetailedRouteNet(
                net=power_template.net,
                role="power",
                route_layer=power_template.rail_layer,
                trunk_y_candidates=(power_template.rail_y,),
                bridge_x_candidates=_preferred_power_bridge_x_candidates(tuple(anchors)),
                color_candidates=_color_candidates_for_layer(power_template.rail_layer, pdk),
                color_roles=_color_roles_for_net_role("power"),
                anchors=tuple(anchors),
                metadata={
                    "template": "power_rail",
                    "route_styles": ("vertical_drops", "shared_drop"),
                    "access_layer": power_template.access_layer,
                },
            )
        )

    return NativeStdCellDetailedRouteProblem(
        graph_name=graph.name,
        nets=tuple(net_problems),
        route_templates=route_templates,
        scoped_nets=tuple(str(net) for net in scoped_nets),
        metadata={
            "cell_bbox_um": floorplan.cell_bbox_um(),
            "template": floorplan.template.name,
            "color_model": "net_level_two_color" if _is_n7_pdk(pdk) else "none",
            "same_color_spacing_um": {
                layer: _same_color_spacing_um(pdk, layer)
                for layer in ("M0", "M1", "M2")
                if _color_candidates_for_layer(layer, pdk)
            },
        },
    )


def solve_native_stdcell_detailed_route_problem(
    problem: NativeStdCellDetailedRouteProblem,
) -> NativeStdCellDetailedRouteSolveResult:
    if z3 is None:
        raise RuntimeError("z3-solver is not installed")

    solver = z3.Optimize()
    access_vars: dict[tuple[str, str, str], object] = {}
    access_domains: dict[tuple[str, str, str], tuple[NativeStdCellAccessCandidate, ...]] = {}
    anchor_lookup: dict[tuple[str, str, str], NativeStdCellDetailedRouteAnchor] = {}
    trunk_x_vars: dict[str, object] = {}
    trunk_y_vars: dict[str, object] = {}
    bridge_x_vars: dict[str, object] = {}
    color_vars: dict[tuple[str, str], object] = {}
    pair_conflict_pairs = 0
    trunk_conflict_pairs = 0
    objective_terms: list[object] = []

    for net_problem in problem.nets:
        if len(net_problem.trunk_x_candidates) > 1:
            var = z3.Int(f"stdcell_detailed_trunk_x_{net_problem.net}")
            solver.add(z3.Or([var == idx for idx in range(len(net_problem.trunk_x_candidates))]))
            trunk_x_vars[net_problem.net] = var
            objective_terms.extend(z3.If(var == idx, idx, 0) for idx in range(len(net_problem.trunk_x_candidates)))
        if len(net_problem.trunk_y_candidates) > 1:
            var = z3.Int(f"stdcell_detailed_trunk_y_{net_problem.net}")
            solver.add(z3.Or([var == idx for idx in range(len(net_problem.trunk_y_candidates))]))
            trunk_y_vars[net_problem.net] = var
            objective_terms.extend(z3.If(var == idx, idx, 0) for idx in range(len(net_problem.trunk_y_candidates)))
        if len(net_problem.bridge_x_candidates) > 1:
            var = z3.Int(f"stdcell_detailed_bridge_x_{net_problem.net}")
            solver.add(z3.Or([var == idx for idx in range(len(net_problem.bridge_x_candidates))]))
            bridge_x_vars[net_problem.net] = var
            objective_terms.extend(z3.If(var == idx, idx, 0) for idx in range(len(net_problem.bridge_x_candidates)))
        if len(net_problem.color_candidates) > 1:
            for color_role in net_problem.color_roles:
                var = z3.Int(f"stdcell_detailed_color_{net_problem.net}_{color_role}")
                solver.add(z3.Or([var == idx for idx in range(len(net_problem.color_candidates))]))
                color_vars[(net_problem.net, color_role)] = var
                objective_terms.extend(z3.If(var == idx, idx, 0) for idx in range(len(net_problem.color_candidates)))
                objective_terms.extend(
                    z3.If(
                        var == idx,
                        _color_assignment_penalty(net_problem.role, color_role, str(color_value)),
                        0,
                    )
                    for idx, color_value in enumerate(net_problem.color_candidates)
                )
        for anchor in net_problem.anchors:
            if anchor.fixed or not anchor.candidates:
                continue
            key = (anchor.instance, anchor.terminal, anchor.net)
            var = z3.Int(f"stdcell_detailed_access_{anchor.instance}_{anchor.terminal}_{anchor.net}")
            solver.add(z3.Or([var == idx for idx in range(len(anchor.candidates))]))
            access_vars[key] = var
            access_domains[key] = tuple(anchor.candidates)
            anchor_lookup[key] = anchor
            objective_terms.extend(z3.If(var == idx, int(candidate.cost), 0) for idx, candidate in enumerate(anchor.candidates))

    access_items = tuple(access_vars.items())
    for left_idx, (left_key, left_var) in enumerate(access_items):
        left_net = left_key[2]
        left_candidates = access_domains[left_key]
        for right_key, right_var in access_items[left_idx + 1 :]:
            if left_net == right_key[2]:
                continue
            right_candidates = access_domains[right_key]
            allowed_pairs = [
                z3.And(left_var == li, right_var == ri)
                for li, left_candidate in enumerate(left_candidates)
                for ri, right_candidate in enumerate(right_candidates)
                if not bbox_overlaps(left_candidate.landing_bbox_um, right_candidate.landing_bbox_um, include_touching=True)
            ]
            pair_conflict_pairs += len(left_candidates) * len(right_candidates) - len(allowed_pairs)
            if not allowed_pairs:
                return NativeStdCellDetailedRouteSolveResult(
                    problem=problem,
                    solution=None,
                    stats=NativeStdCellDetailedRouteSolveStats(
                        backend="z3",
                        sat=False,
                        anchor_variables=len(access_vars),
                        trunk_variables=len(trunk_x_vars) + len(trunk_y_vars) + len(bridge_x_vars),
                        color_variables=len(color_vars),
                        pair_conflict_pairs=pair_conflict_pairs,
                        trunk_conflict_pairs=trunk_conflict_pairs,
                        solver_checks=0,
                    ),
                )
            solver.add(z3.Or(allowed_pairs))
            pair_penalties = [
                z3.If(
                    z3.And(left_var == li, right_var == ri),
                    _same_device_supply_signal_order_penalty(
                        problem,
                        anchor_lookup.get(left_key),
                        anchor_lookup.get(right_key),
                        left_candidate.xy,
                        right_candidate.xy,
                    ),
                    0,
                )
                for li, left_candidate in enumerate(left_candidates)
                for ri, right_candidate in enumerate(right_candidates)
            ]
            if pair_penalties:
                objective_terms.append(z3.Sum(pair_penalties))

    trunk_options = {
        net_problem.net: _enumerate_net_trunk_options(net_problem)
        for net_problem in problem.nets
    }
    net_index = {net_problem.net: idx for idx, net_problem in enumerate(problem.nets)}
    for left_name, left_options in trunk_options.items():
        for right_name, right_options in trunk_options.items():
            if net_index[left_name] >= net_index[right_name]:
                continue
            if not left_options or not right_options:
                continue
            if left_options[0][2] != right_options[0][2]:
                continue
            left_x_var = trunk_x_vars.get(left_name)
            left_y_var = trunk_y_vars.get(left_name)
            right_x_var = trunk_x_vars.get(right_name)
            right_y_var = trunk_y_vars.get(right_name)
            allowed_pairs = []
            for li, left_option in enumerate(left_options):
                for ri, right_option in enumerate(right_options):
                    if not bbox_overlaps(left_option[3], right_option[3], include_touching=True):
                        allowed_pairs.append(
                            z3.And(
                                _option_selected_expr(left_x_var, left_y_var, left_option[0], left_option[1]),
                                _option_selected_expr(right_x_var, right_y_var, right_option[0], right_option[1]),
                            )
                        )
            trunk_conflict_pairs += len(left_options) * len(right_options) - len(allowed_pairs)
            if allowed_pairs:
                solver.add(z3.Or(allowed_pairs))

    route_state_options = {
        net_problem.net: _enumerate_route_state_options(problem, net_problem)
        for net_problem in problem.nets
    }
    for net_name, options in route_state_options.items():
        for option in options:
            option_expr = _route_state_selected_expr(option, access_vars, trunk_x_vars, trunk_y_vars, bridge_x_vars)
            if option.cost <= 0:
                continue
            objective_terms.append(
                z3.If(
                    option_expr,
                    option.cost,
                    0,
                )
            )
    for left_name, left_options in route_state_options.items():
        for right_name, right_options in route_state_options.items():
            if net_index[left_name] >= net_index[right_name]:
                continue
            if not left_options or not right_options:
                continue
            allowed_pairs = []
            for left_option in left_options:
                left_expr = _route_state_selected_expr(left_option, access_vars, trunk_x_vars, trunk_y_vars, bridge_x_vars)
                for right_option in right_options:
                    right_expr = _route_state_selected_expr(right_option, access_vars, trunk_x_vars, trunk_y_vars, bridge_x_vars)
                    if not _route_state_conflicts(left_option, right_option):
                        allowed_pairs.append(
                            z3.And(
                                left_expr,
                                right_expr,
                            )
                        )
                        for color_expr in _route_state_same_color_pair_exprs(
                            problem,
                            left_option,
                            right_option,
                            left_selected_expr=left_expr,
                            right_selected_expr=right_expr,
                            color_vars=color_vars,
                        ):
                            solver.add(color_expr)
                        objective_terms.extend(
                            _route_state_soft_color_pair_terms(
                                problem,
                                left_option,
                                right_option,
                                left_selected_expr=left_expr,
                                right_selected_expr=right_expr,
                                color_vars=color_vars,
                            )
                        )
            if not allowed_pairs:
                return NativeStdCellDetailedRouteSolveResult(
                    problem=problem,
                    solution=None,
                    stats=NativeStdCellDetailedRouteSolveStats(
                        backend="z3",
                        sat=False,
                        anchor_variables=len(access_vars),
                        trunk_variables=len(trunk_x_vars) + len(trunk_y_vars) + len(bridge_x_vars),
                        color_variables=len(color_vars),
                        pair_conflict_pairs=pair_conflict_pairs,
                        trunk_conflict_pairs=trunk_conflict_pairs,
                        solver_checks=0,
                    ),
                )
            solver.add(z3.Or(allowed_pairs))

    total_cost = z3.Int("stdcell_detailed_total_cost")
    solver.add(total_cost == z3.Sum(objective_terms) if objective_terms else z3.IntVal(0))
    solver.minimize(total_cost)
    checks = 1
    status = solver.check()
    if status != z3.sat:
        return NativeStdCellDetailedRouteSolveResult(
            problem=problem,
            solution=None,
            stats=NativeStdCellDetailedRouteSolveStats(
                backend="z3",
                sat=False,
                anchor_variables=len(access_vars),
                trunk_variables=len(trunk_x_vars) + len(trunk_y_vars) + len(bridge_x_vars),
                color_variables=len(color_vars),
                pair_conflict_pairs=pair_conflict_pairs,
                trunk_conflict_pairs=trunk_conflict_pairs,
                solver_checks=checks,
            ),
        )
    model = solver.model()
    solution = NativeStdCellDetailedRouteSolution(
        access_choices=tuple(
            (instance, terminal, net, model.eval(var).as_long())
            for (instance, terminal, net), var in sorted(access_vars.items())
        ),
        trunk_x_choices=tuple(
            (net, problem.net_map()[net].trunk_x_candidates[model.eval(var).as_long()])
            for net, var in sorted(trunk_x_vars.items())
        ),
        trunk_y_choices=tuple(
            (net, problem.net_map()[net].trunk_y_candidates[model.eval(var).as_long()])
            for net, var in sorted(trunk_y_vars.items())
        ),
        bridge_x_choices=tuple(
            (net, problem.net_map()[net].bridge_x_candidates[model.eval(var).as_long()])
            for net, var in sorted(bridge_x_vars.items())
        ),
        segment_color_choices=tuple(
            (net, role, problem.net_map()[net].color_candidates[model.eval(var).as_long()])
            for (net, role), var in sorted(color_vars.items())
        ),
        color_choices=tuple(
            (net_problem.net, problem.net_map()[net_problem.net].color_candidates[model.eval(color_vars[(net_problem.net, net_problem.color_roles[0])]).as_long()])
            for net_problem in problem.nets
            if len(net_problem.color_candidates) > 1 and net_problem.color_roles and (net_problem.net, net_problem.color_roles[0]) in color_vars
        ),
        cost=float(model.eval(total_cost).as_long()),
        metadata={"solver": "z3"},
    )
    return NativeStdCellDetailedRouteSolveResult(
        problem=problem,
        solution=solution,
        stats=NativeStdCellDetailedRouteSolveStats(
            backend="z3",
            sat=True,
            anchor_variables=len(access_vars),
            trunk_variables=len(trunk_x_vars) + len(trunk_y_vars) + len(bridge_x_vars),
            color_variables=len(color_vars),
            pair_conflict_pairs=pair_conflict_pairs,
            trunk_conflict_pairs=trunk_conflict_pairs,
            solver_checks=checks,
        ),
    )


def project_native_stdcell_detailed_route_solution(
    problem: NativeStdCellDetailedRouteProblem,
    solution: NativeStdCellDetailedRouteSolution,
) -> NativeStdCellRouteTemplateSet:
    choice_map = solution.access_choice_map()
    trunk_x_map = solution.trunk_x_map()
    trunk_y_map = solution.trunk_y_map()
    bridge_x_map = solution.bridge_x_map()
    color_map = solution.color_map()
    segment_color_map = solution.segment_color_map()
    route_templates = problem.route_templates
    net_map = problem.net_map()
    input_templates = []
    for input_template in route_templates.input_templates:
        if input_template.net not in net_map:
            input_templates.append(input_template)
            continue
        input_net = net_map[input_template.net]
        gate_anchors = [anchor for anchor in input_net.anchors if anchor.role == "gate_access"]
        selected_gate_points = tuple(_selected_anchor_xy(anchor, choice_map) for anchor in gate_anchors)
        contact_x = trunk_x_map.get(input_template.net, input_template.contact_xy[0])
        contact_xy = (contact_x, input_template.contact_xy[1])
        pin_xy = (contact_x, input_template.pin_xy[1])
        input_templates.append(
            replace(
                input_template,
                gate_points=selected_gate_points or input_template.gate_points,
                contact_xy=contact_xy,
                pin_xy=pin_xy,
            )
        )

    internal_template = route_templates.internal_template
    if internal_template is not None and internal_template.net in net_map:
        internal_net = net_map[internal_template.net]
        internal_sd_anchors = [anchor for anchor in internal_net.anchors if anchor.role == "sd_access"]
        if len(internal_sd_anchors) == 2:
            internal_template = replace(
                internal_template,
                left_xy=_selected_anchor_xy(internal_sd_anchors[0], choice_map),
                right_xy=_selected_anchor_xy(internal_sd_anchors[1], choice_map),
                trunk_y=trunk_y_map.get(internal_template.net, internal_template.trunk_y),
                route_style="shared_vertical_bridge" if internal_template.net in bridge_x_map else "horizontal_bridge",
                bridge_x=bridge_x_map.get(internal_template.net),
            )

    output_template = route_templates.output_template
    if output_template.net in net_map:
        output_net = net_map[output_template.net]
        output_sd_anchors = [anchor for anchor in output_net.anchors if anchor.role == "sd_access"]
        selected_xy_by_instance = {
            anchor.instance: _selected_anchor_xy(anchor, choice_map)
            for anchor in output_sd_anchors
        }
        pmos_points = tuple(selected_xy_by_instance[anchor.instance] for anchor in output_sd_anchors if anchor.instance.upper().startswith("MP"))
        nmos_points = tuple(selected_xy_by_instance[anchor.instance] for anchor in output_sd_anchors if not anchor.instance.upper().startswith("MP"))
        trunk_x = trunk_x_map.get(output_template.net, output_template.trunk_x)
        shared_bus_y = trunk_y_map.get(output_template.net, output_template.pmos_bus_y)
        trunk_bottom_y = min(output_template.pin_xy[1], shared_bus_y)
        trunk_top_y = max(output_template.pin_xy[1], shared_bus_y)
        output_template = replace(
            output_template,
            trunk_x=trunk_x,
            trunk_bottom_y=trunk_bottom_y,
            trunk_top_y=trunk_top_y,
            pmos_bus_y=shared_bus_y,
            pmos_points=pmos_points or output_template.pmos_points,
            nmos_points=nmos_points or output_template.nmos_points,
        )

    power_templates = []
    for power_template in route_templates.power_templates:
        if power_template.net not in net_map:
            power_templates.append(power_template)
            continue
        power_net = net_map[power_template.net]
        access_points = tuple(
            _selected_anchor_xy(anchor, choice_map)
            for anchor in power_net.anchors
            if anchor.role == "sd_access"
        )
        power_templates.append(
            replace(
                power_template,
                rail_y=trunk_y_map.get(power_template.net, power_template.rail_y),
                access_points=access_points or power_template.access_points,
                route_style="shared_drop" if power_template.net in bridge_x_map else "vertical_drops",
                bridge_x=bridge_x_map.get(power_template.net),
            )
        )

    return replace(
        route_templates,
        input_templates=tuple(input_templates),
        internal_template=internal_template,
        output_template=output_template,
        power_templates=tuple(power_templates),
        color_by_net={**dict(getattr(route_templates, "color_by_net", {})), **color_map},
        color_by_segment={**dict(getattr(route_templates, "color_by_segment", {})), **segment_color_map},
    )


def _sd_anchor_with_candidates(
    access_catalog: "NativeStdCellAccessCatalog",
    floorplan: "NativeStdCellFloorplan",
    pdk: "PdkConfig",
    *,
    instance: str,
    terminal: str,
    net: str,
    default_xy: tuple[float, float],
) -> NativeStdCellDetailedRouteAnchor:
    candidates = list(
        enumerate_native_stdcell_sd_access_candidates(
            access_catalog,
            floorplan,
            pdk,
            instance=instance,
            terminal=terminal,
            net=net,
        )
    )
    snapped_default_xy = tuple(float(v) for v in pdk.rules.snap_point_um(default_xy))
    if not any(
        abs(candidate.xy[0] - snapped_default_xy[0]) <= 1e-9
        and abs(candidate.xy[1] - snapped_default_xy[1]) <= 1e-9
        for candidate in candidates
    ):
        via_margin = max(float(pdk.rules.min_width_um("VIA0")) / 2.0, 0.01)
        landing = pdk.rules.snap_bbox_um(
            (
                snapped_default_xy[0] - via_margin,
                snapped_default_xy[1] - via_margin,
                snapped_default_xy[0] + via_margin,
                snapped_default_xy[1] + via_margin,
            ),
            mode="outward",
        )
        candidates.insert(
            0,
            NativeStdCellAccessCandidate(
                instance=str(instance),
                terminal=str(terminal),
                net=str(net),
                xy=snapped_default_xy,
                landing_bbox_um=landing,
                source="default_xy",
                cost=0,
            ),
        )
    return NativeStdCellDetailedRouteAnchor(
        net=str(net),
        role="sd_access",
        instance=str(instance),
        terminal=str(terminal),
        default_xy=tuple(float(v) for v in default_xy),
        candidates=tuple(candidates),
        fixed=False,
    )


def _gate_anchor_with_candidates(
    access_catalog: "NativeStdCellAccessCatalog",
    pdk: "PdkConfig",
    *,
    instance: str,
    terminal: str,
    net: str,
    default_xy: tuple[float, float],
    target_y: float,
) -> NativeStdCellDetailedRouteAnchor:
    candidates = enumerate_native_stdcell_gate_access_candidates(
        access_catalog,
        pdk,
        instance=instance,
        terminal=terminal,
        net=net,
        target_y=target_y,
    )
    return NativeStdCellDetailedRouteAnchor(
        net=str(net),
        role="gate_access",
        instance=str(instance),
        terminal=str(terminal),
        default_xy=tuple(float(v) for v in default_xy),
        candidates=tuple(candidates),
        fixed=False,
    )


def _ordered_net_terminals(
    graph: TopologyGraph,
    net_name: str,
    *,
    terminal_name: str,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(term.device), str(term.terminal))
        for term in graph.nets[net_name].terminals
        if term.device in graph.devices and str(term.terminal) == terminal_name
    )


def _ordered_sd_net_terminals(
    graph: TopologyGraph,
    net_name: str,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (str(term.device), str(term.terminal))
        for term in graph.nets[net_name].terminals
        if term.device in graph.devices and str(term.terminal) in {"S", "D"}
    )


def _lookup_output_anchor_xy(
    output_template: object,
    graph: TopologyGraph,
    device_name: str,
) -> tuple[float, float]:
    pmos_devices = [
        term.device
        for term in graph.nets[getattr(output_template, "net")].terminals
        if term.device in graph.devices and term.terminal == "D" and str(term.device).upper().startswith("MP")
    ]
    if device_name in pmos_devices:
        pmos_points = tuple(getattr(output_template, "pmos_points", ()))
        if pmos_points:
            if len(pmos_points) == 1:
                return tuple(float(v) for v in pmos_points[0])
            idx = min(pmos_devices.index(device_name), len(pmos_points) - 1)
            return tuple(float(v) for v in pmos_points[idx])
    nmos_devices = [
        term.device
        for term in graph.nets[getattr(output_template, "net")].terminals
        if term.device in graph.devices and term.terminal == "D" and not str(term.device).upper().startswith("MP")
    ]
    nmos_points = tuple(getattr(output_template, "nmos_points", ()))
    if nmos_points:
        if len(nmos_points) == 1:
            return tuple(float(v) for v in nmos_points[0])
        idx = min(nmos_devices.index(device_name), len(nmos_points) - 1)
        return tuple(float(v) for v in nmos_points[idx])
    return (0.0, 0.0)


def _terminal_default_xy(
    access_catalog: "NativeStdCellAccessCatalog",
    instance: str,
    terminal: str,
) -> tuple[float, float]:
    breakout = access_catalog.breakout_for(instance, terminal)
    return tuple(float(v) for v in getattr(breakout, "xy_um"))


def _selected_anchor_xy(
    anchor: NativeStdCellDetailedRouteAnchor,
    choice_map: Mapping[tuple[str, str, str], int],
) -> tuple[float, float]:
    if anchor.fixed or not anchor.candidates:
        return anchor.default_xy
    idx = choice_map[(anchor.instance, anchor.terminal, anchor.net)]
    return tuple(float(v) for v in anchor.candidates[idx].xy)


def _enumerate_route_state_options(
    problem: NativeStdCellDetailedRouteProblem,
    net_problem: NativeStdCellDetailedRouteNet,
) -> tuple[_RouteStateOption, ...]:
    nonfixed_anchors = tuple(anchor for anchor in net_problem.anchors if not anchor.fixed and anchor.candidates)
    access_domains = [
        tuple((anchor.instance, anchor.terminal, anchor.net, idx) for idx in range(len(anchor.candidates)))
        for anchor in nonfixed_anchors
    ]
    x_indices = tuple(range(len(net_problem.trunk_x_candidates))) if net_problem.trunk_x_candidates else (0,)
    y_indices = tuple(range(len(net_problem.trunk_y_candidates))) if net_problem.trunk_y_candidates else (0,)
    bridge_indices = tuple(range(len(net_problem.bridge_x_candidates))) if net_problem.bridge_x_candidates else (0,)
    options: list[_RouteStateOption] = []
    for access_choice_product in product(*access_domains) if access_domains else ((),):
        selected_xy = {
            (anchor.instance, anchor.terminal, anchor.net): anchor.candidates[idx].xy
            for anchor, (_, _, _, idx) in zip(nonfixed_anchors, access_choice_product)
        }
        for x_index in x_indices:
            trunk_x = float(net_problem.trunk_x_candidates[x_index]) if net_problem.trunk_x_candidates else 0.0
            for y_index in y_indices:
                trunk_y = float(net_problem.trunk_y_candidates[y_index]) if net_problem.trunk_y_candidates else 0.0
                for bridge_index in bridge_indices:
                    bridge_x = (
                        net_problem.bridge_x_candidates[bridge_index]
                        if net_problem.bridge_x_candidates
                        else None
                    )
                    shapes = _route_state_shapes(
                        problem,
                        net_problem,
                        selected_xy,
                        trunk_x=trunk_x,
                        trunk_y=trunk_y,
                        bridge_x=bridge_x,
                    )
                    option_cost = _route_state_option_cost(
                        problem,
                        net_problem,
                        selected_xy,
                        trunk_x=trunk_x,
                        trunk_y=trunk_y,
                        bridge_x=bridge_x,
                    )
                    options.append(
                        _RouteStateOption(
                            net=net_problem.net,
                            access_choices=tuple(access_choice_product),
                            trunk_x_index=x_index,
                            trunk_y_index=y_index,
                            bridge_x_index=bridge_index,
                            cost=option_cost,
                            shapes=shapes,
                        )
                    )
    return tuple(options)


def _route_state_shapes(
    problem: NativeStdCellDetailedRouteProblem,
    net_problem: NativeStdCellDetailedRouteNet,
    selected_xy: Mapping[tuple[str, str, str], tuple[float, float]],
    *,
    trunk_x: float,
    trunk_y: float,
    bridge_x: float | None,
) -> tuple[tuple[str, BBox, str], ...]:
    route_templates = problem.route_templates
    width = 0.06
    half = width / 2.0
    via_landing_half = 0.04
    if net_problem.role == "input":
        template = next((item for item in route_templates.input_templates if item.net == net_problem.net), None)
        if template is None:
            return ()
        shapes: list[tuple[str, BBox, str]] = []
        gate_points: list[tuple[float, float]] = []
        for gate_access in template.gate_accesses:
            gate_xy = selected_xy.get((gate_access.instance, gate_access.terminal, template.net), gate_access.default_xy)
            gate_points.append(gate_xy)
            if abs(gate_xy[0] - trunk_x) > 1e-9:
                shapes.extend(_polyline_bboxes("PO", (gate_xy, (trunk_x, gate_xy[1])), width=0.03, color_role="gate_escape"))
        contact_xy = (trunk_x, template.contact_xy[1])
        if gate_points:
            trunk_bottom_y = min([contact_xy[1], *(point[1] for point in gate_points)])
            trunk_top_y = max([contact_xy[1], *(point[1] for point in gate_points)])
            if abs(trunk_top_y - trunk_bottom_y) > 1e-9:
                shapes.extend(_polyline_bboxes("PO", ((trunk_x, trunk_bottom_y), (trunk_x, trunk_top_y)), width=0.03, color_role="gate_trunk"))
        shapes.append(("M1", _point_bbox(contact_xy, via_landing_half), "contact"))
        shapes.append(("M2", _point_bbox((trunk_x, template.pin_xy[1]), via_landing_half), "pin"))
        return tuple(shapes)
    if net_problem.role == "internal":
        template = route_templates.internal_template
        if template is None:
            return ()
        anchors = [anchor for anchor in net_problem.anchors if anchor.role == "sd_access"]
        if len(anchors) != 2:
            return ()
        left_xy = selected_xy.get((anchors[0].instance, anchors[0].terminal, anchors[0].net), anchors[0].default_xy)
        right_xy = selected_xy.get((anchors[1].instance, anchors[1].terminal, anchors[1].net), anchors[1].default_xy)
        if template.route_layer == "M2":
            bus_y = trunk_y
            bus_left_x = min(left_xy[0], right_xy[0])
            bus_right_x = max(left_xy[0], right_xy[0])
            left_branch = _extend_vertical_segment_bbox(
                left_xy,
                (left_xy[0], bus_y),
                min_length_um=0.10,
                extend_from="start",
            )
            right_branch = _extend_vertical_segment_bbox(
                right_xy,
                (right_xy[0], bus_y),
                min_length_um=0.10,
                extend_from="start",
            )
            return _dedupe_route_shapes(
                (
                    *_polyline_bboxes("M1", left_branch, width=width, color_role="left_branch"),
                    *_polyline_bboxes("M1", right_branch, width=width, color_role="right_branch"),
                    *_polyline_bboxes("M2", ((bus_left_x, bus_y), (bus_right_x, bus_y)), width=width, color_role="bridge"),
                )
            )
        if bridge_x is not None:
            return _dedupe_route_shapes(
                (
                    *_polyline_bboxes(template.route_layer, (left_xy, (bridge_x, left_xy[1])), width=width, color_role="left_branch"),
                    *_polyline_bboxes(template.route_layer, (right_xy, (bridge_x, right_xy[1])), width=width, color_role="right_branch"),
                    *_polyline_bboxes(
                        template.route_layer,
                        ((bridge_x, min(left_xy[1], right_xy[1])), (bridge_x, max(left_xy[1], right_xy[1]))),
                        width=width,
                        color_role="bridge",
                    ),
                )
            )
        return _dedupe_route_shapes(
            (
                *_polyline_bboxes(template.route_layer, (left_xy, (left_xy[0], trunk_y)), width=width, color_role="left_branch"),
                *_polyline_bboxes(template.route_layer, ((left_xy[0], trunk_y), (right_xy[0], trunk_y)), width=width, color_role="bridge"),
                *_polyline_bboxes(template.route_layer, ((right_xy[0], trunk_y), right_xy), width=width, color_role="right_branch"),
            )
        )
    if net_problem.role == "output":
        template = route_templates.output_template
        pin_xy = template.pin_xy
        pmos_anchors = [anchor for anchor in net_problem.anchors if anchor.role == "sd_access" and anchor.instance.upper().startswith("MP")]
        nmos_anchors = [anchor for anchor in net_problem.anchors if anchor.role == "sd_access" and not anchor.instance.upper().startswith("MP")]
        pmos_points = [
            selected_xy.get((anchor.instance, anchor.terminal, anchor.net), anchor.default_xy)
            for anchor in pmos_anchors
        ]
        nmos_points = [
            selected_xy.get((anchor.instance, anchor.terminal, anchor.net), anchor.default_xy)
            for anchor in nmos_anchors
        ]
        shapes: list[tuple[str, BBox, str]] = []
        bus_y = trunk_y
        if template.trunk_layer == "M2":
            bus_left_x = min(float(trunk_x), *(point[0] for point in (*nmos_points, *pmos_points)))
            if abs(pin_xy[0] - bus_left_x) > 1e-9:
                shapes.extend(
                    _polyline_bboxes(
                        template.trunk_layer,
                        ((bus_left_x, bus_y), pin_xy),
                        width=width,
                        color_role="trunk",
                    )
            )
            shapes.append((template.trunk_layer, _point_bbox(pin_xy, via_landing_half), "pin_landing"))
            for access_xy in (*nmos_points, *pmos_points):
                shapes.extend(
                    _polyline_bboxes(
                        template.pmos_route_layer,
                        (access_xy, (access_xy[0], bus_y)),
                        width=width,
                        color_role="nmos_branch" if access_xy in nmos_points else "pmos_branch",
                    )
                )
        else:
            trunk_bottom_y = min(pin_xy[1], bus_y)
            trunk_top_y = max(pin_xy[1], bus_y)
            shapes.extend(_polyline_bboxes(template.trunk_layer, ((trunk_x, trunk_bottom_y), (trunk_x, trunk_top_y)), width=width, color_role="trunk"))
            shapes.append((template.trunk_layer, _point_bbox(pin_xy, via_landing_half), "pin_landing"))
            if abs(pin_xy[0] - trunk_x) > 1e-9:
                shapes.extend(_polyline_bboxes(template.trunk_layer, ((trunk_x, pin_xy[1]), pin_xy), width=width, color_role="pin_stub"))
            for access_xy in nmos_points:
                shapes.extend(
                    _polyline_bboxes(
                        template.trunk_layer,
                        (access_xy, (access_xy[0], bus_y), (trunk_x, bus_y)),
                        width=width,
                        color_role="nmos_branch",
                    )
                )
            for access_xy in pmos_points:
                shapes.extend(
                    _polyline_bboxes(
                        template.pmos_route_layer,
                        (access_xy, (access_xy[0], bus_y), (trunk_x, bus_y)),
                        width=width,
                        color_role="pmos_branch",
                    )
                )
        return _dedupe_route_shapes(tuple(shapes))
    if net_problem.role == "power":
        template = next((item for item in route_templates.power_templates if item.net == net_problem.net), None)
        if template is None:
            return ()
        left_x, _, right_x, _ = route_templates.cell_bbox_um
        shapes = list(_polyline_bboxes(template.rail_layer, ((left_x, trunk_y), (right_x, trunk_y)), width=width, color_role="rail"))
        access_points: list[tuple[float, float]] = []
        for anchor in net_problem.anchors:
            if anchor.role != "sd_access":
                continue
            access_xy = selected_xy.get((anchor.instance, anchor.terminal, anchor.net), anchor.default_xy)
            access_points.append(access_xy)
        if bridge_x is not None and access_points:
            branch_y = access_points[0][1]
            for access_xy in access_points:
                shapes.extend(_polyline_bboxes(template.access_layer, (access_xy, (bridge_x, branch_y)), width=width, color_role="drop"))
            shapes.extend(_polyline_bboxes(template.access_layer, ((bridge_x, branch_y), (bridge_x, trunk_y)), width=width, color_role="shared_drop"))
            shapes.append((template.rail_layer, _point_bbox((bridge_x, trunk_y), via_landing_half), "rail"))
        else:
            for access_xy in access_points:
                drop_points = _extend_vertical_segment_bbox(
                    access_xy,
                    (access_xy[0], trunk_y),
                    min_length_um=0.10,
                    extend_from="end",
                )
                shapes.extend(_polyline_bboxes(template.access_layer, drop_points, width=width, color_role="drop"))
        return _dedupe_route_shapes(tuple(shapes))
    return ()


def _route_state_option_cost(
    problem: NativeStdCellDetailedRouteProblem,
    net_problem: NativeStdCellDetailedRouteNet,
    selected_xy: Mapping[tuple[str, str, str], tuple[float, float]],
    *,
    trunk_x: float,
    trunk_y: float,
    bridge_x: float | None,
) -> int:
    route_templates = problem.route_templates

    def _um_cost(length_um: float, scale: float = 1000.0) -> int:
        return int(round(max(length_um, 0.0) * scale))

    def _turn_cost(points: tuple[tuple[float, float], ...], *, penalty: int = 25) -> int:
        compact = [tuple(float(v) for v in pt) for pt in points]
        if len(compact) < 3:
            return 0
        turns = 0
        prev_dir: tuple[int, int] | None = None
        for left, right in zip(compact, compact[1:]):
            dx = right[0] - left[0]
            dy = right[1] - left[1]
            if abs(dx) <= 1e-9 and abs(dy) <= 1e-9:
                continue
            direction = (0 if abs(dx) <= 1e-9 else (1 if dx > 0 else -1), 0 if abs(dy) <= 1e-9 else (1 if dy > 0 else -1))
            if prev_dir is not None and direction != prev_dir:
                turns += 1
            prev_dir = direction
        return turns * penalty

    if net_problem.role == "input":
        template = next((item for item in route_templates.input_templates if item.net == net_problem.net), None)
        if template is None:
            return 0
        contact_xy = (trunk_x, template.contact_xy[1])
        cost = 0
        gate_points: list[tuple[float, float]] = []
        for gate_access in template.gate_accesses:
            gate_xy = selected_xy.get((gate_access.instance, gate_access.terminal, template.net), gate_access.default_xy)
            gate_points.append(gate_xy)
            if abs(gate_xy[0] - trunk_x) > 1e-9:
                route_points = (gate_xy, (trunk_x, gate_xy[1]))
                cost += _um_cost(_polyline_length_um(route_points))
        if gate_points:
            trunk_bottom_y = min([contact_xy[1], *(point[1] for point in gate_points)])
            trunk_top_y = max([contact_xy[1], *(point[1] for point in gate_points)])
            cost += _um_cost(trunk_top_y - trunk_bottom_y, scale=400.0)
            gate_xs = [point[0] for point in gate_points]
            span_left = min(gate_xs)
            span_right = max(gate_xs)
            if trunk_x < span_left:
                cost += _um_cost(span_left - trunk_x, scale=1200.0)
            elif trunk_x > span_right:
                cost += _um_cost(trunk_x - span_right, scale=1200.0)
        return cost

    if net_problem.role == "internal":
        template = route_templates.internal_template
        if template is None:
            return 0
        anchors = [anchor for anchor in net_problem.anchors if anchor.role == "sd_access"]
        if len(anchors) != 2:
            return 0
        left_xy = selected_xy.get((anchors[0].instance, anchors[0].terminal, anchors[0].net), anchors[0].default_xy)
        right_xy = selected_xy.get((anchors[1].instance, anchors[1].terminal, anchors[1].net), anchors[1].default_xy)
        if template.route_layer == "M2":
            bus_y = trunk_y
            bus_left_x = min(left_xy[0], right_xy[0])
            bus_right_x = max(left_xy[0], right_xy[0])
            left_route = (left_xy, (left_xy[0], bus_y))
            right_route = (right_xy, (right_xy[0], bus_y))
            bus_route = ((bus_left_x, bus_y), (bus_right_x, bus_y))
            return (
                _um_cost(_polyline_length_um(left_route))
                + _um_cost(_polyline_length_um(right_route))
                + _um_cost(_polyline_length_um(bus_route), scale=500.0)
                + _turn_cost(left_route)
                + _turn_cost(right_route)
            )
        left_x = min(left_xy[0], right_xy[0])
        if bridge_x is not None:
            route_points = (
                (left_xy[0], left_xy[1]),
                (bridge_x, left_xy[1]),
                (bridge_x, right_xy[1]),
                (right_xy[0], right_xy[1]),
            )
            # Reference NAND/NOR cells keep the internal bridge tucked locally,
            # so bias the bridge toward the left-side series-node access.
            return _um_cost(_polyline_length_um(route_points)) + _turn_cost(route_points) + _um_cost(abs(bridge_x - left_x), scale=200.0)
        route_points = (left_xy, (left_xy[0], trunk_y), (right_xy[0], trunk_y), right_xy)
        return _um_cost(_polyline_length_um(route_points)) + _turn_cost(route_points)

    if net_problem.role == "output":
        template = route_templates.output_template
        pin_xy = template.pin_xy
        pmos_anchors = [anchor for anchor in net_problem.anchors if anchor.role == "sd_access" and anchor.instance.upper().startswith("MP")]
        nmos_anchors = [anchor for anchor in net_problem.anchors if anchor.role == "sd_access" and not anchor.instance.upper().startswith("MP")]
        pmos_points = [
            selected_xy.get((anchor.instance, anchor.terminal, anchor.net), anchor.default_xy)
            for anchor in pmos_anchors
        ]
        nmos_points = [
            selected_xy.get((anchor.instance, anchor.terminal, anchor.net), anchor.default_xy)
            for anchor in nmos_anchors
        ]
        bus_y = trunk_y
        cost = 0
        if template.trunk_layer == "M2":
            anchor_xs = [point[0] for point in (*nmos_points, *pmos_points)]
            leftmost = min(anchor_xs) if anchor_xs else float(trunk_x)
            rightmost = max([pin_xy[0], *anchor_xs]) if anchor_xs else pin_xy[0]
            cost += _um_cost(max(rightmost - float(trunk_x), 0.0), scale=140.0)
            cost += _um_cost(abs(float(trunk_x) - leftmost), scale=700.0)
        else:
            # Reference stdcells keep the local output collector inside the active
            # transistor channel and use a higher-layer pin stub to reach the
            # boundary, so pin distance should not dominate trunk placement.
            cost += _um_cost(abs(pin_xy[0] - trunk_x) + abs(pin_xy[1] - bus_y), scale=160.0)
        # Bias the local collector toward the center routing channel used by
        # compact reference NAND/NOR cells instead of collapsing onto the PMOS row.
        cost += _um_cost(abs(bus_y - float(template.pin_xy[1])), scale=700.0)
        for access_xy in nmos_points:
            route_points = (access_xy, (access_xy[0], bus_y)) if template.trunk_layer == "M2" else (access_xy, (access_xy[0], bus_y), (trunk_x, bus_y))
            cost += _um_cost(_polyline_length_um(route_points))
            cost += _turn_cost(route_points)
        for access_xy in pmos_points:
            route_points = (access_xy, (access_xy[0], bus_y)) if template.trunk_layer == "M2" else (access_xy, (access_xy[0], bus_y), (trunk_x, bus_y))
            cost += _um_cost(_polyline_length_um(route_points))
            cost += _turn_cost(route_points)
        # Reference stdcells keep the output channel on the right and minimize
        # unnecessary branch spread, but the local collector still stays within
        # the device-access span instead of hugging the boundary pin.
        anchor_xs = [point[0] for point in (*nmos_points, *pmos_points)]
        if anchor_xs:
            cost += _um_cost(max(anchor_xs) - min(anchor_xs), scale=250.0)
            if template.trunk_layer != "M2":
                span_left = min(anchor_xs)
                span_right = max(anchor_xs)
                if trunk_x < span_left:
                    cost += _um_cost(span_left - trunk_x, scale=1400.0)
                elif trunk_x > span_right:
                    cost += _um_cost(trunk_x - span_right, scale=1400.0)
        return cost

    if net_problem.role == "power":
        template = next((item for item in route_templates.power_templates if item.net == net_problem.net), None)
        if template is None:
            return 0
        access_points = [
            selected_xy.get((anchor.instance, anchor.terminal, anchor.net), anchor.default_xy)
            for anchor in net_problem.anchors
            if anchor.role == "sd_access"
        ]
        cost = 0
        if bridge_x is not None and access_points:
            branch_y = access_points[0][1]
            for access_xy in access_points:
                route_points = (access_xy, (bridge_x, branch_y))
                cost += _um_cost(_polyline_length_um(route_points))
            cost += _um_cost(abs(branch_y - trunk_y), scale=600.0)
        else:
            for access_xy in access_points:
                cost += _um_cost(abs(access_xy[1] - trunk_y), scale=600.0)
        return cost

    return 0


def _route_state_selected_expr(
    option: _RouteStateOption,
    access_vars: Mapping[tuple[str, str, str], object],
    trunk_x_vars: Mapping[str, object],
    trunk_y_vars: Mapping[str, object],
    bridge_x_vars: Mapping[str, object],
) -> object:
    clauses = []
    for instance, terminal, net, idx in option.access_choices:
        var = access_vars.get((instance, terminal, net))
        if var is not None:
            clauses.append(var == idx)
    x_var = trunk_x_vars.get(option.net)
    if x_var is not None:
        clauses.append(x_var == option.trunk_x_index)
    y_var = trunk_y_vars.get(option.net)
    if y_var is not None:
        clauses.append(y_var == option.trunk_y_index)
    bridge_var = bridge_x_vars.get(option.net)
    if bridge_var is not None:
        clauses.append(bridge_var == option.bridge_x_index)
    if not clauses:
        return z3.BoolVal(True)
    if len(clauses) == 1:
        return clauses[0]
    return z3.And(clauses)


def _polyline_length_um(
    points: tuple[tuple[float, float], ...],
) -> float:
    compact: list[tuple[float, float]] = []
    for point in points:
        if compact and abs(compact[-1][0] - point[0]) <= 1e-9 and abs(compact[-1][1] - point[1]) <= 1e-9:
            continue
        compact.append(point)
    total = 0.0
    for left, right in zip(compact, compact[1:]):
        total += abs(float(right[0]) - float(left[0])) + abs(float(right[1]) - float(left[1]))
    return total


def _route_state_conflicts(
    left_option: _RouteStateOption,
    right_option: _RouteStateOption,
) -> bool:
    for left_layer, left_bbox, _left_role in left_option.shapes:
        for right_layer, right_bbox, _right_role in right_option.shapes:
            if left_layer != right_layer:
                continue
            if bbox_overlaps(left_bbox, right_bbox, include_touching=True):
                return True
    return False


def _route_state_same_color_pair_exprs(
    problem: NativeStdCellDetailedRouteProblem,
    left_option: _RouteStateOption,
    right_option: _RouteStateOption,
    *,
    left_selected_expr: object,
    right_selected_expr: object,
    color_vars: Mapping[object, object],
) -> tuple[object, ...]:
    spacing_by_layer = {
        str(layer): float(value)
        for layer, value in dict(problem.metadata.get("same_color_spacing_um", {})).items()
    }
    exprs: list[object] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for left_layer, left_bbox, left_role in left_option.shapes:
        threshold = spacing_by_layer.get(left_layer)
        if threshold is None:
            continue
        for right_layer, right_bbox, right_role in right_option.shapes:
            if left_layer != right_layer:
                continue
            if bbox_overlaps(left_bbox, right_bbox, include_touching=True):
                continue
            if _bbox_distance_um(left_bbox, right_bbox) + 1e-12 < threshold:
                left_expr = _segment_color_expr(color_vars, left_option.net, left_role)
                right_expr = _segment_color_expr(color_vars, right_option.net, right_role)
                if left_expr is None or right_expr is None:
                    continue
                pair_key = (
                    str(left_option.net),
                    str(left_role),
                    str(right_option.net),
                    str(right_role),
                    str(left_layer),
                )
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                exprs.append(
                    z3.Implies(
                        z3.And(left_selected_expr, right_selected_expr),
                        left_expr != right_expr,
                    )
                )
    return tuple(exprs)


def _route_state_soft_color_pair_terms(
    problem: NativeStdCellDetailedRouteProblem,
    left_option: _RouteStateOption,
    right_option: _RouteStateOption,
    *,
    left_selected_expr: object,
    right_selected_expr: object,
    color_vars: Mapping[object, object],
) -> tuple[object, ...]:
    spacing_by_layer = {
        str(layer): float(value)
        for layer, value in dict(problem.metadata.get("same_color_spacing_um", {})).items()
    }
    terms: list[object] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for left_layer, left_bbox, left_role in left_option.shapes:
        threshold = spacing_by_layer.get(left_layer)
        if threshold is None or threshold <= 0.0:
            continue
        for right_layer, right_bbox, right_role in right_option.shapes:
            if left_layer != right_layer:
                continue
            if bbox_overlaps(left_bbox, right_bbox, include_touching=True):
                continue
            distance = _bbox_distance_um(left_bbox, right_bbox)
            soft_limit = threshold * 2.0
            if distance + 1e-12 < threshold or distance > soft_limit:
                continue
            left_expr = _segment_color_expr(color_vars, left_option.net, left_role)
            right_expr = _segment_color_expr(color_vars, right_option.net, right_role)
            if left_expr is None or right_expr is None:
                continue
            pair_key = (
                str(left_option.net),
                str(left_role),
                str(right_option.net),
                str(right_role),
                str(left_layer),
                f"{distance:.6f}",
            )
            if pair_key in seen:
                continue
            seen.add(pair_key)
            penalty = _soft_same_color_penalty(distance, threshold)
            if penalty <= 0:
                continue
            terms.append(
                z3.If(
                    z3.And(left_selected_expr, right_selected_expr, left_expr == right_expr),
                    penalty,
                    0,
                )
            )
    return tuple(terms)


def _point_bbox(
    xy: tuple[float, float],
    half: float,
) -> BBox:
    return (xy[0] - half, xy[1] - half, xy[0] + half, xy[1] + half)


def _polyline_bboxes(
    layer: str,
    points: tuple[tuple[float, float], ...],
    *,
    width: float,
    color_role: str,
) -> tuple[tuple[str, BBox, str], ...]:
    half = width / 2.0
    compact: list[tuple[float, float]] = []
    for point in points:
        if compact and abs(compact[-1][0] - point[0]) <= 1e-9 and abs(compact[-1][1] - point[1]) <= 1e-9:
            continue
        compact.append(point)
    shapes: list[tuple[str, BBox, str]] = []
    for left, right in zip(compact, compact[1:]):
        shapes.append(
            (
                layer,
                (
                    min(left[0], right[0]) - half,
                    min(left[1], right[1]) - half,
                    max(left[0], right[0]) + half,
                    max(left[1], right[1]) + half,
                ),
                color_role,
            )
        )
    return tuple(shapes)


def _n7_min_same_color_spacing_um(layer: str) -> float | None:
    spacing = {
        "M0": 0.06,
        "M1": 0.084,
        "M2": 0.06,
    }
    return spacing.get(str(layer))


def _extend_vertical_segment_bbox(
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
        return (start_xy[0], start_xy[1] + direction * delta), end_xy
    direction = 1.0 if end_xy[1] > start_xy[1] else -1.0
    return start_xy, (end_xy[0], end_xy[1] + direction * delta)


def _dedupe_route_shapes(
    shapes: tuple[tuple[str, BBox, str], ...],
) -> tuple[tuple[str, BBox, str], ...]:
    unique: list[tuple[str, BBox, str]] = []
    seen: set[tuple[str, str]] = set()
    for layer, bbox, color_role in shapes:
        bbox_key = ",".join(f"{float(value):.9f}" for value in bbox)
        key = (str(layer), bbox_key)
        if key in seen:
            continue
        seen.add(key)
        unique.append((layer, bbox, color_role))
    return tuple(unique)


def _bbox_distance_um(a: BBox, b: BBox) -> float:
    if bbox_overlaps(a, b, include_touching=True):
        return 0.0
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _segment_color_expr(
    color_vars: Mapping[object, object],
    net: str,
    role: str,
    fallback_var: object | None = None,
) -> object | None:
    value = color_vars.get((str(net), str(role)))
    if value is not None:
        return value
    if fallback_var is not None:
        return fallback_var
    return color_vars.get(str(net))


def _is_n7_pdk(pdk: "PdkConfig") -> bool:
    return str(getattr(pdk, "name", "")).lower() == "tsmcn7"


def _color_candidates_for_layer(layer: str, pdk: "PdkConfig") -> tuple[str, ...]:
    if not _is_n7_pdk(pdk):
        return ()
    if str(layer) not in {"M0", "M1", "M2"}:
        return ()
    return ("mask1Color", "mask2Color")


def _color_roles_for_net_role(role: str) -> tuple[str, ...]:
    if role == "input":
        return ("contact", "pin")
    if role == "internal":
        return ("left_branch", "bridge", "right_branch")
    if role == "output":
        return ("trunk", "pin_landing", "pin_stub", "nmos_branch", "pmos_branch")
    if role == "power":
        return ("rail", "drop", "shared_drop")
    return ("default",)


def _color_assignment_penalty(net_role: str, color_role: str, color_value: str) -> int:
    if color_value not in {"mask1Color", "mask2Color"}:
        return 0
    preferred: dict[tuple[str, str], str] = {
        ("input", "contact"): "mask1Color",
        ("input", "pin"): "mask1Color",
        ("internal", "left_branch"): "mask1Color",
        ("internal", "bridge"): "mask1Color",
        ("internal", "right_branch"): "mask2Color",
        ("output", "trunk"): "mask2Color",
        ("output", "pin_landing"): "mask2Color",
        ("output", "pin_stub"): "mask2Color",
        ("output", "nmos_branch"): "mask2Color",
        ("output", "pmos_branch"): "mask2Color",
        ("power", "rail"): "mask1Color",
        ("power", "drop"): "mask1Color",
        ("power", "shared_drop"): "mask1Color",
    }
    target = preferred.get((str(net_role), str(color_role)))
    if target is None:
        return 0
    return 0 if color_value == target else 2


def _same_color_spacing_um(pdk: "PdkConfig", layer: str) -> float:
    if _is_n7_pdk(pdk):
        advanced_spacing = _n7_min_same_color_spacing_um(layer)
        if advanced_spacing is not None:
            return advanced_spacing
    rules = getattr(pdk, "rules", None)
    if rules is None:
        return 0.06
    try:
        return float(rules.min_spacing_um(layer))
    except Exception:
        try:
            return float(rules.min_width_um(layer))
        except Exception:
            return 0.06


def _soft_same_color_penalty(distance_um: float, threshold_um: float) -> int:
    if threshold_um <= 0.0:
        return 0
    soft_limit = threshold_um * 2.0
    if distance_um <= threshold_um or distance_um > soft_limit:
        return 0
    proximity = (soft_limit - distance_um) / threshold_um
    return int(round(max(proximity, 0.0) * 18.0))


def _enumerate_net_trunk_options(
    net_problem: NativeStdCellDetailedRouteNet,
) -> tuple[tuple[int, int, str, BBox], ...]:
    x_indices = tuple(range(len(net_problem.trunk_x_candidates))) if net_problem.trunk_x_candidates else (0,)
    y_indices = tuple(range(len(net_problem.trunk_y_candidates))) if net_problem.trunk_y_candidates else (0,)
    anchor_xs = [float(anchor.default_xy[0]) for anchor in net_problem.anchors]
    anchor_ys = [float(anchor.default_xy[1]) for anchor in net_problem.anchors]
    width = 0.06
    options: list[tuple[int, int, str, BBox]] = []
    for x_index in x_indices:
        trunk_x = float(net_problem.trunk_x_candidates[x_index]) if net_problem.trunk_x_candidates else 0.0
        for y_index in y_indices:
            trunk_y = float(net_problem.trunk_y_candidates[y_index]) if net_problem.trunk_y_candidates else 0.0
            bbox = _trunk_option_bbox(net_problem, trunk_x, trunk_y, anchor_xs, anchor_ys, width=width)
            options.append((x_index, y_index, net_problem.route_layer, bbox))
    return tuple(options)


def _trunk_option_bbox(
    net_problem: NativeStdCellDetailedRouteNet,
    trunk_x: float,
    trunk_y: float,
    anchor_xs: list[float],
    anchor_ys: list[float],
    *,
    width: float,
) -> BBox:
    half = width / 2.0
    if net_problem.role == "input":
        return (trunk_x - half, trunk_y - half, trunk_x + half, trunk_y + half)
    if net_problem.role == "output":
        left = min([trunk_x, *(anchor_xs or [trunk_x])])
        right = max(anchor_xs or [trunk_x])
        return (left - half, trunk_y - half, right + half, trunk_y + half)
    left = min(anchor_xs or [trunk_x])
    right = max(anchor_xs or [trunk_x])
    return (left - half, trunk_y - half, right + half, trunk_y + half)


def _option_selected_expr(
    x_var: object | None,
    y_var: object | None,
    x_index: int,
    y_index: int,
) -> object:
    clauses = []
    if x_var is not None:
        clauses.append(x_var == x_index)
    if y_var is not None:
        clauses.append(y_var == y_index)
    if not clauses:
        return z3.BoolVal(True)
    if len(clauses) == 1:
        return clauses[0]
    return z3.And(clauses)


def _unique_candidate_values(values: tuple[float, ...]) -> tuple[float, ...]:
    unique: list[float] = []
    for value in values:
        numeric = float(value)
        if any(abs(existing - numeric) <= 1e-9 for existing in unique):
            continue
        unique.append(numeric)
    return tuple(unique)


def _preferred_output_trunk_x_candidates(
    default_x: float,
    anchors: tuple[NativeStdCellDetailedRouteAnchor, ...],
) -> tuple[float, ...]:
    best_xs: list[float] = []
    for anchor in anchors:
        if anchor.role != "sd_access":
            continue
        if not anchor.candidates:
            continue
        best_candidate = min(
            anchor.candidates,
            key=lambda candidate: (int(candidate.cost), abs(float(candidate.xy[0]) - float(anchor.default_xy[0]))),
        )
        numeric = float(best_candidate.xy[0])
        if any(abs(existing - numeric) <= 1e-9 for existing in best_xs):
            continue
        best_xs.append(numeric)
    signal_xs = tuple(sorted(best_xs))
    ordered: list[float] = []
    if signal_xs:
        ordered.append(min(signal_xs))
        ordered.append(signal_xs[len(signal_xs) // 2])
        ordered.append(sum(signal_xs) / float(len(signal_xs)))
        ordered.append(max(signal_xs))
        span_left = min(signal_xs)
        span_right = max(signal_xs)
        # Reference 7nm stdcells keep the local output collector inside the
        # device-access channel and use a higher-layer pin stub for boundary
        # reach.  Drop far-right boundary trunks from the SMT domain once they
        # sit well outside the local output-access span.
        if span_left - 0.08 <= float(default_x) <= span_right + 0.08:
            ordered.append(float(default_x))
    else:
        ordered.append(float(default_x))
    return _unique_candidate_values(tuple(ordered))


def _preferred_output_trunk_y_candidates(
    pin_y: float,
    preferred_bus_y: float,
    anchors: tuple[NativeStdCellDetailedRouteAnchor, ...],
) -> tuple[float, ...]:
    anchor_ys = [
        float(anchor.default_xy[1])
        for anchor in anchors
        if anchor.role == "sd_access"
    ]
    if not anchor_ys:
        return _unique_candidate_values((float(pin_y), float(preferred_bus_y)))
    lower = min(anchor_ys)
    upper = max(anchor_ys)
    midpoint = (lower + upper) / 2.0
    ordered = (
        float(preferred_bus_y),
        float(pin_y),
        midpoint,
        lower,
    )
    return _unique_candidate_values(ordered)


def _preferred_internal_bridge_x_candidates(
    left_xy: tuple[float, float],
    right_xy: tuple[float, float],
) -> tuple[float | None, ...]:
    left_x = float(left_xy[0])
    right_x = float(right_xy[0])
    midpoint = (left_x + right_x) / 2.0
    return (None, *_unique_candidate_values((left_x, right_x, midpoint)))


def _preferred_power_bridge_x_candidates(
    anchors: tuple[NativeStdCellDetailedRouteAnchor, ...],
) -> tuple[float | None, ...]:
    access_xs = [float(anchor.default_xy[0]) for anchor in anchors if anchor.role == "sd_access"]
    if not access_xs:
        return ()
    ordered = [min(access_xs), max(access_xs), sum(access_xs) / float(len(access_xs))]
    return (None, *_unique_candidate_values(tuple(ordered)))


def _same_device_supply_signal_order_penalty(
    problem: NativeStdCellDetailedRouteProblem,
    left_anchor: NativeStdCellDetailedRouteAnchor | None,
    right_anchor: NativeStdCellDetailedRouteAnchor | None,
    left_xy: tuple[float, float],
    right_xy: tuple[float, float],
) -> int:
    if left_anchor is None or right_anchor is None:
        return 0
    if left_anchor.instance != right_anchor.instance:
        return 0
    supply_nets = {"VDD", "VSS"}
    left_is_supply = left_anchor.net in supply_nets
    right_is_supply = right_anchor.net in supply_nets
    if left_is_supply == right_is_supply:
        return 0
    if {left_anchor.terminal, right_anchor.terminal} != {"S", "D"}:
        return 0
    left_x, _, right_x, _ = problem.route_templates.cell_bbox_um
    cell_center_x = (float(left_x) + float(right_x)) / 2.0
    instance_center_x = (float(left_anchor.default_xy[0]) + float(right_anchor.default_xy[0])) / 2.0
    signal_x = float(right_xy[0] if left_is_supply else left_xy[0])
    supply_x = float(left_xy[0] if left_is_supply else right_xy[0])
    inward_margin = 0.04
    if instance_center_x <= cell_center_x:
        return 0 if signal_x >= supply_x + inward_margin else 400
    return 0 if signal_x <= supply_x - inward_margin else 400
