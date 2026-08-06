"""Calibre-backed PCell DRC/LVS calibration catalog.

The OA introspection cache tells us where terminals are likely routable.  This
module answers the harder signoff question: for a concrete native PCell
parameterization, does Calibre DRC/LVS accept the isolated device, and which
source model/parameters make LVS correct?
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from itertools import product
import json
from math import ceil, sqrt
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

from analogskills.eda.oa import (
    OaCellView,
    OaInstance,
    OaPin,
    OaRect,
    OaWritePlan,
    save_oa_plan_json,
    snap_oa_write_plan_to_grid,
    write_oa_skill,
)
from analogskills.pcell.access import PCellTerminalAccessor
from analogskills.pcell.calibration import PCellCalibrationCache
from analogskills.pcell.generation import PCellInstancePlan
from analogskills.pdk import DesignRuleDeck, PdkConfig


IGNORED_DRC_RULE_PREFIXES = ("G.", "LUP.")
IGNORED_DRC_RULE_NAMES = {
    "EFP_rules_are_OFF:WARNING",
    "IO_CONNECT_CORE_NET_VOLTAGE_IS_CORE:WARNING1",
    "FLIP_CHIP_WITHOUT_28K_AP:WARNING",
    "DIODMY_L:WARNING",
}


@dataclass(frozen=True)
class PCellCalibreTarget:
    """One isolated native PCell realization to verify with Calibre."""

    name: str
    logical_name: str
    lib_name: str
    cell_name: str
    view_name: str = "layout"
    params: dict[str, Any] = field(default_factory=dict)
    terminals: tuple[str, ...] = ()
    source_model: str = ""
    source_params: dict[str, Any] = field(default_factory=dict)
    orient: str = "R0"
    instantiation_method: str = "dbCreateInstByMasterName"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def pcell_key(self) -> str:
        return f"{self.lib_name}/{self.cell_name}/{self.view_name}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pcell_key"] = self.pcell_key
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellCalibreTarget":
        return cls(
            name=str(data.get("name", "")),
            logical_name=str(data.get("logical_name", "")),
            lib_name=str(data.get("lib_name", "")),
            cell_name=str(data.get("cell_name", "")),
            view_name=str(data.get("view_name", "layout")),
            params=dict(data.get("params", {})),
            terminals=tuple(str(item) for item in data.get("terminals", ())),
            source_model=str(data.get("source_model", "")),
            source_params=dict(data.get("source_params", {})),
            orient=str(data.get("orient", "R0")),
            instantiation_method=str(data.get("instantiation_method", "dbCreateInstByMasterName")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class PCellCalibreArtifacts:
    target: PCellCalibreTarget
    cell: str
    layout_json: str
    layout_skill: str
    source_netlist: str
    native_gds: str
    drc_deck: str
    lvs_deck: str
    drc_report: str
    lvs_report: str
    streamout_log: str
    drc_log: str
    lvs_log: str

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "target": self.target.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellCalibreArtifacts":
        return cls(
            target=PCellCalibreTarget.from_dict(data.get("target", {})),
            cell=str(data.get("cell", "")),
            layout_json=str(data.get("layout_json", "")),
            layout_skill=str(data.get("layout_skill", "")),
            source_netlist=str(data.get("source_netlist", "")),
            native_gds=str(data.get("native_gds", "")),
            drc_deck=str(data.get("drc_deck", "")),
            lvs_deck=str(data.get("lvs_deck", "")),
            drc_report=str(data.get("drc_report", "")),
            lvs_report=str(data.get("lvs_report", "")),
            streamout_log=str(data.get("streamout_log", "")),
            drc_log=str(data.get("drc_log", "")),
            lvs_log=str(data.get("lvs_log", "")),
        )


@dataclass(frozen=True)
class PCellCalibreCatalogEntry:
    target: PCellCalibreTarget
    artifacts: PCellCalibreArtifacts | None = None
    classification: dict[str, Any] = field(default_factory=dict)
    drc_summary: dict[str, Any] = field(default_factory=dict)
    lvs_summary: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)

    @property
    def usable_for_layout(self) -> bool:
        return str(self.classification.get("status", "")) == "clean"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "artifacts": self.artifacts.to_dict() if self.artifacts is not None else None,
            "classification": dict(self.classification),
            "drc_summary": dict(self.drc_summary),
            "lvs_summary": dict(self.lvs_summary),
            "execution": dict(self.execution),
            "usable_for_layout": self.usable_for_layout,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellCalibreCatalogEntry":
        raw_artifacts = data.get("artifacts")
        return cls(
            target=PCellCalibreTarget.from_dict(data.get("target", {})),
            artifacts=PCellCalibreArtifacts.from_dict(raw_artifacts) if isinstance(raw_artifacts, Mapping) else None,
            classification=dict(data.get("classification", {})),
            drc_summary=dict(data.get("drc_summary", {})),
            lvs_summary=dict(data.get("lvs_summary", {})),
            execution=dict(data.get("execution", {})),
        )


@dataclass(frozen=True)
class PCellCalibreCatalog:
    pdk: str
    entries: tuple[PCellCalibreCatalogEntry, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def clean_entries(self) -> tuple[PCellCalibreCatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.usable_for_layout)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdk": self.pdk,
            "entries": [entry.to_dict() for entry in self.entries],
            "metadata": dict(self.metadata),
            "summary": summarize_pcell_calibre_catalog(self),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellCalibreCatalog":
        return cls(
            pdk=str(data.get("pdk", "")),
            entries=tuple(PCellCalibreCatalogEntry.from_dict(item) for item in data.get("entries", ())),
            metadata=dict(data.get("metadata", {})),
        )

    @classmethod
    def load_json(cls, path: str | Path) -> "PCellCalibreCatalog":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    load = load_json

    def save_json(self, path: str | Path) -> Path:
        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path_obj

    save = save_json


def build_crn28_pcell_calibre_targets(
    pdk: PdkConfig,
    *,
    logical_names: Sequence[str] = ("nmos", "pmos", "bjt"),
    mos_widths_nm_by_logical: Mapping[str, Sequence[int]] | None = None,
    mos_lengths_nm_by_logical: Mapping[str, Sequence[int]] | None = None,
    bjt_m_values: Sequence[int] | None = None,
    bjt_source_models: Sequence[str] = ("npn5", "npn", "npn_i_mac"),
    resistor_source_models: Sequence[str] = ("rnodl", "rnod"),
    capacitor_source_models: Sequence[str] = ("nmoscap", "nmoscap_18", "nmoscap_25"),
) -> tuple[PCellCalibreTarget, ...]:
    """Build a compact CRN28 PCell target set from PDK configuration.

    MOS candidates use configured per-finger DRC limits and PCell overrides.
    BJT candidates sweep source model names because this PDK's native ``npn``
    layout is commonly extracted by Calibre as a size-specific model.  Passive
    candidates use the Calibre/SPICE subckt names from the PDK model deck.
    """

    selected = tuple(dict.fromkeys(str(item) for item in logical_names))
    targets: list[PCellCalibreTarget] = []
    mos_widths = {
        "nmos": (4000, 8000, 24000),
        "pmos": (3000, 6000, 24000, 32000, 40000),
        **{str(k): tuple(int(vv) for vv in vals) for k, vals in dict(mos_widths_nm_by_logical or {}).items()},
    }
    mos_lengths = {
        "nmos": (120, 180),
        "pmos": (120, 180),
        **{str(k): tuple(int(vv) for vv in vals) for k, vals in dict(mos_lengths_nm_by_logical or {}).items()},
    }
    for logical in selected:
        if logical in {"nmos", "pmos"}:
            for width_nm in mos_widths.get(logical, ()):
                for length_nm in mos_lengths.get(logical, (180,)):
                    targets.append(build_crn28_mos_calibre_target(pdk, logical, width_nm=width_nm, length_nm=length_nm))
        elif logical == "bjt":
            m_values = tuple(dict.fromkeys(max(1, int(item)) for item in (bjt_m_values or (1,))))
            for model in tuple(dict.fromkeys(str(item) for item in bjt_source_models if str(item))):
                for m_value in m_values:
                    target = build_crn28_bjt_calibre_target(pdk, source_model=model, m=m_value)
                    targets.append(_with_realization_metadata(target, _crn28_bjt_realization_row(pdk, m_value)))
        elif logical == "resistor":
            for model in tuple(dict.fromkeys(str(item) for item in resistor_source_models if str(item))):
                sweep_rows = _crn28_passive_realization_sweep_rows(pdk, "resistor")
                if sweep_rows:
                    for row in sweep_rows:
                        target = build_crn28_resistor_calibre_target(
                                pdk,
                                source_model=model,
                                width_m=float(row.get("width_m", 2e-6) or 2e-6),
                                length_m=float(row.get("length_m", 10e-6) or 10e-6),
                                resistance_ohm=float(row.get("resistance_ohm", 1000.0) or 1000.0),
                                pcell_params=_mapping(row.get("pcell_params", {})),
                                name_suffix=str(row.get("name", "")),
                            )
                        targets.append(_with_realization_metadata(target, row))
                else:
                    targets.append(build_crn28_resistor_calibre_target(pdk, source_model=model))
        elif logical == "capacitor":
            for model in tuple(dict.fromkeys(str(item) for item in capacitor_source_models if str(item))):
                sweep_rows = _crn28_passive_realization_sweep_rows(pdk, "capacitor")
                if sweep_rows:
                    for row in sweep_rows:
                        target = build_crn28_capacitor_calibre_target(
                                pdk,
                                source_model=model,
                                width_m=float(row.get("width_m", 1e-6) or 1e-6),
                                length_m=float(row.get("length_m", 1e-6) or 1e-6),
                                capacitance_f=float(row.get("capacitance_f", 1e-15) or 1e-15),
                                pcell_params=_mapping(row.get("pcell_params", {})),
                                name_suffix=str(row.get("name", "")),
                            )
                        targets.append(_with_realization_metadata(target, row))
                else:
                    targets.append(build_crn28_capacitor_calibre_target(pdk, source_model=model))
        else:
            raise ValueError(f"unsupported CRN28 PCell calibre target logical name {logical!r}")
    return tuple(_dedupe_targets(targets))


def crn28_bjt_realization_m_values(pdk: PdkConfig) -> tuple[int, ...]:
    """Return configured CRN28 BJT M values for isolated realization calibration."""

    metadata = _mapping(getattr(pdk, "metadata", {}))
    realization = _mapping(metadata.get("pcell_realization", {}))
    cfg = _mapping(realization.get("bjt", {}))
    values: list[int] = []
    for item_obj in tuple(cfg.get("candidates", ()) or ()):
        item = _mapping(item_obj)
        sizing = _mapping(item.get("sizing_overrides", {}))
        raw_m = sizing.get("M", sizing.get("m", item.get("M", item.get("m", 1))))
        try:
            values.append(max(1, int(raw_m)))
        except (TypeError, ValueError):
            continue
    sweep = _mapping(cfg.get("calibration_sweep", {}))
    for raw_m in tuple(sweep.get("M", sweep.get("m", ())) or ()):
        try:
            values.append(max(1, int(raw_m)))
        except (TypeError, ValueError):
            continue
    return tuple(dict.fromkeys(values or [1]))


def _crn28_bjt_realization_row(pdk: PdkConfig, m_value: int) -> Mapping[str, Any]:
    metadata = _mapping(getattr(pdk, "metadata", {}))
    realization = _mapping(metadata.get("pcell_realization", {}))
    cfg = _mapping(realization.get("bjt", {}))
    target_m = max(1, int(m_value))
    for index, item_obj in enumerate(tuple(cfg.get("candidates", ()) or ())):
        item = _mapping(item_obj)
        sizing = _mapping(item.get("sizing_overrides", {}))
        raw_m = sizing.get("M", sizing.get("m", item.get("M", item.get("m", 1))))
        try:
            candidate_m = max(1, int(raw_m))
        except (TypeError, ValueError):
            candidate_m = 1
        if candidate_m != target_m:
            continue
        return {
            "name": str(item.get("name", f"bjt_M{target_m}_{index}")),
            "sizing_overrides": {"M": target_m},
            "layout_width_um": item.get("layout_width_um"),
            "layout_height_um": item.get("layout_height_um"),
            "cost": item.get("cost", index),
            "notes": item.get("notes", ""),
            "source": "pdk_metadata_candidate",
        }
    return {
        "name": f"bjt_sweep_M{target_m}",
        "sizing_overrides": {"M": target_m},
        "cost": max(0, target_m - 1),
        "notes": "BJT M sweep target generated from metadata.pcell_realization.bjt.calibration_sweep.",
        "source": "pdk_metadata_calibration_sweep",
    }


def _with_realization_metadata(target: PCellCalibreTarget, row: Mapping[str, Any]) -> PCellCalibreTarget:
    if not row:
        return target
    data = dict(row)
    return replace(
        target,
        metadata={
            **target.metadata,
            "realization_candidate": {
                key: value
                for key, value in data.items()
                if key
                in {
                    "name",
                    "sizing_overrides",
                    "pcell_params",
                    "layout_width_um",
                    "layout_height_um",
                    "cost",
                    "notes",
                    "source",
                    "width_m",
                    "length_m",
                    "resistance_ohm",
                    "capacitance_f",
                }
                and value is not None
            },
        },
    )


def _crn28_passive_realization_sweep_rows(pdk: PdkConfig, logical: str) -> tuple[Mapping[str, Any], ...]:
    metadata = _mapping(getattr(pdk, "metadata", {}))
    realization = _mapping(metadata.get("pcell_realization", {}))
    cfg = _mapping(realization.get(str(logical), {}))
    if not cfg:
        cfg = _mapping(_mapping(realization.get("passives", {})).get(str(logical), {}))
    rows: list[dict[str, Any]] = []
    for index, item_obj in enumerate(tuple(cfg.get("candidates", ()) or ())):
        item = _mapping(item_obj)
        pcell_params = dict(_mapping(item.get("pcell_params", item.get("pcell_overrides", item.get("params", {})))))
        sizing_overrides = _mapping(item.get("sizing_overrides", {}))
        merged_params = {**pcell_params, **dict(sizing_overrides)}
        if str(logical) == "resistor":
            width_m = _float_first(merged_params, ("W", "w", "width", "sumW"), 0.5e-6)
            length_m = _float_first(merged_params, ("L", "l", "length", "sumL"), 10e-6)
            resistance = _float_first(merged_params, ("R", "r", "res", "resistance"), 1000.0)
            pcell_params = {**{"R": resistance, "W": width_m, "L": length_m}, **pcell_params}
            rows.append(
                {
                    "name": str(item.get("name", f"candidate_{index}")),
                    "width_m": width_m,
                    "length_m": length_m,
                    "resistance_ohm": resistance,
                    "pcell_params": pcell_params,
                    "sizing_overrides": dict(sizing_overrides),
                    "layout_width_um": item.get("layout_width_um"),
                    "layout_height_um": item.get("layout_height_um"),
                    "cost": item.get("cost", index),
                    "notes": item.get("notes", ""),
                    "source": "pdk_metadata_candidate",
                }
            )
        elif str(logical) == "capacitor":
            capacitance = _float_first(merged_params, ("C", "c", "capacitance"), 1e-15)
            width_m = _float_first(merged_params, ("W", "w", "wr", "width"), 1e-6)
            length_m = _float_first(merged_params, ("L", "l", "lr", "length"), 1e-6)
            pcell_params = {**{"C": capacitance, "W": width_m, "L": length_m}, **pcell_params}
            rows.append(
                {
                    "name": str(item.get("name", f"candidate_{index}")),
                    "width_m": width_m,
                    "length_m": length_m,
                    "capacitance_f": capacitance,
                    "pcell_params": pcell_params,
                    "sizing_overrides": dict(sizing_overrides),
                    "layout_width_um": item.get("layout_width_um"),
                    "layout_height_um": item.get("layout_height_um"),
                    "cost": item.get("cost", index),
                    "notes": item.get("notes", ""),
                    "source": "pdk_metadata_candidate",
                }
            )
    sweep = _mapping(cfg.get("calibration_sweep", {}))
    if str(logical) == "resistor":
        widths_um = tuple(float(item) for item in tuple(sweep.get("W_um", ()) or ()) if float(item) > 0.0)
        lengths_um = tuple(float(item) for item in tuple(sweep.get("L_um", ()) or ()) if float(item) > 0.0)
        resistances = tuple(float(item) for item in tuple(sweep.get("R_ohm", ()) or ()) if float(item) > 0.0)
        for width_um in widths_um:
            for length_um in lengths_um or (10.0,):
                for resistance in resistances or (1000.0,):
                    width_m = width_um * 1e-6
                    length_m = length_um * 1e-6
                    rows.append(
                        {
                            "name": f"sweep_R{_safe_token(f'{resistance:g}')}_W{_safe_token(f'{width_um:g}um')}_L{_safe_token(f'{length_um:g}um')}",
                            "width_m": width_m,
                            "length_m": length_m,
                            "resistance_ohm": resistance,
                            "pcell_params": {"R": resistance, "W": width_m, "L": length_m},
                            "sizing_overrides": {"R": resistance, "W": width_m, "L": length_m},
                            "cost": 100 + len(rows),
                            "notes": "Resistor realization target generated from metadata.pcell_realization.resistor.calibration_sweep.",
                            "source": "pdk_metadata_calibration_sweep",
                        }
                    )
    elif str(logical) == "capacitor":
        caps = tuple(float(item) for item in tuple(sweep.get("C_f", ()) or ()) if float(item) > 0.0)
        widths_um = tuple(float(item) for item in tuple(sweep.get("W_um", sweep.get("wr_um", ())) or ()) if float(item) > 0.0)
        lengths_um = tuple(float(item) for item in tuple(sweep.get("L_um", sweep.get("lr_um", ())) or ()) if float(item) > 0.0)
        for width_um in widths_um or (1.0,):
            for length_um in lengths_um or (1.0,):
                for capacitance in caps or (1e-15,):
                    width_m = width_um * 1e-6
                    length_m = length_um * 1e-6
                    rows.append(
                        {
                            "name": f"sweep_C{_safe_token(f'{capacitance:g}')}_W{_safe_token(f'{width_um:g}um')}_L{_safe_token(f'{length_um:g}um')}",
                            "width_m": width_m,
                            "length_m": length_m,
                            "capacitance_f": capacitance,
                            "pcell_params": {"C": capacitance, "W": width_m, "L": length_m},
                            "sizing_overrides": {"C": capacitance, "W": width_m, "L": length_m},
                            "cost": 100 + len(rows),
                            "notes": "Capacitor realization target generated from metadata.pcell_realization.capacitor.calibration_sweep.",
                            "source": "pdk_metadata_calibration_sweep",
                        }
                    )
    deduped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = _passive_realization_physical_key(str(logical), row)
        deduped.setdefault(key, row)
    return tuple(deduped.values())


def _passive_realization_physical_key(logical: str, row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    """Return a representation-independent key for passive geometry sweeps.

    Metadata candidates carry true CDF params such as ``w="2u"``/``l="5u"``,
    while calibration sweep rows use numeric logical dimensions such as
    ``W=2e-6``/``L=5e-6``.  They are the same physical candidate and must not
    both enter the calibration matrix.
    """

    return (
        str(logical),
        _calibre_sweep_dedupe_token(row.get("width_m")),
        _calibre_sweep_dedupe_token(row.get("length_m")),
        _calibre_sweep_dedupe_token(row.get("resistance_ohm")),
        _calibre_sweep_dedupe_token(row.get("capacitance_f")),
    )


def _calibre_sweep_dedupe_token(value: Any) -> str:
    if isinstance(value, bool):
        return repr(value)
    if isinstance(value, (float, int)):
        return f"{float(value):.12g}"
    return repr(value)


def build_crn28_pcell_lvs_calibration_matrix(
    pdk: PdkConfig,
    *,
    logical_names: Sequence[str] = ("nmos", "pmos", "bjt", "resistor", "capacitor"),
    mos_widths_nm_by_logical: Mapping[str, Sequence[int]] | None = None,
    mos_lengths_nm_by_logical: Mapping[str, Sequence[int]] | None = None,
    bjt_m_values: Sequence[int] | None = None,
    bjt_source_models: Sequence[str] = ("npn5", "npn", "npn_i_mac"),
    resistor_source_models: Sequence[str] = ("rnodl",),
    capacitor_source_models: Sequence[str] = ("nmoscap",),
    include_mos_source_modes: Sequence[str] = ("macro", "finger"),
    mos_access_style: str = "crn28_multifinger_strap",
) -> tuple[PCellCalibreTarget, ...]:
    """Return the standard isolated LVS calibration matrix for CRN28.

    This matrix is intentionally block-independent.  A Bandgap/LDO LVS failure
    should first be reproduced against these isolated PCells before we classify
    it as a block-level routing/connectivity issue.
    """

    base_targets = build_crn28_pcell_calibre_targets(
        pdk,
        logical_names=logical_names,
        mos_widths_nm_by_logical=mos_widths_nm_by_logical,
        mos_lengths_nm_by_logical=mos_lengths_nm_by_logical,
        bjt_m_values=bjt_m_values,
        bjt_source_models=bjt_source_models,
        resistor_source_models=resistor_source_models,
        capacitor_source_models=capacitor_source_models,
    )
    targets: list[PCellCalibreTarget] = []
    source_modes = tuple(dict.fromkeys(str(item) for item in include_mos_source_modes if str(item)))
    for target in base_targets:
        if target.logical_name in {"nmos", "pmos"}:
            for source_mode in source_modes or ("macro",):
                targets.append(
                    PCellCalibreTarget.from_dict(
                        {
                            **target.to_dict(),
                            "name": f"{target.name}_source_{_safe_token(source_mode)}",
                            "metadata": {
                                **target.metadata,
                                "access_style": mos_access_style,
                                "source_mode": source_mode,
                                "matrix": "crn28_pcell_lvs_calibration",
                            },
                        }
                    )
                )
            continue
        targets.append(
            PCellCalibreTarget.from_dict(
                {
                    **target.to_dict(),
                    "metadata": {
                        **target.metadata,
                        "matrix": "crn28_pcell_lvs_calibration",
                    },
                }
            )
        )
    return tuple(_dedupe_targets(targets))


def build_crn28_mos_calibre_target(
    pdk: PdkConfig,
    logical_name: str,
    *,
    width_nm: int,
    length_nm: int = 180,
    nf: int | None = None,
    m: int = 1,
) -> PCellCalibreTarget:
    if logical_name not in {"nmos", "pmos"}:
        raise ValueError("logical_name must be 'nmos' or 'pmos'")
    if width_nm <= 0 or length_nm <= 0:
        raise ValueError("width_nm and length_nm must be positive")
    template = pdk.pcell_template_for(logical_name)
    chosen_nf = int(nf) if nf is not None else choose_crn28_mos_nf(pdk, logical_name, width_nm=width_nm)
    chosen_nf = max(1, chosen_nf)
    chosen_m = max(1, int(m))
    per_finger_nm = float(width_nm) / float(chosen_nf * chosen_m)
    pcell_overrides = _crn28_mos_pcell_overrides(pdk, logical_name)
    params: dict[str, Any] = {
        "Wfg": per_finger_nm * 1e-9,
        "fingers": chosen_nf,
        "l": length_nm * 1e-9,
        "simM": chosen_m,
    }
    params.update(pcell_overrides)
    name = f"{logical_name}_w{int(width_nm)}_l{int(length_nm)}_nf{chosen_nf}_m{chosen_m}"
    return PCellCalibreTarget(
        name=name,
        logical_name=logical_name,
        lib_name=template.resolved_layout_lib_name(),
        cell_name=template.resolved_layout_cell_name(),
        view_name=template.resolved_layout_view_name(),
        params=params,
        terminals=("D", "G", "S", "B"),
        source_model=template.resolved_layout_cell_name(),
        source_params={"W": width_nm * 1e-9, "L": length_nm * 1e-9, "nf": chosen_nf, "M": chosen_m},
        instantiation_method=template.resolved_layout_instantiation_method(),
        metadata={
            "width_nm": int(width_nm),
            "length_nm": int(length_nm),
            "per_finger_width_nm": per_finger_nm,
            "rule_source": "metadata.pcell_drc_sweep.strongarm_mos.mos_finger_constraints",
        },
    )


def build_crn28_bjt_calibre_target(
    pdk: PdkConfig,
    *,
    source_model: str = "npn5",
    m: int = 1,
) -> PCellCalibreTarget:
    template = pdk.pcell_template_for("bjt")
    clean_model = str(source_model or template.resolved_layout_cell_name())
    return PCellCalibreTarget(
        name=f"bjt_{template.resolved_layout_cell_name()}_source_{_safe_token(clean_model)}_m{int(m)}",
        logical_name="bjt",
        lib_name=template.resolved_layout_lib_name(),
        cell_name=template.resolved_layout_cell_name(),
        view_name=template.resolved_layout_view_name(),
        params=_crn28_bjt_pcell_params(clean_model, m=max(1, int(m))),
        terminals=("C", "B", "E"),
        source_model=clean_model,
        source_params={"M": max(1, int(m))},
        instantiation_method="dbCreateParamInst",
        metadata={
            "layout_cell": template.resolved_layout_cell_name(),
            "source_model_candidate": clean_model,
            "expected_bjt_area_um2": _bjt_emitter_area_um2(clean_model),
            "port_style": "direct_label",
            "pin_only_bbox": "port_pad",
            "terminal_xy_overrides": {
                "E": {
                    "xy": [2.63, 2.63],
                    "layer": "M1",
                    "contact_layer": "CO",
                    "source": "calibre_clean_crn28_terminal_xy_probe_0720",
                    "confidence": 1.0,
                }
            },
        },
    )


def build_crn28_resistor_calibre_target(
    pdk: PdkConfig,
    *,
    source_model: str = "rnodl",
    width_m: float = 2e-6,
    length_m: float = 10e-6,
    resistance_ohm: float = 1000.0,
    m: int = 1,
    pcell_params: Mapping[str, Any] | None = None,
    name_suffix: str = "",
) -> PCellCalibreTarget:
    """Build an isolated CRN28 resistor PCell LVS target."""

    template = pdk.pcell_template_for("resistor")
    clean_model = str(source_model or template.resolved_layout_cell_name())
    suffix = f"_{_safe_token(name_suffix)}" if str(name_suffix or "") else ""
    layout_params = _crn28_resistor_pcell_params(
        clean_model,
        width_m=width_m,
        length_m=length_m,
        resistance_ohm=resistance_ohm,
        m=m,
        overrides=pcell_params,
    )
    return PCellCalibreTarget(
        name=f"resistor_{template.resolved_layout_cell_name()}_source_{_safe_token(clean_model)}{suffix}",
        logical_name="resistor",
        lib_name=template.resolved_layout_lib_name(),
        cell_name=template.resolved_layout_cell_name(),
        view_name=template.resolved_layout_view_name(),
        params={**dict(template.default_params), **layout_params},
        terminals=("PLUS", "MINUS"),
        source_model=clean_model,
        source_params={"R": float(resistance_ohm), "w": float(width_m), "l": float(length_m), "M": int(m)},
        instantiation_method="dbCreateParamInst",
        metadata={
            "layout_cell": template.resolved_layout_cell_name(),
            "source_model_candidate": clean_model,
            "source_kind": "subckt_resistor",
            "port_style": "direct_label",
            "pin_only_bbox": "port_pad",
        },
    )


def build_crn28_capacitor_calibre_target(
    pdk: PdkConfig,
    *,
    source_model: str = "nmoscap",
    width_m: float = 1e-6,
    length_m: float = 1e-6,
    capacitance_f: float = 1e-15,
    m: int = 1,
    pcell_params: Mapping[str, Any] | None = None,
    name_suffix: str = "",
) -> PCellCalibreTarget:
    """Build an isolated CRN28 MOS-cap PCell LVS target."""

    template = pdk.pcell_template_for("capacitor")
    clean_model = str(source_model or template.resolved_layout_cell_name())
    suffix = f"_{_safe_token(name_suffix)}" if str(name_suffix or "") else ""
    layout_params = _crn28_capacitor_pcell_params(
        clean_model,
        width_m=width_m,
        length_m=length_m,
        capacitance_f=capacitance_f,
        m=m,
        overrides=pcell_params,
    )
    return PCellCalibreTarget(
        name=f"capacitor_{template.resolved_layout_cell_name()}_source_{_safe_token(clean_model)}{suffix}",
        logical_name="capacitor",
        lib_name=template.resolved_layout_lib_name(),
        cell_name=template.resolved_layout_cell_name(),
        view_name=template.resolved_layout_view_name(),
        params={**dict(template.default_params), **layout_params},
        terminals=("PLUS", "MINUS"),
        source_model=clean_model,
        source_params={"C": float(capacitance_f), "wr": float(width_m), "lr": float(length_m), "M": int(m)},
        instantiation_method="dbCreateParamInst",
        metadata={
            "layout_cell": template.resolved_layout_cell_name(),
            "source_model_candidate": clean_model,
            "source_kind": "subckt_capacitor",
            "port_style": "direct_label",
            "pin_only_bbox": "port_pad",
        },
    )


def build_crn28_passive_array_calibre_targets(
    pdk: PdkConfig,
    *,
    logical_names: Sequence[str] = ("resistor", "capacitor"),
    unit_counts: Sequence[int] = (2, 4),
    max_targets_per_logical: int | None = None,
) -> tuple[PCellCalibreTarget, ...]:
    """Build isolated CRN28 R/C unit-array Calibre targets.

    These targets verify the derived ``passive_unit_array`` realizations from
    :mod:`analogskills.pcell.unit_library`.  Layout contains multiple clean native
    primitive PCells; source contains the matching number of primitive subckt
    instances.  A clean result can later be promoted from
    ``primitive_clean_array`` to an explicitly Calibre-clean array candidate.
    """

    from analogskills.pcell.unit_library import build_pcell_unit_library

    library = build_pcell_unit_library(
        pdk,
        logical_names=tuple(str(item) for item in logical_names),
        clean_only=True,
        include_bjt_arrays=False,
        include_passive_arrays=True,
    )
    requested_counts = {max(1, int(item)) for item in unit_counts}
    targets: list[PCellCalibreTarget] = []
    for logical in tuple(str(item).lower() for item in logical_names):
        if logical not in {"resistor", "capacitor"}:
            continue
        template = pdk.pcell_template_for(logical)
        logical_targets: list[PCellCalibreTarget] = []
        for candidate in library.candidates_for(logical):
            if str(getattr(candidate, "realization_kind", "")) != f"{logical}_unit_array":
                continue
            spec = _mapping(candidate.sizing_overrides.get("passive_unit_array", {}))
            unit_count = max(1, int(float(spec.get("unit_count", 1) or 1)))
            if unit_count not in requested_counts:
                continue
            source_params = _passive_array_unit_source_params(candidate.to_dict())
            params = dict(candidate.pcell_params)
            model = str(params.get("model", template.resolved_layout_cell_name()))
            target = PCellCalibreTarget(
                name=str(candidate.name),
                logical_name=logical,
                lib_name=template.resolved_layout_lib_name(),
                cell_name=template.resolved_layout_cell_name(),
                view_name=template.resolved_layout_view_name(),
                params=params,
                terminals=tuple(candidate.terminals or ("PLUS", "MINUS")),
                source_model=model,
                source_params=source_params,
                instantiation_method=template.resolved_layout_instantiation_method(),
                metadata={
                    "layout_cell": template.resolved_layout_cell_name(),
                    "source_model_candidate": model,
                    "source_kind": f"{logical}_primitive_array",
                    "port_style": "direct_label",
                    "pin_only_bbox": "port_pad",
                    "passive_unit_array": dict(spec),
                    "array_candidate_name": str(candidate.name),
                    "array_calibration": True,
                    "requires_schematic_expansion": True,
                },
            )
            logical_targets.append(target)
        targets.extend(logical_targets[:max_targets_per_logical] if max_targets_per_logical is not None else logical_targets)
    return tuple(_dedupe_targets(targets))


def _crn28_bjt_pcell_params(source_model: str, *, m: int = 1) -> dict[str, Any]:
    clean_model = str(source_model or "npn5")
    params: dict[str, Any] = {"model": clean_model, "m": max(1, int(m))}
    area_um2 = _bjt_emitter_area_um2(clean_model)
    if area_um2 is not None:
        side_um = sqrt(area_um2)
        params["area"] = f"{area_um2 * 1e-12:.12g}"
        params["l"] = _format_spice_dimension(side_um * 1e-6)
        params["w"] = _format_spice_dimension(side_um * 1e-6)
        params["Esize"] = _crn28_bjt_esize_from_area(area_um2)
    return params


def _crn28_bjt_esize_from_area(area_um2: float) -> str:
    if abs(area_um2 - 100.0) < 1e-9:
        return "10x10"
    if abs(area_um2 - 25.0) < 1e-9:
        return "5x5"
    if abs(area_um2 - 4.0) < 1e-9:
        return "2x2"
    if abs(area_um2 - 2.56) < 1e-9:
        return "1d6x1d6"
    side = sqrt(max(float(area_um2), 0.0))
    return f"{side:g}x{side:g}"


def _crn28_resistor_pcell_params(
    source_model: str,
    *,
    width_m: float,
    length_m: float,
    resistance_ohm: float,
    m: int,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    width = _float_first(_mapping(overrides), ("w", "W", "width", "sumW"), float(width_m))
    length = _float_first(_mapping(overrides), ("l", "L", "length", "sumL"), float(length_m))
    resistance = _float_first(_mapping(overrides), ("res", "R", "r", "resistance"), float(resistance_ohm))
    mult = max(1, int(float(_mapping(overrides).get("m", _mapping(overrides).get("M", m)) or m)))
    params: dict[str, Any] = {
        "model": str(source_model or "rnodl"),
        "w": _format_spice_dimension(width),
        "sumW": _format_spice_dimension(width),
        "l": _format_spice_dimension(length),
        "sumL": _format_spice_dimension(length),
    }
    if mult > 1:
        params["m"] = mult
    params.update(_normalize_crn28_passive_pcell_overrides(overrides, logical="resistor"))
    return params


def _crn28_capacitor_pcell_params(
    source_model: str,
    *,
    width_m: float,
    length_m: float,
    capacitance_f: float,
    m: int,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    width = _float_first(_mapping(overrides), ("wr", "w", "W", "width"), float(width_m))
    length = _float_first(_mapping(overrides), ("lr", "l", "L", "length"), float(length_m))
    capacitance = _float_first(_mapping(overrides), ("c", "C", "capacitance"), float(capacitance_f))
    mult = max(1, int(float(_mapping(overrides).get("m", _mapping(overrides).get("M", m)) or m)))
    params: dict[str, Any] = {
        "model": str(source_model or "nmoscap"),
        "wr": _format_spice_dimension(width),
        "lr": _format_spice_dimension(length),
        "c": _format_spice_capacitance(capacitance),
        "m": mult,
        "multi": mult,
    }
    params.update(_normalize_crn28_passive_pcell_overrides(overrides, logical="capacitor"))
    return params


def _passive_array_unit_source_params(candidate: Mapping[str, Any]) -> dict[str, Any]:
    logical = str(candidate.get("logical_name", "") or "").lower()
    sizing = dict(_mapping(candidate.get("sizing_overrides", {})))
    pcell_params = dict(_mapping(candidate.get("pcell_params", {})))
    for key in ("M", "m", "multi", "passive_unit_array", "layout_width_um", "layout_height_um"):
        sizing.pop(key, None)
    if logical == "resistor":
        return {
            "R": _float_first(sizing, ("R", "r", "res", "resistance"), 1000.0),
            "W": _float_first({**pcell_params, **sizing}, ("W", "w", "width", "sumW"), 2e-6),
            "L": _float_first({**pcell_params, **sizing}, ("L", "l", "length", "sumL"), 10e-6),
        }
    if logical == "capacitor":
        return {
            "C": _float_first(sizing, ("C", "c", "capacitance"), 1e-15),
            "W": _float_first({**pcell_params, **sizing}, ("W", "w", "wr", "width"), 1e-6),
            "L": _float_first({**pcell_params, **sizing}, ("L", "l", "lr", "length"), 1e-6),
        }
    return sizing


def _normalize_crn28_passive_pcell_overrides(overrides: Mapping[str, Any] | None, *, logical: str) -> dict[str, Any]:
    raw = dict(_mapping(overrides))
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        key_s = str(key)
        if logical == "resistor":
            if key_s in {"W", "width"}:
                width = _parse_spice_number(value, default=0.0)
                normalized["w"] = _format_spice_dimension(width)
                normalized["sumW"] = _format_spice_dimension(width)
                continue
            if key_s in {"L", "length"}:
                length = _parse_spice_number(value, default=0.0)
                normalized["l"] = _format_spice_dimension(length)
                normalized["sumL"] = _format_spice_dimension(length)
                continue
            if key_s in {"R", "r", "resistance"}:
                # ``res`` changes CRN28 rnod callback behavior and can make
                # Calibre stop extracting the resistor in isolated probes.  The
                # stable geometry calibration axis is w/l; keep electrical R in
                # sizing_overrides/source metadata, not in PCell creation.
                continue
            if key_s in {"M"}:
                normalized["m"] = max(1, int(float(value)))
                normalized["multi"] = max(1, int(float(value)))
                continue
        elif logical == "capacitor":
            if key_s in {"W", "w", "width"}:
                normalized["wr"] = _format_spice_dimension(_parse_spice_number(value, default=0.0))
                continue
            if key_s in {"L", "l", "length"}:
                normalized["lr"] = _format_spice_dimension(_parse_spice_number(value, default=0.0))
                continue
            if key_s in {"C", "capacitance"}:
                normalized["c"] = _format_spice_capacitance(_parse_spice_number(value, default=0.0))
                continue
            if key_s in {"M"}:
                normalized["m"] = max(1, int(float(value)))
                normalized["multi"] = max(1, int(float(value)))
                continue
        normalized[key_s] = value
    return normalized


def choose_crn28_mos_nf(pdk: PdkConfig, logical_name: str, *, width_nm: int) -> int:
    rules = _crn28_mos_finger_rules(pdk)
    max_by_logical = _mapping(rules.get("max_finger_width_nm_by_logical", {}))
    max_finger_nm = float(max_by_logical.get(logical_name, max_by_logical.get("default", 2900)) or 2900)
    max_nf = max(1, int(rules.get("max_nf", 128) or 128))
    prefer_even = bool(rules.get("prefer_even_nf", True))
    nf = max(1, int(ceil(float(width_nm) / max(max_finger_nm, 1.0))))
    if prefer_even and nf > 1 and nf % 2:
        nf += 1
    return max(1, min(max_nf, nf))


def write_pcell_calibre_probe_artifacts(
    output_dir: str | Path,
    *,
    pdk: PdkConfig,
    library: str,
    cell_prefix: str,
    targets: Sequence[PCellCalibreTarget],
    base_drc_deck: str | Path | None = None,
    base_lvs_deck: str | Path | None = None,
    calibration_cache: PCellCalibrationCache | None = None,
) -> tuple[PCellCalibreArtifacts, ...]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    artifacts: list[PCellCalibreArtifacts] = []
    for target in targets:
        cell = f"{cell_prefix}_{_safe_token(target.name)}"
        plan = build_pcell_calibre_probe_plan(pdk, library=library, cell=cell, target=target, calibration_cache=calibration_cache)
        layout_json = output / f"{cell}_layout.json"
        layout_skill = output / f"{cell}_layout.il"
        source_netlist = output / f"{cell}_source.cdl"
        native_gds = output / f"{cell}_native.gds"
        drc_deck = output / f"{cell}_drc.calibre"
        lvs_deck = output / f"{cell}_lvs.calibre"
        drc_report = output / f"{cell}_drc.rep"
        lvs_report = output / f"{cell}_lvs.rep"
        streamout_log = output / cell / f"{cell}.strmout.log"
        drc_log = output / f"{cell}.drc.log"
        lvs_log = output / f"{cell}.lvs.log"

        save_oa_plan_json(plan, layout_json)
        write_oa_skill(
            plan,
            layout_skill,
            grid=pdk,
            top_level_nets=target.terminals,
            pin_net_aliases={term: term for term in target.terminals},
            replace_cellview=True,
            allow_label_only_top_level_nets=True,
        )
        write_pcell_probe_source_netlist(source_netlist, cell=cell, target=target)
        if base_drc_deck is not None and Path(base_drc_deck).exists():
            write_pcell_probe_drc_deck(Path(base_drc_deck), drc_deck, gds=native_gds, cell=cell, report=drc_report)
        if base_lvs_deck is not None and Path(base_lvs_deck).exists():
            write_pcell_probe_lvs_deck(Path(base_lvs_deck), lvs_deck, gds=native_gds, source=source_netlist, cell=cell, report=lvs_report, pdk=pdk)
        artifacts.append(
            PCellCalibreArtifacts(
                target=target,
                cell=cell,
                layout_json=str(layout_json),
                layout_skill=str(layout_skill),
                source_netlist=str(source_netlist),
                native_gds=str(native_gds),
                drc_deck=str(drc_deck),
                lvs_deck=str(lvs_deck),
                drc_report=str(drc_report),
                lvs_report=str(lvs_report),
                streamout_log=str(streamout_log),
                drc_log=str(drc_log),
                lvs_log=str(lvs_log),
            )
        )
    return tuple(artifacts)


def build_pcell_calibre_probe_plan(
    pdk: PdkConfig,
    *,
    library: str,
    cell: str,
    target: PCellCalibreTarget,
    calibration_cache: PCellCalibrationCache | None = None,
) -> OaWritePlan:
    origin = (3.0, 3.0)
    inst_plan = PCellInstancePlan(
        "DUT",
        target.logical_name,
        target.lib_name,
        target.cell_name,
        target.view_name,
        params=dict(target.params),
        xy_um=origin,
        orient=target.orient,
        connections={term: term for term in target.terminals},
        instantiation_method=target.instantiation_method,
        width_um=_target_width_hint_um(target),
        height_um=_target_height_hint_um(target),
    )
    access_style = str(target.metadata.get("access_style", "generic") or "generic")
    if (
        str(getattr(pdk, "name", "")) == "crn28hpcp"
        and target.logical_name in {"nmos", "pmos"}
        and access_style == "crn28_multifinger_strap"
    ):
        return _build_crn28_mos_multifinger_strap_probe_plan(
            pdk,
            library=library,
            cell=cell,
            target=target,
            inst_plan=inst_plan,
        )
    if target.logical_name in {"resistor", "capacitor"} and _mapping(target.metadata.get("passive_unit_array", {})):
        return _build_passive_unit_array_probe_plan(
            pdk,
            library=library,
            cell=cell,
            target=target,
            calibration_cache=calibration_cache,
        )
    accessor = PCellTerminalAccessor(pdk, calibration_cache=calibration_cache, allow_nearest_calibration=True)
    rects: list[OaRect] = []
    pins: list[OaPin] = []
    labels: list[tuple[str, str, tuple[float, float]]] = []
    port_style = str(target.metadata.get("port_style", "auto") or "auto")
    if port_style == "none":
        pins_by_terminal = {}
    else:
        pins_by_terminal = {
            terminal: _apply_probe_terminal_override(pdk, target, inst_plan, terminal, accessor.get_terminal_pin(inst_plan, terminal))
            for terminal in target.terminals
        }
    for terminal in target.terminals:
        if port_style == "none":
            continue
        pin = pins_by_terminal[terminal]
        layer = pin.layer or pdk.layer_map.metals[0]
        if port_style == "label_only":
            xy = pin.xy_um
            if target.logical_name == "bjt" and pin.bbox_um is not None:
                xy = _bjt_direct_port_xy(terminal, pin.xy_um, tuple(pin.bbox_um))
            labels.append((layer, terminal, pdk.rules.snap_point_um(xy)))
            continue
        if port_style == "direct_label":
            xy = pin.xy_um
            if target.logical_name == "bjt" and pin.bbox_um is not None:
                xy = _bjt_direct_port_xy(terminal, pin.xy_um, tuple(pin.bbox_um))
            bbox_mode = str(target.metadata.get("pin_only_bbox", "small") or "small")
            bbox = _port_pad_bbox(pdk, xy, layer) if bbox_mode == "port_pad" else _small_pin_bbox(pdk, xy, layer)
            pins.append(OaPin(terminal, terminal, "inputOutput", layer, bbox, emit_draw_rect=False))
            continue
        if target.logical_name == "bjt" and calibration_cache is not None and pin.bbox_um is not None:
            xy = _bjt_direct_port_xy(terminal, pin.xy_um, tuple(pin.bbox_um))
            bbox = _small_pin_bbox(pdk, xy, layer)
            pins.append(OaPin(terminal, terminal, "inputOutput", layer, bbox, emit_draw_rect=False))
            continue
        port_xy = _external_port_xy(target.logical_name, terminal, pin.xy_um, pins_by_terminal)
        pad_bbox = _port_pad_bbox(pdk, port_xy, layer)
        rects.append(OaRect(layer, "drawing", pad_bbox, terminal, metadata={"kind": "pcell_calibre_probe_external_port_pad", "terminal": terminal}))
        bridge = _bridge_rect(pdk, layer, pin.xy_um, port_xy)
        if bridge is not None:
            rects.append(OaRect(layer, "drawing", bridge, terminal, metadata={"kind": "pcell_calibre_probe_terminal_bridge", "terminal": terminal}))
        pins.append(OaPin(terminal, terminal, "inputOutput", layer, pad_bbox, emit_draw_rect=False))
    plan = OaWritePlan(
        OaCellView(library, cell, "layout", "maskLayout"),
        nets=target.terminals,
        pins=tuple(pins),
        instances=(
            OaInstance(
                "DUT",
                target.lib_name,
                target.cell_name,
                target.view_name,
                xy=origin,
                orient=target.orient,
                connections={term: term for term in target.terminals},
                params=dict(target.params),
                instantiation_method=target.instantiation_method,
            ),
        ),
        rects=tuple(_dedupe_rects(rects)),
        labels=tuple(labels),
    )
    return snap_oa_write_plan_to_grid(plan, _calibre_grid_nm(pdk))


def _build_passive_unit_array_probe_plan(
    pdk: PdkConfig,
    *,
    library: str,
    cell: str,
    target: PCellCalibreTarget,
    calibration_cache: PCellCalibrationCache | None = None,
) -> OaWritePlan:
    spec = _mapping(target.metadata.get("passive_unit_array", {}))
    unit_count = max(1, int(float(spec.get("unit_count", 1) or 1)))
    rows = max(1, int(float(spec.get("rows", 1) or 1)))
    cols = max(1, int(float(spec.get("cols", 1) or 1)))
    unit_width_um = max(0.1, float(spec.get("unit_width_um", _target_width_hint_um(target)) or _target_width_hint_um(target)))
    unit_height_um = max(0.1, float(spec.get("unit_height_um", _target_height_hint_um(target)) or _target_height_hint_um(target)))
    spacing_um = max(0.0, float(spec.get("spacing_um", 0.5) or 0.0), _passive_array_min_spacing_um(pdk, target.logical_name))
    pitch_x_um = max(unit_width_um + spacing_um, float(spec.get("pitch_x_um", unit_width_um + spacing_um) or (unit_width_um + spacing_um)))
    pitch_y_um = max(unit_height_um + spacing_um, float(spec.get("pitch_y_um", unit_height_um + spacing_um) or (unit_height_um + spacing_um)))
    origin = (3.0, 3.0)
    terminals = target.terminals or _default_terminals(target.logical_name)
    instances: list[OaInstance] = []
    inst_plans: list[PCellInstancePlan] = []
    connections = {term: term for term in terminals}
    for index in range(unit_count):
        row = index // cols
        col = index % cols
        if row >= rows:
            break
        xy = (origin[0] + col * pitch_x_um, origin[1] + row * pitch_y_um)
        name = f"DUT_u{index}"
        inst_plan = PCellInstancePlan(
            name,
            target.logical_name,
            target.lib_name,
            target.cell_name,
            target.view_name,
            params=dict(target.params),
            xy_um=xy,
            orient=target.orient,
            connections=connections,
            instantiation_method=target.instantiation_method,
            width_um=unit_width_um,
            height_um=unit_height_um,
        )
        inst_plans.append(inst_plan)
        instances.append(
            OaInstance(
                name,
                target.lib_name,
                target.cell_name,
                target.view_name,
                xy=xy,
                orient=target.orient,
                connections=connections,
                params=dict(target.params),
                instantiation_method=target.instantiation_method,
            )
        )
    accessor = PCellTerminalAccessor(pdk, calibration_cache=calibration_cache, allow_nearest_calibration=True)
    terminal_pins: dict[str, list[Any]] = {str(terminal): [] for terminal in terminals}
    for terminal in terminals:
        for inst_plan in inst_plans:
            pin = _apply_probe_terminal_override(
                pdk,
                target,
                inst_plan,
                terminal,
                accessor.get_terminal_pin(inst_plan, terminal),
            )
            terminal_pins[str(terminal)].append(pin)
    rects: list[OaRect] = []
    pins: list[OaPin] = []
    labels: list[tuple[str, str, tuple[float, float]]] = []
    port_style = str(target.metadata.get("port_style", "direct_label") or "direct_label")
    if port_style == "label_only":
        pin_source_by_terminal = _passive_array_pin_source_indices(target.logical_name, terminals, len(inst_plans))
        for terminal in terminals:
            terminal_key = str(terminal)
            pins_for_terminal = terminal_pins.get(terminal_key, [])
            if not pins_for_terminal:
                continue
            source_index = pin_source_by_terminal.get(terminal_key, 0)
            pin = pins_for_terminal[min(max(source_index, 0), len(pins_for_terminal) - 1)]
            layer = pin.layer or pdk.layer_map.metals[0]
            labels.append((layer, terminal_key, pdk.rules.snap_point_um(pin.xy_um)))
    else:
        routed_rects, routed_pins = _passive_unit_array_probe_routes_and_pins(
            pdk,
            target=target,
            inst_plans=inst_plans,
            terminal_pins=terminal_pins,
        )
        rects.extend(routed_rects)
        pins.extend(routed_pins)
    plan = OaWritePlan(
        OaCellView(library, cell, "layout", "maskLayout"),
        nets=terminals,
        pins=tuple(pins),
        instances=tuple(instances),
        rects=tuple(_dedupe_rects(rects)),
        labels=tuple(labels),
    )
    return snap_oa_write_plan_to_grid(plan, _calibre_grid_nm(pdk))


def build_crn28_passive_unit_array_access_plan(
    pdk: PdkConfig,
    pcell_plan: object,
    *,
    lib: str,
    cell: str,
    view: str = "layout",
    calibration_cache: PCellCalibrationCache | None = None,
) -> OaWritePlan:
    """Build real M1 scaffold for CRN28 resistor/capacitor unit arrays.

    ``generate_pcell_layout_plan`` expands a virtual passive array candidate
    into multiple clean primitive PCells.  OA instance terminal mappings are not
    enough for signoff; Calibre needs real geometry tying each repeated
    terminal to the selected net.  This helper emits the same dogleg bus
    topology that is Calibre-proven by the isolated passive-array probes.
    """

    if str(getattr(pdk, "name", "")).lower() != "crn28hpcp":
        return OaWritePlan(OaCellView(lib, cell, view, "maskLayout"))
    metadata = _mapping(getattr(pcell_plan, "metadata", {}) or {})
    groups = tuple(metadata.get("passive_unit_arrays", ()) or ())
    if not groups:
        return OaWritePlan(OaCellView(lib, cell, view, "maskLayout"))

    inst_by_name = {
        str(getattr(inst, "name", "")): inst
        for inst in tuple(getattr(pcell_plan, "instances", ()) or ())
        if str(getattr(inst, "name", ""))
    }
    accessor = PCellTerminalAccessor(pdk, calibration_cache=calibration_cache, allow_nearest_calibration=True)
    rects: list[OaRect] = []
    nets: list[str] = []
    route_option_groups: list[tuple[tuple[OaRect, ...], ...]] = []
    for raw_group in groups:
        group = _mapping(raw_group)
        logical = str(group.get("logical_name", "") or "").lower()
        if logical not in {"resistor", "capacitor"}:
            continue
        names = tuple(str(name) for name in tuple(group.get("unit_instances", ()) or ()))
        inst_plans = tuple(inst_by_name[name] for name in names if name in inst_by_name)
        if not inst_plans:
            continue
        terminals = tuple(
            dict.fromkeys(
                term
                for inst in inst_plans
                for term in dict(getattr(inst, "connections", {}) or {}).keys()
                if str(term)
            )
        )
        terminal_pins: dict[str, list[Any]] = {str(term): [] for term in terminals}
        terminal_nets: dict[str, str] = {}
        for terminal in terminals:
            terminal_key = str(terminal)
            for inst in inst_plans:
                net = str(dict(getattr(inst, "connections", {}) or {}).get(terminal_key, "") or "")
                if net and terminal_key not in terminal_nets:
                    terminal_nets[terminal_key] = net
                    nets.append(net)
                pin = accessor.get_terminal_pin(inst, terminal_key)
                terminal_pins[terminal_key].append(pin)
        target = PCellCalibreTarget(
            name=str(group.get("device", "passive_array")),
            logical_name=logical,
            lib_name="",
            cell_name="",
            terminals=terminals,
        )
        route_options = _passive_unit_array_access_route_candidates(
            pdk,
            target=target,
            inst_plans=inst_plans,
            terminal_pins=terminal_pins,
            terminal_nets=terminal_nets,
        )
        if route_options:
            route_option_groups.append(route_options)

    for routed_rects in _select_passive_unit_array_access_route_options(route_option_groups):
        rects.extend(routed_rects)

    return snap_oa_write_plan_to_grid(
        OaWritePlan(
            OaCellView(lib, cell, view, "maskLayout"),
            nets=tuple(dict.fromkeys(nets)),
            rects=tuple(_dedupe_rects(rects)),
        ),
        _calibre_grid_nm(pdk),
    )


def _passive_unit_array_probe_routes_and_pins(
    pdk: PdkConfig,
    *,
    target: PCellCalibreTarget,
    inst_plans: Sequence[PCellInstancePlan],
    terminal_pins: Mapping[str, Sequence[Any]],
    terminal_nets: Mapping[str, str] | None = None,
    emit_pins: bool = True,
    terminal_bus_sides_override: Mapping[str, str] | None = None,
    external_bus_margin_um: float | None = None,
) -> tuple[tuple[OaRect, ...], tuple[OaPin, ...]]:
    rects: list[OaRect] = []
    pins: list[OaPin] = []
    if not inst_plans:
        return (), ()
    logical = str(target.logical_name).lower()
    cfg = _passive_array_route_config(pdk, logical)
    array_bbox = _passive_instance_array_bbox(inst_plans)
    instance_bboxes = tuple(_passive_instance_bbox(inst) for inst in inst_plans)
    bus_sides = _passive_array_terminal_bus_sides(pdk, logical)
    bus_sides.update({str(term): str(side) for term, side in _mapping(terminal_bus_sides_override or {}).items() if str(side)})
    fallback_sides = _default_passive_array_terminal_bus_sides(logical)
    margin = float(cfg["external_bus_margin_um"] if external_bus_margin_um is None else external_bus_margin_um)
    configured_escape_margin = float(cfg["row_escape_margin_um"])
    for terminal in target.terminals or _default_terminals(logical):
        terminal_key = str(terminal)
        pins_for_terminal = tuple(terminal_pins.get(terminal_key, ()) or ())
        if not pins_for_terminal:
            continue
        net_name = str(_mapping(terminal_nets or {}).get(terminal_key, terminal_key) or terminal_key)
        layer = str(getattr(pins_for_terminal[0], "layer", "") or pdk.layer_map.metals[0])
        width_um = max(float(cfg["rail_width_um"]), pdk.rules.min_width_um(layer) if layer in pdk.rules.min_width_nm else 0.05)
        side = str(bus_sides.get(terminal_key, fallback_sides.get(terminal_key, "bottom")) or "bottom").lower()
        points = tuple(pdk.rules.snap_point_um(tuple(getattr(pin, "xy_um", (0.0, 0.0)))) for pin in pins_for_terminal)
        if side in {"left", "right"}:
            escape_margin = _passive_array_side_escape_margin_um(
                pdk,
                instance_bboxes=instance_bboxes,
                layer=layer,
                width_um=width_um,
                configured_margin_um=configured_escape_margin,
            )
            bus_x = array_bbox[0] - margin if side == "left" else array_bbox[2] + margin
            escape_points: list[tuple[float, float]] = []
            for index, point in enumerate(points):
                box = instance_bboxes[min(index, len(instance_bboxes) - 1)]
                escape_y = box[1] - escape_margin if side == "left" else box[3] + escape_margin
                escape_points.append((point[0], escape_y))
            y0 = min(point[1] for point in escape_points)
            y1 = max(point[1] for point in escape_points)
            rects.append(
                OaRect(
                    layer,
                    "drawing",
                    _segment_bbox(pdk, layer, (bus_x, y0), (bus_x, y1), width_um),
                    net_name,
                    metadata={"kind": "pcell_calibre_probe_passive_array_bus", "terminal": terminal_key, "side": side},
                )
            )
            for point, escape_point in zip(points, escape_points):
                rects.append(
                    OaRect(
                        layer,
                        "drawing",
                        _segment_bbox(pdk, layer, point, escape_point, width_um),
                        net_name,
                        metadata={"kind": "pcell_calibre_probe_passive_array_terminal_escape", "terminal": terminal_key},
                    )
                )
                rects.append(
                    OaRect(
                        layer,
                        "drawing",
                        _segment_bbox(pdk, layer, escape_point, (bus_x, escape_point[1]), width_um),
                        net_name,
                        metadata={"kind": "pcell_calibre_probe_passive_array_terminal_bridge", "terminal": terminal_key},
                    )
                )
            pad_xy = (bus_x, 0.5 * (y0 + y1))
        else:
            bus_y = array_bbox[1] - margin if side == "bottom" else array_bbox[3] + margin
            x0 = min(point[0] for point in points)
            x1 = max(point[0] for point in points)
            rects.append(
                OaRect(
                    layer,
                    "drawing",
                    _segment_bbox(pdk, layer, (x0, bus_y), (x1, bus_y), width_um),
                    net_name,
                    metadata={"kind": "pcell_calibre_probe_passive_array_bus", "terminal": terminal_key, "side": side},
                )
            )
            for point in points:
                rects.append(
                    OaRect(
                        layer,
                        "drawing",
                        _segment_bbox(pdk, layer, point, (point[0], bus_y), width_um),
                        net_name,
                        metadata={"kind": "pcell_calibre_probe_passive_array_terminal_bridge", "terminal": terminal_key},
                    )
                )
            pad_xy = (0.5 * (x0 + x1), bus_y)
        pad_bbox = _passive_array_port_pad_bbox(pdk, pad_xy, layer, cfg)
        rects.append(
            OaRect(
                layer,
                "drawing",
                pad_bbox,
                net_name,
                metadata={"kind": "pcell_calibre_probe_passive_array_external_port_pad", "terminal": terminal_key, "side": side},
            )
        )
        if emit_pins:
            pins.append(OaPin(terminal_key, net_name, "inputOutput", layer, pad_bbox, emit_draw_rect=False))
    return tuple(_dedupe_rects(rects)), tuple(pins)


def _choose_passive_unit_array_access_routes(
    pdk: PdkConfig,
    *,
    target: PCellCalibreTarget,
    inst_plans: Sequence[PCellInstancePlan],
    terminal_pins: Mapping[str, Sequence[Any]],
    terminal_nets: Mapping[str, str] | None = None,
    blockers: Sequence[OaRect] = (),
) -> tuple[OaRect, ...]:
    """Choose passive-array access sides without creating cross-net shorts.

    Isolated passive-array probes can use fixed side preferences.  Full layouts
    need one more routing decision: neighboring arrays may put two different
    nets into the same external channel.  Treat the configured side as a
    preference, then search a small deterministic candidate set and accept only
    geometries that are cross-net clear against existing emitted access.
    """

    options = _passive_unit_array_access_route_candidates(
        pdk,
        target=target,
        inst_plans=inst_plans,
        terminal_pins=terminal_pins,
        terminal_nets=terminal_nets,
    )
    for routed_rects in options:
        if _passive_array_rects_are_cross_net_clear(routed_rects, blockers):
            return routed_rects
    return options[0] if options else ()


def _passive_unit_array_access_route_candidates(
    pdk: PdkConfig,
    *,
    target: PCellCalibreTarget,
    inst_plans: Sequence[PCellInstancePlan],
    terminal_pins: Mapping[str, Sequence[Any]],
    terminal_nets: Mapping[str, str] | None = None,
) -> tuple[tuple[OaRect, ...], ...]:
    logical = str(target.logical_name).lower()
    terminals = tuple(str(term) for term in tuple(target.terminals or _default_terminals(logical)) if str(term))
    preferred = _passive_array_preferred_terminal_sides(pdk, logical, terminals)
    fallback: tuple[OaRect, ...] = ()
    options: list[tuple[OaRect, ...]] = []
    seen: set[tuple[tuple[str, str, tuple[float, float, float, float], str], ...]] = set()
    for sides in _passive_array_terminal_side_candidates(terminals, preferred):
        for margin in _passive_array_access_margin_candidates(pdk, logical):
            routed_rects, _ = _passive_unit_array_probe_routes_and_pins(
                pdk,
                target=target,
                inst_plans=inst_plans,
                terminal_pins=terminal_pins,
                terminal_nets=terminal_nets,
                emit_pins=False,
                terminal_bus_sides_override=sides,
                external_bus_margin_um=margin,
            )
            if not routed_rects:
                continue
            if not fallback:
                fallback = routed_rects
            if not _passive_array_rects_are_cross_net_clear(routed_rects):
                continue
            key = _passive_array_route_option_key(routed_rects)
            if key in seen:
                continue
            seen.add(key)
            options.append(routed_rects)
    if not options and fallback:
        options.append(fallback)
    return tuple(options[:32])


def _select_passive_unit_array_access_route_options(
    route_option_groups: Sequence[Sequence[Sequence[OaRect]]],
) -> tuple[tuple[OaRect, ...], ...]:
    groups = tuple(tuple(tuple(option) for option in options if option) for options in tuple(route_option_groups or ()))
    groups = tuple(options for options in groups if options)
    if not groups:
        return ()
    if len(groups) > 8:
        selected: list[tuple[OaRect, ...]] = []
        blockers: list[OaRect] = []
        for options in groups:
            chosen = next((option for option in options if _passive_array_rects_are_cross_net_clear(option, blockers)), options[0])
            selected.append(tuple(chosen))
            blockers.extend(chosen)
        return tuple(selected)

    def search(index: int, selected: tuple[tuple[OaRect, ...], ...], blockers: tuple[OaRect, ...]) -> tuple[tuple[OaRect, ...], ...] | None:
        if index >= len(groups):
            return selected
        for option in groups[index]:
            option_tuple = tuple(option)
            if not _passive_array_rects_are_cross_net_clear(option_tuple, blockers):
                continue
            found = search(index + 1, (*selected, option_tuple), (*blockers, *option_tuple))
            if found is not None:
                return found
        return None

    selected = search(0, (), ())
    if selected is not None:
        return selected
    return tuple(tuple(options[0]) for options in groups)


def _passive_array_route_option_key(
    rects: Sequence[OaRect],
) -> tuple[tuple[str, str, tuple[float, float, float, float], str], ...]:
    return tuple(
        (
            str(getattr(rect, "layer", "") or ""),
            str(getattr(rect, "net", "") or ""),
            tuple(round(float(value), 6) for value in tuple(getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0)))),
            str(_mapping(getattr(rect, "metadata", {}) or {}).get("terminal", "")),
        )
        for rect in tuple(rects or ())
    )


def _passive_array_preferred_terminal_sides(
    pdk: PdkConfig,
    logical_name: str,
    terminals: Sequence[str],
) -> dict[str, str]:
    configured = _passive_array_terminal_bus_sides(pdk, logical_name)
    fallback = _default_passive_array_terminal_bus_sides(logical_name)
    return {
        str(term): str(configured.get(str(term), fallback.get(str(term), "bottom")) or "bottom").lower()
        for term in tuple(terminals or ())
    }


def _passive_array_terminal_side_candidates(
    terminals: Sequence[str],
    preferred: Mapping[str, str],
) -> tuple[dict[str, str], ...]:
    terms = tuple(str(term) for term in tuple(terminals or ()) if str(term))
    seen: set[tuple[tuple[str, str], ...]] = set()
    candidates: list[dict[str, str]] = []

    def add(candidate: Mapping[str, str]) -> None:
        normalized = {term: str(candidate.get(term, preferred.get(term, "bottom")) or "bottom").lower() for term in terms}
        key = tuple((term, normalized[term]) for term in terms)
        if key not in seen:
            seen.add(key)
            candidates.append(normalized)

    add(preferred)
    add({term: _flip_passive_array_side(preferred.get(term, "bottom"), horizontal=True) for term in terms})
    add({term: _flip_passive_array_side(preferred.get(term, "bottom"), horizontal=False) for term in terms})
    add({term: ("bottom" if index % 2 == 0 else "top") for index, term in enumerate(terms)})
    add({term: ("top" if index % 2 == 0 else "bottom") for index, term in enumerate(terms)})
    add({term: ("left" if index % 2 == 0 else "right") for index, term in enumerate(terms)})
    add({term: ("right" if index % 2 == 0 else "left") for index, term in enumerate(terms)})
    if 0 < len(terms) <= 3:
        for sides in product(("left", "right", "bottom", "top"), repeat=len(terms)):
            add(dict(zip(terms, sides)))
    return tuple(candidates)


def _flip_passive_array_side(side: object, *, horizontal: bool) -> str:
    text = str(side or "bottom").lower()
    if horizontal:
        return {"left": "right", "right": "left"}.get(text, text)
    return {"bottom": "top", "top": "bottom"}.get(text, text)


def _passive_array_access_margin_candidates(pdk: PdkConfig, logical_name: str) -> tuple[float, ...]:
    cfg = _passive_array_route_config(pdk, logical_name)
    base = max(float(cfg["external_bus_margin_um"]), 0.0)
    rail = max(float(cfg["rail_width_um"]), 0.0)
    spacing = 0.0
    try:
        spacing = max(float(pdk.rules.min_spacing_um(pdk.layer_map.metals[0])), 0.0)
    except Exception:
        spacing = 0.0
    step = max(base, rail + spacing, 0.10)
    return tuple(dict.fromkeys(round(value, 6) for value in (base, step, 1.5 * step, 2.0 * step, 3.0 * step)))


def _passive_array_rects_are_cross_net_clear(
    rects: Sequence[OaRect],
    blockers: Sequence[OaRect] = (),
) -> bool:
    left_rects = tuple(rects or ())
    right_rects = tuple(blockers or ())
    if not left_rects:
        return True
    if right_rects:
        for left in left_rects:
            for right in right_rects:
                if _passive_array_rects_overlap_same_layer_different_net(left, right):
                    return False
        return True
    for left_index, left in enumerate(left_rects):
        for right in left_rects[left_index + 1 :]:
            if _passive_array_rects_overlap_same_layer_different_net(left, right):
                return False
    return True


def _passive_array_rects_overlap_same_layer_different_net(left: OaRect, right: OaRect) -> bool:
    if str(getattr(left, "layer", "") or "") != str(getattr(right, "layer", "") or ""):
        return False
    left_net = str(getattr(left, "net", "") or "")
    right_net = str(getattr(right, "net", "") or "")
    if not left_net or not right_net or left_net == right_net:
        return False
    lx0, ly0, lx1, ly1 = (float(value) for value in tuple(getattr(left, "bbox", (0.0, 0.0, 0.0, 0.0))))
    rx0, ry0, rx1, ry1 = (float(value) for value in tuple(getattr(right, "bbox", (0.0, 0.0, 0.0, 0.0))))
    return lx0 < rx1 and rx0 < lx1 and ly0 < ry1 and ry0 < ly1


def _passive_array_route_config(pdk: PdkConfig, logical_name: str) -> dict[str, float]:
    metadata = _metadata(pdk)
    passive = _mapping(_mapping(metadata.get("calibre", {})).get("passive_array", {}))
    return {
        "rail_width_um": _dimension_config_um(passive, "rail_width_um", "rail_width_nm", 0.12),
        "external_bus_margin_um": _dimension_config_um(passive, "external_bus_margin_um", "external_bus_margin_nm", 0.30),
        "row_escape_margin_um": _dimension_config_um(passive, "row_escape_margin_um", "row_escape_margin_nm", 0.15),
        "port_pad_width_um": _dimension_config_um(passive, "port_pad_width_um", "port_pad_width_nm", 0.24),
    }


def _passive_array_min_spacing_um(pdk: PdkConfig, logical_name: str) -> float:
    metadata = _metadata(pdk)
    passive = _mapping(_mapping(metadata.get("calibre", {})).get("passive_array", {}))
    logical = str(logical_name).lower()
    for key in (
        "minimum_access_array_spacing_um_by_logical",
        "access_array_spacing_um_by_logical",
        "minimum_array_spacing_um_by_logical",
        "spacing_um_by_logical",
        "array_spacing_um_by_logical",
    ):
        by_logical = _mapping(passive.get(key, {}))
        raw = by_logical.get(logical, by_logical.get(str(logical_name), by_logical.get("*")))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0.0:
            return value
    for key in (
        "minimum_access_array_spacing_nm_by_logical",
        "access_array_spacing_nm_by_logical",
        "minimum_array_spacing_nm_by_logical",
        "spacing_nm_by_logical",
        "array_spacing_nm_by_logical",
    ):
        by_logical = _mapping(passive.get(key, {}))
        raw = by_logical.get(logical, by_logical.get(str(logical_name), by_logical.get("*")))
        try:
            value_nm = float(raw)
        except (TypeError, ValueError):
            value_nm = 0.0
        if value_nm > 0.0:
            return value_nm * 1e-3
    for um_key, nm_key in (
        ("minimum_access_array_spacing_um", "minimum_access_array_spacing_nm"),
        ("access_array_spacing_um", "access_array_spacing_nm"),
        ("minimum_array_spacing_um", "minimum_array_spacing_nm"),
        ("spacing_um", "spacing_nm"),
        ("array_spacing_um", "array_spacing_nm"),
    ):
        try:
            value = float(passive.get(um_key, 0.0) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0.0:
            return value
        try:
            value_nm = float(passive.get(nm_key, 0.0) or 0.0)
        except (TypeError, ValueError):
            value_nm = 0.0
        if value_nm > 0.0:
            return value_nm * 1e-3
    return 0.0


def _passive_array_side_escape_margin_um(
    pdk: PdkConfig,
    *,
    instance_bboxes: Sequence[tuple[float, float, float, float]],
    layer: str,
    width_um: float,
    configured_margin_um: float,
) -> float:
    """Keep side-bus row escapes from consuming all row-to-row clearance."""

    margin = max(float(configured_margin_um), 0.0)
    row_gap = _passive_array_min_row_gap_um(instance_bboxes)
    if row_gap is None:
        return margin
    try:
        spacing = pdk.rules.min_spacing_um(layer)
    except Exception:
        spacing = 0.0
    max_margin = 0.5 * (float(row_gap) - max(float(width_um), 0.0) - max(float(spacing), 0.0))
    return min(margin, max(0.0, max_margin))


def _passive_array_min_row_gap_um(
    instance_bboxes: Sequence[tuple[float, float, float, float]]
) -> float | None:
    intervals = sorted((float(box[1]), float(box[3])) for box in instance_bboxes)
    if len(intervals) < 2:
        return None
    merged: list[tuple[float, float]] = []
    for y0, y1 in intervals:
        if not merged or y0 > merged[-1][1]:
            merged.append((y0, y1))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], y1))
    gaps = [merged[index + 1][0] - merged[index][1] for index in range(len(merged) - 1)]
    positive = [gap for gap in gaps if gap > 0.0]
    return min(positive) if positive else None


def _passive_array_terminal_bus_sides(pdk: PdkConfig, logical_name: str) -> dict[str, str]:
    metadata = _metadata(pdk)
    passive = _mapping(_mapping(metadata.get("calibre", {})).get("passive_array", {}))
    by_logical = _mapping(passive.get("terminal_bus_side", passive.get("terminal_bus_sides", {})))
    configured = _mapping(by_logical.get(str(logical_name).lower(), by_logical.get(str(logical_name), {})))
    return {str(term): str(side) for term, side in configured.items()}


def _default_passive_array_terminal_bus_sides(logical_name: str) -> dict[str, str]:
    logical = str(logical_name).lower()
    if logical == "resistor":
        return {"MINUS": "left", "PLUS": "right"}
    if logical == "capacitor":
        return {"PLUS": "left", "MINUS": "right"}
    return {}


def _passive_instance_array_bbox(inst_plans: Sequence[PCellInstancePlan]) -> tuple[float, float, float, float]:
    boxes = tuple(_passive_instance_bbox(inst) for inst in inst_plans)
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _passive_instance_bbox(inst: PCellInstancePlan) -> tuple[float, float, float, float]:
    return _oriented_bbox(
        tuple(getattr(inst, "xy_um", (0.0, 0.0)) or (0.0, 0.0)),
        (
            0.0,
            0.0,
            max(0.0, float(getattr(inst, "width_um", 0.0) or 0.0)),
            max(0.0, float(getattr(inst, "height_um", 0.0) or 0.0)),
        ),
        str(getattr(inst, "orient", "R0") or "R0"),
    )


def _segment_bbox(
    pdk: PdkConfig,
    layer: str,
    start: tuple[float, float],
    end: tuple[float, float],
    width_um: float,
) -> tuple[float, float, float, float]:
    sx, sy = start
    ex, ey = end
    half = 0.5 * max(float(width_um), pdk.rules.min_width_um(layer) if layer in pdk.rules.min_width_nm else 0.05)
    if abs(sx - ex) <= 1e-12 and abs(sy - ey) <= 1e-12:
        bbox = (sx - half, sy - half, sx + half, sy + half)
    elif abs(sx - ex) <= abs(sy - ey):
        bbox = (sx - half, min(sy, ey) - half, sx + half, max(sy, ey) + half)
    else:
        bbox = (min(sx, ex) - half, sy - half, max(sx, ex) + half, sy + half)
    return pdk.rules.snap_bbox_um(bbox, mode="outward")


def _passive_array_port_pad_bbox(
    pdk: PdkConfig,
    xy: tuple[float, float],
    layer: str,
    cfg: Mapping[str, float],
) -> tuple[float, float, float, float]:
    side = max(
        float(cfg.get("port_pad_width_um", 0.24) or 0.24),
        pdk.rules.min_width_um(layer) if layer in pdk.rules.min_width_nm else 0.05,
    )
    x, y = xy
    half = 0.5 * side
    return pdk.rules.snap_bbox_um((x - half, y - half, x + half, y + half), mode="outward")


def _dimension_config_um(config: Mapping[str, Any], um_key: str, nm_key: str, default_um: float) -> float:
    value_um = _positive_config_float(config.get(um_key))
    if value_um > 0.0:
        return value_um
    value_nm = _positive_config_float(config.get(nm_key))
    if value_nm > 0.0:
        return value_nm * 1e-3
    return float(default_um)


def _positive_config_float(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return numeric if numeric > 0.0 else 0.0


def _passive_array_pin_source_indices(logical_name: str, terminals: Sequence[str], instance_count: int) -> dict[str, int]:
    if instance_count <= 1:
        return {str(term): 0 for term in terminals}
    if str(logical_name).lower() == "resistor":
        return {"MINUS": 0, "PLUS": instance_count - 1}
    if str(logical_name).lower() == "capacitor":
        return {"PLUS": 0, "MINUS": instance_count - 1}
    return {str(term): 0 for term in terminals}


def _apply_probe_terminal_override(
    pdk: PdkConfig,
    target: PCellCalibreTarget,
    inst_plan: PCellInstancePlan,
    terminal: str,
    pin: Any,
) -> Any:
    overrides = _mapping(target.metadata.get("terminal_xy_overrides", {}))
    entry = _mapping(overrides.get(str(terminal), {}))
    if not entry:
        return pin
    xy_obj = entry.get("xy", entry.get("xy_um"))
    if not isinstance(xy_obj, (tuple, list)) or len(xy_obj) < 2:
        return pin
    try:
        local_xy = (float(xy_obj[0]), float(xy_obj[1]))
    except (TypeError, ValueError):
        return pin
    abs_xy = _oriented_xy(inst_plan.xy_um, local_xy, inst_plan.orient)
    return replace(
        pin,
        xy_um=pdk.rules.snap_point_um(abs_xy),
        layer=str(entry.get("layer", getattr(pin, "layer", pdk.layer_map.metals[0]))),
        contact_layer=str(entry.get("contact_layer", getattr(pin, "contact_layer", ""))),
        source=str(entry.get("source", "target_terminal_xy_override")),
        confidence=float(entry.get("confidence", 1.0) or 1.0),
    )


def build_crn28_mos_multifinger_access_plan(
    pdk: PdkConfig,
    pcell_plan: object,
    *,
    lib: str,
    cell: str,
    view: str = "layout",
) -> OaWritePlan:
    """Build top-level CRN28 MOS access straps for a full design layout.

    The isolated PCell Calibre smoke proved that CRN28 MOS devices need
    selective S/D M2 straps, explicit gate contacts, and a real body tap for
    Calibre to reduce native multi-finger PCells consistently.  This helper
    applies the same geometry around every MOS instance in a generated layout
    plan, using each instance's own terminal-to-net mapping.
    """

    if str(getattr(pdk, "name", "")).lower() != "crn28hpcp":
        return OaWritePlan(OaCellView(lib, cell, view, "maskLayout"))

    rects: list[OaRect] = []
    nets: list[str] = []
    for inst in tuple(getattr(pcell_plan, "instances", ()) or ()):
        logical = str(getattr(inst, "logical_name", "") or "").lower()
        if logical not in {"nmos", "pmos"}:
            continue
        connections = dict(getattr(inst, "connections", {}) or {})
        nets.extend(str(net) for net in connections.values() if str(net))
        rects.extend(_crn28_mos_multifinger_access_rects_for_instance(pdk, inst))
    rects.extend(_crn28_same_net_mos_m2_bus_gap_bridges(pdk, rects))

    plan = OaWritePlan(
        OaCellView(lib, cell, view, "maskLayout"),
        nets=tuple(dict.fromkeys(nets)),
        rects=tuple(_dedupe_rects(rects)),
    )
    return snap_oa_write_plan_to_grid(plan, _calibre_grid_nm(pdk))


def crn28_mos_lvs_sizing_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Rewrite CRN28 MOS sizing params to match calibrated Calibre extraction.

    Layout sizing uses total effective width ``W`` plus ``nf``/``m``.  The
    CRN28 native PCell extracts as a parallel reduction of per-finger devices,
    so LVS source should emit the snapped per-finger width with ``nf=1`` and a
    multiplier equal to ``nf*m``.
    """

    result = dict(params)
    if not any(key in result for key in ("W", "w", "width")):
        return result
    width = _float_first(result, ("W", "w", "width"), 1e-6)
    length = _float_first(result, ("L", "l", "length"), 0.18e-6)
    nf = max(1, int(float(result.get("nf", result.get("fingers", 1)) or 1)))
    mult = max(1, int(float(result.get("m", result.get("M", result.get("simM", 1))) or 1)))
    total_mult = max(1, nf * mult)
    finger_width = ceil(width / float(total_mult) * 1e9 - 1e-9) * 1e-9
    result["W"] = finger_width
    result["L"] = length
    result["nf"] = 1
    result["m"] = total_mult
    result["M"] = total_mult
    result.pop("fingers", None)
    result.pop("simM", None)
    return result


