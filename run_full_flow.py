#!/usr/bin/env python3
"""Single-repository entry point for frontend design through physical sign-off."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
CIRCUIT_AGENT = ROOT / "Agent_LLM_BO" / "circuit_agent"
for path in (ROOT, CIRCUIT_AGENT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from design_flow_graph import run_design_flow  # noqa: E402
from full_flow_frontend import (  # noqa: E402
    ensure_physical_topology_supported,
    load_pvt_targets,
    prepare_frontend_project,
    run_automatic_review,
    run_frontend_optimization,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Auto_Agent_Design requirements/netlist/BO/Review/PVT, then "
            "layout/GDS/Calibre DRC/LVS."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--project", help="resume an existing outputs/<project> directory")
    source.add_argument("--request", help="free-text circuit requirement (uses the configured frontend LLM)")
    source.add_argument("--requirements", help="structured frontend requirements JSON (offline parsing)")
    parser.add_argument("--project-name", help="output project name when starting from requirements")
    parser.add_argument("--topology", help="explicit Auto_Agent_Design topology override")
    parser.add_argument("--max-iter", type=int, help="maximum frontend BO iterations")
    parser.add_argument("--dry-run", action="store_true", help="use frontend mock simulation; incompatible with physical sign-off")
    parser.add_argument(
        "--workspace",
        default=str(CIRCUIT_AGENT / "workspace"),
        help="Auto_Agent_Design BO workspace used by automatic Review",
    )
    parser.add_argument("--run-pvt", action="store_true")
    parser.add_argument(
        "--pvt-requirements",
        help="optional JSON with PVT acceptance targets, separate from nominal BO targets",
    )
    parser.add_argument("--simulate", action="store_true")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare-schematic", action="store_true")
    action.add_argument("--import-schematic", action="store_true")
    action.add_argument("--prepare-physical", action="store_true")
    action.add_argument("--run-signoff", action="store_true")
    parser.add_argument("--lib", default="BO_Designs")
    parser.add_argument("--max-eco-iterations", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 0 <= args.max_eco_iterations <= 5:
        raise SystemExit("--max-eco-iterations must be between 0 and 5")
    starting_from_requirements = bool(args.request or args.requirements)
    pvt_targets = load_pvt_targets(
        requirements_file=args.requirements,
        pvt_requirements_file=args.pvt_requirements,
    )
    implementation_requested = any((
        args.prepare_schematic,
        args.import_schematic,
        args.prepare_physical,
        args.run_signoff,
    ))
    if args.dry_run and implementation_requested:
        raise SystemExit("--dry-run cannot provide implementation PVT evidence")
    if starting_from_requirements and implementation_requested and not (args.run_pvt and args.simulate):
        raise SystemExit(
            "a new schematic/physical flow requires --run-pvt --simulate so PVT evidence is real"
        )

    if starting_from_requirements:
        frontend = prepare_frontend_project(
            request=args.request,
            requirements_file=args.requirements,
            topology=args.topology,
            project_name=args.project_name,
        )
        if implementation_requested:
            ensure_physical_topology_supported(frontend.topology)
        project = run_frontend_optimization(
            frontend,
            max_iterations=args.max_iter,
            dry_run=args.dry_run,
        )
        print(f"Frontend project: {frontend.input_dir}")
        print(f"BO output: {project}")
    else:
        project = Path(args.project).resolve()

    qualification = run_design_flow(project=project)
    topology = str(qualification.get("topology") or args.topology or "")
    if implementation_requested and topology:
        ensure_physical_topology_supported(topology)
    if str(qualification.get("next_action", "")).startswith("prepare_agent_review"):
        if not topology:
            raise SystemExit("automatic Review requires topology_name in results.json")
        run_automatic_review(
            project_dir=project,
            topology=topology,
            workspace=args.workspace,
            dry_run=args.dry_run,
        )
        print(f"Review output: {Path(project) / 'agent_review'}")

    state = run_design_flow(
        project=project,
        run_pvt=args.run_pvt,
        simulate=args.simulate,
        prepare_schematic=args.prepare_schematic or args.import_schematic,
        import_schematic=args.import_schematic,
        prepare_physical=args.prepare_physical or args.run_signoff,
        run_signoff=args.run_signoff,
        max_eco_iterations=args.max_eco_iterations,
        lib_name=args.lib,
        pvt_targets=pvt_targets,
    )
    report = Path(state["project_dir"]) / "flow" / "flow_report.md"
    print(f"Flow report: {report}")
    print(f"Next action: {state.get('next_action')}")
    successful_actions = {"done", "import_schematic", "run_signoff"}
    return 0 if state.get("next_action") in successful_actions else 2


if __name__ == "__main__":
    raise SystemExit(main())
