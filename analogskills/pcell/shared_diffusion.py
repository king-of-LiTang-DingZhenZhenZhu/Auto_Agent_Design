"""Calibration/readiness helpers for true shared-diffusion MOS candidates.

The main analog layout flow already supports *shared S/D intent* as a compact
placement objective.  That is not the same thing as emitting one drawn diffusion
region shared by two MOS devices.  This module keeps that boundary explicit:
Calibre evidence must prove the drawn/abutted template before SMT may treat a
candidate as a physical diffusion merge.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, is_dataclass, replace
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence


CONTEXT_RULE_NAMES = {
    "EFP_rules_are_OFF:WARNING",
    "IO_CONNECT_CORE_NET_VOLTAGE_IS_CORE:WARNING1",
    "FLIP_CHIP_WITHOUT_28K_AP:WARNING",
    "WITH_SEALRING_OPTION:WARNING",
    "DIODMY_L:WARNING",
    "MATCH.WARN.1",
    "DOD.R.1",
    "DPO.R.1",
    "SR_DOD.R.4",
    "SR_DPO.R.9",
    "SSD.DN.1",
    "MOM.R.2",
    "AP.DN.1",
    "ESD.WARN.1",
}

CONTEXT_RULE_PREFIXES = (
    "LUP.",
)

ACCESS_RULE_PREFIXES = (
    "CO.",
    "M1.",
    "VIA1.",
)

ACCESS_RULE_NAMES = {
    "G.1:CO",
    "G.1:M1i",
    "G.4:M1i",
    "PP.EN.1",
    "NP.EN.1",
}

MOS_TEMPLATE_RULE_PREFIXES = (
    "PO.",
    "SR_DPO.",
    "DPO.S",
    "DPO.EX",
    "DPO.W",
    "OD.",
    "RPO.",
    "PMET.",
)

MOS_TEMPLATE_RULE_NAMES = {
    "PP.S.9",
    "NP.S.9",
}


@dataclass(frozen=True)
class SharedDiffusionReadiness:
    """Machine-readable gate for physical shared-S/D MOS realization."""

    pdk: str = ""
    candidate: str = "abutted_mos_shared_sd"
    status: str = "not_evaluated"
    physical_diffusion_merge_allowed: bool = False
    solver_allowed_mode: str = "proximity_only"
    lvs_required: bool = False
    lvs_correct: bool | None = None
    total_results: int = 0
    rule_counts: Mapping[str, int] = field(default_factory=dict)
    rule_classes: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    evidence: Mapping[str, object] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "pdk": self.pdk,
            "candidate": self.candidate,
            "status": self.status,
            "physical_diffusion_merge_allowed": self.physical_diffusion_merge_allowed,
            "solver_allowed_mode": self.solver_allowed_mode,
            "lvs_required": self.lvs_required,
            "lvs_correct": self.lvs_correct,
            "total_results": self.total_results,
            "rule_counts": dict(self.rule_counts),
            "rule_classes": {key: dict(value) for key, value in self.rule_classes.items()},
            "evidence": dict(self.evidence),
            "notes": list(self.notes),
        }

    def save_json(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return out


def bind_shared_diffusion_pairs_to_oa_plan(
    plan: object,
    checks_or_pairs: Mapping[str, object] | Iterable[Mapping[str, object]],
    *,
    template_catalog: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[object, dict[str, object]]:
    """Replace selected shared-S/D native MOS pairs by calibrated pair masters.

    This is the conservative lowerer-side companion to the SMT readiness gate.
    It does not infer new shared-diffusion opportunities.  It consumes factual
    compiler rows from ``checks["mos_shared_sd_pairs"]`` and binds only rows
    that are both authorized and selected by SMT.

    The inserted instance uses the calibrated pair-master pin convention from
    the v14 probe:

    - ``DL``: left device non-shared S/D terminal
    - ``S``: shared center source/drain net
    - ``DR``: right device non-shared S/D terminal
    - ``G1``/``G2``: left/right gates
    - ``VSS``: common body/substrate net
    """

    template_catalog = {
        str(name): dict(value)
        for name, value in dict(template_catalog or {}).items()
        if isinstance(value, Mapping)
    }
    instances = tuple(getattr(plan, "instances", ()) or ())
    instance_by_name = {str(getattr(inst, "name", "") or ""): inst for inst in instances}
    raw_pair_rows = _shared_sd_pair_rows(checks_or_pairs)
    pair_rows = _expand_shared_sd_pair_rows_for_unit_instances(raw_pair_rows, instance_by_name)
    consumed: set[str] = set()
    inserted = []
    skipped: list[dict[str, object]] = []
    bound_rows: list[dict[str, object]] = []

    for row in pair_rows:
        name = str(row.get("name", "") or "")
        left_name = str(row.get("left", "") or "")
        right_name = str(row.get("right", "") or "")
        expansion_error = str(row.get("_shared_sd_expansion_error", "") or "")
        if expansion_error:
            skipped.append({"pair": name, "left": left_name, "right": right_name, "reason": expansion_error})
            continue
        if not _shared_sd_row_lowerable(row):
            skipped.append({"pair": name, "left": left_name, "right": right_name, "reason": "not_authorized_or_not_selected"})
            continue
        if not left_name or not right_name or left_name not in instance_by_name or right_name not in instance_by_name:
            skipped.append({"pair": name, "left": left_name, "right": right_name, "reason": "missing_instance"})
            continue
        if left_name in consumed or right_name in consumed:
            skipped.append({"pair": name, "left": left_name, "right": right_name, "reason": "instance_already_bound"})
            continue
        left = instance_by_name[left_name]
        right = instance_by_name[right_name]
        connections = _shared_sd_pair_master_connections(left, right, row)
        if not connections:
            skipped.append({"pair": name, "left": left_name, "right": right_name, "reason": "connection_map_unavailable"})
            continue
        template = _shared_sd_template_for_row(row, template_catalog)
        if not template:
            skipped.append({"pair": name, "left": left_name, "right": right_name, "reason": "template_unavailable"})
            continue
        param_mismatch = _shared_sd_template_param_mismatch(left, right, row, template)
        if param_mismatch:
            skipped.append(
                {
                    "pair": name,
                    "left": left_name,
                    "right": right_name,
                    "reason": "template_parameter_mismatch",
                    **param_mismatch,
                }
            )
            continue
        new_inst = _make_shared_sd_pair_instance(left, right, row, template, connections)
        inserted.append(new_inst)
        consumed.update((left_name, right_name))
        bound_rows.append(
            {
                "pair": name,
                "left": left_name,
                "right": right_name,
                "inserted_instance": str(getattr(new_inst, "name", "") or ""),
                "template_lib": str(template.get("lib", template.get("template_lib", "")) or ""),
                "template_cell": str(template.get("cell", template.get("template_cell", "")) or ""),
                "template_view": str(template.get("view", template.get("template_view", "layout")) or "layout"),
                "connections": dict(connections),
            }
        )

    kept = tuple(inst for inst in instances if str(getattr(inst, "name", "") or "") not in consumed)
    final_instances = kept + tuple(inserted)
    nets = _plan_nets_with_instance_connections(plan, final_instances)
    updated_plan = _replace_plan_instances_and_nets(plan, final_instances, nets)
    summary = {
        "input_pair_count": len(raw_pair_rows),
        "lowerer_pair_count": len(pair_rows),
        "expanded_unit_pair_count": max(len(pair_rows) - len(raw_pair_rows), 0),
        "bound_pair_count": len(bound_rows),
        "skipped_pair_count": len(skipped),
        "template_parameter_mismatch_count": sum(1 for row in skipped if row.get("reason") == "template_parameter_mismatch"),
        "required_template_calibrations": _shared_sd_required_template_calibrations(skipped),
        "removed_instances": tuple(sorted(consumed)),
        "inserted_instances": tuple(str(getattr(inst, "name", "") or "") for inst in inserted),
        "bound_pairs": tuple(bound_rows),
        "skipped_pairs": tuple(skipped),
        "physical_diffusion_merge_emitted": bool(bound_rows),
    }
    return updated_plan, summary


def drop_oa_geometry_for_instances(
    plan: object,
    instance_names: Iterable[object],
) -> tuple[object, dict[str, object]]:
    """Remove auxiliary OA geometry explicitly tagged as belonging to instances.

    Shared-S/D binding replaces the two original MOS instances by a calibrated
    pair master.  Any separately generated access scaffold for the consumed
    instances must be removed, otherwise the final top cell can retain stale
    "ghost" straps around devices that no longer exist.  This helper is
    deliberately conservative: it only drops objects whose ``metadata.instance``
    exactly matches a requested instance name.
    """

    targets = {str(name) for name in tuple(instance_names or ()) if str(name)}
    if not targets:
        return plan, {
            "target_instance_count": 0,
            "removed_rect_count": 0,
            "removed_path_count": 0,
            "removed_via_count": 0,
            "removed_pin_count": 0,
            "removed_label_count": 0,
        }

    rects, removed_rect_count = _drop_tagged_objects(tuple(getattr(plan, "rects", ()) or ()), targets)
    paths, removed_path_count = _drop_tagged_objects(tuple(getattr(plan, "paths", ()) or ()), targets)
    vias, removed_via_count = _drop_tagged_objects(tuple(getattr(plan, "vias", ()) or ()), targets)
    pins, removed_pin_count = _drop_tagged_objects(tuple(getattr(plan, "pins", ()) or ()), targets)
    labels = tuple(getattr(plan, "labels", ()) or ())
    updated = _replace_plan_geometry(plan, rects=rects, paths=paths, vias=vias, pins=pins, labels=labels)
    return updated, {
        "target_instance_count": len(targets),
        "target_instances": tuple(sorted(targets)),
        "removed_rect_count": removed_rect_count,
        "removed_path_count": removed_path_count,
        "removed_via_count": removed_via_count,
        "removed_pin_count": removed_pin_count,
        "removed_label_count": 0,
    }


def load_shared_diffusion_readiness(path: str | Path) -> SharedDiffusionReadiness:
    """Load a machine-readable shared-diffusion readiness contract."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError(f"shared-diffusion readiness JSON must be an object: {path}")
    return shared_diffusion_readiness_from_mapping(data)