def _crn28_mos_multifinger_access_rects_for_instance(pdk: PdkConfig, inst: object) -> tuple[OaRect, ...]:
    params = dict(getattr(inst, "params", {}) or {})
    connections = {str(key): str(value) for key, value in dict(getattr(inst, "connections", {}) or {}).items() if str(value)}
    nf = max(1, int(float(params.get("fingers", params.get("nf", 1)) or 1)))
    sim_m = max(1, int(float(params.get("simM", params.get("m", params.get("M", 1))) or 1)))
    column_count = nf * sim_m + 1
    length_um = _float_param(params, "l", _float_param(params, "L", 0.12e-6)) * 1e6
    wfg_um = _float_param(params, "Wfg", _float_param(params, "W", 1e-6) / max(nf * sim_m, 1)) * 1e6
    pitch_um = max(0.24, length_um + 0.12)
    active_top = max(wfg_um, 0.2)
    gate_lefts = [idx * pitch_um for idx in range(nf * sim_m)]
    column_centers = [-0.06 + idx * pitch_um for idx in range(column_count)]

    m1 = pdk.layer_map.metals[0]
    m2 = pdk.layer_map.metals[min(1, len(pdk.layer_map.metals) - 1)]
    po = pdk.layer_map.gate
    via1 = pdk.layer_map.vias[0] if pdk.layer_map.vias else "VIA1"
    active = pdk.layer_map.active
    contact = pdk.layer_map.contact
    pplus = pdk.layer_map.implants.get("pplus", "PP")
    nplus = pdk.layer_map.implants.get("nplus", "NP")
    pmetal = pdk.layer_map.implants.get("pmetal", "PM")
    nwell = pdk.layer_map.wells.get("nwell", "NW")
    logical = str(getattr(inst, "logical_name", "") or "").lower()
    origin = tuple(getattr(inst, "xy_um", (0.0, 0.0)))
    orient = str(getattr(inst, "orient", "R0") or "R0")
    name = str(getattr(inst, "name", "") or "")
    calibre_rules = DesignRuleDeck(grid_nm=_calibre_grid_nm(pdk))
    access_cfg = _crn28_mos_access_config_um(pdk)

    min_x = min(column_centers)
    max_x = max(column_centers)
    s_bus_y = calibre_rules.snap_um(active_top + 0.16)
    d_bus_y = calibre_rules.snap_um(active_top + 0.54)
    gate_bus_y = calibre_rules.snap_um(float(access_cfg["gate_bus_y_offset_um"]))
    tap_cx = _crn28_mos_body_tap_x_um(calibre_rules, min_x, max_x, access_cfg)
    tap_cy = calibre_rules.snap_um(-1.18)

    rects: list[OaRect] = []

    def net(term: str) -> str:
        return str(connections.get(term, "") or "")

    def add_rect(layer: str, bbox: tuple[float, float, float, float], rect_net: str, *, purpose: str = "drawing", kind: str = "") -> None:
        if rect_net == "" and kind not in {"crn28_mos_gate_implant_cover", "crn28_mos_gate_pmetal_cover", "crn28_mos_body_nwell_cover"}:
            return
        abs_bbox = _oriented_bbox(origin, bbox, orient)
        if layer in {contact, via1}:
            abs_bbox = _exact_grid_bbox_around_center_um(pdk, abs_bbox)
        rects.append(
            OaRect(
                layer,
                purpose,
                calibre_rules.snap_bbox_um(abs_bbox, mode="outward"),
                rect_net,
                metadata={"kind": kind, "instance": name} if kind else {"instance": name},
            )
        )

    bus_x0 = min_x - 0.30
    bus_x1 = max_x + 0.30
    sd_bus_half_height = access_cfg["sd_bus_half_height_um"]
    sd_via_half = access_cfg["via1_half_um"]
    sd_via_pitch_y = access_cfg["via1_pitch_y_um"]
    sd_m1_drop_half_width = access_cfg["sd_m1_drop_half_width_um"]
    sd_terminal_y = calibre_rules.snap_um(0.5 * active_top)
    sd_terminal_overlap = float(access_cfg.get("sd_terminal_overlap_um", 0.12) or 0.12)
    sd_m1_drop_y0 = calibre_rules.snap_um(max(0.0, sd_terminal_y - sd_terminal_overlap))
    add_rect(m2, (bus_x0, s_bus_y - sd_bus_half_height, bus_x1, s_bus_y + sd_bus_half_height), net("S"), kind="crn28_mos_source_m2_bus")
    add_rect(m2, (bus_x0, d_bus_y - sd_bus_half_height, bus_x1, d_bus_y + sd_bus_half_height), net("D"), kind="crn28_mos_drain_m2_bus")
    for idx, cx in enumerate(column_centers):
        term = "S" if idx % 2 == 0 else "D"
        bus_y = s_bus_y if term == "S" else d_bus_y
        add_rect(m1, (cx - sd_m1_drop_half_width, sd_m1_drop_y0, cx + sd_m1_drop_half_width, bus_y + sd_bus_half_height), net(term), kind="crn28_mos_sd_m1_drop")
        for via_cy in (bus_y - 0.5 * sd_via_pitch_y, bus_y + 0.5 * sd_via_pitch_y):
            via_cy = calibre_rules.snap_um(via_cy)
            add_rect(via1, (cx - sd_via_half, via_cy - sd_via_half, cx + sd_via_half, via_cy + sd_via_half), net(term), kind="crn28_mos_sd_via1_drop")

    gate_x0 = min(gate_lefts) - 0.005
    gate_x1 = max(gate_lefts) + length_um + 0.005
    gate_bus_half_height = access_cfg["gate_bus_half_height_um"]
    gate_po_half_height = access_cfg["gate_po_extension_half_height_um"]
    gate_po_overlap = access_cfg["gate_po_overlap_um"]
    gate_contact_half = access_cfg["gate_contact_half_um"]
    gate_m1_landing_half = access_cfg["gate_m1_landing_half_um"]
    add_rect(m1, (gate_x0 - 0.30, gate_bus_y - gate_bus_half_height, gate_x1 + 0.30, gate_bus_y + gate_bus_half_height), net("G"), kind="crn28_mos_gate_m1_bus")
    gate_implant = nplus if logical == "nmos" else pplus
    add_rect(gate_implant, (gate_x0 - 0.070, gate_bus_y - 0.110, gate_x1 + 0.070, 0.070), "", kind="crn28_mos_gate_implant_cover")
    if logical == "pmos":
        add_rect(pmetal, (gate_x0 - 0.120, gate_bus_y - 0.140, gate_x1 + 0.120, 0.120), "", purpose="drawing1", kind="crn28_mos_gate_pmetal_cover")
    for gx in gate_lefts:
        add_rect(po, (gx, gate_bus_y - gate_po_half_height, gx + length_um, gate_po_overlap), net("G"), kind="crn28_mos_gate_po_contact_extension")
        add_rect(contact, (gx + 0.5 * length_um - gate_contact_half, gate_bus_y - gate_contact_half, gx + 0.5 * length_um + gate_contact_half, gate_bus_y + gate_contact_half), net("G"), kind="crn28_mos_gate_contact")
        add_rect(m1, (gx + 0.5 * length_um - gate_m1_landing_half, gate_bus_y - gate_m1_landing_half, gx + 0.5 * length_um + gate_m1_landing_half, gate_bus_y + gate_m1_landing_half), net("G"), kind="crn28_mos_gate_m1_landing")

    tap_active = (tap_cx - 0.12, tap_cy - 0.12, tap_cx + 0.12, tap_cy + 0.12)
    if logical == "pmos":
        add_rect(
            nwell,
            (
                min(min_x - 0.32, tap_active[0] - 0.10),
                tap_cy - 0.34,
                max(max_x + 0.32, tap_active[2] + 0.10),
                active_top + 0.30,
            ),
            "",
            kind="crn28_mos_body_nwell_cover",
        )
        implant = nplus
    else:
        implant = pplus
    add_rect(active, tap_active, net("B"), kind="crn28_mos_body_tap_active")
    add_rect(implant, (tap_active[0] - 0.07, tap_active[1] - 0.07, tap_active[2] + 0.07, tap_active[3] + 0.07), net("B"), kind="crn28_mos_body_tap_implant")
    body_m1_half = access_cfg["body_m1_half_um"]
    body_contact_half = access_cfg["body_contact_half_um"]
    add_rect(m1, (tap_cx - body_m1_half, tap_cy - body_m1_half, tap_cx + body_m1_half, tap_cy + body_m1_half), net("B"), kind="crn28_mos_body_tap_m1")
    add_rect(contact, (tap_cx - body_contact_half, tap_cy - body_contact_half, tap_cx + body_contact_half, tap_cy + body_contact_half), net("B"), kind="crn28_mos_body_tap_contact")
    return tuple(_dedupe_rects(rects))


