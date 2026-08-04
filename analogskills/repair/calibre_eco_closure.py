"""Calibre-driven iterative ECO closure orchestration.

The operators in :mod:`analogskills.repair.drc_eco_solver` and
:mod:`analogskills.repair.drc_lvs` are intentionally local: they create one safe
patch from the current marker database.  This module adds the missing closure
layer around them:

1. classify the current Calibre markers;
2. choose the next safe ECO stage;
3. return an append-only OA patch;
4. let the caller apply the patch, stream out, rerun Calibre, and repeat.

The module deliberately does not pretend that a marker is fixed until a fresh
Calibre run proves it.  That rule is what made the manual LDO closure converge:
M10 local fill, rerun, redundant VIA9, rerun, post-via M9/M10 fill, rerun.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

from analogskills.artifacts import ArtifactRef, Checkpoint, CheckpointJournal

from .calibre_closure import (
    classify_calibre_markers_for_local_repair,
    summarize_calibre_marker_repair_classes,
)
from .drc_eco_solver import build_local_routing_drc_eco_patch, local_drc_eco_patch_summary
from .drc_lvs import DrcIssue, localize_drc_issues_to_layout, plan_safe_redundant_via_neighbor_patch


@dataclass(frozen=True)
class CalibreEcoClosureStage:
    """A single ECO stage available to the closure loop."""

    name: str
    kind: str
    config: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CalibreEcoClosurePatchDecision:
    """The next closure action selected from the current Calibre database."""

    stage: CalibreEcoClosureStage | None
    patch: object | None
    marker_count: int
    routing_gating_rule_counts: Mapping[str, int] = field(default_factory=dict)
    nonrouting_signoff_rule_counts: Mapping[str, int] = field(default_factory=dict)
    marker_class_summary: Mapping[str, object] = field(default_factory=dict)
    summary: Mapping[str, object] = field(default_factory=dict)
    reason: str = ""

    @property
    def patch_available(self) -> bool:
        return self.patch is not None

    @property
    def edit_count(self) -> int:
        return int(dict(self.summary).get("edit_count", dict(self.summary).get("accepted_via_count", 0)) or 0)


@dataclass(frozen=True)
class CalibreEcoClosureIteration:
    """One apply/rerun iteration in an ECO closure loop."""

    index: int
    decision: CalibreEcoClosurePatchDecision
    before_routing_gating_rule_counts: Mapping[str, int] = field(default_factory=dict)
    after_routing_gating_rule_counts: Mapping[str, int] = field(default_factory=dict)
    accepted: bool = False
    acceptance_reason: str = ""
    artifacts: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CalibreEcoClosureLoopResult:
    """Result of a bounded iterative ECO closure loop."""

    final_plan: object
    final_results: tuple[object, ...]
    iterations: tuple[CalibreEcoClosureIteration, ...]
    converged: bool = False
    reason: str = ""
    marker_class_summary: Mapping[str, object] = field(default_factory=dict)
    checkpoints: tuple[Checkpoint, ...] = ()

    @property
    def applied_iteration_count(self) -> int:
        return sum(1 for row in self.iterations if row.accepted)


def build_next_calibre_eco_closure_patch(
    oa_plan: object,
    calibre_results: Iterable[object],
    *,
    pdk: object | None = None,
    config: Mapping[str, object] | None = None,
) -> CalibreEcoClosurePatchDecision:
    """Pick the next ECO patch for the current Calibre marker database.

    This function is intentionally one-step.  The caller must apply the patch
    and rerun Calibre before asking for the next patch; otherwise the loop will
    chase stale markers and can easily oscillate.
    """

    results = tuple(calibre_results)
    cfg = _closure_config(pdk, config)
    classification_config = _classification_config(cfg)
    marker_class_summary = summarize_calibre_marker_repair_classes(results, config=classification_config)
    routing_counts = routing_gating_rule_counts(results, config=classification_config)
    nonrouting_counts = nonrouting_signoff_rule_counts(results, config=classification_config)
    if not routing_counts:
        return CalibreEcoClosurePatchDecision(
            None,
            None,
            len(results),
            routing_gating_rule_counts=routing_counts,
            nonrouting_signoff_rule_counts=nonrouting_counts,
            marker_class_summary=marker_class_summary,
            summary={"patch_available": False, "edit_count": 0},
            reason="no_routing_gating_markers",
        )

    for stage in _closure_stages(cfg):
        if stage.kind == "local_routing_drc_eco":
            decision = _build_local_routing_stage_decision(
                oa_plan,
                results,
                pdk=pdk,
                stage=stage,
                marker_class_summary=marker_class_summary,
                routing_counts=routing_counts,
                nonrouting_counts=nonrouting_counts,
            )
        elif stage.kind == "safe_redundant_via_neighbor":
            decision = _build_safe_redundant_via_stage_decision(
                oa_plan,
                results,
                pdk=pdk,
                stage=stage,
                marker_class_summary=marker_class_summary,
                routing_counts=routing_counts,
                nonrouting_counts=nonrouting_counts,
            )
        else:
            decision = CalibreEcoClosurePatchDecision(
                stage,
                None,
                len(results),
                routing_gating_rule_counts=routing_counts,
                nonrouting_signoff_rule_counts=nonrouting_counts,
                marker_class_summary=marker_class_summary,
                summary={"patch_available": False, "edit_count": 0, "stage_kind": stage.kind},
                reason="unsupported_stage_kind",
            )
        if decision.patch_available:
            return decision

    return CalibreEcoClosurePatchDecision(
        None,
        None,
        len(results),
        routing_gating_rule_counts=routing_counts,
        nonrouting_signoff_rule_counts=nonrouting_counts,
        marker_class_summary=marker_class_summary,
        summary={"patch_available": False, "edit_count": 0},
        reason="no_safe_eco_patch_available",
    )


def run_calibre_eco_closure_loop(
    initial_plan: object,
    initial_results: Iterable[object],
    *,
    pdk: object | None = None,
    config: Mapping[str, object] | None = None,
    apply_patch_and_verify: Callable[[CalibreEcoClosurePatchDecision, object, int], Mapping[str, object]] | None = None,
    checkpoint_journal: CheckpointJournal | None = None,
    parent_checkpoint_id: str = "",
) -> CalibreEcoClosureLoopResult:
    """Run a bounded ECO closure loop around external apply/Calibre callbacks.

    ``apply_patch_and_verify`` is the process-specific boundary.  It should:

    - append ``decision.patch`` to the current OA cellview;
    - stream out GDS;
    - rerun Calibre DRC/LVS as needed;
    - return at least ``{"results": <new Calibre DRC rows>}``.

    It may also return ``plan`` and ``artifacts``.  If no callback is supplied,
    the function stops as soon as a patch is ready and reports that verification
    is required.
    """

    cfg = _closure_config(pdk, config)
    max_iterations = max(int(cfg.get("max_iterations", 1) or 1), 1)
    current_plan = initial_plan
    current_results = tuple(initial_results)
    iterations: list[CalibreEcoClosureIteration] = []
    checkpoints: list[Checkpoint] = []
    active_parent_checkpoint_id = str(parent_checkpoint_id)
    history: set[tuple[tuple[tuple[str, int], ...], str]] = set()

    for index in range(1, max_iterations + 1):
        decision = build_next_calibre_eco_closure_patch(current_plan, current_results, pdk=pdk, config=cfg)
        before_counts = dict(decision.routing_gating_rule_counts)
        if not before_counts:
            return CalibreEcoClosureLoopResult(
                current_plan,
                current_results,
                tuple(iterations),
                converged=True,
                reason="routing_eco_converged",
                marker_class_summary=decision.marker_class_summary,
                checkpoints=tuple(checkpoints),
            )
        if decision.patch is None:
            iterations.append(
                CalibreEcoClosureIteration(
                    index,
                    decision,
                    before_routing_gating_rule_counts=before_counts,
                    accepted=False,
                    acceptance_reason=decision.reason,
                )
            )
            return CalibreEcoClosureLoopResult(
                current_plan,
                current_results,
                tuple(iterations),
                converged=False,
                reason=decision.reason,
                marker_class_summary=decision.marker_class_summary,
                checkpoints=tuple(checkpoints),
            )
        if apply_patch_and_verify is None:
            iterations.append(
                CalibreEcoClosureIteration(
                    index,
                    decision,
                    before_routing_gating_rule_counts=before_counts,
                    accepted=False,
                    acceptance_reason="verification_callback_required",
                )
            )
            return CalibreEcoClosureLoopResult(
                current_plan,
                current_results,
                tuple(iterations),
                converged=False,
                reason="patch_ready_verification_callback_required",
                marker_class_summary=decision.marker_class_summary,
                checkpoints=tuple(checkpoints),
            )

        signature = (_marker_signature(current_results), str(decision.stage.kind if decision.stage else ""))
        if signature in history:
            iterations.append(
                CalibreEcoClosureIteration(
                    index,
                    decision,
                    before_routing_gating_rule_counts=before_counts,
                    accepted=False,
                    acceptance_reason="repeated_marker_stage_signature",
                )
            )
            return CalibreEcoClosureLoopResult(
                current_plan,
                current_results,
                tuple(iterations),
                converged=False,
                reason="repeated_marker_stage_signature",
                marker_class_summary=decision.marker_class_summary,
                checkpoints=tuple(checkpoints),
            )
        history.add(signature)

        verification = dict(apply_patch_and_verify(decision, current_plan, index) or {})
        next_results = tuple(verification.get("results", ()) or ())
        if not next_results and verification.get("results") is None:
            raise ValueError("apply_patch_and_verify must return a 'results' iterable")
        if "plan" in verification and verification["plan"] is not None:
            candidate_plan = verification["plan"]
        else:
            candidate_plan = _merge_oa_plans(current_plan, decision.patch, pdk=pdk)
        after_counts = routing_gating_rule_counts(next_results, config=_classification_config(cfg))
        accepted = bool(verification.get("accepted", True))
        acceptance_reason = str(verification.get("acceptance_reason", "applied_and_calibre_rerun"))
        verification_artifacts = dict(verification.get("artifacts", {}) or {})
        checkpoint = Checkpoint(
            name=f"calibre-eco-{index:03d}",
            stage=str(decision.stage.kind if decision.stage else "calibre_eco"),
            parent_checkpoint_id=active_parent_checkpoint_id,
            layout_patch={"decision": calibre_eco_closure_decision_summary(decision)},
            repair_actions=(
                {
                    "stage": str(decision.stage.name if decision.stage else ""),
                    "kind": str(decision.stage.kind if decision.stage else ""),
                    "edit_count": decision.edit_count,
                },
            ),
            artifacts=tuple(item for item in verification_artifacts.values() if isinstance(item, ArtifactRef)),
            metrics={
                "before_routing_marker_count": float(sum(before_counts.values())),
                "after_routing_marker_count": float(sum(after_counts.values())),
            },
            accepted=accepted,
            notes=acceptance_reason,
        )
        checkpoints.append(checkpoint)
        if checkpoint_journal is not None:
            checkpoint_journal.append(checkpoint)
        active_parent_checkpoint_id = checkpoint.checkpoint_id
        iterations.append(
            CalibreEcoClosureIteration(
                index,
                decision,
                before_routing_gating_rule_counts=before_counts,
                after_routing_gating_rule_counts=after_counts,
                accepted=accepted,
                acceptance_reason=acceptance_reason,
                artifacts=verification_artifacts,
            )
        )
        if not accepted:
            return CalibreEcoClosureLoopResult(
                current_plan,
                current_results,
                tuple(iterations),
                converged=False,
                reason=acceptance_reason,
                marker_class_summary=decision.marker_class_summary,
                checkpoints=tuple(checkpoints),
            )
        current_plan = candidate_plan
        current_results = next_results

    final_summary = summarize_calibre_marker_repair_classes(current_results, config=_classification_config(cfg))
    final_counts = routing_gating_rule_counts(current_results, config=_classification_config(cfg))
    return CalibreEcoClosureLoopResult(
        current_plan,
        current_results,
        tuple(iterations),
        converged=not bool(final_counts),
        reason="routing_eco_converged" if not final_counts else "max_iterations_reached",
        marker_class_summary=final_summary,
        checkpoints=tuple(checkpoints),
    )


def routing_gating_rule_counts(
    calibre_results: Iterable[object],
    *,
    config: Mapping[str, object] | None = None,
) -> dict[str, int]:
    """Count Calibre DRC markers owned by routing ECO closure."""

    rows = tuple(calibre_results)
    classifications = classify_calibre_markers_for_local_repair(rows, config=config)
    counts: Counter[str] = Counter()
    for row, classification in zip(rows, classifications):
        if not classification.signoff_gated:
            continue
        if classification.owner != "routing":
            continue
        if classification.repair_class not in {"local_auto_repair", "local_smt_repair", "manual_review"}:
            continue
        rule = str(getattr(row, "rule", row if isinstance(row, str) else ""))
        if rule:
            counts[rule] += 1
    return dict(sorted(counts.items()))


def nonrouting_signoff_rule_counts(
    calibre_results: Iterable[object],
    *,
    config: Mapping[str, object] | None = None,
) -> dict[str, int]:
    """Count signoff-gated markers that are outside the routing ECO loop."""

    rows = tuple(calibre_results)
    classifications = classify_calibre_markers_for_local_repair(rows, config=config)
    counts: Counter[str] = Counter()
    for row, classification in zip(rows, classifications):
        if not classification.signoff_gated:
            continue
        if classification.owner == "routing":
            continue
        rule = str(getattr(row, "rule", row if isinstance(row, str) else ""))
        if rule:
            counts[rule] += 1
    return dict(sorted(counts.items()))


def calibre_eco_closure_decision_summary(decision: CalibreEcoClosurePatchDecision) -> dict[str, object]:
    """Return a JSON-serializable summary of a closure decision."""

    return {
        "stage": None if decision.stage is None else {
            "name": decision.stage.name,
            "kind": decision.stage.kind,
        },
        "patch_available": decision.patch_available,
        "edit_count": decision.edit_count,
        "marker_count": decision.marker_count,
        "routing_gating_rule_counts": dict(decision.routing_gating_rule_counts),
        "nonrouting_signoff_rule_counts": dict(decision.nonrouting_signoff_rule_counts),
        "marker_class_summary": dict(decision.marker_class_summary),
        "summary": dict(decision.summary),
        "reason": decision.reason,
    }


def calibre_eco_closure_loop_summary(result: CalibreEcoClosureLoopResult) -> dict[str, object]:
    """Return a JSON-serializable summary of an ECO closure loop."""

    return {
        "converged": result.converged,
        "reason": result.reason,
        "iteration_count": len(result.iterations),
        "applied_iteration_count": result.applied_iteration_count,
        "checkpoint_count": len(result.checkpoints),
        "checkpoint_ids": [item.checkpoint_id for item in result.checkpoints],
        "final_marker_class_summary": dict(result.marker_class_summary),
        "iterations": [
            {
                "index": row.index,
                "accepted": row.accepted,
                "acceptance_reason": row.acceptance_reason,
                "before_routing_gating_rule_counts": dict(row.before_routing_gating_rule_counts),
                "after_routing_gating_rule_counts": dict(row.after_routing_gating_rule_counts),
                "artifacts": dict(row.artifacts),
                "decision": calibre_eco_closure_decision_summary(row.decision),
            }
            for row in result.iterations
        ],
    }


def _build_local_routing_stage_decision(
    oa_plan: object,
    results: tuple[object, ...],
    *,
    pdk: object | None,
    stage: CalibreEcoClosureStage,
    marker_class_summary: Mapping[str, object],
    routing_counts: Mapping[str, int],
    nonrouting_counts: Mapping[str, int],
) -> CalibreEcoClosurePatchDecision:
    patch_result = build_local_routing_drc_eco_patch(oa_plan, results, pdk=pdk, config=stage.config)
    summary = local_drc_eco_patch_summary(patch_result)
    if patch_result.patch is None:
        return CalibreEcoClosurePatchDecision(
            stage,
            None,
            len(results),
            routing_gating_rule_counts=dict(routing_counts),
            nonrouting_signoff_rule_counts=dict(nonrouting_counts),
            marker_class_summary=marker_class_summary,
            summary=summary,
            reason=str(summary.get("reason", "no_local_routing_patch")),
        )
    return CalibreEcoClosurePatchDecision(
        stage,
        patch_result.patch,
        len(results),
        routing_gating_rule_counts=dict(routing_counts),
        nonrouting_signoff_rule_counts=dict(nonrouting_counts),
        marker_class_summary=marker_class_summary,
        summary=summary,
        reason="local_routing_drc_eco_patch_available",
    )


def _build_safe_redundant_via_stage_decision(
    oa_plan: object,
    results: tuple[object, ...],
    *,
    pdk: object | None,
    stage: CalibreEcoClosureStage,
    marker_class_summary: Mapping[str, object],
    routing_counts: Mapping[str, int],
    nonrouting_counts: Mapping[str, int],
) -> CalibreEcoClosurePatchDecision:
    via_markers = tuple(row for row in results if _is_redundant_via_marker(row))
    if not via_markers:
        return CalibreEcoClosurePatchDecision(
            stage,
            None,
            len(results),
            routing_gating_rule_counts=dict(routing_counts),
            nonrouting_signoff_rule_counts=dict(nonrouting_counts),
            marker_class_summary=marker_class_summary,
            summary={"patch_available": False, "edit_count": 0, "marker_count": 0},
            reason="no_redundant_via_markers",
        )

    from analogskills.eda.oa import oa_write_plan_to_layout_plan

    layout_plan = oa_write_plan_to_layout_plan(oa_plan) if hasattr(oa_plan, "cellview") else oa_plan
    issues = tuple(DrcIssue(str(getattr(row, "rule", "")), str(getattr(row, "layer", "")), str(getattr(row, "message", "")), tuple(getattr(row, "bbox"))) for row in via_markers if getattr(row, "bbox", None) is not None)
    localized = localize_drc_issues_to_layout(issues, layout_plan)
    via_cfg = dict(stage.config or {})
    patch = plan_safe_redundant_via_neighbor_patch(
        localized,
        base_plan=layout_plan,
        pdk=pdk,
        via_def_by_layer=_via_def_by_layer(via_cfg),
        include_landing_enclosures=bool(via_cfg.get("include_landing_enclosures", True)),
        allow_same_net_landing_spacing=bool(via_cfg.get("allow_same_net_landing_spacing", False)),
        max_candidates=_optional_positive_int(via_cfg.get("max_candidates")),
    )
    accepted_via_count = len(tuple(edit for edit in patch.edits if getattr(edit, "action", "") == "add_redundant_via_neighbor"))
    rect_count = len(tuple(getattr(patch.layout_patch, "rects", ()) or ()))
    summary = {
        "patch_available": bool(patch.edits),
        "edit_count": len(patch.edits),
        "marker_count": len(via_markers),
        "localized_count": len(localized),
        "accepted_via_count": accepted_via_count,
        "rect_count": rect_count,
        "patch_metadata": dict(getattr(patch.layout_patch, "metadata", {}) or {}),
    }
    if not patch.edits:
        return CalibreEcoClosurePatchDecision(
            stage,
            None,
            len(results),
            routing_gating_rule_counts=dict(routing_counts),
            nonrouting_signoff_rule_counts=dict(nonrouting_counts),
            marker_class_summary=marker_class_summary,
            summary=summary,
            reason="no_safe_redundant_via_patch",
        )
    return CalibreEcoClosurePatchDecision(
        stage,
        patch.oa_patch,
        len(results),
        routing_gating_rule_counts=dict(routing_counts),
        nonrouting_signoff_rule_counts=dict(nonrouting_counts),
        marker_class_summary=marker_class_summary,
        summary=summary,
        reason="safe_redundant_via_neighbor_patch_available",
    )


def _closure_config(pdk: object | None, override: Mapping[str, object] | None) -> dict[str, object]:
    cfg: dict[str, object] = {
        "enabled": True,
        "max_iterations": 8,
        "stage_order": ("local_routing_drc_eco", "safe_redundant_via_neighbor"),
        "local_drc_eco": {},
        "safe_redundant_via_neighbor": {
            "include_landing_enclosures": True,
            "allow_same_net_landing_spacing": False,
        },
        "rule_classification": {},
    }
    metadata = getattr(pdk, "metadata", {}) if pdk is not None else {}
    if isinstance(metadata, Mapping):
        routing_geometry = metadata.get("routing_geometry", {})
        if isinstance(routing_geometry, Mapping):
            raw = routing_geometry.get("calibre_eco_closure", {})
            if isinstance(raw, Mapping):
                cfg = _deep_update(cfg, raw)
    if override is not None:
        cfg = _deep_update(cfg, dict(override))
    return cfg


def _classification_config(cfg: Mapping[str, object]) -> dict[str, object]:
    local_cfg = dict(cfg.get("local_drc_eco", {}) if isinstance(cfg.get("local_drc_eco", {}), Mapping) else {})
    rule_cfg = dict(cfg.get("rule_classification", {}) if isinstance(cfg.get("rule_classification", {}), Mapping) else {})
    merged = _deep_update(local_cfg, rule_cfg)
    return merged


def _closure_stages(cfg: Mapping[str, object]) -> tuple[CalibreEcoClosureStage, ...]:
    rows: list[CalibreEcoClosureStage] = []
    for index, item in enumerate(_tuple_config(cfg.get("stage_order", ()))):
        kind = str(item)
        config_key = _stage_config_key(kind)
        stage_cfg = dict(cfg.get(config_key, {}) if isinstance(cfg.get(config_key, {}), Mapping) else {})
        rows.append(CalibreEcoClosureStage(f"{index + 1:02d}_{kind}", kind, stage_cfg))
    return tuple(rows)


def _stage_config_key(kind: str) -> str:
    if kind == "local_routing_drc_eco":
        return "local_drc_eco"
    return kind


def _is_redundant_via_marker(row: object) -> bool:
    rule = str(getattr(row, "rule", row if isinstance(row, str) else "")).upper()
    return rule.startswith("VIA") and ".R." in rule


def _via_def_by_layer(config: Mapping[str, object]) -> dict[str, str]:
    raw = config.get("via_def_by_layer")
    if isinstance(raw, Mapping):
        return {str(key): str(value) for key, value in raw.items() if str(key) and str(value)}
    max_via_index = max(int(config.get("max_via_index", 16) or 16), 1)
    return {f"VIA{index}": f"VIA{index}" for index in range(1, max_via_index + 1)}


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _tuple_config(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in tuple(value or ()) if str(item))


def _deep_update(base: Mapping[str, object], override: Mapping[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in dict(override).items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_update(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged


def _merge_oa_plans(plan: object, patch: object, *, pdk: object | None = None) -> object:
    from analogskills.eda.oa import merge_oa_write_plans

    if not hasattr(plan, "cellview") or not hasattr(patch, "cellview"):
        return plan
    return merge_oa_write_plans(plan, patch, cellview=getattr(plan, "cellview"), grid=pdk)


def _marker_signature(results: Sequence[object]) -> tuple[tuple[str, tuple[float, float, float, float], int | None], ...]:
    rows: list[tuple[str, tuple[float, float, float, float], int | None]] = []
    for row in results:
        rule = str(getattr(row, "rule", row if isinstance(row, str) else ""))
        bbox_obj = getattr(row, "bbox", None)
        if bbox_obj is None:
            bbox = (0.0, 0.0, 0.0, 0.0)
        else:
            values = tuple(float(value) for value in tuple(bbox_obj))
            bbox = values if len(values) == 4 else (0.0, 0.0, 0.0, 0.0)
        result_index = getattr(row, "result_index", None)
        try:
            parsed_index = None if result_index is None else int(result_index)
        except (TypeError, ValueError):
            parsed_index = None
        rows.append((rule, bbox, parsed_index))
    return tuple(sorted(rows))
