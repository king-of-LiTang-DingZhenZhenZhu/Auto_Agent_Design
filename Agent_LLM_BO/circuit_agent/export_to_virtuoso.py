"""CLI for exporting optimized BO netlists to Virtuoso SKILL."""

from __future__ import annotations

import argparse
from pathlib import Path

from virtuoso_export.exporter import (
    default_device_map_json,
    export_from_results,
    export_netlist,
)


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
        )

    print(f"SKILL file: {report['skill_file']}")
    print(f"Report: {Path(report['skill_file']).parent / 'export_report.json'}")
    print(f"Virtuoso target: {report['target_lib']}/{report['target_cell']}/schematic")


def parse_args() -> argparse.Namespace:
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
    return parser.parse_args()


if __name__ == "__main__":
    main()
