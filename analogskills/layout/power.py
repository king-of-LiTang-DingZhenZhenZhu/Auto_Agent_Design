"""Lightweight power rail planning helpers."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, TYPE_CHECKING

from analogskills.pdk import PdkConfig

if TYPE_CHECKING:
    from analogskills.pcell.calibration import PCellCalibrationCache


@dataclass(frozen=True)
class PowerRailSpec:
    net: str
    side: str
    layer: str
    bbox: tuple[float, float, float, float]
    width_um: float


@dataclass(frozen=True)
class PowerDropSpec:
    instance: str
    terminal: str
    net: str
    layer: str
    points: tuple[tuple[float, float], ...]
    width_um: float


@dataclass(frozen=True)
class SupplyTapSpec:
    net: str
    kind: str
    xy_um: tuple[float, float]
    bbox: tuple[float, float, float, float]
    rail_layer: str


@dataclass(frozen=True)
class WellRegionSpec:
    kind: str
    layer: str
    bbox: tuple[float, float, float, float]
    device_count: int


@dataclass(frozen=True)
class GuardRingSpec:
    net: str
    kind: str
    inner_bbox: tuple[float, float, float, float]
    outer_bbox: tuple[float, float, float, float]
    active_layer: str
    metal_layer: str
    width_um: float
    spacing_um: float
    contact_count: int


@dataclass(frozen=True)
class PowerIntegritySuggestion:
    action: str
    net: str = ""
    reason: str = ""
    priority: int = 0
    params: dict[str, object] = field(default_factory=dict)


def top_level_marker_requires_global_cover(pdk: PdkConfig, marker: str) -> bool:
    """Whether a top-level FEOL marker must be drawn around native PCells.

    Native PCells normally own their well/implant geometry.  Some flows still
    need an abstract top-level cover for incomplete PCells or generated tap
    geometry, so the choice belongs to the PDK rather than to the layout
    algorithm.  Unknown or absent settings deliberately retain the legacy,
    conservative ``global_cover`` behavior.
    """

    key = str(marker or "").strip().lower().replace("-", "_")
    aliases = {"nw": "nwell", "np": "nplus", "pp": "pplus", "pm": "pmetal"}
    key = aliases.get(key, key)
    metadata = dict(getattr(pdk, "metadata", {}) or {})
    calibre = dict(metadata.get("calibre", {}) or {})
    policy = dict(calibre.get("top_level_marker_policy", {}) or {})
    mode = policy.get(key, policy.get("default", "global_cover"))
    if isinstance(mode, bool):
        return mode
    return str(mode or "global_cover").strip().lower() not in {
        "native_pcell",
        "native",
        "none",
        "disabled",
        "off",
    }


def plan_power_rails(
    plan: Any,
    pdk: PdkConfig | None = None,
    *,
    lib: str = "work",
    cell: str = "power_rails",
    view: str = "layout",
    top_net: str | None = "VDD",
    bottom_net: str | None = "VSS",
    layer: str | None = None,
    rail_width_um: float | None = None,
    margin_um: float = 0.25,
    rail_offset_um: float = 0.2,
    output: str = "oa",
):
    """Create a small OA plan containing top/bottom supply rails.

    This helper intentionally does not connect device sources or add taps.  It
    gives agents a deterministic rail proposal they can inspect, merge, or
    override before later source-drop/tap steps.
    """

    from analogskills.eda.oa import OaCellView, OaPin, OaRect, OaWritePlan, layout_plan_to_oa_write_plan, snap_oa_write_plan_to_grid
    from analogskills.layout.ir import LayoutCellRef, LayoutPin, LayoutPlan, LayoutRect, snap_layout_plan_to_grid

    pdk = pdk or PdkConfig.generic()
    if output not in {"oa", "layout_ir"}:
        raise ValueError("output must be 'oa' or 'layout_ir'")
    rail_layer = layer or _default_power_layer(pdk)
    width = rail_width_um if rail_width_um is not None else _default_rail_width_um(pdk, rail_layer)
    requested_nets = tuple(str(net) for net in (bottom_net, top_net) if net is not None and str(net))
    if not requested_nets:
        layout_plan = LayoutPlan(LayoutCellRef(lib, cell, view, "maskLayout"))
        return layout_plan if output == "layout_ir" else layout_plan_to_oa_write_plan(layout_plan)
    bbox = _instances_bbox_um(tuple(getattr(plan, "instances", ())))
    if bbox is None:
        layout_plan = LayoutPlan(LayoutCellRef(lib, cell, view, "maskLayout"), nets=requested_nets)
        return layout_plan if output == "layout_ir" else layout_plan_to_oa_write_plan(layout_plan)

    x0, y0, x1, y1 = bbox
    rail_x0 = x0 - margin_um
    rail_x1 = x1 + margin_um
    bottom_y0 = y0 - rail_offset_um - width
    bottom_y1 = y0 - rail_offset_um
    top_y0 = y1 + rail_offset_um
    top_y1 = y1 + rail_offset_um + width
    rails = []
    if bottom_net is not None and str(bottom_net):
        rails.append(PowerRailSpec(str(bottom_net), "bottom", rail_layer, (rail_x0, bottom_y0, rail_x1, bottom_y1), width))
    if top_net is not None and str(top_net):
        rails.append(PowerRailSpec(str(top_net), "top", rail_layer, (rail_x0, top_y0, rail_x1, top_y1), width))
    rails = tuple(rails)
    rects = tuple(OaRect(rail.layer, "drawing", rail.bbox, rail.net) for rail in rails)
    pins = tuple(OaPin(rail.net, rail.net, "inputOutput", rail.layer, _pin_bbox_for_rail(rail)) for rail in rails)
    if output == "layout_ir":
        layout_plan = LayoutPlan(
            LayoutCellRef(lib, cell, view, "maskLayout"),
            nets=tuple(rail.net for rail in rails),
            pins=tuple(LayoutPin(pin.name, pin.net, pin.direction, pin.layer, pin.bbox) for pin in pins),
            rects=tuple(LayoutRect(rect.layer, rect.bbox, rect.net, rect.purpose) for rect in rects),
        )
        return snap_layout_plan_to_grid(layout_plan, pdk)
    oa_plan = OaWritePlan(OaCellView(lib, cell, view, "maskLayout"), nets=tuple(rail.net for rail in rails), pins=pins, rects=rects)
    return snap_oa_write_plan_to_grid(oa_plan, pdk)


def analyze_power_plan(
    rail_plan: Any | None = None,
    drop_plan: Any | None = None,
    tap_plan: Any | None = None,
    pdk: PdkConfig | None = None,
    *,
    device_plan: Any | None = None,
    supply_nets: tuple[str, ...] = ("VDD", "VSS"),
    source_terminals: tuple[str, ...] = ("S",),
    body_terminals: tuple[str, ...] = ("B", "BODY", "BULK"),
    min_rail_width_um: float | None = None,
    require_drops: bool = True,
    require_taps: bool = True,
    require_body_taps: bool = True,
) -> dict[str, object]:
    """Analyze supply proposal artifacts without changing them."""

    pdk = pdk or PdkConfig.generic()
    rail_layer = _default_power_layer(pdk)
    rail_min = min_rail_width_um if min_rail_width_um is not None else _min_width_um(pdk, rail_layer, 0.1)
    rails = _rail_rects_by_net(rail_plan, supply_nets) if rail_plan is not None else {}
    paths_by_net = _paths_by_net(drop_plan, supply_nets) if drop_plan is not None else {net: () for net in supply_nets}
    tap_rects_by_net = _rects_by_net(tap_plan, supply_nets) if tap_plan is not None else {net: () for net in supply_nets}
    tap_vias_by_net = _vias_by_net(tap_plan, supply_nets) if tap_plan is not None else {net: () for net in supply_nets}
    tap_helper_instances_by_net = _tap_helper_instances_by_net(tap_plan, supply_nets) if tap_plan is not None else {net: () for net in supply_nets}
    drop_required_nets = _source_drop_required_nets(device_plan, supply_nets, source_terminals) if device_plan is not None else set(supply_nets)
    body_tap_required = _body_tap_required_kinds(device_plan, body_terminals) if device_plan is not None else {}
    body_tap_kinds = _body_tap_kinds_by_net(tap_plan, pdk, tuple(body_tap_required)) if tap_plan is not None else {net: () for net in body_tap_required}

    issues: list[str] = []
    min_widths: dict[str, float] = {}
    max_widths: dict[str, float] = {}
    for net in supply_nets:
        net_rails = rails.get(net, ())
        if not net_rails:
            issues.append(f"net {net} missing supply rail")
        else:
            widths = tuple(rail.width_um for rail in net_rails)
            min_widths[net] = min(widths)
            max_widths[net] = max(widths)
            if min_widths[net] < rail_min:
                issues.append(f"net {net} rail width {min_widths[net]:g}um below target {rail_min:g}um")
        if require_drops and net in drop_required_nets and not paths_by_net.get(net, ()):
            issues.append(f"net {net} missing source drop route")
        if require_taps:
            if not tap_rects_by_net.get(net, ()) and not tap_helper_instances_by_net.get(net, ()):
                issues.append(f"net {net} missing supply tap geometry")
            if not tap_vias_by_net.get(net, ()):
                issues.append(f"net {net} missing tap contact/via")
    if require_body_taps:
        for net, required_kinds in sorted(body_tap_required.items()):
            available = set(body_tap_kinds.get(net, ()))
            for kind in required_kinds:
                if kind not in available:
                    issues.append(f"net {net} missing {kind} body tap for MOS bulk terminal")

    return {
        "passed": not issues,
        "issues": tuple(issues),
        "rail_count_by_net": {net: len(rails.get(net, ())) for net in supply_nets},
        "drop_count_by_net": {net: len(paths_by_net.get(net, ())) for net in supply_nets},
        "tap_rect_count_by_net": {net: len(tap_rects_by_net.get(net, ())) for net in supply_nets},
        "tap_via_count_by_net": {net: len(tap_vias_by_net.get(net, ())) for net in supply_nets},
        "tap_helper_instance_count_by_net": {net: len(tap_helper_instances_by_net.get(net, ())) for net in supply_nets},
        "drop_required_nets": tuple(sorted(drop_required_nets)),
        "body_tap_required_by_net": {net: tuple(kinds) for net, kinds in sorted(body_tap_required.items())},
        "body_tap_kinds_by_net": {net: tuple(body_tap_kinds.get(net, ())) for net in sorted(body_tap_required)},
        "min_rail_width_um_by_net": min_widths,
        "max_rail_width_um_by_net": max_widths,
    }


def suggest_power_integrity_ecos(report: dict[str, object]) -> tuple[PowerIntegritySuggestion, ...]:
    """Map a power-plan analysis report to reviewable ECO suggestions."""

    suggestions = tuple(_power_suggestion_for_issue(str(issue)) for issue in tuple(report.get("issues", ())))
    return tuple(sorted(suggestions, key=lambda item: (-item.priority, item.action, item.net)))


def plan_well_regions(
    device_plan: Any,
    pdk: PdkConfig | None = None,
    *,
    lib: str = "work",
    cell: str = "well_regions",
    view: str = "layout",
    include_pmos_nwell: bool = True,
    output: str = "oa",
):
    """Plan well marker regions around device groups.

    Currently this emits a single n-well proposal covering all PMOS PCells.  It
    is intentionally separate from tap planning so agents can review well
    coverage independently.
    """

    from analogskills.eda.oa import OaCellView, OaRect, OaWritePlan, snap_oa_write_plan_to_grid
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, LayoutRect, snap_layout_plan_to_grid

    pdk = pdk or PdkConfig.generic()
    if output not in {"oa", "layout_ir"}:
        raise ValueError("output must be 'oa' or 'layout_ir'")
    rects = []
    marker_boxes = _device_marker_boxes(device_plan, pdk)
    if include_pmos_nwell and marker_boxes["pmos_active"]:
        nwell_layer = pdk.layer_map.wells.get("nwell", "NW")
        nplus_layer = pdk.layer_map.implants.get("nplus", "NP")
        pmetal_layer = pdk.layer_map.implants.get("pmetal", "PM")
        nwell_bbox = _expand_bbox(
            _bbox_union_all(marker_boxes["pmos_active"]),
            _enclosure_um(pdk, f"{nwell_layer}_{pdk.layer_map.active}", 0.18),
        )
        if top_level_marker_requires_global_cover(pdk, "nwell"):
            rects.append(OaRect(nwell_layer, "drawing", nwell_bbox, ""))
        if top_level_marker_requires_global_cover(pdk, "nplus"):
            rects.append(
                OaRect(
                    nplus_layer,
                    "drawing",
                    _expand_bbox(_bbox_union_all(marker_boxes["pmos_active"]), _enclosure_um(pdk, f"{nplus_layer}_{pdk.layer_map.active}", 0.065)),
                    "",
                )
            )
        pmetal_boxes = marker_boxes["pmos_gate_or_active"] or marker_boxes["pmos_active"]
        pmetal_margin = max(
            _enclosure_um(pdk, f"{pmetal_layer}_{pdk.layer_map.active}", 0.065),
            _enclosure_um(pdk, f"{pmetal_layer}_{pdk.layer_map.gate}", 0.065),
        )
        if top_level_marker_requires_global_cover(pdk, "pmetal"):
            rects.append(OaRect(pmetal_layer, "drawing", _expand_bbox(_bbox_union_all(pmetal_boxes), pmetal_margin), ""))
    if marker_boxes["nmos_active"] and top_level_marker_requires_global_cover(pdk, "pplus"):
        pplus_layer = pdk.layer_map.implants.get("pplus", "PP")
        rects.append(
            OaRect(
                pplus_layer,
                "drawing",
                _expand_bbox(_bbox_union_all(marker_boxes["nmos_active"]), _enclosure_um(pdk, f"{pplus_layer}_{pdk.layer_map.active}", 0.065)),
                "",
            )
        )
    oa_plan = OaWritePlan(OaCellView(lib, cell, view, "maskLayout"), rects=tuple(rects))
    if output == "layout_ir":
        layout_plan = LayoutPlan(
            LayoutCellRef(lib, cell, view, "maskLayout"),
            rects=tuple(LayoutRect(rect.layer, rect.bbox, rect.net, rect.purpose) for rect in rects),
        )
        return snap_layout_plan_to_grid(layout_plan, pdk)
    return snap_oa_write_plan_to_grid(oa_plan, pdk)


def plan_guard_ring(
    target_plan: Any,
    pdk: PdkConfig | None = None,
    *,
    lib: str = "work",
    cell: str = "guard_ring",
    view: str = "layout",
    net: str = "VSS",
    kind: str = "substrate",
    bbox: tuple[float, float, float, float] | None = None,
    layer: str | None = None,
    ring_width_um: float | None = None,
    spacing_um: float = 0.6,
    contact_pitch_um: float = 1.0,
    connect_to_core: bool = False,
    output: str = "oa",
):
    """Plan a simple four-sided guard-ring proposal around a bbox or plan."""

    from analogskills.eda.oa import OaCellView, OaRect, OaVia, OaWritePlan, layout_plan_to_oa_write_plan, snap_oa_write_plan_to_grid
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, LayoutRect, LayoutVia, snap_layout_plan_to_grid

    pdk = pdk or PdkConfig.generic()
    if output not in {"oa", "layout_ir"}:
        raise ValueError("output must be 'oa' or 'layout_ir'")
    if kind not in {"substrate", "nwell"}:
        raise ValueError("guard ring kind must be 'substrate' or 'nwell'")
    if spacing_um < 0:
        raise ValueError("guard ring spacing must be non-negative")
    if contact_pitch_um <= 0:
        raise ValueError("guard ring contact pitch must be positive")

    core_bbox = bbox if bbox is not None else _instances_bbox_um(tuple(getattr(target_plan, "instances", ())))
    if core_bbox is None:
        layout_plan = LayoutPlan(LayoutCellRef(lib, cell, view, "maskLayout"), nets=(net,))
        return layout_plan if output == "layout_ir" else layout_plan_to_oa_write_plan(layout_plan)

    active = pdk.layer_map.active
    metal = layer or pdk.layer_map.metals[0]
    width = ring_width_um if ring_width_um is not None else _default_guard_ring_width_um(pdk, active, metal)
    if width <= 0:
        raise ValueError("guard ring width must be positive")

    guard_config = _power_geometry_config(pdk, "guard_ring")
    left_extra_um = _configured_nm_um(guard_config, "left_extra_spacing_nm", 0.0)
    bottom_extra_um = _configured_nm_um(guard_config, "bottom_extra_spacing_nm", 0.0)
    right_extra_um = _configured_nm_um(guard_config, "right_extra_spacing_nm", 0.0)
    top_extra_um = _configured_nm_um(guard_config, "top_extra_spacing_nm", 0.0)
    inner = (
        core_bbox[0] - spacing_um - left_extra_um,
        core_bbox[1] - spacing_um - bottom_extra_um,
        core_bbox[2] + spacing_um + right_extra_um,
        core_bbox[3] + spacing_um + top_extra_um,
    )
    outer = _expand_bbox(inner, width)
    ring_bboxes = _ring_rectangles(inner, outer)
    rects = []
    for ring_bbox in ring_bboxes:
        rects.append(OaRect(active, "drawing", ring_bbox, net))
        rects.append(OaRect(metal, "drawing", ring_bbox, net))
    if connect_to_core:
        cx = 0.5 * (core_bbox[0] + core_bbox[2])
        bridge_half = 0.5 * width
        rects.append(OaRect(metal, "drawing", (cx - bridge_half, outer[1], cx + bridge_half, core_bbox[1]), net))

    implant = pdk.layer_map.implants.get("nplus" if kind == "nwell" else "pplus", "NP" if kind == "nwell" else "PP")
    implant_enc = _guard_ring_implant_enclosure_um(pdk, implant, active)
    rects.extend(OaRect(implant, "drawing", _expand_bbox(ring_bbox, implant_enc), "") for ring_bbox in ring_bboxes)
    if kind == "nwell":
        well = pdk.layer_map.wells.get("nwell", "NW")
        rects.append(OaRect(well, "drawing", _expand_bbox(outer, _enclosure_um(pdk, f"{well}_{active}", 0.18)), ""))

    contact_points = _ring_contact_points(inner, outer, contact_pitch_um)
    vias = [OaVia(pdk.layer_map.contact, point, net, metadata={"landing_layers": (active, pdk.layer_map.metals[0])}) for point in contact_points]
    if metal != pdk.layer_map.metals[0]:
        for point in contact_points:
            vias.extend(_via_stack_between_layers(pdk, pdk.layer_map.metals[0], metal, point, net, ""))

    oa_plan = OaWritePlan(
        OaCellView(lib, cell, view, "maskLayout"),
        nets=(net,),
        rects=tuple(rects),
        vias=tuple(vias),
    )
    if output == "layout_ir":
        layout_plan = LayoutPlan(
            LayoutCellRef(lib, cell, view, "maskLayout"),
            nets=(net,),
            rects=tuple(
                LayoutRect(
                    rect.layer,
                    rect.bbox,
                    rect.net,
                    rect.purpose,
                    (
                        {"kind": "guard_ring_active", "marker_layer": implant}
                        if rect.layer == active and rect.net
                        else {"kind": "guard_ring_implant", "preserve_marker_topology": True}
                        if rect.layer == implant and not rect.net
                        else {}
                    ),
                )
                for rect in rects
            ),
            vias=tuple(LayoutVia(via.via_def, via.xy, via.net, via.rows, via.cols, dict(getattr(via, "metadata", {}) or {})) for via in vias),
        )
        return snap_layout_plan_to_grid(layout_plan, pdk)
    return snap_oa_write_plan_to_grid(oa_plan, pdk)


def configured_guard_ring_geometry(
    pdk: PdkConfig | None = None,
    *,
    block: str = "",
) -> dict[str, object]:
    """Return the PDK-owned guard-ring envelope used by SMT and OA planning.

    The returned spacing is the empty core-to-ring clearance.  Side-specific
    extra spacing is kept separate because foundry rules can be asymmetric
    (CRN28 currently needs additional clearance on the top side).
    """

    pdk = pdk or PdkConfig.generic()
    config = _power_geometry_config(pdk, "guard_ring")
    enabled_blocks = tuple(str(item).lower() for item in tuple(config.get("smt_enabled_blocks", ()) or ()))
    enabled = bool(config.get("enabled", False)) and (
        not enabled_blocks or str(block).lower() in enabled_blocks
    )
    active = pdk.layer_map.active
    metal = pdk.layer_map.metals[0]
    configured_width = _configured_nm_um(config, "ring_width_nm", 0.0)
    width_um = configured_width if configured_width > 0 else _default_guard_ring_width_um(pdk, active, metal)
    return {
        "enabled": enabled,
        "net": str(config.get("net", "VSS") or "VSS"),
        "kind": str(config.get("kind", "substrate") or "substrate"),
        "width_um": float(width_um),
        "spacing_um": _configured_nm_um(config, "spacing_nm", 0.0),
        "contact_pitch_um": max(_configured_nm_um(config, "contact_pitch_nm", 1000.0), pdk.rules.grid_step_um),
        "extra_spacing_um_by_side": {
            side: _configured_nm_um(config, f"{side}_extra_spacing_nm", 0.0)
            for side in ("left", "bottom", "right", "top")
        },
        "source": "metadata.power_geometry.guard_ring",
    }


def physical_plan_bbox_um(plan: Any) -> tuple[float, float, float, float] | None:
    """Return the visible physical bbox of an OA/Layout plan.

    PCell placement origins are not reliable guard-ring anchors: native cells
    and generated access/tap geometry may extend to negative local coordinates.
    This helper includes planned rectangles, paths, pins, vias and any instance
    envelope available in the IR, so a ring cannot cut through access geometry.
    """

    boxes: list[tuple[float, float, float, float]] = []
    instance_bbox = _instances_bbox_um(tuple(getattr(plan, "instances", ()) or ()))
    if instance_bbox is not None:
        boxes.append(instance_bbox)
    for item in tuple(getattr(plan, "rects", ()) or ()) + tuple(getattr(plan, "pins", ()) or ()):
        bbox = tuple(float(value) for value in tuple(getattr(item, "bbox", ()))[:4])
        if len(bbox) == 4:
            boxes.append(bbox)  # type: ignore[arg-type]
    for item in tuple(getattr(plan, "paths", ()) or ()):
        points = tuple(tuple(float(value) for value in point[:2]) for point in tuple(getattr(item, "points", ()) or ()))
        if not points:
            continue
        half = 0.5 * max(0.0, float(getattr(item, "width", 0.0) or 0.0))
        boxes.append(
            (
                min(point[0] for point in points) - half,
                min(point[1] for point in points) - half,
                max(point[0] for point in points) + half,
                max(point[1] for point in points) + half,
            )
        )
    for item in tuple(getattr(plan, "vias", ()) or ()):
        xy = tuple(float(value) for value in tuple(getattr(item, "xy", getattr(item, "xy_um", ())) or ())[:2])
        if len(xy) == 2:
            boxes.append((xy[0], xy[1], xy[0], xy[1]))
    return _bbox_union_all(tuple(boxes)) if boxes else None


def plan_guard_ring_tap_implant_joins(
    guard_plan: Any,
    tap_plan: Any,
    pdk: PdkConfig | None = None,
    *,
    lib: str = "work",
    cell: str = "guard_ring_tap_join",
    view: str = "layout",
) -> Any:
    """Join a configured, short implant gap between a guard ring and body tap.

    A substrate-tap implant can land just inside the guard-ring implant.  It
    is not a routing connection, but it must not leave an isolated short PP
    gap: Calibre applies both PP.S.1 and the long-edge PP.S.9 rule there.  The
    PDK explicitly controls this utility geometry, including the maximum gap
    that may be closed and the overlap kept after grid snapping.
    """

    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, LayoutRect, snap_layout_plan_to_grid

    pdk = pdk or PdkConfig.generic()
    config = _power_geometry_config(pdk, "guard_ring_tap_join")
    if not bool(config.get("enabled", False)):
        return LayoutPlan(LayoutCellRef(lib, cell, view, "maskLayout"))
    marker_layers = tuple(str(layer) for layer in config.get("marker_layers", (pdk.layer_map.implants.get("pplus", "PP"),)))
    max_gap_um = _configured_nm_um(config, "maximum_gap_nm", 0.0)
    overlap_um = _configured_nm_um(config, "minimum_overlap_nm", 0.0)
    if max_gap_um <= 0.0 or overlap_um < 0.0:
        return LayoutPlan(LayoutCellRef(lib, cell, view, "maskLayout"))

    guard_markers = [
        rect
        for rect in tuple(getattr(guard_plan, "rects", ()))
        if str(getattr(rect, "layer", "")) in marker_layers
        and str(dict(getattr(rect, "metadata", {}) or {}).get("kind", "")) == "guard_ring_implant"
    ]
    tap_markers = [
        rect
        for rect in tuple(getattr(tap_plan, "rects", ()))
        if str(getattr(rect, "layer", "")) in marker_layers and not str(getattr(rect, "net", ""))
    ]
    bridges: list[LayoutRect] = []
    seen: set[tuple[str, tuple[float, float, float, float]]] = set()
    for guard in guard_markers:
        for tap in tap_markers:
            if str(getattr(guard, "layer", "")) != str(getattr(tap, "layer", "")):
                continue
            bridge_bbox = _marker_gap_bridge_bbox(
                _bbox_tuple(getattr(guard, "bbox")),
                _bbox_tuple(getattr(tap, "bbox")),
                max_gap_um=max_gap_um,
                overlap_um=overlap_um,
            )
            if bridge_bbox is None:
                continue
            key = (str(getattr(guard, "layer", "")), bridge_bbox)
            if key in seen:
                continue
            seen.add(key)
            bridges.append(
                LayoutRect(
                    key[0],
                    bridge_bbox,
                    "",
                    metadata={"kind": "guard_ring_tap_implant_join", "preserve_marker_topology": True},
                )
            )
    plan = LayoutPlan(LayoutCellRef(lib, cell, view, "maskLayout"), rects=tuple(bridges))
    return snap_layout_plan_to_grid(plan, pdk)


def plan_power_source_drops(
    device_plan: Any,
    rail_plan: Any,
    pdk: PdkConfig | None = None,
    *,
    lib: str = "work",
    cell: str = "power_drops",
    view: str = "layout",
    supply_nets: tuple[str, ...] = ("VDD", "VSS"),
    terminals: tuple[str, ...] = ("S",),
    drop_width_um: float | None = None,
    output: str = "oa",
    calibration_cache: PCellCalibrationCache | None = None,
    allow_nearest_calibration: bool = False,
    max_nearest_distance: float = 0.25,
):
    """Plan source-to-rail drops from PCell terminals to existing supply rails.

    The function emits only route/via proposals.  It does not create taps,
    guard rings, rail geometry, or mutate the incoming plans.
    """

    from analogskills.eda.oa import OaCellView, OaPath, OaRect, OaVia, OaWritePlan, layout_plan_to_oa_write_plan, snap_oa_write_plan_to_grid
    from analogskills.layout.ir import LayoutCellRef, LayoutPath, LayoutPlan, LayoutRect, LayoutVia, snap_layout_plan_to_grid
    from analogskills.layout.physical import via_landing_bboxes
    from analogskills.pcell import PCellTerminalAccessor, PCellTerminalRequiresTap

    pdk = pdk or PdkConfig.generic()
    if output not in {"oa", "layout_ir"}:
        raise ValueError("output must be 'oa' or 'layout_ir'")
    rail_by_net = _rail_rects_by_net(rail_plan, supply_nets)
    if not rail_by_net:
        layout_plan = LayoutPlan(LayoutCellRef(lib, cell, view, "maskLayout"), nets=supply_nets)
        return layout_plan if output == "layout_ir" else layout_plan_to_oa_write_plan(layout_plan)

    accessor = PCellTerminalAccessor(
        pdk,
        calibration_cache=calibration_cache,
        allow_nearest_calibration=allow_nearest_calibration,
        max_nearest_distance=max_nearest_distance,
    )
    avoid_bboxes_by_layer = _source_drop_avoid_bboxes_by_layer(
        device_plan,
        accessor,
        pdk,
        supply_nets=supply_nets,
    )
    paths = []
    vias = []
    rects = []
    seen_paths: set[tuple[str, str, tuple[tuple[float, float], ...], float]] = set()
    seen_vias: set[tuple[str, tuple[float, float], str, int, int]] = set()
    seen_rects: set[tuple[str, tuple[float, float, float, float], str]] = set()
    used_drop_x_by_key: dict[tuple[str, str, float], list[float]] = {}
    for inst in tuple(getattr(device_plan, "instances", ())):
        connections = getattr(inst, "connections", {})
        for terminal in terminals:
            net = str(connections.get(terminal, ""))
            if net not in rail_by_net:
                continue
            try:
                pin = accessor.select_terminal_breakout(
                    inst,
                    terminal,
                    require_lvs_safe=True,
                    preferred_layers=(pdk.layer_map.metals[0],),
                )
            except ValueError:
                try:
                    pin = accessor.select_terminal_breakout(
                        inst,
                        terminal,
                        require_lvs_safe=False,
                        preferred_layers=(pdk.layer_map.metals[0],),
                    )
                except (KeyError, PCellTerminalRequiresTap) as exc:
                    pin = _source_drop_body_marker_fallback_pin(accessor, inst, terminal, pdk, exc)
                    if pin is None:
                        continue
            except (KeyError, PCellTerminalRequiresTap) as exc:
                pin = _source_drop_body_marker_fallback_pin(accessor, inst, terminal, pdk, exc)
                if pin is None:
                    continue
            pin = _select_source_drop_breakout_candidate(
                accessor,
                inst,
                terminal,
                fallback_pin=pin,
                avoid_bboxes_by_layer=avoid_bboxes_by_layer,
            )
            rail = _nearest_rail_for_pin(pin.xy_um, rail_by_net[net])
            layer = rail.layer
            width = drop_width_um if drop_width_um is not None else _default_drop_width_um(pdk, layer)
            points = _drop_points_to_rail(
                pin.xy_um,
                rail.bbox,
                pdk.rules.grid_step_um,
                width_um=width,
                keepout_um=_min_width_um(pdk, layer, pdk.rules.grid_step_um),
                avoid_bboxes=tuple(avoid_bboxes_by_layer.get(layer, ())),
            )
            if len(points) < 2:
                continue
            points = _spread_source_drop_branch_points(
                points,
                rail=rail,
                net=net,
                layer=layer,
                width_um=width,
                pdk=pdk,
                used_drop_x_by_key=used_drop_x_by_key,
            )
            path_key = (layer, net, tuple(tuple(float(v) for v in point) for point in points), float(width))
            if path_key not in seen_paths:
                paths.append(OaPath(layer, "drawing", points, width, net))
                seen_paths.add(path_key)
            via_stack = _via_stack_between_layers(pdk, pin.layer, layer, pin.xy_um, net, pin.contact_layer)
            for via in via_stack:
                via_key = (
                    str(via.via_def),
                    (float(via.xy[0]), float(via.xy[1])),
                    str(via.net),
                    int(getattr(via, "rows", 1) or 1),
                    int(getattr(via, "cols", 1) or 1),
                )
                if via_key in seen_vias:
                    continue
                seen_vias.add(via_key)
                vias.append(via)
                for landing_layer, bbox in via_landing_bboxes(via, pdk):
                    snapped_bbox = pdk.rules.snap_bbox_um(bbox, mode="outward")
                    rect_key = (str(landing_layer), tuple(float(v) for v in snapped_bbox), str(net))
                    if rect_key in seen_rects:
                        continue
                    seen_rects.add(rect_key)
                    rects.append(OaRect(landing_layer, "drawing", snapped_bbox, net))
            if points and layer != rail.layer:
                rail_xy = tuple(float(value) for value in points[-1])
                rail_stack = _via_stack_between_layers(pdk, layer, rail.layer, rail_xy, net, "")
                for via in rail_stack:
                    via_key = (
                        str(via.via_def),
                        (float(via.xy[0]), float(via.xy[1])),
                        str(via.net),
                        int(getattr(via, "rows", 1) or 1),
                        int(getattr(via, "cols", 1) or 1),
                    )
                    if via_key in seen_vias:
                        continue
                    seen_vias.add(via_key)
                    vias.append(via)
                    for landing_layer, bbox in via_landing_bboxes(via, pdk):
                        snapped_bbox = pdk.rules.snap_bbox_um(bbox, mode="outward")
                        rect_key = (str(landing_layer), tuple(float(v) for v in snapped_bbox), str(net))
                        if rect_key in seen_rects:
                            continue
                        seen_rects.add(rect_key)
                        rects.append(OaRect(landing_layer, "drawing", snapped_bbox, net))

    oa_plan = OaWritePlan(
        OaCellView(lib, cell, view, "maskLayout"),
        nets=tuple(dict.fromkeys(supply_nets)),
        rects=tuple(rects),
        paths=tuple(paths),
        vias=tuple(vias),
    )
    if output == "layout_ir":
        layout_plan = LayoutPlan(
            LayoutCellRef(lib, cell, view, "maskLayout"),
            nets=tuple(dict.fromkeys(supply_nets)),
            rects=tuple(LayoutRect(rect.layer, rect.bbox, rect.net, rect.purpose, {"kind": "via_landing"}) for rect in rects),
            paths=tuple(LayoutPath(path.layer, path.points, path.width, path.net, path.purpose) for path in paths),
            vias=tuple(LayoutVia(via.via_def, via.xy, via.net, via.rows, via.cols, dict(getattr(via, "metadata", {}) or {})) for via in vias),
        )
        return snap_layout_plan_to_grid(layout_plan, pdk)
    snapped = snap_oa_write_plan_to_grid(oa_plan, pdk)
    return snapped


def plan_supply_taps(
    rail_plan: Any,
    pdk: PdkConfig | None = None,
    *,
    lib: str = "work",
    cell: str = "supply_taps",
    view: str = "layout",
    top_net: str | None = "VDD",
    bottom_net: str | None = "VSS",
    tap_width_um: float = 0.24,
    tap_height_um: float = 0.24,
    output: str = "oa",
):
    """Plan one nwell tap and one substrate tap near existing supply rails."""

    from analogskills.eda.oa import OaCellView, OaRect, OaVia, OaWritePlan, snap_oa_write_plan_to_grid
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, LayoutRect, LayoutVia, snap_layout_plan_to_grid

    pdk = pdk or PdkConfig.generic()
    if output not in {"oa", "layout_ir"}:
        raise ValueError("output must be 'oa' or 'layout_ir'")
    requested_nets = tuple(str(net) for net in (top_net, bottom_net) if net is not None and str(net))
    rails = _rail_rects_by_net(rail_plan, requested_nets)
    rects = []
    vias = []
    specs: list[SupplyTapSpec] = []
    for net, kind in tuple(
        (str(net), kind)
        for net, kind in ((top_net, "nwell"), (bottom_net, "substrate"))
        if net is not None and str(net)
    ):
        if net not in rails:
            continue
        rail = _rail_for_tap(rails[net], prefer_top=(kind == "nwell"))
        cx, cy = _tap_center_for_rail(rail)
        active_bbox = _centered_bbox((cx, cy), tap_width_um, min(tap_height_um, max(rail.width_um, tap_height_um)))
        specs.append(SupplyTapSpec(net, kind, (cx, cy), active_bbox, rail.layer))
        rects.extend(_tap_rects_for_bbox(pdk, active_bbox, net, kind))
        vias.append(OaVia(pdk.layer_map.contact, (cx, cy), net, metadata={"landing_layers": (pdk.layer_map.active, pdk.layer_map.metals[0])}))
        if rail.layer != pdk.layer_map.metals[0]:
            vias.extend(_via_stack_between_layers(pdk, pdk.layer_map.metals[0], rail.layer, (cx, cy), net, ""))

    oa_plan = OaWritePlan(
        OaCellView(lib, cell, view, "maskLayout"),
        nets=requested_nets,
        rects=tuple(rects),
        vias=tuple(vias),
    )
    if output == "layout_ir":
        layout_plan = LayoutPlan(
            LayoutCellRef(lib, cell, view, "maskLayout"),
            nets=requested_nets,
            rects=tuple(
                LayoutRect(
                    rect.layer,
                    rect.bbox,
                    rect.net,
                    rect.purpose,
                    {"kind": "supply_tap_active"}
                    if rect.layer == pdk.layer_map.active and rect.net
                    else {},
                )
                for rect in rects
            ),
            vias=tuple(LayoutVia(via.via_def, via.xy, via.net, via.rows, via.cols, dict(getattr(via, "metadata", {}) or {})) for via in vias),
        )
        return snap_layout_plan_to_grid(layout_plan, pdk)
    return snap_oa_write_plan_to_grid(oa_plan, pdk)


def build_supply_tap_plan_from_specs(
    specs: tuple[SupplyTapSpec, ...],
    pdk: PdkConfig | None = None,
    *,
    lib: str = "work",
    cell: str = "supply_taps",
    view: str = "layout",
    output: str = "oa",
):
    """Materialize tap geometry from explicit supply-tap specs.

    This is intentionally lower-level than ``plan_supply_taps()`` so higher
    layers, such as stdcell row compilers, can choose tap sites themselves
    while still reusing one geometry builder.
    """

    from analogskills.eda.oa import OaCellView, OaRect, OaVia, OaWritePlan, snap_oa_write_plan_to_grid
    from analogskills.layout.ir import LayoutCellRef, LayoutPlan, LayoutRect, LayoutVia, snap_layout_plan_to_grid

    pdk = pdk or PdkConfig.generic()
    if output not in {"oa", "layout_ir"}:
        raise ValueError("output must be 'oa' or 'layout_ir'")
    rects = []
    vias = []
    nets = tuple(dict.fromkeys(spec.net for spec in specs))
    for spec in specs:
        rects.extend(_tap_rects_for_bbox(pdk, spec.bbox, spec.net, spec.kind))
        vias.append(OaVia(pdk.layer_map.contact, spec.xy_um, spec.net, metadata={"landing_layers": (pdk.layer_map.active, pdk.layer_map.metals[0])}))
        if spec.rail_layer != pdk.layer_map.metals[0]:
            vias.extend(_via_stack_between_layers(pdk, pdk.layer_map.metals[0], spec.rail_layer, spec.xy_um, spec.net, ""))
    oa_plan = OaWritePlan(
        OaCellView(lib, cell, view, "maskLayout"),
        nets=nets,
        rects=tuple(rects),
        vias=tuple(vias),
    )
    if output == "layout_ir":
        layout_plan = LayoutPlan(
            LayoutCellRef(lib, cell, view, "maskLayout"),
            nets=nets,
            rects=tuple(
                LayoutRect(
                    rect.layer,
                    rect.bbox,
                    rect.net,
                    rect.purpose,
                    {"kind": "supply_tap_active"}
                    if rect.layer == pdk.layer_map.active and rect.net
                    else {},
                )
                for rect in rects
            ),
            vias=tuple(LayoutVia(via.via_def, via.xy, via.net, via.rows, via.cols, dict(getattr(via, "metadata", {}) or {})) for via in vias),
        )
        return snap_layout_plan_to_grid(layout_plan, pdk)
    return snap_oa_write_plan_to_grid(oa_plan, pdk)


def _default_power_layer(pdk: PdkConfig) -> str:
    if pdk.preferred_power_layers:
        return pdk.preferred_power_layers[0]
    return pdk.layer_map.metals[0]


def _default_rail_width_um(pdk: PdkConfig, layer: str) -> float:
    try:
        min_width = pdk.rules.min_width_um(layer)
    except KeyError:
        min_width = 0.1
    return pdk.rules.snap_dimension_um(max(2.0 * min_width, 0.2))


def _default_drop_width_um(pdk: PdkConfig, layer: str) -> float:
    try:
        min_width = pdk.rules.min_width_um(layer)
    except KeyError:
        min_width = 0.1
    return pdk.rules.snap_dimension_um(max(min_width, pdk.rules.grid_step_um))


def _default_guard_ring_width_um(pdk: PdkConfig, active: str, metal: str) -> float:
    return pdk.rules.snap_dimension_um(max(_min_width_um(pdk, active, 0.18), _min_width_um(pdk, metal, 0.12), 0.24))


def _min_width_um(pdk: PdkConfig, layer: str, fallback_um: float) -> float:
    try:
        return pdk.rules.min_width_um(layer)
    except KeyError:
        return fallback_um


def _point_bbox_distance_um(point: tuple[float, float], bbox: tuple[float, float, float, float]) -> float:
    px, py = float(point[0]), float(point[1])
    x0, y0, x1, y1 = (float(value) for value in bbox)
    dx = max(x0 - px, px - x1, 0.0)
    dy = max(y0 - py, py - y1, 0.0)
    if dx <= 0.0 and dy <= 0.0:
        return -min(x1 - px, px - x0, y1 - py, py - y0)
    return (dx * dx + dy * dy) ** 0.5


def _is_template_or_fallback_pin_source(source: str) -> bool:
    source_text = str(source or "")
    return source_text == "pdk_template" or "fallback" in source_text


def _select_source_drop_breakout_candidate(
    accessor: Any,
    instance: Any,
    terminal: str,
    *,
    fallback_pin: Any,
    avoid_bboxes_by_layer: Mapping[str, Sequence[tuple[float, float, float, float]]],
) -> Any:
    if fallback_pin is None or not _is_template_or_fallback_pin_source(str(getattr(fallback_pin, "source", ""))):
        return fallback_pin
    synthetic = ()
    if hasattr(accessor, "synthetic_terminal_pins"):
        try:
            synthetic = tuple(accessor.synthetic_terminal_pins(instance, terminal, preferred_layers=(getattr(fallback_pin, "layer", ""),)))
        except Exception:
            synthetic = ()
    if not synthetic:
        return fallback_pin
    candidates = (fallback_pin, *synthetic)
    best = fallback_pin
    best_score = float("-inf")
    for pin in candidates:
        layer = str(getattr(pin, "layer", "") or "")
        point = tuple(float(value) for value in tuple(getattr(pin, "xy_um", (0.0, 0.0)))[:2])
        clearance = min(
            (_point_bbox_distance_um(point, bbox) for bbox in tuple(avoid_bboxes_by_layer.get(layer, ()) or ())),
            default=1.0,
        )
        score = clearance
        if str(terminal) == "S":
            score -= point[0]
        elif str(terminal) == "D":
            score += point[0]
        if score > best_score:
            best = pin
            best_score = score
    return best


def _spread_source_drop_branch_points(
    points: tuple[tuple[float, float], ...],
    *,
    rail: PowerRailSpec,
    net: str,
    layer: str,
    width_um: float,
    pdk: PdkConfig,
    used_drop_x_by_key: dict[tuple[str, str, float], list[float]],
) -> tuple[tuple[float, float], ...]:
    if len(points) != 2:
        return points
    (x0, y0), (x1, y1) = points
    if abs(x0 - x1) > pdk.rules.grid_step_um or abs(y0 - y1) <= pdk.rules.grid_step_um:
        return points
    try:
        spacing_um = float(pdk.rules.min_spacing_um(layer))
    except (AttributeError, KeyError, TypeError, ValueError):
        spacing_um = pdk.rules.grid_step_um
    pitch_um = max(float(width_um) + spacing_um, 2.0 * pdk.rules.grid_step_um)
    key = (str(net), str(layer), round(float(y1), 6))
    used_x = used_drop_x_by_key.setdefault(key, [])
    if not used_x:
        used_x.append(float(x0))
        return points
    rail_center_x = 0.5 * (float(rail.bbox[0]) + float(rail.bbox[2]))
    step_sign = 1.0 if float(x0) >= rail_center_x else -1.0
    candidate_x = float(x0)
    iterations = 0
    while any(abs(candidate_x - prev_x) < pitch_um - 1e-12 for prev_x in used_x):
        candidate_x += step_sign * pitch_um
        candidate_x = pdk.rules.snap_point_um((candidate_x, 0.0))[0]
        iterations += 1
        if iterations > 16:
            break
    used_x.append(candidate_x)
    if abs(candidate_x - float(x0)) <= pdk.rules.grid_step_um:
        return points
    return (
        pdk.rules.snap_point_um((x0, y0)),
        pdk.rules.snap_point_um((candidate_x, y0)),
        pdk.rules.snap_point_um((candidate_x, y1)),
    )


def _rail_rects_by_net(rail_plan: Any, supply_nets: tuple[str, ...]) -> dict[str, tuple[PowerRailSpec, ...]]:
    result: dict[str, list[PowerRailSpec]] = {net: [] for net in supply_nets}
    for rect in tuple(getattr(rail_plan, "rects", ())):
        metadata = getattr(rect, "metadata", {})
        if isinstance(metadata, dict) and metadata.get("kind") == "via_landing":
            continue
        net = str(getattr(rect, "net", ""))
        if net not in result:
            continue
        bbox = tuple(float(v) for v in getattr(rect, "bbox"))
        layer = str(getattr(rect, "layer"))
        width = min(abs(bbox[2] - bbox[0]), abs(bbox[3] - bbox[1]))
        side = "bottom" if (bbox[1] + bbox[3]) / 2.0 < 0 else "top"
        result[net].append(PowerRailSpec(net, side, layer, bbox, width))
    return {net: tuple(rails) for net, rails in result.items() if rails}


def _paths_by_net(plan: Any, nets: tuple[str, ...]) -> dict[str, tuple[object, ...]]:
    result: dict[str, list[object]] = {net: [] for net in nets}
    for path_obj in tuple(getattr(plan, "paths", ())):
        net = str(getattr(path_obj, "net", ""))
        if net in result:
            result[net].append(path_obj)
    return {net: tuple(paths) for net, paths in result.items()}


def _rects_by_net(plan: Any, nets: tuple[str, ...]) -> dict[str, tuple[object, ...]]:
    result: dict[str, list[object]] = {net: [] for net in nets}
    for rect in tuple(getattr(plan, "rects", ())):
        net = str(getattr(rect, "net", ""))
        if net in result:
            result[net].append(rect)
    return {net: tuple(rects) for net, rects in result.items()}


def _vias_by_net(plan: Any, nets: tuple[str, ...]) -> dict[str, tuple[object, ...]]:
    result: dict[str, list[object]] = {net: [] for net in nets}
    for via in tuple(getattr(plan, "vias", ())):
        net = str(getattr(via, "net", ""))
        if net in result:
            result[net].append(via)
    return {net: tuple(vias) for net, vias in result.items()}


def _tap_helper_instances_by_net(plan: Any, nets: tuple[str, ...]) -> dict[str, tuple[object, ...]]:
    result: dict[str, list[object]] = {net: [] for net in nets}
    for inst in tuple(getattr(plan, "instances", ())):
        net = _instance_supply_net(inst)
        if net in result and _instance_tap_kind(inst):
            result[net].append(inst)
    return {net: tuple(instances) for net, instances in result.items()}


def _source_drop_required_nets(device_plan: Any, supply_nets: tuple[str, ...], terminals: tuple[str, ...]) -> set[str]:
    required: set[str] = set()
    supply_set = set(supply_nets)
    for inst in tuple(getattr(device_plan, "instances", ())):
        connections = getattr(inst, "connections", {})
        for terminal in terminals:
            net = str(connections.get(terminal, ""))
            if net in supply_set:
                required.add(net)
    return required


def _body_tap_required_kinds(device_plan: Any, terminals: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    required: dict[str, set[str]] = {}
    terminal_names = tuple(str(terminal) for terminal in terminals)
    for inst in tuple(getattr(device_plan, "instances", ())):
        logical_name = str(getattr(inst, "logical_name", "")).lower()
        kind = _body_tap_kind_for_device(logical_name)
        if kind == "":
            continue
        connections = getattr(inst, "connections", {})
        for terminal in terminal_names:
            net = str(connections.get(terminal, ""))
            if net:
                required.setdefault(net, set()).add(kind)
    return {net: tuple(sorted(kinds)) for net, kinds in sorted(required.items())}


def _body_tap_kind_for_device(logical_name: str) -> str:
    if logical_name == "nmos" or logical_name.startswith("nmos"):
        return "substrate"
    if logical_name == "pmos" or logical_name.startswith("pmos"):
        return "nwell"
    return ""


def _body_tap_kinds_by_net(tap_plan: Any, pdk: PdkConfig, nets: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    required_nets = set(str(net) for net in nets)
    result: dict[str, set[str]] = {net: set() for net in required_nets}
    if not required_nets:
        return {}
    active_layer = pdk.layer_map.active
    nwell_layer = pdk.layer_map.wells.get("nwell", "NW")
    pplus_layer = pdk.layer_map.implants.get("pplus", "PP")
    markers = tuple(
        (str(getattr(rect, "layer", "")), _bbox_tuple(getattr(rect, "bbox")))
        for rect in tuple(getattr(tap_plan, "rects", ()))
        if not str(getattr(rect, "net", ""))
    )
    for rect in tuple(getattr(tap_plan, "rects", ())):
        net = str(getattr(rect, "net", ""))
        if net not in required_nets:
            continue
        layer = str(getattr(rect, "layer", ""))
        if layer != active_layer:
            continue
        bbox = _bbox_tuple(getattr(rect, "bbox"))
        explicit_kind = _rect_tap_kind(rect)
        if explicit_kind:
            result.setdefault(net, set()).add(explicit_kind)
        if any(marker_layer == nwell_layer and _bbox_overlaps(bbox, marker_bbox) for marker_layer, marker_bbox in markers):
            result.setdefault(net, set()).add("nwell")
        if any(marker_layer == pplus_layer and _bbox_overlaps(bbox, marker_bbox) for marker_layer, marker_bbox in markers):
            result.setdefault(net, set()).add("substrate")
    for inst in tuple(getattr(tap_plan, "instances", ())):
        net = _instance_supply_net(inst)
        if net not in required_nets:
            continue
        helper_kind = _instance_tap_kind(inst)
        if helper_kind:
            result.setdefault(net, set()).add(helper_kind)
    return {net: tuple(sorted(kinds)) for net, kinds in sorted(result.items())}


def _rect_tap_kind(rect: Any) -> str:
    metadata = getattr(rect, "metadata", {})
    if isinstance(metadata, dict):
        for key in ("tap_kind", "kind"):
            value = str(metadata.get(key, ""))
            if value in {"nwell", "substrate"}:
                return value
    return ""


def _instance_supply_net(inst: Any) -> str:
    connections = getattr(inst, "connections", None)
    if isinstance(connections, dict):
        for key in ("B", "BODY", "BULK", "S", "D", "net", "NET"):
            net = str(connections.get(key, ""))
            if net:
                return net
    params = getattr(inst, "params", None)
    if isinstance(params, dict):
        for key in ("net", "NET"):
            net = str(params.get(key, ""))
            if net:
                return net
    return ""


def _instance_tap_kind(inst: Any) -> str:
    metadata = getattr(inst, "metadata", None)
    if isinstance(metadata, dict):
        for key in ("tap_kind", "helper_kind", "intent_kind"):
            value = str(metadata.get(key, ""))
            if value in {"nwell", "substrate"}:
                return value
    cell = str(getattr(inst, "cell", "") or "").upper()
    if cell == "M0_NW":
        return "nwell"
    if cell == "M0_SUB":
        return "substrate"
    return ""


def _bbox_tuple(value: Any) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = value
    return (float(x0), float(y0), float(x1), float(y1))


def _bbox_overlaps(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    lx0, ly0, lx1, ly1 = left
    rx0, ry0, rx1, ry1 = right
    return min(lx1, rx1) >= max(lx0, rx0) and min(ly1, ry1) >= max(ly0, ry0)


def _power_suggestion_for_issue(issue: str) -> PowerIntegritySuggestion:
    words = issue.split()
    net = words[1] if len(words) > 1 and words[0] == "net" else ""
    if "missing supply rail" in issue:
        return PowerIntegritySuggestion("plan_power_rails", net, issue, 90)
    if "rail width" in issue and "below target" in issue:
        return PowerIntegritySuggestion("widen_power_rail", net, issue, 80)
    if "missing source drop route" in issue:
        return PowerIntegritySuggestion("plan_power_source_drops", net, issue, 70)
    if "missing supply tap geometry" in issue or "missing tap contact/via" in issue:
        return PowerIntegritySuggestion("plan_supply_taps", net, issue, 65)
    if "missing " in issue and " body tap for MOS bulk terminal" in issue:
        return PowerIntegritySuggestion("plan_body_tap", net, issue, 75)
    return PowerIntegritySuggestion("manual_power_integrity_review", net, issue, 10)


def _nearest_rail_for_pin(xy: tuple[float, float], rails: tuple[PowerRailSpec, ...]) -> PowerRailSpec:
    _x, y = xy
    return min(rails, key=lambda rail: _distance_to_bbox_y(y, rail.bbox))


def _rail_for_tap(rails: tuple[PowerRailSpec, ...], *, prefer_top: bool) -> PowerRailSpec:
    return max(rails, key=lambda rail: (rail.bbox[1] + rail.bbox[3]) / 2.0) if prefer_top else min(rails, key=lambda rail: (rail.bbox[1] + rail.bbox[3]) / 2.0)


def _tap_center_for_rail(rail: PowerRailSpec) -> tuple[float, float]:
    x0, y0, x1, y1 = rail.bbox
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _centered_bbox(center: tuple[float, float], width: float, height: float) -> tuple[float, float, float, float]:
    cx, cy = center
    return (cx - width / 2.0, cy - height / 2.0, cx + width / 2.0, cy + height / 2.0)


def _ring_rectangles(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float, float], ...]:
    ix0, iy0, ix1, iy1 = inner
    ox0, oy0, ox1, oy1 = outer
    return (
        (ox0, oy0, ox1, iy0),
        (ox0, iy1, ox1, oy1),
        (ox0, iy0, ix0, iy1),
        (ix1, iy0, ox1, iy1),
    )


def _ring_contact_points(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float],
    pitch_um: float,
) -> tuple[tuple[float, float], ...]:
    ix0, iy0, ix1, iy1 = inner
    ox0, oy0, ox1, oy1 = outer
    width = min(ix0 - ox0, iy0 - oy0, ox1 - ix1, oy1 - iy1)
    inset = max(width / 2.0, 0.0)
    bottom_y = (oy0 + iy0) / 2.0
    top_y = (iy1 + oy1) / 2.0
    left_x = (ox0 + ix0) / 2.0
    right_x = (ix1 + ox1) / 2.0
    xs = _spaced_positions(ox0 + inset, ox1 - inset, pitch_um)
    ys = _spaced_positions(iy0 + inset, iy1 - inset, pitch_um)
    points = [(x, bottom_y) for x in xs]
    points.extend((x, top_y) for x in xs)
    points.extend((left_x, y) for y in ys)
    points.extend((right_x, y) for y in ys)
    return tuple(dict.fromkeys(points))


def _spaced_positions(start: float, stop: float, pitch_um: float) -> tuple[float, ...]:
    if stop <= start:
        return ((start + stop) / 2.0,)
    span = stop - start
    count = max(1, int(span // pitch_um) + 1)
    if count == 1:
        return ((start + stop) / 2.0,)
    step = span / float(count - 1)
    return tuple(start + step * idx for idx in range(count))


def _tap_rects_for_bbox(pdk: PdkConfig, active_bbox: tuple[float, float, float, float], net: str, kind: str) -> tuple[object, ...]:
    from analogskills.eda.oa import OaRect

    active = pdk.layer_map.active
    metal = pdk.layer_map.metals[0]
    metal_enclosure = max(
        _enclosure_um(pdk, f"{pdk.layer_map.contact}_{metal}", 0.03),
        _min_width_um(pdk, metal, pdk.rules.grid_step_um) + max(0.03, pdk.rules.grid_step_um),
    )
    rects = [
        OaRect(active, "drawing", active_bbox, net),
        OaRect(metal, "drawing", _expand_bbox(active_bbox, metal_enclosure), net),
    ]
    if kind == "nwell":
        implant = pdk.layer_map.implants.get("nplus", "NP")
        implant_key = f"{implant}_{active}"
        well = pdk.layer_map.wells.get("nwell", "NW")
        rects.append(OaRect(implant, "drawing", _expand_bbox(active_bbox, _enclosure_um(pdk, implant_key, 0.065)), ""))
        rects.append(OaRect(well, "drawing", _expand_bbox(active_bbox, _enclosure_um(pdk, f"{well}_{active}", 0.18)), ""))
    else:
        implant = pdk.layer_map.implants.get("pplus", "PP")
        implant_key = f"{implant}_{active}"
        rects.append(OaRect(implant, "drawing", _expand_bbox(active_bbox, _enclosure_um(pdk, implant_key, 0.065)), ""))
    return tuple(rects)


def _enclosure_um(pdk: PdkConfig, key: str, fallback_um: float) -> float:
    try:
        return pdk.rules.enclosure(key) * 1e-3
    except KeyError:
        return fallback_um


def _power_geometry_config(pdk: PdkConfig, name: str) -> Mapping[str, object]:
    metadata = dict(getattr(pdk, "metadata", {}) or {})
    geometry = metadata.get("power_geometry", {})
    if not isinstance(geometry, Mapping):
        return {}
    config = geometry.get(name, {})
    return config if isinstance(config, Mapping) else {}


def _configured_nm_um(config: Mapping[str, object], key: str, fallback_um: float) -> float:
    value = config.get(key)
    if value is None:
        return fallback_um
    try:
        value_nm = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid power-geometry value for {key}") from exc
    if value_nm < 0:
        raise ValueError(f"power-geometry value for {key} must be non-negative")
    return value_nm * 1e-3


def _guard_ring_implant_enclosure_um(pdk: PdkConfig, implant: str, active: str) -> float:
    minimum_um = _enclosure_um(pdk, f"{implant}_{active}", 0.065)
    config = _power_geometry_config(pdk, "guard_ring")
    configured = config.get("implant_enclosure_nm_by_layer", {})
    if not isinstance(configured, Mapping):
        return minimum_um
    value = configured.get(implant)
    if value is None:
        return minimum_um
    try:
        requested_um = float(value) * 1e-3
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid guard-ring implant enclosure for {implant}") from exc
    if requested_um < 0:
        raise ValueError(f"guard-ring implant enclosure for {implant} must be non-negative")
    return max(minimum_um, requested_um)


def _marker_gap_bridge_bbox(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    *,
    max_gap_um: float,
    overlap_um: float,
) -> tuple[float, float, float, float] | None:
    """Return a short orthogonal bridge when two markers face each other."""

    ax0, ay0, ax1, ay1 = first
    bx0, by0, bx1, by1 = second
    x0, x1 = max(ax0, bx0), min(ax1, bx1)
    if x1 > x0:
        if ay1 <= by0 and by0 - ay1 <= max_gap_um:
            return (x0, ay1 - overlap_um, x1, by0 + overlap_um)
        if by1 <= ay0 and ay0 - by1 <= max_gap_um:
            return (x0, by1 - overlap_um, x1, ay0 + overlap_um)
    y0, y1 = max(ay0, by0), min(ay1, by1)
    if y1 > y0:
        if ax1 <= bx0 and bx0 - ax1 <= max_gap_um:
            return (ax1 - overlap_um, y0, bx0 + overlap_um, y1)
        if bx1 <= ax0 and ax0 - bx1 <= max_gap_um:
            return (bx1 - overlap_um, y0, ax0 + overlap_um, y1)
    return None


def _expand_bbox(bbox: tuple[float, float, float, float], amount: float) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    return (x0 - amount, y0 - amount, x1 + amount, y1 + amount)


def _distance_to_bbox_y(y: float, bbox: tuple[float, float, float, float]) -> float:
    if bbox[1] <= y <= bbox[3]:
        return 0.0
    return min(abs(y - bbox[1]), abs(y - bbox[3]))

def _source_drop_avoid_bboxes_by_layer(
    device_plan: Any,
    accessor: Any,
    pdk: PdkConfig,
    *,
    supply_nets: tuple[str, ...],
) -> dict[str, tuple[tuple[float, float, float, float], ...]]:
    grouped: dict[str, list[tuple[float, float, float, float]]] = {}
    try:
        from analogskills.pcell import fallback_shapes_for_instance
    except Exception:  # pragma: no cover - defensive fallback for stripped runtimes
        fallback_shapes_for_instance = None
    metals = tuple(getattr(pdk.layer_map, "metals", ()))
    first_metal = str(metals[0]) if metals else ""
    first_metal_half = (
        max(_min_width_um(pdk, first_metal, pdk.rules.grid_step_um), pdk.rules.grid_step_um) / 2.0
        if first_metal
        else 0.0
    )
    for inst in tuple(getattr(device_plan, "instances", ())):
        for terminal, net in dict(getattr(inst, "connections", {}) or {}).items():
            net_name = str(net or "")
            if not net_name or net_name in supply_nets:
                continue
            try:
                pin = accessor.select_terminal_breakout(
                    inst,
                    str(terminal),
                    require_lvs_safe=False,
                    preferred_layers=(pdk.layer_map.metals[0],),
                )
            except Exception:
                continue
            layer = str(getattr(pin, "layer", "") or "")
            if not layer:
                continue
            bbox = getattr(pin, "bbox_um", None)
            if bbox is None:
                x, y = tuple(float(value) for value in getattr(pin, "xy_um", (0.0, 0.0)))
                half = max(_min_width_um(pdk, layer, pdk.rules.grid_step_um), pdk.rules.grid_step_um) / 2.0
                bbox = (x - half, y - half, x + half, y + half)
            grouped.setdefault(layer, []).append(tuple(float(value) for value in bbox))
            if first_metal and layer != first_metal:
                x, y = tuple(float(value) for value in getattr(pin, "xy_um", (0.0, 0.0)))
                projected_bbox = (
                    x - first_metal_half,
                    y - first_metal_half,
                    x + first_metal_half,
                    y + first_metal_half,
                )
                grouped.setdefault(first_metal, []).append(projected_bbox)
        if fallback_shapes_for_instance is None:
            continue
        try:
            fallback_shapes = tuple(fallback_shapes_for_instance(inst, pdk, snap_to_grid=True))
        except Exception:
            fallback_shapes = ()
        for shape in fallback_shapes:
            net_name = str(getattr(shape, "net", "") or "")
            if not net_name or net_name in supply_nets:
                continue
            layer = str(getattr(shape, "layer", "") or "")
            if not layer:
                continue
            bbox = tuple(float(value) for value in tuple(getattr(shape, "bbox", ()))[:4])
            if len(bbox) != 4:
                continue
            grouped.setdefault(layer, []).append(bbox)
    return {layer: tuple(boxes) for layer, boxes in grouped.items()}


def _source_drop_body_marker_fallback_pin(
    accessor: Any,
    inst: Any,
    terminal: str,
    pdk: PdkConfig,
    error: Exception,
):
    """Use configured/synthetic MOS body access when calibration exposes only markers.

    Some foundry MOS PCells report the bulk terminal as a PDK/NW marker rather
    than a routable metal pin.  That is correct for device recognition, but the
    source-drop planner must not silently drop the requested B-to-rail stitch:
    the compact StrongARM flow relies on these local stitches to keep Calibre's
    device extractor from classifying the native MOS rows as bad devices.
    """

    if not _source_drop_body_marker_fallback_enabled(pdk):
        return None
    if str(terminal) != "B" or str(getattr(inst, "logical_name", "")) not in {"nmos", "pmos"}:
        return None
    try:
        candidates = tuple(
            accessor.synthetic_terminal_pins(
                inst,
                terminal,
                preferred_layers=(pdk.layer_map.metals[0],),
            )
        )
    except Exception:
        return None
    if not candidates:
        return None
    pin = candidates[0]
    warnings = tuple(
        dict.fromkeys(
            [
                *tuple(getattr(pin, "warnings", ()) or ()),
                f"body marker fallback source-drop used for {getattr(inst, 'name', '')}.{terminal}: {error}",
            ]
        )
    )
    return replace(pin, source=f"{pin.source}_body_marker_drop", warnings=warnings)


def _source_drop_body_marker_fallback_enabled(pdk: PdkConfig) -> bool:
    metadata = dict(getattr(pdk, "metadata", {}) or {})
    power = dict(metadata.get("power", {}) or {})
    if "source_drop_body_marker_fallback" in power:
        return bool(power.get("source_drop_body_marker_fallback"))
    calibre = dict(metadata.get("calibre", {}) or {})
    lvs = dict(calibre.get("lvs", {}) or {})
    return bool(lvs.get("source_drop_body_marker_fallback"))


def _drop_points_to_rail(
    pin_xy: tuple[float, float],
    rail_bbox: tuple[float, float, float, float],
    grid_step_um: float,
    *,
    width_um: float = 0.0,
    keepout_um: float = 0.0,
    avoid_bboxes: tuple[tuple[float, float, float, float], ...] = (),
) -> tuple[tuple[float, float], ...]:
    x, y = pin_xy
    rx0, ry0, rx1, ry1 = rail_bbox
    target_y = (ry0 + ry1) / 2.0
    clamped_x = min(max(x, rx0), rx1)
    if abs(target_y - y) <= grid_step_um:
        return ((x, y),)

    step = max(width_um + keepout_um, 4.0 * grid_step_um, 0.05)
    candidates = [clamped_x]
    for scale in range(1, 7):
        for sign in (-1.0, 1.0):
            candidate = min(max(clamped_x + sign * scale * step, rx0), rx1)
            if all(abs(candidate - existing) > 1e-12 for existing in candidates):
                candidates.append(candidate)

    best_score: tuple[int, float] | None = None
    best_points: tuple[tuple[float, float], ...] = ((x, y), (clamped_x, target_y))
    half = max(width_um, grid_step_um) / 2.0
    for candidate_x in candidates:
        candidate_points = [(x, y)]
        if abs(candidate_x - x) > grid_step_um:
            candidate_points.append((candidate_x, y))
        candidate_points.append((candidate_x, target_y))
        segments = []
        for a, b in zip(candidate_points, candidate_points[1:]):
            x0, x1 = sorted((float(a[0]), float(b[0])))
            y0, y1 = sorted((float(a[1]), float(b[1])))
            segments.append((x0 - half, y0 - half, x1 + half, y1 + half))
        conflicts = 0
        for obstacle in avoid_bboxes:
            inflated = (
                obstacle[0] - keepout_um,
                obstacle[1] - keepout_um,
                obstacle[2] + keepout_um,
                obstacle[3] + keepout_um,
            )
            if any(_bbox_overlaps(segment, inflated) for segment in segments):
                conflicts += 1
        score = (conflicts, abs(candidate_x - x))
        if best_score is None or score < best_score:
            best_score = score
            best_points = tuple(candidate_points)
            if conflicts == 0 and abs(candidate_x - x) <= grid_step_um:
                break
    return best_points


def _bbox_overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _via_stack_between_layers(
    pdk: PdkConfig,
    start_layer: str,
    end_layer: str,
    xy: tuple[float, float],
    net: str,
    contact_layer: str,
) -> tuple[object, ...]:
    from analogskills.eda.oa import OaVia

    if start_layer == end_layer:
        return ()
    vias: list[OaVia] = []
    metals = pdk.layer_map.metals
    current = start_layer
    if start_layer not in metals and metals:
        vias.append(
            OaVia(
                contact_layer or pdk.layer_map.contact,
                xy,
                net,
                metadata={"landing_layers": (start_layer, metals[0])},
            )
        )
        current = metals[0]
    start_idx = _metal_index(metals, current)
    end_idx = _metal_index(metals, end_layer)
    if start_idx is None or end_idx is None:
        return tuple(vias)
    step = 1 if end_idx >= start_idx else -1
    for idx in range(start_idx, end_idx, step):
        via_idx = idx if step > 0 else idx - 1
        if 0 <= via_idx < len(pdk.layer_map.vias):
            lower = metals[min(idx, idx + step)]
            upper = metals[max(idx, idx + step)]
            vias.append(OaVia(pdk.layer_map.vias[via_idx], xy, net, metadata={"landing_layers": (lower, upper)}))
    return tuple(vias)


def _source_drop_route_layer(pdk: PdkConfig, *, start_layer: str, rail_layer: str) -> str:
    metals = tuple(getattr(pdk.layer_map, "metals", ()))
    if not metals:
        return rail_layer or start_layer
    try:
        rail_idx = metals.index(str(rail_layer))
    except ValueError:
        rail_idx = 0
    try:
        start_idx = metals.index(str(start_layer))
    except ValueError:
        start_idx = 0
    candidate_idx = max(start_idx, rail_idx)
    if candidate_idx == 0 and len(metals) > 1:
        candidate_idx = 1
    return str(metals[candidate_idx])


def _metal_index(metals: tuple[str, ...], layer: str) -> int | None:
    try:
        return metals.index(layer)
    except ValueError:
        return None


def _instances_bbox_um(instances: tuple[Any, ...]) -> tuple[float, float, float, float] | None:
    if not instances:
        return None
    boxes = [_instance_bbox_um(inst) for inst in instances]
    return _bbox_union_all(tuple(boxes))


def _instance_bbox_um(inst: Any) -> tuple[float, float, float, float]:
    x, y = getattr(inst, "xy_um", getattr(inst, "xy", (0.0, 0.0)))
    width = float(getattr(inst, "width_um", 0.0) or 0.0)
    height = float(getattr(inst, "height_um", 0.0) or 0.0)
    orient = str(getattr(inst, "orient", "R0") or "R0")
    bx0 = float(getattr(inst, "bbox_x0_um", 0.0) or 0.0)
    by0 = float(getattr(inst, "bbox_y0_um", 0.0) or 0.0)
    corners = tuple(
        _transform_local_point(px, py, orient)
        for px, py in ((bx0, by0), (bx0 + width, by0), (bx0, by0 + height), (bx0 + width, by0 + height))
    )
    xs = tuple(float(x) + point[0] for point in corners)
    ys = tuple(float(y) + point[1] for point in corners)
    return (min(xs), min(ys), max(xs), max(ys))


def _transform_local_point(x: float, y: float, orient: str) -> tuple[float, float]:
    transforms = {
        "R0": (x, y), "R90": (-y, x), "R180": (-x, -y), "R270": (y, -x),
        "MX": (x, -y), "MY": (-x, y), "MXR90": (y, x), "MYR90": (-y, -x),
    }
    return transforms.get(orient, (x, y))


def _bbox_union_all(boxes: tuple[tuple[float, float, float, float], ...]) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _pin_bbox_for_rail(rail: PowerRailSpec) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = rail.bbox
    pin_width = min(max(rail.width_um, 0.1), max(x1 - x0, rail.width_um))
    if rail.side == "bottom":
        return (x0, y0, x0 + pin_width, y1)
    return (x1 - pin_width, y0, x1, y1)


def _device_marker_boxes(device_plan: Any, pdk: PdkConfig) -> dict[str, tuple[tuple[float, float, float, float], ...]]:
    try:
        from analogskills.contracts import Device, DeviceRole
        from analogskills.pcell import PCellInstancePlan, estimate_pcell_bbox_um, fallback_shapes_for_instance
    except Exception:
        return {
            "pmos_active": (),
            "pmos_gate_or_active": (),
            "nmos_active": (),
        }

    active_layer = str(getattr(pdk.layer_map, "active", "") or "")
    gate_layer = str(getattr(pdk.layer_map, "gate", "") or "")
    pmos_active: list[tuple[float, float, float, float]] = []
    pmos_gate_or_active: list[tuple[float, float, float, float]] = []
    nmos_active: list[tuple[float, float, float, float]] = []
    for inst in tuple(getattr(device_plan, "instances", ())):
        logical_name = str(getattr(inst, "logical_name", "") or "").lower()
        if logical_name not in {"pmos", "nmos"}:
            continue
        params = dict(getattr(inst, "params", {}) or {})
        role = DeviceRole.BIAS if logical_name in {"pmos", "nmos"} else DeviceRole.PASSIVE
        device = Device(
            name=str(getattr(inst, "name", "") or ""),
            role=role,
            model=logical_name,
            terminals=tuple(str(term) for term in dict(getattr(inst, "connections", {}) or {})),
        )
        try:
            width_um, height_um = estimate_pcell_bbox_um(device, params)
        except Exception:
            width_um = float(getattr(inst, "width_um", params.get("width_um", params.get("w_um", 0.0))) or 0.0)
            height_um = float(getattr(inst, "height_um", params.get("height_um", params.get("h_um", 0.0))) or 0.0)
            width_um = max(width_um, 0.2)
            height_um = max(height_um, 0.2)
        marker_instance = PCellInstancePlan(
            name=str(getattr(inst, "name", "") or ""),
            logical_name=logical_name,
            lib_name=str(getattr(inst, "lib_name", "work") or "work"),
            cell_name=str(getattr(inst, "cell_name", logical_name) or logical_name),
            view_name=str(getattr(inst, "view_name", "layout") or "layout"),
            params=params,
            xy_um=tuple(float(value) for value in tuple(getattr(inst, "xy_um", getattr(inst, "xy", (0.0, 0.0))))[:2]),
            orient=str(getattr(inst, "orient", "R0") or "R0"),
            connections=dict(getattr(inst, "connections", {}) or {}),
            width_um=max(width_um, 0.2),
            height_um=max(height_um, 0.2),
        )
        shapes = tuple(fallback_shapes_for_instance(marker_instance, pdk, snap_to_grid=True))
        active_boxes = tuple(
            tuple(float(value) for value in shape.bbox)
            for shape in shapes
            if str(getattr(shape, "layer", "") or "") == active_layer
        )
        gate_boxes = tuple(
            tuple(float(value) for value in shape.bbox)
            for shape in shapes
            if str(getattr(shape, "layer", "") or "") == gate_layer
        )
        if logical_name == "pmos":
            if active_boxes:
                pmos_active.extend(active_boxes)
            if active_boxes or gate_boxes:
                pmos_gate_or_active.append(_bbox_union_all(tuple((*active_boxes, *gate_boxes) or active_boxes)))
        else:
            nmos_active.extend(active_boxes)
    return {
        "pmos_active": tuple(pmos_active),
        "pmos_gate_or_active": tuple(pmos_gate_or_active),
        "nmos_active": tuple(nmos_active),
    }
