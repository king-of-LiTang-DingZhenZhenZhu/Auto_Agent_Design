"""Deterministic layout aesthetic scoring for analog blocks and systems.

The scores here are intentionally geometric and machine-readable.  They are
not signoff checks and they do not prescribe edits; their purpose is to expose
stable facts that DSL/SMT loops and agents can optimize against.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import pstdev
from typing import Any, Mapping, Sequence


BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class BlockAestheticScoreConfig:
    """Weights and tolerances for block-level aesthetic scoring."""

    alignment_tolerance_um: float = 0.08
    pin_boundary_tolerance_fraction: float = 0.08
    target_component_occupancy: float = 0.55
    target_route_to_component_bbox_ratio: float = 1.15
    heatmap_bins: int = 12


@dataclass(frozen=True)
class SystemAestheticScoreConfig:
    """Weights and tolerances for system-level floorplan aesthetic scoring."""

    alignment_tolerance_um: float = 0.25
    target_block_occupancy: float = 0.70
    target_aspect_ratio: float = 1.0
    heatmap_bins: int = 10


def score_block_layout_aesthetics(
    layout: Mapping[str, Any] | object,
    *,
    config: BlockAestheticScoreConfig | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score block layout aesthetics on a 0-100 scale."""

    cfg = _block_config(config)
    rects = _rows(layout, "rects")
    paths = _rows(layout, "paths")
    pins = _rows(layout, "pins")
    instances = _rows(layout, "instances")

    layout_rects = [row for row in rects if not _is_marker(row)]
    path_boxes = [box for box in (_path_bbox(row) for row in paths if _has_net(row)) if box is not None]
    pin_boxes = [box for box in (_rect_bbox(row) for row in pins if _has_net(row)) if box is not None]
    all_shape_boxes = [box for box in (_rect_bbox(row) for row in layout_rects) if box is not None]
    all_shape_boxes += path_boxes + pin_boxes
    shape_bbox = _bbox_union(all_shape_boxes)

    component_bboxes = _component_bboxes(layout_rects, instances)
    component_boxes = tuple(component_bboxes.values())
    component_bbox = _bbox_union(component_boxes) or shape_bbox
    route_bbox = _bbox_union(path_boxes)
    pin_bbox = _bbox_union(pin_boxes)
    reference_bbox = component_bbox or shape_bbox

    aspect = _aspect_ratio(reference_bbox)
    squareness_score = _score_squareness(aspect)
    vertical_symmetry = _heatmap_symmetry_score(component_boxes or all_shape_boxes, reference_bbox, axis="vertical", bins=cfg.heatmap_bins)
    horizontal_symmetry = _heatmap_symmetry_score(component_boxes or all_shape_boxes, reference_bbox, axis="horizontal", bins=cfg.heatmap_bins)
    symmetry_score = max(vertical_symmetry, horizontal_symmetry)
    alignment_score = _box_alignment_score(component_boxes, cfg.alignment_tolerance_um)
    component_occupancy = _safe_ratio(_union_area(component_boxes), _bbox_area(reference_bbox))
    component_occupancy_score = _score_min_target(component_occupancy, cfg.target_component_occupancy)

    route_orthogonality_score = _route_orthogonality_score(paths)
    route_envelope_ratio = _safe_ratio(_bbox_area(route_bbox), _bbox_area(reference_bbox))
    route_envelope_score = _score_ratio_ceiling(route_envelope_ratio, cfg.target_route_to_component_bbox_ratio, hard_ceiling=2.0)
    route_escape_by_side = _bbox_escape_by_side(route_bbox, reference_bbox)
    route_escape_max_um = max(route_escape_by_side.values()) if route_escape_by_side else 0.0
    escape_scale = max(_bbox_width(reference_bbox), _bbox_height(reference_bbox), 1.0) * 0.10
    route_escape_score = _score_escape(route_escape_max_um, escape_scale)
    route_symmetry_score = _heatmap_symmetry_score(path_boxes, reference_bbox, axis="vertical", bins=cfg.heatmap_bins)
    route_layer_coordination_score = _route_layer_coordination_score(paths)
    routing_score = _weighted_score(
        {
            "orthogonality": (route_orthogonality_score, 0.25),
            "envelope": (route_envelope_score, 0.30),
            "escape": (route_escape_score, 0.20),
            "symmetry_distribution": (route_symmetry_score, 0.15),
            "layer_coordination": (route_layer_coordination_score, 0.10),
        }
    )

    pin_boundary_score, pin_alignment_score, pin_side_score, pin_side_counts = _pin_scores(
        pin_boxes,
        shape_bbox or reference_bbox,
        cfg,
    )
    pin_score = _weighted_score(
        {
            "boundary": (pin_boundary_score, 0.45),
            "alignment": (pin_alignment_score, 0.35),
            "side_coordination": (pin_side_score, 0.20),
        }
    )

    placement_score = _weighted_score(
        {
            "symmetry": (symmetry_score, 0.35),
            "squareness": (squareness_score, 0.20),
            "alignment": (alignment_score, 0.25),
            "component_occupancy": (component_occupancy_score, 0.20),
        }
    )
    total = _weighted_score(
        {
            "placement": (placement_score, 0.45),
            "routing": (routing_score, 0.35),
            "pins": (pin_score, 0.20),
        }
    )

    return {
        "schema": "analogskills.block_aesthetic_score/v1",
        "score": _round_score(total),
        "grade": _grade(total),
        "scores": {
            "placement": _round_score(placement_score),
            "routing": _round_score(routing_score),
            "pins": _round_score(pin_score),
            "layout_symmetry": _round_score(symmetry_score),
            "layout_squareness": _round_score(squareness_score),
            "layout_alignment": _round_score(alignment_score),
            "layout_component_occupancy": _round_score(component_occupancy_score),
            "route_orthogonality": _round_score(route_orthogonality_score),
            "route_envelope": _round_score(route_envelope_score),
            "route_escape": _round_score(route_escape_score),
            "route_symmetry_distribution": _round_score(route_symmetry_score),
            "route_layer_coordination": _round_score(route_layer_coordination_score),
            "pin_boundary": _round_score(pin_boundary_score),
            "pin_alignment": _round_score(pin_alignment_score),
            "pin_side_coordination": _round_score(pin_side_score),
        },
        "metrics": {
            "component_count": len(component_boxes),
            "route_path_count": len([row for row in paths if _has_net(row)]),
            "pin_count": len(pin_boxes),
            "shape_bbox_um": _round_bbox(shape_bbox),
            "component_bbox_um": _round_bbox(component_bbox),
            "route_bbox_um": _round_bbox(route_bbox),
            "pin_bbox_um": _round_bbox(pin_bbox),
            "component_bbox_area_um2": _round_float(_bbox_area(component_bbox)),
            "component_union_area_um2": _round_float(_union_area(component_boxes)),
            "component_occupancy_ratio": _round_float(component_occupancy),
            "aspect_ratio": _round_float(aspect),
            "vertical_symmetry_score": _round_score(vertical_symmetry),
            "horizontal_symmetry_score": _round_score(horizontal_symmetry),
            "route_to_component_bbox_area_ratio": _round_float(route_envelope_ratio),
            "route_escape_by_side_um": {side: _round_float(value) for side, value in route_escape_by_side.items()},
            "pin_side_counts": pin_side_counts,
            "rect_count_by_instance": dict(sorted((name, _instance_rect_count(layout_rects, name)) for name in component_bboxes)),
            "path_count_by_layer": _count_by([row for row in paths if _has_net(row)], "layer"),
        },
        "deductions": _deductions(
            {
                "layout_symmetry": symmetry_score,
                "layout_squareness": squareness_score,
                "layout_alignment": alignment_score,
                "layout_component_occupancy": component_occupancy_score,
                "route_envelope": route_envelope_score,
                "route_escape": route_escape_score,
                "pin_boundary": pin_boundary_score,
                "pin_alignment": pin_alignment_score,
            }
        ),
    }


