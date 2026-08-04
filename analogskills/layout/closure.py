"""IR-first layout assembly helpers."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence, TYPE_CHECKING

from analogskills.contracts import LayoutConstraintSet, RoutingConstraint
from analogskills.pdk import PdkConfig
from analogskills.repair import (
    DrcIssue,
    localize_drc_issues_to_layout,
    localize_spacing_drc_issues_to_layout,
    plan_localized_drc_layout_patch,
    plan_localized_spacing_replacement,
    plan_lvs_open_route_patch,
    plan_lvs_short_replacement,
    plan_via_enclosure_patch,
)

from .ir import LayoutCellRef, LayoutInstance, LayoutPlan, LayoutRect, merge_layout_plans, snap_layout_plan_to_grid
from .constraints import build_routing_intent_set
from .physical import (
    _via_required_layers,
    analyze_plan_physical_connectivity,
    analyze_via_landings,
    bbox_contains,
    bbox_overlaps,
    collect_plan_shapes,
    detect_plan_net_opens,
    detect_plan_shape_shorts,
    path_segment_bboxes,
    via_landing_bboxes,
)
from .power import analyze_power_plan, plan_guard_ring, plan_guard_ring_tap_implant_joins, plan_power_rails, plan_power_source_drops, plan_supply_taps, plan_well_regions, top_level_marker_requires_global_cover
from .routing import InterconnectCandidate, analyze_interconnect_plan, generate_interconnect, rank_interconnect_candidates, require_interconnect_precheck

if TYPE_CHECKING:
    from analogskills.analysis import AnalogDesignContext, AnalogSolverGuide, AnalogStrategyEvaluation
    from analogskills.contracts import AnalogPlacementStrategy, AnalogRoutingStrategy
    from analogskills.pcell.calibration import PCellCalibrationCache


_LEGACY_INLINE_RULE_CACHE: dict[str, dict[str, object]] = {}


def _enrich_seed_metadata_from_hierarchy(
    placement_seed_metadata: Mapping[str, object] | None,
    hierarchy_context: Mapping[str, object] | None,
) -> dict[str, object]:
    effective_seed_metadata = dict(placement_seed_metadata or {})
    hierarchy_plan = dict((hierarchy_context or {}).get("hierarchical_floorplan_plan", {}) or {}) if hierarchy_context else {}
    if hierarchy_plan:
        if not effective_seed_metadata.get("preferred_partition_order"):
            effective_seed_metadata["preferred_partition_order"] = tuple(str(name) for name in hierarchy_plan.get("preferred_partition_order", ()) if str(name))
        if not effective_seed_metadata.get("anchor_partitions"):
            effective_seed_metadata["anchor_partitions"] = tuple(str(name) for name in hierarchy_plan.get("anchor_partitions", ()) if str(name))
        if not effective_seed_metadata.get("focus_partitions"):
            effective_seed_metadata["focus_partitions"] = tuple(str(name) for name in hierarchy_plan.get("focus_partitions", ()) if str(name))
        provenance = dict(hierarchy_plan.get("provenance", {}) or {})
        if not effective_seed_metadata.get("partition_device_map"):
            effective_seed_metadata["partition_device_map"] = dict(provenance.get("partition_device_map", {}) or {})
        if not effective_seed_metadata.get("partition_net_map"):
            effective_seed_metadata["partition_net_map"] = dict(provenance.get("partition_net_map", {}) or {})
    binding_plan = dict((hierarchy_context or {}).get("hierarchical_partition_pcell_binding_plan", {}) or {}) if hierarchy_context else {}
    if binding_plan and "hierarchical_partition_pcell_binding_plan" not in effective_seed_metadata:
        effective_seed_metadata["hierarchical_partition_pcell_binding_plan"] = binding_plan
    if binding_plan and "pdk_binding_coverage" not in effective_seed_metadata:
        partitions = tuple(
            dict(item)
            for item in tuple(binding_plan.get("partitions", ()) or ())
            if isinstance(item, Mapping)
        )
        effective_seed_metadata["pdk_binding_coverage"] = {
            "binding_blocked_partitions": tuple(
                sorted(
                    str(item.get("name", ""))
                    for item in partitions
                    if str(item.get("name", ""))
                    and (
                        (bool(item.get("pcell_binding_applicable", False)) and not bool(item.get("pcell_binding_ready", False)))
                        or (bool(item.get("macro_binding_applicable", False)) and not bool(item.get("macro_binding_ready", False)))
                    )
                )
            ),
            "macro_bound_partitions": tuple(
                sorted(
                    str(item.get("name", ""))
                    for item in partitions
                    if str(item.get("name", "")) and bool(item.get("macro_binding_ready", False))
                )
            ),
            "pcell_bound_partitions": tuple(
                sorted(
                    str(item.get("name", ""))
                    for item in partitions
                    if str(item.get("name", "")) and bool(item.get("pcell_binding_ready", False))
                )
            ),
        }
    parasitic_plan = dict((hierarchy_context or {}).get("hierarchical_partition_parasitic_target_plan", {}) or {}) if hierarchy_context else {}
    if parasitic_plan and "hierarchical_partition_parasitic_target_plan" not in effective_seed_metadata:
        effective_seed_metadata["hierarchical_partition_parasitic_target_plan"] = parasitic_plan
    return effective_seed_metadata


def _snapshot_analog_placement_strategy(strategy: "AnalogPlacementStrategy | None") -> dict[str, object]:
    if strategy is None:
        return {}
    if isinstance(strategy, Mapping):
        return dict(strategy)
    groups = tuple(
        {
            "name": str(getattr(group, "name", "")),
            "device_names": tuple(str(name) for name in tuple(getattr(group, "device_names", ()) or ()) if str(name)),
            "row_target": str(getattr(group, "row_target", "")),
            "anchor": bool(getattr(group, "anchor", False)),
            "focus": bool(getattr(group, "focus", False)),
            "critical_nets": tuple(str(net) for net in tuple(getattr(group, "critical_nets", ()) or ()) if str(net)),
            "notes": str(getattr(group, "notes", "")),
        }
        for group in tuple(getattr(strategy, "groups", ()) or ())
    )
    objectives = tuple(
        {
            "name": str(getattr(objective, "name", "")),
            "weight": float(getattr(objective, "weight", 0.0) or 0.0),
            "priority": int(getattr(objective, "priority", 0) or 0),
            "notes": str(getattr(objective, "notes", "")),
        }
        for objective in tuple(getattr(strategy, "objectives", ()) or ())
    )
    return {
        "initial_mode": str(getattr(strategy, "initial_mode", "")),
        "groups": groups,
        "objectives": objectives,
        "notes": tuple(str(note) for note in tuple(getattr(strategy, "notes", ()) or ()) if str(note)),
    }


def _snapshot_analog_routing_strategy(strategy: "AnalogRoutingStrategy | None") -> dict[str, object]:
    if strategy is None:
        return {}
    if isinstance(strategy, Mapping):
        return dict(strategy)
    groups = tuple(
        {
            "name": str(getattr(group, "name", "")),
            "nets": tuple(str(net) for net in tuple(getattr(group, "nets", ()) or ()) if str(net)),
            "route_mode": str(getattr(group, "route_mode", "")),
            "priority": int(getattr(group, "priority", 0) or 0),
            "preferred_layer": str(getattr(group, "preferred_layer", "")),
            "corridor": str(getattr(group, "corridor", "")),
            "shield_net": str(getattr(group, "shield_net", "")),
            "critical": bool(getattr(group, "critical", False)),
            "notes": str(getattr(group, "notes", "")),
        }
        for group in tuple(getattr(strategy, "groups", ()) or ())
    )
    return {
        "route_order": tuple(str(net) for net in tuple(getattr(strategy, "route_order", ()) or ()) if str(net)),
        "allow_ripup": bool(getattr(strategy, "allow_ripup", False)),
        "groups": groups,
        "notes": tuple(str(note) for note in tuple(getattr(strategy, "notes", ()) or ()) if str(note)),
    }


def _solver_guide_agent_contract_payload(guide: "AnalogSolverGuide | Mapping[str, object] | None") -> dict[str, object]:
    if guide is None:
        return {}
    if isinstance(guide, Mapping):
        nested = dict(guide.get("agent_contract", {}) or {})
        if nested:
            return nested
        return {
            "intent_priorities": tuple(str(item) for item in tuple(guide.get("intent_priorities", ()) or ()) if str(item)),
            "hard_rules": tuple(dict(item) for item in tuple(guide.get("hard_rules", ()) or ()) if isinstance(item, Mapping)),
            "soft_rules": tuple(dict(item) for item in tuple(guide.get("soft_rules", ()) or ()) if isinstance(item, Mapping)),
            "forbidden_actions": tuple(dict(item) for item in tuple(guide.get("forbidden_actions", ()) or ()) if isinstance(item, Mapping)),
            "required_artifacts": tuple(dict(item) for item in tuple(guide.get("required_artifacts", ()) or ()) if isinstance(item, Mapping)),
            "review_checklist": tuple(dict(item) for item in tuple(guide.get("review_checklist", ()) or ()) if isinstance(item, Mapping)),
            "fallback_actions": tuple(dict(item) for item in tuple(guide.get("fallback_actions", ()) or ()) if isinstance(item, Mapping)),
            "iteration_policy": dict(guide.get("iteration_policy", {}) or {}),
        }
    return {
        "intent_priorities": tuple(str(item) for item in tuple(getattr(guide, "intent_priorities", ()) or ()) if str(item)),
        "hard_rules": tuple(dict(item) for item in tuple(getattr(guide, "hard_rules", ()) or ()) if isinstance(item, Mapping)),
        "soft_rules": tuple(dict(item) for item in tuple(getattr(guide, "soft_rules", ()) or ()) if isinstance(item, Mapping)),
        "forbidden_actions": tuple(dict(item) for item in tuple(getattr(guide, "forbidden_actions", ()) or ()) if isinstance(item, Mapping)),
        "required_artifacts": tuple(dict(item) for item in tuple(getattr(guide, "required_artifacts", ()) or ()) if isinstance(item, Mapping)),
        "review_checklist": tuple(dict(item) for item in tuple(getattr(guide, "review_checklist", ()) or ()) if isinstance(item, Mapping)),
        "fallback_actions": tuple(dict(item) for item in tuple(getattr(guide, "fallback_actions", ()) or ()) if isinstance(item, Mapping)),
        "iteration_policy": dict(getattr(guide, "iteration_policy", {}) or {}),
    }


def _solver_guide_iteration_policy(guide: "AnalogSolverGuide | Mapping[str, object] | None") -> dict[str, object]:
    return dict(_solver_guide_agent_contract_payload(guide).get("iteration_policy", {}) or {})


def _solver_guide_max_rounds(guide: "AnalogSolverGuide | Mapping[str, object] | None") -> int:
    try:
        value = int(_solver_guide_iteration_policy(guide).get("max_rounds", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _snapshot_analog_solver_guide(guide: "AnalogSolverGuide | None") -> dict[str, object]:
    if guide is None:
        return {}
    agent_contract = _solver_guide_agent_contract_payload(guide)
    if isinstance(guide, Mapping):
        snapshot = dict(guide)
        snapshot["agent_contract"] = agent_contract
        return snapshot
    return {
        "topology_name": str(getattr(guide, "topology_name", "")),
        "placement_steps": tuple(str(step) for step in tuple(getattr(guide, "placement_steps", ()) or ()) if str(step)),
        "routing_steps": tuple(str(step) for step in tuple(getattr(guide, "routing_steps", ()) or ()) if str(step)),
        "placement_objectives": tuple(dict(item) for item in tuple(getattr(guide, "placement_objectives", ()) or ()) if isinstance(item, Mapping)),
        "routing_groups": tuple(dict(item) for item in tuple(getattr(guide, "routing_groups", ()) or ()) if isinstance(item, Mapping)),
        "sizing_policy": dict(getattr(guide, "sizing_policy", {}) or {}),
        "implementation_policy": dict(getattr(guide, "implementation_policy", {}) or {}),
        "intent_priorities": tuple(str(item) for item in tuple(agent_contract.get("intent_priorities", ()) or ()) if str(item)),
        "hard_rules": tuple(dict(item) for item in tuple(agent_contract.get("hard_rules", ()) or ()) if isinstance(item, Mapping)),
        "soft_rules": tuple(dict(item) for item in tuple(agent_contract.get("soft_rules", ()) or ()) if isinstance(item, Mapping)),
        "forbidden_actions": tuple(dict(item) for item in tuple(agent_contract.get("forbidden_actions", ()) or ()) if isinstance(item, Mapping)),
        "required_artifacts": tuple(dict(item) for item in tuple(agent_contract.get("required_artifacts", ()) or ()) if isinstance(item, Mapping)),
        "review_checklist": tuple(dict(item) for item in tuple(agent_contract.get("review_checklist", ()) or ()) if isinstance(item, Mapping)),
        "fallback_actions": tuple(dict(item) for item in tuple(agent_contract.get("fallback_actions", ()) or ()) if isinstance(item, Mapping)),
        "iteration_policy": dict(agent_contract.get("iteration_policy", {}) or {}),
        "agent_contract": agent_contract,
        "stop_conditions": tuple(str(item) for item in tuple(getattr(guide, "stop_conditions", ()) or ()) if str(item)),
        "summary": tuple(str(item) for item in tuple(getattr(guide, "summary", ()) or ()) if str(item)),
        "provenance": dict(getattr(guide, "provenance", {}) or {}),
    }


def _snapshot_analog_design_context(context: "AnalogDesignContext | None") -> dict[str, object]:
    if context is None:
        return {}
    if isinstance(context, Mapping):
        return dict(context)
    return {
        "topology_name": str(getattr(context, "topology_name", "")),
        "circuit_family": str(getattr(context, "circuit_family", "")),
        "preferred_skeleton": str(getattr(context, "preferred_skeleton", "")),
        "critical_nets": tuple(str(net) for net in tuple(getattr(context, "critical_nets", ()) or ()) if str(net)),
        "preferred_partition_order": tuple(str(name) for name in tuple(getattr(context, "preferred_partition_order", ()) or ()) if str(name)),
        "anchor_partitions": tuple(str(name) for name in tuple(getattr(context, "anchor_partitions", ()) or ()) if str(name)),
        "focus_partitions": tuple(str(name) for name in tuple(getattr(context, "focus_partitions", ()) or ()) if str(name)),
        "floorplan_summary": dict(getattr(context, "floorplan_summary", {}) or {}),
        "placement_summary": dict(getattr(context, "placement_summary", {}) or {}),
        "routing_summary": dict(getattr(context, "routing_summary", {}) or {}),
        "guide_summary": tuple(str(item) for item in tuple(getattr(getattr(context, "guide", None), "summary", ()) or ()) if str(item)),
        "iteration_trace": tuple(
            dict(item)
            for item in tuple(getattr(context, "iteration_trace", ()) or ())
            if isinstance(item, Mapping)
        ),
        "summary": tuple(str(item) for item in tuple(getattr(context, "summary", ()) or ()) if str(item)),
        "provenance": dict(getattr(context, "provenance", {}) or {}),
    }


def _snapshot_analog_strategy_evaluation(evaluation: "AnalogStrategyEvaluation | None") -> dict[str, object]:
    if evaluation is None:
        return {}
    if isinstance(evaluation, Mapping):
        return dict(evaluation)
    return {
        "topology_name": str(getattr(evaluation, "topology_name", "")),
        "passed": bool(getattr(evaluation, "passed", False)),
        "issues": tuple(str(issue) for issue in tuple(getattr(evaluation, "issues", ()) or ()) if str(issue)),
        "metrics": dict(getattr(evaluation, "metrics", {}) or {}),
        "summary": tuple(str(item) for item in tuple(getattr(evaluation, "summary", ()) or ()) if str(item)),
        "provenance": dict(getattr(evaluation, "provenance", {}) or {}),
    }


def _pcell_device_layout_plan(
    device_plan: Any,
    *,
    target: LayoutCellRef,
    pdk: PdkConfig,
) -> LayoutPlan | None:
    instances = tuple(getattr(device_plan, "instances", ()) or ())
    if not instances:
        return None
    first = instances[0]
    if not hasattr(first, "lib_name") or not hasattr(first, "cell_name"):
        return None

    fallback_shapes = tuple(getattr(device_plan, "fallback_shapes", ()) or ())
    drawn_primitive_terminal_map = {
        f"{str(inst.name)}.{str(term)}": str(net)
        for inst in instances
        if str(getattr(inst, "instantiation_method", "")) == "drawn_primitive"
        for term, net in dict(getattr(inst, "connections", {}) or {}).items()
        if str(term) and str(net)
    }
    layout_instances = tuple(
        LayoutInstance(
            name=str(inst.name),
            master=LayoutCellRef(str(inst.lib_name), str(inst.cell_name), str(inst.view_name), "maskLayout"),
            xy=tuple(inst.xy_um),
            orient=str(inst.orient),
            connections=dict(inst.connections),
            params=dict(inst.params),
            metadata={
                "instantiation_method": str(inst.instantiation_method),
                "logical_device_type": str(getattr(inst, "logical_name", "") or ""),
                "logical_pcell_name": str(getattr(inst, "logical_name", "") or ""),
                "source_device_name": str(getattr(inst, "name", "") or ""),
            },
        )
        for inst in instances
    )
    rect_list: list[LayoutRect] = []
    for shape in fallback_shapes:
        layer_name = str(shape.layer)
        bbox = tuple(shape.bbox)
        net_name = str(shape.net)
        rect_list.append(
            LayoutRect(
                layer_name,
                bbox,
                net_name,
                "drawing",
                {"source_shape": str(shape.id)},
            )
        )
        # Drawn passive resistor primitives use a netless PO body in the fallback
        # geometry.  The inline cover checker expects the configured p-metal
        # layer to enclose that PO body, matching the CRN28 PMET/PO rule.
        pmetal_layer = str(getattr(pdk.layer_map, "implants", {}).get("pmetal", "") or "")
        if layer_name == str(getattr(pdk.layer_map, "gate", "") or "") and not net_name and pmetal_layer:
            margin = 0.065
            rect_list.append(
                LayoutRect(
                    pmetal_layer,
                    pdk.rules.snap_bbox_um(
                        (
                            bbox[0] - margin,
                            bbox[1] - margin,
                            bbox[2] + margin,
                            bbox[3] + margin,
                        ),
                        mode="outward",
                    ),
                    "",
                    "drawing",
                    {"source_shape": f"{str(shape.id)}.pmetal_cover", "kind": "passive_implant_cover"},
                )
            )
    rects = tuple(rect_list)
    nets = tuple(
        dict.fromkeys(
            net
            for inst in instances
            for net in tuple(dict(inst.connections).values())
            if str(net)
        )
    )
    device_metadata = getattr(device_plan, "metadata", {}) if isinstance(getattr(device_plan, "metadata", {}), Mapping) else {}
    return LayoutPlan(
        target,
        nets=nets,
        instances=layout_instances,
        rects=rects,
        metadata={
            "source": "pcell_device_layout_plan",
            "pcell_instance_count": len(layout_instances),
            "pcell_fallback_shape_count": len(rects),
            "drawn_primitive_terminal_map": dict(drawn_primitive_terminal_map),
            "graph_name": str(device_metadata.get("graph_name", "")),
            "top_level_nets": tuple(str(net) for net in tuple(device_metadata.get("top_level_nets", ())) if str(net)),
            "top_level_pin_nets": {
                str(pin_name): str(net_name)
                for pin_name, net_name in dict(device_metadata.get("top_level_pin_nets", {}) or {}).items()
                if str(pin_name) and str(net_name)
            },
            "top_level_pin_roles": {
                str(pin_name): str(role)
                for pin_name, role in dict(device_metadata.get("top_level_pin_roles", {}) or {}).items()
                if str(pin_name)
            },
        },
    )


def _device_plan_passthrough_metadata(device_plan: Any) -> dict[str, object]:
    plan_metadata = getattr(device_plan, "metadata", {}) if isinstance(getattr(device_plan, "metadata", {}), Mapping) else {}
    return {
        "graph_name": str(plan_metadata.get("graph_name", "")),
        "top_level_nets": tuple(str(net) for net in tuple(plan_metadata.get("top_level_nets", ())) if str(net)),
        "top_level_pin_nets": {
            str(pin_name): str(net_name)
            for pin_name, net_name in dict(plan_metadata.get("top_level_pin_nets", {}) or {}).items()
            if str(pin_name) and str(net_name)
        },
        "top_level_pin_roles": {
            str(pin_name): str(role)
            for pin_name, role in dict(plan_metadata.get("top_level_pin_roles", {}) or {}).items()
            if str(pin_name)
        },
    }


def _available_supply_nets(
    device_plan: Any,
    *,
    top_net: str,
    bottom_net: str,
    top_level_nets: Sequence[str] | None = None,
) -> tuple[str, ...]:
    passive_only_logical_names = {"resistor", "res", "capacitor", "cap"}
    instance_logical_names = tuple(
        str(getattr(inst, "logical_name", "") or "").lower()
        for inst in tuple(getattr(device_plan, "instances", ()) or ())
    )
    if instance_logical_names and all(name in passive_only_logical_names for name in instance_logical_names if name):
        return ()
    device_nets = {
        str(net)
        for inst in tuple(getattr(device_plan, "instances", ()) or ())
        for net in tuple(dict(getattr(inst, "connections", {}) or {}).values())
        if str(net)
    }
    top_level_net_set = {str(net) for net in tuple(top_level_nets or ()) if str(net)}
    return tuple(
        net
        for net in (bottom_net, top_net)
        if str(net) and (str(net) in device_nets or str(net) in top_level_net_set)
    )


def _body_supply_nets_by_kind(device_plan: Any) -> dict[str, tuple[str, ...]]:
    by_kind: dict[str, list[str]] = {"nwell": [], "substrate": []}
    for inst in tuple(getattr(device_plan, "instances", ()) or ()):
        logical_name = str(getattr(inst, "logical_name", "") or "").lower()
        if logical_name.startswith("pmos"):
            kind = "nwell"
        elif logical_name.startswith("nmos"):
            kind = "substrate"
        else:
            continue
        connections = dict(getattr(inst, "connections", {}) or {})
        for terminal in ("B", "BODY", "BULK"):
            net = str(connections.get(terminal, "") or "")
            if net:
                by_kind.setdefault(kind, []).append(net)
                break
    return {kind: tuple(dict.fromkeys(nets)) for kind, nets in by_kind.items()}


def _resolve_effective_body_supply_net(
    device_plan: Any,
    requested_net: str,
    *,
    kind: str,
    top_level_nets: Sequence[str] | None = None,
) -> str:
    requested = str(requested_net or "")
    device_nets = {
        str(net)
        for inst in tuple(getattr(device_plan, "instances", ()) or ())
        for net in tuple(dict(getattr(inst, "connections", {}) or {}).values())
        if str(net)
    }
    top_level_net_set = {str(net) for net in tuple(top_level_nets or ()) if str(net)}
    if requested and (requested in device_nets or requested in top_level_net_set):
        return requested
    body_nets = tuple(
        net
        for net in _body_supply_nets_by_kind(device_plan).get(str(kind), ())
        if net in device_nets or net in top_level_net_set
    )
    if body_nets:
        return body_nets[0]
    return requested


@dataclass(frozen=True)
class PhysicalLegalitySuggestion:
    action: str
    domain: str
    target: str = ""
    reason: str = ""
    priority: int = 0
    params: Mapping[str, object] = ()


@dataclass(frozen=True)
class PhysicalRepairIteration:
    iteration: int
    issue_count_before: int
    issue_count_after: int
    issue_breakdown_before: dict[str, int] = field(default_factory=dict)
    issue_breakdown_after: dict[str, int] = field(default_factory=dict)
    actions: tuple[str, ...] = ()
    changed: bool = False
    passed: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PhysicalRepairLoopResult:
    plan: LayoutPlan
    passed: bool
    iterations: tuple[PhysicalRepairIteration, ...] = ()
    physical_report: dict[str, object] = field(default_factory=dict)
    interconnect_report: dict[str, object] = field(default_factory=dict)
    summary: tuple[str, ...] = ()


def plan_device_layout_ir(
    device_plan: Any,
    constraints: LayoutConstraintSet | None = None,
    pdk: PdkConfig | None = None,
    *,
    lib: str = "work",
    cell: str = "layout_closure",
    view: str = "layout",
    top_net: str = "VDD",
    bottom_net: str = "VSS",
    shield_net: str = "VSS",
    include_interconnect: bool = True,
    include_power_rails: bool = True,
    include_source_drops: bool = True,
    include_supply_taps: bool = True,
    include_well_regions: bool = False,
    include_guard_ring: bool = False,
    guard_ring_net: str | None = None,
    guard_ring_kind: str = "substrate",
    calibration_cache: PCellCalibrationCache | None = None,
    allow_nearest_calibration: bool = False,
    max_nearest_distance: float = 0.25,
    obstacle_sources: Sequence[Any] = (),
    routing_corridors: Sequence[Any] = (),
    routing_strategy: "AnalogRoutingStrategy | None" = None,
    design_context: "AnalogDesignContext | None" = None,
    solver_guide: "AnalogSolverGuide | None" = None,
    strict_terminal_access: bool = False,
    strict_precheck: bool = False,
    strict_top_level_nets: tuple[str, ...] | None = None,
    strict_require_lvs_labels: bool = False,
    strict_include_open_checks: bool = False,
    strict_require_all_via_landings: bool = False,
    strict_include_via_landing_short_checks: bool = False,
    strict_require_antenna_checks: bool = False,
    antenna_max_metal_length_um: float = 20.0,
    antenna_max_length_per_via_um: float = 10.0,
    strict_require_min_area_checks: bool = False,
    route_min_area_um2_by_layer: Mapping[str, float] | None = None,
) -> LayoutPlan:
    """Assemble reviewed device-level layout proposals as LayoutIR.

    The returned plan is backend-neutral.  Use the OA adapter only at the write
    boundary.
    """

    pdk = pdk or PdkConfig.generic()
    if _requires_minimal_analog_interconnect_backbone(device_plan):
        # Keep reviewed supply rails and source drops so analog backbones do
        # not route VDD/VSS as ordinary long signal trunks across the block.
        include_power_rails = False
        include_source_drops = False
        include_supply_taps = False
        routing_strategy = None
        design_context = None
        solver_guide = None
    target = LayoutCellRef(lib, cell, view, "maskLayout")
    metadata = {
        "source": "plan_device_layout_ir",
        "routing_strategy": _snapshot_analog_routing_strategy(routing_strategy),
        "analog_design_context": _snapshot_analog_design_context(design_context),
        "analog_solver_guide": _snapshot_analog_solver_guide(solver_guide),
    }
    plan_metadata = getattr(device_plan, "metadata", {}) if isinstance(getattr(device_plan, "metadata", {}), Mapping) else {}
    effective_top_level_nets = tuple(
        str(net)
        for net in (
            strict_top_level_nets
            if strict_top_level_nets is not None
            else tuple(plan_metadata.get("top_level_nets", ()))
        )
        if str(net)
    ) or None
    body_supply_nets_by_kind = _body_supply_nets_by_kind(device_plan)
    effective_top_net = top_net
    effective_bottom_net = bottom_net
    metadata["effective_top_net"] = effective_top_net
    metadata["effective_bottom_net"] = effective_bottom_net
    metadata["requested_top_net"] = top_net
    metadata["requested_bottom_net"] = bottom_net
    metadata["body_supply_nets_by_kind"] = body_supply_nets_by_kind
    plans: list[LayoutPlan] = []
    components: list[str] = []

    device_geometry_plan = _pcell_device_layout_plan(device_plan, target=target, pdk=pdk)
    if device_geometry_plan is not None:
        plans.append(device_geometry_plan)
        components.append("pcell_devices")

    available_supply_nets = _available_supply_nets(
        device_plan,
        top_net=effective_top_net,
        bottom_net=effective_bottom_net,
        top_level_nets=effective_top_level_nets,
    )

    rail_plan: LayoutPlan | None = None
    if include_power_rails and available_supply_nets:
        rail_plan = plan_power_rails(
            device_plan,
            pdk,
            lib=lib,
            cell=cell,
            view=view,
            top_net=effective_top_net if effective_top_net in available_supply_nets else None,
            bottom_net=effective_bottom_net if effective_bottom_net in available_supply_nets else None,
            output="layout_ir",
        )

    source_drop_plan: LayoutPlan | None = None
    if include_source_drops and rail_plan is not None:
        if include_supply_taps or include_well_regions:
            source_drop_subplans = []
            top_source_drop_plan = plan_power_source_drops(
                device_plan,
                rail_plan,
                pdk,
                lib=lib,
                cell=cell,
                view=view,
                supply_nets=(effective_top_net,),
                terminals=("S",),
                output="layout_ir",
                calibration_cache=calibration_cache,
                allow_nearest_calibration=allow_nearest_calibration,
                max_nearest_distance=max_nearest_distance,
            )
            if top_source_drop_plan is not None:
                source_drop_subplans.append(top_source_drop_plan)
            bottom_source_drop_plan = plan_power_source_drops(
                device_plan,
                rail_plan,
                pdk,
                lib=lib,
                cell=cell,
                view=view,
                supply_nets=(effective_bottom_net,),
                terminals=("S", "B"),
                output="layout_ir",
                calibration_cache=calibration_cache,
                allow_nearest_calibration=allow_nearest_calibration,
                max_nearest_distance=max_nearest_distance,
            )
            if bottom_source_drop_plan is not None:
                source_drop_subplans.append(bottom_source_drop_plan)
            if source_drop_subplans:
                source_drop_plan = merge_layout_plans(*source_drop_subplans, cell=target, grid=pdk)
        else:
            source_drop_plan = plan_power_source_drops(
                device_plan,
                rail_plan,
                pdk,
                lib=lib,
                cell=cell,
                view=view,
                supply_nets=available_supply_nets,
                terminals=("S", "B"),
                output="layout_ir",
                calibration_cache=calibration_cache,
                allow_nearest_calibration=allow_nearest_calibration,
                max_nearest_distance=max_nearest_distance,
            )

    tap_plan: LayoutPlan | None = None
    if include_supply_taps and rail_plan is not None:
        tap_plan = plan_supply_taps(
            rail_plan,
            pdk,
            lib=lib,
            cell=cell,
            view=view,
            top_net=effective_top_net if effective_top_net in available_supply_nets else None,
            bottom_net=effective_bottom_net if effective_bottom_net in available_supply_nets else None,
            output="layout_ir",
        )

    well_plan: LayoutPlan | None = None
    if include_well_regions:
        well_plan = plan_well_regions(device_plan, pdk, lib=lib, cell=cell, view=view, output="layout_ir")

    guard_plan: LayoutPlan | None = None
    if include_guard_ring:
        guard_plan = plan_guard_ring(
            device_plan,
            pdk,
            lib=lib,
            cell=cell,
            view=view,
            net=guard_ring_net or effective_bottom_net,
            kind=guard_ring_kind,
            connect_to_core=(guard_ring_net or effective_bottom_net) == effective_bottom_net,
            output="layout_ir",
        )

    guard_tap_join_plan: LayoutPlan | None = None
    if guard_plan is not None and tap_plan is not None:
        guard_tap_join_plan = plan_guard_ring_tap_implant_joins(
            guard_plan,
            tap_plan,
            pdk,
            lib=lib,
            cell=cell,
            view=view,
        )
        if not guard_tap_join_plan.rects:
            guard_tap_join_plan = None

    generated_obstacles = tuple(
        plan
        for plan in (rail_plan, source_drop_plan, tap_plan, well_plan, guard_plan, guard_tap_join_plan)
        if plan is not None
    )

    if include_interconnect:
        source_drop_skip_nets: tuple[str, ...] = ()
        if include_power_rails and include_source_drops and source_drop_plan is not None:
            source_drop_materialized_nets = {
                str(getattr(shape, "net", ""))
                for collection in (
                    tuple(getattr(source_drop_plan, "paths", ())),
                    tuple(getattr(source_drop_plan, "rects", ())),
                    tuple(getattr(source_drop_plan, "vias", ())),
                )
                for shape in collection
                if str(getattr(shape, "net", ""))
            }
            source_drop_skip_nets = tuple(
                str(net)
                for net in (effective_top_net, effective_bottom_net)
                if net is not None and str(net) in source_drop_materialized_nets
            )
        plans.append(
            generate_interconnect(
                device_plan,
                constraints,
                pdk,
                lib=lib,
                cell=cell,
                view=view,
                shield_net=shield_net,
                output="layout_ir",
                calibration_cache=calibration_cache,
                allow_nearest_calibration=allow_nearest_calibration,
                max_nearest_distance=max_nearest_distance,
                obstacle_sources=(*obstacle_sources, *generated_obstacles),
                routing_corridors=routing_corridors,
                routing_strategy=routing_strategy,
                strict_terminal_access=strict_terminal_access,
                strict_require_antenna_checks=strict_require_antenna_checks,
                antenna_max_metal_length_um=antenna_max_metal_length_um,
                antenna_max_length_per_via_um=antenna_max_length_per_via_um,
                strict_require_min_area_checks=strict_require_min_area_checks,
                strict_include_via_landing_short_checks=strict_include_via_landing_short_checks,
                route_min_area_um2_by_layer=route_min_area_um2_by_layer,
                strict_top_level_nets=effective_top_level_nets,
                skip_nets=source_drop_skip_nets,
            )
        )
        components.append("interconnect")

    if rail_plan is not None:
        plans.append(rail_plan)
        components.append("power_rails")

    if source_drop_plan is not None:
        plans.append(source_drop_plan)
        components.append("source_drops")

    if tap_plan is not None:
        plans.append(tap_plan)
        components.append("supply_taps")

    if well_plan is not None:
        plans.append(well_plan)
        components.append("well_regions")

    if guard_plan is not None:
        plans.append(guard_plan)
        components.append("guard_ring")

    if guard_tap_join_plan is not None:
        plans.append(guard_tap_join_plan)
        components.append("guard_ring_tap_implant_join")

    if not plans:
        return LayoutPlan(target, metadata={**metadata, "components": ()})

    merged = merge_layout_plans(*plans, cell=target, grid=pdk)
    merged = replace(
        merged,
        metadata={
            **merged.metadata,
            **_device_plan_passthrough_metadata(device_plan),
            **metadata,
            "components": tuple(components),
        },
    )
    if strict_precheck:
        require_interconnect_precheck(
            merged,
            constraints,
            pdk,
            shield_net=shield_net,
            pcell_plan=device_plan,
            calibration_cache=calibration_cache,
            allow_nearest_calibration=allow_nearest_calibration,
            max_nearest_distance=max_nearest_distance,
            routing_corridors=routing_corridors,
            top_level_nets=effective_top_level_nets,
            require_lvs_labels=strict_require_lvs_labels,
            include_open_checks=strict_include_open_checks,
            require_all_via_landings=strict_require_all_via_landings,
            include_via_landing_short_checks=strict_include_via_landing_short_checks,
            require_antenna_checks=strict_require_antenna_checks,
            antenna_max_metal_length_um=antenna_max_metal_length_um,
            antenna_max_length_per_via_um=antenna_max_length_per_via_um,
            require_min_area_checks=strict_require_min_area_checks,
            route_min_area_um2_by_layer=route_min_area_um2_by_layer,
        )
    return merged


def run_physical_drc_repair_loop(
    plan: LayoutPlan,
    *,
    constraints: LayoutConstraintSet | None = None,
    pdk: PdkConfig | None = None,
    max_iterations: int = 4,
    fixed_nets: tuple[str, ...] | list[str] = (),
    min_width_um_by_layer: Mapping[str, float] | None = None,
    min_spacing_um_by_layer: Mapping[str, float] | None = None,
    min_area_um2_by_layer: Mapping[str, float] | None = None,
    require_all_via_landings: bool = True,
    include_via_landing_short_checks: bool = True,
) -> PhysicalRepairLoopResult:
    """Run a bounded DRC repair loop over a backend-neutral ``LayoutPlan``.

    The loop stitches together existing geometry patch, open-route patch,
    short replacement, and via enclosure planners into one iterative closure
    pass with explicit iteration trace.
    """

    pdk = pdk or PdkConfig.generic()
    constraints = constraints or LayoutConstraintSet()
    width_rules = {str(layer): float(value) for layer, value in dict(min_width_um_by_layer or {}).items()}
    spacing_rules = {str(layer): float(value) for layer, value in dict(min_spacing_um_by_layer or {}).items()}
    area_rules = {str(layer): float(value) for layer, value in dict(min_area_um2_by_layer or {}).items()}
    current = _annotate_repair_metadata(snap_layout_plan_to_grid(plan, pdk), source="run_physical_drc_repair_loop")
    preferred_fixed_nets = _resolve_fixed_nets(current, fixed_nets=fixed_nets)
    iterations: list[PhysicalRepairIteration] = []

    for iteration in range(1, max(1, int(max_iterations)) + 1):
        current, pre_dedupe_actions = _dedupe_close_same_net_vias(current, pdk)
        physical_report = analyze_plan_physical_connectivity(
            current,
            include_opens=True,
            include_via_landing_shorts=include_via_landing_short_checks,
            pdk=pdk,
        )
        interconnect_report = analyze_interconnect_plan(
            current,
            constraints,
            pdk,
            include_open_checks=True,
            require_all_via_landings=require_all_via_landings,
            include_via_landing_short_checks=include_via_landing_short_checks,
            require_min_area_checks=bool(area_rules),
            route_min_area_um2_by_layer=area_rules or None,
        )
        width_area_issues, spacing_issues, enclosure_issues = _collect_rule_driven_drc_issues(
            current,
            pdk=pdk,
            min_width_um_by_layer=width_rules,
            min_area_um2_by_layer=area_rules,
            min_spacing_um_by_layer=spacing_rules,
        )
        issue_breakdown_before = _repair_issue_breakdown(
            physical_report,
            interconnect_report,
            width_area_issues=width_area_issues,
            spacing_issues=spacing_issues,
            enclosure_issues=enclosure_issues,
        )
        issue_count_before = sum(issue_breakdown_before.values())
        if issue_count_before == 0:
            summary = (
                f"iterations={len(iterations)}",
                "passed=True",
                "remaining_issues=0",
            )
            return PhysicalRepairLoopResult(
                plan=current,
                passed=True,
                iterations=tuple(iterations),
                physical_report=physical_report,
                interconnect_report=interconnect_report,
                summary=summary,
            )

        actions: list[str] = list(pre_dedupe_actions)
        working = current

        shorts = detect_plan_shape_shorts(
            working,
            include_via_landings=include_via_landing_short_checks,
            pdk=pdk,
        )
        if shorts:
            for short in shorts:
                keep_net = _select_short_keep_net_for_short(
                    short,
                    plan=working,
                    fixed_nets=preferred_fixed_nets,
                    explicit_fixed_nets=fixed_nets,
                )
                victim_net = short.net_b if keep_net == short.net_a else short.net_a
                replacement = plan_lvs_short_replacement(
                    working,
                    keep_net=keep_net,
                    victim_net=victim_net,
                    pdk=pdk,
                    min_spacing_by_layer=spacing_rules or None,
                )
                working = _annotate_repair_metadata(
                    replacement.replacement_layout,
                    source="run_physical_drc_repair_loop",
                    action=f"short_replacement:{keep_net}>{victim_net}",
                )
                actions.append(f"short_replacement:{keep_net}>{victim_net}")

        opens = detect_plan_net_opens(working, pdk=pdk)
        if opens:
            for open_issue in opens:
                try:
                    patch = plan_lvs_open_route_patch(
                        working,
                        pdk=pdk,
                        net=open_issue.net,
                        min_width_by_layer=width_rules or None,
                        min_spacing_by_layer=spacing_rules or None,
                    )
                except ValueError as exc:
                    actions.append(f"open_route_patch_skipped:{open_issue.net}:{exc}")
                    continue
                if not tuple(getattr(patch.layout_patch, "paths", ()) or ()) and not tuple(getattr(patch.layout_patch, "rects", ()) or ()) and not tuple(getattr(patch.layout_patch, "vias", ()) or ()):
                    actions.append(f"open_route_patch_empty:{open_issue.net}")
                    continue
                working = _merge_repair_patch(
                    working,
                    patch.layout_patch,
                    pdk=pdk,
                    source="run_physical_drc_repair_loop",
                    action=f"open_route_patch:{open_issue.net}",
                )
                actions.append(f"open_route_patch:{open_issue.net}")

        if width_area_issues:
            localizations = localize_drc_issues_to_layout(width_area_issues, working)
            if localizations:
                patch_plan = plan_localized_drc_layout_patch(
                    localizations,
                    min_width_by_layer=width_rules or None,
                    min_area_by_layer=area_rules or None,
                    pdk=pdk,
                    base_plan=working,
                )
                working = _apply_geometry_edits_replacement(
                    working,
                    patch_plan.edits,
                    pdk=pdk,
                    source="run_physical_drc_repair_loop",
                    action=f"localized_drc_patch:{len(localizations)}",
                )
                actions.append(f"localized_drc_patch:{len(localizations)}")

        if spacing_issues:
            notch_patch = _plan_same_net_notch_fill_patch(
                working,
                spacing_issues,
                min_spacing_by_layer=spacing_rules,
                pdk=pdk,
            )
            notch_rect_count = len(tuple(getattr(notch_patch, "rects", ()) or ()))
            if notch_rect_count:
                working = _merge_repair_patch(
                    working,
                    notch_patch,
                    pdk=pdk,
                    source="run_physical_drc_repair_loop",
                    action=f"same_net_notch_fill:{notch_rect_count}",
                )
                actions.append(f"same_net_notch_fill:{notch_rect_count}")
            spacing_localizations = localize_spacing_drc_issues_to_layout(
                spacing_issues,
                working,
                min_spacing_by_layer=spacing_rules,
            )
            if spacing_localizations:
                replacement = plan_localized_spacing_replacement(
                    spacing_localizations,
                    base_plan=working,
                    fixed_nets=preferred_fixed_nets,
                    pdk=pdk,
                )
                if tuple(getattr(replacement, "edits", ()) or ()):
                    working = _annotate_repair_metadata(
                        replacement.replacement_layout,
                        source="run_physical_drc_repair_loop",
                        action=f"spacing_replacement:{len(spacing_localizations)}",
                    )
                    actions.append(f"spacing_replacement:{len(spacing_localizations)}")
                else:
                    actions.append(f"spacing_replacement_empty:{len(spacing_localizations)}")

        if require_all_via_landings:
            landing_margin = max((pdk.rules.min_width_um(layer) / 2.0) for layer in pdk.layer_map.metals[:2]) if len(pdk.layer_map.metals) >= 2 else pdk.rules.grid_step_um
            via_patch = plan_via_enclosure_patch(working, pdk, landing_margin_um=landing_margin)
            if via_patch.edits:
                working = _merge_repair_patch(
                    working,
                    via_patch.layout_patch,
                    pdk=pdk,
                    source="run_physical_drc_repair_loop",
                    action=f"via_enclosure_patch:{len(via_patch.edits)}",
                )
                actions.append(f"via_enclosure_patch:{len(via_patch.edits)}")

        working, post_dedupe_actions = _dedupe_close_same_net_vias(working, pdk)
        actions.extend(post_dedupe_actions)

        physical_after = analyze_plan_physical_connectivity(
            working,
            include_opens=True,
            include_via_landing_shorts=include_via_landing_short_checks,
            pdk=pdk,
        )
        interconnect_after = analyze_interconnect_plan(
            working,
            constraints,
            pdk,
            include_open_checks=True,
            require_all_via_landings=require_all_via_landings,
            include_via_landing_short_checks=include_via_landing_short_checks,
            require_min_area_checks=bool(area_rules),
            route_min_area_um2_by_layer=area_rules or None,
        )
        width_area_after, spacing_after, enclosure_after = _collect_rule_driven_drc_issues(
            working,
            pdk=pdk,
            min_width_um_by_layer=width_rules,
            min_area_um2_by_layer=area_rules,
            min_spacing_um_by_layer=spacing_rules,
        )
        issue_breakdown_after = _repair_issue_breakdown(
            physical_after,
            interconnect_after,
            width_area_issues=width_area_after,
            spacing_issues=spacing_after,
            enclosure_issues=enclosure_after,
        )
        issue_count_after = sum(issue_breakdown_after.values())
        changed = working != current
        passed = issue_count_after == 0
        iterations.append(
            PhysicalRepairIteration(
                iteration=iteration,
                issue_count_before=issue_count_before,
                issue_count_after=issue_count_after,
                issue_breakdown_before=issue_breakdown_before,
                issue_breakdown_after=issue_breakdown_after,
                actions=tuple(actions),
                changed=changed,
                passed=passed,
                metadata={"fixed_nets": preferred_fixed_nets},
            )
        )
        current = working
        if passed or not changed:
            summary = (
                f"iterations={len(iterations)}",
                f"passed={passed}",
                f"remaining_issues={issue_count_after}",
            )
            return PhysicalRepairLoopResult(
                plan=current,
                passed=passed,
                iterations=tuple(iterations),
                physical_report=physical_after,
                interconnect_report=interconnect_after,
                summary=summary,
            )

    final_physical = analyze_plan_physical_connectivity(
        current,
        include_opens=True,
        include_via_landing_shorts=include_via_landing_short_checks,
        pdk=pdk,
    )
    final_interconnect = analyze_interconnect_plan(
        current,
        constraints,
        pdk,
        include_open_checks=True,
        require_all_via_landings=require_all_via_landings,
        include_via_landing_short_checks=include_via_landing_short_checks,
        require_min_area_checks=bool(area_rules),
        route_min_area_um2_by_layer=area_rules or None,
    )
    final_width_area, final_spacing, final_enclosure = _collect_rule_driven_drc_issues(
        current,
        pdk=pdk,
        min_width_um_by_layer=width_rules,
        min_area_um2_by_layer=area_rules,
        min_spacing_um_by_layer=spacing_rules,
    )
    final_breakdown = _repair_issue_breakdown(
        final_physical,
        final_interconnect,
        width_area_issues=final_width_area,
        spacing_issues=final_spacing,
        enclosure_issues=final_enclosure,
    )
    summary = (
        f"iterations={len(iterations)}",
        "passed=False",
        f"remaining_issues={sum(final_breakdown.values())}",
    )
    return PhysicalRepairLoopResult(
        plan=current,
        passed=False,
        iterations=tuple(iterations),
        physical_report=final_physical,
        interconnect_report=final_interconnect,
        summary=summary,
    )


def _requires_minimal_analog_interconnect_backbone(device_plan: Any) -> bool:
    instances = tuple(getattr(device_plan, "instances", ()))
    names = {str(getattr(instance, "name", "")) for instance in instances}
    padc = {"REFBUF_P", "REFBUF_N", "S1_SWP", "S1_SWN", "S1_INP", "S1_INN", "S2_SWP", "S2_SWN", "S2_INP", "S2_INN", "FLASH_INP", "FLASH_INN"}
    mdac = {"SWP", "SWN", "INP", "INN", "LOADP", "LOADN", "TAIL", "CAPP", "CAPN"}
    bandgap = {"Q1", "R1", "M3A", "M3B", "M1A", "M1B", "M5A", "M5B", "M7"}
    mdac_specialized = mdac.issubset({name.rsplit("_", 1)[-1] for name in names})
    return padc.issubset(names) or mdac_specialized or bandgap.issubset(names)


def _collect_rule_driven_drc_issues(
    plan: LayoutPlan,
    *,
    pdk: PdkConfig,
    min_width_um_by_layer: Mapping[str, float],
    min_area_um2_by_layer: Mapping[str, float],
    min_spacing_um_by_layer: Mapping[str, float],
    array_spacing_um_by_layer: Mapping[str, float] | None = None,
    diagonal_spacing_um_by_layer: Mapping[str, float] | None = None,
    extension_um_by_layer: Mapping[str, float] | None = None,
) -> tuple[tuple[DrcIssue, ...], tuple[DrcIssue, ...], tuple[DrcIssue, ...]]:
    width_area_issues: list[DrcIssue] = []
    spacing_issues: list[DrcIssue] = []
    enclosure_issues: list[DrcIssue] = []
    array_spacing_rules = {
        str(layer): float(value)
        for layer, value in dict(array_spacing_um_by_layer or {}).items()
        if float(value) > 0.0
    }
    diagonal_spacing_rules = {
        str(layer): float(value)
        for layer, value in dict(diagonal_spacing_um_by_layer or {}).items()
        if float(value) > 0.0
    }
    extension_rules = {
        str(layer): float(value)
        for layer, value in dict(extension_um_by_layer or {}).items()
        if float(value) > 0.0
    }
    rule_shapes: list[object] = []
    width_area_shapes: list[object] = []
    auxiliary_width_area_shapes: list[object] = []
    for rect in tuple(getattr(plan, "rects", ()) or ()):
        layer = str(getattr(rect, "layer", "") or "")
        net = str(getattr(rect, "net", "") or "")
        metadata = getattr(rect, "metadata", {}) if isinstance(getattr(rect, "metadata", {}), Mapping) else {}
        if not layer or not net:
            continue
        if str(dict(metadata).get("kind", "")) in {"via_landing", "pin_anchor"}:
            auxiliary_width_area_shapes.append(rect)
            continue
        rule_shapes.append(rect)
        width_area_shapes.append(rect)
    for path in tuple(getattr(plan, "paths", ()) or ()):
        layer = str(getattr(path, "layer", "") or "")
        net = str(getattr(path, "net", "") or "")
        if not layer or not net:
            continue
        try:
            points = tuple(tuple(float(value) for value in tuple(point)[:2]) for point in tuple(getattr(path, "points", ()) or ()))
            width = float(getattr(path, "width", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        for bbox in path_segment_bboxes(points, width):
            shape = SimpleNamespace(layer=layer, net=net, bbox=bbox)
            rule_shapes.append(shape)
            width_area_shapes.append(shape)
    marker_shapes = _collect_inline_marker_rect_shapes(plan)
    connected_auxiliary_shapes = tuple(
        aux
        for aux in auxiliary_width_area_shapes
        if any(
            str(getattr(aux, "layer", "") or "") == str(getattr(shape, "layer", "") or "")
            and str(getattr(aux, "net", "") or "") == str(getattr(shape, "net", "") or "")
            and bbox_overlaps(tuple(getattr(aux, "bbox", ())), tuple(getattr(shape, "bbox", ())), include_touching=True)
            for shape in width_area_shapes
        )
    )
    # Spacing must not check against an artificial bounding box for an L-shaped
    # same-net conductor.  Use strictly rectangular-preserving coalescing here;
    # width/min-area keeps the older permissive coalescing to avoid reintroducing
    # noisy min-area reports for connected access fragments.
    shapes = _coalesce_rule_check_shapes((*rule_shapes, *marker_shapes), allow_l_shape_bbox_merge=False)
    width_area_check_shapes = _coalesce_rule_check_shapes(
        (*width_area_shapes, *connected_auxiliary_shapes, *marker_shapes),
        allow_l_shape_bbox_merge=True,
    )
    for shape in width_area_check_shapes:
        layer = str(shape["layer"])
        width_rule = float(min_width_um_by_layer.get(layer, 0.0) or 0.0)
        if width_rule > 0.0:
            bbox = tuple(shape["bbox"])
            width = min(bbox[2] - bbox[0], bbox[3] - bbox[1])
            if width < width_rule - 1e-12:
                width_area_issues.append(
                    DrcIssue(
                        "MIN_WIDTH",
                        layer,
                        f"{layer} width {width:.4g}um below {width_rule:.4g}um",
                        bbox,
                    )
                )
        area_rule = float(min_area_um2_by_layer.get(layer, 0.0) or 0.0)
        if area_rule > 0.0:
            bbox = tuple(shape["bbox"])
            area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
            if area < area_rule - 1e-12:
                width_area_issues.append(
                    DrcIssue(
                        "MIN_AREA",
                        layer,
                        f"{layer} area {area:.4g}um2 below {area_rule:.4g}um2",
                        bbox,
                    )
                )
    by_layer: dict[str, list[object]] = {}
    for shape in shapes:
        by_layer.setdefault(str(shape["layer"]), []).append(shape)
    for layer, layer_shapes in by_layer.items():
        spacing_rule = float(min_spacing_um_by_layer.get(layer, 0.0) or 0.0)
        if spacing_rule <= 0.0:
            continue
        array_rule = float(array_spacing_rules.get(layer, 0.0) or 0.0)
        diagonal_rule = float(diagonal_spacing_rules.get(layer, 0.0) or 0.0)
        for idx, left in enumerate(layer_shapes):
            for right in layer_shapes[idx + 1 :]:
                left_net = str(left["net"])
                right_net = str(right["net"])
                same_net = bool(left_net) and left_net == right_net
                if same_net:
                    if _shape_pair_uses_notch(tuple(left["bbox"]), tuple(right["bbox"])):
                        notch_gap = _axis_aligned_gap(tuple(left["bbox"]), tuple(right["bbox"]))
                        if 0.0 <= notch_gap < spacing_rule - 1e-12:
                            fill_bbox = _same_net_notch_fill_bbox(tuple(left["bbox"]), tuple(right["bbox"]))
                            if fill_bbox is not None and any(
                                other is not left
                                and other is not right
                                and str(other["net"]) == left_net
                                and bbox_contains(tuple(other["bbox"]), fill_bbox, include_touching=True)
                                for other in layer_shapes
                            ):
                                continue
                            spacing_issues.append(
                                DrcIssue(
                                    "NOTCH_SPACING",
                                    layer,
                                    f"{layer} notch spacing {notch_gap:.4g}um below {spacing_rule:.4g}um",
                                    _bbox_union(tuple(left["bbox"]), tuple(right["bbox"])),
                                )
                            )
                    continue
                required_spacing = _required_spacing_for_shape_pair(
                    tuple(left["bbox"]),
                    tuple(right["bbox"]),
                    min_spacing_um=spacing_rule,
                    array_spacing_um=array_rule,
                    diagonal_spacing_um=diagonal_rule,
                )
                distance = _bbox_distance(tuple(left["bbox"]), tuple(right["bbox"]))
                if distance >= required_spacing - 1e-12:
                    continue
                if _shape_pair_uses_eol_spacing(tuple(left["bbox"]), tuple(right["bbox"])):
                    spacing_issues.append(
                        DrcIssue(
                            "EOL_SPACING",
                            layer,
                            f"{layer} line-end spacing {distance:.4g}um below {required_spacing:.4g}um",
                            _bbox_union(tuple(left["bbox"]), tuple(right["bbox"])),
                        )
                    )
                    continue
                spacing_issues.append(
                    DrcIssue(
                        "MIN_SPACING",
                        layer,
                        f"{layer} spacing {distance:.4g}um below {required_spacing:.4g}um",
                        _bbox_union(tuple(left["bbox"]), tuple(right["bbox"])),
                    )
                )
    enclosure_issues.extend(_collect_rule_driven_enclosure_issues(plan, pdk=pdk, extension_um_by_layer=extension_rules))
    return (
        tuple(width_area_issues),
        tuple(_dedupe_drc_issues(spacing_issues)),
        tuple(_dedupe_drc_issues(enclosure_issues)),
    )


def _plan_same_net_notch_fill_patch(
    plan: LayoutPlan,
    spacing_issues: Sequence[DrcIssue],
    *,
    min_spacing_by_layer: Mapping[str, float],
    pdk: PdkConfig,
) -> LayoutPlan:
    """Create additive same-net metal fills for local notch-spacing issues.

    This is intentionally narrower than generic spacing repair.  Opposite-net
    spacing must be handled by reroute/push; same-net notch spacing is normally
    fixed by filling the notch between two same-layer conductors on the same net.
    The fill is skipped if the target union overlaps any other-net shape on the
    same layer.
    """

    rule_shapes: list[object] = []
    for rect_idx, rect in enumerate(tuple(getattr(plan, "rects", ()) or ())):
        layer = str(getattr(rect, "layer", "") or "")
        net = str(getattr(rect, "net", "") or "")
        metadata = getattr(rect, "metadata", {}) if isinstance(getattr(rect, "metadata", {}), Mapping) else {}
        if not layer or not net:
            continue
        if str(dict(metadata).get("kind", "")) in {"via_landing", "pin_anchor"}:
            continue
        try:
            bbox = tuple(float(value) for value in tuple(getattr(rect, "bbox", ()))[:4])
        except (TypeError, ValueError):
            continue
        if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        rule_shapes.append(SimpleNamespace(layer=layer, net=net, bbox=bbox, source=f"rect[{rect_idx}]"))
    for path_idx, path in enumerate(tuple(getattr(plan, "paths", ()) or ())):
        layer = str(getattr(path, "layer", "") or "")
        net = str(getattr(path, "net", "") or "")
        if not layer or not net:
            continue
        try:
            points = tuple(tuple(float(value) for value in tuple(point)[:2]) for point in tuple(getattr(path, "points", ()) or ()))
            width = float(getattr(path, "width", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if width <= 0.0 or len(points) < 2:
            continue
        for segment_idx, bbox in enumerate(path_segment_bboxes(points, width)):
            rule_shapes.append(
                SimpleNamespace(
                    layer=layer,
                    net=net,
                    bbox=tuple(float(value) for value in tuple(bbox)[:4]),
                    source=f"path[{path_idx}].segment[{segment_idx}]",
                )
            )

    coalesced = tuple(_coalesce_rule_check_shapes(rule_shapes, allow_l_shape_bbox_merge=False))
    by_layer: dict[str, list[Mapping[str, object]]] = {}
    for shape in coalesced:
        by_layer.setdefault(str(shape.get("layer", "") or ""), []).append(shape)

    rects: list[LayoutRect] = []
    seen: set[tuple[str, str, tuple[float, float, float, float]]] = set()
    for issue in tuple(spacing_issues or ()):
        if str(issue.rule).upper() != "NOTCH_SPACING":
            continue
        layer = str(issue.layer or "")
        required = float(dict(min_spacing_by_layer).get(layer, 0.0) or 0.0)
        if not layer or required <= 0.0 or issue.bbox is None:
            continue
        try:
            issue_bbox = tuple(float(value) for value in tuple(issue.bbox)[:4])
        except (TypeError, ValueError):
            continue
        if len(issue_bbox) != 4:
            continue
        candidates = tuple(
            shape
            for shape in by_layer.get(layer, ())
            if bbox_overlaps(tuple(shape.get("bbox", ())), issue_bbox, include_touching=True)
        )
        for idx, left in enumerate(candidates):
            for right in candidates[idx + 1 :]:
                left_net = str(left.get("net", "") or "")
                right_net = str(right.get("net", "") or "")
                if not left_net or left_net != right_net:
                    continue
                left_bbox = tuple(float(value) for value in tuple(left.get("bbox", ()))[:4])
                right_bbox = tuple(float(value) for value in tuple(right.get("bbox", ()))[:4])
                if not _shape_pair_uses_notch(left_bbox, right_bbox):
                    continue
                notch_gap = _axis_aligned_gap(left_bbox, right_bbox)
                if notch_gap < 0.0 or notch_gap >= required - 1e-12:
                    continue
                target_bbox = _same_net_notch_fill_bbox(left_bbox, right_bbox)
                if target_bbox is None:
                    continue
                if not bbox_overlaps(target_bbox, issue_bbox, include_touching=True):
                    continue
                if any(
                    str(other.get("net", "") or "") not in {"", left_net}
                    and bbox_overlaps(target_bbox, tuple(other.get("bbox", ())), include_touching=False)
                    for other in by_layer.get(layer, ())
                ):
                    continue
                key = (layer, left_net, target_bbox)
                if key in seen:
                    continue
                seen.add(key)
                rects.append(
                    LayoutRect(
                        layer,
                        target_bbox,
                        left_net,
                        metadata={
                            "kind": "same_net_notch_fill",
                            "source_issue": issue.rule,
                            "source_issue_bbox": issue_bbox,
                            "required_spacing_um": required,
                            "notch_gap_um": notch_gap,
                        },
                    )
                )
    return LayoutPlan(
        plan.cell,
        nets=tuple(dict.fromkeys((*tuple(getattr(plan, "nets", ()) or ()), *(rect.net for rect in rects if rect.net)))),
        rects=tuple(rects),
        metadata={"source": "same_net_notch_fill_patch", "rect_count": len(rects)},
    )


def _same_net_notch_fill_bbox(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    tol_um: float = 1e-12,
) -> tuple[float, float, float, float] | None:
    x_overlap = min(left[2], right[2]) - max(left[0], right[0])
    y_overlap = min(left[3], right[3]) - max(left[1], right[1])
    dx = max(right[0] - left[2], left[0] - right[2], 0.0)
    dy = max(right[1] - left[3], left[1] - right[3], 0.0)
    if y_overlap > tol_um and dx > tol_um:
        bbox = (
            min(left[0], right[0]),
            max(left[1], right[1]),
            max(left[2], right[2]),
            min(left[3], right[3]),
        )
    elif x_overlap > tol_um and dy > tol_um:
        bbox = (
            max(left[0], right[0]),
            min(left[1], right[1]),
            min(left[2], right[2]),
            max(left[3], right[3]),
        )
    else:
        return None
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return bbox


def _required_spacing_for_shape_pair(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    min_spacing_um: float,
    array_spacing_um: float = 0.0,
    diagonal_spacing_um: float = 0.0,
) -> float:
    required = float(min_spacing_um)
    if array_spacing_um > 0.0 and _shape_pair_uses_array_spacing(left, right):
        required = max(required, float(array_spacing_um))
    if diagonal_spacing_um > 0.0 and _shape_pair_uses_diagonal_spacing(left, right):
        required = max(required, float(diagonal_spacing_um))
    return required


def _shape_pair_uses_diagonal_spacing(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    tol_um: float = 1e-12,
) -> bool:
    x_overlap = min(left[2], right[2]) - max(left[0], right[0])
    y_overlap = min(left[3], right[3]) - max(left[1], right[1])
    if x_overlap > tol_um or y_overlap > tol_um:
        return False
    dx = max(right[0] - left[2], left[0] - right[2], 0.0)
    dy = max(right[1] - left[3], left[1] - right[3], 0.0)
    return dx > tol_um and dy > tol_um


def _shape_pair_uses_array_spacing(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    tol_um: float = 1e-12,
) -> bool:
    left_orient = _shape_long_axis_orientation(left)
    right_orient = _shape_long_axis_orientation(right)
    if left_orient == "square" or right_orient == "square" or left_orient != right_orient:
        return False
    x_overlap = min(left[2], right[2]) - max(left[0], right[0])
    y_overlap = min(left[3], right[3]) - max(left[1], right[1])
    if left_orient == "h":
        return x_overlap > tol_um and y_overlap <= tol_um
    if left_orient == "v":
        return y_overlap > tol_um and x_overlap <= tol_um
    return False


def _shape_long_axis_orientation(bbox: tuple[float, float, float, float], *, tol_um: float = 1e-12) -> str:
    width = float(bbox[2] - bbox[0])
    height = float(bbox[3] - bbox[1])
    if width > height + tol_um:
        return "h"
    if height > width + tol_um:
        return "v"
    return "square"


def _shape_pair_uses_notch(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    tol_um: float = 1e-12,
) -> bool:
    x_overlap = min(left[2], right[2]) - max(left[0], right[0])
    y_overlap = min(left[3], right[3]) - max(left[1], right[1])
    dx = max(right[0] - left[2], left[0] - right[2], 0.0)
    dy = max(right[1] - left[3], left[1] - right[3], 0.0)
    return (x_overlap > tol_um and dy > tol_um) or (y_overlap > tol_um and dx > tol_um)


def _axis_aligned_gap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    dx = max(right[0] - left[2], left[0] - right[2], 0.0)
    dy = max(right[1] - left[3], left[1] - right[3], 0.0)
    if dx > 0.0 and dy > 0.0:
        return min(dx, dy)
    return max(dx, dy)


def _shape_pair_uses_eol_spacing(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    tol_um: float = 1e-12,
) -> bool:
    return _shape_uses_eol_against_neighbor(left, right, tol_um=tol_um) or _shape_uses_eol_against_neighbor(right, left, tol_um=tol_um)


def _shape_uses_eol_against_neighbor(
    driver: tuple[float, float, float, float],
    neighbor: tuple[float, float, float, float],
    *,
    tol_um: float = 1e-12,
) -> bool:
    orient = _shape_long_axis_orientation(driver, tol_um=tol_um)
    if orient == "h":
        y_overlap = min(driver[3], neighbor[3]) - max(driver[1], neighbor[1])
        if y_overlap <= tol_um:
            return False
        gap_left = driver[0] - neighbor[2]
        gap_right = neighbor[0] - driver[2]
        return gap_left > tol_um or gap_right > tol_um
    if orient == "v":
        x_overlap = min(driver[2], neighbor[2]) - max(driver[0], neighbor[0])
        if x_overlap <= tol_um:
            return False
        gap_bottom = driver[1] - neighbor[3]
        gap_top = neighbor[1] - driver[3]
        return gap_bottom > tol_um or gap_top > tol_um
    return False


def _collect_rule_driven_enclosure_issues(
    plan: LayoutPlan,
    *,
    pdk: PdkConfig,
    extension_um_by_layer: Mapping[str, float],
) -> tuple[DrcIssue, ...]:
    deferred_cover_kinds = _inline_deferred_cover_check_kinds(pdk)
    deferred_rect_sources = {
        f"rect[{index}]"
        for index, rect in enumerate(tuple(getattr(plan, "rects", ()) or ()))
        if str(dict(getattr(rect, "metadata", {}) or {}).get("kind", "") or "") in deferred_cover_kinds
    }
    shapes = list(collect_plan_shapes(plan, include_pins=False))
    shapes.extend(_collect_inline_marker_rect_shapes(plan))
    shapes.extend(_collect_inline_instance_fallback_shapes(plan, pdk=pdk))
    shapes_by_net_layer: dict[tuple[str, str], list[object]] = {}
    shapes_by_layer: dict[str, list[object]] = {}
    seen_layer_shapes: set[tuple[str, tuple[float, float, float, float], str]] = set()
    for shape in shapes:
        try:
            bbox = tuple(float(value) for value in tuple(getattr(shape, "bbox", (0.0, 0.0, 0.0, 0.0)))[:4])
        except (TypeError, ValueError):
            continue
        layer = str(getattr(shape, "layer", "") or "")
        net = str(getattr(shape, "net", "") or "")
        if not layer or len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        layer_key = (layer, bbox, net)
        if layer_key in seen_layer_shapes:
            continue
        seen_layer_shapes.add(layer_key)
        if net:
            shapes_by_net_layer.setdefault((net, layer), []).append(shape)
        shapes_by_layer.setdefault(str(shape.layer), []).append(shape)

    issues: list[DrcIssue] = []
    legacy_rules = _legacy_inline_rule_tables(pdk)
    legacy_enclosure_by_pair = {
        str(key): float(value)
        for key, value in dict(legacy_rules.get("enclosure_um_by_pair", {}) or {}).items()
        if float(value) > 0.0
    }
    legacy_array_spacing_by_layer = {
        str(key): float(value)
        for key, value in dict(legacy_rules.get("array_spacing_um_by_layer", {}) or {}).items()
        if float(value) > 0.0
    }
    for via_index, via in enumerate(getattr(plan, "vias", ())):
        via_def = str(getattr(via, "via_def", "") or "")
        net = str(getattr(via, "net", "") or "")
        if not via_def or not net:
            continue
        via_metadata = getattr(via, "metadata", {}) if isinstance(getattr(via, "metadata", {}), Mapping) else {}
        explicit_landing_layers = tuple(str(layer) for layer in tuple(via_metadata.get("landing_layers", ()) or ()) if str(layer))
        explicit_landing_set = set(explicit_landing_layers)
        for mode, required_layers in _enclosure_requirement_groups(via_def, pdk):
            if explicit_landing_set:
                required_layers = tuple(layer for layer in required_layers if layer in explicit_landing_set)
            if not required_layers:
                continue
            landing_bboxes = _landing_bboxes_for_layers(via, pdk, required_layers=required_layers)
            if not landing_bboxes:
                continue
            missing_layers: list[tuple[str, tuple[float, float, float, float], float]] = []
            for layer in required_layers:
                landing = landing_bboxes.get(layer)
                if landing is None:
                    continue
                margin = _required_enclosure_margin_um(
                    pdk,
                    via_def=via_def,
                    layer=layer,
                    extension_um_by_layer=extension_um_by_layer,
                    legacy_enclosure_um_by_pair=legacy_enclosure_by_pair,
                )
                required_bbox = _expand_bbox(tuple(landing), margin)
                layer_shapes = _enclosure_candidate_shapes_for_via_layer(
                    via_def,
                    layer,
                    net=net,
                    pdk=pdk,
                    shapes_by_net_layer=shapes_by_net_layer,
                    shapes_by_layer=shapes_by_layer,
                )
                if any(bbox_contains(tuple(getattr(shape, "bbox", ())), required_bbox, include_touching=True) for shape in layer_shapes):
                    continue
                missing_layers.append((layer, required_bbox, margin))
            if mode == "any" and len(missing_layers) < len(required_layers):
                continue
            for layer, required_bbox, margin in missing_layers:
                issues.append(
                    DrcIssue(
                        "MIN_ENCLOSURE",
                        layer,
                        f"{layer} enclosure/extension around {via_def} for net {net} below {margin:.4g}um",
                        required_bbox,
                    )
                )

    cover_shapes_by_layer = {
        layer: tuple(
            shape
            for shape in layer_shapes
            if str(getattr(shape, "source", "") or "") not in deferred_rect_sources
        )
        for layer, layer_shapes in shapes_by_layer.items()
    }
    issues.extend(
        _collect_cover_enclosure_issues(
            cover_shapes_by_layer,
            pdk=pdk,
            legacy_enclosure_um_by_pair=legacy_enclosure_by_pair,
        )
    )
    issues.extend(
        _collect_via_array_issues(
            plan,
            pdk=pdk,
            legacy_array_spacing_um_by_layer=legacy_array_spacing_by_layer,
        )
    )
    issues.extend(_collect_gate_extension_issues(shapes_by_net_layer, extension_um_by_layer=extension_um_by_layer))
    return tuple(issues)


def _enclosure_candidate_shapes_for_via_layer(
    via_def: str,
    layer: str,
    *,
    net: str,
    pdk: PdkConfig,
    shapes_by_net_layer: Mapping[tuple[str, str], Sequence[object]],
    shapes_by_layer: Mapping[str, Sequence[object]],
) -> tuple[object, ...]:
    active = str(getattr(pdk.layer_map, "active", "") or "")
    gate = str(getattr(pdk.layer_map, "gate", "") or "")
    contact = str(getattr(pdk.layer_map, "contact", "") or "")
    if via_def == contact and layer in {active, gate}:
        return tuple(shapes_by_layer.get(layer, ()))
    return tuple(shapes_by_net_layer.get((net, layer), ()))


def _collect_inline_instance_fallback_shapes(
    plan: Any,
    *,
    pdk: PdkConfig,
) -> tuple[object, ...]:
    fallback_shapes: list[object] = []
    seen: set[tuple[str, tuple[float, float, float, float], str]] = set()
    for instance in tuple(getattr(plan, "instances", ()) or ()):
        instance_metadata = dict(getattr(instance, "metadata", {}) or {})
        native_pcell = str(instance_metadata.get("instantiation_method", "") or "") == "dbCreateParamInst"
        internal_marker_owned = _layout_instance_owns_internal_marker_rules(instance)
        for shape in _inline_fallback_shapes_for_layout_instance(instance, pdk=pdk):
            try:
                bbox = tuple(float(value) for value in tuple(getattr(shape, "bbox", (0.0, 0.0, 0.0, 0.0)))[:4])
            except (TypeError, ValueError):
                continue
            layer = str(getattr(shape, "layer", "") or "")
            net = str(getattr(shape, "net", "") or "")
            if not layer or len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                continue
            key = (layer, bbox, net)
            if key in seen:
                continue
            seen.add(key)
            fallback_shapes.append(
                SimpleNamespace(
                    layer=layer,
                    net=net,
                    bbox=bbox,
                    kind="instance_fallback",
                    source=f"instance_fallback.{str(getattr(instance, 'name', '') or '<unnamed>')}",
                    native_pcell=native_pcell,
                    internal_marker_owned=internal_marker_owned,
                )
            )
    return tuple(fallback_shapes)


def _layout_instance_owns_internal_marker_rules(instance: Any) -> bool:
    """Return True when fallback OD/PO belongs to an instantiated device cell.

    Inline fallback geometry is an abstract proxy used by the lightweight checker
    to reason about pins/access and connectivity.  For native PCells and fixed
    device masters, well/implant/pmetal marker ownership stays inside that
    instantiated cell.  Requiring an additional top-level marker cover around
    the fallback OD/PO creates false errors that Calibre will not report against
    the streamed hierarchical layout.

    Drawn primitives intentionally remain False: their marker coverage must be
    represented explicitly at the top level.
    """

    metadata = getattr(instance, "metadata", {})
    if not isinstance(metadata, Mapping):
        return False
    method = str(metadata.get("instantiation_method", "") or "").strip().lower()
    return method in {"dbcreateparaminst", "dbcreateinstbymastername"}


def _collect_inline_marker_rect_shapes(plan: Any) -> tuple[object, ...]:
    marker_shapes: list[object] = []
    seen: set[tuple[str, tuple[float, float, float, float], str]] = set()
    for rect in tuple(getattr(plan, "rects", ()) or ()):
        layer = str(getattr(rect, "layer", "") or "")
        net = str(getattr(rect, "net", "") or "")
        purpose = str(getattr(rect, "purpose", "") or "")
        if net:
            continue
        if purpose and purpose != "drawing":
            continue
        try:
            bbox = tuple(float(value) for value in tuple(getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0)))[:4])
        except (TypeError, ValueError):
            continue
        if not layer or len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        key = (layer, bbox, net)
        if key in seen:
            continue
        seen.add(key)
        marker_shapes.append(
            SimpleNamespace(
                layer=layer,
                net="",
                bbox=bbox,
                kind="marker_rect",
                source=f"marker_rect.{layer}",
            )
        )
    return tuple(marker_shapes)


def _required_enclosure_margin_um(
    pdk: PdkConfig,
    *,
    via_def: str,
    layer: str,
    extension_um_by_layer: Mapping[str, float],
    legacy_enclosure_um_by_pair: Mapping[str, float] | None = None,
) -> float:
    values = [float(getattr(pdk.rules, "grid_step_um", 0.0) or 0.0)]
    for key in (f"{via_def}_{layer}", f"{layer}_{via_def}"):
        legacy_value = float(dict(legacy_enclosure_um_by_pair or {}).get(key, 0.0) or 0.0)
        if legacy_value > 0.0:
            values.append(legacy_value)
        try:
            values.append(float(pdk.rules.enclosure(key)) * 1e-3)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return max(values or [0.0])


def _enclosure_requirement_groups(via_def: str, pdk: PdkConfig) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if via_def == str(getattr(pdk.layer_map, "contact", "") or ""):
        lower_layers = tuple(
            layer
            for layer in (str(getattr(pdk.layer_map, "active", "") or ""), str(getattr(pdk.layer_map, "gate", "") or ""))
            if layer
        )
        top_metal = str(tuple(getattr(pdk.layer_map, "metals", ("M1",)))[0])
        return (("all", (top_metal,)), ("any", lower_layers))
    via_layers = tuple(layer for layer in _via_required_layers(via_def, pdk) if str(layer))
    if via_layers:
        return (("all", via_layers),)
    return ()


def _landing_bboxes_for_layers(
    via: Any,
    pdk: PdkConfig,
    *,
    required_layers: Sequence[str],
) -> dict[str, tuple[float, float, float, float]]:
    via_metadata = getattr(via, "metadata", {}) if isinstance(getattr(via, "metadata", {}), Mapping) else {}
    if tuple(required_layers) == tuple(_via_required_layers(str(getattr(via, "via_def", "") or ""), pdk)):
        try:
            return dict(via_landing_bboxes(via, pdk, landing_margin_um=0.0))
        except Exception:
            return {}
    try:
        xy = tuple(float(value) for value in tuple(getattr(via, "xy", (0.0, 0.0)))[:2])
    except (TypeError, ValueError):
        return {}
    point_bbox = (xy[0], xy[1], xy[0], xy[1])
    explicit = tuple(str(layer) for layer in tuple(via_metadata.get("landing_layers", ())) if str(layer))
    if explicit:
        return {layer: point_bbox for layer in explicit if layer in set(required_layers)}
    return {str(layer): point_bbox for layer in required_layers if str(layer)}


def _collect_cover_enclosure_issues(
    shapes_by_layer: Mapping[str, Sequence[object]],
    *,
    pdk: PdkConfig,
    legacy_enclosure_um_by_pair: Mapping[str, float] | None = None,
) -> tuple[DrcIssue, ...]:
    pair_rules: dict[tuple[str, str], float] = {}
    for key, value in dict(getattr(pdk.rules, "enclosure_nm", {}) or {}).items():
        text = str(key)
        if "_" not in text:
            continue
        outer, inner = text.split("_", 1)
        numeric = float(value) * 1e-3
        if numeric > 0.0:
            pair_rules[(outer, inner)] = max(pair_rules.get((outer, inner), 0.0), numeric)
    for key, value in dict(legacy_enclosure_um_by_pair or {}).items():
        text = str(key)
        if "_" not in text:
            continue
        outer, inner = text.split("_", 1)
        numeric = float(value)
        if numeric > 0.0:
            pair_rules[(outer, inner)] = max(pair_rules.get((outer, inner), 0.0), numeric)

    candidate_pairs = tuple(
        (outer, inner, margin)
        for (outer, inner), margin in sorted(pair_rules.items())
        if outer
        and inner
        and outer not in {str(getattr(pdk.layer_map, "contact", "") or ""), *tuple(getattr(pdk.layer_map, "vias", ()) or ())}
        and inner in {str(getattr(pdk.layer_map, "active", "") or ""), str(getattr(pdk.layer_map, "gate", "") or "")}
    )
    issues: list[DrcIssue] = []
    marker_name_by_layer = {
        str(getattr(pdk.layer_map, "wells", {}).get("nwell", "NW") or "NW"): "nwell",
        str(getattr(pdk.layer_map, "implants", {}).get("nplus", "NP") or "NP"): "nplus",
        str(getattr(pdk.layer_map, "implants", {}).get("pplus", "PP") or "PP"): "pplus",
        str(getattr(pdk.layer_map, "implants", {}).get("pmetal", "PM") or "PM"): "pmetal",
    }
    for outer_layer, inner_layer, margin in candidate_pairs:
        inner_shapes = tuple(shapes_by_layer.get(str(inner_layer), ()))
        if not inner_shapes:
            continue
        outer_shapes = tuple(shapes_by_layer.get(str(outer_layer), ()))
        for inner_shape in inner_shapes:
            # Abstract fallback geometry represents the OD/PO access of a
            # native PCell, not its foundry-owned well/implant layers.  When
            # the PDK explicitly delegates that marker to the native PCell,
            # checking for a top-level cover is a false failure.  Manual/drawn
            # geometry remains subject to the exact same enclosure rule.
            marker_name = marker_name_by_layer.get(str(outer_layer))
            if (
                marker_name
                and not top_level_marker_requires_global_cover(pdk, marker_name)
                and bool(
                    getattr(inner_shape, "internal_marker_owned", False)
                    or getattr(inner_shape, "native_pcell", False)
                )
            ):
                continue
            inner_bbox = tuple(getattr(inner_shape, "bbox", ()))
            required_bbox = _expand_bbox(inner_bbox, margin)
            if any(bbox_contains(tuple(getattr(shape, "bbox", ())), required_bbox, include_touching=True) for shape in outer_shapes):
                continue
            rule_name = "ACTIVE_COVER" if str(inner_layer) == str(getattr(pdk.layer_map, "active", "") or "") else "LAYER_COVER"
            issues.append(
                DrcIssue(
                    rule_name,
                    str(outer_layer),
                    f"{outer_layer} enclosure around {inner_layer} below {margin:.4g}um",
                    required_bbox,
                )
            )
    return tuple(issues)


def _collect_via_array_issues(
    plan: Any,
    *,
    pdk: PdkConfig,
    legacy_array_spacing_um_by_layer: Mapping[str, float] | None = None,
) -> tuple[DrcIssue, ...]:
    issues: list[DrcIssue] = []
    rule_by_via = {
        str(getattr(rule, "via_def", "") or ""): rule
        for rule in tuple(getattr(pdk, "via_stack", ()) or ())
        if str(getattr(rule, "via_def", "") or "")
    }
    vias = tuple(getattr(plan, "vias", ()) or ())
    for via in vias:
        via_def = str(getattr(via, "via_def", "") or "")
        net = str(getattr(via, "net", "") or "")
        rule = rule_by_via.get(via_def)
        if rule is None:
            continue
        rows = int(getattr(via, "rows", 1) or 1)
        cols = int(getattr(via, "cols", 1) or 1)
        if rows > int(getattr(rule, "max_rows", rows) or rows) or cols > int(getattr(rule, "max_cols", cols) or cols):
            xy = tuple(float(value) for value in tuple(getattr(via, "xy", (0.0, 0.0)))[:2])
            issues.append(
                DrcIssue(
                    "VIA_ARRAY_LIMIT",
                    via_def,
                    f"{via_def} array {rows}x{cols} exceeds max {int(getattr(rule, 'max_rows', rows) or rows)}x{int(getattr(rule, 'max_cols', cols) or cols)}",
                    _via_cut_bbox(via, pdk),
                )
            )
    for idx, left in enumerate(vias):
        left_def = str(getattr(left, "via_def", "") or "")
        if not left_def:
            continue
        for right in vias[idx + 1 :]:
            right_def = str(getattr(right, "via_def", "") or "")
            if left_def != right_def:
                continue
            left_bbox = _via_cut_bbox(left, pdk)
            right_bbox = _via_cut_bbox(right, pdk)
            distance = _bbox_distance(left_bbox, right_bbox)
            min_spacing = _via_required_spacing_um(
                left_def,
                pdk,
                same_net=str(getattr(left, "net", "") or "") == str(getattr(right, "net", "") or ""),
                legacy_array_spacing_um_by_layer=legacy_array_spacing_um_by_layer,
            )
            if min_spacing <= 0.0 or distance >= min_spacing - 1e-12:
                continue
            same_net = str(getattr(left, "net", "") or "") == str(getattr(right, "net", "") or "")
            issues.append(
                DrcIssue(
                    "VIA_ARRAY_SPACING" if same_net else "VIA_SPACING",
                    left_def,
                    f"{left_def} {'array ' if same_net else ''}spacing {distance:.4g}um below {min_spacing:.4g}um",
                    _bbox_union(left_bbox, right_bbox),
                )
            )
    return tuple(_dedupe_drc_issues(issues))


def _via_cut_bbox(via: Any, pdk: PdkConfig) -> tuple[float, float, float, float]:
    via_def = str(getattr(via, "via_def", "") or "")
    try:
        cut_size = float(pdk.rules.min_width_um(via_def))
    except (AttributeError, KeyError, TypeError, ValueError):
        cut_size = max(float(getattr(pdk.rules, "grid_step_um", 0.001) or 0.001), 0.001)
    xy = tuple(float(value) for value in tuple(getattr(via, "xy", (0.0, 0.0)))[:2])
    half = cut_size * 0.5
    return (xy[0] - half, xy[1] - half, xy[0] + half, xy[1] + half)


def _via_required_spacing_um(
    via_def: str,
    pdk: PdkConfig,
    *,
    same_net: bool,
    legacy_array_spacing_um_by_layer: Mapping[str, float] | None = None,
) -> float:
    values: list[float] = []
    try:
        values.append(float(pdk.rules.min_spacing_um(via_def)))
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    if same_net:
        legacy_key = via_def
        if via_def.startswith("VIA") and via_def[3:].isdigit():
            legacy_key = via_def
        legacy_value = float(dict(legacy_array_spacing_um_by_layer or {}).get(legacy_key, 0.0) or 0.0)
        if legacy_value > 0.0:
            values.append(legacy_value)
    return max(values or [0.0])


def _collect_gate_extension_issues(
    shapes_by_net_layer: Mapping[tuple[str, str], Sequence[object]],
    extension_um_by_layer: Mapping[str, float],
) -> tuple[DrcIssue, ...]:
    extension = float(extension_um_by_layer.get("PO", 0.0) or 0.0)
    if extension <= 0.0:
        return ()
    issues: list[DrcIssue] = []
    for (net, layer), po_shapes in shapes_by_net_layer.items():
        if str(layer) != "PO":
            continue
        od_shapes = tuple(shapes_by_net_layer.get((str(net), "OD"), ()))
        if not od_shapes:
            continue
        for po_shape in po_shapes:
            po_bbox = tuple(getattr(po_shape, "bbox", ()))
            orient = _shape_long_axis_orientation(po_bbox)
            if orient == "square":
                continue
            for od_shape in od_shapes:
                od_bbox = tuple(getattr(od_shape, "bbox", ()))
                if not _gate_shape_overlaps_active(po_bbox, od_bbox):
                    continue
                if _gate_extension_ok(po_bbox, od_bbox, extension_um=extension, orient=orient):
                    continue
                issues.append(
                    DrcIssue(
                        "GATE_EXTENSION",
                        "PO",
                        f"PO extension over OD for net {net} below {extension:.4g}um",
                        _bbox_union(po_bbox, od_bbox),
                    )
                )
    return tuple(_dedupe_drc_issues(issues))


def _gate_shape_overlaps_active(
    po_bbox: tuple[float, float, float, float],
    od_bbox: tuple[float, float, float, float],
    *,
    tol_um: float = 1e-12,
) -> bool:
    x_overlap = min(po_bbox[2], od_bbox[2]) - max(po_bbox[0], od_bbox[0])
    y_overlap = min(po_bbox[3], od_bbox[3]) - max(po_bbox[1], od_bbox[1])
    return x_overlap > tol_um and y_overlap > tol_um


def _gate_extension_ok(
    po_bbox: tuple[float, float, float, float],
    od_bbox: tuple[float, float, float, float],
    *,
    extension_um: float,
    orient: str,
) -> bool:
    if orient == "v":
        lower = po_bbox[1] - od_bbox[1]
        upper = od_bbox[3] - po_bbox[3]
        return lower <= -extension_um + 1e-12 and upper <= -extension_um + 1e-12
    if orient == "h":
        left = po_bbox[0] - od_bbox[0]
        right = od_bbox[2] - po_bbox[2]
        return left <= -extension_um + 1e-12 and right <= -extension_um + 1e-12
    return True


def _coalesce_rule_check_shapes(
    shapes: Sequence[object],
    *,
    allow_l_shape_bbox_merge: bool = True,
) -> tuple[dict[str, object], ...]:
    grouped: dict[tuple[str, str], list[tuple[float, float, float, float]]] = {}
    for shape in shapes:
        layer = str(getattr(shape, "layer", "") or "")
        net = str(getattr(shape, "net", "") or "")
        bbox = tuple(getattr(shape, "bbox", ()))
        if not layer or len(bbox) != 4:
            continue
        grouped.setdefault((layer, net), []).append(tuple(float(value) for value in bbox))

    merged_shapes: list[dict[str, object]] = []
    for (layer, net), bboxes in grouped.items():
        clusters: list[tuple[float, float, float, float]] = []
        merge_all_touching = not net
        for bbox in bboxes:
            merged = bbox
            changed = True
            while changed:
                changed = False
                survivors: list[tuple[float, float, float, float]] = []
                for existing in clusters:
                    if _rule_check_bboxes_mergeable(
                        merged,
                        existing,
                        merge_all_touching=merge_all_touching,
                        allow_l_shape_bbox_merge=allow_l_shape_bbox_merge,
                    ):
                        merged = _bbox_union(merged, existing)
                        changed = True
                    else:
                        survivors.append(existing)
                clusters = survivors
            clusters.append(merged)
        merged_shapes.extend({"layer": layer, "net": net, "bbox": bbox} for bbox in clusters)
    return tuple(merged_shapes)


def _rule_check_bboxes_mergeable(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    tol_um: float = 1e-12,
    merge_all_touching: bool = False,
    allow_l_shape_bbox_merge: bool = True,
) -> bool:
    if _bbox_distance(left, right) > tol_um:
        return False
    if merge_all_touching:
        return True
    if bbox_contains(left, right, include_touching=True) or bbox_contains(right, left, include_touching=True):
        return True
    x_overlap = min(left[2], right[2]) - max(left[0], right[0])
    y_overlap = min(left[3], right[3]) - max(left[1], right[1])
    if allow_l_shape_bbox_merge and x_overlap > tol_um and y_overlap > tol_um:
        return True
    same_y_span = abs(left[1] - right[1]) <= tol_um and abs(left[3] - right[3]) <= tol_um
    same_x_span = abs(left[0] - right[0]) <= tol_um and abs(left[2] - right[2]) <= tol_um
    if not (left[2] <= right[0] or right[2] <= left[0] or left[3] <= right[1] or right[3] <= left[1]):
        return same_y_span or same_x_span
    return same_y_span or same_x_span


def _repair_issue_breakdown(
    physical_report: Mapping[str, object],
    interconnect_report: Mapping[str, object],
    *,
    width_area_issues: Sequence[DrcIssue],
    spacing_issues: Sequence[DrcIssue],
    enclosure_issues: Sequence[DrcIssue] = (),
) -> dict[str, int]:
    return {
        "shape_geometry": len(tuple(physical_report.get("shape_geometry_issues", ()) or ())),
        "path_geometry": len(tuple(physical_report.get("path_geometry_issues", ()) or ())),
        "via_geometry": len(tuple(physical_report.get("via_geometry_issues", ()) or ())),
        "shorts": len(tuple(physical_report.get("shorts", ()) or ())),
        "opens": len(tuple(physical_report.get("opens", ()) or ())),
        "width_area": len(tuple(width_area_issues)),
        "spacing": len(tuple(spacing_issues)),
        "enclosure": len(tuple(enclosure_issues)),
        "interconnect": len(tuple(interconnect_report.get("issues", ()) or ())),
    }


def _resolve_fixed_nets(plan: LayoutPlan, *, fixed_nets: Sequence[str]) -> tuple[str, ...]:
    explicit = tuple(dict.fromkeys(str(net) for net in fixed_nets if str(net)))
    if explicit:
        return explicit
    defaults: list[str] = [net for net in plan.nets if str(net).upper() in {"VDD", "VSS", "VCC", "GND"}]
    metadata = dict(getattr(plan, "metadata", {}) or {})
    top_level_pin_nets = metadata.get("top_level_pin_nets", ())
    if isinstance(top_level_pin_nets, Mapping):
        defaults.extend(str(net) for net in tuple(top_level_pin_nets.values()) if str(net))
        defaults.extend(str(net) for net in tuple(top_level_pin_nets.keys()) if str(net))
    elif isinstance(top_level_pin_nets, (tuple, list, set)):
        defaults.extend(str(net) for net in tuple(top_level_pin_nets) if str(net))
    top_level_pin_roles = metadata.get("top_level_pin_roles", {})
    if isinstance(top_level_pin_roles, Mapping):
        defaults.extend(str(net) for net in tuple(top_level_pin_roles.keys()) if str(net))
    return tuple(dict.fromkeys(defaults))


def _select_short_keep_net(net_a: str, net_b: str, *, fixed_nets: Sequence[str]) -> str:
    fixed = {str(net) for net in fixed_nets if str(net)}
    if net_a in fixed and net_b not in fixed:
        return net_a
    if net_b in fixed and net_a not in fixed:
        return net_b
    if net_a.upper() in {"VDD", "VSS", "VCC", "GND"} and net_b.upper() not in {"VDD", "VSS", "VCC", "GND"}:
        return net_a
    if net_b.upper() in {"VDD", "VSS", "VCC", "GND"} and net_a.upper() not in {"VDD", "VSS", "VCC", "GND"}:
        return net_b
    return min(str(net_a), str(net_b))


def _select_short_keep_net_for_short(
    short: object,
    *,
    plan: LayoutPlan | None = None,
    fixed_nets: Sequence[str],
    explicit_fixed_nets: Sequence[str] = (),
) -> str:
    """Choose the net to preserve when cutting a detected short.

    Default supply nets are useful preferences, but they are not always the
    right local ECO anchor.  In generated layouts many false routes are paths
    that collide with a compact access/landing rectangle.  Cutting the landing
    often leaves a virtual via landing or device access behind and does not
    clear the short.  Unless the caller explicitly fixed a net, prefer the
    non-path side for path-vs-access shorts and let the path be cut or rerouted.
    """

    net_a = str(getattr(short, "net_a", "") or "")
    net_b = str(getattr(short, "net_b", "") or "")
    explicit = {str(net) for net in explicit_fixed_nets if str(net)}
    if net_a in explicit and net_b not in explicit:
        return net_a
    if net_b in explicit and net_a not in explicit:
        return net_b

    source_a = str(getattr(short, "source_a", "") or "")
    source_b = str(getattr(short, "source_b", "") or "")
    a_is_path = _short_source_is_path(source_a)
    b_is_path = _short_source_is_path(source_b)
    a_is_landing = _short_source_is_required_landing(plan, source_a)
    b_is_landing = _short_source_is_required_landing(plan, source_b)
    if a_is_landing != b_is_landing:
        return net_a if a_is_landing else net_b

    fixed = {str(net) for net in fixed_nets if str(net)}
    if net_a in fixed and net_b not in fixed:
        return net_a
    if net_b in fixed and net_a not in fixed:
        return net_b

    if a_is_path != b_is_path:
        return net_b if a_is_path else net_a

    return _select_short_keep_net(net_a, net_b, fixed_nets=fixed_nets)


def _short_source_is_path(source: str) -> bool:
    text = str(source or "").strip().lower()
    return text.startswith("path[") or text.startswith("path_") or ".path[" in text


def _short_source_is_required_landing(plan: LayoutPlan | None, source: str) -> bool:
    if plan is None:
        return False
    text = str(source or "").strip()
    if not text.startswith("rect["):
        return False
    try:
        idx = int(text.split("[", 1)[1].split("]", 1)[0])
    except (IndexError, ValueError):
        return False
    rects = tuple(getattr(plan, "rects", ()) or ())
    if idx < 0 or idx >= len(rects):
        return False
    metadata = getattr(rects[idx], "metadata", {})
    if not isinstance(metadata, Mapping):
        return False
    kind = str(metadata.get("kind", "") or "")
    action = str(metadata.get("action", "") or "")
    reason = str(metadata.get("reason", "") or "")
    return kind == "via_landing" or action in {"grow_via_landing_or_enclosure", "add_lvs_open_via_landing"} or "landing" in reason.lower()


def _merge_repair_patch(
    base: LayoutPlan,
    patch: LayoutPlan,
    *,
    pdk: PdkConfig,
    source: str,
    action: str,
) -> LayoutPlan:
    merged = merge_layout_plans(base, patch, cell=base.cell, grid=pdk)
    return _annotate_repair_metadata(merged, source=source, action=action)


def _apply_geometry_edits_replacement(
    base: LayoutPlan,
    edits: Sequence[object],
    *,
    pdk: PdkConfig,
    source: str,
    action: str,
) -> LayoutPlan:
    edit_by_shape = {
        str(getattr(edit, "shape_id", "")): edit
        for edit in edits
        if str(getattr(edit, "shape_id", "")) and getattr(edit, "target_bbox", None) is not None
    }
    rects = []
    for rect_idx, rect in enumerate(base.rects):
        edit = next(
            (
                candidate
                for shape_id, candidate in edit_by_shape.items()
                if _layout_shape_id_index(shape_id, "rect") == rect_idx
            ),
            None,
        )
        if edit is None:
            rects.append(rect)
            continue
        rects.append(
            replace(
                rect,
                bbox=tuple(getattr(edit, "target_bbox")),
                metadata={**dict(rect.metadata), "action": getattr(edit, "action", "repair"), "source_rect": rect_idx},
            )
        )
    path_width_updates: dict[int, float] = {}
    path_target_bboxes: dict[int, list[tuple[float, float, float, float]]] = {}
    for shape_id, edit in edit_by_shape.items():
        path_idx = _layout_shape_id_index(shape_id, "path")
        if path_idx is None:
            continue
        target_bbox = tuple(getattr(edit, "target_bbox"))
        inferred_width = min(target_bbox[2] - target_bbox[0], target_bbox[3] - target_bbox[1])
        if inferred_width > 0.0:
            path_width_updates[path_idx] = max(path_width_updates.get(path_idx, 0.0), inferred_width)
            path_target_bboxes.setdefault(path_idx, []).append(target_bbox)
    paths = []
    for path_idx, path in enumerate(base.paths):
        if path_idx not in path_width_updates:
            paths.append(path)
            continue
        new_width = max(float(path.width or 0.0), float(path_width_updates[path_idx]))
        target_bbox = _bbox_union_many(tuple(path_target_bboxes.get(path_idx, ())))
        points = _path_points_for_target_bbox(path, target_bbox, new_width)
        paths.append(
            replace(
                path,
                points=points if points is not None else path.points,
                width=new_width,
                metadata={**dict(path.metadata), "action": "repair_path_geometry", "source_path": path_idx},
            )
        )
    replaced = LayoutPlan(
        base.cell,
        nets=base.nets,
        pins=base.pins,
        instances=base.instances,
        rects=tuple(rects),
        paths=tuple(paths),
        vias=base.vias,
        labels=base.labels,
        metadata=base.metadata,
    )
    return _annotate_repair_metadata(snap_layout_plan_to_grid(replaced, pdk), source=source, action=action)


def _path_points_for_target_bbox(
    path: Any,
    target_bbox: tuple[float, float, float, float],
    width: float,
) -> tuple[tuple[float, float], ...] | None:
    """Return adjusted points for a two-point path whose segment bbox should grow."""

    raw_points = tuple(tuple(float(value) for value in tuple(point)[:2]) for point in tuple(getattr(path, "points", ()) or ()))
    if len(raw_points) != 2 or len(target_bbox) != 4 or width <= 0.0:
        return None
    (x0, y0), (x1, y1) = raw_points
    half = width / 2.0
    try:
        old_width = float(getattr(path, "width", 0.0) or 0.0)
    except (TypeError, ValueError):
        old_width = 0.0
    old_half = old_width / 2.0
    if abs(x1 - x0) >= abs(y1 - y0):
        old_left = min(x0, x1) - old_half
        old_right = max(x0, x1) + old_half
        target_expands_long_axis = (
            float(target_bbox[0]) < old_left - 1e-12
            or float(target_bbox[2]) > old_right + 1e-12
        )
        if not target_expands_long_axis:
            return raw_points
        left = float(target_bbox[0]) + half
        right = float(target_bbox[2]) - half
        if right < left:
            return None
        cy = (float(target_bbox[1]) + float(target_bbox[3])) / 2.0
        return ((left, cy), (right, cy)) if x1 >= x0 else ((right, cy), (left, cy))
    old_bottom = min(y0, y1) - old_half
    old_top = max(y0, y1) + old_half
    target_expands_long_axis = (
        float(target_bbox[1]) < old_bottom - 1e-12
        or float(target_bbox[3]) > old_top + 1e-12
    )
    if not target_expands_long_axis:
        return raw_points
    bottom = float(target_bbox[1]) + half
    top = float(target_bbox[3]) - half
    if top < bottom:
        return None
    cx = (float(target_bbox[0]) + float(target_bbox[2])) / 2.0
    return ((cx, bottom), (cx, top)) if y1 >= y0 else ((cx, top), (cx, bottom))


def _layout_shape_id_index(shape_id: str, prefix: str) -> int | None:
    text = str(shape_id or "")
    legacy_prefix = f"{prefix}_"
    if text.startswith(legacy_prefix):
        parts = text.split("_")
        if len(parts) >= 2:
            try:
                return int(parts[1])
            except ValueError:
                return None
    bracket_prefix = f"{prefix}["
    if text.startswith(bracket_prefix):
        tail = text[len(bracket_prefix):]
        index_text = tail.split("]", 1)[0]
        try:
            return int(index_text)
        except ValueError:
            return None
    return None


def _annotate_repair_metadata(plan: LayoutPlan, *, source: str, action: str = "") -> LayoutPlan:
    metadata = dict(plan.metadata)
    raw_history = metadata.get("repair_history", ())
    history = tuple(raw_history) if isinstance(raw_history, (tuple, list)) else tuple()
    if action:
        history = (*history, action)
    metadata.update({"source": source, "repair_history": history})
    return replace(plan, metadata=metadata)


def _bbox_union(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _bbox_union_many(boxes: Sequence[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    materialized = tuple(boxes)
    if not materialized:
        return (0.0, 0.0, 0.0, 0.0)
    result = tuple(float(value) for value in materialized[0])
    for bbox in materialized[1:]:
        result = _bbox_union(result, tuple(float(value) for value in bbox))
    return result


def _bbox_distance(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    if dx == 0.0:
        return dy
    if dy == 0.0:
        return dx
    return (dx * dx + dy * dy) ** 0.5


def _expand_bbox(
    bbox: tuple[float, float, float, float],
    amount: float,
) -> tuple[float, float, float, float]:
    grow = max(float(amount), 0.0)
    return (bbox[0] - grow, bbox[1] - grow, bbox[2] + grow, bbox[3] + grow)


def _dedupe_drc_issues(issues: Sequence[DrcIssue]) -> tuple[DrcIssue, ...]:
    seen: set[tuple[str, str, str, tuple[float, float, float, float] | None]] = set()
    deduped: list[DrcIssue] = []
    for issue in issues:
        key = (issue.rule, issue.layer, issue.message, issue.bbox)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return tuple(deduped)


def _legacy_inline_rule_tables(pdk: PdkConfig) -> dict[str, object]:
    name = str(getattr(pdk, "name", "") or "").strip().lower()
    if not name:
        return {}
    cached = _LEGACY_INLINE_RULE_CACHE.get(name)
    if cached is not None:
        return dict(cached)
    path = Path(__file__).resolve().parents[2] / "skills" / "tech_interface" / "pdk_data" / name / "pdk.json"
    if not path.exists():
        _LEGACY_INLINE_RULE_CACHE[name] = {}
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        _LEGACY_INLINE_RULE_CACHE[name] = {}
        return {}

    min_area_um2_by_layer: dict[str, float] = {}
    min_width_um_by_layer: dict[str, float] = {}
    min_spacing_um_by_layer: dict[str, float] = {}
    array_spacing_um_by_layer: dict[str, float] = {}
    diagonal_spacing_um_by_layer: dict[str, float] = {}
    extension_um_by_layer: dict[str, float] = {}
    enclosure_um_by_pair: dict[str, float] = {}
    for rule in tuple(data.get("drc_rules", ()) or ()):
        if not isinstance(rule, Mapping):
            continue
        layer = str(rule.get("layer", "") or "")
        rule_type = str(rule.get("rule_type", "") or "").lower()
        try:
            value = float(rule.get("value", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if not layer or value <= 0.0:
            continue
        if rule_type == "width":
            min_width_um_by_layer[layer] = max(min_width_um_by_layer.get(layer, 0.0), value)
        elif rule_type == "spacing":
            min_spacing_um_by_layer[layer] = max(min_spacing_um_by_layer.get(layer, 0.0), value)
        elif rule_type == "area":
            min_area_um2_by_layer[layer] = max(min_area_um2_by_layer.get(layer, 0.0), value)
        elif rule_type == "array_spacing":
            array_spacing_um_by_layer[layer] = max(array_spacing_um_by_layer.get(layer, 0.0), value)
        elif rule_type == "diagonal_spacing":
            diagonal_spacing_um_by_layer[layer] = max(diagonal_spacing_um_by_layer.get(layer, 0.0), value)
        elif rule_type == "extension":
            extension_um_by_layer[layer] = max(extension_um_by_layer.get(layer, 0.0), value)
        elif rule_type == "enclosure":
            key = _legacy_enclosure_pair_key(rule)
            if key:
                enclosure_um_by_pair[key] = max(enclosure_um_by_pair.get(key, 0.0), value)
    result = {
        "path": str(path),
        "min_width_um_by_layer": min_width_um_by_layer,
        "min_spacing_um_by_layer": min_spacing_um_by_layer,
        "min_area_um2_by_layer": min_area_um2_by_layer,
        "array_spacing_um_by_layer": array_spacing_um_by_layer,
        "diagonal_spacing_um_by_layer": diagonal_spacing_um_by_layer,
        "extension_um_by_layer": extension_um_by_layer,
        "enclosure_um_by_pair": enclosure_um_by_pair,
    }
    _LEGACY_INLINE_RULE_CACHE[name] = dict(result)
    return dict(result)


def _legacy_enclosure_pair_key(rule: Mapping[str, object]) -> str:
    name = str(rule.get("name", "") or "").strip().lower()
    tokens = tuple(token for token in name.split("_") if token)
    if len(tokens) >= 2:
        alias = {
            "pmet": "PM",
            "nwell": "NW",
            "nplus": "NP",
            "pplus": "PP",
        }
        outer = alias.get(tokens[-2], tokens[-2].upper())
        inner = alias.get(tokens[-1], tokens[-1].upper())
        if outer and inner:
            return f"{outer}_{inner}"
    return ""


def _inline_rule_tables_from_pdk(
    pdk: PdkConfig,
    *,
    min_area_um2_by_layer: Mapping[str, float] | None = None,
) -> dict[str, object]:
    width_rules = {
        str(layer): float(pdk.rules.min_width_um(str(layer)))
        for layer in dict(getattr(pdk.rules, "min_width_nm", {}) or {})
    }
    spacing_rules = {
        str(layer): float(pdk.rules.min_spacing_um(str(layer)))
        for layer in dict(getattr(pdk.rules, "min_spacing_nm", {}) or {})
    }
    area_rules = {
        str(layer): float(value) * 1e-6
        for layer, value in dict(getattr(pdk.rules, "min_area_nm2", {}) or {}).items()
        if float(value) > 0.0
    }
    legacy = _legacy_inline_rule_tables(pdk)
    for layer, value in dict(legacy.get("min_width_um_by_layer", {}) or {}).items():
        width_rules[str(layer)] = max(float(width_rules.get(str(layer), 0.0) or 0.0), float(value))
    for layer, value in dict(legacy.get("min_spacing_um_by_layer", {}) or {}).items():
        spacing_rules[str(layer)] = max(float(spacing_rules.get(str(layer), 0.0) or 0.0), float(value))
    for layer, value in dict(legacy.get("min_area_um2_by_layer", {}) or {}).items():
        area_rules.setdefault(str(layer), float(value))
    if min_area_um2_by_layer:
        area_rules.update({
            str(layer): float(value)
            for layer, value in dict(min_area_um2_by_layer).items()
            if float(value) > 0.0
        })
    return {
        "min_width_um_by_layer": width_rules,
        "min_spacing_um_by_layer": spacing_rules,
        "min_area_um2_by_layer": area_rules,
        "legacy_rule_source": str(legacy.get("path", "")),
        "legacy_min_width_um_by_layer": dict(legacy.get("min_width_um_by_layer", {}) or {}),
        "legacy_min_spacing_um_by_layer": dict(legacy.get("min_spacing_um_by_layer", {}) or {}),
        "legacy_min_area_um2_by_layer": dict(legacy.get("min_area_um2_by_layer", {}) or {}),
        "legacy_array_spacing_um_by_layer": dict(legacy.get("array_spacing_um_by_layer", {}) or {}),
        "legacy_diagonal_spacing_um_by_layer": dict(legacy.get("diagonal_spacing_um_by_layer", {}) or {}),
        "legacy_extension_um_by_layer": dict(legacy.get("extension_um_by_layer", {}) or {}),
        "legacy_enclosure_um_by_pair": dict(legacy.get("enclosure_um_by_pair", {}) or {}),
        "deferred_cover_check_kinds": tuple(sorted(_inline_deferred_cover_check_kinds(pdk))),
    }


def _inline_deferred_cover_check_kinds(pdk: PdkConfig) -> frozenset[str]:
    metadata = dict(getattr(pdk, "metadata", {}) or {})
    inline_drc = dict(metadata.get("inline_drc", {}) or {})
    return frozenset(
        str(kind)
        for kind in tuple(inline_drc.get("defer_cover_check_kinds", ()) or ())
        if str(kind)
    )


def _layout_terminal_net_map(plan: Any, *, pdk: PdkConfig | None = None) -> dict[str, str]:
    observed: dict[str, str] = {}
    active_pdk = pdk or PdkConfig.generic()
    metadata = getattr(plan, "metadata", {}) if isinstance(getattr(plan, "metadata", {}), Mapping) else {}
    for terminal_name, net_name in dict(metadata.get("drawn_primitive_terminal_map", {}) or {}).items():
        term = str(terminal_name or "")
        net = str(net_name or "")
        if term and net:
            observed[term] = net
    for pin in getattr(plan, "pins", ()):
        pin_name = str(getattr(pin, "name", "") or "")
        pin_net = str(getattr(pin, "net", "") or pin_name)
        if not pin_net:
            continue
        terminal_name = pin_name or pin_net
        observed.setdefault(f"{terminal_name}.PIN", pin_net)
        if terminal_name == pin_net:
            observed.setdefault(f"{pin_net}.PIN", pin_net)
    grouped_instances: dict[str, list[Any]] = {}
    for instance in getattr(plan, "instances", ()):
        inst_name = _layout_instance_source_name(instance)
        if inst_name:
            grouped_instances.setdefault(inst_name, []).append(instance)
    for inst_name, instances in grouped_instances.items():
        for terminal_name, net_name in _aggregate_layout_instance_connections(instances, pdk=active_pdk).items():
            if terminal_name and net_name:
                observed[f"{inst_name}.{terminal_name}"] = net_name
    return observed


def _layout_instance_param_map(plan: Any) -> dict[str, dict[str, object]]:
    sizing: dict[str, dict[str, object]] = {}
    for instance in getattr(plan, "instances", ()):
        raw_name = str(getattr(instance, "name", "") or "")
        name = _layout_instance_source_name(instance) or raw_name
        if not name:
            continue
        params = dict(getattr(instance, "params", {}) or {})
        existing = sizing.get(name)
        if existing is None:
            sizing[name] = params
        elif existing == params:
            continue
    return sizing


def _layout_unit_parent_name(name: str) -> str:
    base, sep, suffix = str(name or "").rpartition("_u")
    if sep and suffix.isdigit():
        return base
    return ""


def _layout_instance_source_name(instance: Any) -> str:
    metadata = getattr(instance, "metadata", {}) if isinstance(getattr(instance, "metadata", {}), Mapping) else {}
    explicit = str(metadata.get("source_device_name", "") or metadata.get("source_name", "") or "")
    if explicit:
        return explicit
    raw_name = str(getattr(instance, "name", "") or "")
    parent = _layout_unit_parent_name(raw_name)
    if parent:
        return parent
    return raw_name


def _canonical_layout_device_name(name: str, pdk: PdkConfig) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    try:
        return str(pdk.resolve_pcell_logical_name(raw))
    except Exception:
        return raw


def _layout_instance_device_name(instance: Any, pdk: PdkConfig) -> str:
    metadata = getattr(instance, "metadata", {}) if isinstance(getattr(instance, "metadata", {}), Mapping) else {}
    candidates = (
        str(metadata.get("logical_device_type", "") or ""),
        str(metadata.get("logical_pcell_name", "") or ""),
        str(getattr(getattr(instance, "master", None), "cell", "") or ""),
    )
    for candidate in candidates:
        canonical = _canonical_layout_device_name(candidate, pdk)
        if canonical:
            return canonical
    return ""


def _layout_instance_swappable_terminal_groups(instance: Any, pdk: PdkConfig) -> tuple[tuple[str, ...], ...]:
    device_kind = _layout_instance_device_name(instance, pdk)
    if device_kind in {"nmos", "pmos"}:
        return (("D", "S"),)
    if device_kind in {"resistor", "capacitor"}:
        return (("PLUS", "MINUS"),)
    return ()


def _aggregate_layout_instance_connections(
    instances: Sequence[Any],
    *,
    pdk: PdkConfig,
) -> dict[str, str]:
    if not instances:
        return {}
    groups = _layout_instance_swappable_terminal_groups(instances[0], pdk)
    grouped_terms = {term for group in groups for term in group}
    aggregated: dict[str, str] = {}

    for group in groups:
        signatures = {
            tuple(sorted(str(dict(getattr(instance, "connections", {}) or {}).get(term, "") or "") for term in group))
            for instance in instances
        }
        signatures.discard(tuple("" for _ in group))
        if len(signatures) == 1:
            nets = next(iter(signatures))
            if all(net for net in nets):
                for term, net in zip(group, nets):
                    aggregated[str(term)] = str(net)
                continue
        if signatures:
            for term in group:
                aggregated[str(term)] = "<<conflict>>"

    for terminal in {
        str(term)
        for instance in instances
        for term in dict(getattr(instance, "connections", {}) or {})
        if str(term)
    } - grouped_terms:
        nets = {
            str(dict(getattr(instance, "connections", {}) or {}).get(terminal, "") or "")
            for instance in instances
            if str(dict(getattr(instance, "connections", {}) or {}).get(terminal, "") or "")
        }
        if len(nets) == 1:
            aggregated[terminal] = next(iter(nets))
        elif len(nets) > 1:
            aggregated[terminal] = "<<conflict>>"
    return aggregated


def _collect_inline_lvs_instance_issues(
    graph: Any,
    plan: Any,
    *,
    pdk: PdkConfig,
) -> tuple[str, ...]:
    graph_devices = dict(getattr(graph, "devices", {}) or {})
    plan_instances_by_source: dict[str, list[Any]] = {}
    unmapped_instances: list[str] = []
    for instance in getattr(plan, "instances", ()):
        raw_name = str(getattr(instance, "name", "") or "")
        if not raw_name:
            continue
        source_name = _layout_instance_source_name(instance)
        if source_name:
            plan_instances_by_source.setdefault(source_name, []).append(instance)
        else:
            unmapped_instances.append(raw_name)
    issues: list[str] = []

    for name in sorted(set(graph_devices) - set(plan_instances_by_source)):
        issues.append(f"layout missing device instance {name}")
    for name in sorted(set(plan_instances_by_source) - set(graph_devices)):
        issues.append(f"layout has extra instance {name} not present in source graph")
    for name in sorted(unmapped_instances):
        issues.append(f"layout has extra instance {name} not present in source graph")

    for name, device in sorted(graph_devices.items()):
        instances = tuple(plan_instances_by_source.get(name, ()))
        if not instances:
            continue
        expected_terms = {str(term) for term in tuple(getattr(device, "terminals", ()) or ()) if str(term)}
        observed_terms = {
            str(term)
            for instance in instances
            for term in dict(getattr(instance, "connections", {}) or {})
            if str(term)
        }
        missing_terms = tuple(sorted(expected_terms - observed_terms))
        extra_terms = tuple(sorted(observed_terms - expected_terms))
        if missing_terms:
            issues.append(f"layout instance {name} missing terminal connection(s): {', '.join(missing_terms)}")
        if extra_terms:
            issues.append(f"layout instance {name} has unexpected terminal connection(s): {', '.join(extra_terms)}")
        if len(instances) > 1:
            observed_term_sets = {
                tuple(sorted(str(term) for term in dict(getattr(instance, "connections", {}) or {}) if str(term)))
                for instance in instances
            }
            if len(observed_term_sets) > 1:
                issues.append(f"layout instance {name} has inconsistent unitized terminal sets across split instances")
            connection_signatures = {
                _inline_connection_signature(dict(getattr(instance, "connections", {}) or {}), instance=instance, pdk=pdk)
                for instance in instances
            }
            if len(connection_signatures) > 1:
                issues.append(f"layout instance {name} has inconsistent unitized connection patterns across split instances")
            param_signatures = {
                _inline_param_signature(dict(getattr(instance, "params", {}) or {}))
                for instance in instances
                if dict(getattr(instance, "params", {}) or {})
            }
            if len(param_signatures) > 1:
                issues.append(f"layout instance {name} has inconsistent unitized parameter sets across split instances")

        expected_kind = _canonical_layout_device_name(str(getattr(device, "model", "") or ""), pdk)
        observed_kinds = {
            _layout_instance_device_name(instance, pdk)
            for instance in instances
            if _layout_instance_device_name(instance, pdk)
        }
        if len(observed_kinds) > 1:
            issues.append(
                f"layout instance {name} has inconsistent unitized device kinds {', '.join(sorted(observed_kinds))}"
            )
        if expected_kind and any(kind != expected_kind for kind in observed_kinds):
            issues.append(
                f"layout instance {name} device kind mismatch expected {expected_kind} observed {', '.join(sorted(observed_kinds))}"
            )
    return tuple(issues)


def _collect_inline_lvs_layout_param_issues(
    plan: Any,
    *,
    pdk: PdkConfig,
) -> tuple[str, ...]:
    try:
        from analogskills.eda.netlist import (
            _bjt_param_semantics_issues,
            _mos_param_semantics_issues,
            _positive_scalar_param_issues,
        )
    except Exception:
        return ()

    issues: list[str] = []
    for instance in getattr(plan, "instances", ()):
        name = str(getattr(instance, "name", "") or "")
        if not name:
            continue
        params = dict(getattr(instance, "params", {}) or {})
        if not params:
            continue
        logical_name = _layout_instance_device_name(instance, pdk)
        if logical_name in {"nmos", "pmos"}:
            issues.extend(_mos_param_semantics_issues(name, params))
        elif logical_name == "resistor":
            issues.extend(_positive_scalar_param_issues(name, logical_name, "resistance", ("R", "r", "r_ohm"), params))
        elif logical_name == "capacitor":
            issues.extend(_positive_scalar_param_issues(name, logical_name, "capacitance", ("C", "c", "c_f"), params))
        elif logical_name == "bjt":
            issues.extend(_bjt_param_semantics_issues(name, params))
    return tuple(dict.fromkeys(issues))


def _inline_connection_signature(
    connections: Mapping[str, object],
    *,
    instance: Any | None = None,
    pdk: PdkConfig | None = None,
) -> tuple[tuple[str, object], ...]:
    swappable_groups = _layout_instance_swappable_terminal_groups(instance, pdk or PdkConfig.generic()) if instance is not None else ()
    grouped_terms = {term for group in swappable_groups for term in group}
    signature: list[tuple[str, object]] = []
    for group in swappable_groups:
        nets = tuple(sorted(str(dict(connections or {}).get(term, "") or "") for term in group))
        signature.append(("/".join(group), nets))
    signature.extend(
        sorted(
            (str(term), str(net))
            for term, net in dict(connections or {}).items()
            if str(term) and str(term) not in grouped_terms and str(net)
        )
    )
    return tuple(signature)


def _inline_param_signature(params: Mapping[str, object]) -> tuple[tuple[str, object], ...]:
    def _normalize(value: object) -> object:
        if isinstance(value, float):
            return round(value, 18)
        if isinstance(value, (str, int, bool)) or value is None:
            return value
        return repr(value)

    return tuple(
        sorted(
            (str(key), _normalize(value))
            for key, value in dict(params or {}).items()
            if str(key)
        )
    )


def _mos_param_has_dimension_key(params: Mapping[str, object], keys: tuple[str, ...]) -> bool:
    lowered = {str(key) for key in dict(params or {})}
    for key in keys:
        if key in lowered or f"{key}_um" in lowered or f"{key}_nm" in lowered:
            return True
    return False


def _mos_param_has_any_key(params: Mapping[str, object], keys: tuple[str, ...]) -> bool:
    lowered = {str(key) for key in dict(params or {})}
    return any(key in lowered for key in keys)


def _extract_mos_effective_metrics(params: Mapping[str, object]) -> dict[str, object]:
    try:
        from analogskills.eda.netlist import _dimension_m, _first_positive_integral_param
    except Exception:
        return {}

    metrics: dict[str, object] = {}
    width_present = _mos_param_has_dimension_key(params, ("W", "w", "width"))
    unit_width_present = _mos_param_has_dimension_key(params, ("wf", "Wfg", "wfg", "finger_width"))
    length_present = _mos_param_has_dimension_key(params, ("L", "l", "length"))
    nf_present = _mos_param_has_any_key(params, ("nf", "fingers"))
    mult_present = _mos_param_has_any_key(params, ("m", "M", "simM"))

    try:
        nf = _first_positive_integral_param(params, ("nf", "fingers"), 1, "nf/fingers")
        mult = _first_positive_integral_param(params, ("m", "M", "simM"), 1, "m/M/simM")
    except Exception:
        return {}

    metrics["gate_multiplier"] = nf * mult
    metrics["nf"] = nf
    metrics["m"] = mult
    metrics["explicit_nf_or_m"] = bool(nf_present or mult_present)

    if width_present or unit_width_present:
        total_width_m = None
        unit_width_m = None
        try:
            if width_present:
                total_width_m = _dimension_m(params, ("W", "w", "width"), 1e-6)
        except Exception:
            total_width_m = None
        try:
            if unit_width_present:
                unit_width_m = _dimension_m(params, ("wf", "Wfg", "wfg", "finger_width"), 1e-6)
        except Exception:
            unit_width_m = None
        if total_width_m is not None:
            metrics["width_m"] = total_width_m
            metrics["effective_width_m"] = total_width_m
            if unit_width_m is not None:
                metrics["unit_width_m"] = unit_width_m
            elif nf * mult > 0:
                metrics["unit_width_m"] = total_width_m / float(nf * mult)
        elif unit_width_m is not None:
            metrics["width_m"] = unit_width_m
            metrics["unit_width_m"] = unit_width_m
            metrics["effective_width_m"] = unit_width_m * nf * mult
    if length_present:
        try:
            length_m = _dimension_m(params, ("L", "l", "length"), 0.18e-6)
        except Exception:
            length_m = None
        if length_m is not None:
            metrics["length_m"] = length_m
    return metrics


def _layout_metric_close(expected: float, observed: float, *, rel_tol: float = 2e-3, abs_tol: float = 1e-12) -> bool:
    return abs(float(expected) - float(observed)) <= max(float(abs_tol), float(rel_tol) * max(abs(float(expected)), abs(float(observed)), 1.0e-18))


def _aggregate_unitized_mos_metrics(instances: Sequence[Any]) -> dict[str, object]:
    metrics_by_instance: list[dict[str, object]] = []
    lengths: list[float] = []
    total_gate_multiplier = 0
    total_effective_width_m = 0.0
    have_effective_width = True
    explicit_nf_or_m = False
    unit_widths: list[float] = []

    for instance in instances:
        params = dict(getattr(instance, "params", {}) or {})
        metrics = _extract_mos_effective_metrics(params)
        if not metrics:
            continue
        metrics_by_instance.append(metrics)
        explicit_nf_or_m = explicit_nf_or_m or bool(metrics.get("explicit_nf_or_m", False))
        total_gate_multiplier += int(metrics.get("gate_multiplier", 0) or 0)
        if "length_m" in metrics:
            lengths.append(float(metrics["length_m"]))
        else:
            have_effective_width = False if "effective_width_m" not in metrics else have_effective_width
        if "effective_width_m" in metrics:
            total_effective_width_m += float(metrics["effective_width_m"])
        else:
            have_effective_width = False
        if "unit_width_m" in metrics:
            unit_widths.append(float(metrics["unit_width_m"]))

    if not metrics_by_instance:
        return {}

    result: dict[str, object] = {
        "instance_count": len(metrics_by_instance),
        "total_gate_multiplier": total_gate_multiplier,
        "explicit_nf_or_m": explicit_nf_or_m,
    }
    if have_effective_width:
        result["total_effective_width_m"] = total_effective_width_m
    rounded_unit_widths = {round(value, 18) for value in unit_widths}
    if len(rounded_unit_widths) == 1 and unit_widths:
        result["unit_width_m"] = unit_widths[0]
    elif len(rounded_unit_widths) > 1:
        result["unit_width_conflict_m"] = tuple(sorted(rounded_unit_widths))
    rounded_lengths = {round(value, 18) for value in lengths}
    if len(rounded_lengths) == 1 and lengths:
        result["length_m"] = lengths[0]
    elif len(rounded_lengths) > 1:
        result["length_conflict_m"] = tuple(sorted(rounded_lengths))
    return result


def _collect_inline_lvs_parameter_alignment(
    graph: Any,
    plan: Any,
    *,
    pdk: PdkConfig,
) -> dict[str, object]:
    graph_devices = dict(getattr(graph, "devices", {}) or {})
    plan_metadata = getattr(plan, "metadata", {}) if isinstance(getattr(plan, "metadata", {}), Mapping) else {}
    effective_sizing = {
        str(name): dict(params)
        for name, params in dict(plan_metadata.get("effective_sizing", {}) or {}).items()
        if str(name) and isinstance(params, Mapping)
    }
    plan_instances_by_source: dict[str, list[Any]] = {}
    for instance in getattr(plan, "instances", ()):
        raw_name = str(getattr(instance, "name", "") or "")
        source_name = _layout_instance_source_name(instance)
        if raw_name and source_name:
            plan_instances_by_source.setdefault(source_name, []).append(instance)

    issues: list[str] = []
    comparisons: list[dict[str, object]] = []

    for source_name, instances in sorted(plan_instances_by_source.items()):
        device = graph_devices.get(source_name)
        if device is None or _canonical_layout_device_name(str(getattr(device, "model", "") or ""), pdk) not in {"nmos", "pmos"}:
            continue
        source_params = dict(effective_sizing.get(source_name) or getattr(device, "parameters", {}) or {})
        if not source_params:
            continue
        layout_metrics = _aggregate_unitized_mos_metrics(instances)
        source_metrics = _extract_mos_effective_metrics(source_params)
        if not layout_metrics or not source_metrics:
            continue
        source_has_width = isinstance(source_metrics.get("effective_width_m"), float)
        source_has_explicit_multiplier = bool(source_metrics.get("explicit_nf_or_m", False))
        if not source_has_width and not source_has_explicit_multiplier:
            continue

        comparison_issues: list[str] = []
        source_length = source_metrics.get("length_m")
        layout_length = layout_metrics.get("length_m")
        if "length_conflict_m" in layout_metrics:
            comparison_issues.append(
                f"layout instance {source_name} has inconsistent unitized channel lengths {layout_metrics['length_conflict_m']}"
            )
        elif isinstance(source_length, float) and isinstance(layout_length, float):
            if not _layout_metric_close(source_length, layout_length, rel_tol=5e-4, abs_tol=1e-12):
                comparison_issues.append(
                    f"layout instance {source_name} aggregate length mismatch expected {source_length:g} observed {layout_length:g}"
                )

        source_width = source_metrics.get("effective_width_m")
        layout_width = layout_metrics.get("total_effective_width_m")
        if isinstance(source_width, float) and isinstance(layout_width, float):
            if not _layout_metric_close(source_width, layout_width):
                comparison_issues.append(
                    f"layout instance {source_name} aggregate effective width mismatch expected {source_width:g} observed {layout_width:g}"
                )

        compare_gate_multiplier = False
        source_unit_width = source_metrics.get("unit_width_m")
        layout_unit_width = layout_metrics.get("unit_width_m")
        if isinstance(source_unit_width, float) and isinstance(layout_unit_width, float):
            compare_gate_multiplier = _layout_metric_close(source_unit_width, layout_unit_width)
        elif source_has_explicit_multiplier and (
            not isinstance(source_metrics.get("effective_width_m"), float)
            or not isinstance(layout_metrics.get("total_effective_width_m"), float)
        ):
            compare_gate_multiplier = True

        if compare_gate_multiplier and (
            bool(source_metrics.get("explicit_nf_or_m", False)) or bool(layout_metrics.get("explicit_nf_or_m", False))
        ):
            source_mult = int(source_metrics.get("gate_multiplier", 0) or 0)
            layout_mult = int(layout_metrics.get("total_gate_multiplier", 0) or 0)
            if source_mult and layout_mult and source_mult != layout_mult:
                comparison_issues.append(
                    f"layout instance {source_name} aggregate gate multiplier mismatch expected {source_mult} observed {layout_mult}"
                )

        comparisons.append(
            {
                "source_name": source_name,
                "device_kind": _canonical_layout_device_name(str(getattr(device, "model", "") or ""), pdk),
                "instance_names": tuple(sorted(str(getattr(instance, "name", "") or "") for instance in instances)),
                "source_metrics": source_metrics,
                "layout_metrics": layout_metrics,
                "issues": tuple(comparison_issues),
            }
        )
        issues.extend(comparison_issues)

    return {
        "enabled": True,
        "passed": not issues,
        "issues": tuple(dict.fromkeys(issues)),
        "comparisons": tuple(comparisons),
    }


def _collect_inline_lvs_merge_candidates(
    graph: Any,
    plan: Any,
    *,
    pdk: PdkConfig,
) -> tuple[dict[str, object], ...]:
    graph_devices = dict(getattr(graph, "devices", {}) or {})
    plan_instances_by_source: dict[str, list[Any]] = {}
    for instance in getattr(plan, "instances", ()):
        raw_name = str(getattr(instance, "name", "") or "")
        source_name = _layout_instance_source_name(instance)
        if raw_name and source_name:
            plan_instances_by_source.setdefault(source_name, []).append(instance)

    candidates: list[dict[str, object]] = []
    for source_name, instances in sorted(plan_instances_by_source.items()):
        if len(instances) < 2 or source_name not in graph_devices:
            continue
        observed_term_sets = {
            tuple(sorted(str(term) for term in dict(getattr(instance, "connections", {}) or {}) if str(term)))
            for instance in instances
        }
        connection_signatures = {
            _inline_connection_signature(dict(getattr(instance, "connections", {}) or {}), instance=instance, pdk=pdk)
            for instance in instances
        }
        param_signatures = {
            _inline_param_signature(dict(getattr(instance, "params", {}) or {}))
            for instance in instances
        }
        observed_kinds = {
            _layout_instance_device_name(instance, pdk)
            for instance in instances
            if _layout_instance_device_name(instance, pdk)
        }
        if len(observed_term_sets) != 1 or len(connection_signatures) != 1 or len(param_signatures) != 1 or len(observed_kinds) != 1:
            continue
        candidate = {
            "source_name": source_name,
            "instance_names": tuple(sorted(str(getattr(instance, "name", "") or "") for instance in instances)),
            "count": len(instances),
            "device_kind": next(iter(observed_kinds)),
            "connection_signature": next(iter(connection_signatures)),
            "param_signature": next(iter(param_signatures)),
        }
        aggregate_metrics = _aggregate_unitized_mos_metrics(instances) if candidate["device_kind"] in {"nmos", "pmos"} else {}
        if aggregate_metrics:
            candidate["aggregate_metrics"] = aggregate_metrics
        expected_kind = _canonical_layout_device_name(str(getattr(graph_devices[source_name], "model", "") or ""), pdk)
        if expected_kind:
            candidate["expected_device_kind"] = expected_kind
        candidates.append(candidate)
    return tuple(candidates)


def _build_inline_lvs_body_tap_device_plan(
    graph: Any,
    plan: Any,
    *,
    pdk: PdkConfig,
) -> Any:
    from analogskills.contracts import TerminalRef

    try:
        source_terminal_map = graph.terminal_net_map() if hasattr(graph, "terminal_net_map") else {}
    except Exception:
        source_terminal_map = {}
    plan_instances = {
        str(getattr(instance, "name", "") or ""): instance
        for instance in getattr(plan, "instances", ())
        if str(getattr(instance, "name", "") or "")
    }
    instances = []
    for name, device in sorted(dict(getattr(graph, "devices", {}) or {}).items()):
        plan_instance = plan_instances.get(name)
        connections: dict[str, str] = {}
        for terminal in tuple(getattr(device, "terminals", ()) or ()):
            net = source_terminal_map.get(TerminalRef(name, str(terminal)))
            if net is None and plan_instance is not None:
                net = dict(getattr(plan_instance, "connections", {}) or {}).get(str(terminal))
            if str(net or ""):
                connections[str(terminal)] = str(net)
        instances.append(
            SimpleNamespace(
                name=str(name),
                logical_name=_canonical_layout_device_name(str(getattr(device, "model", "") or ""), pdk),
                connections=connections,
            )
        )
    return SimpleNamespace(instances=tuple(instances))


def _collect_inline_lvs_power_semantics(
    graph: Any,
    plan: Any,
    *,
    pdk: PdkConfig,
) -> dict[str, object]:
    device_plan = _build_inline_lvs_body_tap_device_plan(graph, plan, pdk=pdk)
    report = analyze_power_plan(
        tap_plan=plan,
        pdk=pdk,
        device_plan=device_plan,
        supply_nets=(),
        require_drops=False,
        require_taps=False,
        require_body_taps=True,
    )
    support = _collect_inline_body_tap_support(plan, pdk=pdk)
    issues = list(str(issue) for issue in tuple(report.get("issues", ()) or ()))
    native_pcell_body_semantics = _collect_native_pcell_body_tap_semantics(plan, pdk=pdk)
    required_by_net = {
        str(net): tuple(str(kind) for kind in tuple(kinds or ()))
        for net, kinds in dict(report.get("body_tap_required_by_net", {}) or {}).items()
        if str(net)
    }
    available_by_net = {
        str(net): tuple(str(kind) for kind in tuple(kinds or ()))
        for net, kinds in dict(report.get("body_tap_kinds_by_net", {}) or {}).items()
        if str(net)
    }
    available_by_net = {
        net: tuple(dict.fromkeys((*tuple(kinds), *tuple(native_pcell_body_semantics.get(net, ())))))
        for net, kinds in {**available_by_net, **{net: () for net in native_pcell_body_semantics}}.items()
    }
    issues = [
        issue
        for issue in issues
        if not _body_tap_issue_satisfied_by_semantics(issue, native_pcell_body_semantics)
    ]
    for net, required_kinds in sorted(required_by_net.items()):
        available_kinds = tuple(kind for kind in available_by_net.get(net, ()) if kind in required_kinds)
        if not available_kinds:
            continue
        if any(kind in set(native_pcell_body_semantics.get(net, ())) for kind in available_kinds):
            continue
        helper_count = int(dict(support.get("helper_count_by_net", {}) or {}).get(net, 0) or 0)
        contact_count = int(dict(support.get("contact_count_by_net", {}) or {}).get(net, 0) or 0)
        if helper_count == 0 and contact_count == 0:
            issues.append(
                f"net {net} missing body tap contact/via support for {','.join(available_kinds)} tap"
            )
    well_cover_issues = _collect_inline_instance_well_cover_issues(plan, pdk=pdk)
    issues.extend(well_cover_issues)
    issues = tuple(dict.fromkeys(str(issue) for issue in issues))
    return {
        "enabled": True,
        "passed": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "body_tap_required_by_net": dict(report.get("body_tap_required_by_net", {}) or {}),
        "body_tap_kinds_by_net": available_by_net,
        "native_pcell_body_semantics": native_pcell_body_semantics,
        "body_tap_support": support,
        "well_cover_issues": tuple(well_cover_issues),
    }


def _collect_native_pcell_body_tap_semantics(
    plan: Any,
    *,
    pdk: PdkConfig,
) -> dict[str, tuple[str, ...]]:
    by_net: dict[str, list[str]] = {}
    for instance in tuple(getattr(plan, "instances", ()) or ()):
        metadata = getattr(instance, "metadata", {}) if isinstance(getattr(instance, "metadata", {}), Mapping) else {}
        if str(metadata.get("instantiation_method", "") or "") != "dbCreateParamInst":
            continue
        logical = str(metadata.get("logical_device_type", "") or metadata.get("logical_pcell_name", "") or "").lower()
        if not logical:
            logical = _layout_instance_device_name(instance, pdk)
        if not logical.startswith("pmos") and not logical.startswith("nmos"):
            continue
        connections = dict(getattr(instance, "connections", {}) or {})
        body_net = str(connections.get("B", "") or connections.get("BODY", "") or connections.get("BULK", "") or "")
        source_net = str(connections.get("S", "") or "")
        if not body_net or body_net != source_net:
            continue
        kind = "nwell" if logical.startswith("pmos") else "substrate"
        by_net.setdefault(body_net, []).append(kind)
    return {net: tuple(sorted(set(kinds))) for net, kinds in sorted(by_net.items())}


def _body_tap_issue_satisfied_by_semantics(issue: str, semantics: Mapping[str, Sequence[str]]) -> bool:
    text = str(issue)
    if " missing " not in text or " body tap" not in text:
        return False
    parts = text.split()
    if len(parts) < 2 or parts[0] != "net":
        return False
    net = parts[1]
    kinds = set(str(kind) for kind in tuple(semantics.get(net, ()) or ()))
    if not kinds:
        return False
    if "nwell body tap" in text:
        return "nwell" in kinds
    if "substrate body tap" in text:
        return "substrate" in kinds
    return False


def _collect_inline_body_tap_support(
    plan: Any,
    *,
    pdk: PdkConfig,
) -> dict[str, object]:
    helper_count_by_net: dict[str, int] = {}
    for instance in tuple(getattr(plan, "instances", ()) or ()):
        kind = _inline_tap_helper_kind(instance)
        if not kind:
            continue
        net = _inline_instance_supply_net(instance)
        if not net:
            continue
        helper_count_by_net[net] = helper_count_by_net.get(net, 0) + 1

    contact_defs = {str(getattr(pdk.layer_map, "contact", "") or "")}
    contact_count_by_net: dict[str, int] = {}
    for via in tuple(getattr(plan, "vias", ()) or ()):
        via_def = str(getattr(via, "via_def", "") or "")
        if via_def not in contact_defs:
            continue
        net = str(getattr(via, "net", "") or "")
        if not net:
            continue
        contact_count_by_net[net] = contact_count_by_net.get(net, 0) + 1
    return {
        "helper_count_by_net": dict(sorted(helper_count_by_net.items())),
        "contact_count_by_net": dict(sorted(contact_count_by_net.items())),
    }


def _inline_instance_supply_net(instance: Any) -> str:
    connections = getattr(instance, "connections", None)
    if isinstance(connections, Mapping):
        for key in ("B", "BODY", "BULK", "S", "D", "net", "NET"):
            net = str(connections.get(key, "") or "")
            if net:
                return net
    params = getattr(instance, "params", None)
    if isinstance(params, Mapping):
        for key in ("net", "NET"):
            net = str(params.get(key, "") or "")
            if net:
                return net
    return ""


def _inline_tap_helper_kind(instance: Any) -> str:
    metadata = getattr(instance, "metadata", None)
    if isinstance(metadata, Mapping):
        for key in ("tap_kind", "helper_kind", "intent_kind"):
            value = str(metadata.get(key, "") or "")
            if value in {"nwell", "substrate"}:
                return value
    master = getattr(instance, "master", None)
    cell = str(getattr(master, "cell", "") or getattr(instance, "cell", "") or "").upper()
    if cell == "M0_NW":
        return "nwell"
    if cell == "M0_SUB":
        return "substrate"
    return ""


def _collect_inline_instance_well_cover_issues(
    plan: Any,
    *,
    pdk: PdkConfig,
) -> tuple[str, ...]:
    nwell_layer = str(getattr(pdk.layer_map, "wells", {}).get("nwell", "NW") or "NW")
    if not nwell_layer:
        return ()
    nwell_shapes = tuple(
        tuple(getattr(rect, "bbox", ()))
        for rect in tuple(getattr(plan, "rects", ()) or ())
        if str(getattr(rect, "layer", "") or "") == nwell_layer
    )
    requires_top_level_nwell = top_level_marker_requires_global_cover(pdk, "nwell")

    def should_check_instance(instance: Any) -> bool:
        if _layout_instance_device_name(instance, pdk) != "pmos":
            return False
        return requires_top_level_nwell or not _layout_instance_owns_internal_marker_rules(instance)

    if not nwell_shapes:
        pmos_instances = tuple(
            inst for inst in tuple(getattr(plan, "instances", ()) or ())
            if should_check_instance(inst)
        )
        if not pmos_instances:
            return ()
        return tuple(f"layout missing {nwell_layer} cover for PMOS instance {str(getattr(inst, 'name', '') or '<unnamed>')}" for inst in pmos_instances)

    issues: list[str] = []
    for instance, active_bbox in _inline_instance_active_bboxes(plan, pdk=pdk):
        if not should_check_instance(instance):
            continue
        if not any(bbox_contains(tuple(bbox), active_bbox, include_touching=True) for bbox in nwell_shapes):
            issues.append(
                f"PMOS instance {str(getattr(instance, 'name', '') or '<unnamed>')} active region missing {nwell_layer} cover"
            )
    return tuple(issues)


def _inline_instance_active_bboxes(
    plan: Any,
    *,
    pdk: PdkConfig,
) -> tuple[tuple[Any, tuple[float, float, float, float]], ...]:
    active_layer = str(getattr(pdk.layer_map, "active", "") or "")
    result: list[tuple[Any, tuple[float, float, float, float]]] = []
    for instance in tuple(getattr(plan, "instances", ()) or ()):
        if _layout_instance_device_name(instance, pdk) != "pmos":
            continue
        for shape in _inline_fallback_shapes_for_layout_instance(instance, pdk=pdk):
            if str(getattr(shape, "layer", "") or "") != active_layer:
                continue
            bbox = tuple(float(value) for value in tuple(getattr(shape, "bbox", (0.0, 0.0, 0.0, 0.0)))[:4])
            if len(bbox) == 4 and bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                result.append((instance, bbox))
                break
    return tuple(result)


def _inline_fallback_shapes_for_layout_instance(
    instance: Any,
    *,
    pdk: PdkConfig,
) -> tuple[Any, ...]:
    try:
        from analogskills.contracts import Device, DeviceRole
        from analogskills.pcell import PCellInstancePlan, estimate_pcell_bbox_um, fallback_shapes_for_instance
    except Exception:
        return ()

    logical_name = _layout_instance_device_name(instance, pdk)
    if logical_name not in {"pmos", "nmos", "bjt", "resistor", "capacitor"}:
        return ()
    params = dict(getattr(instance, "params", {}) or {})
    metadata = dict(getattr(instance, "metadata", {}) or {}) if isinstance(getattr(instance, "metadata", {}), Mapping) else {}
    width_um = float(metadata.get("width_um", params.get("width_um", params.get("w_um", 0.0))) or 0.0)
    height_um = float(metadata.get("height_um", params.get("height_um", params.get("h_um", 0.0))) or 0.0)
    if width_um <= 0.0 or height_um <= 0.0:
        role = DeviceRole.PASSIVE
        if logical_name in {"pmos", "nmos"}:
            role = DeviceRole.BIAS
        elif logical_name == "bjt":
            role = DeviceRole.BIPOLAR
        elif logical_name == "resistor":
            role = DeviceRole.COMP_RESISTOR
        elif logical_name == "capacitor":
            role = DeviceRole.COMP_CAPACITOR
        device = Device(
            name=str(getattr(instance, "name", "") or ""),
            role=role,
            model=logical_name,
            terminals=tuple(str(term) for term in dict(getattr(instance, "connections", {}) or {})),
        )
        try:
            width_um, height_um = estimate_pcell_bbox_um(device, params)
        except Exception:
            return ()
    fallback_instance = PCellInstancePlan(
        name=str(getattr(instance, "name", "") or ""),
        logical_name=logical_name,
        lib_name=str(getattr(getattr(instance, "master", None), "lib", "") or ""),
        cell_name=str(getattr(getattr(instance, "master", None), "cell", "") or ""),
        view_name=str(getattr(getattr(instance, "master", None), "view", "layout") or "layout"),
        params=params,
        xy_um=tuple(float(value) for value in tuple(getattr(instance, "xy", (0.0, 0.0)))[:2]),
        orient=str(getattr(instance, "orient", "R0") or "R0"),
        connections=dict(getattr(instance, "connections", {}) or {}),
        width_um=max(width_um, 0.2),
        height_um=max(height_um, 0.2),
    )
    try:
        return tuple(fallback_shapes_for_instance(fallback_instance, pdk, snap_to_grid=True))
    except Exception:
        return ()


def _build_inline_drc_contract(
    plan: Any,
    *,
    pdk: PdkConfig,
    min_area_um2_by_layer: Mapping[str, float] | None = None,
) -> dict[str, object]:
    rules = _inline_rule_tables_from_pdk(pdk, min_area_um2_by_layer=min_area_um2_by_layer)
    physical_report = analyze_plan_physical_connectivity(
        plan,
        pdk=pdk,
        include_via_landing_shorts=True,
    )
    via_landing_report = analyze_via_landings(plan, pdk, require_all_layers=True)
    width_area_issues, spacing_issues, enclosure_issues = _collect_rule_driven_drc_issues(
        plan,
        pdk=pdk,
        min_width_um_by_layer=dict(rules["min_width_um_by_layer"]),
        min_area_um2_by_layer=dict(rules["min_area_um2_by_layer"]),
        min_spacing_um_by_layer=dict(rules["min_spacing_um_by_layer"]),
        array_spacing_um_by_layer=dict(rules.get("legacy_array_spacing_um_by_layer", {}) or {}),
        diagonal_spacing_um_by_layer=dict(rules.get("legacy_diagonal_spacing_um_by_layer", {}) or {}),
        extension_um_by_layer=dict(rules.get("legacy_extension_um_by_layer", {}) or {}),
    )
    issues = tuple(
        dict.fromkeys(
            (
                *(str(issue) for issue in tuple(physical_report.get("issues", ()) or ())),
                *(issue.message for issue in width_area_issues),
                *(issue.message for issue in spacing_issues),
                *(issue.message for issue in enclosure_issues),
                *(str(issue) for issue in tuple(via_landing_report.get("issues", ()) or ())),
            )
        )
    )
    return {
        "enabled": True,
        "passed": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "physical_connectivity": physical_report,
        "via_landings": via_landing_report,
        "width_area_issues": tuple(issue.message for issue in width_area_issues),
        "spacing_issues": tuple(issue.message for issue in spacing_issues),
        "enclosure_issues": tuple(issue.message for issue in enclosure_issues),
        "rule_tables": rules,
    }


def _build_inline_lvs_contract(
    graph: Any | None,
    plan: Any,
    *,
    pdk: PdkConfig,
    require_lvs_labels: bool = False,
) -> dict[str, object]:
    if graph is None:
        return {"enabled": False, "passed": True, "issue_count": 0, "issues": (), "reason": "no_graph"}
    if not getattr(plan, "instances", ()) and not getattr(plan, "pins", ()):
        return {"enabled": False, "passed": True, "issue_count": 0, "issues": (), "reason": "no_layout_terminals"}

    from analogskills.contracts import TerminalRef
    from analogskills.eda import analyze_lvs_pin_label_stamping, analyze_lvs_source_precheck, compare_topology_terminal_map

    observed_terminal_map = _layout_terminal_net_map(plan, pdk=pdk)
    try:
        terminal_mismatches = compare_topology_terminal_map(graph, observed_terminal_map)
    except Exception as exc:
        terminal_mismatches = {"<topology>": (None, f"terminal map extraction failed: {exc}")}
    source_precheck = analyze_lvs_source_precheck(
        graph,
        _layout_instance_param_map(plan),
        layout_plan=plan,
        require_model_map=False,
    )
    instance_issues = _collect_inline_lvs_instance_issues(graph, plan, pdk=pdk)
    layout_param_issues = _collect_inline_lvs_layout_param_issues(plan, pdk=pdk)
    merge_candidates = _collect_inline_lvs_merge_candidates(graph, plan, pdk=pdk)
    parameter_alignment = _collect_inline_lvs_parameter_alignment(graph, plan, pdk=pdk)
    power_semantics = _collect_inline_lvs_power_semantics(graph, plan, pdk=pdk)
    opens = detect_plan_net_opens(
        plan,
        pdk=pdk,
        include_instance_terminals=True,
    )
    shorts = detect_plan_shape_shorts(
        plan,
        pdk=pdk,
        include_via_landings=True,
        include_instance_terminals=True,
    )
    top_level_nets = tuple(str(pin) for pin in tuple(getattr(graph, "pins", ()) or ()))
    terminal_map = graph.terminal_net_map() if hasattr(graph, "terminal_net_map") else {}
    pin_net_aliases = {
        pin_name: str(terminal_map.get(TerminalRef(pin_name, "PIN"), pin_name))
        for pin_name in top_level_nets
    }
    pin_label_report = analyze_lvs_pin_label_stamping(
        plan,
        top_level_nets=top_level_nets,
        pdk=pdk,
        require_explicit_labels=require_lvs_labels,
        pin_net_aliases=pin_net_aliases,
    )
    mismatch_issues = tuple(
        f"terminal {term} expected {expected or '<missing>'} observed {observed or '<missing>'}"
        for term, (expected, observed) in sorted(terminal_mismatches.items())
    )
    open_issues = tuple(
        f"net {item.net} has {item.component_count} disconnected geometry components"
        for item in opens
    )
    short_issues = tuple(
        f"short {item.net_a}-{item.net_b} on {item.layer}"
        for item in shorts
    )
    pin_issues = tuple(str(issue) for issue in tuple(pin_label_report.get("issues", ()) or ()))
    source_issues = tuple(str(issue) for issue in tuple(getattr(source_precheck, "issues", ()) or ()))
    power_issues = tuple(str(issue) for issue in tuple(power_semantics.get("issues", ()) or ()))
    issues = tuple(
        dict.fromkeys(
            (
                *mismatch_issues,
                *instance_issues,
                *layout_param_issues,
                *(str(issue) for issue in tuple(parameter_alignment.get("issues", ()) or ())),
                *source_issues,
                *power_issues,
                *open_issues,
                *short_issues,
                *pin_issues,
            )
        )
    )
    return {
        "enabled": True,
        "passed": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "instance_issues": instance_issues,
        "layout_param_issues": layout_param_issues,
        "merge_candidates": merge_candidates,
        "parameter_alignment": parameter_alignment,
        "power_semantics": power_semantics,
        "source_precheck": source_precheck.to_dict(),
        "terminal_mismatches": tuple(
            {"terminal": term, "expected": expected, "observed": observed}
            for term, (expected, observed) in sorted(terminal_mismatches.items())
        ),
        "opens": tuple(
            {
                "net": item.net,
                "component_count": int(item.component_count),
                "shape_count": int(item.shape_count),
                "layers": tuple(item.layers),
                "sources": tuple(item.sources),
            }
            for item in opens
        ),
        "shorts": tuple(
            {
                "layer": item.layer,
                "net_a": item.net_a,
                "net_b": item.net_b,
                "source_a": item.source_a,
                "source_b": item.source_b,
            }
            for item in shorts
        ),
        "pin_label_report": pin_label_report,
    }


def _build_inline_verification_contract(
    plan: Any,
    *,
    graph: Any | None,
    pdk: PdkConfig,
    min_area_um2_by_layer: Mapping[str, float] | None = None,
    require_lvs_labels: bool = False,
) -> dict[str, object]:
    inline_drc = _build_inline_drc_contract(
        plan,
        pdk=pdk,
        min_area_um2_by_layer=min_area_um2_by_layer,
    )
    inline_lvs = _build_inline_lvs_contract(
        graph,
        plan,
        pdk=pdk,
        require_lvs_labels=require_lvs_labels,
    )
    passed = bool(inline_drc.get("passed", False)) and (
        not bool(inline_lvs.get("enabled", False)) or bool(inline_lvs.get("passed", False))
    )
    issues = tuple(
        dict.fromkeys(
            (
                *(str(issue) for issue in tuple(inline_drc.get("issues", ()) or ())),
                *(str(issue) for issue in tuple(inline_lvs.get("issues", ()) or ())),
            )
        )
    )
    return {
        "enabled": True,
        "passed": passed,
        "issue_count": len(issues),
        "issues": issues,
        "inline_drc": inline_drc,
        "inline_lvs": inline_lvs,
    }


def plan_pex_hotspot_layout_ir(
    route_plan: Any,
    hotspot_evidence: Any | Iterable[Any],
    pdk: PdkConfig | None = None,
    *,
    lib: str = "work",
    cell: str = "pex_route_eco",
    view: str = "layout",
    width_multiplier: float = 1.5,
    min_width_um: float | None = None,
    allowed_nets: Sequence[str] = (),
    blocked_nets: Sequence[str] = (),
    scope_policy: str = "advisory_only",
    system_contract: Mapping[str, object] | None = None,
    hierarchy_lowering: Mapping[str, object] | None = None,
    hierarchy_parasitics: Mapping[str, object] | None = None,
    hierarchy_binding: Mapping[str, object] | None = None,
) -> LayoutPlan:
    """Map PEX hotspot evidence to a reviewable LayoutIR route-width proposal."""

    from .ir import LayoutPath, snap_layout_plan_to_grid

    pdk = pdk or PdkConfig.generic()
    if width_multiplier <= 0:
        raise ValueError("width_multiplier must be positive")
    hotspot_nets = _hotspot_nets(hotspot_evidence)
    allowed = {str(net) for net in allowed_nets if str(net)}
    blocked = {str(net) for net in blocked_nets if str(net)}
    normalized_system = dict(system_contract or {})
    normalized_lowering = dict(hierarchy_lowering or {})
    normalized_parasitics = dict(hierarchy_parasitics or {})
    normalized_binding = dict(hierarchy_binding or {})
    bus_contracts = tuple(dict(item) for item in normalized_system.get("bus_contracts", ()) if isinstance(item, Mapping))
    feedback_contracts = tuple(dict(item) for item in normalized_system.get("feedback_contracts", ()) if isinstance(item, Mapping))
    reference_paths = tuple(dict(item) for item in normalized_system.get("reference_paths", ()) if isinstance(item, Mapping))
    parasitic_partitions = tuple(dict(item) for item in normalized_parasitics.get("partitions", ()) if isinstance(item, Mapping))
    anchor_nets = tuple(str(net) for net in tuple(normalized_lowering.get("routing_anchor_nets", ())) if str(net))
    restore_bus_nets = tuple(
        dict.fromkeys(
            str(net)
            for item in bus_contracts
            if bool(item.get("restore_required", False))
            for net in tuple(item.get("nets", ()))
            if str(net)
        )
    )
    restore_feedback_nets = tuple(
        dict.fromkeys(str(item.get("net", "")) for item in feedback_contracts if bool(item.get("restore_required", False)) and str(item.get("net", "")))
    )
    protected_reference_nets = tuple(
        dict.fromkeys(str(item.get("net", "")) for item in reference_paths if bool(item.get("preserve_integrity", False)) and str(item.get("net", "")))
    )
    architecture_protected_nets = tuple(
        dict.fromkeys(
            str(net)
            for partition in parasitic_partitions
            if str(
                dict(partition.get("architecture_budget", {}) or {}).get(
                    "sensitivity",
                    dict(partition.get("architecture_budget", {}) or {}).get("sensitivity_class", ""),
                )
                or ""
            ) in {"reference_critical", "timing_critical", "feedback_critical"}
            for net in (
                tuple(partition.get("critical_nets", ()) or ())
                + tuple(partition.get("reference_nets", ()) or ())
                + tuple(partition.get("feedback_nets", ()) or ())
                + tuple(partition.get("routing_anchor_nets", ()) or ())
            )
            if str(net) and str(net) not in hotspot_nets
        )
    )
    anchor_protected_nets = tuple(net for net in anchor_nets if net not in hotspot_nets)
    effective_blocked = {str(net) for net in (*blocked, *anchor_protected_nets, *architecture_protected_nets) if str(net)}
    paths = []
    for path in tuple(getattr(route_plan, "paths", ())):
        net = str(getattr(path, "net", ""))
        if net not in hotspot_nets:
            continue
        if allowed:
            if net not in allowed:
                continue
        elif scope_policy != "advisory_only" and hotspot_nets:
            continue
        if net in effective_blocked:
            continue
        layer = str(getattr(path, "layer", ""))
        base_width = float(getattr(path, "width", 0.0) or 0.0)
        floor_width = min_width_um if min_width_um is not None else _min_route_width_um(pdk, layer, base_width)
        target_width = max(base_width * width_multiplier, floor_width)
        paths.append(
            LayoutPath(
                layer,
                tuple(getattr(path, "points", ())),
                pdk.rules.snap_dimension_um(target_width),
                net,
                str(getattr(path, "purpose", "drawing")),
                metadata={
                    "source": "pex_hotspot",
                    "action": "widen_or_shorten_resistive_route",
                    "original_width_um": base_width,
                    "scope_policy": scope_policy,
                    "scope_allowed_nets": tuple(sorted(allowed)),
                    "scope_blocked_nets": tuple(sorted(effective_blocked)),
                    "restore_bus_nets": restore_bus_nets,
                    "restore_feedback_nets": restore_feedback_nets,
                    "protected_reference_nets": protected_reference_nets,
                    "anchor_protected_nets": anchor_protected_nets,
                    "architecture_protected_nets": architecture_protected_nets,
                },
            )
        )
    proposal = LayoutPlan(
        LayoutCellRef(lib, cell, view, "maskLayout"),
        nets=tuple(dict.fromkeys(path.net for path in paths if path.net)),
        paths=tuple(paths),
        metadata={
            "source": "plan_pex_hotspot_layout_ir",
            "hotspot_nets": tuple(sorted(hotspot_nets)),
            "scope_policy": scope_policy,
            "allowed_nets": tuple(sorted(allowed)),
            "blocked_nets": tuple(sorted(effective_blocked)),
            "restore_bus_nets": restore_bus_nets,
            "restore_feedback_nets": restore_feedback_nets,
            "protected_reference_nets": protected_reference_nets,
            "anchor_protected_nets": anchor_protected_nets,
            "architecture_protected_nets": architecture_protected_nets,
            "binding_ready_partitions": tuple(
                str(name) for name in tuple(normalized_binding.get("pcell_binding_partitions", ()) or ()) if str(name)
            ),
            "macro_bound_partitions": tuple(
                str(name) for name in tuple(normalized_binding.get("macro_binding_partitions", ()) or ()) if str(name)
            ),
            "binding_blocked_partitions": tuple(
                str(name) for name in tuple(normalized_binding.get("blocked_partitions", ()) or ()) if str(name)
            ),
        },
    )
    return snap_layout_plan_to_grid(proposal, pdk)


def rank_and_select_interconnect_candidate(
    candidates: Sequence[Any],
    constraints: LayoutConstraintSet | None = None,
    pdk: PdkConfig | None = None,
    *,
    strict_include_via_landing_short_checks: bool = False,
    fallback_to_relaxed: bool = True,
    require_inline_verification: bool = False,
    inline_verification_graph: Any | None = None,
    require_lvs_labels: bool = False,
    **rank_kwargs: Any,
) -> tuple[InterconnectCandidate, tuple[InterconnectCandidate, ...]]:
    """Rank interconnect candidates and optionally retry without strict via short checks.

    Returns the selected candidate and the ranked candidate list used for the decision.
    """

    pdk = pdk or PdkConfig.generic()
    ranked = rank_interconnect_candidates(
        candidates,
        constraints,
        pdk,
        include_via_landing_short_checks=strict_include_via_landing_short_checks,
        **rank_kwargs,
    )
    if require_inline_verification:
        min_area_rules = (
            dict(rank_kwargs.get("route_min_area_um2_by_layer", {}) or {})
            if bool(rank_kwargs.get("require_min_area_checks", False))
            else None
        )
        ranked = tuple(
            sorted(
                ranked,
                key=lambda row: (
                    _interconnect_inline_issue_count(
                        row,
                        graph=inline_verification_graph,
                        pdk=pdk,
                        min_area_um2_by_layer=min_area_rules,
                        require_lvs_labels=require_lvs_labels,
                    ),
                    _interconnect_strict_short_count(row) if strict_include_via_landing_short_checks else 0.0,
                    float(row.score),
                    len(tuple(row.issues)),
                ),
            )
        )
    if strict_include_via_landing_short_checks:
        ranked = tuple(
            sorted(
                ranked,
                key=lambda row: (
                    _interconnect_inline_issue_count(
                        row,
                        graph=inline_verification_graph,
                        pdk=pdk,
                        min_area_um2_by_layer=(
                            dict(rank_kwargs.get("route_min_area_um2_by_layer", {}) or {})
                            if require_inline_verification and bool(rank_kwargs.get("require_min_area_checks", False))
                            else None
                        ),
                        require_lvs_labels=require_lvs_labels,
                    )
                    if require_inline_verification
                    else 0,
                    _interconnect_strict_short_count(row),
                    float(row.score),
                    len(tuple(row.issues)),
                ),
            )
        )
    if not ranked:
        raise ValueError("no interconnect candidates were provided")
    selected = ranked[0]
    if strict_include_via_landing_short_checks and fallback_to_relaxed and selected.issues:
        relaxed = rank_interconnect_candidates(candidates, constraints, pdk, include_via_landing_short_checks=False, **rank_kwargs)
        if relaxed and relaxed[0].score < selected.score:
            ranked = relaxed
            selected = relaxed[0]
    return selected, ranked


def _interconnect_inline_issue_count(
    candidate: InterconnectCandidate,
    *,
    graph: Any | None,
    pdk: PdkConfig,
    min_area_um2_by_layer: Mapping[str, float] | None,
    require_lvs_labels: bool,
) -> int:
    verification = _build_inline_verification_contract(
        candidate.plan,
        graph=graph,
        pdk=pdk,
        min_area_um2_by_layer=min_area_um2_by_layer,
        require_lvs_labels=require_lvs_labels,
    )
    return int(verification.get("issue_count", 0) or 0)


def _interconnect_strict_short_count(candidate: InterconnectCandidate) -> float:
    costs = dict(getattr(candidate, "costs", {}) or {})
    strict_cost = float(costs.get("via_landing_short_risk", 0.0) or 0.0) + float(costs.get("short_risk", 0.0) or 0.0)
    if strict_cost:
        return strict_cost
    return float(
        sum(
            1
            for issue in tuple(getattr(candidate, "issues", ()) or ())
            if "short risk" in str(issue) or str(issue).startswith("short ")
        )
    )


def build_physical_implementation_contract(
    placement: Sequence[Any],
    interconnect_plan: Any,
    *,
    constraints: LayoutConstraintSet | None = None,
    graph: Any | None = None,
    pdk: PdkConfig | None = None,
    system_contract: Mapping[str, object] | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
    routing_corridors: Sequence[Any] = (),
    include_open_checks: bool = False,
    include_via_landing_short_checks: bool = False,
    require_antenna_checks: bool = False,
    antenna_max_metal_length_um: float = 20.0,
    antenna_max_length_per_via_um: float = 10.0,
    require_min_area_checks: bool = False,
    route_min_area_um2_by_layer: Mapping[str, float] | None = None,
    require_inline_verification: bool = False,
    require_lvs_labels: bool = False,
) -> dict[str, object]:
    """Build a serializable physical implementation contract.

    This is a read-only summary layer over existing placement/routing/PDK
    analyzers so higher layers can reason about implementation readiness
    without depending on analyzer-specific return payloads.
    """

    from .placement import analyze_placement
    from .routing import analyze_interconnect_plan

    pdk = pdk or PdkConfig.generic()
    active_constraints = constraints or getattr(graph, "layout_constraints", None) or LayoutConstraintSet()
    placement_report = analyze_placement(tuple(placement), active_constraints, pdk=pdk, graph=graph)
    routing_report = analyze_interconnect_plan(
        interconnect_plan,
        active_constraints,
        pdk,
        routing_corridors=routing_corridors,
        include_open_checks=include_open_checks,
        include_via_landing_short_checks=include_via_landing_short_checks,
        require_antenna_checks=require_antenna_checks,
        antenna_max_metal_length_um=antenna_max_metal_length_um,
        antenna_max_length_per_via_um=antenna_max_length_per_via_um,
        require_min_area_checks=require_min_area_checks,
        route_min_area_um2_by_layer=route_min_area_um2_by_layer,
    )
    pdk_issues = tuple(str(issue) for issue in pdk.validate())
    inline_verification = _build_inline_verification_contract(
        interconnect_plan,
        graph=graph,
        pdk=pdk,
        min_area_um2_by_layer=route_min_area_um2_by_layer,
        require_lvs_labels=require_lvs_labels,
    )
    readiness = {
        "pdk_valid": not pdk_issues,
        "placement_legal": bool(placement_report.get("passed", False)),
        "routing_legal": bool(routing_report.get("passed", False)),
    }
    placement_contract = _placement_contract(tuple(placement), placement_report, active_constraints)
    readiness["ready_for_extraction"] = bool(
        readiness["pdk_valid"] and readiness["placement_legal"] and readiness["routing_legal"] and bool(pdk.extraction_corners)
    )
    if require_inline_verification and not bool(inline_verification.get("passed", False)):
        readiness["ready_for_extraction"] = False

    return {
        "pdk": _pdk_physical_contract(pdk),
        "placement": placement_contract,
        "routing": _routing_contract(routing_report),
        "verification": inline_verification,
        "system": _system_contract_summary(system_contract),
        "hierarchy_lowering": _physical_hierarchy_lowering_contract(hierarchy_context),
        "readiness": readiness,
        "issues": {
            "pdk": pdk_issues,
            "placement": tuple(str(issue) for issue in placement_report.get("issues", ())),
            "routing": tuple(str(issue) for issue in routing_report.get("issues", ())),
            "verification": tuple(str(issue) for issue in tuple(inline_verification.get("issues", ()) or ())),
        },
    }


def legalize_physical_implementation(
    graph: Any,
    *,
    sizing: Mapping[str, Mapping[str, object]] | None = None,
    placement_candidates: Sequence[Sequence[Any]] = (),
    route_candidates: Sequence[Any] = (),
    placements: Sequence[Any] | None = None,
    layout_plan: Any | None = None,
    constraints: LayoutConstraintSet | None = None,
    pdk: PdkConfig | None = None,
    system_contract: Mapping[str, object] | None = None,
    rebuild_layout: bool = False,
    max_iterations: int = 3,
    routing_corridors: Sequence[Any] = (),
    include_open_checks: bool = False,
    include_via_landing_short_checks: bool = False,
    require_antenna_checks: bool = False,
    antenna_max_metal_length_um: float = 20.0,
    antenna_max_length_per_via_um: float = 10.0,
    require_min_area_checks: bool = False,
    route_min_area_um2_by_layer: Mapping[str, float] | None = None,
    require_inline_verification: bool = False,
    require_lvs_labels: bool = False,
    plan_device_layout_kwargs: Mapping[str, object] | None = None,
    placement_strategy: "AnalogPlacementStrategy | None" = None,
    routing_strategy: "AnalogRoutingStrategy | None" = None,
    placement_seed_metadata: Mapping[str, object] | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Run a thin legalization loop over placement/routing candidates.

    This tool does not orchestrate broader flows. It only evaluates a bounded
    set of candidate physical states and returns the best legalizable result
    plus a transparent iteration trace.
    """

    from .placement import generate_placement, rank_placement_candidates

    pdk = pdk or PdkConfig.generic()
    active_constraints = constraints or getattr(graph, "layout_constraints", None) or LayoutConstraintSet()
    layout_kwargs = dict(plan_device_layout_kwargs or {})
    explicit_design_context = layout_kwargs.get("design_context")
    explicit_solver_guide = layout_kwargs.get("solver_guide")
    respect_solver_iteration_policy = bool(layout_kwargs.pop("respect_solver_iteration_policy", False))
    effective_solver_guide = explicit_solver_guide
    if effective_solver_guide is None and explicit_design_context is not None:
        effective_solver_guide = getattr(explicit_design_context, "guide", None)
        if effective_solver_guide is None and isinstance(explicit_design_context, Mapping):
            effective_solver_guide = explicit_design_context.get("guide")
    candidate_routing_strategy = layout_kwargs.pop("routing_strategy", None)
    large_dirty_inline_issue_threshold = int(layout_kwargs.pop("large_dirty_inline_issue_threshold", 50) or 0)
    if routing_strategy is None:
        if candidate_routing_strategy is not None:
            routing_strategy = candidate_routing_strategy
    if max_iterations < 1:
        raise ValueError("max_iterations must be positive")
    if rebuild_layout and sizing is None:
        raise ValueError("sizing is required when rebuild_layout=True")
    effective_max_iterations = max_iterations
    if respect_solver_iteration_policy:
        effective_max_iterations = max(max_iterations, _solver_guide_max_rounds(effective_solver_guide))
    solver_guide_agent_contract = _solver_guide_agent_contract_payload(effective_solver_guide)

    base_candidates: list[tuple[Any, ...]] = [tuple(candidate) for candidate in placement_candidates if tuple(candidate)]
    if placements is not None:
        normalized = tuple(placements)
        if normalized:
            base_candidates.insert(0, normalized)
    if not base_candidates:
        base_candidates.append(tuple(generate_placement(graph, active_constraints, pdk)))

    effective_seed_metadata = _enrich_seed_metadata_from_hierarchy(placement_seed_metadata, hierarchy_context)
    if "anchor_spread_target_um" not in effective_seed_metadata:
        effective_seed_metadata["anchor_spread_target_um"] = float(
            getattr(getattr(pdk, "analog_placement_constraints", None), "anchor_spread_target_um", 0.0) or 0.0
        )
    if "focus_separation_target_um" not in effective_seed_metadata:
        effective_seed_metadata["focus_separation_target_um"] = float(
            getattr(getattr(pdk, "analog_placement_constraints", None), "focus_separation_target_um", 0.0) or 0.0
        )
    ranked_placements = rank_placement_candidates(
        tuple(base_candidates),
        active_constraints,
        graph=graph,
        pdk=pdk,
        placement_seed_metadata=effective_seed_metadata,
    )
    selected_placements = ranked_placements[0].placements
    selected_layout = layout_plan
    baseline_placements = tuple(placements) if placements is not None else tuple(selected_placements)
    baseline_layout = layout_plan
    baseline_contract: dict[str, object] | None = None
    trace: list[dict[str, object]] = []
    final_contract = None
    final_readiness_contract: dict[str, object] = {}
    entry_inline_issue_count = 0
    large_dirty_inline_guard = False
    if require_inline_verification and large_dirty_inline_issue_threshold > 0 and isinstance(selected_layout, LayoutPlan):
        entry_inline_verification = _build_inline_verification_contract(
            selected_layout,
            graph=graph,
            pdk=pdk,
            min_area_um2_by_layer=route_min_area_um2_by_layer if require_min_area_checks else None,
            require_lvs_labels=require_lvs_labels,
        )
        entry_inline_issue_count = int(entry_inline_verification.get("issue_count", 0) or 0)
        large_dirty_inline_guard = entry_inline_issue_count >= large_dirty_inline_issue_threshold
        if large_dirty_inline_guard:
            effective_max_iterations = min(effective_max_iterations, 1)

    for iteration in range(effective_max_iterations):
        placement_for_iteration = selected_placements if iteration == 0 else _next_placement_candidate(
            ranked_placements,
            current=selected_placements,
        )
        if placement_for_iteration is not None:
            selected_placements = placement_for_iteration
        selected_placements, placement_actions = _legalize_placement_candidate(
            tuple(selected_placements),
            active_constraints,
            pdk,
            graph=graph,
            placement_seed_metadata=effective_seed_metadata,
        )
        if rebuild_layout or selected_layout is None:
            selected_layout = plan_device_layout_ir(
                _materialize_device_plan(graph, sizing or {}, selected_placements, pdk),
                active_constraints,
                pdk,
                routing_corridors=tuple(routing_corridors),
                routing_strategy=routing_strategy,
                **layout_kwargs,
            )
        route_seed_layout = selected_layout
        selected_layout, routing_actions = _legalize_route_candidate(
            selected_layout,
            active_constraints,
            pdk,
            graph=graph,
            hierarchy_context=hierarchy_context,
            include_open_checks=include_open_checks,
            include_via_landing_short_checks=include_via_landing_short_checks,
            require_min_area_checks=require_min_area_checks,
            route_min_area_um2_by_layer=route_min_area_um2_by_layer,
            require_inline_verification=require_inline_verification,
            inline_repair_max_iterations=4 if large_dirty_inline_guard else 8,
            return_after_seed_inline_repair=large_dirty_inline_guard,
        )
        route_pool_candidates: list[Any] = []
        if route_seed_layout is not None:
            route_pool_candidates.append(route_seed_layout)
        route_pool_candidates.append(selected_layout)
        route_pool_candidates.extend(tuple(route_candidates))
        route_pool = tuple(route_pool_candidates)
        strict_route_short_checks = bool(include_via_landing_short_checks or require_inline_verification)
        selected_route, ranked_routes = rank_and_select_interconnect_candidate(
            route_pool,
            active_constraints,
            pdk,
            strict_include_via_landing_short_checks=strict_route_short_checks,
            fallback_to_relaxed=not strict_route_short_checks,
            include_open_checks=include_open_checks,
            require_antenna_checks=require_antenna_checks,
            antenna_max_metal_length_um=antenna_max_metal_length_um,
            antenna_max_length_per_via_um=antenna_max_length_per_via_um,
            require_min_area_checks=require_min_area_checks,
            route_min_area_um2_by_layer=route_min_area_um2_by_layer,
            require_inline_verification=require_inline_verification,
            inline_verification_graph=graph,
            require_lvs_labels=require_lvs_labels,
        )
        selected_layout = selected_route.plan
        final_contract = build_physical_implementation_contract(
            selected_placements,
            selected_layout,
            constraints=active_constraints,
            graph=graph,
            pdk=pdk,
            system_contract=system_contract,
            hierarchy_context=hierarchy_context,
            routing_corridors=tuple(routing_corridors),
            include_open_checks=include_open_checks,
            include_via_landing_short_checks=include_via_landing_short_checks,
            require_antenna_checks=require_antenna_checks,
            antenna_max_metal_length_um=antenna_max_metal_length_um,
            antenna_max_length_per_via_um=antenna_max_length_per_via_um,
            require_min_area_checks=require_min_area_checks,
            route_min_area_um2_by_layer=route_min_area_um2_by_layer,
            require_inline_verification=require_inline_verification,
            require_lvs_labels=require_lvs_labels,
        )
        if baseline_layout is not None:
            if baseline_contract is None:
                baseline_contract = build_physical_implementation_contract(
                    baseline_placements,
                    baseline_layout,
                    constraints=active_constraints,
                    graph=graph,
                    pdk=pdk,
                    system_contract=system_contract,
                    hierarchy_context=hierarchy_context,
                    routing_corridors=tuple(routing_corridors),
                    include_open_checks=include_open_checks,
                    include_via_landing_short_checks=include_via_landing_short_checks,
                    require_antenna_checks=require_antenna_checks,
                    antenna_max_metal_length_um=antenna_max_metal_length_um,
                    antenna_max_length_per_via_um=antenna_max_length_per_via_um,
                    require_min_area_checks=require_min_area_checks,
                    route_min_area_um2_by_layer=route_min_area_um2_by_layer,
                    require_inline_verification=require_inline_verification,
                    require_lvs_labels=require_lvs_labels,
                )
            baseline_issue_count = int(dict(baseline_contract.get("verification", {}) or {}).get("issue_count", 0))
            selected_issue_count = int(dict(final_contract.get("verification", {}) or {}).get("issue_count", 0))
            if baseline_issue_count < selected_issue_count:
                selected_placements = baseline_placements
                selected_layout = baseline_layout
                final_contract = baseline_contract
        readiness_contract = _physical_readiness_contract(final_contract, pdk)
        final_readiness_contract = dict(readiness_contract)
        trace.append(
            {
                "iteration": iteration,
                "effective_max_iterations": int(effective_max_iterations),
                "placement_candidate_rank": _placement_rank_for_candidate(ranked_placements, selected_placements),
                "route_score": float(selected_route.score),
                "route_issues": tuple(str(issue) for issue in selected_route.issues),
                "placement_passed": bool(final_contract["placement"]["passed"]),
                "matching_passed": bool(dict(final_contract["placement"].get("constraint_contract", {})).get("matched_groups_passed", True)),
                "symmetry_passed": bool(dict(final_contract["placement"].get("constraint_contract", {})).get("symmetry_groups_passed", True)),
                "routing_passed": bool(final_contract["routing"]["passed"]),
                "ready_for_extraction": bool(final_contract["readiness"]["ready_for_extraction"]),
                "hierarchy_streamout_ready": bool(readiness_contract.get("streamout_ready", False)),
                "hierarchy_pex_ready": bool(readiness_contract.get("pex_ready", False)),
                "hierarchy_verification_ready": bool(readiness_contract.get("verification_ready", False)),
                "missing_pcell_bindings": tuple(
                    str(name)
                    for name in tuple(dict(final_contract.get("hierarchy_lowering", {}) or {}).get("missing_pcell_bindings", ()))
                    if str(name)
                ),
                "hierarchy_anchor_nets": tuple(
                    str(net)
                    for net in tuple(dict(final_contract.get("hierarchy_lowering", {}) or {}).get("routing_anchor_nets", ()))
                    if str(net)
                ),
                "enclosing_context_partitions": tuple(
                    str(name)
                    for name in tuple(dict(final_contract.get("hierarchy_lowering", {}) or {}).get("enclosing_context_partitions", ()))
                    if str(name)
                ),
                "placement_count": int(final_contract["placement"]["placement_count"]),
                "layout_path_count": len(tuple(getattr(selected_layout, "paths", ()))),
                "placement_actions": tuple(placement_actions),
                "routing_actions": tuple(routing_actions),
                "entry_inline_issue_count": entry_inline_issue_count,
                "large_dirty_inline_guard": large_dirty_inline_guard,
                "large_dirty_inline_issue_threshold": large_dirty_inline_issue_threshold,
                "solver_review_checks": tuple(
                    str(item.get("name", ""))
                    for item in tuple(solver_guide_agent_contract.get("review_checklist", ()) or ())
                    if isinstance(item, Mapping) and str(item.get("name", ""))
                ),
                "solver_fallback_actions": tuple(
                    str(item.get("action", ""))
                    for item in tuple(solver_guide_agent_contract.get("fallback_actions", ()) or ())
                    if isinstance(item, Mapping) and str(item.get("action", ""))
                ),
            }
        )
        if bool(readiness_contract.get("pex_ready", False)):
            break

    design_context = None
    solver_guide = None
    strategy_evaluation = None
    try:
        from analogskills.analysis import build_analog_design_context, evaluate_analog_strategies

        design_context = build_analog_design_context(
            graph,
            constraints=active_constraints,
            pdk=pdk,
            placement_strategy=placement_strategy,
            routing_strategy=routing_strategy,
            iteration_trace=tuple(trace),
        )
        solver_guide = getattr(design_context, "guide", None)
        strategy_evaluation = evaluate_analog_strategies(
            design_context,
            placement_strategy=placement_strategy,
            routing_strategy=routing_strategy,
        )
    except Exception:
        design_context = None
        solver_guide = None
        strategy_evaluation = None
    if design_context is None:
        design_context = explicit_design_context
    if solver_guide is None:
        solver_guide = explicit_solver_guide
    if strategy_evaluation is None and design_context is not None:
        try:
            from analogskills.analysis import evaluate_analog_strategies

            strategy_evaluation = evaluate_analog_strategies(
                design_context,
                placement_strategy=placement_strategy,
                routing_strategy=routing_strategy,
            )
        except Exception:
            strategy_evaluation = None

    if isinstance(selected_layout, LayoutPlan):
        selected_layout = replace(
            selected_layout,
            metadata={
                **dict(selected_layout.metadata),
                "analog_design_context": _snapshot_analog_design_context(design_context),
                "analog_solver_guide": _snapshot_analog_solver_guide(solver_guide),
                "analog_strategy_evaluation": _snapshot_analog_strategy_evaluation(strategy_evaluation),
                "placement_strategy": _snapshot_analog_placement_strategy(placement_strategy),
                "routing_strategy": _snapshot_analog_routing_strategy(routing_strategy),
                "legalization_iterations": tuple(dict(item) for item in trace),
            },
        )

    return {
        "placements": tuple(selected_placements),
        "layout_plan": selected_layout,
        "contract": final_contract or {},
        "readiness_contract": final_readiness_contract,
        "blocking_reasons": _physical_legalization_blocking_reasons(final_contract or {}, final_readiness_contract),
        "iterations": tuple(trace),
        "design_context": design_context,
        "solver_guide": solver_guide,
        "strategy_evaluation": strategy_evaluation,
        "effective_max_iterations": int(effective_max_iterations),
        "legalized": bool(final_readiness_contract.get("pex_ready", False)),
    }


