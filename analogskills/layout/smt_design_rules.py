"""Configuration-driven design-rule inputs for SMT physical solvers.

The SMT solvers should consume compact integer abstractions: sites, tracks,
capacities, and route demands.  This module owns the translation from PDK
metadata to those abstractions, keeping foundry/design-rule numbers out of the
solver implementations themselves.
"""
from __future__ import annotations

from math import ceil
from typing import Mapping

from analogskills.pdk import PdkConfig


DEFAULT_STRONGARM_DEVICE_PLACEMENT_NM = {
    "intra_row_spacing_nm": 180,
    "row_spacing_nm": 250,
    "max_matched_pair_gap_nm": 180,
    "max_row_spacing_nm": 250,
}

DEFAULT_STRONGARM_CRITICAL_TRACK_DEMAND = {
    "INPUT_DIFF": 2,
    "OUTPUT_REGEN_DIFF": 3,
    "TAIL_CURRENT": 2,
}

DEFAULT_STRONGARM_NONCRITICAL_TRACK_DEMAND = {
    "CLK": 1,
    "RST": 1,
}


def smt_site_nm(pdk: PdkConfig | None, *, default: int = 10) -> int:
    """Return the configured SMT site quantum in nanometers."""

    root = _smt_design_rules_root(pdk)
    try:
        value = int(root.get("site_nm", default) or default)
    except (TypeError, ValueError, AttributeError):
        value = default
    return max(value, 1)


