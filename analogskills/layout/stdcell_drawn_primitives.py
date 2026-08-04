"""Stdcell-native drawn transistor primitive contracts.

This module intentionally does not depend on foundry PCells.  It defines the
first reusable contract for compact standard-cell-specific transistor
generation, so later compilers can consume:

- drawn FEOL/BEOL geometry
- gate/source/drain access windows
- diffusion merge legality
- local keepouts

The first implementation only builds a compact abstract primitive layout
contract; it does not yet emit full foundry-valid geometry for signoff.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, TYPE_CHECKING

from analogskills.layout.physical import BBox

if TYPE_CHECKING:
    from .stdcell_carriers import NativeStdCellCarrier


@dataclass(frozen=True)
class NativeStdCellMosPrimitiveSpec:
    name: str
    device_type: str
    row: str
    nf: int
    nfin: int
    shared_diffusion_role: str = "isolated"
    contact_style: str = "both"
    dummy_style: str = "none"
    vt_flavor: str = "svt"
    well_flavor: str = "default"
    implant_flavor: str = "default"
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeStdCellPrimitiveAccessWindow:
    terminal: str
    side: str
    layer: str
    center_xy_um: tuple[float, float]
    bbox_um: BBox
    legal: bool = True


@dataclass(frozen=True)
class NativeStdCellPrimitiveDiffusionPort:
    side: str
    net_role: str
    bbox_um: BBox
    merge_key: str
    supports_merge: bool = True


@dataclass(frozen=True)
class NativeStdCellMosPrimitiveAccessContract:
    gate_windows: tuple[NativeStdCellPrimitiveAccessWindow, ...]
    source_windows: tuple[NativeStdCellPrimitiveAccessWindow, ...]
    drain_windows: tuple[NativeStdCellPrimitiveAccessWindow, ...]
    diffusion_ports: tuple[NativeStdCellPrimitiveDiffusionPort, ...]
    keepouts_by_layer: Mapping[str, tuple[BBox, ...]]
    lvs_recognition_hints: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeStdCellMosPrimitiveLayout:
    cell_bbox_um: BBox
    shapes_by_layer: Mapping[str, tuple[BBox, ...]]
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeStdCellMosPrimitive:
    spec: NativeStdCellMosPrimitiveSpec
    layout: NativeStdCellMosPrimitiveLayout
    access_contract: NativeStdCellMosPrimitiveAccessContract


@dataclass(frozen=True)
class NativeStdCellSharedDiffusionPair:
    left: NativeStdCellMosPrimitive
    right: NativeStdCellMosPrimitive
    merged_layout: NativeStdCellMosPrimitiveLayout
    merged_access_contract: NativeStdCellMosPrimitiveAccessContract
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeStdCellPrimitiveCluster:
    carrier_name: str
    carrier_kind: str
    row: str
    primitives: tuple[NativeStdCellMosPrimitive, ...]
    layout: NativeStdCellMosPrimitiveLayout
    access_contract: NativeStdCellMosPrimitiveAccessContract
    metadata: Mapping[str, object] = field(default_factory=dict)


def build_native_stdcell_primitive_specs_from_carrier(
    carrier: "NativeStdCellCarrier",
    *,
    nfin_by_model: Mapping[str, int] | None = None,
) -> tuple[NativeStdCellMosPrimitiveSpec, ...]:
    """Map a stdcell carrier decomposition to primitive specs.

    This is the bridge between:
    - topology/carrier decomposition
    - drawn stdcell-native primitive generation
    """
    fin_lookup = {str(key).lower(): int(value) for key, value in dict(nfin_by_model or {}).items()}
    device_type = "pmos" if carrier.row == "pmos" else "nmos"
    specs: list[NativeStdCellMosPrimitiveSpec] = []
    device_count = len(carrier.devices)
    for index, device in enumerate(carrier.devices):
        if device_count == 1:
            shared_role = "isolated"
        elif device_count == 2 and index == 0:
            shared_role = "right_shared"
        elif device_count == 2 and index == 1:
            shared_role = "left_shared"
        elif index == 0:
            shared_role = "right_shared"
        elif index == device_count - 1:
            shared_role = "left_shared"
        else:
            shared_role = "both_shared"
        model_key = str(carrier.model).lower()
        specs.append(
            NativeStdCellMosPrimitiveSpec(
                name=str(device.device),
                device_type=device_type,
                row="prow" if carrier.row == "pmos" else "nrow",
                nf=1,
                nfin=fin_lookup.get(model_key, 5 if device_type == "nmos" else 10),
                shared_diffusion_role=shared_role,
                contact_style="both",
                vt_flavor="svt",
                metadata={
                    "carrier_name": carrier.name,
                    "carrier_kind": carrier.kind,
                    "gate_net": device.gate_net,
                    "source_net": device.source_net,
                    "drain_net": device.drain_net,
                },
            )
        )
    return tuple(specs)


def build_native_stdcell_primitive_cluster_from_carrier(
    carrier: "NativeStdCellCarrier",
    *,
    nfin_by_model: Mapping[str, int] | None = None,
    inter_primitive_gap_um: float = 0.02,
) -> NativeStdCellPrimitiveCluster:
    specs = build_native_stdcell_primitive_specs_from_carrier(
        carrier,
        nfin_by_model=nfin_by_model,
    )
    return build_native_stdcell_primitive_cluster(
        specs,
        carrier_name=carrier.name,
        carrier_kind=carrier.kind,
        row="prow" if carrier.row == "pmos" else "nrow",
        inter_primitive_gap_um=inter_primitive_gap_um,
        metadata={
            "endpoint_nets": carrier.endpoint_nets,
            "internal_nets": carrier.internal_nets,
            "gate_nets": carrier.gate_nets,
            "bulk_nets": carrier.bulk_nets,
            "device_names": carrier.device_names,
        },
    )


def build_native_stdcell_primitive_cluster(
    specs: tuple[NativeStdCellMosPrimitiveSpec, ...],
    *,
    carrier_name: str,
    carrier_kind: str,
    row: str,
    inter_primitive_gap_um: float = 0.02,
    metadata: Mapping[str, object] | None = None,
) -> NativeStdCellPrimitiveCluster:
    if not specs:
        raise ValueError("primitive cluster requires at least one spec")
    primitives = tuple(build_native_stdcell_mos_primitive(spec) for spec in specs)
    placed = _place_cluster_primitives(primitives, inter_primitive_gap_um=inter_primitive_gap_um)
    shapes_by_layer = _coalesce_cluster_shapes(placed)
    keepouts_by_layer = _coalesce_cluster_keepouts(placed)
    diffusion_ports = _collapse_cluster_diffusion_ports(placed)
    gate_windows = tuple(window for item in placed for window in item["gate_windows"])
    source_windows = tuple(window for item in placed for window in item["source_windows"])
    drain_windows = tuple(window for item in placed for window in item["drain_windows"])
    cluster_layout = NativeStdCellMosPrimitiveLayout(
        cell_bbox_um=_union_bbox(tuple(box for boxes in shapes_by_layer.values() for box in boxes)),
        shapes_by_layer=shapes_by_layer,
        metadata={
            "carrier_name": carrier_name,
            "carrier_kind": carrier_kind,
            "row": row,
            "primitive_count": len(primitives),
            "merge_strategy": "shared_diffusion_chain" if any(item["shared_with_left"] for item in placed[1:]) else "discrete_chain",
        },
    )
    cluster_access = NativeStdCellMosPrimitiveAccessContract(
        gate_windows=gate_windows,
        source_windows=source_windows,
        drain_windows=drain_windows,
        diffusion_ports=diffusion_ports,
        keepouts_by_layer=keepouts_by_layer,
        lvs_recognition_hints={
            "carrier_name": carrier_name,
            "carrier_kind": carrier_kind,
            "row": row,
            "primitive_count": len(primitives),
            "shared_diffusion_interfaces": sum(1 for item in placed[1:] if item["shared_with_left"]),
        },
        metadata={
            "carrier_name": carrier_name,
            "carrier_kind": carrier_kind,
        },
    )
    return NativeStdCellPrimitiveCluster(
        carrier_name=carrier_name,
        carrier_kind=carrier_kind,
        row=row,
        primitives=primitives,
        layout=cluster_layout,
        access_contract=cluster_access,
        metadata={
            **dict(metadata or {}),
            "primitive_names": tuple(spec.name for spec in specs),
            "shared_diffusion_interfaces": sum(1 for item in placed[1:] if item["shared_with_left"]),
        },
    )


def build_native_stdcell_shared_diffusion_pair(
    left_spec: NativeStdCellMosPrimitiveSpec,
    right_spec: NativeStdCellMosPrimitiveSpec,
    *,
    shared_gap_um: float = 0.0,
    inter_gate_keepout_um: float = 0.02,
) -> NativeStdCellSharedDiffusionPair:
    """Build a two-device compact pair with a merged center diffusion.

    This helper is the first reusable abstraction for:
    - NMOS series pair
    - PMOS parallel pair
    - future AOI/OAI local transistor clusters
    """
    _validate_pair_specs(left_spec, right_spec)
    left = build_native_stdcell_mos_primitive(left_spec)
    right = build_native_stdcell_mos_primitive(right_spec)

    left_bbox = left.layout.cell_bbox_um
    right_bbox = right.layout.cell_bbox_um
    left_right_port = next(port for port in left.access_contract.diffusion_ports if port.side == "right")
    right_left_port = next(port for port in right.access_contract.diffusion_ports if port.side == "left")

    shared_width = max(
        left_right_port.bbox_um[2] - left_right_port.bbox_um[0],
        right_left_port.bbox_um[2] - right_left_port.bbox_um[0],
    )
    right_shift_x = (
        left_bbox[2]
        - (right_left_port.bbox_um[2] - right_left_port.bbox_um[0])
        - (right_bbox[0] - right_left_port.bbox_um[0])
        + float(shared_gap_um)
    )

    merged_shapes: dict[str, list[BBox]] = {}
    for layer, boxes in left.layout.shapes_by_layer.items():
        merged_shapes.setdefault(layer, []).extend(boxes)
    for layer, boxes in right.layout.shapes_by_layer.items():
        merged_shapes.setdefault(layer, []).extend(_shift_boxes(boxes, dx=right_shift_x))

    shared_center_x0 = left_bbox[2] - shared_width
    shared_center_x1 = shared_center_x0 + shared_width
    active_top = max(left_right_port.bbox_um[3], right_left_port.bbox_um[3])
    active_bottom = min(left_right_port.bbox_um[1], right_left_port.bbox_um[1])
    merged_od = _merge_od_boxes(tuple(merged_shapes.get("OD", ())), shared_center_x0, shared_center_x1, active_bottom, active_top)
    merged_shapes["OD"] = list(merged_od)

    merged_keepouts: dict[str, tuple[BBox, ...]] = {}
    for layer, boxes in left.access_contract.keepouts_by_layer.items():
        merged_keepouts[layer] = tuple(boxes)
    for layer, boxes in right.access_contract.keepouts_by_layer.items():
        merged_keepouts[layer] = tuple((*merged_keepouts.get(layer, ()), *_shift_boxes(boxes, dx=right_shift_x)))

    shifted_right_gates = tuple(
        _shift_window(window, dx=right_shift_x)
        for window in right.access_contract.gate_windows
    )
    shifted_right_sources = tuple(
        _shift_window(window, dx=right_shift_x)
        for window in right.access_contract.source_windows
    )
    shifted_right_drains = tuple(
        _shift_window(window, dx=right_shift_x)
        for window in right.access_contract.drain_windows
    )
    shifted_right_ports = tuple(
        _shift_port(port, dx=right_shift_x)
        for port in right.access_contract.diffusion_ports
    )

    left_outer_port = next(port for port in left.access_contract.diffusion_ports if port.side == "left")
    right_outer_port = next(port for port in shifted_right_ports if port.side == "right")
    shared_port_bbox = (
        shared_center_x0,
        active_bottom,
        shared_center_x1,
        active_top,
    )
    shared_port = NativeStdCellPrimitiveDiffusionPort(
        side="center",
        net_role="shared",
        bbox_um=shared_port_bbox,
        merge_key=f"{left.spec.device_type}:{left.spec.row}:center_shared",
        supports_merge=True,
    )

    merged_layout = NativeStdCellMosPrimitiveLayout(
        cell_bbox_um=_union_bbox(tuple(box for boxes in merged_shapes.values() for box in boxes)),
        shapes_by_layer={layer: tuple(boxes) for layer, boxes in merged_shapes.items()},
        metadata={
            "pair": True,
            "left": left.spec.name,
            "right": right.spec.name,
            "shared_width_um": shared_width,
        },
    )
    merged_access = NativeStdCellMosPrimitiveAccessContract(
        gate_windows=tuple((*left.access_contract.gate_windows, *shifted_right_gates)),
        source_windows=tuple((*left.access_contract.source_windows, *shifted_right_sources)),
        drain_windows=tuple((*left.access_contract.drain_windows, *shifted_right_drains)),
        diffusion_ports=(left_outer_port, shared_port, right_outer_port),
        keepouts_by_layer=merged_keepouts,
        lvs_recognition_hints={
            **dict(left.access_contract.lvs_recognition_hints),
            "pair": True,
            "shared_diffusion": True,
            "inter_gate_keepout_um": inter_gate_keepout_um,
        },
        metadata={
            "left_name": left.spec.name,
            "right_name": right.spec.name,
        },
    )
    return NativeStdCellSharedDiffusionPair(
        left=left,
        right=right,
        merged_layout=merged_layout,
        merged_access_contract=merged_access,
        metadata={
            "pair_type": f"{left.spec.device_type}_shared_diffusion",
        },
    )


def _validate_pair_specs(
    left_spec: NativeStdCellMosPrimitiveSpec,
    right_spec: NativeStdCellMosPrimitiveSpec,
) -> None:
    _validate_primitive_spec(left_spec)
    _validate_primitive_spec(right_spec)
    if left_spec.device_type != right_spec.device_type:
        raise ValueError("shared diffusion pair requires matching device_type")
    if left_spec.row != right_spec.row:
        raise ValueError("shared diffusion pair requires matching row")
    if left_spec.shared_diffusion_role not in {"right_shared", "both_shared"}:
        raise ValueError("left spec must expose right shared diffusion")
    if right_spec.shared_diffusion_role not in {"left_shared", "both_shared"}:
        raise ValueError("right spec must expose left shared diffusion")


def _place_cluster_primitives(
    primitives: tuple[NativeStdCellMosPrimitive, ...],
    *,
    inter_primitive_gap_um: float,
) -> tuple[dict[str, object], ...]:
    placed: list[dict[str, object]] = []
    previous: dict[str, object] | None = None
    for primitive in primitives:
        dx = 0.0
        shared_with_left = False
        if previous is not None:
            if _can_share_diffusion(previous["primitive"], primitive):
                prev_port = _find_port(previous["primitive"].access_contract.diffusion_ports, side="right")
                curr_port = _find_port(primitive.access_contract.diffusion_ports, side="left")
                dx = float(previous["dx"]) + float(prev_port.bbox_um[0]) - float(curr_port.bbox_um[0])
                shared_with_left = True
            else:
                prev_bbox = previous["primitive"].layout.cell_bbox_um
                curr_bbox = primitive.layout.cell_bbox_um
                dx = float(previous["dx"]) + float(prev_bbox[2]) + float(inter_primitive_gap_um) - float(curr_bbox[0])
        placed_item = {
            "primitive": primitive,
            "dx": dx,
            "gate_windows": tuple(_shift_window(window, dx=dx) for window in primitive.access_contract.gate_windows),
            "source_windows": tuple(_shift_window(window, dx=dx) for window in primitive.access_contract.source_windows),
            "drain_windows": tuple(_shift_window(window, dx=dx) for window in primitive.access_contract.drain_windows),
            "diffusion_ports": tuple(_shift_port(port, dx=dx) for port in primitive.access_contract.diffusion_ports),
            "shapes_by_layer": {
                layer: _shift_boxes(boxes, dx=dx)
                for layer, boxes in primitive.layout.shapes_by_layer.items()
            },
            "keepouts_by_layer": {
                layer: _shift_boxes(boxes, dx=dx)
                for layer, boxes in primitive.access_contract.keepouts_by_layer.items()
            },
            "shared_with_left": shared_with_left,
        }
        placed.append(placed_item)
        previous = placed_item
    return tuple(placed)


def _coalesce_cluster_shapes(
    placed: tuple[dict[str, object], ...],
) -> Mapping[str, tuple[BBox, ...]]:
    shapes_by_layer: dict[str, list[BBox]] = {}
    for item in placed:
        for layer, boxes in item["shapes_by_layer"].items():
            shapes_by_layer.setdefault(layer, []).extend(boxes)
    merged: dict[str, tuple[BBox, ...]] = {}
    for layer, boxes in shapes_by_layer.items():
        if layer in {"OD", "NW"}:
            merged[layer] = _coalesce_boxes(tuple(boxes))
        else:
            merged[layer] = tuple(boxes)
    return merged


def _coalesce_cluster_keepouts(
    placed: tuple[dict[str, object], ...],
) -> Mapping[str, tuple[BBox, ...]]:
    keepouts_by_layer: dict[str, list[BBox]] = {}
    for item in placed:
        for layer, boxes in item["keepouts_by_layer"].items():
            keepouts_by_layer.setdefault(layer, []).extend(boxes)
    merged: dict[str, tuple[BBox, ...]] = {}
    for layer, boxes in keepouts_by_layer.items():
        merged[layer] = _coalesce_boxes(tuple(boxes)) if layer in {"OD", "NW"} else tuple(boxes)
    return merged


def _collapse_cluster_diffusion_ports(
    placed: tuple[dict[str, object], ...],
) -> tuple[NativeStdCellPrimitiveDiffusionPort, ...]:
    ports = [port for item in placed for port in item["diffusion_ports"]]
    consumed: set[int] = set()
    collapsed: list[NativeStdCellPrimitiveDiffusionPort] = []
    for index, port in enumerate(ports):
        if index in consumed:
            continue
        shared_index = _find_matching_shared_port_index(ports, index, consumed)
        if shared_index is None:
            collapsed.append(port)
            consumed.add(index)
            continue
        other = ports[shared_index]
        merged_bbox = _union_bbox((port.bbox_um, other.bbox_um))
        collapsed.append(
            NativeStdCellPrimitiveDiffusionPort(
                side="center",
                net_role="shared",
                bbox_um=merged_bbox,
                merge_key=f"{port.merge_key}|{other.merge_key}",
                supports_merge=True,
            )
        )
        consumed.add(index)
        consumed.add(shared_index)
    return tuple(sorted(collapsed, key=lambda port: (port.bbox_um[0], port.side)))


def _find_matching_shared_port_index(
    ports: list[NativeStdCellPrimitiveDiffusionPort],
    index: int,
    consumed: set[int],
) -> int | None:
    port = ports[index]
    if not port.supports_merge or port.side not in {"left", "right"}:
        return None
    expected_side = "left" if port.side == "right" else "right"
    for candidate_index in range(index + 1, len(ports)):
        if candidate_index in consumed:
            continue
        candidate = ports[candidate_index]
        if not candidate.supports_merge or candidate.side != expected_side:
            continue
        if _bbox_same(port.bbox_um, candidate.bbox_um):
            return candidate_index
    return None


def _can_share_diffusion(
    left: NativeStdCellMosPrimitive,
    right: NativeStdCellMosPrimitive,
) -> bool:
    if left.spec.device_type != right.spec.device_type or left.spec.row != right.spec.row:
        return False
    return (
        left.spec.shared_diffusion_role in {"right_shared", "both_shared"}
        and right.spec.shared_diffusion_role in {"left_shared", "both_shared"}
    )


def _find_port(
    ports: tuple[NativeStdCellPrimitiveDiffusionPort, ...],
    *,
    side: str,
) -> NativeStdCellPrimitiveDiffusionPort:
    for port in ports:
        if port.side == side:
            return port
    raise KeyError(f"diffusion port with side {side!r} not found")


def _shift_boxes(
    boxes: tuple[BBox, ...],
    *,
    dx: float = 0.0,
    dy: float = 0.0,
) -> tuple[BBox, ...]:
    return tuple(
        (
            float(box[0]) + float(dx),
            float(box[1]) + float(dy),
            float(box[2]) + float(dx),
            float(box[3]) + float(dy),
        )
        for box in boxes
    )


def _coalesce_boxes(
    boxes: tuple[BBox, ...],
    *,
    tol: float = 1e-9,
) -> tuple[BBox, ...]:
    if not boxes:
        return ()
    ordered = sorted(
        ((float(box[0]), float(box[1]), float(box[2]), float(box[3])) for box in boxes),
        key=lambda box: (box[1], box[3], box[0], box[2]),
    )
    merged: list[BBox] = []
    for box in ordered:
        if not merged:
            merged.append(box)
            continue
        prev = merged[-1]
        same_span = abs(prev[1] - box[1]) <= tol and abs(prev[3] - box[3]) <= tol
        touches = box[0] <= prev[2] + tol
        if same_span and touches:
            merged[-1] = (
                min(prev[0], box[0]),
                prev[1],
                max(prev[2], box[2]),
                prev[3],
            )
        else:
            merged.append(box)
    return tuple(merged)


def _shift_window(
    window: NativeStdCellPrimitiveAccessWindow,
    *,
    dx: float = 0.0,
    dy: float = 0.0,
) -> NativeStdCellPrimitiveAccessWindow:
    return NativeStdCellPrimitiveAccessWindow(
        terminal=window.terminal,
        side=window.side,
        layer=window.layer,
        center_xy_um=(float(window.center_xy_um[0]) + float(dx), float(window.center_xy_um[1]) + float(dy)),
        bbox_um=_shift_boxes((window.bbox_um,), dx=dx, dy=dy)[0],
        legal=window.legal,
    )


def _shift_port(
    port: NativeStdCellPrimitiveDiffusionPort,
    *,
    dx: float = 0.0,
    dy: float = 0.0,
) -> NativeStdCellPrimitiveDiffusionPort:
    return NativeStdCellPrimitiveDiffusionPort(
        side=port.side,
        net_role=port.net_role,
        bbox_um=_shift_boxes((port.bbox_um,), dx=dx, dy=dy)[0],
        merge_key=port.merge_key,
        supports_merge=port.supports_merge,
    )


def _merge_od_boxes(
    boxes: tuple[BBox, ...],
    shared_x0: float,
    shared_x1: float,
    active_bottom: float,
    active_top: float,
) -> tuple[BBox, ...]:
    od_boxes = [box for box in boxes if _bbox_matches_vertical_span(box, active_bottom, active_top)]
    if len(od_boxes) < 2:
        return boxes
    merged_box = (
        min(float(box[0]) for box in od_boxes),
        float(active_bottom),
        max(float(box[2]) for box in od_boxes),
        float(active_top),
    )
    preserved = [box for box in boxes if box not in od_boxes]
    if shared_x1 > shared_x0:
        merged_box = (
            min(merged_box[0], float(shared_x0)),
            merged_box[1],
            max(merged_box[2], float(shared_x1)),
            merged_box[3],
        )
    preserved.append(merged_box)
    return tuple(preserved)


def _bbox_matches_vertical_span(
    bbox: BBox,
    bottom: float,
    top: float,
    *,
    tol: float = 1e-9,
) -> bool:
    return abs(float(bbox[1]) - float(bottom)) <= tol and abs(float(bbox[3]) - float(top)) <= tol


def _bbox_same(
    left: BBox,
    right: BBox,
    *,
    tol: float = 1e-9,
) -> bool:
    return all(abs(float(a) - float(b)) <= tol for a, b in zip(left, right))


def _union_bbox(boxes: tuple[BBox, ...]) -> BBox:
    if not boxes:
        raise ValueError("cannot build union bbox from empty box set")
    return (
        min(float(box[0]) for box in boxes),
        min(float(box[1]) for box in boxes),
        max(float(box[2]) for box in boxes),
        max(float(box[3]) for box in boxes),
    )


def build_native_stdcell_mos_primitive(
    spec: NativeStdCellMosPrimitiveSpec,
    *,
    fin_pitch_um: float = 0.03,
    gate_pitch_um: float = 0.09,
    gate_width_um: float = 0.03,
    diffusion_height_um: float = 0.08,
    diffusion_extension_um: float = 0.05,
    contact_keepout_um: float = 0.03,
) -> NativeStdCellMosPrimitive:
    """Build a compact abstract transistor primitive contract.

    The generated object is intentionally simple:
    - one OD strip
    - one or more PO gate stripes
    - explicit left/right diffusion ports
    - explicit gate/source/drain access windows

    The goal is to freeze the interface between stdcell primitive generation
    and later placement/routing SMT, not to replace a signoff-quality device
    generator yet.
    """

    _validate_primitive_spec(spec)

    active_width_um = max((int(spec.nf) - 1) * float(gate_pitch_um) + float(gate_width_um) + 2.0 * float(diffusion_extension_um), float(gate_width_um) + 2.0 * float(diffusion_extension_um))
    active_height_um = float(diffusion_height_um)
    active_bbox = (0.0, 0.0, active_width_um, active_height_um)

    gate_boxes: list[BBox] = []
    gate_windows: list[NativeStdCellPrimitiveAccessWindow] = []
    for gate_index in range(int(spec.nf)):
        gate_x = float(diffusion_extension_um) + gate_index * float(gate_pitch_um)
        gate_box = (
            gate_x,
            -0.02,
            gate_x + float(gate_width_um),
            active_height_um + 0.02,
        )
        gate_boxes.append(gate_box)
        gate_windows.append(
            NativeStdCellPrimitiveAccessWindow(
                terminal="G",
                side="center",
                layer="PO",
                center_xy_um=((gate_box[0] + gate_box[2]) / 2.0, gate_box[3]),
                bbox_um=gate_box,
            )
        )

    left_port_bbox = (active_bbox[0], active_bbox[1], active_bbox[0] + float(diffusion_extension_um), active_bbox[3])
    right_port_bbox = (active_bbox[2] - float(diffusion_extension_um), active_bbox[1], active_bbox[2], active_bbox[3])

    source_side, drain_side = _sd_role_by_device_and_share(spec)
    source_bbox = left_port_bbox if source_side == "left" else right_port_bbox
    drain_bbox = right_port_bbox if drain_side == "right" else left_port_bbox

    source_windows = (
        NativeStdCellPrimitiveAccessWindow(
            terminal="S",
            side=source_side,
            layer="M0",
            center_xy_um=((source_bbox[0] + source_bbox[2]) / 2.0, (source_bbox[1] + source_bbox[3]) / 2.0),
            bbox_um=source_bbox,
        ),
    )
    drain_windows = (
        NativeStdCellPrimitiveAccessWindow(
            terminal="D",
            side=drain_side,
            layer="M0",
            center_xy_um=((drain_bbox[0] + drain_bbox[2]) / 2.0, (drain_bbox[1] + drain_bbox[3]) / 2.0),
            bbox_um=drain_bbox,
        ),
    )

    diffusion_ports = (
        NativeStdCellPrimitiveDiffusionPort(
            side="left",
            net_role="source" if source_side == "left" else "drain",
            bbox_um=left_port_bbox,
            merge_key=f"{spec.device_type}:{spec.row}:left",
            supports_merge=spec.shared_diffusion_role in {"left_shared", "both_shared"},
        ),
        NativeStdCellPrimitiveDiffusionPort(
            side="right",
            net_role="drain" if drain_side == "right" else "source",
            bbox_um=right_port_bbox,
            merge_key=f"{spec.device_type}:{spec.row}:right",
            supports_merge=spec.shared_diffusion_role in {"right_shared", "both_shared"},
        ),
    )

    keepouts = {
        "OD": (_expand_bbox(active_bbox, contact_keepout_um),),
        "PO": tuple(_expand_bbox(box, contact_keepout_um) for box in gate_boxes),
    }
    if spec.device_type == "pmos":
        keepouts["NW"] = (_expand_bbox(active_bbox, 0.12),)

    layout = NativeStdCellMosPrimitiveLayout(
        cell_bbox_um=_expand_bbox(active_bbox, 0.02),
        shapes_by_layer={
            "OD": (active_bbox,),
            "PO": tuple(gate_boxes),
        },
        metadata={
            "abstract_only": True,
            "device_type": spec.device_type,
            "row": spec.row,
            "shared_diffusion_role": spec.shared_diffusion_role,
        },
    )
    contract = NativeStdCellMosPrimitiveAccessContract(
        gate_windows=tuple(gate_windows),
        source_windows=source_windows,
        drain_windows=drain_windows,
        diffusion_ports=diffusion_ports,
        keepouts_by_layer=keepouts,
        lvs_recognition_hints={
            "device_type": spec.device_type,
            "nf": spec.nf,
            "nfin": spec.nfin,
            "vt_flavor": spec.vt_flavor,
            "requires_foundry_alignment": True,
        },
        metadata={
            "abstract_only": True,
            "contact_style": spec.contact_style,
            "dummy_style": spec.dummy_style,
        },
    )
    return NativeStdCellMosPrimitive(spec=spec, layout=layout, access_contract=contract)


def _validate_primitive_spec(spec: NativeStdCellMosPrimitiveSpec) -> None:
    if spec.device_type not in {"nmos", "pmos"}:
        raise ValueError(f"unsupported device_type {spec.device_type!r}")
    if spec.row not in {"nrow", "prow"}:
        raise ValueError(f"unsupported row {spec.row!r}")
    if int(spec.nf) <= 0:
        raise ValueError("nf must be positive")
    if int(spec.nfin) <= 0:
        raise ValueError("nfin must be positive")
    if spec.shared_diffusion_role not in {"isolated", "left_shared", "right_shared", "both_shared"}:
        raise ValueError(f"unsupported shared_diffusion_role {spec.shared_diffusion_role!r}")
    if spec.contact_style not in {"none", "left", "right", "both"}:
        raise ValueError(f"unsupported contact_style {spec.contact_style!r}")


def _sd_role_by_device_and_share(
    spec: NativeStdCellMosPrimitiveSpec,
) -> tuple[str, str]:
    """Return (source_side, drain_side) for a compact stdcell primitive."""
    if spec.shared_diffusion_role == "left_shared":
        return ("left", "right")
    if spec.shared_diffusion_role == "right_shared":
        return ("left", "right")
    if spec.shared_diffusion_role == "both_shared":
        return ("left", "right")
    return ("left", "right")


def _expand_bbox(
    bbox: BBox,
    margin_um: float,
) -> BBox:
    return (
        float(bbox[0]) - float(margin_um),
        float(bbox[1]) - float(margin_um),
        float(bbox[2]) + float(margin_um),
        float(bbox[3]) + float(margin_um),
    )
