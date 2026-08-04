"""Local path-layer promotion ECO for Calibre enclosed-area markers.

This pass is intentionally more invasive than the append-only local DRC ECO:
it rewrites selected path layers and adds via stacks at path endpoints.  The
target use case is a small enclosed-area marker created when an additive
same-net route_fill plus a nearby different-net low-metal path closes a tiny
hole.  In that situation more M1 fill often trades one DRC for another; moving
the offending boundary path to a configured upper layer is the simpler local
repair.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Iterable, Mapping, Sequence

from .calibre_closure import LocalRepairAction


@dataclass(frozen=True)
class PathLayerPromotionEdit:
    path_index: int
    from_layer: str
    to_layer: str
    net: str
    via_stack: tuple[str, ...]
    endpoints: tuple[tuple[float, float], ...]
    source_rules: tuple[str, ...] = ()
    source_result_indices: tuple[int, ...] = ()
    target_shape_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PathLayerPromotionResult:
    plan: object
    edits: tuple[PathLayerPromotionEdit, ...]
    skipped: tuple[str, ...] = ()
    reason: str = ""


def solve_path_layer_promotion_eco(
    plan: object,
    actions: Iterable[LocalRepairAction],
    *,
    pdk: object | None = None,
    source_layer: str = "M1",
    target_layer: str = "M3",
    via_stack: Sequence[str] = ("VIA1", "VIA2"),
    trigger_rule_families: Sequence[str] = ("M*.A.*",),
    route_fill_kinds: Sequence[str] = ("route_fill",),
    route_fill_sources: Sequence[str] = ("local_routing_drc_eco",),
    require_other_net: bool = True,
    allow_spacing_path_promotion: bool = False,
    protected_nets: Sequence[str] = ("VSS", "VDD", "VDDHV", "AVSS", "AVDD"),
    max_promoted_paths_per_marker: int = 1,
    via_dedupe_tolerance_um: float | None = None,
) -> PathLayerPromotionResult:
    """Promote marker-boundary paths from ``source_layer`` to ``target_layer``.

    The pass only promotes a path segment when a configured marker also
    localizes to a configured route_fill rectangle on the source layer.  This
    makes the operator specific to fill-induced enclosed-area DRC, instead of
    becoming a generic rerouter.
    """

    from analogskills.eda.oa import OaVia, OaWritePlan

    src_layer = str(source_layer or "")
    dst_layer = str(target_layer or "")
    vias = tuple(str(via) for via in tuple(via_stack or ()) if str(via))
    if not src_layer or not dst_layer or src_layer == dst_layer or not vias:
        return PathLayerPromotionResult(plan, (), reason="invalid_configuration")

    route_fill_kind_set = {str(kind) for kind in tuple(route_fill_kinds or ()) if str(kind)}
    route_fill_source_set = {str(source) for source in tuple(route_fill_sources or ()) if str(source)}
    trigger_families = tuple(str(family) for family in tuple(trigger_rule_families or ()) if str(family))
    protected_net_set = {str(net).upper() for net in tuple(protected_nets or ()) if str(net)}
    max_paths_per_marker = max(int(max_promoted_paths_per_marker), 1)
    tol = _via_dedupe_tolerance_um(pdk, via_dedupe_tolerance_um)

    skipped: list[str] = []
    requests: dict[int, dict[str, object]] = {}
    path_segment_re = re.compile(r"^path\[(\d+)\]\.segment\[(\d+)\]$")
    for action in tuple(actions):
        if action.owner != "routing":
            continue
        rule = str(action.marker.rule or "")
        if trigger_families and not _matches_any_family(rule, trigger_families):
            continue
        marker_bbox = _bbox_tuple(action.marker.bbox)
        fill_nets: set[str] = set()
        fill_shape_ids: list[str] = []
        for shape_id in tuple(action.target_shape_ids):
            rect = _rect_from_shape_id(plan, str(shape_id))
            if rect is None or str(getattr(rect, "layer", "")) != src_layer:
                continue
            metadata = getattr(rect, "metadata", {}) if isinstance(getattr(rect, "metadata", {}), Mapping) else {}
            kind = str(metadata.get("kind", "") or "")
            source = str(metadata.get("source", "") or "")
            if route_fill_kind_set and kind not in route_fill_kind_set:
                continue
            if route_fill_source_set and source not in route_fill_source_set:
                continue
            net = str(getattr(rect, "net", "") or "")
            if net:
                fill_nets.add(net)
            fill_shape_ids.append(str(shape_id))
        promoted_this_marker = False
        if fill_shape_ids:
            candidate_shape_ids = tuple(action.target_shape_ids)
        elif bool(allow_spacing_path_promotion) and _is_numbered_metal_rule(rule, contains=".S."):
            action_nets = tuple(str(net) for net in tuple(action.params.get("nets", ()) or ()) if str(net))
            if bool(require_other_net) and len(set(action_nets)) < 2:
                skipped.append(f"spacing_marker_without_other_net:{rule}:{action.marker.result_index}")
                continue
            candidate_shape_ids = tuple(_spacing_promotion_path_shape_ids(plan, action, src_layer, marker_bbox, protected_net_set))
            if not candidate_shape_ids:
                skipped.append(f"no_spacing_promotable_path:{rule}:{action.marker.result_index}")
                continue
            candidate_shape_ids = candidate_shape_ids[:max_paths_per_marker]
        else:
            skipped.append(f"no_configured_route_fill:{rule}:{action.marker.result_index}")
            continue

        for shape_id in candidate_shape_ids:
            match = path_segment_re.match(str(shape_id))
            if match is None:
                continue
            path_index = int(match.group(1))
            segment_index = int(match.group(2))
            path = _path_from_index(plan, path_index)
            if path is None or str(getattr(path, "layer", "")) != src_layer:
                continue
            path_net = str(getattr(path, "net", "") or "")
            if not path_net:
                skipped.append(f"path_without_net:{rule}:{action.marker.result_index}:{shape_id}")
                continue
            if fill_shape_ids and bool(require_other_net) and path_net in fill_nets:
                skipped.append(f"same_net_boundary_path:{rule}:{action.marker.result_index}:{shape_id}")
                continue
            segment_bbox = _path_segment_bbox(path, segment_index)
            if segment_bbox is None or not _overlaps_or_touches(marker_bbox, segment_bbox):
                skipped.append(f"path_not_on_marker_boundary:{rule}:{action.marker.result_index}:{shape_id}")
                continue
            row = requests.setdefault(
                path_index,
                {
                    "rules": [],
                    "result_indices": [],
                    "shape_ids": [],
                },
            )
            row["rules"].append(rule)  # type: ignore[index, union-attr]
            row["result_indices"].append(int(action.marker.result_index or 0))  # type: ignore[index, union-attr]
            row["shape_ids"].extend([str(shape_id), *fill_shape_ids])  # type: ignore[index, union-attr]
            promoted_this_marker = True
        if not promoted_this_marker:
            skipped.append(f"no_promotable_path:{rule}:{action.marker.result_index}")

    if not requests:
        return PathLayerPromotionResult(plan, (), tuple(skipped), reason="no_promotable_paths")

    path_rows = list(tuple(getattr(plan, "paths", ())))
    via_rows = list(tuple(getattr(plan, "vias", ())))
    edits: list[PathLayerPromotionEdit] = []
    for path_index in sorted(requests):
        if not (0 <= path_index < len(path_rows)):
            continue
        path = path_rows[path_index]
        if str(getattr(path, "layer", "")) != src_layer:
            continue
        points = tuple((float(x), float(y)) for x, y in tuple(getattr(path, "points", ()) or ()))
        net = str(getattr(path, "net", "") or "")
        if len(points) < 2 or not net:
            continue
        endpoints = (_snap_point(points[0], pdk), _snap_point(points[-1], pdk))
        path_rows[path_index] = replace(path, layer=dst_layer)
        for endpoint in endpoints:
            for via_def in vias:
                if _via_exists(via_rows, via_def, net, endpoint, tol=tol):
                    continue
                via_rows.append(
                    OaVia(
                        via_def,
                        endpoint,
                        net,
                        metadata={
                            "source": "path_layer_promotion_eco",
                            "from_layer": src_layer,
                            "to_layer": dst_layer,
                        },
                    )
                )
        request = requests[path_index]
        edits.append(
            PathLayerPromotionEdit(
                path_index,
                src_layer,
                dst_layer,
                net,
                vias,
                endpoints,
                source_rules=tuple(dict.fromkeys(str(rule) for rule in tuple(request.get("rules", ())) if str(rule))),
                source_result_indices=tuple(dict.fromkeys(int(idx) for idx in tuple(request.get("result_indices", ())) if int(idx) >= 0)),
                target_shape_ids=tuple(dict.fromkeys(str(item) for item in tuple(request.get("shape_ids", ())) if str(item))),
            )
        )

    if not edits:
        return PathLayerPromotionResult(plan, (), tuple(skipped), reason="no_effective_edits")
    return PathLayerPromotionResult(
        OaWritePlan(
            getattr(plan, "cellview"),
            nets=tuple(getattr(plan, "nets", ())),
            pins=tuple(getattr(plan, "pins", ())),
            instances=tuple(getattr(plan, "instances", ())),
            rects=tuple(getattr(plan, "rects", ())),
            labels=tuple(getattr(plan, "labels", ())),
            paths=tuple(path_rows),
            vias=tuple(via_rows),
        ),
        tuple(edits),
        tuple(skipped),
        reason="promoted_paths",
    )


def build_path_layer_promotion_eco(
    oa_plan: object,
    calibre_results: Iterable[object],
    *,
    pdk: object | None = None,
    config: Mapping[str, object] | None = None,
) -> PathLayerPromotionResult:
    """Build a full-plan path promotion ECO from Calibre results."""

    from .calibre_closure import localize_calibre_markers, markers_from_calibre_results, plan_marker_repairs
    from .drc_lvs import layout_shapes_from_plan
    from analogskills.eda.oa import oa_write_plan_to_layout_plan

    cfg = _path_layer_promotion_config(pdk, config)
    if not bool(cfg.get("enabled", False)):
        return PathLayerPromotionResult(oa_plan, (), reason="disabled_by_config")
    results = tuple(calibre_results)
    candidate_results = tuple(row for row in results if _matches_any_family(str(getattr(row, "rule", "")), tuple(cfg["trigger_rule_families"])))
    if not candidate_results:
        return PathLayerPromotionResult(oa_plan, (), reason="no_configured_candidate_markers")
    layout_plan = oa_write_plan_to_layout_plan(oa_plan)
    shapes = layout_shapes_from_plan(layout_plan, pdk=pdk)
    ownership = localize_calibre_markers(markers_from_calibre_results(candidate_results), shapes, halo_um=float(cfg["halo_um"]))
    actions = tuple(action for action in plan_marker_repairs(ownership) if not action.requires_global_resolve)
    return solve_path_layer_promotion_eco(
        oa_plan,
        actions,
        pdk=pdk,
        source_layer=str(cfg["source_layer"]),
        target_layer=str(cfg["target_layer"]),
        via_stack=tuple(str(item) for item in tuple(cfg["via_stack"]) if str(item)),
        trigger_rule_families=tuple(str(item) for item in tuple(cfg["trigger_rule_families"]) if str(item)),
        route_fill_kinds=tuple(str(item) for item in tuple(cfg["route_fill_kinds"]) if str(item)),
        route_fill_sources=tuple(str(item) for item in tuple(cfg["route_fill_sources"]) if str(item)),
        require_other_net=bool(cfg["require_other_net"]),
        allow_spacing_path_promotion=bool(cfg["allow_spacing_path_promotion"]),
        protected_nets=tuple(str(item) for item in tuple(cfg["protected_nets"]) if str(item)),
        max_promoted_paths_per_marker=int(cfg["max_promoted_paths_per_marker"]),
        via_dedupe_tolerance_um=float(cfg["via_dedupe_tolerance_um"]),
    )


def path_layer_promotion_summary(result: PathLayerPromotionResult) -> dict[str, object]:
    return {
        "edit_count": len(result.edits),
        "skipped_count": len(result.skipped),
        "reason": result.reason,
        "edits": [
            {
                "path_index": edit.path_index,
                "from_layer": edit.from_layer,
                "to_layer": edit.to_layer,
                "net": edit.net,
                "via_stack": edit.via_stack,
                "endpoints": edit.endpoints,
                "source_rules": edit.source_rules,
                "source_result_indices": edit.source_result_indices,
                "target_shape_ids": edit.target_shape_ids,
            }
            for edit in result.edits
        ],
        "skipped": result.skipped,
    }


def _path_layer_promotion_config(pdk: object | None, override: Mapping[str, object] | None) -> dict[str, object]:
    cfg: dict[str, object] = {
        "enabled": False,
        "halo_um": 0.02,
        "source_layer": "M1",
        "target_layer": "M3",
        "via_stack": ("VIA1", "VIA2"),
        "trigger_rule_families": ("M1.A.4",),
        "route_fill_kinds": ("route_fill",),
        "route_fill_sources": ("local_routing_drc_eco",),
        "require_other_net": True,
        "allow_spacing_path_promotion": False,
        "protected_nets": ("VSS", "VDD", "VDDHV", "AVSS", "AVDD"),
        "max_promoted_paths_per_marker": 1,
        "via_dedupe_tolerance_um": 0.006,
    }
    metadata = getattr(pdk, "metadata", {}) if pdk is not None else {}
    if isinstance(metadata, Mapping):
        routing_geometry = metadata.get("routing_geometry", {})
        if isinstance(routing_geometry, Mapping):
            raw = routing_geometry.get("path_layer_promotion_eco", {})
            if isinstance(raw, Mapping):
                cfg.update(dict(raw))
    if override is not None:
        cfg.update(dict(override))
    if "halo_nm" in cfg:
        cfg["halo_um"] = max(float(cfg.get("halo_nm", 0.0)), 0.0) * 1e-3
    else:
        cfg["halo_um"] = max(float(cfg.get("halo_um", 0.02)), 0.0)
    if "via_dedupe_tolerance_nm" in cfg:
        cfg["via_dedupe_tolerance_um"] = max(float(cfg.get("via_dedupe_tolerance_nm", 0.0)), 0.0) * 1e-3
    else:
        cfg["via_dedupe_tolerance_um"] = max(float(cfg.get("via_dedupe_tolerance_um", 0.006)), 0.0)
    for key in ("via_stack", "trigger_rule_families", "route_fill_kinds", "route_fill_sources", "protected_nets"):
        cfg[key] = tuple(str(item) for item in tuple(cfg.get(key, ()) or ()) if str(item))
    cfg["source_layer"] = str(cfg.get("source_layer", "M1") or "M1")
    cfg["target_layer"] = str(cfg.get("target_layer", "M3") or "M3")
    cfg["enabled"] = bool(cfg.get("enabled", False))
    cfg["require_other_net"] = bool(cfg.get("require_other_net", True))
    cfg["allow_spacing_path_promotion"] = bool(cfg.get("allow_spacing_path_promotion", False))
    cfg["max_promoted_paths_per_marker"] = max(int(cfg.get("max_promoted_paths_per_marker", 1) or 1), 1)
    return cfg


def _spacing_promotion_path_shape_ids(
    plan: object,
    action: LocalRepairAction,
    source_layer: str,
    marker_bbox: tuple[float, float, float, float],
    protected_nets: set[str],
) -> tuple[str, ...]:
    path_segment_re = re.compile(r"^path\[(\d+)\]\.segment\[(\d+)\]$")
    rows: list[tuple[tuple[int, int, int], str]] = []
    seen: set[str] = set()
    for shape_id in tuple(action.target_shape_ids):
        text = str(shape_id)
        if text in seen:
            continue
        match = path_segment_re.match(text)
        if match is None:
            continue
        path_index = int(match.group(1))
        segment_index = int(match.group(2))
        path = _path_from_index(plan, path_index)
        if path is None or str(getattr(path, "layer", "")) != source_layer:
            continue
        segment_bbox = _path_segment_bbox(path, segment_index)
        if segment_bbox is None or not _overlaps_or_touches(marker_bbox, segment_bbox):
            continue
        net = str(getattr(path, "net", "") or "")
        if not net:
            continue
        # Prefer moving local signal wires instead of supply trunks.  Keep the
        # original localized order as the final tie-breaker so Calibre
        # ownership remains deterministic.
        priority = (1 if net.upper() in protected_nets else 0, path_index, segment_index)
        rows.append((priority, text))
        seen.add(text)
    rows.sort(key=lambda item: item[0])
    return tuple(text for _, text in rows)


def _rect_from_shape_id(plan: object, shape_id: str) -> object | None:
    if not (shape_id.startswith("rect[") and shape_id.endswith("]") and shape_id[5:-1].isdigit()):
        return None
    index = int(shape_id[5:-1])
    rects = tuple(getattr(plan, "rects", ()))
    if 0 <= index < len(rects):
        return rects[index]
    return None


def _path_from_index(plan: object, index: int) -> object | None:
    paths = tuple(getattr(plan, "paths", ()))
    if 0 <= index < len(paths):
        return paths[index]
    return None


def _path_segment_bbox(path: object, segment_index: int) -> tuple[float, float, float, float] | None:
    points = tuple((float(x), float(y)) for x, y in tuple(getattr(path, "points", ()) or ()))
    if not (0 <= segment_index < len(points) - 1):
        return None
    left = points[segment_index]
    right = points[segment_index + 1]
    half = 0.5 * float(getattr(path, "width", 0.0) or 0.0)
    return (
        min(left[0], right[0]) - half,
        min(left[1], right[1]) - half,
        max(left[0], right[0]) + half,
        max(left[1], right[1]) + half,
    )


def _via_exists(vias: Sequence[object], via_def: str, net: str, xy: tuple[float, float], *, tol: float) -> bool:
    for via in tuple(vias):
        if str(getattr(via, "via_def", "")) != via_def or str(getattr(via, "net", "")) != net:
            continue
        vx, vy = tuple(getattr(via, "xy", (None, None)) or (None, None))[:2]
        if vx is None or vy is None:
            continue
        if abs(float(vx) - xy[0]) <= tol and abs(float(vy) - xy[1]) <= tol:
            return True
    return False


def _snap_point(point: tuple[float, float], pdk: object | None) -> tuple[float, float]:
    rules = getattr(pdk, "rules", None)
    if rules is not None and hasattr(rules, "snap_point_um"):
        return tuple(float(value) for value in rules.snap_point_um(point))  # type: ignore[return-value]
    return (float(point[0]), float(point[1]))


def _via_dedupe_tolerance_um(pdk: object | None, value: float | None) -> float:
    if value is not None:
        return max(float(value), 0.0)
    rules = getattr(pdk, "rules", None)
    try:
        return max(float(getattr(rules, "grid_step_um", 0.001) or 0.001), 0.001)
    except (TypeError, ValueError):
        return 0.001


def _bbox_tuple(value: object) -> tuple[float, float, float, float]:
    row = tuple(float(item) for item in tuple(value))  # type: ignore[arg-type]
    if len(row) != 4:
        raise ValueError("bbox must have four coordinates")
    return row


def _overlaps_or_touches(a: tuple[float, float, float, float], b: tuple[float, float, float, float], tol: float = 1e-9) -> bool:
    return not (a[2] < b[0] - tol or b[2] < a[0] - tol or a[3] < b[1] - tol or b[3] < a[1] - tol)


def _matches_any_family(rule: str, families: Sequence[object]) -> bool:
    upper = str(rule).upper()
    return any(_matches_rule_family(upper, str(family).upper()) for family in tuple(families or ()))


def _matches_rule_family(rule: str, family: str) -> bool:
    if family == "M*.A.*":
        return _is_numbered_metal_rule(rule, contains=".A.")
    if family == "M*.S.*":
        return _is_numbered_metal_rule(rule, contains=".S.")
    if family == "M*.W.*":
        return _is_numbered_metal_rule(rule, contains=".W.")
    return rule == family or rule.startswith(family.rstrip("*"))


def _is_numbered_metal_rule(rule: str, *, contains: str | None = None) -> bool:
    head = str(rule).upper().split(":", 1)[0]
    if not head.startswith("M"):
        return False
    metal_number = head[1:].split(".", 1)[0]
    if not metal_number.isdigit():
        return False
    if contains is not None and contains not in head:
        return False
    return True