def shared_diffusion_readiness_from_mapping(data: Mapping[str, object]) -> SharedDiffusionReadiness:
    """Normalize a mapping into :class:`SharedDiffusionReadiness`."""

    raw_lvs = data.get("lvs_correct", None)
    lvs_correct = raw_lvs if isinstance(raw_lvs, bool) or raw_lvs is None else _bool_like(raw_lvs)
    return SharedDiffusionReadiness(
        pdk=str(data.get("pdk", "") or ""),
        candidate=str(data.get("candidate", "abutted_mos_shared_sd") or "abutted_mos_shared_sd"),
        status=str(data.get("status", "not_evaluated") or "not_evaluated"),
        physical_diffusion_merge_allowed=bool(_bool_like(data.get("physical_diffusion_merge_allowed", False))),
        solver_allowed_mode=str(data.get("solver_allowed_mode", "proximity_only") or "proximity_only"),
        lvs_required=bool(_bool_like(data.get("lvs_required", False))),
        lvs_correct=lvs_correct,
        total_results=int(data.get("total_results", 0) or 0),
        rule_counts={str(key): int(value) for key, value in dict(data.get("rule_counts", {}) or {}).items()},
        rule_classes={
            str(key): {str(rule): int(count) for rule, count in dict(value or {}).items()}
            for key, value in dict(data.get("rule_classes", {}) or {}).items()
            if isinstance(value, Mapping)
        },
        evidence=dict(data.get("evidence", {}) or {}),
        notes=tuple(str(item) for item in tuple(data.get("notes", ()) or ())),
    )


