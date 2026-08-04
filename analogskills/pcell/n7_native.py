"""N7-native PCell calibration helpers for batch Virtuoso runs."""
from __future__ import annotations

import json
from json import dumps as json_dumps
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from analogskills.eda import EdaCommand, make_virtuoso_batch_command, run_eda_command
from analogskills.eda.pcell_introspection import _skill_introspection_expr, load_pcell_introspection_json
from analogskills.pcell.calibration import PCellCalibrationCache, PCellCalibrationEntry, load_pcell_calibration_cache
from analogskills.pcell.calibration_run import (
    PCellCalibrationManifest,
    PCellCalibrationRunRecord,
    PCellCalibrationRunResult,
    PCellCalibrationTarget,
    analyze_pcell_calibration_coverage,
)
from analogskills.pdk import PdkConfig


def default_n7_calibration_dir(project_dir: str | Path) -> Path:
    return Path(project_dir).resolve() / "pcell_calibration_n7"


def n7_calibration_cache_path(project_dir: str | Path) -> Path:
    return default_n7_calibration_dir(project_dir) / "pcell_access.json"


def load_n7_calibration_cache(project_dir: str | Path) -> PCellCalibrationCache | None:
    path = n7_calibration_cache_path(project_dir)
    if not path.exists():
        return None
    return load_pcell_calibration_cache(path)


def build_n7_calibration_manifest(
    pdk: PdkConfig,
    *,
    mos_param_sets: Sequence[Mapping[str, Any]] = (),
    bjt_param_sets: Sequence[Mapping[str, Any]] = (),
    resistor_param_sets: Sequence[Mapping[str, Any]] = (),
    orientations: Sequence[str] = ("R0",),
) -> PCellCalibrationManifest:
    targets: list[PCellCalibrationTarget] = []
    for logical_name, param_sets in (
        ("nmos", mos_param_sets),
        ("pmos", mos_param_sets),
        ("bjt", bjt_param_sets),
        ("resistor", resistor_param_sets),
    ):
        if not param_sets:
            continue
        template = pdk.pcell_template_for(logical_name)
        for orient in orientations:
            for params in param_sets:
                targets.append(
                    PCellCalibrationTarget(
                        logical_name=logical_name,
                        lib_name=str(template.resolved_layout_lib_name()),
                        cell_name=str(template.resolved_layout_cell_name()),
                        view_name=str(template.resolved_layout_view_name()),
                        params=dict(params),
                        orient=str(orient),
                    )
                )
    return PCellCalibrationManifest(
        pdk=pdk.name,
        targets=tuple(targets),
        metadata={
            "source": "analogskills.pcell.n7_native.build_n7_calibration_manifest",
            "orientations": list(orientations),
        },
    )


def ensure_n7_calibration_cache(
    *,
    pdk: PdkConfig,
    project_dir: str | Path,
    virtuoso_binary: str,
    batch_env: Mapping[str, str],
    batch_cwd: str | Path | None,
    work_lib: str,
    work_lib_path: str | Path,
    tech_lib: str,
    mos_param_sets: Sequence[Mapping[str, Any]] = (),
    bjt_param_sets: Sequence[Mapping[str, Any]] = (),
    resistor_param_sets: Sequence[Mapping[str, Any]] = (),
    force: bool = False,
) -> PCellCalibrationCache:
    out_dir = default_n7_calibration_dir(project_dir)
    cache_path = n7_calibration_cache_path(project_dir)
    manifest = build_n7_calibration_manifest(
        pdk,
        mos_param_sets=mos_param_sets,
        bjt_param_sets=bjt_param_sets,
        resistor_param_sets=resistor_param_sets,
    )
    if cache_path.exists() and not force:
        existing_cache = load_pcell_calibration_cache(cache_path)
        missing_targets = _missing_calibration_targets(existing_cache, manifest)
        if not missing_targets:
            return existing_cache
        result = run_n7_calibration_manifest_batch(
            PCellCalibrationManifest(
                manifest.pdk,
                tuple(missing_targets),
                metadata={
                    **dict(manifest.metadata),
                    "incremental": True,
                    "requested_target_count": len(manifest.targets),
                    "missing_target_count": len(missing_targets),
                },
            ),
            out_dir=out_dir,
            virtuoso_binary=virtuoso_binary,
            batch_env=batch_env,
            batch_cwd=batch_cwd,
            work_lib=work_lib,
            work_lib_path=work_lib_path,
            tech_lib=tech_lib,
            preferred_layers=tuple(pdk.preferred_signal_layers or pdk.layer_map.metals),
        )
        merged_cache = PCellCalibrationCache(
            existing_cache.pdk or result.cache.pdk,
            entries=dict(existing_cache.entries),
            metadata={
                **dict(existing_cache.metadata),
                "incremental_updates": [
                    *tuple(dict(item) for item in existing_cache.metadata.get("incremental_updates", ())),
                    {
                        "requested_target_count": len(manifest.targets),
                        "missing_target_count": len(missing_targets),
                        "ok": bool(result.ok),
                        "failed_targets": [
                            record.target.target_key
                            for record in result.records
                            if not record.ok
                        ],
                    },
                ],
            },
        )
        for entry in result.cache.entries.values():
            merged_cache.put(entry)
        merged_cache.save_json(cache_path)
        return merged_cache
    result = run_n7_calibration_manifest_batch(
        manifest,
        out_dir=out_dir,
        virtuoso_binary=virtuoso_binary,
        batch_env=batch_env,
        batch_cwd=batch_cwd,
        work_lib=work_lib,
        work_lib_path=work_lib_path,
        tech_lib=tech_lib,
        preferred_layers=tuple(pdk.preferred_signal_layers or pdk.layer_map.metals),
    )
    if not result.ok:
        failed = [record for record in result.records if not record.ok]
        details = "; ".join(
            f"{record.target.target_key}: {', '.join(record.errors) or 'unknown error'}"
            for record in failed[:8]
        )
        raise RuntimeError(f"N7 PCell calibration failed for {len(failed)} targets: {details}")
    return result.cache


