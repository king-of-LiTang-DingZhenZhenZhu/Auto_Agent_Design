"""Backend-neutral layout intermediate representation."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

from analogskills.pdk import DesignRuleDeck, PdkConfig

BBox = tuple[float, float, float, float]
Point = tuple[float, float]


@dataclass(frozen=True)
class LayoutCellRef:
    lib: str
    cell: str
    view: str = "layout"
    view_type: str = "maskLayout"


@dataclass(frozen=True)
class LayoutLayerRef:
    layer: str
    purpose: str = "drawing"


@dataclass(frozen=True)
class LayoutRect:
    layer: str
    bbox: BBox
    net: str = ""
    purpose: str = "drawing"
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LayoutPath:
    layer: str
    points: tuple[Point, ...]
    width: float
    net: str = ""
    purpose: str = "drawing"
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LayoutVia:
    via_def: str
    xy: Point
    net: str = ""
    rows: int = 1
    cols: int = 1
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LayoutPin:
    name: str
    net: str
    direction: str = "inputOutput"
    layer: str = "M1"
    bbox: BBox | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LayoutLabel:
    layer: str
    text: str
    xy: Point
    purpose: str = "label"
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LayoutInstance:
    name: str
    master: LayoutCellRef
    xy: Point = (0.0, 0.0)
    orient: str = "R0"
    connections: dict[str, str] = field(default_factory=dict)
    params: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LayoutPlan:
    cell: LayoutCellRef
    nets: tuple[str, ...] = ()
    pins: tuple[LayoutPin, ...] = ()
    instances: tuple[LayoutInstance, ...] = ()
    rects: tuple[LayoutRect, ...] = ()
    paths: tuple[LayoutPath, ...] = ()
    vias: tuple[LayoutVia, ...] = ()
    labels: tuple[LayoutLabel, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


def layout_plan_nets(plan: LayoutPlan) -> tuple[str, ...]:
    """Return explicit and inferred nets in stable order."""

    nets = list(plan.nets)
    nets.extend(pin.net for pin in plan.pins)
    nets.extend(rect.net for rect in plan.rects)
    nets.extend(path.net for path in plan.paths)
    nets.extend(via.net for via in plan.vias)
    for inst in plan.instances:
        nets.extend(inst.connections.values())
    return tuple(dict.fromkeys(net for net in nets if net))


def merge_layout_plans(
    *plans: LayoutPlan,
    cell: LayoutCellRef | None = None,
    grid: DesignRuleDeck | PdkConfig | int | None = None,
    snap_to_grid: bool = True,
) -> LayoutPlan:
    """Merge reviewed layout proposals without adding physical decisions."""

    if not plans:
        raise ValueError("at least one layout plan is required")
    target_cell = cell or plans[0].cell
    merged = LayoutPlan(
        target_cell,
        nets=tuple(dict.fromkeys(net for plan in plans for net in layout_plan_nets(plan))),
        pins=tuple(pin for plan in plans for pin in plan.pins),
        instances=tuple(inst for plan in plans for inst in plan.instances),
        rects=tuple(rect for plan in plans for rect in plan.rects),
        paths=tuple(path for plan in plans for path in plan.paths),
        vias=tuple(via for plan in plans for via in plan.vias),
        labels=tuple(label for plan in plans for label in plan.labels),
        metadata={key: value for plan in plans for key, value in plan.metadata.items()},
    )
    return snap_layout_plan_to_grid(merged, grid) if grid is not None and snap_to_grid else merged


def layout_plan_bbox(plan: LayoutPlan) -> BBox | None:
    """Return the bbox covering geometry and pins with physical extents."""

    boxes: list[BBox] = [rect.bbox for rect in plan.rects]
    boxes.extend(pin.bbox for pin in plan.pins if pin.bbox is not None)
    boxes.extend(_path_bbox(path) for path in plan.paths)
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def compact_path_points(points: Sequence[Point], *, tol_um: float = 1e-12) -> tuple[Point, ...]:
    """Remove repeated and collinear points from a routed polyline."""

    try:
        normalized = tuple((float(x), float(y)) for x, y in points)
    except (TypeError, ValueError):
        return tuple(points)
    if len(normalized) <= 1:
        return normalized
    deduped: list[Point] = []
    for point in normalized:
        if not deduped:
            deduped.append(point)
            continue
        prev = deduped[-1]
        if abs(prev[0] - point[0]) <= tol_um and abs(prev[1] - point[1]) <= tol_um:
            continue
        deduped.append(point)
    if len(deduped) <= 2:
        return tuple(deduped)
    compacted = [deduped[0]]
    for idx in range(1, len(deduped) - 1):
        prev = compacted[-1]
        current = deduped[idx]
        nxt = deduped[idx + 1]
        prev_dx = current[0] - prev[0]
        prev_dy = current[1] - prev[1]
        next_dx = nxt[0] - current[0]
        next_dy = nxt[1] - current[1]
        if abs(prev_dx) <= tol_um and abs(next_dx) <= tol_um:
            continue
        if abs(prev_dy) <= tol_um and abs(next_dy) <= tol_um:
            continue
        compacted.append(current)
    compacted.append(deduped[-1])
    return tuple(compacted)


def sanitize_layout_path(path: LayoutPath, *, drop_degenerate: bool = True, tol_um: float = 1e-12) -> LayoutPath | None:
    """Normalize a path while optionally dropping degenerate geometry."""

    compacted = compact_path_points(path.points, tol_um=tol_um)
    if len(compacted) < 2:
        return None if drop_degenerate else replace(path, points=compacted)
    if compacted == path.points:
        return path
    return replace(path, points=compacted)


def sanitize_layout_plan(plan: LayoutPlan, *, drop_degenerate_paths: bool = True, tol_um: float = 1e-12) -> LayoutPlan:
    """Return a layout plan with normalized path geometry."""

    sanitized_paths: list[LayoutPath] = []
    dropped_paths: list[dict[str, object]] = []
    for path in plan.paths:
        sanitized = sanitize_layout_path(path, drop_degenerate=drop_degenerate_paths, tol_um=tol_um)
        if sanitized is None:
            dropped_paths.append({"net": path.net, "layer": path.layer})
            continue
        sanitized_paths.append(sanitized)
    metadata = dict(plan.metadata)
    if dropped_paths:
        metadata["sanitized_paths_dropped"] = tuple(dropped_paths)
    return replace(plan, paths=tuple(sanitized_paths), metadata=metadata)


def snap_layout_plan_to_grid(plan: LayoutPlan, grid: DesignRuleDeck | PdkConfig | int, *, bbox_mode: str = "outward") -> LayoutPlan:
    rules = _grid_rules(grid)
    snapped = LayoutPlan(
        plan.cell,
        nets=plan.nets,
        pins=tuple(_snap_layout_pin(pin, rules, bbox_mode) for pin in plan.pins),
        instances=tuple(replace(inst, xy=rules.snap_point_um(inst.xy)) for inst in plan.instances),
        rects=tuple(replace(rect, bbox=rules.snap_bbox_um(rect.bbox, mode=bbox_mode)) for rect in plan.rects),
        paths=tuple(_snap_layout_path(path, rules) for path in plan.paths),
        vias=tuple(replace(via, xy=rules.snap_point_um(via.xy)) for via in plan.vias),
        labels=tuple(replace(label, xy=rules.snap_point_um(label.xy)) for label in plan.labels),
        metadata=plan.metadata,
    )
    return sanitize_layout_plan(snapped)


def validate_layout_plan_grid(plan: LayoutPlan, grid: DesignRuleDeck | PdkConfig | int, *, tol_um: float = 1e-12) -> list[str]:
    rules = _grid_rules(grid)
    issues: list[str] = []
    for inst in plan.instances:
        issues.extend(_point_grid_issues(f"instance {inst.name}.xy", inst.xy, rules, tol_um))
    for rect in plan.rects:
        issues.extend(_bbox_grid_issues(f"rect {rect.layer}/{rect.net or 'no_net'}", rect.bbox, rules, tol_um))
    for pin in plan.pins:
        if pin.bbox is not None:
            issues.extend(_bbox_grid_issues(f"pin {pin.name}.bbox", pin.bbox, rules, tol_um))
    for path in plan.paths:
        if not rules.is_on_grid_um(path.width, tol_um=tol_um):
            issues.append(f"path {path.net or path.layer}.width={path.width:g}um is off-grid for {rules.grid_nm}nm grid")
        for idx, point in enumerate(path.points):
            issues.extend(_point_grid_issues(f"path {path.net or path.layer}.points[{idx}]", point, rules, tol_um))
    for via in plan.vias:
        issues.extend(_point_grid_issues(f"via {via.via_def}.xy", via.xy, rules, tol_um))
    for label in plan.labels:
        issues.extend(_point_grid_issues(f"label {label.text}.xy", label.xy, rules, tol_um))
    return issues


def layout_plan_to_dict(plan: LayoutPlan) -> dict[str, object]:
    return {
        "cell": {"lib": plan.cell.lib, "cell": plan.cell.cell, "view": plan.cell.view, "view_type": plan.cell.view_type},
        "nets": list(plan.nets),
        "pins": [
            {"name": pin.name, "net": pin.net, "direction": pin.direction, "layer": pin.layer, "bbox": _list_or_none(pin.bbox), "metadata": dict(pin.metadata)}
            for pin in plan.pins
        ],
        "instances": [
            {
                "name": inst.name,
                "master": {"lib": inst.master.lib, "cell": inst.master.cell, "view": inst.master.view, "view_type": inst.master.view_type},
                "xy": list(inst.xy),
                "orient": inst.orient,
                "connections": dict(inst.connections),
                "params": dict(inst.params),
                "metadata": dict(inst.metadata),
            }
            for inst in plan.instances
        ],
        "rects": [
            {"layer": rect.layer, "purpose": rect.purpose, "bbox": list(rect.bbox), "net": rect.net, "metadata": dict(rect.metadata)}
            for rect in plan.rects
        ],
        "paths": [
            {
                "layer": path.layer,
                "purpose": path.purpose,
                "points": [list(point) for point in path.points],
                "width": path.width,
                "net": path.net,
                "metadata": dict(path.metadata),
            }
            for path in plan.paths
        ],
        "vias": [
            {"via_def": via.via_def, "xy": list(via.xy), "net": via.net, "rows": via.rows, "cols": via.cols, "metadata": dict(via.metadata)}
            for via in plan.vias
        ],
        "labels": [
            {"layer": label.layer, "purpose": label.purpose, "text": label.text, "xy": list(label.xy), "metadata": dict(label.metadata)}
            for label in plan.labels
        ],
        "metadata": dict(plan.metadata),
    }


def layout_plan_from_dict(data: Mapping[str, object]) -> LayoutPlan:
    cell_data = dict(data["cell"])  # type: ignore[arg-type]
    return LayoutPlan(
        LayoutCellRef(str(cell_data["lib"]), str(cell_data["cell"]), str(cell_data.get("view", "layout")), str(cell_data.get("view_type", "maskLayout"))),
        nets=tuple(str(net) for net in data.get("nets", ())),  # type: ignore[union-attr]
        pins=tuple(_pin_from_dict(item) for item in data.get("pins", ())),  # type: ignore[arg-type, union-attr]
        instances=tuple(_instance_from_dict(item) for item in data.get("instances", ())),  # type: ignore[arg-type, union-attr]
        rects=tuple(_rect_from_dict(item) for item in data.get("rects", ())),  # type: ignore[arg-type, union-attr]
        paths=tuple(_path_from_dict(item) for item in data.get("paths", ())),  # type: ignore[arg-type, union-attr]
        vias=tuple(_via_from_dict(item) for item in data.get("vias", ())),  # type: ignore[arg-type, union-attr]
        labels=tuple(_label_from_dict(item) for item in data.get("labels", ())),  # type: ignore[arg-type, union-attr]
        metadata=dict(data.get("metadata", {})),  # type: ignore[arg-type]
    )


def _pin_from_dict(data: Mapping[str, object]) -> LayoutPin:
    bbox = data.get("bbox")
    return LayoutPin(
        str(data["name"]),
        str(data["net"]),
        str(data.get("direction", "inputOutput")),
        str(data.get("layer", "M1")),
        None if bbox is None else _bbox_tuple(bbox),
        dict(data.get("metadata", {})),  # type: ignore[arg-type]
    )


def _instance_from_dict(data: Mapping[str, object]) -> LayoutInstance:
    master = dict(data["master"])  # type: ignore[arg-type]
    return LayoutInstance(
        str(data["name"]),
        LayoutCellRef(str(master["lib"]), str(master["cell"]), str(master.get("view", "layout")), str(master.get("view_type", "maskLayout"))),
        _point_tuple(data.get("xy", (0.0, 0.0))),
        str(data.get("orient", "R0")),
        {str(key): str(value) for key, value in dict(data.get("connections", {})).items()},  # type: ignore[arg-type]
        dict(data.get("params", {})),  # type: ignore[arg-type]
        dict(data.get("metadata", {})),  # type: ignore[arg-type]
    )


def _rect_from_dict(data: Mapping[str, object]) -> LayoutRect:
    return LayoutRect(
        str(data["layer"]),
        _bbox_tuple(data["bbox"]),
        str(data.get("net", "")),
        str(data.get("purpose", "drawing")),
        dict(data.get("metadata", {})),  # type: ignore[arg-type]
    )


def _path_from_dict(data: Mapping[str, object]) -> LayoutPath:
    return LayoutPath(
        str(data["layer"]),
        tuple(_point_tuple(point) for point in data.get("points", ())),  # type: ignore[arg-type, union-attr]
        float(data["width"]),
        str(data.get("net", "")),
        str(data.get("purpose", "drawing")),
        dict(data.get("metadata", {})),  # type: ignore[arg-type]
    )


def _via_from_dict(data: Mapping[str, object]) -> LayoutVia:
    return LayoutVia(
        str(data["via_def"]),
        _point_tuple(data["xy"]),
        str(data.get("net", "")),
        int(data.get("rows", 1)),
        int(data.get("cols", 1)),
        dict(data.get("metadata", {})),  # type: ignore[arg-type]
    )


def _label_from_dict(data: Mapping[str, object]) -> LayoutLabel:
    return LayoutLabel(
        str(data["layer"]),
        str(data["text"]),
        _point_tuple(data["xy"]),
        str(data.get("purpose", "label")),
        dict(data.get("metadata", {})),  # type: ignore[arg-type]
    )


def _grid_rules(grid: DesignRuleDeck | PdkConfig | int) -> DesignRuleDeck:
    if isinstance(grid, PdkConfig):
        return grid.rules
    if isinstance(grid, DesignRuleDeck):
        return grid
    return DesignRuleDeck(grid_nm=int(grid))


def _snap_layout_pin(pin: LayoutPin, rules: DesignRuleDeck, bbox_mode: str) -> LayoutPin:
    if pin.bbox is None:
        return pin
    return replace(pin, bbox=rules.snap_bbox_um(pin.bbox, mode=bbox_mode))


def _snap_layout_path(path: LayoutPath, rules: DesignRuleDeck) -> LayoutPath:
    return replace(path, points=tuple(rules.snap_point_um(point) for point in path.points), width=rules.snap_dimension_um(path.width))


def _path_bbox(path: LayoutPath) -> BBox:
    half_width = path.width / 2.0
    xs = [point[0] for point in path.points]
    ys = [point[1] for point in path.points]
    return (min(xs) - half_width, min(ys) - half_width, max(xs) + half_width, max(ys) + half_width)


def _bbox_grid_issues(label: str, bbox: BBox, rules: DesignRuleDeck, tol_um: float) -> list[str]:
    if rules.bbox_is_on_grid_um(bbox, tol_um=tol_um):
        return []
    return [f"{label} bbox {bbox} is off-grid for {rules.grid_nm}nm grid"]


def _point_grid_issues(label: str, point: Point, rules: DesignRuleDeck, tol_um: float) -> list[str]:
    x, y = point
    issues = []
    if not rules.is_on_grid_um(x, tol_um=tol_um):
        issues.append(f"{label}.x={x:g}um is off-grid for {rules.grid_nm}nm grid")
    if not rules.is_on_grid_um(y, tol_um=tol_um):
        issues.append(f"{label}.y={y:g}um is off-grid for {rules.grid_nm}nm grid")
    return issues


def _point_tuple(value: object) -> Point:
    x, y = value  # type: ignore[misc]
    return (float(x), float(y))


def _bbox_tuple(value: object) -> BBox:
    x0, y0, x1, y1 = value  # type: ignore[misc]
    return (float(x0), float(y0), float(x1), float(y1))


def _list_or_none(value: tuple[float, ...] | None) -> list[float] | None:
    return None if value is None else list(value)
