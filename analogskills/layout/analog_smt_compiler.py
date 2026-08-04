"""Compile the analog layout DSL into a compact SMT placement problem."""
from __future__ import annotations

import os
from dataclasses import dataclass
from itertools import product
from math import ceil, gcd, sqrt
from typing import Mapping, Sequence

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None

from analogskills.contracts import TerminalRef, TopologyGraph
from analogskills.env import get_env
from analogskills.layout.analog_layout_dsl import (
    AnalogLayoutSpec,
    CriticalNetSpec,
    DevicePatternSpec,
    LayoutObjectiveTermSpec,
    PackConstraintSpec,
    PairConstraintSpec,
    PatternCandidateSpec,
    PatternRelationSpec,
    PlacementWindowSpec,
    PCellRealizationCandidateSpec,
    PCellRealizationGroupSpec,
    RouteResourceSpec,
)
from analogskills.layout.placement import Placement


DeviceSizeMap = Mapping[str, tuple[float, float]]


@dataclass(frozen=True)
class CompiledAnalogLayout:
    block: str
    placements: tuple[Placement, ...]
    pattern_bboxes_tracks: Mapping[str, tuple[int, int, int, int]]
    selected_candidates: Mapping[str, str]
    total_width_tracks: int
    total_height_tracks: int
    track_pitch_um: float
    checks: Mapping[str, object]

    @property
    def passed(self) -> bool:
        return bool(self.checks.get("passed", False))


@dataclass(frozen=True)
class _PatternInstance:
    spec: DevicePatternSpec
    candidate: PatternCandidateSpec
    order: tuple[str, ...]
    width_tracks: int
    height_tracks: int
    offsets_tracks: Mapping[str, tuple[int, int]]
    sizes_tracks: Mapping[str, tuple[int, int]]
    realizations: Mapping[str, PCellRealizationCandidateSpec]


@dataclass(frozen=True)
class _PatternChoice:
    candidate: PatternCandidateSpec
    realizations: Mapping[str, PCellRealizationCandidateSpec]


def compile_analog_layout_smt(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
    *,
    device_sizes_um: DeviceSizeMap,
    track_pitch_um: float = 0.5,
    placement_spacing_um: float | None = None,
    max_candidate_count: int = 64,
    solver_timeout_ms: int | None = 15_000,
) -> CompiledAnalogLayout:
    """Solve a compact pattern-level SMT placement from a DSL spec."""

    if z3 is None:  # pragma: no cover
        raise RuntimeError("z3-solver is required for analog layout DSL compilation")
    pitch = max(float(track_pitch_um), 1e-6)
    complete_spec, auto_singletons = _complete_spec(spec, graph)
    spacing_um = _resolved_spacing_um(complete_spec, placement_spacing_um, pitch)
    spacing_tracks = _um_to_tracks(spacing_um, pitch)
    candidate_rows = [
        _pattern_choices(
            pattern,
            complete_spec.pcell_realization_groups,
            pairs=complete_spec.pairs,
            drc=complete_spec.drc,
            max_choices=max_candidate_count,
        )
        for pattern in complete_spec.patterns
    ]
    pattern_choice_count_by_pattern = _pattern_choice_count_by_pattern(complete_spec.patterns, candidate_rows)
    pattern_candidate_combination_upper_bound = _candidate_combination_upper_bound(candidate_rows)
    use_pattern_choice_smt = _should_use_pattern_choice_smt(candidate_rows, complete_spec.pcell_realization_groups)
    relation_choice_upper_bound = _relation_choice_upper_bound(complete_spec.relations)
    inline_relation_choices = _should_inline_relation_choices(
        complete_spec.relations,
        relation_choice_upper_bound,
        max_candidate_count=max_candidate_count,
    )
    relation_variants = (
        ((complete_spec, {}, 0),)
        if inline_relation_choices
        else _relation_choice_variants(complete_spec, max_choice_count=max_candidate_count)
    )
    if not candidate_rows:
        return CompiledAnalogLayout(
            complete_spec.block,
            (),
            {},
            {},
            0,
            0,
            pitch,
            {"passed": False, "issues": ("no patterns in layout spec",)},
        )

    best: _CandidateSolve | None = None
    candidate_count = 0
    relation_choice_count = 0
    if use_pattern_choice_smt:
        pattern_choice_smt_probe_limit = min(max(8, min(16, max(1, int(max_candidate_count)))), len(relation_variants))
        for relation_spec, selected_relation_choices, relation_choice_cost in relation_variants:
            relation_choice_count += 1
            candidate_count += 1
            solved = _solve_pattern_choice_candidate(
                relation_spec,
                graph,
                candidate_rows,
                device_sizes_um,
                pitch,
                spacing_tracks=spacing_tracks,
                solver_timeout_ms=solver_timeout_ms,
            )
            if solved is None:
                if best is None and candidate_count >= pattern_choice_smt_probe_limit:
                    break
                continue
            solved = _apply_guard_ring_envelope(complete_spec, solved, pitch)
            if selected_relation_choices:
                solved = _with_relation_choice_checks(
                    solved,
                    selected_relation_choices,
                    relation_choice_cost,
                )
            if best is None or _candidate_solve_order_key(relation_spec, solved) < _candidate_solve_order_key(relation_spec, best):
                best = solved
    if not use_pattern_choice_smt or best is None:
        fallback_reason = "pattern_choice_smt_no_solution" if use_pattern_choice_smt else ""
        pattern_choice_smt_attempt_count = candidate_count if fallback_reason else 0
        if fallback_reason:
            candidate_count = 0
            relation_choice_count = 0
        for relation_spec, selected_relation_choices, relation_choice_cost in relation_variants:
            if not use_pattern_choice_smt:
                relation_choice_count += 1
            elif fallback_reason:
                relation_choice_count += 1
            for combo in product(*candidate_rows):
                candidate_count += 1
                if candidate_count > max_candidate_count:
                    break
                instances: list[_PatternInstance] = []
                invalid: list[str] = []
                for pattern, choice in zip(relation_spec.patterns, combo):
                    try:
                        instances.append(
                            _pattern_instance(
                                pattern,
                                choice.candidate,
                                device_sizes_um,
                                pitch,
                                realizations=choice.realizations,
                            )
                        )
                    except ValueError as exc:
                        invalid.append(str(exc))
                if invalid:
                    continue
                solved = _solve_candidate(
                    relation_spec,
                    graph,
                    tuple(instances),
                    pitch,
                    spacing_tracks=spacing_tracks,
                    solver_timeout_ms=solver_timeout_ms,
                )
                if solved is None:
                    continue
                solved = _apply_guard_ring_envelope(complete_spec, solved, pitch)
                candidate_choice_cost = sum(max(0, int(getattr(choice.candidate, "cost", 0) or 0)) for choice in combo)
                if candidate_choice_cost:
                    solved = _with_candidate_choice_cost_checks(solved, candidate_choice_cost)
                if fallback_reason:
                    solved = _with_checks(
                        solved,
                        pattern_choice_mode="python_outer_product_fallback",
                        pattern_choice_fallback_reason=fallback_reason,
                        pattern_choice_smt_attempt_count=pattern_choice_smt_attempt_count,
                    )
                if selected_relation_choices:
                    solved = _with_relation_choice_checks(
                        solved,
                        selected_relation_choices,
                        relation_choice_cost,
                    )
                if best is None or _candidate_solve_order_key(relation_spec, solved) < _candidate_solve_order_key(relation_spec, best):
                    best = solved
            if candidate_count > max_candidate_count:
                break

    if best is None:
        return CompiledAnalogLayout(
            complete_spec.block,
            (),
            {},
            {},
            0,
            0,
            pitch,
            {
                "passed": False,
                "issues": ("compact DSL SMT problem is unsat",),
                "candidate_count": candidate_count,
                "relation_choice_count": relation_choice_count,
                "auto_singleton_devices": tuple(auto_singletons),
                "pattern_choice_mode": "z3_choice_variables" if use_pattern_choice_smt else "python_outer_product",
                "relation_choice_mode": "z3_choice_variables" if inline_relation_choices else "python_outer_product",
                "pattern_choice_count_by_pattern": pattern_choice_count_by_pattern,
                "pattern_candidate_combination_upper_bound": pattern_candidate_combination_upper_bound,
                "relation_choice_upper_bound": relation_choice_upper_bound,
            },
        )

    overlap_issue_count = int(best.checks.get("overlap_issue_count", 0) or 0)
    smt_verified = bool(best.checks.get("smt_verified", True))
    dummy_contract = _matched_mos_dummy_contract_report(complete_spec, graph, best)
    checks = {
        **best.checks,
        "passed": overlap_issue_count == 0 and smt_verified and bool(dummy_contract["passed"]),
        "block": complete_spec.block,
        "candidate_count": candidate_count,
        "relation_choice_count": relation_choice_count,
        "pattern_choice_mode": best.checks.get(
            "pattern_choice_mode",
            "z3_choice_variables" if use_pattern_choice_smt else "python_outer_product",
        ),
        "relation_choice_mode": "z3_choice_variables" if inline_relation_choices else "python_outer_product",
        "pattern_choice_count_by_pattern": pattern_choice_count_by_pattern,
        "pattern_candidate_combination_upper_bound": pattern_candidate_combination_upper_bound,
        "relation_choice_upper_bound": relation_choice_upper_bound,
        "selected_candidates": dict(best.selected_candidates),
        "auto_singleton_devices": tuple(auto_singletons),
        "noncritical_router": complete_spec.noncritical_router,
        "dsl_pattern_count": len(complete_spec.patterns),
        "dsl_relation_count": len(complete_spec.relations),
        "dsl_critical_net_count": len(complete_spec.critical_nets),
        "dsl_route_resource_count": len(complete_spec.route_resources),
        "dsl_pack_count": len(complete_spec.pack_constraints),
        "dsl_placement_window_count": len(complete_spec.placement_windows),
        "dsl_objective_term_count": len(complete_spec.objective_terms),
        "dsl_pcell_realization_group_count": len(complete_spec.pcell_realization_groups),
        "guard_ring_policy": _guard_ring_policy_report(complete_spec, pitch),
        "matched_mos_dummy_contract": dummy_contract,
        "pattern_bboxes_tracks": {
            str(name): tuple(int(v) for v in bbox)
            for name, bbox in sorted(best.pattern_bboxes_tracks.items())
        },
        "dsl_pairs": tuple(_pair_report_row(item) for item in complete_spec.pairs),
        "mos_shared_sd_pairs": tuple(
            _shared_sd_pair_report_row(
                item,
                graph,
                complete_spec,
                {**dict(best.checks), "selected_candidates": dict(best.selected_candidates)},
                pitch,
            )
            for item in complete_spec.pairs
            if bool(getattr(item, "shared_sd", False))
        ),
        "dsl_relations": tuple(_relation_report_row(item) for item in complete_spec.relations),
        "dsl_packs": tuple(_pack_report_row(item) for item in complete_spec.pack_constraints),
        "dsl_placement_windows": tuple(
            _placement_window_report_row(item)
            for item in complete_spec.placement_windows
        ),
        "dsl_objective_terms": tuple(
            _objective_term_report_row(item)
            for item in complete_spec.objective_terms
        ),
        "dsl_pcell_realization_groups": tuple(
            _pcell_realization_group_report_row(item)
            for item in complete_spec.pcell_realization_groups
        ),
        "dsl_critical_nets": tuple(_critical_net_report_row(item) for item in complete_spec.critical_nets),
        "dsl_route_resources": tuple(_route_resource_report_row(item) for item in complete_spec.route_resources),
        "dsl_objective": {
            "bbox_weight": int(complete_spec.objective.bbox_weight),
            "width_weight": int(complete_spec.objective.width_weight),
            "height_weight": int(complete_spec.objective.height_weight),
            "area_weight": int(complete_spec.objective.area_weight),
            "true_area_weight": int(getattr(complete_spec.objective, "true_area_weight", 0) or 0),
            "max_side_weight": int(complete_spec.objective.max_side_weight),
            "hpwl_weight": int(complete_spec.objective.hpwl_weight),
            "right_whitespace_weight": int(complete_spec.objective.right_whitespace_weight),
            "aspect_weight": int(complete_spec.objective.aspect_weight),
            "aspect_num": int(complete_spec.objective.aspect_num),
            "aspect_den": int(complete_spec.objective.aspect_den),
            "objective_term_weight": int(complete_spec.objective.objective_term_weight),
            "realization_weight": int(complete_spec.objective.realization_weight),
        },
        "solver_timeout_ms": _positive_int_or_default(solver_timeout_ms, 15_000),
    }
    return CompiledAnalogLayout(
        complete_spec.block,
        best.placements,
        best.pattern_bboxes_tracks,
        best.selected_candidates,
        best.total_width_tracks,
        best.total_height_tracks,
        pitch,
        checks,
    )


@dataclass(frozen=True)
class _CandidateSolve:
    score: tuple[int, ...]
    placements: tuple[Placement, ...]
    pattern_bboxes_tracks: Mapping[str, tuple[int, int, int, int]]
    selected_candidates: Mapping[str, str]
    total_width_tracks: int
    total_height_tracks: int
    checks: Mapping[str, object]


def _guard_ring_halo_tracks(spec: AnalogLayoutSpec, pitch: float) -> tuple[int, int, int, int]:
    drc = spec.drc
    if not bool(getattr(drc, "guard_ring_enabled", False)):
        return (0, 0, 0, 0)
    width = max(0.0, float(getattr(drc, "guard_ring_width_um", 0.0) or 0.0))
    spacing = max(0.0, float(getattr(drc, "guard_ring_spacing_um", 0.0) or 0.0))
    extras = dict(getattr(drc, "guard_ring_extra_spacing_um_by_side", {}) or {})
    return tuple(
        _um_to_tracks(width + spacing + max(0.0, float(extras.get(side, 0.0) or 0.0)), pitch)
        for side in ("left", "bottom", "right", "top")
    )  # type: ignore[return-value]


def _guard_ring_policy_report(spec: AnalogLayoutSpec, pitch: float) -> dict[str, object]:
    left, bottom, right, top = _guard_ring_halo_tracks(spec, pitch)
    return {
        "enabled": bool(getattr(spec.drc, "guard_ring_enabled", False)),
        "net": str(getattr(spec.drc, "guard_ring_net", "VSS") or "VSS"),
        "kind": str(getattr(spec.drc, "guard_ring_kind", "substrate") or "substrate"),
        "width_um": float(getattr(spec.drc, "guard_ring_width_um", 0.0) or 0.0),
        "spacing_um": float(getattr(spec.drc, "guard_ring_spacing_um", 0.0) or 0.0),
        "contact_pitch_um": float(getattr(spec.drc, "guard_ring_contact_pitch_um", 1.0) or 1.0),
        "extra_spacing_um_by_side": dict(getattr(spec.drc, "guard_ring_extra_spacing_um_by_side", {}) or {}),
        "halo_tracks_by_side": {"left": left, "bottom": bottom, "right": right, "top": top},
        "objective_scope": "core_plus_guard_ring_envelope",
    }


def _compact_bbox_objective_component(spec: AnalogLayoutSpec, width: int, height: int) -> int:
    width = max(0, int(width))
    height = max(0, int(height))
    return int(
        max(0, spec.objective.bbox_weight) * (width + height)
        + max(0, spec.objective.width_weight) * width
        + max(0, spec.objective.height_weight) * height
        + max(0, spec.objective.area_weight) * (width + height + abs(width - height))
        + _objective_true_area_weight(spec) * width * height
        + max(0, spec.objective.max_side_weight) * max(width, height)
        + max(0, spec.objective.aspect_weight)
        * abs(width * max(1, spec.objective.aspect_den) - height * max(1, spec.objective.aspect_num))
    )


def _apply_guard_ring_envelope(
    spec: AnalogLayoutSpec,
    candidate: _CandidateSolve,
    pitch: float,
) -> _CandidateSolve:
    """Shift a core solve into its PDK guard envelope and re-score it.

    This is applied to every candidate before candidate/refinement selection,
    including heuristic fallbacks, so no backend can silently omit the ring.
    """

    if bool(candidate.checks.get("guard_ring_envelope_applied", False)):
        return candidate
    left, bottom, right, top = _guard_ring_halo_tracks(spec, pitch)
    if not any((left, bottom, right, top)):
        return _with_checks(candidate, guard_ring_envelope_applied=False)
    core_w = int(candidate.total_width_tracks)
    core_h = int(candidate.total_height_tracks)
    total_w = core_w + left + right
    total_h = core_h + bottom + top
    placements = tuple(
        Placement(
            item.name,
            float(item.x_um) + left * pitch,
            float(item.y_um) + bottom * pitch,
            item.orient,
            item.role,
        )
        for item in candidate.placements
    )
    pattern_bboxes = {
        str(name): (int(box[0]) + left, int(box[1]) + bottom, int(box[2]) + left, int(box[3]) + bottom)
        for name, box in candidate.pattern_bboxes_tracks.items()
    }
    device_bboxes = {
        str(name): (int(box[0]) + left, int(box[1]) + bottom, int(box[2]) + left, int(box[3]) + bottom)
        for name, box in dict(candidate.checks.get("device_bboxes_tracks", {}) or {}).items()
    }
    old_objective = int(candidate.checks.get("objective_score", 0) or 0)
    if bool(candidate.checks.get("guard_ring_objective_in_z3", False)):
        objective = old_objective
    else:
        objective = old_objective - _compact_bbox_objective_component(spec, core_w, core_h)
        objective += _compact_bbox_objective_component(spec, total_w, total_h)
    score = _candidate_selection_score(total_w, total_h, objective)
    checks = {
        **dict(candidate.checks),
        "core_total_width_tracks": core_w,
        "core_total_height_tracks": core_h,
        "total_width_tracks": total_w,
        "total_height_tracks": total_h,
        "estimated_area_tracks": total_w * total_h,
        "true_area_objective": _objective_true_area_weight(spec) * total_w * total_h,
        "max_side_tracks": max(total_w, total_h),
        "objective_score": objective,
        "selection_score": score,
        "device_bboxes_tracks": device_bboxes,
        "guard_ring_envelope_applied": True,
        "guard_ring_core_bbox_tracks": (left, bottom, left + core_w, bottom + core_h),
        "guard_ring_outer_bbox_tracks": (0, 0, total_w, total_h),
        "guard_ring_halo_tracks_by_side": {"left": left, "bottom": bottom, "right": right, "top": top},
    }
    return _CandidateSolve(
        score,
        placements,
        pattern_bboxes,
        candidate.selected_candidates,
        total_w,
        total_h,
        checks,
    )


def _matched_mos_dummy_contract_report(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
    candidate: _CandidateSolve,
) -> dict[str, object]:
    policy = str(getattr(spec.drc, "matched_mos_dummy_policy", "none") or "none")
    required = tuple(str(item) for item in tuple(getattr(spec.drc, "matched_mos_dummy_required_params", ()) or ()))
    if policy == "none" or not required:
        return {"passed": True, "policy": policy, "required_params": required, "devices": {}}
    matched_devices = {
        str(device)
        for pair in spec.pairs
        for device in (pair.left, pair.right)
        if device in graph.devices and "mos" in str(graph.devices[device].model).lower()
    }
    selected = dict(candidate.checks.get("selected_pcell_realizations", {}) or {})
    if not tuple(spec.pcell_realization_groups) or not selected:
        return {
            "passed": True,
            "policy": policy,
            "required_params": required,
            "devices": {},
            "status": "deferred_until_pcell_realization_selection",
            "lvs_semantics": "native_pcell_dummy_geometry_only_no_extra_extracted_mos",
        }
    rows: dict[str, object] = {}
    missing_devices: list[str] = []
    for device in sorted(matched_devices):
        realization = dict(selected.get(device, {}) or {})
        params = dict(realization.get("pcell_overrides", {}) or {})
        missing = tuple(key for key in required if key not in params)
        if missing:
            missing_devices.append(device)
        rows[device] = {
            "realization": str(realization.get("name", "") or ""),
            "bbox_includes_native_dummy": bool(realization) and not missing,
            "missing_params": missing,
        }
    return {
        "passed": not missing_devices,
        "policy": policy,
        "required_params": required,
        "devices": rows,
        "missing_devices": tuple(missing_devices),
        "lvs_semantics": "native_pcell_dummy_geometry_only_no_extra_extracted_mos",
    }


def _relation_report_row(item: PatternRelationSpec) -> dict[str, object]:
    return {
        "source": item.source,
        "target": item.target,
        "kind": item.kind,
        "hard": bool(item.hard),
        "weight": int(item.weight),
        "min_gap_um": float(item.min_gap_um),
        "tolerance_um": float(item.tolerance_um),
        "candidates": tuple(item.candidates),
        "candidate_costs": dict(item.candidate_costs),
    }


def _pair_report_row(item: PairConstraintSpec) -> dict[str, object]:
    return {
        "name": item.name,
        "left": item.left,
        "right": item.right,
        "role": item.role,
        "spacing_um": item.spacing_um,
        "mirror_right": bool(item.mirror_right),
        "same_y": bool(item.same_y),
        "notes": item.notes,
        "shared_sd": bool(getattr(item, "shared_sd", False)),
        "shared_sd_net": str(getattr(item, "shared_sd_net", "") or ""),
        "shared_sd_role": str(getattr(item, "shared_sd_role", "") or ""),
        "shared_sd_spacing_um": getattr(item, "shared_sd_spacing_um", None),
        "shared_sd_weight": int(getattr(item, "shared_sd_weight", 0) or 0),
        "shared_sd_readiness": dict(getattr(item, "shared_sd_readiness", {}) or {}),
    }


