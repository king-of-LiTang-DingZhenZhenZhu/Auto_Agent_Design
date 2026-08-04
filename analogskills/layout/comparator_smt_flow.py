"""End-to-end hierarchical SMT physical prototype for a StrongARM comparator."""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from pathlib import Path
import re
from typing import Mapping

from analogskills.blocks import make_strongarm_comparator
from analogskills.contracts import TopologyGraph
from analogskills.pdk import PdkConfig, PdkProfile, ProcessNode, resolve_pdk_config
from .floorplan import partition_by_function
from .hierarchical_smt import HierarchicalRouteCandidate, HierarchicalRouteDemand
from .hierarchical_smt_2d import (
    HierarchicalPhysicalGroup2D,
    HierarchicalPhysicalProblem2D,
    HierarchicalPhysicalSolution2D,
    HierarchicalRoutingCorridor2D,
    solve_hierarchical_physical_problem_2d,
)
from .smt_design_rules import strongarm_hierarchical_rule_config
from .routing import Grid, RoutedNet, route_astar_costed, route_length
from .placement import Placement
from .structured_routing import (
    PowerMeshResult,
    PowerMeshSpec,
    route_coupled_differential_pair,
    synthesize_power_mesh,
)


@dataclass(frozen=True)
class ComparatorRouteSegment:
    name: str
    logical_nets: tuple[str, ...]
    corridor: str
    routes: tuple[RoutedNet, ...]


@dataclass(frozen=True)
class StrongArmHierarchicalResult:
    graph: TopologyGraph
    problem: HierarchicalPhysicalProblem2D
    physical: HierarchicalPhysicalSolution2D
    critical_segments: tuple[ComparatorRouteSegment, ...]
    noncritical_routes: tuple[RoutedNet, ...]
    power_mesh: PowerMeshResult
    checks: Mapping[str, object]

    @property
    def passed(self) -> bool:
        return bool(self.checks.get("passed", False))


@dataclass(frozen=True)
class StrongArmGdsArtifacts:
    output_dir: Path
    gds_path: Path
    layout_json_path: Path
    layout_skill_path: Path
    layout_plan_path: Path
    summary_path: Path
    native_load_skill_path: Path
    foundry_drc_deck_path: Path
    lvs_source_netlist_path: Path
    lvs_source_precheck_path: Path
    foundry_lvs_deck_path: Path
    lvs_report_path: Path
    verification_issue_count: int
    placement_count: int
    path_count: int
    rect_count: int


def build_strongarm_hierarchical_problem(
    graph: TopologyGraph | None = None,
    *,
    pdk: object | None = None,
) -> HierarchicalPhysicalProblem2D:
    graph = graph or make_strongarm_comparator("SMT_COMP")
    if isinstance(pdk, (PdkProfile, PdkConfig, ProcessNode, str, Path)):
        pdk = resolve_pdk_config(pdk)
    smt_rules = strongarm_hierarchical_rule_config(pdk)
    partitions = {partition.name: partition for partition in partition_by_function(graph)}
    required = ("tail_switch", "input_pair", "regenerative_latch", "reset")
    missing = tuple(name for name in required if name not in partitions)
    if missing:
        raise ValueError(f"graph is not a recognized StrongARM comparator; missing partitions {missing}")

    sizes = {
        "tail_switch": (4, 2),
        "input_pair": (8, 3),
        "regenerative_latch": (10, 5),
        "reset": (8, 3),
    }
    groups = tuple(
        HierarchicalPhysicalGroup2D(name, *sizes[name], allow_rotate=False)
        for name in required
    )
    corridor_rules = dict(smt_rules["corridors"])

    def corridor_kwargs(name: str) -> dict[str, object]:
        return dict(corridor_rules.get(name, {}))

    corridors = (
        HierarchicalRoutingCorridor2D(
            "C_TAIL_INPUT", "tail_switch", "input_pair", "vertical",
            **corridor_kwargs("C_TAIL_INPUT"),
        ),
        HierarchicalRoutingCorridor2D(
            "C_INPUT_LATCH", "input_pair", "regenerative_latch", "vertical",
            **corridor_kwargs("C_INPUT_LATCH"),
        ),
        HierarchicalRoutingCorridor2D(
            "C_LATCH_RESET", "regenerative_latch", "reset", "vertical",
            **corridor_kwargs("C_LATCH_RESET"),
        ),
    )
    critical_demand = dict(smt_rules["critical_track_demand"])
    noncritical_demand = dict(smt_rules["noncritical_track_demand"])
    critical = (
        HierarchicalRouteDemand(
            "INPUT_DIFF",
            int(critical_demand.get("INPUT_DIFF", 2)),
            (HierarchicalRouteCandidate("INPUT_ACCESS", ("C_TAIL_INPUT",), cost=1),),
            critical=True,
        ),
        HierarchicalRouteDemand(
            "OUTPUT_REGEN_DIFF",
            int(critical_demand.get("OUTPUT_REGEN_DIFF", 3)),
            (HierarchicalRouteCandidate("REGEN_VERTICAL", ("C_INPUT_LATCH", "C_LATCH_RESET"), cost=1),),
            critical=True,
        ),
        HierarchicalRouteDemand(
            "TAIL_CURRENT",
            int(critical_demand.get("TAIL_CURRENT", 2)),
            (HierarchicalRouteCandidate("TAIL_VERTICAL", ("C_TAIL_INPUT", "C_INPUT_LATCH"), cost=1),),
            critical=True,
        ),
    )
    noncritical = (
        HierarchicalRouteDemand("CLK", int(noncritical_demand.get("CLK", 1)), (HierarchicalRouteCandidate("CLK_LOCAL", ("C_TAIL_INPUT",), cost=1),)),
        HierarchicalRouteDemand("RST", int(noncritical_demand.get("RST", 1)), (HierarchicalRouteCandidate("RST_LOCAL", ("C_LATCH_RESET",), cost=1),)),
    )
    return HierarchicalPhysicalProblem2D(
        groups=groups,
        corridors=corridors,
        critical_routes=critical,
        noncritical_routes=noncritical,
        placement_spacing_tracks=int(smt_rules["placement_spacing_tracks"]),
        target_aspect_num=int(smt_rules["target_aspect_num"]),
        target_aspect_den=int(smt_rules["target_aspect_den"]),
        rule_metadata=smt_rules,
    )


