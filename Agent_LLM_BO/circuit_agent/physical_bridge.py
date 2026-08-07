"""Bridge the existing BO/Review/PVT state to the embedded physical backend."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

from virtuoso_export.parser import parse_netlist


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from analogskills.imported_design import (  # noqa: E402
    PhysicalAdapterRequired,
    build_imported_design_handoff,
    import_prepared_schematic,
    prepare_imported_schematic,
    prepare_imported_physical_run,
    run_imported_design_signoff,
)


def execute_schematic_from_state(
    state: dict[str, Any],
    *,
    prepare_schematic: bool,
    import_schematic: bool,
) -> dict[str, Any]:
    if not (prepare_schematic or import_schematic):
        return state
    if not (state.get("nominal_pass") or state.get("review_pass")) or state.get("audit_status") == "block":
        return {**state, "schematic_requested": True}
    if state.get("pvt_pass") is not True:
        return {**state, "next_action": "run_pvt" if state.get("pvt_pass") is None else "inspect_pvt_report"}
    final_netlist = state.get("final_netlist")
    if not final_netlist:
        return _schematic_error(state, "final netlist is unavailable")

    project = Path(str(state["project_dir"])).resolve()
    output_root = project / "schematic"
    try:
        ir = parse_netlist(Path(str(final_netlist)).resolve())
        topology = str(state.get("topology") or ir.subckt_name)
        handoff = build_imported_design_handoff(
            project_dir=project,
            topology=topology,
            final_netlist=final_netlist,
            final_source=str(state.get("final_source") or "bo_best"),
            pvt_results=project / "pvt" / "pvt_results.json",
            schematic_ir=ir,
            output_dir=output_root,
        )
        prepared = prepare_imported_schematic(
            handoff,
            output_root=output_root,
            lib_name=str(state.get("lib_name", "BO_Designs")),
        )
        result = import_prepared_schematic(prepared) if import_schematic else prepared
    except PhysicalAdapterRequired as exc:
        return _schematic_error(state, str(exc), "physical_adapter_required")
    except Exception as exc:
        return _schematic_error(state, str(exc))

    blocker = "; ".join(str(item) for item in result.errors if str(item)) or None
    return {
        **state,
        "schematic_requested": True,
        "schematic_status": result.status,
        "schematic_root": result.schematic_root,
        "schematic_handoff": result.handoff_path,
        "schematic_plan": result.oa_plan_path,
        "schematic_skill": result.skill_path,
        "schematic_import_skill": result.import_skill_path,
        "schematic_blocker": blocker,
        "next_action": "done" if result.imported else ("import_schematic" if result.status == "prepared" else "fix_schematic_blocker"),
    }


def execute_physical_from_state(
    state: dict[str, Any],
    *,
    prepare_physical: bool,
    run_signoff: bool,
    max_eco_iterations: int,
) -> dict[str, Any]:
    if not (prepare_physical or run_signoff):
        return state
    if not (state.get("nominal_pass") or state.get("review_pass")) or state.get("audit_status") == "block":
        return {**state, "physical_requested": True}
    if state.get("pvt_pass") is not True:
        return {**state, "next_action": "run_pvt" if state.get("pvt_pass") is None else "inspect_pvt_report"}
    final_netlist = state.get("final_netlist")
    if not final_netlist:
        return _physical_error(state, "final netlist is unavailable", "fix_physical_blocker")

    project = Path(str(state["project_dir"])).resolve()
    physical_root = project / "physical"
    try:
        ir = parse_netlist(Path(str(final_netlist)))
        topology = str(state.get("topology") or ir.subckt_name)
        handoff = build_imported_design_handoff(
            project_dir=project,
            topology=topology,
            final_netlist=final_netlist,
            final_source=str(state.get("final_source") or "bo_best"),
            pvt_results=project / "pvt" / "pvt_results.json",
            schematic_ir=ir,
            output_dir=physical_root,
        )
        prepared = prepare_imported_physical_run(
            handoff,
            physical_root=physical_root,
            lib_name=str(state.get("lib_name", "BO_Designs")),
        )
        result = (
            run_imported_design_signoff(
                prepared,
                max_eco_iterations=max_eco_iterations,
            )
            if run_signoff
            else prepared
        )
    except PhysicalAdapterRequired as exc:
        return _physical_error(state, str(exc), "physical_adapter_required")
    except Exception as exc:
        return _physical_error(state, str(exc), "fix_physical_blocker")

    blocker = "; ".join(str(item) for item in result.errors if str(item)) or None
    return {
        **state,
        "physical_requested": True,
        "physical_status": result.status,
        "physical_root": result.physical_root,
        "physical_handoff": result.handoff_path,
        "physical_layout_plan": result.layout_plan_path,
        "physical_gds": result.gds_path,
        "physical_drc_report": result.drc_report_path,
        "physical_lvs_report": result.lvs_report_path,
        "physical_drc_violations": result.drc_violations,
        "physical_lvs_issues": result.lvs_issues,
        "physical_eco_iterations": result.eco_iterations,
        "physical_blocker": blocker,
        "next_action": "done" if result.passed else ("run_signoff" if result.status == "prepared" else "fix_physical_blocker"),
    }


def _physical_error(state: dict[str, Any], message: str, action: str) -> dict[str, Any]:
    errors = list(state.get("errors", []))
    errors.append(message)
    return {
        **state,
        "physical_requested": True,
        "physical_status": "physical_blocked",
        "physical_blocker": message,
        "errors": errors,
        "next_action": action,
    }


def _schematic_error(state: dict[str, Any], message: str, action: str = "fix_schematic_blocker") -> dict[str, Any]:
    errors = list(state.get("errors", []))
    errors.append(message)
    return {
        **state,
        "schematic_requested": True,
        "schematic_status": "schematic_blocked",
        "schematic_blocker": message,
        "errors": errors,
        "next_action": action,
    }
