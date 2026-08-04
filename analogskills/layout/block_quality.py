"""Block-level layout quality checks for analog macro generation.

The checks here intentionally sit above DRC/LVS.  Their job is to catch layout
artifacts that make a block look unlike a human macro even when the electrical
or signoff reports are acceptable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from analogskills.env import CANONICAL_ENV_PREFIX, canonical_env_view


BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class BlockLayoutQualityConfig:
    """Policy for block-level visual/layout-quality validation."""

    allow_signoff_markers: bool = False
    marker_detach_halo_um: float = 0.0
    min_electrical_shape_count: int = 1
    max_route_escape_um: float | None = None
    max_route_to_rect_bbox_area_ratio: float | None = None
    max_non_electrical_rect_count: int | None = None
    top_route_net_count: int = 8


def analyze_block_layout_quality(
    oa_layout: Mapping[str, Any] | object,
    *,
    config: BlockLayoutQualityConfig | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a machine-readable block-level layout quality report.

    The report states constraints, objectives, actual values, and pass/fail
    issues.  It does not recommend edits; higher-level agents should inspect
    the observations and decide how to change the DSL/SMT problem.
    """

    cfg = _config(config)
    rects = _rows(oa_layout, "rects")
    paths = _rows(oa_layout, "paths")
    pins = _rows(oa_layout, "pins")

    marker_rects = [row for row in rects if _is_signoff_marker(row)]
    layout_rects = [row for row in rects if not _is_signoff_marker(row)]
    electrical_rects = [row for row in layout_rects if _has_net(row)]
    electrical_paths = [row for row in paths if _has_net(row)]
    electrical_pins = [row for row in pins if _has_net(row)]
    unnetted_rects = [row for row in layout_rects if not _has_net(row)]
    access_rects = [row for row in layout_rects if _rect_access_kind(row) != "none"]
    unnetted_access_rects = [row for row in unnetted_rects if _rect_access_kind(row) != "none"]
    non_electrical_rects = [row for row in unnetted_rects if _rect_access_kind(row) == "none"]
    non_access_electrical_rects = [row for row in electrical_rects if _rect_access_kind(row) == "none"]

    electrical_boxes = [_rect_bbox(row) for row in electrical_rects]
    electrical_boxes += [_path_bbox(row) for row in electrical_paths]
    electrical_boxes += [_rect_bbox(row) for row in electrical_pins]
    electrical_boxes = [box for box in electrical_boxes if box is not None]
    block_shape_boxes = [box for box in (_rect_bbox(row) for row in layout_rects) if box is not None]
    block_shape_boxes += [_path_bbox(row) for row in electrical_paths]
    block_shape_boxes += [_rect_bbox(row) for row in electrical_pins]
    block_shape_boxes = [box for box in block_shape_boxes if box is not None]
    electrical_rect_boxes = [box for box in (_rect_bbox(row) for row in electrical_rects) if box is not None]
    access_boxes = [box for box in (_rect_bbox(row) for row in access_rects) if box is not None]
    unnetted_access_boxes = [box for box in (_rect_bbox(row) for row in unnetted_access_rects) if box is not None]
    route_boxes = [box for box in (_path_bbox(row) for row in electrical_paths) if box is not None]
    pin_boxes = [box for box in (_rect_bbox(row) for row in electrical_pins) if box is not None]
    non_electrical_boxes = [box for box in (_rect_bbox(row) for row in non_electrical_rects) if box is not None]
    marker_boxes = [box for box in (_rect_bbox(row) for row in marker_rects) if box is not None]

    block_shape_bbox = _bbox_union(block_shape_boxes)
    electrical_bbox = _bbox_union(electrical_boxes)
    electrical_rect_bbox = _bbox_union(electrical_rect_boxes)
    access_bbox = _bbox_union(access_boxes)
    unnetted_access_bbox = _bbox_union(unnetted_access_boxes)
    route_bbox = _bbox_union(route_boxes)
    pin_bbox = _bbox_union(pin_boxes)
    non_electrical_bbox = _bbox_union(non_electrical_boxes)
    route_reference_bbox = electrical_rect_bbox or access_bbox or electrical_bbox
    route_escape_by_side = _bbox_escape_by_side(route_bbox, route_reference_bbox)
    route_escape_max_um = max(route_escape_by_side.values()) if route_escape_by_side else 0.0
    route_to_rect_ratio = _safe_ratio(_bbox_area(route_bbox), _bbox_area(electrical_rect_bbox))
    final_bbox_area = _bbox_area(electrical_bbox)
    block_shape_bbox_area = _bbox_area(block_shape_bbox)
    block_to_electrical_bbox_ratio = _safe_ratio(block_shape_bbox_area, final_bbox_area)
    electrical_union_area = _union_area(electrical_boxes)
    block_shape_union_area = _union_area(block_shape_boxes)
    electrical_bbox_occupancy = _safe_ratio(electrical_union_area, final_bbox_area)
    block_shape_bbox_occupancy = _safe_ratio(block_shape_union_area, block_shape_bbox_area)
    marker_detached = []
    if electrical_bbox is not None:
        expanded = _expand(electrical_bbox, cfg.marker_detach_halo_um)
        for row, box in zip(marker_rects, marker_boxes):
            if not _bbox_overlaps(expanded, box):
                marker_detached.append(_marker_observation(row, box))
    else:
        marker_detached = [_marker_observation(row, box) for row, box in zip(marker_rects, marker_boxes)]

    constraints = {
        "no_signoff_marker_geometry_in_block_layout": not cfg.allow_signoff_markers,
        "min_electrical_shape_count": cfg.min_electrical_shape_count,
        "max_route_escape_um": cfg.max_route_escape_um,
        "max_route_to_rect_bbox_area_ratio": cfg.max_route_to_rect_bbox_area_ratio,
        "max_non_electrical_rect_count": cfg.max_non_electrical_rect_count,
    }
    actual = {
        "electrical_shape_count": len(electrical_boxes),
        "electrical_rect_count": len(electrical_rects),
        "access_rect_count": len(access_rects),
        "unnetted_access_rect_count": len(unnetted_access_rects),
        "non_access_electrical_rect_count": len(non_access_electrical_rects),
        "route_path_count": len(electrical_paths),
        "pin_count": len(electrical_pins),
        "non_electrical_rect_count": len(non_electrical_rects),
        "signoff_marker_rect_count": len(marker_rects),
        "detached_signoff_marker_count": len(marker_detached),
        "block_shape_bbox_um": _round_bbox(block_shape_bbox),
        "electrical_bbox_um": _round_bbox(electrical_bbox),
        "electrical_rect_bbox_um": _round_bbox(electrical_rect_bbox),
        "access_rect_bbox_um": _round_bbox(access_bbox),
        "unnetted_access_bbox_um": _round_bbox(unnetted_access_bbox),
        "route_bbox_um": _round_bbox(route_bbox),
        "pin_bbox_um": _round_bbox(pin_bbox),
        "non_electrical_bbox_um": _round_bbox(non_electrical_bbox),
        "block_shape_bbox_area_um2": _round_float(block_shape_bbox_area),
        "electrical_bbox_area_um2": _round_float(final_bbox_area),
        "block_shape_union_area_um2": _round_float(block_shape_union_area),
        "electrical_union_area_um2": _round_float(electrical_union_area),
        "block_shape_bbox_occupancy_ratio": _round_float(block_shape_bbox_occupancy),
        "electrical_bbox_occupancy_ratio": _round_float(electrical_bbox_occupancy),
        "block_to_electrical_bbox_area_ratio": _round_float(block_to_electrical_bbox_ratio),
        "route_bbox_area_um2": _round_float(_bbox_area(route_bbox)),
        "route_to_rect_bbox_area_ratio": _round_float(route_to_rect_ratio),
        "route_escape_by_side_um": {key: _round_float(value) for key, value in route_escape_by_side.items()},
        "route_escape_max_um": _round_float(route_escape_max_um),
        "rect_count_by_category": _category_counts(rects),
        "path_count_by_layer": _count_by(electrical_paths, "layer"),
        "route_bbox_by_net_um": _top_route_bboxes_by_net(electrical_paths, limit=cfg.top_route_net_count),
        "signoff_marker_bboxes_um": [_round_bbox(box) for box in marker_boxes],
    }

    issues: list[dict[str, Any]] = []
    if len(electrical_boxes) < cfg.min_electrical_shape_count:
        issues.append(
            {
                "kind": "missing_electrical_geometry",
                "constraint": "min_electrical_shape_count",
                "expected": cfg.min_electrical_shape_count,
                "actual": len(electrical_boxes),
            }
        )
    if marker_rects and not cfg.allow_signoff_markers:
        issues.append(
            {
                "kind": "signoff_marker_geometry_in_block_layout",
                "constraint": "no_signoff_marker_geometry_in_block_layout",
                "expected": 0,
                "actual": len(marker_rects),
            }
        )
    if cfg.max_non_electrical_rect_count is not None and len(non_electrical_rects) > cfg.max_non_electrical_rect_count:
        issues.append(
            {
                "kind": "non_electrical_geometry_in_block_layout",
                "constraint": "max_non_electrical_rect_count",
                "expected": cfg.max_non_electrical_rect_count,
                "actual": len(non_electrical_rects),
            }
        )
    if cfg.max_route_escape_um is not None and route_escape_max_um > cfg.max_route_escape_um:
        issues.append(
            {
                "kind": "route_envelope_escape",
                "constraint": "max_route_escape_um",
                "expected": cfg.max_route_escape_um,
                "actual": _round_float(route_escape_max_um),
                "actual_by_side_um": actual["route_escape_by_side_um"],
            }
        )
    if (
        cfg.max_route_to_rect_bbox_area_ratio is not None
        and route_to_rect_ratio is not None
        and route_to_rect_ratio > cfg.max_route_to_rect_bbox_area_ratio
    ):
        issues.append(
            {
                "kind": "route_bbox_area_ratio",
                "constraint": "max_route_to_rect_bbox_area_ratio",
                "expected": cfg.max_route_to_rect_bbox_area_ratio,
                "actual": _round_float(route_to_rect_ratio),
            }
        )
    for marker in marker_detached:
        issues.append(
            {
                "kind": "detached_signoff_marker",
                "constraint": "marker_must_not_be_detached_from_electrical_bbox",
                "actual": marker,
            }
        )

    return {
        "schema": "analogskills.block_layout_quality/v1",
        "passed": not issues,
        "constraints": constraints,
        "objectives": {
            "visual_block_layout_should_not_contain_detached_signoff_artifacts": "satisfy",
            "block_geometry_should_be_driven_by_devices_routes_and_pins": "satisfy",
            "route_envelope_should_remain_close_to_device_access_envelope": "minimize",
            "non_electrical_geometry_should_not_drive_block_bbox": "minimize",
            "electrical_bbox_occupancy_should_be_high": "maximize",
        },
        "actual": actual,
        "issues": issues,
    }


