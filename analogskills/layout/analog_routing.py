"""Advanced-node analog routing partition helpers.

This module does not attempt to route analog blocks directly. Instead, it
builds a stable planning contract that separates:

- template-routable regions such as power, guard, and long escape trunks
- local high-density regions that should be handed to an SMT legalizer
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import inf
from typing import Mapping, Sequence

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None

from analogskills.contracts import LayoutConstraintSet, NetRole, RoutingConstraint, TopologyGraph
from analogskills.layout.constraints import (
    build_pin_alias_map,
    build_routing_intent_set,
    normalize_routing_constraints as shared_normalize_routing_constraints,
    normalized_critical_nets as shared_normalized_critical_nets,
    resolve_layout_net_name as shared_resolve_layout_net_name,
)


@dataclass(frozen=True)
class AnalogCriticalNetCluster:
    name: str
    nets: tuple[str, ...]
    boundary_pins: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    quiet_nets: tuple[str, ...] = ()
    policy_kinds: tuple[str, ...] = ()
    requires_local_smt: bool = False
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalogRouteRegion:
    name: str
    nets: tuple[str, ...]
    boundary_pins: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    critical_clusters: tuple[str, ...] = ()
    template_nets: tuple[str, ...] = ()
    quiet_nets: tuple[str, ...] = ()
    strategy: str = "template"
    notes: str = ""


@dataclass(frozen=True)
class AnalogLocalSmtPatchRegion:
    name: str
    parent_region: str
    nets: tuple[str, ...]
    boundary_pins: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdvancedAnalogRoutingPlan:
    graph_name: str
    normalized_critical_nets: tuple[str, ...]
    template_nets: tuple[str, ...]
    pin_alias_map: Mapping[str, str] = field(default_factory=dict)
    critical_net_clusters: tuple[AnalogCriticalNetCluster, ...] = ()
    route_regions: tuple[AnalogRouteRegion, ...] = ()
    local_smt_patch_regions: tuple[AnalogLocalSmtPatchRegion, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AnalogLocalSmtNetConstraint:
    net: str
    role: str
    allowed_layers: tuple[str, ...]
    allowed_tracks: tuple[int, ...]
    min_spacing_tracks: int = 1
    differential_partners: tuple[str, ...] = ()
    avoid_nets: tuple[str, ...] = ()
    preferred_track: int | None = None
    shield_required: bool = False
    boundary_pin: str = ""
    notes: str = ""


@dataclass(frozen=True)
class AnalogLocalSmtProblem:
    graph_name: str
    patch_name: str
    layers: tuple[str, ...]
    track_count: int
    net_constraints: tuple[AnalogLocalSmtNetConstraint, ...]
    quiet_nets: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AnalogLocalSmtSolution:
    assignments: tuple[tuple[str, str, int], ...]
    cost: float
    metadata: Mapping[str, object] = field(default_factory=dict)

    def assignment_map(self) -> dict[str, tuple[str, int]]:
        return {net: (layer, track) for net, layer, track in self.assignments}


@dataclass(frozen=True)
class AnalogLocalSmtSolveStats:
    backend: str = "dfs"
    solver_checks: int = 0
    branch_nodes: int = 0
    feasible_solutions: int = 0
    pruned_states: int = 0
    prune_reasons: tuple[tuple[str, int], ...] = ()
    incumbent_cost: float | None = None


@dataclass(frozen=True)
class AnalogLocalSmtSolveResult:
    problem: AnalogLocalSmtProblem
    solutions: tuple[AnalogLocalSmtSolution, ...]
    stats: AnalogLocalSmtSolveStats


@dataclass(frozen=True)
class AnalogLocalSmtAnchor:
    net: str
    xy_um: tuple[float, float]
    layer: str
    instance: str = ""
    terminal: str = ""
    source: str = ""
    lvs_safe: bool = True


@dataclass(frozen=True)
class AnalogLocalSmtSynthesisResult:
    plan: object
    track_templates: tuple[tuple[str, str, float, tuple[float, float]], ...]
    anchors: tuple[AnalogLocalSmtAnchor, ...]
    warnings: tuple[str, ...] = ()


def build_advanced_analog_routing_plan(
    graph: TopologyGraph,
    constraints: LayoutConstraintSet | None = None,
) -> AdvancedAnalogRoutingPlan:
    active = constraints or getattr(graph, "layout_constraints", None) or LayoutConstraintSet()
    pin_alias_map = build_pin_alias_map(graph)
    intent_set = build_routing_intent_set(active, graph=graph, pin_alias_map=pin_alias_map)
    normalized_constraints = intent_set.constraints
    policy_map = _routing_policy_map(normalized_constraints)
    partner_map = _differential_partner_map(normalized_constraints)
    net_devices = _net_devices(graph)
    template_nets = tuple(
        sorted(
            net.name
            for net in graph.nets.values()
            if net.role in {NetRole.SUPPLY, NetRole.GROUND}
        )
    )
    critical_nets = intent_set.critical_nets
    signal_critical_nets = tuple(net for net in critical_nets if net not in template_nets)
    clusters = _critical_clusters(signal_critical_nets, net_devices, partner_map)

    cluster_rows: list[AnalogCriticalNetCluster] = []
    region_rows: list[AnalogRouteRegion] = []
    patch_rows: list[AnalogLocalSmtPatchRegion] = []
    covered_signal_nets: set[str] = set()

    for idx, nets in enumerate(clusters):
        devices = _cluster_devices(nets, net_devices)
        region_nets = _cluster_region_nets(graph, devices, template_nets)
        covered_signal_nets.update(region_nets)
        boundary_pins = _boundary_pins_for_nets(region_nets, pin_alias_map)
        quiet_nets = tuple(sorted(net for net in region_nets if _is_quiet_net(graph, net, policy_map)))
        policy_kinds = tuple(
            sorted(
                {
                    kind
                    for net in region_nets
                    for kind in policy_map.get(net, ())
                }
            )
        )
        reasons = _cluster_reasons(graph, nets, region_nets, policy_map, partner_map)
        requires_local_smt = bool(reasons)
        cluster_name = f"critical_cluster_{idx}"
        cluster_rows.append(
            AnalogCriticalNetCluster(
                name=cluster_name,
                nets=tuple(sorted(nets)),
                boundary_pins=boundary_pins,
                devices=devices,
                quiet_nets=quiet_nets,
                policy_kinds=policy_kinds,
                requires_local_smt=requires_local_smt,
                reasons=reasons,
            )
        )
        region_name = f"analog_region_{idx}"
        region_rows.append(
            AnalogRouteRegion(
                name=region_name,
                nets=tuple(sorted(region_nets)),
                boundary_pins=boundary_pins,
                devices=devices,
                critical_clusters=(cluster_name,),
                template_nets=tuple(sorted(net for net in template_nets if any(_device_touches_net(graph, device, net) for device in devices))),
                quiet_nets=quiet_nets,
                strategy="local_smt" if requires_local_smt else "template",
                notes="critical analog cluster region",
            )
        )
        if requires_local_smt:
            patch_rows.append(
                AnalogLocalSmtPatchRegion(
                    name=f"local_smt_patch_{idx}",
                    parent_region=region_name,
                    nets=tuple(sorted(region_nets)),
                    boundary_pins=boundary_pins,
                    devices=devices,
                    reasons=reasons,
                )
            )

    if template_nets:
        power_devices = _cluster_devices(template_nets, net_devices)
        region_rows.insert(
            0,
            AnalogRouteRegion(
                name="template_power_distribution",
                nets=template_nets,
                boundary_pins=_boundary_pins_for_nets(template_nets, pin_alias_map),
                devices=power_devices,
                template_nets=template_nets,
                strategy="template",
                notes="power and ground should stay on template-routed infrastructure",
            ),
        )

    leftover_nets = tuple(
        sorted(
            net
            for net in graph.nets
            if net not in template_nets and net not in covered_signal_nets
        )
    )
    if leftover_nets:
        region_rows.append(
            AnalogRouteRegion(
                name="template_escape_region",
                nets=leftover_nets,
                boundary_pins=_boundary_pins_for_nets(leftover_nets, pin_alias_map),
                devices=_cluster_devices(leftover_nets, net_devices),
                strategy="template",
                notes="non-critical analog escape and long-distance interconnect",
            )
        )

    return AdvancedAnalogRoutingPlan(
        graph_name=graph.name,
        normalized_critical_nets=critical_nets,
        template_nets=template_nets,
        pin_alias_map=pin_alias_map,
        critical_net_clusters=tuple(cluster_rows),
        route_regions=tuple(region_rows),
        local_smt_patch_regions=tuple(patch_rows),
        metadata={
            "normalized_constraint_count": len(normalized_constraints),
            "critical_cluster_count": len(cluster_rows),
            "route_region_count": len(region_rows),
            "local_smt_patch_count": len(patch_rows),
        },
    )


def build_analog_local_smt_problem(
    graph: TopologyGraph,
    routing_plan: AdvancedAnalogRoutingPlan,
    patch_name: str,
    *,
    candidate_layers: Sequence[str] = ("M2", "M3", "M4"),
    track_count: int = 8,
) -> AnalogLocalSmtProblem:
    if track_count <= 0:
        raise ValueError("track_count must be positive")
    layers = tuple(str(layer) for layer in candidate_layers if str(layer))
    if not layers:
        raise ValueError("candidate_layers must not be empty")
    patch = next((item for item in routing_plan.local_smt_patch_regions if item.name == patch_name), None)
    if patch is None:
        raise ValueError(f"unknown analog local SMT patch: {patch_name!r}")
    active = getattr(graph, "layout_constraints", None) or LayoutConstraintSet()
    intent_set = build_routing_intent_set(active, graph=graph, pin_alias_map=routing_plan.pin_alias_map)
    normalized_constraints = intent_set.constraints
    policy_map = _routing_policy_map(normalized_constraints)
    partner_map = _differential_partner_map(normalized_constraints)
    avoid_map = _avoid_net_map(normalized_constraints)
    track_domain = tuple(range(track_count))
    net_constraints: list[AnalogLocalSmtNetConstraint] = []
    center_track = track_count // 2
    boundary_pin_map = {net: pin for pin, net in routing_plan.pin_alias_map.items()}

    for net in patch.nets:
        net_obj = graph.nets[net]
        policy_kinds = set(policy_map.get(net, ()))
        sensitive = net_obj.role in {NetRole.HIGH_Z, NetRole.REFERENCE, NetRole.DIFFERENTIAL}
        shield_required = "shield" in policy_kinds or sensitive
        wide_required = "wide" in policy_kinds
        route_layer = _fixed_route_layer_from_constraints(normalized_constraints, net)
        if route_layer:
            allowed_layers = (route_layer,) if route_layer in layers else layers
        elif shield_required or wide_required:
            allowed_layers = layers[1:] if len(layers) >= 2 else layers
        else:
            allowed_layers = layers
        allowed_tracks = track_domain[1:-1] if shield_required and track_count >= 3 else track_domain
        min_spacing = 2 if shield_required or wide_required else 1
        preferred_track = center_track if sensitive else None
        net_constraints.append(
            AnalogLocalSmtNetConstraint(
                net=net,
                role=str(net_obj.role.value if hasattr(net_obj.role, "value") else net_obj.role),
                allowed_layers=tuple(allowed_layers),
                allowed_tracks=tuple(allowed_tracks),
                min_spacing_tracks=min_spacing,
                differential_partners=tuple(sorted(partner for partner in partner_map.get(net, ()) if partner in patch.nets)),
                avoid_nets=tuple(sorted(peer for peer in avoid_map.get(net, ()) if peer in patch.nets)),
                preferred_track=preferred_track,
                shield_required=shield_required,
                boundary_pin=str(boundary_pin_map.get(net, "")),
                notes="sensitive analog patch net" if sensitive else "",
            )
        )

    return AnalogLocalSmtProblem(
        graph_name=graph.name,
        patch_name=patch_name,
        layers=layers,
        track_count=track_count,
        net_constraints=tuple(sorted(net_constraints, key=lambda item: (len(item.allowed_layers) * len(item.allowed_tracks), item.net))),
        quiet_nets=tuple(sorted(net for net in patch.nets if _is_quiet_net(graph, net, policy_map))),
        metadata={
            "boundary_pins": patch.boundary_pins,
            "devices": patch.devices,
            "reasons": patch.reasons,
        },
    )


def solve_analog_local_smt(
    problem: AnalogLocalSmtProblem,
    *,
    max_solutions: int = 1,
    backend: str = "auto",
) -> AnalogLocalSmtSolveResult:
    if max_solutions <= 0:
        raise ValueError("max_solutions must be positive")
    selected_backend = _select_analog_solver_backend(backend)
    if selected_backend == "z3":
        return _solve_analog_local_smt_z3(problem, max_solutions=max_solutions)
    return _solve_analog_local_smt_dfs(problem, max_solutions=max_solutions)


def collect_analog_local_smt_anchors(
    graph: TopologyGraph,
    routing_plan: AdvancedAnalogRoutingPlan,
    patch_name: str,
    pcell_plan: object,
    pdk: object,
    *,
    calibration_cache: object | None = None,
) -> tuple[AnalogLocalSmtAnchor, ...]:
    from analogskills.pcell import PCellTerminalAccessor

    patch = next((item for item in routing_plan.local_smt_patch_regions if item.name == patch_name), None)
    if patch is None:
        raise ValueError(f"unknown analog local SMT patch: {patch_name!r}")
    accessor = PCellTerminalAccessor(pdk, calibration_cache=calibration_cache)
    instance_by_name = {str(instance.name): instance for instance in getattr(pcell_plan, "instances", ())}
    anchors: list[AnalogLocalSmtAnchor] = []
    seen: set[tuple[str, str, str]] = set()
    for net in patch.nets:
        for terminal in graph.nets[net].terminals:
            if terminal.device not in graph.devices:
                continue
            if terminal.device not in patch.devices:
                continue
            instance = instance_by_name.get(str(terminal.device))
            if instance is None:
                continue
            key = (net, str(terminal.device), str(terminal.terminal))
            if key in seen:
                continue
            seen.add(key)
            try:
                pin = accessor.select_terminal_breakout(instance, terminal.terminal, require_lvs_safe=True)
            except Exception:
                continue
            anchors.append(
                AnalogLocalSmtAnchor(
                    net=net,
                    xy_um=tuple(pin.xy_um),
                    layer=str(pin.layer),
                    instance=str(terminal.device),
                    terminal=str(terminal.terminal),
                    source=str(pin.source),
                    lvs_safe=bool(pin.lvs_safe),
                )
            )
    return tuple(sorted(anchors, key=lambda item: (item.net, item.instance, item.terminal, item.xy_um)))


def synthesize_analog_local_smt_template_plan(
    problem: AnalogLocalSmtProblem,
    solution: AnalogLocalSmtSolution,
    anchors: Sequence[AnalogLocalSmtAnchor],
    *,
    lib: str = "work",
    cell: str = "analog_local_smt_template",
    view: str = "layout",
    route_width_um: float = 0.1,
    track_pitch_um: float = 0.4,
    track_origin_y_um: float | None = None,
    stub_length_um: float = 0.2,
    pdk: object | None = None,
) -> AnalogLocalSmtSynthesisResult:
    from analogskills.eda.oa import OaCellView, OaPath, OaWritePlan, snap_oa_write_plan_to_grid

    assignment_map = solution.assignment_map()
    anchors_by_net: dict[str, list[AnalogLocalSmtAnchor]] = {}
    for anchor in anchors:
        anchors_by_net.setdefault(str(anchor.net), []).append(anchor)
    all_anchor_y = [anchor.xy_um[1] for anchor in anchors]
    origin_y = float(track_origin_y_um) if track_origin_y_um is not None else ((min(all_anchor_y) - track_pitch_um) if all_anchor_y else 0.0)
    paths = []
    templates: list[tuple[str, str, float, tuple[float, float]]] = []
    warnings: list[str] = []

    for net, (layer, track) in sorted(assignment_map.items()):
        net_anchors = sorted(anchors_by_net.get(net, ()), key=lambda item: (item.xy_um[0], item.xy_um[1]))
        y = origin_y + track * float(track_pitch_um)
        if net_anchors:
            xs = [anchor.xy_um[0] for anchor in net_anchors]
            x0 = min(xs) - stub_length_um / 2.0
            x1 = max(xs) + stub_length_um / 2.0
            for anchor in net_anchors:
                if anchor.layer != layer:
                    warnings.append(
                        f"template preview keeps net {net} on {layer} but anchor {anchor.instance}.{anchor.terminal} is on {anchor.layer}; landing/via synthesis still required"
                    )
        else:
            x0 = 0.0
            x1 = stub_length_um
            warnings.append(f"net {net} has no collected terminal anchors; template preview uses synthetic span")
        points = ((float(x0), float(y)), (float(x1), float(y)))
        paths.append(OaPath(layer, "drawing", points, float(route_width_um), net))
        templates.append((net, layer, float(y), (float(x0), float(x1))))

    plan = OaWritePlan(
        OaCellView(lib, cell, view, "maskLayout"),
        nets=tuple(sorted(assignment_map)),
        paths=tuple(paths),
    )
    if pdk is not None:
        plan = snap_oa_write_plan_to_grid(plan, pdk)
    return AnalogLocalSmtSynthesisResult(
        plan=plan,
        track_templates=tuple(templates),
        anchors=tuple(anchors),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _pin_alias_map(graph: TopologyGraph) -> dict[str, str]:
    return build_pin_alias_map(graph)


def _resolve_net_name(name: str, graph: TopologyGraph, pin_alias_map: Mapping[str, str]) -> str:
    resolved = shared_resolve_layout_net_name(name, graph=graph, pin_alias_map=pin_alias_map)
    return resolved if resolved in graph.nets else ""


def _normalized_critical_nets(
    graph: TopologyGraph,
    constraints: LayoutConstraintSet,
    pin_alias_map: Mapping[str, str],
) -> tuple[str, ...]:
    return shared_normalized_critical_nets(constraints, graph=graph, pin_alias_map=pin_alias_map)


def _normalize_routing_constraints(
    constraints: tuple[RoutingConstraint, ...],
    graph: TopologyGraph,
    pin_alias_map: Mapping[str, str],
) -> tuple[RoutingConstraint, ...]:
    return shared_normalize_routing_constraints(constraints, graph=graph, pin_alias_map=pin_alias_map)


def _routing_policy_map(constraints: tuple[RoutingConstraint, ...]) -> dict[str, tuple[str, ...]]:
    policy: dict[str, set[str]] = {}
    for item in constraints:
        policy.setdefault(str(item.net), set()).add(str(item.kind))
    return {net: tuple(sorted(kinds)) for net, kinds in sorted(policy.items())}


def _avoid_net_map(constraints: tuple[RoutingConstraint, ...]) -> dict[str, tuple[str, ...]]:
    avoid: dict[str, set[str]] = {}
    for item in constraints:
        if str(item.kind) != "avoid_nets":
            continue
        if isinstance(item.value, str):
            values = (str(item.value),)
        elif isinstance(item.value, tuple):
            values = tuple(str(value) for value in item.value if str(value))
        else:
            values = ()
        if values:
            avoid.setdefault(str(item.net), set()).update(values)
    return {net: tuple(sorted(values)) for net, values in sorted(avoid.items())}


def _differential_partner_map(constraints: tuple[RoutingConstraint, ...]) -> dict[str, tuple[str, ...]]:
    partners: dict[str, set[str]] = {}
    for item in constraints:
        if str(item.kind) != "differential_partner":
            continue
        values = (item.value,) if isinstance(item.value, str) else tuple(item.value if isinstance(item.value, tuple) else ())
        for value in values:
            if isinstance(value, str) and value:
                partners.setdefault(str(item.net), set()).add(str(value))
    return {net: tuple(sorted(items)) for net, items in sorted(partners.items())}


def _net_devices(graph: TopologyGraph) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for net in graph.nets.values():
        devices = sorted(
            {
                str(term.device)
                for term in net.terminals
                if term.device in graph.devices
            }
        )
        result[net.name] = tuple(devices)
    return result


def _critical_clusters(
    critical_nets: tuple[str, ...],
    net_devices: Mapping[str, tuple[str, ...]],
    partner_map: Mapping[str, tuple[str, ...]],
) -> tuple[tuple[str, ...], ...]:
    remaining = set(critical_nets)
    clusters: list[tuple[str, ...]] = []
    while remaining:
        root = min(remaining)
        queue = [root]
        component: set[str] = set()
        while queue:
            current = queue.pop()
            if current in component:
                continue
            component.add(current)
            shared = set(net_devices.get(current, ()))
            for other in tuple(remaining):
                if other in component:
                    continue
                if shared & set(net_devices.get(other, ())):
                    queue.append(other)
            for partner in partner_map.get(current, ()):
                if partner in remaining:
                    queue.append(partner)
        remaining -= component
        clusters.append(tuple(sorted(component)))
    return tuple(sorted(clusters))


def _cluster_devices(nets: tuple[str, ...], net_devices: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(sorted({device for net in nets for device in net_devices.get(net, ())}))


def _cluster_region_nets(graph: TopologyGraph, devices: tuple[str, ...], template_nets: tuple[str, ...]) -> tuple[str, ...]:
    nets: set[str] = set()
    for device in devices:
        for terminal in graph.devices[device].terminals:
            net = graph.get_net_for(device, terminal)
            if net and net not in template_nets:
                nets.add(str(net))
    return tuple(sorted(nets))


def _boundary_pins_for_nets(nets: tuple[str, ...], pin_alias_map: Mapping[str, str]) -> tuple[str, ...]:
    net_set = set(nets)
    return tuple(sorted(pin for pin, net in pin_alias_map.items() if net in net_set))


def _is_quiet_net(graph: TopologyGraph, net: str, policy_map: Mapping[str, tuple[str, ...]]) -> bool:
    role = getattr(graph.nets.get(net), "role", None)
    return bool(role in {NetRole.HIGH_Z, NetRole.REFERENCE} or "shield" in policy_map.get(net, ()))


def _cluster_reasons(
    graph: TopologyGraph,
    cluster_nets: tuple[str, ...],
    region_nets: tuple[str, ...],
    policy_map: Mapping[str, tuple[str, ...]],
    partner_map: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if len(cluster_nets) > 1:
        reasons.append("multi_net_cluster")
    if any("shield" in policy_map.get(net, ()) for net in region_nets):
        reasons.append("shield_required")
    if any(policy in {"avoid_nets", "match_length_with"} for net in region_nets for policy in policy_map.get(net, ())):
        reasons.append("policy_constrained")
    if any(partner_map.get(net) for net in cluster_nets):
        reasons.append("differential_or_matched_pair")
    if any(graph.nets[net].role in {NetRole.HIGH_Z, NetRole.REFERENCE, NetRole.DIFFERENTIAL} for net in region_nets):
        reasons.append("sensitive_nets")
    if any(graph.nets[net].role == NetRole.INTERNAL for net in region_nets):
        reasons.append("internal_branching")
    return tuple(dict.fromkeys(reasons))


def _device_touches_net(graph: TopologyGraph, device: str, net: str) -> bool:
    return any(graph.get_net_for(device, terminal) == net for terminal in graph.devices[device].terminals)


def _fixed_route_layer_from_constraints(constraints: tuple[RoutingConstraint, ...], net: str) -> str:
    for item in constraints:
        if str(item.net) == str(net) and str(item.kind) == "route_layer" and isinstance(item.value, str) and item.value:
            return str(item.value)
    return ""


def _select_analog_solver_backend(backend: str) -> str:
    normalized = str(backend or "auto").strip().lower()
    if normalized not in {"auto", "z3", "dfs"}:
        raise ValueError(f"unsupported analog local SMT backend: {backend!r}")
    if normalized == "auto":
        return "z3" if z3 is not None else "dfs"
    if normalized == "z3" and z3 is None:
        raise RuntimeError("z3 backend requested but z3-solver is not installed")
    return normalized


def _solve_analog_local_smt_dfs(
    problem: AnalogLocalSmtProblem,
    *,
    max_solutions: int,
) -> AnalogLocalSmtSolveResult:
    constraints = {item.net: item for item in problem.net_constraints}
    order = tuple(item.net for item in sorted(problem.net_constraints, key=lambda item: (len(item.allowed_layers) * len(item.allowed_tracks), -len(item.differential_partners), item.net)))
    branch_nodes = 0
    pruned = 0
    feasible = 0
    best_cost = inf
    solutions: list[AnalogLocalSmtSolution] = []

    def add_solution(assignments: dict[str, tuple[str, int]]) -> None:
        nonlocal feasible, best_cost
        feasible += 1
        solution = _build_analog_solution(problem, assignments)
        solutions.append(solution)
        solutions.sort(key=lambda item: item.cost)
        del solutions[max_solutions:]
        if solutions:
            best_cost = min(best_cost, solutions[0].cost)

    def walk(index: int, assignments: dict[str, tuple[str, int]]) -> None:
        nonlocal branch_nodes, pruned
        branch_nodes += 1
        if index >= len(order):
            add_solution(assignments)
            return
        net = order[index]
        constraint = constraints[net]
        for layer in constraint.allowed_layers:
            for track in constraint.allowed_tracks:
                assignments[net] = (layer, track)
                if not _analog_assignment_feasible(problem, assignments):
                    pruned += 1
                    assignments.pop(net, None)
                    continue
                lower_bound = _analog_cost_lower_bound(problem, assignments)
                if lower_bound > best_cost:
                    pruned += 1
                    assignments.pop(net, None)
                    continue
                walk(index + 1, assignments)
                assignments.pop(net, None)

    walk(0, {})
    return AnalogLocalSmtSolveResult(
        problem=problem,
        solutions=tuple(solutions),
        stats=AnalogLocalSmtSolveStats(
            backend="dfs",
            solver_checks=0,
            branch_nodes=branch_nodes,
            feasible_solutions=feasible,
            pruned_states=pruned,
            incumbent_cost=solutions[0].cost if solutions else None,
        ),
    )


def _solve_analog_local_smt_z3(
    problem: AnalogLocalSmtProblem,
    *,
    max_solutions: int,
) -> AnalogLocalSmtSolveResult:
    assert z3 is not None
    constraint_by_net = {item.net: item for item in problem.net_constraints}
    layer_index = {layer: idx for idx, layer in enumerate(problem.layers)}
    solver = z3.Optimize()
    layer_vars = {item.net: z3.Int(f"analog_layer_{item.net}") for item in problem.net_constraints}
    track_vars = {item.net: z3.Int(f"analog_track_{item.net}") for item in problem.net_constraints}

    for item in problem.net_constraints:
        solver.add(z3.Or([layer_vars[item.net] == layer_index[layer] for layer in item.allowed_layers]))
        solver.add(z3.Or([track_vars[item.net] == track for track in item.allowed_tracks]))

    ordered_pairs: set[tuple[str, str]] = set()
    for item in problem.net_constraints:
        for partner in item.differential_partners:
            if partner not in layer_vars:
                continue
            key = tuple(sorted((item.net, partner)))
            if key in ordered_pairs:
                continue
            ordered_pairs.add(key)
            solver.add(layer_vars[item.net] == layer_vars[partner])
            solver.add(z3.Abs(track_vars[item.net] - track_vars[partner]) == 1)

    all_nets = tuple(item.net for item in problem.net_constraints)
    for idx, left in enumerate(all_nets):
        left_item = constraint_by_net[left]
        for right in all_nets[idx + 1 :]:
            right_item = constraint_by_net[right]
            spacing = _analog_pair_min_spacing(left_item, right_item)
            solver.add(
                z3.Implies(
                    layer_vars[left] == layer_vars[right],
                    z3.Abs(track_vars[left] - track_vars[right]) >= int(spacing),
                )
            )

    span_expr, _ = _analog_span_expr(solver, tuple(track_vars.values()), "analog_track_span")
    layer_cost_expr = z3.Int("analog_layer_cost")
    solver.add(layer_cost_expr == z3.Sum([layer_vars[item.net] for item in problem.net_constraints]) if problem.net_constraints else layer_cost_expr == 0)
    preference_cost_expr = z3.Int("analog_preference_cost")
    preference_terms = [
        z3.Abs(track_vars[item.net] - int(item.preferred_track))
        for item in problem.net_constraints
        if item.preferred_track is not None
    ]
    solver.add(preference_cost_expr == z3.Sum(preference_terms) if preference_terms else preference_cost_expr == 0)
    total_cost_expr = z3.Int("analog_total_cost")
    solver.add(total_cost_expr == span_expr * 100 + layer_cost_expr * 10 + preference_cost_expr)
    solver.minimize(total_cost_expr)

    solver_checks = 1
    if solver.check() != z3.sat:
        return AnalogLocalSmtSolveResult(
            problem=problem,
            solutions=(),
            stats=AnalogLocalSmtSolveStats(
                backend="z3",
                solver_checks=solver_checks,
                branch_nodes=0,
                feasible_solutions=0,
                pruned_states=0,
                incumbent_cost=None,
            ),
        )
    model = solver.model()
    assignments = {
        net: (problem.layers[model.eval(layer_vars[net], model_completion=True).as_long()], model.eval(track_vars[net], model_completion=True).as_long())
        for net in all_nets
    }
    solution = _build_analog_solution(
        problem,
        assignments,
        overrides={
            "track_span": model.eval(span_expr, model_completion=True).as_long(),
            "layer_cost": model.eval(layer_cost_expr, model_completion=True).as_long(),
            "preference_cost": model.eval(preference_cost_expr, model_completion=True).as_long(),
            "cost": model.eval(total_cost_expr, model_completion=True).as_long(),
        },
    )
    return AnalogLocalSmtSolveResult(
        problem=problem,
        solutions=(solution,),
        stats=AnalogLocalSmtSolveStats(
            backend="z3",
            solver_checks=solver_checks,
            branch_nodes=len(problem.net_constraints),
            feasible_solutions=min(1, max_solutions),
            pruned_states=0,
            incumbent_cost=solution.cost,
        ),
    )


def _build_analog_solution(
    problem: AnalogLocalSmtProblem,
    assignments: Mapping[str, tuple[str, int]],
    *,
    overrides: Mapping[str, object] | None = None,
) -> AnalogLocalSmtSolution:
    overrides = dict(overrides or {})
    track_values = [track for _, track in assignments.values()]
    track_span = int(overrides.get("track_span", (max(track_values) - min(track_values) + 1) if track_values else 0))
    layer_index = {layer: idx for idx, layer in enumerate(problem.layers)}
    layer_cost = int(overrides.get("layer_cost", sum(layer_index[layer] for layer, _track in assignments.values())))
    preferred = {item.net: item.preferred_track for item in problem.net_constraints if item.preferred_track is not None}
    preference_cost = int(
        overrides.get(
            "preference_cost",
            sum(abs(assignments[net][1] - preferred_track) for net, preferred_track in preferred.items() if net in assignments),
        )
    )
    cost = float(overrides.get("cost", track_span * 100 + layer_cost * 10 + preference_cost))
    return AnalogLocalSmtSolution(
        assignments=tuple(sorted((net, layer, int(track)) for net, (layer, track) in assignments.items())),
        cost=cost,
        metadata={
            "track_span": track_span,
            "layer_cost": layer_cost,
            "preference_cost": preference_cost,
            **overrides,
        },
    )


def _analog_assignment_feasible(problem: AnalogLocalSmtProblem, assignments: Mapping[str, tuple[str, int]]) -> bool:
    constraint_by_net = {item.net: item for item in problem.net_constraints}
    for net, (layer, track) in assignments.items():
        item = constraint_by_net[net]
        if layer not in item.allowed_layers or track not in item.allowed_tracks:
            return False
        for partner in item.differential_partners:
            if partner in assignments:
                p_layer, p_track = assignments[partner]
                if p_layer != layer or abs(p_track - track) != 1:
                    return False
        for other, (other_layer, other_track) in assignments.items():
            if other == net:
                continue
            other_item = constraint_by_net[other]
            if other_layer != layer:
                continue
            spacing = _analog_pair_min_spacing(item, other_item)
            if abs(other_track - track) < spacing:
                return False
    return True


def _analog_cost_lower_bound(problem: AnalogLocalSmtProblem, assignments: Mapping[str, tuple[str, int]]) -> float:
    if not assignments:
        return 0.0
    track_values = [track for _layer, track in assignments.values()]
    track_span = max(track_values) - min(track_values) + 1
    layer_index = {layer: idx for idx, layer in enumerate(problem.layers)}
    layer_cost = sum(layer_index[layer] for layer, _track in assignments.values())
    preferred = {item.net: item.preferred_track for item in problem.net_constraints if item.preferred_track is not None}
    preference_cost = sum(abs(assignments[net][1] - preferred_track) for net, preferred_track in preferred.items() if net in assignments)
    return float(track_span * 100 + layer_cost * 10 + preference_cost)


def _analog_pair_min_spacing(left: AnalogLocalSmtNetConstraint, right: AnalogLocalSmtNetConstraint) -> int:
    if right.net in left.differential_partners or left.net in right.differential_partners:
        spacing = 1
    else:
        spacing = max(left.min_spacing_tracks, right.min_spacing_tracks)
    if right.net in left.avoid_nets or left.net in right.avoid_nets:
        spacing += 1
    return int(spacing)


def _analog_span_expr(optimizer: object, vars_: tuple[object, ...], prefix: str) -> tuple[object, Mapping[str, object]]:
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