def rank_physical_implementation_candidates(
    graph: Any,
    *,
    placement_candidates: Sequence[Sequence[Any]],
    route_candidates: Sequence[Any],
    constraints: LayoutConstraintSet | None = None,
    pdk: PdkConfig | None = None,
    routing_corridors: Sequence[Any] = (),
    include_open_checks: bool = False,
    include_via_landing_short_checks: bool = False,
    require_antenna_checks: bool = False,
    antenna_max_metal_length_um: float = 20.0,
    antenna_max_length_per_via_um: float = 10.0,
    require_min_area_checks: bool = False,
    route_min_area_um2_by_layer: Mapping[str, float] | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
    placement_seed_metadata: Mapping[str, object] | None = None,
    score_weights: Mapping[str, float] | None = None,
    foundry_deck_spec: Mapping[str, object] | None = None,
    foundry_available_inputs: Mapping[str, object] | None = None,
    top_k: int | None = None,
) -> tuple[dict[str, object], ...]:
    """Rank combined placement/routing physical candidates with one stable contract."""

    from .placement import rank_placement_candidates
    from analogskills.opt.blackbox import score_physical_implementation

    pdk = pdk or PdkConfig.generic()
    active_constraints = constraints or getattr(graph, "layout_constraints", None) or LayoutConstraintSet()
    effective_seed_metadata = _enrich_seed_metadata_from_hierarchy(placement_seed_metadata, hierarchy_context)
    ranked_placements = rank_placement_candidates(
        tuple(tuple(candidate) for candidate in placement_candidates),
        active_constraints,
        graph=graph,
        pdk=pdk,
        placement_seed_metadata=effective_seed_metadata,
    )
    ranked_routes = rank_interconnect_candidates(
        tuple(route_candidates),
        active_constraints,
        pdk,
        include_open_checks=include_open_checks,
        include_via_landing_short_checks=include_via_landing_short_checks,
        require_antenna_checks=require_antenna_checks,
        antenna_max_metal_length_um=antenna_max_metal_length_um,
        antenna_max_length_per_via_um=antenna_max_length_per_via_um,
        require_min_area_checks=require_min_area_checks,
        route_min_area_um2_by_layer=route_min_area_um2_by_layer,
        hierarchy_context=hierarchy_context,
    )

    rows: list[dict[str, object]] = []
    for placement_index, placement_row in enumerate(ranked_placements):
        for route_index, route_row in enumerate(ranked_routes):
            effective_hierarchy_context = dict(hierarchy_context or {})
            if "hierarchical_partition_parasitic_target_plan" not in effective_hierarchy_context:
                effective_hierarchy_context["hierarchical_partition_parasitic_target_plan"] = dict(
                    (hierarchy_context or {}).get("hierarchical_partition_parasitic_target_plan", {}) or {}
                )
            contract = build_physical_implementation_contract(
                placement_row.placements,
                route_row.plan,
                constraints=active_constraints,
                graph=graph,
                pdk=pdk,
                system_contract=dict(effective_hierarchy_context.get("hierarchical_system_contract", {}) or {}) if effective_hierarchy_context else None,
                hierarchy_context=effective_hierarchy_context,
                routing_corridors=tuple(routing_corridors),
                include_open_checks=include_open_checks,
                include_via_landing_short_checks=include_via_landing_short_checks,
                require_antenna_checks=require_antenna_checks,
                antenna_max_metal_length_um=antenna_max_metal_length_um,
                antenna_max_length_per_via_um=antenna_max_length_per_via_um,
                require_min_area_checks=require_min_area_checks,
                route_min_area_um2_by_layer=route_min_area_um2_by_layer,
                require_inline_verification=require_inline_verification,
                require_lvs_labels=require_lvs_labels,
            )
            implementation_lowering_contract = _candidate_implementation_lowering_contract(effective_hierarchy_context)
            partition_implementation_bundle_contract = _candidate_partition_implementation_bundle_contract(effective_hierarchy_context)
            partition_realization_contract = _candidate_partition_realization_contract(effective_hierarchy_context)
            partition_pcell_binding_contract = _candidate_partition_pcell_binding_contract(effective_hierarchy_context)
            partition_parasitic_target_contract = _candidate_partition_parasitic_target_contract(effective_hierarchy_context)
            verification_intent_contract = _candidate_verification_intent_contract(effective_hierarchy_context)
            if not dict(effective_hierarchy_context.get("hierarchical_partition_parasitic_target_plan", {}) or {}) and partition_parasitic_target_contract.get("present", False):
                effective_hierarchy_context["hierarchical_partition_parasitic_target_plan"] = {
                    "topology_name": str(partition_parasitic_target_contract.get("topology_name", "")),
                    "partitions": tuple(partition_parasitic_target_contract.get("partitions", ()) or ()),
                    "summary": tuple(partition_parasitic_target_contract.get("summary", ()) or ()),
                }
            contract["hierarchy_binding"] = dict(partition_pcell_binding_contract)
            contract["hierarchy_parasitics"] = dict(partition_parasitic_target_contract)
            contract["hierarchy_implementation_bundle"] = dict(partition_implementation_bundle_contract)
            physical_score = score_physical_implementation(contract, weights=score_weights)
            readiness_contract = _physical_readiness_contract(contract, pdk)
            foundry_execution_contract = _foundry_execution_contract(
                readiness_contract,
                contract,
                foundry_deck_spec=foundry_deck_spec,
                foundry_available_inputs=foundry_available_inputs,
            )
            foundry_costs = _foundry_candidate_costs(foundry_execution_contract)
            foundry_score = _foundry_candidate_score(foundry_costs)
            foundry_metrics = _foundry_candidate_metrics(foundry_execution_contract, foundry_costs)
            implementation_backbone_score = _candidate_backbone_score(
                placement_score=float(placement_row.score),
                routing_score=float(route_row.score),
                physical_score=physical_score,
                foundry_score=foundry_score,
                foundry_metrics=foundry_metrics,
            )
            legalization_action_summary = _physical_candidate_legalization_summary(contract, hierarchy_context=effective_hierarchy_context)
            rows.append(
                {
                    "placement_rank": placement_index,
                    "route_rank": route_index,
                    "placement": {
                        "score": float(placement_row.score),
                        "costs": dict(placement_row.costs),
                        "issues": tuple(str(issue) for issue in placement_row.issues),
                        "placements": tuple(placement_row.placements),
                    },
                    "routing": {
                        "score": float(route_row.score),
                        "costs": dict(route_row.costs),
                        "issues": tuple(str(issue) for issue in route_row.issues),
                        "plan": route_row.plan,
                    },
                    "contract": contract,
                    "readiness_contract": readiness_contract,
                    "foundry_execution_contract": foundry_execution_contract,
                    "foundry_score": foundry_score,
                    "foundry_metrics": foundry_metrics,
                    "implementation_backbone_score": implementation_backbone_score,
                    "candidate_contract": {
                        "placement_rank": placement_index,
                        "route_rank": route_index,
                        "matched_groups_passed": bool(dict(contract.get("placement", {})).get("constraint_contract", {}).get("matched_groups_passed", True)),
                        "symmetry_groups_passed": bool(dict(contract.get("placement", {})).get("constraint_contract", {}).get("symmetry_groups_passed", True)),
                        "matched_group_count": int(dict(contract.get("placement", {})).get("constraint_contract", {}).get("matched_group_count", 0)),
                        "symmetry_group_count": int(dict(contract.get("placement", {})).get("constraint_contract", {}).get("symmetry_group_count", 0)),
                        "preferred_partition_order": tuple(str(name) for name in tuple(effective_seed_metadata.get("preferred_partition_order", ()))),
                        "anchor_partitions": tuple(str(name) for name in tuple(effective_seed_metadata.get("anchor_partitions", ()))),
                        "focus_partitions": tuple(str(name) for name in tuple(effective_seed_metadata.get("focus_partitions", ()))),
                        "partition_order_violations": float(placement_row.costs.get("partition_order_violations", 0.0)),
                        "anchor_partition_spread": float(placement_row.costs.get("anchor_partition_spread", 0.0)),
                        "focus_partition_separation": float(placement_row.costs.get("focus_partition_separation", 0.0)),
                        "focus_partition_target_shortfall": float(placement_row.costs.get("focus_partition_target_shortfall", 0.0)),
                        "anchor_partition_target_overflow": float(placement_row.costs.get("anchor_partition_target_overflow", 0.0)),
                        "pcell_partition_internal_spread": float(placement_row.costs.get("pcell_partition_internal_spread", 0.0)),
                        "pex_focus_partition_spread": float(placement_row.costs.get("pex_focus_partition_spread", 0.0)),
                        "reference_sensitive_partition_spread": float(placement_row.costs.get("reference_sensitive_partition_spread", 0.0)),
                        "feedback_sensitive_partition_spread": float(placement_row.costs.get("feedback_sensitive_partition_spread", 0.0)),
                        "analog_placement_profile": dict(dict(contract.get("placement", {})).get("analog_profile", {}) or {}),
                        "analog_routing_profile": dict(dict(contract.get("pdk", {})).get("analog_routing_constraints", {}) or {}),
                        "corridor_issue_count": float(route_row.costs.get("corridor_violation", 0.0)),
                        "hierarchy_bus_restore_risk": float(route_row.costs.get("hierarchy_bus_restore_risk", 0.0)),
                        "hierarchy_feedback_restore_risk": float(route_row.costs.get("hierarchy_feedback_restore_risk", 0.0)),
                        "hierarchy_parasitic_focus_risk": float(route_row.costs.get("hierarchy_parasitic_focus_risk", 0.0)),
                        "hierarchy_reference_restore_risk": float(route_row.costs.get("hierarchy_reference_restore_risk", 0.0)),
                        "hierarchy_anchor_net_restore_risk": float(route_row.costs.get("hierarchy_anchor_net_restore_risk", 0.0)),
                        "routing_corridor_count": len(tuple(routing_corridors)),
                        "hierarchy_context_present": bool(hierarchy_context),
                        "pex_ready": bool(readiness_contract.get("pex_ready", False)),
                        "verification_ready": bool(readiness_contract.get("verification_ready", False)),
                        "streamout_ready": bool(readiness_contract.get("streamout_ready", False)),
                        "foundry_ready": bool(foundry_metrics.get("foundry_ready", 0.0)),
                        "foundry_ready_stage_count": float(foundry_metrics.get("foundry_ready_stage_count", 0.0)),
                        "foundry_blocked_stage_count": float(foundry_metrics.get("foundry_blocked_stage_count", 0.0)),
                        "foundry_issue_count": float(foundry_metrics.get("foundry_issue_count", 0.0)),
                        "foundry_missing_input_count": float(foundry_metrics.get("foundry_missing_input_count", 0.0)),
                        "foundry_missing_file_count": float(foundry_metrics.get("foundry_missing_file_count", 0.0)),
                        "foundry_stage_coverage": float(foundry_metrics.get("foundry_stage_coverage", 0.0)),
                        "foundry_blocked_stage_fraction": float(foundry_metrics.get("foundry_blocked_stage_fraction", 0.0)),
                        "legalization_action_summary": legalization_action_summary,
                        "implementation_lowering_contract": implementation_lowering_contract,
                        "partition_implementation_bundle_contract": partition_implementation_bundle_contract,
                        "partition_realization_contract": partition_realization_contract,
                        "partition_pcell_binding_contract": partition_pcell_binding_contract,
                        "partition_parasitic_target_contract": partition_parasitic_target_contract,
                        "verification_intent_contract": verification_intent_contract,
                    },
                    "physical_score": physical_score,
                    "combined_score": float(implementation_backbone_score["score"]),
                }
            )
    ranked = tuple(
        sorted(
            rows,
            key=lambda row: (
                float(row["combined_score"]),
                float(dict(row.get("foundry_score", {})).get("score", 0.0)),
                float(dict(row["physical_score"]).get("score", 0.0)),
            ),
        )
    )
    selected = ranked[:top_k] if top_k is not None else ranked
    return tuple(
        {
            **row,
            "selected": index == 0,
        }
        for index, row in enumerate(selected)
    )