def build_shared_diffusion_readiness(
    *,
    rule_counts: Mapping[str, int],
    total_results: int | None = None,
    pdk: str = "",
    candidate: str = "abutted_mos_shared_sd",
    evidence: Mapping[str, object] | None = None,
    lvs_correct: bool | None = None,
    require_lvs: bool = False,
) -> SharedDiffusionReadiness:
    """Classify one abutted-MOS probe result.

    ``rule_counts`` must include all nonzero Calibre rules, not only globally
    "actionable" ones: rules such as ``G.4:M1i`` are ignored in some isolated
    PCell summaries, but they are real access-geometry failures for a drawn
    shared-S/D generator.
    """

    counts = {str(name): int(count) for name, count in rule_counts.items() if int(count)}
    classes = classify_shared_diffusion_rule_counts(counts)
    access = classes["access"]
    template = classes["mos_template"]
    unknown = classes["unknown"]
    blocking_count = sum(access.values()) + sum(template.values()) + sum(unknown.values())
    if blocking_count == 0:
        status = "ready"
    elif not access and template:
        status = "access_clean_template_blocked"
    elif access:
        status = "access_blocked"
    else:
        status = "blocked_unknown_rules"
    if status == "ready" and require_lvs and lvs_correct is not True:
        status = "lvs_blocked" if lvs_correct is False else "lvs_not_verified"
    allowed = status == "ready"
    notes = _readiness_notes(status, classes)
    return SharedDiffusionReadiness(
        pdk=str(pdk),
        candidate=str(candidate),
        status=status,
        physical_diffusion_merge_allowed=allowed,
        solver_allowed_mode="physical_shared_diffusion" if allowed else "proximity_only",
        lvs_required=bool(require_lvs),
        lvs_correct=lvs_correct,
        total_results=sum(counts.values()) if total_results is None else int(total_results),
        rule_counts=dict(sorted(counts.items())),
        rule_classes={key: dict(sorted(value.items())) for key, value in sorted(classes.items())},
        evidence=dict(evidence or {}),
        notes=notes,
    )


def build_shared_diffusion_readiness_from_calibre_report(
    drc_summary_report: str | Path,
    *,
    pdk: str = "",
    candidate: str = "abutted_mos_shared_sd",
    evidence: Mapping[str, object] | None = None,
    lvs_correct: bool | None = None,
    require_lvs: bool = False,
) -> SharedDiffusionReadiness:
    """Parse a Calibre DRC summary and classify shared-diffusion readiness."""

    summary = _summarize_calibre_drc_report(drc_summary_report)
    rule_counts = dict(summary.get("rule_counts", {}) or {})
    merged_evidence = {
        "drc_summary_report": str(drc_summary_report),
        "calibre_summary_exists": bool(summary.get("exists", False)),
        "actionable_results_by_generic_pcell_filter": int(summary.get("actionable_results", 0) or 0),
        **dict(evidence or {}),
    }
    return build_shared_diffusion_readiness(
        rule_counts=rule_counts,
        total_results=int(summary.get("total_results", 0) or 0),
        pdk=pdk,
        candidate=candidate,
        evidence=merged_evidence,
        lvs_correct=lvs_correct,
        require_lvs=require_lvs,
    )


def classify_shared_diffusion_rule_counts(rule_counts: Mapping[str, int]) -> dict[str, dict[str, int]]:
    """Split Calibre rules into context, access, MOS-template, and unknown buckets."""

    result: dict[str, dict[str, int]] = {
        "context": {},
        "access": {},
        "mos_template": {},
        "unknown": {},
    }
    for raw_name, raw_count in rule_counts.items():
        name = str(raw_name).strip()
        count = int(raw_count)
        if count <= 0:
            continue
        result[_shared_diffusion_rule_class(name)][name] = count
    return result


