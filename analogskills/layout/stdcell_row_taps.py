"""Row-level body-tap planning for native standard-cell rows."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from analogskills.layout.power import SupplyTapSpec, build_supply_tap_plan_from_specs
from analogskills.pdk import PdkConfig

from .stdcell_primitives import NativeStdCellFloorplan


@dataclass(frozen=True)
class NativeStdCellRowTapRequirement:
    net: str
    kind: str
    row: str
    source_devices: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class NativeStdCellRowTapSite:
    net: str
    kind: str
    side: str
    center_xy_um: tuple[float, float]
    active_bbox_um: tuple[float, float, float, float]
    keepout_bbox_um: tuple[float, float, float, float]
    rail_layer: str


@dataclass(frozen=True)
class NativeStdCellRowTapPlan:
    requirements: tuple[NativeStdCellRowTapRequirement, ...]
    sites: tuple[NativeStdCellRowTapSite, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def supply_tap_specs(self) -> tuple[SupplyTapSpec, ...]:
        return tuple(
            SupplyTapSpec(site.net, site.kind, site.center_xy_um, site.active_bbox_um, site.rail_layer)
            for site in self.sites
        )

    def to_oa_plan(
        self,
        pdk: PdkConfig,
        *,
        lib: str,
        cell: str,
        view: str = "layout",
    ):
        return build_supply_tap_plan_from_specs(
            self.supply_tap_specs(),
            pdk,
            lib=lib,
            cell=cell,
            view=view,
            output="oa",
        )


def build_native_stdcell_row_tap_requirements(device_plan: Any) -> tuple[NativeStdCellRowTapRequirement, ...]:
    requirements: dict[tuple[str, str], set[str]] = {}
    for inst in tuple(getattr(device_plan, "instances", ())):
        logical_name = str(getattr(inst, "logical_name", "")).lower()
        kind = _body_tap_kind_for_device(logical_name)
        if kind == "":
            continue
        row = "pmos" if kind == "nwell" else "nmos"
        net = _body_net_for_instance(inst, kind)
        if not net:
            continue
        requirements.setdefault((net, kind), set()).add(str(getattr(inst, "name", "")))
    result = []
    for (net, kind), source_devices in sorted(requirements.items()):
        row = "pmos" if kind == "nwell" else "nmos"
        result.append(
            NativeStdCellRowTapRequirement(
                net=net,
                kind=kind,
                row=row,
                source_devices=tuple(sorted(device for device in source_devices if device)),
                reason=f"{row} row bulk closure must be provided at row level",
            )
        )
    return tuple(result)


def plan_native_stdcell_row_taps(
    floorplan: NativeStdCellFloorplan,
    device_plan: Any,
    pdk: PdkConfig,
    *,
    side: str = "left",
    edge_margin_um: float = 0.10,
    tap_width_um: float = 0.24,
    tap_height_um: float = 0.24,
    keepout_margin_um: float = 0.18,
) -> NativeStdCellRowTapPlan:
    requirements = build_native_stdcell_row_tap_requirements(device_plan)
    left_x, bottom_y, right_x, top_y = floorplan.cell_bbox_um()
    if side not in {"left", "right"}:
        raise ValueError("side must be 'left' or 'right'")
    if side == "left":
        cx = left_x - edge_margin_um - tap_width_um / 2.0
    else:
        cx = right_x + edge_margin_um + tap_width_um / 2.0
    rail_layer = floorplan.template.pin_layers.get("VDD", "M2")
    sites: list[NativeStdCellRowTapSite] = []
    for requirement in requirements:
        cy = _tap_center_y_for_requirement(floorplan, requirement)
        active_bbox = _centered_bbox((cx, cy), tap_width_um, tap_height_um)
        keepout_bbox = _expand_bbox(active_bbox, keepout_margin_um)
        sites.append(
            NativeStdCellRowTapSite(
                net=requirement.net,
                kind=requirement.kind,
                side=side,
                center_xy_um=(cx, cy),
                active_bbox_um=active_bbox,
                keepout_bbox_um=keepout_bbox,
                rail_layer=rail_layer,
            )
        )
    metadata = {
        "row_bbox_um": (left_x, bottom_y, right_x, top_y),
        "tap_side": side,
        "requires_row_insertion": bool(requirements),
        "omits_embedded_taps": True,
        "tap_count": len(sites),
    }
    return NativeStdCellRowTapPlan(tuple(requirements), tuple(sites), metadata)


def _body_tap_kind_for_device(logical_name: str) -> str:
    if logical_name == "nmos" or logical_name.startswith("nmos"):
        return "substrate"
    if logical_name == "pmos" or logical_name.startswith("pmos"):
        return "nwell"
    return ""


def _body_net_for_instance(inst: Any, kind: str) -> str:
    connections = getattr(inst, "connections", {})
    if not isinstance(connections, Mapping):
        return ""
    body_net = str(connections.get("B", ""))
    if body_net:
        return body_net
    fallback_terminal = "S" if kind == "nwell" else "S"
    return str(connections.get(fallback_terminal, ""))


def _tap_center_y_for_requirement(
    floorplan: NativeStdCellFloorplan,
    requirement: NativeStdCellRowTapRequirement,
) -> float:
    if requirement.kind == "nwell":
        return float(floorplan.template.row_y_um["pmos"])
    return float(floorplan.template.row_y_um["nmos"])


def _centered_bbox(
    center: tuple[float, float],
    width_um: float,
    height_um: float,
) -> tuple[float, float, float, float]:
    cx, cy = center
    return (
        float(cx - width_um / 2.0),
        float(cy - height_um / 2.0),
        float(cx + width_um / 2.0),
        float(cy + height_um / 2.0),
    )


def _expand_bbox(
    bbox: tuple[float, float, float, float],
    margin_um: float,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    return (
        float(x0 - margin_um),
        float(y0 - margin_um),
        float(x1 + margin_um),
        float(y1 + margin_um),
    )