def screen_physical_implementation_candidates(
    graph: Any,
    *,
    placement_candidates: Sequence[Sequence[Any]],
    route_candidates: Sequence[Any],
    constraints: LayoutConstraintSet | None = None,
    pdk: PdkConfig | None = None,
    routing_corridors: Sequence[Any] = (),
    include_open_checks: bool = False,
    include_via_landing_short_checks: bool = False,
    require_antenna_checks: bool = False,
    antenna_max_metal_length_um: float = 20.0,
    antenna_max_length_per_via_um: float = 10.0,
    require_min_area_checks: bool = False,
    route_min_area_um2_by_layer: Mapping[str, float] | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
    placement_seed_metadata: Mapping[str, object] | None = None,
    score_weights: Mapping[str, float] | None = None,
    foundry_deck_spec: Mapping[str, object] | None = None,
    foundry_available_inputs: Mapping[str, object] | None = None,
    placement_top_k: int = 3,
    routing_top_k: int = 3,
    physical_top_k: int = 5,
) -> dict[str, object]:
    """Run staged candidate screening without taking over broader orchestration."""

    from .placement import rank_placement_candidates

    pdk = pdk or PdkConfig.generic()
    active_constraints = constraints or getattr(graph, "layout_constraints", None) or LayoutConstraintSet()
    effective_seed_metadata = _enrich_seed_metadata_from_hierarchy(placement_seed_metadata, hierarchy_context)

    ranked_placements = rank_placement_candidates(
        tuple(tuple(candidate) for candidate in placement_candidates),
        active_constraints,
        graph=graph,
        pdk=pdk,
        placement_seed_metadata=effective_seed_metadata,
        top_k=placement_top_k,
    )
    ranked_routes = rank_interconnect_candidates(
        tuple(route_candidates),
        active_constraints,
        pdk,
        include_open_checks=include_open_checks,
        include_via_landing_short_checks=include_via_landing_short_checks,
        require_antenna_checks=require_antenna_checks,
        antenna_max_metal_length_um=antenna_max_metal_length_um,
        antenna_max_length_per_via_um=antenna_max_length_per_via_um,
        require_min_area_checks=require_min_area_checks,
        route_min_area_um2_by_layer=route_min_area_um2_by_layer,
        hierarchy_context=hierarchy_context,
        top_k=routing_top_k,
    )
    physical = rank_physical_implementation_candidates(
        graph,
        placement_candidates=tuple(row.placements for row in ranked_placements),
        route_candidates=tuple(row.plan for row in ranked_routes),
        constraints=active_constraints,
        pdk=pdk,
        routing_corridors=routing_corridors,
        include_open_checks=include_open_checks,
        include_via_landing_short_checks=include_via_landing_short_checks,
        require_antenna_checks=require_antenna_checks,
        antenna_max_metal_length_um=antenna_max_metal_length_um,
        antenna_max_length_per_via_um=antenna_max_length_per_via_um,
        require_min_area_checks=require_min_area_checks,
        route_min_area_um2_by_layer=route_min_area_um2_by_layer,
        hierarchy_context=hierarchy_context,
        placement_seed_metadata=effective_seed_metadata,
        score_weights=score_weights,
        foundry_deck_spec=foundry_deck_spec,
        foundry_available_inputs=foundry_available_inputs,
        top_k=physical_top_k,
    )
    return {
        "placement_candidates": tuple(
            {
                "score": float(row.score),
                "costs": dict(row.costs),
                "issues": tuple(row.issues),
                "placements": tuple(row.placements),
                "selected": index == 0,
            }
            for index, row in enumerate(ranked_placements)
        ),
        "route_candidates": tuple(
            {
                "score": float(row.score),
                "costs": dict(row.costs),
                "issues": tuple(row.issues),
                "plan": row.plan,
                "selected": index == 0,
            }
            for index, row in enumerate(ranked_routes)
        ),
        "physical_candidates": tuple(physical),
        "metadata": {
            "placement_top_k": int(placement_top_k),
            "routing_top_k": int(routing_top_k),
            "physical_top_k": int(physical_top_k),
            "hierarchy_context_present": bool(hierarchy_context),
            "routing_corridor_count": len(tuple(routing_corridors)),
            "foundry_deck_spec_present": bool(foundry_deck_spec),
            **_screened_foundry_metadata(physical),
        },
    }