def extract_gds_layer_counts(path: str | Path, layermap_path: str | Path | None = None) -> dict[str, object]:
    """Return layer/datatype counts for a GDS file.

    ``gdstk`` is optional at import time; callers running only unit tests can
    avoid this helper.  Counts include polygons and labels because both can be
    relevant when debugging PCell-marker streamout.
    """

    try:
        import gdstk  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - depends on local EDA env
        raise RuntimeError("gdstk is required to inspect GDS layer counts") from exc

    gds_path = Path(path)
    layer_names = _load_layermap(layermap_path) if layermap_path is not None else {}
    lib = gdstk.read_gds(str(gds_path))
    counts: dict[tuple[int, int], int] = {}
    for cell in lib.cells:
        for poly in cell.polygons:
            key = (int(poly.layer), int(poly.datatype))
            counts[key] = counts.get(key, 0) + 1
        for label in cell.labels:
            key = (int(label.layer), int(label.texttype))
            counts[key] = counts.get(key, 0) + 1
    rows = []
    for key, count in sorted(counts.items()):
        rows.append(
            {
                "layer": key[0],
                "datatype": key[1],
                "name": layer_names.get(key, ""),
                "count": count,
            }
        )
    return {"gds_path": str(gds_path), "layer_counts": rows}


def extract_gds_layer_geometry_summary(
    path: str | Path,
    layermap_path: str | Path | None = None,
    *,
    include_layer_names: Iterable[str] | None = None,
    max_examples_per_layer: int = 12,
    max_size_classes_per_layer: int = 12,
) -> dict[str, object]:
    """Return compact per-layer geometry metrics for agent/debug review.

    This is intentionally a summary, not a full GDS dump.  It is used to compare
    drawn shared-S/D templates against native PCell streamout: layer counts,
    aggregate bbox, common rectangle sizes, and a few representative bboxes are
    enough to spot wrong access offsets and missing template segmentation.
    """

    try:
        import gdstk  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - depends on local EDA env
        raise RuntimeError("gdstk is required to inspect GDS geometry") from exc

    gds_path = Path(path)
    layer_names = _load_layermap(layermap_path) if layermap_path is not None else {}
    included = {str(name) for name in include_layer_names} if include_layer_names is not None else None
    lib = gdstk.read_gds(str(gds_path))
    by_layer: dict[str, dict[str, object]] = {}
    for cell in lib.cells:
        for poly in cell.polygons:
            key = (int(poly.layer), int(poly.datatype))
            name = layer_names.get(key, f"{key[0]}/{key[1]}")
            if included is not None and name not in included:
                continue
            bbox_array = poly.bounding_box()
            if bbox_array is None:
                continue
            x0, y0 = (float(bbox_array[0][0]), float(bbox_array[0][1]))
            x1, y1 = (float(bbox_array[1][0]), float(bbox_array[1][1]))
            width = round(x1 - x0, 6)
            height = round(y1 - y0, 6)
            row = by_layer.setdefault(
                name,
                {
                    "layer": key[0],
                    "datatype": key[1],
                    "polygon_count": 0,
                    "bbox_um": [float("inf"), float("inf"), float("-inf"), float("-inf")],
                    "_sizes": Counter(),
                    "examples_um": [],
                },
            )
            row["polygon_count"] = int(row["polygon_count"]) + 1
            bbox = row["bbox_um"]
            assert isinstance(bbox, list)
            bbox[0] = min(float(bbox[0]), x0)
            bbox[1] = min(float(bbox[1]), y0)
            bbox[2] = max(float(bbox[2]), x1)
            bbox[3] = max(float(bbox[3]), y1)
            sizes = row["_sizes"]
            assert isinstance(sizes, Counter)
            sizes[(width, height)] += 1
            examples = row["examples_um"]
            assert isinstance(examples, list)
            if len(examples) < max(0, int(max_examples_per_layer)):
                examples.append([round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)])

    layers: list[dict[str, object]] = []
    for name, row in sorted(by_layer.items()):
        sizes = row.pop("_sizes")
        assert isinstance(sizes, Counter)
        bbox = row["bbox_um"]
        assert isinstance(bbox, list)
        row["bbox_um"] = [round(float(value), 4) for value in bbox]
        row["top_sizes_um"] = [
            {"width": width, "height": height, "count": count}
            for (width, height), count in sizes.most_common(max(0, int(max_size_classes_per_layer)))
        ]
        layers.append({"name": name, **row})
    return {"gds_path": str(gds_path), "layers": layers}


def write_shared_diffusion_readiness_markdown(readiness: SharedDiffusionReadiness, path: str | Path) -> Path:
    """Write a short reviewable shared-S/D readiness report."""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = readiness.to_dict()
    lines = [
        "# Shared-diffusion MOS realization readiness",
        "",
        f"- Candidate: `{readiness.candidate}`",
        f"- PDK: `{readiness.pdk}`",
        f"- Status: `{readiness.status}`",
        f"- SMT mode: `{readiness.solver_allowed_mode}`",
        f"- Physical diffusion merge allowed: `{str(readiness.physical_diffusion_merge_allowed).lower()}`",
        f"- LVS required: `{str(readiness.lvs_required).lower()}`",
        f"- LVS correct: `{readiness.lvs_correct}`",
        f"- Total Calibre results: {readiness.total_results}",
        "",
        "## Rule classes",
        "",
        "| class | result count | rules |",
        "|---|---:|---|",
    ]
    for klass in ("access", "mos_template", "unknown", "context"):
        rows = dict(readiness.rule_classes.get(klass, {}) or {})
        total = sum(int(v) for v in rows.values())
        rules = ", ".join(f"`{name}`={count}" for name, count in sorted(rows.items())) or "-"
        lines.append(f"| {klass} | {total} | {rules} |")
    lines.extend(["", "## Notes", ""])
    for note in readiness.notes:
        lines.append(f"- {note}")
    evidence = dict(data.get("evidence", {}) or {})
    if evidence:
        lines.extend(["", "## Evidence", "", "```json", json.dumps(evidence, indent=2, sort_keys=True), "```"])
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def _shared_diffusion_rule_class(rule: str) -> str:
    name = str(rule).strip()
    if not name:
        return "unknown"
    if name in CONTEXT_RULE_NAMES or ".DN." in name:
        return "context"
    if _starts_with_any(name, CONTEXT_RULE_PREFIXES):
        return "context"
    if re.match(r"^DM\d+\.R\.1$", name):
        return "context"
    if name in ACCESS_RULE_NAMES or _starts_with_any(name, ACCESS_RULE_PREFIXES):
        return "access"
    if name in MOS_TEMPLATE_RULE_NAMES or _starts_with_any(name, MOS_TEMPLATE_RULE_PREFIXES):
        return "mos_template"
    if ":WARNING" in name:
        return "context"
    return "unknown"


