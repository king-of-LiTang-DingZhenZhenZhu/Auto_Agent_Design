"""Calibrated PCell unit-library view of PDK metadata.

The PDK JSON is the source of truth for calibrated native PCell realizations,
but the layout/SMT code should not have to interpret raw metadata rows every
time.  This module exposes a small typed library of candidates that have:

* a legal layout bounding box,
* explicit native PCell/CDF parameters, and
* Calibre DRC/LVS usability status.

By default only Calibre-clean candidates are exported.  DRC-dirty or LVS-dirty
rows can remain in the PDK metadata as research/calibration data without being
selected by layout generation.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import sqrt
from pathlib import Path
from typing import Any


DEFAULT_UNIT_LIBRARY_LOGICALS = ("bjt", "resistor", "capacitor")
DEFAULT_BJT_ARRAY_SPACING_UM = 0.5
DEFAULT_PASSIVE_ARRAY_SPACING_UM = 0.5
DEFAULT_PASSIVE_ARRAY_UNIT_COUNTS = (2, 4, 8)


@dataclass(frozen=True)
class PCellUnitCandidate:
    """One calibrated native PCell realization candidate.

    ``pcell_params`` are the exact CDF/native PCell parameters that must be
    passed to layout instantiation.  They are intentionally separate from
    ``sizing_overrides`` so the SMT solver can reason about geometry while the
    final streamout still receives the native parameters that produced that
    geometry during calibration.
    """

    logical_name: str
    name: str
    lib_name: str
    cell_name: str
    view_name: str
    width_um: float
    height_um: float
    pcell_params: Mapping[str, Any] = field(default_factory=dict)
    sizing_overrides: Mapping[str, Any] = field(default_factory=dict)
    realization_kind: str = "native"
    terminals: tuple[str, ...] = ()
    terminal_access: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    cost: int = 0
    drc_clean: bool = False
    lvs_clean: bool = False
    calibre_status: str = ""
    usable_for_layout: bool = False
    notes: str = ""
    source: str = "pdk_metadata"
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def area_um2(self) -> float:
        return float(self.width_um) * float(self.height_um)

    @property
    def clean(self) -> bool:
        return bool(self.drc_clean and self.lvs_clean and self.usable_for_layout)

    def smt_sizing_overrides(self) -> dict[str, Any]:
        """Return geometry/status data to write back after SMT selection."""

        overrides = dict(_mapping(self.sizing_overrides))
        overrides.update(
            {
                "layout_width_um": float(self.width_um),
                "layout_height_um": float(self.height_um),
                "native_pcell_realization": True,
                "calibrated_pcell_realization": bool(self.clean),
                "configured_pcell_params": bool(self.pcell_params),
                "pcell_calibre_status": str(self.calibre_status),
                "pcell_calibre_usable_for_layout": bool(self.usable_for_layout),
                "pcell_unit_library": True,
                "pcell_unit_candidate": str(self.name),
                "pcell_realization_kind": str(self.realization_kind),
                "pcell_realization_source": str(self.source),
            }
        )
        return overrides

    def to_smt_candidate_spec(self) -> object:
        """Convert to the existing AnalogLayout DSL PCell candidate spec."""

        from analogskills.layout.analog_layout_dsl import pcell_candidate

        return pcell_candidate(
            self.name,
            self.width_um,
            self.height_um,
            sizing_overrides=self.smt_sizing_overrides(),
            pcell_overrides=dict(_mapping(self.pcell_params)),
            cost=self.cost,
            drc_clean=self.drc_clean,
            lvs_clean=self.lvs_clean,
            notes=self.notes,
            metadata=self.smt_metadata(),
        )

    def smt_metadata(self) -> dict[str, Any]:
        """Return solver-facing shape metadata and secondary cost terms.

        The SMT solver already optimizes candidate width/height through the
        placement objective.  This metadata captures engineering preferences
        that are not visible from bbox alone: whether the realization is a
        regular unit array, whether it is very elongated, and whether streamout
        must expand many primitive cells.  The costs are deliberately small; they
        rank equivalent/near-equivalent geometry without overriding hard
        compactness.
        """

        return _candidate_shape_metadata(
            logical_name=self.logical_name,
            realization_kind=self.realization_kind,
            name=self.name,
            width_um=self.width_um,
            height_um=self.height_um,
            sizing_overrides=self.sizing_overrides,
            pcell_params=self.pcell_params,
            source=self.source,
            clean=self.clean,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_name": self.logical_name,
            "name": self.name,
            "lib_name": self.lib_name,
            "cell_name": self.cell_name,
            "view_name": self.view_name,
            "width_um": self.width_um,
            "height_um": self.height_um,
            "area_um2": self.area_um2,
            "pcell_params": dict(_mapping(self.pcell_params)),
            "sizing_overrides": dict(_mapping(self.sizing_overrides)),
            "realization_kind": self.realization_kind,
            "terminals": list(self.terminals),
            "terminal_access": {
                str(term): dict(_mapping(access)) for term, access in dict(_mapping(self.terminal_access)).items()
            },
            "cost": self.cost,
            "drc_clean": self.drc_clean,
            "lvs_clean": self.lvs_clean,
            "calibre_status": self.calibre_status,
            "usable_for_layout": self.usable_for_layout,
            "notes": self.notes,
            "source": self.source,
        }


@dataclass(frozen=True)
class PCellUnitLibrary:
    """Typed collection of calibrated PCell unit candidates."""

    pdk_name: str
    candidates: tuple[PCellUnitCandidate, ...] = ()

    def candidates_for(self, logical_name: str, *, clean_only: bool = True) -> tuple[PCellUnitCandidate, ...]:
        logical = str(logical_name).lower()
        rows = tuple(candidate for candidate in self.candidates if candidate.logical_name == logical)
        if clean_only:
            rows = tuple(candidate for candidate in rows if candidate.clean)
        return tuple(sorted(rows, key=lambda item: (item.cost, item.area_um2, item.name)))

    def by_name(self, name: str) -> PCellUnitCandidate:
        target = str(name)
        for candidate in self.candidates:
            if candidate.name == target:
                return candidate
        raise KeyError(f"unknown PCell unit candidate {target!r}")

    def smt_candidates_for(self, logical_name: str, *, clean_only: bool = True) -> tuple[object, ...]:
        return tuple(candidate.to_smt_candidate_spec() for candidate in self.candidates_for(logical_name, clean_only=clean_only))

    def summary(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for candidate in self.candidates:
            bucket = result.setdefault(candidate.logical_name, {"total": 0, "clean": 0, "dirty": 0})
            bucket["total"] += 1
            if candidate.clean:
                bucket["clean"] += 1
            else:
                bucket["dirty"] += 1
        return result

    def to_markdown(self, path: str | Path | None = None) -> str:
        lines = [
            f"# PCell unit library: {self.pdk_name}",
            "",
            "Only candidates with explicit layout dimensions and native PCell parameters are listed here.",
            "",
            "| Logical | Candidate | Cell | W x H (um) | Area (um^2) | Calibre | DRC | LVS | Params |",
            "| --- | --- | --- | ---: | ---: | --- | --- | --- | --- |",
        ]
        for candidate in sorted(self.candidates, key=lambda item: (item.logical_name, item.cost, item.name)):
            params = ", ".join(f"{key}={value}" for key, value in sorted(dict(_mapping(candidate.pcell_params)).items()))
            lines.append(
                "| "
                f"{candidate.logical_name} | "
                f"{candidate.name} | "
                f"{candidate.lib_name}/{candidate.cell_name}/{candidate.view_name} | "
                f"{candidate.width_um:.3f} x {candidate.height_um:.3f} | "
                f"{candidate.area_um2:.3f} | "
                f"{candidate.calibre_status or '-'} | "
                f"{'yes' if candidate.drc_clean else 'no'} | "
                f"{'yes' if candidate.lvs_clean else 'no'} | "
                f"{params} |"
            )
        text = "\n".join(lines) + "\n"
        if path is not None:
            Path(path).write_text(text, encoding="utf-8")
        return text


def build_pcell_unit_library(
    pdk: object,
    *,
    logical_names: Sequence[str] | None = DEFAULT_UNIT_LIBRARY_LOGICALS,
    clean_only: bool = True,
    include_bjt_arrays: bool = True,
    include_passive_arrays: bool = True,
) -> PCellUnitLibrary:
    """Build a calibrated PCell unit library from ``pdk.metadata``.

    Args:
        pdk: A :class:`analogskills.pdk.PdkConfig` instance or a compatible mapping.
        logical_names: Logical device classes to export.  ``None`` means all
            keys under ``metadata.pcell_realization``.
        clean_only: When true, only candidates that are DRC clean, LVS clean,
            and marked usable for layout are exported.
        include_bjt_arrays: When true, derive virtual BJT array candidates
            from the clean M1 primitive and configured calibration-sweep M
            values.  These are not new native PCells; they are macro
            realizations that streamout must expand into M clean primitive
            instances.
        include_passive_arrays: When true, derive virtual resistor/capacitor
            array candidates from clean primitive PCells.  They are intended
            for explicit multiplicity/schematic-expansion flows; they are not
            substitutes for uncalibrated native aspect mutations.
    """

    metadata = _metadata(pdk)
    realization = _mapping(metadata.get("pcell_realization", {}))
    if logical_names is None:
        selected_logicals = tuple(str(key).lower() for key in realization.keys())
    else:
        selected_logicals = tuple(str(key).lower() for key in logical_names)

    candidates: list[PCellUnitCandidate] = []
    for logical in selected_logicals:
        cfg = _mapping(realization.get(logical, {}))
        if not cfg:
            continue
        template = _pcell_template(pdk, logical)
        terminal_access = _terminal_access_from_template(template)
        terminals = _terminals_from_config_or_template(cfg, terminal_access)
        for index, raw_item in enumerate(tuple(cfg.get("candidates", ()) or ())):
            item = _mapping(raw_item)
            candidate = _candidate_from_metadata_item(
                pdk,
                logical,
                item,
                index=index,
                template=template,
                terminal_access=terminal_access,
                terminals=terminals,
            )
            if candidate is None:
                continue
            if clean_only and not candidate.clean:
                continue
            candidates.append(candidate)
        if logical == "bjt" and include_bjt_arrays:
            primitive_candidates = tuple(
                candidate
                for candidate in candidates
                if candidate.logical_name == "bjt"
                and candidate.clean
                and _candidate_m_value(candidate) == 1
                and _mapping(candidate.pcell_params)
            )
            if primitive_candidates:
                m_values = _configured_bjt_array_m_values(cfg)
                gap_um = _positive_float(cfg.get("array_spacing_um", cfg.get("array_gap_um")))
                if gap_um <= 0.0:
                    gap_um = DEFAULT_BJT_ARRAY_SPACING_UM
                for primitive in primitive_candidates[:1]:
                    candidates.extend(
                        build_bjt_unit_array_candidates(
                            primitive,
                            m_values=m_values,
                            spacing_um=gap_um,
                            clean_only=clean_only,
                        )
                    )
        elif logical in {"resistor", "capacitor"} and include_passive_arrays:
            primitive_candidates = tuple(
                candidate
                for candidate in candidates
                if candidate.logical_name == logical
                and candidate.clean
                and candidate.realization_kind == "native"
                and _mapping(candidate.pcell_params)
            )
            if primitive_candidates:
                m_values = _configured_passive_array_unit_counts(cfg)
                gap_um = _configured_passive_array_spacing_um(pdk, cfg, logical)
                for primitive in primitive_candidates:
                    candidates.extend(
                        build_passive_unit_array_candidates(
                            primitive,
                            unit_counts=m_values,
                            spacing_um=gap_um,
                            clean_only=clean_only,
                        )
                    )
    return PCellUnitLibrary(_pdk_name(pdk), tuple(candidates))


def build_bjt_unit_array_candidates(
    primitive: PCellUnitCandidate,
    *,
    m_values: Sequence[int],
    spacing_um: float = DEFAULT_BJT_ARRAY_SPACING_UM,
    clean_only: bool = True,
) -> tuple[PCellUnitCandidate, ...]:
    """Derive BJT array macro candidates from one clean M1 primitive."""

    if primitive.logical_name != "bjt":
        return ()
    if clean_only and not primitive.clean:
        return ()
    unit_width = float(primitive.width_um)
    unit_height = float(primitive.height_um)
    gap = max(0.0, float(spacing_um))
    candidates: list[PCellUnitCandidate] = []
    for raw_m in m_values:
        unit_count = max(1, int(raw_m))
        if unit_count <= 1:
            continue
        for rows, cols in _factor_pairs(unit_count):
            width = cols * unit_width + max(0, cols - 1) * gap
            height = rows * unit_height + max(0, rows - 1) * gap
            aspect_penalty = abs(float(cols) / max(float(rows), 1.0) - 1.0)
            spec = {
                "enabled": True,
                "unit_count": unit_count,
                "rows": rows,
                "cols": cols,
                "unit_candidate": primitive.name,
                "unit_width_um": unit_width,
                "unit_height_um": unit_height,
                "spacing_um": gap,
                "pitch_x_um": unit_width + gap,
                "pitch_y_um": unit_height + gap,
                "unit_pcell_params": dict(_mapping(primitive.pcell_params)),
            }
            candidates.append(
                PCellUnitCandidate(
                    logical_name="bjt",
                    name=f"{primitive.name}_array_M{unit_count}_{rows}x{cols}",
                    lib_name=primitive.lib_name,
                    cell_name=primitive.cell_name,
                    view_name=primitive.view_name,
                    width_um=width,
                    height_um=height,
                    pcell_params=dict(_mapping(primitive.pcell_params)),
                    sizing_overrides={
                        "M": unit_count,
                        "m": unit_count,
                        "bjt_unit_array": spec,
                        "layout_width_um": width,
                        "layout_height_um": height,
                        "native_pcell_realization": True,
                        "calibrated_pcell_realization": bool(primitive.clean),
                        "configured_pcell_params": True,
                        "pcell_calibre_status": "primitive_clean_array",
                        "pcell_calibre_usable_for_layout": bool(primitive.usable_for_layout),
                    },
                    realization_kind="bjt_unit_array",
                    terminals=primitive.terminals,
                    terminal_access=primitive.terminal_access,
                    cost=int(100 + unit_count * 10 + rows + cols + round(aspect_penalty * 10.0)),
                    drc_clean=primitive.drc_clean,
                    lvs_clean=primitive.lvs_clean,
                    calibre_status="primitive_clean_array",
                    usable_for_layout=primitive.usable_for_layout,
                    notes=(
                        f"Virtual BJT array realization: {unit_count} x {primitive.name} "
                        f"as {rows} rows x {cols} cols. Streamout must expand this macro "
                        "into primitive clean PCells."
                    ),
                    source="derived_bjt_unit_array",
                    raw={"primitive": primitive.to_dict(), "bjt_unit_array": spec},
                )
            )
    return tuple(candidates)


def bjt_unit_array_candidates_for_m(
    pdk: object,
    multiplicity: int,
    *,
    clean_only: bool = True,
) -> tuple[PCellUnitCandidate, ...]:
    """Return derived BJT array candidates for one requested electrical M."""

    library = build_pcell_unit_library(
        pdk,
        logical_names=("bjt",),
        clean_only=clean_only,
        include_bjt_arrays=True,
    )
    target_m = max(1, int(multiplicity))
    return tuple(
        candidate
        for candidate in library.candidates_for("bjt", clean_only=clean_only)
        if _candidate_m_value(candidate) == target_m and candidate.realization_kind == "bjt_unit_array"
    )


def build_passive_unit_array_candidates(
    primitive: PCellUnitCandidate,
    *,
    unit_counts: Sequence[int] = DEFAULT_PASSIVE_ARRAY_UNIT_COUNTS,
    spacing_um: float = DEFAULT_PASSIVE_ARRAY_SPACING_UM,
    clean_only: bool = True,
) -> tuple[PCellUnitCandidate, ...]:
    """Derive resistor/capacitor array macro candidates from clean primitives."""

    logical = str(primitive.logical_name).lower()
    if logical not in {"resistor", "capacitor"}:
        return ()
    if clean_only and not primitive.clean:
        return ()
    unit_width = float(primitive.width_um)
    unit_height = float(primitive.height_um)
    gap = max(0.0, float(spacing_um))
    candidates: list[PCellUnitCandidate] = []
    for raw_count in unit_counts:
        unit_count = max(1, int(raw_count))
        if unit_count <= 1:
            continue
        for rows, cols in _factor_pairs(unit_count):
            width = cols * unit_width + max(0, cols - 1) * gap
            height = rows * unit_height + max(0, rows - 1) * gap
            aspect_penalty = abs(float(cols) / max(float(rows), 1.0) - 1.0)
            spec = {
                "enabled": True,
                "logical_name": logical,
                "unit_count": unit_count,
                "rows": rows,
                "cols": cols,
                "unit_candidate": primitive.name,
                "unit_width_um": unit_width,
                "unit_height_um": unit_height,
                "spacing_um": gap,
                "pitch_x_um": unit_width + gap,
                "pitch_y_um": unit_height + gap,
                "unit_pcell_params": dict(_mapping(primitive.pcell_params)),
                "connection_mode": "parallel",
                "requires_schematic_expansion": True,
            }
            sizing_overrides = dict(_mapping(primitive.sizing_overrides))
            sizing_overrides.update(
                {
                    "M": unit_count,
                    "m": unit_count,
                    "multi": unit_count,
                    "passive_unit_array": spec,
                    "layout_width_um": width,
                    "layout_height_um": height,
                    "use_drawn_primitive": False,
                    "allow_pcell_aspect_candidates": True,
                    "native_pcell_realization": True,
                    "calibrated_pcell_realization": bool(primitive.clean),
                    "configured_pcell_params": True,
                    "pcell_calibre_status": "primitive_clean_array",
                    "pcell_calibre_usable_for_layout": bool(primitive.usable_for_layout),
                    "requires_schematic_expansion": True,
                }
            )
            candidates.append(
                PCellUnitCandidate(
                    logical_name=logical,
                    name=f"{primitive.name}_array_M{unit_count}_{rows}x{cols}",
                    lib_name=primitive.lib_name,
                    cell_name=primitive.cell_name,
                    view_name=primitive.view_name,
                    width_um=width,
                    height_um=height,
                    pcell_params=dict(_mapping(primitive.pcell_params)),
                    sizing_overrides=sizing_overrides,
                    realization_kind=f"{logical}_unit_array",
                    terminals=primitive.terminals,
                    terminal_access=primitive.terminal_access,
                    cost=int(200 + primitive.cost * 20 + unit_count * 10 + rows + cols + round(aspect_penalty * 10.0)),
                    drc_clean=primitive.drc_clean,
                    lvs_clean=primitive.lvs_clean,
                    calibre_status="primitive_clean_array",
                    usable_for_layout=primitive.usable_for_layout,
                    notes=(
                        f"Virtual {logical} array realization: {unit_count} x {primitive.name} "
                        f"as {rows} rows x {cols} cols. Streamout expands this macro into "
                        "primitive clean PCells; schematic/LVS flow must use matching expansion "
                        "or verified equivalence."
                    ),
                    source=f"derived_{logical}_unit_array",
                    raw={"primitive": primitive.to_dict(), "passive_unit_array": spec},
                )
            )
    return tuple(candidates)


def passive_unit_array_candidates_for_m(
    pdk: object,
    logical_name: str,
    multiplicity: int,
    *,
    clean_only: bool = True,
) -> tuple[PCellUnitCandidate, ...]:
    """Return derived passive array candidates for one requested multiplicity."""

    logical = str(logical_name).lower()
    if logical not in {"resistor", "capacitor"}:
        return ()
    library = build_pcell_unit_library(
        pdk,
        logical_names=(logical,),
        clean_only=clean_only,
        include_bjt_arrays=False,
        include_passive_arrays=True,
    )
    target_m = max(1, int(multiplicity))
    return tuple(
        candidate
        for candidate in library.candidates_for(logical, clean_only=clean_only)
        if _candidate_m_value(candidate) == target_m and candidate.realization_kind == f"{logical}_unit_array"
    )


def _configured_passive_array_spacing_um(pdk: object, cfg: Mapping[str, Any], logical: str) -> float:
    local = _positive_float(cfg.get("array_spacing_um", cfg.get("array_gap_um")))
    if local > 0.0:
        return local
    metadata = _metadata(pdk)
    calibre = _mapping(metadata.get("calibre", {}))
    passive = _mapping(calibre.get("passive_array", {}))
    for key in (
        "minimum_access_array_spacing_um_by_logical",
        "access_array_spacing_um_by_logical",
        "minimum_array_spacing_um_by_logical",
        "spacing_um_by_logical",
        "array_spacing_um_by_logical",
    ):
        by_logical = _mapping(passive.get(key, {}))
        spacing = _positive_float(by_logical.get(str(logical).lower(), by_logical.get(str(logical), by_logical.get("*"))))
        if spacing > 0.0:
            return spacing
    for key in (
        "minimum_access_array_spacing_nm_by_logical",
        "access_array_spacing_nm_by_logical",
        "minimum_array_spacing_nm_by_logical",
        "spacing_nm_by_logical",
        "array_spacing_nm_by_logical",
    ):
        by_logical = _mapping(passive.get(key, {}))
        spacing_nm = _positive_float(by_logical.get(str(logical).lower(), by_logical.get(str(logical), by_logical.get("*"))))
        if spacing_nm > 0.0:
            return spacing_nm * 1e-3
    spacing = _positive_float(
        passive.get(
            "minimum_access_array_spacing_um",
            passive.get("access_array_spacing_um", passive.get("minimum_array_spacing_um", passive.get("spacing_um"))),
        )
    )
    if spacing > 0.0:
        return spacing
    spacing_nm = _positive_float(
        passive.get(
            "minimum_access_array_spacing_nm",
            passive.get("access_array_spacing_nm", passive.get("minimum_array_spacing_nm", passive.get("spacing_nm"))),
        )
    )
    if spacing_nm > 0.0:
        return spacing_nm * 1e-3
    return DEFAULT_PASSIVE_ARRAY_SPACING_UM


def _candidate_from_metadata_item(
    pdk: object,
    logical: str,
    item: Mapping[str, Any],
    *,
    index: int,
    template: object | None,
    terminal_access: Mapping[str, Mapping[str, Any]],
    terminals: tuple[str, ...],
) -> PCellUnitCandidate | None:
    width_um = _positive_float(item.get("layout_width_um", item.get("width_um")))
    height_um = _positive_float(item.get("layout_height_um", item.get("height_um")))
    if width_um <= 0.0 or height_um <= 0.0:
        return None

    pcell_params = _first_mapping(item, ("pcell_params", "pcell_overrides", "params"))
    sizing_overrides = dict(_mapping(item.get("sizing_overrides", {})))
    if "M" in item and "M" not in sizing_overrides:
        sizing_overrides["M"] = item.get("M")
    if "m" in item and "m" not in sizing_overrides:
        sizing_overrides["m"] = item.get("m")

    status = str(item.get("pcell_calibre_status", "") or "").lower()
    status_clean = status in {"clean", "drc_lvs_clean", "calibre_clean", "ok"}
    drc_clean = _truthy(item.get("drc_clean"), default=status_clean)
    lvs_clean = _truthy(item.get("lvs_clean"), default=status_clean)
    usable = _truthy(item.get("pcell_calibre_usable_for_layout"), default=bool(drc_clean and lvs_clean and status_clean))
    if not status and drc_clean and lvs_clean and usable:
        status = "clean"

    return PCellUnitCandidate(
        logical_name=str(logical).lower(),
        name=str(item.get("name", f"{logical}_unit_{index}")),
        lib_name=str(item.get("lib_name", _template_attr(template, "resolved_layout_lib_name", "lib_name", ""))),
        cell_name=str(item.get("cell_name", _template_attr(template, "resolved_layout_cell_name", "cell_name", logical))),
        view_name=str(item.get("view_name", _template_attr(template, "resolved_layout_view_name", "view_name", "layout"))),
        width_um=width_um,
        height_um=height_um,
        pcell_params=pcell_params,
        sizing_overrides=sizing_overrides,
        realization_kind=str(item.get("pcell_realization_kind", item.get("realization_kind", "native"))),
        terminals=_terminals_from_config_or_template(item, terminal_access, default=terminals),
        terminal_access=terminal_access,
        cost=int(item.get("cost", index) or index),
        drc_clean=drc_clean,
        lvs_clean=lvs_clean,
        calibre_status=status,
        usable_for_layout=usable,
        notes=str(item.get("notes", "")),
        source=str(item.get("pcell_realization_source", item.get("source", "pdk_metadata"))),
        raw=dict(item),
    )


def _metadata(pdk: object) -> Mapping[str, Any]:
    if isinstance(pdk, Mapping):
        return _mapping(pdk.get("metadata", {}))
    return _mapping(getattr(pdk, "metadata", {}))


def _pdk_name(pdk: object) -> str:
    if isinstance(pdk, Mapping):
        return str(pdk.get("name", "unnamed_pdk"))
    return str(getattr(pdk, "name", "unnamed_pdk"))


def _pcell_template(pdk: object, logical: str) -> object | None:
    if isinstance(pdk, Mapping):
        return _mapping(_mapping(pdk.get("pcell_templates", {})).get(logical, {})) or None
    templates = getattr(pdk, "pcell_templates", {})
    if isinstance(templates, Mapping):
        return templates.get(logical)
    return None


def _terminal_access_from_template(template: object | None) -> Mapping[str, Mapping[str, Any]]:
    if template is None:
        return {}
    if isinstance(template, Mapping):
        access = _mapping(template.get("terminal_access", {}))
    else:
        access = _mapping(getattr(template, "terminal_access", {}))
    return {str(term): dict(_mapping(config)) for term, config in dict(access).items()}


def _terminals_from_config_or_template(
    item: Mapping[str, Any],
    terminal_access: Mapping[str, Mapping[str, Any]],
    *,
    default: tuple[str, ...] = (),
) -> tuple[str, ...]:
    if item.get("terminals"):
        return tuple(str(term) for term in tuple(item.get("terminals", ()) or ()) if str(term))
    if default:
        return default
    return tuple(str(term) for term in terminal_access.keys())


def _first_mapping(item: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    for key in keys:
        value = _mapping(item.get(key, {}))
        if value:
            return {str(param): raw for param, raw in dict(value).items()}
    return {}


def _configured_bjt_array_m_values(cfg: Mapping[str, Any]) -> tuple[int, ...]:
    values: list[int] = []
    for item_obj in tuple(cfg.get("candidates", ()) or ()):
        item = _mapping(item_obj)
        sizing = _mapping(item.get("sizing_overrides", {}))
        for key in ("M", "m"):
            raw = sizing.get(key, item.get(key))
            if raw is None:
                continue
            try:
                values.append(max(1, int(float(raw))))
            except (TypeError, ValueError):
                pass
    sweep = _mapping(cfg.get("calibration_sweep", {}))
    for key in ("M", "m"):
        for raw in tuple(sweep.get(key, ()) or ()):
            try:
                values.append(max(1, int(float(raw))))
            except (TypeError, ValueError):
                pass
    return tuple(dict.fromkeys(values or (1,)))


def _configured_passive_array_unit_counts(cfg: Mapping[str, Any]) -> tuple[int, ...]:
    raw_values = (
        cfg.get("array_unit_counts")
        or cfg.get("array_m_values")
        or cfg.get("multiplicity_values")
        or DEFAULT_PASSIVE_ARRAY_UNIT_COUNTS
    )
    values: list[int] = []
    for raw in tuple(raw_values or ()):
        try:
            values.append(max(1, int(float(raw))))
        except (TypeError, ValueError):
            pass
    return tuple(dict.fromkeys(values or DEFAULT_PASSIVE_ARRAY_UNIT_COUNTS))


def _candidate_m_value(candidate: PCellUnitCandidate) -> int:
    sizing = _mapping(candidate.sizing_overrides)
    params = _mapping(candidate.pcell_params)
    for key in ("M", "m"):
        raw = sizing.get(key, params.get(key))
        if raw is None:
            continue
        try:
            return max(1, int(float(raw)))
        except (TypeError, ValueError):
            continue
    return 1


def _candidate_shape_metadata(
    *,
    logical_name: str,
    realization_kind: str,
    name: str,
    width_um: float,
    height_um: float,
    sizing_overrides: Mapping[str, Any],
    pcell_params: Mapping[str, Any],
    source: str,
    clean: bool,
) -> dict[str, Any]:
    width = max(float(width_um), 1e-9)
    height = max(float(height_um), 1e-9)
    area = width * height
    long_side = max(width, height)
    short_side = max(min(width, height), 1e-9)
    aspect = long_side / short_side
    shape_class = _shape_class(width, height)
    overrides = _mapping(sizing_overrides)
    array = _mapping(overrides.get("bjt_unit_array", overrides.get("passive_unit_array", {})))
    unit_count = _positive_int(array.get("unit_count"), 1)
    rows = _positive_int(array.get("rows"), 1)
    cols = _positive_int(array.get("cols"), 1)
    is_array = bool(array) or unit_count > 1 or "array" in str(realization_kind)
    aspect_cost = max(0, int(round((aspect - 1.0) * 4.0)))
    array_cost = max(0, unit_count - 1) if is_array else 0
    regularity_cost = 0
    if is_array and unit_count > 1:
        regularity_cost = abs(int(rows) - int(cols))
    topology_cost = 0 if shape_class in {"square", "compact"} else 2
    fragmentation_cost = max(0, unit_count - 4) if is_array else 0
    route_access_cost = 0
    if is_array and min(rows, cols) > 1:
        route_access_cost = max(0, min(rows, cols) - 1)
    pin_access_cost = 0 if bool(clean) else 50
    return {
        "pcell_unit_library": True,
        "logical_name": str(logical_name),
        "candidate_name": str(name),
        "realization_kind": str(realization_kind),
        "realization_source": str(source),
        "width_um": width,
        "height_um": height,
        "area_um2": area,
        "aspect_ratio": aspect,
        "shape_class": shape_class,
        "topology": f"unit_array_{rows}x{cols}" if is_array else "native",
        "unit_count": unit_count,
        "rows": rows,
        "cols": cols,
        "has_pcell_params": bool(_mapping(pcell_params)),
        "calibre_clean": bool(clean),
        "shape_cost": topology_cost + aspect_cost,
        "aspect_cost": aspect_cost,
        "topology_cost": topology_cost,
        "array_cost": array_cost,
        "regularity_cost": regularity_cost,
        "route_access_cost": route_access_cost,
        "fragmentation_cost": fragmentation_cost,
        "pin_access_cost": pin_access_cost,
    }


def _shape_class(width_um: float, height_um: float) -> str:
    width = max(float(width_um), 1e-9)
    height = max(float(height_um), 1e-9)
    ratio = max(width, height) / max(min(width, height), 1e-9)
    if ratio <= 1.15:
        return "square"
    if ratio <= 2.0:
        return "compact"
    return "wide" if width > height else "tall"


def _positive_int(value: object | None, default: int) -> int:
    try:
        parsed = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return max(1, int(default))
    return max(1, parsed)


def _factor_pairs(value: int) -> tuple[tuple[int, int], ...]:
    count = max(1, int(value))
    rows: list[tuple[int, int]] = []
    for row in range(1, int(sqrt(count)) + 1):
        if count % row:
            continue
        col = count // row
        rows.append((row, col))
        if row != col:
            rows.append((col, row))
    return tuple(sorted(rows, key=lambda item: (abs(item[0] - item[1]), item[0], item[1])))


def _mapping(value: object | None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _positive_float(value: object | None) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed > 0.0 else 0.0


def _truthy(value: object | None, *, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "clean", "ok"}:
        return True
    if text in {"0", "false", "no", "n", "dirty", "fail", "failed"}:
        return False
    return bool(default)


def _template_attr(template: object | None, method_name: str, attr_name: str, fallback: str) -> str:
    if template is None:
        return fallback
    if isinstance(template, Mapping):
        return str(template.get(attr_name, fallback))
    method = getattr(template, method_name, None)
    if callable(method):
        return str(method())
    return str(getattr(template, attr_name, fallback))
