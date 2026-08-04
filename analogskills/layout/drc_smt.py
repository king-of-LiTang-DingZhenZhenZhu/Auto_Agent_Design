"""Foundry-rule encoding for local SMT access and via selection."""
from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from typing import Mapping

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None


@dataclass(frozen=True)
class SmtDrcRuleProfile:
    site_nm: int
    min_width_sites: Mapping[str, int]
    min_spacing_sites: Mapping[str, int]
    enclosure_sites: Mapping[tuple[str, str], int]
    eol_spacing_sites: Mapping[str, int] = field(default_factory=dict)
    array_spacing_sites: Mapping[str, int] = field(default_factory=dict)
    extension_sites: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def from_pdk(cls, pdk: object, *, site_nm: int = 1) -> "SmtDrcRuleProfile":
        if site_nm <= 0:
            raise ValueError("site_nm must be positive")
        rules = getattr(pdk, "rules")
        widths = {str(layer): ceil(int(value) / site_nm) for layer, value in dict(getattr(rules, "min_width_nm", {}) or {}).items()}
        spacings = {str(layer): ceil(int(value) / site_nm) for layer, value in dict(getattr(rules, "min_spacing_nm", {}) or {}).items()}
        enclosure = {}
        for key, value in dict(getattr(rules, "enclosure_nm", {}) or {}).items():
            parts = str(key).split("_")
            if len(parts) == 2:
                enclosure[(parts[0], parts[1])] = ceil(int(value) / site_nm)
        eol = {str(layer): ceil(int(value) / site_nm) for layer, value in dict(getattr(rules, "eol_spacing_nm", {}) or {}).items()}
        arrays = {str(layer): ceil(int(value) / site_nm) for layer, value in dict(getattr(rules, "array_spacing_nm", {}) or {}).items()}
        extensions = {str(layer): ceil(int(value) / site_nm) for layer, value in dict(getattr(rules, "extension_nm", {}) or {}).items()}
        return cls(site_nm, widths, spacings, enclosure, eol, arrays, extensions)


@dataclass(frozen=True)
class SmtPruningPolicy:
    """Cheap, configuration-driven filters applied before Z3 variables exist."""

    enabled_checks: tuple[str, ...] = ("width", "via_enclosure", "bounds", "intrinsic_cost")
    max_intrinsic_drc_cost: int | None = None
    routing_bounds_sites: tuple[int, int, int, int] | None = None
    disabled_layers: tuple[str, ...] = ()

    @classmethod
    def from_pdk(cls, pdk: object) -> "SmtPruningPolicy":
        metadata = getattr(pdk, "metadata", {})
        router = dict(metadata.get("smt_router", {}) or {}) if isinstance(metadata, Mapping) else {}
        pruning = dict(router.get("pruning", {}) or {})
        bounds = pruning.get("routing_bounds_sites")
        parsed_bounds = tuple(int(v) for v in bounds) if isinstance(bounds, (list, tuple)) and len(bounds) == 4 else None
        max_cost = pruning.get("max_intrinsic_drc_cost")
        return cls(
            enabled_checks=tuple(str(v) for v in pruning.get("enabled_checks", ("width", "via_enclosure", "bounds", "intrinsic_cost"))),
            max_intrinsic_drc_cost=int(max_cost) if max_cost is not None else None,
            routing_bounds_sites=parsed_bounds,  # type: ignore[arg-type]
            disabled_layers=tuple(str(v) for v in pruning.get("disabled_layers", ())),
        )


@dataclass(frozen=True)
class DrcAccessCandidate:
    name: str
    terminal: str
    net: str
    layer: str
    bbox_sites: tuple[int, int, int, int]
    via_layer: str = ""
    via_bbox_sites: tuple[int, int, int, int] | None = None
    route_cost: int = 0
    intrinsic_drc_cost: int = 0


@dataclass(frozen=True)
class DrcAccessSolution:
    selected_by_terminal: Mapping[str, DrcAccessCandidate]
    total_drc_cost: int
    total_route_cost: int


