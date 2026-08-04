"""Batch PCell OA introspection manifests and coverage reports."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

from analogskills.eda.pcell_introspection import PCellIntrospectionRequest, PCellIntrospectionResult, PCellIntrospector, VirtuosoSkillPCellIntrospectionBackend
from analogskills.eda.skill_server import VirtuosoSkillClient
from analogskills.pcell.calibration import PCellCalibrationCache, PCellCalibrationEntry
from analogskills.pdk import PCellTemplate, PdkConfig


@dataclass(frozen=True)
class PCellCalibrationTarget:
    logical_name: str
    lib_name: str
    cell_name: str
    view_name: str = "layout"
    params: dict[str, Any] = field(default_factory=dict)
    orient: str = "R0"
    instance_name: str = "DUT"
    calibration_lib: str = "analogskills_pcell_calib"
    calibration_cell: str = "pcell_introspect"

    @property
    def pcell_key(self) -> str:
        return f"{self.lib_name}/{self.cell_name}/{self.view_name}"

    @property
    def target_key(self) -> str:
        return f"{self.logical_name}|{self.pcell_key}|{_params_slug(self.params)}|{self.orient}"

    def to_request(self) -> PCellIntrospectionRequest:
        return PCellIntrospectionRequest(
            self.logical_name,
            self.lib_name,
            self.cell_name,
            self.view_name,
            params=dict(self.params),
            orient=self.orient,
            instance_name=self.instance_name,
            calibration_lib=self.calibration_lib,
            calibration_cell=self.calibration_cell,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellCalibrationTarget":
        return cls(
            logical_name=str(data.get("logical_name", "")),
            lib_name=str(data.get("lib_name", "")),
            cell_name=str(data.get("cell_name", "")),
            view_name=str(data.get("view_name", "layout")),
            params=dict(data.get("params", {})),
            orient=str(data.get("orient", "R0")),
            instance_name=str(data.get("instance_name", "DUT")),
            calibration_lib=str(data.get("calibration_lib", "analogskills_pcell_calib")),
            calibration_cell=str(data.get("calibration_cell", "pcell_introspect")),
        )


@dataclass(frozen=True)
class PCellCalibrationManifest:
    pdk: str
    targets: tuple[PCellCalibrationTarget, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdk": self.pdk,
            "targets": [target.to_dict() for target in self.targets],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PCellCalibrationManifest":
        return cls(
            pdk=str(data.get("pdk", "")),
            targets=tuple(PCellCalibrationTarget.from_dict(target) for target in data.get("targets", ())),
            metadata=dict(data.get("metadata", {})),
        )

    @classmethod
    def load_json(cls, path: str | Path) -> "PCellCalibrationManifest":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    load = load_json

    def save_json(self, path: str | Path) -> Path:
        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        path_obj.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path_obj

    save = save_json


@dataclass(frozen=True)
class PCellCalibrationRunRecord:
    target: PCellCalibrationTarget
    artifact_path: str
    ok: bool
    terminals: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.to_dict(),
            "artifact_path": self.artifact_path,
            "ok": self.ok,
            "terminals": list(self.terminals),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class PCellCalibrationRunResult:
    manifest: PCellCalibrationManifest
    records: tuple[PCellCalibrationRunRecord, ...]
    cache: PCellCalibrationCache
    coverage: dict[str, Any]

    @property
    def ok(self) -> bool:
        return all(record.ok for record in self.records)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "records": [record.to_dict() for record in self.records],
            "coverage": self.coverage,
            "ok": self.ok,
        }


def pcell_calibration_preflight(
    *,
    port_file: str | Path = "skill_server_port.txt",
    virtuoso_binary: str = "virtuoso",
    cds_lib: str | Path = "cds.lib",
    cdsinit: str | Path = ".cdsinit",
    pdk_libs: Sequence[str] = ("tsmcN28",),
) -> dict[str, Any]:
    """Return actionable readiness checks for a real Virtuoso-backed run."""

    port_path = Path(port_file)
    cds_lib_path = Path(cds_lib)
    cdsinit_path = Path(cdsinit)
    issues: list[str] = []
    binary_path = shutil.which(virtuoso_binary)
    if binary_path is None:
        issues.append(f"Virtuoso binary {virtuoso_binary!r} was not found on PATH")
    if not cds_lib_path.exists():
        issues.append(f"cds.lib not found at {cds_lib_path}")
        cds_text = ""
    else:
        cds_text = cds_lib_path.read_text(encoding="utf-8")
    for lib_name in pdk_libs:
        if cds_text and f"DEFINE {lib_name} " not in cds_text:
            issues.append(f"PDK library {lib_name!r} is not defined in {cds_lib_path}")
    if not cdsinit_path.exists():
        issues.append(f".cdsinit not found at {cdsinit_path}")
    elif not _loads_compatible_skill_server(cdsinit_path.read_text(encoding="utf-8")):
        issues.append(f"{cdsinit_path} does not appear to load a analogskills-compatible SKILL server")
    if not port_path.exists():
        issues.append(f"SKILL server port file not found at {port_path}; start Virtuoso and load analogskills/eda/skill_server.il")
    return {
        "passed": not issues,
        "issues": issues,
        "virtuoso_binary": binary_path or "",
        "port_file": str(port_path),
        "cds_lib": str(cds_lib_path),
        "cdsinit": str(cdsinit_path),
        "pdk_libs": list(pdk_libs),
    }


def run_pcell_calibration_manifest_via_skill_server(
    manifest: PCellCalibrationManifest,
    out_dir: str | Path,
    *,
    port_file: str | Path = "skill_server_port.txt",
    timeout_ms: int | None = 120000,
    host: str = "127.0.0.1",
    skill_script: str | Path | None = None,
    preferred_layers: Sequence[str] = (),
    cache_metadata: Mapping[str, Any] | None = None,
    client: VirtuosoSkillClient | None = None,
    shutdown_server: bool = False,
) -> PCellCalibrationRunResult:
    """Run a calibration manifest through a live Virtuoso SKILL server."""

    owns_client = client is None
    skill_client = client or VirtuosoSkillClient(port_file=port_file, host=host, timeout_ms=timeout_ms)
    try:
        backend = VirtuosoSkillPCellIntrospectionBackend(skill_client, skill_script=skill_script)
        return run_pcell_calibration_manifest(
            manifest,
            PCellIntrospector(backend),
            out_dir,
            preferred_layers=preferred_layers,
            cache_metadata={
                **dict(cache_metadata or {}),
                "backend": "VirtuosoSkillPCellIntrospectionBackend",
                "port_file": str(port_file),
                "host": host,
            },
        )
    finally:
        if owns_client:
            if shutdown_server:
                skill_client.close()
            else:
                skill_client.disconnect()


def build_default_pcell_calibration_manifest(
    pdk: PdkConfig,
    *,
    logical_names: Sequence[str] | None = None,
    mos_nf: Sequence[int] = (1, 2, 4, 8),
    mos_width_um: Sequence[float] = (0.36, 0.6, 1.0, 2.0),
    mos_length_um: Sequence[float] = (0.03, 0.06, 0.18),
    mos_sim_m: Sequence[int] = (1, 2),
    passive_defaults: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    orientations: Sequence[str] = ("R0",),
) -> PCellCalibrationManifest:
    """Create a reviewable starter manifest from PDK PCell templates."""

    selected = tuple(logical_names) if logical_names is not None else tuple(sorted(pdk.pcell_templates))
    passive_defaults = dict(passive_defaults or {})
    targets: list[PCellCalibrationTarget] = []
    for logical_name in selected:
        template = pdk.pcell_template_for(logical_name)
        param_sets = _default_param_sets_for_template(
            logical_name,
            template,
            mos_nf=mos_nf,
            mos_width_um=mos_width_um,
            mos_length_um=mos_length_um,
            mos_sim_m=mos_sim_m,
            passive_defaults=passive_defaults,
        )
        for orient in orientations:
            for params in param_sets:
                targets.append(_target_from_template(template, params, orient=orient))
    return PCellCalibrationManifest(
        pdk=pdk.name,
        targets=tuple(targets),
        metadata={
            "source": "build_default_pcell_calibration_manifest",
            "logical_names": list(selected),
            "orientations": list(orientations),
        },
    )


def run_pcell_calibration_manifest(
    manifest: PCellCalibrationManifest,
    introspector: PCellIntrospector,
    out_dir: str | Path,
    *,
    preferred_layers: Sequence[str] = (),
    cache_metadata: Mapping[str, Any] | None = None,
) -> PCellCalibrationRunResult:
    """Run all manifest targets and write per-target artifacts plus a cache."""

    out_path = Path(out_dir)
    artifacts_dir = out_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    cache = PCellCalibrationCache(manifest.pdk, metadata={**dict(cache_metadata or {}), "manifest": manifest.to_dict()})
    records: list[PCellCalibrationRunRecord] = []
    results: list[PCellIntrospectionResult] = []
    for index, target in enumerate(manifest.targets):
        result = _validate_introspection_result(introspector.run(target.to_request()))
        artifact_path = artifacts_dir / f"{index:04d}_{_safe_slug(target.target_key)}.json"
        result.save_json(artifact_path)
        if result.ok:
            cache.put(PCellCalibrationEntry.from_introspection(result, preferred_layers=preferred_layers))
        results.append(result)
        records.append(
            PCellCalibrationRunRecord(
                target,
                str(artifact_path),
                result.ok,
                terminals=_terminal_names(result),
                warnings=result.warnings,
                errors=result.errors,
            )
        )
    cache.save_json(out_path / "pcell_access.json")
    coverage = analyze_pcell_calibration_coverage(manifest, results, cache=cache)
    (out_path / "coverage.json").write_text(json.dumps(coverage, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    run_result = PCellCalibrationRunResult(manifest, tuple(records), cache, coverage)
    (out_path / "run_summary.json").write_text(json.dumps(run_result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_result


def analyze_pcell_calibration_coverage(
    manifest: PCellCalibrationManifest,
    results: Sequence[PCellIntrospectionResult] | None = None,
    *,
    cache: PCellCalibrationCache | None = None,
) -> dict[str, Any]:
    """Summarize calibration readiness by logical PCell and terminal."""

    by_key = {_request_key(result.request): result for result in results or ()}
    summary: dict[str, dict[str, Any]] = {}
    missing_targets: list[str] = []
    errored_targets: list[str] = []
    for target in manifest.targets:
        row = summary.setdefault(
            target.logical_name,
            {
                "target_count": 0,
                "ok_count": 0,
                "error_count": 0,
                "terminals": {},
                "sources": {},
                "fallback_or_low_confidence": 0,
            },
        )
        row["target_count"] += 1
        result = by_key.get(target.target_key)
        if result is None and cache is not None:
            entry = cache.lookup(logical_name=target.logical_name, pcell=target.pcell_key, params=target.params, orient=target.orient)
            if entry is not None:
                _accumulate_cache_entry_coverage(row, entry)
                row["ok_count"] += 1
                continue
        if result is None:
            missing_targets.append(target.target_key)
            continue
        if not result.ok:
            row["error_count"] += 1
            errored_targets.append(target.target_key)
            continue
        row["ok_count"] += 1
        _accumulate_result_coverage(row, result)
    for row in summary.values():
        row["terminals"] = {key: value for key, value in sorted(row["terminals"].items())}
        row["sources"] = {key: value for key, value in sorted(row["sources"].items())}
    total_targets = len(manifest.targets)
    ok_targets = sum(int(row["ok_count"]) for row in summary.values())
    return {
        "pdk": manifest.pdk,
        "passed": not missing_targets and not errored_targets and total_targets > 0 and ok_targets == total_targets,
        "target_count": total_targets,
        "ok_count": ok_targets,
        "missing_targets": missing_targets,
        "errored_targets": errored_targets,
        "by_logical_name": summary,
    }


def load_pcell_calibration_manifest(path: str | Path) -> PCellCalibrationManifest:
    return PCellCalibrationManifest.load_json(path)


def save_pcell_calibration_manifest(manifest: PCellCalibrationManifest, path: str | Path) -> Path:
    return manifest.save_json(path)


def _default_param_sets_for_template(
    logical_name: str,
    template: PCellTemplate,
    *,
    mos_nf: Sequence[int],
    mos_width_um: Sequence[float],
    mos_length_um: Sequence[float],
    mos_sim_m: Sequence[int],
    passive_defaults: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], ...]:
    if logical_name in {"nmos", "pmos"}:
        rows = []
        for nf in mos_nf:
            for width_um in mos_width_um:
                for length_um in mos_length_um:
                    for sim_m in mos_sim_m:
                        logical = {"W": float(width_um) * 1e-6, "L": float(length_um) * 1e-6, "nf": int(nf), "m": int(sim_m)}
                        rows.append(template.map_parameters(logical))
        return tuple(_dedupe_param_sets(rows))
    if logical_name in passive_defaults:
        return tuple(_dedupe_param_sets(dict(row) for row in passive_defaults[logical_name]))
    defaults = dict(template.default_params)
    if logical_name == "resistor":
        defaults.update({"R": 1000.0, "W": 1e-6})
    elif logical_name == "capacitor":
        defaults.update({"C": 1e-12})
    elif logical_name == "bjt":
        defaults.update({"M": 1})
    return (template.map_parameters(defaults),)


def _target_from_template(template: PCellTemplate, params: Mapping[str, Any], *, orient: str) -> PCellCalibrationTarget:
    return PCellCalibrationTarget(
        template.logical_name,
        template.lib_name,
        template.cell_name,
        template.view_name,
        params=dict(params),
        orient=str(orient),
    )


def _accumulate_result_coverage(row: dict[str, Any], result: PCellIntrospectionResult) -> None:
    for terminal in _terminal_names(result):
        candidates = tuple(candidate for candidate in result.terminal_access_candidates(terminal) if _is_routable_access_layer(candidate.layer))
        if not candidates:
            continue
        _accumulate_terminal(row, terminal, candidates[0].source, candidates[0].confidence)


def _accumulate_cache_entry_coverage(row: dict[str, Any], entry: PCellCalibrationEntry) -> None:
    for terminal, candidates in entry.terminals.items():
        if not candidates:
            continue
        _accumulate_terminal(row, terminal, candidates[0].source, candidates[0].confidence)


def _accumulate_terminal(row: dict[str, Any], terminal: str, source: str, confidence: float) -> None:
    terminals = row["terminals"]
    terminal_row = terminals.setdefault(str(terminal), {"count": 0, "sources": {}})
    terminal_row["count"] += 1
    terminal_row["sources"][str(source)] = terminal_row["sources"].get(str(source), 0) + 1
    sources = row["sources"]
    sources[str(source)] = sources.get(str(source), 0) + 1
    if str(source).startswith("pdk_") or "fallback" in str(source) or float(confidence) < 0.5:
        row["fallback_or_low_confidence"] += 1


def _terminal_names(result: PCellIntrospectionResult) -> tuple[str, ...]:
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


def _validate_introspection_result(result: PCellIntrospectionResult) -> PCellIntrospectionResult:
    errors = list(result.errors)
    warnings = list(result.warnings)
    if not errors:
        if result.master_bbox_um is None:
            errors.append("PCell introspection missing master_bbox_um")
        if result.instance_bbox_um is None:
            errors.append("PCell introspection missing instance_bbox_um")
        terminal_names = _terminal_names(result)
        if not terminal_names:
            errors.append("PCell introspection produced no terminal metadata")
        else:
            missing_access = tuple(terminal for terminal in terminal_names if not result.terminal_access_candidates(terminal))
            if missing_access:
                errors.append(f"PCell introspection produced no access candidates for terminals: {', '.join(missing_access)}")
            non_routable_access = tuple(
                terminal
                for terminal in terminal_names
                if result.terminal_access_candidates(terminal)
                and not any(_is_routable_access_layer(candidate.layer) for candidate in result.terminal_access_candidates(terminal))
            )
            if non_routable_access:
                warnings.append(f"PCell introspection produced no routable access layers for terminals: {', '.join(non_routable_access)}")
            routable_terminals = tuple(
                terminal
                for terminal in terminal_names
                if any(_is_routable_access_layer(candidate.layer) for candidate in result.terminal_access_candidates(terminal))
            )
            if not routable_terminals:
                errors.append("PCell introspection produced no routable terminal access")
    errors_tuple = tuple(dict.fromkeys(errors))
    warnings_tuple = tuple(dict.fromkeys(warnings))
    if errors_tuple == result.errors and warnings_tuple == result.warnings:
        return result
    return replace(result, errors=errors_tuple, warnings=warnings_tuple)


def _dedupe_param_sets(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    deduped: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    for row in rows:
        data = dict(row)
        key = tuple(sorted((str(item_key), str(item_value)) for item_key, item_value in data.items()))
        deduped[key] = data
    return tuple(deduped.values())


def _request_key(request: PCellIntrospectionRequest) -> str:
    return f"{request.logical_name}|{request.pcell_key}|{_params_slug(request.params)}|{request.orient}"


def _params_slug(params: Mapping[str, Any]) -> str:
    if not params:
        return "default"
    return ",".join(f"{key}={value}" for key, value in sorted((str(key), str(value)) for key, value in params.items()))


def _safe_slug(value: str, *, max_length: int = 96) -> str:
    """Return a deterministic, filesystem-safe artifact basename."""

    slug = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value))
    if len(slug) <= max_length:
        return slug
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:16]
    return f"{slug[:max_length - len(digest) - 1]}_{digest}"


def _loads_compatible_skill_server(cdsinit_text: str) -> bool:
    return "analogskills/eda/skill_server.il" in cdsinit_text or "skills/virtuoso/server.il" in cdsinit_text or "skill_server.il" in cdsinit_text


def _is_routable_access_layer(layer: str) -> bool:
    layer_text = str(layer).upper()
    return layer_text in {"PO", "OD", "CO"} or layer_text.startswith("M") or layer_text.startswith("VIA")


def _is_terminal_name(value: object) -> bool:
    text = str(value).strip()
    return bool(text) and all(ch.isalnum() or ch == "_" for ch in text) and len(text) <= 16


def _is_label_terminal_name(value: object) -> bool:
    text = str(value).strip()
    return _is_terminal_name(text) and (text.isupper() or text in {"+", "-"})
