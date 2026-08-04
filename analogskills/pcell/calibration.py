"""PCell calibration cache built from OA introspection artifacts."""
from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
import re
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from analogskills.eda.pcell_introspection import BBox, PCellAccessCandidate, PCellIntrospectionResult, Point
from analogskills.pcell.generation import PCellInstancePlan


@dataclass(frozen=True)
class PCellCalibrationAccess:
    terminal: str
    xy_um: Point
    layer: str
    source: str
    bbox_um: BBox | None = None
    confidence: float = 1.0
    reason: str = ""
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_candidate(cls, candidate: PCellAccessCandidate, *, warnings: Sequence[str] = ()) -> "PCellCalibrationAccess":
        candidate_warnings = tuple(str(item) for item in getattr(candidate, "warnings", ()))
        return cls(
            terminal=candidate.terminal,
            xy_um=tuple(candidate.xy_um),
            layer=candidate.layer,
            source=candidate.source,
            bbox_um=tuple(candidate.bbox_um) if candidate.bbox_um is not None else None,
            confidence=float(candidate.confidence),
            reason=candidate.reason,
            warnings=tuple(dict.fromkeys([*candidate_warnings, *(str(item) for item in warnings)])),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellCalibrationAccess":
        return cls(
            terminal=str(data.get("terminal", "")),
            xy_um=_point_tuple(data.get("xy_um", data.get("xy", (0.0, 0.0)))),
            layer=str(data.get("layer", "")),
            source=str(data.get("source", "calibration")),
            bbox_um=_optional_bbox(data.get("bbox_um", data.get("bbox"))),
            confidence=float(data.get("confidence", 1.0)),
            reason=str(data.get("reason", "")),
            warnings=tuple(str(item) for item in data.get("warnings", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "terminal": self.terminal,
            "xy_um": list(self.xy_um),
            "layer": self.layer,
            "source": self.source,
            "bbox_um": list(self.bbox_um) if self.bbox_um is not None else None,
            "confidence": self.confidence,
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PCellCalibrationEntry:
    logical_name: str
    pcell: str
    params_signature: tuple[tuple[str, str], ...]
    orient: str = "R0"
    bbox_um: BBox | None = None
    instance_bbox_um: BBox | None = None
    terminals: dict[str, tuple[PCellCalibrationAccess, ...]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return _entry_key(self.logical_name, self.pcell, self.params_signature, self.orient)

    @classmethod
    def from_introspection(
        cls,
        result: PCellIntrospectionResult,
        *,
        preferred_layers: Sequence[str] = (),
    ) -> "PCellCalibrationEntry":
        terminals: dict[str, tuple[PCellCalibrationAccess, ...]] = {}
        warnings = list(result.warnings)
        names = _terminal_names_from_result(result)
        merged: dict[str, list[PCellCalibrationAccess]] = {}
        for terminal in names:
            candidates = result.terminal_access_candidates(terminal, preferred_layers=preferred_layers)
            routable_candidates = tuple(candidate for candidate in candidates if _is_routable_access_layer(candidate.layer))
            if routable_candidates:
                logical_terminal = _logical_finger_terminal(terminal) if result.request.logical_name in {"nmos", "pmos"} else terminal
                merged.setdefault(logical_terminal, []).extend(PCellCalibrationAccess.from_candidate(candidate) for candidate in routable_candidates)
            elif candidates and _is_terminal_name(terminal):
                warnings.append(f"terminal {terminal} has no routable access candidate; external tap or template fallback required")
        terminals = {
            terminal: tuple(dict.fromkeys(accesses))
            for terminal, accesses in merged.items()
        }
        return cls(
            logical_name=result.request.logical_name,
            pcell=result.request.pcell_key,
            params_signature=_params_signature(result.request.params),
            orient=result.request.orient,
            bbox_um=result.master_bbox_um,
            instance_bbox_um=result.instance_bbox_um,
            terminals=terminals,
            warnings=tuple(dict.fromkeys(warnings)),
            errors=result.errors,
            metadata={**result.metadata, "raw_artifact_path": result.raw_artifact_path, "coordinate_space": "master_local_um"},
        )
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellCalibrationEntry":
        terminals: dict[str, tuple[PCellCalibrationAccess, ...]] = {}
        raw_terminals = data.get("terminals", {})
        if isinstance(raw_terminals, Mapping):
            for terminal, access_data in raw_terminals.items():
                if isinstance(access_data, Mapping) and "access_points_um" in access_data:
                    terminals[str(terminal)] = _legacy_terminal_access(str(terminal), access_data)
                else:
                    terminals[str(terminal)] = tuple(PCellCalibrationAccess.from_dict(item) for item in access_data)
        return cls(
            logical_name=str(data.get("logical_name", "")),
            pcell=str(data.get("pcell", data.get("pcell_key", ""))),
            params_signature=_params_signature(data.get("params_signature", data.get("params", {}))),
            orient=str(data.get("orient", "R0")),
            bbox_um=_optional_bbox(data.get("bbox_um", data.get("bbox"))),
            instance_bbox_um=_optional_bbox(data.get("instance_bbox_um", data.get("instance_bbox"))),
            terminals=terminals,
            warnings=tuple(str(item) for item in data.get("warnings", ())),
            errors=tuple(str(item) for item in data.get("errors", ())),
            metadata=dict(data.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_name": self.logical_name,
            "pcell": self.pcell,
            "params_signature": {key: value for key, value in self.params_signature},
            "orient": self.orient,
            "bbox_um": list(self.bbox_um) if self.bbox_um is not None else None,
            "instance_bbox_um": list(self.instance_bbox_um) if self.instance_bbox_um is not None else None,
            "terminals": {
                terminal: [access.to_dict() for access in accesses]
                for terminal, accesses in sorted(self.terminals.items())
            },
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "metadata": dict(self.metadata),
        }

    def terminal_access_candidates(
        self,
        terminal: str,
        *,
        preferred_layers: Sequence[str] = (),
    ) -> tuple[PCellCalibrationAccess, ...]:
        accesses = tuple(self.terminals.get(str(terminal), ()))
        return _sort_accesses(accesses, preferred_layers)


def _logical_finger_terminal(name: str) -> str:
    match = re.fullmatch(r"([GSD])(?:_[0-9]+)?", str(name))
    return match.group(1) if match else str(name)


@dataclass(frozen=True)
class PCellCalibrationCache:
    pdk: str = ""
    entries: dict[str, PCellCalibrationEntry] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_results(
        cls,
        pdk: str,
        results: Sequence[PCellIntrospectionResult],
        *,
        preferred_layers: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> "PCellCalibrationCache":
        cache = cls(pdk=str(pdk), metadata=dict(metadata or {}))
        for result in results:
            cache.put(PCellCalibrationEntry.from_introspection(result, preferred_layers=preferred_layers))
        return cache

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellCalibrationCache":
        entries: dict[str, PCellCalibrationEntry] = {}
        raw_entries = data.get("entries", ())
        if isinstance(raw_entries, Mapping):
            raw_iterable = raw_entries.values()
        else:
            raw_iterable = raw_entries
        for item in raw_iterable:
            entry = PCellCalibrationEntry.from_dict(item)
            entries[entry.key] = entry
        return cls(str(data.get("pdk", "")), entries, dict(data.get("metadata", {})))

    @classmethod
    def load_json(cls, path: str | Path) -> "PCellCalibrationCache":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    load = load_json

    def save_json(self, path: str | Path) -> Path:
        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return path_obj

    save = save_json

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdk": self.pdk,
            "entries": [entry.to_dict() for entry in sorted(self.entries.values(), key=lambda item: item.key)],
            "metadata": dict(self.metadata),
        }

    def put(self, entry: PCellCalibrationEntry) -> None:
        self.entries[entry.key] = entry

    def update(self, result: PCellIntrospectionResult, *, preferred_layers: Sequence[str] = ()) -> PCellCalibrationEntry:
        entry = PCellCalibrationEntry.from_introspection(result, preferred_layers=preferred_layers)
        self.put(entry)
        return entry

    def lookup(
        self,
        *,
        logical_name: str,
        pcell: str,
        params: Mapping[str, Any],
        orient: str = "R0",
        allow_nearest: bool = False,
        max_normalized_distance: float = 0.25,
    ) -> PCellCalibrationEntry | None:
        signature = _params_signature(params)
        exact = self.entries.get(_entry_key(logical_name, pcell, signature, orient))
        if exact is not None:
            return exact
        r0 = self.entries.get(_entry_key(logical_name, pcell, signature, "R0"))
        if r0 is not None:
            return r0
        for entry in self.entries.values():
            if entry.logical_name == logical_name and entry.pcell == pcell and entry.params_signature == signature:
                return entry
        wildcard = self._wildcard_params_entry(logical_name, pcell, orient)
        if wildcard is not None:
            return wildcard
        if allow_nearest:
            return self._nearest_entry(
                logical_name,
                pcell,
                signature,
                orient,
                max_normalized_distance=max_normalized_distance,
            )
        return None

    def lookup_instance(
        self,
        instance: PCellInstancePlan,
        *,
        allow_nearest: bool = False,
        max_normalized_distance: float = 0.25,
    ) -> PCellCalibrationEntry | None:
        return self.lookup(
            logical_name=instance.logical_name,
            pcell=f"{instance.lib_name}/{instance.cell_name}/{instance.view_name}",
            params=instance.params,
            orient=instance.orient,
            allow_nearest=allow_nearest,
            max_normalized_distance=max_normalized_distance,
        )

    def _wildcard_params_entry(
        self,
        logical_name: str,
        pcell: str,
        orient: str,
    ) -> PCellCalibrationEntry | None:
        candidates: list[tuple[int, str, PCellCalibrationEntry]] = []
        for entry in self.entries.values():
            if entry.logical_name != logical_name or entry.pcell != pcell:
                continue
            if entry.params_signature:
                continue
            orient_rank = 0 if entry.orient == orient else 1 if entry.orient == "R0" else 2
            candidates.append((orient_rank, entry.key, entry))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    def _nearest_entry(
        self,
        logical_name: str,
        pcell: str,
        params_signature: tuple[tuple[str, str], ...],
        orient: str,
        *,
        max_normalized_distance: float,
    ) -> PCellCalibrationEntry | None:
        best: tuple[float, PCellCalibrationEntry] | None = None
        for entry in self.entries.values():
            if entry.logical_name != logical_name or entry.pcell != pcell:
                continue
            if entry.orient not in {orient, "R0"}:
                continue
            distance = _params_distance(params_signature, entry.params_signature)
            if distance is None or distance > max_normalized_distance:
                continue
            if entry.orient != orient:
                distance += 0.001
            if best is None or distance < best[0]:
                best = (distance, entry)
        if best is None:
            return None
        distance, entry = best
        return _nearest_marked_entry(entry, requested_signature=params_signature, distance=distance)


def load_pcell_calibration_cache(path: str | Path) -> PCellCalibrationCache:
    return PCellCalibrationCache.load_json(path)


def save_pcell_calibration_cache(cache: PCellCalibrationCache, path: str | Path) -> Path:
    return cache.save_json(path)


def _terminal_names_from_result(result: PCellIntrospectionResult) -> tuple[str, ...]:
    names = [term.name for term in result.terms if _is_terminal_name(term.name)]
    names.extend(pin.terminal for pin in result.pins if _is_terminal_name(pin.terminal))
    base_names = set(str(name) for name in names)
    if base_names:
        names.extend(label.text for label in result.labels if str(label.text) in base_names or _is_label_terminal_name(label.text))
    else:
        names.extend(label.text for label in result.labels if _is_label_terminal_name(label.text))
    names.extend(shape.terminal for shape in result.conductive_shapes if shape.terminal)
    names.extend(shape.net for shape in result.conductive_shapes if shape.net)
    return tuple(name for name in dict.fromkeys(str(item) for item in names) if _is_terminal_name(name))


def _legacy_terminal_access(terminal: str, data: Mapping[str, Any]) -> tuple[PCellCalibrationAccess, ...]:
    layer = str(data.get("layer", ""))
    source = str(data.get("source", "calibration"))
    confidence = float(data.get("confidence", 1.0))
    warnings = tuple(str(item) for item in data.get("warnings", ()))
    accesses = []
    for xy in data.get("access_points_um", ()):
        accesses.append(PCellCalibrationAccess(terminal, _point_tuple(xy), layer, source, None, confidence, "legacy cache entry", warnings))
    return tuple(accesses)


def _nearest_marked_entry(
    entry: PCellCalibrationEntry,
    *,
    requested_signature: tuple[tuple[str, str], ...],
    distance: float,
) -> PCellCalibrationEntry:
    warning = f"nearest calibration match used: requested={dict(requested_signature)!r} calibrated={dict(entry.params_signature)!r} distance={distance:.4g}"
    terminals = {
        terminal: tuple(_nearest_marked_access(access, warning) for access in accesses)
        for terminal, accesses in entry.terminals.items()
    }
    metadata = dict(entry.metadata)
    metadata.update(
        {
            "match_policy": "nearest",
            "nearest_distance": distance,
            "calibrated_params_signature": dict(entry.params_signature),
            "requested_params_signature": dict(requested_signature),
        }
    )
    return PCellCalibrationEntry(
        logical_name=entry.logical_name,
        pcell=entry.pcell,
        params_signature=requested_signature,
        orient=entry.orient,
        bbox_um=entry.bbox_um,
        instance_bbox_um=entry.instance_bbox_um,
        terminals=terminals,
        warnings=tuple(dict.fromkeys([*entry.warnings, warning])),
        errors=entry.errors,
        metadata=metadata,
    )


def _nearest_marked_access(access: PCellCalibrationAccess, warning: str) -> PCellCalibrationAccess:
    return PCellCalibrationAccess(
        terminal=access.terminal,
        xy_um=access.xy_um,
        layer=access.layer,
        source=f"nearest_{access.source}" if not access.source.startswith("nearest_") else access.source,
        bbox_um=access.bbox_um,
        confidence=min(access.confidence, 0.8),
        reason=f"nearest calibration: {access.reason}".strip(),
        warnings=tuple(dict.fromkeys([*access.warnings, warning])),
    )


def _params_signature(value: Any) -> tuple[tuple[str, str], ...]:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _canonical_param_value(item)) for key, item in value.items()))
    pairs = []
    for item in value or ():
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append((str(item[0]), _canonical_param_value(item[1])))
    return tuple(sorted(pairs))


def _canonical_param_value(value: Any) -> str:
    """Normalize numerically equivalent CDF params for cache lookup.

    OA introspection artifacts serialize PCell parameters as strings while the
    generated layout plan often carries Python floats.  Values such as
    ``8e-07`` and ``8.000000000000001e-07`` must match exactly; otherwise the
    terminal accessor falls back to nearest-calibration mode and marks the
    access as LVS-unsafe even when the characterized PCell is exact.
    """

    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _canonical_float(value)
    text = str(value)
    try:
        numeric = float(text)
    except (TypeError, ValueError):
        return text
    return _canonical_float(numeric) if isfinite(numeric) else text


def _canonical_float(value: float) -> str:
    return f"{float(value):.12g}"


def _is_terminal_name(value: object) -> bool:
    text = str(value).strip()
    return bool(text) and all(ch.isalnum() or ch == "_" for ch in text) and len(text) <= 16


def _is_label_terminal_name(value: object) -> bool:
    text = str(value).strip()
    return _is_terminal_name(text) and (text.isupper() or text in {"+", "-"})


def _is_routable_access_layer(layer: str) -> bool:
    layer_text = str(layer).upper()
    return (
        layer_text in {"MD", "VD", "OD", "PO", "PDK", "NW"}
        or layer_text.startswith("M")
        or layer_text.startswith("VIA")
    )


def _params_distance(
    requested: tuple[tuple[str, str], ...],
    calibrated: tuple[tuple[str, str], ...],
) -> float | None:
    req = dict(requested)
    cal = dict(calibrated)
    keys = tuple(sorted(set(req) | set(cal)))
    if not keys:
        return 0.0
    distance = 0.0
    comparable = 0
    for key in keys:
        if key not in req or key not in cal:
            distance += 1.0
            comparable += 1
            continue
        req_num = _float_or_none(req[key])
        cal_num = _float_or_none(cal[key])
        if req_num is None or cal_num is None:
            if str(req[key]) != str(cal[key]):
                distance += 1.0
            comparable += 1
            continue
        denom = max(abs(req_num), abs(cal_num), 1e-18)
        distance += abs(req_num - cal_num) / denom
        comparable += 1
    if comparable == 0:
        return None
    return distance / comparable


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _entry_key(logical_name: str, pcell: str, params_signature: Sequence[tuple[str, str]], orient: str) -> str:
    params = ",".join(f"{key}={value}" for key, value in sorted(params_signature))
    return f"{logical_name}|{pcell}|{params}|{orient}"


def _sort_accesses(accesses: Sequence[PCellCalibrationAccess], preferred_layers: Sequence[str]) -> tuple[PCellCalibrationAccess, ...]:
    layer_rank = {str(layer): idx for idx, layer in enumerate(preferred_layers)}
    return tuple(sorted(accesses, key=lambda item: (-item.confidence, layer_rank.get(item.layer, 10_000), item.xy_um)))


def _bbox_tuple(value: Any) -> BBox:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"bbox must be a 4-tuple, got {value!r}")
    return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))


def _optional_bbox(value: Any) -> BBox | None:
    if value is None:
        return None
    return _bbox_tuple(value)


def _point_tuple(value: Any) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"point must be a 2-tuple, got {value!r}")
    return (float(value[0]), float(value[1]))
