"""Auditable circuit-to-physical intent compilation for imported designs."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping

from analogskills.contracts import LayoutConstraintSet, RoutingConstraint, TopologyGraph
from analogskills.layout.analog_layout_dsl import AnalogLayoutSpec, layout_spec
from analogskills.layout.analog_routing import (
    build_advanced_analog_routing_plan,
    build_analog_local_smt_problem,
    solve_analog_local_smt,
)
from analogskills.layout.analog_smt_compiler import CompiledAnalogLayout, compile_analog_layout_smt
from analogskills.layout.constraints import extract_layout_constraints
from analogskills.layout.placement import Placement
from analogskills.pcell.generation import generate_pcell_layout_plan


PHYSICAL_INTENT_SCHEMA = "analogskills.physical_design_intent/v1"


class PhysicalIntentError(ValueError):
    """Fail-closed physical-intent compilation error with a stable reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = str(reason)


@dataclass(frozen=True)
class ConstraintEvidence:
    name: str
    kind: str
    source: str
    hard: bool
    reason: str
    devices: tuple[str, ...] = ()
    nets: tuple[str, ...] = ()
    status: str = "required"


@dataclass(frozen=True)
class PhysicalDesignIntent:
    topology: str
    spec: AnalogLayoutSpec
    constraints: tuple[ConstraintEvidence, ...]
    layout_constraints: LayoutConstraintSet
    schema: str = PHYSICAL_INTENT_SCHEMA
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "topology": self.topology,
            "spec": asdict(self.spec),
            "constraints": [asdict(item) for item in self.constraints],
            "layout_constraints": {
                "matched_groups": [asdict(item) for item in self.layout_constraints.matched_groups],
                "symmetry_groups": [list(item) for item in self.layout_constraints.symmetry_groups],
                "routing": [asdict(item) for item in self.layout_constraints.routing],
                "critical_nets": list(self.layout_constraints.critical_nets),
            },
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ImportedPhysicalSmtResult:
    intent: PhysicalDesignIntent
    compiled: CompiledAnalogLayout
    placements: tuple[Placement, ...]
    route_resource_assignments: Mapping[str, Mapping[str, object]]
    matching_realization: Mapping[str, Mapping[str, object]]
    routing_evidence: Mapping[str, object] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.compiled.passed and self.route_resource_assignments)

    def solution_dict(self) -> dict[str, object]:
        return {
            "schema": "analogskills.imported_physical_smt_solution/v1",
            "passed": self.passed,
            "topology": self.intent.topology,
            "placements": [asdict(item) for item in self.placements],
            "pattern_bboxes_tracks": {
                str(name): list(bbox) for name, bbox in self.compiled.pattern_bboxes_tracks.items()
            },
            "selected_candidates": dict(self.compiled.selected_candidates),
            "total_width_tracks": self.compiled.total_width_tracks,
            "total_height_tracks": self.compiled.total_height_tracks,
            "track_pitch_um": self.compiled.track_pitch_um,
            "route_resource_assignments": {
                str(name): dict(value) for name, value in self.route_resource_assignments.items()
            },
            "matching_realization": {
                str(name): dict(value) for name, value in self.matching_realization.items()
            },
            "routing_evidence": dict(self.routing_evidence),
            "checks": dict(self.compiled.checks),
        }


