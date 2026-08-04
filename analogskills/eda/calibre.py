"""Calibre/PVS-style verification command-spec builders."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from analogskills.repair import DrcIssue, LvsIssue

from .command import EdaCommand, EdaRunResult, run_eda_command
from .reports import PexReport, parse_drc_report, parse_lvs_report, parse_pex_report


@dataclass(frozen=True)
class PexExtractionPlan:
    command: EdaCommand
    layout: str
    source: str
    rule_deck: str
    report_path: str
    extracted_netlist_path: str
    engine: str = "calibre"
    corner: str = ""
    format: str = "spf"
    pre_commands: tuple[EdaCommand, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PexExtractionResult:
    plan: PexExtractionPlan
    run: EdaRunResult
    report: PexReport


@dataclass(frozen=True)
class PexInputBundle:
    source_netlist_path: str
    layout_artifact_path: str
    layout_artifact_kind: str = "oa_json"
    oa_json_path: str = ""
    oa_skill_path: str = ""
    stream_skill_path: str = ""
    stream_command: object | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def build_calibre_pex_packaging_contract(
    *,
    pdk: object | None = None,
    rule_deck: str | Path | None = None,
    layout_artifact_kind: str | None = None,
    corner: str | None = None,
    pex_format: str | None = None,
    stream_format: str | None = None,
    stream_layer_map: str | Path | None = None,
    stream_object_map: str | Path | None = None,
    stream_binary: str | None = None,
    replace_cellview: bool | None = None,
) -> dict[str, object]:
    """Build a serializable PDK-backed packaging contract for Calibre PEX."""

    pdk_metadata = _mapping_or_empty(getattr(pdk, "metadata", {}))
    calibre_metadata = _mapping_or_empty(pdk_metadata.get("calibre"))
    pex_metadata = _mapping_or_empty(calibre_metadata.get("pex"))
    stream_metadata = _mapping_or_empty(pdk_metadata.get("streamout"))

    artifact_kind = str(layout_artifact_kind or stream_metadata.get("layout_artifact_kind") or "oa_json").strip().lower()
    if artifact_kind not in {"oa_json", "oa_skill", "gds", "oasis"}:
        raise ValueError("layout_artifact_kind must be 'oa_json', 'oa_skill', 'gds', or 'oasis'")
    resolved_stream_format = str(stream_format or stream_metadata.get("format") or (artifact_kind if artifact_kind in {"gds", "oasis"} else "gds")).strip().lower()
    if resolved_stream_format not in {"gds", "oasis"}:
        raise ValueError("stream_format must be 'gds' or 'oasis'")
    resolved_rule_deck = str(rule_deck or pex_metadata.get("rule_deck") or "").strip()
    resolved_corner = str(corner or pex_metadata.get("corner") or _default_calibre_pex_corner(pdk)).strip()
    resolved_pex_format = str(pex_format or pex_metadata.get("format") or "spf").strip().lower()
    resolved_stream_layer_map = str(stream_layer_map or stream_metadata.get("layer_map") or "").strip()
    resolved_stream_object_map = str(stream_object_map or stream_metadata.get("object_map") or "").strip()
    resolved_stream_binary = str(stream_binary or stream_metadata.get("binary") or "virtuoso").strip()
    resolved_replace_cellview = bool(stream_metadata.get("replace_cellview", True) if replace_cellview is None else replace_cellview)

    available_corners = tuple(sorted(str(name) for name in getattr(pdk, "extraction_corners", {}).keys()))
    issues: list[str] = []
    if not resolved_rule_deck:
        issues.append("missing calibre PEX rule deck")
    if artifact_kind in {"gds", "oasis"} and not resolved_stream_layer_map:
        issues.append(f"{artifact_kind} packaging requires a stream layer map")
    if resolved_corner and available_corners and resolved_corner not in available_corners:
        issues.append(f"PEX corner {resolved_corner!r} is not defined by the PDK")

    return {
        "engine": "calibre",
        "pdk": {
            "name": str(getattr(pdk, "name", "")),
            "available_extraction_corners": available_corners,
            "preferred_extraction_corner": resolved_corner,
        },
        "layout_artifact_kind": artifact_kind,
        "streamout": {
            "required": artifact_kind in {"gds", "oasis"},
            "format": resolved_stream_format,
            "layer_map_path": resolved_stream_layer_map,
            "object_map_path": resolved_stream_object_map,
            "binary": resolved_stream_binary,
        },
        "pex": {
            "rule_deck": resolved_rule_deck,
            "corner": resolved_corner,
            "format": resolved_pex_format,
        },
        "writeback": {
            "replace_cellview": resolved_replace_cellview,
        },
        "ready": not issues,
        "issues": tuple(issues),
    }


def build_foundry_packaging_spec(
    *,
    pdk: object | None = None,
    rule_deck: str | Path | None = None,
    layout_artifact_kind: str | None = None,
    corner: str | None = None,
    pex_format: str | None = None,
    stream_format: str | None = None,
    stream_layer_map: str | Path | None = None,
    stream_object_map: str | Path | None = None,
    stream_binary: str | None = None,
    replace_cellview: bool | None = None,
) -> dict[str, object]:
    """Build a foundry-facing packaging spec from PDK metadata plus overrides."""

    contract = build_calibre_pex_packaging_contract(
        pdk=pdk,
        rule_deck=rule_deck,
        layout_artifact_kind=layout_artifact_kind,
        corner=corner,
        pex_format=pex_format,
        stream_format=stream_format,
        stream_layer_map=stream_layer_map,
        stream_object_map=stream_object_map,
        stream_binary=stream_binary,
        replace_cellview=replace_cellview,
    )
    pdk_metadata = _mapping_or_empty(getattr(pdk, "metadata", {}))
    foundry_metadata = _mapping_or_empty(pdk_metadata.get("foundry_packaging"))
    streamout = dict(contract.get("streamout", {}))
    pex = dict(contract.get("pex", {}))
    required_files = {
        "rule_deck": str(pex.get("rule_deck", "")),
        "stream_layer_map": str(streamout.get("layer_map_path", "")),
        "stream_object_map": str(streamout.get("object_map_path", "")),
    }
    artifact_policy = {
        "layout_artifact_kind": str(contract.get("layout_artifact_kind", "")),
        "stream_required": bool(streamout.get("required", False)),
        "replace_cellview": bool(dict(contract.get("writeback", {})).get("replace_cellview", False)),
        "stream_format": str(streamout.get("format", "")),
        "pex_format": str(pex.get("format", "")),
        "corner": str(pex.get("corner", "")),
    }
    required_inputs = tuple(
        name
        for name, path in required_files.items()
        if path or name == "stream_object_map"
    )
    missing_files = tuple(
        name
        for name, path in required_files.items()
        if name != "stream_object_map" and not path
    )
    spec_issues = [*tuple(contract.get("issues", ()))]
    for name in missing_files:
        issue = f"missing required foundry packaging file: {name}"
        if issue not in spec_issues:
            spec_issues.append(issue)
    return {
        "engine": str(contract.get("engine", "")),
        "pdk_name": str(dict(contract.get("pdk", {})).get("name", "")),
        "artifact_policy": artifact_policy,
        "required_inputs": required_inputs,
        "required_files": required_files,
        "defaults": {
            "stream_binary": str(streamout.get("binary", "")),
            "preferred_corner": str(pex.get("corner", "")),
        },
        "foundry_metadata": dict(foundry_metadata),
        "ready": not spec_issues,
        "issues": tuple(str(issue) for issue in spec_issues),
    }


def build_foundry_verification_deck_spec(
    *,
    pdk: object | None = None,
    layout_artifact_kind: str | None = None,
    stream_format: str | None = None,
    stream_layer_map: str | Path | None = None,
    stream_object_map: str | Path | None = None,
    stream_binary: str | None = None,
    replace_cellview: bool | None = None,
    drc_rule_deck: str | Path | None = None,
    drc_runset: str | Path | None = None,
    lvs_rule_deck: str | Path | None = None,
    lvs_runset: str | Path | None = None,
    pex_rule_deck: str | Path | None = None,
    pex_runset: str | Path | None = None,
    corner: str | None = None,
    pex_format: str | None = None,
) -> dict[str, object]:
    """Build a stable foundry-facing DRC/LVS/PEX deck contract from PDK metadata."""

    pdk_metadata = _mapping_or_empty(getattr(pdk, "metadata", {}))
    calibre_metadata = _mapping_or_empty(pdk_metadata.get("calibre"))
    foundry_metadata = _mapping_or_empty(pdk_metadata.get("foundry_packaging"))
    verification_metadata = _mapping_or_empty(foundry_metadata.get("verification_decks"))

    packaging = build_foundry_packaging_spec(
        pdk=pdk,
        rule_deck=pex_rule_deck,
        layout_artifact_kind=layout_artifact_kind,
        corner=corner,
        pex_format=pex_format,
        stream_format=stream_format,
        stream_layer_map=stream_layer_map,
        stream_object_map=stream_object_map,
        stream_binary=stream_binary,
        replace_cellview=replace_cellview,
    )

    artifact_policy = dict(packaging.get("artifact_policy", {}))
    shared_files = {
        "stream_layer_map": str(dict(packaging.get("required_files", {})).get("stream_layer_map", "")),
        "stream_object_map": str(dict(packaging.get("required_files", {})).get("stream_object_map", "")),
    }
    shared_inputs = ("layout_artifact",)
    stream_required = bool(artifact_policy.get("stream_required", False))
    layout_kind = str(artifact_policy.get("layout_artifact_kind", ""))

    drc_stage = _build_foundry_verification_stage_spec(
        "drc",
        stage_metadata=_merged_stage_metadata(calibre_metadata, verification_metadata, "drc"),
        rule_deck=drc_rule_deck,
        runset=drc_runset,
        required_inputs=shared_inputs,
        shared_files=shared_files,
        stream_required=stream_required,
        layout_artifact_kind=layout_kind,
    )
    lvs_stage = _build_foundry_verification_stage_spec(
        "lvs",
        stage_metadata=_merged_stage_metadata(calibre_metadata, verification_metadata, "lvs"),
        rule_deck=lvs_rule_deck,
        runset=lvs_runset,
        required_inputs=shared_inputs + ("source_netlist",),
        shared_files=shared_files,
        stream_required=stream_required,
        layout_artifact_kind=layout_kind,
    )

    pex_stage_metadata = _merged_stage_metadata(calibre_metadata, verification_metadata, "pex")
    pex_stage_metadata = {
        **pex_stage_metadata,
        "rule_deck": str(dict(packaging.get("required_files", {})).get("rule_deck", "")),
        "corner": str(artifact_policy.get("corner", "")),
        "format": str(artifact_policy.get("pex_format", "")),
    }
    pex_stage = _build_foundry_verification_stage_spec(
        "pex",
        stage_metadata=pex_stage_metadata,
        rule_deck=pex_rule_deck,
        runset=pex_runset,
        required_inputs=shared_inputs + ("source_netlist",),
        shared_files=shared_files,
        stream_required=stream_required,
        layout_artifact_kind=layout_kind,
    )
    pex_stage["corner"] = str(pex_stage_metadata.get("corner", ""))
    pex_stage["format"] = str(pex_stage_metadata.get("format", ""))
    pex_stage["available_corners"] = tuple(sorted(str(name) for name in getattr(pdk, "extraction_corners", {}).keys()))

    issues: list[str] = [*tuple(packaging.get("issues", ()))]
    for stage in (drc_stage, lvs_stage, pex_stage):
        for issue in stage["issues"]:
            text = str(issue)
            if text not in issues:
                issues.append(text)

    required_inputs = tuple(
        name
        for name in ("layout_artifact", "source_netlist")
        if any(name in tuple(stage["required_inputs"]) for stage in (drc_stage, lvs_stage, pex_stage))
    )
    ready = bool(packaging.get("ready", False)) and drc_stage["ready"] and lvs_stage["ready"] and pex_stage["ready"]
    return {
        "engine": str(packaging.get("engine", "")),
        "pdk_name": str(packaging.get("pdk_name", "")),
        "artifact_policy": artifact_policy,
        "required_inputs": required_inputs,
        "shared_required_files": shared_files,
        "stages": {
            "drc": drc_stage,
            "lvs": lvs_stage,
            "pex": pex_stage,
        },
        "foundry_metadata": dict(foundry_metadata),
        "ready": ready,
        "issues": tuple(issues),
    }


def build_foundry_execution_readiness_contract(
    *,
    candidate_readiness: Mapping[str, object] | None = None,
    physical_contract: Mapping[str, object] | None = None,
    deck_spec: Mapping[str, object] | None = None,
    packaging_spec: Mapping[str, object] | None = None,
    available_inputs: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a stable candidate-to-foundry execution readiness contract.

    This function does not execute tools or orchestrate flow sequencing. It
    only lowers candidate physical readiness, foundry deck requirements, and
    currently available input artifacts into an explicit per-stage readiness
    contract for downstream callers.
    """

    normalized_candidate = dict(_mapping_or_empty(candidate_readiness))
    normalized_physical = dict(_mapping_or_empty(physical_contract))
    normalized_inputs = {str(name): bool(value) for name, value in dict(_mapping_or_empty(available_inputs)).items()}
    normalized_packaging = dict(_mapping_or_empty(packaging_spec))
    normalized_deck = dict(_mapping_or_empty(deck_spec))
    if not normalized_packaging and normalized_deck:
        normalized_packaging = {
            "engine": str(normalized_deck.get("engine", "")),
            "pdk_name": str(normalized_deck.get("pdk_name", "")),
            "artifact_policy": dict(_mapping_or_empty(normalized_deck.get("artifact_policy"))),
            "required_inputs": tuple(str(name) for name in normalized_deck.get("required_inputs", ())),
            "required_files": dict(_mapping_or_empty(normalized_deck.get("shared_required_files"))),
            "ready": bool(normalized_deck.get("ready", False)),
            "issues": tuple(str(issue) for issue in normalized_deck.get("issues", ())),
        }

    artifact_policy = dict(_mapping_or_empty(normalized_deck.get("artifact_policy") or normalized_packaging.get("artifact_policy")))
    deck_stages = dict(_mapping_or_empty(normalized_deck.get("stages")))
    physical_readiness = dict(_mapping_or_empty(normalized_physical.get("readiness")))
    system_contract = dict(_mapping_or_empty(normalized_physical.get("system")))
    hierarchy_lowering = dict(_mapping_or_empty(normalized_physical.get("hierarchy_lowering")))
    hierarchy_binding = dict(_mapping_or_empty(normalized_physical.get("hierarchy_binding")))
    hierarchy_parasitics = dict(_mapping_or_empty(normalized_physical.get("hierarchy_parasitics")))
    contract_issues = dict(_mapping_or_empty(normalized_physical.get("issues")))

    candidate_blocking_issues: list[str] = []
    for group_name in ("pdk", "placement", "routing"):
        for issue in contract_issues.get(group_name, ()):
            text = str(issue)
            if text:
                candidate_blocking_issues.append(text)
    if int(system_contract.get("restore_bus_required_count", 0)) > 0:
        candidate_blocking_issues.append("system bus corridor restoration is still required before foundry execution")
    if int(system_contract.get("restore_feedback_required_count", 0)) > 0:
        candidate_blocking_issues.append("system feedback restoration is still required before foundry execution")
    missing_pcell_bindings = tuple(str(name) for name in tuple(hierarchy_lowering.get("missing_pcell_bindings", ())) if str(name))
    if missing_pcell_bindings:
        candidate_blocking_issues.append(
            "hierarchical lowering still lacks PDK PCell bindings for: " + ", ".join(missing_pcell_bindings)
        )
    binding_blocked_partitions = tuple(
        str(name)
        for name in tuple(hierarchy_binding.get("blocked_partitions", ()))
        if str(name)
    )
    if binding_blocked_partitions:
        candidate_blocking_issues.append(
            "hierarchical binding coverage still blocked for: " + ", ".join(binding_blocked_partitions)
        )
    architecture_budget_blocked_partitions = tuple(
        str(dict(item).get("name", ""))
        for item in tuple(hierarchy_parasitics.get("partitions", ()))
        if isinstance(item, Mapping)
        and str(dict(item).get("name", ""))
        and not dict(dict(item).get("architecture_budget", {}) or {})
    )
    if architecture_budget_blocked_partitions:
        candidate_blocking_issues.append(
            "hierarchical architecture budget coverage still missing for: "
            + ", ".join(architecture_budget_blocked_partitions)
        )

    physical_ready = bool(
        normalized_candidate.get("pex_ready", physical_readiness.get("ready_for_extraction", False))
    )
    streamout_ready = bool(
        normalized_candidate.get("streamout_ready", physical_readiness.get("pdk_valid", False))
    )
    verification_candidate_ready = bool(
        normalized_candidate.get("verification_ready", streamout_ready and physical_ready)
    )
    layout_input_ready = bool(normalized_inputs.get("layout_artifact", False))
    source_input_ready = bool(normalized_inputs.get("source_netlist", False))
    extracted_input_ready = bool(normalized_inputs.get("extracted_netlist", False))

    packaging_issues = tuple(str(issue) for issue in normalized_packaging.get("issues", ()) if str(issue))
    deck_issues = tuple(str(issue) for issue in normalized_deck.get("issues", ()) if str(issue))
    shared_required_files = {
        str(name): str(value)
        for name, value in dict(
            _mapping_or_empty(normalized_deck.get("shared_required_files") or normalized_packaging.get("required_files"))
        ).items()
    }

    shared_missing_files = tuple(
        name
        for name, value in shared_required_files.items()
        if name != "stream_object_map" and not str(value).strip()
    )

    stage_rows: dict[str, dict[str, object]] = {}
    for stage_name in ("drc", "lvs", "pex"):
        stage_spec = dict(_mapping_or_empty(deck_stages.get(stage_name)))
        required_inputs = tuple(str(name) for name in stage_spec.get("required_inputs", ()))
        required_files = {
            str(name): str(value)
            for name, value in dict(_mapping_or_empty(stage_spec.get("required_files"))).items()
        }
        missing_inputs = tuple(
            name
            for name in required_inputs
            if not normalized_inputs.get(name, False)
        )
        missing_files = tuple(
            name
            for name, value in required_files.items()
            if not str(value).strip()
        )
        blocking_issues = [
            *tuple(str(issue) for issue in stage_spec.get("issues", ()) if str(issue)),
            *tuple(f"missing available input: {name}" for name in missing_inputs),
            *tuple(f"missing required file: {name}" for name in missing_files),
            *tuple(f"missing shared required file: {name}" for name in shared_missing_files),
        ]
        if stage_name == "drc" and not streamout_ready:
            blocking_issues.append("candidate is not ready for streamout")
        if stage_name in {"lvs", "pex"} and not physical_ready:
            blocking_issues.append("candidate is not ready for extraction")
        if stage_name in {"drc", "lvs"} and not verification_candidate_ready:
            blocking_issues.append("candidate is not ready for foundry verification")
        if stage_name == "pex" and not extracted_input_ready and not physical_ready:
            # Physical readiness is the minimum gate for generating extracted data.
            pass
        if stage_name == "pex" and not verification_candidate_ready:
            blocking_issues.append("candidate is not ready for foundry PEX")

        deduped_blocking = tuple(dict.fromkeys(text for text in blocking_issues if text))
        stage_rows[stage_name] = {
            "ready": not deduped_blocking and bool(stage_spec.get("ready", False)),
            "required_inputs": required_inputs,
            "required_files": required_files,
            "missing_inputs": missing_inputs,
            "missing_files": missing_files,
            "blocking_issues": deduped_blocking,
        }

    ready_stages = tuple(stage for stage, row in stage_rows.items() if bool(row.get("ready", False)))
    blocked_stages = tuple(stage for stage, row in stage_rows.items() if not bool(row.get("ready", False)))
    system_repair_targets = _system_repair_targets(system_contract)
    overall_issues = tuple(
        dict.fromkeys(
            [
                *candidate_blocking_issues,
                *packaging_issues,
                *deck_issues,
                *(issue for row in stage_rows.values() for issue in tuple(row.get("blocking_issues", ()))),
            ]
        )
    )
    return {
        "engine": str(normalized_deck.get("engine") or normalized_packaging.get("engine") or "calibre"),
        "pdk_name": str(normalized_deck.get("pdk_name") or normalized_packaging.get("pdk_name") or ""),
        "artifact_policy": artifact_policy,
        "system": {
            "topology_name": str(system_contract.get("topology_name", "")),
            "bus_contract_count": int(system_contract.get("bus_contract_count", 0)),
            "reference_path_count": int(system_contract.get("reference_path_count", 0)),
            "feedback_contract_count": int(system_contract.get("feedback_contract_count", 0)),
            "timing_chain_count": int(system_contract.get("timing_chain_count", 0)),
            "restore_bus_required_count": int(system_contract.get("restore_bus_required_count", 0)),
            "restore_feedback_required_count": int(system_contract.get("restore_feedback_required_count", 0)),
            "repair_targets": system_repair_targets,
        },
        "candidate_readiness": {
            "streamout_ready": streamout_ready,
            "pex_ready": physical_ready,
            "verification_ready": verification_candidate_ready,
            "blocking_issue_count": int(normalized_candidate.get("blocking_issue_count", len(candidate_blocking_issues)) or 0),
        },
        "available_inputs": {
            "layout_artifact": layout_input_ready,
            "source_netlist": source_input_ready,
            "extracted_netlist": extracted_input_ready,
        },
        "shared_required_files": shared_required_files,
        "hierarchy_binding_summary": {
            "missing_pcell_bindings": missing_pcell_bindings,
            "binding_blocked_partitions": binding_blocked_partitions,
            "macro_binding_partitions": tuple(
                str(name)
                for name in tuple(hierarchy_binding.get("macro_binding_partitions", ()))
                if str(name)
            ),
            "ready_for_pdk_implementation_partition_count": int(
                hierarchy_binding.get("ready_for_pdk_implementation_partition_count", 0) or 0
            ),
            "architecture_budget_blocked_partitions": architecture_budget_blocked_partitions,
        },
        "stages": stage_rows,
        "ready_stages": ready_stages,
        "blocked_stages": blocked_stages,
        "ready": bool(normalized_packaging.get("ready", False) or not normalized_packaging)
        and bool(normalized_deck.get("ready", False) or not normalized_deck)
        and not overall_issues
        and not blocked_stages,
        "issues": overall_issues,
    }