def _physical_readiness_contract(contract: Mapping[str, object], pdk: PdkConfig) -> dict[str, object]:
    readiness = dict(contract.get("readiness", {}) or {})
    hierarchy_lowering = dict(contract.get("hierarchy_lowering", {}) or {})
    hierarchy_bundle = dict(contract.get("hierarchy_implementation_bundle", {}) or {})
    issues = dict(contract.get("issues", {}) or {})
    pdk_issues = tuple(str(issue) for issue in issues.get("pdk", ()) if str(issue))
    placement_issues = tuple(str(issue) for issue in issues.get("placement", ()) if str(issue))
    routing_issues = tuple(str(issue) for issue in issues.get("routing", ()) if str(issue))
    missing_pcell_bindings = tuple(str(name) for name in tuple(hierarchy_lowering.get("missing_pcell_bindings", ())) if str(name))
    hierarchy_pdk_ready = not missing_pcell_bindings
    streamout_ready = bool(readiness.get("pdk_valid", False) and hierarchy_pdk_ready)
    extraction_corner_count = len(tuple(getattr(pdk, "extraction_corners", ()) or ()))
    bundle_partition_count = int(hierarchy_bundle.get("partition_count", 0) or 0)
    bundle_implementation_ready_partition_count = int(hierarchy_bundle.get("implementation_ready_partition_count", 0) or 0)
    bundle_blocked_partition_count = int(hierarchy_bundle.get("blocked_partition_count", 0) or 0)
    hierarchy_bundle_ready = bundle_blocked_partition_count == 0 if bundle_partition_count > 0 else True
    pex_ready = bool(readiness.get("ready_for_extraction", False) and hierarchy_pdk_ready and hierarchy_bundle_ready)
    verification_ready = bool(streamout_ready and pex_ready and extraction_corner_count > 0)
    return {
        "streamout_ready": streamout_ready,
        "pex_ready": pex_ready,
        "verification_ready": verification_ready,
        "pdk_issue_count": len(pdk_issues),
        "placement_issue_count": len(placement_issues),
        "routing_issue_count": len(routing_issues),
        "missing_pcell_binding_count": len(missing_pcell_bindings),
        "bundle_partition_count": bundle_partition_count,
        "bundle_implementation_ready_partition_count": bundle_implementation_ready_partition_count,
        "bundle_blocked_partition_count": bundle_blocked_partition_count,
        "extraction_corner_count": extraction_corner_count,
        "blocking_issue_count": len(pdk_issues) + len(placement_issues) + len(routing_issues) + len(missing_pcell_bindings) + bundle_blocked_partition_count,
    }


