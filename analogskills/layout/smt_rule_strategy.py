"""Configuration-driven SMT rule ownership for analog layout closure.

The goal is not to translate a foundry Calibre deck verbatim into one huge SMT
problem.  This module records which rule families should be owned by the main
placement SMT problem, represented there only as routing/resource proxies,
deferred to local SMT repair, handled by A*/ECO, or tracked as signoff-only
post-processing for a selected closure mode.
"""
from __future__ import annotations

from copy import deepcopy
from fnmatch import fnmatchcase
from typing import Mapping


SMT_RULE_MODES = ("full", "hybrid", "critical")
SMT_RULE_OWNERS = ("main_smt_hard", "main_smt_proxy", "local_smt", "a_star", "eco", "signoff_only", "ignore")
LEGACY_SMT_RULE_OWNERS = ("main_smt", "external_eco")
SMT_RULE_OWNER_SCHEMA_VERSION = "smt_rule_ownership/v2"

SMT_RULE_OWNER_DESCRIPTIONS = {
    "main_smt_hard": "Hard structural constraints encoded directly in the global placement/packing SMT.",
    "main_smt_proxy": "Global SMT proxy constraints/objectives such as access/route envelopes and resource demand.",
    "local_smt": "Detailed local geometry choices solved after placement, e.g. terminal escape, via, and spacing repair.",
    "a_star": "Detailed noncritical routing delegated to A*/structured routing.",
    "eco": "Mechanical or localized ECO repair that should not expand the global SMT search space.",
    "signoff_only": "Density, marker, LVS-assist, and signoff bookkeeping not intended to define electrical topology.",
    "ignore": "Warnings or waivable reminders tracked but not used for closure decisions.",
}

DEFAULT_TIMEOUT_MS_BY_MODE = {
    "full": 120_000,
    "hybrid": 60_000,
    "critical": 30_000,
}

