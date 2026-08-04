"""Structured OpenAccess write plans and SKILL emitters.

The functions here do not require an OA Python binding at import time. They build a
serializable write plan that can be emitted as SKILL and executed by Virtuoso in
batch mode, or consumed by a future native OA backend.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import product
import json
from pathlib import Path
from typing import Mapping, Sequence

from analogskills._utils import coerce_dimension_m
from analogskills.contracts import NetRole, TerminalRef, TopologyGraph
from analogskills.layout import Placement, RoutedNet
from analogskills.layout.ir import LayoutCellRef, LayoutInstance, LayoutLabel, LayoutPath, LayoutPin, LayoutPlan, LayoutRect, LayoutVia, layout_plan_nets
from analogskills.pdk import DesignRuleDeck, PdkConfig
from analogskills.repair import LayoutShape


@dataclass(frozen=True)
class OaCellView:
    lib: str
    cell: str
    view: str
    view_type: str
    mode: str = "w"


@dataclass(frozen=True)
class OaInstance:
    name: str
    lib: str
    cell: str
    view: str
    xy: tuple[float, float] = (0.0, 0.0)
    orient: str = "R0"
    connections: dict[str, str] = field(default_factory=dict)
    params: dict[str, object] = field(default_factory=dict)
    instantiation_method: str = "dbCreateInstByMasterName"
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OaRect:
    layer: str
    purpose: str
    bbox: tuple[float, float, float, float]
    net: str = ""
    color: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OaPin:
    name: str
    net: str
    direction: str = "inputOutput"
    layer: str = "M1"
    bbox: tuple[float, float, float, float] | None = None
    emit_draw_rect: bool = True


@dataclass(frozen=True)
class OaPath:
    layer: str
    purpose: str
    points: tuple[tuple[float, float], ...]
    width: float
    net: str = ""
    color: str = ""


@dataclass(frozen=True)
class OaVia:
    via_def: str
    xy: tuple[float, float]
    net: str = ""
    rows: int = 1
    cols: int = 1
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OaWritePlan:
    cellview: OaCellView
    nets: tuple[str, ...] = ()
    pins: tuple[OaPin, ...] = ()
    instances: tuple[OaInstance, ...] = ()
    rects: tuple[OaRect, ...] = ()
    labels: tuple[tuple[str, str, tuple[float, float]], ...] = ()
    paths: tuple[OaPath, ...] = ()
    vias: tuple[OaVia, ...] = ()


def layout_plan_to_oa_write_plan(plan: LayoutPlan) -> OaWritePlan:
    """Adapt backend-neutral LayoutIR into an OA/SKILL write plan."""

    return OaWritePlan(
        OaCellView(plan.cell.lib, plan.cell.cell, plan.cell.view, plan.cell.view_type),
        nets=layout_plan_nets(plan),
        pins=tuple(OaPin(pin.name, pin.net, pin.direction, pin.layer, pin.bbox) for pin in plan.pins),
        instances=tuple(
            OaInstance(
                inst.name,
                inst.master.lib,
                inst.master.cell,
                inst.master.view,
                xy=inst.xy,
                orient=inst.orient,
                connections=inst.connections,
                params=inst.params,
                instantiation_method=str(inst.metadata.get("instantiation_method", "dbCreateInstByMasterName")),
                metadata=dict(getattr(inst, "metadata", {}) or {}),
            )
            for inst in plan.instances
            if str(dict(getattr(inst, "metadata", {}) or {}).get("instantiation_method", "")) != "drawn_primitive"
        ),
        rects=tuple(
            OaRect(rect.layer, rect.purpose, rect.bbox, rect.net, getattr(rect, "color", ""), dict(getattr(rect, "metadata", {}) or {}))
            for rect in plan.rects
        ),
        labels=tuple((label.layer, label.text, label.xy) for label in plan.labels),
        paths=tuple(OaPath(path.layer, path.purpose, path.points, path.width, path.net, getattr(path, "color", "")) for path in plan.paths),
        vias=tuple(
            OaVia(via.via_def, via.xy, via.net, via.rows, via.cols, dict(getattr(via, "metadata", {}) or {}))
            for via in plan.vias
        ),
    )


def oa_write_plan_to_layout_plan(plan: OaWritePlan) -> LayoutPlan:
    """Adapt an OA/SKILL write plan into backend-neutral LayoutIR."""

    return LayoutPlan(
        LayoutCellRef(plan.cellview.lib, plan.cellview.cell, plan.cellview.view, plan.cellview.view_type),
        nets=tuple(plan.nets),
        pins=tuple(LayoutPin(pin.name, pin.net, pin.direction, pin.layer, pin.bbox) for pin in plan.pins),
        instances=tuple(
            LayoutInstance(
                inst.name,
                LayoutCellRef(inst.lib, inst.cell, inst.view, ""),
                xy=inst.xy,
                orient=inst.orient,
                connections=dict(inst.connections),
                params=dict(inst.params),
                metadata={**dict(getattr(inst, "metadata", {}) or {}), "instantiation_method": inst.instantiation_method},
            )
            for inst in plan.instances
        ),
        rects=tuple(LayoutRect(rect.layer, rect.bbox, rect.net, rect.purpose, dict(getattr(rect, "metadata", {}) or {})) for rect in plan.rects),
        paths=tuple(LayoutPath(path.layer, path.points, path.width, path.net, path.purpose) for path in plan.paths),
        vias=tuple(
            LayoutVia(via.via_def, via.xy, via.net, via.rows, via.cols, dict(getattr(via, "metadata", {}) or {}))
            for via in plan.vias
        ),
        labels=tuple(LayoutLabel(layer, text, xy) for layer, text, xy in plan.labels),
    )


def merge_oa_write_plans(
    *plans: OaWritePlan,
    cellview: OaCellView | None = None,
    grid: DesignRuleDeck | PdkConfig | int | None = None,
    snap_to_grid: bool = True,
) -> OaWritePlan:
    """Merge reviewed OA proposal plans without adding physical decisions."""
    if not plans:
        raise ValueError("at least one OA write plan is required")
    target_cellview = cellview or plans[0].cellview
    merged = OaWritePlan(
        target_cellview,
        nets=tuple(dict.fromkeys(net for plan in plans for net in plan.nets if net)),
        pins=tuple(pin for plan in plans for pin in plan.pins),
        instances=tuple(inst for plan in plans for inst in plan.instances),
        rects=tuple(rect for plan in plans for rect in plan.rects),
        labels=tuple(label for plan in plans for label in plan.labels),
        paths=tuple(path_obj for plan in plans for path_obj in plan.paths),
        vias=tuple(via for plan in plans for via in plan.vias),
    )
    return snap_oa_write_plan_to_grid(merged, grid) if grid is not None and snap_to_grid else merged


def snap_oa_write_plan_to_grid(
    plan: OaWritePlan,
    grid: DesignRuleDeck | PdkConfig | int,
    *,
    bbox_mode: str = "outward",
    snap_instances: bool = True,
) -> OaWritePlan:
    rules = _grid_rules(grid)
    return OaWritePlan(
        plan.cellview,
        nets=plan.nets,
        pins=tuple(_snap_pin_to_grid(pin, rules, bbox_mode=bbox_mode) for pin in plan.pins),
        instances=tuple(replace(inst, xy=rules.snap_point_um(inst.xy)) for inst in plan.instances) if snap_instances else plan.instances,
        rects=tuple(_snap_rect_to_grid(rect, rules, bbox_mode=bbox_mode) for rect in plan.rects),
        labels=tuple((layer, text, rules.snap_point_um(xy)) for layer, text, xy in plan.labels),
        paths=tuple(_snap_path_to_grid(path_obj, rules) for path_obj in plan.paths),
        vias=tuple(replace(via, xy=rules.snap_point_um(via.xy)) for via in plan.vias),
    )


def validate_oa_write_plan_grid(
    plan: OaWritePlan,
    grid: DesignRuleDeck | PdkConfig | int,
    *,
    tol_um: float = 1e-12,
    validate_instances: bool = True,
) -> list[str]:
    rules = _grid_rules(grid)
    issues: list[str] = []
    if validate_instances:
        for inst in plan.instances:
            issues.extend(_point_grid_issues(f"instance {inst.name}.xy", inst.xy, rules, tol_um=tol_um))
    for rect in plan.rects:
        issues.extend(_bbox_grid_issues(f"rect {rect.layer}/{rect.net or 'no_net'}", rect.bbox, rules, tol_um=tol_um))
    for pin in plan.pins:
        if pin.bbox is not None:
            issues.extend(_bbox_grid_issues(f"pin {pin.name}.bbox", pin.bbox, rules, tol_um=tol_um))
    for path_obj in plan.paths:
        if not rules.is_on_grid_um(path_obj.width, tol_um=tol_um):
            issues.append(f"path {path_obj.net or path_obj.layer}.width={path_obj.width:g}um is off-grid for {rules.grid_nm}nm grid")
        for idx, point in enumerate(path_obj.points):
            issues.extend(_point_grid_issues(f"path {path_obj.net or path_obj.layer}.points[{idx}]", point, rules, tol_um=tol_um))
    for via in plan.vias:
        issues.extend(_point_grid_issues(f"via {via.via_def}.xy", via.xy, rules, tol_um=tol_um))
    for idx, (_layer, text, xy) in enumerate(plan.labels):
        issues.extend(_point_grid_issues(f"label {text or idx}.xy", xy, rules, tol_um=tol_um))
    return issues


def analyze_lvs_pin_label_stamping(
    plan: object,
    *,
    top_level_nets: Sequence[str] | None = None,
    pdk: PdkConfig | None = None,
    require_explicit_labels: bool = False,
    pin_net_aliases: Mapping[str, str] | None = None,
    allow_label_only_top_level_nets: bool = False,
) -> dict[str, object]:
    """Precheck top-level pins and text labels before Calibre LVS streamout."""

    pins = _plan_pins(plan)
    labels = _plan_labels(plan)
    alias_map = {str(k): str(v) for k, v in dict(pin_net_aliases or {}).items() if str(k) and str(v)}
    reverse_alias_map: dict[str, tuple[str, ...]] = {}
    for pin_name, net_name in alias_map.items():
        reverse_alias_map.setdefault(net_name, tuple())
    for pin_name, net_name in alias_map.items():
        reverse_alias_map[net_name] = tuple(dict.fromkeys((*reverse_alias_map.get(net_name, ()), pin_name)))

    required_pin_names = tuple(dict.fromkeys(top_level_nets or tuple(pin.name for pin in pins if pin.name)))
    required_nets = tuple(dict.fromkeys(alias_map.get(pin_name, pin_name) for pin_name in required_pin_names))
    issues: list[str] = []
    pins_by_net: dict[str, list[OaPin]] = {}
    for pin in pins:
        if pin.net:
            pins_by_net.setdefault(pin.net, []).append(pin)

    label_nets: dict[str, list[tuple[str, str, tuple[float, float]]]] = {}
    for label in labels:
        layer, text, xy = label
        if text:
            label_nets.setdefault(text, []).append((layer, text, xy))

    top_level_presence: dict[str, str] = {}
    for pin_name, net in zip(required_pin_names, required_nets):
        pins = pins_by_net.get(net, [])
        explicit_label_keys = {net, pin_name, *reverse_alias_map.get(net, ())}
        if not pins:
            if allow_label_only_top_level_nets and any(label_nets.get(key) for key in explicit_label_keys):
                top_level_presence[net] = "label_only"
            else:
                issues.append(f"missing top-level pin for net {pin_name}")
                continue
        else:
            top_level_presence[net] = "pin"
        if len(pins) > 1:
            issues.append(f"duplicate top-level pins for net {pin_name}: {len(pins)}")
        for pin in pins:
            issues.extend(_pin_stamping_issues(plan, pin))
        if require_explicit_labels and not any(label_nets.get(key) for key in explicit_label_keys):
            issues.append(f"missing explicit text label for net {pin_name}")

    known_nets = set(str(net) for net in getattr(plan, "nets", ()) if str(net)) | set(pins_by_net) | {net for _layer, net, _bbox in _drawing_bboxes(plan)}
    for layer, text, xy in labels:
        if not text:
            issues.append(f"empty label on {layer} at {xy}")
            continue
        if text not in known_nets and text not in alias_map:
            issues.append(f"label {text} has no matching net in OA write plan")
        if pdk is not None and layer not in pdk.layer_map.metals:
            issues.append(f"label {text} uses non-metal layer {layer}")
        stamp_net = alias_map.get(text, text)
        issues.extend(_label_stamping_issues(plan, layer, stamp_net, xy))

    unique_pin_nets = tuple(sorted(pins_by_net))
    present_top_nets = tuple(net for net in required_nets if net in top_level_presence)
    missing = tuple(pin_name for pin_name, net in zip(required_pin_names, required_nets) if net not in top_level_presence)
    extra = tuple(net for net in unique_pin_nets if required_nets and net not in required_nets)
    if required_nets and len(present_top_nets) != len(required_nets):
        issues.append(f"port count {len(present_top_nets)} does not match schematic pin count {len(required_nets)}")
    if extra:
        issues.append(f"extra top-level pins not in schematic pins: {extra}")

    return {
        "passed": not issues,
        "issues": issues,
        "pin_count": len(unique_pin_nets),
        "present_top_level_count": len(present_top_nets),
        "required_pin_count": len(required_nets),
        "missing_nets": missing,
        "extra_nets": extra,
        "label_count": len(labels),
    }


def build_oa_schematic_plan(
    graph: TopologyGraph,
    *,
    lib: str,
    cell: str,
    view: str = "schematic",
    pcell_lib: str = "pdk",
    symbol_view: str = "symbol",
    sizing: Mapping[str, Mapping[str, object]] | None = None,
    pdk: PdkConfig | None = None,
) -> OaWritePlan:
    term_map = graph.terminal_net_map()
    instances = []
    for idx, device in enumerate(graph.devices.values()):
        connections = {term: term_map.get(TerminalRef(device.name, term), "") for term in device.terminals}
        params = _schematic_params_for_device(device.name, device.model, dict(device.parameters), sizing or {}, pdk)
        inst_lib = pcell_lib
        inst_cell = device.model
        if pdk is not None:
            try:
                template = pdk.pcell_template_for(_logical_pcell_name(device.model, ""))
                inst_lib = template.resolved_schematic_lib_name()
                inst_cell = template.resolved_schematic_cell_name()
                inst_method = template.resolved_schematic_instantiation_method()
            except KeyError:
                inst_method = "dbCreateInstByMasterName"
        else:
            inst_method = "dbCreateInstByMasterName"
        instances.append(
            OaInstance(
                name=device.name,
                lib=inst_lib,
                cell=inst_cell,
                view=symbol_view,
                xy=(20.0 * idx, 0.0),
                orient="R0",
                connections=connections,
                params=params,
                instantiation_method=inst_method,
            )
        )
    pins = tuple(OaPin(name, name, _pin_direction(role)) for name, role in graph.pins.items())
    nets = tuple(dict.fromkeys([*graph.pins.keys(), *graph.nets.keys()]))
    return OaWritePlan(OaCellView(lib, cell, view, "schematic"), nets=nets, pins=pins, instances=tuple(instances))


def build_lvs_pins(
    plan: OaWritePlan,
    pdk: PdkConfig | None = None,
    *,
    top_level_nets: Sequence[str] | None = None,
    top_level_pin_nets: Mapping[str, str] | None = None,
    allow_placeholder_pins: bool = True,
    pin_selection_policy: str = "safe_first",
) -> tuple[OaPin, ...]:
    """Build Calibre-visible pins for top-level terminals.

    If a terminal net has no existing geometry, a small default M1 pin is
    created outside the expected device area so that ``dbCreatePin`` and the
    accompanying text label can still be exported to GDS for LVS.
    """
    default_layer = pdk.layer_map.metals[0] if pdk is not None else "M1"
    pins: list[OaPin] = []
    seen: set[str] = set()
    rects = plan.rects
    paths = plan.paths
    default_index = 0

    if top_level_pin_nets is not None:
        candidate_pins = [(str(pin_name), str(net_name)) for pin_name, net_name in top_level_pin_nets.items()]
    else:
        candidate_nets = list(top_level_nets) if top_level_nets is not None else [pin.net for pin in plan.pins]
        candidate_pins = [(str(net), str(net)) for net in candidate_nets]
    if str(pin_selection_policy).lower() in {"boundary_aware", "aesthetic_boundary", "coordinated_boundary"}:
        return _build_boundary_aware_lvs_pins(
            plan,
            pdk,
            candidate_pins,
            default_layer=default_layer,
            allow_placeholder_pins=allow_placeholder_pins,
            pin_selection_policy=pin_selection_policy,
        )
    for pin_name, net in candidate_pins:
        if not net or not pin_name or net in seen:
            continue
        seen.add(net)
        if pdk is not None:
            preferred_layers = tuple(
                dict.fromkeys(
                    (
                        *tuple(str(layer) for layer in pdk.preferred_signal_layers),
                        *tuple(str(layer) for layer in pdk.preferred_power_layers),
                        *tuple(str(layer) for layer in pdk.layer_map.metals),
                    )
                )
            )
        else:
            preferred_layers = ()
        # Prefer routed access on signal/power layers over device-internal
        # shapes, but do not blindly use the first segment: dense analog routes
        # can overlap other nets at one endpoint while another point on the same
        # net is a valid Calibre stamping location.
        selected = _select_lvs_pin_geometry(
            plan,
            pin_name,
            net,
            pdk,
            preferred_layers,
            default_layer,
            pin_selection_policy=pin_selection_policy,
        )
        if selected is not None:
            layer, bbox = selected
        else:
            layer = default_layer
            bbox = None
        if bbox is None and allow_placeholder_pins:
            y = 20.0 + default_index * 0.5
            bbox = (0.0, y, 0.2, y + 0.2)
            default_index += 1
        if bbox is None:
            continue
        pins.append(OaPin(pin_name, net, "inputOutput", layer or default_layer, bbox))
    return tuple(pins)


def _build_boundary_aware_lvs_pins(
    plan: object,
    pdk: PdkConfig | None,
    candidate_pins: Sequence[tuple[str, str]],
    *,
    default_layer: str,
    allow_placeholder_pins: bool,
    pin_selection_policy: str,
) -> tuple[OaPin, ...]:
    """Build safe LVS pins while coordinating top-level pin side choices.

    The candidate bboxes are constrained to overlap existing same-net drawing
    geometry.  For boundary-aware/coordinated policies, the selector chooses
    among safe candidates jointly so the final top-level pins use at most two
    block sides when the physical routes provide enough choices.
    """

    preferred_layers = _preferred_lvs_pin_layers(pdk)
    plan_bbox = _drawing_bbox(plan)
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    default_index = 0
    for pin_name, net in candidate_pins:
        if not net or not pin_name or net in seen:
            continue
        seen.add(net)
        candidates = _safe_lvs_pin_candidates(
            plan,
            pin_name,
            net,
            pdk,
            preferred_layers,
            default_layer,
            contained_pin_bboxes=True,
        )
        if candidates:
            rows.append({"pin_name": pin_name, "net": net, "candidates": candidates})
            continue
        fallback = _first_lvs_pin_candidate(plan, net, pdk, preferred_layers)
        if fallback is not None:
            layer, bbox = fallback
            rows.append(
                {
                    "pin_name": pin_name,
                    "net": net,
                    "candidates": (
                        {
                            "layer": layer or default_layer,
                            "bbox": bbox,
                            "side": _nearest_bbox_side(bbox, plan_bbox),
                            "cost": _lvs_pin_boundary_selection_cost(bbox, plan_bbox, index=0),
                        },
                    ),
                }
            )
            continue
        if allow_placeholder_pins:
            y = 20.0 + default_index * 0.5
            bbox = (0.0, y, 0.2, y + 0.2)
            default_index += 1
            rows.append(
                {
                    "pin_name": pin_name,
                    "net": net,
                    "candidates": (
                        {
                            "layer": default_layer,
                            "bbox": bbox,
                            "side": _nearest_bbox_side(bbox, plan_bbox),
                            "cost": _lvs_pin_boundary_selection_cost(bbox, plan_bbox, index=0),
                        },
                    ),
                }
            )

    selected = _select_coordinated_lvs_pin_candidates(rows, plan_bbox, policy=pin_selection_policy)
    pins: list[OaPin] = []
    for row, candidate in zip(rows, selected):
        pins.append(
            OaPin(
                str(row.get("pin_name", "")),
                str(row.get("net", "")),
                "inputOutput",
                str(candidate.get("layer", default_layer) or default_layer),
                _bbox_tuple(candidate.get("bbox")),
            )
        )
    return tuple(pins)


def _preferred_lvs_pin_layers(pdk: PdkConfig | None) -> tuple[str, ...]:
    if pdk is None:
        return ()
    return tuple(
        dict.fromkeys(
            (
                *tuple(str(layer) for layer in pdk.preferred_signal_layers),
                *tuple(str(layer) for layer in pdk.preferred_power_layers),
                *tuple(str(layer) for layer in pdk.layer_map.metals),
            )
        )
    )


def _safe_lvs_pin_candidates(
    plan: object,
    pin_name: str,
    net: str,
    pdk: PdkConfig | None,
    preferred_layers: Sequence[str],
    default_layer: str,
    *,
    contained_pin_bboxes: bool,
) -> tuple[dict[str, object], ...]:
    plan_bbox = _drawing_bbox(plan)
    safe: list[dict[str, object]] = []
    for index, (layer, bbox) in enumerate(
        _iter_lvs_pin_geometry_candidates(
            plan,
            net,
            pdk,
            preferred_layers,
            contained_pin_bboxes=contained_pin_bboxes,
        )
    ):
        layer_name = layer or default_layer
        candidate = OaPin(pin_name, net, "inputOutput", layer_name, bbox)
        if _pin_stamping_issues(plan, candidate):
            continue
        safe.append(
            {
                "layer": layer_name,
                "bbox": bbox,
                "side": _nearest_bbox_side(bbox, plan_bbox),
                "cost": _lvs_pin_boundary_selection_cost(bbox, plan_bbox, index=index),
            }
        )
    return tuple(safe)


def _first_lvs_pin_candidate(
    plan: object,
    net: str,
    pdk: PdkConfig | None,
    preferred_layers: Sequence[str],
) -> tuple[str, tuple[float, float, float, float]] | None:
    candidates = _iter_lvs_pin_geometry_candidates(
        plan,
        net,
        pdk,
        preferred_layers,
        contained_pin_bboxes=False,
    )
    return candidates[0] if candidates else None


def _select_coordinated_lvs_pin_candidates(
    rows: Sequence[Mapping[str, object]],
    plan_bbox: tuple[float, float, float, float] | None,
    *,
    policy: str,
) -> tuple[Mapping[str, object], ...]:
    if not rows:
        return ()
    candidate_lists = tuple(
        _prune_lvs_pin_candidates_for_joint_selection(tuple(_mapping_candidate(candidate) for candidate in tuple(row.get("candidates", ()) or ())))
        for row in rows
    )
    if not candidate_lists or any(not candidates for candidates in candidate_lists):
        return tuple(_mapping_candidate(tuple(row.get("candidates", ()) or ())[0]) for row in rows if tuple(row.get("candidates", ()) or ()))
    combination_count = 1
    for candidates in candidate_lists:
        combination_count *= max(1, len(candidates))
    if len(candidate_lists) > 7 or combination_count > 250_000:
        return tuple(min(candidates, key=lambda candidate: _candidate_cost_tuple(candidate)) for candidates in candidate_lists)

    best_selection: tuple[Mapping[str, object], ...] | None = None
    best_key: tuple[int, int, int, int] | None = None
    for selection in product(*candidate_lists):
        score = _pin_selection_proxy_score(selection, plan_bbox)
        boundary_cost = sum(_candidate_cost_tuple(candidate)[0] for candidate in selection)
        area_cost = sum(_candidate_cost_tuple(candidate)[1] for candidate in selection)
        side_count = len(tuple(dict.fromkeys(str(candidate.get("side", "")) for candidate in selection if str(candidate.get("side", "")))))
        key = (
            int(round(score * 1_000_000)),
            -int(boundary_cost),
            -int(area_cost),
            -int(side_count),
        )
        if best_key is None or key > best_key:
            best_key = key
            best_selection = tuple(selection)
    return best_selection or tuple(min(candidates, key=lambda candidate: _candidate_cost_tuple(candidate)) for candidates in candidate_lists)


def _prune_lvs_pin_candidates_for_joint_selection(
    candidates: Sequence[Mapping[str, object]],
    *,
    max_per_side: int = 3,
    max_total: int = 14,
) -> tuple[Mapping[str, object], ...]:
    by_side: dict[str, list[Mapping[str, object]]] = {}
    for candidate in candidates:
        by_side.setdefault(str(candidate.get("side", "")), []).append(candidate)
    selected: list[Mapping[str, object]] = []
    seen: set[tuple[str, tuple[int, int, int, int]]] = set()
    for side in sorted(by_side):
        for candidate in sorted(by_side[side], key=lambda row: _candidate_cost_tuple(row))[: max(1, int(max_per_side))]:
            bbox = _bbox_tuple(candidate.get("bbox"))
            key = (str(candidate.get("layer", "")), tuple(int(round(value * 1_000_000)) for value in bbox))
            if key in seen:
                continue
            seen.add(key)
            selected.append(candidate)
    selected = sorted(selected, key=lambda row: _candidate_cost_tuple(row))[: max(1, int(max_total))]
    return tuple(selected)


def _pin_selection_proxy_score(
    candidates: Sequence[Mapping[str, object]],
    plan_bbox: tuple[float, float, float, float] | None,
) -> float:
    if not candidates or plan_bbox is None:
        return 100.0
    px0, py0, px1, py1 = plan_bbox
    width = max(0.0, px1 - px0)
    height = max(0.0, py1 - py0)
    scale = max(min(width, height) * 0.08, 0.1)
    boundary_scores: list[float] = []
    side_positions: dict[str, list[float]] = {}
    side_counts: dict[str, int] = {}
    for candidate in candidates:
        bbox = _bbox_tuple(candidate.get("bbox"))
        cx, cy = _bbox_center(bbox)
        distances = {
            "left": abs(cx - px0),
            "right": abs(cx - px1),
            "bottom": abs(cy - py0),
            "top": abs(cy - py1),
        }
        side = min(distances, key=distances.get)
        side_counts[side] = side_counts.get(side, 0) + 1
        side_positions.setdefault(side, []).append(cy if side in {"left", "right"} else cx)
        boundary_scores.append(_proxy_score_escape(distances[side], scale))
    boundary_score = sum(boundary_scores) / len(boundary_scores)
    alignment_parts = [_proxy_spacing_uniformity_score(values) for values in side_positions.values()]
    alignment_score = sum(alignment_parts) / len(alignment_parts) if alignment_parts else 100.0
    side_count = len(side_counts)
    if side_count <= 2:
        side_score = 100.0
    elif side_count == 3:
        side_score = 78.0
    else:
        side_score = 62.0
    return 0.45 * boundary_score + 0.35 * alignment_score + 0.20 * side_score


def _proxy_score_escape(escape_um: float, scale_um: float) -> float:
    if scale_um <= 0:
        return 100.0 if escape_um <= 0 else 50.0
    return max(0.0, min(100.0, 100.0 * (1.0 - float(escape_um) / max(scale_um, 1e-12))))


def _proxy_spacing_uniformity_score(values: Sequence[float]) -> float:
    vals = sorted(float(value) for value in values)
    if len(vals) <= 2:
        return 100.0
    gaps = [right - left for left, right in zip(vals, vals[1:]) if right > left]
    if len(gaps) <= 1:
        return 100.0
    mean = sum(gaps) / len(gaps)
    if mean <= 1e-12:
        return 100.0
    deviation = sum(abs(gap - mean) for gap in gaps) / (len(gaps) * mean)
    return max(0.0, min(100.0, 100.0 * (1.0 - min(deviation, 1.0))))


def _candidate_side_sets(rows: Sequence[Mapping[str, object]]) -> tuple[tuple[str, ...], ...]:
    sides = tuple(
        dict.fromkeys(
            str(candidate.get("side", ""))
            for row in rows
            for candidate in tuple(row.get("candidates", ()) or ())
            if str(candidate.get("side", ""))
        )
    )
    if not sides:
        return ((),)
    result: list[tuple[str, ...]] = [(side,) for side in sides]
    for left_index, left in enumerate(sides):
        for right in sides[left_index + 1 :]:
            result.append((left, right))
    return tuple(result)


def _mapping_candidate(candidate: object) -> Mapping[str, object]:
    return candidate if isinstance(candidate, Mapping) else {}


def _candidate_cost_tuple(candidate: Mapping[str, object]) -> tuple[int, int, int]:
    raw = candidate.get("cost", (0, 0, 0))
    if isinstance(raw, (tuple, list)) and len(raw) >= 3:
        try:
            return (int(raw[0]), int(raw[1]), int(raw[2]))
        except (TypeError, ValueError):
            pass
    bbox = _bbox_tuple(candidate.get("bbox"))
    return _lvs_pin_boundary_selection_cost(bbox, None, index=0)


def _pin_side_spread_cost(
    candidates: Sequence[Mapping[str, object]],
    plan_bbox: tuple[float, float, float, float] | None,
) -> int:
    if plan_bbox is None:
        return 0
    side_positions: dict[str, list[float]] = {}
    for candidate in candidates:
        bbox = _bbox_tuple(candidate.get("bbox"))
        side = str(candidate.get("side", ""))
        cx, cy = _bbox_center(bbox)
        side_positions.setdefault(side, []).append(cy if side in {"left", "right"} else cx)
    cost = 0.0
    for positions in side_positions.values():
        if len(positions) <= 2:
            continue
        values = sorted(positions)
        gaps = [right - left for left, right in zip(values, values[1:]) if right > left]
        if not gaps:
            continue
        mean = sum(gaps) / len(gaps)
        cost += sum(abs(gap - mean) for gap in gaps)
    return int(round(cost * 1_000_000))


def build_oa_layout_plan(
    shapes: Sequence[LayoutShape],
    placements: Sequence[Placement],
    *,
    lib: str,
    cell: str,
    view: str = "layout",
    pcell_lib: str = "pdk",
    layout_view: str = "layout",
    grid: DesignRuleDeck | PdkConfig | int | None = None,
    snap_to_grid: bool = True,
) -> OaWritePlan:
    rects = tuple(OaRect(shape.layer, "drawing", shape.bbox, shape.net) for shape in shapes)
    labels = tuple((shape.layer, shape.net, _bbox_center(shape.bbox)) for shape in shapes if shape.net)
    instances = tuple(
        OaInstance(
            name=p.name,
            lib=pcell_lib,
            cell=p.role or p.name,
            view=layout_view,
            xy=(p.x_um, p.y_um),
            orient=p.orient,
            connections={},
        )
        for p in placements
    )
    nets = tuple(dict.fromkeys(shape.net for shape in shapes if shape.net))
    pins = tuple(OaPin(net, net, "inputOutput", _first_layer_for_net(rects, net), _first_bbox_for_net(rects, net)) for net in nets)
    plan = OaWritePlan(OaCellView(lib, cell, view, "maskLayout"), nets=nets, pins=pins, instances=instances, rects=rects, labels=labels)
    return snap_oa_write_plan_to_grid(plan, grid) if grid is not None and snap_to_grid else plan


def build_oa_routing_plan(
    routes: Sequence[RoutedNet],
    *,
    lib: str,
    cell: str,
    view: str = "layout",
    default_width_um: float = 0.2,
    grid: DesignRuleDeck | PdkConfig | int | None = None,
    snap_to_grid: bool = True,
) -> OaWritePlan:
    nets = tuple(dict.fromkeys(route.net for route in routes if route.net))
    paths = tuple(
        OaPath(route.layer, "drawing", tuple((float(x), float(y)) for x, y in route.points), (route.width_nm * 1e-3 if route.width_nm else default_width_um), route.net)
        for route in routes
    )
    pins = tuple(OaPin(net, net, "inputOutput", _first_layer_for_path(paths, net), _path_pin_bbox(_first_path_for_net(paths, net), default_width_um)) for net in nets)
    plan = OaWritePlan(OaCellView(lib, cell, view, "maskLayout"), nets=nets, pins=pins, paths=paths)
    return snap_oa_write_plan_to_grid(plan, grid) if grid is not None and snap_to_grid else plan


def write_oa_skill(
    plan: OaWritePlan,
    path: str | Path,
    *,
    grid: DesignRuleDeck | PdkConfig | int | None = None,
    snap_to_grid: bool = True,
    validate_grid: bool = False,
    validate_lvs_stamping: bool = False,
    top_level_nets: Sequence[str] | None = None,
    require_lvs_labels: bool = False,
    pin_net_aliases: Mapping[str, str] | None = None,
    replace_cellview: bool = False,
    emit_pin_purpose_labels: bool = False,
    allow_label_only_top_level_nets: bool = False,
    rect_chunk_size: int = 0,
    rect_chunk_dir: str | Path | None = None,
    exit_after_write: bool = False,
) -> Path:
    path = Path(path)
    if grid is not None:
        if snap_to_grid:
            plan = snap_oa_write_plan_to_grid(plan, grid)
        if validate_grid:
            issues = validate_oa_write_plan_grid(plan, grid)
            if issues:
                raise ValueError("OA write plan has off-grid geometry: " + "; ".join(issues))
    elif validate_grid:
        raise ValueError("validate_grid requires a grid source")
    if validate_lvs_stamping:
        pdk = grid if isinstance(grid, PdkConfig) else None
        report = analyze_lvs_pin_label_stamping(
            plan,
            top_level_nets=top_level_nets,
            pdk=pdk,
            require_explicit_labels=require_lvs_labels,
            pin_net_aliases=pin_net_aliases,
            allow_label_only_top_level_nets=allow_label_only_top_level_nets,
        )
        if not report["passed"]:
            raise ValueError("OA write plan has LVS pin/label stamping issues: " + "; ".join(str(issue) for issue in report["issues"]))
    pdk_for_write = grid if isinstance(grid, PdkConfig) else None
    cv = plan.cellview
    lines = [
        f'unless(ddGetObj("{cv.lib}") ddCreateLib("{cv.lib}"))',
        f'cv = dbOpenCellViewByType("{cv.lib}" "{cv.cell}" "{cv.view}" "{cv.view_type}" "{cv.mode}")',
    ]
    lines.append('unless(cv error("failed to open cellview"))')
    if replace_cellview:
        lines.extend(_clear_cellview_skill_lines())
    for net in plan.nets:
        lines.append(f'unless(dbFindNetByName(cv "{net}") dbCreateNet(cv "{net}"))')
    for inst in plan.instances:
        if inst.instantiation_method == "drawn_primitive":
            continue
        x, y = inst.xy
        inst_id = _skill_id("inst_" + inst.name)
        if inst.instantiation_method == "dbCreateParamInst":
            param_types = _pcell_param_types_for_cell(inst.cell)
            param_list = _skill_param_inst_list(inst.params, param_types)
            lines.append(
                f'{inst_id} = dbCreateParamInst(cv dbOpenCellViewByType("{inst.lib}" "{inst.cell}" "{inst.view}" nil "r") '
                f'"{inst.name}" {x:g}:{y:g} "{inst.orient}" 1 {param_list})'
            )
        else:
            lines.append(f'{inst_id} = dbCreateInstByMasterName(cv "{inst.lib}" "{inst.cell}" "{inst.view}" "{inst.name}" list({x:g} {y:g}) "{inst.orient}")')
            for key, value in sorted(inst.params.items()):
                lines.append(f'dbReplaceProp({inst_id} "{key}" {_skill_prop_type(value)} {_skill_prop_value(value)})')
        for term, net in sorted(inst.connections.items()):
            if net:
                lines.append(
                    f'when({inst_id} '
                    f'masterTerm = dbFindTermByName({inst_id}->master "{term}") '
                    f'netObj = dbFindNetByName(cv "{net}") '
                    f'when(masterTerm && netObj dbCreateInstTerm(netObj {inst_id} masterTerm)))'
                )
    rect_chunk_paths: tuple[Path, ...] = ()
    if rect_chunk_size > 0 and len(plan.rects) > rect_chunk_size:
        chunk_dir = Path(rect_chunk_dir) if rect_chunk_dir is not None else path.parent / f"{path.stem}.rect_chunks"
        rect_chunk_paths = _write_oa_rect_chunk_skill_files(
            tuple(plan.rects),
            chunk_dir=chunk_dir,
            chunk_size=int(rect_chunk_size),
            pdk_for_write=pdk_for_write,
        )
    if rect_chunk_paths:
        for chunk_path in rect_chunk_paths:
            lines.append(f'load("{_skill_path_literal(chunk_path)}")')
    else:
        for rect in plan.rects:
            lines.extend(_oa_rect_skill_lines(rect, pdk_for_write))
    for path_obj in plan.paths:
        point_text = " ".join(f'{x:g}:{y:g}' for x, y in path_obj.points)
        path_id = _skill_id("path_" + path_obj.layer + "_" + path_obj.net)
        lines.append(f'{path_id} = dbCreatePath(cv list("{path_obj.layer}" "{path_obj.purpose}") list({point_text}) {path_obj.width:g})')
        lines.extend(_shape_color_skill_lines(path_id, path_obj.layer, path_obj.purpose, pdk_for_write, explicit_color=path_obj.color))
        if path_obj.net:
            lines.append(_attach_fig_to_net_skill(path_id, path_obj.net))
    for via in plan.vias:
        x, y = via.xy
        via_id = _skill_id(f"via_{via.via_def}_{via.net}_{x:g}_{y:g}")
        if _native_contact_via_is_instance(via.via_def, pdk_for_write):
            lines.append(f'{via_id} = dbCreateInstByMasterName(cv "{pdk_for_write.pcell_template_for("nmos").lib_name}" "{via.via_def}" "layout" "{via_id}" list({x:g} {y:g}) "R0")')
        elif _emit_native_via_geometry(via.via_def, pdk_for_write) and not bool(dict(getattr(via, "metadata", {}) or {}).get("force_oa_via", False)):
            for idx, skill_line in enumerate(_skill_native_via_geometry_lines(via_id, via, pdk_for_write)):
                lines.append(skill_line)
        else:
            via_def_id = _skill_id(f"viaDef_{via.via_def}_{x:g}_{y:g}")
            lines.append(f"{via_id} = nil")
            lines.append(f'{via_def_id} = techFindViaDefByName(techGetTechFile(cv) "{via.via_def}")')
            lines.append(f'when({via_def_id} {via_id} = dbCreateVia(cv {via_def_id} list({x:g} {y:g}) "R0" list({via.rows} {via.cols})))')
            if via.net:
                lines.append(f'when({via_id} netObj = dbFindNetByName(cv "{via.net}") when(netObj {via_id}~>net = netObj dbSetViaNet(cv netObj {via_id})))')
    explicit_label_nets = {text for _layer, text, _xy in plan.labels if text}
    top_level_label_nets = tuple(dict.fromkeys(str(net) for net in top_level_nets or () if str(net)))
    for pin in plan.pins:
        lines.append(f'unless(dbFindNetByName(cv "{pin.net}") dbCreateNet(cv "{pin.net}"))')
        if pin.bbox is not None:
            x0, y0, x1, y1 = pin.bbox
            if pin.emit_draw_rect:
                lines.append(f'pinDrawFig = dbCreateRect(cv list("{pin.layer}" "drawing") list({x0:g}:{y0:g} {x1:g}:{y1:g}))')
                lines.extend(_shape_color_skill_lines("pinDrawFig", pin.layer, "drawing", pdk_for_write))
                lines.append(_attach_fig_to_net_skill("pinDrawFig", pin.net))
            lines.append(f'pinFig = dbCreateRect(cv list("{pin.layer}" "pin") list({x0:g}:{y0:g} {x1:g}:{y1:g}))')
            lines.append(_attach_fig_to_net_skill("pinFig", pin.net))
            lines.append(f'pinObj = dbCreatePin(dbFindNetByName(cv "{pin.net}") pinFig "{pin.name}")')
            lines.append(f'when(pinObj && pinObj~>term pinObj~>term~>direction = "{pin.direction}")')
            cx, cy = _bbox_center(pin.bbox)
            if emit_pin_purpose_labels:
                lines.append(f'dbCreateLabel(cv list("{pin.layer}" "pin") list({cx:g} {cy:g}) "{pin.name}" "centerCenter" "R0" "stick" 0.1)')
            if pin.name not in explicit_label_nets:
                lines.append(f'dbCreateLabel(cv list("{pin.layer}" "text") list({cx:g} {cy:g}) "{pin.name}" "centerCenter" "R0" "stick" 0.1)')
        lines.append(f'; term {pin.name} direction={pin.direction} net={pin.net}')
    if allow_label_only_top_level_nets and top_level_nets:
        created_pin_nets = {str(pin.net) for pin in plan.pins if pin.net}
        for net_name in tuple(dict.fromkeys(str(net) for net in top_level_nets if str(net))):
            if net_name in created_pin_nets or net_name not in explicit_label_nets:
                continue
            lines.append(
                f'when(netObj = dbFindNetByName(cv "{net_name}") '
                f'termObj = dbCreateTerm(netObj "{net_name}" "inputOutput"))'
            )
    for layer, text, xy in plan.labels:
        x, y = xy
        # Use the "text" purpose so that strmout maps the label to the GDS
        # text layer that the Calibre LVS deck attaches to the corresponding
        # drawing layer.
        label_layer = "text" if allow_label_only_top_level_nets and text in top_level_label_nets else layer
        label_purpose = "drawing" if label_layer == "text" else "text"
        lines.append(
            f'dbCreateLabel(cv list("{label_layer}" "{label_purpose}") list({x:g} {y:g}) "{text}" "centerCenter" "R0" "stick" 0.1)'
        )
    lines.append('dbSave(cv)')
    lines.append('dbClose(cv)')
    if exit_after_write:
        lines.append('exit()')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_oa_rect_chunk_skill_files(
    rects: Sequence[OaRect],
    *,
    chunk_dir: Path,
    chunk_size: int,
    pdk_for_write: PdkConfig | None,
) -> tuple[Path, ...]:
    chunk_dir.mkdir(parents=True, exist_ok=True)
    size = max(int(chunk_size), 1)
    paths: list[Path] = []
    for chunk_index, start in enumerate(range(0, len(rects), size)):
        chunk_path = chunk_dir / f"rect_chunk_{chunk_index:04d}.il"
        lines = [f"; OA rectangle chunk {chunk_index} generated by analogskills.eda.oa.write_oa_skill"]
        for rect in rects[start : start + size]:
            lines.extend(_oa_rect_skill_lines(rect, pdk_for_write))
        chunk_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append(chunk_path)
    return tuple(paths)


def _oa_rect_skill_lines(rect: OaRect, pdk_for_write: PdkConfig | None) -> tuple[str, ...]:
    x0, y0, x1, y1 = rect.bbox
    rect_id = _skill_id("rect_" + rect.layer + "_" + rect.net)
    lines = [f'{rect_id} = dbCreateRect(cv list("{rect.layer}" "{rect.purpose}") list({x0:g}:{y0:g} {x1:g}:{y1:g}))']
    lines.extend(_shape_color_skill_lines(rect_id, rect.layer, rect.purpose, pdk_for_write, explicit_color=rect.color))
    if rect.net:
        lines.append(_attach_fig_to_net_skill(rect_id, rect.net))
    return tuple(lines)


def _skill_path_literal(path: str | Path) -> str:
    return str(path).replace("\\", "/").replace('"', '\\"')


def write_oa_replacement_skill(
    plan: OaWritePlan,
    path: str | Path,
    *,
    grid: DesignRuleDeck | PdkConfig | int | None = None,
    validate_grid: bool = False,
    validate_lvs_stamping: bool = False,
    top_level_nets: Sequence[str] | None = None,
    require_lvs_labels: bool = False,
    emit_pin_purpose_labels: bool = False,
    allow_label_only_top_level_nets: bool = False,
) -> Path:
    """Emit SKILL that clears the target cellview before rewriting a full plan."""

    return write_oa_skill(
        plan,
        path,
        grid=grid,
        validate_grid=validate_grid,
        validate_lvs_stamping=validate_lvs_stamping,
        top_level_nets=top_level_nets,
        require_lvs_labels=require_lvs_labels,
        replace_cellview=True,
        emit_pin_purpose_labels=emit_pin_purpose_labels,
        allow_label_only_top_level_nets=allow_label_only_top_level_nets,
    )


def _clear_cellview_skill_lines() -> tuple[str, ...]:
    return (
        "; replace_cellview: clear previous generated layout content before rewrite",
        "foreach(i cv~>instances dbDeleteObject(i))",
        "foreach(s cv~>shapes dbDeleteObject(s))",
        "foreach(l cv~>labels dbDeleteObject(l))",
        "foreach(p cv~>pins dbDeleteObject(p))",
        "foreach(t cv~>terminals dbDeleteObject(t))",
    )


def plan_to_dict(plan: OaWritePlan) -> dict[str, object]:
    return {
        "cellview": plan.cellview.__dict__,
        "nets": list(plan.nets),
        "pins": [pin.__dict__ for pin in plan.pins],
        "instances": [inst.__dict__ for inst in plan.instances],
        "rects": [rect.__dict__ for rect in plan.rects],
        "labels": list(plan.labels),
        "paths": [path.__dict__ for path in plan.paths],
        "vias": [via.__dict__ for via in plan.vias],
    }


def plan_from_dict(data: Mapping[str, object]) -> OaWritePlan:
    cv_data = dict(data["cellview"])
    return OaWritePlan(
        OaCellView(**cv_data),
        nets=tuple(str(net) for net in data.get("nets", ())),
        pins=tuple(OaPin(**_coerce_pin(pin)) for pin in data.get("pins", ())),
        instances=tuple(OaInstance(**_coerce_instance(inst)) for inst in data.get("instances", ())),
        rects=tuple(OaRect(**_coerce_rect(rect)) for rect in data.get("rects", ())),
        labels=tuple((str(layer), str(text), tuple(xy)) for layer, text, xy in data.get("labels", ())),
        paths=tuple(OaPath(**_coerce_path(path)) for path in data.get("paths", ())),
        vias=tuple(OaVia(**_coerce_via(via)) for via in data.get("vias", ())),
    )


def save_oa_plan_json(plan: OaWritePlan, path: str | Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(plan_to_dict(plan), indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_oa_plan_json(path: str | Path) -> OaWritePlan:
    return plan_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _snap_pin_to_grid(pin: OaPin, rules: DesignRuleDeck, *, bbox_mode: str) -> OaPin:
    bbox = None if pin.bbox is None else rules.snap_bbox_um(pin.bbox, mode=bbox_mode)
    return replace(pin, bbox=bbox)


def _snap_rect_to_grid(rect: OaRect, rules: DesignRuleDeck, *, bbox_mode: str) -> OaRect:
    metadata = dict(getattr(rect, "metadata", {}) or {})
    if str(metadata.get("snap_mode", "") or "") == "exact_size_on_grid":
        width = _positive_metadata_float(metadata.get("exact_width_um"))
        height = _positive_metadata_float(metadata.get("exact_height_um"))
        if width is not None and height is not None:
            snapped_width = rules.snap_dimension_um(width)
            snapped_height = rules.snap_dimension_um(height)
            x0, y0, x1, y1 = (float(value) for value in rect.bbox)
            cx = 0.5 * (x0 + x1)
            cy = 0.5 * (y0 + y1)
            lower_x = rules.snap_um(cx - 0.5 * snapped_width)
            lower_y = rules.snap_um(cy - 0.5 * snapped_height)
            upper_x = rules.snap_um(lower_x + snapped_width)
            upper_y = rules.snap_um(lower_y + snapped_height)
            return replace(rect, bbox=(lower_x, lower_y, upper_x, upper_y))
    return replace(rect, bbox=rules.snap_bbox_um(rect.bbox, mode=bbox_mode))


def _positive_metadata_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0.0 else None


def _snap_path_to_grid(path_obj: OaPath, rules: DesignRuleDeck) -> OaPath:
    width = rules.snap_dimension_um(path_obj.width)
    points = tuple(rules.snap_point_um(point) for point in path_obj.points)
    return replace(path_obj, points=points, width=width)


def _grid_rules(grid: DesignRuleDeck | PdkConfig | int) -> DesignRuleDeck:
    if isinstance(grid, PdkConfig):
        return grid.rules
    if isinstance(grid, DesignRuleDeck):
        return grid
    if isinstance(grid, int):
        return DesignRuleDeck(grid_nm=grid)
    raise TypeError(f"unsupported grid source {type(grid)!r}")


def _point_grid_issues(prefix: str, point: tuple[float, float], rules: DesignRuleDeck, *, tol_um: float) -> list[str]:
    labels = ("x", "y")
    return [
        f"{prefix}.{label}={value:g}um is off-grid for {rules.grid_nm}nm grid"
        for label, value in zip(labels, point)
        if not rules.is_on_grid_um(value, tol_um=tol_um)
    ]


def _bbox_grid_issues(prefix: str, bbox: tuple[float, float, float, float], rules: DesignRuleDeck, *, tol_um: float) -> list[str]:
    labels = ("x0", "y0", "x1", "y1")
    return [
        f"{prefix}.{label}={value:g}um is off-grid for {rules.grid_nm}nm grid"
        for label, value in zip(labels, bbox)
        if not rules.is_on_grid_um(value, tol_um=tol_um)
    ]


def _pin_direction(role: NetRole) -> str:
    if role == NetRole.INPUT:
        return "input"
    if role == NetRole.OUTPUT:
        return "output"
    if role in {NetRole.SUPPLY, NetRole.GROUND}:
        return "inputOutput"
    return "inputOutput"


def _schematic_params_for_device(
    name: str,
    model: str,
    base_params: dict[str, object],
    sizing: Mapping[str, Mapping[str, object]],
    pdk: PdkConfig | None,
) -> dict[str, object]:
    params = dict(base_params)
    params.update(dict(sizing.get(name, {})))
    params = _normalize_sizing_params(params)
    if pdk is None:
        return params
    try:
        template = pdk.pcell_template_for(_logical_pcell_name(model, ""))
    except KeyError:
        return params
    return template.map_parameters(params, schematic=True)


def _normalize_sizing_params(params: Mapping[str, object]) -> dict[str, object]:
    result = dict(params)
    for key in ("W", "w", "L", "l", "width", "length"):
        if key in result and isinstance(result[key], (float, int)):
            result[key] = _coerce_dimension_m(float(result[key]))
    for key in ("W", "L"):
        um_key = f"{key}_um"
        nm_key = f"{key}_nm"
        if um_key in params:
            result[key] = float(params[um_key]) * 1e-6
        if nm_key in params:
            result[key] = float(params[nm_key]) * 1e-9
    return result


def _coerce_dimension_m(value: float) -> float:
    return coerce_dimension_m(value)


def _logical_pcell_name(model: str, role: str) -> str:
    lowered = model.lower()
    if "pmos" in lowered or lowered.startswith("pch") or lowered.startswith("p_"):
        return "pmos"
    if "nmos" in lowered or lowered.startswith("nch") or lowered.startswith("n_"):
        return "nmos"
    if "npn" in lowered or "pnp" in lowered or "bjt" in lowered:
        return "bjt"
    if "res" in lowered:
        return "resistor"
    if "cap" in lowered:
        return "capacitor"
    return role or model


def _bbox_center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x0, y0, x1, y1 = bbox
    return ((x0 + x1) / 2, (y0 + y1) / 2)


def _pin_stamping_issues(plan: object, pin: OaPin) -> list[str]:
    if pin.bbox is None:
        return [f"pin {pin.name} on net {pin.net} has no bbox"]
    hits = _net_geometry_hits_in_bbox(plan, pin.layer, pin.bbox)
    if pin.net not in hits:
        return [f"pin {pin.name} bbox does not overlap drawing geometry for net {pin.net} on {pin.layer}"]
    other_hits = tuple(net for net in hits if net != pin.net)
    if other_hits:
        return [f"pin {pin.name} bbox on {pin.layer} also overlaps other nets {other_hits}"]
    return []


def _label_stamping_issues(plan: object, layer: str, text: str, xy: tuple[float, float]) -> list[str]:
    hits = _net_geometry_hits_at_point(plan, layer, xy)
    if text not in hits:
        return [f"label {text} at {xy} on {layer} is not on drawing geometry for net {text}"]
    other_hits = tuple(net for net in hits if net != text)
    if other_hits:
        return [f"label {text} at {xy} on {layer} also overlaps other nets {other_hits}"]
    return []


def _bbox_has_net_geometry(plan: object, layer: str, net: str, bbox: tuple[float, float, float, float]) -> bool:
    for shape_layer, shape_net, shape_bbox in _drawing_bboxes(plan):
        if shape_layer == layer and shape_net == net and _bbox_overlaps(bbox, shape_bbox):
            return True
    return False


def _net_geometry_hits_in_bbox(plan: object, layer: str, bbox: tuple[float, float, float, float]) -> tuple[str, ...]:
    hits: list[str] = []
    for shape_layer, shape_net, shape_bbox in _drawing_bboxes(plan):
        if shape_layer == layer and shape_net and _bbox_overlaps(bbox, shape_bbox):
            hits.append(shape_net)
    return tuple(dict.fromkeys(hits))


def _net_geometry_hits_at_point(plan: object, layer: str, xy: tuple[float, float]) -> tuple[str, ...]:
    hits: list[str] = []
    for shape_layer, shape_net, shape_bbox in _drawing_bboxes(plan):
        if shape_layer == layer and shape_net and _point_in_bbox(xy, shape_bbox):
            hits.append(shape_net)
    return tuple(dict.fromkeys(hits))


def _drawing_bboxes(plan: object) -> tuple[tuple[str, str, tuple[float, float, float, float]], ...]:
    bboxes: list[tuple[str, str, tuple[float, float, float, float]]] = []
    bboxes.extend((str(getattr(rect, "layer", "")), str(getattr(rect, "net", "")), _bbox_tuple(getattr(rect, "bbox"))) for rect in getattr(plan, "rects", ()) if str(getattr(rect, "net", "")))
    for path_obj in getattr(plan, "paths", ()):
        if not str(getattr(path_obj, "net", "")):
            continue
        bboxes.extend((str(getattr(path_obj, "layer", "")), str(getattr(path_obj, "net", "")), bbox) for bbox in _path_segment_bboxes(path_obj))
    return tuple(bboxes)


def _plan_pins(plan: object) -> tuple[OaPin, ...]:
    return tuple(
        OaPin(
            str(getattr(pin, "name", "")),
            str(getattr(pin, "net", "")),
            str(getattr(pin, "direction", "inputOutput")),
            str(getattr(pin, "layer", "M1")),
            None if getattr(pin, "bbox", None) is None else _bbox_tuple(getattr(pin, "bbox")),
        )
        for pin in getattr(plan, "pins", ())
    )


def _plan_labels(plan: object) -> tuple[tuple[str, str, tuple[float, float]], ...]:
    labels: list[tuple[str, str, tuple[float, float]]] = []
    for label in getattr(plan, "labels", ()):
        if isinstance(label, (tuple, list)) and len(label) == 3:
            layer, text, xy = label
        else:
            layer = getattr(label, "layer", "")
            text = getattr(label, "text", "")
            xy = getattr(label, "xy", (0.0, 0.0))
        labels.append((str(layer), str(text), _point_tuple(xy)))
    return tuple(labels)


def _path_segment_bboxes(path_obj: object) -> tuple[tuple[float, float, float, float], ...]:
    points = tuple(_point_tuple(point) for point in getattr(path_obj, "points", ()))
    width = float(getattr(path_obj, "width", 0.0) or 0.0)
    if len(points) < 2:
        bbox = _path_pin_bbox(path_obj, width)
        return () if bbox is None else (bbox,)
    half = width / 2.0
    bboxes = []
    for start, end in zip(points, points[1:]):
        x0, y0 = start
        x1, y1 = end
        bboxes.append((min(x0, x1) - half, min(y0, y1) - half, max(x0, x1) + half, max(y0, y1) + half))
    return tuple(bboxes)


def _bbox_overlaps(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    return left[0] < right[2] and right[0] < left[2] and left[1] < right[3] and right[1] < left[3]


def _point_in_bbox(point: tuple[float, float], bbox: tuple[float, float, float, float]) -> bool:
    x, y = point
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def _first_layer_for_net(rects: Sequence[OaRect], net: str, *, preferred_layers: Sequence[str] = ()) -> str:
    preferred = tuple(dict.fromkeys(str(layer) for layer in preferred_layers if str(layer)))
    for layer in preferred:
        for rect in rects:
            if rect.net == net and rect.layer == layer:
                return rect.layer
    for rect in rects:
        if rect.net == net:
            return rect.layer
    return "M1"


def _first_bbox_for_net(
    rects: Sequence[OaRect],
    net: str,
    *,
    preferred_layers: Sequence[str] = (),
) -> tuple[float, float, float, float] | None:
    preferred = tuple(dict.fromkeys(str(layer) for layer in preferred_layers if str(layer)))
    for layer in preferred:
        for rect in rects:
            if rect.net == net and rect.layer == layer:
                return rect.bbox
    for rect in rects:
        if rect.net == net:
            return rect.bbox
    return None


def _select_lvs_pin_geometry(
    plan: object,
    pin_name: str,
    net: str,
    pdk: PdkConfig | None,
    preferred_layers: Sequence[str],
    default_layer: str,
    *,
    pin_selection_policy: str = "safe_first",
) -> tuple[str, tuple[float, float, float, float]] | None:
    first_candidate: tuple[str, tuple[float, float, float, float]] | None = None
    candidates = _iter_lvs_pin_geometry_candidates(
        plan,
        net,
        pdk,
        preferred_layers,
        contained_pin_bboxes=str(pin_selection_policy).lower() in {"boundary_aware", "aesthetic_boundary"},
    )
    safe_candidates: list[tuple[int, str, tuple[float, float, float, float]]] = []
    for index, (layer, bbox) in enumerate(candidates):
        if first_candidate is None:
            first_candidate = (layer, bbox)
        candidate = OaPin(pin_name, net, "inputOutput", layer or default_layer, bbox)
        if not _pin_stamping_issues(plan, candidate):
            if str(pin_selection_policy).lower() in {"boundary_aware", "aesthetic_boundary"}:
                safe_candidates.append((index, layer or default_layer, bbox))
                continue
            return (layer or default_layer, bbox)
    if safe_candidates:
        plan_bbox = _drawing_bbox(plan)
        best = min(
            safe_candidates,
            key=lambda row: _lvs_pin_boundary_selection_cost(
                row[2],
                plan_bbox,
                index=row[0],
            ),
        )
        return (best[1], best[2])
    return first_candidate


def _iter_lvs_pin_geometry_candidates(
    plan: object,
    net: str,
    pdk: PdkConfig | None,
    preferred_layers: Sequence[str],
    *,
    contained_pin_bboxes: bool = False,
) -> tuple[tuple[str, tuple[float, float, float, float]], ...]:
    paths = tuple(getattr(plan, "paths", ()) or ())
    rects = tuple(getattr(plan, "rects", ()) or ())
    allowed_layers = _allowed_lvs_pin_layers(pdk)
    ordered_layers = tuple(
        layer
        for layer in dict.fromkeys(
            (
                *tuple(str(layer) for layer in preferred_layers if str(layer)),
                *tuple(str(getattr(path_obj, "layer", "")) for path_obj in paths if str(getattr(path_obj, "net", "")) == net),
                *tuple(str(getattr(rect, "layer", "")) for rect in rects if str(getattr(rect, "net", "")) == net),
            )
        )
        if _lvs_pin_layer_allowed(str(layer), allowed_layers)
    )
    candidates: list[tuple[str, tuple[float, float, float, float]]] = []
    seen: set[tuple[str, tuple[int, int, int, int]]] = set()

    def add(layer: str, bbox: tuple[float, float, float, float] | None) -> None:
        if bbox is None:
            return
        key = (str(layer), tuple(int(round(float(v) * 1_000_000)) for v in bbox))
        if key in seen:
            return
        seen.add(key)
        candidates.append((str(layer), bbox))

    for layer in ordered_layers:
        for path_obj in paths:
            if str(getattr(path_obj, "net", "")) != net or str(getattr(path_obj, "layer", "")) != layer:
                continue
            for point in _path_pin_candidate_points(path_obj):
                add(layer, _point_pin_bbox(point, max(float(getattr(path_obj, "width", 0.0) or 0.0), _min_pin_width_um(layer, pdk))))
            if contained_pin_bboxes:
                for bbox in _contained_path_pin_bboxes(path_obj, layer, pdk):
                    add(layer, bbox)
        for rect in rects:
            if str(getattr(rect, "net", "")) == net and str(getattr(rect, "layer", "")) == layer:
                rect_bbox = _bbox_tuple(getattr(rect, "bbox"))
                add(layer, rect_bbox)
                if contained_pin_bboxes:
                    for bbox in _contained_rect_pin_bboxes(rect_bbox, layer, pdk):
                        add(layer, bbox)
    return tuple(candidates)


def _allowed_lvs_pin_layers(pdk: PdkConfig | None) -> frozenset[str] | None:
    if pdk is None:
        return None
    return frozenset(str(layer) for layer in tuple(pdk.layer_map.metals or ()) if str(layer))


def _lvs_pin_layer_allowed(layer: str, allowed_layers: frozenset[str] | None) -> bool:
    if allowed_layers is None:
        return bool(str(layer))
    return str(layer) in allowed_layers


def _path_pin_candidate_points(path_obj: object) -> tuple[tuple[float, float], ...]:
    points = tuple(_point_tuple(point) for point in tuple(getattr(path_obj, "points", ()) or ()))
    if not points:
        return ()
    candidates: list[tuple[float, float]] = [points[0], points[-1]]
    for left, right in zip(points, points[1:]):
        candidates.append(((left[0] + right[0]) / 2.0, (left[1] + right[1]) / 2.0))
    return tuple(dict.fromkeys(candidates))


def _point_pin_bbox(point: tuple[float, float], width: float) -> tuple[float, float, float, float]:
    x, y = point
    half = max(float(width), 1e-6) / 2.0
    return (x - half, y - half, x + half, y + half)


def _contained_rect_pin_bboxes(
    bbox: tuple[float, float, float, float],
    layer: str,
    pdk: PdkConfig | None,
) -> tuple[tuple[float, float, float, float], ...]:
    x0, y0, x1, y1 = bbox
    width = max(0.0, x1 - x0)
    height = max(0.0, y1 - y0)
    if width <= 0.0 or height <= 0.0:
        return ()
    size = min(max(_min_pin_width_um(layer, pdk), 1e-6), width, height)
    half = size / 2.0
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    points = (
        (x0 + half, cy),
        (x1 - half, cy),
        (cx, y0 + half),
        (cx, y1 - half),
        (x0 + half, y0 + half),
        (x0 + half, y1 - half),
        (x1 - half, y0 + half),
        (x1 - half, y1 - half),
        (cx, cy),
    )
    return tuple(dict.fromkeys(_contained_point_pin_bbox(point, size, bbox) for point in points))


def _contained_path_pin_bboxes(
    path_obj: object,
    layer: str,
    pdk: PdkConfig | None,
) -> tuple[tuple[float, float, float, float], ...]:
    width = max(float(getattr(path_obj, "width", 0.0) or 0.0), _min_pin_width_um(layer, pdk))
    bboxes: list[tuple[float, float, float, float]] = []
    for segment_bbox in _path_segment_bboxes(path_obj):
        sx0, sy0, sx1, sy1 = segment_bbox
        if sx1 <= sx0 or sy1 <= sy0:
            continue
        size = min(width, sx1 - sx0, sy1 - sy0)
        if size <= 0.0:
            continue
        half = size / 2.0
        cx = (sx0 + sx1) / 2.0
        cy = (sy0 + sy1) / 2.0
        points = (
            (sx0 + half, cy),
            (sx1 - half, cy),
            (cx, sy0 + half),
            (cx, sy1 - half),
            (cx, cy),
        )
        bboxes.extend(_contained_point_pin_bbox(point, size, segment_bbox) for point in points)
    return tuple(dict.fromkeys(bboxes))


def _contained_point_pin_bbox(
    point: tuple[float, float],
    size: float,
    container_bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = container_bbox
    side = max(float(size), 1e-6)
    half = side / 2.0
    if x1 - x0 < side:
        side = max(x1 - x0, 1e-6)
        half = side / 2.0
    if y1 - y0 < side:
        side = max(min(side, y1 - y0), 1e-6)
        half = side / 2.0
    x = min(max(float(point[0]), x0 + half), x1 - half)
    y = min(max(float(point[1]), y0 + half), y1 - half)
    return (x - half, y - half, x + half, y + half)


def _drawing_bbox(plan: object) -> tuple[float, float, float, float] | None:
    boxes = tuple(bbox for _layer, _net, bbox in _drawing_bboxes(plan))
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _nearest_bbox_side(
    bbox: tuple[float, float, float, float],
    plan_bbox: tuple[float, float, float, float] | None,
) -> str:
    if plan_bbox is None:
        return ""
    cx, cy = _bbox_center(bbox)
    px0, py0, px1, py1 = plan_bbox
    distances = {
        "left": abs(cx - px0),
        "right": abs(cx - px1),
        "bottom": abs(cy - py0),
        "top": abs(cy - py1),
    }
    return min(distances, key=distances.get)


def _lvs_pin_boundary_selection_cost(
    bbox: tuple[float, float, float, float],
    plan_bbox: tuple[float, float, float, float] | None,
    *,
    index: int,
) -> tuple[int, int, int]:
    if plan_bbox is None:
        return (0, int(round(_bbox_area_um2(bbox) * 1_000_000)), int(index))
    cx, cy = _bbox_center(bbox)
    px0, py0, px1, py1 = plan_bbox
    boundary_distance = min(
        abs(cx - px0),
        abs(cx - px1),
        abs(cy - py0),
        abs(cy - py1),
    )
    area = _bbox_area_um2(bbox)
    return (
        int(round(boundary_distance * 1_000_000)),
        int(round(area * 1_000_000)),
        int(index),
    )


def _bbox_area_um2(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))


def _min_pin_width_um(layer: str, pdk: PdkConfig | None) -> float:
    if pdk is None:
        return 0.2
    try:
        return pdk.rules.min_width_um(layer)
    except KeyError:
        return 0.2


def _skill_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value)


def _skill_prop_type(value: object) -> str:
    if isinstance(value, bool):
        return "'boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "'int"
    if isinstance(value, float):
        return "'float"
    return "'string"


def _skill_prop_value(value: object) -> str:
    if isinstance(value, bool):
        return "t" if value else "nil"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _pcell_param_types_for_cell(cell_name: str) -> dict[str, str]:
    """Return SKILL parameter types for known TSMC PCell CDFs."""
    if cell_name in {"nch_mac", "pch_mac", "nch_svt_mac", "pch_svt_mac"}:
        # These layout-only options are CDF strings, including values that
        # look numeric (for example ``100n``).  They must be emitted with the
        # same types as introspection requests or a native OA instantiation
        # will ignore a calibrated template override.
        return {
            "Wfg": "string", "fingers": "string", "l": "string", "simM": "string",
            "DFM_display": "string", "DFM_options": "string",
            "DUpper_PO_EX_INC": "string", "DLower_PO_EX_INC": "string",
            "LdiffExt": "string", "RdiffExt": "string",
            "PO_EX_INC": "string", "pMetalOption": "string",
            "pMetalEncNS": "string", "pMetalEncEW": "string",
            "dummyPolyLayer": "string", "leftDummyPoly": "string",
            "rightDummyPoly": "string", "secondLeftDummy": "string",
            "secondRightDummy": "string", "secondDummyPolySpacing": "string",
            "dummyPolyWidth": "string", "dummyPolyWidth2": "string",
            "secondDummyPolyWidth": "string", "firstDummyPolySpacing": "string",
            "dummyPolyNumLeft": "string", "dummyPolyNumRight": "string",
            "DPO_CO_EN_INC": "string", "DM1_CO_EN_INC": "string",
            "DM1_CO_EN_INCX": "string", "DCO_CO_SP_INC": "string",
            "DGA_CO_SP_INC": "string", "DGA_GA_SP_INC": "string",
            "LGA_CO_SP_INC": "string", "RGA_CO_SP_INC": "string",
            "CO_EN_1_1_INC": "string", "gateToContactExtension": "string",
            "routePolydir": "string", "polyContactsEnh": "string",
            "polyContactNumTop": "string", "polyContactNumBot": "string",
            "routeUPoly_SP_INC": "string", "routeDPoly_SP_INC": "string",
            "MatchDpoWithGate": "string", "Poly_HardCons": "string",
            "STIdummyGate": "string", "rPD_Ext": "string",
            "rPD_Ext_adj": "string", "rPD_Ext_adj2": "string",
            "dummyPolyInc": "string", "SDISDEnc_inc": "string",
        }
    if cell_name in {"nch_svt_macx", "pch_svt_macx"}:
        return {"fingers": "string", "nfin": "string", "l": "string", "simM": "string"}
    if cell_name == "rnod":
        # CRN28 passive dimensions are true CDF PCell parameters.  They must be
        # passed when creating the parameterized instance; replacing properties
        # after dbCreateInstByMasterName leaves the default subMaster geometry.
        return {
            "model": "string",
            "macro": "string",
            "ResCalc": "string",
            "connection": "string",
            "w": "string",
            "sumW": "string",
            "l": "string",
            "sumL": "string",
            "res": "string",
            "m": "int",
            "mf": "int",
            "multi": "int",
            "segments": "int",
            "srs": "int",
            "prl": "int",
        }
    if cell_name == "nmoscap":
        return {
            "model": "string",
            "macro": "string",
            "wr": "string",
            "lr": "string",
            "c": "string",
            "cmax": "string",
            "cmin": "string",
            "volt": "string",
            "m": "int",
            "multi": "int",
        }
    if cell_name in {"npn", "pnp"}:
        return {"model": "string", "macro": "string", "Esize": "string", "area": "string", "l": "string", "w": "string", "m": "int", "multi": "int"}
    return {}


def _skill_param_inst_list(params: Mapping[str, object], param_types: Mapping[str, str] | None = None) -> str:
    param_types = param_types or {}
    entries = []
    for key, value in sorted(params.items()):
        param_type = param_types.get(key)
        if param_type == "string" and not isinstance(value, str):
            escaped = _skill_string_param_value(key, value).replace('\\', '\\\\').replace('"', '\\"')
            skill_value = f'"{escaped}"'
        else:
            param_type = param_type or _skill_param_type(value)
            skill_value = _skill_prop_value(value)
        entries.append(f'list("{key}" "{param_type}" {skill_value})')
    return "list(" + " ".join(entries) + ")"


def _skill_string_param_value(key: str, value: object) -> str:
    """Return a CDF-friendly string for dbCreateParamInst string parameters."""

    lowered = str(key).lower()
    if lowered in {"wfg", "l", "w", "lr", "wr", "sumw", "suml", "ldiffext", "rdiffext", "pmetalencns", "pmetalencew"}:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return str(value)
        if numeric > 0.0:
            nm = numeric * 1e9
            if 0.001 <= nm < 1_000_000:
                return f"{nm:.12g}n"
            um = numeric * 1e6
            if 0.001 <= um < 1_000_000:
                return f"{um:.12g}u"
    return str(value)


def _skill_param_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    return "string"


def _attach_fig_to_net_skill(fig_id: str, net: str) -> str:
    return f'when({fig_id} netObj = dbFindNetByName(cv "{net}") when(netObj {fig_id}~>net = netObj))'


def _shape_color_skill_lines(
    fig_id: str,
    layer: str,
    purpose: str,
    pdk: PdkConfig | None,
    *,
    explicit_color: str = "",
) -> tuple[str, ...]:
    if pdk is None or pdk.name != "tsmcn7" or purpose != "drawing":
        return ()
    if layer not in {"M0", "M1", "M2"}:
        return ()
    # TSMC N7 streamout maps uncolored drawing metals to *_META datatypes
    # (for example M0 180/250, M2 32/250). Those shapes fail signoff checks
    # such as M0.R.30.T / M2.R.30.1.T because they never land on the required
    # color-locked main masks. Native helper PCells already emit the required
    # companion layers, so for top-level routed metals we must force a real
    # mask color before streamout. A direct M2:drawingy/drawingz workaround is
    # not viable here: the shipped N7 layermap does not define those purposes
    # for M2, and XStream ignores them.
    color_name = str(explicit_color or "mask1Color")
    return (
        f'when({fig_id} errset(dbSetShapeColor({fig_id} "{color_name}") t))',
        f'when({fig_id} errset(dbSetShapeColorLocked({fig_id} t) t))',
    )


def _native_contact_via_is_instance(via_def: str, pdk: PdkConfig | None) -> bool:
    if pdk is None or pdk.name != "tsmcn7":
        return False
    return False


def _emit_native_via_geometry(via_def: str, pdk: PdkConfig | None) -> bool:
    if pdk is None:
        return False
    configured = _configured_native_via_geometry_defs(pdk)
    if via_def in configured:
        return True
    if pdk.name != "tsmcn7":
        return False
    return via_def in {"M0_PO", "M0_PO_VD", "M0_SUB", "M0_NW", "VIA0", "VIA1", "VIA2"}


def _configured_native_via_geometry_defs(pdk: PdkConfig) -> set[str]:
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), dict) else {}
    oa_metadata = metadata.get("oa", {}) if isinstance(metadata.get("oa", {}), dict) else {}
    raw = oa_metadata.get("emit_native_via_geometry", ())
    if isinstance(raw, str):
        return {raw}
    try:
        return {str(value) for value in tuple(raw or ()) if str(value)}
    except TypeError:
        return set()


def _skill_native_via_geometry_lines(via_id: str, via: OaVia, pdk: PdkConfig | None) -> tuple[str, ...]:
    if pdk is None:
        return ()
    from analogskills.layout.physical import via_landing_bboxes

    lines: list[str] = []
    cut_bboxes = _native_via_cut_bboxes(via, pdk)
    for cut_index, cut_bbox in enumerate(cut_bboxes):
        x0, y0, x1, y1 = cut_bbox
        cut_id = via_id + "_cut" if len(cut_bboxes) == 1 else f"{via_id}_cut_{cut_index}"
        lines.append(f'{cut_id} = dbCreateRect(cv list("{via.via_def}" "drawing") list({x0:g}:{y0:g} {x1:g}:{y1:g}))')
    for idx, (layer, bbox) in enumerate(via_landing_bboxes(via, pdk)):
        x0, y0, x1, y1 = bbox
        landing_id = f"{via_id}_landing_{idx}"
        lines.append(f'{landing_id} = dbCreateRect(cv list("{layer}" "drawing") list({x0:g}:{y0:g} {x1:g}:{y1:g}))')
        lines.extend(_shape_color_skill_lines(landing_id, layer, "drawing", pdk))
        if via.net:
            lines.append(_attach_fig_to_net_skill(landing_id, via.net))
    return tuple(lines)


def _native_via_cut_bbox(via: OaVia, pdk: PdkConfig) -> tuple[float, float, float, float] | None:
    cut_bboxes = _native_via_cut_bboxes(via, pdk)
    if not cut_bboxes:
        return None
    return (
        min(bbox[0] for bbox in cut_bboxes),
        min(bbox[1] for bbox in cut_bboxes),
        max(bbox[2] for bbox in cut_bboxes),
        max(bbox[3] for bbox in cut_bboxes),
    )


def _native_via_cut_bboxes(via: OaVia, pdk: PdkConfig) -> tuple[tuple[float, float, float, float], ...]:
    rules = getattr(pdk, "rules", None)
    if rules is None:
        return ()
    try:
        cut_width = float(rules.min_width_um(via.via_def))
    except Exception:
        try:
            cut_width = float(rules.min_width_um("VD")) if via.via_def in {"VIA0", "M0_PO", "M0_PO_VD", "M0_SUB", "M0_NW"} else 0.0
        except Exception:
            cut_width = 0.0
    if cut_width <= 0.0:
        return ()
    try:
        rows = max(int(getattr(via, "rows", 1) or 1), 1)
    except (TypeError, ValueError):
        rows = 1
    try:
        cols = max(int(getattr(via, "cols", 1) or 1), 1)
    except (TypeError, ValueError):
        cols = 1
    via_metadata = getattr(via, "metadata", {}) if isinstance(getattr(via, "metadata", {}), Mapping) else {}
    if "emit_cut_array" in via_metadata:
        use_cut_array = bool(via_metadata.get("emit_cut_array", False))
    else:
        use_cut_array = rows > 1 or cols > 1
    if not use_cut_array:
        rows = 1
        cols = 1
    try:
        cut_spacing = float(rules.array_spacing_um(via.via_def))
    except Exception:
        try:
            cut_spacing = float(rules.min_spacing_um(via.via_def))
        except Exception:
            cut_spacing = cut_width
    pitch = cut_width + max(cut_spacing, 0.0)
    x, y = via.xy
    half = cut_width / 2.0
    x0 = x - 0.5 * float(cols - 1) * pitch
    y0 = y - 0.5 * float(rows - 1) * pitch
    bboxes: list[tuple[float, float, float, float]] = []
    for row in range(rows):
        cy = y0 + row * pitch
        for col in range(cols):
            cx = x0 + col * pitch
            bbox = (cx - half, cy - half, cx + half, cy + half)
            try:
                bbox = tuple(float(value) for value in rules.snap_bbox_um(bbox, mode="nearest"))  # type: ignore[assignment]
            except Exception:
                pass
            bboxes.append(bbox)
    return tuple(bboxes)



class OaBackendRecorder:
    """Minimal backend adapter useful for tests and future OA binding shims."""

    def __init__(self) -> None:
        self.operations: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def open_cellview(self, cellview: OaCellView) -> None:
        self.operations.append(("open_cellview", (cellview,), {}))

    def clear_cellview(self) -> None:
        self.operations.append(("clear_cellview", (), {}))

    def create_net(self, net: str) -> None:
        self.operations.append(("create_net", (net,), {}))

    def create_pin(self, pin: OaPin) -> None:
        self.operations.append(("create_pin", (pin,), {}))

    def create_instance(self, instance: OaInstance) -> None:
        self.operations.append(("create_instance", (instance,), {}))

    def connect_instance_terminal(self, instance: str, terminal: str, net: str) -> None:
        self.operations.append(("connect_instance_terminal", (instance, terminal, net), {}))

    def create_rect(self, rect: OaRect) -> None:
        self.operations.append(("create_rect", (rect,), {}))

    def create_path(self, path: OaPath) -> None:
        self.operations.append(("create_path", (path,), {}))

    def create_via(self, via: OaVia) -> None:
        self.operations.append(("create_via", (via,), {}))

    def create_label(self, layer: str, text: str, xy: tuple[float, float]) -> None:
        self.operations.append(("create_label", (layer, text, xy), {}))

    def save(self) -> None:
        self.operations.append(("save", (), {}))

    def close(self) -> None:
        self.operations.append(("close", (), {}))


def apply_oa_write_plan(plan: OaWritePlan, backend: object) -> object:
    backend.open_cellview(plan.cellview)
    for net in plan.nets:
        backend.create_net(net)
    for inst in plan.instances:
        backend.create_instance(inst)
        for terminal, net in sorted(inst.connections.items()):
            if net:
                backend.connect_instance_terminal(inst.name, terminal, net)
    for rect in plan.rects:
        backend.create_rect(rect)
    for path_obj in plan.paths:
        backend.create_path(path_obj)
    for via in plan.vias:
        backend.create_via(via)
    for pin in plan.pins:
        backend.create_pin(pin)
    for layer, text, xy in plan.labels:
        backend.create_label(layer, text, xy)
    backend.save()
    backend.close()
    return backend


def apply_oa_replacement_plan(plan: OaWritePlan, backend: object) -> object:
    """Replace a cellview with a complete OA write plan."""

    backend.open_cellview(plan.cellview)
    backend.clear_cellview()
    for net in plan.nets:
        backend.create_net(net)
    for inst in plan.instances:
        backend.create_instance(inst)
        for terminal, net in sorted(inst.connections.items()):
            if net:
                backend.connect_instance_terminal(inst.name, terminal, net)
    for rect in plan.rects:
        backend.create_rect(rect)
    for path_obj in plan.paths:
        backend.create_path(path_obj)
    for via in plan.vias:
        backend.create_via(via)
    for pin in plan.pins:
        backend.create_pin(pin)
    for layer, text, xy in plan.labels:
        backend.create_label(layer, text, xy)
    backend.save()
    backend.close()
    return backend



def _first_path_for_net(paths: Sequence[OaPath], net: str, *, preferred_layers: Sequence[str] = ()) -> OaPath | None:
    preferred = tuple(dict.fromkeys(str(layer) for layer in preferred_layers if str(layer)))
    for layer in preferred:
        for path_obj in paths:
            if path_obj.net == net and path_obj.layer == layer:
                return path_obj
    for path_obj in paths:
        if path_obj.net == net:
            return path_obj
    return None


def _first_layer_for_path(paths: Sequence[OaPath], net: str) -> str:
    path_obj = _first_path_for_net(paths, net)
    return path_obj.layer if path_obj is not None else "M1"


def _path_pin_bbox(path_obj: object | None, default_width: float) -> tuple[float, float, float, float] | None:
    points = tuple(getattr(path_obj, "points", ())) if path_obj is not None else ()
    if path_obj is None or not points:
        return None
    x, y = _point_tuple(points[0])
    half = max(float(getattr(path_obj, "width", 0.0) or 0.0), default_width) / 2
    return (x - half, y - half, x + half, y + half)


def _bbox_tuple(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise ValueError(f"bbox must be a 4-tuple, got {value!r}")
    return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))


def _point_tuple(value: object) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(f"point must be a 2-tuple, got {value!r}")
    return (float(value[0]), float(value[1]))


def _coerce_pin(data: object) -> dict[str, object]:
    result = dict(data)
    if result.get("bbox") is not None:
        result["bbox"] = tuple(result["bbox"])
    result.setdefault("emit_draw_rect", True)
    return result


def _coerce_instance(data: object) -> dict[str, object]:
    result = dict(data)
    result["xy"] = tuple(result.get("xy", (0.0, 0.0)))
    result["connections"] = dict(result.get("connections", {}))
    result["params"] = dict(result.get("params", {}))
    result["metadata"] = dict(result.get("metadata", {}) or {})
    result.setdefault("instantiation_method", "dbCreateInstByMasterName")
    return result


def _coerce_rect(data: object) -> dict[str, object]:
    result = dict(data)
    result["bbox"] = tuple(result["bbox"])
    result.setdefault("color", "")
    result["metadata"] = dict(result.get("metadata", {}) or {})
    return result


def _coerce_path(data: object) -> dict[str, object]:
    result = dict(data)
    result["points"] = tuple(tuple(point) for point in result.get("points", ()))
    result.setdefault("color", "")
    return result


def _coerce_via(data: object) -> dict[str, object]:
    result = dict(data)
    result["xy"] = tuple(result["xy"])
    return result