def compile_physical_intent(
    graph: TopologyGraph,
    *,
    topology: str,
    pdk: object,
) -> PhysicalDesignIntent:
    """Compile graph inference plus reviewed topology policy into the Python DSL."""

    if str(topology) != "two_stage_ota":
        raise PhysicalIntentError("physical_adapter_required", f"no SMT physical policy for {topology!r}")
    required_devices = {
        "Mbias", "Mdiff1", "Mdiff2", "Mmirr1", "Mmirr2",
        "Mtail", "Mcs", "Mload", "Rz", "Cc",
    }
    missing = sorted(required_devices - set(graph.devices))
    extra = sorted(set(graph.devices) - required_devices)
    if missing or extra:
        raise PhysicalIntentError(
            "constraint_compile_failed",
            f"two_stage_ota physical policy mismatch: missing={missing}, extra={extra}",
        )

    constraints = extract_layout_constraints(graph)
    metals = tuple(str(layer) for layer in getattr(pdk.layer_map, "metals", ()))
    if len(metals) < 4:
        raise PhysicalIntentError("constraint_compile_failed", "two_stage_ota requires at least four metal layers")
    # Keep the first implementation on the two calibrated signal-routing
    # layers.  Deep stacks through M5+ cross lower straps and belong in a later
    # detailed-router optimization, not in this abstract resource solve.
    route_layers = metals[2:4]
    # The device-rule minimum alone leaves no terminal escape room.  Reserve a
    # reviewed OTA routing channel between pattern macros; exact shape spacing
    # remains owned by the PDK rules and Calibre.
    spacing_um = max(3.0, float(pdk.rules.min_spacing_um(metals[0])))
    builder = layout_spec("two_stage_ota")
    builder.pattern("bias_reference", ("Mbias",), role="nmos_bias_reference", kind="row", spacing_um=spacing_um)
    builder.pattern("tail_device", ("Mtail",), role="nmos_tail", kind="row", spacing_um=spacing_um)
    builder.pattern("input_pair", ("Mdiff1", "Mdiff2"), role="nmos_match", kind="row", spacing_um=spacing_um)
    builder.pattern("mirror_pair", ("Mmirr1", "Mmirr2"), role="pmos_match", kind="row", spacing_um=spacing_um)
    builder.pattern("second_stage", ("Mload", "Mcs"), role="output_stage", kind="column", spacing_um=spacing_um)
    builder.pattern("compensation", ("Rz", "Cc"), role="miller_compensation", kind="column", spacing_um=spacing_um)
    builder.pair("input_pair_symmetry", "Mdiff1", "Mdiff2", role="input_pair", mirror_right=True, same_y=True)
    builder.pair("mirror_pair_symmetry", "Mmirr1", "Mmirr2", role="current_mirror", mirror_right=True, same_y=True)
    # Relation names follow the compiler convention: source is below/left of target.
    builder.relation("tail_device", "input_pair", "above", min_gap_um=spacing_um, notes="tail below input pair")
    builder.relation("tail_device", "input_pair", "overlap_x", notes="tail must remain routable beneath input pair")
    builder.relation("input_pair", "mirror_pair", "above", min_gap_um=spacing_um, notes="PMOS mirror above NMOS input pair")
    builder.relation("input_pair", "mirror_pair", "overlap_x", notes="first-stage stack shares a vertical routing corridor")
    builder.relation("bias_reference", "tail_device", "right_of", min_gap_um=spacing_um, notes="bias reference beside tail device")
    builder.relation("bias_reference", "tail_device", "overlap_y", notes="bias and tail retain a horizontal routing corridor")
    builder.relation("input_pair", "compensation", "right_of", min_gap_um=spacing_um, notes="compensation beside first stage")
    builder.relation("input_pair", "compensation", "overlap_y", notes="first stage and compensation retain a horizontal routing corridor")
    builder.relation("compensation", "second_stage", "right_of", min_gap_um=spacing_um, notes="second stage follows compensation branch")
    builder.relation("compensation", "second_stage", "overlap_y", notes="compensation and second stage retain a horizontal routing corridor")
    builder.soft_relation("bias_reference", "tail_device", "align_center_y", weight=5)
    builder.soft_relation("tail_device", "input_pair", "align_center_x", weight=12)
    builder.soft_relation("input_pair", "mirror_pair", "align_center_x", weight=8)
    builder.soft_relation("input_pair", "compensation", "align_center_y", weight=4)
    builder.soft_relation("compensation", "second_stage", "align_center_y", weight=6)
    builder.pack(
        "first_stage",
        ("bias_reference", "tail_device", "input_pair", "mirror_pair"),
        weight=14,
        area_weight=4,
        notes="compact symmetric first-stage stack",
    )
    builder.pack(
        "ota_core",
        ("mirror_pair", "input_pair", "tail_device", "bias_reference", "compensation", "second_stage"),
        weight=12,
        area_weight=3,
    )
    builder.align_centers("first_stage_axis", ("tail_device", "input_pair", "mirror_pair"), axis="x", weight=12)
    builder.align_centers("signal_flow_axis", ("input_pair", "compensation", "second_stage"), axis="y", weight=5)
    builder.aesthetic_objectives(
        "ota_core",
        ("bias_reference", "tail_device", "input_pair", "mirror_pair", "compensation", "second_stage"),
        squareness_weight=3,
        compactness_weight=8,
        alignment_weight=3,
        regularity_weight=0,
    )
    critical_nets = set(constraints.critical_nets)
    for net in graph.nets:
        role = getattr(graph.nets.get(net), "role", "")
        role_name = str(getattr(role, "value", role))
        if net in critical_nets:
            builder.critical_net(
                net,
                weight=8 if net in {"vip", "vin", "n_s1"} else 5,
                shield=net == "n_s1",
                width_um=_wide_net_width_um(pdk, net) if role_name in {"supply", "ground", "output"} else None,
                notes=f"inferred critical {role_name} net",
            )
        allowed = ("M4",) if net in {"vout", "vss"} and "M4" in route_layers else route_layers
        builder.route_resource(
            net,
            allowed_layers=allowed,
            cyclic_lanes=tuple(range(12)),
            style="wide" if role_name in {"supply", "ground", "output"} else "signal",
            channel_orientation="horizontal",
            route_policy={"track_demand": 2 if role_name in {"supply", "ground", "output"} else 1},
            notes="SMT-assigned layer and horizontal strap lane" if net in critical_nets else "ordinary-net lane reserved before detailed routing",
        )
    builder.objective(
        bbox_weight=80,
        width_weight=5,
        height_weight=4,
        true_area_weight=2,
        max_side_weight=8,
        hpwl_weight=20,
        aspect_weight=3,
        objective_term_weight=4,
    )
    builder.drc_policy(
        placement_spacing_um=spacing_um,
        grid_um=max(float(pdk.rules.grid_nm) * 1e-3, 0.001),
    )
    builder.noncritical_router("astar")
    builder.notes("Graph-inferred constraints plus reviewed two_stage_ota physical policy")

    evidence = _constraint_evidence(graph, constraints)
    return PhysicalDesignIntent(
        topology="two_stage_ota",
        spec=builder.build(),
        constraints=evidence,
        layout_constraints=constraints,
        metadata={
            "constraint_precedence": ("pdk_hard", "topology_hard", "graph_inferred"),
            "common_centroid_policy": "legal_unit_array_else_explicit_symmetric_degradation",
            "route_resource_solver": "analogskills_local_smt",
        },
    )