DEFAULT_RULE_FAMILIES: Mapping[str, Mapping[str, object]] = {
    "device_non_overlap": {
        "rule_ids": ("abstract.non_overlap",),
        "owner_by_mode": {"full": "main_smt_hard", "hybrid": "main_smt_hard", "critical": "main_smt_hard"},
        "description": "Device/group non-overlap is structural and belongs in the placement SMT.",
    },
    "grid_snapping": {
        "rule_ids": ("abstract.grid", "smt.grid", "stream.grid"),
        "owner_by_mode": {"full": "main_smt_hard", "hybrid": "main_smt_hard", "critical": "main_smt_hard"},
        "description": "Use a legal grid in SMT variables; residual stream/grid markers can still be mechanically snapped.",
    },
    "calibre_grid_marker": {
        "rule_ids": ("G.*",),
        "owner_by_mode": {"full": "eco", "hybrid": "eco", "critical": "eco"},
        "description": "Calibre G.* stream/grid markers are repaired by mechanical snapping/stream cleanup rather than enlarging the placement SMT.",
    },
    "symmetry_matching": {
        "rule_ids": ("matching.symmetry", "matching.mirror", "matching.common_centroid"),
        "owner_by_mode": {"full": "main_smt_hard", "hybrid": "main_smt_hard", "critical": "main_smt_hard"},
        "description": "Symmetry and matching constraints directly shape analog placement quality.",
    },
    "well_domain_spacing": {
        "rule_ids": ("NW.S.*", "DNW.S.*", "OD2.S.*"),
        "owner_by_mode": {"full": "main_smt_hard", "hybrid": "main_smt_hard", "critical": "main_smt_hard"},
        "description": "Well/deep-nwell/domain spacing changes group placement and should not be a late ECO.",
    },
    "guard_ring_keepout": {
        "rule_ids": ("guard_ring.keepout", "tap.keepout", "LUP.*"),
        "owner_by_mode": {"full": "main_smt_proxy", "hybrid": "main_smt_proxy", "critical": "local_smt"},
        "description": "Guard-ring and tap envelopes reserve physical space; detailed stitching can remain downstream.",
    },
    "pcell_access_keepout": {
        "rule_ids": ("pcell_access.keepout", "native_pcell.envelope"),
        "owner_by_mode": {"full": "main_smt_proxy", "hybrid": "main_smt_proxy", "critical": "local_smt"},
        "description": "PCell terminal/access envelopes strongly affect placement and first escape routing.",
    },
    "critical_route_topology": {
        "rule_ids": ("critical.*", "matched_sense.*", "supply.*"),
        "owner_by_mode": {"full": "main_smt_proxy", "hybrid": "main_smt_proxy", "critical": "main_smt_proxy"},
        "description": "Critical net topology, corridor capacity, and route-resource demand belong in SMT.",
    },
    "critical_route_width_spacing": {
        "rule_ids": ("M*.W.*", "M*.S.*"),
        "owner_by_mode": {"full": "local_smt", "hybrid": "local_smt", "critical": "local_smt"},
        "description": "Exact metal width/spacing is local geometry; main SMT should reserve envelopes, not own polygon-level routing.",
    },
    "pin_access_reachability": {
        "rule_ids": ("pin_access.*", "terminal_access.*"),
        "owner_by_mode": {"full": "local_smt", "hybrid": "local_smt", "critical": "local_smt"},
        "description": "Terminals must have legal escape options; choose detailed access locally against the placed context.",
    },
    "terminal_escape_direction": {
        "rule_ids": ("terminal_escape.*", "pcell_access.escape"),
        "owner_by_mode": {"full": "local_smt", "hybrid": "local_smt", "critical": "local_smt"},
        "description": "Escape direction is a local SMT/ECO choice constrained by the global placement envelope.",
    },
    "via_enclosure": {
        "rule_ids": ("VIA*.EN.*", "CO.EN.*"),
        "owner_by_mode": {"full": "local_smt", "hybrid": "local_smt", "critical": "local_smt"},
        "description": "Via enclosure is local geometry and should be solved with terminal/via placement.",
    },
    "redundant_via_array": {
        "rule_ids": ("VIA*.R.*", "CO.R.*"),
        "owner_by_mode": {"full": "local_smt", "hybrid": "local_smt", "critical": "local_smt"},
        "description": "Via-array requirements are local discrete choices and good candidates for local SMT fallback.",
    },
    "wide_metal_spacing": {
        "rule_ids": ("M*.S.3", "M*.S.13", "wide_metal.spacing"),
        "owner_by_mode": {"full": "local_smt", "hybrid": "local_smt", "critical": "local_smt"},
        "description": "Wide-metal spacing depends on route width/parallel length and is handled by local detailed routing.",
    },
    "same_net_notch_gap": {
        "rule_ids": ("M*.S.1", "same_net.notch", "same_net.gap_fill"),
        "owner_by_mode": {"full": "eco", "hybrid": "eco", "critical": "eco"},
        "description": "Same-net notch/gap fixes should be legal choices, not coordinate-specific patches.",
    },
    "noncritical_route_resource": {
        "rule_ids": ("noncritical.resource",),
        "owner_by_mode": {"full": "main_smt_proxy", "hybrid": "main_smt_proxy", "critical": "a_star"},
        "description": "Hybrid still reserves noncritical route resources; critical mode lets detail routing handle them.",
    },
    "noncritical_route_exact_path": {
        "rule_ids": ("noncritical.path", "A*.route"),
        "owner_by_mode": {"full": "local_smt", "hybrid": "a_star", "critical": "a_star"},
        "description": "Exact noncritical paths are deferred from global SMT; full mode may use local SMT, hybrid/critical use A*/detail routing.",
    },
    "density_dummy_fill": {
        "rule_ids": ("*.DN.*", "DM*.R.*", "dummy_fill.*", "CSR.R.1"),
        "owner_by_mode": {"full": "signoff_only", "hybrid": "signoff_only", "critical": "signoff_only"},
        "description": "Density and dummy fill are signoff post-processing, not placement/routing search constraints.",
    },
    "lvs_assist_marker": {
        "rule_ids": ("lvs.assist", "text_port.*", "marker.*"),
        "owner_by_mode": {"full": "signoff_only", "hybrid": "signoff_only", "critical": "signoff_only"},
        "description": "LVS assist/marker structures should not define the final electrical layout topology.",
    },
    "warnings_waivers": {
        "rule_ids": ("*.WARN.*", "*WARNING*", "IO_CONNECT_*", "MOM.R.2"),
        "owner_by_mode": {"full": "ignore", "hybrid": "ignore", "critical": "ignore"},
        "description": "Warnings/waivable reminders are tracked but not allowed to dominate SMT closure.",
    },
}


