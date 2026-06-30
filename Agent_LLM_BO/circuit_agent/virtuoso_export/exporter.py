"""High-level Virtuoso export helpers."""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import DEFAULT_DEVICE_MAP, DeviceMap, DeviceMapEntry
from .parser import parse_netlist
from .skill_writer import write_skill


def export_from_results(
    results_path: str | Path,
    lib_name: str,
    cell_name: str | None = None,
    out_path: str | Path | None = None,
    device_map_path: str | Path | None = None,
    virtuoso_workdir: str | Path | None = None,
    tech_lib: str = "tsmcN28",
    run_virtuoso: bool = False,
    virtuoso_bin: str = "virtuoso",
    include_cds_libs: list[str | Path] | None = None,
    pdk_lib_path: str | Path | None = None,
    cds_log_path: str | Path | None = None,
) -> dict[str, Any]:
    """Export a Virtuoso SKILL script from an optimizer results.json file."""
    results_path = Path(results_path)
    result_data = json.loads(results_path.read_text(encoding="utf-8"))

    netlist_path, export_source = select_export_netlist(results_path, result_data)

    if cell_name is None:
        cell_name = _default_cell_name(result_data, netlist_path, export_source)

    if out_path is None:
        out_path = results_path.parent / "virtuoso" / "import_schematic.il"

    return export_netlist(
        netlist_path=netlist_path,
        lib_name=lib_name,
        cell_name=cell_name,
        out_path=out_path,
        device_map_path=device_map_path,
        results_path=results_path,
        export_source=export_source,
        virtuoso_workdir=virtuoso_workdir,
        tech_lib=tech_lib,
        run_virtuoso=run_virtuoso,
        virtuoso_bin=virtuoso_bin,
        include_cds_libs=include_cds_libs,
        pdk_lib_path=pdk_lib_path,
        cds_log_path=cds_log_path,
    )


def export_netlist(
    netlist_path: str | Path,
    lib_name: str,
    cell_name: str,
    out_path: str | Path,
    device_map_path: str | Path | None = None,
    results_path: str | Path | None = None,
    export_source: str = "explicit_netlist",
    virtuoso_workdir: str | Path | None = None,
    tech_lib: str = "tsmcN28",
    run_virtuoso: bool = False,
    virtuoso_bin: str = "virtuoso",
    include_cds_libs: list[str | Path] | None = None,
    pdk_lib_path: str | Path | None = None,
    cds_log_path: str | Path | None = None,
) -> dict[str, Any]:
    """Export a final rendered DUT netlist to a Virtuoso SKILL script."""
    netlist_path = Path(netlist_path)
    out_path = Path(out_path)
    device_map = load_device_map(device_map_path)
    ir = parse_netlist(netlist_path)
    skill = write_skill(ir, device_map, lib_name=lib_name, cell_name=cell_name)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(skill, encoding="utf-8")

    report = {
        "skill_file": str(out_path),
        "results_file": str(results_path) if results_path else None,
        "netlist_file": str(netlist_path),
        "export_source": export_source,
        "target_lib": lib_name,
        "target_cell": cell_name,
        "target_view": "schematic",
        "subckt_name": ir.subckt_name,
        "ports": ir.ports,
        "nets": len(ir.nets),
        "instances": len(ir.instances),
        "models": sorted({inst.model for inst in ir.instances}),
        "device_map_file": str(device_map_path) if device_map_path else None,
    }
    report_path = out_path.parent / "export_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if virtuoso_workdir is not None or run_virtuoso:
        workspace_report = prepare_virtuoso_workspace(
            skill_path=out_path,
            lib_name=lib_name,
            cell_name=cell_name,
            tech_lib=tech_lib,
            workdir=virtuoso_workdir or _default_virtuoso_workdir(results_path, cell_name),
            run_virtuoso=run_virtuoso,
            virtuoso_bin=virtuoso_bin,
            include_cds_libs=include_cds_libs,
            pdk_lib_path=pdk_lib_path,
            cds_log_path=cds_log_path,
        )
        report.update(workspace_report)
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return report