def _physical_candidate_legalization_summary(
    contract: Mapping[str, object],
    *,
    hierarchy_context: Mapping[str, object] | None = None,
    solver_guide: "AnalogSolverGuide | Mapping[str, object] | None" = None,
) -> dict[str, object]:
    placement = dict(contract.get("placement", {}) or {})
    routing = dict(contract.get("routing", {}) or {})
    hierarchy_lowering = dict(contract.get("hierarchy_lowering", {}) or {})
    hierarchy_parasitics = dict(contract.get("hierarchy_parasitics", {}) or {})
    agent_contract = _solver_guide_agent_contract_payload(solver_guide)
    placement_actions: list[str] = []
    routing_actions: list[str] = []
    if int(dict(placement.get("legality_contract", {}) or {}).get("row_policy_issue_count", 0)) > 0:
        placement_actions.append("enforce_role_row_policy")
    if int(dict(placement.get("legality_contract", {}) or {}).get("orientation_policy_issue_count", 0)) > 0:
        placement_actions.append("enforce_role_orientation_policy")
    if tuple(str(name) for name in tuple((hierarchy_context or {}).get("hierarchical_floorplan_plan", {}).get("preferred_partition_order", ())) if str(name)):
        placement_actions.append("retune_partition_order_from_floorplan_seed")
    if tuple(str(name) for name in tuple((hierarchy_context or {}).get("hierarchical_floorplan_plan", {}).get("anchor_partitions", ())) if str(name)):
        placement_actions.append("compact_anchor_partitions_from_floorplan_seed")
    if tuple(str(name) for name in tuple((hierarchy_context or {}).get("hierarchical_floorplan_plan", {}).get("focus_partitions", ())) if str(name)):
        placement_actions.append("separate_focus_partitions_from_floorplan_seed")
    routing_legality = dict(routing.get("legality_contract", {}) or {})
    if int(routing_legality.get("current_capacity_issue_count", 0)) > 0:
        routing_actions.append("widen_or_relayer_current_limited_routes")
    if int(routing_legality.get("via_capacity_issue_count", 0)) > 0:
        routing_actions.append("increase_via_array_capacity")
    if int(routing_legality.get("bus_order_issue_count", 0)) > 0:
        routing_actions.append("restore_bus_order")
    if tuple(str(net) for net in tuple(hierarchy_lowering.get("routing_anchor_nets", ())) if str(net)):
        routing_actions.append("protect_hierarchical_anchor_nets_during_legalization")
    architecture_critical_partition_present = any(
        isinstance(partition, Mapping)
        and bool(dict(partition.get("architecture_budget", {}) or {}))
        and str(
            dict(partition.get("architecture_budget", {}) or {}).get(
                "sensitivity",
                dict(partition.get("architecture_budget", {}) or {}).get("sensitivity_class", ""),
            )
            or ""
        ) in {"reference_critical", "timing_critical", "feedback_critical"}
        for partition in tuple(hierarchy_parasitics.get("partitions", ()) or ())
    )
    if architecture_critical_partition_present:
        routing_actions.append("protect_architecture_critical_nets_during_legalization")
    readiness = dict(contract.get("readiness", {}) or {})
    blocking_reasons: list[str] = []
    for group_name in ("pdk", "placement", "routing"):
        for issue in tuple(dict(contract.get("issues", {}) or {}).get(group_name, ()) or ()):
            text = str(issue)
            if text:
                blocking_reasons.append(text)
    for partition in tuple(hierarchy_lowering.get("missing_pcell_bindings", ()) or ()):
        name = str(partition)
        if name:
            blocking_reasons.append(f"missing PDK PCell binding for lowered partition {name}")
    if not bool(readiness.get("pdk_valid", False)):
        blocking_reasons.append("candidate is not PDK-valid for streamout")
    if not bool(readiness.get("ready_for_extraction", False)):
        blocking_reasons.append("candidate is not ready for extraction")
    return {
        "placement_actions": tuple(dict.fromkeys(placement_actions)),
        "routing_actions": tuple(dict.fromkeys(routing_actions)),
        "blocking_reasons": tuple(dict.fromkeys(blocking_reasons)),
        "forbidden_actions": tuple(
            str(item.get("name", ""))
            for item in tuple(agent_contract.get("forbidden_actions", ()) or ())
            if isinstance(item, Mapping) and str(item.get("name", ""))
        ),
        "required_artifacts": tuple(
            str(item.get("name", ""))
            for item in tuple(agent_contract.get("required_artifacts", ()) or ())
            if isinstance(item, Mapping) and str(item.get("name", ""))
        ),
        "review_checks": tuple(
            str(item.get("name", ""))
            for item in tuple(agent_contract.get("review_checklist", ()) or ())
            if isinstance(item, Mapping) and str(item.get("name", ""))
        ),
        "fallback_actions": tuple(
            str(item.get("action", ""))
            for item in tuple(agent_contract.get("fallback_actions", ()) or ())
            if isinstance(item, Mapping) and str(item.get("action", ""))
        ),
        "iteration_policy": dict(agent_contract.get("iteration_policy", {}) or {}),
    }


