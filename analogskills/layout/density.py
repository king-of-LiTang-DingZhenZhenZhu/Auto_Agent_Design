"""Lightweight density and dummy-fill proposal helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from analogskills.pdk import PdkConfig
from analogskills.repair import rect_area, rect_intersection


@dataclass(frozen=True)
class DensityWindowReport:
    layer: str
    bbox: tuple[float, float, float, float]
    density: float
    target_density: float
    covered_area_um2: float
    deficit_area_um2: float


@dataclass(frozen=True)
class DensityFillSpec:
    layer: str
    bbox: tuple[float, float, float, float]
    window_bbox: tuple[float, float, float, float]
    net: str = ""


@dataclass(frozen=True)
class DensityFillImpactReport:
    passed: bool
    fill_count: int
    fill_area_um2: float
    issues: tuple[str, ...] = ()
    critical_overlap_count: int = 0
    keepout_overlap_count: int = 0
    critical_proximity_count: int = 0


@dataclass(frozen=True)
class DensityFillCandidate:
    plan: Any
    score: float
    costs: Mapping[str, float]
    impact: DensityFillImpactReport
    before_deficit_um2: float
    after_deficit_um2: float
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class DensityFillEcoSuggestion:
    action: str
    reason: str = ""
    priority: int = 5
    params: Mapping[str, object] = field(default_factory=dict)


def analyze_density_windows(
    shapes: Iterable[Any],
    *,
    layer: str,
    bbox: tuple[float, float, float, float] | None = None,
    window_um: float = 10.0,
    step_um: float | None = None,
    target_density: float = 0.2,
) -> tuple[DensityWindowReport, ...]:
    """Report per-window rectangular density for one layer."""

    if window_um <= 0:
        raise ValueError("density window size must be positive")
    step = step_um if step_um is not None else window_um
    if step <= 0:
        raise ValueError("density window step must be positive")

    layer_boxes = tuple(_shape_bbox(shape) for shape in shapes if _shape_layer(shape) == layer)
    region = bbox if bbox is not None else _bbox_union_all(layer_boxes)
    if region is None:
        return ()

    reports = []
    for window in _windows_for_bbox(region, window_um, step):
        area = rect_area(window)
        covered = _covered_area_um2(layer_boxes, window)
        density = covered / area if area > 0 else 0.0
        deficit = max(target_density * area - covered, 0.0)
        reports.append(DensityWindowReport(layer, window, density, target_density, covered, deficit))
    return tuple(reports)


def analyze_density_fill_impact(
    fill_plan: Any,
    *,
    critical_shapes: Iterable[Any] = (),
    keepouts: Iterable[tuple[float, float, float, float]] = (),
    proximity_um: float = 0.2,
    max_fill_area_um2: float | None = None,
) -> DensityFillImpactReport:
    """Report lightweight fill risk near critical shapes and keepouts."""

    if proximity_um < 0:
        raise ValueError("fill proximity must be non-negative")
    fill_rects = tuple(getattr(fill_plan, "rects", ()))
    fill_boxes = tuple(_shape_bbox(rect) for rect in fill_rects)
    critical = tuple(critical_shapes)
    critical_boxes = tuple(_shape_bbox(shape) for shape in critical)
    critical_layers = tuple(_shape_layer(shape) for shape in critical)
    keepout_boxes = tuple(tuple(float(value) for value in keepout) for keepout in keepouts)
    fill_area = sum(rect_area(box) for box in fill_boxes)

    critical_overlap = 0
    keepout_overlap = 0
    proximity = 0
    issues: list[str] = []
    for fill, fill_box in zip(fill_rects, fill_boxes):
        fill_layer = _shape_layer(fill)
        for critical_layer, critical_box in zip(critical_layers, critical_boxes):
            if fill_layer != critical_layer:
                continue
            if rect_intersection(fill_box, critical_box) is not None:
                critical_overlap += 1
                issues.append(f"fill on {fill_layer} overlaps critical shape")
            elif rect_intersection(_expand_bbox(critical_box, proximity_um), fill_box) is not None:
                proximity += 1
                issues.append(f"fill on {fill_layer} within {proximity_um:g}um of critical shape")
        for keepout in keepout_boxes:
            if rect_intersection(fill_box, keepout) is not None:
                keepout_overlap += 1
                issues.append("fill overlaps keepout")
    if max_fill_area_um2 is not None and fill_area > max_fill_area_um2:
        issues.append(f"fill area {fill_area:g}um^2 exceeds {max_fill_area_um2:g}um^2")

    return DensityFillImpactReport(
        passed=not issues,
        fill_count=len(fill_rects),
        fill_area_um2=fill_area,
        issues=tuple(dict.fromkeys(issues)),
        critical_overlap_count=critical_overlap,
        keepout_overlap_count=keepout_overlap,
        critical_proximity_count=proximity,
    )


def rank_density_fill_candidates(
    base_shapes: Iterable[Any],
    candidates: Iterable[Any],
    *,
    layer: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    window_um: float = 10.0,
    step_um: float | None = None,
    target_density: float = 0.2,
    critical_shapes: Iterable[Any] = (),
    keepouts: Iterable[tuple[float, float, float, float]] = (),
    proximity_um: float = 0.2,
    max_fill_area_um2: float | None = None,
    weights: Mapping[str, float] | None = None,
    top_k: int | None = None,
) -> tuple[DensityFillCandidate, ...]:
    """Rank fill proposal alternatives by density closure and critical-net risk."""

    base = tuple(base_shapes)
    candidate_tuple = tuple(candidates)
    layers = (str(layer),) if layer else _candidate_density_layers(base, candidate_tuple)
    weight_map = {
        "remaining_deficit": 1.0,
        "lost_deficit_closure": 0.5,
        "critical_overlap": 50.0,
        "keepout_overlap": 40.0,
        "critical_proximity": 10.0,
        "issue_count": 5.0,
        "fill_area": 0.02,
        "fill_count": 0.01,
        "area_limit": 2.0,
    }
    if weights:
        weight_map.update({str(key): float(value) for key, value in weights.items()})

    before_deficit = _density_deficit_sum(base, layers, bbox, window_um, step_um, target_density)
    rows = []
    for plan in candidate_tuple:
        impact = analyze_density_fill_impact(
            plan,
            critical_shapes=critical_shapes,
            keepouts=keepouts,
            proximity_um=proximity_um,
            max_fill_area_um2=max_fill_area_um2,
        )
        after_shapes = (*base, *tuple(getattr(plan, "rects", ())))
        after_deficit = _density_deficit_sum(after_shapes, layers, bbox, window_um, step_um, target_density)
        closure = max(before_deficit - after_deficit, 0.0)
        lost_closure = max(before_deficit - closure, 0.0)
        area_limit = max(impact.fill_area_um2 - float(max_fill_area_um2), 0.0) if max_fill_area_um2 is not None else 0.0
        costs = {
            "remaining_deficit": after_deficit,
            "lost_deficit_closure": lost_closure,
            "critical_overlap": float(impact.critical_overlap_count),
            "keepout_overlap": float(impact.keepout_overlap_count),
            "critical_proximity": float(impact.critical_proximity_count),
            "issue_count": float(len(impact.issues)),
            "fill_area": impact.fill_area_um2,
            "fill_count": float(impact.fill_count),
            "area_limit": area_limit,
        }
        issues = list(impact.issues)
        if before_deficit > 0 and closure <= 0:
            issues.append("fill candidate does not reduce density deficit")
        score = sum(weight_map.get(name, 0.0) * value for name, value in costs.items())
        rows.append(
            DensityFillCandidate(
                plan=plan,
                score=score,
                costs=costs,
                impact=impact,
                before_deficit_um2=before_deficit,
                after_deficit_um2=after_deficit,
                issues=tuple(dict.fromkeys(issues)),
            )
        )

    ranked = tuple(sorted(rows, key=lambda row: (row.score, len(row.issues), row.after_deficit_um2)))
    return ranked if top_k is None else ranked[:top_k]


def suggest_density_fill_ecos(
    candidates: DensityFillCandidate | Iterable[DensityFillCandidate],
    *,
    max_remaining_deficit_um2: float = 0.0,
    max_suggestions: int | None = None,
) -> tuple[DensityFillEcoSuggestion, ...]:
    """Map ranked fill candidates to reviewable ECO actions."""

    candidate_tuple = (candidates,) if isinstance(candidates, DensityFillCandidate) else tuple(candidates)
    suggestions: list[DensityFillEcoSuggestion] = []
    for idx, candidate in enumerate(candidate_tuple):
        label = "selected" if idx == 0 else f"candidate_{idx}"
        suggestions.extend(_density_fill_suggestions_for_candidate(candidate, label, max_remaining_deficit_um2))

    deduped: dict[tuple[str, tuple[tuple[str, object], ...]], DensityFillEcoSuggestion] = {}
    for suggestion in suggestions:
        params = tuple(sorted(dict(suggestion.params or {}).items()))
        key = (suggestion.action, params)
        current = deduped.get(key)
        if current is None or suggestion.priority < current.priority:
            deduped[key] = suggestion
    ranked = tuple(sorted(deduped.values(), key=lambda item: (item.priority, item.action, item.reason)))
    return ranked if max_suggestions is None else ranked[:max_suggestions]


def plan_density_fill(
    shapes: Iterable[Any],
    pdk: PdkConfig | None = None,
    *,
    lib: str = "work",
    cell: str = "density_fill",
    view: str = "layout",
    layer: str,
    bbox: tuple[float, float, float, float] | None = None,
    target_density: float = 0.2,
    window_um: float = 10.0,
    step_um: float | None = None,
    fill_width_um: float | None = None,
    fill_height_um: float | None = None,
    fill_spacing_um: float | None = None,
    keepouts: Iterable[tuple[float, float, float, float]] = (),
    net: str = "",
    max_fill_rects: int = 256,
    output: str = "oa",
):
    """Create LVS-neutral fill rectangles for low-density windows.

    The function proposes fill only.  It does not merge fill into a layout,
    short fill to any net, or override keepouts/critical-net spacing.
    """

    from analogskills.eda.oa import OaCellView, OaRect, OaWritePlan, snap_oa_write_plan_to_grid
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, LayoutRect, snap_layout_plan_to_grid

    pdk = pdk or PdkConfig.generic()
    if output not in {"oa", "layout_ir"}:
        raise ValueError("output must be 'oa' or 'layout_ir'")
    if max_fill_rects < 0:
        raise ValueError("max fill rectangle count must be non-negative")
    width = fill_width_um if fill_width_um is not None else _default_fill_dimension_um(pdk, layer)
    height = fill_height_um if fill_height_um is not None else width
    spacing = fill_spacing_um if fill_spacing_um is not None else _default_fill_spacing_um(pdk, layer)
    if width <= 0 or height <= 0:
        raise ValueError("fill dimensions must be positive")
    if spacing < 0:
        raise ValueError("fill spacing must be non-negative")

    shape_tuple = tuple(shapes)
    reports = analyze_density_windows(shape_tuple, layer=layer, bbox=bbox, window_um=window_um, step_um=step_um, target_density=target_density)
    fixed_boxes = tuple(_shape_bbox(shape) for shape in shape_tuple if _shape_layer(shape) == layer)
    blockers = tuple(_expand_bbox(box, spacing) for box in (*fixed_boxes, *tuple(keepouts)))
    rects = []
    for report in reports:
        if report.deficit_area_um2 <= 0:
            continue
        planned_area = 0.0
        for fill_bbox in _fill_bboxes_for_window(report.bbox, width, height, spacing):
            if len(rects) >= max_fill_rects or planned_area >= report.deficit_area_um2:
                break
            if any(rect_intersection(fill_bbox, blocker) is not None for blocker in blockers):
                continue
            rects.append(OaRect(layer, "drawing", fill_bbox, net))
            blockers = (*blockers, _expand_bbox(fill_bbox, spacing))
            planned_area += rect_area(fill_bbox)
        if len(rects) >= max_fill_rects:
            break

    oa_plan = OaWritePlan(
        OaCellView(lib, cell, view, "maskLayout"),
        nets=(net,) if net else (),
        rects=tuple(rects),
    )
    if output == "layout_ir":
        layout_plan = LayoutPlan(
            LayoutCellRef(lib, cell, view, "maskLayout"),
            nets=(net,) if net else (),
            rects=tuple(LayoutRect(rect.layer, rect.bbox, rect.net, rect.purpose) for rect in rects),
        )
        return snap_layout_plan_to_grid(layout_plan, pdk)
    return snap_oa_write_plan_to_grid(oa_plan, pdk)


def plan_density_fill_from_drc(
    shapes: Iterable[Any],
    issues: Iterable[Any],
    pdk: PdkConfig | None = None,
    *,
    lib: str = "work",
    cell: str = "density_fill_eco",
    view: str = "layout",
    bbox: tuple[float, float, float, float] | None = None,
    target_density: float = 0.2,
    window_um: float = 10.0,
    step_um: float | None = None,
    fill_width_um: float | None = None,
    fill_height_um: float | None = None,
    fill_spacing_um: float | None = None,
    keepouts: Iterable[tuple[float, float, float, float]] = (),
    layer_aliases: Mapping[str, str] | None = None,
    max_fill_rects_per_issue: int = 256,
    output: str = "oa",
):
    """Create fill proposals for DRC issues classified as density/dummy/fill."""

    from analogskills.eda.oa import OaCellView, OaWritePlan, merge_oa_write_plans
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, merge_layout_plans

    pdk = pdk or PdkConfig.generic()
    if output not in {"oa", "layout_ir"}:
        raise ValueError("output must be 'oa' or 'layout_ir'")
    aliases = {str(key): str(value) for key, value in dict(layer_aliases or {}).items()}
    current_shapes = tuple(shapes)
    plans = []
    seen: set[tuple[str, tuple[float, float, float, float] | None]] = set()
    for issue in issues:
        if not _is_density_issue(issue):
            continue
        layer = aliases.get(_issue_layer(issue), _issue_layer(issue))
        if not layer:
            continue
        region = _issue_bbox(issue) or bbox or _bbox_union_all(tuple(_shape_bbox(shape) for shape in current_shapes if _shape_layer(shape) == layer))
        key = (layer, region)
        if key in seen:
            continue
        seen.add(key)
        plan = plan_density_fill(
            current_shapes,
            pdk,
            lib=lib,
            cell=f"{cell}_{len(plans)}",
            view=view,
            layer=layer,
            bbox=region,
            target_density=target_density,
            window_um=window_um,
            step_um=step_um,
            fill_width_um=fill_width_um,
            fill_height_um=fill_height_um,
            fill_spacing_um=fill_spacing_um,
            keepouts=keepouts,
            max_fill_rects=max_fill_rects_per_issue,
            output=output,
        )
        if plan.rects:
            plans.append(plan)
            current_shapes = (*current_shapes, *plan.rects)
    if not plans:
        if output == "layout_ir":
            return LayoutPlan(LayoutCellRef(lib, cell, view, "maskLayout"))
        return OaWritePlan(OaCellView(lib, cell, view, "maskLayout"))
    if output == "layout_ir":
        return merge_layout_plans(*plans, cell=LayoutCellRef(lib, cell, view, "maskLayout"), grid=pdk)
    return merge_oa_write_plans(*plans, cellview=OaCellView(lib, cell, view, "maskLayout"), grid=pdk)


def _shape_layer(shape: Any) -> str:
    return str(getattr(shape, "layer", ""))


def _shape_bbox(shape: Any) -> tuple[float, float, float, float]:
    return tuple(float(value) for value in getattr(shape, "bbox"))


def _issue_layer(issue: Any) -> str:
    return str(getattr(issue, "layer", ""))


def _issue_bbox(issue: Any) -> tuple[float, float, float, float] | None:
    bbox = getattr(issue, "bbox", None)
    if bbox is None:
        return None
    return tuple(float(value) for value in bbox)


def _is_density_issue(issue: Any) -> bool:
    text = f"{getattr(issue, 'rule', '')} {getattr(issue, 'message', '')}".lower()
    rule = str(getattr(issue, "rule", "")).upper()
    return (
        "density" in text
        or "dummy" in text
        or "fill" in text
        or ".DN." in rule
        or rule.startswith(("DM", "DOD", "DPO", "SR_DOD", "SR_DPO", "SSD"))
    )


def _candidate_density_layers(base_shapes: tuple[Any, ...], candidates: tuple[Any, ...]) -> tuple[str, ...]:
    layers = []
    for shape in base_shapes:
        layer = _shape_layer(shape)
        if layer:
            layers.append(layer)
    for plan in candidates:
        for rect in getattr(plan, "rects", ()):
            layer = _shape_layer(rect)
            if layer:
                layers.append(layer)
    return tuple(dict.fromkeys(layers))


def _density_fill_suggestions_for_candidate(
    candidate: DensityFillCandidate,
    label: str,
    max_remaining_deficit_um2: float,
) -> tuple[DensityFillEcoSuggestion, ...]:
    suggestions: list[DensityFillEcoSuggestion] = []
    params = {
        "candidate": label,
        "score": candidate.score,
        "after_deficit_um2": candidate.after_deficit_um2,
    }
    if candidate.impact.critical_overlap_count:
        suggestions.append(
            DensityFillEcoSuggestion(
                "revise_fill_keepouts",
                "fill overlaps critical geometry",
                1,
                {**params, "critical_overlap_count": candidate.impact.critical_overlap_count},
            )
        )
    if candidate.impact.keepout_overlap_count:
        suggestions.append(
            DensityFillEcoSuggestion(
                "reject_or_clip_keepout_fill",
                "fill overlaps keepout",
                1,
                {**params, "keepout_overlap_count": candidate.impact.keepout_overlap_count},
            )
        )
    if candidate.impact.critical_proximity_count:
        suggestions.append(
            DensityFillEcoSuggestion(
                "increase_critical_net_fill_spacing",
                "fill is too close to critical geometry",
                2,
                {**params, "critical_proximity_count": candidate.impact.critical_proximity_count},
            )
        )
    if candidate.costs.get("area_limit", 0.0) > 0:
        suggestions.append(
            DensityFillEcoSuggestion(
                "trim_fill_area",
                "fill area exceeds review limit",
                3,
                {**params, "excess_fill_area_um2": candidate.costs["area_limit"]},
            )
        )
    if candidate.after_deficit_um2 > max_remaining_deficit_um2:
        suggestions.append(
            DensityFillEcoSuggestion(
                "increase_fill_budget_or_reduce_spacing",
                "density deficit remains after fill proposal",
                4,
                {**params, "remaining_deficit_um2": candidate.after_deficit_um2},
            )
        )
    if not suggestions and candidate.impact.passed:
        suggestions.append(
            DensityFillEcoSuggestion(
                "accept_density_fill_candidate",
                "fill candidate closes density without reported critical risk",
                8,
                params,
            )
        )
    return tuple(suggestions)


def _density_deficit_sum(
    shapes: tuple[Any, ...],
    layers: tuple[str, ...],
    bbox: tuple[float, float, float, float] | None,
    window_um: float,
    step_um: float | None,
    target_density: float,
) -> float:
    total = 0.0
    for layer in layers:
        total += sum(
            report.deficit_area_um2
            for report in analyze_density_windows(
                shapes,
                layer=layer,
                bbox=bbox,
                window_um=window_um,
                step_um=step_um,
                target_density=target_density,
            )
        )
    return total


def _bbox_union_all(boxes: tuple[tuple[float, float, float, float], ...]) -> tuple[float, float, float, float] | None:
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _windows_for_bbox(
    bbox: tuple[float, float, float, float],
    window_um: float,
    step_um: float,
) -> tuple[tuple[float, float, float, float], ...]:
    x0, y0, x1, y1 = bbox
    windows = []
    y = y0
    while y < y1:
        x = x0
        while x < x1:
            windows.append((x, y, min(x + window_um, x1), min(y + window_um, y1)))
            x += step_um
        y += step_um
    return tuple(window for window in windows if rect_area(window) > 0)


def _covered_area_um2(
    boxes: tuple[tuple[float, float, float, float], ...],
    window: tuple[float, float, float, float],
) -> float:
    clipped = tuple(inter for box in boxes if (inter := rect_intersection(box, window)) is not None)
    return _rect_union_area(clipped)


def _rect_union_area(rects: tuple[tuple[float, float, float, float], ...]) -> float:
    if not rects:
        return 0.0
    xs = sorted({coord for rect in rects for coord in (rect[0], rect[2])})
    area = 0.0
    for x0, x1 in zip(xs, xs[1:]):
        if x1 <= x0:
            continue
        intervals = sorted((rect[1], rect[3]) for rect in rects if rect[0] < x1 and rect[2] > x0)
        merged = _merge_intervals(intervals)
        area += (x1 - x0) * sum(y1 - y0 for y0, y1 in merged)
    return area


def _merge_intervals(intervals: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    merged: list[tuple[float, float]] = []
    for y0, y1 in intervals:
        if y1 <= y0:
            continue
        if not merged or y0 > merged[-1][1]:
            merged.append((y0, y1))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], y1))
    return tuple(merged)


def _fill_bboxes_for_window(
    window: tuple[float, float, float, float],
    width: float,
    height: float,
    spacing: float,
) -> tuple[tuple[float, float, float, float], ...]:
    x0, y0, x1, y1 = window
    pitch_x = width + spacing
    pitch_y = height + spacing
    bboxes = []
    y = y0 + spacing
    while y + height <= y1 - spacing:
        x = x0 + spacing
        while x + width <= x1 - spacing:
            bboxes.append((x, y, x + width, y + height))
            x += pitch_x
        y += pitch_y
    return tuple(bboxes)


def _default_fill_dimension_um(pdk: PdkConfig, layer: str) -> float:
    try:
        minimum = pdk.rules.min_width_um(layer)
    except KeyError:
        minimum = 0.1
    return pdk.rules.snap_dimension_um(max(minimum, 0.2))


def _default_fill_spacing_um(pdk: PdkConfig, layer: str) -> float:
    try:
        spacing = pdk.rules.min_spacing_um(layer)
    except KeyError:
        spacing = 0.1
    return pdk.rules.snap_dimension_um(spacing)


def _expand_bbox(bbox: tuple[float, float, float, float], amount: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    return (x0 - amount, y0 - amount, x1 + amount, y1 + amount)
