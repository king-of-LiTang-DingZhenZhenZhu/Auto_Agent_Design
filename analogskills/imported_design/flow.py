from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
from typing import Any, Mapping

from analogskills.contracts import (
    Device,
    DeviceRole,
    LayoutConstraintSet,
    MatchGroup,
    NetRole,
    RoutingConstraint,
    TerminalRef,
    TopologyGraph,
)
from analogskills.eda.calibre import make_calibre_drc_command, make_calibre_lvs_command
from analogskills.eda.command import EdaCommand, EdaRunResult, run_eda_command
from analogskills.eda.netlist import export_lvs_netlist
from analogskills.eda.oa import (
    OaPath,
    OaWritePlan,
    build_lvs_pins,
    build_oa_schematic_plan,
    merge_oa_write_plans,
    save_oa_plan_json,
    write_oa_skill,
)
from analogskills.eda.reports import parse_drc_report, parse_lvs_report
from analogskills.eda.skill_server import VirtuosoSkillClient, run_skill_file
from analogskills.eda.virtuoso import (
    build_layout_streamout_plan,
    make_strmout_command,
    make_virtuoso_batch_command,
    write_virtuoso_session_skill,
)
from analogskills.layout.placement import Placement
from analogskills.layout.min_router import StrapRouterConfig, build_strap_interconnect_result
from analogskills.layout.physical import analyze_plan_physical_connectivity
from analogskills.layout.power import (
    SupplyTapSpec,
    build_supply_tap_plan_from_specs,
    physical_plan_bbox_um,
    plan_guard_ring,
    plan_power_rails,
    plan_power_source_drops,
    plan_supply_taps,
    plan_well_regions,
)
from analogskills.layout.routing import generate_interconnect
from analogskills.pcell.generation import build_pcell_oa_layout_plan, generate_pcell_layout_plan
from analogskills.pcell.calibre_calibration import build_crn28_mos_multifinger_access_plan
from analogskills.pdk import resolve_pdk_config
from analogskills.repair.calibre_eco_closure import (
    calibre_eco_closure_loop_summary,
    run_calibre_eco_closure_loop,
)

from .schema import ImportedDesignHandoff
from .eco import accept_eco_candidate
from .physical_intent import PhysicalIntentError, solve_imported_physical_smt


@dataclass(frozen=True)
class ImportedPhysicalResult:
    status: str
    physical_root: str
    handoff_path: str
    layout_plan_path: str
    layout_skill_path: str
    schematic_skill_path: str
    lvs_source_path: str
    gds_path: str
    drc_report_path: str = ""
    lvs_report_path: str = ""
    drc_violations: int | None = None
    lvs_issues: int | None = None
    eco_iterations: int = 0
    errors: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "done" and self.drc_violations == 0 and self.lvs_issues == 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImportedSchematicResult:
    status: str
    schematic_root: str
    handoff_path: str
    oa_plan_path: str
    skill_path: str
    import_skill_path: str
    lib_name: str
    cell_name: str
    errors: tuple[str, ...] = ()

    @property
    def imported(self) -> bool:
        return self.status == "imported"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compile_imported_design(handoff: ImportedDesignHandoff) -> tuple[TopologyGraph, dict[str, dict[str, Any]]]:
    handoff.validate()
    graph = TopologyGraph(handoff.subckt_name)
    for port in handoff.ports:
        graph.add_pin(port, _net_role(handoff.net_roles.get(port, "internal")))
    model_map = {"nch_mac": "nmos", "nch_lvt_mac": "nmos", "pch_mac": "pmos", "pch_lvt_mac": "pmos"}
    for item in handoff.devices:
        if item.kind == "mos":
            model = model_map.get(item.model.lower())
            if model is None:
                raise ValueError(f"unsupported MOS model {item.model!r}")
        else:
            model = "resistor" if item.kind == "res" else "capacitor"
        graph.add_device(
            Device(item.name, _device_role(item.role), model, item.terminals, dict(item.parameters), notes=f"frontend_model={item.model}")
        )
    net_terms: dict[str, list[TerminalRef]] = {name: [] for name in handoff.net_roles}
    for item in handoff.devices:
        for terminal, net in zip(item.terminals, item.nodes):
            net_terms.setdefault(net, []).append(TerminalRef(item.name, terminal))
    for port in handoff.ports:
        net_terms.setdefault(port, []).append(TerminalRef(port, "PIN"))
    for name, terminals in net_terms.items():
        graph.add_net(name, _net_role(handoff.net_roles.get(name, "internal")), terminals)
    graph.layout_constraints = LayoutConstraintSet(
        matched_groups=tuple(
            MatchGroup(
                str(item["name"]),
                tuple(str(v) for v in item["devices"]),
                str(item.get("style", "mirror")),
                bool(item.get("require_dummies", True)),
                int(item.get("unit_segments", 1)),
            )
            for item in handoff.matched_groups
        ),
        symmetry_groups=handoff.symmetry_groups,
        routing=tuple(
            RoutingConstraint(
                str(item["net"]),
                str(item["kind"]),
                tuple(item["value"]) if isinstance(item.get("value"), list) else item.get("value", ""),
                str(item.get("reason", "")),
            )
            for item in handoff.routing_constraints
        ),
        critical_nets=handoff.critical_nets,
    )
    issues = graph.validate()
    if issues:
        raise ValueError("invalid imported topology: " + "; ".join(issues))
    sizing = {item.name: dict(item.parameters) for item in handoff.devices}
    return graph, sizing


def prepare_imported_schematic(
    handoff: ImportedDesignHandoff | str | Path,
    *,
    output_root: str | Path,
    lib_name: str = "BO_Designs",
) -> ImportedSchematicResult:
    """Prepare the qualified frontend schematic without generating layout."""
    handoff_obj = ImportedDesignHandoff.read_json(handoff) if isinstance(handoff, (str, Path)) else handoff
    handoff_obj.validate()
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    handoff_path = handoff_obj.write_json(root / "handoff.json")
    graph, sizing = compile_imported_design(handoff_obj)
    pdk = resolve_pdk_config("crn28hpcp")
    plan = build_oa_schematic_plan(
        graph,
        lib=lib_name,
        cell=handoff_obj.subckt_name,
        sizing=sizing,
        pdk=pdk,
    )
    plan_path = save_oa_plan_json(plan, root / "schematic.oa_plan.json")
    skill_path = write_oa_skill(
        plan,
        root / "schematic.il",
        replace_cellview=True,
        exit_after_write=False,
        tech_lib=pdk.pcell_template_for("nmos").lib_name,
    )
    import_skill = write_virtuoso_session_skill(root / "import_schematic.il", (skill_path,))
    result = ImportedSchematicResult(
        status="prepared",
        schematic_root=str(root),
        handoff_path=str(handoff_path),
        oa_plan_path=str(plan_path),
        skill_path=str(skill_path),
        import_skill_path=str(import_skill),
        lib_name=str(lib_name),
        cell_name=handoff_obj.subckt_name,
    )
    _write_json(
        root / "schematic_manifest.json",
        {
            "schema": "analogskills.imported_schematic/v1",
            **result.to_dict(),
            "input_netlist_sha256": handoff_obj.final_netlist_sha256,
            "pvt_results_sha256": handoff_obj.pvt_results_sha256,
        },
    )
    return result


