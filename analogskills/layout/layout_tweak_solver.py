"""Local SMT solver for fine-grained layout tweak patches.

This solver operates on observation-level group boxes.  It does not choose
PCell realizations, rewrite pattern candidates, or generate physical geometry.
Its job is to answer a narrow question quickly: can a proposed micro-adjustment
move the selected group boxes inside the current layout envelope without
creating group-box overlaps?
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None

from .layout_tweak import (
    LayoutTweakOperation,
    LayoutTweakPatch,
    layout_tweak,
    layout_tweak_patch_from_dict,
    layout_tweak_patch_to_dict,
    layout_tweak_patch_to_placement_windows,
    layout_tweak_patch_with_operation_hardness,
    write_layout_tweak_patch_json,
)


@dataclass(frozen=True)
class LayoutTweakLocalSolveResult:
    passed: bool
    bboxes_tracks: Mapping[str, tuple[int, int, int, int]]
    deltas_tracks: Mapping[str, tuple[int, int]]
    objective: int
    checks: Mapping[str, object]


@dataclass(frozen=True)
class LayoutTweakLocalCandidateResult:
    patch: LayoutTweakPatch
    solve_result: LayoutTweakLocalSolveResult
    before_metrics: Mapping[str, object]
    after_metrics: Mapping[str, object]
    score: int
    accepted_by_proxy: bool
    checks: Mapping[str, object]


def solve_layout_tweak_local(
    observation: Mapping[str, Any],
    patch: LayoutTweakPatch | Mapping[str, Any],
    *,
    movable_groups: Sequence[str] = (),
    spacing_tracks: int = 0,
    keep_within_current_bbox: bool = True,
    solver_timeout_ms: int = 1_000,
) -> LayoutTweakLocalSolveResult:
    """Solve a small fixed-size group-box micro-adjustment problem."""

    if z3 is None:  # pragma: no cover
        return LayoutTweakLocalSolveResult(
            False,
            {},
            {},
            0,
            {"passed": False, "issues": ("z3-solver is required for local layout tweak solving",)},
        )
    patch_obj = patch if isinstance(patch, LayoutTweakPatch) else layout_tweak_patch_from_dict(patch)
    original = _observation_group_bboxes(observation)
    if not original:
        return LayoutTweakLocalSolveResult(
            False,
            {},
            {},
            0,
            {"passed": False, "issues": ("observation group bboxes are not available",)},
        )
    windows = layout_tweak_patch_to_placement_windows(patch_obj, observation=observation)
    movable = tuple(dict.fromkeys(str(name) for name in movable_groups if str(name)))
    if not movable:
        movable = tuple(dict.fromkeys(window.pattern for window in windows if window.pattern in original))
    if not movable:
        return LayoutTweakLocalSolveResult(
            False,
            original,
            {},
            0,
            {"passed": False, "issues": ("no movable groups were selected by the tweak patch",)},
        )

    global_bbox = _global_bbox_tracks(observation, original)
    solver = z3.Optimize()
    try:
        solver.set(timeout=max(1, int(solver_timeout_ms)))
    except Exception:
        pass

    width = {name: original[name][2] - original[name][0] for name in original}
    height = {name: original[name][3] - original[name][1] for name in original}
    x: dict[str, object] = {}
    y: dict[str, object] = {}
    for name, bbox in original.items():
        if name in movable:
            x[name] = z3.Int(f"ltw_x__{_safe_symbol_name(name)}")
            y[name] = z3.Int(f"ltw_y__{_safe_symbol_name(name)}")
        else:
            x[name] = z3.IntVal(int(bbox[0]))
            y[name] = z3.IntVal(int(bbox[1]))

    for name in movable:
        solver.add(x[name] >= 0, y[name] >= 0)
        if keep_within_current_bbox and global_bbox is not None:
            gx0, gy0, gx1, gy1 = global_bbox
            solver.add(
                x[name] >= int(gx0),
                y[name] >= int(gy0),
                x[name] + int(width[name]) <= int(gx1),
                y[name] + int(height[name]) <= int(gy1),
            )

    names = tuple(original)
    spacing = max(0, int(spacing_tracks))
    relaxed_spacing_pair_count = 0
    baseline_overlap_pair_count = 0
    for idx, left in enumerate(names):
        for right in names[idx + 1 :]:
            if _baseline_pair_overlaps(original[left], original[right]):
                baseline_overlap_pair_count += 1
                continue
            pair_spacing = spacing
            if spacing > 0 and not _baseline_pair_can_preserve_spacing(original[left], original[right], spacing):
                pair_spacing = 0
                relaxed_spacing_pair_count += 1
            solver.add(
                z3.Or(
                    x[left] + int(width[left]) + pair_spacing <= x[right],
                    x[right] + int(width[right]) + pair_spacing <= x[left],
                    y[left] + int(height[left]) + pair_spacing <= y[right],
                    y[right] + int(height[right]) + pair_spacing <= y[left],
                )
            )

    window_terms: list[object] = []
    hard_window_count = 0
    for window in windows:
        pattern = str(window.pattern)
        if pattern not in movable:
            continue
        expr_parts: list[object] = []
        hard_window_count += int(bool(window.hard))
        for value, expr, sense in (
            (window.min_x_tracks, x[pattern], ">="),
            (window.max_x_tracks, x[pattern], "<="),
            (window.min_y_tracks, y[pattern], ">="),
            (window.max_y_tracks, y[pattern], "<="),
        ):
            if value is None:
                continue
            bound = int(value)
            if sense == ">=":
                if window.hard:
                    solver.add(expr >= bound)
                else:
                    expr_parts.append(_z3_pos(bound - expr))
            else:
                if window.hard:
                    solver.add(expr <= bound)
                else:
                    expr_parts.append(_z3_pos(expr - bound))
        if window.target_x_tracks is not None:
            target = int(window.target_x_tracks)
            if window.hard:
                solver.add(x[pattern] == target)
            else:
                expr_parts.append(_z3_abs(x[pattern] - target))
        if window.target_y_tracks is not None:
            target = int(window.target_y_tracks)
            if window.hard:
                solver.add(y[pattern] == target)
            else:
                expr_parts.append(_z3_abs(y[pattern] - target))
        if expr_parts and not window.hard:
            window_terms.append(max(1, int(window.weight)) * z3.Sum(expr_parts))

    movement_terms = [
        _z3_abs(x[name] - int(original[name][0])) + _z3_abs(y[name] - int(original[name][1]))
        for name in movable
    ]
    bbox_terms = []
    if global_bbox is not None:
        bbox_terms = [
            x[name] + int(width[name])
            + y[name] + int(height[name])
            for name in movable
        ]
    objective = (
        1000 * (z3.Sum(window_terms) if window_terms else z3.IntVal(0))
        + 10 * (z3.Sum(movement_terms) if movement_terms else z3.IntVal(0))
        + (z3.Sum(bbox_terms) if bbox_terms else z3.IntVal(0))
    )
    solver.minimize(objective)
    check_result = solver.check()
    if check_result != z3.sat:
        return LayoutTweakLocalSolveResult(
            False,
            original,
            {},
            0,
            {
                "passed": False,
                "z3_result": str(check_result),
                "movable_groups": movable,
                "placement_window_count": len(windows),
                "hard_placement_window_count": hard_window_count,
                "spacing_tracks": spacing,
                "relaxed_spacing_pair_count": int(relaxed_spacing_pair_count),
                "baseline_overlap_pair_count": int(baseline_overlap_pair_count),
            },
        )

    model = solver.model()
    solved: dict[str, tuple[int, int, int, int]] = {}
    deltas: dict[str, tuple[int, int]] = {}
    for name, bbox in original.items():
        sx = _model_int(model, x[name])
        sy = _model_int(model, y[name])
        solved[name] = (sx, sy, sx + int(width[name]), sy + int(height[name]))
        if name in movable:
            deltas[name] = (sx - int(bbox[0]), sy - int(bbox[1]))
    obj_value = _model_int(model, objective)
    return LayoutTweakLocalSolveResult(
        True,
        solved,
        deltas,
        obj_value,
        {
            "passed": True,
            "z3_result": str(check_result),
            "movable_groups": movable,
            "placement_window_count": len(windows),
            "hard_placement_window_count": hard_window_count,
            "spacing_tracks": spacing,
            "relaxed_spacing_pair_count": int(relaxed_spacing_pair_count),
            "baseline_overlap_pair_count": int(baseline_overlap_pair_count),
            "keep_within_current_bbox": bool(keep_within_current_bbox),
        },
    )


def generate_local_nudge_candidates_from_observation(
    observation: Mapping[str, Any],
    *,
    groups: Sequence[str] = (),
    step_tracks: Sequence[int] = (1, 2, 4, 8),
    directions: Sequence[str] = ("left", "right", "down", "up"),
    hard: bool = True,
    window_margin_tracks: int = 1,
    baseline_layout_id: str = "",
) -> tuple[LayoutTweakPatch, ...]:
    """Generate simple one-group nudge patches from observation group boxes."""

    bboxes = _observation_group_bboxes(observation)
    selected_groups = tuple(dict.fromkeys(str(name) for name in groups if str(name)))
    if not selected_groups:
        selected_groups = tuple(sorted(bboxes))
    steps = tuple(dict.fromkeys(max(1, int(value)) for value in step_tracks if int(value) != 0))
    direction_rows = tuple(_direction_delta(direction) for direction in directions)
    patches: list[LayoutTweakPatch] = []
    for group in selected_groups:
        if group not in bboxes:
            continue
        for direction, dx_sign, dy_sign in direction_rows:
            for step in steps:
                dx = int(dx_sign) * int(step)
                dy = int(dy_sign) * int(step)
                patch_id = f"local_nudge_{group}_{direction}_{step}t"
                patches.append(
                    layout_tweak(patch_id, baseline_layout_id=baseline_layout_id)
                    .nudge(
                        group,
                        dx_tracks=dx,
                        dy_tracks=dy,
                        axis="both",
                        window_margin_tracks=int(window_margin_tracks),
                        hard=bool(hard),
                        weight=120 if bool(hard) else 60,
                        observation_refs=(
                            f"observations.OBS-T-001.data.groups.{group}.origin_tracks",
                            "compactness.whitespace.largest_empty_rect_bbox_tracks",
                        ),
                        metadata={"baseline_bbox_tracks": tuple(int(v) for v in bboxes[group])},
                        risk="local fixed-bbox nudge candidate; requires downstream route/DRC validation",
                    )
                    .acceptance(
                        hard_constraints="pass_or_unchanged",
                        direct_geometry_mutation=False,
                        compare_against_baseline=True,
                    )
                    .notes("Generated from observation group bboxes for local SMT screening.")
                    .build()
                )
    return tuple(patches)


def solve_local_nudge_candidates(
    observation: Mapping[str, Any],
    *,
    candidates: Sequence[LayoutTweakPatch | Mapping[str, Any]] | None = None,
    groups: Sequence[str] = (),
    step_tracks: Sequence[int] = (1, 2, 4, 8),
    directions: Sequence[str] = ("left", "right", "down", "up"),
    hard: bool = True,
    window_margin_tracks: int = 1,
    spacing_tracks: int = 1,
    solver_timeout_ms: int = 1_000,
    max_candidates: int = 64,
    replay_penalties: Mapping[tuple[object, ...], int] | None = None,
) -> tuple[LayoutTweakLocalCandidateResult, ...]:
    """Generate/solve local nudge candidates and rank by proxy compactness."""

    generated = tuple(candidates) if candidates is not None else generate_local_nudge_candidates_from_observation(
        observation,
        groups=groups,
        step_tracks=step_tracks,
        directions=directions,
        hard=bool(hard),
        window_margin_tracks=int(window_margin_tracks),
    )
    baseline_bboxes = _observation_group_bboxes(observation)
    bbox_tracks = _global_bbox_tracks(observation, baseline_bboxes)
    before_metrics = layout_tweak_local_metrics_from_bboxes(baseline_bboxes, bbox_tracks=bbox_tracks)
    rows: list[LayoutTweakLocalCandidateResult] = []
    for raw_patch in generated[: max(0, int(max_candidates))]:
        patch = raw_patch if isinstance(raw_patch, LayoutTweakPatch) else layout_tweak_patch_from_dict(raw_patch)
        solve = solve_layout_tweak_local(
            observation,
            patch,
            spacing_tracks=spacing_tracks,
            solver_timeout_ms=solver_timeout_ms,
        )
        after_metrics = layout_tweak_local_metrics_from_bboxes(solve.bboxes_tracks, bbox_tracks=bbox_tracks)
        raw_score = _local_candidate_score(before_metrics, after_metrics, solve)
        replay_penalty = _layout_tweak_replay_penalty_for_patch(patch, replay_penalties or {})
        score = int(raw_score) - int(replay_penalty)
        rows.append(
            LayoutTweakLocalCandidateResult(
                patch,
                solve,
                before_metrics,
                after_metrics,
                score,
                bool(solve.passed and score > 0),
                {
                    "spacing_tracks": int(spacing_tracks),
                    "solver_timeout_ms": int(solver_timeout_ms),
                    "raw_score": int(raw_score),
                    "replay_penalty": int(replay_penalty),
                },
            )
        )
    return tuple(sorted(rows, key=lambda row: (-int(row.accepted_by_proxy), -int(row.score), row.patch.patch_id)))


def layout_tweak_replay_penalties_from_report(report: Mapping[str, Any] | str | Path) -> dict[tuple[object, ...], int]:
    """Extract operation-level penalties from rejected candidate replay results."""

    if isinstance(report, (str, Path)):
        try:
            payload = json.loads(Path(report).read_text(encoding="utf-8"))
        except Exception:
            return {}
    else:
        payload = dict(report)
    result: dict[tuple[object, ...], int] = {}
    for row_obj in tuple(_mapping(payload).get("results", ()) or ()):
        row = _mapping(row_obj)
        if bool(row.get("accepted", False)):
            continue
        reasons = tuple(str(reason) for reason in tuple(row.get("reasons", ()) or ()))
        penalty = _replay_rejection_penalty(reasons, returncode=int(row.get("returncode", 0) or 0), timed_out=bool(row.get("timed_out", False)))
        if penalty <= 0:
            continue
        patch_path = Path(str(row.get("candidate_path", "")))
        if not patch_path.exists():
            continue
        try:
            patch = layout_tweak_patch_from_dict(json.loads(patch_path.read_text(encoding="utf-8")))
        except Exception:
            continue
        for operation in patch.operations:
            for key in _layout_tweak_operation_penalty_keys(operation):
                result[key] = max(int(result.get(key, 0)), int(penalty))
    return result


def generate_combined_layout_tweak_candidates(
    results: Sequence[LayoutTweakLocalCandidateResult],
    *,
    max_combo_size: int = 2,
    max_base_candidates: int = 8,
    max_combinations: int = 64,
    require_unique_targets: bool = True,
    include_unaccepted_feasible: bool = True,
) -> tuple[LayoutTweakPatch, ...]:
    """Combine top local candidates into small replayable multi-operation patches."""

    if max_combo_size < 2 or max_base_candidates <= 0 or max_combinations <= 0:
        return ()
    base_rows = [
        row
        for row in results
        if row.solve_result.passed and (row.accepted_by_proxy or bool(include_unaccepted_feasible))
    ]
    base_rows.sort(key=lambda row: (-int(row.accepted_by_proxy), -int(row.score), row.patch.patch_id))
    bases = tuple(row.patch for row in base_rows[: max(0, int(max_base_candidates))])
    patches: list[LayoutTweakPatch] = []
    seen_ids: set[str] = set()
    for size in range(2, max(2, int(max_combo_size)) + 1):
        for combo in combinations(bases, size):
            if len(patches) >= int(max_combinations):
                return tuple(patches)
            targets = tuple(target for patch in combo for target in _patch_operation_targets(patch))
            if require_unique_targets and len(set(targets)) != len(targets):
                continue
            patch_id = _safe_symbol_name("combo__" + "__".join(patch.patch_id for patch in combo))
            if patch_id in seen_ids:
                continue
            seen_ids.add(patch_id)
            operations = tuple(operation for patch in combo for operation in patch.operations)
            observation_refs = tuple(
                dict.fromkeys(ref for patch in combo for ref in tuple(patch.observation_refs or ()))
            )
            patches.append(
                LayoutTweakPatch(
                    patch_id=patch_id,
                    baseline_layout_id=combo[0].baseline_layout_id,
                    observation_refs=observation_refs,
                    operations=operations,
                    acceptance={
                        "hard_constraints": "pass_or_unchanged",
                        "direct_geometry_mutation": False,
                        "compare_against_baseline": True,
                        "component_patch_ids": tuple(patch.patch_id for patch in combo),
                    },
                    notes="Combined from accepted local SMT nudge candidates.",
                )
            )
    return tuple(patches)


def layout_tweak_candidate_patches_from_patch(
    patch: LayoutTweakPatch | Mapping[str, Any],
    *,
    operation_kinds: Sequence[str] = ("nudge", "placement_window"),
    include_all: bool = True,
    include_single: bool = True,
    max_candidates: int = 64,
) -> tuple[LayoutTweakPatch, ...]:
    """Split replayable adjustment operations into local-screenable patches.

    This is the bridge from factual/aesthetic feedback to actionable adjustment
    handles.  A feedback patch may contain compact-gap, pattern, route, and pin
    policy operations; the local tweak solver can only evaluate fixed-bbox
    placement handles.  This helper extracts exactly those handles and emits
    small candidate patches that can be screened before global replay.
    """

    patch_obj = patch if isinstance(patch, LayoutTweakPatch) else layout_tweak_patch_from_dict(patch)
    selected_kinds = {str(kind).lower() for kind in operation_kinds}
    operations = tuple(
        operation
        for operation in patch_obj.operations
        if str(operation.op).lower() in selected_kinds and _operation_has_local_screen_target(operation)
    )
    if not operations or max_candidates <= 0:
        return ()
    candidates: list[LayoutTweakPatch] = []
    if include_all and len(operations) > 1:
        candidates.append(
            LayoutTweakPatch(
                patch_id=_safe_symbol_name(f"{patch_obj.patch_id or 'patch'}__placement_all"),
                baseline_layout_id=patch_obj.baseline_layout_id,
                observation_refs=tuple(patch_obj.observation_refs or ()),
                operations=operations,
                acceptance={
                    "hard_constraints": "pass_or_unchanged",
                    "direct_geometry_mutation": False,
                    "compare_against_baseline": True,
                    "source_patch_id": patch_obj.patch_id,
                },
                notes="All locally screenable placement handles extracted from feedback patch.",
            )
        )
    if include_single:
        for index, operation in enumerate(operations):
            if len(candidates) >= int(max_candidates):
                break
            target = str(operation.target or operation.target_group or f"op{index}")
            candidates.append(
                LayoutTweakPatch(
                    patch_id=_safe_symbol_name(f"{patch_obj.patch_id or 'patch'}__{operation.op}_{target}_{index}"),
                    baseline_layout_id=patch_obj.baseline_layout_id,
                    observation_refs=tuple(patch_obj.observation_refs or ()),
                    operations=(operation,),
                    acceptance={
                        "hard_constraints": "pass_or_unchanged",
                        "direct_geometry_mutation": False,
                        "compare_against_baseline": True,
                        "source_patch_id": patch_obj.patch_id,
                    },
                    notes="Single locally screenable placement handle extracted from feedback patch.",
                )
            )
    return tuple(candidates[: int(max_candidates)])


def layout_tweak_local_metrics_from_bboxes(
    bboxes_tracks: Mapping[str, Sequence[int]],
    *,
    bbox_tracks: Sequence[int] | Sequence[float] | None = None,
) -> dict[str, object]:
    """Compute compactness proxy metrics from fixed group boxes."""

    bboxes = {
        str(name): tuple(int(round(value)) for value in bbox)
        for name, bbox in bboxes_tracks.items()
        if _sequence4(bbox) is not None
    }
    if not bboxes:
        return {"status": "not_available", "reason": "group_bboxes_unavailable"}
    if bbox_tracks is not None and len(tuple(bbox_tracks)) == 4:
        bb = tuple(int(round(value)) for value in tuple(bbox_tracks))  # type: ignore[arg-type]
        width = max(0, bb[2] - bb[0])
        height = max(0, bb[3] - bb[1])
        origin = (bb[0], bb[1])
    elif bbox_tracks is not None and len(tuple(bbox_tracks)) == 2:
        width = max(0, int(round(tuple(bbox_tracks)[0])))
        height = max(0, int(round(tuple(bbox_tracks)[1])))
        origin = (0, 0)
    else:
        x0 = min(bbox[0] for bbox in bboxes.values())
        y0 = min(bbox[1] for bbox in bboxes.values())
        x1 = max(bbox[2] for bbox in bboxes.values())
        y1 = max(bbox[3] for bbox in bboxes.values())
        width = max(0, x1 - x0)
        height = max(0, y1 - y0)
        origin = (x0, y0)
    global_bbox = [int(origin[0]), int(origin[1]), int(origin[0]) + int(width), int(origin[1]) + int(height)]
    whitespace = _whitespace_metrics_for_bboxes(tuple(bboxes.values()), width, height, origin=origin)
    union_bbox = [
        min(bbox[0] for bbox in bboxes.values()),
        min(bbox[1] for bbox in bboxes.values()),
        max(bbox[2] for bbox in bboxes.values()),
        max(bbox[3] for bbox in bboxes.values()),
    ]
    union_width = max(0, union_bbox[2] - union_bbox[0])
    union_height = max(0, union_bbox[3] - union_bbox[1])
    union_area = union_width * union_height
    overlap = _pair_overlap_metrics_for_bboxes(bboxes)
    internal_whitespace = _prefix_keys(
        _whitespace_metrics_for_bboxes(
            tuple(bboxes.values()),
            union_width,
            union_height,
            origin=(int(union_bbox[0]), int(union_bbox[1])),
        ),
        "internal_",
    )
    boundary = _boundary_pressure_metrics_for_bboxes(bboxes, tuple(global_bbox))
    return {
        "status": "pass",
        "global_bbox_tracks": global_bbox,
        "global_area_tracks2": width * height,
        "group_union_bbox_tracks": union_bbox,
        "group_union_width_tracks": union_width,
        "group_union_height_tracks": union_height,
        "group_union_area_tracks2": union_area,
        "group_union_perimeter_tracks": 2 * (union_width + union_height),
        "exterior_deadspace_area_tracks2": max(0, width * height - union_area),
        **overlap,
        **boundary,
        **whitespace,
        **internal_whitespace,
    }


def layout_tweak_local_candidate_result_to_dict(result: LayoutTweakLocalCandidateResult) -> dict[str, object]:
    return {
        "patch": layout_tweak_patch_to_dict(result.patch),
        "solve_result": layout_tweak_local_solve_result_to_dict(result.solve_result),
        "before_metrics": dict(result.before_metrics),
        "after_metrics": dict(result.after_metrics),
        "score": int(result.score),
        "accepted_by_proxy": bool(result.accepted_by_proxy),
        "checks": dict(result.checks),
    }


def write_layout_tweak_local_refinement_report(
    results: Sequence[LayoutTweakLocalCandidateResult],
    path: str | Path,
) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "candidate_count": len(tuple(results)),
        "results": [layout_tweak_local_candidate_result_to_dict(row) for row in results],
    }
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def select_best_layout_tweak_local_candidate(
    results: Sequence[LayoutTweakLocalCandidateResult],
    *,
    accepted_only: bool = True,
) -> LayoutTweakLocalCandidateResult | None:
    """Return the top ranked candidate that should be replayed globally."""

    rows = tuple(results)
    if accepted_only:
        accepted = tuple(row for row in rows if row.accepted_by_proxy)
        if accepted:
            return accepted[0]
    return rows[0] if rows else None


def write_best_layout_tweak_local_patch(
    results: Sequence[LayoutTweakLocalCandidateResult],
    path: str | Path,
    *,
    accepted_only: bool = True,
    output_hard: bool | None = None,
    output_weight: int | None = None,
) -> Path | None:
    """Write the best local candidate as a replayable layout tweak patch."""

    best = select_best_layout_tweak_local_candidate(results, accepted_only=accepted_only)
    if best is None:
        return None
    patch = _output_patch_with_optional_hardness(best.patch, output_hard=output_hard, output_weight=output_weight)
    return write_layout_tweak_patch_json(patch, path)


def write_top_layout_tweak_local_patches(
    results: Sequence[LayoutTweakLocalCandidateResult],
    directory: str | Path,
    *,
    limit: int = 5,
    accepted_only: bool = True,
    output_hard: bool | None = None,
    output_weight: int | None = None,
) -> tuple[Path, ...]:
    """Write the top ranked replayable tweak patches into a directory."""

    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    selected: list[LayoutTweakLocalCandidateResult] = []
    for row in results:
        if accepted_only and not row.accepted_by_proxy:
            continue
        selected.append(row)
        if len(selected) >= max(0, int(limit)):
            break
    paths: list[Path] = []
    for rank, row in enumerate(selected, start=1):
        path = out_dir / f"rank{rank:02d}_{_safe_symbol_name(row.patch.patch_id)}.json"
        patch = _output_patch_with_optional_hardness(row.patch, output_hard=output_hard, output_weight=output_weight)
        paths.append(write_layout_tweak_patch_json(patch, path))
    return tuple(paths)


def _output_patch_with_optional_hardness(
    patch: LayoutTweakPatch,
    *,
    output_hard: bool | None,
    output_weight: int | None,
) -> LayoutTweakPatch:
    if output_hard is None and output_weight is None:
        return patch
    return layout_tweak_patch_with_operation_hardness(
        patch,
        hard=bool(output_hard) if output_hard is not None else any(operation.hard for operation in patch.operations),
        weight=output_weight,
    )


def write_layout_tweak_local_refinement_markdown(
    results: Sequence[LayoutTweakLocalCandidateResult],
    path: str | Path,
    *,
    title: str = "Layout Tweak Local Refinement",
) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", ""]
    lines.append("| rank | patch | passed | accepted_proxy | score | deltas | union_area_after | internal_void_before | internal_void_after | global_void_after |")
    lines.append("|---:|---|---:|---:|---:|---|---:|---:|---:|---:|")
    for index, row in enumerate(results, start=1):
        before = row.before_metrics
        after = row.after_metrics
        lines.append(
            "| {rank} | `{patch}` | {passed} | {accepted} | {score} | `{deltas}` | {union_area} | {before_internal_void} | {after_internal_void} | {global_void} |".format(
                rank=index,
                patch=row.patch.patch_id,
                passed=str(row.solve_result.passed),
                accepted=str(row.accepted_by_proxy),
                score=int(row.score),
                deltas=dict(row.solve_result.deltas_tracks),
                union_area=int(after.get("group_union_area_tracks2", 0) or 0),
                before_internal_void=int(before.get("internal_largest_empty_rect_area_tracks2", 0) or 0),
                after_internal_void=int(after.get("internal_largest_empty_rect_area_tracks2", 0) or 0),
                global_void=int(after.get("largest_empty_rect_area_tracks2", 0) or 0),
            )
        )
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def layout_tweak_local_solve_result_to_dict(result: LayoutTweakLocalSolveResult) -> dict[str, object]:
    return {
        "passed": bool(result.passed),
        "bboxes_tracks": {
            str(name): [int(v) for v in bbox]
            for name, bbox in sorted(result.bboxes_tracks.items())
        },
        "deltas_tracks": {
            str(name): [int(v) for v in delta]
            for name, delta in sorted(result.deltas_tracks.items())
        },
        "objective": int(result.objective),
        "checks": dict(result.checks),
    }


def _local_candidate_score(
    before: Mapping[str, object],
    after: Mapping[str, object],
    solve: LayoutTweakLocalSolveResult,
) -> int:
    if not solve.passed:
        return -1_000_000
    before_internal_void = int(before.get("internal_largest_empty_rect_area_tracks2", before.get("largest_empty_rect_area_tracks2", 0)) or 0)
    after_internal_void = int(after.get("internal_largest_empty_rect_area_tracks2", after.get("largest_empty_rect_area_tracks2", 0)) or 0)
    before_internal_empty = int(before.get("internal_empty_area_tracks2", before.get("empty_area_tracks2", 0)) or 0)
    after_internal_empty = int(after.get("internal_empty_area_tracks2", after.get("empty_area_tracks2", 0)) or 0)
    before_global_void = int(before.get("largest_empty_rect_area_tracks2", 0) or 0)
    after_global_void = int(after.get("largest_empty_rect_area_tracks2", 0) or 0)
    before_union = int(before.get("group_union_area_tracks2", 0) or 0)
    after_union = int(after.get("group_union_area_tracks2", 0) or 0)
    before_perimeter = int(before.get("group_union_perimeter_tracks", 0) or 0)
    after_perimeter = int(after.get("group_union_perimeter_tracks", 0) or 0)
    union_delta = before_union - after_union
    perimeter_delta = before_perimeter - after_perimeter
    before_edge_touch = int(before.get("edge_touch_group_count", 0) or 0)
    after_edge_touch = int(after.get("edge_touch_group_count", 0) or 0)
    before_near_edge = int(before.get("near_edge_group_count", 0) or 0)
    after_near_edge = int(after.get("near_edge_group_count", 0) or 0)
    before_boundary_pressure = int(before.get("boundary_pressure_tracks", 0) or 0)
    after_boundary_pressure = int(after.get("boundary_pressure_tracks", 0) or 0)
    before_overlap_area = int(before.get("pair_overlap_area_tracks2", 0) or 0)
    after_overlap_area = int(after.get("pair_overlap_area_tracks2", 0) or 0)
    before_overlap_count = int(before.get("pair_overlap_count", 0) or 0)
    after_overlap_count = int(after.get("pair_overlap_count", 0) or 0)
    movement = sum(abs(dx) + abs(dy) for dx, dy in solve.deltas_tracks.values())
    large_move_without_envelope_gain = max(0, movement - 4) if union_delta <= 0 and perimeter_delta <= 0 else 0
    return (
        25 * (before_internal_void - after_internal_void)
        + 4 * (before_internal_empty - after_internal_empty)
        + 6 * union_delta
        + 10 * perimeter_delta
        + 2 * (before_global_void - after_global_void)
        - 3000 * max(0, after_edge_touch - before_edge_touch)
        - 800 * max(0, after_near_edge - before_near_edge)
        - 300 * max(0, after_boundary_pressure - before_boundary_pressure)
        - 120 * max(0, after_overlap_area - before_overlap_area)
        - 1500 * max(0, after_overlap_count - before_overlap_count)
        - 500 * large_move_without_envelope_gain
        - 2 * movement
    )


def _layout_tweak_replay_penalty_for_patch(
    patch: LayoutTweakPatch,
    penalties: Mapping[tuple[object, ...], int],
) -> int:
    if not penalties:
        return 0
    total = 0
    for operation in tuple(patch.operations or ()):
        best = 0
        for key in _layout_tweak_operation_penalty_keys(operation):
            best = max(best, int(penalties.get(key, 0) or 0))
        total += best
    return int(total)


def _layout_tweak_operation_penalty_keys(operation: LayoutTweakOperation) -> tuple[tuple[object, ...], ...]:
    op = str(operation.op).lower()
    target = str(operation.target or operation.target_group or "")
    if not target:
        return ()
    keys: list[tuple[object, ...]] = [("target", target)]
    if op == "nudge":
        dx = int(operation.dx_tracks or 0)
        dy = int(operation.dy_tracks or 0)
        keys.append(("nudge", target, _sign(dx), _sign(dy)))
        if dx:
            keys.append(("nudge_axis", target, "x", _sign(dx)))
        if dy:
            keys.append(("nudge_axis", target, "y", _sign(dy)))
    elif op == "placement_window":
        keys.append(("placement_window", target))
    else:
        keys.append((op, target))
    return tuple(keys)


def _replay_rejection_penalty(
    reasons: Sequence[str],
    *,
    returncode: int,
    timed_out: bool,
) -> int:
    if timed_out:
        return 20_000
    if returncode != 0:
        return 16_000
    reason_set = {str(reason) for reason in reasons}
    penalty = 0
    if any(reason.startswith("physical_regressed") for reason in reason_set):
        penalty = max(penalty, 14_000)
    if "metric_regressed:route_escape" in reason_set:
        penalty = max(penalty, 10_000)
    if "metric_regressed:pin_boundary" in reason_set:
        penalty = max(penalty, 8_000)
    return penalty


def _sign(value: int) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _direction_delta(direction: str) -> tuple[str, int, int]:
    name = str(direction or "").lower()
    if name in {"left", "l", "-x"}:
        return ("left", -1, 0)
    if name in {"right", "r", "+x"}:
        return ("right", 1, 0)
    if name in {"down", "bottom", "below", "-y"}:
        return ("down", 0, -1)
    if name in {"up", "top", "above", "+y"}:
        return ("up", 0, 1)
    return (name or "none", 0, 0)


def _whitespace_metrics_for_bboxes(
    bboxes: Sequence[Sequence[int]],
    width: int,
    height: int,
    *,
    origin: tuple[int, int] = (0, 0),
) -> dict[str, object]:
    width_i = int(width)
    height_i = int(height)
    if width_i <= 0 or height_i <= 0 or width_i > 2000 or height_i > 2000:
        return {
            "whitespace_status": "not_available",
            "reason": "bbox_grid_too_large_or_invalid",
        }
    ox, oy = origin
    grid = [[False for _ in range(width_i)] for _ in range(height_i)]
    for raw in bboxes:
        bbox = _sequence4(raw)
        if bbox is None:
            continue
        x0, y0, x1, y1 = [int(round(value)) for value in bbox]
        x0 = max(0, min(width_i, x0 - ox))
        x1 = max(0, min(width_i, x1 - ox))
        y0 = max(0, min(height_i, y0 - oy))
        y1 = max(0, min(height_i, y1 - oy))
        if x1 <= x0:
            x1 = min(width_i, x0 + 1)
        if y1 <= y0:
            y1 = min(height_i, y0 + 1)
        for y in range(y0, y1):
            row = grid[y]
            for x in range(x0, x1):
                row[x] = True
    occupied = sum(1 for row in grid for item in row if item)
    total = max(1, width_i * height_i)
    largest = _largest_empty_rectangle(grid)
    return {
        "whitespace_status": "pass",
        "empty_area_tracks2": total - occupied,
        "empty_area_ratio": round((total - occupied) / total, 6),
        "largest_empty_rect_area_tracks2": int(largest["area_tracks2"]),
        "largest_empty_rect_tracks": [int(largest["width_tracks"]), int(largest["height_tracks"])],
        "largest_empty_rect_bbox_tracks": [
            int(largest["bbox_tracks"][0]) + ox,
            int(largest["bbox_tracks"][1]) + oy,
            int(largest["bbox_tracks"][2]) + ox,
            int(largest["bbox_tracks"][3]) + oy,
        ],
    }


def _boundary_pressure_metrics_for_bboxes(
    bboxes: Mapping[str, Sequence[int]],
    global_bbox: Sequence[int],
    *,
    near_edge_tracks: int = 2,
) -> dict[str, object]:
    if len(tuple(global_bbox)) != 4:
        return {}
    gx0, gy0, gx1, gy1 = [int(value) for value in tuple(global_bbox)]
    near = max(0, int(near_edge_tracks))
    margins: dict[str, int] = {}
    edge_touch = 0
    near_edge = 0
    pressure = 0
    for name, raw_bbox in bboxes.items():
        bbox = _sequence4(raw_bbox)
        if bbox is None:
            continue
        x0, y0, x1, y1 = [int(round(value)) for value in bbox]
        margin = min(x0 - gx0, y0 - gy0, gx1 - x1, gy1 - y1)
        margins[str(name)] = int(margin)
        if margin <= 0:
            edge_touch += 1
        if margin <= near:
            near_edge += 1
        pressure += max(0, near - margin)
    return {
        "group_boundary_margin_tracks": dict(sorted(margins.items())),
        "min_group_boundary_margin_tracks": min(margins.values()) if margins else None,
        "edge_touch_group_count": int(edge_touch),
        "near_edge_group_count": int(near_edge),
        "boundary_pressure_tracks": int(pressure),
        "boundary_near_edge_threshold_tracks": int(near),
    }


def _pair_overlap_metrics_for_bboxes(bboxes: Mapping[str, Sequence[int]]) -> dict[str, object]:
    names = tuple(sorted(str(name) for name in bboxes))
    rows: list[dict[str, object]] = []
    total_area = 0
    for idx, left in enumerate(names):
        left_box = _sequence4(bboxes[left])
        if left_box is None:
            continue
        for right in names[idx + 1 :]:
            right_box = _sequence4(bboxes[right])
            if right_box is None:
                continue
            x0 = max(int(round(left_box[0])), int(round(right_box[0])))
            y0 = max(int(round(left_box[1])), int(round(right_box[1])))
            x1 = min(int(round(left_box[2])), int(round(right_box[2])))
            y1 = min(int(round(left_box[3])), int(round(right_box[3])))
            area = max(0, x1 - x0) * max(0, y1 - y0)
            if area <= 0:
                continue
            total_area += area
            rows.append(
                {
                    "left": left,
                    "right": right,
                    "area_tracks2": int(area),
                    "bbox_tracks": [x0, y0, x1, y1],
                }
            )
    return {
        "pair_overlap_count": len(rows),
        "pair_overlap_area_tracks2": int(total_area),
        "pair_overlaps": tuple(rows),
    }


def _prefix_keys(values: Mapping[str, object], prefix: str) -> dict[str, object]:
    return {f"{prefix}{key}": value for key, value in values.items()}


def _patch_operation_targets(patch: LayoutTweakPatch) -> tuple[str, ...]:
    targets: list[str] = []
    for operation in patch.operations:
        op = str(operation.op).lower()
        if op in {"nudge", "placement_window"}:
            target = str(operation.target or operation.target_group)
            if target:
                targets.append(target)
    return tuple(targets)


def _operation_has_local_screen_target(operation: LayoutTweakOperation) -> bool:
    return bool(str(operation.target or operation.target_group or "").strip())


def _baseline_pair_can_preserve_spacing(
    left: Sequence[int],
    right: Sequence[int],
    spacing: int,
) -> bool:
    spacing_i = max(0, int(spacing))
    if spacing_i <= 0:
        return True
    l0, b0, l1, b1 = [int(value) for value in left]
    r0, t0, r1, t1 = [int(value) for value in right]
    return (
        l1 + spacing_i <= r0
        or r1 + spacing_i <= l0
        or b1 + spacing_i <= t0
        or t1 + spacing_i <= b0
    )


def _baseline_pair_overlaps(left: Sequence[int], right: Sequence[int]) -> bool:
    l0, b0, l1, b1 = [int(value) for value in left]
    r0, t0, r1, t1 = [int(value) for value in right]
    return not (l1 <= r0 or r1 <= l0 or b1 <= t0 or t1 <= b0)


def _largest_empty_rectangle(grid: Sequence[Sequence[bool]]) -> dict[str, object]:
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
        "width_tracks": max(0, x1 - x0),
        "height_tracks": max(0, y1 - y0),
        "area_tracks2": best_area,
        "bbox_tracks": [x0, y0, x1, y1],
    }


def _observation_group_bboxes(observation: Mapping[str, Any]) -> dict[str, tuple[int, int, int, int]]:
    groups = _mapping(_mapping(observation.get("entities")).get("groups"))
    if not groups:
        groups = _mapping(_layout_tweakability_data(observation).get("groups"))
    result: dict[str, tuple[int, int, int, int]] = {}
    for name, row_obj in groups.items():
        if str(name) == "__global__":
            continue
        row = _mapping(row_obj)
        bbox = _sequence4(row.get("bbox_tracks"))
        if bbox is None:
            continue
        result[str(name)] = tuple(int(round(value)) for value in bbox)  # type: ignore[assignment]
    return result


def _global_bbox_tracks(
    observation: Mapping[str, Any],
    bboxes: Mapping[str, tuple[int, int, int, int]],
) -> tuple[int, int, int, int] | None:
    summary = _mapping(observation.get("summary"))
    bbox_tracks = summary.get("bbox_tracks")
    if isinstance(bbox_tracks, Sequence) and not isinstance(bbox_tracks, (str, bytes)) and len(bbox_tracks) == 2:
        try:
            return (0, 0, int(bbox_tracks[0]), int(bbox_tracks[1]))
        except (TypeError, ValueError):
            pass
    if not bboxes:
        return None
    return (
        min(bbox[0] for bbox in bboxes.values()),
        min(bbox[1] for bbox in bboxes.values()),
        max(bbox[2] for bbox in bboxes.values()),
        max(bbox[3] for bbox in bboxes.values()),
    )


def _layout_tweakability_data(observation: Mapping[str, Any]) -> Mapping[str, Any]:
    for row_obj in tuple(observation.get("observations", ()) or ()):
        row = _mapping(row_obj)
        if row.get("kind") == "layout_tweakability_facts":
            return _mapping(row.get("data"))
    return {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence4(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return None
    try:
        return tuple(float(item) for item in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def _safe_symbol_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(value)) or "layout_tweak"


def _model_int(model: object, expr: object) -> int:
    value = model.eval(expr, model_completion=True)
    try:
        return int(value.as_long())
    except AttributeError:
        return int(str(value))


def _z3_abs(expr: object) -> object:
    return z3.If(expr >= 0, expr, -expr)


def _z3_pos(expr: object) -> object:
    return z3.If(expr >= 0, expr, 0)