def _physical_legalization_blocking_reasons(
    contract: Mapping[str, object],
    readiness_contract: Mapping[str, object],
) -> tuple[str, ...]:
    issues = dict(contract.get("issues", {}) or {})
    hierarchy_lowering = dict(contract.get("hierarchy_lowering", {}) or {})
    reasons: list[str] = []
    for group_name in ("pdk", "placement", "routing"):
        for issue in tuple(issues.get(group_name, ()) or ()):
            text = str(issue)
            if text:
                reasons.append(text)
    for partition in tuple(hierarchy_lowering.get("missing_pcell_bindings", ()) or ()):
        name = str(partition)
        if name:
            reasons.append(f"missing PDK PCell binding for lowered partition {name}")
    if not bool(readiness_contract.get("streamout_ready", False)):
        reasons.append("candidate is not hierarchy-safe for streamout")
    if not bool(readiness_contract.get("pex_ready", False)):
        reasons.append("candidate is not hierarchy-safe for extraction")
    return tuple(dict.fromkeys(reasons))


def _foundry_execution_contract(
    readiness_contract: Mapping[str, object],
    physical_contract: Mapping[str, object],
    *,
    foundry_deck_spec: Mapping[str, object] | None = None,
    foundry_available_inputs: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    if not foundry_deck_spec:
        return None
    from analogskills.eda import build_foundry_execution_readiness_contract

    return build_foundry_execution_readiness_contract(
        candidate_readiness=readiness_contract,
        physical_contract=physical_contract,
        deck_spec=foundry_deck_spec,
        available_inputs=foundry_available_inputs,
    )


def _foundry_candidate_costs(foundry_execution_contract: Mapping[str, object] | None) -> dict[str, float]:
    if not foundry_execution_contract:
        return {
            "contract_present": 0.0,
            "blocked_stage_count": 0.0,
            "issue_count": 0.0,
            "missing_input_count": 0.0,
            "missing_file_count": 0.0,
            "binding_blocked_partition_count": 0.0,
            "architecture_budget_blocked_partition_count": 0.0,
            "macro_binding_partition_count": 0.0,
        }
    stages = dict(foundry_execution_contract.get("stages", {}) or {})
    issue_count = float(len(tuple(foundry_execution_contract.get("issues", ()) or ())))
    binding_summary = dict(foundry_execution_contract.get("hierarchy_binding_summary", {}) or {})
    missing_input_count = float(
        sum(len(tuple(dict(stage).get("missing_inputs", ()) or ())) for stage in stages.values())
    )
    missing_file_count = float(
        sum(len(tuple(dict(stage).get("missing_files", ()) or ())) for stage in stages.values())
    )
    return {
        "contract_present": 1.0,
        "blocked_stage_count": float(len(tuple(foundry_execution_contract.get("blocked_stages", ()) or ()))),
        "issue_count": issue_count,
        "missing_input_count": missing_input_count,
        "missing_file_count": missing_file_count,
        "binding_blocked_partition_count": float(
            len(tuple(binding_summary.get("binding_blocked_partitions", ()) or ()))
        ),
        "architecture_budget_blocked_partition_count": float(
            len(tuple(binding_summary.get("architecture_budget_blocked_partitions", ()) or ()))
        ),
        "macro_binding_partition_count": float(
            len(tuple(binding_summary.get("macro_binding_partitions", ()) or ()))
        ),
    }


def _foundry_candidate_score(costs: Mapping[str, float]) -> dict[str, object]:
    normalized = {str(name): float(value) for name, value in dict(costs).items()}
    weight_map = {
        "contract_present": 0.0,
        "blocked_stage_count": 40.0,
        "issue_count": 8.0,
        "missing_input_count": 12.0,
        "missing_file_count": 12.0,
        "binding_blocked_partition_count": 10.0,
        "architecture_budget_blocked_partition_count": 8.0,
        "macro_binding_partition_count": 1.5,
    }
    return {
        "score": float(sum(weight_map.get(name, 0.0) * value for name, value in normalized.items())),
        "costs": normalized,
        "weights": weight_map,
    }


def _foundry_candidate_metrics(
    foundry_execution_contract: Mapping[str, object] | None,
    costs: Mapping[str, float],
) -> dict[str, float]:
    contract = dict(foundry_execution_contract or {})
    stages = dict(contract.get("stages", {}) or {})
    ready_stages = tuple(contract.get("ready_stages", ()) or ())
    blocked_stages = tuple(contract.get("blocked_stages", ()) or ())
    binding_summary = dict(contract.get("hierarchy_binding_summary", {}) or {})
    stage_count = len(stages)
    return {
        "foundry_ready": 1.0 if bool(contract.get("ready", False)) else 0.0,
        "foundry_ready_stage_count": float(len(ready_stages)),
        "foundry_blocked_stage_count": float(costs.get("blocked_stage_count", 0.0)),
        "foundry_missing_input_count": float(costs.get("missing_input_count", 0.0)),
        "foundry_missing_file_count": float(costs.get("missing_file_count", 0.0)),
        "foundry_issue_count": float(costs.get("issue_count", 0.0)),
        "foundry_stage_coverage": (float(len(ready_stages)) / float(stage_count)) if stage_count else 0.0,
        "foundry_blocked_stage_fraction": (float(len(blocked_stages)) / float(stage_count)) if stage_count else 0.0,
        "foundry_binding_blocked_partition_count": float(
            len(tuple(binding_summary.get("binding_blocked_partitions", ()) or ()))
        ),
        "foundry_architecture_budget_blocked_partition_count": float(
            len(tuple(binding_summary.get("architecture_budget_blocked_partitions", ()) or ()))
        ),
        "foundry_macro_binding_partition_count": float(
            len(tuple(binding_summary.get("macro_binding_partitions", ()) or ()))
        ),
    }


def _candidate_backbone_score(
    *,
    placement_score: float,
    routing_score: float,
    physical_score: Mapping[str, object],
    foundry_score: Mapping[str, object],
    foundry_metrics: Mapping[str, float],
) -> dict[str, object]:
    physical_total = float(dict(physical_score).get("score", 0.0))
    foundry_total = float(dict(foundry_score).get("score", 0.0))
    total = float(placement_score) + float(routing_score) + physical_total + foundry_total
    return {
        "score": total,
        "components": {
            "placement_score": float(placement_score),
            "routing_score": float(routing_score),
            "physical_score": physical_total,
            "foundry_penalty": foundry_total,
        },
        "metrics": {
            **{str(name): float(value) for name, value in dict(dict(physical_score).get("metrics", {})).items()},
            **{str(name): float(value) for name, value in dict(foundry_metrics).items()},
        },
        "readiness": {
            **dict(dict(physical_score).get("readiness", {})),
            "foundry_ready": bool(dict(foundry_metrics).get("foundry_ready", 0.0)),
        },
    }


def _screened_foundry_metadata(physical_candidates: Sequence[Mapping[str, object]]) -> dict[str, object]:
    from analogskills.eda.reports import summarize_foundry_execution_contract

    rows = tuple(dict(candidate) for candidate in physical_candidates)
    if not rows:
        return {
            "foundry_ready_candidate_count": 0,
            "foundry_blocked_candidate_count": 0,
            "foundry_blocked_stage_histogram": {},
            "foundry_missing_input_histogram": {},
            "foundry_missing_file_histogram": {},
            "foundry_binding_blocked_partition_histogram": {},
            "foundry_architecture_budget_blocked_partition_histogram": {},
            "foundry_macro_binding_partition_histogram": {},
            "foundry_best_candidate_summary": (),
            "foundry_best_candidate_metrics": {},
            "best_candidate_contract": {},
        }
    blocked_stage_histogram: dict[str, int] = {}
    missing_input_histogram: dict[str, int] = {}
    missing_file_histogram: dict[str, int] = {}
    binding_blocked_partition_histogram: dict[str, int] = {}
    architecture_budget_blocked_partition_histogram: dict[str, int] = {}
    macro_binding_partition_histogram: dict[str, int] = {}
    ready_count = 0
    for candidate in rows:
        contract = dict(candidate.get("foundry_execution_contract", {}) or {})
        binding_summary = dict(contract.get("hierarchy_binding_summary", {}) or {})
        if bool(contract.get("ready", False)):
            ready_count += 1
        for stage in tuple(contract.get("blocked_stages", ()) or ()):
            key = str(stage)
            if key:
                blocked_stage_histogram[key] = blocked_stage_histogram.get(key, 0) + 1
        for name in tuple(binding_summary.get("binding_blocked_partitions", ()) or ()):
            key = str(name)
            if key:
                binding_blocked_partition_histogram[key] = binding_blocked_partition_histogram.get(key, 0) + 1
        for name in tuple(binding_summary.get("architecture_budget_blocked_partitions", ()) or ()):
            key = str(name)
            if key:
                architecture_budget_blocked_partition_histogram[key] = architecture_budget_blocked_partition_histogram.get(key, 0) + 1
        for name in tuple(binding_summary.get("macro_binding_partitions", ()) or ()):
            key = str(name)
            if key:
                macro_binding_partition_histogram[key] = macro_binding_partition_histogram.get(key, 0) + 1
        for stage_row in dict(contract.get("stages", {}) or {}).values():
            stage_mapping = dict(stage_row or {})
            for name in tuple(stage_mapping.get("missing_inputs", ()) or ()):
                key = str(name)
                if key:
                    missing_input_histogram[key] = missing_input_histogram.get(key, 0) + 1
            for name in tuple(stage_mapping.get("missing_files", ()) or ()):
                key = str(name)
                if key:
                    missing_file_histogram[key] = missing_file_histogram.get(key, 0) + 1
    best_summary = tuple(
        summarize_foundry_execution_contract(dict(rows[0].get("foundry_execution_contract", {}) or {})).summary
    )
    return {
        "foundry_ready_candidate_count": ready_count,
        "foundry_blocked_candidate_count": len(rows) - ready_count,
        "foundry_blocked_stage_histogram": blocked_stage_histogram,
        "foundry_missing_input_histogram": missing_input_histogram,
        "foundry_missing_file_histogram": missing_file_histogram,
        "foundry_binding_blocked_partition_histogram": binding_blocked_partition_histogram,
        "foundry_architecture_budget_blocked_partition_histogram": architecture_budget_blocked_partition_histogram,
        "foundry_macro_binding_partition_histogram": macro_binding_partition_histogram,
        "foundry_best_candidate_summary": best_summary,
        "foundry_best_candidate_metrics": dict(rows[0].get("foundry_metrics", {}) or {}),
        "best_candidate_contract": dict(rows[0].get("candidate_contract", {}) or {}),
    }


def _candidate_implementation_lowering_contract(
    hierarchy_context: Mapping[str, object] | None,
) -> dict[str, object]:
    lowering = dict((hierarchy_context or {}).get("hierarchical_implementation_lowering", {}) or {})
    partitions = tuple(
        dict(item)
        for item in tuple(lowering.get("partitions", ()) or ())
        if isinstance(item, Mapping)
    )
    if not partitions:
        return {
            "present": False,
            "topology_name": str(lowering.get("topology_name", "")),
            "partition_count": 0,
            "materialized_partition_count": 0,
            "primitive_partition_count": 0,
            "macro_partition_count": 0,
            "behavioral_stub_partition_count": 0,
            "pcell_backbone_partition_count": 0,
            "macro_boundary_partition_count": 0,
            "behavioral_model_partition_count": 0,
            "restore_partition_count": 0,
            "required_external_net_count": 0,
            "routing_anchor_net_count": 0,
            "exposed_pin_count": 0,
            "device_template_count": 0,
            "partition_verification_ready_count": 0,
            "device_synthesis_ready_count": 0,
            "pdk_bindable_partition_count": 0,
            "pdk_implementation_ready_count": 0,
            "materialized_partitions": (),
            "implementation_carriers": (),
            "summary": tuple(lowering.get("summary", ()) or ()),
        }
    materialized = tuple(str(item.get("name", "")) for item in partitions if bool(item.get("materialized", False)) and str(item.get("name", "")))
    primitive_count = sum(1 for item in partitions if str(item.get("implementation_class", "")) == "primitive")
    macro_count = sum(1 for item in partitions if str(item.get("implementation_class", "")) == "macro")
    stub_count = sum(1 for item in partitions if str(item.get("implementation_class", "")) == "behavioral_stub")
    pcell_backbone_count = sum(1 for item in partitions if str(item.get("implementation_carrier", "")) == "pcell_backbone")
    macro_boundary_count = sum(1 for item in partitions if str(item.get("implementation_carrier", "")) == "macro_boundary")
    behavioral_model_count = sum(1 for item in partitions if str(item.get("implementation_carrier", "")) == "behavioral_model")
    restore_count = sum(
        1
        for item in partitions
        if bool(item.get("restore_bus_corridor", False)) or bool(item.get("restore_feedback_loop", False))
    )
    required_external_net_count = sum(
        len(tuple(item.get("required_external_nets", ()) or ()))
        for item in partitions
    )
    exposed_pin_count = sum(
        len(tuple(item.get("exposed_pins", ()) or ()))
        for item in partitions
    )
    routing_anchor_net_count = sum(
        len(tuple(item.get("routing_anchor_nets", ()) or ()))
        for item in partitions
    )
    device_template_count = sum(
        len(tuple(item.get("device_template_plan", ()) or ()))
        for item in partitions
    )
    partition_verification_ready_count = sum(
        1
        for item in partitions
        if bool(dict(item.get("realization_readiness", {}) or {}).get("ready_for_partition_verification", False))
    )
    device_synthesis_ready_count = sum(
        1
        for item in partitions
        if bool(dict(item.get("realization_readiness", {}) or {}).get("ready_for_device_synthesis", False))
    )
    pdk_bindable_partition_count = sum(
        1
        for item in partitions
        if bool(dict(item.get("pdk_binding", {}) or {}).get("pcell_bindable", False))
    )
    pdk_implementation_ready_count = sum(
        1
        for item in partitions
        if bool(dict(item.get("pdk_binding", {}) or {}).get("ready_for_pdk_implementation", False))
    )
    implementation_carriers = tuple(
        sorted(
            {
                str(item.get("implementation_carrier", ""))
                for item in partitions
                if str(item.get("implementation_carrier", ""))
            }
        )
    )
    return {
        "present": True,
        "topology_name": str(lowering.get("topology_name", "")),
        "partition_count": len(partitions),
        "materialized_partition_count": len(materialized),
        "primitive_partition_count": primitive_count,
        "macro_partition_count": macro_count,
        "behavioral_stub_partition_count": stub_count,
        "pcell_backbone_partition_count": pcell_backbone_count,
        "macro_boundary_partition_count": macro_boundary_count,
        "behavioral_model_partition_count": behavioral_model_count,
        "restore_partition_count": restore_count,
        "required_external_net_count": required_external_net_count,
        "routing_anchor_net_count": routing_anchor_net_count,
        "exposed_pin_count": exposed_pin_count,
        "device_template_count": device_template_count,
        "partition_verification_ready_count": partition_verification_ready_count,
        "device_synthesis_ready_count": device_synthesis_ready_count,
        "pdk_bindable_partition_count": pdk_bindable_partition_count,
        "pdk_implementation_ready_count": pdk_implementation_ready_count,
        "materialized_partitions": materialized,
        "implementation_carriers": implementation_carriers,
        "summary": tuple(lowering.get("summary", ()) or ()),
    }


def _candidate_partition_realization_contract(
    hierarchy_context: Mapping[str, object] | None,
) -> dict[str, object]:
    plan = dict((hierarchy_context or {}).get("hierarchical_partition_realization_plan", {}) or {})
    partitions = tuple(
        dict(item)
        for item in tuple(plan.get("partitions", ()) or ())
        if isinstance(item, Mapping)
    )
    if not partitions:
        return {
            "present": False,
            "topology_name": str(plan.get("topology_name", "")),
            "partition_count": 0,
            "realization_ready_partition_count": 0,
            "pdk_checked_partition_count": 0,
            "pdk_implementation_ready_partition_count": 0,
            "needs_enclosing_route_context_count": 0,
            "blocked_partition_count": 0,
            "binding_blocked_partition_count": 0,
            "macro_bound_partition_count": 0,
            "architecture_budget_blocked_partition_count": 0,
            "max_blocking_issue_count": 0,
            "blocked_partitions": (),
            "binding_blocked_partitions": (),
            "macro_bound_partitions": (),
            "architecture_budget_blocked_partitions": (),
            "summary": tuple(plan.get("summary", ()) or ()),
        }
    blocked_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and not bool(item.get("realization_ready", False))
        )
    )
    binding_blocked_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("binding_blocked", False))
        )
    )
    macro_bound_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("macro_bound", False))
        )
    )
    architecture_budget_blocked_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("architecture_budget_blocked", False))
        )
    )
    return {
        "present": True,
        "topology_name": str(plan.get("topology_name", "")),
        "partition_count": len(partitions),
        "realization_ready_partition_count": sum(1 for item in partitions if bool(item.get("realization_ready", False))),
        "pdk_checked_partition_count": sum(1 for item in partitions if bool(item.get("pdk_checked", False))),
        "pdk_implementation_ready_partition_count": sum(1 for item in partitions if bool(item.get("pcell_bindable", False))),
        "needs_enclosing_route_context_count": sum(
            1 for item in partitions if bool(item.get("needs_enclosing_route_context", False))
        ),
        "blocked_partition_count": len(blocked_partitions),
        "binding_blocked_partition_count": len(binding_blocked_partitions),
        "macro_bound_partition_count": len(macro_bound_partitions),
        "architecture_budget_blocked_partition_count": len(architecture_budget_blocked_partitions),
        "max_blocking_issue_count": max(
            (int(item.get("blocking_issue_count", 0) or 0) for item in partitions),
            default=0,
        ),
        "blocked_partitions": blocked_partitions,
        "binding_blocked_partitions": binding_blocked_partitions,
        "macro_bound_partitions": macro_bound_partitions,
        "architecture_budget_blocked_partitions": architecture_budget_blocked_partitions,
        "summary": tuple(plan.get("summary", ()) or ()),
    }


