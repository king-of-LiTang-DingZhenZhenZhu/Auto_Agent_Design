"""Post-optimization PVT verification for BO/review netlists."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from config import Settings, settings
from models import DesignTarget, SimResult
from pdk_profiles import PDKProfile, get_pdk_profile, spectre_include_line
from simulator import Simulator
from summarize_metrics import build_report_from_sim_result
from virtuoso_export.exporter import select_export_netlist


@dataclass(frozen=True)
class PVTCorner:
    process: str
    section: str
    vdd_label: str
    vdd: float
    temperature_c: float

    @property
    def corner_id(self) -> str:
        return (
            f"{self.process}_"
            f"{self.vdd_label}{_token_float(self.vdd)}_"
            f"t{_token_temp(self.temperature_c)}"
        )


def default_pvt_corners(profile: PDKProfile | None = None) -> list[PVTCorner]:
    pdk = profile or get_pdk_profile()
    vdds = [
        ("vmin", pdk.vdd_min),
        ("vtyp", pdk.vdd),
        ("vmax", pdk.vdd_max),
    ]
    temps = list(pdk.pvt_temperatures_c)
    corners: list[PVTCorner] = []
    for process in ("tt", "ss", "ff"):
        section = pdk.process_sections[process]
        for vdd_label, vdd in vdds:
            for temp in temps:
                corners.append(PVTCorner(process, section, vdd_label, vdd, temp))
    return corners


def patch_netlist_for_corner(
    netlist_text: str,
    corner: PVTCorner,
    profile: PDKProfile | None = None,
) -> str:
    pdk = profile or get_pdk_profile()
    include_pattern = re.compile(r'(?m)^include\s+"[^"]+"\s+section=\S+')
    include_line = spectre_include_line(pdk)
    include_line = re.sub(r"\bsection=\S+", f"section={corner.section}", include_line)
    if include_pattern.search(netlist_text):
        return include_pattern.sub(include_line, netlist_text, count=1)
    return netlist_text.replace(
        "simulator lang=spectre insensitive=yes",
        f"simulator lang=spectre insensitive=yes\n\n{include_line}",
        1,
    )


def patch_testbench_for_corner(testbench_text: str, corner: PVTCorner) -> str:
    text = re.sub(
        r"(?m)(parameters\b[^\n]*\bVDD=)([^\s]+)",
        rf"\g<1>{_fmt_spectre(corner.vdd)}",
        testbench_text,
        count=1,
    )
    if "VDD=" not in text:
        text = text.replace(
            "include \"circuit.cir\"",
            f"include \"circuit.cir\"\n\nparameters VDD={_fmt_spectre(corner.vdd)}",
            1,
        )
    temp_pattern = re.compile(r"(?m)^tempOption\s+options\s+temp=[^\s]+")
    temp_line = f"tempOption options temp={_fmt_spectre(corner.temperature_c)}"
    if temp_pattern.search(text):
        text = temp_pattern.sub(temp_line, text, count=1)
    else:
        text += f"\n{temp_line}\n"
    return text


def run_pvt_verification(
    results_path: str | Path | None = None,
    netlist_path: str | Path | None = None,
    testbench_paths: list[str | Path] | None = None,
    simulate: bool = False,
    dry_run: bool = False,
    config: Settings | None = None,
    profile: PDKProfile | None = None,
) -> dict[str, Any]:
    cfg = config or settings
    if dry_run:
        cfg.dry_run = True
    pdk = profile or get_pdk_profile()

    project_root, selected_netlist, source = _resolve_source(
        results_path=results_path,
        netlist_path=netlist_path,
    )
    targets = _load_targets(project_root, results_path)
    testbenches = _resolve_testbenches(
        project_root=project_root,
        selected_netlist=selected_netlist,
        testbench_paths=testbench_paths,
    )

    pvt_root = project_root / "pvt"
    corners_root = pvt_root / "corners"
    if corners_root.exists():
        shutil.rmtree(corners_root)
    corners_root.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    json_corners: list[dict[str, Any]] = []
    simulator = Simulator(cfg)
    for corner in default_pvt_corners(pdk):
        corner_dir = corners_root / corner.corner_id
        corner_dir.mkdir(parents=True, exist_ok=True)
        corner_netlist = corner_dir / "circuit.cir"
        corner_netlist.write_text(
            patch_netlist_for_corner(
                selected_netlist.read_text(encoding="utf-8"),
                corner,
                pdk,
            ),
            encoding="utf-8",
        )
        tb_paths = _write_corner_testbenches(corner_dir, testbenches, corner)
        if simulate:
            result = simulator.run_all_testbenches(tb_paths, corner_dir)
        else:
            result = SimResult(
                converged=False,
                error_message="Simulation not run; pass --simulate to run Spectre",
            )
        report = build_report_from_sim_result(result, corner_dir, tb_paths)
        (corner_dir / "metrics_summary.txt").write_text(report, encoding="utf-8")

        all_met, status = targets.is_satisfied(result) if targets else (False, {})
        row = _row_from_corner(corner, result, status, all_met)
        rows.append(row)
        json_corners.append({
            "corner": asdict(corner),
            "corner_id": corner.corner_id,
            "result": result.to_result_dict(targets=targets if targets else None),
            "status": status,
            "all_targets_met": all_met,
            "path": str(corner_dir),
        })

    summary = summarize_pvt(rows)
    pvt_root.mkdir(parents=True, exist_ok=True)
    _write_pvt_csv(pvt_root / "pvt_results.csv", rows)
    (pvt_root / "pvt_results.json").write_text(
        json.dumps(
            {
                "pvt_pass": summary["pvt_pass"],
                "source": source,
                "netlist_file": str(selected_netlist),
                "pdk_profile": pdk.to_dict(),
                "targets": targets.to_requirements_dict()["targets"] if targets else {},
                "summary": summary,
                "corners": json_corners,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    (pvt_root / "pvt_report.md").write_text(
        _render_pvt_report(summary, rows, selected_netlist, source),
        encoding="utf-8",
    )
    return {
        "pvt_root": str(pvt_root),
        "pvt_pass": summary["pvt_pass"],
        "source": source,
        "netlist_file": str(selected_netlist),
        "corners": len(rows),
        "summary": summary,
    }


def summarize_pvt(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pvt_pass = bool(rows) and all(row["all_targets_met"] for row in rows)
    failures = [row for row in rows if not row["all_targets_met"]]
    return {
        "pvt_pass": pvt_pass,
        "failed_corners": len(failures),
        "total_corners": len(rows),
        "worst": {
            "min_gain_db": _worst_min(rows, "gain_db(dB)"),
            "min_gbw_mhz": _worst_min(rows, "gbw_hz(MHz)"),
            "min_phase_margin_deg": _worst_min(rows, "phase_margin_deg(deg)"),
            "max_power_mw": _worst_max(rows, "power_w(mW)"),
            "min_slew_rate_v_us": _worst_min(rows, "slew_rate_v_per_s(V/us)"),
            "max_settling_time_ns": _worst_max(rows, "settling_time_s(ns)"),
        },
        "failed_corner_ids": [row["corner_id"] for row in failures],
    }


def main() -> None:
    args = parse_args()
    if not args.results and not args.netlist:
        raise SystemExit("Provide --results or --netlist")
    report = run_pvt_verification(
        results_path=args.results,
        netlist_path=args.netlist,
        testbench_paths=args.testbench,
        simulate=args.simulate,
        dry_run=args.dry_run,
        config=settings,
    )
    print(f"PVT root: {report['pvt_root']}")
    print(f"PVT pass: {report['pvt_pass']}")
    print(f"Corners: {report['corners']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run PVT verification on BO/review final netlists"
    )
    parser.add_argument("--results", type=str, default=None)
    parser.add_argument("--netlist", type=str, default=None)
    parser.add_argument(
        "--testbench",
        action="append",
        default=[],
        help="Testbench path for --netlist mode. May be repeated.",
    )
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _resolve_source(
    results_path: str | Path | None,
    netlist_path: str | Path | None,
) -> tuple[Path, Path, str]:
    if results_path:
        result_path = Path(results_path).resolve()
        result_data = json.loads(result_path.read_text(encoding="utf-8"))
        if netlist_path:
            return result_path.parent, Path(netlist_path).resolve(), "explicit_netlist"
        selected, source = select_export_netlist(result_path, result_data)
        return result_path.parent, selected.resolve(), source
    if netlist_path:
        selected = Path(netlist_path).resolve()
        return selected.parent, selected, "explicit_netlist"
    raise ValueError("Provide --results or --netlist")


def _resolve_testbenches(
    project_root: Path,
    selected_netlist: Path,
    testbench_paths: list[str | Path] | None,
) -> list[Path]:
    if testbench_paths:
        return [Path(path).resolve() for path in testbench_paths]
    local = sorted(selected_netlist.parent.glob("tb*.scs"))
    if local:
        return local
    project_tbs = sorted((project_root / "simulation").glob("tb_circuit*.scs"))
    if project_tbs:
        return project_tbs
    raise ValueError(
        "No testbenches found. Use --testbench with --netlist mode or keep "
        "simulation/tb_circuit*.scs in the project output."
    )


def _write_corner_testbenches(
    corner_dir: Path,
    source_testbenches: list[Path],
    corner: PVTCorner,
) -> list[Path]:
    written: list[Path] = []
    for i, tb_path in enumerate(source_testbenches):
        target = corner_dir / ("tb.scs" if i == 0 else f"tb_{i}.scs")
        target.write_text(
            patch_testbench_for_corner(tb_path.read_text(encoding="utf-8"), corner),
            encoding="utf-8",
        )
        written.append(target)
    return written


def _load_targets(project_root: Path, results_path: str | Path | None) -> DesignTarget | None:
    log_path = project_root / "optimization_log.json"
    if log_path.exists():
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
            targets = data.get("targets", {})
            if isinstance(targets, dict):
                return _target_from_dict(targets)
        except json.JSONDecodeError:
            pass
    if results_path:
        result_data = json.loads(Path(results_path).read_text(encoding="utf-8"))
        targets = result_data.get("targets")
        if isinstance(targets, dict):
            return _target_from_dict(targets)
    return None


def _target_from_dict(data: dict[str, Any]) -> DesignTarget:
    return DesignTarget(
        gain_db=data.get("gain_db"),
        bandwidth_hz=data.get("gbw_hz", data.get("bandwidth_hz")),
        phase_margin_deg=data.get("phase_margin_deg"),
        power_w=data.get("power_w"),
        load_cap_f=data.get("load_cap_f"),
        slew_rate_v_per_s=data.get("slew_rate_v_per_s"),
        settling_time_s=data.get("settling_time_s"),
    )


def _row_from_corner(
    corner: PVTCorner,
    result: SimResult,
    status: dict[str, bool],
    all_met: bool,
) -> dict[str, Any]:
    return {
        "corner_id": corner.corner_id,
        "process": corner.process,
        "section": corner.section,
        "vdd_label": corner.vdd_label,
        "vdd(V)": _fmt_csv(corner.vdd, 3),
        "temp(C)": _fmt_csv(corner.temperature_c, 1),
        "all_targets_met": all_met,
        "gain_db(dB)": _fmt_csv(result.gain_db, 2),
        "gbw_hz(MHz)": _fmt_csv(result.bandwidth_hz, 2, 1e-6),
        "phase_margin_deg(deg)": _fmt_csv(result.phase_margin_deg, 2),
        "power_w(mW)": _fmt_csv(result.power_w, 3, 1e3),
        "slew_rate_v_per_s(V/us)": _fmt_csv(result.slew_rate_v_per_s, 2, 1e-6),
        "settling_time_s(ns)": _fmt_csv(result.settling_time_s, 2, 1e9),
        "failed_metrics": ";".join(
            name for name, passed in sorted(status.items()) if not passed
        ),
        "error_message": result.error_message,
    }


def _write_pvt_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "corner_id",
        "process",
        "section",
        "vdd_label",
        "vdd(V)",
        "temp(C)",
        "all_targets_met",
        "gain_db(dB)",
        "gbw_hz(MHz)",
        "phase_margin_deg(deg)",
        "power_w(mW)",
        "slew_rate_v_per_s(V/us)",
        "settling_time_s(ns)",
        "failed_metrics",
        "error_message",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _render_pvt_report(
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    netlist_path: Path,
    source: str,
) -> str:
    lines = [
        "# PVT Verification Report",
        "",
        f"- Source: `{source}`",
        f"- Netlist: `{netlist_path}`",
        f"- PVT pass: `{summary['pvt_pass']}`",
        f"- Failed corners: {summary['failed_corners']} / {summary['total_corners']}",
        "",
        "## Worst Metrics",
        "",
    ]
    for name, value in summary["worst"].items():
        lines.append(f"- {name}: {value}")
    if summary["failed_corner_ids"]:
        lines.extend(["", "## Failed Corners", ""])
        for corner_id in summary["failed_corner_ids"]:
            row = next(row for row in rows if row["corner_id"] == corner_id)
            lines.append(
                f"- `{corner_id}` failed `{row['failed_metrics'] or 'simulation'}`"
            )
    return "\n".join(lines) + "\n"


def _worst_min(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    numeric = [(row, _to_float(row.get(key))) for row in rows]
    numeric = [(row, value) for row, value in numeric if value is not None]
    if not numeric:
        return None
    row, value = min(numeric, key=lambda item: item[1])
    return {"corner_id": row["corner_id"], "value": value}


def _worst_max(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    numeric = [(row, _to_float(row.get(key))) for row in rows]
    numeric = [(row, value) for row, value in numeric if value is not None]
    if not numeric:
        return None
    row, value = max(numeric, key=lambda item: item[1])
    return {"corner_id": row["corner_id"], "value": value}


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed):
        return None
    return parsed


def _fmt_csv(value: float | None, digits: int, scale: float = 1.0) -> str:
    if value is None:
        return ""
    return f"{value * scale:.{digits}f}"


def _fmt_spectre(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6g}"


def _token_float(value: float) -> str:
    return f"{value:.3g}".replace("-", "m").replace(".", "p")


def _token_temp(value: float) -> str:
    prefix = "m" if value < 0 else ""
    return f"{prefix}{abs(int(round(value)))}"


if __name__ == "__main__":
    main()