def prepare_virtuoso_workspace(
    skill_path: str | Path,
    lib_name: str,
    cell_name: str,
    tech_lib: str = "tsmcN28",
    workdir: str | Path | None = None,
    run_virtuoso: bool = False,
    virtuoso_bin: str = "virtuoso",
    include_cds_libs: list[str | Path] | None = None,
    pdk_lib_path: str | Path | None = None,
    cds_log_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create a Cadence working directory and optionally run Virtuoso batch import."""
    skill_path = Path(skill_path).resolve()
    if workdir is None:
        workdir = _default_virtuoso_workdir(None, cell_name)
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    workspace_skill = workdir / "import_schematic.il"
    if skill_path != workspace_skill:
        shutil.copy2(skill_path, workspace_skill)

    cds_lib = workdir / "cds.lib"
    run_script = workdir / "run_import.il"
    run_log = workdir / "virtuoso_import.log"
    cds_log = Path(cds_log_path).resolve() if cds_log_path else workdir / "CDS.log"
    readme = workdir / "README_import.md"

    cds_lib.write_text(
        _render_cds_lib(
            lib_name=lib_name,
            tech_lib=tech_lib,
            include_cds_libs=include_cds_libs,
            pdk_lib_path=pdk_lib_path,
        ),
        encoding="utf-8",
    )
    run_script.write_text(
        _render_run_import_skill(
            lib_name=lib_name,
            cell_name=cell_name,
            tech_lib=tech_lib,
            import_skill=workspace_skill.name,
        ),
        encoding="utf-8",
    )
    readme.write_text(
        _render_workspace_readme(
            lib_name=lib_name,
            cell_name=cell_name,
            tech_lib=tech_lib,
            run_script=run_script.name,
            include_cds_libs=include_cds_libs,
            pdk_lib_path=pdk_lib_path,
            cds_log=cds_log,
        ),
        encoding="utf-8",
    )

    report: dict[str, Any] = {
        "virtuoso_workdir": str(workdir),
        "tech_lib": tech_lib,
        "run_script": str(run_script),
        "run_log": str(run_log),
        "cds_log": str(cds_log),
        "cds_lib": str(cds_lib),
        "workspace_skill_file": str(workspace_skill),
        "include_cds_libs": [
            str(Path(path).expanduser()) for path in (include_cds_libs or [])
        ],
        "pdk_lib_path": str(Path(pdk_lib_path).expanduser()) if pdk_lib_path else None,
        "virtuoso_ran": False,
    }
    if run_virtuoso:
        command = [virtuoso_bin, "-nograph", "-replay", str(run_script)]
        env = os.environ.copy()
        env["CDS_LOG"] = str(cds_log)
        completed = subprocess.run(
            command,
            cwd=workdir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        run_log.write_text(completed.stdout or "", encoding="utf-8")
        report.update(
            {
                "virtuoso_ran": True,
                "virtuoso_command": command,
                "virtuoso_returncode": completed.returncode,
            }
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Virtuoso import failed with code {completed.returncode}. "
                f"See {run_log}"
            )
    return report


def select_export_netlist(
    results_path: str | Path,
    result_data: dict[str, Any] | None = None,
) -> tuple[Path, str]:
    """Select the final netlist for Virtuoso export.

    Priority:
    1. Review candidate that satisfies the original BO targets.
    2. BO best netlist from ``results.json["netlist_file"]``.

    This keeps BO as the default export source, but lets a successful
    post-BO Agent review candidate replace it automatically.
    """
    results_path = Path(results_path)
    if result_data is None:
        result_data = json.loads(results_path.read_text(encoding="utf-8"))

    bo_netlist = _resolve_bo_netlist(results_path, result_data)
    targets = _load_targets(results_path)
    review_netlist = _select_passing_review_candidate(results_path.parent, targets)
    if review_netlist is not None:
        return review_netlist, "agent_review"

    if bool(result_data.get("all_targets_met")):
        return bo_netlist, "bo_best"
    return bo_netlist, "bo_best_unmet"


def load_device_map(path: str | Path | None = None) -> DeviceMap:
    """Load a device map JSON file and merge it over the defaults."""
    device_map = dict(DEFAULT_DEVICE_MAP)
    if not path:
        return device_map

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key, value in data.items():
        if not isinstance(value, dict):
            raise ValueError(f"Device map entry for {key!r} must be an object")
        device_map[key] = DeviceMapEntry(
            lib=value["lib"],
            cell=value["cell"],
            view=value.get("view", "symbol"),
            term_order=list(value.get("term_order", [])),
            param_map=dict(value.get("param_map", {})),
        )
    return device_map


def default_device_map_json() -> str:
    """Return the default device map as pretty JSON for users to customize."""
    return json.dumps(
        {name: asdict(entry) for name, entry in DEFAULT_DEVICE_MAP.items()},
        indent=2,
        ensure_ascii=False,
    )


def _default_virtuoso_workdir(
    results_path: str | Path | None,
    cell_name: str,
) -> Path:
    if results_path is not None:
        project_name = Path(results_path).parent.name
    else:
        project_name = cell_name
    clean = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in project_name)
    clean = clean.strip("_") or cell_name
    return Path(__file__).resolve().parents[2] / "virtuoso_runs" / clean


def _render_cds_lib(
    lib_name: str,
    tech_lib: str,
    include_cds_libs: list[str | Path] | None = None,
    pdk_lib_path: str | Path | None = None,
) -> str:
    lines = [
        "-- Auto-generated by Circuit Agent Virtuoso exporter",
        "-- SOFTINCLUDE site/user cds.lib files so batch Virtuoso can see",
        "-- Cadence basic libraries, analogLib, and PDK symbol libraries.",
    ]
    for path in include_cds_libs or []:
        lines.append(f"SOFTINCLUDE {Path(path).expanduser()}")
    if pdk_lib_path:
        lines.append(f"DEFINE {tech_lib} {Path(pdk_lib_path).expanduser()}")
    lines.extend([
        f"DEFINE {lib_name} ./{lib_name}",
        "",
    ])
    return "\n".join(lines)


def _render_run_import_skill(
    lib_name: str,
    cell_name: str,
    tech_lib: str,
    import_skill: str,
) -> str:
    return "\n".join([
        ";; Auto-generated Virtuoso batch import wrapper",
        "let((libName cellName techLibName importSkill libObj libPath)",
        f"  libName = {_skill_str(lib_name)}",
        f"  cellName = {_skill_str(cell_name)}",
        f"  techLibName = {_skill_str(tech_lib)}",
        f"  importSkill = {_skill_str('./' + import_skill)}",
        "  libPath = strcat(getShellEnvVar(\"PWD\") \"/\" libName)",
        "",
        "  libObj = ddGetObj(libName)",
        "  unless(libObj",
        "    libObj = ddCreateLib(libName libPath)",
        "  )",
        "  unless(libObj",
        "    error(\"Unable to create or open library %s at %s\\n\" libName libPath)",
        "  )",
        "",
        "  when(techLibName != \"\"",
        "    unless(ddGetObj(techLibName)",
        "      error(\"Tech library %s is not visible. Check cds.lib / Cadence environment.\\n\" techLibName)",
        "    )",
        "    techBindTechFile(libObj techLibName)",
        "  )",
        "  ddReleaseObj(libObj)",
        "",
        "  printf(\"Loading BO schematic import: %s\\n\" importSkill)",
        "  load(importSkill)",
        "  printf(\"Virtuoso import completed: %s/%s/schematic\\n\" libName cellName)",
        "  exit(0)",
        ")",
        "",
    ])


def _render_workspace_readme(
    lib_name: str,
    cell_name: str,
    tech_lib: str,
    run_script: str,
    include_cds_libs: list[str | Path] | None = None,
    pdk_lib_path: str | Path | None = None,
    cds_log: str | Path | None = None,
) -> str:
    includes = "\n".join(
        f"- SOFTINCLUDE `{Path(path).expanduser()}`"
        for path in (include_cds_libs or [])
    ) or "- No external cds.lib included"
    pdk_line = (
        f"- DEFINE `{tech_lib}` `{Path(pdk_lib_path).expanduser()}`"
        if pdk_lib_path
        else f"- No explicit PDK DEFINE for `{tech_lib}`; it must come from an included cds.lib"
    )
    cds_log_line = f"- CDS log: `{cds_log}`" if cds_log else "- CDS log: workspace default"
    return f"""# Virtuoso Import Workspace

This directory was generated by Circuit Agent.

- Target library: `{lib_name}`
- Target cell: `{cell_name}`
- Target view: `schematic`
- Tech library: `{tech_lib}`
{cds_log_line}

Generated `cds.lib` visibility setup:
{includes}
{pdk_line}

Run manually from this directory if automatic import was not executed:

```bash
virtuoso -nograph -replay {run_script}
```

Make sure your Cadence startup environment can resolve `{tech_lib}`, `analogLib`,
and the PDK symbol libraries. The Spectre model include path is not a Virtuoso
technology library and is not used for schematic library attachment.
"""


def _resolve_bo_netlist(results_path: Path, result_data: dict[str, Any]) -> Path:
    netlist_ref = result_data.get("netlist_file")
    if not netlist_ref:
        raise ValueError(f"results.json does not contain 'netlist_file': {results_path}")
    netlist_path = Path(netlist_ref)
    if not netlist_path.is_absolute():
        netlist_path = (results_path.parent / netlist_path).resolve()
    return netlist_path


def _load_targets(results_path: Path) -> dict[str, float]:
    log_path = results_path.parent / "optimization_log.json"
    if not log_path.exists():
        return {}
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    targets = data.get("targets")
    if not isinstance(targets, dict):
        return {}
    return {
        key: float(value)
        for key, value in targets.items()
        if value is not None
    }


def _select_passing_review_candidate(
    project_dir: Path,
    targets: dict[str, float],
) -> Path | None:
    if not targets:
        return None
    metrics_path = project_dir / "agent_review" / "candidate_metrics.csv"
    if not metrics_path.exists():
        return None

    with metrics_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        if row.get("error_message", "").strip():
            continue
        if not _candidate_meets_targets(row, targets):
            continue
        candidate_path = Path(row.get("candidate_path", ""))
        if not candidate_path.is_absolute():
            candidate_path = (metrics_path.parent / candidate_path).resolve()
        netlist_path = (
            candidate_path / "circuit.cir"
            if candidate_path.is_dir()
            else candidate_path
        )
        if netlist_path.exists():
            return netlist_path
    return None


def _candidate_meets_targets(row: dict[str, str], targets: dict[str, float]) -> bool:
    checks: list[bool] = []
    if "gain_db" in targets:
        checks.append(_read_metric(row, "gain_db(dB)") >= targets["gain_db"])
    if "bandwidth_hz" in targets:
        checks.append(
            _read_metric(row, "gbw_hz(MHz)", scale=1e6)
            >= targets["bandwidth_hz"]
        )
    if "phase_margin_deg" in targets:
        checks.append(
            _read_metric(row, "phase_margin_deg(deg)")
            >= targets["phase_margin_deg"]
        )
    if "power_w" in targets:
        checks.append(
            _read_metric(row, "power_w(mW)", scale=1e-3)
            <= targets["power_w"]
        )
    if "slew_rate_v_per_s" in targets:
        checks.append(
            _read_metric(row, "slew_rate_v_per_s(V/us)", scale=1e6)
            >= targets["slew_rate_v_per_s"]
        )
    if "settling_time_s" in targets:
        checks.append(
            _read_metric(row, "settling_time_s(ns)", scale=1e-9)
            <= targets["settling_time_s"]
        )
    return bool(checks) and all(checks)


def _read_metric(row: dict[str, str], key: str, scale: float = 1.0) -> float:
    value = row.get(key, "")
    if value is None or value == "":
        return float("nan")
    try:
        return float(value) * scale
    except ValueError:
        return float("nan")


def _skill_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("\"", "\\\"")
    return f"\"{escaped}\""


def _default_cell_name(
    result_data: dict[str, Any],
    netlist_path: Path,
    export_source: str = "bo_best",
) -> str:
    project_name = str(result_data.get("project_name") or netlist_path.stem)
    clean = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in project_name)
    clean = clean.strip("_") or netlist_path.stem
    suffix = "review_opt" if export_source == "agent_review" else "opt"
    return f"{clean}_{suffix}"