def _candidate_partition_implementation_bundle_contract(
    hierarchy_context: Mapping[str, object] | None,
) -> dict[str, object]:
    plan = dict((hierarchy_context or {}).get("hierarchical_partition_implementation_bundle", {}) or {})
    partitions = tuple(
        dict(item)
        for item in tuple(plan.get("partitions", ()) or ())
        if isinstance(item, Mapping)
    )
    if not partitions:
        return {
            "present": False,
            "topology_name": str(plan.get("topology_name", "")),
            "partition_count": 0,
            "implementation_ready_partition_count": 0,
            "pcell_binding_ready_partition_count": 0,
            "pex_focus_partition_count": 0,
            "reference_sensitive_partition_count": 0,
            "feedback_sensitive_partition_count": 0,
            "keep_stable_partition_count": 0,
            "retarget_changed_partition_count": 0,
            "blocked_partition_count": 0,
            "binding_blocked_partition_count": 0,
            "macro_bound_partition_count": 0,
            "architecture_budget_blocked_partition_count": 0,
            "retarget_focus_score_total": 0,
            "retarget_focus_score_max": 0,
            "retarget_action_count": 0,
            "retarget_actions": (),
            "binding_blocked_partitions": (),
            "macro_bound_partitions": (),
            "architecture_budget_blocked_partitions": (),
            "summary": tuple(plan.get("summary", ()) or ()),
        }
    blocked = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and not bool(item.get("implementation_ready", False))
        )
    )
    retarget_insights = tuple(
        dict(item.get("retarget_insight", {}) or {})
        for item in partitions
        if isinstance(item.get("retarget_insight", {}), Mapping)
    )
    retarget_focus_scores = tuple(int(item.get("focus_score", 0) or 0) for item in retarget_insights)
    retarget_actions = tuple(
        dict.fromkeys(
            str(action)
            for item in retarget_insights
            for action in tuple(item.get("actions", ()) or ())
            if str(action)
        )
    )
    architecture_budgets = tuple(
        dict(item.get("architecture_budget", {}) or {})
        for item in partitions
        if isinstance(item.get("architecture_budget", {}), Mapping)
        and dict(item.get("architecture_budget", {}) or {})
    )
    binding_blocked_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("binding_blocked", False))
        )
    )
    macro_bound_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("macro_bound", False))
        )
    )
    architecture_budget_blocked_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("architecture_budget_blocked", False))
        )
    )
    return {
        "present": True,
        "topology_name": str(plan.get("topology_name", "")),
        "partition_count": len(partitions),
        "implementation_ready_partition_count": sum(1 for item in partitions if bool(item.get("implementation_ready", False))),
        "pcell_binding_ready_partition_count": sum(1 for item in partitions if bool(item.get("pcell_binding_ready", False))),
        "pex_focus_partition_count": sum(1 for item in partitions if bool(item.get("pex_focus_required", False))),
        "reference_sensitive_partition_count": sum(1 for item in partitions if tuple(item.get("reference_nets", ()) or ())),
        "feedback_sensitive_partition_count": sum(1 for item in partitions if tuple(item.get("feedback_nets", ()) or ())),
        "keep_stable_partition_count": sum(1 for item in partitions if bool(item.get("keep_stable", False))),
        "retarget_changed_partition_count": sum(1 for item in partitions if bool(item.get("retarget_changed", False))),
        "blocked_partition_count": len(blocked),
        "binding_blocked_partition_count": len(binding_blocked_partitions),
        "macro_bound_partition_count": len(macro_bound_partitions),
        "architecture_budget_blocked_partition_count": len(architecture_budget_blocked_partitions),
        "retarget_focus_score_total": sum(retarget_focus_scores),
        "retarget_focus_score_max": max(retarget_focus_scores) if retarget_focus_scores else 0,
        "retarget_action_count": len(retarget_actions),
        "retarget_actions": retarget_actions,
        "binding_blocked_partitions": binding_blocked_partitions,
        "macro_bound_partitions": macro_bound_partitions,
        "architecture_budget_blocked_partitions": architecture_budget_blocked_partitions,
        "architecture_budget_partition_count": len(architecture_budgets),
        "architecture_reference_critical_partition_count": sum(
            1 for item in architecture_budgets if str(item.get("sensitivity", item.get("sensitivity_class", ""))) == "reference_critical"
        ),
        "architecture_timing_critical_partition_count": sum(
            1 for item in architecture_budgets if str(item.get("sensitivity", item.get("sensitivity_class", ""))) == "timing_critical"
        ),
        "architecture_feedback_critical_partition_count": sum(
            1 for item in architecture_budgets if str(item.get("sensitivity", item.get("sensitivity_class", ""))) == "feedback_critical"
        ),
        "summary": tuple(plan.get("summary", ()) or ()),
    }


def _candidate_partition_pcell_binding_contract(
    hierarchy_context: Mapping[str, object] | None,
) -> dict[str, object]:
    plan = dict((hierarchy_context or {}).get("hierarchical_partition_pcell_binding_plan", {}) or {})
    partitions = tuple(
        dict(item)
        for item in tuple(plan.get("partitions", ()) or ())
        if isinstance(item, Mapping)
    )
    if not partitions:
        blocked_partitions = tuple(
            str(item)
            for item in tuple(plan.get("blocked_partitions", ()) or ())
            if str(item)
        )
        ready_partitions = tuple(
            str(item)
            for item in tuple(plan.get("ready_partitions", ()) or ())
            if str(item)
        )
        macro_binding_partitions = tuple(
            str(item)
            for item in tuple(plan.get("macro_binding_partitions", ()) or ())
            if str(item)
        )
        missing_pcell_logical_names = tuple(
            str(item)
            for item in tuple(plan.get("missing_pcell_logical_names", ()) or ())
            if str(item)
        )
        return {
            "present": bool(
                blocked_partitions
                or ready_partitions
                or macro_binding_partitions
                or missing_pcell_logical_names
                or int(plan.get("ready_for_pdk_implementation_partition_count", 0) or 0) > 0
            ),
            "topology_name": str(plan.get("topology_name", "")),
            "partition_count": 0,
            "binding_applicable_partition_count": 0,
            "binding_ready_partition_count": 0,
            "macro_binding_applicable_partition_count": 0,
            "macro_binding_ready_partition_count": 0,
            "pdk_checked_partition_count": 0,
            "missing_template_partition_count": 0,
            "missing_macro_binding_partition_count": 0,
            "parameter_issue_partition_count": 0,
            "max_instance_validation_issue_count": 0,
            "ready_for_pdk_implementation_partition_count": int(
                plan.get("ready_for_pdk_implementation_partition_count", len(ready_partitions)) or 0
            ),
            "ready_partitions": ready_partitions,
            "blocked_partitions": blocked_partitions,
            "blocked_partition_reasons": {},
            "missing_pcell_logical_names": missing_pcell_logical_names,
            "macro_binding_partitions": macro_binding_partitions,
            "macro_binding_cells": (),
            "pcell_binding_partitions": (),
            "summary": tuple(plan.get("summary", ()) or ()),
        }
    applicable = tuple(
        str(item.get("name", ""))
        for item in partitions
        if str(item.get("name", "")) and bool(item.get("pcell_binding_applicable", False))
    )
    ready = tuple(
        str(item.get("name", ""))
        for item in partitions
        if str(item.get("name", "")) and bool(item.get("pcell_binding_ready", False))
    )
    blocked = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", ""))
            and (
                (bool(item.get("pcell_binding_applicable", False)) and not bool(item.get("pcell_binding_ready", False)))
                or (bool(item.get("macro_binding_applicable", False)) and not bool(item.get("macro_binding_ready", False)))
            )
        )
    )
    ready_for_pdk_implementation = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("ready_for_pdk_implementation", False))
        )
    )
    blocked_partition_reasons = {
        str(item.get("name", "")): tuple(
            str(reason) for reason in tuple(item.get("blocking_reasons", ()) or ()) if str(reason)
        )
        for item in partitions
        if str(item.get("name", "")) and tuple(item.get("blocking_reasons", ()) or ())
    }
    missing_pcell_logical_names = tuple(
        dict.fromkeys(
            str(name)
            for item in partitions
            for name in tuple(item.get("missing_pcell_logical_names", ()) or ())
            if str(name)
        )
    )
    macro_binding_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("macro_binding_ready", False))
        )
    )
    macro_binding_cells = tuple(
        dict.fromkeys(
            str(dict(item.get("macro_binding", {}) or {}).get("cell_name", ""))
            for item in partitions
            if bool(item.get("macro_binding_ready", False))
            and str(dict(item.get("macro_binding", {}) or {}).get("cell_name", ""))
        )
    )
    pcell_binding_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("pcell_binding_ready", False))
        )
    )
    missing_template_partition_count = sum(
        1
        for item in partitions
        if any(
            not bool(instance.get("template_available", False))
            for instance in tuple(item.get("pcell_instances", ()) or ())
            if isinstance(instance, Mapping)
        )
    )
    parameter_issue_partition_count = sum(
        1
        for item in partitions
        if any(
            tuple(instance.get("validation_issues", ()) or ())
            for instance in tuple(item.get("pcell_instances", ()) or ())
            if isinstance(instance, Mapping)
        )
    )
    max_instance_validation_issue_count = max(
        (
            sum(
                len(tuple(instance.get("validation_issues", ()) or ()))
                for instance in tuple(item.get("pcell_instances", ()) or ())
                if isinstance(instance, Mapping)
            )
            for item in partitions
        ),
        default=0,
    )
    return {
        "present": True,
        "topology_name": str(plan.get("topology_name", "")),
        "partition_count": len(partitions),
        "binding_applicable_partition_count": len(applicable),
        "binding_ready_partition_count": len(ready),
        "macro_binding_applicable_partition_count": sum(1 for item in partitions if bool(item.get("macro_binding_applicable", False))),
        "macro_binding_ready_partition_count": sum(1 for item in partitions if bool(item.get("macro_binding_ready", False))),
        "pdk_checked_partition_count": sum(1 for item in partitions if bool(item.get("pdk_checked", False))),
        "missing_template_partition_count": missing_template_partition_count,
        "missing_macro_binding_partition_count": sum(
            1
            for item in partitions
            if bool(item.get("macro_binding_applicable", False)) and not bool(item.get("macro_binding_ready", False))
        ),
        "parameter_issue_partition_count": parameter_issue_partition_count,
        "max_instance_validation_issue_count": max_instance_validation_issue_count,
        "ready_for_pdk_implementation_partition_count": len(ready_for_pdk_implementation),
        "ready_partitions": ready,
        "blocked_partitions": blocked,
        "blocked_partition_reasons": blocked_partition_reasons,
        "missing_pcell_logical_names": missing_pcell_logical_names,
        "macro_binding_partitions": macro_binding_partitions,
        "macro_binding_cells": macro_binding_cells,
        "pcell_binding_partitions": pcell_binding_partitions,
        "summary": tuple(plan.get("summary", ()) or ()),
    }


def _candidate_partition_parasitic_target_contract(
    hierarchy_context: Mapping[str, object] | None,
) -> dict[str, object]:
    plan = dict((hierarchy_context or {}).get("hierarchical_partition_parasitic_target_plan", {}) or {})
    partitions = tuple(
        dict(item)
        for item in tuple(plan.get("partitions", ()) or ())
        if isinstance(item, Mapping)
    )
    if not partitions:
        return {
            "present": False,
            "topology_name": str(plan.get("topology_name", "")),
            "partition_count": 0,
            "pex_focus_partition_count": 0,
            "reference_sensitive_partition_count": 0,
            "feedback_sensitive_partition_count": 0,
            "critical_net_count": 0,
            "max_target_cap_budget_f": 0.0,
            "max_target_res_budget_ohm": 0.0,
            "binding_blocked_partition_count": 0,
            "macro_bound_partition_count": 0,
            "architecture_budget_blocked_partition_count": 0,
            "binding_blocked_partitions": (),
            "macro_bound_partitions": (),
            "architecture_budget_blocked_partitions": (),
            "architecture_budget_partition_count": 0,
            "architecture_reference_critical_partition_count": 0,
            "architecture_timing_critical_partition_count": 0,
            "architecture_feedback_critical_partition_count": 0,
            "summary": tuple(plan.get("summary", ()) or ()),
        }
    architecture_budgets = tuple(
        dict(item.get("architecture_budget", {}) or {})
        for item in partitions
        if isinstance(item.get("architecture_budget", {}), Mapping)
        and dict(item.get("architecture_budget", {}) or {})
    )
    binding_blocked_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("binding_blocked", False))
        )
    )
    macro_bound_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("macro_bound", False))
        )
    )
    architecture_budget_blocked_partitions = tuple(
        sorted(
            str(item.get("name", ""))
            for item in partitions
            if str(item.get("name", "")) and bool(item.get("architecture_budget_blocked", False))
        )
    )
    return {
        "present": True,
        "topology_name": str(plan.get("topology_name", "")),
        "partitions": partitions,
        "partition_count": len(partitions),
        "pex_focus_partition_count": sum(1 for item in partitions if bool(item.get("pex_focus_required", False))),
        "reference_sensitive_partition_count": sum(1 for item in partitions if tuple(item.get("reference_nets", ()) or ())),
        "feedback_sensitive_partition_count": sum(1 for item in partitions if tuple(item.get("feedback_nets", ()) or ())),
        "critical_net_count": sum(len(tuple(item.get("critical_nets", ()) or ())) for item in partitions),
        "max_target_cap_budget_f": max((float(item.get("target_cap_budget_f", 0.0) or 0.0) for item in partitions), default=0.0),
        "max_target_res_budget_ohm": max((float(item.get("target_res_budget_ohm", 0.0) or 0.0) for item in partitions), default=0.0),
        "binding_blocked_partition_count": len(binding_blocked_partitions),
        "macro_bound_partition_count": len(macro_bound_partitions),
        "architecture_budget_blocked_partition_count": len(architecture_budget_blocked_partitions),
        "binding_blocked_partitions": binding_blocked_partitions,
        "macro_bound_partitions": macro_bound_partitions,
        "architecture_budget_blocked_partitions": architecture_budget_blocked_partitions,
        "architecture_budget_partition_count": len(architecture_budgets),
        "architecture_reference_critical_partition_count": sum(
            1 for item in architecture_budgets if str(item.get("sensitivity", item.get("sensitivity_class", ""))) == "reference_critical"
        ),
        "architecture_timing_critical_partition_count": sum(
            1 for item in architecture_budgets if str(item.get("sensitivity", item.get("sensitivity_class", ""))) == "timing_critical"
        ),
        "architecture_feedback_critical_partition_count": sum(
            1 for item in architecture_budgets if str(item.get("sensitivity", item.get("sensitivity_class", ""))) == "feedback_critical"
        ),
        "summary": tuple(plan.get("summary", ()) or ()),
    }


def _physical_hierarchy_lowering_contract(
    hierarchy_context: Mapping[str, object] | None,
) -> dict[str, object]:
    lowering = dict((hierarchy_context or {}).get("hierarchical_implementation_lowering", {}) or {})
    partitions = tuple(
        dict(item)
        for item in tuple(lowering.get("partitions", ()) or ())
        if isinstance(item, Mapping)
    )
    if not partitions:
        return {
            "present": False,
            "topology_name": str(lowering.get("topology_name", "")),
            "missing_pcell_bindings": (),
            "routing_anchor_nets": (),
            "enclosing_context_partitions": (),
        }
    missing_pcell_bindings = tuple(
        sorted(
            {
                str(item.get("name", ""))
                for item in partitions
                if str(item.get("name", ""))
                and tuple(dict(item.get("pdk_binding", {}) or {}).get("missing_pcell_logical_names", ()) or ())
            }
        )
    )
    routing_anchor_nets = tuple(
        sorted(
            {
                str(net)
                for item in partitions
                for net in tuple(item.get("routing_anchor_nets", ()) or ())
                if str(net)
            }
        )
    )
    enclosing_context_partitions = tuple(
        sorted(
            {
                str(item.get("name", ""))
                for item in partitions
                if str(item.get("name", ""))
                and bool(dict(item.get("realization_readiness", {}) or {}).get("needs_enclosing_route_context", False))
            }
        )
    )
    return {
        "present": True,
        "topology_name": str(lowering.get("topology_name", "")),
        "missing_pcell_bindings": missing_pcell_bindings,
        "routing_anchor_nets": routing_anchor_nets,
        "enclosing_context_partitions": enclosing_context_partitions,
    }


def _candidate_verification_intent_contract(
    hierarchy_context: Mapping[str, object] | None,
) -> dict[str, object]:
    verification = dict((hierarchy_context or {}).get("hierarchical_verification_intent", {}) or {})
    stages = tuple(
        dict(item)
        for item in tuple(verification.get("stages", ()) or ())
        if isinstance(item, Mapping)
    )
    if not stages:
        return {
            "present": False,
            "topology_name": str(verification.get("topology_name", "")),
            "stage_count": 0,
            "materialized_stage_count": 0,
            "lowering_ready_stage_count": 0,
            "reference_sensitive_stage_count": 0,
            "timing_sensitive_stage_count": 0,
            "restore_sensitive_stage_count": 0,
            "required_net_count": 0,
            "exposed_pin_count": 0,
            "verification_views": (),
            "verification_focuses": (),
            "summary": tuple(verification.get("summary", ()) or ()),
        }
    verification_views = tuple(
        dict.fromkeys(str(item.get("verification_view", "")) for item in stages if str(item.get("verification_view", "")))
    )
    verification_focuses = tuple(
        dict.fromkeys(str(item.get("verification_focus", "")) for item in stages if str(item.get("verification_focus", "")))
    )
    required_net_count = sum(len(tuple(item.get("required_nets", ()) or ())) for item in stages)
    exposed_pin_count = sum(len(tuple(item.get("exposed_pins", ()) or ())) for item in stages)
    return {
        "present": True,
        "topology_name": str(verification.get("topology_name", "")),
        "stage_count": len(stages),
        "materialized_stage_count": sum(1 for item in stages if bool(item.get("materialized", False))),
        "lowering_ready_stage_count": sum(1 for item in stages if bool(item.get("lowering_ready", False))),
        "reference_sensitive_stage_count": sum(1 for item in stages if bool(item.get("reference_sensitive", False))),
        "timing_sensitive_stage_count": sum(1 for item in stages if bool(item.get("timing_sensitive", False))),
        "restore_sensitive_stage_count": sum(1 for item in stages if bool(item.get("restore_sensitive", False))),
        "required_net_count": required_net_count,
        "exposed_pin_count": exposed_pin_count,
        "verification_views": verification_views,
        "verification_focuses": verification_focuses,
        "summary": tuple(verification.get("summary", ()) or ()),
    }


def _hotspot_nets(hotspot_evidence: Any | Iterable[Any]) -> set[str]:
    if hasattr(hotspot_evidence, "deltas"):
        return {str(delta.net) for delta in tuple(getattr(hotspot_evidence, "deltas", ())) if tuple(getattr(delta, "issues", ()))}
    items = tuple(hotspot_evidence if isinstance(hotspot_evidence, Iterable) and not isinstance(hotspot_evidence, (str, bytes)) else (hotspot_evidence,))
    return {str(getattr(item, "net", "")) for item in items if str(getattr(item, "net", "")) and tuple(getattr(item, "issues", ()))}


def _min_route_width_um(pdk: PdkConfig, layer: str, fallback_um: float) -> float:
    try:
        return pdk.rules.min_width_um(layer)
    except KeyError:
        return fallback_um


def _pdk_physical_contract(pdk: PdkConfig) -> dict[str, object]:
    return {
        "name": pdk.name,
        "grid_um": pdk.rules.grid_step_um,
        "analog_placement_constraints": {
            "match_tolerance_um": pdk.analog_placement_constraints.match_tolerance_um,
            "symmetry_tolerance_um": pdk.analog_placement_constraints.symmetry_tolerance_um,
            "row_alignment_tolerance_um": pdk.analog_placement_constraints.row_alignment_tolerance_um,
            "partition_order_tolerance_um": pdk.analog_placement_constraints.partition_order_tolerance_um,
            "focus_separation_target_um": pdk.analog_placement_constraints.focus_separation_target_um,
            "anchor_spread_target_um": pdk.analog_placement_constraints.anchor_spread_target_um,
        },
        "analog_routing_constraints": {
            "length_match_tolerance_um": pdk.analog_routing_constraints.length_match_tolerance_um,
            "current_derate": pdk.analog_routing_constraints.current_derate,
            "via_current_derate": pdk.analog_routing_constraints.via_current_derate,
            "preferred_power_penalty": pdk.analog_routing_constraints.preferred_power_penalty,
            "preferred_signal_penalty": pdk.analog_routing_constraints.preferred_signal_penalty,
            "bus_order_penalty": pdk.analog_routing_constraints.bus_order_penalty,
            "matched_route_penalty": pdk.analog_routing_constraints.matched_route_penalty,
            "antenna_penalty": pdk.analog_routing_constraints.antenna_penalty,
            "min_area_penalty": pdk.analog_routing_constraints.min_area_penalty,
        },
        "routing_layers": tuple(
            {
                "name": layer,
                "direction": rule.direction,
                "preferred": rule.preferred,
                "role": rule.role,
                "track_pitch_nm": rule.track_pitch_nm,
                "track_offset_nm": rule.track_offset_nm,
                "max_current_ma": rule.max_current_ma,
            }
            for layer, rule in sorted(pdk.routing_layers.items())
        ),
        "via_stack": tuple(
            {
                "via_def": via.via_def,
                "lower_layer": via.lower_layer,
                "upper_layer": via.upper_layer,
                "default_rows": via.default_rows,
                "default_cols": via.default_cols,
                "max_rows": via.max_rows,
                "max_cols": via.max_cols,
                "max_current_ma_per_cut": via.max_current_ma_per_cut,
            }
            for via in pdk.via_stack
        ),
        "placement_site": {
            "device_pitch_um": pdk.placement_site.device_pitch_um,
            "row_pitch_um": pdk.placement_site.row_pitch_um,
            "common_centroid_pitch_um": pdk.placement_site.common_centroid_pitch_um,
            "interdigitated_pitch_um": pdk.placement_site.interdigitated_pitch_um,
            "symmetry_axis": pdk.placement_site.symmetry_axis,
            "row_policy": pdk.placement_site.row_policy,
            "role_orient_policy": {
                str(role): tuple(str(orient) for orient in values)
                for role, values in sorted(pdk.placement_site.role_orient_policy.items())
            },
            "role_row_policy": dict(sorted((str(role), str(policy)) for role, policy in pdk.placement_site.role_row_policy.items())),
        },
        "preferred_signal_layers": tuple(str(layer) for layer in pdk.preferred_signal_layers),
        "preferred_power_layers": tuple(str(layer) for layer in pdk.preferred_power_layers),
        "extraction_corners": tuple(
            {
                "name": name,
                "cap_scale": corner.cap_scale,
                "res_scale": corner.res_scale,
                "temperature_c": corner.temperature_c,
            }
            for name, corner in sorted(pdk.extraction_corners.items())
        ),
    }


def _placement_contract(
    placement: Sequence[Any],
    report: Mapping[str, Any],
    constraints: LayoutConstraintSet | None = None,
) -> dict[str, object]:
    role_counts: dict[str, int] = {}
    for item in placement:
        role = str(getattr(item, "role", "") or getattr(item, "name", ""))
        role_counts[role] = role_counts.get(role, 0) + 1
    constraint_contract = _placement_constraint_contract(placement, report, constraints or LayoutConstraintSet())
    legality_contract = _placement_legality_contract(report)
    return {
        "passed": bool(report.get("passed", False)),
        "placement_count": int(report.get("count", len(tuple(placement)))),
        "role_counts": dict(sorted(role_counts.items())),
        "constraint_contract": constraint_contract,
        "legality_contract": legality_contract,
        "analog_profile": dict(report.get("analog_profile", {}) or {}),
        "issues": tuple(str(issue) for issue in report.get("issues", ())),
    }


def _routing_contract(report: Mapping[str, Any]) -> dict[str, object]:
    routing_policy = report.get("routing_policy", {})
    bus_order = report.get("bus_order", {})
    antenna = report.get("antenna", {})
    min_area = report.get("min_area", {})
    physical_connectivity = report.get("physical_connectivity", {})
    legality_contract = _routing_legality_contract(report)
    return {
        "passed": bool(report.get("passed", False)),
        "path_count_by_net": dict(sorted((str(net), int(count)) for net, count in dict(report.get("path_count_by_net", {})).items())),
        "via_count_by_net": dict(sorted((str(net), int(count)) for net, count in dict(report.get("via_count_by_net", {})).items())),
        "route_layers_by_net": {
            str(net): tuple(str(layer) for layer in layers)
            for net, layers in sorted(dict(report.get("route_layers_by_net", {})).items())
        },
        "lengths_um": dict(sorted((str(net), float(length)) for net, length in dict(report.get("lengths_um", {})).items())),
        "legality_contract": legality_contract,
        "issues": tuple(str(issue) for issue in report.get("issues", ())),
        "routing_policy_issues": tuple(str(issue) for issue in dict(routing_policy).get("issues", ())),
        "bus_order_issues": tuple(str(issue) for issue in dict(bus_order).get("issues", ())),
        "antenna_issues": tuple(str(issue) for issue in dict(antenna).get("issues", ())),
        "min_area_issues": tuple(str(issue) for issue in dict(min_area).get("issues", ())),
        "physical_connectivity_issues": tuple(str(issue) for issue in dict(physical_connectivity).get("issues", ())),
    }


def _placement_legality_contract(report: Mapping[str, Any]) -> dict[str, object]:
    issues = tuple(str(issue) for issue in report.get("issues", ()))
    row_policy_issues = tuple(issue for issue in issues if "row policy" in issue)
    orient_policy_issues = tuple(issue for issue in issues if "orientation policy" in issue or "orient policy" in issue)
    return {
        "row_policy_passed": not row_policy_issues,
        "row_policy_issue_count": len(row_policy_issues),
        "orientation_policy_passed": not orient_policy_issues,
        "orientation_policy_issue_count": len(orient_policy_issues),
    }


def _routing_legality_contract(report: Mapping[str, Any]) -> dict[str, object]:
    issues = tuple(str(issue) for issue in report.get("issues", ()))
    routing_policy = dict(report.get("routing_policy", {}) or {})
    bus_order = dict(report.get("bus_order", {}) or {})
    current_capacity_issues = tuple(issue for issue in issues if "current " in issue and " exceeds layer " in issue)
    via_capacity_issues = tuple(issue for issue in issues if "via capacity " in issue)
    preferred_layer_role_issues = tuple(
        issue for issue in tuple(str(issue) for issue in routing_policy.get("issues", ()))
        if "route_layer requires" in issue
    )
    bus_order_issues = tuple(str(issue) for issue in bus_order.get("issues", ()))
    return {
        "current_capacity_passed": not current_capacity_issues,
        "current_capacity_issue_count": len(current_capacity_issues),
        "via_capacity_passed": not via_capacity_issues,
        "via_capacity_issue_count": len(via_capacity_issues),
        "preferred_layer_role_passed": not preferred_layer_role_issues,
        "preferred_layer_role_issue_count": len(preferred_layer_role_issues),
        "bus_order_passed": not bus_order_issues,
        "bus_order_issue_count": len(bus_order_issues),
    }


def _system_contract_summary(system_contract: Mapping[str, object] | None) -> dict[str, object]:
    contract = dict(system_contract or {})
    bus_contracts = tuple(dict(item) for item in contract.get("bus_contracts", ()) if isinstance(item, Mapping))
    reference_paths = tuple(dict(item) for item in contract.get("reference_paths", ()) if isinstance(item, Mapping))
    feedback_contracts = tuple(dict(item) for item in contract.get("feedback_contracts", ()) if isinstance(item, Mapping))
    timing_chains = tuple(dict(item) for item in contract.get("timing_chains", ()) if isinstance(item, Mapping))
    interface_contracts = tuple(dict(item) for item in contract.get("interface_contracts", ()) if isinstance(item, Mapping))
    return {
        "topology_name": str(contract.get("topology_name", "")),
        "bus_contract_count": len(bus_contracts),
        "reference_path_count": len(reference_paths),
        "feedback_contract_count": len(feedback_contracts),
        "timing_chain_count": len(timing_chains),
        "restore_bus_required_count": sum(1 for item in bus_contracts if bool(item.get("restore_required", False))),
        "restore_feedback_required_count": sum(1 for item in feedback_contracts if bool(item.get("restore_required", False))),
        "preserve_reference_path_count": sum(1 for item in reference_paths if bool(item.get("preserve_integrity", False))),
        "preserve_timing_chain_count": sum(1 for item in timing_chains if bool(item.get("preserve_order", False))),
        "interface_contracts": interface_contracts,
        "bus_contracts": bus_contracts,
        "reference_paths": reference_paths,
        "feedback_contracts": feedback_contracts,
        "timing_chains": timing_chains,
        "summary": tuple(str(item) for item in contract.get("summary", ()) if str(item)),
        "provenance": dict(contract.get("provenance", {}) or {}),
    }