def _system_repair_targets(system_contract: Mapping[str, object] | None) -> tuple[dict[str, object], ...]:
    normalized = dict(_mapping_or_empty(system_contract))
    targets: list[dict[str, object]] = []
    for bus in tuple(normalized.get("bus_contracts", ()) or ()):
        item = dict(_mapping_or_empty(bus))
        if not bool(item.get("restore_required", False)):
            continue
        targets.append(
            {
                "kind": "bus_corridor_restore",
                "name": str(item.get("name", "")),
                "nets": tuple(str(net) for net in tuple(item.get("nets", ()) or ()) if str(net)),
                "recommended_level": "parent",
                "reason": "bus corridor restoration usually belongs to enclosing routing context",
            }
        )
    for feedback in tuple(normalized.get("feedback_contracts", ()) or ()):
        item = dict(_mapping_or_empty(feedback))
        if not bool(item.get("restore_required", False)):
            continue
        net = str(item.get("net", ""))
        targets.append(
            {
                "kind": "feedback_path_restore",
                "name": net,
                "nets": (net,) if net else (),
                "recommended_level": "top",
                "reason": "feedback restoration usually spans system-level control connectivity",
            }
        )
    for reference in tuple(normalized.get("reference_paths", ()) or ()):
        item = dict(_mapping_or_empty(reference))
        if not bool(item.get("preserve_integrity", False)):
            continue
        net = str(item.get("net", ""))
        targets.append(
            {
                "kind": "reference_integrity_protect",
                "name": net,
                "nets": (net,) if net else (),
                "recommended_level": "leaf_or_parent",
                "reason": "reference integrity should be preserved within the local block unless corridor-level routing is involved",
            }
        )
    if not any(item.get("kind") == "bus_corridor_restore" for item in targets) and int(normalized.get("restore_bus_required_count", 0)) > 0:
        targets.append(
            {
                "kind": "bus_corridor_restore",
                "name": "aggregate_restore_bus_corridor",
                "nets": (),
                "recommended_level": "parent",
                "reason": "bus corridor restoration usually belongs to enclosing routing context",
            }
        )
    if not any(item.get("kind") == "feedback_path_restore" for item in targets) and int(normalized.get("restore_feedback_required_count", 0)) > 0:
        targets.append(
            {
                "kind": "feedback_path_restore",
                "name": "aggregate_restore_feedback_path",
                "nets": (),
                "recommended_level": "top",
                "reason": "feedback restoration usually spans system-level control connectivity",
            }
        )
    return tuple(targets)