def run_strongarm_hierarchical_flow(
    graph: TopologyGraph | None = None,
    *,
    pdk: object | None = None,
) -> StrongArmHierarchicalResult:
    graph = graph or make_strongarm_comparator("SMT_COMP")
    problem = build_strongarm_hierarchical_problem(graph, pdk=pdk)
    physical = solve_hierarchical_physical_problem_2d(problem)
    if not physical.converged:
        raise ValueError("StrongARM hierarchical physical solve did not converge")

    margin = 2
    width = physical.master.total_width_tracks + 2 * margin + 1
    height = physical.master.total_height_tracks + 2 * margin + 1
    grid = Grid(width, height)
    corridor_boxes = {
        name: _shift_bbox(bbox, margin)
        for name, bbox in physical.master.corridor_bboxes.items()
    }
    segments = (
        _route_vertical_diff_segment(grid, "INPUT_ACCESS", ("INP", "INN"), "C_TAIL_INPUT", corridor_boxes["C_TAIL_INPUT"]),
        _route_vertical_diff_segment(grid, "OUT_REGEN_LOWER", ("OUTP", "OUTN"), "C_INPUT_LATCH", corridor_boxes["C_INPUT_LATCH"]),
        _route_vertical_diff_segment(grid, "OUT_REGEN_UPPER", ("OUTP", "OUTN"), "C_LATCH_RESET", corridor_boxes["C_LATCH_RESET"]),
    )
    noncritical = (
        _route_vertical_scalar(grid, "CLK", corridor_boxes["C_TAIL_INPUT"], layer="M2", lane=0),
        _route_vertical_scalar(grid, "RST", corridor_boxes["C_LATCH_RESET"], layer="M2", lane=-1),
        _route_vertical_scalar(grid, "TAIL", corridor_boxes["C_INPUT_LATCH"], layer="M4", lane=0, width_nm=320),
    )
    mesh = synthesize_power_mesh(
        PowerMeshSpec(
            bbox=(0, 0, width - 1, height - 1),
            nets=("VDD", "VSS"),
            horizontal_layer="M1",
            vertical_layer="M5",
            horizontal_pitch=max(4, height // 4),
            vertical_pitch=max(4, width // 4),
            current_ma={"VDD": 4.0, "VSS": 4.0},
            min_redundant_straps_per_net=2,
        )
    )
    checks = _strongarm_flow_checks(graph, physical, segments, noncritical, mesh, corridor_boxes)
    return StrongArmHierarchicalResult(graph, problem, physical, segments, noncritical, mesh, checks)


def build_strongarm_smt_gds_artifacts(
    output_dir: str | Path,
    *,
    graph: TopologyGraph | None = None,
    pdk_path: str | Path | None = None,
    calibre_feedback_db: str | Path | None = None,
    lib_name: str = "skillsZSmtCompLib",
    cell_name: str = "smt_strongarm_comp",
) -> StrongArmGdsArtifacts:
    """Lower the SMT result through the real PCell/interconnect backbone to GDS."""

    from analogskills.comparator_dsl_bundle import default_strongarm_sizing
    from analogskills.eda import OaCellView, OaWritePlan, layout_plan_to_oa_write_plan, prepare_lvs_source_netlist, save_oa_plan_json, snap_oa_write_plan_to_grid, validate_oa_write_plan_grid, write_oa_skill
    from analogskills.eda.gds import oa_plan_to_gds
    from analogskills.layout.ir import layout_plan_to_dict
    from analogskills.opt import build_sizing_layout_implementation
    from analogskills.pdk import resolve_pdk_config
    import json

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    pdk = resolve_pdk_config(pdk_path)
    graph = graph or make_strongarm_comparator("SMT_GDS")
    flow = run_strongarm_hierarchical_flow(graph, pdk=pdk)
    if not flow.passed:
        raise ValueError("StrongARM SMT planning checks failed: " + "; ".join(str(item) for item in flow.checks.get("issues", ())))
    sizing = default_strongarm_sizing()
    nf_artifacts = target.parent / "nf_characterization_exact" / "artifacts"
    nf_solution = None
    calibration_cache = None
    if nf_artifacts.exists() and any(nf_artifacts.glob("*.json")):
        from analogskills.layout.nf_characterization import build_strongarm_calibration_cache, solve_strongarm_characterized_nf
        nf_solution = solve_strongarm_characterized_nf(sizing, nf_artifacts, pdk=pdk)
        calibration_cache = build_strongarm_calibration_cache(nf_artifacts, pdk=pdk)
        placements = tuple(
            Placement(name, row.pcell_origin_x_sites * 0.01, row.pcell_origin_y_sites * 0.01, orient=row.orient, role=name)
            for name, row in nf_solution.placements.items()
        )
        sizing = {
            name: {
                **dict(values),
                "nf": nf_solution.placements[name].nf,
                "m": nf_solution.placements[name].m,
                "wf": nf_solution.placements[name].finger_width_nm * 1e-9,
                "layout_width_um": nf_solution.placements[name].width_sites * 0.01,
                "layout_height_um": nf_solution.placements[name].height_sites * 0.01,
                "layout_bbox_x0_um": nf_solution.placements[name].pcell_bbox_x0_sites * 0.01,
                "layout_bbox_y0_um": nf_solution.placements[name].pcell_bbox_y0_sites * 0.01,
                "pcell_overrides": dict(nf_solution.placements[name].pcell_params),
            }
            for name, values in sizing.items()
        }
    else:
        placements = lower_strongarm_smt_device_placements(flow.physical)
    implementation = build_sizing_layout_implementation(
        graph,
        sizing,
        pdk=pdk,
        placements=placements,
        include_interconnect=True,
        include_power_rails=True,
        include_source_drops=True,
        include_supply_taps=True,
        include_well_regions=True,
        include_guard_ring=True,
        guard_ring_net="VSS",
        strict_terminal_access=False,
        strict_precheck=False,
        legalize_physical=True,
        calibration_cache=calibration_cache,
        allow_nearest_calibration=calibration_cache is not None,
        max_nearest_distance=0.01,
        legalization_max_iterations=3,
        metadata={
            "placement_origin": "hierarchical_smt_2d",
            "smt_refinement_iterations": len(flow.physical.iterations),
            "smt_corridor_capacity_tracks": dict(flow.physical.master.corridor_capacity_tracks),
            "nf_smt_characterized": nf_solution is not None,
        },
    )
    source_plan = layout_plan_to_oa_write_plan(implementation.layout_plan)
    oa_plan = OaWritePlan(
        OaCellView(lib_name, cell_name, "layout", "maskLayout"),
        nets=source_plan.nets,
        pins=source_plan.pins,
        instances=source_plan.instances,
        rects=source_plan.rects,
        labels=source_plan.labels,
        paths=source_plan.paths,
        vias=source_plan.vias,
    )
    oa_plan = _dedupe_oa_pins(oa_plan)
    oa_plan = _apply_configured_oa_path_rewrites(oa_plan, pdk)
    # The abstract layout grid can be finer than the foundry stream grid.  Snap
    # only the generated top-level geometry before SKILL/GDS emission; PCell
    # internals remain governed by their native parameterized implementation.
    pdk_metadata = dict(getattr(pdk, "metadata", {}) or {})
    calibre_metadata = dict(pdk_metadata.get("calibre", {}) or {})
    calibre_grid_nm = int(calibre_metadata.get("grid_nm", pdk.rules.grid_nm))
    oa_plan = snap_oa_write_plan_to_grid(oa_plan, calibre_grid_nm)
    oa_plan = _apply_lvs_assist_geometry(oa_plan, pdk, calibration_cache=calibration_cache)
    oa_plan = _apply_lvs_assist_tail_scaffold(oa_plan, pdk)
    oa_plan = _apply_lvs_gate_landing_clips(oa_plan, pdk, calibration_cache=calibration_cache)
    oa_plan = _apply_lvs_multifinger_marker_assist(oa_plan, pdk, calibration_cache=calibration_cache)
    oa_plan = _apply_lvs_assist_top_rail_clip(oa_plan, pdk)
    oa_plan = _apply_lvs_assist_pmos_vdd_bridges(oa_plan, pdk)
    oa_plan = _apply_configured_pmos_nwell_bridges(oa_plan, pdk)
    oa_plan = _apply_lvs_assist_pmos_vdd_labels(oa_plan, pdk)
    feedback_summary: dict[str, object] = {"enabled": False, "accepted_eco_applied": 0}
    accepted_eco_path = target / f"{cell_name}_accepted_eco.json"
    if accepted_eco_path.exists():
        from analogskills.repair import apply_oa_rect_replacement_journal

        accepted_journal = json.loads(accepted_eco_path.read_text(encoding="utf-8"))
        oa_plan, accepted_count, accepted_skips = apply_oa_rect_replacement_journal(oa_plan, accepted_journal)
        feedback_summary.update({
            "accepted_eco_path": str(accepted_eco_path),
            "accepted_eco_applied": accepted_count,
            "accepted_eco_skips": accepted_skips,
        })
    feedback_path = Path(calibre_feedback_db) if calibre_feedback_db is not None else None
    if feedback_path is not None and feedback_path.exists():
        # Previous Calibre markers are feedback, not a second rule deck: use
        # them only to select conservative same-net jog-fill candidates.
        from analogskills.eda.reports import parse_calibre_ascii_drc_db
        from analogskills.repair import build_oa_jog_fill_eco, localize_calibre_markers, markers_from_calibre_results, plan_marker_repairs

        feedback_markers = markers_from_calibre_results(parse_calibre_ascii_drc_db(feedback_path))
        feedback_actions = plan_marker_repairs(localize_calibre_markers(feedback_markers, _oa_plan_shapes_for_feedback(oa_plan)))
        spacing = {layer: value / 1000.0 for layer, value in pdk.rules.min_spacing_nm.items()}
        jog_eco = build_oa_jog_fill_eco(oa_plan, feedback_actions, min_spacing_um_by_layer=spacing)
        oa_plan = jog_eco.plan
        feedback_summary = {
            "enabled": True,
            "database": str(feedback_path),
            "accepted_eco_path": str(accepted_eco_path) if accepted_eco_path.exists() else "",
            "accepted_eco_applied": feedback_summary.get("accepted_eco_applied", 0),
            "accepted_eco_skips": feedback_summary.get("accepted_eco_skips", ()),
            "marker_count": len(feedback_markers),
            "replaced_rect_count": len(jog_eco.replaced_rect_indices),
            "added_rect_count": jog_eco.added_rect_count,
            "skipped_group_count": len(jog_eco.skipped_groups),
        }
    oa_plan = _apply_configured_oa_rect_rewrites(oa_plan, pdk)
    oa_plan = _apply_configured_oa_via_rewrites(oa_plan, pdk)
    # All policy/ECO/helper geometry has been applied at this point.  The
    # abstract PDK grid is intentionally finer than the Calibre stream grid, so
    # enforce the configured signoff grid immediately before serializing and
    # before the SKILL writer performs any PDK-aware via emission.
    oa_plan = snap_oa_write_plan_to_grid(oa_plan, calibre_grid_nm)
    stream_grid_issues = validate_oa_write_plan_grid(oa_plan, calibre_grid_nm)
    if stream_grid_issues:
        raise ValueError(
            f"OA write plan has {len(stream_grid_issues)} off-grid geometries for Calibre {calibre_grid_nm}nm grid: "
            + "; ".join(stream_grid_issues[:10])
        )
    layout_json = target / f"{cell_name}_layout.json"
    layout_skill = target / f"{cell_name}_layout.il"
    layout_plan_path = target / f"{cell_name}_layout_ir.json"
    summary_path = target / f"{cell_name}_summary.json"
    native_load_skill_path = target / f"{cell_name}_native_load.il"
    foundry_drc_deck_path = target / f"{cell_name}_foundry_drc.calibre"
    lvs_source_netlist_path = target / f"{cell_name}_lvs_source.cdl"
    lvs_source_precheck_path = target / f"{cell_name}_lvs_source_precheck.json"
    foundry_lvs_deck_path = target / f"{cell_name}_foundry_lvs.calibre"
    lvs_report_path = target / f"{cell_name}_lvs.rep"
    gds_path = target / f"{cell_name}.gds"
    save_oa_plan_json(oa_plan, layout_json)
    write_oa_skill(
        oa_plan,
        layout_skill,
        grid=pdk,
        top_level_nets=tuple(graph.pins),
        pin_net_aliases={pin: pin for pin in graph.pins},
        replace_cellview=True,
        allow_label_only_top_level_nets=True,
    )
    native_lib_path = (target / "oa_lib").resolve()
    native_load_skill_path.write_text(
        "\n".join(
            (
                f'libObj = ddGetObj("{lib_name}")',
                f'unless(libObj libObj = ddCreateLib("{lib_name}" "{native_lib_path.as_posix()}"))',
                'when(libObj techBindTechFile(libObj "tsmcN28"))',
                f'load("{layout_skill.resolve().as_posix()}")',
                'exit()',
                "",
            )
        ),
        encoding="utf-8",
    )
    source_drc_deck = Path(__file__).resolve().parents[2] / "iPDK_t28" / "CRN28HPCp" / "Calibre" / "drc" / "calibre.drc"
    if source_drc_deck.exists():
        native_gds_path = (target / f"{cell_name}_native.gds").resolve()
        drc_results_path = (target / f"{cell_name}_foundry_drc.db").resolve()
        drc_summary_path = (target / f"{cell_name}_foundry_drc.rep").resolve()
        deck_text = source_drc_deck.read_text(encoding="utf-8", errors="replace")
        for option in ("EFP", "FULL_CHIP", "WITH_SEALRING", "WITH_APRDL", "WITH_POLYIMIDE", "AP_28K_THICKNESS", "GUIDELINE_ESD", "CHECK_LOW_DENSITY"):
            deck_text = deck_text.replace(f"#DEFINE {option}", f"//#DEFINE {option}")
        deck_text = deck_text.replace('LAYOUT PATH "GDSFILENAME"', f'LAYOUT PATH "{native_gds_path.as_posix()}"')
        deck_text = deck_text.replace('LAYOUT PRIMARY "TOPCELLNAME"', f'LAYOUT PRIMARY "{cell_name}"')
        deck_text = deck_text.replace('DRC RESULTS DATABASE "DRC_RES.db"', f'DRC RESULTS DATABASE "{drc_results_path.as_posix()}"')
        deck_text = deck_text.replace('DRC SUMMARY REPORT "DRC.rep"', f'DRC SUMMARY REPORT "{drc_summary_path.as_posix()}"')
        foundry_drc_deck_path.write_text(deck_text, encoding="utf-8")
    lvs_config = calibre_metadata.get("lvs", {}) if isinstance(calibre_metadata.get("lvs", {}), Mapping) else {}
    lvs_source_sizing = _apply_configured_lvs_source_dimension_adjustments(graph, sizing, pdk)
    lvs_source_path, lvs_source_report = prepare_lvs_source_netlist(
        graph,
        lvs_source_sizing,
        lvs_source_netlist_path,
        subckt_name=cell_name,
        layout_plan=oa_plan,
        layout_ports=tuple(graph.pins),
        model_map={"nch_mac": "nch_mac", "pch_mac": "pch_mac", "nmos": "nch_mac", "pmos": "pch_mac"},
        require_model_map=True,
        report_path=lvs_source_precheck_path,
        mos_expansion=str(lvs_config.get("source_mos_expansion", "macro") or "macro"),
    )
    source_lvs_deck = Path(__file__).resolve().parents[2] / "iPDK_t28" / "CRN28HPCp" / "Calibre" / "online" / "lvs_online" / "1P10M_5X2Y2R" / "calibre.lvs"
    if source_lvs_deck.exists():
        native_gds_path = (target / f"{cell_name}_native.gds").resolve()
        erc_results_path = (target / f"{cell_name}_erc.db").resolve()
        deck_text = source_lvs_deck.read_text(encoding="utf-8", errors="replace")
        deck_text = deck_text.replace('LAYOUT PRIMARY "lvs_top"', f'LAYOUT PRIMARY "{cell_name}"')
        deck_text = deck_text.replace('LAYOUT PATH "lvs_top.gds"', f'LAYOUT PATH "{native_gds_path.as_posix()}"')
        deck_text = deck_text.replace('SOURCE PRIMARY "lvs_top"', f'SOURCE PRIMARY "{cell_name}"')
        deck_text = deck_text.replace('SOURCE PATH "lvs_top.cdl"', f'SOURCE PATH "{lvs_source_path.resolve().as_posix()}"')
        deck_text = deck_text.replace('ERC RESULTS DATABASE "calibre_erc.db" ASCII', f'ERC RESULTS DATABASE "{erc_results_path.as_posix()}" ASCII')
        deck_text = deck_text.replace('LVS REPORT "lvs.rep"', f'LVS REPORT "{lvs_report_path.resolve().as_posix()}"')
        deck_text = _apply_configured_lvs_deck_rewrites(deck_text, calibre_metadata)
        foundry_lvs_deck_path.write_text(deck_text, encoding="utf-8")
    layout_plan_path.write_text(json.dumps(layout_plan_to_dict(implementation.layout_plan), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    oa_plan_to_gds(oa_plan, gds_path, pdk=pdk, top_cell=cell_name, lib_name=lib_name)
    verification = dict(implementation.metadata.get("physical_implementation_contract", {}).get("verification", {}) or {})
    summary_path.write_text(
        json.dumps(
            {
                "gds_path": str(gds_path),
                "placement_count": len(placements),
                "path_count": len(oa_plan.paths),
                "rect_count": len(oa_plan.rects),
                "via_count": len(oa_plan.vias),
                "calibre_stream_grid_nm": calibre_grid_nm,
                "calibre_feedback": feedback_summary,
                "lvs": {
                    "source_netlist": str(lvs_source_path),
                    "source_precheck": lvs_source_report.to_dict(),
                    "source_precheck_path": str(lvs_source_precheck_path),
                    "foundry_lvs_deck": str(foundry_lvs_deck_path),
                    "lvs_report": str(lvs_report_path),
                },
                "verification": verification,
                "smt_checks": dict(flow.checks),
                "smt_design_rules": dict(flow.problem.rule_metadata),
                "smt_corridor_capacity_tracks": dict(flow.physical.master.corridor_capacity_tracks),
                "smt_group_bboxes": {name: placement.bbox for name, placement in flow.physical.master.placements.items()},
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    return StrongArmGdsArtifacts(
        output_dir=target,
        gds_path=gds_path,
        layout_json_path=layout_json,
        layout_skill_path=layout_skill,
        layout_plan_path=layout_plan_path,
        summary_path=summary_path,
        native_load_skill_path=native_load_skill_path,
        foundry_drc_deck_path=foundry_drc_deck_path,
        lvs_source_netlist_path=lvs_source_path,
        lvs_source_precheck_path=lvs_source_precheck_path,
        foundry_lvs_deck_path=foundry_lvs_deck_path,
        lvs_report_path=lvs_report_path,
        verification_issue_count=int(verification.get("issue_count", 0) or 0),
        placement_count=len(placements),
        path_count=len(oa_plan.paths),
        rect_count=len(oa_plan.rects),
    )


def _dedupe_oa_pins(plan: object) -> object:
    from dataclasses import replace

    from analogskills.eda import OaPin, OaWritePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    seen: set[str] = set()
    deduped_rows: list[object] = []
    for pin in tuple(plan.pins):
        name = str(getattr(pin, "name", ""))
        if not name or name in seen:
            continue
        seen.add(name)
        deduped_rows.append(pin)
    deduped = tuple(deduped_rows)
    if len(deduped) == len(tuple(plan.pins)):
        return plan
    return replace(plan, pins=deduped)


def _apply_lvs_assist_geometry(plan: object, pdk: object, *, calibration_cache: object | None = None) -> object:
    """Add LVS-only helper geometry to an explicitly separated assist artifact.

    This function is intentionally gated by metadata written only by the CLI
    wrapper's ``--emit-lvs-assist-artifact`` path.  It must never affect the
    primary/signoff layout artifact.
    """

    from dataclasses import replace

    from analogskills.eda import OaRect, OaVia, OaWritePlan
    from analogskills.pcell.access import PCellTerminalAccessor
    from analogskills.pcell.generation import PCellInstancePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    artifact_policy = metadata.get("artifact_policy", {}) if isinstance(metadata.get("artifact_policy", {}), Mapping) else {}
    if not bool(artifact_policy.get("lvs_assist_only", False)):
        return plan
    mode = str(artifact_policy.get("lvs_assist_geometry", "none") or "none").strip().lower()
    if mode in {"", "none", "off", "disabled"}:
        return plan
    if mode not in {"gate_contact_array", "gate_poly_bridge", "gate_poly_bridge_sdstrap", "gate_poly_bridge_sdstrap_nmos"}:
        return plan

    accessor = PCellTerminalAccessor(
        pdk,  # type: ignore[arg-type]
        calibration_cache,  # type: ignore[arg-type]
        allow_nearest_calibration=calibration_cache is not None,
        max_nearest_distance=0.01,
    )
    rects = list(tuple(getattr(plan, "rects", ()) or ()))
    vias = list(tuple(getattr(plan, "vias", ()) or ()))
    labels = list(tuple(getattr(plan, "labels", ()) or ()))
    label_sd_straps = bool(artifact_policy.get("lvs_label_sd_straps", False))
    seen: set[tuple[str, str]] = set()
    for instance in tuple(getattr(plan, "instances", ()) or ()):
        logical = _assist_logical_mos_name(instance)
        if logical not in {"nmos", "pmos"}:
            continue
        gate_net = str(dict(getattr(instance, "connections", {}) or {}).get("G", "") or "")
        if not gate_net:
            continue
        try:
            nf = int(dict(getattr(instance, "params", {}) or {}).get("fingers", dict(getattr(instance, "params", {}) or {}).get("nf", 1)) or 1)
        except (TypeError, ValueError):
            nf = 1
        if nf <= 1:
            continue
        key = (str(getattr(instance, "name", "")), gate_net)
        if key in seen:
            continue
        seen.add(key)
        inst_plan = PCellInstancePlan(
            str(getattr(instance, "name", "")),
            logical,
            str(getattr(instance, "lib", getattr(instance, "lib_name", "")) or getattr(instance, "lib_name", "")),
            str(getattr(instance, "cell", getattr(instance, "cell_name", "")) or getattr(instance, "cell_name", "")),
            str(getattr(instance, "view", getattr(instance, "view_name", "layout")) or getattr(instance, "view_name", "layout")),
            params=dict(getattr(instance, "params", {}) or {}),
            xy_um=tuple(float(value) for value in getattr(instance, "xy", getattr(instance, "xy_um", (0.0, 0.0)))),
            orient=str(getattr(instance, "orient", "R0") or "R0"),
            connections=dict(getattr(instance, "connections", {}) or {}),
            instantiation_method=str(getattr(instance, "instantiation_method", "dbCreateInstByMasterName") or "dbCreateInstByMasterName"),
        )
        try:
            gate_pin = accessor.get_terminal_pin(inst_plan, "G")
        except Exception:
            continue
        points = _assist_gate_access_points(pdk, gate_pin.xy_um, nf=nf, orient=inst_plan.orient)
        if len(points) < 2:
            continue
        if mode in {"gate_poly_bridge", "gate_poly_bridge_sdstrap", "gate_poly_bridge_sdstrap_nmos"}:
            bridge_points = _assist_gate_poly_bridge_points(
                pdk,
                points,
                logical=logical,
                params=inst_plan.params,
            )
            rects.append(OaRect("PO", "drawing", _assist_poly_bridge_bbox(pdk, bridge_points), gate_net, metadata={
                "kind": "lvs_assist_gate_poly_bridge",
                "source_instance": inst_plan.name,
                "fingers": nf,
                "lvs_assist_only": True,
            }))
            if mode == "gate_poly_bridge_sdstrap" or (mode == "gate_poly_bridge_sdstrap_nmos" and logical == "nmos"):
                sd_rects, sd_vias = _assist_source_drain_strap_geometry(
                    pdk,
                    inst_plan,
                    nf=nf,
                    accessor=accessor,
                    existing_rects=tuple(rects),
                    existing_paths=tuple(getattr(plan, "paths", ()) or ()),
                )
                rects.extend(sd_rects)
                vias.extend(sd_vias)
                if label_sd_straps:
                    labels.extend(_assist_sd_strap_labels(sd_rects))
            continue
        bridge = _assist_m1_bridge_bbox(pdk, points)
        rects.append(OaRect(pdk.layer_map.metals[0], "drawing", bridge, gate_net, metadata={
            "kind": "lvs_assist_gate_m1_bridge",
            "source_instance": inst_plan.name,
            "lvs_assist_only": True,
        }))
        for finger_index, point in enumerate(points):
            contact_rects, contact_vias = _assist_gate_contact_geometry(
                pdk,
                gate_net,
                point,
                source_instance=inst_plan.name,
                finger_index=finger_index,
                fingers=nf,
            )
            rects.extend(contact_rects)
            vias.extend(contact_vias)
    if (
        len(rects) == len(tuple(getattr(plan, "rects", ()) or ()))
        and len(vias) == len(tuple(getattr(plan, "vias", ()) or ()))
        and len(labels) == len(tuple(getattr(plan, "labels", ()) or ()))
    ):
        return plan
    return replace(plan, rects=tuple(rects), vias=tuple(vias), labels=tuple(labels))


def _apply_lvs_gate_landing_clips(plan: object, pdk: object, *, calibration_cache: object | None = None) -> object:
    """Clip template-gate M1 landings that edge-touch native MOS S/D columns.

    This is deliberately a final OA-plan ECO.  The routing stage may choose the
    CRN28 template gate access so Calibre keeps native multi-finger devices
    recognizable, but the generated M1 landing can exactly touch an adjacent
    calibrated source/drain column and short OUTN/VDD after streamout.  We keep
    the PO/CO gate assist unchanged and trim only the offending M1 rectangle.
    """

    from dataclasses import replace

    from analogskills.eda import OaRect, OaWritePlan
    from analogskills.pcell.access import PCellTerminalAccessor
    from analogskills.pcell.generation import PCellInstancePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    artifact_policy = metadata.get("artifact_policy", {}) if isinstance(metadata.get("artifact_policy", {}), Mapping) else {}
    pcell_access = metadata.get("pcell_access", {}) if isinstance(metadata.get("pcell_access", {}), Mapping) else {}
    gate_access_mode = str(pcell_access.get("mos_gate_access", "") or "").strip().lower().replace("-", "_")
    if not bool(artifact_policy.get("lvs_assist_only", False)) and gate_access_mode not in {"template", "pdk_template", "fallback", "force_template"}:
        return plan

    metal1 = str(getattr(pdk.layer_map, "metals", ("M1",))[0])
    clearance = max(float(getattr(pdk.rules, "grid_step_um", 0.001)), 0.005)
    accessor = PCellTerminalAccessor(
        pdk,  # type: ignore[arg-type]
        calibration_cache,  # type: ignore[arg-type]
        allow_nearest_calibration=calibration_cache is not None,
        max_nearest_distance=0.01,
    )
    rects = list(tuple(getattr(plan, "rects", ()) or ()))
    replacements: dict[int, OaRect] = {}
    for instance in tuple(getattr(plan, "instances", ()) or ()):
        logical = _assist_logical_mos_name(instance)
        if logical not in {"nmos", "pmos"}:
            continue
        connections = dict(getattr(instance, "connections", {}) or {})
        gate_net = str(connections.get("G", "") or "")
        if not gate_net:
            continue
        inst_plan = PCellInstancePlan(
            str(getattr(instance, "name", "")),
            logical,
            str(getattr(instance, "lib", getattr(instance, "lib_name", "")) or getattr(instance, "lib_name", "")),
            str(getattr(instance, "cell", getattr(instance, "cell_name", "")) or getattr(instance, "cell_name", "")),
            str(getattr(instance, "view", getattr(instance, "view_name", "layout")) or getattr(instance, "view_name", "layout")),
            params=dict(getattr(instance, "params", {}) or {}),
            xy_um=tuple(float(value) for value in getattr(instance, "xy", getattr(instance, "xy_um", (0.0, 0.0)))),
            orient=str(getattr(instance, "orient", "R0") or "R0"),
            connections=connections,
            instantiation_method=str(getattr(instance, "instantiation_method", "dbCreateInstByMasterName") or "dbCreateInstByMasterName"),
        )
        try:
            gate_pin = accessor.get_terminal_pin(inst_plan, "G")
        except Exception:
            continue
        gate_xy = tuple(float(value) for value in getattr(gate_pin, "xy_um", (0.0, 0.0)))
        sd_bboxes: list[tuple[float, float, float, float]] = []
        for sd_terminal in ("S", "D"):
            try:
                sd_pins = tuple(accessor.get_terminal_pins(inst_plan, sd_terminal, preferred_layers=(metal1,)))
            except Exception:
                sd_pins = ()
            # Multi-finger native PCells expose several adjacent S/D M1 columns.
            # Clipping against only the single "best" terminal pin can miss the
            # column next to a gate-access landing; that leaves a real M1
            # gate-to-S/D touch and Calibre later folds TAIL/OUT together.
            for sd_pin in sd_pins:
                if str(getattr(sd_pin, "layer", "") or "") != metal1:
                    continue
                sd_bbox = getattr(sd_pin, "bbox_um", None)
                if sd_bbox is not None:
                    sd_bboxes.append(tuple(float(value) for value in sd_bbox))
            if sd_pins:
                continue
            try:
                sd_pin = accessor.select_terminal_pin(
                    inst_plan,
                    sd_terminal,
                    require_lvs_safe=True,
                    preferred_layers=(metal1, *tuple(getattr(pdk.layer_map, "metals", ()) or ())),
                )
            except Exception:
                continue
            sd_bbox = getattr(sd_pin, "bbox_um", None)
            if sd_bbox is not None:
                sd_bboxes.append(tuple(float(value) for value in sd_bbox))
        if not sd_bboxes:
            continue
        for rect_index, rect in enumerate(rects):
            if rect_index in replacements:
                continue
            if str(getattr(rect, "net", "") or "") != gate_net or str(getattr(rect, "layer", "") or "") != metal1:
                continue
            rect_metadata = dict(getattr(rect, "metadata", {}) or {})
            if str(rect_metadata.get("kind", "") or "") != "via_landing":
                continue
            x0, y0, x1, y1 = tuple(float(value) for value in getattr(rect, "bbox", (0.0, 0.0, 0.0, 0.0)))
            if abs(0.5 * (y0 + y1) - gate_xy[1]) > 0.25:
                continue
            new_bbox = (x0, y0, x1, y1)
            for sx0, sy0, sx1, sy1 in sd_bboxes:
                if x1 <= sx0 or x0 >= sx1:
                    continue
                if sy0 >= gate_xy[1] and y0 < sy0 and y1 >= sy0:
                    clipped_y1 = pdk.rules.snap_um(sy0 - clearance)
                    if clipped_y1 > y0 + pdk.rules.grid_step_um:
                        new_bbox = (new_bbox[0], new_bbox[1], new_bbox[2], min(new_bbox[3], clipped_y1))
                elif sy1 <= gate_xy[1] and y0 <= sy1 and y1 > sy1:
                    clipped_y0 = pdk.rules.snap_um(sy1 + clearance)
                    if clipped_y0 < y1 - pdk.rules.grid_step_um:
                        new_bbox = (new_bbox[0], max(new_bbox[1], clipped_y0), new_bbox[2], new_bbox[3])
            if new_bbox == (x0, y0, x1, y1):
                continue
            rect_metadata["lvs_gate_landing_clip"] = {
                "source_instance": inst_plan.name,
                "source_terminal": "G",
                "reason": "avoid_adjacent_sd_m1_edge_touch",
                "clearance_um": clearance,
                "original_bbox": (x0, y0, x1, y1),
            }
            replacements[rect_index] = OaRect(
                rect.layer,
                rect.purpose,
                pdk.rules.snap_bbox_um(new_bbox, mode="nearest"),
                rect.net,
                getattr(rect, "color", ""),
                rect_metadata,
            )
    if not replacements:
        return plan
    return replace(plan, rects=tuple(replacements.get(index, rect) for index, rect in enumerate(rects)))


def _apply_lvs_multifinger_marker_assist(plan: object, pdk: object, *, calibration_cache: object | None = None) -> object:
    """Add LVS-only LVSDMY2 markers for native multi-finger MOS recognition.

    The foundry LVS deck derives multi-finger devices from ``LVSDMY/dummy2``
    (GDS 208/2, rule layer ``LVSDMY2``).  Some native PCell streamouts expose
    only a partial marker, so Calibre counts too few gates in the multi-finger
    region.  The preferred ``tight_od`` mode mirrors the single-device sweep:
    cover exactly the active diffusion span inferred from the alternating S/D
    columns.  It is emitted only into the LVS-assist artifact, never into the
    primary/signoff layout.
    """

    from dataclasses import replace

    from analogskills.eda import OaRect, OaWritePlan
    from analogskills.pcell.access import PCellTerminalAccessor
    from analogskills.pcell.generation import PCellInstancePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    artifact_policy = metadata.get("artifact_policy", {}) if isinstance(metadata.get("artifact_policy", {}), Mapping) else {}
    if not bool(artifact_policy.get("lvs_assist_only", False)):
        return plan
    mode = str(artifact_policy.get("lvs_multifinger_marker_assist", "off") or "off").strip().lower()
    if mode in {"", "none", "off", "disabled", "false", "0"}:
        return plan
    accessor = PCellTerminalAccessor(
        pdk,  # type: ignore[arg-type]
        calibration_cache,  # type: ignore[arg-type]
        allow_nearest_calibration=calibration_cache is not None,
        max_nearest_distance=0.01,
    )
    rects = list(tuple(getattr(plan, "rects", ()) or ()))
    added = 0
    active_layer = str(getattr(pdk.layer_map, "active", "OD") or "OD")
    marker_margin = max(float(getattr(pdk.rules, "grid_step_um", 0.001)), 0.005)
    for instance in tuple(getattr(plan, "instances", ()) or ()):
        logical = _assist_logical_mos_name(instance)
        if logical not in {"nmos", "pmos"}:
            continue
        params = dict(getattr(instance, "params", {}) or {})
        try:
            nf = int(params.get("fingers", params.get("nf", 1)) or 1)
        except (TypeError, ValueError):
            nf = 1
        if nf <= 1:
            continue
        inst_plan = PCellInstancePlan(
            str(getattr(instance, "name", "")),
            logical,
            str(getattr(instance, "lib", getattr(instance, "lib_name", "")) or getattr(instance, "lib_name", "")),
            str(getattr(instance, "cell", getattr(instance, "cell_name", "")) or getattr(instance, "cell_name", "")),
            str(getattr(instance, "view", getattr(instance, "view_name", "layout")) or getattr(instance, "view_name", "layout")),
            params=params,
            xy_um=tuple(float(value) for value in getattr(instance, "xy", getattr(instance, "xy_um", (0.0, 0.0)))),
            orient=str(getattr(instance, "orient", "R0") or "R0"),
            connections=dict(getattr(instance, "connections", {}) or {}),
            instantiation_method=str(getattr(instance, "instantiation_method", "dbCreateInstByMasterName") or "dbCreateInstByMasterName"),
        )
        if mode in {"tight", "tight_od", "od", "od_bbox"}:
            marker_bbox = _assist_tight_multifinger_marker_bbox(pdk, inst_plan, nf=nf)
        else:
            bboxes: list[tuple[float, float, float, float]] = []
            for terminal in ("S", "D"):
                try:
                    pins = tuple(accessor.get_terminal_pins(inst_plan, terminal, preferred_layers=(active_layer, "M1")))
                except Exception:
                    pins = ()
                active_bboxes = [
                    tuple(float(value) for value in getattr(pin, "bbox_um", ()))
                    for pin in pins
                    if str(getattr(pin, "layer", "") or "") == active_layer and getattr(pin, "bbox_um", None) is not None
                ]
                if active_bboxes:
                    bboxes.extend(active_bboxes)
                    continue
                bboxes.extend(
                    tuple(float(value) for value in getattr(pin, "bbox_um", ()))
                    for pin in pins
                    if getattr(pin, "bbox_um", None) is not None
                )
            if not bboxes:
                continue
            x0 = min(bbox[0] for bbox in bboxes)
            y0 = min(bbox[1] for bbox in bboxes)
            x1 = max(bbox[2] for bbox in bboxes)
            y1 = max(bbox[3] for bbox in bboxes)
            marker_bbox = pdk.rules.snap_bbox_um(
                (x0 - marker_margin, y0 - marker_margin, x1 + marker_margin, y1 + marker_margin),
                mode="outward",
            )
        if marker_bbox is None:
            continue
        rects.append(
            OaRect(
                "LVSDMY",
                "dummy2",
                marker_bbox,
                "",
                metadata={
                    "kind": "lvs_multifinger_marker_assist",
                    "source_instance": inst_plan.name,
                    "fingers": nf,
                    "lvs_assist_only": True,
                },
            )
        )
        added += 1
    if not added:
        return plan
    return replace(plan, rects=tuple(rects))


def _apply_lvs_assist_top_rail_clip(plan: object, pdk: object) -> object:
    """Remove LVS-assist top VDD rail segments that overlap signal ports.

    The primary layout may legally route an upper signal metal over an M1 supply
    rail.  In the LVS-assist artifact, extra PMOS S/D VIA1/M2 straps can make the
    foundry extractor include that overlap in a reported port short.  This ECO is
    deliberately narrow: it runs only for the assist artifact and only when the
    policy requests it, and it removes large top-level VDD/M1 rail rectangles
    whose xy projection overlaps non-supply routes on upper metals.
    """

    from dataclasses import replace

    from analogskills.eda import OaPin, OaWritePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    artifact_policy = metadata.get("artifact_policy", {}) if isinstance(metadata.get("artifact_policy", {}), Mapping) else {}
    if not bool(artifact_policy.get("lvs_assist_only", False)):
        return plan
    if not bool(artifact_policy.get("lvs_clip_top_supply_rail", False)):
        return plan

    metals = tuple(getattr(getattr(pdk, "layer_map", object()), "metals", ()) or ())
    if not metals:
        return plan
    m1 = str(metals[0])
    upper_metals = {str(layer) for layer in metals[2:]}
    if not upper_metals:
        return plan

    supply_nets = {
        str(net).upper()
        for net in ("VDD", "VDDA", "VSS", "GND")
    }
    signal_bboxes: list[tuple[float, float, float, float]] = []
    for rect in tuple(getattr(plan, "rects", ()) or ()):
        layer = str(getattr(rect, "layer", "") or "")
        net = str(getattr(rect, "net", "") or "")
        if layer in upper_metals and net and net.upper() not in supply_nets:
            signal_bboxes.append(tuple(float(value) for value in getattr(rect, "bbox", (0, 0, 0, 0))))
    for path in tuple(getattr(plan, "paths", ()) or ()):
        layer = str(getattr(path, "layer", "") or "")
        net = str(getattr(path, "net", "") or "")
        if layer in upper_metals and net and net.upper() not in supply_nets:
            signal_bboxes.extend(_assist_path_bboxes(path))
    if not signal_bboxes:
        return plan

    min_rail_width = max(1.0, 10.0 * float(getattr(pdk.rules, "min_width_um", lambda _layer: 0.05)(m1)))
    kept_rects: list[object] = []
    removed = 0
    for rect in tuple(getattr(plan, "rects", ()) or ()):
        layer = str(getattr(rect, "layer", "") or "")
        net = str(getattr(rect, "net", "") or "")
        bbox = tuple(float(value) for value in getattr(rect, "bbox", (0, 0, 0, 0)))
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        is_candidate = (
            layer == m1
            and net.upper() == "VDD"
            and width >= min_rail_width
            and width > 5.0 * max(height, 1e-6)
            and not (getattr(rect, "metadata", {}) or {})
        )
        if is_candidate and any(_assist_bbox_overlaps(bbox, signal_bbox) for signal_bbox in signal_bboxes):
            removed += 1
            continue
        kept_rects.append(rect)
    if not removed:
        return plan
    return replace(plan, rects=tuple(kept_rects))


def _apply_lvs_assist_pmos_vdd_labels(plan: object, pdk: object) -> object:
    """Stamp LVS-only VDD labels on PMOS source islands and NW bodies.

    The native CRN28 PMOS PCell extracts its body as an NW net.  In the LVS
    assist artifact we deliberately clip some top-level VDD rail geometry to
    avoid false upper-metal shorts.  That can leave right-side PMOS VDD source
    islands and all PMOS body regions electrically isolated in Calibre even
    though the source CDL ties them to VDD.  These labels are LVS-assist-only:
    they name the PCell-local VDD islands/body regions without modifying the
    primary/signoff layout.
    """

    from dataclasses import replace

    from analogskills.eda import OaPin, OaWritePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    artifact_policy = metadata.get("artifact_policy", {}) if isinstance(metadata.get("artifact_policy", {}), Mapping) else {}
    if not bool(artifact_policy.get("lvs_assist_only", False)):
        return plan
    if not bool(artifact_policy.get("lvs_label_pmos_vdd_regions", False)):
        return plan

    metals = tuple(getattr(getattr(pdk, "layer_map", object()), "metals", ()) or ())
    metal1 = str(metals[0] if metals else "M1")
    wells = getattr(getattr(pdk, "layer_map", object()), "wells", {}) or {}
    nwell = str(wells.get("nwell", "NW") if isinstance(wells, Mapping) else "NW")

    pins = list(tuple(getattr(plan, "pins", ()) or ()))
    seen: set[tuple[str, str, float, float, float, float]] = set()
    for pin in pins:
        bbox = getattr(pin, "bbox", None)
        if bbox is None:
            continue
        bx0, by0, bx1, by1 = tuple(float(value) for value in bbox)
        seen.add((str(getattr(pin, "name", "") or ""), str(getattr(pin, "layer", "") or ""), round(bx0, 6), round(by0, 6), round(bx1, 6), round(by1, 6)))

    def add_pin(layer: str, xy: tuple[float, float], *, half_size: float = 0.0275) -> None:
        snapped = pdk.rules.snap_point_um((float(xy[0]), float(xy[1])))
        bbox = pdk.rules.snap_bbox_um(
            (snapped[0] - half_size, snapped[1] - half_size, snapped[0] + half_size, snapped[1] + half_size),
            mode="outward",
        )
        key = ("VDD", str(layer), round(bbox[0], 6), round(bbox[1], 6), round(bbox[2], 6), round(bbox[3], 6))
        if key in seen:
            return
        seen.add(key)
        pins.append(OaPin("VDD", "VDD", "inputOutput", str(layer), bbox, emit_draw_rect=False))

    added = 0
    for instance in tuple(getattr(plan, "instances", ()) or ()):
        if _assist_logical_mos_name(instance) != "pmos":
            continue
        connections = dict(getattr(instance, "connections", {}) or {})
        if str(connections.get("B", "") or "").upper() != "VDD":
            continue
        try:
            nf = int(dict(getattr(instance, "params", {}) or {}).get("fingers", dict(getattr(instance, "params", {}) or {}).get("nf", 1)) or 1)
        except (TypeError, ValueError):
            nf = 1
        source_points, drain_points = _assist_fallback_sd_points(pdk, instance, nf=nf)
        all_points = tuple((*source_points, *drain_points))
        if not all_points:
            continue

        if str(connections.get("S", "") or "").upper() == "VDD" and source_points:
            # A single M1 text on the local source island is enough for Calibre
            # to merge split VDD source islands with the top-level VDD net.
            add_pin(metal1, source_points[0])
            added += 1

        params = dict(getattr(instance, "params", {}) or {})
        origin = tuple(float(value) for value in tuple(getattr(instance, "xy", getattr(instance, "xy_um", (0.0, 0.0))))[:2])
        wfg_um = _assist_wfg_um(params)
        # The native PMOS NW bbox observed in CRN28 streamout encloses the
        # alternating S/D columns with about 0.15um lateral and 0.105um vertical
        # enclosure.  Put the label safely inside the region center rather than
        # on its edge so streamout text stamping remains stable.
        x0 = min(point[0] for point in all_points) - 0.15
        x1 = max(point[0] for point in all_points) + 0.15
        y0 = origin[1] - 0.105
        y1 = origin[1] + wfg_um + 0.105
        add_pin(nwell, ((x0 + x1) * 0.5, (y0 + y1) * 0.5))
        added += 1

    if not added:
        return plan
    return replace(plan, pins=tuple(pins))


def _apply_lvs_assist_pmos_vdd_bridges(plan: object, pdk: object) -> object:
    """Add LVS-only local geometry to tie PMOS VDD source/body islands.

    This stays geometry-based, not text-label based.  The CRN28 streamout maps
    ordinary explicit labels to GDS texttype 0, which Calibre does not use as a
    metal/well label in this flow.

    The first version used an M2 bridge from the top well tap down to the
    right-side PMOS source island.  That crossed the OUTN M2 strap around
    y=7.135um and shorted the PMOS body/source to OUTN.  The safer ECO is:

    * a narrow M1 bridge near the top of the PMOS row, where only VDD/source M1
      columns and the well-tap M1 exist;
    * narrow NW bridges only across the well gaps needed to connect the four
      PMOS wells to the existing VDD well tap.
    """

    from dataclasses import replace

    from analogskills.eda import OaRect, OaWritePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    artifact_policy = metadata.get("artifact_policy", {}) if isinstance(metadata.get("artifact_policy", {}), Mapping) else {}
    if not bool(artifact_policy.get("lvs_assist_only", False)):
        return plan
    if not bool(artifact_policy.get("lvs_bridge_pmos_vdd_regions", False)):
        return plan

    metals = tuple(getattr(getattr(pdk, "layer_map", object()), "metals", ()) or ())
    if not metals:
        return plan
    metal1 = str(metals[0])
    wells = getattr(getattr(pdk, "layer_map", object()), "wells", {}) or {}
    nwell = str(wells.get("nwell", "NW") if isinstance(wells, Mapping) else "NW")

    pmos_regions: list[tuple[float, float, float, float]] = []
    left_source_candidates: list[tuple[float, float]] = []
    right_source_candidates: list[tuple[float, float]] = []
    for instance in tuple(getattr(plan, "instances", ()) or ()):
        if _assist_logical_mos_name(instance) != "pmos":
            continue
        connections = dict(getattr(instance, "connections", {}) or {})
        if str(connections.get("B", "") or "").upper() != "VDD":
            continue
        try:
            nf = int(dict(getattr(instance, "params", {}) or {}).get("fingers", dict(getattr(instance, "params", {}) or {}).get("nf", 1)) or 1)
        except (TypeError, ValueError):
            nf = 1
        source_points, drain_points = _assist_fallback_sd_points(pdk, instance, nf=nf)
        all_points = tuple((*source_points, *drain_points))
        if not all_points:
            continue
        params = dict(getattr(instance, "params", {}) or {})
        origin = tuple(float(value) for value in tuple(getattr(instance, "xy", getattr(instance, "xy_um", (0.0, 0.0))))[:2])
        wfg_um = _assist_wfg_um(params)
        pmos_regions.append(
            (
                min(point[0] for point in all_points) - 0.18,
                origin[1] - 0.13,
                max(point[0] for point in all_points) + 0.18,
                origin[1] + wfg_um + 0.13,
            )
        )
        if str(connections.get("S", "") or "").upper() == "VDD" and source_points:
            orient = str(getattr(instance, "orient", "R0") or "R0")
            if "MY" in orient:
                right_source_candidates.append(min(source_points, key=lambda point: point[0]))
            else:
                left_source_candidates.append(min(source_points, key=lambda point: point[0]))

    if not pmos_regions:
        return plan

    tap_nwell_bboxes: list[tuple[float, float, float, float]] = []
    tap_m1_bboxes: list[tuple[float, float, float, float]] = []
    for rect in tuple(getattr(plan, "rects", ()) or ()):
        layer = str(getattr(rect, "layer", "") or "")
        bbox = tuple(float(value) for value in getattr(rect, "bbox", (0, 0, 0, 0)))
        if len(bbox) != 4:
            continue
        if layer == nwell:
            tap_nwell_bboxes.append(bbox)
        elif layer == metal1 and str(getattr(rect, "net", "") or "").upper() == "VDD":
            tap_m1_bboxes.append(bbox)

    rects = list(tuple(getattr(plan, "rects", ()) or ()))

    def append_rect(layer: str, bbox: tuple[float, float, float, float], net: str, kind: str) -> None:
        snapped = pdk.rules.snap_bbox_um(bbox, mode="outward")
        if snapped[2] <= snapped[0] or snapped[3] <= snapped[1]:
            return
        rects.append(
            OaRect(
                layer,
                "drawing",
                snapped,
                net,
                metadata={"kind": kind, "lvs_assist_only": True},
            )
        )

    # Source island repair: use the top PMOS M1 source rail area, not M2, so
    # the ECO does not cross the OUTN M2 strap.
    if left_source_candidates and right_source_candidates:
        left_xy = max(left_source_candidates, key=lambda point: point[1])
        right_xy = max(right_source_candidates, key=lambda point: point[1])
        top_m1 = max(tap_m1_bboxes, key=lambda bbox: bbox[3]) if tap_m1_bboxes else None
        width = pdk.rules.snap_dimension_ceil_um(max(float(getattr(pdk.rules, "min_width_um", lambda _layer: 0.05)(metal1)), 0.05))
        half = 0.5 * width
        if top_m1 is not None:
            bridge_y = pdk.rules.snap_um(float(top_m1[1]) + half)
        else:
            bridge_y = pdk.rules.snap_um(max(left_xy[1], right_xy[1]) + 0.65)
        margin_x = max(0.08, width)
        append_rect(
            metal1,
            (
                min(left_xy[0], right_xy[0]) - margin_x,
                bridge_y - half,
                max(left_xy[0], right_xy[0]) + margin_x,
                bridge_y + half,
            ),
            "VDD",
            "lvs_assist_pmos_vdd_m1_source_bridge",
        )

    # Body repair: merge PMOS nwell islands through only the empty gaps, then
    # merge the top PMOS wells into the existing VDD nwell tap.
    nwell_meta_kind = "lvs_assist_pmos_vdd_nwell_gap_bridge"
    nwell_overlap = max(0.06, 2.0 * float(getattr(pdk.rules, "grid_step_um", 0.001) or 0.001))
    for lower in pmos_regions:
        for upper in pmos_regions:
            if upper[1] <= lower[1]:
                continue
            gap = upper[1] - lower[3]
            if gap <= 0.0 or gap > 0.80:
                continue
            overlap_x0 = max(lower[0], upper[0])
            overlap_x1 = min(lower[2], upper[2])
            if overlap_x1 - overlap_x0 < 0.05:
                continue
            append_rect(nwell, (overlap_x0, lower[3] - nwell_overlap, overlap_x1, upper[1] + nwell_overlap), "", nwell_meta_kind)

    if tap_nwell_bboxes:
        tap_nwell = max(tap_nwell_bboxes, key=lambda bbox: bbox[3])
        for region in pmos_regions:
            gap = tap_nwell[1] - region[3]
            if gap <= 0.0 or gap > 0.30:
                continue
            overlap_x0 = max(region[0], tap_nwell[0])
            overlap_x1 = min(region[2], tap_nwell[2])
            if overlap_x1 - overlap_x0 < 0.05:
                continue
            append_rect(nwell, (overlap_x0, region[3] - nwell_overlap, overlap_x1, tap_nwell[1] + nwell_overlap), "", nwell_meta_kind)

    return replace(plan, rects=tuple(rects))


def _apply_configured_pmos_nwell_bridges(plan: object, pdk: object) -> object:
    """Add real configured NW bridges that tie PMOS well islands to the tap.

    This is a signoff geometry ECO, unlike the LVS-only source/body assist
    geometry above.  CRN28 native PMOS PCells stream out separate NW islands;
    if the explicit VDD well tap is not merged into the same NW region, Calibre
    LUP.6 sees every PMOS S/D column as lacking a nearby NW strap.  Keep the
    policy in PDK metadata so the dimensions are rule/config controlled.
    """

    from dataclasses import replace

    from analogskills.eda import OaRect, OaWritePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    routing_geometry = metadata.get("routing_geometry", {}) if isinstance(metadata.get("routing_geometry", {}), Mapping) else {}
    config = routing_geometry.get("strongarm_pmos_nwell_bridge", {}) if isinstance(routing_geometry.get("strongarm_pmos_nwell_bridge", {}), Mapping) else {}
    if not bool(config.get("enabled", False)):
        return plan

    wells = getattr(getattr(pdk, "layer_map", object()), "wells", {}) or {}
    default_nwell = str(wells.get("nwell", "NW") if isinstance(wells, Mapping) else "NW")
    nwell = str(config.get("layer", default_nwell) or default_nwell)
    try:
        max_row_gap_um = float(config.get("max_row_gap_nm", 800.0) or 0.0) * 1e-3
    except (TypeError, ValueError):
        max_row_gap_um = 0.0
    try:
        max_tap_gap_um = float(config.get("max_tap_gap_nm", 300.0) or 0.0) * 1e-3
    except (TypeError, ValueError):
        max_tap_gap_um = 0.0
    try:
        overlap_um = float(config.get("overlap_nm", 60.0) or 0.0) * 1e-3
    except (TypeError, ValueError):
        overlap_um = 0.0
    overlap_um = max(overlap_um, 2.0 * float(getattr(pdk.rules, "grid_step_um", 0.001) or 0.001))
    if max_row_gap_um <= 0.0 and max_tap_gap_um <= 0.0:
        return plan

    pmos_regions: list[tuple[float, float, float, float]] = []
    for instance in tuple(getattr(plan, "instances", ()) or ()):
        if _assist_logical_mos_name(instance) != "pmos":
            continue
        connections = dict(getattr(instance, "connections", {}) or {})
        if str(connections.get("B", "") or "").upper() != "VDD":
            continue
        try:
            nf = int(dict(getattr(instance, "params", {}) or {}).get("fingers", dict(getattr(instance, "params", {}) or {}).get("nf", 1)) or 1)
        except (TypeError, ValueError):
            nf = 1
        source_points, drain_points = _assist_fallback_sd_points(pdk, instance, nf=nf)
        all_points = tuple((*source_points, *drain_points))
        if not all_points:
            continue
        params = dict(getattr(instance, "params", {}) or {})
        origin = tuple(float(value) for value in tuple(getattr(instance, "xy", getattr(instance, "xy_um", (0.0, 0.0))))[:2])
        wfg_um = _assist_wfg_um(params)
        pmos_regions.append(
            (
                min(point[0] for point in all_points) - 0.18,
                origin[1] - 0.13,
                max(point[0] for point in all_points) + 0.18,
                origin[1] + wfg_um + 0.13,
            )
        )
    if not pmos_regions:
        return plan

    tap_nwell_bboxes: list[tuple[float, float, float, float]] = []
    for rect in tuple(getattr(plan, "rects", ()) or ()):
        if str(getattr(rect, "layer", "") or "") != nwell:
            continue
        try:
            bbox = tuple(float(value) for value in getattr(rect, "bbox", ()))
        except (TypeError, ValueError):
            continue
        if len(bbox) == 4:
            tap_nwell_bboxes.append(bbox)

    rects = list(tuple(getattr(plan, "rects", ()) or ()))

    def append_bridge(bbox: tuple[float, float, float, float], reason: str) -> None:
        snapped = pdk.rules.snap_bbox_um(bbox, mode="outward")
        if snapped[2] <= snapped[0] or snapped[3] <= snapped[1]:
            return
        rects.append(
            OaRect(
                nwell,
                "drawing",
                snapped,
                "",
                metadata={
                    "kind": "strongarm_pmos_nwell_bridge",
                    "reason": reason,
                    "rule_ids": tuple(str(item) for item in tuple(config.get("rule_ids", ()) or ()) if str(item)),
                },
            )
        )

    mode = str(config.get("mode", "gap_bridge") or "gap_bridge").strip().lower()
    if mode in {"cover_bbox", "cover", "merged_cover"}:
        cover_regions = list(pmos_regions)
        if tap_nwell_bboxes:
            cover_regions.append(max(tap_nwell_bboxes, key=lambda bbox: bbox[3]))
        try:
            margin_um = float(config.get("cover_margin_nm", 0.0) or 0.0) * 1e-3
        except (TypeError, ValueError):
            margin_um = 0.0
        append_bridge(
            (
                min(region[0] for region in cover_regions) - margin_um,
                min(region[1] for region in pmos_regions) - margin_um,
                max(region[2] for region in cover_regions) + margin_um,
                max(region[3] for region in cover_regions) + margin_um,
            ),
            "pmos_nwell_cover_bbox",
        )
        return replace(plan, rects=tuple(rects))

    for lower in pmos_regions:
        for upper in pmos_regions:
            if upper[1] <= lower[1]:
                continue
            gap = upper[1] - lower[3]
            if gap <= 0.0 or (max_row_gap_um > 0.0 and gap > max_row_gap_um):
                continue
            overlap_x0 = max(lower[0], upper[0])
            overlap_x1 = min(lower[2], upper[2])
            if overlap_x1 - overlap_x0 < 0.05:
                continue
            append_bridge((overlap_x0, lower[3] - overlap_um, overlap_x1, upper[1] + overlap_um), "pmos_row_gap")

    if tap_nwell_bboxes and max_tap_gap_um > 0.0:
        tap_nwell = max(tap_nwell_bboxes, key=lambda bbox: bbox[3])
        for region in pmos_regions:
            gap = tap_nwell[1] - region[3]
            if gap <= 0.0 or gap > max_tap_gap_um:
                continue
            overlap_x0 = max(region[0], tap_nwell[0])
            overlap_x1 = min(region[2], tap_nwell[2])
            if overlap_x1 - overlap_x0 < 0.05:
                continue
            append_bridge((overlap_x0, region[3] - overlap_um, overlap_x1, tap_nwell[1] + overlap_um), "pmos_to_vdd_tap")

    if len(rects) == len(tuple(getattr(plan, "rects", ()) or ())):
        return plan
    return replace(plan, rects=tuple(rects))


def _apply_lvs_assist_tail_scaffold(plan: object, pdk: object) -> object:
    """Replace over-passing TAIL routes with a local M2 LVS scaffold."""

    from dataclasses import replace

    from analogskills.eda import OaRect, OaWritePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    artifact_policy = metadata.get("artifact_policy", {}) if isinstance(metadata.get("artifact_policy", {}), Mapping) else {}
    if not bool(artifact_policy.get("lvs_assist_only", False)):
        return plan
    if not bool(artifact_policy.get("lvs_tail_m2_scaffold", False)):
        return plan

    metals = tuple(getattr(getattr(pdk, "layer_map", object()), "metals", ()) or ())
    if len(metals) < 2:
        return plan
    m2 = str(metals[1])
    tail = "TAIL"
    assist_kind = "lvs_assist_sd_m2_strap"
    tail_trunks: list[tuple[float, float, float, float]] = []
    for rect in tuple(getattr(plan, "rects", ()) or ()):
        if str(getattr(rect, "layer", "") or "") != m2 or str(getattr(rect, "net", "") or "") != tail:
            continue
        meta = getattr(rect, "metadata", {}) or {}
        if meta.get("kind") != assist_kind:
            continue
        bbox = tuple(float(value) for value in getattr(rect, "bbox", (0, 0, 0, 0)))
        if (bbox[2] - bbox[0]) > 2.0 * max(bbox[3] - bbox[1], 1e-9):
            tail_trunks.append(bbox)
    if len(tail_trunks) < 3:
        return plan

    y_groups: dict[float, list[tuple[float, float, float, float]]] = {}
    for bbox in tail_trunks:
        yc = round((bbox[1] + bbox[3]) * 0.5, 3)
        y_groups.setdefault(yc, []).append(bbox)
    if len(y_groups) < 3:
        return plan
    rows = sorted((y, bboxes) for y, bboxes in y_groups.items())
    low_y, low_bboxes = rows[0]
    mid_y, mid_bboxes = rows[1]
    high_y, high_bboxes = rows[-1]
    all_bboxes = [bbox for _y, bboxes in rows for bbox in bboxes]
    min_x = min(bbox[0] for bbox in all_bboxes)
    max_x = max(bbox[2] for bbox in all_bboxes)
    left_x = min((bbox[0] + bbox[2]) * 0.5 for bbox in high_bboxes)
    right_x = max((bbox[0] + bbox[2]) * 0.5 for bbox in high_bboxes)
    low_x = (min(bbox[0] for bbox in low_bboxes) + max(bbox[2] for bbox in low_bboxes)) * 0.5

    width = pdk.rules.snap_dimension_ceil_um(max(float(getattr(pdk.rules, "min_width_um", lambda _layer: 0.05)(m2)), 0.05))
    half = 0.5 * width

    def hrect(x0: float, x1: float, y: float) -> tuple[float, float, float, float]:
        return pdk.rules.snap_bbox_um((min(x0, x1), y - half, max(x0, x1), y + half), mode="outward")

    def vrect(x: float, y0: float, y1: float) -> tuple[float, float, float, float]:
        return pdk.rules.snap_bbox_um((x - half, min(y0, y1), x + half, max(y0, y1)), mode="outward")

    scaffold_bboxes = (
        hrect(min_x, max_x, mid_y),
        vrect(left_x, mid_y, high_y),
        vrect(right_x, mid_y, high_y),
        vrect(low_x, low_y, mid_y),
    )
    scaffold_meta = {"kind": "lvs_assist_tail_m2_scaffold", "lvs_assist_only": True}
    scaffold_rects = tuple(OaRect(m2, "drawing", bbox, tail, metadata=scaffold_meta) for bbox in scaffold_bboxes)

    kept_rects = tuple(
        rect
        for rect in tuple(getattr(plan, "rects", ()) or ())
        if not (
            str(getattr(rect, "net", "") or "") == tail
            and (getattr(rect, "metadata", {}) or {}).get("kind") != assist_kind
        )
    )
    kept_paths = tuple(path for path in tuple(getattr(plan, "paths", ()) or ()) if str(getattr(path, "net", "") or "") != tail)
    kept_vias = tuple(
        via
        for via in tuple(getattr(plan, "vias", ()) or ())
        if not (
            str(getattr(via, "net", "") or "") == tail
            and (getattr(via, "metadata", {}) or {}).get("kind") != assist_kind
        )
    )
    return replace(plan, rects=(*kept_rects, *scaffold_rects), paths=kept_paths, vias=kept_vias)


def _assist_tight_multifinger_marker_bbox(pdk: object, instance: object, *, nf: int) -> tuple[float, float, float, float] | None:
    source_points, drain_points = _assist_fallback_sd_points(pdk, instance, nf=nf)
    points = tuple((*source_points, *drain_points))
    if not points:
        return None
    params = dict(getattr(instance, "params", {}) or {})
    wfg_um = _assist_wfg_um(params)
    origin = tuple(float(value) for value in tuple(getattr(instance, "xy_um", getattr(instance, "xy", (0.0, 0.0))))[:2])
    min_width = 0.5 * float(getattr(pdk.rules, "min_width_um", lambda _layer: 0.05)("M1"))
    x0 = min(point[0] for point in points) - min_width
    x1 = max(point[0] for point in points) + min_width
    y0 = origin[1]
    y1 = origin[1] + wfg_um
    if y1 < y0:
        y0, y1 = y1, y0
    return pdk.rules.snap_bbox_um((x0, y0, x1, y1), mode="outward")


def _assist_logical_mos_name(instance: object) -> str:
    logical = str(getattr(instance, "logical_name", "") or "").lower()
    if logical in {"nmos", "pmos"}:
        return logical
    cell = str(getattr(instance, "cell", getattr(instance, "cell_name", "")) or "").lower()
    if "nch" in cell or cell.startswith("nmos"):
        return "nmos"
    if "pch" in cell or cell.startswith("pmos"):
        return "pmos"
    return ""


def _assist_gate_access_points(pdk: object, first_xy: tuple[float, float], *, nf: int, orient: str) -> tuple[tuple[float, float], ...]:
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    access = metadata.get("pcell_access", {}) if isinstance(metadata.get("pcell_access", {}), Mapping) else {}
    strap = access.get("multifinger_gate_strap", {}) if isinstance(access.get("multifinger_gate_strap", {}), Mapping) else {}
    try:
        pitch_nm = float(strap.get("pitch_nm", 300) or 300)
    except (TypeError, ValueError):
        pitch_nm = 300.0
    step = max(pitch_nm, 1.0) * 1e-3
    if "MY" in str(orient):
        step = -step
    return tuple(pdk.rules.snap_point_um((first_xy[0] + index * step, first_xy[1])) for index in range(max(1, nf)))


def _assist_gate_poly_bridge_points(
    pdk: object,
    points: tuple[tuple[float, float], ...],
    *,
    logical: str,
    params: Mapping[str, object],
) -> tuple[tuple[float, float], ...]:
    if not points:
        return ()
    y = float(points[0][1])
    if logical == "nmos":
        y += _assist_wfg_um(params) + 0.08
    elif logical == "pmos":
        y -= 0.02
    return tuple(pdk.rules.snap_point_um((point[0], y)) for point in points)


def _assist_wfg_um(params: Mapping[str, object]) -> float:
    value = params.get("Wfg", params.get("wfg", params.get("wf", params.get("W", params.get("w", 0.8e-6)))))
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.8
    # Native CRN28 PCell parameters are stored in meters in the generated plan.
    if number < 1e-3:
        return number * 1e6
    return number


def _assist_source_drain_strap_geometry(
    pdk: object,
    instance: object,
    *,
    nf: int,
    accessor: object | None = None,
    existing_rects: tuple[object, ...] = (),
    existing_paths: tuple[object, ...] = (),
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    from analogskills.eda import OaRect, OaVia

    connections = dict(getattr(instance, "connections", {}) or {})
    source_net = str(connections.get("S", "") or "")
    drain_net = str(connections.get("D", "") or "")
    if not source_net and not drain_net:
        return (), ()
    calibrated = _assist_calibrated_sd_points(pdk, instance, accessor)
    if calibrated:
        source_points = calibrated.get("S", ())
        drain_points = calibrated.get("D", ())
    else:
        source_points, drain_points = _assist_fallback_sd_points(pdk, instance, nf=nf)
    logical = _assist_logical_mos_name(instance)
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    artifact_policy = metadata.get("artifact_policy", {}) if isinstance(metadata.get("artifact_policy", {}), Mapping) else {}
    if logical == "nmos" and str(artifact_policy.get("lvs_nmos_sd_access_bias", "") or "").strip().lower() == "lower_half":
        source_points = _assist_lower_half_sd_points(pdk, instance, source_points)
        drain_points = _assist_lower_half_sd_points(pdk, instance, drain_points)
    rects: list[object] = []
    vias: list[object] = []
    prefer_below = logical == "pmos"
    for terminal, net, term_points in (("S", source_net, source_points), ("D", drain_net, drain_points)):
        if not net or len(term_points) < 2:
            continue
        term_metadata = {
            "kind": "lvs_assist_sd_m2_strap",
            "source_instance": str(getattr(instance, "name", "")),
            "terminal": terminal,
            "fingers": nf,
            "lvs_assist_only": True,
        }
        strap_layer = _assist_sd_strap_layer(pdk)
        trunk_y = _assist_choose_sd_trunk_y(
            pdk,
            term_points,
            terminal=terminal,
            net=net,
            layer=strap_layer,
            existing_rects=tuple((*existing_rects, *rects)),
            existing_paths=existing_paths,
            prefer_below=prefer_below,
        )
        rects.extend(
            OaRect(strap_layer, "drawing", bbox, net, metadata=term_metadata)
            for bbox in _assist_sd_strap_bboxes(pdk, term_points, trunk_y, layer=strap_layer)
        )
        for finger_index, point in enumerate(term_points):
            if _assist_point_has_same_net_access(
                point,
                net=net,
                layer=strap_layer,
                existing_rects=existing_rects,
                existing_paths=existing_paths,
            ):
                continue
            vias.append(OaVia(_assist_sd_strap_via(pdk), point, net, metadata={**term_metadata, "strap_index": finger_index}))
    return tuple(rects), tuple(vias)


def _assist_sd_strap_labels(rects: tuple[object, ...]) -> tuple[tuple[str, str, tuple[float, float]], ...]:
    labels: list[tuple[str, str, tuple[float, float]]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for rect in rects:
        metadata = getattr(rect, "metadata", {}) or {}
        if metadata.get("kind") != "lvs_assist_sd_m2_strap":
            continue
        net = str(getattr(rect, "net", "") or "")
        layer = str(getattr(rect, "layer", "") or "")
        if not net or not layer:
            continue
        # Use the horizontal trunk rather than every vertical stem.
        bbox = tuple(float(value) for value in getattr(rect, "bbox", (0, 0, 0, 0)))
        if (bbox[2] - bbox[0]) <= 2.0 * max((bbox[3] - bbox[1]), 1e-9):
            continue
        key = (
            str(metadata.get("source_instance", "")),
            str(metadata.get("terminal", "")),
            layer,
            net,
        )
        if key in seen:
            continue
        seen.add(key)
        labels.append((layer, net, ((bbox[0] + bbox[2]) * 0.5, (bbox[1] + bbox[3]) * 0.5)))
    return tuple(labels)


def _assist_point_has_same_net_access(
    point: tuple[float, float],
    *,
    net: str,
    layer: str,
    existing_rects: tuple[object, ...],
    existing_paths: tuple[object, ...],
) -> bool:
    x, y = float(point[0]), float(point[1])
    for rect in existing_rects:
        if str(getattr(rect, "net", "") or "") != net or str(getattr(rect, "layer", "") or "") != layer:
            continue
        x0, y0, x1, y1 = tuple(float(value) for value in getattr(rect, "bbox", (0, 0, 0, 0)))
        if x0 <= x <= x1 and y0 <= y <= y1:
            return True
    for path in existing_paths:
        if str(getattr(path, "net", "") or "") != net or str(getattr(path, "layer", "") or "") != layer:
            continue
        for bbox in _assist_path_bboxes(path):
            if bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]:
                return True
    return False


def _assist_calibrated_sd_points(pdk: object, instance: object, accessor: object | None) -> dict[str, tuple[tuple[float, float], ...]]:
    if accessor is None:
        return {}
    metal1 = str(tuple(getattr(getattr(pdk, "layer_map", object()), "metals", ("M1",)) or ("M1",))[0])
    rows: dict[str, tuple[tuple[float, float], ...]] = {}
    for terminal in ("S", "D"):
        try:
            pins = tuple(accessor.get_terminal_pins(instance, terminal, preferred_layers=(metal1,)))  # type: ignore[attr-defined]
        except Exception:
            pins = ()
        points = tuple(
            getattr(pin, "xy_um")
            for pin in pins
            if str(getattr(pin, "layer", "")) == metal1 and getattr(pin, "xy_um", None) is not None
        )
        points = tuple(pdk.rules.snap_point_um((float(point[0]), float(point[1]))) for point in points)
        unique = tuple(dict.fromkeys(points))
        if unique:
            rows[terminal] = tuple(sorted(unique, key=lambda point: (point[0], point[1])))
    if not rows.get("S") or not rows.get("D"):
        return {}
    return rows


def _assist_fallback_sd_points(pdk: object, instance: object, *, nf: int) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]:
    params = dict(getattr(instance, "params", {}) or {})
    wfg_um = _assist_wfg_um(params)
    origin = tuple(float(value) for value in tuple(getattr(instance, "xy_um", getattr(instance, "xy", (0.0, 0.0))))[:2])
    orient = str(getattr(instance, "orient", "R0") or "R0")
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    access = metadata.get("pcell_access", {}) if isinstance(metadata.get("pcell_access", {}), Mapping) else {}
    strap = access.get("multifinger_gate_strap", {}) if isinstance(access.get("multifinger_gate_strap", {}), Mapping) else {}
    try:
        pitch_um = max(float(strap.get("pitch_nm", 300) or 300), 1.0) * 1e-3
    except (TypeError, ValueError):
        pitch_um = 0.3
    mirrored = "MY" in orient
    base_dx = 0.06 if mirrored else -0.06
    step = -pitch_um if mirrored else pitch_um
    y_center = origin[1] + 0.5 * wfg_um
    points = tuple(
        pdk.rules.snap_point_um((origin[0] + base_dx + index * step, y_center))
        for index in range(max(2, int(nf) + 1))
    )
    source_points = tuple(point for index, point in enumerate(points) if index % 2 == 0)
    drain_points = tuple(point for index, point in enumerate(points) if index % 2 == 1)
    return source_points, drain_points


def _assist_lower_half_sd_points(
    pdk: object,
    instance: object,
    points: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    """Move LVS-assist S/D contacts away from routable center taps.

    Native CRN28 MOS PCells expose full-height M1 source/drain columns.  The
    center access point is often also used by the real top-level TAIL/OUT/VDD
    route.  Adding LVS-assist VIA1s at exactly that center point can make the
    foundry extractor merge the assist net with unrelated over-passing routes.
    For the assist artifact we can connect to the same M1 columns in the lower
    half of the diffusion instead; this preserves multi-finger recognition while
    avoiding the real route access hot spot.
    """

    if not points:
        return points
    params = dict(getattr(instance, "params", {}) or {})
    wfg_um = _assist_wfg_um(params)
    if wfg_um <= 0.0:
        return points
    origin = tuple(float(value) for value in tuple(getattr(instance, "xy_um", getattr(instance, "xy", (0.0, 0.0))))[:2])
    min_width = float(getattr(pdk.rules, "min_width_um", lambda _layer: 0.05)("M1"))
    grid = float(getattr(pdk.rules, "grid_step_um", 0.001) or 0.001)
    margin = max(0.08, 2.0 * min_width, 10.0 * grid)
    if 2.0 * margin >= wfg_um:
        margin = max(2.0 * grid, 0.2 * wfg_um)
    target_y = origin[1] + margin
    upper = origin[1] + max(margin, wfg_um - margin)
    target_y = min(max(target_y, origin[1] + margin), upper)
    target_y = pdk.rules.snap_point_um((points[0][0], target_y))[1]
    return tuple(pdk.rules.snap_point_um((float(point[0]), target_y)) for point in points)


def _assist_sd_strap_layer(pdk: object) -> str:
    metals = tuple(getattr(getattr(pdk, "layer_map", object()), "metals", ()) or ())
    return str(metals[1] if len(metals) > 1 else (metals[0] if metals else "M2"))


def _assist_sd_strap_via(pdk: object) -> str:
    vias = tuple(getattr(getattr(pdk, "layer_map", object()), "vias", ()) or ())
    return str(vias[0] if vias else "VIA1")


def _assist_choose_sd_trunk_y(
    pdk: object,
    points: tuple[tuple[float, float], ...],
    *,
    terminal: str,
    net: str,
    layer: str,
    existing_rects: tuple[object, ...],
    existing_paths: tuple[object, ...],
    prefer_below: bool = False,
) -> float:
    y = points[0][1]
    min_width = pdk.rules.min_width_um(layer)
    spacing = _assist_spacing_um(pdk, layer)
    base = pdk.rules.snap_dimension_ceil_um(max(2.0 * min_width + spacing, 0.16))
    if prefer_below:
        offsets = (-base, -2.0 * base, -3.0 * base, -4.0 * base, base, 2.0 * base)
    elif str(terminal) == "S":
        offsets = (-base, base, -1.5 * base, 1.5 * base, -2.0 * base, 2.0 * base)
    else:
        offsets = (base, -base, 1.5 * base, -1.5 * base, 2.0 * base, -2.0 * base)
    for offset in offsets:
        candidate_y = pdk.rules.snap_point_um((points[0][0], y + offset))[1]
        if not _assist_sd_bboxes_conflict(
            pdk,
            _assist_sd_strap_bboxes(pdk, points, candidate_y, layer=layer),
            net=net,
            layer=layer,
            existing_rects=existing_rects,
            existing_paths=existing_paths,
        ):
            return candidate_y
    return pdk.rules.snap_point_um((points[0][0], y + offsets[0]))[1]


def _assist_sd_strap_bboxes(
    pdk: object,
    points: tuple[tuple[float, float], ...],
    trunk_y: float,
    *,
    layer: str,
) -> tuple[tuple[float, float, float, float], ...]:
    trunk = _assist_sd_trunk_bbox(pdk, points, trunk_y, layer=layer)
    stems = tuple(_assist_sd_stem_bbox(pdk, point, trunk_y, layer=layer) for point in points)
    return (trunk, *stems)


def _assist_sd_trunk_bbox(pdk: object, points: tuple[tuple[float, float], ...], trunk_y: float, *, layer: str) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    min_width = pdk.rules.min_width_um(layer)
    half_y = 0.5 * min_width
    min_area_um2 = float(getattr(pdk.rules, "min_area_nm2", {}).get(layer, 0.0) or 0.0) * 1e-6
    width = max(max(xs) - min(xs), min_width)
    if min_area_um2 > 0.0 and width > 0.0:
        half_y = max(half_y, 0.5 * min_area_um2 / width)
    half_y = pdk.rules.snap_dimension_ceil_um(half_y)
    half_x = max(0.5 * min_width, 0.025)
    return pdk.rules.snap_bbox_um((min(xs) - half_x, trunk_y - half_y, max(xs) + half_x, trunk_y + half_y), mode="outward")


def _assist_sd_stem_bbox(pdk: object, point: tuple[float, float], trunk_y: float, *, layer: str) -> tuple[float, float, float, float]:
    min_width = pdk.rules.min_width_um(layer)
    half_x = pdk.rules.snap_dimension_ceil_um(0.5 * min_width)
    y0, y1 = sorted((point[1], trunk_y))
    half_y = max(0.5 * min_width, 0.025)
    return pdk.rules.snap_bbox_um((point[0] - half_x, y0 - half_y, point[0] + half_x, y1 + half_y), mode="outward")


def _assist_sd_bboxes_conflict(
    pdk: object,
    bboxes: tuple[tuple[float, float, float, float], ...],
    *,
    net: str,
    layer: str,
    existing_rects: tuple[object, ...],
    existing_paths: tuple[object, ...],
) -> bool:
    spacing = _assist_spacing_um(pdk, layer)
    for bbox in bboxes:
        padded = (bbox[0] - spacing, bbox[1] - spacing, bbox[2] + spacing, bbox[3] + spacing)
        for rect in existing_rects:
            if str(getattr(rect, "layer", "")) != layer:
                continue
            other_net = str(getattr(rect, "net", "") or "")
            if other_net == net:
                continue
            if _assist_bbox_overlaps(padded, tuple(float(value) for value in getattr(rect, "bbox", (0, 0, 0, 0)))):
                return True
        for path in existing_paths:
            if str(getattr(path, "layer", "")) != layer:
                continue
            other_net = str(getattr(path, "net", "") or "")
            if other_net == net:
                continue
            for path_bbox in _assist_path_bboxes(path):
                if _assist_bbox_overlaps(padded, path_bbox):
                    return True
    return False


def _assist_path_bboxes(path: object) -> tuple[tuple[float, float, float, float], ...]:
    points = tuple(getattr(path, "points", ()) or ())
    half_width = 0.5 * float(getattr(path, "width", 0.0) or 0.0)
    rows: list[tuple[float, float, float, float]] = []
    for left, right in zip(points, points[1:]):
        x0, y0 = float(left[0]), float(left[1])
        x1, y1 = float(right[0]), float(right[1])
        rows.append((min(x0, x1) - half_width, min(y0, y1) - half_width, max(x0, x1) + half_width, max(y0, y1) + half_width))
    return tuple(rows)


def _assist_bbox_overlaps(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    return not (left[2] <= right[0] or right[2] <= left[0] or left[3] <= right[1] or right[3] <= left[1])


def _assist_spacing_um(pdk: object, layer: str) -> float:
    try:
        return pdk.rules.min_spacing_um(layer)
    except Exception:
        return 0.0


def _assist_gate_contact_geometry(
    pdk: object,
    net: str,
    xy: tuple[float, float],
    *,
    source_instance: str,
    finger_index: int,
    fingers: int,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    from analogskills.eda import OaRect, OaVia

    xy = pdk.rules.snap_point_um(xy)
    po_bbox = _assist_contact_landing_bbox(pdk, xy, "PO")
    m1_bbox = _assist_contact_landing_bbox(pdk, xy, pdk.layer_map.metals[0])
    metadata = {
        "kind": "lvs_assist_gate_contact_array",
        "source_instance": source_instance,
        "finger_index": finger_index,
        "fingers": fingers,
        "lvs_assist_only": True,
    }
    rects = (
        OaRect("PO", "drawing", po_bbox, net, metadata=metadata),
        OaRect(pdk.layer_map.metals[0], "drawing", m1_bbox, net, metadata=metadata),
    )
    vias = (
        OaVia(pdk.layer_map.contact, xy, net, metadata=metadata),
    )
    return rects, vias


def _assist_contact_landing_bbox(pdk: object, xy: tuple[float, float], layer: str) -> tuple[float, float, float, float]:
    via_def = pdk.layer_map.contact
    half = 0.5 * pdk.rules.min_width_um(via_def)
    for key in (f"{via_def}_{layer}", f"{layer}_{via_def}"):
        if key in pdk.rules.enclosure_nm:
            half = max(half, 0.5 * pdk.rules.min_width_um(via_def) + float(pdk.rules.enclosure_nm[key]) * 1e-3)
    half = max(half, 0.5 * pdk.rules.min_width_um(layer))
    min_area_um2 = float(getattr(pdk.rules, "min_area_nm2", {}).get(layer, 0.0) or 0.0) * 1e-6
    if min_area_um2 > 0.0:
        half = max(half, 0.5 * sqrt(min_area_um2))
    half = pdk.rules.snap_dimension_ceil_um(half)
    x, y = xy
    return pdk.rules.snap_bbox_um((x - half, y - half, x + half, y + half), mode="outward")


def _assist_m1_bridge_bbox(pdk: object, points: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    y = points[0][1]
    landing = _assist_contact_landing_bbox(pdk, points[0], pdk.layer_map.metals[0])
    half_y = max((landing[3] - landing[1]) * 0.5, 0.5 * pdk.rules.min_width_um(pdk.layer_map.metals[0]))
    min_area_um2 = float(getattr(pdk.rules, "min_area_nm2", {}).get(pdk.layer_map.metals[0], 0.0) or 0.0) * 1e-6
    width = max(max(xs) - min(xs), pdk.rules.min_width_um(pdk.layer_map.metals[0]))
    if min_area_um2 > 0.0 and width > 0.0:
        half_y = max(half_y, 0.5 * min_area_um2 / width)
    half_y = pdk.rules.snap_dimension_ceil_um(half_y)
    half_x = 0.5 * (landing[2] - landing[0])
    return pdk.rules.snap_bbox_um((min(xs) - half_x, y - half_y, max(xs) + half_x, y + half_y), mode="outward")


def _assist_poly_bridge_bbox(pdk: object, points: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    y = points[0][1]
    layer = "PO"
    min_width = pdk.rules.min_width_um(layer)
    half_y = 0.5 * min_width
    min_area_um2 = float(getattr(pdk.rules, "min_area_nm2", {}).get(layer, 0.0) or 0.0) * 1e-6
    width = max(max(xs) - min(xs), min_width)
    if min_area_um2 > 0.0 and width > 0.0:
        half_y = max(half_y, 0.5 * min_area_um2 / width)
    half_y = pdk.rules.snap_dimension_ceil_um(half_y)
    half_x = max(0.5 * min_width, 0.025)
    return pdk.rules.snap_bbox_um((min(xs) - half_x, y - half_y, max(xs) + half_x, y + half_y), mode="outward")


def _oa_plan_shapes_for_feedback(plan: object) -> tuple[object, ...]:
    """Lower generated OA rectangles/paths to the repair shape subset."""

    from analogskills.repair import LayoutShape

    rows: list[LayoutShape] = []
    for index, rect in enumerate(tuple(getattr(plan, "rects", ()) or ())):
        rows.append(LayoutShape(f"rect[{index}]", str(rect.layer), tuple(float(value) for value in rect.bbox), str(rect.net)))
    for path_index, path in enumerate(tuple(getattr(plan, "paths", ()) or ())):
        half_width = float(path.width) / 2.0
        for segment_index, (left, right) in enumerate(zip(path.points, path.points[1:])):
            rows.append(LayoutShape(
                f"path[{path_index}].segment[{segment_index}]", str(path.layer),
                (min(left[0], right[0]) - half_width, min(left[1], right[1]) - half_width,
                 max(left[0], right[0]) + half_width, max(left[1], right[1]) + half_width), str(path.net),
            ))
    return tuple(rows)


def _apply_configured_lvs_source_dimension_adjustments(
    graph: TopologyGraph,
    sizing: Mapping[str, Mapping[str, object]],
    pdk: object,
) -> dict[str, dict[str, object]]:
    """Return source-CDL-only MOS sizing adjusted to calibrated LVS extraction.

    Native foundry PCells can extract slightly different W/L than the nominal
    schematic parameters.  This adjustment is intentionally applied only to the
    LVS source netlist; it does not change the layout/PCell generation sizing.
    """

    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    calibre = metadata.get("calibre", {}) if isinstance(metadata.get("calibre", {}), Mapping) else {}
    lvs_config = calibre.get("lvs", {}) if isinstance(calibre.get("lvs", {}), Mapping) else {}
    config = lvs_config.get("source_mos_dimension_adjustment", {}) if isinstance(lvs_config.get("source_mos_dimension_adjustment", {}), Mapping) else {}
    if not bool(config.get("enabled", False)):
        return {name: dict(values) for name, values in sizing.items()}

    model_alias = config.get("model_alias", {}) if isinstance(config.get("model_alias", {}), Mapping) else {}
    length_tables = config.get("length_by_model_wfg_nm", {}) if isinstance(config.get("length_by_model_wfg_nm", {}), Mapping) else {}
    try:
        width_add_m = float(config.get("width_add_nm", 0.0) or 0.0) * 1e-9
    except (TypeError, ValueError):
        width_add_m = 0.0

    adjusted: dict[str, dict[str, object]] = {name: dict(values) for name, values in sizing.items()}
    for device in graph.devices.values():
        if not all(term in device.terminals for term in ("D", "G", "S", "B")):
            continue
        params: dict[str, object] = dict(getattr(device, "parameters", {}) or {})
        params.update(dict(sizing.get(device.name, {}) or {}))
        try:
            width_m = _configured_dimension_m(params, ("W", "w", "width"), 1e-6)
            nf = _configured_positive_int(params, ("nf", "fingers"), 1)
        except (TypeError, ValueError):
            continue
        if nf <= 0:
            continue
        model = str(getattr(device, "model", "") or "")
        model = str(model_alias.get(model, model) or model)
        device_adjusted = adjusted.setdefault(device.name, dict(sizing.get(device.name, {}) or {}))
        if width_add_m:
            _set_dimension_aliases_m(device_adjusted, ("W", "w", "width"), width_m + width_add_m)
        length_m = _lookup_configured_lvs_length_m(length_tables.get(model), width_m * 1e9 / float(nf))
        if length_m is not None:
            _set_dimension_aliases_m(device_adjusted, ("L", "l", "length"), length_m)
    return adjusted


def _configured_dimension_m(params: Mapping[str, object], keys: tuple[str, ...], default_m: float) -> float:
    for key in keys:
        if key in params:
            return _configured_dimension_value_m(params[key], "auto")
        um_key = f"{key}_um"
        if um_key in params:
            return _configured_dimension_value_m(params[um_key], "um")
        nm_key = f"{key}_nm"
        if nm_key in params:
            return _configured_dimension_value_m(params[nm_key], "nm")
    return default_m


def _configured_dimension_value_m(value: object, unit: str) -> float:
    number = float(value)  # type: ignore[arg-type]
    if unit == "um":
        return number * 1e-6
    if unit == "nm":
        return number * 1e-9
    if abs(number) < 1e-3:
        return number
    return number * 1e-6


def _configured_positive_int(params: Mapping[str, object], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        if key not in params:
            continue
        value = int(float(params[key]))  # type: ignore[arg-type]
        if value > 0:
            return value
    return default


def _set_dimension_aliases_m(params: dict[str, object], keys: tuple[str, ...], value_m: float) -> None:
    primary = keys[0]
    params[primary] = value_m
    for key in keys[1:]:
        if key in params:
            params[key] = value_m
    for key in keys:
        um_key = f"{key}_um"
        if um_key in params:
            params[um_key] = value_m * 1e6
        nm_key = f"{key}_nm"
        if nm_key in params:
            params[nm_key] = value_m * 1e9


def _lookup_configured_lvs_length_m(table: object, wfg_nm: float) -> float | None:
    if not isinstance(table, (list, tuple)):
        return None
    points: list[tuple[float, float]] = []
    for item in table:
        if not isinstance(item, Mapping):
            continue
        try:
            x = float(item.get("wfg_nm"))
            y = float(item.get("l_nm"))
        except (TypeError, ValueError):
            continue
        points.append((x, y))
    if not points:
        return None
    points.sort()
    for x, y in points:
        if abs(wfg_nm - x) <= 1e-6:
            return y * 1e-9
    if wfg_nm <= points[0][0]:
        return points[0][1] * 1e-9
    if wfg_nm >= points[-1][0]:
        return points[-1][1] * 1e-9
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= wfg_nm <= x1:
            if x1 == x0:
                return y0 * 1e-9
            ratio = (wfg_nm - x0) / (x1 - x0)
            return (y0 + ratio * (y1 - y0)) * 1e-9
    return None


def _apply_configured_lvs_deck_rewrites(deck_text: str, calibre_metadata: Mapping[str, object]) -> str:
    lvs_config = calibre_metadata.get("lvs", {}) if isinstance(calibre_metadata.get("lvs", {}), Mapping) else {}
    rewritten = deck_text
    if bool(lvs_config.get("enable_multifinger", False)):
        rewritten = rewritten.replace("//#define MULTI_FINGER", "#define MULTI_FINGER")
    property_tolerances = lvs_config.get("property_tolerances", {}) if isinstance(lvs_config.get("property_tolerances", {}), Mapping) else {}
    for name, value in property_tolerances.items():
        if not str(name).replace("_", "").isalnum():
            continue
        try:
            numeric = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        pattern = re.compile(rf"(^\s*VARIABLE\s+{re.escape(str(name))}\s+)(\S+)(.*$)", re.MULTILINE)
        rewritten = pattern.sub(lambda match, numeric=numeric: f"{match.group(1)}{numeric:g}{match.group(3)}", rewritten, count=1)
    for layer in tuple(lvs_config.get("streamout_text_port_layers", ()) or ()):
        try:
            layer_id = int(layer)
        except (TypeError, ValueError):
            continue
        statement = f"PORT LAYER TEXT {layer_id}"
        if statement in rewritten:
            continue
        needle = f"TEXT LAYER {layer_id} ATTACH"
        if needle in rewritten:
            rewritten = rewritten.replace(needle, statement + "\n" + needle, 1)
    return rewritten


def _apply_configured_oa_path_rewrites(plan: object, pdk: object) -> object:
    from dataclasses import replace
    from typing import Mapping

    from analogskills.eda import OaPath, OaWritePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    routing_geometry = metadata.get("routing_geometry", {}) if isinstance(metadata.get("routing_geometry", {}), Mapping) else {}
    config = routing_geometry.get("strongarm_path_rewrites", {}) if isinstance(routing_geometry.get("strongarm_path_rewrites", {}), Mapping) else {}
    if not bool(config.get("enabled", False)):
        return _apply_configured_oa_rect_rewrites(plan, pdk)
    rules = getattr(pdk, "rules", None)
    paths: list[object | None] = list(tuple(getattr(plan, "paths", ()) or ()))
    used: set[int] = set()
    for item in tuple(config.get("entries", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        net = str(item.get("net", "") or "")
        layer = str(item.get("layer", "") or "")
        match_points = _configured_path_points(item.get("match_points_um", ()), rules)
        replacement_points = _configured_path_points(item.get("replacement_points_um", ()), rules)
        remove = bool(item.get("remove", False))
        if not net or not layer or len(match_points) < 2 or (not remove and len(replacement_points) < 2):
            continue
        match_tolerance_um = _configured_match_tolerance_um(item.get("match_tolerance_nm", config.get("match_tolerance_nm", 2.0)), rules)
        for index, path in enumerate(paths):
            if index in used:
                continue
            if path is None:
                continue
            if str(getattr(path, "net", "") or "") != net or str(getattr(path, "layer", "") or "") != layer:
                continue
            current_points = _configured_path_points(getattr(path, "points", ()), rules)
            if not _configured_path_points_match(current_points, match_points, match_tolerance_um):
                continue
            if remove:
                paths[index] = None
                used.add(index)
                break
            paths[index] = OaPath(
                str(getattr(path, "layer", layer)),
                str(getattr(path, "purpose", "drawing")),
                replacement_points,
                float(getattr(path, "width", 0.0) or 0.0),
                str(getattr(path, "net", net)),
                str(getattr(path, "color", "")),
            )
            used.add(index)
            break
    if not used:
        rewritten = plan
    else:
        rewritten = replace(plan, paths=tuple(path for path in paths if path is not None))
    return _apply_configured_oa_rect_rewrites(rewritten, pdk)


def _apply_configured_oa_rect_rewrites(plan: object, pdk: object) -> object:
    from dataclasses import replace
    from typing import Mapping

    from analogskills.eda import OaRect, OaWritePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    routing_geometry = metadata.get("routing_geometry", {}) if isinstance(metadata.get("routing_geometry", {}), Mapping) else {}
    config = routing_geometry.get("strongarm_rect_rewrites", {}) if isinstance(routing_geometry.get("strongarm_rect_rewrites", {}), Mapping) else {}
    if not bool(config.get("enabled", False)):
        return plan
    rules = getattr(pdk, "rules", None)
    rects: list[object | None] = list(tuple(getattr(plan, "rects", ()) or ()))
    used: set[int] = set()
    for item in tuple(config.get("entries", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        net = str(item.get("net", "") or "")
        layer = str(item.get("layer", "") or "")
        match_bbox = _configured_bbox(item.get("match_bbox_um", ()), rules)
        replacement_bbox = _configured_bbox(item.get("replacement_bbox_um", ()), rules)
        remove = bool(item.get("remove", False))
        try:
            max_matches = int(item.get("max_matches", 1) or 1)
        except (TypeError, ValueError):
            max_matches = 1
        if not net or not layer or len(match_bbox) != 4 or (not remove and len(replacement_bbox) != 4) or max_matches <= 0:
            continue
        matched = 0
        for index, rect in enumerate(rects):
            if index in used or rect is None:
                continue
            if str(getattr(rect, "net", "") or "") != net or str(getattr(rect, "layer", "") or "") != layer:
                continue
            current_bbox = _configured_bbox(getattr(rect, "bbox", ()), rules)
            if current_bbox != match_bbox:
                continue
            if remove:
                rects[index] = None
            else:
                rects[index] = OaRect(
                    str(getattr(rect, "layer", layer)),
                    str(getattr(rect, "purpose", "drawing")),
                    replacement_bbox,
                    str(getattr(rect, "net", net)),
                    str(getattr(rect, "color", "")),
                    dict(getattr(rect, "metadata", {}) or {}),
                )
            used.add(index)
            matched += 1
            if matched >= max_matches:
                break
    if not used:
        return plan
    return replace(plan, rects=tuple(rect for rect in rects if rect is not None))


def _apply_configured_oa_via_rewrites(plan: object, pdk: object) -> object:
    from dataclasses import replace
    from typing import Mapping

    from analogskills.eda import OaVia, OaWritePlan

    if not isinstance(plan, OaWritePlan):
        return plan
    metadata = getattr(pdk, "metadata", {}) if isinstance(getattr(pdk, "metadata", {}), Mapping) else {}
    routing_geometry = metadata.get("routing_geometry", {}) if isinstance(metadata.get("routing_geometry", {}), Mapping) else {}
    config = routing_geometry.get("strongarm_via_rewrites", {}) if isinstance(routing_geometry.get("strongarm_via_rewrites", {}), Mapping) else {}
    if not bool(config.get("enabled", False)):
        return plan
    rules = getattr(pdk, "rules", None)
    vias = list(tuple(getattr(plan, "vias", ()) or ()))
    used: set[int] = set()
    for item in tuple(config.get("entries", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        net = str(item.get("net", "") or "")
        via_def = str(item.get("via_def", "") or "")
        match_xy = _configured_point(item.get("xy_um", ()), rules)
        replacement_xy = _configured_point(item.get("replacement_xy_um", match_xy), rules)
        if not net or not via_def or len(match_xy) != 2 or len(replacement_xy) != 2:
            continue
        try:
            rows = int(item.get("rows", 1) or 1)
            cols = int(item.get("cols", 1) or 1)
        except (TypeError, ValueError):
            continue
        if rows <= 0 or cols <= 0:
            continue
        match_tolerance_um = _configured_match_tolerance_um(item.get("match_tolerance_nm", config.get("match_tolerance_nm", 2.0)), rules)
        for index, via in enumerate(vias):
            if index in used:
                continue
            if str(getattr(via, "net", "") or "") != net or str(getattr(via, "via_def", "") or "") != via_def:
                continue
            current_xy = _configured_point(getattr(via, "xy", ()), rules)
            if len(current_xy) != 2:
                continue
            if abs(current_xy[0] - match_xy[0]) > match_tolerance_um or abs(current_xy[1] - match_xy[1]) > match_tolerance_um:
                continue
            via_metadata = dict(getattr(via, "metadata", {}) or {})
            if bool(item.get("emit_cut_array", False)):
                via_metadata["emit_cut_array"] = True
            if bool(item.get("force_oa_via", False)):
                via_metadata["force_oa_via"] = True
            via_metadata.update(
                {
                    "source": str(item.get("source", "configured_via_rewrite") or "configured_via_rewrite"),
                    "rule_ids": tuple(str(value) for value in tuple(item.get("rule_ids", ()) or ()) if str(value)),
                }
            )
            vias[index] = OaVia(via_def, replacement_xy, net, rows=rows, cols=cols, metadata=via_metadata)
            used.add(index)
            break
    if not used:
        return plan
    return replace(plan, vias=tuple(vias))


def _configured_path_points(points: object, rules: object | None) -> tuple[tuple[float, float], ...]:
    try:
        parsed = tuple((float(point[0]), float(point[1])) for point in tuple(points or ()))  # type: ignore[index]
    except (TypeError, ValueError, IndexError):
        return ()
    if rules is None:
        return parsed
    try:
        return tuple(rules.snap_point_um(point) for point in parsed)
    except AttributeError:
        return parsed


def _configured_point(point: object, rules: object | None) -> tuple[float, ...]:
    try:
        parsed = tuple(float(value) for value in tuple(point or ()))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ()
    if len(parsed) != 2 or rules is None:
        return parsed
    try:
        return tuple(float(value) for value in rules.snap_point_um(parsed))  # type: ignore[union-attr]
    except AttributeError:
        return parsed


def _configured_match_tolerance_um(value: object, rules: object | None) -> float:
    try:
        tolerance_um = float(value) * 1e-3
    except (TypeError, ValueError):
        tolerance_um = 0.002
    if tolerance_um < 0:
        tolerance_um = 0.0
    try:
        grid_um = float(getattr(rules, "grid_step_um"))
    except (TypeError, ValueError, AttributeError):
        grid_um = 0.001
    return max(tolerance_um, grid_um * 1.5)


def _configured_path_points_match(
    current_points: tuple[tuple[float, float], ...],
    match_points: tuple[tuple[float, float], ...],
    tolerance_um: float,
) -> bool:
    if len(current_points) != len(match_points):
        return False
    for current, target in zip(current_points, match_points):
        if abs(current[0] - target[0]) > tolerance_um or abs(current[1] - target[1]) > tolerance_um:
            return False
    return True


def _configured_bbox(bbox: object, rules: object | None) -> tuple[float, ...]:
    try:
        parsed = tuple(float(value) for value in tuple(bbox or ()))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ()
    if len(parsed) != 4 or rules is None:
        return parsed
    try:
        return tuple(float(value) for value in rules.snap_bbox_um(parsed, mode="nearest"))  # type: ignore[union-attr]
    except AttributeError:
        return parsed


def lower_strongarm_smt_device_placements(
    physical: HierarchicalPhysicalSolution2D,
    *,
    track_pitch_um: float = 0.5,
) -> tuple[Placement, ...]:
    """Map group-level SMT boxes to symmetric device-level PCell origins."""

    if track_pitch_um <= 0.0:
        raise ValueError("track_pitch_um must be positive")
    groups = physical.master.placements

    def point(group_name: str, dx: float, dy: float) -> tuple[float, float]:
        group = groups[group_name]
        return ((group.x_tracks + dx) * track_pitch_um, (group.y_tracks + dy) * track_pitch_um)

    tail = groups["tail_switch"]
    input_group = groups["input_pair"]
    latch = groups["regenerative_latch"]
    reset = groups["reset"]
    positions = {
        "MCLK": point("tail_switch", tail.width_tracks / 2.0, 0.4),
        "MIN_P": point("input_pair", input_group.width_tracks / 2.0 - 1.5, 0.5),
        "MIN_N": point("input_pair", input_group.width_tracks / 2.0 + 1.5, 0.5),
        "MLATN_P": point("regenerative_latch", latch.width_tracks / 2.0 - 2.0, 0.5),
        "MLATN_N": point("regenerative_latch", latch.width_tracks / 2.0 + 2.0, 0.5),
        "MLATP_P": point("regenerative_latch", latch.width_tracks / 2.0 - 2.0, 2.7),
        "MLATP_N": point("regenerative_latch", latch.width_tracks / 2.0 + 2.0, 2.7),
        "MRST_P": point("reset", reset.width_tracks / 2.0 - 1.5, 0.5),
        "MRST_N": point("reset", reset.width_tracks / 2.0 + 1.5, 0.5),
    }
    mirrored = {"MIN_N", "MLATN_N", "MLATP_N", "MRST_N"}
    return tuple(
        Placement(name, xy[0], xy[1], orient="MY" if name in mirrored else "R0", role=name)
        for name, xy in positions.items()
    )


def _route_vertical_diff_segment(
    grid: Grid,
    name: str,
    logical_nets: tuple[str, str],
    corridor: str,
    bbox: tuple[int, int, int, int],
) -> ComparatorRouteSegment:
    x0, y0, x1, y1 = bbox
    if x1 - x0 < 2 or y1 <= y0:
        raise ValueError(f"corridor {corridor} is too small for differential routing: {bbox}")
    left_x = x0
    right_x = x0 + 1
    result = route_coupled_differential_pair(
        grid,
        (left_x, y0),
        (left_x, y1),
        (right_x, y0),
        (right_x, y1),
        net_names=logical_nets,
        layer="M3",
        width_nm=160,
    )
    return ComparatorRouteSegment(name, logical_nets, corridor, result.routes)


def _route_vertical_scalar(
    grid: Grid,
    net: str,
    bbox: tuple[int, int, int, int],
    *,
    layer: str,
    lane: int,
    width_nm: int | None = None,
) -> RoutedNet:
    x0, y0, x1, y1 = bbox
    x = x0 + lane if lane >= 0 else x1 + lane
    if not (x0 <= x < x1):
        x = (x0 + x1 - 1) // 2
    points = route_astar_costed(grid, (x, y0), (x, y1), bend_cost=0.2)
    return RoutedNet.from_points(net, points, layer=layer, width_nm=width_nm)


def _strongarm_flow_checks(
    graph: TopologyGraph,
    physical: HierarchicalPhysicalSolution2D,
    segments: tuple[ComparatorRouteSegment, ...],
    noncritical: tuple[RoutedNet, ...],
    mesh: PowerMeshResult,
    corridor_boxes: Mapping[str, tuple[int, int, int, int]],
) -> dict[str, object]:
    issues: list[str] = []
    required_devices = {"MIN_P", "MIN_N", "MLATN_P", "MLATN_N", "MLATP_P", "MLATP_N", "MCLK", "MRST_P", "MRST_N"}
    if set(graph.devices) != required_devices:
        issues.append("StrongARM device set mismatch")
    if not physical.converged or not physical.routing.passed:
        issues.append("hierarchical physical solve did not close routing capacity")
    for segment in segments:
        if len(segment.routes) != 2:
            issues.append(f"segment {segment.name} is not differential")
            continue
        left, right = segment.routes
        if abs(route_length(left.points) - route_length(right.points)) > 1e-9:
            issues.append(f"segment {segment.name} length mismatch")
        if not _routes_within_bbox(segment.routes, corridor_boxes[segment.corridor]):
            issues.append(f"segment {segment.name} escapes corridor {segment.corridor}")
    if {route.net for route in noncritical} != {"CLK", "RST", "TAIL"}:
        issues.append("noncritical comparator route set mismatch")
    if not mesh.passed:
        issues.extend(mesh.issues)
    routed_nets = {net for segment in segments for net in segment.logical_nets} | {route.net for route in noncritical} | set(mesh.widths_nm)
    required_routed = {"INP", "INN", "OUTP", "OUTN", "TAIL", "CLK", "RST", "VDD", "VSS"}
    missing = sorted(required_routed - routed_nets)
    if missing:
        issues.append(f"missing routed logical nets {missing}")
    return {
        "passed": not issues,
        "issues": tuple(issues),
        "device_count": len(graph.devices),
        "net_count": len(graph.nets),
        "placement_group_count": len(physical.master.placements),
        "refinement_iterations": len(physical.iterations),
        "critical_segment_count": len(segments),
        "power_route_count": len(mesh.routes),
        "power_via_count": len(mesh.vias),
        "routed_logical_nets": tuple(sorted(routed_nets)),
    }


def _routes_within_bbox(routes: tuple[RoutedNet, ...], bbox: tuple[int, int, int, int]) -> bool:
    x0, y0, x1, y1 = bbox
    return all(x0 <= x <= x1 and y0 <= y <= y1 for route in routes for x, y in route.points)


def _shift_bbox(bbox: tuple[int, int, int, int], amount: int) -> tuple[int, int, int, int]:
    return tuple(value + amount for value in bbox)  # type: ignore[return-value]
