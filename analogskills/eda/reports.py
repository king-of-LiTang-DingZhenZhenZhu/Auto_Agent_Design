"""Parsers for common text artifacts produced by EDA tools."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from pathlib import Path
from typing import Mapping

from analogskills.repair import (
    DrcEcoComparison,
    DrcEcoSuggestion,
    DrcIssue,
    LvsEcoComparison,
    LvsEcoSuggestion,
    LvsIssue,
    compare_drc_eco_results,
    compare_lvs_eco_results,
    suggest_drc_ecos,
    suggest_lvs_ecos,
)

_FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


@dataclass(frozen=True)
class PexReport:
    extracted_netlist: str
    parasitic_count: int = 0
    net_cap_f: dict[str, float] = field(default_factory=dict)
    net_res_ohm: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class PexHotspot:
    net: str
    cap_f: float = 0.0
    res_ohm: float = 0.0
    critical: bool = False
    issues: tuple[str, ...] = ()
    score: float = 0.0


@dataclass(frozen=True)
class PexHotspotDelta:
    net: str
    before_cap_f: float = 0.0
    after_cap_f: float = 0.0
    cap_delta_f: float = 0.0
    before_res_ohm: float = 0.0
    after_res_ohm: float = 0.0
    res_delta_ohm: float = 0.0
    critical: bool = False
    improved: bool | None = None
    issues: tuple[str, ...] = ()
    score: float = 0.0


@dataclass(frozen=True)
class PexHotspotComparison:
    deltas: tuple[PexHotspotDelta, ...] = ()
    worsened_nets: tuple[str, ...] = ()
    improved_nets: tuple[str, ...] = ()
    new_hotspots: tuple[str, ...] = ()
    cleared_hotspots: tuple[str, ...] = ()
    summary: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetricAssessment:
    name: str
    value: float
    minimum: float | None = None
    maximum: float | None = None
    passed: bool = True
    margin: float | None = None


@dataclass(frozen=True)
class PostLayoutScorecard:
    passed: bool
    metrics: dict[str, float]
    metric_assessments: tuple[MetricAssessment, ...] = ()
    drc_count: int = 0
    lvs_count: int = 0
    pex_parasitic_count: int = 0
    extracted_netlist: str = ""
    issues: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostLayoutEcoSuggestion:
    action: str
    target: str = ""
    reason: str = ""
    priority: int = 0
    source: str = ""
    params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CalibreClosurePlan:
    passed: bool
    drc_suggestions: tuple[DrcEcoSuggestion, ...] = ()
    lvs_suggestions: tuple[LvsEcoSuggestion, ...] = ()
    owners: dict[str, int] = field(default_factory=dict)
    blocking_issues: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    provenance: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CalibreDrcResult:
    rule: str
    layer: str = ""
    message: str = ""
    result_index: int | None = None
    cell: str = ""
    instance: str = ""
    bbox: tuple[float, float, float, float] | None = None
    polygon: tuple[tuple[float, float], ...] = ()
    properties: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricDelta:
    name: str
    before: float | None
    after: float | None
    delta: float | None
    objective: str = "unknown"
    improved: bool | None = None


@dataclass(frozen=True)
class PostLayoutScorecardComparison:
    before_passed: bool
    after_passed: bool
    metric_deltas: tuple[MetricDelta, ...] = ()
    drc_delta: int = 0
    lvs_delta: int = 0
    pex_parasitic_delta: int = 0
    issue_delta: int = 0
    summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class FoundryExecutionSummary:
    ready: bool
    ready_stages: tuple[str, ...] = ()
    blocked_stages: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    missing_files: tuple[str, ...] = ()
    binding_blocked_partitions: tuple[str, ...] = ()
    macro_binding_partitions: tuple[str, ...] = ()
    architecture_budget_blocked_partitions: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    summary: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class FoundryExecutionSummaryComparison:
    before_ready: bool
    after_ready: bool
    newly_ready_stages: tuple[str, ...] = ()
    newly_blocked_stages: tuple[str, ...] = ()
    resolved_missing_inputs: tuple[str, ...] = ()
    added_missing_inputs: tuple[str, ...] = ()
    resolved_missing_files: tuple[str, ...] = ()
    added_missing_files: tuple[str, ...] = ()
    issue_delta: int = 0
    summary: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class HierarchicalCandidateContractComparison:
    before_present: bool
    after_present: bool
    materialized_partition_delta: int = 0
    verification_stage_delta: int = 0
    required_external_net_delta: int = 0
    reference_sensitive_stage_delta: int = 0
    timing_sensitive_stage_delta: int = 0
    restore_sensitive_stage_delta: int = 0
    added_verification_views: tuple[str, ...] = ()
    removed_verification_views: tuple[str, ...] = ()
    added_verification_focuses: tuple[str, ...] = ()
    removed_verification_focuses: tuple[str, ...] = ()
    summary: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationClosureDecision:
    action: str
    accepted: bool
    reason: str
    blocking_issues: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationClosureArtifact:
    action: str
    accepted: bool
    reason: str
    blocking_issues: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    scorecard_comparison: PostLayoutScorecardComparison | None = None
    run_summary_comparison: "PostLayoutRunSummaryComparison | None" = None
    pex_hotspot_comparison: PexHotspotComparison | None = None
    drc_eco_comparison: DrcEcoComparison | None = None
    lvs_eco_comparison: LvsEcoComparison | None = None
    provenance: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationClosureIteration:
    iteration_index: int = 0
    passed: bool = False
    improved: bool = False
    decision: VerificationClosureDecision | None = None
    artifact: VerificationClosureArtifact | None = None
    scorecard_comparison: PostLayoutScorecardComparison | None = None
    run_summary_comparison: "PostLayoutRunSummaryComparison | None" = None
    pex_hotspot_comparison: PexHotspotComparison | None = None
    drc_eco_comparison: DrcEcoComparison | None = None
    lvs_eco_comparison: LvsEcoComparison | None = None
    post_layout_repair_proposal: dict[str, object] | None = None
    drc_repair_proposal: dict[str, object] | None = None
    lvs_repair_proposal: dict[str, object] | None = None
    post_layout_repair_object: object | None = None
    drc_repair_object: object | None = None
    lvs_repair_object: object | None = None
    blocking_issues: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    summary: tuple[str, ...] = ()
    stop_reason: str = ""
    provenance: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationClosureLoop:
    iterations: tuple[VerificationClosureIteration, ...] = ()
    final_iteration: VerificationClosureIteration | None = None
    passed: bool = False
    stop_action: str = ""
    stop_reason: str = ""
    stop_iteration_index: int | None = None
    terminated_early: bool = False
    blocking_issues: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    repair_queue: tuple[dict[str, object], ...] = ()
    summary: tuple[str, ...] = ()
    provenance: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationRepairAction:
    iteration_index: int
    source: str
    kind: str
    selected_plan_kind: str
    selected_passed: bool
    selected_score: float
    candidate_count: int = 0
    selected_issues_after: tuple[object, ...] = ()
    repair_scope: dict[str, object] = field(default_factory=dict)
    execution_profile: dict[str, object] = field(default_factory=dict)
    proposal: dict[str, object] = field(default_factory=dict)
    repair_proposal: object | None = None


@dataclass(frozen=True)
class VerificationRepairExecutionPlan:
    action: VerificationRepairAction
    recommended_rerun: str
    reason: str
    followup_actions: tuple[str, ...] = ()
    writeback_level: str = ""
    writeback_target: str = ""
    rerun_levels: tuple[str, ...] = ()
    dispatch_mode: str = "direct_apply"
    execution_profile: dict[str, object] = field(default_factory=dict)
    dispatch_plan: dict[str, object] = field(default_factory=dict)
    repair_proposal: object | None = None


@dataclass(frozen=True)
class VerificationRepairExecutionResult:
    plan: VerificationRepairExecutionPlan
    applied: bool
    backend: object
    rerun_result: object | None = None
    dispatch_summary: dict[str, object] = field(default_factory=dict)
    summary: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationStageSynthesisResult:
    supported: bool
    proposal: object | None = None
    summary: tuple[str, ...] = ()
    artifact: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationStageProposalValidation:
    valid: bool
    reason: str
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class HierarchyCellviewNode:
    name: str
    lib: str
    cell: str
    view: str = "layout"
    view_type: str = "maskLayout"
    parent: str = ""
    aliases: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PostLayoutRunRecord:
    run_id: str
    scorecard: PostLayoutScorecard
    corner: str = ""
    voltage_v: float | None = None
    temperature_c: float | None = None
    monte_carlo_seed: int | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostLayoutRunSummary:
    runs: tuple[PostLayoutRunRecord, ...] = ()
    total_runs: int = 0
    passing_runs: int = 0
    failing_runs: int = 0
    worst_metrics: dict[str, float] = field(default_factory=dict)
    best_metrics: dict[str, float] = field(default_factory=dict)
    failing_run_ids: tuple[str, ...] = ()
    summary: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostLayoutRunSummaryComparison:
    before_total_runs: int = 0
    after_total_runs: int = 0
    passing_delta: int = 0
    failing_delta: int = 0
    new_failing_run_ids: tuple[str, ...] = ()
    recovered_run_ids: tuple[str, ...] = ()
    still_failing_run_ids: tuple[str, ...] = ()
    worst_metric_deltas: tuple[MetricDelta, ...] = ()
    summary: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()


def build_post_layout_scorecard(
    measurements: dict[str, float] | None = None,
    *,
    pex: PexReport | None = None,
    drc_issues: tuple[DrcIssue, ...] | list[DrcIssue] = (),
    lvs_issues: tuple[LvsIssue, ...] | list[LvsIssue] = (),
    targets: dict[str, tuple[float | None, float | None]] | None = None,
) -> PostLayoutScorecard:
    """Summarize post-layout verification artifacts for agent decisions."""

    metrics = dict(measurements or {})
    assessments = tuple(_assess_metric(name, value, *(targets or {}).get(name, (None, None))) for name, value in sorted(metrics.items()))
    issues: list[str] = []
    next_actions: list[str] = []
    for assessment in assessments:
        if assessment.passed:
            continue
        if assessment.minimum is not None and assessment.value < assessment.minimum:
            issues.append(f"metric {assessment.name}={assessment.value:g} below minimum {assessment.minimum:g}")
        elif assessment.maximum is not None and assessment.value > assessment.maximum:
            issues.append(f"metric {assessment.name}={assessment.value:g} above maximum {assessment.maximum:g}")
        else:
            issues.append(f"metric {assessment.name}={assessment.value:g} outside target")
        next_actions.append(f"review_sizing_or_layout_for_{assessment.name}")
    if drc_issues:
        issues.append(f"{len(drc_issues)} DRC issue(s) remain")
        next_actions.append("run_drc_ecos")
    if lvs_issues:
        issues.append(f"{len(lvs_issues)} LVS issue(s) remain")
        next_actions.append("run_lvs_repairs")
    if pex is not None and pex.parasitic_count > 0:
        next_actions.append("review_parasitic_hotspots")
    return PostLayoutScorecard(
        passed=not issues,
        metrics=metrics,
        metric_assessments=assessments,
        drc_count=len(drc_issues),
        lvs_count=len(lvs_issues),
        pex_parasitic_count=pex.parasitic_count if pex is not None else 0,
        extracted_netlist=pex.extracted_netlist if pex is not None else "",
        issues=tuple(issues),
        next_actions=tuple(dict.fromkeys(next_actions)),
    )


def build_post_layout_run_record(
    run_id: str,
    scorecard: PostLayoutScorecard,
    *,
    corner: str = "",
    voltage_v: float | None = None,
    temperature_c: float | None = None,
    monte_carlo_seed: int | None = None,
    artifacts: Mapping[str, str | Path] | None = None,
    tags: tuple[str, ...] | list[str] = (),
) -> PostLayoutRunRecord:
    """Attach provenance to a post-layout scorecard."""

    artifact_map = {str(key): str(value) for key, value in dict(artifacts or {}).items()}
    return PostLayoutRunRecord(
        str(run_id),
        scorecard,
        corner=str(corner),
        voltage_v=voltage_v,
        temperature_c=temperature_c,
        monte_carlo_seed=monte_carlo_seed,
        artifacts=artifact_map,
        tags=tuple(str(tag) for tag in tags),
    )


def summarize_post_layout_runs(
    runs: tuple[PostLayoutRunRecord, ...] | list[PostLayoutRunRecord],
    *,
    objectives: Mapping[str, str] | None = None,
) -> PostLayoutRunSummary:
    """Summarize PVT/Monte-Carlo post-layout runs without changing closure state."""

    records = tuple(runs)
    objective_map = {str(key): str(value).lower() for key, value in dict(objectives or {}).items()}
    passing = tuple(record for record in records if record.scorecard.passed)
    failing = tuple(record for record in records if not record.scorecard.passed)
    metric_names = sorted({name for record in records for name in record.scorecard.metrics})
    worst_metrics = {name: _run_metric_extreme(records, name, objective_map.get(name), worst=True) for name in metric_names}
    best_metrics = {name: _run_metric_extreme(records, name, objective_map.get(name), worst=False) for name in metric_names}
    next_actions = []
    for record in failing:
        next_actions.extend(record.scorecard.next_actions)
    if failing:
        next_actions.append("review_failing_post_layout_runs")
    summary = _post_layout_run_summary_lines(records, failing, worst_metrics)
    return PostLayoutRunSummary(
        runs=records,
        total_runs=len(records),
        passing_runs=len(passing),
        failing_runs=len(failing),
        worst_metrics=worst_metrics,
        best_metrics=best_metrics,
        failing_run_ids=tuple(record.run_id for record in failing),
        summary=summary,
        next_actions=tuple(dict.fromkeys(next_actions)),
    )


def compare_post_layout_run_summaries(
    before: PostLayoutRunSummary,
    after: PostLayoutRunSummary,
    *,
    objectives: Mapping[str, str] | None = None,
    tol: float = 1e-12,
) -> PostLayoutRunSummaryComparison:
    """Compare two PVT/Monte-Carlo post-layout run summaries."""

    before_failing = set(before.failing_run_ids)
    after_failing = set(after.failing_run_ids)
    objective_map = {str(key): str(value).lower() for key, value in dict(objectives or {}).items()}
    metric_names = sorted(set(before.worst_metrics) | set(after.worst_metrics))
    worst_metric_deltas = tuple(
        _metric_delta(name, before.worst_metrics.get(name), after.worst_metrics.get(name), objective_map.get(name), tol) for name in metric_names
    )
    new_failing = tuple(sorted(after_failing - before_failing))
    recovered = tuple(sorted(before_failing - after_failing))
    still_failing = tuple(sorted(before_failing & after_failing))
    return PostLayoutRunSummaryComparison(
        before_total_runs=before.total_runs,
        after_total_runs=after.total_runs,
        passing_delta=after.passing_runs - before.passing_runs,
        failing_delta=after.failing_runs - before.failing_runs,
        new_failing_run_ids=new_failing,
        recovered_run_ids=recovered,
        still_failing_run_ids=still_failing,
        worst_metric_deltas=worst_metric_deltas,
        summary=_post_layout_run_comparison_summary(new_failing, recovered, still_failing, worst_metric_deltas, before.total_runs, after.total_runs),
        next_actions=_post_layout_run_comparison_actions(new_failing, still_failing, worst_metric_deltas),
    )


def suggest_post_layout_ecos(
    scorecard: PostLayoutScorecard,
    *,
    metric_action_map: Mapping[str, str] | None = None,
    max_suggestions: int | None = None,
    hierarchy_context: Mapping[str, object] | None = None,
) -> tuple[PostLayoutEcoSuggestion, ...]:
    """Map a post-layout scorecard to reviewable ECO next steps."""

    action_map = {str(key): str(value) for key, value in dict(metric_action_map or {}).items()}
    suggestions: list[PostLayoutEcoSuggestion] = []
    if scorecard.lvs_count:
        suggestions.append(
            PostLayoutEcoSuggestion(
                "run_lvs_repairs",
                reason=f"{scorecard.lvs_count} LVS issue(s) remain",
                priority=100,
                source="lvs",
                params={"count": scorecard.lvs_count},
            )
        )
    if scorecard.drc_count:
        suggestions.append(
            PostLayoutEcoSuggestion(
                "run_drc_ecos",
                reason=f"{scorecard.drc_count} DRC issue(s) remain",
                priority=95,
                source="drc",
                params={"count": scorecard.drc_count},
            )
        )
    for assessment in scorecard.metric_assessments:
        if assessment.passed:
            continue
        action = action_map.get(assessment.name, _default_metric_eco_action(assessment))
        suggestions.append(
            PostLayoutEcoSuggestion(
                action,
                target=assessment.name,
                reason=_metric_failure_reason(assessment),
                priority=_metric_priority(assessment),
                source="metric",
                params={
                    "value": assessment.value,
                    "minimum": assessment.minimum,
                    "maximum": assessment.maximum,
                    "margin": assessment.margin,
                },
            )
        )
    if scorecard.pex_parasitic_count:
        suggestions.append(
            PostLayoutEcoSuggestion(
                "review_parasitic_hotspots",
                target=scorecard.extracted_netlist,
                reason=f"{scorecard.pex_parasitic_count} extracted parasitic item(s)",
                priority=50,
                source="pex",
                params={"parasitic_count": scorecard.pex_parasitic_count},
            )
        )
    suggestions = [_scope_post_layout_eco(item, hierarchy_context) for item in suggestions]
    suggestions = [item for item in suggestions if item is not None]
    suggestions = [_reprioritize_post_layout_eco(item, hierarchy_context) for item in suggestions]
    ranked = tuple(sorted(suggestions, key=lambda item: (-item.priority, item.action, item.target)))
    return ranked if max_suggestions is None else ranked[:max_suggestions]


def _scope_post_layout_eco(
    suggestion: PostLayoutEcoSuggestion,
    hierarchy_context: Mapping[str, object] | None,
) -> PostLayoutEcoSuggestion | None:
    if not hierarchy_context:
        return suggestion
    changed_devices = tuple(str(name) for name in hierarchy_context.get("retarget_changed_devices", ()) if str(name))
    stable_devices = tuple(str(name) for name in hierarchy_context.get("keep_stable_devices", ()) if str(name))
    changed_nets = tuple(str(name) for name in hierarchy_context.get("retarget_changed_nets", ()) if str(name))
    stable_nets = tuple(str(name) for name in hierarchy_context.get("keep_stable_nets", ()) if str(name))
    scope_mode = str(hierarchy_context.get("scope_mode", "advisory_only"))
    params = dict(suggestion.params)
    if suggestion.source == "metric":
        if changed_devices:
            params["scope_devices"] = changed_devices
            params["scope_policy"] = "changed_devices_only"
        elif stable_devices:
            params["avoid_devices"] = stable_devices
            params["scope_policy"] = "avoid_stable_devices"
    elif suggestion.source in {"pex", "pex_delta"}:
        if changed_nets:
            params["scope_nets"] = changed_nets
            params["scope_policy"] = "changed_nets_only"
        elif stable_nets:
            params["avoid_nets"] = stable_nets
            params["scope_policy"] = "avoid_stable_nets"
    elif suggestion.source in {"drc", "lvs"} and changed_devices:
        params["scope_devices"] = changed_devices
        params["scope_policy"] = "prefer_changed_devices"
    if scope_mode != "advisory_only":
        if suggestion.source == "metric" and not changed_devices:
            return None
        if suggestion.source in {"pex", "pex_delta"} and not changed_nets:
            return None
    return replace(suggestion, params=params)


def _reprioritize_post_layout_eco(
    suggestion: PostLayoutEcoSuggestion,
    hierarchy_context: Mapping[str, object] | None,
) -> PostLayoutEcoSuggestion:
    if hierarchy_context is None:
        return suggestion
    priority = int(suggestion.priority)
    target = str(suggestion.target or "")
    changed = {str(name) for name in hierarchy_context.get("retarget_changed_partitions", ()) if str(name)}
    removed_bus = {str(name) for name in hierarchy_context.get("removed_bus_corridors", ()) if str(name)}
    removed_feedback = {str(name) for name in hierarchy_context.get("removed_feedback_loops", ()) if str(name)}
    system_contract = dict(hierarchy_context.get("hierarchical_system_contract", {}) or {})
    focus_metrics = {str(name).lower() for name in hierarchy_context.get("focus_metrics", ()) if str(name)}
    if suggestion.source == "pex" and removed_feedback:
        priority += 12
    if suggestion.source == "pex" and any(bool(item.get("restore_required", False)) for item in tuple(system_contract.get("feedback_contracts", ()) or ())):
        priority += 10
    if target and target.lower() in focus_metrics:
        priority += 10
    if suggestion.action == "review_parasitic_hotspots" and removed_bus:
        priority += 8
    if suggestion.action == "review_parasitic_hotspots" and any(bool(item.get("restore_required", False)) for item in tuple(system_contract.get("bus_contracts", ()) or ())):
        priority += 8
    if suggestion.source == "metric" and any(bool(item.get("preserve_integrity", False)) for item in tuple(system_contract.get("reference_paths", ()) or ())):
        priority += 4
    if suggestion.source == "metric" and changed:
        priority += 5
    return replace(suggestion, priority=min(priority, 100))


def build_calibre_closure_plan(
    drc_issues: tuple[DrcIssue, ...] | list[DrcIssue] = (),
    lvs_issues: tuple[LvsIssue, ...] | list[LvsIssue] = (),
    *,
    layout_plan: object | None = None,
    floorplan: object | None = None,
    pin_label_report: Mapping[str, object] | None = None,
    layer_aliases: Mapping[str, str] | None = None,
    max_suggestions: int | None = None,
    provenance: Mapping[str, object] | None = None,
) -> CalibreClosurePlan:
    """Convert current Calibre DRC/LVS failures into an owner-scoped ECO plan."""

    drc_records = tuple(drc_issues)
    lvs_records = tuple(lvs_issues)
    drc_suggestions = suggest_drc_ecos(drc_records, layer_aliases=layer_aliases)
    lvs_suggestions = suggest_lvs_ecos(
        lvs_records,
        layout_plan=layout_plan,
        floorplan=floorplan,
        pin_label_report=pin_label_report,
    )
    if max_suggestions is not None:
        ranked = _rank_calibre_suggestions(drc_suggestions, lvs_suggestions)[:max_suggestions]
        drc_suggestions = tuple(suggestion for source, suggestion in ranked if source == "drc")
        lvs_suggestions = tuple(suggestion for source, suggestion in ranked if source == "lvs")
    ranked = _rank_calibre_suggestions(drc_suggestions, lvs_suggestions)
    return CalibreClosurePlan(
        passed=not drc_records and not lvs_records,
        drc_suggestions=drc_suggestions,
        lvs_suggestions=lvs_suggestions,
        owners=_calibre_owner_counts(drc_suggestions, lvs_suggestions),
        blocking_issues=_calibre_blocking_issues(drc_suggestions, lvs_suggestions),
        next_actions=tuple(dict.fromkeys(str(suggestion.action) for _, suggestion in ranked)),
        provenance=_calibre_closure_provenance(provenance, drc_records, lvs_records, drc_suggestions, lvs_suggestions),
    )


def analyze_pex_hotspots(
    pex: PexReport,
    *,
    critical_nets: tuple[str, ...] | list[str] = (),
    cap_limit_f: float | None = None,
    res_limit_ohm: float | None = None,
    top_k: int | None = None,
) -> tuple[PexHotspot, ...]:
    """Rank extracted parasitic hotspots by net without changing the layout."""

    critical = set(critical_nets)
    nets = sorted(set(pex.net_cap_f) | set(pex.net_res_ohm))
    hotspots = []
    for net in nets:
        cap = float(pex.net_cap_f.get(net, 0.0))
        res = float(pex.net_res_ohm.get(net, 0.0))
        issues = []
        if cap_limit_f is not None and cap > cap_limit_f:
            issues.append(f"cap {cap:g}F exceeds {cap_limit_f:g}F")
        if res_limit_ohm is not None and res > res_limit_ohm:
            issues.append(f"res {res:g}ohm exceeds {res_limit_ohm:g}ohm")
        if net in critical and (cap > 0.0 or res > 0.0):
            issues.append("critical net has extracted parasitic loading")
        score = _pex_hotspot_score(cap, res, net in critical, bool(issues), cap_limit_f, res_limit_ohm)
        hotspots.append(PexHotspot(net, cap, res, net in critical, tuple(issues), score))
    ranked = tuple(sorted(hotspots, key=lambda item: (-item.score, item.net)))
    return ranked if top_k is None else ranked[:top_k]


def suggest_pex_ecos(
    hotspots: tuple[PexHotspot, ...] | list[PexHotspot],
    *,
    max_suggestions: int | None = None,
) -> tuple[PostLayoutEcoSuggestion, ...]:
    """Map PEX hotspots to reviewable routing/layout ECO suggestions."""

    suggestions = []
    for hotspot in hotspots:
        if not hotspot.issues:
            continue
        action = _pex_hotspot_action(hotspot)
        suggestions.append(
            PostLayoutEcoSuggestion(
                action,
                target=hotspot.net,
                reason="; ".join(hotspot.issues),
                priority=70 if hotspot.critical else 55,
                source="pex",
                params={"cap_f": hotspot.cap_f, "res_ohm": hotspot.res_ohm, "score": hotspot.score, "critical": hotspot.critical},
            )
        )
    ranked = tuple(sorted(suggestions, key=lambda item: (-item.priority, item.action, item.target)))
    return ranked if max_suggestions is None else ranked[:max_suggestions]


def compare_pex_hotspots(
    before: PexReport,
    after: PexReport,
    *,
    critical_nets: tuple[str, ...] | list[str] = (),
    cap_limit_f: float | None = None,
    res_limit_ohm: float | None = None,
    cap_tol_f: float = 0.0,
    res_tol_ohm: float = 0.0,
) -> PexHotspotComparison:
    """Compare PEX net parasitics before and after an ECO."""

    critical = set(critical_nets)
    nets = sorted(set(before.net_cap_f) | set(before.net_res_ohm) | set(after.net_cap_f) | set(after.net_res_ohm))
    deltas = []
    for net in nets:
        before_cap = float(before.net_cap_f.get(net, 0.0))
        after_cap = float(after.net_cap_f.get(net, 0.0))
        before_res = float(before.net_res_ohm.get(net, 0.0))
        after_res = float(after.net_res_ohm.get(net, 0.0))
        cap_delta = after_cap - before_cap
        res_delta = after_res - before_res
        issues = _pex_delta_issues(
            net,
            before_cap,
            after_cap,
            cap_delta,
            before_res,
            after_res,
            res_delta,
            net in critical,
            cap_limit_f,
            res_limit_ohm,
            cap_tol_f,
            res_tol_ohm,
        )
        improved = _pex_delta_improved(cap_delta, res_delta, cap_tol_f, res_tol_ohm)
        score = _pex_delta_score(cap_delta, res_delta, net in critical, bool(issues), cap_limit_f, res_limit_ohm, cap_tol_f, res_tol_ohm)
        deltas.append(PexHotspotDelta(net, before_cap, after_cap, cap_delta, before_res, after_res, res_delta, net in critical, improved, tuple(issues), score))
    ranked = tuple(sorted(deltas, key=lambda item: (-item.score, item.net)))
    worsened = tuple(delta.net for delta in ranked if delta.improved is False or delta.issues)
    improved_nets = tuple(delta.net for delta in ranked if delta.improved is True and not delta.issues)
    new_hotspots = tuple(delta.net for delta in ranked if _pex_delta_was_new_hotspot(delta, cap_tol_f, res_tol_ohm))
    cleared_hotspots = tuple(delta.net for delta in ranked if _pex_delta_was_cleared_hotspot(delta, cap_tol_f, res_tol_ohm))
    return PexHotspotComparison(
        deltas=ranked,
        worsened_nets=worsened,
        improved_nets=improved_nets,
        new_hotspots=new_hotspots,
        cleared_hotspots=cleared_hotspots,
        summary=_pex_hotspot_comparison_summary(ranked, new_hotspots, cleared_hotspots),
        next_actions=_pex_hotspot_comparison_actions(ranked),
    )


def suggest_pex_comparison_ecos(
    comparison: PexHotspotComparison,
    *,
    hierarchy_context: Mapping[str, object] | None = None,
    max_suggestions: int | None = None,
) -> tuple[PostLayoutEcoSuggestion, ...]:
    """Map PEX hotspot deltas to reviewable ECO suggestions."""

    suggestions = []
    for delta in comparison.deltas:
        if not delta.issues:
            continue
        action = _pex_delta_action(delta)
        suggestions.append(
            PostLayoutEcoSuggestion(
                action,
                target=delta.net,
                reason="; ".join(delta.issues),
                priority=_pex_delta_priority(delta),
                source="pex_delta",
                params={
                    "before_cap_f": delta.before_cap_f,
                    "after_cap_f": delta.after_cap_f,
                    "cap_delta_f": delta.cap_delta_f,
                    "before_res_ohm": delta.before_res_ohm,
                    "after_res_ohm": delta.after_res_ohm,
                    "res_delta_ohm": delta.res_delta_ohm,
                    "critical": delta.critical,
                    "score": delta.score,
                },
            )
        )
    suggestions = [_scope_post_layout_eco(item, hierarchy_context) for item in suggestions]
    suggestions = [item for item in suggestions if item is not None]
    suggestions = [_reprioritize_pex_comparison_eco(item, hierarchy_context) for item in suggestions]
    ranked = tuple(sorted(suggestions, key=lambda item: (-item.priority, item.action, item.target)))
    return ranked if max_suggestions is None else ranked[:max_suggestions]


def _reprioritize_pex_comparison_eco(
    suggestion: PostLayoutEcoSuggestion,
    hierarchy_context: Mapping[str, object] | None,
) -> PostLayoutEcoSuggestion:
    if hierarchy_context is None:
        return suggestion
    priority = int(suggestion.priority)
    target = str(suggestion.target or "")
    removed_feedback = {str(name) for name in hierarchy_context.get("removed_feedback_loops", ()) if str(name)}
    removed_bus = {str(name) for name in hierarchy_context.get("removed_bus_corridors", ()) if str(name)}
    system_contract = dict(hierarchy_context.get("hierarchical_system_contract", {}) or {})
    parasitic_plan = dict(hierarchy_context.get("hierarchical_partition_parasitic_target_plan", {}) or {})
    changed_nets = {str(name) for name in hierarchy_context.get("retarget_changed_nets", ()) if str(name)}
    architecture_critical_nets = {
        str(net)
        for partition in tuple(parasitic_plan.get("partitions", ()) or ())
        if isinstance(partition, Mapping)
        and str(dict(partition.get("architecture_budget", {}) or {}).get("sensitivity", "") or "") in {"reference_critical", "timing_critical", "feedback_critical"}
        for net in (
            tuple(partition.get("critical_nets", ()) or ())
            + tuple(partition.get("reference_nets", ()) or ())
            + tuple(partition.get("feedback_nets", ()) or ())
            + tuple(partition.get("routing_anchor_nets", ()) or ())
        )
        if str(net)
    }
    if target and target in changed_nets:
        priority += 8
    if target and target in architecture_critical_nets:
        priority += 10
    if target and target in removed_feedback:
        priority += 12
    if removed_bus:
        for bus in tuple(system_contract.get("bus_contracts", ()) or ()):
            nets = {str(net) for net in tuple(dict(bus).get("nets", ()) or ()) if str(net)}
            if target in nets and bool(dict(bus).get("restore_required", False)):
                priority += 10
                break
    if any(
        target == str(dict(item).get("net", ""))
        and bool(dict(item).get("restore_required", False))
        for item in tuple(system_contract.get("feedback_contracts", ()) or ())
    ):
        priority += 12
    return replace(suggestion, priority=min(priority, 100))


def compare_post_layout_scorecards(
    before: PostLayoutScorecard,
    after: PostLayoutScorecard,
    *,
    objectives: Mapping[str, str] | None = None,
    tol: float = 1e-12,
) -> PostLayoutScorecardComparison:
    """Compare two post-layout scorecards without making rollback decisions."""

    objective_map = {str(key): str(value).lower() for key, value in dict(objectives or {}).items()}
    metric_names = sorted(set(before.metrics) | set(after.metrics))
    metric_deltas = tuple(_metric_delta(name, before.metrics.get(name), after.metrics.get(name), objective_map.get(name), tol) for name in metric_names)
    drc_delta = after.drc_count - before.drc_count
    lvs_delta = after.lvs_count - before.lvs_count
    pex_delta = after.pex_parasitic_count - before.pex_parasitic_count
    issue_delta = len(after.issues) - len(before.issues)
    return PostLayoutScorecardComparison(
        before_passed=before.passed,
        after_passed=after.passed,
        metric_deltas=metric_deltas,
        drc_delta=drc_delta,
        lvs_delta=lvs_delta,
        pex_parasitic_delta=pex_delta,
        issue_delta=issue_delta,
        summary=_scorecard_comparison_summary(metric_deltas, drc_delta, lvs_delta, pex_delta, issue_delta, before.passed, after.passed),
    )


def summarize_foundry_execution_contract(contract: Mapping[str, object] | None) -> FoundryExecutionSummary:
    normalized = dict(contract or {})
    stages = dict(normalized.get("stages", {}) or {})
    system_contract = dict(normalized.get("system", {}) or {})
    hierarchy_binding = dict(normalized.get("hierarchy_binding_summary", {}) or {})
    repair_targets = tuple(
        dict(item)
        for item in tuple(system_contract.get("repair_targets", ()) or ())
        if isinstance(item, Mapping)
    )
    ready_stages = tuple(str(name) for name in normalized.get("ready_stages", ()) if str(name))
    blocked_stages = tuple(str(name) for name in normalized.get("blocked_stages", ()) if str(name))
    missing_inputs = tuple(
        dict.fromkeys(
            str(name)
            for stage in stages.values()
            for name in tuple(dict(stage).get("missing_inputs", ()) or ())
            if str(name)
        )
    )
    missing_files = tuple(
        dict.fromkeys(
            str(name)
            for stage in stages.values()
            for name in tuple(dict(stage).get("missing_files", ()) or ())
            if str(name)
        )
    )
    binding_blocked_partitions = tuple(
        str(name)
        for name in tuple(hierarchy_binding.get("binding_blocked_partitions", ()) or ())
        if str(name)
    )
    macro_binding_partitions = tuple(
        str(name)
        for name in tuple(hierarchy_binding.get("macro_binding_partitions", ()) or ())
        if str(name)
    )
    architecture_budget_blocked_partitions = tuple(
        str(name)
        for name in tuple(hierarchy_binding.get("architecture_budget_blocked_partitions", ()) or ())
        if str(name)
    )
    issues = tuple(str(issue) for issue in normalized.get("issues", ()) if str(issue))
    next_actions: list[str] = []
    if blocked_stages:
        next_actions.append("review_blocked_foundry_stages")
    if missing_inputs:
        next_actions.append("provide_missing_foundry_inputs")
    if missing_files:
        next_actions.append("provide_missing_foundry_files")
    if int(system_contract.get("restore_bus_required_count", 0)) > 0:
        next_actions.append("restore_system_bus_corridors_before_foundry")
    if int(system_contract.get("restore_feedback_required_count", 0)) > 0:
        next_actions.append("restore_system_feedback_paths_before_foundry")
    if repair_targets:
        next_actions.append("review_system_repair_levels_before_foundry")
    if binding_blocked_partitions:
        next_actions.append("close_hierarchical_pdk_binding_before_foundry")
    if architecture_budget_blocked_partitions:
        next_actions.append("close_architecture_budget_coverage_before_foundry")
    return FoundryExecutionSummary(
        ready=bool(normalized.get("ready", False)),
        ready_stages=ready_stages,
        blocked_stages=blocked_stages,
        missing_inputs=missing_inputs,
        missing_files=missing_files,
        binding_blocked_partitions=binding_blocked_partitions,
        macro_binding_partitions=macro_binding_partitions,
        architecture_budget_blocked_partitions=architecture_budget_blocked_partitions,
        issues=issues,
        summary=tuple(
            (
                *_foundry_execution_summary_lines(
                    bool(normalized.get("ready", False)),
                    ready_stages,
                    blocked_stages,
                    missing_inputs,
                    missing_files,
                    issues,
                ),
                *(("binding_blocked_partitions=" + ",".join(binding_blocked_partitions),) if binding_blocked_partitions else ()),
                *(("macro_binding_partitions=" + ",".join(macro_binding_partitions),) if macro_binding_partitions else ()),
                *(("architecture_budget_blocked_partitions=" + ",".join(architecture_budget_blocked_partitions),) if architecture_budget_blocked_partitions else ()),
                *(f"repair_target:{item.get('kind','')}@{item.get('recommended_level','')}" for item in repair_targets),
            )
        ),
        next_actions=tuple(next_actions),
    )


def compare_foundry_execution_summaries(
    before: FoundryExecutionSummary,
    after: FoundryExecutionSummary,
) -> FoundryExecutionSummaryComparison:
    before_ready_stages = set(before.ready_stages)
    after_ready_stages = set(after.ready_stages)
    before_blocked_stages = set(before.blocked_stages)
    after_blocked_stages = set(after.blocked_stages)
    before_missing_inputs = set(before.missing_inputs)
    after_missing_inputs = set(after.missing_inputs)
    before_missing_files = set(before.missing_files)
    after_missing_files = set(after.missing_files)

    newly_ready_stages = tuple(sorted(after_ready_stages - before_ready_stages))
    newly_blocked_stages = tuple(sorted(after_blocked_stages - before_blocked_stages))
    resolved_missing_inputs = tuple(sorted(before_missing_inputs - after_missing_inputs))
    added_missing_inputs = tuple(sorted(after_missing_inputs - before_missing_inputs))
    resolved_missing_files = tuple(sorted(before_missing_files - after_missing_files))
    added_missing_files = tuple(sorted(after_missing_files - before_missing_files))
    issue_delta = len(after.issues) - len(before.issues)
    next_actions: list[str] = []
    if newly_blocked_stages or added_missing_inputs or added_missing_files or issue_delta > 0:
        next_actions.append("review_foundry_readiness_regressions")
    if newly_ready_stages or resolved_missing_inputs or resolved_missing_files or issue_delta < 0:
        next_actions.append("promote_foundry_ready_candidate")
    return FoundryExecutionSummaryComparison(
        before_ready=before.ready,
        after_ready=after.ready,
        newly_ready_stages=newly_ready_stages,
        newly_blocked_stages=newly_blocked_stages,
        resolved_missing_inputs=resolved_missing_inputs,
        added_missing_inputs=added_missing_inputs,
        resolved_missing_files=resolved_missing_files,
        added_missing_files=added_missing_files,
        issue_delta=issue_delta,
        summary=_foundry_execution_comparison_summary(
            before.ready,
            after.ready,
            newly_ready_stages,
            newly_blocked_stages,
            resolved_missing_inputs,
            added_missing_inputs,
            resolved_missing_files,
            added_missing_files,
            issue_delta,
        ),
        next_actions=tuple(next_actions),
    )


def compare_hierarchical_candidate_contracts(
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
) -> HierarchicalCandidateContractComparison:
    before_contract = dict(before or {})
    after_contract = dict(after or {})
    before_lowering = dict(before_contract.get("implementation_lowering_contract", {}) or {})
    after_lowering = dict(after_contract.get("implementation_lowering_contract", {}) or {})
    before_verification = dict(before_contract.get("verification_intent_contract", {}) or {})
    after_verification = dict(after_contract.get("verification_intent_contract", {}) or {})

    before_present = bool(before_contract)
    after_present = bool(after_contract)
    materialized_partition_delta = int(after_lowering.get("materialized_partition_count", 0)) - int(before_lowering.get("materialized_partition_count", 0))
    verification_stage_delta = int(after_verification.get("stage_count", 0)) - int(before_verification.get("stage_count", 0))
    required_external_net_delta = int(after_lowering.get("required_external_net_count", 0)) - int(before_lowering.get("required_external_net_count", 0))
    reference_sensitive_stage_delta = int(after_verification.get("reference_sensitive_stage_count", 0)) - int(before_verification.get("reference_sensitive_stage_count", 0))
    timing_sensitive_stage_delta = int(after_verification.get("timing_sensitive_stage_count", 0)) - int(before_verification.get("timing_sensitive_stage_count", 0))
    restore_sensitive_stage_delta = int(after_verification.get("restore_sensitive_stage_count", 0)) - int(before_verification.get("restore_sensitive_stage_count", 0))

    before_views = {str(item) for item in tuple(before_verification.get("verification_views", ()) or ()) if str(item)}
    after_views = {str(item) for item in tuple(after_verification.get("verification_views", ()) or ()) if str(item)}
    before_focuses = {str(item) for item in tuple(before_verification.get("verification_focuses", ()) or ()) if str(item)}
    after_focuses = {str(item) for item in tuple(after_verification.get("verification_focuses", ()) or ()) if str(item)}
    added_verification_views = tuple(sorted(after_views - before_views))
    removed_verification_views = tuple(sorted(before_views - after_views))
    added_verification_focuses = tuple(sorted(after_focuses - before_focuses))
    removed_verification_focuses = tuple(sorted(before_focuses - after_focuses))

    next_actions: list[str] = []
    if not before_present and after_present:
        next_actions.append("promote_hierarchical_candidate_contract")
    if before_present and not after_present:
        next_actions.append("restore_hierarchical_candidate_contract")
    if materialized_partition_delta > 0 or verification_stage_delta > 0:
        next_actions.append("promote_more_executable_hierarchical_candidate")
    if materialized_partition_delta < 0 or verification_stage_delta < 0:
        next_actions.append("review_hierarchical_candidate_regressions")
    if added_verification_views or added_verification_focuses:
        next_actions.append("review_new_hierarchical_verification_coverage")
    if removed_verification_views or removed_verification_focuses:
        next_actions.append("restore_lost_hierarchical_verification_coverage")

    summary = (
        f"before_present={before_present}",
        f"after_present={after_present}",
        f"materialized_partition_delta={materialized_partition_delta}",
        f"verification_stage_delta={verification_stage_delta}",
        f"required_external_net_delta={required_external_net_delta}",
        f"reference_sensitive_stage_delta={reference_sensitive_stage_delta}",
        f"timing_sensitive_stage_delta={timing_sensitive_stage_delta}",
        f"restore_sensitive_stage_delta={restore_sensitive_stage_delta}",
        *(f"added_verification_view={item}" for item in added_verification_views),
        *(f"removed_verification_view={item}" for item in removed_verification_views),
        *(f"added_verification_focus={item}" for item in added_verification_focuses),
        *(f"removed_verification_focus={item}" for item in removed_verification_focuses),
    )
    return HierarchicalCandidateContractComparison(
        before_present=before_present,
        after_present=after_present,
        materialized_partition_delta=materialized_partition_delta,
        verification_stage_delta=verification_stage_delta,
        required_external_net_delta=required_external_net_delta,
        reference_sensitive_stage_delta=reference_sensitive_stage_delta,
        timing_sensitive_stage_delta=timing_sensitive_stage_delta,
        restore_sensitive_stage_delta=restore_sensitive_stage_delta,
        added_verification_views=added_verification_views,
        removed_verification_views=removed_verification_views,
        added_verification_focuses=added_verification_focuses,
        removed_verification_focuses=removed_verification_focuses,
        summary=summary,
        next_actions=tuple(dict.fromkeys(next_actions)),
    )


def decide_verification_closure(
    comparison: PostLayoutScorecardComparison,
    *,
    allow_pex_growth: bool = True,
    require_after_passed: bool = False,
    pex_hotspot_comparison: PexHotspotComparison | None = None,
    block_on_critical_pex_regression: bool = True,
    block_on_any_pex_regression: bool = False,
) -> VerificationClosureDecision:
    """Convert scorecard deltas into an agent-reviewable closure decision."""

    blocking = []
    next_actions = []
    if require_after_passed and not comparison.after_passed:
        blocking.append("post-layout scorecard still failing")
        next_actions.append("continue_verification_ecos")
    if comparison.lvs_delta > 0:
        blocking.append(f"LVS issue count increased by {comparison.lvs_delta}")
        next_actions.append("run_lvs_repairs")
    if comparison.drc_delta > 0:
        blocking.append(f"DRC issue count increased by {comparison.drc_delta}")
        next_actions.append("run_drc_ecos")
    worsened_metrics = tuple(delta.name for delta in comparison.metric_deltas if delta.improved is False)
    if worsened_metrics:
        blocking.append(f"metric regression: {', '.join(worsened_metrics)}")
        next_actions.append("review_metric_regressions")
    if comparison.pex_parasitic_delta > 0 and not allow_pex_growth:
        blocking.append(f"PEX parasitic count increased by {comparison.pex_parasitic_delta}")
        next_actions.append("review_parasitic_hotspots")
    if pex_hotspot_comparison is not None:
        blocking.extend(_pex_hotspot_closure_blockers(pex_hotspot_comparison, block_on_critical_pex_regression, block_on_any_pex_regression))
        next_actions.extend(pex_hotspot_comparison.next_actions)

    if comparison.after_passed and not blocking:
        return VerificationClosureDecision("accept_verification_closure", True, "after scorecard passes with no blocking regressions")
    if blocking:
        return VerificationClosureDecision("reject_or_continue_eco", False, "; ".join(blocking), tuple(blocking), tuple(dict.fromkeys(next_actions)))

    improvements = _scorecard_improvement_count(comparison)
    if improvements > 0:
        return VerificationClosureDecision(
            "continue_verification_ecos",
            False,
            f"{improvements} verification signal(s) improved but closure is not clean",
            next_actions=("continue_verification_ecos",),
        )
    return VerificationClosureDecision(
        "hold_for_manual_review",
        False,
        "verification did not regress, but no clear closure improvement was detected",
        next_actions=("manual_verification_review",),
    )


def build_verification_closure_artifact(
    scorecard_comparison: PostLayoutScorecardComparison | None = None,
    *,
    run_summary_comparison: PostLayoutRunSummaryComparison | None = None,
    pex_hotspot_comparison: PexHotspotComparison | None = None,
    drc_eco_comparison: DrcEcoComparison | None = None,
    lvs_eco_comparison: LvsEcoComparison | None = None,
    allow_pex_growth: bool = True,
    require_after_passed: bool = False,
    block_on_critical_pex_regression: bool = True,
    block_on_any_pex_regression: bool = False,
    provenance: Mapping[str, object] | None = None,
) -> VerificationClosureArtifact:
    """Build one closure artifact from scorecard, PVT/MC, PEX, DRC, and LVS deltas."""

    if scorecard_comparison is not None:
        decision = decide_verification_closure(
            scorecard_comparison,
            allow_pex_growth=allow_pex_growth,
            require_after_passed=require_after_passed,
            pex_hotspot_comparison=pex_hotspot_comparison,
            block_on_critical_pex_regression=block_on_critical_pex_regression,
            block_on_any_pex_regression=block_on_any_pex_regression,
        )
    else:
        decision = VerificationClosureDecision(
            "hold_for_manual_review",
            False,
            "no scorecard comparison was provided",
            next_actions=("manual_verification_review",),
        )

    blocking = list(decision.blocking_issues)
    next_actions = list(decision.next_actions)
    blocking.extend(_run_summary_closure_blockers(run_summary_comparison))
    if run_summary_comparison is not None:
        next_actions.extend(run_summary_comparison.next_actions)
    blocking.extend(_drc_eco_closure_blockers(drc_eco_comparison))
    if drc_eco_comparison is not None:
        next_actions.extend(drc_eco_comparison.next_actions)
    blocking.extend(_lvs_eco_closure_blockers(lvs_eco_comparison))
    if lvs_eco_comparison is not None:
        next_actions.extend(lvs_eco_comparison.next_actions)

    blocking_issues = tuple(dict.fromkeys(blocking))
    actions = tuple(dict.fromkeys(next_actions))
    system_blocking_issues, system_actions = _system_closure_contract_actions(provenance)
    if system_blocking_issues:
        blocking_issues = tuple(dict.fromkeys((*blocking_issues, *system_blocking_issues)))
    if system_actions:
        actions = tuple(dict.fromkeys((*actions, *system_actions)))
    if blocking_issues:
        return VerificationClosureArtifact(
            "reject_or_continue_eco",
            False,
            "; ".join(blocking_issues),
            blocking_issues,
            actions,
            scorecard_comparison,
            run_summary_comparison,
            pex_hotspot_comparison,
            drc_eco_comparison,
            lvs_eco_comparison,
            _closure_provenance(provenance, scorecard_comparison, run_summary_comparison, pex_hotspot_comparison, drc_eco_comparison, lvs_eco_comparison),
        )

    return VerificationClosureArtifact(
        decision.action,
        decision.accepted,
        "; ".join(blocking_issues) if blocking_issues else decision.reason,
        (),
        actions or decision.next_actions,
        scorecard_comparison,
        run_summary_comparison,
        pex_hotspot_comparison,
        drc_eco_comparison,
        lvs_eco_comparison,
        _closure_provenance(provenance, scorecard_comparison, run_summary_comparison, pex_hotspot_comparison, drc_eco_comparison, lvs_eco_comparison),
    )


def _system_closure_contract_actions(provenance: Mapping[str, object] | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    root = dict(provenance or {})
    cycle_metadata = dict(root.get("cycle_metadata", {}) or {})
    system_contract = dict(cycle_metadata.get("hierarchical_system_contract", {}) or {})
    blocking: list[str] = []
    actions: list[str] = []
    if not system_contract:
        return (), ()
    if any(bool(item.get("restore_required", False)) for item in tuple(system_contract.get("bus_contracts", ()) or ())):
        blocking.append("system bus corridor restoration is still required")
        actions.append("restore_system_bus_corridors")
    if any(bool(item.get("restore_required", False)) for item in tuple(system_contract.get("feedback_contracts", ()) or ())):
        blocking.append("system feedback path restoration is still required")
        actions.append("restore_system_feedback_paths")
    return tuple(dict.fromkeys(blocking)), tuple(dict.fromkeys(actions))


def build_verification_closure_iteration(
    *,
    before_scorecard: PostLayoutScorecard | None = None,
    after_scorecard: PostLayoutScorecard | None = None,
    before_drc_issues: tuple[DrcIssue, ...] | list[DrcIssue] | None = None,
    after_drc_issues: tuple[DrcIssue, ...] | list[DrcIssue] | None = None,
    before_lvs_issues: tuple[LvsIssue, ...] | list[LvsIssue] | None = None,
    after_lvs_issues: tuple[LvsIssue, ...] | list[LvsIssue] | None = None,
    before_run_summary: PostLayoutRunSummary | None = None,
    after_run_summary: PostLayoutRunSummary | None = None,
    before_pex: PexReport | None = None,
    after_pex: PexReport | None = None,
    before_drc_report: str | Path | None = None,
    after_drc_report: str | Path | None = None,
    before_lvs_report: str | Path | None = None,
    after_lvs_report: str | Path | None = None,
    layout_plan: object | None = None,
    pdk: object | None = None,
    floorplan: object | None = None,
    pin_label_report: Mapping[str, object] | None = None,
    top_level_nets: tuple[str, ...] | list[str] | None = None,
    require_explicit_labels: bool = True,
    metric_targets: dict[str, tuple[float | None, float | None]] | None = None,
    metric_objectives: Mapping[str, str] | None = None,
    critical_nets: tuple[str, ...] | list[str] = (),
    cap_limit_f: float | None = None,
    res_limit_ohm: float | None = None,
    allow_pex_growth: bool = True,
    require_after_passed: bool = False,
    block_on_critical_pex_regression: bool = True,
    block_on_any_pex_regression: bool = False,
    min_width_by_layer: Mapping[str, float] | None = None,
    min_area_by_layer: Mapping[str, float] | None = None,
    min_spacing_by_layer: Mapping[str, float] | None = None,
    via_def_by_layer: Mapping[str, str] | None = None,
    fixed_nets: tuple[str, ...] = (),
    include_via_array_enclosures: bool = True,
    landing_margin_um: float | None = None,
    max_candidates: int | None = None,
    max_lvs_items: int | None = None,
    iteration_index: int = 0,
    provenance: Mapping[str, object] | None = None,
) -> VerificationClosureIteration:
    """Compose one verification closure iteration from before/after artifacts."""

    from analogskills.eda.oa import layout_plan_to_oa_write_plan
    from analogskills.repair import PostLayoutEcoRepairProposal, build_drc_repair_proposal, build_lvs_repair_proposal, repair_proposal_summary

    drc_before = tuple(before_drc_issues) if before_drc_issues is not None else parse_drc_report(before_drc_report) if before_drc_report is not None else ()
    drc_after = tuple(after_drc_issues) if after_drc_issues is not None else parse_drc_report(after_drc_report) if after_drc_report is not None else ()
    lvs_before = tuple(before_lvs_issues) if before_lvs_issues is not None else parse_lvs_report(before_lvs_report) if before_lvs_report is not None else ()
    lvs_after = tuple(after_lvs_issues) if after_lvs_issues is not None else parse_lvs_report(after_lvs_report) if after_lvs_report is not None else ()

    before_card = before_scorecard
    after_card = after_scorecard
    if before_card is None and (metric_targets is not None or before_pex is not None or drc_before or lvs_before):
        before_card = build_post_layout_scorecard(
            pex=before_pex,
            drc_issues=drc_before,
            lvs_issues=lvs_before,
            targets=metric_targets,
        )
    if after_card is None and (metric_targets is not None or after_pex is not None or drc_after or lvs_after):
        after_card = build_post_layout_scorecard(
            pex=after_pex,
            drc_issues=drc_after,
            lvs_issues=lvs_after,
            targets=metric_targets,
        )

    scorecard_comparison = None
    if before_card is not None and after_card is not None:
        scorecard_comparison = compare_post_layout_scorecards(before_card, after_card, objectives=metric_objectives)
    run_summary_comparison = None
    if before_run_summary is not None and after_run_summary is not None:
        run_summary_comparison = compare_post_layout_run_summaries(before_run_summary, after_run_summary, objectives=metric_objectives)
    pex_hotspot_comparison = None
    if before_pex is not None and after_pex is not None:
        pex_hotspot_comparison = compare_pex_hotspots(
            before_pex,
            after_pex,
            critical_nets=critical_nets,
            cap_limit_f=cap_limit_f,
            res_limit_ohm=res_limit_ohm,
        )
    drc_eco_comparison = compare_drc_eco_results(drc_before, drc_after) if (drc_before or drc_after) else None
    lvs_eco_comparison = compare_lvs_eco_results(lvs_before, lvs_after) if (lvs_before or lvs_after) else None

    artifact = build_verification_closure_artifact(
        scorecard_comparison,
        run_summary_comparison=run_summary_comparison,
        pex_hotspot_comparison=pex_hotspot_comparison,
        drc_eco_comparison=drc_eco_comparison,
        lvs_eco_comparison=lvs_eco_comparison,
        allow_pex_growth=allow_pex_growth,
        require_after_passed=require_after_passed,
        block_on_critical_pex_regression=block_on_critical_pex_regression,
        block_on_any_pex_regression=block_on_any_pex_regression,
        provenance=provenance,
    )
    decision = _closure_iteration_decision(
        artifact,
        scorecard_comparison,
        drc_eco_comparison,
        lvs_eco_comparison,
        run_summary_comparison,
        pex_hotspot_comparison,
        block_on_critical_pex_regression=block_on_critical_pex_regression,
        block_on_any_pex_regression=block_on_any_pex_regression,
    )

    drc_proposal = None
    drc_proposal_summary = None
    if drc_after and layout_plan is not None:
        drc_proposal = build_drc_repair_proposal(
            drc_after,
            layout_plan=layout_plan,
            pdk=pdk,
            min_width_by_layer=min_width_by_layer,
            min_area_by_layer=min_area_by_layer,
            min_spacing_by_layer=min_spacing_by_layer,
            via_def_by_layer=via_def_by_layer,
            fixed_nets=fixed_nets,
            include_via_array_enclosures=include_via_array_enclosures,
            landing_margin_um=landing_margin_um,
            max_candidates=max_candidates,
        )
        drc_proposal_summary = repair_proposal_summary(drc_proposal)

    post_layout_proposal = None
    post_layout_proposal_summary = None
    if (
        layout_plan is not None
        and pex_hotspot_comparison is not None
        and pex_hotspot_comparison.worsened_nets
        and hasattr(layout_plan, "paths")
    ):
        hotspot_nets = tuple(pex_hotspot_comparison.worsened_nets)
        scope_nets = tuple(dict.fromkeys(str(net) for net in dict(provenance or {}).get("flow_metadata", {}).get("hierarchical_retarget_changed_nets", ()) if str(net)))
        avoid_nets = tuple(dict.fromkeys(str(net) for net in dict(provenance or {}).get("flow_metadata", {}).get("hierarchical_keep_stable_nets", ()) if str(net)))
        route_patch = None
        try:
            from analogskills.layout import plan_pex_hotspot_layout_ir

            route_patch = plan_pex_hotspot_layout_ir(
                layout_plan,
                pex_hotspot_comparison,
                pdk,
                lib=getattr(getattr(layout_plan, "cell", None), "lib", "work"),
                cell=f"{getattr(getattr(layout_plan, 'cell', None), 'cell', 'post_layout')}_pex_eco",
                view=getattr(getattr(layout_plan, "cell", None), "view", "layout"),
                allowed_nets=scope_nets,
                blocked_nets=avoid_nets,
                scope_policy="prefer_changed_devices",
                hierarchy_lowering=dict(dict(provenance or {}).get("flow_metadata", {}).get("hierarchical_implementation_lowering", {}) or {}),
                hierarchy_parasitics=dict(dict(provenance or {}).get("flow_metadata", {}).get("hierarchical_partition_parasitic_target_plan", {}) or {}),
            )
        except Exception:
            route_patch = None
        if route_patch is not None and (
            tuple(getattr(route_patch, "paths", ())) or tuple(getattr(route_patch, "rects", ())) or tuple(getattr(route_patch, "vias", ()))
        ):
            score = max(
                (
                    float(delta.score)
                    for delta in pex_hotspot_comparison.deltas
                    if delta.net in hotspot_nets and delta.issues
                ),
                default=float("inf"),
            )
            guidance_score, guidance_metadata = _post_layout_system_guidance_adjustment(
                hotspot_nets,
                provenance,
            )
            post_layout_proposal = PostLayoutEcoRepairProposal(
                kind="post_layout_pex_route_eco",
                layout_patch=route_patch,
                oa_patch=layout_plan_to_oa_write_plan(route_patch),
                score=score + guidance_score,
                passed=False,
                issues_after=tuple(f"pex hotspot remains on {net}" for net in hotspot_nets),
                hotspot_nets=hotspot_nets,
                metadata={
                    "critical_nets": tuple(delta.net for delta in pex_hotspot_comparison.deltas if delta.critical and delta.issues),
                    "comparison_summary": tuple(pex_hotspot_comparison.summary),
                    "scope_nets": tuple(getattr(route_patch, "metadata", {}).get("allowed_nets", ())),
                    "avoid_nets": tuple(getattr(route_patch, "metadata", {}).get("blocked_nets", ())),
                    "scope_policy": str(getattr(route_patch, "metadata", {}).get("scope_policy", "")),
                    **guidance_metadata,
                },
            )
            post_layout_proposal_summary = repair_proposal_summary(post_layout_proposal)

    lvs_proposal = None
    lvs_proposal_summary = None
    if lvs_after and layout_plan is not None:
        lvs_proposal = build_lvs_repair_proposal(
            lvs_after,
            layout_plan=layout_plan,
            pdk=pdk,
            floorplan=floorplan,
            pin_label_report=pin_label_report,
            top_level_nets=top_level_nets,
            require_explicit_labels=require_explicit_labels,
            min_width_by_layer=min_width_by_layer,
            min_spacing_by_layer=min_spacing_by_layer,
            max_items=max_lvs_items,
            max_candidates=max_candidates,
        )
        lvs_proposal_summary = repair_proposal_summary(lvs_proposal)

    improved = _closure_iteration_improved(
        scorecard_comparison,
        drc_eco_comparison,
        lvs_eco_comparison,
        run_summary_comparison,
        pex_hotspot_comparison,
    )
    summary = _closure_iteration_summary(
        artifact,
        scorecard_comparison,
        drc_eco_comparison,
        lvs_eco_comparison,
        run_summary_comparison,
        pex_hotspot_comparison,
        improved=improved,
    )
    return VerificationClosureIteration(
        iteration_index=iteration_index,
        passed=artifact.accepted,
        improved=improved,
        decision=decision,
        artifact=artifact,
        scorecard_comparison=scorecard_comparison,
        run_summary_comparison=run_summary_comparison,
        pex_hotspot_comparison=pex_hotspot_comparison,
        drc_eco_comparison=drc_eco_comparison,
        lvs_eco_comparison=lvs_eco_comparison,
        post_layout_repair_proposal=post_layout_proposal_summary,
        drc_repair_proposal=drc_proposal_summary,
        lvs_repair_proposal=lvs_proposal_summary,
        post_layout_repair_object=post_layout_proposal,
        drc_repair_object=drc_proposal,
        lvs_repair_object=lvs_proposal,
        blocking_issues=artifact.blocking_issues,
        next_actions=tuple(dict.fromkeys((*decision.next_actions, *artifact.next_actions))),
        summary=summary,
        stop_reason=artifact.reason,
        provenance=_closure_iteration_provenance(
            provenance,
            artifact.provenance,
            iteration_index=iteration_index,
            drc_before_count=len(drc_before),
            drc_after_count=len(drc_after),
            lvs_before_count=len(lvs_before),
            lvs_after_count=len(lvs_after),
            has_scorecard=scorecard_comparison is not None,
            has_run_summary=run_summary_comparison is not None,
            has_pex=pex_hotspot_comparison is not None,
            has_post_layout_proposal=post_layout_proposal_summary is not None,
            has_drc_proposal=drc_proposal_summary is not None,
            has_lvs_proposal=lvs_proposal_summary is not None,
        ),
    )


def _post_layout_system_guidance_adjustment(
    hotspot_nets: tuple[str, ...],
    provenance: Mapping[str, object] | None,
) -> tuple[float, dict[str, object]]:
    cycle_metadata = dict(dict(provenance or {}).get("cycle_metadata", {}) or {})
    system_contract = dict(cycle_metadata.get("hierarchical_system_contract", {}) or {})
    nets = {str(net) for net in hotspot_nets if str(net)}
    if not nets or not system_contract:
        return 0.0, {}
    guidance: list[dict[str, object]] = []
    penalty = 0.0
    if any(
        bool(dict(item).get("restore_required", False))
        and nets & {str(net) for net in tuple(dict(item).get("nets", ()) or ()) if str(net)}
        for item in tuple(system_contract.get("bus_contracts", ()) or ())
        if isinstance(item, Mapping)
    ):
        guidance.append(
            {
                "kind": "bus_corridor_restore",
                "recommended_level": "parent",
            }
        )
        penalty += 25.0
    if any(
        bool(dict(item).get("restore_required", False))
        and str(dict(item).get("net", "")) in nets
        for item in tuple(system_contract.get("feedback_contracts", ()) or ())
        if isinstance(item, Mapping)
    ):
        guidance.append(
            {
                "kind": "feedback_path_restore",
                "recommended_level": "top",
            }
        )
        penalty += 35.0
    if any(
        bool(dict(item).get("preserve_integrity", False))
        and str(dict(item).get("net", "")) in nets
        for item in tuple(system_contract.get("reference_paths", ()) or ())
        if isinstance(item, Mapping)
    ):
        guidance.append(
            {
                "kind": "reference_integrity_protect",
                "recommended_level": "leaf_or_parent",
            }
        )
        penalty += 20.0
    if not guidance:
        return 0.0, {}
    recommended_level = _system_repair_guidance_level(tuple(guidance))
    return penalty, {
        "system_repair_guidance": tuple(guidance),
        "system_recommended_level": recommended_level,
        "escalation_required": recommended_level in {"parent", "top", "cross_hierarchy"},
        "hotspot_system_penalty": penalty,
    }


def run_verification_closure_loop(
    iterations: tuple[Mapping[str, object], ...] | list[Mapping[str, object]],
    *,
    stop_on_no_improvement: bool = True,
    stop_on_regression: bool = True,
    stop_on_accept: bool = True,
    provenance: Mapping[str, object] | None = None,
) -> VerificationClosureLoop:
    """Evaluate repeated verification closure iterations until a stop condition is met."""

    records: list[VerificationClosureIteration] = []
    terminated_early = False
    for idx, payload in enumerate(iterations):
        params = dict(payload)
        params.setdefault("iteration_index", idx)
        current = build_verification_closure_iteration(**params)
        records.append(current)

        action = current.decision.action if current.decision is not None else ""
        if stop_on_accept and current.passed:
            terminated_early = idx < len(iterations) - 1
            break
        if stop_on_regression and action == "reject_or_continue_eco":
            terminated_early = idx < len(iterations) - 1
            break
        if stop_on_no_improvement and action == "hold_for_manual_review":
            terminated_early = idx < len(iterations) - 1
            break

    final_iteration = records[-1] if records else None
    return VerificationClosureLoop(
        iterations=tuple(records),
        final_iteration=final_iteration,
        passed=bool(final_iteration.passed) if final_iteration is not None else False,
        stop_action=final_iteration.decision.action if final_iteration is not None and final_iteration.decision is not None else "",
        stop_reason=final_iteration.stop_reason if final_iteration is not None else "no iterations were executed",
        stop_iteration_index=final_iteration.iteration_index if final_iteration is not None else None,
        terminated_early=terminated_early,
        blocking_issues=final_iteration.blocking_issues if final_iteration is not None else (),
        next_actions=final_iteration.next_actions if final_iteration is not None else (),
        repair_queue=_closure_loop_repair_queue(tuple(records)),
        summary=_closure_loop_summary(tuple(records), terminated_early=terminated_early),
        provenance=_closure_loop_provenance(provenance, tuple(records), terminated_early=terminated_early),
    )


def run_verification_closure_loop_from_baseline(
    *,
    baseline: Mapping[str, object],
    after_iterations: tuple[Mapping[str, object], ...] | list[Mapping[str, object]],
    stop_on_no_improvement: bool = True,
    stop_on_regression: bool = True,
    stop_on_accept: bool = True,
    provenance: Mapping[str, object] | None = None,
) -> VerificationClosureLoop:
    """Evaluate repeated closure iterations using one baseline and chained after-artifacts."""

    previous = dict(baseline)
    chained: list[dict[str, object]] = []
    for idx, after_payload in enumerate(after_iterations):
        current = dict(after_payload)
        chained.append(
            {
                **_closure_iteration_before_payload(previous),
                **_closure_iteration_after_payload(current),
                **_closure_iteration_shared_payload(previous, current),
                "iteration_index": idx,
            }
        )
        previous = current
    return run_verification_closure_loop(
        tuple(chained),
        stop_on_no_improvement=stop_on_no_improvement,
        stop_on_regression=stop_on_regression,
        stop_on_accept=stop_on_accept,
        provenance=provenance,
    )


def select_next_verification_repair_action(loop: VerificationClosureLoop) -> VerificationRepairAction | None:
    """Pick the next repair action from a closure loop repair queue."""

    if not loop.repair_queue:
        return None
    ranked = sorted(loop.repair_queue, key=_verification_repair_action_rank)
    best = ranked[0]
    return VerificationRepairAction(
        iteration_index=int(best["iteration_index"]),
        source=str(best["source"]),
        kind=str(best["kind"]),
        selected_plan_kind=str(best["selected_plan_kind"]),
        selected_passed=bool(best["selected_passed"]),
        selected_score=float(best["selected_score"]),
        candidate_count=int(best["candidate_count"]),
        selected_issues_after=tuple(best.get("selected_issues_after", ()) or ()),
        repair_scope=dict(best.get("repair_scope", {})),
        execution_profile=dict(best.get("execution_profile", {})),
        proposal=dict(best.get("proposal", {})),
        repair_proposal=best.get("repair_proposal"),
    )


def build_verification_repair_execution_plan(loop: VerificationClosureLoop) -> VerificationRepairExecutionPlan | None:
    """Convert the best repair action into an executor-facing verification plan."""

    action = select_next_verification_repair_action(loop)
    if action is None:
        return None
    hierarchy_database = _verification_repair_hierarchy_database(loop)
    recommended_rerun = _verification_repair_rerun_kind(action)
    writeback_level = _verification_repair_writeback_level(action)
    writeback_target = _verification_repair_writeback_target(action)
    rerun_levels = _verification_repair_rerun_levels(action, recommended_rerun)
    dispatch_mode = _verification_repair_dispatch_mode(action)
    execution_profile = _verification_repair_execution_profile(
        action,
        recommended_rerun=recommended_rerun,
        writeback_level=writeback_level,
        dispatch_mode=dispatch_mode,
    )
    action = replace(action, execution_profile=execution_profile)
    dispatch_plan = _verification_repair_dispatch_plan(
        action,
        writeback_level=writeback_level,
        writeback_target=writeback_target,
        rerun_levels=rerun_levels,
        dispatch_mode=dispatch_mode,
        hierarchy_database=hierarchy_database,
    )
    return VerificationRepairExecutionPlan(
        action=action,
        recommended_rerun=recommended_rerun,
        reason=_verification_repair_reason(action),
        followup_actions=_verification_repair_followup_actions(action, recommended_rerun),
        writeback_level=writeback_level,
        writeback_target=writeback_target,
        rerun_levels=rerun_levels,
        dispatch_mode=dispatch_mode,
        execution_profile=execution_profile,
        dispatch_plan=dispatch_plan,
        repair_proposal=action.repair_proposal,
    )


def execute_verification_repair_plan(
    plan: VerificationRepairExecutionPlan,
    *,
    backend: object,
    rerun: object | None = None,
    dispatch_executor: object | None = None,
) -> VerificationRepairExecutionResult:
    """Apply the selected repair proposal and optionally trigger its rerun stage."""

    from analogskills.repair import apply_repair_proposal

    if plan.repair_proposal is None:
        raise ValueError("verification repair execution plan has no executable repair proposal")
    execution_profile = _execution_profile_from_plan(plan)
    dispatch_summary = {
        "writeback_level": plan.writeback_level,
        "writeback_target": plan.writeback_target,
        "rerun_levels": plan.rerun_levels,
        "dispatch_mode": plan.dispatch_mode,
        "execution_profile": execution_profile,
        "dispatch_plan": dict(plan.dispatch_plan),
        "dispatch_bundle": build_verification_repair_dispatch_bundle(plan),
        "stage_apply_objects": build_verification_repair_stage_apply_objects(plan),
        "applied_to_target": False,
    }
    scope_check = _verify_dispatch_scope_guard(plan.repair_proposal, plan.dispatch_plan)
    if scope_check["allowed"] is False:
        dispatch_summary["scope_guard_violation"] = scope_check
        return VerificationRepairExecutionResult(
            plan=plan,
            applied=False,
            backend=backend,
            rerun_result=None,
            dispatch_summary=dispatch_summary,
            summary=(
                f"blocked {plan.action.source} repair {plan.action.selected_plan_kind or plan.action.kind}",
                f"scope_guard_violation={scope_check['reason']}",
                f"dispatch_mode={plan.dispatch_mode}",
            ),
        )
    if plan.dispatch_mode == "manual_orchestrated_apply":
        applied_backend = backend
        dispatch_summary["requires_manual_dispatch"] = True
        if dispatch_executor is not None:
            dispatch_result = _run_dispatch_executor(dispatch_executor, plan, dispatch_summary["dispatch_bundle"])
            dispatch_summary["dispatch_executor_result"] = dispatch_result
            dispatch_summary["manual_dispatch_submitted"] = True
    else:
        applied_backend = _dispatch_verification_repair_stage_sequence(
            plan,
            dispatch_summary["stage_apply_objects"],
            backend,
        )
        dispatch_summary["applied_to_target"] = bool(plan.writeback_target)
        dispatch_summary["applied_cellview"] = dict(plan.dispatch_plan.get("target_cellview", {}))
        dispatch_summary["staged_execution"] = _execute_stage_apply_objects(dispatch_summary["stage_apply_objects"])
    rerun_result = None
    if rerun is not None:
        if callable(rerun):
            rerun_result = rerun(plan)
        elif hasattr(rerun, "run"):
            rerun_result = rerun.run(plan)
        else:
            raise TypeError("rerun must be callable or expose a run(plan) method")
    summary = [
        f"applied {plan.action.source} repair {plan.action.selected_plan_kind or plan.action.kind}",
        f"recommended rerun={plan.recommended_rerun}",
        f"writeback_level={plan.writeback_level or 'unknown'}",
        f"dispatch_mode={plan.dispatch_mode}",
    ]
    if plan.writeback_target:
        summary.append(f"writeback_target={plan.writeback_target}")
    if rerun_result is not None:
        summary.append("rerun completed")
    return VerificationRepairExecutionResult(
        plan=plan,
        applied=plan.dispatch_mode != "manual_orchestrated_apply",
        backend=applied_backend,
        rerun_result=rerun_result,
        dispatch_summary=dispatch_summary,
        summary=tuple(summary),
    )


def build_verification_repair_dispatch_bundle(
    plan: VerificationRepairExecutionPlan,
) -> dict[str, object]:
    dispatch_plan = dict(plan.dispatch_plan)
    orchestration_plan = dict(dispatch_plan.get("orchestration_plan", {}) or {})
    hierarchy_resolution = dict(dispatch_plan.get("hierarchy_resolution", {}) or {})
    decomposed_subactions = _decompose_verification_repair_subactions(plan)
    scope_guard = dict(dispatch_plan.get("scope_guard", {}) or {})
    return {
        "dispatch_mode": plan.dispatch_mode,
        "writeback_level": plan.writeback_level,
        "writeback_target": plan.writeback_target,
        "recommended_rerun": plan.recommended_rerun,
        "rerun_levels": tuple(plan.rerun_levels),
        "reason": plan.reason,
        "followup_actions": tuple(plan.followup_actions),
        "scope_guard": scope_guard,
        "system_repair_guidance": tuple(
            dict(item)
            for item in tuple(dict(scope_guard.get("metadata", {}) or {}).get("system_repair_guidance", ()) or ())
            if isinstance(item, Mapping)
        ),
        "system_recommended_level": str(scope_guard.get("system_recommended_level", "")),
        "system_scope_escalation_required": bool(scope_guard.get("escalation_required", False)),
        "orchestration_plan": orchestration_plan,
        "hierarchy_contract": _serialize_hierarchy_resolution_contract(hierarchy_resolution),
        "decomposed_subactions": decomposed_subactions,
        "apply_steps": tuple(dispatch_plan.get("apply_steps", ()) or ()),
        "verification_steps": tuple(dispatch_plan.get("verification_steps", ()) or ()),
        "source_cellview": dict(dispatch_plan.get("source_cellview", {}) or {}),
        "target_cellview": dict(dispatch_plan.get("target_cellview", {}) or {}),
    }


def build_verification_repair_stage_apply_objects(
    plan: VerificationRepairExecutionPlan,
) -> tuple[dict[str, object], ...]:
    orchestration_plan = dict(plan.dispatch_plan.get("orchestration_plan", {}) or {})
    stages = tuple(orchestration_plan.get("stages", ()) or ())
    stage_objects: list[dict[str, object]] = []
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        role = str(stage.get("role", ""))
        cell = str(stage.get("cell", ""))
        target_cellview = dict(stage.get("target_cellview", {}) or {})
        if not target_cellview and cell:
            target_cellview = {"cell": cell}
        stage_dispatch_plan = _stage_dispatch_plan(plan, stage, target_cellview)
        stage_execution_profile = _stage_execution_profile(
            plan,
            stage,
            stage_dispatch_plan=stage_dispatch_plan,
            target_cellview=target_cellview,
        )
        stage_object = _stage_apply_object(
            plan,
            role=role,
            execution_kind=(
                "target_writeback"
                if role == "target"
                else ("manual_handoff" if plan.dispatch_mode == "manual_orchestrated_apply" else "stage_preparation")
            ),
            stage_dispatch_plan=stage_dispatch_plan,
            stage_execution_profile=stage_execution_profile,
        )
        stage_objects.append(stage_object)
    return tuple(stage_objects)


def synthesize_verification_repair_stage_proposal(
    plan: VerificationRepairExecutionPlan,
    stage_apply_object: Mapping[str, object],
) -> VerificationStageSynthesisResult:
    proposal = getattr(plan, "repair_proposal", None)
    artifact = dict(stage_apply_object.get("stage_synthesis_artifact", {}) or {})
    if proposal is None:
        return VerificationStageSynthesisResult(
            supported=False,
            proposal=None,
            artifact=artifact,
            summary=("stage synthesis skipped: execution plan has no repair proposal",),
        )
    if not artifact:
        return VerificationStageSynthesisResult(
            supported=False,
            proposal=None,
            artifact=artifact,
            summary=("stage synthesis skipped: no stage synthesis artifact available",),
        )
    kind = str(artifact.get("source_proposal_kind", ""))
    role = str(stage_apply_object.get("role", ""))
    if role != "intermediate":
        return VerificationStageSynthesisResult(
            supported=False,
            proposal=None,
            artifact=artifact,
            summary=(f"stage synthesis skipped: role {role or 'unknown'} does not require synthesized intermediate proposal",),
        )
    synthesized = _synthesize_post_layout_intermediate_stage_proposal(plan, proposal, stage_apply_object, artifact)
    if synthesized is not None:
        return VerificationStageSynthesisResult(
            supported=True,
            proposal=synthesized,
            artifact=artifact,
            summary=(
                f"synthesized intermediate stage proposal for {artifact.get('target_cellview', {}).get('cell', '') or artifact.get('writeback_target', '')}",
                f"source_proposal_kind={kind or 'unknown'}",
            ),
        )
    return VerificationStageSynthesisResult(
        supported=False,
        proposal=None,
        artifact=artifact,
        summary=(f"stage synthesis unsupported for proposal kind {kind or 'unknown'}",),
    )


def build_parent_route_stage_proposal(
    plan: VerificationRepairExecutionPlan,
    stage_apply_object: Mapping[str, object],
    *,
    route_plan: object | None = None,
    pdk: object | None = None,
) -> VerificationStageSynthesisResult:
    proposal = getattr(plan, "repair_proposal", None)
    artifact = dict(stage_apply_object.get("stage_synthesis_artifact", {}) or {})
    if proposal is None:
        return VerificationStageSynthesisResult(
            supported=False,
            proposal=None,
            artifact=artifact,
            summary=("parent route synthesis skipped: execution plan has no repair proposal",),
        )
    synthesized = _build_parent_route_stage_proposal(plan, proposal, stage_apply_object, artifact, route_plan=route_plan, pdk=pdk)
    if synthesized is None:
        return VerificationStageSynthesisResult(
            supported=False,
            proposal=None,
            artifact=artifact,
            summary=("parent route synthesis unsupported for current stage/proposal kind",),
        )
    return VerificationStageSynthesisResult(
        supported=True,
        proposal=synthesized,
        artifact=artifact,
        summary=(
            f"built parent/intermediate route stage proposal for {artifact.get('target_cellview', {}).get('cell', '')}",
            f"source_proposal_kind={artifact.get('source_proposal_kind', '') or getattr(proposal, 'kind', 'unknown')}",
        ),
    )


def materialize_verification_repair_stage_proposal(
    plan: VerificationRepairExecutionPlan,
    stage_apply_object: Mapping[str, object],
    *,
    route_plan: object | None = None,
    pdk: object | None = None,
) -> VerificationStageSynthesisResult:
    artifact = dict(stage_apply_object.get("stage_synthesis_artifact", {}) or {})
    role = str(stage_apply_object.get("role", ""))
    execution_kind = str(stage_apply_object.get("execution_kind", ""))
    stage_proposal = stage_apply_object.get("stage_proposal")
    if execution_kind == "manual_handoff":
        return VerificationStageSynthesisResult(
            supported=False,
            proposal=None,
            artifact=artifact,
            summary=("stage proposal materialization skipped: manual handoff stage requires external execution",),
        )
    if stage_proposal is not None and role in {"source", "target"}:
        return VerificationStageSynthesisResult(
            supported=True,
            proposal=stage_proposal,
            artifact=artifact,
            summary=(
                f"materialized existing {role or 'stage'} proposal for {dict(stage_apply_object.get('target_cellview', {}) or {}).get('cell', '')}",
                f"source_proposal_kind={str(stage_apply_object.get('stage_proposal_kind', '')) or 'unknown'}",
            ),
        )
    if role == "intermediate":
        route_result = build_parent_route_stage_proposal(
            plan,
            stage_apply_object,
            route_plan=route_plan,
            pdk=pdk,
        )
        if route_result.supported:
            return route_result
        return synthesize_verification_repair_stage_proposal(plan, stage_apply_object)
    return VerificationStageSynthesisResult(
        supported=False,
        proposal=None,
        artifact=artifact,
        summary=(f"stage proposal materialization unsupported for role {role or 'unknown'}",),
    )


def decompose_verification_repair_stage_proposals(
    plan: VerificationRepairExecutionPlan,
    *,
    route_plan: object | None = None,
    pdk: object | None = None,
) -> tuple[dict[str, object], ...]:
    stage_objects = build_verification_repair_stage_apply_objects(plan)
    decomposed: list[dict[str, object]] = []
    for index, stage_apply_object in enumerate(stage_objects, start=1):
        materialized = materialize_verification_repair_stage_proposal(
            plan,
            stage_apply_object,
            route_plan=route_plan,
            pdk=pdk,
        )
        validation = None
        if materialized.supported and materialized.proposal is not None:
            validation = validate_synthesized_verification_stage_proposal(
                materialized.proposal,
                stage_apply_object=stage_apply_object,
            )
        materialized_summary = _repair_proposal_dispatch_summary(materialized.proposal)
        if materialized_summary:
            materialized_summary["target_cellview"] = dict(stage_apply_object.get("target_cellview", {}) or {})
        stage_contract = _stage_materialization_contract(
            stage_apply_object=stage_apply_object,
            materialized=materialized,
            materialized_summary=materialized_summary,
            validation=validation,
        )
        stage_scope_contract = _stage_scope_contract(stage_apply_object)
        decomposed.append(
            {
                "order": index,
                "role": str(stage_apply_object.get("role", "")),
                "execution_kind": str(stage_apply_object.get("execution_kind", "")),
                "target_cellview": dict(stage_apply_object.get("target_cellview", {}) or {}),
                "backend_applicable": bool(stage_apply_object.get("backend_applicable", False)),
                "requires_stage_specific_synthesis": bool(stage_apply_object.get("requires_stage_specific_synthesis", False)),
                "stage_apply_object": dict(stage_apply_object),
                "materialized_result": materialized,
                "materialized_summary": materialized_summary,
                "stage_contract": stage_contract,
                "stage_scope_contract": stage_scope_contract,
                "validation": validation,
            }
        )
    return tuple(decomposed)


def build_verification_repair_stage_decomposition_bundle(
    plan: VerificationRepairExecutionPlan,
    *,
    route_plan: object | None = None,
    pdk: object | None = None,
) -> dict[str, object]:
    decomposed = decompose_verification_repair_stage_proposals(
        plan,
        route_plan=route_plan,
        pdk=pdk,
    )
    return {
        "dispatch_mode": str(plan.dispatch_mode),
        "writeback_level": str(plan.writeback_level),
        "writeback_target": str(plan.writeback_target),
        "recommended_rerun": str(plan.recommended_rerun),
        "rerun_levels": tuple(plan.rerun_levels),
        "reason": str(plan.reason),
        "followup_actions": tuple(plan.followup_actions),
        "decomposition_contract": _verification_repair_decomposition_contract(plan),
        "stages": tuple(
            {
                "order": int(item.get("order", 0) or 0),
                "role": str(item.get("role", "")),
                "execution_kind": str(item.get("execution_kind", "")),
                "target_cellview": dict(item.get("target_cellview", {}) or {}),
                "backend_applicable": bool(item.get("backend_applicable", False)),
                "requires_stage_specific_synthesis": bool(item.get("requires_stage_specific_synthesis", False)),
                "materialized_summary": dict(item.get("materialized_summary", {}) or {}),
                "stage_contract": dict(item.get("stage_contract", {}) or {}),
                "stage_scope_contract": dict(item.get("stage_scope_contract", {}) or {}),
                "validation": _serialize_stage_validation(item.get("validation")),
            }
            for item in decomposed
        ),
    }


def _stage_materialization_contract(
    *,
    stage_apply_object: Mapping[str, object],
    materialized: VerificationStageSynthesisResult,
    materialized_summary: Mapping[str, object],
    validation: VerificationStageProposalValidation | None,
) -> dict[str, object]:
    target_cellview = dict(stage_apply_object.get("target_cellview", {}) or {})
    proposal_kind = ""
    if materialized_summary:
        proposal_kind = str(materialized_summary.get("kind", ""))
    if not proposal_kind:
        proposal_kind = str(stage_apply_object.get("stage_proposal_kind", ""))
    selected_plan_kind = str(materialized_summary.get("selected_plan_kind", "")) if materialized_summary else ""
    return {
        "role": str(stage_apply_object.get("role", "")),
        "execution_kind": str(stage_apply_object.get("execution_kind", "")),
        "target_cell": str(target_cellview.get("cell", "")),
        "stage_dependencies": tuple(
            int(order)
            for order in tuple(stage_apply_object.get("stage_dependencies", ()) or ())
            if int(order or 0) > 0
        ),
        "required_enclosing_reruns": tuple(
            str(level)
            for level in tuple(stage_apply_object.get("required_enclosing_reruns", ()) or ())
            if str(level)
        ),
        "supported": bool(materialized.supported),
        "proposal_kind": proposal_kind,
        "selected_plan_kind": selected_plan_kind,
        "backend_applicable": bool(stage_apply_object.get("backend_applicable", False)),
        "requires_stage_specific_synthesis": bool(stage_apply_object.get("requires_stage_specific_synthesis", False)),
        "validation_valid": None if validation is None else bool(validation.valid),
        "validation_reason": "" if validation is None else str(validation.reason),
    }


def _stage_scope_contract(stage_apply_object: Mapping[str, object]) -> dict[str, object]:
    artifact = dict(stage_apply_object.get("stage_synthesis_artifact", {}) or {})
    target_cellview = dict(stage_apply_object.get("target_cellview", {}) or {})
    stage_dispatch_plan = dict(stage_apply_object.get("stage_dispatch_plan", {}) or {})
    scope_guard = dict(stage_dispatch_plan.get("scope_guard", {}) or {})
    stage_hierarchy = dict(stage_apply_object.get("hierarchy_context", {}) or {})
    execution_profile = dict(stage_apply_object.get("execution_profile", {}) or {})
    return {
        "target_cell": str(target_cellview.get("cell", "")),
        "stage_dependencies": tuple(
            int(order)
            for order in tuple(stage_apply_object.get("stage_dependencies", ()) or ())
            if int(order or 0) > 0
        ),
        "required_enclosing_reruns": tuple(
            str(level)
            for level in tuple(stage_apply_object.get("required_enclosing_reruns", ()) or ())
            if str(level)
        ),
        "editable": bool(stage_apply_object.get("editable", False)),
        "stage_hierarchy_node": str(stage_hierarchy.get("node_name", "")),
        "stage_hierarchy_parent": str(stage_hierarchy.get("parent_node", "")),
        "stage_hierarchy_depth": int(stage_hierarchy.get("depth", 0) or 0),
        "stage_target_cellview": target_cellview,
        "allowed_scope_nets": tuple(str(net) for net in scope_guard.get("scope_nets", ()) if str(net)),
        "blocked_scope_nets": tuple(
            dict.fromkeys(
                str(net)
                for net in (
                    *tuple(scope_guard.get("avoid_nets", ()) or ()),
                    *tuple(scope_guard.get("protected_reference_nets", ()) or ()),
                    *tuple(scope_guard.get("architecture_protected_nets", ()) or ()),
                )
                if str(net)
            )
        ),
        "allowed_scope_devices": tuple(str(device) for device in scope_guard.get("scope_devices", ()) if str(device)),
        "blocked_scope_devices": tuple(str(device) for device in scope_guard.get("avoid_devices", ()) if str(device)),
        "allowed_scope_regions": tuple(str(region) for region in scope_guard.get("scope_regions", ()) if str(region)),
        "source_proposal_kind": str(artifact.get("source_proposal_kind", "")),
        "synthesis_goal": str(artifact.get("synthesis_goal", "")),
        "scope_policy": str(artifact.get("scope_policy", "")),
        "scope_nets": tuple(str(net) for net in artifact.get("scope_nets", ()) if str(net)),
        "scope_devices": tuple(str(device) for device in artifact.get("scope_devices", ()) if str(device)),
        "scope_regions": tuple(str(region) for region in artifact.get("scope_regions", ()) if str(region)),
        "restore_bus_nets": tuple(str(net) for net in artifact.get("restore_bus_nets", ()) if str(net)),
        "restore_feedback_nets": tuple(str(net) for net in artifact.get("restore_feedback_nets", ()) if str(net)),
        "protected_reference_nets": tuple(str(net) for net in artifact.get("protected_reference_nets", ()) if str(net)),
        "architecture_protected_nets": tuple(str(net) for net in artifact.get("architecture_protected_nets", ()) if str(net)),
        "binding_blocked_partitions": tuple(str(item) for item in artifact.get("binding_blocked_partitions", ()) if str(item)),
        "macro_bound_partitions": tuple(str(item) for item in artifact.get("macro_bound_partitions", ()) if str(item)),
        "architecture_budget_blocked_partitions": tuple(
            str(item) for item in artifact.get("architecture_budget_blocked_partitions", ()) if str(item)
        ),
        "system_repair_guidance": tuple(
            dict(item)
            for item in tuple(dict(scope_guard.get("metadata", {}) or {}).get("system_repair_guidance", ()) or ())
            if isinstance(item, Mapping)
        ),
        "execution_profile": execution_profile,
        "execution_class": str(execution_profile.get("execution_class", "")),
        "blocking_system_kinds": tuple(
            str(item) for item in tuple(execution_profile.get("blocking_system_kinds", ()) or ()) if str(item)
        ),
        "requires_manual_handoff": bool(execution_profile.get("requires_manual_handoff", False)),
        "requires_enclosing_context": bool(execution_profile.get("requires_enclosing_context", False)),
        "system_recommended_level": str(scope_guard.get("system_recommended_level", "")),
        "system_scope_escalation_required": bool(scope_guard.get("escalation_required", False)),
        "region_bbox": _coerce_bbox(artifact.get("region_bbox")),
        "issue_bbox": _coerce_bbox(artifact.get("issue_bbox")),
    }


def _verification_repair_decomposition_contract(
    plan: VerificationRepairExecutionPlan,
) -> dict[str, object]:
    dispatch_plan = dict(getattr(plan, "dispatch_plan", {}) or {})
    orchestration_plan = dict(dispatch_plan.get("orchestration_plan", {}) or {})
    scope_guard = dict(dispatch_plan.get("scope_guard", {}) or {})
    hierarchy_resolution = dict(dispatch_plan.get("hierarchy_resolution", {}) or {})
    target_cellview = dict(dispatch_plan.get("target_cellview", {}) or {})
    source_cellview = dict(dispatch_plan.get("source_cellview", {}) or {})
    execution_profile = _execution_profile_from_plan(plan)
    return {
        "dispatch_mode": str(getattr(plan, "dispatch_mode", "")),
        "writeback_level": str(getattr(plan, "writeback_level", "")),
        "writeback_target": str(getattr(plan, "writeback_target", "")),
        "recommended_rerun": str(getattr(plan, "recommended_rerun", "")),
        "rerun_levels": tuple(getattr(plan, "rerun_levels", ()) or ()),
        "required_enclosing_reruns": tuple(
            str(level)
            for level in tuple(getattr(plan, "rerun_levels", ()) or ())
            if str(level) and str(level) != str(getattr(plan, "recommended_rerun", ""))
        ),
        "hierarchy_mode": str(orchestration_plan.get("mode", "")),
        "hierarchy_path": tuple(orchestration_plan.get("hierarchy_path", ()) or ()),
        "hierarchy_node_path": tuple(orchestration_plan.get("hierarchy_node_path", ()) or ()),
        "hierarchy_contract": _serialize_hierarchy_resolution_contract(hierarchy_resolution),
        "stage_count": int(orchestration_plan.get("stage_count", len(tuple(orchestration_plan.get("stages", ()) or ()))) or 0),
        "requires_multi_cell_orchestration": bool(orchestration_plan.get("requires_multi_cell_orchestration", False)),
        "source_cell": str(source_cellview.get("cell", "")),
        "target_cell": str(target_cellview.get("cell", "")),
        "scope_policy": str(scope_guard.get("scope_policy", "")),
        "scope_nets": tuple(str(net) for net in scope_guard.get("scope_nets", ()) if str(net)),
        "scope_devices": tuple(str(device) for device in scope_guard.get("scope_devices", ()) if str(device)),
        "scope_regions": tuple(str(region) for region in scope_guard.get("scope_regions", ()) if str(region)),
        "restore_bus_nets": tuple(str(net) for net in scope_guard.get("restore_bus_nets", ()) if str(net)),
        "restore_feedback_nets": tuple(str(net) for net in scope_guard.get("restore_feedback_nets", ()) if str(net)),
        "protected_reference_nets": tuple(str(net) for net in scope_guard.get("protected_reference_nets", ()) if str(net)),
        "architecture_protected_nets": tuple(str(net) for net in scope_guard.get("architecture_protected_nets", ()) if str(net)),
        "binding_blocked_partitions": tuple(
            str(item) for item in scope_guard.get("binding_blocked_partitions", ()) if str(item)
        ),
        "macro_bound_partitions": tuple(
            str(item) for item in scope_guard.get("macro_bound_partitions", ()) if str(item)
        ),
        "architecture_budget_blocked_partitions": tuple(
            str(item) for item in scope_guard.get("architecture_budget_blocked_partitions", ()) if str(item)
        ),
        "system_repair_guidance": tuple(
            dict(item)
            for item in tuple(dict(scope_guard.get("metadata", {}) or {}).get("system_repair_guidance", ()) or ())
            if isinstance(item, Mapping)
        ),
        "execution_profile": execution_profile,
        "execution_class": str(execution_profile.get("execution_class", "")),
        "blocking_system_kinds": tuple(
            str(item)
            for item in tuple(execution_profile.get("blocking_system_kinds", ()) or ())
            if str(item)
        ),
        "system_recommended_level": str(scope_guard.get("system_recommended_level", "")),
        "system_scope_escalation_required": bool(scope_guard.get("escalation_required", False)),
        "region_bbox": _coerce_bbox(scope_guard.get("region_bbox")),
        "issue_bbox": _coerce_bbox(scope_guard.get("issue_bbox")),
    }


def _serialize_stage_validation(validation: VerificationStageProposalValidation | None) -> dict[str, object]:
    if validation is None:
        return {}
    return {
        "valid": bool(validation.valid),
        "reason": str(validation.reason),
        "details": dict(validation.details),
    }


def _serialize_hierarchy_resolution_contract(hierarchy_resolution: Mapping[str, object]) -> dict[str, object]:
    source_node = dict(hierarchy_resolution.get("source_node", {}) or {})
    target_node = dict(hierarchy_resolution.get("target_node", {}) or {})
    path_nodes = tuple(
        {
            "name": str(dict(node).get("name", "")),
            "lib": str(dict(node).get("lib", "")),
            "cell": str(dict(node).get("cell", "")),
            "view": str(dict(node).get("view", "")),
            "view_type": str(dict(node).get("view_type", "")),
            "parent": str(dict(node).get("parent", "")),
            "aliases": tuple(str(alias) for alias in tuple(dict(node).get("aliases", ()) or ()) if str(alias)),
        }
        for node in tuple(hierarchy_resolution.get("path_nodes", ()) or ())
        if isinstance(node, Mapping)
    )
    return {
        "mode": str(hierarchy_resolution.get("mode", "")),
        "path": tuple(str(item) for item in tuple(hierarchy_resolution.get("path", ()) or ()) if str(item)),
        "source_node": {
            "name": str(source_node.get("name", "")),
            "lib": str(source_node.get("lib", "")),
            "cell": str(source_node.get("cell", "")),
            "view": str(source_node.get("view", "")),
            "view_type": str(source_node.get("view_type", "")),
            "parent": str(source_node.get("parent", "")),
            "aliases": tuple(str(alias) for alias in tuple(source_node.get("aliases", ()) or ()) if str(alias)),
        },
        "target_node": {
            "name": str(target_node.get("name", "")),
            "lib": str(target_node.get("lib", "")),
            "cell": str(target_node.get("cell", "")),
            "view": str(target_node.get("view", "")),
            "view_type": str(target_node.get("view_type", "")),
            "parent": str(target_node.get("parent", "")),
            "aliases": tuple(str(alias) for alias in tuple(target_node.get("aliases", ()) or ()) if str(alias)),
        },
        "path_nodes": path_nodes,
    }


def build_hierarchy_cellview_index(
    hierarchy_database: object,
) -> dict[str, object]:
    nodes = _coerce_hierarchy_database(hierarchy_database)
    by_name: dict[str, dict[str, object]] = {}
    by_cell: dict[str, dict[str, object]] = {}
    alias_to_name: dict[str, str] = {}
    children: dict[str, tuple[str, ...]] = {}
    roots: list[str] = []
    for node in nodes:
        node_dict = _hierarchy_node_to_dict(node)
        by_name[node.name] = node_dict
        if node.cell:
            by_cell[node.cell] = node_dict
        for alias in node.aliases:
            if alias:
                alias_to_name[alias] = node.name
        if node.parent:
            entries = list(children.get(node.parent, ()))
            entries.append(node.name)
            children[node.parent] = tuple(dict.fromkeys(entries))
        else:
            roots.append(node.name)
    return {
        "node_count": len(nodes),
        "roots": tuple(dict.fromkeys(name for name in roots if name)),
        "by_name": by_name,
        "by_cell": by_cell,
        "alias_to_name": alias_to_name,
        "children": children,
    }


def query_hierarchy_cellview_path(
    hierarchy_database: object,
    *,
    source: object,
    target: object,
) -> dict[str, object]:
    nodes = _coerce_hierarchy_database(hierarchy_database)
    source_node = _match_hierarchy_node(nodes, source)
    target_node = _match_hierarchy_node(nodes, target)
    path_nodes = _hierarchy_path_nodes(source_node, target_node, nodes)
    return {
        "mode": "hierarchy_database" if source_node is not None or target_node is not None else "fallback",
        "source_node": _hierarchy_node_to_dict(source_node),
        "target_node": _hierarchy_node_to_dict(target_node),
        "path": _hierarchy_path(source_node, target_node, nodes),
        "path_nodes": tuple(_hierarchy_node_to_dict(node) for node in path_nodes),
        "cell_path": tuple(node.cell for node in path_nodes if node.cell),
        "depth": max(len(path_nodes) - 1, 0),
    }


def annotate_dispatch_bundle_with_hierarchy_database(
    bundle: Mapping[str, object],
    hierarchy_database: object,
) -> dict[str, object]:
    annotated = dict(bundle)
    hierarchy_contract = dict(annotated.get("hierarchy_contract", {}) or {})
    source_cellview = dict(annotated.get("source_cellview", {}) or {})
    target_cellview = dict(annotated.get("target_cellview", {}) or {})
    source = hierarchy_contract.get("source_node", {}).get("name") or source_cellview.get("cell", "")
    target = hierarchy_contract.get("target_node", {}).get("name") or target_cellview.get("cell", "")
    query = query_hierarchy_cellview_path(
        hierarchy_database,
        source=source,
        target=target,
    )
    annotated["hierarchy_contract"] = query
    orchestration_plan = dict(annotated.get("orchestration_plan", {}) or {})
    if orchestration_plan:
        updated_plan = dict(orchestration_plan)
        if not tuple(updated_plan.get("hierarchy_node_path", ()) or ()):
            updated_plan["hierarchy_node_path"] = tuple(query.get("path", ()) or ())
        if not tuple(updated_plan.get("hierarchy_path", ()) or ()):
            updated_plan["hierarchy_path"] = tuple(query.get("cell_path", ()) or ())
        annotated["orchestration_plan"] = updated_plan
    decomposition_contract = dict(annotated.get("decomposition_contract", {}) or {})
    if decomposition_contract:
        updated_contract = dict(decomposition_contract)
        updated_contract["hierarchy_contract"] = query
        if not tuple(updated_contract.get("hierarchy_node_path", ()) or ()):
            updated_contract["hierarchy_node_path"] = tuple(query.get("path", ()) or ())
        if not tuple(updated_contract.get("hierarchy_path", ()) or ()):
            updated_contract["hierarchy_path"] = tuple(query.get("cell_path", ()) or ())
        annotated["decomposition_contract"] = updated_contract
    return annotated


def query_decomposition_stage_by_cell(
    bundle: Mapping[str, object],
    *,
    cell: object,
) -> dict[str, object]:
    key = str(cell or "")
    if not key:
        return {}
    for stage in tuple(bundle.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        target_cellview = dict(stage.get("target_cellview", {}) or {})
        stage_scope = dict(stage.get("stage_scope_contract", {}) or {})
        if key in {
            str(stage.get("target_cellview", {}) and target_cellview.get("cell", "")),
            str(stage_scope.get("target_cell", "")),
        }:
            return _serialize_decomposition_stage_query_result(stage)
    return {}


def query_decomposition_stage_by_node(
    bundle: Mapping[str, object],
    *,
    node: object,
) -> dict[str, object]:
    key = str(node or "")
    if not key:
        return {}
    for stage in tuple(bundle.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        stage_scope = dict(stage.get("stage_scope_contract", {}) or {})
        if key == str(stage_scope.get("stage_hierarchy_node", "")):
            return _serialize_decomposition_stage_query_result(stage)
    return {}


def summarize_decomposition_stage_targets(
    bundle: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for stage in tuple(bundle.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        rows.append(_serialize_decomposition_stage_query_result(stage))
    return tuple(rows)


def _serialize_decomposition_stage_query_result(
    stage: Mapping[str, object],
) -> dict[str, object]:
    target_cellview = dict(stage.get("target_cellview", {}) or {})
    stage_contract = dict(stage.get("stage_contract", {}) or {})
    stage_scope = dict(stage.get("stage_scope_contract", {}) or {})
    return {
        "order": int(stage.get("order", 0) or 0),
        "role": str(stage.get("role", "")),
        "execution_kind": str(stage.get("execution_kind", "")),
        "target_cell": str(target_cellview.get("cell", stage_scope.get("target_cell", ""))),
        "target_cellview": target_cellview,
        "stage_hierarchy_node": str(stage_scope.get("stage_hierarchy_node", "")),
        "stage_hierarchy_parent": str(stage_scope.get("stage_hierarchy_parent", "")),
        "editable": bool(stage_scope.get("editable", False)),
        "proposal_kind": str(stage_contract.get("proposal_kind", "")),
        "selected_plan_kind": str(stage_contract.get("selected_plan_kind", "")),
        "backend_applicable": bool(stage.get("backend_applicable", False)),
        "requires_stage_specific_synthesis": bool(stage.get("requires_stage_specific_synthesis", False)),
        "stage_dependencies": tuple(int(order) for order in tuple(stage_scope.get("stage_dependencies", ()) or ()) if int(order or 0) > 0),
        "required_enclosing_reruns": tuple(str(level) for level in tuple(stage_scope.get("required_enclosing_reruns", ()) or ()) if str(level)),
    }


def build_partition_hierarchy_execution_map(
    *,
    hierarchy_database: object,
    implementation_intent: Mapping[str, object] | None = None,
    execution_scope: Mapping[str, object] | None = None,
    partition_node_map: Mapping[str, object] | None = None,
) -> dict[str, object]:
    nodes = _coerce_hierarchy_database(hierarchy_database)
    intent_partitions = tuple(dict(item) for item in tuple(dict(implementation_intent or {}).get("partitions", ()) or ()) if isinstance(item, Mapping))
    scope_row = dict(execution_scope or {})
    explicit_partition_map = {
        str(name): tuple(str(value) for value in tuple(values if isinstance(values, (tuple, list)) else (values,)) if str(value))
        for name, values in dict(partition_node_map or {}).items()
        if str(name)
    }
    rows: list[dict[str, object]] = []
    for partition in intent_partitions:
        name = str(partition.get("name", ""))
        if not name:
            continue
        matched_nodes = _match_partition_hierarchy_nodes(name, nodes, explicit_partition_map.get(name, ()))
        matched_node_dicts = tuple(_hierarchy_node_to_dict(node) for node in matched_nodes)
        rows.append(
            {
                "partition": name,
                "role": str(partition.get("role", "")),
                "implementation_class": str(partition.get("implementation_class", "")),
                "primitive_family": str(partition.get("primitive_family", "")),
                "keep_stable": name in {str(item) for item in tuple(scope_row.get("keep_stable_partitions", ()) or ()) if str(item)},
                "retarget_changed": name in {str(item) for item in tuple(scope_row.get("retarget_changed_partitions", ()) or ()) if str(item)},
                "focus": name in {str(item) for item in tuple(scope_row.get("focus_partitions", ()) or ()) if str(item)},
                "devices": tuple(str(item) for item in tuple(partition.get("devices", ()) or ()) if str(item)),
                "nets": tuple(str(item) for item in tuple(partition.get("nets", ()) or ()) if str(item)),
                "matched_hierarchy_nodes": matched_node_dicts,
                "matched_cells": tuple(
                    dict.fromkeys(
                        str(node.get("cell", ""))
                        for node in matched_node_dicts
                        if str(node.get("cell", ""))
                    )
                ),
                "editable_cells": tuple(
                    str(node.get("cell", ""))
                    for node in matched_node_dicts
                    if str(node.get("cell", "")) and not bool(partition.get("keep_stable", False))
                ),
            }
        )
    return {
        "partition_count": len(rows),
        "mapped_partition_count": sum(1 for row in rows if row["matched_hierarchy_nodes"]),
        "partitions": tuple(rows),
    }


def query_partition_execution_targets(
    partition_execution_map: Mapping[str, object],
    *,
    partition: object,
) -> dict[str, object]:
    key = str(partition or "")
    if not key:
        return {}
    for row in tuple(partition_execution_map.get("partitions", ()) or ()):
        if not isinstance(row, Mapping):
            continue
        if key == str(row.get("partition", "")):
            return dict(row)
    return {}


def annotate_decomposition_bundle_with_partition_execution_map(
    bundle: Mapping[str, object],
    partition_execution_map: Mapping[str, object],
) -> dict[str, object]:
    annotated = dict(bundle)
    partitions = tuple(dict(item) for item in tuple(partition_execution_map.get("partitions", ()) or ()) if isinstance(item, Mapping))
    stages: list[dict[str, object]] = []
    for stage in tuple(annotated.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        stage_scope = dict(stage.get("stage_scope_contract", {}) or {})
        stage_nets = {str(net) for net in tuple(stage_scope.get("scope_nets", ()) or ()) if str(net)}
        stage_devices = {str(device) for device in tuple(stage_scope.get("scope_devices", ()) or ()) if str(device)}
        matched = []
        for row in partitions:
            row_nets = {str(net) for net in tuple(row.get("nets", ()) or ()) if str(net)}
            row_devices = {str(device) for device in tuple(row.get("devices", ()) or ()) if str(device)}
            if (stage_nets and stage_nets & row_nets) or (stage_devices and stage_devices & row_devices):
                matched.append(
                    {
                        "partition": str(row.get("partition", "")),
                        "role": str(row.get("role", "")),
                        "implementation_class": str(row.get("implementation_class", "")),
                        "matched_cells": tuple(row.get("matched_cells", ()) or ()),
                        "keep_stable": bool(row.get("keep_stable", False)),
                        "retarget_changed": bool(row.get("retarget_changed", False)),
                    }
                )
        updated_stage = dict(stage)
        updated_stage["partition_execution_targets"] = tuple(matched)
        stages.append(updated_stage)
    annotated["stages"] = tuple(stages)
    annotated["partition_execution_map"] = {
        "partition_count": int(partition_execution_map.get("partition_count", 0) or 0),
        "mapped_partition_count": int(partition_execution_map.get("mapped_partition_count", 0) or 0),
    }
    return annotated


def annotate_decomposition_bundle_with_system_contract(
    bundle: Mapping[str, object],
    system_contract: Mapping[str, object],
) -> dict[str, object]:
    annotated = dict(bundle)
    stages: list[dict[str, object]] = []
    for stage in tuple(annotated.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        stage_scope = dict(stage.get("stage_scope_contract", {}) or {})
        stage_nets = {str(net) for net in tuple(stage_scope.get("scope_nets", ()) or ()) if str(net)}
        stage_nodes = {
            str(stage_scope.get("stage_hierarchy_node", "")),
            str(stage_scope.get("stage_hierarchy_parent", "")),
        }
        stage_nodes.discard("")
        stage_targets = _system_contract_targets_for_stage(
            stage_nets=stage_nets,
            stage_nodes=stage_nodes,
            system_contract=system_contract,
        )
        updated_stage = dict(stage)
        updated_stage["system_contract_targets"] = stage_targets
        stages.append(updated_stage)
    annotated["stages"] = tuple(stages)
    annotated["system_contract_summary"] = {
        "interface_contract_count": len(tuple(system_contract.get("interface_contracts", ()) or ())),
        "bus_contract_count": len(tuple(system_contract.get("bus_contracts", ()) or ())),
        "reference_path_count": len(tuple(system_contract.get("reference_paths", ()) or ())),
        "feedback_contract_count": len(tuple(system_contract.get("feedback_contracts", ()) or ())),
        "timing_chain_count": len(tuple(system_contract.get("timing_chains", ()) or ())),
    }
    return annotated


def build_hierarchical_verification_stage_bundle(
    verification_intent: Mapping[str, object],
) -> dict[str, object]:
    """Lower hierarchical verification intent into a serializable stage bundle."""

    intent_row = dict(verification_intent or {})
    stages: list[dict[str, object]] = []
    for order, stage in enumerate(tuple(intent_row.get("stages", ()) or ()), start=1):
        if not isinstance(stage, Mapping):
            continue
        row = dict(stage)
        partition = str(row.get("partition", ""))
        verification_view = str(row.get("verification_view", ""))
        execution_kind = "target_writeback" if verification_view == "primitive_graph" else "stage_preparation"
        backend_applicable = verification_view == "primitive_graph"
        requires_stage_specific_synthesis = verification_view == "macro_boundary"
        system_targets = tuple(dict(item) for item in tuple(row.get("system_targets", ()) or ()) if isinstance(item, Mapping))
        partition_targets = (
            {
                "partition": partition,
                "role": str(row.get("role", "")),
                "implementation_class": str(row.get("implementation_class", "")),
                "matched_cells": (),
                "keep_stable": False,
                "retarget_changed": False,
            },
        )
        required_reruns = ("rerun_partition_verification",) if system_targets else ()
        stages.append(
            {
                "order": order,
                "role": "target" if backend_applicable else "intermediate",
                "execution_kind": execution_kind,
                "target_cellview": {"cell": partition},
                "backend_applicable": backend_applicable,
                "requires_stage_specific_synthesis": requires_stage_specific_synthesis,
                "stage_contract": {
                    "proposal_kind": verification_view,
                    "selected_plan_kind": verification_view,
                },
                "stage_scope_contract": {
                    "target_cell": partition,
                    "stage_hierarchy_node": partition,
                    "stage_hierarchy_parent": "",
                    "editable": backend_applicable,
                    "stage_dependencies": (),
                    "required_enclosing_reruns": required_reruns,
                    "scope_nets": tuple(str(net) for net in tuple(row.get("required_nets", ()) or ()) if str(net)),
                    "scope_devices": tuple(str(device) for device in tuple(row.get("exposed_pins", ()) or ()) if str(device)),
                    "scope_regions": (partition,) if partition else (),
                },
                "partition_execution_targets": partition_targets,
                "system_contract_targets": system_targets,
                "verification_intent": {
                    "verification_focus": str(row.get("verification_focus", "")),
                    "verification_checks": tuple(str(item) for item in tuple(row.get("verification_checks", ()) or ()) if str(item)),
                    "restore_sensitive": bool(row.get("restore_sensitive", False)),
                    "reference_sensitive": bool(row.get("reference_sensitive", False)),
                    "timing_sensitive": bool(row.get("timing_sensitive", False)),
                },
            }
        )
    return {
        "topology_name": str(intent_row.get("topology_name", "")),
        "stages": tuple(stages),
        "verification_intent_summary": tuple(str(item) for item in tuple(intent_row.get("summary", ()) or ()) if str(item)),
        "verification_intent_provenance": dict(intent_row.get("provenance", {}) or {}),
    }


def query_stage_system_contract_targets(
    bundle: Mapping[str, object],
    *,
    cell: object | None = None,
    node: object | None = None,
) -> dict[str, object]:
    stage = {}
    if cell is not None:
        stage = query_decomposition_stage_by_cell(bundle, cell=cell)
    elif node is not None:
        stage = query_decomposition_stage_by_node(bundle, node=node)
    if not stage:
        return {}
    for raw_stage in tuple(bundle.get("stages", ()) or ()):
        if not isinstance(raw_stage, Mapping):
            continue
        if int(raw_stage.get("order", 0) or 0) == int(stage.get("order", 0) or 0):
            return {
                **stage,
                "system_contract_targets": tuple(raw_stage.get("system_contract_targets", ()) or ()),
            }
    return {}


def build_stage_verification_contract(
    stage: Mapping[str, object],
) -> dict[str, object]:
    target_cellview = dict(stage.get("target_cellview", {}) or {})
    stage_contract = dict(stage.get("stage_contract", {}) or {})
    stage_scope = dict(stage.get("stage_scope_contract", {}) or {})
    partition_targets = tuple(dict(item) for item in tuple(stage.get("partition_execution_targets", ()) or ()) if isinstance(item, Mapping))
    system_targets = tuple(dict(item) for item in tuple(stage.get("system_contract_targets", ()) or ()) if isinstance(item, Mapping))
    return {
        "order": int(stage.get("order", 0) or 0),
        "role": str(stage.get("role", "")),
        "execution_kind": str(stage.get("execution_kind", "")),
        "target_cell": str(target_cellview.get("cell", stage_scope.get("target_cell", ""))),
        "target_cellview": target_cellview,
        "stage_hierarchy_node": str(stage_scope.get("stage_hierarchy_node", "")),
        "editable": bool(stage_scope.get("editable", False)),
        "proposal_kind": str(stage_contract.get("proposal_kind", "")),
        "selected_plan_kind": str(stage_contract.get("selected_plan_kind", "")),
        "backend_applicable": bool(stage.get("backend_applicable", False)),
        "requires_stage_specific_synthesis": bool(stage.get("requires_stage_specific_synthesis", False)),
        "stage_dependencies": tuple(int(order) for order in tuple(stage_scope.get("stage_dependencies", ()) or ()) if int(order or 0) > 0),
        "required_enclosing_reruns": tuple(str(level) for level in tuple(stage_scope.get("required_enclosing_reruns", ()) or ()) if str(level)),
        "scope_nets": tuple(str(net) for net in tuple(stage_scope.get("scope_nets", ()) or ()) if str(net)),
        "scope_devices": tuple(str(device) for device in tuple(stage_scope.get("scope_devices", ()) or ()) if str(device)),
        "scope_regions": tuple(str(region) for region in tuple(stage_scope.get("scope_regions", ()) or ()) if str(region)),
        "binding_blocked_partitions": tuple(
            str(item) for item in tuple(stage_scope.get("binding_blocked_partitions", ()) or ()) if str(item)
        ),
        "macro_bound_partitions": tuple(
            str(item) for item in tuple(stage_scope.get("macro_bound_partitions", ()) or ()) if str(item)
        ),
        "architecture_budget_blocked_partitions": tuple(
            str(item)
            for item in tuple(stage_scope.get("architecture_budget_blocked_partitions", ()) or ())
            if str(item)
        ),
        "partition_targets": partition_targets,
        "system_targets": system_targets,
        "verification_focus": {
            "partition_count": len(partition_targets),
            "system_target_count": len(system_targets),
            "focus_partition_count": sum(1 for item in partition_targets if bool(item.get("retarget_changed", False))),
            "restore_contract_count": sum(1 for item in system_targets if bool(item.get("restore_required", False))),
            "reference_contract_count": sum(1 for item in system_targets if str(item.get("kind", "")) == "reference_path"),
            "timing_contract_count": sum(1 for item in system_targets if str(item.get("kind", "")) == "timing_chain"),
        },
    }


def summarize_stage_verification_contracts(
    bundle: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    return tuple(
        build_stage_verification_contract(stage)
        for stage in tuple(bundle.get("stages", ()) or ())
        if isinstance(stage, Mapping)
    )


def query_stage_verification_contract(
    bundle: Mapping[str, object],
    *,
    cell: object | None = None,
    node: object | None = None,
) -> dict[str, object]:
    if cell is not None:
        stage = query_decomposition_stage_by_cell(bundle, cell=cell)
    elif node is not None:
        stage = query_decomposition_stage_by_node(bundle, node=node)
    else:
        stage = {}
    if not stage:
        return {}
    for raw_stage in tuple(bundle.get("stages", ()) or ()):
        if not isinstance(raw_stage, Mapping):
            continue
        if int(raw_stage.get("order", 0) or 0) == int(stage.get("order", 0) or 0):
            return build_stage_verification_contract(raw_stage)
    return {}


def build_stage_foundry_readiness_contract(
    stage_verification_contract: Mapping[str, object],
    foundry_execution_contract: Mapping[str, object],
) -> dict[str, object]:
    foundry = dict(foundry_execution_contract or {})
    stage = dict(stage_verification_contract or {})
    ready_stages = {str(name) for name in tuple(foundry.get("ready_stages", ()) or ()) if str(name)}
    blocked_stages = {str(name) for name in tuple(foundry.get("blocked_stages", ()) or ()) if str(name)}
    scope_nets = tuple(str(net) for net in tuple(stage.get("scope_nets", ()) or ()) if str(net))
    system_targets = tuple(dict(item) for item in tuple(stage.get("system_targets", ()) or ()) if isinstance(item, Mapping))
    partition_targets = tuple(dict(item) for item in tuple(stage.get("partition_targets", ()) or ()) if isinstance(item, Mapping))
    stage_partitions = {
        str(item.get("partition", ""))
        for item in partition_targets
        if str(item.get("partition", ""))
    }
    stage_partitions.update(
        str(item)
        for item in tuple(stage.get("binding_blocked_partitions", ()) or ())
        if str(item)
    )
    stage_partitions.update(
        str(item)
        for item in tuple(stage.get("macro_bound_partitions", ()) or ())
        if str(item)
    )
    stage_partitions.update(
        str(item)
        for item in tuple(stage.get("architecture_budget_blocked_partitions", ()) or ())
        if str(item)
    )
    binding_summary = dict(foundry.get("hierarchy_binding_summary", {}) or {})
    global_binding_blocked = {
        str(item)
        for item in tuple(binding_summary.get("binding_blocked_partitions", ()) or ())
        if str(item)
    }
    global_macro_binding = {
        str(item)
        for item in tuple(binding_summary.get("macro_binding_partitions", ()) or ())
        if str(item)
    }
    global_budget_blocked = {
        str(item)
        for item in tuple(binding_summary.get("architecture_budget_blocked_partitions", ()) or ())
        if str(item)
    }
    binding_blocked_partitions = tuple(sorted(stage_partitions & global_binding_blocked))
    macro_bound_partitions = tuple(
        sorted(
            (stage_partitions & global_macro_binding)
            | {str(item) for item in tuple(stage.get("macro_bound_partitions", ()) or ()) if str(item)}
        )
    )
    architecture_budget_blocked_partitions = tuple(sorted(stage_partitions & global_budget_blocked))
    restore_required = any(bool(item.get("restore_required", False)) for item in system_targets)
    reference_sensitive = any(str(item.get("kind", "")) == "reference_path" for item in system_targets)
    stage_type = str(stage.get("proposal_kind", "") or "")
    required_checks = _stage_required_foundry_checks(stage_type, stage, restore_required=restore_required, reference_sensitive=reference_sensitive)
    local_ready = tuple(name for name in required_checks if name in ready_stages)
    local_blocked = tuple(name for name in required_checks if name in blocked_stages or name not in ready_stages)
    issues: list[str] = []
    if restore_required:
        issues.append("stage requires restore-sensitive system contract review before foundry signoff")
    if reference_sensitive:
        issues.append("stage touches reference-sensitive nets that should be preserved through foundry verification")
    if any(bool(item.get("retarget_changed", False)) for item in partition_targets):
        issues.append("stage modifies retarget-changed partitions and should rerun enclosing verification")
    if binding_blocked_partitions:
        issues.append(
            "stage intersects partitions still blocked on hierarchical PDK binding: "
            + ", ".join(binding_blocked_partitions)
        )
    if architecture_budget_blocked_partitions:
        issues.append(
            "stage intersects partitions still missing architecture budget coverage: "
            + ", ".join(architecture_budget_blocked_partitions)
        )
    return {
        "order": int(stage.get("order", 0) or 0),
        "target_cell": str(stage.get("target_cell", "")),
        "proposal_kind": stage_type,
        "required_checks": required_checks,
        "ready_checks": local_ready,
        "blocked_checks": local_blocked,
        "ready": not local_blocked and not binding_blocked_partitions and not architecture_budget_blocked_partitions,
        "required_enclosing_reruns": tuple(str(level) for level in tuple(stage.get("required_enclosing_reruns", ()) or ()) if str(level)),
        "scope_nets": scope_nets,
        "restore_required": restore_required,
        "reference_sensitive": reference_sensitive,
        "binding_blocked_partitions": binding_blocked_partitions,
        "macro_bound_partitions": macro_bound_partitions,
        "architecture_budget_blocked_partitions": architecture_budget_blocked_partitions,
        "issues": tuple(dict.fromkeys(issues)),
    }


def summarize_stage_foundry_readiness_contracts(
    stage_contracts: tuple[Mapping[str, object], ...] | list[Mapping[str, object]],
    foundry_execution_contract: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    return tuple(
        build_stage_foundry_readiness_contract(stage, foundry_execution_contract)
        for stage in stage_contracts
        if isinstance(stage, Mapping)
    )


def query_stage_foundry_readiness_contract(
    bundle: Mapping[str, object],
    foundry_execution_contract: Mapping[str, object],
    *,
    cell: object | None = None,
    node: object | None = None,
) -> dict[str, object]:
    stage = query_stage_verification_contract(bundle, cell=cell, node=node)
    if not stage:
        return {}
    return build_stage_foundry_readiness_contract(stage, foundry_execution_contract)


def build_multi_stage_verification_matrix(
    bundle: Mapping[str, object],
    foundry_execution_contract: Mapping[str, object],
) -> dict[str, object]:
    stage_contracts = summarize_stage_verification_contracts(bundle)
    readiness_contracts = summarize_stage_foundry_readiness_contracts(stage_contracts, foundry_execution_contract)
    readiness_by_order = {
        int(contract.get("order", 0) or 0): dict(contract)
        for contract in readiness_contracts
        if isinstance(contract, Mapping)
    }
    rows: list[dict[str, object]] = []
    blocked_checks: list[str] = []
    ready_stage_count = 0
    editable_stage_count = 0
    synthesis_required_stage_count = 0
    rerun_stage_count = 0
    for stage in stage_contracts:
        if not isinstance(stage, Mapping):
            continue
        order = int(stage.get("order", 0) or 0)
        readiness = readiness_by_order.get(order, {})
        if bool(readiness.get("ready", False)):
            ready_stage_count += 1
        if bool(stage.get("editable", False)):
            editable_stage_count += 1
        if bool(stage.get("requires_stage_specific_synthesis", False)):
            synthesis_required_stage_count += 1
        if tuple(stage.get("required_enclosing_reruns", ()) or ()):
            rerun_stage_count += 1
        blocked_checks.extend(
            str(name)
            for name in tuple(readiness.get("blocked_checks", ()) or ())
            if str(name)
        )
        rows.append(
            {
                "order": order,
                "role": str(stage.get("role", "")),
                "target_cell": str(stage.get("target_cell", "")),
                "target_cellview": dict(stage.get("target_cellview", {}) or {}),
                "stage_hierarchy_node": str(stage.get("stage_hierarchy_node", "")),
                "editable": bool(stage.get("editable", False)),
                "proposal_kind": str(stage.get("proposal_kind", "")),
                "selected_plan_kind": str(stage.get("selected_plan_kind", "")),
                "backend_applicable": bool(stage.get("backend_applicable", False)),
                "requires_stage_specific_synthesis": bool(stage.get("requires_stage_specific_synthesis", False)),
                "stage_dependencies": tuple(int(item) for item in tuple(stage.get("stage_dependencies", ()) or ()) if int(item or 0) > 0),
                "required_enclosing_reruns": tuple(str(level) for level in tuple(stage.get("required_enclosing_reruns", ()) or ()) if str(level)),
                "scope_nets": tuple(str(net) for net in tuple(stage.get("scope_nets", ()) or ()) if str(net)),
                "scope_devices": tuple(str(device) for device in tuple(stage.get("scope_devices", ()) or ()) if str(device)),
                "scope_regions": tuple(str(region) for region in tuple(stage.get("scope_regions", ()) or ()) if str(region)),
                "partition_targets": tuple(dict(item) for item in tuple(stage.get("partition_targets", ()) or ()) if isinstance(item, Mapping)),
                "system_targets": tuple(dict(item) for item in tuple(stage.get("system_targets", ()) or ()) if isinstance(item, Mapping)),
                "verification_focus": dict(stage.get("verification_focus", {}) or {}),
                "foundry_readiness": {
                    "required_checks": tuple(str(name) for name in tuple(readiness.get("required_checks", ()) or ()) if str(name)),
                    "ready_checks": tuple(str(name) for name in tuple(readiness.get("ready_checks", ()) or ()) if str(name)),
                    "blocked_checks": tuple(str(name) for name in tuple(readiness.get("blocked_checks", ()) or ()) if str(name)),
                    "ready": bool(readiness.get("ready", False)),
                    "restore_required": bool(readiness.get("restore_required", False)),
                    "reference_sensitive": bool(readiness.get("reference_sensitive", False)),
                    "binding_blocked_partitions": tuple(
                        str(item) for item in tuple(readiness.get("binding_blocked_partitions", ()) or ()) if str(item)
                    ),
                    "macro_bound_partitions": tuple(
                        str(item) for item in tuple(readiness.get("macro_bound_partitions", ()) or ()) if str(item)
                    ),
                    "architecture_budget_blocked_partitions": tuple(
                        str(item)
                        for item in tuple(readiness.get("architecture_budget_blocked_partitions", ()) or ())
                        if str(item)
                    ),
                    "issues": tuple(str(item) for item in tuple(readiness.get("issues", ()) or ()) if str(item)),
                },
            }
        )
    rows.sort(key=lambda item: (int(item.get("order", 0) or 0), str(item.get("target_cell", ""))))
    total_stage_count = len(rows)
    return {
        "stages": tuple(rows),
        "summary": {
            "total_stage_count": total_stage_count,
            "ready_stage_count": ready_stage_count,
            "blocked_stage_count": max(total_stage_count - ready_stage_count, 0),
            "editable_stage_count": editable_stage_count,
            "synthesis_required_stage_count": synthesis_required_stage_count,
            "rerun_stage_count": rerun_stage_count,
            "blocked_checks": tuple(dict.fromkeys(blocked_checks)),
        },
    }


def query_multi_stage_verification_matrix(
    bundle: Mapping[str, object],
    foundry_execution_contract: Mapping[str, object],
    *,
    cell: object | None = None,
    node: object | None = None,
) -> dict[str, object]:
    matrix = build_multi_stage_verification_matrix(bundle, foundry_execution_contract)
    if cell is None and node is None:
        return matrix
    target_cell = str(cell) if cell is not None else ""
    target_node = str(node) if node is not None else ""
    for stage in tuple(matrix.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        if target_cell and str(stage.get("target_cell", "")) == target_cell:
            return dict(stage)
        if target_node and str(stage.get("stage_hierarchy_node", "")) == target_node:
            return dict(stage)
    return {}


def build_multi_cell_writeback_contract(
    bundle: Mapping[str, object],
    foundry_execution_contract: Mapping[str, object] | None = None,
) -> dict[str, object]:
    decomposition_contract = dict(bundle.get("decomposition_contract", {}) or {})
    foundry = dict(foundry_execution_contract or {})
    rows: list[dict[str, object]] = []
    direct_writeback_stage_count = 0
    synthesis_required_stage_count = 0
    editable_stage_count = 0
    blocked_stage_count = 0
    target_cells: list[str] = []
    for raw_stage in tuple(bundle.get("stages", ()) or ()):
        if not isinstance(raw_stage, Mapping):
            continue
        stage = build_stage_verification_contract(raw_stage)
        stage_scope = dict(raw_stage.get("stage_scope_contract", {}) or {})
        materialized_summary = dict(raw_stage.get("materialized_summary", {}) or {})
        validation = dict(raw_stage.get("validation", {}) or {})
        validation_valid = validation.get("valid")
        validation_ok = True if validation_valid is None else bool(validation_valid)
        backend_applicable = bool(stage.get("backend_applicable", False))
        requires_synthesis = bool(stage.get("requires_stage_specific_synthesis", False))
        writeback_ready = backend_applicable and not requires_synthesis and validation_ok
        if writeback_ready:
            direct_writeback_stage_count += 1
        if requires_synthesis:
            synthesis_required_stage_count += 1
        if bool(stage.get("editable", False)):
            editable_stage_count += 1
        row: dict[str, object] = {
            "order": int(stage.get("order", 0) or 0),
            "role": str(stage.get("role", "")),
            "execution_kind": str(stage.get("execution_kind", "")),
            "target_cell": str(stage.get("target_cell", "")),
            "target_cellview": dict(stage.get("target_cellview", {}) or {}),
            "stage_hierarchy_node": str(stage.get("stage_hierarchy_node", "")),
            "stage_hierarchy_parent": str(stage_scope.get("stage_hierarchy_parent", "")),
            "editable": bool(stage.get("editable", False)),
            "backend_applicable": backend_applicable,
            "requires_stage_specific_synthesis": requires_synthesis,
            "writeback_ready": writeback_ready,
            "stage_dependencies": tuple(int(item) for item in tuple(stage.get("stage_dependencies", ()) or ()) if int(item or 0) > 0),
            "required_enclosing_reruns": tuple(str(level) for level in tuple(stage.get("required_enclosing_reruns", ()) or ()) if str(level)),
            "proposal_kind": str(stage.get("proposal_kind", "")),
            "selected_plan_kind": str(stage.get("selected_plan_kind", "")),
            "materialized_kind": str(materialized_summary.get("kind", "")),
            "allowed_scope_nets": tuple(str(net) for net in tuple(stage_scope.get("allowed_scope_nets", ()) or ()) if str(net)),
            "blocked_scope_nets": tuple(str(net) for net in tuple(stage_scope.get("blocked_scope_nets", ()) or ()) if str(net)),
            "allowed_scope_devices": tuple(str(device) for device in tuple(stage_scope.get("allowed_scope_devices", ()) or ()) if str(device)),
            "blocked_scope_devices": tuple(str(device) for device in tuple(stage_scope.get("blocked_scope_devices", ()) or ()) if str(device)),
            "allowed_scope_regions": tuple(str(region) for region in tuple(stage_scope.get("allowed_scope_regions", ()) or ()) if str(region)),
            "scope_nets": tuple(str(net) for net in tuple(stage.get("scope_nets", ()) or ()) if str(net)),
            "scope_devices": tuple(str(device) for device in tuple(stage.get("scope_devices", ()) or ()) if str(device)),
            "scope_regions": tuple(str(region) for region in tuple(stage.get("scope_regions", ()) or ()) if str(region)),
            "protected_reference_nets": tuple(str(net) for net in tuple(stage_scope.get("protected_reference_nets", ()) or ()) if str(net)),
            "architecture_protected_nets": tuple(str(net) for net in tuple(stage_scope.get("architecture_protected_nets", ()) or ()) if str(net)),
            "binding_blocked_partitions": tuple(
                str(item) for item in tuple(stage_scope.get("binding_blocked_partitions", ()) or ()) if str(item)
            ),
            "macro_bound_partitions": tuple(
                str(item) for item in tuple(stage_scope.get("macro_bound_partitions", ()) or ()) if str(item)
            ),
            "architecture_budget_blocked_partitions": tuple(
                str(item)
                for item in tuple(stage_scope.get("architecture_budget_blocked_partitions", ()) or ())
                if str(item)
            ),
            "system_recommended_level": str(stage_scope.get("system_recommended_level", "")),
            "system_scope_escalation_required": bool(stage_scope.get("system_scope_escalation_required", False)),
            "validation": {
                "valid": None if validation_valid is None else bool(validation_valid),
                "reason": str(validation.get("reason", "")),
                "details": dict(validation.get("details", {}) or {}),
            },
            "verification_focus": dict(stage.get("verification_focus", {}) or {}),
        }
        if foundry:
            readiness = build_stage_foundry_readiness_contract(stage, foundry)
            row["foundry_readiness"] = readiness
            row["binding_blocked_partitions"] = tuple(
                str(item) for item in tuple(readiness.get("binding_blocked_partitions", ()) or ()) if str(item)
            )
            row["macro_bound_partitions"] = tuple(
                str(item) for item in tuple(readiness.get("macro_bound_partitions", ()) or ()) if str(item)
            )
            row["architecture_budget_blocked_partitions"] = tuple(
                str(item)
                for item in tuple(readiness.get("architecture_budget_blocked_partitions", ()) or ())
                if str(item)
            )
            if not bool(readiness.get("ready", False)):
                blocked_stage_count += 1
        elif not writeback_ready:
            blocked_stage_count += 1
        target_cell = str(row.get("target_cell", ""))
        if target_cell:
            target_cells.append(target_cell)
        rows.append(row)
    rows.sort(key=lambda item: (int(item.get("order", 0) or 0), str(item.get("target_cell", ""))))
    return {
        "writeback_contract": {
            "dispatch_mode": str(bundle.get("dispatch_mode", decomposition_contract.get("dispatch_mode", ""))),
            "writeback_level": str(bundle.get("writeback_level", decomposition_contract.get("writeback_level", ""))),
            "writeback_target": str(bundle.get("writeback_target", decomposition_contract.get("writeback_target", ""))),
            "recommended_rerun": str(bundle.get("recommended_rerun", decomposition_contract.get("recommended_rerun", ""))),
            "rerun_levels": tuple(str(level) for level in tuple(bundle.get("rerun_levels", decomposition_contract.get("rerun_levels", ())) or ()) if str(level)),
            "hierarchy_mode": str(decomposition_contract.get("hierarchy_mode", "")),
            "hierarchy_path": tuple(str(item) for item in tuple(decomposition_contract.get("hierarchy_path", ()) or ()) if str(item)),
            "hierarchy_node_path": tuple(str(item) for item in tuple(decomposition_contract.get("hierarchy_node_path", ()) or ()) if str(item)),
            "requires_multi_cell_writeback": bool(decomposition_contract.get("requires_multi_cell_orchestration", False)),
            "source_cell": str(decomposition_contract.get("source_cell", "")),
            "target_cell": str(decomposition_contract.get("target_cell", "")),
        },
        "stages": tuple(rows),
        "summary": {
            "total_stage_count": len(rows),
            "direct_writeback_stage_count": direct_writeback_stage_count,
            "synthesis_required_stage_count": synthesis_required_stage_count,
            "editable_stage_count": editable_stage_count,
            "blocked_stage_count": blocked_stage_count,
            "target_cells": tuple(dict.fromkeys(target_cells)),
        },
    }


def query_multi_cell_writeback_target(
    bundle: Mapping[str, object],
    foundry_execution_contract: Mapping[str, object] | None = None,
    *,
    cell: object | None = None,
    node: object | None = None,
) -> dict[str, object]:
    contract = build_multi_cell_writeback_contract(bundle, foundry_execution_contract)
    if cell is None and node is None:
        return contract
    target_cell = str(cell) if cell is not None else ""
    target_node = str(node) if node is not None else ""
    for stage in tuple(contract.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        if target_cell and str(stage.get("target_cell", "")) == target_cell:
            return dict(stage)
        if target_node and str(stage.get("stage_hierarchy_node", "")) == target_node:
            return dict(stage)
    return {}


def _hierarchical_system_regression_report_row(
    report: Mapping[str, object] | None,
) -> dict[str, object]:
    return {str(key): value for key, value in dict(report or {}).items()} if isinstance(report, Mapping) else {}


def _stage_system_regression_target_names(
    verification: Mapping[str, object],
    writeback: Mapping[str, object],
    *,
    target_cell: str,
    stage_hierarchy_node: str,
) -> tuple[str, ...]:
    names: list[str] = []
    if target_cell:
        names.append(target_cell)
    if stage_hierarchy_node and stage_hierarchy_node not in names:
        names.append(stage_hierarchy_node)
    for item in tuple(verification.get("partition_targets", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        partition = str(item.get("partition", ""))
        if partition and partition not in names:
            names.append(partition)
    for item in tuple(writeback.get("partition_targets", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        partition = str(item.get("partition", ""))
        if partition and partition not in names:
            names.append(partition)
    return tuple(names)


def _stage_system_target_signatures(verification: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    signatures: list[tuple[str, str]] = []
    for item in tuple(verification.get("system_targets", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind", ""))
        name = ""
        if kind in {"feedback_contract", "reference_path"}:
            name = str(item.get("net", ""))
        elif kind == "timing_chain":
            name = str(item.get("name", ""))
        elif kind == "bus_contract":
            name = str(item.get("name", ""))
        if not kind or not name:
            continue
        normalized_kind = {
            "feedback_contract": "feedback_loop",
            "bus_contract": "bus_corridor",
        }.get(kind, kind)
        signature = (normalized_kind, name)
        if signature not in signatures:
            signatures.append(signature)
    return tuple(signatures)


def _system_regression_check_matches_stage(
    check: Mapping[str, object],
    stage_targets: tuple[str, ...],
    stage_signatures: tuple[tuple[str, str], ...],
) -> bool:
    affected = {
        str(item)
        for item in tuple(check.get("affected_partitions", ()) or ())
        if str(item)
    }
    if affected and affected & set(stage_targets):
        return True
    signature = (str(check.get("kind", "")), str(check.get("name", "")))
    return bool(signature[0] and signature[1] and signature in stage_signatures)


def _build_stage_system_regression_view(
    stage: Mapping[str, object],
    system_regression: Mapping[str, object],
) -> dict[str, object]:
    if not system_regression:
        return {}
    verification = dict(stage.get("verification", {}) or {})
    writeback = dict(stage.get("writeback", {}) or {})
    target_cell = str(stage.get("target_cell", ""))
    stage_hierarchy_node = str(stage.get("stage_hierarchy_node", ""))
    stage_targets = _stage_system_regression_target_names(
        verification,
        writeback,
        target_cell=target_cell,
        stage_hierarchy_node=stage_hierarchy_node,
    )
    stage_signatures = _stage_system_target_signatures(verification)
    partition_insights = tuple(
        dict(item)
        for item in tuple(system_regression.get("partition_insights", ()) or ())
        if isinstance(item, Mapping)
        and str(item.get("partition", "")) in set(stage_targets)
    )
    checks = tuple(
        dict(item)
        for item in tuple(system_regression.get("contract_checks", ()) or ())
        if isinstance(item, Mapping) and _system_regression_check_matches_stage(item, stage_targets, stage_signatures)
    )
    applies = bool(partition_insights or checks)
    failing_checks = tuple(item for item in checks if not bool(item.get("passed", False)))
    restore_sensitive_partition_count = sum(1 for item in partition_insights if bool(item.get("restore_sensitive", False)))
    implementation_blocked_partition_count = sum(1 for item in partition_insights if not bool(item.get("implementation_ready", True)))
    pcell_binding_blocked_partition_count = sum(1 for item in partition_insights if not bool(item.get("pcell_binding_ready", True)))
    retarget_focus_score_total = sum(int(item.get("retarget_focus_score", 0) or 0) for item in partition_insights)
    retarget_actions = tuple(
        dict.fromkeys(
            str(action)
            for item in partition_insights
            for action in tuple(item.get("retarget_actions", ()) or ())
            if str(action)
        )
    )
    failing_check_kinds = tuple(
        dict.fromkeys(str(item.get("kind", "")) for item in failing_checks if str(item.get("kind", "")))
    )
    recommended_level = str(writeback.get("system_recommended_level", ""))
    escalation_required = bool(writeback.get("system_scope_escalation_required", False))
    if failing_checks and recommended_level in {"parent", "top"}:
        escalation_required = True
    return {
        "applies": applies,
        "passed": not failing_checks,
        "issue_count": sum(int(item.get("issue_count", 0) or 0) for item in partition_insights) if partition_insights else len(failing_checks),
        "failing_check_count": len(failing_checks),
        "failing_check_kinds": failing_check_kinds,
        "restore_sensitive_partition_count": restore_sensitive_partition_count,
        "implementation_blocked_partition_count": implementation_blocked_partition_count,
        "pcell_binding_blocked_partition_count": pcell_binding_blocked_partition_count,
        "retarget_focus_score_total": retarget_focus_score_total,
        "retarget_actions": retarget_actions,
        "recommended_level": recommended_level,
        "escalation_required": escalation_required,
        "checks": checks,
        "partition_insights": partition_insights,
    }


def _system_regression_check_key(item: Mapping[str, object]) -> str:
    kind = str(item.get("kind", ""))
    name = str(item.get("name", ""))
    partitions = ",".join(str(part) for part in tuple(item.get("affected_partitions", ()) or ()) if str(part))
    if not kind and not name and not partitions:
        return ""
    return f"{kind}:{name}:{partitions}"


def build_hierarchical_repair_execution_contract(
    bundle: Mapping[str, object],
    *,
    foundry_execution_contract: Mapping[str, object] | None = None,
    candidate_readiness: Mapping[str, object] | None = None,
    physical_contract: Mapping[str, object] | None = None,
    deck_spec: Mapping[str, object] | None = None,
    packaging_spec: Mapping[str, object] | None = None,
    available_inputs: Mapping[str, object] | None = None,
    system_regression_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    from .calibre import build_stage_foundry_execution_contracts

    decomposition_contract = dict(bundle.get("decomposition_contract", {}) or {})
    writeback_contract = build_multi_cell_writeback_contract(bundle, foundry_execution_contract)
    verification_matrix = (
        build_multi_stage_verification_matrix(bundle, foundry_execution_contract)
        if foundry_execution_contract
        else {
            "stages": tuple(build_stage_verification_contract(stage) for stage in tuple(bundle.get("stages", ()) or ()) if isinstance(stage, Mapping)),
            "summary": {
                "total_stage_count": len(tuple(bundle.get("stages", ()) or ())),
                "ready_stage_count": 0,
                "blocked_stage_count": 0,
                "editable_stage_count": 0,
                "synthesis_required_stage_count": 0,
                "rerun_stage_count": 0,
                "blocked_checks": (),
            },
        }
    )
    stage_foundry_execution = (
        build_stage_foundry_execution_contracts(
            bundle,
            candidate_readiness=candidate_readiness,
            physical_contract=physical_contract,
            deck_spec=deck_spec,
            packaging_spec=packaging_spec,
            available_inputs=available_inputs,
        )
        if any(
            value
            for value in (
                candidate_readiness,
                physical_contract,
                deck_spec,
                packaging_spec,
                available_inputs,
            )
        )
        else {}
    )
    verification_by_order = {
        int(item.get("order", 0) or 0): dict(item)
        for item in tuple(verification_matrix.get("stages", ()) or ())
        if isinstance(item, Mapping)
    }
    writeback_by_order = {
        int(item.get("order", 0) or 0): dict(item)
        for item in tuple(writeback_contract.get("stages", ()) or ())
        if isinstance(item, Mapping)
    }
    foundry_by_order = {
        int(item.get("order", 0) or 0): dict(item)
        for item in tuple(stage_foundry_execution.get("stages", ()) or ())
        if isinstance(item, Mapping)
    }
    system_regression = _hierarchical_system_regression_report_row(system_regression_report)
    rows: list[dict[str, object]] = []
    direct_writeback_stage_count = 0
    foundry_ready_stage_count = 0
    system_regression_stage_count = 0
    failing_system_regression_stage_count = 0
    implementation_blocked_stage_count = 0
    pcell_binding_blocked_stage_count = 0
    foundry_binding_blocked_stage_count = 0
    foundry_architecture_budget_blocked_stage_count = 0
    failing_system_contract_checks: dict[str, dict[str, object]] = {}
    for stage in tuple(bundle.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        order = int(stage.get("order", 0) or 0)
        verification = verification_by_order.get(order, {})
        writeback = writeback_by_order.get(order, {})
        foundry = foundry_by_order.get(order, {})
        effective_foundry_binding_blocked = tuple(
            str(item)
            for item in tuple(
                foundry.get("binding_blocked_partitions", ())
                or dict(verification.get("foundry_readiness", {}) or {}).get("binding_blocked_partitions", ())
                or ()
            )
            if str(item)
        )
        effective_foundry_budget_blocked = tuple(
            str(item)
            for item in tuple(
                foundry.get("architecture_budget_blocked_partitions", ())
                or dict(verification.get("foundry_readiness", {}) or {}).get("architecture_budget_blocked_partitions", ())
                or ()
            )
            if str(item)
        )
        stage_system_regression = _build_stage_system_regression_view(
            {
                "order": order,
                "target_cell": str(verification.get("target_cell", writeback.get("target_cell", ""))),
                "stage_hierarchy_node": str(verification.get("stage_hierarchy_node", writeback.get("stage_hierarchy_node", ""))),
                "verification": verification,
                "writeback": writeback,
            },
            system_regression,
        )
        if bool(writeback.get("writeback_ready", False)):
            direct_writeback_stage_count += 1
        if bool(foundry.get("ready", False)):
            foundry_ready_stage_count += 1
        if effective_foundry_binding_blocked:
            foundry_binding_blocked_stage_count += 1
        if effective_foundry_budget_blocked:
            foundry_architecture_budget_blocked_stage_count += 1
        if bool(stage_system_regression.get("applies", False)):
            system_regression_stage_count += 1
        if int(stage_system_regression.get("implementation_blocked_partition_count", 0) or 0) > 0:
            implementation_blocked_stage_count += 1
        if int(stage_system_regression.get("pcell_binding_blocked_partition_count", 0) or 0) > 0:
            pcell_binding_blocked_stage_count += 1
        if bool(stage_system_regression.get("applies", False)) and not bool(stage_system_regression.get("passed", True)):
            failing_system_regression_stage_count += 1
            for item in tuple(stage_system_regression.get("checks", ()) or ()):
                if not isinstance(item, Mapping) or bool(item.get("passed", False)):
                    continue
                key = _system_regression_check_key(item)
                if key:
                    failing_system_contract_checks[key] = dict(item)
        rows.append(
            {
                "order": order,
                "role": str(stage.get("role", "")),
                "execution_kind": str(stage.get("execution_kind", "")),
                "target_cell": str(verification.get("target_cell", writeback.get("target_cell", ""))),
                "stage_hierarchy_node": str(verification.get("stage_hierarchy_node", writeback.get("stage_hierarchy_node", ""))),
                "target_cellview": dict(verification.get("target_cellview", {}) or writeback.get("target_cellview", {}) or {}),
                "verification": verification,
                "writeback": writeback,
                "foundry_execution": foundry,
                "system_regression": stage_system_regression,
            }
        )
    rows.sort(key=lambda item: (int(item.get("order", 0) or 0), str(item.get("target_cell", ""))))
    return {
        "decomposition_contract": decomposition_contract,
        "writeback_contract": dict(writeback_contract.get("writeback_contract", {}) or {}),
        "verification_summary": dict(verification_matrix.get("summary", {}) or {}),
        "foundry_execution_summary": dict(stage_foundry_execution.get("summary", {}) or {}),
        "global_foundry_execution": dict(stage_foundry_execution.get("global_foundry_execution", {}) or {}),
        "system_regression_summary": {
            "topology_name": str(system_regression.get("topology_name", "")),
            "passed": bool(system_regression.get("passed", True)) if system_regression else True,
            "issue_count": len(tuple(system_regression.get("issues", ()) or ())),
            "failing_check_count": sum(
                1
                for item in tuple(system_regression.get("contract_checks", ()) or ())
                if isinstance(item, Mapping) and not bool(item.get("passed", False))
            ),
            "summary": tuple(str(item) for item in tuple(system_regression.get("summary", ()) or ()) if str(item)),
            "stage_count": system_regression_stage_count,
            "failing_stage_count": failing_system_regression_stage_count,
            "implementation_blocked_stage_count": implementation_blocked_stage_count,
            "pcell_binding_blocked_stage_count": pcell_binding_blocked_stage_count,
            "foundry_binding_blocked_stage_count": foundry_binding_blocked_stage_count,
            "foundry_architecture_budget_blocked_stage_count": foundry_architecture_budget_blocked_stage_count,
        },
        "stages": tuple(rows),
        "summary": {
            "total_stage_count": len(rows),
            "direct_writeback_stage_count": direct_writeback_stage_count,
            "foundry_ready_stage_count": foundry_ready_stage_count,
            "requires_multi_cell_writeback": bool(dict(writeback_contract.get("writeback_contract", {}) or {}).get("requires_multi_cell_writeback", False)),
            "system_regression_stage_count": system_regression_stage_count,
            "failing_system_regression_stage_count": failing_system_regression_stage_count,
            "failing_system_contract_check_count": len(failing_system_contract_checks),
            "implementation_blocked_stage_count": implementation_blocked_stage_count,
            "pcell_binding_blocked_stage_count": pcell_binding_blocked_stage_count,
            "foundry_binding_blocked_stage_count": foundry_binding_blocked_stage_count,
            "foundry_architecture_budget_blocked_stage_count": foundry_architecture_budget_blocked_stage_count,
        },
    }


def query_hierarchical_repair_execution_stage(
    bundle: Mapping[str, object],
    *,
    foundry_execution_contract: Mapping[str, object] | None = None,
    candidate_readiness: Mapping[str, object] | None = None,
    physical_contract: Mapping[str, object] | None = None,
    deck_spec: Mapping[str, object] | None = None,
    packaging_spec: Mapping[str, object] | None = None,
    available_inputs: Mapping[str, object] | None = None,
    system_regression_report: Mapping[str, object] | None = None,
    cell: object | None = None,
    node: object | None = None,
) -> dict[str, object]:
    contract = build_hierarchical_repair_execution_contract(
        bundle,
        foundry_execution_contract=foundry_execution_contract,
        candidate_readiness=candidate_readiness,
        physical_contract=physical_contract,
        deck_spec=deck_spec,
        packaging_spec=packaging_spec,
        available_inputs=available_inputs,
        system_regression_report=system_regression_report,
    )
    if cell is None and node is None:
        return contract
    target_cell = str(cell) if cell is not None else ""
    target_node = str(node) if node is not None else ""
    for stage in tuple(contract.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        if target_cell and str(stage.get("target_cell", "")) == target_cell:
            return dict(stage)
        if target_node and str(stage.get("stage_hierarchy_node", "")) == target_node:
            return dict(stage)
    return {}


def annotate_hierarchical_repair_execution_with_hierarchy_database(
    contract: Mapping[str, object],
    hierarchy_database: object,
) -> dict[str, object]:
    annotated = dict(contract)
    decomposition_contract = dict(annotated.get("decomposition_contract", {}) or {})
    writeback_contract = dict(annotated.get("writeback_contract", {}) or {})
    source = (
        dict(decomposition_contract.get("hierarchy_contract", {}) or {}).get("source_node", {}).get("name")
        or decomposition_contract.get("source_cell", "")
        or writeback_contract.get("source_cell", "")
    )
    target = (
        dict(decomposition_contract.get("hierarchy_contract", {}) or {}).get("target_node", {}).get("name")
        or decomposition_contract.get("target_cell", "")
        or writeback_contract.get("target_cell", "")
    )
    hierarchy_contract = query_hierarchy_cellview_path(
        hierarchy_database,
        source=source,
        target=target,
    )
    updated_decomposition = dict(decomposition_contract)
    updated_decomposition["hierarchy_contract"] = hierarchy_contract
    if not tuple(updated_decomposition.get("hierarchy_path", ()) or ()):
        updated_decomposition["hierarchy_path"] = tuple(hierarchy_contract.get("cell_path", ()) or ())
    if not tuple(updated_decomposition.get("hierarchy_node_path", ()) or ()):
        updated_decomposition["hierarchy_node_path"] = tuple(hierarchy_contract.get("path", ()) or ())
    annotated["decomposition_contract"] = updated_decomposition

    updated_writeback = dict(writeback_contract)
    if not tuple(updated_writeback.get("hierarchy_path", ()) or ()):
        updated_writeback["hierarchy_path"] = tuple(hierarchy_contract.get("cell_path", ()) or ())
    if not tuple(updated_writeback.get("hierarchy_node_path", ()) or ()):
        updated_writeback["hierarchy_node_path"] = tuple(hierarchy_contract.get("path", ()) or ())
    annotated["writeback_contract"] = updated_writeback
    annotated["hierarchy_contract"] = hierarchy_contract

    stages: list[dict[str, object]] = []
    path_nodes = {
        str(dict(item).get("name", "")): dict(item)
        for item in tuple(hierarchy_contract.get("path_nodes", ()) or ())
        if isinstance(item, Mapping) and str(dict(item).get("name", ""))
    }
    path_cells = {
        str(dict(item).get("cell", "")): dict(item)
        for item in tuple(hierarchy_contract.get("path_nodes", ()) or ())
        if isinstance(item, Mapping) and str(dict(item).get("cell", ""))
    }
    for stage in tuple(annotated.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        updated_stage = dict(stage)
        stage_node = str(updated_stage.get("stage_hierarchy_node", ""))
        stage_cell = str(updated_stage.get("target_cell", ""))
        hierarchy_node = path_nodes.get(stage_node) or path_cells.get(stage_cell) or {}
        if hierarchy_node:
            updated_stage["stage_hierarchy_binding"] = {
                "name": str(hierarchy_node.get("name", "")),
                "cell": str(hierarchy_node.get("cell", "")),
                "parent": str(hierarchy_node.get("parent", "")),
                "lib": str(hierarchy_node.get("lib", "")),
                "view": str(hierarchy_node.get("view", "")),
                "view_type": str(hierarchy_node.get("view_type", "")),
                "aliases": tuple(str(alias) for alias in tuple(hierarchy_node.get("aliases", ()) or ()) if str(alias)),
            }
        stages.append(updated_stage)
    annotated["stages"] = tuple(stages)
    return annotated


def query_hierarchical_repair_execution_path_stage(
    contract: Mapping[str, object],
    *,
    cell: object | None = None,
    node: object | None = None,
    path_index: object | None = None,
) -> dict[str, object]:
    stages = tuple(contract.get("stages", ()) or ())
    if path_index is not None:
        try:
            index = int(path_index)
        except (TypeError, ValueError):
            index = -1
        for stage in stages:
            if not isinstance(stage, Mapping):
                continue
            binding = dict(stage.get("stage_hierarchy_binding", {}) or {})
            hierarchy_contract = dict(contract.get("hierarchy_contract", {}) or {})
            path_nodes = tuple(hierarchy_contract.get("path_nodes", ()) or ())
            stage_name = str(binding.get("name", stage.get("stage_hierarchy_node", "")))
            for current_index, item in enumerate(path_nodes):
                if not isinstance(item, Mapping):
                    continue
                if current_index == index and stage_name == str(dict(item).get("name", "")):
                    return dict(stage)
        return {}
    target_cell = str(cell) if cell is not None else ""
    target_node = str(node) if node is not None else ""
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        binding = dict(stage.get("stage_hierarchy_binding", {}) or {})
        if target_cell and target_cell in {str(stage.get("target_cell", "")), str(binding.get("cell", ""))}:
            return dict(stage)
        if target_node and target_node in {str(stage.get("stage_hierarchy_node", "")), str(binding.get("name", ""))}:
            return dict(stage)
    return {}


def build_hierarchical_stage_insight_overlay(
    contract: Mapping[str, object],
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    focus_partition_stage_count = 0
    restore_sensitive_stage_count = 0
    reference_sensitive_stage_count = 0
    direct_writeback_stage_count = 0
    foundry_blocked_stage_count = 0
    foundry_binding_blocked_stage_count = 0
    foundry_architecture_budget_blocked_stage_count = 0
    system_regression_stage_count = 0
    failing_system_regression_stage_count = 0
    implementation_blocked_stage_count = 0
    pcell_binding_blocked_stage_count = 0
    failing_system_contract_checks: dict[str, dict[str, object]] = {}
    for stage in tuple(contract.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        verification = dict(stage.get("verification", {}) or {})
        writeback = dict(stage.get("writeback", {}) or {})
        foundry = dict(stage.get("foundry_execution", {}) or {})
        system_regression = dict(stage.get("system_regression", {}) or {})
        hierarchy_binding = dict(stage.get("stage_hierarchy_binding", {}) or {})
        partition_targets = tuple(dict(item) for item in tuple(verification.get("partition_targets", ()) or ()) if isinstance(item, Mapping))
        system_targets = tuple(dict(item) for item in tuple(verification.get("system_targets", ()) or ()) if isinstance(item, Mapping))
        focus_partition_count = sum(1 for item in partition_targets if bool(item.get("retarget_changed", False)))
        restore_target_count = sum(1 for item in system_targets if bool(item.get("restore_required", False)))
        reference_target_count = sum(1 for item in system_targets if str(item.get("kind", "")) == "reference_path")
        if focus_partition_count:
            focus_partition_stage_count += 1
        if restore_target_count:
            restore_sensitive_stage_count += 1
        if reference_target_count:
            reference_sensitive_stage_count += 1
        if bool(writeback.get("writeback_ready", False)):
            direct_writeback_stage_count += 1
        if foundry and not bool(foundry.get("ready", False)):
            foundry_blocked_stage_count += 1
        foundry_binding_blocked_partitions = tuple(
            str(item)
            for item in tuple(
                foundry.get("binding_blocked_partitions", ())
                or dict(verification.get("foundry_readiness", {}) or {}).get("binding_blocked_partitions", ())
                or tuple(writeback.get("binding_blocked_partitions", ()) or ())
                or ()
            )
            if str(item)
        )
        foundry_macro_bound_partitions = tuple(
            str(item)
            for item in tuple(
                foundry.get("macro_bound_partitions", ())
                or dict(verification.get("foundry_readiness", {}) or {}).get("macro_bound_partitions", ())
                or tuple(writeback.get("macro_bound_partitions", ()) or ())
                or ()
            )
            if str(item)
        )
        foundry_architecture_budget_blocked_partitions = tuple(
            str(item)
            for item in tuple(
                foundry.get("architecture_budget_blocked_partitions", ())
                or dict(verification.get("foundry_readiness", {}) or {}).get("architecture_budget_blocked_partitions", ())
                or tuple(writeback.get("architecture_budget_blocked_partitions", ()) or ())
                or ()
            )
            if str(item)
        )
        if foundry_binding_blocked_partitions:
            foundry_binding_blocked_stage_count += 1
        if foundry_architecture_budget_blocked_partitions:
            foundry_architecture_budget_blocked_stage_count += 1
        if bool(system_regression.get("applies", False)):
            system_regression_stage_count += 1
        if int(system_regression.get("implementation_blocked_partition_count", 0) or 0) > 0:
            implementation_blocked_stage_count += 1
        if int(system_regression.get("pcell_binding_blocked_partition_count", 0) or 0) > 0:
            pcell_binding_blocked_stage_count += 1
        if bool(system_regression.get("applies", False)) and not bool(system_regression.get("passed", True)):
            failing_system_regression_stage_count += 1
            for item in tuple(system_regression.get("checks", ()) or ()):
                if not isinstance(item, Mapping) or bool(item.get("passed", False)):
                    continue
                key = _system_regression_check_key(item)
                if key:
                    failing_system_contract_checks[key] = dict(item)
        rows.append(
            {
                "order": int(stage.get("order", 0) or 0),
                "role": str(stage.get("role", "")),
                "execution_kind": str(stage.get("execution_kind", "")),
                "target_cell": str(stage.get("target_cell", "")),
                "stage_hierarchy_node": str(stage.get("stage_hierarchy_node", "")),
                "hierarchy_binding": hierarchy_binding,
                "focus_partition_count": focus_partition_count,
                "restore_target_count": restore_target_count,
                "reference_target_count": reference_target_count,
                "partition_targets": partition_targets,
                "system_targets": system_targets,
                "writeback_ready": bool(writeback.get("writeback_ready", False)),
                "requires_stage_specific_synthesis": bool(writeback.get("requires_stage_specific_synthesis", False)),
                "foundry_ready": bool(foundry.get("ready", False)) if foundry else False,
                "foundry_blocked_checks": tuple(str(item) for item in tuple(foundry.get("blocked_checks", ()) or ()) if str(item)),
                "foundry_binding_blocked_partitions": foundry_binding_blocked_partitions,
                "foundry_macro_bound_partitions": foundry_macro_bound_partitions,
                "foundry_architecture_budget_blocked_partitions": foundry_architecture_budget_blocked_partitions,
                "system_regression_applies": bool(system_regression.get("applies", False)),
                "system_regression_passed": bool(system_regression.get("passed", True)),
                "system_regression_issue_count": int(system_regression.get("issue_count", 0) or 0),
                "system_regression_failing_check_count": int(system_regression.get("failing_check_count", 0) or 0),
                "system_regression_failing_check_kinds": tuple(
                    str(item) for item in tuple(system_regression.get("failing_check_kinds", ()) or ()) if str(item)
                ),
                "system_regression_restore_sensitive_partition_count": int(
                    system_regression.get("restore_sensitive_partition_count", 0) or 0
                ),
                "system_regression_implementation_blocked_partition_count": int(
                    system_regression.get("implementation_blocked_partition_count", 0) or 0
                ),
                "system_regression_pcell_binding_blocked_partition_count": int(
                    system_regression.get("pcell_binding_blocked_partition_count", 0) or 0
                ),
                "system_regression_retarget_focus_score_total": int(
                    system_regression.get("retarget_focus_score_total", 0) or 0
                ),
                "system_regression_retarget_actions": tuple(
                    str(item) for item in tuple(system_regression.get("retarget_actions", ()) or ()) if str(item)
                ),
                "required_enclosing_reruns": tuple(str(item) for item in tuple(verification.get("required_enclosing_reruns", ()) or writeback.get("required_enclosing_reruns", ()) or ()) if str(item)),
            }
        )
    rows.sort(key=lambda item: (int(item.get("order", 0) or 0), str(item.get("target_cell", ""))))
    return {
        "stages": tuple(rows),
        "summary": {
            "total_stage_count": len(rows),
            "focus_partition_stage_count": focus_partition_stage_count,
            "restore_sensitive_stage_count": restore_sensitive_stage_count,
            "reference_sensitive_stage_count": reference_sensitive_stage_count,
            "direct_writeback_stage_count": direct_writeback_stage_count,
            "foundry_blocked_stage_count": foundry_blocked_stage_count,
            "foundry_binding_blocked_stage_count": foundry_binding_blocked_stage_count,
            "foundry_architecture_budget_blocked_stage_count": foundry_architecture_budget_blocked_stage_count,
            "system_regression_stage_count": system_regression_stage_count,
            "failing_system_regression_stage_count": failing_system_regression_stage_count,
            "failing_system_contract_check_count": len(failing_system_contract_checks),
            "implementation_blocked_stage_count": implementation_blocked_stage_count,
            "pcell_binding_blocked_stage_count": pcell_binding_blocked_stage_count,
        },
    }


def query_hierarchical_stage_insight_overlay(
    contract: Mapping[str, object],
    *,
    cell: object | None = None,
    node: object | None = None,
) -> dict[str, object]:
    overlay = build_hierarchical_stage_insight_overlay(contract)
    if cell is None and node is None:
        return overlay
    target_cell = str(cell) if cell is not None else ""
    target_node = str(node) if node is not None else ""
    for stage in tuple(overlay.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        binding = dict(stage.get("hierarchy_binding", {}) or {})
        if target_cell and target_cell in {str(stage.get("target_cell", "")), str(binding.get("cell", ""))}:
            return dict(stage)
        if target_node and target_node in {str(stage.get("stage_hierarchy_node", "")), str(binding.get("name", ""))}:
            return dict(stage)
    return {}


def build_hierarchical_repair_scope_intent(
    contract: Mapping[str, object],
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    keep_stable_stage_count = 0
    retarget_changed_stage_count = 0
    escalation_required_stage_count = 0
    protected_reference_stage_count = 0
    restore_bus_stage_count = 0
    restore_feedback_stage_count = 0
    foundry_binding_blocked_stage_count = 0
    foundry_architecture_budget_blocked_stage_count = 0
    system_regression_stage_count = 0
    implementation_blocked_stage_count = 0
    pcell_binding_blocked_stage_count = 0
    failing_system_contract_checks: dict[str, dict[str, object]] = {}
    for stage in tuple(contract.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        verification = dict(stage.get("verification", {}) or {})
        writeback = dict(stage.get("writeback", {}) or {})
        system_regression = dict(stage.get("system_regression", {}) or {})
        partition_targets = tuple(dict(item) for item in tuple(verification.get("partition_targets", ()) or ()) if isinstance(item, Mapping))
        system_targets = tuple(dict(item) for item in tuple(verification.get("system_targets", ()) or ()) if isinstance(item, Mapping))
        keep_stable_partitions = tuple(
            str(item.get("partition", ""))
            for item in partition_targets
            if bool(item.get("keep_stable", False)) and str(item.get("partition", ""))
        )
        retarget_changed_partitions = tuple(
            str(item.get("partition", ""))
            for item in partition_targets
            if bool(item.get("retarget_changed", False)) and str(item.get("partition", ""))
        )
        restore_bus_nets = tuple(
            dict.fromkeys(
                str(net)
                for item in system_targets
                if str(item.get("kind", "")) == "bus_contract" and bool(item.get("restore_required", False))
                for net in tuple(item.get("nets", ()) or ())
                if str(net)
            )
        )
        restore_feedback_nets = tuple(
            dict.fromkeys(
                str(item.get("net", ""))
                for item in system_targets
                if str(item.get("kind", "")) == "feedback_contract" and bool(item.get("restore_required", False)) and str(item.get("net", ""))
            )
        )
        protected_reference_nets = tuple(
            dict.fromkeys(
                str(item.get("net", ""))
                for item in system_targets
                if str(item.get("kind", "")) == "reference_path" and str(item.get("net", ""))
            )
        )
        architecture_protected_nets = tuple(
            str(item)
            for item in tuple(writeback.get("architecture_protected_nets", ()) or ())
            if str(item)
        )
        binding_blocked_partitions = tuple(
            str(item)
            for item in tuple(
                dict(verification.get("foundry_readiness", {}) or {}).get("binding_blocked_partitions", ())
                or tuple(writeback.get("binding_blocked_partitions", ()) or ())
                or ()
            )
            if str(item)
        )
        macro_bound_partitions = tuple(
            str(item)
            for item in tuple(
                dict(verification.get("foundry_readiness", {}) or {}).get("macro_bound_partitions", ())
                or tuple(writeback.get("macro_bound_partitions", ()) or ())
                or ()
            )
            if str(item)
        )
        architecture_budget_blocked_partitions = tuple(
            str(item)
            for item in tuple(
                dict(verification.get("foundry_readiness", {}) or {}).get("architecture_budget_blocked_partitions", ())
                or tuple(writeback.get("architecture_budget_blocked_partitions", ()) or ())
                or ()
            )
            if str(item)
        )
        scope_nets = tuple(str(item) for item in tuple(writeback.get("scope_nets", ()) or verification.get("scope_nets", ()) or ()) if str(item))
        blocked_scope_nets = tuple(str(item) for item in tuple(writeback.get("blocked_scope_nets", ()) or ()) if str(item))
        allowed_scope_devices = tuple(str(item) for item in tuple(writeback.get("allowed_scope_devices", ()) or ()) if str(item))
        blocked_scope_devices = tuple(str(item) for item in tuple(writeback.get("blocked_scope_devices", ()) or ()) if str(item))
        system_recommended_level = str(writeback.get("system_recommended_level", ""))
        system_regression_escalation_required = bool(system_regression.get("escalation_required", False))
        escalation_required = bool(writeback.get("system_scope_escalation_required", False) or system_regression_escalation_required)
        if keep_stable_partitions:
            keep_stable_stage_count += 1
        if retarget_changed_partitions:
            retarget_changed_stage_count += 1
        if escalation_required:
            escalation_required_stage_count += 1
        if protected_reference_nets or architecture_protected_nets:
            protected_reference_stage_count += 1
        if restore_bus_nets:
            restore_bus_stage_count += 1
        if restore_feedback_nets:
            restore_feedback_stage_count += 1
        if binding_blocked_partitions:
            foundry_binding_blocked_stage_count += 1
        if architecture_budget_blocked_partitions:
            foundry_architecture_budget_blocked_stage_count += 1
        if bool(system_regression.get("applies", False)):
            system_regression_stage_count += 1
        if int(system_regression.get("implementation_blocked_partition_count", 0) or 0) > 0:
            implementation_blocked_stage_count += 1
        if int(system_regression.get("pcell_binding_blocked_partition_count", 0) or 0) > 0:
            pcell_binding_blocked_stage_count += 1
        if bool(system_regression.get("applies", False)):
            for item in tuple(system_regression.get("checks", ()) or ()):
                if not isinstance(item, Mapping) or bool(item.get("passed", False)):
                    continue
                key = _system_regression_check_key(item)
                if key:
                    failing_system_contract_checks[key] = dict(item)
        rows.append(
            {
                "order": int(stage.get("order", 0) or 0),
                "role": str(stage.get("role", "")),
                "target_cell": str(stage.get("target_cell", "")),
                "stage_hierarchy_node": str(stage.get("stage_hierarchy_node", "")),
                "keep_stable_partitions": keep_stable_partitions,
                "retarget_changed_partitions": retarget_changed_partitions,
                "restore_bus_nets": restore_bus_nets,
                "restore_feedback_nets": restore_feedback_nets,
                "protected_reference_nets": protected_reference_nets,
                "architecture_protected_nets": architecture_protected_nets,
                "binding_blocked_partitions": binding_blocked_partitions,
                "macro_bound_partitions": macro_bound_partitions,
                "architecture_budget_blocked_partitions": architecture_budget_blocked_partitions,
                "scope_nets": scope_nets,
                "blocked_scope_nets": blocked_scope_nets,
                "allowed_scope_devices": allowed_scope_devices,
                "blocked_scope_devices": blocked_scope_devices,
                "system_recommended_level": system_recommended_level,
                "system_scope_escalation_required": escalation_required,
                "system_regression_passed": bool(system_regression.get("passed", True)),
                "system_regression_issue_count": int(system_regression.get("issue_count", 0) or 0),
                "system_regression_failing_check_count": int(system_regression.get("failing_check_count", 0) or 0),
                "system_regression_failing_check_kinds": tuple(
                    str(item) for item in tuple(system_regression.get("failing_check_kinds", ()) or ()) if str(item)
                ),
                "system_regression_implementation_blocked_partition_count": int(
                    system_regression.get("implementation_blocked_partition_count", 0) or 0
                ),
                "system_regression_pcell_binding_blocked_partition_count": int(
                    system_regression.get("pcell_binding_blocked_partition_count", 0) or 0
                ),
                "system_regression_retarget_focus_score_total": int(
                    system_regression.get("retarget_focus_score_total", 0) or 0
                ),
                "system_regression_retarget_actions": tuple(
                    str(item) for item in tuple(system_regression.get("retarget_actions", ()) or ()) if str(item)
                ),
                "system_regression_escalation_required": system_regression_escalation_required,
            }
        )
    rows.sort(key=lambda item: (int(item.get("order", 0) or 0), str(item.get("target_cell", ""))))
    return {
        "stages": tuple(rows),
        "summary": {
            "total_stage_count": len(rows),
            "keep_stable_stage_count": keep_stable_stage_count,
            "retarget_changed_stage_count": retarget_changed_stage_count,
            "escalation_required_stage_count": escalation_required_stage_count,
            "protected_reference_stage_count": protected_reference_stage_count,
            "restore_bus_stage_count": restore_bus_stage_count,
            "restore_feedback_stage_count": restore_feedback_stage_count,
            "foundry_binding_blocked_stage_count": foundry_binding_blocked_stage_count,
            "foundry_architecture_budget_blocked_stage_count": foundry_architecture_budget_blocked_stage_count,
            "system_regression_stage_count": system_regression_stage_count,
            "failing_system_contract_check_count": len(failing_system_contract_checks),
            "implementation_blocked_stage_count": implementation_blocked_stage_count,
            "pcell_binding_blocked_stage_count": pcell_binding_blocked_stage_count,
        },
    }


def query_hierarchical_repair_scope_intent(
    contract: Mapping[str, object],
    *,
    cell: object | None = None,
    node: object | None = None,
) -> dict[str, object]:
    intent = build_hierarchical_repair_scope_intent(contract)
    if cell is None and node is None:
        return intent
    target_cell = str(cell) if cell is not None else ""
    target_node = str(node) if node is not None else ""
    for stage in tuple(intent.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        if target_cell and str(stage.get("target_cell", "")) == target_cell:
            return dict(stage)
        if target_node and str(stage.get("stage_hierarchy_node", "")) == target_node:
            return dict(stage)
    return {}


def build_hierarchical_dispatch_scope_overlay(
    contract: Mapping[str, object],
) -> dict[str, object]:
    scope_intent = build_hierarchical_repair_scope_intent(contract)
    intent_by_order = {
        int(item.get("order", 0) or 0): dict(item)
        for item in tuple(scope_intent.get("stages", ()) or ())
        if isinstance(item, Mapping)
    }
    rows: list[dict[str, object]] = []
    aligned_stage_count = 0
    escalated_stage_count = 0
    blocked_reference_stage_count = 0
    restore_sensitive_stage_count = 0
    foundry_binding_blocked_stage_count = 0
    foundry_architecture_budget_blocked_stage_count = 0
    failing_system_regression_stage_count = 0
    implementation_blocked_stage_count = 0
    pcell_binding_blocked_stage_count = 0
    failing_system_contract_checks: dict[str, dict[str, object]] = {}
    for stage in tuple(contract.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        order = int(stage.get("order", 0) or 0)
        intent = intent_by_order.get(order, {})
        writeback = dict(stage.get("writeback", {}) or {})
        verification = dict(stage.get("verification", {}) or {})
        system_regression = dict(stage.get("system_regression", {}) or {})
        scope_nets = tuple(str(item) for item in tuple(intent.get("scope_nets", ()) or ()) if str(item))
        blocked_scope_nets = tuple(str(item) for item in tuple(intent.get("blocked_scope_nets", ()) or ()) if str(item))
        restore_bus_nets = tuple(str(item) for item in tuple(intent.get("restore_bus_nets", ()) or ()) if str(item))
        restore_feedback_nets = tuple(str(item) for item in tuple(intent.get("restore_feedback_nets", ()) or ()) if str(item))
        protected_reference_nets = tuple(str(item) for item in tuple(intent.get("protected_reference_nets", ()) or ()) if str(item))
        architecture_protected_nets = tuple(str(item) for item in tuple(intent.get("architecture_protected_nets", ()) or ()) if str(item))
        binding_blocked_partitions = tuple(str(item) for item in tuple(intent.get("binding_blocked_partitions", ()) or ()) if str(item))
        macro_bound_partitions = tuple(str(item) for item in tuple(intent.get("macro_bound_partitions", ()) or ()) if str(item))
        architecture_budget_blocked_partitions = tuple(
            str(item) for item in tuple(intent.get("architecture_budget_blocked_partitions", ()) or ()) if str(item)
        )
        verification_scope_nets = tuple(str(item) for item in tuple(verification.get("scope_nets", ()) or ()) if str(item))
        allowed_scope_nets = tuple(str(item) for item in tuple(writeback.get("allowed_scope_nets", ()) or ()) if str(item))
        blocked_writeback_nets = tuple(str(item) for item in tuple(writeback.get("blocked_scope_nets", ()) or ()) if str(item))
        system_recommended_level = str(intent.get("system_recommended_level", "") or writeback.get("system_recommended_level", ""))
        escalation_required = bool(intent.get("system_scope_escalation_required", False) or writeback.get("system_scope_escalation_required", False))
        scope_alignment = {
            "allowed_nets_match": set(scope_nets).issubset(set(allowed_scope_nets) or set(verification_scope_nets)) if scope_nets else True,
            "blocked_nets_match": set((*protected_reference_nets, *architecture_protected_nets)).issubset(set(blocked_writeback_nets))
            if protected_reference_nets or architecture_protected_nets
            else True,
            "restore_nets_covered": set((*restore_bus_nets, *restore_feedback_nets)).issubset(set(verification_scope_nets) | set(allowed_scope_nets))
            if restore_bus_nets or restore_feedback_nets
            else True,
        }
        aligned = all(bool(value) for value in scope_alignment.values())
        if aligned:
            aligned_stage_count += 1
        if escalation_required:
            escalated_stage_count += 1
        if protected_reference_nets:
            blocked_reference_stage_count += 1
        if restore_bus_nets or restore_feedback_nets:
            restore_sensitive_stage_count += 1
        if binding_blocked_partitions:
            foundry_binding_blocked_stage_count += 1
        if architecture_budget_blocked_partitions:
            foundry_architecture_budget_blocked_stage_count += 1
        if int(system_regression.get("implementation_blocked_partition_count", 0) or 0) > 0:
            implementation_blocked_stage_count += 1
        if int(system_regression.get("pcell_binding_blocked_partition_count", 0) or 0) > 0:
            pcell_binding_blocked_stage_count += 1
        if bool(system_regression.get("applies", False)) and not bool(system_regression.get("passed", True)):
            failing_system_regression_stage_count += 1
            for item in tuple(system_regression.get("checks", ()) or ()):
                if not isinstance(item, Mapping) or bool(item.get("passed", False)):
                    continue
                key = _system_regression_check_key(item)
                if key:
                    failing_system_contract_checks[key] = dict(item)
        rows.append(
            {
                "order": order,
                "role": str(stage.get("role", "")),
                "target_cell": str(stage.get("target_cell", "")),
                "stage_hierarchy_node": str(stage.get("stage_hierarchy_node", "")),
                "system_recommended_level": system_recommended_level,
                "system_scope_escalation_required": escalation_required,
                "intent_scope_nets": scope_nets,
                "intent_blocked_scope_nets": blocked_scope_nets,
                "dispatch_allowed_scope_nets": allowed_scope_nets,
                "dispatch_blocked_scope_nets": blocked_writeback_nets,
                "restore_bus_nets": restore_bus_nets,
                "restore_feedback_nets": restore_feedback_nets,
                "protected_reference_nets": protected_reference_nets,
                "architecture_protected_nets": architecture_protected_nets,
                "binding_blocked_partitions": binding_blocked_partitions,
                "macro_bound_partitions": macro_bound_partitions,
                "architecture_budget_blocked_partitions": architecture_budget_blocked_partitions,
                "system_regression_passed": bool(system_regression.get("passed", True)),
                "system_regression_issue_count": int(system_regression.get("issue_count", 0) or 0),
                "system_regression_failing_check_count": int(system_regression.get("failing_check_count", 0) or 0),
                "system_regression_failing_check_kinds": tuple(
                    str(item) for item in tuple(system_regression.get("failing_check_kinds", ()) or ()) if str(item)
                ),
                "system_regression_implementation_blocked_partition_count": int(
                    system_regression.get("implementation_blocked_partition_count", 0) or 0
                ),
                "system_regression_pcell_binding_blocked_partition_count": int(
                    system_regression.get("pcell_binding_blocked_partition_count", 0) or 0
                ),
                "system_regression_retarget_focus_score_total": int(
                    system_regression.get("retarget_focus_score_total", 0) or 0
                ),
                "system_regression_retarget_actions": tuple(
                    str(item) for item in tuple(system_regression.get("retarget_actions", ()) or ()) if str(item)
                ),
                "scope_alignment": scope_alignment,
                "aligned": aligned,
            }
        )
    rows.sort(key=lambda item: (int(item.get("order", 0) or 0), str(item.get("target_cell", ""))))
    return {
        "stages": tuple(rows),
        "summary": {
            "total_stage_count": len(rows),
            "aligned_stage_count": aligned_stage_count,
            "escalated_stage_count": escalated_stage_count,
            "blocked_reference_stage_count": blocked_reference_stage_count,
            "restore_sensitive_stage_count": restore_sensitive_stage_count,
            "foundry_binding_blocked_stage_count": foundry_binding_blocked_stage_count,
            "foundry_architecture_budget_blocked_stage_count": foundry_architecture_budget_blocked_stage_count,
            "failing_system_regression_stage_count": failing_system_regression_stage_count,
            "failing_system_contract_check_count": len(failing_system_contract_checks),
            "implementation_blocked_stage_count": implementation_blocked_stage_count,
            "pcell_binding_blocked_stage_count": pcell_binding_blocked_stage_count,
        },
    }


def query_hierarchical_dispatch_scope_overlay(
    contract: Mapping[str, object],
    *,
    cell: object | None = None,
    node: object | None = None,
) -> dict[str, object]:
    overlay = build_hierarchical_dispatch_scope_overlay(contract)
    if cell is None and node is None:
        return overlay
    target_cell = str(cell) if cell is not None else ""
    target_node = str(node) if node is not None else ""
    for stage in tuple(overlay.get("stages", ()) or ()):
        if not isinstance(stage, Mapping):
            continue
        if target_cell and str(stage.get("target_cell", "")) == target_cell:
            return dict(stage)
        if target_node and str(stage.get("stage_hierarchy_node", "")) == target_node:
            return dict(stage)
    return {}


def build_post_layout_eco_dispatch_scope_overlay(
    proposal_summary: Mapping[str, object],
    dispatch_bundle: Mapping[str, object],
) -> dict[str, object]:
    proposal = dict(proposal_summary or {})
    dispatch = dict(dispatch_bundle or {})
    scope_guard = dict(dispatch.get("scope_guard", {}) or {})
    proposal_scope_nets = tuple(str(item) for item in tuple(proposal.get("scope_nets", ()) or ()) if str(item))
    proposal_avoid_nets = tuple(str(item) for item in tuple(proposal.get("avoid_nets", ()) or ()) if str(item))
    proposal_restore_bus_nets = tuple(str(item) for item in tuple(proposal.get("restore_bus_nets", ()) or ()) if str(item))
    proposal_restore_feedback_nets = tuple(str(item) for item in tuple(proposal.get("restore_feedback_nets", ()) or ()) if str(item))
    proposal_protected_reference_nets = tuple(str(item) for item in tuple(proposal.get("protected_reference_nets", ()) or ()) if str(item))
    proposal_architecture_protected_nets = tuple(str(item) for item in tuple(proposal.get("architecture_protected_nets", ()) or ()) if str(item))
    proposal_binding_blocked_partitions = tuple(
        str(item) for item in tuple(proposal.get("binding_blocked_partitions", ()) or ()) if str(item)
    )
    proposal_macro_bound_partitions = tuple(
        str(item) for item in tuple(proposal.get("macro_bound_partitions", ()) or ()) if str(item)
    )
    proposal_architecture_budget_blocked_partitions = tuple(
        str(item) for item in tuple(proposal.get("architecture_budget_blocked_partitions", ()) or ()) if str(item)
    )
    dispatch_scope_nets = tuple(str(item) for item in tuple(scope_guard.get("scope_nets", ()) or ()) if str(item))
    dispatch_avoid_nets = tuple(str(item) for item in tuple(scope_guard.get("avoid_nets", ()) or ()) if str(item))
    dispatch_restore_bus_nets = tuple(str(item) for item in tuple(scope_guard.get("restore_bus_nets", ()) or ()) if str(item))
    dispatch_restore_feedback_nets = tuple(str(item) for item in tuple(scope_guard.get("restore_feedback_nets", ()) or ()) if str(item))
    dispatch_protected_reference_nets = tuple(str(item) for item in tuple(scope_guard.get("protected_reference_nets", ()) or ()) if str(item))
    dispatch_architecture_protected_nets = tuple(str(item) for item in tuple(scope_guard.get("architecture_protected_nets", ()) or ()) if str(item))
    dispatch_binding_blocked_partitions = tuple(
        str(item) for item in tuple(scope_guard.get("binding_blocked_partitions", ()) or ()) if str(item)
    )
    dispatch_macro_bound_partitions = tuple(
        str(item) for item in tuple(scope_guard.get("macro_bound_partitions", ()) or ()) if str(item)
    )
    dispatch_architecture_budget_blocked_partitions = tuple(
        str(item) for item in tuple(scope_guard.get("architecture_budget_blocked_partitions", ()) or ()) if str(item)
    )
    alignment = {
        "scope_nets_match": set(proposal_scope_nets).issubset(set(dispatch_scope_nets)) if proposal_scope_nets else True,
        "avoid_nets_match": set(proposal_avoid_nets).issubset(set(dispatch_avoid_nets)) if proposal_avoid_nets else True,
        "restore_bus_nets_match": set(proposal_restore_bus_nets).issubset(set(dispatch_restore_bus_nets)) if proposal_restore_bus_nets else True,
        "restore_feedback_nets_match": set(proposal_restore_feedback_nets).issubset(set(dispatch_restore_feedback_nets)) if proposal_restore_feedback_nets else True,
        "protected_reference_nets_match": set(proposal_protected_reference_nets).issubset(set(dispatch_protected_reference_nets)) if proposal_protected_reference_nets else True,
        "architecture_protected_nets_match": set(proposal_architecture_protected_nets).issubset(set(dispatch_architecture_protected_nets)) if proposal_architecture_protected_nets else True,
        "binding_blocked_partitions_match": set(proposal_binding_blocked_partitions).issubset(set(dispatch_binding_blocked_partitions))
        if proposal_binding_blocked_partitions
        else True,
        "macro_bound_partitions_match": set(proposal_macro_bound_partitions).issubset(set(dispatch_macro_bound_partitions))
        if proposal_macro_bound_partitions
        else True,
        "architecture_budget_blocked_partitions_match": set(proposal_architecture_budget_blocked_partitions).issubset(
            set(dispatch_architecture_budget_blocked_partitions)
        )
        if proposal_architecture_budget_blocked_partitions
        else True,
        "scope_policy_match": str(proposal.get("scope_policy", "")) == str(scope_guard.get("scope_policy", "")) if proposal.get("scope_policy") else True,
    }
    return {
        "proposal_kind": str(proposal.get("kind", "")),
        "dispatch_mode": str(dispatch.get("dispatch_mode", "")),
        "writeback_level": str(dispatch.get("writeback_level", "")),
        "writeback_target": str(dispatch.get("writeback_target", "")),
        "proposal_scope_nets": proposal_scope_nets,
        "proposal_avoid_nets": proposal_avoid_nets,
        "proposal_restore_bus_nets": proposal_restore_bus_nets,
        "proposal_restore_feedback_nets": proposal_restore_feedback_nets,
        "proposal_protected_reference_nets": proposal_protected_reference_nets,
        "proposal_architecture_protected_nets": proposal_architecture_protected_nets,
        "proposal_binding_blocked_partitions": proposal_binding_blocked_partitions,
        "proposal_macro_bound_partitions": proposal_macro_bound_partitions,
        "proposal_architecture_budget_blocked_partitions": proposal_architecture_budget_blocked_partitions,
        "dispatch_scope_nets": dispatch_scope_nets,
        "dispatch_avoid_nets": dispatch_avoid_nets,
        "dispatch_restore_bus_nets": dispatch_restore_bus_nets,
        "dispatch_restore_feedback_nets": dispatch_restore_feedback_nets,
        "dispatch_protected_reference_nets": dispatch_protected_reference_nets,
        "dispatch_architecture_protected_nets": dispatch_architecture_protected_nets,
        "dispatch_binding_blocked_partitions": dispatch_binding_blocked_partitions,
        "dispatch_macro_bound_partitions": dispatch_macro_bound_partitions,
        "dispatch_architecture_budget_blocked_partitions": dispatch_architecture_budget_blocked_partitions,
        "proposal_scope_policy": str(proposal.get("scope_policy", "")),
        "dispatch_scope_policy": str(scope_guard.get("scope_policy", "")),
        "system_recommended_level": str(dispatch.get("system_recommended_level", "")),
        "system_scope_escalation_required": bool(dispatch.get("system_scope_escalation_required", False)),
        "alignment": alignment,
        "aligned": all(bool(value) for value in alignment.values()),
    }


def build_hierarchical_repair_execution_contract_from_dispatch_bundle(
    dispatch_bundle: Mapping[str, object],
    *,
    foundry_execution_contract: Mapping[str, object] | None = None,
    candidate_readiness: Mapping[str, object] | None = None,
    physical_contract: Mapping[str, object] | None = None,
    deck_spec: Mapping[str, object] | None = None,
    packaging_spec: Mapping[str, object] | None = None,
    available_inputs: Mapping[str, object] | None = None,
    system_regression_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    decomposition_contract = dict(dispatch_bundle.get("decomposition_contract", {}) or {})
    scope_guard = dict(dispatch_bundle.get("scope_guard", {}) or {})
    if not decomposition_contract:
        hierarchy_contract = dict(dispatch_bundle.get("hierarchy_contract", {}) or {})
        source_cellview = dict(dispatch_bundle.get("source_cellview", {}) or {})
        target_cellview = dict(dispatch_bundle.get("target_cellview", {}) or {})
        decomposition_contract = {
            "dispatch_mode": str(dispatch_bundle.get("dispatch_mode", "")),
            "writeback_level": str(dispatch_bundle.get("writeback_level", "")),
            "writeback_target": str(dispatch_bundle.get("writeback_target", "")),
            "recommended_rerun": str(dispatch_bundle.get("recommended_rerun", "")),
            "rerun_levels": tuple(str(item) for item in tuple(dispatch_bundle.get("rerun_levels", ()) or ()) if str(item)),
            "hierarchy_mode": str(dict(dispatch_bundle.get("orchestration_plan", {}) or {}).get("mode", "")),
            "hierarchy_path": tuple(str(item) for item in tuple(dict(dispatch_bundle.get("orchestration_plan", {}) or {}).get("hierarchy_path", ()) or ()) if str(item)),
            "hierarchy_node_path": tuple(str(item) for item in tuple(hierarchy_contract.get("path", ()) or ()) if str(item)),
            "hierarchy_contract": hierarchy_contract,
            "requires_multi_cell_orchestration": bool(dict(dispatch_bundle.get("orchestration_plan", {}) or {}).get("requires_multi_cell_orchestration", False)),
            "source_cell": str(source_cellview.get("cell", "")),
            "target_cell": str(target_cellview.get("cell", "")),
            "scope_nets": tuple(str(item) for item in tuple(dict(dispatch_bundle.get("scope_guard", {}) or {}).get("scope_nets", ()) or ()) if str(item)),
            "scope_devices": tuple(str(item) for item in tuple(dict(dispatch_bundle.get("scope_guard", {}) or {}).get("scope_devices", ()) or ()) if str(item)),
            "scope_regions": tuple(str(item) for item in tuple(dict(dispatch_bundle.get("scope_guard", {}) or {}).get("scope_regions", ()) or ()) if str(item)),
        }
    stages: list[dict[str, object]] = []
    for raw_stage in tuple(dispatch_bundle.get("decomposed_subactions", ()) or ()):
        if not isinstance(raw_stage, Mapping):
            continue
        stage_apply_unit = dict(raw_stage.get("stage_apply_unit", {}) or {})
        stage_scope_contract = dict(raw_stage.get("stage_scope_contract", {}) or {})
        if "binding_blocked_partitions" not in stage_scope_contract and tuple(scope_guard.get("binding_blocked_partitions", ()) or ()):
            stage_scope_contract["binding_blocked_partitions"] = tuple(
                str(item) for item in tuple(scope_guard.get("binding_blocked_partitions", ()) or ()) if str(item)
            )
        if "macro_bound_partitions" not in stage_scope_contract and tuple(scope_guard.get("macro_bound_partitions", ()) or ()):
            stage_scope_contract["macro_bound_partitions"] = tuple(
                str(item) for item in tuple(scope_guard.get("macro_bound_partitions", ()) or ()) if str(item)
            )
        if "architecture_budget_blocked_partitions" not in stage_scope_contract and tuple(scope_guard.get("architecture_budget_blocked_partitions", ()) or ()):
            stage_scope_contract["architecture_budget_blocked_partitions"] = tuple(
                str(item)
                for item in tuple(scope_guard.get("architecture_budget_blocked_partitions", ()) or ())
                if str(item)
            )
        stage_contract = dict(raw_stage.get("stage_contract", {}) or {})
        target_cellview = dict(raw_stage.get("target_cellview", {}) or stage_apply_unit.get("target_cellview", {}) or {})
        stages.append(
            {
                "order": int(raw_stage.get("order", 0) or 0),
                "role": str(raw_stage.get("role", "")),
                "execution_kind": str(raw_stage.get("execution_kind", stage_apply_unit.get("execution_kind", ""))),
                "target_cellview": target_cellview,
                "backend_applicable": bool(raw_stage.get("backend_applicable", stage_apply_unit.get("backend_applicable", False))),
                "requires_stage_specific_synthesis": bool(raw_stage.get("requires_stage_specific_synthesis", stage_apply_unit.get("requires_stage_specific_synthesis", False))),
                "materialized_summary": dict(raw_stage.get("stage_retargeted_summary", {}) or {}),
                "stage_contract": stage_contract,
                "stage_scope_contract": stage_scope_contract,
                "validation": {},
                "partition_execution_targets": tuple(raw_stage.get("partition_execution_targets", ()) or ()),
                "system_contract_targets": tuple(raw_stage.get("system_contract_targets", ()) or ()),
            }
        )
    bundle = {
        "dispatch_mode": str(dispatch_bundle.get("dispatch_mode", "")),
        "writeback_level": str(dispatch_bundle.get("writeback_level", "")),
        "writeback_target": str(dispatch_bundle.get("writeback_target", "")),
        "recommended_rerun": str(dispatch_bundle.get("recommended_rerun", "")),
        "rerun_levels": tuple(str(item) for item in tuple(dispatch_bundle.get("rerun_levels", ()) or ()) if str(item)),
        "decomposition_contract": decomposition_contract,
        "stages": tuple(stages),
    }
    return build_hierarchical_repair_execution_contract(
        bundle,
        foundry_execution_contract=foundry_execution_contract,
        candidate_readiness=candidate_readiness,
        physical_contract=physical_contract,
        deck_spec=deck_spec,
        packaging_spec=packaging_spec,
        available_inputs=available_inputs,
        system_regression_report=system_regression_report,
    )


def build_hierarchical_stage_insight_overlay_from_dispatch_bundle(
    dispatch_bundle: Mapping[str, object],
    *,
    foundry_execution_contract: Mapping[str, object] | None = None,
    candidate_readiness: Mapping[str, object] | None = None,
    physical_contract: Mapping[str, object] | None = None,
    deck_spec: Mapping[str, object] | None = None,
    packaging_spec: Mapping[str, object] | None = None,
    available_inputs: Mapping[str, object] | None = None,
    system_regression_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    contract = build_hierarchical_repair_execution_contract_from_dispatch_bundle(
        dispatch_bundle,
        foundry_execution_contract=foundry_execution_contract,
        candidate_readiness=candidate_readiness,
        physical_contract=physical_contract,
        deck_spec=deck_spec,
        packaging_spec=packaging_spec,
        available_inputs=available_inputs,
        system_regression_report=system_regression_report,
    )
    return build_hierarchical_stage_insight_overlay(contract)


def build_hierarchical_repair_scope_intent_from_dispatch_bundle(
    dispatch_bundle: Mapping[str, object],
    *,
    foundry_execution_contract: Mapping[str, object] | None = None,
    candidate_readiness: Mapping[str, object] | None = None,
    physical_contract: Mapping[str, object] | None = None,
    deck_spec: Mapping[str, object] | None = None,
    packaging_spec: Mapping[str, object] | None = None,
    available_inputs: Mapping[str, object] | None = None,
    system_regression_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    contract = build_hierarchical_repair_execution_contract_from_dispatch_bundle(
        dispatch_bundle,
        foundry_execution_contract=foundry_execution_contract,
        candidate_readiness=candidate_readiness,
        physical_contract=physical_contract,
        deck_spec=deck_spec,
        packaging_spec=packaging_spec,
        available_inputs=available_inputs,
        system_regression_report=system_regression_report,
    )
    return build_hierarchical_repair_scope_intent(contract)


def build_hierarchical_dispatch_scope_overlay_from_dispatch_bundle(
    dispatch_bundle: Mapping[str, object],
    *,
    foundry_execution_contract: Mapping[str, object] | None = None,
    candidate_readiness: Mapping[str, object] | None = None,
    physical_contract: Mapping[str, object] | None = None,
    deck_spec: Mapping[str, object] | None = None,
    packaging_spec: Mapping[str, object] | None = None,
    available_inputs: Mapping[str, object] | None = None,
    system_regression_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    contract = build_hierarchical_repair_execution_contract_from_dispatch_bundle(
        dispatch_bundle,
        foundry_execution_contract=foundry_execution_contract,
        candidate_readiness=candidate_readiness,
        physical_contract=physical_contract,
        deck_spec=deck_spec,
        packaging_spec=packaging_spec,
        available_inputs=available_inputs,
        system_regression_report=system_regression_report,
    )
    return build_hierarchical_dispatch_scope_overlay(contract)


def build_hierarchical_dispatch_bundle_contracts(
    dispatch_bundle: Mapping[str, object],
    *,
    hierarchy_database: object | None = None,
    foundry_execution_contract: Mapping[str, object] | None = None,
    candidate_readiness: Mapping[str, object] | None = None,
    physical_contract: Mapping[str, object] | None = None,
    deck_spec: Mapping[str, object] | None = None,
    packaging_spec: Mapping[str, object] | None = None,
    available_inputs: Mapping[str, object] | None = None,
    system_regression_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    contract = build_hierarchical_repair_execution_contract_from_dispatch_bundle(
        dispatch_bundle,
        foundry_execution_contract=foundry_execution_contract,
        candidate_readiness=candidate_readiness,
        physical_contract=physical_contract,
        deck_spec=deck_spec,
        packaging_spec=packaging_spec,
        available_inputs=available_inputs,
        system_regression_report=system_regression_report,
    )
    if hierarchy_database is not None:
        contract = annotate_hierarchical_repair_execution_with_hierarchy_database(
            contract,
            hierarchy_database,
        )
    proposal_scope_summary = build_dispatch_bundle_scope_proposal_summary(dispatch_bundle)
    return {
        "execution_contract": contract,
        "stage_insight_overlay": build_hierarchical_stage_insight_overlay(contract),
        "repair_scope_intent": build_hierarchical_repair_scope_intent(contract),
        "dispatch_scope_overlay": build_hierarchical_dispatch_scope_overlay(contract),
        "proposal_scope_summary": proposal_scope_summary,
        "post_layout_eco_dispatch_scope_overlay": (
            build_post_layout_eco_dispatch_scope_overlay(proposal_scope_summary, dispatch_bundle)
            if str(proposal_scope_summary.get("kind", "")) in {"post_layout_pex_route_eco", "pex_layout_eco", "post_layout"}
            else {}
        ),
    }


def build_hierarchy_indexed_execution_contract_view(
    contract: Mapping[str, object],
) -> dict[str, object]:
    stages = tuple(contract.get("stages", ()) or ())
    by_cell: dict[str, dict[str, object]] = {}
    by_node: dict[str, dict[str, object]] = {}
    by_path_index: dict[int, dict[str, object]] = {}
    hierarchy_contract = dict(contract.get("hierarchy_contract", {}) or {})
    path_nodes = tuple(hierarchy_contract.get("path_nodes", ()) or ())
    node_to_index = {
        str(dict(item).get("name", "")): index
        for index, item in enumerate(path_nodes)
        if isinstance(item, Mapping) and str(dict(item).get("name", ""))
    }
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        stage_row = _serialize_hierarchy_indexed_execution_stage_row(stage)
        target_cell = str(stage_row.get("target_cell", ""))
        stage_node = str(stage_row.get("stage_hierarchy_node", ""))
        binding = dict(stage_row.get("stage_hierarchy_binding", {}) or {})
        if target_cell:
            by_cell[target_cell] = stage_row
        bound_cell = str(binding.get("cell", ""))
        if bound_cell and bound_cell not in by_cell:
            by_cell[bound_cell] = stage_row
        if stage_node:
            by_node[stage_node] = stage_row
        bound_name = str(binding.get("name", ""))
        if bound_name and bound_name not in by_node:
            by_node[bound_name] = stage_row
        lookup_name = bound_name or stage_node
        if lookup_name and lookup_name in node_to_index:
            by_path_index[int(node_to_index[lookup_name])] = stage_row
    return {
        "hierarchy_contract": hierarchy_contract,
        "stage_count": len(stages),
        "by_cell": by_cell,
        "by_node": by_node,
        "by_path_index": by_path_index,
    }


def query_hierarchy_indexed_execution_contract_view(
    contract: Mapping[str, object],
    *,
    cell: object | None = None,
    node: object | None = None,
    path_index: object | None = None,
) -> dict[str, object]:
    view = build_hierarchy_indexed_execution_contract_view(contract)
    if path_index is not None:
        try:
            index = int(path_index)
        except (TypeError, ValueError):
            index = -1
        return dict(view.get("by_path_index", {}).get(index, {}))
    if cell is not None:
        return dict(view.get("by_cell", {}).get(str(cell), {}))
    if node is not None:
        return dict(view.get("by_node", {}).get(str(node), {}))
    return view


def _serialize_hierarchy_indexed_execution_stage_row(
    stage: Mapping[str, object],
) -> dict[str, object]:
    row = dict(stage)
    verification = dict(row.get("verification", {}) or {})
    writeback = dict(row.get("writeback", {}) or {})
    target_cell = str(
        row.get("target_cell", "")
        or verification.get("target_cell", "")
        or writeback.get("target_cell", "")
    )
    stage_hierarchy_node = str(
        row.get("stage_hierarchy_node", "")
        or verification.get("stage_hierarchy_node", "")
        or writeback.get("stage_hierarchy_node", "")
    )
    stage_hierarchy_binding = dict(
        row.get("stage_hierarchy_binding", {})
        or verification.get("stage_hierarchy_binding", {})
        or writeback.get("stage_hierarchy_binding", {})
        or {}
    )
    allowed_scope_nets = tuple(
        str(item)
        for item in tuple(writeback.get("allowed_scope_nets", ()) or verification.get("scope_nets", ()) or ())
        if str(item)
    )
    blocked_scope_nets = tuple(
        str(item)
        for item in tuple(writeback.get("blocked_scope_nets", ()) or ())
        if str(item)
    )
    allowed_scope_devices = tuple(
        str(item)
        for item in tuple(writeback.get("allowed_scope_devices", ()) or verification.get("scope_devices", ()) or ())
        if str(item)
    )
    blocked_scope_devices = tuple(
        str(item)
        for item in tuple(writeback.get("blocked_scope_devices", ()) or ())
        if str(item)
    )
    protected_reference_nets = tuple(
        str(item)
        for item in tuple(writeback.get("protected_reference_nets", ()) or ())
        if str(item)
    )
    architecture_protected_nets = tuple(
        str(item)
        for item in tuple(writeback.get("architecture_protected_nets", ()) or ())
        if str(item)
    )
    return {
        **row,
        "target_cell": target_cell,
        "stage_hierarchy_node": stage_hierarchy_node,
        "stage_hierarchy_binding": stage_hierarchy_binding,
        "target_cellview": dict(row.get("target_cellview", {}) or verification.get("target_cellview", {}) or writeback.get("target_cellview", {}) or {}),
        "allowed_scope_nets": allowed_scope_nets,
        "blocked_scope_nets": blocked_scope_nets,
        "allowed_scope_devices": allowed_scope_devices,
        "blocked_scope_devices": blocked_scope_devices,
        "protected_reference_nets": protected_reference_nets,
        "architecture_protected_nets": architecture_protected_nets,
        "system_recommended_level": str(row.get("system_recommended_level", "") or writeback.get("system_recommended_level", "")),
        "system_scope_escalation_required": bool(
            row.get("system_scope_escalation_required", False)
            or writeback.get("system_scope_escalation_required", False)
        ),
    }


def build_dispatch_bundle_scope_proposal_summary(
    dispatch_bundle: Mapping[str, object],
) -> dict[str, object]:
    dispatch = dict(dispatch_bundle or {})
    scope_guard = dict(dispatch.get("scope_guard", {}) or {})
    stages = tuple(dispatch.get("decomposed_subactions", ()) or ())
    candidate_stage: Mapping[str, object] | None = None
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        summary = dict(stage.get("stage_retargeted_summary", {}) or {})
        kind = str(summary.get("kind", ""))
        if kind in {"post_layout_pex_route_eco", "pex_layout_eco", "post_layout"}:
            candidate_stage = stage
    if candidate_stage is None:
        for stage in stages:
            if not isinstance(stage, Mapping):
                continue
            contract = dict(stage.get("stage_contract", {}) or {})
            kind = str(contract.get("proposal_kind", ""))
            if kind in {"post_layout", "post_layout_pex_route_eco", "pex_layout_eco"}:
                candidate_stage = stage
    if candidate_stage is None:
        return {}
    stage = dict(candidate_stage)
    summary = dict(stage.get("stage_retargeted_summary", {}) or {})
    stage_contract = dict(stage.get("stage_contract", {}) or {})
    stage_scope = dict(stage.get("stage_scope_contract", {}) or {})
    kind = str(summary.get("kind", "") or stage_contract.get("proposal_kind", ""))
    if not kind:
        return {}
    return {
        "kind": kind,
        "selected_plan_kind": str(summary.get("selected_plan_kind", "") or stage_contract.get("selected_plan_kind", "")),
        "scope_nets": tuple(
            str(item)
            for item in tuple(
                stage_scope.get("allowed_scope_nets", ())
                or stage_scope.get("scope_nets", ())
                or scope_guard.get("scope_nets", ())
                or ()
            )
            if str(item)
        ),
        "avoid_nets": tuple(
            str(item)
            for item in tuple(
                scope_guard.get("avoid_nets", ())
                or stage_scope.get("blocked_scope_nets", ())
                or ()
            )
            if str(item)
        ),
        "scope_policy": str(stage_scope.get("scope_policy", "") or scope_guard.get("scope_policy", "")),
        "restore_bus_nets": tuple(
            str(item)
            for item in tuple(
                stage_scope.get("restore_bus_nets", ())
                or scope_guard.get("restore_bus_nets", ())
                or ()
            )
            if str(item)
        ),
        "restore_feedback_nets": tuple(
            str(item)
            for item in tuple(
                stage_scope.get("restore_feedback_nets", ())
                or scope_guard.get("restore_feedback_nets", ())
                or ()
            )
            if str(item)
        ),
        "protected_reference_nets": tuple(
            str(item)
            for item in tuple(
                stage_scope.get("protected_reference_nets", ())
                or scope_guard.get("protected_reference_nets", ())
                or ()
            )
            if str(item)
        ),
        "architecture_protected_nets": tuple(
            str(item)
            for item in tuple(
                stage_scope.get("architecture_protected_nets", ())
                or scope_guard.get("architecture_protected_nets", ())
                or ()
            )
            if str(item)
        ),
        "binding_blocked_partitions": tuple(
            str(item)
            for item in tuple(
                stage_scope.get("binding_blocked_partitions", ())
                or scope_guard.get("binding_blocked_partitions", ())
                or ()
            )
            if str(item)
        ),
        "macro_bound_partitions": tuple(
            str(item)
            for item in tuple(
                stage_scope.get("macro_bound_partitions", ())
                or scope_guard.get("macro_bound_partitions", ())
                or ()
            )
            if str(item)
        ),
        "architecture_budget_blocked_partitions": tuple(
            str(item)
            for item in tuple(
                stage_scope.get("architecture_budget_blocked_partitions", ())
                or scope_guard.get("architecture_budget_blocked_partitions", ())
                or ()
            )
            if str(item)
        ),
    }


def _stage_required_foundry_checks(
    proposal_kind: str,
    stage_verification_contract: Mapping[str, object],
    *,
    restore_required: bool,
    reference_sensitive: bool,
) -> tuple[str, ...]:
    checks = ["drc", "lvs"]
    kind = str(proposal_kind or "")
    scope_nets = tuple(str(net) for net in tuple(stage_verification_contract.get("scope_nets", ()) or ()) if str(net))
    if "post_layout" in kind or restore_required or reference_sensitive or scope_nets:
        checks.append("pex")
    return tuple(dict.fromkeys(checks))


def _system_contract_targets_for_stage(
    *,
    stage_nets: set[str],
    stage_nodes: set[str],
    system_contract: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    targets: list[dict[str, object]] = []
    for item in tuple(system_contract.get("interface_contracts", ()) or ()):
        row = dict(item) if isinstance(item, Mapping) else {}
        net = str(row.get("net", ""))
        if net and net in stage_nets:
            targets.append(
                {
                    "kind": "interface_contract",
                    "net": net,
                    "source": str(row.get("source", "")),
                    "target": str(row.get("target", "")),
                    "contract_kind": str(row.get("kind", "")),
                    "focus": bool(row.get("focus", False)),
                }
            )
    for item in tuple(system_contract.get("bus_contracts", ()) or ()):
        row = dict(item) if isinstance(item, Mapping) else {}
        nets = {str(net) for net in tuple(row.get("nets", ()) or ()) if str(net)}
        if stage_nets & nets:
            targets.append(
                {
                    "kind": "bus_contract",
                    "name": str(row.get("name", "")),
                    "nets": tuple(sorted(stage_nets & nets)),
                    "source": str(row.get("source", "")),
                    "target": str(row.get("target", "")),
                    "restore_required": bool(row.get("restore_required", False)),
                }
            )
    for item in tuple(system_contract.get("reference_paths", ()) or ()):
        row = dict(item) if isinstance(item, Mapping) else {}
        net = str(row.get("net", ""))
        if net and net in stage_nets:
            targets.append(
                {
                    "kind": "reference_path",
                    "net": net,
                    "source": str(row.get("source", "")),
                    "target": str(row.get("target", "")),
                    "preserve_integrity": bool(row.get("preserve_integrity", False)),
                }
            )
    for item in tuple(system_contract.get("feedback_contracts", ()) or ()):
        row = dict(item) if isinstance(item, Mapping) else {}
        net = str(row.get("net", ""))
        if net and net in stage_nets:
            targets.append(
                {
                    "kind": "feedback_contract",
                    "net": net,
                    "source": str(row.get("source", "")),
                    "target": str(row.get("target", "")),
                    "restore_required": bool(row.get("restore_required", False)),
                }
            )
    for item in tuple(system_contract.get("timing_chains", ()) or ()):
        row = dict(item) if isinstance(item, Mapping) else {}
        blocks = {str(block) for block in tuple(row.get("blocks", ()) or ()) if str(block)}
        if stage_nodes & blocks:
            targets.append(
                {
                    "kind": "timing_chain",
                    "name": str(row.get("name", "")),
                    "blocks": tuple(str(block) for block in tuple(row.get("blocks", ()) or ()) if str(block)),
                    "preserve_order": bool(row.get("preserve_order", False)),
                }
            )
    return tuple(targets)


def _match_partition_hierarchy_nodes(
    partition: str,
    hierarchy_database: tuple[HierarchyCellviewNode, ...],
    explicit_targets: tuple[str, ...] = (),
) -> tuple[HierarchyCellviewNode, ...]:
    if explicit_targets:
        rows = []
        for target in explicit_targets:
            node = _match_hierarchy_node(hierarchy_database, target)
            if node is not None:
                rows.append(node)
        return tuple(dict.fromkeys(rows))
    lowered = partition.lower()
    rows = []
    for node in hierarchy_database:
        haystack = {node.name.lower(), node.cell.lower(), *(alias.lower() for alias in node.aliases)}
        if lowered in haystack or any(lowered in token for token in haystack):
            rows.append(node)
    return tuple(rows)


def apply_synthesized_verification_stage_proposal(
    proposal: object,
    *,
    backend: object,
) -> object:
    from analogskills.repair import apply_repair_proposal

    return apply_repair_proposal(proposal, backend)


def validate_synthesized_verification_stage_proposal(
    proposal: object,
    *,
    stage_apply_object: Mapping[str, object],
) -> VerificationStageProposalValidation:
    artifact = dict(stage_apply_object.get("stage_synthesis_artifact", {}) or {})
    dispatch_plan = dict(stage_apply_object.get("stage_dispatch_plan", {}) or {})
    if proposal is None:
        return VerificationStageProposalValidation(
            valid=False,
            reason="no_synthesized_proposal",
            details={"artifact": artifact},
        )
    if _synthesized_stage_proposal_is_empty(proposal):
        return VerificationStageProposalValidation(
            valid=False,
            reason="empty_after_region_filter",
            details={"artifact": artifact},
        )
    overlap_reason = _validate_required_stage_scope_overlap(proposal, artifact)
    if overlap_reason:
        return VerificationStageProposalValidation(
            valid=False,
            reason=overlap_reason,
            details={"artifact": artifact, "proposal_nets": tuple(sorted(_repair_proposal_nets(proposal)))},
        )
    connectivity_reason = _validate_synthesized_stage_connectivity(proposal)
    if connectivity_reason:
        return VerificationStageProposalValidation(
            valid=False,
            reason=connectivity_reason,
            details={"artifact": artifact},
        )
    scope_check = _verify_dispatch_scope_guard(proposal, dispatch_plan)
    if scope_check.get("allowed", False) is False:
        return VerificationStageProposalValidation(
            valid=False,
            reason=str(scope_check.get("reason", "scope_guard_violation")),
            details=scope_check,
        )
    return VerificationStageProposalValidation(
        valid=True,
        reason="valid_synthesized_stage_proposal",
        details={
            "artifact": artifact,
            "proposal_bbox": _repair_proposal_bbox(proposal),
            "proposal_edit_count": _repair_proposal_edit_count(proposal),
        },
    )


def _run_dispatch_executor(
    dispatch_executor: object,
    plan: VerificationRepairExecutionPlan,
    dispatch_bundle: Mapping[str, object],
) -> object:
    if callable(dispatch_executor):
        return dispatch_executor(plan, dispatch_bundle)
    if hasattr(dispatch_executor, "run"):
        return dispatch_executor.run(plan, dispatch_bundle)
    raise TypeError("dispatch_executor must be callable or expose a run(plan, dispatch_bundle) method")


def _decompose_verification_repair_subactions(
    plan: VerificationRepairExecutionPlan,
) -> tuple[dict[str, object], ...]:
    orchestration_plan = dict(plan.dispatch_plan.get("orchestration_plan", {}) or {})
    stages = tuple(orchestration_plan.get("stages", ()) or ())
    if not stages:
        return ()
    proposal_summary = _repair_proposal_dispatch_summary(getattr(plan, "repair_proposal", None))
    subactions: list[dict[str, object]] = []
    for stage in stages:
        if not isinstance(stage, Mapping):
            continue
        role = str(stage.get("role", ""))
        cell = str(stage.get("cell", ""))
        action = str(stage.get("action", ""))
        target_cellview = dict(stage.get("target_cellview", {}) or {})
        if not target_cellview and cell:
            target_cellview = {"cell": cell}
        stage_dispatch_plan = _stage_dispatch_plan(plan, stage, target_cellview)
        stage_summary = _retargeted_repair_proposal_summary(plan, stage_dispatch_plan)
        stage_execution_profile = _stage_execution_profile(
            plan,
            stage,
            stage_dispatch_plan=stage_dispatch_plan,
            target_cellview=target_cellview,
        )
        stage_apply_unit = _stage_apply_unit(
            plan,
            role=role,
            execution_kind=(
                "target_writeback"
                if role == "target"
                else ("manual_handoff" if plan.dispatch_mode == "manual_orchestrated_apply" else "stage_preparation")
            ),
            stage_dispatch_plan=stage_dispatch_plan,
            stage_summary=stage_summary,
            stage_execution_profile=stage_execution_profile,
        )
        subactions.append(
            {
                "order": int(stage.get("order", len(subactions) + 1) or len(subactions) + 1),
                "cell": cell,
                "role": role,
                "action": action,
                "dispatch_mode": str(stage.get("dispatch_mode", plan.dispatch_mode)),
                "scope_level": str(stage.get("scope_level", plan.writeback_level)),
                "target_cellview": target_cellview,
                "proposal_summary": proposal_summary,
                "execution_profile": stage_execution_profile,
                "stage_dispatch_plan": stage_dispatch_plan,
                "stage_retargeted_summary": stage_summary,
                "retargeted_proposal_summary": stage_summary if role == "target" else {},
                "stage_apply_unit": stage_apply_unit,
                "execution_kind": str(stage_apply_unit.get("execution_kind", "")),
                "editable": bool(stage_apply_unit.get("editable", role == "target" or plan.dispatch_mode == "manual_orchestrated_apply")),
            }
        )
    return tuple(subactions)


def _repair_proposal_dispatch_summary(proposal: object | None) -> dict[str, object]:
    if proposal is None:
        return {}
    try:
        from analogskills.repair import repair_proposal_summary
    except ImportError:
        return {}
    try:
        summary = repair_proposal_summary(proposal)
    except TypeError:
        return {}
    return dict(summary)


def _retargeted_repair_proposal_summary(
    plan: VerificationRepairExecutionPlan,
    dispatch_plan: Mapping[str, object] | None = None,
) -> dict[str, object]:
    proposal = getattr(plan, "repair_proposal", None)
    if proposal is None:
        return {}
    active_dispatch_plan = plan.dispatch_plan if dispatch_plan is None else dispatch_plan
    retargeted = _retarget_repair_proposal_for_dispatch(proposal, active_dispatch_plan)
    summary = _repair_proposal_dispatch_summary(retargeted)
    if summary:
        summary["target_cellview"] = dict(active_dispatch_plan.get("target_cellview", {}) or {})
    return summary


def _synthesize_post_layout_intermediate_stage_proposal(
    plan: VerificationRepairExecutionPlan,
    proposal: object,
    stage_apply_object: Mapping[str, object],
    artifact: Mapping[str, object],
) -> object | None:
    from analogskills.repair import DrcRepairProposal, LvsRepairProposal, PostLayoutEcoRepairProposal

    stage_dispatch_plan = dict(stage_apply_object.get("stage_dispatch_plan", {}) or {})
    if isinstance(proposal, PostLayoutEcoRepairProposal):
        proposal_kind = str(artifact.get("source_proposal_kind", "")) or str(getattr(proposal, "kind", ""))
        if proposal_kind not in {"pex_layout_eco", "post_layout_pex_route_eco"}:
            return None
        stage_proposal = _retarget_repair_proposal_for_dispatch(proposal, stage_dispatch_plan)
        if not isinstance(stage_proposal, PostLayoutEcoRepairProposal):
            return None
        clipped_layout = _clip_layout_plan_to_stage_region(stage_proposal.layout_patch, artifact)
        clipped_oa = _clip_oa_write_plan_to_stage_region(stage_proposal.oa_patch, artifact)
        synthesis_metadata = _merge_stage_synthesis_metadata(
            getattr(stage_proposal, "metadata", {}) or {},
            stage_apply_object,
            artifact,
        )
        return replace(
            stage_proposal,
            layout_patch=replace(
                clipped_layout,
                metadata={**dict(getattr(clipped_layout, "metadata", {}) or {}), **synthesis_metadata},
            ),
            oa_patch=clipped_oa,
            metadata=synthesis_metadata,
        )
    if isinstance(proposal, (DrcRepairProposal, LvsRepairProposal)):
        stage_proposal = _retarget_repair_proposal_for_dispatch(proposal, stage_dispatch_plan)
        if not isinstance(stage_proposal, (DrcRepairProposal, LvsRepairProposal)):
            return None
        return _stamp_candidate_proposal_synthesis_metadata(stage_proposal, stage_apply_object, artifact)
    return None


def _merge_stage_synthesis_metadata(
    metadata: Mapping[str, object],
    stage_apply_object: Mapping[str, object],
    artifact: Mapping[str, object],
) -> dict[str, object]:
    return {
        **dict(metadata),
        "synthesized_from_stage_artifact": True,
        "stage_role": str(stage_apply_object.get("role", "")),
        "stage_execution_kind": str(stage_apply_object.get("execution_kind", "")),
        "stage_target_cell": str(dict(artifact.get("target_cellview", {}) or {}).get("cell", "")),
        "stage_synthesis_goal": str(artifact.get("synthesis_goal", "")),
        "scope_nets": tuple(str(net) for net in artifact.get("scope_nets", ()) if str(net)),
        "scope_devices": tuple(str(device) for device in artifact.get("scope_devices", ()) if str(device)),
        "scope_regions": tuple(str(region) for region in artifact.get("scope_regions", ()) if str(region)),
        "region_bbox": artifact.get("region_bbox"),
        "issue_bbox": artifact.get("issue_bbox"),
        "scope_policy": str(artifact.get("scope_policy", "")),
    }


def _build_parent_route_stage_proposal(
    plan: VerificationRepairExecutionPlan,
    proposal: object,
    stage_apply_object: Mapping[str, object],
    artifact: Mapping[str, object],
    *,
    route_plan: object | None,
    pdk: object | None,
) -> object | None:
    from analogskills.layout import plan_pex_hotspot_layout_ir
    from analogskills.pdk import PdkConfig
    from analogskills.repair import PostLayoutEcoRepairProposal

    if not isinstance(proposal, PostLayoutEcoRepairProposal):
        return None
    proposal_kind = str(getattr(proposal, "kind", ""))
    if proposal_kind not in {"pex_layout_eco", "post_layout_pex_route_eco"}:
        return None
    if str(stage_apply_object.get("role", "")) not in {"intermediate", "target"}:
        return None
    base_route_plan = route_plan or getattr(proposal, "layout_patch", None)
    if base_route_plan is None or not hasattr(base_route_plan, "paths"):
        return None
    target_cellview = dict(stage_apply_object.get("target_cellview", {}) or {})
    scope_nets = tuple(str(net) for net in artifact.get("scope_nets", ()) if str(net))
    if not scope_nets:
        scope_nets = tuple(str(net) for net in getattr(proposal, "hotspot_nets", ()) if str(net))
    if not scope_nets:
        return None
    stage_target_cellview = dict(target_cellview)
    if not stage_target_cellview:
        stage_target_cellview = dict(artifact.get("target_cellview", {}) or {})
    hotspot_evidence = tuple(
        PexHotspot(
            net=str(net),
            cap_f=0.0,
            res_ohm=0.0,
            critical=True,
            issues=("stage_scope_route_rebuild",),
            score=0.0,
        )
        for net in scope_nets
    )
    active_pdk = pdk if pdk is not None else PdkConfig.generic()
    route_patch = plan_pex_hotspot_layout_ir(
        base_route_plan,
        hotspot_evidence,
        active_pdk,
        lib=str(stage_target_cellview.get("lib", getattr(getattr(base_route_plan, "cell", None), "lib", "work"))),
        cell=str(stage_target_cellview.get("cell", getattr(getattr(base_route_plan, "cell", None), "cell", "route_stage"))),
        view=str(stage_target_cellview.get("view", getattr(getattr(base_route_plan, "cell", None), "view", "layout"))),
        allowed_nets=scope_nets,
        blocked_nets=tuple(str(net) for net in artifact.get("blocked_nets", ()) if str(net)),
        scope_policy=str(artifact.get("scope_policy", "allowed_nets_only") or "allowed_nets_only"),
    )
    clipped_layout = _clip_layout_plan_to_stage_region(route_patch, artifact)
    clipped_oa = _clip_oa_write_plan_to_stage_region(_layout_plan_to_oa_write_plan_for_stage(route_patch), artifact)
    metadata = _merge_stage_synthesis_metadata(
        {
            **dict(getattr(proposal, "metadata", {}) or {}),
            "built_by_parent_route_stage_tool": True,
            "source_hotspot_nets": tuple(scope_nets),
        },
        stage_apply_object,
        artifact,
    )
    return PostLayoutEcoRepairProposal(
        kind="post_layout_parent_route_stage_eco",
        layout_patch=replace(
            clipped_layout,
            metadata={**dict(getattr(clipped_layout, "metadata", {}) or {}), **metadata},
        ),
        oa_patch=clipped_oa,
        score=float(getattr(proposal, "score", 0.0) or 0.0),
        passed=False,
        issues_after=tuple(getattr(proposal, "issues_after", ()) or ()),
        hotspot_nets=tuple(scope_nets),
        source=str(getattr(proposal, "source", "pex_layout_eco")),
        metadata=metadata,
    )


def _stamp_candidate_proposal_synthesis_metadata(
    proposal: object,
    stage_apply_object: Mapping[str, object],
    artifact: Mapping[str, object],
) -> object:
    selected = getattr(proposal, "selected_candidate", None)
    if selected is None:
        return proposal
    selected_plan = getattr(selected, "plan", None)
    stamped_plan = _stamp_repair_plan_stage_synthesis_metadata(selected_plan, stage_apply_object, artifact)
    stamped_selected = replace(selected, plan=stamped_plan)
    candidates = tuple(
        stamped_selected if candidate is selected else candidate
        for candidate in tuple(getattr(proposal, "candidates", ()) or ())
    )
    return replace(proposal, selected_candidate=stamped_selected, candidates=candidates)


def _stamp_repair_plan_stage_synthesis_metadata(
    repair_plan: object,
    stage_apply_object: Mapping[str, object],
    artifact: Mapping[str, object],
) -> object:
    from analogskills.repair import DrcReplacementPlan, LocalizedDrcPatchPlan, LvsShortReplacementPlan

    if isinstance(repair_plan, LocalizedDrcPatchPlan):
        return replace(
            repair_plan,
            layout_patch=_stamp_layout_stage_synthesis_metadata(repair_plan.layout_patch, stage_apply_object, artifact),
        )
    if isinstance(repair_plan, DrcReplacementPlan):
        return replace(
            repair_plan,
            replacement_layout=_stamp_layout_stage_synthesis_metadata(repair_plan.replacement_layout, stage_apply_object, artifact),
        )
    if isinstance(repair_plan, LvsShortReplacementPlan):
        return replace(
            repair_plan,
            replacement_layout=_stamp_layout_stage_synthesis_metadata(repair_plan.replacement_layout, stage_apply_object, artifact),
        )
    return repair_plan


def _stamp_layout_stage_synthesis_metadata(
    layout_plan: object,
    stage_apply_object: Mapping[str, object],
    artifact: Mapping[str, object],
) -> object:
    metadata = _merge_stage_synthesis_metadata(
        getattr(layout_plan, "metadata", {}) or {},
        stage_apply_object,
        artifact,
    )
    clipped = _clip_layout_plan_to_stage_region(layout_plan, artifact)
    return replace(clipped, metadata={**dict(getattr(clipped, "metadata", {}) or {}), **metadata})


def _clip_layout_plan_to_stage_region(
    layout_plan: object,
    artifact: Mapping[str, object],
) -> object:
    region_bbox = _coerce_bbox(artifact.get("region_bbox"))
    if region_bbox is None:
        return layout_plan
    rects = tuple(
        replace(rect, bbox=clipped_bbox)
        for rect in getattr(layout_plan, "rects", ())
        for clipped_bbox in (_clip_bbox_to_region(region_bbox, getattr(rect, "bbox", None)),)
        if clipped_bbox is not None
    )
    paths = tuple(
        clipped_path
        for path in getattr(layout_plan, "paths", ())
        for clipped_path in (_clip_layout_path_to_region(path, region_bbox),)
        if clipped_path is not None
    )
    vias = tuple(
        via
        for via in getattr(layout_plan, "vias", ())
        if _point_in_bbox_for_stage_clip(region_bbox, getattr(via, "xy", None))
    )
    pins = tuple(
        pin if getattr(pin, "bbox", None) is None else replace(pin, bbox=clipped_bbox)
        for pin in getattr(layout_plan, "pins", ())
        for clipped_bbox in (
            getattr(pin, "bbox", None) is None
            and (None,)
            or (_clip_bbox_to_region(region_bbox, getattr(pin, "bbox", None)),)
        )
        if getattr(pin, "bbox", None) is None or clipped_bbox is not None
    )
    labels = tuple(
        label
        for label in getattr(layout_plan, "labels", ())
        if _point_in_bbox_for_stage_clip(region_bbox, getattr(label, "xy", None))
    )
    metadata = {
        **dict(getattr(layout_plan, "metadata", {}) or {}),
        "region_clipped_for_stage_synthesis": True,
        "region_clip_bbox": region_bbox,
    }
    return replace(
        layout_plan,
        rects=rects,
        paths=paths,
        vias=vias,
        pins=pins,
        labels=labels,
        metadata=metadata,
    )


def _clip_oa_write_plan_to_stage_region(
    oa_plan: object,
    artifact: Mapping[str, object],
) -> object:
    region_bbox = _coerce_bbox(artifact.get("region_bbox"))
    if region_bbox is None:
        return oa_plan
    rects = tuple(
        replace(rect, bbox=clipped_bbox)
        for rect in getattr(oa_plan, "rects", ())
        for clipped_bbox in (_clip_bbox_to_region(region_bbox, getattr(rect, "bbox", None)),)
        if clipped_bbox is not None
    )
    paths = tuple(
        clipped_path
        for path in getattr(oa_plan, "paths", ())
        for clipped_path in (_clip_oa_path_to_region(path, region_bbox),)
        if clipped_path is not None
    )
    vias = tuple(
        via
        for via in getattr(oa_plan, "vias", ())
        if _point_in_bbox_for_stage_clip(region_bbox, getattr(via, "xy", None))
    )
    pins = tuple(
        pin if getattr(pin, "bbox", None) is None else replace(pin, bbox=clipped_bbox)
        for pin in getattr(oa_plan, "pins", ())
        for clipped_bbox in (
            getattr(pin, "bbox", None) is None
            and (None,)
            or (_clip_bbox_to_region(region_bbox, getattr(pin, "bbox", None)),)
        )
        if getattr(pin, "bbox", None) is None or clipped_bbox is not None
    )
    labels = tuple(
        label
        for label in getattr(oa_plan, "labels", ())
        if _point_in_bbox_for_stage_clip(region_bbox, getattr(label, "xy", None))
    )
    return replace(
        oa_plan,
        rects=rects,
        paths=paths,
        vias=vias,
        pins=pins,
        labels=labels,
    )


def _layout_plan_to_oa_write_plan_for_stage(layout_plan: object) -> object:
    from analogskills.eda.oa import layout_plan_to_oa_write_plan

    return layout_plan_to_oa_write_plan(layout_plan)


def _bbox_overlaps_or_contains(
    region_bbox: tuple[float, float, float, float],
    bbox: object,
) -> bool:
    candidate = _coerce_bbox(bbox)
    if candidate is None:
        return False
    return not (
        candidate[2] <= region_bbox[0]
        or candidate[0] >= region_bbox[2]
        or candidate[3] <= region_bbox[1]
        or candidate[1] >= region_bbox[3]
    )


def _clip_bbox_to_region(
    region_bbox: tuple[float, float, float, float],
    bbox: object,
) -> tuple[float, float, float, float] | None:
    candidate = _coerce_bbox(bbox)
    if candidate is None:
        return None
    x0 = max(region_bbox[0], candidate[0])
    y0 = max(region_bbox[1], candidate[1])
    x1 = min(region_bbox[2], candidate[2])
    y1 = min(region_bbox[3], candidate[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _point_in_bbox_for_stage_clip(
    region_bbox: tuple[float, float, float, float],
    point: object,
) -> bool:
    if not isinstance(point, (tuple, list)) or len(point) != 2:
        return False
    x = float(point[0])
    y = float(point[1])
    return region_bbox[0] <= x <= region_bbox[2] and region_bbox[1] <= y <= region_bbox[3]


def _path_bboxes_for_stage_clip(path: object) -> tuple[tuple[float, float, float, float], ...]:
    points = tuple(getattr(path, "points", ()) or ())
    width = float(getattr(path, "width", 0.0) or 0.0)
    if len(points) < 2:
        return ()
    half = width / 2.0
    boxes: list[tuple[float, float, float, float]] = []
    for start, end in zip(points, points[1:]):
        if len(start) != 2 or len(end) != 2:
            continue
        x0 = float(start[0])
        y0 = float(start[1])
        x1 = float(end[0])
        y1 = float(end[1])
        boxes.append((min(x0, x1) - half, min(y0, y1) - half, max(x0, x1) + half, max(y0, y1) + half))
    return tuple(boxes)


def _clip_layout_path_to_region(
    path: object,
    region_bbox: tuple[float, float, float, float],
) -> object | None:
    clipped_points = _clip_path_points_to_region(
        getattr(path, "points", ()) or (),
        width=float(getattr(path, "width", 0.0) or 0.0),
        region_bbox=region_bbox,
    )
    if clipped_points is None:
        return None
    return replace(path, points=clipped_points)


def _clip_oa_path_to_region(
    path: object,
    region_bbox: tuple[float, float, float, float],
) -> object | None:
    clipped_points = _clip_path_points_to_region(
        getattr(path, "points", ()) or (),
        width=float(getattr(path, "width", 0.0) or 0.0),
        region_bbox=region_bbox,
    )
    if clipped_points is None:
        return None
    return replace(path, points=clipped_points)


def _clip_path_points_to_region(
    points: object,
    *,
    width: float,
    region_bbox: tuple[float, float, float, float],
) -> tuple[tuple[float, float], ...] | None:
    sequence = tuple(points or ())
    if len(sequence) < 2:
        return None
    if len(sequence) > 2:
        return _clip_manhattan_path_points_to_region(sequence, width=width, region_bbox=region_bbox)
    start = sequence[0]
    end = sequence[1]
    if len(start) != 2 or len(end) != 2:
        return None
    x0 = float(start[0])
    y0 = float(start[1])
    x1 = float(end[0])
    y1 = float(end[1])
    half = width / 2.0
    if abs(y0 - y1) <= 1e-12:
        min_y = region_bbox[1] + half
        max_y = region_bbox[3] - half
        if max_y < min_y:
            return None
        clipped_y = min(max(y0, min_y), max_y)
        low = max(min(x0, x1), region_bbox[0] + half)
        high = min(max(x0, x1), region_bbox[2] - half)
        if high <= low:
            return None
        if x0 <= x1:
            return ((low, clipped_y), (high, clipped_y))
        return ((high, clipped_y), (low, clipped_y))
    if abs(x0 - x1) <= 1e-12:
        min_x = region_bbox[0] + half
        max_x = region_bbox[2] - half
        if max_x < min_x:
            return None
        clipped_x = min(max(x0, min_x), max_x)
        low = max(min(y0, y1), region_bbox[1] + half)
        high = min(max(y0, y1), region_bbox[3] - half)
        if high <= low:
            return None
        if y0 <= y1:
            return ((clipped_x, low), (clipped_x, high))
        return ((clipped_x, high), (clipped_x, low))
    return sequence if any(_bbox_overlaps_or_contains(region_bbox, bbox) for bbox in _path_bboxes_from_points(sequence, width)) else None


def _clip_manhattan_path_points_to_region(
    points: tuple[tuple[float, float], ...],
    *,
    width: float,
    region_bbox: tuple[float, float, float, float],
) -> tuple[tuple[float, float], ...] | None:
    kept: list[tuple[float, float]] = []
    for start, end in zip(points, points[1:]):
        clipped = _clip_path_points_to_region((start, end), width=width, region_bbox=region_bbox)
        if clipped is None:
            continue
        if not kept:
            kept.extend(clipped)
            continue
        if kept[-1] == clipped[0]:
            kept.append(clipped[1])
        else:
            kept.extend(clipped)
    normalized: list[tuple[float, float]] = []
    for point in kept:
        if normalized and normalized[-1] == point:
            continue
        normalized.append(point)
    return tuple(normalized) if len(normalized) >= 2 else None


def _path_bboxes_from_points(
    points: tuple[tuple[float, float], ...],
    width: float,
) -> tuple[tuple[float, float, float, float], ...]:
    if len(points) < 2:
        return ()
    half = width / 2.0
    boxes: list[tuple[float, float, float, float]] = []
    for start, end in zip(points, points[1:]):
        if len(start) != 2 or len(end) != 2:
            continue
        x0 = float(start[0])
        y0 = float(start[1])
        x1 = float(end[0])
        y1 = float(end[1])
        boxes.append((min(x0, x1) - half, min(y0, y1) - half, max(x0, x1) + half, max(y0, y1) + half))
    return tuple(boxes)


def _synthesized_stage_proposal_is_empty(proposal: object) -> bool:
    layout_patch = getattr(proposal, "layout_patch", None)
    if layout_patch is not None:
        if any(getattr(layout_patch, attr, ()) for attr in ("rects", "paths", "vias", "pins", "labels", "instances")):
            return False
    selected = getattr(proposal, "selected_candidate", None)
    if selected is None:
        return True
    selected_plan = getattr(selected, "plan", None)
    if selected_plan is None:
        return True
    for attr in ("layout_patch", "replacement_layout"):
        candidate_layout = getattr(selected_plan, attr, None)
        if candidate_layout is None:
            continue
        if any(getattr(candidate_layout, field, ()) for field in ("rects", "paths", "vias", "pins", "labels", "instances")):
            return False
    return True


def _validate_required_stage_scope_overlap(
    proposal: object,
    artifact: Mapping[str, object],
) -> str:
    required_nets = {str(net) for net in artifact.get("scope_nets", ()) if str(net)}
    required_nets.update(str(net) for net in artifact.get("restore_bus_nets", ()) if str(net))
    required_nets.update(str(net) for net in artifact.get("restore_feedback_nets", ()) if str(net))
    proposal_kind = str(getattr(proposal, "kind", ""))
    if not required_nets or proposal_kind not in {"pex_layout_eco", "post_layout_pex_route_eco"}:
        return ""
    proposal_nets = _repair_proposal_nets(proposal)
    if not proposal_nets:
        return f"missing_required_stage_scope_nets: {', '.join(sorted(required_nets))}"
    if required_nets.isdisjoint(proposal_nets):
        return (
            f"missing_required_stage_scope_nets: {', '.join(sorted(required_nets))}; "
            f"proposal_nets={', '.join(sorted(proposal_nets))}"
        )
    return ""


def _validate_synthesized_stage_connectivity(proposal: object) -> str:
    layout_patch = getattr(proposal, "layout_patch", None)
    if layout_patch is None:
        return ""
    try:
        from analogskills.layout.physical import detect_plan_net_opens
    except ImportError:
        return ""
    open_issues = tuple(detect_plan_net_opens(layout_patch))
    if not open_issues:
        return ""
    synthesized_nets = _repair_proposal_nets(proposal)
    blocking = tuple(issue for issue in open_issues if issue.net in synthesized_nets or not synthesized_nets)
    if not blocking:
        return ""
    names = ", ".join(f"{issue.net}:{issue.component_count}" for issue in blocking)
    return f"synthesized_stage_connectivity_open: {names}"


def _stage_dispatch_plan(
    plan: VerificationRepairExecutionPlan,
    stage: Mapping[str, object],
    target_cellview: Mapping[str, object],
) -> dict[str, object]:
    dispatch_plan = dict(plan.dispatch_plan)
    source_cellview = dict(dispatch_plan.get("source_cellview", {}) or {})
    stage_mode = str(stage.get("dispatch_mode", plan.dispatch_mode))
    hierarchy_resolution = dict(dispatch_plan.get("hierarchy_resolution", {}) or {})
    hierarchy_context = _stage_hierarchy_context(
        stage,
        target_cellview=target_cellview,
        hierarchy_resolution=hierarchy_resolution,
    )
    return {
        **dispatch_plan,
        "dispatch_mode": stage_mode,
        "target_cellview": dict(target_cellview),
        "writeback_level": str(stage.get("scope_level", plan.writeback_level)),
        "writeback_target": str(target_cellview.get("cell", stage.get("cell", ""))),
        "source_cellview": source_cellview,
        "hierarchy_context": hierarchy_context,
        "stage_metadata": {
            "order": int(stage.get("order", 0) or 0),
            "role": str(stage.get("role", "")),
            "cell": str(stage.get("cell", "")),
            "action": str(stage.get("action", "")),
        },
    }


def _stage_apply_unit(
    plan: VerificationRepairExecutionPlan,
    *,
    role: str,
    execution_kind: str,
    stage_dispatch_plan: Mapping[str, object],
    stage_summary: Mapping[str, object],
    stage_execution_profile: Mapping[str, object],
) -> dict[str, object]:
    target_cellview = dict(stage_dispatch_plan.get("target_cellview", {}) or {})
    apply_ready = bool(stage_summary) and (
        role == "target" or execution_kind == "stage_preparation"
    )
    proposal_kind = str(stage_summary.get("kind", ""))
    backend_applicable = _stage_backend_applicable(
        plan,
        role=role,
        execution_kind=execution_kind,
        stage_dispatch_plan=stage_dispatch_plan,
        stage_summary=stage_summary,
    )
    synthesis_artifact = _stage_synthesis_artifact(
        plan,
        role=role,
        execution_kind=execution_kind,
        stage_dispatch_plan=stage_dispatch_plan,
        proposal_summary=stage_summary,
        backend_applicable=backend_applicable,
    )
    proposal_origin = "retarget_only" if bool(stage_summary) else ""
    backend_block_reason = _stage_backend_block_reason(
        role=role,
        execution_kind=execution_kind,
        apply_ready=apply_ready,
        backend_applicable=backend_applicable,
    )
    return {
        "execution_kind": execution_kind,
        "apply_ready": apply_ready,
        "backend_applicable": backend_applicable,
        "has_stage_proposal": bool(stage_summary),
        "stage_proposal_kind": proposal_kind,
        "stage_proposal_origin": proposal_origin,
        "target_cellview": target_cellview,
        "stage_dispatch_plan": dict(stage_dispatch_plan),
        "execution_profile": dict(stage_execution_profile),
        "retargeted_summary": dict(stage_summary),
        "writeback_target": str(target_cellview.get("cell", "")),
        "editable": bool(role == "target" or plan.dispatch_mode == "manual_orchestrated_apply"),
        "requires_manual_handoff": execution_kind == "manual_handoff",
        "requires_stage_specific_synthesis": bool(stage_summary) and role == "intermediate" and not backend_applicable,
        "stage_synthesis_artifact": synthesis_artifact,
        "backend_block_reason": backend_block_reason,
    }


def _stage_apply_object(
    plan: VerificationRepairExecutionPlan,
    *,
    role: str,
    execution_kind: str,
    stage_dispatch_plan: Mapping[str, object],
    stage_execution_profile: Mapping[str, object],
) -> dict[str, object]:
    proposal = getattr(plan, "repair_proposal", None)
    stage_proposal = None
    if proposal is not None:
        stage_proposal = _retarget_repair_proposal_for_dispatch(proposal, stage_dispatch_plan)
    summary = _repair_proposal_dispatch_summary(stage_proposal)
    target_cellview = dict(stage_dispatch_plan.get("target_cellview", {}) or {})
    backend_applicable = _stage_backend_applicable(
        plan,
        role=role,
        execution_kind=execution_kind,
        stage_dispatch_plan=stage_dispatch_plan,
        stage_summary=summary,
    )
    apply_ready = bool(summary) and execution_kind != "manual_handoff"
    synthesis_artifact = _stage_synthesis_artifact(
        plan,
        role=role,
        execution_kind=execution_kind,
        stage_dispatch_plan=stage_dispatch_plan,
        proposal_summary=summary,
        backend_applicable=backend_applicable,
    )
    proposal_origin = "retarget_only" if bool(summary) else ""
    backend_block_reason = _stage_backend_block_reason(
        role=role,
        execution_kind=execution_kind,
        apply_ready=apply_ready,
        backend_applicable=backend_applicable,
    )
    stage_metadata = dict(stage_dispatch_plan.get("stage_metadata", {}) or {})
    stage_order = int(stage_metadata.get("order", 0) or 0)
    return {
        "role": role,
        "execution_kind": execution_kind,
        "apply_ready": apply_ready,
        "backend_applicable": backend_applicable,
        "target_cellview": target_cellview,
        "hierarchy_context": dict(stage_dispatch_plan.get("hierarchy_context", {}) or {}),
        "stage_dependencies": tuple(range(1, stage_order)) if stage_order > 1 else (),
        "required_enclosing_reruns": _stage_required_enclosing_reruns(
            plan,
            role=role,
            stage_dispatch_plan=stage_dispatch_plan,
        ),
        "editable": bool(role == "target" or plan.dispatch_mode == "manual_orchestrated_apply"),
        "stage_dispatch_plan": dict(stage_dispatch_plan),
        "execution_profile": dict(stage_execution_profile),
        "stage_proposal": stage_proposal,
        "stage_proposal_summary": summary,
        "stage_proposal_kind": str(summary.get("kind", "")),
        "stage_proposal_origin": proposal_origin,
        "requires_stage_specific_synthesis": bool(summary) and role == "intermediate" and not backend_applicable,
        "stage_synthesis_artifact": synthesis_artifact,
        "backend_block_reason": backend_block_reason,
    }


def _stage_required_enclosing_reruns(
    plan: VerificationRepairExecutionPlan,
    *,
    role: str,
    stage_dispatch_plan: Mapping[str, object],
) -> tuple[str, ...]:
    rerun_levels = tuple(str(level) for level in tuple(getattr(plan, "rerun_levels", ()) or ()) if str(level))
    if not rerun_levels:
        return ()
    if role == "target":
        return rerun_levels
    if role == "intermediate":
        return rerun_levels[:-1] if len(rerun_levels) > 1 else rerun_levels
    return rerun_levels[:-1] if len(rerun_levels) > 1 else ()


def _stage_hierarchy_context(
    stage: Mapping[str, object],
    *,
    target_cellview: Mapping[str, object],
    hierarchy_resolution: Mapping[str, object],
) -> dict[str, object]:
    stage_cell = str(target_cellview.get("cell", stage.get("cell", "")))
    stage_order = int(stage.get("order", 0) or 0)
    for depth, node in enumerate(tuple(hierarchy_resolution.get("path_nodes", ()) or ())):
        if not isinstance(node, Mapping):
            continue
        node_name = str(node.get("name", ""))
        node_cell = str(node.get("cell", ""))
        if stage_cell not in {node_name, node_cell}:
            continue
        return {
            "order": stage_order,
            "depth": depth,
            "node_name": node_name,
            "cell": node_cell or stage_cell,
            "lib": str(node.get("lib", "")),
            "view": str(node.get("view", "")),
            "view_type": str(node.get("view_type", "")),
            "parent_node": str(node.get("parent", "")),
            "aliases": tuple(str(alias) for alias in tuple(node.get("aliases", ()) or ()) if str(alias)),
        }
    return {
        "order": stage_order,
        "depth": max(stage_order - 1, 0),
        "node_name": str(stage.get("cell", "")),
        "cell": stage_cell,
        "lib": str(target_cellview.get("lib", "")),
        "view": str(target_cellview.get("view", "")),
        "view_type": str(target_cellview.get("view_type", "")),
        "parent_node": "",
        "aliases": (),
    }


def _execute_stage_apply_objects(
    stage_apply_objects: tuple[dict[str, object], ...],
) -> dict[str, object]:
    executed: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for item in stage_apply_objects:
        role = str(item.get("role", ""))
        execution_kind = str(item.get("execution_kind", ""))
        target_cellview = dict(item.get("target_cellview", {}) or {})
        if bool(item.get("backend_applicable", False)):
            executed.append(
                {
                    "role": role,
                    "execution_kind": execution_kind,
                    "target_cellview": target_cellview,
                    "status": "executed_by_primary_apply",
                }
            )
        else:
            block_reason = str(item.get("backend_block_reason", ""))
            skipped.append(
                {
                    "role": role,
                    "execution_kind": execution_kind,
                    "target_cellview": target_cellview,
                    "status": "skipped",
                    "reason": block_reason or "stage_not_apply_ready",
                }
            )
    return {
        "executed": tuple(executed),
        "skipped": tuple(skipped),
        "executed_count": len(executed),
        "skipped_count": len(skipped),
    }


def _dispatch_verification_repair_stage_sequence(
    plan: VerificationRepairExecutionPlan,
    stage_apply_objects: tuple[dict[str, object], ...],
    backend: object,
) -> object:
    applied_backend = backend
    applied_any = False
    for item in stage_apply_objects:
        if not bool(item.get("backend_applicable", False)):
            continue
        proposal = item.get("stage_proposal")
        if proposal is None:
            continue
        if str(item.get("role", "")) == "target":
            applied_backend = _dispatch_verification_repair_apply(plan, proposal, applied_backend)
        else:
            applied_backend = _dispatch_stage_apply_object(plan, item, applied_backend)
        applied_any = True
    if applied_any:
        return applied_backend
    executable_proposal = _retarget_repair_proposal_for_dispatch(plan.repair_proposal, plan.dispatch_plan)
    return _dispatch_verification_repair_apply(plan, executable_proposal, backend)


def _stage_backend_applicable(
    plan: VerificationRepairExecutionPlan,
    *,
    role: str,
    execution_kind: str,
    stage_dispatch_plan: Mapping[str, object],
    stage_summary: Mapping[str, object],
) -> bool:
    if not stage_summary or execution_kind == "manual_handoff":
        return False
    if role == "target" and execution_kind == "target_writeback":
        return True
    if execution_kind != "stage_preparation":
        return False
    source_cell = str(dict(plan.dispatch_plan.get("source_cellview", {}) or {}).get("cell", ""))
    final_target = str(dict(plan.dispatch_plan.get("target_cellview", {}) or {}).get("cell", ""))
    stage_target = str(dict(stage_dispatch_plan.get("target_cellview", {}) or {}).get("cell", ""))
    writeback_level = str(plan.writeback_level or "")
    return bool(stage_target) and stage_target == source_cell and stage_target != final_target and writeback_level in {"parent", "top"}


def _stage_backend_block_reason(
    *,
    role: str,
    execution_kind: str,
    apply_ready: bool,
    backend_applicable: bool,
) -> str:
    if backend_applicable:
        return ""
    if execution_kind == "manual_handoff":
        return "manual_handoff_required"
    if not apply_ready:
        return "stage_not_apply_ready"
    if role == "intermediate":
        return "requires_stage_specific_synthesis"
    return "stage_not_backend_applicable"


def _stage_synthesis_artifact(
    plan: VerificationRepairExecutionPlan,
    *,
    role: str,
    execution_kind: str,
    stage_dispatch_plan: Mapping[str, object],
    proposal_summary: Mapping[str, object],
    backend_applicable: bool,
) -> dict[str, object]:
    if not proposal_summary or execution_kind == "manual_handoff":
        return {}
    target_cellview = dict(stage_dispatch_plan.get("target_cellview", {}) or {})
    scope_guard = dict(stage_dispatch_plan.get("scope_guard", {}) or {})
    artifact = {
        "role": role,
        "execution_kind": execution_kind,
        "source_proposal_kind": str(proposal_summary.get("kind", "")),
        "target_cellview": target_cellview,
        "writeback_level": str(stage_dispatch_plan.get("writeback_level", plan.writeback_level or "")),
        "writeback_target": str(stage_dispatch_plan.get("writeback_target", plan.writeback_target or "")),
        "scope_nets": tuple(str(net) for net in scope_guard.get("scope_nets", ()) if str(net)),
        "scope_devices": tuple(str(device) for device in scope_guard.get("scope_devices", ()) if str(device)),
        "scope_regions": tuple(str(region) for region in scope_guard.get("scope_regions", ()) if str(region)),
        "restore_bus_nets": tuple(str(net) for net in scope_guard.get("restore_bus_nets", ()) if str(net)),
        "restore_feedback_nets": tuple(str(net) for net in scope_guard.get("restore_feedback_nets", ()) if str(net)),
        "protected_reference_nets": tuple(str(net) for net in scope_guard.get("protected_reference_nets", ()) if str(net)),
        "architecture_protected_nets": tuple(str(net) for net in scope_guard.get("architecture_protected_nets", ()) if str(net)),
        "binding_blocked_partitions": tuple(
            str(item) for item in scope_guard.get("binding_blocked_partitions", ()) if str(item)
        ),
        "macro_bound_partitions": tuple(
            str(item) for item in scope_guard.get("macro_bound_partitions", ()) if str(item)
        ),
        "architecture_budget_blocked_partitions": tuple(
            str(item) for item in scope_guard.get("architecture_budget_blocked_partitions", ()) if str(item)
        ),
        "region_bbox": _coerce_bbox(scope_guard.get("region_bbox")),
        "issue_bbox": _coerce_bbox(scope_guard.get("issue_bbox")),
        "scope_policy": str(scope_guard.get("scope_policy", "")),
        "retargeted_only": True,
        "backend_applicable": backend_applicable,
    }
    if role == "intermediate" and not backend_applicable:
        artifact["synthesis_goal"] = "derive_stage_specific_intermediate_eco"
        artifact["blocking_reason"] = "requires_stage_specific_synthesis"
    elif execution_kind == "manual_handoff":
        artifact["synthesis_goal"] = "handoff_manual_orchestrated_eco"
    else:
        artifact["synthesis_goal"] = "direct_stage_apply"
    return artifact


def _dispatch_stage_apply_object(
    plan: VerificationRepairExecutionPlan,
    stage_apply_object: Mapping[str, object],
    backend: object,
) -> object:
    from analogskills.repair import apply_repair_proposal

    proposal = stage_apply_object.get("stage_proposal")
    if proposal is None:
        return backend
    target_cellview = dict(stage_apply_object.get("target_cellview", {}) or {})
    if hasattr(backend, "operations"):
        getattr(backend, "operations").append(
            (
                "dispatch_repair_stage",
                (),
                {
                    "dispatch_mode": plan.dispatch_mode,
                    "writeback_level": plan.writeback_level,
                    "writeback_target": plan.writeback_target,
                    "stage_role": str(stage_apply_object.get("role", "")),
                    "stage_execution_kind": str(stage_apply_object.get("execution_kind", "")),
                    "target_cellview": target_cellview,
                },
            )
        )
    return apply_repair_proposal(proposal, backend)


def parse_measurements(text_or_path: str | Path) -> dict[str, float]:
    text = _read_text(text_or_path)
    metrics: dict[str, float] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(rf"^([A-Za-z_]\w*)\s*(?:=|:)\s*({_FLOAT_RE})", stripped)
        if match:
            metrics[match.group(1)] = float(match.group(2))
    return metrics


def parse_drc_report(text_or_path: str | Path) -> tuple[DrcIssue, ...]:
    text = _read_text(text_or_path)
    detailed = parse_calibre_drc_results(text)
    if detailed:
        return tuple(_drc_issue_from_calibre_result(result) for result in detailed)
    if "RULECHECK" in text and "TOTAL Result Count" in text:
        return parse_calibre_drc_summary(text)
    issues: list[DrcIssue] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(rf"^(?P<rule>[\w.:-]+)\s+(?P<layer>\w+)\s*:\s*(?P<msg>.*?)(?:\s*@\s*\((?P<bbox>[^)]*)\))?$", stripped)
        if not match:
            continue
        bbox = _parse_bbox(match.group("bbox"))
        issues.append(DrcIssue(match.group("rule"), match.group("layer"), match.group("msg"), bbox))
    return tuple(issues)


def parse_calibre_drc_results(text_or_path: str | Path) -> tuple[CalibreDrcResult, ...]:
    """Parse detailed Calibre/RVE-like DRC result text into geometry records."""

    text = _read_text(text_or_path)
    ascii_db = parse_calibre_ascii_drc_db(text)
    if ascii_db:
        return ascii_db
    results: list[CalibreDrcResult] = []
    current_rule = ""
    current_message = ""
    current: dict[str, object] | None = None
    properties: dict[str, str] = {}

    def flush() -> None:
        nonlocal current, properties
        if current is None:
            return
        rule = str(current.get("rule") or current_rule)
        if not rule:
            current = None
            properties = {}
            return
        result_props = {key: value for key, value in properties.items() if key not in {"rule", "layer", "message", "cell", "instance"}}
        results.append(
            CalibreDrcResult(
                rule=rule,
                layer=str(current.get("layer") or _rule_layer(rule)),
                message=str(current.get("message") or current_message or f"Calibre DRC result for {rule}"),
                result_index=current.get("result_index") if isinstance(current.get("result_index"), int) else None,
                cell=str(current.get("cell") or ""),
                instance=str(current.get("instance") or ""),
                bbox=current.get("bbox") if _is_bbox(current.get("bbox")) else None,
                polygon=current.get("polygon") if isinstance(current.get("polygon"), tuple) else (),
                properties=result_props,
            )
        )
        current = None
        properties = {}

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rule_match = re.match(r"^(?:RULECHECK|RULE)\s+(?P<rule>[\w.:-]+)(?:\s*[:=]\s*(?P<msg>.*?))?\s*$", stripped, flags=re.IGNORECASE)
        if rule_match and "TOTAL Result Count" not in stripped:
            flush()
            current_rule = rule_match.group("rule").strip()
            current_message = (rule_match.group("msg") or "").strip()
            continue
        count_match = re.match(r"^\s*RULECHECK\s+(?P<rule>.+?)\s+\.{2,}\s+TOTAL\s+Result\s+Count\s*=", stripped, flags=re.IGNORECASE)
        if count_match:
            flush()
            current_rule = count_match.group("rule").strip()
            current_message = ""
            continue
        result_match = re.match(r"^(?:RESULT|Result|DRC\s+Result)\s*#?\s*(?P<idx>\d+)?(?:\s+(?P<rule>[\w.:-]+))?(?:\s*[:=]\s*(?P<msg>.*))?$", stripped)
        if result_match:
            flush()
            current = {
                "rule": (result_match.group("rule") or current_rule).strip(),
                "message": (result_match.group("msg") or current_message).strip(),
                "result_index": int(result_match.group("idx")) if result_match.group("idx") else None,
            }
            properties = {}
            continue
        if current is None:
            inline = _inline_calibre_drc_result(stripped, current_rule=current_rule, current_message=current_message)
            if inline is not None:
                results.append(inline)
            continue
        inline = _inline_calibre_drc_result(stripped, current_rule=current_rule, current_message=current_message)
        if inline is not None and _looks_like_new_inline_calibre_result(stripped):
            flush()
            results.append(inline)
            continue
        key_value = re.match(r"^(?P<key>[A-Za-z][\w /.-]*)\s*(?:=|:)\s*(?P<value>.+)$", stripped)
        if not key_value:
            continue
        key = _normalise_calibre_key(key_value.group("key"))
        value = key_value.group("value").strip()
        properties[key] = value
        if key == "rule":
            current["rule"] = value
        elif key == "layer":
            current["layer"] = value
        elif key == "message":
            current["message"] = value
        elif key == "cell":
            current["cell"] = value
        elif key in {"instance", "inst", "path"}:
            current["instance"] = value
        elif key in {"bbox", "box", "extent", "rectangle"}:
            current["bbox"] = _parse_bbox(value)
        elif key in {"polygon", "poly", "points", "vertices"}:
            current["polygon"] = _parse_points(value)
            if current.get("bbox") is None and current["polygon"]:
                current["bbox"] = _points_bbox(current["polygon"])  # type: ignore[arg-type]
    flush()
    return tuple(results)


def parse_calibre_ascii_drc_db(text_or_path: str | Path) -> tuple[CalibreDrcResult, ...]:
    """Parse a Calibre ``DRC RESULTS DATABASE ... ASCII`` file.

    Calibre stores coordinates as integer database units.  The first line is
    ``<top-cell> <dbu-per-micron>``; each rule section is followed by a count
    line, an optional SVRF description block, and ``p``/``e`` geometry records.
    Geometry is converted to microns so it can be localized against LayoutIR.
    """

    text = _read_text(text_or_path)
    lines = text.splitlines()
    if not lines:
        return ()
    header = re.match(r"^\s*(?P<cell>\S+)\s+(?P<dbu>\d+)\s*$", lines[0])
    if not header or int(header.group("dbu")) <= 0:
        return ()
    cell = header.group("cell")
    dbu = int(header.group("dbu"))
    count_re = re.compile(r"^\s*(?P<total>\d+)\s+(?P<original>\d+)\s+(?P<kind>\d+)\s+.+$")
    geom_re = re.compile(r"^\s*(?P<kind>[pe])\s+(?P<index>\d+)\s+(?P<count>\d+)\s*$", re.IGNORECASE)
    coord_re = re.compile(r"^\s*[-+]?\d+(?:\s+[-+]?\d+){1,3}\s*$")
    results: list[CalibreDrcResult] = []
    index = 1
    while index + 1 < len(lines):
        rule = lines[index].strip()
        count_match = count_re.match(lines[index + 1])
        if not rule or not count_match:
            index += 1
            continue
        section_count = int(count_match.group("total"))
        index += 2
        description: list[str] = []
        brace_depth = 0
        while index < len(lines):
            stripped = lines[index].strip()
            if geom_re.match(stripped):
                break
            if index + 1 < len(lines) and count_re.match(lines[index + 1]) and brace_depth <= 0:
                break
            description.append(stripped)
            brace_depth += stripped.count("{") - stripped.count("}")
            index += 1
        parsed = 0
        while index < len(lines):
            geom = geom_re.match(lines[index].strip())
            if not geom:
                break
            primitive = geom.group("kind").lower()
            result_index = int(geom.group("index"))
            coordinate_row_count = int(geom.group("count"))
            index += 1
            coordinate_rows: list[tuple[int, ...]] = []
            for _ in range(coordinate_row_count):
                if index >= len(lines) or not coord_re.match(lines[index]):
                    break
                coordinate_rows.append(tuple(int(value) for value in lines[index].split()))
                index += 1
            if not coordinate_rows:
                continue
            points: list[tuple[float, float]] = []
            for row in coordinate_rows:
                points.append((row[0] / dbu, row[1] / dbu))
                if len(row) == 4:
                    points.append((row[2] / dbu, row[3] / dbu))
            bbox = _points_bbox(tuple(points))
            message = next((row[1:].strip() for row in description if row.startswith("@")), "")
            results.append(CalibreDrcResult(
                rule=rule,
                layer=_rule_layer(rule),
                message=message or f"Calibre ASCII DRC result for {rule}",
                result_index=result_index,
                cell=cell,
                bbox=bbox,
                polygon=tuple(points) if primitive == "p" else (),
                properties={"primitive": primitive, "dbu_per_micron": str(dbu)},
            ))
            parsed += 1
        # Empty checks are normally omitted, but accepting them makes the parser
        # tolerant of databases produced with KEEP EMPTY CHECKS enabled.
        if section_count and parsed == 0:
            return ()
    return tuple(results)


def parse_calibre_drc_summary(text_or_path: str | Path, *, include_zero: bool = False) -> tuple[DrcIssue, ...]:
    text = _read_text(text_or_path)
    issues: list[DrcIssue] = []
    for line in text.splitlines():
        match = re.match(r"^\s*RULECHECK\s+(?P<rule>.+?)\s+\.{2,}\s+TOTAL\s+Result\s+Count\s*=\s*(?P<count>\d+)", line)
        if not match:
            continue
        count = int(match.group("count"))
        if count == 0 and not include_zero:
            continue
        rule = match.group("rule").strip()
        layer = _rule_layer(rule)
        category = _rule_category(rule)
        message = f"Calibre rulecheck {rule} reported {count} result(s); category={category}; suggested_action={_suggested_drc_action(category)}"
        issues.append(DrcIssue(rule, layer, message, None))
    return tuple(issues)


def parse_lvs_report(text_or_path: str | Path) -> tuple[LvsIssue, ...]:
    text = _read_text(text_or_path)
    calibre_issues = _parse_calibre_lvs_report(text)
    if calibre_issues is not None:
        return calibre_issues
    return _parse_legacy_lvs_report(text)


def _parse_legacy_lvs_report(text: str) -> tuple[LvsIssue, ...]:
    issues: list[LvsIssue] = []
    current_section = ""
    current_net = ""
    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if not stripped or stripped.startswith("#"):
            continue
        if _is_lvs_success_line(upper):
            continue
        section = _lvs_section_name(upper)
        if section:
            current_section = section
        net_header = re.match(r"\d+\s+Net\s+([A-Za-z_][A-Za-z0-9_.$:-]*)", stripped)
        if net_header:
            current_net = net_header.group(1)
        if "OPEN" in upper:
            net = _extract_net(stripped)
            issues.append(LvsIssue("open", stripped, net))
        elif "SHORT" in upper:
            net = _extract_net(stripped)
            issues.append(LvsIssue("short", stripped, net))
        elif "MISSING CONNECTION" in upper:
            issues.append(LvsIssue("open", stripped, current_net or _extract_net(stripped)))
        elif "MISMATCH" in upper or "PROPERTY" in upper and "DIFFER" in upper:
            issues.append(LvsIssue("mismatch", stripped, _extract_net(stripped)))
        elif "NOT COMPARED" in upper or "NOTCOMPARED" in upper:
            issues.append(LvsIssue("not_compared", stripped, _extract_net(stripped)))
        elif "INCORRECT" in upper:
            issues.append(LvsIssue("incorrect", stripped, _extract_net(stripped)))
        elif "WARNING" in upper and ("SOFT" in upper or "CONNECT" in upper or "SOURCE" in upper or "LAYOUT" in upper):
            issues.append(LvsIssue("warning", stripped, _extract_net(stripped)))
        elif current_section and _looks_like_lvs_difference_detail(stripped):
            issues.append(LvsIssue(current_section, stripped, _extract_net(stripped)))
    return tuple(issues)


def _parse_calibre_lvs_report(text: str) -> tuple[LvsIssue, ...] | None:
    upper_text = text.upper()
    if "OVERALL COMPARISON RESULTS" not in upper_text:
        return None

    lines = text.splitlines()
    main_end = next((idx for idx, line in enumerate(lines) if "LVS PARAMETERS" in line.upper()), len(lines))
    main_lines = lines[:main_end]

    issues: list[LvsIssue] = []
    issues.extend(_parse_calibre_lvs_extraction_warnings(main_lines))
    issues.extend(_parse_calibre_lvs_overall_errors(main_lines))
    issues.extend(_parse_calibre_lvs_count_mismatches(main_lines))
    issues.extend(_parse_calibre_lvs_incorrect_net_sections(main_lines))
    issues.extend(_parse_calibre_lvs_incorrect_instance_sections(main_lines))
    issues.extend(_parse_calibre_lvs_property_error_sections(main_lines))
    return _dedupe_lvs_issues(issues)


def parse_pex_report(text_or_path: str | Path) -> PexReport:
    text = _read_text(text_or_path)
    netlist = ""
    count = 0
    net_cap: dict[str, float] = {}
    net_res: dict[str, float] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("EXTRACTED_NETLIST"):
            netlist = stripped.split("=", 1)[1].strip()
        elif stripped.lower().endswith((".spf", ".spef", ".pex.netlist")):
            netlist = stripped
        match = re.search(r"PARASITICS\s*[:=]\s*(\d+)", stripped, flags=re.IGNORECASE)
        if match:
            count = int(match.group(1))
        cap = _parse_pex_net_value(stripped, ("NETCAP", "CAP", "C"), ("F", "fF", "pF"))
        if cap is not None:
            net, value = cap
            net_cap[net] = net_cap.get(net, 0.0) + value
        res = _parse_pex_net_value(stripped, ("NETRES", "RES", "R"), ("OHM", "ohm"))
        if res is not None:
            net, value = res
            net_res[net] = net_res.get(net, 0.0) + value
    return PexReport(netlist, count, net_cap, net_res)


def _read_text(text_or_path: str | Path) -> str:
    if isinstance(text_or_path, Path):
        return text_or_path.read_text(encoding="utf-8")
    candidate = Path(text_or_path)
    if "\n" not in str(text_or_path) and candidate.exists():
        return candidate.read_text(encoding="utf-8")
    return str(text_or_path)


def _assess_metric(name: str, value: float, minimum: float | None, maximum: float | None) -> MetricAssessment:
    passed = True
    margins = []
    if minimum is not None:
        margins.append(value - minimum)
        passed = passed and value >= minimum
    if maximum is not None:
        margins.append(maximum - value)
        passed = passed and value <= maximum
    margin = min(margins) if margins else None
    return MetricAssessment(name, value, minimum, maximum, passed, margin)


def _default_metric_eco_action(assessment: MetricAssessment) -> str:
    name = assessment.name.lower()
    high_is_bad = assessment.maximum is not None and assessment.value > assessment.maximum
    low_is_bad = assessment.minimum is not None and assessment.value < assessment.minimum
    if any(token in name for token in ("phase", "pm")) and low_is_bad:
        return "review_compensation_and_parasitic_loading"
    if any(token in name for token in ("gain", "ugb", "gbw", "bandwidth", "slew")) and low_is_bad:
        return "review_sizing_bias_or_finger_candidates"
    if any(token in name for token in ("delay", "settling", "rise", "fall")) and high_is_bad:
        return "review_drive_strength_and_routing_parasitics"
    if any(token in name for token in ("power", "current", "idd")) and high_is_bad:
        return "reduce_bias_or_device_size"
    if any(token in name for token in ("noise", "offset", "kickback", "mismatch")) and high_is_bad:
        return "review_matching_finger_and_routing_symmetry"
    if any(token in name for token in ("swing", "headroom")) and low_is_bad:
        return "review_bias_headroom"
    return "review_sizing_or_layout"


def _metric_failure_reason(assessment: MetricAssessment) -> str:
    if assessment.minimum is not None and assessment.value < assessment.minimum:
        return f"{assessment.name}={assessment.value:g} below minimum {assessment.minimum:g}"
    if assessment.maximum is not None and assessment.value > assessment.maximum:
        return f"{assessment.name}={assessment.value:g} above maximum {assessment.maximum:g}"
    return f"{assessment.name}={assessment.value:g} outside target"


def _metric_priority(assessment: MetricAssessment) -> int:
    if assessment.minimum is not None and assessment.value < assessment.minimum:
        scale = max(abs(assessment.minimum), 1e-12)
        severity = (assessment.minimum - assessment.value) / scale
    elif assessment.maximum is not None and assessment.value > assessment.maximum:
        scale = max(abs(assessment.maximum), 1e-12)
        severity = (assessment.value - assessment.maximum) / scale
    else:
        severity = 0.0
    return 60 + min(30, int(max(severity, 0.0) * 100))


def _metric_delta(name: str, before: float | None, after: float | None, objective: str | None, tol: float) -> MetricDelta:
    direction = _normalize_objective(objective) or _infer_metric_objective(name)
    delta = None if before is None or after is None else after - before
    improved = None
    if delta is not None and direction == "max":
        if delta > tol:
            improved = True
        elif delta < -tol:
            improved = False
    elif delta is not None and direction == "min":
        if delta < -tol:
            improved = True
        elif delta > tol:
            improved = False
    return MetricDelta(name, before, after, delta, direction if direction in {"min", "max"} else "unknown", improved)


def _normalize_objective(objective: str | None) -> str | None:
    if objective in {"min", "minimize", "lower"}:
        return "min"
    if objective in {"max", "maximize", "higher"}:
        return "max"
    return None


def _infer_metric_objective(name: str) -> str:
    lowered = name.lower()
    if any(token in lowered for token in ("gain", "ugb", "gbw", "bandwidth", "phase", "pm", "slew", "swing", "snr", "sndr", "enob")):
        return "max"
    if any(token in lowered for token in ("power", "current", "idd", "noise", "offset", "kickback", "delay", "settling", "jitter", "area")):
        return "min"
    return "unknown"


def _scorecard_comparison_summary(
    metric_deltas: tuple[MetricDelta, ...],
    drc_delta: int,
    lvs_delta: int,
    pex_delta: int,
    issue_delta: int,
    before_passed: bool,
    after_passed: bool,
) -> tuple[str, ...]:
    summary: list[str] = []
    if before_passed != after_passed:
        summary.append("scorecard pass state changed to passed" if after_passed else "scorecard pass state changed to failed")
    for label, value in (("DRC", drc_delta), ("LVS", lvs_delta), ("PEX parasitic", pex_delta), ("issue", issue_delta)):
        if value:
            summary.append(f"{label} count delta {value:+d}")
    for delta in metric_deltas:
        if delta.improved is None or delta.delta is None:
            continue
        direction = "improved" if delta.improved else "regressed"
        summary.append(f"metric {delta.name} {direction} by {delta.delta:g}")
    return tuple(summary)


def _foundry_execution_summary_lines(
    ready: bool,
    ready_stages: tuple[str, ...],
    blocked_stages: tuple[str, ...],
    missing_inputs: tuple[str, ...],
    missing_files: tuple[str, ...],
    issues: tuple[str, ...],
) -> tuple[str, ...]:
    summary = [f"foundry_ready={ready}"]
    if ready_stages:
        summary.append(f"ready_stages={','.join(ready_stages)}")
    if blocked_stages:
        summary.append(f"blocked_stages={','.join(blocked_stages)}")
    if missing_inputs:
        summary.append(f"missing_inputs={','.join(missing_inputs)}")
    if missing_files:
        summary.append(f"missing_files={','.join(missing_files)}")
    if issues:
        summary.append(f"foundry_issue_count={len(issues)}")
    return tuple(summary)


def _foundry_execution_comparison_summary(
    before_ready: bool,
    after_ready: bool,
    newly_ready_stages: tuple[str, ...],
    newly_blocked_stages: tuple[str, ...],
    resolved_missing_inputs: tuple[str, ...],
    added_missing_inputs: tuple[str, ...],
    resolved_missing_files: tuple[str, ...],
    added_missing_files: tuple[str, ...],
    issue_delta: int,
) -> tuple[str, ...]:
    summary: list[str] = []
    if before_ready != after_ready:
        summary.append("foundry readiness changed to ready" if after_ready else "foundry readiness changed to blocked")
    if newly_ready_stages:
        summary.append(f"newly ready stage(s): {', '.join(newly_ready_stages)}")
    if newly_blocked_stages:
        summary.append(f"newly blocked stage(s): {', '.join(newly_blocked_stages)}")
    if resolved_missing_inputs:
        summary.append(f"resolved missing input(s): {', '.join(resolved_missing_inputs)}")
    if added_missing_inputs:
        summary.append(f"added missing input(s): {', '.join(added_missing_inputs)}")
    if resolved_missing_files:
        summary.append(f"resolved missing file(s): {', '.join(resolved_missing_files)}")
    if added_missing_files:
        summary.append(f"added missing file(s): {', '.join(added_missing_files)}")
    if issue_delta:
        summary.append(f"foundry issue delta {issue_delta:+d}")
    return tuple(summary)


def _scorecard_improvement_count(comparison: PostLayoutScorecardComparison) -> int:
    count = 0
    count += int(comparison.drc_delta < 0)
    count += int(comparison.lvs_delta < 0)
    count += int(comparison.issue_delta < 0)
    count += sum(1 for delta in comparison.metric_deltas if delta.improved is True)
    return count


def _run_metric_extreme(records: tuple[PostLayoutRunRecord, ...], name: str, objective: str | None, *, worst: bool) -> float:
    values = [record.scorecard.metrics[name] for record in records if name in record.scorecard.metrics]
    if not values:
        return 0.0
    direction = _normalize_objective(objective) or _infer_metric_objective(name)
    if direction == "max":
        return min(values) if worst else max(values)
    if direction == "min":
        return max(values) if worst else min(values)
    return min(values) if worst else max(values)


def _post_layout_run_summary_lines(
    records: tuple[PostLayoutRunRecord, ...],
    failing: tuple[PostLayoutRunRecord, ...],
    worst_metrics: Mapping[str, float],
) -> tuple[str, ...]:
    summary = [f"{len(records)} post-layout run(s), {len(failing)} failing"]
    if failing:
        summary.append(f"failing run(s): {', '.join(record.run_id for record in failing)}")
    for name, value in sorted(worst_metrics.items()):
        summary.append(f"worst {name}={value:g}")
    return tuple(summary)


def _post_layout_run_comparison_summary(
    new_failing: tuple[str, ...],
    recovered: tuple[str, ...],
    still_failing: tuple[str, ...],
    worst_metric_deltas: tuple[MetricDelta, ...],
    before_total: int,
    after_total: int,
) -> tuple[str, ...]:
    summary = []
    if before_total != after_total:
        summary.append(f"post-layout run count changed {before_total} -> {after_total}")
    if new_failing:
        summary.append(f"new failing run(s): {', '.join(new_failing)}")
    if recovered:
        summary.append(f"recovered run(s): {', '.join(recovered)}")
    if still_failing:
        summary.append(f"still failing run(s): {', '.join(still_failing)}")
    for delta in worst_metric_deltas:
        if delta.delta is None or delta.improved is None:
            continue
        direction = "improved" if delta.improved else "regressed"
        summary.append(f"worst metric {delta.name} {direction} by {delta.delta:g}")
    return tuple(summary)


def _post_layout_run_comparison_actions(
    new_failing: tuple[str, ...],
    still_failing: tuple[str, ...],
    worst_metric_deltas: tuple[MetricDelta, ...],
) -> tuple[str, ...]:
    actions = []
    if new_failing:
        actions.append("review_new_failing_post_layout_runs")
    if still_failing:
        actions.append("review_persistent_failing_post_layout_runs")
    if any(delta.improved is False for delta in worst_metric_deltas):
        actions.append("review_worst_metric_regressions")
    if new_failing or still_failing:
        actions.append("continue_post_layout_ecos")
    return tuple(dict.fromkeys(actions))


def _rank_calibre_suggestions(
    drc_suggestions: tuple[DrcEcoSuggestion, ...],
    lvs_suggestions: tuple[LvsEcoSuggestion, ...],
) -> tuple[tuple[str, DrcEcoSuggestion | LvsEcoSuggestion], ...]:
    ranked: list[tuple[str, DrcEcoSuggestion | LvsEcoSuggestion]] = [
        ("lvs", suggestion) for suggestion in lvs_suggestions
    ] + [("drc", suggestion) for suggestion in drc_suggestions]
    return tuple(sorted(ranked, key=lambda item: (-int(getattr(item[1], "priority", 0)), item[0], str(item[1].action), _calibre_target(item[1]))))


def _calibre_target(suggestion: DrcEcoSuggestion | LvsEcoSuggestion) -> str:
    if isinstance(suggestion, DrcEcoSuggestion):
        return suggestion.rule
    return suggestion.net


def _calibre_owner_counts(
    drc_suggestions: tuple[DrcEcoSuggestion, ...],
    lvs_suggestions: tuple[LvsEcoSuggestion, ...],
) -> dict[str, int]:
    owners: dict[str, int] = {}
    for suggestion in (*drc_suggestions, *lvs_suggestions):
        owner = str(getattr(suggestion, "owner", "") or "manual")
        owners[owner] = owners.get(owner, 0) + 1
    return dict(sorted(owners.items()))


def _calibre_blocking_issues(
    drc_suggestions: tuple[DrcEcoSuggestion, ...],
    lvs_suggestions: tuple[LvsEcoSuggestion, ...],
) -> tuple[str, ...]:
    issues: list[str] = []
    for suggestion in drc_suggestions:
        location = f" {suggestion.bbox}" if suggestion.bbox is not None else ""
        issues.append(f"DRC {suggestion.rule} owner={suggestion.owner} action={suggestion.action} layer={suggestion.layer}{location}")
    for suggestion in lvs_suggestions:
        net = suggestion.net or "<unknown>"
        peers = f" peers={','.join(suggestion.peer_nets)}" if suggestion.peer_nets else ""
        issues.append(f"LVS {net} owner={suggestion.owner} action={suggestion.action}{peers}")
    return tuple(issues)


def _calibre_closure_provenance(
    provenance: Mapping[str, object] | None,
    drc_issues: tuple[DrcIssue, ...],
    lvs_issues: tuple[LvsIssue, ...],
    drc_suggestions: tuple[DrcEcoSuggestion, ...],
    lvs_suggestions: tuple[LvsEcoSuggestion, ...],
) -> dict[str, object]:
    result = dict(provenance or {})
    result.update(
        {
            "sources": ("calibre_drc", "calibre_lvs"),
            "drc_issue_count": len(drc_issues),
            "lvs_issue_count": len(lvs_issues),
            "drc_suggestion_count": len(drc_suggestions),
            "lvs_suggestion_count": len(lvs_suggestions),
        }
    )
    return result


def _pex_hotspot_closure_blockers(
    comparison: PexHotspotComparison,
    block_on_critical_pex_regression: bool,
    block_on_any_pex_regression: bool,
) -> list[str]:
    blockers = []
    critical_regressions = tuple(delta.net for delta in comparison.deltas if delta.critical and delta.issues)
    if block_on_critical_pex_regression and critical_regressions:
        blockers.append(f"critical PEX hotspot regression: {', '.join(critical_regressions)}")
    elif block_on_any_pex_regression and comparison.worsened_nets:
        blockers.append(f"PEX hotspot regression: {', '.join(comparison.worsened_nets)}")
    return blockers


def _run_summary_closure_blockers(comparison: PostLayoutRunSummaryComparison | None) -> tuple[str, ...]:
    if comparison is None:
        return ()
    blockers = []
    if comparison.new_failing_run_ids:
        blockers.append(f"new failing post-layout run(s): {', '.join(comparison.new_failing_run_ids)}")
    if comparison.still_failing_run_ids:
        blockers.append(f"still failing post-layout run(s): {', '.join(comparison.still_failing_run_ids)}")
    regressed = tuple(delta.name for delta in comparison.worst_metric_deltas if delta.improved is False)
    if regressed:
        blockers.append(f"worst post-layout metric regression: {', '.join(regressed)}")
    return tuple(blockers)


def _drc_eco_closure_blockers(comparison: DrcEcoComparison | None) -> tuple[str, ...]:
    if comparison is None or comparison.passed:
        return ()
    blockers = []
    if comparison.new_rules:
        blockers.append(f"new DRC rule(s): {', '.join(comparison.new_rules)}")
    if comparison.remaining_rules:
        blockers.append(f"remaining DRC rule(s): {', '.join(comparison.remaining_rules)}")
    if not blockers and comparison.after_count:
        blockers.append(f"{comparison.after_count} DRC issue(s) remain")
    return tuple(blockers)


def _lvs_eco_closure_blockers(comparison: LvsEcoComparison | None) -> tuple[str, ...]:
    if comparison is None or comparison.passed:
        return ()
    blockers = []
    if comparison.new:
        blockers.append(f"new LVS issue(s): {', '.join(_lvs_key_text(item) for item in comparison.new)}")
    if comparison.remaining:
        blockers.append(f"remaining LVS issue(s): {', '.join(_lvs_key_text(item) for item in comparison.remaining)}")
    if not blockers and comparison.after_count:
        blockers.append(f"{comparison.after_count} LVS issue(s) remain")
    return tuple(blockers)


def _lvs_key_text(item: tuple[str, str]) -> str:
    kind, net = item
    return f"{kind}:{net}" if net else kind


def _closure_provenance(
    provenance: Mapping[str, object] | None,
    scorecard_comparison: PostLayoutScorecardComparison | None,
    run_summary_comparison: PostLayoutRunSummaryComparison | None,
    pex_hotspot_comparison: PexHotspotComparison | None,
    drc_eco_comparison: DrcEcoComparison | None,
    lvs_eco_comparison: LvsEcoComparison | None,
) -> dict[str, object]:
    result = dict(provenance or {})
    sources = []
    if scorecard_comparison is not None:
        sources.append("scorecard")
    if run_summary_comparison is not None:
        sources.append("run_summary")
    if pex_hotspot_comparison is not None:
        sources.append("pex_hotspots")
    if drc_eco_comparison is not None:
        sources.append("drc_eco")
    if lvs_eco_comparison is not None:
        sources.append("lvs_eco")
    result["sources"] = tuple(sources)
    return result


def _closure_iteration_improved(
    scorecard_comparison: PostLayoutScorecardComparison | None,
    drc_eco_comparison: DrcEcoComparison | None,
    lvs_eco_comparison: LvsEcoComparison | None,
    run_summary_comparison: PostLayoutRunSummaryComparison | None,
    pex_hotspot_comparison: PexHotspotComparison | None,
) -> bool:
    if scorecard_comparison is not None and _scorecard_improvement_count(scorecard_comparison) > 0:
        return True
    if drc_eco_comparison is not None and drc_eco_comparison.improved:
        return True
    if lvs_eco_comparison is not None and lvs_eco_comparison.improved:
        return True
    if run_summary_comparison is not None and (
        run_summary_comparison.passing_delta > 0
        or bool(run_summary_comparison.recovered_run_ids)
        or any(delta.improved is True for delta in run_summary_comparison.worst_metric_deltas)
    ):
        return True
    if pex_hotspot_comparison is not None and (pex_hotspot_comparison.improved_nets or pex_hotspot_comparison.cleared_hotspots):
        return True
    return False


def _closure_iteration_decision(
    artifact: VerificationClosureArtifact,
    scorecard_comparison: PostLayoutScorecardComparison | None,
    drc_eco_comparison: DrcEcoComparison | None,
    lvs_eco_comparison: LvsEcoComparison | None,
    run_summary_comparison: PostLayoutRunSummaryComparison | None,
    pex_hotspot_comparison: PexHotspotComparison | None,
    *,
    block_on_critical_pex_regression: bool = True,
    block_on_any_pex_regression: bool = False,
) -> VerificationClosureDecision:
    if artifact.accepted:
        return VerificationClosureDecision("accept_verification_closure", True, artifact.reason, (), artifact.next_actions)

    regressions = []
    next_actions = list(artifact.next_actions)
    if scorecard_comparison is not None:
        if scorecard_comparison.drc_delta > 0:
            regressions.append(f"DRC issue count increased by {scorecard_comparison.drc_delta}")
        if scorecard_comparison.lvs_delta > 0:
            regressions.append(f"LVS issue count increased by {scorecard_comparison.lvs_delta}")
        worsened_metrics = tuple(delta.name for delta in scorecard_comparison.metric_deltas if delta.improved is False)
        if worsened_metrics:
            regressions.append(f"metric regression: {', '.join(worsened_metrics)}")
    if run_summary_comparison is not None:
        regressions.extend(_run_summary_closure_blockers(run_summary_comparison))
    if pex_hotspot_comparison is not None:
        regressions.extend(
            _pex_hotspot_closure_blockers(
                pex_hotspot_comparison,
                block_on_critical_pex_regression,
                block_on_any_pex_regression,
            )
        )
    if drc_eco_comparison is not None and drc_eco_comparison.new_rules:
        regressions.append(f"new DRC rule(s): {', '.join(drc_eco_comparison.new_rules)}")
    if lvs_eco_comparison is not None and lvs_eco_comparison.new:
        regressions.append(f"new LVS issue(s): {', '.join(_lvs_key_text(item) for item in lvs_eco_comparison.new)}")
    if regressions:
        return VerificationClosureDecision(
            "reject_or_continue_eco",
            False,
            "; ".join(dict.fromkeys(regressions)),
            tuple(dict.fromkeys(regressions)),
            tuple(dict.fromkeys(next_actions)),
        )

    if _closure_iteration_improved(
        scorecard_comparison,
        drc_eco_comparison,
        lvs_eco_comparison,
        run_summary_comparison,
        pex_hotspot_comparison,
    ):
        return VerificationClosureDecision(
            "continue_verification_ecos",
            False,
            "verification improved but closure is not yet clean",
            (),
            tuple(dict.fromkeys(("continue_verification_ecos", *next_actions))),
        )

    return VerificationClosureDecision(
        "hold_for_manual_review",
        False,
        "verification did not regress, but no clear closure improvement was detected",
        (),
        tuple(dict.fromkeys(("manual_verification_review", *next_actions))),
    )


def _closure_iteration_summary(
    artifact: VerificationClosureArtifact,
    scorecard_comparison: PostLayoutScorecardComparison | None,
    drc_eco_comparison: DrcEcoComparison | None,
    lvs_eco_comparison: LvsEcoComparison | None,
    run_summary_comparison: PostLayoutRunSummaryComparison | None,
    pex_hotspot_comparison: PexHotspotComparison | None,
    *,
    improved: bool,
) -> tuple[str, ...]:
    lines = [artifact.reason]
    if scorecard_comparison is not None:
        lines.append(
            f"scorecard after_passed={scorecard_comparison.after_passed} drc_delta={scorecard_comparison.drc_delta} lvs_delta={scorecard_comparison.lvs_delta}"
        )
    if drc_eco_comparison is not None:
        lines.append(
            f"drc before={drc_eco_comparison.before_count} after={drc_eco_comparison.after_count} improved={drc_eco_comparison.improved}"
        )
    if lvs_eco_comparison is not None:
        lines.append(
            f"lvs before={lvs_eco_comparison.before_count} after={lvs_eco_comparison.after_count} improved={lvs_eco_comparison.improved}"
        )
    if run_summary_comparison is not None:
        lines.append(
            f"runs passing_delta={run_summary_comparison.passing_delta} failing_delta={run_summary_comparison.failing_delta}"
        )
    if pex_hotspot_comparison is not None:
        lines.append(
            f"pex worsened={len(pex_hotspot_comparison.worsened_nets)} improved={len(pex_hotspot_comparison.improved_nets)}"
        )
    lines.extend(_flow_comparison_summary_lines(artifact.provenance))
    lines.append("closure improved" if improved else "closure not improved")
    return tuple(lines)


def _closure_iteration_provenance(
    provenance: Mapping[str, object] | None,
    artifact_provenance: Mapping[str, object] | None,
    *,
    iteration_index: int,
    drc_before_count: int,
    drc_after_count: int,
    lvs_before_count: int,
    lvs_after_count: int,
    has_scorecard: bool,
    has_run_summary: bool,
    has_pex: bool,
    has_post_layout_proposal: bool,
    has_drc_proposal: bool,
    has_lvs_proposal: bool,
) -> dict[str, object]:
    result = dict(provenance or {})
    result.update(
        {
            "iteration_index": iteration_index,
            "drc_before_count": drc_before_count,
            "drc_after_count": drc_after_count,
            "lvs_before_count": lvs_before_count,
            "lvs_after_count": lvs_after_count,
            "has_scorecard_comparison": has_scorecard,
            "has_run_summary_comparison": has_run_summary,
            "has_pex_comparison": has_pex,
            "has_post_layout_repair_proposal": has_post_layout_proposal,
            "has_drc_repair_proposal": has_drc_proposal,
            "has_lvs_repair_proposal": has_lvs_proposal,
        }
    )
    flow_snapshot = _flow_comparison_provenance_snapshot(artifact_provenance)
    if flow_snapshot:
        result.update(flow_snapshot)
    return result


def _closure_loop_summary(
    iterations: tuple[VerificationClosureIteration, ...],
    *,
    terminated_early: bool,
) -> tuple[str, ...]:
    if not iterations:
        return ("no verification closure iterations were executed",)
    final_iteration = iterations[-1]
    lines = [
        f"executed {len(iterations)} verification closure iteration(s)",
        f"final action={final_iteration.decision.action if final_iteration.decision is not None else ''} accepted={final_iteration.passed}",
        f"final reason={final_iteration.stop_reason}",
    ]
    flow_iterations = tuple(
        iteration
        for iteration in iterations
        if bool(iteration.provenance.get("has_flow_comparison", False))
    )
    if flow_iterations:
        lines.append(
            "flow comparison "
            f"iterations={len(flow_iterations)} "
            f"hierarchical={sum(1 for item in flow_iterations if 'hierarchical_flow_comparison' in item.provenance)} "
            f"foundry={sum(1 for item in flow_iterations if 'foundry_flow_comparison' in item.provenance)} "
            f"system={sum(1 for item in flow_iterations if 'hierarchical_system_regression_flow_comparison' in item.provenance)}"
        )
        latest_summary = tuple(flow_iterations[-1].provenance.get("flow_comparison_summary", ()) or ())
        if latest_summary:
            lines.append(f"latest flow comparison={latest_summary[0]}")
    if terminated_early:
        lines.append("loop terminated early on stop condition")
    return tuple(lines)


def _closure_loop_provenance(
    provenance: Mapping[str, object] | None,
    iterations: tuple[VerificationClosureIteration, ...],
    *,
    terminated_early: bool,
) -> dict[str, object]:
    result = dict(provenance or {})
    result.update(
        {
            "iteration_count": len(iterations),
            "terminated_early": terminated_early,
            "accepted_iteration_count": sum(1 for item in iterations if item.passed),
            "improved_iteration_count": sum(1 for item in iterations if item.improved),
        }
    )
    if iterations:
        result["final_iteration_index"] = iterations[-1].iteration_index
        result["final_action"] = iterations[-1].decision.action if iterations[-1].decision is not None else ""
    flow_iterations = tuple(
        iteration
        for iteration in iterations
        if bool(iteration.provenance.get("has_flow_comparison", False))
    )
    result["flow_comparison_iteration_count"] = len(flow_iterations)
    result["hierarchical_flow_comparison_iteration_count"] = sum(
        1 for iteration in flow_iterations if "hierarchical_flow_comparison" in iteration.provenance
    )
    result["foundry_flow_comparison_iteration_count"] = sum(
        1 for iteration in flow_iterations if "foundry_flow_comparison" in iteration.provenance
    )
    result["hierarchical_system_regression_flow_comparison_iteration_count"] = sum(
        1 for iteration in flow_iterations if "hierarchical_system_regression_flow_comparison" in iteration.provenance
    )
    if flow_iterations:
        result["flow_comparison_iteration_indexes"] = tuple(item.iteration_index for item in flow_iterations)
        latest = flow_iterations[-1].provenance
        latest_summary = tuple(latest.get("flow_comparison_summary", ()) or ())
        if latest_summary:
            result["latest_flow_comparison_summary"] = latest_summary
        if "hierarchical_flow_comparison" in latest:
            result["latest_hierarchical_flow_comparison"] = dict(latest["hierarchical_flow_comparison"])
        if "foundry_flow_comparison" in latest:
            result["latest_foundry_flow_comparison"] = dict(latest["foundry_flow_comparison"])
        if "hierarchical_system_regression_flow_comparison" in latest:
            result["latest_hierarchical_system_regression_flow_comparison"] = dict(
                latest["hierarchical_system_regression_flow_comparison"]
            )
    result["repair_queue_size"] = len(_closure_loop_repair_queue(iterations))
    return result


def _flow_comparison_summary_lines(provenance: Mapping[str, object] | None) -> tuple[str, ...]:
    root = dict(provenance or {})
    summary = root.get("flow_comparison_summary", ())
    if not isinstance(summary, (tuple, list)):
        return ()
    return tuple(str(line) for line in summary if str(line))


def _flow_comparison_provenance_snapshot(provenance: Mapping[str, object] | None) -> dict[str, object]:
    root = dict(provenance or {})
    if not bool(root.get("has_flow_comparison", False)):
        return {}
    result: dict[str, object] = {"has_flow_comparison": True}
    summary = _flow_comparison_summary_lines(root)
    if summary:
        result["flow_comparison_summary"] = summary
    foundry = root.get("foundry_flow_comparison")
    if isinstance(foundry, Mapping):
        result["foundry_flow_comparison"] = dict(foundry)
    hierarchical = root.get("hierarchical_flow_comparison")
    if isinstance(hierarchical, Mapping):
        result["hierarchical_flow_comparison"] = dict(hierarchical)
    system_regression = root.get("hierarchical_system_regression_flow_comparison")
    if isinstance(system_regression, Mapping):
        result["hierarchical_system_regression_flow_comparison"] = dict(system_regression)
    return result


def _closure_loop_repair_queue(iterations: tuple[VerificationClosureIteration, ...]) -> tuple[dict[str, object], ...]:
    queue: list[dict[str, object]] = []
    for iteration in iterations:
        if iteration.post_layout_repair_proposal is not None:
            queue.append(
                _closure_loop_repair_queue_item(
                    iteration.iteration_index,
                    "post_layout",
                    iteration.post_layout_repair_proposal,
                    iteration.post_layout_repair_object,
                    iteration.provenance,
                )
            )
        if iteration.drc_repair_proposal is not None:
            queue.append(
                _closure_loop_repair_queue_item(
                    iteration.iteration_index,
                    "drc",
                    iteration.drc_repair_proposal,
                    iteration.drc_repair_object,
                    iteration.provenance,
                )
            )
        if iteration.lvs_repair_proposal is not None:
            queue.append(
                _closure_loop_repair_queue_item(
                    iteration.iteration_index,
                    "lvs",
                    iteration.lvs_repair_proposal,
                    iteration.lvs_repair_object,
                    iteration.provenance,
                )
            )
    return tuple(queue)


def _closure_loop_repair_queue_item(
    iteration_index: int,
    source: str,
    proposal: Mapping[str, object],
    repair_proposal: object | None,
    provenance: Mapping[str, object] | None,
) -> dict[str, object]:
    selected_scope = _selected_repair_scope(proposal)
    system_guidance = _match_system_repair_guidance(
        proposal,
        repair_proposal,
        provenance,
    )
    selected_scope = _annotate_repair_scope_with_system_guidance(selected_scope, system_guidance)
    execution_profile = _repair_queue_execution_profile(
        source=source,
        proposal=proposal,
        repair_scope=selected_scope,
        system_guidance=system_guidance,
    )
    return {
        "iteration_index": iteration_index,
        "source": source,
        "kind": str(proposal.get("kind", source)),
        "candidate_count": int(proposal.get("candidate_count", 0)),
        "selected_plan_kind": str(proposal.get("selected_plan_kind", "")),
        "selected_passed": bool(proposal.get("selected_passed", False)),
        "selected_score": float(proposal.get("selected_score", float("inf"))),
        "selected_issues_after": tuple(proposal.get("selected_issues_after", ()) or ()),
        "repair_scope": selected_scope,
        "system_repair_guidance": system_guidance,
        "execution_profile": execution_profile,
        "proposal": dict(proposal),
        "repair_proposal": repair_proposal,
    }


def _match_system_repair_guidance(
    proposal: Mapping[str, object],
    repair_proposal: object | None,
    provenance: Mapping[str, object] | None,
) -> tuple[dict[str, object], ...]:
    cycle_metadata = dict(dict(provenance or {}).get("cycle_metadata", {}) or {})
    system_contract = dict(cycle_metadata.get("hierarchical_system_contract", {}) or {})
    if not system_contract:
        return ()
    proposal_nets = _repair_proposal_nets(repair_proposal)
    if not proposal_nets:
        proposal_nets = {
            str(net)
            for key in ("scope_nets", "hotspot_nets", "avoid_nets")
            for net in tuple(proposal.get(key, ()) or ())
            if str(net)
        }
    if not proposal_nets:
        return ()
    guidance: list[dict[str, object]] = []
    for bus in tuple(system_contract.get("bus_contracts", ()) or ()):
        item = dict(bus) if isinstance(bus, Mapping) else {}
        nets = {str(net) for net in tuple(item.get("nets", ()) or ()) if str(net)}
        if bool(item.get("restore_required", False)) and nets & proposal_nets:
            guidance.append(
                {
                    "kind": "bus_corridor_restore",
                    "name": str(item.get("name", "")),
                    "nets": tuple(sorted(nets & proposal_nets)),
                    "recommended_level": "parent",
                    "reason": "restoring a removed system bus corridor usually belongs to enclosing routing context",
                }
            )
    for feedback in tuple(system_contract.get("feedback_contracts", ()) or ()):
        item = dict(feedback) if isinstance(feedback, Mapping) else {}
        net = str(item.get("net", ""))
        if bool(item.get("restore_required", False)) and net and net in proposal_nets:
            guidance.append(
                {
                    "kind": "feedback_path_restore",
                    "name": net,
                    "nets": (net,),
                    "recommended_level": "top",
                    "reason": "restoring a removed system feedback path usually spans top-level control connectivity",
                }
            )
    for reference in tuple(system_contract.get("reference_paths", ()) or ()):
        item = dict(reference) if isinstance(reference, Mapping) else {}
        net = str(item.get("net", ""))
        if bool(item.get("preserve_integrity", False)) and net and net in proposal_nets:
            guidance.append(
                {
                    "kind": "reference_integrity_protect",
                    "name": net,
                    "nets": (net,),
                    "recommended_level": "leaf_or_parent",
                    "reason": "preserving a reference path should stay local unless enclosing routing is required",
                }
            )
    return tuple(guidance)


def _annotate_repair_scope_with_system_guidance(
    scope: Mapping[str, object] | None,
    guidance: tuple[dict[str, object], ...],
) -> dict[str, object]:
    result = dict(scope or {})
    if not guidance:
        return result
    metadata = dict(result.get("metadata", {}) or {})
    metadata["system_repair_guidance"] = tuple(dict(item) for item in guidance)
    metadata["system_repair_levels"] = tuple(
        dict.fromkeys(str(item.get("recommended_level", "")) for item in guidance if str(item.get("recommended_level", "")))
    )
    result["metadata"] = metadata
    current_level = str(result.get("level", "") or "")
    current_rank = _repair_scope_rank({"level": current_level})
    recommended_level = _system_repair_guidance_level(guidance)
    if recommended_level:
        result["system_recommended_level"] = recommended_level
        recommended_rank = _repair_scope_rank({"level": recommended_level})
        if recommended_rank > current_rank:
            result["escalation_required"] = True
    return result


def _system_repair_guidance_level(guidance: tuple[dict[str, object], ...]) -> str:
    best_level = ""
    best_rank = -1
    for item in guidance:
        level = str(item.get("recommended_level", ""))
        normalized = "parent" if level == "leaf_or_parent" else level
        rank = _repair_scope_rank({"level": normalized})
        if rank > best_rank:
            best_rank = rank
            best_level = level
    return best_level


def _verification_repair_action_rank(item: Mapping[str, object]) -> tuple[int, int, float, int, int]:
    # Prefer executable/cleaner selected candidates, then narrower repair scope, then lower score, earlier iteration, then DRC before LVS.
    selected_passed = bool(item.get("selected_passed", False))
    scope = dict(item.get("repair_scope", {}))
    scope_rank = _repair_scope_rank(scope)
    recommended_level = str(scope.get("system_recommended_level", ""))
    effective_scope_rank = max(
        scope_rank,
        _repair_scope_rank({"level": "parent" if recommended_level == "leaf_or_parent" else recommended_level}),
    ) if recommended_level else scope_rank
    selected_score = float(item.get("selected_score", float("inf")))
    iteration_index = int(item.get("iteration_index", 0))
    source = str(item.get("source", ""))
    source_rank = {"drc": 0, "lvs": 1, "post_layout": 2}.get(source, 3)
    escalation_penalty = 1 if bool(scope.get("escalation_required", False)) else 0
    return (0 if selected_passed else 1, escalation_penalty, effective_scope_rank, selected_score, iteration_index, source_rank)


def _verification_repair_rerun_kind(action: VerificationRepairAction) -> str:
    scope_level = str(action.repair_scope.get("level", ""))
    if scope_level == "cross_hierarchy":
        return "rerun_enclosing_hierarchy_verification"
    if scope_level == "parent":
        return "rerun_parent_level_verification"
    if scope_level == "top":
        return "rerun_top_level_verification"
    if action.source == "drc":
        return "rerun_drc"
    if action.source == "lvs":
        return "rerun_lvs"
    if action.source == "post_layout":
        return "rerun_pex_and_post_layout_simulation"
    return "rerun_full_verification"


def _verification_repair_reason(action: VerificationRepairAction) -> str:
    state = "passed" if action.selected_passed else "unproven"
    scope = str(action.repair_scope.get("level", "unknown"))
    target = str(action.repair_scope.get("target", ""))
    reason = (
        f"selected {action.source} repair {action.selected_plan_kind or action.kind} "
        f"from iteration {action.iteration_index} with score {action.selected_score:g} ({state}); "
        f"scope={scope}{f' target={target}' if target else ''}"
    )
    if bool(action.repair_scope.get("escalation_required", False)):
        recommended = str(action.repair_scope.get("system_recommended_level", ""))
        if recommended:
            reason += f"; system_guidance_requires_scope_escalation={recommended}"
    execution_class = str(action.execution_profile.get("execution_class", ""))
    if execution_class:
        reason += f"; execution_class={execution_class}"
    blocker_kinds = tuple(str(item) for item in tuple(action.execution_profile.get("blocking_system_kinds", ()) or ()) if str(item))
    if blocker_kinds:
        reason += f"; system_blockers={','.join(blocker_kinds)}"
    return reason


def _verification_repair_followup_actions(action: VerificationRepairAction, recommended_rerun: str) -> tuple[str, ...]:
    actions = [recommended_rerun]
    if bool(action.repair_scope.get("escalation_required", False)):
        actions.append("review_system_scope_escalation")
    blocker_kinds = tuple(str(item) for item in tuple(action.execution_profile.get("blocking_system_kinds", ()) or ()) if str(item))
    if "feedback_path_restore" in blocker_kinds or "system_regression_feedback_loop" in blocker_kinds:
        actions.append("review_feedback_restore_coverage")
    if "bus_corridor_restore" in blocker_kinds or "system_regression_bus_corridor" in blocker_kinds:
        actions.append("review_enclosing_bus_route_eco")
    if "reference_integrity_protect" in blocker_kinds or "system_regression_reference_path" in blocker_kinds:
        actions.append("review_reference_path_integrity")
    if "system_regression_timing_chain" in blocker_kinds:
        actions.append("review_timing_chain_propagation")
    scope_level = str(action.repair_scope.get("level", ""))
    if scope_level == "cross_hierarchy":
        actions.append("review_cross_hierarchy_eco_dispatch")
        actions.append("rerun_enclosing_levels_after_writeback")
        return tuple(actions)
    if scope_level == "parent":
        actions.append("rerun_leaf_checks_if_parent_route_changes")
    elif scope_level == "top":
        actions.append("propagate_top_level_writeback_to_signoff_checks")
    if action.source == "drc":
        actions.append("rerun_lvs_if_drc_cleans")
    elif action.source == "lvs":
        actions.append("rerun_drc_after_lvs_writeback")
    elif action.source == "post_layout":
        actions.append("compare_post_layout_scorecard_after_eco")
    else:
        actions.append("review_full_verification_state")
    return tuple(actions)


def _verification_repair_writeback_level(action: VerificationRepairAction) -> str:
    scope_level = str(action.repair_scope.get("level", ""))
    if scope_level in {"leaf", "parent", "top", "cross_hierarchy"}:
        return scope_level
    return "leaf"


def _verification_repair_writeback_target(action: VerificationRepairAction) -> str:
    return str(action.repair_scope.get("target", ""))


def _verification_repair_rerun_levels(action: VerificationRepairAction, recommended_rerun: str) -> tuple[str, ...]:
    scope_level = str(action.repair_scope.get("level", ""))
    if scope_level == "cross_hierarchy":
        return (recommended_rerun, "rerun_top_level_verification", "rerun_signoff_bundle")
    if scope_level == "top":
        return (recommended_rerun, "rerun_signoff_bundle")
    if scope_level == "parent":
        return (recommended_rerun, _source_specific_rerun(action))
    return (recommended_rerun,)


def _verification_repair_requires_manual_handoff(action: VerificationRepairAction) -> bool:
    if str(action.selected_plan_kind or "") in {"model_pcell_handoff"}:
        return True
    if bool(dict(action.execution_profile).get("requires_manual_handoff", False)):
        return True
    metadata = dict(action.repair_scope.get("metadata", {}) or {})
    return bool(metadata.get("manual_dispatch_required", False))


def _verification_repair_dispatch_mode(action: VerificationRepairAction) -> str:
    if _verification_repair_requires_manual_handoff(action):
        return "manual_orchestrated_apply"
    scope_level = str(action.repair_scope.get("level", ""))
    if scope_level == "cross_hierarchy":
        return "manual_orchestrated_apply"
    if scope_level == "top":
        return "top_level_apply"
    if scope_level == "parent":
        return "enclosing_level_apply"
    return "direct_apply"


def _repair_queue_execution_profile(
    *,
    source: str,
    proposal: Mapping[str, object],
    repair_scope: Mapping[str, object],
    system_guidance: tuple[dict[str, object], ...],
) -> dict[str, object]:
    scope_level = str(repair_scope.get("level", ""))
    selected_plan_kind = str(proposal.get("selected_plan_kind", ""))
    metadata = dict(repair_scope.get("metadata", {}) or {})
    requires_manual_handoff = bool(metadata.get("manual_dispatch_required", False)) or selected_plan_kind in {"model_pcell_handoff"}
    blocking_system_kinds = tuple(
        dict.fromkeys(str(item.get("kind", "")) for item in system_guidance if str(item.get("kind", "")))
    )
    execution_class = "leaf_inplace_fix"
    if requires_manual_handoff:
        execution_class = "leaf_manual_handoff"
    elif scope_level == "parent":
        execution_class = "enclosing_route_or_context_fix"
    elif scope_level == "top":
        execution_class = "top_level_integration_fix"
    elif scope_level == "cross_hierarchy":
        execution_class = "cross_hierarchy_handoff"
    return {
        "source": source,
        "selected_plan_kind": selected_plan_kind,
        "scope_level": scope_level,
        "scope_target": str(repair_scope.get("target", "")),
        "execution_class": execution_class,
        "requires_manual_handoff": requires_manual_handoff,
        "system_escalation_required": bool(repair_scope.get("escalation_required", False)),
        "system_recommended_level": str(repair_scope.get("system_recommended_level", "")),
        "blocking_system_kinds": blocking_system_kinds,
    }


def _verification_repair_execution_profile(
    action: VerificationRepairAction,
    *,
    recommended_rerun: str,
    writeback_level: str,
    dispatch_mode: str,
) -> dict[str, object]:
    profile = dict(action.execution_profile)
    if not profile:
        profile = _repair_queue_execution_profile(
            source=action.source,
            proposal={"selected_plan_kind": action.selected_plan_kind or action.kind},
            repair_scope=action.repair_scope,
            system_guidance=tuple(
                dict(item)
                for item in tuple(dict(action.repair_scope.get("metadata", {}) or {}).get("system_repair_guidance", ()) or ())
                if isinstance(item, Mapping)
            ),
        )
    profile.setdefault("source", action.source)
    profile.setdefault("selected_plan_kind", action.selected_plan_kind or action.kind)
    profile["recommended_rerun"] = recommended_rerun
    profile["writeback_level"] = writeback_level
    profile["dispatch_mode"] = dispatch_mode
    profile["requires_manual_handoff"] = dispatch_mode == "manual_orchestrated_apply"
    profile["requires_enclosing_context"] = writeback_level in {"parent", "top", "cross_hierarchy"}
    return profile


def _execution_profile_from_plan(plan: object) -> dict[str, object]:
    action = getattr(plan, "action", None)
    action_profile = dict(getattr(action, "execution_profile", {}) or {}) if action is not None else {}
    plan_profile = dict(getattr(plan, "execution_profile", {}) or {})
    profile = {**action_profile, **plan_profile}
    if profile:
        profile.setdefault("source", str(getattr(action, "source", "")) if action is not None else "")
        profile.setdefault("selected_plan_kind", str(getattr(action, "selected_plan_kind", "") or getattr(action, "kind", "")) if action is not None else "")
    dispatch_mode = str(getattr(plan, "dispatch_mode", "") or "")
    writeback_level = str(getattr(plan, "writeback_level", "") or "")
    if not profile:
        scope = dict(getattr(action, "repair_scope", {}) or {}) if action is not None else {}
        profile = _repair_queue_execution_profile(
            source=str(getattr(action, "source", "")) if action is not None else "",
            proposal={"selected_plan_kind": str(getattr(action, "selected_plan_kind", "") or getattr(action, "kind", ""))},
            repair_scope=scope,
            system_guidance=tuple(
                dict(item)
                for item in tuple(dict(scope.get("metadata", {}) or {}).get("system_repair_guidance", ()) or ())
                if isinstance(item, Mapping)
            ),
        )
    profile["writeback_level"] = writeback_level
    profile["dispatch_mode"] = dispatch_mode
    profile["requires_manual_handoff"] = dispatch_mode == "manual_orchestrated_apply"
    profile["requires_enclosing_context"] = writeback_level in {"parent", "top", "cross_hierarchy"}
    return profile


def _stage_execution_profile(
    plan: VerificationRepairExecutionPlan,
    stage: Mapping[str, object],
    *,
    stage_dispatch_plan: Mapping[str, object],
    target_cellview: Mapping[str, object],
) -> dict[str, object]:
    plan_profile = _execution_profile_from_plan(plan)
    role = str(stage.get("role", ""))
    scope_level = str(stage.get("scope_level", plan.writeback_level))
    dispatch_mode = str(stage.get("dispatch_mode", plan.dispatch_mode))
    target_cell = str(target_cellview.get("cell", "") or stage.get("cell", ""))
    execution_class = str(plan_profile.get("execution_class", ""))
    if role == "source":
        execution_class = "leaf_inplace_fix" if scope_level in {"leaf", "top"} else "enclosing_route_or_context_fix"
    elif role == "intermediate" and scope_level in {"parent", "top"}:
        execution_class = "enclosing_route_or_context_fix"
    elif role == "target" and scope_level == "top":
        execution_class = "top_level_integration_fix"
    elif role == "target" and scope_level == "cross_hierarchy":
        execution_class = "cross_hierarchy_handoff"
    elif role == "source" and scope_level == "leaf":
        execution_class = "leaf_inplace_fix"
    return {
        **plan_profile,
        "role": role,
        "scope_level": scope_level,
        "dispatch_mode": dispatch_mode,
        "stage_target": target_cell,
        "execution_class": execution_class,
        "requires_manual_handoff": dispatch_mode == "manual_orchestrated_apply" or execution_class == "cross_hierarchy_handoff",
        "requires_enclosing_context": scope_level in {"parent", "top", "cross_hierarchy"} or role == "intermediate",
    }


def _source_specific_rerun(action: VerificationRepairAction) -> str:
    if action.source == "drc":
        return "rerun_drc"
    if action.source == "lvs":
        return "rerun_lvs"
    if action.source == "post_layout":
        return "rerun_pex_and_post_layout_simulation"
    return "rerun_full_verification"


def _verification_repair_dispatch_plan(
    action: VerificationRepairAction,
    *,
    writeback_level: str,
    writeback_target: str,
    rerun_levels: tuple[str, ...],
    dispatch_mode: str,
    hierarchy_database: tuple[HierarchyCellviewNode, ...] = (),
) -> dict[str, object]:
    source_cellview = _repair_proposal_source_cellview(action.repair_proposal)
    target_cellview, hierarchy_resolution = _resolve_dispatch_target_cellview(
        source_cellview,
        writeback_level=writeback_level,
        writeback_target=writeback_target,
        hierarchy_database=hierarchy_database,
    )
    apply_steps = _verification_repair_dispatch_steps(
        action,
        source_cellview=source_cellview,
        target_cellview=target_cellview,
        writeback_level=writeback_level,
        dispatch_mode=dispatch_mode,
    )
    verification_steps = _verification_repair_verification_steps(
        rerun_levels,
        source_cellview=source_cellview,
        target_cellview=target_cellview,
        writeback_level=writeback_level,
    )
    return {
        "dispatch_mode": dispatch_mode,
        "writeback_level": writeback_level,
        "writeback_target": writeback_target,
        "source_cellview": source_cellview,
        "target_cellview": target_cellview,
        "hierarchy_resolution": hierarchy_resolution,
        "orchestration_plan": _verification_repair_orchestration_plan(
            action,
            source_cellview=source_cellview,
            target_cellview=target_cellview,
            hierarchy_resolution=hierarchy_resolution,
            writeback_level=writeback_level,
            dispatch_mode=dispatch_mode,
        ),
        "rerun_levels": rerun_levels,
        "apply_steps": apply_steps,
        "verification_steps": verification_steps,
        "apply_policy": (
            "manual_orchestrated"
            if dispatch_mode == "manual_orchestrated_apply"
            else "direct_target_writeback"
        ),
        "scope_guard": _verification_repair_scope_guard(action),
    }


def _verification_repair_orchestration_plan(
    action: VerificationRepairAction,
    *,
    source_cellview: Mapping[str, object],
    target_cellview: Mapping[str, object],
    hierarchy_resolution: Mapping[str, object],
    writeback_level: str,
    dispatch_mode: str,
) -> dict[str, object]:
    node_path = tuple(str(item) for item in hierarchy_resolution.get("path", ()) if str(item))
    path = _verification_repair_cell_path(
        source_cellview=source_cellview,
        target_cellview=target_cellview,
        hierarchy_resolution=hierarchy_resolution,
    )
    repair_kind = action.selected_plan_kind or action.kind
    scope_target = str(action.repair_scope.get("target", ""))
    stages = _verification_repair_orchestration_stages(
        path=path,
        source_cellview=source_cellview,
        target_cellview=target_cellview,
        hierarchy_resolution=hierarchy_resolution,
        writeback_level=writeback_level,
        dispatch_mode=dispatch_mode,
    )
    return {
        "mode": "manual_handoff" if dispatch_mode == "manual_orchestrated_apply" else "direct_writeback",
        "repair_kind": repair_kind,
        "scope_level": writeback_level,
        "scope_target": scope_target,
        "hierarchy_path": path,
        "hierarchy_node_path": node_path,
        "stage_count": len(stages),
        "stages": stages,
        "requires_multi_cell_orchestration": len(stages) > 1 or dispatch_mode == "manual_orchestrated_apply",
        "source_cellview": dict(source_cellview),
        "target_cellview": dict(target_cellview),
    }


def _verification_repair_orchestration_stages(
    *,
    path: tuple[str, ...],
    source_cellview: Mapping[str, object],
    target_cellview: Mapping[str, object],
    hierarchy_resolution: Mapping[str, object],
    writeback_level: str,
    dispatch_mode: str,
) -> tuple[dict[str, object], ...]:
    source_cell = str(source_cellview.get("cell", ""))
    target_cell = str(target_cellview.get("cell", ""))
    stages: list[dict[str, object]] = []
    stage_path = path or tuple(
        name for name in (source_cell, target_cell) if name
    )
    for order, name in enumerate(stage_path, start=1):
        role = "intermediate"
        if order == 1:
            role = "source"
        if order == len(stage_path):
            role = "target"
        stages.append(
            {
                "order": order,
                "cell": name,
                "role": role,
                "scope_level": writeback_level,
                "dispatch_mode": dispatch_mode,
                "target_cellview": _orchestration_stage_target_cellview(
                    name,
                    source_cellview=source_cellview,
                    target_cellview=target_cellview,
                    hierarchy_resolution=hierarchy_resolution,
                ),
                "action": (
                    "handoff_boundary"
                    if dispatch_mode == "manual_orchestrated_apply" and role != "target"
                    else ("apply_writeback" if role == "target" else "propagate_context")
                ),
            }
        )
    if dispatch_mode == "manual_orchestrated_apply" and not stages:
        stages.append(
            {
                "order": 1,
                "cell": target_cell,
                "role": "target",
                "scope_level": writeback_level,
                "dispatch_mode": dispatch_mode,
                "target_cellview": dict(target_cellview),
                "action": "handoff_boundary",
            }
        )
    return tuple(stages)


def _verification_repair_cell_path(
    *,
    source_cellview: Mapping[str, object],
    target_cellview: Mapping[str, object],
    hierarchy_resolution: Mapping[str, object],
) -> tuple[str, ...]:
    source_cell = str(source_cellview.get("cell", ""))
    target_cell = str(target_cellview.get("cell", ""))
    path = [source_cell] if source_cell else []
    source_node = dict(hierarchy_resolution.get("source_node", {}) or {})
    target_node = dict(hierarchy_resolution.get("target_node", {}) or {})
    path_nodes = tuple(hierarchy_resolution.get("path_nodes", ()) or ())
    path_node_map = {
        str(node.get("name", "")): dict(node)
        for node in path_nodes
        if isinstance(node, Mapping) and str(node.get("name", ""))
    }
    for name in tuple(hierarchy_resolution.get("path", ()) or ()):
        text = str(name)
        if not text:
            continue
        if text == str(source_node.get("name", "")):
            continue
        if text == str(target_node.get("name", "")):
            mapped = str(target_node.get("cell", "")) or target_cell or text
        else:
            mapped = str(path_node_map.get(text, {}).get("cell", "")) or text
        if mapped and mapped not in path:
            path.append(mapped)
    if target_cell and target_cell not in path:
        path.append(target_cell)
    return tuple(path)


def _orchestration_stage_target_cellview(
    stage_cell: str,
    *,
    source_cellview: Mapping[str, object],
    target_cellview: Mapping[str, object],
    hierarchy_resolution: Mapping[str, object],
) -> dict[str, object]:
    source_cell = str(source_cellview.get("cell", ""))
    target_cell = str(target_cellview.get("cell", ""))
    if stage_cell == source_cell and source_cellview:
        return dict(source_cellview)
    if stage_cell == target_cell and target_cellview:
        return dict(target_cellview)
    for node in tuple(hierarchy_resolution.get("path_nodes", ()) or ()):
        if not isinstance(node, Mapping):
            continue
        node_name = str(node.get("name", ""))
        node_cell = str(node.get("cell", ""))
        if stage_cell not in {node_name, node_cell}:
            continue
        return {
            "lib": str(node.get("lib", "")),
            "cell": node_cell or stage_cell,
            "view": str(node.get("view", "layout")),
            "view_type": str(node.get("view_type", "maskLayout")),
            "mode": "w",
        }
    return {"cell": stage_cell}


def _verification_repair_scope_guard(action: VerificationRepairAction) -> dict[str, object]:
    proposal = action.repair_proposal
    if proposal is None:
        return {}
    metadata = dict(getattr(proposal, "metadata", {}) or {})
    selected = getattr(proposal, "selected_candidate", None)
    plan = getattr(selected, "plan", None) if selected is not None else None
    layout_patch = getattr(proposal, "layout_patch", None)
    candidate_scope = {}
    if plan is not None:
        candidate_scope = dict(getattr(plan, "metadata", {}) or {})
    elif layout_patch is not None:
        candidate_scope = dict(getattr(layout_patch, "metadata", {}) or {})
    repair_scope = dict(getattr(action, "repair_scope", {}) or {})
    repair_scope_metadata = dict(repair_scope.get("metadata", {}) or {})
    scope_guard = {
        "scope_devices": tuple(
            metadata.get(
                "scope_devices",
                candidate_scope.get("scope_devices", repair_scope_metadata.get("scope_devices", ())),
            )
        ),
        "avoid_devices": tuple(
            metadata.get(
                "avoid_devices",
                candidate_scope.get("avoid_devices", repair_scope_metadata.get("avoid_devices", ())),
            )
        ),
        "scope_nets": tuple(
            metadata.get(
                "scope_nets",
                candidate_scope.get("scope_nets", repair_scope_metadata.get("scope_nets", ())),
            )
        ),
        "avoid_nets": tuple(
            metadata.get(
                "avoid_nets",
                candidate_scope.get("avoid_nets", repair_scope_metadata.get("avoid_nets", ())),
            )
        ),
        "scope_regions": tuple(
            metadata.get(
                "scope_regions",
                candidate_scope.get("scope_regions", repair_scope_metadata.get("scope_regions", ())),
            )
        ),
        "scope_policy": str(metadata.get("scope_policy", candidate_scope.get("scope_policy", ""))),
        "issue_bbox": metadata.get(
            "issue_bbox",
            candidate_scope.get("issue_bbox", repair_scope.get("issue_bbox")),
        ),
        "region_bbox": metadata.get(
            "region_bbox",
            candidate_scope.get("region_bbox", repair_scope.get("region_bbox")),
        ),
        "max_edit_count": metadata.get("max_edit_count", candidate_scope.get("max_edit_count", _repair_proposal_edit_count(proposal))),
        "restore_bus_nets": tuple(
            metadata.get(
                "restore_bus_nets",
                candidate_scope.get("restore_bus_nets", repair_scope_metadata.get("restore_bus_nets", ())),
            )
        ),
        "restore_feedback_nets": tuple(
            metadata.get(
                "restore_feedback_nets",
                candidate_scope.get("restore_feedback_nets", repair_scope_metadata.get("restore_feedback_nets", ())),
            )
        ),
        "protected_reference_nets": tuple(
            metadata.get(
                "protected_reference_nets",
                candidate_scope.get("protected_reference_nets", repair_scope_metadata.get("protected_reference_nets", ())),
            )
        ),
        "architecture_protected_nets": tuple(
            metadata.get(
                "architecture_protected_nets",
                candidate_scope.get("architecture_protected_nets", repair_scope_metadata.get("architecture_protected_nets", ())),
            )
        ),
        "binding_blocked_partitions": tuple(
            metadata.get(
                "binding_blocked_partitions",
                candidate_scope.get("binding_blocked_partitions", repair_scope_metadata.get("binding_blocked_partitions", ())),
            )
        ),
        "macro_bound_partitions": tuple(
            metadata.get(
                "macro_bound_partitions",
                candidate_scope.get("macro_bound_partitions", repair_scope_metadata.get("macro_bound_partitions", ())),
            )
        ),
        "architecture_budget_blocked_partitions": tuple(
            metadata.get(
                "architecture_budget_blocked_partitions",
                candidate_scope.get(
                    "architecture_budget_blocked_partitions",
                    repair_scope_metadata.get("architecture_budget_blocked_partitions", ()),
                ),
            )
        ),
    }
    return {key: value for key, value in scope_guard.items() if value}


def _verify_dispatch_scope_guard(
    proposal: object,
    dispatch_plan: Mapping[str, object],
) -> dict[str, object]:
    scope_guard = dict(dispatch_plan.get("scope_guard", {}) or {})
    if not scope_guard:
        return {"allowed": True, "reason": "no_scope_guard"}
    proposal_nets = _repair_proposal_nets(proposal)
    proposal_devices = _repair_proposal_devices(proposal)
    proposal_regions = _repair_proposal_regions(proposal)
    allowed_nets = {str(net) for net in scope_guard.get("scope_nets", ()) if str(net)}
    blocked_nets = {str(net) for net in scope_guard.get("avoid_nets", ()) if str(net)}
    blocked_nets.update(str(net) for net in scope_guard.get("protected_reference_nets", ()) if str(net))
    blocked_nets.update(str(net) for net in scope_guard.get("architecture_protected_nets", ()) if str(net))
    allowed_devices = {str(device) for device in scope_guard.get("scope_devices", ()) if str(device)}
    blocked_devices = {str(device) for device in scope_guard.get("avoid_devices", ()) if str(device)}
    allowed_regions = {str(region) for region in scope_guard.get("scope_regions", ()) if str(region)}
    scope_policy = str(scope_guard.get("scope_policy", "") or "")
    region_bbox = _coerce_bbox(scope_guard.get("region_bbox"))
    proposal_bbox = _repair_proposal_bbox(proposal)
    max_edit_count = int(scope_guard.get("max_edit_count", 0) or 0)
    proposal_edit_count = _repair_proposal_edit_count(proposal)
    violations: list[str] = []
    hard_scope_nets = bool(allowed_nets) and scope_policy in {
        "changed_nets_only",
        "allowed_nets_only",
        "changed_devices_only",
        "prefer_changed_devices",
    }
    hard_scope_devices = bool(allowed_devices) and scope_policy in {
        "changed_devices_only",
        "allowed_devices_only",
        "prefer_changed_devices",
    }
    hard_scope_regions = bool(allowed_regions) and scope_policy in {
        "changed_devices_only",
        "allowed_devices_only",
        "prefer_changed_devices",
    }
    if hard_scope_nets:
        outside = sorted(net for net in proposal_nets if net not in allowed_nets)
        if outside:
            violations.append(f"proposal nets outside allowed scope: {', '.join(outside)}")
    if blocked_nets:
        blocked = sorted(net for net in proposal_nets if net in blocked_nets)
        if blocked:
            violations.append(f"proposal nets overlap blocked scope: {', '.join(blocked)}")
    if hard_scope_devices:
        outside = sorted(device for device in proposal_devices if device not in allowed_devices)
        if outside:
            violations.append(f"proposal devices outside allowed scope: {', '.join(outside)}")
    if blocked_devices:
        blocked = sorted(device for device in proposal_devices if device in blocked_devices)
        if blocked:
            violations.append(f"proposal devices overlap blocked scope: {', '.join(blocked)}")
    if hard_scope_regions:
        outside = sorted(region for region in proposal_regions if region not in allowed_regions)
        if outside:
            violations.append(f"proposal regions outside allowed scope: {', '.join(outside)}")
    if region_bbox is not None and proposal_bbox is not None and not _bbox_contains(region_bbox, proposal_bbox):
        violations.append(f"proposal bbox {proposal_bbox} exceeds region scope {region_bbox}")
    if max_edit_count > 0 and proposal_edit_count > max_edit_count:
        violations.append(f"proposal edit count {proposal_edit_count} exceeds scope budget {max_edit_count}")
    return {
        "allowed": not violations,
        "reason": "; ".join(violations) if violations else "within_scope",
        "proposal_nets": tuple(sorted(proposal_nets)),
        "proposal_devices": tuple(sorted(proposal_devices)),
        "proposal_regions": tuple(sorted(proposal_regions)),
        "proposal_bbox": proposal_bbox,
        "proposal_edit_count": proposal_edit_count,
        "scope_guard": scope_guard,
    }


def _repair_proposal_nets(proposal: object) -> set[str]:
    nets: set[str] = set()
    layout_patch = getattr(proposal, "layout_patch", None)
    if layout_patch is not None:
        nets.update(_layout_plan_nets_for_scope(layout_patch))
    selected = getattr(proposal, "selected_candidate", None)
    if selected is not None:
        nets.update(_repair_plan_nets_for_scope(getattr(selected, "plan", None)))
    metadata = dict(getattr(proposal, "metadata", {}) or {})
    nets.update(str(net) for net in metadata.get("scope_nets", ()) if str(net))
    nets.update(str(net) for net in metadata.get("hotspot_nets", ()) if str(net))
    nets.update(str(net) for net in getattr(proposal, "hotspot_nets", ()) if str(net))
    return {net for net in nets if net}


def _repair_proposal_devices(proposal: object) -> set[str]:
    devices: set[str] = set()
    layout_patch = getattr(proposal, "layout_patch", None)
    if layout_patch is not None:
        devices.update(_layout_plan_devices_for_scope(layout_patch))
    selected = getattr(proposal, "selected_candidate", None)
    if selected is not None:
        devices.update(_repair_plan_devices_for_scope(getattr(selected, "plan", None)))
    metadata = dict(getattr(proposal, "metadata", {}) or {})
    devices.update(str(device) for device in metadata.get("scope_devices", ()) if str(device))
    return {device for device in devices if device}


def _repair_proposal_regions(proposal: object) -> set[str]:
    regions: set[str] = set()
    layout_patch = getattr(proposal, "layout_patch", None)
    if layout_patch is not None:
        regions.update(_layout_plan_regions_for_scope(layout_patch))
    selected = getattr(proposal, "selected_candidate", None)
    if selected is not None:
        regions.update(_repair_plan_regions_for_scope(getattr(selected, "plan", None)))
    metadata = dict(getattr(proposal, "metadata", {}) or {})
    regions.update(str(region) for region in metadata.get("scope_regions", ()) if str(region))
    return {region for region in regions if region}


def _repair_proposal_bbox(proposal: object) -> tuple[float, float, float, float] | None:
    layout_patch = getattr(proposal, "layout_patch", None)
    boxes = []
    if layout_patch is not None:
        bbox = _layout_plan_bbox_for_scope(layout_patch)
        if bbox is not None:
            boxes.append(bbox)
    selected = getattr(proposal, "selected_candidate", None)
    if selected is not None:
        bbox = _repair_plan_bbox_for_scope(getattr(selected, "plan", None))
        if bbox is not None:
            boxes.append(bbox)
    return _bbox_union_many(tuple(boxes)) if boxes else None


def _repair_proposal_edit_count(proposal: object) -> int:
    selected = getattr(proposal, "selected_candidate", None)
    if selected is not None:
        return _repair_plan_edit_count_for_scope(getattr(selected, "plan", None))
    layout_patch = getattr(proposal, "layout_patch", None)
    if layout_patch is not None:
        return int(getattr(layout_patch, "metadata", {}).get("geometry_edit_count", 0) or 0)
    return 0


def _repair_plan_nets_for_scope(plan: object | None) -> set[str]:
    if plan is None:
        return set()
    layout_patch = getattr(plan, "layout_patch", None)
    if layout_patch is not None:
        return _layout_plan_nets_for_scope(layout_patch)
    oa_patch = getattr(plan, "oa_patch", None)
    if oa_patch is not None:
        return _oa_write_plan_nets_for_scope(oa_patch)
    replacement_layout = getattr(plan, "replacement_layout", None)
    if replacement_layout is not None:
        return _layout_plan_nets_for_scope(replacement_layout)
    replacement_oa_plan = getattr(plan, "replacement_oa_plan", None)
    if replacement_oa_plan is not None:
        return _oa_write_plan_nets_for_scope(replacement_oa_plan)
    edits = getattr(plan, "edits", None)
    if edits is not None:
        return {
            str(getattr(edit, "net", ""))
            for edit in edits
            if str(getattr(edit, "net", ""))
        }
    return set()


def _repair_plan_devices_for_scope(plan: object | None) -> set[str]:
    if plan is None:
        return set()
    layout_patch = getattr(plan, "layout_patch", None)
    if layout_patch is not None:
        return _layout_plan_devices_for_scope(layout_patch)
    replacement_layout = getattr(plan, "replacement_layout", None)
    if replacement_layout is not None:
        return _layout_plan_devices_for_scope(replacement_layout)
    metadata = dict(getattr(plan, "metadata", {}) or {})
    return {str(device) for device in metadata.get("scope_devices", ()) if str(device)}


def _repair_plan_regions_for_scope(plan: object | None) -> set[str]:
    if plan is None:
        return set()
    layout_patch = getattr(plan, "layout_patch", None)
    if layout_patch is not None:
        return _layout_plan_regions_for_scope(layout_patch)
    replacement_layout = getattr(plan, "replacement_layout", None)
    if replacement_layout is not None:
        return _layout_plan_regions_for_scope(replacement_layout)
    metadata = dict(getattr(plan, "metadata", {}) or {})
    return {str(region) for region in metadata.get("scope_regions", ()) if str(region)}


def _repair_plan_bbox_for_scope(plan: object | None) -> tuple[float, float, float, float] | None:
    if plan is None:
        return None
    layout_patch = getattr(plan, "layout_patch", None)
    if layout_patch is not None:
        return _layout_plan_bbox_for_scope(layout_patch)
    replacement_layout = getattr(plan, "replacement_layout", None)
    if replacement_layout is not None:
        return _layout_plan_bbox_for_scope(replacement_layout)
    return None


def _repair_plan_edit_count_for_scope(plan: object | None) -> int:
    if plan is None:
        return 0
    edits = getattr(plan, "edits", None)
    if edits is not None:
        return len(tuple(edits))
    layout_patch = getattr(plan, "layout_patch", None)
    if layout_patch is not None:
        return int(getattr(layout_patch, "metadata", {}).get("geometry_edit_count", 0) or 0)
    replacement_layout = getattr(plan, "replacement_layout", None)
    if replacement_layout is not None:
        return int(getattr(replacement_layout, "metadata", {}).get("geometry_edit_count", 0) or 0)
    return 0


def _layout_plan_nets_for_scope(plan: object) -> set[str]:
    nets = set(str(net) for net in getattr(plan, "nets", ()) if str(net))
    nets.update(str(getattr(rect, "net", "")) for rect in getattr(plan, "rects", ()) if str(getattr(rect, "net", "")))
    nets.update(str(getattr(path, "net", "")) for path in getattr(plan, "paths", ()) if str(getattr(path, "net", "")))
    nets.update(str(getattr(via, "net", "")) for via in getattr(plan, "vias", ()) if str(getattr(via, "net", "")))
    return {net for net in nets if net}


def _layout_plan_devices_for_scope(plan: object) -> set[str]:
    metadata = dict(getattr(plan, "metadata", {}) or {})
    devices = {str(device) for device in metadata.get("scope_devices", ()) if str(device)}
    for region in tuple(metadata.get("hierarchy_regions", ()) or ()):
        if not isinstance(region, Mapping):
            continue
        for device in tuple(region.get("devices", ()) or ()):
            if str(device):
                devices.add(str(device))
    for rect in getattr(plan, "rects", ()):
        devices.update(str(device) for device in getattr(rect, "metadata", {}).get("devices", ()) if str(device))
    for path in getattr(plan, "paths", ()):
        devices.update(str(device) for device in getattr(path, "metadata", {}).get("devices", ()) if str(device))
    for via in getattr(plan, "vias", ()):
        devices.update(str(device) for device in getattr(via, "metadata", {}).get("devices", ()) if str(device))
    return {device for device in devices if device}


def _layout_plan_regions_for_scope(plan: object) -> set[str]:
    metadata = dict(getattr(plan, "metadata", {}) or {})
    regions = {str(region) for region in metadata.get("scope_regions", ()) if str(region)}
    for region in tuple(metadata.get("hierarchy_regions", ()) or ()):
        if isinstance(region, Mapping):
            name = str(region.get("name", ""))
            if name:
                regions.add(name)
    for rect in getattr(plan, "rects", ()):
        regions.update(str(region) for region in getattr(rect, "metadata", {}).get("regions", ()) if str(region))
    for path in getattr(plan, "paths", ()):
        regions.update(str(region) for region in getattr(path, "metadata", {}).get("regions", ()) if str(region))
    for via in getattr(plan, "vias", ()):
        regions.update(str(region) for region in getattr(via, "metadata", {}).get("regions", ()) if str(region))
    return {region for region in regions if region}


def _layout_plan_bbox_for_scope(plan: object) -> tuple[float, float, float, float] | None:
    from analogskills.layout.ir import layout_plan_bbox

    if hasattr(plan, "cell"):
        return layout_plan_bbox(plan)
    return None


def _oa_write_plan_nets_for_scope(plan: object) -> set[str]:
    nets = set()
    nets.update(str(getattr(rect, "net", "")) for rect in getattr(plan, "rects", ()) if str(getattr(rect, "net", "")))
    nets.update(str(getattr(path, "net", "")) for path in getattr(plan, "paths", ()) if str(getattr(path, "net", "")))
    nets.update(str(getattr(via, "net", "")) for via in getattr(plan, "vias", ()) if str(getattr(via, "net", "")))
    return {net for net in nets if net}


def _coerce_bbox(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = value
        return (float(x0), float(y0), float(x1), float(y1))
    except (TypeError, ValueError):
        return None


def _bbox_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    *,
    tol: float = 1e-12,
) -> bool:
    return (
        outer[0] <= inner[0] + tol
        and outer[1] <= inner[1] + tol
        and outer[2] + tol >= inner[2]
        and outer[3] + tol >= inner[3]
    )


def _bbox_union_many(
    boxes: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float, float, float] | None:
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _dispatch_verification_repair_apply(
    plan: VerificationRepairExecutionPlan,
    proposal: object,
    backend: object,
) -> object:
    from analogskills.repair import apply_repair_proposal

    if hasattr(backend, "operations"):
        getattr(backend, "operations").append(
            (
                "dispatch_repair_target",
                (),
                {
                    "dispatch_mode": plan.dispatch_mode,
                    "writeback_level": plan.writeback_level,
                    "writeback_target": plan.writeback_target,
                    "rerun_levels": plan.rerun_levels,
                    "dispatch_plan": dict(plan.dispatch_plan),
                },
            )
        )
    return apply_repair_proposal(proposal, backend)


def _repair_proposal_source_cellview(proposal: object | None) -> dict[str, object]:
    if proposal is None:
        return {}
    selected = getattr(proposal, "selected_candidate", None)
    if selected is not None:
        plan = getattr(selected, "plan", None)
        cellview = _plan_cellview(plan)
        if cellview:
            return cellview
    layout_patch = getattr(proposal, "layout_patch", None)
    if layout_patch is not None:
        cellview = _plan_cellview(layout_patch)
        if cellview:
            return cellview
    oa_patch = getattr(proposal, "oa_patch", None)
    if oa_patch is not None:
        cellview = _plan_cellview(oa_patch)
        if cellview:
            return cellview
    return {}


def _dispatch_target_cellview(
    source_cellview: Mapping[str, object],
    writeback_level: str,
    writeback_target: str,
) -> dict[str, object]:
    if not source_cellview and not writeback_target:
        return {}
    target = dict(source_cellview)
    if writeback_target:
        target["cell"] = writeback_target
    if writeback_level == "cross_hierarchy":
        target["mode"] = "orchestrated"
    return target


def _resolve_dispatch_target_cellview(
    source_cellview: Mapping[str, object],
    *,
    writeback_level: str,
    writeback_target: str,
    hierarchy_database: tuple[HierarchyCellviewNode, ...],
) -> tuple[dict[str, object], dict[str, object]]:
    fallback_target = _dispatch_target_cellview(source_cellview, writeback_level, writeback_target)
    if not hierarchy_database:
        return fallback_target, {
            "mode": "fallback",
            "source_node": {},
            "target_node": {},
            "path": (),
        }
    source_node = _match_hierarchy_node(hierarchy_database, source_cellview.get("cell", ""))
    target_node = _match_hierarchy_node(hierarchy_database, writeback_target)
    if target_node is None and writeback_level == "parent" and source_node is not None and source_node.parent:
        target_node = _match_hierarchy_node(hierarchy_database, source_node.parent)
    target_cellview = dict(fallback_target)
    if target_node is not None:
        target_cellview.update(
            {
                "lib": target_node.lib,
                "cell": target_node.cell,
                "view": target_node.view,
                "view_type": target_node.view_type,
            }
        )
    if writeback_level == "cross_hierarchy":
        target_cellview["mode"] = "orchestrated"
    return target_cellview, {
        "mode": "hierarchy_database" if target_node is not None or source_node is not None else "fallback",
        "source_node": _hierarchy_node_to_dict(source_node),
        "target_node": _hierarchy_node_to_dict(target_node),
        "path": _hierarchy_path(source_node, target_node, hierarchy_database),
        "path_nodes": tuple(
            _hierarchy_node_to_dict(node)
            for node in _hierarchy_path_nodes(source_node, target_node, hierarchy_database)
        ),
    }


def _retarget_repair_proposal_for_dispatch(
    proposal: object,
    dispatch_plan: Mapping[str, object],
) -> object:
    target_cellview = dispatch_plan.get("target_cellview", {})
    if not isinstance(target_cellview, Mapping) or not target_cellview:
        return proposal
    source_cellview = dispatch_plan.get("source_cellview", {})
    if isinstance(source_cellview, Mapping) and dict(source_cellview) == dict(target_cellview):
        return proposal

    from analogskills.repair import (
        DrcRepairProposal,
        DrcReplacementPlan,
        LocalizedDrcPatchPlan,
        LvsRepairProposal,
        LvsShortReplacementPlan,
        PostLayoutEcoRepairProposal,
    )

    if isinstance(proposal, DrcRepairProposal):
        return _retarget_candidate_proposal(proposal, target_cellview)
    if isinstance(proposal, LvsRepairProposal):
        return _retarget_candidate_proposal(proposal, target_cellview)
    if isinstance(proposal, PostLayoutEcoRepairProposal):
        return replace(
            proposal,
            layout_patch=_retarget_layout_plan_cellview(proposal.layout_patch, target_cellview),
            oa_patch=_retarget_oa_write_plan_cellview(proposal.oa_patch, target_cellview),
        )
    if isinstance(proposal, (LocalizedDrcPatchPlan, DrcReplacementPlan, LvsShortReplacementPlan)):
        return _retarget_repair_plan_cellview(proposal, target_cellview)
    return proposal


def _retarget_candidate_proposal(proposal: object, target_cellview: Mapping[str, object]) -> object:
    selected = getattr(proposal, "selected_candidate", None)
    if selected is None:
        return proposal
    retargeted_selected = replace(
        selected,
        plan=_retarget_repair_plan_cellview(getattr(selected, "plan"), target_cellview),
    )
    candidates = tuple(
        retargeted_selected if candidate is selected else candidate
        for candidate in tuple(getattr(proposal, "candidates", ()) or ())
    )
    return replace(proposal, candidates=candidates, selected_candidate=retargeted_selected)


def _retarget_repair_plan_cellview(plan: object, target_cellview: Mapping[str, object]) -> object:
    from analogskills.repair import DrcReplacementPlan, LocalizedDrcPatchPlan, LvsShortReplacementPlan

    if isinstance(plan, LocalizedDrcPatchPlan):
        return replace(
            plan,
            layout_patch=_retarget_layout_plan_cellview(plan.layout_patch, target_cellview),
            oa_patch=_retarget_oa_write_plan_cellview(plan.oa_patch, target_cellview),
        )
    if isinstance(plan, DrcReplacementPlan):
        return replace(
            plan,
            replacement_layout=_retarget_layout_plan_cellview(plan.replacement_layout, target_cellview),
            replacement_oa_plan=_retarget_oa_write_plan_cellview(plan.replacement_oa_plan, target_cellview),
        )
    if isinstance(plan, LvsShortReplacementPlan):
        return replace(
            plan,
            replacement_layout=_retarget_layout_plan_cellview(plan.replacement_layout, target_cellview),
            replacement_oa_plan=_retarget_oa_write_plan_cellview(plan.replacement_oa_plan, target_cellview),
        )
    return plan


def _retarget_layout_plan_cellview(plan: object, target_cellview: Mapping[str, object]) -> object:
    cell = getattr(plan, "cell", None)
    if cell is None:
        return plan
    from analogskills.layout.ir import LayoutCellRef

    target = LayoutCellRef(
        str(target_cellview.get("lib", getattr(cell, "lib", "work"))),
        str(target_cellview.get("cell", getattr(cell, "cell", ""))),
        str(target_cellview.get("view", getattr(cell, "view", "layout"))),
        str(target_cellview.get("view_type", getattr(cell, "view_type", "maskLayout"))),
    )
    return replace(plan, cell=target)


def _retarget_oa_write_plan_cellview(plan: object, target_cellview: Mapping[str, object]) -> object:
    cellview = getattr(plan, "cellview", None)
    if cellview is None:
        return plan
    from analogskills.eda.oa import OaCellView

    target = OaCellView(
        str(target_cellview.get("lib", getattr(cellview, "lib", "work"))),
        str(target_cellview.get("cell", getattr(cellview, "cell", ""))),
        str(target_cellview.get("view", getattr(cellview, "view", "layout"))),
        str(target_cellview.get("view_type", getattr(cellview, "view_type", "maskLayout"))),
        str(target_cellview.get("mode", getattr(cellview, "mode", "w"))),
    )
    return replace(plan, cellview=target)


def _verification_repair_hierarchy_database(loop: VerificationClosureLoop) -> tuple[HierarchyCellviewNode, ...]:
    provenance = dict(loop.provenance or {})
    for key in ("hierarchy_database", "hierarchy_cellviews", "hierarchy_nodes"):
        if key in provenance:
            return _coerce_hierarchy_database(provenance.get(key))
    return ()


def _coerce_hierarchy_database(raw: object) -> tuple[HierarchyCellviewNode, ...]:
    if raw is None:
        return ()
    records = raw.get("nodes", ()) if isinstance(raw, Mapping) else raw
    nodes: list[HierarchyCellviewNode] = []
    for record in tuple(records or ()):
        if isinstance(record, HierarchyCellviewNode):
            nodes.append(record)
            continue
        if not isinstance(record, Mapping):
            continue
        aliases_raw = tuple(record.get("aliases", ()) or ())
        nodes.append(
            HierarchyCellviewNode(
                name=str(record.get("name", record.get("cell", ""))),
                lib=str(record.get("lib", "work")),
                cell=str(record.get("cell", "")),
                view=str(record.get("view", "layout")),
                view_type=str(record.get("view_type", "maskLayout")),
                parent=str(record.get("parent", "")),
                aliases=tuple(str(alias) for alias in aliases_raw if str(alias)),
                metadata=dict(record.get("metadata", {}) or {}),
            )
        )
    return tuple(nodes)


def _match_hierarchy_node(
    hierarchy_database: tuple[HierarchyCellviewNode, ...],
    name: object,
) -> HierarchyCellviewNode | None:
    key = str(name or "")
    if not key:
        return None
    for node in hierarchy_database:
        if key in {node.name, node.cell, *node.aliases}:
            return node
    return None


def _hierarchy_node_to_dict(node: HierarchyCellviewNode | None) -> dict[str, object]:
    if node is None:
        return {}
    return {
        "name": node.name,
        "lib": node.lib,
        "cell": node.cell,
        "view": node.view,
        "view_type": node.view_type,
        "parent": node.parent,
        "aliases": node.aliases,
        "metadata": dict(node.metadata),
    }


def _hierarchy_path(
    source_node: HierarchyCellviewNode | None,
    target_node: HierarchyCellviewNode | None,
    hierarchy_database: tuple[HierarchyCellviewNode, ...],
) -> tuple[str, ...]:
    if source_node is None and target_node is None:
        return ()
    if source_node is None:
        return (target_node.name,) if target_node is not None else ()
    if target_node is None:
        return (source_node.name,)
    lineage = {node.name: node for node in hierarchy_database}
    path = [source_node.name]
    seen = {source_node.name}
    current = source_node
    while current.parent and current.parent not in seen:
        path.append(current.parent)
        seen.add(current.parent)
        if current.parent == target_node.name:
            return tuple(path)
        current = lineage.get(current.parent)
        if current is None:
            break
    if target_node.name not in seen:
        path.append(target_node.name)
    return tuple(path)


def _hierarchy_path_nodes(
    source_node: HierarchyCellviewNode | None,
    target_node: HierarchyCellviewNode | None,
    hierarchy_database: tuple[HierarchyCellviewNode, ...],
) -> tuple[HierarchyCellviewNode, ...]:
    path = _hierarchy_path(source_node, target_node, hierarchy_database)
    if not path:
        return ()
    lineage = {node.name: node for node in hierarchy_database}
    return tuple(lineage[name] for name in path if name in lineage)


def _verification_repair_dispatch_steps(
    action: VerificationRepairAction,
    *,
    source_cellview: Mapping[str, object],
    target_cellview: Mapping[str, object],
    writeback_level: str,
    dispatch_mode: str,
) -> tuple[dict[str, object], ...]:
    repair_kind = action.selected_plan_kind or action.kind
    if dispatch_mode == "manual_orchestrated_apply":
        return (
            {
                "kind": "collect_hierarchy_context",
                "scope_level": writeback_level,
                "source_cellview": dict(source_cellview),
                "target_cellview": dict(target_cellview),
            },
            {
                "kind": "handoff_to_hierarchy_eco_orchestrator",
                "scope_level": writeback_level,
                "repair_kind": repair_kind,
                "target_cellview": dict(target_cellview),
            },
            {
                "kind": "await_manual_orchestrated_apply",
                "scope_level": writeback_level,
                "repair_kind": repair_kind,
            },
        )
    open_kind = "open_target_cellview"
    if dispatch_mode == "enclosing_level_apply":
        open_kind = "open_enclosing_cellview"
    elif dispatch_mode == "top_level_apply":
        open_kind = "open_top_level_cellview"
    return (
        {
            "kind": open_kind,
            "scope_level": writeback_level,
            "target_cellview": dict(target_cellview),
        },
        {
            "kind": "apply_repair_proposal",
            "scope_level": writeback_level,
            "repair_kind": repair_kind,
            "source_cellview": dict(source_cellview),
            "target_cellview": dict(target_cellview),
        },
        {
            "kind": "save_target_cellview",
            "scope_level": writeback_level,
            "target_cellview": dict(target_cellview),
        },
    )


def _verification_repair_verification_steps(
    rerun_levels: tuple[str, ...],
    *,
    source_cellview: Mapping[str, object],
    target_cellview: Mapping[str, object],
    writeback_level: str,
) -> tuple[dict[str, object], ...]:
    steps: list[dict[str, object]] = []
    for index, rerun_kind in enumerate(rerun_levels, start=1):
        target = source_cellview
        if any(
            token in rerun_kind
            for token in ("parent_level", "top_level", "enclosing_hierarchy")
        ):
            target = target_cellview or source_cellview
        steps.append(
            {
                "kind": "rerun_verification_stage",
                "order": index,
                "rerun_kind": rerun_kind,
                "scope_level": writeback_level,
                "target_cellview": dict(target),
            }
        )
    return tuple(steps)


def _plan_cellview(plan: object | None) -> dict[str, object]:
    if plan is None:
        return {}
    cellview = getattr(plan, "cellview", None)
    if cellview is not None:
        return {
            "lib": str(getattr(cellview, "lib", "")),
            "cell": str(getattr(cellview, "cell", "")),
            "view": str(getattr(cellview, "view", "")),
            "view_type": str(getattr(cellview, "view_type", "")),
            "mode": str(getattr(cellview, "mode", "")),
        }
    replacement_oa = getattr(plan, "replacement_oa_plan", None)
    if replacement_oa is not None:
        return _plan_cellview(replacement_oa)
    oa_patch = getattr(plan, "oa_patch", None)
    if oa_patch is not None:
        return _plan_cellview(oa_patch)
    layout_patch = getattr(plan, "layout_patch", None)
    if layout_patch is not None:
        return _plan_cellview(layout_patch)
    return {}


def _selected_repair_scope(proposal: Mapping[str, object]) -> dict[str, object]:
    candidates = tuple(proposal.get("candidates", ()) or ())
    selected_plan_kind = str(proposal.get("selected_plan_kind", ""))
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        if str(candidate.get("plan_kind", "")) != selected_plan_kind:
            continue
        scope = candidate.get("repair_scope", {})
        return dict(scope) if isinstance(scope, Mapping) else {}
    return {}


def _repair_scope_rank(scope: Mapping[str, object]) -> int:
    level = str(scope.get("level", ""))
    return {"leaf": 0, "parent": 1, "top": 2, "cross_hierarchy": 3}.get(level, 4)


def _closure_iteration_before_payload(payload: Mapping[str, object]) -> dict[str, object]:
    pairs = (
        ("scorecard", "before_scorecard"),
        ("drc_issues", "before_drc_issues"),
        ("lvs_issues", "before_lvs_issues"),
        ("run_summary", "before_run_summary"),
        ("pex", "before_pex"),
        ("drc_report", "before_drc_report"),
        ("lvs_report", "before_lvs_report"),
    )
    return {target: payload[source] for source, target in pairs if source in payload}


def _closure_iteration_after_payload(payload: Mapping[str, object]) -> dict[str, object]:
    pairs = (
        ("scorecard", "after_scorecard"),
        ("drc_issues", "after_drc_issues"),
        ("lvs_issues", "after_lvs_issues"),
        ("run_summary", "after_run_summary"),
        ("pex", "after_pex"),
        ("drc_report", "after_drc_report"),
        ("lvs_report", "after_lvs_report"),
    )
    return {target: payload[source] for source, target in pairs if source in payload}


def _closure_iteration_shared_payload(previous: Mapping[str, object], current: Mapping[str, object]) -> dict[str, object]:
    shared_keys = (
        "layout_plan",
        "pdk",
        "floorplan",
        "pin_label_report",
        "top_level_nets",
        "require_explicit_labels",
        "metric_targets",
        "metric_objectives",
        "critical_nets",
        "cap_limit_f",
        "res_limit_ohm",
        "allow_pex_growth",
        "require_after_passed",
        "block_on_critical_pex_regression",
        "block_on_any_pex_regression",
        "min_width_by_layer",
        "min_area_by_layer",
        "min_spacing_by_layer",
        "via_def_by_layer",
        "fixed_nets",
        "include_via_array_enclosures",
        "landing_margin_um",
        "max_candidates",
        "max_lvs_items",
        "provenance",
    )
    shared: dict[str, object] = {}
    for key in shared_keys:
        if key in current:
            shared[key] = current[key]
        elif key in previous:
            shared[key] = previous[key]
    return shared


def _pex_hotspot_score(
    cap_f: float,
    res_ohm: float,
    critical: bool,
    has_issue: bool,
    cap_limit_f: float | None,
    res_limit_ohm: float | None,
) -> float:
    cap_term = cap_f / cap_limit_f if cap_limit_f and cap_limit_f > 0 else cap_f * 1e15
    res_term = res_ohm / res_limit_ohm if res_limit_ohm and res_limit_ohm > 0 else res_ohm * 1e-3
    return cap_term + res_term + (2.0 if critical else 0.0) + (1.0 if has_issue else 0.0)


def _pex_hotspot_action(hotspot: PexHotspot) -> str:
    issue_text = " ".join(hotspot.issues).lower()
    has_cap_issue = "cap" in issue_text
    has_res_issue = "res" in issue_text
    if hotspot.critical and (hotspot.cap_f > 0.0 or hotspot.res_ohm > 0.0):
        return "review_route_topology_and_width_for_pex"
    if has_cap_issue and has_res_issue:
        return "review_route_topology_and_width_for_pex"
    if has_cap_issue:
        return "reduce_parasitic_cap_or_add_shielding"
    if has_res_issue:
        return "widen_or_shorten_resistive_route"
    return "review_pex_hotspot"


def _pex_delta_action(delta: PexHotspotDelta) -> str:
    issue_text = " ".join(delta.issues).lower()
    has_cap_issue = "cap" in issue_text
    has_res_issue = "res" in issue_text
    if delta.critical and delta.issues:
        return "review_critical_net_parasitic_regression"
    if "new extracted parasitic hotspot" in issue_text:
        return "review_new_pex_hotspot"
    if has_cap_issue and has_res_issue:
        return "review_route_topology_and_width_for_pex"
    if has_cap_issue:
        return "reduce_parasitic_cap_or_add_shielding"
    if has_res_issue:
        return "widen_or_shorten_resistive_route"
    return "review_pex_hotspot_regression"


def _pex_delta_priority(delta: PexHotspotDelta) -> int:
    priority = 65 if delta.critical else 50
    issue_text = " ".join(delta.issues).lower()
    if "new extracted parasitic hotspot" in issue_text:
        priority += 8
    if "crossed limit" in issue_text:
        priority += 10
    if delta.cap_delta_f > 0.0 and delta.res_delta_ohm > 0.0:
        priority += 5
    return min(priority + min(10, int(max(delta.score, 0.0))), 100)


def _pex_delta_improved(cap_delta_f: float, res_delta_ohm: float, cap_tol_f: float, res_tol_ohm: float) -> bool | None:
    cap_tol = max(float(cap_tol_f), 0.0)
    res_tol = max(float(res_tol_ohm), 0.0)
    if cap_delta_f > cap_tol or res_delta_ohm > res_tol:
        return False
    if cap_delta_f < -cap_tol or res_delta_ohm < -res_tol:
        return True
    return None


def _pex_delta_issues(
    net: str,
    before_cap_f: float,
    after_cap_f: float,
    cap_delta_f: float,
    before_res_ohm: float,
    after_res_ohm: float,
    res_delta_ohm: float,
    critical: bool,
    cap_limit_f: float | None,
    res_limit_ohm: float | None,
    cap_tol_f: float,
    res_tol_ohm: float,
) -> tuple[str, ...]:
    cap_tol = max(float(cap_tol_f), 0.0)
    res_tol = max(float(res_tol_ohm), 0.0)
    issues = []
    cap_regressed = cap_delta_f > cap_tol
    res_regressed = res_delta_ohm > res_tol
    if _pex_net_has_loading(before_cap_f, before_res_ohm, cap_tol, res_tol) is False and _pex_net_has_loading(after_cap_f, after_res_ohm, cap_tol, res_tol):
        issues.append("new extracted parasitic hotspot")
    if cap_regressed:
        issues.append(f"cap increased by {cap_delta_f:g}F")
    if res_regressed:
        issues.append(f"res increased by {res_delta_ohm:g}ohm")
    if cap_limit_f is not None and after_cap_f > cap_limit_f and before_cap_f <= cap_limit_f + cap_tol:
        issues.append(f"cap crossed limit {cap_limit_f:g}F")
    if res_limit_ohm is not None and after_res_ohm > res_limit_ohm and before_res_ohm <= res_limit_ohm + res_tol:
        issues.append(f"res crossed limit {res_limit_ohm:g}ohm")
    if critical and (cap_regressed or res_regressed):
        issues.append(f"critical net {net} parasitic loading increased")
    return tuple(issues)


def _pex_delta_score(
    cap_delta_f: float,
    res_delta_ohm: float,
    critical: bool,
    has_issue: bool,
    cap_limit_f: float | None,
    res_limit_ohm: float | None,
    cap_tol_f: float,
    res_tol_ohm: float,
) -> float:
    cap_regression = max(cap_delta_f - max(float(cap_tol_f), 0.0), 0.0)
    res_regression = max(res_delta_ohm - max(float(res_tol_ohm), 0.0), 0.0)
    cap_term = cap_regression / cap_limit_f if cap_limit_f and cap_limit_f > 0 else cap_regression * 1e15
    res_term = res_regression / res_limit_ohm if res_limit_ohm and res_limit_ohm > 0 else res_regression * 1e-3
    return cap_term + res_term + (2.0 if critical and has_issue else 0.0) + (1.0 if has_issue else 0.0)


def _pex_delta_was_new_hotspot(delta: PexHotspotDelta, cap_tol_f: float, res_tol_ohm: float) -> bool:
    cap_tol = max(float(cap_tol_f), 0.0)
    res_tol = max(float(res_tol_ohm), 0.0)
    return not _pex_net_has_loading(delta.before_cap_f, delta.before_res_ohm, cap_tol, res_tol) and _pex_net_has_loading(delta.after_cap_f, delta.after_res_ohm, cap_tol, res_tol)


def _pex_delta_was_cleared_hotspot(delta: PexHotspotDelta, cap_tol_f: float, res_tol_ohm: float) -> bool:
    cap_tol = max(float(cap_tol_f), 0.0)
    res_tol = max(float(res_tol_ohm), 0.0)
    return _pex_net_has_loading(delta.before_cap_f, delta.before_res_ohm, cap_tol, res_tol) and not _pex_net_has_loading(delta.after_cap_f, delta.after_res_ohm, cap_tol, res_tol)


def _pex_net_has_loading(cap_f: float, res_ohm: float, cap_tol_f: float, res_tol_ohm: float) -> bool:
    return cap_f > cap_tol_f or res_ohm > res_tol_ohm


def _pex_hotspot_comparison_summary(
    deltas: tuple[PexHotspotDelta, ...],
    new_hotspots: tuple[str, ...],
    cleared_hotspots: tuple[str, ...],
) -> tuple[str, ...]:
    summary = []
    if new_hotspots:
        summary.append(f"new PEX hotspot(s): {', '.join(new_hotspots)}")
    if cleared_hotspots:
        summary.append(f"cleared PEX hotspot(s): {', '.join(cleared_hotspots)}")
    for delta in deltas:
        if delta.improved is False:
            summary.append(f"PEX net {delta.net} regressed: cap {delta.cap_delta_f:+g}F, res {delta.res_delta_ohm:+g}ohm")
        elif delta.improved is True:
            summary.append(f"PEX net {delta.net} improved: cap {delta.cap_delta_f:+g}F, res {delta.res_delta_ohm:+g}ohm")
    return tuple(summary)


def _pex_hotspot_comparison_actions(deltas: tuple[PexHotspotDelta, ...]) -> tuple[str, ...]:
    actions = []
    issue_text = " ".join(" ".join(delta.issues).lower() for delta in deltas)
    if any(delta.critical and delta.issues for delta in deltas):
        actions.append("review_critical_net_parasitic_regression")
    if "new extracted parasitic hotspot" in issue_text:
        actions.append("review_new_pex_hotspots")
    if "cap" in issue_text:
        actions.append("reduce_parasitic_cap_or_add_shielding")
    if "res" in issue_text:
        actions.append("widen_or_shorten_resistive_route")
    if any(delta.issues for delta in deltas):
        actions.append("review_parasitic_hotspots")
    return tuple(dict.fromkeys(actions))


def _parse_pex_net_value(line: str, keywords: tuple[str, ...], units: tuple[str, ...]) -> tuple[str, float] | None:
    unit_pattern = "|".join(re.escape(unit) for unit in units)
    keyword_pattern = "|".join(re.escape(keyword) for keyword in keywords)
    match = re.search(
        rf"\b(?:{keyword_pattern})\b\s+(?P<net>[A-Za-z_]\w*)\s*(?:=|:)?\s*(?P<value>{_FLOAT_RE})\s*(?P<unit>{unit_pattern})?\b",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    value = float(match.group("value"))
    unit = match.group("unit") or ""
    return match.group("net"), _scale_pex_value(value, unit)


def _scale_pex_value(value: float, unit: str) -> float:
    lowered = unit.lower()
    if lowered == "ff":
        return value * 1e-15
    if lowered == "pf":
        return value * 1e-12
    return value


def _parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    nums = re.findall(_FLOAT_RE, value)
    if len(nums) != 4:
        return None
    return tuple(float(num) for num in nums)  # type: ignore[return-value]


def _parse_points(value: str | None) -> tuple[tuple[float, float], ...]:
    if not value:
        return ()
    nums = [float(num) for num in re.findall(_FLOAT_RE, value)]
    if len(nums) < 4 or len(nums) % 2:
        return ()
    return tuple((nums[idx], nums[idx + 1]) for idx in range(0, len(nums), 2))


def _points_bbox(points: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _is_bbox(value: object) -> bool:
    return isinstance(value, tuple) and len(value) == 4 and all(isinstance(coord, (int, float)) for coord in value)


def _normalise_calibre_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def _looks_like_new_inline_calibre_result(stripped: str) -> bool:
    if re.search(r"\b(?:rule|rulecheck)\s*[=:]\s*[\w.:-]+", stripped, flags=re.IGNORECASE):
        return True
    if not re.search(r"\b(?:bbox|box|extent|rect(?:angle)?|polygon|poly|points|vertices|layer)\s*[=:]", stripped, flags=re.IGNORECASE):
        return False
    first = stripped.split()[0] if stripped.split() else ""
    first = first.rstrip("=:").lower()
    if first in {"bbox", "box", "extent", "rect", "rectangle", "polygon", "poly", "points", "vertices", "layer", "cell", "inst", "instance", "path"}:
        return False
    return "." in first


def _inline_calibre_drc_result(stripped: str, *, current_rule: str, current_message: str) -> CalibreDrcResult | None:
    if not _looks_like_new_inline_calibre_result(stripped):
        return None
    rule = current_rule
    rule_match = re.search(r"\b(?:rule|rulecheck)\s*[=:]\s*([\w.:-]+)", stripped, flags=re.IGNORECASE)
    if rule_match:
        rule = rule_match.group(1)
    elif re.match(r"^[\w.:-]+\s+", stripped):
        head = stripped.split()[0]
        if "." in head or ":" in head:
            rule = head
    if not rule:
        return None
    layer = ""
    layer_match = re.search(r"\blayer\s*[=:]\s*([\w.:-]+)", stripped, flags=re.IGNORECASE)
    if layer_match:
        layer = layer_match.group(1)
    bbox = None
    bbox_match = re.search(r"\b(?:bbox|box|extent|rect(?:angle)?)\s*[=:]\s*(\([^)]*\)|\[[^]]*\]|[-+0-9eE.,\s]+)", stripped, flags=re.IGNORECASE)
    if bbox_match:
        bbox = _parse_bbox(bbox_match.group(1))
    polygon = ()
    poly_match = re.search(r"\b(?:polygon|poly|points|vertices)\s*[=:]\s*(.+)$", stripped, flags=re.IGNORECASE)
    if poly_match:
        polygon = _parse_points(poly_match.group(1))
        if bbox is None:
            bbox = _points_bbox(polygon)
    cell = ""
    cell_match = re.search(r"\bcell\s*[=:]\s*([\w.$:/-]+)", stripped, flags=re.IGNORECASE)
    if cell_match:
        cell = cell_match.group(1)
    instance = ""
    inst_match = re.search(r"\b(?:inst|instance|path)\s*[=:]\s*([\w.$:/-]+)", stripped, flags=re.IGNORECASE)
    if inst_match:
        instance = inst_match.group(1)
    message = current_message or f"Calibre DRC result for {rule}"
    return CalibreDrcResult(rule, layer or _rule_layer(rule), message, None, cell, instance, bbox, polygon, {})


def _drc_issue_from_calibre_result(result: CalibreDrcResult) -> DrcIssue:
    parts = [result.message or f"Calibre DRC result for {result.rule}"]
    if result.result_index is not None:
        parts.append(f"result={result.result_index}")
    if result.cell:
        parts.append(f"cell={result.cell}")
    if result.instance:
        parts.append(f"instance={result.instance}")
    if result.polygon:
        parts.append(f"polygon_points={len(result.polygon)}")
    return DrcIssue(result.rule, result.layer or _rule_layer(result.rule), "; ".join(parts), result.bbox)


def _parse_calibre_lvs_extraction_warnings(lines: list[str]) -> tuple[LvsIssue, ...]:
    issues: list[LvsIssue] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.upper().startswith("WARNING:"):
            continue
        block = [stripped]
        lookahead = index + 1
        while lookahead < len(lines) and lines[lookahead].strip():
            block.append(lines[lookahead].strip())
            lookahead += 1
        message = " ".join(block)
        upper = message.upper()
        if "DIRECT CONNECTION BETWEEN DIFFERENT PORTS" in upper or "SHORT CIRCUIT" in upper:
            issues.append(LvsIssue("short", message, _extract_net(message)))
        elif "UNATTACHED" in upper and ("LABEL" in upper or "PORT" in upper):
            issues.append(LvsIssue("warning", message, _extract_net(message)))
        elif "SOFT" in upper and ("CONNECT" in upper or "SUBSTRATE" in upper):
            issues.append(LvsIssue("warning", message, _extract_net(message)))
    return tuple(issues)


def _parse_calibre_lvs_overall_errors(lines: list[str]) -> tuple[LvsIssue, ...]:
    issues: list[LvsIssue] = []
    for line in lines:
        match = re.match(r"^\s*Error:\s*(?P<message>.+?)\s*$", line)
        if not match:
            continue
        message = f"Calibre LVS error: {match.group('message')}"
        upper = message.upper()
        kind = "short" if "SHORT" in upper or "CONNECTIVITY" in upper else "mismatch"
        issues.append(LvsIssue(kind, message, ""))
    return tuple(issues)


def _parse_calibre_lvs_count_mismatches(lines: list[str]) -> tuple[LvsIssue, ...]:
    section = _calibre_lvs_section_after_header(
        lines,
        "NUMBERS OF OBJECTS AFTER TRANSFORMATION",
        stop_headers=("INCORRECT OBJECTS", "INCORRECT NETS", "LVS PARAMETERS"),
    )
    if not section:
        section = _calibre_lvs_section_after_header(
            lines,
            "INITIAL NUMBERS OF OBJECTS",
            stop_headers=("NUMBERS OF OBJECTS AFTER TRANSFORMATION", "INCORRECT OBJECTS", "INCORRECT NETS", "LVS PARAMETERS"),
        )
    issues: list[LvsIssue] = []
    in_instance_rows = False
    for line in section:
        stripped = line.strip()
        if not stripped or stripped.startswith(("-", "*")) or stripped.upper().startswith(("LAYOUT", "SOURCE", "COMPONENT TYPE")):
            continue
        row = re.match(r"^(Ports|Nets|Total Inst):\s+(?P<layout>\d+)\s+(?P<source>\d+)\s*(?P<star>\*)?", stripped)
        if row:
            label = row.group(1)
            in_instance_rows = label != "Total Inst"
            issues.extend(_calibre_lvs_count_issue(label, "", row))
            continue
        row = re.match(r"^Instances:\s+(?P<layout>\d+)\s+(?P<source>\d+)\s*(?P<star>\*)?\s*(?P<component>.*)$", stripped)
        if row:
            in_instance_rows = True
            issues.extend(_calibre_lvs_count_issue("Instances", row.group("component").strip(), row))
            continue
        if in_instance_rows:
            row = re.match(r"^(?P<layout>\d+)\s+(?P<source>\d+)\s*(?P<star>\*)?\s*(?P<component>.+)$", stripped)
            if row:
                issues.extend(_calibre_lvs_count_issue("Instances", row.group("component").strip(), row))
    return tuple(issues)


def _calibre_lvs_count_issue(label: str, component: str, row: re.Match[str]) -> tuple[LvsIssue, ...]:
    layout = int(row.group("layout"))
    source = int(row.group("source"))
    star = bool(row.group("star"))
    if layout == source and not star:
        return ()
    subject = f"{label} {component}".strip()
    return (
        LvsIssue(
            "mismatch",
            f"Calibre transformed object count mismatch: {subject} layout={layout} source={source}",
            "",
        ),
    )


def _parse_calibre_lvs_incorrect_net_sections(lines: list[str]) -> tuple[LvsIssue, ...]:
    section = _calibre_lvs_section_after_header(
        lines,
        "INCORRECT NETS",
        stop_headers=("INCORRECT INSTANCES", "PROPERTY ERRORS", "LVS PARAMETERS"),
    )
    blocks = _calibre_lvs_disc_blocks(section)
    issues: list[LvsIssue] = []
    for block in blocks:
        header = next((line.strip() for line in block if re.match(r"^\d+\s+", line.strip())), "")
        if not header:
            continue
        layout_net = _extract_net(header)
        block_text = " ".join(line.strip() for line in block if line.strip())
        upper = block_text.upper()
        if "MISSING CONNECTION" in upper:
            for line in block:
                if "MISSING CONNECTION" in line.upper():
                    issues.append(LvsIssue("open", line.strip(), layout_net or _extract_net(line)))
        if "NO SIMILAR NET" in upper:
            issues.append(LvsIssue("mismatch", f"Calibre net has no similar counterpart: {block_text}", layout_net or _extract_net(block_text)))
        source_names = _calibre_lvs_source_net_names(block)
        if (
            layout_net
            and len(source_names) > 1
            and "MISSING CONNECTION" not in upper
            and "NO SIMILAR NET" not in upper
        ):
            peers = tuple(name for name in source_names if name != layout_net)
            peer_text = ", ".join(peers or source_names)
            issues.append(LvsIssue("short", f"Calibre net discrepancy: layout net {layout_net} corresponds to source net(s) {peer_text}", layout_net))
    return tuple(issues)


def _parse_calibre_lvs_incorrect_instance_sections(lines: list[str]) -> tuple[LvsIssue, ...]:
    section = _calibre_lvs_section_after_header(
        lines,
        "INCORRECT INSTANCES",
        stop_headers=("PROPERTY ERRORS", "INCORRECT NETS", "LVS PARAMETERS"),
    )
    blocks = _calibre_lvs_disc_blocks(section)
    issues: list[LvsIssue] = []
    for block in blocks:
        text = " ".join(line.strip() for line in block if line.strip())
        upper = text.upper()
        if not text:
            continue
        if "MISSING INSTANCE" in upper or "NO SIMILAR" in upper or "INCORRECT" in upper:
            issues.append(LvsIssue("mismatch", f"Calibre incorrect instance: {text}", _extract_net(text)))
    return tuple(issues)


def _parse_calibre_lvs_property_error_sections(lines: list[str]) -> tuple[LvsIssue, ...]:
    section = _calibre_lvs_section_after_header(
        lines,
        "PROPERTY ERRORS",
        stop_headers=("INCORRECT NETS", "INCORRECT INSTANCES", "LVS PARAMETERS"),
    )
    issues: list[LvsIssue] = []
    current = ""
    for line in section:
        stripped = line.strip()
        if not stripped or stripped.startswith(("-", "*")) or stripped.upper().startswith(("DISC#", "LAYOUT")):
            continue
        if re.match(r"^\d+\s+", stripped):
            current = stripped
            continue
        if current and "%" in stripped:
            message = f"Calibre property error: {current}; {stripped}"
            issues.append(LvsIssue("mismatch", message, _extract_net(message)))
    return tuple(issues)


def _calibre_lvs_section_after_header(lines: list[str], header: str, *, stop_headers: tuple[str, ...]) -> tuple[str, ...]:
    header_upper = _calibre_lvs_normalized_header(header)
    start = next((idx for idx, line in enumerate(lines) if _calibre_lvs_normalized_header(line) == header_upper), None)
    if start is None:
        return ()
    out: list[str] = []
    for line in lines[start + 1 :]:
        normalized = _calibre_lvs_normalized_header(line)
        if any(normalized == _calibre_lvs_normalized_header(stop) for stop in stop_headers):
            break
        out.append(line)
    return tuple(out)


def _calibre_lvs_normalized_header(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().upper())


def _calibre_lvs_disc_blocks(lines: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    blocks: list[tuple[str, ...]] = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("*") or stripped.upper().startswith(("DISC#", "LEGEND", "NE  =", "CIRCUIT,")):
            continue
        if set(stripped) == {"-"}:
            if current:
                blocks.append(tuple(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(tuple(current))
    return tuple(blocks)


def _calibre_lvs_source_net_names(block: tuple[str, ...]) -> tuple[str, ...]:
    names: list[str] = []
    ignored = {"CONNECTIONS", "ON", "THIS", "NET"}
    for line in block:
        right = line[65:].strip() if len(line) > 65 else ""
        upper = right.upper()
        if not right or right.startswith("---") or "**" in right or "CONNECTIONS ON THIS NET" in upper:
            continue
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.$:-]*", right)
        for token in tokens:
            base = token.split(":", 1)[0]
            if base.upper() in ignored:
                continue
            names.append(base)
            break
    return tuple(dict.fromkeys(names))


def _dedupe_lvs_issues(issues: list[LvsIssue]) -> tuple[LvsIssue, ...]:
    deduped: list[LvsIssue] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (issue.kind, issue.message, issue.net)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return tuple(deduped)


def _extract_net(line: str) -> str:
    match = re.search(r"(?:net|NET)\s+([A-Za-z_]\w*)", line)
    if match:
        return match.group(1)
    match = re.search(r"\b(?:Net|NET)\s*:\s*([A-Za-z_]\w*)", line)
    if match:
        return match.group(1)
    tokens = re.findall(r"[A-Za-z_]\w*", line)
    ignored = {
        "OPEN",
        "SHORT",
        "MISMATCH",
        "PROPERTY",
        "DIFFER",
        "DIFFERS",
        "INCORRECT",
        "CORRECT",
        "NET",
        "NOT",
        "COMPARED",
        "WARNING",
        "ERROR",
        "ERRORS",
        "BELOW",
        "SEE",
        "SOURCE",
        "LAYOUT",
        "SOFT",
        "CONNECT",
        "SUMMARY",
        "LVS",
    }
    for token in reversed(tokens):
        if token.upper() not in ignored:
            return token
    return ""


def _is_lvs_success_line(upper: str) -> bool:
    return bool(re.search(r"\bCORRECT\b", upper)) and "INCORRECT" not in upper and "NOT COMPARED" not in upper


def _lvs_section_name(upper: str) -> str:
    if "OPEN" in upper:
        return "open"
    if "SHORT" in upper:
        return "short"
    if "MISMATCH" in upper or "PROPERTY" in upper:
        return "mismatch"
    if "NOT COMPARED" in upper or "NOTCOMPARED" in upper:
        return "not_compared"
    if "INCORRECT" in upper:
        return "incorrect"
    return ""


def _looks_like_lvs_difference_detail(line: str) -> bool:
    upper = line.upper()
    if upper.startswith(("---", "***", "===", "LVS REPORT")):
        return False
    return bool(re.search(r"\b(?:NET|DEVICE|INSTANCE|PIN|TERMINAL|PROPERTY|SOURCE|LAYOUT)\b", upper))


def _rule_layer(rule: str) -> str:
    grid_match = re.match(r"^G\.\d+:(?P<layer>M\d+|VIA\d+|CO|OD|PO)i{0,2}$", rule, flags=re.IGNORECASE)
    if grid_match:
        return grid_match.group("layer").upper()
    token = rule.split(":", 1)[0].split(".", 1)[0].replace("_", ".")
    if token.startswith("DM") and token[2:].isdigit():
        return token
    layer_map = {
        "OD": "OD",
        "DOD": "DOD",
        "SR.DOD": "SRDOD",
        "PO": "PO",
        "DPO": "DPO",
        "SR.DPO": "SRDPO",
        "M1": "M1",
        "MOM": "MOM",
        "AP": "AP",
        "PMET": "PM",
        "PP": "PP",
        "NP": "NP",
        "NW": "NW",
    }
    if token.startswith("M") and token[1:].isdigit():
        return token
    return layer_map.get(token, token)


def _rule_category(rule: str) -> str:
    upper = rule.upper()
    if ".DN." in upper or upper.startswith(("DM", "DOD", "DPO", "SR_DOD", "SR_DPO", "SSD")):
        return "density_or_dummy"
    if ".EN." in upper or ".ENC" in upper:
        return "enclosure"
    if ".W." in upper:
        return "width"
    if ".S." in upper:
        return "spacing"
    if ".A." in upper or upper.startswith("AP."):
        return "area_or_antenna"
    if "MATCH" in upper:
        return "matching"
    if "ESD" in upper:
        return "esd_warning"
    if "WARN" in upper or "WARNING" in upper:
        return "warning"
    return "rulecheck"


def _suggested_drc_action(category: str) -> str:
    return {
        "density_or_dummy": "insert_density_or_dummy_fill",
        "enclosure": "grow_enclosure",
        "width": "widen_shape",
        "spacing": "move_or_reroute",
        "area_or_antenna": "check_area_or_add_antenna_protection",
        "matching": "review_matching_constraints",
        "esd_warning": "review_esd_topology",
        "warning": "manual_warning_review",
    }.get(category, "manual_drc_review")