def block_layout_quality_config_from_mapping(
    values: Mapping[str, object] | None = None,
    *,
    prefix: str = "SKILLS_Z_BLOCK_QUALITY_",
) -> BlockLayoutQualityConfig:
    """Build block-quality policy from env-style key/value pairs.

    This keeps block-specific quality gates configurable by run scripts and CI
    without baking design thresholds into the solver or layout generators.
    """

    values = values or {}
    return BlockLayoutQualityConfig(
        allow_signoff_markers=_mapping_bool(values, f"{prefix}ALLOW_SIGNOFF_MARKERS", False),
        marker_detach_halo_um=_mapping_float(values, f"{prefix}MARKER_DETACH_HALO_UM", 0.0),
        min_electrical_shape_count=_mapping_int(values, f"{prefix}MIN_ELECTRICAL_SHAPE_COUNT", 1),
        max_route_escape_um=_mapping_optional_float(values, f"{prefix}MAX_ROUTE_ESCAPE_UM"),
        max_route_to_rect_bbox_area_ratio=_mapping_optional_float(
            values, f"{prefix}MAX_ROUTE_TO_RECT_BBOX_AREA_RATIO"
        ),
        max_non_electrical_rect_count=_mapping_optional_int(values, f"{prefix}MAX_NON_ELECTRICAL_RECT_COUNT"),
        top_route_net_count=max(1, _mapping_int(values, f"{prefix}TOP_ROUTE_NET_COUNT", 8)),
    )