def build_foundry_execution_readiness_contract_from_bundle(
    bundle: PexInputBundle,
    *,
    candidate_readiness: Mapping[str, object] | None = None,
    physical_contract: Mapping[str, object] | None = None,
    deck_spec: Mapping[str, object] | None = None,
    extracted_netlist_ready: bool = False,
) -> dict[str, object]:
    """Build foundry execution readiness directly from a prepared PEX input bundle."""

    available_inputs = {
        "layout_artifact": bool(str(getattr(bundle, "layout_artifact_path", "")).strip()),
        "source_netlist": bool(str(getattr(bundle, "source_netlist_path", "")).strip()),
        "extracted_netlist": bool(extracted_netlist_ready),
    }
    packaging_contract = _mapping_or_empty(getattr(bundle, "metadata", {}).get("packaging_contract", {}))
    return build_foundry_execution_readiness_contract(
        candidate_readiness=candidate_readiness,
        physical_contract=physical_contract,
        deck_spec=deck_spec,
        packaging_spec=packaging_contract,
        available_inputs=available_inputs,
    )


def build_stage_foundry_execution_contracts(
    bundle: Mapping[str, object],
    *,
    candidate_readiness: Mapping[str, object] | None = None,
    physical_contract: Mapping[str, object] | None = None,
    deck_spec: Mapping[str, object] | None = None,
    packaging_spec: Mapping[str, object] | None = None,
    available_inputs: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Lower global foundry readiness and deck specs into stage-local contracts."""

    from .reports import build_stage_foundry_readiness_contract, summarize_stage_verification_contracts

    global_contract = build_foundry_execution_readiness_contract(
        candidate_readiness=candidate_readiness,
        physical_contract=physical_contract,
        deck_spec=deck_spec,
        packaging_spec=packaging_spec,
        available_inputs=available_inputs,
    )
    normalized_deck = dict(_mapping_or_empty(deck_spec))
    deck_stages = dict(_mapping_or_empty(normalized_deck.get("stages")))
    shared_required_files = {
        str(name): str(value)
        for name, value in dict(_mapping_or_empty(global_contract.get("shared_required_files"))).items()
        if str(name)
    }
    artifact_policy = dict(_mapping_or_empty(global_contract.get("artifact_policy")))
    stage_contracts = summarize_stage_verification_contracts(bundle)
    rows: list[dict[str, object]] = []
    ready_stage_count = 0
    blocked_stage_count = 0
    for stage in stage_contracts:
        local = build_stage_foundry_readiness_contract(stage, global_contract)
        required_checks = tuple(str(name) for name in tuple(local.get("required_checks", ()) or ()) if str(name))
        deck_requirements = {
            name: {
                "required_inputs": tuple(
                    str(item)
                    for item in tuple(dict(_mapping_or_empty(deck_stages.get(name))).get("required_inputs", ()) or ())
                    if str(item)
                ),
                "required_files": {
                    str(key): str(value)
                    for key, value in dict(_mapping_or_empty(dict(_mapping_or_empty(deck_stages.get(name))).get("required_files"))).items()
                    if str(key)
                },
                "missing_inputs": tuple(
                    str(item)
                    for item in tuple(dict(_mapping_or_empty(global_contract.get("stages"))).get(name, {}).get("missing_inputs", ()) or ())
                    if str(item)
                ),
                "missing_files": tuple(
                    str(item)
                    for item in tuple(dict(_mapping_or_empty(global_contract.get("stages"))).get(name, {}).get("missing_files", ()) or ())
                    if str(item)
                ),
                "blocking_issues": tuple(
                    str(item)
                    for item in tuple(dict(_mapping_or_empty(global_contract.get("stages"))).get(name, {}).get("blocking_issues", ()) or ())
                    if str(item)
                ),
            }
            for name in required_checks
        }
        row = {
            "order": int(stage.get("order", 0) or 0),
            "target_cell": str(stage.get("target_cell", "")),
            "stage_hierarchy_node": str(stage.get("stage_hierarchy_node", "")),
            "proposal_kind": str(stage.get("proposal_kind", "")),
            "selected_plan_kind": str(stage.get("selected_plan_kind", "")),
            "required_checks": required_checks,
            "ready_checks": tuple(str(name) for name in tuple(local.get("ready_checks", ()) or ()) if str(name)),
            "blocked_checks": tuple(str(name) for name in tuple(local.get("blocked_checks", ()) or ()) if str(name)),
            "ready": bool(local.get("ready", False)),
            "required_enclosing_reruns": tuple(str(level) for level in tuple(local.get("required_enclosing_reruns", ()) or ()) if str(level)),
            "restore_required": bool(local.get("restore_required", False)),
            "reference_sensitive": bool(local.get("reference_sensitive", False)),
            "artifact_policy": artifact_policy,
            "shared_required_files": shared_required_files,
            "deck_requirements": deck_requirements,
            "issues": tuple(str(item) for item in tuple(local.get("issues", ()) or ()) if str(item)),
        }
        if row["ready"]:
            ready_stage_count += 1
        else:
            blocked_stage_count += 1
        rows.append(row)
    rows.sort(key=lambda item: (int(item.get("order", 0) or 0), str(item.get("target_cell", ""))))
    return {
        "global_foundry_execution": global_contract,
        "artifact_policy": artifact_policy,
        "shared_required_files": shared_required_files,
        "stages": tuple(rows),
        "summary": {
            "total_stage_count": len(rows),
            "ready_stage_count": ready_stage_count,
            "blocked_stage_count": blocked_stage_count,
        },
    }


def query_stage_foundry_execution_contract(
    bundle: Mapping[str, object],
    *,
    candidate_readiness: Mapping[str, object] | None = None,
    physical_contract: Mapping[str, object] | None = None,
    deck_spec: Mapping[str, object] | None = None,
    packaging_spec: Mapping[str, object] | None = None,
    available_inputs: Mapping[str, object] | None = None,
    cell: object | None = None,
    node: object | None = None,
) -> dict[str, object]:
    contract = build_stage_foundry_execution_contracts(
        bundle,
        candidate_readiness=candidate_readiness,
        physical_contract=physical_contract,
        deck_spec=deck_spec,
        packaging_spec=packaging_spec,
        available_inputs=available_inputs,
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


def derive_calibre_precision(*, dbu_per_uu: float, user_units: float) -> int:
    """Convert a GDS/OASIS unit convention into the Calibre PRECISION value."""

    if dbu_per_uu <= 0:
        raise ValueError("dbu_per_uu must be positive")
    if user_units <= 0:
        raise ValueError("user_units must be positive")
    return int(round(float(dbu_per_uu) / float(user_units)))


def render_calibre_drc_runset(
    *,
    layout: str | Path,
    primary: str,
    results_database: str | Path,
    summary_report: str | Path | None = None,
    body_lines: Sequence[object] = (),
    layout_system: str = "GDSII",
    precision: int | None = None,
    title: str = "",
) -> str:
    lines = [
        f"// {title or f'Calibre DRC runset for {primary}'}",
        f'LAYOUT PATH "{layout}"',
        f'LAYOUT PRIMARY "{primary}"',
        f"LAYOUT SYSTEM {layout_system}",
        f'DRC RESULTS DATABASE "{results_database}" ASCII',
    ]
    if precision is not None:
        lines.insert(4, f"PRECISION {int(precision)}")
    if summary_report is not None and str(summary_report).strip():
        lines.append(f'DRC SUMMARY REPORT "{summary_report}"')
    lines.extend(_normalized_runset_body_lines(body_lines))
    return "\n".join(lines).rstrip() + "\n"


def write_calibre_drc_runset(path: str | Path, **kwargs: object) -> Path:
    target = Path(path)
    target.write_text(render_calibre_drc_runset(**kwargs), encoding="utf-8")
    return target


def render_calibre_lvs_runset(
    *,
    layout: str | Path,
    source: str | Path,
    primary: str,
    layout_primary: str | None = None,
    source_primary: str | None = None,
    report: str | Path,
    body_lines: Sequence[object] = (),
    power_names: Sequence[str] = (),
    ground_names: Sequence[str] = (),
    report_options: Sequence[str] = ("S",),
    layout_system: str = "GDSII",
    source_system: str = "SPICE",
    precision: int | None = None,
    title: str = "",
) -> str:
    resolved_layout_primary = str(layout_primary or primary)
    resolved_source_primary = str(source_primary or primary)
    lines = [
        f"// {title or f'Calibre LVS runset for {primary}'}",
        f'LAYOUT PATH "{layout}"',
        f'LAYOUT PRIMARY "{resolved_layout_primary}"',
        f"LAYOUT SYSTEM {layout_system}",
        f'SOURCE PATH "{source}"',
        f'SOURCE PRIMARY "{resolved_source_primary}"',
        f"SOURCE SYSTEM {source_system}",
        f'LVS REPORT "{report}"',
    ]
    if precision is not None:
        lines.insert(7, f"PRECISION {int(precision)}")
    for option in report_options:
        value = str(option).strip()
        if value:
            lines.append(f"LVS REPORT OPTION {value}")
    for name in power_names:
        value = str(name).strip()
        if value:
            lines.append(f'LVS POWER NAME "{value}"')
    for name in ground_names:
        value = str(name).strip()
        if value:
            lines.append(f'LVS GROUND NAME "{value}"')
    lines.extend(_normalized_runset_body_lines(body_lines))
    return "\n".join(lines).rstrip() + "\n"


def write_calibre_lvs_runset(path: str | Path, **kwargs: object) -> Path:
    target = Path(path)
    target.write_text(render_calibre_lvs_runset(**kwargs), encoding="utf-8")
    return target


def render_calibre_pex_runset(
    *,
    layout: str | Path,
    source: str | Path,
    primary: str,
    layout_primary: str | None = None,
    source_primary: str | None = None,
    extracted_netlist: str | Path,
    body_lines: Sequence[object] = (),
    pex_format: str = "SPF",
    layout_system: str = "GDSII",
    source_system: str = "SPICE",
    precision: int | None = None,
    title: str = "",
) -> str:
    resolved_layout_primary = str(layout_primary or primary)
    resolved_source_primary = str(source_primary or primary)
    lines = [
        f"// {title or f'Calibre PEX runset for {primary}'}",
        f'LAYOUT PATH "{layout}"',
        f'LAYOUT PRIMARY "{resolved_layout_primary}"',
        f"LAYOUT SYSTEM {layout_system}",
        f'SOURCE PATH "{source}"',
        f'SOURCE PRIMARY "{resolved_source_primary}"',
        f"SOURCE SYSTEM {source_system}",
        f'PEX NETLIST "{extracted_netlist}"',
        f"PEX FORMAT {str(pex_format).upper()}",
    ]
    if precision is not None:
        lines.insert(7, f"PRECISION {int(precision)}")
    lines.extend(_normalized_runset_body_lines(body_lines))
    return "\n".join(lines).rstrip() + "\n"


def write_calibre_pex_runset(path: str | Path, **kwargs: object) -> Path:
    target = Path(path)
    target.write_text(render_calibre_pex_runset(**kwargs), encoding="utf-8")
    return target


def make_calibre_drc_command(rule_deck: str | Path, layout: str | Path | None = None, *, binary: str = "calibre") -> EdaCommand:
    cmd = [binary, "-drc", str(rule_deck)]
    if layout is not None and str(layout).strip():
        cmd.append(str(layout))
    return EdaCommand(cmd)


def make_calibre_lvs_command(
    rule_deck: str | Path,
    layout: str | Path | None = None,
    source: str | Path | None = None,
    *,
    binary: str = "calibre",
) -> EdaCommand:
    cmd = [binary, "-lvs", str(rule_deck)]
    if layout is not None and str(layout).strip():
        cmd.append(str(layout))
    if source is not None and str(source).strip():
        cmd.append(str(source))
    return EdaCommand(cmd)


def make_calibre_pex_command(
    rule_deck: str | Path,
    layout: str | Path | None = None,
    source: str | Path | None = None,
    *,
    report: str | Path | None = None,
    extracted_netlist: str | Path | None = None,
    pex_format: str = "spf",
    corner: str | None = None,
    switches: Mapping[str, str | int | float | bool] | None = None,
    binary: str = "calibre",
) -> EdaCommand:
    cmd = [binary, "-pex", str(rule_deck)]
    if layout is not None and str(layout).strip():
        cmd.append(str(layout))
    if source is not None and str(source).strip():
        cmd.append(str(source))
    if report is not None:
        cmd.extend(["-pexreport", str(report)])
    if extracted_netlist is not None:
        cmd.extend(["-pexnetlist", str(extracted_netlist)])
    if pex_format:
        cmd.extend(["-pexfmt", str(pex_format)])
    if corner:
        cmd.extend(["-pexcorner", str(corner)])
    for key, value in dict(switches or {}).items():
        option = str(key).strip()
        if not option:
            continue
        if value is True:
            cmd.append(option)
        elif value is False or value is None:
            continue
        else:
            cmd.extend([option, str(value)])
    return EdaCommand(cmd)


def build_calibre_pex_plan(
    rule_deck: str | Path,
    layout: str | Path,
    source: str | Path,
    *,
    report: str | Path,
    extracted_netlist: str | Path,
    pex_format: str = "spf",
    corner: str | None = None,
    switches: Mapping[str, str | int | float | bool] | None = None,
    binary: str = "calibre",
    cwd: str | Path | None = None,
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
    metadata: Mapping[str, object] | None = None,
    pdk: object | None = None,
) -> PexExtractionPlan:
    if corner is None and pdk is not None:
        extraction_corners = getattr(pdk, "extraction_corners", {})
        if "typ" in extraction_corners:
            corner = "typ"
    command = make_calibre_pex_command(
        rule_deck,
        layout,
        source,
        report=report,
        extracted_netlist=extracted_netlist,
        pex_format=pex_format,
        corner=corner,
        switches=switches,
        binary=binary,
    )
    command = EdaCommand(
        command.command,
        cwd=cwd,
        timeout_s=timeout_s,
        env=env,
    )
    return PexExtractionPlan(
        command=command,
        layout=str(layout),
        source=str(source),
        rule_deck=str(rule_deck),
        report_path=str(report),
        extracted_netlist_path=str(extracted_netlist),
        engine="calibre",
        corner=str(corner or ""),
        format=str(pex_format),
        pre_commands=(),
        metadata={
            **dict(metadata or {}),
            **({"extraction_corner": str(corner)} if corner else {}),
        },
    )


def prepare_calibre_pex_input_bundle(
    graph: object,
    sizing: Mapping[str, Mapping[str, object]],
    layout_plan: object,
    directory: str | Path,
    *,
    pdk: object | None = None,
    prefix: str = "pex",
    source_netlist_name: str | None = None,
    subckt_name: str | None = None,
    model_map: Mapping[str, str] | None = None,
    layout_artifact_kind: str | None = None,
    oa_json_name: str | None = None,
    oa_skill_name: str | None = None,
    stream_file_name: str | None = None,
    stream_skill_name: str | None = None,
    stream_format: str | None = None,
    stream_layer_map: str | Path | None = None,
    stream_object_map: str | Path | None = None,
    stream_binary: str = "virtuoso",
    stream_cwd: str | Path | None = None,
    stream_timeout_s: float = 120.0,
    stream_env: Mapping[str, str] | None = None,
    replace_cellview: bool | None = None,
    packaging_contract: Mapping[str, object] | None = None,
) -> PexInputBundle:
    """Export source/layout artifacts required to build a PEX execution plan."""

    from .netlist import export_lvs_netlist
    from .oa import layout_plan_to_oa_write_plan, save_oa_plan_json, write_oa_skill

    from .virtuoso import build_layout_streamout_plan

    normalized_packaging = _normalize_packaging_contract(packaging_contract)
    streamout_contract = _mapping_or_empty(normalized_packaging.get("streamout"))
    writeback_contract = _mapping_or_empty(normalized_packaging.get("writeback"))
    artifact_kind = str(layout_artifact_kind or normalized_packaging.get("layout_artifact_kind") or "oa_json").strip().lower()
    if artifact_kind not in {"oa_json", "oa_skill", "gds", "oasis"}:
        raise ValueError("layout_artifact_kind must be 'oa_json', 'oa_skill', 'gds', or 'oasis'")
    resolved_stream_format = str(stream_format or streamout_contract.get("format") or (artifact_kind if artifact_kind in {"gds", "oasis"} else "gds")).strip().lower()
    resolved_stream_layer_map = stream_layer_map if stream_layer_map is not None else _optional_path(streamout_contract.get("layer_map_path"))
    resolved_stream_object_map = stream_object_map if stream_object_map is not None else _optional_path(streamout_contract.get("object_map_path"))
    resolved_stream_binary = str(stream_binary or streamout_contract.get("binary") or "virtuoso")
    resolved_replace_cellview = bool(writeback_contract.get("replace_cellview", True) if replace_cellview is None else replace_cellview)
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = str(prefix or "pex")
    source_path = export_lvs_netlist(
        graph,
        sizing,
        target_dir / (source_netlist_name or f"{stem}_source.sp"),
        subckt_name=subckt_name,
        model_map=model_map,
        require_model_map=False,
    )
    oa_plan = layout_plan_to_oa_write_plan(layout_plan)
    oa_json_path = save_oa_plan_json(oa_plan, target_dir / (oa_json_name or f"{stem}_layout.oa.json"))
    oa_skill_path = write_oa_skill(
        oa_plan,
        target_dir / (oa_skill_name or f"{stem}_layout.il"),
        grid=pdk if pdk is not None else None,
        replace_cellview=resolved_replace_cellview,
    )
    stream_plan = None
    if artifact_kind in {"gds", "oasis"}:
        cellview = getattr(oa_plan, "cellview")
        stream_plan = build_layout_streamout_plan(
            lib=cellview.lib,
            cell=cellview.cell,
            view=cellview.view,
            output_path=target_dir / (stream_file_name or f"{stem}.{artifact_kind}"),
            skill_path=target_dir / (stream_skill_name or f"{stem}_streamout.il"),
            stream_format=resolved_stream_format if resolved_stream_format else artifact_kind,
            layer_map=resolved_stream_layer_map,
            object_map=resolved_stream_object_map,
            binary=resolved_stream_binary,
            cwd=stream_cwd or target_dir,
            timeout_s=stream_timeout_s,
            env=dict(stream_env or {}),
            metadata={"source": "prepare_calibre_pex_input_bundle"},
        )
    if artifact_kind == "oa_json":
        selected_layout_path = oa_json_path
    elif artifact_kind == "oa_skill":
        selected_layout_path = oa_skill_path
    else:
        selected_layout_path = Path(stream_plan.output_path)
    return PexInputBundle(
        source_netlist_path=str(source_path),
        layout_artifact_path=str(selected_layout_path),
        layout_artifact_kind=artifact_kind,
        oa_json_path=str(oa_json_path),
        oa_skill_path=str(oa_skill_path),
        stream_skill_path=str(stream_plan.skill_path) if stream_plan is not None else "",
        stream_command=getattr(stream_plan, "command", None),
        metadata={
            "directory": str(target_dir),
            "prefix": stem,
            "subckt_name": str(subckt_name or ""),
            "replace_cellview": resolved_replace_cellview,
            "stream_format": str(resolved_stream_format or ""),
            "stream_layer_map": str(resolved_stream_layer_map) if resolved_stream_layer_map is not None else "",
            "stream_object_map": str(resolved_stream_object_map) if resolved_stream_object_map is not None else "",
            "packaging_contract": normalized_packaging,
        },
    )


def build_calibre_pex_plan_from_bundle(
    rule_deck: str | Path,
    bundle: PexInputBundle,
    *,
    report: str | Path,
    extracted_netlist: str | Path,
    pex_format: str = "spf",
    corner: str | None = None,
    switches: Mapping[str, str | int | float | bool] | None = None,
    binary: str = "calibre",
    cwd: str | Path | None = None,
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
    metadata: Mapping[str, object] | None = None,
    pdk: object | None = None,
) -> PexExtractionPlan:
    """Build a PEX plan from a prepared source/layout artifact bundle."""

    return build_calibre_pex_plan(
        rule_deck,
        bundle.layout_artifact_path,
        bundle.source_netlist_path,
        report=report,
        extracted_netlist=extracted_netlist,
        pex_format=pex_format,
        corner=corner,
        switches=switches,
        binary=binary,
        cwd=cwd,
        timeout_s=timeout_s,
        env=env,
        metadata={
            **dict(metadata or {}),
            "layout_artifact_kind": bundle.layout_artifact_kind,
            "oa_json_path": bundle.oa_json_path,
            "oa_skill_path": bundle.oa_skill_path,
            "input_bundle": dict(bundle.metadata),
        },
        pdk=pdk,
    ) if bundle.stream_command is None else _with_pre_commands(
        build_calibre_pex_plan(
            rule_deck,
            bundle.layout_artifact_path,
            bundle.source_netlist_path,
            report=report,
            extracted_netlist=extracted_netlist,
            pex_format=pex_format,
            corner=corner,
            switches=switches,
            binary=binary,
            cwd=cwd,
            timeout_s=timeout_s,
            env=env,
            metadata={
                **dict(metadata or {}),
                "layout_artifact_kind": bundle.layout_artifact_kind,
                "oa_json_path": bundle.oa_json_path,
                "oa_skill_path": bundle.oa_skill_path,
                "input_bundle": dict(bundle.metadata),
            },
            pdk=pdk,
        ),
        (bundle.stream_command,),
    )


def build_calibre_pex_plan_from_packaging_contract(
    bundle: PexInputBundle,
    *,
    packaging_contract: Mapping[str, object],
    report: str | Path,
    extracted_netlist: str | Path,
    switches: Mapping[str, str | int | float | bool] | None = None,
    binary: str = "calibre",
    cwd: str | Path | None = None,
    timeout_s: float = 120.0,
    env: Mapping[str, str] | None = None,
    metadata: Mapping[str, object] | None = None,
    pdk: object | None = None,
) -> PexExtractionPlan:
    """Build a PEX plan directly from a bundle plus packaging contract."""

    normalized_packaging = _normalize_packaging_contract(packaging_contract)
    pex_contract = _mapping_or_empty(normalized_packaging.get("pex"))
    rule_deck = str(pex_contract.get("rule_deck", "")).strip()
    if not rule_deck:
        raise ValueError("packaging contract does not define a calibre PEX rule deck")
    corner = str(pex_contract.get("corner", "")).strip() or None
    pex_format = str(pex_contract.get("format", "spf")).strip().lower() or "spf"
    return build_calibre_pex_plan_from_bundle(
        rule_deck,
        bundle,
        report=report,
        extracted_netlist=extracted_netlist,
        pex_format=pex_format,
        corner=corner,
        switches=switches,
        binary=binary,
        cwd=cwd,
        timeout_s=timeout_s,
        env=env,
        metadata={
            **dict(metadata or {}),
            "packaging_contract": normalized_packaging,
        },
        pdk=pdk,
    )


def run_drc_and_parse(spec: EdaCommand, report: str | Path) -> tuple[EdaRunResult, tuple[DrcIssue, ...]]:
    result = run_eda_command(spec)
    return result, parse_drc_report(report)


def run_lvs_and_parse(spec: EdaCommand, report: str | Path) -> tuple[EdaRunResult, tuple[LvsIssue, ...]]:
    result = run_eda_command(spec)
    return result, parse_lvs_report(report)


def run_pex_and_parse(plan: PexExtractionPlan) -> PexExtractionResult:
    for pre_command in plan.pre_commands:
        pre_run = run_eda_command(pre_command)
        if not pre_run.ok:
            raise RuntimeError(f"PEX pre-command failed rc={pre_run.returncode}: {' '.join(pre_run.command)}")
    run = run_eda_command(plan.command)
    report = parse_pex_report(plan.report_path)
    extracted = str(report.extracted_netlist or "").strip()
    if not extracted:
        report = PexReport(
            extracted_netlist=plan.extracted_netlist_path,
            parasitic_count=report.parasitic_count,
            net_cap_f=dict(report.net_cap_f),
            net_res_ohm=dict(report.net_res_ohm),
        )
    return PexExtractionResult(plan=plan, run=run, report=report)


def _with_pre_commands(plan: PexExtractionPlan, pre_commands: tuple[object, ...]) -> PexExtractionPlan:
    commands = tuple(command for command in pre_commands if isinstance(command, EdaCommand))
    if not commands:
        return plan
    return PexExtractionPlan(
        command=plan.command,
        layout=plan.layout,
        source=plan.source,
        rule_deck=plan.rule_deck,
        report_path=plan.report_path,
        extracted_netlist_path=plan.extracted_netlist_path,
        engine=plan.engine,
        corner=plan.corner,
        format=plan.format,
        pre_commands=commands,
        metadata=plan.metadata,
    )


def _default_calibre_pex_corner(pdk: object | None) -> str:
    extraction_corners = getattr(pdk, "extraction_corners", {})
    if "typ" in extraction_corners:
        return "typ"
    if extraction_corners:
        return str(sorted(str(name) for name in extraction_corners.keys())[0])
    return ""


def _normalized_runset_body_lines(lines: Sequence[object]) -> list[str]:
    normalized: list[str] = []
    for item in lines:
        text = str(item).rstrip()
        if not text:
            if normalized and normalized[-1]:
                normalized.append("")
            continue
        normalized.append(text)
    while normalized and not normalized[-1]:
        normalized.pop()
    return normalized


def _mapping_or_empty(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _merged_stage_metadata(
    calibre_metadata: Mapping[str, object],
    verification_metadata: Mapping[str, object],
    stage_name: str,
) -> dict[str, object]:
    return {
        **dict(_mapping_or_empty(calibre_metadata.get(stage_name))),
        **dict(_mapping_or_empty(verification_metadata.get(stage_name))),
    }


def _build_foundry_verification_stage_spec(
    stage_name: str,
    *,
    stage_metadata: Mapping[str, object],
    rule_deck: str | Path | None,
    runset: str | Path | None,
    required_inputs: tuple[str, ...],
    shared_files: Mapping[str, str],
    stream_required: bool,
    layout_artifact_kind: str,
) -> dict[str, object]:
    resolved_rule_deck = str(rule_deck or stage_metadata.get("rule_deck") or "").strip()
    resolved_runset = str(runset or stage_metadata.get("runset") or "").strip()
    issues: list[str] = []
    if not resolved_rule_deck and not resolved_runset:
        issues.append(f"missing calibre {stage_name.upper()} rule deck or runset")
    if stream_required and not str(shared_files.get("stream_layer_map", "")).strip():
        issues.append(f"{stage_name.upper()} requires a stream layer map for {layout_artifact_kind or 'streamout'} packaging")
    return {
        "engine": "calibre",
        "stage": stage_name,
        "required_inputs": tuple(required_inputs),
        "required_files": {
            "rule_deck": resolved_rule_deck,
            "runset": resolved_runset,
        },
        "ready": not issues,
        "issues": tuple(issues),
    }


def _optional_path(value: object) -> str | Path | None:
    text = str(value or "").strip()
    return text or None


def _normalize_packaging_contract(contract: Mapping[str, object] | None) -> dict[str, object]:
    if not contract:
        return {}
    return {
        "engine": str(contract.get("engine", "")),
        "layout_artifact_kind": str(contract.get("layout_artifact_kind", "")),
        "streamout": dict(_mapping_or_empty(contract.get("streamout"))),
        "pex": dict(_mapping_or_empty(contract.get("pex"))),
        "writeback": dict(_mapping_or_empty(contract.get("writeback"))),
        "pdk": dict(_mapping_or_empty(contract.get("pdk"))),
        "ready": bool(contract.get("ready", False)),
        "issues": tuple(str(issue) for issue in contract.get("issues", ())),
    }
