"""Convert aesthetic scores into machine-readable layout feedback.

The score report remains factual.  This module creates the separate feedback
artifact that can drive the next DSL/SMT iteration.
"""
from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from .layout_tweak import LayoutTweakPatch, layout_tweak, layout_tweak_patch_to_dict


SCHEMA_VERSION = "analogskills.block_aesthetic_feedback/v1"


def build_block_aesthetic_feedback(
    aesthetic_report: Mapping[str, Any],
    *,
    block: str = "",
    layout_id: str = "",
    pattern_names: Sequence[str] = (),
    score_threshold: float = 80.0,
) -> dict[str, Any]:
    """Build a structured feedback artifact from a block aesthetic score.

    Feedback is intentionally separate from the score report.  The returned
    artifact includes objective/stage feedback and a best-effort
    ``layout_tweak_patch`` using only currently replayable operations.
    """

    scores = _mapping(aesthetic_report.get("scores"))
    metrics = _mapping(aesthetic_report.get("metrics"))
    patterns = tuple(dict.fromkeys(str(name) for name in pattern_names if str(name)))
    items: list[dict[str, Any]] = []

    def add_item(
        metric: str,
        *,
        stage: str,
        objective: str,
        action: str,
        dsl: Mapping[str, Any] | None = None,
        observation_refs: Sequence[str] = (),
    ) -> None:
        score = _optional_float(scores.get(metric))
        if score is None or score >= float(score_threshold):
            return
        items.append(
            {
                "id": f"AFB-{len(items) + 1:03d}",
                "metric": metric,
                "score": round(score, 3),
                "severity": "major" if score < 60.0 else "minor",
                "target_stage": stage,
                "objective": objective,
                "feedback_action": action,
                "observation_refs": tuple(observation_refs),
                "dsl_feedback": dict(dsl or {}),
            }
        )

    add_item(
        "layout_component_occupancy",
        stage="global_smt",
        objective="increase component occupancy inside the component envelope",
        action="increase local/global compact-envelope pressure before route merge",
        dsl={
            "objective_terms": (
                {
                    "kind": "compact_envelope",
                    "patterns": patterns,
                    "weight_delta": _weight_delta(scores.get("layout_component_occupancy")),
                },
            ),
            "objective_weights": {"true_area_weight": "increase", "area_weight": "increase"},
        },
        observation_refs=("scores.layout_component_occupancy", "metrics.component_occupancy_ratio"),
    )
    add_item(
        "layout_symmetry",
        stage="global_smt",
        objective="improve visual symmetry of paired/local motifs",
        action="strengthen mirror/edge alignment objectives on ordered pattern pairs",
        dsl={
            "objective_terms": (
                {
                    "kind": "mirror_symmetry",
                    "patterns": _outer_pair(patterns),
                    "axis": "x",
                    "weight_delta": _weight_delta(scores.get("layout_symmetry")),
                },
            ),
        },
        observation_refs=("scores.layout_symmetry", "metrics.vertical_symmetry_score", "metrics.horizontal_symmetry_score"),
    )
    add_item(
        "layout_squareness",
        stage="global_smt",
        objective="make block envelope closer to the configured aesthetic aspect target",
        action="increase squareness/aspect surrogate objective and compact the long axis",
        dsl={
            "objective_terms": (
                {
                    "kind": "aesthetic_squareness",
                    "patterns": patterns,
                    "target": _aspect_target_from_report(metrics),
                    "weight_delta": _weight_delta(scores.get("layout_squareness")),
                },
            ),
            "compact_axis": _long_axis(metrics),
        },
        observation_refs=("scores.layout_squareness", "metrics.aspect_ratio", "metrics.component_bbox_um"),
    )
    add_item(
        "pin_boundary",
        stage="pin_planning",
        objective="move top-level pins to coordinated block-boundary access",
        action="create boundary pin rails/ports after route access, then re-score pin boundary",
        dsl={
            "pin_policy": {
                "boundary_export": "required",
                "preferred_sides": "derive_from_net_role",
                "alignment": "regular_edge_slots",
            }
        },
        observation_refs=("scores.pin_boundary", "metrics.pin_side_counts", "metrics.shape_bbox_um"),
    )
    add_item(
        "pin_alignment",
        stage="pin_planning",
        objective="make pins on each side regularly spaced and visually coordinated",
        action="quantize boundary pin slots per side",
        dsl={"pin_policy": {"slot_quantization": "uniform_per_side"}},
        observation_refs=("scores.pin_alignment", "metrics.pin_side_counts"),
    )
    add_item(
        "route_escape",
        stage="routing",
        objective="keep routes inside or close to the device/access envelope",
        action="increase internal reserved-channel preference and penalize route-envelope escape",
        dsl={
            "route_policy": {
                "internal_reserved_channels": "increase_weight",
                "route_escape_budget": "tighten",
            }
        },
        observation_refs=("scores.route_escape", "metrics.route_to_component_bbox_area_ratio"),
    )
    add_item(
        "route_symmetry_distribution",
        stage="routing",
        objective="balance routing density across the block envelope",
        action="balance critical-net trunk sides and layer allocation",
        dsl={"route_policy": {"balanced_trunk_sides": True, "layer_coordination": "preserve"}},
        observation_refs=("scores.route_symmetry_distribution", "metrics.path_count_by_layer"),
    )

    patch = build_block_aesthetic_tweak_patch(
        items,
        block=block,
        layout_id=layout_id,
        pattern_names=patterns,
        metrics=metrics,
        scores=scores,
    )
    patch_dict = layout_tweak_patch_to_dict(patch)
    patch_dict.setdefault("operations", [])
    return {
        "schema": SCHEMA_VERSION,
        "block": str(block),
        "layout_id": str(layout_id),
        "source_score_schema": str(aesthetic_report.get("schema", "")),
        "score": _optional_float(aesthetic_report.get("score")),
        "grade": str(aesthetic_report.get("grade", "")),
        "score_threshold": float(score_threshold),
        "feedback_item_count": len(items),
        "smt_visible_feedback_count": sum(1 for item in items if item["target_stage"] == "global_smt"),
        "routing_feedback_count": sum(1 for item in items if item["target_stage"] == "routing"),
        "pin_feedback_count": sum(1 for item in items if item["target_stage"] == "pin_planning"),
        "feedback_items": items,
        "layout_tweak_patch": patch_dict,
        "acceptance": {
            "min_score_delta": 2.0,
            "no_new_overlap": True,
            "no_bbox_area_regression_ratio": 0.05,
            "pin_boundary_must_not_regress": True,
            "route_escape_must_not_regress": True,
            "pin_boundary_regression_tolerance": 0.1,
            "route_escape_regression_tolerance": 0.1,
        },
    }