def block_layout_quality_config_from_env(*, prefix: str = f"{CANONICAL_ENV_PREFIX}BLOCK_QUALITY_") -> BlockLayoutQualityConfig:
    """Build block-quality policy from process environment variables."""

    return block_layout_quality_config_from_mapping(canonical_env_view(os.environ), prefix=prefix)


def _config(config: BlockLayoutQualityConfig | Mapping[str, Any] | None) -> BlockLayoutQualityConfig:
    if config is None:
        return BlockLayoutQualityConfig()
    if isinstance(config, BlockLayoutQualityConfig):
        return config
    return BlockLayoutQualityConfig(
        allow_signoff_markers=bool(config.get("allow_signoff_markers", False)),
        marker_detach_halo_um=float(config.get("marker_detach_halo_um", 0.0) or 0.0),
        min_electrical_shape_count=int(config.get("min_electrical_shape_count", 1) or 1),
        max_route_escape_um=_optional_float(config.get("max_route_escape_um")),
        max_route_to_rect_bbox_area_ratio=_optional_float(config.get("max_route_to_rect_bbox_area_ratio")),
        max_non_electrical_rect_count=_optional_int(config.get("max_non_electrical_rect_count")),
        top_route_net_count=max(1, int(config.get("top_route_net_count", 8) or 8)),
    )