def solve_drc_access_assignment(
    candidates: tuple[DrcAccessCandidate, ...],
    rules: SmtDrcRuleProfile,
    *,
    pruning: SmtPruningPolicy | None = None,
) -> DrcAccessSolution:
    """Select one access per terminal with hard cross-net spacing rules."""
    if z3 is None:  # pragma: no cover
        raise RuntimeError("z3-solver is required for DRC access assignment")
    pruning = pruning or SmtPruningPolicy()
    by_terminal: dict[str, list[DrcAccessCandidate]] = {}
    for candidate in candidates:
        if not candidate.terminal or not candidate.net or not candidate.layer:
            raise ValueError("access candidate is missing terminal/net/layer")
        if not _positive_bbox(candidate.bbox_sites):
            raise ValueError(f"candidate {candidate.name} has an invalid bbox")
        if _candidate_prune_reason(candidate, rules, pruning):
            continue
        by_terminal.setdefault(candidate.terminal, []).append(candidate)
    if not by_terminal:
        raise ValueError("no legal access candidates")
    opt = z3.Optimize()
    selected = {candidate.name: z3.Bool(f"access__{candidate.name}") for rows in by_terminal.values() for candidate in rows}
    for terminal, rows in by_terminal.items():
        opt.add(z3.PbEq([(selected[row.name], 1) for row in rows], 1))
    flat = tuple(candidate for rows in by_terminal.values() for candidate in rows)
    for index, left in enumerate(flat):
        for right in flat[index + 1:]:
            if left.terminal == right.terminal:
                continue
            if _candidate_conflict(left, right, rules):
                opt.add(z3.Or(z3.Not(selected[left.name]), z3.Not(selected[right.name])))
    drc_cost = z3.Sum([z3.If(selected[row.name], row.intrinsic_drc_cost, 0) for row in flat])
    route_cost = z3.Sum([z3.If(selected[row.name], row.route_cost, 0) for row in flat])
    opt.minimize(drc_cost)
    opt.minimize(route_cost)
    if opt.check() != z3.sat:
        raise ValueError("no DRC-compatible terminal access assignment")
    model = opt.model()
    chosen = {terminal: next(row for row in rows if z3.is_true(model.eval(selected[row.name], model_completion=True))) for terminal, rows in by_terminal.items()}
    return DrcAccessSolution(chosen, sum(row.intrinsic_drc_cost for row in chosen.values()), sum(row.route_cost for row in chosen.values()))


def prune_drc_access_candidates(
    candidates: tuple[DrcAccessCandidate, ...], rules: SmtDrcRuleProfile, policy: SmtPruningPolicy,
) -> tuple[tuple[DrcAccessCandidate, ...], Mapping[str, str]]:
    """Return survivors and deterministic rejection reasons without invoking Z3."""
    survivors: list[DrcAccessCandidate] = []
    rejected: dict[str, str] = {}
    for candidate in candidates:
        reason = _candidate_prune_reason(candidate, rules, policy)
        if reason:
            rejected[candidate.name] = reason
        else:
            survivors.append(candidate)
    return tuple(survivors), rejected


def _candidate_prune_reason(candidate: DrcAccessCandidate, rules: SmtDrcRuleProfile, policy: SmtPruningPolicy) -> str:
    checks = set(policy.enabled_checks)
    if candidate.layer in policy.disabled_layers:
        return "disabled_layer"
    if "width" in checks:
        min_width = int(rules.min_width_sites.get(candidate.layer, 0))
        if min(candidate.bbox_sites[2] - candidate.bbox_sites[0], candidate.bbox_sites[3] - candidate.bbox_sites[1]) < min_width:
            return "min_width"
    if "intrinsic_cost" in checks and policy.max_intrinsic_drc_cost is not None and candidate.intrinsic_drc_cost > policy.max_intrinsic_drc_cost:
        return "intrinsic_drc_cost"
    if "bounds" in checks and policy.routing_bounds_sites is not None:
        bounds, box = policy.routing_bounds_sites, candidate.bbox_sites
        if box[0] < bounds[0] or box[1] < bounds[1] or box[2] > bounds[2] or box[3] > bounds[3]:
            return "routing_bounds"
    if "via_enclosure" in checks and candidate.via_layer and candidate.via_bbox_sites:
        enclosure = int(rules.enclosure_sites.get((candidate.layer, candidate.via_layer), 0))
        metal, via = candidate.bbox_sites, candidate.via_bbox_sites
        if via[0] - metal[0] < enclosure or via[1] - metal[1] < enclosure or metal[2] - via[2] < enclosure or metal[3] - via[3] < enclosure:
            return "via_enclosure"
    return ""


def _candidate_conflict(left: DrcAccessCandidate, right: DrcAccessCandidate, rules: SmtDrcRuleProfile) -> bool:
    same_net = left.net == right.net
    if not same_net and left.layer == right.layer:
        spacing = int(rules.min_spacing_sites.get(left.layer, 0))
        if _expanded_overlap(left.bbox_sites, right.bbox_sites, spacing):
            return True
    if left.via_layer and left.via_layer == right.via_layer and left.via_bbox_sites and right.via_bbox_sites:
        spacing = int((rules.array_spacing_sites if same_net else rules.min_spacing_sites).get(left.via_layer, 0))
        if _expanded_overlap(left.via_bbox_sites, right.via_bbox_sites, spacing):
            return True
    return False


def _expanded_overlap(left: tuple[int, int, int, int], right: tuple[int, int, int, int], spacing: int) -> bool:
    return not (left[2] + spacing <= right[0] or right[2] + spacing <= left[0] or left[3] + spacing <= right[1] or right[3] + spacing <= left[1])


def _positive_bbox(bbox: tuple[int, int, int, int]) -> bool:
    return bbox[2] > bbox[0] and bbox[3] > bbox[1]
