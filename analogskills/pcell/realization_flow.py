"""Turn Calibre/OA PCell calibration artifacts into SMT realization candidates.

The lower-level calibration modules answer two separate questions:

* OA introspection: where is the PCell bbox and where are routable terminals?
* Calibre: is a concrete PCell parameterization DRC/LVS clean?

This module joins those answers into a stable asset for the layout solver:
``metadata.pcell_realization.<logical>.candidates`` rows that are backed by
real PCell parameters, real bbox data, and Calibre classification.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from analogskills.pcell.calibre_calibration import PCellCalibreCatalog, PCellCalibreCatalogEntry, PCellCalibreTarget
from analogskills.pcell.calibration import PCellCalibrationCache
from analogskills.pcell.calibration_run import PCellCalibrationManifest, PCellCalibrationTarget
from analogskills.pdk import PdkConfig


DEFAULT_REALIZATION_LOGICALS = ("bjt", "resistor", "capacitor")
DEFAULT_EXPORT_STATUSES = ("clean",)


@dataclass(frozen=True)
class PCellRealizationExport:
    """Exported PCell realization rows plus audit metadata."""

    pdk: str
    config: dict[str, Any]
    rows: tuple[dict[str, Any], ...] = ()
    skipped: tuple[dict[str, Any], ...] = ()
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdk": self.pdk,
            "config": self.config,
            "rows": list(self.rows),
            "skipped": list(self.skipped),
            "summary": dict(self.summary),
        }

    def save_json(self, path: str | Path) -> Path:
        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path_obj


def build_pcell_access_manifest_from_calibre_targets(
    pdk_name: str,
    targets: Sequence[PCellCalibreTarget],
    *,
    calibration_lib: str = "analogskills_pcell_calib",
    calibration_cell_prefix: str = "pcell_access",
) -> PCellCalibrationManifest:
    """Build an OA-introspection manifest from the Calibre calibration targets."""

    access_targets: list[PCellCalibrationTarget] = []
    seen: set[tuple[str, str, tuple[tuple[str, str], ...], str]] = set()
    for index, target in enumerate(targets):
        key = (target.logical_name, target.pcell_key, _params_signature(target.params), target.orient)
        if key in seen:
            continue
        seen.add(key)
        access_targets.append(
            PCellCalibrationTarget(
                logical_name=target.logical_name,
                lib_name=target.lib_name,
                cell_name=target.cell_name,
                view_name=target.view_name,
                params=dict(target.params),
                orient=target.orient,
                instance_name="DUT",
                calibration_lib=calibration_lib,
                calibration_cell=f"{calibration_cell_prefix}_{index:04d}_{_safe_token(target.name)}",
            )
        )
    return PCellCalibrationManifest(
        pdk=str(pdk_name),
        targets=tuple(access_targets),
        metadata={
            "source": "build_pcell_access_manifest_from_calibre_targets",
            "calibre_target_count": len(tuple(targets)),
        },
    )


def build_pcell_realization_config_from_catalog(
    catalog: PCellCalibreCatalog,
    pdk: PdkConfig,
    *,
    calibration_cache: PCellCalibrationCache | None = None,
    logical_names: Sequence[str] = DEFAULT_REALIZATION_LOGICALS,
    include_statuses: Sequence[str] = DEFAULT_EXPORT_STATUSES,
    include_existing_candidates: bool = False,
) -> PCellRealizationExport:
    """Build PDK ``pcell_realization`` config rows from a Calibre catalog.

    By default only fully clean entries are exported.  Rows without a trusted
    bbox are skipped because SMT placement cannot use them safely.
    """

    allowed_logicals = {str(item).lower() for item in logical_names}
    allowed_statuses = {str(item).lower() for item in include_statuses}
    pdk_realization = _pdk_realization_config(pdk)
    by_logical: dict[str, list[dict[str, Any]]] = {logical: [] for logical in sorted(allowed_logicals)}
    skipped: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...], int, int]] = set()

    for entry in catalog.entries:
        target = entry.target
        logical = str(target.logical_name).lower()
        status = str(entry.classification.get("status", "unknown") or "unknown").lower()
        if logical not in allowed_logicals:
            skipped.append(_skip_row(entry, "logical_not_requested"))
            continue
        if status not in allowed_statuses:
            skipped.append(_skip_row(entry, f"status_{status}_not_exported"))
            continue
        bbox = _entry_bbox_um(entry, pdk, calibration_cache)
        if bbox is None:
            skipped.append(_skip_row(entry, "missing_trusted_bbox"))
            continue
        candidate = _candidate_from_catalog_entry(entry, bbox)
        if candidate is None:
            skipped.append(_skip_row(entry, "unsupported_logical_for_candidate_export"))
            continue
        signature = (
            logical,
            _params_signature(candidate.get("pcell_params", {})),
            _candidate_sizing_signature(candidate.get("sizing_overrides", {})),
            round(float(candidate["layout_width_um"]) * 1000),
            round(float(candidate["layout_height_um"]) * 1000),
        )
        if signature in seen:
            skipped.append(_skip_row(entry, "duplicate_candidate_signature"))
            continue
        seen.add(signature)
        by_logical.setdefault(logical, []).append(candidate)
        candidate_rows.append({"logical_name": logical, **candidate})

    realization_config: dict[str, Any] = {}
    for logical in sorted(allowed_logicals):
        base_cfg = dict(_mapping(pdk_realization.get(logical, {})))
        existing = tuple(_mapping(item) for item in tuple(base_cfg.get("candidates", ()) or ()))
        candidates = tuple(by_logical.get(logical, ()))
        if not candidates and not include_existing_candidates:
            continue
        if include_existing_candidates:
            candidates = _dedupe_candidate_dicts((*existing, *candidates))
        base_cfg["candidates"] = list(candidates)
        base_cfg.setdefault("require_calibrated", False)
        base_cfg.setdefault("allow_nearest_calibration", False)
        base_cfg["candidate_source"] = "pcell_realization_flow"
        realization_config[logical] = base_cfg

    config = {
        "metadata": {
            "source": "build_pcell_realization_config_from_catalog",
            "catalog_pdk": catalog.pdk,
            "include_statuses": sorted(allowed_statuses),
            "logical_names": sorted(allowed_logicals),
            "uses_calibration_cache": calibration_cache is not None,
        },
        "pcell_realization": realization_config,
    }
    summary = _export_summary(catalog, candidate_rows, skipped)
    return PCellRealizationExport(str(pdk.name), config, tuple(candidate_rows), tuple(skipped), summary)


def merge_pcell_realization_config_into_pdk_data(
    pdk_data: Mapping[str, Any],
    export: PCellRealizationExport | Mapping[str, Any],
    *,
    replace_candidates: bool = True,
) -> dict[str, Any]:
    """Return a PDK JSON dictionary with exported realization candidates merged."""

    data = json.loads(json.dumps(dict(pdk_data)))
    export_config = export.config if isinstance(export, PCellRealizationExport) else dict(export).get("config", dict(export))
    exported_realization = _mapping(export_config.get("pcell_realization", {}))
    metadata = data.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        data["metadata"] = metadata
    pcell_realization = metadata.setdefault("pcell_realization", {})
    if not isinstance(pcell_realization, dict):
        pcell_realization = {}
        metadata["pcell_realization"] = pcell_realization
    for logical, exported_cfg_obj in exported_realization.items():
        logical_key = str(logical)
        exported_cfg = dict(_mapping(exported_cfg_obj))
        current_cfg = dict(_mapping(pcell_realization.get(logical_key, {})))
        exported_candidates = tuple(_mapping(item) for item in tuple(exported_cfg.get("candidates", ()) or ()))
        if replace_candidates:
            merged_candidates = exported_candidates
        else:
            merged_candidates = _dedupe_candidate_dicts(
                *(
                    tuple(_mapping(item) for item in tuple(current_cfg.get("candidates", ()) or ())),
                    exported_candidates,
                )
            )
        current_cfg.update({key: value for key, value in exported_cfg.items() if key != "candidates"})
        current_cfg["candidates"] = list(merged_candidates)
        pcell_realization[logical_key] = current_cfg
    metadata["pcell_realization_export"] = dict(_mapping(export_config.get("metadata", {})))
    return data


def write_pdk_json_with_pcell_realization_config(
    pdk_json: str | Path,
    export: PCellRealizationExport,
    output_path: str | Path,
    *,
    replace_candidates: bool = True,
) -> Path:
    """Write a PDK JSON with exported PCell realization candidates merged in."""

    source_path = Path(pdk_json)
    data = json.loads(source_path.read_text(encoding="utf-8"))
    merged = merge_pcell_realization_config_into_pdk_data(data, export, replace_candidates=replace_candidates)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def write_pcell_realization_markdown_report(export: PCellRealizationExport, path: str | Path) -> Path:
    """Write a concise review report for the exported SMT candidates."""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# PCell realization calibration export",
        "",
        f"- PDK: `{export.pdk}`",
        f"- Exported candidates: {export.summary.get('exported_count', 0)}",
        f"- Skipped catalog entries: {export.summary.get('skipped_count', 0)}",
        "",
        "## Exported candidates",
        "",
        "| logical | name | bbox_um | pcell_params | status |",
        "|---|---|---:|---|---|",
    ]
    for row in export.rows:
        bbox = f"{float(row.get('layout_width_um', 0.0)):.4g} x {float(row.get('layout_height_um', 0.0)):.4g}"
        params = json.dumps(row.get("pcell_params", {}), sort_keys=True)
        status = str(row.get("calibre_status", ""))
        lines.append(f"| {row.get('logical_name', '')} | `{row.get('name', '')}` | {bbox} | `{params}` | {status} |")
    lines.extend(["", "## Skipped entries", "", "| logical | target | reason | status |", "|---|---|---|---|"])
    for row in export.skipped[:200]:
        lines.append(f"| {row.get('logical_name', '')} | `{row.get('target', '')}` | {row.get('reason', '')} | {row.get('status', '')} |")
    if len(export.skipped) > 200:
        lines.append(f"| ... | ... | {len(export.skipped) - 200} more skipped entries omitted | ... |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def _candidate_from_catalog_entry(entry: PCellCalibreCatalogEntry, bbox_um: tuple[float, float]) -> dict[str, Any] | None:
    target = entry.target
    logical = str(target.logical_name).lower()
    metadata = _mapping(target.metadata)
    realization = _mapping(metadata.get("realization_candidate", {}))
    sizing = _candidate_sizing_overrides(target, realization)
    if logical not in DEFAULT_REALIZATION_LOGICALS:
        return None
    width_um, height_um = bbox_um
    base_name = str(realization.get("name", "") or target.name)
    return {
        "name": _safe_candidate_name(logical, base_name),
        "width_um": float(width_um),
        "height_um": float(height_um),
        "layout_width_um": float(width_um),
        "layout_height_um": float(height_um),
        "native_pcell_realization": True,
        "calibrated_pcell_realization": True,
        "configured_pcell_params": bool(target.params),
        "pcell_realization_kind": f"{logical}_native",
        "pcell_realization_source": "calibre_catalog",
        "pcell_calibre_status": str(entry.classification.get("status", "")),
        "pcell_calibre_usable_for_layout": bool(entry.usable_for_layout),
        "sizing_overrides": sizing,
        "pcell_params": dict(target.params),
        "pcell_overrides": dict(target.params),
        "cost": int(realization.get("cost", 0) or 0),
        "drc_clean": bool(entry.usable_for_layout),
        "lvs_clean": bool(entry.usable_for_layout),
        "calibre_status": str(entry.classification.get("status", "")),
        "calibre_source_model": target.source_model,
        "notes": _candidate_notes(entry, realization),
    }


def _candidate_sizing_overrides(target: PCellCalibreTarget, realization: Mapping[str, Any]) -> dict[str, Any]:
    logical = str(target.logical_name).lower()
    sizing = dict(_mapping(realization.get("sizing_overrides", {})))
    source = _mapping(target.source_params)
    if logical == "bjt":
        sizing.setdefault("M", max(1, int(float(source.get("M", source.get("m", 1)) or 1))))
    elif logical == "resistor":
        if "R" in source or "r" in source:
            sizing.setdefault("R", float(source.get("R", source.get("r"))))
        if "w" in source or "W" in source or "width" in source:
            sizing.setdefault("W", float(source.get("W", source.get("w", source.get("width")))))
    elif logical == "capacitor":
        if "C" in source or "c" in source:
            sizing.setdefault("C", float(source.get("C", source.get("c"))))
    return sizing


def _entry_bbox_um(
    entry: PCellCalibreCatalogEntry,
    pdk: PdkConfig,
    calibration_cache: PCellCalibrationCache | None,
) -> tuple[float, float] | None:
    target = entry.target
    cached = _calibration_cache_bbox_um(target, calibration_cache)
    if cached is not None:
        return cached
    metadata_bbox = _metadata_realization_bbox_um(target)
    if metadata_bbox is not None:
        return metadata_bbox
    configured = _configured_candidate_bbox_um(target, pdk)
    if configured is not None:
        return configured
    return None


def _calibration_cache_bbox_um(
    target: PCellCalibreTarget,
    calibration_cache: PCellCalibrationCache | None,
) -> tuple[float, float] | None:
    if calibration_cache is None:
        return None
    entry = calibration_cache.lookup(
        logical_name=target.logical_name,
        pcell=target.pcell_key,
        params=target.params,
        orient=target.orient,
        allow_nearest=False,
    )
    if entry is None:
        return None
    bbox = entry.instance_bbox_um or entry.bbox_um
    return _bbox_width_height(bbox)


def _metadata_realization_bbox_um(target: PCellCalibreTarget) -> tuple[float, float] | None:
    realization = _mapping(_mapping(target.metadata).get("realization_candidate", {}))
    width = _positive_float(realization.get("layout_width_um", realization.get("width_um")))
    height = _positive_float(realization.get("layout_height_um", realization.get("height_um")))
    if width is not None and height is not None:
        return (width, height)
    return None


def _configured_candidate_bbox_um(target: PCellCalibreTarget, pdk: PdkConfig) -> tuple[float, float] | None:
    cfg = _mapping(_pdk_realization_config(pdk).get(str(target.logical_name).lower(), {}))
    target_name = str(_mapping(_mapping(target.metadata).get("realization_candidate", {})).get("name", ""))
    for item_obj in tuple(cfg.get("candidates", ()) or ()):
        item = _mapping(item_obj)
        if target_name and str(item.get("name", "")) != target_name:
            continue
        width = _positive_float(item.get("layout_width_um", item.get("width_um")))
        height = _positive_float(item.get("layout_height_um", item.get("height_um")))
        if width is not None and height is not None:
            return (width, height)
    return None


def _candidate_notes(entry: PCellCalibreCatalogEntry, realization: Mapping[str, Any]) -> str:
    parts = []
    notes = str(realization.get("notes", "") or "")
    if notes:
        parts.append(notes)
    parts.append(f"Exported from Calibre PCell catalog target={entry.target.name} status={entry.classification.get('status', '')}.")
    return " ".join(parts)


def _skip_row(entry: PCellCalibreCatalogEntry, reason: str) -> dict[str, Any]:
    return {
        "logical_name": entry.target.logical_name,
        "target": entry.target.name,
        "status": str(entry.classification.get("status", "unknown") or "unknown"),
        "reason": reason,
    }


def _export_summary(
    catalog: PCellCalibreCatalog,
    rows: Sequence[Mapping[str, Any]],
    skipped: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_logical: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for row in rows:
        logical = str(row.get("logical_name", ""))
        by_logical[logical] = by_logical.get(logical, 0) + 1
        status = str(row.get("calibre_status", ""))
        by_status[status] = by_status.get(status, 0) + 1
    skipped_by_reason: dict[str, int] = {}
    for row in skipped:
        reason = str(row.get("reason", ""))
        skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
    return {
        "catalog_entry_count": len(catalog.entries),
        "exported_count": len(rows),
        "skipped_count": len(skipped),
        "by_logical_name": dict(sorted(by_logical.items())),
        "by_status": dict(sorted(by_status.items())),
        "skipped_by_reason": dict(sorted(skipped_by_reason.items())),
    }


def _pdk_realization_config(pdk: PdkConfig) -> Mapping[str, Any]:
    return _mapping(_mapping(getattr(pdk, "metadata", {})).get("pcell_realization", {}))


def _dedupe_candidate_dicts(*candidate_groups: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], tuple[tuple[str, str], ...], int, int]] = set()
    for group in candidate_groups:
        for item_obj in group:
            item = dict(_mapping(item_obj))
            key = (
                tuple(sorted((str(k), repr(v)) for k, v in _mapping(item.get("pcell_params", item.get("pcell_overrides", {}))).items())),
                _candidate_sizing_signature(item.get("sizing_overrides", {})),
                round(float(item.get("layout_width_um", item.get("width_um", 0.0)) or 0.0) * 1000),
                round(float(item.get("layout_height_um", item.get("height_um", 0.0)) or 0.0) * 1000),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
    return tuple(result)


def _candidate_sizing_signature(sizing: object) -> tuple[tuple[str, str], ...]:
    keep = {
        key: value
        for key, value in _mapping(sizing).items()
        if key
        not in {
            "layout_width_um",
            "layout_height_um",
            "native_pcell_realization",
            "calibrated_pcell_realization",
            "configured_pcell_params",
            "pcell_realization_kind",
            "pcell_realization_source",
            "pcell_calibre_status",
            "pcell_calibre_usable_for_layout",
        }
    }
    return _params_signature(keep)


def _params_signature(params: object) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), repr(value)) for key, value in _mapping(params).items()))


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bbox_width_height(bbox: object) -> tuple[float, float] | None:
    if not isinstance(bbox, (tuple, list)) or len(bbox) < 4:
        return None
    try:
        width = abs(float(bbox[2]) - float(bbox[0]))
        height = abs(float(bbox[3]) - float(bbox[1]))
    except (TypeError, ValueError):
        return None
    if width <= 0.0 or height <= 0.0:
        return None
    return (width, height)


def _positive_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0.0 else None


def _safe_candidate_name(logical: str, raw: str) -> str:
    base = _safe_token(raw) or "candidate"
    return base if base.startswith(f"{logical}_") else f"{logical}_{base}"


def _safe_token(value: object) -> str:
    token = re.sub(r"[^A-Za-z0-9_]+", "_", str(value).strip())
    token = re.sub(r"_+", "_", token).strip("_")
    return token or "x"