def _rows(oa_layout: Mapping[str, Any] | object, field: str) -> list[Mapping[str, Any]]:
    if isinstance(oa_layout, Mapping):
        raw = oa_layout.get(field, ()) or ()
    else:
        raw = getattr(oa_layout, field, ()) or ()
    rows: list[Mapping[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            rows.append(item)
        else:
            rows.append(_object_row(item))
    return rows


def _object_row(item: object) -> Mapping[str, Any]:
    row: dict[str, Any] = {}
    for name in ("layer", "purpose", "bbox", "net", "metadata", "points", "width"):
        if hasattr(item, name):
            row[name] = getattr(item, name)
    return row


def _has_net(row: Mapping[str, Any]) -> bool:
    return bool(str(row.get("net", "") or "").strip())


def _is_signoff_marker(row: Mapping[str, Any]) -> bool:
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


def _rect_access_kind(row: Mapping[str, Any]) -> str:
    metadata = row.get("metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return "none"
    kind = str(metadata.get("kind", metadata.get("access_kind", "")) or "").strip().lower()
    if kind in {"structured_terminal_access", "structured_unit_array_local_bus"}:
        return kind
    if kind.startswith("crn28_mos_"):
        return "generated_mos_access"
    if any(token in kind for token in ("access", "landing", "drop", "bus", "via_stack")):
        return kind or "access"
    if metadata.get("access_contract") or metadata.get("access_role") or metadata.get("terminal"):
        return "metadata_terminal_access"
    return "none"


def _rect_bbox(row: Mapping[str, Any]) -> BBox | None:
    return _bbox4(row.get("bbox"))


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
    xs = [x for x, _ in parsed]
    ys = [y for _, y in parsed]
    return (min(xs) - half, min(ys) - half, max(xs) + half, max(ys) + half)


def _bbox4(value: object) -> BBox | None:
    try:
        raw = tuple(value or ())  # type: ignore[arg-type]
    except TypeError:
        return None
    if len(raw) != 4:
        return None
    x0, y0, x1, y1 = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _bbox_union(boxes: Sequence[BBox]) -> BBox | None:
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _bbox_overlaps(a: BBox, b: BBox) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _bbox_area(box: BBox | None) -> float | None:
    if box is None:
        return None
    return max(0.0, float(box[2]) - float(box[0])) * max(0.0, float(box[3]) - float(box[1]))


def _bbox_escape_by_side(box: BBox | None, reference: BBox | None) -> dict[str, float]:
    if box is None or reference is None:
        return {}
    return {
        "left": max(0.0, reference[0] - box[0]),
        "bottom": max(0.0, reference[1] - box[1]),
        "right": max(0.0, box[2] - reference[2]),
        "top": max(0.0, box[3] - reference[3]),
    }


def _expand(box: BBox, halo: float) -> BBox:
    h = max(float(halo), 0.0)
    return (box[0] - h, box[1] - h, box[2] + h, box[3] + h)


def _marker_observation(row: Mapping[str, Any], box: BBox) -> dict[str, Any]:
    metadata = row.get("metadata", {}) or {}
    return {
        "layer": row.get("layer", ""),
        "purpose": row.get("purpose", ""),
        "bbox_um": _round_bbox(box),
        "marker_name": metadata.get("marker_name", "") if isinstance(metadata, Mapping) else "",
        "marker_role": metadata.get("marker_role", "") if isinstance(metadata, Mapping) else "",
    }


def _round_bbox(box: BBox | None) -> list[float] | None:
    if box is None:
        return None
    return [round(float(value), 6) for value in box]


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def _round_float(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _category_counts(rects: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rects:
        category = _rect_category(row)
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


def _rect_category(row: Mapping[str, Any]) -> str:
    if _is_signoff_marker(row):
        return "signoff_marker"
    access_kind = _rect_access_kind(row)
    if access_kind != "none" and not _has_net(row):
        return "unnetted_access"
    if access_kind != "none":
        return "access"
    if _has_net(row):
        return "electrical_rect"
    return "non_electrical_rect"


def _count_by(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(field, "") or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _top_route_bboxes_by_net(paths: Sequence[Mapping[str, Any]], *, limit: int) -> dict[str, dict[str, Any]]:
    boxes_by_net: dict[str, list[BBox]] = {}
    count_by_net: dict[str, int] = {}
    for row in paths:
        net = str(row.get("net", "") or "")
        box = _path_bbox(row)
        if not net or box is None:
            continue
        boxes_by_net.setdefault(net, []).append(box)
        count_by_net[net] = count_by_net.get(net, 0) + 1
    rows: list[tuple[str, BBox, float, int]] = []
    for net, boxes in boxes_by_net.items():
        bbox = _bbox_union(boxes)
        if bbox is None:
            continue
        rows.append((net, bbox, _bbox_area(bbox) or 0.0, count_by_net.get(net, 0)))
    rows.sort(key=lambda item: (-item[2], item[0]))
    return {
        net: {"bbox_um": _round_bbox(bbox), "bbox_area_um2": _round_float(area), "path_count": count}
        for net, bbox, area, count in rows[: max(1, int(limit))]
    }


def _union_area(boxes: Sequence[BBox]) -> float:
    events: list[tuple[float, float, float, int]] = []
    for box in boxes:
        x0, y0, x1, y1 = box
        if x1 <= x0 or y1 <= y0:
            continue
        events.append((float(x0), float(y0), float(y1), 1))
        events.append((float(x1), float(y0), float(y1), -1))
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


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping_bool(values: Mapping[str, object], key: str, default: bool) -> bool:
    value = values.get(key)
    if value is None:
        return bool(default)
    return str(value).strip().lower() not in {"", "0", "false", "off", "no", "none"}


def _mapping_float(values: Mapping[str, object], key: str, default: float) -> float:
    value = values.get(key)
    if value is None:
        return float(default)
    parsed = _optional_float(value)
    return float(default) if parsed is None else parsed


def _mapping_int(values: Mapping[str, object], key: str, default: int) -> int:
    value = values.get(key)
    if value is None:
        return int(default)
    parsed = _optional_int(value)
    return int(default) if parsed is None else parsed


def _mapping_optional_float(values: Mapping[str, object], key: str) -> float | None:
    return _optional_float(values.get(key))


def _mapping_optional_int(values: Mapping[str, object], key: str) -> int | None:
    return _optional_int(values.get(key))
