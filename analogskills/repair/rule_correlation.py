"""Geometric correlation between inline DRC facts and Calibre markers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from typing import Iterable, Mapping


BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class RuleCorrelationConfig:
    inline_rule_map: Mapping[str, str] = field(default_factory=dict)
    calibre_rule_map: Mapping[str, str] = field(default_factory=dict)
    bbox_halo_um: float = 0.02
    min_overlap_ratio: float = 0.0
    require_layer_match: bool = True


@dataclass(frozen=True)
class ViolationFact:
    source: str
    rule: str
    family: str
    layer: str
    bbox: BBox | None
    index: int


@dataclass(frozen=True)
class CorrelatedViolation:
    inline_index: int
    calibre_index: int
    family: str
    layer: str
    overlap_ratio: float


@dataclass(frozen=True)
class RuleFamilyCorrelation:
    family: str
    inline_count: int
    calibre_count: int
    matched_count: int
    false_positive_count: int
    false_negative_count: int
    precision: float
    recall: float


@dataclass(frozen=True)
class RuleCorrelationReport:
    inline_count: int
    calibre_count: int
    matched_count: int
    false_positive_count: int
    false_negative_count: int
    precision: float
    recall: float
    matches: tuple[CorrelatedViolation, ...]
    false_positive_inline_indices: tuple[int, ...]
    false_negative_calibre_indices: tuple[int, ...]
    families: tuple[RuleFamilyCorrelation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "analogskills.inline_calibre_correlation/v1",
            **asdict(self),
        }


def correlate_inline_and_calibre_violations(
    inline_violations: Iterable[object],
    calibre_markers: Iterable[object],
    *,
    config: RuleCorrelationConfig | None = None,
) -> RuleCorrelationReport:
    """Return facts only: matches, false positives/negatives, precision and recall."""

    cfg = config or RuleCorrelationConfig()
    inline = tuple(_fact(item, "inline", index, cfg.inline_rule_map) for index, item in enumerate(inline_violations))
    calibre = tuple(_fact(item, "calibre", index, cfg.calibre_rule_map) for index, item in enumerate(calibre_markers))
    candidates: list[tuple[float, int, int, str, str]] = []
    for left in inline:
        for right in calibre:
            if left.family != right.family:
                continue
            if cfg.require_layer_match and left.layer and right.layer and left.layer != right.layer:
                continue
            overlap = _bbox_match_score(left.bbox, right.bbox, max(0.0, cfg.bbox_halo_um))
            if overlap is None or overlap + 1e-12 < max(0.0, cfg.min_overlap_ratio):
                continue
            candidates.append((overlap, left.index, right.index, left.family, left.layer or right.layer))
    used_inline: set[int] = set()
    used_calibre: set[int] = set()
    matches: list[CorrelatedViolation] = []
    for overlap, inline_index, calibre_index, family, layer in sorted(
        candidates, key=lambda item: (-item[0], item[1], item[2])
    ):
        if inline_index in used_inline or calibre_index in used_calibre:
            continue
        used_inline.add(inline_index)
        used_calibre.add(calibre_index)
        matches.append(CorrelatedViolation(inline_index, calibre_index, family, layer, round(overlap, 6)))

    fp = tuple(item.index for item in inline if item.index not in used_inline)
    fn = tuple(item.index for item in calibre if item.index not in used_calibre)
    families = tuple(
        _family_report(family, inline, calibre, matches)
        for family in sorted({item.family for item in (*inline, *calibre)})
    )
    return RuleCorrelationReport(
        inline_count=len(inline),
        calibre_count=len(calibre),
        matched_count=len(matches),
        false_positive_count=len(fp),
        false_negative_count=len(fn),
        precision=_ratio(len(matches), len(inline)),
        recall=_ratio(len(matches), len(calibre)),
        matches=tuple(matches),
        false_positive_inline_indices=fp,
        false_negative_calibre_indices=fn,
        families=families,
    )


def rule_correlation_config_from_pdk(pdk: object) -> RuleCorrelationConfig:
    """Load rule-family mapping and geometric tolerance from PDK metadata."""

    metadata = _value(pdk, "metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    calibre = metadata.get("calibre", {})
    calibre = calibre if isinstance(calibre, Mapping) else {}
    raw = metadata.get("inline_calibre_correlation", calibre.get("inline_calibre_correlation", {}))
    raw = raw if isinstance(raw, Mapping) else {}
    return RuleCorrelationConfig(
        inline_rule_map={str(key): str(value) for key, value in _mapping(raw.get("inline_rule_map")).items()},
        calibre_rule_map={str(key): str(value) for key, value in _mapping(raw.get("calibre_rule_map")).items()},
        bbox_halo_um=float(raw.get("bbox_halo_um", 0.02) or 0.0),
        min_overlap_ratio=float(raw.get("min_overlap_ratio", 0.0) or 0.0),
        require_layer_match=bool(raw.get("require_layer_match", True)),
    )
def _fact(item: object, source: str, index: int, rule_map: Mapping[str, str]) -> ViolationFact:
    rule = str(_value(item, "rule", ""))
    layer = str(_value(item, "layer", "")).upper()
    raw_bbox = _value(item, "bbox", None)
    raw_values = tuple(raw_bbox) if raw_bbox is not None else ()
    bbox = tuple(float(value) for value in raw_values) if len(raw_values) == 4 else None
    return ViolationFact(source, rule, _map_rule(rule, rule_map), layer, bbox, index)


def _map_rule(rule: str, mapping: Mapping[str, str]) -> str:
    upper = str(rule).strip().upper()
    exact = {str(pattern).strip().upper(): str(family).strip().lower() for pattern, family in mapping.items()}
    if upper in exact:
        return exact[upper]
    for pattern, family in exact.items():
        if any(token in pattern for token in "*?[") and fnmatchcase(upper, pattern):
            return family
    return upper.lower()


def _bbox_match_score(left: BBox | None, right: BBox | None, halo: float) -> float | None:
    if left is None or right is None:
        return 1.0
    expanded_left = (left[0] - halo, left[1] - halo, left[2] + halo, left[3] + halo)
    expanded_right = (right[0] - halo, right[1] - halo, right[2] + halo, right[3] + halo)
    width = min(expanded_left[2], expanded_right[2]) - max(expanded_left[0], expanded_right[0])
    height = min(expanded_left[3], expanded_right[3]) - max(expanded_left[1], expanded_right[1])
    if width < 0.0 or height < 0.0:
        return None
    intersection = max(width, 0.0) * max(height, 0.0)
    left_area = max(expanded_left[2] - expanded_left[0], 0.0) * max(expanded_left[3] - expanded_left[1], 0.0)
    right_area = max(expanded_right[2] - expanded_right[0], 0.0) * max(expanded_right[3] - expanded_right[1], 0.0)
    denominator = min(left_area, right_area)
    return 1.0 if denominator <= 0.0 else intersection / denominator


def _family_report(
    family: str,
    inline: tuple[ViolationFact, ...],
    calibre: tuple[ViolationFact, ...],
    matches: list[CorrelatedViolation],
) -> RuleFamilyCorrelation:
    inline_count = sum(item.family == family for item in inline)
    calibre_count = sum(item.family == family for item in calibre)
    matched = sum(item.family == family for item in matches)
    return RuleFamilyCorrelation(
        family,
        inline_count,
        calibre_count,
        matched,
        inline_count - matched,
        calibre_count - matched,
        _ratio(matched, inline_count),
        _ratio(matched, calibre_count),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return round(float(numerator) / float(denominator), 6) if denominator else (1.0 if numerator == 0 else 0.0)


def _value(item: object, name: str, default: object) -> object:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _mapping(value: object) -> Mapping[object, object]:
    return value if isinstance(value, Mapping) else {}
