"""PCell instance planning and fallback primitive geometry generation."""
from __future__ import annotations

from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass, field, replace
from math import ceil, inf, log10, sqrt
from typing import Any, Mapping, Sequence

from analogskills._utils import coerce_dimension_m
from analogskills.contracts import Device, DeviceRole, TerminalRef, TopologyGraph
from analogskills.eda.oa import OaCellView, OaInstance, OaRect, OaWritePlan, snap_oa_write_plan_to_grid
from analogskills.layout import Placement
from analogskills.pdk import PCellTemplate, PdkConfig
from analogskills.repair import LayoutShape, snap_shapes_to_grid, validate_shapes_on_grid


@dataclass(frozen=True)
class FingerChoice:
    nf: int
    m: int
    finger_width_m: float
    total_width_m: float
    length_m: float
    objective: str = "balanced"
    score: float = 0.0
    gate_resistance_index: float = 0.0
    diffusion_cap_index: float = 0.0
    matching_index: float = 0.0

    @property
    def unit_count(self) -> int:
        return self.nf * self.m


@dataclass(frozen=True)
class LayoutFingerCandidate:
    """A layout-aware MOS finger candidate for agent-side review.

    The score is intentionally advisory.  Callers can inspect the cost
    breakdown, override weights, or ignore the ranking when another constraint
    is more important for a specific layout.
    """

    choice: FingerChoice
    width_um: float
    height_um: float
    area_um2: float
    aspect_ratio: float
    score: float
    costs: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class SizingFingerProposal:
    device: str
    metric: str
    action: str
    reason: str
    current_width_m: float
    proposed_width_m: float
    length_m: float
    finger_choice: FingerChoice
    params: dict[str, Any]
    candidates: tuple[LayoutFingerCandidate, ...] = ()
    priority: int = 50


