"""PDK rule/query kernel for hard design capabilities."""
from __future__ import annotations

from dataclasses import dataclass, field
import ast
import json
from math import ceil, floor
from pathlib import Path
from typing import Any, Mapping


PointUm = tuple[float, float]
BBoxUm = tuple[float, float, float, float]
GridPoint = tuple[int, int]
GridBBox = tuple[int, int, int, int]


@dataclass(frozen=True)
class LayerMap:
    active: str
    gate: str
    contact: str
    metals: tuple[str, ...]
    vias: tuple[str, ...] = ()
    wells: dict[str, str] = field(default_factory=dict)
    implants: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LayerMap":
        metals = tuple(str(v) for v in data.get("metals", ()))
        if not metals:
            raise ValueError("PDK layer_map requires at least one metal layer")
        return cls(
            active=str(data.get("active", "OD")),
            gate=str(data.get("gate", "PO")),
            contact=str(data.get("contact", "CO")),
            metals=metals,
            vias=tuple(str(v) for v in data.get("vias", ())),
            wells={str(k): str(v) for k, v in dict(data.get("wells", {})).items()},
            implants={str(k): str(v) for k, v in dict(data.get("implants", {})).items()},
        )


@dataclass(frozen=True)
class RoutingLayerRule:
    name: str
    direction: str = "any"
    preferred: bool = False
    role: str = "signal"
    track_pitch_nm: int = 0
    track_offset_nm: int = 0
    max_current_ma: float | None = None

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> "RoutingLayerRule":
        return cls(
            name=str(name),
            direction=str(data.get("direction", "any")).lower(),
            preferred=bool(data.get("preferred", False)),
            role=str(data.get("role", "signal")).lower(),
            track_pitch_nm=int(data.get("track_pitch_nm", 0) or 0),
            track_offset_nm=int(data.get("track_offset_nm", 0) or 0),
            max_current_ma=float(data["max_current_ma"]) if data.get("max_current_ma") is not None else None,
        )


@dataclass(frozen=True)
class ViaStackRule:
    via_def: str
    lower_layer: str
    upper_layer: str
    default_rows: int = 1
    default_cols: int = 1
    max_rows: int = 1
    max_cols: int = 1
    max_current_ma_per_cut: float | None = None
    kind: str = "routing"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ViaStackRule":
        via_def = str(data.get("via_def", ""))
        if not via_def:
            raise ValueError("via_stack entry requires via_def")
        return cls(
            via_def=via_def,
            lower_layer=str(data.get("lower_layer", "")),
            upper_layer=str(data.get("upper_layer", "")),
            default_rows=max(1, int(data.get("default_rows", 1) or 1)),
            default_cols=max(1, int(data.get("default_cols", 1) or 1)),
            max_rows=max(1, int(data.get("max_rows", data.get("default_rows", 1)) or 1)),
            max_cols=max(1, int(data.get("max_cols", data.get("default_cols", 1)) or 1)),
            max_current_ma_per_cut=float(data["max_current_ma_per_cut"]) if data.get("max_current_ma_per_cut") is not None else None,
            kind=str(data.get("kind", "routing")).lower(),
        )


@dataclass(frozen=True)
class ExtractionCorner:
    name: str
    cap_scale: float = 1.0
    res_scale: float = 1.0
    temperature_c: float | None = None

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> "ExtractionCorner":
        return cls(
            name=str(name),
            cap_scale=float(data.get("cap_scale", 1.0)),
            res_scale=float(data.get("res_scale", 1.0)),
            temperature_c=float(data["temperature_c"]) if data.get("temperature_c") is not None else None,
        )


@dataclass(frozen=True)
class SpectreModelLibrary:
    path: str
    section: str = ""
    section_by_corner: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: str | Mapping[str, Any]) -> "SpectreModelLibrary":
        if isinstance(data, str):
            return cls(path=str(data))
        return cls(
            path=str(data.get("path", data.get("file", ""))),
            section=str(data.get("section", "")),
            section_by_corner={
                str(key): str(value)
                for key, value in dict(data.get("section_by_corner", data.get("sections_by_corner", {}))).items()
            },
        )

    def resolve_section(self, corner: str) -> str:
        resolved_corner = str(corner or "")
        if resolved_corner and resolved_corner in self.section_by_corner:
            return self.section_by_corner[resolved_corner]
        return self.section


@dataclass(frozen=True)
class SpectreMonteCarloPreset:
    mode: str = ""
    name: str = "mc1"
    numruns: int = 1
    options: tuple[str, ...] = ()
    statement_template: str = ""
    statistics_lines: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SpectreMonteCarloPreset":
        return cls(
            mode=str(data.get("mode", "")),
            name=str(data.get("name", "mc1")),
            numruns=max(1, int(data.get("numruns", 1) or 1)),
            options=tuple(str(item) for item in tuple(data.get("options", ()) or ()) if str(item)),
            statement_template=str(data.get("statement_template", "")),
            statistics_lines=tuple(str(item) for item in tuple(data.get("statistics_lines", ()) or ()) if str(item)),
        )


