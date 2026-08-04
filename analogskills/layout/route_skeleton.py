"""Lower lightweight route-skeleton LayoutPlan artifacts into routing handoff data.

The route skeleton is intentionally weaker than detailed routing: it records
pin escape order, preferred layers, coarse route corridors, bus ordering, and
minimum-width intent.  This module converts that information into existing
analogskills routing contracts so the detailed router, local SMT router, and agent
diagnostics can consume the same artifact without special-casing reference
design flows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from analogskills.contracts import LayoutConstraintSet, RoutingConstraint, TopologyGraph


BBox = tuple[float, float, float, float]
Point = tuple[float, float]


@dataclass(frozen=True)
class RouteSkeletonCorridor:
    name: str
    nets: tuple[str, ...]
    bbox_um: BBox
    layer: str
    role: str = "route_skeleton"
    status: str = "intent"
    source: str = "route_skeleton"
    target: str = ""
    routing_style: str = ""
    forbidden_nets: tuple[str, ...] = ()
    waiver_nets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "nets": self.nets,
            "bbox_um": self.bbox_um,
            "layer": self.layer,
            "role": self.role,
            "status": self.status,
            "source": self.source,
            "target": self.target,
            "routing_style": self.routing_style,
            "forbidden_nets": self.forbidden_nets,
            "waiver_nets": self.waiver_nets,
        }


@dataclass(frozen=True)
class RouteSkeletonRoutingHandoff:
    graph_name: str = ""
    constraints: LayoutConstraintSet = field(default_factory=LayoutConstraintSet)
    corridors: tuple[RouteSkeletonCorridor, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "graph_name": self.graph_name,
            "constraints": _layout_constraints_to_dict(self.constraints),
            "corridors": tuple(corridor.to_dict() for corridor in self.corridors),
            "metadata": dict(self.metadata),
        }


def build_route_skeleton_routing_handoff(
    route_skeleton: object,
    *,
    graph: TopologyGraph | None = None,
    base_constraints: LayoutConstraintSet | None = None,
    corridor_margin_um: float = 0.2,
    forbid_nonmember_nets: bool = False,
) -> RouteSkeletonRoutingHandoff:
    """Build a backend-consumable routing handoff from a route-skeleton plan."""

    active_base = base_constraints or (graph.layout_constraints if graph is not None else LayoutConstraintSet())
    known_nets = _known_nets(graph, route_skeleton)
    skeleton_constraints = route_skeleton_constraints(route_skeleton, graph=graph, known_nets=known_nets)
    merged_constraints = _merge_layout_constraints(active_base, skeleton_constraints)
    corridors = route_skeleton_corridors(
        route_skeleton,
        graph=graph,
        known_nets=known_nets,
        margin_um=corridor_margin_um,
        forbid_nonmember_nets=forbid_nonmember_nets,
    )
    graph_name = str(getattr(route_skeleton, "metadata", {}).get("graph_name", "") if isinstance(getattr(route_skeleton, "metadata", {}), Mapping) else "")
    if not graph_name and graph is not None:
        graph_name = graph.name
    return RouteSkeletonRoutingHandoff(
        graph_name=graph_name,
        constraints=merged_constraints,
        corridors=corridors,
        metadata={
            "source": "layout.route_skeleton.build_route_skeleton_routing_handoff",
            "route_skeleton_pin_count": len(tuple(getattr(route_skeleton, "pins", ()) or ())),
            "route_skeleton_path_count": len(tuple(getattr(route_skeleton, "paths", ()) or ())),
            "route_skeleton_constraint_count": len(skeleton_constraints.routing),
            "merged_routing_constraint_count": len(merged_constraints.routing),
            "route_skeleton_corridor_count": len(corridors),
            "route_skeleton_bus_order_count": sum(1 for item in skeleton_constraints.routing if item.kind == "bus_order"),
            "forbid_nonmember_nets": bool(forbid_nonmember_nets),
        },
    )


def route_skeleton_constraints(
    route_skeleton: object,
    *,
    graph: TopologyGraph | None = None,
    known_nets: Sequence[str] | None = None,
) -> LayoutConstraintSet:
    """Convert route-skeleton pins/paths into standard routing constraints."""

    known = set(known_nets if known_nets is not None else _known_nets(graph, route_skeleton))
    constraints: list[RoutingConstraint] = []
    critical_nets = list(_critical_nets_from_skeleton(route_skeleton, known))
    pin_edges: dict[str, str] = {}

    for pin in tuple(getattr(route_skeleton, "pins", ()) or ()):
        net = _pin_net(pin)
        if not _is_known_or_unconstrained(net, known):
            continue
        layer = str(getattr(pin, "layer", "") or "")
        metadata = _metadata(pin)
        if layer:
            constraints.append(RoutingConstraint(net, "route_layer", layer, "route skeleton top-level pin layer"))
        edge = str(metadata.get("reference_edge", "") or metadata.get("edge", "") or "")
        if edge:
            pin_edges[net] = edge
            constraints.append(RoutingConstraint(net, "pin_edge", edge, "route skeleton reference pin edge"))
        if "reference_order" in metadata:
            order = _int_or_none(metadata.get("reference_order"))
            if order is not None:
                constraints.append(RoutingConstraint(net, "pin_order_index", order, "route skeleton reference pin order"))

    for path in tuple(getattr(route_skeleton, "paths", ()) or ()):
        layer = str(getattr(path, "layer", "") or "")
        width = float(getattr(path, "width", 0.0) or 0.0)
        metadata = _metadata(path)
        kind = str(metadata.get("kind", "") or "route_skeleton_path")
        members = tuple(net for net in _path_member_nets(path, known) if _is_known_or_unconstrained(net, known))
        if kind == "bus_escape_trunk" and len(members) >= 2:
            family = str(getattr(path, "net", "") or _common_bus_family(members) or "bus")
            constraints.append(RoutingConstraint(family, "bus_order", members, "route skeleton bus escape trunk order"))
        for net in members:
            if layer:
                constraints.append(RoutingConstraint(net, "route_layer", layer, f"route skeleton {kind} layer"))
            if width > 0:
                constraints.append(RoutingConstraint(net, "min_width_nm", max(1, int(round(width * 1000.0))), f"route skeleton {kind} width"))
            if kind:
                constraints.append(RoutingConstraint(net, "route_skeleton_kind", kind, "route skeleton path classification"))
            edge = pin_edges.get(net)
            if edge:
                constraints.append(RoutingConstraint(net, "preferred_escape_edge", edge, "route skeleton pin edge preference"))

    for family, members in _bus_families_from_skeleton(route_skeleton, known).items():
        if len(members) >= 2:
            constraints.append(RoutingConstraint(family, "bus_order", members, "route skeleton bus family metadata"))

    return LayoutConstraintSet(
        matched_groups=(),
        symmetry_groups=(),
        routing=_dedupe_routing_constraints(constraints),
        critical_nets=tuple(dict.fromkeys(net for net in critical_nets if _is_known_or_unconstrained(net, known))),
    )


def route_skeleton_corridors(
    route_skeleton: object,
    *,
    graph: TopologyGraph | None = None,
    known_nets: Sequence[str] | None = None,
    margin_um: float = 0.2,
    forbid_nonmember_nets: bool = False,
) -> tuple[RouteSkeletonCorridor, ...]:
    """Convert skeleton paths to lightweight named corridor hints."""

    known = set(known_nets if known_nets is not None else _known_nets(graph, route_skeleton))
    all_known = tuple(sorted(net for net in known if net))
    rows: list[RouteSkeletonCorridor] = []
    for index, path in enumerate(tuple(getattr(route_skeleton, "paths", ()) or ())):
        layer = str(getattr(path, "layer", "") or "")
        if not layer:
            continue
        bbox = _path_bbox(path, margin_um=margin_um)
        if bbox is None or not _bbox_has_area(bbox):
            continue
        metadata = _metadata(path)
        role = str(metadata.get("kind", "") or "route_skeleton_path")
        nets = tuple(net for net in _path_member_nets(path, known) if _is_known_or_unconstrained(net, known))
        if not nets:
            continue
        forbidden = tuple(net for net in all_known if net not in nets) if forbid_nonmember_nets else ()
        rows.append(
            RouteSkeletonCorridor(
                name=_corridor_name(index, role, nets),
                nets=nets,
                bbox_um=bbox,
                layer=layer,
                role=role,
                status="intent",
                source="route_skeleton",
                routing_style="preferred_escape" if role != "bus_escape_trunk" else "ordered_bus_escape",
                forbidden_nets=forbidden,
            )
        )
    return tuple(rows)


def augment_layout_constraints_with_route_skeleton(
    constraints: LayoutConstraintSet,
    route_skeleton: object,
    *,
    graph: TopologyGraph | None = None,
) -> LayoutConstraintSet:
    """Return constraints plus route-skeleton-derived routing intent."""

    return build_route_skeleton_routing_handoff(
        route_skeleton,
        graph=graph,
        base_constraints=constraints,
    ).constraints


def _known_nets(graph: TopologyGraph | None, route_skeleton: object | None = None) -> tuple[str, ...]:
    nets: list[str] = []
    if graph is not None:
        nets.extend(str(net) for net in graph.nets)
        nets.extend(str(pin) for pin in graph.pins)
    if route_skeleton is not None:
        nets.extend(str(net) for net in tuple(getattr(route_skeleton, "nets", ()) or ()) if str(net))
        nets.extend(_pin_net(pin) for pin in tuple(getattr(route_skeleton, "pins", ()) or ()))
    return tuple(dict.fromkeys(net for net in nets if net))


def _critical_nets_from_skeleton(route_skeleton: object, known: set[str]) -> tuple[str, ...]:
    metadata = _metadata(route_skeleton)
    values = metadata.get("critical_nets", ())
    if isinstance(values, str):
        candidates = (values,)
    elif isinstance(values, Sequence):
        candidates = tuple(str(item) for item in values if str(item))
    else:
        candidates = ()
    return tuple(net for net in candidates if _is_known_or_unconstrained(net, known))


def _bus_families_from_skeleton(route_skeleton: object, known: set[str]) -> dict[str, tuple[str, ...]]:
    metadata = _metadata(route_skeleton)
    raw = metadata.get("bus_families", {})
    result: dict[str, tuple[str, ...]] = {}
    if isinstance(raw, Mapping):
        for family, members in raw.items():
            if isinstance(members, str):
                member_tuple = (members,)
            elif isinstance(members, Sequence):
                member_tuple = tuple(str(item) for item in members if str(item))
            else:
                member_tuple = ()
            filtered = tuple(net for net in member_tuple if _is_known_or_unconstrained(net, known))
            if len(filtered) >= 2:
                result[str(family)] = filtered
    return result


def _path_member_nets(path: object, known: set[str]) -> tuple[str, ...]:
    metadata = _metadata(path)
    members = metadata.get("members", ())
    if isinstance(members, str):
        raw_members = (members,)
    elif isinstance(members, Sequence):
        raw_members = tuple(str(item) for item in members if str(item))
    else:
        raw_members = ()
    if raw_members:
        return tuple(dict.fromkeys(raw_members))
    net = str(getattr(path, "net", "") or "")
    if not net:
        return ()
    if _is_known_or_unconstrained(net, known):
        return (net,)
    return ()


def _pin_net(pin: object) -> str:
    return str(getattr(pin, "net", "") or getattr(pin, "name", "") or "")


def _metadata(item: object) -> Mapping[str, object]:
    raw = getattr(item, "metadata", {}) if item is not None else {}
    return raw if isinstance(raw, Mapping) else {}


def _path_bbox(path: object, *, margin_um: float) -> BBox | None:
    points = tuple(getattr(path, "points", ()) or ())
    if not points:
        return None
    coords = tuple((float(point[0]), float(point[1])) for point in points)
    half = max(float(getattr(path, "width", 0.0) or 0.0) * 0.5, 0.0) + max(float(margin_um), 0.0)
    xs = [point[0] for point in coords]
    ys = [point[1] for point in coords]
    return (min(xs) - half, min(ys) - half, max(xs) + half, max(ys) + half)


def _bbox_has_area(bbox: BBox) -> bool:
    return bbox[2] > bbox[0] and bbox[3] > bbox[1]


def _is_known_or_unconstrained(net: str, known: set[str]) -> bool:
    return bool(net) and (not known or net in known)


def _common_bus_family(members: Sequence[str]) -> str:
    if not members:
        return ""
    prefixes = []
    for member in members:
        text = str(member)
        idx = len(text)
        while idx > 0 and text[idx - 1].isdigit():
            idx -= 1
        prefixes.append(text[:idx])
    first = prefixes[0]
    return first if all(prefix == first for prefix in prefixes) else ""


def _corridor_name(index: int, role: str, nets: Sequence[str]) -> str:
    net_stem = "_".join(str(net) for net in tuple(nets)[:3])
    if len(tuple(nets)) > 3:
        net_stem += "_bus"
    text = f"route_skeleton_{index}_{role}_{net_stem}"
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in text).strip("_")


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _merge_layout_constraints(base: LayoutConstraintSet, extra: LayoutConstraintSet) -> LayoutConstraintSet:
    return LayoutConstraintSet(
        matched_groups=base.matched_groups,
        symmetry_groups=base.symmetry_groups,
        routing=_dedupe_routing_constraints((*base.routing, *extra.routing)),
        critical_nets=tuple(dict.fromkeys((*base.critical_nets, *extra.critical_nets))),
        standard_cell=base.standard_cell,
    )


def _dedupe_routing_constraints(constraints: Sequence[RoutingConstraint]) -> tuple[RoutingConstraint, ...]:
    rows: dict[tuple[str, str, object], RoutingConstraint] = {}
    for constraint in constraints:
        value = constraint.value
        if isinstance(value, list):
            value = tuple(value)
        elif isinstance(value, set):
            value = tuple(sorted(str(item) for item in value))
        key = (str(constraint.net), str(constraint.kind), value)
        if key not in rows:
            rows[key] = RoutingConstraint(str(constraint.net), str(constraint.kind), value, str(constraint.reason))
    return tuple(rows.values())


def _layout_constraints_to_dict(constraints: LayoutConstraintSet) -> dict[str, object]:
    return {
        "matched_groups": tuple(
            {
                "name": group.name,
                "devices": group.devices,
                "style": group.style,
                "require_dummies": group.require_dummies,
                "unit_segments": group.unit_segments,
                "notes": group.notes,
            }
            for group in constraints.matched_groups
        ),
        "symmetry_groups": constraints.symmetry_groups,
        "routing": tuple(
            {
                "net": item.net,
                "kind": item.kind,
                "value": item.value,
                "reason": item.reason,
            }
            for item in constraints.routing
        ),
        "critical_nets": constraints.critical_nets,
    }
