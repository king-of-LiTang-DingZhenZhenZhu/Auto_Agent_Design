"""LangGraph-style orchestration for BO -> Review -> PVT -> Virtuoso export."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Literal, TypedDict

from design_audit import run_design_audit
from pvt_simulation import run_pvt_verification
from virtuoso_export.exporter import export_from_results, select_export_netlist

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
    export_virtuoso: bool
    lib_name: str
    nominal_pass: bool | None
    review_available: bool
    review_pass: bool | None
    audit_status: str | None
    audit_blockers: int
    audit_warnings: int
    audit_report: str | None
    pvt_requested: bool
    pvt_pass: bool | None
    final_source: str | None
    final_netlist: str | None
    virtuoso_skill: str | None
    next_action: str
    errors: list[str]
    langgraph_available: bool


def run_design_flow(
    project: str | Path,
    run_pvt: bool = False,
    simulate: bool = False,
    export_virtuoso: bool = False,
    lib_name: str = "BO_Designs",
) -> DesignFlowState:
    project_dir = Path(project)
    initial: DesignFlowState = {
        "project_dir": str(project_dir),
        "results_json": str(project_dir / "results.json"),
        "run_pvt": run_pvt,
        "simulate": simulate,
        "export_virtuoso": export_virtuoso,
        "lib_name": lib_name,
        "errors": [],
        "pvt_requested": run_pvt,
        "langgraph_available": StateGraph is not None,
    }
    if StateGraph is None:
        state = _run_fallback(initial)
    else:
        graph = _build_graph()
        state = graph.invoke(initial)
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
    graph.add_node("export_virtuoso", export_virtuoso_node)

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
    graph.add_conditional_edges(
        "check_pvt",
        route_after_pvt,
        {
            "export": "export_virtuoso",
            "done": END,
        },
    )
    graph.add_edge("export_virtuoso", END)
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
    if route_after_pvt(state) == "export":
        state = export_virtuoso_node(state)
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
    audit_note = ""
    if state.get("audit_status") == "block":
        audit_note = f" inspect `{state.get('audit_report')}`, then"
    return {
        **state,
        "next_action": (
            f"prepare_agent_review:{audit_note} run `python review_optimization.py "
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
        if state.get("export_virtuoso"):
            return {**state, "next_action": "export_virtuoso"}
        return {**state, "next_action": "ready_to_export_virtuoso"}
    if state.get("pvt_pass") is False:
        return {**state, "next_action": "inspect_pvt_report"}
    return {**state, "next_action": "run_pvt"}


def route_after_pvt(state: DesignFlowState) -> Literal["export", "done"]:
    if state.get("pvt_pass") is True and state.get("export_virtuoso"):
        return "export"
    return "done"


def export_virtuoso_node(state: DesignFlowState) -> DesignFlowState:
    report = export_from_results(
        results_path=state["results_json"],
        lib_name=state.get("lib_name", "BO_Designs"),
    )
    return {
        **state,
        "virtuoso_skill": report["skill_file"],
        "next_action": "done",
    }


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
    (flow_dir / "flow_state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False, default=str),
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
        f"- Design audit: `{state.get('audit_status')}`",
        f"- Audit blockers: `{state.get('audit_blockers')}`",
        f"- Audit warnings: `{state.get('audit_warnings')}`",
        f"- Audit report: `{state.get('audit_report')}`",
        f"- Final source: `{state.get('final_source')}`",
        f"- Final netlist: `{state.get('final_netlist')}`",
        f"- PVT requested: `{state.get('pvt_requested')}`",
        f"- PVT pass: `{state.get('pvt_pass')}`",
        f"- Virtuoso SKILL: `{state.get('virtuoso_skill')}`",
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
        export_virtuoso=args.export_virtuoso,
        lib_name=args.lib,
    )
    print(f"Flow report: {Path(state['project_dir']) / 'flow' / 'flow_report.md'}")
    print(f"Next action: {state.get('next_action')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Orchestrate BO -> Review -> PVT -> Virtuoso export."
    )
    parser.add_argument("--project", required=True, help="outputs/<project> directory")
    parser.add_argument("--run-pvt", action="store_true")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--export-virtuoso", action="store_true")
    parser.add_argument("--lib", default="BO_Designs")
    return parser.parse_args()


if __name__ == "__main__":
    main()
