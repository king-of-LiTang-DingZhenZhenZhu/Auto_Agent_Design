"""Signal-flow floorplanning kernels for block-level analog layout."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence

from analogskills.contracts import AnalogFloorplanAdjacencyConstraint, AnalogFloorplanContract, AnalogFloorplanIntent, AnalogFloorplanPartitionConstraint, Device, DeviceRole, LayoutConstraintSet, NetRole, TopologyGraph
from analogskills.layout.constraints import extract_layout_constraints
from analogskills.layout.placement import Placement


@dataclass(frozen=True)
class BlockSpec:
    name: str
    width_um: float
    height_um: float
    role: str = ""


@dataclass(frozen=True)
class BlockPlacement:
    name: str
    x_um: float
    y_um: float
    width_um: float
    height_um: float
    role: str = ""

    @property
    def center(self) -> tuple[float, float]:
        return (self.x_um + self.width_um / 2, self.y_um + self.height_um / 2)


@dataclass(frozen=True)
class SignalEdge:
    source: str
    target: str
    weight: float = 1.0
    net: str = ""


@dataclass(frozen=True)
class FunctionalPartition:
    name: str
    role: str
    devices: tuple[str, ...]
    nets: tuple[str, ...]
    width_um: float
    height_um: float

    def to_block_spec(self) -> BlockSpec:
        return BlockSpec(self.name, self.width_um, self.height_um, self.role)


@dataclass(frozen=True)
class BusCorridor:
    name: str
    nets: tuple[str, ...]
    source: str
    target: str
    width_um: float
    layer: str = "M2"


@dataclass(frozen=True)
class LdoRoutingCorridor:
    name: str
    nets: tuple[str, ...]
    bbox_um: tuple[float, float, float, float]
    layer: str = "M2"
    role: str = ""
    forbidden_nets: tuple[str, ...] = ()
    waiver_nets: tuple[str, ...] = ()


@dataclass(frozen=True)
class HierarchicalRoutingCorridor:
    name: str
    nets: tuple[str, ...]
    bbox_um: tuple[float, float, float, float]
    layer: str = "M2"
    role: str = ""
    forbidden_nets: tuple[str, ...] = ()
    waiver_nets: tuple[str, ...] = ()
    status: str = "active"
    source: str = ""
    target: str = ""


@dataclass(frozen=True)
class LdoFloorplan:
    placements: tuple[BlockPlacement, ...]
    corridors: tuple[LdoRoutingCorridor, ...]
    forbidden_channels: tuple[LdoRoutingCorridor, ...] = ()
    partitions: tuple[FunctionalPartition, ...] = ()
    issues: tuple[str, ...] = ()

    @property
    def bbox_um(self) -> tuple[float, float, float, float]:
        return _bbox(self.placements)


@dataclass(frozen=True)
class SignalFlow:
    blocks: tuple[BlockSpec, ...]
    edges: tuple[SignalEdge, ...]
    partitions: tuple[FunctionalPartition, ...] = ()
    feedback_edges: tuple[SignalEdge, ...] = ()
    bus_corridors: tuple[BusCorridor, ...] = ()

    def ordered_block_names(self) -> tuple[str, ...]:
        return tuple(_topological_order(tuple(block.name for block in self.blocks), self.edges))


@dataclass(frozen=True)
class GlobalPlacementSeed:
    floorplan: tuple[BlockPlacement, ...]
    placements: tuple[Placement, ...]
    partitions: tuple[FunctionalPartition, ...] = ()
    metadata: dict[str, object] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})


@dataclass(frozen=True)
class GlobalPlacementSeedCandidate:
    seed: GlobalPlacementSeed
    score: float
    costs: dict[str, float]
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalogFloorplanPlan:
    intent: AnalogFloorplanIntent
    contract: AnalogFloorplanContract
    signal_flow: SignalFlow
    seed: GlobalPlacementSeed
    constraints: LayoutConstraintSet


def partition_by_function(graph: TopologyGraph) -> tuple[FunctionalPartition, ...]:
    bandgap = _bandgap_partitions(graph)
    if bandgap is not None:
        return bandgap
    strongarm = _strongarm_partitions(graph)
    if strongarm is not None:
        return strongarm

    groups: dict[str, list[str]] = {}
    role_by_group: dict[str, str] = {}
    for device in graph.devices.values():
        role = _classify_device(device)
        name = _partition_name(role, device)
        groups.setdefault(name, []).append(device.name)
        role_by_group[name] = role

    partitions: list[FunctionalPartition] = []
    for name, devices in groups.items():
        role = role_by_group[name]
        partitions.append(
            FunctionalPartition(
                name=name,
                role=role,
                devices=tuple(sorted(devices)),
                nets=_nets_for_devices(graph, tuple(devices)),
                width_um=_partition_width_um(role, len(devices)),
                height_um=_partition_height_um(role, len(devices)),
            )
        )
    return tuple(sorted(partitions, key=lambda p: (_ROLE_ORDER.get(p.role, 100), p.name)))


def build_analog_floorplan_intent(
    graph: TopologyGraph,
    *,
    constraints=None,
    partitions: Sequence[FunctionalPartition] | None = None,
) -> AnalogFloorplanIntent:
    partition_rows = tuple(partitions or partition_by_function(graph))
    role_by_name = {partition.name: partition.role for partition in partition_rows}
    roles = {partition.role for partition in partition_rows}
    active_constraints = extract_layout_constraints(graph, base_constraints=constraints or graph.layout_constraints)
    motifs = _intent_motifs(graph, partition_rows, active_constraints)
    notes: list[str] = []

    if "regenerative_latch" in roles:
        family = "dynamic_comparator"
        skeleton = "comparator_latch"
        notes.append("cross-coupled latch detected")
    elif _is_folded_cascode_ota_topology(graph):
        family = "folded_cascode_ota"
        skeleton = "folded_cascode_ota"
        notes.append("folded-cascode OTA topology detected")
    elif _is_telescopic_ota_topology(graph):
        family = "telescopic_ota"
        skeleton = "telescopic_ota"
        notes.append("telescopic OTA topology detected")
    elif _is_two_stage_ota_topology(graph):
        family = "two_stage_ota"
        skeleton = "two_stage_ota"
        notes.append("two-stage OTA topology detected")
    elif _is_three_stage_ota_topology(graph):
        family = "three_stage_ota"
        skeleton = "three_stage_ota"
        notes.append("three-stage OTA topology detected")
    elif _is_pipeline_adc_system_partition_set(role_by_name):
        family = "pipeline_adc_system"
        skeleton = "pipeline_adc_system"
        notes.append("pipeline ADC system partition set detected")
    elif _is_mdac_stage_topology(graph):
        family = "mdac_stage"
        skeleton = "mdac_stage"
        notes.append("MDAC stage topology detected")
    elif _is_pipeline_adc_partition_set(role_by_name):
        family = "pipeline_adc_frontend"
        skeleton = "pipeline_adc_frontend"
        notes.append("pipeline ADC frontend partition set detected")
    elif _is_pll_partition_set(role_by_name):
        family = "pll_system"
        skeleton = "pll_system"
        notes.append("PLL control chain partition set detected")
    elif _is_sampler_topology(graph):
        family = "sampler"
        skeleton = "sampler"
        notes.append("sampler switch topology detected")
    elif _is_reference_buffer_topology(graph):
        family = "reference_buffer"
        skeleton = "reference_buffer"
        notes.append("reference buffer topology detected")
    elif _is_loop_filter_topology(graph):
        family = "loop_filter"
        skeleton = "loop_filter"
        notes.append("loop filter topology detected")
    elif _is_charge_pump_topology(graph):
        family = "charge_pump"
        skeleton = "charge_pump"
        notes.append("charge pump topology detected")
    elif _is_brokaw_bandgap_topology(graph):
        family = "bandgap_reference"
        skeleton = "bandgap_reference"
        notes.append("Brokaw-style bandgap topology detected")
    elif "input" in roles and ("gain" in roles or "pmos_load" in roles or "load" in roles):
        family = "differential_amplifier"
        skeleton = "differential_row"
        notes.append("differential gain path detected")
    elif any(device.role == DeviceRole.CURRENT_MIRROR for device in graph.devices.values()):
        family = "bias_network"
        skeleton = "mirror_fanout"
        notes.append("current-mirror bias motif detected")
    else:
        family = "generic_analog"
        skeleton = "signal_flow_chain"

    return AnalogFloorplanIntent(
        circuit_family=family,
        preferred_skeleton=skeleton,
        motifs=motifs,
        critical_nets=tuple(active_constraints.critical_nets),
        placement_priorities=_intent_priorities(skeleton),
        notes=tuple(notes),
    )


def build_analog_floorplan_contract(
    graph: TopologyGraph,
    *,
    constraints=None,
    partitions: Sequence[FunctionalPartition] | None = None,
    intent: AnalogFloorplanIntent | None = None,
) -> AnalogFloorplanContract:
    partition_rows = tuple(partitions or partition_by_function(graph))
    active_constraints = extract_layout_constraints(graph, base_constraints=constraints or graph.layout_constraints)
    resolved_intent = intent or build_analog_floorplan_intent(graph, constraints=active_constraints, partitions=partition_rows)
    preferred_order = _preferred_partition_order(partition_rows, resolved_intent.preferred_skeleton)
    anchor_partitions = _anchor_partitions(partition_rows, resolved_intent.preferred_skeleton)
    focus_partitions = _focus_partitions(partition_rows, resolved_intent.preferred_skeleton)
    partition_constraints = tuple(
        AnalogFloorplanPartitionConstraint(
            name=partition.name,
            role=partition.role,
            devices=partition.devices,
            nets=partition.nets,
            width_um=partition.width_um,
            height_um=partition.height_um,
            anchor=partition.name in anchor_partitions,
            focus=partition.name in focus_partitions,
            order_index=preferred_order.index(partition.name) if partition.name in preferred_order else None,
        )
        for partition in partition_rows
    )
    return AnalogFloorplanContract(
        intent=resolved_intent,
        partitions=partition_constraints,
        preferred_partition_order=preferred_order,
        anchor_partitions=anchor_partitions,
        focus_partitions=focus_partitions,
        adjacency=_adjacency_constraints(partition_rows, resolved_intent.preferred_skeleton),
        matched_groups=active_constraints.matched_groups,
        symmetry_groups=active_constraints.symmetry_groups,
        routing=active_constraints.routing,
        critical_nets=active_constraints.critical_nets,
        row_roles=_row_role_contract(resolved_intent.preferred_skeleton),
    )


def extract_signal_flow(
    graph: TopologyGraph,
    *,
    track_pitch_um: float = 0.4,
    guard_tracks: int = 2,
    partitions: Sequence[FunctionalPartition] | None = None,
    contract: AnalogFloorplanContract | None = None,
) -> SignalFlow:
    partitions = tuple(partitions or _contract_partitions(contract) or partition_by_function(graph))
    partition_by_device = {device: partition.name for partition in partitions for device in partition.devices}
    role_by_partition = {partition.name: partition.role for partition in partitions}
    blocks = [partition.to_block_spec() for partition in partitions if partition.role not in {"bias"}]

    has_input_pin = any(role in {NetRole.INPUT, NetRole.DIFFERENTIAL, NetRole.CLOCK} for role in graph.pins.values())
    has_output_pin = any(role == NetRole.OUTPUT for role in graph.pins.values())
    if has_input_pin:
        blocks.insert(0, BlockSpec("input", 2.0, 2.0, "io"))
    if has_output_pin:
        blocks.append(BlockSpec("output", 2.0, 2.0, "io"))

    net_edges: list[SignalEdge] = []
    for net_name in graph.nets:
        net_edges.extend(_net_signal_edges(graph, partition_by_device, net_name))
    semantic_edges = _semantic_signal_edges(tuple(block.name for block in blocks), role_by_partition)
    edges = _dedupe_edges((*net_edges, *semantic_edges))
    feedback_edges = _dedupe_edges(_feedback_edges(graph, partition_by_device, role_by_partition))
    corridors = reserve_bus_corridors(graph, partitions=partitions, track_pitch_um=track_pitch_um, guard_tracks=guard_tracks)
    return SignalFlow(tuple(blocks), edges, partitions, feedback_edges, corridors)


def plan_system_floorplan(
    flow: SignalFlow,
    *,
    row_height_um: float | None = None,
    channel_um: float = 5.0,
) -> tuple[BlockPlacement, ...]:
    corridor_width = max((corridor.width_um for corridor in flow.bus_corridors), default=0.0)
    return signal_flow_floorplan(flow.blocks, flow.edges, row_height_um=row_height_um, channel_um=max(channel_um, corridor_width))


def _contract_chain_floorplan(
    flow: SignalFlow,
    contract: AnalogFloorplanContract,
    *,
    channel_um: float,
) -> tuple[BlockPlacement, ...]:
    if contract.intent.preferred_skeleton not in {"pipeline_adc_system", "pll_system"}:
        return ()

    corridor_width = max((corridor.width_um for corridor in flow.bus_corridors), default=0.0)
    gap_um = max(channel_um, corridor_width)
    by_name = {block.name: block for block in flow.blocks}
    core_names = [name for name in contract.preferred_partition_order if name in by_name and by_name[name].role != "io"]
    for block in flow.blocks:
        if block.role != "io" and block.name not in core_names:
            core_names.append(block.name)
    if not core_names:
        return ()

    core_height = max(by_name[name].height_um for name in core_names)
    placements: list[BlockPlacement] = []
    x_cursor = 0.0
    for name in core_names:
        block = by_name[name]
        y = (core_height - block.height_um) / 2.0
        placements.append(BlockPlacement(name, x_cursor, y, block.width_um, block.height_um, block.role))
        x_cursor += block.width_um + gap_um
    total_width = x_cursor - gap_um

    input_block = by_name.get("input")
    if input_block is not None:
        placements.append(
            BlockPlacement(
                input_block.name,
                -input_block.width_um - gap_um,
                (core_height - input_block.height_um) / 2.0,
                input_block.width_um,
                input_block.height_um,
                input_block.role,
            )
        )
    output_block = by_name.get("output")
    if output_block is not None:
        placements.append(
            BlockPlacement(
                output_block.name,
                total_width + gap_um,
                (core_height - output_block.height_um) / 2.0,
                output_block.width_um,
                output_block.height_um,
                output_block.role,
            )
        )
    return tuple(placements)


def build_global_placement_seed(
    graph: TopologyGraph,
    *,
    pdk: object | None = None,
    row_height_um: float | None = None,
    channel_um: float = 5.0,
    preferred_partition_order: Sequence[str] | None = None,
    anchor_partitions: Sequence[str] | None = None,
    focus_partitions: Sequence[str] | None = None,
    architecture_contract: Mapping[str, object] | None = None,
    floorplan_contract: AnalogFloorplanContract | None = None,
) -> GlobalPlacementSeed:
    """Build a block-level floorplan and project it into device-level seed placements."""

    contract = floorplan_contract or build_analog_floorplan_contract(graph)
    preferred_partition_order = preferred_partition_order or contract.preferred_partition_order
    anchor_partitions = anchor_partitions or contract.anchor_partitions
    focus_partitions = focus_partitions or contract.focus_partitions
    flow = extract_signal_flow(graph, contract=contract)
    preferred_partition_order = _resolve_block_selectors(flow.blocks, preferred_partition_order)
    anchor_partitions = _resolve_block_selectors(flow.blocks, anchor_partitions)
    focus_partitions = _resolve_block_selectors(flow.blocks, focus_partitions)
    floorplan_flow = flow
    if preferred_partition_order or anchor_partitions or focus_partitions:
        floorplan_flow = _biased_signal_flow(
            flow,
            preferred_partition_order=preferred_partition_order,
            anchor_partitions=anchor_partitions,
            focus_partitions=focus_partitions,
        )
    floorplan = _contract_chain_floorplan(
        floorplan_flow,
        contract,
        channel_um=channel_um,
    ) or _contract_row_floorplan(
        floorplan_flow,
        contract,
        row_height_um=row_height_um,
        channel_um=channel_um,
    ) or plan_system_floorplan(floorplan_flow, row_height_um=row_height_um, channel_um=channel_um)
    by_block = {placement.name: placement for placement in floorplan}
    site = getattr(pdk, "placement_site", None)
    pitch = float(getattr(site, "device_pitch_um", 1.0) or 1.0)
    row_pitch = float(getattr(site, "row_pitch_um", 2.0) or 2.0)
    placements: list[Placement] = []
    for partition in flow.partitions:
        block = by_block.get(partition.name)
        if block is None or not partition.devices:
            continue
        ordered_devices = tuple(partition.devices)
        center_x = block.x_um + block.width_um / 2
        center_y = block.y_um + block.height_um / 2
        x_origin = center_x - ((len(ordered_devices) - 1) * pitch) / 2
        y_target = center_y
        if partition.role in {"pmos_load", "load", "reset"}:
            y_target = center_y + 0.25 * row_pitch
        elif partition.role in {"bias", "tail", "passive", "feedback", "feedback_divider"}:
            y_target = center_y - 0.25 * row_pitch
        for idx, device_name in enumerate(ordered_devices):
            orient = "R0"
            role = device_name
            if device_name in graph.devices:
                dev_role = graph.devices[device_name].role
                if dev_role in {DeviceRole.LOAD, DeviceRole.PASS_TRANSISTOR}:
                    orient = "MY"
            placements.append(Placement(device_name, x_origin + idx * pitch, y_target, orient=orient, role=role))
    return GlobalPlacementSeed(
        floorplan=tuple(floorplan),
        placements=tuple(placements),
        partitions=flow.partitions,
        metadata={
            "topology_name": graph.name,
            "circuit_family": contract.intent.circuit_family,
            "preferred_skeleton": contract.intent.preferred_skeleton,
            "seed_strategy": (
                "chain_template_projection"
                if contract.intent.preferred_skeleton in {"pipeline_adc_system", "pll_system"}
                else "row_template_projection"
                if contract.row_roles
                else "floorplan_projection"
            ),
            "uses_deterministic_template": contract.intent.preferred_skeleton != "signal_flow_chain",
            "flow_block_count": len(flow.blocks),
            "partition_count": len(flow.partitions),
            "bus_corridor_count": len(flow.bus_corridors),
            "channel_um": float(channel_um),
            "row_height_um": float(row_height_um) if row_height_um is not None else None,
            "preferred_partition_order": tuple(str(name) for name in (preferred_partition_order or ())),
            "anchor_partitions": tuple(str(name) for name in (anchor_partitions or ())),
            "focus_partitions": tuple(str(name) for name in (focus_partitions or ())),
            "motifs": tuple(contract.intent.motifs),
            "placement_priorities": tuple(contract.intent.placement_priorities),
            "architecture_contract": {
                "system_type": str(dict(architecture_contract or {}).get("system_type", "")),
                "architecture_kind": str(dict(architecture_contract or {}).get("architecture_kind", "")),
                "stage_order": tuple(str(name) for name in tuple(dict(architecture_contract or {}).get("stage_order", ()) or ()) if str(name)),
                "focus_partitions": tuple(str(name) for name in tuple(dict(architecture_contract or {}).get("focus_partitions", ()) or ()) if str(name)),
                "keep_stable_partitions": tuple(str(name) for name in tuple(dict(architecture_contract or {}).get("keep_stable_partitions", ()) or ()) if str(name)),
                "retarget_first_partitions": tuple(str(name) for name in tuple(dict(architecture_contract or {}).get("retarget_first_partitions", ()) or ()) if str(name)),
                "critical_nets": tuple(str(name) for name in tuple(dict(architecture_contract or {}).get("critical_nets", ()) or ()) if str(name)),
                "partition_budgets": tuple(
                    dict(item)
                    for item in tuple(dict(architecture_contract or {}).get("partition_budgets", ()) or ())
                    if isinstance(item, Mapping)
                ),
            } if architecture_contract else {},
            "pdk_binding_coverage": _seed_pdk_binding_coverage(graph, architecture_contract),
            "bus_corridors": tuple(
                {
                    "name": corridor.name,
                    "nets": tuple(corridor.nets),
                    "source": corridor.source,
                    "target": corridor.target,
                    "layer": corridor.layer,
                    "status": "active",
                }
                for corridor in flow.bus_corridors
            ),
            "feedback_loops": tuple(
                {
                    "net": edge.net,
                    "source": edge.source,
                    "target": edge.target,
                    "status": "active",
                }
                for edge in flow.feedback_edges
                if edge.net
            ),
        },
    )


def plan_analog_floorplan(
    graph: TopologyGraph,
    *,
    constraints: LayoutConstraintSet | None = None,
    pdk: object | None = None,
    row_height_um: float | None = None,
    channel_um: float = 5.0,
    intent: AnalogFloorplanIntent | None = None,
    contract: AnalogFloorplanContract | None = None,
) -> AnalogFloorplanPlan:
    active_constraints = extract_layout_constraints(graph, base_constraints=constraints or graph.layout_constraints)
    resolved_contract = contract or build_analog_floorplan_contract(graph, constraints=active_constraints, intent=intent)
    flow = extract_signal_flow(graph, contract=resolved_contract)
    seed = build_global_placement_seed(
        graph,
        pdk=pdk,
        row_height_um=row_height_um,
        channel_um=channel_um,
        floorplan_contract=resolved_contract,
    )
    return AnalogFloorplanPlan(
        intent=resolved_contract.intent,
        contract=resolved_contract,
        signal_flow=flow,
        seed=seed,
        constraints=active_constraints,
    )


def build_hierarchical_routing_corridors(
    floorplan_blocks: Sequence[Mapping[str, object]],
    bus_corridors: Sequence[Mapping[str, object]] = (),
    feedback_loops: Sequence[Mapping[str, object]] = (),
    *,
    default_layer: str = "M2",
    feedback_layer: str = "M3",
    margin_um: float = 1.0,
    channel_height_um: float = 1.2,
    hierarchy_binding: Mapping[str, object] | None = None,
    hierarchy_parasitics: Mapping[str, object] | None = None,
) -> tuple[HierarchicalRoutingCorridor, ...]:
    """Lower system-level floorplan planning artifacts into concrete routing corridors."""

    by_name = {str(block.get("name", "")): dict(block) for block in floorplan_blocks if str(block.get("name", ""))}
    binding = dict(hierarchy_binding or {})
    parasitics = dict(hierarchy_parasitics or {})
    blocked_partitions = {str(name) for name in tuple(binding.get("blocked_partitions", ())) if str(name)}
    macro_binding_partitions = {str(name) for name in tuple(binding.get("macro_binding_partitions", ())) if str(name)}
    pcell_binding_partitions = {str(name) for name in tuple(binding.get("pcell_binding_partitions", ())) if str(name)}
    architecture_critical_nets = {
        str(net)
        for partition in tuple(parasitics.get("partitions", ()) or ())
        if isinstance(partition, Mapping)
        and str(
            dict(partition.get("architecture_budget", {}) or {}).get(
                "sensitivity",
                dict(partition.get("architecture_budget", {}) or {}).get("sensitivity_class", ""),
            )
            or ""
        ) in {"reference_critical", "timing_critical", "feedback_critical"}
        for net in (
            tuple(partition.get("critical_nets", ()) or ())
            + tuple(partition.get("reference_nets", ()) or ())
            + tuple(partition.get("feedback_nets", ()) or ())
            + tuple(partition.get("routing_anchor_nets", ()) or ())
        )
        if str(net)
    }
    corridors: list[HierarchicalRoutingCorridor] = []
    for corridor in bus_corridors:
        row = dict(corridor)
        name = str(row.get("name", ""))
        nets = tuple(str(net) for net in row.get("nets", ()) if str(net))
        source = str(row.get("source", ""))
        target = str(row.get("target", ""))
        if not name or not nets or source not in by_name or target not in by_name:
            continue
        bbox = _corridor_bbox_from_blocks(
            by_name[source],
            by_name[target],
            margin_um=margin_um,
            channel_height_um=max(channel_height_um, 0.4 * len(nets)),
        )
        role = "restore_bus" if str(row.get("status", "")) == "restore" else "bus"
        forbidden = tuple(str(net) for net in row.get("forbidden_nets", ()) if str(net))
        status = str(row.get("status", "active") or "active")
        if source in blocked_partitions or target in blocked_partitions:
            status = "binding_blocked"
        elif source in macro_binding_partitions or target in macro_binding_partitions:
            status = "macro_bound"
        elif source in pcell_binding_partitions or target in pcell_binding_partitions:
            status = "pcell_bound"
        waiver_nets = tuple(dict.fromkeys((*nets, *(net for net in nets if net in architecture_critical_nets))))
        corridors.append(
            HierarchicalRoutingCorridor(
                name=name,
                nets=nets,
                bbox_um=bbox,
                layer=str(row.get("layer", default_layer) or default_layer),
                role=role,
                forbidden_nets=forbidden,
                waiver_nets=waiver_nets,
                status=status,
                source=source,
                target=target,
            )
        )
    for loop in feedback_loops:
        row = dict(loop)
        net = str(row.get("net", ""))
        source = str(row.get("source", ""))
        target = str(row.get("target", ""))
        if not net or source not in by_name or target not in by_name:
            continue
        bbox = _corridor_bbox_from_blocks(
            by_name[source],
            by_name[target],
            margin_um=margin_um,
            channel_height_um=max(channel_height_um * 0.8, 0.8),
        )
        role = "restore_feedback" if str(row.get("status", "")) == "restore" else "feedback"
        status = str(row.get("status", "active") or "active")
        if source in blocked_partitions or target in blocked_partitions:
            status = "binding_blocked"
        elif source in macro_binding_partitions or target in macro_binding_partitions:
            status = "macro_bound"
        elif source in pcell_binding_partitions or target in pcell_binding_partitions:
            status = "pcell_bound"
        corridors.append(
            HierarchicalRoutingCorridor(
                name=f"{net}_FEEDBACK",
                nets=(net,),
                bbox_um=bbox,
                layer=str(row.get("layer", feedback_layer) or feedback_layer),
                role=role,
                forbidden_nets=tuple(str(item) for item in row.get("forbidden_nets", ()) if str(item)),
                waiver_nets=((net,) if net not in architecture_critical_nets else (net,)),
                status=status,
                source=source,
                target=target,
            )
        )
    return tuple(corridors)


def _seed_pdk_binding_coverage(
    graph: TopologyGraph,
    architecture_contract: Mapping[str, object] | None,
) -> dict[str, object]:
    contract = dict(architecture_contract or {})
    focus = tuple(str(name) for name in tuple(contract.get("focus_partitions", ()) or ()) if str(name))
    keep_stable = tuple(str(name) for name in tuple(contract.get("keep_stable_partitions", ()) or ()) if str(name))
    retarget = tuple(str(name) for name in tuple(contract.get("retarget_first_partitions", ()) or ()) if str(name))
    budgets = tuple(
        dict(item)
        for item in tuple(contract.get("partition_budgets", ()) or ())
        if isinstance(item, Mapping)
    )
    architecture_roles = tuple(dict.fromkeys(str(item.get("role", "")) for item in budgets if str(item.get("role", ""))))
    return {
        "topology_name": str(graph.name),
        "focus_partitions": focus,
        "keep_stable_partitions": keep_stable,
        "retarget_first_partitions": retarget,
        "architecture_budget_partition_count": len(budgets),
        "architecture_budget_roles": architecture_roles,
    }


def rank_global_placement_seeds(
    seeds: Sequence[GlobalPlacementSeed],
    *,
    score_weights: Mapping[str, float] | None = None,
) -> tuple[GlobalPlacementSeedCandidate, ...]:
    """Rank global placement seeds before detailed placement/routing expansion."""

    from analogskills.analysis import analyze_hierarchical_floorplan_plan

    weights = {
        "hpwl_um": 0.05,
        "aspect_error": 0.5,
        "partition_order_violations": 4.0,
        "anchor_partition_spread": 0.1,
        "focus_partition_separation": 0.1,
        "architecture_focus_spread": 0.2,
        "architecture_retarget_spread": 0.2,
        "architecture_critical_partition_spread": 0.25,
        "binding_blocked_partition_spread": 0.35,
        "macro_bound_partition_spread": 0.15,
        "restore_bus_gap": 2.0,
        "restore_feedback_gap": 2.0,
        "issues": 1.0,
    }
    if score_weights:
        weights.update({str(key): float(value) for key, value in score_weights.items()})

    rows: list[GlobalPlacementSeedCandidate] = []
    for seed in seeds:
        quality = analyze_hierarchical_floorplan_plan(_seed_to_floorplan_plan_dict(seed))
        metrics = dict(quality.metrics)
        costs = {
            "hpwl_um": float(metrics.get("hpwl_um", 0.0)),
            "aspect_error": abs(float(metrics.get("aspect_ratio", 0.0)) - 1.0),
            "partition_order_violations": float(metrics.get("partition_order_violations", 0.0)),
            "anchor_partition_spread": float(metrics.get("anchor_partition_spread", 0.0)),
            "focus_partition_separation": float(metrics.get("focus_partition_separation", 0.0)),
            **_architecture_seed_costs(seed),
            "restore_bus_gap": float(metrics.get("restore_bus_corridor_total", 0)) - float(metrics.get("restore_bus_corridor_ready", 0)),
            "restore_feedback_gap": float(metrics.get("restore_feedback_loop_total", 0)) - float(metrics.get("restore_feedback_loop_ready", 0)),
            "issues": float(len(tuple(quality.issues))),
        }
        score = sum(weights.get(name, 0.0) * value for name, value in costs.items())
        rows.append(GlobalPlacementSeedCandidate(seed, score, costs, tuple(quality.issues)))
    return tuple(sorted(rows, key=lambda row: (row.score, len(row.issues), len(row.seed.floorplan))))


def _biased_signal_flow(
    flow: SignalFlow,
    *,
    preferred_partition_order: Sequence[str] | None = None,
    anchor_partitions: Sequence[str] | None = None,
    focus_partitions: Sequence[str] | None = None,
) -> SignalFlow:
    preferred = tuple(str(name) for name in (preferred_partition_order or ()) if str(name))
    anchors = tuple(str(name) for name in (anchor_partitions or ()) if str(name))
    focus = tuple(str(name) for name in (focus_partitions or ()) if str(name))
    if not preferred and not anchors and not focus:
        return flow
    block_map = {block.name: block for block in flow.blocks}
    block_names = tuple(block.name for block in flow.blocks)
    preferred_existing = tuple(name for name in preferred if name in block_map)
    anchor_existing = tuple(name for name in anchors if name in block_map)
    focus_existing = tuple(name for name in focus if name in block_map)
    middle = tuple(name for name in preferred_existing if name in focus_existing) or focus_existing
    left = tuple(name for name in preferred_existing if name in anchor_existing and name not in middle)
    ordered = [*left, *middle]
    for name in preferred_existing:
        if name not in ordered:
            ordered.append(name)
    for name in block_names:
        if name not in ordered:
            ordered.append(name)
    weighted_edges = list(flow.edges)
    for idx, left_name in enumerate(ordered):
        for right_name in ordered[idx + 1 :]:
            if left_name == right_name:
                continue
            weighted_edges.append(SignalEdge(left_name, right_name, 0.05, "hierarchical_order_bias"))
            break
    reordered_blocks = tuple(block_map[name] for name in ordered if name in block_map)
    return SignalFlow(reordered_blocks, tuple(weighted_edges), flow.partitions, flow.feedback_edges, flow.bus_corridors)


def _resolve_block_selectors(
    blocks: Sequence[BlockSpec],
    selectors: Sequence[str] | None,
) -> tuple[str, ...]:
    if not selectors:
        return ()
    by_name = {block.name: block for block in blocks}
    by_role: dict[str, list[str]] = {}
    for block in blocks:
        by_role.setdefault(block.role, []).append(block.name)
    for names in by_role.values():
        names.sort(key=_natural_name_key)
    resolved: list[str] = []
    for raw_selector in selectors:
        selector = str(raw_selector)
        if not selector:
            continue
        if selector in by_name:
            resolved.append(selector)
            continue
        if selector in by_role:
            resolved.extend(by_role[selector])
            continue
        resolved.extend(
            name
            for name in sorted(by_name, key=_natural_name_key)
            if name == selector or name.startswith(f"{selector}_")
        )
    return tuple(dict.fromkeys(name for name in resolved if name in by_name))


def reserve_bus_corridors(
    graph: TopologyGraph,
    *,
    partitions: Sequence[FunctionalPartition] | None = None,
    track_pitch_um: float = 0.4,
    guard_tracks: int = 2,
    layer: str = "M2",
) -> tuple[BusCorridor, ...]:
    partitions = tuple(partitions or partition_by_function(graph))
    partition_by_device = {device: partition.name for partition in partitions for device in partition.devices}
    role_by_partition = {partition.name: partition.role for partition in partitions}

    corridors: list[BusCorridor] = list(
        _system_bus_corridors(
            graph,
            partition_by_device,
            role_by_partition,
            track_pitch_um=track_pitch_um,
            guard_tracks=guard_tracks,
        )
    )
    bus_groups: list[tuple[str, tuple[str, ...]]] = []
    for constraint in graph.layout_constraints.routing:
        if constraint.kind == "bus_order":
            nets = _tuple_value(constraint.value)
            if nets:
                bus_groups.append((constraint.net, nets))
    if not bus_groups:
        nets = tuple(sorted(net.name for net in graph.nets.values() if net.role == NetRole.BUS))
        if nets:
            bus_groups.append(("bus", nets))

    for name, nets in bus_groups:
        if any(existing.name == name for existing in corridors):
            continue
        endpoints = _endpoint_partitions_for_nets(graph, partition_by_device, nets)
        source = _preferred_endpoint(endpoints, role_by_partition, ("logic", "input", "sampler"), default="logic")
        target = _preferred_endpoint(endpoints, role_by_partition, ("cdac", "comparator", "gain", "output"), default="bus_target", exclude=source)
        width = max(track_pitch_um, (len(nets) + guard_tracks) * track_pitch_um)
        corridors.append(BusCorridor(name, nets, source, target, width, layer))
    return tuple(corridors)


def _system_bus_corridors(
    graph: TopologyGraph,
    partition_by_device: Mapping[str, str],
    role_by_partition: Mapping[str, str],
    *,
    track_pitch_um: float,
    guard_tracks: int,
) -> tuple[BusCorridor, ...]:
    corridors: list[BusCorridor] = []
    roles = set(role_by_partition.values())
    net_names = set(graph.nets)
    by_role = _partition_names_by_role_map(role_by_partition)

    def add_corridor(
        name: str,
        nets: tuple[str, ...],
        *,
        source: str,
        target: str,
        layer: str,
    ) -> None:
        active_nets = tuple(net for net in nets if net in net_names)
        if not active_nets:
            return
        width = max(track_pitch_um, (len(active_nets) + guard_tracks) * track_pitch_um)
        corridors.append(BusCorridor(name, active_nets, source, target, width, layer))

    def add_endpoint_corridor(
        name: str,
        nets: tuple[str, ...],
        *,
        layer: str,
        source_roles: Sequence[str],
        target_roles: Sequence[str],
    ) -> None:
        active_nets = tuple(net for net in nets if net in net_names)
        if not active_nets:
            return
        endpoints = _endpoint_partitions_for_nets(graph, dict(partition_by_device), active_nets)
        source = _preferred_endpoint(endpoints, dict(role_by_partition), tuple(source_roles), default="")
        target = _preferred_endpoint(
            endpoints,
            dict(role_by_partition),
            tuple(target_roles),
            default="",
            exclude=source or None,
        )
        if source and target:
            add_corridor(name, active_nets, source=source, target=target, layer=layer)

    if {"sampler", "mdac_stage", "subadc_flash", "reference_buffer", "logic"} <= roles:
        add_endpoint_corridor("RES1_CORRIDOR", ("RES1_P", "RES1_N"), layer="M3", source_roles=("mdac_stage",), target_roles=("mdac_stage",))
        add_endpoint_corridor("RES2_CORRIDOR", ("RES2_P", "RES2_N"), layer="M3", source_roles=("mdac_stage",), target_roles=("subadc_flash",))
        mdac_targets = tuple(by_role.get("mdac_stage", ()))
        for idx, target in enumerate(mdac_targets):
            suffix = "" if idx == 0 else f"_{idx + 1}"
            add_corridor(f"REFBUF_P_CORRIDOR{suffix}", ("VREFP_BUF",), source="reference_buffer", target=target, layer="M4")
            add_corridor(f"REFBUF_N_CORRIDOR{suffix}", ("VREFN_BUF",), source="reference_buffer", target=target, layer="M4")
    elif {"pfd", "charge_pump", "loop_filter", "vco", "divider"} <= roles:
        divider_chain = tuple(by_role.get("divider", ()))
        ctrl_target = divider_chain[-1] if divider_chain else "divider"
        fb_source = divider_chain[0] if divider_chain else "divider"
        add_corridor("PLL_CTRL_CHAIN", ("PFD_OUT", "CP_OUT", "VTUNE", "VCO_CLK"), source="pfd", target=ctrl_target, layer="M3")
        add_corridor("PLL_FEEDBACK_CORRIDOR", ("FB",), source=fb_source, target="pfd", layer="M4")
        add_corridor("PLL_REF_CORRIDOR", ("REF",), source="input", target="pfd", layer="M2")
        add_corridor("PLL_OUT_CORRIDOR", ("CLKOUT",), source=ctrl_target, target="output", layer="M4")

    return tuple(corridors)


def _corridor_bbox_from_blocks(
    source_block: Mapping[str, object],
    target_block: Mapping[str, object],
    *,
    margin_um: float,
    channel_height_um: float,
) -> tuple[float, float, float, float]:
    sx0, sy0, sx1, sy1 = _bbox_from_block_dict(source_block)
    tx0, ty0, tx1, ty1 = _bbox_from_block_dict(target_block)
    source_cy = (sy0 + sy1) / 2
    target_cy = (ty0 + ty1) / 2
    y_center = (source_cy + target_cy) / 2
    x0 = min(sx1, tx1) if sx1 <= tx1 else min(tx1, sx1)
    x1 = max(tx0, sx0) if sx1 <= tx1 else max(sx0, tx0)
    if x1 <= x0:
        x0 = min(sx0, tx0)
        x1 = max(sx1, tx1)
    x0 -= max(margin_um, 0.0)
    x1 += max(margin_um, 0.0)
    half_h = max(channel_height_um, 0.2) / 2
    return (x0, y_center - half_h, x1, y_center + half_h)


def _seed_to_floorplan_plan_dict(seed: GlobalPlacementSeed) -> dict[str, object]:
    floorplan_blocks = tuple(
        {
            "name": block.name,
            "role": block.role,
            "bbox_um": (
                float(block.x_um),
                float(block.y_um),
                float(block.x_um + block.width_um),
                float(block.y_um + block.height_um),
            ),
            "center_um": (float(block.center[0]), float(block.center[1])),
            "width_um": float(block.width_um),
            "height_um": float(block.height_um),
        }
        for block in seed.floorplan
    )
    metadata = dict(seed.metadata)
    return {
        "topology_name": str(metadata.get("topology_name", "")),
        "preferred_partition_order": tuple(str(name) for name in metadata.get("preferred_partition_order", ()) if str(name)),
        "anchor_partitions": tuple(str(name) for name in metadata.get("anchor_partitions", ()) if str(name)),
        "focus_partitions": tuple(str(name) for name in metadata.get("focus_partitions", ()) if str(name)),
        "bus_corridors": tuple(metadata.get("bus_corridors", ())),
        "feedback_loops": tuple(metadata.get("feedback_loops", ())),
        "floorplan_blocks": floorplan_blocks,
    }


def _architecture_seed_costs(seed: GlobalPlacementSeed) -> dict[str, float]:
    metadata = dict(getattr(seed, "metadata", {}) or {})
    architecture = dict(metadata.get("architecture_contract", {}) or {})
    if not architecture:
        return {
            "architecture_focus_spread": 0.0,
            "architecture_retarget_spread": 0.0,
            "architecture_critical_partition_spread": 0.0,
            "binding_blocked_partition_spread": 0.0,
            "macro_bound_partition_spread": 0.0,
        }
    centers = {
        str(block.name): tuple(block.center)
        for block in tuple(getattr(seed, "floorplan", ()))
    }
    focus = tuple(str(name) for name in tuple(architecture.get("focus_partitions", ()) or ()) if str(name))
    retarget = tuple(str(name) for name in tuple(architecture.get("retarget_first_partitions", ()) or ()) if str(name))
    critical = tuple(
        str(item.get("name", ""))
        for item in tuple(architecture.get("partition_budgets", ()) or ())
        if isinstance(item, Mapping)
        and str(item.get("name", ""))
        and str(item.get("sensitivity", "")) in {"reference_critical", "timing_critical", "feedback_critical"}
    )
    binding_coverage = dict(metadata.get("pdk_binding_coverage", {}) or {})
    blocked = tuple(str(name) for name in tuple(binding_coverage.get("binding_blocked_partitions", ()) or ()) if str(name))
    macro_bound = tuple(str(name) for name in tuple(binding_coverage.get("macro_bound_partitions", ()) or ()) if str(name))
    return {
        "architecture_focus_spread": _architecture_partition_spread(centers, focus),
        "architecture_retarget_spread": _architecture_partition_spread(centers, retarget),
        "architecture_critical_partition_spread": _architecture_partition_spread(centers, critical),
        "binding_blocked_partition_spread": _architecture_partition_spread(centers, blocked),
        "macro_bound_partition_spread": _architecture_partition_spread(centers, macro_bound),
    }


def _architecture_partition_spread(
    centers: Mapping[str, tuple[float, float]],
    names: Sequence[str],
) -> float:
    available = tuple(name for name in names if name in centers)
    if len(available) < 2:
        return 0.0
    xs = [float(centers[name][0]) for name in available]
    ys = [float(centers[name][1]) for name in available]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def _bbox_from_block_dict(block: Mapping[str, object]) -> tuple[float, float, float, float]:
    bbox = block.get("bbox_um", (0.0, 0.0, 0.0, 0.0))
    if isinstance(bbox, Sequence) and len(bbox) == 4:
        return tuple(float(value) for value in bbox)  # type: ignore[return-value]
    x = float(block.get("x_um", 0.0) or 0.0)
    y = float(block.get("y_um", 0.0) or 0.0)
    width = float(block.get("width_um", 0.0) or 0.0)
    height = float(block.get("height_um", 0.0) or 0.0)
    return (x, y, x + width, y + height)


def signal_flow_floorplan(
    blocks: Sequence[BlockSpec],
    edges: Sequence[SignalEdge],
    *,
    row_height_um: float | None = None,
    channel_um: float = 5.0,
) -> tuple[BlockPlacement, ...]:
    if not blocks:
        return ()
    by_name = {block.name: block for block in blocks}
    order = _topological_order(tuple(block.name for block in blocks), edges)
    row_height = row_height_um or max(block.height_um for block in blocks)
    x = 0.0
    placements: list[BlockPlacement] = []
    for name in order:
        block = by_name[name]
        y = (row_height - block.height_um) / 2
        placements.append(BlockPlacement(block.name, x, y, block.width_um, block.height_um, block.role))
        x += block.width_um + channel_um
    return tuple(placements)


_ROW_BAND_ORDER = {
    "bottom": 0,
    "lower_mid": 1,
    "shared": 2,
    "mid": 2,
    "upper_mid": 3,
    "top": 4,
}


def _contract_row_floorplan(
    flow: SignalFlow,
    contract: AnalogFloorplanContract,
    *,
    row_height_um: float | None,
    channel_um: float,
) -> tuple[BlockPlacement, ...]:
    if not contract.row_roles:
        return ()
    role_to_band = {str(role): str(band) for role, band in contract.row_roles if str(role) and str(band)}
    core_blocks = tuple(block for block in flow.blocks if block.role != "io")
    if not core_blocks:
        return ()

    order_index = {name: idx for idx, name in enumerate(contract.preferred_partition_order)}
    row_map: dict[str, list[BlockSpec]] = {}
    fallback_row = "mid"
    for block in core_blocks:
        band = role_to_band.get(block.role, fallback_row)
        row_map.setdefault(band, []).append(block)
    ordered_rows = sorted(
        row_map.items(),
        key=lambda item: (
            _ROW_BAND_ORDER.get(item[0], 50),
            min(order_index.get(block.name, 10_000) for block in item[1]),
            item[0],
        ),
    )

    row_specs: list[tuple[str, tuple[BlockSpec, ...], float, float]] = []
    max_width = 0.0
    for band, blocks in ordered_rows:
        ordered_blocks = tuple(sorted(blocks, key=lambda block: (order_index.get(block.name, 10_000), block.name)))
        row_width = sum(block.width_um for block in ordered_blocks) + max(len(ordered_blocks) - 1, 0) * channel_um
        row_height = row_height_um or max(block.height_um for block in ordered_blocks)
        row_specs.append((band, ordered_blocks, row_width, row_height))
        max_width = max(max_width, row_width)

    placements: list[BlockPlacement] = []
    y_cursor = 0.0
    total_height = 0.0
    for _, blocks, row_width, row_height in row_specs:
        x_cursor = max((max_width - row_width) / 2.0, 0.0)
        for block in blocks:
            y = y_cursor + (row_height - block.height_um) / 2.0
            placements.append(BlockPlacement(block.name, x_cursor, y, block.width_um, block.height_um, block.role))
            x_cursor += block.width_um + channel_um
        y_cursor += row_height + channel_um
        total_height = y_cursor - channel_um

    io_blocks = tuple(block for block in flow.blocks if block.role == "io")
    if not io_blocks:
        return tuple(placements)

    center_y = total_height / 2.0 if total_height > 0 else 0.0
    left_x = -max((block.width_um for block in io_blocks), default=0.0) - channel_um
    right_x = max_width + channel_um
    for block in io_blocks:
        x = left_x if block.name == "input" else right_x
        y = center_y - block.height_um / 2.0
        placements.append(BlockPlacement(block.name, x, y, block.width_um, block.height_um, block.role))
    return tuple(placements)


def analyze_floorplan(placements: Sequence[BlockPlacement], edges: Sequence[SignalEdge], *, aspect_ratio_target: float | None = None) -> dict[str, object]:
    by_name = {placement.name: placement for placement in placements}
    issues: list[str] = []
    hpwl = 0.0
    for edge in edges:
        if edge.source not in by_name or edge.target not in by_name:
            issues.append(f"missing block for edge {edge.source}->{edge.target}")
            continue
        sx, sy = by_name[edge.source].center
        tx, ty = by_name[edge.target].center
        hpwl += edge.weight * (abs(tx - sx) + abs(ty - sy))
        if tx < sx:
            issues.append(f"signal flow reversal {edge.source}->{edge.target}")
    bbox = _bbox(placements)
    aspect = (bbox[2] - bbox[0]) / max(bbox[3] - bbox[1], 1e-12) if placements else 0.0
    if aspect_ratio_target is not None and placements:
        if aspect > aspect_ratio_target * 1.5 or aspect < aspect_ratio_target / 1.5:
            issues.append(f"aspect ratio {aspect:.4g} outside target neighborhood {aspect_ratio_target:.4g}")
    return {"passed": not issues, "issues": issues, "hpwl_um": hpwl, "bbox_um": bbox, "aspect_ratio": aspect}


def plan_ldo_floorplan(
    graph: TopologyGraph,
    pdk: object | None = None,
    *,
    pass_device: str = "MPASS",
    channel_um: float = 1.2,
    body_tap_height_um: float = 0.8,
    quiet_channel_height_um: float = 0.8,
) -> LdoFloorplan:
    """Build a PMOS-pass LDO floorplan with explicit quiet and forbidden corridors."""

    partitions, partition_issues = _ldo_partitions(graph, pass_device=pass_device)
    dimensions = {
        "error_amp_input": (7.0, 3.0),
        "pmos_load": (7.0, 2.5),
        "pass_device": (5.5, 3.5),
        "feedback_divider": (3.4, 2.6),
        "output_cap": (3.2, 3.0),
    }
    by_name = {partition.name: partition for partition in partitions}
    body_tap_h = _snap_dimension_um(pdk, body_tap_height_um)
    quiet_h = _snap_dimension_um(pdk, quiet_channel_height_um)
    gap = _snap_dimension_um(pdk, channel_um)
    error_y = body_tap_h + quiet_h + 2.0 * gap
    load_y = error_y + dimensions["error_amp_input"][1] + gap
    pass_x = dimensions["error_amp_input"][0] + 2.0 * gap
    feedback_x = pass_x
    output_x = feedback_x + dimensions["feedback_divider"][0] + gap

    placements: list[BlockPlacement] = []
    if "error_amp_input" in by_name:
        width, height = dimensions["error_amp_input"]
        placements.append(BlockPlacement("error_amp_input", 0.0, error_y, width, height, "error_amp_input"))
    if "pmos_load" in by_name:
        width, height = dimensions["pmos_load"]
        placements.append(BlockPlacement("pmos_load", 0.0, load_y, width, height, "pmos_load"))
    if "pass_device" in by_name:
        width, height = dimensions["pass_device"]
        placements.append(BlockPlacement("pass_device", pass_x, load_y, width, height, "pass_device"))
    if "feedback_divider" in by_name:
        width, height = dimensions["feedback_divider"]
        placements.append(BlockPlacement("feedback_divider", feedback_x, error_y, width, height, "feedback_divider"))
    if "output_cap" in by_name:
        width, height = dimensions["output_cap"]
        placements.append(BlockPlacement("output_cap", output_x, error_y, width, height, "output_cap"))

    x1 = max((p.x_um + p.width_um for p in placements), default=0.0)
    top_y = max((p.y_um + p.height_um for p in placements), default=body_tap_h + quiet_h + gap)
    power_layer = _preferred_power_layer(pdk)
    quiet_layer = _upper_preferred_signal_layer(pdk)
    ground_layer = _lowest_metal_layer(pdk)
    quiet_y0 = body_tap_h + gap
    quiet_y1 = quiet_y0 + quiet_h
    gate_y0 = error_y + dimensions["error_amp_input"][1] + gap * 0.25
    gate_y1 = gate_y0 + quiet_h
    x_margin = gap
    corridors = (
        LdoRoutingCorridor("VIN_CORRIDOR", _existing_nets(graph, ("VIN",)), (pass_x - x_margin, top_y + gap * 0.25, x1, top_y + gap * 0.25 + quiet_h), power_layer, "wide_current"),
        LdoRoutingCorridor("VOUT_CORRIDOR", _existing_nets(graph, ("VOUT",)), (pass_x, error_y - gap * 0.75, x1, error_y - gap * 0.75 + quiet_h), power_layer, "wide_current"),
        LdoRoutingCorridor("QUIET_FB_CORRIDOR", _existing_nets(graph, ("VFB", "VREF")), (0.0, quiet_y0, x1, quiet_y1), quiet_layer, "quiet_signal", ("VSS", "VIN", "VOUT", "VGATE_PASS")),
        LdoRoutingCorridor("PASS_GATE_CORRIDOR", _existing_nets(graph, ("VGATE_PASS",)), (0.0, gate_y0, pass_x + dimensions["pass_device"][0], gate_y1), quiet_layer, "quiet_gate", ("VSS", "VIN", "VOUT", "VFB")),
        LdoRoutingCorridor("GROUND_TAP_CORRIDOR", _existing_nets(graph, ("VSS",)), (0.0, 0.0, x1, body_tap_h), ground_layer, "ground_tap", ("VFB", "VREF", "VGATE_PASS")),
    )
    forbidden_channels = tuple(corridor for corridor in corridors if corridor.forbidden_nets)
    return LdoFloorplan(tuple(placements), corridors, forbidden_channels, partitions, partition_issues)


def analyze_ldo_floorplan(
    floorplan: LdoFloorplan,
    *,
    shorted_nets: Sequence[tuple[str, str]] = (),
) -> dict[str, object]:
    issues = list(floorplan.issues)
    by_name = {placement.name: placement for placement in floorplan.placements}
    corridors = {corridor.name: corridor for corridor in floorplan.corridors}
    for name in ("error_amp_input", "pmos_load", "pass_device", "feedback_divider", "output_cap"):
        if name not in by_name:
            issues.append(f"missing LDO floorplan block {name}")
    for name in ("VIN_CORRIDOR", "VOUT_CORRIDOR", "QUIET_FB_CORRIDOR", "PASS_GATE_CORRIDOR", "GROUND_TAP_CORRIDOR"):
        if name not in corridors:
            issues.append(f"missing LDO routing corridor {name}")

    pass_error_y_overlap = 0.0
    pass_body_tap_y_overlap = 0.0
    pass_block = by_name.get("pass_device")
    input_block = by_name.get("error_amp_input")
    ground = corridors.get("GROUND_TAP_CORRIDOR")
    if pass_block is not None and input_block is not None:
        pass_error_y_overlap = _y_overlap_um(_placement_bbox(pass_block), _placement_bbox(input_block))
        if pass_error_y_overlap > 0.0:
            issues.append(f"pass_device overlaps error_amp_input y-channel by {pass_error_y_overlap:.4g} um")
    if pass_block is not None and ground is not None:
        pass_body_tap_y_overlap = _y_overlap_um(_placement_bbox(pass_block), ground.bbox_um)
        if pass_body_tap_y_overlap > 0.0:
            issues.append(f"pass_device overlaps GROUND_TAP_CORRIDOR by {pass_body_tap_y_overlap:.4g} um")

    corridor_violations: list[dict[str, object]] = []
    for corridor in floorplan.corridors:
        forbidden = set(corridor.forbidden_nets) - set(corridor.waiver_nets)
        present_forbidden = sorted(set(corridor.nets) & forbidden)
        for net in present_forbidden:
            message = f"forbidden net {net} assigned to {corridor.name}"
            issues.append(message)
            corridor_violations.append({"corridor": corridor.name, "net": net, "message": message})
    for left, right in shorted_nets:
        for corridor in floorplan.forbidden_channels:
            corridor_nets = set(corridor.nets)
            forbidden = set(corridor.forbidden_nets) - set(corridor.waiver_nets)
            if left in corridor_nets and right in forbidden:
                message = f"short {left}-{right} maps to {corridor.name} forbidden channel"
            elif right in corridor_nets and left in forbidden:
                message = f"short {left}-{right} maps to {corridor.name} forbidden channel"
            else:
                continue
            issues.append(message)
            corridor_violations.append({"corridor": corridor.name, "nets": (left, right), "message": message})

    return {
        "passed": not issues,
        "issues": issues,
        "bbox_um": floorplan.bbox_um,
        "pass_error_amp_y_overlap_um": pass_error_y_overlap,
        "pass_body_tap_y_overlap_um": pass_body_tap_y_overlap,
        "corridor_violations": tuple(corridor_violations),
    }


def _ldo_partitions(graph: TopologyGraph, *, pass_device: str) -> tuple[tuple[FunctionalPartition, ...], tuple[str, ...]]:
    issues: list[str] = []
    role_devices = {
        "error_amp_input": tuple(name for name in ("M1A", "M1B", "MTAIL") if name in graph.devices),
        "pmos_load": tuple(name for name in ("M3A", "M3B") if name in graph.devices),
        "pass_device": (pass_device,) if pass_device in graph.devices else (),
        "feedback_divider": tuple(name for name in ("RFB_TOP", "RFB_BOT") if name in graph.devices),
        "output_cap": tuple(name for name in ("COUT",) if name in graph.devices),
    }
    expected = {
        "error_amp_input": ("M1A", "M1B", "MTAIL"),
        "pmos_load": ("M3A", "M3B"),
        "pass_device": (pass_device,),
        "feedback_divider": ("RFB_TOP", "RFB_BOT"),
        "output_cap": ("COUT",),
    }
    for role, names in expected.items():
        missing = tuple(name for name in names if name not in graph.devices)
        if missing:
            issues.append(f"missing LDO {role} devices {missing}")

    partitions: list[FunctionalPartition] = []
    for role, devices in role_devices.items():
        if not devices:
            continue
        partitions.append(
            FunctionalPartition(
                name=role,
                role=role,
                devices=devices,
                nets=_nets_for_devices(graph, devices),
                width_um=_partition_width_um(role, len(devices)),
                height_um=_partition_height_um(role, len(devices)),
            )
        )
    pin_nets = tuple(sorted(pin for pin in graph.pins if pin in graph.nets))
    if pin_nets:
        partitions.append(FunctionalPartition("pins", "pins", tuple(sorted(graph.pins)), pin_nets, 2.0, 2.0))
    else:
        issues.append("missing LDO top-level pins")
    return tuple(partitions), tuple(issues)


def _existing_nets(graph: TopologyGraph, nets: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(net for net in nets if net in graph.nets)


def _placement_bbox(placement: BlockPlacement) -> tuple[float, float, float, float]:
    return (placement.x_um, placement.y_um, placement.x_um + placement.width_um, placement.y_um + placement.height_um)


def _y_overlap_um(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    return max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def _snap_dimension_um(pdk: object | None, value_um: float) -> float:
    if pdk is None:
        return float(value_um)
    snap_dimension = getattr(pdk, "snap_dimension_um", None)
    if callable(snap_dimension):
        return float(snap_dimension(float(value_um)))
    return float(value_um)


def _preferred_power_layer(pdk: object | None) -> str:
    if pdk is not None:
        layers = tuple(str(layer) for layer in getattr(pdk, "preferred_power_layers", ()))
        if layers:
            return layers[min(1, len(layers) - 1)]
    return "M3"


def _preferred_signal_layer(pdk: object | None) -> str:
    if pdk is not None:
        layers = tuple(str(layer) for layer in getattr(pdk, "preferred_signal_layers", ()))
        if layers:
            return layers[0]
    return "M2"


def _upper_preferred_signal_layer(pdk: object | None) -> str:
    if pdk is not None:
        layers = tuple(str(layer) for layer in getattr(pdk, "preferred_signal_layers", ()))
        if layers:
            return layers[min(1, len(layers) - 1)]
    return _preferred_signal_layer(pdk)


def _lowest_metal_layer(pdk: object | None) -> str:
    if pdk is not None:
        layer_map = getattr(pdk, "layer_map", None)
        metals = tuple(str(layer) for layer in getattr(layer_map, "metals", ()))
        if metals:
            return metals[0]
    return "M1"


def _topological_order(nodes: tuple[str, ...], edges: Sequence[SignalEdge]) -> list[str]:
    index = {name: idx for idx, name in enumerate(nodes)}
    outgoing: dict[str, list[str]] = {name: [] for name in nodes}
    indegree: dict[str, int] = {name: 0 for name in nodes}
    for edge in edges:
        if edge.source not in indegree or edge.target not in indegree:
            continue
        outgoing[edge.source].append(edge.target)
        indegree[edge.target] += 1
    ready = sorted((name for name, degree in indegree.items() if degree == 0), key=index.get)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for nxt in sorted(outgoing[node], key=index.get):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=index.get)
    if len(order) != len(nodes):
        return list(nodes)
    return order


def _bbox(placements: Sequence[BlockPlacement]) -> tuple[float, float, float, float]:
    if not placements:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(p.x_um for p in placements),
        min(p.y_um for p in placements),
        max(p.x_um + p.width_um for p in placements),
        max(p.y_um + p.height_um for p in placements),
    )


_ROLE_ORDER = {
    "io": -10,
    "input": 0,
    "tail": 1,
    "sampler": 1,
    "mdac_stage": 2,
    "cdac": 2,
    "subadc_flash": 3,
    "comparator": 3,
    "regenerative_latch": 4,
    "gain": 4,
    "load": 5,
    "reset": 5,
    "feedback": 6,
    "logic": 7,
    "reference_buffer": 8,
    "reference": 8,
    "pmos_mirror": 9,
    "error_amplifier": 10,
    "bjt_core": 11,
    "resistor_ladder": 12,
    "bias_tail": 13,
    "pfd": 10,
    "charge_pump": 11,
    "loop_filter": 12,
    "vco": 13,
    "divider": 14,
    "pmos_load": 20,
    "pass_device": 21,
    "feedback_divider": 22,
    "output_cap": 23,
    "error_amp_input": 24,
    "pins": 25,
    "bias": 50,
    "passive": 60,
    "unknown": 100,
}
_INPUT_TERMINALS = {"G", "PLUS", "IN", "INP", "INN", "CLK", "CTRL", "REF", "VIN", "TOP"}
_OUTPUT_TERMINALS = {"D", "MINUS", "OUT", "OUTP", "OUTN", "VOUT"}
_SIGNAL_NET_ROLES = {NetRole.INPUT, NetRole.OUTPUT, NetRole.HIGH_Z, NetRole.INTERNAL, NetRole.DIFFERENTIAL, NetRole.COMPENSATION, NetRole.CLOCK, NetRole.BUS}


def _classify_device(device: Device) -> str:
    if device.role == DeviceRole.PASS_TRANSISTOR:
        return "pass_device"
    if device.role == DeviceRole.FEEDBACK_RESISTOR:
        return "feedback_divider"
    if device.role == DeviceRole.COMP_CAPACITOR and str(device.parameters.get("role", "")).lower() == "output_cap":
        return "output_cap"
    if device.role in {DeviceRole.COMP_RESISTOR, DeviceRole.COMP_CAPACITOR}:
        return "feedback"

    text = _normalized_device_text(device)
    if _contains_any(text, ("cdac", "capdac", "capacitor dac")):
        return "cdac"
    if _contains_any(text, ("sampler", "sample hold", "sample")):
        return "sampler"
    if _contains_any(text, ("mdac", "pipeline stage", "residue amplifier")):
        return "mdac_stage"
    if _contains_any(text, ("flash", "subadc", "backend flash")):
        return "subadc_flash"
    if _contains_any(text, ("comparator", " cmp ")):
        return "comparator"
    if _contains_any(text, ("sar logic", "pipeline logic", "digital correction", "logic", "controller", "ctrl", "fsm")):
        return "logic"
    if _contains_any(text, ("reference buffer", "reference_buffer", "refbuf")):
        return "reference_buffer"
    if _contains_any(text, ("reference", "refbuf", "vref")):
        return "reference"
    if _contains_any(text, ("pfd", "phase frequency detector")):
        return "pfd"
    if _contains_any(text, ("charge pump", "chargepump", " cp ")):
        return "charge_pump"
    if _contains_any(text, ("loop filter", "filter")):
        return "loop_filter"
    if _contains_any(text, ("vco", "oscillator")):
        return "vco"
    if _contains_any(text, ("divider", " div ")):
        return "divider"

    if device.role == DeviceRole.INPUT_PAIR:
        return "input"
    if device.role in {DeviceRole.DRIVER, DeviceRole.CASCODE}:
        return "gain"
    if device.role == DeviceRole.LOAD:
        if "pmos" in text or "pch" in text:
            return "pmos_load"
        return "load"
    if device.role in {DeviceRole.CURRENT_MIRROR, DeviceRole.TAIL, DeviceRole.BIAS}:
        if device.role == DeviceRole.TAIL:
            return "tail"
        return "bias"
    if device.role == DeviceRole.PASSIVE:
        return "passive"
    return "unknown"


def _partition_name(role: str, device: Device) -> str:
    system_name = _system_partition_name(role, device)
    if system_name:
        return system_name
    if role == "input" and device.role == DeviceRole.INPUT_PAIR:
        return "input_pair"
    if role == "gain" and device.role == DeviceRole.DRIVER:
        return "gain_stage"
    return role


def _system_partition_name(role: str, device: Device) -> str:
    if role not in {
        "sampler",
        "mdac_stage",
        "subadc_flash",
        "logic",
        "reference_buffer",
        "pfd",
        "charge_pump",
        "loop_filter",
        "vco",
        "divider",
    }:
        return ""
    if device.role not in {DeviceRole.UNKNOWN, DeviceRole.PASSIVE}:
        return ""

    normalized = _normalized_device_text(device)
    digits = _first_numeric_suffix(device.name)
    if role == "mdac_stage":
        if digits:
            return f"mdac_stage_{digits}"
        return "mdac_stage"
    if role == "divider":
        if "post" in normalized:
            return "divider_post"
        if digits:
            return f"divider_{digits}"
        if normalized.startswith("div"):
            return "divider_fb"
    if role == "logic" and "lock" in normalized:
        return "logic_lock"
    return role


def _first_numeric_suffix(text: str) -> str:
    match = re.search(r"(\d+)", str(text))
    return match.group(1) if match else ""


def _nets_for_devices(graph: TopologyGraph, devices: tuple[str, ...]) -> tuple[str, ...]:
    device_set = set(devices)
    nets = [net.name for net in graph.nets.values() if any(term.device in device_set for term in net.terminals)]
    return tuple(sorted(nets))


def _partition_width_um(role: str, count: int) -> float:
    base = {
        "input": 4.4,
        "tail": 2.4,
        "sampler": 4.8,
        "mdac_stage": 7.5,
        "cdac": 7.5,
        "subadc_flash": 4.8,
        "comparator": 4.8,
        "regenerative_latch": 5.4,
        "gain": 5.2,
        "load": 4.2,
        "reset": 3.8,
        "feedback": 2.8,
        "logic": 5.8,
        "reference_buffer": 4.2,
        "reference": 3.6,
        "pmos_mirror": 4.8,
        "error_amplifier": 6.4,
        "bjt_core": 6.0,
        "resistor_ladder": 7.2,
        "bias_tail": 2.8,
        "pfd": 4.2,
        "charge_pump": 4.2,
        "loop_filter": 4.8,
        "vco": 5.2,
        "divider": 4.2,
        "pmos_load": 4.2,
        "pass_device": 3.8,
        "feedback_divider": 2.8,
        "output_cap": 2.8,
        "error_amp_input": 4.8,
        "pins": 2.0,
        "bias": 2.8,
    }.get(role, 3.2)
    return base + max(0, count - 1) * 1.0


def _partition_height_um(role: str, count: int) -> float:
    base = {
        "tail": 1.8,
        "mdac_stage": 4.2,
        "cdac": 4.8,
        "subadc_flash": 3.2,
        "regenerative_latch": 2.8,
        "logic": 3.2,
        "reference_buffer": 2.8,
        "feedback": 2.0,
        "reset": 2.2,
        "pmos_mirror": 2.4,
        "error_amplifier": 3.0,
        "bjt_core": 3.0,
        "resistor_ladder": 2.2,
        "bias_tail": 1.8,
        "feedback_divider": 2.0,
        "output_cap": 2.0,
        "bias": 1.8,
        "loop_filter": 2.8,
    }.get(role, 2.6)
    return base + max(0, count - 3) * 0.35


def _net_signal_edges(graph: TopologyGraph, partition_by_device: dict[str, str], net_name: str) -> tuple[SignalEdge, ...]:
    net = graph.nets[net_name]
    if net.role == NetRole.BUS:
        return ()
    if net.role not in _SIGNAL_NET_ROLES:
        return ()

    partitions = set(partition_by_device.values())
    drivers: set[str] = set()
    receivers: set[str] = set()
    for terminal in net.terminals:
        if terminal.device in graph.pins:
            pin_role = graph.pins[terminal.device]
            if pin_role in {NetRole.INPUT, NetRole.DIFFERENTIAL, NetRole.CLOCK}:
                drivers.add("input")
            elif pin_role == NetRole.OUTPUT:
                receivers.add("output")
            continue

        partition = partition_by_device.get(terminal.device)
        if partition is None:
            continue
        if _is_output_terminal(terminal.terminal):
            drivers.add(partition)
        if _is_input_terminal(terminal.terminal):
            receivers.add(partition)
        if net.role == NetRole.BUS:
            receivers.add(partition)

    if net.role in {NetRole.INPUT, NetRole.DIFFERENTIAL, NetRole.CLOCK}:
        drivers.add("input")
    if net.role == NetRole.OUTPUT:
        receivers.add("output")
    if net.role == NetRole.BUS:
        if "logic" in partitions:
            drivers.add("logic")
        elif "input" in partitions:
            drivers.add("input")

    weight = 2.0 if net.role in {NetRole.HIGH_Z, NetRole.OUTPUT, NetRole.BUS} else 1.0
    return tuple(SignalEdge(src, dst, weight, net_name) for src in sorted(drivers) for dst in sorted(receivers) if src != dst)


def _semantic_signal_edges(block_names: tuple[str, ...], role_by_partition: dict[str, str]) -> tuple[SignalEdge, ...]:
    by_role: dict[str, list[str]] = {}
    for name, role in role_by_partition.items():
        if name in block_names:
            by_role.setdefault(role, []).append(name)
    for names in by_role.values():
        names.sort(key=lambda name: block_names.index(name))

    edges: list[SignalEdge] = []
    if "input" in block_names:
        for role in ("input", "sampler", "pfd"):
            for name in by_role.get(role, ()):  # type: ignore[arg-type]
                edges.append(SignalEdge("input", name, 0.5, "semantic_input"))

    for chain in (
        ("sampler", "cdac", "comparator", "logic"),
        ("sampler", "mdac_stage", "subadc_flash", "logic"),
        ("pmos_mirror", "error_amplifier", "bjt_core", "resistor_ladder"),
        ("reference_buffer", "mdac_stage"),
        ("pfd", "charge_pump", "loop_filter", "vco", "divider"),
        ("input", "gain"),
        ("input", "regenerative_latch", "reset"),
    ):
        previous = None
        for role in chain:
            current_names = tuple(by_role.get(role, ()))
            if not current_names:
                continue
            if previous is not None:
                edges.append(SignalEdge(previous, current_names[0], 0.5, "semantic_order"))
            for left, right in zip(current_names, current_names[1:]):
                edges.append(SignalEdge(left, right, 0.5, "semantic_order"))
            previous = current_names[-1]

    if "output" in block_names:
        for role in ("gain", "regenerative_latch", "reset", "comparator", "vco", "logic"):
            for name in by_role.get(role, ()):  # type: ignore[arg-type]
                edges.append(SignalEdge(name, "output", 0.5, "semantic_output"))
    return tuple(edges)


def _feedback_edges(graph: TopologyGraph, partition_by_device: dict[str, str], role_by_partition: dict[str, str]) -> tuple[SignalEdge, ...]:
    edges: list[SignalEdge] = []
    by_role: dict[str, list[str]] = {}
    for name, role in role_by_partition.items():
        by_role.setdefault(role, []).append(name)
    if by_role.get("divider") and by_role.get("pfd"):
        edges.append(SignalEdge(by_role["divider"][0], by_role["pfd"][0], 0.2, "pll_feedback"))

    comp_adjacency: dict[str, set[str]] = {}
    for device in graph.devices.values():
        if device.role not in {DeviceRole.COMP_RESISTOR, DeviceRole.COMP_CAPACITOR}:
            continue
        nets = sorted(set(_nets_for_terminal_device(graph, device.name)))
        for idx, left in enumerate(nets):
            for right in nets[idx + 1 :]:
                comp_adjacency.setdefault(left, set()).add(right)
                comp_adjacency.setdefault(right, set()).add(left)

    output_nets = [net.name for net in graph.nets.values() if net.role == NetRole.OUTPUT]
    target_roles = ("gain", "input", "comparator", "cdac")
    for output_net in output_nets:
        for target_net in _reachable_compensation_targets(graph, comp_adjacency, output_net):
            endpoints = _endpoint_partitions_for_nets(graph, partition_by_device, (target_net,))
            target = _preferred_endpoint(endpoints, role_by_partition, target_roles, default="")
            if target:
                edges.append(SignalEdge("output", target, 0.2, f"{output_net}->{target_net}"))
    return tuple(edges)


def _reachable_compensation_targets(graph: TopologyGraph, adjacency: dict[str, set[str]], start: str) -> tuple[str, ...]:
    stack = list(adjacency.get(start, ()))
    seen = {start}
    targets: list[str] = []
    while stack:
        net_name = stack.pop()
        if net_name in seen:
            continue
        seen.add(net_name)
        role = graph.nets[net_name].role
        if role in {NetRole.HIGH_Z, NetRole.INTERNAL, NetRole.COMPENSATION}:
            targets.append(net_name)
        stack.extend(sorted(adjacency.get(net_name, ())))
    return tuple(targets)


def _nets_for_terminal_device(graph: TopologyGraph, device_name: str) -> tuple[str, ...]:
    nets = []
    for net in graph.nets.values():
        if any(terminal.device == device_name for terminal in net.terminals):
            nets.append(net.name)
    return tuple(nets)


def _endpoint_partitions_for_nets(graph: TopologyGraph, partition_by_device: dict[str, str], nets: tuple[str, ...]) -> tuple[str, ...]:
    endpoints: set[str] = set()
    for net_name in nets:
        net = graph.nets.get(net_name)
        if net is None:
            continue
        for terminal in net.terminals:
            partition = partition_by_device.get(terminal.device)
            if partition is not None:
                endpoints.add(partition)
    return tuple(sorted(endpoints))


def _contract_partitions(contract: AnalogFloorplanContract | None) -> tuple[FunctionalPartition, ...]:
    if contract is None:
        return ()
    return tuple(
        FunctionalPartition(
            name=partition.name,
            role=partition.role,
            devices=partition.devices,
            nets=partition.nets,
            width_um=partition.width_um,
            height_um=partition.height_um,
        )
        for partition in contract.partitions
    )


def _intent_motifs(
    graph: TopologyGraph,
    partitions: Sequence[FunctionalPartition],
    constraints,
) -> tuple[str, ...]:
    motifs: list[str] = []
    roles = {partition.role for partition in partitions}
    device_roles = {device.role for device in graph.devices.values()}
    if any(device.role == DeviceRole.INPUT_PAIR for device in graph.devices.values()):
        motifs.append("diff_pair")
    if any(device.role == DeviceRole.CURRENT_MIRROR for device in graph.devices.values()):
        motifs.append("current_mirror")
    if DeviceRole.CASCODE in device_roles:
        motifs.append("cascode")
    if DeviceRole.TAIL in device_roles:
        motifs.append("tail_bias")
    if "regenerative_latch" in roles:
        motifs.append("cross_coupled_latch")
    if "reset" in roles:
        motifs.append("reset_precharge")
    if any(net.role == NetRole.COMPENSATION for net in graph.nets.values()):
        motifs.append("compensation")
    if any(device.role in {DeviceRole.COMP_CAPACITOR, DeviceRole.COMP_RESISTOR} for device in graph.devices.values()):
        motifs.append("feedback")
    if any(item.kind == "differential_partner" for item in constraints.routing):
        motifs.append("differential_routing")
    return tuple(dict.fromkeys(motifs))


def _intent_priorities(skeleton: str) -> tuple[str, ...]:
    if skeleton == "comparator_latch":
        return ("match", "symmetry", "adjacency", "critical_nets", "area")
    if skeleton == "bandgap_reference":
        return ("match", "symmetry", "critical_nets", "adjacency", "area")
    if skeleton in {"folded_cascode_ota", "telescopic_ota", "two_stage_ota", "three_stage_ota", "differential_row"}:
        return ("match", "symmetry", "critical_nets", "adjacency", "area")
    if skeleton in {"sampler", "reference_buffer", "mdac_stage", "loop_filter", "charge_pump"}:
        return ("critical_nets", "match", "symmetry", "adjacency", "area")
    if skeleton in {"pipeline_adc_system", "pll_system"}:
        return ("critical_nets", "adjacency", "match", "symmetry", "area")
    if skeleton == "mirror_fanout":
        return ("match", "critical_nets", "area")
    return ("critical_nets", "match", "symmetry", "area")


def _preferred_partition_order(
    partitions: Sequence[FunctionalPartition],
    skeleton: str,
) -> tuple[str, ...]:
    by_name = {partition.name for partition in partitions}
    templates = {
        "comparator_latch": ("tail_switch", "input_pair", "regenerative_latch", "reset"),
        "bandgap_reference": ("pmos_mirror", "error_amplifier", "bjt_core", "resistor_ladder", "bias_tail"),
        "folded_cascode_ota": ("input_pair", "gain_stage", "pmos_load"),
        "telescopic_ota": ("input_pair", "gain_stage", "pmos_load"),
        "two_stage_ota": ("input_pair", "gain_stage", "pmos_load"),
        "three_stage_ota": ("input_pair", "gain_stage", "pmos_load"),
        "sampler": ("pass_device",),
        "reference_buffer": ("gain_stage", "bias"),
        "mdac_stage": ("pass_device", "input_pair", "pmos_load", "feedback", "tail"),
        "loop_filter": ("passive", "feedback"),
        "charge_pump": ("bias", "pass_device"),
        "pipeline_adc_system": ("reference_buffer", "sampler", "mdac_stage", "subadc_flash", "logic"),
        "pipeline_adc_frontend": ("reference_buffer", "sampler", "mdac_stage", "subadc_flash", "logic"),
        "pll_system": ("pfd", "charge_pump", "loop_filter", "vco", "divider", "logic"),
        "differential_row": ("input_pair", "gain_stage", "pmos_load", "load"),
        "mirror_fanout": ("current_mirror", "bias"),
    }
    ordered = list(_expand_partition_selectors(partitions, templates.get(skeleton, ()), role_first=True))
    for partition in partitions:
        if partition.name not in ordered:
            ordered.append(partition.name)
    return tuple(ordered)


def _anchor_partitions(
    partitions: Sequence[FunctionalPartition],
    skeleton: str,
) -> tuple[str, ...]:
    preferred = {
        "comparator_latch": ("tail_switch", "input_pair"),
        "bandgap_reference": ("bjt_core", "resistor_ladder"),
        "folded_cascode_ota": ("input_pair",),
        "telescopic_ota": ("input_pair",),
        "two_stage_ota": ("input_pair",),
        "three_stage_ota": ("input_pair",),
        "sampler": ("pass_device",),
        "reference_buffer": ("gain_stage",),
        "mdac_stage": ("input_pair", "feedback"),
        "loop_filter": ("passive",),
        "charge_pump": ("bias",),
        "pipeline_adc_system": ("reference_buffer", "mdac_stage"),
        "pipeline_adc_frontend": ("reference_buffer", "sampler"),
        "pll_system": ("loop_filter", "vco"),
        "differential_row": ("input_pair",),
        "mirror_fanout": ("current_mirror",),
    }.get(skeleton, ())
    return _expand_partition_selectors(partitions, preferred, role_first=True)


def _focus_partitions(
    partitions: Sequence[FunctionalPartition],
    skeleton: str,
) -> tuple[str, ...]:
    preferred = {
        "comparator_latch": ("regenerative_latch", "reset"),
        "bandgap_reference": ("error_amplifier", "pmos_mirror"),
        "folded_cascode_ota": ("gain_stage", "pmos_load"),
        "telescopic_ota": ("gain_stage", "pmos_load"),
        "two_stage_ota": ("gain_stage", "pmos_load"),
        "three_stage_ota": ("gain_stage", "pmos_load"),
        "sampler": ("pass_device",),
        "reference_buffer": ("gain_stage", "bias"),
        "mdac_stage": ("input_pair", "feedback", "pass_device"),
        "loop_filter": ("feedback", "passive"),
        "charge_pump": ("pass_device", "bias"),
        "pipeline_adc_system": ("mdac_stage", "reference_buffer", "logic"),
        "pipeline_adc_frontend": ("mdac_stage", "subadc_flash"),
        "pll_system": ("charge_pump", "loop_filter", "vco", "divider"),
        "differential_row": ("gain_stage", "pmos_load"),
        "mirror_fanout": ("current_mirror",),
    }.get(skeleton, ())
    return _expand_partition_selectors(partitions, preferred, role_first=True)


def _partition_names_by_role(partitions: Sequence[FunctionalPartition]) -> dict[str, tuple[str, ...]]:
    names_by_role: dict[str, list[str]] = {}
    for partition in partitions:
        names_by_role.setdefault(partition.role, []).append(partition.name)
    return {role: tuple(sorted(names, key=_natural_name_key)) for role, names in names_by_role.items()}


def _partition_names_by_role_map(role_by_partition: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    names_by_role: dict[str, list[str]] = {}
    for name, role in role_by_partition.items():
        names_by_role.setdefault(str(role), []).append(str(name))
    return {role: tuple(sorted(names, key=_natural_name_key)) for role, names in names_by_role.items()}


def _expand_partition_selectors(
    partitions: Sequence[FunctionalPartition],
    selectors: Sequence[str],
    *,
    role_first: bool = False,
) -> tuple[str, ...]:
    by_name = {partition.name: partition for partition in partitions}
    by_role = _partition_names_by_role(partitions)
    resolved: list[str] = []
    for raw_selector in selectors:
        selector = str(raw_selector)
        if not selector:
            continue
        if role_first and selector in by_role:
            resolved.extend(by_role[selector])
            continue
        if selector in by_name:
            resolved.append(selector)
            continue
        if selector in by_role:
            resolved.extend(by_role[selector])
            continue
        resolved.extend(
            name
            for name in sorted(by_name, key=_natural_name_key)
            if name == selector or name.startswith(f"{selector}_")
        )
    return tuple(dict.fromkeys(name for name in resolved if name in by_name))


def _natural_name_key(text: str) -> tuple[object, ...]:
    parts = re.split(r"(\d+)", str(text).lower())
    key: list[object] = []
    for part in parts:
        if not part:
            continue
        key.append(int(part) if part.isdigit() else part)
    return tuple(key)


def _adjacency_constraints(
    partitions: Sequence[FunctionalPartition],
    skeleton: str,
) -> tuple[AnalogFloorplanAdjacencyConstraint, ...]:
    by_name = {partition.name for partition in partitions}
    by_role = _partition_names_by_role(partitions)
    if skeleton == "pipeline_adc_system":
        pairs: tuple[tuple[str, str], ...] = (
            *((ref, mdac) for ref in by_role.get("reference_buffer", ()) for mdac in by_role.get("mdac_stage", ())),
            *((sampler, by_role["mdac_stage"][0]) for sampler in by_role.get("sampler", ()) if by_role.get("mdac_stage")),
            *(zip(by_role.get("mdac_stage", ()), by_role.get("mdac_stage", ())[1:])),
            *((by_role["mdac_stage"][-1], flash) for flash in by_role.get("subadc_flash", ()) if by_role.get("mdac_stage")),
            *((flash, logic) for flash in by_role.get("subadc_flash", ()) for logic in by_role.get("logic", ())),
        )
    elif skeleton == "pll_system":
        divider_chain = by_role.get("divider", ())
        pairs = (
            *((pfd, cp) for pfd in by_role.get("pfd", ()) for cp in by_role.get("charge_pump", ())),
            *((cp, lf) for cp in by_role.get("charge_pump", ()) for lf in by_role.get("loop_filter", ())),
            *((lf, vco) for lf in by_role.get("loop_filter", ()) for vco in by_role.get("vco", ())),
            *((vco, divider_chain[0]) for vco in by_role.get("vco", ()) if divider_chain),
            *(zip(divider_chain, divider_chain[1:])),
            *((divider_chain[-1], logic) for logic in by_role.get("logic", ()) if divider_chain),
        )
    else:
        pairs = {
            "comparator_latch": (("tail_switch", "input_pair"), ("input_pair", "regenerative_latch"), ("regenerative_latch", "reset")),
            "bandgap_reference": (("pmos_mirror", "error_amplifier"), ("error_amplifier", "bjt_core"), ("bjt_core", "resistor_ladder"), ("bias_tail", "error_amplifier")),
            "folded_cascode_ota": (("input_pair", "gain_stage"), ("gain_stage", "pmos_load")),
            "telescopic_ota": (("input_pair", "gain_stage"), ("gain_stage", "pmos_load")),
            "two_stage_ota": (("input_pair", "gain_stage"), ("gain_stage", "pmos_load")),
            "three_stage_ota": (("input_pair", "gain_stage"), ("gain_stage", "pmos_load")),
            "sampler": (),
            "reference_buffer": (("gain_stage", "bias"),),
            "mdac_stage": (("pass_device", "input_pair"), ("input_pair", "pmos_load"), ("input_pair", "feedback"), ("tail", "input_pair")),
            "loop_filter": (("passive", "feedback"),),
            "charge_pump": (("bias", "pass_device"),),
            "pipeline_adc_frontend": (("reference_buffer", "sampler"), ("sampler", "mdac_stage"), ("mdac_stage", "subadc_flash")),
            "differential_row": (("input_pair", "gain_stage"), ("gain_stage", "pmos_load")),
            "mirror_fanout": (("current_mirror", "bias"),),
        }.get(skeleton, ())
    return tuple(
        AnalogFloorplanAdjacencyConstraint(source, target, "close", "vertical", "high")
        for source, target in pairs
        if source in by_name and target in by_name
    )


def _row_role_contract(skeleton: str) -> tuple[tuple[str, str], ...]:
    if skeleton == "comparator_latch":
        return (("tail", "bottom"), ("input", "lower_mid"), ("regenerative_latch", "upper_mid"), ("reset", "top"))
    if skeleton == "bandgap_reference":
        return (
            ("bias_tail", "bottom"),
            ("resistor_ladder", "lower_mid"),
            ("bjt_core", "shared"),
            ("error_amplifier", "upper_mid"),
            ("pmos_mirror", "top"),
        )
    if skeleton in {"folded_cascode_ota", "telescopic_ota", "two_stage_ota", "three_stage_ota", "differential_row"}:
        return (("tail", "bottom"), ("input", "lower_mid"), ("gain", "upper_mid"), ("pmos_load", "top"), ("load", "top"))
    if skeleton == "sampler":
        return (("pass_device", "shared"),)
    if skeleton == "reference_buffer":
        return (("bias", "bottom"), ("gain", "upper_mid"))
    if skeleton == "mdac_stage":
        return (("pass_device", "bottom"), ("tail", "bottom"), ("input", "lower_mid"), ("feedback", "shared"), ("pmos_load", "top"))
    if skeleton == "loop_filter":
        return (("passive", "lower_mid"), ("feedback", "upper_mid"))
    if skeleton == "charge_pump":
        return (("bias", "bottom"), ("pass_device", "upper_mid"))
    if skeleton == "mirror_fanout":
        return (("bias", "shared"),)
    return ()


def _is_pipeline_adc_system_partition_set(role_by_name: Mapping[str, str]) -> bool:
    roles = set(role_by_name.values())
    return {"sampler", "mdac_stage", "subadc_flash", "reference_buffer", "logic"} <= roles


def _is_sampler_topology(graph: TopologyGraph) -> bool:
    return str(graph.name).lower().endswith("_sampler") or {"VINP", "VINN", "TOPP", "TOPN", "CLK"} <= set(graph.pins)


def _is_reference_buffer_topology(graph: TopologyGraph) -> bool:
    return str(graph.name).lower().endswith("_reference_buffer") or {"VINP", "VINN", "VOUTP", "VOUTN", "BIAS"} <= set(graph.pins)


def _is_mdac_stage_topology(graph: TopologyGraph) -> bool:
    return str(graph.name).lower().endswith("_mdac_stage") or {"OUTP", "OUTN", "VREFP", "VREFN"} <= set(graph.pins) and any(name.endswith("_CAPP") for name in graph.devices)


def _is_loop_filter_topology(graph: TopologyGraph) -> bool:
    return str(graph.name).lower().endswith("_loop_filter") or any(name.endswith("_CMAIN") for name in graph.devices)


def _is_charge_pump_topology(graph: TopologyGraph) -> bool:
    return str(graph.name).lower().endswith("_charge_pump") or any(name.endswith("_UPSRC") for name in graph.devices)


def _is_pipeline_adc_partition_set(role_by_name: Mapping[str, str]) -> bool:
    roles = set(role_by_name.values())
    return {"sampler", "mdac_stage", "subadc_flash", "reference_buffer"} <= roles


def _is_pll_partition_set(role_by_name: Mapping[str, str]) -> bool:
    roles = set(role_by_name.values())
    return {"pfd", "charge_pump", "loop_filter", "vco", "divider"} <= roles


def _is_brokaw_bandgap_topology(graph: TopologyGraph) -> bool:
    required = {"Q1", "R1", "M3A", "M3B", "M1A", "M1B", "M5A", "M5B", "M7"}
    if not required.issubset(graph.devices):
        return False
    q2_devices = tuple(name for name in graph.devices if name.startswith("Q2_"))
    r2_devices = tuple(name for name in graph.devices if name.startswith("R2_"))
    required_nets = {"diode1", "diode2", "ea_out", "TAIL", "BIAS_N", "VDD", "VSS"}
    return bool(q2_devices and r2_devices and required_nets.issubset(graph.nets))


def _is_folded_cascode_ota_topology(graph: TopologyGraph) -> bool:
    required = {"M1A", "M1B", "MTAIL", "MFOLDA", "MFOLDB", "MLOADA", "MLOADB"}
    net_roles = {net.role for net in graph.nets.values()}
    device_roles = {device.role for device in graph.devices.values()}
    return (
        required.issubset(graph.devices)
        and DeviceRole.INPUT_PAIR in device_roles
        and DeviceRole.CASCODE in device_roles
        and DeviceRole.LOAD in device_roles
        and NetRole.HIGH_Z in net_roles
        and {"OUTP", "OUTN", "FOLDP", "FOLDN"}.issubset(graph.nets)
    )


def _is_telescopic_ota_topology(graph: TopologyGraph) -> bool:
    required = {"M1A", "M1B", "MTAIL", "M2A", "M2B", "M3A", "M3B", "M4A", "M4B"}
    net_roles = {net.role for net in graph.nets.values()}
    device_roles = {device.role for device in graph.devices.values()}
    return (
        required.issubset(graph.devices)
        and DeviceRole.INPUT_PAIR in device_roles
        and DeviceRole.CASCODE in device_roles
        and DeviceRole.LOAD in device_roles
        and NetRole.HIGH_Z in net_roles
        and {"OUTP", "OUTN", "N1P", "N1N"}.issubset(graph.nets)
    )


def _is_two_stage_ota_topology(graph: TopologyGraph) -> bool:
    required = {"M1A", "M1B", "M2A", "M2B", "MTAIL", "MDRV", "MLOAD", "RZ", "CC"}
    net_roles = {net.role for net in graph.nets.values()}
    device_roles = {device.role for device in graph.devices.values()}
    return (
        required.issubset(graph.devices)
        and DeviceRole.INPUT_PAIR in device_roles
        and DeviceRole.DRIVER in device_roles
        and DeviceRole.COMP_RESISTOR in device_roles
        and DeviceRole.COMP_CAPACITOR in device_roles
        and NetRole.COMPENSATION in net_roles
        and NetRole.HIGH_Z in net_roles
    )


def _is_three_stage_ota_topology(graph: TopologyGraph) -> bool:
    required = {"M1A", "M1B", "M2A", "M2B", "MTAIL", "M3", "M4", "M5", "M6", "RZ1", "CC1", "CC2"}
    net_roles = {net.role for net in graph.nets.values()}
    device_roles = {device.role for device in graph.devices.values()}
    return (
        required.issubset(graph.devices)
        and DeviceRole.INPUT_PAIR in device_roles
        and DeviceRole.DRIVER in device_roles
        and DeviceRole.COMP_RESISTOR in device_roles
        and DeviceRole.COMP_CAPACITOR in device_roles
        and NetRole.HIGH_Z in net_roles
        and NetRole.OUTPUT in net_roles
    )


def _bandgap_partitions(graph: TopologyGraph) -> tuple[FunctionalPartition, ...] | None:
    if not _is_brokaw_bandgap_topology(graph):
        return None

    q2_devices = tuple(sorted(name for name in graph.devices if name.startswith("Q2_")))
    r2_devices = tuple(sorted(name for name in graph.devices if name.startswith("R2_")))
    partition_defs = (
        ("pmos_mirror", "pmos_mirror", ("M3A", "M3B")),
        ("error_amplifier", "error_amplifier", ("M1A", "M1B", "M5A", "M5B")),
        ("bjt_core", "bjt_core", ("Q1", *q2_devices)),
        ("resistor_ladder", "resistor_ladder", ("R1", *r2_devices)),
        ("bias_tail", "bias_tail", ("M7",)),
    )
    partitions: list[FunctionalPartition] = []
    for name, role, devices in partition_defs:
        present = tuple(device for device in devices if device in graph.devices)
        if not present:
            continue
        partitions.append(
            FunctionalPartition(
                name=name,
                role=role,
                devices=present,
                nets=_nets_for_devices(graph, present),
                width_um=_partition_width_um(role, len(present)),
                height_um=_partition_height_um(role, len(present)),
            )
        )
    return tuple(partitions)


def _strongarm_partitions(graph: TopologyGraph) -> tuple[FunctionalPartition, ...] | None:
    input_pair = tuple(sorted(name for name, device in graph.devices.items() if device.role == DeviceRole.INPUT_PAIR))
    tail_devices = tuple(sorted(name for name, device in graph.devices.items() if device.role == DeviceRole.TAIL))
    driver_pair = _cross_coupled_pair(graph, DeviceRole.DRIVER)
    load_pair = _cross_coupled_pair(graph, DeviceRole.LOAD)
    if len(input_pair) != 2 or len(tail_devices) != 1 or len(driver_pair) != 2 or len(load_pair) != 2:
        return None

    regenerative = tuple(sorted(dict.fromkeys((*driver_pair, *load_pair))))
    reset_devices = tuple(
        sorted(
            name
            for name, device in graph.devices.items()
            if device.role == DeviceRole.LOAD and name not in regenerative and _is_reset_switch(graph, name)
        )
    )
    if len(reset_devices) != 2:
        return None

    assigned = set((*input_pair, *tail_devices, *regenerative, *reset_devices))
    partitions = [
        FunctionalPartition("input_pair", "input", input_pair, _nets_for_devices(graph, input_pair), _partition_width_um("input", len(input_pair)), _partition_height_um("input", len(input_pair))),
        FunctionalPartition("tail_switch", "tail", tail_devices, _nets_for_devices(graph, tail_devices), _partition_width_um("tail", len(tail_devices)), _partition_height_um("tail", len(tail_devices))),
        FunctionalPartition("regenerative_latch", "regenerative_latch", regenerative, _nets_for_devices(graph, regenerative), _partition_width_um("regenerative_latch", len(regenerative)), _partition_height_um("regenerative_latch", len(regenerative))),
        FunctionalPartition("reset", "reset", reset_devices, _nets_for_devices(graph, reset_devices), _partition_width_um("reset", len(reset_devices)), _partition_height_um("reset", len(reset_devices))),
    ]
    for device in sorted(name for name in graph.devices if name not in assigned):
        current = graph.devices[device]
        role = _classify_device(current)
        name = _partition_name(role, current)
        partitions.append(
            FunctionalPartition(
                name=name,
                role=role,
                devices=(device,),
                nets=_nets_for_devices(graph, (device,)),
                width_um=_partition_width_um(role, 1),
                height_um=_partition_height_um(role, 1),
            )
        )
    return tuple(sorted(partitions, key=lambda p: (_ROLE_ORDER.get(p.role, 100), p.name)))


def _cross_coupled_pair(graph: TopologyGraph, role: DeviceRole) -> tuple[str, str]:
    devices = tuple(sorted(name for name, device in graph.devices.items() if device.role == role))
    by_terminal = {
        device_name: _device_terminal_nets(graph, device_name)
        for device_name in devices
    }
    for left_idx, left in enumerate(devices):
        left_terms = by_terminal[left]
        left_drain = left_terms.get("D", "")
        left_gate = left_terms.get("G", "")
        if not left_drain or not left_gate or left_drain == left_gate:
            continue
        for right in devices[left_idx + 1 :]:
            right_terms = by_terminal[right]
            right_drain = right_terms.get("D", "")
            right_gate = right_terms.get("G", "")
            if not right_drain or not right_gate or right_drain == right_gate:
                continue
            if left_drain == right_gate and right_drain == left_gate:
                return _ordered_pair(left, right)
    return ()


def _device_terminal_nets(graph: TopologyGraph, device_name: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for net in graph.nets.values():
        for terminal in net.terminals:
            if terminal.device == device_name:
                mapping[str(terminal.terminal)] = net.name
    return mapping


def _is_reset_switch(graph: TopologyGraph, device_name: str) -> bool:
    terminals = _device_terminal_nets(graph, device_name)
    gate = graph.nets.get(terminals.get("G", ""))
    drain = graph.nets.get(terminals.get("D", ""))
    source = graph.nets.get(terminals.get("S", ""))
    if gate is None or drain is None or source is None:
        return False
    return gate.role == NetRole.CLOCK and drain.role == NetRole.OUTPUT and source.role == NetRole.SUPPLY


def _ordered_pair(left: str, right: str) -> tuple[str, str]:
    if left.endswith("_P") and right.endswith("_N"):
        return (left, right)
    if left.endswith("_N") and right.endswith("_P"):
        return (right, left)
    if left.endswith("_L") and right.endswith("_R"):
        return (left, right)
    if left.endswith("_R") and right.endswith("_L"):
        return (right, left)
    return tuple(sorted((left, right)))  # type: ignore[return-value]


def _preferred_endpoint(
    endpoints: tuple[str, ...],
    role_by_partition: dict[str, str],
    roles: tuple[str, ...],
    *,
    default: str,
    exclude: str = "",
) -> str:
    for role in roles:
        for endpoint in endpoints:
            if endpoint != exclude and role_by_partition.get(endpoint) == role:
                return endpoint
    for endpoint in endpoints:
        if endpoint != exclude:
            return endpoint
    return default


def _dedupe_edges(edges: Sequence[SignalEdge]) -> tuple[SignalEdge, ...]:
    seen: set[tuple[str, str, str]] = set()
    result: list[SignalEdge] = []
    for edge in edges:
        if edge.source == edge.target:
            continue
        key = (edge.source, edge.target, edge.net)
        if key in seen:
            continue
        seen.add(key)
        result.append(edge)
    return tuple(result)


def _tuple_value(value: object) -> tuple[str, ...]:
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, str):
        return (value,)
    return ()


def _is_input_terminal(name: str) -> bool:
    return name.upper() in _INPUT_TERMINALS


def _is_output_terminal(name: str) -> bool:
    return name.upper() in _OUTPUT_TERMINALS


def _normalized_device_text(device: Device) -> str:
    raw = f" {device.name} {device.model} {device.role.value} ".lower()
    for char in "_-/.:":
        raw = raw.replace(char, " ")
    return f" {raw} "


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)