def _oriented_bbox(origin: Sequence[float], bbox: tuple[float, float, float, float], orient: str) -> tuple[float, float, float, float]:
    points = (
        _oriented_xy(origin, (bbox[0], bbox[1]), orient),
        _oriented_xy(origin, (bbox[0], bbox[3]), orient),
        _oriented_xy(origin, (bbox[2], bbox[1]), orient),
        _oriented_xy(origin, (bbox[2], bbox[3]), orient),
    )
    xs = tuple(point[0] for point in points)
    ys = tuple(point[1] for point in points)
    return (min(xs), min(ys), max(xs), max(ys))


def _oriented_xy(origin: Sequence[float], local: tuple[float, float], orient: str) -> tuple[float, float]:
    x, y = local
    if orient == "R0":
        dx, dy = x, y
    elif orient == "R90":
        dx, dy = -y, x
    elif orient == "R180":
        dx, dy = -x, -y
    elif orient == "R270":
        dx, dy = y, -x
    elif orient == "MX":
        dx, dy = x, -y
    elif orient == "MY":
        dx, dy = -x, y
    elif orient == "MXR90":
        dx, dy = y, x
    elif orient == "MYR90":
        dx, dy = -y, -x
    else:
        dx, dy = x, y
    return (float(origin[0]) + dx, float(origin[1]) + dy)