def resolve_smt_rule_strategy(
    pdk: object | None,
    block: str,
    *,
    mode: object | None = None,
) -> dict[str, object]:
    """Resolve block SMT mode and rule-family ownership from PDK metadata."""

    root = _mapping(_metadata(pdk).get("smt_design_rules", {}))
    global_strategy = _mapping(root.get("rule_strategy", {}))
    block_rules = _mapping(root.get(str(block), {}))
    block_strategy = _mapping(block_rules.get("rule_strategy", {}))

    selected_mode = _normalize_mode(
        mode
        if mode is not None
        else block_strategy.get(
            "mode",
            _mapping(global_strategy.get("mode_by_block", {})).get(
                str(block),
                global_strategy.get("mode", global_strategy.get("default_mode", "hybrid")),
            ),
        )
    )

    timeout_ms_by_mode = {
        **DEFAULT_TIMEOUT_MS_BY_MODE,
        **{
            _normalize_mode(key): _positive_int(value, DEFAULT_TIMEOUT_MS_BY_MODE[_normalize_mode(key)])
            for key, value in _mapping(global_strategy.get("timeout_ms_by_mode", {})).items()
        },
        **{
            _normalize_mode(key): _positive_int(value, DEFAULT_TIMEOUT_MS_BY_MODE[_normalize_mode(key)])
            for key, value in _mapping(block_strategy.get("timeout_ms_by_mode", {})).items()
        },
    }
    timeout_ms = _positive_int(
        block_strategy.get("timeout_ms", global_strategy.get("timeout_ms", timeout_ms_by_mode[selected_mode])),
        timeout_ms_by_mode[selected_mode],
    )

    family_rows = _merge_rule_families(
        DEFAULT_RULE_FAMILIES,
        _mapping(global_strategy.get("rule_families", {})),
        _mapping(block_strategy.get("rule_families", {})),
    )
    owner_by_family: dict[str, str] = {}
    details: dict[str, dict[str, object]] = {}
    by_owner: dict[str, list[str]] = {owner: [] for owner in SMT_RULE_OWNERS}
    for family in sorted(family_rows):
        row = family_rows[family]
        owner = _owner_for_mode(row, selected_mode)
        owner_by_family[family] = owner
        by_owner.setdefault(owner, []).append(family)
        details[family] = {
            **row,
            "owner": owner,
            "rule_ids": tuple(str(item) for item in tuple(row.get("rule_ids", ()) or ())),
        }

    main_smt_hard = tuple(by_owner.get("main_smt_hard", ()))
    main_smt_proxy = tuple(by_owner.get("main_smt_proxy", ()))
    eco = tuple(by_owner.get("eco", ()))
    signoff_only = tuple(by_owner.get("signoff_only", ()))
    main_smt_legacy = tuple((*main_smt_hard, *main_smt_proxy))
    external_eco_legacy = tuple((*eco, *signoff_only))
    return {
        "owner_schema_version": SMT_RULE_OWNER_SCHEMA_VERSION,
        "mode": selected_mode,
        "timeout_ms": timeout_ms,
        "configuration_path": f"metadata.smt_design_rules.rule_strategy + metadata.smt_design_rules.{block}.rule_strategy",
        "owner_descriptions": dict(SMT_RULE_OWNER_DESCRIPTIONS),
        "rule_family_owners": dict(owner_by_family),
        "rule_families_by_owner": {owner: tuple(items) for owner, items in by_owner.items()},
        "legacy_rule_families_by_owner": {
            "main_smt": main_smt_legacy,
            "external_eco": external_eco_legacy,
        },
        "main_smt_hard_rule_families": main_smt_hard,
        "main_smt_proxy_rule_families": main_smt_proxy,
        "main_smt_rule_families": main_smt_legacy,
        "local_smt_rule_families": tuple(by_owner.get("local_smt", ())),
        "a_star_rule_families": tuple(by_owner.get("a_star", ())),
        "eco_rule_families": eco,
        "signoff_only_rule_families": signoff_only,
        "external_eco_rule_families": external_eco_legacy,
        "ignored_rule_families": tuple(by_owner.get("ignore", ())),
        "rule_family_details": details,
    }


