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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run BO evidence -> Review/Audit -> PVT -> layout -> DRC/LVS.")
    parser.add_argument("--project", required=True, help="outputs/<project> directory")
    parser.add_argument("--run-pvt", action="store_true")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--export-virtuoso", action="store_true")
    parser.add_argument("--prepare-physical", action="store_true")
    parser.add_argument("--run-signoff", action="store_true")
    parser.add_argument("--lib", default="BO_Designs")
    parser.add_argument("--max-eco-iterations", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 0 <= args.max_eco_iterations <= 5:
        raise SystemExit("--max-eco-iterations must be between 0 and 5")
    state = run_design_flow(
        project=args.project,
        run_pvt=args.run_pvt,
        simulate=args.simulate,
        export_virtuoso=args.export_virtuoso,
        prepare_physical=args.prepare_physical or args.run_signoff,
        run_signoff=args.run_signoff,
        max_eco_iterations=args.max_eco_iterations,
        lib_name=args.lib,
    )
    report = Path(state["project_dir"]) / "flow" / "flow_report.md"
    print(f"Flow report: {report}")
    print(f"Next action: {state.get('next_action')}")
    return 0 if state.get("next_action") in {"done", "ready_to_export_virtuoso", "run_signoff"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
