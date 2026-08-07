"""LangGraph-style orchestration for BO -> Review -> PVT and implementation."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Literal, TypedDict

from design_audit import run_design_audit
from models import DesignTarget
from pdk_profiles import PDKProfile
from pvt_simulation import run_pvt_verification
from virtuoso_export.exporter import select_export_netlist

try:  # Optional at runtime; requirements.txt includes it for real graph use.
    from langgraph.graph import END, StateGraph
except Exception:  # pragma: no cover - exercised when dependency is absent
    END = "__end__"
    StateGraph = None


class DesignFlowState(TypedDict, total=False):
    project_dir: str
    results_json: str
    topology: str
    run_pvt: bool
    simulate: bool
    prepare_schematic: bool
    import_schematic: bool
    prepare_physical: bool
    run_signoff: bool
    max_eco_iterations: int
    lib_name: str
    nominal_pass: bool | None
    review_available: bool
    review_pass: bool | None
    audit_status: str | None
    audit_blockers: int
    audit_warnings: int
    audit_report: str | None
    review_mode: str | None
    pvt_requested: bool
    pvt_targets: DesignTarget | None
    pvt_profile: PDKProfile | None
    pvt_pass: bool | None
    final_source: str | None
    final_netlist: str | None
    schematic_requested: bool
    schematic_status: str | None
    schematic_root: str | None
    schematic_handoff: str | None
    schematic_plan: str | None
    schematic_skill: str | None
    schematic_import_skill: str | None
    schematic_blocker: str | None
    physical_requested: bool
    physical_status: str | None
    physical_root: str | None
    physical_handoff: str | None
    physical_layout_plan: str | None
    physical_gds: str | None
    physical_drc_report: str | None
    physical_lvs_report: str | None
    physical_drc_violations: int | None
    physical_lvs_issues: int | None
    physical_eco_iterations: int
    physical_blocker: str | None
    next_action: str
    errors: list[str]
    langgraph_available: bool


def run_design_flow(
    project: str | Path,
    run_pvt: bool = False,
    simulate: bool = False,
    prepare_schematic: bool = False,
    import_schematic: bool = False,
    prepare_physical: bool = False,
    run_signoff: bool = False,
    max_eco_iterations: int = 5,
    lib_name: str = "BO_Designs",
    pvt_targets: DesignTarget | None = None,
    pvt_profile: PDKProfile | None = None,
) -> DesignFlowState:
    if not 0 <= max_eco_iterations <= 5:
        raise ValueError("max_eco_iterations must be between 0 and 5")
    project_dir = Path(project)
    initial: DesignFlowState = {
        "project_dir": str(project_dir),
        "results_json": str(project_dir / "results.json"),
        "run_pvt": run_pvt,
        "simulate": simulate,
        "prepare_schematic": prepare_schematic or import_schematic,
        "import_schematic": import_schematic,
        "prepare_physical": prepare_physical or run_signoff,
        "run_signoff": run_signoff,
        "max_eco_iterations": max_eco_iterations,
        "lib_name": lib_name,
        "errors": [],
        "pvt_requested": run_pvt,
        "pvt_targets": pvt_targets,
        "pvt_profile": pvt_profile,
        "langgraph_available": StateGraph is not None,
        "physical_requested": prepare_physical or run_signoff,
        "schematic_requested": prepare_schematic or import_schematic,
    }
    if StateGraph is None:
        state = _run_fallback(initial)
    else:
        graph = _build_graph()
        state = graph.invoke(initial)
    if (prepare_schematic or import_schematic) and not (prepare_physical or run_signoff):
        from physical_bridge import execute_schematic_from_state

        state = execute_schematic_from_state(
            state,
            prepare_schematic=prepare_schematic or import_schematic,
            import_schematic=import_schematic,
        )
    if prepare_physical or run_signoff:
        from physical_bridge import execute_physical_from_state

        state = execute_physical_from_state(
            state,
            prepare_physical=prepare_physical or run_signoff,
            run_signoff=run_signoff,
            max_eco_iterations=max_eco_iterations,
        )
    _write_flow_outputs(state)
    return state


def _build_graph():
    graph = StateGraph(DesignFlowState)
    graph.add_node("load_results", load_results)
    graph.add_node("check_nominal", check_nominal)
    graph.add_node("run_design_audit", run_design_audit_node)
    graph.add_node("prepare_review", prepare_review)
    graph.add_node("run_pvt", run_pvt_node)
    graph.add_node("check_pvt", check_pvt)

    graph.set_entry_point("load_results")
    graph.add_edge("load_results", "check_nominal")
    graph.add_conditional_edges(
        "check_nominal",
        route_after_nominal,
        {
            "review": "prepare_review",
            "audit": "run_design_audit",
        },
    )
    graph.add_conditional_edges(
        "run_design_audit",
        route_after_audit,
        {
            "review": "prepare_review",
            "pvt": "run_pvt",
        },
    )
    graph.add_edge("prepare_review", END)
    graph.add_edge("run_pvt", "check_pvt")
    graph.add_edge("check_pvt", END)
    return graph.compile()


def _run_fallback(state: DesignFlowState) -> DesignFlowState:
    state = load_results(state)
    state = check_nominal(state)
    if route_after_nominal(state) == "review":
        return prepare_review(state)
    state = run_design_audit_node(state)
    if route_after_audit(state) == "review":
        return prepare_review(state)
    state = run_pvt_node(state)
    state = check_pvt(state)
    return state


def load_results(state: DesignFlowState) -> DesignFlowState:
    project = Path(state["project_dir"])
    results_path = Path(state["results_json"])
    errors = list(state.get("errors", []))
    if not results_path.exists():
        errors.append(f"Missing results.json: {results_path}")
        return {**state, "errors": errors, "next_action": "run_bo"}

    result_data = json.loads(results_path.read_text(encoding="utf-8"))
    topology = str(result_data.get("topology_name") or result_data.get("topology") or "")
    nominal_pass = bool(result_data.get("all_targets_met"))
    final_netlist, final_source = _select_final_netlist(results_path, result_data)
    review_available = (project / "agent_review" / "candidate_metrics.csv").exists()
    review_pass = final_source == "agent_review"
    return {
        **state,
        "topology": topology,
        "nominal_pass": nominal_pass,
        "review_available": review_available,
        "review_pass": review_pass,
        "final_source": final_source,
        "final_netlist": str(final_netlist) if final_netlist else None,
        "errors": errors,
    }


def check_nominal(state: DesignFlowState) -> DesignFlowState:
    if state.get("errors"):
        return {**state, "next_action": "fix_errors"}
    if state.get("nominal_pass") or state.get("review_pass"):
        return {**state, "next_action": "run_design_audit"}
    return {**state, "next_action": "prepare_agent_review"}


def route_after_nominal(state: DesignFlowState) -> Literal["review", "audit"]:
    if state.get("nominal_pass") or state.get("review_pass"):
        return "audit"
    return "review"


def run_design_audit_node(state: DesignFlowState) -> DesignFlowState:
    report = run_design_audit(
        project=state["project_dir"],
        results_path=state["results_json"],
        netlist_path=state.get("final_netlist"),
        topology_name=state.get("topology", ""),
    )
    return {
        **state,
        "audit_status": report["status"],
        "audit_blockers": report["blocker_count"],
        "audit_warnings": report["warning_count"],
        "audit_report": report["report_file"],
        "next_action": "prepare_agent_review" if report["status"] == "block" else "run_pvt",
    }


def route_after_audit(state: DesignFlowState) -> Literal["review", "pvt"]:
    return "review" if state.get("audit_status") == "block" else "pvt"


def prepare_review(state: DesignFlowState) -> DesignFlowState:
    project = Path(state["project_dir"])
    review_mode = (
        "audit_repair"
        if state.get("audit_status") == "block"
        else "failure_repair"
    )
    audit_note = ""
    if state.get("audit_status") == "block":
        audit_note = f" inspect `{state.get('audit_report')}`, then"
    return {
        **state,
        "review_mode": review_mode,
        "next_action": (
            f"prepare_agent_review:{review_mode}:{audit_note} "
            f"run `python review_optimization.py "
            f"--project {project} --workspace workspace --topology <topology> "
            "--prepare-agent-review`"
        ),
    }


def run_pvt_node(state: DesignFlowState) -> DesignFlowState:
    project = Path(state["project_dir"])
    pvt_results = project / "pvt" / "pvt_results.json"
    if state.get("run_pvt"):
        report = run_pvt_verification(
            results_path=state["results_json"],
            simulate=bool(state.get("simulate")),
            dry_run=not bool(state.get("simulate")),
            targets=state.get("pvt_targets"),
            profile=state.get("pvt_profile"),
        )
        return {
            **state,
            "pvt_pass": bool(report.get("pvt_pass")),
            "next_action": "check_pvt",
        }
    if pvt_results.exists():
        data = json.loads(pvt_results.read_text(encoding="utf-8"))
        return {
            **state,
            "pvt_pass": bool(data.get("pvt_pass")),
            "next_action": "check_pvt",
        }
    return {
        **state,
        "pvt_pass": None,
        "next_action": "run_pvt",
    }


def check_pvt(state: DesignFlowState) -> DesignFlowState:
    if state.get("pvt_pass") is True:
        return {**state, "next_action": "done"}
    if state.get("pvt_pass") is False:
        return {**state, "next_action": "inspect_pvt_report"}
    return {**state, "next_action": "run_pvt"}


def _select_final_netlist(
    results_path: Path,
    result_data: dict[str, Any],
) -> tuple[Path | None, str | None]:
    try:
        return select_export_netlist(results_path, result_data)
    except Exception:
        netlist_ref = result_data.get("netlist_file")
        if not netlist_ref:
            return None, None
        path = Path(netlist_ref)
        if not path.is_absolute():
            path = (results_path.parent / path).resolve()
        return path, "bo_best"


def _write_flow_outputs(state: DesignFlowState) -> None:
    project = Path(state["project_dir"])
    flow_dir = project / "flow"
    flow_dir.mkdir(parents=True, exist_ok=True)
    persisted_state = dict(state)
    pvt_targets = state.get("pvt_targets")
    if pvt_targets is not None:
        persisted_state["pvt_targets"] = pvt_targets.to_requirements_dict()["targets"]
        persisted_state["pvt_metric_goals"] = {
            name: goal.to_dict()
            for name, goal in pvt_targets.resolved_metric_goals().items()
        }
    pvt_profile = state.get("pvt_profile")
    if pvt_profile is not None:
        persisted_state["pvt_profile"] = pvt_profile.to_dict()
    (flow_dir / "flow_state.json").write_text(
        json.dumps(persisted_state, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    (flow_dir / "flow_report.md").write_text(
        _render_flow_report(state),
        encoding="utf-8",
    )


def _render_flow_report(state: DesignFlowState) -> str:
    lines = [
        "# Design Flow Report",
        "",
        f"- LangGraph available: `{state.get('langgraph_available')}`",
        f"- Project: `{state.get('project_dir')}`",
        f"- Results: `{state.get('results_json')}`",
        f"- Nominal pass: `{state.get('nominal_pass')}`",
        f"- Review available: `{state.get('review_available')}`",
        f"- Review pass: `{state.get('review_pass')}`",
        f"- Review mode: `{state.get('review_mode')}`",
        f"- Design audit: `{state.get('audit_status')}`",
        f"- Audit blockers: `{state.get('audit_blockers')}`",
        f"- Audit warnings: `{state.get('audit_warnings')}`",
        f"- Audit report: `{state.get('audit_report')}`",
        f"- Final source: `{state.get('final_source')}`",
        f"- Final netlist: `{state.get('final_netlist')}`",
        f"- PVT requested: `{state.get('pvt_requested')}`",
        f"- PVT pass: `{state.get('pvt_pass')}`",
        f"- Schematic status: `{state.get('schematic_status')}`",
        f"- Schematic handoff: `{state.get('schematic_handoff')}`",
        f"- Schematic plan: `{state.get('schematic_plan')}`",
        f"- Schematic SKILL: `{state.get('schematic_skill')}`",
        f"- Schematic blocker: `{state.get('schematic_blocker')}`",
        f"- Physical status: `{state.get('physical_status')}`",
        f"- Physical handoff: `{state.get('physical_handoff')}`",
        f"- Physical layout: `{state.get('physical_layout_plan')}`",
        f"- Physical GDS: `{state.get('physical_gds')}`",
        f"- Physical DRC violations: `{state.get('physical_drc_violations')}`",
        f"- Physical LVS issues: `{state.get('physical_lvs_issues')}`",
        f"- Physical ECO iterations: `{state.get('physical_eco_iterations')}`",
        f"- Physical blocker: `{state.get('physical_blocker')}`",
        f"- Next action: `{state.get('next_action')}`",
    ]
    if state.get("errors"):
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {err}" for err in state["errors"])
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    state = run_design_flow(
        project=args.project,
        run_pvt=args.run_pvt,
        simulate=args.simulate,
        prepare_schematic=args.prepare_schematic or args.import_schematic,
        import_schematic=args.import_schematic,
        prepare_physical=args.prepare_physical or args.run_signoff,
        run_signoff=args.run_signoff,
        max_eco_iterations=args.max_eco_iterations,
        lib_name=args.lib,
    )
    print(f"Flow report: {Path(state['project_dir']) / 'flow' / 'flow_report.md'}")
    print(f"Next action: {state.get('next_action')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Orchestrate BO -> Review -> PVT and implementation.")
    parser.add_argument("--project", required=True, help="outputs/<project> directory")
    parser.add_argument("--run-pvt", action="store_true")
    parser.add_argument("--simulate", action="store_true")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare-schematic", action="store_true")
    action.add_argument("--import-schematic", action="store_true")
    action.add_argument("--prepare-physical", action="store_true")
    action.add_argument("--run-signoff", action="store_true")
    parser.add_argument("--max-eco-iterations", type=int, default=5)
    parser.add_argument("--lib", default="BO_Designs")
    return parser.parse_args()


if __name__ == "__main__":
    main()
