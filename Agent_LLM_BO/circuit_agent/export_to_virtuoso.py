"""CLI for exporting optimized BO netlists to Virtuoso SKILL."""

from __future__ import annotations

import argparse
from pathlib import Path

from virtuoso_export.exporter import (
    default_device_map_json,
    export_from_results,
    export_netlist,
)
from pdk_profiles import get_pdk_profile


def main() -> None:
    args = parse_args()

    if args.dump_default_device_map:
        path = Path(args.dump_default_device_map)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_device_map_json() + "\n", encoding="utf-8")
        print(f"Default device map written: {path}")
        return

    if not args.results and not args.netlist:
        raise SystemExit("Provide --results or --netlist")
    if args.results and args.netlist:
        raise SystemExit("Use either --results or --netlist, not both")
    if not args.lib:
        raise SystemExit("Provide --lib <virtuoso_library>")

    if args.results:
        report = export_from_results(
            results_path=args.results,
            lib_name=args.lib,
            cell_name=args.cell,
            out_path=args.out,
            device_map_path=args.device_map,
            virtuoso_workdir=args.virtuoso_workdir,
            tech_lib=args.tech_lib,
            run_virtuoso=args.run_virtuoso,
            virtuoso_bin=args.virtuoso_bin,
            include_cds_libs=args.include_cds_lib,
            pdk_lib_path=args.pdk_lib_path,
            cds_log_path=args.cds_log,
        )
    else:
        if not args.cell:
            raise SystemExit("--cell is required when exporting directly from --netlist")
        if not args.out:
            raise SystemExit("--out is required when exporting directly from --netlist")
        report = export_netlist(
            netlist_path=args.netlist,
            lib_name=args.lib,
            cell_name=args.cell,
            out_path=args.out,
            device_map_path=args.device_map,
            virtuoso_workdir=args.virtuoso_workdir,
            tech_lib=args.tech_lib,
            run_virtuoso=args.run_virtuoso,
            virtuoso_bin=args.virtuoso_bin,
            include_cds_libs=args.include_cds_lib,
            pdk_lib_path=args.pdk_lib_path,
            cds_log_path=args.cds_log,
        )

    print(f"SKILL file: {report['skill_file']}")
    print(f"Report: {Path(report['skill_file']).parent / 'export_report.json'}")
    print(f"Virtuoso target: {report['target_lib']}/{report['target_cell']}/schematic")
    if "virtuoso_workdir" in report:
        print(f"Virtuoso workdir: {report['virtuoso_workdir']}")
        print(f"Run script: {report['run_script']}")
        print(f"Run log: {report['run_log']}")
        print(f"CDS log: {report['cds_log']}")


def parse_args() -> argparse.Namespace:
    pdk = get_pdk_profile()
    parser = argparse.ArgumentParser(
        description="Export Circuit Agent BO final netlist to Virtuoso SKILL"
    )
    parser.add_argument(
        "--results",
        type=str,
        default=None,
        help="Path to outputs/<project>/results.json",
    )
    parser.add_argument(
        "--netlist",
        type=str,
        default=None,
        help="Path to final rendered circuit.cir; use with --cell and --out",
    )
    parser.add_argument(
        "--lib",
        type=str,
        default="BO_Designs",
        help="Target Virtuoso library name",
    )
    parser.add_argument(
        "--cell",
        type=str,
        default=None,
        help="Target Virtuoso cell name. Defaults to <project>_opt for --results.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output SKILL path. Defaults to outputs/<project>/virtuoso/import_schematic.il for --results.",
    )
    parser.add_argument(
        "--device-map",
        type=str,
        default=None,
        help="JSON file overriding model-to-Virtuoso-symbol mappings",
    )
    parser.add_argument(
        "--dump-default-device-map",
        type=str,
        default=None,
        help="Write the default device map JSON to this path and exit",
    )
    parser.add_argument(
        "--virtuoso-workdir",
        type=str,
        default=None,
        help=(
            "Cadence working directory. Defaults to "
            "Agent_LLM_BO/virtuoso_runs/<project> when --run-virtuoso is used."
        ),
    )
    parser.add_argument(
        "--tech-lib",
        type=str,
        default=pdk.virtuoso_tech_lib,
        help="Virtuoso technology library to attach to the generated design library",
    )
    parser.add_argument(
        "--include-cds-lib",
        action="append",
        default=[],
        help=(
            "Add SOFTINCLUDE <path> to generated cds.lib. May be passed "
            "multiple times, e.g. --include-cds-lib /home/userone/cds.lib"
        ),
    )
    parser.add_argument(
        "--pdk-lib-path",
        type=str,
        default=pdk.virtuoso_pdk_lib_path,
        help=(
            "Explicit OA library path for --tech-lib, written as "
            "DEFINE <tech-lib> <path> in generated cds.lib"
        ),
    )
    parser.add_argument(
        "--cds-log",
        type=str,
        default=None,
        help=(
            "CDS_LOG path for --run-virtuoso. Defaults to "
            "<virtuoso-workdir>/CDS.log to avoid locking ~/CDS.log"
        ),
    )
    parser.add_argument(
        "--virtuoso-bin",
        type=str,
        default="virtuoso",
        help="Virtuoso executable used with --run-virtuoso",
    )
    parser.add_argument(
        "--run-virtuoso",
        action="store_true",
        help="Launch virtuoso -nograph -replay run_import.il after exporting SKILL",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
