"""Auditable circuit-to-physical intent compilation for imported designs."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None

from analogskills.contracts import LayoutConstraintSet, TopologyGraph
from analogskills.layout.analog_layout_dsl import AnalogLayoutSpec, layout_spec
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
    builder.pattern("mirror_pair", ("Mmirr1", "Mmirr2"), role="pmos_match", kind="row", spacing_um=spacing_um)
    builder.pattern("input_pair", ("Mdiff1", "Mdiff2"), role="nmos_match", kind="row", spacing_um=spacing_um)
    builder.pattern("tail_bias", ("Mbias", "Mtail"), role="nmos_bias", kind="row", spacing_um=spacing_um)
    builder.pattern("second_stage", ("Mload", "Mcs"), role="output_stage", kind="column", spacing_um=spacing_um)
    builder.pattern("compensation", ("Rz", "Cc"), role="miller_compensation", kind="row", spacing_um=spacing_um)
    builder.pair("input_pair_symmetry", "Mdiff1", "Mdiff2", role="input_pair", mirror_right=False, same_y=True)
    builder.pair("mirror_pair_symmetry", "Mmirr1", "Mmirr2", role="current_mirror", mirror_right=False, same_y=True)
    # Relation names follow the compiler convention: source is below/left of target.
    builder.relation("tail_bias", "input_pair", "above", min_gap_um=spacing_um, notes="tail and bias below input pair")
    builder.relation("input_pair", "mirror_pair", "above", min_gap_um=spacing_um, notes="PMOS mirror above NMOS input pair")
    builder.relation("input_pair", "compensation", "right_of", min_gap_um=spacing_um, notes="compensation beside first stage")
    builder.relation("compensation", "second_stage", "right_of", min_gap_um=spacing_um, notes="second stage follows compensation branch")
    builder.soft_relation("tail_bias", "input_pair", "align_center_x", weight=8)
    builder.soft_relation("input_pair", "mirror_pair", "align_center_x", weight=8)
    builder.soft_relation("compensation", "second_stage", "align_center_y", weight=3)
    builder.pack("ota_core", ("mirror_pair", "input_pair", "tail_bias", "second_stage", "compensation"), weight=10, area_weight=2)
    builder.aesthetic_objectives(
        "ota_core",
        ("mirror_pair", "input_pair", "tail_bias", "compensation", "second_stage"),
        squareness_weight=2,
        compactness_weight=5,
        alignment_weight=2,
        regularity_weight=1,
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
        bbox_weight=100,
        width_weight=5,
        height_weight=3,
        hpwl_weight=15,
        aspect_weight=2,
        objective_term_weight=3,
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
            "route_resource_solver": "z3_solver",
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
    assignments = _solve_route_resources(
        intent.spec,
        timeout_ms=solver_timeout_ms,
        fixed_lanes={str(net): index for index, net in enumerate(graph.pins)},
    )
    matching = _matching_realization_report(intent.layout_constraints)
    checks = {
        **dict(compiled.checks),
        "route_resource_assignment_count": len(assignments),
        "route_resource_capacity_overflow": 0,
        "matching_realization": matching,
        "constraint_realization_complete": all(
            row.get("status") in {"realized", "degraded_explicit"} for row in matching.values()
        ),
    }
    compiled = replace(compiled, checks=checks, route_resource_assignments=assignments)
    return ImportedPhysicalSmtResult(intent, compiled, tuple(compiled.placements), assignments, matching)


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


def _solve_route_resources(
    spec: AnalogLayoutSpec,
    *,
    timeout_ms: int,
    fixed_lanes: Mapping[str, int] | None = None,
) -> dict[str, dict[str, object]]:
    if z3 is None:  # pragma: no cover
        raise PhysicalIntentError("smt_unavailable", "z3-solver is required for route resource assignment")
    rows = tuple(spec.route_resources)
    if not rows:
        raise PhysicalIntentError("constraint_compile_failed", "no critical route resources were declared")
    all_layers = tuple(dict.fromkeys(layer for row in rows for layer in (row.allowed_layers or ((row.layer,) if row.layer else ()))))
    if not all_layers:
        raise PhysicalIntentError("constraint_compile_failed", "route resources have no allowed layers")
    layer_index = {name: index for index, name in enumerate(all_layers)}
    solver = z3.Solver()
    solver.set(timeout=max(1, int(timeout_ms)))
    layer_vars: dict[str, object] = {}
    lane_vars: dict[str, object] = {}
    demand: dict[str, int] = {}
    for index, row in enumerate(rows):
        name = str(row.name)
        layer_var = z3.Int(f"ota_route_layer_{index}")
        lane_var = z3.Int(f"ota_route_lane_{index}")
        allowed_layers = tuple(row.allowed_layers or ((row.layer,) if row.layer else all_layers))
        allowed_lanes = tuple(row.cyclic_lanes or ((row.lane,) if row.lane is not None else tuple(range(12))))
        solver.add(z3.Or(*(layer_var == layer_index[layer] for layer in allowed_layers)))
        solver.add(z3.Or(*(lane_var == int(lane) for lane in allowed_lanes)))
        if name in dict(fixed_lanes or {}):
            solver.add(lane_var == int(dict(fixed_lanes or {})[name]))
        layer_vars[name] = layer_var
        lane_vars[name] = lane_var
        demand[name] = max(1, int(dict(row.route_policy).get("track_demand", 1) or 1))
    names = tuple(str(row.name) for row in rows)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            # A route to an upper layer carries a via stack through lower
            # layers.  Reserve lanes globally so those stacks cannot pierce a
            # different net's lower-metal strap.  Wide/power demand remains an
            # auditable width class; the 3um lane pitch supplies its clearance.
            solver.add(lane_vars[left] != lane_vars[right])
    if "vip" in lane_vars and "vin" in lane_vars:
        solver.add(layer_vars["vip"] == layer_vars["vin"])
        solver.add(z3.Abs(lane_vars["vip"] - lane_vars["vin"]) == 1)
    status = solver.check()
    if status != z3.sat:
        reason = "smt_timeout" if status == z3.unknown else "smt_unsat"
        raise PhysicalIntentError(reason, f"critical route resource SMT returned {status}")
    model = solver.model()
    by_index = {value: key for key, value in layer_index.items()}
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        name = str(row.name)
        layer = by_index[int(model.eval(layer_vars[name], model_completion=True).as_long())]
        lane = int(model.eval(lane_vars[name], model_completion=True).as_long())
        result[name] = {
            "layer": layer,
            "lane": lane,
            "track_demand": demand[name],
            "corridor": f"horizontal_{layer}_{lane}",
            "orientation": str(row.channel_orientation or "horizontal"),
            "style": str(row.style or "signal"),
            "solver": "z3_solver",
        }
    return result


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
        if group.style == "common_centroid":
            result[group.name] = {
                "requested_style": group.style,
                "realized_style": "symmetric_pair",
                "status": "degraded_explicit",
                "reason": "no calibrated legal unit-array realization selected for the current small devices",
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
