"""Calibre-marker driven local repair planning and convergence guards."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Iterable, Mapping

from .drc_lvs import LayoutShape


@dataclass(frozen=True)
class CalibreMarker:
    rule: str
    bbox: tuple[float, float, float, float]
    layer: str = ""
    result_index: int | None = None
    message: str = ""


@dataclass(frozen=True)
class MarkerOwnership:
    marker: CalibreMarker
    shape_ids: tuple[str, ...] = ()
    nets: tuple[str, ...] = ()
    confidence: float = 0.0


@dataclass(frozen=True)
class LocalRepairAction:
    kind: str
    marker: CalibreMarker
    target_shape_ids: tuple[str, ...] = ()
    params: Mapping[str, object] = field(default_factory=dict)
    requires_global_resolve: bool = False
    owner: str = "manual"


@dataclass(frozen=True)
class RepairIterationState:
    marker_counts: Mapping[str, int]
    geometry_fingerprint: str
    weighted_cost: int


@dataclass(frozen=True)
class MarkerRepairClassification:
    rule: str
    repair_class: str
    reason: str = ""
    owner: str = "manual"
    signoff_gated: bool = True


@dataclass(frozen=True)
class CalibreRuleTriage:
    """Root-cause queue entry for a Calibre result.

    ``domain`` identifies the generator that must be fixed before a later
    routing ECO is useful.  It is deliberately separate from ``repair_class``:
    the latter decides whether a geometry patch is safe, while this record
    controls diagnosis order.
    """

    rule: str
    domain: str
    priority: int
    owner: str
    action: str
    blocks_routing_eco: bool = False
    parameters: tuple[str, ...] = ()


_ACTION_BY_TOKEN = (
    (("G.1:",), "snap_to_grid"),
    (("G.4:",), "remove_short_jog"),
    (("ENC", "EX."), "increase_enclosure"),
    (("EOL",), "repair_eol"),
    (("NOTCH",), "fill_notch"),
    ((".A.", "AREA"), "add_min_area_patch"),
    ((".W.", "WIDTH"), "widen_shape"),
    ((".S.", "SPACE", "SPACING"), "push_or_reroute"),
    (("VIA", "CO."), "replace_via_template"),
)


_SIGNOFF_ONLY_RULE_FAMILIES = (
    "CSR.*",
    "*.DN.*",
    "*.DENSITY",
    "DENS*",
    "IND.DN.*",
    "M*.DN.*",
    "MX.DN.*",
    "AP.DN.*",
    "OD.DN.*",
    "PO.DN.*",
    "SR_DOD.DN.*",
    "SR_DPO.DN.*",
    "BDTCD.DN.*",
    "DTCD.DN.*",
    "ICOVL.*.DENSITY",
    "WITH_SEALRING_OPTION*",
    "IO_CONNECT_*",
    "DIODMY_*",
)
_WARNING_RULE_FAMILIES = ("*WARNING*",)
_PCELL_NATIVE_RULE_FAMILIES = (
    "SR_DPO.*",
    "SR_DOD.*",
    "DOD.*",
    "DPO.*",
    "NW.*",
    "DNW.*",
    "OD.*",
    "PO.*",
    "PP.*",
    "NP.*",
    "BJT.*",
    "CO.R.*",
)
_LOCAL_AUTO_RULE_FAMILIES = (
    "G.4*",
    "M*.W.4",
    "M*.A.*",
)
_LOCAL_SMT_RULE_FAMILIES = (
    "M*.S.*",
    "M*.EOL.*",
    "M*.NOTCH*",
    "VIA*.EN.*",
    "VIA*.S.*",
    "VIA*.W.*",
    "VIA*.R.*",
)


def classify_calibre_marker_for_local_repair(
    rule: str,
    *,
    config: Mapping[str, object] | None = None,
) -> MarkerRepairClassification:
    """Classify a Calibre DRC marker before local ECO.

    The class is intentionally about *repair ownership*, not about whether the
    foundry rule matters.  ``signoff_only`` markers still remain in the deck,
    but blind local geometry edits are not allowed to chase them.
    """

    upper = str(rule or "").upper().strip()
    cfg = dict(config or {})
    if not upper:
        return MarkerRepairClassification(str(rule or ""), "manual_review", "empty_rule", "manual", True)

    warning_families = _configured_rule_families(cfg, "warning_rule_families", _WARNING_RULE_FAMILIES)
    signoff_only_families = _configured_rule_families(cfg, "signoff_only_rule_families", _SIGNOFF_ONLY_RULE_FAMILIES)
    pcell_native_families = _configured_rule_families(cfg, "pcell_native_rule_families", _PCELL_NATIVE_RULE_FAMILIES)
    local_auto_families = _configured_rule_families(cfg, "local_auto_rule_families", _LOCAL_AUTO_RULE_FAMILIES)
    local_smt_families = _configured_rule_families(cfg, "local_smt_rule_families", _LOCAL_SMT_RULE_FAMILIES)

    if _matches_configured_family(upper, cfg.get("force_ignore_rule_families", ())):
        return MarkerRepairClassification(str(rule), "ignored", "forced_ignore_by_config", "manual", False)
    if _matches_configured_family(upper, cfg.get("force_local_auto_rule_families", ())):
        owner, gated = classify_calibre_rule(upper)
        return MarkerRepairClassification(str(rule), "local_auto_repair", "forced_local_auto_by_config", owner, gated)
    if _matches_configured_family(upper, cfg.get("force_local_smt_rule_families", ())):
        owner, gated = classify_calibre_rule(upper)
        return MarkerRepairClassification(str(rule), "local_smt_repair", "forced_local_smt_by_config", owner, gated)

    if _matches_any_rule_family(upper, warning_families) or "WARNING" in upper:
        return MarkerRepairClassification(str(rule), "ignored", "warning", "warning", False)
    if upper.startswith("G.1:"):
        return MarkerRepairClassification(str(rule), "signoff_only", "calibre_grid_or_stream_marker", "global", False)
    if _matches_any_rule_family(upper, signoff_only_families):
        return MarkerRepairClassification(str(rule), "signoff_only", "density_csr_or_fullchip_marker", "global", False)
    if _matches_any_rule_family(upper, pcell_native_families):
        return MarkerRepairClassification(str(rule), "pcell_or_native", "native_device_or_marker_layer_rule", "pcell", True)
    if _matches_configured_family(upper, cfg.get("candidate_rule_families", ())):
        owner, gated = classify_calibre_rule(upper)
        return MarkerRepairClassification(str(rule), "local_auto_repair", "configured_local_candidate", owner, gated)
    if _matches_any_rule_family(upper, local_auto_families):
        owner, gated = classify_calibre_rule(upper)
        return MarkerRepairClassification(str(rule), "local_auto_repair", "safe_same_net_local_candidate", owner, gated)
    if _matches_any_rule_family(upper, local_smt_families):
        owner, gated = classify_calibre_rule(upper)
        return MarkerRepairClassification(str(rule), "local_smt_repair", "requires_push_reroute_or_window_smt", owner, gated)

    owner, gated = classify_calibre_rule(upper)
    if owner == "routing":
        return MarkerRepairClassification(str(rule), "manual_review", "routing_rule_not_in_safe_auto_family", owner, gated)
    if owner == "pcell":
        return MarkerRepairClassification(str(rule), "pcell_or_native", "classified_by_legacy_pcell_owner", owner, gated)
    if owner == "global":
        return MarkerRepairClassification(str(rule), "signoff_only", "classified_by_legacy_global_owner", owner, gated)
    if owner == "warning":
        return MarkerRepairClassification(str(rule), "ignored", "classified_by_legacy_warning_owner", owner, False)
    return MarkerRepairClassification(str(rule), "manual_review", "unclassified_rule_family", owner, gated)


def classify_calibre_markers_for_local_repair(
    results: Iterable[object],
    *,
    config: Mapping[str, object] | None = None,
) -> tuple[MarkerRepairClassification, ...]:
    return tuple(
        classify_calibre_marker_for_local_repair(_rule_from_result(row), config=config)
        for row in results
    )


def summarize_calibre_marker_repair_classes(
    results: Iterable[object],
    *,
    config: Mapping[str, object] | None = None,
    top_n: int = 10,
) -> dict[str, object]:
    rows = tuple(results)
    classifications = classify_calibre_markers_for_local_repair(rows, config=config)
    class_counts = Counter(row.repair_class for row in classifications)
    reason_counts = Counter(f"{row.repair_class}:{row.reason}" for row in classifications)
    top_rules_by_class: dict[str, list[dict[str, object]]] = {}
    rule_counts_by_class: dict[str, Counter[str]] = {}
    for row, classification in zip(rows, classifications):
        rule = _rule_from_result(row)
        rule_counts_by_class.setdefault(classification.repair_class, Counter())[rule] += 1
    limit = max(int(top_n), 0)
    for repair_class, rule_counts in sorted(rule_counts_by_class.items()):
        top_rules_by_class[repair_class] = [
            {"rule": rule, "count": count}
            for rule, count in rule_counts.most_common(limit)
        ]
    return {
        "total": len(rows),
        "class_counts": dict(sorted(class_counts.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "top_rules_by_class": top_rules_by_class,
    }


def classify_calibre_rule_for_triage(
    result: object,
    *,
    pdk: object | None = None,
) -> CalibreRuleTriage:
    """Classify one result by root cause, using rule ID plus marker context.

    Metal spacing IDs are intentionally not called terminal-access errors from
    their rule ID alone.  They enter that bucket only when Calibre context says
    the marker touches PCell/terminal-access geometry.
    """

    rule = _rule_from_result(result)
    upper = rule.upper().strip()
    context = _result_context(result).upper()
    dummy_parameter_sources, marker_rules = _pdk_local_rule_sources(pdk)
    dummy_parameter_rules = tuple(dummy_parameter_sources)

    if _matches_any_rule_family(upper, dummy_parameter_rules):
        parameters = next(
            (
                tuple(str(item) for item in tuple(source.get("parameters", ()) or ()))
                for family, source in dummy_parameter_sources.items()
                if _matches_rule_family(upper, family)
            ),
            (),
        )
        return CalibreRuleTriage(rule, "pcell_dummy", 0, "pcell", "recalibrate_mos_dummy_cdf", True, parameters)
    if _matches_any_rule_family(upper, marker_rules):
        return CalibreRuleTriage(rule, "dummy_marker", 0, "pcell", "repair_required_dummy_marker", True)
    if _is_terminal_access_result(upper, context):
        return CalibreRuleTriage(rule, "terminal_access", 0, "pcell_access", "repair_terminal_access_template", True)

    repair = classify_calibre_marker_for_local_repair(rule)
    if repair.owner == "pcell":
        return CalibreRuleTriage(rule, "pcell_device", 0, "pcell", "recharacterize_native_pcell", True)
    if repair.owner == "routing":
        return CalibreRuleTriage(rule, "routing", 1, "routing", "run_local_routing_eco", False)
    if repair.owner == "global":
        return CalibreRuleTriage(rule, "global_signoff", 2, "global", "defer_to_global_signoff", False)
    if repair.owner == "warning":
        return CalibreRuleTriage(rule, "warning", 3, "warning", "record_warning", False)
    return CalibreRuleTriage(rule, "manual_review", 2, "manual", "inspect_rule_and_marker", False)


def summarize_calibre_rule_triage(
    results: Iterable[object],
    *,
    pdk: object | None = None,
) -> dict[str, object]:
    """Build an ordered, rule-ID aggregated DRC root-cause report."""

    rows = tuple(results)
    classified = tuple(classify_calibre_rule_for_triage(row, pdk=pdk) for row in rows)
    domain_counts = Counter(row.domain for row in classified)
    rule_counts: dict[tuple[int, str, str, str, str, bool, tuple[str, ...]], int] = Counter(
        (row.priority, row.domain, row.rule, row.owner, row.action, row.blocks_routing_eco, row.parameters)
        for row in classified
    )
    queue = [
        {
            "priority": priority,
            "domain": domain,
            "rule": rule,
            "count": count,
            "owner": owner,
            "action": action,
            "blocks_routing_eco": blocks,
            "parameters": list(parameters),
        }
        for (priority, domain, rule, owner, action, blocks, parameters), count in sorted(
            rule_counts.items(), key=lambda item: (item[0][0], item[0][1], -item[1], item[0][2])
        )
    ]
    blocking_count = sum(count for key, count in rule_counts.items() if key[-2])
    return {
        "total": len(rows),
        "domain_counts": dict(sorted(domain_counts.items())),
        "priority_blocking_count": blocking_count,
        "routing_eco_blocked": blocking_count > 0,
        "repair_queue": queue,
    }


def classify_calibre_rule(rule: str) -> tuple[str, bool]:
    """Return the responsible generator and whether the result is signoff-gated.

    This deliberately classifies rule *families*, not numerical limits.  Limits
    remain in the PDK/deck; the classification only decides whether a marker can
    be repaired by the detailed router, needs a PCell re-realization, or is a
    non-gating run warning.
    """

    upper = rule.upper()
    if "WARNING" in upper:
        return "warning", False
    if upper.startswith(("DENS", "DM.", "DFM.")):
        return "global", False
    if upper.startswith(("PMET.", "SR_DPO.", "SR_DOD.", "PO.", "PP.", "NP.", "NW.", "OD.")):
        return "pcell", True
    if upper.startswith(("G.1:", "G.4:", "M", "VIA", "CO.")):
        return "routing", True
    return "manual", True


def _matches_configured_family(rule: str, families: object) -> bool:
    if isinstance(families, str):
        rows = (families,)
    else:
        rows = tuple(families or ())
    return _matches_any_rule_family(rule, tuple(str(item) for item in rows if str(item)))


def _configured_rule_families(
    config: Mapping[str, object],
    key: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    if key not in config:
        return default
    return _tuple_config(config.get(key, ()))


def _tuple_config(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        rows = (value,)
    else:
        rows = tuple(value or ())
    return tuple(str(item) for item in rows if str(item))


def _rule_from_result(row: object) -> str:
    if isinstance(row, str):
        return row
    return str(getattr(row, "rule", ""))


def _result_context(row: object) -> str:
    if isinstance(row, str):
        return ""
    values = [
        getattr(row, "message", ""),
        getattr(row, "cell", ""),
        getattr(row, "instance", ""),
        getattr(row, "properties", ""),
    ]
    return " ".join(str(value) for value in values if value)


def _is_terminal_access_result(rule: str, context: str) -> bool:
    explicit = rule.startswith(("PCELL_ACCESS.", "LVS.SOURCE_TERMINAL_ACCESS", "LVS.DRAIN_TERMINAL_ACCESS", "LVS.GATE_TERMINAL_ACCESS"))
    contextual = any(token in context for token in ("PCELL_ACCESS", "TERMINAL_ACCESS", "CRN28_MOS_SOURCE", "CRN28_MOS_DRAIN", "CRN28_MOS_GATE"))
    return explicit or contextual


def _pdk_local_rule_sources(pdk: object | None) -> tuple[dict[str, Mapping[str, object]], tuple[str, ...]]:
    metadata = getattr(pdk, "metadata", {}) if pdk is not None else {}
    metadata = metadata if isinstance(metadata, Mapping) else {}
    sweep = metadata.get("pcell_drc_sweep", {})
    sweep = sweep if isinstance(sweep, Mapping) else {}
    mos = sweep.get("strongarm_mos", {})
    mos = mos if isinstance(mos, Mapping) else {}
    sources = mos.get("rule_parameter_sources", {})
    sources = sources if isinstance(sources, Mapping) else {}
    dummy_parameter_sources = {
        str(rule): source
        for rule, source in sources.items()
        if isinstance(source, Mapping)
    }

    required = metadata.get("required_marker_layers", {})
    required = required if isinstance(required, Mapping) else {}
    marker_rules: list[str] = []
    for marker in tuple(required.get("markers", ()) or ()):
        if not isinstance(marker, Mapping):
            continue
        marker_rules.extend(str(rule) for rule in tuple(marker.get("rule_ids", ()) or ()))
        for enclosure in tuple(marker.get("enclosures", ()) or ()):
            if isinstance(enclosure, Mapping):
                marker_rules.extend(str(rule) for rule in tuple(enclosure.get("rule_ids", ()) or ()))
    return dummy_parameter_sources, tuple(marker_rules)


def _matches_any_rule_family(rule: str, families: tuple[object, ...]) -> bool:
    upper = str(rule).upper()
    return any(_matches_rule_family(upper, str(family).upper()) for family in families)


def _matches_rule_family(rule: str, family: str) -> bool:
    family = family.strip().upper()
    if not family:
        return False
    if family in {"*", "*.*"}:
        return True
    if "*" in family:
        parts = family.split("*")
        pos = 0
        if not family.startswith("*"):
            first = parts[0]
            if not rule.startswith(first):
                return False
            pos = len(first)
            parts = parts[1:]
        for part in parts:
            if not part:
                continue
            idx = rule.find(part, pos)
            if idx < 0:
                return False
            pos = idx + len(part)
        if not family.endswith("*") and parts and parts[-1]:
            return rule.endswith(parts[-1])
        return True
    return rule == family or rule.startswith(family.rstrip("*"))


def markers_from_calibre_results(results: Iterable[object]) -> tuple[CalibreMarker, ...]:
    markers = []
    for row in results:
        bbox = getattr(row, "bbox", None)
        if bbox is None:
            continue
        markers.append(CalibreMarker(
            rule=str(getattr(row, "rule", "")), bbox=tuple(float(v) for v in bbox),
            layer=str(getattr(row, "layer", "")), result_index=getattr(row, "result_index", None),
            message=str(getattr(row, "message", "")),
        ))
    return tuple(markers)


def localize_calibre_markers(
    markers: Iterable[CalibreMarker], shapes: Iterable[LayoutShape], *, halo_um: float = 0.02,
) -> tuple[MarkerOwnership, ...]:
    shape_rows = tuple(shapes)
    localized = []
    for marker in markers:
        candidates: list[tuple[float, LayoutShape]] = []
        expanded = _expand(marker.bbox, max(halo_um, 0.0))
        for shape in shape_rows:
            if marker.layer and shape.layer and marker.layer != shape.layer:
                continue
            overlap = _intersection_area(expanded, shape.bbox)
            if overlap > 0:
                candidates.append((overlap, shape))
        candidates.sort(key=lambda item: (-item[0], item[1].id))
        owners = tuple(row[1] for row in candidates[:4])
        confidence = 0.0 if not owners else min(1.0, sum(row[0] for row in candidates[:4]) / max(_area(expanded), 1e-18))
        localized.append(MarkerOwnership(marker, tuple(row.id for row in owners), tuple(dict.fromkeys(row.net for row in owners if row.net)), confidence))
    return tuple(localized)


def plan_marker_repairs(ownership: Iterable[MarkerOwnership]) -> tuple[LocalRepairAction, ...]:
    actions = []
    for item in ownership:
        upper = item.marker.rule.upper()
        owner, signoff_gated = classify_calibre_rule(upper)
        kind = "recharacterize_pcell" if owner == "pcell" else "ignore_warning" if owner == "warning" else "manual_rule_handoff"
        if owner == "routing":
            if _is_numbered_metal_short_edge_width_rule(upper):
                kind = "remove_short_jog"
            else:
                for tokens, candidate_kind in _ACTION_BY_TOKEN:
                    if any(token in upper for token in tokens):
                        kind = candidate_kind
                        break
        global_resolve = owner in {"global", "manual"} or (signoff_gated and not item.shape_ids)
        actions.append(LocalRepairAction(kind, item.marker, item.shape_ids, {
            "nets": item.nets,
            "localization_confidence": item.confidence,
            "signoff_gated": signoff_gated,
        }, global_resolve, owner))
    return tuple(actions)


def _is_numbered_metal_short_edge_width_rule(rule: str) -> bool:
    """Calibre ``M*.W.4`` is a local 270/90/270 short-edge notch rule.

    Other metal width rules still map to ``widen_shape``; only this family is
    safe to treat as a same-net jog/notch fill candidate.
    """

    head = str(rule).upper().split(":", 1)[0]
    if not head.startswith("M") or not head.endswith(".W.4"):
        return False
    metal_number = head[1:].split(".", 1)[0]
    return metal_number.isdigit()


def build_repair_iteration_state(
    markers: Iterable[CalibreMarker], shapes: Iterable[LayoutShape], *, rule_weights: Mapping[str, int] | None = None,
) -> RepairIterationState:
    marker_rows, shape_rows = tuple(markers), tuple(shapes)
    counts = Counter(row.rule for row in marker_rows)
    weights = dict(rule_weights or {})
    cost = sum(count * max(1, int(weights.get(rule, 1))) for rule, count in counts.items())
    geometry = "|".join(f"{row.id}:{row.layer}:{','.join(f'{v:.9g}' for v in row.bbox)}:{row.net}" for row in sorted(shape_rows, key=lambda row: row.id))
    return RepairIterationState(dict(sorted(counts.items())), sha256(geometry.encode("utf-8")).hexdigest(), cost)


def build_repair_iteration_state_from_rule_counts(
    rule_counts: Mapping[str, int],
    *,
    geometry_fingerprint: str = "",
    rule_weights: Mapping[str, int] | None = None,
) -> RepairIterationState:
    """Build an ECO acceptance state from a Calibre rule-count summary.

    This is the right representation for post-Calibre iteration gating when
    the flow has already streamed out and parsed signoff results.  Geometry can
    still be supplied when available; otherwise the acceptance decision is based
    on weighted signoff marker cost only.
    """

    counts = {str(rule): max(int(count), 0) for rule, count in dict(rule_counts or {}).items() if str(rule)}
    weights = dict(rule_weights or {})
    cost = sum(count * max(1, int(weights.get(rule, 1))) for rule, count in counts.items())
    fingerprint = str(geometry_fingerprint or "")
    if not fingerprint:
        fingerprint = sha256(repr(tuple(sorted(counts.items()))).encode("utf-8")).hexdigest()
    return RepairIterationState(dict(sorted(counts.items())), fingerprint, cost)


def accept_repair_iteration(before: RepairIterationState, after: RepairIterationState, history: Iterable[RepairIterationState] = ()) -> tuple[bool, str]:
    seen = {(row.geometry_fingerprint, tuple(sorted(row.marker_counts.items()))) for row in history}
    signature = (after.geometry_fingerprint, tuple(sorted(after.marker_counts.items())))
    if signature in seen:
        return False, "repeated_state"
    if after.weighted_cost >= before.weighted_cost:
        return False, "drc_cost_not_reduced"
    return True, "drc_cost_reduced"


def _expand(box: tuple[float, float, float, float], halo: float) -> tuple[float, float, float, float]:
    return box[0] - halo, box[1] - halo, box[2] + halo, box[3] + halo


def _area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _intersection_area(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