def _build_crn28_mos_multifinger_strap_probe_plan(
    pdk: PdkConfig,
    *,
    library: str,
    cell: str,
    target: PCellCalibreTarget,
    inst_plan: PCellInstancePlan,
) -> OaWritePlan:
    """Build a Calibre probe with explicit multi-finger MOS access straps.

    The CRN28 MOS PCells extract each gate finger as a separate MN device until
    the caller supplies real top-level access: odd/even source-drain diffusion
    columns must be strapped separately, all gates must be tied, and the body
    must be connected through an external tap.  A plain M1 horizontal strap is
    not safe because it shorts adjacent diffusion columns; use M2 buses with
    selective VIA1 drops instead.
    """

    params = target.params
    nf = max(1, int(float(params.get("fingers", target.source_params.get("nf", 1)) or 1)))
    sim_m = max(1, int(float(params.get("simM", target.source_params.get("M", 1)) or 1)))
    column_count = nf * sim_m + 1
    length_um = _float_param(params, "l", _float_param(target.source_params, "L", 0.12e-6)) * 1e6
    wfg_um = _float_param(params, "Wfg", _float_param(target.source_params, "W", 1e-6) / max(nf * sim_m, 1)) * 1e6
    pitch_um = max(0.24, length_um + 0.12)
    origin_x, origin_y = inst_plan.xy_um
    active_top = origin_y + max(wfg_um, 0.2)
    gate_lefts = [origin_x + idx * pitch_um for idx in range(nf * sim_m)]
    column_centers = [origin_x - 0.06 + idx * pitch_um for idx in range(column_count)]

    m1 = pdk.layer_map.metals[0]
    m2 = pdk.layer_map.metals[min(1, len(pdk.layer_map.metals) - 1)]
    po = pdk.layer_map.gate
    via1 = pdk.layer_map.vias[0] if pdk.layer_map.vias else "VIA1"
    active = pdk.layer_map.active
    contact = pdk.layer_map.contact
    pplus = pdk.layer_map.implants.get("pplus", "PP")
    nplus = pdk.layer_map.implants.get("nplus", "NP")
    pmetal = pdk.layer_map.implants.get("pmetal", "PM")
    nwell = pdk.layer_map.wells.get("nwell", "NW")
    calibre_rules = DesignRuleDeck(grid_nm=_calibre_grid_nm(pdk))
    access_cfg = _crn28_mos_access_config_um(pdk)

    min_x = min(column_centers)
    max_x = max(column_centers)
    s_bus_y = calibre_rules.snap_um(active_top + 0.16)
    d_bus_y = calibre_rules.snap_um(active_top + 0.54)
    gate_bus_y = calibre_rules.snap_um(origin_y + float(access_cfg["gate_bus_y_offset_um"]))
    tap_cx = _crn28_mos_body_tap_x_um(calibre_rules, min_x, max_x, access_cfg)
    tap_cy = calibre_rules.snap_um(origin_y - 1.18)

    rects: list[OaRect] = []
    pins: list[OaPin] = []

    def add_rect(layer: str, bbox: tuple[float, float, float, float], net: str, *, purpose: str = "drawing", kind: str = "") -> None:
        snapped_bbox = _exact_grid_bbox_around_center_um(pdk, bbox) if layer in {contact, via1} else bbox
        rects.append(
            OaRect(
                layer,
                purpose,
                calibre_rules.snap_bbox_um(snapped_bbox, mode="outward"),
                net,
                metadata={"kind": kind} if kind else {},
            )
        )

    def add_pin(name: str, layer: str, bbox: tuple[float, float, float, float]) -> None:
        pins.append(OaPin(name, name, "inputOutput", layer, calibre_rules.snap_bbox_um(bbox, mode="outward"), emit_draw_rect=False))

    # S/D M2 buses: only selected M1 diffusion columns are connected through VIA1.
    bus_x0 = min_x - 0.30
    bus_x1 = max_x + 0.30
    sd_bus_half_height = access_cfg["sd_bus_half_height_um"]
    sd_via_half = access_cfg["via1_half_um"]
    sd_via_pitch_y = access_cfg["via1_pitch_y_um"]
    sd_m1_drop_half_width = access_cfg["sd_m1_drop_half_width_um"]
    add_rect(m2, (bus_x0, s_bus_y - sd_bus_half_height, bus_x1, s_bus_y + sd_bus_half_height), "S", kind="crn28_mos_source_m2_bus")
    add_rect(m2, (bus_x0, d_bus_y - sd_bus_half_height, bus_x1, d_bus_y + sd_bus_half_height), "D", kind="crn28_mos_drain_m2_bus")
    for idx, cx in enumerate(column_centers):
        net = "S" if idx % 2 == 0 else "D"
        bus_y = s_bus_y if net == "S" else d_bus_y
        add_rect(m1, (cx - sd_m1_drop_half_width, active_top - 0.10, cx + sd_m1_drop_half_width, bus_y + sd_bus_half_height), net, kind="crn28_mos_sd_m1_drop")
        for via_cy in (bus_y - 0.5 * sd_via_pitch_y, bus_y + 0.5 * sd_via_pitch_y):
            via_cy = calibre_rules.snap_um(via_cy)
            add_rect(via1, (cx - sd_via_half, via_cy - sd_via_half, cx + sd_via_half, via_cy + sd_via_half), net, kind="crn28_mos_sd_via1_drop")
    s_port_bbox = (bus_x0 - 0.32, s_bus_y - sd_bus_half_height, bus_x0 + 0.12, s_bus_y + sd_bus_half_height)
    d_port_bbox = (bus_x1 - 0.12, d_bus_y - sd_bus_half_height, bus_x1 + 0.32, d_bus_y + sd_bus_half_height)
    add_rect(m2, s_port_bbox, "S", kind="crn28_mos_source_port")
    add_rect(m2, d_port_bbox, "D", kind="crn28_mos_drain_port")
    add_pin("S", m2, s_port_bbox)
    add_pin("D", m2, d_port_bbox)

    # Gate bus: do not merge all PO fingers into one continuous PO polygon,
    # because that can break Calibre MOS recognition.  Extend each gate PO
    # locally outside OD, drop a legal CO/M1 contact, then short the gates on M1.
    gate_x0 = min(gate_lefts) - 0.005
    gate_x1 = max(gate_lefts) + length_um + 0.005
    gate_bus_half_height = access_cfg["gate_bus_half_height_um"]
    gate_po_half_height = access_cfg["gate_po_extension_half_height_um"]
    gate_po_overlap = access_cfg["gate_po_overlap_um"]
    gate_contact_half = access_cfg["gate_contact_half_um"]
    gate_m1_landing_half = access_cfg["gate_m1_landing_half_um"]
    add_rect(m1, (gate_x0 - 0.30, gate_bus_y - gate_bus_half_height, gate_x1 + 0.30, gate_bus_y + gate_bus_half_height), "G", kind="crn28_mos_gate_m1_bus")
    gate_implant = nplus if target.logical_name == "nmos" else pplus
    add_rect(gate_implant, (gate_x0 - 0.070, gate_bus_y - 0.110, gate_x1 + 0.070, origin_y + 0.070), "", kind="crn28_mos_gate_implant_cover")
    if target.logical_name == "pmos":
        add_rect(pmetal, (gate_x0 - 0.120, gate_bus_y - 0.140, gate_x1 + 0.120, origin_y + 0.120), "", purpose="drawing1", kind="crn28_mos_gate_pmetal_cover")
    for gx in gate_lefts:
        add_rect(po, (gx, gate_bus_y - gate_po_half_height, gx + length_um, origin_y + gate_po_overlap), "G", kind="crn28_mos_gate_po_contact_extension")
        add_rect(contact, (gx + 0.5 * length_um - gate_contact_half, gate_bus_y - gate_contact_half, gx + 0.5 * length_um + gate_contact_half, gate_bus_y + gate_contact_half), "G", kind="crn28_mos_gate_contact")
        add_rect(m1, (gx + 0.5 * length_um - gate_m1_landing_half, gate_bus_y - gate_m1_landing_half, gx + 0.5 * length_um + gate_m1_landing_half, gate_bus_y + gate_m1_landing_half), "G", kind="crn28_mos_gate_m1_landing")
    add_rect(m1, (gate_x0 - 0.42, gate_bus_y - 0.12, gate_x0 - 0.18, gate_bus_y + 0.12), "G", kind="crn28_mos_gate_port")
    add_pin("G", m1, (gate_x0 - 0.42, gate_bus_y - 0.12, gate_x0 - 0.18, gate_bus_y + 0.12))

    # Body tap.  NMOS uses a substrate tap; PMOS uses an nwell tap with an NW
    # cover intended to merge the tap into the PCell well in this isolated probe.
    tap_active = (tap_cx - 0.12, tap_cy - 0.12, tap_cx + 0.12, tap_cy + 0.12)
    if target.logical_name == "pmos":
        add_rect(
            nwell,
            (
                min(min_x - 0.32, tap_active[0] - 0.10),
                tap_cy - 0.34,
                max(max_x + 0.32, tap_active[2] + 0.10),
                active_top + 0.30,
            ),
            "",
            kind="crn28_mos_body_nwell_cover",
        )
        implant = nplus
    else:
        implant = pplus
    add_rect(active, tap_active, "B", kind="crn28_mos_body_tap_active")
    add_rect(implant, (tap_active[0] - 0.07, tap_active[1] - 0.07, tap_active[2] + 0.07, tap_active[3] + 0.07), "B", kind="crn28_mos_body_tap_implant")
    body_m1_half = access_cfg["body_m1_half_um"]
    body_contact_half = access_cfg["body_contact_half_um"]
    add_rect(m1, (tap_cx - body_m1_half, tap_cy - body_m1_half, tap_cx + body_m1_half, tap_cy + body_m1_half), "B", kind="crn28_mos_body_tap_m1")
    add_rect(contact, (tap_cx - body_contact_half, tap_cy - body_contact_half, tap_cx + body_contact_half, tap_cy + body_contact_half), "B", kind="crn28_mos_body_tap_contact")
    add_pin("B", m1, (tap_cx - body_m1_half, tap_cy - body_m1_half, tap_cx + body_m1_half, tap_cy + body_m1_half))

    plan = OaWritePlan(
        OaCellView(library, cell, "layout", "maskLayout"),
        nets=target.terminals,
        pins=tuple(pins),
        instances=(
            OaInstance(
                "DUT",
                target.lib_name,
                target.cell_name,
                target.view_name,
                xy=inst_plan.xy_um,
                orient=target.orient,
                connections={term: term for term in target.terminals},
                params=dict(target.params),
                instantiation_method=target.instantiation_method,
            ),
        ),
        rects=tuple(_dedupe_rects(rects)),
    )
    return snap_oa_write_plan_to_grid(plan, _calibre_grid_nm(pdk))


