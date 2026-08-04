"""Pure-Python GDS writer — converts OaWritePlan to GDS without Virtuoso.

Produces a minimal but valid GDS from an OaWritePlan. PCell instances are
written as SREF with placeholder BOUNDARY boxes; routing paths are written
as PATH records; rectangles as BOUNDARY records.

GDS is a binary stream of records:  [len:u16][type:u8][datatype:u8][data...]
Coordinate precision: 1nm (dbu_per_uu=1000).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# GDS record types & data types
# ---------------------------------------------------------------------------
_RECORD = {
    "HEADER": 0x00, "BGNLIB": 0x01, "LIBNAME": 0x02, "UNITS": 0x03,
    "ENDLIB": 0x04, "BGNSTR": 0x05, "STRNAME": 0x06, "ENDSTR": 0x07,
    "BOUNDARY": 0x08, "PATH": 0x09, "SREF": 0x0A, "AREF": 0x0B,
    "TEXT": 0x0C, "LAYER": 0x0D, "DATATYPE": 0x0E, "WIDTH": 0x0F,
    "XY": 0x10, "ENDEL": 0x11, "SNAME": 0x12, "TEXTTYPE": 0x16,
    "STRING": 0x19, "STRANS": 0x1A, "MAG": 0x1B, "BOX": 0x2D,
    "BOXTYPE": 0x2E, "PATHTYPE": 0x21, "PROPATTR": 0x2B, "PROPVALUE": 0x2C,
}
_DT = {
    "NONE": 0x00, "BIT": 0x01, "INT16": 0x02, "INT32": 0x03,
    "REAL8": 0x05, "STRING": 0x06,
}


def _record(name: str, dtype: str, data: bytes = b"") -> bytes:
    rt = _RECORD[name]
    dt = _DT[dtype]
    length = 4 + len(data)
    if length % 2:
        data += b"\x00"
        length += 1
    return struct.pack(">HBB", length, rt, dt) + data


def _int16(val: int) -> bytes:
    return struct.pack(">h", val)


def _uint16(val: int) -> bytes:
    return struct.pack(">H", val)


def _int32(val: int) -> bytes:
    return struct.pack(">i", val)


def _real8(val: float) -> bytes:
    """GDS uses excess-16 floating point, not IEEE 754."""
    if val == 0:
        return b"\x00" * 8
    negative = val < 0
    val = abs(val)
    exp = 0
    while val >= 1:
        val /= 16.0
        exp += 1
    while val < 1 / 16:
        val *= 16.0
        exp -= 1
    mantissa = int(val * (2**56))
    result = struct.pack(">Q", mantissa)
    first = result[0]
    if negative:
        first |= 0x80
    first = (first & 0x7F) | ((exp + 64) << 0 if False else 0)
    exp_byte = (exp + 64) & 0x7F
    if negative:
        exp_byte |= 0x80
    return bytes([exp_byte]) + result[1:]


def _string(s: str) -> bytes:
    b = s.encode("ascii", errors="replace")
    if len(b) % 2:
        b += b"\x00"
    return b


def _timestamp() -> bytes:
    """Returns 12 x int16 for BGNLIB / BGNSTR timestamps (all zeros = valid)."""
    return b"\x00" * 24


# ---------------------------------------------------------------------------
# Layer mapping
# ---------------------------------------------------------------------------
DEFAULT_LAYER_MAP: dict[str, tuple[int, int]] = {
    "OD": (2, 0), "PO": (4, 0), "NW": (3, 0), "CO": (6, 0),
    "M1": (8, 0), "M2": (10, 0), "M3": (12, 0), "M4": (14, 0),
    "M5": (16, 0), "M6": (18, 0), "M7": (20, 0), "M8": (22, 0),
    "M9": (24, 0), "M10": (26, 0),
    "VIA1": (9, 0), "VIA2": (11, 0), "VIA3": (13, 0), "VIA4": (15, 0),
    "VIA5": (17, 0), "VIA6": (19, 0), "VIA7": (21, 0), "VIA8": (23, 0),
    "VIA9": (25, 0),
    "NP": (5, 0), "PP": (5, 1),
    "drawing": (127, 0),
    "pin": (127, 0),
}

PORT_TEXT_LAYER_BY_DRAWING: dict[str, int] = {
    "M1": 131,
    "M2": 132,
    "M3": 133,
    "M4": 134,
    "M5": 135,
    "M6": 136,
    "M7": 137,
    "M8": 138,
    "M9": 139,
    "M10": 140,
    "PO": 149,
}

LABEL_TEXTTYPE_BY_DRAWING: dict[str, int] = {
    "M1": 31,
    "M2": 32,
    "M3": 33,
    "M4": 34,
    "M5": 35,
    "M6": 36,
    "M7": 37,
    "M8": 38,
    "M9": 39,
    "M10": 40,
    "PO": 17,
}


def _resolve_layer(
    layer: str,
    custom_map: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[int, int]:
    if custom_map and layer in custom_map:
        return custom_map[layer]
    return DEFAULT_LAYER_MAP.get(layer, (200, 0))


def _port_text_layer(layer: str) -> int | None:
    return PORT_TEXT_LAYER_BY_DRAWING.get(str(layer).upper())


def _label_texttype(layer: str) -> int | None:
    return LABEL_TEXTTYPE_BY_DRAWING.get(str(layer).upper())


def _via_layer_name(via: Any) -> str:
    layer_name = str(getattr(via, "layer", "") or "").strip()
    if layer_name:
        return layer_name
    via_def = str(getattr(via, "via_def", "") or "").strip()
    return via_def or "VIA1"


def _via_xy_um(via: Any) -> tuple[float, float]:
    xy = getattr(via, "xy", None)
    if isinstance(xy, (tuple, list)) and len(xy) >= 2:
        try:
            return float(xy[0]), float(xy[1])
        except (TypeError, ValueError):
            pass
    return (
        float(getattr(via, "x_um", getattr(via, "x", 0.0)) or 0.0),
        float(getattr(via, "y_um", getattr(via, "y", 0.0)) or 0.0),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GdsWriteConfig:
    """Configuration for GDS output."""

    dbu_per_uu: float = 1000.0  # 1nm resolution
    user_units: float = 1e-6  # 1um = 1 user unit
    layer_map: Mapping[str, tuple[int, int]] | None = None
    top_cell: str = "TOP"
    lib_name: str = "analogskills"


def write_gds(
    plan: Any,  # OaWritePlan or compatible
    path: str | Path,
    config: GdsWriteConfig | None = None,
) -> Path:
    """Write an OaWritePlan to a GDS file.

    Args:
        plan: OaWritePlan with .rects, .paths, .vias, .pins, .instances, .labels
        path: Output GDS file path
        config: GDS write configuration

    Returns:
        The output path
    """
    path = Path(path)
    cfg = config or GdsWriteConfig()
    nm_per_um = cfg.dbu_per_uu  # 1000 means 1um = 1000 database units

    def um_to_db(um: float) -> int:
        return int(round(um * nm_per_um))

    buf = bytearray()

    # --- Library header ---
    buf += _record("HEADER", "INT16", _int16(600))  # GDS version 6.0.0
    buf += _record("BGNLIB", "INT16", _timestamp())
    buf += _record("LIBNAME", "STRING", _string(cfg.lib_name))
    db_per_user = 1.0 / cfg.dbu_per_uu
    db_in_meters = cfg.user_units / cfg.dbu_per_uu
    buf += _record("UNITS", "REAL8", _real8(db_per_user) + _real8(db_in_meters))

    # --- Collect all cells needed ---
    top_cell_name = cfg.top_cell
    if not str(top_cell_name).strip() and hasattr(plan, "cellview") and hasattr(plan.cellview, "cell"):
        top_cell_name = plan.cellview.cell or top_cell_name

    # Build subcell geometries from instances (PCell placeholders)
    subcells: dict[str, list[bytes]] = {}
    if hasattr(plan, "instances") and plan.instances:
        for inst in plan.instances:
            cname = getattr(inst, "cell_name", getattr(inst, "cell", "PCELL"))
            if cname not in subcells:
                subcells[cname] = []
            # PCell placeholder: emit a minimal transistor stack so Calibre can
            # recognize MOS devices without a native PDK streamout.
            w = getattr(inst, "width_um", 0.5)
            h = getattr(inst, "height_um", 0.5)
            is_pmos = "pch" in cname.lower() or "pmos" in cname.lower() or cname.lower().startswith("p")
            x0, y0 = 0, 0
            x1, y1 = um_to_db(w), um_to_db(h)
            if is_pmos:
                nw_layer, nw_dtype = _resolve_layer("NW", cfg.layer_map)
                margin = um_to_db(0.12)
                subcells[cname].append(
                    _record("BOUNDARY", "NONE")
                    + _record("LAYER", "INT16", _int16(nw_layer))
                    + _record("DATATYPE", "INT16", _int16(nw_dtype))
                    + _record("XY", "INT32", _int32(x0 - margin) + _int32(y0 - margin)
                              + _int32(x1 + margin) + _int32(y0 - margin)
                              + _int32(x1 + margin) + _int32(y1 + margin)
                              + _int32(x0 - margin) + _int32(y1 + margin)
                              + _int32(x0 - margin) + _int32(y0 - margin))
                    + _record("ENDEL", "NONE")
                )
            od_layer, od_dtype = _resolve_layer("OD", cfg.layer_map)
            subcells[cname].append(
                _record("BOUNDARY", "NONE")
                + _record("LAYER", "INT16", _int16(od_layer))
                + _record("DATATYPE", "INT16", _int16(od_dtype))
                + _record("XY", "INT32", _int32(x0) + _int32(y0)
                          + _int32(x1) + _int32(y0) + _int32(x1) + _int32(y1)
                          + _int32(x0) + _int32(y1) + _int32(x0) + _int32(y0))
                + _record("ENDEL", "NONE")
            )
            po_layer, po_dtype = _resolve_layer("PO", cfg.layer_map)
            gate_x0 = um_to_db(max(w * 0.4, 0.08))
            gate_x1 = um_to_db(min(w * 0.6, max(w - 0.08, w * 0.6)))
            subcells[cname].append(
                _record("BOUNDARY", "NONE")
                + _record("LAYER", "INT16", _int16(po_layer))
                + _record("DATATYPE", "INT16", _int16(po_dtype))
                + _record("XY", "INT32", _int32(gate_x0) + _int32(y0 - um_to_db(0.08))
                          + _int32(gate_x1) + _int32(y0 - um_to_db(0.08))
                          + _int32(gate_x1) + _int32(y1 + um_to_db(0.08))
                          + _int32(gate_x0) + _int32(y1 + um_to_db(0.08))
                          + _int32(gate_x0) + _int32(y0 - um_to_db(0.08)))
                + _record("ENDEL", "NONE")
            )

    # Write subcell structures
    for cname, elements in subcells.items():
        safe_name = cname[:32]
        buf += _record("BGNSTR", "INT16", _timestamp())
        buf += _record("STRNAME", "STRING", _string(safe_name))
        for elem in elements:
            buf += elem
        buf += _record("ENDSTR", "NONE")

    # --- Top cell ---
    buf += _record("BGNSTR", "INT16", _timestamp())
    buf += _record("STRNAME", "STRING", _string(top_cell_name))

    # Rectangles → BOUNDARY
    if hasattr(plan, "rects") and plan.rects:
        for rect in plan.rects:
            layer_name = getattr(rect, "layer", "M1")
            layer_num, dtype_num = _resolve_layer(layer_name, cfg.layer_map)
            bbox = rect.bbox
            x0, y0 = um_to_db(bbox[0]), um_to_db(bbox[1])
            x1, y1 = um_to_db(bbox[2]), um_to_db(bbox[3])
            buf += (
                _record("BOUNDARY", "NONE")
                + _record("LAYER", "INT16", _int16(layer_num))
                + _record("DATATYPE", "INT16", _int16(dtype_num))
                + _record("XY", "INT32",
                          _int32(x0) + _int32(y0) + _int32(x1) + _int32(y0)
                          + _int32(x1) + _int32(y1) + _int32(x0) + _int32(y1)
                          + _int32(x0) + _int32(y0))
                + _record("ENDEL", "NONE")
            )

    # Paths → PATH
    if hasattr(plan, "paths") and plan.paths:
        for path_obj in plan.paths:
            layer_name = getattr(path_obj, "layer", "M1")
            layer_num, dtype_num = _resolve_layer(layer_name, cfg.layer_map)
            width_nm = int(getattr(path_obj, "width", 0.08) * 1000)
            points = getattr(path_obj, "points", ())
            if len(points) < 2:
                continue
            xy_data = b""
            for px, py in points:
                xy_data += _int32(um_to_db(px)) + _int32(um_to_db(py))
            buf += (
                _record("PATH", "NONE")
                + _record("LAYER", "INT16", _int16(layer_num))
                + _record("DATATYPE", "INT16", _int16(dtype_num))
                + _record("PATHTYPE", "INT16", _int16(2))  # extend=half-width
                + _record("WIDTH", "INT32", _int32(width_nm))
                + _record("XY", "INT32", xy_data)
                + _record("ENDEL", "NONE")
            )

    # Vias → BOUNDARY (square contact cuts)
    if hasattr(plan, "vias") and plan.vias:
        for via in plan.vias:
            layer_name = _via_layer_name(via)
            layer_num, dtype_num = _resolve_layer(layer_name, cfg.layer_map)
            via_x_um, via_y_um = _via_xy_um(via)
            cx = um_to_db(via_x_um)
            cy = um_to_db(via_y_um)
            half = um_to_db(0.03)  # 30nm half-size via
            buf += (
                _record("BOUNDARY", "NONE")
                + _record("LAYER", "INT16", _int16(layer_num))
                + _record("DATATYPE", "INT16", _int16(dtype_num))
                + _record("XY", "INT32",
                          _int32(cx - half) + _int32(cy - half)
                          + _int32(cx + half) + _int32(cy - half)
                          + _int32(cx + half) + _int32(cy + half)
                          + _int32(cx - half) + _int32(cy + half)
                          + _int32(cx - half) + _int32(cy - half))
                + _record("ENDEL", "NONE")
            )

    # Instance references → SREF
    if hasattr(plan, "instances") and plan.instances:
        for inst in plan.instances:
            cname = getattr(inst, "cell_name", getattr(inst, "cell", "PCELL"))[:32]
            xy = getattr(inst, "xy_um", getattr(inst, "xy", (0.0, 0.0)))
            ix = um_to_db(xy[0])
            iy = um_to_db(xy[1])
            orient = getattr(inst, "orient", "R0")
            strans_val = 0
            if orient in ("MY", "R180"):
                strans_val = 0x8000  # reflect X
            elif orient in ("MX",):
                strans_val = 0x8000
            elif orient == "R90":
                strans_val = 0x0000  # rotation handled by angle
            buf += (
                _record("SREF", "NONE")
                + _record("SNAME", "STRING", _string(cname))
                + (_record("STRANS", "BIT", _uint16(strans_val)) if strans_val else b"")
                + _record("XY", "INT32", _int32(ix) + _int32(iy))
                + _record("ENDEL", "NONE")
            )

    # Pins → TEXT on layer 127/0 (Calibre port detection layer)
    if hasattr(plan, "pins") and plan.pins:
        for pin in plan.pins:
            pin_name = getattr(pin, "name", "PIN")
            pin_layer_name = str(getattr(pin, "layer", "M1"))
            bbox = getattr(pin, "bbox", None)
            if bbox and len(bbox) >= 4:
                px = um_to_db((bbox[0] + bbox[2]) / 2)
                py = um_to_db((bbox[1] + bbox[3]) / 2)
                # Also draw pin boundary on drawing layer
                layer_num, dtype_num = _resolve_layer(pin_layer_name, cfg.layer_map)
                x0, y0 = um_to_db(bbox[0]), um_to_db(bbox[1])
                x1, y1 = um_to_db(bbox[2]), um_to_db(bbox[3])
                buf += (
                    _record("BOUNDARY", "NONE")
                    + _record("LAYER", "INT16", _int16(layer_num))
                    + _record("DATATYPE", "INT16", _int16(dtype_num))
                    + _record("XY", "INT32",
                              _int32(x0) + _int32(y0) + _int32(x1) + _int32(y0)
                              + _int32(x1) + _int32(y1) + _int32(x0) + _int32(y1)
                              + _int32(x0) + _int32(y0))
                    + _record("ENDEL", "NONE")
                )
            else:
                px, py = 0, 0
            buf += (
                _record("TEXT", "NONE")
                + _record("LAYER", "INT16", _int16(_port_text_layer(pin_layer_name) or 131))
                + _record("TEXTTYPE", "INT16", _int16(0))
                + _record("XY", "INT32", _int32(px) + _int32(py))
                + _record("STRING", "STRING", _string(pin_name))
                + _record("ENDEL", "NONE")
            )

    # Labels → TEXT
    if hasattr(plan, "labels") and plan.labels:
        for label in plan.labels:
            if isinstance(label, tuple) and len(label) >= 3:
                layer_name = label[0]
                text = label[1]
                pos = label[2]
                texttype = _label_texttype(str(layer_name))
                if texttype is None:
                    continue
                px = um_to_db(pos[0]) if isinstance(pos, (tuple, list)) and len(pos) >= 2 else 0
                py = um_to_db(pos[1]) if isinstance(pos, (tuple, list)) and len(pos) >= 2 else 0
                buf += (
                    _record("TEXT", "NONE")
                    + _record("LAYER", "INT16", _int16(127))
                    + _record("TEXTTYPE", "INT16", _int16(texttype))
                    + _record("XY", "INT32", _int32(px) + _int32(py))
                    + _record("STRING", "STRING", _string(str(text)))
                    + _record("ENDEL", "NONE")
                )

    buf += _record("ENDSTR", "NONE")
    buf += _record("ENDLIB", "NONE")

    path.write_bytes(bytes(buf))
    return path


def oa_plan_to_gds(
    plan: Any,
    path: str | Path,
    pdk: Any = None,
    top_cell: str = "TOP",
    lib_name: str = "analogskills",
) -> Path:
    """Convenience wrapper: convert OaWritePlan to GDS with PDK-aware layer mapping.

    Args:
        plan: OaWritePlan
        path: Output .gds path
        pdk: Optional PdkConfig for layer mapping
        top_cell: Top cell name
        lib_name: Library name

    Returns:
        Output path
    """
    layer_map = dict(DEFAULT_LAYER_MAP)
    if pdk and hasattr(pdk, "layer_map"):
        gds_layer = 8
        for metal in getattr(pdk.layer_map, "metals", ()):
            layer_map[metal] = (gds_layer, 0)
            gds_layer += 2
        gds_layer = 9
        for via in getattr(pdk.layer_map, "vias", ()):
            layer_map[via] = (gds_layer, 0)
            gds_layer += 2

    cfg = GdsWriteConfig(
        layer_map=layer_map,
        top_cell=top_cell,
        lib_name=lib_name,
    )
    return write_gds(plan, path, cfg)