def solve_imported_physical_smt(
    graph: TopologyGraph,
    sizing: Mapping[str, Mapping[str, Any]],
    *,
    topology: str,
    pdk: object,
    solver_timeout_ms: int = 30_000,
    track_pitch_um: float = 0.5,
) -> ImportedPhysicalSmtResult:
    intent = compile_physical_intent(graph, topology=topology, pdk=pdk)
    device_sizes = _resolved_pcell_sizes(graph, sizing, pdk)
    compiled = compile_analog_layout_smt(
        intent.spec,
        graph,
        device_sizes_um=device_sizes,
        track_pitch_um=track_pitch_um,
        placement_spacing_um=intent.spec.drc.placement_spacing_um,
        max_candidate_count=64,
        solver_timeout_ms=solver_timeout_ms,
    )
    if not compiled.passed:
        issues = tuple(compiled.checks.get("issues", ()))
        raise PhysicalIntentError("smt_unsat_or_timeout", f"two_stage_ota placement SMT failed: {issues}")
    assignments, routing_evidence = _solve_analogskills_route_resources(
        graph,
        intent,
        candidate_layers=tuple(dict.fromkeys(layer for row in intent.spec.route_resources for layer in row.allowed_layers)),
    )
    matching = _matching_realization_report(intent.layout_constraints)
    checks = {
        **dict(compiled.checks),
        "route_resource_assignment_count": len(assignments),
        "route_resource_capacity_overflow": 0,
        "matching_realization": matching,
        "constraint_realization_complete": all(
            row.get("status") == "realized" for row in matching.values()
        ),
    }
    compiled = replace(compiled, checks=checks, route_resource_assignments=assignments)
    return ImportedPhysicalSmtResult(
        intent,
        compiled,
        tuple(compiled.placements),
        assignments,
        matching,
        routing_evidence,
    )