@dataclass(frozen=True)
class SpectreSignoffPreset:
    name: str
    title: str
    body_template: str
    model_libraries: tuple[SpectreModelLibrary, ...] = ()
    save_lines: tuple[str, ...] = ()
    measure_lines: tuple[str, ...] = ()
    setup_lines: tuple[str, ...] = ()
    statistics_lines: tuple[str, ...] = ()
    variables: dict[str, float | int | str] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    monte_carlo: SpectreMonteCarloPreset = field(default_factory=SpectreMonteCarloPreset)
    default_measurement_file_name: str = "meas.txt"

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> "SpectreSignoffPreset":
        return cls(
            name=str(name),
            title=str(data.get("title", name)),
            body_template=str(data.get("body_template", "")),
            model_libraries=tuple(
                SpectreModelLibrary.from_dict(item)
                for item in tuple(data.get("model_libraries", data.get("model_includes", ())) or ())
            ),
            save_lines=tuple(str(item) for item in tuple(data.get("save_lines", ()) or ()) if str(item)),
            measure_lines=tuple(str(item) for item in tuple(data.get("measure_lines", ()) or ()) if str(item)),
            setup_lines=tuple(str(item) for item in tuple(data.get("setup_lines", ()) or ()) if str(item)),
            statistics_lines=tuple(str(item) for item in tuple(data.get("statistics_lines", ()) or ()) if str(item)),
            variables={str(key): value for key, value in dict(data.get("variables", {})).items()},
            context=dict(data.get("context", {})),
            monte_carlo=SpectreMonteCarloPreset.from_dict(dict(data.get("monte_carlo", {}))),
            default_measurement_file_name=str(data.get("default_measurement_file_name", "meas.txt")),
        )


@dataclass(frozen=True)
class PlacementSite:
    device_pitch_um: float = 1.0
    row_pitch_um: float = 2.0
    common_centroid_pitch_um: float = 1.0
    interdigitated_pitch_um: float = 1.0
    symmetry_axis: str = "y"
    row_policy: str = "single"
    role_orient_policy: dict[str, tuple[str, ...]] = field(default_factory=dict)
    role_row_policy: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PlacementSite":
        return cls(
            device_pitch_um=float(data.get("device_pitch_um", 1.0)),
            row_pitch_um=float(data.get("row_pitch_um", 2.0)),
            common_centroid_pitch_um=float(data.get("common_centroid_pitch_um", data.get("device_pitch_um", 1.0))),
            interdigitated_pitch_um=float(data.get("interdigitated_pitch_um", data.get("device_pitch_um", 1.0))),
            symmetry_axis=str(data.get("symmetry_axis", "y")).lower(),
            row_policy=str(data.get("row_policy", "single")).lower(),
            role_orient_policy={
                str(role): tuple(str(orient) for orient in value)
                for role, value in dict(data.get("role_orient_policy", {})).items()
            },
            role_row_policy={str(role): str(policy) for role, policy in dict(data.get("role_row_policy", {})).items()},
        )


@dataclass(frozen=True)
class AnalogPlacementConstraintProfile:
    match_tolerance_um: float = 1e-6
    symmetry_tolerance_um: float = 1e-6
    row_alignment_tolerance_um: float = 1e-6
    partition_order_tolerance_um: float = 1e-6
    focus_separation_target_um: float = 0.0
    anchor_spread_target_um: float = 0.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalogPlacementConstraintProfile":
        return cls(
            match_tolerance_um=float(data.get("match_tolerance_um", 1e-6)),
            symmetry_tolerance_um=float(data.get("symmetry_tolerance_um", 1e-6)),
            row_alignment_tolerance_um=float(data.get("row_alignment_tolerance_um", 1e-6)),
            partition_order_tolerance_um=float(data.get("partition_order_tolerance_um", 1e-6)),
            focus_separation_target_um=float(data.get("focus_separation_target_um", 0.0)),
            anchor_spread_target_um=float(data.get("anchor_spread_target_um", 0.0)),
        )


@dataclass(frozen=True)
class AnalogRoutingConstraintProfile:
    length_match_tolerance_um: float = 1e-6
    current_derate: float = 1.0
    via_current_derate: float = 1.0
    preferred_power_penalty: float = 1.0
    preferred_signal_penalty: float = 1.0
    bus_order_penalty: float = 1.0
    matched_route_penalty: float = 1.0
    antenna_penalty: float = 1.0
    min_area_penalty: float = 1.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AnalogRoutingConstraintProfile":
        return cls(
            length_match_tolerance_um=float(data.get("length_match_tolerance_um", 1e-6)),
            current_derate=float(data.get("current_derate", 1.0)),
            via_current_derate=float(data.get("via_current_derate", 1.0)),
            preferred_power_penalty=float(data.get("preferred_power_penalty", 1.0)),
            preferred_signal_penalty=float(data.get("preferred_signal_penalty", 1.0)),
            bus_order_penalty=float(data.get("bus_order_penalty", 1.0)),
            matched_route_penalty=float(data.get("matched_route_penalty", 1.0)),
            antenna_penalty=float(data.get("antenna_penalty", 1.0)),
            min_area_penalty=float(data.get("min_area_penalty", 1.0)),
        )