def nm_to_sites(value_nm: object, *, site_nm: int, minimum: int = 0) -> int:
    """Convert a non-negative nanometer rule to integer SMT sites."""

    try:
        value = float(value_nm or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return max(int(ceil(max(value, 0.0) / max(site_nm, 1))), minimum)


def strongarm_device_placement_rule_sites(
    pdk: PdkConfig | None,
    *,
    site_nm: int | None = None,
) -> dict[str, object]:
    """Return StrongARM fixed-size/nf placement rules in site units.

    New configuration path:
      metadata.smt_design_rules.strongarm.device_placement

    Legacy fallback:
      metadata.smt_placement.strongarm_characterized_nf
    """

    site = smt_site_nm(pdk) if site_nm is None else max(int(site_nm), 1)
    values = dict(DEFAULT_STRONGARM_DEVICE_PLACEMENT_NM)
    strongarm_rules = _strongarm_smt_rules(pdk)
    device_rules = _mapping(strongarm_rules.get("device_placement", {}))
    if device_rules:
        _overlay_numeric(values, device_rules, values.keys())
    else:
        metadata = _metadata(pdk)
        legacy = _mapping(_mapping(metadata.get("smt_placement", {})).get("strongarm_characterized_nf", {}))
        _overlay_numeric(values, legacy, values.keys())

    result = {key.replace("_nm", "_sites"): nm_to_sites(value, site_nm=site) for key, value in values.items()}
    row_spacing_overrides_nm = _mapping(device_rules.get("row_spacing_overrides_nm", {}))
    if not row_spacing_overrides_nm:
        metadata = _metadata(pdk)
        legacy = _mapping(_mapping(metadata.get("smt_placement", {})).get("strongarm_characterized_nf", {}))
        row_spacing_overrides_nm = _mapping(legacy.get("row_spacing_overrides_nm", {}))
    row_spacing_overrides_sites = {
        transition: nm_to_sites(value, site_nm=site)
        for key, value in row_spacing_overrides_nm.items()
        if (transition := _parse_row_transition(key)) is not None
    }
    return {
        "spacing_sites": result["intra_row_spacing_sites"],
        "row_spacing_sites": result["row_spacing_sites"],
        "max_matched_pair_gap_sites": result["max_matched_pair_gap_sites"],
        "max_row_spacing_sites": result["max_row_spacing_sites"],
        "row_spacing_overrides_sites": row_spacing_overrides_sites,
    }


def strongarm_hierarchical_rule_config(
    pdk: PdkConfig | None,
    *,
    site_nm: int | None = None,
) -> dict[str, object]:
    """Return StrongARM hierarchical placement/routing SMT config.

    The returned values are intentionally solver-native integer abstractions.
    """

    site = smt_site_nm(pdk) if site_nm is None else max(int(site_nm), 1)
    strongarm_rules = _strongarm_smt_rules(pdk)
    placement = _mapping(strongarm_rules.get("hierarchical_placement", {}))
    routing = _mapping(strongarm_rules.get("routing_resource", {}))
    corridor_defaults = _mapping(routing.get("corridor_defaults", {}))
    corridor_overrides = _mapping(routing.get("corridors", {}))

    placement_spacing_tracks = nm_to_sites(placement.get("minimum_group_spacing_nm", 0), site_nm=site)
    target_aspect = _mapping(placement.get("target_aspect", {}))
    target_aspect_num = _positive_int(target_aspect.get("num", 3), 3)
    target_aspect_den = _positive_int(target_aspect.get("den", 2), 2)
    default_pitch_sites = nm_to_sites(
        corridor_defaults.get("pitch_nm", routing.get("corridor_pitch_nm", site)),
        site_nm=site,
        minimum=1,
    )
    default_channel_gap_sites = nm_to_sites(
        corridor_defaults.get("channel_gap_nm", routing.get("default_channel_gap_nm", site)),
        site_nm=site,
        minimum=0,
    )
    default_fixed_reserved_tracks = _nonnegative_int(
        corridor_defaults.get("fixed_reserved_tracks", routing.get("default_fixed_reserved_tracks", 1)),
        1,
    )
    default_estimated_noncritical_tracks = _nonnegative_int(
        corridor_defaults.get("estimated_noncritical_tracks", routing.get("default_estimated_noncritical_tracks", 0)),
        0,
    )
    default_base_capacity_tracks = _nonnegative_int(
        corridor_defaults.get("base_capacity_tracks", routing.get("default_base_capacity_tracks", 0)),
        0,
    )
    default_capacity_consumes_gap = bool(corridor_defaults.get("capacity_consumes_gap", routing.get("capacity_consumes_gap", False)))
    default_require_orthogonal_overlap = bool(corridor_defaults.get("require_orthogonal_overlap", routing.get("require_orthogonal_overlap", True)))

    def corridor_rule(name: str) -> dict[str, object]:
        row = _mapping(corridor_overrides.get(name, {}))
        return {
            "base_capacity_tracks": _nonnegative_int(row.get("base_capacity_tracks", default_base_capacity_tracks), default_base_capacity_tracks),
            "estimated_noncritical_tracks": _nonnegative_int(row.get("estimated_noncritical_tracks", default_estimated_noncritical_tracks), default_estimated_noncritical_tracks),
            "fixed_reserved_tracks": _nonnegative_int(row.get("fixed_reserved_tracks", default_fixed_reserved_tracks), default_fixed_reserved_tracks),
            "pitch_sites": nm_to_sites(row.get("pitch_nm", default_pitch_sites * site), site_nm=site, minimum=1),
            "require_orthogonal_overlap": bool(row.get("require_orthogonal_overlap", default_require_orthogonal_overlap)),
            "capacity_consumes_gap": bool(row.get("capacity_consumes_gap", default_capacity_consumes_gap)),
            "channel_gap_sites": nm_to_sites(row.get("channel_gap_nm", default_channel_gap_sites * site), site_nm=site, minimum=0),
        }

    critical_track_demand = dict(DEFAULT_STRONGARM_CRITICAL_TRACK_DEMAND)
    _overlay_int(critical_track_demand, _mapping(routing.get("critical_track_demand", {})))
    noncritical_track_demand = dict(DEFAULT_STRONGARM_NONCRITICAL_TRACK_DEMAND)
    _overlay_int(noncritical_track_demand, _mapping(routing.get("noncritical_track_demand", {})))

    return {
        "site_nm": site,
        "placement_spacing_tracks": placement_spacing_tracks,
        "target_aspect_num": target_aspect_num,
        "target_aspect_den": target_aspect_den,
        "corridor_defaults": {
            "pitch_sites": default_pitch_sites,
            "channel_gap_sites": default_channel_gap_sites,
            "fixed_reserved_tracks": default_fixed_reserved_tracks,
            "estimated_noncritical_tracks": default_estimated_noncritical_tracks,
            "base_capacity_tracks": default_base_capacity_tracks,
            "capacity_consumes_gap": default_capacity_consumes_gap,
            "require_orthogonal_overlap": default_require_orthogonal_overlap,
        },
        "corridors": {
            name: corridor_rule(name)
            for name in ("C_TAIL_INPUT", "C_INPUT_LATCH", "C_LATCH_RESET")
        },
        "critical_track_demand": critical_track_demand,
        "noncritical_track_demand": noncritical_track_demand,
        "configuration_path": "metadata.smt_design_rules.strongarm",
        "enabled": bool(_smt_design_rules_root(pdk).get("enabled", False)),
    }


def _metadata(pdk: PdkConfig | None) -> Mapping[str, object]:
    if pdk is None:
        return {}
    metadata = getattr(pdk, "metadata", {})
    return metadata if isinstance(metadata, Mapping) else {}


def _smt_design_rules_root(pdk: PdkConfig | None) -> Mapping[str, object]:
    root = _metadata(pdk).get("smt_design_rules", {})
    if not isinstance(root, Mapping):
        return {}
    if root and not bool(root.get("enabled", True)):
        return {}
    return root


def _strongarm_smt_rules(pdk: PdkConfig | None) -> Mapping[str, object]:
    root = _smt_design_rules_root(pdk)
    strongarm = root.get("strongarm", {})
    return strongarm if isinstance(strongarm, Mapping) else {}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _overlay_numeric(target: dict[str, object], source: Mapping[str, object], keys: object) -> None:
    for key in keys:
        if key in source:
            try:
                target[str(key)] = float(source[key] or 0.0)
            except (TypeError, ValueError):
                pass


def _overlay_int(target: dict[str, int], source: Mapping[str, object]) -> None:
    for key, value in source.items():
        target[str(key)] = _nonnegative_int(value, target.get(str(key), 0))


def _parse_row_transition(key: object) -> tuple[int, int] | None:
    if isinstance(key, (tuple, list)) and len(key) == 2:
        try:
            return (int(key[0]), int(key[1]))
        except (TypeError, ValueError):
            return None
    text = str(key or "").strip()
    for sep in ("->", ":", ",", "-"):
        if sep not in text:
            continue
        left, right = text.split(sep, 1)
        try:
            return (int(left.strip()), int(right.strip()))
        except (TypeError, ValueError):
            return None
    return None


def _nonnegative_int(value: object, default: int) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return max(int(default), 0)


def _positive_int(value: object, default: int) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return max(int(default), 1)