def write_pcell_probe_source_netlist(path: str | Path, *, cell: str, target: PCellCalibreTarget) -> Path:
    terminals = target.terminals or _default_terminals(target.logical_name)
    lines = [f"* PCell Calibre calibration probe {cell}", f".SUBCKT {cell} {' '.join(terminals)}"]
    model = target.source_model or target.cell_name
    if target.logical_name in {"nmos", "pmos"}:
        if str(target.metadata.get("source_mode", "macro") or "macro") == "finger":
            lines.extend(_mos_finger_source_lines(target, terminals, model))
        else:
            params = _mos_source_param_text(
                target.source_params,
                lvs_style=str(target.metadata.get("access_style", "") or ""),
                pcell_params=target.params,
            )
            lines.append(f"M_DUT {' '.join(terminals)} {model}" + (f" {params}" if params else ""))
    elif target.logical_name == "bjt":
        params = _bjt_source_param_text(target.source_params, model)
        lines.append(f"Q_DUT {' '.join(terminals)} {model}" + (f" {params}" if params else ""))
    elif target.logical_name == "resistor":
        if _mapping(target.metadata.get("passive_unit_array", {})):
            lines.extend(_passive_array_source_lines(target, terminals, model))
        else:
            params = _resistor_subckt_param_text(target.source_params)
            lines.append(f"X_DUT {' '.join(terminals)} {model}" + (f" {params}" if params else ""))
    elif target.logical_name == "capacitor":
        if _mapping(target.metadata.get("passive_unit_array", {})):
            lines.extend(_passive_array_source_lines(target, terminals, model))
        else:
            params = _capacitor_subckt_param_text(target.source_params)
            lines.append(f"X_DUT {' '.join(terminals)} {model}" + (f" {params}" if params else ""))
    else:
        raise ValueError(f"unsupported source netlist logical name {target.logical_name!r}")
    lines.append(f".ENDS {cell}")
    if target.logical_name in {"resistor", "capacitor"}:
        lines[1:1] = _passive_model_stub_lines(target.logical_name, model)
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    path_obj.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path_obj