def _shared_sd_pair_report_row(
    item: PairConstraintSpec,
    graph: TopologyGraph,
    spec: AnalogLayoutSpec,
    checks: Mapping[str, object],
    pitch: float,
) -> dict[str, object]:
    connection = _shared_sd_connection_for_pair(item, graph)
    device_to_pattern = spec.device_to_pattern()
    device_bboxes = dict(checks.get("device_bboxes_tracks", {}) or {})
    gap_tracks: int | None = None
    center_manhattan_tracks: int | None = None
    if item.left in device_bboxes and item.right in device_bboxes:
        left_bbox = tuple(int(v) for v in device_bboxes[item.left])
        right_bbox = tuple(int(v) for v in device_bboxes[item.right])
        center_manhattan_tracks = abs((left_bbox[0] + left_bbox[2]) - (right_bbox[0] + right_bbox[2])) + abs(
            (left_bbox[1] + left_bbox[3]) - (right_bbox[1] + right_bbox[3])
        )
        if left_bbox[2] <= right_bbox[0]:
            gap_tracks = right_bbox[0] - left_bbox[2]
        elif right_bbox[2] <= left_bbox[0]:
            gap_tracks = left_bbox[0] - right_bbox[2]
        elif left_bbox[3] <= right_bbox[1]:
            gap_tracks = right_bbox[1] - left_bbox[3]
        elif right_bbox[3] <= left_bbox[1]:
            gap_tracks = left_bbox[1] - right_bbox[3]
        else:
            gap_tracks = 0
    selected_candidates = dict(checks.get("selected_candidates", {}) or {})
    pattern = device_to_pattern.get(item.left, "")
    selected_candidate = str(selected_candidates.get(pattern, "") or "")
    readiness = _shared_sd_readiness_contract(item)
    target_spacing_um = _shared_sd_spacing_um(item, spec.drc)
    actual_gap_um = None if gap_tracks is None else round(float(gap_tracks) * pitch, 6)
    selected_shared_candidate = _shared_sd_candidate_name_is_shared(selected_candidate)
    spacing_matches_intent = actual_gap_um is not None and actual_gap_um <= target_spacing_um + max(float(pitch) * 1e-6, 1e-9)
    physical_authorized = connection is not None and _shared_sd_contract_allows_physical_merge(readiness)
    physical_candidate_selected = _shared_sd_candidate_name_is_physical(selected_candidate)
    physical_selected = bool(physical_authorized and physical_candidate_selected and spacing_matches_intent)
    emitter_bound = _bool_like(readiness.get("emitter_bound", False))
    if physical_selected:
        limitation = (
            ""
            if emitter_bound
            else "Physical shared-S/D is solver-authorized, but final GDS emitter/template binding has not been confirmed by this compiler report."
        )
    else:
        limitation = (
            "Current analog flow keeps native PCell instances independent; "
            "true diffusion abutment needs a calibrated abutted MOS PCell/master."
        )
    return {
        "name": item.name,
        "left": item.left,
        "right": item.right,
        "role": item.role,
        "left_pattern": pattern,
        "right_pattern": device_to_pattern.get(item.right, ""),
        "selected_pattern_candidate": selected_candidate,
        "shareable": connection is not None,
        "net": "" if connection is None else connection[0],
        "left_terminal": "" if connection is None else connection[1],
        "right_terminal": "" if connection is None else connection[2],
        "requested_spacing_um": target_spacing_um,
        "actual_gap_tracks": gap_tracks,
        "actual_gap_um": actual_gap_um,
        "center_manhattan_tracks2": center_manhattan_tracks,
        "objective_term": dict(checks.get("shared_sd_terms", {}) or {}).get(item.name, 0),
        "selected_shared_sd_candidate": selected_shared_candidate,
        "selected_physical_shared_sd_candidate": physical_candidate_selected,
        "shared_sd_readiness_status": str(readiness.get("status", "") or ""),
        "shared_sd_solver_allowed_mode": str(readiness.get("solver_allowed_mode", "") or ""),
        "shared_sd_readiness_candidate": str(readiness.get("candidate", "") or ""),
        "shared_sd_readiness_source": str(readiness.get("source", readiness.get("readiness_source", "")) or ""),
        "shared_sd_readiness_artifact": str(readiness.get("readiness_artifact", readiness.get("readiness_json", "")) or ""),
        "shared_sd_gds_artifact": str(readiness.get("gds_artifact", "") or ""),
        "shared_sd_template_lib": str(readiness.get("template_lib", "") or ""),
        "shared_sd_template_cell": str(readiness.get("template_cell", "") or ""),
        "shared_sd_template_view": str(readiness.get("template_view", "layout") or "layout"),
        "shared_sd_terminal_access": dict(readiness.get("terminal_access", readiness.get("shared_sd_terminal_access", {})) or {}),
        "shared_sd_layout_bbox_um": tuple(readiness.get("layout_bbox_um", readiness.get("bbox_um", ())) or ()),
        "shared_sd_layout_width_um": readiness.get("layout_width_um", readiness.get("width_um", None)),
        "shared_sd_layout_height_um": readiness.get("layout_height_um", readiness.get("height_um", None)),
        "shared_sd_template_instance_params": dict(
            readiness.get(
                "compatible_instance_params",
                readiness.get("template_instance_params", readiness.get("instance_params", {})),
            )
            or {}
        ),
        "lvs_required": _bool_like(readiness.get("lvs_required", False)),
        "lvs_correct": readiness.get("lvs_correct", None),
        "physical_diffusion_merge_authorized": physical_authorized,
        "physical_diffusion_merge_selected_by_smt": physical_selected,
        "physical_diffusion_merge_emitted": bool(physical_selected and emitter_bound),
        "native_pcell_limitation": limitation,
    }


def _pack_report_row(item: PackConstraintSpec) -> dict[str, object]:
    return {
        "name": item.name,
        "patterns": tuple(item.patterns),
        "max_width_um": item.max_width_um,
        "max_height_um": item.max_height_um,
        "weight": int(item.weight),
        "width_weight": int(item.width_weight),
        "height_weight": int(item.height_weight),
        "area_weight": int(item.area_weight),
    }


def _placement_window_report_row(item: PlacementWindowSpec) -> dict[str, object]:
    return {
        "name": item.name,
        "pattern": item.pattern,
        "min_x_tracks": item.min_x_tracks,
        "max_x_tracks": item.max_x_tracks,
        "min_y_tracks": item.min_y_tracks,
        "max_y_tracks": item.max_y_tracks,
        "target_x_tracks": item.target_x_tracks,
        "target_y_tracks": item.target_y_tracks,
        "weight": int(item.weight),
        "hard": bool(item.hard),
        "notes": item.notes,
    }


def _objective_term_report_row(item: LayoutObjectiveTermSpec) -> dict[str, object]:
    return {
        "name": item.name,
        "kind": item.kind,
        "patterns": tuple(item.patterns),
        "devices": tuple(item.devices),
        "weight": int(item.weight),
        "axis": item.axis,
        "metric": item.metric,
        "target": item.target,
        "notes": item.notes,
    }


def _pcell_realization_group_report_row(item: PCellRealizationGroupSpec) -> dict[str, object]:
    return {
        "name": item.name,
        "devices": tuple(item.devices),
        "require_same": bool(item.require_same),
        "candidate_count": len(item.candidates),
        "candidates": tuple(_pcell_realization_candidate_report_row(candidate) for candidate in item.candidates),
        "notes": item.notes,
    }


def _pcell_realization_candidate_report_row(item: PCellRealizationCandidateSpec) -> dict[str, object]:
    return {
        "name": item.name,
        "width_um": float(item.width_um),
        "height_um": float(item.height_um),
        "sizing_overrides": dict(item.sizing_overrides),
        "pcell_overrides": dict(item.pcell_overrides),
        "cost": int(item.cost),
        "drc_clean": bool(item.drc_clean),
        "lvs_clean": bool(item.lvs_clean),
        "notes": item.notes,
        "metadata": dict(item.metadata),
    }


def _critical_net_report_row(item: CriticalNetSpec) -> dict[str, object]:
    return {
        "name": item.name,
        "weight": int(item.weight),
        "route_in_smt": bool(item.route_in_smt),
        "shield": bool(item.shield),
        "width_um": item.width_um,
    }


def _route_resource_report_row(item: RouteResourceSpec) -> dict[str, object]:
    return {
        "name": item.name,
        "match": item.match,
        "layer": item.layer,
        "allowed_layers": tuple(item.allowed_layers),
        "forbidden_layers": tuple(item.forbidden_layers),
        "cyclic_layers": tuple(item.cyclic_layers),
        "lane": item.lane,
        "cyclic_lanes": tuple(item.cyclic_lanes),
        "avoid_nets": tuple(item.avoid_nets),
        "avoid_prefixes": tuple(item.avoid_prefixes),
        "style": item.style,
        "channel_orientation": item.channel_orientation,
        "channel_side": item.channel_side,
        "channel_offset_um": item.channel_offset_um,
        "dogleg_side": item.dogleg_side,
        "dogleg_offset_um": item.dogleg_offset_um,
        "dogleg_offset_step_um": item.dogleg_offset_step_um,
        "terminal_escape_style": item.terminal_escape_style,
        "terminal_escape_um": item.terminal_escape_um,
        "route_policy": dict(item.route_policy),
        "notes": item.notes,
    }


def _with_checks(candidate: _CandidateSolve, **updates: object) -> _CandidateSolve:
    return _CandidateSolve(
        candidate.score,
        candidate.placements,
        candidate.pattern_bboxes_tracks,
        candidate.selected_candidates,
        candidate.total_width_tracks,
        candidate.total_height_tracks,
        {**dict(candidate.checks), **updates},
    )


def _with_relation_choice_checks(
    candidate: _CandidateSolve,
    selected_relations: Mapping[str, str],
    relation_choice_cost: int,
) -> _CandidateSolve:
    objective_score = int(candidate.checks.get("objective_score", 0) or 0) + int(relation_choice_cost)
    checks = {
        **dict(candidate.checks),
        "selected_relations": {
            **dict(candidate.checks.get("selected_relations", {}) or {}),
            **dict(selected_relations),
        },
        "relation_choice_cost": int(relation_choice_cost),
        "objective_score": objective_score,
    }
    score = _with_extra_objective_score(candidate.score, relation_choice_cost)
    return _CandidateSolve(
        score,
        candidate.placements,
        candidate.pattern_bboxes_tracks,
        candidate.selected_candidates,
        candidate.total_width_tracks,
        candidate.total_height_tracks,
        checks,
    )


def _with_candidate_choice_cost_checks(candidate: _CandidateSolve, candidate_choice_cost: int) -> _CandidateSolve:
    objective_score = int(candidate.checks.get("objective_score", 0) or 0) + int(candidate_choice_cost)
    checks = {
        **dict(candidate.checks),
        "candidate_choice_cost": int(candidate_choice_cost),
        "objective_score": objective_score,
    }
    score = _with_extra_objective_score(candidate.score, candidate_choice_cost)
    return _CandidateSolve(
        score,
        candidate.placements,
        candidate.pattern_bboxes_tracks,
        candidate.selected_candidates,
        candidate.total_width_tracks,
        candidate.total_height_tracks,
        checks,
    )


def _candidate_selection_score(width_tracks: int, height_tracks: int, objective_score: int) -> tuple[int, int, int, int, int]:
    width = max(0, int(width_tracks))
    height = max(0, int(height_tracks))
    area = width * height if width > 0 and height > 0 else 10**12
    return (area, max(width, height) if area < 10**12 else 10**9, int(objective_score), width, height)