def classify_drc_rule_name(rule_name: object, strategy: Mapping[str, object]) -> dict[str, object]:
    """Classify a Calibre DRC rule name using a resolved SMT rule strategy."""

    name = str(rule_name or "").strip()
    details = _mapping(strategy.get("rule_family_details", {}))
    owners = _mapping(strategy.get("rule_family_owners", {}))
    best: tuple[int, str, str, str] | None = None
    for family, raw_detail in details.items():
        detail = _mapping(raw_detail)
        for raw_pattern in tuple(detail.get("rule_ids", ()) or ()):
            pattern = str(raw_pattern or "").strip()
            if not pattern:
                continue
            if not _rule_pattern_matches(name, pattern):
                continue
            score = _rule_pattern_score(pattern)
            candidate = (score, str(family), _normalize_owner(detail.get("owner", owners.get(str(family), "local_smt"))), pattern)
            if best is None or candidate > best:
                best = candidate
    if best is None:
        return {
            "rule": name,
            "family": "unclassified_drc",
            "owner": "local_smt",
            "matched_rule_id": "",
            "classified": False,
        }
    _score, family, owner, pattern = best
    return {
        "rule": name,
        "family": family,
        "owner": owner,
        "matched_rule_id": pattern,
        "classified": True,
    }


def classify_drc_rule_counts(
    rule_counts: Mapping[str, int] | tuple[tuple[str, int], ...] | list[tuple[str, int]],
    strategy: Mapping[str, object],
) -> dict[str, object]:
    """Group Calibre rule/count pairs by canonical owner and legacy rollups."""

    if isinstance(rule_counts, Mapping):
        items = tuple((str(rule), int(count)) for rule, count in rule_counts.items())
    else:
        items = tuple((str(rule), int(count)) for rule, count in tuple(rule_counts or ()))
    by_owner: dict[str, dict[str, object]] = {
        owner: {"total_count": 0, "rules": []}
        for owner in SMT_RULE_OWNERS
    }
    rows: list[dict[str, object]] = []
    for rule, count in items:
        if count <= 0:
            continue
        classification = classify_drc_rule_name(rule, strategy)
        owner = _normalize_owner(classification.get("owner", "local_smt"))
        row = {**classification, "count": int(count)}
        rows.append(row)
        bucket = by_owner.setdefault(owner, {"total_count": 0, "rules": []})
        bucket["total_count"] = int(bucket.get("total_count", 0) or 0) + int(count)
        rules = bucket.setdefault("rules", [])
        if isinstance(rules, list):
            rules.append(row)
    legacy_by_owner = {
        "main_smt": _merge_owner_count_buckets(by_owner, ("main_smt_hard", "main_smt_proxy")),
        "external_eco": _merge_owner_count_buckets(by_owner, ("eco", "signoff_only")),
    }
    return {
        "owner_schema_version": SMT_RULE_OWNER_SCHEMA_VERSION,
        "mode": strategy.get("mode", "hybrid"),
        "total_count": sum(int(row["count"]) for row in rows),
        "classified_count": sum(int(row["count"]) for row in rows if bool(row.get("classified", False))),
        "unclassified_count": sum(int(row["count"]) for row in rows if not bool(row.get("classified", False))),
        "by_owner": by_owner,
        "legacy_by_owner": legacy_by_owner,
        "rules": tuple(rows),
    }