@dataclass(frozen=True)
class PCellInstancePlan:
    name: str
    logical_name: str
    lib_name: str
    cell_name: str
    view_name: str
    params: dict[str, Any]
    xy_um: tuple[float, float] = (0.0, 0.0)
    orient: str = "R0"
    role: str = ""
    connections: dict[str, str] = field(default_factory=dict)
    width_um: float = 0.0
    height_um: float = 0.0
    finger_choice: FingerChoice | None = None
    validation_issues: tuple[str, ...] = ()
    instantiation_method: str = "dbCreateInstByMasterName"
    bbox_x0_um: float = 0.0
    bbox_y0_um: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PCellLayoutPlan:
    instances: tuple[PCellInstancePlan, ...]
    fallback_shapes: tuple[LayoutShape, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def instance_map(self) -> dict[str, PCellInstancePlan]:
        return {inst.name: inst for inst in self.instances}


def snap_pcell_layout_plan_to_grid(plan: PCellLayoutPlan, pdk: PdkConfig) -> PCellLayoutPlan:
    rules = pdk.rules
    instances = tuple(
        replace(
            inst,
            params=_snap_pcell_params_to_grid(inst.params, pdk),
            xy_um=rules.snap_point_um(inst.xy_um),
            width_um=rules.snap_dimension_um(inst.width_um) if inst.width_um > 0 else 0.0,
            height_um=rules.snap_dimension_um(inst.height_um) if inst.height_um > 0 else 0.0,
        )
        for inst in plan.instances
    )
    metadata = dict(plan.metadata)
    metadata["grid_nm"] = rules.grid_nm
    metadata["grid_snapped"] = True
    return PCellLayoutPlan(instances, snap_shapes_to_grid(plan.fallback_shapes, pdk, mode="outward"), metadata)


def validate_pcell_layout_plan_grid(plan: PCellLayoutPlan, pdk: PdkConfig, *, tol_um: float = 1e-12) -> list[str]:
    rules = pdk.rules
    issues: list[str] = []
    for inst in plan.instances:
        if not rules.point_is_on_grid_um(inst.xy_um, tol_um=tol_um):
            issues.append(f"{inst.name}.xy_um={inst.xy_um!r} is off-grid for {rules.grid_nm}nm grid")
        for key, value in inst.params.items():
            if _is_dimension_param(key) and isinstance(value, (float, int)) and value > 0:
                value_um = float(value) * 1e6
                if not rules.is_on_grid_um(value_um, tol_um=tol_um):
                    issues.append(f"{inst.name}.params[{key!r}]={value:g}m is off-grid for {rules.grid_nm}nm grid")
        if inst.width_um > 0 and not rules.is_on_grid_um(inst.width_um, tol_um=tol_um):
            issues.append(f"{inst.name}.width_um={inst.width_um:g}um is off-grid for {rules.grid_nm}nm grid")
        if inst.height_um > 0 and not rules.is_on_grid_um(inst.height_um, tol_um=tol_um):
            issues.append(f"{inst.name}.height_um={inst.height_um:g}um is off-grid for {rules.grid_nm}nm grid")
    issues.extend(validate_shapes_on_grid(plan.fallback_shapes, pdk, tol_um=tol_um))
    return issues


def enumerate_mos_finger_choices(
    *,
    width_m: float,
    length_m: float,
    objective: str = "balanced",
    min_finger_width_m: float = 0.5e-6,
    max_finger_width_m: float = 10e-6,
    max_fingers: int = 64,
    max_multiplier: int = 16,
) -> tuple[FingerChoice, ...]:
    if width_m <= 0 or length_m <= 0:
        raise ValueError("MOS width and length must be positive")
    if min_finger_width_m <= 0 or max_finger_width_m < min_finger_width_m:
        raise ValueError("invalid finger-width bounds")
    objective = objective.lower()
    target_width = sqrt(min_finger_width_m * max_finger_width_m)
    choices: list[FingerChoice] = []
    for m in range(1, max_multiplier + 1):
        for nf in range(1, max_fingers + 1):
            wf = width_m / (nf * m)
            if wf < min_finger_width_m or wf > max_finger_width_m:
                continue
            unit_count = nf * m
            gate_r = 1.0 / max(unit_count, 1)
            diffusion_c = (nf + 1) * m * max(wf, min_finger_width_m)
            matching = sqrt(width_m * length_m * unit_count)
            width_center_penalty = abs(wf / target_width - 1.0)
            score = _finger_score(objective, nf, m, unit_count, gate_r, diffusion_c, matching, width_center_penalty)
            choices.append(FingerChoice(nf, m, wf, width_m, length_m, objective, score, gate_r, diffusion_c, matching))
    if choices:
        return tuple(sorted(choices, key=lambda choice: (-choice.score, _finger_tiebreak(choice))))

    unit_count = max(1, min(max_fingers * max_multiplier, round(width_m / max_finger_width_m)))
    nf = min(max_fingers, unit_count)
    m = max(1, round(unit_count / nf))
    wf = width_m / (nf * m)
    fallback = FingerChoice(nf, m, wf, width_m, length_m, objective, -inf, 1.0 / (nf * m), (nf + 1) * m * wf, sqrt(width_m * length_m * nf * m))
    return (fallback,)


def select_mos_fingers(
    *,
    width_m: float,
    length_m: float,
    objective: str = "balanced",
    min_finger_width_m: float = 0.5e-6,
    max_finger_width_m: float = 10e-6,
    max_fingers: int = 64,
    max_multiplier: int = 16,
) -> FingerChoice:
    return enumerate_mos_finger_choices(
        width_m=width_m,
        length_m=length_m,
        objective=objective,
        min_finger_width_m=min_finger_width_m,
        max_finger_width_m=max_finger_width_m,
        max_fingers=max_fingers,
        max_multiplier=max_multiplier,
    )[0]


def rank_mos_finger_layout_candidates(
    *,
    width_m: float,
    length_m: float,
    objective: str = "balanced",
    min_finger_width_m: float = 0.5e-6,
    max_finger_width_m: float = 10e-6,
    max_fingers: int = 64,
    max_multiplier: int = 16,
    target_height_um: float | None = None,
    max_width_um: float | None = None,
    target_aspect: float | None = None,
    weights: Mapping[str, float] | None = None,
    top_k: int | None = 8,
) -> tuple[LayoutFingerCandidate, ...]:
    """Rank MOS finger choices using transparent layout-oriented costs.

    This helper is deliberately separate from ``generate_pcell_layout_plan`` so
    an agent can inspect alternatives and decide when to apply them.  Lower cost
    components are better; the returned score is the negative weighted normalized
    cost, so larger scores rank earlier.
    """

    choices = enumerate_mos_finger_choices(
        width_m=width_m,
        length_m=length_m,
        objective=objective,
        min_finger_width_m=min_finger_width_m,
        max_finger_width_m=max_finger_width_m,
        max_fingers=max_fingers,
        max_multiplier=max_multiplier,
    )
    if top_k is not None and top_k <= 0:
        return ()

    raw_rows: list[dict[str, float]] = []
    base_rows: list[tuple[FingerChoice, float, float, float, float]] = []
    for choice in choices:
        width_um, height_um = _mos_bbox_for_finger_choice(choice)
        area_um2 = width_um * height_um
        aspect_ratio = width_um / max(height_um, 1e-12)
        base_rows.append((choice, width_um, height_um, area_um2, aspect_ratio))
        raw_rows.append(
            {
                "intrinsic": -choice.score if choice.score != -inf else 1e12,
                "area": area_um2,
                "gate_resistance": choice.gate_resistance_index,
                "diffusion_cap": choice.diffusion_cap_index,
                "matching": -choice.matching_index,
                "height": _relative_abs_error(height_um, target_height_um),
                "aspect": _relative_abs_error(aspect_ratio, target_aspect),
                "width_limit": _relative_excess(width_um, max_width_um),
            }
        )

    normalized_rows = _normalize_cost_rows(raw_rows)
    weight_map = {
        "intrinsic": 0.10,
        "area": 0.20,
        "gate_resistance": 0.15,
        "diffusion_cap": 0.15,
        "matching": 0.15,
        "height": 0.15,
        "aspect": 0.05,
        "width_limit": 0.05,
    }
    weight_map.update({str(key): float(value) for key, value in dict(weights or {}).items()})

    ranked: list[LayoutFingerCandidate] = []
    for (choice, width_um, height_um, area_um2, aspect_ratio), costs in zip(base_rows, normalized_rows):
        score = -sum(weight_map.get(key, 0.0) * value for key, value in costs.items())
        ranked.append(
            LayoutFingerCandidate(
                choice=choice,
                width_um=width_um,
                height_um=height_um,
                area_um2=area_um2,
                aspect_ratio=aspect_ratio,
                score=score,
                costs=tuple(sorted(costs.items())),
            )
        )

    ranked.sort(key=lambda candidate: (-candidate.score, -candidate.choice.score, _finger_tiebreak(candidate.choice)))
    return tuple(ranked if top_k is None else ranked[:top_k])


def propose_metric_sizing_finger_ecos(
    scorecard: Any,
    sizing: Mapping[str, Mapping[str, Any]],
    *,
    metric_device_map: Mapping[str, Sequence[str]] | None = None,
    objective_by_metric: Mapping[str, str] | None = None,
    default_scale_up: float = 1.2,
    default_scale_down: float = 0.9,
    top_k: int = 3,
    hierarchy_context: Mapping[str, Any] | None = None,
) -> tuple[SizingFingerProposal, ...]:
    """Map failing Spectre/post-layout metrics to reviewable sizing/finger proposals."""

    if default_scale_up <= 0 or default_scale_down <= 0:
        raise ValueError("sizing scale factors must be positive")
    device_map = {str(metric): tuple(str(device) for device in devices) for metric, devices in dict(metric_device_map or {}).items()}
    objective_map = {str(metric): str(objective) for metric, objective in dict(objective_by_metric or {}).items()}
    proposals = []
    allowed_devices = tuple(str(name) for name in hierarchy_context.get("allowed_devices", ()) if str(name)) if hierarchy_context else ()
    blocked_devices = {str(name) for name in hierarchy_context.get("blocked_devices", ()) if str(name)} if hierarchy_context else set()
    scope_mode = str(hierarchy_context.get("scope_mode", "advisory_only")) if hierarchy_context else "advisory_only"
    for assessment in tuple(getattr(scorecard, "metric_assessments", ())):
        if bool(getattr(assessment, "passed", True)):
            continue
        metric = str(getattr(assessment, "name", ""))
        devices = device_map.get(metric, tuple(sizing))
        if allowed_devices:
            filtered = tuple(device for device in devices if device in allowed_devices)
            if filtered:
                devices = filtered
            elif scope_mode != "advisory_only":
                continue
        if blocked_devices:
            filtered = tuple(device for device in devices if device not in blocked_devices)
            if filtered:
                devices = filtered
            elif scope_mode != "advisory_only":
                continue
        scale, action = _metric_sizing_scale_and_action(assessment, default_scale_up, default_scale_down)
        objective = objective_map.get(metric, _metric_finger_objective(metric))
        for device in devices:
            current = dict(sizing.get(device, {}))
            if not current:
                continue
            current_width = _dimension_m(current, ("W", "w", "width"), 1e-6)
            length = _dimension_m(current, ("L", "l", "length"), 0.18e-6)
            proposed_width = max(current_width * scale, 1e-12)
            candidates = rank_mos_finger_layout_candidates(width_m=proposed_width, length_m=length, objective=objective, top_k=top_k)
            if not candidates:
                continue
            choice = candidates[0].choice
            params = dict(current)
            params.update({"W": proposed_width, "L": length, "nf": choice.nf, "m": choice.m, "wf": choice.finger_width_m})
            proposals.append(
                _reprioritize_sizing_finger_proposal(
                    SizingFingerProposal(
                        device,
                        metric,
                        action,
                        _metric_sizing_reason(assessment),
                        current_width,
                        proposed_width,
                        length,
                        choice,
                        params,
                        candidates,
                        _metric_sizing_priority(assessment),
                    ),
                    hierarchy_context=hierarchy_context,
                )
            )
    return tuple(sorted(proposals, key=lambda item: (-item.priority, item.metric, item.device)))


def _reprioritize_sizing_finger_proposal(
    proposal: SizingFingerProposal,
    *,
    hierarchy_context: Mapping[str, Any] | None,
) -> SizingFingerProposal:
    if hierarchy_context is None:
        return proposal
    priority = int(proposal.priority)
    focus_metrics = {str(name).lower() for name in hierarchy_context.get("focus_metrics", ()) if str(name)}
    changed_devices = {str(name) for name in hierarchy_context.get("retarget_changed_devices", ()) if str(name)}
    keep_stable_devices = {str(name) for name in hierarchy_context.get("keep_stable_devices", ()) if str(name)}
    if proposal.metric.lower() in focus_metrics:
        priority += 10
    if proposal.device in changed_devices:
        priority += 8
    if proposal.device in keep_stable_devices:
        priority -= 10
    return replace(proposal, priority=max(0, min(priority, 100)))


def apply_sizing_finger_proposal(
    sizing: Mapping[str, Mapping[str, Any]],
    proposal: SizingFingerProposal,
    *,
    merge_device_params: bool = True,
) -> dict[str, dict[str, Any]]:
    """Apply one sizing/finger proposal to a sizing map and return a new copy."""

    updated = {str(name): dict(values) for name, values in dict(sizing).items()}
    current = dict(updated.get(proposal.device, {})) if merge_device_params else {}
    current.update(dict(proposal.params))
    updated[str(proposal.device)] = current
    return updated


def apply_sizing_finger_proposals(
    sizing: Mapping[str, Mapping[str, Any]],
    proposals: Sequence[SizingFingerProposal],
    *,
    merge_device_params: bool = True,
) -> dict[str, dict[str, Any]]:
    """Apply multiple sizing/finger proposals in order and return a new sizing map."""

    updated = {str(name): dict(values) for name, values in dict(sizing).items()}
    for proposal in proposals:
        updated = apply_sizing_finger_proposal(updated, proposal, merge_device_params=merge_device_params)
    return updated


def _layout_bbox_origin_um(sizing: Mapping[str, Any]) -> tuple[float, float]:
    try:
        x0 = float(sizing.get("layout_bbox_x0_um", sizing.get("bbox_x0_um", 0.0)) or 0.0)
    except (TypeError, ValueError):
        x0 = 0.0
    try:
        y0 = float(sizing.get("layout_bbox_y0_um", sizing.get("bbox_y0_um", 0.0)) or 0.0)
    except (TypeError, ValueError):
        y0 = 0.0
    return (x0, y0)


def _has_nonzero_layout_bbox_origin(sizing: Mapping[str, Any], *, tol_um: float = 1e-12) -> bool:
    bbox_x0_um, bbox_y0_um = _layout_bbox_origin_um(sizing)
    return abs(float(bbox_x0_um)) > tol_um or abs(float(bbox_y0_um)) > tol_um


def _pcell_origin_for_footprint_lower_left(
    footprint_x_um: float,
    footprint_y_um: float,
    width_um: float,
    height_um: float,
    *,
    bbox_x0_um: float,
    bbox_y0_um: float,
    orient: str,
) -> tuple[float, float]:
    """Convert a footprint lower-left placement into a native PCell origin."""

    width = max(float(width_um), 0.0)
    height = max(float(height_um), 0.0)
    local_bbox = (
        float(bbox_x0_um),
        float(bbox_y0_um),
        float(bbox_x0_um) + width,
        float(bbox_y0_um) + height,
    )
    points = (
        _absolute_xy((0.0, 0.0), (local_bbox[0], local_bbox[1]), str(orient or "R0")),
        _absolute_xy((0.0, 0.0), (local_bbox[0], local_bbox[3]), str(orient or "R0")),
        _absolute_xy((0.0, 0.0), (local_bbox[2], local_bbox[1]), str(orient or "R0")),
        _absolute_xy((0.0, 0.0), (local_bbox[2], local_bbox[3]), str(orient or "R0")),
    )
    xs = tuple(point[0] for point in points)
    ys = tuple(point[1] for point in points)
    return (float(footprint_x_um) - min(xs), float(footprint_y_um) - min(ys))


def generate_pcell_layout_plan(
    graph: TopologyGraph,
    sizing: Mapping[str, Mapping[str, Any]],
    *,
    pdk: PdkConfig | None = None,
    placements: Sequence[Placement] | Mapping[str, tuple[float, float] | Placement] | None = None,
    finger_objective: str = "balanced",
    strict: bool = True,
    include_fallback_shapes: bool = False,
    snap_to_grid: bool = True,
) -> PCellLayoutPlan:
    pdk = pdk or PdkConfig.generic()
    term_map = graph.terminal_net_map()
    placement_map = _placement_map(placements)
    instances: list[PCellInstancePlan] = []
    x_cursor = 0.0
    issues: list[str] = []
    mos_unit_arrays: list[dict[str, Any]] = []
    bjt_unit_arrays: list[dict[str, Any]] = []
    passive_unit_arrays: list[dict[str, Any]] = []
    pcell_unit_arrays: list[dict[str, Any]] = []
    for device in graph.devices.values():
        logical_name = logical_pcell_name(device)
        try:
            template = pdk.pcell_template_for(logical_name)
        except KeyError as exc:
            if strict:
                raise
            issues.append(str(exc))
            continue
        device_sizing = realize_device_pcell_sizing(device, sizing.get(device.name, {}), pdk=pdk)
        finger = _finger_choice_for_device(device, device_sizing, finger_objective)
        params = pcell_params_for_device(device, device_sizing, template, finger_choice=finger, pdk=pdk if snap_to_grid else None)
        if logical_name == "capacitor" and _should_use_drawn_passive_primitive(logical_name, pdk, device_sizing):
            # Drawn capacitor implementations do not create a native OA
            # instance, so private geometry controls are safe to retain in the
            # plan.  This lets fallback lowering distinguish an interdigitated
            # MOM from the simple two-plate preview capacitor.
            for key in (
                "drawn_capacitor_style",
                "mom_start_metal",
                "mom_stop_metal",
                "mom_finger_width_um",
                "mom_finger_spacing_um",
                "mom_bus_width_um",
                "mom_edge_margin_um",
            ):
                if key in device_sizing:
                    params[f"__{key}"] = device_sizing[key]
        logical_for_validation = _logical_params_for_validation(device, device_sizing, finger)
        if snap_to_grid:
            logical_for_validation = _snap_pcell_params_to_grid(logical_for_validation, pdk)
        validation = tuple(template.validate_params(logical_for_validation))
        if validation and strict:
            raise ValueError("; ".join(validation))
        x_um, y_um, orient = _placement_for_device(device.name, placement_map, x_cursor)
        width_um, height_um = _snap_pcell_bbox_dimensions_um(*estimate_pcell_bbox_um(device, device_sizing, finger), pdk=pdk)
        x_cursor = x_um + width_um + max(0.5, pdk.rules.min_spacing_um(pdk.layer_map.metals[0]) if pdk.layer_map.metals else 0.5)
        instantiation_method = template.resolved_layout_instantiation_method()
        if _should_use_drawn_passive_primitive(logical_name, pdk, device_sizing):
            instantiation_method = "drawn_primitive"
        mos_array = _mos_unit_array_spec(logical_name, device_sizing)
        if mos_array is not None:
            array_instances = _mos_unit_array_instance_plans(
                device,
                template,
                device_sizing,
                mos_array,
                term_map,
                x_um=x_um,
                y_um=y_um,
                orient=orient,
                pdk=pdk,
            )
            instances.extend(array_instances)
            mos_unit_arrays.append(
                {
                    "device": device.name,
                    "logical_name": logical_name,
                    "unit_instances": tuple(inst.name for inst in array_instances),
                    "rows": int(mos_array.get("rows", 1) or 1),
                    "cols": int(mos_array.get("cols", 1) or 1),
                    "unit_count": int(mos_array.get("unit_count", len(array_instances)) or len(array_instances)),
                    "layout_width_um": width_um,
                    "layout_height_um": height_um,
                    "parallel_reduction_expected": bool(mos_array.get("parallel_reduction_expected", True)),
                    "source": str(mos_array.get("source", "")),
                    "matching_group": str(mos_array.get("matching_group", "")),
                    "matching_role": str(mos_array.get("matching_role", "")),
                    "pattern": tuple(mos_array.get("pattern", ()) or ()),
                    "dummy_realization": str(mos_array.get("dummy_realization", "")),
                }
            )
            pcell_unit_arrays.append(mos_unit_arrays[-1])
            continue
        bjt_array = _bjt_unit_array_spec(logical_name, device_sizing)
        if bjt_array is not None:
            array_instances = _bjt_unit_array_instance_plans(
                device,
                template,
                device_sizing,
                bjt_array,
                term_map,
                x_um=x_um,
                y_um=y_um,
                orient=orient,
                pdk=pdk,
            )
            instances.extend(array_instances)
            bjt_unit_arrays.append(
                {
                    "device": device.name,
                    "unit_instances": tuple(inst.name for inst in array_instances),
                    "rows": int(bjt_array.get("rows", 1) or 1),
                    "cols": int(bjt_array.get("cols", 1) or 1),
                    "unit_count": int(bjt_array.get("unit_count", len(array_instances)) or len(array_instances)),
                    "layout_width_um": width_um,
                    "layout_height_um": height_um,
                }
            )
            pcell_unit_arrays.append({**bjt_unit_arrays[-1], "logical_name": "bjt"})
            continue
        passive_array = _passive_unit_array_spec(logical_name, device_sizing)
        if passive_array is not None:
            array_instances = _passive_unit_array_instance_plans(
                device,
                template,
                device_sizing,
                passive_array,
                term_map,
                x_um=x_um,
                y_um=y_um,
                orient=orient,
                pdk=pdk,
            )
            instances.extend(array_instances)
            passive_unit_arrays.append(
                {
                    "device": device.name,
                    "logical_name": logical_name,
                    "unit_instances": tuple(inst.name for inst in array_instances),
                    "rows": int(passive_array.get("rows", 1) or 1),
                    "cols": int(passive_array.get("cols", 1) or 1),
                    "unit_count": int(passive_array.get("unit_count", len(array_instances)) or len(array_instances)),
                    "layout_width_um": width_um,
                    "layout_height_um": height_um,
                    "requires_schematic_expansion": bool(passive_array.get("requires_schematic_expansion", True)),
                }
            )
            pcell_unit_arrays.append(passive_unit_arrays[-1])
            continue
        bbox_x0_um, bbox_y0_um = _layout_bbox_origin_um(device_sizing)
        if _has_nonzero_layout_bbox_origin(device_sizing):
            origin_x_um, origin_y_um = _pcell_origin_for_footprint_lower_left(
                x_um,
                y_um,
                width_um,
                height_um,
                bbox_x0_um=bbox_x0_um,
                bbox_y0_um=bbox_y0_um,
                orient=orient,
            )
        else:
            origin_x_um, origin_y_um = float(x_um), float(y_um)
        origin_x_um, origin_y_um = _apply_pcell_origin_phase(
            origin_x_um,
            origin_y_um,
            logical_name=logical_name,
            cell_name=template.resolved_layout_cell_name(),
            params=params,
            sizing=device_sizing,
            pdk=pdk,
        )
        instances.append(
            PCellInstancePlan(
                name=device.name,
                logical_name=logical_name,
                lib_name=template.resolved_layout_lib_name(),
                cell_name=template.resolved_layout_cell_name(),
                view_name=template.resolved_layout_view_name(),
                params=params,
                instantiation_method=instantiation_method,
                xy_um=(origin_x_um, origin_y_um),
                orient=orient,
                role=device.role.value,
                connections={term: term_map.get(TerminalRef(device.name, term), "") for term in device.terminals},
                width_um=width_um,
                height_um=height_um,
                finger_choice=finger,
                validation_issues=validation,
                bbox_x0_um=bbox_x0_um,
                bbox_y0_um=bbox_y0_um,
            )
        )
    plan = PCellLayoutPlan(
        tuple(instances),
        metadata={
            "pdk": pdk.name,
            "issues": issues,
            "graph_name": str(getattr(graph, "name", "")),
            "top_level_nets": tuple(str(pin) for pin in graph.pins),
            "top_level_pin_nets": {
                str(pin): str(term_map.get(TerminalRef(str(pin), "PIN"), str(pin)))
                for pin in graph.pins
            },
            "top_level_pin_roles": {
                str(pin): str(getattr(role, "value", role))
                for pin, role in graph.pins.items()
            },
            "mos_unit_arrays": tuple(mos_unit_arrays),
            "bjt_unit_arrays": tuple(bjt_unit_arrays),
            "passive_unit_arrays": tuple(passive_unit_arrays),
            "pcell_unit_arrays": tuple(pcell_unit_arrays),
        },
    )
    include_device_fallback_shapes = include_fallback_shapes or any(_is_drawn_primitive_instance(inst) for inst in plan.instances)
    if include_device_fallback_shapes:
        source_plan = snap_pcell_layout_plan_to_grid(plan, pdk) if snap_to_grid else plan
        fallback_shapes = [
            shape
            for inst in source_plan.instances
            if include_fallback_shapes or _is_drawn_primitive_instance(inst)
            for shape in fallback_shapes_for_instance(inst, pdk, snap_to_grid=snap_to_grid)
        ]
        if include_fallback_shapes:
            fallback_shapes.extend(_topology_fallback_shapes(graph, source_plan.instances, pdk, snap_to_grid=snap_to_grid))
        plan = PCellLayoutPlan(plan.instances, tuple(fallback_shapes), plan.metadata)
    if snap_to_grid:
        plan = snap_pcell_layout_plan_to_grid(plan, pdk)
    return plan


def _apply_pcell_origin_phase(
    origin_x_um: float,
    origin_y_um: float,
    *,
    logical_name: str,
    cell_name: str,
    params: Mapping[str, Any],
    sizing: Mapping[str, Any],
    pdk: PdkConfig,
) -> tuple[float, float]:
    phase = _pcell_origin_phase_um(
        logical_name=logical_name,
        cell_name=cell_name,
        params=params,
        sizing=sizing,
        pdk=pdk,
    )
    if phase == (0.0, 0.0):
        return (float(origin_x_um), float(origin_y_um))
    return (float(origin_x_um) + phase[0], float(origin_y_um) + phase[1])


def _pcell_origin_phase_um(
    *,
    logical_name: str,
    cell_name: str,
    params: Mapping[str, Any],
    sizing: Mapping[str, Any],
    pdk: PdkConfig,
) -> tuple[float, float]:
    direct = _phase_from_value(sizing.get("pcell_origin_phase_um"))
    if direct is not None:
        return direct
    generation = _pcell_generation_metadata(pdk)
    for rule in tuple(generation.get("origin_phase_rules", ()) or ()):
        if not isinstance(rule, Mapping):
            continue
        if not _pcell_phase_rule_matches(rule, logical_name=logical_name, cell_name=cell_name, params=params, sizing=sizing):
            continue
        phase = _phase_from_value(rule.get("phase_um", rule.get("origin_phase_um")))
        if phase is not None:
            return phase
    by_cell = generation.get("origin_phase_um_by_cell", {})
    if isinstance(by_cell, Mapping):
        for key in (cell_name, logical_name):
            phase = _phase_from_value(by_cell.get(key))
            if phase is not None:
                return phase
    return (0.0, 0.0)


def _pcell_generation_metadata(pdk: PdkConfig) -> Mapping[str, Any]:
    metadata = getattr(pdk, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return {}
    generation = metadata.get("pcell_generation", {}) or {}
    return generation if isinstance(generation, Mapping) else {}


def _calibre_stream_grid_nm(pdk: PdkConfig) -> int:
    metadata = getattr(pdk, "metadata", {}) or {}
    if isinstance(metadata, Mapping):
        calibre = metadata.get("calibre", {}) or {}
        if isinstance(calibre, Mapping):
            raw = calibre.get("grid_nm")
            try:
                return max(1, int(raw))
            except (TypeError, ValueError):
                pass
    return max(1, int(getattr(getattr(pdk, "rules", None), "grid_nm", 1) or 1))


def _pcell_parameter_grid_nm(pdk: PdkConfig) -> int:
    generation = _pcell_generation_metadata(pdk)
    raw = generation.get("parameter_grid_nm", generation.get("param_grid_nm"))
    if raw is None:
        raw = _calibre_stream_grid_nm(pdk)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _calibre_stream_grid_nm(pdk)


def _pcell_pitch_grid_nm(pdk: PdkConfig) -> int:
    generation = _pcell_generation_metadata(pdk)
    raw = generation.get("pitch_grid_nm", generation.get("array_pitch_grid_nm"))
    if raw is None:
        raw = _calibre_stream_grid_nm(pdk)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _calibre_stream_grid_nm(pdk)


def _snap_um_to_nm_grid_nearest(value_um: float, grid_nm: int) -> float:
    if value_um <= 0:
        return float(value_um)
    units = max(1, int(float(value_um) * 1e3 / float(grid_nm) + 0.5))
    return units * float(grid_nm) * 1e-3


def _snap_um_to_nm_grid_ceil(value_um: float, grid_nm: int) -> float:
    if value_um <= 0:
        return float(value_um)
    units = max(1, int(ceil(float(value_um) * 1e3 / float(grid_nm) - 1e-9)))
    return units * float(grid_nm) * 1e-3


def _snap_pcell_bbox_dimensions_um(width_um: float, height_um: float, *, pdk: PdkConfig) -> tuple[float, float]:
    grid_nm = _pcell_pitch_grid_nm(pdk)
    return (
        _snap_um_to_nm_grid_ceil(float(width_um), grid_nm),
        _snap_um_to_nm_grid_ceil(float(height_um), grid_nm),
    )


def _snap_pcell_pitch_um(value_um: float, pdk: PdkConfig) -> float:
    return _snap_um_to_nm_grid_ceil(float(value_um), _pcell_pitch_grid_nm(pdk))


def _pcell_phase_rule_matches(
    rule: Mapping[str, Any],
    *,
    logical_name: str,
    cell_name: str,
    params: Mapping[str, Any],
    sizing: Mapping[str, Any],
) -> bool:
    rule_cell = str(rule.get("cell_name", rule.get("cell", "")) or "")
    if rule_cell and rule_cell != str(cell_name):
        return False
    rule_logical = str(rule.get("logical_name", rule.get("logical", "")) or "")
    if rule_logical and rule_logical != str(logical_name):
        return False
    match_params = rule.get("match_params", {})
    if isinstance(match_params, Mapping) and not _params_match(match_params, params):
        return False
    match_sizing = rule.get("match_sizing", {})
    if isinstance(match_sizing, Mapping) and not _params_match(match_sizing, sizing):
        return False
    match_params_nm = rule.get("match_params_nm", {})
    if isinstance(match_params_nm, Mapping) and not _params_match_nm(match_params_nm, params):
        return False
    match_sizing_nm = rule.get("match_sizing_nm", {})
    if isinstance(match_sizing_nm, Mapping) and not _params_match_nm(match_sizing_nm, sizing):
        return False
    return True


def _params_match(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    for key, expected_value in expected.items():
        if key not in actual:
            return False
        if not _values_match(expected_value, actual.get(key)):
            return False
    return True


def _params_match_nm(expected_nm: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    for key, expected_value in expected_nm.items():
        if key not in actual:
            return False
        actual_nm = _dimension_value_to_nm(actual.get(key))
        expected_number = _float_or_none(expected_value)
        if actual_nm is None or expected_number is None:
            return False
        if abs(actual_nm - expected_number) > max(0.5, abs(expected_number) * 1e-6):
            return False
    return True


def _values_match(expected: Any, actual: Any) -> bool:
    expected_number = _float_or_none(expected)
    actual_number = _float_or_none(actual)
    if expected_number is not None and actual_number is not None:
        return abs(actual_number - expected_number) <= max(1e-15, abs(expected_number) * 1e-9)
    return str(actual) == str(expected)


def _dimension_value_to_nm(value: Any) -> float | None:
    if isinstance(value, str):
        text = value.strip().lower()
        for suffix, scale in (("nm", 1.0), ("n", 1.0), ("um", 1000.0), ("u", 1000.0), ("m", 1e9)):
            if text.endswith(suffix):
                number = _float_or_none(text[: -len(suffix)])
                return None if number is None else number * scale
    number = _float_or_none(value)
    if number is None:
        return None
    return number * 1e9


def _phase_from_value(value: Any) -> tuple[float, float] | None:
    if isinstance(value, Mapping):
        try:
            return (float(value.get("x_um", value.get("x", 0.0)) or 0.0), float(value.get("y_um", value.get("y", 0.0)) or 0.0))
        except (TypeError, ValueError):
            return None
    if isinstance(value, SequenceABC) and not isinstance(value, (str, bytes)) and len(value) >= 2:
        try:
            return (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mos_unit_array_spec(logical_name: str, sizing: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if str(logical_name).lower() not in {"nmos", "pmos"}:
        return None
    raw = sizing.get("mos_unit_array")
    if not isinstance(raw, Mapping) or not bool(raw.get("enabled", False)):
        return None
    try:
        unit_count = max(1, int(float(raw.get("unit_count", 1) or 1)))
        rows = max(1, int(float(raw.get("rows", 1) or 1)))
        cols = max(1, int(float(raw.get("cols", 1) or 1)))
    except (TypeError, ValueError):
        return None
    if unit_count <= 1:
        return None
    if rows * cols < unit_count:
        return None
    return dict(raw)


def _bjt_unit_array_spec(logical_name: str, sizing: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if str(logical_name).lower() != "bjt":
        return None
    raw = sizing.get("bjt_unit_array")
    if not isinstance(raw, Mapping) or not bool(raw.get("enabled", False)):
        return None
    try:
        unit_count = max(1, int(float(raw.get("unit_count", 1) or 1)))
        rows = max(1, int(float(raw.get("rows", 1) or 1)))
        cols = max(1, int(float(raw.get("cols", 1) or 1)))
    except (TypeError, ValueError):
        return None
    if unit_count <= 1:
        return None
    if rows * cols < unit_count:
        return None
    return dict(raw)


def _passive_unit_array_spec(logical_name: str, sizing: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if str(logical_name).lower() not in {"resistor", "capacitor"}:
        return None
    raw = sizing.get("passive_unit_array")
    if not isinstance(raw, Mapping) or not bool(raw.get("enabled", False)):
        return None
    try:
        unit_count = max(1, int(float(raw.get("unit_count", 1) or 1)))
        rows = max(1, int(float(raw.get("rows", 1) or 1)))
        cols = max(1, int(float(raw.get("cols", 1) or 1)))
    except (TypeError, ValueError):
        return None
    if unit_count <= 1:
        return None
    if rows * cols < unit_count:
        return None
    return dict(raw)


def _mos_unit_array_instance_plans(
    device: Device,
    template: PCellTemplate,
    sizing: Mapping[str, Any],
    spec: Mapping[str, Any],
    term_map: Mapping[TerminalRef, str],
    *,
    x_um: float,
    y_um: float,
    orient: str,
    pdk: PdkConfig,
) -> tuple[PCellInstancePlan, ...]:
    logical_name = logical_pcell_name(device)
    unit_count = max(1, int(float(spec.get("unit_count", 1) or 1)))
    rows = max(1, int(float(spec.get("rows", 1) or 1)))
    cols = max(1, int(float(spec.get("cols", 1) or 1)))
    unit_sizing = dict(sizing)
    unit_sizing["W"] = float(spec.get("unit_total_width_m", spec.get("unit_width_m", unit_sizing.get("W", 1e-6))) or 1e-6)
    unit_sizing["L"] = float(spec.get("unit_length_m", unit_sizing.get("L", unit_sizing.get("l", 0.18e-6))) or 0.18e-6)
    unit_sizing["nf"] = max(1, int(float(spec.get("unit_nf", unit_sizing.get("nf", 1)) or 1)))
    unit_sizing["m"] = max(1, int(float(spec.get("unit_m", 1) or 1)))
    if "unit_layout_bbox_x0_um" in spec or "layout_bbox_x0_um" in spec:
        unit_sizing["layout_bbox_x0_um"] = float(spec.get("unit_layout_bbox_x0_um", spec.get("layout_bbox_x0_um", 0.0)) or 0.0)
    if "unit_layout_bbox_y0_um" in spec or "layout_bbox_y0_um" in spec:
        unit_sizing["layout_bbox_y0_um"] = float(spec.get("unit_layout_bbox_y0_um", spec.get("layout_bbox_y0_um", 0.0)) or 0.0)
    unit_pcell_params = dict(spec.get("unit_pcell_params", {}) if isinstance(spec.get("unit_pcell_params", {}), Mapping) else {})
    raw_unit_param_rows = spec.get("unit_pcell_params_by_index", ())
    unit_param_rows = tuple(raw_unit_param_rows) if isinstance(raw_unit_param_rows, SequenceABC) and not isinstance(raw_unit_param_rows, (str, bytes)) else ()
    raw_unit_slots = spec.get("unit_slots", ())
    unit_slots = tuple(raw_unit_slots) if isinstance(raw_unit_slots, SequenceABC) and not isinstance(raw_unit_slots, (str, bytes)) else ()
    raw_unit_orients = spec.get("unit_orients", ())
    unit_orients = tuple(raw_unit_orients) if isinstance(raw_unit_orients, SequenceABC) and not isinstance(raw_unit_orients, (str, bytes)) else ()
    if unit_pcell_params:
        unit_sizing["pcell_overrides"] = unit_pcell_params
    unit_sizing.pop("mos_unit_array", None)
    unit_finger = _finger_choice_for_device(device, unit_sizing, "balanced")
    estimated_width_um, estimated_height_um = estimate_pcell_bbox_um(device, unit_sizing, unit_finger)
    unit_width_um = max(0.1, float(spec.get("unit_width_um", estimated_width_um) or estimated_width_um))
    unit_height_um = max(0.1, float(spec.get("unit_height_um", estimated_height_um) or estimated_height_um))
    unit_width_um, unit_height_um = _snap_pcell_bbox_dimensions_um(unit_width_um, unit_height_um, pdk=pdk)
    unit_sizing["layout_width_um"] = unit_width_um
    unit_sizing["layout_height_um"] = unit_height_um
    spacing = max(0.0, float(spec.get("spacing_um", 0.5) or 0.0))
    pitch_x = _snap_pcell_pitch_um(
        max(unit_width_um, float(spec.get("pitch_x_um", unit_width_um + spacing) or (unit_width_um + spacing))),
        pdk,
    )
    pitch_y = _snap_pcell_pitch_um(
        max(unit_height_um, float(spec.get("pitch_y_um", unit_height_um + spacing) or (unit_height_um + spacing))),
        pdk,
    )
    validation = tuple(template.validate_params(_logical_params_for_validation(device, unit_sizing, unit_finger)))
    connections = {term: term_map.get(TerminalRef(device.name, term), "") for term in device.terminals}
    bbox_x0_um, bbox_y0_um = _layout_bbox_origin_um(unit_sizing)
    anchor_by_expanded_footprint = _has_nonzero_layout_bbox_origin(unit_sizing)
    instances: list[PCellInstancePlan] = []
    for index in range(unit_count):
        try:
            slot = int(unit_slots[index]) if index < len(unit_slots) else index
        except (TypeError, ValueError):
            slot = index
        if slot < 0:
            raise ValueError(f"{device.name} MOS unit slot must be non-negative")
        row = slot // cols
        col = slot % cols
        if row >= rows:
            raise ValueError(f"{device.name} MOS unit slot {slot} exceeds {rows}x{cols} array")
        unit_orient = str(unit_orients[index]) if index < len(unit_orients) and str(unit_orients[index]) else str(orient or "R0")
        indexed_params = unit_param_rows[index] if index < len(unit_param_rows) else {}
        if indexed_params and not isinstance(indexed_params, Mapping):
            raise ValueError(f"{device.name} unit_pcell_params_by_index[{index}] must be a mapping")
        indexed_sizing = dict(unit_sizing)
        if indexed_params:
            merged_params = dict(unit_pcell_params)
            merged_params.update({str(key): value for key, value in indexed_params.items()})
            indexed_sizing["pcell_overrides"] = merged_params
        params = pcell_params_for_device(device, indexed_sizing, template, finger_choice=unit_finger, pdk=pdk)
        unit_x = float(x_um) + col * pitch_x
        unit_y = float(y_um) + row * pitch_y
        if anchor_by_expanded_footprint:
            origin_x_um, origin_y_um = _pcell_origin_for_footprint_lower_left(
                unit_x,
                unit_y,
                unit_width_um,
                unit_height_um,
                bbox_x0_um=bbox_x0_um,
                bbox_y0_um=bbox_y0_um,
                orient=unit_orient,
            )
        else:
            origin_x_um, origin_y_um = float(unit_x), float(unit_y)
        origin_x_um, origin_y_um = _apply_pcell_origin_phase(
            origin_x_um,
            origin_y_um,
            logical_name=logical_name,
            cell_name=template.resolved_layout_cell_name(),
            params=params,
            sizing=indexed_sizing,
            pdk=pdk,
        )
        instances.append(
            PCellInstancePlan(
                name=f"{device.name}_u{index}",
                logical_name=logical_name,
                lib_name=template.resolved_layout_lib_name(),
                cell_name=template.resolved_layout_cell_name(),
                view_name=template.resolved_layout_view_name(),
                params=dict(params),
                instantiation_method=template.resolved_layout_instantiation_method(),
                xy_um=(origin_x_um, origin_y_um),
                orient=unit_orient,
                role=device.role.value,
                connections=connections,
                width_um=unit_width_um,
                height_um=unit_height_um,
                validation_issues=validation,
                bbox_x0_um=bbox_x0_um,
                bbox_y0_um=bbox_y0_um,
            )
        )
    return tuple(instances)


def _bjt_unit_array_instance_plans(
    device: Device,
    template: PCellTemplate,
    sizing: Mapping[str, Any],
    spec: Mapping[str, Any],
    term_map: Mapping[TerminalRef, str],
    *,
    x_um: float,
    y_um: float,
    orient: str,
    pdk: PdkConfig,
) -> tuple[PCellInstancePlan, ...]:
    unit_count = max(1, int(float(spec.get("unit_count", 1) or 1)))
    rows = max(1, int(float(spec.get("rows", 1) or 1)))
    cols = max(1, int(float(spec.get("cols", 1) or 1)))
    unit_width = max(0.1, float(spec.get("unit_width_um", 9.2) or 9.2))
    unit_height = max(0.1, float(spec.get("unit_height_um", 9.2) or 9.2))
    unit_width, unit_height = _snap_pcell_bbox_dimensions_um(unit_width, unit_height, pdk=pdk)
    spacing = max(0.0, float(spec.get("spacing_um", 0.5) or 0.0))
    pitch_x = _snap_pcell_pitch_um(
        max(unit_width, float(spec.get("pitch_x_um", unit_width + spacing) or (unit_width + spacing))),
        pdk,
    )
    pitch_y = _snap_pcell_pitch_um(
        max(unit_height, float(spec.get("pitch_y_um", unit_height + spacing) or (unit_height + spacing))),
        pdk,
    )
    unit_pcell_params = dict(spec.get("unit_pcell_params", {}) if isinstance(spec.get("unit_pcell_params", {}), Mapping) else {})
    if not unit_pcell_params:
        unit_pcell_params = dict(sizing.get("pcell_overrides", {}) if isinstance(sizing.get("pcell_overrides", {}), Mapping) else {})
    unit_sizing = dict(sizing)
    unit_sizing["M"] = 1
    unit_sizing["m"] = 1
    unit_sizing["layout_width_um"] = unit_width
    unit_sizing["layout_height_um"] = unit_height
    if unit_pcell_params:
        unit_sizing["pcell_overrides"] = unit_pcell_params
    unit_sizing.pop("bjt_unit_array", None)
    params = pcell_params_for_device(device, unit_sizing, template, pdk=pdk)
    validation = tuple(template.validate_params({"M": 1}))
    connections = {term: term_map.get(TerminalRef(device.name, term), "") for term in device.terminals}
    instances: list[PCellInstancePlan] = []
    for index in range(unit_count):
        row = index // cols
        col = index % cols
        if row >= rows:
            break
        origin_x_um, origin_y_um = _apply_pcell_origin_phase(
            float(x_um) + col * pitch_x,
            float(y_um) + row * pitch_y,
            logical_name="bjt",
            cell_name=template.resolved_layout_cell_name(),
            params=params,
            sizing=unit_sizing,
            pdk=pdk,
        )
        instances.append(
            PCellInstancePlan(
                name=f"{device.name}_u{index}",
                logical_name="bjt",
                lib_name=template.resolved_layout_lib_name(),
                cell_name=template.resolved_layout_cell_name(),
                view_name=template.resolved_layout_view_name(),
                params=dict(params),
                instantiation_method=template.resolved_layout_instantiation_method(),
                xy_um=(origin_x_um, origin_y_um),
                orient=str(orient or "R0"),
                role=device.role.value,
                connections=connections,
                width_um=unit_width,
                height_um=unit_height,
                validation_issues=validation,
            )
        )
    return tuple(instances)


def _passive_unit_array_instance_plans(
    device: Device,
    template: PCellTemplate,
    sizing: Mapping[str, Any],
    spec: Mapping[str, Any],
    term_map: Mapping[TerminalRef, str],
    *,
    x_um: float,
    y_um: float,
    orient: str,
    pdk: PdkConfig,
) -> tuple[PCellInstancePlan, ...]:
    logical_name = logical_pcell_name(device)
    unit_count = max(1, int(float(spec.get("unit_count", 1) or 1)))
    rows = max(1, int(float(spec.get("rows", 1) or 1)))
    cols = max(1, int(float(spec.get("cols", 1) or 1)))
    unit_width = max(0.1, float(spec.get("unit_width_um", 1.0) or 1.0))
    unit_height = max(0.1, float(spec.get("unit_height_um", 1.0) or 1.0))
    unit_width, unit_height = _snap_pcell_bbox_dimensions_um(unit_width, unit_height, pdk=pdk)
    spacing = max(0.0, float(spec.get("spacing_um", 0.5) or 0.0), _passive_unit_array_min_spacing_um(pdk, logical_name))
    pitch_x = _snap_pcell_pitch_um(
        max(unit_width + spacing, float(spec.get("pitch_x_um", unit_width + spacing) or (unit_width + spacing))),
        pdk,
    )
    pitch_y = _snap_pcell_pitch_um(
        max(unit_height + spacing, float(spec.get("pitch_y_um", unit_height + spacing) or (unit_height + spacing))),
        pdk,
    )
    unit_pcell_params = dict(spec.get("unit_pcell_params", {}) if isinstance(spec.get("unit_pcell_params", {}), Mapping) else {})
    if not unit_pcell_params:
        unit_pcell_params = dict(sizing.get("pcell_overrides", {}) if isinstance(sizing.get("pcell_overrides", {}), Mapping) else {})
    unit_sizing = dict(sizing)
    unit_sizing["M"] = 1
    unit_sizing["m"] = 1
    unit_sizing["multi"] = 1
    unit_sizing["layout_width_um"] = unit_width
    unit_sizing["layout_height_um"] = unit_height
    unit_sizing["use_drawn_primitive"] = False
    if unit_pcell_params:
        unit_sizing["pcell_overrides"] = unit_pcell_params
    unit_sizing.pop("passive_unit_array", None)
    params = pcell_params_for_device(device, unit_sizing, template, pdk=pdk)
    validation = tuple(template.validate_params(_logical_params_for_validation(device, unit_sizing, None)))
    connections = {term: term_map.get(TerminalRef(device.name, term), "") for term in device.terminals}
    instances: list[PCellInstancePlan] = []
    for index in range(unit_count):
        row = index // cols
        col = index % cols
        if row >= rows:
            break
        origin_x_um, origin_y_um = _apply_pcell_origin_phase(
            float(x_um) + col * pitch_x,
            float(y_um) + row * pitch_y,
            logical_name=logical_name,
            cell_name=template.resolved_layout_cell_name(),
            params=params,
            sizing=unit_sizing,
            pdk=pdk,
        )
        instances.append(
            PCellInstancePlan(
                name=f"{device.name}_u{index}",
                logical_name=logical_name,
                lib_name=template.resolved_layout_lib_name(),
                cell_name=template.resolved_layout_cell_name(),
                view_name=template.resolved_layout_view_name(),
                params=dict(params),
                instantiation_method=template.resolved_layout_instantiation_method(),
                xy_um=(origin_x_um, origin_y_um),
                orient=str(orient or "R0"),
                role=device.role.value,
                connections=connections,
                width_um=unit_width,
                height_um=unit_height,
                validation_issues=validation,
            )
        )
    return tuple(instances)


def _passive_unit_array_min_spacing_um(pdk: PdkConfig | None, logical_name: str) -> float:
    """Return PDK-configured minimum native passive-array spacing in microns."""

    if pdk is None or str(logical_name).lower() not in {"resistor", "capacitor"}:
        return 0.0
    metadata = getattr(pdk, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return 0.0
    calibre = metadata.get("calibre", {}) or {}
    if not isinstance(calibre, Mapping):
        return 0.0
    passive = calibre.get("passive_array", {}) or {}
    if not isinstance(passive, Mapping):
        return 0.0
    logical = str(logical_name).lower()
    for key in (
        "minimum_access_array_spacing_um_by_logical",
        "access_array_spacing_um_by_logical",
        "minimum_array_spacing_um_by_logical",
        "spacing_um_by_logical",
        "array_spacing_um_by_logical",
    ):
        by_logical = passive.get(key, {}) or {}
        if not isinstance(by_logical, Mapping):
            continue
        raw = by_logical.get(logical, by_logical.get(str(logical_name), by_logical.get("*")))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0.0:
            return value
    for key in (
        "minimum_access_array_spacing_nm_by_logical",
        "access_array_spacing_nm_by_logical",
        "minimum_array_spacing_nm_by_logical",
        "spacing_nm_by_logical",
        "array_spacing_nm_by_logical",
    ):
        by_logical = passive.get(key, {}) or {}
        if not isinstance(by_logical, Mapping):
            continue
        raw = by_logical.get(logical, by_logical.get(str(logical_name), by_logical.get("*")))
        try:
            value_nm = float(raw)
        except (TypeError, ValueError):
            value_nm = 0.0
        if value_nm > 0.0:
            return value_nm * 1e-3
    for um_key, nm_key in (
        ("minimum_access_array_spacing_um", "minimum_access_array_spacing_nm"),
        ("access_array_spacing_um", "access_array_spacing_nm"),
        ("minimum_array_spacing_um", "minimum_array_spacing_nm"),
        ("spacing_um", "spacing_nm"),
        ("array_spacing_um", "array_spacing_nm"),
    ):
        try:
            value = float(passive.get(um_key, 0.0) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0.0:
            return value
        try:
            value_nm = float(passive.get(nm_key, 0.0) or 0.0)
        except (TypeError, ValueError):
            value_nm = 0.0
        if value_nm > 0.0:
            return value_nm * 1e-3
    return 0.0


def pcell_params_for_device(
    device: Device,
    sizing: Mapping[str, Any],
    template: PCellTemplate,
    *,
    finger_choice: FingerChoice | None = None,
    pdk: PdkConfig | None = None,
) -> dict[str, Any]:
    logical = _logical_params_for_validation(device, sizing, finger_choice)
    if pdk is not None:
        logical = _snap_pcell_params_to_grid(logical, pdk)
    mapped = template.map_layout_parameters(logical)
    if pdk is not None and str(getattr(pdk, "name", "")).lower() == "crn28hpcp" and logical_pcell_name(device) in {"nmos", "pmos"}:
        mapped = _apply_crn28_mos_total_width_semantics(mapped, sizing)
    overrides = sizing.get("pcell_overrides", {})
    if overrides:
        if not isinstance(overrides, Mapping):
            raise ValueError("pcell_overrides must be a mapping")
        mapped.update({str(key): value for key, value in overrides.items()})
    mapped = template.filter_layout_parameters(mapped)
    return mapped


def realize_pcell_source_sizing(
    graph: TopologyGraph,
    sizing: Mapping[str, Mapping[str, Any]],
    *,
    pdk: PdkConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Return sizing augmented with PDK calibrated PCell realization choices.

    This keeps source netlisting and layout generation on the same physical
    realization.  The original electrical sizing is preserved unless the
    selected calibrated PCell explicitly contributes layout/source parameters
    such as ``pcell_overrides`` and measured bbox dimensions.
    """

    pdk_obj = pdk or PdkConfig.generic()
    result = {str(name): dict(values) for name, values in (sizing or {}).items()}
    for device in graph.devices.values():
        result[device.name] = realize_device_pcell_sizing(device, result.get(device.name, {}), pdk=pdk_obj)
    return result


def realize_device_pcell_sizing(
    device: Device,
    sizing: Mapping[str, Any],
    *,
    pdk: PdkConfig | None = None,
) -> dict[str, Any]:
    """Return one device sizing augmented with a calibrated PCell candidate."""

    pdk_obj = pdk or PdkConfig.generic()
    logical = logical_pcell_name(device)
    result = dict(sizing or {})
    if logical not in {"resistor", "capacitor"}:
        return result
    if _should_use_drawn_passive_primitive(logical, pdk_obj, result):
        return result
    candidate = _select_native_passive_realization_candidate(logical, pdk_obj, result)
    if not candidate:
        return result
    overrides = candidate.get("pcell_params", {})
    if isinstance(overrides, Mapping) and overrides:
        existing = result.get("pcell_overrides", {})
        if isinstance(existing, Mapping) and existing:
            merged = {str(key): value for key, value in overrides.items()}
            merged.update({str(key): value for key, value in existing.items()})
            result["pcell_overrides"] = merged
        else:
            result["pcell_overrides"] = {str(key): value for key, value in overrides.items()}
    for key in ("layout_width_um", "layout_height_um", "bbox_width_um", "bbox_height_um"):
        if key not in result and key in candidate:
            result[key] = candidate[key]
    result.setdefault("pcell_realization_candidate", str(candidate.get("name", "")))
    result.setdefault("calibrated_pcell_realization", bool(candidate.get("calibrated_pcell_realization", False)))
    return result


def logical_pcell_name(device: Device) -> str:
    model = device.model.lower()
    if device.role == DeviceRole.BIPOLAR or "npn" in model or "pnp" in model or "bjt" in model:
        return "bjt"
    if device.role == DeviceRole.COMP_RESISTOR or "res" in model:
        return "resistor"
    if device.role == DeviceRole.COMP_CAPACITOR or "cap" in model:
        return "capacitor"
    if "pmos" in model or model.startswith("pch") or model.startswith("p_"):
        return "pmos"
    if "nmos" in model or model.startswith("nch") or model.startswith("n_"):
        return "nmos"
    return model


def _apply_crn28_mos_total_width_semantics(mapped: Mapping[str, Any], sizing: Mapping[str, Any]) -> dict[str, Any]:
    """Return CRN28 MOS PCell params where sizing ``W`` means total device width.

    The native T28 MOS PCell uses ``Wfg`` as the per-finger width and ``simM``
    as an additional parallel multiplier.  The public analogskills sizing contract
    uses ``W`` as total effective width, so ``Wfg`` must be divided by both the
    finger count and the multiplier.  The generic parameter-map language can
    express ``W / nf`` but not the cross-key ``/ m`` correction reliably, so we
    apply it here only for CRN28.
    """

    result = dict(mapped)
    if "W" not in sizing and "w" not in sizing and "width" not in sizing:
        return result
    width_m = _dimension_m(sizing, ("W", "w", "width"), 1e-6)
    try:
        nf = max(1, int(float(result.get("fingers", sizing.get("nf", 1)) or 1)))
    except (TypeError, ValueError):
        nf = 1
    try:
        mult = max(1, int(float(result.get("simM", sizing.get("m", sizing.get("M", 1))) or 1)))
    except (TypeError, ValueError):
        mult = 1
    result["Wfg"] = width_m / float(max(nf * mult, 1))
    return result


def estimate_pcell_bbox_um(device: Device, sizing: Mapping[str, Any], finger_choice: FingerChoice | None = None) -> tuple[float, float]:
    logical = logical_pcell_name(device)
    if logical in {"nmos", "pmos"}:
        explicit_width = float(sizing.get("layout_width_um", sizing.get("bbox_width_um", 0.0)) or 0.0)
        explicit_height = float(sizing.get("layout_height_um", sizing.get("bbox_height_um", 0.0)) or 0.0)
        if explicit_width > 0.0 and explicit_height > 0.0:
            return (explicit_width, explicit_height)
        width_um = _dimension_m(sizing, ("W", "w", "width"), 1e-6) * 1e6
        length_um = _dimension_m(sizing, ("L", "l", "length"), 0.18e-6) * 1e6
        nf = finger_choice.nf if finger_choice is not None else int(sizing.get("nf", 1) or 1)
        m = finger_choice.m if finger_choice is not None else int(sizing.get("m", 1) or 1)
        finger_pitch = max(0.28, length_um + 0.32)
        return (max(0.6, (nf * m + 1) * finger_pitch), max(0.5, width_um / max(nf * m, 1) + 0.4))
    if logical == "bjt":
        # TSMC 28 ``npn`` layout default bbox observed from the PDK master.
        return _dimension_um_pair(
            sizing,
            ("layout_width_um", "bbox_width_um", "width_um", "w_um"),
            ("layout_height_um", "bbox_height_um", "height_um", "h_um"),
            (9.2, 9.2),
        )
    if logical == "resistor":
        value = max(float(sizing.get("R", sizing.get("r", 1e3))), 1.0)
        stripe_h = max(0.18, min(0.5, _dimension_m(sizing, ("W", "w", "width"), 0.5e-6) * 1e6))
        body_len = max(0.55, min(1.8, 0.55 + 0.35 * max(log10(value), 0.0)))
        pad_len = max(0.08, min(0.16, 0.35 * stripe_h))
        return _dimension_um_pair(
            sizing,
            ("layout_width_um", "bbox_width_um", "width_um", "w_um"),
            ("layout_height_um", "bbox_height_um", "height_um", "h_um"),
            (body_len + 2.0 * pad_len, stripe_h),
        )
    if logical == "capacitor":
        value = max(float(sizing.get("C", sizing.get("c", 1e-15))), 1e-18)
        scale = max(log10(value * 1e15), 0.0)
        plate_w = max(0.6, min(1.3, 0.65 + 0.14 * scale))
        plate_h = max(0.7, min(1.4, 0.75 + 0.16 * scale))
        return _dimension_um_pair(
            sizing,
            ("layout_width_um", "bbox_width_um", "width_um", "w_um"),
            ("layout_height_um", "bbox_height_um", "height_um", "h_um"),
            (plate_w, plate_h),
        )
    return (1.0, 1.0)


def fallback_shapes_for_instance(instance: PCellInstancePlan, pdk: PdkConfig, *, snap_to_grid: bool = True) -> tuple[LayoutShape, ...]:
    shapes: tuple[LayoutShape, ...]
    if instance.logical_name in {"nmos", "pmos"}:
        shapes = _mos_fallback_shapes(instance, pdk)
    elif instance.logical_name == "bjt":
        shapes = _bjt_fallback_shapes(instance, pdk)
    elif instance.logical_name == "resistor":
        shapes = _resistor_fallback_shapes(instance, pdk)
    elif instance.logical_name == "capacitor":
        shapes = _capacitor_fallback_shapes(instance, pdk)
    else:
        shapes = ()
    return snap_shapes_to_grid(shapes, pdk, mode="outward") if snap_to_grid else shapes


def _topology_fallback_shapes(
    graph: TopologyGraph,
    instances: Sequence[PCellInstancePlan],
    pdk: PdkConfig,
    *,
    snap_to_grid: bool = True,
) -> tuple[LayoutShape, ...]:
    instance_map = {inst.name: inst for inst in instances}
    if not instance_map:
        return ()
    signal_layers = tuple(getattr(pdk.layer_map, "metals", ()))
    layer = signal_layers[min(1, len(signal_layers) - 1)] if signal_layers else "M1"
    try:
        route_w = max(0.06, float(pdk.rules.min_width_um(layer)))
    except KeyError:
        route_w = 0.06

    shapes: list[LayoutShape] = []
    for net in graph.nets.values():
        drain_anchors = []
        gate_anchors = []
        for terminal in net.terminals:
            instance = instance_map.get(terminal.device)
            if instance is None:
                continue
            if terminal.terminal == "D":
                drain_anchors.append((terminal.device, _fallback_terminal_anchor(instance, "D")))
            elif terminal.terminal == "G":
                gate_anchors.append((terminal.device, _fallback_terminal_anchor(instance, "G")))
        if not drain_anchors or not gate_anchors:
            continue
        if not any(source != sink for source, _ in drain_anchors for sink, _ in gate_anchors):
            continue
        all_x = [anchor[0] for _, anchor in (*drain_anchors, *gate_anchors)]
        all_y = [anchor[1] for _, anchor in (*drain_anchors, *gate_anchors)]
        if not all_x or not all_y:
            continue
        trunk_y = sum(all_y) / len(all_y)
        x0 = min(all_x)
        x1 = max(all_x)
        if x1 - x0 > 1e-9:
            shapes.append(_route_rect(f"net_{net.name}_trunk", layer, net.name, x0, trunk_y, x1, trunk_y, route_w))
        for device_name, anchor in (*drain_anchors, *gate_anchors):
            if abs(anchor[1] - trunk_y) <= 1e-9:
                continue
            shapes.append(
                _route_rect(
                    f"net_{net.name}_{device_name}",
                    layer,
                    net.name,
                    anchor[0],
                    anchor[1],
                    anchor[0],
                    trunk_y,
                    route_w,
                )
            )

    deduped: dict[tuple[str, tuple[float, float, float, float], str], LayoutShape] = {}
    for shape in shapes:
        deduped[(shape.layer, shape.bbox, shape.net)] = shape
    result = tuple(deduped.values())
    return snap_shapes_to_grid(result, pdk, mode="outward") if snap_to_grid else result


def _fallback_terminal_anchor(instance: PCellInstancePlan, terminal: str) -> tuple[float, float]:
    width = max(instance.width_um, 0.2)
    height = max(instance.height_um, 0.2)
    if terminal == "G":
        return _absolute_xy(instance.xy_um, (0.5 * width, 0.5 * height), instance.orient)
    if terminal == "D":
        return _absolute_xy(instance.xy_um, (0.5 * width, 0.75 * height), instance.orient)
    if terminal == "S":
        return _absolute_xy(instance.xy_um, (0.5 * width, 0.25 * height), instance.orient)
    return _absolute_xy(instance.xy_um, (0.5 * width, 0.5 * height), instance.orient)


def _route_rect(
    shape_id: str,
    layer: str,
    net: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    width_um: float,
) -> LayoutShape:
    half = max(width_um, 1e-6) / 2.0
    if abs(y0 - y1) <= 1e-12:
        bbox = (min(x0, x1), y0 - half, max(x0, x1), y0 + half)
    elif abs(x0 - x1) <= 1e-12:
        bbox = (x0 - half, min(y0, y1), x0 + half, max(y0, y1))
    else:
        bbox = (min(x0, x1) - half, min(y0, y1) - half, max(x0, x1) + half, max(y0, y1) + half)
    return LayoutShape(shape_id, layer, bbox, net)


def build_pcell_oa_layout_plan(
    plan: PCellLayoutPlan,
    *,
    lib: str,
    cell: str,
    view: str = "layout",
    include_fallback_shapes: bool = False,
    pdk: PdkConfig | None = None,
    snap_to_grid: bool = True,
) -> OaWritePlan:
    instances = tuple(
        OaInstance(
            name=inst.name,
            lib=inst.lib_name,
            cell=inst.cell_name,
            view=inst.view_name,
            xy=inst.xy_um,
            orient=inst.orient,
            connections=dict(inst.connections),
            params=dict(inst.params),
            instantiation_method=inst.instantiation_method,
            metadata={
                **dict(inst.metadata),
                "logical_name": inst.logical_name,
                "width_um": inst.width_um,
                "height_um": inst.height_um,
                "bbox_x0_um": inst.bbox_x0_um,
                "bbox_y0_um": inst.bbox_y0_um,
            },
        )
        for inst in plan.instances
        if not _is_drawn_primitive_instance(inst)
    )
    rects = (
        tuple(
            OaRect(
                shape.layer,
                str(shape.metadata.get("purpose", "drawing")),
                shape.bbox,
                shape.net,
            )
            for shape in plan.fallback_shapes
        )
        if include_fallback_shapes
        else ()
    )
    nets = tuple(dict.fromkeys(net for inst in plan.instances for net in inst.connections.values() if net))
    oa_plan = OaWritePlan(OaCellView(lib, cell, view, "maskLayout"), nets=nets, instances=instances, rects=rects)
    return snap_oa_write_plan_to_grid(oa_plan, pdk) if pdk is not None and snap_to_grid else oa_plan


def _snap_pcell_params_to_grid(params: Mapping[str, Any], pdk: PdkConfig) -> dict[str, Any]:
    grid_nm = _pcell_parameter_grid_nm(pdk)
    snapped: dict[str, Any] = {}
    for key, value in params.items():
        if _is_dimension_param(key) and isinstance(value, (float, int)) and value > 0:
            snapped[key] = _snap_um_to_nm_grid_nearest(float(value) * 1e6, grid_nm) * 1e-6
        else:
            snapped[key] = value
    return snapped


def _is_dimension_param(key: str) -> bool:
    return key.lower() in {"w", "l", "wf", "wfg", "width", "length"}


def _select_native_passive_realization_candidate(
    logical_name: str,
    pdk: PdkConfig,
    sizing: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    metadata = getattr(pdk, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return None
    realization = metadata.get("pcell_realization", {})
    if not isinstance(realization, Mapping):
        return None
    cfg = realization.get(str(logical_name), {})
    if not isinstance(cfg, Mapping):
        return None
    raw_candidates = cfg.get("candidates", ())
    candidates = [candidate for candidate in raw_candidates if isinstance(candidate, Mapping)]
    if not candidates:
        return None
    requested_name = str(
        sizing.get("pcell_realization_candidate", sizing.get("pcell_candidate", sizing.get("candidate", ""))) or ""
    )
    if requested_name:
        for candidate in candidates:
            if str(candidate.get("name", "")) == requested_name and _native_passive_candidate_preserves_electrical_value(
                str(logical_name),
                sizing,
                candidate,
                cfg,
            ):
                return candidate
        return None
    candidates = [
        candidate
        for candidate in candidates
        if _native_passive_candidate_preserves_electrical_value(str(logical_name), sizing, candidate, cfg)
    ]
    if not candidates:
        return None
    usable = [
        candidate
        for candidate in candidates
        if bool(candidate.get("pcell_calibre_usable_for_layout", candidate.get("lvs_clean", True)))
    ]
    if usable:
        candidates = usable
    requested_w = _passive_requested_dimension_um(sizing, ("W", "w", "width", "wr"))
    requested_l = _passive_requested_dimension_um(sizing, ("L", "l", "length", "lr"))

    def score(candidate: Mapping[str, Any]) -> tuple[float, float, str]:
        overrides = candidate.get("sizing_overrides", {})
        pcell_params = candidate.get("pcell_params", {})
        if not isinstance(overrides, Mapping):
            overrides = {}
        if not isinstance(pcell_params, Mapping):
            pcell_params = {}
        cand_w = _passive_requested_dimension_um(overrides, ("W", "w", "width", "wr"))
        if cand_w is None:
            cand_w = _passive_requested_dimension_um(pcell_params, ("W", "w", "width", "wr", "sumW"))
        cand_l = _passive_requested_dimension_um(overrides, ("L", "l", "length", "lr"))
        if cand_l is None:
            cand_l = _passive_requested_dimension_um(pcell_params, ("L", "l", "length", "lr", "sumL"))
        distance = 0.0
        if requested_w is not None and cand_w is not None:
            distance += abs(log10(max(cand_w, 1e-12) / max(requested_w, 1e-12)))
        if requested_l is not None and cand_l is not None:
            distance += abs(log10(max(cand_l, 1e-12) / max(requested_l, 1e-12)))
        try:
            cost = float(candidate.get("cost", 0.0) or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        return (distance, cost, str(candidate.get("name", "")))

    return min(candidates, key=score)


def _native_passive_candidate_preserves_electrical_value(
    logical_name: str,
    sizing: Mapping[str, Any],
    candidate: Mapping[str, Any],
    cfg: Mapping[str, Any],
) -> bool:
    """Reject native passive candidates that would change frontend R/C.

    PCell calibration catalogs often contain only a few clean probe values
    (for example 1k resistors or 1f capacitors).  Those values are useful
    templates, but they are not valid replacements for an arbitrary sized
    frontend device unless the sizing explicitly permits an electrical override.
    """

    if _truthy(sizing.get("allow_passive_electrical_override", False)) or _truthy(
        cfg.get("allow_electrical_override", False)
    ):
        return True
    overrides = candidate.get("sizing_overrides", {})
    if not isinstance(overrides, Mapping):
        return True
    keys = ("R", "r") if str(logical_name).lower() == "resistor" else ("C", "c") if str(logical_name).lower() == "capacitor" else ()
    if not keys:
        return True
    requested = _first_float_for_keys(sizing, keys)
    realized = _first_float_for_keys(overrides, keys)
    if requested is None or realized is None:
        return True
    rel_tol = _positive_float_local(cfg.get("electrical_value_relative_tolerance", 1e-6), 1e-6)
    abs_tol = _positive_float_local(cfg.get("electrical_value_absolute_tolerance", 0.0), 0.0)
    return abs(requested - realized) <= max(abs(requested) * rel_tol, abs_tol)


def _first_float_for_keys(row: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in row:
            continue
        try:
            return float(row[key])
        except (TypeError, ValueError):
            return None
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no", "none"}
    return bool(value)


def _positive_float_local(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if result > 0.0 else float(default)


def _passive_requested_dimension_um(params: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in params:
            return _dimension_token_um(params[key])
        um_key = f"{key}_um"
        if um_key in params:
            return float(params[um_key])
        nm_key = f"{key}_nm"
        if nm_key in params:
            return float(params[nm_key]) * 1e-3
    return None


def _dimension_token_um(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("physical dimensions must be numeric")
    if isinstance(value, str):
        text = value.strip().lower()
        if text.endswith("um"):
            return float(text[:-2])
        if text.endswith("u"):
            return float(text[:-1])
        if text.endswith("nm"):
            return float(text[:-2]) * 1e-3
        if text.endswith("n"):
            return float(text[:-1]) * 1e-3
        if text.endswith("m"):
            return float(text[:-1]) * 1e6
        return coerce_dimension_m(float(text)) * 1e6
    return coerce_dimension_m(float(value)) * 1e6


def _should_use_drawn_passive_primitive(logical_name: str, pdk: PdkConfig, sizing: Mapping[str, Any] | None = None) -> bool:
    sizing = dict(sizing or {})
    for key in ("use_drawn_primitive", "use_drawn_passive_primitive"):
        if key in sizing:
            return bool(sizing[key])
    logical = str(logical_name).lower()
    if logical not in {"resistor", "capacitor"}:
        return False
    metadata = getattr(pdk, "metadata", {}) or {}
    generation_meta = dict(metadata.get("pcell_generation", {}) or {}) if isinstance(metadata, Mapping) else {}
    if bool(generation_meta.get("native_passive_pcell_by_default", False)):
        return False
    return str(getattr(pdk, "name", "")).lower() == "crn28hpcp"


def _is_drawn_primitive_instance(instance: PCellInstancePlan) -> bool:
    return str(getattr(instance, "instantiation_method", "")) == "drawn_primitive"


def _finger_choice_for_device(device: Device, sizing: Mapping[str, Any], objective: str) -> FingerChoice | None:
    logical = logical_pcell_name(device)
    if logical not in {"nmos", "pmos"}:
        return None
    width_m = _dimension_m(sizing, ("W", "w", "width"), 1e-6)
    length_m = _dimension_m(sizing, ("L", "l", "length"), 0.18e-6)
    if "nf" in sizing and "m" in sizing:
        nf = max(1, int(sizing["nf"]))
        m = max(1, int(sizing["m"]))
        wf = width_m / (nf * m)
        return FingerChoice(nf, m, wf, width_m, length_m, objective, 0.0, 1.0 / (nf * m), (nf + 1) * m * wf, sqrt(width_m * length_m * nf * m))
    return select_mos_fingers(width_m=width_m, length_m=length_m, objective=objective)


def _logical_params_for_validation(device: Device, sizing: Mapping[str, Any], finger_choice: FingerChoice | None) -> dict[str, Any]:
    logical = logical_pcell_name(device)
    if logical in {"nmos", "pmos"}:
        width_m = _dimension_m(sizing, ("W", "w", "width"), 1e-6)
        length_m = _dimension_m(sizing, ("L", "l", "length"), 0.18e-6)
        params = {"W": width_m, "L": length_m}
        if finger_choice is not None:
            params.update({"nf": finger_choice.nf, "m": finger_choice.m, "wf": finger_choice.finger_width_m})
        return params
    if logical == "bjt":
        return {"M": int(sizing.get("M", sizing.get("m", 1)) or 1)}
    if logical == "resistor":
        value = float(sizing.get("R", sizing.get("r", device.parameters.get("r_ohm", 1e3))))
        return {"R": value, "W": _dimension_m(sizing, ("W", "w", "width"), 0.5e-6)}
    if logical == "capacitor":
        value = float(sizing.get("C", sizing.get("c", device.parameters.get("c_f", 1e-15))))
        return {"C": value}
    return dict(sizing)


def _dimension_m(sizing: Mapping[str, Any], keys: tuple[str, ...], default_m: float) -> float:
    for key in keys:
        if key in sizing:
            return coerce_dimension_m(float(sizing[key]))
        nm_key = f"{key}_nm"
        if nm_key in sizing:
            return float(sizing[nm_key]) * 1e-9
        um_key = f"{key}_um"
        if um_key in sizing:
            return float(sizing[um_key]) * 1e-6
    return default_m


def _dimension_um_pair(
    sizing: Mapping[str, Any],
    width_keys: tuple[str, ...],
    height_keys: tuple[str, ...],
    default: tuple[float, float],
) -> tuple[float, float]:
    width = default[0]
    height = default[1]
    for key in width_keys:
        if key in sizing:
            width = float(sizing[key])
            break
    for key in height_keys:
        if key in sizing:
            height = float(sizing[key])
            break
    return (max(0.1, width), max(0.1, height))


def _coerce_dimension_m(value: float) -> float:
    return coerce_dimension_m(value)


def _placement_map(placements: Sequence[Placement] | Mapping[str, tuple[float, float] | Placement] | None) -> dict[str, Placement]:
    if placements is None:
        return {}
    if isinstance(placements, Mapping):
        result = {}
        for name, value in placements.items():
            if isinstance(value, Placement):
                result[name] = value
            else:
                x, y = value
                result[name] = Placement(name, float(x), float(y))
        return result
    result = {placement.name: placement for placement in placements}
    for name, placement in _device_level_placements(tuple(result.values())).items():
        result.setdefault(name, placement)
    return result


def _placement_for_device(name: str, placement_map: dict[str, Placement], x_default: float) -> tuple[float, float, str]:
    placement = placement_map.get(name)
    if placement is None:
        return (x_default, 0.0, "R0")
    return (placement.x_um, placement.y_um, placement.orient)


def _device_level_placements(placements: Sequence[Placement]) -> dict[str, Placement]:
    groups: dict[str, list[Placement]] = {}
    explicit: dict[str, Placement] = {}
    for placement in placements:
        device_name = _placement_device_name(placement)
        if not device_name:
            continue
        groups.setdefault(device_name, []).append(placement)
        if placement.name == device_name:
            explicit[device_name] = placement

    resolved: dict[str, Placement] = {}
    for device_name, members in groups.items():
        direct = explicit.get(device_name)
        if direct is not None:
            resolved[device_name] = direct
            continue
        x_um = sum(member.x_um for member in members) / float(len(members))
        y_um = sum(member.y_um for member in members) / float(len(members))
        role = next((member.role for member in members if member.role and member.role != "dummy"), device_name)
        resolved[device_name] = Placement(device_name, x_um, y_um, _dominant_orient(members), role)
    return resolved


def _placement_device_name(placement: Placement) -> str:
    role = str(placement.role or "")
    if role and role != "dummy":
        return role
    if placement.name.startswith("DUMMY_"):
        return ""
    parent = _unit_parent_name(placement.name)
    if parent:
        return parent
    return placement.name


def _dominant_orient(placements: Sequence[Placement]) -> str:
    counts: dict[str, int] = {}
    for placement in placements:
        orient = str(placement.orient or "R0")
        counts[orient] = counts.get(orient, 0) + 1
    if not counts:
        return "R0"
    return max(counts.items(), key=lambda item: (item[1], item[0] == "R0"))[0]


def _unit_parent_name(name: str) -> str:
    base, sep, suffix = name.rpartition("_u")
    if sep and suffix.isdigit():
        return base
    return ""


def _finger_score(objective: str, nf: int, m: int, unit_count: int, gate_r: float, diffusion_c: float, matching: float, width_penalty: float) -> float:
    if objective == "area":
        return -0.9 * unit_count - 0.1 * width_penalty
    if objective == "matching":
        return 10.0 * matching - 0.02 * diffusion_c * 1e6 + 0.1 * unit_count
    if objective == "speed":
        return -5.0 * gate_r - 0.4 * diffusion_c * 1e6 - 0.2 * width_penalty
    if objective == "noise":
        return 8.0 * matching + 0.05 * unit_count - 0.1 * width_penalty
    return -2.0 * gate_r - 0.2 * diffusion_c * 1e6 - 0.4 * width_penalty - 0.01 * abs(nf - m)


def _metric_sizing_scale_and_action(assessment: Any, scale_up: float, scale_down: float) -> tuple[float, str]:
    value = float(getattr(assessment, "value", 0.0))
    minimum = getattr(assessment, "minimum", None)
    maximum = getattr(assessment, "maximum", None)
    if minimum is not None and value < float(minimum):
        return (scale_up, "increase_device_width")
    if maximum is not None and value > float(maximum):
        return (scale_down, "reduce_device_width")
    return (scale_up, "adjust_device_width")


def _metric_sizing_reason(assessment: Any) -> str:
    name = str(getattr(assessment, "name", "metric"))
    value = float(getattr(assessment, "value", 0.0))
    minimum = getattr(assessment, "minimum", None)
    maximum = getattr(assessment, "maximum", None)
    if minimum is not None and value < float(minimum):
        return f"{name}={value:g} below minimum {float(minimum):g}"
    if maximum is not None and value > float(maximum):
        return f"{name}={value:g} above maximum {float(maximum):g}"
    return f"{name}={value:g} outside target"


def _metric_sizing_priority(assessment: Any) -> int:
    margin = getattr(assessment, "margin", None)
    if margin is None:
        return 50
    return 80 if float(margin) < 0 else 50


def _metric_finger_objective(metric: str) -> str:
    lowered = metric.lower()
    if any(token in lowered for token in ("offset", "mismatch", "noise")):
        return "matching"
    if any(token in lowered for token in ("bw", "bandwidth", "delay", "speed", "settling")):
        return "speed"
    if any(token in lowered for token in ("area", "power", "current")):
        return "area"
    return "balanced"


def _mos_bbox_for_finger_choice(choice: FingerChoice) -> tuple[float, float]:
    length_um = choice.length_m * 1e6
    finger_pitch = max(0.28, length_um + 0.32)
    width_um = max(0.6, (choice.unit_count + 1) * finger_pitch)
    height_um = max(0.5, choice.finger_width_m * 1e6 + 0.4)
    return width_um, height_um


def _relative_abs_error(value: float, target: float | None) -> float:
    if target is None:
        return 0.0
    denom = max(abs(float(target)), 1e-12)
    return abs(float(value) - float(target)) / denom


def _relative_excess(value: float, limit: float | None) -> float:
    if limit is None:
        return 0.0
    denom = max(abs(float(limit)), 1e-12)
    return max(0.0, float(value) - float(limit)) / denom


def _normalize_cost_rows(rows: list[dict[str, float]]) -> list[dict[str, float]]:
    if not rows:
        return []
    keys = tuple(rows[0])
    bounds: dict[str, tuple[float, float]] = {}
    for key in keys:
        values = [row[key] for row in rows]
        bounds[key] = (min(values), max(values))
    normalized: list[dict[str, float]] = []
    for row in rows:
        next_row: dict[str, float] = {}
        for key in keys:
            lo, hi = bounds[key]
            span = hi - lo
            next_row[key] = 0.0 if abs(span) <= 1e-18 else (row[key] - lo) / span
        normalized.append(next_row)
    return normalized


def _finger_tiebreak(choice: FingerChoice | None) -> tuple[int, int, float]:
    if choice is None:
        return (10**9, 10**9, 10**9.0)
    return (choice.m, choice.nf, choice.finger_width_m)


def _mos_fallback_shapes(instance: PCellInstancePlan, pdk: PdkConfig) -> tuple[LayoutShape, ...]:
    width = max(instance.width_um, 0.2)
    height = max(instance.height_um, 0.2)
    active = pdk.layer_map.active
    gate = pdk.layer_map.gate
    metal = pdk.layer_map.metals[0]
    nf = instance.finger_choice.nf if instance.finger_choice is not None else int(instance.params.get("nf", 1) or 1)
    m = instance.finger_choice.m if instance.finger_choice is not None else int(instance.params.get("m", 1) or 1)
    gate_count = max(1, nf * m)
    pitch = width / (gate_count + 1)
    gate_w = max(0.04, min(0.12, pitch * 0.35))
    shapes: list[LayoutShape] = [LayoutShape(f"{instance.name}_od", active, _absolute_bbox(instance, (0.0, 0.0, width, height)), "")]
    gate_net = instance.connections.get("G", instance.connections.get("PLUS", ""))
    source_net = instance.connections.get("S", instance.connections.get("MINUS", ""))
    drain_net = instance.connections.get("D", instance.connections.get("PLUS", ""))
    sd_w = max(0.08, pitch * 0.35)
    for idx in range(gate_count):
        gx = (idx + 1) * pitch
        shapes.append(
            LayoutShape(
                f"{instance.name}_po{idx}",
                gate,
                _absolute_bbox(instance, (gx - gate_w / 2, -0.08, gx + gate_w / 2, height + 0.08)),
                gate_net,
            )
        )
    for idx in range(gate_count + 1):
        sx = idx * pitch
        net = source_net if idx % 2 == 0 else drain_net
        shapes.append(
            LayoutShape(
                f"{instance.name}_sd{idx}",
                metal,
                _absolute_bbox(instance, (sx - sd_w / 2, 0.05, sx + sd_w / 2, height - 0.05)),
                net,
            )
        )
    body_net = instance.connections.get("B", "")
    if body_net:
        shapes.append(LayoutShape(f"{instance.name}_body", metal, _absolute_bbox(instance, (0.0, -0.18, width, -0.08)), body_net))
    return tuple(shapes)


def _bjt_fallback_shapes(instance: PCellInstancePlan, pdk: PdkConfig) -> tuple[LayoutShape, ...]:
    width = max(instance.width_um, 0.2)
    height = max(instance.height_um, 0.2)
    active = pdk.layer_map.active
    metal = pdk.layer_map.metals[0]
    collector = instance.connections.get("C", "")
    base = instance.connections.get("B", "")
    emitter = instance.connections.get("E", "")
    return (
        LayoutShape(f"{instance.name}_od", active, _absolute_bbox(instance, (0.0, 0.0, width, height)), ""),
        LayoutShape(f"{instance.name}_c", metal, _absolute_bbox(instance, (width * 0.35, height * 0.75, width * 0.65, height)), collector),
        LayoutShape(f"{instance.name}_b", metal, _absolute_bbox(instance, (0.0, height * 0.35, width * 0.25, height * 0.65)), base),
        LayoutShape(f"{instance.name}_e", metal, _absolute_bbox(instance, (width * 0.35, 0.0, width * 0.65, height * 0.25)), emitter),
    )


def _resistor_fallback_shapes(instance: PCellInstancePlan, pdk: PdkConfig) -> tuple[LayoutShape, ...]:
    metal = pdk.layer_map.metals[0]
    body_layer = pdk.layer_map.gate
    width = max(instance.width_um, 0.3)
    height = max(instance.height_um, 0.18)
    pad = max(0.08, min(0.16, 0.35 * width))
    body_y0 = height * 0.15
    body_y1 = height * 0.85
    return (
        LayoutShape(f"{instance.name}_body", body_layer, _absolute_bbox(instance, (pad, body_y0, max(pad, width - pad), body_y1)), ""),
        LayoutShape(f"{instance.name}_plus", metal, _absolute_bbox(instance, (0.0, 0.0, pad, height)), instance.connections.get("PLUS", "")),
        LayoutShape(f"{instance.name}_minus", metal, _absolute_bbox(instance, (max(0.0, width - pad), 0.0, width, height)), instance.connections.get("MINUS", "")),
    )


def _capacitor_fallback_shapes(instance: PCellInstancePlan, pdk: PdkConfig) -> tuple[LayoutShape, ...]:
    if str(instance.params.get("__drawn_capacitor_style", "")).strip().lower() == "mom":
        return _mom_capacitor_fallback_shapes(instance, pdk)
    bot = pdk.layer_map.metals[0]
    top = pdk.layer_map.metals[min(1, len(pdk.layer_map.metals) - 1)]
    width = max(instance.width_um, 0.4)
    height = max(instance.height_um, 0.4)
    inset = max(0.06, min(min(width, height) * 0.18, 0.14))
    return (
        LayoutShape(f"{instance.name}_bot", bot, _absolute_bbox(instance, (0.0, 0.0, width, height)), instance.connections.get("MINUS", "")),
        LayoutShape(f"{instance.name}_top", top, _absolute_bbox(instance, (inset, inset, max(inset, width - inset), max(inset, height - inset))), instance.connections.get("PLUS", "")),
    )


def _mom_capacitor_fallback_shapes(instance: PCellInstancePlan, pdk: PdkConfig) -> tuple[LayoutShape, ...]:
    """Lower a compact two-terminal interdigitated MOM capacitor.

    The geometry is deliberately described by PDK-aware width/spacing values
    and explicit MOM recognition markers rather than by a hidden hard-coded
    GDS macro.  It is a usable preview/fallback when the legacy CRN28
    Ciranova PyCell runtime cannot be loaded by the installed Virtuoso/OA
    version.  Sign-off still requires calibration against the official PCell.
    """

    metals = tuple(pdk.layer_map.metals)
    if not metals:
        return ()
    try:
        start_number = max(1, int(instance.params.get("__mom_start_metal", 4)))
        stop_number = max(start_number, int(instance.params.get("__mom_stop_metal", min(8, len(metals)))))
    except (TypeError, ValueError):
        start_number, stop_number = 4, min(8, len(metals))
    start_number = min(start_number, len(metals))
    stop_number = min(stop_number, len(metals))
    selected = tuple((number, metals[number - 1]) for number in range(start_number, stop_number + 1))

    width = max(float(instance.width_um), 1.0)
    height = max(float(instance.height_um), 1.0)
    finger_width = max(
        float(instance.params.get("__mom_finger_width_um", 0.05) or 0.05),
        max(pdk.rules.min_width_um(layer) for _, layer in selected),
    )
    finger_spacing = max(
        float(instance.params.get("__mom_finger_spacing_um", 0.05) or 0.05),
        max(pdk.rules.min_spacing_um(layer) for _, layer in selected),
    )
    bus_width = max(float(instance.params.get("__mom_bus_width_um", 0.30) or 0.30), finger_width)
    edge = max(float(instance.params.get("__mom_edge_margin_um", 0.20) or 0.20), finger_spacing)
    pitch = finger_width + finger_spacing
    usable_width = max(width - 2.0 * edge, finger_width)
    finger_count = max(2, int((usable_width + finger_spacing) // pitch))
    active_width = finger_count * pitch - finger_spacing
    x0 = edge + max((usable_width - active_width) / 2.0, 0.0)
    bottom_bus = (edge, edge, width - edge, edge + bus_width)
    top_bus = (edge, height - edge - bus_width, width - edge, height - edge)
    plus_y0 = edge + 0.5 * bus_width
    plus_y1 = height - edge - bus_width - finger_spacing
    minus_y0 = edge + bus_width + finger_spacing
    minus_y1 = height - edge - 0.5 * bus_width
    plus_net = instance.connections.get("PLUS", "")
    minus_net = instance.connections.get("MINUS", "")

    shapes: list[LayoutShape] = []
    for metal_number, layer in selected:
        shapes.append(LayoutShape(f"{instance.name}_{layer}_plus_bus", layer, _absolute_bbox(instance, bottom_bus), plus_net))
        shapes.append(LayoutShape(f"{instance.name}_{layer}_minus_bus", layer, _absolute_bbox(instance, top_bus), minus_net))
        for index in range(finger_count):
            fx0 = x0 + index * pitch
            fx1 = fx0 + finger_width
            if index % 2 == 0:
                bbox = (fx0, plus_y0, fx1, plus_y1)
                net = plus_net
                suffix = "plus"
            else:
                bbox = (fx0, minus_y0, fx1, minus_y1)
                net = minus_net
                suffix = "minus"
            shapes.append(
                LayoutShape(
                    f"{instance.name}_{layer}_{suffix}_finger_{index}",
                    layer,
                    _absolute_bbox(instance, bbox),
                    net,
                )
            )

        # One via on each terminal bus is sufficient to stitch the metal
        # stack electrically while leaving most of the MOM edge available for
        # routing access.
        if metal_number < stop_number and metal_number - 1 < len(pdk.layer_map.vias):
            via_layer = pdk.layer_map.vias[metal_number - 1]
            via_size = max(pdk.rules.min_width_um(via_layer), finger_width)
            plus_via = (
                edge + 0.5 * bus_width - 0.5 * via_size,
                edge + 0.5 * bus_width - 0.5 * via_size,
                edge + 0.5 * bus_width + 0.5 * via_size,
                edge + 0.5 * bus_width + 0.5 * via_size,
            )
            minus_via = (
                width - edge - 0.5 * bus_width - 0.5 * via_size,
                height - edge - 0.5 * bus_width - 0.5 * via_size,
                width - edge - 0.5 * bus_width + 0.5 * via_size,
                height - edge - 0.5 * bus_width + 0.5 * via_size,
            )
            shapes.append(LayoutShape(f"{instance.name}_{via_layer}_plus", via_layer, _absolute_bbox(instance, plus_via), plus_net))
            shapes.append(LayoutShape(f"{instance.name}_{via_layer}_minus", via_layer, _absolute_bbox(instance, minus_via), minus_net))

    marker_bbox = _absolute_bbox(instance, (0.0, 0.0, width, height))
    shapes.append(LayoutShape(f"{instance.name}_mom_region", "MOMDMY", marker_bbox, "", {"purpose": "drawing"}))
    shapes.append(LayoutShape(f"{instance.name}_mom_2t", "MOMDMY", marker_bbox, "", {"purpose": "drawing27"}))
    for metal_number, _layer in selected:
        shapes.append(
            LayoutShape(
                f"{instance.name}_mom_m{metal_number}",
                "MOMDMY",
                marker_bbox,
                "",
                {"purpose": f"drawing{metal_number}"},
            )
        )
    return tuple(shapes)


def _rect_device_shapes(instance: PCellInstancePlan, layer: str, plus_terminal: str, minus_terminal: str) -> tuple[LayoutShape, ...]:
    x, y = instance.xy_um
    w, h = instance.width_um, instance.height_um
    plus = instance.connections.get(plus_terminal, "")
    minus = instance.connections.get(minus_terminal, "")
    min_gap = max(0.02, min(w * 0.2, 0.08))
    terminal_w = min(max(w * 0.2, 0.02), max((w - min_gap) / 2.0, 0.0), 0.2)
    if terminal_w <= 0:
        terminal_w = max(w / 3.0, 0.001)
    left_x1 = min(x + terminal_w, x + w)
    right_x0 = max(x + w - terminal_w, left_x1 + min_gap)
    right_x0 = min(right_x0, x + w)
    return (
        LayoutShape(f"{instance.name}_body", layer, (x, y, x + w, y + h), ""),
        LayoutShape(f"{instance.name}_plus", layer, (x, y, left_x1, y + h), plus),
        LayoutShape(f"{instance.name}_minus", layer, (right_x0, y, x + w, y + h), minus),
    )


def _absolute_xy(origin: tuple[float, float], local: tuple[float, float], orient: str) -> tuple[float, float]:
    x, y = local
    if orient == "R0":
        dx, dy = x, y
    elif orient == "R90":
        dx, dy = -y, x
    elif orient == "R180":
        dx, dy = -x, -y
    elif orient == "R270":
        dx, dy = y, -x
    elif orient == "MX":
        dx, dy = x, -y
    elif orient == "MY":
        dx, dy = -x, y
    elif orient == "MXR90":
        dx, dy = y, x
    elif orient == "MYR90":
        dx, dy = -y, -x
    else:
        raise ValueError(f"unsupported orientation {orient!r}")
    return (origin[0] + dx, origin[1] + dy)


def _absolute_bbox(instance: PCellInstancePlan, bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    points = (
        _absolute_xy(instance.xy_um, (bbox[0], bbox[1]), instance.orient),
        _absolute_xy(instance.xy_um, (bbox[0], bbox[3]), instance.orient),
        _absolute_xy(instance.xy_um, (bbox[2], bbox[1]), instance.orient),
        _absolute_xy(instance.xy_um, (bbox[2], bbox[3]), instance.orient),
    )
    xs = tuple(point[0] for point in points)
    ys = tuple(point[1] for point in points)
    return (min(xs), min(ys), max(xs), max(ys))
