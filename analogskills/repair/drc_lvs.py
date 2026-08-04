"""Local DRC/LVS repair classifiers and operators."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from analogskills.pdk import DesignRuleDeck, PdkConfig


@dataclass(frozen=True)
class LayoutShape:
    id: str
    layer: str
    bbox: tuple[float, float, float, float]
    net: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _routing_guides_for_net(plan: object, net: str) -> tuple[dict[str, object], ...]:
    metadata = getattr(plan, "metadata", {})
    if not isinstance(metadata, Mapping):
        return ()
    by_net = metadata.get("routing_guides_by_net", {})
    if not isinstance(by_net, Mapping):
        return ()
    hints = by_net.get(str(net), ())
    return tuple(dict(item) for item in tuple(hints or ()) if isinstance(item, Mapping))


def snap_shape_to_grid(shape: LayoutShape, grid: DesignRuleDeck | PdkConfig | int, *, mode: str = "outward") -> LayoutShape:
    rules = _grid_rules(grid)
    return LayoutShape(shape.id, shape.layer, rules.snap_bbox_um(shape.bbox, mode=mode), shape.net, dict(shape.metadata))


def snap_shapes_to_grid(shapes: list[LayoutShape] | tuple[LayoutShape, ...], grid: DesignRuleDeck | PdkConfig | int, *, mode: str = "outward") -> tuple[LayoutShape, ...]:
    return tuple(snap_shape_to_grid(shape, grid, mode=mode) for shape in shapes)


def layout_shapes_from_plan(
    plan: object,
    *,
    pdk: PdkConfig | None = None,
    include_pins: bool = False,
    include_vias: bool = True,
    include_via_cuts: bool = False,
) -> tuple[LayoutShape, ...]:
    """Lower a layout plan into repair-ready same-layer boxes.

    Routed paths are expanded into per-segment rectangles so downstream DRC/LVS
    repair operates on the same conductive geometry that physical verification
    sees during shape-based checks.
    """

    from analogskills.layout.physical import collect_plan_shapes

    rows = collect_plan_shapes(
        plan,
        include_pins=include_pins,
        include_vias=include_vias,
        pdk=pdk,
    )
    lowered = [
        LayoutShape(str(getattr(shape, "source", f"shape[{idx}]")), str(shape.layer), _bbox_tuple(shape.bbox), str(shape.net))
        for idx, shape in enumerate(rows)
    ]
    if include_via_cuts and pdk is not None:
        lowered.extend(_layout_via_cut_shapes(plan, pdk))
    return tuple(lowered)


def _layout_via_cut_shapes(plan: object, pdk: PdkConfig) -> tuple[LayoutShape, ...]:
    rows: list[LayoutShape] = []
    for via_index, via in enumerate(getattr(plan, "vias", ())):
        via_def = str(getattr(via, "via_def", "") or "")
        net = str(getattr(via, "net", "") or "")
        if not via_def:
            continue
        for cut_index, bbox in enumerate(_via_cut_bboxes_for_repair(via, pdk)):
            rows.append(
                LayoutShape(
                    f"via[{via_index}].cut[{cut_index}]",
                    via_def,
                    bbox,
                    net,
                    metadata={
                        "kind": "via_cut",
                        "via_def": via_def,
                        "via_index": via_index,
                        "cut_index": cut_index,
                        "rows": getattr(via, "rows", 1),
                        "cols": getattr(via, "cols", 1),
                    },
                )
            )
    return tuple(rows)


def _via_cut_bboxes_for_repair(via: object, pdk: PdkConfig) -> tuple[tuple[float, float, float, float], ...]:
    via_def = str(getattr(via, "via_def", "") or "")
    if not via_def:
        return ()
    try:
        x, y = (float(value) for value in tuple(getattr(via, "xy", ()))[:2])
    except (TypeError, ValueError):
        return ()
    try:
        cut_width = float(pdk.rules.min_width_um(via_def))
    except (AttributeError, KeyError, TypeError, ValueError):
        return ()
    if cut_width <= 0.0:
        return ()
    try:
        rows = max(int(getattr(via, "rows", 1) or 1), 1)
    except (TypeError, ValueError):
        rows = 1
    try:
        cols = max(int(getattr(via, "cols", 1) or 1), 1)
    except (TypeError, ValueError):
        cols = 1
    metadata = getattr(via, "metadata", {}) if isinstance(getattr(via, "metadata", {}), Mapping) else {}
    if "emit_cut_array" in metadata:
        use_cut_array = bool(metadata.get("emit_cut_array", False))
    else:
        use_cut_array = rows > 1 or cols > 1
    if not use_cut_array:
        rows, cols = 1, 1
    try:
        cut_spacing = float(pdk.rules.array_spacing_um(via_def))
    except (AttributeError, KeyError, TypeError, ValueError):
        try:
            cut_spacing = float(pdk.rules.min_spacing_um(via_def))
        except (AttributeError, KeyError, TypeError, ValueError):
            cut_spacing = cut_width
    pitch = cut_width + max(cut_spacing, 0.0)
    half = 0.5 * cut_width
    x0 = x - 0.5 * float(cols - 1) * pitch
    y0 = y - 0.5 * float(rows - 1) * pitch
    bboxes: list[tuple[float, float, float, float]] = []
    for row in range(rows):
        cy = y0 + row * pitch
        for col in range(cols):
            cx = x0 + col * pitch
            bbox = (cx - half, cy - half, cx + half, cy + half)
            try:
                bbox = _bbox_tuple(pdk.rules.snap_bbox_um(bbox, mode="nearest"))
            except (AttributeError, TypeError, ValueError):
                bbox = _bbox_tuple(bbox)
            bboxes.append(bbox)
    return tuple(bboxes)


def validate_shapes_on_grid(shapes: list[LayoutShape] | tuple[LayoutShape, ...], grid: DesignRuleDeck | PdkConfig | int, *, tol_um: float = 1e-12) -> list[str]:
    rules = _grid_rules(grid)
    issues: list[str] = []
    labels = ("x0", "y0", "x1", "y1")
    for shape in shapes:
        for label, value in zip(labels, shape.bbox):
            if not rules.is_on_grid_um(value, tol_um=tol_um):
                issues.append(f"{shape.id}.{label}={value:g}um is off-grid for {rules.grid_nm}nm grid")
    return issues


@dataclass(frozen=True)
class GeometryEdit:
    action: str
    layer: str
    bbox: tuple[float, float, float, float] | None
    target_bbox: tuple[float, float, float, float] | None = None
    reason: str = ""
    net: str = ""
    shape_id: str = ""


@dataclass(frozen=True)
class DrcIssue:
    rule: str
    layer: str
    message: str
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class DrcEcoSuggestion:
    action: str
    rule: str
    layer: str
    bbox: tuple[float, float, float, float] | None = None
    reason: str = ""
    priority: int = 0
    params: dict[str, object] = field(default_factory=dict)
    owner: str = "manual"


@dataclass(frozen=True)
class DrcEcoPlan:
    suggestions: tuple[DrcEcoSuggestion, ...]
    geometry_edits: tuple[GeometryEdit, ...] = ()
    density_issues: tuple[DrcIssue, ...] = ()
    review_issues: tuple[DrcIssue, ...] = ()
    layout_proposal: object | None = None


@dataclass(frozen=True)
class DrcRepairCandidate:
    issues: tuple[DrcIssue, ...]
    plan_kind: str
    plan: object
    score: float
    passed: bool
    issues_after: tuple[str, ...] = ()
    repair_scope: "HierarchicalRepairScope | None" = None


@dataclass(frozen=True)
class DrcRepairProposal:
    eco_plan: DrcEcoPlan
    candidates: tuple[DrcRepairCandidate, ...] = ()
    selected_candidate: DrcRepairCandidate | None = None


@dataclass(frozen=True)
class DrcEcoComparison:
    before_count: int
    after_count: int
    fixed_rules: tuple[str, ...]
    new_rules: tuple[str, ...]
    remaining_rules: tuple[str, ...]
    rule_deltas: dict[str, int]
    improved: bool
    passed: bool
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DrcIssueLocalization:
    issue: DrcIssue
    shape: LayoutShape
    overlap_bbox: tuple[float, float, float, float]
    overlap_area: float
    issue_area: float
    shape_area: float
    score: float


@dataclass(frozen=True)
class HierarchicalRepairRegion:
    name: str
    kind: str
    bbox: tuple[float, float, float, float]
    parent: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class HierarchicalRepairScope:
    level: str
    target: str
    rationale: tuple[str, ...] = ()
    issue_bbox: tuple[float, float, float, float] | None = None
    region_bbox: tuple[float, float, float, float] | None = None
    confidence: float = 0.0
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class HierarchicalRepairTriage:
    kind: str
    scope: HierarchicalRepairScope
    issue_count: int = 0
    affected_nets: tuple[str, ...] = ()
    blocking_system_kinds: tuple[str, ...] = ()
    summary: tuple[str, ...] = ()
    provenance: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class DrcSpacingLocalization:
    issue: DrcIssue
    shape: LayoutShape
    peer_shape: LayoutShape
    distance: float
    required_spacing: float
    score: float


@dataclass(frozen=True)
class LocalizedDrcPatchPlan:
    edits: tuple[GeometryEdit, ...]
    layout_patch: object
    oa_patch: object
    physical_report: Mapping[str, object]
    interconnect_report: Mapping[str, object] | None = None
    merged_physical_report: Mapping[str, object] | None = None
    merged_interconnect_report: Mapping[str, object] | None = None


@dataclass(frozen=True)
class DrcReplacementPlan:
    edits: tuple[GeometryEdit, ...]
    replacement_layout: object
    replacement_oa_plan: object
    physical_report: Mapping[str, object]
    interconnect_report: Mapping[str, object] | None = None
    before_spacing_violations: tuple[tuple[str, str, float], ...] = ()
    after_spacing_violations: tuple[tuple[str, str, float], ...] = ()


@dataclass(frozen=True)
class LvsIssue:
    kind: str
    message: str
    net: str = ""


@dataclass(frozen=True)
class LvsEcoComparison:
    before_count: int
    after_count: int
    fixed: tuple[tuple[str, str], ...]
    new: tuple[tuple[str, str], ...]
    remaining: tuple[tuple[str, str], ...]
    issue_deltas: dict[tuple[str, str], int]
    improved: bool
    passed: bool
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class LvsEcoSuggestion:
    action: str
    owner: str
    net: str
    peer_nets: tuple[str, ...] = ()
    reason: str = ""
    priority: int = 0
    evidence: tuple[str, ...] = ()
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LvsRepairItem:
    issue: LvsIssue
    suggestion: LvsEcoSuggestion
    evidence: tuple[str, ...] = ()
    priority: int = 0


@dataclass(frozen=True)
class LvsRepairPlan:
    items: tuple[LvsRepairItem, ...]
    owners: dict[str, int] = field(default_factory=dict)
    actions: tuple[str, ...] = ()
    unresolved: tuple[LvsIssue, ...] = ()
    passed: bool = False


@dataclass(frozen=True)
class LvsShortReplacementPlan:
    edits: tuple[GeometryEdit, ...]
    replacement_layout: object
    replacement_oa_plan: object
    physical_report: Mapping[str, object]
    interconnect_report: Mapping[str, object] | None = None
    before_shorts: tuple[object, ...] = ()
    after_shorts: tuple[object, ...] = ()


@dataclass(frozen=True)
class LvsManualRepairHandoffPlan:
    action: str
    owner: str
    issue_message: str
    net: str = ""
    peer_nets: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    params: dict[str, object] = field(default_factory=dict)
    edits: tuple[GeometryEdit, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LvsRepairCandidate:
    item: LvsRepairItem
    plan_kind: str
    plan: object
    score: float
    passed: bool
    issues: tuple[str, ...] = ()
    repair_scope: HierarchicalRepairScope | None = None


@dataclass(frozen=True)
class LvsRepairProposal:
    repair_plan: LvsRepairPlan
    candidates: tuple[LvsRepairCandidate, ...] = ()
    selected_candidate: LvsRepairCandidate | None = None


@dataclass(frozen=True)
class PostLayoutEcoRepairProposal:
    """Executable post-layout ECO proposal backed by additive LayoutIR/OA patches."""

    kind: str
    layout_patch: object
    oa_patch: object
    score: float = 0.0
    passed: bool = False
    issues_after: tuple[str, ...] = ()
    hotspot_nets: tuple[str, ...] = ()
    source: str = "pex_layout_eco"
    metadata: dict[str, object] = field(default_factory=dict)


def propose_drc_repairs(issues: list[DrcIssue]) -> list[dict[str, object]]:
    repairs = []
    for issue in issues:
        text = f"{issue.rule} {issue.message}".lower()
        if "density" in text or "dummy" in text or "fill" in text:
            repairs.append({"action": "insert_density_or_dummy_fill", "layer": issue.layer, "bbox": issue.bbox, "reason": issue.message})
        elif "antenna" in text:
            repairs.append({"action": "add_antenna_protection_or_reduce_area", "layer": issue.layer, "bbox": issue.bbox, "reason": issue.message})
        elif "matching" in text or "match" in text:
            repairs.append({"action": "review_matching_constraints", "layer": issue.layer, "bbox": issue.bbox, "reason": issue.message})
        elif "esd" in text:
            repairs.append({"action": "review_esd_topology", "layer": issue.layer, "bbox": issue.bbox, "reason": issue.message})
        elif "width" in text:
            repairs.append({"action": "widen_shape", "layer": issue.layer, "bbox": issue.bbox, "reason": issue.message})
        elif "spacing" in text or "space" in text:
            repairs.append({"action": "move_or_reroute", "layer": issue.layer, "bbox": issue.bbox, "reason": issue.message})
        elif "enclosure" in text or "enc" in text:
            repairs.append({"action": "grow_enclosure", "layer": issue.layer, "bbox": issue.bbox, "reason": issue.message})
        elif "via" in text:
            repairs.append({"action": "replace_with_via_array", "layer": issue.layer, "bbox": issue.bbox, "reason": issue.message})
        else:
            repairs.append({"action": "manual_drc_review", "layer": issue.layer, "bbox": issue.bbox, "reason": issue.message})
    return repairs


def suggest_drc_ecos(
    issues: list[DrcIssue] | tuple[DrcIssue, ...],
    *,
    layer_aliases: Mapping[str, str] | None = None,
    max_suggestions: int | None = None,
) -> tuple[DrcEcoSuggestion, ...]:
    """Map DRC issues to reviewable ECO actions without editing geometry."""

    aliases = {str(key): str(value) for key, value in dict(layer_aliases or {}).items()}
    suggestions = tuple(_suggest_drc_eco_for_issue(issue, aliases.get(issue.layer, issue.layer)) for issue in issues)
    ranked = tuple(sorted(suggestions, key=lambda item: (-item.priority, item.action, item.rule)))
    return ranked if max_suggestions is None else ranked[:max_suggestions]


def localize_drc_issues_to_layout(
    issues: list[DrcIssue] | tuple[DrcIssue, ...],
    layout_plan: object,
    *,
    layer_aliases: Mapping[str, str] | None = None,
    top_k: int | None = None,
) -> tuple[DrcIssueLocalization, ...]:
    """Map DRC issue bboxes to overlapping LayoutIR/OA-style layout shapes."""

    aliases = {str(key): str(value) for key, value in dict(layer_aliases or {}).items()}
    shapes = _layout_plan_shapes(layout_plan)
    rows: list[DrcIssueLocalization] = []
    for issue in issues:
        bbox = _float_bbox(issue.bbox)
        if bbox is None:
            continue
        issue_layer = aliases.get(issue.layer, issue.layer)
        issue_area = rect_area(bbox)
        for shape in shapes:
            if issue_layer and shape.layer and shape.layer != issue_layer:
                continue
            overlap = rect_intersection(shape.bbox, bbox)
            if overlap is None:
                continue
            overlap_area = rect_area(overlap)
            shape_area = rect_area(shape.bbox)
            issue_fraction = overlap_area / issue_area if issue_area > 0.0 else 0.0
            shape_fraction = overlap_area / shape_area if shape_area > 0.0 else 0.0
            score = overlap_area + issue_fraction + 0.25 * shape_fraction
            rows.append(DrcIssueLocalization(issue, shape, overlap, overlap_area, issue_area, shape_area, score))
    ranked = tuple(sorted(rows, key=lambda item: (-item.score, item.issue.rule, item.shape.id)))
    return ranked if top_k is None else ranked[:top_k]


def localize_spacing_drc_issues_to_layout(
    issues: list[DrcIssue] | tuple[DrcIssue, ...],
    layout_plan: object,
    *,
    min_spacing_by_layer: Mapping[str, float],
    layer_aliases: Mapping[str, str] | None = None,
    top_k: int | None = None,
) -> tuple[DrcSpacingLocalization, ...]:
    """Map spacing DRC bboxes to the most likely same-layer shape pairs."""

    aliases = {str(key): str(value) for key, value in dict(layer_aliases or {}).items()}
    min_spacings = {str(layer): float(value) for layer, value in dict(min_spacing_by_layer).items()}
    shapes = _layout_plan_shapes(layout_plan)
    rows: list[DrcSpacingLocalization] = []
    for issue in issues:
        text = f"{issue.rule} {issue.message}".lower()
        rule = issue.rule.upper()
        if not _is_spacing_rule(rule, text):
            continue
        bbox = _float_bbox(issue.bbox)
        if bbox is None:
            continue
        layer = aliases.get(issue.layer, issue.layer)
        required = min_spacings.get(layer, 0.0)
        if required <= 0.0:
            continue
        candidates = tuple(shape for shape in shapes if shape.layer == layer and _bbox_overlaps(_expand_bbox(shape.bbox, required), bbox))
        for idx, shape in enumerate(candidates):
            for peer in candidates[idx + 1 :]:
                if shape.net and peer.net and shape.net == peer.net:
                    continue
                distance = _rect_axis_distance(shape.bbox, peer.bbox)
                if distance >= required:
                    continue
                evidence = rect_area(rect_intersection(_expand_bbox(shape.bbox, required) or shape.bbox, bbox) or bbox)
                score = (required - distance) + evidence
                rows.append(DrcSpacingLocalization(issue, shape, peer, distance, required, score))
    ranked = tuple(sorted(rows, key=lambda item: (-item.score, item.issue.rule, item.shape.id, item.peer_shape.id)))
    return ranked if top_k is None else ranked[:top_k]


def plan_localized_drc_patches(
    localizations: list[DrcIssueLocalization] | tuple[DrcIssueLocalization, ...],
    *,
    min_width_by_layer: Mapping[str, float] | None = None,
    min_area_by_layer: Mapping[str, float] | None = None,
) -> tuple[GeometryEdit, ...]:
    """Create narrow geometry patches for localized metal width/min-area DRCs."""

    min_widths = {str(layer): float(value) for layer, value in dict(min_width_by_layer or {}).items()}
    min_areas = {str(layer): float(value) for layer, value in dict(min_area_by_layer or {}).items()}
    ranked_localizations = _select_localized_patch_targets(localizations)
    edits: list[GeometryEdit] = []
    seen: set[tuple[str, str, tuple[float, float, float, float] | None, tuple[float, float, float, float] | None]] = set()
    for item in ranked_localizations:
        issue = item.issue
        shape = item.shape
        text = f"{issue.rule} {issue.message}".lower()
        target: tuple[float, float, float, float] | None = None
        action = ""
        if _is_width_rule(issue.rule.upper(), text):
            target = _widen_bbox(shape.bbox, min_widths.get(shape.layer, 0.0))
            action = "widen_shape"
        elif _is_area_or_antenna_rule(issue.rule.upper(), text) or "min-area" in text or "minimum area" in text:
            target = _grow_bbox_to_min_area(shape.bbox, min_areas.get(shape.layer, 0.0))
            action = "grow_min_area_shape"
        if target is None or target == shape.bbox or not action:
            continue
        key = (action, shape.layer, shape.bbox, target)
        if key in seen:
            continue
        seen.add(key)
        edits.append(GeometryEdit(action, shape.layer, shape.bbox, target, issue.message, shape.net, shape.id))
    return tuple(edits)


def _select_localized_patch_targets(
    localizations: list[DrcIssueLocalization] | tuple[DrcIssueLocalization, ...],
) -> tuple[DrcIssueLocalization, ...]:
    """Reduce noisy min-area localizations to one conductor edit per issue.

    Min-area checks are performed on merged conductive islands.  The same issue
    bbox can therefore overlap the real route segment plus several tiny via
    landing/contact rectangles.  Growing every overlapped rectangle often fixes
    the area number but creates shorts and spacing failures.  For area-like
    rules, choose one representative conductor: path segments first, then larger
    shapes.  Width rules still keep all localized shapes because the actual
    narrow primitive may be a rect.
    """

    selected: list[DrcIssueLocalization] = []
    area_groups: dict[tuple[str, str, str, tuple[float, float, float, float] | None], list[DrcIssueLocalization]] = {}
    for item in localizations:
        issue = item.issue
        text = f"{issue.rule} {issue.message}".lower()
        if _is_area_or_antenna_rule(issue.rule.upper(), text) or "min-area" in text or "minimum area" in text:
            area_groups.setdefault((issue.rule, issue.layer, issue.message, issue.bbox), []).append(item)
            continue
        selected.append(item)
    for rows in area_groups.values():
        selected.append(
            max(
                rows,
                key=lambda item: (
                    str(item.shape.id).startswith("path_"),
                    item.shape_area,
                    item.overlap_area,
                    item.score,
                ),
            )
        )
    return tuple(selected)


def plan_spacing_drc_push_edits(
    localizations: list[DrcSpacingLocalization] | tuple[DrcSpacingLocalization, ...],
    *,
    fixed_nets: tuple[str, ...] = (),
) -> tuple[GeometryEdit, ...]:
    """Create replacement-style move edits for localized spacing violations."""

    fixed = {str(net) for net in fixed_nets}
    edits: list[GeometryEdit] = []
    seen: set[tuple[str, str, tuple[float, float, float, float], tuple[float, float, float, float]]] = set()
    for item in localizations:
        moving = _spacing_movable_shape(item.shape, item.peer_shape, fixed)
        peer = item.peer_shape if moving is item.shape else item.shape
        target = _push_bbox_away(moving.bbox, peer.bbox, item.required_spacing)
        if target == moving.bbox:
            continue
        key = ("push_spacing_shape", moving.layer, moving.bbox, target)
        if key in seen:
            continue
        seen.add(key)
        edits.append(GeometryEdit("push_spacing_shape", moving.layer, moving.bbox, target, item.issue.message, moving.net, moving.id))
    return tuple(edits)


def plan_localized_drc_layout_patch(
    localizations: list[DrcIssueLocalization] | tuple[DrcIssueLocalization, ...],
    *,
    min_width_by_layer: Mapping[str, float] | None = None,
    min_area_by_layer: Mapping[str, float] | None = None,
    lib: str = "work",
    cell: str = "localized_drc_patch",
    view: str = "layout",
    pdk: PdkConfig | None = None,
    base_plan: object | None = None,
    strict_precheck: bool = False,
) -> LocalizedDrcPatchPlan:
    """Build a LayoutIR patch proposal for localized width/min-area DRCs."""

    edits = plan_localized_drc_patches(localizations, min_width_by_layer=min_width_by_layer, min_area_by_layer=min_area_by_layer)
    patch = _layout_proposal_for_geometry_edits(edits, lib=lib, cell=cell, view=view, pdk=pdk)
    from analogskills.eda.oa import layout_plan_to_oa_write_plan
    from analogskills.layout.ir import merge_layout_plans
    from analogskills.layout.physical import analyze_plan_physical_connectivity

    oa_patch = layout_plan_to_oa_write_plan(patch)
    oa_patch = replace(oa_patch, cellview=replace(oa_patch.cellview, mode="a"))
    physical_report = analyze_plan_physical_connectivity(patch, pdk=pdk)
    interconnect_report = None
    merged_physical_report = None
    merged_interconnect_report = None
    if strict_precheck:
        from analogskills.layout.routing import analyze_interconnect_plan

        interconnect_report = analyze_interconnect_plan(patch, pdk=pdk)
    if base_plan is not None and hasattr(base_plan, "cell"):
        merged = merge_layout_plans(base_plan, patch, cell=getattr(base_plan, "cell", None), grid=pdk)
        merged_physical_report = analyze_plan_physical_connectivity(merged, include_opens=True, pdk=pdk)
        if strict_precheck:
            from analogskills.layout.routing import analyze_interconnect_plan

            merged_interconnect_report = analyze_interconnect_plan(merged, pdk=pdk, include_open_checks=True)
    return LocalizedDrcPatchPlan(edits, patch, oa_patch, physical_report, interconnect_report, merged_physical_report, merged_interconnect_report)


def plan_localized_via_array_patch(
    localizations: list[DrcIssueLocalization] | tuple[DrcIssueLocalization, ...],
    *,
    via_def_by_layer: Mapping[str, str] | None = None,
    lib: str = "work",
    cell: str = "localized_via_array_patch",
    view: str = "layout",
    pdk: PdkConfig | None = None,
    base_plan: object | None = None,
    rows: int = 2,
    cols: int = 2,
    include_landing_enclosures: bool = False,
    landing_margin_um: float | None = None,
) -> LocalizedDrcPatchPlan:
    """Build a narrow additive via-array patch for localized via/contact issues."""

    from analogskills.eda.oa import layout_plan_to_oa_write_plan
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, LayoutRect, LayoutVia, merge_layout_plans, snap_layout_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity, analyze_via_landings, via_landing_bboxes
    from analogskills.layout.routing import analyze_interconnect_plan

    aliases = {str(key): str(value) for key, value in dict(via_def_by_layer or {}).items()}
    edits: list[GeometryEdit] = []
    vias: list[object] = []
    rects: list[object] = []
    seen: set[tuple[str, tuple[float, float], str, int, int]] = set()
    seen_landings: set[tuple[str, tuple[float, float, float, float], str]] = set()
    for item in localizations:
        issue = item.issue
        shape = item.shape
        text = f"{issue.rule} {issue.message}".lower()
        if not _is_via_or_contact_rule(issue.rule.upper(), issue.layer, text):
            continue
        via_def = aliases.get(issue.layer) or aliases.get(shape.layer) or str(issue.layer or shape.layer)
        if not via_def:
            continue
        xy = _bbox_center(item.overlap_bbox)
        key = (via_def, xy, shape.net, rows, cols)
        if key in seen:
            continue
        seen.add(key)
        edits.append(GeometryEdit("replace_with_via_array", issue.layer, issue.bbox, item.overlap_bbox, issue.message, shape.net, shape.id))
        via = LayoutVia(
            via_def,
            xy,
            shape.net,
            rows=rows,
            cols=cols,
            metadata={
                "action": "replace_with_via_array",
                "source_shape": shape.id,
                "source_issue": issue.rule,
                "emit_cut_array": True,
            },
        )
        vias.append(via)
        if include_landing_enclosures and pdk is not None and shape.net:
            for layer, landing in via_landing_bboxes(via, pdk, landing_margin_um=landing_margin_um):
                landing_key = (layer, landing, shape.net)
                if landing_key in seen_landings:
                    continue
                if base_plan is not None and _same_net_landing_covered(base_plan, shape.net, layer, landing):
                    continue
                seen_landings.add(landing_key)
                rects.append(LayoutRect(layer, landing, shape.net, metadata={"action": "grow_via_landing_or_enclosure", "source_issue": issue.rule, "source_shape": shape.id, "kind": "via_landing"}))
    if not vias:
        seen_bbox: set[tuple[str, tuple[float, float], int, int]] = set()
        for item in localizations:
            issue = item.issue
            text = f"{issue.rule} {issue.message}".lower()
            if not _is_via_or_contact_rule(issue.rule.upper(), issue.layer, text):
                continue
            bbox = _float_bbox(issue.bbox)
            if bbox is None:
                continue
            via_def = aliases.get(issue.layer) or str(issue.layer)
            if not via_def:
                continue
            xy = _bbox_center(bbox)
            key = (via_def, xy, rows, cols)
            if key in seen_bbox:
                continue
            seen_bbox.add(key)
            edits.append(GeometryEdit("replace_with_via_array", issue.layer, bbox, bbox, issue.message))
            via = LayoutVia(
                via_def,
                xy,
                "",
                rows=rows,
                cols=cols,
                metadata={
                    "action": "replace_with_via_array",
                    "source_issue": issue.rule,
                    "emit_cut_array": True,
                },
            )
            vias.append(via)
    patch = LayoutPlan(
        LayoutCellRef(lib, cell, view, "maskLayout"),
        rects=tuple(rects),
        vias=tuple(vias),
        metadata={"source": "plan_localized_via_array_patch", "geometry_edit_count": len(edits)},
    )
    if pdk is not None:
        patch = snap_layout_plan_to_grid(patch, pdk)
    oa_patch = layout_plan_to_oa_write_plan(patch)
    oa_patch = replace(oa_patch, cellview=replace(oa_patch.cellview, mode="a"))
    physical_report = analyze_plan_physical_connectivity(patch, pdk=pdk)
    interconnect_report = analyze_interconnect_plan(patch, pdk=pdk)
    merged_physical_report = None
    merged_interconnect_report = None
    if base_plan is not None and hasattr(base_plan, "cell"):
        merged = merge_layout_plans(base_plan, patch, cell=getattr(base_plan, "cell", None), grid=pdk)
        merged_physical_report = analyze_plan_physical_connectivity(merged, include_opens=True, pdk=pdk)
        merged_interconnect_report = analyze_interconnect_plan(merged, pdk=pdk, include_open_checks=True)
        if include_landing_enclosures and pdk is not None:
            via_report = analyze_via_landings(merged, pdk, landing_margin_um=landing_margin_um)
            merged_interconnect_report = {**dict(merged_interconnect_report), "via_landings_after_patch": via_report}
    return LocalizedDrcPatchPlan(tuple(edits), patch, oa_patch, physical_report, interconnect_report, merged_physical_report, merged_interconnect_report)


def plan_safe_redundant_via_array_patch(
    localizations: list[DrcIssueLocalization] | tuple[DrcIssueLocalization, ...],
    *,
    base_plan: object,
    via_def_by_layer: Mapping[str, str] | None = None,
    lib: str | None = None,
    cell: str | None = None,
    view: str | None = None,
    pdk: PdkConfig | None = None,
    rows: int = 2,
    cols: int = 2,
    include_landing_enclosures: bool = True,
    landing_margin_um: float | None = None,
    max_candidates: int | None = None,
) -> LocalizedDrcPatchPlan:
    """Greedily add redundant via arrays only when precheck shorts do not grow.

    Calibre redundant-via markers are good ECO candidates, but blindly adding
    2x2 arrays can extend landing metal into neighboring nets.  This helper
    accepts localized via markers one at a time and rejects any candidate that
    introduces a new cross-net same-layer contact according to the inline
    physical precheck.  It is intentionally conservative; rejected markers can
    later be handed to a local SMT/ECO solver.
    """

    from analogskills.eda.oa import layout_plan_to_oa_write_plan, oa_write_plan_to_layout_plan
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, merge_layout_plans, snap_layout_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity, detect_plan_shape_shorts
    from analogskills.layout.routing import analyze_interconnect_plan

    base = oa_write_plan_to_layout_plan(base_plan) if hasattr(base_plan, "cellview") else base_plan
    if not hasattr(base, "cell"):
        raise TypeError("base_plan must be a LayoutPlan or OaWritePlan")
    source_cell = getattr(base, "cell")
    target_cell = LayoutCellRef(
        str(lib or source_cell.lib),
        str(cell or source_cell.cell),
        str(view or source_cell.view),
        str(getattr(source_cell, "view_type", "maskLayout") or "maskLayout"),
    )

    candidates = tuple(localizations)
    if max_candidates is not None:
        candidates = candidates[: max(int(max_candidates), 0)]

    current = base
    accepted_edits: list[GeometryEdit] = []
    accepted_rects: list[object] = []
    accepted_vias: list[object] = []
    accepted_keys: set[tuple[str, str, tuple[int, int], int, int]] = set()
    accepted_rect_keys: set[tuple[str, str, tuple[int, int, int, int]]] = set()
    rejected: list[dict[str, object]] = []
    covered_duplicates = 0
    current_short_signature = _plan_short_signature(detect_plan_shape_shorts(current, include_via_landings=True, pdk=pdk))

    for item in candidates:
        single = plan_localized_via_array_patch(
            (item,),
            via_def_by_layer=via_def_by_layer,
            lib=target_cell.lib,
            cell=target_cell.cell,
            view=target_cell.view,
            pdk=pdk,
            base_plan=current,
            rows=rows,
            cols=cols,
            include_landing_enclosures=include_landing_enclosures,
            landing_margin_um=landing_margin_um,
        )
        candidate_vias = []
        candidate_rects = []
        for via in tuple(getattr(single.layout_patch, "vias", ())):
            key = (
                str(getattr(via, "via_def", "")),
                str(getattr(via, "net", "")),
                _point_key_um(getattr(via, "xy", (0.0, 0.0))),
                int(getattr(via, "rows", 1) or 1),
                int(getattr(via, "cols", 1) or 1),
            )
            if key in accepted_keys:
                continue
            candidate_vias.append(via)
        for rect in tuple(getattr(single.layout_patch, "rects", ())):
            key = (
                str(getattr(rect, "layer", "")),
                str(getattr(rect, "net", "")),
                _bbox_key_um(getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0))),
            )
            if key in accepted_rect_keys:
                continue
            candidate_rects.append(rect)
        if not candidate_vias and not candidate_rects:
            covered_duplicates += 1
            continue
        single_patch = replace(single.layout_patch, rects=tuple(candidate_rects), vias=tuple(candidate_vias))
        candidate_plan = merge_layout_plans(current, single_patch, cell=source_cell, grid=pdk)
        candidate_short_signature = _plan_short_signature(detect_plan_shape_shorts(candidate_plan, include_via_landings=True, pdk=pdk))
        new_shorts = candidate_short_signature - current_short_signature
        if new_shorts:
            rejected.append(
                {
                    "rule": item.issue.rule,
                    "bbox": item.issue.bbox,
                    "shape": item.shape.id,
                    "net": item.shape.net,
                    "new_short_count": len(new_shorts),
                }
            )
            continue
        current = candidate_plan
        current_short_signature = candidate_short_signature
        accepted_edits.extend(single.edits)
        accepted_rects.extend(candidate_rects)
        accepted_vias.extend(candidate_vias)
        for via in candidate_vias:
            accepted_keys.add(
                (
                    str(getattr(via, "via_def", "")),
                    str(getattr(via, "net", "")),
                    _point_key_um(getattr(via, "xy", (0.0, 0.0))),
                    int(getattr(via, "rows", 1) or 1),
                    int(getattr(via, "cols", 1) or 1),
                )
            )
        for rect in candidate_rects:
            accepted_rect_keys.add(
                (
                    str(getattr(rect, "layer", "")),
                    str(getattr(rect, "net", "")),
                    _bbox_key_um(getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0))),
                )
            )

    patch = LayoutPlan(
        target_cell,
        rects=tuple(accepted_rects),
        vias=tuple(accepted_vias),
        metadata={
            "source": "plan_safe_redundant_via_array_patch",
            "geometry_edit_count": len(accepted_edits),
            "candidate_count": len(candidates),
            "accepted_candidate_count": len(accepted_vias),
            "rejected_candidate_count": len(rejected),
            "covered_duplicate_count": covered_duplicates,
            "rejected_candidates": tuple(rejected),
            "safety_policy": "reject_if_inline_same_layer_short_signature_grows",
        },
    )
    if pdk is not None:
        patch = snap_layout_plan_to_grid(patch, pdk)
    oa_patch = layout_plan_to_oa_write_plan(patch)
    oa_patch = replace(oa_patch, cellview=replace(oa_patch.cellview, mode="a"))
    physical_report = analyze_plan_physical_connectivity(patch, pdk=pdk)
    interconnect_report = analyze_interconnect_plan(patch, pdk=pdk)
    merged = merge_layout_plans(base, patch, cell=source_cell, grid=pdk)
    merged_physical_report = analyze_plan_physical_connectivity(merged, include_opens=True, include_via_landing_shorts=True, pdk=pdk)
    merged_interconnect_report = analyze_interconnect_plan(merged, pdk=pdk, include_open_checks=True)
    return LocalizedDrcPatchPlan(tuple(accepted_edits), patch, oa_patch, physical_report, interconnect_report, merged_physical_report, merged_interconnect_report)


def plan_safe_redundant_via_neighbor_patch(
    localizations: list[DrcIssueLocalization] | tuple[DrcIssueLocalization, ...],
    *,
    base_plan: object,
    via_def_by_layer: Mapping[str, str] | None = None,
    lib: str | None = None,
    cell: str | None = None,
    view: str | None = None,
    pdk: PdkConfig | None = None,
    redundant_spacing_um: float | None = None,
    include_landing_enclosures: bool = True,
    allow_same_net_landing_spacing: bool = False,
    max_candidates: int | None = None,
) -> LocalizedDrcPatchPlan:
    """Add one legal neighboring via cut per marker instead of a centered array.

    This is the additive-safe form of redundant-via repair.  The original via
    cut remains in the layout, so a centered 1x2/2x2 array would place new cuts
    too close to the original.  Here each accepted ECO adds exactly one adjacent
    cut at a legal edge spacing and a same-net landing bar that connects back to
    the original landing.
    """

    from analogskills.eda.oa import layout_plan_to_oa_write_plan, oa_write_plan_to_layout_plan
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, LayoutRect, merge_layout_plans, snap_layout_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity, detect_plan_shape_shorts
    from analogskills.layout.routing import analyze_interconnect_plan

    base = oa_write_plan_to_layout_plan(base_plan) if hasattr(base_plan, "cellview") else base_plan
    if not hasattr(base, "cell"):
        raise TypeError("base_plan must be a LayoutPlan or OaWritePlan")
    source_cell = getattr(base, "cell")
    target_cell = LayoutCellRef(
        str(lib or source_cell.lib),
        str(cell or source_cell.cell),
        str(view or source_cell.view),
        str(getattr(source_cell, "view_type", "maskLayout") or "maskLayout"),
    )
    aliases = {str(key): str(value) for key, value in dict(via_def_by_layer or {}).items()}
    candidates = tuple(localizations)
    if max_candidates is not None:
        candidates = candidates[: max(int(max_candidates), 0)]

    current = base
    current_short_signature = _plan_short_signature(detect_plan_shape_shorts(current, include_via_landings=True, pdk=pdk))
    accepted_edits: list[GeometryEdit] = []
    accepted_rects: list[object] = []
    accepted_cut_keys: set[tuple[str, str, tuple[int, int, int, int]]] = set()
    accepted_landing_keys: set[tuple[str, str, tuple[int, int, int, int]]] = set()
    rejected: list[dict[str, object]] = []

    for item in candidates:
        issue = item.issue
        shape = item.shape
        text = f"{issue.rule} {issue.message}".lower()
        if not _is_via_or_contact_rule(issue.rule.upper(), issue.layer, text):
            continue
        via_def = aliases.get(issue.layer) or aliases.get(shape.layer) or str(issue.layer or shape.layer)
        if not via_def:
            continue
        original_cut = _float_bbox(shape.bbox) or _float_bbox(item.overlap_bbox)
        if original_cut is None:
            continue
        net = str(shape.net)
        best_patch: object | None = None
        best_edit: GeometryEdit | None = None
        best_reason = "no_legal_neighbor_direction"
        for cut_bbox, raw_landing_rects, direction in _redundant_via_neighbor_rects(
            original_cut,
            via_def=via_def,
            net=net,
            pdk=pdk,
            redundant_spacing_um=redundant_spacing_um,
            include_landing_enclosures=include_landing_enclosures,
        ):
            cut_key = (via_def, net, _bbox_key_um(cut_bbox))
            if cut_key in accepted_cut_keys:
                best_reason = "duplicate_neighbor_cut"
                continue
            if not _via_neighbor_spacing_is_legal(current, via_def, cut_bbox, original_cut, pdk=pdk):
                best_reason = "neighbor_cut_spacing_precheck_failed"
                continue
            landing_rects = _effective_redundant_via_neighbor_landings(
                current,
                net=net,
                via_def=via_def,
                original_cut=original_cut,
                cut_bbox=cut_bbox,
                direction=direction,
                raw_landing_rects=raw_landing_rects,
                pdk=pdk,
            )
            landing_spacing_ok = True
            for layer, bbox in landing_rects:
                if not _landing_spacing_is_legal(current, layer, net, bbox, pdk=pdk, allow_same_net_spacing=allow_same_net_landing_spacing):
                    landing_spacing_ok = False
                    best_reason = f"landing_spacing_precheck_failed:{layer}"
                    break
            if not landing_spacing_ok:
                continue
            rects = [
                LayoutRect(
                    via_def,
                    cut_bbox,
                    net,
                    metadata={
                        "action": "add_redundant_via_neighbor",
                        "source_issue": issue.rule,
                        "source_shape": shape.id,
                        "direction": direction,
                    },
                )
            ]
            for layer, bbox in landing_rects:
                key = (layer, net, _bbox_key_um(bbox))
                if key in accepted_landing_keys:
                    continue
                rects.append(
                    LayoutRect(
                        layer,
                        bbox,
                        net,
                        metadata={
                            "action": "add_redundant_via_neighbor_landing",
                            "source_issue": issue.rule,
                            "source_shape": shape.id,
                            "direction": direction,
                            "via_def": via_def,
                        },
                    )
                )
            single_patch = LayoutPlan(target_cell, rects=tuple(rects))
            if pdk is not None:
                single_patch = snap_layout_plan_to_grid(single_patch, pdk)
            candidate_plan = merge_layout_plans(current, single_patch, cell=source_cell, grid=pdk)
            candidate_short_signature = _plan_short_signature(detect_plan_shape_shorts(candidate_plan, include_via_landings=True, pdk=pdk))
            new_shorts = candidate_short_signature - current_short_signature
            if new_shorts:
                best_reason = f"new_short_count:{len(new_shorts)}"
                continue
            best_patch = single_patch
            best_edit = GeometryEdit("add_redundant_via_neighbor", via_def, issue.bbox, cut_bbox, issue.message, net, shape.id)
            break
        if best_patch is None or best_edit is None:
            rejected.append({"rule": issue.rule, "bbox": issue.bbox, "shape": shape.id, "net": net, "reason": best_reason})
            continue
        current = merge_layout_plans(current, best_patch, cell=source_cell, grid=pdk)
        current_short_signature = _plan_short_signature(detect_plan_shape_shorts(current, include_via_landings=True, pdk=pdk))
        accepted_edits.append(best_edit)
        for rect in tuple(getattr(best_patch, "rects", ())):
            key = (str(rect.layer), str(rect.net), _bbox_key_um(rect.bbox))
            if str(rect.layer) == via_def:
                accepted_cut_keys.add((str(rect.layer), str(rect.net), _bbox_key_um(rect.bbox)))
            else:
                accepted_landing_keys.add(key)
            accepted_rects.append(rect)

    patch = LayoutPlan(
        target_cell,
        rects=tuple(accepted_rects),
        metadata={
            "source": "plan_safe_redundant_via_neighbor_patch",
            "geometry_edit_count": len(accepted_edits),
            "candidate_count": len(candidates),
            "accepted_candidate_count": len(accepted_edits),
            "rejected_candidate_count": len(rejected),
            "rejected_candidates": tuple(rejected),
            "safety_policy": "add_one_neighbor_cut_reject_if_short_or_spacing_precheck_fails",
            "allow_same_net_landing_spacing": bool(allow_same_net_landing_spacing),
        },
    )
    if pdk is not None:
        patch = snap_layout_plan_to_grid(patch, pdk)
    oa_patch = layout_plan_to_oa_write_plan(patch)
    oa_patch = replace(oa_patch, cellview=replace(oa_patch.cellview, mode="a"))
    physical_report = analyze_plan_physical_connectivity(patch, pdk=pdk)
    interconnect_report = analyze_interconnect_plan(patch, pdk=pdk)
    merged = merge_layout_plans(base, patch, cell=source_cell, grid=pdk)
    merged_physical_report = analyze_plan_physical_connectivity(merged, include_opens=True, include_via_landing_shorts=True, pdk=pdk)
    merged_interconnect_report = analyze_interconnect_plan(merged, pdk=pdk, include_open_checks=True)
    return LocalizedDrcPatchPlan(tuple(accepted_edits), patch, oa_patch, physical_report, interconnect_report, merged_physical_report, merged_interconnect_report)


def apply_localized_drc_patch_plan(patch_plan: LocalizedDrcPatchPlan, backend: object) -> object:
    """Apply a localized DRC patch through the existing OA write-plan backend."""

    from analogskills.eda.oa import apply_oa_write_plan

    return apply_oa_write_plan(patch_plan.oa_patch, backend)


def plan_localized_spacing_replacement(
    localizations: list[DrcSpacingLocalization] | tuple[DrcSpacingLocalization, ...],
    *,
    base_plan: object,
    fixed_nets: tuple[str, ...] = (),
    pdk: PdkConfig | None = None,
    min_spacing: float | None = None,
    lib: str | None = None,
    cell: str | None = None,
    view: str | None = None,
) -> DrcReplacementPlan:
    """Build a full-cell replacement plan for localized spacing violations."""

    from analogskills.eda.oa import oa_write_plan_to_layout_plan

    base = oa_write_plan_to_layout_plan(base_plan) if hasattr(base_plan, "cellview") else base_plan
    push_replacement = _build_spacing_push_replacement(
        localizations,
        base_plan=base,
        fixed_nets=fixed_nets,
        pdk=pdk,
        min_spacing=min_spacing,
        lib=lib,
        cell=cell,
        view=view,
    )
    reroute_replacement = _build_spacing_reroute_replacement(
        localizations,
        base_plan=base,
        fixed_nets=fixed_nets,
        pdk=pdk,
        min_spacing=min_spacing,
        lib=lib,
        cell=cell,
        view=view,
    )
    if reroute_replacement is None:
        return push_replacement
    if _spacing_replacement_rank(reroute_replacement) <= _spacing_replacement_rank(push_replacement):
        return reroute_replacement
    return push_replacement


def _build_spacing_push_replacement(
    localizations: list[DrcSpacingLocalization] | tuple[DrcSpacingLocalization, ...],
    *,
    base_plan: object,
    fixed_nets: tuple[str, ...] = (),
    pdk: PdkConfig | None = None,
    min_spacing: float | None = None,
    lib: str | None = None,
    cell: str | None = None,
    view: str | None = None,
) -> DrcReplacementPlan:
    from analogskills.eda.oa import layout_plan_to_oa_write_plan
    from analogskills.layout.ir import LayoutCellRef, LayoutPath, LayoutPlan, LayoutRect, snap_layout_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity
    from analogskills.layout.routing import analyze_interconnect_plan

    base = base_plan
    edits = plan_spacing_drc_push_edits(localizations, fixed_nets=fixed_nets)
    edit_by_shape = {edit.shape_id: edit for edit in edits if edit.shape_id}
    before_shapes = list(_layout_plan_shapes(base))
    spacing_target = max(float(min_spacing or 0.0), max((float(item.required_spacing) for item in localizations), default=0.0))
    before_spacing = tuple(shape_spacing_violations(before_shapes, min_spacing=spacing_target)) if spacing_target > 0.0 else ()
    rects = []
    paths = []
    for rect_idx, rect in enumerate(tuple(getattr(base, "rects", ()))):
        edit = edit_by_shape.get(f"rect_{rect_idx}")
        if edit is None or edit.bbox is None or edit.target_bbox is None:
            rects.append(rect)
            continue
        dx = edit.target_bbox[0] - edit.bbox[0]
        dy = edit.target_bbox[1] - edit.bbox[1]
        rects.append(
            LayoutRect(
                str(getattr(rect, "layer", "")),
                _shift_bbox(_bbox_tuple(getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0))), dx, dy),
                str(getattr(rect, "net", "")),
                str(getattr(rect, "purpose", "drawing")),
                {**dict(getattr(rect, "metadata", {})), "action": edit.action, "source_rect": rect_idx},
            )
        )
    path_edits: dict[int, tuple[float, float, GeometryEdit]] = {}
    for shape_id, edit in edit_by_shape.items():
        path_idx = _shape_path_index(shape_id)
        if path_idx is None or edit.bbox is None or edit.target_bbox is None:
            continue
        dx = edit.target_bbox[0] - edit.bbox[0]
        dy = edit.target_bbox[1] - edit.bbox[1]
        path_edits.setdefault(path_idx, (dx, dy, edit))
    for path_idx, path in enumerate(tuple(getattr(base, "paths", ()))):
        moved = path_edits.get(path_idx)
        if moved is None:
            paths.append(path)
            continue
        dx, dy, edit = moved
        points = tuple((float(x) + dx, float(y) + dy) for x, y in tuple(getattr(path, "points", ())))
        paths.append(
            LayoutPath(
                str(getattr(path, "layer", "")),
                points,
                float(getattr(path, "width", 0.0) or 0.0),
                str(getattr(path, "net", "")),
                str(getattr(path, "purpose", "drawing")),
                {**dict(getattr(path, "metadata", {})), "action": edit.action, "source_path": path_idx},
            )
        )
    source_cell = getattr(base, "cell", LayoutCellRef(lib or "work", cell or "spacing_replacement", view or "layout", "maskLayout"))
    target_cell = LayoutCellRef(lib or source_cell.lib, cell or source_cell.cell, view or source_cell.view, source_cell.view_type)
    replacement = LayoutPlan(
        target_cell,
        nets=tuple(getattr(base, "nets", ())),
        pins=tuple(getattr(base, "pins", ())),
        instances=tuple(getattr(base, "instances", ())),
        rects=tuple(rects),
        paths=tuple(paths),
        vias=tuple(getattr(base, "vias", ())),
        labels=tuple(getattr(base, "labels", ())),
        metadata={**dict(getattr(base, "metadata", {})), "source": "plan_localized_spacing_replacement", "spacing_repair_mode": "push", "geometry_edit_count": len(edits)},
    )
    if pdk is not None:
        replacement = snap_layout_plan_to_grid(replacement, pdk)
    after_shapes = list(_layout_plan_shapes(replacement))
    after_spacing = tuple(shape_spacing_violations(after_shapes, min_spacing=spacing_target)) if spacing_target > 0.0 else ()
    physical_report = analyze_plan_physical_connectivity(replacement, include_opens=True, pdk=pdk)
    interconnect_report = analyze_interconnect_plan(replacement, pdk=pdk, include_open_checks=True)
    return DrcReplacementPlan(tuple(edits), replacement, layout_plan_to_oa_write_plan(replacement), physical_report, interconnect_report, before_spacing, after_spacing)


def _build_spacing_reroute_replacement(
    localizations: list[DrcSpacingLocalization] | tuple[DrcSpacingLocalization, ...],
    *,
    base_plan: object,
    fixed_nets: tuple[str, ...] = (),
    pdk: PdkConfig | None = None,
    min_spacing: float | None = None,
    lib: str | None = None,
    cell: str | None = None,
    view: str | None = None,
) -> DrcReplacementPlan | None:
    if pdk is None:
        return None
    from analogskills.eda.oa import layout_plan_to_oa_write_plan
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, snap_layout_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity
    from analogskills.layout.routing import AStarPenaltyRegion, _track_graph_points_avoiding, analyze_interconnect_plan

    base = base_plan
    fixed = {str(net) for net in fixed_nets if str(net)}
    spacing_target = max(float(min_spacing or 0.0), max((float(item.required_spacing) for item in localizations), default=0.0))
    before_shapes = list(_layout_plan_shapes(base))
    before_spacing = tuple(shape_spacing_violations(before_shapes, min_spacing=spacing_target)) if spacing_target > 0.0 else ()
    path_items: dict[int, list[DrcSpacingLocalization]] = {}
    for item in localizations:
        moving = _spacing_movable_shape(item.shape, item.peer_shape, fixed)
        path_idx = _shape_path_index(moving.id)
        if path_idx is None or moving.net in fixed:
            continue
        path_items.setdefault(path_idx, []).append(item)
    if not path_items:
        return None

    paths = list(tuple(getattr(base, "paths", ())))
    edits: list[GeometryEdit] = []
    changed = False
    for path_idx, items in sorted(path_items.items()):
        if path_idx < 0 or path_idx >= len(paths):
            continue
        path = paths[path_idx]
        layer = str(getattr(path, "layer", ""))
        net = str(getattr(path, "net", ""))
        points = tuple((float(x), float(y)) for x, y in tuple(getattr(path, "points", ())))
        if not layer or not net or len(points) < 2:
            continue
        width = max(float(getattr(path, "width", 0.0) or 0.0), _min_route_width(layer, pdk))
        occupied = _spacing_reroute_occupied(base, exclude_path_idx=path_idx, pdk=pdk, min_spacing_um=spacing_target)
        avoid_nets = tuple(
            dict.fromkeys(
                str(shape.net)
                for item in items
                for shape in (item.shape, item.peer_shape)
                if str(shape.net) and str(shape.net) != net
            )
        )
        compact_bbox = _bbox_union_many(_path_segment_bboxes(points, width))
        violation_regions = tuple(
            dict.fromkeys(
                AStarPenaltyRegion(
                    bbox=bbox,
                    layer=layer,
                    cost=max(15.0, float(item.required_spacing) * 40.0),
                    keepout_um=max(float(item.required_spacing), _min_route_spacing(layer, pdk)),
                )
                for item in items
                for bbox in (
                    *(() if _float_bbox(item.issue.bbox) is None else (_float_bbox(item.issue.bbox),)),
                    item.shape.bbox if item.shape.net != net else None,
                    item.peer_shape.bbox if item.peer_shape.net != net else None,
                )
                if bbox is not None
            )
        )
        corridor_hints = _routing_guides_for_net(base, net)
        rerouted = _track_graph_points_avoiding(
            points[0],
            points[-1],
            layer,
            width,
            net,
            occupied,
            pdk,
            avoid_nets=avoid_nets,
            corridor_hints=corridor_hints,
            compact_bbox_um=compact_bbox,
            violation_regions=violation_regions,
        )
        if rerouted is None:
            continue
        rerouted_points = tuple((float(x), float(y)) for x, y in rerouted)
        if rerouted_points == points:
            continue
        paths[path_idx] = replace(
            path,
            points=rerouted_points,
            width=width,
            metadata={
                **dict(getattr(path, "metadata", {})),
                "action": "reroute_spacing_path",
                "source_path": path_idx,
                "localized_spacing_issue_count": len(items),
                "guide_hint_count": len(corridor_hints),
            },
        )
        edits.append(
            GeometryEdit(
                "reroute_spacing_path",
                layer,
                _bbox_union_many(_path_segment_bboxes(points, width)),
                _bbox_union_many(_path_segment_bboxes(rerouted_points, width)),
                "; ".join(str(item.issue.message) for item in items),
                net,
                f"path_{path_idx}",
            )
        )
        changed = True
    if not changed:
        return None

    source_cell = getattr(base, "cell", LayoutCellRef(lib or "work", cell or "spacing_reroute", view or "layout", "maskLayout"))
    target_cell = LayoutCellRef(lib or source_cell.lib, cell or source_cell.cell, view or source_cell.view, source_cell.view_type)
    replacement = LayoutPlan(
        target_cell,
        nets=tuple(getattr(base, "nets", ())),
        pins=tuple(getattr(base, "pins", ())),
        instances=tuple(getattr(base, "instances", ())),
        rects=tuple(getattr(base, "rects", ())),
        paths=tuple(paths),
        vias=tuple(getattr(base, "vias", ())),
        labels=tuple(getattr(base, "labels", ())),
        metadata={**dict(getattr(base, "metadata", {})), "source": "plan_localized_spacing_replacement", "spacing_repair_mode": "reroute", "geometry_edit_count": len(edits)},
    )
    replacement = snap_layout_plan_to_grid(replacement, pdk)
    after_shapes = list(_layout_plan_shapes(replacement))
    after_spacing = tuple(shape_spacing_violations(after_shapes, min_spacing=spacing_target)) if spacing_target > 0.0 else ()
    physical_report = analyze_plan_physical_connectivity(replacement, include_opens=True, pdk=pdk)
    interconnect_report = analyze_interconnect_plan(replacement, pdk=pdk, include_open_checks=True)
    return DrcReplacementPlan(tuple(edits), replacement, layout_plan_to_oa_write_plan(replacement), physical_report, interconnect_report, before_spacing, after_spacing)


def _spacing_replacement_rank(plan: DrcReplacementPlan) -> tuple[float, float, float, float]:
    physical_issue_count = float(len(tuple(plan.physical_report.get("issues", ()))))
    interconnect_issue_count = float(len(tuple((plan.interconnect_report or {}).get("issues", ()))))
    mode_penalty = 0.0 if dict(getattr(plan.replacement_layout, "metadata", {})).get("spacing_repair_mode") == "reroute" else 1.0
    return (
        float(len(plan.after_spacing_violations)),
        physical_issue_count + interconnect_issue_count,
        float(len(plan.edits)),
        mode_penalty,
    )


def _spacing_reroute_occupied(
    base_plan: object,
    *,
    exclude_path_idx: int,
    pdk: PdkConfig | None = None,
    min_spacing_um: float = 0.0,
) -> tuple[tuple[str, str, tuple[float, float, float, float]], ...]:
    occupied: list[tuple[str, str, tuple[float, float, float, float]]] = []
    for shape in _layout_plan_shapes(base_plan):
        path_idx = _shape_path_index(shape.id)
        if path_idx is not None and path_idx == exclude_path_idx:
            continue
        bbox = shape.bbox
        extra_keepout = max(float(min_spacing_um) - _min_route_spacing(shape.layer, pdk), 0.0)
        if extra_keepout > 1e-12:
            bbox = _expand_bbox(bbox, extra_keepout)
        occupied.append((shape.layer, shape.net, bbox))
    return tuple(occupied)


def _shape_path_index(shape_id: str) -> int | None:
    if not shape_id.startswith("path_"):
        return None
    parts = shape_id.split("_")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def apply_drc_replacement_plan(replacement_plan: DrcReplacementPlan, backend: object) -> object:
    """Apply a full-cell DRC replacement through the OA replacement backend."""

    from analogskills.eda.oa import apply_oa_replacement_plan

    return apply_oa_replacement_plan(replacement_plan.replacement_oa_plan, backend)


def apply_drc_repair_candidate(candidate: DrcRepairCandidate, backend: object) -> object:
    """Apply a selected DRC repair candidate through the matching OA backend path."""

    plan = candidate.plan
    if isinstance(plan, LocalizedDrcPatchPlan):
        return apply_localized_drc_patch_plan(plan, backend)
    if isinstance(plan, DrcReplacementPlan):
        return apply_drc_replacement_plan(plan, backend)
    raise TypeError(f"unsupported DRC repair candidate plan {type(plan)!r}")


def apply_drc_repair_proposal(proposal: DrcRepairProposal, backend: object) -> object:
    """Apply the selected candidate from a DRC repair proposal."""

    if proposal.selected_candidate is None:
        raise ValueError("DRC repair proposal has no selected candidate")
    return apply_drc_repair_candidate(proposal.selected_candidate, backend)


def apply_lvs_short_replacement_plan(replacement_plan: LvsShortReplacementPlan, backend: object) -> object:
    """Apply a full-cell LVS short replacement through the OA replacement backend."""

    from analogskills.eda.oa import apply_oa_replacement_plan

    return apply_oa_replacement_plan(replacement_plan.replacement_oa_plan, backend)


def apply_lvs_repair_candidate(candidate: LvsRepairCandidate, backend: object) -> object:
    """Apply a selected LVS repair candidate through the matching OA backend path."""

    plan = candidate.plan
    if isinstance(plan, LocalizedDrcPatchPlan):
        return apply_localized_drc_patch_plan(plan, backend)
    if isinstance(plan, LvsShortReplacementPlan):
        return apply_lvs_short_replacement_plan(plan, backend)
    if isinstance(plan, LvsManualRepairHandoffPlan):
        raise TypeError("manual LVS repair handoff requires orchestrated dispatch, not direct OA apply")
    raise TypeError(f"unsupported LVS repair candidate plan {type(plan)!r}")


def apply_lvs_repair_proposal(proposal: LvsRepairProposal, backend: object) -> object:
    """Apply the selected candidate from an LVS repair proposal."""

    if proposal.selected_candidate is None:
        raise ValueError("LVS repair proposal has no selected candidate")
    return apply_lvs_repair_candidate(proposal.selected_candidate, backend)


def drc_repair_candidate_to_dict(candidate: DrcRepairCandidate) -> dict[str, object]:
    """Serialize a DRC repair candidate into a stable summary payload."""

    return {
        "kind": "drc",
        "plan_kind": candidate.plan_kind,
        "score": candidate.score,
        "passed": candidate.passed,
        "issue_rules": tuple(issue.rule for issue in candidate.issues),
        "issue_layers": tuple(issue.layer for issue in candidate.issues),
        "issues_after": candidate.issues_after,
        "edit_count": _repair_plan_edit_count(candidate.plan),
        "repair_scope": _repair_scope_to_dict(candidate.repair_scope),
    }


def drc_repair_proposal_summary(proposal: DrcRepairProposal) -> dict[str, object]:
    """Summarize a DRC repair proposal for downstream orchestration."""

    selected_scope = _repair_scope_to_dict(proposal.selected_candidate.repair_scope) if proposal.selected_candidate is not None else {}
    selected_metadata = dict(selected_scope.get("metadata", {}) or {})
    return {
        "kind": "drc",
        "suggestion_count": len(proposal.eco_plan.suggestions),
        "density_issue_count": len(proposal.eco_plan.density_issues),
        "review_issue_count": len(proposal.eco_plan.review_issues),
        "candidate_count": len(proposal.candidates),
        "selected_plan_kind": proposal.selected_candidate.plan_kind if proposal.selected_candidate is not None else "",
        "selected_passed": bool(proposal.selected_candidate.passed) if proposal.selected_candidate is not None else False,
        "selected_score": float(proposal.selected_candidate.score) if proposal.selected_candidate is not None else float("inf"),
        "selected_issues_after": proposal.selected_candidate.issues_after if proposal.selected_candidate is not None else (),
        "repair_scope": selected_scope,
        "system_recommended_level": str(selected_scope.get("system_recommended_level", "")),
        "system_scope_escalation_required": bool(selected_scope.get("escalation_required", False)),
        "system_repair_guidance": tuple(
            dict(item)
            for item in tuple(selected_metadata.get("system_repair_guidance", ()) or ())
            if isinstance(item, Mapping)
        ),
        "candidates": tuple(drc_repair_candidate_to_dict(candidate) for candidate in proposal.candidates),
    }


def lvs_repair_candidate_to_dict(candidate: LvsRepairCandidate) -> dict[str, object]:
    """Serialize an LVS repair candidate into a stable summary payload."""

    return {
        "kind": "lvs",
        "plan_kind": candidate.plan_kind,
        "score": candidate.score,
        "passed": candidate.passed,
        "issue_kind": candidate.item.issue.kind,
        "issue_net": candidate.item.issue.net,
        "owner": candidate.item.suggestion.owner,
        "action": candidate.item.suggestion.action,
        "issues_after": candidate.issues,
        "edit_count": _repair_plan_edit_count(candidate.plan),
        "repair_scope": _repair_scope_to_dict(candidate.repair_scope),
    }


def lvs_repair_proposal_summary(proposal: LvsRepairProposal) -> dict[str, object]:
    """Summarize an LVS repair proposal for downstream orchestration."""

    selected_scope = _repair_scope_to_dict(proposal.selected_candidate.repair_scope) if proposal.selected_candidate is not None else {}
    selected_metadata = dict(selected_scope.get("metadata", {}) or {})
    return {
        "kind": "lvs",
        "repair_item_count": len(proposal.repair_plan.items),
        "owner_counts": dict(proposal.repair_plan.owners),
        "candidate_count": len(proposal.candidates),
        "selected_plan_kind": proposal.selected_candidate.plan_kind if proposal.selected_candidate is not None else "",
        "selected_passed": bool(proposal.selected_candidate.passed) if proposal.selected_candidate is not None else False,
        "selected_score": float(proposal.selected_candidate.score) if proposal.selected_candidate is not None else float("inf"),
        "selected_issues_after": proposal.selected_candidate.issues if proposal.selected_candidate is not None else (),
        "repair_scope": selected_scope,
        "system_recommended_level": str(selected_scope.get("system_recommended_level", "")),
        "system_scope_escalation_required": bool(selected_scope.get("escalation_required", False)),
        "system_repair_guidance": tuple(
            dict(item)
            for item in tuple(selected_metadata.get("system_repair_guidance", ()) or ())
            if isinstance(item, Mapping)
        ),
        "candidates": tuple(lvs_repair_candidate_to_dict(candidate) for candidate in proposal.candidates),
    }


def post_layout_eco_repair_proposal_summary(proposal: PostLayoutEcoRepairProposal) -> dict[str, object]:
    """Summarize a post-layout ECO proposal for closure queueing."""

    metadata = dict(proposal.metadata)
    return {
        "kind": proposal.kind,
        "candidate_count": 1,
        "selected_plan_kind": proposal.kind,
        "selected_passed": proposal.passed,
        "selected_score": proposal.score,
        "selected_issues_after": proposal.issues_after,
        "hotspot_nets": proposal.hotspot_nets,
        "source": proposal.source,
        "repair_scope": {
            "level": str(metadata.get("scope_level", "")),
            "target": str(metadata.get("scope_target", "")),
            "metadata": metadata,
            "system_recommended_level": str(metadata.get("system_recommended_level", "")),
            "escalation_required": bool(metadata.get("escalation_required", False)),
        },
        "system_recommended_level": str(metadata.get("system_recommended_level", "")),
        "system_scope_escalation_required": bool(metadata.get("escalation_required", False)),
        "system_repair_guidance": tuple(
            dict(item)
            for item in tuple(metadata.get("system_repair_guidance", ()) or ())
            if isinstance(item, Mapping)
        ),
        "metadata": metadata,
    }


def repair_candidate_to_dict(candidate: object) -> dict[str, object]:
    """Serialize either a DRC or LVS repair candidate into a unified payload."""

    if isinstance(candidate, DrcRepairCandidate):
        return drc_repair_candidate_to_dict(candidate)
    if isinstance(candidate, LvsRepairCandidate):
        return lvs_repair_candidate_to_dict(candidate)
    raise TypeError(f"unsupported repair candidate {type(candidate)!r}")


def repair_proposal_summary(proposal: object) -> dict[str, object]:
    """Summarize either a DRC or LVS repair proposal into a unified payload."""

    if isinstance(proposal, DrcRepairProposal):
        return drc_repair_proposal_summary(proposal)
    if isinstance(proposal, LvsRepairProposal):
        return lvs_repair_proposal_summary(proposal)
    if isinstance(proposal, PostLayoutEcoRepairProposal):
        return post_layout_eco_repair_proposal_summary(proposal)
    raise TypeError(f"unsupported repair proposal {type(proposal)!r}")


def apply_repair_proposal(proposal: object, backend: object) -> object:
    """Apply either a DRC or LVS repair proposal through its selected candidate."""

    from analogskills.eda.oa import apply_oa_write_plan

    if isinstance(proposal, DrcRepairProposal):
        return apply_drc_repair_proposal(proposal, backend)
    if isinstance(proposal, LvsRepairProposal):
        return apply_lvs_repair_proposal(proposal, backend)
    if isinstance(proposal, PostLayoutEcoRepairProposal):
        return apply_oa_write_plan(proposal.oa_patch, backend)
    raise TypeError(f"unsupported repair proposal {type(proposal)!r}")


def plan_via_enclosure_patch(
    layout_plan: object,
    pdk: PdkConfig,
    *,
    lib: str = "work",
    cell: str = "via_enclosure_patch",
    view: str = "layout",
    landing_margin_um: float | None = None,
) -> LocalizedDrcPatchPlan:
    """Build additive same-net landing patches for missing via/contact enclosure."""

    from analogskills.eda.oa import layout_plan_to_oa_write_plan
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, LayoutRect, merge_layout_plans, snap_layout_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity, analyze_via_landings, via_landing_bboxes
    from analogskills.layout.routing import analyze_interconnect_plan

    edits: list[GeometryEdit] = []
    seen: set[tuple[str, str, tuple[float, float, float, float], str]] = set()
    for via in tuple(getattr(layout_plan, "vias", ())):
        via_def = str(getattr(via, "via_def", ""))
        net = str(getattr(via, "net", ""))
        if not via_def or not net:
            continue
        for layer, landing in via_landing_bboxes(via, pdk, landing_margin_um=landing_margin_um):
            if _same_net_landing_covered(layout_plan, net, layer, landing):
                continue
            key = ("grow_via_landing_or_enclosure", layer, landing, net)
            if key in seen:
                continue
            seen.add(key)
            edits.append(GeometryEdit("grow_via_landing_or_enclosure", layer, None, landing, f"{via_def} landing/enclosure", net))
    rects = tuple(
        LayoutRect(
            edit.layer,
            edit.target_bbox,
            edit.net,
            metadata={"action": edit.action, "reason": edit.reason, "source_bbox": edit.bbox, "kind": "via_landing"},
        )
        for edit in edits
        if edit.target_bbox is not None
    )
    patch = snap_layout_plan_to_grid(LayoutPlan(LayoutCellRef(lib, cell, view, "maskLayout"), rects=rects, metadata={"source": "plan_via_enclosure_patch", "geometry_edit_count": len(edits)}), pdk)
    oa_patch = layout_plan_to_oa_write_plan(patch)
    physical_report = analyze_plan_physical_connectivity(patch, pdk=pdk)
    interconnect_report = analyze_interconnect_plan(patch, pdk=pdk, require_all_via_landings=False)
    merged = merge_layout_plans(layout_plan, patch, cell=getattr(layout_plan, "cell", None), grid=pdk)
    merged_physical_report = analyze_plan_physical_connectivity(merged, include_opens=True, pdk=pdk)
    merged_interconnect_report = analyze_interconnect_plan(merged, pdk=pdk, include_open_checks=True)
    via_report = analyze_via_landings(merged, pdk, landing_margin_um=landing_margin_um)
    merged_interconnect_report = {**dict(merged_interconnect_report), "via_landings_after_patch": via_report}
    return LocalizedDrcPatchPlan(tuple(edits), patch, oa_patch, physical_report, interconnect_report, merged_physical_report, merged_interconnect_report)


def plan_lvs_pin_label_patch(
    layout_plan: object,
    *,
    top_level_nets: tuple[str, ...] | list[str],
    pdk: PdkConfig | None = None,
    require_explicit_labels: bool = True,
    lib: str = "work",
    cell: str = "lvs_pin_label_patch",
    view: str = "layout",
) -> LocalizedDrcPatchPlan:
    """Build additive pin/label patches for nets with existing drawing geometry."""

    from analogskills.eda.oa import analyze_lvs_pin_label_stamping, layout_plan_to_oa_write_plan
    from analogskills.layout.ir import LayoutCellRef, LayoutLabel, LayoutPin, LayoutPlan, merge_layout_plans, snap_layout_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity
    from analogskills.layout.routing import analyze_interconnect_plan

    top_nets = tuple(dict.fromkeys(str(net) for net in top_level_nets if str(net)))
    current_report = analyze_lvs_pin_label_stamping(layout_plan, top_level_nets=top_nets, pdk=pdk, require_explicit_labels=require_explicit_labels)
    existing_pin_nets = {str(getattr(pin, "net", "")) for pin in tuple(getattr(layout_plan, "pins", ())) if str(getattr(pin, "net", ""))}
    existing_label_nets = {_label_net(label) for label in tuple(getattr(layout_plan, "labels", ()))}
    existing_label_nets.discard("")
    pins = []
    labels = []
    edits: list[GeometryEdit] = []
    for net in top_nets:
        shape = _first_shape_for_net(layout_plan, net)
        if shape is None:
            continue
        center = _bbox_center(shape.bbox)
        if net not in existing_pin_nets:
            pins.append(LayoutPin(net, net, "inputOutput", shape.layer, shape.bbox, {"action": "add_lvs_pin"}))
            edits.append(GeometryEdit("add_lvs_pin", shape.layer, None, shape.bbox, f"missing top-level pin for net {net}", net, shape.id))
        if require_explicit_labels and net not in existing_label_nets:
            labels.append(LayoutLabel(shape.layer, net, center, metadata={"action": "add_lvs_label"}))
            edits.append(GeometryEdit("add_lvs_label", shape.layer, None, None, f"missing explicit text label for net {net}", net, shape.id))
    patch = LayoutPlan(LayoutCellRef(lib, cell, view, "maskLayout"), nets=top_nets, pins=tuple(pins), labels=tuple(labels), metadata={"source": "plan_lvs_pin_label_patch", "geometry_edit_count": len(edits), "precheck_issues": tuple(current_report.get("issues", ()))})
    if pdk is not None:
        patch = snap_layout_plan_to_grid(patch, pdk)
    oa_patch = layout_plan_to_oa_write_plan(patch)
    physical_report = analyze_plan_physical_connectivity(patch, pdk=pdk)
    interconnect_report = analyze_interconnect_plan(patch, pdk=pdk)
    merged = merge_layout_plans(layout_plan, patch, cell=getattr(layout_plan, "cell", None), grid=pdk)
    merged_physical_report = analyze_plan_physical_connectivity(merged, include_opens=True, pdk=pdk)
    merged_interconnect_report = analyze_interconnect_plan(merged, pdk=pdk, include_open_checks=True, top_level_nets=top_nets, require_lvs_labels=require_explicit_labels)
    return LocalizedDrcPatchPlan(tuple(edits), patch, oa_patch, physical_report, interconnect_report, merged_physical_report, merged_interconnect_report)


def plan_lvs_open_route_patch(
    layout_plan: object,
    *,
    pdk: PdkConfig | None = None,
    net: str = "",
    preferred_layer: str = "",
    min_width_by_layer: Mapping[str, float] | None = None,
    min_spacing_by_layer: Mapping[str, float] | None = None,
    lib: str = "work",
    cell: str = "lvs_open_route_patch",
    view: str = "layout",
) -> LocalizedDrcPatchPlan:
    """Build additive route/via patches for simple LVS/open components."""

    from analogskills.eda.oa import layout_plan_to_oa_write_plan
    from analogskills.layout.ir import LayoutCellRef, LayoutPath, LayoutPlan, LayoutRect, LayoutVia, merge_layout_plans, snap_layout_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity, collect_plan_shapes, detect_plan_net_opens, via_landing_bboxes
    from analogskills.layout.routing import analyze_interconnect_plan

    widths = {str(layer): float(value) for layer, value in dict(min_width_by_layer or {}).items()}
    spacings = {str(layer): float(value) for layer, value in dict(min_spacing_by_layer or {}).items()}
    open_issues = detect_plan_net_opens(layout_plan, pdk=pdk)
    # Include via landing geometry in the open-route obstacle set.  Otherwise an
    # open repair can reconnect a just-cut path through another net's via
    # landing and recreate the same short in the next closure iteration.
    shapes = collect_plan_shapes(layout_plan, pdk=pdk)
    rects = []
    paths = []
    vias = []
    edits: list[GeometryEdit] = []
    for open_issue in open_issues:
        if net and open_issue.net != net:
            continue
        corridor_hints = _routing_guides_for_net(layout_plan, open_issue.net)
        components = _same_net_components(
            shapes,
            open_issue.net,
            preferred_layer=preferred_layer,
            layout_plan=layout_plan,
            pdk=pdk,
        )
        routed_open = False
        for bridge in _component_bridge_candidates(components):
            left, right, layer, start, end = bridge
            width = widths.get(layer, _min_route_width(layer, pdk))
            spacing = spacings.get(layer, _min_route_spacing(layer, pdk))
            points = _select_open_route_points(
                start,
                end,
                layer,
                width,
                spacing,
                open_issue.net,
                shapes,
                corridor_hints=corridor_hints,
            )
            if not points:
                continue
            path = LayoutPath(
                layer,
                points,
                width,
                open_issue.net,
                metadata={
                    "action": "route_lvs_open",
                    "source_components": (tuple(_component_sources(left)), tuple(_component_sources(right))),
                    "guide_hint_count": len(corridor_hints),
                },
            )
            paths.append(path)
            edits.append(GeometryEdit("route_lvs_open", layer, None, _path_bbox_from_points(points, width), f"net {open_issue.net} has {open_issue.component_count} disconnected geometry components", open_issue.net))
            routed_open = True
            break
        if routed_open:
            continue
        via_pair = _closest_via_component_pair(components, pdk)
        if via_pair is None:
            continue
        left, right, via_def, xy = via_pair
        via = LayoutVia(via_def, xy, open_issue.net, metadata={"action": "add_lvs_open_via", "source_components": (tuple(_component_sources(left)), tuple(_component_sources(right)))})
        vias.append(via)
        landing_boxes = []
        for layer, landing in via_landing_bboxes(via, pdk):
            landing_boxes.append(landing)
            rects.append(LayoutRect(layer, landing, open_issue.net, metadata={"action": "add_lvs_open_via_landing", "kind": "via_landing"}))
        edits.append(GeometryEdit("add_lvs_open_via", via_def, None, _bbox_union_many(tuple(landing_boxes)), f"net {open_issue.net} has {open_issue.component_count} disconnected geometry components", open_issue.net))
    patch = LayoutPlan(LayoutCellRef(lib, cell, view, "maskLayout"), rects=tuple(rects), paths=tuple(paths), vias=tuple(vias), metadata={"source": "plan_lvs_open_route_patch", "geometry_edit_count": len(edits)})
    if pdk is not None:
        patch = snap_layout_plan_to_grid(patch, pdk)
    oa_patch = layout_plan_to_oa_write_plan(patch)
    physical_report = analyze_plan_physical_connectivity(patch, pdk=pdk)
    interconnect_report = analyze_interconnect_plan(patch, pdk=pdk)
    merged = merge_layout_plans(layout_plan, patch, cell=getattr(layout_plan, "cell", None), grid=pdk)
    merged_physical_report = analyze_plan_physical_connectivity(
        merged,
        include_opens=True,
        include_via_landing_shorts=True,
        pdk=pdk,
    )
    merged_interconnect_report = analyze_interconnect_plan(merged, pdk=pdk, include_open_checks=True)
    return LocalizedDrcPatchPlan(tuple(edits), patch, oa_patch, physical_report, interconnect_report, merged_physical_report, merged_interconnect_report)


def plan_lvs_short_replacement(
    layout_plan: object,
    *,
    keep_net: str,
    victim_net: str = "",
    pdk: PdkConfig | None = None,
    min_spacing_by_layer: Mapping[str, float] | None = None,
    lib: str | None = None,
    cell: str | None = None,
    view: str | None = None,
) -> LvsShortReplacementPlan:
    """Build a full replacement LayoutIR/OA plan for localized shorts."""

    from analogskills.eda.oa import layout_plan_to_oa_write_plan, oa_write_plan_to_layout_plan
    from analogskills.layout.ir import LayoutCellRef, LayoutPath, LayoutPlan, LayoutRect, snap_layout_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity, detect_plan_shape_shorts
    from analogskills.layout.routing import analyze_interconnect_plan

    base = oa_write_plan_to_layout_plan(layout_plan) if hasattr(layout_plan, "cellview") else layout_plan
    spacings = {str(layer): float(value) for layer, value in dict(min_spacing_by_layer or {}).items()}
    before_shorts = detect_plan_shape_shorts(base, include_via_landings=True, pdk=pdk)
    target_pairs = tuple(short for short in before_shorts if keep_net in {getattr(short, "net_a", ""), getattr(short, "net_b", "")} and (not victim_net or victim_net in {getattr(short, "net_a", ""), getattr(short, "net_b", "")}))
    rects = []
    paths = []
    vias = []
    edits: list[GeometryEdit] = []
    for rect_idx, rect in enumerate(tuple(getattr(base, "rects", ()))):
        rect_net = str(getattr(rect, "net", ""))
        rect_layer = str(getattr(rect, "layer", ""))
        rect_bbox = _bbox_tuple(getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0)))
        cutters = tuple(
            rect_intersection(rect_bbox, keepout)
            for keepout in _short_keepout_cutters(target_pairs, keep_net, victim_net, rect_net, rect_layer, f"rect[{rect_idx}]", spacings, pdk)
        )
        pieces = (rect_bbox,)
        for cutter in tuple(cutter for cutter in cutters if cutter is not None):
            next_pieces: list[tuple[float, float, float, float]] = []
            for piece in pieces:
                next_pieces.extend(rect_subtract(piece, cutter))
            pieces = tuple(next_pieces)
        if pieces == (rect_bbox,):
            rects.append(rect)
            continue
        edits.append(GeometryEdit("replace_short_victim_rect", rect_layer, rect_bbox, _bbox_union_many(pieces), f"cut LVS short against {keep_net}", rect_net, f"rect_{rect_idx}"))
        for piece_idx, piece in enumerate(pieces):
            rects.append(LayoutRect(rect_layer, piece, rect_net, str(getattr(rect, "purpose", "drawing")), {**dict(getattr(rect, "metadata", {})), "action": "replace_short_victim_rect", "source_rect": rect_idx, "piece": piece_idx}))
    for path_idx, path in enumerate(tuple(getattr(base, "paths", ()))):
        path_net = str(getattr(path, "net", ""))
        path_layer = str(getattr(path, "layer", ""))
        path_width = float(getattr(path, "width", 0.0) or 0.0)
        path_points = tuple(tuple(point) for point in getattr(path, "points", ()))
        corridor_hints = _routing_guides_for_net(base, path_net)
        cutters = _short_keepout_cutters(
            target_pairs,
            keep_net,
            victim_net,
            path_net,
            path_layer,
            f"path[{path_idx}]",
            spacings,
            pdk,
            candidate_width=path_width,
        )
        if not cutters or len(path_points) < 2 or path_width <= 0.0:
            paths.append(path)
            continue
        reroute_points = _path_points_around_cutters(
            path_points,
            path_width,
            cutters,
            layer=path_layer,
            corridor_hints=corridor_hints,
        )
        if reroute_points and reroute_points != path_points:
            edit_bbox = _path_bbox_from_points(path_points, path_width)
            target_bbox = _path_bbox_from_points(reroute_points, path_width)
            edits.append(GeometryEdit("reroute_short_victim_path", path_layer, edit_bbox, target_bbox, f"reroute LVS short around {keep_net}", path_net, f"path_{path_idx}"))
            paths.append(
                LayoutPath(
                    path_layer,
                    reroute_points,
                    path_width,
                    path_net,
                    str(getattr(path, "purpose", "drawing")),
                    {
                        **dict(getattr(path, "metadata", {})),
                        "action": "reroute_short_victim_path",
                        "source_path": path_idx,
                        "guide_hint_count": len(corridor_hints),
                    },
                )
            )
            continue
        segments = _path_segments_after_cutters(path_points, path_width, cutters)
        original_segments = tuple((start, end) for start, end in zip(path_points, path_points[1:]))
        if segments == original_segments:
            paths.append(path)
            continue
        edit_bbox = _path_bbox_from_points(path_points, path_width)
        target_bbox = _bbox_union_many(tuple(_path_bbox_from_points(segment, path_width) for segment in segments))
        edits.append(GeometryEdit("replace_short_victim_path", path_layer, edit_bbox, target_bbox, f"cut LVS short against {keep_net}", path_net, f"path_{path_idx}"))
        for piece_idx, segment in enumerate(segments):
            paths.append(
                LayoutPath(
                    path_layer,
                    segment,
                    path_width,
                    path_net,
                    str(getattr(path, "purpose", "drawing")),
                    {**dict(getattr(path, "metadata", {})), "action": "replace_short_victim_path", "source_path": path_idx, "piece": piece_idx},
                )
            )
    for via_idx, via in enumerate(tuple(getattr(base, "vias", ()))):
        via_net = str(getattr(via, "net", ""))
        if _via_matches_short_source(target_pairs, keep_net, victim_net, via_net, f"via[{via_idx}]"):
            edits.append(GeometryEdit("remove_short_victim_via", "", _short_source_bbox(target_pairs, victim_net or via_net, f"via[{via_idx}]"), None, f"remove via short against {keep_net}", via_net, f"via_{via_idx}"))
            continue
        vias.append(via)
    source_cell = getattr(base, "cell", LayoutCellRef(lib or "work", cell or "lvs_short_replacement", view or "layout", "maskLayout"))
    target_cell = LayoutCellRef(lib or source_cell.lib, cell or source_cell.cell, view or source_cell.view, source_cell.view_type)
    replacement = LayoutPlan(
        target_cell,
        nets=tuple(getattr(base, "nets", ())),
        pins=tuple(getattr(base, "pins", ())),
        instances=tuple(getattr(base, "instances", ())),
        rects=tuple(rects),
        paths=tuple(paths),
        vias=tuple(vias),
        labels=tuple(getattr(base, "labels", ())),
        metadata={**dict(getattr(base, "metadata", {})), "source": "plan_lvs_short_replacement", "geometry_edit_count": len(edits)},
    )
    if pdk is not None:
        replacement = snap_layout_plan_to_grid(replacement, pdk)
    physical_report = analyze_plan_physical_connectivity(replacement, include_opens=True, include_via_landing_shorts=True, pdk=pdk)
    interconnect_report = analyze_interconnect_plan(replacement, pdk=pdk, include_open_checks=True)
    after_shorts = detect_plan_shape_shorts(replacement, include_via_landings=True, pdk=pdk)
    return LvsShortReplacementPlan(tuple(edits), replacement, layout_plan_to_oa_write_plan(replacement), physical_report, interconnect_report, before_shorts, after_shorts)


def plan_drc_ecos(
    issues: list[DrcIssue] | tuple[DrcIssue, ...],
    *,
    min_width: float = 0.0,
    enclosure: float = 0.0,
    spacing: float = 0.0,
    layer_aliases: Mapping[str, str] | None = None,
    lib: str = "work",
    cell: str = "drc_eco",
    view: str = "layout",
    pdk: PdkConfig | None = None,
) -> DrcEcoPlan:
    """Group DRC ECO suggestions into geometry, density-fill, and review buckets."""

    aliases = {str(key): str(value) for key, value in dict(layer_aliases or {}).items()}
    aliased_issues = tuple(_alias_issue_layer(issue, aliases) for issue in issues)
    suggestions = suggest_drc_ecos(aliased_issues)
    by_rule = {suggestion.rule: suggestion for suggestion in suggestions}
    geometry_actions = {"widen_shape", "grow_enclosure", "grow_via_landing_or_enclosure", "move_or_reroute", "replace_with_via_array"}
    geometry_issues = [issue for issue in aliased_issues if by_rule.get(issue.rule) and by_rule[issue.rule].action in geometry_actions]
    density_issues = tuple(issue for issue in aliased_issues if by_rule.get(issue.rule) and by_rule[issue.rule].action == "plan_density_fill")
    review_issues = tuple(issue for issue in aliased_issues if by_rule.get(issue.rule) and by_rule[issue.rule].action not in {*geometry_actions, "plan_density_fill"})
    geometry_edits = tuple(propose_geometric_drc_edits(geometry_issues, min_width=min_width, enclosure=enclosure, spacing=spacing))
    layout_proposal = _layout_proposal_for_geometry_edits(geometry_edits, lib=lib, cell=cell, view=view, pdk=pdk)
    return DrcEcoPlan(suggestions, geometry_edits, density_issues, review_issues, layout_proposal)


def compare_drc_eco_results(
    before: list[DrcIssue] | tuple[DrcIssue, ...],
    after: list[DrcIssue] | tuple[DrcIssue, ...],
    *,
    layer_aliases: Mapping[str, str] | None = None,
) -> DrcEcoComparison:
    """Compare DRC reports before and after an ECO without applying fixes."""

    aliases = {str(key): str(value) for key, value in dict(layer_aliases or {}).items()}
    before_issues = tuple(_alias_issue_layer(issue, aliases) for issue in before)
    after_issues = tuple(_alias_issue_layer(issue, aliases) for issue in after)
    before_counts = _drc_rule_counts(before_issues)
    after_counts = _drc_rule_counts(after_issues)
    rules = tuple(sorted(set(before_counts) | set(after_counts)))
    rule_deltas = {rule: after_counts.get(rule, 0) - before_counts.get(rule, 0) for rule in rules}
    fixed_rules = tuple(rule for rule in sorted(before_counts) if after_counts.get(rule, 0) == 0)
    new_rules = tuple(rule for rule in sorted(after_counts) if before_counts.get(rule, 0) == 0)
    remaining_rules = tuple(rule for rule in sorted(before_counts) if after_counts.get(rule, 0) > 0)
    before_count = sum(before_counts.values())
    after_count = sum(after_counts.values())
    improved = after_count < before_count and not new_rules
    passed = after_count == 0
    next_actions = _drc_comparison_next_actions(after_issues, new_rules)
    return DrcEcoComparison(
        before_count=before_count,
        after_count=after_count,
        fixed_rules=fixed_rules,
        new_rules=new_rules,
        remaining_rules=remaining_rules,
        rule_deltas=rule_deltas,
        improved=improved,
        passed=passed,
        next_actions=next_actions,
    )


def plan_drc_repair_candidates(
    issues: list[DrcIssue] | tuple[DrcIssue, ...],
    *,
    layout_plan: object,
    pdk: PdkConfig | None = None,
    min_width_by_layer: Mapping[str, float] | None = None,
    min_area_by_layer: Mapping[str, float] | None = None,
    min_spacing_by_layer: Mapping[str, float] | None = None,
    via_def_by_layer: Mapping[str, str] | None = None,
    fixed_nets: tuple[str, ...] = (),
    include_via_array_enclosures: bool = True,
    landing_margin_um: float | None = None,
    max_candidates: int | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> tuple[DrcRepairCandidate, ...]:
    """Build executable DRC repair candidates from localized issues."""

    issue_records = tuple(issues)
    candidates: list[DrcRepairCandidate] = []
    width_area = tuple(
        issue
        for issue in issue_records
        if _is_width_rule(issue.rule.upper(), issue.message.lower())
        or _is_area_or_antenna_rule(issue.rule.upper(), issue.message.lower())
        or "min-area" in issue.message.lower()
        or "minimum area" in issue.message.lower()
    )
    if width_area:
        localized = localize_drc_issues_to_layout(width_area, layout_plan)
        patch = plan_localized_drc_layout_patch(
            localized,
            min_width_by_layer=min_width_by_layer,
            min_area_by_layer=min_area_by_layer,
            pdk=pdk,
            base_plan=layout_plan,
            strict_precheck=True,
        )
        if patch.edits:
            candidates.append(_make_drc_repair_candidate(width_area, "localized_drc_patch", patch, layout_plan=layout_plan, hierarchy_context=hierarchy_context))
    spacing_issues = tuple(issue for issue in issue_records if _is_spacing_rule(issue.rule.upper(), issue.message.lower()))
    if spacing_issues:
        spacings = {str(layer): float(value) for layer, value in dict(min_spacing_by_layer or {}).items()}
        localized_spacing = localize_spacing_drc_issues_to_layout(spacing_issues, layout_plan, min_spacing_by_layer=spacings)
        if localized_spacing:
            replacement = plan_localized_spacing_replacement(
                localized_spacing,
                base_plan=layout_plan,
                fixed_nets=fixed_nets,
                pdk=pdk,
                min_spacing=max((float(item.required_spacing) for item in localized_spacing), default=0.0),
            )
            if replacement.edits:
                candidates.append(_make_drc_repair_candidate(spacing_issues, "spacing_replacement", replacement, layout_plan=layout_plan, hierarchy_context=hierarchy_context))
    via_issues = tuple(issue for issue in issue_records if _is_via_or_contact_rule(issue.rule.upper(), issue.layer, issue.message.lower()))
    if via_issues:
        localized_via = localize_drc_issues_to_layout(via_issues, layout_plan)
        if localized_via:
            via_patch = plan_localized_via_array_patch(
                localized_via,
                via_def_by_layer=via_def_by_layer,
                pdk=pdk,
                base_plan=layout_plan,
                include_landing_enclosures=include_via_array_enclosures,
                landing_margin_um=landing_margin_um,
            )
            if via_patch.edits:
                candidates.append(_make_drc_repair_candidate(via_issues, "via_array_patch", via_patch, layout_plan=layout_plan, hierarchy_context=hierarchy_context))
        if pdk is not None and any(_is_enclosure_rule(issue.rule.upper(), issue.message.lower()) for issue in via_issues):
            enclosure_patch = plan_via_enclosure_patch(layout_plan, pdk, landing_margin_um=landing_margin_um)
            if enclosure_patch.edits:
                candidates.append(_make_drc_repair_candidate(via_issues, "via_enclosure_patch", enclosure_patch, layout_plan=layout_plan, hierarchy_context=hierarchy_context))
    ranked = tuple(sorted(candidates, key=lambda candidate: (candidate.score, candidate.plan_kind)))
    return ranked if max_candidates is None else ranked[:max_candidates]


def select_drc_repair_candidate(
    issues: list[DrcIssue] | tuple[DrcIssue, ...],
    *,
    layout_plan: object,
    pdk: PdkConfig | None = None,
    min_width_by_layer: Mapping[str, float] | None = None,
    min_area_by_layer: Mapping[str, float] | None = None,
    min_spacing_by_layer: Mapping[str, float] | None = None,
    via_def_by_layer: Mapping[str, str] | None = None,
    fixed_nets: tuple[str, ...] = (),
    include_via_array_enclosures: bool = True,
    landing_margin_um: float | None = None,
    max_candidates: int | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> tuple[DrcRepairCandidate, tuple[DrcRepairCandidate, ...]]:
    """Rank DRC repair candidates and return the selected candidate."""

    ranked = plan_drc_repair_candidates(
        issues,
        layout_plan=layout_plan,
        pdk=pdk,
        min_width_by_layer=min_width_by_layer,
        min_area_by_layer=min_area_by_layer,
        min_spacing_by_layer=min_spacing_by_layer,
        via_def_by_layer=via_def_by_layer,
        fixed_nets=fixed_nets,
        include_via_array_enclosures=include_via_array_enclosures,
        landing_margin_um=landing_margin_um,
        max_candidates=max_candidates,
        hierarchy_context=hierarchy_context,
    )
    if not ranked:
        raise ValueError("no executable DRC repair candidates were produced")
    return ranked[0], ranked


def build_drc_repair_proposal(
    issues: list[DrcIssue] | tuple[DrcIssue, ...],
    *,
    layout_plan: object,
    pdk: PdkConfig | None = None,
    min_width: float = 0.0,
    enclosure: float = 0.0,
    spacing: float = 0.0,
    min_width_by_layer: Mapping[str, float] | None = None,
    min_area_by_layer: Mapping[str, float] | None = None,
    min_spacing_by_layer: Mapping[str, float] | None = None,
    via_def_by_layer: Mapping[str, str] | None = None,
    layer_aliases: Mapping[str, str] | None = None,
    fixed_nets: tuple[str, ...] = (),
    include_via_array_enclosures: bool = True,
    landing_margin_um: float | None = None,
    max_candidates: int | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
    lib: str = "work",
    cell: str = "drc_eco",
    view: str = "layout",
) -> DrcRepairProposal:
    """Build a DRC ECO summary and select the best executable geometry repair."""

    eco_plan = plan_drc_ecos(
        issues,
        min_width=min_width,
        enclosure=enclosure,
        spacing=spacing,
        layer_aliases=layer_aliases,
        lib=lib,
        cell=cell,
        view=view,
        pdk=pdk,
    )
    candidates = plan_drc_repair_candidates(
        issues,
        layout_plan=layout_plan,
        pdk=pdk,
        min_width_by_layer=min_width_by_layer,
        min_area_by_layer=min_area_by_layer,
        min_spacing_by_layer=min_spacing_by_layer,
        via_def_by_layer=via_def_by_layer,
        fixed_nets=fixed_nets,
        include_via_array_enclosures=include_via_array_enclosures,
        landing_margin_um=landing_margin_um,
        max_candidates=max_candidates,
        hierarchy_context=hierarchy_context,
    )
    if not candidates:
        fallback_candidate = _build_drc_geometry_patch_fallback_candidate(
            tuple(issues),
            eco_plan=eco_plan,
            layout_plan=layout_plan,
            pdk=pdk,
            hierarchy_context=hierarchy_context,
        )
        if fallback_candidate is not None:
            candidates = (fallback_candidate,)
    return DrcRepairProposal(eco_plan, candidates, candidates[0] if candidates else None)


def propose_lvs_repairs(issues: list[LvsIssue]) -> list[dict[str, object]]:
    repairs = []
    for issue in issues:
        lowered = issue.message.lower()
        if issue.kind == "open" or "open" in lowered or "missing" in lowered:
            repairs.append({"action": "route_missing_connection", "net": issue.net, "reason": issue.message})
        elif issue.kind == "short" or "short" in lowered:
            repairs.append({"action": "split_or_reroute_short", "net": issue.net, "reason": issue.message})
        elif "pin" in lowered or "label" in lowered:
            repairs.append({"action": "fix_pin_or_label", "net": issue.net, "reason": issue.message})
        else:
            repairs.append({"action": "manual_lvs_review", "net": issue.net, "reason": issue.message})
    return repairs


def suggest_lvs_ecos(
    issues: list[LvsIssue] | tuple[LvsIssue, ...],
    *,
    layout_plan: object | None = None,
    floorplan: object | None = None,
    pin_label_report: Mapping[str, object] | None = None,
    max_suggestions: int | None = None,
) -> tuple[LvsEcoSuggestion, ...]:
    """Map LVS report issues to route, pin/label, corridor, or calibration owners."""

    suggestions = tuple(
        _suggest_lvs_eco_for_issue(
            issue,
            layout_plan=layout_plan,
            floorplan=floorplan,
            pin_label_report=pin_label_report,
        )
        for issue in issues
    )
    ranked = tuple(sorted(suggestions, key=lambda item: (-item.priority, item.owner, item.action, item.net)))
    return ranked if max_suggestions is None else ranked[:max_suggestions]


def plan_lvs_repairs(
    issues: list[LvsIssue] | tuple[LvsIssue, ...],
    *,
    layout_plan: object | None = None,
    floorplan: object | None = None,
    pin_label_report: Mapping[str, object] | None = None,
    max_items: int | None = None,
) -> LvsRepairPlan:
    """Build an owner-scoped LVS repair plan with evidence preserved per issue."""

    issue_records = tuple(issues)
    suggestions = tuple(
        _suggest_lvs_eco_for_issue(
            issue,
            layout_plan=layout_plan,
            floorplan=floorplan,
            pin_label_report=pin_label_report,
        )
        for issue in issue_records
    )
    items = tuple(
        sorted(
            (
                LvsRepairItem(issue, suggestion, suggestion.evidence, suggestion.priority)
                for issue, suggestion in zip(issue_records, suggestions)
            ),
            key=lambda item: (-item.priority, item.suggestion.owner, item.suggestion.action, item.suggestion.net),
        )
    )
    if max_items is not None:
        items = items[:max_items]
    owners: dict[str, int] = {}
    for item in items:
        owners[item.suggestion.owner] = owners.get(item.suggestion.owner, 0) + 1
    unresolved = tuple(item.issue for item in items if item.suggestion.owner == "manual" or not item.suggestion.evidence and item.priority < 80)
    actions = tuple(dict.fromkeys(item.suggestion.action for item in items))
    return LvsRepairPlan(items, owners, actions, unresolved, passed=not issue_records)


def plan_lvs_repair_candidates(
    repair_plan: LvsRepairPlan,
    *,
    layout_plan: object,
    pdk: PdkConfig | None = None,
    top_level_nets: tuple[str, ...] | list[str] | None = None,
    require_explicit_labels: bool = True,
    min_width_by_layer: Mapping[str, float] | None = None,
    min_spacing_by_layer: Mapping[str, float] | None = None,
    max_candidates: int | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> tuple[LvsRepairCandidate, ...]:
    """Build executable LVS repair candidates and rank them by closure quality."""

    candidates: list[LvsRepairCandidate] = []
    top_nets = tuple(dict.fromkeys(str(net) for net in tuple(top_level_nets or ()) if str(net)))
    for item in repair_plan.items:
        suggestion = item.suggestion
        target_nets = tuple(dict.fromkeys(str(net) for net in (suggestion.net, *suggestion.peer_nets) if str(net)))
        if suggestion.owner == "pin_label" and suggestion.net:
            patch = plan_lvs_pin_label_patch(
                layout_plan,
                top_level_nets=target_nets,
                pdk=pdk,
                require_explicit_labels=require_explicit_labels,
            )
            if patch.edits:
                candidates.append(_make_lvs_repair_candidate(item, "pin_label_patch", patch, layout_plan=layout_plan, hierarchy_context=hierarchy_context))
            continue
        if suggestion.owner == "model_pcell":
            handoff = _build_lvs_model_pcell_handoff_plan(item, layout_plan=layout_plan)
            candidates.append(_make_lvs_repair_candidate(item, "model_pcell_handoff", handoff, layout_plan=layout_plan, hierarchy_context=hierarchy_context))
            continue
        if suggestion.owner not in {"routing", "terminal_access"} or not suggestion.net:
            continue
        if suggestion.action in {"route_missing_connection", "recalibrate_terminal_or_add_route"}:
            patch = plan_lvs_open_route_patch(
                layout_plan,
                pdk=pdk,
                net=suggestion.net,
                min_width_by_layer=min_width_by_layer,
                min_spacing_by_layer=min_spacing_by_layer,
            )
            if patch.edits:
                candidates.append(_make_lvs_repair_candidate(item, "open_route_patch", patch, layout_plan=layout_plan, hierarchy_context=hierarchy_context))
                continue
            if suggestion.net in top_nets:
                label_patch = plan_lvs_pin_label_patch(
                    layout_plan,
                    top_level_nets=target_nets,
                    pdk=pdk,
                    require_explicit_labels=require_explicit_labels,
                )
                if label_patch.edits:
                    candidates.append(_make_lvs_repair_candidate(item, "pin_label_fallback", label_patch, layout_plan=layout_plan, hierarchy_context=hierarchy_context))
            continue
        if suggestion.action in {"split_or_reroute_short", "reroute_or_change_layer"}:
            nets = tuple(dict.fromkeys((suggestion.net, *suggestion.peer_nets)))
            if len(nets) < 2:
                continue
            for keep_net, victim_net in ((nets[0], nets[1]), (nets[1], nets[0])):
                replacement = plan_lvs_short_replacement(
                    layout_plan,
                    keep_net=keep_net,
                    victim_net=victim_net,
                    pdk=pdk,
                    min_spacing_by_layer=min_spacing_by_layer,
                )
                if replacement.edits:
                    candidates.append(_make_lvs_repair_candidate(item, "short_replacement", replacement, layout_plan=layout_plan, hierarchy_context=hierarchy_context))
    ranked = tuple(sorted(candidates, key=lambda candidate: (candidate.score, candidate.plan_kind, candidate.item.issue.net)))
    return ranked if max_candidates is None else ranked[:max_candidates]


def select_lvs_repair_candidate(
    repair_plan: LvsRepairPlan,
    *,
    layout_plan: object,
    pdk: PdkConfig | None = None,
    top_level_nets: tuple[str, ...] | list[str] | None = None,
    require_explicit_labels: bool = True,
    min_width_by_layer: Mapping[str, float] | None = None,
    min_spacing_by_layer: Mapping[str, float] | None = None,
    max_candidates: int | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> tuple[LvsRepairCandidate, tuple[LvsRepairCandidate, ...]]:
    """Rank LVS repair candidates and return the selected candidate."""

    ranked = plan_lvs_repair_candidates(
        repair_plan,
        layout_plan=layout_plan,
        pdk=pdk,
        top_level_nets=top_level_nets,
        require_explicit_labels=require_explicit_labels,
        min_width_by_layer=min_width_by_layer,
        min_spacing_by_layer=min_spacing_by_layer,
        max_candidates=max_candidates,
        hierarchy_context=hierarchy_context,
    )
    if not ranked:
        raise ValueError("no executable LVS repair candidates were produced")
    return ranked[0], ranked


def build_lvs_repair_proposal(
    issues: list[LvsIssue] | tuple[LvsIssue, ...],
    *,
    layout_plan: object,
    pdk: PdkConfig | None = None,
    floorplan: object | None = None,
    pin_label_report: Mapping[str, object] | None = None,
    top_level_nets: tuple[str, ...] | list[str] | None = None,
    require_explicit_labels: bool = True,
    min_width_by_layer: Mapping[str, float] | None = None,
    min_spacing_by_layer: Mapping[str, float] | None = None,
    max_items: int | None = None,
    max_candidates: int | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> LvsRepairProposal:
    """Build an LVS repair plan and select the best executable candidate."""

    repair_plan = plan_lvs_repairs(
        issues,
        layout_plan=layout_plan,
        floorplan=floorplan,
        pin_label_report=pin_label_report,
        max_items=max_items,
    )
    candidates = plan_lvs_repair_candidates(
        repair_plan,
        layout_plan=layout_plan,
        pdk=pdk,
        top_level_nets=top_level_nets,
        require_explicit_labels=require_explicit_labels,
        min_width_by_layer=min_width_by_layer,
        min_spacing_by_layer=min_spacing_by_layer,
        max_candidates=max_candidates,
        hierarchy_context=hierarchy_context,
    )
    return LvsRepairProposal(repair_plan, candidates, candidates[0] if candidates else None)


def compare_lvs_eco_results(
    before: list[LvsIssue] | tuple[LvsIssue, ...],
    after: list[LvsIssue] | tuple[LvsIssue, ...],
) -> LvsEcoComparison:
    """Compare LVS reports before and after an ECO without applying fixes."""

    before_issues = tuple(before)
    after_issues = tuple(after)
    before_counts = _lvs_issue_counts(before_issues)
    after_counts = _lvs_issue_counts(after_issues)
    keys = tuple(sorted(set(before_counts) | set(after_counts)))
    issue_deltas = {key: after_counts.get(key, 0) - before_counts.get(key, 0) for key in keys}
    fixed = tuple(key for key in sorted(before_counts) if after_counts.get(key, 0) == 0)
    new = tuple(key for key in sorted(after_counts) if before_counts.get(key, 0) == 0)
    remaining = tuple(key for key in sorted(before_counts) if after_counts.get(key, 0) > 0)
    before_count = sum(before_counts.values())
    after_count = sum(after_counts.values())
    improved = after_count < before_count and not new
    passed = after_count == 0
    next_actions = _lvs_comparison_next_actions(after_issues, new)
    return LvsEcoComparison(
        before_count=before_count,
        after_count=after_count,
        fixed=fixed,
        new=new,
        remaining=remaining,
        issue_deltas=issue_deltas,
        improved=improved,
        passed=passed,
        next_actions=next_actions,
    )



def propose_geometric_drc_edits(issues: list[DrcIssue], *, min_width: float = 0.0, enclosure: float = 0.0, spacing: float = 0.0) -> list[GeometryEdit]:
    edits: list[GeometryEdit] = []
    for issue in issues:
        text = f"{issue.rule} {issue.message}".lower()
        bbox = _float_bbox(issue.bbox)
        if "width" in text:
            edits.append(GeometryEdit("widen_shape", issue.layer, bbox, _widen_bbox(bbox, min_width), issue.message))
        elif "enclosure" in text or "enc" in text:
            edits.append(GeometryEdit("grow_enclosure", issue.layer, bbox, _expand_bbox(bbox, enclosure), issue.message))
        elif "spacing" in text or "space" in text:
            edits.append(GeometryEdit("shift_or_reroute", issue.layer, bbox, _shift_bbox(bbox, spacing, 0.0), issue.message))
        elif "via" in text:
            edits.append(GeometryEdit("replace_with_via_array", issue.layer, bbox, bbox, issue.message))
        else:
            edits.append(GeometryEdit("manual_drc_review", issue.layer, bbox, bbox, issue.message))
    return edits


def _float_bbox(bbox: tuple[float, float, float, float] | None) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    return (float(x0), float(y0), float(x1), float(y1))


def _expand_bbox(bbox: tuple[float, float, float, float] | None, amount: float) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    return (x0 - amount, y0 - amount, x1 + amount, y1 + amount)


def _shift_bbox(bbox: tuple[float, float, float, float] | None, dx: float, dy: float) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    return (x0 + dx, y0 + dy, x1 + dx, y1 + dy)


def _push_bbox_away(
    moving: tuple[float, float, float, float],
    fixed: tuple[float, float, float, float],
    min_spacing: float,
) -> tuple[float, float, float, float]:
    if min_spacing <= 0.0:
        return moving
    x0, y0, x1, y1 = moving
    gap_left = fixed[0] - x1
    gap_right = x0 - fixed[2]
    gap_below = fixed[1] - y1
    gap_above = y0 - fixed[3]
    candidates: list[tuple[float, float, float, float, float]] = []
    if 0.0 <= gap_left < min_spacing:
        delta = min_spacing - gap_left
        candidates.append((-delta, 0.0, 0.0, 0.0, delta))
    if 0.0 <= gap_right < min_spacing:
        delta = min_spacing - gap_right
        candidates.append((delta, 0.0, 0.0, 0.0, delta))
    if 0.0 <= gap_below < min_spacing:
        delta = min_spacing - gap_below
        candidates.append((0.0, -delta, 0.0, 0.0, delta))
    if 0.0 <= gap_above < min_spacing:
        delta = min_spacing - gap_above
        candidates.append((0.0, delta, 0.0, 0.0, delta))
    valid = tuple(item for item in candidates if item[4] > 0.0)
    if valid:
        dx, dy, _, _, _ = min(valid, key=lambda item: item[4])
        return (x0 + dx, y0 + dy, x1 + dx, y1 + dy)

    overlap_x = min(x1, fixed[2]) - max(x0, fixed[0])
    overlap_y = min(y1, fixed[3]) - max(y0, fixed[1])
    moving_cx = (x0 + x1) / 2.0
    fixed_cx = (fixed[0] + fixed[2]) / 2.0
    moving_cy = (y0 + y1) / 2.0
    fixed_cy = (fixed[1] + fixed[3]) / 2.0
    if overlap_x <= overlap_y:
        dx = -(overlap_x + min_spacing) if moving_cx <= fixed_cx else overlap_x + min_spacing
        return (x0 + dx, y0, x1 + dx, y1)
    dy = -(overlap_y + min_spacing) if moving_cy <= fixed_cy else overlap_y + min_spacing
    return (x0, y0 + dy, x1, y1 + dy)


def _spacing_movable_shape(left: LayoutShape, right: LayoutShape, fixed_nets: set[str]) -> LayoutShape:
    left_landing = _shape_is_required_landing(left)
    right_landing = _shape_is_required_landing(right)
    if left_landing and not right_landing:
        return right
    if right_landing and not left_landing:
        return left
    if left.net in fixed_nets and right.net not in fixed_nets:
        return right
    if right.net in fixed_nets and left.net not in fixed_nets:
        return left
    return right


def _shape_is_required_landing(shape: LayoutShape) -> bool:
    metadata = getattr(shape, "metadata", {})
    if not isinstance(metadata, Mapping):
        return False
    kind = str(metadata.get("kind", "") or "")
    action = str(metadata.get("action", "") or "")
    reason = str(metadata.get("reason", "") or "")
    return kind == "via_landing" or action in {"grow_via_landing_or_enclosure", "add_lvs_open_via_landing"} or "landing" in reason.lower()


def _same_net_landing_covered(layout_plan: object, net: str, layer: str, landing: tuple[float, float, float, float]) -> bool:
    for shape in _layout_plan_shapes(layout_plan):
        if shape.net == net and shape.layer == layer and _bbox_contains(shape.bbox, landing):
            return True
    return False


def _same_net_rect_landing_covered(layout_plan: object, net: str, layer: str, landing: tuple[float, float, float, float]) -> bool:
    for rect in tuple(getattr(layout_plan, "rects", ())):
        if str(getattr(rect, "net", "")) != net or str(getattr(rect, "layer", "")) != layer:
            continue
        bbox = _float_bbox(getattr(rect, "bbox", None))
        if bbox is not None and _bbox_contains(bbox, landing):
            return True
    return False


def _same_net_landing_anchors(
    layout_plan: object,
    net: str,
    layer: str,
    landing: tuple[float, float, float, float],
    *,
    direction: str,
) -> tuple[LayoutShape, ...]:
    anchors: list[LayoutShape] = []
    for shape in _layout_plan_shapes(layout_plan):
        if shape.net != net or shape.layer != layer:
            continue
        if _bbox_overlaps(shape.bbox, landing):
            anchors.append(shape)
    if not anchors:
        return ()

    horizontal = direction in {"left", "right"}
    landing_orth_span = max(landing[3] - landing[1], 0.0) if horizontal else max(landing[2] - landing[0], 0.0)

    def rank(shape: LayoutShape) -> tuple[float, float, float]:
        bbox = shape.bbox
        inter = rect_intersection(bbox, landing)
        overlap_area = 0.0 if inter is None else rect_area(inter)
        orth_span = max(bbox[3] - bbox[1], 0.0) if horizontal else max(bbox[2] - bbox[0], 0.0)
        return (abs(orth_span - landing_orth_span), -overlap_area, rect_area(bbox))

    return tuple(sorted(anchors, key=rank))


def _rectangularized_neighbor_landing(
    anchor: tuple[float, float, float, float],
    landing: tuple[float, float, float, float],
    direction: str,
) -> tuple[float, float, float, float]:
    if direction == "right":
        return (min(anchor[2], landing[0]), min(anchor[1], landing[1]), max(anchor[2], landing[2]), max(anchor[3], landing[3]))
    if direction == "left":
        return (min(anchor[0], landing[0]), min(anchor[1], landing[1]), max(anchor[0], landing[2]), max(anchor[3], landing[3]))
    if direction == "up":
        return (min(anchor[0], landing[0]), min(anchor[3], landing[1]), max(anchor[2], landing[2]), max(anchor[3], landing[3]))
    if direction == "down":
        return (min(anchor[0], landing[0]), min(anchor[1], landing[1]), max(anchor[2], landing[2]), max(anchor[1], landing[3]))
    return landing


def _effective_redundant_via_neighbor_landings(
    layout_plan: object,
    *,
    net: str,
    via_def: str,
    original_cut: tuple[float, float, float, float],
    cut_bbox: tuple[float, float, float, float],
    direction: str,
    raw_landing_rects: tuple[tuple[str, tuple[float, float, float, float]], ...],
    pdk: PdkConfig | None,
) -> tuple[tuple[str, tuple[float, float, float, float]], ...]:
    rules = getattr(pdk, "rules", None)
    result: list[tuple[str, tuple[float, float, float, float]]] = []
    for layer, raw_landing in raw_landing_rects:
        landing = raw_landing
        if _same_net_rect_landing_covered(layout_plan, net, layer, landing):
            continue
        anchors: tuple[LayoutShape, ...] = ()
        if str(layer).upper() == "M1":
            anchors = _same_net_landing_anchors(layout_plan, net, layer, landing, direction=direction)
            if not anchors:
                enclosure = _via_enclosure_um(pdk, via_def, layer)
                original_landing = _expand_bbox(original_cut, enclosure) or original_cut
                anchors = _same_net_landing_anchors(layout_plan, net, layer, original_landing, direction=direction)
        if anchors:
            landing = _rectangularized_neighbor_landing(anchors[0].bbox, landing, direction)
        else:
            enclosure = _via_enclosure_um(pdk, via_def, layer)
            landing = _expand_bbox(_bbox_union(original_cut, cut_bbox), enclosure) or landing
        if not _bbox_contains(landing, raw_landing):
            landing = _bbox_union(landing, raw_landing)
        if rules is not None and hasattr(rules, "snap_bbox_um"):
            landing = rules.snap_bbox_um(landing, mode="outward")
        if _same_net_rect_landing_covered(layout_plan, net, layer, landing):
            continue
        result.append((layer, landing))
    return tuple(result)


def _landing_spacing_is_legal(
    layout_plan: object,
    layer: str,
    net: str,
    bbox: tuple[float, float, float, float],
    *,
    pdk: PdkConfig | None,
    allow_same_net_spacing: bool = False,
    tol_um: float = 1e-9,
) -> bool:
    min_spacing = _rule_min_spacing_um(pdk, layer, 0.0)
    if min_spacing <= 0.0:
        return True
    for shape in _layout_plan_shapes(layout_plan):
        if shape.layer != layer:
            continue
        same_net = bool(net) and shape.net == net
        if same_net and allow_same_net_spacing:
            continue
        if rect_intersection(bbox, shape.bbox) is not None:
            if same_net:
                continue
            return False
        distance = _rect_axis_distance(bbox, shape.bbox)
        if same_net and distance <= tol_um:
            continue
        if distance + tol_um < min_spacing:
            return False
    return True


def _first_shape_for_net(layout_plan: object, net: str) -> LayoutShape | None:
    for shape in _layout_plan_shapes(layout_plan):
        if shape.net == net:
            return shape
    return None


def _label_net(label: object) -> str:
    if isinstance(label, (tuple, list)) and len(label) == 3:
        return str(label[1])
    return str(getattr(label, "text", ""))


def _bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _same_net_components(
    shapes: tuple[object, ...],
    net: str,
    *,
    preferred_layer: str = "",
    layout_plan: object | None = None,
    pdk: PdkConfig | None = None,
) -> tuple[tuple[object, ...], ...]:
    candidates = tuple(shape for shape in shapes if str(getattr(shape, "net", "")) == net and (not preferred_layer or str(getattr(shape, "layer", "")) == preferred_layer))
    if not candidates and preferred_layer:
        candidates = tuple(shape for shape in shapes if str(getattr(shape, "net", "")) == net)
    parent = {idx: idx for idx in range(len(candidates))}
    for idx, left in enumerate(candidates):
        for jdx, right in enumerate(candidates[idx + 1 :], start=idx + 1):
            if str(getattr(left, "layer", "")) == str(getattr(right, "layer", "")) and _bbox_overlaps(_shape_bbox(left), _shape_bbox(right)):
                _union_local(parent, idx, jdx)
    if layout_plan is not None and pdk is not None:
        try:
            from analogskills.layout.physical import _same_net_cut_rect_links, _same_net_via_links

            via_link_groups = (
                *tuple(_same_net_via_links(layout_plan, net, pdk)),
                *tuple(_same_net_cut_rect_links(layout_plan, net, pdk)),
            )
        except Exception:
            via_link_groups = ()
        for via_links in via_link_groups:
            touched = [
                idx
                for idx, shape in enumerate(candidates)
                if any(
                    str(getattr(shape, "layer", "")) == str(via_layer)
                    and _bbox_overlaps(_shape_bbox(shape), _bbox_tuple(via_bbox))
                    for via_layer, via_bbox in tuple(via_links)
                )
            ]
            for left_idx, right_idx in zip(touched, touched[1:]):
                _union_local(parent, left_idx, right_idx)
    groups: dict[int, list[object]] = {}
    for idx, shape in enumerate(candidates):
        groups.setdefault(_find_local(parent, idx), []).append(shape)
    return tuple(tuple(group) for group in groups.values())


def _closest_component_pair(components: tuple[tuple[object, ...], ...]) -> tuple[tuple[object, ...], tuple[object, ...]] | None:
    bridge = _closest_component_bridge(components)
    if bridge is not None:
        return bridge[0], bridge[1]
    return None


def _closest_component_bridge(
    components: tuple[tuple[object, ...], ...],
) -> tuple[tuple[object, ...], tuple[object, ...], str, tuple[float, float], tuple[float, float]] | None:
    candidates = _component_bridge_candidates(components)
    return candidates[0] if candidates else None


def _component_bridge_candidates(
    components: tuple[tuple[object, ...], ...],
) -> tuple[tuple[tuple[object, ...], tuple[object, ...], str, tuple[float, float], tuple[float, float]], ...]:
    candidates: list[tuple[float, int, tuple[object, ...], tuple[object, ...], str, tuple[float, float], tuple[float, float]]] = []
    seen: set[tuple[int, int, str, tuple[float, float], tuple[float, float]]] = set()
    for idx, left in enumerate(components):
        for jdx, right in enumerate(components[idx + 1 :], start=idx + 1):
            for left_shape in left:
                left_layer = str(getattr(left_shape, "layer", "") or "")
                if not left_layer:
                    continue
                for right_shape in right:
                    right_layer = str(getattr(right_shape, "layer", "") or "")
                    if left_layer != right_layer:
                        continue
                    left_bbox = _shape_bbox(left_shape)
                    right_bbox = _shape_bbox(right_shape)
                    distance = _rect_axis_distance(left_bbox, right_bbox)
                    point_pairs = (
                        _nearest_bbox_bridge_points(left_bbox, right_bbox),
                        (_bbox_center(left_bbox), _bbox_center(right_bbox)),
                    )
                    for variant, (start, end) in enumerate(point_pairs):
                        key = (idx, jdx, left_layer, start, end)
                        if key in seen:
                            continue
                        seen.add(key)
                        candidates.append((distance + 0.001 * variant, variant, left, right, left_layer, start, end))
    return tuple((left, right, layer, start, end) for _, _, left, right, layer, start, end in sorted(candidates, key=lambda item: (item[0], item[1])))


def _closest_via_component_pair(
    components: tuple[tuple[object, ...], ...],
    pdk: PdkConfig | None,
) -> tuple[tuple[object, ...], tuple[object, ...], str, tuple[float, float]] | None:
    if pdk is None:
        return None
    best: tuple[float, tuple[object, ...], tuple[object, ...], str, tuple[float, float]] | None = None
    for idx, left in enumerate(components):
        for right in components[idx + 1 :]:
            via_def = _via_between_layers(_component_layer(left), _component_layer(right), pdk)
            if not via_def:
                continue
            overlap = rect_intersection(_component_bbox(left), _component_bbox(right))
            if overlap is None:
                continue
            xy = _bbox_center(overlap)
            distance = _point_distance(_component_anchor(left), _component_anchor(right))
            if best is None or distance < best[0]:
                best = (distance, left, right, via_def, xy)
    return None if best is None else (best[1], best[2], best[3], best[4])


def _via_between_layers(left_layer: str, right_layer: str, pdk: PdkConfig) -> str:
    metals = tuple(getattr(pdk.layer_map, "metals", ()))
    vias = tuple(getattr(pdk.layer_map, "vias", ()))
    if left_layer not in metals or right_layer not in metals:
        return ""
    left_idx = metals.index(left_layer)
    right_idx = metals.index(right_layer)
    if abs(left_idx - right_idx) != 1:
        return ""
    via_idx = min(left_idx, right_idx)
    if via_idx < len(vias):
        return vias[via_idx]
    return f"V{via_idx + 1}"


def _short_other_net(short: object, net: str) -> str:
    net_a = str(getattr(short, "net_a", ""))
    net_b = str(getattr(short, "net_b", ""))
    return net_b if net_a == net else net_a


def _short_bbox_for_net(short: object, net: str) -> tuple[float, float, float, float]:
    net_a = str(getattr(short, "net_a", ""))
    bbox = getattr(short, "bbox_a", None) if net_a == net else getattr(short, "bbox_b", None)
    return _bbox_tuple(bbox)


def _short_keepout_cutters(
    shorts: tuple[object, ...],
    keep_net: str,
    victim_net: str,
    candidate_net: str,
    candidate_layer: str,
    source_prefix: str,
    spacings: Mapping[str, float],
    pdk: PdkConfig | None,
    *,
    candidate_width: float = 0.0,
) -> tuple[tuple[float, float, float, float], ...]:
    cutters: list[tuple[float, float, float, float]] = []
    for short in shorts:
        target_victim = victim_net or _short_other_net(short, keep_net)
        if candidate_net != target_victim or candidate_layer != str(getattr(short, "layer", "")):
            continue
        if not _short_source_matches_net(short, candidate_net, source_prefix):
            continue
        margin = spacings.get(candidate_layer, _min_route_spacing(candidate_layer, pdk))
        if str(source_prefix).startswith("path[") or str(source_prefix).startswith("path_"):
            margin += max(float(candidate_width), 0.0) / 2.0
        keepout = _expand_bbox(_short_bbox_for_net(short, keep_net), margin)
        if keepout is not None:
            cutters.append(keepout)
    return tuple(cutters)


def _short_source_matches_net(short: object, net: str, source_prefix: str) -> bool:
    net_a = str(getattr(short, "net_a", ""))
    source = str(getattr(short, "source_a", "")) if net_a == net else str(getattr(short, "source_b", ""))
    return source == source_prefix or source.startswith(source_prefix + ".")


def _via_matches_short_source(shorts: tuple[object, ...], keep_net: str, victim_net: str, candidate_net: str, source: str) -> bool:
    for short in shorts:
        target_victim = victim_net or _short_other_net(short, keep_net)
        if candidate_net == target_victim and _short_source_matches_net(short, candidate_net, source):
            return True
    return False


def _short_source_bbox(shorts: tuple[object, ...], net: str, source_prefix: str) -> tuple[float, float, float, float] | None:
    boxes = tuple(_short_bbox_for_net(short, net) for short in shorts if _short_source_matches_net(short, net, source_prefix))
    return _bbox_union_many(boxes)


def _component_layer(component: tuple[object, ...]) -> str:
    return str(getattr(component[0], "layer", "")) if component else ""


def _component_anchor(component: tuple[object, ...]) -> tuple[float, float]:
    bbox = _component_bbox(component)
    return _bbox_center(bbox)


def _nearest_bbox_bridge_points(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    x_overlap = (max(left[0], right[0]), min(left[2], right[2]))
    y_overlap = (max(left[1], right[1]), min(left[3], right[3]))
    if x_overlap[0] <= x_overlap[1]:
        x = (x_overlap[0] + x_overlap[1]) / 2.0
        if left[3] <= right[1]:
            return (x, left[3]), (x, right[1])
        if right[3] <= left[1]:
            return (x, left[1]), (x, right[3])
    if y_overlap[0] <= y_overlap[1]:
        y = (y_overlap[0] + y_overlap[1]) / 2.0
        if left[2] <= right[0]:
            return (left[2], y), (right[0], y)
        if right[2] <= left[0]:
            return (left[0], y), (right[2], y)
    left_x = left[2] if left[2] <= right[0] else left[0]
    right_x = right[0] if left[2] <= right[0] else right[2]
    left_y = left[3] if left[3] <= right[1] else left[1]
    right_y = right[1] if left[3] <= right[1] else right[3]
    return (left_x, left_y), (right_x, right_y)


def _component_bbox(component: tuple[object, ...]) -> tuple[float, float, float, float]:
    boxes = tuple(_shape_bbox(shape) for shape in component)
    return (min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes))


def _component_sources(component: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(str(getattr(shape, "source", "")) for shape in component)


def _shape_bbox(shape: object) -> tuple[float, float, float, float]:
    return _bbox_tuple(getattr(shape, "bbox", (0.0, 0.0, 0.0, 0.0)))


def _manhattan_points(start: tuple[float, float], end: tuple[float, float]) -> tuple[tuple[float, float], ...]:
    if start[0] == end[0] or start[1] == end[1]:
        return (start, end)
    mid = (end[0], start[1])
    return (start, mid, end)


def _select_open_route_points(
    start: tuple[float, float],
    end: tuple[float, float],
    layer: str,
    width: float,
    spacing: float,
    net: str,
    shapes: tuple[object, ...],
    *,
    corridor_hints: tuple[dict[str, object], ...] = (),
) -> tuple[tuple[float, float], ...]:
    candidates = _open_route_candidates(start, end, layer, width, spacing, net, shapes, corridor_hints=corridor_hints)
    for points in candidates:
        if not _route_points_hit_foreign_shapes(points, layer, width, spacing, net, shapes):
            return points
    return ()


def _open_route_candidates(
    start: tuple[float, float],
    end: tuple[float, float],
    layer: str,
    width: float,
    spacing: float,
    net: str,
    shapes: tuple[object, ...],
    *,
    corridor_hints: tuple[dict[str, object], ...] = (),
) -> tuple[tuple[tuple[float, float], ...], ...]:
    candidates: list[tuple[tuple[float, float], ...]] = []
    candidates.extend(_guide_detour_candidates(start, end, layer, corridor_hints))
    candidates.append(_manhattan_points(start, end))
    if start[0] != end[0] and start[1] != end[1]:
        candidates.append((start, (start[0], end[1]), end))
    route_clearance = max(width / 2.0 + spacing, 0.0)
    spacing_clearance = max(spacing, 0.0)
    obstacles = tuple(shape for shape in shapes if str(getattr(shape, "layer", "")) == layer and str(getattr(shape, "net", "")) not in {"", net})
    for obstacle in obstacles:
        bbox = _shape_bbox(obstacle)
        spacing_keepout = _expand_bbox(bbox, spacing_clearance)
        if spacing_keepout is None or not _route_points_hit_bbox(candidates[0], width, spacing_keepout):
            continue
        expanded = _expand_bbox(bbox, route_clearance)
        if expanded is None:
            continue
        y_low = expanded[1]
        y_high = expanded[3]
        x_low = expanded[0]
        x_high = expanded[2]
        candidates.extend(
            (
                (start, (start[0], y_low), (end[0], y_low), end),
                (start, (start[0], y_high), (end[0], y_high), end),
                (start, (x_low, start[1]), (x_low, end[1]), end),
                (start, (x_high, start[1]), (x_high, end[1]), end),
            )
        )
    nearby_halo = max(10.0 * spacing_clearance, 4.0 * width, 0.25)
    route_window = (
        min(start[0], end[0]) - nearby_halo,
        min(start[1], end[1]) - nearby_halo,
        max(start[0], end[0]) + nearby_halo,
        max(start[1], end[1]) + nearby_halo,
    )
    for obstacle in obstacles:
        bbox = _shape_bbox(obstacle)
        if not _bbox_strictly_overlaps(bbox, route_window):
            continue
        expanded = _expand_bbox(bbox, route_clearance)
        if expanded is None:
            continue
        x_positions = (expanded[0] - spacing_clearance, expanded[2] + spacing_clearance)
        y_positions = (expanded[1] - spacing_clearance, expanded[3] + spacing_clearance)
        for x_detour in x_positions:
            candidates.append((start, (x_detour, start[1]), (x_detour, end[1]), end))
        for y_detour in y_positions:
            candidates.append((start, (start[0], y_detour), (end[0], y_detour), end))
    return tuple(dict.fromkeys(candidates))


def _route_points_hit_foreign_shapes(
    points: tuple[tuple[float, float], ...],
    layer: str,
    width: float,
    spacing: float,
    net: str,
    shapes: tuple[object, ...],
) -> bool:
    spacing_clearance = max(spacing, 0.0)
    for shape in shapes:
        if str(getattr(shape, "layer", "")) != layer or str(getattr(shape, "net", "")) in {"", net}:
            continue
        expanded = _expand_bbox(_shape_bbox(shape), spacing_clearance)
        if expanded is not None and _route_points_hit_bbox(points, width, expanded):
            return True
    return False


def _route_points_hit_bbox(points: tuple[tuple[float, float], ...], width: float, bbox: tuple[float, float, float, float]) -> bool:
    for segment in _path_segment_bboxes(points, width):
        if _bbox_strictly_overlaps(segment, bbox):
            return True
    return False


def _bbox_strictly_overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _path_bbox_from_points(points: tuple[tuple[float, float], ...], width: float) -> tuple[float, float, float, float]:
    half = width / 2.0
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs) - half, min(ys) - half, max(xs) + half, max(ys) + half)


def _path_points_around_cutters(
    points: tuple[tuple[float, float], ...],
    width: float,
    cutters: tuple[tuple[float, float, float, float], ...],
    *,
    layer: str = "",
    corridor_hints: tuple[dict[str, object], ...] = (),
) -> tuple[tuple[float, float], ...]:
    if len(points) != 2:
        return ()
    start, end = points
    horizontal = abs(start[1] - end[1]) <= 1e-12
    vertical = abs(start[0] - end[0]) <= 1e-12
    if not horizontal and not vertical:
        return ()
    blocking = tuple(cutter for cutter in cutters if _route_points_hit_bbox(points, width, cutter))
    if not blocking:
        return ()
    blocked_bbox = _bbox_union_many(blocking)
    if blocked_bbox is None:
        return ()
    if horizontal:
        low_y = blocked_bbox[1] - width / 2.0
        high_y = blocked_bbox[3] + width / 2.0
        guided_candidates = list(_guide_detour_candidates(start, end, layer, corridor_hints))
        fallback_candidates = [
            (start, (start[0], low_y), (end[0], low_y), end),
            (start, (start[0], high_y), (end[0], high_y), end),
        ]
    else:
        low_x = blocked_bbox[0] - width / 2.0
        high_x = blocked_bbox[2] + width / 2.0
        guided_candidates = list(_guide_detour_candidates(start, end, layer, corridor_hints))
        fallback_candidates = [
            (start, (low_x, start[1]), (low_x, end[1]), end),
            (start, (high_x, start[1]), (high_x, end[1]), end),
        ]
    for candidate in tuple(dict.fromkeys(guided_candidates)):
        if not any(_route_points_hit_bbox(candidate, width, cutter) for cutter in cutters):
            return candidate
    for candidate in sorted(tuple(dict.fromkeys(fallback_candidates)), key=lambda candidate_points: _path_detour_cost(points, candidate_points)):
        if not any(_route_points_hit_bbox(candidate, width, cutter) for cutter in cutters):
            return candidate
    return ()


def _guide_detour_candidates(
    start: tuple[float, float],
    end: tuple[float, float],
    layer: str,
    corridor_hints: tuple[dict[str, object], ...],
) -> tuple[tuple[tuple[float, float], ...], ...]:
    candidates: list[tuple[tuple[float, float], ...]] = []
    for hint in corridor_hints:
        if str(dict(hint).get("layer", "")) != layer:
            continue
        bbox = _float_bbox(dict(hint).get("bbox_um"))
        if bbox is None:
            continue
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        if abs(start[1] - end[1]) <= 1e-12:
            if abs(cy - start[1]) > 1e-12:
                candidates.append((start, (start[0], cy), (end[0], cy), end))
            continue
        if abs(start[0] - end[0]) <= 1e-12:
            if abs(cx - start[0]) > 1e-12:
                candidates.append((start, (cx, start[1]), (cx, end[1]), end))
            continue
        candidates.append((start, (start[0], cy), (end[0], cy), end))
        candidates.append((start, (cx, start[1]), (cx, end[1]), end))
    return tuple(dict.fromkeys(candidates))


def _path_detour_cost(original: tuple[tuple[float, float], ...], candidate: tuple[tuple[float, float], ...]) -> float:
    original_len = _polyline_manhattan_length(original)
    candidate_len = _polyline_manhattan_length(candidate)
    return candidate_len - original_len


def _polyline_manhattan_length(points: tuple[tuple[float, float], ...]) -> float:
    return sum(abs(right[0] - left[0]) + abs(right[1] - left[1]) for left, right in zip(points, points[1:]))


def _path_segments_after_cutters(
    points: tuple[tuple[float, float], ...],
    width: float,
    cutters: tuple[tuple[float, float, float, float], ...],
) -> tuple[tuple[tuple[float, float], tuple[float, float]], ...]:
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for start, end in zip(points, points[1:]):
        segments.extend(_path_segment_after_cutters(start, end, width, cutters))
    return tuple(segments)


def _path_segment_after_cutters(
    start: tuple[float, float],
    end: tuple[float, float],
    width: float,
    cutters: tuple[tuple[float, float, float, float], ...],
) -> tuple[tuple[tuple[float, float], tuple[float, float]], ...]:
    horizontal = abs(start[1] - end[1]) <= 1e-12
    vertical = abs(start[0] - end[0]) <= 1e-12
    if not horizontal and not vertical:
        return ((start, end),)
    ranges: tuple[tuple[float, float], ...]
    if horizontal:
        y = start[1]
        x0, x1 = sorted((start[0], end[0]))
        ranges = tuple((max(x0, cutter[0]), min(x1, cutter[2])) for cutter in cutters if cutter[1] <= y + width / 2.0 and cutter[3] >= y - width / 2.0)
        kept = _subtract_1d_interval((x0, x1), ranges)
        if start[0] <= end[0]:
            return tuple(((lo, y), (hi, y)) for lo, hi in kept if hi > lo)
        return tuple(((hi, y), (lo, y)) for lo, hi in reversed(kept) if hi > lo)
    x = start[0]
    y0, y1 = sorted((start[1], end[1]))
    ranges = tuple((max(y0, cutter[1]), min(y1, cutter[3])) for cutter in cutters if cutter[0] <= x + width / 2.0 and cutter[2] >= x - width / 2.0)
    kept = _subtract_1d_interval((y0, y1), ranges)
    if start[1] <= end[1]:
        return tuple(((x, lo), (x, hi)) for lo, hi in kept if hi > lo)
    return tuple(((x, hi), (x, lo)) for lo, hi in reversed(kept) if hi > lo)


def _subtract_1d_interval(base: tuple[float, float], cutters: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
    ranges = (base,)
    for cut_lo, cut_hi in sorted((lo, hi) for lo, hi in cutters if hi > lo):
        next_ranges: list[tuple[float, float]] = []
        for lo, hi in ranges:
            if cut_hi <= lo or cut_lo >= hi:
                next_ranges.append((lo, hi))
                continue
            if cut_lo > lo:
                next_ranges.append((lo, min(cut_lo, hi)))
            if cut_hi < hi:
                next_ranges.append((max(cut_hi, lo), hi))
        ranges = tuple(next_ranges)
    return tuple((lo, hi) for lo, hi in ranges if hi > lo)


def _bbox_union_many(boxes: tuple[tuple[float, float, float, float], ...]) -> tuple[float, float, float, float] | None:
    if not boxes:
        return None
    return (min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes))


def _min_route_width(layer: str, pdk: PdkConfig | None) -> float:
    if pdk is None:
        return 0.2
    try:
        return pdk.rules.min_width_um(layer)
    except (AttributeError, KeyError, TypeError, ValueError):
        return 0.2


def _min_route_spacing(layer: str, pdk: PdkConfig | None) -> float:
    if pdk is None:
        return 0.0
    try:
        return pdk.rules.min_spacing_um(layer)
    except (AttributeError, KeyError, TypeError, ValueError):
        return 0.0


def _point_distance(left: tuple[float, float], right: tuple[float, float]) -> float:
    dx = right[0] - left[0]
    dy = right[1] - left[1]
    return (dx * dx + dy * dy) ** 0.5


def _find_local(parent: dict[int, int], idx: int) -> int:
    while parent[idx] != idx:
        parent[idx] = parent[parent[idx]]
        idx = parent[idx]
    return idx


def _union_local(parent: dict[int, int], left: int, right: int) -> None:
    root_left = _find_local(parent, left)
    root_right = _find_local(parent, right)
    if root_left != root_right:
        parent[root_right] = root_left


def _widen_bbox(bbox: tuple[float, float, float, float] | None, min_width: float) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    width = abs(x1 - x0)
    height = abs(y1 - y0)
    if min_width <= 0:
        return bbox
    if width <= height and width < min_width:
        delta = (min_width - width) / 2
        return (x0 - delta, y0, x1 + delta, y1)
    if height < min_width:
        delta = (min_width - height) / 2
        return (x0, y0 - delta, x1, y1 + delta)
    return bbox


def _grow_bbox_to_min_area(bbox: tuple[float, float, float, float] | None, min_area: float) -> tuple[float, float, float, float] | None:
    if bbox is None or min_area <= 0.0:
        return bbox
    area = rect_area(bbox)
    if area >= min_area or area <= 0.0:
        return bbox
    x0, y0, x1, y1 = bbox
    width = x1 - x0
    height = y1 - y0
    # Prefer a perpendicular grow for route-like islands.  In dense analog
    # access regions, long-axis extension can run into adjacent device access
    # wires; a width-only grow usually preserves the endpoint connectivity and
    # keeps the ECO local.  The path edit applicator preserves the original
    # centerline endpoints for this case.
    if width >= height:
        target_height = min_area / max(width, 1e-12)
        delta = (target_height - height) / 2.0
        return (x0, y0 - delta, x1, y1 + delta)
    target_width = min_area / max(height, 1e-12)
    delta = (target_width - width) / 2.0
    return (x0 - delta, y0, x1 + delta, y1)


def _suggest_drc_eco_for_issue(issue: DrcIssue, layer: str) -> DrcEcoSuggestion:
    rule = issue.rule.upper()
    text = f"{issue.rule} {issue.message}".lower()
    params: dict[str, object] = {}
    owner = "manual"
    if _is_latchup_or_tap_rule(rule, layer, text):
        action = "repair_tap_or_guard_spacing"
        owner = "tap_guard"
        priority = 88
    elif _is_pmos_marker_rule(rule, layer, text):
        action = "repair_pmos_recognition_or_pcell_option"
        owner = "pcell"
        priority = 82
    elif _is_area_or_antenna_rule(rule, text):
        action = "review_antenna_or_area_protection"
        owner = "manual"
        priority = 70
    elif _is_density_or_dummy_rule(rule, text):
        action = "plan_density_fill"
        owner = "fill"
        priority = 80
        params["target_layer"] = layer
    elif _is_enclosure_rule(rule, text):
        action = "grow_via_landing_or_enclosure" if _is_via_or_contact_rule(rule, layer, text) else "grow_enclosure"
        owner = "routing" if _is_via_or_contact_rule(rule, layer, text) else _owner_for_physical_layer(rule, layer, text)
        priority = 85
    elif _is_width_rule(rule, text):
        action = "widen_shape"
        owner = _owner_for_physical_layer(rule, layer, text)
        priority = 75
    elif _is_spacing_rule(rule, text):
        action = "move_or_reroute"
        owner = _owner_for_physical_layer(rule, layer, text)
        priority = 75
    elif _is_via_or_contact_rule(rule, layer, text):
        action = "replace_with_via_array"
        owner = "routing"
        priority = 75
    elif "matching" in text or "match" in text:
        action = "review_matching_symmetry"
        owner = "manual"
        priority = 65
    elif "esd" in text:
        action = "review_esd_topology"
        owner = "manual"
        priority = 60
    else:
        action = "manual_drc_review"
        priority = 10
    params["owner"] = owner
    return DrcEcoSuggestion(action, issue.rule, layer, issue.bbox, issue.message, priority, params, owner)


def _alias_issue_layer(issue: DrcIssue, aliases: Mapping[str, str]) -> DrcIssue:
    layer = aliases.get(issue.layer, issue.layer)
    if layer == issue.layer:
        return issue
    return DrcIssue(issue.rule, layer, issue.message, issue.bbox)


def _drc_rule_counts(issues: tuple[DrcIssue, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.rule] = counts.get(issue.rule, 0) + 1
    return counts


def _drc_comparison_next_actions(after_issues: tuple[DrcIssue, ...], new_rules: tuple[str, ...]) -> tuple[str, ...]:
    actions = []
    if new_rules:
        actions.append("review_new_drc_regressions")
    actions.extend(suggestion.action for suggestion in suggest_drc_ecos(after_issues))
    return tuple(dict.fromkeys(actions))


def _layout_proposal_for_geometry_edits(
    edits: tuple[GeometryEdit, ...],
    *,
    lib: str,
    cell: str,
    view: str,
    pdk: PdkConfig | None,
) -> object:
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, LayoutRect, snap_layout_plan_to_grid

    rects = tuple(
        LayoutRect(
            edit.layer,
            edit.target_bbox,
            edit.net,
            metadata={
                "action": edit.action,
                "reason": edit.reason,
                "source_bbox": edit.bbox,
            },
        )
        for edit in edits
        if edit.target_bbox is not None
    )
    plan = LayoutPlan(
        LayoutCellRef(lib, cell, view, "maskLayout"),
        rects=rects,
        metadata={"source": "plan_drc_ecos", "geometry_edit_count": len(edits)},
    )
    return snap_layout_plan_to_grid(plan, pdk) if pdk is not None else plan


def _lvs_issue_key(issue: LvsIssue) -> tuple[str, str]:
    return (issue.kind, issue.net)


def _lvs_issue_counts(issues: tuple[LvsIssue, ...]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for issue in issues:
        key = _lvs_issue_key(issue)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _lvs_comparison_next_actions(after_issues: tuple[LvsIssue, ...], new_issues: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    actions = []
    if new_issues:
        actions.append("review_new_lvs_regressions")
    actions.extend(str(repair["action"]) for repair in propose_lvs_repairs(list(after_issues)))
    return tuple(dict.fromkeys(actions))


def recommend_drc_repair_scope(
    issues: list[DrcIssue] | tuple[DrcIssue, ...],
    *,
    layout_plan: object | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> HierarchicalRepairScope:
    from analogskills.layout.ir import layout_plan_bbox

    issue_records = tuple(issues)
    issue_boxes = tuple(_float_bbox(issue.bbox) for issue in issue_records if _float_bbox(issue.bbox) is not None)
    issue_bbox = _bbox_union_many(issue_boxes) if issue_boxes else None
    regions = _hierarchy_regions_from_layout(layout_plan)
    if issue_bbox is not None and regions:
        containing = [region for region in regions if _bbox_contains(region.bbox, issue_bbox)]
        containing.sort(key=lambda region: (_bbox_area(region.bbox), region.name))
        if containing:
            region = containing[0]
            level = "leaf" if region.kind in {"leaf", "cell", "instance"} else "parent"
            region_devices = tuple(str(name) for name in region.metadata.get("devices", ()) if str(name))
            region_nets = tuple(str(name) for name in region.metadata.get("nets", ()) if str(name))
            affected_nets = tuple(
                dict.fromkeys(
                    net
                    for net in (
                        *region_nets,
                        *_issue_scope_nets(issue_records, layout_plan=layout_plan, issue_bbox=issue_bbox),
                    )
                    if net
                )
            )
            return _apply_system_scope_guidance(
                HierarchicalRepairScope(
                level=level,
                target=region.name,
                rationale=(
                    f"issue bbox is contained by hierarchy region {region.name}",
                    f"region kind={region.kind}",
                ),
                issue_bbox=issue_bbox,
                region_bbox=region.bbox,
                confidence=0.95,
                metadata={
                    "region_kind": region.kind,
                    "parent": region.parent,
                    "source": "hierarchy_regions",
                    "region_name": region.name,
                    "scope_regions": (region.name,),
                    "scope_devices": region_devices,
                    "scope_nets": region_nets,
                },
            ),
                affected_nets=affected_nets,
                hierarchy_context=hierarchy_context,
            )
    if issue_bbox is None:
        return _apply_system_scope_guidance(
            HierarchicalRepairScope(
            level="top",
            target=_layout_plan_name(layout_plan),
            rationale=("issue has no bbox; defaulting to current layout level",),
            issue_bbox=None,
            region_bbox=None,
            confidence=0.35,
            metadata={"source": "issue_without_bbox"},
        ),
            affected_nets=tuple(
                dict.fromkeys(
                    str(net)
                    for net in (
                        getattr(issue, "net", "")
                        for issue in issue_records
                    )
                    if str(net)
                )
            ),
            hierarchy_context=hierarchy_context,
        )
    return _apply_system_scope_guidance(
        HierarchicalRepairScope(
        level="top",
        target=_layout_plan_name(layout_plan),
        rationale=("issue bbox is not fully contained by any child region; likely upper-level routing/context",),
        issue_bbox=issue_bbox,
        region_bbox=layout_plan_bbox(layout_plan) if layout_plan is not None and hasattr(layout_plan, "cell") else None,
        confidence=0.8,
        metadata={"source": "bbox_outside_child_regions"},
    ),
        affected_nets=tuple(
            dict.fromkeys(
                str(net)
                for net in _issue_scope_nets(issue_records, layout_plan=layout_plan, issue_bbox=issue_bbox)
                if str(net)
            )
        ),
        hierarchy_context=hierarchy_context,
    )


def recommend_lvs_repair_scope(
    issue: LvsIssue,
    *,
    layout_plan: object | None = None,
    suggestion: LvsEcoSuggestion | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> HierarchicalRepairScope:
    owner = str(getattr(suggestion, "owner", ""))
    action = str(getattr(suggestion, "action", ""))
    evidence = tuple(str(item) for item in getattr(suggestion, "evidence", ()))
    net = str(issue.net or getattr(suggestion, "net", "") or "")
    if owner == "pin_label":
        return _apply_system_scope_guidance(
            HierarchicalRepairScope(
            level="top",
            target=_layout_plan_name(layout_plan),
            rationale=("pin/label LVS issue is usually fixed at the current integration level",),
            confidence=0.9,
            metadata={
                "owner": owner,
                "action": action,
                "net": net,
                "scope_nets": (net,) if net else (),
            },
        ),
            affected_nets=(net,) if net else (),
            hierarchy_context=hierarchy_context,
        )
    if owner == "floorplan":
        return _apply_system_scope_guidance(
            HierarchicalRepairScope(
            level="parent",
            target=_layout_plan_name(layout_plan),
            rationale=("floorplan-owned LVS issue implies the short/open is introduced by block placement or top-level corridor usage",),
            confidence=0.9,
            metadata={
                "owner": owner,
                "action": action,
                "net": net,
                "scope_nets": (net,) if net else (),
            },
        ),
            affected_nets=(net, *getattr(suggestion, "peer_nets", ())) if net else tuple(getattr(suggestion, "peer_nets", ())),
            hierarchy_context=hierarchy_context,
        )
    if owner == "routing":
        if any("path[" in item or "rect[" in item or "via[" in item for item in evidence):
            return _apply_system_scope_guidance(
                HierarchicalRepairScope(
                level="leaf",
                target=_layout_plan_name(layout_plan),
                rationale=("routing evidence points to explicit local geometry, so repair can start in the current cell",),
                confidence=0.8,
                metadata={
                    "owner": owner,
                    "action": action,
                    "net": net,
                    "evidence_count": len(evidence),
                    "scope_nets": (net,) if net else (),
                },
            ),
                affected_nets=tuple(dict.fromkeys(net_name for net_name in (net, *getattr(suggestion, "peer_nets", ())) if net_name)),
                hierarchy_context=hierarchy_context,
            )
        return _apply_system_scope_guidance(
            HierarchicalRepairScope(
            level="parent",
            target=_layout_plan_name(layout_plan),
            rationale=("routing-owned LVS issue lacks local geometry evidence and may originate at a higher interconnect level",),
            confidence=0.65,
            metadata={
                "owner": owner,
                "action": action,
                "net": net,
                "evidence_count": len(evidence),
                "scope_nets": (net,) if net else (),
            },
        ),
            affected_nets=tuple(dict.fromkeys(net_name for net_name in (net, *getattr(suggestion, "peer_nets", ())) if net_name)),
            hierarchy_context=hierarchy_context,
        )
    if owner in {"terminal_access", "model_pcell"}:
        return _apply_system_scope_guidance(
            HierarchicalRepairScope(
            level="leaf",
            target=_layout_plan_name(layout_plan),
            rationale=(f"{owner} issues are typically repaired in the leaf cell or device implementation",),
            confidence=0.85,
            metadata={
                "owner": owner,
                "action": action,
                "net": net,
                "scope_nets": (net,) if net else (),
            },
        ),
            affected_nets=(net,) if net else (),
            hierarchy_context=hierarchy_context,
        )
    return _apply_system_scope_guidance(
        HierarchicalRepairScope(
        level="cross_hierarchy",
        target=_layout_plan_name(layout_plan),
        rationale=("unable to identify a single safe repair level; manual or cross-hierarchy review is required",),
        confidence=0.4,
        metadata={
            "owner": owner,
            "action": action,
            "net": net,
            "scope_nets": (net,) if net else (),
        },
    ),
        affected_nets=tuple(dict.fromkeys(net_name for net_name in (net, *getattr(suggestion, "peer_nets", ())) if net_name)),
        hierarchy_context=hierarchy_context,
    )


def triage_drc_repair_scope(
    issues: list[DrcIssue] | tuple[DrcIssue, ...],
    *,
    layout_plan: object | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> HierarchicalRepairTriage:
    issue_records = tuple(issues)
    scope = recommend_drc_repair_scope(
        issue_records,
        layout_plan=layout_plan,
        hierarchy_context=hierarchy_context,
    )
    affected_nets = tuple(
        dict.fromkeys(
            str(net)
            for net in (
                *_issue_scope_nets(
                    issue_records,
                    layout_plan=layout_plan,
                    issue_bbox=scope.issue_bbox,
                ),
                *tuple(dict(scope.metadata).get("scope_nets", ()) or ()),
            )
            if str(net)
        )
    )
    blocking_system_kinds = _blocking_system_kinds(scope)
    summary = (
        f"kind=drc",
        f"scope_level={scope.level}",
        f"scope_target={scope.target}",
        f"issue_count={len(issue_records)}",
        f"affected_net_count={len(affected_nets)}",
        f"blocking_system_kind_count={len(blocking_system_kinds)}",
    )
    return HierarchicalRepairTriage(
        kind="drc",
        scope=scope,
        issue_count=len(issue_records),
        affected_nets=affected_nets,
        blocking_system_kinds=blocking_system_kinds,
        summary=summary,
        provenance={
            "issue_rules": tuple(str(issue.rule) for issue in issue_records if str(issue.rule)),
            "issue_layers": tuple(str(issue.layer) for issue in issue_records if str(issue.layer)),
        },
    )


def triage_lvs_repair_scope(
    issue: LvsIssue,
    *,
    layout_plan: object | None = None,
    suggestion: LvsEcoSuggestion | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> HierarchicalRepairTriage:
    scope = recommend_lvs_repair_scope(
        issue,
        layout_plan=layout_plan,
        suggestion=suggestion,
        hierarchy_context=hierarchy_context,
    )
    affected_nets = tuple(
        dict.fromkeys(
            str(net)
            for net in (
                issue.net,
                *(getattr(suggestion, "peer_nets", ()) if suggestion is not None else ()),
                *tuple(dict(scope.metadata).get("scope_nets", ()) or ()),
            )
            if str(net)
        )
    )
    blocking_system_kinds = _blocking_system_kinds(scope)
    summary = (
        f"kind=lvs",
        f"scope_level={scope.level}",
        f"scope_target={scope.target}",
        f"issue_kind={issue.kind}",
        f"affected_net_count={len(affected_nets)}",
        f"blocking_system_kind_count={len(blocking_system_kinds)}",
    )
    return HierarchicalRepairTriage(
        kind="lvs",
        scope=scope,
        issue_count=1,
        affected_nets=affected_nets,
        blocking_system_kinds=blocking_system_kinds,
        summary=summary,
        provenance={
            "issue_kind": str(issue.kind),
            "issue_net": str(issue.net),
            "owner": str(getattr(suggestion, "owner", "") if suggestion is not None else ""),
            "action": str(getattr(suggestion, "action", "") if suggestion is not None else ""),
        },
    )


def _make_drc_repair_candidate(
    issues: tuple[DrcIssue, ...],
    plan_kind: str,
    plan: object,
    *,
    layout_plan: object | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> DrcRepairCandidate:
    passed, issues_after, score = _score_drc_repair_plan(plan)
    repair_scope = recommend_drc_repair_scope(issues, layout_plan=layout_plan, hierarchy_context=hierarchy_context)
    plan = _stamp_plan_scope_metadata(plan, repair_scope)
    score += _hierarchy_repair_score_delta(
        repair_scope=repair_scope,
        affected_nets=_repair_plan_affected_nets(plan),
        hierarchy_context=hierarchy_context,
    )
    return DrcRepairCandidate(
        issues,
        plan_kind,
        plan,
        score,
        passed,
        issues_after,
        repair_scope,
    )


def _build_drc_geometry_patch_fallback_candidate(
    issues: tuple[DrcIssue, ...],
    *,
    eco_plan: DrcEcoPlan,
    layout_plan: object,
    pdk: PdkConfig | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> DrcRepairCandidate | None:
    patch = eco_plan.layout_proposal
    edits = tuple(eco_plan.geometry_edits or ())
    if patch is None or not edits or not hasattr(patch, "cell"):
        return None
    from analogskills.eda.oa import layout_plan_to_oa_write_plan
    from analogskills.layout import analyze_plan_physical_connectivity, analyze_interconnect_plan, merge_layout_plans

    physical_report = analyze_plan_physical_connectivity(patch, pdk=pdk)
    interconnect_report = analyze_interconnect_plan(patch, pdk=pdk)
    merged_physical_report = None
    merged_interconnect_report = None
    if hasattr(layout_plan, "cell"):
        merged = merge_layout_plans(layout_plan, patch, cell=getattr(layout_plan, "cell", None), grid=pdk)
        merged_physical_report = analyze_plan_physical_connectivity(merged, include_opens=True, pdk=pdk)
        merged_interconnect_report = analyze_interconnect_plan(merged, pdk=pdk, include_open_checks=True)
    plan = LocalizedDrcPatchPlan(
        edits,
        patch,
        layout_plan_to_oa_write_plan(patch),
        physical_report,
        interconnect_report,
        merged_physical_report,
        merged_interconnect_report,
    )
    return _make_drc_repair_candidate(
        issues,
        "geometry_patch_fallback",
        plan,
        layout_plan=layout_plan,
        hierarchy_context=hierarchy_context,
    )


def _repair_plan_edit_count(plan: object) -> int:
    edits = getattr(plan, "edits", ())
    return len(tuple(edits))


def _repair_scope_to_dict(scope: HierarchicalRepairScope | None) -> dict[str, object]:
    if scope is None:
        return {}
    metadata = dict(scope.metadata)
    return {
        "level": scope.level,
        "target": scope.target,
        "rationale": tuple(scope.rationale),
        "issue_bbox": scope.issue_bbox,
        "region_bbox": scope.region_bbox,
        "confidence": float(scope.confidence),
        "metadata": metadata,
        "system_recommended_level": str(metadata.get("system_recommended_level", "")),
        "escalation_required": bool(metadata.get("escalation_required", False)),
    }


def _score_drc_repair_plan(plan: object) -> tuple[bool, tuple[str, ...], float]:
    if isinstance(plan, LocalizedDrcPatchPlan):
        physical = dict(plan.merged_physical_report or plan.physical_report)
        interconnect = dict(plan.merged_interconnect_report or plan.interconnect_report or {})
        merged_issues = tuple(str(issue) for issue in (*tuple(physical.get("issues", ())), *tuple(interconnect.get("issues", ()))))
        passed = bool(physical.get("passed", False)) and (not interconnect or bool(interconnect.get("passed", False)))
        score = (0.0 if passed else 1000.0) + 10.0 * len(merged_issues) + float(len(plan.edits))
        return passed, merged_issues, score
    if isinstance(plan, DrcReplacementPlan):
        physical = dict(plan.physical_report)
        interconnect = dict(plan.interconnect_report or {})
        merged_issues = tuple(str(issue) for issue in (*tuple(physical.get("issues", ())), *tuple(interconnect.get("issues", ()))))
        passed = bool(physical.get("passed", False)) and (not interconnect or bool(interconnect.get("passed", False))) and not plan.after_spacing_violations
        score = (0.0 if passed else 1000.0) + 100.0 * float(len(plan.after_spacing_violations)) + 10.0 * len(merged_issues) + float(len(plan.edits))
        return passed, merged_issues, score
    return False, (), 1000.0


def _make_lvs_repair_candidate(
    item: LvsRepairItem,
    plan_kind: str,
    plan: object,
    *,
    layout_plan: object | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> LvsRepairCandidate:
    passed, issues, score = _score_lvs_repair_plan(plan)
    repair_scope = recommend_lvs_repair_scope(item.issue, layout_plan=layout_plan, suggestion=item.suggestion, hierarchy_context=hierarchy_context)
    plan = _stamp_plan_scope_metadata(plan, repair_scope)
    score += _hierarchy_repair_score_delta(
        repair_scope=repair_scope,
        affected_nets=_repair_plan_affected_nets(plan, default_nets=(item.issue.net, *item.suggestion.peer_nets)),
        hierarchy_context=hierarchy_context,
    )
    return LvsRepairCandidate(
        item,
        plan_kind,
        plan,
        score,
        passed,
        issues,
        repair_scope,
    )


def _score_lvs_repair_plan(plan: object) -> tuple[bool, tuple[str, ...], float]:
    if isinstance(plan, LocalizedDrcPatchPlan):
        physical = dict(plan.merged_physical_report or plan.physical_report)
        interconnect = dict(plan.merged_interconnect_report or plan.interconnect_report or {})
        issues = tuple(str(issue) for issue in (*tuple(physical.get("issues", ())), *tuple(interconnect.get("issues", ()))))
        passed = bool(physical.get("passed", False)) and (not interconnect or bool(interconnect.get("passed", False)))
        score = (0.0 if passed else 1000.0) + 10.0 * len(issues) + float(len(plan.edits))
        return passed, issues, score
    if isinstance(plan, LvsShortReplacementPlan):
        physical = dict(plan.physical_report)
        interconnect = dict(plan.interconnect_report or {})
        issues = tuple(str(issue) for issue in (*tuple(physical.get("issues", ())), *tuple(interconnect.get("issues", ()))))
        passed = bool(physical.get("passed", False)) and (not interconnect or bool(interconnect.get("passed", False))) and not plan.after_shorts
        score = (0.0 if passed else 1000.0) + 100.0 * float(len(plan.after_shorts)) + 10.0 * len(issues) + float(len(plan.edits))
        return passed, issues, score
    if isinstance(plan, LvsManualRepairHandoffPlan):
        issues = (plan.issue_message,) if plan.issue_message else ()
        return False, issues, 5000.0
    return False, (), 1000.0


def _repair_plan_affected_nets(plan: object, default_nets: tuple[str, ...] = ()) -> tuple[str, ...]:
    nets: list[str] = [str(net) for net in default_nets if str(net)]
    for attr in ("layout_patch", "replacement_layout"):
        layout = getattr(plan, attr, None)
        if layout is None:
            continue
        for item in tuple(getattr(layout, "nets", ()) or ()):
            if str(item):
                nets.append(str(item))
        for collection in ("rects", "paths", "vias", "pins", "labels"):
            for shape in tuple(getattr(layout, collection, ()) or ()):
                net = str(getattr(shape, "net", "") or "")
                if net:
                    nets.append(net)
    return tuple(dict.fromkeys(nets))


def _stamp_plan_scope_metadata(plan: object, repair_scope: HierarchicalRepairScope | None) -> object:
    if repair_scope is None:
        return plan
    scope_metadata = _repair_scope_plan_metadata(repair_scope)
    if not scope_metadata:
        return plan
    if isinstance(plan, LocalizedDrcPatchPlan):
        return replace(
            plan,
            layout_patch=_stamp_layout_scope_metadata(plan.layout_patch, scope_metadata),
        )
    if isinstance(plan, DrcReplacementPlan):
        return replace(
            plan,
            replacement_layout=_stamp_layout_scope_metadata(plan.replacement_layout, scope_metadata),
        )
    if isinstance(plan, LvsShortReplacementPlan):
        return replace(
            plan,
            replacement_layout=_stamp_layout_scope_metadata(plan.replacement_layout, scope_metadata),
        )
    if isinstance(plan, LvsManualRepairHandoffPlan):
        return replace(
            plan,
            metadata={**dict(plan.metadata), **scope_metadata},
        )
    return plan


def _build_lvs_model_pcell_handoff_plan(
    item: LvsRepairItem,
    *,
    layout_plan: object | None,
) -> LvsManualRepairHandoffPlan:
    suggestion = item.suggestion
    scope_nets = tuple(
        dict.fromkeys(str(net) for net in (suggestion.net, *suggestion.peer_nets) if str(net))
    )
    metadata: dict[str, object] = {
        "manual_dispatch_required": True,
        "handoff_kind": "model_pcell_parameter_review",
        "target_cell": _layout_plan_name(layout_plan),
        "scope_nets": scope_nets,
        "device_hint": str(suggestion.params.get("device_hint", "")),
    }
    device_hint = str(suggestion.params.get("device_hint", ""))
    if device_hint:
        metadata["scope_devices"] = (device_hint,)
    return LvsManualRepairHandoffPlan(
        action=suggestion.action,
        owner=suggestion.owner,
        issue_message=item.issue.message,
        net=str(item.issue.net or suggestion.net or ""),
        peer_nets=tuple(str(net) for net in suggestion.peer_nets if str(net)),
        evidence=tuple(str(entry) for entry in suggestion.evidence if str(entry)),
        params=dict(suggestion.params),
        edits=tuple(
            GeometryEdit(
                action="manual_model_pcell_review",
                layer="",
                bbox=None,
                reason=item.issue.message,
                net=net,
            )
            for net in scope_nets
        ),
        metadata=metadata,
    )


def _repair_scope_plan_metadata(repair_scope: HierarchicalRepairScope) -> dict[str, object]:
    metadata = dict(repair_scope.metadata)
    plan_metadata: dict[str, object] = {
        "scope_level": repair_scope.level,
        "scope_target": repair_scope.target,
    }
    if repair_scope.issue_bbox is not None:
        plan_metadata["issue_bbox"] = repair_scope.issue_bbox
    if repair_scope.region_bbox is not None:
        plan_metadata["region_bbox"] = repair_scope.region_bbox
    for key in (
        "scope_regions",
        "scope_devices",
        "scope_nets",
        "avoid_devices",
        "avoid_nets",
        "region_name",
        "region_kind",
        "parent",
        "source",
        "system_repair_guidance",
        "system_repair_levels",
        "system_recommended_level",
        "escalation_required",
    ):
        value = metadata.get(key)
        if value:
            plan_metadata[key] = value
    return plan_metadata


def _issue_scope_nets(
    issues: tuple[DrcIssue, ...],
    *,
    layout_plan: object | None,
    issue_bbox: tuple[float, float, float, float] | None,
) -> tuple[str, ...]:
    nets: list[str] = []
    for issue in issues:
        net = str(getattr(issue, "net", "") or "")
        if net:
            nets.append(net)
    if issue_bbox is None or layout_plan is None:
        return tuple(dict.fromkeys(nets))
    for shape in _layout_plan_shapes(layout_plan):
        if not shape.net:
            continue
        overlap = rect_intersection(shape.bbox, issue_bbox)
        if overlap is None:
            continue
        nets.append(str(shape.net))
    return tuple(dict.fromkeys(str(net) for net in nets if str(net)))


def _apply_system_scope_guidance(
    scope: HierarchicalRepairScope,
    *,
    affected_nets: tuple[str, ...],
    hierarchy_context: Mapping[str, object] | None,
) -> HierarchicalRepairScope:
    if hierarchy_context is None:
        return scope
    system_contract = dict(hierarchy_context.get("hierarchical_system_contract", {}) or {})
    partition_bundle = dict(hierarchy_context.get("hierarchical_partition_implementation_bundle", {}) or {})
    system_regression = dict(hierarchy_context.get("hierarchical_system_regression", {}) or {})
    if not system_contract and not partition_bundle and not system_regression:
        return scope
    guidance = (
        *_partition_bundle_scope_guidance_for_nets(affected_nets, partition_bundle),
        *_system_scope_guidance_for_nets(affected_nets, system_contract),
        *_system_regression_scope_guidance_for_nets(affected_nets, system_regression),
    )
    if not guidance:
        return scope
    current_rank = _repair_scope_level_rank(scope.level)
    recommended_level = _system_repair_guidance_level(guidance)
    recommended_rank = _repair_scope_level_rank(recommended_level)
    escalation_required = recommended_rank > current_rank
    metadata = dict(scope.metadata)
    existing_guidance = tuple(item for item in tuple(metadata.get("system_repair_guidance", ()) or ()) if isinstance(item, Mapping))
    merged_guidance = tuple(dict(item) for item in (*existing_guidance, *guidance))
    metadata["system_repair_guidance"] = merged_guidance
    metadata["system_repair_levels"] = tuple(dict.fromkeys(str(dict(item).get("recommended_level", "")) for item in merged_guidance if str(dict(item).get("recommended_level", ""))))
    metadata["system_recommended_level"] = recommended_level
    metadata["escalation_required"] = escalation_required
    rationale = tuple(scope.rationale)
    if escalation_required:
        rationale = (
            *rationale,
            f"system contract requires repair scope escalation to {recommended_level}",
        )
        return replace(scope, level=recommended_level, rationale=rationale, metadata=metadata)
    rationale = (
        *rationale,
        f"system contract confirms repair scope at or above {recommended_level}",
    )
    return replace(scope, rationale=rationale, metadata=metadata)


def _stamp_layout_scope_metadata(layout: object, scope_metadata: Mapping[str, object]) -> object:
    current = dict(getattr(layout, "metadata", {}) or {})
    merged = {**scope_metadata, **current}
    return replace(layout, metadata=merged)


def _hierarchy_repair_score_delta(
    *,
    repair_scope: HierarchicalRepairScope | None,
    affected_nets: tuple[str, ...],
    hierarchy_context: Mapping[str, object] | None,
) -> float:
    if hierarchy_context is None:
        return 0.0
    delta = 0.0
    partition_bundle = dict(hierarchy_context.get("hierarchical_partition_implementation_bundle", {}) or {})
    partition_rows = tuple(
        dict(item)
        for item in tuple(partition_bundle.get("partitions", ()) or ())
        if isinstance(item, Mapping)
    )
    keep_stable = {
        str(item.get("name", ""))
        for item in partition_rows
        if str(item.get("name", "")) and bool(item.get("keep_stable", False))
    } | {str(name) for name in hierarchy_context.get("keep_stable_partitions", ()) if str(name)}
    changed = {
        str(item.get("name", ""))
        for item in partition_rows
        if str(item.get("name", "")) and bool(item.get("retarget_changed", False))
    } | {str(name) for name in hierarchy_context.get("retarget_changed_partitions", ()) if str(name)}
    critical_nets = {
        str(net)
        for item in partition_rows
        for net in tuple(item.get("critical_nets", ()) or ())
        if str(net)
    } | {str(net) for net in hierarchy_context.get("critical_nets", ()) if str(net)}
    removed_feedback = {
        str(net)
        for item in partition_rows
        for net in tuple(item.get("feedback_nets", ()) or ())
        if str(net) and bool(item.get("restore_feedback_loop", False))
    } | {str(net) for net in hierarchy_context.get("removed_feedback_loops", ()) if str(net)}
    scope_target = str(getattr(repair_scope, "target", "") or "")
    if scope_target and scope_target in keep_stable:
        delta += 40.0
    if scope_target and scope_target in changed:
        delta -= 25.0
    affected = {str(net) for net in affected_nets if str(net)}
    if affected & critical_nets:
        delta -= 20.0
    if affected & removed_feedback:
        delta -= 25.0
    system_contract = dict(hierarchy_context.get("hierarchical_system_contract", {}) or {})
    if system_contract and repair_scope is not None:
        current_rank = _repair_scope_level_rank(str(getattr(repair_scope, "level", "") or ""))
        recommended_rank = -1
        if any(
            bool(dict(item).get("restore_required", False))
            and affected & {str(net) for net in tuple(dict(item).get("nets", ()) or ()) if str(net)}
            for item in tuple(system_contract.get("bus_contracts", ()) or ())
            if isinstance(item, Mapping)
        ):
            recommended_rank = max(recommended_rank, _repair_scope_level_rank("parent"))
        if any(
            bool(dict(item).get("restore_required", False))
            and str(dict(item).get("net", "")) in affected
            for item in tuple(system_contract.get("feedback_contracts", ()) or ())
            if isinstance(item, Mapping)
        ):
            recommended_rank = max(recommended_rank, _repair_scope_level_rank("top"))
        if any(
            bool(dict(item).get("preserve_integrity", False))
            and str(dict(item).get("net", "")) in affected
            for item in tuple(system_contract.get("reference_paths", ()) or ())
            if isinstance(item, Mapping)
        ):
            recommended_rank = max(recommended_rank, _repair_scope_level_rank("parent"))
        if recommended_rank > current_rank:
            delta += 60.0 * float(recommended_rank - current_rank)
    return delta


def _repair_scope_level_rank(level: str) -> int:
    return {
        "leaf": 0,
        "leaf_or_parent": 1,
        "parent": 1,
        "top": 2,
        "cross_hierarchy": 3,
    }.get(str(level), -1)


def _system_scope_guidance_for_nets(
    affected_nets: tuple[str, ...],
    system_contract: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    affected = {str(net) for net in affected_nets if str(net)}
    if not affected:
        return ()
    guidance: list[dict[str, object]] = []
    for item in tuple(system_contract.get("bus_contracts", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        nets = tuple(str(net) for net in tuple(dict(item).get("nets", ()) or ()) if str(net))
        if not nets or not affected & set(nets):
            continue
        if not bool(dict(item).get("restore_required", False)):
            continue
        guidance.append(
            {
                "kind": "bus_corridor_restore",
                "recommended_level": "parent",
                "nets": nets,
            }
        )
    for item in tuple(system_contract.get("feedback_contracts", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        net = str(dict(item).get("net", ""))
        if not net or net not in affected:
            continue
        if not bool(dict(item).get("restore_required", False)):
            continue
        guidance.append(
            {
                "kind": "feedback_path_restore",
                "recommended_level": "top",
                "net": net,
            }
        )
    for item in tuple(system_contract.get("reference_paths", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        net = str(dict(item).get("net", ""))
        if not net or net not in affected:
            continue
        if not bool(dict(item).get("preserve_integrity", False)):
            continue
        guidance.append(
            {
                "kind": "reference_integrity_protect",
                "recommended_level": "leaf_or_parent",
                "net": net,
            }
        )
    return tuple(guidance)


def _partition_bundle_scope_guidance_for_nets(
    affected_nets: tuple[str, ...],
    partition_bundle: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    affected = {str(net) for net in affected_nets if str(net)}
    if not affected:
        return ()
    guidance: list[dict[str, object]] = []
    for item in tuple(partition_bundle.get("partitions", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        partition = dict(item)
        name = str(partition.get("name", ""))
        if not name:
            continue
        bundle_nets = {
            str(net)
            for net in (
                tuple(partition.get("critical_nets", ()) or ())
                + tuple(partition.get("reference_nets", ()) or ())
                + tuple(partition.get("feedback_nets", ()) or ())
                + tuple(partition.get("bus_nets", ()) or ())
                + tuple(partition.get("required_external_nets", ()) or ())
            )
            if str(net)
        }
        if not affected & bundle_nets:
            continue
        if bool(partition.get("restore_feedback_loop", False)) and affected & {
            str(net) for net in tuple(partition.get("feedback_nets", ()) or ()) if str(net)
        }:
            guidance.append(
                {
                    "kind": "partition_feedback_restore",
                    "recommended_level": "top",
                    "partition": name,
                }
            )
        if bool(partition.get("restore_bus_corridor", False)) and affected & {
            str(net) for net in tuple(partition.get("bus_nets", ()) or ()) if str(net)
        }:
            guidance.append(
                {
                    "kind": "partition_bus_restore",
                    "recommended_level": "parent",
                    "partition": name,
                }
            )
        if affected & {str(net) for net in tuple(partition.get("reference_nets", ()) or ()) if str(net)}:
            guidance.append(
                {
                    "kind": "partition_reference_integrity",
                    "recommended_level": "leaf_or_parent",
                    "partition": name,
                }
            )
    return tuple(guidance)


def _system_regression_scope_guidance_for_nets(
    affected_nets: tuple[str, ...],
    system_regression: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    affected = {str(net) for net in affected_nets if str(net)}
    if not affected:
        return ()
    guidance: list[dict[str, object]] = []
    for item in tuple(system_regression.get("contract_checks", ()) or ()):
        if not isinstance(item, Mapping) or bool(item.get("passed", False)):
            continue
        kind = str(item.get("kind", ""))
        name = str(item.get("name", ""))
        nets = tuple(str(net) for net in tuple(item.get("nets", ()) or ()) if str(net))
        net_match = bool(name and name in affected) or bool(set(nets) & affected)
        if not net_match:
            continue
        recommended_level = {
            "bus_corridor": "parent",
            "reference_path": "leaf_or_parent",
            "feedback_loop": "top",
            "timing_chain": "top",
        }.get(kind, "")
        if not recommended_level:
            continue
        guidance.append(
            {
                "kind": f"system_regression_{kind}",
                "recommended_level": recommended_level,
                "net": name,
                "reason": str(item.get("reason", "")),
            }
        )
    return tuple(guidance)


def _blocking_system_kinds(scope: HierarchicalRepairScope) -> tuple[str, ...]:
    metadata = dict(scope.metadata)
    return tuple(
        dict.fromkeys(
            str(dict(item).get("kind", ""))
            for item in tuple(metadata.get("system_repair_guidance", ()) or ())
            if isinstance(item, Mapping) and str(dict(item).get("kind", ""))
        )
    )


def _system_repair_guidance_level(guidance: tuple[Mapping[str, object], ...] | tuple[dict[str, object], ...]) -> str:
    best_level = ""
    best_rank = -1
    for item in guidance:
        level = str(dict(item).get("recommended_level", "") or "")
        rank = _repair_scope_level_rank(level)
        if rank > best_rank:
            best_level = level
            best_rank = rank
    return best_level


def _layout_plan_name(layout_plan: object | None) -> str:
    if layout_plan is None:
        return ""
    cell = getattr(layout_plan, "cell", None)
    if cell is not None:
        return str(getattr(cell, "cell", ""))
    cellview = getattr(layout_plan, "cellview", None)
    if cellview is not None:
        return str(getattr(cellview, "cell", ""))
    return ""


def _hierarchy_regions_from_layout(layout_plan: object | None) -> tuple[HierarchicalRepairRegion, ...]:
    metadata = dict(getattr(layout_plan, "metadata", {}) or {}) if layout_plan is not None else {}
    raw_regions = tuple(metadata.get("hierarchy_regions", ()))
    regions: list[HierarchicalRepairRegion] = []
    for raw in raw_regions:
        if isinstance(raw, HierarchicalRepairRegion):
            regions.append(raw)
            continue
        if not isinstance(raw, Mapping):
            continue
        bbox = _float_bbox(raw.get("bbox"))
        name = str(raw.get("name", ""))
        if bbox is None or not name:
            continue
        regions.append(
            HierarchicalRepairRegion(
                name=name,
                kind=str(raw.get("kind", "leaf")),
                bbox=bbox,
                parent=str(raw.get("parent", "")),
                metadata={str(key): value for key, value in dict(raw.get("metadata", {})).items()},
            )
        )
    return tuple(regions)


def _bbox_contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float], *, tol: float = 1e-12) -> bool:
    return outer[0] - tol <= inner[0] and outer[1] - tol <= inner[1] and outer[2] + tol >= inner[2] and outer[3] + tol >= inner[3]


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _suggest_lvs_eco_for_issue(
    issue: LvsIssue,
    *,
    layout_plan: object | None,
    floorplan: object | None,
    pin_label_report: Mapping[str, object] | None,
) -> LvsEcoSuggestion:
    net = issue.net or _first_net_token(issue.message)
    peer_nets = _lvs_peer_nets(issue.message, net)
    evidence: list[str] = []
    params: dict[str, object] = {}

    if issue.kind == "short" or "short" in issue.message.lower():
        corridor = _corridor_short_evidence(floorplan, net, peer_nets)
        if corridor:
            evidence.extend(corridor)
            params["corridor"] = _preferred_corridor_from_evidence(corridor)
            return LvsEcoSuggestion("move_block_or_split_supply_body_channel", "floorplan", net, peer_nets, issue.message, 95, tuple(evidence), params)
        short_shapes = _layout_short_evidence(layout_plan, net, peer_nets)
        if short_shapes:
            evidence.extend(short_shapes)
            return LvsEcoSuggestion("reroute_or_change_layer", "routing", net, peer_nets, issue.message, 90, tuple(evidence), params)
        label_evidence = _pin_label_evidence(pin_label_report, (net, *peer_nets))
        if label_evidence:
            evidence.extend(label_evidence)
            return LvsEcoSuggestion("move_pin_label", "pin_label", net, peer_nets, issue.message, 85, tuple(evidence), params)
        return LvsEcoSuggestion("split_or_reroute_short", "routing", net, peer_nets, issue.message, 70, (), params)

    if issue.kind == "open" or "open" in issue.message.lower() or "missing" in issue.message.lower():
        label_evidence = _pin_label_evidence(pin_label_report, (net,))
        if label_evidence:
            evidence.extend(label_evidence)
            return LvsEcoSuggestion("move_or_add_pin_label", "pin_label", net, peer_nets, issue.message, 85, tuple(evidence), params)
        route_evidence = _layout_net_presence_evidence(layout_plan, net)
        if route_evidence:
            evidence.extend(route_evidence)
            return LvsEcoSuggestion("route_missing_connection", "routing", net, peer_nets, issue.message, 80, tuple(evidence), params)
        return LvsEcoSuggestion("recalibrate_terminal_or_add_route", "terminal_access", net, peer_nets, issue.message, 75, (), params)

    if issue.kind == "mismatch":
        return _suggest_mismatch_lvs_eco(issue, net, peer_nets)
    return LvsEcoSuggestion("manual_lvs_review", "manual", net, peer_nets, issue.message, 10, (), params)


def _suggest_mismatch_lvs_eco(issue: LvsIssue, net: str, peer_nets: tuple[str, ...]) -> LvsEcoSuggestion:
    message = issue.message
    text = message.lower()
    params = {"device_hint": _device_hint(message)}
    if _mentions_any(text, ("split", "merge", "merged", "parallel", "finger", "fingers", "nf", "simm", " m ")):
        return LvsEcoSuggestion(
            "review_mos_merge_parameters",
            "model_pcell",
            net,
            peer_nets,
            message,
            82,
            ("check nf/fingers/simM/M against Calibre MOS merge rules",),
            params,
        )
    if _mentions_any(text, ("resistor", "res ", " r_", "rnod", "rnodl", "terminal order")):
        return LvsEcoSuggestion(
            "review_resistor_model_and_terminal_order",
            "model_pcell",
            net,
            peer_nets,
            message,
            78,
            ("check resistor model_map, W/L/value, and PLUS/MINUS terminal order",),
            params,
        )
    if _mentions_any(text, ("capacitor", "cap ", " c_", "nmoscap", "moscap", "varactor")):
        return LvsEcoSuggestion(
            "review_capacitor_representation",
            "model_pcell",
            net,
            peer_nets,
            message,
            78,
            ("check whether capacitor must netlist as primitive, MOS capacitor, varactor, or subckt",),
            params,
        )
    if _mentions_any(text, ("bjt", "npn", "pnp", "area", "emitter")):
        return LvsEcoSuggestion(
            "review_bjt_model_and_area_factor",
            "model_pcell",
            net,
            peer_nets,
            message,
            76,
            ("check BJT model_map and emitter area factor expected by Calibre",),
            params,
        )
    return LvsEcoSuggestion("review_model_or_pcell_parameters", "model_pcell", net, peer_nets, message, 60, (), params)


def _mentions_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _device_hint(message: str) -> str:
    tokens = _net_tokens(message)
    for token in tokens:
        upper = token.upper()
        if upper.startswith(("M", "R", "C", "Q", "X")) and upper not in {"MISMATCH", "MODEL"}:
            return token
    return ""


def _lvs_peer_nets(message: str, primary_net: str) -> tuple[str, ...]:
    ignored = {"SHORT", "OPEN", "NET", "SOURCE", "LAYOUT", "MISSING", "CONNECTION", "WARNING", "LVS"}
    peers: list[str] = []
    for token in _net_tokens(message):
        upper = token.upper()
        if upper in ignored or token == primary_net:
            continue
        peers.append(token)
    return tuple(dict.fromkeys(peers))


def _first_net_token(message: str) -> str:
    for token in _net_tokens(message):
        if token.upper() not in {"SHORT", "OPEN", "NET", "SOURCE", "LAYOUT", "MISSING", "CONNECTION"}:
            return token
    return ""


def _net_tokens(message: str) -> tuple[str, ...]:
    import re

    return tuple(re.findall(r"[A-Za-z_][A-Za-z0-9_.$:-]*", message))


def _corridor_short_evidence(floorplan: object | None, net: str, peer_nets: tuple[str, ...]) -> tuple[str, ...]:
    if floorplan is None:
        return ()
    evidence: list[str] = []
    short_nets = {net, *peer_nets}
    for corridor in tuple(getattr(floorplan, "forbidden_channels", ())):
        corridor_nets = set(getattr(corridor, "nets", ()))
        forbidden = set(getattr(corridor, "forbidden_nets", ())) - set(getattr(corridor, "waiver_nets", ()))
        if corridor_nets & short_nets and forbidden & short_nets:
            evidence.append(f"{getattr(corridor, 'name', '<corridor>')}: forbidden short nets {tuple(sorted(short_nets))}")
    return tuple(evidence)


def _preferred_corridor_from_evidence(evidence: tuple[str, ...]) -> str:
    names = tuple(item.split(":", 1)[0] for item in evidence)
    for name in names:
        if "GROUND" in name or "TAP" in name:
            return name
    return names[0] if names else ""


def _layout_short_evidence(layout_plan: object | None, net: str, peer_nets: tuple[str, ...]) -> tuple[str, ...]:
    if layout_plan is None:
        return ()
    target_nets = {net, *peer_nets}
    shapes = _layout_plan_shapes(layout_plan)
    evidence: list[str] = []
    for idx, left in enumerate(shapes):
        for right in shapes[idx + 1 :]:
            if left.layer != right.layer or left.net == right.net:
                continue
            if {left.net, right.net} <= target_nets and rect_intersection(left.bbox, right.bbox) is not None:
                evidence.append(f"{left.layer}:{left.net}-{right.net} overlap {rect_intersection(left.bbox, right.bbox)}")
    return tuple(evidence)


def _layout_net_presence_evidence(layout_plan: object | None, net: str) -> tuple[str, ...]:
    if layout_plan is None or not net:
        return ()
    shapes = [shape for shape in _layout_plan_shapes(layout_plan) if shape.net == net]
    pins = [pin for pin in tuple(getattr(layout_plan, "pins", ())) if getattr(pin, "net", "") == net]
    if shapes and not pins:
        return (f"net {net} has drawing geometry but no top-level pin",)
    if pins and not shapes:
        return (f"net {net} has pin but no drawing geometry",)
    if shapes:
        return (f"net {net} has {len(shapes)} drawing shape(s); review missing terminal branch",)
    return ()


def _pin_label_evidence(report: Mapping[str, object] | None, nets: tuple[str, ...]) -> tuple[str, ...]:
    if report is None:
        return ()
    wanted = tuple(net for net in nets if net)
    evidence = []
    for issue in tuple(report.get("issues", ())):
        text = str(issue)
        if not wanted or any(net in text for net in wanted):
            evidence.append(text)
    missing = tuple(str(net) for net in report.get("missing_nets", ()) if not wanted or str(net) in wanted)
    if missing:
        evidence.append(f"missing pin nets {missing}")
    return tuple(evidence)


def _layout_plan_shapes(layout_plan: object) -> tuple[LayoutShape, ...]:
    shapes: list[LayoutShape] = []
    for idx, rect in enumerate(tuple(getattr(layout_plan, "rects", ()))):
        net = str(getattr(rect, "net", ""))
        bbox = getattr(rect, "bbox", None)
        if net and bbox is not None:
            metadata = getattr(rect, "metadata", {})
            shapes.append(
                LayoutShape(
                    f"rect_{idx}",
                    str(getattr(rect, "layer", "")),
                    _bbox_tuple(bbox),
                    net,
                    dict(metadata) if isinstance(metadata, Mapping) else {},
                )
            )
    for idx, path in enumerate(tuple(getattr(layout_plan, "paths", ()))):
        net = str(getattr(path, "net", ""))
        layer = str(getattr(path, "layer", ""))
        width = float(getattr(path, "width", 0.0) or 0.0)
        points = tuple(getattr(path, "points", ()))
        if not net or not points:
            continue
        metadata = getattr(path, "metadata", {})
        shapes.extend(
            LayoutShape(
                f"path_{idx}_{seg_idx}",
                layer,
                bbox,
                net,
                dict(metadata) if isinstance(metadata, Mapping) else {},
            )
            for seg_idx, bbox in enumerate(_path_segment_bboxes(points, width))
        )
    return tuple(shapes)


def _path_segment_bboxes(points: tuple[tuple[float, float], ...], width: float) -> tuple[tuple[float, float, float, float], ...]:
    half = width / 2.0
    if len(points) < 2:
        x, y = points[0]
        return ((x - half, y - half, x + half, y + half),)
    bboxes = []
    for start, end in zip(points, points[1:]):
        x0, y0 = start
        x1, y1 = end
        bboxes.append((min(x0, x1) - half, min(y0, y1) - half, max(x0, x1) + half, max(y0, y1) + half))
    return tuple(bboxes)


def _bbox_tuple(value: object) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = value  # type: ignore[misc]
    return (float(x0), float(y0), float(x1), float(y1))


def _is_density_or_dummy_rule(rule: str, text: str) -> bool:
    return "density" in text or "dummy" in text or "fill" in text or ".DN." in rule or rule.startswith(("DM", "DOD", "DPO", "SR_DOD", "SR_DPO", "SSD"))


def _owner_for_physical_layer(rule: str, layer: str, text: str) -> str:
    combined = f"{rule} {layer} {text}".upper()
    if _is_latchup_or_tap_rule(rule, layer, text):
        return "tap_guard"
    if _is_via_or_contact_rule(rule, layer, text):
        return "routing"
    if _is_pcell_geometry_rule(rule, layer, text):
        return "pcell"
    if layer.upper().startswith("M") or "MET" in combined:
        return "routing"
    return "manual"


def _is_pcell_geometry_rule(rule: str, layer: str, text: str) -> bool:
    combined = f"{rule} {layer} {text}".upper()
    return any(token in combined for token in (" OD", "PO", "POLY", "ACTIVE", "DIFF"))


def _is_pmos_marker_rule(rule: str, layer: str, text: str) -> bool:
    combined = f"{rule} {layer} {text}".upper()
    return any(token in combined for token in ("PMET", "PMOS", "PPLUS", " PP", "PM MARKER", "PMARK"))


def _is_latchup_or_tap_rule(rule: str, layer: str, text: str) -> bool:
    combined = f"{rule} {layer} {text}".upper()
    return any(token in combined for token in ("LUP", "LATCH", "TAP", "WELLTAP", "GUARD"))


def _is_enclosure_rule(rule: str, text: str) -> bool:
    return "enclosure" in text or " enc" in f" {text}" or ".EN." in rule or ".ENC" in rule


def _is_width_rule(rule: str, text: str) -> bool:
    return "width" in text or ".W." in rule


def _is_spacing_rule(rule: str, text: str) -> bool:
    return "spacing" in text or " space" in f" {text}" or ".S." in rule


def _is_area_or_antenna_rule(rule: str, text: str) -> bool:
    normalized_rule = str(rule or "").upper()
    return (
        "antenna" in text
        or "area" in text
        or ".A." in normalized_rule
        or "AREA" in normalized_rule
        or normalized_rule.startswith("AP.")
    )


def _is_via_or_contact_rule(rule: str, layer: str, text: str) -> bool:
    combined = f"{rule} {layer} {text}".upper()
    return "VIA" in combined or "CONTACT" in combined or " CO" in f" {combined}"



def apply_geometry_edits(shapes: list[LayoutShape], edits: list[GeometryEdit]) -> list[LayoutShape]:
    result = list(shapes)
    for edit in edits:
        if edit.target_bbox is None:
            continue
        applied = False
        for idx, shape in enumerate(result):
            matches_id = bool(edit.shape_id) and shape.id == edit.shape_id
            matches_bbox = not edit.shape_id and shape.layer == edit.layer and _bbox_overlaps(shape.bbox, edit.bbox)
            if matches_id or matches_bbox:
                result[idx] = LayoutShape(shape.id, shape.layer, edit.target_bbox, shape.net)
                applied = True
                break
        if not applied and edit.bbox is not None:
            result.append(LayoutShape(f"eco_{len(result)}", edit.layer, edit.target_bbox, edit.net))
    return result


def apply_lvs_route_patch(shapes: list[LayoutShape], *, net: str, points: list[tuple[float, float]], layer: str = "M1", width: float = 1.0) -> list[LayoutShape]:
    result = list(shapes)
    for idx, (a, b) in enumerate(zip(points, points[1:])):
        x0, y0 = a
        x1, y1 = b
        if abs(x0 - x1) >= abs(y0 - y1):
            bbox = (min(x0, x1), y0 - width / 2, max(x0, x1), y0 + width / 2)
        else:
            bbox = (x0 - width / 2, min(y0, y1), x0 + width / 2, max(y0, y1))
        result.append(LayoutShape(f"lvs_{net}_{idx}", layer, bbox, net))
    return result


def _bbox_overlaps(a: tuple[float, float, float, float] | None, b: tuple[float, float, float, float] | None) -> bool:
    if a is None or b is None:
        return False
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _bbox_contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float], *, tol: float = 1e-12) -> bool:
    return outer[0] <= inner[0] + tol and outer[1] <= inner[1] + tol and outer[2] + tol >= inner[2] and outer[3] + tol >= inner[3]



def detect_shape_shorts(shapes: list[LayoutShape]) -> list[tuple[str, str, tuple[float, float, float, float]]]:
    shorts = []
    for idx, a in enumerate(shapes):
        for b in shapes[idx + 1:]:
            if a.layer == b.layer and a.net and b.net and a.net != b.net:
                inter = rect_intersection(a.bbox, b.bbox)
                if inter is not None:
                    shorts.append((a.net, b.net, inter))
    return shorts


def isolate_short_shapes(shapes: list[LayoutShape], *, keep_net: str, spacing: float = 1.0) -> list[LayoutShape]:
    result = []
    for shape in shapes:
        if shape.net and shape.net != keep_net:
            x0, y0, x1, y1 = shape.bbox
            result.append(LayoutShape(shape.id, shape.layer, (x0 + spacing, y0, x1 + spacing, y1), shape.net))
        else:
            result.append(shape)
    return result


def _bbox_intersection(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))



def rect_intersection(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> tuple[float, float, float, float] | None:
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def rect_area(rect: tuple[float, float, float, float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def rect_subtract(rect: tuple[float, float, float, float], cutter: tuple[float, float, float, float]) -> tuple[tuple[float, float, float, float], ...]:
    inter = rect_intersection(rect, cutter)
    if inter is None:
        return (rect,)
    x0, y0, x1, y1 = rect
    ix0, iy0, ix1, iy1 = inter
    pieces = [
        (x0, y0, ix0, y1),
        (ix1, y0, x1, y1),
        (ix0, y0, ix1, iy0),
        (ix0, iy1, ix1, y1),
    ]
    return tuple(piece for piece in pieces if rect_area(piece) > 0)


def boolean_subtract_shapes(shapes: list[LayoutShape], cutters: list[LayoutShape] | list[tuple[float, float, float, float]], *, layer: str | None = None, net: str | None = None) -> list[LayoutShape]:
    cutter_boxes = [c.bbox if isinstance(c, LayoutShape) else c for c in cutters]
    result: list[LayoutShape] = []
    for shape in shapes:
        if layer is not None and shape.layer != layer:
            result.append(shape)
            continue
        if net is not None and shape.net != net:
            result.append(shape)
            continue
        pieces = [shape.bbox]
        for cutter in cutter_boxes:
            next_pieces: list[tuple[float, float, float, float]] = []
            for piece in pieces:
                next_pieces.extend(rect_subtract(piece, cutter))
            pieces = next_pieces
        for idx, piece in enumerate(pieces):
            result.append(LayoutShape(f"{shape.id}_cut{idx}" if len(pieces) > 1 else shape.id, shape.layer, piece, shape.net))
    return result


def boolean_union_shapes(shapes: list[LayoutShape]) -> list[LayoutShape]:
    pending = list(shapes)
    changed = True
    while changed:
        changed = False
        result: list[LayoutShape] = []
        used = [False] * len(pending)
        for i, shape in enumerate(pending):
            if used[i]:
                continue
            merged = shape
            used[i] = True
            for j in range(i + 1, len(pending)):
                other = pending[j]
                if used[j] or merged.layer != other.layer or merged.net != other.net:
                    continue
                if _rectangles_mergeable(merged.bbox, other.bbox):
                    merged = LayoutShape(merged.id, merged.layer, _bbox_union(merged.bbox, other.bbox), merged.net)
                    used[j] = True
                    changed = True
            result.append(merged)
        pending = result
    return pending


def repair_short_by_cut(shapes: list[LayoutShape], *, keep_net: str, cut_bbox: tuple[float, float, float, float] | None = None, layer: str | None = None) -> list[LayoutShape]:
    if cut_bbox is None:
        shorts = detect_shape_shorts(shapes)
        if not shorts:
            return list(shapes)
        cut_bbox = shorts[0][2]
    result: list[LayoutShape] = []
    for shape in shapes:
        if shape.net and shape.net != keep_net and (layer is None or shape.layer == layer):
            pieces = rect_subtract(shape.bbox, cut_bbox)
            for idx, piece in enumerate(pieces):
                result.append(LayoutShape(f"{shape.id}_iso{idx}", shape.layer, piece, shape.net))
        else:
            result.append(shape)
    return result


def repair_spacing_by_push(shapes: list[LayoutShape], *, min_spacing: float, fixed_net: str = "") -> list[LayoutShape]:
    result = list(shapes)
    for i, a in enumerate(result):
        for j in range(i + 1, len(result)):
            b = result[j]
            if a.layer != b.layer or not a.net or not b.net or a.net == b.net:
                continue
            gap_x = max(b.bbox[0] - a.bbox[2], a.bbox[0] - b.bbox[2], 0.0)
            gap_y = max(b.bbox[1] - a.bbox[3], a.bbox[1] - b.bbox[3], 0.0)
            if max(gap_x, gap_y) >= min_spacing:
                continue
            move_idx = i if fixed_net and b.net == fixed_net else j
            moving = result[move_idx]
            dx = min_spacing - max(gap_x, 0.0)
            if moving.bbox[0] < (a.bbox[0] + b.bbox[0]) / 2:
                dx = -dx
            x0, y0, x1, y1 = moving.bbox
            result[move_idx] = LayoutShape(moving.id, moving.layer, (x0 + dx, y0, x1 + dx, y1), moving.net)
    return result


def _rectangles_mergeable(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    if rect_intersection(a, b) is not None:
        return True
    same_y = abs(a[1] - b[1]) <= 1e-12 and abs(a[3] - b[3]) <= 1e-12 and (abs(a[2] - b[0]) <= 1e-12 or abs(b[2] - a[0]) <= 1e-12)
    same_x = abs(a[0] - b[0]) <= 1e-12 and abs(a[2] - b[2]) <= 1e-12 and (abs(a[3] - b[1]) <= 1e-12 or abs(b[3] - a[1]) <= 1e-12)
    return same_y or same_x


def _bbox_union(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))



def shape_spacing_violations(shapes: list[LayoutShape], *, min_spacing: float) -> list[tuple[str, str, float]]:
    violations: list[tuple[str, str, float]] = []
    for idx, a in enumerate(shapes):
        for b in shapes[idx + 1:]:
            if a.layer != b.layer or a.net == b.net:
                continue
            dist = _rect_distance(a.bbox, b.bbox)
            if dist < min_spacing:
                violations.append((a.id, b.id, dist))
    return violations


def repair_min_area_by_growth(shapes: list[LayoutShape], *, min_area: float) -> list[LayoutShape]:
    result: list[LayoutShape] = []
    for shape in shapes:
        area = rect_area(shape.bbox)
        if area >= min_area or area <= 0:
            result.append(shape)
            continue
        x0, y0, x1, y1 = shape.bbox
        width = x1 - x0
        height = y1 - y0
        if width <= height:
            target_width = min_area / max(height, 1e-12)
            delta = (target_width - width) / 2
            result.append(LayoutShape(shape.id, shape.layer, (x0 - delta, y0, x1 + delta, y1), shape.net))
        else:
            target_height = min_area / max(width, 1e-12)
            delta = (target_height - height) / 2
            result.append(LayoutShape(shape.id, shape.layer, (x0, y0 - delta, x1, y1 + delta), shape.net))
    return result


def clip_shapes_to_keepouts(shapes: list[LayoutShape], keepouts: list[tuple[float, float, float, float]], *, layer: str | None = None) -> list[LayoutShape]:
    return boolean_subtract_shapes(shapes, keepouts, layer=layer)


def _rect_distance(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    if rect_intersection(a, b) is not None:
        return 0.0
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _point_key_um(point: object) -> tuple[int, int]:
    try:
        x, y = point  # type: ignore[misc]
        return (int(round(float(x) * 1_000_000)), int(round(float(y) * 1_000_000)))
    except Exception:
        return (0, 0)


def _bbox_key_um(bbox: object) -> tuple[int, int, int, int]:
    try:
        x0, y0, x1, y1 = bbox  # type: ignore[misc]
        return (
            int(round(float(x0) * 1_000_000)),
            int(round(float(y0) * 1_000_000)),
            int(round(float(x1) * 1_000_000)),
            int(round(float(y1) * 1_000_000)),
        )
    except Exception:
        return (0, 0, 0, 0)


def _plan_short_signature(shorts: object) -> set[tuple[str, tuple[str, str], tuple[int, int, int, int] | None]]:
    signature: set[tuple[str, tuple[str, str], tuple[int, int, int, int] | None]] = set()
    for short in tuple(shorts or ()):
        layer = str(getattr(short, "layer", ""))
        nets = tuple(sorted((str(getattr(short, "net_a", "")), str(getattr(short, "net_b", "")))))
        overlap = rect_intersection(tuple(getattr(short, "bbox_a", (0.0, 0.0, 0.0, 0.0))), tuple(getattr(short, "bbox_b", (0.0, 0.0, 0.0, 0.0))))
        signature.add((layer, (nets[0], nets[1]), None if overlap is None else _bbox_key_um(overlap)))
    return signature


def _redundant_via_neighbor_rects(
    original_cut: tuple[float, float, float, float],
    *,
    via_def: str,
    net: str,
    pdk: PdkConfig | None,
    redundant_spacing_um: float | None,
    include_landing_enclosures: bool,
) -> tuple[tuple[tuple[float, float, float, float], tuple[tuple[str, tuple[float, float, float, float]], ...], str], ...]:
    cut_width = _rule_min_width_um(pdk, via_def, _bbox_width(original_cut))
    spacing = _redundant_via_spacing_um(pdk, via_def, redundant_spacing_um)
    pitch = cut_width + spacing
    cx, cy = _bbox_center(original_cut)
    half = cut_width / 2.0
    directions = (
        ("right", pitch, 0.0),
        ("left", -pitch, 0.0),
        ("up", 0.0, pitch),
        ("down", 0.0, -pitch),
    )
    rows: list[tuple[tuple[float, float, float, float], tuple[tuple[str, tuple[float, float, float, float]], ...], str]] = []
    rules = getattr(pdk, "rules", None)
    for direction, dx, dy in directions:
        cut = (cx + dx - half, cy + dy - half, cx + dx + half, cy + dy + half)
        cut = _snap_exact_size_bbox_around_center(cut, pdk=pdk)
        landings: list[tuple[str, tuple[float, float, float, float]]] = []
        if include_landing_enclosures:
            for layer in _via_required_layers_for_def(pdk, via_def):
                enclosure = _via_enclosure_um(pdk, via_def, layer)
                landing = _expand_bbox(cut, enclosure)
                if rules is not None and hasattr(rules, "snap_bbox_um"):
                    landing = rules.snap_bbox_um(landing, mode="outward")
                landings.append((layer, landing))
        rows.append((cut, tuple(landings), direction))
    return tuple(rows)


def _snap_exact_size_bbox_around_center(
    bbox: tuple[float, float, float, float],
    *,
    pdk: PdkConfig | None,
) -> tuple[float, float, float, float]:
    """Snap a fixed-size via/contact bbox to grid without outward growth."""

    rules = getattr(pdk, "rules", None)
    grid_nm = max(1, int(getattr(rules, "grid_nm", 1) or 1))
    x0, y0, x1, y1 = (float(value) for value in bbox)
    width_grid = max(1, int(round(abs(x1 - x0) * 1000.0 / grid_nm)))
    height_grid = max(1, int(round(abs(y1 - y0) * 1000.0 / grid_nm)))
    cx_grid = ((x0 + x1) * 0.5) * 1000.0 / grid_nm
    cy_grid = ((y0 + y1) * 0.5) * 1000.0 / grid_nm
    x0_grid = int(round(cx_grid - 0.5 * width_grid))
    y0_grid = int(round(cy_grid - 0.5 * height_grid))

    def to_um(grid_value: int) -> float:
        return round(grid_value * grid_nm * 1e-3, 12)

    return (
        to_um(x0_grid),
        to_um(y0_grid),
        to_um(x0_grid + width_grid),
        to_um(y0_grid + height_grid),
    )


def _via_neighbor_spacing_is_legal(
    plan: object,
    via_def: str,
    cut_bbox: tuple[float, float, float, float],
    original_cut: tuple[float, float, float, float],
    *,
    pdk: PdkConfig | None,
    tol_um: float = 1e-9,
) -> bool:
    min_spacing = _rule_min_spacing_um(pdk, via_def, 0.0)
    if min_spacing <= 0.0:
        return True
    for rect in tuple(getattr(plan, "rects", ())):
        if str(getattr(rect, "layer", "")) != via_def:
            continue
        bbox = _float_bbox(getattr(rect, "bbox", None))
        if bbox is None:
            continue
        if rect_intersection(cut_bbox, bbox) is not None:
            return False
        distance = _rect_axis_distance(cut_bbox, bbox)
        if abs(distance - min_spacing) <= tol_um:
            continue
        if distance + tol_um < min_spacing:
            return False
    # The new cut must be close enough to the original marker to satisfy the
    # redundant-via rule that motivated the ECO.
    return _rect_axis_distance(cut_bbox, original_cut) <= max(0.100, min_spacing) + tol_um


def _rule_min_width_um(pdk: PdkConfig | None, layer: str, fallback: float) -> float:
    if pdk is not None:
        try:
            return max(float(pdk.rules.min_width_um(layer)), 0.0)
        except Exception:
            pass
    return max(float(fallback), 0.0)


def _rule_min_spacing_um(pdk: PdkConfig | None, layer: str, fallback: float) -> float:
    if pdk is not None:
        try:
            return max(float(pdk.rules.min_spacing_um(layer)), 0.0)
        except Exception:
            pass
    return max(float(fallback), 0.0)


def _redundant_via_spacing_um(pdk: PdkConfig | None, via_def: str, override: float | None) -> float:
    if override is not None:
        return max(float(override), 0.0)
    spacing = _rule_min_spacing_um(pdk, via_def, 0.080)
    if spacing <= 0.0:
        spacing = 0.080
    return max(spacing, 0.0)


def _bbox_width(bbox: tuple[float, float, float, float]) -> float:
    return max(float(bbox[2]) - float(bbox[0]), 0.0)


def _via_required_layers_for_def(pdk: PdkConfig | None, via_def: str) -> tuple[str, ...]:
    if pdk is None:
        return ()
    for rule in tuple(getattr(pdk, "via_stack", ())):
        if str(getattr(rule, "via_def", "")) == via_def:
            return (str(getattr(rule, "lower_layer", "")), str(getattr(rule, "upper_layer", "")))
    return ()


def _via_enclosure_um(pdk: PdkConfig | None, via_def: str, layer: str) -> float:
    if pdk is not None:
        for key in (f"{via_def}_{layer}", f"{layer}_{via_def}"):
            try:
                return max(float(pdk.rules.enclosure(key)) * 1e-3, 0.0)
            except Exception:
                pass
    return 0.025


def _rect_axis_distance(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    if rect_intersection(a, b) is not None:
        return 0.0
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    if dx == 0.0:
        return dy
    if dy == 0.0:
        return dx
    return (dx * dx + dy * dy) ** 0.5



def repair_via_enclosure(
    metal_shapes: list[LayoutShape],
    via_shapes: list[LayoutShape],
    *,
    metal_layer: str,
    enclosure: float,
) -> list[LayoutShape]:
    result = list(metal_shapes)
    for via in via_shapes:
        required = _expand_bbox(via.bbox, enclosure)
        candidates = [idx for idx, shape in enumerate(result) if shape.layer == metal_layer and (not via.net or shape.net == via.net) and rect_intersection(shape.bbox, via.bbox) is not None]
        if candidates:
            idx = candidates[0]
            shape = result[idx]
            result[idx] = LayoutShape(shape.id, shape.layer, _bbox_union(shape.bbox, required), shape.net)
        else:
            result.append(LayoutShape(f"enc_{via.id}_{metal_layer}", metal_layer, required, via.net))
    return boolean_union_shapes(result)


def fill_notches(shapes: list[LayoutShape], *, notch_width: float) -> list[LayoutShape]:
    # Rectangular abstraction: merge same-net rectangles separated by a small gap.
    result = list(shapes)
    changed = True
    while changed:
        changed = False
        merged: list[LayoutShape] = []
        used = [False] * len(result)
        for i, a in enumerate(result):
            if used[i]:
                continue
            current = a
            used[i] = True
            for j in range(i + 1, len(result)):
                b = result[j]
                if used[j] or current.layer != b.layer or current.net != b.net:
                    continue
                if _notch_gap(current.bbox, b.bbox) <= notch_width:
                    current = LayoutShape(current.id, current.layer, _bbox_union(current.bbox, b.bbox), current.net)
                    used[j] = True
                    changed = True
            merged.append(current)
        result = merged
    return result


def repair_drc_by_rule(
    shapes: list[LayoutShape],
    issues: list[DrcIssue],
    *,
    min_width: float = 0.0,
    min_area: float = 0.0,
    spacing: float = 0.0,
    enclosure: float = 0.0,
) -> list[LayoutShape]:
    result = list(shapes)
    edits = propose_geometric_drc_edits(issues, min_width=min_width, enclosure=enclosure, spacing=spacing)
    result = apply_geometry_edits(result, edits)
    if min_area > 0:
        result = repair_min_area_by_growth(result, min_area=min_area)
    if spacing > 0:
        result = repair_spacing_by_push(result, min_spacing=spacing)
    return boolean_union_shapes(result)


def _notch_gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    if rect_intersection(a, b) is not None:
        return 0.0
    overlap_y = min(a[3], b[3]) - max(a[1], b[1])
    overlap_x = min(a[2], b[2]) - max(a[0], b[0])
    if overlap_y > 0:
        return max(b[0] - a[2], a[0] - b[2], 0.0)
    if overlap_x > 0:
        return max(b[1] - a[3], a[1] - b[3], 0.0)
    return float("inf")


def _grid_rules(grid: DesignRuleDeck | PdkConfig | int) -> DesignRuleDeck:
    if isinstance(grid, PdkConfig):
        return grid.rules
    if isinstance(grid, DesignRuleDeck):
        return grid
    if isinstance(grid, int):
        return DesignRuleDeck(grid_nm=grid)
    raise TypeError(f"unsupported grid source {type(grid)!r}")
