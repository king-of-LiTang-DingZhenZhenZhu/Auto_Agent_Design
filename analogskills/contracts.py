"""Core hard-capability contracts: circuit graph and layout constraints."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class DeviceRole(str, Enum):
    INPUT_PAIR = "input_pair"
    CURRENT_MIRROR = "current_mirror"
    CASCODE = "cascode"
    TAIL = "tail"
    LOAD = "load"
    DRIVER = "driver"
    PASS_TRANSISTOR = "pass_transistor"
    BIAS = "bias"
    BIPOLAR = "bipolar"
    FEEDBACK_RESISTOR = "feedback_resistor"
    COMP_RESISTOR = "comp_resistor"
    COMP_CAPACITOR = "comp_capacitor"
    PASSIVE = "passive"
    PIN = "pin"
    UNKNOWN = "unknown"


class NetRole(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    SUPPLY = "supply"
    GROUND = "ground"
    BIAS = "bias"
    HIGH_Z = "high_z"
    INTERNAL = "internal"
    DIFFERENTIAL = "differential"
    COMPENSATION = "compensation"
    REFERENCE = "reference"
    FEEDBACK = "feedback"
    CLOCK = "clock"
    BUS = "bus"


@dataclass(frozen=True, order=True)
class TerminalRef:
    device: str
    terminal: str

    @classmethod
    def parse(cls, value: str) -> "TerminalRef":
        if "." not in value:
            raise ValueError(f"terminal reference must be DEVICE.TERM: {value!r}")
        device, terminal = value.split(".", 1)
        if not device or not terminal:
            raise ValueError(f"invalid terminal reference: {value!r}")
        return cls(device, terminal)

    def __str__(self) -> str:
        return f"{self.device}.{self.terminal}"


@dataclass(frozen=True)
class Device:
    name: str
    role: DeviceRole
    model: str
    terminals: tuple[str, ...]
    parameters: dict[str, float | int | str] = field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class Net:
    name: str
    role: NetRole
    terminals: tuple[TerminalRef, ...]
    notes: str = ""


@dataclass(frozen=True)
class MatchGroup:
    name: str
    devices: tuple[str, ...]
    style: str = "mirror"
    require_dummies: bool = True
    unit_segments: int = 1
    notes: str = ""


@dataclass(frozen=True)
class RoutingConstraint:
    net: str
    kind: str
    value: float | int | str | bool | tuple[str, ...]
    reason: str = ""


@dataclass(frozen=True)
class StandardCellDeviceConstraint:
    device: str
    row: str
    allowed_columns: tuple[int, ...] = ()
    fixed_column: int | None = None
    allowed_orientations: tuple[str, ...] = ()
    fixed_orientation: str | None = None
    order_before: tuple[str, ...] = ()
    adjacent_to: tuple[str, ...] = ()
    boundary_anchor: str = ""
    notes: str = ""


@dataclass(frozen=True)
class StandardCellNetConstraint:
    net: str
    pin_side: str = "internal"
    pin_order_index: int | None = None
    allowed_pin_columns: tuple[int, ...] = ()
    trunk_layer: str = ""
    allowed_tracks: tuple[int, ...] = ()
    fixed_track: int | None = None
    avoid_nets: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class StandardCellDeviceClusterConstraint:
    name: str
    devices: tuple[str, ...]
    row: str = ""
    max_span: int = 0
    allow_permutation: bool = True
    notes: str = ""


@dataclass(frozen=True)
class StandardCellPinGroupConstraint:
    name: str
    nets: tuple[str, ...]
    pin_side: str = "top"
    max_span: int = 0
    ordered: bool = True
    anchor: str = ""
    notes: str = ""


@dataclass(frozen=True)
class StandardCellInternalNetClusterConstraint:
    name: str
    nets: tuple[str, ...]
    max_track_span: int = 0
    ordered: bool = False
    notes: str = ""


@dataclass(frozen=True)
class StandardCellConstraintSet:
    rows: tuple[str, ...] = ()
    rail_nets: tuple[str, ...] = ()
    max_columns: int = 0
    device_constraints: tuple[StandardCellDeviceConstraint, ...] = ()
    net_constraints: tuple[StandardCellNetConstraint, ...] = ()
    device_clusters: tuple[StandardCellDeviceClusterConstraint, ...] = ()
    pin_groups: tuple[StandardCellPinGroupConstraint, ...] = ()
    internal_net_clusters: tuple[StandardCellInternalNetClusterConstraint, ...] = ()
    compact_style: str = "standard_cell"

    def device_constraint_for(self, device: str) -> StandardCellDeviceConstraint | None:
        for item in self.device_constraints:
            if item.device == device:
                return item
        return None

    def net_constraint_for(self, net: str) -> StandardCellNetConstraint | None:
        for item in self.net_constraints:
            if item.net == net:
                return item
        return None

    def device_cluster_for(self, name: str) -> StandardCellDeviceClusterConstraint | None:
        for item in self.device_clusters:
            if item.name == name:
                return item
        return None

    def pin_group_for(self, name: str) -> StandardCellPinGroupConstraint | None:
        for item in self.pin_groups:
            if item.name == name:
                return item
        return None

    def internal_net_cluster_for(self, name: str) -> StandardCellInternalNetClusterConstraint | None:
        for item in self.internal_net_clusters:
            if item.name == name:
                return item
        return None


@dataclass(frozen=True)
class LayoutConstraintSet:
    matched_groups: tuple[MatchGroup, ...] = ()
    symmetry_groups: tuple[tuple[str, ...], ...] = ()
    routing: tuple[RoutingConstraint, ...] = ()
    critical_nets: tuple[str, ...] = ()
    standard_cell: StandardCellConstraintSet | None = None

    def constraints_for_net(self, net: str) -> tuple[RoutingConstraint, ...]:
        return tuple(c for c in self.routing if c.net == net)


@dataclass(frozen=True)
class AnalogFloorplanIntent:
    circuit_family: str = "generic_analog"
    preferred_skeleton: str = "signal_flow_chain"
    motifs: tuple[str, ...] = ()
    critical_nets: tuple[str, ...] = ()
    placement_priorities: tuple[str, ...] = ("match", "symmetry", "critical_nets", "area")
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalogFloorplanPartitionConstraint:
    name: str
    role: str
    devices: tuple[str, ...] = ()
    nets: tuple[str, ...] = ()
    width_um: float = 0.0
    height_um: float = 0.0
    anchor: bool = False
    focus: bool = False
    order_index: int | None = None
    notes: str = ""


@dataclass(frozen=True)
class AnalogFloorplanAdjacencyConstraint:
    source: str
    target: str
    relation: str = "close"
    axis: str = "vertical"
    priority: str = "medium"
    notes: str = ""


@dataclass(frozen=True)
class AnalogFloorplanContract:
    intent: AnalogFloorplanIntent = field(default_factory=AnalogFloorplanIntent)
    partitions: tuple[AnalogFloorplanPartitionConstraint, ...] = ()
    preferred_partition_order: tuple[str, ...] = ()
    anchor_partitions: tuple[str, ...] = ()
    focus_partitions: tuple[str, ...] = ()
    adjacency: tuple[AnalogFloorplanAdjacencyConstraint, ...] = ()
    matched_groups: tuple[MatchGroup, ...] = ()
    symmetry_groups: tuple[tuple[str, ...], ...] = ()
    routing: tuple[RoutingConstraint, ...] = ()
    critical_nets: tuple[str, ...] = ()
    row_roles: tuple[tuple[str, str], ...] = ()

    def to_layout_constraints(self) -> LayoutConstraintSet:
        return LayoutConstraintSet(
            matched_groups=self.matched_groups,
            symmetry_groups=self.symmetry_groups,
            routing=self.routing,
            critical_nets=self.critical_nets,
        )


@dataclass
class AnalogPlacementObjective:
    name: str
    weight: float = 1.0
    priority: int = 0
    notes: str = ""


@dataclass(frozen=True)
class AnalogPlacementGroup:
    name: str
    devices: tuple[str, ...] = ()
    role: str = ""
    anchor: bool = False
    focus: bool = False
    order_index: int | None = None
    target_row: str = ""
    target_partition: str = ""
    critical_nets: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class AnalogPlacementStrategy:
    groups: tuple[AnalogPlacementGroup, ...] = ()
    objectives: tuple[AnalogPlacementObjective, ...] = ()
    initial_mode: str = "analytical_seed"
    tune_with_agent: bool = True
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalogRoutingGroup:
    name: str
    nets: tuple[str, ...] = ()
    route_mode: str = "astar"
    priority: int = 100
    preferred_layer: str = ""
    corridor: str = ""
    shield_net: str = ""
    critical: bool = False
    notes: str = ""


@dataclass(frozen=True)
class AnalogRoutingStrategy:
    groups: tuple[AnalogRoutingGroup, ...] = ()
    route_order: tuple[str, ...] = ()
    allow_ripup: bool = True
    notes: tuple[str, ...] = ()


@dataclass
class TopologyGraph:
    name: str
    devices: dict[str, Device] = field(default_factory=dict)
    nets: dict[str, Net] = field(default_factory=dict)
    pins: dict[str, NetRole] = field(default_factory=dict)
    layout_constraints: LayoutConstraintSet = field(default_factory=LayoutConstraintSet)

    def add_device(self, device: Device) -> None:
        if device.name in self.devices:
            raise ValueError(f"duplicate device {device.name!r}")
        self.devices[device.name] = device

    def add_pin(self, name: str, role: NetRole) -> None:
        self.pins[name] = role

    def add_net(self, name: str, role: NetRole, terminals: Iterable[str | TerminalRef], notes: str = "") -> None:
        if name in self.nets:
            raise ValueError(f"duplicate net {name!r}")
        refs = tuple(t if isinstance(t, TerminalRef) else TerminalRef.parse(t) for t in terminals)
        self.nets[name] = Net(name, role, refs, notes)

    def terminal_net_map(self) -> dict[TerminalRef, str]:
        result: dict[TerminalRef, str] = {}
        for net in self.nets.values():
            for terminal in net.terminals:
                if terminal in result:
                    raise ValueError(f"terminal {terminal} appears on both {result[terminal]!r} and {net.name!r}")
                result[terminal] = net.name
        return result

    def get_net_for(self, device: str, terminal: str) -> str | None:
        return self.terminal_net_map().get(TerminalRef(device, terminal))

    def validate(self) -> list[str]:
        issues: list[str] = []
        known = set(self.devices) | set(self.pins)
        try:
            term_map = self.terminal_net_map()
        except ValueError as exc:
            issues.append(str(exc))
            term_map = {}
        for terminal in term_map:
            if terminal.device not in known:
                issues.append(f"unknown terminal device {terminal.device!r} on {terminal}")
                continue
            if terminal.device in self.devices and terminal.terminal not in self.devices[terminal.device].terminals:
                issues.append(f"unknown terminal {terminal} for device model {self.devices[terminal.device].model}")
        return issues