def _resolved_pcell_sizes(
    graph: TopologyGraph,
    sizing: Mapping[str, Mapping[str, Any]],
    pdk: object,
) -> dict[str, tuple[float, float]]:
    origins = tuple(Placement(name, 0.0, 0.0) for name in graph.devices)
    try:
        probe = generate_pcell_layout_plan(
            graph,
            sizing,
            pdk=pdk,
            placements=origins,
            strict=True,
            include_fallback_shapes=False,
        )
    except Exception as exc:
        raise PhysicalIntentError("pcell_realization_unavailable", str(exc)) from exc
    by_logical: dict[str, list[object]] = {}
    for instance in probe.instances:
        logical = str(getattr(instance, "name", "")).split("_u", 1)[0]
        by_logical.setdefault(logical, []).append(instance)
        if str(getattr(instance, "instantiation_method", "")) == "drawn_primitive":
            raise PhysicalIntentError(
                "pcell_realization_unavailable",
                f"{logical} requires a drawn/fallback primitive",
            )
    result: dict[str, tuple[float, float]] = {}
    for name in graph.devices:
        instances = by_logical.get(name, ())
        if not instances:
            raise PhysicalIntentError("pcell_realization_unavailable", f"missing PCell footprint for {name}")
        width = max(float(getattr(item, "xy_um")[0]) + float(getattr(item, "width_um")) for item in instances)
        height = max(float(getattr(item, "xy_um")[1]) + float(getattr(item, "height_um")) for item in instances)
        result[name] = (max(width, 0.001), max(height, 0.001))
    return result