def write_pcell_probe_drc_deck(base_deck: Path, output: Path, *, gds: Path, cell: str, report: Path) -> Path:
    text = base_deck.read_text(encoding="utf-8", errors="replace")
    for option in (
        "EFP",
        "FULL_CHIP",
        "WITH_SEALRING",
        "WITH_APRDL",
        "WITH_POLYIMIDE",
        "AP_28K_THICKNESS",
        "GUIDELINE_ESD",
        "CHECK_LOW_DENSITY",
    ):
        text = text.replace(f"#DEFINE {option}", f"//#DEFINE {option}")
    database = output.with_suffix(".db")
    text = text.replace('LAYOUT PATH "GDSFILENAME"', f'LAYOUT PATH "{gds.resolve().as_posix()}"')
    text = text.replace('LAYOUT PRIMARY "TOPCELLNAME"', f'LAYOUT PRIMARY "{cell}"')
    text = text.replace('DRC RESULTS DATABASE "DRC_RES.db"', f'DRC RESULTS DATABASE "{database.resolve().as_posix()}"')
    text = text.replace('DRC SUMMARY REPORT "DRC.rep"', f'DRC SUMMARY REPORT "{report.resolve().as_posix()}"')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return output


def write_pcell_probe_lvs_deck(
    base_deck: Path,
    output: Path,
    *,
    gds: Path,
    source: Path,
    cell: str,
    report: Path,
    pdk: PdkConfig,
) -> Path:
    text = base_deck.read_text(encoding="utf-8", errors="replace")
    erc_database = output.with_suffix(".erc.db")
    text = text.replace('LAYOUT PRIMARY "lvs_top"', f'LAYOUT PRIMARY "{cell}"')
    text = text.replace('LAYOUT PATH "lvs_top.gds"', f'LAYOUT PATH "{gds.resolve().as_posix()}"')
    text = text.replace('SOURCE PRIMARY "lvs_top"', f'SOURCE PRIMARY "{cell}"')
    text = text.replace('SOURCE PATH "lvs_top.cdl"', f'SOURCE PATH "{source.resolve().as_posix()}"')
    text = text.replace('ERC RESULTS DATABASE "calibre_erc.db" ASCII', f'ERC RESULTS DATABASE "{erc_database.resolve().as_posix()}" ASCII')
    text = text.replace('LVS REPORT "lvs.rep"', f'LVS REPORT "{report.resolve().as_posix()}"')
    text = _apply_lvs_deck_rewrites(text, pdk)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return output


def write_native_loader(output_dir: str | Path, library: str, skill_files: Sequence[Path], *, tech_lib: str = "tsmcN28") -> Path:
    output = Path(output_dir)
    lib_path = (output / "oa_lib").resolve()
    loader = output / "load_all_pcell_calibre_probes.il"
    lines = [
        f'libObj = ddGetObj("{library}")',
        f'unless(libObj libObj = ddCreateLib("{library}" "{lib_path.as_posix()}"))',
        f'when(libObj techBindTechFile(libObj "{tech_lib}"))',
    ]
    for skill in skill_files:
        lines.append(f'load("{skill.resolve().as_posix()}")')
    lines.extend(["exit()", ""])
    loader.write_text("\n".join(lines), encoding="utf-8")
    return loader


def run_pcell_calibre_artifacts(
    artifacts: Sequence[PCellCalibreArtifacts],
    *,
    root: str | Path,
    library: str,
    output_dir: str | Path,
    layer_map: str | Path,
    virtuoso: str = "./run_ic618.sh",
    strmout: str = "strmout",
    calibre: str = "calibre",
    run_load: bool = False,
    run_streamout: bool = False,
    run_drc: bool = False,
    run_lvs: bool = False,
    continue_after_load_failure: bool = False,
) -> PCellCalibreCatalog:
    root_path = Path(root)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    loader = write_native_loader(output_path, library, tuple(Path(item.layout_skill) for item in artifacts))
    execution: dict[str, Any] = {"load": None, "cells": {}}
    if run_load:
        execution["load"] = _run_command(
            [virtuoso, "-nograph", "-replay", str(loader), "-log", str(output_path / "virtuoso_native_load.log")],
            cwd=root_path,
            log_path=output_path / "native_load.stdout.log",
        )
    load_failed = _returncode(execution.get("load")) not in (None, 0)
    entries: list[PCellCalibreCatalogEntry] = []
    for item in artifacts:
        cell_dir = output_path / item.cell
        cell_dir.mkdir(parents=True, exist_ok=True)
        cell_exec: dict[str, Any] = {}
        if load_failed and not continue_after_load_failure:
            classification = {
                "status": "load_failed",
                "usable_for_layout": False,
                "load_returncode": _returncode(execution.get("load")),
            }
            entry = PCellCalibreCatalogEntry(item.target, item, classification=classification, execution=cell_exec)
            entries.append(entry)
            execution["cells"][item.cell] = entry.to_dict()
            continue
        if run_streamout:
            cell_exec["streamout"] = _run_command(
                [
                    strmout,
                    "-library",
                    library,
                    "-strmFile",
                    item.native_gds,
                    "-topCell",
                    item.cell,
                    "-view",
                    "layout",
                    "-runDir",
                    str(cell_dir),
                    "-logFile",
                    Path(item.streamout_log).name,
                    "-layerMap",
                    str(Path(layer_map).resolve()),
                    "-case",
                    "Preserve",
                    "-convertDot",
                    "node",
                    "-flattenPcells",
                ],
                cwd=root_path,
                log_path=cell_dir / "strmout.stdout.log",
            )
        if run_drc:
            cell_exec["drc"] = _run_command([calibre, "-drc", item.drc_deck], cwd=root_path, log_path=Path(item.drc_log))
        if run_lvs:
            cell_exec["lvs"] = _run_command([calibre, "-lvs", item.lvs_deck], cwd=root_path, log_path=Path(item.lvs_log))
        drc_summary = summarize_calibre_drc_report(Path(item.drc_report))
        lvs_summary = summarize_calibre_lvs_report(Path(item.lvs_report))
        classification = classify_pcell_calibre_result(
            drc_summary=drc_summary,
            lvs_summary=lvs_summary,
            streamout_returncode=_returncode(cell_exec.get("streamout")),
            drc_returncode=_returncode(cell_exec.get("drc")),
            lvs_returncode=_returncode(cell_exec.get("lvs")),
            load_log=output_path / "virtuoso_native_load.log",
            streamout_log=Path(item.streamout_log),
        )
        entry = PCellCalibreCatalogEntry(
            item.target,
            item,
            classification=classification,
            drc_summary=drc_summary,
            lvs_summary=lvs_summary,
            execution=cell_exec,
        )
        entries.append(entry)
        execution["cells"][item.cell] = entry.to_dict()
    catalog = PCellCalibreCatalog(
        pdk="",
        entries=tuple(entries),
        metadata={"loader": str(loader), "library": library, "execution": execution},
    )
    catalog.save_json(output_path / "pcell_calibre_catalog.json")
    return catalog


def summarize_calibre_drc_report(path: str | Path) -> dict[str, Any]:
    path_obj = Path(path)
    if not path_obj.exists():
        return {"exists": False, "total_results": 0, "actionable_results": 0, "rule_counts": {}, "actionable_rule_counts": {}}
    pattern = re.compile(r"^\s*RULECHECK\s+(.+?)\s+\.{2,}\s+TOTAL\s+Result\s+Count\s*=\s*(\d+)", flags=re.IGNORECASE)
    counts: dict[str, int] = {}
    for line in path_obj.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        count = int(match.group(2))
        if count:
            counts[match.group(1).strip()] = count
    actionable = {name: count for name, count in counts.items() if not _ignored_drc_rule(name)}
    return {
        "exists": True,
        "total_results": sum(counts.values()),
        "actionable_results": sum(actionable.values()),
        "rule_counts": counts,
        "actionable_rule_counts": actionable,
    }