def build_block_aesthetic_tweak_patch(
    feedback_items: Sequence[Mapping[str, Any]],
    *,
    block: str = "",
    layout_id: str = "",
    pattern_names: Sequence[str] = (),
    metrics: Mapping[str, Any] | None = None,
    scores: Mapping[str, Any] | None = None,
) -> LayoutTweakPatch:
    """Create a best-effort replayable patch from aesthetic feedback items."""

    metrics = _mapping(metrics)
    score_map = _mapping(scores)
    patterns = tuple(dict.fromkeys(str(name) for name in pattern_names if str(name)))
    patch = layout_tweak(_safe_name(f"{block or 'block'}_aesthetic_feedback"), baseline_layout_id=layout_id)
    patch.observation_refs("block_aesthetic_score.json")

    item_metrics = {str(item.get("metric", "")) for item in feedback_items}
    occupancy_score = _feedback_item_score(feedback_items, "layout_component_occupancy")
    if occupancy_score is None:
        occupancy_score = _optional_float(score_map.get("layout_component_occupancy"))
    occupancy_ratio = _optional_float(metrics.get("component_occupancy_ratio"))
    route_escape_score = _feedback_item_score(feedback_items, "route_escape")
    if route_escape_score is None:
        route_escape_score = _optional_float(score_map.get("route_escape"))
    pin_boundary_score = _feedback_item_score(feedback_items, "pin_boundary")
    if pin_boundary_score is None:
        pin_boundary_score = _optional_float(score_map.get("pin_boundary"))
    if occupancy_score is not None:
        placement_compaction_needed = occupancy_score < 70.0
    else:
        placement_compaction_needed = occupancy_ratio is not None and occupancy_ratio < 0.55
    routing_or_pin_fragile = (
        (route_escape_score is not None and route_escape_score < 75.0)
        or (pin_boundary_score is not None and pin_boundary_score < 60.0)
    )
    if "layout_component_occupancy" in item_metrics and len(patterns) >= 2:
        for left, right in _adjacent_pairs(patterns, limit=3):
            patch.compact_gap(
                left,
                right,
                axis="both",
                observation_refs=("scores.layout_component_occupancy", "metrics.component_occupancy_ratio"),
                risk="may increase local routing pressure; accept only if physical precheck does not regress",
            )
    if (
        ("layout_component_occupancy" in item_metrics or "layout_squareness" in item_metrics)
        and placement_compaction_needed
    ):
        for target, topology_options, spacing_options in _pattern_topology_adjustments(block, patterns):
            patch.pattern_candidate(
                target,
                topology_options=topology_options,
                spacing_options_um=spacing_options,
                observation_refs=(
                    "scores.layout_component_occupancy",
                    "scores.layout_squareness",
                    "metrics.component_occupancy_ratio",
                    "metrics.aspect_ratio",
                ),
                risk="changes pattern realization search space; accept only if total aesthetics improves and physical precheck does not regress",
            )
    if "layout_squareness" in item_metrics and len(patterns) >= 2 and not routing_or_pin_fragile:
        axis = _long_axis(metrics)
        for left, right in _adjacent_pairs(patterns, limit=2):
            patch.compact_gap(
                left,
                right,
                axis=axis,
                observation_refs=("scores.layout_squareness", "metrics.aspect_ratio"),
                risk="may trade aspect against symmetry; accept by total aesthetic score",
            )
    if "layout_symmetry" in item_metrics and len(patterns) >= 2 and not routing_or_pin_fragile:
        for left, right in _outer_pairs(patterns, limit=2):
            patch.align_edge(
                left,
                right,
                edge="both",
                observation_refs=("scores.layout_symmetry",),
                risk="soft alignment only; reject if compactness or route score regresses",
            )
    route_adjustments = _route_lane_adjustments(block)
    if "route_escape" in item_metrics or (placement_compaction_needed and route_adjustments):
        for route_name, route_kwargs in route_adjustments or (("critical_routes", {"channel_side": "internal"}),):
            patch.route_lane(
                route_name,
                **route_kwargs,
                observation_refs=("scores.route_escape", "scores.route_symmetry_distribution"),
                risk="route-resource replay; accept only if route/pin scores and physical precheck do not regress",
            )
    patch.acceptance(
        source="block_aesthetic_feedback",
        min_score_delta=2.0,
        max_bbox_area_regression_ratio=0.05,
        no_new_overlap=True,
        direct_geometry_mutation=False,
    )
    patch.notes(
        "Generated from block aesthetic score; global-SMT operations are replayable, "
        "pin/route policy feedback is recorded for downstream stages. "
        f"placement_compaction_needed={placement_compaction_needed}; routing_or_pin_fragile={routing_or_pin_fragile}."
    )
    return patch.build()


