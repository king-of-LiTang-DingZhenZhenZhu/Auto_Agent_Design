"""Conservative OA candidates derived from localized Calibre routing markers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .calibre_closure import LocalRepairAction


@dataclass(frozen=True)
class OaJogFillEcoPlan:
    plan: object
    replaced_rect_indices: tuple[int, ...]
    added_rect_count: int
    skipped_groups: tuple[str, ...] = ()
    replacements: tuple["OaRectReplacement", ...] = ()


@dataclass(frozen=True)
class OaRectReplacement:
    sources: tuple[Mapping[str, object], ...]
    replacement: Mapping[str, object]


def build_oa_jog_fill_eco(
    plan: object,
    actions: Iterable[LocalRepairAction],
    *,
    min_spacing_um_by_layer: Mapping[str, float] | None = None,
) -> OaJogFillEcoPlan:
    """Fill local same-net rectangle clusters implicated in Calibre ``G.4`` checks.

    It intentionally produces a *candidate* only.  The bounding fill is allowed
    only when its expanded box has no other-net geometry on that layer, so a
    later Calibre run can safely decide whether it is an improvement.
    """

    from analogskills.eda.oa import OaRect, OaWritePlan

    rects = tuple(getattr(plan, "rects", ()))
    groups = _connected_target_rect_groups(actions, rects)
    spacing = {str(layer): float(value) for layer, value in dict(min_spacing_um_by_layer or {}).items()}
    replacements: list[tuple[tuple[int, ...], object]] = []
    skipped: list[str] = []
    for indices in groups:
        members = tuple(rects[index] for index in indices)
        layer = str(members[0].layer)
        net = str(members[0].net)
        if not net or any(str(row.layer) != layer or str(row.net) != net for row in members):
            skipped.append(f"mixed_or_unlabeled:{','.join(str(index) for index in indices)}")
            continue
        bbox = _union(tuple(tuple(float(v) for v in row.bbox) for row in members))
        if _has_other_net_conflict(plan, bbox, layer, net, set(indices), spacing.get(layer, 0.0)):
            skipped.append(f"spacing:{layer}:{net}:{','.join(str(index) for index in indices)}")
            continue
        base = members[0]
        replacements.append((indices, OaRect(layer, base.purpose, bbox, net, base.color)))
    replaced = {index for indices, _row in replacements for index in indices}
    new_rects = tuple(row for index, row in enumerate(rects) if index not in replaced) + tuple(row for _indices, row in replacements)
    journal_replacements = tuple(
        OaRectReplacement(tuple(_rect_signature(rects[index]) for index in indices), _rect_signature(row))
        for indices, row in replacements
    )
    return OaJogFillEcoPlan(
        OaWritePlan(
            getattr(plan, "cellview"), nets=tuple(getattr(plan, "nets", ())), pins=tuple(getattr(plan, "pins", ())),
            instances=tuple(getattr(plan, "instances", ())), rects=new_rects,
            labels=tuple(getattr(plan, "labels", ())), paths=tuple(getattr(plan, "paths", ())), vias=tuple(getattr(plan, "vias", ())),
        ),
        tuple(sorted(replaced)), len(replacements), tuple(skipped), journal_replacements,
    )


def oa_rect_replacement_journal(eco: OaJogFillEcoPlan) -> dict[str, object]:
    """Serialize accepted local geometry replacements for deterministic replay."""

    return {
        "version": 1,
        "kind": "oa_rect_replacements",
        "replacements": [
            {"sources": [dict(row) for row in item.sources], "replacement": dict(item.replacement)}
            for item in eco.replacements
        ],
    }


def infer_oa_rect_replacement_journal(
    before: object,
    after: object,
    *,
    tol_um: float = 1e-9,
) -> dict[str, object]:
    """Infer same-net rectangle replacements from a Calibre-accepted A/B pair."""

    before_rows = tuple(getattr(before, "rects", ()))
    after_rows = tuple(getattr(after, "rects", ()))
    removed = [row for row in before_rows if not _contains_rect(after_rows, row, tol_um=tol_um)]
    added = [row for row in after_rows if not _contains_rect(before_rows, row, tol_um=tol_um)]
    replacements: list[OaRectReplacement] = []
    consumed: set[int] = set()
    for added_index, replacement in enumerate(added):
        if not str(getattr(replacement, "net", "")):
            continue
        source_indices = tuple(
            index for index, source in enumerate(removed)
            if str(getattr(source, "layer", "")) == str(getattr(replacement, "layer", ""))
            and str(getattr(source, "net", "")) == str(getattr(replacement, "net", ""))
            and _contains_bbox(tuple(float(value) for value in replacement.bbox), tuple(float(value) for value in source.bbox), tol_um)
        )
        if len(source_indices) < 2 or any(index in consumed for index in source_indices):
            continue
        consumed.update(source_indices)
        replacements.append(OaRectReplacement(tuple(_rect_signature(removed[index]) for index in source_indices), _rect_signature(replacement)))
    return {
        "version": 1,
        "kind": "oa_rect_replacements",
        "replacements": [
            {"sources": [dict(row) for row in item.sources], "replacement": dict(item.replacement)}
            for item in replacements
        ],
    }


def apply_oa_rect_replacement_journal(
    plan: object,
    journal: Mapping[str, object],
    *,
    tol_um: float = 1e-9,
) -> tuple[object, int, tuple[str, ...]]:
    """Replay only exact accepted replacements; never broaden their scope."""

    from analogskills.eda.oa import OaRect, OaWritePlan

    if str(journal.get("kind", "")) != "oa_rect_replacements":
        raise ValueError("unsupported OA ECO journal kind")
    current = list(getattr(plan, "rects", ()))
    applied = 0
    skipped: list[str] = []
    for item_index, item in enumerate(tuple(journal.get("replacements", ()) or ())):
        if not isinstance(item, Mapping):
            skipped.append(f"invalid:{item_index}")
            continue
        source_rows = tuple(item.get("sources", ()) or ())
        replacement_data = item.get("replacement", {})
        if not isinstance(replacement_data, Mapping) or not source_rows:
            skipped.append(f"invalid:{item_index}")
            continue
        if any(_signature_matches(_rect_signature(row), replacement_data, tol_um) for row in current):
            continue
        matched_indices: list[int] = []
        for source in source_rows:
            match = next((index for index, row in enumerate(current) if index not in matched_indices and _signature_matches(_rect_signature(row), source, tol_um)), None)
            if match is None:
                matched_indices = []
                break
            matched_indices.append(match)
        if not matched_indices:
            skipped.append(f"missing_source:{item_index}")
            continue
        current = [row for index, row in enumerate(current) if index not in set(matched_indices)]
        current.append(_rect_from_signature(replacement_data, OaRect))
        applied += 1
    return OaWritePlan(
        getattr(plan, "cellview"), nets=tuple(getattr(plan, "nets", ())), pins=tuple(getattr(plan, "pins", ())),
        instances=tuple(getattr(plan, "instances", ())), rects=tuple(current), labels=tuple(getattr(plan, "labels", ())),
        paths=tuple(getattr(plan, "paths", ())), vias=tuple(getattr(plan, "vias", ())),
    ), applied, tuple(skipped)


def _connected_target_rect_groups(actions: Iterable[LocalRepairAction], rects: tuple[object, ...]) -> tuple[tuple[int, ...], ...]:
    sets: list[set[int]] = []
    for action in actions:
        if action.owner != "routing" or action.kind != "remove_short_jog":
            continue
        indices = {int(token[5:-1]) for token in action.target_shape_ids if token.startswith("rect[") and token.endswith("]") and token[5:-1].isdigit()}
        indices = {index for index in indices if index < len(rects)}
        if len(indices) >= 2:
            sets.append(indices)
    changed = True
    while changed:
        changed = False
        result: list[set[int]] = []
        for current in sets:
            overlap = [existing for existing in result if existing & current]
            if overlap:
                merged = set(current)
                for existing in overlap:
                    result.remove(existing)
                    merged.update(existing)
                result.append(merged)
                changed = True
            else:
                result.append(set(current))
        sets = result
    return tuple(sorted((tuple(sorted(row)) for row in sets), key=lambda row: row))


def _has_other_net_conflict(
    plan: object,
    bbox: tuple[float, float, float, float],
    layer: str,
    net: str,
    replaced_rect_indices: set[int],
    spacing: float,
) -> bool:
    expanded = (bbox[0] - spacing, bbox[1] - spacing, bbox[2] + spacing, bbox[3] + spacing)
    for index, rect in enumerate(tuple(getattr(plan, "rects", ()))):
        if index in replaced_rect_indices or str(rect.layer) != layer or str(rect.net) in {"", net}:
            continue
        if _overlaps(expanded, tuple(float(v) for v in rect.bbox)):
            return True
    for path in tuple(getattr(plan, "paths", ())):
        if str(path.layer) != layer or str(path.net) in {"", net}:
            continue
        half = float(path.width) / 2.0
        for left, right in zip(path.points, path.points[1:]):
            path_box = (min(left[0], right[0]) - half, min(left[1], right[1]) - half,
                        max(left[0], right[0]) + half, max(left[1], right[1]) + half)
            if _overlaps(expanded, path_box):
                return True
    return False


def _union(boxes: tuple[tuple[float, float, float, float], ...]) -> tuple[float, float, float, float]:
    return min(row[0] for row in boxes), min(row[1] for row in boxes), max(row[2] for row in boxes), max(row[3] for row in boxes)


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _rect_signature(rect: object) -> dict[str, object]:
    return {
        "layer": str(getattr(rect, "layer", "")), "purpose": str(getattr(rect, "purpose", "drawing")),
        "bbox": tuple(float(value) for value in getattr(rect, "bbox", ())), "net": str(getattr(rect, "net", "")),
        "color": str(getattr(rect, "color", "")),
    }


def _rect_from_signature(data: Mapping[str, object], rect_type: object) -> object:
    bbox = tuple(float(value) for value in tuple(data.get("bbox", ())))
    if len(bbox) != 4:
        raise ValueError("OA ECO replacement requires a four-coordinate bbox")
    return rect_type(str(data.get("layer", "")), str(data.get("purpose", "drawing")), bbox, str(data.get("net", "")), str(data.get("color", "")))


def _contains_rect(rows: Iterable[object], candidate: object, *, tol_um: float) -> bool:
    signature = _rect_signature(candidate)
    return any(_signature_matches(_rect_signature(row), signature, tol_um) for row in rows)


def _signature_matches(left: Mapping[str, object], right: Mapping[str, object], tol_um: float) -> bool:
    if any(str(left.get(key, "")) != str(right.get(key, "")) for key in ("layer", "purpose", "net", "color")):
        return False
    left_bbox = tuple(float(value) for value in tuple(left.get("bbox", ())))
    right_bbox = tuple(float(value) for value in tuple(right.get("bbox", ())))
    return len(left_bbox) == len(right_bbox) == 4 and all(abs(a - b) <= tol_um for a, b in zip(left_bbox, right_bbox))


def _contains_bbox(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float], tol_um: float) -> bool:
    return outer[0] <= inner[0] + tol_um and outer[1] <= inner[1] + tol_um and outer[2] + tol_um >= inner[2] and outer[3] + tol_um >= inner[3]