def _readiness_notes(status: str, classes: Mapping[str, Mapping[str, int]]) -> tuple[str, ...]:
    if status == "ready":
        return (
            "No shared-S/D blocking DRC rules remain in the probe summary.",
            "The layout solver may enable a physical shared-diffusion realization candidate for the covered template only.",
        )
    if status == "lvs_not_verified":
        return (
            "No shared-S/D blocking DRC rules remain, but LVS is required and has not been run.",
            "Keep this candidate out of main-flow physical shared-S/D mode until LVS is correct.",
        )
    if status == "lvs_blocked":
        return (
            "No shared-S/D blocking DRC rules remain, but LVS did not compare cleanly.",
            "Use the LVS report to repair terminal labels, connectivity, or source/extracted model parameters before enabling the candidate.",
        )
    access = dict(classes.get("access", {}) or {})
    template = dict(classes.get("mos_template", {}) or {})
    unknown = dict(classes.get("unknown", {}) or {})
    notes: list[str] = []
    if access:
        notes.append("Access/contact geometry is still DRC-dirty; keep SMT shared-S/D intent as proximity-only.")
    if template:
        notes.append("MOS template/dummy/marker rules are still DRC-dirty; calibrate against native PCell dummy/SRM_DPO/RPO structure before enabling physical merge.")
    if unknown:
        notes.append("Unknown non-context rules remain; classify them before enabling a physical shared-diffusion candidate.")
    if not notes:
        notes.append("Only context/full-chip rules remain, but readiness did not reach ready; inspect classification inputs.")
    return tuple(notes)


def _starts_with_any(value: str, prefixes: tuple[str, ...]) -> bool:
    return any(value.startswith(prefix) for prefix in prefixes)