def _objective_true_area_weight(spec: AnalogLayoutSpec) -> int:
    """Strict bbox area weight.

    ``ObjectiveSpec.area_weight`` is the legacy linear compactness proxy.  This
    explicit opt-in adds the nonlinear ``W*H`` term and enables bounded-area
    compaction for larger placement problems.
    """

    try:
        return max(0, int(getattr(spec.objective, "true_area_weight", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _analog_smt_random_seed() -> int:
    try:
        return int(get_env("ANALOG_SMT_RANDOM_SEED", "0") or "0")
    except ValueError:
        return 0


def _configure_z3_solver_options(solver: object, timeout_ms: int | None = None) -> None:
    seed = _analog_smt_random_seed()
    if timeout_ms is not None:
        try:
            solver.set(timeout=max(1, int(timeout_ms)))  # type: ignore[attr-defined]
        except Exception:
            pass
    # ``Solver`` accepts ``random_seed`` across supported Z3 releases.  Some
    # bindings appear to accept the generic ``seed`` option here but defer the
    # unknown-parameter error until the first assertion is added, so probing
    # that alias is unsafe.
    try:
        solver.set("random_seed", seed)  # type: ignore[attr-defined]
    except Exception:
        pass


def _candidate_solve_order_key(spec: AnalogLayoutSpec, candidate: _CandidateSolve) -> tuple[int, ...]:
    if _objective_true_area_weight(spec) > 0:
        width = max(0, int(candidate.total_width_tracks))
        height = max(0, int(candidate.total_height_tracks))
        area = width * height if width > 0 and height > 0 else 10**12
        objective = int(candidate.checks.get("objective_score", area) or area)
        effective_area = _effective_area_score_for_wh(spec, width, height)
        return (effective_area, area, max(width, height) if area < 10**12 else 10**9, objective, width, height)
    return tuple(int(item) for item in candidate.score)


def _effective_area_score_for_wh(spec: AnalogLayoutSpec, width_tracks: int, height_tracks: int) -> int:
    width = max(0, int(width_tracks))
    height = max(0, int(height_tracks))
    area = width * height if width > 0 and height > 0 else 10**12
    aspect_num = max(1, int(getattr(spec.objective, "aspect_num", 1) or 1))
    aspect_den = max(1, int(getattr(spec.objective, "aspect_den", 1) or 1))
    aspect_penalty = 2 * max(1, int(getattr(spec.objective, "aspect_weight", 1) or 1)) * abs(
        width * aspect_den - height * aspect_num
    )
    return int(area + aspect_penalty)


def _with_extra_objective_score(score: Sequence[int], extra_cost: int) -> tuple[int, ...]:
    values = tuple(int(item) for item in score)
    if len(values) < 3:
        return (int(extra_cost),) + values
    return values[:2] + (values[2] + int(extra_cost),) + values[3:]


def _solve_pattern_choice_candidate(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
    candidate_rows: Sequence[Sequence[_PatternChoice]],
    device_sizes_um: DeviceSizeMap,
    pitch: float,
    *,
    spacing_tracks: int,
    solver_timeout_ms: int | None,
) -> _CandidateSolve | None:
    """Solve pattern placement and PCell realization selection in one Z3 problem.

    The older path forms a Python outer product of pattern/realization choices and
    solves a separate placement problem per tuple.  That is simple but prevents the
    optimizer from seeing the shape tradeoff jointly with packing, relation and
    HPWL objectives.  This path keeps one Int choice variable per pattern and uses
    z3.If expressions for the selected width, height and device centers.
    """

    pattern_names = tuple(pattern.name for pattern in spec.patterns)
    row_instances: dict[str, tuple[_PatternInstance, ...]] = {}
    invalid: list[str] = []
    for pattern, choices in zip(spec.patterns, candidate_rows):
        instances: list[_PatternInstance] = []
        for choice in choices:
            try:
                instances.append(
                    _pattern_instance(
                        pattern,
                        choice.candidate,
                        device_sizes_um,
                        pitch,
                        realizations=choice.realizations,
                    )
                )
            except ValueError as exc:
                invalid.append(str(exc))
        if not instances:
            invalid.append(f"pattern {pattern.name} has no legal PCell realization choice")
        else:
            row_instances[pattern.name] = tuple(instances)
    if invalid or len(row_instances) != len(pattern_names):
        return None

    opt = z3.Optimize()
    effective_solver_timeout_ms = _positive_int_or_default(solver_timeout_ms, 15_000)
    _configure_z3_solver_options(opt, effective_solver_timeout_ms)

    choice_vars = {
        name: z3.Int(f"als_choice__{_safe_symbol_name(name)}")
        for name in pattern_names
    }
    for name in pattern_names:
        rows = row_instances[name]
        opt.add(z3.Or([choice_vars[name] == index for index in range(len(rows))]))

    x = {name: z3.Int(f"als_x__{name}") for name in pattern_names}
    y = {name: z3.Int(f"als_y__{name}") for name in pattern_names}
    width = {
        name: _choice_int_expr(choice_vars[name], tuple(inst.width_tracks for inst in row_instances[name]))
        for name in pattern_names
    }
    height = {
        name: _choice_int_expr(choice_vars[name], tuple(inst.height_tracks for inst in row_instances[name]))
        for name in pattern_names
    }
    max_width_bound = max(
        1,
        sum(max(inst.width_tracks for inst in row_instances[name]) for name in pattern_names)
        + spacing_tracks * (len(pattern_names) + 4),
    )
    max_height_bound = max(
        1,
        sum(max(inst.height_tracks for inst in row_instances[name]) for name in pattern_names)
        + spacing_tracks * (len(pattern_names) + 4),
    )
    for name in pattern_names:
        opt.add(x[name] >= 0, y[name] >= 0, x[name] <= max_width_bound, y[name] <= max_height_bound)

    for idx, left in enumerate(pattern_names):
        for right in pattern_names[idx + 1 :]:
            opt.add(
                z3.Or(
                    x[left] + width[left] + spacing_tracks <= x[right],
                    x[right] + width[right] + spacing_tracks <= x[left],
                    y[left] + height[left] + spacing_tracks <= y[right],
                    y[right] + height[right] + spacing_tracks <= y[left],
                )
            )

    relation_penalty_terms: list[object] = []
    selected_relation_bools: dict[str, dict[str, object]] = {}
    selected_relation_cost_terms: list[object] = []
    for relation_index, relation in enumerate(spec.relations):
        if relation.source not in x or relation.target not in x:
            continue
        gap = _um_to_tracks(relation.min_gap_um, pitch)
        tol = _um_to_tracks(relation.tolerance_um, pitch)
        s, t = relation.source, relation.target
        if relation.candidates:
            choice_by_kind: dict[str, object] = {}
            relation_bools = []
            for kind in tuple(dict.fromkeys(str(item).lower() for item in relation.candidates if str(item))):
                var = z3.Bool(f"als_rel__{relation_index}__{s}__{t}__{kind}")
                choice_by_kind[kind] = var
                relation_bools.append(var)
                constraint = _relation_constraint_expr(kind, s, t, x, y, width, height, gap, tol)
                if constraint is not None:
                    if relation.hard:
                        opt.add(z3.Implies(var, constraint))
                    else:
                        penalty = _relation_violation_expr(kind, s, t, x, y, width, height, gap, tol)
                        if penalty is not None:
                            relation_penalty_terms.append(z3.If(var, max(1, int(relation.weight)) * penalty, 0))
                selected_relation_cost_terms.append(z3.If(var, int(relation.candidate_costs.get(kind, 0)), 0))
            if relation_bools:
                opt.add(z3.PbEq([(var, 1) for var in relation_bools], 1))
                selected_relation_bools[_relation_key(relation_index, relation)] = choice_by_kind
            continue

        kind = relation.kind.lower()
        if relation.hard:
            constraint = _relation_constraint_expr(kind, s, t, x, y, width, height, gap, tol)
            if constraint is not None:
                opt.add(constraint)
        else:
            penalty = _relation_violation_expr(kind, s, t, x, y, width, height, gap, tol)
            if penalty is not None:
                relation_penalty_terms.append(max(1, int(relation.weight)) * penalty)

    total_w = z3.Int("als_total_width")
    total_h = z3.Int("als_total_height")
    opt.add(total_w >= 0, total_h >= 0, total_w <= max_width_bound, total_h <= max_height_bound)
    for name in pattern_names:
        opt.add(total_w >= x[name] + width[name], total_h >= y[name] + height[name])
    max_side = z3.Int("als_max_side")
    opt.add(max_side >= total_w, max_side >= total_h, max_side <= max(max_width_bound, max_height_bound))

    device_exprs = _device_center_choice_exprs(row_instances, choice_vars, x, y)
    shared_sd_objective_total, shared_sd_exprs = _shared_sd_objective_from_device_exprs(
        spec.pairs,
        graph,
        device_exprs,
    )
    hpwl_terms: list[object] = []
    hpwl_by_net_expr: dict[str, object] = {}
    for net_spec in spec.critical_nets:
        if not net_spec.route_in_smt or net_spec.name not in graph.nets:
            continue
        centers = []
        for terminal in graph.nets[net_spec.name].terminals:
            if terminal.device in device_exprs:
                centers.append(device_exprs[terminal.device])
        if len(centers) < 2:
            continue
        minx = z3.Int(f"als_minx__{net_spec.name}")
        maxx = z3.Int(f"als_maxx__{net_spec.name}")
        miny = z3.Int(f"als_miny__{net_spec.name}")
        maxy = z3.Int(f"als_maxy__{net_spec.name}")
        opt.add(minx >= 0, miny >= 0, maxx >= minx, maxy >= miny)
        for cx, cy in centers:
            opt.add(minx <= cx, maxx >= cx, miny <= cy, maxy >= cy)
        hpwl = (maxx - minx) + (maxy - miny)
        hpwl_by_net_expr[net_spec.name] = hpwl
        hpwl_terms.append(max(1, int(net_spec.weight)) * hpwl)

    hpwl_total = z3.Sum(hpwl_terms) if hpwl_terms else z3.IntVal(0)
    pack_objective_total, pack_width_exprs, pack_height_exprs = _add_pack_constraints(
        opt,
        spec.pack_constraints,
        x,
        y,
        width,
        height,
        max_width_bound=max_width_bound,
        max_height_bound=max_height_bound,
        pitch=pitch,
    )
    placement_window_total, placement_window_exprs = _add_placement_windows(
        opt,
        spec.placement_windows,
        x,
        y,
        max_width_bound=max_width_bound,
        max_height_bound=max_height_bound,
    )
    layout_objective_total, layout_objective_exprs = _add_layout_objective_terms(
        opt,
        spec.objective_terms,
        x,
        y,
        width,
        height,
        max_width_bound=max_width_bound,
        max_height_bound=max_height_bound,
    )
    relation_penalty_total = z3.Sum(relation_penalty_terms) if relation_penalty_terms else z3.IntVal(0)
    relation_choice_cost_total = z3.Sum(selected_relation_cost_terms) if selected_relation_cost_terms else z3.IntVal(0)
    pattern_choice_cost_total = z3.Sum(
        [
            _choice_int_expr(
                choice_vars[name],
                tuple(max(0, int(inst.candidate.cost)) for inst in row_instances[name]),
            )
            for name in pattern_names
        ]
    ) if pattern_names else z3.IntVal(0)
    sum_x = z3.Sum([x[name] for name in pattern_names]) if pattern_names else z3.IntVal(0)
    guard_left, guard_bottom, guard_right, guard_top = _guard_ring_halo_tracks(spec, pitch)
    objective_w = total_w + guard_left + guard_right
    objective_h = total_h + guard_bottom + guard_top
    aspect_error = _z3_abs(objective_w * max(1, spec.objective.aspect_den) - objective_h * max(1, spec.objective.aspect_num))
    true_area_weight = _objective_true_area_weight(spec)
    cost = (
        max(0, spec.objective.bbox_weight) * (objective_w + objective_h)
        + max(0, spec.objective.width_weight) * objective_w
        + max(0, spec.objective.height_weight) * objective_h
        + max(0, spec.objective.area_weight) * (objective_w + objective_h + _z3_abs(objective_w - objective_h))
        + true_area_weight * objective_w * objective_h
        + max(0, spec.objective.max_side_weight) * (max_side + max(guard_left + guard_right, guard_bottom + guard_top))
        + max(0, spec.objective.hpwl_weight) * hpwl_total
        + max(0, spec.objective.right_whitespace_weight) * sum_x
        + pack_objective_total
        + placement_window_total
        + max(0, spec.objective.objective_term_weight) * layout_objective_total
        + relation_penalty_total
        + relation_choice_cost_total
        + max(0, spec.objective.realization_weight) * pattern_choice_cost_total
        + shared_sd_objective_total
        + max(0, spec.objective.aspect_weight) * aspect_error
    )
    # Use one integrated objective instead of a long lexicographic chain.  The
    # DSL now expresses soft layout experience, pack windows, HPWL, relation
    # penalties, and PCell realization preference as weighted cost terms.  Z3 is
    # much less likely to hit ``unknown`` when it proves one weighted optimum
    # instead of several sequential optima on the same non-overlap disjunctions.
    opt.minimize(cost)
    check_result = opt.check()
    if check_result != z3.sat:
        return None

    model = opt.model()
    selected_indices = {
        name: _bounded_index(_model_int(model, choice_vars[name]), len(row_instances[name]))
        for name in pattern_names
    }
    selected_instances = tuple(row_instances[name][selected_indices[name]] for name in pattern_names)
    total_width_tracks = _model_int(model, total_w)
    total_height_tracks = _model_int(model, total_h)
    placement_by_name = _placements_from_model(spec, selected_instances, x, y, model, pitch)
    bboxes = {
        name: (
            _model_int(model, x[name]),
            _model_int(model, y[name]),
            _model_int(model, x[name]) + row_instances[name][selected_indices[name]].width_tracks,
            _model_int(model, y[name]) + row_instances[name][selected_indices[name]].height_tracks,
        )
        for name in pattern_names
    }
    hpwl_by_net = {name: _model_int(model, expr) for name, expr in hpwl_by_net_expr.items()}
    shared_sd_terms = {name: _model_int(model, expr) for name, expr in sorted(shared_sd_exprs.items())}
    pack_windows = {
        name: {
            "width_tracks": _model_int(model, pack_width_exprs[name]),
            "height_tracks": _model_int(model, pack_height_exprs[name]),
        }
        for name in sorted(pack_width_exprs)
    }
    layout_objective_terms = {
        name: _model_int(model, expr)
        for name, expr in sorted(layout_objective_exprs.items())
    }
    placement_window_terms = {
        name: _model_int(model, expr)
        for name, expr in sorted(placement_window_exprs.items())
    }
    selected_relations = _selected_relations_from_model(model, selected_relation_bools)
    selected_pcell_realizations = _selected_pcell_realizations_from_instances(selected_instances)
    device_bboxes_tracks = _device_bboxes_tracks_from_model(selected_instances, x, y, model)
    objective_value = _model_int(model, cost)
    score = _candidate_selection_score(total_width_tracks, total_height_tracks, objective_value)
    return _CandidateSolve(
        score,
        tuple(placement_by_name[name] for name in sorted(placement_by_name)),
        bboxes,
        {inst.spec.name: inst.candidate.name for inst in selected_instances},
        total_width_tracks,
        total_height_tracks,
        {
            "total_width_tracks": total_width_tracks,
            "total_height_tracks": total_height_tracks,
            "estimated_area_tracks": total_width_tracks * total_height_tracks,
            "true_area_weight": true_area_weight,
            "true_area_objective": true_area_weight * total_width_tracks * total_height_tracks,
            "max_side_tracks": max(total_width_tracks, total_height_tracks),
            "objective_score": objective_value,
            "selection_score": score,
            "critical_hpwl_tracks2_by_net": hpwl_by_net,
            "pack_windows_tracks": pack_windows,
            "pack_objective": _model_int(model, pack_objective_total),
            "placement_window_terms": placement_window_terms,
            "placement_window_objective": _model_int(model, placement_window_total),
            "layout_objective_terms": layout_objective_terms,
            "layout_objective": _model_int(model, layout_objective_total),
            "shared_sd_terms": shared_sd_terms,
            "shared_sd_objective": _model_int(model, shared_sd_objective_total),
            "soft_relation_penalty": _model_int(model, relation_penalty_total),
            "selected_relations": selected_relations,
            "selected_pcell_realizations": selected_pcell_realizations,
            "selected_pcell_realization_count": len(selected_pcell_realizations),
            "device_bboxes_tracks": device_bboxes_tracks,
            "selected_pattern_choice_indices": selected_indices,
            "candidate_choice_cost": _model_int(model, pattern_choice_cost_total),
            "pattern_choice_mode": "z3_choice_variables",
            "guard_ring_objective_in_z3": bool(any((guard_left, guard_bottom, guard_right, guard_top))),
            "pattern_choice_count_by_pattern": {
                name: len(row_instances[name])
                for name in pattern_names
            },
            "overlap_issues": _pattern_overlap_issues(bboxes),
            "overlap_issue_count": len(_pattern_overlap_issues(bboxes)),
            "smt_verified": True,
            "solver_timeout_ms": effective_solver_timeout_ms,
            "solve_backend": "z3_optimize_pattern_choice",
        },
    )


def _solve_candidate(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
    instances: tuple[_PatternInstance, ...],
    pitch: float,
    *,
    spacing_tracks: int,
    solver_timeout_ms: int | None,
) -> _CandidateSolve | None:
    heuristic_upper: _CandidateSolve | None = None
    hard_simple_relations = _simple_hard_relations(spec.relations)
    if instances:
        # Always compute a cheap legal packing upper bound.  Even when the DSL
        # has only soft relations, the disjunctive non-overlap problem is much
        # easier for Optimize once x/y are bounded near a known feasible layout.
        # Only hard simple relations are kept in this heuristic spec; soft/global
        # experience remains in the SMT objective.
        heuristic_spec = spec if len(hard_simple_relations) == len(spec.relations) else type(spec)(
            block=spec.block,
            patterns=spec.patterns,
            pairs=spec.pairs,
            relations=hard_simple_relations,
            critical_nets=spec.critical_nets,
            route_resources=spec.route_resources,
            pack_constraints=spec.pack_constraints,
            placement_windows=spec.placement_windows,
            objective_terms=spec.objective_terms,
            pcell_realization_groups=spec.pcell_realization_groups,
            noncritical_router=spec.noncritical_router,
            objective=spec.objective,
            drc=spec.drc,
            notes=spec.notes,
        )
        heuristic_upper = _solve_candidate_by_relation_relaxation(
            heuristic_spec,
            graph,
            instances,
            pitch,
            spacing_tracks=spacing_tracks,
            solver_timeout_ms=solver_timeout_ms,
        )
        if heuristic_upper is not None and (
            int(heuristic_upper.checks.get("overlap_issue_count", 0) or 0) > 0
            or not bool(heuristic_upper.checks.get("direct_relation_verified", True))
        ):
            heuristic_upper = None
    bounded_upper: _CandidateSolve | None = None
    if heuristic_upper is not None:
        bounded_upper = _solve_candidate_by_bounded_smt_compaction(
            spec,
            graph,
            instances,
            pitch,
            spacing_tracks=spacing_tracks,
            initial=heuristic_upper,
            solver_timeout_ms=solver_timeout_ms,
        )
        if bounded_upper is not None:
            heuristic_upper = bounded_upper
            if _prefer_bounded_compaction_result(spec, instances):
                return _with_checks(
                    bounded_upper,
                    solve_backend="bounded_smt_compaction",
                    z3_optimize_skipped=True,
                    z3_optimize_skip_reason="large soft-objective placement problem",
                )

    opt = z3.Optimize()
    effective_solver_timeout_ms = _positive_int_or_default(solver_timeout_ms, 15_000)
    if heuristic_upper is not None:
        effective_solver_timeout_ms = min(effective_solver_timeout_ms, 15_000)
    _configure_z3_solver_options(opt, effective_solver_timeout_ms)
    x = {inst.spec.name: z3.Int(f"als_x__{inst.spec.name}") for inst in instances}
    y = {inst.spec.name: z3.Int(f"als_y__{inst.spec.name}") for inst in instances}
    width = {inst.spec.name: int(inst.width_tracks) for inst in instances}
    height = {inst.spec.name: int(inst.height_tracks) for inst in instances}
    names = tuple(inst.spec.name for inst in instances)
    max_width_bound = max(1, sum(width.values()) + spacing_tracks * (len(names) + 4))
    max_height_bound = max(1, sum(height.values()) + spacing_tracks * (len(names) + 4))
    if heuristic_upper is not None:
        slack = max(4, spacing_tracks) * (8 if len(hard_simple_relations) != len(spec.relations) else 2)
        max_width_bound = min(max_width_bound, max(1, heuristic_upper.total_width_tracks + slack))
        max_height_bound = min(max_height_bound, max(1, heuristic_upper.total_height_tracks + slack))
    for name in names:
        opt.add(x[name] >= 0, y[name] >= 0, x[name] <= max_width_bound, y[name] <= max_height_bound)

    for idx, left in enumerate(names):
        for right in names[idx + 1 :]:
            opt.add(
                z3.Or(
                    x[left] + width[left] + spacing_tracks <= x[right],
                    x[right] + width[right] + spacing_tracks <= x[left],
                    y[left] + height[left] + spacing_tracks <= y[right],
                    y[right] + height[right] + spacing_tracks <= y[left],
                )
            )

    relation_penalty_terms: list[object] = []
    selected_relation_bools: dict[str, dict[str, object]] = {}
    selected_relation_cost_terms: list[object] = []
    for relation_index, relation in enumerate(spec.relations):
        if relation.source not in x or relation.target not in x:
            continue
        gap = _um_to_tracks(relation.min_gap_um, pitch)
        tol = _um_to_tracks(relation.tolerance_um, pitch)
        s, t = relation.source, relation.target
        if relation.candidates:
            choice_vars: dict[str, object] = {}
            bools = []
            for kind in tuple(dict.fromkeys(str(item).lower() for item in relation.candidates if str(item))):
                var = z3.Bool(f"als_rel__{relation_index}__{s}__{t}__{kind}")
                choice_vars[kind] = var
                bools.append(var)
                constraint = _relation_constraint_expr(kind, s, t, x, y, width, height, gap, tol)
                if constraint is not None:
                    if relation.hard:
                        opt.add(z3.Implies(var, constraint))
                    else:
                        penalty = _relation_violation_expr(kind, s, t, x, y, width, height, gap, tol)
                        if penalty is not None:
                            relation_penalty_terms.append(z3.If(var, max(1, int(relation.weight)) * penalty, 0))
                selected_relation_cost_terms.append(
                    z3.If(var, int(relation.candidate_costs.get(kind, 0)), 0)
                )
            if bools:
                opt.add(z3.PbEq([(var, 1) for var in bools], 1))
                selected_relation_bools[_relation_key(relation_index, relation)] = choice_vars
            continue

        kind = relation.kind.lower()
        if relation.hard:
            constraint = _relation_constraint_expr(kind, s, t, x, y, width, height, gap, tol)
            if constraint is not None:
                opt.add(constraint)
        else:
            penalty = _relation_violation_expr(kind, s, t, x, y, width, height, gap, tol)
            if penalty is not None:
                relation_penalty_terms.append(max(1, int(relation.weight)) * penalty)

    total_w = z3.Int("als_total_width")
    total_h = z3.Int("als_total_height")
    opt.add(total_w >= 0, total_h >= 0, total_w <= max_width_bound, total_h <= max_height_bound)
    for name in names:
        opt.add(total_w >= x[name] + width[name], total_h >= y[name] + height[name])
    max_side = z3.Int("als_max_side")
    opt.add(max_side >= total_w, max_side >= total_h, max_side <= max(max_width_bound, max_height_bound))

    device_exprs = _device_center_exprs(instances, x, y)
    shared_sd_objective_total, shared_sd_exprs = _shared_sd_objective_from_device_exprs(
        spec.pairs,
        graph,
        device_exprs,
    )
    hpwl_terms: list[object] = []
    hpwl_by_net_expr: dict[str, object] = {}
    for net_spec in spec.critical_nets:
        if not net_spec.route_in_smt or net_spec.name not in graph.nets:
            continue
        centers = []
        for terminal in graph.nets[net_spec.name].terminals:
            if terminal.device in device_exprs:
                centers.append(device_exprs[terminal.device])
        if len(centers) < 2:
            continue
        minx = z3.Int(f"als_minx__{net_spec.name}")
        maxx = z3.Int(f"als_maxx__{net_spec.name}")
        miny = z3.Int(f"als_miny__{net_spec.name}")
        maxy = z3.Int(f"als_maxy__{net_spec.name}")
        opt.add(minx >= 0, miny >= 0, maxx >= minx, maxy >= miny)
        for cx, cy in centers:
            opt.add(minx <= cx, maxx >= cx, miny <= cy, maxy >= cy)
        hpwl = (maxx - minx) + (maxy - miny)
        hpwl_by_net_expr[net_spec.name] = hpwl
        hpwl_terms.append(max(1, int(net_spec.weight)) * hpwl)

    hpwl_total = z3.Sum(hpwl_terms) if hpwl_terms else z3.IntVal(0)
    pack_objective_total, pack_width_exprs, pack_height_exprs = _add_pack_constraints(
        opt,
        spec.pack_constraints,
        x,
        y,
        width,
        height,
        max_width_bound=max_width_bound,
        max_height_bound=max_height_bound,
        pitch=pitch,
    )
    placement_window_total, placement_window_exprs = _add_placement_windows(
        opt,
        spec.placement_windows,
        x,
        y,
        max_width_bound=max_width_bound,
        max_height_bound=max_height_bound,
    )
    layout_objective_total, layout_objective_exprs = _add_layout_objective_terms(
        opt,
        spec.objective_terms,
        x,
        y,
        width,
        height,
        max_width_bound=max_width_bound,
        max_height_bound=max_height_bound,
    )
    relation_penalty_total = z3.Sum(relation_penalty_terms) if relation_penalty_terms else z3.IntVal(0)
    relation_choice_cost_total = z3.Sum(selected_relation_cost_terms) if selected_relation_cost_terms else z3.IntVal(0)
    sum_x = z3.Sum([x[name] for name in names]) if names else z3.IntVal(0)
    guard_left, guard_bottom, guard_right, guard_top = _guard_ring_halo_tracks(spec, pitch)
    objective_w = total_w + guard_left + guard_right
    objective_h = total_h + guard_bottom + guard_top
    aspect_error = _z3_abs(objective_w * max(1, spec.objective.aspect_den) - objective_h * max(1, spec.objective.aspect_num))
    true_area_weight = _objective_true_area_weight(spec)
    cost = (
        max(0, spec.objective.bbox_weight) * (objective_w + objective_h)
        + max(0, spec.objective.width_weight) * objective_w
        + max(0, spec.objective.height_weight) * objective_h
        + max(0, spec.objective.area_weight) * (objective_w + objective_h + _z3_abs(objective_w - objective_h))
        + true_area_weight * objective_w * objective_h
        + max(0, spec.objective.max_side_weight) * (max_side + max(guard_left + guard_right, guard_bottom + guard_top))
        + max(0, spec.objective.hpwl_weight) * hpwl_total
        + max(0, spec.objective.right_whitespace_weight) * sum_x
        + pack_objective_total
        + placement_window_total
        + max(0, spec.objective.objective_term_weight) * layout_objective_total
        + relation_penalty_total
        + relation_choice_cost_total
        + shared_sd_objective_total
        + max(0, spec.objective.aspect_weight) * aspect_error
    )
    # Same integrated objective policy as the pattern-choice solver.  Compactness
    # remains in ``cost`` through bbox/width/height/max-side weights; pack,
    # routing and soft relation terms are optimized in the same pass.
    opt.minimize(cost)
    check_result = opt.check()
    if check_result != z3.sat:
        if heuristic_upper is not None:
            return _with_checks(
                heuristic_upper,
                solve_backend="relation_relaxation_fallback_after_z3_optimize",
                z3_optimize_result=str(check_result),
                z3_optimize_timeout_ms=effective_solver_timeout_ms,
            )
        return None

    model = opt.model()
    total_width_tracks = _model_int(model, total_w)
    total_height_tracks = _model_int(model, total_h)
    placement_by_name = _placements_from_model(spec, instances, x, y, model, pitch)
    bboxes = {
        name: (
            _model_int(model, x[name]),
            _model_int(model, y[name]),
            _model_int(model, x[name]) + width[name],
            _model_int(model, y[name]) + height[name],
        )
        for name in names
    }
    hpwl_by_net = {name: _model_int(model, expr) for name, expr in hpwl_by_net_expr.items()}
    shared_sd_terms = {name: _model_int(model, expr) for name, expr in sorted(shared_sd_exprs.items())}
    pack_windows = {
        name: {
            "width_tracks": _model_int(model, pack_width_exprs[name]),
            "height_tracks": _model_int(model, pack_height_exprs[name]),
        }
        for name in sorted(pack_width_exprs)
    }
    layout_objective_terms = {
        name: _model_int(model, expr)
        for name, expr in sorted(layout_objective_exprs.items())
    }
    placement_window_terms = {
        name: _model_int(model, expr)
        for name, expr in sorted(placement_window_exprs.items())
    }
    selected_relations = _selected_relations_from_model(model, selected_relation_bools)
    selected_pcell_realizations = _selected_pcell_realizations_from_instances(instances)
    device_bboxes_tracks = _device_bboxes_tracks_from_model(instances, x, y, model)
    objective_value = _model_int(model, cost)
    score = _candidate_selection_score(total_width_tracks, total_height_tracks, objective_value)
    return _CandidateSolve(
        score,
        tuple(placement_by_name[name] for name in sorted(placement_by_name)),
        bboxes,
        {inst.spec.name: inst.candidate.name for inst in instances},
        total_width_tracks,
        total_height_tracks,
        {
            "total_width_tracks": total_width_tracks,
            "total_height_tracks": total_height_tracks,
            "estimated_area_tracks": total_width_tracks * total_height_tracks,
            "true_area_weight": true_area_weight,
            "true_area_objective": true_area_weight * total_width_tracks * total_height_tracks,
            "max_side_tracks": max(total_width_tracks, total_height_tracks),
            "objective_score": objective_value,
            "selection_score": score,
            "critical_hpwl_tracks2_by_net": hpwl_by_net,
            "pack_windows_tracks": pack_windows,
            "pack_objective": _model_int(model, pack_objective_total),
            "placement_window_terms": placement_window_terms,
            "placement_window_objective": _model_int(model, placement_window_total),
            "layout_objective_terms": layout_objective_terms,
            "layout_objective": _model_int(model, layout_objective_total),
            "shared_sd_terms": shared_sd_terms,
            "shared_sd_objective": _model_int(model, shared_sd_objective_total),
            "soft_relation_penalty": _model_int(model, relation_penalty_total),
            "selected_relations": selected_relations,
            "selected_pcell_realizations": selected_pcell_realizations,
            "selected_pcell_realization_count": len(selected_pcell_realizations),
            "device_bboxes_tracks": device_bboxes_tracks,
            "overlap_issues": _pattern_overlap_issues(bboxes),
            "overlap_issue_count": len(_pattern_overlap_issues(bboxes)),
            "smt_verified": True,
            "solver_timeout_ms": effective_solver_timeout_ms,
            "solve_backend": "z3_optimize",
            "guard_ring_objective_in_z3": bool(any((guard_left, guard_bottom, guard_right, guard_top))),
        },
    )


def _solve_candidate_by_relation_relaxation(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
    instances: tuple[_PatternInstance, ...],
    pitch: float,
    *,
    spacing_tracks: int,
    solver_timeout_ms: int | None,
) -> _CandidateSolve | None:
    width = {inst.spec.name: int(inst.width_tracks) for inst in instances}
    height = {inst.spec.name: int(inst.height_tracks) for inst in instances}
    names = tuple(inst.spec.name for inst in instances)
    x_pos = {name: 0 for name in names}
    y_pos = {name: 0 for name in names}
    max_iterations = max(8, len(spec.relations) * max(1, len(names)) * 2)
    for _ in range(max_iterations):
        changed = False
        for relation in spec.relations:
            if relation.source not in x_pos or relation.target not in x_pos:
                continue
            gap = _um_to_tracks(relation.min_gap_um, pitch)
            tol2 = 2 * _um_to_tracks(relation.tolerance_um, pitch)
            source, target = relation.source, relation.target
            kind = relation.kind.lower()
            if kind in {"right_of", "source_left_of_target"}:
                changed |= _raise_to(x_pos, target, x_pos[source] + width[source] + gap)
            elif kind in {"left_of", "source_right_of_target"}:
                changed |= _raise_to(x_pos, source, x_pos[target] + width[target] + gap)
            elif kind in {"above", "source_below_target"}:
                changed |= _raise_to(y_pos, target, y_pos[source] + height[source] + gap)
            elif kind in {"below", "source_above_target"}:
                changed |= _raise_to(y_pos, source, y_pos[target] + height[target] + gap)
            elif kind in {"align_x", "align_center_x", "same_center_x"}:
                s2 = 2 * x_pos[source] + width[source]
                t2 = 2 * x_pos[target] + width[target]
                if abs(s2 - t2) > max(0, tol2):
                    if s2 < t2:
                        changed |= _raise_to(x_pos, source, max(0, (t2 - width[source] + 1) // 2))
                    else:
                        changed |= _raise_to(x_pos, target, max(0, (s2 - width[target] + 1) // 2))
            elif kind in {"align_y", "align_center_y", "same_center_y"}:
                s2 = 2 * y_pos[source] + height[source]
                t2 = 2 * y_pos[target] + height[target]
                if abs(s2 - t2) > max(0, tol2):
                    if s2 < t2:
                        changed |= _raise_to(y_pos, source, max(0, (t2 - height[source] + 1) // 2))
                    else:
                        changed |= _raise_to(y_pos, target, max(0, (s2 - height[target] + 1) // 2))
            elif kind == "overlap_x":
                if x_pos[source] + width[source] <= x_pos[target]:
                    changed |= _raise_to(x_pos, source, x_pos[target] - width[source] + 1)
                elif x_pos[target] + width[target] <= x_pos[source]:
                    changed |= _raise_to(x_pos, target, x_pos[source] - width[target] + 1)
            elif kind == "overlap_y":
                if y_pos[source] + height[source] <= y_pos[target]:
                    changed |= _raise_to(y_pos, source, y_pos[target] - height[source] + 1)
                elif y_pos[target] + height[target] <= y_pos[source]:
                    changed |= _raise_to(y_pos, target, y_pos[source] - height[target] + 1)
        changed |= _resolve_overlaps_greedily(x_pos, y_pos, width, height, spacing_tracks)
        if not changed:
            break
    else:
        return None

    bboxes = {name: (x_pos[name], y_pos[name], x_pos[name] + width[name], y_pos[name] + height[name]) for name in names}
    overlap_issues = _pattern_overlap_issues(bboxes)
    pack_issues = _pack_constraint_issues_from_bboxes(spec.pack_constraints, bboxes, pitch)
    placement_window_issues = _placement_window_issues_from_bboxes(spec.placement_windows, bboxes)
    if pack_issues or placement_window_issues:
        return None
    placements = _placements_from_positions(spec, instances, x_pos, y_pos, pitch)
    device_bboxes_tracks = _device_bboxes_tracks_from_positions(instances, x_pos, y_pos)
    shared_sd_terms = _shared_sd_terms_from_device_bboxes(spec.pairs, graph, device_bboxes_tracks)
    shared_sd_objective = sum(shared_sd_terms.values())
    total_width_tracks = max((bbox[2] for bbox in bboxes.values()), default=0)
    total_height_tracks = max((bbox[3] for bbox in bboxes.values()), default=0)
    hpwl_by_net = _hpwl_by_net_from_positions(spec, graph, instances, x_pos, y_pos)
    hpwl_total = sum(hpwl_by_net.values())
    pack_windows = _pack_windows_from_bboxes(spec.pack_constraints, bboxes)
    pack_objective = _pack_objective_from_windows(spec.pack_constraints, pack_windows)
    placement_window_terms = _placement_window_terms_from_bboxes(spec.placement_windows, bboxes)
    placement_window_objective = _placement_window_objective_from_terms(spec.placement_windows, placement_window_terms)
    layout_objective_terms = _layout_objective_terms_from_bboxes(spec.objective_terms, bboxes)
    layout_objective = sum(
        max(1, int(term.weight)) * int(layout_objective_terms.get(term.name, 0))
        for term in spec.objective_terms
    )
    selected_pcell_realizations = _selected_pcell_realizations_from_instances(instances)
    true_area_weight = _objective_true_area_weight(spec)
    objective_score = (
        max(0, spec.objective.bbox_weight) * (total_width_tracks + total_height_tracks)
        + max(0, spec.objective.width_weight) * total_width_tracks
        + max(0, spec.objective.height_weight) * total_height_tracks
        + max(0, spec.objective.area_weight)
        * (total_width_tracks + total_height_tracks + abs(total_width_tracks - total_height_tracks))
        + true_area_weight * total_width_tracks * total_height_tracks
        + max(0, spec.objective.max_side_weight) * max(total_width_tracks, total_height_tracks)
        + max(0, spec.objective.hpwl_weight) * hpwl_total
        + max(0, spec.objective.right_whitespace_weight) * sum(x_pos.values())
        + max(0, spec.objective.objective_term_weight) * layout_objective
        + max(0, spec.objective.aspect_weight)
        * abs(total_width_tracks * max(1, spec.objective.aspect_den) - total_height_tracks * max(1, spec.objective.aspect_num))
        + pack_objective
        + placement_window_objective
        + shared_sd_objective
    )
    return _CandidateSolve(
        _candidate_selection_score(total_width_tracks, total_height_tracks, objective_score),
        tuple(placements[name] for name in sorted(placements)),
        bboxes,
        {inst.spec.name: inst.candidate.name for inst in instances},
        total_width_tracks,
        total_height_tracks,
        {
            "total_width_tracks": total_width_tracks,
            "total_height_tracks": total_height_tracks,
            "estimated_area_tracks": total_width_tracks * total_height_tracks,
            "true_area_weight": true_area_weight,
            "true_area_objective": true_area_weight * total_width_tracks * total_height_tracks,
            "max_side_tracks": max(total_width_tracks, total_height_tracks),
            "objective_score": objective_score,
            "selection_score": _candidate_selection_score(total_width_tracks, total_height_tracks, objective_score),
            "critical_hpwl_tracks2_by_net": hpwl_by_net,
            "pack_windows_tracks": pack_windows,
            "pack_objective": pack_objective,
            "placement_window_terms": placement_window_terms,
            "placement_window_objective": placement_window_objective,
            "layout_objective_terms": layout_objective_terms,
            "layout_objective": layout_objective,
            "shared_sd_terms": shared_sd_terms,
            "shared_sd_objective": shared_sd_objective,
            "selected_pcell_realizations": selected_pcell_realizations,
            "selected_pcell_realization_count": len(selected_pcell_realizations),
            "device_bboxes_tracks": device_bboxes_tracks,
            "overlap_issues": overlap_issues,
            "overlap_issue_count": len(overlap_issues),
            "direct_relation_verified": _verify_relation_positions_direct(
                spec,
                instances,
                x_pos,
                y_pos,
                pitch,
            ),
            "smt_verified": _verify_relation_positions_direct(
                spec,
                instances,
                x_pos,
                y_pos,
                pitch,
            ),
            "solver_timeout_ms": _positive_int_or_default(solver_timeout_ms, 15_000),
            "solve_backend": "dsl_relation_compact_direct_verify",
        },
    )


def _prefer_bounded_compaction_result(spec: AnalogLayoutSpec, instances: Sequence[_PatternInstance]) -> bool:
    """Use bounded feasibility compaction for larger soft-objective problems.

    For bandgap/LDO-like blocks the expensive part is not satisfiability; it is
    proving the optimum of HPWL + pack windows + soft relation choices +
    high-level layout-objective terms.  A bounded SMT feasibility search gives a
    compact legal placement quickly; the fixed-envelope secondary SMT phase then
    optimizes those soft costs before detailed routing/ECO.
    """

    if any(not bool(window.hard) for window in tuple(spec.placement_windows)):
        return False
    soft_objective_terms = (
        len(tuple(spec.pack_constraints))
        + len(tuple(spec.objective_terms))
        + len(tuple(spec.critical_nets))
        + len(tuple(relation for relation in spec.relations if not bool(relation.hard) or relation.candidates))
    )
    return len(tuple(instances)) >= 5 and soft_objective_terms >= 8


def _bounded_area_width_candidates(width_lower: int, width_upper: int, *, max_count: int) -> tuple[int, ...]:
    width_lower = max(1, int(width_lower))
    width_upper = max(width_lower, int(width_upper))
    max_count = max(2, int(max_count))
    span = width_upper - width_lower
    if span <= max_count:
        return tuple(range(width_lower, width_upper + 1))
    values = {width_lower, width_upper}
    for idx in range(max_count):
        values.add(width_lower + int(round(span * idx / max(1, max_count - 1))))
    return tuple(sorted(value for value in values if width_lower <= value <= width_upper))


def _bounded_area_compaction_search(
    spec: AnalogLayoutSpec,
    names: Sequence[str],
    width: Mapping[str, int],
    height: Mapping[str, int],
    pitch: float,
    *,
    spacing_tracks: int,
    initial_width_tracks: int,
    initial_height_tracks: int,
    solver_timeout_ms: int,
) -> tuple[
    dict[str, tuple[int, int]] | None,
    tuple[int, int] | None,
    dict[str, str],
    int,
    int,
]:
    """Search compact W/H bounds without putting ``W*H`` into the SAT query."""

    initial_width_tracks = max(1, int(initial_width_tracks))
    initial_height_tracks = max(1, int(initial_height_tracks))
    width_lower = max(max((int(width[name]) for name in names), default=1), 1)
    height_lower = max(max((int(height[name]) for name in names), default=1), 1)
    device_area_lower = max(1, sum(max(1, int(width[name])) * max(1, int(height[name])) for name in names))
    timeout_budget = _positive_int_or_default(solver_timeout_ms, 15_000)
    per_attempt_timeout_ms = max(150, min(600, timeout_budget // 20 if timeout_budget >= 20 else timeout_budget))
    max_attempts = max(12, min(40, timeout_budget // max(1, per_attempt_timeout_ms)))
    width_candidates = _bounded_area_width_candidates(
        width_lower,
        initial_width_tracks,
        max_count=max(4, min(18, max_attempts // 2)),
    )
    max_height_bound_absolute = max(
        initial_height_tracks,
        sum(max(1, int(height[name])) for name in names) + max(0, int(spacing_tracks)) * (len(tuple(names)) + 4),
    )
    best_area = initial_width_tracks * initial_height_tracks
    best_sum = initial_width_tracks + initial_height_tracks
    best_positions: dict[str, tuple[int, int]] | None = None
    best_wh: tuple[int, int] | None = None
    best_selected_relations: dict[str, str] = {}
    attempts = 0
    sat_attempts = 0

    for max_width_tracks in width_candidates:
        if attempts >= max_attempts:
            break
        min_height_by_area = int(ceil(device_area_lower / max(1, int(max_width_tracks))))
        low = max(height_lower, min_height_by_area)
        area_height_cap = max(0, (best_area - 1) // max(1, int(max_width_tracks)))
        high = min(max_height_bound_absolute, area_height_cap)
        if low > high:
            continue
        while low <= high and attempts < max_attempts:
            target_height = (low + high) // 2
            attempts += 1
            solved = _bounded_compaction_solve_once(
                spec,
                names,
                width,
                height,
                pitch,
                spacing_tracks=spacing_tracks,
                max_sum_tracks=int(max_width_tracks) + int(target_height),
                max_width_tracks=int(max_width_tracks),
                max_height_tracks=int(target_height),
                solver_timeout_ms=per_attempt_timeout_ms,
            )
            if solved is None:
                low = target_height + 1
                continue
            sat_attempts += 1
            positions, wh, selected_relations = solved
            candidate_area = int(wh[0]) * int(wh[1])
            candidate_sum = int(wh[0]) + int(wh[1])
            if candidate_area < best_area or (candidate_area == best_area and candidate_sum < best_sum):
                best_area = candidate_area
                best_sum = candidate_sum
                best_positions = positions
                best_wh = wh
                best_selected_relations = selected_relations
            high = min(target_height - 1, int(wh[1]) - 1)

    return best_positions, best_wh, best_selected_relations, attempts, sat_attempts


def _bounded_sum_compaction_search(
    spec: AnalogLayoutSpec,
    names: Sequence[str],
    width: Mapping[str, int],
    height: Mapping[str, int],
    pitch: float,
    *,
    spacing_tracks: int,
    initial_width_tracks: int,
    initial_height_tracks: int,
    lower_sum_tracks: int,
    solver_timeout_ms: int,
) -> tuple[
    dict[str, tuple[int, int]] | None,
    tuple[int, int] | None,
    dict[str, str],
    int,
    int,
]:
    timeout_budget = _positive_int_or_default(solver_timeout_ms, 15_000)
    per_attempt_timeout_ms = max(250, min(1_500, timeout_budget // 6))
    best_sum = int(initial_width_tracks) + int(initial_height_tracks)
    low = int(lower_sum_tracks)
    high = best_sum - 1
    attempts = 0
    sat_attempts = 0
    best_positions: dict[str, tuple[int, int]] | None = None
    best_wh: tuple[int, int] | None = None
    best_selected_relations: dict[str, str] = {}

    while low <= high and attempts < 8:
        attempts += 1
        target_sum = (low + high) // 2
        solved = _bounded_compaction_solve_once(
            spec,
            names,
            width,
            height,
            pitch,
            spacing_tracks=spacing_tracks,
            max_sum_tracks=target_sum,
            max_width_tracks=int(initial_width_tracks),
            max_height_tracks=int(initial_height_tracks),
            solver_timeout_ms=per_attempt_timeout_ms,
        )
        if solved is None:
            low = target_sum + 1
            continue
        sat_attempts += 1
        positions, wh, selected_relations = solved
        best_positions = positions
        best_wh = wh
        best_selected_relations = selected_relations
        best_sum = int(wh[0]) + int(wh[1])
        high = target_sum - 1

    return best_positions, best_wh, best_selected_relations, attempts, sat_attempts


def _solve_candidate_by_bounded_smt_compaction(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
    instances: tuple[_PatternInstance, ...],
    pitch: float,
    *,
    spacing_tracks: int,
    initial: _CandidateSolve,
    solver_timeout_ms: int | None,
) -> _CandidateSolve | None:
    """Improve a legal heuristic placement with bounded SMT feasibility checks."""

    names = tuple(inst.spec.name for inst in instances)
    if len(names) < 2 or initial.total_width_tracks <= 0 or initial.total_height_tracks <= 0:
        return initial
    width = {inst.spec.name: int(inst.width_tracks) for inst in instances}
    height = {inst.spec.name: int(inst.height_tracks) for inst in instances}
    best_positions = {
        name: (int(bbox[0]), int(bbox[1]))
        for name, bbox in dict(initial.pattern_bboxes_tracks).items()
        if name in width
    }
    if len(best_positions) != len(names):
        return initial

    initial_width_tracks = int(initial.total_width_tracks)
    initial_height_tracks = int(initial.total_height_tracks)
    true_area_weight = _objective_true_area_weight(spec)
    timeout_budget = _positive_int_or_default(solver_timeout_ms, 15_000)
    bounded_mode = "area" if true_area_weight > 0 else "sum"
    best_sum = initial_width_tracks + initial_height_tracks
    lower_sum = max(max(width.values(), default=1), 1) + max(max(height.values(), default=1), 1)
    if best_sum <= lower_sum:
        return initial

    attempts = 0
    sat_attempts = 0
    best_model_positions: dict[str, tuple[int, int]] | None = None
    best_model_wh: tuple[int, int] | None = None
    best_selected_relations: dict[str, str] = {}
    selected_bounded_mode = bounded_mode

    if true_area_weight > 0:
        bounded_mode = "area+sum"
        area_positions, area_wh, area_relations, area_attempts, area_sat_attempts = _bounded_area_compaction_search(
            spec,
            names,
            width,
            height,
            pitch,
            spacing_tracks=spacing_tracks,
            initial_width_tracks=initial_width_tracks,
            initial_height_tracks=initial_height_tracks,
            solver_timeout_ms=timeout_budget,
        )
        sum_positions, sum_wh, sum_relations, sum_attempts, sum_sat_attempts = _bounded_sum_compaction_search(
            spec,
            names,
            width,
            height,
            pitch,
            spacing_tracks=spacing_tracks,
            initial_width_tracks=initial_width_tracks,
            initial_height_tracks=initial_height_tracks,
            lower_sum_tracks=lower_sum,
            solver_timeout_ms=timeout_budget,
        )
        attempts = area_attempts + sum_attempts
        sat_attempts = area_sat_attempts + sum_sat_attempts
        candidates = []
        if area_positions is not None and area_wh is not None:
            candidates.append(("area", area_positions, area_wh, area_relations))
        if sum_positions is not None and sum_wh is not None:
            candidates.append(("sum", sum_positions, sum_wh, sum_relations))
        if candidates:
            selected_bounded_mode, best_model_positions, best_model_wh, best_selected_relations = min(
                candidates,
                key=lambda row: (
                    _effective_area_score_for_wh(spec, int(row[2][0]), int(row[2][1])),
                    int(row[2][0]) * int(row[2][1]),
                    int(row[2][0]) + int(row[2][1]),
                    max(int(row[2][0]), int(row[2][1])),
                ),
            )
    else:
        (
            best_model_positions,
            best_model_wh,
            best_selected_relations,
            attempts,
            sat_attempts,
        ) = _bounded_sum_compaction_search(
            spec,
            names,
            width,
            height,
            pitch,
            spacing_tracks=spacing_tracks,
            initial_width_tracks=initial_width_tracks,
            initial_height_tracks=initial_height_tracks,
            lower_sum_tracks=lower_sum,
            solver_timeout_ms=timeout_budget,
        )

    bounded_compaction_found = best_model_positions is not None and best_model_wh is not None
    if not bounded_compaction_found:
        _, initial_soft_selected_relations = _soft_relation_penalty_from_bboxes(
            spec.relations,
            initial.pattern_bboxes_tracks,
            pitch,
        )
        best_model_positions = {
            name: (int(initial.pattern_bboxes_tracks[name][0]), int(initial.pattern_bboxes_tracks[name][1]))
            for name in names
        }
        best_model_wh = (initial_width_tracks, initial_height_tracks)
        best_selected_relations = {
            **dict(initial.checks.get("selected_relations", {}) or {}),
            **initial_soft_selected_relations,
        }

    pre_refine_positions = dict(best_model_positions)
    pre_refine_relations = dict(best_selected_relations)
    secondary_before = _bounded_secondary_objective_from_positions(
        spec, graph, instances, pre_refine_positions, pitch
    )
    try:
        soft_refinement_timeout_ms = max(
            100,
            min(timeout_budget, int(get_env("BOUNDED_SOFT_REFINEMENT_TIMEOUT_MS", "1000") or 1000)),
        )
    except ValueError:
        soft_refinement_timeout_ms = max(100, min(timeout_budget, 1_000))
    refined = _bounded_soft_refinement_solve_once(
        spec,
        graph,
        instances,
        width,
        height,
        pitch,
        spacing_tracks=spacing_tracks,
        fixed_width_tracks=int(best_model_wh[0]),
        fixed_height_tracks=int(best_model_wh[1]),
        initial_positions=pre_refine_positions,
        solver_timeout_ms=soft_refinement_timeout_ms,
    )
    soft_refinement_attempted = True
    soft_refinement_solved = refined is not None
    if refined is not None:
        refined_positions, refined_relations = refined
        secondary_after = _bounded_secondary_objective_from_positions(
            spec, graph, instances, refined_positions, pitch
        )
        if secondary_after <= secondary_before:
            best_model_positions = refined_positions
            best_selected_relations = {**pre_refine_relations, **refined_relations}
        else:
            secondary_after = secondary_before
    else:
        secondary_after = secondary_before

    bboxes = {
        name: (
            best_model_positions[name][0],
            best_model_positions[name][1],
            best_model_positions[name][0] + width[name],
            best_model_positions[name][1] + height[name],
        )
        for name in names
    }
    placements = _placements_from_positions(spec, instances, {k: v[0] for k, v in best_model_positions.items()}, {k: v[1] for k, v in best_model_positions.items()}, pitch)
    device_bboxes_tracks = _device_bboxes_tracks_from_positions(
        instances,
        {k: v[0] for k, v in best_model_positions.items()},
        {k: v[1] for k, v in best_model_positions.items()},
    )
    shared_sd_terms = _shared_sd_terms_from_device_bboxes(spec.pairs, graph, device_bboxes_tracks)
    shared_sd_objective = sum(shared_sd_terms.values())
    total_width_tracks, total_height_tracks = best_model_wh
    hpwl_by_net = _hpwl_by_net_from_positions(
        spec,
        graph,
        instances,
        {k: v[0] for k, v in best_model_positions.items()},
        {k: v[1] for k, v in best_model_positions.items()},
    )
    hpwl_total = sum(hpwl_by_net.values())
    pack_windows = _pack_windows_from_bboxes(spec.pack_constraints, bboxes)
    pack_objective = _pack_objective_from_windows(spec.pack_constraints, pack_windows)
    placement_window_terms = _placement_window_terms_from_bboxes(spec.placement_windows, bboxes)
    placement_window_objective = _placement_window_objective_from_terms(spec.placement_windows, placement_window_terms)
    layout_objective_terms = _layout_objective_terms_from_bboxes(spec.objective_terms, bboxes)
    layout_objective = sum(
        max(1, int(term.weight)) * int(layout_objective_terms.get(term.name, 0))
        for term in spec.objective_terms
    )
    soft_relation_penalty, soft_selected_relations = _soft_relation_penalty_from_bboxes(spec.relations, bboxes, pitch)
    selected_pcell_realizations = _selected_pcell_realizations_from_instances(instances)
    objective_score = (
        max(0, spec.objective.bbox_weight) * (total_width_tracks + total_height_tracks)
        + max(0, spec.objective.width_weight) * total_width_tracks
        + max(0, spec.objective.height_weight) * total_height_tracks
        + max(0, spec.objective.area_weight)
        * (total_width_tracks + total_height_tracks + abs(total_width_tracks - total_height_tracks))
        + true_area_weight * total_width_tracks * total_height_tracks
        + max(0, spec.objective.max_side_weight) * max(total_width_tracks, total_height_tracks)
        + max(0, spec.objective.hpwl_weight) * hpwl_total
        + max(0, spec.objective.right_whitespace_weight) * sum(pos[0] for pos in best_model_positions.values())
        + pack_objective
        + placement_window_objective
        + max(0, spec.objective.objective_term_weight) * layout_objective
        + soft_relation_penalty
        + shared_sd_objective
        + max(0, spec.objective.aspect_weight)
        * abs(total_width_tracks * max(1, spec.objective.aspect_den) - total_height_tracks * max(1, spec.objective.aspect_num))
    )
    selected_relations = {**soft_selected_relations, **best_selected_relations}
    return _CandidateSolve(
        _candidate_selection_score(total_width_tracks, total_height_tracks, objective_score),
        tuple(placements[name] for name in sorted(placements)),
        bboxes,
        {inst.spec.name: inst.candidate.name for inst in instances},
        total_width_tracks,
        total_height_tracks,
        {
            "total_width_tracks": total_width_tracks,
            "total_height_tracks": total_height_tracks,
            "estimated_area_tracks": total_width_tracks * total_height_tracks,
            "true_area_weight": true_area_weight,
            "true_area_objective": true_area_weight * total_width_tracks * total_height_tracks,
            "max_side_tracks": max(total_width_tracks, total_height_tracks),
            "objective_score": objective_score,
            "selection_score": _candidate_selection_score(total_width_tracks, total_height_tracks, objective_score),
            "critical_hpwl_tracks2_by_net": hpwl_by_net,
            "pack_windows_tracks": pack_windows,
            "pack_objective": pack_objective,
            "placement_window_terms": placement_window_terms,
            "placement_window_objective": placement_window_objective,
            "layout_objective_terms": layout_objective_terms,
            "layout_objective": layout_objective,
            "shared_sd_terms": shared_sd_terms,
            "shared_sd_objective": shared_sd_objective,
            "soft_relation_penalty": soft_relation_penalty,
            "selected_relations": selected_relations,
            "selected_pcell_realizations": selected_pcell_realizations,
            "selected_pcell_realization_count": len(selected_pcell_realizations),
            "device_bboxes_tracks": device_bboxes_tracks,
            "overlap_issues": _pattern_overlap_issues(bboxes),
            "overlap_issue_count": len(_pattern_overlap_issues(bboxes)),
            "smt_verified": True,
            "solver_timeout_ms": timeout_budget,
            "bounded_smt_compaction_mode": bounded_mode,
            "bounded_smt_selected_compaction_mode": selected_bounded_mode,
            "bounded_smt_initial_area_tracks": initial_width_tracks * initial_height_tracks,
            "bounded_smt_compaction_attempts": attempts,
            "bounded_smt_compaction_sat_attempts": sat_attempts,
            "bounded_smt_compaction_found_smaller_envelope": bool(bounded_compaction_found),
            "bounded_smt_compaction_improved": (
                (total_width_tracks * total_height_tracks) < (initial_width_tracks * initial_height_tracks)
                if true_area_weight > 0
                else (total_width_tracks + total_height_tracks) < (initial_width_tracks + initial_height_tracks)
            ),
            "bounded_smt_soft_refinement_attempted": soft_refinement_attempted,
            "bounded_smt_soft_refinement_solved": soft_refinement_solved,
            "bounded_smt_soft_refinement_accepted": bool(
                soft_refinement_solved and secondary_after <= secondary_before
            ),
            "bounded_smt_secondary_objective_before": int(secondary_before),
            "bounded_smt_secondary_objective_after": int(secondary_after),
            "bounded_smt_soft_refinement_timeout_ms": int(soft_refinement_timeout_ms),
            "solve_backend": "bounded_smt_compaction",
        },
    )


def _bounded_compaction_solve_once(
    spec: AnalogLayoutSpec,
    names: Sequence[str],
    width: Mapping[str, int],
    height: Mapping[str, int],
    pitch: float,
    *,
    spacing_tracks: int,
    max_sum_tracks: int,
    max_width_tracks: int,
    max_height_tracks: int,
    solver_timeout_ms: int,
) -> tuple[dict[str, tuple[int, int]], tuple[int, int], dict[str, str]] | None:
    solver = z3.Solver()
    _configure_z3_solver_options(solver, max(1, int(solver_timeout_ms)))
    x = {name: z3.Int(f"als_bc_x__{_safe_symbol_name(name)}") for name in names}
    y = {name: z3.Int(f"als_bc_y__{_safe_symbol_name(name)}") for name in names}
    total_w = z3.Int("als_bc_total_width")
    total_h = z3.Int("als_bc_total_height")
    solver.add(total_w >= 0, total_h >= 0, total_w <= max_width_tracks, total_h <= max_height_tracks)
    solver.add(total_w + total_h <= max(1, int(max_sum_tracks)))
    for name in names:
        solver.add(
            x[name] >= 0,
            y[name] >= 0,
            x[name] <= max_width_tracks,
            y[name] <= max_height_tracks,
            total_w >= x[name] + int(width[name]),
            total_h >= y[name] + int(height[name]),
        )
    _add_hard_placement_windows_to_solver(solver, spec.placement_windows, x, y)
    for idx, left in enumerate(names):
        for right in names[idx + 1 :]:
            solver.add(
                z3.Or(
                    x[left] + int(width[left]) + spacing_tracks <= x[right],
                    x[right] + int(width[right]) + spacing_tracks <= x[left],
                    y[left] + int(height[left]) + spacing_tracks <= y[right],
                    y[right] + int(height[right]) + spacing_tracks <= y[left],
                )
            )

    selected_relation_bools: dict[str, dict[str, object]] = {}
    for relation_index, relation in enumerate(spec.relations):
        if relation.source not in x or relation.target not in x:
            continue
        if not bool(relation.hard):
            continue
        gap = _um_to_tracks(relation.min_gap_um, pitch)
        tol = _um_to_tracks(relation.tolerance_um, pitch)
        if relation.candidates:
            relation_bools = []
            choice_by_kind: dict[str, object] = {}
            for kind in tuple(dict.fromkeys(str(item).lower() for item in relation.candidates if str(item))):
                constraint = _relation_constraint_expr(kind, relation.source, relation.target, x, y, width, height, gap, tol)
                if constraint is None:
                    continue
                var = z3.Bool(f"als_bc_rel__{relation_index}__{relation.source}__{relation.target}__{kind}")
                solver.add(z3.Implies(var, constraint))
                relation_bools.append(var)
                choice_by_kind[kind] = var
            if relation_bools:
                solver.add(z3.PbEq([(var, 1) for var in relation_bools], 1))
                selected_relation_bools[_relation_key(relation_index, relation)] = choice_by_kind
            continue
        constraint = _relation_constraint_expr(
            relation.kind,
            relation.source,
            relation.target,
            x,
            y,
            width,
            height,
            gap,
            tol,
        )
        if constraint is not None:
            solver.add(constraint)

    _add_pack_hard_limits(
        solver,
        spec.pack_constraints,
        x,
        y,
        width,
        height,
        pitch=pitch,
        max_width_bound=max_width_tracks,
        max_height_bound=max_height_tracks,
    )
    if solver.check() != z3.sat:
        return None
    model = solver.model()
    positions = {name: (_model_int(model, x[name]), _model_int(model, y[name])) for name in names}
    wh = (_model_int(model, total_w), _model_int(model, total_h))
    return positions, wh, _selected_relations_from_model(model, selected_relation_bools)


def _bounded_soft_refinement_solve_once(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
    instances: Sequence[_PatternInstance],
    width: Mapping[str, int],
    height: Mapping[str, int],
    pitch: float,
    *,
    spacing_tracks: int,
    fixed_width_tracks: int,
    fixed_height_tracks: int,
    initial_positions: Mapping[str, tuple[int, int]],
    solver_timeout_ms: int,
) -> tuple[dict[str, tuple[int, int]], dict[str, str]] | None:
    """Optimize aesthetics/routing cost without reopening the compact bbox.

    Bounded feasibility finds the smallest legal envelope quickly.  This second
    phase fixes that envelope and lets soft relations, critical-net HPWL,
    local packs, placement windows, shared-S/D proximity and DSL aesthetic
    objectives move macros inside it.  Thus soft intent affects coordinates
    without sacrificing the area result from phase one.
    """

    names = tuple(str(name) for name in width)
    if not names:
        return None
    has_secondary = bool(
        any(not bool(row.hard) or tuple(row.candidates) for row in spec.relations)
        or spec.critical_nets
        or spec.pack_constraints
        or spec.placement_windows
        or spec.objective_terms
        or any(bool(getattr(row, "shared_sd", False)) for row in spec.pairs)
    )
    if not has_secondary:
        return None

    opt = z3.Optimize()
    _configure_z3_solver_options(opt, max(1, int(solver_timeout_ms)))
    x = {name: z3.Int(f"als_br_x__{_safe_symbol_name(name)}") for name in names}
    y = {name: z3.Int(f"als_br_y__{_safe_symbol_name(name)}") for name in names}
    max_w = max(1, int(fixed_width_tracks))
    max_h = max(1, int(fixed_height_tracks))
    for name in names:
        opt.add(
            x[name] >= 0,
            y[name] >= 0,
            x[name] + int(width[name]) <= max_w,
            y[name] + int(height[name]) <= max_h,
        )
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            opt.add(
                z3.Or(
                    x[left] + int(width[left]) + spacing_tracks <= x[right],
                    x[right] + int(width[right]) + spacing_tracks <= x[left],
                    y[left] + int(height[left]) + spacing_tracks <= y[right],
                    y[right] + int(height[right]) + spacing_tracks <= y[left],
                )
            )

    relation_terms: list[object] = []
    selected_relation_bools: dict[str, dict[str, object]] = {}
    for relation_index, relation in enumerate(spec.relations):
        if relation.source not in x or relation.target not in x:
            continue
        gap = _um_to_tracks(relation.min_gap_um, pitch)
        tol = _um_to_tracks(relation.tolerance_um, pitch)
        if relation.candidates:
            choice_by_kind: dict[str, object] = {}
            vars_for_relation = []
            for kind in tuple(dict.fromkeys(str(item).lower() for item in relation.candidates if str(item))):
                var = z3.Bool(f"als_br_rel__{relation_index}__{_safe_symbol_name(kind)}")
                choice_by_kind[kind] = var
                vars_for_relation.append(var)
                constraint = _relation_constraint_expr(
                    kind, relation.source, relation.target, x, y, width, height, gap, tol
                )
                penalty = _relation_violation_expr(
                    kind, relation.source, relation.target, x, y, width, height, gap, tol
                )
                if relation.hard and constraint is not None:
                    opt.add(z3.Implies(var, constraint))
                elif penalty is not None:
                    relation_terms.append(z3.If(var, max(1, int(relation.weight)) * penalty, 0))
                relation_terms.append(z3.If(var, int(relation.candidate_costs.get(kind, 0)), 0))
            if vars_for_relation:
                opt.add(z3.PbEq([(var, 1) for var in vars_for_relation], 1))
                selected_relation_bools[_relation_key(relation_index, relation)] = choice_by_kind
            continue
        constraint = _relation_constraint_expr(
            relation.kind, relation.source, relation.target, x, y, width, height, gap, tol
        )
        if relation.hard:
            if constraint is not None:
                opt.add(constraint)
        else:
            penalty = _relation_violation_expr(
                relation.kind, relation.source, relation.target, x, y, width, height, gap, tol
            )
            if penalty is not None:
                relation_terms.append(max(1, int(relation.weight)) * penalty)

    pack_total, _, _ = _add_pack_constraints(
        opt, spec.pack_constraints, x, y, width, height,
        max_width_bound=max_w, max_height_bound=max_h, pitch=pitch,
    )
    placement_total, _ = _add_placement_windows(
        opt, spec.placement_windows, x, y,
        max_width_bound=max_w, max_height_bound=max_h,
    )
    layout_total, _ = _add_layout_objective_terms(
        opt, spec.objective_terms, x, y, width, height,
        max_width_bound=max_w, max_height_bound=max_h,
    )
    device_exprs = _device_center_exprs(instances, x, y)
    shared_total, _ = _shared_sd_objective_from_device_exprs(spec.pairs, graph, device_exprs)

    hpwl_terms: list[object] = []
    for net_index, net_spec in enumerate(spec.critical_nets):
        if not net_spec.route_in_smt or net_spec.name not in graph.nets:
            continue
        centers = [
            device_exprs[terminal.device]
            for terminal in graph.nets[net_spec.name].terminals
            if terminal.device in device_exprs
        ]
        if len(centers) < 2:
            continue
        minx = z3.Int(f"als_br_minx__{net_index}")
        maxx = z3.Int(f"als_br_maxx__{net_index}")
        miny = z3.Int(f"als_br_miny__{net_index}")
        maxy = z3.Int(f"als_br_maxy__{net_index}")
        opt.add(minx >= 0, miny >= 0, maxx >= minx, maxy >= miny)
        for cx, cy in centers:
            opt.add(minx <= cx, maxx >= cx, miny <= cy, maxy >= cy)
        hpwl_terms.append(max(1, int(net_spec.weight)) * ((maxx - minx) + (maxy - miny)))

    secondary = (
        (z3.Sum(relation_terms) if relation_terms else z3.IntVal(0))
        + max(0, int(spec.objective.hpwl_weight)) * (z3.Sum(hpwl_terms) if hpwl_terms else z3.IntVal(0))
        + pack_total
        + placement_total
        + max(0, int(spec.objective.objective_term_weight)) * layout_total
        + shared_total
    )
    displacement = z3.Sum(
        [
            _z3_abs(x[name] - int(initial_positions[name][0]))
            + _z3_abs(y[name] - int(initial_positions[name][1]))
            for name in names
            if name in initial_positions
        ]
    )
    opt.minimize(secondary)
    opt.minimize(displacement)
    if opt.check() != z3.sat:
        return None
    model = opt.model()
    positions = {name: (_model_int(model, x[name]), _model_int(model, y[name])) for name in names}
    return positions, _selected_relations_from_model(model, selected_relation_bools)


def _bounded_secondary_objective_from_positions(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
    instances: Sequence[_PatternInstance],
    positions: Mapping[str, tuple[int, int]],
    pitch: float,
) -> int:
    bboxes = {
        inst.spec.name: (
            int(positions[inst.spec.name][0]),
            int(positions[inst.spec.name][1]),
            int(positions[inst.spec.name][0]) + int(inst.width_tracks),
            int(positions[inst.spec.name][1]) + int(inst.height_tracks),
        )
        for inst in instances
    }
    x_pos = {name: int(value[0]) for name, value in positions.items()}
    y_pos = {name: int(value[1]) for name, value in positions.items()}
    soft, _ = _soft_relation_penalty_from_bboxes(spec.relations, bboxes, pitch)
    hpwl = sum(_hpwl_by_net_from_positions(spec, graph, instances, x_pos, y_pos).values())
    pack = _pack_objective_from_windows(spec.pack_constraints, _pack_windows_from_bboxes(spec.pack_constraints, bboxes))
    placement_terms = _placement_window_terms_from_bboxes(spec.placement_windows, bboxes)
    placement = _placement_window_objective_from_terms(spec.placement_windows, placement_terms)
    layout_terms = _layout_objective_terms_from_bboxes(spec.objective_terms, bboxes)
    layout = sum(max(1, int(term.weight)) * int(layout_terms.get(term.name, 0)) for term in spec.objective_terms)
    device_bboxes = _device_bboxes_tracks_from_positions(instances, x_pos, y_pos)
    shared = sum(_shared_sd_terms_from_device_bboxes(spec.pairs, graph, device_bboxes).values())
    return int(
        soft
        + max(0, int(spec.objective.hpwl_weight)) * hpwl
        + pack
        + placement
        + max(0, int(spec.objective.objective_term_weight)) * layout
        + shared
    )


def _add_pack_hard_limits(
    solver: object,
    packs: Sequence[PackConstraintSpec],
    x: Mapping[str, object],
    y: Mapping[str, object],
    width: Mapping[str, int],
    height: Mapping[str, int],
    *,
    pitch: float,
    max_width_bound: int,
    max_height_bound: int,
) -> None:
    for pack in packs:
        if pack.max_width_um is None and pack.max_height_um is None:
            continue
        members = tuple(dict.fromkeys(str(name) for name in pack.patterns if str(name) in x))
        if len(members) < 2:
            continue
        safe_name = _safe_symbol_name(pack.name)
        px0 = z3.Int(f"als_bc_pack_x0__{safe_name}")
        py0 = z3.Int(f"als_bc_pack_y0__{safe_name}")
        px1 = z3.Int(f"als_bc_pack_x1__{safe_name}")
        py1 = z3.Int(f"als_bc_pack_y1__{safe_name}")
        solver.add(px0 >= 0, py0 >= 0, px1 >= px0, py1 >= py0, px1 <= max_width_bound, py1 <= max_height_bound)
        for name in members:
            solver.add(px0 <= x[name], py0 <= y[name], px1 >= x[name] + int(width[name]), py1 >= y[name] + int(height[name]))
        if pack.max_width_um is not None:
            solver.add(px1 - px0 <= _um_to_tracks(pack.max_width_um, pitch))
        if pack.max_height_um is not None:
            solver.add(py1 - py0 <= _um_to_tracks(pack.max_height_um, pitch))


def _soft_relation_penalty_from_bboxes(
    relations: Sequence[PatternRelationSpec],
    bboxes: Mapping[str, tuple[int, int, int, int]],
    pitch: float,
) -> tuple[int, dict[str, str]]:
    total = 0
    selected: dict[str, str] = {}
    for relation_index, relation in enumerate(relations):
        if relation.source not in bboxes or relation.target not in bboxes:
            continue
        gap = _um_to_tracks(relation.min_gap_um, pitch)
        tol = _um_to_tracks(relation.tolerance_um, pitch)
        if relation.candidates:
            best_kind = ""
            best_cost: int | None = None
            for kind in tuple(dict.fromkeys(str(item).lower() for item in relation.candidates if str(item))):
                violation = _relation_violation_tracks_from_bboxes(
                    kind,
                    bboxes[relation.source],
                    bboxes[relation.target],
                    gap,
                    tol,
                )
                cost = max(1, int(relation.weight)) * violation + int(relation.candidate_costs.get(kind, 0))
                if best_cost is None or cost < best_cost:
                    best_cost = cost
                    best_kind = kind
            if best_kind:
                selected[_relation_key(relation_index, relation)] = best_kind
            if not bool(relation.hard) and best_cost is not None:
                total += max(0, int(best_cost))
            continue
        if bool(relation.hard):
            continue
        violation = _relation_violation_tracks_from_bboxes(
            relation.kind,
            bboxes[relation.source],
            bboxes[relation.target],
            gap,
            tol,
        )
        total += max(1, int(relation.weight)) * violation
    return total, selected


def _relation_violation_tracks_from_bboxes(
    kind: str,
    source_bbox: tuple[int, int, int, int],
    target_bbox: tuple[int, int, int, int],
    gap: int,
    tol: int,
) -> int:
    kind = str(kind).lower()
    sx0, sy0, sx1, sy1 = source_bbox
    tx0, ty0, tx1, ty1 = target_bbox
    if kind in {"right_of", "source_left_of_target"}:
        return max(0, sx1 + gap - tx0)
    if kind in {"left_of", "source_right_of_target"}:
        return max(0, tx1 + gap - sx0)
    if kind in {"above", "source_below_target"}:
        return max(0, sy1 + gap - ty0)
    if kind in {"below", "source_above_target"}:
        return max(0, ty1 + gap - sy0)
    if kind in {"align_x", "align_center_x", "same_center_x"}:
        return max(0, abs((sx0 + sx1) - (tx0 + tx1)) - max(0, 2 * tol))
    if kind in {"align_y", "align_center_y", "same_center_y"}:
        return max(0, abs((sy0 + sy1) - (ty0 + ty1)) - max(0, 2 * tol))
    if kind == "overlap_x":
        if sx0 >= tx1:
            return sx0 - tx1
        if tx0 >= sx1:
            return tx0 - sx1
        return 0
    if kind == "overlap_y":
        if sy0 >= ty1:
            return sy0 - ty1
        if ty0 >= sy1:
            return ty0 - sy1
        return 0
    return 0


def _simple_hard_relations(relations: Sequence[PatternRelationSpec]) -> tuple[PatternRelationSpec, ...]:
    return tuple(
        relation
        for relation in relations
        if bool(getattr(relation, "hard", True)) and not tuple(getattr(relation, "candidates", ()) or ())
    )


def _has_flexible_relations(relations: Sequence[PatternRelationSpec]) -> bool:
    return any(
        not bool(getattr(relation, "hard", True)) or tuple(getattr(relation, "candidates", ()) or ())
        for relation in relations
    )


def _relation_key(index: int, relation: PatternRelationSpec) -> str:
    return f"{index}:{relation.source}->{relation.target}"


def _relation_constraint_expr(
    kind: str,
    source: str,
    target: str,
    x: Mapping[str, object],
    y: Mapping[str, object],
    width: Mapping[str, int],
    height: Mapping[str, int],
    gap: int,
    tol: int,
) -> object | None:
    kind = str(kind).lower()
    if kind in {"right_of", "source_left_of_target"}:
        return x[target] >= x[source] + width[source] + gap
    if kind in {"left_of", "source_right_of_target"}:
        return x[source] >= x[target] + width[target] + gap
    if kind in {"above", "source_below_target"}:
        return y[target] >= y[source] + height[source] + gap
    if kind in {"below", "source_above_target"}:
        return y[source] >= y[target] + height[target] + gap
    if kind in {"align_x", "align_center_x", "same_center_x"}:
        lhs = 2 * x[source] + width[source]
        rhs = 2 * x[target] + width[target]
        return _z3_abs(lhs - rhs) <= max(0, 2 * tol)
    if kind in {"align_y", "align_center_y", "same_center_y"}:
        lhs = 2 * y[source] + height[source]
        rhs = 2 * y[target] + height[target]
        return _z3_abs(lhs - rhs) <= max(0, 2 * tol)
    if kind == "overlap_x":
        return z3.And(x[source] < x[target] + width[target], x[target] < x[source] + width[source])
    if kind == "overlap_y":
        return z3.And(y[source] < y[target] + height[target], y[target] < y[source] + height[source])
    return None


def _relation_violation_expr(
    kind: str,
    source: str,
    target: str,
    x: Mapping[str, object],
    y: Mapping[str, object],
    width: Mapping[str, int],
    height: Mapping[str, int],
    gap: int,
    tol: int,
) -> object | None:
    kind = str(kind).lower()
    if kind in {"right_of", "source_left_of_target"}:
        return _z3_pos((x[source] + width[source] + gap) - x[target])
    if kind in {"left_of", "source_right_of_target"}:
        return _z3_pos((x[target] + width[target] + gap) - x[source])
    if kind in {"above", "source_below_target"}:
        return _z3_pos((y[source] + height[source] + gap) - y[target])
    if kind in {"below", "source_above_target"}:
        return _z3_pos((y[target] + height[target] + gap) - y[source])
    if kind in {"align_x", "align_center_x", "same_center_x"}:
        lhs = 2 * x[source] + width[source]
        rhs = 2 * x[target] + width[target]
        return _z3_pos(_z3_abs(lhs - rhs) - max(0, 2 * tol))
    if kind in {"align_y", "align_center_y", "same_center_y"}:
        lhs = 2 * y[source] + height[source]
        rhs = 2 * y[target] + height[target]
        return _z3_pos(_z3_abs(lhs - rhs) - max(0, 2 * tol))
    if kind == "overlap_x":
        return z3.If(
            x[source] >= x[target] + width[target],
            x[source] - (x[target] + width[target]),
            z3.If(x[target] >= x[source] + width[source], x[target] - (x[source] + width[source]), 0),
        )
    if kind == "overlap_y":
        return z3.If(
            y[source] >= y[target] + height[target],
            y[source] - (y[target] + height[target]),
            z3.If(y[target] >= y[source] + height[source], y[target] - (y[source] + height[source]), 0),
        )
    return None


def _add_pack_constraints(
    opt: object,
    packs: Sequence[PackConstraintSpec],
    x: Mapping[str, object],
    y: Mapping[str, object],
    width: Mapping[str, int],
    height: Mapping[str, int],
    *,
    max_width_bound: int,
    max_height_bound: int,
    pitch: float,
) -> tuple[object, Mapping[str, object], Mapping[str, object]]:
    terms: list[object] = []
    width_exprs: dict[str, object] = {}
    height_exprs: dict[str, object] = {}
    for pack in packs:
        members = tuple(dict.fromkeys(str(name) for name in pack.patterns if str(name) in x))
        if len(members) < 2:
            continue
        safe_name = _safe_symbol_name(pack.name)
        px0 = z3.Int(f"als_pack_x0__{safe_name}")
        py0 = z3.Int(f"als_pack_y0__{safe_name}")
        px1 = z3.Int(f"als_pack_x1__{safe_name}")
        py1 = z3.Int(f"als_pack_y1__{safe_name}")
        pw = z3.Int(f"als_pack_w__{safe_name}")
        ph = z3.Int(f"als_pack_h__{safe_name}")
        opt.add(
            px0 >= 0,
            py0 >= 0,
            px1 >= px0,
            py1 >= py0,
            px1 <= max_width_bound,
            py1 <= max_height_bound,
            pw == px1 - px0,
            ph == py1 - py0,
        )
        for name in members:
            opt.add(px0 <= x[name], py0 <= y[name], px1 >= x[name] + width[name], py1 >= y[name] + height[name])
        if pack.max_width_um is not None:
            opt.add(pw <= _um_to_tracks(pack.max_width_um, pitch))
        if pack.max_height_um is not None:
            opt.add(ph <= _um_to_tracks(pack.max_height_um, pitch))
        width_exprs[pack.name] = pw
        height_exprs[pack.name] = ph
        terms.append(
            max(1, int(pack.weight))
            * (
                max(0, int(pack.width_weight)) * pw
                + max(0, int(pack.height_weight)) * ph
                + max(0, int(pack.area_weight)) * (pw + ph + _z3_abs(pw - ph))
            )
        )
    return (z3.Sum(terms) if terms else z3.IntVal(0), width_exprs, height_exprs)


def _add_placement_windows(
    opt: object,
    windows: Sequence[PlacementWindowSpec],
    x: Mapping[str, object],
    y: Mapping[str, object],
    *,
    max_width_bound: int,
    max_height_bound: int,
) -> tuple[object, Mapping[str, object]]:
    terms: list[object] = []
    exprs: dict[str, object] = {}
    for window in windows:
        pattern = str(window.pattern)
        if pattern not in x:
            continue
        safe_name = _safe_symbol_name(window.name)
        parts: list[object] = []
        bounds = (
            ("min_x", x[pattern], window.min_x_tracks, ">="),
            ("max_x", x[pattern], window.max_x_tracks, "<="),
            ("min_y", y[pattern], window.min_y_tracks, ">="),
            ("max_y", y[pattern], window.max_y_tracks, "<="),
        )
        for _, expr, value, sense in bounds:
            if value is None:
                continue
            bound = int(value)
            if sense == ">=":
                if window.hard:
                    opt.add(expr >= bound)
                else:
                    parts.append(_z3_pos(bound - expr))
            else:
                if window.hard:
                    opt.add(expr <= bound)
                else:
                    parts.append(_z3_pos(expr - bound))
        target_parts: list[object] = []
        if window.target_x_tracks is not None:
            target_x = max(0, min(int(window.target_x_tracks), int(max_width_bound)))
            if window.hard:
                opt.add(x[pattern] == target_x)
            else:
                target_parts.append(_z3_abs(x[pattern] - target_x))
        if window.target_y_tracks is not None:
            target_y = max(0, min(int(window.target_y_tracks), int(max_height_bound)))
            if window.hard:
                opt.add(y[pattern] == target_y)
            else:
                target_parts.append(_z3_abs(y[pattern] - target_y))
        parts.extend(target_parts)
        expr = z3.Sum(parts) if parts else z3.IntVal(0)
        exprs[window.name] = expr
        if not bool(window.hard) and parts:
            terms.append(max(1, int(window.weight)) * expr)
    return (z3.Sum(terms) if terms else z3.IntVal(0), exprs)


def _add_hard_placement_windows_to_solver(
    solver: object,
    windows: Sequence[PlacementWindowSpec],
    x: Mapping[str, object],
    y: Mapping[str, object],
) -> None:
    for window in windows:
        if not bool(window.hard):
            continue
        pattern = str(window.pattern)
        if pattern not in x:
            continue
        if window.min_x_tracks is not None:
            solver.add(x[pattern] >= int(window.min_x_tracks))
        if window.max_x_tracks is not None:
            solver.add(x[pattern] <= int(window.max_x_tracks))
        if window.min_y_tracks is not None:
            solver.add(y[pattern] >= int(window.min_y_tracks))
        if window.max_y_tracks is not None:
            solver.add(y[pattern] <= int(window.max_y_tracks))
        if window.target_x_tracks is not None:
            solver.add(x[pattern] == int(window.target_x_tracks))
        if window.target_y_tracks is not None:
            solver.add(y[pattern] == int(window.target_y_tracks))


def _add_layout_objective_terms(
    opt: object,
    objective_terms: Sequence[LayoutObjectiveTermSpec],
    x: Mapping[str, object],
    y: Mapping[str, object],
    width: Mapping[str, object],
    height: Mapping[str, object],
    *,
    max_width_bound: int,
    max_height_bound: int,
) -> tuple[object, Mapping[str, object]]:
    terms: list[object] = []
    exprs: dict[str, object] = {}
    for term in objective_terms:
        members = tuple(dict.fromkeys(str(name) for name in term.patterns if str(name) in x))
        if len(members) < 2:
            continue
        kind = str(term.kind).lower()
        axis = str(term.axis or "both").lower()
        weight = max(1, int(term.weight))
        safe_name = _safe_symbol_name(term.name)
        expr: object | None = None

        if kind in {"compact", "compact_envelope", "local_envelope", "void", "whitespace", "minimize_gap"}:
            px0 = z3.Int(f"als_obj_x0__{safe_name}")
            py0 = z3.Int(f"als_obj_y0__{safe_name}")
            px1 = z3.Int(f"als_obj_x1__{safe_name}")
            py1 = z3.Int(f"als_obj_y1__{safe_name}")
            pw = z3.Int(f"als_obj_w__{safe_name}")
            ph = z3.Int(f"als_obj_h__{safe_name}")
            opt.add(
                px0 >= 0,
                py0 >= 0,
                px1 >= px0,
                py1 >= py0,
                px1 <= max_width_bound,
                py1 <= max_height_bound,
                pw == px1 - px0,
                ph == py1 - py0,
            )
            for name in members:
                opt.add(px0 <= x[name], py0 <= y[name], px1 >= x[name] + width[name], py1 >= y[name] + height[name])
            if axis == "x":
                expr = pw
            elif axis == "y":
                expr = ph
            elif kind in {"void", "whitespace", "minimize_gap"}:
                expr = pw + ph + _z3_abs(pw - ph)
            else:
                expr = pw + ph

        elif kind in {"edge_alignment", "align_edges"}:
            anchor = members[0]
            parts: list[object] = []
            for name in members[1:]:
                if axis in {"x", "both", "xy"}:
                    parts.append(_z3_abs(x[name] - x[anchor]))
                    parts.append(_z3_abs((x[name] + width[name]) - (x[anchor] + width[anchor])))
                if axis in {"y", "both", "xy"}:
                    parts.append(_z3_abs(y[name] - y[anchor]))
                    parts.append(_z3_abs((y[name] + height[name]) - (y[anchor] + height[anchor])))
            expr = z3.Sum(parts) if parts else None

        elif kind in {"center_alignment", "align_centers", "centerline_alignment"}:
            anchor = members[0]
            parts = []
            for name in members[1:]:
                if axis in {"x", "both", "xy"}:
                    parts.append(_z3_abs((2 * x[name] + width[name]) - (2 * x[anchor] + width[anchor])))
                if axis in {"y", "both", "xy"}:
                    parts.append(_z3_abs((2 * y[name] + height[name]) - (2 * y[anchor] + height[anchor])))
            expr = z3.Sum(parts) if parts else None

        elif kind in {"same_width", "width_match"}:
            anchor = members[0]
            expr = z3.Sum([_z3_abs(width[name] - width[anchor]) for name in members[1:]])

        elif kind in {"same_height", "height_match"}:
            anchor = members[0]
            expr = z3.Sum([_z3_abs(height[name] - height[anchor]) for name in members[1:]])

        elif kind in {"aesthetic_squareness", "squareness", "square_bbox", "bbox_aspect"}:
            bounds = _add_z3_group_envelope(
                opt,
                f"als_obj_square__{safe_name}",
                members,
                x,
                y,
                width,
                height,
                max_width_bound=max_width_bound,
                max_height_bound=max_height_bound,
            )
            if bounds is not None:
                _, _, _, _, pw, ph = bounds
                aspect_num, aspect_den = _target_aspect_ints(term.target)
                expr = _z3_abs(pw * aspect_den - ph * aspect_num)

        elif kind in {"mirror_symmetry", "symmetry", "aesthetic_symmetry"}:
            bounds = _add_z3_group_envelope(
                opt,
                f"als_obj_sym__{safe_name}",
                members,
                x,
                y,
                width,
                height,
                max_width_bound=max_width_bound,
                max_height_bound=max_height_bound,
            )
            if bounds is not None:
                px0, py0, px1, py1, _, _ = bounds
                expr = _mirror_symmetry_expr_z3(members, x, y, width, height, px0, py0, px1, py1, axis)

        elif kind in {"regular_spacing", "spacing_regularity", "aesthetic_regularity"}:
            expr = _regular_spacing_expr_z3(members, x, y, width, height, axis)

        if expr is None:
            continue
        exprs[term.name] = expr
        terms.append(weight * expr)
    return (z3.Sum(terms) if terms else z3.IntVal(0), exprs)


def _add_z3_group_envelope(
    opt: object,
    prefix: str,
    members: Sequence[str],
    x: Mapping[str, object],
    y: Mapping[str, object],
    width: Mapping[str, object],
    height: Mapping[str, object],
    *,
    max_width_bound: int,
    max_height_bound: int,
) -> tuple[object, object, object, object, object, object] | None:
    if not members:
        return None
    px0 = z3.Int(f"{prefix}_x0")
    py0 = z3.Int(f"{prefix}_y0")
    px1 = z3.Int(f"{prefix}_x1")
    py1 = z3.Int(f"{prefix}_y1")
    pw = z3.Int(f"{prefix}_w")
    ph = z3.Int(f"{prefix}_h")
    opt.add(
        px0 >= 0,
        py0 >= 0,
        px1 >= px0,
        py1 >= py0,
        px1 <= max_width_bound,
        py1 <= max_height_bound,
        pw == px1 - px0,
        ph == py1 - py0,
    )
    for name in members:
        opt.add(px0 <= x[name], py0 <= y[name], px1 >= x[name] + width[name], py1 >= y[name] + height[name])
    return px0, py0, px1, py1, pw, ph


def _mirror_symmetry_expr_z3(
    members: Sequence[str],
    x: Mapping[str, object],
    y: Mapping[str, object],
    width: Mapping[str, object],
    height: Mapping[str, object],
    px0: object,
    py0: object,
    px1: object,
    py1: object,
    axis: str,
) -> object | None:
    if len(members) < 2:
        return z3.IntVal(0)
    parts: list[object] = []
    axis = str(axis or "x").lower()
    pair_count = len(members) // 2
    for idx in range(pair_count):
        left = members[idx]
        right = members[-idx - 1]
        if axis in {"x", "vertical", "both", "xy"}:
            parts.append(_z3_abs((2 * x[left] + width[left]) + (2 * x[right] + width[right]) - 2 * (px0 + px1)))
            parts.append(_z3_abs(y[left] - y[right]))
            parts.append(_z3_abs((y[left] + height[left]) - (y[right] + height[right])))
        if axis in {"y", "horizontal", "both", "xy"}:
            parts.append(_z3_abs((2 * y[left] + height[left]) + (2 * y[right] + height[right]) - 2 * (py0 + py1)))
            parts.append(_z3_abs(x[left] - x[right]))
            parts.append(_z3_abs((x[left] + width[left]) - (x[right] + width[right])))
    if len(members) % 2:
        middle = members[pair_count]
        if axis in {"x", "vertical", "both", "xy"}:
            parts.append(_z3_abs((2 * x[middle] + width[middle]) - (px0 + px1)))
        if axis in {"y", "horizontal", "both", "xy"}:
            parts.append(_z3_abs((2 * y[middle] + height[middle]) - (py0 + py1)))
    return z3.Sum(parts) if parts else None


def _regular_spacing_expr_z3(
    members: Sequence[str],
    x: Mapping[str, object],
    y: Mapping[str, object],
    width: Mapping[str, object],
    height: Mapping[str, object],
    axis: str,
) -> object | None:
    axis = str(axis or "x").lower()
    parts: list[object] = []
    if axis in {"x", "both", "xy"}:
        x_gaps = [x[right] - (x[left] + width[left]) for left, right in zip(members, members[1:])]
        if len(x_gaps) >= 2:
            parts.extend(_z3_abs(gap - x_gaps[0]) for gap in x_gaps[1:])
    if axis in {"y", "both", "xy"}:
        y_gaps = [y[right] - (y[left] + height[left]) for left, right in zip(members, members[1:])]
        if len(y_gaps) >= 2:
            parts.extend(_z3_abs(gap - y_gaps[0]) for gap in y_gaps[1:])
    return z3.Sum(parts) if parts else z3.IntVal(0)


def _pack_windows_from_bboxes(
    packs: Sequence[PackConstraintSpec],
    bboxes: Mapping[str, tuple[int, int, int, int]],
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for pack in packs:
        members = tuple(bboxes[name] for name in pack.patterns if name in bboxes)
        if len(members) < 2:
            continue
        x0 = min(item[0] for item in members)
        y0 = min(item[1] for item in members)
        x1 = max(item[2] for item in members)
        y1 = max(item[3] for item in members)
        result[pack.name] = {"width_tracks": x1 - x0, "height_tracks": y1 - y0}
    return result


def _pack_objective_from_windows(
    packs: Sequence[PackConstraintSpec],
    windows: Mapping[str, Mapping[str, int]],
) -> int:
    total = 0
    for pack in packs:
        row = windows.get(pack.name)
        if not row:
            continue
        width_tracks = int(row.get("width_tracks", 0))
        height_tracks = int(row.get("height_tracks", 0))
        total += max(1, int(pack.weight)) * (
            max(0, int(pack.width_weight)) * width_tracks
            + max(0, int(pack.height_weight)) * height_tracks
            + max(0, int(pack.area_weight))
            * (width_tracks + height_tracks + abs(width_tracks - height_tracks))
        )
    return total


def _placement_window_terms_from_bboxes(
    windows: Sequence[PlacementWindowSpec],
    bboxes: Mapping[str, tuple[int, int, int, int]],
) -> dict[str, int]:
    result: dict[str, int] = {}
    for window in windows:
        bbox = bboxes.get(window.pattern)
        if bbox is None:
            continue
        x0, y0, _, _ = bbox
        penalty = 0
        if window.min_x_tracks is not None:
            penalty += max(0, int(window.min_x_tracks) - int(x0))
        if window.max_x_tracks is not None:
            penalty += max(0, int(x0) - int(window.max_x_tracks))
        if window.min_y_tracks is not None:
            penalty += max(0, int(window.min_y_tracks) - int(y0))
        if window.max_y_tracks is not None:
            penalty += max(0, int(y0) - int(window.max_y_tracks))
        if window.target_x_tracks is not None:
            penalty += abs(int(x0) - int(window.target_x_tracks))
        if window.target_y_tracks is not None:
            penalty += abs(int(y0) - int(window.target_y_tracks))
        result[window.name] = penalty
    return result


def _placement_window_objective_from_terms(
    windows: Sequence[PlacementWindowSpec],
    terms: Mapping[str, int],
) -> int:
    total = 0
    for window in windows:
        if bool(window.hard):
            continue
        total += max(1, int(window.weight)) * int(terms.get(window.name, 0))
    return total


def _placement_window_issues_from_bboxes(
    windows: Sequence[PlacementWindowSpec],
    bboxes: Mapping[str, tuple[int, int, int, int]],
) -> tuple[str, ...]:
    issues: list[str] = []
    for window in windows:
        if not bool(window.hard):
            continue
        bbox = bboxes.get(window.pattern)
        if bbox is None:
            continue
        x0, y0, _, _ = bbox
        if window.min_x_tracks is not None and int(x0) < int(window.min_x_tracks):
            issues.append(f"placement window {window.name}: {window.pattern}.x below min")
        if window.max_x_tracks is not None and int(x0) > int(window.max_x_tracks):
            issues.append(f"placement window {window.name}: {window.pattern}.x above max")
        if window.min_y_tracks is not None and int(y0) < int(window.min_y_tracks):
            issues.append(f"placement window {window.name}: {window.pattern}.y below min")
        if window.max_y_tracks is not None and int(y0) > int(window.max_y_tracks):
            issues.append(f"placement window {window.name}: {window.pattern}.y above max")
        if window.target_x_tracks is not None and int(x0) != int(window.target_x_tracks):
            issues.append(f"placement window {window.name}: {window.pattern}.x target mismatch")
        if window.target_y_tracks is not None and int(y0) != int(window.target_y_tracks):
            issues.append(f"placement window {window.name}: {window.pattern}.y target mismatch")
    return tuple(issues)


def _layout_objective_terms_from_bboxes(
    objective_terms: Sequence[LayoutObjectiveTermSpec],
    bboxes: Mapping[str, tuple[int, int, int, int]],
) -> dict[str, int]:
    result: dict[str, int] = {}
    for term in objective_terms:
        members = tuple(dict.fromkeys(str(name) for name in term.patterns if str(name) in bboxes))
        if len(members) < 2:
            continue
        kind = str(term.kind).lower()
        axis = str(term.axis or "both").lower()
        value: int | None = None
        if kind in {"compact", "compact_envelope", "local_envelope", "void", "whitespace", "minimize_gap"}:
            x0 = min(bboxes[name][0] for name in members)
            y0 = min(bboxes[name][1] for name in members)
            x1 = max(bboxes[name][2] for name in members)
            y1 = max(bboxes[name][3] for name in members)
            width_tracks = x1 - x0
            height_tracks = y1 - y0
            if axis == "x":
                value = width_tracks
            elif axis == "y":
                value = height_tracks
            elif kind in {"void", "whitespace", "minimize_gap"}:
                value = width_tracks + height_tracks + abs(width_tracks - height_tracks)
            else:
                value = width_tracks + height_tracks
        elif kind in {"edge_alignment", "align_edges"}:
            anchor = bboxes[members[0]]
            total = 0
            for name in members[1:]:
                bbox = bboxes[name]
                if axis in {"x", "both", "xy"}:
                    total += abs(bbox[0] - anchor[0]) + abs(bbox[2] - anchor[2])
                if axis in {"y", "both", "xy"}:
                    total += abs(bbox[1] - anchor[1]) + abs(bbox[3] - anchor[3])
            value = total
        elif kind in {"center_alignment", "align_centers", "centerline_alignment"}:
            anchor = bboxes[members[0]]
            anchor_cx = anchor[0] + anchor[2]
            anchor_cy = anchor[1] + anchor[3]
            total = 0
            for name in members[1:]:
                bbox = bboxes[name]
                if axis in {"x", "both", "xy"}:
                    total += abs((bbox[0] + bbox[2]) - anchor_cx)
                if axis in {"y", "both", "xy"}:
                    total += abs((bbox[1] + bbox[3]) - anchor_cy)
            value = total
        elif kind in {"same_width", "width_match"}:
            anchor_width = bboxes[members[0]][2] - bboxes[members[0]][0]
            value = sum(abs((bboxes[name][2] - bboxes[name][0]) - anchor_width) for name in members[1:])
        elif kind in {"same_height", "height_match"}:
            anchor_height = bboxes[members[0]][3] - bboxes[members[0]][1]
            value = sum(abs((bboxes[name][3] - bboxes[name][1]) - anchor_height) for name in members[1:])
        elif kind in {"aesthetic_squareness", "squareness", "square_bbox", "bbox_aspect"}:
            x0 = min(bboxes[name][0] for name in members)
            y0 = min(bboxes[name][1] for name in members)
            x1 = max(bboxes[name][2] for name in members)
            y1 = max(bboxes[name][3] for name in members)
            width_tracks = x1 - x0
            height_tracks = y1 - y0
            aspect_num, aspect_den = _target_aspect_ints(term.target)
            value = abs(width_tracks * aspect_den - height_tracks * aspect_num)
        elif kind in {"mirror_symmetry", "symmetry", "aesthetic_symmetry"}:
            value = _mirror_symmetry_value_from_bboxes(members, bboxes, axis)
        elif kind in {"regular_spacing", "spacing_regularity", "aesthetic_regularity"}:
            value = _regular_spacing_value_from_bboxes(members, bboxes, axis)
        if value is not None:
            result[term.name] = int(value)
    return result


def _target_aspect_ints(target: object, default: tuple[int, int] = (1, 1)) -> tuple[int, int]:
    text = str(target or "").strip().lower()
    if not text:
        return default
    for sep in (":", "/", "x"):
        if sep in text:
            left, right = text.split(sep, 1)
            try:
                num = max(1, int(round(float(left.strip()))))
                den = max(1, int(round(float(right.strip()))))
                factor = gcd(num, den) or 1
                return num // factor, den // factor
            except ValueError:
                return default
    try:
        value = float(text)
    except ValueError:
        return default
    if value <= 0:
        return default
    den = 1000
    num = max(1, int(round(value * den)))
    factor = gcd(num, den) or 1
    return num // factor, den // factor


def _mirror_symmetry_value_from_bboxes(
    members: Sequence[str],
    bboxes: Mapping[str, tuple[int, int, int, int]],
    axis: str,
) -> int:
    if len(members) < 2:
        return 0
    x0 = min(bboxes[name][0] for name in members)
    y0 = min(bboxes[name][1] for name in members)
    x1 = max(bboxes[name][2] for name in members)
    y1 = max(bboxes[name][3] for name in members)
    axis = str(axis or "x").lower()
    total = 0
    pair_count = len(members) // 2
    for idx in range(pair_count):
        left = bboxes[members[idx]]
        right = bboxes[members[-idx - 1]]
        if axis in {"x", "vertical", "both", "xy"}:
            total += abs((left[0] + left[2]) + (right[0] + right[2]) - 2 * (x0 + x1))
            total += abs(left[1] - right[1]) + abs(left[3] - right[3])
        if axis in {"y", "horizontal", "both", "xy"}:
            total += abs((left[1] + left[3]) + (right[1] + right[3]) - 2 * (y0 + y1))
            total += abs(left[0] - right[0]) + abs(left[2] - right[2])
    if len(members) % 2:
        middle = bboxes[members[pair_count]]
        if axis in {"x", "vertical", "both", "xy"}:
            total += abs((middle[0] + middle[2]) - (x0 + x1))
        if axis in {"y", "horizontal", "both", "xy"}:
            total += abs((middle[1] + middle[3]) - (y0 + y1))
    return total


def _regular_spacing_value_from_bboxes(
    members: Sequence[str],
    bboxes: Mapping[str, tuple[int, int, int, int]],
    axis: str,
) -> int:
    axis = str(axis or "x").lower()
    total = 0
    if axis in {"x", "both", "xy"}:
        gaps = [bboxes[right][0] - bboxes[left][2] for left, right in zip(members, members[1:])]
        if len(gaps) >= 2:
            total += sum(abs(gap - gaps[0]) for gap in gaps[1:])
    if axis in {"y", "both", "xy"}:
        gaps = [bboxes[right][1] - bboxes[left][3] for left, right in zip(members, members[1:])]
        if len(gaps) >= 2:
            total += sum(abs(gap - gaps[0]) for gap in gaps[1:])
    return total


def _pack_constraint_issues_from_bboxes(
    packs: Sequence[PackConstraintSpec],
    bboxes: Mapping[str, tuple[int, int, int, int]],
    pitch: float,
) -> tuple[str, ...]:
    issues: list[str] = []
    windows = _pack_windows_from_bboxes(packs, bboxes)
    by_name = {pack.name: pack for pack in packs}
    for name, row in windows.items():
        pack = by_name[name]
        width_tracks = int(row.get("width_tracks", 0))
        height_tracks = int(row.get("height_tracks", 0))
        if pack.max_width_um is not None and width_tracks > _um_to_tracks(pack.max_width_um, pitch):
            issues.append(f"pack {name} exceeds max width: {width_tracks} tracks")
        if pack.max_height_um is not None and height_tracks > _um_to_tracks(pack.max_height_um, pitch):
            issues.append(f"pack {name} exceeds max height: {height_tracks} tracks")
    return tuple(issues)


def _safe_symbol_name(value: str) -> str:
    text = str(value) or "pack"
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in text)


def _selected_relations_from_model(model: object, selected_relation_bools: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, by_kind in selected_relation_bools.items():
        for kind, var in by_kind.items():
            if str(model.eval(var, model_completion=True)).lower() == "true":
                result[key] = kind
                break
    return result


def _selected_pcell_realizations_from_instances(
    instances: Sequence[_PatternInstance],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for inst in instances:
        for device, candidate in dict(inst.realizations).items():
            result[str(device)] = {
                "name": candidate.name,
                "pattern": inst.spec.name,
                "width_um": float(candidate.width_um),
                "height_um": float(candidate.height_um),
                "sizing_overrides": dict(candidate.sizing_overrides),
                "pcell_overrides": dict(candidate.pcell_overrides),
                "cost": int(candidate.cost),
                "drc_clean": bool(candidate.drc_clean),
                "lvs_clean": bool(candidate.lvs_clean),
                "notes": candidate.notes,
                "metadata": dict(candidate.metadata),
            }
    return dict(sorted(result.items()))


def _mos_source_drain_terminals(graph: TopologyGraph, device_name: str) -> tuple[str, ...]:
    device = graph.devices.get(str(device_name))
    if device is None:
        return ("S", "D")
    model = str(device.model).lower()
    if "mos" not in model:
        return ()
    return tuple(str(term) for term in device.terminals if str(term).upper() in {"S", "D"})


def _shared_sd_connection_for_pair(
    pair: PairConstraintSpec,
    graph: TopologyGraph,
) -> tuple[str, str, str] | None:
    if not bool(getattr(pair, "shared_sd", False)):
        return None
    left_terms = _mos_source_drain_terminals(graph, pair.left)
    right_terms = _mos_source_drain_terminals(graph, pair.right)
    if not left_terms or not right_terms:
        return None
    term_map = graph.terminal_net_map()
    requested_net = str(getattr(pair, "shared_sd_net", "") or "")
    for left_term in left_terms:
        left_net = term_map.get(TerminalRef(pair.left, left_term))
        if requested_net and left_net != requested_net:
            continue
        for right_term in right_terms:
            right_net = term_map.get(TerminalRef(pair.right, right_term))
            if requested_net:
                if right_net == requested_net:
                    return requested_net, left_term, right_term
                continue
            if left_net and left_net == right_net:
                return str(left_net), left_term, right_term
    return None


def _shared_sd_objective_from_device_exprs(
    pairs: Sequence[PairConstraintSpec],
    graph: TopologyGraph,
    device_exprs: Mapping[str, tuple[object, object]],
) -> tuple[object, dict[str, object]]:
    terms: dict[str, object] = {}
    for pair in pairs:
        if _shared_sd_connection_for_pair(pair, graph) is None:
            continue
        if pair.left not in device_exprs or pair.right not in device_exprs:
            continue
        left_cx, left_cy = device_exprs[pair.left]
        right_cx, right_cy = device_exprs[pair.right]
        weight = max(1, int(getattr(pair, "shared_sd_weight", 0) or 8))
        terms[pair.name] = weight * (_z3_abs(left_cx - right_cx) + _z3_abs(left_cy - right_cy))
    return (z3.Sum(list(terms.values())) if terms else z3.IntVal(0), terms)


def _device_bboxes_tracks_from_model(
    instances: Sequence[_PatternInstance],
    x: Mapping[str, object],
    y: Mapping[str, object],
    model: object,
) -> dict[str, tuple[int, int, int, int]]:
    result: dict[str, tuple[int, int, int, int]] = {}
    for inst in instances:
        base_x = _model_int(model, x[inst.spec.name])
        base_y = _model_int(model, y[inst.spec.name])
        for device, (ox, oy) in inst.offsets_tracks.items():
            w, h = inst.sizes_tracks[device]
            result[str(device)] = (base_x + ox, base_y + oy, base_x + ox + int(w), base_y + oy + int(h))
    return dict(sorted(result.items()))


def _device_bboxes_tracks_from_positions(
    instances: Sequence[_PatternInstance],
    x_pos: Mapping[str, int],
    y_pos: Mapping[str, int],
) -> dict[str, tuple[int, int, int, int]]:
    result: dict[str, tuple[int, int, int, int]] = {}
    for inst in instances:
        base_x = int(x_pos[inst.spec.name])
        base_y = int(y_pos[inst.spec.name])
        for device, (ox, oy) in inst.offsets_tracks.items():
            w, h = inst.sizes_tracks[device]
            result[str(device)] = (base_x + ox, base_y + oy, base_x + ox + int(w), base_y + oy + int(h))
    return dict(sorted(result.items()))


def _shared_sd_terms_from_device_bboxes(
    pairs: Sequence[PairConstraintSpec],
    graph: TopologyGraph,
    device_bboxes: Mapping[str, tuple[int, int, int, int]],
) -> dict[str, int]:
    terms: dict[str, int] = {}
    for pair in pairs:
        if _shared_sd_connection_for_pair(pair, graph) is None:
            continue
        if pair.left not in device_bboxes or pair.right not in device_bboxes:
            continue
        left = tuple(int(v) for v in device_bboxes[pair.left])
        right = tuple(int(v) for v in device_bboxes[pair.right])
        center_manhattan = abs((left[0] + left[2]) - (right[0] + right[2])) + abs(
            (left[1] + left[3]) - (right[1] + right[3])
        )
        weight = max(1, int(getattr(pair, "shared_sd_weight", 0) or 8))
        terms[pair.name] = weight * center_manhattan
    return dict(sorted(terms.items()))


def _relation_choice_variants(
    spec: AnalogLayoutSpec,
    *,
    max_choice_count: int,
) -> tuple[tuple[AnalogLayoutSpec, Mapping[str, str], int], ...]:
    choice_rows: list[tuple[tuple[PatternRelationSpec, str, int], ...]] = []
    fixed_relations: list[tuple[int, PatternRelationSpec]] = []
    for index, relation in enumerate(spec.relations):
        candidates = tuple(dict.fromkeys(str(item).lower() for item in tuple(getattr(relation, "candidates", ()) or ()) if str(item)))
        if not candidates:
            fixed_relations.append((index, relation))
            continue
        row: list[tuple[PatternRelationSpec, str, int]] = []
        for kind in candidates:
            chosen = PatternRelationSpec(
                relation.source,
                relation.target,
                kind,
                relation.min_gap_um,
                relation.tolerance_um,
                relation.notes,
                relation.hard,
                relation.weight,
                (),
                {},
            )
            row.append((chosen, _relation_key(index, relation), int(relation.candidate_costs.get(kind, 0))))
        choice_rows.append(tuple(row))
    if not choice_rows:
        return ((spec, {}, 0),)

    variants: list[tuple[AnalogLayoutSpec, Mapping[str, str], int]] = []
    for selected in product(*choice_rows):
        if len(variants) >= max(1, int(max_choice_count)):
            break
        selected_by_index: dict[int, PatternRelationSpec] = {index: relation for index, relation in fixed_relations}
        selected_relations: dict[str, str] = {}
        choice_cost = 0
        choice_offset = 0
        for index, relation in enumerate(spec.relations):
            if tuple(getattr(relation, "candidates", ()) or ()):
                chosen, key, cost = selected[choice_offset]
                choice_offset += 1
                selected_by_index[index] = chosen
                selected_relations[key] = chosen.kind
                choice_cost += cost
        relations = tuple(selected_by_index[index] for index in sorted(selected_by_index))
        variants.append(
            (
                type(spec)(
                    block=spec.block,
                    patterns=spec.patterns,
                    pairs=spec.pairs,
                    relations=relations,
                    critical_nets=spec.critical_nets,
                    route_resources=spec.route_resources,
                    pack_constraints=spec.pack_constraints,
                    placement_windows=spec.placement_windows,
                    objective_terms=spec.objective_terms,
                    pcell_realization_groups=spec.pcell_realization_groups,
                    noncritical_router=spec.noncritical_router,
                    objective=spec.objective,
                    drc=spec.drc,
                    notes=spec.notes,
                ),
                selected_relations,
                choice_cost,
            )
        )
    return tuple(variants)


def _relation_choice_upper_bound(relations: Sequence[PatternRelationSpec]) -> int:
    total = 1
    for relation in relations:
        candidates = tuple(
            dict.fromkeys(
                str(item).lower()
                for item in tuple(getattr(relation, "candidates", ()) or ())
                if str(item)
            )
        )
        total *= max(1, len(candidates))
    return total


def _should_inline_relation_choices(
    relations: Sequence[PatternRelationSpec],
    relation_choice_upper_bound: int,
    *,
    max_candidate_count: int,
) -> bool:
    """Decide whether relation candidate choices belong inside the SMT problem.

    The old path enumerated relation-choice Cartesian products in Python. That is
    acceptable for a few hard architectural alternatives, but it is the wrong
    strategy for soft placement experience: it freezes each global packing hint
    before the solver sees the compactness objective and quickly produces
    arbitrary unsat/timeouts as the DSL gains more optional relations.

    Soft relation choices should therefore stay as Bool choices inside Z3, where
    they are optimized together with bbox, void, alignment, routing and pcell
    realization terms. Hard relation choices are still enumerated when the
    combinational space is large, because they genuinely define discrete
    topology alternatives.
    """

    candidate_relations = tuple(
        relation
        for relation in relations
        if tuple(getattr(relation, "candidates", ()) or ())
    )
    if not candidate_relations:
        return True
    if all(not bool(getattr(relation, "hard", True)) for relation in candidate_relations):
        return True
    return int(relation_choice_upper_bound) <= max(512, max(1, int(max_candidate_count)))


def _complete_spec(spec: AnalogLayoutSpec, graph: TopologyGraph) -> tuple[AnalogLayoutSpec, tuple[str, ...]]:
    covered = spec.device_to_pattern()
    missing = tuple(sorted(str(name) for name in graph.devices if str(name) not in covered))
    if not missing:
        return spec, ()
    additions = tuple(
        DevicePatternSpec(
            f"ungrouped_{name}",
            "ungrouped",
            (name,),
            "row",
            (),
            0.5,
            0.25,
        )
        for name in missing
    )
    return (
        type(spec)(
            block=spec.block,
            patterns=tuple(spec.patterns) + additions,
            pairs=spec.pairs,
            relations=spec.relations,
            critical_nets=spec.critical_nets,
            route_resources=spec.route_resources,
            pack_constraints=spec.pack_constraints,
            placement_windows=spec.placement_windows,
            objective_terms=spec.objective_terms,
            pcell_realization_groups=spec.pcell_realization_groups,
            noncritical_router=spec.noncritical_router,
            objective=spec.objective,
            drc=spec.drc,
            notes=spec.notes,
        ),
        missing,
    )


def _pattern_overlap_issues(bboxes: Mapping[str, tuple[int, int, int, int]]) -> tuple[str, ...]:
    names = tuple(sorted(bboxes))
    issues: list[str] = []
    for idx, left in enumerate(names):
        for right in names[idx + 1 :]:
            if _bbox_tracks_overlap(bboxes[left], bboxes[right]):
                issues.append(f"pattern overlap: {left} vs {right}")
    return tuple(issues)


def _bbox_tracks_overlap(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> bool:
    return left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]


def _pattern_candidates(
    pattern: DevicePatternSpec,
    *,
    pairs: Sequence[PairConstraintSpec] = (),
    drc: object | None = None,
) -> tuple[PatternCandidateSpec, ...]:
    candidates = pattern.candidates
    if candidates:
        base = tuple(_complete_candidate_order(pattern, item) for item in candidates)
    else:
        n = max(1, len(pattern.devices))
        kind = pattern.kind.lower()
        if kind == "column":
            rows, cols = n, 1
        elif kind in {"grid", "common_centroid_grid", "compact_grid"}:
            cols = max(1, int(ceil(sqrt(n))))
            rows = max(1, int(ceil(n / cols)))
        else:
            rows, cols = 1, n
        base = (
            _complete_candidate_order(
                pattern,
                PatternCandidateSpec(f"{kind}_{rows}x{cols}", rows, cols),
            ),
        )
    return _with_shared_sd_pattern_candidates(pattern, base, pairs, drc)


def _with_shared_sd_pattern_candidates(
    pattern: DevicePatternSpec,
    candidates: Sequence[PatternCandidateSpec],
    pairs: Sequence[PairConstraintSpec],
    drc: object | None,
) -> tuple[PatternCandidateSpec, ...]:
    """Add compact pair candidates for S/D sharing intent.

    Without a ready shared-S/D calibration contract this changes only the
    pattern-level spacing seen by SMT.  With a ready contract, the candidate is
    tagged as physical-capable for downstream realization/lowering reports.
    """

    result: list[PatternCandidateSpec] = list(candidates)
    shared_first: list[PatternCandidateSpec] = []
    if len(tuple(pattern.devices)) != 2:
        return tuple(result)
    device_set = set(str(dev) for dev in pattern.devices)
    pair = next(
        (
            item
            for item in pairs
            if bool(getattr(item, "shared_sd", False))
            and {str(item.left), str(item.right)} == device_set
        ),
        None,
    )
    if pair is None:
        return tuple(result)

    target_spacing = _shared_sd_spacing_um(pair, drc)
    physical_ready = _shared_sd_contract_allows_physical_merge(_shared_sd_readiness_contract(pair))
    suffixes = ("_physical_shared_sd", "_shared_sd") if physical_ready else ("_shared_sd",)
    for candidate in tuple(candidates):
        if not _candidate_places_pair_adjacent(candidate, pair):
            continue
        current_spacing = pattern.spacing_um if candidate.spacing_um is None else candidate.spacing_um
        if float(current_spacing) <= float(target_spacing):
            continue
        for suffix in suffixes:
            compact = PatternCandidateSpec(
                f"{candidate.name}{suffix}",
                candidate.rows,
                candidate.cols,
                candidate.order,
                float(target_spacing),
                candidate.margin_um,
                max(0, int(candidate.cost)) + (0 if suffix == "_physical_shared_sd" else int(physical_ready)),
            )
            if compact not in shared_first:
                shared_first.append(compact)
    ordered: list[PatternCandidateSpec] = []
    for item in (*shared_first, *result):
        if item not in ordered:
            ordered.append(item)
    return tuple(ordered)


def _shared_sd_readiness_contract(pair: PairConstraintSpec) -> dict[str, object]:
    raw = getattr(pair, "shared_sd_readiness", {}) or {}
    if isinstance(raw, Mapping):
        data = dict(raw)
    else:
        data = {}
    nested = data.get("readiness")
    if isinstance(nested, Mapping):
        merged = dict(nested)
        merged.update({key: value for key, value in data.items() if key != "readiness"})
        data = merged
    return {str(key): value for key, value in data.items()}


def _shared_sd_contract_allows_physical_merge(contract: Mapping[str, object]) -> bool:
    if not contract:
        return False
    status = str(contract.get("status", "") or "").strip().lower()
    mode = str(contract.get("solver_allowed_mode", "") or "").strip().lower()
    if status and status != "ready":
        return False
    if mode and mode != "physical_shared_diffusion":
        return False
    if not _bool_like(contract.get("physical_diffusion_merge_allowed", False)):
        return False
    if _bool_like(contract.get("lvs_required", False)) and _bool_like(contract.get("lvs_correct", None)) is not True:
        return False
    return True


def _shared_sd_candidate_name_is_shared(name: str) -> bool:
    value = str(name)
    return value.endswith("_shared_sd") or "_shared_sd_" in value or value.endswith("_physical_shared_sd")


def _shared_sd_candidate_name_is_physical(name: str) -> bool:
    return str(name).endswith("_physical_shared_sd")


def _bool_like(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "ready", "clean", "correct"}:
        return True
    if text in {"0", "false", "no", "n", "off", "blocked", "dirty", "incorrect"}:
        return False
    return None


def _candidate_places_pair_adjacent(candidate: PatternCandidateSpec, pair: PairConstraintSpec) -> bool:
    order = tuple(str(item) for item in candidate.order)
    if not order:
        return False
    try:
        left_index = order.index(str(pair.left))
        right_index = order.index(str(pair.right))
    except ValueError:
        return False
    if abs(left_index - right_index) != 1:
        return False
    if candidate.rows == 1:
        return left_index // max(1, candidate.cols) == right_index // max(1, candidate.cols)
    if candidate.cols == 1:
        return True
    return left_index // max(1, candidate.cols) == right_index // max(1, candidate.cols)


def _shared_sd_spacing_um(pair: PairConstraintSpec, drc: object | None) -> float:
    explicit = getattr(pair, "shared_sd_spacing_um", None)
    if explicit is not None:
        return max(0.0, float(explicit))
    role = str(getattr(pair, "role", "") or "")
    by_role = dict(getattr(drc, "pair_spacing_um_by_role", {}) or {}) if drc is not None else {}
    if role and role in by_role:
        return max(0.0, float(by_role[role]))
    try:
        return max(0.0, float(get_env("SHARED_SD_SPACING_UM", "0.5") or "0.5"))
    except ValueError:
        return 0.5


def _pattern_choice_count_by_pattern(
    patterns: Sequence[DevicePatternSpec],
    candidate_rows: Sequence[Sequence[_PatternChoice]],
) -> dict[str, int]:
    return {
        pattern.name: len(tuple(row))
        for pattern, row in zip(patterns, candidate_rows)
    }


def _candidate_combination_upper_bound(candidate_rows: Sequence[Sequence[_PatternChoice]]) -> int:
    total = 1
    for row in candidate_rows:
        total *= max(1, len(tuple(row)))
    return total


def _should_use_pattern_choice_smt(
    candidate_rows: Sequence[Sequence[_PatternChoice]],
    realization_groups: Sequence[PCellRealizationGroupSpec],
) -> bool:
    return bool(tuple(realization_groups)) and any(len(tuple(row)) > 1 for row in candidate_rows)


def _pattern_choices(
    pattern: DevicePatternSpec,
    realization_groups: Sequence[PCellRealizationGroupSpec],
    *,
    pairs: Sequence[PairConstraintSpec] = (),
    drc: object | None = None,
    max_choices: int,
) -> tuple[_PatternChoice, ...]:
    base_candidates = _pattern_candidates(pattern, pairs=pairs, drc=drc)
    relevant_groups = tuple(
        expanded
        for group in realization_groups
        for expanded in _expand_realization_group_for_pattern(group, pattern)
    )
    if not relevant_groups:
        return tuple(_PatternChoice(candidate, {}) for candidate in base_candidates)

    group_rows: list[tuple[tuple[PCellRealizationGroupSpec, PCellRealizationCandidateSpec], ...]] = []
    for group in relevant_groups:
        legal = tuple(
            candidate
            for candidate in group.candidates
            if bool(candidate.drc_clean)
            and bool(candidate.lvs_clean)
            and float(candidate.width_um) > 0.0
            and float(candidate.height_um) > 0.0
        )
        if not legal:
            continue
        group_rows.append(tuple((group, candidate) for candidate in legal))
    if not group_rows:
        return tuple(_PatternChoice(candidate, {}) for candidate in base_candidates)

    choices: list[_PatternChoice] = []
    device_set = set(pattern.devices)
    for base in base_candidates:
        for selected in product(*group_rows):
            realizations: dict[str, PCellRealizationCandidateSpec] = {}
            suffix_parts: list[str] = []
            realization_cost = 0
            for group, candidate in selected:
                affected = tuple(device for device in group.devices if device in device_set)
                if not affected:
                    continue
                suffix_parts.append(f"{group.name}={candidate.name}")
                realization_cost += _pcell_candidate_objective_cost(candidate)
                for device in affected:
                    realizations[str(device)] = candidate
            if not realizations:
                choices.append(_PatternChoice(base, {}))
                continue
            if not _physical_shared_sd_choice_is_realizable(pattern, base, realizations, pairs):
                continue
            choices.append(
                _PatternChoice(
                    PatternCandidateSpec(
                        base.name,
                        base.rows,
                        base.cols,
                        base.order,
                        base.spacing_um,
                        base.margin_um,
                        int(base.cost) + realization_cost,
                    ),
                    realizations,
                )
            )
            if len(choices) >= max(1, int(max_choices)):
                return tuple(choices)
    return tuple(choices or (_PatternChoice(candidate, {}) for candidate in base_candidates))


def _physical_shared_sd_choice_is_realizable(
    pattern: DevicePatternSpec,
    candidate: PatternCandidateSpec,
    realizations: Mapping[str, PCellRealizationCandidateSpec],
    pairs: Sequence[PairConstraintSpec],
) -> bool:
    """Gate a physical shared-S/D packing choice by its selected PCell shape.

    A readiness record qualifies one calibrated abutted template, not every
    electrically equivalent MOS finger split.  Keeping this test beside the
    pattern/realization Cartesian product makes PCell selection and physical
    sharing one joint SMT-domain decision.  A proximity-only ``*_shared_sd``
    candidate remains available when the calibrated template does not match.
    """

    if not _shared_sd_candidate_name_is_physical(candidate.name):
        return True
    device_set = set(str(device) for device in pattern.devices)
    pair = next(
        (
            item
            for item in pairs
            if bool(getattr(item, "shared_sd", False))
            and {str(item.left), str(item.right)} == device_set
        ),
        None,
    )
    if pair is None:
        return False
    contract = _shared_sd_readiness_contract(pair)
    expected = dict(
        contract.get(
            "compatible_instance_params",
            contract.get("template_instance_params", contract.get("instance_params", {})),
        )
        or {}
    )
    if not expected:
        return True
    for device in (str(pair.left), str(pair.right)):
        realization = realizations.get(device)
        if realization is None or not _shared_sd_realization_matches_template(realization, expected):
            return False
    return True


def _shared_sd_realization_matches_template(
    realization: PCellRealizationCandidateSpec,
    expected: Mapping[str, object],
) -> bool:
    sizing = dict(getattr(realization, "sizing_overrides", {}) or {})
    pcell = dict(getattr(realization, "pcell_overrides", {}) or {})
    unit_array = dict(sizing.get("mos_unit_array", {}) or {})
    actual: dict[str, object] = {
        "Wfg": unit_array.get("unit_finger_width_m", pcell.get("Wfg", sizing.get("wf"))),
        "l": unit_array.get("unit_length_m", pcell.get("l", sizing.get("L", sizing.get("l")))),
        "fingers": unit_array.get("unit_nf", pcell.get("fingers", sizing.get("nf"))),
        "simM": unit_array.get("unit_m", pcell.get("simM", sizing.get("m"))),
    }
    aliases = {"wf": "Wfg", "wfg": "Wfg", "length": "l", "nf": "fingers", "m": "simM"}
    for raw_key, expected_value in expected.items():
        key = aliases.get(str(raw_key).lower(), str(raw_key))
        actual_value = actual.get(key, pcell.get(str(raw_key), sizing.get(str(raw_key))))
        if actual_value is None:
            return False
        try:
            lhs = float(actual_value)
            rhs = float(expected_value)
            if abs(lhs - rhs) > max(1e-15, abs(rhs) * 1e-6):
                return False
        except (TypeError, ValueError):
            if str(actual_value) != str(expected_value):
                return False
    return True


def _pcell_candidate_objective_cost(candidate: PCellRealizationCandidateSpec) -> int:
    metadata = dict(getattr(candidate, "metadata", {}) or {})
    total = max(0, int(candidate.cost))
    for key in (
        "shape_cost",
        "aspect_cost",
        "topology_cost",
        "array_cost",
        "regularity_cost",
        "route_access_cost",
        "fragmentation_cost",
        "pin_access_cost",
        "dfm_cost",
    ):
        try:
            total += max(0, int(metadata.get(key, 0) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _expand_realization_group_for_pattern(
    group: PCellRealizationGroupSpec,
    pattern: DevicePatternSpec,
) -> tuple[PCellRealizationGroupSpec, ...]:
    device_set = set(pattern.devices)
    affected = tuple(device for device in group.devices if device in device_set)
    if not affected:
        return ()
    if bool(group.require_same) or len(affected) == 1:
        return (
            PCellRealizationGroupSpec(
                group.name,
                affected,
                group.candidates,
                True,
                group.notes,
            ),
        )
    return tuple(
        PCellRealizationGroupSpec(
            f"{group.name}_{device}",
            (device,),
            group.candidates,
            True,
            group.notes,
        )
        for device in affected
    )


def _complete_candidate_order(pattern: DevicePatternSpec, candidate: PatternCandidateSpec) -> PatternCandidateSpec:
    order = tuple(str(dev) for dev in candidate.order if str(dev) in set(pattern.devices))
    remaining = tuple(str(dev) for dev in pattern.devices if str(dev) not in order)
    order = order + remaining
    if pattern.center_device and pattern.center_device in order:
        devices = [dev for dev in order if dev != pattern.center_device]
        center_index = min(len(devices), max(0, (candidate.rows * candidate.cols) // 2))
        devices.insert(center_index, pattern.center_device)
        order = tuple(devices)
    return PatternCandidateSpec(
        candidate.name,
        candidate.rows,
        candidate.cols,
        order,
        candidate.spacing_um,
        candidate.margin_um,
        candidate.cost,
    )


def _pattern_instance(
    pattern: DevicePatternSpec,
    candidate: PatternCandidateSpec,
    device_sizes_um: DeviceSizeMap,
    pitch: float,
    *,
    realizations: Mapping[str, PCellRealizationCandidateSpec] | None = None,
) -> _PatternInstance:
    order = _complete_candidate_order(pattern, candidate).order
    if len(order) > candidate.rows * candidate.cols:
        raise ValueError(f"candidate {candidate.name} for pattern {pattern.name} has too few sites")
    spacing = _um_to_tracks(pattern.spacing_um if candidate.spacing_um is None else candidate.spacing_um, pitch)
    margin = _um_to_tracks(pattern.margin_um if candidate.margin_um is None else candidate.margin_um, pitch)
    realization_map = {str(name): value for name, value in dict(realizations or {}).items()}
    sizes = {name: _size_tracks(name, device_sizes_um, pitch, realizations=realization_map) for name in order}
    cell_w = max((wh[0] for wh in sizes.values()), default=1)
    cell_h = max((wh[1] for wh in sizes.values()), default=1)
    width_tracks = max(1, 2 * margin + candidate.cols * cell_w + max(0, candidate.cols - 1) * spacing)
    height_tracks = max(1, 2 * margin + candidate.rows * cell_h + max(0, candidate.rows - 1) * spacing)
    offsets: dict[str, tuple[int, int]] = {}
    for idx, name in enumerate(order):
        row = idx // candidate.cols
        col = idx % candidate.cols
        w, h = sizes[name]
        offsets[name] = (
            margin + col * (cell_w + spacing) + max(0, (cell_w - w) // 2),
            margin + row * (cell_h + spacing) + max(0, (cell_h - h) // 2),
        )
    return _PatternInstance(pattern, candidate, order, width_tracks, height_tracks, offsets, sizes, realization_map)


def _device_center_exprs(
    instances: Sequence[_PatternInstance],
    x: Mapping[str, object],
    y: Mapping[str, object],
) -> dict[str, tuple[object, object]]:
    result: dict[str, tuple[object, object]] = {}
    for inst in instances:
        px = x[inst.spec.name]
        py = y[inst.spec.name]
        for dev, (ox, oy) in inst.offsets_tracks.items():
            w, h = inst.sizes_tracks[dev]
            result[dev] = (2 * (px + ox) + w, 2 * (py + oy) + h)
    return result


def _device_center_choice_exprs(
    row_instances: Mapping[str, Sequence[_PatternInstance]],
    choice_vars: Mapping[str, object],
    x: Mapping[str, object],
    y: Mapping[str, object],
) -> dict[str, tuple[object, object]]:
    result: dict[str, tuple[object, object]] = {}
    for pattern_name, instances in row_instances.items():
        if pattern_name not in choice_vars or pattern_name not in x or pattern_name not in y:
            continue
        var = choice_vars[pattern_name]
        px = x[pattern_name]
        py = y[pattern_name]
        devices = tuple(
            dict.fromkeys(
                device
                for inst in instances
                for device in inst.order
            )
        )
        for dev in devices:
            cx_values: list[object] = []
            cy_values: list[object] = []
            for inst in instances:
                if dev not in inst.offsets_tracks or dev not in inst.sizes_tracks:
                    continue
                ox, oy = inst.offsets_tracks[dev]
                w, h = inst.sizes_tracks[dev]
                cx_values.append(2 * (px + ox) + w)
                cy_values.append(2 * (py + oy) + h)
            if cx_values and cy_values:
                result[dev] = (_choice_int_expr(var, tuple(cx_values)), _choice_int_expr(var, tuple(cy_values)))
    return result


def _placements_from_model(
    spec: AnalogLayoutSpec,
    instances: Sequence[_PatternInstance],
    x: Mapping[str, object],
    y: Mapping[str, object],
    model: object,
    pitch: float,
) -> dict[str, Placement]:
    pair_right_orient: dict[str, str] = {}
    pair_left_orient: dict[str, str] = {}
    for pair in spec.pairs:
        pair_left_orient[pair.left] = "R0"
        if pair.mirror_right:
            pair_right_orient[pair.right] = "MY"
    placements: dict[str, Placement] = {}
    for inst in instances:
        base_x = _model_int(model, x[inst.spec.name])
        base_y = _model_int(model, y[inst.spec.name])
        for dev in inst.order:
            ox, oy = inst.offsets_tracks[dev]
            orient = pair_right_orient.get(dev, pair_left_orient.get(dev, inst.spec.orient))
            placements[dev] = Placement(
                dev,
                round((base_x + ox) * pitch, 6),
                round((base_y + oy) * pitch, 6),
                orient=orient,
                role=inst.spec.role,
            )
    return placements


def _placements_from_positions(
    spec: AnalogLayoutSpec,
    instances: Sequence[_PatternInstance],
    x_pos: Mapping[str, int],
    y_pos: Mapping[str, int],
    pitch: float,
) -> dict[str, Placement]:
    pair_right_orient: dict[str, str] = {}
    pair_left_orient: dict[str, str] = {}
    for pair in spec.pairs:
        pair_left_orient[pair.left] = "R0"
        if pair.mirror_right:
            pair_right_orient[pair.right] = "MY"
    placements: dict[str, Placement] = {}
    for inst in instances:
        base_x = int(x_pos[inst.spec.name])
        base_y = int(y_pos[inst.spec.name])
        for dev in inst.order:
            ox, oy = inst.offsets_tracks[dev]
            orient = pair_right_orient.get(dev, pair_left_orient.get(dev, inst.spec.orient))
            placements[dev] = Placement(
                dev,
                round((base_x + ox) * pitch, 6),
                round((base_y + oy) * pitch, 6),
                orient=orient,
                role=inst.spec.role,
            )
    return placements


def _hpwl_by_net_from_positions(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
    instances: Sequence[_PatternInstance],
    x_pos: Mapping[str, int],
    y_pos: Mapping[str, int],
) -> dict[str, int]:
    centers: dict[str, tuple[int, int]] = {}
    for inst in instances:
        for dev, (ox, oy) in inst.offsets_tracks.items():
            w, h = inst.sizes_tracks[dev]
            centers[dev] = (2 * (int(x_pos[inst.spec.name]) + ox) + w, 2 * (int(y_pos[inst.spec.name]) + oy) + h)
    result: dict[str, int] = {}
    for net_spec in spec.critical_nets:
        if not net_spec.route_in_smt or net_spec.name not in graph.nets:
            continue
        pts = [centers[terminal.device] for terminal in graph.nets[net_spec.name].terminals if terminal.device in centers]
        if len(pts) < 2:
            continue
        xs = [pt[0] for pt in pts]
        ys = [pt[1] for pt in pts]
        result[net_spec.name] = max(1, int(net_spec.weight)) * ((max(xs) - min(xs)) + (max(ys) - min(ys)))
    return result


def _verify_relation_positions_direct(
    spec: AnalogLayoutSpec,
    instances: Sequence[_PatternInstance],
    x_pos: Mapping[str, int],
    y_pos: Mapping[str, int],
    pitch: float,
) -> bool:
    width = {inst.spec.name: int(inst.width_tracks) for inst in instances}
    height = {inst.spec.name: int(inst.height_tracks) for inst in instances}
    bboxes = {
        name: (
            int(x_pos[name]),
            int(y_pos[name]),
            int(x_pos[name]) + width[name],
            int(y_pos[name]) + height[name],
        )
        for name in width
        if name in x_pos and name in y_pos
    }
    if len(bboxes) != len(width):
        return False
    if _pattern_overlap_issues(bboxes):
        return False
    for relation in spec.relations:
        if relation.source not in x_pos or relation.target not in x_pos:
            continue
        if not bool(getattr(relation, "hard", True)) or tuple(getattr(relation, "candidates", ()) or ()):
            continue
        gap = _um_to_tracks(relation.min_gap_um, pitch)
        tol = _um_to_tracks(relation.tolerance_um, pitch)
        source, target = relation.source, relation.target
        kind = relation.kind.lower()
        sx, sy = int(x_pos[source]), int(y_pos[source])
        tx, ty = int(x_pos[target]), int(y_pos[target])
        sw, sh = width[source], height[source]
        tw, th = width[target], height[target]
        if kind in {"right_of", "source_left_of_target"}:
            if tx < sx + sw + gap:
                return False
        elif kind in {"left_of", "source_right_of_target"}:
            if sx < tx + tw + gap:
                return False
        elif kind in {"above", "source_below_target"}:
            if ty < sy + sh + gap:
                return False
        elif kind in {"below", "source_above_target"}:
            if sy < ty + th + gap:
                return False
        elif kind in {"align_x", "align_center_x", "same_center_x"}:
            if abs((2 * sx + sw) - (2 * tx + tw)) > max(0, 2 * tol):
                return False
        elif kind in {"align_y", "align_center_y", "same_center_y"}:
            if abs((2 * sy + sh) - (2 * ty + th)) > max(0, 2 * tol):
                return False
        elif kind == "overlap_x":
            if not (sx < tx + tw and tx < sx + sw):
                return False
        elif kind == "overlap_y":
            if not (sy < ty + th and ty < sy + sh):
                return False
    return True


def _verify_relation_positions_with_z3(
    spec: AnalogLayoutSpec,
    instances: Sequence[_PatternInstance],
    x_pos: Mapping[str, int],
    y_pos: Mapping[str, int],
    pitch: float,
    *,
    solver_timeout_ms: int | None,
) -> bool:
    if z3 is None:  # pragma: no cover
        return False
    solver = z3.Solver()
    _configure_z3_solver_options(solver, _positive_int_or_default(solver_timeout_ms, 15_000))
    x = {inst.spec.name: z3.Int(f"als_verify_x__{inst.spec.name}") for inst in instances}
    y = {inst.spec.name: z3.Int(f"als_verify_y__{inst.spec.name}") for inst in instances}
    width = {inst.spec.name: int(inst.width_tracks) for inst in instances}
    height = {inst.spec.name: int(inst.height_tracks) for inst in instances}
    for name in x:
        solver.add(x[name] == int(x_pos[name]), y[name] == int(y_pos[name]))
    names = tuple(x)
    for left_idx, left in enumerate(names):
        for right in names[left_idx + 1 :]:
            solver.add(
                z3.Or(
                    x[left] + width[left] <= x[right],
                    x[right] + width[right] <= x[left],
                    y[left] + height[left] <= y[right],
                    y[right] + height[right] <= y[left],
                )
            )
    for relation in spec.relations:
        if relation.source not in x or relation.target not in x:
            continue
        if not bool(getattr(relation, "hard", True)) or tuple(getattr(relation, "candidates", ()) or ()):
            continue
        gap = _um_to_tracks(relation.min_gap_um, pitch)
        tol = _um_to_tracks(relation.tolerance_um, pitch)
        source, target = relation.source, relation.target
        kind = relation.kind.lower()
        if kind in {"right_of", "source_left_of_target"}:
            solver.add(x[target] >= x[source] + width[source] + gap)
        elif kind in {"left_of", "source_right_of_target"}:
            solver.add(x[source] >= x[target] + width[target] + gap)
        elif kind in {"above", "source_below_target"}:
            solver.add(y[target] >= y[source] + height[source] + gap)
        elif kind in {"below", "source_above_target"}:
            solver.add(y[source] >= y[target] + height[target] + gap)
        elif kind in {"align_x", "align_center_x", "same_center_x"}:
            solver.add(_z3_abs((2 * x[source] + width[source]) - (2 * x[target] + width[target])) <= max(0, 2 * tol))
        elif kind in {"align_y", "align_center_y", "same_center_y"}:
            solver.add(_z3_abs((2 * y[source] + height[source]) - (2 * y[target] + height[target])) <= max(0, 2 * tol))
        elif kind == "overlap_x":
            solver.add(x[source] < x[target] + width[target], x[target] < x[source] + width[source])
        elif kind == "overlap_y":
            solver.add(y[source] < y[target] + height[target], y[target] < y[source] + height[source])
    return solver.check() == z3.sat


def _positive_int_or_default(value: object, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _raise_to(values: dict[str, int], name: str, target: int) -> bool:
    target = max(0, int(target))
    if values[name] >= target:
        return False
    values[name] = target
    return True


def _resolve_overlaps_greedily(
    x_pos: dict[str, int],
    y_pos: dict[str, int],
    width: Mapping[str, int],
    height: Mapping[str, int],
    spacing_tracks: int,
) -> bool:
    changed = False
    names = tuple(sorted(x_pos))
    spacing = max(0, int(spacing_tracks))
    for left_idx, left in enumerate(names):
        for right in names[left_idx + 1 :]:
            if not _bbox_tracks_overlap(
                (
                    x_pos[left] - spacing,
                    y_pos[left] - spacing,
                    x_pos[left] + int(width[left]) + spacing,
                    y_pos[left] + int(height[left]) + spacing,
                ),
                (
                    x_pos[right],
                    y_pos[right],
                    x_pos[right] + int(width[right]),
                    y_pos[right] + int(height[right]),
                ),
            ):
                continue
            push_x = x_pos[left] + int(width[left]) + spacing
            push_y = y_pos[left] + int(height[left]) + spacing
            if push_x - x_pos[right] <= push_y - y_pos[right]:
                changed |= _raise_to(x_pos, right, push_x)
            else:
                changed |= _raise_to(y_pos, right, push_y)
    return changed


def _resolved_spacing_um(spec: AnalogLayoutSpec, placement_spacing_um: float | None, pitch: float) -> float:
    if placement_spacing_um is not None:
        return max(float(placement_spacing_um), pitch)
    if spec.drc.placement_spacing_um is not None:
        return max(float(spec.drc.placement_spacing_um), pitch)
    return max(0.5, pitch)


def _size_tracks(
    name: str,
    device_sizes_um: DeviceSizeMap,
    pitch: float,
    *,
    realizations: Mapping[str, PCellRealizationCandidateSpec] | None = None,
) -> tuple[int, int]:
    realization = dict(realizations or {}).get(str(name))
    if realization is not None:
        width, height = float(realization.width_um), float(realization.height_um)
    else:
        try:
            width, height = device_sizes_um[name]
        except (KeyError, TypeError, ValueError):
            width, height = 1.0, 1.0
    return max(1, _um_to_tracks(float(width), pitch)), max(1, _um_to_tracks(float(height), pitch))


def _um_to_tracks(value_um: float | int | None, pitch: float) -> int:
    if value_um is None:
        value_um = 0.0
    return max(0, int(ceil(max(float(value_um), 0.0) / max(pitch, 1e-6))))


def _model_int(model: object, expr: object) -> int:
    value = model.eval(expr, model_completion=True)
    try:
        return int(value.as_long())
    except AttributeError:
        return int(str(value))


def _choice_int_expr(choice_var: object, values: Sequence[object]) -> object:
    rows = tuple(values)
    if not rows:
        return z3.IntVal(0)
    expr = rows[-1]
    for index in range(len(rows) - 2, -1, -1):
        expr = z3.If(choice_var == index, rows[index], expr)
    return expr


def _bounded_index(value: int, size: int) -> int:
    if size <= 0:
        return 0
    return max(0, min(int(value), int(size) - 1))


def _z3_abs(expr: object) -> object:
    return z3.If(expr >= 0, expr, -expr)


def _z3_pos(expr: object) -> object:
    return z3.If(expr >= 0, expr, 0)