def enrich_block_aesthetic_report_with_layout_observation(
    aesthetic_report: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
    *,
    config: BlockAestheticScoreConfig | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach SMT/observation-level placement facts to a block aesthetic score.

    The raw OA score sees drawn rectangles.  For PCells this can seriously
    understate block-level packing because calibrated devices may contain sparse
    internal shapes.  The layout observation already records the macro/device
    bbox utilization seen by the SMT solver; this enrichment keeps both facts
    visible and uses the solver-visible utilization as the placement-feedback
    occupancy score when it is more representative.
    """

    if observation is None:
        return dict(aesthetic_report)
    cfg = _block_config(config)
    result = dict(aesthetic_report)
    scores = dict(_mapping(result.get("scores")))
    metrics = dict(_mapping(result.get("metrics")))
    compactness = _mapping(_mapping(observation).get("compactness"))
    global_c = _mapping(compactness.get("global"))
    group_util = _optional_float(global_c.get("device_utilization"))
    final_group_util = _optional_float(global_c.get("final_device_utilization"))
    effective_util = group_util if group_util is not None else final_group_util
    if effective_util is None:
        return result

    drawn_occupancy_score = _optional_float(scores.get("layout_component_occupancy"))
    placement_score_before = _optional_float(scores.get("placement"))
    total_score_before = _optional_float(result.get("score"))
    group_occupancy_score = _score_min_target(effective_util, cfg.target_component_occupancy)
    effective_occupancy_score = max(
        drawn_occupancy_score if drawn_occupancy_score is not None else 0.0,
        group_occupancy_score,
    )

    metrics.setdefault("component_occupancy_ratio_drawn", metrics.get("component_occupancy_ratio"))
    metrics["smt_group_device_utilization"] = _round_float(group_util)
    metrics["smt_final_group_device_utilization"] = _round_float(final_group_util)
    metrics["component_occupancy_ratio_effective"] = _round_float(effective_util)
    metrics["smt_group_bbox_tracks"] = global_c.get("bbox_tracks")
    metrics["smt_final_layout_bbox_tracks"] = global_c.get("final_layout_bbox_tracks")

    if drawn_occupancy_score is not None:
        scores.setdefault("layout_component_occupancy_drawn", _round_score(drawn_occupancy_score))
    scores["layout_smt_group_occupancy"] = _round_score(group_occupancy_score)
    scores["layout_component_occupancy_effective"] = _round_score(effective_occupancy_score)
    scores["layout_component_occupancy"] = _round_score(effective_occupancy_score)

    symmetry_score = _optional_float(scores.get("layout_symmetry"))
    squareness_score = _optional_float(scores.get("layout_squareness"))
    alignment_score = _optional_float(scores.get("layout_alignment"))
    routing_score = _optional_float(scores.get("routing"))
    pin_score = _optional_float(scores.get("pins"))
    if symmetry_score is not None and squareness_score is not None and alignment_score is not None:
        placement_score = _weighted_score(
            {
                "symmetry": (symmetry_score, 0.35),
                "squareness": (squareness_score, 0.20),
                "alignment": (alignment_score, 0.25),
                "component_occupancy": (effective_occupancy_score, 0.20),
            }
        )
        if placement_score_before is not None:
            scores.setdefault("placement_drawn", _round_score(placement_score_before))
        scores["placement"] = _round_score(placement_score)
        if routing_score is not None and pin_score is not None:
            total_score = _weighted_score(
                {
                    "placement": (placement_score, 0.45),
                    "routing": (routing_score, 0.35),
                    "pins": (pin_score, 0.20),
                }
            )
            if total_score_before is not None:
                scores.setdefault("total_drawn_oa", _round_score(total_score_before))
            result["score"] = _round_score(total_score)
            result["grade"] = _grade(total_score)

    result["scores"] = scores
    result["metrics"] = metrics
    result["aesthetic_score_enrichment"] = {
        "schema": "analogskills.block_aesthetic_score_enrichment/v1",
        "source": "layout_observation.compactness.global",
        "reason": "PCell drawn-shape occupancy can understate solver-visible macro packing.",
        "drawn_occupancy_score": None if drawn_occupancy_score is None else _round_score(drawn_occupancy_score),
        "smt_group_occupancy_score": _round_score(group_occupancy_score),
        "effective_occupancy_score": _round_score(effective_occupancy_score),
        "score_before": None if total_score_before is None else _round_score(total_score_before),
        "score_after": result.get("score"),
    }
    result["deductions"] = _deductions(
        {
            "layout_symmetry": float(scores.get("layout_symmetry", 100.0) or 100.0),
            "layout_squareness": float(scores.get("layout_squareness", 100.0) or 100.0),
            "layout_alignment": float(scores.get("layout_alignment", 100.0) or 100.0),
            "layout_component_occupancy": float(scores.get("layout_component_occupancy", 100.0) or 100.0),
            "route_envelope": float(scores.get("route_envelope", 100.0) or 100.0),
            "route_escape": float(scores.get("route_escape", 100.0) or 100.0),
            "pin_boundary": float(scores.get("pin_boundary", 100.0) or 100.0),
            "pin_alignment": float(scores.get("pin_alignment", 100.0) or 100.0),
        }
    )
    return result


def score_system_layout_aesthetics(
    floorplan: Mapping[str, Any] | object,
    *,
    config: SystemAestheticScoreConfig | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score system/macro-level floorplan aesthetics on a 0-100 scale."""

    cfg = _system_config(config)
    block_bboxes = _system_block_bboxes(floorplan)
    bbox = _bbox_union(tuple(block_bboxes.values()))
    aspect = _aspect_ratio(bbox)
    aspect_score = _score_aspect_target(aspect, cfg.target_aspect_ratio)
    alignment_score = _box_alignment_score(tuple(block_bboxes.values()), cfg.alignment_tolerance_um)
    symmetry_score = max(
        _heatmap_symmetry_score(tuple(block_bboxes.values()), bbox, axis="vertical", bins=cfg.heatmap_bins),
        _heatmap_symmetry_score(tuple(block_bboxes.values()), bbox, axis="horizontal", bins=cfg.heatmap_bins),
    )
    occupancy = _safe_ratio(_union_area(tuple(block_bboxes.values())), _bbox_area(bbox))
    occupancy_score = _score_min_target(occupancy, cfg.target_block_occupancy)
    row_col_score = _row_column_score(tuple(block_bboxes.values()), cfg.alignment_tolerance_um)
    edge_score = _system_edge_score(floorplan, block_bboxes, bbox)
    corridor_score = _system_corridor_score(floorplan, bbox)

    floorplan_score = _weighted_score(
        {
            "aspect": (aspect_score, 0.18),
            "alignment": (alignment_score, 0.28),
            "row_column_regular": (row_col_score, 0.22),
            "symmetry": (symmetry_score, 0.17),
            "occupancy": (occupancy_score, 0.15),
        }
    )
    interconnect_score = _weighted_score(
        {
            "signal_flow_compactness": (edge_score, 0.60),
            "corridor_coordination": (corridor_score, 0.40),
        }
    )
    total = _weighted_score(
        {
            "floorplan": (floorplan_score, 0.70),
            "interconnect": (interconnect_score, 0.30),
        }
    )

    return {
        "schema": "analogskills.system_aesthetic_score/v1",
        "score": _round_score(total),
        "grade": _grade(total),
        "scores": {
            "floorplan": _round_score(floorplan_score),
            "interconnect": _round_score(interconnect_score),
            "aspect": _round_score(aspect_score),
            "alignment": _round_score(alignment_score),
            "row_column_regular": _round_score(row_col_score),
            "symmetry": _round_score(symmetry_score),
            "occupancy": _round_score(occupancy_score),
            "signal_flow_compactness": _round_score(edge_score),
            "corridor_coordination": _round_score(corridor_score),
        },
        "metrics": {
            "block_count": len(block_bboxes),
            "bbox_um": _round_bbox(bbox),
            "aspect_ratio": _round_float(aspect),
            "block_occupancy_ratio": _round_float(occupancy),
            "block_bboxes_um": {name: _round_bbox(box) for name, box in sorted(block_bboxes.items())},
            "edge_count": len(_system_edges(floorplan)),
            "corridor_count": len(_system_corridors(floorplan)),
        },
        "deductions": _deductions(
            {
                "system_aspect": aspect_score,
                "system_alignment": alignment_score,
                "system_row_column_regular": row_col_score,
                "system_symmetry": symmetry_score,
                "system_occupancy": occupancy_score,
                "system_signal_flow_compactness": edge_score,
                "system_corridor_coordination": corridor_score,
            }
        ),
    }


def _block_config(config: BlockAestheticScoreConfig | Mapping[str, Any] | None) -> BlockAestheticScoreConfig:
    if config is None:
        return BlockAestheticScoreConfig()
    if isinstance(config, BlockAestheticScoreConfig):
        return config
    return BlockAestheticScoreConfig(
        alignment_tolerance_um=float(config.get("alignment_tolerance_um", 0.08) or 0.08),
        pin_boundary_tolerance_fraction=float(config.get("pin_boundary_tolerance_fraction", 0.08) or 0.08),
        target_component_occupancy=float(config.get("target_component_occupancy", 0.55) or 0.55),
        target_route_to_component_bbox_ratio=float(config.get("target_route_to_component_bbox_ratio", 1.15) or 1.15),
        heatmap_bins=max(4, int(config.get("heatmap_bins", 12) or 12)),
    )


def _system_config(config: SystemAestheticScoreConfig | Mapping[str, Any] | None) -> SystemAestheticScoreConfig:
    if config is None:
        return SystemAestheticScoreConfig()
    if isinstance(config, SystemAestheticScoreConfig):
        return config
    return SystemAestheticScoreConfig(
        alignment_tolerance_um=float(config.get("alignment_tolerance_um", 0.25) or 0.25),
        target_block_occupancy=float(config.get("target_block_occupancy", 0.70) or 0.70),
        target_aspect_ratio=float(config.get("target_aspect_ratio", 1.0) or 1.0),
        heatmap_bins=max(4, int(config.get("heatmap_bins", 10) or 10)),
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _rows(container: Mapping[str, Any] | object, field: str) -> list[Mapping[str, Any]]:
    if isinstance(container, Mapping):
        raw = container.get(field, ()) or ()
    else:
        raw = getattr(container, field, ()) or ()
    rows: list[Mapping[str, Any]] = []
    for item in raw:
        rows.append(item if isinstance(item, Mapping) else _object_row(item))
    return rows


def _object_row(item: object) -> Mapping[str, Any]:
    row: dict[str, Any] = {}
    for name in (
        "name",
        "layer",
        "purpose",
        "bbox",
        "bbox_um",
        "net",
        "metadata",
        "points",
        "width",
        "xy",
        "x_um",
        "y_um",
        "width_um",
        "height_um",
        "role",
        "source",
        "target",
        "weight",
    ):
        if hasattr(item, name):
            row[name] = getattr(item, name)
    return row


def _has_net(row: Mapping[str, Any]) -> bool:
    return bool(str(row.get("net", "") or "").strip())


def _is_marker(row: Mapping[str, Any]) -> bool:
    metadata = row.get("metadata", {}) or {}
    if isinstance(metadata, Mapping) and (
        metadata.get("marker_role") or metadata.get("marker_name") or metadata.get("marker_parent")
    ):
        return True
    if _has_net(row):
        return False
    purpose = str(row.get("purpose", "") or "").strip().lower()
    if purpose.startswith("dummy") or purpose in {"marker", "fill"}:
        return True
    if isinstance(metadata, Mapping):
        kind = str(metadata.get("kind", "") or "").strip().lower()
        return "marker" in kind or "dummy" in kind or "density" in kind
    return False


def _component_bboxes(rects: Sequence[Mapping[str, Any]], instances: Sequence[Mapping[str, Any]]) -> dict[str, BBox]:
    boxes_by_instance: dict[str, list[BBox]] = {}
    for row in rects:
        metadata = row.get("metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            continue
        name = str(metadata.get("instance", "") or "").strip()
        box = _rect_bbox(row)
        if name and box is not None:
            boxes_by_instance.setdefault(name, []).append(box)
    result = {name: box for name, boxes in boxes_by_instance.items() if (box := _bbox_union(boxes)) is not None}

    for inst in instances:
        name = str(inst.get("name", "") or "").strip()
        if not name or name in result:
            continue
        box = _instance_bbox(inst)
        if box is not None:
            result[name] = box
    return dict(sorted(result.items()))


def _instance_bbox(row: Mapping[str, Any]) -> BBox | None:
    box = _bbox4(row.get("bbox_um") or row.get("bbox"))
    if box is not None:
        return box
    try:
        x, y = row.get("xy", (row.get("x_um"), row.get("y_um")))  # type: ignore[misc]
        width = row.get("width_um")
        height = row.get("height_um")
        if width is None or height is None:
            return None
        return _valid_bbox((float(x), float(y), float(x) + float(width), float(y) + float(height)))
    except Exception:
        return None


def _instance_rect_count(rects: Sequence[Mapping[str, Any]], instance: str) -> int:
    count = 0
    for row in rects:
        metadata = row.get("metadata", {}) or {}
        if isinstance(metadata, Mapping) and str(metadata.get("instance", "") or "") == instance:
            count += 1
    return count


def _rect_bbox(row: Mapping[str, Any]) -> BBox | None:
    return _bbox4(row.get("bbox_um") or row.get("bbox"))


def _path_bbox(row: Mapping[str, Any]) -> BBox | None:
    points = row.get("points", ()) or ()
    parsed: list[tuple[float, float]] = []
    for item in points:
        try:
            x, y = item
        except Exception:
            continue
        parsed.append((float(x), float(y)))
    if not parsed:
        return None
    half = max(float(row.get("width", 0.0) or 0.0), 0.0) * 0.5
    return _valid_bbox(
        (
            min(x for x, _ in parsed) - half,
            min(y for _, y in parsed) - half,
            max(x for x, _ in parsed) + half,
            max(y for _, y in parsed) + half,
        )
    )


def _bbox4(value: object) -> BBox | None:
    try:
        raw = tuple(value or ())  # type: ignore[arg-type]
    except TypeError:
        return None
    if len(raw) != 4:
        return None
    return _valid_bbox((float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])))


def _valid_bbox(box: BBox) -> BBox | None:
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


def _bbox_union(boxes: Sequence[BBox]) -> BBox | None:
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _bbox_area(box: BBox | None) -> float | None:
    if box is None:
        return None
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _bbox_width(box: BBox | None) -> float:
    return 0.0 if box is None else max(0.0, box[2] - box[0])


def _bbox_height(box: BBox | None) -> float:
    return 0.0 if box is None else max(0.0, box[3] - box[1])


def _aspect_ratio(box: BBox | None) -> float | None:
    width = _bbox_width(box)
    height = _bbox_height(box)
    if width <= 0 or height <= 0:
        return None
    return width / height


def _score_squareness(aspect: float | None) -> float:
    if aspect is None or aspect <= 0:
        return 50.0
    return _clip100(100.0 * min(aspect, 1.0 / aspect))


def _score_aspect_target(aspect: float | None, target: float) -> float:
    if aspect is None or aspect <= 0 or target <= 0:
        return 50.0
    ratio = max(aspect / target, target / aspect)
    return _clip100(100.0 / ratio)


def _box_alignment_score(boxes: Sequence[BBox], tol: float) -> float:
    if len(boxes) <= 2:
        return 100.0
    x0 = [box[0] for box in boxes]
    x1 = [box[2] for box in boxes]
    cx = [(box[0] + box[2]) * 0.5 for box in boxes]
    y0 = [box[1] for box in boxes]
    y1 = [box[3] for box in boxes]
    cy = [(box[1] + box[3]) * 0.5 for box in boxes]
    return _weighted_score(
        {
            "x_edges": ((_duplicate_cluster_fraction(x0, tol) + _duplicate_cluster_fraction(x1, tol)) * 0.5, 0.25),
            "y_edges": ((_duplicate_cluster_fraction(y0, tol) + _duplicate_cluster_fraction(y1, tol)) * 0.5, 0.25),
            "x_centers": (_duplicate_cluster_fraction(cx, tol), 0.25),
            "y_centers": (_duplicate_cluster_fraction(cy, tol), 0.25),
        }
    )


def _row_column_score(boxes: Sequence[BBox], tol: float) -> float:
    if len(boxes) <= 2:
        return 100.0
    cx = [(box[0] + box[2]) * 0.5 for box in boxes]
    cy = [(box[1] + box[3]) * 0.5 for box in boxes]
    row_score = _duplicate_cluster_fraction(cy, tol)
    col_score = _duplicate_cluster_fraction(cx, tol)
    spacing_score = (_spacing_uniformity_score(_cluster_centers(cy, tol)) + _spacing_uniformity_score(_cluster_centers(cx, tol))) * 0.5
    return _weighted_score({"rows": (row_score, 0.35), "columns": (col_score, 0.35), "spacing": (spacing_score, 0.30)})


def _duplicate_cluster_fraction(values: Sequence[float], tol: float) -> float:
    if len(values) <= 2:
        return 100.0
    clusters = _clusters(values, tol)
    covered = sum(len(cluster) for cluster in clusters if len(cluster) >= 2)
    return _clip100(100.0 * covered / len(values))


def _cluster_centers(values: Sequence[float], tol: float) -> tuple[float, ...]:
    return tuple(sum(cluster) / len(cluster) for cluster in _clusters(values, tol))


def _clusters(values: Sequence[float], tol: float) -> list[list[float]]:
    if not values:
        return []
    sorted_values = sorted(float(v) for v in values)
    clusters: list[list[float]] = [[sorted_values[0]]]
    for value in sorted_values[1:]:
        if abs(value - clusters[-1][-1]) <= max(float(tol), 0.0):
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return clusters


def _heatmap_symmetry_score(boxes: Sequence[BBox], bbox: BBox | None, *, axis: str, bins: int) -> float:
    if not boxes or bbox is None:
        return 100.0
    nx = ny = max(4, int(bins))
    width = _bbox_width(bbox)
    height = _bbox_height(bbox)
    if width <= 0 or height <= 0:
        return 50.0
    grid = [[0.0 for _ in range(ny)] for _ in range(nx)]
    for box in boxes:
        x_start = max(0, min(nx - 1, int((box[0] - bbox[0]) / width * nx)))
        x_stop = max(0, min(nx - 1, int((box[2] - bbox[0]) / width * nx)))
        y_start = max(0, min(ny - 1, int((box[1] - bbox[1]) / height * ny)))
        y_stop = max(0, min(ny - 1, int((box[3] - bbox[1]) / height * ny)))
        for ix in range(x_start, x_stop + 1):
            bx0 = bbox[0] + width * ix / nx
            bx1 = bbox[0] + width * (ix + 1) / nx
            for iy in range(y_start, y_stop + 1):
                by0 = bbox[1] + height * iy / ny
                by1 = bbox[1] + height * (iy + 1) / ny
                overlap = _overlap_area(box, (bx0, by0, bx1, by1))
                if overlap > 0:
                    grid[ix][iy] += overlap
    total = sum(sum(col) for col in grid)
    if total <= 0:
        return 100.0
    diff = 0.0
    if axis == "vertical":
        for ix in range(nx):
            mirror = nx - 1 - ix
            for iy in range(ny):
                diff += abs(grid[ix][iy] - grid[mirror][iy])
    else:
        for ix in range(nx):
            for iy in range(ny):
                mirror = ny - 1 - iy
                diff += abs(grid[ix][iy] - grid[ix][mirror])
    return _clip100(100.0 * (1.0 - diff / max(2.0 * total, 1e-12)))


def _overlap_area(a: BBox, b: BBox) -> float:
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    if dx <= 0 or dy <= 0:
        return 0.0
    return dx * dy


def _route_orthogonality_score(paths: Sequence[Mapping[str, Any]]) -> float:
    total = 0
    manhattan = 0
    for row in paths:
        if not _has_net(row):
            continue
        points = []
        for item in row.get("points", ()) or ():
            try:
                x, y = item
            except Exception:
                continue
            points.append((float(x), float(y)))
        for left, right in zip(points, points[1:]):
            dx = abs(left[0] - right[0])
            dy = abs(left[1] - right[1])
            if dx <= 1e-9 and dy <= 1e-9:
                continue
            total += 1
            if dx <= 1e-9 or dy <= 1e-9:
                manhattan += 1
    if total == 0:
        return 100.0
    return _clip100(100.0 * manhattan / total)


def _route_layer_coordination_score(paths: Sequence[Mapping[str, Any]]) -> float:
    layers = _count_by([row for row in paths if _has_net(row)], "layer")
    count = len(layers)
    if count == 0:
        return 100.0
    if count <= 4:
        return 100.0
    return _clip100(max(65.0, 100.0 - 5.0 * (count - 4)))


def _bbox_escape_by_side(box: BBox | None, reference: BBox | None) -> dict[str, float]:
    if box is None or reference is None:
        return {}
    return {
        "left": max(0.0, reference[0] - box[0]),
        "bottom": max(0.0, reference[1] - box[1]),
        "right": max(0.0, box[2] - reference[2]),
        "top": max(0.0, box[3] - reference[3]),
    }


def _score_escape(escape_um: float, scale_um: float) -> float:
    if scale_um <= 0:
        return 100.0 if escape_um <= 0 else 50.0
    return _clip100(100.0 * (1.0 - float(escape_um) / max(scale_um, 1e-12)))


def _score_ratio_ceiling(value: float | None, target: float, *, hard_ceiling: float) -> float:
    if value is None:
        return 100.0
    if value <= target:
        return 100.0
    if value >= hard_ceiling:
        return 35.0
    return _clip100(100.0 - 65.0 * (value - target) / max(hard_ceiling - target, 1e-12))


def _score_min_target(value: float | None, target: float) -> float:
    if value is None:
        return 50.0
    if value <= 0:
        return 0.0
    if value <= target:
        return _clip100(100.0 * value / max(target, 1e-12))
    return _clip100(100.0 - 20.0 * max(0.0, value - target) / max(1.0 - target, 1e-12))


def _pin_scores(
    pin_boxes: Sequence[BBox],
    bbox: BBox | None,
    cfg: BlockAestheticScoreConfig,
) -> tuple[float, float, float, dict[str, int]]:
    if not pin_boxes or bbox is None:
        return 100.0, 100.0, 100.0, {}
    width = _bbox_width(bbox)
    height = _bbox_height(bbox)
    scale = max(min(width, height) * cfg.pin_boundary_tolerance_fraction, 0.1)
    side_counts: dict[str, int] = {}
    side_positions: dict[str, list[float]] = {}
    boundary_scores: list[float] = []
    for box in pin_boxes:
        cx = (box[0] + box[2]) * 0.5
        cy = (box[1] + box[3]) * 0.5
        distances = {
            "left": abs(cx - bbox[0]),
            "right": abs(cx - bbox[2]),
            "bottom": abs(cy - bbox[1]),
            "top": abs(cy - bbox[3]),
        }
        side = min(distances, key=distances.get)
        side_counts[side] = side_counts.get(side, 0) + 1
        side_positions.setdefault(side, []).append(cy if side in {"left", "right"} else cx)
        boundary_scores.append(_score_escape(distances[side], scale))
    boundary_score = sum(boundary_scores) / len(boundary_scores)
    alignment_parts = [_spacing_uniformity_score(values) for values in side_positions.values()]
    alignment_score = sum(alignment_parts) / len(alignment_parts) if alignment_parts else 100.0
    side_count = len(side_counts)
    if side_count <= 2:
        side_score = 100.0
    elif side_count == 3:
        side_score = 78.0
    else:
        side_score = 62.0
    return boundary_score, alignment_score, side_score, dict(sorted(side_counts.items()))


def _spacing_uniformity_score(values: Sequence[float]) -> float:
    vals = sorted(float(v) for v in values)
    if len(vals) <= 2:
        return 100.0
    gaps = [right - left for left, right in zip(vals, vals[1:]) if right > left]
    if len(gaps) <= 1:
        return 100.0
    mean = sum(gaps) / len(gaps)
    if mean <= 1e-12:
        return 100.0
    cv = pstdev(gaps) / mean
    return _clip100(100.0 * (1.0 - min(cv, 1.0)))


def _system_block_bboxes(floorplan: Mapping[str, Any] | object) -> dict[str, BBox]:
    if isinstance(floorplan, Mapping):
        raw = floorplan.get("floorplan") or floorplan.get("placements") or floorplan.get("blocks") or ()
    else:
        raw = getattr(floorplan, "floorplan", None) or getattr(floorplan, "placements", None) or getattr(floorplan, "blocks", ())
    result: dict[str, BBox] = {}
    for idx, item in enumerate(raw or ()):
        row = item if isinstance(item, Mapping) else _object_row(item)
        name = str(row.get("name", f"block_{idx}") or f"block_{idx}")
        box = _bbox4(row.get("bbox_um") or row.get("bbox"))
        if box is None:
            try:
                x = float(row.get("x_um", row.get("x", 0.0)) or 0.0)
                y = float(row.get("y_um", row.get("y", 0.0)) or 0.0)
                w = float(row.get("width_um", row.get("width", 0.0)) or 0.0)
                h = float(row.get("height_um", row.get("height", 0.0)) or 0.0)
                box = _valid_bbox((x, y, x + w, y + h))
            except Exception:
                box = None
        if box is not None:
            result[name] = box
    return dict(sorted(result.items()))


def _system_edges(floorplan: Mapping[str, Any] | object) -> list[Mapping[str, Any]]:
    if isinstance(floorplan, Mapping):
        raw = floorplan.get("edges") or floorplan.get("nets") or ()
    else:
        raw = getattr(floorplan, "edges", ())
    return [row if isinstance(row, Mapping) else _object_row(row) for row in raw or ()]


def _system_corridors(floorplan: Mapping[str, Any] | object) -> list[Mapping[str, Any]]:
    if isinstance(floorplan, Mapping):
        raw = floorplan.get("corridors") or floorplan.get("routing_corridors") or ()
    else:
        raw = getattr(floorplan, "corridors", ()) or getattr(floorplan, "routing_corridors", ())
    return [row if isinstance(row, Mapping) else _object_row(row) for row in raw or ()]


def _system_edge_score(floorplan: Mapping[str, Any] | object, boxes: Mapping[str, BBox], bbox: BBox | None) -> float:
    edges = _system_edges(floorplan)
    if not edges or not boxes or bbox is None:
        return 100.0
    scale = max(_bbox_width(bbox) + _bbox_height(bbox), 1.0)
    weighted = 0.0
    total_weight = 0.0
    for row in edges:
        source = str(row.get("source", "") or "")
        target = str(row.get("target", "") or "")
        if source not in boxes or target not in boxes:
            continue
        weight = max(float(row.get("weight", 1.0) or 1.0), 0.0)
        sx, sy = _bbox_center(boxes[source])
        tx, ty = _bbox_center(boxes[target])
        hpwl = abs(sx - tx) + abs(sy - ty)
        weighted += weight * hpwl
        total_weight += weight
    if total_weight <= 0:
        return 100.0
    normalized = weighted / total_weight / scale
    return _clip100(100.0 * (1.0 - min(normalized, 1.0)))


def _system_corridor_score(floorplan: Mapping[str, Any] | object, bbox: BBox | None) -> float:
    corridors = _system_corridors(floorplan)
    if not corridors or bbox is None:
        return 100.0
    scores = []
    for row in corridors:
        box = _bbox4(row.get("bbox_um") or row.get("bbox"))
        if box is None:
            continue
        escape = max(_bbox_escape_by_side(box, bbox).values() or (0.0,))
        inside_score = _score_escape(escape, max(_bbox_width(bbox), _bbox_height(bbox), 1.0) * 0.05)
        width = _bbox_width(box)
        height = _bbox_height(box)
        aspect = max(width / max(height, 1e-12), height / max(width, 1e-12))
        axis_score = 100.0 if aspect >= 2.0 else 75.0
        scores.append(0.7 * inside_score + 0.3 * axis_score)
    return sum(scores) / len(scores) if scores else 100.0


def _bbox_center(box: BBox) -> tuple[float, float]:
    return ((box[0] + box[2]) * 0.5, (box[1] + box[3]) * 0.5)


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def _union_area(boxes: Sequence[BBox]) -> float:
    events: list[tuple[float, float, float, int]] = []
    for box in boxes:
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        events.append((float(box[0]), float(box[1]), float(box[3]), 1))
        events.append((float(box[2]), float(box[1]), float(box[3]), -1))
    if not events:
        return 0.0
    events.sort()
    active: list[tuple[float, float]] = []
    last_x = events[0][0]
    area = 0.0
    idx = 0
    while idx < len(events):
        x = events[idx][0]
        area += max(0.0, x - last_x) * _covered_y(active)
        while idx < len(events) and events[idx][0] == x:
            _, y0, y1, typ = events[idx]
            interval = (y0, y1)
            if typ > 0:
                active.append(interval)
            else:
                try:
                    active.remove(interval)
                except ValueError:
                    pass
            idx += 1
        last_x = x
    return area


def _covered_y(intervals: Sequence[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    merged: list[list[float]] = []
    for y0, y1 in sorted(intervals):
        if not merged or y0 > merged[-1][1]:
            merged.append([float(y0), float(y1)])
        else:
            merged[-1][1] = max(merged[-1][1], float(y1))
    return sum(y1 - y0 for y0, y1 in merged)


def _count_by(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(field, "") or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _weighted_score(parts: Mapping[str, tuple[float, float]]) -> float:
    total_weight = sum(max(weight, 0.0) for _, weight in parts.values())
    if total_weight <= 0:
        return 0.0
    return _clip100(sum(_clip100(score) * max(weight, 0.0) for score, weight in parts.values()) / total_weight)


def _deductions(scores: Mapping[str, float]) -> list[dict[str, Any]]:
    rows = []
    for name, score in sorted(scores.items(), key=lambda item: item[1]):
        if score < 80.0:
            rows.append({"metric": name, "score": _round_score(score), "severity": "major" if score < 60 else "minor"})
    return rows


def _grade(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def _round_bbox(box: BBox | None) -> list[float] | None:
    if box is None:
        return None
    return [round(float(value), 6) for value in box]


def _round_float(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _round_score(value: float) -> float:
    return round(_clip100(value), 3)


def _clip100(value: float) -> float:
    return max(0.0, min(100.0, float(value)))