def write_block_aesthetic_feedback_json(feedback: Mapping[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_jsonable(feedback), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def augment_block_aesthetic_feedback_with_physical_route_shorts(
    feedback: Mapping[str, Any],
    physical_report: Mapping[str, Any] | None,
    *,
    block: str = "",
    max_repairs: int = 6,
) -> dict[str, Any]:
    """Append route-resource tweak operations derived from physical shorts.

    The generated operations are candidates, not direct geometry fixes.  They
    move the routed path net involved in a same-layer short to a nearby upper
    routing resource, preserving the existing accept/reject gate.
    """

    report = _mapping(physical_report or {})
    shorts = tuple(_mapping(row) for row in tuple(report.get("shorts", ()) or ()) if _mapping(row))
    if not shorts:
        return dict(feedback)
    result = dict(_mapping(feedback))
    patch = dict(_mapping(result.get("layout_tweak_patch")))
    raw_operations = tuple(patch.get("operations", ()) or ())
    operations = _prune_physical_route_short_operations(
        _dedupe_tweak_operations(raw_operations),
        max_repairs=max_repairs,
    )
    existing = {
        (
            str(_mapping(op).get("op", "")),
            str(_mapping(op).get("route_name", "")),
            str(_mapping(op).get("layer", "")),
            _mapping(op).get("lane"),
        )
        for op in operations
    }
    existing_short_keys = {
        _source_short_key(_mapping(_mapping(op).get("metadata")).get("source_short"))
        for op in operations
        if _mapping(_mapping(op).get("metadata")).get("source") == "physical_route_short"
    }
    existing_short_keys.discard(())
    added: list[dict[str, Any]] = []
    for short in _rank_physical_route_shorts(shorts):
        short_key = _source_short_key(short)
        if short_key and short_key in existing_short_keys:
            continue
        repair = _route_short_repair_operation(short, block=block, index=len(added))
        if repair is None:
            continue
        key = (
            str(repair.get("op", "")),
            str(repair.get("route_name", "")),
            str(repair.get("layer", "")),
            repair.get("lane"),
        )
        if key in existing:
            continue
        existing.add(key)
        if short_key:
            existing_short_keys.add(short_key)
        added.append(repair)
        operations.append(repair)
        if len(added) >= max(0, int(max_repairs)):
            break
    if not added:
        if len(operations) != len(raw_operations):
            patch["operations"] = tuple(operations)
            patch["notes"] = _append_note(
                str(patch.get("notes", "")),
                "Duplicate physical route-short candidate repairs removed.",
            )
            result["layout_tweak_patch"] = patch
            result["physical_route_short_feedback_count"] = _physical_route_short_operation_count(operations)
        return result
    deduped_operations = _prune_physical_route_short_operations(
        _dedupe_tweak_operations(operations),
        max_repairs=max_repairs,
    )
    patch["operations"] = tuple(deduped_operations)
    patch["notes"] = _append_note(
        str(patch.get("notes", "")),
        f"Physical route-short candidate repairs appended: {len(added)}.",
    )
    result["layout_tweak_patch"] = patch
    result["physical_route_short_feedback_count"] = _physical_route_short_operation_count(deduped_operations)
    result["physical_route_short_feedback"] = tuple(
        {
            "route_name": row.get("route_name", ""),
            "layer": row.get("layer", ""),
            "lane": row.get("lane"),
            "source_short": _mapping(row.get("metadata")).get("source_short"),
        }
        for row in added
    )
    return result


def augment_block_aesthetic_feedback_with_layout_observation(
    feedback: Mapping[str, Any],
    observation: Mapping[str, Any] | None,
    *,
    block: str = "",
    max_windows: int = 6,
) -> dict[str, Any]:
    """Append solver-visible placement-window tweaks from factual observation.

    The observation artifact is intentionally non-prescriptive.  This helper is
    the policy layer that turns the factual bbox/whitespace data into replayable
    SMT handles.  It does not mutate geometry; generated windows are soft
    placement objectives that must pass the same accept/reject gate as other
    aesthetic candidates.
    """

    obs = _mapping(observation or {})
    tweak = _layout_tweakability_from_observation(obs)
    groups = _mapping(tweak.get("groups"))
    if not groups:
        return dict(feedback)

    result = dict(_mapping(feedback))
    patch = dict(_mapping(result.get("layout_tweak_patch")))
    operations = _dedupe_tweak_operations(tuple(patch.get("operations", ()) or ()))
    generated = _placement_window_operations_from_tweakability(
        tweak,
        block=block or str(result.get("block", "")),
        max_windows=max_windows,
    )
    if not generated:
        return result

    existing_keys = {_tweak_operation_key(op) for op in operations}
    added: list[dict[str, Any]] = []
    for op in generated:
        key = _tweak_operation_key(op)
        if key in existing_keys:
            continue
        existing_keys.add(key)
        operations.append(op)
        added.append(op)

    if not added:
        return result
    patch["operations"] = tuple(operations)
    patch["notes"] = _append_note(
        str(patch.get("notes", "")),
        f"Observation-derived placement-window candidate tweaks appended: {len(added)}.",
    )
    result["layout_tweak_patch"] = patch
    result["layout_observation_feedback_count"] = len(added)
    result["layout_observation_feedback"] = tuple(
        {
            "target": row.get("target", ""),
            "target_x_tracks": row.get("target_x_tracks"),
            "target_y_tracks": row.get("target_y_tracks"),
            "source": _mapping(row.get("metadata")).get("source"),
            "source_kind": _mapping(row.get("metadata")).get("source_kind"),
        }
        for row in added
    )
    return result


def load_block_aesthetic_feedback_tweak_patch(path: str | Path) -> dict[str, Any] | None:
    """Load the replayable layout tweak patch from a feedback JSON file.

    Returns ``None`` when the file is missing, malformed, or contains no
    operations.  This keeps feedback replay opt-in and safe for baseline runs.
    """

    source = Path(path)
    if not source.exists():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    payload_map = _mapping(payload)
    patch = _mapping(payload_map.get("layout_tweak_patch"))
    if not patch and payload_map.get("schema_version") == "layout_tweak_patch/v1":
        patch = payload_map
    if not patch:
        return None
    operations = tuple(patch.get("operations", ()) or ())
    if not operations:
        return None
    return dict(patch)


def build_block_aesthetic_feedback_candidates(
    feedback: Mapping[str, Any],
    *,
    max_candidates: int = 8,
) -> dict[str, Any]:
    """Build independently replayable candidate feedback files.

    A single feedback patch may contain several operations.  Trying all of
    them at once can hide which operation helped or hurt.  This helper keeps
    the original feedback factual, then emits a small ranked set of full
    feedback payloads whose ``layout_tweak_patch`` contains one safe candidate
    operation subset.
    """

    feedback_map = dict(_mapping(feedback))
    patch = dict(_mapping(feedback_map.get("layout_tweak_patch")))
    operations = tuple(dict(_mapping(row)) for row in tuple(patch.get("operations", ()) or ()) if _mapping(row))
    block = str(feedback_map.get("block", ""))
    layout_id = str(feedback_map.get("layout_id", ""))
    base_patch_id = str(patch.get("patch_id", _safe_name(f"{block or 'block'}_aesthetic_feedback")))
    limit = max(0, int(max_candidates))
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_candidate(
        kind: str,
        ops: Sequence[Mapping[str, Any]],
        rationale: str,
        *,
        acceptance_overrides: Mapping[str, Any] | None = None,
    ) -> None:
        if limit and len(candidates) >= limit:
            return
        clean_ops = tuple(dict(_mapping(op)) for op in ops if _mapping(op))
        if not clean_ops:
            return
        signature = json.dumps(_jsonable(clean_ops), sort_keys=True, separators=(",", ":"))
        if signature in seen:
            return
        seen.add(signature)
        candidate_id = f"cand_{len(candidates):02d}_{_safe_name(kind)}"
        candidate_patch = dict(patch)
        candidate_patch["patch_id"] = f"{base_patch_id}_{candidate_id}"
        candidate_patch["operations"] = clean_ops
        candidate_patch["notes"] = _append_note(
            str(candidate_patch.get("notes", "")),
            f"Aesthetic feedback candidate {candidate_id}: {rationale}",
        )
        candidate_feedback = dict(feedback_map)
        if acceptance_overrides:
            acceptance = dict(_mapping(candidate_feedback.get("acceptance")))
            acceptance.update({str(key): value for key, value in dict(acceptance_overrides).items()})
            candidate_feedback["acceptance"] = acceptance
        candidate_feedback["layout_tweak_patch"] = candidate_patch
        candidate_feedback["candidate"] = {
            "candidate_id": candidate_id,
            "kind": str(kind),
            "rationale": str(rationale),
            "operation_count": len(clean_ops),
            "operation_kinds": tuple(str(op.get("op", "")) for op in clean_ops),
        }
        candidates.append(
            {
                "candidate_id": candidate_id,
                "kind": str(kind),
                "rationale": str(rationale),
                "operation_count": len(clean_ops),
                "operation_kinds": tuple(str(op.get("op", "")) for op in clean_ops),
                "feedback": candidate_feedback,
            }
        )

    placement_ops = tuple(
        op
        for op in operations
        if str(op.get("op", "")).lower()
        in {"compact_gap", "align_edge", "nudge", "placement_window", "pattern_candidate"}
    )
    compact_ops = tuple(op for op in operations if str(op.get("op", "")).lower() == "compact_gap")
    align_ops = tuple(op for op in operations if str(op.get("op", "")).lower() == "align_edge")
    pattern_ops = tuple(op for op in operations if str(op.get("op", "")).lower() == "pattern_candidate")
    window_ops = tuple(op for op in operations if str(op.get("op", "")).lower() == "placement_window")
    route_ops = tuple(op for op in operations if str(op.get("op", "")).lower() == "route_lane")
    physical_route_ops = tuple(
        op
        for op in route_ops
        if str(_mapping(op.get("metadata")).get("source", "") or "").lower() == "physical_route_short"
    )

    add_candidate("all_ops", operations, "original feedback patch; highest-risk broad replay")
    add_candidate("placement_all", placement_ops, "all global-placement replayable operations without route policy-only ops")
    route_policy_acceptance = {
        "pin_boundary_regression_tolerance": 0.5,
        "route_escape_regression_tolerance": 0.1,
    }
    add_candidate(
        "route_policy_all",
        route_ops,
        "routing policy feedback only; does not directly mutate placement SMT",
        acceptance_overrides=route_policy_acceptance,
    )
    add_candidate(
        "physical_short_route_repair",
        physical_route_ops,
        "route-resource operations generated directly from physical same-layer short feedback",
    )
    add_candidate("compact_all", compact_ops, "only compact-gap operations; useful for low utilization diagnosis")
    add_candidate("pattern_topology_all", pattern_ops, "only pattern topology/spacing search-space operations")
    add_candidate("placement_window_all", window_ops, "only observation-derived soft placement-window operations")
    if pattern_ops and route_ops:
        add_candidate(
            "pattern_topology_with_route_guard",
            pattern_ops + route_ops,
            "pattern topology replay coupled with route-resource guard operations",
        )
    for idx, op in enumerate(compact_ops):
        add_candidate(f"compact_single_{idx}", (op,), "single compact-gap operation for localized A/B replay")
    for idx, op in enumerate(pattern_ops):
        add_candidate(f"pattern_topology_single_{idx}", (op,), "single pattern-topology operation for localized A/B replay")
    for idx, op in enumerate(window_ops):
        add_candidate(f"placement_window_single_{idx}", (op,), "single placement-window operation for localized A/B replay")
    add_candidate("align_all", align_ops, "only symmetry/alignment operations")
    for idx, op in enumerate(route_ops):
        add_candidate(
            f"route_policy_single_{idx}",
            (op,),
            "single route-resource operation for localized short/escape A/B replay",
            acceptance_overrides=route_policy_acceptance,
        )

    return {
        "schema": "analogskills.block_aesthetic_feedback_candidates/v1",
        "block": block,
        "layout_id": layout_id,
        "source_feedback_schema": str(feedback_map.get("schema", "")),
        "source_score": _optional_float(feedback_map.get("score")),
        "source_grade": str(feedback_map.get("grade", "")),
        "source_patch_id": base_patch_id,
        "source_operation_count": len(operations),
        "candidate_count": len(candidates),
        "max_candidates": limit,
        "candidates": tuple(candidates),
    }


def write_block_aesthetic_feedback_candidates_json(candidate_set: Mapping[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_jsonable(candidate_set), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def write_block_aesthetic_feedback_candidate_files(
    candidate_set: Mapping[str, Any],
    directory: str | Path,
    *,
    clean_stale: bool = True,
) -> tuple[Path, ...]:
    """Write one full feedback JSON file per candidate.

    The generated files can be replayed directly by setting
    ``ANALOGSKILLS_AESTHETIC_FEEDBACK_PATH`` to the chosen candidate path
    (the legacy ``SKILLS_Z_*`` spelling is still accepted).
    """

    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_names: set[str] = set()
    for row in tuple(candidate_set.get("candidates", ()) or ()):
        candidate = _mapping(row)
        feedback = _mapping(candidate.get("feedback"))
        if feedback:
            candidate_id = _safe_name(candidate.get("candidate_id", f"candidate_{len(expected_names)}"))
            expected_names.add(f"{candidate_id}.json")
    if clean_stale:
        _remove_stale_block_aesthetic_feedback_candidate_files(out_dir, expected_names)
    paths: list[Path] = []
    for row in tuple(candidate_set.get("candidates", ()) or ()):
        candidate = _mapping(row)
        feedback = _mapping(candidate.get("feedback"))
        if not feedback:
            continue
        candidate_id = _safe_name(candidate.get("candidate_id", f"candidate_{len(paths)}"))
        path = out_dir / f"{candidate_id}.json"
        write_block_aesthetic_feedback_json(feedback, path)
        paths.append(path)
    return tuple(paths)


def _remove_stale_block_aesthetic_feedback_candidate_files(directory: Path, expected_names: set[str]) -> None:
    for path in directory.glob("*.json"):
        if path.name in expected_names:
            continue
        if _looks_like_generated_block_aesthetic_feedback_file(path):
            path.unlink(missing_ok=True)


def _looks_like_generated_block_aesthetic_feedback_file(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    row = _mapping(payload)
    return (
        row.get("schema") == SCHEMA_VERSION
        and isinstance(row.get("layout_tweak_patch"), Mapping)
        and isinstance(row.get("acceptance"), Mapping)
    )


def evaluate_block_aesthetic_feedback_acceptance(
    feedback: Mapping[str, Any],
    baseline_aesthetic_report: Mapping[str, Any] | None,
    candidate_aesthetic_report: Mapping[str, Any],
    *,
    baseline_physical_report: Mapping[str, Any] | None = None,
    candidate_physical_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate whether a feedback-replay candidate should replace baseline.

    The gate is deliberately conservative: an aesthetic candidate must improve
    the total score and must not regress route/pin quality or physical
    precheck counts.  It produces a factual machine-readable report; callers
    can then decide whether to keep or discard the generated candidate files.
    """

    acceptance = _mapping(feedback.get("acceptance"))
    min_delta = _optional_float(acceptance.get("min_score_delta"))
    if min_delta is None:
        min_delta = 2.0
    max_physical_repair_score_regression = _optional_float(
        acceptance.get(
            "max_score_regression_for_physical_improvement",
            acceptance.get("max_score_regression_for_drc_improvement"),
        )
    )
    if max_physical_repair_score_regression is None:
        max_physical_repair_score_regression = 0.5
    baseline_score = _optional_float(_mapping(baseline_aesthetic_report or {}).get("score"))
    candidate_score = _optional_float(candidate_aesthetic_report.get("score"))
    reasons: list[str] = []
    checks: dict[str, Any] = {
        "min_score_delta": float(min_delta),
        "max_score_regression_for_physical_improvement": float(max_physical_repair_score_regression),
        "baseline_score": baseline_score,
        "candidate_score": candidate_score,
        "score_delta": None,
    }
    pin_boundary_regression_tolerance = _optional_float(acceptance.get("pin_boundary_regression_tolerance"))
    if pin_boundary_regression_tolerance is None:
        pin_boundary_regression_tolerance = 0.1
    route_escape_regression_tolerance = _optional_float(acceptance.get("route_escape_regression_tolerance"))
    if route_escape_regression_tolerance is None:
        route_escape_regression_tolerance = 0.1
    checks["pin_boundary_regression_tolerance"] = float(pin_boundary_regression_tolerance)
    checks["route_escape_regression_tolerance"] = float(route_escape_regression_tolerance)

    physical_checks = _physical_regression_checks(
        baseline_physical_report,
        candidate_physical_report,
        no_new_overlap=bool(acceptance.get("no_new_overlap", True)),
    )
    checks["physical_regression_checks"] = physical_checks
    physical_regressed = any(not row.get("passed", True) for row in physical_checks)
    physical_improved = _physical_regression_checks_show_improvement(physical_checks)
    checks["physical_improved"] = bool(physical_improved)

    if baseline_score is None or candidate_score is None:
        reasons.append("missing_baseline_or_candidate_score")
    else:
        score_delta = float(candidate_score) - float(baseline_score)
        checks["score_delta"] = round(score_delta, 6)
        if score_delta < float(min_delta):
            if physical_improved and not physical_regressed and score_delta >= -float(max_physical_repair_score_regression):
                checks["score_delta_override_reason"] = "physical_precheck_improved"
            else:
                reasons.append("score_delta_below_minimum")

    metric_checks = _metric_regression_checks(
        baseline_aesthetic_report or {},
        candidate_aesthetic_report,
        pin_boundary_must_not_regress=bool(acceptance.get("pin_boundary_must_not_regress", True)),
        route_escape_must_not_regress=bool(acceptance.get("route_escape_must_not_regress", True)),
        pin_boundary_regression_tolerance=float(pin_boundary_regression_tolerance),
        route_escape_regression_tolerance=float(route_escape_regression_tolerance),
    )
    checks["metric_regression_checks"] = metric_checks
    for row in metric_checks:
        if not row.get("passed", True):
            reasons.append(f"metric_regressed:{row.get('metric')}")

    for row in physical_checks:
        if not row.get("passed", True):
            reasons.append(f"physical_regressed:{row.get('metric')}")

    accepted = not reasons
    return {
        "schema": "analogskills.block_aesthetic_feedback_acceptance/v1",
        "block": str(feedback.get("block", "")),
        "layout_id": str(feedback.get("layout_id", "")),
        "accepted": bool(accepted),
        "status": "accepted" if accepted else "rejected",
        "reasons": tuple(reasons),
        "checks": checks,
    }


def write_block_aesthetic_feedback_acceptance_json(report: Mapping[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_jsonable(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def snapshot_block_artifacts(
    paths: Mapping[str, str | Path],
    *,
    snapshot_dir: str | Path,
    label: str = "baseline",
) -> dict[str, Any]:
    """Copy a flat list of block artifacts before a candidate replay run.

    The helper is intentionally file-only.  It does not recurse through
    directories, and it records missing files so restore can preserve the
    original baseline state.
    """

    root = Path(snapshot_dir)
    root.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for idx, (name, path_value) in enumerate(paths.items()):
        target = Path(path_value)
        entry: dict[str, Any] = {
            "name": str(name),
            "path": str(target),
            "exists": bool(target.exists()),
        }
        if target.exists() and target.is_file():
            suffix = target.suffix or ".artifact"
            snapshot_path = root / f"{idx:02d}_{_safe_name(name)}{suffix}"
            shutil.copy2(target, snapshot_path)
            entry.update(
                {
                    "is_file": True,
                    "snapshot_path": str(snapshot_path),
                    "size_bytes": int(target.stat().st_size),
                }
            )
        elif target.exists():
            entry.update({"is_file": False, "reason": "not_a_file_skipped"})
        files.append(entry)
    return {
        "schema": "analogskills.block_artifact_snapshot/v1",
        "label": str(label),
        "snapshot_dir": str(root),
        "file_count": len(files),
        "files": tuple(files),
    }


def restore_block_artifact_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Restore artifacts captured by :func:`snapshot_block_artifacts`.

    Only files explicitly listed in the snapshot are restored or removed.  A
    file that did not exist at snapshot time is removed only when the candidate
    replay created a regular file at the same path.
    """

    rows: list[dict[str, Any]] = []
    restored = 0
    removed = 0
    for raw_entry in tuple(snapshot.get("files", ()) or ()):
        entry = _mapping(raw_entry)
        target_text = str(entry.get("path", ""))
        name = str(entry.get("name", ""))
        if not target_text:
            rows.append({"name": name, "action": "skipped", "reason": "missing_target_path"})
            continue
        target = Path(target_text)
        existed = bool(entry.get("exists", False))
        snapshot_path_text = str(entry.get("snapshot_path", ""))
        if existed:
            if not snapshot_path_text:
                rows.append({"name": name, "path": str(target), "action": "skipped", "reason": "no_snapshot_file"})
                continue
            snapshot_path = Path(snapshot_path_text)
            if not snapshot_path.exists() or not snapshot_path.is_file():
                rows.append({"name": name, "path": str(target), "action": "skipped", "reason": "missing_snapshot_file"})
                continue
            if target.exists() and target.is_dir():
                rows.append({"name": name, "path": str(target), "action": "skipped", "reason": "target_is_directory"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot_path, target)
            restored += 1
            rows.append(
                {
                    "name": name,
                    "path": str(target),
                    "snapshot_path": str(snapshot_path),
                    "action": "restored",
                }
            )
            continue
        if target.exists() and target.is_file():
            target.unlink()
            removed += 1
            rows.append({"name": name, "path": str(target), "action": "removed_candidate_file"})
        elif target.exists():
            rows.append({"name": name, "path": str(target), "action": "skipped", "reason": "target_is_not_file"})
        else:
            rows.append({"name": name, "path": str(target), "action": "kept_absent"})
    return {
        "schema": "analogskills.block_artifact_restore/v1",
        "snapshot_label": str(snapshot.get("label", "")),
        "restored": bool(restored or removed),
        "restored_count": restored,
        "removed_candidate_file_count": removed,
        "files": tuple(rows),
    }


def write_block_artifact_snapshot_json(report: Mapping[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_jsonable(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_float(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _sequence4(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if len(value) != 4:
        return None
    try:
        return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    except (TypeError, ValueError):
        return None


def _feedback_item_score(items: Sequence[Mapping[str, Any]], metric: str) -> float | None:
    for item in items:
        if str(item.get("metric", "")) == metric:
            return _optional_float(item.get("score"))
    return None


def _metric_regression_checks(
    baseline_aesthetic_report: Mapping[str, Any],
    candidate_aesthetic_report: Mapping[str, Any],
    *,
    pin_boundary_must_not_regress: bool,
    route_escape_must_not_regress: bool,
    pin_boundary_regression_tolerance: float = 0.1,
    route_escape_regression_tolerance: float = 0.1,
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    requested: list[tuple[str, float]] = []
    if pin_boundary_must_not_regress:
        requested.append(("pin_boundary", max(0.0, float(pin_boundary_regression_tolerance))))
    if route_escape_must_not_regress:
        requested.append(("route_escape", max(0.0, float(route_escape_regression_tolerance))))
    baseline_scores = _mapping(baseline_aesthetic_report.get("scores"))
    candidate_scores = _mapping(candidate_aesthetic_report.get("scores"))
    for metric, tolerance in requested:
        baseline = _optional_float(baseline_scores.get(metric))
        candidate = _optional_float(candidate_scores.get(metric))
        if baseline is None or candidate is None:
            rows.append(
                {
                    "metric": metric,
                    "passed": True,
                    "baseline": baseline,
                    "candidate": candidate,
                    "delta": None,
                    "reason": "missing_metric_skipped",
                }
            )
            continue
        delta = float(candidate) - float(baseline)
        rows.append(
            {
                "metric": metric,
                "passed": delta >= -float(tolerance),
                "baseline": round(float(baseline), 6),
                "candidate": round(float(candidate), 6),
                "delta": round(delta, 6),
                "regression_tolerance": round(float(tolerance), 6),
            }
        )
    return tuple(rows)


def _physical_regression_checks(
    baseline_physical_report: Mapping[str, Any] | None,
    candidate_physical_report: Mapping[str, Any] | None,
    *,
    no_new_overlap: bool,
) -> tuple[dict[str, Any], ...]:
    if candidate_physical_report is None:
        return (
            {
                "metric": "physical_report",
                "passed": False,
                "reason": "missing_candidate_physical_report",
            },
        )
    baseline = _mapping(baseline_physical_report or {})
    candidate = _mapping(candidate_physical_report)
    rows: list[dict[str, Any]] = []
    for metric, key in (("shorts", "shorts"), ("opens", "opens"), ("issues", "issues")):
        baseline_count = len(tuple(baseline.get(key, ()) or ())) if baseline else None
        candidate_count = len(tuple(candidate.get(key, ()) or ()))
        passed = True
        if baseline_count is None:
            if no_new_overlap and metric == "shorts" and candidate_count > 0:
                passed = False
        elif candidate_count > int(baseline_count):
            passed = False
        rows.append(
            {
                "metric": metric,
                "passed": bool(passed),
                "baseline_count": baseline_count,
                "candidate_count": candidate_count,
            }
        )
    if baseline and bool(baseline.get("passed", False)) and not bool(candidate.get("passed", False)):
        rows.append(
            {
                "metric": "physical_passed",
                "passed": False,
                "baseline": True,
                "candidate": False,
            }
        )
    return tuple(rows)


def _physical_regression_checks_show_improvement(rows: Sequence[Mapping[str, Any]]) -> bool:
    for row in rows:
        if str(row.get("metric", "") or "") not in {"shorts", "opens", "issues"}:
            continue
        baseline_count = row.get("baseline_count")
        candidate_count = row.get("candidate_count")
        if baseline_count is None or candidate_count is None:
            continue
        try:
            if int(candidate_count) < int(baseline_count):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _weight_delta(score: object) -> int:
    value = _optional_float(score)
    if value is None:
        return 2
    if value < 40:
        return 8
    if value < 60:
        return 5
    if value < 75:
        return 3
    return 1


def _aspect_target_from_report(metrics: Mapping[str, Any]) -> str:
    aspect = _optional_float(metrics.get("aspect_ratio"))
    if aspect is None:
        return "1:1"
    if aspect > 1.35:
        return "1:1"
    if aspect < 0.75:
        return "1:1"
    return "current"


def _long_axis(metrics: Mapping[str, Any]) -> str:
    aspect = _optional_float(metrics.get("aspect_ratio"))
    if aspect is None:
        return "both"
    if aspect > 1.15:
        return "x"
    if aspect < 0.87:
        return "y"
    return "both"


def _outer_pair(patterns: Sequence[str]) -> tuple[str, ...]:
    if len(patterns) >= 2:
        return (str(patterns[0]), str(patterns[-1]))
    return tuple(str(item) for item in patterns)


def _adjacent_pairs(patterns: Sequence[str], *, limit: int) -> tuple[tuple[str, str], ...]:
    pairs = [(str(left), str(right)) for left, right in zip(patterns, patterns[1:]) if str(left) != str(right)]
    return tuple(pairs[: max(0, int(limit))])


def _outer_pairs(patterns: Sequence[str], *, limit: int) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    count = len(patterns)
    for idx in range(count // 2):
        left = str(patterns[idx])
        right = str(patterns[-idx - 1])
        if left and right and left != right:
            result.append((left, right))
        if len(result) >= int(limit):
            break
    return tuple(result)


def _pattern_topology_adjustments(
    block: str,
    patterns: Sequence[str],
) -> tuple[tuple[str, tuple[str, ...], tuple[float, ...]], ...]:
    """Return safe pattern-candidate action handles for aesthetic replay.

    This does not assume the target spec has matching candidates.  The replay
    adapter only filters when a candidate matches at least one option, so these
    handles are safe across blocks while still exposing real search-space
    changes for specs that provide alternatives.
    """

    block_l = str(block or "").lower()
    rows: list[tuple[str, tuple[str, ...], tuple[float, ...]]] = []
    for pattern in tuple(dict.fromkeys(str(name) for name in patterns if str(name))):
        name = pattern.lower()
        if "resistor" in name or "ladder" in name or name.startswith("r_"):
            rows.append(
                (
                    pattern,
                    (
                        "*folded*",
                        "*snake*",
                        "*wide*",
                        "near_square",
                        "wide",
                    ),
                    (0.35, 0.5),
                )
            )
            continue
        if "feedback" in name or "output" in name:
            rows.append((pattern, ("*stack*", "*row*", "*col*", "near_square", "wide"), (0.35, 0.5)))
            continue
        if ("bjt" in name or "array" in name) and "bandgap" in block_l:
            rows.append((pattern, ("near_square", "*centered*", "*common*"), (0.8, 1.0, 1.2)))
            continue
        if "mos" in name or "pair" in name or "mirror" in name or "load" in name:
            rows.append((pattern, ("wide", "near_square", "*row*", "*stack*"), (0.8, 1.2, 2.0, 2.6)))
    return tuple(rows)


def _layout_tweakability_from_observation(observation: Mapping[str, Any]) -> Mapping[str, Any]:
    for row_obj in tuple(observation.get("observations", ()) or ()):
        row = _mapping(row_obj)
        if str(row.get("kind", "")) == "layout_tweakability_facts":
            return _mapping(row.get("data"))
    return {}


def _placement_window_operations_from_tweakability(
    tweak: Mapping[str, Any],
    *,
    block: str = "",
    max_windows: int,
) -> tuple[dict[str, Any], ...]:
    groups = {
        str(name): _mapping(row)
        for name, row in _mapping(tweak.get("groups")).items()
        if _sequence4(_mapping(row).get("bbox_tracks")) is not None
    }
    if len(groups) < 2:
        return ()
    anchor_name, anchor_bbox = _largest_group_bbox(groups)
    if not anchor_name or anchor_bbox is None:
        return ()

    global_bbox = _global_bbox_from_group_rows(groups)
    if global_bbox is None:
        return ()
    gx0, gy0, gx1, gy1 = global_bbox
    width = max(1, gx1 - gx0)
    height = max(1, gy1 - gy0)
    aspect = width / max(height, 1)
    ax0, ay0, ax1, ay1 = anchor_bbox
    empty_rect = _sequence4(_mapping(tweak.get("largest_empty_rect")).get("bbox_tracks"))
    candidate_rows: list[tuple[str, tuple[int, int, int, int], int]] = []
    for name, row in groups.items():
        if name == anchor_name:
            continue
        bbox = _sequence4(row.get("bbox_tracks"))
        if bbox is None:
            continue
        bx0, by0, bx1, by1 = [int(round(value)) for value in bbox]
        area = max(0, bx1 - bx0) * max(0, by1 - by0)
        if area <= 0:
            continue
        candidate_rows.append((name, (bx0, by0, bx1, by1), area))
    if not candidate_rows:
        return ()

    limit = max(0, int(max_windows))
    operations: list[dict[str, Any]] = []
    if aspect < 0.85:
        # Tall strip: expose a legal way for upper/lower satellites to move to
        # the side of the dominant anchor, which is how many manual analog
        # blocks avoid a pure vertical stack.
        above_or_below = [
            row
            for row in candidate_rows
            if row[1][1] >= ay1 or row[1][3] <= ay0
        ] or candidate_rows
        for idx, (name, bbox, _area) in enumerate(sorted(above_or_below, key=lambda row: (-row[2], row[0]))):
            bw = max(1, bbox[2] - bbox[0])
            bh = max(1, bbox[3] - bbox[1])
            target_x = ax1 + 1
            if target_x + bw > gx1:
                continue
            target_y = min(max(ay0 + idx * max(1, bh // 2), ay0), max(ay0, ay1 - bh))
            operations.append(
                _placement_window_operation(
                    name,
                    target_x_tracks=target_x,
                    target_y_tracks=target_y,
                    window_margin_tracks=2,
                    weight=28,
                    source_kind="side_fill_anchor",
                    block=block,
                    metadata={
                        "anchor": anchor_name,
                        "anchor_bbox_tracks": anchor_bbox,
                        "baseline_bbox_tracks": bbox,
                        "global_bbox_tracks": global_bbox,
                        "aspect_ratio": round(aspect, 6),
                        "source": "layout_observation",
                    },
                )
            )
            if limit and len(operations) >= limit:
                return tuple(operations)
    elif aspect > 1.18:
        # Wide strip: expose a soft way to stack satellites above the dominant
        # anchor instead of spreading everything horizontally.
        side_groups = [
            row
            for row in candidate_rows
            if row[1][0] >= ax1 or row[1][2] <= ax0
        ] or candidate_rows
        for idx, (name, bbox, _area) in enumerate(sorted(side_groups, key=lambda row: (-row[2], row[0]))):
            bw = max(1, bbox[2] - bbox[0])
            bh = max(1, bbox[3] - bbox[1])
            target_x = min(max(ax0 + idx * max(1, bw // 2), ax0), max(ax0, ax1 - bw))
            target_y = ay1 + 1
            if target_y + bh > gy1:
                continue
            operations.append(
                _placement_window_operation(
                    name,
                    target_x_tracks=target_x,
                    target_y_tracks=target_y,
                    window_margin_tracks=2,
                    weight=28,
                    source_kind="above_fill_anchor",
                    block=block,
                    metadata={
                        "anchor": anchor_name,
                        "anchor_bbox_tracks": anchor_bbox,
                        "baseline_bbox_tracks": bbox,
                        "global_bbox_tracks": global_bbox,
                        "aspect_ratio": round(aspect, 6),
                        "source": "layout_observation",
                    },
                )
            )
            if limit and len(operations) >= limit:
                return tuple(operations)

    empty_contacts = _mapping(tweak.get("largest_empty_rect_contacts_by_group"))
    if empty_rect is not None:
        ex0, ey0, ex1, ey1 = [int(round(value)) for value in empty_rect]
        ew = max(0, ex1 - ex0)
        eh = max(0, ey1 - ey0)
        contact_rows = []
        for name, row_obj in empty_contacts.items():
            if name == anchor_name or name not in groups:
                continue
            row = _mapping(row_obj)
            gaps = _mapping(row.get("signed_gap_tracks"))
            overlap = max(int(row.get("overlap_x_tracks", 0) or 0), int(row.get("overlap_y_tracks", 0) or 0))
            min_abs_gap = min(
                abs(int(value))
                for value in gaps.values()
                if isinstance(value, (int, float))
            ) if gaps else 10**9
            contact_rows.append((min_abs_gap, -overlap, str(name)))
        for _, _, name in sorted(contact_rows):
            bbox = _sequence4(groups[name].get("bbox_tracks"))
            if bbox is None:
                continue
            bx0, by0, bx1, by1 = [int(round(value)) for value in bbox]
            bw = max(1, bx1 - bx0)
            bh = max(1, by1 - by0)
            if bw > ew or bh > eh:
                continue
            target_x = min(max(ex0, 0), max(0, ex1 - bw))
            target_y = min(max(ey0, 0), max(0, ey1 - bh))
            operations.append(
                _placement_window_operation(
                    name,
                    target_x_tracks=target_x,
                    target_y_tracks=target_y,
                    window_margin_tracks=2,
                    weight=18,
                    source_kind="largest_empty_rect_fill",
                    block=block,
                    metadata={
                        "anchor": anchor_name,
                        "baseline_bbox_tracks": (bx0, by0, bx1, by1),
                        "largest_empty_rect_bbox_tracks": (ex0, ey0, ex1, ey1),
                        "source": "layout_observation",
                    },
                )
            )
            if limit and len(operations) >= limit:
                break
    return tuple(operations[:limit] if limit else operations)


def _placement_window_operation(
    target: str,
    *,
    target_x_tracks: int,
    target_y_tracks: int,
    window_margin_tracks: int,
    weight: int,
    source_kind: str,
    block: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    margin = max(0, int(window_margin_tracks))
    tx = max(0, int(target_x_tracks))
    ty = max(0, int(target_y_tracks))
    return {
        "op": "placement_window",
        "target": str(target),
        "min_x_tracks": max(0, tx - margin),
        "max_x_tracks": tx + margin,
        "min_y_tracks": max(0, ty - margin),
        "max_y_tracks": ty + margin,
        "target_x_tracks": tx,
        "target_y_tracks": ty,
        "solver": "global_smt",
        "hard": False,
        "weight": max(1, int(weight)),
        "risk": "observation-derived soft placement window; accept only if aesthetic score improves and physical precheck does not regress",
        "observation_refs": (
            "observations.OBS-T-001.data.groups",
            "observations.OBS-T-001.data.largest_empty_rect",
        ),
        "metadata": {
            **dict(metadata),
            "block": str(block),
            "source_kind": str(source_kind),
        },
    }


def _largest_group_bbox(
    groups: Mapping[str, Mapping[str, Any]],
) -> tuple[str, tuple[int, int, int, int] | None]:
    best_name = ""
    best_bbox: tuple[int, int, int, int] | None = None
    best_area = -1
    for name, row in groups.items():
        bbox = _sequence4(row.get("bbox_tracks"))
        if bbox is None:
            continue
        x0, y0, x1, y1 = [int(round(value)) for value in bbox]
        area = max(0, x1 - x0) * max(0, y1 - y0)
        if area > best_area:
            best_name = str(name)
            best_bbox = (x0, y0, x1, y1)
            best_area = area
    return best_name, best_bbox


def _global_bbox_from_group_rows(
    groups: Mapping[str, Mapping[str, Any]],
) -> tuple[int, int, int, int] | None:
    boxes = []
    for row in groups.values():
        bbox = _sequence4(row.get("bbox_tracks"))
        if bbox is None:
            continue
        boxes.append(tuple(int(round(value)) for value in bbox))
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _route_lane_adjustments(block: str) -> tuple[tuple[str, dict[str, Any]], ...]:
    block_l = str(block or "").lower()
    if "bandgap" in block_l:
        return (
            (
                "diode2",
                {
                    "layer": "M7",
                    "lane": -5,
                    "channel_side": "left",
                    "style": "reserved_channel",
                    "channel_orientation": "vertical",
                    "channel_offset_um": 1.0,
                    "terminal_escape_style": "outward",
                    "terminal_escape_um": 0.7,
                },
            ),
            (
                "VREF",
                {
                    "layer": "M8",
                    "lane": 0,
                    "channel_side": "internal",
                    "style": "reserved_channel",
                    "channel_orientation": "vertical",
                    "channel_offset_um": 0.8,
                    "terminal_escape_style": "outward",
                    "terminal_escape_um": 0.7,
                },
            ),
            (
                "ea_out",
                {
                    "layer": "M7",
                    "lane": 6,
                    "channel_side": "right",
                    "style": "reserved_channel",
                    "channel_orientation": "vertical",
                    "channel_offset_um": 0.9,
                    "terminal_escape_style": "outward",
                    "terminal_escape_um": 0.7,
                },
            ),
            (
                "nR2",
                {
                    "layer": "M9",
                    "lane": -8,
                    "style": "reserved_dogleg",
                    "dogleg_side": "above",
                    "dogleg_offset_um": 2.0,
                    "terminal_escape_style": "outward",
                    "terminal_escape_um": 1.0,
                },
            ),
            (
                "r2_mid_",
                {
                    "match": "prefix",
                    "layer": "M6",
                    "lane": -5,
                    "style": "reserved_dogleg",
                    "dogleg_side": "alternate",
                    "dogleg_offset_um": 1.4,
                    "terminal_escape_style": "outward",
                    "terminal_escape_um": 1.1,
                },
            ),
        )
    if "ldo" in block_l:
        return (
            (
                "VOUT",
                {
                    "layer": "M6",
                    "lane": -7,
                    "channel_side": "below",
                    "style": "reserved_channel",
                    "channel_orientation": "horizontal",
                    "channel_offset_um": 1.5,
                    "terminal_escape_style": "outward",
                    "terminal_escape_um": 0.8,
                },
            ),
            (
                "VFB",
                {
                    "layer": "M7",
                    "lane": 4,
                    "channel_side": "right",
                    "style": "reserved_channel",
                    "channel_orientation": "vertical",
                    "channel_offset_um": 0.9,
                    "terminal_escape_style": "outward",
                    "terminal_escape_um": 0.5,
                },
            ),
            (
                "VSS",
                {
                    "layer": "M5",
                    "lane": -6,
                    "channel_side": "left",
                    "style": "reserved_channel",
                    "channel_orientation": "vertical",
                    "channel_offset_um": 1.2,
                    "terminal_escape_style": "outward",
                    "terminal_escape_um": 0.6,
                },
            ),
        )
    return ()


def _rank_physical_route_shorts(shorts: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    """Rank shorts so scarce repair slots target the dominant conflicts first."""

    pair_counts = Counter(_short_pair_key(short) for short in shorts)

    def key(short: Mapping[str, Any]) -> tuple[object, ...]:
        pair = _short_pair_key(short)
        source_rank = _short_source_repairability_rank(short)
        movable = _movable_short_net(short)
        return (
            -int(pair_counts[pair]),
            -int(source_rank),
            str(pair[0]),
            str(pair[1]),
            str(pair[2]),
            str(movable),
            str(short.get("source_a", "")),
            str(short.get("source_b", "")),
        )

    return tuple(sorted(shorts, key=key))


def _short_pair_key(short: Mapping[str, Any]) -> tuple[str, str, str]:
    nets = tuple(sorted((str(short.get("net_a", "") or ""), str(short.get("net_b", "") or ""))))
    return (str(short.get("layer", "") or ""), nets[0] if nets else "", nets[1] if len(nets) > 1 else "")


def _short_source_repairability_rank(short: Mapping[str, Any]) -> int:
    source_a = str(short.get("source_a", "") or "")
    source_b = str(short.get("source_b", "") or "")
    path_count = int(source_a.startswith("path[")) + int(source_b.startswith("path["))
    rect_count = int(source_a.startswith("rect[")) + int(source_b.startswith("rect["))
    if path_count and rect_count:
        return 3
    if path_count >= 2:
        return 2
    if path_count:
        return 1
    return 0


def _route_short_repair_operation(
    short: Mapping[str, Any],
    *,
    block: str,
    index: int,
) -> dict[str, Any] | None:
    layer = str(short.get("layer", "") or "")
    net = _movable_short_net(short)
    if not layer or not net:
        return None
    target_layer = _next_route_layer(layer)
    lane = _repair_lane_for_net(net, index=index)
    return {
        "op": "route_lane",
        "route_name": net,
        "layer": target_layer,
        "lane": lane,
        "channel_side": _repair_channel_side(net, block=block),
        "solver": "routing_eco",
        "risk": "generated from physical same-layer short; accept only if physical short count and aesthetic score do not regress",
        "observation_refs": (
            "physical_connectivity_report.shorts",
            "scores.route_escape",
            "scores.route_symmetry_distribution",
        ),
        "metadata": {
            "match": "prefix" if net.endswith("_") else "net",
            "style": "reserved_channel" if not net.endswith("_") else "reserved_dogleg",
            "channel_orientation": "vertical",
            "terminal_escape_style": "outward",
            "terminal_escape_um": 0.8,
            "dogleg_side": "alternate" if net.endswith("_") else "",
            "dogleg_offset_um": 1.2 if net.endswith("_") else None,
            "source": "physical_route_short",
            "source_short": {
                "layer": layer,
                "net_a": str(short.get("net_a", "")),
                "net_b": str(short.get("net_b", "")),
                "source_a": str(short.get("source_a", "")),
                "source_b": str(short.get("source_b", "")),
            },
        },
    }


def _dedupe_tweak_operations(operations: Sequence[object]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[object, ...]] = set()
    for raw in operations:
        op = dict(_mapping(raw))
        if not op:
            continue
        key = _tweak_operation_key(op)
        if key in seen:
            continue
        seen.add(key)
        result.append(op)
    return result


def _prune_physical_route_short_operations(
    operations: Sequence[Mapping[str, Any]],
    *,
    max_repairs: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    kept_repairs = 0
    limit = max(0, int(max_repairs))
    for raw in operations:
        op = dict(_mapping(raw))
        if _mapping(_mapping(op).get("metadata")).get("source") == "physical_route_short":
            if kept_repairs >= limit:
                continue
            kept_repairs += 1
        result.append(op)
    return result


def _physical_route_short_operation_count(operations: Sequence[Mapping[str, Any]]) -> int:
    return sum(
        1
        for op in operations
        if _mapping(_mapping(op).get("metadata")).get("source") == "physical_route_short"
    )


def _tweak_operation_key(op: Mapping[str, Any]) -> tuple[object, ...]:
    metadata = _mapping(op.get("metadata"))
    source_short = _source_short_key(metadata.get("source_short"))
    if source_short:
        return ("physical_route_short",) + source_short
    return (
        str(op.get("op", "")),
        str(op.get("source", "")),
        str(op.get("target", "")),
        op.get("min_x_tracks"),
        op.get("max_x_tracks"),
        op.get("min_y_tracks"),
        op.get("max_y_tracks"),
        op.get("target_x_tracks"),
        op.get("target_y_tracks"),
        str(op.get("route_name", "")),
        str(op.get("layer", "")),
        op.get("lane"),
        str(op.get("channel_side", "")),
        json.dumps(_jsonable(metadata), sort_keys=True, separators=(",", ":")),
    )


def _source_short_key(value: object) -> tuple[object, ...]:
    row = _mapping(value)
    if not row:
        return ()
    nets = tuple(sorted((str(row.get("net_a", "")), str(row.get("net_b", "")))))
    return (
        str(row.get("layer", "")),
        *nets,
        str(row.get("source_a", "")),
        str(row.get("source_b", "")),
    )


def _movable_short_net(short: Mapping[str, Any]) -> str:
    net_a = str(short.get("net_a", "") or "")
    net_b = str(short.get("net_b", "") or "")
    source_a = str(short.get("source_a", "") or "")
    source_b = str(short.get("source_b", "") or "")
    path_a = source_a.startswith("path[")
    path_b = source_b.startswith("path[")
    supply = {"VSS", "VDD", "GND", "AVSS", "AVDD", "DVSS", "DVDD"}
    if path_a and net_a and net_a not in supply:
        return net_a
    if path_b and net_b and net_b not in supply:
        return net_b
    if path_a and net_a:
        return net_a
    if path_b and net_b:
        return net_b
    if net_a and net_a not in supply:
        return net_a
    if net_b and net_b not in supply:
        return net_b
    return net_a or net_b


def _next_route_layer(layer: str) -> str:
    normalized = str(layer or "").upper()
    if normalized.startswith("M"):
        try:
            number = int(normalized[1:])
        except ValueError:
            number = 0
        if number > 0:
            return f"M{min(10, number + 1)}"
    return normalized or "M7"


def _repair_lane_for_net(net: str, *, index: int) -> int:
    text = str(net or "")
    total = sum(ord(ch) for ch in text)
    sign = -1 if total % 2 else 1
    magnitude = 4 + (int(index) % 5)
    return sign * magnitude


def _repair_channel_side(net: str, *, block: str) -> str:
    text = str(net or "").lower()
    if "vref" in text or "diode" in text or "bias" in text:
        return "left"
    if "out" in text or "nr" in text:
        return "right"
    if "vss" in text:
        return "left"
    return "internal"


def _safe_name(value: object) -> str:
    text = str(value or "aesthetic_feedback")
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in text) or "aesthetic_feedback"


def _append_note(notes: str, addition: str) -> str:
    base = str(notes or "").strip()
    extra = str(addition or "").strip()
    if not base:
        return extra
    if not extra:
        return base
    return f"{base} {extra}"


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
