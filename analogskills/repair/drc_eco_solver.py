"""Deterministic local ECO candidates for Calibre routing DRC markers.

This is intentionally not a global router.  It consumes localized Calibre
markers and proposes conservative additive same-net fills inside the local
marker windows.  More invasive edits such as path push/reroute are left for a
later local SMT/MILP backend.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

from .calibre_closure import LocalRepairAction


@dataclass(frozen=True)
class LocalDrcEcoEdit:
    kind: str
    layer: str
    net: str
    bbox: tuple[float, float, float, float]
    source_rules: tuple[str, ...] = ()
    source_result_indices: tuple[int, ...] = ()
    target_shape_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalDrcEcoResult:
    plan: object
    edits: tuple[LocalDrcEcoEdit, ...]
    skipped: tuple[str, ...] = ()
    unsupported_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalDrcEcoPatchResult:
    patch: object | None
    eco: LocalDrcEcoResult
    marker_count: int = 0
    candidate_marker_count: int = 0
    action_count: int = 0
    candidate_action_count: int = 0
    reason: str = ""
    marker_class_counts: Mapping[str, int] = field(default_factory=dict)
    skipped_rule_counts: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LocalGeometryEcoSpec:
    """Append-only geometry primitive for deterministic Calibre ECO.

    This is deliberately small: higher-level agents/solvers decide *where* a
    fix belongs, while this object records exactly what same-net geometry must
    be appended.  It covers redundant via arrays, rectangular/bar vias, landing
    hulls, and notch fills without hard-coding any design coordinates in the
    solver.
    """

    layer: str
    net: str
    bbox: tuple[float, float, float, float]
    kind: str = "local_geometry_eco"
    source: str = "local_geometry_eco"
    source_rules: tuple[str, ...] = ()
    source_result_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class LocalPathPushSpec:
    """Replacement/push primitive for local SMT ECO.

    ``point_indices`` identifies which vertices of an existing OA path are
    moved by ``dx_um``/``dy_um``.  This is the compact representation we need
    for fixes such as moving one M4 access trunk away from another net without
    adding fill.
    """

    path_index: int
    point_indices: tuple[int, ...]
    dx_um: float = 0.0
    dy_um: float = 0.0
    source_rules: tuple[str, ...] = ()
    source_result_indices: tuple[int, ...] = ()


def build_local_geometry_eco_patch(
    cellview: object,
    specs: Iterable[LocalGeometryEcoSpec],
    *,
    pdk: object | None = None,
) -> object | None:
    """Build an append-mode OA patch from deterministic geometry specs.

    The function performs only snapping and metadata stamping.  It does not
    decide legality; callers should run inline/Calibre acceptance after applying
    the patch.  Keeping this as an append primitive makes rejected ECOs easy to
    exclude from replay.
    """

    from analogskills.eda.oa import OaRect, OaWritePlan

    rows = tuple(specs)
    if not rows:
        return None
    patch_cellview = replace(cellview, mode="a")
    rects = tuple(
        OaRect(
            spec.layer,
            "drawing",
            _snap_bbox(_bbox_tuple(spec.bbox), pdk),
            spec.net,
            metadata={
                "kind": spec.kind,
                "source": spec.source,
                "source_rules": list(spec.source_rules),
                "source_result_indices": list(spec.source_result_indices),
            },
        )
        for spec in rows
    )
    return OaWritePlan(
        patch_cellview,
        nets=tuple(dict.fromkeys(spec.net for spec in rows if spec.net)),
        rects=rects,
    )


def apply_local_path_pushes(
    plan: object,
    pushes: Iterable[LocalPathPushSpec],
    *,
    pdk: object | None = None,
) -> LocalDrcEcoResult:
    """Return a replacement OA plan with selected path vertices pushed.

    This is the deterministic writeback side of a local SMT repair.  The SMT
    solver may choose the displacement; this function applies it to the typed
    OA artifact and records auditable edits.
    """

    from analogskills.eda.oa import OaWritePlan

    push_rows = tuple(pushes)
    paths = list(tuple(getattr(plan, "paths", ())))
    edits: list[LocalDrcEcoEdit] = []
    skipped: list[str] = []
    for push in push_rows:
        if push.path_index < 0 or push.path_index >= len(paths):
            skipped.append(f"path_index_out_of_range:{push.path_index}")
            continue
        path = paths[push.path_index]
        points = list(tuple(getattr(path, "points", ())))
        if not points:
            skipped.append(f"path_without_points:{push.path_index}")
            continue
        point_indices = tuple(dict.fromkeys(int(index) for index in push.point_indices))
        if any(index < 0 or index >= len(points) for index in point_indices):
            skipped.append(f"point_index_out_of_range:{push.path_index}:{point_indices}")
            continue
        old_bbox = _path_bbox(path)
        for index in point_indices:
            x, y = points[index]
            points[index] = _snap_point((float(x) + float(push.dx_um), float(y) + float(push.dy_um)), pdk)
        new_path = replace(path, points=tuple(points))
        paths[push.path_index] = new_path
        new_bbox = _path_bbox(new_path)
        edits.append(
            LocalDrcEcoEdit(
                "local_path_push",
                str(getattr(new_path, "layer", "")),
                str(getattr(new_path, "net", "")),
                _union((old_bbox, new_bbox)),
                source_rules=push.source_rules,
                source_result_indices=push.source_result_indices,
                target_shape_ids=(f"path[{push.path_index}]",),
            )
        )

    return LocalDrcEcoResult(
        OaWritePlan(
            getattr(plan, "cellview"),
            nets=tuple(getattr(plan, "nets", ())),
            pins=tuple(getattr(plan, "pins", ())),
            instances=tuple(getattr(plan, "instances", ())),
            rects=tuple(getattr(plan, "rects", ())),
            labels=tuple(getattr(plan, "labels", ())),
            paths=tuple(paths),
            vias=tuple(getattr(plan, "vias", ())),
        ),
        tuple(edits),
        tuple(skipped),
        (),
    )


def solve_local_drc_eco(
    plan: object,
    actions: Iterable[LocalRepairAction],
    *,
    pdk: object | None = None,
    min_spacing_um_by_layer: Mapping[str, float] | None = None,
    marker_padding_um: float | None = None,
    same_net_spacing_fill_padding_um: float | None = None,
    same_net_spacing_fill_bridge_max_gap_um: float | None = None,
    fill_enforce_min_area: bool = False,
    contact_enclosure_rules: Mapping[str, object] | None = None,
    enabled_action_kinds: tuple[str, ...] = ("remove_short_jog", "add_min_area_patch"),
    width_fill_requires_expandable_shape: bool = False,
    same_net_spacing_fill_cluster_by_shape: bool = True,
    same_net_spacing_fill_merge_touching: bool = True,
) -> LocalDrcEcoResult:
    """Build a conservative additive local ECO plan.

    Supported in this first version:
    - ``remove_short_jog``: cluster same-net G.4 markers and fill the local jog.
    - ``add_min_area_patch``: fill same-net positive-area marker windows.
    - ``widen_shape``: fill same-net width marker windows.

    The solver only adds geometry when all localized owner shapes agree on a
    single net/layer and the candidate has no same-layer other-net spacing
    conflict under the configured spacing table.
    """

    from analogskills.eda.oa import OaRect, OaWritePlan

    spacing = _spacing_table(pdk, min_spacing_um_by_layer)
    padding = _marker_padding(pdk, marker_padding_um)
    spacing_fill_padding = max(float(same_net_spacing_fill_padding_um or 0.0), 0.0)
    spacing_fill_bridge_max_gap = max(float(same_net_spacing_fill_bridge_max_gap_um or 0.0), 0.0)
    enclosure_rules = _contact_enclosure_rule_table(pdk, contact_enclosure_rules)
    action_rows = tuple(actions)
    enabled = {str(kind) for kind in enabled_action_kinds}
    fill_requests: list[_FillRequest] = []
    skipped: list[str] = []
    unsupported: list[str] = []

    clustered_fill_kinds = tuple(
        kind
        for kind in ("remove_short_jog", "same_net_spacing_fill")
        if kind in enabled
    )
    if clustered_fill_kinds:
        fill_requests.extend(
            _cluster_short_jog_requests(
                plan,
                action_rows,
                pdk=pdk,
                padding_um=padding,
                skipped=skipped,
                accepted_kinds=clustered_fill_kinds,
                expand_um_by_kind={"same_net_spacing_fill": spacing_fill_padding},
                bridge_max_gap_um=spacing_fill_bridge_max_gap,
                same_net_spacing_fill_cluster_by_shape=bool(same_net_spacing_fill_cluster_by_shape),
            )
        )
    for action in action_rows:
        if action.owner != "routing":
            continue
        if action.kind in {"remove_short_jog", "same_net_spacing_fill"}:
            if action.kind not in enabled:
                unsupported.append(f"{action.marker.rule}:{action.kind}:{action.marker.result_index}")
            continue
        if action.kind not in enabled:
            unsupported.append(f"{action.marker.rule}:{action.kind}:{action.marker.result_index}")
            continue
        if action.kind == "replace_via_template":
            request = _contact_enclosure_fill_request(
                plan,
                action,
                pdk=pdk,
                contact_enclosure_rules=enclosure_rules,
            )
            if request is None:
                skipped.append(f"no_contact_enclosure_rule:{action.marker.rule}:{action.marker.result_index}")
                continue
            fill_requests.append(request)
            continue
        if action.kind == "widen_shape":
            request = _width_fill_request(
                plan,
                action,
                pdk=pdk,
                padding_um=padding,
                require_expandable_shape=bool(width_fill_requires_expandable_shape),
            )
            if request is None:
                skipped.append(f"no_single_net:{action.marker.rule}:{action.marker.result_index}")
                continue
            fill_requests.append(request)
            continue
        if action.kind != "add_min_area_patch":
            unsupported.append(f"{action.marker.rule}:{action.kind}:{action.marker.result_index}")
            continue
        request = _min_area_fill_request(plan, action, pdk=pdk, padding_um=padding)
        if request is None:
            skipped.append(f"no_single_net:{action.marker.rule}:{action.marker.result_index}")
            continue
        fill_requests.append(request)

    rects = list(tuple(getattr(plan, "rects", ())))
    edits: list[LocalDrcEcoEdit] = []
    existing_candidates: set[tuple[str, str, tuple[float, float, float, float]]] = set()
    for request in _merge_fill_requests(
        fill_requests,
        merge_same_net_spacing_fill=bool(same_net_spacing_fill_merge_touching),
    ):
        enforce_min_area = bool(fill_enforce_min_area) and request.kind in {
            "same_net_min_area_fill",
            "same_net_spacing_fill",
            "same_net_jog_fill",
        }
        candidate_bboxes = (
            _rule_safe_bbox_candidates(request.bbox, request.layer, pdk)
            if enforce_min_area
            else (_snap_bbox(_ensure_minimum_bbox_span(_snap_bbox(request.bbox, pdk), request.layer, pdk), pdk),)
        )
        accepted_bbox: tuple[float, float, float, float] | None = None
        skipped_reasons: list[str] = []
        for bbox in candidate_bboxes:
            if not _positive_area(bbox):
                skipped_reasons.append("empty_fill")
                continue
            key = (request.layer, request.net, bbox)
            if key in existing_candidates:
                skipped_reasons.append("duplicate")
                continue
            if _has_other_net_conflict(plan, bbox, request.layer, request.net, spacing.get(request.layer, 0.0)):
                skipped_reasons.append("spacing")
                continue
            if _already_covered_by_same_net(plan, bbox, request.layer, request.net):
                skipped_reasons.append("covered")
                continue
            accepted_bbox = bbox
            existing_candidates.add(key)
            break
        if accepted_bbox is None:
            reason = next((row for row in skipped_reasons if row != "duplicate"), "no_candidate")
            skipped.append(f"{reason}:{request.layer}:{request.net}:{request.result_indices}")
            continue
        bbox = accepted_bbox
        rects.append(
            OaRect(
                request.layer,
                "drawing",
                bbox,
                request.net,
                metadata={"kind": "route_fill", "source": f"local_drc_eco_solver:{request.kind}"},
            )
        )
        edits.append(
            LocalDrcEcoEdit(
                request.kind,
                request.layer,
                request.net,
                bbox,
                source_rules=request.rules,
                source_result_indices=request.result_indices,
                target_shape_ids=request.shape_ids,
            )
        )

    return LocalDrcEcoResult(
        OaWritePlan(
            getattr(plan, "cellview"),
            nets=tuple(getattr(plan, "nets", ())),
            pins=tuple(getattr(plan, "pins", ())),
            instances=tuple(getattr(plan, "instances", ())),
            rects=tuple(rects),
            labels=tuple(getattr(plan, "labels", ())),
            paths=tuple(getattr(plan, "paths", ())),
            vias=tuple(getattr(plan, "vias", ())),
        ),
        tuple(edits),
        tuple(skipped),
        tuple(unsupported),
    )


def build_local_routing_drc_eco_patch(
    oa_plan: object,
    calibre_results: Iterable[object],
    *,
    pdk: object | None = None,
    config: Mapping[str, object] | None = None,
) -> LocalDrcEcoPatchResult:
    """Create an append-only OA patch for configured local routing DRC fixes.

    The Calibre marker ownership and rule families are used only to select
    local same-net fill candidates.  The returned patch contains only new
    rectangles; callers can append it to an existing OA cellview without
    duplicating the original layout.
    """

    from .calibre_closure import (
        classify_calibre_markers_for_local_repair,
        localize_calibre_markers,
        markers_from_calibre_results,
        plan_marker_repairs,
    )
    from .drc_lvs import layout_shapes_from_plan
    from analogskills.eda.oa import OaRect, OaWritePlan, oa_write_plan_to_layout_plan

    cfg = _local_drc_eco_config(pdk, config)
    empty = LocalDrcEcoResult(oa_plan, ())
    if not bool(cfg.get("enabled", True)):
        return LocalDrcEcoPatchResult(None, empty, reason="disabled_by_config")

    results = tuple(calibre_results)
    classifications = classify_calibre_markers_for_local_repair(results, config=cfg)
    class_counts = Counter(row.repair_class for row in classifications)
    skipped_rule_counts = Counter(
        f"{classification.repair_class}:{str(getattr(row, 'rule', ''))}"
        for row, classification in zip(results, classifications)
        if classification.repair_class != "local_auto_repair"
    )
    candidate_results = tuple(
        row
        for row, classification in zip(results, classifications)
        if classification.repair_class == "local_auto_repair"
        and _is_candidate_rule(str(getattr(row, "rule", "")), cfg)
    )
    if not candidate_results:
        return LocalDrcEcoPatchResult(
            None,
            empty,
            marker_count=len(results),
            candidate_marker_count=0,
            reason="no_configured_candidate_markers",
            marker_class_counts=dict(sorted(class_counts.items())),
            skipped_rule_counts=dict(sorted(skipped_rule_counts.items())),
        )

    layout_plan = oa_write_plan_to_layout_plan(oa_plan)
    shapes = layout_shapes_from_plan(layout_plan, pdk=pdk)
    markers = markers_from_calibre_results(candidate_results)
    ownership = localize_calibre_markers(markers, shapes, halo_um=float(cfg["halo_um"]))
    actions = tuple(_configured_local_action(action, cfg) for action in plan_marker_repairs(ownership))
    enabled_kinds = tuple(str(kind) for kind in tuple(cfg.get("enabled_action_kinds", ()) or ()))
    candidate_actions = tuple(
        action for action in actions
        if action.owner == "routing"
        and action.kind in set(enabled_kinds)
        and not action.requires_global_resolve
    )
    if not candidate_actions:
        return LocalDrcEcoPatchResult(
            None,
            empty,
            marker_count=len(results),
            candidate_marker_count=len(candidate_results),
            action_count=len(actions),
            candidate_action_count=0,
            reason="no_local_routing_actions",
            marker_class_counts=dict(sorted(class_counts.items())),
            skipped_rule_counts=dict(sorted(skipped_rule_counts.items())),
        )

    eco = solve_local_drc_eco(
        oa_plan,
        candidate_actions,
        pdk=pdk,
        marker_padding_um=float(cfg["marker_padding_um"]),
        same_net_spacing_fill_padding_um=float(cfg["same_net_spacing_fill_padding_um"]),
        same_net_spacing_fill_bridge_max_gap_um=float(cfg["same_net_spacing_fill_bridge_max_gap_um"]),
        fill_enforce_min_area=bool(cfg["fill_enforce_min_area"]),
        contact_enclosure_rules=cfg.get("contact_enclosure_rules", {}),
        enabled_action_kinds=enabled_kinds,
        width_fill_requires_expandable_shape=bool(cfg.get("width_fill_requires_expandable_shape", False)),
        same_net_spacing_fill_cluster_by_shape=bool(cfg.get("same_net_spacing_fill_cluster_by_shape", True)),
        same_net_spacing_fill_merge_touching=bool(cfg.get("same_net_spacing_fill_merge_touching", True)),
    )
    if not eco.edits:
        return LocalDrcEcoPatchResult(
            None,
            eco,
            marker_count=len(results),
            candidate_marker_count=len(candidate_results),
            action_count=len(actions),
            candidate_action_count=len(candidate_actions),
            reason="no_safe_local_edits",
            marker_class_counts=dict(sorted(class_counts.items())),
            skipped_rule_counts=dict(sorted(skipped_rule_counts.items())),
        )

    cellview = replace(getattr(oa_plan, "cellview"), mode="a")
    rects = tuple(
        OaRect(
            edit.layer,
            "drawing",
            edit.bbox,
            edit.net,
            metadata={
                "kind": "route_fill",
                "source": "local_routing_drc_eco",
                "source_rules": list(edit.source_rules),
                "source_result_indices": list(edit.source_result_indices),
            },
        )
        for edit in eco.edits
    )
    patch = OaWritePlan(
        cellview,
        nets=tuple(dict.fromkeys(edit.net for edit in eco.edits if edit.net)),
        rects=rects,
    )
    return LocalDrcEcoPatchResult(
        patch,
        eco,
        marker_count=len(results),
        candidate_marker_count=len(candidate_results),
        action_count=len(actions),
        candidate_action_count=len(candidate_actions),
        reason="local_edits_available",
        marker_class_counts=dict(sorted(class_counts.items())),
        skipped_rule_counts=dict(sorted(skipped_rule_counts.items())),
    )


def local_drc_eco_summary(result: LocalDrcEcoResult) -> dict[str, object]:
    return {
        "edit_count": len(result.edits),
        "skipped_count": len(result.skipped),
        "unsupported_action_count": len(result.unsupported_actions),
        "edits": [
            {
                "kind": edit.kind,
                "layer": edit.layer,
                "net": edit.net,
                "bbox": edit.bbox,
                "source_rules": edit.source_rules,
                "source_result_indices": edit.source_result_indices,
                "target_shape_ids": edit.target_shape_ids,
            }
            for edit in result.edits
        ],
        "skipped": result.skipped,
        "unsupported_actions": result.unsupported_actions,
    }


def local_drc_eco_patch_summary(result: LocalDrcEcoPatchResult) -> dict[str, object]:
    summary = local_drc_eco_summary(result.eco)
    summary.update(
        {
            "patch_available": result.patch is not None,
            "marker_count": result.marker_count,
            "candidate_marker_count": result.candidate_marker_count,
            "action_count": result.action_count,
            "candidate_action_count": result.candidate_action_count,
            "reason": result.reason,
            "marker_class_counts": dict(result.marker_class_counts),
            "skipped_rule_counts": dict(result.skipped_rule_counts),
        }
    )
    return summary


@dataclass(frozen=True)
class _FillRequest:
    kind: str
    layer: str
    net: str
    bbox: tuple[float, float, float, float]
    rules: tuple[str, ...]
    result_indices: tuple[int, ...]
    shape_ids: tuple[str, ...]


@dataclass(frozen=True)
class _ContactEnclosureRule:
    cut_layer: str
    outer_layer: str
    enclosure_um: float
    rule_families: tuple[str, ...] = ()


def _cluster_short_jog_requests(
    plan: object,
    actions: tuple[LocalRepairAction, ...],
    *,
    pdk: object | None,
    padding_um: float,
    skipped: list[str],
    accepted_kinds: tuple[str, ...] = ("remove_short_jog",),
    expand_um_by_kind: Mapping[str, float] | None = None,
    bridge_max_gap_um: float = 0.0,
    same_net_spacing_fill_cluster_by_shape: bool = True,
) -> tuple[_FillRequest, ...]:
    rows: list[_FillRequest] = []
    seeds: list[tuple[str, str, tuple[str, ...], LocalRepairAction]] = []
    accepted = {str(kind) for kind in accepted_kinds}
    expand_by_kind = {
        str(kind): max(float(value), 0.0)
        for kind, value in dict(expand_um_by_kind or {}).items()
    }
    for action in actions:
        if action.owner != "routing" or action.kind not in accepted:
            continue
        layer, net = _single_action_layer_net(plan, action)
        if not layer or not net:
            skipped.append(f"no_single_net:{action.marker.rule}:{action.marker.result_index}")
            continue
        shape_ids = tuple(dict.fromkeys(str(item) for item in action.target_shape_ids if str(item)))
        seeds.append((layer, net, shape_ids, action))
    consumed: set[int] = set()
    for index, (layer, net, shape_ids, action) in enumerate(seeds):
        if index in consumed:
            continue
        consumed.add(index)
        group = [action]
        group_shape_ids = set(shape_ids)
        can_cluster_by_shape = not (
            action.kind == "same_net_spacing_fill"
            and not bool(same_net_spacing_fill_cluster_by_shape)
        )
        changed = bool(can_cluster_by_shape)
        while changed:
            changed = False
            for other_index, (other_layer, other_net, other_shape_ids, other_action) in enumerate(seeds):
                if other_index in consumed or other_layer != layer or other_net != net:
                    continue
                if not bool(same_net_spacing_fill_cluster_by_shape) and (
                    action.kind == "same_net_spacing_fill"
                    or other_action.kind == "same_net_spacing_fill"
                ):
                    continue
                if group_shape_ids & set(other_shape_ids):
                    consumed.add(other_index)
                    group.append(other_action)
                    group_shape_ids.update(other_shape_ids)
                    changed = True
        marker_boxes = tuple(_bbox_tuple(row.marker.bbox) for row in group)
        kind = "same_net_spacing_fill" if any(row.kind == "same_net_spacing_fill" for row in group) else "same_net_jog_fill"
        bbox = _union(marker_boxes)
        extra_padding = expand_by_kind.get(kind, 0.0)
        if extra_padding > 0.0:
            bbox = _expand_bbox(bbox, extra_padding)
        bbox = _grow_degenerate_bbox(bbox, layer, pdk, padding_um=padding_um)
        if kind == "same_net_spacing_fill" and bridge_max_gap_um > 0.0:
            bbox = _stretch_spacing_fill_bbox_to_same_net_neighbors(
                plan,
                bbox,
                layer,
                net,
                max_gap_um=bridge_max_gap_um,
            )
        rows.append(
            _FillRequest(
                kind,
                layer,
                net,
                bbox,
                tuple(dict.fromkeys(row.marker.rule for row in group)),
                tuple(int(row.marker.result_index or 0) for row in group),
                tuple(sorted(group_shape_ids)),
            )
        )
    return tuple(rows)


def _min_area_fill_request(
    plan: object,
    action: LocalRepairAction,
    *,
    pdk: object | None,
    padding_um: float,
) -> _FillRequest | None:
    layer, net = _single_action_layer_net(plan, action)
    if not layer or not net:
        return None
    bbox = _bbox_tuple(action.marker.bbox)
    bbox = _grow_degenerate_bbox(bbox, layer, pdk, padding_um=padding_um)
    return _FillRequest(
        "same_net_min_area_fill",
        layer,
        net,
        bbox,
        (action.marker.rule,),
        (int(action.marker.result_index or 0),),
        tuple(dict.fromkeys(str(item) for item in action.target_shape_ids if str(item))),
    )


def _width_fill_request(
    plan: object,
    action: LocalRepairAction,
    *,
    pdk: object | None,
    padding_um: float,
    require_expandable_shape: bool = False,
) -> _FillRequest | None:
    layer, net = _single_action_layer_net(plan, action)
    if not layer or not net:
        return None
    bbox = _bbox_tuple(action.marker.bbox)
    gate_landing_bbox = _expandable_gate_landing_bbox(plan, action, layer=layer, net=net)
    if gate_landing_bbox is not None:
        bbox = _union((gate_landing_bbox, bbox))
        bbox = _snap_bbox(bbox, pdk)
        return _FillRequest(
            "same_net_shape_expand_fill",
            layer,
            net,
            bbox,
            (action.marker.rule,),
            (int(action.marker.result_index or 0),),
            tuple(dict.fromkeys(str(item) for item in action.target_shape_ids if str(item))),
        )
    if bool(require_expandable_shape):
        return None
    bbox = _grow_degenerate_bbox(bbox, layer, pdk, padding_um=padding_um)
    return _FillRequest(
        "same_net_width_fill",
        layer,
        net,
        bbox,
        (action.marker.rule,),
        (int(action.marker.result_index or 0),),
        tuple(dict.fromkeys(str(item) for item in action.target_shape_ids if str(item))),
    )


def _expandable_gate_landing_bbox(
    plan: object,
    action: LocalRepairAction,
    *,
    layer: str,
    net: str,
) -> tuple[float, float, float, float] | None:
    """Return a gate M1 landing bbox that should be expanded as one rectangle.

    A marker-window fill can leave an L-shaped same-net union around a gate
    access landing.  Calibre then often trades the original width marker for
    G.4/Mx.W.4 corner markers.  For calibrated router gate landings, prefer a
    full bbox expansion; appending this rectangle is geometrically equivalent
    to resizing the original landing in the GDS union.
    """

    for shape_id in tuple(action.target_shape_ids):
        rect = _rect_from_shape_id(plan, str(shape_id))
        if rect is None:
            continue
        if str(getattr(rect, "layer", "")) != layer or str(getattr(rect, "net", "")) != net:
            continue
        metadata = getattr(rect, "metadata", {}) if isinstance(getattr(rect, "metadata", {}), Mapping) else {}
        if str(metadata.get("kind", "") or "") != "router_gate_m1_contact_landing":
            continue
        return _bbox_tuple(getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0)))
    return None


def _rect_from_shape_id(plan: object, shape_id: str) -> object | None:
    if not (shape_id.startswith("rect[") and shape_id.endswith("]") and shape_id[5:-1].isdigit()):
        return None
    index = int(shape_id[5:-1])
    rects = tuple(getattr(plan, "rects", ()))
    if 0 <= index < len(rects):
        return rects[index]
    return None


def _contact_enclosure_fill_request(
    plan: object,
    action: LocalRepairAction,
    *,
    pdk: object | None,
    contact_enclosure_rules: Mapping[str, _ContactEnclosureRule],
) -> _FillRequest | None:
    cut_layer = str(action.marker.layer or "")
    net = ""
    cut_bbox: tuple[float, float, float, float] | None = None
    shape_ids = tuple(dict.fromkeys(str(item) for item in action.target_shape_ids if str(item)))
    for shape_id in shape_ids:
        shape = _shape_signature(plan, shape_id)
        if shape is None:
            continue
        layer = str(shape.get("layer", "") or "")
        if not cut_layer:
            cut_layer = layer
        if layer != cut_layer:
            continue
        shape_net = str(shape.get("net", "") or "")
        if shape_net:
            net = shape_net if not net else net
        cut_bbox = _bbox_tuple(shape.get("bbox", action.marker.bbox))
        break
    if cut_bbox is None:
        nets = tuple(str(row) for row in tuple(action.params.get("nets", ()) or ()) if str(row))
        if len(nets) != 1:
            return None
        net = nets[0]
        cut_bbox = _bbox_tuple(action.marker.bbox)
    if not cut_layer or not net:
        return None
    rule = contact_enclosure_rules.get(cut_layer)
    if rule is None:
        return None
    if rule.rule_families and not _matches_any_family(action.marker.rule, tuple(rule.rule_families)):
        return None
    enclosure = max(float(rule.enclosure_um), 0.0)
    bbox = _expand_bbox(cut_bbox, enclosure)
    bbox = _snap_bbox(bbox, pdk)
    return _FillRequest(
        "contact_po_enclosure",
        rule.outer_layer,
        net,
        bbox,
        (action.marker.rule,),
        (int(action.marker.result_index or 0),),
        shape_ids,
    )


def _contact_enclosure_rule_table(
    pdk: object | None,
    raw_rules: Mapping[str, object] | None,
) -> dict[str, _ContactEnclosureRule]:
    del pdk
    table: dict[str, _ContactEnclosureRule] = {}
    for cut_layer, value in dict(raw_rules or {}).items():
        if not isinstance(value, Mapping):
            continue
        layer = str(cut_layer or "")
        outer = str(value.get("outer_layer", "") or "")
        if not layer or not outer:
            continue
        try:
            enclosure = max(float(value.get("enclosure_um", 0.0) or 0.0), 0.0)
        except (TypeError, ValueError):
            enclosure = 0.0
        if enclosure <= 0.0:
            continue
        table[layer] = _ContactEnclosureRule(
            layer,
            outer,
            enclosure,
            tuple(str(item) for item in tuple(value.get("rule_families", ()) or ()) if str(item)),
        )
    return table


def _merge_fill_requests(
    requests: list[_FillRequest],
    *,
    merge_same_net_spacing_fill: bool = True,
) -> tuple[_FillRequest, ...]:
    pending = list(requests)
    changed = True
    while changed:
        changed = False
        merged: list[_FillRequest] = []
        while pending:
            current = pending.pop(0)
            overlap_index = next(
                (
                    index for index, row in enumerate(merged)
                    if row.layer == current.layer
                    and row.net == current.net
                    and row.kind == current.kind
                    and (
                        merge_same_net_spacing_fill
                        or current.kind != "same_net_spacing_fill"
                    )
                    and _touches_or_overlaps(row.bbox, current.bbox)
                ),
                None,
            )
            if overlap_index is None:
                merged.append(current)
                continue
            existing = merged.pop(overlap_index)
            merged.append(
                _FillRequest(
                    current.kind,
                    current.layer,
                    current.net,
                    _union((existing.bbox, current.bbox)),
                    tuple(dict.fromkeys((*existing.rules, *current.rules))),
                    tuple(dict.fromkeys((*existing.result_indices, *current.result_indices))),
                    tuple(dict.fromkeys((*existing.shape_ids, *current.shape_ids))),
                )
            )
            changed = True
        pending = merged
    return tuple(sorted(pending, key=lambda row: (row.layer, row.net, row.bbox, row.kind)))


def _single_action_layer_net(plan: object, action: LocalRepairAction) -> tuple[str, str]:
    marker_layer = str(getattr(action.marker, "layer", "") or "")
    nets = {str(net) for net in tuple(action.params.get("nets", ()) or ()) if str(net)}
    layers = {marker_layer} if marker_layer else set()
    for shape_id in tuple(action.target_shape_ids):
        shape = _shape_signature(plan, str(shape_id))
        if shape is None:
            continue
        layer = str(shape.get("layer", "") or "")
        net = str(shape.get("net", "") or "")
        if layer:
            layers.add(layer)
        if net:
            nets.add(net)
    if marker_layer:
        layers = {marker_layer}
    if len(layers) != 1 or len(nets) != 1:
        return "", ""
    return next(iter(layers)), next(iter(nets))


def _shape_signature(plan: object, shape_id: str) -> Mapping[str, object] | None:
    if shape_id.startswith("rect[") and shape_id.endswith("]") and shape_id[5:-1].isdigit():
        index = int(shape_id[5:-1])
        rects = tuple(getattr(plan, "rects", ()))
        if 0 <= index < len(rects):
            rect = rects[index]
            return {
                "layer": str(getattr(rect, "layer", "")),
                "net": str(getattr(rect, "net", "")),
                "bbox": _bbox_tuple(getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0))),
            }
    if shape_id.startswith("path[") and "].segment[" in shape_id and shape_id.endswith("]"):
        left, right = shape_id.split("].segment[", 1)
        path_index_text = left[5:]
        segment_index_text = right[:-1]
        if path_index_text.isdigit() and segment_index_text.isdigit():
            path_index = int(path_index_text)
            segment_index = int(segment_index_text)
            paths = tuple(getattr(plan, "paths", ()))
            if 0 <= path_index < len(paths):
                path = paths[path_index]
                points = tuple(getattr(path, "points", ()) or ())
                if 0 <= segment_index < len(points) - 1:
                    left_point = points[segment_index]
                    right_point = points[segment_index + 1]
                    half = 0.5 * float(getattr(path, "width", 0.0) or 0.0)
                    bbox = (
                        min(float(left_point[0]), float(right_point[0])) - half,
                        min(float(left_point[1]), float(right_point[1])) - half,
                        max(float(left_point[0]), float(right_point[0])) + half,
                        max(float(left_point[1]), float(right_point[1])) + half,
                    )
                    return {"layer": str(getattr(path, "layer", "")), "net": str(getattr(path, "net", "")), "bbox": bbox}
    return None


def _stretch_spacing_fill_bbox_to_same_net_neighbors(
    plan: object,
    bbox: tuple[float, float, float, float],
    layer: str,
    net: str,
    *,
    max_gap_um: float,
) -> tuple[float, float, float, float]:
    """Bridge a spacing-fill bbox to nearby same-net shapes without absorbing long trunks.

    Calibre spacing markers around same-net T junctions often sit in the
    concave notch between two touching/nearby same-net shapes.  Filling only
    the marker polygon can move the notch outward.  This helper stretches the
    proposed fill to the nearest same-layer/same-net edge when the perpendicular
    ranges overlap and the edge gap is bounded by configuration.
    """

    max_gap = max(float(max_gap_um), 0.0)
    if max_gap <= 0.0:
        return bbox
    x0, y0, x1, y1 = bbox
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    shapes = tuple(_same_net_layer_bboxes(plan, layer, net))
    if not shapes:
        return (x0, y0, x1, y1)
    # Repeat because stretching to one edge can bring another same-net neighbor
    # within the configured bridge window.
    for _ in range(4):
        changed = False
        for sx0, sy0, sx1, sy1 in shapes:
            if _interval_overlap(y0, y1, sy0, sy1) > 0.0:
                left_gap = x0 - sx1
                if 0.0 <= left_gap <= max_gap:
                    candidate = (sx1, y0, x1, y1)
                    if not _has_other_net_conflict(plan, candidate, layer, net, 0.0):
                        x0 = sx1
                        changed = True
                right_gap = sx0 - x1
                if 0.0 <= right_gap <= max_gap:
                    candidate = (x0, y0, sx0, y1)
                    if not _has_other_net_conflict(plan, candidate, layer, net, 0.0):
                        x1 = sx0
                        changed = True
            if _interval_overlap(x0, x1, sx0, sx1) > 0.0:
                lower_gap = y0 - sy1
                if 0.0 <= lower_gap <= max_gap:
                    candidate = (x0, sy1, x1, y1)
                    if not _has_other_net_conflict(plan, candidate, layer, net, 0.0):
                        y0 = sy1
                        changed = True
                upper_gap = sy0 - y1
                if 0.0 <= upper_gap <= max_gap:
                    candidate = (x0, y0, x1, sy0)
                    if not _has_other_net_conflict(plan, candidate, layer, net, 0.0):
                        y1 = sy0
                        changed = True
        if not changed:
            break
    return (x0, y0, x1, y1)


def _same_net_layer_bboxes(plan: object, layer: str, net: str) -> tuple[tuple[float, float, float, float], ...]:
    rows: list[tuple[float, float, float, float]] = []
    for rect in tuple(getattr(plan, "rects", ())):
        if str(getattr(rect, "layer", "")) == layer and str(getattr(rect, "net", "")) == net:
            rows.append(_bbox_tuple(getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0))))
    for path in tuple(getattr(plan, "paths", ())):
        if str(getattr(path, "layer", "")) != layer or str(getattr(path, "net", "")) != net:
            continue
        points = tuple(getattr(path, "points", ()) or ())
        half = 0.5 * float(getattr(path, "width", 0.0) or 0.0)
        for left, right in zip(points, points[1:]):
            rows.append(
                (
                    min(float(left[0]), float(right[0])) - half,
                    min(float(left[1]), float(right[1])) - half,
                    max(float(left[0]), float(right[0])) + half,
                    max(float(left[1]), float(right[1])) + half,
                )
            )
    return tuple(rows)


def _interval_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(min(float(a1), float(b1)) - max(float(a0), float(b0)), 0.0)


def _spacing_table(pdk: object | None, override: Mapping[str, float] | None) -> dict[str, float]:
    if override is not None:
        return {str(layer): float(value) for layer, value in dict(override).items()}
    rules = getattr(pdk, "rules", None)
    raw = getattr(rules, "min_spacing_nm", {}) if rules is not None else {}
    return {str(layer): float(value) * 1e-3 for layer, value in dict(raw or {}).items()}


def _marker_padding(pdk: object | None, value: float | None) -> float:
    if value is not None:
        return max(float(value), 0.0)
    rules = getattr(pdk, "rules", None)
    try:
        return max(float(getattr(rules, "grid_step_um", 0.001) or 0.001), 0.001)
    except (TypeError, ValueError):
        return 0.001


def _grow_degenerate_bbox(
    bbox: tuple[float, float, float, float],
    layer: str,
    pdk: object | None,
    *,
    padding_um: float,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    width = max(x1 - x0, 0.0)
    height = max(y1 - y0, 0.0)
    min_span = max(_layer_min_width_um(layer, pdk), 2.0 * padding_um)
    if width < min_span:
        cx = 0.5 * (x0 + x1)
        half = 0.5 * min_span
        x0, x1 = cx - half, cx + half
    if height < min_span:
        cy = 0.5 * (y0 + y1)
        half = 0.5 * min_span
        y0, y1 = cy - half, cy + half
    return (x0, y0, x1, y1)


def _ensure_minimum_bbox_span(
    bbox: tuple[float, float, float, float],
    layer: str,
    pdk: object | None,
) -> tuple[float, float, float, float]:
    return _grow_degenerate_bbox(bbox, layer, pdk, padding_um=0.0)


def _ensure_rule_safe_bbox(
    bbox: tuple[float, float, float, float],
    layer: str,
    pdk: object | None,
) -> tuple[float, float, float, float]:
    """Grow an ECO fill candidate to satisfy local width and area rules.

    Local Calibre spacing markers are often skinny slivers.  Emitting those
    slivers directly can trade an M*.S.* marker for M*.W.* or M*.A.* markers.
    This keeps the candidate local, but makes the emitted rectangle legal under
    the configured PDK scalar metal rules before the other-net spacing veto.
    """

    x0, y0, x1, y1 = _ensure_minimum_bbox_span(bbox, layer, pdk)
    min_area = _layer_min_area_um2(layer, pdk)
    width = max(x1 - x0, 0.0)
    height = max(y1 - y0, 0.0)
    if min_area <= 0.0 or width * height >= min_area - 1e-12 or width <= 0.0 or height <= 0.0:
        return (x0, y0, x1, y1)
    cx = 0.5 * (x0 + x1)
    cy = 0.5 * (y0 + y1)
    if width >= height:
        target_width = max(width, min_area / max(height, 1e-12))
        half = 0.5 * target_width
        return (cx - half, y0, cx + half, y1)
    target_height = max(height, min_area / max(width, 1e-12))
    half = 0.5 * target_height
    return (x0, cy - half, x1, cy + half)


def _rule_safe_bbox_candidates(
    bbox: tuple[float, float, float, float],
    layer: str,
    pdk: object | None,
) -> tuple[tuple[float, float, float, float], ...]:
    """Return legal local fill candidates with alternative area-growth axes.

    Small access-pin min-area markers can sit next to unrelated rails.  Growing
    the marker window in only one hard-coded direction often creates a spacing
    conflict even though the perpendicular direction is legal.  Candidate
    ordering keeps the historical behavior first, then tries the alternate
    axis before giving up to a more invasive reroute.
    """

    snapped = _snap_bbox(bbox, pdk)
    x0, y0, x1, y1 = _ensure_minimum_bbox_span(snapped, layer, pdk)
    min_area = _layer_min_area_um2(layer, pdk)
    rows: list[tuple[float, float, float, float]] = [_ensure_rule_safe_bbox((x0, y0, x1, y1), layer, pdk)]
    width = max(x1 - x0, 0.0)
    height = max(y1 - y0, 0.0)
    if min_area > 0.0 and width > 0.0 and height > 0.0 and width * height < min_area - 1e-12:
        cx = 0.5 * (x0 + x1)
        cy = 0.5 * (y0 + y1)
        target_width = max(width, min_area / max(height, 1e-12))
        target_height = max(height, min_area / max(width, 1e-12))
        half_width = 0.5 * target_width
        half_height = 0.5 * target_height
        rows.extend(
            (
                (cx - half_width, y0, cx + half_width, y1),
                (x0, cy - half_height, x1, cy + half_height),
            )
        )
    unique: list[tuple[float, float, float, float]] = []
    seen: set[tuple[float, float, float, float]] = set()
    for row in rows:
        snapped_row = _snap_bbox(row, pdk)
        if snapped_row in seen:
            continue
        seen.add(snapped_row)
        unique.append(snapped_row)
    return tuple(unique)


def _layer_min_width_um(layer: str, pdk: object | None) -> float:
    rules = getattr(pdk, "rules", None)
    if rules is not None:
        try:
            return max(float(rules.min_width_um(layer)), float(getattr(rules, "grid_step_um", 0.001) or 0.001))
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    return 0.001


def _layer_min_area_um2(layer: str, pdk: object | None) -> float:
    rules = getattr(pdk, "rules", None)
    raw = getattr(rules, "min_area_nm2", {}) if rules is not None else {}
    try:
        return max(float(dict(raw or {}).get(str(layer), 0.0) or 0.0) * 1e-6, 0.0)
    except (TypeError, ValueError):
        return 0.0


def _snap_bbox(bbox: tuple[float, float, float, float], pdk: object | None) -> tuple[float, float, float, float]:
    rules = getattr(pdk, "rules", None)
    if rules is not None and hasattr(rules, "snap_bbox_um"):
        return tuple(float(value) for value in rules.snap_bbox_um(bbox, mode="outward"))
    return bbox


def _snap_point(point: tuple[float, float], pdk: object | None) -> tuple[float, float]:
    rules = getattr(pdk, "rules", None)
    if rules is not None and hasattr(rules, "snap_um"):
        return (float(rules.snap_um(point[0])), float(rules.snap_um(point[1])))
    return point


def _path_bbox(path: object) -> tuple[float, float, float, float]:
    points = tuple(getattr(path, "points", ()) or ())
    if not points:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    half_width = 0.5 * float(getattr(path, "width", 0.0) or 0.0)
    return (min(xs) - half_width, min(ys) - half_width, max(xs) + half_width, max(ys) + half_width)


def _bbox_tuple(value: object) -> tuple[float, float, float, float]:
    row = tuple(float(item) for item in tuple(value))  # type: ignore[arg-type]
    if len(row) != 4:
        raise ValueError("bbox must have four coordinates")
    return row


def _union(boxes: tuple[tuple[float, float, float, float], ...]) -> tuple[float, float, float, float]:
    return (min(row[0] for row in boxes), min(row[1] for row in boxes), max(row[2] for row in boxes), max(row[3] for row in boxes))


def _expand_bbox(bbox: tuple[float, float, float, float], padding: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    value = max(float(padding), 0.0)
    return (x0 - value, y0 - value, x1 + value, y1 + value)


def _touches_or_overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _positive_area(bbox: tuple[float, float, float, float]) -> bool:
    return bbox[2] > bbox[0] and bbox[3] > bbox[1]


def _has_other_net_conflict(
    plan: object,
    bbox: tuple[float, float, float, float],
    layer: str,
    net: str,
    spacing: float,
) -> bool:
    expanded = (bbox[0] - spacing, bbox[1] - spacing, bbox[2] + spacing, bbox[3] + spacing)
    for rect in tuple(getattr(plan, "rects", ())):
        if str(getattr(rect, "layer", "")) != layer or str(getattr(rect, "net", "")) in {"", net}:
            continue
        if _overlaps(expanded, _bbox_tuple(getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0)))):
            return True
    for path in tuple(getattr(plan, "paths", ())):
        if str(getattr(path, "layer", "")) != layer or str(getattr(path, "net", "")) in {"", net}:
            continue
        half = 0.5 * float(getattr(path, "width", 0.0) or 0.0)
        for left, right in zip(tuple(getattr(path, "points", ()) or ()), tuple(getattr(path, "points", ()) or ())[1:]):
            path_box = (
                min(float(left[0]), float(right[0])) - half,
                min(float(left[1]), float(right[1])) - half,
                max(float(left[0]), float(right[0])) + half,
                max(float(left[1]), float(right[1])) + half,
            )
            if _overlaps(expanded, path_box):
                return True
    return False


def _already_covered_by_same_net(plan: object, bbox: tuple[float, float, float, float], layer: str, net: str) -> bool:
    for rect in tuple(getattr(plan, "rects", ())):
        if str(getattr(rect, "layer", "")) == layer and str(getattr(rect, "net", "")) == net:
            if _contains(_bbox_tuple(getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0))), bbox):
                return True
    return False


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _local_drc_eco_config(pdk: object | None, override: Mapping[str, object] | None) -> dict[str, object]:
    cfg: dict[str, object] = {
        "enabled": True,
        "halo_um": 0.02,
        "marker_padding_um": 0.001,
        "enabled_action_kinds": ("remove_short_jog", "add_min_area_patch"),
        "candidate_rule_families": ("G.4", "M*.W.4", "M*.A.*"),
        "same_net_fill_rule_families": (),
        "same_net_spacing_fill_padding_um": 0.0,
        "same_net_spacing_fill_bridge_max_gap_um": 0.0,
        "fill_enforce_min_area": False,
        "width_fill_requires_expandable_shape": False,
        "same_net_spacing_fill_cluster_by_shape": True,
        "same_net_spacing_fill_merge_touching": True,
        "contact_enclosure_rules": {},
        "force_ignore_rule_families": (),
        "force_local_auto_rule_families": (),
        "force_local_smt_rule_families": (),
    }
    metadata = getattr(pdk, "metadata", {}) if pdk is not None else {}
    if isinstance(metadata, Mapping):
        routing_geometry = metadata.get("routing_geometry", {})
        if isinstance(routing_geometry, Mapping):
            raw = routing_geometry.get("local_drc_eco", {})
            if isinstance(raw, Mapping):
                cfg.update(dict(raw))
    if override is not None:
        cfg.update(dict(override))
    if "halo_nm" in cfg:
        cfg["halo_um"] = max(float(cfg.get("halo_nm", 0.0)), 0.0) * 1e-3
    else:
        cfg["halo_um"] = max(float(cfg.get("halo_um", 0.02)), 0.0)
    if "marker_padding_nm" in cfg:
        cfg["marker_padding_um"] = max(float(cfg.get("marker_padding_nm", 0.0)), 0.0) * 1e-3
    else:
        cfg["marker_padding_um"] = max(float(cfg.get("marker_padding_um", 0.001)), 0.0)
    if "same_net_spacing_fill_padding_nm" in cfg:
        cfg["same_net_spacing_fill_padding_um"] = max(float(cfg.get("same_net_spacing_fill_padding_nm", 0.0)), 0.0) * 1e-3
    else:
        cfg["same_net_spacing_fill_padding_um"] = max(float(cfg.get("same_net_spacing_fill_padding_um", 0.0)), 0.0)
    if "same_net_spacing_fill_bridge_max_gap_nm" in cfg:
        cfg["same_net_spacing_fill_bridge_max_gap_um"] = max(float(cfg.get("same_net_spacing_fill_bridge_max_gap_nm", 0.0)), 0.0) * 1e-3
    else:
        cfg["same_net_spacing_fill_bridge_max_gap_um"] = max(float(cfg.get("same_net_spacing_fill_bridge_max_gap_um", 0.0)), 0.0)
    cfg["enabled_action_kinds"] = _tuple_config(cfg.get("enabled_action_kinds", ()))
    cfg["candidate_rule_families"] = _tuple_config(cfg.get("candidate_rule_families", ()))
    cfg["same_net_fill_rule_families"] = _tuple_config(cfg.get("same_net_fill_rule_families", ()))
    cfg["force_ignore_rule_families"] = _tuple_config(cfg.get("force_ignore_rule_families", ()))
    cfg["force_local_auto_rule_families"] = _tuple_config(cfg.get("force_local_auto_rule_families", ()))
    cfg["force_local_smt_rule_families"] = _tuple_config(cfg.get("force_local_smt_rule_families", ()))
    cfg["fill_enforce_min_area"] = bool(cfg.get("fill_enforce_min_area", False))
    cfg["width_fill_requires_expandable_shape"] = bool(cfg.get("width_fill_requires_expandable_shape", False))
    cfg["same_net_spacing_fill_cluster_by_shape"] = bool(cfg.get("same_net_spacing_fill_cluster_by_shape", True))
    cfg["same_net_spacing_fill_merge_touching"] = bool(cfg.get("same_net_spacing_fill_merge_touching", True))
    cfg["contact_enclosure_rules"] = _normalize_contact_enclosure_rules(pdk, cfg.get("contact_enclosure_rules", {}))
    return cfg


def _tuple_config(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        rows = (value,)
    else:
        rows = tuple(value or ())
    return tuple(str(item) for item in rows if str(item))


def _normalize_contact_enclosure_rules(pdk: object | None, raw: object) -> dict[str, dict[str, object]]:
    if not isinstance(raw, Mapping):
        return {}
    rows: dict[str, dict[str, object]] = {}
    for cut_layer, value in dict(raw).items():
        if not isinstance(value, Mapping):
            continue
        layer = str(cut_layer or "")
        outer = str(value.get("outer_layer", value.get("enclosure_layer", "")) or "")
        if not layer or not outer:
            continue
        enclosure = _configured_um(value, "enclosure_um", "enclosure_nm", 0.0)
        if enclosure <= 0.0:
            enclosure = _configured_contact_enclosure_fallback_um(pdk, layer, outer)
        families = tuple(str(item) for item in tuple(value.get("rule_families", value.get("candidate_rule_families", ())) or ()) if str(item))
        rows[layer] = {
            "outer_layer": outer,
            "enclosure_um": max(float(enclosure), 0.0),
            "rule_families": families,
        }
    return rows


def _configured_um(row: Mapping[str, object], um_key: str, nm_key: str, default_um: float) -> float:
    if um_key in row:
        try:
            return max(float(row.get(um_key, default_um) or default_um), 0.0)
        except (TypeError, ValueError):
            return max(float(default_um), 0.0)
    if nm_key in row:
        try:
            return max(float(row.get(nm_key, default_um * 1000.0) or default_um * 1000.0) * 1e-3, 0.0)
        except (TypeError, ValueError):
            return max(float(default_um), 0.0)
    return max(float(default_um), 0.0)


def _configured_contact_enclosure_fallback_um(pdk: object | None, cut_layer: str, outer_layer: str) -> float:
    if pdk is None:
        return 0.0
    try:
        return max(float(pdk.rules.enclosure_um(f"{cut_layer}_{outer_layer}")), 0.0)
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    metadata = getattr(pdk, "metadata", {}) or {}
    if isinstance(metadata, Mapping):
        calibre = metadata.get("calibre", {}) or {}
        if isinstance(calibre, Mapping):
            mos_access = calibre.get("mos_access", {}) or {}
            if isinstance(mos_access, Mapping) and str(cut_layer) == str(getattr(getattr(pdk, "layer_map", None), "contact", "")):
                try:
                    return max(float(mos_access.get("gate_contact_po_enclosure_nm", 0.0) or 0.0) * 1e-3, 0.0)
                except (TypeError, ValueError):
                    return 0.0
    return 0.0


def _configured_local_action(action: LocalRepairAction, config: Mapping[str, object]) -> LocalRepairAction:
    """Convert configured local spacing rules into conservative same-net fills.

    Calibre metal spacing rules normally require push/reroute.  For explicitly
    configured families such as M*.S.* we allow a narrower local operator: if
    marker localization resolves to one routing net/layer, the ECO solver may
    add a same-net fill rectangle over the marker.  The later spacing check
    still rejects the edit near any other-net same-layer geometry.
    """

    if action.owner != "routing" or action.kind != "push_or_reroute":
        return action
    if not _matches_any_family(action.marker.rule, tuple(config.get("same_net_fill_rule_families", ()) or ())):
        return action
    return LocalRepairAction(
        "same_net_spacing_fill",
        action.marker,
        action.target_shape_ids,
        dict(action.params),
        action.requires_global_resolve,
        action.owner,
    )


def _is_candidate_rule(rule: str, config: Mapping[str, object]) -> bool:
    return _matches_any_family(rule, tuple(config.get("candidate_rule_families", ()) or ()))


def _matches_any_family(rule: str, families: tuple[object, ...]) -> bool:
    rows = tuple(str(item).upper() for item in families)
    upper = str(rule).upper()
    return any(_matches_rule_family(upper, family) for family in rows)


def _matches_rule_family(rule: str, family: str) -> bool:
    if family == "G.4":
        return rule.startswith("G.4")
    if family == "M*.W.4":
        return _is_numbered_metal_rule(rule, suffix=".W.4")
    if family == "M*.W.*":
        return _is_numbered_metal_rule(rule, contains=".W.")
    if family == "M*.A.*":
        return _is_numbered_metal_rule(rule, contains=".A.")
    if family == "M*.S.*":
        return _is_numbered_metal_rule(rule, contains=".S.")
    return rule == family or rule.startswith(family.rstrip("*"))


def _is_numbered_metal_rule(rule: str, *, suffix: str | None = None, contains: str | None = None) -> bool:
    head = str(rule).upper().split(":", 1)[0]
    if not head.startswith("M"):
        return False
    metal_number = head[1:].split(".", 1)[0]
    if not metal_number.isdigit():
        return False
    if suffix is not None and not head.endswith(suffix):
        return False
    if contains is not None and contains not in head:
        return False
    return True
