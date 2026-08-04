"""Physical geometry checks for backend-neutral and OA-style layout plans."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil, isfinite, sqrt
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence

BBox = tuple[float, float, float, float]
Point = tuple[float, float]


@dataclass(frozen=True)
class PlanShape:
    layer: str
    net: str
    bbox: BBox
    kind: str
    source: str = ""


@dataclass(frozen=True)
class PlanShort:
    layer: str
    net_a: str
    net_b: str
    bbox_a: BBox
    bbox_b: BBox
    source_a: str = ""
    source_b: str = ""


@dataclass(frozen=True)
class NetOpen:
    net: str
    component_count: int
    shape_count: int
    layers: tuple[str, ...]
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class PathGeometryIssue:
    net: str
    layer: str
    path_index: int
    message: str
    severity: str = "error"
    segment_index: int | None = None


@dataclass(frozen=True)
class ShapeGeometryIssue:
    kind: str
    index: int
    layer: str
    net: str
    message: str
    severity: str = "error"
    bbox: BBox | None = None


@dataclass(frozen=True)
class ViaGeometryIssue:
    via_def: str
    net: str
    via_index: int
    message: str
    severity: str = "error"
    xy: Point | None = None
    rows: object = 1
    cols: object = 1


@dataclass(frozen=True)
class ViaLandingIssue:
    via_def: str
    net: str
    xy: Point
    layer: str
    message: str
    severity: str = "error"


def collect_plan_shapes(
    plan: Any,
    *,
    include_rects: bool = True,
    include_paths: bool = True,
    include_pins: bool = True,
    include_vias: bool = True,
    include_instance_terminals: bool = False,
    pdk: Any | None = None,
    terminal_accessor: Any | None = None,
    layers: Iterable[str] | None = None,
) -> tuple[PlanShape, ...]:
    """Collect conductive geometry from OA-style or LayoutIR-style plans.

    The helper intentionally uses duck typing so it can validate both
    ``OaWritePlan`` and ``LayoutPlan`` without either layer depending on the
    other. Shapes without a net are ignored because they cannot create a named
    net short in this precheck.
    """

    layer_filter = None if layers is None else {str(layer) for layer in layers}
    shapes: list[PlanShape] = []
    if include_rects:
        shapes.extend(
            _shape_from_bbox(
                layer=getattr(rect, "layer", ""),
                net=getattr(rect, "net", ""),
                bbox=getattr(rect, "bbox", None),
                kind="rect",
                source=f"rect[{idx}]",
                layer_filter=layer_filter,
            )
            for idx, rect in enumerate(getattr(plan, "rects", ()))
        )
    if include_pins:
        shapes.extend(
            _shape_from_bbox(
                layer=getattr(pin, "layer", ""),
                net=getattr(pin, "net", ""),
                bbox=getattr(pin, "bbox", None),
                kind="pin",
                source=f"pin[{idx}]",
                layer_filter=layer_filter,
            )
            for idx, pin in enumerate(getattr(plan, "pins", ()))
        )
    if include_paths:
        for idx, path in enumerate(getattr(plan, "paths", ())):
            layer = str(getattr(path, "layer", ""))
            net = str(getattr(path, "net", ""))
            if not layer or not net or (layer_filter is not None and layer not in layer_filter):
                continue
            try:
                points = tuple(_point_tuple(point) for point in getattr(path, "points", ()))
                width = float(getattr(path, "width", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            for seg_idx, bbox in enumerate(path_segment_bboxes(points, width)):
                shapes.append(PlanShape(layer, net, bbox, "path", f"path[{idx}].segment[{seg_idx}]"))
    if include_vias and pdk is not None:
        for idx, via in enumerate(getattr(plan, "vias", ())):
            net = str(getattr(via, "net", ""))
            if not net:
                continue
            for layer, bbox in via_landing_bboxes(via, pdk):
                if layer_filter is not None and layer not in layer_filter:
                    continue
                shapes.append(PlanShape(layer, net, bbox, "via", f"via[{idx}]"))
    if include_instance_terminals and pdk is not None:
        shapes.extend(_instance_terminal_shapes(plan, pdk, layer_filter=layer_filter, terminal_accessor=terminal_accessor))
    return tuple(shape for shape in shapes if shape.layer and shape.net and _bbox_has_area(shape.bbox))


def detect_plan_shape_shorts(
    plan: Any,
    *,
    ignore_nets: Iterable[str] = (),
    include_touching: bool = True,
    include_via_landings: bool = False,
    include_instance_terminals: bool = False,
    pdk: Any | None = None,
    terminal_accessor: Any | None = None,
    layers: Iterable[str] | None = None,
) -> tuple[PlanShort, ...]:
    """Detect same-layer geometry contacts between different named nets."""

    ignored = {str(net) for net in ignore_nets}
    shape_pdk = pdk if (include_via_landings or include_instance_terminals) else None
    shapes = tuple(
        shape
        for shape in collect_plan_shapes(
            plan,
            include_pins=False,
            pdk=shape_pdk,
            layers=layers,
            include_instance_terminals=include_instance_terminals,
            terminal_accessor=terminal_accessor,
        )
        if shape.net not in ignored
    )
    shorts: list[PlanShort] = []
    seen: set[tuple[str, str, str, BBox, BBox]] = set()
    for idx, a in enumerate(shapes):
        for b in shapes[idx + 1:]:
            if a.layer != b.layer or a.net == b.net:
                continue
            if not bbox_overlaps(a.bbox, b.bbox, include_touching=include_touching):
                continue
            net_a, net_b = sorted((a.net, b.net))
            bbox_a, bbox_b = (a.bbox, b.bbox) if a.net == net_a else (b.bbox, a.bbox)
            key = (a.layer, net_a, net_b, bbox_a, bbox_b)
            if key in seen:
                continue
            seen.add(key)
            source_a, source_b = (a.source, b.source) if a.net == net_a else (b.source, a.source)
            shorts.append(PlanShort(a.layer, net_a, net_b, bbox_a, bbox_b, source_a, source_b))
    return tuple(shorts)


def detect_plan_shape_geometry_issues(plan: Any, *, layers: Iterable[str] | None = None) -> tuple[ShapeGeometryIssue, ...]:
    """Detect invalid rect/pin geometry before connectivity checks consume it."""

    layer_filter = None if layers is None else {str(layer) for layer in layers}
    issues: list[ShapeGeometryIssue] = []
    for kind, items in (("rect", getattr(plan, "rects", ())), ("pin", getattr(plan, "pins", ()))):
        for idx, item in enumerate(items):
            layer = str(getattr(item, "layer", "") or "")
            net = str(getattr(item, "net", "") or "")
            if layer_filter is not None and layer and layer not in layer_filter:
                continue
            bbox_obj = getattr(item, "bbox", None)
            bbox = _try_bbox_tuple(bbox_obj)
            label = _shape_issue_label(kind, idx, layer, net)
            if not layer:
                issues.append(ShapeGeometryIssue(kind, idx, layer, net, f"{label} missing layer", bbox=bbox))
            if kind == "pin" and not net:
                issues.append(ShapeGeometryIssue(kind, idx, layer, net, f"{label} missing net attachment", bbox=bbox))
            if bbox_obj is None:
                issues.append(ShapeGeometryIssue(kind, idx, layer, net, f"{label} missing bbox"))
                continue
            if bbox is None:
                issues.append(ShapeGeometryIssue(kind, idx, layer, net, f"{label} has invalid bbox {bbox_obj!r}"))
                continue
            if not _bbox_coords_are_finite(bbox):
                issues.append(ShapeGeometryIssue(kind, idx, layer, net, f"{label} has non-finite bbox {bbox_obj!r}", bbox=bbox))
            elif not _bbox_has_positive_area(bbox):
                issues.append(ShapeGeometryIssue(kind, idx, layer, net, f"{label} has non-positive bbox area {bbox}", bbox=bbox))
    return tuple(issues)


def detect_plan_path_geometry_issues(plan: Any, *, layers: Iterable[str] | None = None) -> tuple[PathGeometryIssue, ...]:
    """Detect paths that cannot represent robust routed geometry."""

    layer_filter = None if layers is None else {str(layer) for layer in layers}
    issues: list[PathGeometryIssue] = []
    for idx, path in enumerate(getattr(plan, "paths", ())):
        net = str(getattr(path, "net", "") or "")
        layer = str(getattr(path, "layer", "") or "")
        if layer_filter is not None and layer and layer not in layer_filter:
            continue
        display_net = net or "<unnamed>"
        display_layer = layer or "<unknown>"
        try:
            width = float(getattr(path, "width", 0.0) or 0.0)
        except (TypeError, ValueError):
            width = 0.0
        try:
            points = tuple(_point_tuple(point) for point in getattr(path, "points", ()))
        except ValueError as exc:
            issues.append(PathGeometryIssue(net, layer, idx, f"net {display_net} has invalid path point: {exc}"))
            continue
        if not net:
            issues.append(PathGeometryIssue(net, layer, idx, "path missing net attachment"))
        if not layer:
            issues.append(PathGeometryIssue(net, layer, idx, f"net {display_net} path missing route layer"))
        if width <= 0.0:
            issues.append(PathGeometryIssue(net, layer, idx, f"net {display_net} path width {width:.4g}um is non-positive on {display_layer}"))
        if len(points) < 2:
            issues.append(PathGeometryIssue(net, layer, idx, f"net {display_net} has open or degenerate path"))
            continue
        for segment_idx, (left, right) in enumerate(zip(points, points[1:])):
            if abs(left[0] - right[0]) <= 1e-12 and abs(left[1] - right[1]) <= 1e-12:
                issues.append(
                    PathGeometryIssue(
                        net,
                        layer,
                        idx,
                        f"net {display_net} has zero-length path segment on {display_layer}",
                        segment_index=segment_idx,
                    )
                )
    return tuple(issues)


def detect_plan_via_geometry_issues(plan: Any, *, pdk: Any | None = None, layers: Iterable[str] | None = None) -> tuple[ViaGeometryIssue, ...]:
    """Detect invalid via attributes before landing/enclosure checks run."""

    layer_filter = None if layers is None else {str(layer) for layer in layers}
    issues: list[ViaGeometryIssue] = []
    for idx, via in enumerate(getattr(plan, "vias", ())):
        via_def = str(getattr(via, "via_def", "") or "")
        net = str(getattr(via, "net", "") or "")
        display_via = via_def or "<unknown>"
        display_net = net or "<unnamed>"
        rows = getattr(via, "rows", 1)
        cols = getattr(via, "cols", 1)
        try:
            xy = _point_tuple(getattr(via, "xy", (0.0, 0.0)))
        except ValueError as exc:
            issues.append(ViaGeometryIssue(via_def, net, idx, f"via {display_via} net {display_net} has invalid xy: {exc}", rows=rows, cols=cols))
            xy = None
        if layer_filter is not None and pdk is not None and via_def:
            required_layers = _via_required_layers(via_def, pdk)
            if required_layers and not any(layer in layer_filter for layer in required_layers):
                continue
        if not via_def:
            issues.append(ViaGeometryIssue(via_def, net, idx, "via <unknown> missing via definition", xy=xy, rows=rows, cols=cols))
        elif pdk is not None and not _via_def_is_known(via_def, pdk):
            issues.append(ViaGeometryIssue(via_def, net, idx, f"via {display_via} net {display_net} is not recognized by PDK", xy=xy, rows=rows, cols=cols))
        if not net:
            issues.append(ViaGeometryIssue(via_def, net, idx, f"via {display_via} missing net attachment", xy=xy, rows=rows, cols=cols))
        if _positive_int(rows) is None or _positive_int(cols) is None:
            issues.append(ViaGeometryIssue(via_def, net, idx, f"via {display_via} net {display_net} has non-positive via array {rows}x{cols}", xy=xy, rows=rows, cols=cols))
    return tuple(issues)


def analyze_plan_physical_connectivity(
    plan: Any,
    *,
    ignore_nets: Iterable[str] = (),
    include_touching: bool = True,
    include_via_landing_shorts: bool = False,
    include_instance_terminal_shorts: bool = False,
    layers: Iterable[str] | None = None,
    include_opens: bool = False,
    pdk: Any | None = None,
    terminal_accessor: Any | None = None,
) -> dict[str, object]:
    """Return a JSON-friendly physical connectivity precheck report."""

    shapes = collect_plan_shapes(
        plan,
        layers=layers,
        pdk=pdk,
        include_instance_terminals=include_instance_terminal_shorts,
        terminal_accessor=terminal_accessor,
    )
    shape_issues = detect_plan_shape_geometry_issues(plan, layers=layers)
    path_issues = detect_plan_path_geometry_issues(plan, layers=layers)
    via_issues = detect_plan_via_geometry_issues(plan, pdk=pdk, layers=layers)
    shorts = detect_plan_shape_shorts(
        plan,
        ignore_nets=ignore_nets,
        include_touching=include_touching,
        include_via_landings=include_via_landing_shorts,
        include_instance_terminals=include_instance_terminal_shorts,
        pdk=pdk,
        terminal_accessor=terminal_accessor,
        layers=layers,
    )
    opens = (
        detect_plan_net_opens(
            plan,
            pdk=pdk,
            layers=layers,
            include_instance_terminals=include_instance_terminal_shorts,
            terminal_accessor=terminal_accessor,
        )
        if include_opens
        else ()
    )
    shape_count_by_net: dict[str, int] = {}
    shape_count_by_layer: dict[str, int] = {}
    for shape in shapes:
        shape_count_by_net[shape.net] = shape_count_by_net.get(shape.net, 0) + 1
        shape_count_by_layer[shape.layer] = shape_count_by_layer.get(shape.layer, 0) + 1
    issues = (
        *(issue.message for issue in shape_issues),
        *(issue.message for issue in path_issues),
        *(issue.message for issue in via_issues),
        *(f"same-layer short risk {short.net_a}-{short.net_b} on {short.layer}" for short in shorts),
        *(f"net {item.net} has {item.component_count} disconnected geometry components" for item in opens),
    )
    return {
        "passed": not shape_issues and not path_issues and not via_issues and not shorts and not opens,
        "issues": list(dict.fromkeys(issues)),
        "shape_geometry_issues": [asdict(issue) for issue in shape_issues],
        "path_geometry_issues": [asdict(issue) for issue in path_issues],
        "via_geometry_issues": [asdict(issue) for issue in via_issues],
        "shorts": [asdict(short) for short in shorts],
        "via_landing_short_issues": [
            asdict(short)
            for short in shorts
            if short.source_a.startswith("via[") or short.source_b.startswith("via[")
        ],
        "opens": [asdict(open_issue) for open_issue in opens],
        "shape_count": len(shapes),
        "shape_count_by_net": shape_count_by_net,
        "shape_count_by_layer": shape_count_by_layer,
    }


def detect_plan_net_opens(
    plan: Any,
    *,
    pdk: Any | None = None,
    layers: Iterable[str] | None = None,
    min_shapes_per_net: int = 2,
    include_instance_terminals: bool = False,
    terminal_accessor: Any | None = None,
) -> tuple[NetOpen, ...]:
    """Detect same-net geometry split into disconnected physical components."""

    open_layers = _open_connectivity_layers(pdk)
    if layers is not None:
        requested_layers = {str(layer) for layer in layers}
        open_layers = requested_layers if open_layers is None else open_layers & requested_layers
    shapes = collect_plan_shapes(
        plan,
        layers=open_layers,
        pdk=pdk,
        include_instance_terminals=include_instance_terminals,
        terminal_accessor=terminal_accessor,
    )
    by_net: dict[str, list[PlanShape]] = {}
    for shape in shapes:
        by_net.setdefault(shape.net, []).append(shape)
    issues: list[NetOpen] = []
    for net, net_shapes in sorted(by_net.items()):
        if len(net_shapes) < min_shapes_per_net:
            continue
        parent = {idx: idx for idx in range(len(net_shapes))}
        for idx, left in enumerate(net_shapes):
            for jdx, right in enumerate(net_shapes[idx + 1 :], start=idx + 1):
                if _shapes_connect(left, right):
                    _union(parent, idx, jdx)
        for via_links in _same_net_via_links(plan, net, pdk):
            touched = [
                idx
                for idx, shape in enumerate(net_shapes)
                if any(shape.layer == via_layer and bbox_overlaps(shape.bbox, via_bbox, include_touching=True) for via_layer, via_bbox in via_links)
            ]
            for left_idx, right_idx in zip(touched, touched[1:]):
                _union(parent, left_idx, right_idx)
        for via_links in _same_net_cut_rect_links(plan, net, pdk):
            touched = [
                idx
                for idx, shape in enumerate(net_shapes)
                if any(shape.layer == via_layer and bbox_overlaps(shape.bbox, via_bbox, include_touching=True) for via_layer, via_bbox in via_links)
            ]
            for left_idx, right_idx in zip(touched, touched[1:]):
                _union(parent, left_idx, right_idx)
        instance_terminal_groups: dict[str, list[int]] = {}
        for idx, shape in enumerate(net_shapes):
            if shape.kind != "instance_terminal":
                continue
            instance_terminal_groups.setdefault(shape.source, []).append(idx)
        for indices in instance_terminal_groups.values():
            for left_idx, right_idx in zip(indices, indices[1:]):
                _union(parent, left_idx, right_idx)
        components = {idx: _find(parent, idx) for idx in parent}
        component_count = len(set(components.values()))
        if component_count <= 1:
            continue
        issues.append(
            NetOpen(
                net,
                component_count,
                len(net_shapes),
                tuple(sorted({shape.layer for shape in net_shapes})),
                tuple(shape.source for shape in net_shapes),
            )
        )
    return tuple(issues)


def analyze_via_landings(
    plan: Any,
    pdk: Any,
    *,
    landing_margin_um: float | None = None,
    require_all_layers: bool = True,
) -> dict[str, object]:
    """Check that each via has same-net landing geometry on required layers."""

    shapes = collect_plan_shapes(plan, include_vias=False)
    shapes_by_net_layer: dict[tuple[str, str], list[PlanShape]] = {}
    for shape in shapes:
        shapes_by_net_layer.setdefault((shape.net, shape.layer), []).append(shape)

    issues: list[ViaLandingIssue] = []
    for via in getattr(plan, "vias", ()):
        via_def = str(getattr(via, "via_def", ""))
        net = str(getattr(via, "net", ""))
        if not via_def or not net:
            continue
        try:
            xy = _point_tuple(getattr(via, "xy", (0.0, 0.0)))
        except ValueError:
            continue
        landings = via_landing_bboxes(via, pdk, landing_margin_um=landing_margin_um)
        if not landings:
            continue
        via_metadata = getattr(via, "metadata", {}) if isinstance(getattr(via, "metadata", {}), Mapping) else {}
        landing_override = tuple(str(layer) for layer in tuple(via_metadata.get("landing_layers", ())) if str(layer))
        if landing_override:
            landings = tuple((layer, bbox) for layer, bbox in landings if layer in set(landing_override))
            if not landings:
                continue
        covered_layers: list[str] = []
        partial_layers: list[str] = []
        for layer, landing in landings:
            layer_shapes = shapes_by_net_layer.get((net, layer), ())
            if any(bbox_contains(shape.bbox, landing, include_touching=True) for shape in layer_shapes):
                covered_layers.append(layer)
            elif any(bbox_overlaps(shape.bbox, landing, include_touching=True) for shape in layer_shapes):
                partial_layers.append(layer)
            elif layer in landing_override:
                covered_layers.append(layer)
        if not require_all_layers and covered_layers:
            continue
        for layer, _landing in landings:
            if layer not in covered_layers:
                detail = "insufficient" if layer in partial_layers else "missing"
                issues.append(
                    ViaLandingIssue(
                        via_def,
                        net,
                        xy,
                        layer,
                        f"via {via_def} net {net} {detail} {layer} landing/enclosure at {xy}",
                    )
                )
    return {
        "passed": not issues,
        "issues": [issue.message for issue in issues],
        "landing_issues": [asdict(issue) for issue in issues],
    }


def via_landing_bboxes(via: Any, pdk: Any, *, landing_margin_um: float | None = None) -> tuple[tuple[str, BBox], ...]:
    """Return required same-net metal landing boxes for a via."""

    via_def = str(getattr(via, "via_def", ""))
    if not via_def:
        return ()
    try:
        xy = _point_tuple(getattr(via, "xy", (0.0, 0.0)))
    except ValueError:
        return ()
    rows = _positive_int(getattr(via, "rows", 1))
    cols = _positive_int(getattr(via, "cols", 1))
    if rows is None or cols is None:
        return ()
    via_metadata = getattr(via, "metadata", {}) if isinstance(getattr(via, "metadata", {}), Mapping) else {}
    required_layers = _via_required_layers(via_def, pdk)
    if not required_layers:
        required_layers = tuple(str(layer) for layer in tuple(via_metadata.get("landing_layers", ())) if str(layer))
    if not required_layers:
        return ()
    metadata_margin = _metadata_float_or_none(via_metadata.get("landing_margin_um"))
    effective_landing_margin = landing_margin_um if landing_margin_um is not None else metadata_margin
    margin = _via_landing_margin_um(pdk, required_layers, effective_landing_margin, via_def=via_def)
    if "emit_cut_array" in via_metadata:
        use_array_geometry = bool(via_metadata.get("emit_cut_array", False))
    else:
        use_array_geometry = rows > 1 or cols > 1
    if use_array_geometry and (rows > 1 or cols > 1):
        cut_width = _via_cut_width_um(pdk, via_def)
        cut_spacing = _via_array_spacing_um(pdk, via_def, cut_width)
        pitch = cut_width + cut_spacing
        half_x = 0.5 * (cut_width + float(cols - 1) * pitch) + margin
        half_y = 0.5 * (cut_width + float(rows - 1) * pitch) + margin
        landing = (xy[0] - half_x, xy[1] - half_y, xy[0] + half_x, xy[1] + half_y)
    else:
        landing = (xy[0] - margin, xy[1] - margin, xy[0] + margin, xy[1] + margin)
    rules = getattr(pdk, "rules", None)
    if rules is not None and hasattr(rules, "snap_bbox_um"):
        landing = rules.snap_bbox_um(landing, mode="outward")
    return tuple((layer, landing) for layer in required_layers)


def _metadata_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def path_segment_bboxes(points: Sequence[Point], width: float) -> tuple[BBox, ...]:
    half = max(float(width), 0.0) / 2.0
    bboxes: list[BBox] = []
    for a, b in zip(points, points[1:]):
        x0, x1 = sorted((float(a[0]), float(b[0])))
        y0, y1 = sorted((float(a[1]), float(b[1])))
        bboxes.append((x0 - half, y0 - half, x1 + half, y1 + half))
    return tuple(bboxes)


def bbox_overlaps(a: BBox, b: BBox, *, include_touching: bool = True) -> bool:
    if include_touching:
        return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def bbox_contains(container: BBox, inner: BBox, *, include_touching: bool = True, tol_um: float = 1e-12) -> bool:
    if include_touching:
        tol = max(float(tol_um), 0.0)
        return (
            container[0] <= inner[0] + tol
            and container[1] <= inner[1] + tol
            and container[2] + tol >= inner[2]
            and container[3] + tol >= inner[3]
        )
    return container[0] < inner[0] and container[1] < inner[1] and container[2] > inner[2] and container[3] > inner[3]


def _shapes_connect(left: PlanShape, right: PlanShape) -> bool:
    return left.net == right.net and left.layer == right.layer and bbox_overlaps(left.bbox, right.bbox, include_touching=True)


def _same_net_via_links(plan: Any, net: str, pdk: Any | None) -> tuple[tuple[tuple[str, BBox], ...], ...]:
    if pdk is None:
        return ()
    links: list[tuple[tuple[str, BBox], ...]] = []
    for via in getattr(plan, "vias", ()):
        if str(getattr(via, "net", "")) != net:
            continue
        landings = via_landing_bboxes(via, pdk)
        if len(landings) >= 2:
            links.append(landings)
    return tuple(links)


def _same_net_cut_rect_links(plan: Any, net: str, pdk: Any | None) -> tuple[tuple[tuple[str, BBox], ...], ...]:
    """Return vertical connectivity links represented as cut-layer rectangles.

    Several OA/stream-out flows draw CO/VIA cuts as ordinary rectangles instead
    of using via objects.  Calibre still treats those cut shapes as connectivity
    between their legal landing layers, so the inline open checker must do the
    same or it will both miss real opens and report false opens around structured
    access stacks.
    """

    if pdk is None:
        return ()
    links: list[tuple[tuple[str, BBox], ...]] = []
    for rect in getattr(plan, "rects", ()):
        if str(getattr(rect, "net", "")) != net:
            continue
        cut_layer = str(getattr(rect, "layer", "") or "")
        if not cut_layer:
            continue
        landing_layers = _cut_rect_landing_layers(cut_layer, pdk)
        if not landing_layers:
            continue
        bbox = _try_bbox_tuple(getattr(rect, "bbox", None))
        if bbox is None or not _bbox_coords_are_finite(bbox):
            continue
        links.append(((cut_layer, bbox), *((layer, bbox) for layer in landing_layers)))
    return tuple(links)


def _cut_rect_landing_layers(cut_layer: str, pdk: Any) -> tuple[str, ...]:
    layer = str(cut_layer or "")
    if not layer:
        return ()
    via_layers = _via_required_layers(layer, pdk)
    if via_layers:
        return via_layers
    layer_map = getattr(pdk, "layer_map", None)
    if layer_map is None or layer != str(getattr(layer_map, "contact", "") or ""):
        return ()
    metals = tuple(str(item) for item in tuple(getattr(layer_map, "metals", ()) or ()))
    first_metal = metals[0] if metals else ""
    candidates = (
        str(getattr(layer_map, "active", "") or ""),
        str(getattr(layer_map, "gate", "") or ""),
        first_metal,
    )
    return tuple(dict.fromkeys(item for item in candidates if item))


def _open_connectivity_layers(pdk: Any | None) -> set[str] | None:
    if pdk is None:
        return None
    layer_map = getattr(pdk, "layer_map", None)
    if layer_map is None:
        return None
    layers: list[str] = [
        str(getattr(layer_map, "active", "") or ""),
        str(getattr(layer_map, "gate", "") or ""),
        str(getattr(layer_map, "contact", "") or ""),
    ]
    layers.extend(str(layer) for layer in tuple(getattr(layer_map, "metals", ()) or ()))
    layers.extend(str(layer) for layer in tuple(getattr(layer_map, "vias", ()) or ()))
    for via in tuple(getattr(pdk, "via_stack", ()) or ()):
        layers.append(str(getattr(via, "via_def", "") or ""))
        layers.append(str(getattr(via, "lower_layer", "") or ""))
        layers.append(str(getattr(via, "upper_layer", "") or ""))
    return {layer for layer in layers if layer}


def _find(parent: dict[int, int], idx: int) -> int:
    root = idx
    while parent[root] != root:
        root = parent[root]
    while parent[idx] != idx:
        nxt = parent[idx]
        parent[idx] = root
        idx = nxt
    return root


def _union(parent: dict[int, int], left: int, right: int) -> None:
    left_root = _find(parent, left)
    right_root = _find(parent, right)
    if left_root != right_root:
        parent[right_root] = left_root


def _shape_from_bbox(
    *,
    layer: object,
    net: object,
    bbox: object,
    kind: str,
    source: str,
    layer_filter: set[str] | None,
) -> PlanShape:
    layer_text = str(layer or "")
    net_text = str(net or "")
    if not layer_text or not net_text or bbox is None:
        return PlanShape("", "", (0.0, 0.0, 0.0, 0.0), kind, source)
    if layer_filter is not None and layer_text not in layer_filter:
        return PlanShape("", "", (0.0, 0.0, 0.0, 0.0), kind, source)
    bbox_tuple = _try_bbox_tuple(bbox)
    if bbox_tuple is None or not _bbox_coords_are_finite(bbox_tuple):
        return PlanShape("", "", (0.0, 0.0, 0.0, 0.0), kind, source)
    return PlanShape(layer_text, net_text, bbox_tuple, kind, source)


def _instance_terminal_shapes(
    plan: Any,
    pdk: Any,
    *,
    layer_filter: set[str] | None = None,
    terminal_accessor: Any | None = None,
) -> tuple[PlanShape, ...]:
    try:
        from analogskills.pcell import PCellTerminalAccessor, PCellTerminalRequiresTap
    except Exception:
        return ()
    accessor = terminal_accessor
    if accessor is None:
        try:
            accessor = PCellTerminalAccessor(pdk)
        except Exception:
            return ()
    shapes: list[PlanShape] = []
    seen: set[tuple[str, str, tuple[float, float, float, float], str]] = set()
    for inst_idx, instance in enumerate(getattr(plan, "instances", ())):
        normalized_instance = _terminal_accessor_instance(instance)
        connections = dict(getattr(normalized_instance, "connections", {}) or {})
        for terminal, net in sorted(connections.items()):
            if not net:
                continue
            try:
                if hasattr(accessor, "get_terminal_pins"):
                    pins = tuple(accessor.get_terminal_pins(normalized_instance, terminal))
                else:
                    pins = ()
            except (KeyError, ValueError, PCellTerminalRequiresTap):
                continue
            if not pins:
                try:
                    pins = (accessor.get_terminal_pin(normalized_instance, terminal),)
                except (KeyError, ValueError, PCellTerminalRequiresTap):
                    continue
            inst_name = str(getattr(normalized_instance, "name", getattr(instance, "name", inst_idx)))
            for pin in pins:
                layer = str(pin.layer or "")
                if layer_filter is not None and layer not in layer_filter:
                    continue
                bbox = pin.bbox_um
                if bbox is None:
                    x, y = pin.xy_um
                    min_width_fn = getattr(pdk.rules, "min_width_um", None)
                    try:
                        min_width_um = float(min_width_fn(layer) if callable(min_width_fn) else 0.0)
                    except KeyError:
                        min_width_um = 0.0
                    grid_step_um = float(getattr(pdk.rules, "grid_step_um", 0.005) or 0.005)
                    half = max(min_width_um, grid_step_um) / 2.0
                    bbox = (x - half, y - half, x + half, y + half)
                shape_key = (layer, str(net), bbox, f"instance[{inst_idx}].{inst_name}.{terminal}")
                if shape_key in seen:
                    continue
                seen.add(shape_key)
                shapes.append(PlanShape(layer, str(net), bbox, "instance_terminal", f"instance[{inst_idx}].{inst_name}.{terminal}"))
    return tuple(shapes)


def _terminal_accessor_instance(instance: Any) -> Any:
    if hasattr(instance, "logical_name"):
        return instance
    params = dict(getattr(instance, "params", {}) or {})
    metadata = dict(getattr(instance, "metadata", {}) or {})
    logical_name = str(metadata.get("logical_name") or _logical_name_from_instance(instance))
    width_um = float(metadata.get("width_um", params.get("width_um", params.get("w_um", 1.0))) or 1.0)
    height_um = float(metadata.get("height_um", params.get("height_um", params.get("h_um", 1.0))) or 1.0)
    bbox_x0_um = float(metadata.get("bbox_x0_um", metadata.get("layout_bbox_x0_um", params.get("bbox_x0_um", 0.0))) or 0.0)
    bbox_y0_um = float(metadata.get("bbox_y0_um", metadata.get("layout_bbox_y0_um", params.get("bbox_y0_um", 0.0))) or 0.0)
    return SimpleNamespace(
        name=str(getattr(instance, "name", "")),
        logical_name=logical_name,
        lib_name=str(getattr(instance, "lib", metadata.get("lib_name", ""))),
        cell_name=str(getattr(instance, "cell", metadata.get("cell_name", ""))),
        view_name=str(getattr(instance, "view", metadata.get("view_name", "layout"))),
        params=params,
        xy_um=tuple(getattr(instance, "xy", (0.0, 0.0))),
        orient=str(getattr(instance, "orient", "R0")),
        connections=dict(getattr(instance, "connections", {}) or {}),
        width_um=width_um,
        height_um=height_um,
        bbox_x0_um=bbox_x0_um,
        bbox_y0_um=bbox_y0_um,
        finger_choice=None,
        metadata=metadata,
    )


def _logical_name_from_instance(instance: Any) -> str:
    cell = str(getattr(instance, "cell", "") or "").lower()
    if "pch" in cell or "pmos" in cell or cell.startswith("p_"):
        return "pmos"
    if "nch" in cell or "nmos" in cell or cell.startswith("n_"):
        return "nmos"
    if "cap" in cell:
        return "capacitor"
    if "res" in cell:
        return "resistor"
    if "npn" in cell or "pnp" in cell or "bjt" in cell:
        return "bjt"
    return cell or "unknown"


def _bbox_tuple(value: object) -> BBox:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"bbox must be a 4-tuple, got {value!r}")
    return tuple(float(coord) for coord in value)  # type: ignore[return-value]


def _try_bbox_tuple(value: object) -> BBox | None:
    try:
        return _bbox_tuple(value)
    except (TypeError, ValueError):
        return None


def _point_tuple(value: object) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"point must be a 2-tuple, got {value!r}")
    return (float(value[0]), float(value[1]))


def _shape_issue_label(kind: str, idx: int, layer: str, net: str) -> str:
    layer_text = layer or "<unknown>"
    net_text = net or "<unnamed>"
    return f"{kind} {idx} {layer_text}/{net_text}"


def _bbox_has_area(bbox: BBox) -> bool:
    return bbox[2] >= bbox[0] and bbox[3] >= bbox[1]


def _bbox_has_positive_area(bbox: BBox) -> bool:
    return bbox[2] > bbox[0] and bbox[3] > bbox[1]


def _bbox_coords_are_finite(bbox: BBox) -> bool:
    return all(isfinite(coord) for coord in bbox)


def _via_required_layers(via_def: str, pdk: Any) -> tuple[str, ...]:
    layer_map = getattr(pdk, "layer_map", None)
    if layer_map is None:
        return ()
    if str(getattr(pdk, "name", "")).lower() == "tsmcn7":
        native_contact_map = {
            "M0_PO": (str(getattr(layer_map, "gate", "PO")), str(tuple(getattr(layer_map, "metals", ("M0",)))[0])),
            "M0_PO_VD": (str(getattr(layer_map, "gate", "PO")), str(tuple(getattr(layer_map, "metals", ("M0",)))[0])),
            "M0_SUB": (str(getattr(layer_map, "active", "OD")), str(tuple(getattr(layer_map, "metals", ("M0",)))[0])),
            "M0_NW": (str(getattr(layer_map, "active", "OD")), str(tuple(getattr(layer_map, "metals", ("M0",)))[0])),
        }
        if via_def in native_contact_map:
            return native_contact_map[via_def]
    via_rules = tuple(getattr(pdk, "via_stack", ()))
    for rule in via_rules:
        if str(getattr(rule, "via_def", "")) == via_def:
            lower = str(getattr(rule, "lower_layer", ""))
            upper = str(getattr(rule, "upper_layer", ""))
            if lower and upper:
                return (lower, upper)
    metals = tuple(getattr(layer_map, "metals", ()))
    if not metals:
        return ()
    if via_def == getattr(layer_map, "contact", ""):
        return ()
    vias = tuple(getattr(layer_map, "vias", ()))
    if via_def in vias:
        idx = vias.index(via_def)
        if idx + 1 < len(metals):
            return (metals[idx], metals[idx + 1])
    upper = via_def.upper()
    if upper.startswith("VIA") and upper[3:].isdigit():
        layers = _via_layers_from_digits(upper[3:], metals)
        if layers:
            return layers
    if upper.startswith("V") and upper[1:].isdigit() and len(upper) >= 3:
        layers = _via_layers_from_digits(upper[1:], metals)
        if layers:
            return layers
    return ()


def _via_layers_from_digits(digits: str, metals: Sequence[str]) -> tuple[str, str]:
    if len(digits) >= 2:
        lo = int(digits[0]) - 1
        hi = int(digits[-1]) - 1
        if 0 <= lo < len(metals) and 0 <= hi < len(metals) and lo != hi:
            return (metals[min(lo, hi)], metals[max(lo, hi)])
    else:
        idx = int(digits) - 1
        if 0 <= idx and idx + 1 < len(metals):
            return (metals[idx], metals[idx + 1])
    return ()


def _via_def_is_known(via_def: str, pdk: Any) -> bool:
    layer_map = getattr(pdk, "layer_map", None)
    if layer_map is None:
        return True
    if via_def == getattr(layer_map, "contact", ""):
        return True
    if via_def in tuple(getattr(layer_map, "vias", ())):
        return True
    return bool(_via_required_layers(via_def, pdk))


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _via_landing_margin_um(pdk: Any, layers: Sequence[str], override: float | None, *, via_def: str = "") -> float:
    rules = getattr(pdk, "rules", None)
    values: list[float] = []
    if override is not None:
        values.append(max(float(override), 0.0))
    if rules is not None:
        for layer in layers:
            try:
                values.append(float(rules.min_width_um(layer)) / 2.0)
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
            try:
                min_area_nm2 = float(getattr(rules, "min_area_nm2", {}).get(str(layer), 0) or 0)
                if min_area_nm2 > 0.0:
                    values.append(0.5 * sqrt(min_area_nm2) * 1e-3)
            except (AttributeError, TypeError, ValueError):
                pass
            for key in (f"{via_def}_{layer}", f"{layer}_{via_def}"):
                try:
                    values.append(float(rules.enclosure(key)) * 1e-3)
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass
        grid = getattr(rules, "grid_step_um", 0.0)
        if grid:
            values.append(float(grid))
    margin = max(values or [0.05])
    if rules is not None:
        try:
            margin = float(rules.snap_dimension_ceil_um(margin))
        except (AttributeError, TypeError, ValueError):
            pass
    signoff_grid = _signoff_grid_step_um(pdk)
    if signoff_grid > 0.0:
        return _snap_dimension_to_step_ceil_um(margin, signoff_grid)
    return margin


def _signoff_grid_step_um(pdk: Any) -> float:
    metadata = getattr(pdk, "metadata", {}) or {}
    if isinstance(metadata, Mapping):
        calibre = metadata.get("calibre", {}) or {}
        if isinstance(calibre, Mapping):
            try:
                grid_nm = float(calibre.get("grid_nm", 0) or 0)
                if grid_nm > 0.0:
                    return grid_nm * 1e-3
            except (TypeError, ValueError):
                pass
    return 0.0


def _snap_dimension_to_step_ceil_um(value_um: float, step_um: float) -> float:
    if step_um <= 0.0:
        return max(float(value_um), 0.0)
    return ceil(max(float(value_um), 0.0) / float(step_um) - 1e-12) * float(step_um)


def _via_cut_width_um(pdk: Any, via_def: str) -> float:
    rules = getattr(pdk, "rules", None)
    if rules is not None:
        try:
            return max(float(rules.min_width_um(via_def)), 0.0)
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        if via_def in {"VIA0", "M0_PO", "M0_PO_VD", "M0_SUB", "M0_NW"}:
            try:
                return max(float(rules.min_width_um("VD")), 0.0)
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
    return 0.0


def _via_array_spacing_um(pdk: Any, via_def: str, cut_width_um: float) -> float:
    rules = getattr(pdk, "rules", None)
    if rules is not None:
        try:
            return max(float(rules.array_spacing_um(via_def)), 0.0)
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        try:
            return max(float(rules.min_spacing_um(via_def)), 0.0)
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    return max(float(cut_width_um), 0.0)