@dataclass(frozen=True)
class DesignRuleDeck:
    grid_nm: int = 1
    min_width_nm: dict[str, int] = field(default_factory=dict)
    legal_width_nm: dict[str, tuple[int, ...]] = field(default_factory=dict)
    unrestricted_width_min_nm: dict[str, int] = field(default_factory=dict)
    min_spacing_nm: dict[str, int] = field(default_factory=dict)
    enclosure_nm: dict[str, int] = field(default_factory=dict)
    min_area_nm2: dict[str, int] = field(default_factory=dict)
    eol_spacing_nm: dict[str, int] = field(default_factory=dict)
    array_spacing_nm: dict[str, int] = field(default_factory=dict)
    diagonal_spacing_nm: dict[str, int] = field(default_factory=dict)
    extension_nm: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.grid_nm <= 0:
            raise ValueError("PDK grid_nm must be positive")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DesignRuleDeck":
        return cls(
            grid_nm=int(data.get("grid_nm", 1)),
            min_width_nm={str(k): int(v) for k, v in dict(data.get("min_width_nm", {})).items()},
            legal_width_nm={
                str(k): tuple(sorted({int(item) for item in tuple(v or ())}))
                for k, v in dict(data.get("legal_width_nm", {})).items()
            },
            unrestricted_width_min_nm={str(k): int(v) for k, v in dict(data.get("unrestricted_width_min_nm", {})).items()},
            min_spacing_nm={str(k): int(v) for k, v in dict(data.get("min_spacing_nm", {})).items()},
            enclosure_nm={str(k): int(v) for k, v in dict(data.get("enclosure_nm", {})).items()},
            min_area_nm2={str(k): int(v) for k, v in dict(data.get("min_area_nm2", {})).items()},
            eol_spacing_nm={str(k): int(v) for k, v in dict(data.get("eol_spacing_nm", {})).items()},
            array_spacing_nm={str(k): int(v) for k, v in dict(data.get("array_spacing_nm", {})).items()},
            diagonal_spacing_nm={str(k): int(v) for k, v in dict(data.get("diagonal_spacing_nm", {})).items()},
            extension_nm={str(k): int(v) for k, v in dict(data.get("extension_nm", {})).items()},
        )

    def min_width(self, layer: str) -> int:
        if layer not in self.min_width_nm:
            raise KeyError(f"no min-width rule for layer {layer!r}")
        return self.min_width_nm[layer]

    def min_spacing(self, layer: str) -> int:
        if layer not in self.min_spacing_nm:
            raise KeyError(f"no min-spacing rule for layer {layer!r}")
        return self.min_spacing_nm[layer]

    def min_width_um(self, layer: str) -> float:
        return self.min_width(layer) * 1e-3

    def legal_widths_um(self, layer: str) -> tuple[float, ...]:
        return tuple(float(value) * 1e-3 for value in self.legal_width_nm.get(str(layer), ()))

    def unrestricted_width_min_um(self, layer: str) -> float | None:
        value = self.unrestricted_width_min_nm.get(str(layer))
        return None if value is None else float(value) * 1e-3

    def next_legal_width_um(self, layer: str, width_um: float) -> float:
        """Return the next configured legal drawn width for ``layer``.

        Some advanced-node layers have discrete legal widths plus an
        unrestricted region for sufficiently wide wires.  If no discrete rule
        is configured for the layer, fall back to min-width + grid snapping.
        """

        layer_name = str(layer)
        requested = max(float(width_um), 0.0)
        legal = self.legal_widths_um(layer_name)
        unrestricted = self.unrestricted_width_min_um(layer_name)
        if legal:
            for candidate in legal:
                if requested <= candidate + 1e-12:
                    return candidate
            if unrestricted is not None:
                if requested <= unrestricted + 1e-12:
                    return unrestricted
                return self.snap_dimension_ceil_um(requested)
            return legal[-1]
        minimum = self.min_width_um(layer_name) if layer_name in self.min_width_nm else 0.0
        return self.snap_dimension_ceil_um(max(requested, minimum))

    def min_spacing_um(self, layer: str) -> float:
        return self.min_spacing(layer) * 1e-3

    def array_spacing_um(self, layer: str) -> float:
        if layer not in self.array_spacing_nm:
            raise KeyError(f"no array-spacing rule for layer {layer!r}")
        return self.array_spacing_nm[layer] * 1e-3

    def extension_um(self, layer: str) -> float:
        if layer not in self.extension_nm:
            raise KeyError(f"no extension rule for layer {layer!r}")
        return self.extension_nm[layer] * 1e-3

    def enclosure_um(self, rule: str) -> float:
        if rule not in self.enclosure_nm:
            raise KeyError(f"no enclosure rule for {rule!r}")
        return self.enclosure_nm[rule] * 1e-3

    @property
    def grid_step_um(self) -> float:
        return self.grid_nm * 1e-3

    def nm_to_grid(self, value_nm: float) -> int:
        return _round_to_grid_units(value_nm, self.grid_nm)

    def grid_to_nm(self, value_grid: int) -> int:
        return int(value_grid) * self.grid_nm

    def um_to_grid(self, value_um: float) -> int:
        return self.nm_to_grid(float(value_um) * 1e3)

    def grid_to_um(self, value_grid: int) -> float:
        return self.grid_to_nm(value_grid) * 1e-3

    def snap_nm(self, value_nm: float) -> int:
        return self.grid_to_nm(self.nm_to_grid(value_nm))

    def snap_um(self, value_um: float) -> float:
        return self.grid_to_um(self.um_to_grid(value_um))

    def snap_dimension_um(self, value_um: float, *, minimum_grid_units: int = 1) -> float:
        if value_um <= 0:
            raise ValueError("physical dimensions must be positive")
        grid_units = max(int(minimum_grid_units), self.um_to_grid_ceil(value_um))
        return self.grid_to_um(grid_units)

    def snap_dimension_ceil_um(self, value_um: float, *, minimum_grid_units: int = 1) -> float:
        return self.snap_dimension_um(value_um, minimum_grid_units=minimum_grid_units)

    def um_to_grid_ceil(self, value_um: float) -> int:
        return _ceil_to_grid_units(float(value_um) * 1e3, self.grid_nm)

    def point_um_to_grid(self, point: PointUm) -> GridPoint:
        x, y = point
        return (self.um_to_grid(x), self.um_to_grid(y))

    def point_grid_to_um(self, point: GridPoint) -> PointUm:
        x, y = point
        return (self.grid_to_um(x), self.grid_to_um(y))

    def bbox_um_to_grid(self, bbox: BBoxUm, *, mode: str = "nearest") -> GridBBox:
        x0, y0, x1, y1 = bbox
        xlo, xhi = sorted((float(x0), float(x1)))
        ylo, yhi = sorted((float(y0), float(y1)))
        if mode == "nearest":
            return (self.um_to_grid(xlo), self.um_to_grid(ylo), self.um_to_grid(xhi), self.um_to_grid(yhi))
        if mode == "outward":
            return (
                _floor_to_grid_units(xlo * 1e3, self.grid_nm),
                _floor_to_grid_units(ylo * 1e3, self.grid_nm),
                _ceil_to_grid_units(xhi * 1e3, self.grid_nm),
                _ceil_to_grid_units(yhi * 1e3, self.grid_nm),
            )
        if mode == "inward":
            return (
                _ceil_to_grid_units(xlo * 1e3, self.grid_nm),
                _ceil_to_grid_units(ylo * 1e3, self.grid_nm),
                _floor_to_grid_units(xhi * 1e3, self.grid_nm),
                _floor_to_grid_units(yhi * 1e3, self.grid_nm),
            )
        raise ValueError(f"unknown bbox snap mode {mode!r}")

    def bbox_grid_to_um(self, bbox: GridBBox) -> BBoxUm:
        x0, y0, x1, y1 = bbox
        return (self.grid_to_um(x0), self.grid_to_um(y0), self.grid_to_um(x1), self.grid_to_um(y1))

    def snap_point_um(self, point: PointUm) -> PointUm:
        return self.point_grid_to_um(self.point_um_to_grid(point))

    def snap_bbox_um(self, bbox: BBoxUm, *, mode: str = "nearest") -> BBoxUm:
        return self.bbox_grid_to_um(self.bbox_um_to_grid(bbox, mode=mode))

    def is_on_grid_um(self, value_um: float, *, tol_um: float = 1e-12) -> bool:
        return abs(float(value_um) - self.snap_um(value_um)) <= tol_um

    def point_is_on_grid_um(self, point: PointUm, *, tol_um: float = 1e-12) -> bool:
        return all(self.is_on_grid_um(coord, tol_um=tol_um) for coord in point)

    def bbox_is_on_grid_um(self, bbox: BBoxUm, *, tol_um: float = 1e-12) -> bool:
        return all(self.is_on_grid_um(coord, tol_um=tol_um) for coord in bbox)

    def enclosure(self, key: str) -> int:
        if key not in self.enclosure_nm:
            raise KeyError(f"no enclosure rule {key!r}")
        return self.enclosure_nm[key]


