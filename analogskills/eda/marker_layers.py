"""Config-driven non-electrical marker-layer insertion.

These helpers handle signoff-context marker layers such as CRN28 DOD/DPO/SR_DOD
``must exist`` rules.  They intentionally do not attach shapes to nets and do
not affect placement/routing SMT variables.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from .oa import OaRect, OaWritePlan


def add_required_marker_layers(plan: OaWritePlan, pdk: object) -> OaWritePlan:
    """Return ``plan`` with configured required marker rectangles appended."""

    rects = required_marker_layer_rects(plan, pdk)
    if not rects:
        return plan
    return replace(plan, rects=tuple(getattr(plan, "rects", ()) or ()) + rects)


def required_marker_layer_rects(plan: OaWritePlan, pdk: object) -> tuple[OaRect, ...]:
    """Build marker rectangles from ``metadata.required_marker_layers``.

    By default these rules are treated as signoff context.  A block-level
    layout should not gain detached DOD/DPO/SR_DOD islands just to silence a
    full-chip deck check; such islands look like floating devices and pollute
    placement-quality metrics.  Detached emission is available only when the
    PDK metadata explicitly requests it with ``emission_policy``/``mode`` set
    to ``detached`` or ``layout``.

    Supported marker row fields:
    - ``layer`` / ``purpose``: OA LPP to emit.
    - ``width_um`` / ``height_um``: marker rectangle size.
    - ``offset_um``: lower-left offset from the selected plan anchor.
    - ``enclosures``: optional companion non-electrical rectangles around the
      marker, e.g. a PP enclosure for CRN28 DOD/SR_DOD marker layers.
    - ``rule_ids`` / ``name``: provenance metadata only.
    """

    cfg = _required_marker_config(pdk)
    if not _bool_like(cfg.get("enabled", False)):
        return ()
    if _emission_policy(cfg) not in {"detached", "layout", "layout_detached"}:
        return ()
    return _detached_required_marker_layer_rects(plan, cfg, pdk)


def required_marker_layer_specs(pdk: object) -> tuple[Mapping[str, object], ...]:
    """Return configured marker specs without emitting layout geometry.

    This is used by documentation/observation flows to keep the rule knowledge
    visible even when block-level layout generation correctly treats the marker
    family as signoff-only.
    """

    cfg = _required_marker_config(pdk)
    markers = tuple(item for item in tuple(cfg.get("markers", ()) or ()) if isinstance(item, Mapping))
    return tuple(_mapping(item) for item in markers)


def _detached_required_marker_layer_rects(
    plan: OaWritePlan,
    cfg: Mapping[str, object],
    pdk: object,
) -> tuple[OaRect, ...]:
    """Build legacy detached marker rectangles from a caller-approved policy."""

    markers = tuple(item for item in tuple(cfg.get("markers", ()) or ()) if isinstance(item, Mapping))
    if not markers:
        return ()
    bbox = _plan_bbox_um(plan)
    if bbox is None:
        return ()
    anchor = _anchor_point(bbox, str(cfg.get("anchor", "lower_left") or "lower_left"))
    rects: list[OaRect] = []
    for idx, raw_marker in enumerate(markers):
        marker = _mapping(raw_marker)
        layer = str(marker.get("layer", "") or "").strip()
        purpose = str(marker.get("purpose", "drawing") or "drawing").strip() or "drawing"
        if not layer:
            continue
        width = _positive_float(marker.get("width_um"), 0.4)
        height = _positive_float(marker.get("height_um"), width)
        dx, dy = _offset_um(marker.get("offset_um", marker.get("offset", (0.0, 0.0))))
        x0 = anchor[0] + dx
        y0 = anchor[1] + dy
        marker_name = str(marker.get("name", layer) or layer)
        marker_rule_ids = tuple(str(rule) for rule in tuple(marker.get("rule_ids", ()) or ()))
        marker_bbox = (x0, y0, x0 + width, y0 + height)
        rects.append(
            OaRect(
                layer,
                purpose,
                marker_bbox,
                "",
                metadata={
                    "origin": "required_marker_layers",
                    "marker_name": marker_name,
                    "marker_role": "primary",
                    "rule_ids": marker_rule_ids,
                    "index": idx,
                },
            )
        )
        for enc_idx, raw_enclosure in enumerate(tuple(marker.get("enclosures", ()) or ())):
            enclosure = _mapping(raw_enclosure)
            enc_layer = str(enclosure.get("layer", "") or "").strip()
            if not enc_layer:
                continue
            enc_purpose = str(enclosure.get("purpose", "drawing") or "drawing").strip() or "drawing"
            left, bottom, right, top = _margin_um(
                enclosure.get("margin_um", enclosure.get("enclosure_um", enclosure.get("margin", 0.0)))
            )
            enc_dx, enc_dy = _offset_um(enclosure.get("offset_um", enclosure.get("offset", (0.0, 0.0))))
            enc_name = str(enclosure.get("name", f"{marker_name}_{enc_layer}_enclosure") or f"{marker_name}_{enc_layer}_enclosure")
            enc_rule_ids = tuple(str(rule) for rule in tuple(enclosure.get("rule_ids", marker_rule_ids) or ()))
            rects.append(
                OaRect(
                    enc_layer,
                    enc_purpose,
                    (
                        marker_bbox[0] - left + enc_dx,
                        marker_bbox[1] - bottom + enc_dy,
                        marker_bbox[2] + right + enc_dx,
                        marker_bbox[3] + top + enc_dy,
                    ),
                    "",
                    metadata={
                        "origin": "required_marker_layers",
                        "marker_name": enc_name,
                        "marker_parent": marker_name,
                        "marker_role": "enclosure",
                        "rule_ids": enc_rule_ids,
                        "index": idx,
                        "enclosure_index": enc_idx,
                    },
                )
            )
    rules = getattr(pdk, "rules", None)
    if rules is not None:
        snapped: list[OaRect] = []
        for rect in rects:
            try:
                snapped.append(replace(rect, bbox=rules.snap_bbox_um(rect.bbox, mode="outward")))
            except Exception:
                snapped.append(rect)
        rects = snapped
    return tuple(rects)


def _required_marker_config(pdk: object) -> Mapping[str, object]:
    metadata = getattr(pdk, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return {}
    return _mapping(metadata.get("required_marker_layers", {}))


def _emission_policy(cfg: Mapping[str, object]) -> str:
    raw = cfg.get("emission_policy", cfg.get("mode", cfg.get("placement_policy", "signoff_only")))
    return str(raw or "signoff_only").strip().lower().replace("-", "_")


def _plan_bbox_um(plan: OaWritePlan) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for rect in tuple(getattr(plan, "rects", ()) or ()):
        try:
            x0, y0, x1, y1 = rect.bbox
        except Exception:
            continue
        xs.extend((float(x0), float(x1)))
        ys.extend((float(y0), float(y1)))
    for path in tuple(getattr(plan, "paths", ()) or ()):
        try:
            half = float(path.width) * 0.5
            for x, y in path.points:
                xs.extend((float(x) - half, float(x) + half))
                ys.extend((float(y) - half, float(y) + half))
        except Exception:
            continue
    for pin in tuple(getattr(plan, "pins", ()) or ()):
        bbox = getattr(pin, "bbox", None)
        if bbox is None:
            continue
        try:
            x0, y0, x1, y1 = bbox
        except Exception:
            continue
        xs.extend((float(x0), float(x1)))
        ys.extend((float(y0), float(y1)))
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _anchor_point(bbox: tuple[float, float, float, float], anchor: str) -> tuple[float, float]:
    x0, y0, x1, y1 = bbox
    normalized = str(anchor or "lower_left").strip().lower().replace("-", "_")
    if normalized in {"lower_right", "bottom_right"}:
        return (x1, y0)
    if normalized in {"upper_left", "top_left"}:
        return (x0, y1)
    if normalized in {"upper_right", "top_right"}:
        return (x1, y1)
    return (x0, y0)


def _offset_um(value: object) -> tuple[float, float]:
    if isinstance(value, Mapping):
        return (
            _float(value.get("x_um", value.get("dx_um", 0.0)), 0.0),
            _float(value.get("y_um", value.get("dy_um", 0.0)), 0.0),
        )
    try:
        raw = tuple(value or ())  # type: ignore[arg-type]
    except TypeError:
        raw = ()
    if len(raw) >= 2:
        return (_float(raw[0], 0.0), _float(raw[1], 0.0))
    return (0.0, 0.0)


def _margin_um(value: object) -> tuple[float, float, float, float]:
    if isinstance(value, Mapping):
        horizontal = _float(value.get("x_um", value.get("horizontal_um", value.get("all_um", 0.0))), 0.0)
        vertical = _float(value.get("y_um", value.get("vertical_um", value.get("all_um", 0.0))), 0.0)
        return (
            _float(value.get("left_um", horizontal), horizontal),
            _float(value.get("bottom_um", vertical), vertical),
            _float(value.get("right_um", horizontal), horizontal),
            _float(value.get("top_um", vertical), vertical),
        )
    try:
        raw = tuple(value or ())  # type: ignore[arg-type]
    except TypeError:
        raw = ()
    if len(raw) >= 4:
        return (_float(raw[0], 0.0), _float(raw[1], 0.0), _float(raw[2], 0.0), _float(raw[3], 0.0))
    if len(raw) >= 2:
        return (_float(raw[0], 0.0), _float(raw[1], 0.0), _float(raw[0], 0.0), _float(raw[1], 0.0))
    scalar = _float(value, 0.0)
    return (scalar, scalar, scalar, scalar)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _bool_like(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no"}
    return bool(value)


def _positive_float(value: object, default: float) -> float:
    return max(_float(value, default), 1e-9)


def _float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
