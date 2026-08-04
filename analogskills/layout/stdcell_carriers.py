"""Primitive-carrier decomposition helpers for native standard-cell topology."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from analogskills.contracts import NetRole, TerminalRef, TopologyGraph


@dataclass(frozen=True)
class NativeStdCellCarrierDevice:
    device: str
    source_net: str
    drain_net: str
    gate_net: str
    bulk_net: str

    @property
    def conduction_nets(self) -> tuple[str, str]:
        return (self.source_net, self.drain_net)


@dataclass(frozen=True)
class NativeStdCellCarrier:
    name: str
    row: str
    kind: str
    model: str
    devices: tuple[NativeStdCellCarrierDevice, ...]
    endpoint_nets: tuple[str, ...]
    internal_nets: tuple[str, ...]
    gate_nets: tuple[str, ...]
    bulk_nets: tuple[str, ...]

    @property
    def device_names(self) -> tuple[str, ...]:
        return tuple(device.device for device in self.devices)

    @property
    def device_count(self) -> int:
        return len(self.devices)

    @property
    def preferred_generator(self) -> str:
        if self.kind == "series":
            if self.device_count == 2 and len(set(self.gate_nets)) == 2:
                return "cascode"
            return "stack_series"
        if self.kind == "parallel":
            return "parallel_group"
        if self.kind == "single":
            return "single_device"
        return "custom_array"


def build_native_stdcell_carriers(
    graph: TopologyGraph,
    *,
    role_by_device: Mapping[str, str] | None = None,
) -> tuple[NativeStdCellCarrier, ...]:
    term_map = graph.terminal_net_map()
    row_map = {
        name: _row_name_for_device(name, device.model, role_by_device or {})
        for name, device in graph.devices.items()
        if _is_supported_mos(device.model)
    }
    if not row_map:
        return ()

    device_records = {
        name: NativeStdCellCarrierDevice(
            device=name,
            source_net=str(term_map[TerminalRef(name, "S")]),
            drain_net=str(term_map[TerminalRef(name, "D")]),
            gate_net=str(term_map[TerminalRef(name, "G")]),
            bulk_net=str(term_map[TerminalRef(name, "B")]),
        )
        for name in row_map
        if all(TerminalRef(name, term) in term_map for term in ("S", "D", "G", "B"))
    }

    carriers: list[NativeStdCellCarrier] = []
    row_indices: dict[str, int] = defaultdict(int)
    for row in ("pmos", "nmos"):
        row_devices = tuple(name for name, dev_row in row_map.items() if dev_row == row and name in device_records)
        for component in _row_components(row_devices, device_records):
            ordered_devices = _ordered_component_devices(component, device_records, graph)
            kind = _classify_component(ordered_devices, device_records)
            records = tuple(device_records[name] for name in ordered_devices)
            endpoint_nets, internal_nets = _component_nets(kind, ordered_devices, device_records, graph)
            bulk_nets = tuple(
                sorted(
                    {record.bulk_net for record in records},
                    key=lambda net: (_net_priority(graph, net), net),
                )
            )
            carrier_name = f"{row}_{kind}_{row_indices[row]}"
            row_indices[row] += 1
            carriers.append(
                NativeStdCellCarrier(
                    name=carrier_name,
                    row=row,
                    kind=kind,
                    model=_normalize_model(graph.devices[ordered_devices[0]].model),
                    devices=records,
                    endpoint_nets=endpoint_nets,
                    internal_nets=internal_nets,
                    gate_nets=tuple(record.gate_net for record in records),
                    bulk_nets=bulk_nets,
                )
            )
    return tuple(carriers)


def _is_supported_mos(model: str) -> bool:
    normalized = _normalize_model(model)
    return normalized.startswith("nch") or normalized.startswith("pch") or "nmos" in normalized or "pmos" in normalized


def _normalize_model(model: str) -> str:
    return str(model).strip().lower()


def _row_name_for_device(name: str, model: str, role_by_device: Mapping[str, str]) -> str:
    role = str(role_by_device.get(name, "")).lower()
    if role.startswith("pmos"):
        return "pmos"
    if role.startswith("nmos"):
        return "nmos"
    normalized = _normalize_model(model)
    if normalized.startswith("pch") or "pmos" in normalized:
        return "pmos"
    return "nmos"


def _row_components(
    row_devices: Iterable[str],
    device_records: Mapping[str, NativeStdCellCarrierDevice],
) -> tuple[tuple[str, ...], ...]:
    row_device_set = {str(name) for name in row_devices}
    if not row_device_set:
        return ()
    net_to_devices: dict[str, set[str]] = defaultdict(set)
    for name in row_device_set:
        record = device_records[name]
        net_to_devices[record.source_net].add(name)
        net_to_devices[record.drain_net].add(name)
    remaining = set(row_device_set)
    components: list[tuple[str, ...]] = []
    while remaining:
        seed = min(remaining)
        queue = [seed]
        visited: set[str] = set()
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            record = device_records[current]
            for net in (record.source_net, record.drain_net):
                queue.extend(sorted(net_to_devices.get(net, ())))
        remaining -= visited
        components.append(tuple(sorted(visited)))
    return tuple(components)


def _classify_component(
    devices: tuple[str, ...],
    device_records: Mapping[str, NativeStdCellCarrierDevice],
) -> str:
    if len(devices) == 1:
        return "single"
    net_counts = _component_net_counts(devices, device_records)
    unique_nets = tuple(net_counts)
    common_nets = tuple(net for net, count in net_counts.items() if count == len(devices))
    if len(unique_nets) == 2 and len(common_nets) == 2:
        return "parallel"
    endpoint_nets = tuple(net for net, count in net_counts.items() if count == 1)
    internal_nets = tuple(net for net, count in net_counts.items() if count == 2)
    if (
        len(unique_nets) == len(devices) + 1
        and len(endpoint_nets) == 2
        and len(internal_nets) == len(devices) - 1
        and all(count <= 2 for count in net_counts.values())
    ):
        return "series"
    return "mixed"


def _component_net_counts(
    devices: tuple[str, ...],
    device_records: Mapping[str, NativeStdCellCarrierDevice],
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for name in devices:
        record = device_records[name]
        counts[record.source_net] += 1
        counts[record.drain_net] += 1
    return dict(counts)


def _ordered_component_devices(
    devices: tuple[str, ...],
    device_records: Mapping[str, NativeStdCellCarrierDevice],
    graph: TopologyGraph,
) -> tuple[str, ...]:
    kind = _classify_component(devices, device_records)
    if kind == "series":
        return _series_order(devices, device_records, graph)
    if kind == "parallel":
        return tuple(sorted(devices, key=lambda name: (_net_priority(graph, device_records[name].gate_net), device_records[name].gate_net, name)))
    return tuple(sorted(devices))


def _series_order(
    devices: tuple[str, ...],
    device_records: Mapping[str, NativeStdCellCarrierDevice],
    graph: TopologyGraph,
) -> tuple[str, ...]:
    net_counts = _component_net_counts(devices, device_records)
    endpoints = [net for net, count in net_counts.items() if count == 1]
    if len(endpoints) != 2:
        return tuple(sorted(devices))
    start_net = min(endpoints, key=lambda net: (_net_priority(graph, net), net))
    net_to_devices: dict[str, list[str]] = defaultdict(list)
    for name in devices:
        record = device_records[name]
        net_to_devices[record.source_net].append(name)
        net_to_devices[record.drain_net].append(name)
    ordered: list[str] = []
    used: set[str] = set()
    current_net = start_net
    while len(ordered) < len(devices):
        choices = [name for name in net_to_devices[current_net] if name not in used]
        if not choices:
            break
        name = min(choices, key=lambda item: (_net_priority(graph, device_records[item].gate_net), device_records[item].gate_net, item))
        ordered.append(name)
        used.add(name)
        record = device_records[name]
        current_net = record.drain_net if record.source_net == current_net else record.source_net
    if len(ordered) != len(devices):
        return tuple(sorted(devices))
    return tuple(ordered)


def _component_nets(
    kind: str,
    devices: tuple[str, ...],
    device_records: Mapping[str, NativeStdCellCarrierDevice],
    graph: TopologyGraph,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    net_counts = _component_net_counts(devices, device_records)
    if kind == "parallel":
        endpoints = tuple(sorted(net_counts, key=lambda net: (_net_priority(graph, net), net)))
        return endpoints, ()
    if kind == "series":
        endpoints = tuple(sorted((net for net, count in net_counts.items() if count == 1), key=lambda net: (_net_priority(graph, net), net)))
        internals = tuple(sorted((net for net, count in net_counts.items() if count == 2), key=lambda net: (_net_priority(graph, net), net)))
        return endpoints, internals
    if kind == "single":
        record = device_records[devices[0]]
        endpoints = tuple(sorted((record.source_net, record.drain_net), key=lambda net: (_net_priority(graph, net), net)))
        return endpoints, ()
    endpoints = tuple(sorted((net for net, count in net_counts.items() if count == 1), key=lambda net: (_net_priority(graph, net), net)))
    internals = tuple(sorted((net for net, count in net_counts.items() if count > 1 and net not in endpoints), key=lambda net: (_net_priority(graph, net), net)))
    return endpoints, internals


def _net_priority(graph: TopologyGraph, net: str) -> int:
    if net in graph.nets:
        role = graph.nets[net].role
    elif net in graph.pins:
        role = graph.pins[net]
    else:
        role = None
    if role == NetRole.SUPPLY:
        return 0
    if role == NetRole.GROUND:
        return 1
    if role == NetRole.OUTPUT:
        return 2
    if role == NetRole.INPUT:
        return 3
    if role == NetRole.INTERNAL:
        return 4
    return 5