@dataclass(frozen=True)
class PCellTemplate:
    logical_name: str
    lib_name: str
    cell_name: str
    view_name: str = "layout"
    layout_lib_name: str = ""
    layout_cell_name: str = ""
    layout_view_name: str = ""
    parameter_ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    default_params: dict[str, Any] = field(default_factory=dict)
    parameter_map: dict[str, str] = field(default_factory=dict)
    layout_parameter_map: dict[str, str] = field(default_factory=dict)
    schematic_parameter_map: dict[str, str] = field(default_factory=dict)
    instantiation_method: str = "dbCreateInstByMasterName"
    layout_instantiation_method: str = ""
    layout_parameter_allowlist: tuple[str, ...] = ()
    terminal_access: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, logical_name: str, data: Mapping[str, Any]) -> "PCellTemplate":
        ranges = {}
        for key, value in dict(data.get("parameter_ranges", {})).items():
            ranges[str(key)] = (float(value[0]), float(value[1]))
        return cls(
            logical_name=logical_name,
            lib_name=str(data.get("lib_name", data.get("lib", ""))),
            cell_name=str(data.get("cell_name", data.get("cell", logical_name))),
            view_name=str(data.get("view_name", data.get("view", "layout"))),
            layout_lib_name=str(data.get("layout_lib_name", data.get("layout_lib", ""))),
            layout_cell_name=str(data.get("layout_cell_name", data.get("layout_cell", ""))),
            layout_view_name=str(data.get("layout_view_name", data.get("layout_view", ""))),
            parameter_ranges=ranges,
            default_params=dict(data.get("default_params", {})),
            parameter_map={str(k): str(v) for k, v in dict(data.get("parameter_map", {})).items()},
            layout_parameter_map={str(k): str(v) for k, v in dict(data.get("layout_parameter_map", {})).items()},
            schematic_parameter_map={str(k): str(v) for k, v in dict(data.get("schematic_parameter_map", {})).items()},
            instantiation_method=str(data.get("instantiation_method", "dbCreateInstByMasterName")),
            layout_instantiation_method=str(data.get("layout_instantiation_method", "")),
            layout_parameter_allowlist=tuple(
                str(item)
                for item in data.get(
                    "layout_parameter_allowlist",
                    data.get("layout_param_allowlist", ()),
                )
            ),
            terminal_access={str(k): dict(v) for k, v in dict(data.get("terminal_access", {})).items()},
        )

    def validate_params(self, params: Mapping[str, Any]) -> list[str]:
        issues: list[str] = []
        for key, value in params.items():
            if key not in self.parameter_ranges:
                continue
            lo, hi = self.parameter_ranges[key]
            numeric = float(value)
            if numeric < lo or numeric > hi:
                issues.append(f"{self.logical_name}.{key}={numeric:g} outside [{lo:g}, {hi:g}]")
        return issues

    def map_parameters(self, logical_params: Mapping[str, Any], *, schematic: bool = False) -> dict[str, Any]:
        """Map logical sizing parameters to CDF/PCell parameters.

        ``parameter_map`` supports two forms:

        - legacy source-to-target maps, e.g. ``{"W": "w", "L": "l"}``
        - expression target maps, e.g. ``{"Wfg": "W / nf", "fingers": "nf"}``

        The expression context contains the supplied keys plus upper/lower-case
        aliases, so both ``W`` and ``w`` work for common MOS dimensions.
        """
        params = dict(self.default_params)
        mapping = self.schematic_parameter_map if schematic and self.schematic_parameter_map else self.parameter_map
        if not mapping:
            params.update(dict(logical_params))
            return params

        raw_context = dict(logical_params)
        context = _parameter_context(raw_context)
        for target_or_source, expression_or_target in mapping.items():
            if _is_legacy_parameter_map_entry(target_or_source, expression_or_target, raw_context):
                params[expression_or_target] = raw_context[target_or_source]
                continue
            params[target_or_source] = _evaluate_parameter_expression(expression_or_target, context)
        return params

    def map_layout_parameters(self, logical_params: Mapping[str, Any]) -> dict[str, Any]:
        if not self.layout_parameter_map:
            return self.map_parameters(logical_params, schematic=False)
        params = dict(self.default_params)
        raw_context = dict(logical_params)
        context = _parameter_context(raw_context)
        for target_or_source, expression_or_target in self.layout_parameter_map.items():
            if _is_legacy_parameter_map_entry(target_or_source, expression_or_target, raw_context):
                params[expression_or_target] = raw_context[target_or_source]
                continue
            params[target_or_source] = _evaluate_parameter_expression(expression_or_target, context)
        return params

    def filter_layout_parameters(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if not self.layout_parameter_allowlist:
            return {str(key): value for key, value in dict(params).items()}
        allowed = set(self.layout_parameter_allowlist)
        return {str(key): value for key, value in dict(params).items() if str(key) in allowed}

    def resolved_layout_lib_name(self) -> str:
        return self.layout_lib_name or self.lib_name

    def resolved_layout_cell_name(self) -> str:
        return self.layout_cell_name or self.cell_name

    def resolved_layout_view_name(self) -> str:
        return self.layout_view_name or self.view_name

    def resolved_layout_instantiation_method(self) -> str:
        return self.layout_instantiation_method or self.instantiation_method

    def resolved_schematic_lib_name(self) -> str:
        return self.lib_name

    def resolved_schematic_cell_name(self) -> str:
        return self.cell_name

    def resolved_schematic_view_name(self) -> str:
        return self.view_name

    def resolved_schematic_instantiation_method(self) -> str:
        return self.instantiation_method


@dataclass(frozen=True)
class MacroBinding:
    logical_name: str
    lib_name: str
    cell_name: str
    view_name: str = "layout"
    abstract_view_name: str = "abstract"
    layout_artifact: str = ""
    pin_contract: dict[str, tuple[str, ...]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, logical_name: str, data: Mapping[str, Any]) -> "MacroBinding":
        pin_contract: dict[str, tuple[str, ...]] = {}
        for pin_name, aliases in dict(data.get("pin_contract", {})).items():
            pin_contract[str(pin_name)] = tuple(str(alias) for alias in tuple(aliases or ()) if str(alias))
        return cls(
            logical_name=str(logical_name),
            lib_name=str(data.get("lib_name", data.get("lib", ""))),
            cell_name=str(data.get("cell_name", data.get("cell", logical_name))),
            view_name=str(data.get("view_name", data.get("view", "layout"))),
            abstract_view_name=str(data.get("abstract_view_name", data.get("abstract_view", "abstract"))),
            layout_artifact=str(data.get("layout_artifact", data.get("gds", data.get("layout_or_gds", "")))),
            pin_contract=pin_contract,
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class PdkConfig:
    name: str
    layer_map: LayerMap
    rules: DesignRuleDeck
    pcell_templates: dict[str, PCellTemplate] = field(default_factory=dict)
    pcell_aliases: dict[str, str] = field(default_factory=dict)
    macro_bindings: dict[str, MacroBinding] = field(default_factory=dict)
    routing_layers: dict[str, RoutingLayerRule] = field(default_factory=dict)
    via_stack: tuple[ViaStackRule, ...] = ()
    extraction_corners: dict[str, ExtractionCorner] = field(default_factory=dict)
    signoff_presets: dict[str, SpectreSignoffPreset] = field(default_factory=dict)
    placement_site: PlacementSite = field(default_factory=PlacementSite)
    analog_placement_constraints: AnalogPlacementConstraintProfile = field(default_factory=AnalogPlacementConstraintProfile)
    analog_routing_constraints: AnalogRoutingConstraintProfile = field(default_factory=AnalogRoutingConstraintProfile)
    preferred_signal_layers: tuple[str, ...] = ()
    preferred_power_layers: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PdkConfig":
        return cls(
            name=str(data.get("name", "unnamed_pdk")),
            layer_map=LayerMap.from_dict(data.get("layer_map", {})),
            rules=DesignRuleDeck.from_dict(data.get("rules", {})),
            pcell_templates={
                str(k): PCellTemplate.from_dict(str(k), v)
                for k, v in dict(data.get("pcell_templates", {})).items()
            },
            pcell_aliases={str(k): str(v) for k, v in dict(data.get("pcell_aliases", {})).items()},
            macro_bindings={
                str(k): MacroBinding.from_dict(str(k), v)
                for k, v in dict(data.get("macro_bindings", {})).items()
            },
            routing_layers={
                str(k): RoutingLayerRule.from_dict(str(k), v)
                for k, v in dict(data.get("routing_layers", {})).items()
            },
            via_stack=tuple(ViaStackRule.from_dict(item) for item in data.get("via_stack", ())),
            extraction_corners={
                str(k): ExtractionCorner.from_dict(str(k), v)
                for k, v in dict(data.get("extraction_corners", {})).items()
            },
            signoff_presets={
                str(k): SpectreSignoffPreset.from_dict(str(k), v)
                for k, v in dict(data.get("signoff_presets", {})).items()
            },
            placement_site=PlacementSite.from_dict(data.get("placement_site", {})),
            analog_placement_constraints=AnalogPlacementConstraintProfile.from_dict(data.get("analog_placement_constraints", {})),
            analog_routing_constraints=AnalogRoutingConstraintProfile.from_dict(data.get("analog_routing_constraints", {})),
            preferred_signal_layers=tuple(data.get("preferred_signal_layers", ())),
            preferred_power_layers=tuple(data.get("preferred_power_layers", ())),
            metadata=dict(data.get("metadata", {})),
        )

    @classmethod
    def generic(cls) -> "PdkConfig":
        return cls.from_dict({
            "name": "generic_hard_kernel_pdk",
            "layer_map": {"active": "OD", "gate": "PO", "contact": "CO", "metals": ["M1", "M2", "M3"], "vias": ["V1", "V2"]},
            "rules": {"grid_nm": 1, "min_width_nm": {"M1": 120, "M2": 160, "M3": 200}, "min_spacing_nm": {"M1": 120, "M2": 160, "M3": 200}, "enclosure_nm": {"V1_M1": 40}},
            "routing_layers": {
                "M1": {"direction": "h", "preferred": True, "role": "signal", "track_pitch_nm": 240},
                "M2": {"direction": "v", "preferred": True, "role": "power", "track_pitch_nm": 320, "max_current_ma": 12.0},
                "M3": {"direction": "h", "preferred": False, "role": "power", "track_pitch_nm": 400, "max_current_ma": 20.0},
            },
            "via_stack": (
                {"via_def": "V1", "lower_layer": "M1", "upper_layer": "M2", "default_rows": 1, "default_cols": 1, "max_rows": 4, "max_cols": 4, "max_current_ma_per_cut": 4.0},
                {"via_def": "V2", "lower_layer": "M2", "upper_layer": "M3", "default_rows": 1, "default_cols": 1, "max_rows": 4, "max_cols": 4, "max_current_ma_per_cut": 6.0},
            ),
            "extraction_corners": {
                "rcmin": {"cap_scale": 0.85, "res_scale": 0.9, "temperature_c": -40.0},
                "typ": {"cap_scale": 1.0, "res_scale": 1.0, "temperature_c": 25.0},
                "rcmax": {"cap_scale": 1.2, "res_scale": 1.15, "temperature_c": 125.0},
            },
            "signoff_presets": {
                "ac_nominal": {
                    "title": "Generic AC signoff bench",
                    "body_template": (
                        "parameters VDD={voltage_v}\n"
                        "simulatorOptions options temp={temperature_c}\n"
                        "ac ac dec 20 1 1G\n"
                        "// corner={corner} run={run_id}"
                    ),
                    "model_libraries": (
                        {
                            "path": "models.scs",
                            "section_by_corner": {
                                "rcmin": "tt",
                                "typ": "tt",
                                "rcmax": "ss",
                            },
                        },
                    ),
                    "measure_lines": ("export ac_gain=v(out)",),
                    "setup_lines": ("global 0 vdd!",),
                },
            },
            "placement_site": {
                "device_pitch_um": 1.2,
                "row_pitch_um": 2.4,
                "common_centroid_pitch_um": 1.0,
                "interdigitated_pitch_um": 1.1,
                "symmetry_axis": "y",
                "row_policy": "staggered",
                "role_orient_policy": {
                    "dummy": ["R0"],
                    "CURRENT_MIRROR": ["R0", "MY"],
                    "CASCODE": ["R0", "MY"],
                    "LOAD": ["R0", "MY"],
                },
                "role_row_policy": {
                    "CURRENT_MIRROR": "shared",
                    "CASCODE": "shared",
                    "TAIL": "bottom",
                    "LOAD": "top",
                    "DRIVER": "upper_mid",
                },
            },
            "analog_placement_constraints": {
                "match_tolerance_um": 0.05,
                "symmetry_tolerance_um": 0.05,
                "row_alignment_tolerance_um": 0.05,
                "partition_order_tolerance_um": 0.05,
                "focus_separation_target_um": 2.4,
                "anchor_spread_target_um": 3.6,
            },
            "analog_routing_constraints": {
                "length_match_tolerance_um": 0.2,
                "current_derate": 0.85,
                "via_current_derate": 0.85,
                "preferred_power_penalty": 1.25,
                "preferred_signal_penalty": 1.1,
                "bus_order_penalty": 1.2,
                "matched_route_penalty": 1.2,
                "antenna_penalty": 1.2,
                "min_area_penalty": 1.1,
            },
            "preferred_signal_layers": ["M1", "M2"],
            "preferred_power_layers": ["M2", "M3"],
            "pcell_templates": {
                "nmos": {"lib_name": "generic", "cell_name": "nmos", "parameter_ranges": {"W": [0.12e-6, 100e-6], "L": [0.03e-6, 10e-6], "nf": [1, 128], "m": [1, 64]}, "parameter_map": {"W": "W", "L": "L", "nf": "nf", "m": "m"}},
                "pmos": {"lib_name": "generic", "cell_name": "pmos", "parameter_ranges": {"W": [0.12e-6, 100e-6], "L": [0.03e-6, 10e-6], "nf": [1, 128], "m": [1, 64]}, "parameter_map": {"W": "W", "L": "L", "nf": "nf", "m": "m"}},
                "resistor": {"lib_name": "generic", "cell_name": "resistor", "parameter_ranges": {"R": [1.0, 1e9], "W": [0.1e-6, 100e-6]}, "parameter_map": {"R": "R", "W": "W"}},
                "capacitor": {"lib_name": "generic", "cell_name": "capacitor", "parameter_ranges": {"C": [1e-18, 1e-9]}, "parameter_map": {"C": "C"}},
            },
            "pcell_aliases": {
                "nfet": "nmos",
                "pfet": "pmos",
                "mimcap": "capacitor",
                "momcap": "capacitor",
                "polyres": "resistor",
            },
            "macro_bindings": {
                "vco": {
                    "lib_name": "generic_macro",
                    "cell_name": "vco_core",
                    "view_name": "layout",
                    "abstract_view_name": "abstract",
                    "layout_artifact": "vco_core.gds",
                    "pin_contract": {
                        "VCTRL": ("VCTRL",),
                        "VCO_OUT": ("VCO_OUT",),
                    },
                },
            },
            "metadata": {"test_only": True},
        })

    @classmethod
    def load_json(cls, path: str | Path) -> "PdkConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def validate(self) -> list[str]:
        issues: list[str] = []
        layers = self.layer_map
        if not layers.metals:
            issues.append("layer_map.metals is required")
        if layers.vias and len(layers.vias) != max(0, len(layers.metals) - 1):
            issues.append("via count should be one less than metal count")
        known_layers = {layers.active, layers.gate, layers.contact, *layers.metals, *layers.vias, *layers.wells.values(), *layers.implants.values()}
        for layer in layers.metals:
            if layer not in self.rules.min_width_nm:
                issues.append(f"missing min_width rule for {layer}")
            if layer not in self.rules.min_spacing_nm:
                issues.append(f"missing min_spacing rule for {layer}")
        for layer, rule in self.routing_layers.items():
            if layer not in known_layers:
                issues.append(f"routing layer {layer} is not in layer_map")
            if rule.direction not in {"h", "v", "any"}:
                issues.append(f"routing layer {layer} has invalid direction {rule.direction!r}")
            if rule.role not in {"signal", "power", "mixed"}:
                issues.append(f"routing layer {layer} has invalid role {rule.role!r}")
        for via in self.via_stack:
            if via.kind not in {"routing", "native_contact", "helper_contact"}:
                issues.append(f"via {via.via_def} has invalid kind {via.kind!r}")
            if via.kind == "routing":
                if via.lower_layer not in layers.metals:
                    issues.append(f"via {via.via_def} lower_layer {via.lower_layer} is not a routing metal")
                if via.upper_layer not in layers.metals:
                    issues.append(f"via {via.via_def} upper_layer {via.upper_layer} is not a routing metal")
            else:
                if via.lower_layer not in known_layers:
                    issues.append(f"via {via.via_def} lower_layer {via.lower_layer} is not in layer_map")
                if via.upper_layer not in known_layers:
                    issues.append(f"via {via.via_def} upper_layer {via.upper_layer} is not in layer_map")
            if via.lower_layer == via.upper_layer:
                issues.append(f"via {via.via_def} cannot connect identical layers")
            if via.max_current_ma_per_cut is not None and via.max_current_ma_per_cut <= 0.0:
                issues.append(f"via {via.via_def} max_current_ma_per_cut must be positive")
        for name, corner in self.extraction_corners.items():
            if corner.cap_scale <= 0.0 or corner.res_scale <= 0.0:
                issues.append(f"extraction corner {name} must have positive RC scale")
        for name, preset in self.signoff_presets.items():
            if not preset.body_template.strip():
                issues.append(f"signoff preset {name} missing body_template")
            if not preset.default_measurement_file_name.strip():
                issues.append(f"signoff preset {name} missing default_measurement_file_name")
            if preset.monte_carlo.numruns <= 0:
                issues.append(f"signoff preset {name} monte_carlo.numruns must be positive")
            for library in preset.model_libraries:
                if not library.path.strip():
                    issues.append(f"signoff preset {name} has model library with empty path")
        if self.placement_site.row_policy not in {"single", "staggered", "mirrored"}:
            issues.append(f"placement row_policy {self.placement_site.row_policy!r} is invalid")
        if self.placement_site.symmetry_axis not in {"x", "y"}:
            issues.append(f"placement symmetry_axis {self.placement_site.symmetry_axis!r} is invalid")
        if self.analog_placement_constraints.match_tolerance_um < 0.0:
            issues.append("analog placement match_tolerance_um must be non-negative")
        if self.analog_placement_constraints.symmetry_tolerance_um < 0.0:
            issues.append("analog placement symmetry_tolerance_um must be non-negative")
        if self.analog_placement_constraints.row_alignment_tolerance_um < 0.0:
            issues.append("analog placement row_alignment_tolerance_um must be non-negative")
        if self.analog_routing_constraints.length_match_tolerance_um < 0.0:
            issues.append("analog routing length_match_tolerance_um must be non-negative")
        if self.analog_routing_constraints.current_derate <= 0.0:
            issues.append("analog routing current_derate must be positive")
        if self.analog_routing_constraints.via_current_derate <= 0.0:
            issues.append("analog routing via_current_derate must be positive")
        for role, policy in self.placement_site.role_row_policy.items():
            if policy not in {"shared", "bottom", "top", "upper_mid", "lower_mid", "any"}:
                issues.append(f"placement role_row_policy {role}={policy!r} is invalid")
        for layer in self.preferred_signal_layers + self.preferred_power_layers:
            if layer not in known_layers:
                issues.append(f"preferred layer {layer} is not in layer_map")
        for name, template in self.pcell_templates.items():
            if not template.lib_name:
                issues.append(f"PCell {name} missing lib_name")
            if not template.cell_name:
                issues.append(f"PCell {name} missing cell_name")
            if template.instantiation_method not in {"dbCreateInstByMasterName", "dbCreateParamInst"}:
                issues.append(f"PCell {name} has unsupported instantiation_method {template.instantiation_method!r}")
        for alias, target in self.pcell_aliases.items():
            if not alias:
                issues.append("PCell alias cannot be empty")
            if target not in self.pcell_templates:
                issues.append(f"PCell alias {alias} targets missing template {target!r}")
        for name, macro in self.macro_bindings.items():
            if not macro.lib_name:
                issues.append(f"macro {name} missing lib_name")
            if not macro.cell_name:
                issues.append(f"macro {name} missing cell_name")
        return issues

    @property
    def is_test_only(self) -> bool:
        return bool(self.metadata.get("test_only", False))

    def um_to_grid(self, value_um: float) -> int:
        return self.rules.um_to_grid(value_um)

    def grid_to_um(self, value_grid: int) -> float:
        return self.rules.grid_to_um(value_grid)

    def snap_um(self, value_um: float) -> float:
        return self.rules.snap_um(value_um)

    def snap_dimension_um(self, value_um: float, *, minimum_grid_units: int = 1) -> float:
        return self.rules.snap_dimension_um(value_um, minimum_grid_units=minimum_grid_units)

    def snap_dimension_ceil_um(self, value_um: float, *, minimum_grid_units: int = 1) -> float:
        return self.rules.snap_dimension_ceil_um(value_um, minimum_grid_units=minimum_grid_units)

    def snap_point_um(self, point: PointUm) -> PointUm:
        return self.rules.snap_point_um(point)

    def snap_bbox_um(self, bbox: BBoxUm, *, mode: str = "nearest") -> BBoxUm:
        return self.rules.snap_bbox_um(bbox, mode=mode)

    def pcell_template_for(self, logical_name: str) -> PCellTemplate:
        canonical_name = self.resolve_pcell_logical_name(logical_name)
        if canonical_name not in self.pcell_templates:
            raise KeyError(f"PDK {self.name!r} has no PCell template for {logical_name!r}")
        return self.pcell_templates[canonical_name]

    def resolve_pcell_logical_name(self, logical_name: str) -> str:
        name = str(logical_name)
        if name in self.pcell_templates:
            return name
        alias_map = {str(alias): str(target) for alias, target in self.pcell_aliases.items()}
        return alias_map.get(name, name)

    def has_pcell_template(self, logical_name: str) -> bool:
        canonical_name = self.resolve_pcell_logical_name(logical_name)
        return canonical_name in self.pcell_templates

    def pcell_template_aliases_for(self, logical_name: str) -> tuple[str, ...]:
        canonical_name = self.resolve_pcell_logical_name(logical_name)
        aliases = [canonical_name]
        aliases.extend(
            str(alias)
            for alias, target in self.pcell_aliases.items()
            if str(target) == canonical_name and str(alias) != canonical_name
        )
        return tuple(dict.fromkeys(item for item in aliases if item))

    def macro_binding_for(self, logical_name: str) -> MacroBinding:
        name = str(logical_name)
        if name not in self.macro_bindings:
            raise KeyError(f"PDK {self.name!r} has no macro binding for {logical_name!r}")
        binding = self.macro_bindings[name]
        if isinstance(binding, MacroBinding):
            return binding
        return MacroBinding.from_dict(name, dict(binding))

    def has_macro_binding(self, logical_name: str) -> bool:
        return str(logical_name) in self.macro_bindings

    def routing_layer(self, layer: str) -> RoutingLayerRule:
        if layer in self.routing_layers:
            return self.routing_layers[layer]
        role = "power" if layer in self.preferred_power_layers else "signal"
        preferred = layer in self.preferred_power_layers or layer in self.preferred_signal_layers
        return RoutingLayerRule(layer, "any", preferred, role)

    def via_rule_for_layers(self, lower_layer: str, upper_layer: str) -> ViaStackRule | None:
        for via in self.via_stack:
            if via.lower_layer == lower_layer and via.upper_layer == upper_layer:
                return via
            if via.lower_layer == upper_layer and via.upper_layer == lower_layer:
                return via
        return None

    def extraction_corner(self, name: str) -> ExtractionCorner:
        if name in self.extraction_corners:
            return self.extraction_corners[name]
        if not self.extraction_corners:
            return ExtractionCorner(str(name))
        raise KeyError(f"PDK {self.name!r} has no extraction corner {name!r}")

    def signoff_preset(self, name: str) -> SpectreSignoffPreset:
        if name in self.signoff_presets:
            return self.signoff_presets[name]
        raise KeyError(f"PDK {self.name!r} has no signoff preset {name!r}")

    def has_signoff_preset(self, name: str) -> bool:
        return str(name) in self.signoff_presets


_GRID_UNIT_EPS = 1e-9


def _floor_to_grid_units(value_nm: float, grid_nm: int) -> int:
    return int(floor(float(value_nm) / grid_nm + _GRID_UNIT_EPS))


def _ceil_to_grid_units(value_nm: float, grid_nm: int) -> int:
    return int(ceil(float(value_nm) / grid_nm - _GRID_UNIT_EPS))


def _round_to_grid_units(value_nm: float, grid_nm: int) -> int:
    scaled = float(value_nm) / grid_nm
    if scaled >= 0:
        return int(floor(scaled + 0.5))
    return int(ceil(scaled - 0.5))


def _parameter_context(params: Mapping[str, Any]) -> dict[str, Any]:
    context = dict(params)
    for key, value in params.items():
        context.setdefault(str(key).lower(), value)
        context.setdefault(str(key).upper(), value)
    return context


def _is_legacy_parameter_map_entry(source: str, target: str, raw_context: Mapping[str, Any]) -> bool:
    return source in raw_context and _is_identifier(target) and target not in raw_context


def _is_identifier(value: str) -> bool:
    return value.isidentifier()


def _evaluate_parameter_expression(expression: str, context: Mapping[str, Any]) -> Any:
    if not isinstance(expression, str):
        return expression
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return expression
    if isinstance(tree.body, ast.Name) and tree.body.id not in context:
        return expression
    return _eval_ast_node(tree.body, context)


def _eval_ast_node(node: ast.AST, context: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in context:
            raise ValueError(f"unknown parameter name {node.id!r} in PCell parameter expression")
        return context[node.id]
    if isinstance(node, ast.UnaryOp):
        value = _eval_ast_node(node.operand, context)
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.USub):
            return -value
    if isinstance(node, ast.BinOp):
        left = _eval_ast_node(node.left, context)
        right = _eval_ast_node(node.right, context)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Pow):
            return left**right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        allowed = {"abs": abs, "float": float, "int": int, "max": max, "min": min, "round": round}
        if node.func.id not in allowed:
            raise ValueError(f"unsupported function {node.func.id!r} in PCell parameter expression")
        args = [_eval_ast_node(arg, context) for arg in node.args]
        if node.keywords:
            raise ValueError("keyword arguments are not supported in PCell parameter expressions")
        return allowed[node.func.id](*args)
    raise ValueError(f"unsupported PCell parameter expression: {ast.dump(node, include_attributes=False)}")