def _merge_owner_count_buckets(by_owner: Mapping[str, Mapping[str, object]], owners: tuple[str, ...]) -> dict[str, object]:
    rows: list[object] = []
    total = 0
    for owner in owners:
        bucket = _mapping(by_owner.get(owner, {}))
        total += int(bucket.get("total_count", 0) or 0)
        raw_rules = bucket.get("rules", ())
        if isinstance(raw_rules, list):
            rows.extend(raw_rules)
        elif isinstance(raw_rules, tuple):
            rows.extend(raw_rules)
    return {"total_count": total, "rules": rows}


def parse_calibre_drc_summary_rule_counts(text: str) -> tuple[tuple[str, int], ...]:
    """Parse nonzero RULECHECK counts from a Calibre DRC summary report."""

    import re

    rows: list[tuple[str, int]] = []
    for line in str(text or "").splitlines():
        match = re.match(r"RULECHECK\s+(.+?)\s+\.*\s+TOTAL Result Count\s*=\s*(\d+)", line)
        if not match:
            continue
        count = int(match.group(2))
        if count:
            rows.append((match.group(1).strip(), count))
    return tuple(rows)


def _merge_rule_families(*sources: Mapping[str, object]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for source in sources:
        for name, raw_row in source.items():
            if not isinstance(raw_row, Mapping):
                continue
            row = deepcopy(dict(raw_row))
            base = deepcopy(result.get(str(name), {}))
            if isinstance(base.get("owner_by_mode"), Mapping) or isinstance(row.get("owner_by_mode"), Mapping):
                row["owner_by_mode"] = {
                    **dict(base.get("owner_by_mode", {}) if isinstance(base.get("owner_by_mode"), Mapping) else {}),
                    **dict(row.get("owner_by_mode", {}) if isinstance(row.get("owner_by_mode"), Mapping) else {}),
                }
            base.update(row)
            result[str(name)] = base
    return result


def _rule_pattern_matches(rule_name: str, pattern: str) -> bool:
    if rule_name == pattern:
        return True
    if rule_name.startswith(pattern + ":") or rule_name.startswith(pattern + "__"):
        return True
    if "*" in pattern or "?" in pattern or "[" in pattern:
        return fnmatchcase(rule_name, pattern)
    return False


def _rule_pattern_score(pattern: str) -> int:
    wildcard_count = pattern.count("*") + pattern.count("?") + pattern.count("[")
    return max(0, len(pattern) * 4 - wildcard_count * 25)


def _owner_for_mode(row: Mapping[str, object], mode: str) -> str:
    by_mode = _mapping(row.get("owner_by_mode", {}))
    return _normalize_owner(by_mode.get(mode, row.get("owner", row.get("default_owner", "eco"))))


def _normalize_mode(value: object) -> str:
    text = str(value or "hybrid").strip().lower()
    if text in {"all", "full_smt", "monolithic"}:
        return "full"
    if text in {"crit", "critical_only"}:
        return "critical"
    if text not in SMT_RULE_MODES:
        return "hybrid"
    return text


def _normalize_owner(value: object) -> str:
    text = str(value or "eco").strip().lower().replace("-", "_")
    aliases = {
        "main": "main_smt_proxy",
        "smt": "main_smt_proxy",
        "main_smt": "main_smt_proxy",
        "global_smt": "main_smt_proxy",
        "global_smt_hard": "main_smt_hard",
        "hard_smt": "main_smt_hard",
        "placement_smt": "main_smt_hard",
        "proxy_smt": "main_smt_proxy",
        "route_proxy": "main_smt_proxy",
        "local": "local_smt",
        "repair_smt": "local_smt",
        "astar": "a_star",
        "a*": "a_star",
        "external_eco": "eco",
        "external": "eco",
        "post": "eco",
        "post_process": "eco",
        "signoff": "signoff_only",
        "signoff_eco": "signoff_only",
        "external_signoff": "signoff_only",
        "waive": "ignore",
        "ignored": "ignore",
    }
    text = aliases.get(text, text)
    if text not in SMT_RULE_OWNERS:
        return "eco"
    return text


def _metadata(pdk: object | None) -> Mapping[str, object]:
    raw = getattr(pdk, "metadata", {}) if pdk is not None else {}
    return raw if isinstance(raw, Mapping) else {}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _positive_int(value: object, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))