def _shared_sd_pair_rows(checks_or_pairs: Mapping[str, object] | Iterable[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    if isinstance(checks_or_pairs, Mapping):
        raw = checks_or_pairs.get("mos_shared_sd_pairs", ())
    else:
        raw = checks_or_pairs
    return tuple(item for item in tuple(raw or ()) if isinstance(item, Mapping))


def _expand_shared_sd_pair_rows_for_unit_instances(
    pair_rows: tuple[Mapping[str, object], ...],
    instance_by_name: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    expanded: list[Mapping[str, object]] = []
    for row in pair_rows:
        left = str(row.get("left", "") or "")
        right = str(row.get("right", "") or "")
        if left in instance_by_name and right in instance_by_name:
            expanded.append(row)
            continue
        left_units = _shared_sd_unit_instance_names(left, instance_by_name)
        right_units = _shared_sd_unit_instance_names(right, instance_by_name)
        if not left_units and not right_units:
            expanded.append(row)
            continue
        if not left_units or not right_units or len(left_units) != len(right_units):
            clone = dict(row)
            clone["_shared_sd_expansion_error"] = "unit_instance_count_mismatch"
            clone["left_unit_count"] = len(left_units)
            clone["right_unit_count"] = len(right_units)
            expanded.append(clone)
            continue
        base_name = str(row.get("name", "") or f"{left}_{right}")
        for idx, (left_unit, right_unit) in enumerate(zip(left_units, right_units)):
            clone = dict(row)
            clone["name"] = f"{base_name}_u{idx}"
            clone["parent_pair"] = base_name
            clone["unit_pair_index"] = idx
            clone["left"] = left_unit
            clone["right"] = right_unit
            expanded.append(clone)
    return tuple(expanded)


def _shared_sd_unit_instance_names(logical_name: str, instance_by_name: Mapping[str, object]) -> tuple[str, ...]:
    if not logical_name:
        return ()
    pattern = re.compile(rf"^{re.escape(logical_name)}_u([0-9]+)$")
    rows: list[tuple[int, str]] = []
    for name in instance_by_name:
        match = pattern.match(str(name))
        if match:
            rows.append((int(match.group(1)), str(name)))
    return tuple(name for _, name in sorted(rows))


def _shared_sd_row_lowerable(row: Mapping[str, object]) -> bool:
    return (
        _bool_like(row.get("physical_diffusion_merge_authorized", False)) is True
        and _bool_like(row.get("physical_diffusion_merge_selected_by_smt", False)) is True
    )


def _shared_sd_pair_master_connections(left: object, right: object, row: Mapping[str, object]) -> dict[str, str]:
    left_conn = {str(key): str(value) for key, value in dict(getattr(left, "connections", {}) or {}).items()}
    right_conn = {str(key): str(value) for key, value in dict(getattr(right, "connections", {}) or {}).items()}
    left_shared = str(row.get("left_terminal", "") or "")
    right_shared = str(row.get("right_terminal", "") or "")
    if not left_shared or not right_shared:
        return {}
    left_other = _other_mos_sd_terminal(left_shared)
    right_other = _other_mos_sd_terminal(right_shared)
    if not left_other or not right_other:
        return {}
    shared_net = str(row.get("net", "") or left_conn.get(left_shared, "") or right_conn.get(right_shared, ""))
    body_left = left_conn.get("B", "")
    body_right = right_conn.get("B", "")
    if body_left and body_right and body_left != body_right:
        return {}
    body_net = body_left or body_right or shared_net
    required = {
        "DL": left_conn.get(left_other, ""),
        "S": shared_net,
        "DR": right_conn.get(right_other, ""),
        "G1": left_conn.get("G", ""),
        "G2": right_conn.get("G", ""),
        "VSS": body_net,
    }
    if any(not net for net in required.values()):
        return {}
    return required


def _other_mos_sd_terminal(terminal: str) -> str:
    text = str(terminal).upper()
    if text == "S":
        return "D"
    if text == "D":
        return "S"
    return ""


def _shared_sd_template_for_row(
    row: Mapping[str, object],
    template_catalog: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    candidate = str(row.get("shared_sd_readiness_candidate", row.get("candidate", "")) or "")
    catalog_row = dict(template_catalog.get(candidate, {}))
    lib = str(
        row.get(
            "shared_sd_template_lib",
            row.get("template_lib", catalog_row.get("lib", catalog_row.get("template_lib", ""))),
        )
        or ""
    )
    cell = str(
        row.get(
            "shared_sd_template_cell",
            row.get("template_cell", catalog_row.get("cell", catalog_row.get("template_cell", ""))),
        )
        or ""
    )
    view = str(
        row.get(
            "shared_sd_template_view",
            row.get("template_view", catalog_row.get("view", catalog_row.get("template_view", "layout"))),
        )
        or "layout"
    )
    if not lib or not cell:
        return {}
    result = {
        "candidate": candidate,
        "lib": lib,
        "cell": cell,
        "view": view,
        **catalog_row,
    }
    terminal_access = _shared_sd_template_terminal_access(row, result)
    if terminal_access:
        result["terminal_access"] = terminal_access
    for row_key, template_key in (
        ("shared_sd_layout_bbox_um", "layout_bbox_um"),
        ("shared_sd_layout_width_um", "layout_width_um"),
        ("shared_sd_layout_height_um", "layout_height_um"),
        ("shared_sd_layout_bbox_x0_um", "layout_bbox_x0_um"),
        ("shared_sd_layout_bbox_y0_um", "layout_bbox_y0_um"),
    ):
        value = row.get(row_key, None)
        if value is not None:
            result[template_key] = value
    return result


def _shared_sd_template_terminal_access(
    row: Mapping[str, object],
    template: Mapping[str, object],
) -> dict[str, object]:
    for key in (
        "shared_sd_terminal_access",
        "terminal_access",
        "template_terminal_access",
    ):
        value = row.get(key, template.get(key, None))
        if isinstance(value, Mapping):
            return {str(term): dict(entry) if isinstance(entry, Mapping) else entry for term, entry in value.items()}
    return {}


def _shared_sd_template_param_mismatch(
    left: object,
    right: object,
    row: Mapping[str, object],
    template: Mapping[str, object],
) -> dict[str, object]:
    constraints = _shared_sd_template_instance_params(row, template)
    if not constraints:
        return {}
    left_matches = _shared_sd_instance_matches_params(left, constraints)
    right_matches = _shared_sd_instance_matches_params(right, constraints)
    if left_matches and right_matches:
        return {}
    left_params = _shared_sd_relevant_instance_params(left, constraints)
    right_params = _shared_sd_relevant_instance_params(right, constraints)
    return {
        "candidate": str(template.get("candidate", row.get("shared_sd_readiness_candidate", "")) or ""),
        "template_lib": str(template.get("lib", "")),
        "template_cell": str(template.get("cell", "")),
        "template_view": str(template.get("view", "layout") or "layout"),
        "expected_instance_params": dict(constraints),
        "left_cell": str(getattr(left, "cell", "") or ""),
        "right_cell": str(getattr(right, "cell", "") or ""),
        "left_instance_params": left_params,
        "right_instance_params": right_params,
    }


def _shared_sd_template_instance_params(row: Mapping[str, object], template: Mapping[str, object]) -> dict[str, object]:
    for key in (
        "shared_sd_template_instance_params",
        "compatible_instance_params",
        "template_instance_params",
        "instance_params",
    ):
        value = row.get(key, template.get(key, None))
        if isinstance(value, Mapping):
            return {str(k): v for k, v in value.items()}
    return {}


def _shared_sd_instance_matches_params(inst: object, constraints: Mapping[str, object]) -> bool:
    params = {str(key): value for key, value in dict(getattr(inst, "params", {}) or {}).items()}
    for key, expected in constraints.items():
        actual = _shared_sd_lookup_param(params, key)
        if actual is None:
            return False
        if not _shared_sd_param_value_matches(actual, expected):
            return False
    return True


def _shared_sd_relevant_instance_params(inst: object, constraints: Mapping[str, object]) -> dict[str, object]:
    params = {str(key): value for key, value in dict(getattr(inst, "params", {}) or {}).items()}
    result: dict[str, object] = {}
    for key in constraints:
        actual = _shared_sd_lookup_param(params, key)
        result[str(key)] = actual
    return result


def _shared_sd_required_template_calibrations(skipped: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    grouped: dict[str, dict[str, object]] = {}
    for row in skipped:
        if row.get("reason") != "template_parameter_mismatch":
            continue
        actual = row.get("left_instance_params", {})
        right_actual = row.get("right_instance_params", {})
        if isinstance(right_actual, Mapping) and dict(right_actual) != dict(actual if isinstance(actual, Mapping) else {}):
            actual_payload: object = {"left": dict(actual if isinstance(actual, Mapping) else {}), "right": dict(right_actual)}
        else:
            actual_payload = dict(actual if isinstance(actual, Mapping) else {})
        payload = {
            "logical": "nmos",
            "topology": "two_gate_shared_source",
            "left_cell": str(row.get("left_cell", "") or ""),
            "right_cell": str(row.get("right_cell", "") or ""),
            "actual_instance_params": actual_payload,
            "expected_instance_params": dict(row.get("expected_instance_params", {}) or {}),
            "blocked_pairs": [],
        }
        key = json.dumps(payload, sort_keys=True, default=str)
        item = grouped.setdefault(key, payload)
        blocked = item.setdefault("blocked_pairs", [])
        if isinstance(blocked, list):
            blocked.append(str(row.get("pair", "") or ""))
    result = []
    for item in grouped.values():
        blocked = tuple(str(name) for name in tuple(item.get("blocked_pairs", ()) or ()) if str(name))
        result.append({**item, "blocked_pair_count": len(blocked), "blocked_pairs": blocked})
    return tuple(result)


def _shared_sd_lookup_param(params: Mapping[str, object], key: str) -> object | None:
    aliases = {
        "l": ("l", "L", "length"),
        "L": ("L", "l", "length"),
        "fingers": ("fingers", "nf"),
        "nf": ("nf", "fingers"),
        "simM": ("simM", "m", "M"),
        "m": ("m", "M", "simM"),
        "M": ("M", "m", "simM"),
        "Wfg": ("Wfg", "wfg"),
    }
    for candidate in aliases.get(str(key), (str(key),)):
        if candidate in params:
            return params[candidate]
    return None


def _shared_sd_param_value_matches(actual: object, expected: object) -> bool:
    actual_num = _shared_sd_float(actual)
    expected_num = _shared_sd_float(expected)
    if actual_num is not None and expected_num is not None:
        return abs(actual_num - expected_num) <= max(1e-12, abs(expected_num) * 1e-6)
    return str(actual).strip() == str(expected).strip()


def _shared_sd_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _make_shared_sd_pair_instance(
    left: object,
    right: object,
    row: Mapping[str, object],
    template: Mapping[str, object],
    connections: Mapping[str, str],
) -> object:
    left_name = str(getattr(left, "name", row.get("left", "")) or row.get("left", "") or "")
    right_name = str(getattr(right, "name", row.get("right", "")) or row.get("right", "") or "")
    pair_name = str(row.get("name", "") or f"{left_name}_{right_name}")
    xy = _shared_sd_pair_instance_xy(left, right)
    orient = str(getattr(left, "orient", "") or "R0")
    params = {
        "shared_sd_candidate": str(template.get("candidate", row.get("shared_sd_readiness_candidate", "")) or ""),
        "shared_sd_pair": pair_name,
        "shared_sd_left_device": left_name,
        "shared_sd_right_device": right_name,
        "shared_sd_source": str(row.get("shared_sd_readiness_source", "") or ""),
        "shared_sd_readiness_artifact": str(row.get("shared_sd_readiness_artifact", "") or ""),
        "physical_diffusion_merge_emitted": True,
    }
    terminal_access = _shared_sd_template_terminal_access(row, template)
    metadata: dict[str, object] = {
        "shared_sd_candidate": params["shared_sd_candidate"],
        "shared_sd_pair": pair_name,
        "shared_sd_left_device": left_name,
        "shared_sd_right_device": right_name,
        "physical_diffusion_merge_emitted": True,
    }
    if terminal_access:
        metadata["logical_name"] = str(template.get("logical_name", "shared_nmos_pair") or "shared_nmos_pair")
        metadata["terminal_access"] = terminal_access
        metadata["shared_sd_terminal_access"] = terminal_access
    metadata.update(_shared_sd_template_bbox_metadata(template))
    instance_name = f"{pair_name}__shared_sd"
    inst_type = type(left)
    try:
        return inst_type(
            name=instance_name,
            lib=str(template.get("lib", "")),
            cell=str(template.get("cell", "")),
            view=str(template.get("view", "layout") or "layout"),
            xy=xy,
            orient=orient,
            connections=dict(connections),
            params=params,
            instantiation_method="dbCreateInstByMasterName",
            metadata=metadata,
        )
    except TypeError:
        updates = {
            "name": instance_name,
            "lib": str(template.get("lib", "")),
            "cell": str(template.get("cell", "")),
            "view": str(template.get("view", "layout") or "layout"),
            "xy": xy,
            "orient": orient,
            "connections": dict(connections),
            "params": params,
            "instantiation_method": "dbCreateInstByMasterName",
        }
        try:
            return replace(left, **updates, metadata=metadata)
        except TypeError:
            return replace(left, **updates)


def _shared_sd_template_bbox_metadata(template: Mapping[str, object]) -> dict[str, object]:
    for key in ("layout_bbox_um", "bbox_um", "bbox"):
        value = template.get(key)
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                x0, y0, x1, y1 = (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
            except (TypeError, ValueError):
                continue
            return {
                "bbox_x0_um": x0,
                "bbox_y0_um": y0,
                "width_um": max(0.0, x1 - x0),
                "height_um": max(0.0, y1 - y0),
            }
    result: dict[str, object] = {}
    for source, target in (
        ("layout_width_um", "width_um"),
        ("width_um", "width_um"),
        ("layout_height_um", "height_um"),
        ("height_um", "height_um"),
        ("layout_bbox_x0_um", "bbox_x0_um"),
        ("bbox_x0_um", "bbox_x0_um"),
        ("layout_bbox_y0_um", "bbox_y0_um"),
        ("bbox_y0_um", "bbox_y0_um"),
    ):
        if source in template:
            result[target] = template[source]
    return result


def _shared_sd_pair_instance_xy(left: object, right: object) -> tuple[float, float]:
    left_xy = tuple(getattr(left, "xy", getattr(left, "xy_um", (0.0, 0.0))) or (0.0, 0.0))
    right_xy = tuple(getattr(right, "xy", getattr(right, "xy_um", (0.0, 0.0))) or (0.0, 0.0))
    try:
        return (min(float(left_xy[0]), float(right_xy[0])), min(float(left_xy[1]), float(right_xy[1])))
    except (TypeError, ValueError, IndexError):
        return (0.0, 0.0)


def _plan_nets_with_instance_connections(plan: object, instances: Iterable[object]) -> tuple[str, ...]:
    nets = [str(net) for net in tuple(getattr(plan, "nets", ()) or ()) if str(net)]
    for inst in instances:
        for net in dict(getattr(inst, "connections", {}) or {}).values():
            text = str(net)
            if text and text not in nets:
                nets.append(text)
    return tuple(nets)


def _replace_plan_instances_and_nets(plan: object, instances: tuple[object, ...], nets: tuple[str, ...]) -> object:
    if is_dataclass(plan):
        return replace(plan, instances=instances, nets=nets)
    try:
        return type(plan)(
            cellview=getattr(plan, "cellview", None),
            nets=nets,
            pins=tuple(getattr(plan, "pins", ()) or ()),
            instances=instances,
            rects=tuple(getattr(plan, "rects", ()) or ()),
            labels=tuple(getattr(plan, "labels", ()) or ()),
            paths=tuple(getattr(plan, "paths", ()) or ()),
            vias=tuple(getattr(plan, "vias", ()) or ()),
        )
    except TypeError:
        setattr(plan, "instances", instances)
        setattr(plan, "nets", nets)
        return plan


def _drop_tagged_objects(objects: tuple[object, ...], instance_names: set[str]) -> tuple[tuple[object, ...], int]:
    kept = []
    removed = 0
    for item in objects:
        if _tagged_instance_name(item) in instance_names:
            removed += 1
            continue
        kept.append(item)
    return tuple(kept), removed


def _tagged_instance_name(item: object) -> str:
    metadata = getattr(item, "metadata", None)
    if isinstance(metadata, Mapping):
        return str(metadata.get("instance", "") or metadata.get("source_instance", "") or "")
    return ""


def _replace_plan_geometry(
    plan: object,
    *,
    rects: tuple[object, ...],
    paths: tuple[object, ...],
    vias: tuple[object, ...],
    pins: tuple[object, ...],
    labels: tuple[object, ...],
) -> object:
    if is_dataclass(plan):
        return replace(plan, rects=rects, paths=paths, vias=vias, pins=pins, labels=labels)
    try:
        return type(plan)(
            cellview=getattr(plan, "cellview", None),
            nets=tuple(getattr(plan, "nets", ()) or ()),
            pins=pins,
            instances=tuple(getattr(plan, "instances", ()) or ()),
            rects=rects,
            labels=labels,
            paths=paths,
            vias=vias,
        )
    except TypeError:
        setattr(plan, "rects", rects)
        setattr(plan, "paths", paths)
        setattr(plan, "vias", vias)
        setattr(plan, "pins", pins)
        setattr(plan, "labels", labels)
        return plan


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


def _load_layermap(path: str | Path | None) -> dict[tuple[int, int], str]:
    if path is None:
        return {}
    result: dict[tuple[int, int], str] = {}
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[2].isdigit() or not parts[3].isdigit():
            continue
        result[(int(parts[2]), int(parts[3]))] = f"{parts[0]}/{parts[1]}"
    return result


def _summarize_calibre_drc_report(path: str | Path) -> dict[str, object]:
    path_obj = Path(path)
    if not path_obj.exists():
        return {"exists": False, "total_results": 0, "rule_counts": {}}
    pattern = re.compile(
        r"^\s*RULECHECK\s+(.+?)\s+\.{2,}\s+TOTAL\s+Result\s+Count\s*=\s*(\d+)",
        flags=re.IGNORECASE,
    )
    counts: dict[str, int] = {}
    for line in path_obj.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        count = int(match.group(2))
        if count:
            counts[match.group(1).strip()] = count
    return {"exists": True, "total_results": sum(counts.values()), "rule_counts": dict(sorted(counts.items()))}