def import_prepared_schematic(prepared: ImportedSchematicResult) -> ImportedSchematicResult:
    """Load a prepared complete schematic through the persistent CIW or batch fallback."""
    root = Path(prepared.schematic_root)
    config = _schematic_preflight(root, prepared.lib_name)
    manifest_path = root / "schematic_manifest.json"
    manifest = _read_json_mapping(manifest_path)
    record = _run_cached_oa_stage(
        root=root,
        name="schematic_oa",
        components=(Path(prepared.skill_path),),
        live_skill=root / "import_schematic_live.il",
        batch_skill=Path(prepared.import_skill_path),
        state_path=root / "oa_stage_state.json",
        fingerprint_extra=json.dumps(
            {"lib": prepared.lib_name, "cell": prepared.cell_name, "view": "schematic"},
            sort_keys=True,
        ),
        config=config,
    )
    status = "imported" if record["ok"] else "schematic_blocked"
    errors = () if record["ok"] else (str(record.get("stderr_tail") or "schematic import failed"),)
    result = replace(prepared, status=status, errors=errors)
    manifest.update({"status": status, "runs": [record], "errors": list(errors)})
    _write_json(manifest_path, manifest)
    return result


def prepare_imported_physical_run(
    handoff: ImportedDesignHandoff | str | Path,
    *,
    physical_root: str | Path | None = None,
    lib_name: str = "BO_Designs",
    placement_mode: str = "smt",
    smt_timeout_ms: int = 30_000,
) -> ImportedPhysicalResult:
    handoff_obj = ImportedDesignHandoff.read_json(handoff) if isinstance(handoff, (str, Path)) else handoff
    handoff_obj.validate()
    root = (
        Path(physical_root)
        if physical_root is not None
        else Path(handoff_obj.final_netlist).parents[1]
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    layout_dir = root / "layout"
    oa_dir = root / "oa"
    lvs_dir = root / "lvs"
    drc_dir = root / "signoff" / "drc"
    lvs_signoff_dir = root / "signoff" / "lvs"
    eco_dir = root / "eco"
    for directory in (layout_dir, oa_dir, lvs_dir, drc_dir, lvs_signoff_dir, eco_dir):
        directory.mkdir(parents=True, exist_ok=True)

    handoff_path = handoff_obj.write_json(root / "handoff.json")
    graph, sizing = compile_imported_design(handoff_obj)
    pdk = resolve_pdk_config("crn28hpcp")
    physical_sizing = _physical_pcell_sizing(handoff_obj, sizing)
    physical_smt = None
    signoff_eligible = True
    normalized_placement_mode = str(placement_mode or "smt").strip().lower()
    if handoff_obj.topology == "two_stage_ota" and normalized_placement_mode == "smt":
        try:
            physical_smt = solve_imported_physical_smt(
                graph,
                physical_sizing,
                topology=handoff_obj.topology,
                pdk=pdk,
                solver_timeout_ms=smt_timeout_ms,
            )
        except PhysicalIntentError:
            raise
        placements = physical_smt.placements
        _write_json(layout_dir / "design_intent.json", physical_smt.intent.to_dict())
        _write_json(layout_dir / "smt_solution.json", physical_smt.solution_dict())
        _write_json(
            layout_dir / "routing_resources.json",
            {
                "schema": "analogskills.routing_resource_assignments/v1",
                "assignments": {
                    name: dict(row) for name, row in physical_smt.route_resource_assignments.items()
                },
            },
        )
    elif normalized_placement_mode == "legacy_seed_debug":
        placements = _imported_seed_placements(handoff_obj)
        signoff_eligible = handoff_obj.topology != "two_stage_ota"
        _write_json(
            layout_dir / "smt_solution.json",
            {
                "schema": "analogskills.imported_physical_smt_solution/v1",
                "passed": False,
                "placement_mode": "legacy_seed_debug",
                "signoff_eligible": signoff_eligible,
                "reason": "legacy seed is retained for debug comparison only",
            },
        )
    elif handoff_obj.topology == "two_stage_ota":
        raise ValueError("two_stage_ota placement_mode must be 'smt' or 'legacy_seed_debug'")
    else:
        placements = _imported_seed_placements(handoff_obj)
    pcell_plan = generate_pcell_layout_plan(
        graph,
        physical_sizing,
        pdk=pdk,
        placements=placements,
        strict=True,
        include_fallback_shapes=False,
    )
    mos_access = build_crn28_mos_multifinger_access_plan(
        pdk,
        pcell_plan,
        lib=lib_name,
        cell=handoff_obj.subckt_name,
    )
    pcell_plan = _attach_crn28_mos_access_metadata(pcell_plan, mos_access)
    if any(str(getattr(inst, "instantiation_method", "")) == "drawn_primitive" for inst in pcell_plan.instances):
        raise ValueError("sign-off package cannot contain drawn/fallback primitive devices")
    cell = handoff_obj.subckt_name
    device_plan = build_pcell_oa_layout_plan(pcell_plan, lib=lib_name, cell=cell, pdk=pdk, include_fallback_shapes=False)
    device_plan = merge_oa_write_plans(device_plan, mos_access, cellview=device_plan.cellview, grid=pdk)
    if handoff_obj.topology == "two_stage_ota":
        layout_plan, physical_stages = _build_imported_two_stage_ota_layout(
            handoff_obj,
            pcell_plan,
            device_plan,
            pdk,
            lib_name=lib_name,
            cell=cell,
            route_resource_assignments=(physical_smt.route_resource_assignments if physical_smt is not None else {}),
        )
    else:
        interconnect = generate_interconnect(
            pcell_plan,
            graph.layout_constraints,
            pdk,
            lib=lib_name,
            cell=cell,
            # The embedded CRN28 profile carries calibrated coordinates in its
            # PCell templates, but the upstream analyzer labels template-backed
            # coordinates as fallback unless an external cache is also supplied.
            # Keep routing enabled here and let OA/Calibre remain the sign-off gate.
            strict_terminal_access=False,
            strict_routing=False,
            strict_top_level_nets=handoff_obj.ports,
        )
        supply = _supply_names(handoff_obj)
        rails = plan_power_rails(device_plan, pdk, lib=lib_name, cell=cell, top_net=supply[0], bottom_net=supply[1])
        drops = plan_power_source_drops(
            pcell_plan,
            rails,
            pdk,
            lib=lib_name,
            cell=cell,
            supply_nets=tuple(net for net in supply if net),
            terminals=("S", "B"),
        )
        taps = plan_supply_taps(rails, pdk, lib=lib_name, cell=cell, top_net=supply[0], bottom_net=supply[1])
        wells = plan_well_regions(pcell_plan, pdk, lib=lib_name, cell=cell)
        guard = plan_guard_ring(device_plan, pdk, lib=lib_name, cell=cell, net=supply[1] or "vss")
        layout_plan = merge_oa_write_plans(device_plan, interconnect, rails, drops, taps, wells, guard, cellview=device_plan.cellview, grid=pdk)
        physical_stages = {}
    pins = build_lvs_pins(
        layout_plan,
        pdk,
        top_level_nets=handoff_obj.ports,
        top_level_pin_nets={name: name for name in handoff_obj.ports},
        allow_placeholder_pins=False,
        pin_selection_policy="boundary_aware",
    )
    if len(pins) != len(handoff_obj.ports):
        raise ValueError(f"layout routing did not produce all top-level pins: {len(pins)}/{len(handoff_obj.ports)}")
    labels = tuple((pin.layer, pin.name, ((pin.bbox[0] + pin.bbox[2]) / 2, (pin.bbox[1] + pin.bbox[3]) / 2)) for pin in pins if pin.bbox)
    layout_plan = replace(layout_plan, pins=pins, labels=labels)

    if handoff_obj.topology == "two_stage_ota":
        physical_stages["final_with_pins"] = _physical_connectivity_report(layout_plan, pdk)
        if physical_smt is not None:
            physical_stages["final_with_pins"]["constraint_realization"] = {
                "complete": bool(physical_smt.compiled.checks.get("constraint_realization_complete", False)),
                "matching": {
                    name: dict(row) for name, row in physical_smt.matching_realization.items()
                },
                "route_resource_assignment_count": len(physical_smt.route_resource_assignments),
                "route_resource_capacity_overflow": 0,
            }
        _write_json(layout_dir / "physical_precheck_stages.json", physical_stages)
        failed_stages = [name for name, report in physical_stages.items() if not report["passed"]]
        if failed_stages:
            final_report = physical_stages[failed_stages[-1]]
            raise ValueError(
                "physical connectivity preparation failed at "
                f"{','.join(failed_stages)}: {len(final_report['shorts'])} short(s), "
                f"{len(final_report['opens'])} open net(s)"
            )

    layout_plan_path = save_oa_plan_json(layout_plan, layout_dir / "layout.oa_plan.json")
    layout_skill = write_oa_skill(
        layout_plan,
        oa_dir / "layout.il",
        grid=pdk,
        validate_grid=True,
        validate_lvs_stamping=True,
        top_level_nets=handoff_obj.ports,
        require_lvs_labels=True,
        replace_cellview=True,
        exit_after_write=False,
    )
    schematic_plan = build_oa_schematic_plan(graph, lib=lib_name, cell=cell, sizing=sizing, pdk=pdk)
    schematic_skill = write_oa_skill(
        schematic_plan,
        oa_dir / "schematic.il",
        replace_cellview=True,
        exit_after_write=False,
        tech_lib=pdk.pcell_template_for("nmos").lib_name,
    )
    lvs_source = export_lvs_netlist(
        graph,
        sizing,
        lvs_dir / "source.cdl",
        subckt_name=cell,
        model_map={"nmos": "nch_mac", "pmos": "pch_mac", "resistor": "rnodl", "capacitor": "nmoscap"},
        require_model_map=True,
        mos_expansion="finger",
        passive_device_style="subckt",
    )
    gds_path = layout_dir / f"{cell}.gds"
    stream_plan = build_layout_streamout_plan(
        lib=lib_name,
        cell=cell,
        output_path=gds_path,
        skill_path=oa_dir / "streamout.il",
        binary=os.environ.get("ANALOGSKILLS_VIRTUOSO_BINARY", "virtuoso"),
        cwd=root,
        exit_after_export=False,
    )
    oa_batch_skill = write_virtuoso_session_skill(
        oa_dir / "write_all.il",
        (schematic_skill, layout_skill),
    )
    mapping = _realization_mapping(handoff_obj, pcell_plan, lvs_source)
    (root / "instance_mapping.json").write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest = {
        "schema": "auto_agent_design.physical_run_manifest/v1",
        "status": "prepared",
        "project": handoff_obj.project,
        "topology": handoff_obj.topology,
        "cellview": {"lib": lib_name, "cell": cell, "schematic": "schematic", "layout": "layout"},
        "input_netlist_sha256": handoff_obj.final_netlist_sha256,
        "pvt_results_sha256": handoff_obj.pvt_results_sha256,
        "artifacts": {
            "handoff": str(handoff_path), "layout_plan": str(layout_plan_path), "layout_skill": str(layout_skill),
            "schematic_skill": str(schematic_skill), "oa_batch_skill": str(oa_batch_skill),
            "streamout_skill": stream_plan.skill_path,
            "lvs_source": str(lvs_source), "gds": str(gds_path),
            **(
                {
                    "design_intent": str(layout_dir / "design_intent.json"),
                    "smt_solution": str(layout_dir / "smt_solution.json"),
                    "routing_resources": str(layout_dir / "routing_resources.json"),
                }
                if physical_smt is not None
                else {}
            ),
        },
        "runtime": _runtime_manifest(),
        "physical_planning": {
            "placement_mode": normalized_placement_mode,
            "signoff_eligible": signoff_eligible,
            "solver": "z3" if physical_smt is not None else "none",
            "constraint_realization_complete": bool(
                physical_smt and physical_smt.compiled.checks.get("constraint_realization_complete", False)
            ),
        },
    }
    _write_json(root / "run_manifest.json", manifest)
    result = ImportedPhysicalResult(
        "prepared", str(root), str(handoff_path), str(layout_plan_path), str(layout_skill),
        str(schematic_skill), str(lvs_source), str(gds_path),
    )
    _persist_state(result)
    return result


def run_imported_design_signoff(
    prepared: ImportedPhysicalResult | ImportedDesignHandoff | str | Path,
    *,
    physical_root: str | Path | None = None,
    lib_name: str = "BO_Designs",
    max_eco_iterations: int = 5,
) -> ImportedPhysicalResult:
    if max_eco_iterations < 0 or max_eco_iterations > 5:
        raise ValueError("max_eco_iterations must be between 0 and 5")
    if isinstance(prepared, ImportedPhysicalResult):
        base = prepared
    else:
        base = prepare_imported_physical_run(prepared, physical_root=physical_root, lib_name=lib_name)
    root = Path(base.physical_root)
    manifest = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
    if not bool(dict(manifest.get("physical_planning", {})).get("signoff_eligible", True)):
        return _signoff_failure(base, [], "legacy_seed_debug is not eligible for sign-off")
    config = _preflight(root, str(manifest["cellview"]["lib"]))
    cell = str(manifest["cellview"]["cell"])
    pdk = resolve_pdk_config("crn28hpcp")

    runs: list[dict[str, Any]] = []
    physical_precheck = analyze_plan_physical_connectivity(
        _load_oa_plan(base.layout_plan_path),
        pdk=pdk,
        include_via_landing_shorts=True,
        include_opens=True,
    )
    _write_json(root / "signoff" / "physical_precheck.json", physical_precheck)
    if not physical_precheck["passed"]:
        return _signoff_failure(
            base,
            runs,
            "physical connectivity precheck failed: "
            f"{len(physical_precheck['shorts'])} short(s), "
            f"{len(physical_precheck['opens'])} open net(s)",
        )
    oa_record = _run_oa_write_stage(root, manifest, config)
    runs.append(oa_record)
    if not oa_record["ok"]:
        return _signoff_failure(base, runs, "oa_write failed")
    stream_run = run_eda_command(_streamout_command(root, cell, base, config))
    runs.append(_run_record("streamout", stream_run))
    if not stream_run.ok:
        return _signoff_failure(base, runs, "streamout failed")
    gds = Path(base.gds_path)
    if not gds.is_file() or gds.stat().st_size == 0:
        return _signoff_failure(base, runs, "Virtuoso stream-out did not produce a non-empty GDS")

    drc_deck, drc_results, drc_summary = _materialize_drc_deck(root, cell, gds, Path(config["drc_deck"]), pdk)
    lvs_deck, lvs_report = _materialize_lvs_deck(root, cell, gds, Path(base.lvs_source_path), Path(config["lvs_deck"]), pdk)
    drc_run = run_eda_command(EdaCommand(make_calibre_drc_command(drc_deck, binary=config["calibre"]).command, cwd=drc_deck.parent, timeout_s=1800.0))
    runs.append(_run_record("calibre_drc", drc_run))
    lvs_run = run_eda_command(EdaCommand(make_calibre_lvs_command(lvs_deck, binary=config["calibre"]).command, cwd=lvs_deck.parent, timeout_s=1800.0))
    runs.append(_run_record("calibre_lvs", lvs_run))
    if not drc_run.ok or not drc_results.is_file() or not lvs_run.ok or not lvs_report.is_file():
        return _signoff_failure(base, runs, "Calibre command or required report failed", drc_results, lvs_report)

    current_plan = _load_oa_plan(base.layout_plan_path)
    current_drc = tuple(parse_drc_report(drc_results))
    current_lvs = tuple(parse_lvs_report(lvs_report))
    latest: dict[str, Any] = {"drc": current_drc, "lvs": current_lvs, "plan": current_plan, "runs": runs}
    accepted_artifacts = root / "eco" / "accepted"
    _checkpoint_signoff_artifacts(gds, drc_results, drc_summary, lvs_report, accepted_artifacts)
    eco_summary: dict[str, Any] = {"iteration_count": 0, "converged": not current_drc}
    if (current_drc or current_lvs) and max_eco_iterations:
        def verify(decision: object, accepted_plan: object, index: int) -> Mapping[str, object]:
            candidate = merge_oa_write_plans(accepted_plan, getattr(decision, "patch"), cellview=accepted_plan.cellview, grid=pdk)
            iteration_dir = root / "eco" / f"{index:03d}"
            iteration_dir.mkdir(parents=True, exist_ok=True)
            skill = write_oa_skill(
                candidate,
                iteration_dir / "candidate.il",
                grid=pdk,
                replace_cellview=True,
                exit_after_write=False,
            )
            save_oa_plan_json(candidate, iteration_dir / "candidate.oa_plan.json")
            candidate_runs, candidate_drc, candidate_lvs = _rerun_candidate(
                root, iteration_dir, skill, cell, base, config, pdk,
            )
            _checkpoint_signoff_artifacts(gds, drc_results, drc_summary, lvs_report, iteration_dir)
            before_drc = len(latest["drc"])
            before_lvs = len(latest["lvs"])
            after_drc = len(candidate_drc)
            after_lvs = len(candidate_lvs)
            accepted = accept_eco_candidate(
                before_drc=before_drc,
                before_lvs=before_lvs,
                after_drc=after_drc,
                after_lvs=after_lvs,
                stages_ok=all(row["ok"] for row in candidate_runs),
            )
            reason = "strict_overall_improvement" if accepted else "candidate_rejected_no_strict_nonregressing_improvement"
            _write_json(iteration_dir / "verification.json", {
                "accepted": accepted, "reason": reason,
                "before": {"drc": before_drc, "lvs": before_lvs},
                "after": {"drc": after_drc, "lvs": after_lvs}, "runs": candidate_runs,
            })
            if accepted:
                latest.update({"drc": candidate_drc, "lvs": candidate_lvs, "plan": candidate})
                _checkpoint_signoff_artifacts(gds, drc_results, drc_summary, lvs_report, accepted_artifacts)
            else:
                rollback = write_oa_skill(
                    accepted_plan,
                    iteration_dir / "rollback.il",
                    grid=pdk,
                    replace_cellview=True,
                    exit_after_write=False,
                )
                rollback_batch = write_virtuoso_session_skill(iteration_dir / "rollback_batch.il", (rollback,))
                _run_virtuoso_skill_stage(root, "layout_rollback", rollback, rollback_batch, config)
                run_eda_command(_streamout_command(root, cell, base, config))
                _restore_signoff_artifacts(accepted_artifacts, gds, drc_results, drc_summary, lvs_report)
            return {"plan": candidate, "results": candidate_drc, "accepted": accepted, "acceptance_reason": reason, "artifacts": {}}

        closure = run_calibre_eco_closure_loop(
            current_plan,
            current_drc,
            pdk=pdk,
            config={"max_iterations": max_eco_iterations},
            apply_patch_and_verify=verify,
        )
        eco_summary = calibre_eco_closure_loop_summary(closure)
        _write_json(root / "eco" / "checkpoint_journal.json", eco_summary)

    save_oa_plan_json(latest["plan"], base.layout_plan_path)
    write_oa_skill(
        latest["plan"],
        base.layout_skill_path,
        grid=pdk,
        replace_cellview=True,
        exit_after_write=False,
    )

    drc_count = len(latest["drc"])
    lvs_count = len(latest["lvs"])
    status = "done" if drc_count == 0 and lvs_count == 0 else "physical_blocked"
    result = replace(
        base,
        status=status,
        drc_report_path=str(drc_results),
        lvs_report_path=str(lvs_report),
        drc_violations=drc_count,
        lvs_issues=lvs_count,
        eco_iterations=int(eco_summary.get("iteration_count", 0)),
        errors=() if status == "done" else ("DRC/LVS did not converge within the bounded ECO policy",),
    )
    manifest["status"] = status
    manifest["signoff"] = {"runs": runs, "drc_violations": drc_count, "lvs_issues": lvs_count, "eco": eco_summary}
    manifest["runtime"] = _runtime_manifest(config)
    _write_json(root / "run_manifest.json", manifest)
    _persist_state(result)
    return result


def _physical_pcell_sizing(
    handoff: ImportedDesignHandoff,
    sizing: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Fold Spectre multiplicity into native PCell fingers for extraction."""
    mos_names = {device.name for device in handoff.devices if device.kind == "mos"}
    result = {name: dict(params) for name, params in sizing.items()}
    for name in mos_names:
        params = result.get(name, {})
        multiplier = max(1, int(float(params.get("m", params.get("M", 1)) or 1)))
        if multiplier == 1:
            continue
        fingers = max(1, int(float(params.get("nf", 1) or 1)))
        width = float(params.get("W", params.get("w", 0.0)) or 0.0)
        if width <= 0.0:
            raise ValueError(f"{name} has invalid MOS width for physical multiplicity folding")
        params["W"] = width * multiplier
        params["nf"] = fingers * multiplier
        params["m"] = 1
    return result


def _attach_crn28_mos_access_metadata(pcell_plan: object, access_plan: OaWritePlan) -> object:
    terminal_kind = {
        "crn28_mos_gate_m1_bus": "G",
        "crn28_mos_source_m2_bus": "S",
        "crn28_mos_drain_m2_bus": "D",
        "crn28_mos_body_tap_m1": "B",
    }
    access_by_instance: dict[str, dict[str, dict[str, Any]]] = {}
    owned_shapes_by_instance: dict[str, list[dict[str, Any]]] = {}
    for rect in access_plan.rects:
        metadata = dict(getattr(rect, "metadata", {}) or {})
        terminal = terminal_kind.get(str(metadata.get("kind", "")))
        instance = str(metadata.get("instance", ""))
        if instance and str(rect.net):
            owned_shapes_by_instance.setdefault(instance, []).append(
                {
                    "layer": str(rect.layer),
                    "net": str(rect.net),
                    "bbox_um": tuple(float(value) for value in rect.bbox),
                    "kind": str(metadata.get("kind", "crn28_calibre_access")),
                }
            )
        if not terminal or not instance:
            continue
        access_by_instance.setdefault(instance, {})[terminal] = {
            "absolute_bbox_um": tuple(float(value) for value in rect.bbox),
            "layer": str(rect.layer),
            "contact_layer": "",
            "source": "crn28_calibre_access_plan",
            "confidence": 1.0,
            "lvs_safe": True,
            "access_priority": 0,
        }
    instances = tuple(
        replace(
            instance,
            metadata={
                **dict(getattr(instance, "metadata", {}) or {}),
                "terminal_access": access_by_instance.get(str(instance.name), {}),
                "routing_owned_shapes": owned_shapes_by_instance.get(str(instance.name), []),
            },
        )
        for instance in tuple(getattr(pcell_plan, "instances", ()) or ())
    )
    return replace(pcell_plan, instances=instances)


def _supply_names(handoff: ImportedDesignHandoff) -> tuple[str | None, str | None]:
    top = next((name for name, role in handoff.net_roles.items() if role == "supply"), None)
    bottom = next((name for name, role in handoff.net_roles.items() if role == "ground"), None)
    return top, bottom


def _build_imported_two_stage_ota_layout(
    handoff: ImportedDesignHandoff,
    pcell_plan: object,
    device_plan: OaWritePlan,
    pdk: object,
    *,
    lib_name: str,
    cell: str,
    route_resource_assignments: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[OaWritePlan, dict[str, dict[str, Any]]]:
    """Route the exact frontend OTA without replacing its electrical graph."""
    core_bbox = physical_plan_bbox_um(device_plan)
    if core_bbox is None:
        raise ValueError("two_stage_ota device plan has no physical bbox")
    x0, _y0, _x1, y1 = core_bbox
    route_y = y1 + 3.0
    pin_x = x0 - 29.0
    metals = tuple(str(layer) for layer in pdk.layer_map.metals[2:])
    if len(metals) < 2:
        raise ValueError("two_stage_ota physical adapter requires at least four metal layers")
    assignments = {
        str(name): dict(row) for name, row in dict(route_resource_assignments or {}).items()
    }
    layer_by_net = {
        name: str(row.get("layer", "")) for name, row in assignments.items() if str(row.get("layer", ""))
    }
    lane_by_net = {
        name: int(row.get("lane", 0)) for name, row in assignments.items() if row.get("lane") is not None
    }
    ordered_nets = tuple(
        name for name, _row in sorted(
            assignments.items(),
            key=lambda item: (str(item[1].get("layer", "")), int(item[1].get("lane", 0)), item[0]),
        )
    )
    route_result = build_strap_interconnect_result(
        pcell_plan,
        handoff.ports,
        pdk,
        lib=lib_name,
        cell=cell,
        config=StrapRouterConfig(
            local_net_prefixes=(),
            route_layers=metals,
            route_layer_strategy="cyclic",
            route_layer_by_net=layer_by_net,
            strap_lane_by_net=lane_by_net,
            global_net_order=ordered_nets,
            global_net_order_strategy="explicit_then_name",
            drop_route_layers=(pdk.layer_map.metals[1],),
            strap_y_start_um=route_y,
            strap_y_pitch_um=3.0,
            pin_origin_um=(pin_x, route_y),
            pin_pitch_um=3.0,
            pin_drop_x_start_um=pin_x + 5.0,
            pin_drop_x_pitch_um=-2.0,
            fanout_search_steps=60,
            fanout_y_search_steps=24,
            strap_landing_search_steps=24,
            maze_escape_enabled=True,
            maze_escape_search_steps=8,
            maze_escape_max_expansions=4096,
            connect_to_existing_net=True,
            existing_net_target_limit=64 if assignments else 24,
            existing_net_fanout_search_steps=24,
            existing_net_fanout_y_search_steps=24,
        ),
    )
    routed_core = merge_oa_write_plans(
        device_plan,
        route_result.plan,
        cellview=device_plan.cellview,
        grid=pdk,
    )
    stages = {"routed_core": _physical_connectivity_report(routed_core, pdk)}

    supply, ground = _supply_names(handoff)
    if not supply or not ground:
        raise ValueError("two_stage_ota physical adapter requires supply and ground nets")
    supply_trunk = _supply_trunk(route_result.plan, supply)
    ground_trunk = _supply_trunk(route_result.plan, ground)
    tap_size = 0.24
    taps = build_supply_tap_plan_from_specs(
        (
            SupplyTapSpec(
                supply,
                "nwell",
                supply_trunk[0],
                _centered_bbox(supply_trunk[0], tap_size),
                supply_trunk[1],
            ),
            SupplyTapSpec(
                ground,
                "substrate",
                ground_trunk[0],
                _centered_bbox(ground_trunk[0], tap_size),
                ground_trunk[1],
            ),
        ),
        pdk,
        lib=lib_name,
        cell=cell,
    )
    with_taps = merge_oa_write_plans(routed_core, taps, cellview=device_plan.cellview, grid=pdk)
    stages["supply_taps"] = _physical_connectivity_report(with_taps, pdk)

    wells = plan_well_regions(pcell_plan, pdk, lib=lib_name, cell=cell)
    with_wells = merge_oa_write_plans(with_taps, wells, cellview=device_plan.cellview, grid=pdk)
    stages["wells"] = _physical_connectivity_report(with_wells, pdk)

    guard = plan_guard_ring(
        with_wells,
        pdk,
        lib=lib_name,
        cell=cell,
        net=ground,
        bbox=physical_plan_bbox_um(with_wells),
    )
    ground_escape = _top_ground_escape(route_result.plan, ground, pdk.layer_map.metals[0])
    top_guard = max(
        (
            rect
            for rect in guard.rects
            if str(rect.layer) == str(pdk.layer_map.metals[0]) and str(rect.net) == ground
        ),
        key=lambda rect: float(rect.bbox[3]),
    )
    guard_y = 0.5 * (float(top_guard.bbox[1]) + float(top_guard.bbox[3]))
    bridge = OaWritePlan(
        device_plan.cellview,
        nets=(ground,),
        paths=(
            OaPath(
                str(pdk.layer_map.metals[0]),
                "drawing",
                (ground_escape, (ground_escape[0], guard_y)),
                pdk.rules.min_width_um(str(pdk.layer_map.metals[0])),
                ground,
            ),
        ),
    )
    final_plan = merge_oa_write_plans(
        with_wells,
        guard,
        bridge,
        cellview=device_plan.cellview,
        grid=pdk,
    )
    stages["guard_ring"] = _physical_connectivity_report(final_plan, pdk)
    return final_plan, stages


def _physical_connectivity_report(plan: object, pdk: object) -> dict[str, Any]:
    return dict(
        analyze_plan_physical_connectivity(
            plan,
            pdk=pdk,
            include_via_landing_shorts=True,
            include_instance_terminal_shorts=True,
            include_opens=True,
        )
    )


def _supply_trunk(plan: object, net: str) -> tuple[tuple[float, float], str]:
    candidates = []
    for path in tuple(getattr(plan, "paths", ()) or ()):
        points = tuple(getattr(path, "points", ()) or ())
        if str(getattr(path, "net", "")) != net or len(points) != 2:
            continue
        if abs(float(points[0][1]) - float(points[1][1])) > 1e-12:
            continue
        span = abs(float(points[1][0]) - float(points[0][0]))
        candidates.append((span, path))
    if not candidates:
        raise ValueError(f"two_stage_ota routing did not produce a supply trunk for {net}")
    path = max(candidates, key=lambda item: item[0])[1]
    point = min(
        (tuple(float(value) for value in xy) for xy in path.points),
        key=lambda xy: xy[0],
    )
    return point, str(path.layer)


def _top_ground_escape(plan: object, net: str, layer: str) -> tuple[float, float]:
    points = [
        tuple(float(value) for value in point)
        for path in tuple(getattr(plan, "paths", ()) or ())
        if str(getattr(path, "net", "")) == net and str(getattr(path, "layer", "")) == str(layer)
        for point in tuple(getattr(path, "points", ()) or ())
    ]
    if not points:
        raise ValueError(f"two_stage_ota routing did not produce a local ground escape for {net}")
    return max(points, key=lambda point: (point[1], -point[0]))


def _centered_bbox(point: tuple[float, float], size: float) -> tuple[float, float, float, float]:
    half = 0.5 * size
    return (point[0] - half, point[1] - half, point[0] + half, point[1] + half)


def _imported_seed_placements(handoff: ImportedDesignHandoff) -> tuple[Placement, ...]:
    """Exact v1 adapter seeds; physical optimization starts from these symmetries."""
    if handoff.topology == "two_stage_ota":
        rows = (
            (("Mmirr1", -10.0), ("Mmirr2", 10.0), ("Mcs", 30.0)),
            (("Mdiff1", -10.0), ("Mdiff2", 10.0), ("Rz", 30.0), ("Cc", 50.0)),
            (("Mbias", -30.0), ("Mtail", 0.0), ("Mload", 30.0)),
        )
    elif handoff.topology == "strongarm_latch":
        rows = (
            (("S1", -30.0), ("S2", -10.0), ("S3", 10.0), ("S4", 30.0)),
            (("M5", -20.0), ("M6", 20.0)),
            (("M3", -20.0), ("M4", 20.0)),
            (("M1", -20.0), ("M2", 20.0)),
            (("M7", 0.0),),
        )
    else:
        raise ValueError(f"no imported placement seed for {handoff.topology}")
    device_roles = {item.name: item.role for item in handoff.devices}
    placements = []
    for row_index, row in enumerate(rows):
        for name, x_um in row:
            placements.append(Placement(name, x_um, row_index * 20.0, role=device_roles[name]))
    return tuple(placements)


def _device_role(value: str) -> DeviceRole:
    try:
        return DeviceRole(value)
    except ValueError:
        return DeviceRole.UNKNOWN


def _net_role(value: str) -> NetRole:
    try:
        return NetRole(value)
    except ValueError:
        return NetRole.INTERNAL


def _realization_mapping(handoff: ImportedDesignHandoff, pcell_plan: object, lvs_source: str | Path) -> dict[str, Any]:
    realized: dict[str, list[str]] = {item.name: [] for item in handoff.devices}
    for instance in getattr(pcell_plan, "instances", ()):
        name = str(getattr(instance, "name"))
        owner = name if name in realized else next((candidate for candidate in realized if name.startswith(candidate + "_")), "")
        if owner:
            realized[owner].append(name)
    missing = [name for name, items in realized.items() if not items]
    if missing:
        raise ValueError(f"PCell realization missing frontend instances: {missing}")
    lvs_realized: dict[str, list[str]] = {item.name: [] for item in handoff.devices}
    candidates = sorted(lvs_realized, key=len, reverse=True)
    for line in Path(lvs_source).read_text(encoding="utf-8").splitlines():
        tokens = line.split()
        if not tokens or not tokens[0].startswith(("M_", "X_")):
            continue
        instance_name = tokens[0]
        logical_token = instance_name[2:]
        owner = next(
            (name for name in candidates if logical_token == name or logical_token.startswith(name + "_")),
            "",
        )
        if owner:
            lvs_realized[owner].append(instance_name)
    missing_lvs = [name for name, items in lvs_realized.items() if not items]
    if missing_lvs:
        raise ValueError(f"LVS realization missing frontend instances: {missing_lvs}")
    return {
        name: {**handoff.instance_mapping[name], "oa_instances": instances, "lvs_instances": lvs_realized[name]}
        for name, instances in realized.items()
    }


def _preflight(root: Path, lib_name: str) -> dict[str, str]:
    virtuoso = os.environ.get("ANALOGSKILLS_VIRTUOSO_BINARY", "")
    strmout = os.environ.get("ANALOGSKILLS_STRMOUT_BINARY", "")
    if not strmout and virtuoso and os.path.sep in virtuoso:
        strmout = str(Path(virtuoso).with_name("strmout"))
    values = {
        "virtuoso": virtuoso,
        "strmout": strmout or "strmout",
        "calibre": os.environ.get("ANALOGSKILLS_CALIBRE_BINARY", ""),
        "drc_deck": os.environ.get("ANALOGSKILLS_CRN28HPCP_DRC_DECK", ""),
        "lvs_deck": os.environ.get("ANALOGSKILLS_CRN28HPCP_LVS_DECK", ""),
        "pdk_lib": os.environ.get("ANALOGSKILLS_VIRTUOSO_PDK_LIB_PATH", ""),
    }
    errors: list[str] = []
    execution = os.environ.get("ANALOGSKILLS_VIRTUOSO_EXECUTION", "auto").strip().lower()
    binary_keys = ("strmout", "calibre") if execution == "skill_server" else ("virtuoso", "strmout", "calibre")
    for key in binary_keys:
        value = values[key]
        if not value or (os.path.sep in value and not Path(value).is_file()) or (os.path.sep not in value and shutil.which(value) is None):
            errors.append(f"{key} binary is unavailable: {value or '<unset>'}")
    for key in ("drc_deck", "lvs_deck"):
        if not values[key] or not Path(values[key]).is_file():
            errors.append(f"{key} is unavailable: {values[key] or '<unset>'}")
    if not values["pdk_lib"] or not Path(values["pdk_lib"]).exists():
        errors.append(f"Virtuoso PDK library path is unavailable: {values['pdk_lib'] or '<unset>'}")
    _write_json(root / "signoff" / "preflight.json", {"ready": not errors, "values": values, "errors": errors})
    if errors:
        raise RuntimeError("physical sign-off preflight failed: " + "; ".join(errors))
    target_library = (root / "oa_library" / lib_name).resolve()
    target_library.mkdir(parents=True, exist_ok=True)
    (root / "cds.lib").write_text(
        "DEFINE basic $CDSHOME/tools/dfII/etc/cdslib/basic\n"
        "DEFINE analogLib $CDSHOME/tools/dfII/etc/cdslib/artist/analogLib\n"
        f"DEFINE tsmcN28 {Path(values['pdk_lib']).resolve()}\n"
        f"DEFINE {lib_name} {target_library}\n",
        encoding="utf-8",
    )
    return values


def _schematic_preflight(root: Path, lib_name: str) -> dict[str, str]:
    values = {
        "virtuoso": os.environ.get("ANALOGSKILLS_VIRTUOSO_BINARY", ""),
        "pdk_lib": os.environ.get("ANALOGSKILLS_VIRTUOSO_PDK_LIB_PATH", ""),
    }
    execution = os.environ.get("ANALOGSKILLS_VIRTUOSO_EXECUTION", "auto").strip().lower()
    errors: list[str] = []
    if execution != "skill_server":
        binary = values["virtuoso"]
        if not binary or (os.path.sep in binary and not Path(binary).is_file()) or (os.path.sep not in binary and shutil.which(binary) is None):
            errors.append(f"virtuoso binary is unavailable: {binary or '<unset>'}")
    if not values["pdk_lib"] or not Path(values["pdk_lib"]).exists():
        errors.append(f"Virtuoso PDK library path is unavailable: {values['pdk_lib'] or '<unset>'}")
    _write_json(root / "preflight.json", {"ready": not errors, "values": values, "errors": errors})
    if errors:
        raise RuntimeError("schematic import preflight failed: " + "; ".join(errors))
    target_library = (root / "oa_library" / lib_name).resolve()
    target_library.mkdir(parents=True, exist_ok=True)
    (root / "cds.lib").write_text(
        "DEFINE basic $CDSHOME/tools/dfII/etc/cdslib/basic\n"
        "DEFINE analogLib $CDSHOME/tools/dfII/etc/cdslib/artist/analogLib\n"
        f"DEFINE tsmcN28 {Path(values['pdk_lib']).resolve()}\n"
        f"DEFINE {lib_name} {target_library}\n",
        encoding="utf-8",
    )
    return values


def _materialize_drc_deck(root: Path, cell: str, gds: Path, template: Path, pdk: object) -> tuple[Path, Path, Path]:
    out = root / "signoff" / "drc"
    results = out / f"{cell}.drc.results"
    summary = out / f"{cell}.drc.summary"
    text = template.read_text(encoding="utf-8", errors="ignore")
    text = _replace_directive(text, "LAYOUT PATH", str(gds))
    text = _replace_directive(text, "LAYOUT PRIMARY", cell)
    text = _replace_directive(text, "DRC RESULTS DATABASE", str(results))
    text = _replace_directive(text, "DRC SUMMARY REPORT", str(summary))
    meta = dict(getattr(pdk, "metadata", {}).get("calibre", {}).get("generated_drc_run", {}))
    for define in meta.get("disable_defines", ()):
        text = _comment_define(text, str(define))
    deck = out / f"{cell}.drc.calibre"
    deck.write_text(text, encoding="utf-8")
    return deck, results, summary


def _materialize_lvs_deck(root: Path, cell: str, gds: Path, source: Path, template: Path, pdk: object) -> tuple[Path, Path]:
    out = root / "signoff" / "lvs"
    report = out / f"{cell}.lvs.report"
    text = template.read_text(encoding="utf-8", errors="ignore")
    for directive, value in (
        ("LAYOUT PATH", str(gds)), ("LAYOUT PRIMARY", cell), ("SOURCE PATH", str(source)),
        ("SOURCE PRIMARY", cell), ("LVS REPORT", str(report)),
    ):
        text = _replace_directive(text, directive, value)
    calibre_meta = dict(getattr(pdk, "metadata", {}).get("calibre", {}))
    meta = dict(calibre_meta.get("generated_lvs_run", {}))
    if meta.get("mos_parallel_reduction") is False:
        for kind in meta.get("mos_parallel_reduction_device_types", ("MN", "MP")):
            text = _replace_bool_statement(text, f"LVS REDUCE {kind} PARALLEL", "NO")
        text = _replace_bool_statement(text, "LVS REDUCE PARALLEL MOS", "NO")
    port_layers = tuple(dict.fromkeys(int(layer) for layer in dict(calibre_meta.get("lvs", {})).get("streamout_text_port_layers", ())))
    missing_port_lines = [
        f"PORT LAYER TEXT {layer}"
        for layer in port_layers
        if not _has_port_layer_text(text, layer)
    ]
    if missing_port_lines:
        generated = "// Generated top-level port text layers from the PDK profile.\n" + "\n".join(missing_port_lines)
        if text.lstrip().lower().startswith("#!tvf"):
            text += f"\n\ntvf::VERBATIM {{\n{generated}\n}}\n"
        else:
            text += f"\n{generated}\n"
    deck = out / f"{cell}.lvs.calibre"
    deck.write_text(text, encoding="utf-8")
    return deck, report


def _rerun_candidate(root: Path, iteration_dir: Path, skill: Path, cell: str, base: ImportedPhysicalResult, config: Mapping[str, str], pdk: object) -> tuple[list[dict[str, Any]], tuple[object, ...], tuple[object, ...]]:
    records: list[dict[str, Any]] = []
    candidate_batch = write_virtuoso_session_skill(iteration_dir / "candidate_batch.il", (skill,))
    layout_record = _run_virtuoso_skill_stage(root, "layout_oa", skill, candidate_batch, config)
    records.append(layout_record)
    if not layout_record["ok"]:
        return records, (), ("eda_stage_failed",)
    stream_run = run_eda_command(_streamout_command(root, cell, base, config))
    records.append(_run_record("streamout", stream_run))
    if not stream_run.ok:
        return records, (), ("eda_stage_failed",)
    drc_deck, drc_results, _ = _materialize_drc_deck(root, cell, Path(base.gds_path), Path(config["drc_deck"]), pdk)
    lvs_deck, lvs_report = _materialize_lvs_deck(root, cell, Path(base.gds_path), Path(base.lvs_source_path), Path(config["lvs_deck"]), pdk)
    for name, command, cwd in (
        ("calibre_drc", make_calibre_drc_command(drc_deck, binary=config["calibre"]), drc_deck.parent),
        ("calibre_lvs", make_calibre_lvs_command(lvs_deck, binary=config["calibre"]), lvs_deck.parent),
    ):
        run = run_eda_command(EdaCommand(command.command, cwd=cwd, timeout_s=1800.0))
        records.append(_run_record(name, run))
    return records, tuple(parse_drc_report(drc_results)) if drc_results.is_file() else ("drc_report_missing",), tuple(parse_lvs_report(lvs_report)) if lvs_report.is_file() else ("lvs_report_missing",)


def _run_oa_write_stage(root: Path, manifest: Mapping[str, Any], config: Mapping[str, str]) -> dict[str, Any]:
    artifacts = dict(manifest.get("artifacts", {}))
    components = (Path(str(artifacts["schematic_skill"])), Path(str(artifacts["layout_skill"])))
    return _run_cached_oa_stage(
        root=root,
        name="oa_write",
        components=components,
        live_skill=root / "oa" / "write_all_live.il",
        batch_skill=Path(str(artifacts["oa_batch_skill"])),
        state_path=root / "oa" / "oa_stage_state.json",
        fingerprint_extra=json.dumps(manifest.get("cellview", {}), sort_keys=True),
        config=config,
    )


def _run_cached_oa_stage(
    *,
    root: Path,
    name: str,
    components: tuple[Path, ...],
    live_skill: Path,
    batch_skill: Path,
    state_path: Path,
    fingerprint_extra: str,
    config: Mapping[str, str],
) -> dict[str, Any]:
    fingerprint = _files_fingerprint(components, extra=fingerprint_extra)
    previous = _read_json_mapping(state_path)
    if previous.get("fingerprint") == fingerprint and previous.get("status") == "completed":
        return {
            "name": name,
            "command": [],
            "returncode": 0,
            "ok": True,
            "timed_out": False,
            "skipped": True,
            "reason": "unchanged_oa_fingerprint",
            "fingerprint": fingerprint,
        }
    record = _run_virtuoso_skill_stage(
        root,
        name,
        live_skill,
        batch_skill,
        config,
        live_components=components,
    )
    record["fingerprint"] = fingerprint
    if record["ok"]:
        _write_json(state_path, {
            "schema": "analogskills.oa_stage_state/v1",
            "status": "completed",
            "fingerprint": fingerprint,
            "executor": record.get("executor", "batch"),
        })
    return record


def _run_virtuoso_skill_stage(
    root: Path,
    name: str,
    live_skill: str | Path,
    batch_skill: str | Path,
    config: Mapping[str, str],
    *,
    live_components: tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    mode = os.environ.get("ANALOGSKILLS_VIRTUOSO_EXECUTION", "auto").strip().lower()
    if mode not in {"auto", "skill_server", "batch"}:
        raise ValueError("ANALOGSKILLS_VIRTUOSO_EXECUTION must be auto, skill_server, or batch")
    port_file = _skill_server_port_file(root)
    if mode != "batch" and port_file is not None:
        client = VirtuosoSkillClient(port_file=port_file, timeout_ms=600000)
        try:
            ping = client.ping()
            if not ping.ok:
                raise RuntimeError(ping.error or "SKILL server ping failed")
            if live_components:
                live_path = write_virtuoso_session_skill(live_skill, live_components, exit_after_run=False)
            else:
                live_path = Path(live_skill)
            data = run_skill_file(client, live_path)
            return {
                "name": name,
                "command": ["skill-server", str(port_file), f'load("{live_path}")'],
                "returncode": 0,
                "ok": True,
                "timed_out": False,
                "executor": "skill_server",
                "stdout_tail": str(data)[-4000:],
                "stderr_tail": "",
            }
        except Exception as exc:
            if mode == "skill_server":
                return {
                    "name": name,
                    "command": ["skill-server", str(port_file)],
                    "returncode": 1,
                    "ok": False,
                    "timed_out": False,
                    "executor": "skill_server",
                    "stdout_tail": "",
                    "stderr_tail": str(exc)[-4000:],
                }
        finally:
            client.disconnect()
    elif mode == "skill_server":
        return {
            "name": name,
            "command": ["skill-server"],
            "returncode": 1,
            "ok": False,
            "timed_out": False,
            "executor": "skill_server",
            "stdout_tail": "",
            "stderr_tail": "SKILL server port file is unavailable",
        }
    command = make_virtuoso_batch_command(batch_skill, binary=config["virtuoso"])
    run = run_eda_command(EdaCommand(command.command, cwd=root, timeout_s=600.0))
    return {**_run_record(name, run), "executor": "batch"}


def _skill_server_port_file(root: Path) -> Path | None:
    configured = os.environ.get("ANALOGSKILLS_SKILL_SERVER_PORT_FILE", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend((root / "skill_server_port.txt", Path.cwd() / "skill_server_port.txt"))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _files_fingerprint(paths: tuple[Path, ...], *, extra: str = "") -> str:
    digest = hashlib.sha256(extra.encode("utf-8"))
    for path in paths:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _read_json_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _streamout_command(root: Path, cell: str, base: ImportedPhysicalResult, config: Mapping[str, str]) -> EdaCommand:
    return EdaCommand(
        make_strmout_command(
            lib=json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))["cellview"]["lib"],
            cell=cell,
            output_path=base.gds_path,
            binary=config["strmout"],
            run_dir=root,
            log_file=root / "signoff" / "streamout.log",
            summary_file=root / "signoff" / "streamout.summary",
        ).command,
        cwd=root,
        timeout_s=600.0,
    )


def _replace_directive(text: str, directive: str, value: str) -> str:
    import re
    pattern = re.compile(rf'(?m)^(\s*){re.escape(directive)}\s+".*?"(.*)$')
    replacement = lambda match: f'{match.group(1)}{directive} "{value}"{match.group(2)}'
    updated, count = pattern.subn(replacement, text, count=1)
    if count == 0:
        updated = f'{directive} "{value}"\n' + updated
    return updated


def _comment_define(text: str, name: str) -> str:
    import re
    return re.sub(rf"(?m)^(\s*)#DEFINE(\s+){re.escape(name)}(\b.*)$", rf"\1//#DEFINE\2{name}\3", text)


def _replace_bool_statement(text: str, statement: str, value: str) -> str:
    import re
    pattern = re.compile(rf"(?im)^(\s*{re.escape(statement)}\s+)(YES|NO)(\s*)$")
    return pattern.sub(rf"\g<1>{value}\g<3>", text, count=1)


def _has_port_layer_text(text: str, layer: int) -> bool:
    import re
    return re.search(rf"(?im)^\s*PORT\s+LAYER\s+TEXT\s+{int(layer)}\s*(?://.*)?$", text) is not None


def _load_oa_plan(path: str | Path) -> OaWritePlan:
    from analogskills.eda.oa import load_oa_plan_json
    return load_oa_plan_json(path)


def _run_record(name: str, run: EdaRunResult) -> dict[str, Any]:
    return {"name": name, "command": list(run.command), "returncode": run.returncode, "ok": run.ok, "timed_out": run.timed_out, "stdout_tail": run.stdout[-4000:], "stderr_tail": run.stderr[-4000:]}


def _checkpoint_signoff_artifacts(gds: Path, drc_results: Path, drc_summary: Path, lvs_report: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for source in (gds, drc_results, drc_summary, lvs_report):
        if source.is_file():
            shutil.copy2(source, destination / source.name)


def _restore_signoff_artifacts(source_dir: Path, gds: Path, drc_results: Path, drc_summary: Path, lvs_report: Path) -> None:
    for target in (gds, drc_results, drc_summary, lvs_report):
        source = source_dir / target.name
        if source.is_file():
            shutil.copy2(source, target)


def _runtime_manifest(config: Mapping[str, str] | None = None) -> dict[str, Any]:
    return {
        "python": platform.python_version(), "platform": platform.platform(),
        "git_commit": _git_commit(), "configured_tools": dict(config or {}),
    }


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _signoff_failure(base: ImportedPhysicalResult, runs: list[dict[str, Any]], reason: str, drc: Path | None = None, lvs: Path | None = None) -> ImportedPhysicalResult:
    result = replace(base, status="physical_blocked", drc_report_path=str(drc or ""), lvs_report_path=str(lvs or ""), errors=(reason,))
    root = Path(base.physical_root)
    manifest = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
    manifest["status"] = result.status
    manifest["signoff"] = {"runs": runs, "error": reason}
    _write_json(root / "run_manifest.json", manifest)
    _persist_state(result)
    return result


def _persist_state(result: ImportedPhysicalResult) -> None:
    root = Path(result.physical_root)
    _write_json(root / "physical_state.json", result.to_dict())
    lines = [
        "# Physical Implementation Report", "", f"- Status: `{result.status}`",
        f"- GDS: `{result.gds_path}`", f"- DRC violations: `{result.drc_violations}`",
        f"- LVS issues: `{result.lvs_issues}`", f"- ECO iterations: `{result.eco_iterations}`",
    ]
    if result.errors:
        lines.extend(["", "## Errors", "", *(f"- {item}" for item in result.errors)])
    (root / "physical_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path