def _missing_calibration_targets(
    cache: PCellCalibrationCache,
    manifest: PCellCalibrationManifest,
) -> tuple[PCellCalibrationTarget, ...]:
    missing: list[PCellCalibrationTarget] = []
    for target in manifest.targets:
        entry = cache.lookup(
            logical_name=target.logical_name,
            pcell=target.pcell_key,
            params=target.params,
            orient=target.orient,
        )
        if entry is None:
            missing.append(target)
    return tuple(missing)


def run_n7_calibration_manifest_batch(
    manifest: PCellCalibrationManifest,
    *,
    out_dir: str | Path,
    virtuoso_binary: str,
    batch_env: Mapping[str, str],
    batch_cwd: str | Path | None,
    work_lib: str,
    work_lib_path: str | Path,
    tech_lib: str,
    preferred_layers: Sequence[str] = (),
) -> PCellCalibrationRunResult:
    out_path = Path(out_dir).resolve()
    artifacts_dir = out_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    manifest.save_json(out_path / "manifest.json")

    skill_script = out_path / "run_pcell_calibration.il"
    log_path = out_path / "virtuoso_calibration.log"
    batch_script = out_path / "run_pcell_calibration_batch.il"
    skill_text = _build_batch_calibration_skill(
        manifest,
        artifacts_dir=artifacts_dir,
        work_lib=work_lib,
        work_lib_path=Path(work_lib_path).resolve(),
        tech_lib=tech_lib,
    )
    skill_script.write_text(skill_text, encoding="utf-8")
    batch_script.write_text(skill_text + "\nexit()\n", encoding="utf-8")

    cmd = make_virtuoso_batch_command(batch_script, binary=virtuoso_binary)
    cmd = EdaCommand(
        cmd.command,
        cwd=batch_cwd or cmd.cwd,
        timeout_s=1800.0,
        env=dict(batch_env),
    )
    run = run_eda_command(cmd, check=False)
    log_path.write_text((run.stdout or "") + ("\n" if run.stdout else "") + (run.stderr or ""), encoding="utf-8")
    if not run.ok:
        raise RuntimeError(
            f"Virtuoso batch calibration rc={run.returncode}; see {log_path}"
        )

    cache = PCellCalibrationCache(
        manifest.pdk,
        metadata={
            "manifest": manifest.to_dict(),
            "backend": "VirtuosoBatchReplay",
            "virtuoso_binary": str(virtuoso_binary),
        },
    )
    records: list[PCellCalibrationRunRecord] = []
    results = []
    for index, target in enumerate(manifest.targets):
        artifact_path = artifacts_dir / f"{index:04d}_{_safe_slug(target.target_key)}.json"
        if not artifact_path.exists():
            records.append(
                PCellCalibrationRunRecord(
                    target=target,
                    artifact_path=str(artifact_path),
                    ok=False,
                    errors=("missing introspection artifact",),
                )
            )
            continue
        try:
            result = load_pcell_introspection_json(artifact_path)
        except Exception as exc:
            records.append(
                PCellCalibrationRunRecord(
                    target=target,
                    artifact_path=str(artifact_path),
                    ok=False,
                    errors=(f"failed to parse introspection artifact: {exc}",),
                )
            )
            continue
        results.append(result)
        if result.ok:
            cache.put(PCellCalibrationEntry.from_introspection(result, preferred_layers=preferred_layers))
        records.append(
            PCellCalibrationRunRecord(
                target=target,
                artifact_path=str(artifact_path),
                ok=result.ok,
                terminals=tuple(term.name for term in result.terms),
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


def _build_batch_calibration_skill(
    manifest: PCellCalibrationManifest,
    *,
    artifacts_dir: Path,
    work_lib: str,
    work_lib_path: Path,
    tech_lib: str,
) -> str:
    lines = [
        f'libObj = ddGetObj("{work_lib}")',
        f'unless(libObj libObj = ddCreateLib("{work_lib}" "{work_lib_path}"))',
        f'when(libObj techBindTechFile(libObj "{tech_lib}"))',
    ]
    for index, target in enumerate(manifest.targets):
        out_file = artifacts_dir / f"{index:04d}_{_safe_slug(target.target_key)}.json"
        expr = _skill_introspection_expr(
            target.to_request(),
            Path(__file__).resolve().parents[1] / "eda" / "pcell_introspect.il",
            str(out_file),
        )
        expr = expr.replace(f"__FILE:{str(out_file)}__", json_dumps(str(out_file)))
        lines.append(expr)
    return "\n".join(lines) + "\n"


def _safe_slug(text: str, *, max_length: int = 96) -> str:
    """Make a stable artifact filename component without exceeding NAME_MAX.

    A native calibration target can contain many CDF overrides.  Its complete
    parameter signature remains in the JSON request; artifact filenames only
    need a readable prefix plus a collision-resistant stable suffix.
    """

    slug = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in str(text))
    if len(slug) <= max_length:
        return slug
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:16]
    return f"{slug[:max_length - len(digest) - 1]}_{digest}"