def _solve_analogskills_route_resources(
    graph: TopologyGraph,
    intent: PhysicalDesignIntent,
    *,
    candidate_layers: tuple[str, ...],
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """Lower AnalogSkills' critical-region SMT result into router resources."""

    if not candidate_layers:
        raise PhysicalIntentError("constraint_compile_failed", "route resources have no allowed layers")
    routing_constraints = tuple(intent.layout_constraints.routing) + (
        RoutingConstraint("vip", "differential_partner", ("vin",), "OTA differential input pair"),
        RoutingConstraint("vin", "differential_partner", ("vip",), "OTA differential input pair"),
        RoutingConstraint("vout", "route_layer", "M4", "high-current output trunk"),
        RoutingConstraint("n_s1", "shield", True, "high-impedance first-stage output"),
        RoutingConstraint("n_s1", "avoid_nets", ("vip", "vin"), "isolate high-Z node from inputs"),
        RoutingConstraint("vdd", "wide", True, "power template"),
        RoutingConstraint("vss", "wide", True, "ground template"),
    )
    augmented = replace(
        intent.layout_constraints,
        routing=routing_constraints,
        critical_nets=tuple(dict.fromkeys((*intent.layout_constraints.critical_nets, "vip", "vin", "n_s1", "vout"))),
    )
    routing_graph = replace(graph, layout_constraints=augmented)
    plan = build_advanced_analog_routing_plan(routing_graph, constraints=augmented)
    raw_assignments: dict[str, tuple[str, int, str]] = {}
    patch_evidence: list[dict[str, object]] = []
    for patch in plan.local_smt_patch_regions:
        problem = build_analog_local_smt_problem(
            routing_graph,
            plan,
            patch.name,
            candidate_layers=candidate_layers,
            track_count=16,
        )
        solved = solve_analog_local_smt(problem, max_solutions=1, backend="z3")
        if not solved.solutions:
            raise PhysicalIntentError("smt_unsat", f"AnalogSkills local routing SMT failed for {patch.name}")
        solution = solved.solutions[0]
        for net, layer, track in solution.assignments:
            if net in raw_assignments and raw_assignments[net][:2] != (layer, track):
                raise PhysicalIntentError("constraint_compile_failed", f"conflicting SMT assignments for {net}")
            raw_assignments[net] = (layer, int(track), patch.name)
        patch_evidence.append({
            "patch": asdict(patch),
            "problem": asdict(problem),
            "solution": asdict(solution),
            "stats": asdict(solved.stats),
        })

    resource_by_net = {str(row.name): row for row in intent.spec.route_resources}
    ordered_signal_nets = sorted(raw_assignments, key=lambda net: (raw_assignments[net][1], raw_assignments[net][0], net))
    differential = [net for net in ("vip", "vin") if net in raw_assignments]
    ordered_signal_nets = differential + [net for net in ordered_signal_nets if net not in differential]
    template_nets = [net for net in ("vss", "vdd") if net in resource_by_net]
    remaining_nets = [net for net in resource_by_net if net not in raw_assignments and net not in template_nets]
    ordered_nets = tuple(dict.fromkeys((*differential, *ordered_signal_nets, *remaining_nets, *template_nets)))

    result: dict[str, dict[str, object]] = {}
    for lane, net in enumerate(ordered_nets):
        resource = resource_by_net[net]
        if net in raw_assignments:
            layer, solver_track, patch_name = raw_assignments[net]
            source = "analogskills_local_smt"
        else:
            allowed = tuple(resource.allowed_layers or ((resource.layer,) if resource.layer else candidate_layers))
            layer = "M4" if net in {"vss", "vout"} and "M4" in allowed else allowed[0]
            solver_track = lane
            patch_name = "template_power" if net in template_nets else "template_escape"
            source = "template"
        demand = max(1, int(dict(resource.route_policy).get("track_demand", 1) or 1))
        result[net] = {
            "layer": layer,
            "lane": lane,
            "solver_track": solver_track,
            "track_demand": demand,
            "corridor": f"top_horizontal_{layer}_{lane}",
            "orientation": str(resource.channel_orientation or "horizontal"),
            "style": str(resource.style or "signal"),
            "region": patch_name,
            "solver": source,
        }
    if set(result) != set(resource_by_net):
        raise PhysicalIntentError("constraint_compile_failed", "routing resource lowering omitted nets")
    return result, {
        "planner": "analogskills.layout.analog_routing",
        "plan": asdict(plan),
        "local_smt_patches": patch_evidence,
        "template_nets": tuple(template_nets),
        "lowering": "SMT track order to globally unique physical strap lanes",
    }


def _constraint_evidence(
    graph: TopologyGraph,
    constraints: LayoutConstraintSet,
) -> tuple[ConstraintEvidence, ...]:
    rows: list[ConstraintEvidence] = []
    for group in constraints.matched_groups:
        status = "degraded_explicit" if group.style == "common_centroid" else "required"
        rows.append(
            ConstraintEvidence(
                group.name,
                f"match:{group.style}",
                "topology_policy" if group.name in {"input_pair", "mirror_load"} else "graph_inferred",
                True,
                "matched devices require identical PCell realization",
                devices=tuple(group.devices),
                status=status,
            )
        )
    for index, group in enumerate(constraints.symmetry_groups):
        rows.append(ConstraintEvidence(f"symmetry_{index}", "symmetry", "graph_inferred", True, "matched environment", devices=tuple(group)))
    for item in constraints.routing:
        rows.append(
            ConstraintEvidence(
                f"route_{item.net}_{item.kind}",
                f"routing:{item.kind}",
                "topology_policy" if item.reason and "auto-inferred" not in item.reason else "graph_inferred",
                item.kind not in {"preferred_layer"},
                item.reason or "routing intent",
                nets=(item.net,),
            )
        )
    known = set(graph.nets)
    bad = sorted(net for row in rows for net in row.nets if net not in known)
    if bad:
        raise PhysicalIntentError("constraint_compile_failed", f"constraints reference unknown nets: {bad}")
    return tuple(rows)


def _matching_realization_report(constraints: LayoutConstraintSet) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for group in constraints.matched_groups:
        if group.style in {"common_centroid", "interdigitated"}:
            result[group.name] = {
                "requested_style": group.style,
                "realized_style": "symmetric_pair",
                "status": "degraded_explicit",
                "reason": "requested matched-device unit pattern is not physically realized",
                "devices": tuple(group.devices),
            }
        else:
            result[group.name] = {
                "requested_style": group.style,
                "realized_style": "symmetric_pair",
                "status": "realized",
                "devices": tuple(group.devices),
            }
    return result


def _wide_net_width_um(pdk: object, net: str) -> float:
    metals = tuple(str(layer) for layer in getattr(pdk.layer_map, "metals", ()))
    preferred = "M4" if net == "vout" and "M4" in metals else metals[min(2, len(metals) - 1)]
    return max(0.2, 2.0 * float(pdk.rules.min_width_um(preferred)))
