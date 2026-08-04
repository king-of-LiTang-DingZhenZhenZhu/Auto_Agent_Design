"""Reusable primitive/carrier helpers for native standard-cell style layouts."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, TYPE_CHECKING

from analogskills.layout.placement import Placement

if TYPE_CHECKING:
    from analogskills.pcell import PCellPin
    from analogskills.pcell.generation import PCellLayoutPlan
    from .stdcell_carriers import NativeStdCellCarrier
    from .stdcell_drawn_primitives import NativeStdCellMosPrimitiveSpec


@dataclass(frozen=True)
class NativeStdCellTemplate:
    name: str
    placement_columns: tuple[int, ...]
    pin_columns: tuple[int, ...]
    row_y_um: Mapping[str, float]
    rail_y_um: Mapping[str, float]
    band_y_um: Mapping[str, float]
    device_origin_x_um: float
    device_pitch_um: float
    pin_origin_x_um: float
    pin_pitch_um: float
    left_boundary_um: float
    right_boundary_um: float
    bottom_boundary_um: float
    top_boundary_um: float
    route_layers: tuple[str, ...] = ("M0", "M1", "M2")
    pin_layers: Mapping[str, str] = field(default_factory=dict)
    boundary_pin_size_um: float = 0.08
    rail_width_um: float = 0.06
    signal_width_um: float = 0.06
    gate_poly_width_um: float = 0.03
    body_tap_x_um: float = -0.42
    body_tap_nw_y_um: float = 0.75
    body_tap_sub_y_um: float = 0.0

    def device_x(self, column: int) -> float:
        return float(self.device_origin_x_um + float(column) * self.device_pitch_um)

    def pin_x(self, column: int) -> float:
        return float(self.pin_origin_x_um + float(column) * self.pin_pitch_um)

    def cell_bbox_um(self) -> tuple[float, float, float, float]:
        return (
            float(self.left_boundary_um),
            float(self.bottom_boundary_um),
            float(self.right_boundary_um),
            float(self.top_boundary_um),
        )

    @property
    def cell_height_um(self) -> float:
        return float(self.top_boundary_um - self.bottom_boundary_um)


@dataclass(frozen=True)
class NativeStdCellFloorplan:
    template: NativeStdCellTemplate
    placements: tuple[Placement, ...]
    device_columns: Mapping[str, int]
    device_orientations: Mapping[str, str]
    pin_columns: Mapping[str, int]
    cost: float
    metadata: Mapping[str, object] = field(default_factory=dict)

    def pin_x(self, net: str) -> float:
        column = self.pin_columns.get(net)
        if column is None:
            raise KeyError(f"pin column not defined for net {net!r}")
        return self.template.pin_x(int(column))

    def cell_bbox_um(self) -> tuple[float, float, float, float]:
        return self.template.cell_bbox_um()


@dataclass(frozen=True)
class NativeStdCellAccessPin:
    xy_um: tuple[float, float]
    bbox_um: tuple[float, float, float, float]
    layer: str
    source: str
    lvs_safe: bool = True
    access_priority: int = 50
    center_um: tuple[float, float] | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeStdCellAccessCatalog:
    pins_by_instance_terminal: Mapping[tuple[str, str], tuple[object, ...]]
    breakout_by_instance_terminal: Mapping[tuple[str, str], object]

    def pins_for(self, instance: str, terminal: str) -> tuple[object, ...]:
        return tuple(self.pins_by_instance_terminal.get((str(instance), str(terminal)), ()))

    def breakout_for(self, instance: str, terminal: str) -> object:
        return self.breakout_by_instance_terminal[(str(instance), str(terminal))]

    @classmethod
    def from_primitive_clusters(
        cls,
        floorplan: "NativeStdCellFloorplan",
        carriers: tuple["NativeStdCellCarrier", ...],
        *,
        nfin_by_model: Mapping[str, int] | None = None,
        pcell_plan: "PCellLayoutPlan | Any | None" = None,
        pdk: Any | None = None,
        calibration_cache: Any | None = None,
    ) -> "NativeStdCellAccessCatalog":
        from .stdcell_drawn_primitives import build_native_stdcell_primitive_specs_from_carrier
        from analogskills.pcell import PCellTerminalAccessor

        placement_by_device = {str(placement.name): placement for placement in floorplan.placements}
        pins_by_key: dict[tuple[str, str], tuple[NativeStdCellAccessPin, ...]] = {}
        breakout_by_key: dict[tuple[str, str], NativeStdCellAccessPin] = {}
        instance_by_name = {
            str(instance.name): instance
            for instance in tuple(getattr(pcell_plan, "instances", ()))
        }
        accessor = None
        if pdk is not None and calibration_cache is not None and instance_by_name:
            accessor = PCellTerminalAccessor(pdk, calibration_cache=calibration_cache)

        def add_pin(key: tuple[str, str], pin: object) -> None:
            existing = list(pins_by_key.get(key, ()))
            existing.append(pin)
            pins_by_key[key] = tuple(existing)
            priority = int(getattr(pin, "access_priority", 50))
            current = breakout_by_key.get(key)
            if current is None or priority < int(getattr(current, "access_priority", 50)):
                breakout_by_key[key] = pin

        def add_pcell_pins(instance_name: str) -> bool:
            if accessor is None:
                return False
            instance = instance_by_name.get(str(instance_name))
            if instance is None:
                return False
            added = False
            for terminal, preferred_layers in (
                ("G", ("PO",)),
                ("S", ("M0", "MD", "OD")),
                ("D", ("M0", "MD", "OD")),
            ):
                pins = accessor.get_terminal_pins(instance, terminal, preferred_layers=preferred_layers)
                if not pins:
                    continue
                key = (str(instance_name), str(terminal))
                for pin in pins:
                    add_pin(key, pin)
                breakout = accessor.select_terminal_breakout(
                    instance,
                    terminal,
                    require_lvs_safe=True,
                    preferred_layers=preferred_layers,
                )
                breakout_by_key[key] = breakout
                added = True
            return added

        for carrier in carriers:
            specs = build_native_stdcell_primitive_specs_from_carrier(
                carrier,
                nfin_by_model=nfin_by_model,
            )
            primitive_x = 0.0
            for device, spec in zip(carrier.devices, specs):
                placement = placement_by_device.get(str(device.device))
                if placement is None:
                    continue
                if add_pcell_pins(str(device.device)):
                    continue
                primitive = _build_positioned_primitive_for_spec(
                    spec,
                    dx=primitive_x,
                )
                _append_primitive_accesses(
                    pins_by_key,
                    breakout_by_key,
                    primitive=primitive,
                    placement=placement,
                )
                primitive_x = _next_cluster_primitive_origin_x(
                    primitive,
                    carrier=carrier,
                    current_origin_x=primitive_x,
                )
        return cls(
            pins_by_instance_terminal={key: tuple(value) for key, value in pins_by_key.items()},
            breakout_by_instance_terminal=breakout_by_key,
        )


def _build_positioned_primitive_for_spec(
    spec: "NativeStdCellMosPrimitiveSpec",
    *,
    dx: float,
):
    from .stdcell_drawn_primitives import build_native_stdcell_mos_primitive

    primitive = build_native_stdcell_mos_primitive(spec)
    return {
        "spec": spec,
        "primitive": primitive,
        "dx": float(dx),
    }


def _next_cluster_primitive_origin_x(
    previous_primitive: Mapping[str, object],
    *,
    carrier: "NativeStdCellCarrier",
    current_origin_x: float,
) -> float:
    previous = previous_primitive["primitive"]
    prev_spec = previous_primitive["spec"]
    prev_bbox = previous.layout.cell_bbox_um
    step = float(prev_bbox[2] - prev_bbox[0]) + 0.02
    if carrier.kind == "series":
        step -= 0.05
    if prev_spec.shared_diffusion_role in {"right_shared", "both_shared"}:
        step -= 0.05
    return float(current_origin_x + max(step, 0.02))


def _append_primitive_accesses(
    pins_by_key: dict[tuple[str, str], tuple[NativeStdCellAccessPin, ...] | list[NativeStdCellAccessPin]],
    breakout_by_key: dict[tuple[str, str], NativeStdCellAccessPin],
    *,
    primitive: Mapping[str, object],
    placement: Placement,
) -> None:
    spec = primitive["spec"]
    generated = primitive["primitive"]
    dx = float(primitive["dx"])
    orient = str(getattr(placement, "orient", "R0"))
    origin_xy = (float(placement.x_um), float(placement.y_um))

    def add_pin(terminal: str, pin: NativeStdCellAccessPin) -> None:
        key = (str(spec.name), terminal)
        existing = list(pins_by_key.get(key, ()))
        existing.append(pin)
        pins_by_key[key] = tuple(existing)
        if key not in breakout_by_key or pin.access_priority < breakout_by_key[key].access_priority:
            breakout_by_key[key] = pin

    for window in generated.access_contract.gate_windows:
        pin = _absolute_access_pin(
            window.layer,
            _shift_bbox(window.bbox_um, dx=dx),
            _shift_point(window.center_xy_um, dx=dx),
            orient=orient,
            origin_xy=origin_xy,
            source="primitive_gate",
            access_priority=10,
            metadata={"side": window.side, "legal": window.legal},
        )
        add_pin("G", pin)
    for terminal, windows in (("S", generated.access_contract.source_windows), ("D", generated.access_contract.drain_windows)):
        for window in windows:
            pin = _absolute_access_pin(
                window.layer,
                _shift_bbox(window.bbox_um, dx=dx),
                _shift_point(window.center_xy_um, dx=dx),
                orient=orient,
                origin_xy=origin_xy,
                source="primitive_sd",
                access_priority=0 if window.side in {"left", "right"} else 5,
                metadata={"side": window.side, "legal": window.legal},
            )
            add_pin(terminal, pin)


def _absolute_access_pin(
    layer: str,
    bbox_um: tuple[float, float, float, float],
    center_xy_um: tuple[float, float],
    *,
    orient: str,
    origin_xy: tuple[float, float],
    source: str,
    access_priority: int,
    metadata: Mapping[str, object] | None = None,
) -> NativeStdCellAccessPin:
    abs_bbox = _absolute_bbox(origin_xy, bbox_um, orient)
    abs_xy = _absolute_point(origin_xy, center_xy_um, orient)
    return NativeStdCellAccessPin(
        xy_um=abs_xy,
        bbox_um=abs_bbox,
        layer=str(layer),
        source=str(source),
        lvs_safe=True,
        access_priority=int(access_priority),
        center_um=abs_xy,
        metadata=dict(metadata or {}),
    )


def _shift_bbox(
    bbox: tuple[float, float, float, float],
    *,
    dx: float = 0.0,
    dy: float = 0.0,
) -> tuple[float, float, float, float]:
    return (
        float(bbox[0]) + float(dx),
        float(bbox[1]) + float(dy),
        float(bbox[2]) + float(dx),
        float(bbox[3]) + float(dy),
    )


def _shift_point(
    xy: tuple[float, float],
    *,
    dx: float = 0.0,
    dy: float = 0.0,
) -> tuple[float, float]:
    return (float(xy[0]) + float(dx), float(xy[1]) + float(dy))


def _absolute_point(
    origin_xy: tuple[float, float],
    local_xy: tuple[float, float],
    orient: str,
) -> tuple[float, float]:
    x, y = local_xy
    if orient == "MY":
        px, py = (-x, y)
    elif orient == "MX":
        px, py = (x, -y)
    elif orient == "R180":
        px, py = (-x, -y)
    else:
        px, py = (x, y)
    return (float(origin_xy[0]) + float(px), float(origin_xy[1]) + float(py))


def build_n7_native_stdcell_template(*, max_device_columns: int = 4, max_pin_columns: int = 4) -> NativeStdCellTemplate:
    if max_device_columns <= 0:
        raise ValueError("max_device_columns must be positive")
    if max_pin_columns <= 0:
        raise ValueError("max_pin_columns must be positive")
    return NativeStdCellTemplate(
        name="n7_2row_native",
        placement_columns=tuple(range(int(max_device_columns))),
        pin_columns=tuple(range(int(max_pin_columns))),
        # Compact two-row template modeled after reference stdcell topology.
        # The template owns the routing fabric and keeps all top-level access
        # openings inside the cell boundary instead of reserving negative-x
        # "hang-off" space for standalone PCell diagnostics.
        row_y_um={"nmos": 0.0, "pmos": 0.40},
        rail_y_um={"VSS": -0.10, "VDD": 0.60},
        band_y_um={"gate": 0.16, "internal": 0.08, "output": 0.26},
        # Native N7 MOS PCells extend substantially left of their origin.
        # Shift the placement fabric so the calibrated instance envelope sits
        # inside the stdcell boundary instead of forcing a negative-x boundary.
        device_origin_x_um=0.42,
        device_pitch_um=0.574,
        pin_origin_x_um=0.42,
        pin_pitch_um=0.18,
        left_boundary_um=0.0,
        right_boundary_um=1.40,
        bottom_boundary_um=-0.14,
        top_boundary_um=0.70,
        pin_layers={"A": "M2", "B": "M2", "Z": "M2", "VDD": "M2", "VSS": "M2"},
        body_tap_x_um=0.04,
        body_tap_nw_y_um=0.60,
        body_tap_sub_y_um=-0.10,
    )


def build_native_stdcell_floorplan(
    solution: Any,
    *,
    role_by_device: Mapping[str, str] | None = None,
) -> NativeStdCellFloorplan:
    roles = dict(role_by_device or {})
    columns = solution.device_column_map()
    orientations = solution.device_orientation_map()
    placements = tuple(
        Placement(
            name,
            solution.template.device_x(column),
            float(solution.template.row_y_um[_row_name_for_device(name, roles)]),
            orientations.get(name, "R0"),
            role=roles.get(name, ""),
        )
        for name, column in sorted(columns.items())
    )
    return NativeStdCellFloorplan(
        template=solution.template,
        placements=placements,
        device_columns=columns,
        device_orientations=orientations,
        pin_columns=solution.pin_column_map(),
        cost=float(solution.cost),
        metadata=dict(solution.metadata),
    )


def expand_floorplan_to_calibrated_instance_bboxes(
    floorplan: NativeStdCellFloorplan,
    pcell_plan: Any,
    *,
    calibration_cache: Any | None = None,
    pdk: Any | None = None,
    margin_um: float = 0.0,
) -> NativeStdCellFloorplan:
    """Expand a template bbox so it encloses the calibrated native PCell shapes.

    Advanced-node native PCells often extend well outside the abstract device
    origin/pitch assumptions used by the placement solver.  This helper keeps
    the compact solver result but widens the floorplan boundary to the true
    calibrated instance envelope so downstream pin placement and route bands do
    not get clipped by an undersized template box.
    """

    boxes: list[tuple[float, float, float, float]] = []
    for instance in tuple(getattr(pcell_plan, "instances", ())):
        bbox = _instance_bbox_from_calibration(instance, calibration_cache)
        if bbox is not None:
            boxes.append(bbox)
    if not boxes:
        return floorplan

    left_x, bottom_y, right_x, top_y = floorplan.cell_bbox_um()
    min_x = min([left_x, *(box[0] for box in boxes)]) - float(margin_um)
    min_y = min([bottom_y, *(box[1] for box in boxes)]) - float(margin_um)
    max_x = max([right_x, *(box[2] for box in boxes)]) + float(margin_um)
    max_y = max([top_y, *(box[3] for box in boxes)]) + float(margin_um)
    bbox = (min_x, min_y, max_x, max_y)
    if pdk is not None:
        bbox = pdk.rules.snap_bbox_um(bbox, mode="outward")
    if all(abs(a - b) <= 1e-12 for a, b in zip((left_x, bottom_y, right_x, top_y), bbox)):
        return floorplan

    template = replace(
        floorplan.template,
        left_boundary_um=float(bbox[0]),
        bottom_boundary_um=float(bbox[1]),
        right_boundary_um=float(bbox[2]),
        top_boundary_um=float(bbox[3]),
    )
    metadata = {
        **dict(floorplan.metadata),
        "expanded_to_calibrated_bbox": True,
        "calibrated_instance_bbox_um": bbox,
    }
    return replace(floorplan, template=template, metadata=metadata)


def _row_name_for_device(name: str, roles: Mapping[str, str]) -> str:
    role = str(roles.get(name, ""))
    if role.startswith("pmos"):
        return "pmos"
    return "pmos" if name.upper().startswith("MP") else "nmos"


def _instance_bbox_from_calibration(
    instance: Any,
    calibration_cache: Any | None,
) -> tuple[float, float, float, float] | None:
    if calibration_cache is None:
        return None
    lookup = getattr(calibration_cache, "lookup_instance", None)
    if lookup is None:
        return None
    entry = lookup(instance)
    if entry is None:
        return None
    local_bbox = getattr(entry, "instance_bbox_um", None) or getattr(entry, "bbox_um", None)
    if local_bbox is None:
        return None
    return _absolute_bbox(tuple(float(v) for v in instance.xy_um), tuple(float(v) for v in local_bbox), str(getattr(instance, "orient", "R0")))


def _absolute_bbox(
    origin_xy: tuple[float, float],
    local_bbox: tuple[float, float, float, float],
    orient: str,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = local_bbox
    if orient == "MY":
        points = ((-x1, y0), (-x0, y0), (-x0, y1), (-x1, y1))
    elif orient == "MX":
        points = ((x0, -y1), (x1, -y1), (x1, -y0), (x0, -y0))
    elif orient == "R180":
        points = ((-x1, -y1), (-x0, -y1), (-x0, -y0), (-x1, -y0))
    else:
        points = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    ox, oy = origin_xy
    abs_points = tuple((ox + px, oy + py) for px, py in points)
    xs = tuple(point[0] for point in abs_points)
    ys = tuple(point[1] for point in abs_points)
    return (min(xs), min(ys), max(xs), max(ys))
