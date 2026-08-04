"""Helpers for hierarchy-style merge-back of ArrayAPI companion layouts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from analogskills.pdk import PdkConfig

if TYPE_CHECKING:
    from analogskills.eda.oa import OaWritePlan


@dataclass(frozen=True)
class NativeStdCellArrayApiChildLayout:
    instance_name: str
    lib: str
    cell: str
    view: str
    bbox: tuple[float, float, float, float]

    @property
    def width_um(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height_um(self) -> float:
        return self.bbox[3] - self.bbox[1]


def build_native_stdcell_arrayapi_mergeback_wrapper(
    *,
    pdk: PdkConfig,
    lib: str,
    cell: str,
    pmos_child: NativeStdCellArrayApiChildLayout,
    nmos_child: NativeStdCellArrayApiChildLayout,
    gap_um: float = 0.20,
    margin_um: float = 0.08,
    align: str = "center",
    emit_power_rails: bool = False,
    emit_power_pins: bool = False,
    power_layer: str = "M2",
    rail_width_um: float = 0.06,
    rail_margin_um: float = 0.18,
    power_pin_width_um: float = 0.32,
    emit_signal_pins: bool = False,
    signal_layer: str = "M2",
    signal_pin_size_um: float = 0.08,
    signal_pin_names: tuple[str, ...] = ("A", "B", "Z"),
    signal_pin_x_fractions: tuple[float, ...] = (0.22, 0.5, 0.78),
    signal_margin_um: float = 0.18,
    emit_chip_boundary: bool = False,
) -> OaWritePlan:
    """Create a top-level wrapper that instantiates PMOS/NMOS companion layouts.

    This is intentionally minimal: it only composes the child layout views and
    emits outer boundary markers. Pins/terminals are expected to be added by a
    higher-level wrapper because current ArrayAPI-generated child layouts do not
    expose reusable layout pins.
    """

    from analogskills.eda.oa import OaCellView, OaInstance, OaRect, OaWritePlan

    pmos_x0 = 0.0
    pmos_y0 = nmos_child.height_um + gap_um
    if align == "center":
        nmos_x0 = max(0.0, (pmos_child.width_um - nmos_child.width_um) / 2.0)
    elif align == "left":
        nmos_x0 = 0.0
    else:
        raise ValueError("align must be 'center' or 'left'")
    nmos_y0 = 0.0

    x_pmos, y_pmos = pdk.rules.snap_point_um((pmos_x0 - pmos_child.bbox[0], pmos_y0 - pmos_child.bbox[1]))
    x_nmos, y_nmos = pdk.rules.snap_point_um((nmos_x0 - nmos_child.bbox[0], nmos_y0 - nmos_child.bbox[1]))

    width_um = max(pmos_child.width_um, nmos_x0 + nmos_child.width_um)
    height_um = pmos_y0 + pmos_child.height_um
    extra_top = margin_um
    if emit_signal_pins:
        extra_top += signal_margin_um
    if emit_power_rails:
        extra_top += rail_margin_um
    extra_bottom = rail_margin_um if emit_power_rails else margin_um
    bbox = pdk.rules.snap_bbox_um(
        (-margin_um, -extra_bottom, width_um + margin_um, height_um + extra_top),
        mode="outward",
    )
    nets_list: list[str] = []
    rects_list = [
        OaRect("prBoundary", "boundary", bbox, ""),
    ]
    if emit_chip_boundary:
        rects_list.append(OaRect("chipBoundary", "chipBoundary", bbox, ""))
    pins_list = []
    if emit_power_rails:
        y_vss = pdk.rules.snap_point_um((0.0, bbox[1] + rail_margin_um / 2.0))[1]
        y_vdd = pdk.rules.snap_point_um((0.0, bbox[3] - rail_margin_um / 2.0))[1]
        rail_width = pdk.rules.snap_dimension_um(rail_width_um)
        from analogskills.eda.oa import OaPin

        nets_list.extend(("VDD", "VSS"))
        rects_list.extend(
            (
                OaRect(power_layer, "drawing", (bbox[0], y_vdd - rail_width / 2.0, bbox[2], y_vdd + rail_width / 2.0), "VDD"),
                OaRect(power_layer, "drawing", (bbox[0], y_vss - rail_width / 2.0, bbox[2], y_vss + rail_width / 2.0), "VSS"),
            )
        )
        if emit_power_pins:
            pin_w = pdk.rules.snap_dimension_um(power_pin_width_um)
            half_w = pin_w / 2.0
            half_h = rail_width / 2.0
            x_pin = pdk.rules.snap_point_um((bbox[0] + half_w + margin_um, 0.0))[0]
            pins_list.extend(
                (
                    OaPin("VDD", "VDD", "inputOutput", power_layer, (x_pin - half_w, y_vdd - half_h, x_pin + half_w, y_vdd + half_h)),
                    OaPin("VSS", "VSS", "inputOutput", power_layer, (x_pin - half_w, y_vss - half_h, x_pin + half_w, y_vss + half_h)),
                )
            )
    if emit_signal_pins:
        from analogskills.eda.oa import OaPin

        pin_size = pdk.rules.snap_dimension_um(signal_pin_size_um)
        half = pin_size / 2.0
        signal_y = pdk.rules.snap_point_um((0.0, height_um + signal_margin_um / 2.0))[1]
        for name, frac in zip(signal_pin_names, signal_pin_x_fractions):
            x = pdk.rules.snap_point_um((bbox[0] + (bbox[2] - bbox[0]) * frac, 0.0))[0]
            pins_list.append(
                OaPin(
                    name,
                    name,
                    "output" if name == "Z" else "input",
                    signal_layer,
                    (x - half, signal_y - half, x + half, signal_y + half),
                )
            )
            nets_list.append(name)
    nets = tuple(dict.fromkeys(nets_list))
    pins = tuple(pins_list)
    return OaWritePlan(
        OaCellView(lib, cell, "layout", "maskLayout"),
        nets=nets,
        pins=pins,
        instances=(
            OaInstance(pmos_child.instance_name, pmos_child.lib, pmos_child.cell, pmos_child.view, (x_pmos, y_pmos), "R0"),
            OaInstance(nmos_child.instance_name, nmos_child.lib, nmos_child.cell, nmos_child.view, (x_nmos, y_nmos), "R0"),
        ),
        rects=tuple(rects_list),
    )
