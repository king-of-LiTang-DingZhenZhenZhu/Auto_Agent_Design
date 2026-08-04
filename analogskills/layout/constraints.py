"""Topology-to-layout constraint extraction and routing-intent lowering."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from analogskills.contracts import DeviceRole, LayoutConstraintSet, MatchGroup, NetRole, RoutingConstraint, TerminalRef, TopologyGraph


_AUTO_MATCH_STYLE = {
    DeviceRole.INPUT_PAIR: ("common_centroid", True, 4),
    DeviceRole.CURRENT_MIRROR: ("interdigitated", True, 2),
    DeviceRole.DRIVER: ("common_centroid", True, 2),
    DeviceRole.LOAD: ("common_centroid", True, 2),
    DeviceRole.CASCODE: ("mirror", True, 1),
    DeviceRole.TAIL: ("mirror", False, 1),
    DeviceRole.BIAS: ("mirror", False, 1),
}
_PAIR_SUFFIXES = (
    ("_P", "_N"),
    ("_L", "_R"),
    ("_A", "_B"),
    ("P", "N"),
    ("L", "R"),
    ("A", "B"),
)
_CRITICAL_NET_ROLES = {
    NetRole.INPUT,
    NetRole.OUTPUT,
    NetRole.HIGH_Z,
    NetRole.DIFFERENTIAL,
    NetRole.COMPENSATION,
    NetRole.CLOCK,
}


@dataclass(frozen=True)
class RoutingNetIntent:
    net: str
    constraints: tuple[RoutingConstraint, ...] = ()
    critical: bool = False
    role: str = ""
    policy_kinds: tuple[str, ...] = ()
    route_layer: str = ""
    shield: bool = False
    shield_net: str = ""
    wide: bool = False
    via_array: bool = False
    min_width_nm: float | None = None
    max_length_um: float | None = None
    current_ma: float | None = None
    target_current_ma: float | None = None
    avoid_nets: tuple[str, ...] = ()
    differential_partners: tuple[str, ...] = ()
    match_length_with: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutingIntentSet:
    intents: tuple[RoutingNetIntent, ...] = ()
    constraints: tuple[RoutingConstraint, ...] = ()
    critical_nets: tuple[str, ...] = ()
    pin_alias_map: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def for_net(self, net: str) -> RoutingNetIntent:
        normalized = resolve_layout_net_name(net, pin_alias_map=self.pin_alias_map)
        for intent in self.intents:
            if intent.net == normalized:
                return intent
        return RoutingNetIntent(net=normalized)

    def constraint_map(self) -> dict[str, tuple[RoutingConstraint, ...]]:
        return {item.net: item.constraints for item in self.intents}

    def intent_map(self) -> dict[str, RoutingNetIntent]:
        return {item.net: item for item in self.intents}


def extract_layout_constraints(
    graph: TopologyGraph,
    base_constraints: LayoutConstraintSet | None = None,
) -> LayoutConstraintSet:
    base = base_constraints or graph.layout_constraints
    matched = _merge_match_groups(base.matched_groups, _infer_match_groups(graph, base))
    symmetry = _merge_symmetry_groups(base.symmetry_groups, _infer_symmetry_groups(graph, matched))
    routing = _merge_routing_constraints(base.routing, _infer_routing_constraints(graph))
    critical = tuple(dict.fromkeys((*base.critical_nets, *_infer_critical_nets(graph))))
    return LayoutConstraintSet(
        matched_groups=matched,
        symmetry_groups=symmetry,
        routing=routing,
        critical_nets=critical,
        standard_cell=base.standard_cell,
    )


def build_pin_alias_map(graph: TopologyGraph | None) -> dict[str, str]:
    if graph is None:
        return {}
    term_map = graph.terminal_net_map()
    return {
        str(pin): str(term_map[TerminalRef(str(pin), "PIN")])
        for pin in graph.pins
        if TerminalRef(str(pin), "PIN") in term_map
    }


def resolve_layout_net_name(
    name: str,
    *,
    graph: TopologyGraph | None = None,
    pin_alias_map: Mapping[str, str] | None = None,
) -> str:
    candidate = str(name or "")
    if not candidate:
        return ""
    if graph is not None and candidate in graph.nets:
        return candidate
    aliases = pin_alias_map or {}
    return str(aliases.get(candidate, candidate))


def normalize_routing_constraints(
    constraints: Sequence[RoutingConstraint],
    *,
    graph: TopologyGraph | None = None,
    pin_alias_map: Mapping[str, str] | None = None,
) -> tuple[RoutingConstraint, ...]:
    aliases = dict(pin_alias_map or {})
    rows: list[RoutingConstraint] = []
    for item in constraints:
        resolved_net = resolve_layout_net_name(str(item.net), graph=graph, pin_alias_map=aliases)
        if graph is not None and resolved_net not in graph.nets:
            continue
        value = item.value
        if isinstance(value, str):
            value = resolve_layout_net_name(value, graph=graph, pin_alias_map=aliases)
        elif isinstance(value, (tuple, list, set)):
            value = tuple(
                resolve_layout_net_name(str(entry), graph=graph, pin_alias_map=aliases) if isinstance(entry, str) else entry
                for entry in value
            )
        rows.append(RoutingConstraint(resolved_net, item.kind, value, item.reason))
    return tuple(rows)


def normalized_critical_nets(
    constraints: LayoutConstraintSet,
    *,
    graph: TopologyGraph | None = None,
    pin_alias_map: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    aliases = dict(pin_alias_map or {})
    candidates = list(constraints.critical_nets)
    if graph is not None:
        candidates.extend(
            net.name
            for net in graph.nets.values()
            if net.role in {NetRole.HIGH_Z, NetRole.REFERENCE, NetRole.DIFFERENTIAL}
        )
    normalized: list[str] = []
    for candidate in candidates:
        resolved = resolve_layout_net_name(str(candidate), graph=graph, pin_alias_map=aliases)
        if not resolved:
            continue
        if graph is not None and resolved not in graph.nets:
            continue
        normalized.append(resolved)
    return tuple(dict.fromkeys(normalized))


def build_routing_intent_set(
    constraints: LayoutConstraintSet | None = None,
    *,
    graph: TopologyGraph | None = None,
    available_nets: Sequence[str] = (),
    pin_alias_map: Mapping[str, str] | None = None,
) -> RoutingIntentSet:
    active = constraints or (graph.layout_constraints if graph is not None else LayoutConstraintSet())
    aliases = dict(pin_alias_map or build_pin_alias_map(graph))
    normalized_constraints = normalize_routing_constraints(active.routing, graph=graph, pin_alias_map=aliases)
    critical_nets = normalized_critical_nets(active, graph=graph, pin_alias_map=aliases)
    known_nets: list[str] = []
    if graph is not None:
        known_nets.extend(str(net) for net in graph.nets)
    known_nets.extend(resolve_layout_net_name(str(net), graph=graph, pin_alias_map=aliases) for net in available_nets if str(net))
    known_nets.extend(str(item.net) for item in normalized_constraints if str(item.net))
    known_nets.extend(critical_nets)
    net_order = tuple(dict.fromkeys(net for net in known_nets if net))
    per_net: dict[str, list[RoutingConstraint]] = {net: [] for net in net_order}
    for item in normalized_constraints:
        per_net.setdefault(str(item.net), []).append(item)

    intents: list[RoutingNetIntent] = []
    for net in net_order:
        net_constraints = tuple(per_net.get(net, ()))
        role = ""
        if graph is not None and net in graph.nets:
            net_role = getattr(graph.nets[net], "role", None)
            role = str(net_role.value if hasattr(net_role, "value") else net_role or "")
        policy_kinds = tuple(sorted({str(item.kind) for item in net_constraints}))
        route_layer = next(
            (
                str(item.value)
                for item in net_constraints
                if str(item.kind) == "route_layer" and isinstance(item.value, str) and str(item.value)
            ),
            "",
        )
        shield = any(str(item.kind) == "shield" and bool(item.value) for item in net_constraints)
        shield_net = next(
            (
                str(item.value)
                for item in net_constraints
                if str(item.kind) == "shield_net" and isinstance(item.value, str) and str(item.value)
            ),
            "VSS" if shield else "",
        )
        wide = any(str(item.kind) == "wide" and bool(item.value) for item in net_constraints)
        via_array = any(str(item.kind) == "via_array" and bool(item.value) for item in net_constraints)
        min_width_values = [
            float(item.value)
            for item in net_constraints
            if str(item.kind) == "min_width_nm" and isinstance(item.value, (float, int))
        ]
        max_length_values = [
            float(item.value)
            for item in net_constraints
            if str(item.kind) == "max_length_um" and isinstance(item.value, (float, int))
        ]
        current_values = [
            float(item.value)
            for item in net_constraints
            if str(item.kind) in {"current_ma", "target_current_ma"} and isinstance(item.value, (float, int))
        ]
        avoid_nets = _normalized_constraint_values(net_constraints, "avoid_nets", graph=graph, pin_alias_map=aliases, net=net)
        differential_partners = _normalized_constraint_values(
            net_constraints,
            "differential_partner",
            graph=graph,
            pin_alias_map=aliases,
            net=net,
        )
        match_length_with = _normalized_constraint_values(
            net_constraints,
            "match_length_with",
            graph=graph,
            pin_alias_map=aliases,
            net=net,
        )
        intents.append(
            RoutingNetIntent(
                net=net,
                constraints=net_constraints,
                critical=net in critical_nets,
                role=role,
                policy_kinds=policy_kinds,
                route_layer=route_layer,
                shield=shield,
                shield_net=shield_net,
                wide=wide,
                via_array=via_array,
                min_width_nm=max(min_width_values) if min_width_values else None,
                max_length_um=min(max_length_values) if max_length_values else None,
                current_ma=max(current_values) if current_values else None,
                target_current_ma=max(current_values) if current_values else None,
                avoid_nets=avoid_nets,
                differential_partners=differential_partners,
                match_length_with=match_length_with,
            )
        )
    return RoutingIntentSet(
        intents=tuple(intents),
        constraints=normalized_constraints,
        critical_nets=critical_nets,
        pin_alias_map=aliases,
        metadata={
            "known_net_count": len(net_order),
            "normalized_constraint_count": len(normalized_constraints),
        },
    )


def _infer_match_groups(graph: TopologyGraph, base: LayoutConstraintSet) -> tuple[MatchGroup, ...]:
    groups: list[MatchGroup] = []
    existing = tuple(base.matched_groups)

    input_pair = tuple(name for name, dev in graph.devices.items() if dev.role == DeviceRole.INPUT_PAIR)
    if len(input_pair) >= 2 and not _has_match_group(existing, input_pair[:2]):
        groups.append(MatchGroup("input_pair", input_pair[:2], style="common_centroid", require_dummies=True, unit_segments=4))

    mirrors = tuple(name for name, dev in graph.devices.items() if dev.role == DeviceRole.CURRENT_MIRROR)
    if len(mirrors) >= 2 and not _has_match_group(existing, mirrors):
        groups.append(MatchGroup("current_mirror", mirrors, style="interdigitated", require_dummies=True, unit_segments=max(2, len(mirrors))))

    for left, right in _infer_device_pairs(graph):
        if _has_match_group((*existing, *groups), (left, right)):
            continue
        role = graph.devices[left].role
        if role == DeviceRole.CURRENT_MIRROR:
            continue
        style, require_dummies, unit_segments = _AUTO_MATCH_STYLE.get(role, ("mirror", True, 1))
        groups.append(
            MatchGroup(
                _auto_group_name(graph, left, right),
                (left, right),
                style=style,
                require_dummies=require_dummies,
                unit_segments=unit_segments,
            )
        )
    return tuple(groups)


def _infer_symmetry_groups(
    graph: TopologyGraph,
    matched_groups: tuple[MatchGroup, ...],
) -> tuple[tuple[str, ...], ...]:
    groups: list[tuple[str, ...]] = [
        tuple(group.devices)
        for group in matched_groups
        if len(group.devices) == 2 and group.style != "interdigitated"
    ]
    for pair in _infer_device_pairs(graph):
        if not _has_symmetry_group(tuple(groups), pair):
            groups.append(pair)
    return tuple(groups)


def _infer_routing_constraints(graph: TopologyGraph) -> tuple[RoutingConstraint, ...]:
    routing: list[RoutingConstraint] = []
    for left, right in _infer_differential_net_pairs(graph):
        routing.append(RoutingConstraint(left, "differential_partner", right, "auto-inferred differential partner"))
        routing.append(RoutingConstraint(right, "differential_partner", left, "auto-inferred differential partner"))

    for net in graph.nets.values():
        if net.role == NetRole.HIGH_Z:
            routing.append(RoutingConstraint(net.name, "shield", True, "high-Z net"))
            routing.append(RoutingConstraint(net.name, "max_length_um", 10.0, "high-Z net"))
        if net.role in {NetRole.OUTPUT, NetRole.SUPPLY, NetRole.GROUND}:
            routing.append(RoutingConstraint(net.name, "wide", True, f"{net.role.value} net"))
        if net.role == NetRole.BIAS and (_net_name_contains(net.name, "TAIL") or len(net.terminals) >= 4):
            routing.append(RoutingConstraint(net.name, "wide", True, "shared bias net"))
        if net.role == NetRole.CLOCK and len(net.terminals) >= 3:
            routing.append(RoutingConstraint(net.name, "max_length_um", 12.0, "clock distribution net"))
    return tuple(routing)


def _infer_critical_nets(graph: TopologyGraph) -> tuple[str, ...]:
    nets = [
        net.name
        for net in graph.nets.values()
        if net.role in _CRITICAL_NET_ROLES
        or (net.role == NetRole.BIAS and (_net_name_contains(net.name, "TAIL") or _net_connects_role(graph, net.name, {DeviceRole.TAIL})))
    ]
    return tuple(dict.fromkeys(nets))


def _merge_match_groups(
    existing: tuple[MatchGroup, ...],
    inferred: tuple[MatchGroup, ...],
) -> tuple[MatchGroup, ...]:
    groups = list(existing)
    for group in inferred:
        if not _has_match_group(tuple(groups), group.devices):
            groups.append(group)
    return tuple(groups)


def _merge_symmetry_groups(
    existing: tuple[tuple[str, ...], ...],
    inferred: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], ...]:
    groups = list(existing)
    for group in inferred:
        if not _has_symmetry_group(tuple(groups), group):
            groups.append(group)
    return tuple(groups)


def _merge_routing_constraints(
    existing: tuple[RoutingConstraint, ...],
    inferred: tuple[RoutingConstraint, ...],
) -> tuple[RoutingConstraint, ...]:
    merged = list(existing)
    seen = {(item.net, item.kind, item.value) for item in merged}
    for item in inferred:
        key = (item.net, item.kind, item.value)
        if key in seen:
            continue
        merged.append(item)
        seen.add(key)
    return tuple(merged)


def _infer_device_pairs(graph: TopologyGraph) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name, device in graph.devices.items():
        for left_suffix, right_suffix in _PAIR_SUFFIXES:
            if not name.endswith(left_suffix) or len(name) <= len(left_suffix):
                continue
            partner = f"{name[:-len(left_suffix)]}{right_suffix}"
            if partner not in graph.devices:
                continue
            other = graph.devices[partner]
            if device.role != other.role or device.model != other.model or device.terminals != other.terminals:
                continue
            pair = (name, partner)
            if pair in seen:
                break
            reverse = (partner, name)
            if reverse in seen:
                break
            pairs.append(pair)
            seen.add(pair)
            break
    return tuple(pairs)


def _infer_differential_net_pairs(graph: TopologyGraph) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name, net in graph.nets.items():
        if net.role not in {NetRole.INPUT, NetRole.OUTPUT, NetRole.DIFFERENTIAL, NetRole.CLOCK, NetRole.INTERNAL}:
            continue
        for left_suffix, right_suffix in _PAIR_SUFFIXES:
            if not name.endswith(left_suffix) or len(name) <= len(left_suffix):
                continue
            partner = f"{name[:-len(left_suffix)]}{right_suffix}"
            if partner not in graph.nets:
                continue
            other = graph.nets[partner]
            if other.role != net.role:
                continue
            pair = (name, partner)
            if pair in seen or (partner, name) in seen:
                break
            pairs.append(pair)
            seen.add(pair)
            break
    return tuple(pairs)


def _auto_group_name(graph: TopologyGraph, left: str, right: str) -> str:
    role = graph.devices[left].role.value
    stem = _common_stem(left, right) or f"{left}_{right}"
    return f"{role}_{stem}".strip("_")


def _common_stem(left: str, right: str) -> str:
    idx = 0
    limit = min(len(left), len(right))
    while idx < limit and left[idx] == right[idx]:
        idx += 1
    return left[:idx].rstrip("_")


def _has_match_group(groups: tuple[MatchGroup, ...], devices: tuple[str, ...]) -> bool:
    return any(set(group.devices) == set(devices) for group in groups)


def _has_symmetry_group(groups: tuple[tuple[str, ...], ...], devices: tuple[str, ...]) -> bool:
    return any(set(group) == set(devices) for group in groups)


def _net_name_contains(name: str, token: str) -> bool:
    return token.lower() in name.lower()


def _net_connects_role(graph: TopologyGraph, net_name: str, roles: set[DeviceRole]) -> bool:
    net = graph.nets.get(net_name)
    if net is None:
        return False
    return any(graph.devices.get(term.device) and graph.devices[term.device].role in roles for term in net.terminals)


def _normalized_constraint_values(
    constraints: Sequence[RoutingConstraint],
    kind: str,
    *,
    graph: TopologyGraph | None = None,
    pin_alias_map: Mapping[str, str] | None = None,
    net: str = "",
) -> tuple[str, ...]:
    aliases = dict(pin_alias_map or {})
    values: list[str] = []
    for item in constraints:
        if str(item.kind) != kind:
            continue
        raw_value = item.value
        if isinstance(raw_value, (tuple, list, set)):
            entries = tuple(raw_value)
        else:
            entries = (raw_value,)
        for entry in entries:
            if not isinstance(entry, str):
                continue
            resolved = resolve_layout_net_name(entry, graph=graph, pin_alias_map=aliases)
            if not resolved or resolved == net:
                continue
            if graph is not None and resolved not in graph.nets:
                continue
            values.append(resolved)
    return tuple(dict.fromkeys(values))
