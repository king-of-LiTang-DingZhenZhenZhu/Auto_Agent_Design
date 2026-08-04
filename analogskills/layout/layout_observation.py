"""Generate factual layout constraint/compactness observations.

The observation artifact is deliberately not a diagnosis.  It records
constraints, objectives, selected solver facts, compactness metrics, and
connectivity/routing observations so an agent can analyze them separately.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "layout_constraint_observation/v1"


def build_layout_observation(
    layout_smt: Mapping[str, Any],
    *,
    layout_id: str = "",
    source_files: Mapping[str, str] | None = None,
    routes: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    connectivity: Mapping[str, Any] | None = None,
    oa_layout: Mapping[str, Any] | None = None,
    baseline: Mapping[str, Any] | None = None,
    track_pitch_um: float | None = None,
) -> dict[str, Any]:
    """Build a factual layout observation from SMT/routing/connectivity reports."""

    checks = _mapping(layout_smt.get("checks", {}))
    pitch = _positive_float(track_pitch_um, _positive_float(layout_smt.get("track_pitch_um"), 0.5))
    block = str(layout_smt.get("block", checks.get("block", "")) or "")
    actual_layout_id = str(layout_id or layout_smt.get("layout_id") or block or "layout")
    bbox_tracks = _extract_bbox_tracks(layout_smt)
    bbox_area = bbox_tracks[0] * bbox_tracks[1] if bbox_tracks else None
    estimated_bbox_um = _bbox_um_from_report(layout_smt, bbox_tracks, pitch)
    shape_envelopes = _oa_layout_shape_envelopes(oa_layout, estimated_bbox_um)

    entity_result = _build_entities(layout_smt, pitch, bbox_tracks)
    route_rows = _route_rows(routes)
    compactness = _build_compactness(
        layout_smt,
        entity_result,
        pitch,
        bbox_tracks,
        baseline,
        shape_envelopes=shape_envelopes,
    )
    constraints = _build_constraints(layout_smt, connectivity)
    objectives = _build_objectives(layout_smt, bbox_tracks)
    observations = _build_observations(
        layout_smt,
        entity_result,
        route_rows,
        connectivity,
        compactness=compactness,
        oa_layout=oa_layout,
        placement_bbox_um=estimated_bbox_um,
        shape_envelopes=shape_envelopes,
    )

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "layout_id": actual_layout_id,
        "block": block,
        "unit": {
            "geometry": "um",
            "grid": "tracks",
            "track_pitch_um": pitch,
        },
        "source_files": dict(source_files or {}),
        "summary": {
            "passed": _bool_or_none(layout_smt.get("passed", checks.get("passed"))),
            "bbox_tracks": bbox_tracks,
            "bbox_area_tracks2": bbox_area,
            "bbox_um": estimated_bbox_um,
            "final_layout_bbox_um": shape_envelopes.get("final_layout_bbox_um"),
            "electrical_shape_bbox_um": shape_envelopes.get("electrical_shape_bbox_um"),
            "access_rect_bbox_um": shape_envelopes.get("access_rect_bbox_um"),
            "route_path_bbox_um": shape_envelopes.get("route_path_bbox_um"),
            "solve_backend": checks.get("solve_backend"),
            "smt_verified": _bool_or_none(checks.get("smt_verified")),
        },
        "entities": {
            "devices": entity_result["devices"],
            "groups": entity_result["groups"],
        },
        "constraints": constraints,
        "objectives": objectives,
        "compactness": compactness,
        "observations": observations,
    }
    return _clean_json(result)


def write_layout_observation_json(observation: Mapping[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_clean_json(dict(observation)), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def write_layout_observation_markdown(observation: Mapping[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_layout_observation_markdown(observation), encoding="utf-8")
    return out


def render_layout_observation_markdown(observation: Mapping[str, Any]) -> str:
    """Render a concise Markdown view of the same factual observation."""

    summary = _mapping(observation.get("summary", {}))
    unit = _mapping(observation.get("unit", {}))
    compactness = _mapping(observation.get("compactness", {}))
    global_c = _mapping(compactness.get("global", {}))
    whitespace = _mapping(compactness.get("whitespace", {}))
    baseline_delta = _mapping(compactness.get("baseline_delta", {}))
    observations = tuple(observation.get("observations", ()) or ())

    lines: list[str] = []
    lines.append(f"# Layout Constraint Observation: {observation.get('layout_id', 'layout')}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- schema: `{observation.get('schema_version', '')}`")
    lines.append(f"- block: `{observation.get('block', '')}`")
    lines.append(f"- passed: `{summary.get('passed')}`")
    lines.append(f"- bbox_tracks: `{summary.get('bbox_tracks')}`")
    lines.append(f"- bbox_area_tracks2: `{summary.get('bbox_area_tracks2')}`")
    lines.append(f"- track_pitch_um: `{unit.get('track_pitch_um')}`")
    lines.append(f"- solve_backend: `{summary.get('solve_backend')}`")
    lines.append("")

    lines.append("## Compactness")
    lines.append("")
    compact_rows = [
        ("bbox_tracks", global_c.get("bbox_tracks")),
        ("bbox_area_tracks2", global_c.get("bbox_area_tracks2")),
        ("aspect_ratio", global_c.get("aspect_ratio")),
        ("device_bbox_area_tracks2", global_c.get("device_bbox_area_tracks2")),
        ("device_utilization", global_c.get("device_utilization")),
        ("final_layout_bbox_tracks", global_c.get("final_layout_bbox_tracks")),
        ("final_layout_bbox_area_tracks2", global_c.get("final_layout_bbox_area_tracks2")),
        ("final_device_utilization", global_c.get("final_device_utilization")),
        ("empty_area_tracks2", whitespace.get("empty_area_tracks2")),
        ("empty_area_ratio", whitespace.get("empty_area_ratio")),
        ("largest_empty_rect_tracks", whitespace.get("largest_empty_rect_tracks")),
        ("right_whitespace_tracks", whitespace.get("right_whitespace_tracks")),
        ("top_whitespace_tracks", whitespace.get("top_whitespace_tracks")),
    ]
    lines.extend(_markdown_table(("metric", "actual"), compact_rows))
    lines.append("")

    local_envelopes = tuple(compactness.get("local_envelopes", ()) or ())
    if local_envelopes:
        lines.append("### Local envelopes")
        lines.append("")
        rows = []
        for env in local_envelopes:
            item = _mapping(env)
            rows.append(
                (
                    item.get("id"),
                    item.get("scope"),
                    item.get("bbox_tracks"),
                    item.get("bbox_area_tracks2"),
                    item.get("utilization", item.get("utilization_status")),
                )
            )
        lines.extend(_markdown_table(("id", "scope", "bbox_tracks", "area", "utilization"), rows))
        lines.append("")

    if baseline_delta:
        lines.append("### Baseline delta")
        lines.append("")
        rows = [(key, value) for key, value in baseline_delta.items()]
        lines.extend(_markdown_table(("metric", "actual"), rows))
        lines.append("")

    lines.append("## Constraints")
    lines.append("")
    constraint_rows = []
    for item in tuple(observation.get("constraints", ()) or ()):
        row = _mapping(item)
        actual = _mapping(row.get("actual", {}))
        constraint_rows.append(
            (
                row.get("id"),
                row.get("kind"),
                row.get("scope"),
                row.get("expression"),
                actual.get("status"),
                _compact_actual(actual),
            )
        )
    lines.extend(_markdown_table(("id", "kind", "scope", "expression", "status", "actual"), constraint_rows))
    lines.append("")

    lines.append("## Objectives")
    lines.append("")
    objective_rows = []
    for item in tuple(observation.get("objectives", ()) or ()):
        row = _mapping(item)
        objective_rows.append(
            (
                row.get("id"),
                row.get("kind"),
                row.get("scope"),
                row.get("metric"),
                row.get("sense"),
                row.get("actual"),
            )
        )
    lines.extend(_markdown_table(("id", "kind", "scope", "metric", "sense", "actual"), objective_rows))
    lines.append("")

    lines.append("## Observations")
    lines.append("")
    for item in observations:
        row = _mapping(item)
        lines.append(f"### {row.get('id')}: {row.get('kind')}")
        lines.append("")
        lines.append(f"- scope: `{row.get('scope', '')}`")
        lines.append("")
        data = row.get("data", {})
        if isinstance(data, (Mapping, list, tuple)):
            lines.append("```json")
            lines.append(json.dumps(_clean_json(data), indent=2, sort_keys=True))
            lines.append("```")
        else:
            lines.append(f"`{data}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _build_entities(layout_smt: Mapping[str, Any], pitch: float, bbox_tracks: list[int] | None) -> dict[str, Any]:
    checks = _mapping(layout_smt.get("checks", {}))
    placements = tuple(_mapping(item) for item in tuple(layout_smt.get("placements", ()) or ()))
    device_sizes = _mapping(layout_smt.get("device_sizes_um", checks.get("device_sizes_um", {})))
    devices: dict[str, Any] = {}
    role_to_device_boxes: dict[str, list[list[float]]] = defaultdict(list)
    role_to_origin_boxes: dict[str, list[list[float]]] = defaultdict(list)

    for placement in placements:
        name = str(placement.get("name", "") or "")
        if not name:
            continue
        x = _positive_or_float(placement.get("x_um"), 0.0)
        y = _positive_or_float(placement.get("y_um"), 0.0)
        role = str(placement.get("role", "") or "")
        orient = str(placement.get("orient", "R0") or "R0")
        row: dict[str, Any] = {
            "role": role,
            "origin_um": [_round(x), _round(y)],
            "orient": orient,
        }
        size = _sequence2(device_sizes.get(name))
        if size is not None:
            bbox = [_round(x), _round(y), _round(x + size[0]), _round(y + size[1])]
            row["size_um"] = [_round(size[0]), _round(size[1])]
            row["bbox_um"] = bbox
            row["bbox_tracks"] = _bbox_um_to_tracks(bbox, pitch)
            if role:
                role_to_device_boxes[role].append(bbox)
        if role:
            role_to_origin_boxes[role].append([_round(x), _round(y), _round(x), _round(y)])
        devices[name] = row

    groups: dict[str, Any] = {}
    group_bboxes_tracks = _mapping(layout_smt.get("group_bboxes_tracks", checks.get("group_bboxes_tracks", checks.get("pattern_bboxes_tracks", {}))))
    group_bboxes_um = _mapping(layout_smt.get("group_bboxes_um", checks.get("group_bboxes_um", {})))
    for name, bbox in group_bboxes_tracks.items():
        tb = _sequence4(bbox)
        if tb is None:
            continue
        groups[str(name)] = {
            "bbox_tracks": [int(round(v)) for v in tb],
            "bbox_um": [_round(v * pitch) for v in tb],
            "bbox_source": "group_bboxes_tracks",
        }
    for name, bbox in group_bboxes_um.items():
        ub = _sequence4(bbox)
        if ub is None:
            continue
        row = groups.setdefault(str(name), {})
        row["bbox_um"] = [_round(v) for v in ub]
        row.setdefault("bbox_tracks", _bbox_um_to_tracks(ub, pitch))
        row.setdefault("bbox_source", "group_bboxes_um")

    for role, boxes in role_to_device_boxes.items():
        if role in groups:
            continue
        bbox = _union_bbox(boxes)
        if bbox is not None:
            groups[role] = {
                "bbox_um": bbox,
                "bbox_tracks": _bbox_um_to_tracks(bbox, pitch),
                "bbox_source": "device_bboxes",
            }
    for role, boxes in role_to_origin_boxes.items():
        if role in groups:
            continue
        bbox = _union_bbox(boxes)
        if bbox is not None:
            groups[role] = {
                "bbox_um": bbox,
                "bbox_tracks": _bbox_um_to_tracks(bbox, pitch),
                "bbox_source": "placement_origins_only",
            }

    if bbox_tracks:
        groups.setdefault(
            "__global__",
            {
                "bbox_tracks": [0, 0, int(bbox_tracks[0]), int(bbox_tracks[1])],
                "bbox_um": [0.0, 0.0, _round(bbox_tracks[0] * pitch), _round(bbox_tracks[1] * pitch)],
                "bbox_source": "smt_total_bbox",
            },
        )

    device_bboxes_tracks = [
        row["bbox_tracks"]
        for row in devices.values()
        if isinstance(row, Mapping) and isinstance(row.get("bbox_tracks"), list)
    ]
    return {
        "devices": devices,
        "groups": groups,
        "device_bboxes_tracks": device_bboxes_tracks,
    }


def _build_constraints(layout_smt: Mapping[str, Any], connectivity: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    checks = _mapping(layout_smt.get("checks", {}))
    constraints: list[dict[str, Any]] = []
    overlap_count = int(_number(checks.get("overlap_issue_count"), 0))
    constraints.append(
        {
            "id": "C-P-001",
            "kind": "non_overlap",
            "scope": "placement",
            "expression": "no_pattern_overlap()",
            "hardness": "hard",
            "expected": {"overlap_issue_count": 0},
            "actual": {
                "status": "pass" if overlap_count == 0 else "fail",
                "overlap_issue_count": overlap_count,
            },
            "evidence": tuple(checks.get("overlap_issues", ()) or ()),
        }
    )
    passed = _bool_or_none(layout_smt.get("passed", checks.get("passed")))
    constraints.append(
        {
            "id": "C-S-001",
            "kind": "solver_status",
            "scope": "solver",
            "expression": "layout_smt_passed()",
            "hardness": "hard",
            "expected": {"passed": True},
            "actual": {
                "status": "pass" if passed is True else "fail" if passed is False else "not_available",
                "passed": passed,
                "solve_backend": checks.get("solve_backend"),
                "z3_optimize_result": checks.get("z3_optimize_result"),
            },
            "evidence": tuple(checks.get("issues", ()) or ()),
        }
    )
    if connectivity is None:
        constraints.append(
            {
                "id": "C-R-001",
                "kind": "connectivity",
                "scope": "routing",
                "expression": "no_same_layer_cross_net_contact()",
                "hardness": "hard",
                "expected": {"short_count": 0},
                "actual": {"status": "not_available", "reason": "connectivity_report_not_provided"},
                "evidence": [],
            }
        )
        return constraints

    shorts = tuple(connectivity.get("shorts", ()) or ())
    opens = tuple(connectivity.get("opens", ()) or ())
    actual_passed = _bool_or_none(connectivity.get("passed"))
    constraints.append(
        {
            "id": "C-R-001",
            "kind": "connectivity",
            "scope": "routing",
            "expression": "no_same_layer_cross_net_contact()",
            "hardness": "hard",
            "expected": {"short_count": 0},
            "actual": {
                "status": "pass" if len(shorts) == 0 else "fail",
                "short_count": len(shorts),
                "open_count": len(opens),
                "passed": actual_passed,
            },
            "evidence": list(shorts),
        }
    )
    constraints.append(
        {
            "id": "C-R-002",
            "kind": "connectivity",
            "scope": "routing",
            "expression": "no_open_nets()",
            "hardness": "hard",
            "expected": {"open_count": 0},
            "actual": {
                "status": "pass" if len(opens) == 0 else "fail",
                "open_count": len(opens),
            },
            "evidence": list(opens),
        }
    )
    return constraints


def _build_objectives(layout_smt: Mapping[str, Any], bbox_tracks: list[int] | None) -> list[dict[str, Any]]:
    checks = _mapping(layout_smt.get("checks", {}))
    objectives: list[dict[str, Any]] = []
    if bbox_tracks:
        objectives.append(
            {
                "id": "O-C-001",
                "kind": "compactness",
                "scope": "global",
                "metric": "bbox_area_tracks2",
                "sense": "minimize",
                "actual": {
                    "bbox_tracks": bbox_tracks,
                    "area_tracks2": bbox_tracks[0] * bbox_tracks[1],
                },
            }
        )
        objectives.append(
            {
                "id": "O-C-002",
                "kind": "compactness",
                "scope": "global",
                "metric": "bbox_width_height_tracks",
                "sense": "minimize",
                "actual": {"width_tracks": bbox_tracks[0], "height_tracks": bbox_tracks[1]},
            }
        )
    if "objective_score" in checks:
        objectives.append(
            {
                "id": "O-S-001",
                "kind": "solver_objective",
                "scope": "solver",
                "metric": "objective_score",
                "sense": "minimize",
                "actual": checks.get("objective_score"),
            }
        )
    pack_windows = _mapping(checks.get("pack_windows_tracks", {}))
    for index, (name, window) in enumerate(sorted(pack_windows.items()), start=1):
        row = _mapping(window)
        width = int(_number(row.get("width_tracks"), 0))
        height = int(_number(row.get("height_tracks"), 0))
        objectives.append(
            {
                "id": f"O-P-{index:03d}",
                "kind": "compactness",
                "scope": str(name),
                "metric": "pack_envelope_area_tracks2",
                "sense": "minimize",
                "actual": {
                    "width_tracks": width,
                    "height_tracks": height,
                    "area_tracks2": width * height,
                },
            }
        )
    layout_terms = _mapping(checks.get("layout_objective_terms", {}))
    for index, (name, value) in enumerate(sorted(layout_terms.items()), start=1):
        objectives.append(
            {
                "id": f"O-L-{index:03d}",
                "kind": "layout_quality",
                "scope": str(name),
                "metric": "objective_term_value",
                "sense": "minimize",
                "actual": value,
            }
        )
    hpwl = _mapping(checks.get("critical_hpwl_tracks2_by_net", {}))
    for index, (net, value) in enumerate(sorted(hpwl.items()), start=1):
        objectives.append(
            {
                "id": f"O-R-{index:03d}",
                "kind": "routing",
                "scope": str(net),
                "metric": "critical_hpwl_tracks",
                "sense": "minimize",
                "actual": value,
            }
        )
    return objectives


def _build_compactness(
    layout_smt: Mapping[str, Any],
    entity_result: Mapping[str, Any],
    pitch: float,
    bbox_tracks: list[int] | None,
    baseline: Mapping[str, Any] | None,
    *,
    shape_envelopes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    checks = _mapping(layout_smt.get("checks", {}))
    global_row: dict[str, Any] = {}
    if bbox_tracks:
        area = int(bbox_tracks[0]) * int(bbox_tracks[1])
        global_row.update(
            {
                "bbox_tracks": [int(bbox_tracks[0]), int(bbox_tracks[1])],
                "bbox_area_tracks2": area,
                "aspect_ratio": _round(float(bbox_tracks[0]) / max(float(bbox_tracks[1]), 1e-12), 6),
            }
        )
    device_bboxes_tracks = tuple(entity_result.get("device_bboxes_tracks", ()) or ())
    occupied = _union_area_tracks(device_bboxes_tracks)
    if bbox_tracks and device_bboxes_tracks:
        total_area = max(int(bbox_tracks[0]) * int(bbox_tracks[1]), 1)
        global_row["device_bbox_area_tracks2"] = occupied
        global_row["device_utilization"] = _round(occupied / total_area, 6)
    else:
        global_row["device_bbox_area_status"] = "not_available"
    final_bbox_um = _sequence4(_mapping(shape_envelopes or {}).get("final_layout_bbox_um"))
    if final_bbox_um is not None:
        final_tracks = _bbox_um_to_tracks(final_bbox_um, pitch)
        final_area = _bbox_tracks_area(final_tracks)
        global_row["final_layout_bbox_um"] = [_round(v) for v in final_bbox_um]
        global_row["final_layout_bbox_tracks"] = final_tracks
        global_row["final_layout_bbox_area_tracks2"] = final_area
        if device_bboxes_tracks:
            global_row["final_device_utilization"] = _round(occupied / max(final_area, 1), 6)
            global_row["final_device_utilization_basis"] = "device_bbox_union_area_tracks2 / final_layout_bbox_area_tracks2"

    local_envelopes = []
    pack_windows = _mapping(checks.get("pack_windows_tracks", {}))
    for index, (name, window) in enumerate(sorted(pack_windows.items()), start=1):
        row = _mapping(window)
        width = int(_number(row.get("width_tracks"), 0))
        height = int(_number(row.get("height_tracks"), 0))
        local_envelopes.append(
            {
                "id": f"LC-{index:03d}",
                "scope": str(name),
                "bbox_tracks": [width, height],
                "bbox_area_tracks2": width * height,
                "utilization_status": "not_available",
                "utilization_reason": "pack_members_not_serialized_in_smt_report",
            }
        )

    whitespace = _whitespace_metrics(device_bboxes_tracks, bbox_tracks)
    baseline_delta = _baseline_delta(baseline, bbox_tracks)

    result: dict[str, Any] = {
        "global": global_row,
        "local_envelopes": local_envelopes,
        "whitespace": whitespace,
    }
    if baseline_delta:
        result["baseline_delta"] = baseline_delta
    return result


def _build_observations(
    layout_smt: Mapping[str, Any],
    entity_result: Mapping[str, Any],
    route_rows: Sequence[Mapping[str, Any]],
    connectivity: Mapping[str, Any] | None,
    *,
    compactness: Mapping[str, Any] | None = None,
    oa_layout: Mapping[str, Any] | None = None,
    placement_bbox_um: Sequence[float] | None = None,
    shape_envelopes: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    checks = _mapping(layout_smt.get("checks", {}))
    observations: list[dict[str, Any]] = []
    selected_relations = {
        _strip_relation_key(str(key)): value
        for key, value in _mapping(checks.get("selected_relations", {})).items()
    }
    if selected_relations:
        observations.append(
            {
                "id": "OBS-P-001",
                "kind": "selected_relations",
                "scope": "placement",
                "data": selected_relations,
            }
        )
        relation_geometry = _relation_geometry(selected_relations, _mapping(entity_result.get("groups", {})))
        if relation_geometry:
            observations.append(
                {
                    "id": "OBS-P-002",
                    "kind": "relation_geometry",
                    "scope": "placement",
                    "data": relation_geometry,
                }
            )
    selected_candidates = _mapping(checks.get("selected_candidates", {}))
    if selected_candidates:
        observations.append(
            {
                "id": "OBS-P-003",
                "kind": "selected_candidates",
                "scope": "placement",
                "data": dict(selected_candidates),
            }
        )
    selected_pcell_realizations = _mapping(checks.get("selected_pcell_realizations", {}))
    if selected_pcell_realizations:
        observations.append(
            {
                "id": "OBS-P-007",
                "kind": "selected_pcell_realizations",
                "scope": "placement",
                "data": {
                    str(name): _mapping(row)
                    for name, row in sorted(selected_pcell_realizations.items())
                    if isinstance(row, Mapping)
                },
            }
        )
    shared_sd_pairs = tuple(checks.get("mos_shared_sd_pairs", ()) or ())
    if shared_sd_pairs:
        observations.append(
            {
                "id": "OBS-P-011",
                "kind": "mos_shared_sd_pairs",
                "scope": "placement",
                "data": tuple(
                    _mapping(item)
                    for item in shared_sd_pairs
                    if isinstance(item, Mapping)
                ),
            }
        )
    dsl_relations = tuple(checks.get("dsl_relations", ()) or ())
    if dsl_relations:
        observations.append(
            {
                "id": "OBS-P-005",
                "kind": "declared_relations",
                "scope": "placement",
                "data": tuple(_mapping(item) for item in dsl_relations if isinstance(item, Mapping)),
            }
        )
    dsl_pcell_realization_groups = tuple(checks.get("dsl_pcell_realization_groups", ()) or ())
    if dsl_pcell_realization_groups:
        observations.append(
            {
                "id": "OBS-P-008",
                "kind": "declared_pcell_realization_groups",
                "scope": "placement",
                "data": tuple(
                    _mapping(item)
                    for item in dsl_pcell_realization_groups
                    if isinstance(item, Mapping)
                ),
            }
        )
    dsl_packs = tuple(checks.get("dsl_packs", ()) or ())
    if dsl_packs:
        observations.append(
            {
                "id": "OBS-P-006",
                "kind": "declared_pack_windows",
                "scope": "placement",
                "data": tuple(_mapping(item) for item in dsl_packs if isinstance(item, Mapping)),
            }
        )
    dsl_placement_windows = tuple(checks.get("dsl_placement_windows", ()) or ())
    if dsl_placement_windows:
        observations.append(
            {
                "id": "OBS-P-010",
                "kind": "declared_placement_windows",
                "scope": "placement",
                "data": tuple(_mapping(item) for item in dsl_placement_windows if isinstance(item, Mapping)),
            }
        )
    dsl_objective_terms = tuple(checks.get("dsl_objective_terms", ()) or ())
    if dsl_objective_terms:
        observations.append(
            {
                "id": "OBS-P-009",
                "kind": "declared_layout_objective_terms",
                "scope": "placement",
                "data": tuple(_mapping(item) for item in dsl_objective_terms if isinstance(item, Mapping)),
            }
        )
    dsl_critical_nets = tuple(checks.get("dsl_critical_nets", ()) or ())
    if dsl_critical_nets:
        observations.append(
            {
                "id": "OBS-R-003",
                "kind": "declared_critical_nets",
                "scope": "routing",
                "data": tuple(_mapping(item) for item in dsl_critical_nets if isinstance(item, Mapping)),
            }
        )
    dsl_route_resources = tuple(checks.get("dsl_route_resources", ()) or ())
    if dsl_route_resources:
        observations.append(
            {
                "id": "OBS-R-006",
                "kind": "declared_route_resources",
                "scope": "routing",
                "data": tuple(_mapping(item) for item in dsl_route_resources if isinstance(item, Mapping)),
            }
        )

    placement_counts = Counter(
        str(row.get("role", "") or "unknown")
        for row in _mapping(entity_result.get("devices", {})).values()
        if isinstance(row, Mapping)
    )
    observations.append(
        {
            "id": "OBS-P-004",
            "kind": "placement_summary",
            "scope": "placement",
            "data": {
                "device_count": sum(placement_counts.values()),
                "device_count_by_role": dict(sorted(placement_counts.items())),
                "group_bbox_sources": {
                    name: _mapping(row).get("bbox_source")
                    for name, row in sorted(_mapping(entity_result.get("groups", {})).items())
                },
            },
        }
    )

    observations.append(
        _layout_tweakability_observation(
            layout_smt,
            entity_result,
            route_rows,
            connectivity,
            compactness=compactness,
            shape_envelopes=shape_envelopes,
        )
    )

    observations.append(
        {
            "id": "OBS-S-001",
            "kind": "solver_summary",
            "scope": "solver",
            "data": {
                "solve_backend": checks.get("solve_backend"),
                "smt_mode": checks.get("smt_mode"),
                "smt_solver_timeout_ms": checks.get("smt_solver_timeout_ms", checks.get("solver_timeout_ms")),
                "z3_optimize_result": checks.get("z3_optimize_result"),
                "z3_optimize_timeout_ms": checks.get("z3_optimize_timeout_ms"),
                "candidate_count": checks.get("candidate_count"),
                "relation_choice_count": checks.get("relation_choice_count"),
                "relation_choice_upper_bound": checks.get("relation_choice_upper_bound"),
                "pattern_candidate_combination_upper_bound": checks.get("pattern_candidate_combination_upper_bound"),
                "pattern_choice_mode": checks.get("pattern_choice_mode"),
                "relation_choice_mode": checks.get("relation_choice_mode"),
                "dsl_pack_count": checks.get("dsl_pack_count"),
                "dsl_placement_window_count": checks.get("dsl_placement_window_count"),
                "placement_window_objective": checks.get("placement_window_objective"),
                "dsl_pcell_realization_group_count": checks.get("dsl_pcell_realization_group_count"),
                "selected_pcell_realization_count": checks.get("selected_pcell_realization_count"),
                "pcell_realization_mode": checks.get("pcell_realization_mode"),
            },
        }
    )

    if route_rows:
        observations.append(_route_summary_observation(route_rows))
    route_envelope = _oa_layout_route_envelope_observation(oa_layout, placement_bbox_um, shape_envelopes=shape_envelopes)
    if route_envelope is not None:
        observations.append(route_envelope)
    if connectivity is not None:
        if tuple(connectivity.get("shorts", ()) or ()):
            observations.append(_routing_conflicts_observation(connectivity))
        if route_rows:
            observations.append(_route_resources_observation(route_rows, connectivity))
        observations.append(
            {
                "id": "OBS-R-002",
                "kind": "connectivity_summary",
                "scope": "routing",
                "data": {
                    "passed": _bool_or_none(connectivity.get("passed")),
                    "shape_count": connectivity.get("shape_count"),
                    "short_count": len(tuple(connectivity.get("shorts", ()) or ())),
                    "open_count": len(tuple(connectivity.get("opens", ()) or ())),
                    "shape_count_by_layer": dict(_mapping(connectivity.get("shape_count_by_layer", {}))),
                    "shape_count_by_net": dict(_mapping(connectivity.get("shape_count_by_net", {}))),
                },
            }
        )
    return observations


def _layout_tweakability_observation(
    layout_smt: Mapping[str, Any],
    entity_result: Mapping[str, Any],
    route_rows: Sequence[Mapping[str, Any]],
    connectivity: Mapping[str, Any] | None,
    *,
    compactness: Mapping[str, Any] | None = None,
    shape_envelopes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    checks = _mapping(layout_smt.get("checks", {}))
    compact = _mapping(compactness)
    whitespace = _mapping(compact.get("whitespace", {}))
    selected_candidates = _mapping(checks.get("selected_candidates", {}))
    selected_pcell_realizations = _mapping(checks.get("selected_pcell_realizations", {}))
    shared_sd_pairs = tuple(checks.get("mos_shared_sd_pairs", ()) or ())
    declared_pcell_groups = tuple(checks.get("dsl_pcell_realization_groups", ()) or ())
    declared_route_resources = tuple(checks.get("dsl_route_resources", ()) or ())
    declared_placement_windows = tuple(checks.get("dsl_placement_windows", ()) or ())
    group_rows = _layout_tweakability_group_rows(_mapping(entity_result.get("groups", {})))
    largest_empty_bbox = _sequence4(whitespace.get("largest_empty_rect_bbox_tracks"))
    empty_contacts = _layout_tweakability_empty_rect_contacts(group_rows, largest_empty_bbox)
    group_aesthetic_facts = _layout_tweakability_group_aesthetic_facts(group_rows)
    envelopes = _mapping(shape_envelopes)
    route_bbox = envelopes.get("route_path_bbox_um")
    final_bbox = envelopes.get("final_layout_bbox_um")

    return {
        "id": "OBS-T-001",
        "kind": "layout_tweakability_facts",
        "scope": "agent_tweak",
        "data": {
            "schema_version": "layout_tweakability_facts/v1",
            "non_prescriptive": True,
            "output_artifact": "agent_layout_tweak_patch.json",
            "operation_classes": (
                {
                    "op": "pattern_candidate",
                    "target_scope": "pattern",
                    "input_facts": (
                        "observations.OBS-P-003.selected_candidates",
                        "observations.OBS-P-007.selected_pcell_realizations",
                        "observations.OBS-P-011.mos_shared_sd_pairs",
                        "observations.OBS-P-008.declared_pcell_realization_groups",
                        "entities.groups",
                    ),
                    "apply_stage": "dsl_before_smt",
                    "solver_scope": "global_smt",
                    "direct_geometry_mutation": False,
                },
                {
                    "op": "compact_gap",
                    "target_scope": "pattern_pair_or_group_pair",
                    "input_facts": (
                        "compactness.whitespace",
                        "entities.groups",
                        "observations.OBS-P-002.relation_geometry",
                    ),
                    "apply_stage": "dsl_before_smt",
                    "solver_scope": "global_smt",
                    "direct_geometry_mutation": False,
                },
                {
                    "op": "align_edge",
                    "target_scope": "pattern_pair",
                    "input_facts": (
                        "entities.groups.*.bbox_tracks",
                        "objectives",
                    ),
                    "apply_stage": "dsl_before_smt",
                    "solver_scope": "global_smt",
                    "direct_geometry_mutation": False,
                },
                {
                    "op": "nudge",
                    "target_scope": "pattern_or_group",
                    "input_facts": (
                        "entities.groups.*.bbox_tracks",
                        "observations.OBS-T-001.data.groups.*.origin_tracks",
                        "compactness.whitespace.largest_empty_rect_bbox_tracks",
                    ),
                    "apply_stage": "local_placement_after_smt",
                    "solver_scope": "global_smt_or_local_smt",
                    "direct_geometry_mutation": False,
                },
                {
                    "op": "placement_window",
                    "target_scope": "pattern_or_group",
                    "input_facts": (
                        "observations.OBS-T-001.data.groups.*.origin_tracks",
                        "observations.OBS-T-001.data.largest_empty_rect_contacts_by_group",
                        "observations.OBS-P-010.declared_placement_windows",
                    ),
                    "apply_stage": "dsl_before_smt",
                    "solver_scope": "global_smt",
                    "direct_geometry_mutation": False,
                },
                {
                    "op": "route_lane",
                    "target_scope": "route_resource",
                    "input_facts": (
                        "observations.OBS-R-006.declared_route_resources",
                        "observations.OBS-R-007.route_envelope",
                        "observations.OBS-R-004.route_resources",
                    ),
                    "apply_stage": "routing_after_access",
                    "solver_scope": "routing_smt_or_astar",
                    "direct_geometry_mutation": False,
                },
            ),
            "solver_visible_inputs": {
                "selected_pattern_candidates_available": bool(selected_candidates),
                "selected_pcell_realizations_available": bool(selected_pcell_realizations),
                "shared_sd_pair_facts_available": bool(shared_sd_pairs),
                "declared_pcell_realization_groups_available": bool(declared_pcell_groups),
                "group_bboxes_available": bool(group_rows),
                "declared_placement_windows_available": bool(declared_placement_windows),
                "route_rows_available": bool(route_rows),
                "declared_route_resources_available": bool(declared_route_resources),
                "route_envelope_available": route_bbox is not None,
                "final_shape_envelope_available": final_bbox is not None,
            },
            "largest_empty_rect": {
                "status": whitespace.get("status"),
                "bbox_tracks": whitespace.get("largest_empty_rect_bbox_tracks"),
                "size_tracks": whitespace.get("largest_empty_rect_tracks"),
                "area_tracks2": whitespace.get("largest_empty_rect_area_tracks2"),
            },
            "global_whitespace": {
                "empty_area_ratio": whitespace.get("empty_area_ratio"),
                "right_whitespace_tracks": whitespace.get("right_whitespace_tracks"),
                "top_whitespace_tracks": whitespace.get("top_whitespace_tracks"),
            },
            "groups": group_rows,
            "group_aesthetic_facts": group_aesthetic_facts,
            "largest_empty_rect_contacts_by_group": empty_contacts,
            "route_envelope": {
                "final_layout_bbox_um": final_bbox,
                "route_path_bbox_um": route_bbox,
                "access_rect_bbox_um": envelopes.get("access_rect_bbox_um"),
            },
            "connectivity_available": connectivity is not None,
            "required_acceptance": {
                "hard_constraints": "pass_or_unchanged",
                "max_bbox_area_regression_ratio": 0.02,
                "direct_geometry_mutation": False,
                "compare_against_baseline": True,
            },
        },
    }


def _layout_tweakability_group_rows(groups: Mapping[str, Any]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for name, row_obj in sorted(groups.items()):
        if str(name) == "__global__":
            continue
        row = _mapping(row_obj)
        bbox = _sequence4(row.get("bbox_tracks"))
        out: dict[str, Any] = {
            "bbox_source": row.get("bbox_source"),
            "bbox_tracks": None,
            "width_tracks": None,
            "height_tracks": None,
        }
        if bbox is not None:
            x0, y0, x1, y1 = [int(round(value)) for value in bbox]
            out.update(
                {
                    "bbox_tracks": [x0, y0, x1, y1],
                    "origin_tracks": [x0, y0],
                    "center2_tracks": [x0 + x1, y0 + y1],
                    "width_tracks": max(0, x1 - x0),
                    "height_tracks": max(0, y1 - y0),
                    "area_tracks2": max(0, x1 - x0) * max(0, y1 - y0),
                }
            )
        bbox_um = _sequence4(row.get("bbox_um"))
        if bbox_um is not None:
            out["bbox_um"] = [_round(value) for value in bbox_um]
        rows[str(name)] = out
    return rows


def _layout_tweakability_group_aesthetic_facts(group_rows: Mapping[str, Any]) -> dict[str, Any]:
    boxes: dict[str, tuple[int, int, int, int]] = {}
    for name, row_obj in sorted(group_rows.items()):
        bbox = _sequence4(_mapping(row_obj).get("bbox_tracks"))
        if bbox is None:
            continue
        x0, y0, x1, y1 = [int(round(value)) for value in bbox]
        if x1 <= x0 or y1 <= y0:
            continue
        boxes[str(name)] = (x0, y0, x1, y1)
    if not boxes:
        return {
            "schema_version": "layout_tweakability_group_aesthetic_facts/v1",
            "status": "not_available",
            "reason": "group_bboxes_unavailable",
        }
    gx0 = min(box[0] for box in boxes.values())
    gy0 = min(box[1] for box in boxes.values())
    gx1 = max(box[2] for box in boxes.values())
    gy1 = max(box[3] for box in boxes.values())
    global_bbox = (gx0, gy0, gx1, gy1)
    global_center2 = (gx0 + gx1, gy0 + gy1)
    group_facts: dict[str, Any] = {}
    for name, (x0, y0, x1, y1) in boxes.items():
        width = x1 - x0
        height = y1 - y0
        center2 = (x0 + x1, y0 + y1)
        group_facts[name] = {
            "bbox_tracks": [x0, y0, x1, y1],
            "center2_tracks": [center2[0], center2[1]],
            "center2_delta_from_global_center_tracks": [
                center2[0] - global_center2[0],
                center2[1] - global_center2[1],
            ],
            "edge_margin_tracks": {
                "left": x0 - gx0,
                "right": gx1 - x1,
                "bottom": y0 - gy0,
                "top": gy1 - y1,
            },
            "width_tracks": width,
            "height_tracks": height,
            "area_tracks2": width * height,
            "aspect_ratio": _round(width / max(height, 1), 6),
        }
    return {
        "schema_version": "layout_tweakability_group_aesthetic_facts/v1",
        "status": "pass",
        "global_bbox_tracks": [gx0, gy0, gx1, gy1],
        "global_center2_tracks": [global_center2[0], global_center2[1]],
        "group_count": len(group_facts),
        "groups": group_facts,
        "edge_alignment_clusters": {
            "x0": _coordinate_clusters({name: box[0] for name, box in boxes.items()}),
            "x1": _coordinate_clusters({name: box[2] for name, box in boxes.items()}),
            "center_x2": _coordinate_clusters({name: box[0] + box[2] for name, box in boxes.items()}),
            "y0": _coordinate_clusters({name: box[1] for name, box in boxes.items()}),
            "y1": _coordinate_clusters({name: box[3] for name, box in boxes.items()}),
            "center_y2": _coordinate_clusters({name: box[1] + box[3] for name, box in boxes.items()}),
        },
        "nearest_pairs": _nearest_group_pairs(boxes),
    }


def _coordinate_clusters(values_by_name: Mapping[str, int], *, tolerance_tracks: int = 2) -> list[dict[str, Any]]:
    rows = sorted((int(value), str(name)) for name, value in values_by_name.items())
    clusters: list[list[tuple[int, str]]] = []
    for value, name in rows:
        if not clusters or abs(value - clusters[-1][-1][0]) > max(0, int(tolerance_tracks)):
            clusters.append([(value, name)])
        else:
            clusters[-1].append((value, name))
    result = []
    for cluster in clusters:
        coords = [value for value, _ in cluster]
        names = [name for _, name in cluster]
        result.append(
            {
                "coord_min_tracks": min(coords),
                "coord_max_tracks": max(coords),
                "coord_mean_tracks": _round(sum(coords) / max(len(coords), 1), 6),
                "groups": names,
                "group_count": len(names),
            }
        )
    return result


def _nearest_group_pairs(boxes: Mapping[str, tuple[int, int, int, int]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    names = sorted(boxes)
    for idx, left_name in enumerate(names):
        lx0, ly0, lx1, ly1 = boxes[left_name]
        for right_name in names[idx + 1 :]:
            rx0, ry0, rx1, ry1 = boxes[right_name]
            gap_x = max(0, max(rx0 - lx1, lx0 - rx1))
            gap_y = max(0, max(ry0 - ly1, ly0 - ry1))
            overlap_x = max(0, min(lx1, rx1) - max(lx0, rx0))
            overlap_y = max(0, min(ly1, ry1) - max(ly0, ry0))
            rows.append(
                {
                    "source": left_name,
                    "target": right_name,
                    "gap_x_tracks": gap_x,
                    "gap_y_tracks": gap_y,
                    "overlap_x_tracks": overlap_x,
                    "overlap_y_tracks": overlap_y,
                    "manhattan_gap_tracks": gap_x + gap_y,
                }
            )
    rows.sort(key=lambda row: (int(row["manhattan_gap_tracks"]), str(row["source"]), str(row["target"])))
    return rows[:16]


def _layout_tweakability_empty_rect_contacts(
    group_rows: Mapping[str, Any],
    empty_bbox: Sequence[float] | None,
) -> dict[str, Any]:
    eb = _sequence4(empty_bbox)
    if eb is None:
        return {}
    ex0, ey0, ex1, ey1 = [int(round(value)) for value in eb]
    result: dict[str, Any] = {}
    for name, row_obj in sorted(group_rows.items()):
        row = _mapping(row_obj)
        bbox = _sequence4(row.get("bbox_tracks"))
        if bbox is None:
            continue
        x0, y0, x1, y1 = [int(round(value)) for value in bbox]
        overlap_x = max(0, min(x1, ex1) - max(x0, ex0))
        overlap_y = max(0, min(y1, ey1) - max(y0, ey0))
        result[str(name)] = {
            "group_bbox_tracks": [x0, y0, x1, y1],
            "empty_rect_bbox_tracks": [ex0, ey0, ex1, ey1],
            "overlap_x_tracks": overlap_x,
            "overlap_y_tracks": overlap_y,
            "signed_gap_tracks": {
                "group_right_to_empty_left": ex0 - x1,
                "empty_right_to_group_left": x0 - ex1,
                "group_top_to_empty_bottom": ey0 - y1,
                "empty_top_to_group_bottom": y0 - ey1,
            },
        }
    return result


def _oa_layout_route_envelope_observation(
    oa_layout: Mapping[str, Any] | None,
    placement_bbox_um: Sequence[float] | None,
    *,
    shape_envelopes: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    envelopes = _mapping(shape_envelopes) or _oa_layout_shape_envelopes(oa_layout, placement_bbox_um)
    if not envelopes:
        return None
    return {
        "id": "OBS-R-007",
        "kind": "route_envelope",
        "scope": "routing",
        "data": {
            "placement_bbox_um": envelopes.get("placement_bbox_um"),
            "route_path_bbox_um": envelopes.get("route_path_bbox_um"),
            "access_rect_bbox_um": envelopes.get("access_rect_bbox_um"),
            "electrical_shape_bbox_um": envelopes.get("electrical_shape_bbox_um"),
            "all_shape_bbox_um": envelopes.get("all_shape_bbox_um"),
            "final_layout_bbox_um": envelopes.get("final_layout_bbox_um"),
            "route_expansion_vs_placement_um": envelopes.get("route_expansion_vs_placement_um"),
            "access_expansion_vs_placement_um": envelopes.get("access_expansion_vs_placement_um"),
            "electrical_shape_expansion_vs_placement_um": envelopes.get("electrical_shape_expansion_vs_placement_um"),
            "all_shape_expansion_vs_placement_um": envelopes.get("all_shape_expansion_vs_placement_um"),
            "final_layout_expansion_vs_placement_um": envelopes.get("final_layout_expansion_vs_placement_um"),
            "path_count": envelopes.get("path_count"),
            "rect_count": envelopes.get("rect_count"),
            "access_rect_count": envelopes.get("access_rect_count"),
            "electrical_rect_count": envelopes.get("electrical_rect_count"),
            "routes_by_net": envelopes.get("routes_by_net", {}),
            "access_rect_count_by_kind": envelopes.get("access_rect_count_by_kind", {}),
            "access_rect_count_by_layer": envelopes.get("access_rect_count_by_layer", {}),
            "access_rect_count_by_net": envelopes.get("access_rect_count_by_net", {}),
            "rect_count_by_category": envelopes.get("rect_count_by_category", {}),
        },
    }


def _oa_layout_shape_envelopes(
    oa_layout: Mapping[str, Any] | None,
    placement_bbox_um: Sequence[float] | None,
) -> dict[str, Any]:
    if not isinstance(oa_layout, Mapping):
        return {}
    placement_bbox = _float_bbox4(placement_bbox_um)
    path_rows = tuple(_mapping(row) for row in tuple(oa_layout.get("paths", ()) or ()))
    rect_rows = tuple(_mapping(row) for row in tuple(oa_layout.get("rects", ()) or ()))
    path_bboxes = tuple(bbox for bbox in (_oa_path_bbox_um(row) for row in path_rows) if bbox is not None)
    rect_bboxes = tuple(bbox for bbox in (_float_bbox4(row.get("bbox")) for row in rect_rows) if bbox is not None)
    route_bbox = _merge_float_bboxes(path_bboxes)
    all_shape_bbox = _merge_float_bboxes((*path_bboxes, *rect_bboxes))
    if route_bbox is None and all_shape_bbox is None:
        return {}

    route_by_net: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
    path_count_by_net: Counter[str] = Counter()
    for row, bbox in zip(path_rows, (_oa_path_bbox_um(row) for row in path_rows)):
        net = str(row.get("net", "") or "")
        if not net or bbox is None:
            continue
        route_by_net[net].append(bbox)
        path_count_by_net[net] += 1
    by_net: dict[str, Any] = {}
    for net, bboxes in sorted(route_by_net.items()):
        bbox = _merge_float_bboxes(tuple(bboxes))
        if bbox is None:
            continue
        by_net[net] = {
            "path_count": int(path_count_by_net.get(net, 0)),
            "bbox_um": bbox,
            "expansion_vs_placement_um": _bbox_expansion_um(placement_bbox, bbox),
            "max_abs_protrusion_um": _max_abs_protrusion_um(placement_bbox, bbox),
        }

    access_bboxes: list[tuple[float, float, float, float]] = []
    electrical_rect_bboxes: list[tuple[float, float, float, float]] = []
    access_count_by_kind: Counter[str] = Counter()
    access_count_by_layer: Counter[str] = Counter()
    access_count_by_net: Counter[str] = Counter()
    rect_count_by_category: Counter[str] = Counter()
    electrical_rect_count = 0
    for row in rect_rows:
        bbox = _float_bbox4(row.get("bbox"))
        if bbox is None:
            continue
        category = _oa_rect_category(row)
        rect_count_by_category[category] += 1
        if category == "access":
            access_bboxes.append(bbox)
            access_kind = _oa_rect_access_kind(row)
            layer = str(row.get("layer", "") or "")
            net = str(row.get("net", "") or "")
            access_count_by_kind[access_kind] += 1
            if layer:
                access_count_by_layer[layer] += 1
            if net:
                access_count_by_net[net] += 1
        if category in {"access", "electrical_rect"}:
            electrical_rect_bboxes.append(bbox)
            electrical_rect_count += 1
    access_bbox = _merge_float_bboxes(tuple(access_bboxes))
    electrical_rect_bbox = _merge_float_bboxes(tuple(electrical_rect_bboxes))
    electrical_shape_bbox = _merge_float_bboxes(tuple(bbox for bbox in (*path_bboxes, *electrical_rect_bboxes) if bbox is not None))
    final_layout_bbox = _merge_float_bboxes(
        tuple(bbox for bbox in (placement_bbox, route_bbox, access_bbox, electrical_rect_bbox) if bbox is not None)
    )
    return {
        "placement_bbox_um": placement_bbox,
        "route_path_bbox_um": route_bbox,
        "access_rect_bbox_um": access_bbox,
        "electrical_rect_bbox_um": electrical_rect_bbox,
        "electrical_shape_bbox_um": electrical_shape_bbox,
        "all_shape_bbox_um": all_shape_bbox,
        "final_layout_bbox_um": final_layout_bbox,
        "route_expansion_vs_placement_um": _bbox_expansion_um(placement_bbox, route_bbox),
        "access_expansion_vs_placement_um": _bbox_expansion_um(placement_bbox, access_bbox),
        "electrical_shape_expansion_vs_placement_um": _bbox_expansion_um(placement_bbox, electrical_shape_bbox),
        "all_shape_expansion_vs_placement_um": _bbox_expansion_um(placement_bbox, all_shape_bbox),
        "final_layout_expansion_vs_placement_um": _bbox_expansion_um(placement_bbox, final_layout_bbox),
        "path_count": len(path_rows),
        "rect_count": len(rect_rows),
        "access_rect_count": len(access_bboxes),
        "electrical_rect_count": electrical_rect_count,
        "routes_by_net": by_net,
        "access_rect_count_by_kind": dict(sorted(access_count_by_kind.items())),
        "access_rect_count_by_layer": dict(sorted(access_count_by_layer.items())),
        "access_rect_count_by_net": dict(sorted(access_count_by_net.items())),
        "rect_count_by_category": dict(sorted(rect_count_by_category.items())),
    }


def _route_summary_observation(route_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_net: dict[str, dict[str, Any]] = {}
    layers = Counter()
    for row in route_rows:
        net = str(row.get("net", "") or "")
        if not net:
            continue
        layer = str(row.get("layer", "") or "")
        if layer:
            layers[layer] += 1
        by_net[net] = {
            "layer": layer,
            "lane": row.get("lane"),
            "width_um": row.get("width_um", row.get("requested_width_um")),
            "path_count": row.get("path_count"),
            "anchor_count": row.get("anchor_count"),
            "corridors": row.get("corridors", ()),
            "route_resource": row.get("route_resource"),
        }
    return {
        "id": "OBS-R-001",
        "kind": "route_summary",
        "scope": "routing",
        "data": {
            "route_count": len(route_rows),
            "route_count_by_layer": dict(sorted(layers.items())),
            "routes_by_net": by_net,
        },
    }


def _routing_conflicts_observation(connectivity: Mapping[str, Any]) -> dict[str, Any]:
    shorts = tuple(_mapping(item) for item in tuple(connectivity.get("shorts", ()) or ()) if isinstance(item, Mapping))
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for short in shorts:
        layer = str(short.get("layer", "") or "")
        net_a = str(short.get("net_a", "") or "")
        net_b = str(short.get("net_b", "") or "")
        if not layer or not net_a or not net_b:
            continue
        net_pair = tuple(sorted((net_a, net_b)))
        key = (layer, net_pair[0], net_pair[1])
        row = grouped.setdefault(
            key,
            {
                "layer": layer,
                "net_pair": [net_pair[0], net_pair[1]],
                "count": 0,
                "source_kind_pair_count": Counter(),
                "union_bbox_um": None,
                "evidence": [],
            },
        )
        row["count"] += 1
        source_a = str(short.get("source_a", "") or "")
        source_b = str(short.get("source_b", "") or "")
        kind_pair = tuple(sorted((_source_kind(source_a), _source_kind(source_b))))
        row["source_kind_pair_count"][f"{kind_pair[0]}:{kind_pair[1]}"] += 1
        row["union_bbox_um"] = _union_optional_bbox(
            _sequence4(row.get("union_bbox_um")),
            _sequence4(short.get("bbox_a")),
            _sequence4(short.get("bbox_b")),
        )
        row["evidence"].append(
            {
                "source_a": source_a,
                "source_b": source_b,
                "source_kind_pair": [_source_kind(source_a), _source_kind(source_b)],
                "bbox_a_um": _round_bbox(short.get("bbox_a")),
                "bbox_b_um": _round_bbox(short.get("bbox_b")),
            }
        )

    rows = []
    for row in sorted(grouped.values(), key=lambda item: (str(item["layer"]), tuple(item["net_pair"]))):
        rows.append(
            {
                "layer": row["layer"],
                "net_pair": row["net_pair"],
                "count": row["count"],
                "source_kind_pair_count": dict(sorted(row["source_kind_pair_count"].items())),
                "union_bbox_um": _round_bbox(row["union_bbox_um"]),
                "evidence": row["evidence"],
            }
        )
    return {
        "id": "OBS-R-004",
        "kind": "routing_conflicts",
        "scope": "routing",
        "data": {
            "short_count": len(shorts),
            "conflict_count": len(rows),
            "conflicts": rows,
        },
    }


def _route_resources_observation(
    route_rows: Sequence[Mapping[str, Any]],
    connectivity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    by_net = _route_resources_by_net(route_rows)
    shorts = tuple(_mapping(item) for item in tuple(_mapping(connectivity).get("shorts", ()) or ()) if isinstance(item, Mapping))
    conflict_count_by_layer = Counter()
    conflict_count_by_layer_net_pair = Counter()
    conflict_resources_by_net: dict[str, dict[str, Any]] = {}
    for short in shorts:
        layer = str(short.get("layer", "") or "")
        net_a = str(short.get("net_a", "") or "")
        net_b = str(short.get("net_b", "") or "")
        if not layer or not net_a or not net_b:
            continue
        pair = tuple(sorted((net_a, net_b)))
        conflict_count_by_layer[layer] += 1
        conflict_count_by_layer_net_pair[f"{layer}:{pair[0]}:{pair[1]}"] += 1
        for net, other in ((net_a, net_b), (net_b, net_a)):
            row = conflict_resources_by_net.setdefault(
                net,
                {
                    "route_layer": by_net.get(net, {}).get("layer"),
                    "route_lane": by_net.get(net, {}).get("lane"),
                    "short_count": 0,
                    "short_count_by_layer": Counter(),
                    "short_count_by_other_net": Counter(),
                    "source_kind_count": Counter(),
                },
            )
            row["short_count"] += 1
            row["short_count_by_layer"][layer] += 1
            row["short_count_by_other_net"][other] += 1
            source = str(short.get("source_a" if net == net_a else "source_b", "") or "")
            row["source_kind_count"][_source_kind(source)] += 1

    clean_conflict_resources: dict[str, dict[str, Any]] = {}
    for net, row in sorted(conflict_resources_by_net.items()):
        clean_conflict_resources[net] = {
            "route_layer": row.get("route_layer"),
            "route_lane": row.get("route_lane"),
            "short_count": row.get("short_count"),
            "short_count_by_layer": dict(sorted(row["short_count_by_layer"].items())),
            "short_count_by_other_net": dict(sorted(row["short_count_by_other_net"].items())),
            "source_kind_count": dict(sorted(row["source_kind_count"].items())),
        }
    return {
        "id": "OBS-R-005",
        "kind": "route_resources",
        "scope": "routing",
        "data": {
            "by_net": by_net,
            "conflict_count_by_layer": dict(sorted(conflict_count_by_layer.items())),
            "conflict_count_by_layer_net_pair": dict(sorted(conflict_count_by_layer_net_pair.items())),
            "conflict_resources_by_net": clean_conflict_resources,
        },
    }


def _route_resources_by_net(route_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    by_net: dict[str, dict[str, Any]] = {}
    for row in route_rows:
        net = str(row.get("net", "") or "")
        if not net:
            continue
        by_net[net] = {
            "layer": row.get("layer"),
            "lane": row.get("lane"),
            "width_um": row.get("width_um", row.get("requested_width_um")),
            "path_count": row.get("path_count"),
            "anchor_count": row.get("anchor_count"),
            "corridors": tuple(row.get("corridors", ()) or ()),
            "route_resource": row.get("route_resource"),
        }
    return dict(sorted(by_net.items()))


def _relation_geometry(selected_relations: Mapping[str, Any], groups: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for edge, relation in sorted(selected_relations.items()):
        if "->" not in edge:
            continue
        source, target = edge.split("->", 1)
        source_group = _mapping(groups.get(source, {}))
        target_group = _mapping(groups.get(target, {}))
        sb = _sequence4(source_group.get("bbox_tracks"))
        tb = _sequence4(target_group.get("bbox_tracks"))
        if sb is None or tb is None:
            rows.append(
                {
                    "source": source,
                    "target": target,
                    "actual_relation": relation,
                    "geometry_status": "not_available",
                    "reason": "group_bbox_unavailable",
                }
            )
            continue
        row = _relation_geometry_row(source, target, str(relation), sb, tb)
        row["source_bbox_source"] = source_group.get("bbox_source")
        row["target_bbox_source"] = target_group.get("bbox_source")
        if "placement_origins_only" in {row.get("source_bbox_source"), row.get("target_bbox_source")}:
            row["geometry_status"] = "estimated_from_placement_origins"
        rows.append(row)
    return rows


def _relation_geometry_row(source: str, target: str, relation: str, sb: Sequence[float], tb: Sequence[float]) -> dict[str, Any]:
    sx0, sy0, sx1, sy1 = sb
    tx0, ty0, tx1, ty1 = tb
    row: dict[str, Any] = {
        "source": source,
        "target": target,
        "actual_relation": relation,
        "source_bbox_tracks": [int(round(v)) for v in sb],
        "target_bbox_tracks": [int(round(v)) for v in tb],
    }
    kind = relation.lower()
    if kind == "right_of":
        row["actual_gap_tracks"] = int(round(tx0 - sx1))
        row["axis"] = "x"
    elif kind == "left_of":
        row["actual_gap_tracks"] = int(round(sx0 - tx1))
        row["axis"] = "x"
    elif kind == "above":
        row["actual_gap_tracks"] = int(round(ty0 - sy1))
        row["axis"] = "y"
    elif kind == "below":
        row["actual_gap_tracks"] = int(round(sy0 - ty1))
        row["axis"] = "y"
    elif kind == "overlap_x":
        row["actual_overlap_tracks"] = int(round(min(sx1, tx1) - max(sx0, tx0)))
        row["axis"] = "x"
    elif kind == "overlap_y":
        row["actual_overlap_tracks"] = int(round(min(sy1, ty1) - max(sy0, ty0)))
        row["axis"] = "y"
    else:
        row["geometry_status"] = "not_available"
        row["reason"] = "unsupported_relation_kind"
    return row


def _whitespace_metrics(device_bboxes_tracks: Sequence[Sequence[int]], bbox_tracks: list[int] | None) -> dict[str, Any]:
    if not bbox_tracks or not device_bboxes_tracks:
        return {
            "status": "not_available",
            "reason": "device_bboxes_unavailable",
        }
    width = int(bbox_tracks[0])
    height = int(bbox_tracks[1])
    if width <= 0 or height <= 0 or width > 2000 or height > 2000:
        return {
            "status": "not_available",
            "reason": "bbox_grid_too_large_or_invalid",
            "bbox_tracks": bbox_tracks,
        }
    grid = [[False for _ in range(width)] for _ in range(height)]
    for raw in device_bboxes_tracks:
        bbox = _sequence4(raw)
        if bbox is None:
            continue
        x0, y0, x1, y1 = [int(round(v)) for v in bbox]
        x0 = max(0, min(width, x0))
        x1 = max(0, min(width, x1))
        y0 = max(0, min(height, y0))
        y1 = max(0, min(height, y1))
        if x1 <= x0:
            x1 = min(width, x0 + 1)
        if y1 <= y0:
            y1 = min(height, y0 + 1)
        for y in range(y0, y1):
            row = grid[y]
            for x in range(x0, x1):
                row[x] = True
    occupied = sum(1 for row in grid for cell in row if cell)
    total = width * height
    largest = _largest_empty_rectangle(grid)
    empty_cols = _empty_column_ranges(grid)
    empty_rows = _empty_row_ranges(grid)
    max_x = 0
    max_y = 0
    for raw in device_bboxes_tracks:
        bbox = _sequence4(raw)
        if bbox is None:
            continue
        max_x = max(max_x, int(round(bbox[2])))
        max_y = max(max_y, int(round(bbox[3])))
    return {
        "status": "pass",
        "empty_area_tracks2": total - occupied,
        "empty_area_ratio": _round((total - occupied) / max(total, 1), 6),
        "largest_empty_rect_tracks": [largest["width_tracks"], largest["height_tracks"]],
        "largest_empty_rect_area_tracks2": largest["area_tracks2"],
        "largest_empty_rect_bbox_tracks": largest["bbox_tracks"],
        "right_whitespace_tracks": max(0, width - max_x),
        "top_whitespace_tracks": max(0, height - max_y),
        "empty_columns": empty_cols[:8],
        "empty_rows": empty_rows[:8],
    }


def _largest_empty_rectangle(grid: Sequence[Sequence[bool]]) -> dict[str, Any]:
    height = len(grid)
    width = len(grid[0]) if height else 0
    hist = [0] * width
    best_area = 0
    best = (0, 0, 0, 0)
    for y, row in enumerate(grid):
        for x, occupied in enumerate(row):
            hist[x] = 0 if occupied else hist[x] + 1
        stack: list[int] = []
        for x in range(width + 1):
            current = hist[x] if x < width else 0
            while stack and hist[stack[-1]] > current:
                top = stack.pop()
                h = hist[top]
                left = stack[-1] + 1 if stack else 0
                w = x - left
                area = w * h
                if area > best_area:
                    best_area = area
                    best = (left, y - h + 1, x, y + 1)
            stack.append(x)
    x0, y0, x1, y1 = best
    return {
        "bbox_tracks": [x0, y0, x1, y1],
        "width_tracks": x1 - x0,
        "height_tracks": y1 - y0,
        "area_tracks2": best_area,
    }


def _empty_column_ranges(grid: Sequence[Sequence[bool]]) -> list[dict[str, Any]]:
    if not grid:
        return []
    height = len(grid)
    width = len(grid[0])
    empty = []
    start = None
    for x in range(width):
        is_empty = not any(grid[y][x] for y in range(height))
        if is_empty and start is None:
            start = x
        if (not is_empty or x == width - 1) and start is not None:
            end = x if not is_empty else x + 1
            empty.append({"x_track_range": [start, end], "height_tracks": height})
            start = None
    return empty


def _empty_row_ranges(grid: Sequence[Sequence[bool]]) -> list[dict[str, Any]]:
    if not grid:
        return []
    height = len(grid)
    width = len(grid[0])
    empty = []
    start = None
    for y in range(height):
        is_empty = not any(grid[y][x] for x in range(width))
        if is_empty and start is None:
            start = y
        if (not is_empty or y == height - 1) and start is not None:
            end = y if not is_empty else y + 1
            empty.append({"y_track_range": [start, end], "width_tracks": width})
            start = None
    return empty


def _baseline_delta(baseline: Mapping[str, Any] | None, bbox_tracks: list[int] | None) -> dict[str, Any]:
    if baseline is None or not bbox_tracks:
        return {}
    before = _extract_bbox_tracks(baseline)
    if not before:
        return {}
    before_area = before[0] * before[1]
    after_area = bbox_tracks[0] * bbox_tracks[1]
    return {
        "baseline_id": baseline.get("layout_id", baseline.get("block", "baseline")),
        "bbox_tracks_before": before,
        "bbox_tracks_after": bbox_tracks,
        "delta_width_tracks": bbox_tracks[0] - before[0],
        "delta_height_tracks": bbox_tracks[1] - before[1],
        "delta_area_tracks2": after_area - before_area,
        "area_ratio_after_over_before": _round(after_area / max(before_area, 1), 6),
    }


def _extract_bbox_tracks(report: Mapping[str, Any]) -> list[int] | None:
    checks = _mapping(report.get("checks", {}))
    compactness = _mapping(report.get("compactness", {}))
    global_c = _mapping(compactness.get("global", {}))
    summary = _mapping(report.get("summary", {}))
    candidates = (
        summary.get("bbox_tracks"),
        global_c.get("bbox_tracks"),
        report.get("bbox_tracks"),
    )
    for raw in candidates:
        pair = _sequence2(raw)
        if pair is not None:
            return [int(round(pair[0])), int(round(pair[1]))]
    width = checks.get("total_width_tracks", report.get("total_width_tracks"))
    height = checks.get("total_height_tracks", report.get("total_height_tracks"))
    if width is None or height is None:
        return None
    return [int(round(_number(width, 0))), int(round(_number(height, 0)))]


def _bbox_um_from_report(report: Mapping[str, Any], bbox_tracks: list[int] | None, pitch: float) -> list[float] | None:
    raw = report.get("estimated_bbox_um")
    bbox = _sequence4(raw)
    if bbox is not None:
        return [_round(v) for v in bbox]
    if bbox_tracks:
        return [0.0, 0.0, _round(bbox_tracks[0] * pitch), _round(bbox_tracks[1] * pitch)]
    return None


def _route_rows(routes: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> tuple[Mapping[str, Any], ...]:
    if routes is None:
        return ()
    if isinstance(routes, Mapping):
        raw = routes.get("routes", ())
    else:
        raw = routes
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(_mapping(item) for item in raw if isinstance(item, Mapping))


def _strip_relation_key(key: str) -> str:
    return key.split(":", 1)[1] if ":" in key else key


def _union_bbox(boxes: Sequence[Sequence[float]]) -> list[float] | None:
    valid = tuple(_sequence4(box) for box in boxes)
    valid = tuple(box for box in valid if box is not None)
    if not valid:
        return None
    return [
        _round(min(box[0] for box in valid)),
        _round(min(box[1] for box in valid)),
        _round(max(box[2] for box in valid)),
        _round(max(box[3] for box in valid)),
    ]


def _union_optional_bbox(*boxes: Sequence[float] | None) -> list[float] | None:
    valid = tuple(box for box in boxes if box is not None)
    if not valid:
        return None
    return [
        _round(min(box[0] for box in valid)),
        _round(min(box[1] for box in valid)),
        _round(max(box[2] for box in valid)),
        _round(max(box[3] for box in valid)),
    ]


def _round_bbox(value: Any) -> list[float] | None:
    bbox = _sequence4(value)
    if bbox is None:
        return None
    return [_round(v) for v in bbox]


def _source_kind(source: str) -> str:
    prefix = str(source).split("[", 1)[0].strip().lower()
    if prefix == "path":
        return "path_segment"
    if prefix == "rect":
        return "rect"
    if prefix == "via":
        return "via"
    if prefix == "label":
        return "label"
    if prefix:
        return prefix
    return "unknown"


def _oa_rect_category(row: Mapping[str, Any]) -> str:
    if _oa_rect_is_signoff_marker(row):
        return "signoff_marker"
    if _oa_rect_access_kind(row) != "none":
        return "access"
    if str(row.get("net", "") or ""):
        return "electrical_rect"
    return "non_electrical_rect"


def _oa_rect_access_kind(row: Mapping[str, Any]) -> str:
    metadata = _mapping(row.get("metadata", {}))
    kind = str(metadata.get("kind", metadata.get("access_kind", "")) or "").strip().lower()
    if kind in {"structured_terminal_access", "structured_unit_array_local_bus"}:
        return kind
    if kind.startswith("crn28_mos_"):
        return "generated_mos_access"
    if any(token in kind for token in ("access", "landing", "drop", "bus", "via_stack")):
        return kind or "access"
    if metadata.get("access_contract") or metadata.get("access_role") or metadata.get("terminal"):
        return "metadata_terminal_access"
    if str(row.get("net", "") or "") and not metadata:
        # In this flow, long routes are OA paths. Net-bearing rects without
        # metadata are terminal landings/via geometry emitted by older plans.
        return "unmarked_net_rect_access"
    return "none"


def _oa_rect_is_signoff_marker(row: Mapping[str, Any]) -> bool:
    metadata = _mapping(row.get("metadata", {}))
    if metadata.get("marker_role") or metadata.get("marker_name") or metadata.get("marker_parent"):
        return True
    if str(row.get("net", "") or ""):
        return False
    purpose = str(row.get("purpose", "") or "").strip().lower()
    if purpose.startswith("dummy") or purpose in {"marker", "fill"}:
        return True
    kind = str(metadata.get("kind", "") or "").strip().lower()
    return "marker" in kind or "density" in kind or "dummy" in kind


def _union_area_tracks(boxes: Sequence[Sequence[int]]) -> int:
    events: list[tuple[int, int, int, int]] = []
    for raw in boxes:
        bbox = _sequence4(raw)
        if bbox is None:
            continue
        x0, y0, x1, y1 = [int(round(v)) for v in bbox]
        if x1 <= x0 or y1 <= y0:
            continue
        events.append((x0, y0, y1, 1))
        events.append((x1, y0, y1, -1))
    if not events:
        return 0
    events.sort()
    active: list[tuple[int, int]] = []
    last_x = events[0][0]
    area = 0
    idx = 0
    while idx < len(events):
        x = events[idx][0]
        area += max(0, x - last_x) * _covered_y(active)
        while idx < len(events) and events[idx][0] == x:
            _, y0, y1, typ = events[idx]
            if typ > 0:
                active.append((y0, y1))
            else:
                try:
                    active.remove((y0, y1))
                except ValueError:
                    pass
            idx += 1
        last_x = x
    return int(area)


def _covered_y(intervals: Sequence[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    merged = []
    for y0, y1 in sorted(intervals):
        if not merged or y0 > merged[-1][1]:
            merged.append([y0, y1])
        else:
            merged[-1][1] = max(merged[-1][1], y1)
    return sum(y1 - y0 for y0, y1 in merged)


def _bbox_um_to_tracks(bbox: Sequence[float], pitch: float) -> list[int]:
    x0, y0, x1, y1 = [float(v) for v in bbox]
    p = max(float(pitch), 1e-12)
    return [
        int(math.floor(x0 / p + 1e-9)),
        int(math.floor(y0 / p + 1e-9)),
        int(math.ceil(x1 / p - 1e-9)),
        int(math.ceil(y1 / p - 1e-9)),
    ]


def _bbox_tracks_area(bbox: Sequence[int]) -> int:
    if not isinstance(bbox, Sequence) or len(bbox) < 4:
        return 0
    return max(0, int(bbox[2]) - int(bbox[0])) * max(0, int(bbox[3]) - int(bbox[1]))


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    result = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        values = list(row) + [""] * (len(headers) - len(row))
        result.append("| " + " | ".join(_markdown_cell(value) for value in values[: len(headers)]) + " |")
    return result


def _markdown_cell(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(_clean_json(value), sort_keys=True)
    else:
        text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _compact_actual(actual: Mapping[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in actual.items() if key != "status"},
        sort_keys=True,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _positive_float(value: Any, default: float) -> float:
    number = _number(value, default)
    return number if number > 0 else float(default)


def _positive_or_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "pass", "passed"}:
            return True
        if lowered in {"false", "no", "0", "fail", "failed"}:
            return False
    return None


def _sequence2(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < 2:
        return None
    try:
        return (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None


def _sequence4(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < 4:
        return None
    try:
        return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    except (TypeError, ValueError):
        return None


def _round(value: float, ndigits: int = 6) -> float:
    rounded = round(float(value), ndigits)
    if abs(rounded) < 10 ** (-(ndigits + 1)):
        return 0.0
    return rounded


def _float_bbox4(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (tuple, list)) or len(value) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in (x0, y0, x1, y1)):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _merge_float_bboxes(
    bboxes: Sequence[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    rows = tuple(_float_bbox4(row) for row in bboxes)
    rows = tuple(row for row in rows if row is not None)
    if not rows:
        return None
    return (
        min(row[0] for row in rows),
        min(row[1] for row in rows),
        max(row[2] for row in rows),
        max(row[3] for row in rows),
    )


def _oa_path_bbox_um(row: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    points = tuple(row.get("points", ()) or ())
    if not points:
        return None
    xy: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, (tuple, list)) or len(point) < 2:
            return None
        try:
            xy.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError):
            return None
    try:
        half_width = max(float(row.get("width", 0.0) or 0.0), 0.0) * 0.5
    except (TypeError, ValueError):
        half_width = 0.0
    return (
        min(point[0] for point in xy) - half_width,
        min(point[1] for point in xy) - half_width,
        max(point[0] for point in xy) + half_width,
        max(point[1] for point in xy) + half_width,
    )


def _bbox_expansion_um(
    placement_bbox: tuple[float, float, float, float] | None,
    bbox: tuple[float, float, float, float] | None,
) -> dict[str, float] | None:
    if placement_bbox is None or bbox is None:
        return None
    return {
        "left": max(0.0, placement_bbox[0] - bbox[0]),
        "bottom": max(0.0, placement_bbox[1] - bbox[1]),
        "right": max(0.0, bbox[2] - placement_bbox[2]),
        "top": max(0.0, bbox[3] - placement_bbox[3]),
    }


def _max_abs_protrusion_um(
    placement_bbox: tuple[float, float, float, float] | None,
    bbox: tuple[float, float, float, float] | None,
) -> float | None:
    expansion = _bbox_expansion_um(placement_bbox, bbox)
    if expansion is None:
        return None
    return max(float(value) for value in expansion.values())


def _clean_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _clean_json(val) for key, val in value.items() if val is not None}
    if isinstance(value, tuple):
        return [_clean_json(item) for item in value]
    if isinstance(value, list):
        return [_clean_json(item) for item in value]
    if isinstance(value, float):
        if math.isfinite(value):
            return _round(value)
        return None
    return value


def _load_json(path: str | Path | None) -> Any:
    if path is None:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a layout constraint observation JSON/Markdown artifact.")
    parser.add_argument("--layout-smt", required=True, help="Flat compact SMT physical JSON report.")
    parser.add_argument("--routes", help="Optional structured route summary JSON.")
    parser.add_argument("--connectivity", help="Optional physical connectivity report JSON.")
    parser.add_argument("--oa-layout", help="Optional OA layout JSON with paths/rects for route-envelope metrics.")
    parser.add_argument("--baseline", help="Optional baseline observation or SMT JSON for numeric deltas.")
    parser.add_argument("--layout-id", default="", help="Stable layout identifier for the generated observation.")
    parser.add_argument("--track-pitch-um", type=float, default=None, help="Override track pitch in microns.")
    parser.add_argument("--out-json", help="Output observation JSON path.")
    parser.add_argument("--out-md", help="Output observation Markdown path.")
    args = parser.parse_args(argv)

    layout_smt_path = Path(args.layout_smt)
    routes_path = Path(args.routes) if args.routes else None
    connectivity_path = Path(args.connectivity) if args.connectivity else None
    oa_layout_path = Path(args.oa_layout) if args.oa_layout else None
    baseline_path = Path(args.baseline) if args.baseline else None
    out_json = Path(args.out_json) if args.out_json else layout_smt_path.with_name(layout_smt_path.stem + "_layout_observation.json")
    out_md = Path(args.out_md) if args.out_md else layout_smt_path.with_name(layout_smt_path.stem + "_layout_observation.md")

    source_files = {
        "layout_smt": str(layout_smt_path),
    }
    if routes_path is not None:
        source_files["routes"] = str(routes_path)
    if connectivity_path is not None:
        source_files["connectivity"] = str(connectivity_path)
    if oa_layout_path is not None:
        source_files["oa_layout"] = str(oa_layout_path)
    if baseline_path is not None:
        source_files["baseline"] = str(baseline_path)

    observation = build_layout_observation(
        _load_json(layout_smt_path),
        layout_id=args.layout_id,
        source_files=source_files,
        routes=_load_json(routes_path) if routes_path is not None else None,
        connectivity=_load_json(connectivity_path) if connectivity_path is not None else None,
        oa_layout=_load_json(oa_layout_path) if oa_layout_path is not None else None,
        baseline=_load_json(baseline_path) if baseline_path is not None else None,
        track_pitch_um=args.track_pitch_um,
    )
    write_layout_observation_json(observation, out_json)
    write_layout_observation_markdown(observation, out_md)
    print(str(out_json))
    print(str(out_md))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