def summarize_calibre_lvs_report(path: str | Path) -> dict[str, Any]:
    path_obj = Path(path)
    if not path_obj.exists():
        return {"exists": False, "correct": False, "incorrect": False}
    text = path_obj.read_text(encoding="utf-8", errors="replace")
    upper = text.upper()
    ports = _last_table_int_pair_for_label(text, "Ports")
    nets = _last_table_int_pair_for_label(text, "Nets")
    instances = _last_table_int_pair_for_label(text, "Instances")
    property_errors = _lvs_property_errors(text)
    issue_classes = _lvs_issue_classes(
        text,
        ports=ports,
        nets=nets,
        instances=instances,
        property_errors=property_errors,
    )
    return {
        "exists": True,
        "correct": bool(re.search(r"\bCORRECT\b", upper)) and "INCORRECT" not in upper and "NOT COMPARED" not in upper,
        "incorrect": "INCORRECT" in upper,
        "not_compared": "NOT COMPARED" in upper or "NOTCOMPARED" in upper,
        "issue_classes": issue_classes,
        "property_errors": property_errors,
        "property_error_count": len(property_errors),
        "connectivity_error": "CONNECTIVITY ERRORS" in upper,
        "port_mismatch": bool(ports and ports[0] != ports[1]),
        "net_mismatch": bool(nets and nets[0] != nets[1]),
        "instance_mismatch": bool(instances and instances[0] != instances[1]),
        "direct_connection_warnings": len(re.findall(r"Direct connection between different ports", text, flags=re.IGNORECASE)),
        "short_circuit_warnings": len(re.findall(r"Short circuit - Different names on one net", text, flags=re.IGNORECASE)),
        "bad_device_mentions": len(re.findall(r"\bBad Device\b", text, flags=re.IGNORECASE)),
        "too_many_pins_mentions": len(re.findall(r"Too many pins", text, flags=re.IGNORECASE)),
        "ports": ports,
        "nets": nets,
        "instances": instances,
        "device_count_lines": re.findall(r"DEVICE COUNT\s+(.+)", text, flags=re.IGNORECASE)[-10:],
        "source_models": tuple(dict.fromkeys(match.group(1) for match in re.finditer(r"netlist model\s+([A-Za-z_][A-Za-z0-9_.$:-]*)", text, flags=re.IGNORECASE))),
        "layout_models": tuple(dict.fromkeys(match.group(1) for match in re.finditer(r"DEVICE\s+[A-Z]\(([^)]+)\)", text, flags=re.IGNORECASE))),
    }


def classify_pcell_calibre_result(
    *,
    drc_summary: Mapping[str, Any] | None = None,
    lvs_summary: Mapping[str, Any] | None = None,
    streamout_returncode: int | None = None,
    drc_returncode: int | None = None,
    lvs_returncode: int | None = None,
    load_log: str | Path | None = None,
    streamout_log: str | Path | None = None,
) -> dict[str, Any]:
    drc = dict(drc_summary or {})
    lvs = dict(lvs_summary or {})
    pcell_eval_failed = _log_has_pcell_eval_failure(load_log) or _log_has_pcell_eval_failure(streamout_log)
    streamout_missing_topcell = _log_has_streamout_missing_topcell(streamout_log)
    drc_actionable = _summary_int(drc, "actionable_results")
    bad_device = _summary_int(lvs, "bad_device_mentions")
    too_many = _summary_int(lvs, "too_many_pins_mentions")
    lvs_correct = bool(lvs.get("correct", False))
    lvs_issue_classes = tuple(str(item) for item in tuple(lvs.get("issue_classes", ()) or ()))
    status = "not_run"
    if pcell_eval_failed:
        status = "pcell_eval_failed"
    elif streamout_missing_topcell:
        status = "streamout_missing_topcell"
    elif streamout_returncode not in (None, 0):
        status = "streamout_failed"
    elif drc_returncode not in (None, 0):
        status = "drc_failed"
    elif bad_device or too_many:
        status = "lvs_model_unrecognized"
    elif lvs_returncode not in (None, 0) or bool(lvs.get("incorrect", False)) or bool(lvs.get("not_compared", False)):
        status = "lvs_incorrect"
    elif lvs_correct:
        status = "clean" if drc_actionable == 0 else "lvs_clean_drc_dirty"
    elif drc.get("exists"):
        status = "drc_dirty" if drc_actionable else "drc_only"
    return {
        "status": status,
        "usable_for_layout": status == "clean",
        "pcell_eval_failed": pcell_eval_failed,
        "streamout_missing_topcell": streamout_missing_topcell,
        "streamout_returncode": streamout_returncode,
        "drc_returncode": drc_returncode,
        "lvs_returncode": lvs_returncode,
        "drc_actionable_results": drc_actionable,
        "lvs_correct": lvs_correct,
        "lvs_issue_classes": lvs_issue_classes,
        "lvs_property_error_count": _summary_int(lvs, "property_error_count"),
        "bad_device_mentions": bad_device,
        "too_many_pins_mentions": too_many,
    }


def summarize_pcell_calibre_catalog(catalog: PCellCalibreCatalog) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_logical: dict[str, dict[str, int]] = {}
    for entry in catalog.entries:
        status = str(entry.classification.get("status", "unknown"))
        logical = entry.target.logical_name
        by_status[status] = by_status.get(status, 0) + 1
        row = by_logical.setdefault(logical, {})
        row[status] = row.get(status, 0) + 1
    return {
        "entry_count": len(catalog.entries),
        "clean_count": len(catalog.clean_entries),
        "by_status": dict(sorted(by_status.items())),
        "by_logical_name": {key: dict(sorted(value.items())) for key, value in sorted(by_logical.items())},
    }


def _mos_source_param_text(params: Mapping[str, Any], *, lvs_style: str = "", pcell_params: Mapping[str, Any] | None = None) -> str:
    width = _float_param(params, "W", 1e-6)
    length = _float_param(params, "L", 0.18e-6)
    nf = max(1, int(params.get("nf", params.get("fingers", 1)) or 1))
    mult = max(1, int(params.get("M", params.get("m", 1)) or 1))
    if lvs_style == "crn28_multifinger_strap":
        finger_width = _float_param(pcell_params or {}, "Wfg", width / float(nf * mult))
        finger_width = ceil(finger_width * 1e9 - 1e-9) * 1e-9
        return f"W={_format_spice_dimension(finger_width)} L={_format_spice_dimension(length)} nf=1 M={nf * mult}"
    return f"W={_format_spice_dimension(width)} L={_format_spice_dimension(length)} nf={nf} M={mult}"


def _mos_finger_source_lines(target: PCellCalibreTarget, terminals: Sequence[str], model: str) -> list[str]:
    params = target.source_params
    width = _float_param(params, "W", 1e-6)
    length = _float_param(params, "L", 0.18e-6)
    nf = max(1, int(params.get("nf", params.get("fingers", 1)) or 1))
    mult = max(1, int(params.get("M", params.get("m", 1)) or 1))
    finger_width = width / float(nf * mult)
    result = []
    for index in range(nf * mult):
        result.append(
            f"M_DUT_F{index + 1} {' '.join(terminals)} {model} "
            f"W={_format_spice_dimension(finger_width)} L={_format_spice_dimension(length)}"
        )
    return result


def _bjt_source_param_text(params: Mapping[str, Any], model: str) -> str:
    mult = max(1, int(params.get("M", params.get("m", 1)) or 1))
    parts: list[str] = []
    area = _bjt_emitter_area_um2(model)
    if area is not None:
        parts.append(_format_spice_area_um2(area))
    if mult > 1:
        parts.append(f"M={mult}")
    return " ".join(parts)


def _resistor_subckt_param_text(params: Mapping[str, Any]) -> str:
    width = _float_first(params, ("w", "W", "width", "wr"), 2e-6)
    length = _float_first(params, ("l", "L", "length", "lr"), 10e-6)
    mult = max(1, int(float(params.get("M", params.get("m", 1)) or 1)))
    parts = [f"l={_format_spice_dimension(length)}", f"w={_format_spice_dimension(width)}"]
    if mult > 1:
        parts.append(f"multi={mult}")
    return " ".join(parts)


def _capacitor_subckt_param_text(params: Mapping[str, Any]) -> str:
    width = _float_first(params, ("wr", "w", "W", "width"), 1e-6)
    length = _float_first(params, ("lr", "l", "L", "length"), 1e-6)
    mult = max(1, int(float(params.get("M", params.get("m", 1)) or 1)))
    parts = [f"lr={_format_spice_dimension(length)}", f"wr={_format_spice_dimension(width)}"]
    if mult > 1:
        parts.append(f"multi={mult}")
    return " ".join(parts)


def _passive_array_source_lines(target: PCellCalibreTarget, terminals: Sequence[str], model: str) -> list[str]:
    spec = _mapping(target.metadata.get("passive_unit_array", {}))
    unit_count = max(1, int(float(spec.get("unit_count", 1) or 1)))
    if target.logical_name == "resistor":
        params = _resistor_subckt_param_text(_passive_unit_source_params(target))
    elif target.logical_name == "capacitor":
        params = _capacitor_subckt_param_text(_passive_unit_source_params(target))
    else:
        return []
    return [
        f"X_DUT_u{index} {' '.join(terminals)} {model}" + (f" {params}" if params else "")
        for index in range(unit_count)
    ]


def _passive_unit_source_params(target: PCellCalibreTarget) -> dict[str, Any]:
    params = dict(target.source_params)
    for key in ("M", "m", "multi"):
        params.pop(key, None)
    return params


def _passive_model_stub_lines(logical_name: str, model: str) -> list[str]:
    clean_model = str(model or "").strip()
    if not clean_model:
        return []
    if logical_name in {"resistor", "capacitor"}:
        return [f".SUBCKT {clean_model} PLUS MINUS", f".ENDS {clean_model}"]
    return []


def _format_spice_dimension(value_m: float) -> str:
    value = float(value_m)
    if value <= 0:
        return "0"
    value_nm = value * 1e9
    if abs(value_nm - round(value_nm)) < 1e-9:
        rounded = int(round(value_nm))
        if rounded % 1000 == 0:
            return f"{rounded // 1000}u"
        return f"{rounded}n"
    return f"{value:.12g}"


def _format_spice_value(value: float) -> str:
    return f"{float(value):.12g}"


def _format_spice_capacitance(value_f: float) -> str:
    value = float(value_f)
    if value <= 0:
        return "0"
    value_af = value * 1e18
    if abs(value_af - round(value_af)) < 1e-9 and value_af < 1000:
        return f"{int(round(value_af))}a"
    value_ff = value * 1e15
    if abs(value_ff - round(value_ff)) < 1e-9 and value_ff < 1000:
        return f"{int(round(value_ff))}f"
    value_pf = value * 1e12
    if abs(value_pf - round(value_pf)) < 1e-9 and value_pf < 1000:
        return f"{int(round(value_pf))}p"
    return f"{value:.12g}"


def _format_spice_area_um2(value_um2: float) -> str:
    # Calibre reports BJT area ``a`` in square microns.  In a SPICE numeric
    # field, plain ``25`` is interpreted as 25 m^2 and becomes 2.5e13 sq u.
    # ``25p`` is 25e-12 m^2, i.e. 25 um^2.
    return f"{float(value_um2):.12g}p"


def _float_param(params: Mapping[str, Any], key: str, default: float) -> float:
    return _parse_spice_number(params.get(key, default), default=default)


def _float_first(params: Mapping[str, Any], keys: Sequence[str], default: float) -> float:
    for key in keys:
        if key in params:
            return _float_param(params, key, default)
    return float(default)


def _parse_spice_number(value: Any, *, default: float) -> float:
    if isinstance(value, bool):
        return float(default)
    if isinstance(value, (float, int)):
        return float(value)
    text = str(value).strip()
    if not text:
        return float(default)
    lower = text.lower().replace(" ", "")
    suffixes = (
        ("meg", 1e6),
        ("t", 1e12),
        ("g", 1e9),
        ("k", 1e3),
        ("m", 1e-3),
        ("u", 1e-6),
        ("n", 1e-9),
        ("p", 1e-12),
        ("f", 1e-15),
        ("a", 1e-18),
    )
    for suffix, multiplier in suffixes:
        if lower.endswith(suffix):
            raw = lower[: -len(suffix)]
            if not raw:
                return float(default)
            try:
                return float(raw) * multiplier
            except ValueError:
                return float(default)
    try:
        return float(lower)
    except ValueError:
        return float(default)


def _bjt_emitter_area_um2(model: str) -> float | None:
    name = str(model or "").lower()
    if "10" in name:
        return 100.0
    if "1d6" in name or "1.6" in name:
        return 2.56
    if "5" in name:
        return 25.0
    if "2" in name:
        return 4.0
    return None


def _target_width_hint_um(target: PCellCalibreTarget) -> float:
    if target.logical_name in {"nmos", "pmos"}:
        try:
            return max(float(target.params.get("fingers", 1) or 1) * 0.3, 0.3)
        except (TypeError, ValueError):
            return 0.3
    if target.logical_name == "bjt":
        return 9.2
    if target.logical_name == "resistor":
        return 10.6
    if target.logical_name == "capacitor":
        return 2.0
    return 1.0


def _target_height_hint_um(target: PCellCalibreTarget) -> float:
    if target.logical_name in {"nmos", "pmos"}:
        try:
            return max(float(target.params.get("Wfg", 1e-6)) * 1e6, 0.2)
        except (TypeError, ValueError):
            return 0.2
    if target.logical_name == "bjt":
        return 9.2
    if target.logical_name == "resistor":
        return 3.2
    if target.logical_name == "capacitor":
        return 2.0
    return 1.0


def _default_terminals(logical_name: str) -> tuple[str, ...]:
    if logical_name in {"nmos", "pmos"}:
        return ("D", "G", "S", "B")
    if logical_name == "bjt":
        return ("C", "B", "E")
    if logical_name in {"resistor", "capacitor"}:
        return ("PLUS", "MINUS")
    return ()


def _metal_access_bbox(pdk: PdkConfig, xy: tuple[float, float], layer: str) -> tuple[float, float, float, float]:
    min_w = pdk.rules.min_width_um(layer)
    side = max(min_w, 0.05)
    half = 0.5 * side
    x, y = xy
    return pdk.rules.snap_bbox_um((x - half, y - half, x + half, y + half), mode="outward")


def _small_pin_bbox(pdk: PdkConfig, xy: tuple[float, float], layer: str) -> tuple[float, float, float, float]:
    side = max(pdk.rules.min_width_um(layer), 0.03)
    half = 0.5 * side
    x, y = xy
    return pdk.rules.snap_bbox_um((x - half, y - half, x + half, y + half), mode="outward")


def _port_pad_bbox(pdk: PdkConfig, xy: tuple[float, float], layer: str) -> tuple[float, float, float, float]:
    min_width = pdk.rules.min_width_um(layer) if layer in pdk.rules.min_width_nm else 0.05
    min_area_um2 = float(pdk.rules.min_area_nm2.get(str(layer), 0)) * 1e-6
    side = max(min_width, sqrt(min_area_um2) + 0.02 if min_area_um2 > 0.0 else 0.12)
    # Keep pads comfortably visible to streamout/Calibre.  The excess area is
    # part of the isolated testbench, not the calibrated PCell realization.
    side = max(side, 0.24 if str(layer).startswith("M") else 0.12)
    half = 0.5 * side
    x, y = xy
    return pdk.rules.snap_bbox_um((x - half, y - half, x + half, y + half), mode="outward")


