from __future__ import annotations

from typing import TYPE_CHECKING

from analogskills.pdk import PdkConfig

if TYPE_CHECKING:
    from .stdcell_primitives import NativeStdCellAccessCatalog, NativeStdCellFloorplan


def native_stdcell_terminal_bbox(
    access_catalog: "NativeStdCellAccessCatalog",
    instance: str,
    terminal: str,
) -> tuple[float, float, float, float] | None:
    pins = access_catalog.pins_for(instance, terminal)
    if not pins:
        return None
    bbox_um = getattr(pins[0], "bbox_um", None)
    if bbox_um is None:
        return None
    return tuple(float(v) for v in bbox_um)


def native_stdcell_sd_access_side(
    floorplan: "NativeStdCellFloorplan",
    instance: str,
    terminal: str,
) -> str:
    orient = str(floorplan.device_orientations.get(instance, "R0"))
    if orient == "MY":
        return "left" if terminal == "D" else "right"
    return "right" if terminal == "D" else "left"


def native_stdcell_terminal_xy(
    access_catalog: "NativeStdCellAccessCatalog",
    floorplan: "NativeStdCellFloorplan",
    pdk: PdkConfig,
    instance: str,
    terminal: str,
) -> tuple[float, float]:
    if terminal in {"S", "D"}:
        bbox_um = native_stdcell_terminal_bbox(access_catalog, instance, terminal)
        if bbox_um is not None:
            via_half = max(float(pdk.rules.min_width_um("VIA0")) / 2.0, 0.01)
            side = native_stdcell_sd_access_side(floorplan, instance, terminal)
            x = bbox_um[0] + via_half if side == "left" else bbox_um[2] - via_half
            y = (bbox_um[1] + bbox_um[3]) / 2.0
            return pdk.rules.snap_point_um((x, y))
    pin = access_catalog.breakout_for(instance, terminal)
    return tuple(float(v) for v in getattr(pin, "xy_um"))