def suggest_physical_legality_ecos(
    physical_contract: Mapping[str, object],
    *,
    solver_guide: "AnalogSolverGuide | Mapping[str, object] | None" = None,
) -> tuple[PhysicalLegalitySuggestion, ...]:
    placement = dict(physical_contract.get("placement", {}) or {})
    routing = dict(physical_contract.get("routing", {}) or {})
    hierarchy_lowering = dict(physical_contract.get("hierarchy_lowering", {}) or {})
    hierarchy_parasitics = dict(physical_contract.get("hierarchy_parasitics", {}) or {})
    readiness = dict(physical_contract.get("readiness", {}) or {})
    agent_contract = _solver_guide_agent_contract_payload(solver_guide)
    placement_legality = dict(placement.get("legality_contract", {}) or {})
    routing_legality = dict(routing.get("legality_contract", {}) or {})
    placement_issues = tuple(str(issue) for issue in placement.get("issues", ()))
    routing_issues = tuple(str(issue) for issue in routing.get("issues", ()))
    suggestions: list[PhysicalLegalitySuggestion] = []
    if int(placement_legality.get("row_policy_issue_count", 0)) > 0:
        suggestions.append(
            PhysicalLegalitySuggestion(
                "enforce_role_row_policy",
                domain="placement",
                reason="placement violates PDK role-row policy",
                priority=10,
                params={"issues": tuple(issue for issue in placement_issues if "row policy" in issue)},
            )
        )
    if int(placement_legality.get("orientation_policy_issue_count", 0)) > 0:
        suggestions.append(
            PhysicalLegalitySuggestion(
                "enforce_role_orientation_policy",
                domain="placement",
                reason="placement violates PDK orientation policy",
                priority=9,
                params={"issues": tuple(issue for issue in placement_issues if "orientation policy" in issue or "orient policy" in issue)},
            )
        )
    if int(routing_legality.get("current_capacity_issue_count", 0)) > 0:
        suggestions.append(
            PhysicalLegalitySuggestion(
                "widen_or_relayer_current_limited_routes",
                domain="routing",
                reason="route current exceeds layer capacity",
                priority=10,
                params={"issues": tuple(issue for issue in routing_issues if "current " in issue and " exceeds layer " in issue)},
            )
        )
    if int(routing_legality.get("via_capacity_issue_count", 0)) > 0:
        suggestions.append(
            PhysicalLegalitySuggestion(
                "increase_via_array_capacity",
                domain="routing",
                reason="via array capacity is below required current",
                priority=10,
                params={"issues": tuple(issue for issue in routing_issues if "via capacity " in issue)},
            )
        )
    if int(routing_legality.get("preferred_layer_role_issue_count", 0)) > 0:
        suggestions.append(
            PhysicalLegalitySuggestion(
                "move_net_to_preferred_layer_role",
                domain="routing",
                reason="routing violates required/preferred layer role policy",
                priority=7,
                params={"issues": tuple(issue for issue in routing.get("routing_policy_issues", ()))},
            )
        )
    if int(routing_legality.get("bus_order_issue_count", 0)) > 0:
        suggestions.append(
            PhysicalLegalitySuggestion(
                "reroute_bus_to_restore_order",
                domain="routing",
                reason="bus routing order is inconsistent with constraint",
                priority=8,
                params={"issues": tuple(issue for issue in routing.get("bus_order_issues", ()))},
            )
        )
    missing_pcell_bindings = tuple(str(name) for name in tuple(hierarchy_lowering.get("missing_pcell_bindings", ())) if str(name))
    if missing_pcell_bindings:
        suggestions.append(
            PhysicalLegalitySuggestion(
                "add_missing_pcell_templates_for_lowered_partitions",
                domain="pdk",
                reason="lowered primitive partitions cannot bind to current PDK PCell templates",
                priority=10,
                params={"partitions": missing_pcell_bindings},
            )
        )
    anchor_nets = tuple(str(net) for net in tuple(hierarchy_lowering.get("routing_anchor_nets", ())) if str(net))
    if anchor_nets and int(routing_legality.get("bus_order_issue_count", 0)) > 0:
        suggestions.append(
            PhysicalLegalitySuggestion(
                "protect_hierarchical_anchor_nets_during_reroute",
                domain="routing",
                reason="hierarchical lowering declares anchor nets that should remain stable during reroute",
                priority=8,
                params={"anchor_nets": anchor_nets},
            )
        )
    architecture_critical_nets = tuple(
        dict.fromkeys(
            str(net)
            for partition in tuple(hierarchy_parasitics.get("partitions", ()) or ())
            if isinstance(partition, Mapping)
            and str(
                dict(partition.get("architecture_budget", {}) or {}).get(
                    "sensitivity",
                    dict(partition.get("architecture_budget", {}) or {}).get("sensitivity_class", ""),
                )
                or ""
            ) in {"reference_critical", "timing_critical", "feedback_critical"}
            for net in (
                tuple(partition.get("critical_nets", ()) or ())
                + tuple(partition.get("reference_nets", ()) or ())
                + tuple(partition.get("feedback_nets", ()) or ())
                + tuple(partition.get("routing_anchor_nets", ()) or ())
            )
            if str(net)
        )
    )
    if architecture_critical_nets and tuple(routing_issues):
        suggestions.append(
            PhysicalLegalitySuggestion(
                "protect_architecture_critical_nets_during_reroute",
                domain="routing",
                reason="architecture-critical nets should remain protected during reroute and ECO repair",
                priority=9,
                params={"nets": architecture_critical_nets},
            )
        )
    forbidden_actions = tuple(
        str(item.get("name", ""))
        for item in tuple(agent_contract.get("forbidden_actions", ()) or ())
        if isinstance(item, Mapping) and str(item.get("name", ""))
    )
    if forbidden_actions:
        suggestions.append(
            PhysicalLegalitySuggestion(
                "honor_agent_forbidden_action_contract",
                domain="planning",
                reason="template agent contract forbids unconstrained freeform layout or routing behavior",
                priority=6,
                params={"forbidden_actions": forbidden_actions},
            )
        )
    review_checks = tuple(
        str(item.get("name", ""))
        for item in tuple(agent_contract.get("review_checklist", ()) or ())
        if isinstance(item, Mapping) and str(item.get("name", ""))
    )
    if review_checks and (placement_issues or routing_issues or not bool(readiness.get("ready_for_extraction", True))):
        suggestions.append(
            PhysicalLegalitySuggestion(
                "run_dsl_review_checklist",
                domain="verification",
                reason="template agent contract requires explicit review checks before closure acceptance",
                priority=5,
                params={"review_checks": review_checks},
            )
        )
    fallback_actions = tuple(
        {
            "trigger": str(item.get("trigger", "")),
            "action": str(item.get("action", "")),
        }
        for item in tuple(agent_contract.get("fallback_actions", ()) or ())
        if isinstance(item, Mapping) and str(item.get("trigger", "")) and str(item.get("action", ""))
    )
    if fallback_actions and (placement_issues or routing_issues or not bool(readiness.get("ready_for_extraction", True))):
        suggestions.append(
            PhysicalLegalitySuggestion(
                "invoke_dsl_fallback_action",
                domain="planning",
                reason="template agent contract defines fallback actions for stalled physical closure",
                priority=6,
                params={"fallback_actions": fallback_actions},
            )
        )
    return tuple(sorted(suggestions, key=lambda item: (-item.priority, item.domain, item.action)))


def _placement_constraint_contract(
    placement: Sequence[Any],
    report: Mapping[str, Any],
    constraints: LayoutConstraintSet,
) -> dict[str, object]:
    issues = tuple(str(issue) for issue in report.get("issues", ()))
    matched_groups = []
    symmetry_groups = []
    for group in constraints.matched_groups:
        name = str(group.name)
        group_issues = tuple(
            issue for issue in issues
            if f"matched group {name} " in issue
        )
        matched_groups.append(
            {
                "name": name,
                "style": str(group.style),
                "devices": tuple(str(device) for device in group.devices),
                "require_dummies": bool(group.require_dummies),
                "passed": not group_issues,
                "issues": group_issues,
            }
        )
    for pair in constraints.symmetry_groups:
        label = tuple(str(name) for name in pair)
        group_issues = _symmetry_group_issues(tuple(placement), label)
        symmetry_groups.append(
            {
                "devices": label,
                "passed": not group_issues,
                "issues": group_issues,
            }
        )
    return {
        "matched_group_count": len(tuple(constraints.matched_groups)),
        "symmetry_group_count": len(tuple(constraints.symmetry_groups)),
        "matched_groups_passed": all(group["passed"] for group in matched_groups),
        "symmetry_groups_passed": all(group["passed"] for group in symmetry_groups),
        "matched_groups": tuple(matched_groups),
        "symmetry_groups": tuple(symmetry_groups),
    }


def _symmetry_group_issues(
    placement: Sequence[Any],
    devices: tuple[str, ...],
) -> tuple[str, ...]:
    if len(devices) != 2:
        return ()
    by_role = _placement_items_by_role(placement)
    left = tuple(by_role.get(devices[0], ()))
    right = tuple(by_role.get(devices[1], ()))
    if not left or not right:
        return ()
    issues: list[str] = []
    if len(left) != len(right):
        issues.append(f"symmetry group {devices[0]}/{devices[1]} unit-count mismatch")
        return tuple(issues)
    left_y = sum(float(getattr(item, "y_um", 0.0)) for item in left) / len(left)
    right_y = sum(float(getattr(item, "y_um", 0.0)) for item in right) / len(right)
    if abs(left_y - right_y) > 1e-6:
        issues.append(f"symmetry group {devices[0]}/{devices[1]} y-mismatch")
    axis = 0.5 * (
        (sum(float(getattr(item, "x_um", 0.0)) for item in left) / len(left))
        + (sum(float(getattr(item, "x_um", 0.0)) for item in right) / len(right))
    )
    left_offsets = sorted(abs(float(getattr(item, "x_um", 0.0)) - axis) for item in left)
    right_offsets = sorted(abs(float(getattr(item, "x_um", 0.0)) - axis) for item in right)
    if any(abs(a - b) > 1e-6 for a, b in zip(left_offsets, right_offsets)):
        issues.append(f"symmetry group {devices[0]}/{devices[1]} mirrored-offset mismatch")
    return tuple(issues)


def _placement_items_by_role(placement: Sequence[Any]) -> dict[str, tuple[Any, ...]]:
    by_role: dict[str, list[Any]] = {}
    for item in placement:
        role = str(getattr(item, "role", "") or getattr(item, "name", ""))
        name = str(getattr(item, "name", ""))
        if role:
            by_role.setdefault(role, []).append(item)
        if name:
            by_role.setdefault(name, []).append(item)
    return {key: tuple(values) for key, values in by_role.items()}


def _next_placement_candidate(
    ranked: Sequence[Any],
    *,
    current: Sequence[Any],
) -> tuple[Any, ...] | None:
    current_tuple = tuple(current)
    for candidate in ranked:
        placements = tuple(getattr(candidate, "placements", ()))
        if placements != current_tuple:
            return placements
    return None


def _placement_rank_for_candidate(ranked: Sequence[Any], placements: Sequence[Any]) -> int:
    target = tuple(placements)
    for idx, candidate in enumerate(ranked):
        if tuple(getattr(candidate, "placements", ())) == target:
            return idx
    return -1


def _legalize_placement_candidate(
    placements: tuple[Any, ...],
    constraints: LayoutConstraintSet,
    pdk: PdkConfig,
    *,
    graph: Any | None = None,
    placement_seed_metadata: Mapping[str, object] | None = None,
) -> tuple[tuple[Any, ...], tuple[str, ...]]:
    from .placement import analyze_placement
    from .placement import _apply_role_orient_policy, _apply_role_row_policy, _device_role_map
    from .placement import tune_placement

    if _is_specialized_analog_backbone_placement(tuple(placements)):
        report = analyze_placement(tuple(placements), constraints, pdk=pdk, graph=graph)
        if not tuple(str(item) for item in report.get("issues", ())):
            return tuple(placements), ("preserve_specialized_analog_backbone_placement",)

    actions: list[str] = []
    device_role_map = _device_role_map(graph)
    legalized = tuple(placements)
    report = analyze_placement(legalized, constraints, pdk=pdk, graph=graph)
    issues = tuple(str(item) for item in report.get("issues", ()))
    if any("orientation policy" in issue or "orient policy" in issue for issue in issues):
        updated = _apply_role_orient_policy(legalized, pdk, device_role_map=device_role_map)
        if updated != legalized:
            legalized = updated
            actions.append("enforce_role_orientation_policy")
    report = analyze_placement(legalized, constraints, pdk=pdk, graph=graph)
    issues = tuple(str(item) for item in report.get("issues", ()))
    if any("row policy" in issue for issue in issues):
        updated = _apply_role_row_policy(legalized, pdk, device_role_map=device_role_map)
        if updated != legalized:
            legalized = updated
            actions.append("enforce_role_row_policy")
    updated = tune_placement(legalized, constraints)
    if updated != legalized:
        legalized = updated
        actions.append("retune_matched_and_centroid_placement")
    updated, hierarchy_actions = _retune_placement_hierarchy_guidance(
        legalized,
        placement_seed_metadata=placement_seed_metadata,
    )
    if updated != legalized:
        legalized = updated
    actions.extend(hierarchy_actions)
    return legalized, tuple(actions)


def _legalize_route_candidate(
    plan: Any,
    constraints: LayoutConstraintSet,
    pdk: PdkConfig,
    *,
    graph: Any | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
    include_open_checks: bool = False,
    include_via_landing_short_checks: bool = False,
    require_min_area_checks: bool = False,
    route_min_area_um2_by_layer: Mapping[str, float] | None = None,
    require_inline_verification: bool = False,
    inline_repair_max_iterations: int = 8,
    return_after_seed_inline_repair: bool = False,
) -> tuple[Any, tuple[str, ...]]:
    from .ir import LayoutPath, LayoutPlan, LayoutVia
    from .routing import analyze_interconnect_plan

    if not isinstance(plan, LayoutPlan):
        return plan, ()
    plan, duplicate_via_actions = _dedupe_close_same_net_vias(plan, pdk)
    if _is_specialized_analog_backbone(plan):
        current_report = analyze_interconnect_plan(plan, constraints, pdk)
        if not tuple(str(item) for item in current_report.get("issues", ())) and not require_inline_verification:
            return replace(
                plan,
                metadata={
                    **dict(plan.metadata),
                    "legalization_actions": ("preserve_specialized_analog_backbone",),
                    "specialized_route_report": current_report,
                },
            ), ("preserve_specialized_analog_backbone",)
    actions: list[str] = list(duplicate_via_actions)
    effective_constraints, hierarchy_actions = _route_constraints_with_hierarchy_guidance(
        constraints,
        plan,
        hierarchy_context=hierarchy_context,
    )
    actions.extend(hierarchy_actions)
    current_report = analyze_interconnect_plan(plan, effective_constraints, pdk)
    issue_text = tuple(str(item) for item in current_report.get("issues", ()))
    if not issue_text and not require_inline_verification:
        if not hierarchy_actions:
            return plan, ()
        return replace(
            plan,
            metadata={
                **dict(getattr(plan, "metadata", {})),
                "legalization_actions": tuple(actions),
            },
        ), tuple(actions)

    inline_rules = (
        _inline_rule_tables_from_pdk(
            pdk,
            min_area_um2_by_layer=route_min_area_um2_by_layer if require_min_area_checks else None,
        )
        if require_inline_verification
        else {}
    )
    seed_repaired_plan = None
    seed_repair_actions: tuple[str, ...] = ()
    if require_inline_verification:
        seed_repair = run_physical_drc_repair_loop(
            plan,
            constraints=effective_constraints,
            pdk=pdk,
            max_iterations=max(1, int(inline_repair_max_iterations)),
            min_width_um_by_layer=dict(inline_rules.get("min_width_um_by_layer", {}) or {}),
            min_spacing_um_by_layer=dict(inline_rules.get("min_spacing_um_by_layer", {}) or {}),
            min_area_um2_by_layer=dict(inline_rules.get("min_area_um2_by_layer", {}) or {}),
            require_all_via_landings=True,
            include_via_landing_short_checks=True if include_via_landing_short_checks or require_inline_verification else False,
        )
        if seed_repair.plan != plan:
            seed_repaired_plan = seed_repair.plan
            seed_repair_actions = (
                f"seed_inline_drc_repair:{len(tuple(seed_repair.iterations))}",
                *(() if seed_repair.passed else ("seed_inline_drc_repair_remaining",)),
            )
        if return_after_seed_inline_repair:
            if seed_repaired_plan is None:
                if not actions:
                    return plan, ("bounded_seed_inline_repair_no_change",)
                return plan, tuple((*actions, "bounded_seed_inline_repair_no_change"))
            return replace(
                seed_repaired_plan,
                metadata={
                    **dict(getattr(seed_repaired_plan, "metadata", {}) or {}),
                    "route_legalization_actions": tuple((*actions, *seed_repair_actions, "bounded_seed_inline_repair_return")),
                },
            ), tuple((*actions, *seed_repair_actions, "bounded_seed_inline_repair_return"))

    estimated_current = dict(current_report.get("estimated_current_ma_by_net", {}) or {})
    updated_paths = []
    updated_vias = []
    path_changed = False
    via_changed = False
    via_by_net: dict[str, list[LayoutVia]] = {}
    for via in plan.vias:
        via_by_net.setdefault(str(via.net), []).append(via)

    from .routing import _route_width_um, _via_array_size
    intent_set = build_routing_intent_set(
        effective_constraints,
        available_nets=tuple(
            dict.fromkeys(
                str(net)
                for net in (
                    *(str(path.net) for path in plan.paths),
                    *(str(via.net) for via in plan.vias),
                )
                if str(net)
            )
        ),
    )

    for path in plan.paths:
        net = str(path.net)
        net_constraints = intent_set.for_net(net)
        target_width = _route_width_um(
            str(path.layer),
            net_constraints,
            pdk,
            estimated_current_ma=float(estimated_current.get(net, 0.0) or 0.0),
        )
        if target_width > float(path.width) + 1e-12:
            updated_paths.append(replace(path, width=target_width))
            path_changed = True
        else:
            updated_paths.append(path)
    for via in plan.vias:
        net = str(via.net)
        net_constraints = intent_set.for_net(net)
        route_layer = ""
        for path in updated_paths:
            if str(path.net) == net:
                route_layer = str(path.layer)
                break
        rows, cols = _via_array_size(
            net_constraints,
            pdk=pdk,
            pin_layer="",
            route_layer=route_layer,
            estimated_current_ma=float(estimated_current.get(net, 0.0) or 0.0),
        )
        new_rows = max(int(via.rows), int(rows))
        new_cols = max(int(via.cols), int(cols))
        if new_rows != via.rows or new_cols != via.cols:
            updated_vias.append(replace(via, rows=new_rows, cols=new_cols))
            via_changed = True
        else:
            updated_vias.append(via)
    if path_changed:
        actions.append("widen_or_relayer_current_limited_routes")
    if via_changed:
        actions.append("increase_via_array_capacity")
    legalized = replace(
        plan,
        paths=tuple(updated_paths),
        vias=tuple(updated_vias),
        metadata={
            **dict(plan.metadata),
            "legalization_actions": tuple(actions),
        },
    )
    legalized, route_actions = _retune_route_candidate(legalized, effective_constraints)
    actions.extend(route_actions)
    if require_inline_verification:
        repair = run_physical_drc_repair_loop(
            legalized,
            constraints=effective_constraints,
            pdk=pdk,
            max_iterations=max(1, int(inline_repair_max_iterations)),
            min_width_um_by_layer=dict(inline_rules.get("min_width_um_by_layer", {}) or {}),
            min_spacing_um_by_layer=dict(inline_rules.get("min_spacing_um_by_layer", {}) or {}),
            min_area_um2_by_layer=dict(inline_rules.get("min_area_um2_by_layer", {}) or {}),
            require_all_via_landings=True,
            include_via_landing_short_checks=True if include_via_landing_short_checks or require_inline_verification else False,
        )
        if repair.plan != legalized:
            legalized = repair.plan
            actions.append(f"inline_drc_repair:{len(tuple(repair.iterations))}")
        if not repair.passed:
            actions.append("inline_drc_repair_remaining")
        if seed_repaired_plan is not None:
            selected_count = _inline_issue_count_for_route_legalization(
                legalized,
                graph=graph,
                pdk=pdk,
                min_area_um2_by_layer=dict(inline_rules.get("min_area_um2_by_layer", {}) or {}),
            )
            seed_count = _inline_issue_count_for_route_legalization(
                seed_repaired_plan,
                graph=graph,
                pdk=pdk,
                min_area_um2_by_layer=dict(inline_rules.get("min_area_um2_by_layer", {}) or {}),
            )
            if seed_count < selected_count:
                legalized = seed_repaired_plan
                actions = [*seed_repair_actions, "prefer_seed_inline_repair_over_route_retune"]
    if not actions:
        return plan, ()
    return replace(
        legalized,
        metadata={
            **dict(getattr(legalized, "metadata", {})),
            "legalization_actions": tuple(actions),
        },
    ), tuple(actions)


def _dedupe_close_same_net_vias(plan: Any, pdk: PdkConfig) -> tuple[Any, tuple[str, ...]]:
    from .ir import LayoutPlan

    if not isinstance(plan, LayoutPlan):
        return plan, ()
    kept: list[Any] = []
    removed = 0
    for via in tuple(getattr(plan, "vias", ()) or ()):
        via_def = str(getattr(via, "via_def", "") or "")
        net = str(getattr(via, "net", "") or "")
        if not via_def or not net:
            kept.append(via)
            continue
        cut = _via_cut_bbox(via, pdk)
        spacing = _via_required_spacing_um(via_def, pdk, same_net=True)
        redundant = False
        for existing in kept:
            if str(getattr(existing, "via_def", "") or "") != via_def:
                continue
            if str(getattr(existing, "net", "") or "") != net:
                continue
            existing_cut = _via_cut_bbox(existing, pdk)
            if _bbox_distance(cut, existing_cut) < max(spacing, 0.0) - 1e-12 or bbox_overlaps(cut, existing_cut, include_touching=True):
                redundant = True
                removed += 1
                break
        if not redundant:
            kept.append(via)
    if removed <= 0:
        return plan, ()
    return (
        replace(
            plan,
            vias=tuple(kept),
            metadata={
                **dict(getattr(plan, "metadata", {}) or {}),
                "duplicate_same_net_via_removed_count": removed,
            },
        ),
        (f"dedupe_close_same_net_vias:{removed}",),
    )


def _inline_issue_count_for_route_legalization(
    plan: Any,
    *,
    graph: Any | None,
    pdk: PdkConfig,
    min_area_um2_by_layer: Mapping[str, float] | None,
) -> int:
    verification = _build_inline_verification_contract(
        plan,
        graph=graph,
        pdk=pdk,
        min_area_um2_by_layer=min_area_um2_by_layer,
    )
    return int(verification.get("issue_count", 0) or 0)


def _is_specialized_analog_backbone(plan: Any) -> bool:
    nets = {str(net) for net in tuple(getattr(plan, "nets", ()) or ()) if str(net)}
    ota3 = {"INP", "INN", "TAIL", "N1", "N3", "OUT", "BIAS_N", "BIAS_P"}
    padc = {"VINP", "VINN", "RES1P", "RES1N", "RES2P", "RES2N", "VREFP_BUF", "VREFN_BUF", "DOUTP", "DOUTN"}
    return ota3.issubset(nets) or padc.issubset(nets)


def _is_specialized_analog_backbone_placement(placements: tuple[Any, ...]) -> bool:
    roles = {str(getattr(item, "role", "")) for item in placements}
    ota3_roles = {"M1A", "M1B", "M2A", "M2B", "MTAIL", "M3", "M4", "M5", "M6", "RZ1", "CC1", "CC2"}
    padc_roles = {
        "REFBUF_P",
        "REFBUF_N",
        "REFBIAS_P",
        "REFBIAS_N",
        "S1_SWP",
        "S1_SWN",
        "S1_INP",
        "S1_INN",
        "S1_LOADP",
        "S1_LOADN",
        "S1_TAIL",
        "S1_CAPP",
        "S1_CAPN",
        "S2_SWP",
        "S2_SWN",
        "S2_INP",
        "S2_INN",
        "S2_LOADP",
        "S2_LOADN",
        "S2_TAIL",
        "S2_CAPP",
        "S2_CAPN",
        "FLASH_INP",
        "FLASH_INN",
        "FLASH_LOADP",
        "FLASH_LOADN",
        "FLASH_TAIL",
    }
    return ota3_roles.issubset(roles) or padc_roles.issubset(roles)


def _route_constraints_with_hierarchy_guidance(
    constraints: LayoutConstraintSet,
    plan: Any,
    *,
    hierarchy_context: Mapping[str, object] | None = None,
) -> tuple[LayoutConstraintSet, tuple[str, ...]]:
    hierarchy_lowering = _physical_hierarchy_lowering_contract(hierarchy_context)
    hotspot_nets = {
        str(net)
        for net in tuple(getattr(plan, "nets", ()) or ())
        if str(net)
    }
    protected_nets: list[str] = []
    anchor_nets = tuple(str(net) for net in tuple(hierarchy_lowering.get("routing_anchor_nets", ())) if str(net))
    protected_nets.extend(net for net in anchor_nets if net not in hotspot_nets)
    parasitic_plan = dict((hierarchy_context or {}).get("hierarchical_partition_parasitic_target_plan", {}) or {}) if hierarchy_context else {}
    for partition in tuple(parasitic_plan.get("partitions", ()) or ()):
        if not isinstance(partition, Mapping):
            continue
        budget = dict(partition.get("architecture_budget", {}) or {})
        sensitivity = str(budget.get("sensitivity", budget.get("sensitivity_class", "")) or "")
        if sensitivity not in {"reference_critical", "timing_critical", "feedback_critical"}:
            continue
        for net in (
            tuple(partition.get("critical_nets", ()) or ())
            + tuple(partition.get("reference_nets", ()) or ())
            + tuple(partition.get("feedback_nets", ()) or ())
            + tuple(partition.get("routing_anchor_nets", ()) or ())
        ):
            if str(net) and str(net) not in hotspot_nets:
                protected_nets.append(str(net))
    protected_anchor_nets = tuple(dict.fromkeys(protected_nets))
    if not protected_anchor_nets:
        return constraints, ()
    extra_constraints: list[RoutingConstraint] = list(constraints.routing)
    for net in hotspot_nets:
        extra_constraints.append(
            RoutingConstraint(
                net,
                "avoid_nets",
                tuple(protected_anchor_nets),
                "avoid protected hierarchical anchor nets during legalization",
            )
        )
    actions = ["protect_hierarchical_anchor_nets_during_legalization"]
    if any(net not in set(anchor_nets) for net in protected_anchor_nets):
        actions.append("protect_architecture_critical_nets_during_legalization")
    return replace(constraints, routing=tuple(extra_constraints)), tuple(actions)


def _retune_placement_hierarchy_guidance(
    placements: tuple[Any, ...],
    *,
    placement_seed_metadata: Mapping[str, object] | None = None,
) -> tuple[tuple[Any, ...], tuple[str, ...]]:
    metadata = dict(placement_seed_metadata or {})
    partition_device_map = {
        str(name): tuple(str(device) for device in devices if str(device))
        for name, devices in dict(metadata.get("partition_device_map", {}) or {}).items()
    }
    preferred_order = tuple(str(name) for name in metadata.get("preferred_partition_order", ()) if str(name))
    if not partition_device_map:
        return tuple(placements), ()
    device_to_partition = {
        device: partition
        for partition, devices in partition_device_map.items()
        for device in devices
    }
    anchor_partitions = tuple(str(name) for name in metadata.get("anchor_partitions", ()) if str(name))
    focus_partitions = tuple(str(name) for name in metadata.get("focus_partitions", ()) if str(name))
    anchor_spread_target = float(metadata.get("anchor_spread_target_um", 0.0) or 0.0)
    focus_separation_target = float(metadata.get("focus_separation_target_um", 0.0) or 0.0)
    by_partition: dict[str, list[Any]] = {}
    for item in placements:
        name = str(getattr(item, "name", ""))
        role = str(getattr(item, "role", "") or name)
        normalized_role = role.lower()
        normalized_name = name.lower()
        partition = (
            device_to_partition.get(name)
            or device_to_partition.get(role)
            or device_to_partition.get(normalized_name)
            or device_to_partition.get(normalized_role)
        )
        if partition:
            by_partition.setdefault(partition, []).append(item)
    available = tuple(name for name in preferred_order if by_partition.get(name))
    all_centers = {
        name: sum(float(getattr(item, "x_um", 0.0)) for item in items) / len(items)
        for name, items in by_partition.items()
        if items
    }
    shifts = {name: 0.0 for name in by_partition}
    actions: list[str] = []
    if len(available) >= 2:
        current_centers = {name: all_centers[name] for name in available}
        target_xs = sorted(current_centers.values())
        order_shifts = {
            name: target_xs[idx] - current_centers[name]
            for idx, name in enumerate(available)
        }
        for name, shift in order_shifts.items():
            shifts[name] = shifts.get(name, 0.0) + shift
        if any(abs(shift) > 1e-12 for shift in order_shifts.values()):
            actions.append("retune_partition_order_from_floorplan_seed")
            for name in current_centers:
                all_centers[name] = current_centers[name] + order_shifts.get(name, 0.0)
    available_anchors = tuple(name for name in anchor_partitions if name in all_centers)
    if len(available_anchors) >= 2 and anchor_spread_target > 0.0:
        min_x = min(all_centers[name] for name in available_anchors)
        max_x = max(all_centers[name] for name in available_anchors)
        spread = max_x - min_x
        if spread > anchor_spread_target + 1e-12:
            center = 0.5 * (min_x + max_x)
            target_min = center - (0.5 * anchor_spread_target)
            target_max = center + (0.5 * anchor_spread_target)
            sorted_anchors = sorted(available_anchors, key=lambda name: all_centers[name])
            if len(sorted_anchors) == 2:
                target_positions = (target_min, target_max)
            else:
                step = anchor_spread_target / max(len(sorted_anchors) - 1, 1)
                target_positions = tuple(target_min + (idx * step) for idx in range(len(sorted_anchors)))
            for idx, name in enumerate(sorted_anchors):
                delta = target_positions[idx] - all_centers[name]
                shifts[name] = shifts.get(name, 0.0) + delta
                all_centers[name] = all_centers[name] + delta
            actions.append("compact_anchor_partitions_from_floorplan_seed")
    available_focus = tuple(name for name in focus_partitions if name in all_centers)
    if len(available_focus) >= 2 and focus_separation_target > 0.0:
        sorted_focus = sorted(available_focus, key=lambda name: all_centers[name])
        min_focus = all_centers[sorted_focus[0]]
        max_focus = all_centers[sorted_focus[-1]]
        span = max_focus - min_focus
        if span < focus_separation_target - 1e-12:
            center = 0.5 * (min_focus + max_focus)
            target_min = center - (0.5 * focus_separation_target)
            target_max = center + (0.5 * focus_separation_target)
            if len(sorted_focus) == 2:
                target_positions = (target_min, target_max)
            else:
                step = focus_separation_target / max(len(sorted_focus) - 1, 1)
                target_positions = tuple(target_min + (idx * step) for idx in range(len(sorted_focus)))
            for idx, name in enumerate(sorted_focus):
                delta = target_positions[idx] - all_centers[name]
                shifts[name] = shifts.get(name, 0.0) + delta
                all_centers[name] = all_centers[name] + delta
            actions.append("separate_focus_partitions_from_floorplan_seed")
    if not actions:
        return tuple(placements), ()
    updated = []
    for item in placements:
        name = str(getattr(item, "name", ""))
        role = str(getattr(item, "role", "") or name)
        normalized_role = role.lower()
        normalized_name = name.lower()
        partition = (
            device_to_partition.get(name)
            or device_to_partition.get(role)
            or device_to_partition.get(normalized_name)
            or device_to_partition.get(normalized_role)
        )
        shift = shifts.get(partition, 0.0)
        updated.append(replace(item, x_um=float(getattr(item, "x_um", 0.0)) + shift))
    return tuple(updated), tuple(dict.fromkeys(actions))


def _retune_route_candidate(
    plan: Any,
    constraints: LayoutConstraintSet,
) -> tuple[Any, tuple[str, ...]]:
    from .ir import LayoutPlan
    from .routing import RoutedNet, balance_route_lengths

    if not isinstance(plan, LayoutPlan):
        return plan, ()
    actions: list[str] = []
    paths = list(plan.paths)

    for constraint in constraints.routing:
        if constraint.kind != "bus_order":
            continue
        expected = _constraint_value_tuple(constraint.value)
        if len(expected) < 2:
            continue
        indexed = [
            (idx, path)
            for idx, path in enumerate(paths)
            if str(path.net) in expected and len(tuple(path.points)) >= 2
        ]
        if len(indexed) != len(expected):
            continue
        ordered = sorted(indexed, key=lambda item: (item[1].points[0][1], item[1].points[0][0], str(item[1].net)))
        current = tuple(str(path.net) for _idx, path in ordered)
        if current == expected:
            continue
        for (idx, path), target_net in zip(ordered, expected):
            paths[idx] = replace(path, net=target_net)
        actions.append("restore_bus_order")

    seen_pairs: set[tuple[str, str]] = set()
    for constraint in constraints.routing:
        if constraint.kind not in {"match_length_with", "differential_partner"}:
            continue
        net = str(constraint.net)
        for peer in _constraint_value_tuple(constraint.value):
            pair = tuple(sorted((net, str(peer))))
            if len(pair) != 2 or pair in seen_pairs or not pair[0] or not pair[1]:
                continue
            seen_pairs.add(pair)
            idx_a = _single_path_index_for_net(paths, pair[0])
            idx_b = _single_path_index_for_net(paths, pair[1])
            if idx_a is None or idx_b is None:
                continue
            path_a = paths[idx_a]
            path_b = paths[idx_b]
            balanced = balance_route_lengths(
                (
                    RoutedNet.from_points(path_a.net, path_a.points, layer=path_a.layer),
                    RoutedNet.from_points(path_b.net, path_b.points, layer=path_b.layer),
                ),
                pair,
            )
            balanced_by_net = {route.net: route for route in balanced}
            new_a = balanced_by_net[pair[0]]
            new_b = balanced_by_net[pair[1]]
            changed = False
            if tuple(new_a.points) != tuple(path_a.points):
                paths[idx_a] = replace(path_a, points=tuple(new_a.points))
                changed = True
            if tuple(new_b.points) != tuple(path_b.points):
                paths[idx_b] = replace(path_b, points=tuple(new_b.points))
                changed = True
            if changed:
                actions.append("retune_matched_route_lengths")

    if not actions:
        return plan, ()
    return replace(plan, paths=tuple(paths)), tuple(dict.fromkeys(actions))


def _single_path_index_for_net(paths: Sequence[Any], net: str) -> int | None:
    matches = [idx for idx, path in enumerate(paths) if str(getattr(path, "net", "")) == net]
    if len(matches) != 1:
        return None
    return matches[0]


def _constraint_value_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, (tuple, list)):
        return tuple(str(item) for item in value if str(item))
    if value is None:
        return ()
    text = str(value)
    return (text,) if text else ()


def _materialize_device_plan(
    graph: Any,
    sizing: Mapping[str, Mapping[str, object]],
    placements: Sequence[Any],
    pdk: PdkConfig,
) -> Any:
    from analogskills.pcell import generate_pcell_layout_plan

    return generate_pcell_layout_plan(
        graph,
        sizing,
        pdk=pdk,
        placements=tuple(placements),
    )