def _bridge_rect(
    pdk: PdkConfig,
    layer: str,
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[float, float, float, float] | None:
    sx, sy = start
    ex, ey = end
    width = pdk.rules.min_width_um(layer) if layer in pdk.rules.min_width_nm else 0.05
    half = 0.5 * max(width, 0.05)
    if abs(sx - ex) <= abs(sy - ey):
        x0 = sx - half
        x1 = sx + half
        y0 = min(sy, ey) - half
        y1 = max(sy, ey) + half
    else:
        x0 = min(sx, ex) - half
        x1 = max(sx, ex) + half
        y0 = sy - half
        y1 = sy + half
    if abs(x1 - x0) <= 1e-12 or abs(y1 - y0) <= 1e-12:
        return None
    return pdk.rules.snap_bbox_um((x0, y0, x1, y1), mode="outward")


def _external_port_xy(
    logical_name: str,
    terminal: str,
    terminal_xy: tuple[float, float],
    pins_by_terminal: Mapping[str, Any],
) -> tuple[float, float]:
    xs = [float(pin.xy_um[0]) for pin in pins_by_terminal.values()]
    ys = [float(pin.xy_um[1]) for pin in pins_by_terminal.values()]
    min_x = min(xs) if xs else terminal_xy[0]
    max_x = max(xs) if xs else terminal_xy[0]
    min_y = min(ys) if ys else terminal_xy[1]
    max_y = max(ys) if ys else terminal_xy[1]
    x, y = terminal_xy
    if logical_name in {"nmos", "pmos"}:
        if terminal == "D":
            return (max_x + 1.5, y)
        if terminal == "S":
            return (min_x - 1.5, y)
        if terminal == "B":
            return (x, min_y - 1.0)
        if terminal == "G":
            return (x, min_y - 0.55)
    if logical_name == "bjt":
        if terminal == "C":
            return (x, max_y + 1.0)
        if terminal == "E":
            return (x, min_y - 1.0)
        if terminal == "B":
            return (min_x - 1.0, y)
    if logical_name == "resistor":
        if terminal == "PLUS":
            return (min_x - 1.0, y)
        if terminal == "MINUS":
            return (max_x + 1.0, y)
    if logical_name == "capacitor":
        if terminal == "PLUS":
            return (x, max_y + 1.0)
        if terminal == "MINUS":
            return (x, min_y - 1.0)
    return (max_x + 1.0, y)


def _bjt_direct_port_xy(
    terminal: str,
    xy_um: tuple[float, float],
    bbox_um: tuple[float, float, float, float],
) -> tuple[float, float]:
    if terminal == "B":
        # The CRN28 ``npn`` OA B pin bbox spans over several internal M1
        # shapes.  Calibre's actual base net is near the bbox lower-left
        # corner; placing the text at the bbox center attaches to the emitter
        # net and loses the B top-level port.
        return (float(bbox_um[0]), float(bbox_um[1]))
    return xy_um


def _dedupe_rects(rects: Sequence[OaRect]) -> tuple[OaRect, ...]:
    seen: set[tuple[str, str, tuple[float, float, float, float], str]] = set()
    result: list[OaRect] = []
    for rect in rects:
        key = (rect.layer, rect.purpose, rect.bbox, rect.net)
        if key in seen:
            continue
        seen.add(key)
        result.append(rect)
    return tuple(result)


def _crn28_same_net_mos_m2_bus_gap_bridges(pdk: PdkConfig, rects: Sequence[OaRect]) -> tuple[OaRect, ...]:
    access_cfg = _crn28_mos_access_config_um(pdk)
    max_gap_um = max(float(access_cfg.get("same_net_m2_bus_bridge_max_gap_um", 0.0) or 0.0), 0.0)
    if max_gap_um <= 0.0:
        return ()
    m2 = str(getattr(getattr(pdk, "layer_map", None), "metals", ("", "M2"))[1] or "M2")
    bus_kinds = {"crn28_mos_source_m2_bus", "crn28_mos_drain_m2_bus"}
    candidates_by_row: dict[tuple[str, str, float, float], list[OaRect]] = {}
    for rect in rects:
        if rect.layer != m2:
            continue
        if not str(rect.net or ""):
            continue
        if str(rect.metadata.get("kind", "")) not in bus_kinds:
            continue
        # Generated CRN28 source/drain buses are snapped horizontal rows.  Gap
        # bridges are only intended between adjacent fragments on the same row;
        # bucketing by y-span avoids an O(N^2) scan on reference-scale banks.
        row_key = (
            str(rect.net),
            str(rect.purpose),
            round(float(rect.bbox[1]), 9),
            round(float(rect.bbox[3]), 9),
        )
        candidates_by_row.setdefault(row_key, []).append(rect)
    bridges: list[OaRect] = []
    calibre_rules = DesignRuleDeck(grid_nm=_calibre_grid_nm(pdk))
    for row_rects in candidates_by_row.values():
        if len(row_rects) <= 1:
            continue
        ordered = sorted(row_rects, key=lambda rect: (float(rect.bbox[0]), float(rect.bbox[2])))
        for left, right in zip(ordered, ordered[1:]):
            if float(left.bbox[2]) > float(right.bbox[0]):
                continue
            y0 = max(float(left.bbox[1]), float(right.bbox[1]))
            y1 = min(float(left.bbox[3]), float(right.bbox[3]))
            gap = float(right.bbox[0]) - float(left.bbox[2])
            bbox = (float(left.bbox[2]), y0, float(right.bbox[0]), y1)
            if gap <= 1e-12 or gap > max_gap_um + 1e-12:
                continue
            bridges.append(
                OaRect(
                    m2,
                    left.purpose,
                    calibre_rules.snap_bbox_um(bbox, mode="outward"),
                    left.net,
                    metadata={
                        "kind": "crn28_mos_same_net_m2_bus_gap_bridge",
                        "source_instances": tuple(
                            dict.fromkeys(
                                str(rect.metadata.get("instance", ""))
                                for rect in (left, right)
                                if str(rect.metadata.get("instance", ""))
                            )
                        ),
                    },
                )
            )
    return tuple(_dedupe_rects(bridges))


def _crn28_mos_finger_rules(pdk: PdkConfig) -> Mapping[str, Any]:
    metadata = _metadata(pdk)
    direct = _mapping(metadata.get("mos_finger_constraints", {}))
    if direct:
        return direct
    sweep = _mapping(_mapping(metadata.get("pcell_drc_sweep", {})).get("strongarm_mos", {}))
    return _mapping(sweep.get("mos_finger_constraints", {}))


def _crn28_mos_pcell_overrides(pdk: PdkConfig, logical_name: str) -> dict[str, Any]:
    metadata = _metadata(pdk)
    sweep = _mapping(_mapping(metadata.get("pcell_drc_sweep", {})).get("strongarm_mos", {}))
    rules = _crn28_mos_finger_rules(pdk)
    variant_name = str(rules.get("variant", "") or "")
    variant_params: dict[str, Any] = {}
    for raw in tuple(sweep.get("variants", ()) or ()):
        item = _mapping(raw)
        if str(item.get("name", "")) == variant_name:
            variant_params = dict(_mapping(item.get("params", {})))
            break
    if logical_name == "pmos":
        variant_params.update(dict(_mapping(sweep.get("pmos_params", {}))))
    direct = _mapping(metadata.get("mos_pcell_overrides", {}))
    if logical_name in direct:
        variant_params.update(dict(_mapping(direct.get(logical_name, {}))))
    return variant_params


def _crn28_mos_access_config_um(pdk: PdkConfig) -> dict[str, Any]:
    """Return Calibre-calibrated CRN28 MOS access geometry in um.

    Values are intentionally sourced from ``metadata.calibre.mos_access`` so
    that PDK/rule-deck tuning stays in configuration.  The defaults are only a
    conservative fallback for older config files and unit tests.
    """

    metadata = _metadata(pdk)
    calibre = _mapping(metadata.get("calibre", {}))
    raw = _mapping(calibre.get("mos_access", {}))

    def nm_value(key: str, default_nm: float) -> float:
        value = raw.get(key, raw.get(key.replace("_nm", ""), default_nm))
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float(default_nm)
        if number <= 0.0:
            number = float(default_nm)
        return number * 1e-3

    def nonnegative_nm_value(key: str, default_nm: float) -> float:
        value = raw.get(key, raw.get(key.replace("_nm", ""), default_nm))
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float(default_nm)
        return max(number, 0.0) * 1e-3

    def str_value(key: str, default: str) -> str:
        value = raw.get(key, default)
        return str(value or default)

    contact_width = nm_value("contact_width_nm", 40.0)
    via1_width = nm_value("via1_width_nm", 50.0)
    return {
        "sd_bus_half_height_um": 0.5 * nm_value("sd_bus_height_nm", 240.0),
        "sd_m1_drop_half_width_um": 0.5 * nm_value("sd_m1_drop_width_nm", 120.0),
        "via1_half_um": 0.5 * via1_width,
        "via1_pitch_y_um": nm_value("via1_pitch_y_nm", 140.0),
        "gate_bus_half_height_um": 0.5 * nm_value("gate_bus_height_nm", 100.0),
        "gate_bus_y_offset_um": -nm_value("gate_bus_y_offset_nm", 140.0)
        if "gate_bus_y_offset_nm" not in raw and "gate_bus_y_offset_um" not in raw
        else (
            float(raw.get("gate_bus_y_offset_um", 0.0))
            if "gate_bus_y_offset_um" in raw
            else float(raw.get("gate_bus_y_offset_nm", -140.0)) * 1e-3
        ),
        "gate_po_extension_half_height_um": 0.5 * nm_value("gate_po_extension_height_nm", 90.0),
        "gate_po_overlap_um": nm_value("gate_po_overlap_nm", 80.0),
        "gate_contact_half_um": 0.5 * contact_width,
        "gate_m1_landing_half_um": 0.5 * nm_value("gate_m1_landing_width_nm", 140.0),
        "body_contact_half_um": 0.5 * contact_width,
        "body_m1_half_um": 0.5 * nm_value("body_m1_width_nm", 340.0),
        "body_tap_x_mode": str_value("body_tap_x_mode", "center"),
        "body_tap_side_margin_um": nm_value("body_tap_side_margin_nm", 620.0),
        "same_net_m2_bus_bridge_max_gap_um": nonnegative_nm_value("same_net_m2_bus_bridge_max_gap_nm", 0.0),
    }


def _crn28_mos_body_tap_x_um(
    calibre_rules: DesignRuleDeck,
    min_x: float,
    max_x: float,
    access_cfg: Mapping[str, Any],
) -> float:
    mode = str(access_cfg.get("body_tap_x_mode", "center") or "center").strip().lower()
    margin = max(float(access_cfg.get("body_tap_side_margin_um", 0.62) or 0.62), 0.0)
    if mode in {"left", "start", "outside_left"}:
        return calibre_rules.snap_um(float(min_x) - margin)
    if mode in {"right", "end", "outside_right"}:
        return calibre_rules.snap_um(float(max_x) + margin)
    return calibre_rules.snap_um((float(min_x) + float(max_x)) * 0.5)


def _exact_grid_bbox_around_center_um(pdk: PdkConfig, bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Snap a fixed-size cut bbox to grid without changing its width/height."""

    grid_nm = max(1, _calibre_grid_nm(pdk))
    x0, y0, x1, y1 = (float(value) for value in bbox)
    width_grid = max(1, int(round(abs(x1 - x0) * 1000.0 / grid_nm)))
    height_grid = max(1, int(round(abs(y1 - y0) * 1000.0 / grid_nm)))
    cx_grid = ((x0 + x1) * 0.5) * 1000.0 / grid_nm
    cy_grid = ((y0 + y1) * 0.5) * 1000.0 / grid_nm
    x0_grid = int(round(cx_grid - 0.5 * width_grid))
    y0_grid = int(round(cy_grid - 0.5 * height_grid))

    def to_um(grid_value: int) -> float:
        return round(grid_value * grid_nm * 1e-3, 12)

    return (
        to_um(x0_grid),
        to_um(y0_grid),
        to_um(x0_grid + width_grid),
        to_um(y0_grid + height_grid),
    )


def _metadata(pdk: PdkConfig) -> Mapping[str, Any]:
    metadata = getattr(pdk, "metadata", {})
    return metadata if isinstance(metadata, Mapping) else {}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _apply_lvs_deck_rewrites(deck_text: str, pdk: PdkConfig) -> str:
    metadata = _metadata(pdk)
    calibre = _mapping(metadata.get("calibre", {}))
    lvs = _mapping(calibre.get("lvs", {}))
    rewritten = deck_text
    if bool(lvs.get("enable_multifinger", False)):
        rewritten = rewritten.replace("//#define MULTI_FINGER", "#define MULTI_FINGER")
    for raw_layer in tuple(lvs.get("streamout_text_port_layers", ()) or ()):
        try:
            layer = int(raw_layer)
        except (TypeError, ValueError):
            continue
        statement = f"PORT LAYER TEXT {layer}"
        if statement in rewritten:
            continue
        needle = f"TEXT LAYER {layer} ATTACH"
        if needle in rewritten:
            rewritten = rewritten.replace(needle, statement + "\n" + needle, 1)
    rewritten = _ensure_all_attached_text_layers_are_ports(rewritten)
    return rewritten


def _ensure_all_attached_text_layers_are_ports(deck_text: str) -> str:
    """Make isolated PCell probe text labels visible as LVS ports.

    CRN28 streamout can map OA ``M1/text`` and ``M2/text`` labels to different
    derived text layers depending on runset/variant.  For isolated calibration
    decks we want every attached text layer to be eligible as a port layer.
    """

    lines = deck_text.splitlines()
    existing = {
        match.group(1)
        for line in lines
        for match in (re.match(r"\s*PORT\s+LAYER\s+TEXT\s+(\d+)\b", line, flags=re.IGNORECASE),)
        if match
    }
    emitted: set[str] = set()
    output: list[str] = []
    for line in lines:
        match = re.match(r"(\s*)TEXT\s+LAYER\s+(\d+)\s+ATTACH\b", line, flags=re.IGNORECASE)
        if match:
            layer = match.group(2)
            if layer not in existing and layer not in emitted:
                output.append(f"PORT LAYER TEXT {layer}")
                emitted.add(layer)
        output.append(line)
    return "\n".join(output) + ("\n" if deck_text.endswith("\n") else "")


def _calibre_grid_nm(pdk: PdkConfig) -> int:
    metadata = _metadata(pdk)
    calibre = _mapping(metadata.get("calibre", {}))
    try:
        return int(calibre.get("grid_nm", pdk.rules.grid_nm) or pdk.rules.grid_nm)
    except (TypeError, ValueError):
        return pdk.rules.grid_nm


def _ignored_drc_rule(name: str) -> bool:
    rule = str(name)
    return rule in IGNORED_DRC_RULE_NAMES or rule.startswith(IGNORED_DRC_RULE_PREFIXES) or ".DN." in rule


def _lvs_property_errors(text: str) -> tuple[dict[str, Any], ...]:
    """Parse Calibre LVS PROPERTY ERRORS rows into machine-readable records."""

    errors: list[dict[str, Any]] = []
    pending_disc: str | None = None
    pending_layout_device = ""
    pending_source_device = ""
    disc_pattern = re.compile(
        r"^\s*(\d+)\s+(.+?)\s{2,}(.+?)\s*$",
        flags=re.IGNORECASE,
    )
    prop_pattern = re.compile(
        r"^\s*([A-Za-z_][A-Za-z0-9_.$:-]*)\s*:\s*([-+0-9.eE]+)\s*([A-Za-z ]*?)\s{2,}"
        r"\1\s*:\s*([-+0-9.eE]+)\s*([A-Za-z ]*?)\s{2,}(\S+)",
        flags=re.IGNORECASE,
    )
    in_property_section = False
    for line in text.splitlines():
        upper = line.upper()
        if "PROPERTY ERRORS" in upper:
            in_property_section = True
            pending_disc = None
            pending_layout_device = ""
            pending_source_device = ""
            continue
        if in_property_section and "LVS PARAMETERS" in upper:
            break
        if not in_property_section:
            continue
        if set(line.strip()) <= {"*", "-"}:
            continue
        disc_match = disc_pattern.match(line)
        if disc_match and ":" not in line:
            pending_disc = disc_match.group(1)
            pending_layout_device = " ".join(disc_match.group(2).split())
            pending_source_device = " ".join(disc_match.group(3).split())
            continue
        prop_match = prop_pattern.match(line)
        if not prop_match:
            continue
        prop = prop_match.group(1)
        layout_raw = prop_match.group(2)
        layout_unit = prop_match.group(3)
        source_raw = prop_match.group(4)
        source_unit = prop_match.group(5)
        error = prop_match.group(6)
        errors.append(
            {
                "disc": pending_disc,
                "property": prop,
                "layout_value": _parse_lvs_numeric_with_unit(layout_raw, layout_unit),
                "layout_value_text": " ".join(part for part in (layout_raw, layout_unit) if part),
                "source_value": _parse_lvs_numeric_with_unit(source_raw, source_unit),
                "source_value_text": " ".join(part for part in (source_raw, source_unit) if part),
                "error": error,
                "layout_device": pending_layout_device,
                "source_device": pending_source_device,
            }
        )
    return tuple(errors)


def _lvs_issue_classes(
    text: str,
    *,
    ports: tuple[int, int] | None,
    nets: tuple[int, int] | None,
    instances: tuple[int, int] | None,
    property_errors: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    upper = text.upper()
    issues: list[str] = []
    if property_errors:
        issues.append("property_mismatch")
    if "CONNECTIVITY ERRORS" in upper:
        issues.append("connectivity_mismatch")
    if ports and ports[0] != ports[1]:
        issues.append("port_count_mismatch")
    if nets and nets[0] != nets[1]:
        issues.append("net_count_mismatch")
    if instances and instances[0] != instances[1]:
        issues.append("instance_count_mismatch")
    if "DIFFERENT NUMBERS OF INSTANCES" in upper:
        issues.append("instance_count_mismatch")
    if re.search(r"Direct connection between different ports", text, flags=re.IGNORECASE):
        issues.append("direct_port_short")
    if re.search(r"Short circuit - Different names on one net", text, flags=re.IGNORECASE):
        issues.append("named_net_short")
    return tuple(dict.fromkeys(issues))


def _parse_lvs_numeric_with_unit(value: str, unit: str = "") -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    unit_l = str(unit or "").strip().lower()
    unit_l_compact = unit_l.replace(" ", "").replace("_", "")
    if unit_l_compact in {"squ", "squm", "um2", "u2"}:
        return number * 1e-12
    if unit_l_compact in {"sqn", "sqnm", "nm2", "n2"}:
        return number * 1e-18
    scale_by_unit = {
        "": 1.0,
        "f": 1e-15,
        "p": 1e-12,
        "n": 1e-9,
        "u": 1e-6,
        "m": 1e-3,
        "k": 1e3,
        "meg": 1e6,
        "g": 1e9,
    }
    return number * scale_by_unit.get(unit_l, 1.0)


def _last_table_int_pair_for_label(text: str, label: str) -> tuple[int, int] | None:
    result: tuple[int, int] | None = None
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*:\s+(\d+)\s+(\d+)\b", flags=re.IGNORECASE)
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            result = (int(match.group(1)), int(match.group(2)))
    return result


def _log_has_pcell_eval_failure(path: str | Path | None) -> bool:
    if path is None:
        return False
    path_obj = Path(path)
    if not path_obj.exists():
        return False
    text = path_obj.read_text(encoding="utf-8", errors="replace")
    return bool(re.search(r"pcellEvalFailed|invalid box|PCell evaluation failed", text, flags=re.IGNORECASE))


def _log_has_streamout_missing_topcell(path: str | Path | None) -> bool:
    if path is None:
        return False
    path_obj = Path(path)
    if not path_obj.exists():
        return False
    text = path_obj.read_text(encoding="utf-8", errors="replace")
    return bool(
        re.search(r'specified cell ".+?" was not found in OpenAccess library', text, flags=re.IGNORECASE)
        or re.search(r"gds data was not created for this cell", text, flags=re.IGNORECASE)
    )


def _summary_int(summary: Mapping[str, Any], key: str) -> int:
    try:
        return int(summary.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _returncode(value: object) -> int | None:
    if not isinstance(value, Mapping):
        return None
    raw = value.get("returncode")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _run_command(command: Sequence[str], *, cwd: Path, log_path: Path) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(completed.stdout or "", encoding="utf-8", errors="replace")
    return {"returncode": completed.returncode, "log": str(log_path), "command": list(command)}


def _safe_token(name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_]+", "_", str(name).strip())
    token = re.sub(r"_+", "_", token).strip("_")
    return token or "pcell"


def _dedupe_targets(targets: Sequence[PCellCalibreTarget]) -> tuple[PCellCalibreTarget, ...]:
    seen: set[tuple[str, str, str, str]] = set()
    result: list[PCellCalibreTarget] = []
    for target in targets:
        key = (
            target.logical_name,
            target.pcell_key,
            json.dumps(target.params, sort_keys=True, default=str),
            json.dumps(target.source_params, sort_keys=True, default=str),
            json.dumps(target.metadata, sort_keys=True, default=str),
            target.source_model,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(target)
    return tuple(result)
